"""Incoming webhooks (roadmap M13.4b; ``baluarte.integrations.github.webhooks``).

No session or CSRF token: a delivery is authenticated by its signature alone, checked before the
body is parsed. An address with no webhook set up and a signature that does not match get the
same kind of answer whatever the reason, and neither is recorded.
"""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import database_engine
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.configuration.store import read_secret_value
from baluarte.db.models import GitLabInstance
from baluarte.integrations.github import webhooks

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookReceived(BaseModel):
    status: Literal["queued", "ignored", "duplicate"]


_NOT_SET_UP = HTTPException(status.HTTP_404_NOT_FOUND, "No webhook is set up at this address")


async def _body(request: Request) -> bytes:
    declared = request.headers.get("Content-Length")
    if declared is not None and (not declared.isdigit() or int(declared) > webhooks.MAX_BODY):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The delivery is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > webhooks.MAX_BODY:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The delivery is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _accept(
    engine: Engine,
    connection_id: int,
    body: bytes,
    supplied: str | None,
    event: str,
    delivery_id: str,
) -> WebhookReceived:
    with Session(engine, expire_on_commit=False) as db:
        connection = db.get(GitLabInstance, connection_id)
        if connection is None or connection.provider != "github" or connection.removed_at:
            raise _NOT_SET_UP
        try:
            secret = read_secret_value(db, webhooks.secret_name(connection_id), load_secret_box())
        except SecretUnavailable as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "The webhook secret cannot be read"
            ) from error
        if secret is None:
            raise _NOT_SET_UP
        if not webhooks.verified(secret, body, supplied):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "The signature does not match")
        try:
            payload: Any = json.loads(body)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is not JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is not a JSON object")
        outcome, _ = webhooks.receive(db, connection, delivery_id, event, body, payload)
        db.commit()
        return WebhookReceived(status=outcome)


@router.post(
    "/github/{connection_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookReceived,
    summary="Receive a GitHub webhook delivery",
    description="Called by GitHub, not by people. Authenticated by `X-Hub-Signature-256` (the "
    "HMAC-SHA256 of the body under the connection's webhook secret); needs `X-GitHub-Event` and "
    "`X-GitHub-Delivery`. Each delivery ID is processed once: a repeat answers `duplicate`. At "
    "most 5 MiB.",
    responses={
        400: {"description": "Missing headers, or a body that is not a JSON object"},
        401: {"description": "The signature does not match"},
        404: {"description": "No webhook is set up at this address"},
        413: {"description": "The delivery is larger than 5 MiB"},
    },
)
async def receive_github(
    connection_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    request: Request,
    engine: Annotated[Engine, Depends(database_engine)],
) -> WebhookReceived:
    event = request.headers.get(webhooks.EVENT_HEADER)
    delivery_id = request.headers.get(webhooks.DELIVERY_HEADER)
    if not webhooks.valid_headers(event, delivery_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "X-GitHub-Event and X-GitHub-Delivery are required"
        )
    body = await _body(request)
    return await run_in_threadpool(
        _accept,
        engine,
        connection_id,
        body,
        request.headers.get(webhooks.SIGNATURE_HEADER),
        str(event),
        str(delivery_id),
    )
