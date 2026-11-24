"""The GitHub collection job (roadmap M13.2 to M13.5).

``github.collect`` collects one GitHub connection in order: capability detection, the
organization, its repositories, teams and memberships, each repository's default-branch settings
and security features, then the labels, the changes and, when the connection's workspace has an
issue register, its issues (``github.collect_issues`` runs that last part alone). Each part
is a collection run of the kinds GitLab uses (``capabilities``, ``groups``, ``projects``,
``labels``, ``changes``, ``issues``), so freshness and the Connections page
report GitHub connections the same way. An evaluation is queued after each collection; each rule
type declares the providers it evaluates (``baluarte.policy.evaluation.project``).
"""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.changes import settings as change_settings
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.connections import ConnectionRefused, connection_of, github_connection, payload_for
from baluarte.db.models import GitLabInstance
from baluarte.evidence.observations import Provenance
from baluarte.integrations.github import changes as github_changes
from baluarte.integrations.github import issues as github_issues
from baluarte.integrations.github.branches import read_branches, unconfirmed
from baluarte.integrations.github.capabilities import detect
from baluarte.integrations.github.client import Auth, GitHubClient, organization_of
from baluarte.integrations.github.discovery import discover_organization
from baluarte.integrations.github.repositories import read_all_labels, read_repositories, unread
from baluarte.integrations.gitlab import changes as gitlab_changes
from baluarte.integrations.gitlab import issue_evidence, register_issues
from baluarte.integrations.gitlab.capabilities import observe_snapshot, record_snapshot
from baluarte.integrations.gitlab.client import GitLabError
from baluarte.integrations.gitlab.jobs import GITHUB_COLLECT as COLLECT
from baluarte.integrations.gitlab.jobs import GITHUB_COLLECT_ISSUES as COLLECT_ISSUES
from baluarte.integrations.gitlab.jobs import SNAPSHOT_OUTCOMES
from baluarte.integrations.gitlab.runs import RunOutcome, finish_run, start_run
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.policy.evaluation.runner import queue_after_collection
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

__all__ = ["COLLECT", "COLLECT_ISSUES", "make_collect", "make_collect_issues"]

Handler = Callable[[ClaimedJob, Engine], None]


def collect_changes(
    engine: Engine,
    url: str,
    job: ClaimedJob,
    connection: GitLabInstance,
    auth: Auth,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> None:
    """The ``changes`` run: pull requests, workflow runs, direct pushes and deployments
    (``baluarte.integrations.github.changes``). A failure is recorded on the run; the rest of the
    collection still counts."""
    run_id = start_run(engine, url, "changes", job)
    try:
        with Session(engine) as db:
            options = change_settings.load(db)
            db.commit()
        with GitHubClient(auth, timeout_seconds=timeout, transport=transport) as client:
            result = gitlab_changes.collect(
                None,
                engine,
                connection.id,
                options,
                Provenance(run_id, job.id),
                reader=github_changes.reader(client),
                provider="GitHub",
            )
    except Exception as error:
        finish_run(engine, run_id, "failed", error=f"unexpected error: {type(error).__name__}")
        raise
    problems = []
    if result.failed_projects:
        problems.append(f"changes of {result.failed_projects} repositories could not be listed")
    failed_parts = sum(count for key, count in result.parts.items() if key.endswith(":failed"))
    if failed_parts:
        problems.append(f"{failed_parts} parts of changes could not be read")
    outcome: RunOutcome
    if result.rejected or (result.projects and result.failed_projects == result.projects):
        outcome = "failed"
    else:
        outcome = "partial" if problems else "succeeded"
    finish_run(
        engine,
        run_id,
        outcome,
        seen=result.projects,
        counts=result.as_counts(),
        error="; ".join(problems) or None,
    )


def collect_issues(
    engine: Engine,
    url: str,
    job: ClaimedJob,
    connection: GitLabInstance,
    auth: Auth,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> RunOutcome | None:
    """The ``issues`` run: the register's issue index and the evidence of auditable issues
    (``baluarte.integrations.github.issues``). None, and no run, without a register."""
    with Session(engine) as db:
        active = register_issues.has_register(db, connection.id)
        db.commit()
    if not active:
        return None
    run_id = start_run(engine, url, "issues", job)
    provenance = Provenance(run_id, job.id)
    try:
        with GitHubClient(auth, timeout_seconds=timeout, transport=transport) as client:
            result = register_issues.collect(
                None,
                engine,
                connection.id,
                provenance,
                reader=github_issues.index_reader(client),
            )
            evidence = (
                issue_evidence.collect(
                    None, engine, connection.id, provenance, capturer=github_issues.capturer(client)
                )
                if not result.rejected
                else issue_evidence.EvidenceTally()
            )
    except Exception as error:
        finish_run(engine, run_id, "failed", error=f"unexpected error: {type(error).__name__}")
        raise
    unread_count = sum(count for status, count in result.reads.items() if status != "ok")
    problems = [f"issues of {unread_count} repositories could not be read"] if unread_count else []
    failed = evidence.captured.get("failed", 0)
    if failed:
        issues = "issue" if failed == 1 else "issues"
        problems.append(f"evidence of {failed} auditable {issues} could not be read")
    outcome: RunOutcome
    if result.rejected or evidence.rejected or (result.projects and not result.reads.get("ok")):
        outcome = "failed"
    else:
        outcome = "partial" if problems else "succeeded"
    finish_run(
        engine,
        run_id,
        outcome,
        seen=result.projects,
        counts={**result.as_counts(), "evidence": evidence.as_counts()},
        error="; ".join(problems) or None,
    )
    return outcome


def _connection(job: ClaimedJob) -> int:
    connection_id = connection_of(job.payload)
    if connection_id is None:
        raise PermanentJobError("A GitHub collection must name its connection")
    return connection_id


def make_collect_issues(transport: httpx.BaseTransport | None = None) -> Handler:
    """The register's read of one GitHub connection, asked for from the register."""

    def collect(job: ClaimedJob, engine: Engine) -> None:
        try:
            with Session(engine) as db:
                connection, auth, timeout = github_connection(
                    db, _connection(job), load_secret_box()
                )
                db.expunge(connection)
        except (ConnectionRefused, ConfigurationError, SecretUnavailable) as error:
            raise PermanentJobError(str(error)) from error
        outcome = collect_issues(
            engine, connection.base_url, job, connection, auth, timeout, transport
        )
        if outcome == "failed":
            raise JobError("issue collection read no repository")

    return collect


def make_collect(transport: httpx.BaseTransport | None = None) -> Handler:
    """Build the handler; tests pass a transport that stands in for GitHub."""

    def collect(job: ClaimedJob, engine: Engine) -> None:
        connection_id = _connection(job)
        try:
            with Session(engine) as db:
                connection, auth, timeout = github_connection(db, connection_id, load_secret_box())
                db.expunge(connection)
        except (ConnectionRefused, ConfigurationError, SecretUnavailable) as error:
            raise PermanentJobError(str(error)) from error
        url = connection.base_url
        organization = organization_of(url)
        with GitHubClient(auth, timeout_seconds=timeout, transport=transport) as client:
            run_id = start_run(engine, url, "capabilities", job)
            try:
                snapshot = detect(client, organization)
                with Session(engine) as db:
                    instance = record_snapshot(db, url, snapshot)
                    db.flush()
                    observe_snapshot(db, instance.id, snapshot, Provenance(run_id, job.id))
                    db.commit()
            except Exception as error:
                finish_run(
                    engine, run_id, "failed", error=f"unexpected error: {type(error).__name__}"
                )
                raise
            failing = sorted(n for n, p in snapshot.probes.items() if p.status != "supported")
            finish_run(
                engine,
                run_id,
                SNAPSHOT_OUTCOMES[snapshot.status],
                seen=len(snapshot.probes),
                counts={"not_supported": failing},
                error=snapshot.error,
            )
            if snapshot.status == "failed":
                raise JobError(f"GitHub capability detection failed: {snapshot.error}")

            groups_run = start_run(engine, url, "groups", job)
            projects_run = start_run(engine, url, "projects", job)
            try:
                provenance = Provenance(projects_run, job.id)
                result = discover_organization(client, engine, connection, provenance)
                branches = read_branches(client, engine, connection, provenance)
                security = read_repositories(client, engine, connection, provenance)["security"]
            except GitLabError as error:
                message = f"GitHub discovery failed: {error}"
                finish_run(engine, groups_run, "failed", error=message)
                finish_run(engine, projects_run, "failed", error=message)
                raise JobError(message) from error
            except Exception as error:
                message = f"unexpected error: {type(error).__name__}"
                finish_run(engine, groups_run, "failed", error=message)
                finish_run(engine, projects_run, "failed", error=message)
                raise
        finish_run(engine, groups_run, "succeeded", seen=1, counts={"organization": organization})
        repos = result.repositories
        problems = []
        if repos.reconciled["unchanged"]:
            problems.append(f"{repos.reconciled['unchanged']} repositories could not be confirmed")
        not_read = unconfirmed(branches.items())
        if not_read:
            problems.append(f"the default-branch settings of {not_read} repositories were not read")
        if unread(security.items()):
            problems.append(
                f"the security features of {unread(security.items())} repositories were not read"
            )
        finish_run(
            engine,
            projects_run,
            "partial" if problems else "succeeded",
            seen=repos.seen,
            counts={
                **repos.reconciled,
                "listed": dict(repos.listed),
                "teams": result.teams,
                "members": result.members,
                "branches": branches,
                "security": security,
            },
            error="; ".join(problems) or None,
        )
        labels_run = start_run(engine, url, "labels", job)
        try:
            with GitHubClient(auth, timeout_seconds=timeout, transport=transport) as client:
                labels = read_all_labels(client, engine, connection, Provenance(labels_run, job.id))
        except GitLabError as error:
            finish_run(engine, labels_run, "failed", error=f"GitHub labels failed: {error}")
            labels = None
        except Exception as error:
            finish_run(
                engine, labels_run, "failed", error=f"unexpected error: {type(error).__name__}"
            )
            raise
        if labels is not None:
            missed = unread(labels.items())
            finish_run(
                engine,
                labels_run,
                "partial" if missed else "succeeded",
                seen=sum(labels.values()),
                counts=labels,
                error=f"the labels of {missed} repositories were not read" if missed else None,
            )
        collect_changes(engine, url, job, connection, auth, timeout, transport)
        collect_issues(engine, url, job, connection, auth, timeout, transport)
        # The repositories, their settings, labels and changes are new evidence: evaluate, once
        # per run.
        with Session(engine) as db:
            queue_after_collection(db, projects_run, payload_for(connection.id))
            db.commit()
        logger.info(
            "github %s: %s repositories, %s teams, %s memberships",
            organization,
            repos.seen,
            result.teams["listed"],
            result.members["listed"],
            extra={"job_id": job.id},
        )

    return collect
