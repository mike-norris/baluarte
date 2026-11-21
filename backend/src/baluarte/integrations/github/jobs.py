"""The GitHub collection job (roadmap M13.2, M13.3).

``github.collect`` collects one GitHub connection in order: capability detection, the
organization, then its repositories, teams and memberships, then each repository's default-branch
settings. Each part is a collection run of the same kinds as GitLab's (``capabilities``,
``groups``, ``projects``), so freshness and the Connections page report GitHub connections the
same way. An evaluation is queued after each collection; each rule type declares the providers
it evaluates (``baluarte.policy.evaluation.project``).
"""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.connections import ConnectionRefused, connection_of, github_connection, payload_for
from baluarte.evidence.observations import Provenance
from baluarte.integrations.github.branches import read_branches, unconfirmed
from baluarte.integrations.github.capabilities import detect
from baluarte.integrations.github.client import GitHubClient, organization_of
from baluarte.integrations.github.discovery import discover_organization
from baluarte.integrations.gitlab.capabilities import observe_snapshot, record_snapshot
from baluarte.integrations.gitlab.client import GitLabError
from baluarte.integrations.gitlab.jobs import GITHUB_COLLECT as COLLECT
from baluarte.integrations.gitlab.jobs import SNAPSHOT_OUTCOMES
from baluarte.integrations.gitlab.runs import finish_run, start_run
from baluarte.jobs.queue import ClaimedJob, JobError, PermanentJobError
from baluarte.policy.evaluation.runner import queue_after_collection
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

__all__ = ["COLLECT", "make_collect"]

Handler = Callable[[ClaimedJob, Engine], None]


def make_collect(transport: httpx.BaseTransport | None = None) -> Handler:
    """Build the handler; tests pass a transport that stands in for GitHub."""

    def collect(job: ClaimedJob, engine: Engine) -> None:
        connection_id = connection_of(job.payload)
        if connection_id is None:
            raise PermanentJobError("A GitHub collection must name its connection")
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
        unread = unconfirmed(branches.items())
        if unread:
            problems.append(f"the default-branch settings of {unread} repositories were not read")
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
            },
            error="; ".join(problems) or None,
        )
        # The repositories and their settings are new evidence: evaluate, once per run.
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
