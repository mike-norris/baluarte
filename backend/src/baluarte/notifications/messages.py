"""The text of each notification: plain text, and a simple HTML part for email.

Messages carry no personal data: no names or email addresses of people, only counts, project
paths, rule IDs and links. Links are built from Baluarte's address (``public_url``); without one
a message says where to look instead of linking.
"""

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from baluarte.notifications.channels import Message
from baluarte.settings import ConfigurationError

PREFIX = "[Baluarte]"
SETTINGS_PATH = "/settings"

# Review queue kinds, as people read them.
KIND_TEXT: dict[str, tuple[str, str]] = {
    "baseline_change": ("baseline change", "baseline changes"),
    "taxonomy": ("label taxonomy", "label taxonomies"),
    "label_plan": ("label plan", "label plans"),
    "issue_plan": ("issue plan", "issue plans"),
    "remediation_plan": ("remediation plan", "remediation plans"),
    "exception": ("rule exception", "rule exceptions"),
    "scope_exclusion": ("monitoring exclusion", "monitoring exclusions"),
    "mapping_change": ("control mapping change", "control mapping changes"),
    "control_exception": ("compliance exception", "compliance exceptions"),
}
# Where each kind that expires is listed.
EXPIRING_PATH: dict[str, str] = {
    "exception": "/exceptions",
    "control_exception": "/compliance",
    "label_plan": "/labels/plans",
    "issue_plan": "/time/issue-plans",
    "remediation_plan": "/remediation",
}


def public_url() -> str | None:
    """Baluarte's address from the configuration in force, without a trailing slash."""
    from baluarte.configuration.effective import effective_auth_settings

    try:
        settings = effective_auth_settings()
    except ConfigurationError:
        return None
    if settings is None or not settings.public_url:
        return None
    return settings.public_url.rstrip("/")


def link(base: str | None, path: str) -> str | None:
    return f"{base}{path}" if base else None


@dataclass(frozen=True)
class Line:
    text: str
    url: str | None = None


@dataclass(frozen=True)
class Section:
    heading: str | None
    lines: Sequence[Line]


def render(subject: str, intro: str, sections: Sequence[Section], base: str | None) -> Message:
    footer = "Sent by Baluarte. Change what is sent under Settings → Notifications" + (
        f": {base}{SETTINGS_PATH}" if base else "."
    )
    if base is None:
        footer += " Links are left out because Baluarte's address is not configured."
    text = [intro, ""]
    parts = [f"<p>{html.escape(intro)}</p>"]
    for section in sections:
        if section.heading:
            text.append(section.heading)
            parts.append(f"<p><strong>{html.escape(section.heading)}</strong></p>")
        items = []
        for line in section.lines:
            text.append(f"- {line.text}" + (f": {line.url}" if line.url else ""))
            escaped = html.escape(line.text)
            items.append(
                f'<li><a href="{html.escape(line.url, quote=True)}">{escaped}</a></li>'
                if line.url
                else f"<li>{escaped}</li>"
            )
        parts.append("<ul>" + "".join(items) + "</ul>")
        text.append("")
    text.append(footer)
    parts.append(f'<p style="color:#555">{html.escape(footer)}</p>')
    body_html = (
        '<!doctype html><html><body style="font-family:sans-serif">'
        + "".join(parts)
        + "</body></html>"
    )
    return Message(
        subject=f"{PREFIX} {subject}", text="\n".join(text).strip() + "\n", html=body_html
    )


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _kind(count: int, kind: str) -> str:
    one, many = KIND_TEXT.get(kind, (kind, kind))
    return _plural(count, one, many)


# Events ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingLine:
    id: int
    severity: str
    project_path: str
    policy_name: str
    rule_id: str


def new_findings(
    base: str | None,
    run_id: int,
    min_severity: str,
    total: int,
    listed: Sequence[FindingLine],
) -> Message:
    above = "" if min_severity == "low" else f" at {min_severity} severity or above"
    lines = [
        Line(
            f"{item.severity}: {item.project_path}: {item.policy_name} {item.rule_id}",
            link(base, f"/findings/{item.id}"),
        )
        for item in listed
    ]
    if total > len(listed):
        lines.append(Line(f"and {total - len(listed)} more", link(base, "/findings")))
    return render(
        f"{_plural(total, 'new finding')}{above}",
        f"Evaluation run {run_id} opened {_plural(total, 'finding')}{above}.",
        [Section(None, lines)],
        base,
    )


def _when(moment: datetime | None) -> str:
    return moment.isoformat(timespec="minutes") if moment is not None else "unknown"


def collection_unhealthy(
    base: str | None,
    reason: str,
    kinds: Sequence[str],
    consecutive_failures: int,
    failing_since: datetime | None,
    last_success_at: datetime | None,
) -> Message:
    what = ", ".join(kinds) or "collection"
    if reason == "failing":
        intro = (
            f"GitLab collection of {what} has failed {_plural(consecutive_failures, 'time')} in "
            f"a row since {_when(failing_since)} (UTC). Check that Baluarte can reach GitLab "
            "and that the token is valid."
        )
    else:
        intro = (
            f"GitLab collection of {what} is overdue: it last succeeded at "
            f"{_when(last_success_at)} (UTC). Check that the worker is running."
        )
    return render(
        "GitLab collection is unhealthy",
        intro + " Findings and evidence are not current until it recovers.",
        [Section(None, [Line("Collection status", link(base, "/"))])],
        base,
    )


def collection_recovered(base: str | None, unhealthy_since: datetime | None) -> Message:
    return render(
        "GitLab collection recovered",
        "GitLab collection is healthy again"
        + (f"; it was unhealthy since {_when(unhealthy_since)} (UTC)." if unhealthy_since else "."),
        [Section(None, [Line("Collection status", link(base, "/"))])],
        base,
    )


def _expiring_lines(base: str | None, expiring: Mapping[str, int]) -> list[Line]:
    return [
        Line(
            _kind(count, kind),
            link(base, EXPIRING_PATH.get(kind, "/reviews")),
        )
        for kind, count in expiring.items()
        if count
    ]


def review_waiting(
    base: str | None,
    day: date,
    older_than_days: int,
    waiting: Mapping[str, int],
    partial: Sequence[str],
    expiring: Mapping[str, int],
) -> Message:
    total = sum(waiting.values())
    sections = []
    if total:
        sections.append(
            Section(
                f"Waiting for review for more than {_plural(older_than_days, 'day')}",
                [
                    Line(
                        ("at least " if kind in partial else "") + _kind(count, kind),
                        link(base, "/reviews"),
                    )
                    for kind, count in waiting.items()
                    if count
                ],
            )
        )
    soon = _expiring_lines(base, expiring)
    if soon:
        sections.append(Section("Expiring within 7 days", soon))
    parts = []
    if total:
        parts.append(f"{_plural(total, 'item')} waiting for review")
    if soon:
        parts.append(f"{_plural(sum(expiring.values()), 'item')} expiring soon")
    return render(
        " and ".join(parts) or "Nothing waiting for review",
        f"The review reminder for {day.isoformat()}.",
        sections,
        base,
    )


@dataclass(frozen=True)
class Digest:
    week: str
    evaluated_at: datetime | None
    results: Mapping[str, int]
    findings: Mapping[str, int]
    # None when no compliance framework is selected.
    controls: Mapping[str, int] | None
    controls_error: bool
    queue: Mapping[str, int]
    queue_partial: Sequence[str]
    expiring: Mapping[str, int]


def weekly_digest(base: str | None, digest: Digest) -> Message:
    sections = []
    if digest.evaluated_at is None:
        rules = [Line("No evaluation has finished yet", link(base, "/"))]
    else:
        rules = [
            Line(
                ", ".join(f"{count} {outcome}" for outcome, count in digest.results.items())
                or "no results",
                link(base, "/"),
            ),
            Line(f"latest evaluation finished at {_when(digest.evaluated_at)} (UTC)"),
        ]
    sections.append(Section("Rule results", rules))
    open_total = sum(digest.findings.values())
    sections.append(
        Section(
            f"Open findings: {open_total}",
            [
                Line(f"{digest.findings.get(severity, 0)} {severity}", link(base, "/findings"))
                for severity in ("critical", "high", "medium", "low")
            ],
        )
    )
    if digest.controls_error:
        controls = [
            Line("The compliance posture could not be evaluated", link(base, "/compliance"))
        ]
    elif digest.controls is None:
        controls = [Line("No compliance framework is selected", link(base, "/compliance"))]
    else:
        controls = [
            Line(f"{digest.controls.get('not_met', 0)} not met", link(base, "/compliance")),
            Line(
                f"{digest.controls.get('needs_attestation', 0)} awaiting attestation",
                link(base, "/compliance"),
            ),
        ]
    sections.append(Section("Controls", controls))
    queue_total = sum(digest.queue.values())
    sections.append(
        Section(
            f"Review queue: {queue_total}",
            [
                Line(
                    ("at least " if kind in digest.queue_partial else "") + _kind(count, kind),
                    link(base, "/reviews"),
                )
                for kind, count in digest.queue.items()
                if count
            ]
            or [Line("nothing waiting", link(base, "/reviews"))],
        )
    )
    soon = _expiring_lines(base, digest.expiring)
    sections.append(Section("Expiring next week", soon or [Line("nothing")]))
    return render(
        f"Weekly digest {digest.week}",
        f"Baluarte's weekly digest for {digest.week}.",
        sections,
        base,
    )


def connection_test_message(base: str | None) -> Message:
    return render(
        "Test message",
        "This is a test message from Baluarte. Notifications to this channel work.",
        [Section(None, [Line("Notification settings", link(base, SETTINGS_PATH))])],
        base,
    )
