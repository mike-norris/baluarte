"""The built-in control catalogue and the default mapping of baseline rule types to controls.

The controls are the SOC 2, ISO/IEC 27001:2022 and NIST SP 800-53 Rev. 5 controls that source
code governance in GitLab can support, with the operating profile's framework IDs
(``baluarte.compliance.catalogue``). Titles are short paraphrases written for Baluarte, never the
text of the standards. Each default pair carries a one-sentence rationale.

The mapping is guidance for an organization and its auditor, not a certification: an
organization adds, removes and scopes out controls with a justification
(``baluarte.controls.changes``), and the effective mapping is the default plus those changes.
``MAPPING_VERSION`` changes whenever the catalogue or a default pair changes, and every change
records the version it was made against.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from baluarte.compliance.catalogue import ISO27001, NIST, SOC2, FrameworkId
from baluarte.policy.schema import RULE_TYPES

MAPPING_VERSION = "2026.11.1"


ACCESS = "Access control"
AUTHENTICATION = "Authentication"
CHANGE = "Change management"
CONFIGURATION = "Configuration management"
DEVELOPMENT = "Secure development"
VULNERABILITIES = "Vulnerability management"


@dataclass(frozen=True)
class Control:
    framework: FrameworkId
    id: str
    title: str
    area: str


@dataclass(frozen=True)
class DefaultPair:
    rule_type: str
    framework: FrameworkId
    control_id: str
    rationale: str


CONTROLS: tuple[Control, ...] = (
    Control(SOC2, "CC6.1", "Logical access to information assets is restricted", ACCESS),
    Control(SOC2, "CC6.2", "People are registered and authorized before they get access", ACCESS),
    Control(SOC2, "CC6.3", "Access is granted, changed and removed by role", ACCESS),
    Control(SOC2, "CC6.6", "Access from outside the system boundary is restricted", ACCESS),
    Control(
        SOC2, "CC6.8", "Unauthorized or malicious software is prevented or detected", DEVELOPMENT
    ),
    Control(
        SOC2, "CC7.1", "Configuration changes and vulnerabilities are detected", VULNERABILITIES
    ),
    Control(SOC2, "CC8.1", "Changes are authorized, tested and approved before release", CHANGE),
    Control(ISO27001, "A.5.15", "Access rules are defined and enforced", ACCESS),
    Control(ISO27001, "A.5.17", "Authentication secrets are issued and managed", AUTHENTICATION),
    Control(ISO27001, "A.5.18", "Access rights are granted, reviewed and removed", ACCESS),
    Control(ISO27001, "A.8.2", "Privileged access is limited and managed", ACCESS),
    Control(ISO27001, "A.8.3", "Access to information follows the access rules", ACCESS),
    Control(ISO27001, "A.8.4", "Read and write access to source code is controlled", ACCESS),
    Control(
        ISO27001,
        "A.8.5",
        "Authentication is strong enough for the access it grants",
        AUTHENTICATION,
    ),
    Control(
        ISO27001, "A.8.8", "Technical vulnerabilities are found and addressed", VULNERABILITIES
    ),
    Control(ISO27001, "A.8.9", "Configurations are defined, applied and monitored", CONFIGURATION),
    Control(ISO27001, "A.8.25", "Security is part of the development life cycle", DEVELOPMENT),
    Control(ISO27001, "A.8.28", "Code is checked against secure coding practice", DEVELOPMENT),
    Control(
        ISO27001, "A.8.29", "Security is tested during development and acceptance", DEVELOPMENT
    ),
    Control(ISO27001, "A.8.32", "Changes follow a controlled process", CHANGE),
    Control(NIST, "AC-2", "Accounts are managed from creation to removal", ACCESS),
    Control(NIST, "AC-3", "Approved authorizations are enforced", ACCESS),
    Control(NIST, "AC-5", "Duties are separated between people", ACCESS),
    Control(NIST, "AC-6", "Only the access a task needs is granted", ACCESS),
    Control(NIST, "AC-6(5)", "Privileged accounts are limited to designated people", ACCESS),
    Control(
        NIST, "IA-2(1)", "Privileged accounts sign in with more than one factor", AUTHENTICATION
    ),
    Control(NIST, "IA-5", "Authenticators are managed, including their lifetime", AUTHENTICATION),
    Control(NIST, "CM-2", "A baseline configuration is kept current", CONFIGURATION),
    Control(NIST, "CM-3", "Configuration changes are reviewed and approved", CHANGE),
    Control(NIST, "CM-5", "Who can change the system is restricted", CHANGE),
    Control(NIST, "CM-6", "Security settings are defined and enforced", CONFIGURATION),
    Control(NIST, "RA-5", "Systems are scanned for vulnerabilities", VULNERABILITIES),
    Control(NIST, "SA-11", "Developers test and evaluate security", DEVELOPMENT),
    Control(NIST, "SI-2", "Flaws are found and corrected", VULNERABILITIES),
)


def _pairs(rule_type: str, rationales: Mapping[tuple[FrameworkId, str], str]) -> list[DefaultPair]:
    return [
        DefaultPair(rule_type, framework, control_id, rationale)
        for (framework, control_id), rationale in rationales.items()
    ]


DEFAULT_MAPPING: tuple[DefaultPair, ...] = tuple(
    [
        *_pairs(
            "gitlab.project.visibility",
            {
                (SOC2, "CC6.1"): "Visibility decides who can read a project's code at all.",
                (SOC2, "CC6.6"): "Internal or public visibility opens the code beyond its members.",
                (ISO27001, "A.5.15"): "Allowed visibility levels are an access rule for code.",
                (ISO27001, "A.8.3"): "Visibility restricts access to the information in a project.",
                (
                    ISO27001,
                    "A.8.4",
                ): "Visibility is the first control on read access to source code.",
                (NIST, "AC-3"): "GitLab enforces the visibility level on every read.",
            },
        ),
        *_pairs(
            "gitlab.branch.default_protected",
            {
                (SOC2, "CC8.1"): "A protected default branch takes changes only through its rules.",
                (ISO27001, "A.8.4"): "Branch protection controls write access to released code.",
                (
                    ISO27001,
                    "A.8.32",
                ): "Protection routes changes to the default branch through a process.",
                (NIST, "CM-3"): "Protection keeps unreviewed changes off the default branch.",
                (NIST, "CM-5"): "Protection limits who can push or merge to the default branch.",
            },
        ),
        *_pairs(
            "gitlab.security_policy.scan_execution",
            {
                (SOC2, "CC7.1"): "Required scans detect vulnerabilities as code changes.",
                (SOC2, "CC8.1"): "Scans run as part of testing every change.",
                (
                    ISO27001,
                    "A.8.8",
                ): "Dependency and container scans find technical vulnerabilities.",
                (
                    ISO27001,
                    "A.8.28",
                ): "SAST and secret detection check code against secure practice.",
                (ISO27001, "A.8.29"): "Scans are security testing built into development.",
                (NIST, "RA-5"): "Scheduled and pipeline scans are vulnerability scanning.",
                (NIST, "SA-11"): "Scans in the pipeline are developer security testing.",
                (NIST, "SI-2"): "Scan results identify flaws to correct.",
            },
        ),
        *_pairs(
            "gitlab.security_policy.approval",
            {
                (SOC2, "CC8.1"): "An approval policy requires changes to be approved before merge.",
                (
                    ISO27001,
                    "A.8.25",
                ): "Required approvals put review into the development life cycle.",
                (ISO27001, "A.8.32"): "Approval before merge is a step of the change process.",
                (NIST, "CM-3"): "Changes are approved by someone before they are merged.",
                (NIST, "AC-5"): "Forbidding author approval separates writing from approving.",
            },
        ),
        *_pairs(
            "gitlab.project.merge_request_approvals",
            {
                (SOC2, "CC8.1"): "Project approval settings require changes to be approved.",
                (ISO27001, "A.8.32"): "Approval settings make review part of every change.",
                (NIST, "CM-3"): "Changes need the configured approvals before merge.",
                (NIST, "AC-5"): "Forbidding author and committer approval separates duties.",
            },
        ),
        *_pairs(
            "gitlab.project.privileged_members",
            {
                (
                    SOC2,
                    "CC6.1",
                ): "Fewer privileged members means fewer people who can change access.",
                (SOC2, "CC6.2"): "Inactive accounts that keep access show deprovisioning gaps.",
                (SOC2, "CC6.3"): "A cap on Maintainers and Owners keeps access to what roles need.",
                (ISO27001, "A.5.15"): "The cap is an access rule for privileged roles.",
                (
                    ISO27001,
                    "A.5.18",
                ): "Inactive accounts with access show rights that were not removed.",
                (
                    ISO27001,
                    "A.8.2",
                ): "Maintainer and Owner are the privileged access rights in GitLab.",
                (NIST, "AC-2"): "Inactive accounts holding access show account management gaps.",
                (NIST, "AC-6"): "The cap limits access to what is needed.",
                (
                    NIST,
                    "AC-6(5)",
                ): "Maintainers and Owners are the privileged accounts of a project.",
            },
        ),
        *_pairs(
            "gitlab.group.two_factor_required",
            {
                (SOC2, "CC6.1"): "Two-factor sign-in protects access to the code.",
                (ISO27001, "A.5.17"): "Requiring a second factor governs how people authenticate.",
                (ISO27001, "A.8.5"): "Two-factor authentication is secure authentication.",
                (NIST, "IA-2(1)"): "Enforced two-factor sign-in covers privileged accounts.",
            },
        ),
        *_pairs(
            "gitlab.project.access_tokens",
            {
                (SOC2, "CC6.1"): "Tokens are credentials with access to the project.",
                (SOC2, "CC6.2"): "Expiring and unused tokens show whether credentials are retired.",
                (
                    ISO27001,
                    "A.5.17",
                ): "Token lifetimes are part of managing authentication secrets.",
                (ISO27001, "A.5.18"): "Unused tokens are access rights that should be removed.",
                (NIST, "IA-5"): "A maximum token lifetime is authenticator management.",
                (NIST, "AC-2"): "Project tokens act as accounts and need the same lifecycle.",
            },
        ),
        *_pairs(
            "gitlab.change.reviewed",
            {
                (SOC2, "CC8.1"): "Each change shows who approved it before it was merged.",
                (
                    ISO27001,
                    "A.8.32",
                ): "Reviewed merges and no direct pushes show the change process was followed.",
                (NIST, "CM-3"): "Every change to the default branch was reviewed and approved.",
                (NIST, "AC-5"): "Approval by someone other than the author separates duties.",
            },
        ),
        *_pairs(
            "gitlab.change.pipeline_passed",
            {
                (SOC2, "CC8.1"): "Each change shows a passing pipeline before release.",
                (
                    ISO27001,
                    "A.8.29",
                ): "Passing pipelines and test reports show changes were tested.",
                (NIST, "SA-11"): "The pipeline is the developer's testing of each change.",
            },
        ),
        *_pairs(
            "gitlab.change.deployed_after_review",
            {
                (SOC2, "CC8.1"): "What reached production can be traced to an approved change.",
                (
                    ISO27001,
                    "A.8.32",
                ): "Deployments of reviewed changes only show the process held to production.",
                (NIST, "CM-3"): "Production changes trace to reviewed and approved changes.",
            },
        ),
    ]
)

_CONTROLS: dict[tuple[str, str], Control] = {(c.framework, c.id): c for c in CONTROLS}


def control(framework: str, control_id: str) -> Control | None:
    return _CONTROLS.get((framework, control_id))


def controls_of(framework: str | None = None) -> list[Control]:
    return [c for c in CONTROLS if framework is None or c.framework == framework]


def defaults_for(framework: str, control_id: str) -> list[DefaultPair]:
    return [
        pair
        for pair in DEFAULT_MAPPING
        if pair.framework == framework and pair.control_id == control_id
    ]


def is_default(rule_type: str, framework: str, control_id: str) -> bool:
    return any(pair.rule_type == rule_type for pair in defaults_for(framework, control_id))


def known_rule_type(rule_type: str) -> bool:
    return rule_type in RULE_TYPES
