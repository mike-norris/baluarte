"""Signed GitHub webhooks (roadmap M13.4b; docs/GITHUB_INTEGRATION.md#webhooks).

A GitHub connection with a webhook secret (``connection_webhook_secret:<id>``, sealed like its
token) accepts deliveries at ``POST /api/v1/webhooks/github/<connection id>``:

1. The body is read, at most ``MAX_BODY`` bytes, and its ``X-Hub-Signature-256`` checked: the
   HMAC-SHA256 of the raw body under the secret, compared in constant time. Nothing is parsed or
   stored before the signature matches.
2. The delivery is recorded once per ``X-GitHub-Delivery`` (``webhook_delivery``): a redelivery
   or a repeat is acknowledged and does nothing. The body itself is never stored, only its
   SHA-256, the event, its action and the repository it concerns.
3. An event Baluarte acts on queues a ``github.webhook`` job for the delivery, after ``DELAY`` so a
   burst of events shares one read:

   - change events (pull requests, reviews, workflow runs, deployments, pushes): the
     repository's changes are read (``baluarte.integrations.github.changes``);
   - a repository's settings (branch protection, rulesets, security features, labels): that
     setting is read again for the repository;
   - organization events, and any event about a repository not yet in the inventory: a full
     ``github.collect`` is queued, unless one is already waiting.

   Other events, and ``ping``, are recorded as ``ignored``.

A read covers every delivery queued for the same repository and kind of read that arrived before
it started: those are marked ``processed`` and their own jobs find nothing left to do. Webhook
reads are not collection runs, so they never make a collection look fresh; they record
observations and changes exactly as scheduled collection does, keyed by provider IDs, so the two
never duplicate a record. Each read queues an evaluation of the connection.
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.changes import settings as change_settings
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.connections import (
    CONNECTION,
    ConnectionRefused,
    github_connection,
    payload_for,
    webhook_secret_name,
)
from baluarte.db.models import CollectionJob, GitLabInstance, WebhookDelivery
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.github import changes as github_changes
from baluarte.integrations.github.branches import read_branch
from baluarte.integrations.github.client import GitHubClient
from baluarte.integrations.github.repositories import read_labels, read_security
from baluarte.integrations.gitlab import changes as gitlab_changes
from baluarte.integrations.gitlab.client import GitLabError
from baluarte.integrations.gitlab.jobs import GITHUB_COLLECT
from baluarte.integrations.gitlab.project_reads import ProjectTarget
from baluarte.jobs import queue
from baluarte.jobs.queue import ClaimedJob
from baluarte.policy.evaluation.runner import request_connection_evaluation

logger = logging.getLogger(__name__)

WEBHOOK = "github.webhook"
SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
MAX_BODY = 5 * 1024 * 1024
DELAY = timedelta(seconds=15)
DELIVERY_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")
EVENT_NAME = re.compile(r"^[a-z_]{1,64}$")

# Which read an event calls for, by event name.
Read = Literal["changes", "branch", "security", "labels", "organization"]
READS: dict[str, Read] = {
    "pull_request": "changes",
    "pull_request_review": "changes",
    "workflow_run": "changes",
    "deployment": "changes",
    "deployment_status": "changes",
    "push": "changes",
    "branch_protection_rule": "branch",
    "repository_ruleset": "branch",
    "security_and_analysis": "security",
    "label": "labels",
    "repository": "organization",
    "organization": "organization",
    "member": "organization",
    "membership": "organization",
    "team": "organization",
    "team_add": "organization",
}


def secret_name(connection_id: int) -> str:
    return webhook_secret_name(connection_id)


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verified(secret: str, body: bytes, supplied: str | None) -> bool:
    """Whether ``supplied`` is the body's signature under ``secret``, in constant time."""
    if not supplied:
        return False
    return hmac.compare_digest(signature(secret, body), supplied)


def read_of(event: str, payload: dict[str, Any]) -> Read | None:
    """The read an event calls for; None for one Baluarte does not act on."""
    found = READS.get(event)
    if found is None:
        return None
    if event == "workflow_run" and payload.get("action") != "completed":
        return None
    if event == "pull_request" and payload.get("action") != "closed":
        return None
    return found


def _repository_id(payload: dict[str, Any]) -> int | None:
    repository = payload.get("repository")
    found = repository.get("id") if isinstance(repository, dict) else None
    return found if type(found) is int else None


def receive(
    db: Session,
    connection: GitLabInstance,
    delivery_id: str,
    event: str,
    body: bytes,
    payload: dict[str, Any],
) -> tuple[Literal["queued", "ignored", "duplicate"], int | None]:
    """Record a verified delivery once and queue its work. The caller commits."""
    action = payload.get("action")
    read = None if connection.paused else read_of(event, payload)
    status = "queued" if read is not None else "ignored"
    inserted = db.execute(
        pg_insert(WebhookDelivery)
        .values(
            instance_id=connection.id,
            delivery_id=delivery_id,
            event=event,
            action=action if isinstance(action, str) else None,
            repository_id=_repository_id(payload),
            body_sha256=hashlib.sha256(body).hexdigest(),
            status=status,
            error="the connection is paused" if connection.paused else None,
        )
        .on_conflict_do_nothing(index_elements=["instance_id", "delivery_id"])
        .returning(WebhookDelivery.id)
    ).scalar_one_or_none()
    if inserted is None:
        return "duplicate", None
    if status == "ignored":
        return "ignored", inserted
    job_id = queue.enqueue(
        db,
        WEBHOOK,
        f"{WEBHOOK}:{connection.id}:{delivery_id}",
        {**(payload_for(connection.id) or {}), "delivery": inserted},
        max_attempts=3,
        delay=DELAY,
    )
    db.execute(update(WebhookDelivery).where(WebhookDelivery.id == inserted).values(job_id=job_id))
    return "queued", inserted


def _target(engine: Engine, connection_id: int, repository_id: int) -> ProjectTarget | None:
    """The repository, if it is in the inventory, active, unarchived and in monitoring scope."""
    found = [
        target
        for target in gitlab_changes.change_targets(engine, connection_id)
        if target.gitlab_id == repository_id
    ]
    return found[0] if found else None


def _same_read(read: Read) -> list[str]:
    return [event for event, wanted in READS.items() if wanted == read]


def _settle(
    engine: Engine,
    delivery: WebhookDelivery,
    read: Read,
    started: datetime,
    status: str,
    error: str | None = None,
) -> int:
    """Mark the delivery, and every delivery its read covered, ``status``; how many."""
    with Session(engine) as db:
        covered = [
            WebhookDelivery.instance_id == delivery.instance_id,
            WebhookDelivery.status == "queued",
            WebhookDelivery.received_at <= started,
            WebhookDelivery.event.in_(_same_read(read)),
        ]
        if read != "organization":
            covered.append(WebhookDelivery.repository_id == delivery.repository_id)
        result = db.execute(
            update(WebhookDelivery)
            .where(
                (WebhookDelivery.id == delivery.id)
                | (WebhookDelivery.id.in_(select(WebhookDelivery.id).where(*covered)))
            )
            .values(status=status, processed_at=func.now(), error=error)
        )
        db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _full_collection(engine: Engine, delivery: WebhookDelivery) -> int:
    """Queue a full collection of the connection unless one is already waiting to start."""
    with Session(engine) as db:
        waiting = db.scalars(
            select(CollectionJob.id).where(
                CollectionJob.kind == GITHUB_COLLECT,
                CollectionJob.state == "queued",
                CollectionJob.payload[CONNECTION].as_integer() == delivery.instance_id,
            )
        ).first()
        if waiting is None:
            waiting = queue.enqueue(
                db,
                GITHUB_COLLECT,
                f"{GITHUB_COLLECT}:connection-{delivery.instance_id}:webhook-{delivery.id}",
                payload_for(delivery.instance_id),
                max_attempts=3,
            )
        db.commit()
        return waiting


def make_handle(transport: httpx.BaseTransport | None = None) -> Any:
    def handle(job: ClaimedJob, engine: Engine) -> None:
        delivery_ref = job.payload.get("delivery")
        with Session(engine) as db:
            delivery = (
                db.get(WebhookDelivery, delivery_ref) if isinstance(delivery_ref, int) else None
            )
            if delivery is None or delivery.status != "queued":
                return  # an earlier read covered it
            started: datetime = db.execute(select(func.now())).scalar_one()
            db.expunge(delivery)
        read = READS.get(delivery.event)
        if read is None:
            _settle(engine, delivery, "organization", started, "ignored")
            return
        target = (
            _target(engine, delivery.instance_id, delivery.repository_id)
            if read != "organization" and delivery.repository_id is not None
            else None
        )
        if read == "organization" or target is None:
            # An organization event, or a repository the inventory does not hold yet.
            _full_collection(engine, delivery)
            _settle(engine, delivery, read, started, "processed")
            return
        try:
            with Session(engine) as db:
                _, auth, timeout = github_connection(db, delivery.instance_id, load_secret_box())
                options = change_settings.load(db)
                db.commit()
            provenance = Provenance(None, job.id)
            unread: str | None = None
            with GitHubClient(auth, timeout_seconds=timeout, transport=transport) as client:
                observed: Observed | None = None
                if read == "changes":
                    result = gitlab_changes.collect(
                        None,
                        engine,
                        delivery.instance_id,
                        options,
                        provenance,
                        reader=github_changes.reader(client),
                        provider="GitHub",
                        only={target.row_id},
                    )
                    if result.failed_projects or result.rejected:
                        unread = "the repository's changes could not be read"

                elif read == "branch" and target.default_branch is not None:
                    observed = read_branch(
                        client, target.row_id, target.path, target.default_branch
                    )
                elif read == "security" and target.default_branch is not None:
                    observed = read_security(
                        client, target.row_id, target.path, target.default_branch
                    )
                elif read == "labels":
                    observed = read_labels(client, target.row_id, target.path)
            if observed is not None:
                with Session(engine) as db:
                    record(db, delivery.instance_id, "project_id", [observed], provenance)
                    db.commit()
                # A plan or permission refusal is a real answer; anything else was not read.
                if observed.collection_status not in ("ok", "unsupported"):
                    unread = observed.error or observed.collection_status
        except (GitLabError, ConnectionRefused, SecretUnavailable) as error:
            # The next scheduled collection reads it; the delivery says why this one did not.
            _settle(engine, delivery, read, started, "failed", str(error)[:500])
            logger.warning("github webhook %s: %s", delivery.delivery_id, error)
            return
        if unread is not None:
            # Recorded as scheduled collection would; the next collection reads it again.
            _settle(engine, delivery, read, started, "failed", unread[:500])
        else:
            _settle(engine, delivery, read, started, "processed")
        with Session(engine) as db:
            request_connection_evaluation(db, "webhook", delivery.instance_id)
            db.commit()

    return handle


def valid_headers(event: str | None, delivery_id: str | None) -> bool:
    return bool(
        event and EVENT_NAME.match(event) and delivery_id and DELIVERY_ID.match(delivery_id)
    )
