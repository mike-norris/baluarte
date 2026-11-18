"""The auditor report: a readable PDF of one framework's controls over an audit period (BAL-042).

The report is read from the same snapshot as the audit-period evidence package
(:class:`baluarte.evidence.package.EvidencePackage`), so it is as of the period's end in exactly
the package's sense: the evaluation run that finished last by ``to`` (none is 409
``no_evaluation_run``), the monitoring scope and control mapping in force then, and the control
posture (:func:`baluarte.controls.posture.posture`) computed from that run over the projects in
scope. It holds:

1. A cover: framework, period, GitLab host, evaluation run, report ID, generated at, Baluarte
   version, requester (``user:<id>``) and the statement that the mapping is guidance.
2. A summary: controls by status, rule types evaluated, projects monitored and excluded, and
   collection freshness at ``to``.
3. Every control of the framework with its status, mapped rule types and result counts.
4. The failing project, baseline and rule of each not-met control (at most ``MAX_FAILING``).
5. Policy exceptions and operating-profile exceptions in force at some point in the period.
6. The organization's active not-applicable and manual mapping decisions.
7. Operating-profile attestations current at ``to``.
8. The exclusions from monitoring in force at ``to``.
9. An appendix: the baseline versions the run judged, the mapping version, and how to check the
   report against the evidence package.

Every free-text value passes through the log redactor, as in exports, and is escaped before it
reaches a reportlab ``Paragraph``, which would otherwise read it as markup. Text is set in DejaVu
Sans (shipped in ``fonts/`` with its licence), which covers Latin, Greek, Cyrillic and many other
scripts; a character it lacks (CJK, for example) prints as an empty box rather than failing.

The PDF is deterministic: reportlab's invariant mode fixes the creation date and document ID, so
the same evidence, scope and period give the same bytes apart from the report ID and the
generation time.
"""

import io
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import aliased

from baluarte import __version__
from baluarte.compliance import catalogue as operating
from baluarte.compliance.catalogue import FRAMEWORKS, FrameworkId
from baluarte.controls import catalogue as control_catalogue
from baluarte.controls.posture import ControlStatus, posture
from baluarte.controls.posture import effective as effective_mapping
from baluarte.db.models import (
    ComplianceAttestation,
    ComplianceException,
    Evaluation,
    GitLabProject,
    Policy,
    PolicyException,
)
from baluarte.evidence.export import _ts
from baluarte.evidence.package import MAX_GROUPS, PackageRequest, SampleRequest, check_period
from baluarte.integrations.gitlab import freshness as collection_freshness
from baluarte.logs import redact_value
from baluarte.policy.schema import RULE_TYPES

if TYPE_CHECKING:
    from baluarte.evidence.package import EvidencePackage

# Failing project, baseline and rule rows listed per not-met control.
MAX_FAILING = 25
# Rows of every other detail table (exceptions, decisions, attestations, exclusions, baselines).
MAX_ROWS = 200

STATUSES: tuple[ControlStatus, ...] = (
    "met",
    "not_met",
    "unknown",
    "manual",
    "not_applicable",
    "review_overdue",
    "no_results",
    "not_covered",
)
STATUS_LABELS: dict[str, str] = {
    "met": "Met",
    "not_met": "Not met",
    "unknown": "Unknown",
    "manual": "Manual",
    "not_applicable": "Not applicable",
    "review_overdue": "Review overdue",
    "no_results": "No results",
    "not_covered": "Not covered",
}
FRAMEWORK_NAMES = {framework.id: framework.name for framework in FRAMEWORKS}
# For the running header, where the full name does not fit.
FRAMEWORK_SHORT = {
    "soc2": "SOC 2",
    "iso27001": "ISO/IEC 27001",
    "nist_800_53_moderate": "NIST SP 800-53",
}

STATEMENT = (
    "The mapping of GitLab rule types to controls is guidance for the organization and its "
    "auditor, reviewed by the organization's compliance owner. A met control is supporting "
    "evidence for an organization-defined control objective; it is not a SOC 2 certification, "
    "an ISO/IEC 27001 certification or a NIST authorization. Results that are unknown are neither "
    "compliant nor failing. The report covers only what the GitLab token can see."
)
VERIFY = (
    "This report summarizes evidence; the audit-period evidence package is the complete, "
    "verifiable record. To check the report, request the evidence package for the same period "
    "and groups (POST /api/v1/evidence/packages, or the Evidence page), verify it with "
    "baluarte-verify-export, and compare: its evaluations.csv holds the results of the "
    "evaluation run named on the cover, its policies.csv the baseline versions below, its "
    "exceptions.csv the policy exceptions, and its manifest the control mapping (control_mapping) "
    "and the exclusions from monitoring (monitoring_scope) in force at the period's end. A package "
    "requested with report set holds this report as report.pdf, covered by its manifest and "
    "SHA256SUMS."
)


class ReportRequest(BaseModel):
    """The framework, period and optional group scope of an auditor report."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    framework: FrameworkId
    period_from: AwareDatetime = Field(alias="from", description="Period start (inclusive).")
    period_to: AwareDatetime = Field(
        alias="to", description="Period end (exclusive); results are as of this moment."
    )
    groups: list[str] = Field(
        default_factory=list,
        max_length=MAX_GROUPS,
        description="Group full paths: projects in these groups and their subgroups.",
    )

    @model_validator(mode="after")
    def _valid(self) -> "ReportRequest":
        check_period(self.period_from, self.period_to, self.groups)
        return self

    def described(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_defaults=True)

    def package_request(self) -> PackageRequest:
        """The package request whose snapshot the report is read from (no sample is drawn)."""
        return PackageRequest.model_validate(
            {
                "from": self.period_from,
                "to": self.period_to,
                "groups": self.groups,
                "sample": SampleRequest(size=1, seed=None),
            }
        )

    def filename(self) -> str:
        def stamp(value: datetime) -> str:
            return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

        return f"baluarte-{self.framework}-{stamp(self.period_from)}-{stamp(self.period_to)}.pdf"


# Report data -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Capped:
    """A detail table: its first rows, and how many there are in all."""

    rows: list[list[str]]
    total: int


@dataclass(frozen=True)
class ControlRow:
    control_id: str
    title: str
    status: str
    rule_types: list[str]
    passed: int
    failed: int
    unknown: int
    excepted: int


@dataclass(frozen=True)
class NotMet:
    control_id: str
    title: str
    failing: Capped


@dataclass
class ReportData:
    """Everything the PDF shows, already redacted. Rendering it is a pure function."""

    report_id: str
    generated_at: str
    framework: str
    framework_name: str
    period_from: str
    period_to: str
    instance_host: str
    run_id: int | None
    run_finished_at: str | None
    baluarte_version: str
    requested_by: str | None
    groups: list[str]
    by_status: dict[str, int]
    rule_types_evaluated: int
    rule_types_total: int
    projects_monitored: int
    projects_archived: int
    projects_evaluated: int
    projects_excluded: int
    freshness: list[list[str]]
    controls: list[ControlRow]
    not_met: list[NotMet]
    exceptions: Capped
    decisions: Capped
    attestations: Capped
    exclusions: Capped
    policies: Capped
    mapping_version: str
    mapping_changes: int
    population: str = ""


def _host(url: str) -> str:
    """The GitLab host (and port), never a path, query or credentials."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if parts.port else host


def _date(value: Any) -> str:
    """A table's time, to the minute in UTC (the package has full precision), or a day."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    return str(value.isoformat())


def _user(user_id: int | None) -> str:
    return "" if user_id is None else f"user:{user_id}"


def _clean(rows: list[list[Any]]) -> list[list[str]]:
    """Every cell as text, through the log redactor (as exports do)."""
    cleaned: list[list[str]] = redact_value(
        [["" if cell is None else str(cell) for cell in row] for row in rows]
    )
    return cleaned


def _operating_refs(framework: FrameworkId) -> dict[str, str]:
    """Operating-profile controls that support ``framework``, with their framework references."""
    return {
        control.id: ", ".join(control.mappings.get(framework, ()))
        for control in operating.CONTROLS
        if control.mappings.get(framework)
    }


class ReportBuilder:
    """Reads one report from a package's snapshot. The package must have passed its checks."""

    def __init__(self, snapshot: "EvidencePackage", framework: FrameworkId, report_id: str) -> None:
        self.snapshot = snapshot
        self.framework = framework
        self.report_id = report_id
        self.db = snapshot.db
        self.to = snapshot.to
        self.period_from = snapshot.request.period_from
        # The control mapping in force at the period's end.
        self.mapping = effective_mapping(self.db, self.to)

    def pdf(self) -> bytes:
        return render(self.data())

    def data(self) -> ReportData:
        snapshot = self.snapshot
        run = snapshot.run
        controls = control_catalogue.controls_of(self.framework)
        found = posture(
            self.db,
            snapshot.instance_id,
            self.framework,
            controls,
            as_of=self.to,
            run=run,
            where=snapshot._project_scope(),
        )
        by_status: dict[str, int] = {status: 0 for status in STATUSES}
        rows: list[ControlRow] = []
        for item in found.controls:
            by_status[item.status] += 1
            rows.append(
                ControlRow(
                    control_id=item.control.id,
                    title=item.control.title,
                    status=item.status,
                    rule_types=sorted({rule.rule_type for rule in item.mapped}),
                    passed=item.counts.passed,
                    failed=item.counts.fail,
                    unknown=item.counts.unknown,
                    excepted=item.counts.excepted,
                )
            )
        not_met = [
            NotMet(row.control_id, row.title, self._failing(row.rule_types))
            for row in rows
            if row.status == "not_met"
        ]
        framework_types = sorted({t for row in rows for t in row.rule_types})
        evaluated = self.db.execute(
            select(func.count(func.distinct(Evaluation.rule_type)))
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(*self._run_results())
        ).scalar_one()
        meta_population = collection_freshness.report(
            self.db, snapshot.instance_id, snapshot.settings
        ).population
        mapping = self.mapping
        changes = sum(
            1
            for (framework, _), rules in mapping.rules.items()
            if framework == self.framework
            for rule in rules
            if rule.source == "organization"
        ) + sum(1 for (framework, _) in mapping.status_changes if framework == self.framework)
        return ReportData(
            report_id=self.report_id,
            generated_at=_ts(snapshot.generated_at) or "",
            framework=self.framework,
            framework_name=FRAMEWORK_NAMES[self.framework],
            period_from=_ts(self.period_from) or "",
            period_to=_ts(self.to) or "",
            instance_host=_host(snapshot.settings.url),
            run_id=run.id if run else None,
            run_finished_at=_ts(run.finished_at) if run else None,
            baluarte_version=__version__,
            requested_by=snapshot.requested_by,
            groups=list(snapshot.request.groups),
            by_status=by_status,
            rule_types_evaluated=evaluated,
            rule_types_total=len(RULE_TYPES),
            projects_monitored=snapshot._count(GitLabProject, snapshot._monitored_scope()),
            # Archived projects stay in the inventory but are never evaluated.
            projects_archived=snapshot._count(
                GitLabProject,
                [
                    *snapshot._monitored_scope(),
                    GitLabProject.archived.is_(True),
                    GitLabProject.state == "active",
                ],
            ),
            projects_evaluated=self.db.execute(
                select(func.count(func.distinct(Evaluation.project_id)))
                .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
                .where(*self._run_results())
            ).scalar_one(),
            projects_excluded=snapshot._count(
                GitLabProject, [*snapshot._project_scope(), snapshot.coverage.project_excluded()]
            ),
            freshness=self._freshness(),
            controls=rows,
            not_met=not_met,
            exceptions=self._exceptions(framework_types),
            decisions=self._decisions(),
            attestations=self._attestations(),
            exclusions=self._exclusions(),
            policies=self._policies(),
            mapping_version=control_catalogue.MAPPING_VERSION,
            mapping_changes=changes,
            population=redact_value(meta_population.statement) if meta_population else "",
        )

    # Sections ------------------------------------------------------------------------------

    def _run_results(self) -> list[ColumnElement[bool]]:
        """The run's results for monitored projects in scope."""
        run_id = self.snapshot.run.id if self.snapshot.run else 0
        return [*self.snapshot._monitored_scope(), Evaluation.evaluation_run_id == run_id]

    def _failing(self, rule_types: Sequence[str]) -> Capped:
        conditions = [
            *self._run_results(),
            Evaluation.outcome == "fail",
            Evaluation.rule_type.in_(rule_types),
        ]
        total = self.db.execute(
            select(func.count())
            .select_from(Evaluation)
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(*conditions)
        ).scalar_one()
        rows = self.db.execute(
            select(
                GitLabProject.path_with_namespace,
                Evaluation.policy_name,
                Policy.version,
                Evaluation.rule_id,
                Evaluation.rule_type,
            )
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .join(Policy, Policy.id == Evaluation.policy_id)
            .where(*conditions)
            .order_by(
                GitLabProject.path_with_namespace,
                Evaluation.policy_name,
                Evaluation.rule_id,
                Evaluation.id,
            )
            .limit(MAX_FAILING)
        ).all()
        return Capped(
            _clean(
                [
                    [path, f"{policy} v{version}", rule, rule_type]
                    for path, policy, version, rule, rule_type in rows
                ]
            ),
            total,
        )

    def _exceptions(self, rule_types: Sequence[str]) -> Capped:
        """Policy and operating-profile exceptions in force at some point in the period."""
        start, end = self.period_from, self.to
        typed = aliased(Evaluation)
        policy_conditions = [
            *self.snapshot._monitored_scope(),
            PolicyException.state.in_(("approved", "revoked", "expired")),
            PolicyException.decided_at.is_not(None),
            PolicyException.decided_at < end,
            PolicyException.expires_at > start,
            or_(PolicyException.ended_at.is_(None), PolicyException.ended_at > start),
            # Only rules whose type supports a control of this framework.
            select(typed.id)
            .where(
                typed.policy_name == PolicyException.policy_name,
                typed.rule_id == PolicyException.rule_id,
                typed.rule_type.in_(rule_types),
            )
            .exists(),
        ]
        policy_rows = self.db.execute(
            select(PolicyException, GitLabProject.path_with_namespace)
            .join(GitLabProject, GitLabProject.id == PolicyException.project_id)
            .where(*policy_conditions)
            .order_by(PolicyException.id)
        ).all()
        refs = _operating_refs(self.framework)
        operating_rows = (
            self.db.scalars(
                select(ComplianceException)
                .where(
                    ComplianceException.control_id.in_(sorted(refs)),
                    ComplianceException.state.in_(("approved", "revoked")),
                    ComplianceException.decided_at.is_not(None),
                    ComplianceException.decided_at < end,
                    ComplianceException.expires_on > start.date(),
                    or_(
                        ComplianceException.revoked_at.is_(None),
                        ComplianceException.revoked_at > start,
                    ),
                )
                .order_by(ComplianceException.id)
            ).all()
            if refs
            else []
        )
        rows: list[list[Any]] = [
            [
                f"Policy exception {row.id}\n{path}",
                f"{row.policy_name} / {row.rule_id}",
                row.state,
                row.justification,
                f"Decided {_date(row.decided_at)} by {_user(row.decided_by_user_id)}\n"
                f"Expires {_date(row.expires_at)}",
            ]
            for row, path in policy_rows
        ] + [
            [
                f"Operating exception {row.id}\nBaluarte operation",
                f"{row.control_id} ({refs[row.control_id]})",
                row.state,
                row.justification,
                f"Decided {_date(row.decided_at)} by {_user(row.decided_by_user_id)}\n"
                f"Ends {_date(row.expires_on)} (first day not in force)",
            ]
            for row in operating_rows
        ]
        return Capped(_clean(rows[:MAX_ROWS]), len(rows))

    def _decisions(self) -> Capped:
        changes = sorted(
            (
                change
                for (fw, _), change in self.mapping.status_changes.items()
                if fw == self.framework
            ),
            key=lambda change: (change.control_id, change.id),
        )
        rows: list[list[Any]] = [
            [
                change.control_id,
                STATUS_LABELS.get(change.kind, change.kind),
                change.justification,
                change.evidence_ref or "",
                _date(change.review_by),
                f"{_date(change.decided_at)} by {_user(change.decided_by_user_id)}",
            ]
            for change in changes
        ]
        return Capped(_clean(rows[:MAX_ROWS]), len(rows))

    def _attestations(self) -> Capped:
        """The newest attestation of each operating control by ``to``, if still current then."""
        refs = _operating_refs(self.framework)
        if not refs:
            return Capped([], 0)
        latest: dict[str, ComplianceAttestation] = {}
        for row in self.db.scalars(
            select(ComplianceAttestation)
            .where(
                ComplianceAttestation.control_id.in_(sorted(refs)),
                ComplianceAttestation.attested_at <= self.to,
            )
            .order_by(ComplianceAttestation.attested_at, ComplianceAttestation.id)
        ):
            latest[row.control_id] = row
        current = sorted(
            (row for row in latest.values() if row.expires_at > self.to),
            key=lambda row: row.control_id,
        )
        rows: list[list[Any]] = [
            [
                f"{row.control_id} ({refs[row.control_id]})",
                row.statement,
                _date(row.attested_at),
                _date(row.expires_at),
                _user(row.attested_by_user_id),
            ]
            for row in current
        ]
        return Capped(_clean(rows[:MAX_ROWS]), len(rows))

    def _exclusions(self) -> Capped:
        found = self.snapshot.exclusions_at_end()
        rows: list[list[Any]] = [
            [
                row.target_type.replace("_", " "),
                row.target_path or ("every personal namespace" if row.target_id is None else ""),
                row.justification,
                f"{_date(row.decided_at)} by {_user(row.decided_by_user_id)}",
            ]
            for row in found
        ]
        return Capped(_clean(rows[:MAX_ROWS]), len(rows))

    def _policies(self) -> Capped:
        judged = (
            select(Evaluation.policy_id)
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(*self._run_results())
            .distinct()
        )
        found = self.db.scalars(
            select(Policy).where(Policy.id.in_(judged)).order_by(Policy.name, Policy.version)
        ).all()
        rows: list[list[Any]] = [
            [policy.name, f"v{policy.version}", policy.yaml_digest] for policy in found
        ]
        return Capped(_clean(rows[:MAX_ROWS]), len(rows))

    def _freshness(self) -> list[list[str]]:
        kinds = collection_freshness.kinds_at(
            self.db, self.snapshot.instance_id, self.snapshot.settings, self.to
        )
        return [
            [kind, value.status, _date(value.last_success_at) or "never"]
            for kind, value in kinds.items()
        ]


# Rendering ---------------------------------------------------------------------------------

FONT = "BaluarteSans"
FONT_BOLD = "BaluarteSans-Bold"
_fonts_lock = threading.Lock()
_fonts_ready = False
# C0 control characters other than tab and newline, which reportlab cannot set.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#52606d")
RULE = colors.HexColor("#9aa5b1")
SHADE = colors.HexColor("#e4e7eb")


def _register_fonts() -> None:
    global _fonts_ready
    with _fonts_lock:
        if _fonts_ready:
            return
        folder = resources.files("baluarte.evidence") / "fonts"
        for name, filename in ((FONT, "DejaVuSans.ttf"), (FONT_BOLD, "DejaVuSans-Bold.ttf")):
            with resources.as_file(folder / filename) as path:
                pdfmetrics.registerFont(TTFont(name, str(path)))
        pdfmetrics.registerFontFamily(
            FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD
        )
        _fonts_ready = True


def text(value: Any) -> str:
    """Untrusted text as Paragraph content: escaped, so markup in it is shown, never applied."""
    plain = _CONTROL.sub("", "" if value is None else str(value))
    return escape(plain).replace("\n", "<br/>")


@dataclass(frozen=True)
class _Styles:
    title: ParagraphStyle
    h1: ParagraphStyle
    h2: ParagraphStyle
    body: ParagraphStyle
    small: ParagraphStyle
    cell: ParagraphStyle
    head: ParagraphStyle


def _styles() -> _Styles:
    base = ParagraphStyle(
        "body", fontName=FONT, fontSize=9.5, leading=12.5, textColor=INK, alignment=TA_LEFT
    )
    return _Styles(
        title=ParagraphStyle("title", parent=base, fontName=FONT_BOLD, fontSize=20, leading=25),
        h1=ParagraphStyle(
            "h1",
            parent=base,
            fontName=FONT_BOLD,
            fontSize=14,
            leading=18,
            spaceBefore=10,
            spaceAfter=6,
        ),
        h2=ParagraphStyle(
            "h2",
            parent=base,
            fontName=FONT_BOLD,
            fontSize=11,
            leading=14,
            spaceBefore=8,
            spaceAfter=4,
        ),
        body=ParagraphStyle("p", parent=base, spaceAfter=6),
        small=ParagraphStyle("small", parent=base, fontSize=8.5, leading=11, textColor=MUTED),
        cell=ParagraphStyle("cell", parent=base, fontSize=8, leading=10),
        head=ParagraphStyle("head", parent=base, fontName=FONT_BOLD, fontSize=8, leading=10),
    )


PAGE = A4
# Margins that keep the text inside both A4 and US Letter when printed.
SIDE = 18 * mm
TOP = 24 * mm
BOTTOM = 24 * mm
WIDTH = PAGE[0] - 2 * SIDE


CELL_PADDING = 6.0
BREAK_AFTER = "._-/"


def _wrap_token(token: str, width: float, font: str, size: float) -> list[str]:
    """Split a token too wide for its column after a ``.``, ``_``, ``-`` or ``/`` that fits.

    reportlab breaks lines only at spaces, so an identifier such as
    ``gitlab.project.merge_request_approvals`` would otherwise be cut mid-word.
    """
    lines: list[str] = []
    rest = token
    while pdfmetrics.stringWidth(rest, font, size) > width:
        cut = 0
        for index, char in enumerate(rest):
            if (
                char in BREAK_AFTER
                and pdfmetrics.stringWidth(rest[: index + 1], font, size) <= width
            ):
                cut = index + 1
        if cut == 0 or cut == len(rest):
            break
        lines.append(rest[:cut])
        rest = rest[cut:]
    lines.append(rest)
    return lines


def _fit_cell(value: str, width: float, style: ParagraphStyle) -> str:
    """The cell's text with breaks added inside tokens wider than the column."""
    out = []
    for line in value.split("\n"):
        words = []
        for word in line.split(" "):
            words.append("\n".join(_wrap_token(word, width, style.fontName, style.fontSize)))
        out.append(" ".join(words))
    return "\n".join(out)


def _table(
    styles: _Styles,
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    widths: Sequence[float],
) -> Table:
    total = sum(widths)
    columns = [WIDTH * w / total for w in widths]
    data = [[Paragraph(text(h), styles.head) for h in header]] + [
        [
            Paragraph(text(_fit_cell(cell, column - CELL_PADDING, styles.cell)), styles.cell)
            for cell, column in zip(row, columns, strict=True)
        ]
        for row in rows
    ]
    table = Table(data, colWidths=columns, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), SHADE),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, RULE),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    return table


def _capped(
    styles: _Styles,
    header: Sequence[str],
    capped: Capped,
    widths: Sequence[float],
    more: str = "see the evidence package",
) -> list[Flowable]:
    if capped.total == 0:
        return [Paragraph("None.", styles.body)]
    out: list[Flowable] = [_table(styles, header, capped.rows, widths)]
    if capped.total > len(capped.rows):
        rest = capped.total - len(capped.rows)
        out.append(Spacer(1, 2))
        out.append(Paragraph(text(f"And {rest} more; {more}."), styles.small))
    return out


def _cover(data: ReportData, styles: _Styles) -> list[Flowable]:
    scope = ", ".join(data.groups) if data.groups else "Every monitored project"
    run = (
        f"Run {data.run_id}, finished {data.run_finished_at}" if data.run_id is not None else "None"
    )
    facts = [
        ("Framework", data.framework_name),
        ("Period", f"{data.period_from} to {data.period_to} (from inclusive, to exclusive)"),
        ("Results as of", data.period_to),
        ("Scope", scope),
        ("GitLab instance", data.instance_host),
        ("Evaluation run", run),
        ("Report ID", data.report_id),
        ("Generated at", f"{data.generated_at} (database clock, UTC)"),
        ("Baluarte version", data.baluarte_version),
        ("Requested by", data.requested_by or "unknown"),
    ]
    table = Table(
        [
            [Paragraph(text(label), styles.head), Paragraph(text(value), styles.cell)]
            for label, value in facts
        ],
        colWidths=[WIDTH * 0.28, WIDTH * 0.72],
    )
    table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    out: list[Flowable] = [
        Paragraph("Auditor report", styles.title),
        Spacer(1, 4),
        Paragraph(text(data.framework_name), styles.h2),
        Spacer(1, 8),
        table,
        Spacer(1, 12),
        Paragraph(
            "<b>Supporting evidence, not a certification.</b> " + text(STATEMENT), styles.body
        ),
    ]
    if data.population:
        out.append(Paragraph(text(data.population), styles.small))
    return out


def _summary(data: ReportData, styles: _Styles) -> list[Flowable]:
    status_rows = [[STATUS_LABELS[status], str(data.by_status[status])] for status in STATUSES]
    status_rows.append(["Total controls", str(len(data.controls))])
    return [
        Paragraph("Summary", styles.h1),
        Paragraph("Controls by status", styles.h2),
        _table(styles, ["Status", "Controls"], status_rows, [3, 1]),
        Spacer(1, 6),
        Paragraph(
            text(
                f"Rule types evaluated: {data.rule_types_evaluated} of the "
                f"{data.rule_types_total} rule types Baluarte can check. "
                f"Projects in monitoring scope: {data.projects_monitored}, of which "
                f"{data.projects_archived} archived (archived projects are not evaluated). "
                f"Projects evaluated in this run: {data.projects_evaluated}. "
                f"Projects excluded from monitoring: {data.projects_excluded}."
            ),
            styles.body,
        ),
        Paragraph("Collection freshness at the period's end", styles.h2),
        _table(
            styles, ["Collection", "Status", "Last success by then"], data.freshness, [2, 1.3, 3]
        ),
    ]


def _controls(data: ReportData, styles: _Styles) -> list[Flowable]:
    rows = [
        [
            row.control_id,
            row.title,
            STATUS_LABELS[row.status],
            "\n".join(row.rule_types) or "None",
            str(row.passed),
            str(row.failed),
            str(row.unknown),
            str(row.excepted),
        ]
        for row in data.controls
    ]
    return [
        Paragraph("Controls", styles.h1),
        Paragraph(
            "Each control's status from the evaluation run named on the cover, with the rule "
            "types mapped to it and the results of those rules: pass, fail, unknown (including "
            "unsupported) and excepted.",
            styles.small,
        ),
        _table(
            styles,
            ["Control", "Title", "Status", "Rule types", "Pass", "Fail", "Unknown", "Excepted"],
            rows,
            [1.05, 2.7, 1.25, 3.2, 0.75, 0.7, 1.35, 1.25],
        ),
    ]


def _not_met(data: ReportData, styles: _Styles) -> list[Flowable]:
    out: list[Flowable] = [Paragraph("Not met", styles.h1)]
    if not data.not_met:
        out.append(Paragraph("None.", styles.body))
        return out
    for item in data.not_met:
        out.append(Paragraph(f"{text(item.control_id)}: {text(item.title)}", styles.h2))
        out.extend(
            _capped(
                styles,
                ["Project", "Baseline", "Rule", "Rule type"],
                item.failing,
                [3.2, 2, 1.8, 2.4],
            )
        )
    return out


def _detail_sections(data: ReportData, styles: _Styles) -> list[Flowable]:
    return [
        Paragraph("Exceptions", styles.h1),
        Paragraph(
            "Policy exceptions on rules mapped to this framework, and exceptions to Baluarte's "
            "own operating profile, in force at some point in the period. An excepted result is "
            "a waived failure, not a pass.",
            styles.small,
        ),
        *_capped(
            styles,
            ["Exception and scope", "Rule or control", "State", "Justification", "Decision"],
            data.exceptions,
            [2.1, 1.9, 1.1, 3.6, 2.3],
        ),
        Paragraph("Organization mapping decisions", styles.h1),
        Paragraph(
            "Controls this organization scoped out (not applicable) or meets outside GitLab "
            "(manual), in force at the period's end.",
            styles.small,
        ),
        *_capped(
            styles,
            ["Control", "Decision", "Justification", "Evidence", "Review by", "Decided"],
            data.decisions,
            [1, 1.2, 3.6, 1.7, 1.1, 2],
        ),
        Paragraph("Attestations", styles.h1),
        Paragraph(
            "Statements about Baluarte's own operation that it cannot observe, current at the "
            "period's end.",
            styles.small,
        ),
        *_capped(
            styles,
            ["Control", "Statement", "Attested", "Expires", "By"],
            data.attestations,
            [1.8, 4, 1.6, 1.6, 1],
        ),
        Paragraph("Monitoring scope", styles.h1),
        Paragraph(
            "Exclusions from monitoring in force at the period's end. Excluded projects are not "
            "evaluated and are not counted above.",
            styles.small,
        ),
        *_capped(
            styles,
            ["Excluded", "Target", "Justification", "Since"],
            data.exclusions,
            [1.4, 2.6, 4.2, 2],
        ),
    ]


def _appendix(data: ReportData, styles: _Styles) -> list[Flowable]:
    return [
        Paragraph("Appendix: versions and verification", styles.h1),
        Paragraph("Baseline versions judged by the evaluation run", styles.h2),
        *_capped(
            styles, ["Baseline", "Version", "yaml_digest (SHA-256)"], data.policies, [2.2, 0.8, 5.5]
        ),
        Spacer(1, 6),
        Paragraph(
            text(
                f"Control mapping version {data.mapping_version}, with "
                f"{data.mapping_changes} organization change(s) in force for this framework at "
                "the period's end."
            ),
            styles.body,
        ),
        Paragraph("How to verify", styles.h2),
        Paragraph(text(VERIFY), styles.body),
    ]


def _minute(timestamp: str) -> str:
    """``2026-10-01T00:00:00.123Z`` as ``2026-10-01 00:00Z``, for the running header."""
    return f"{timestamp[:10]} {timestamp[11:16]}Z" if len(timestamp) >= 16 else timestamp


def _fit(value: str, width: float, size: float = 7.5) -> str:
    """``value`` cut to fit ``width`` in the header font."""
    value = _CONTROL.sub("", value)
    while value and pdfmetrics.stringWidth(value, FONT, size) > width:
        value = value[:-2] + "…"
    return value


def _decorate(data: ReportData, total: int | None) -> Callable[[Canvas, Any], None]:
    """Draws the header and footer of every page. ``total`` is None on the counting pass."""
    short = FRAMEWORK_SHORT.get(data.framework, data.framework)
    header = _fit(
        f"Baluarte auditor report: {short}, {_minute(data.period_from)} to "
        f"{_minute(data.period_to)}",
        WIDTH,
    )

    def draw(canvas: Canvas, _document: Any) -> None:
        width, height = PAGE
        canvas.saveState()
        canvas.setFillColor(MUTED)
        canvas.setStrokeColor(RULE)
        canvas.setFont(FONT, 7.5)
        canvas.drawString(SIDE, height - 14 * mm, header)
        canvas.line(SIDE, height - 15.5 * mm, width - SIDE, height - 15.5 * mm)
        canvas.line(SIDE, 15.5 * mm, width - SIDE, 15.5 * mm)
        canvas.drawString(SIDE, 11 * mm, f"Report {data.report_id}")
        page = canvas.getPageNumber()
        canvas.drawRightString(
            width - SIDE, 11 * mm, f"Page {page} of {total if total is not None else page}"
        )
        canvas.restoreState()

    return draw


# Room a heading needs below it (its text, an intro line and a few table rows); with less, it
# starts on the next page. keepWithNext is not used: it keeps a heading with the whole of the
# table after it, so a long table jumps to a new page and leaves the previous one half empty.
HEADING_ROOM = 110


def _keep_headings(story: list[Flowable], styles: _Styles) -> list[Flowable]:
    out: list[Flowable] = []
    for flowable in story:
        if isinstance(flowable, Paragraph) and flowable.style in (styles.h1, styles.h2):
            out.append(CondPageBreak(HEADING_ROOM))
        out.append(flowable)
    return out


def _build(data: ReportData, total: int | None) -> tuple[bytes, int]:
    styles = _styles()
    story: list[Flowable] = [
        *_cover(data, styles),
        PageBreak(),
        *_keep_headings(
            [
                *_summary(data, styles),
                *_controls(data, styles),
                *_not_met(data, styles),
                *_detail_sections(data, styles),
                *_appendix(data, styles),
            ],
            styles,
        ),
    ]
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=PAGE,
        leftMargin=SIDE,
        rightMargin=SIDE,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        # Fixed creation date and document ID, so the bytes depend only on the content.
        invariant=1,
        title=f"Baluarte auditor report: {data.framework_name}",
        author="Baluarte",
        subject=f"{data.framework} {data.period_from} to {data.period_to}",
        creator=f"Baluarte {data.baluarte_version}",
        lang="en",
    )
    draw = _decorate(data, total)
    document.build(story, onFirstPage=draw, onLaterPages=draw)
    return buffer.getvalue(), document.page


def render(data: ReportData) -> bytes:
    """The report as PDF bytes; the same data always gives the same bytes.

    Built twice: the first pass counts the pages, so every footer can say "Page N of M". The
    page count cannot change between passes, as "M" is set in the footer, outside the frame.
    """
    _register_fonts()
    _, pages = _build(data, None)
    pdf, _ = _build(data, pages)
    return pdf
