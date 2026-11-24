"""Job handlers by kind.

A handler receives the claimed job and the worker's database engine. It must be idempotent: it
can run more than once for the same job. Raise JobError to retry with a stored message, or
PermanentJobError to stop retrying. Any other exception is retried and recorded by type name
only, since its text may hold secrets. Open short sessions from the engine; do not hold a
transaction open across GitLab calls.
"""

from collections.abc import Callable, Mapping

from sqlalchemy import Engine

from baluarte.compliance.retention import RETENTION_EXPIRE, handle_expire
from baluarte.compliance.shipping import AUDIT_SHIP, handle_ship
from baluarte.integrations.github.jobs import COLLECT as GITHUB_COLLECT
from baluarte.integrations.github.jobs import COLLECT_ISSUES as GITHUB_COLLECT_ISSUES
from baluarte.integrations.github.jobs import make_collect as make_github_collect
from baluarte.integrations.github.jobs import (
    make_collect_issues as make_github_collect_issues,
)
from baluarte.integrations.github.webhooks import WEBHOOK as GITHUB_WEBHOOK
from baluarte.integrations.github.webhooks import make_handle as make_github_webhook
from baluarte.integrations.gitlab.jobs import (
    COLLECT_CHANGES,
    COLLECT_ISSUES,
    COLLECT_LABELS,
    COLLECT_TIME,
    DETECT_CAPABILITIES,
    DISCOVER_GROUPS,
    DISCOVER_PROJECTS,
    DISCOVER_SECURITY_POLICIES,
    make_collect_changes,
    make_collect_issues,
    make_collect_labels,
    make_collect_time,
    make_detect_capabilities,
    make_discover_groups,
    make_discover_projects,
    make_discover_security_policies,
)
from baluarte.jobs.queue import ClaimedJob
from baluarte.labels.plans import APPLY_LABEL_PLAN, make_apply_label_plan
from baluarte.notifications.delivery import NOTIFY_DELIVER, handle_deliver
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.remediation.execute import make_apply_remediation_plan
from baluarte.remediation.plans import APPLY_REMEDIATION_PLAN
from baluarte.templates.execute import (
    APPLY_CHANGE_SET,
    ROLLBACK_CHANGE_SET,
    make_apply_change_set,
    make_rollback_change_set,
)
from baluarte.timetracking.issue_plans import APPLY_ISSUE_PLAN, make_apply_issue_plan

Handler = Callable[[ClaimedJob, Engine], None]

HANDLERS: Mapping[str, Handler] = {
    DETECT_CAPABILITIES: make_detect_capabilities(),
    DISCOVER_GROUPS: make_discover_groups(),
    DISCOVER_PROJECTS: make_discover_projects(),
    DISCOVER_SECURITY_POLICIES: make_discover_security_policies(),
    COLLECT_CHANGES: make_collect_changes(),
    COLLECT_LABELS: make_collect_labels(),
    COLLECT_TIME: make_collect_time(),
    COLLECT_ISSUES: make_collect_issues(),
    GITHUB_COLLECT: make_github_collect(),
    GITHUB_COLLECT_ISSUES: make_github_collect_issues(),
    GITHUB_WEBHOOK: make_github_webhook(),
    APPLY_LABEL_PLAN: make_apply_label_plan(),
    APPLY_ISSUE_PLAN: make_apply_issue_plan(),
    APPLY_REMEDIATION_PLAN: make_apply_remediation_plan(),
    APPLY_CHANGE_SET: make_apply_change_set(),
    ROLLBACK_CHANGE_SET: make_rollback_change_set(),
    EVALUATE: evaluate,
    RETENTION_EXPIRE: handle_expire,
    AUDIT_SHIP: handle_ship,
    NOTIFY_DELIVER: handle_deliver,
}
