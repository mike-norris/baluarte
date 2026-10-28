from datetime import date
from typing import Any

import httpx
import pytest
from gitlab_fake import URL, FakeGitLab, settings

from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import DiagnosticsReport, run_diagnostics

TODAY = date(2026, 10, 28)


def diagnose(fake: FakeGitLab) -> DiagnosticsReport:
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return run_diagnostics(client, URL, today=lambda: TODAY)


def statuses(report: DiagnosticsReport) -> dict[str, str]:
    return {check.id: check.status for check in report.checks}


def token(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "baluarte",
        "scopes": ["read_api"],
        "active": True,
        "revoked": False,
        "expires_at": "2027-06-30",
    } | overrides


def test_healthy_read_only_token() -> None:
    report = diagnose(FakeGitLab())

    assert report.status == "ok"
    assert report.version == "17.5.1-ee"
    assert report.gitlab_url == URL
    assert statuses(report) == {
        "connectivity": "ok",
        "token": "ok",
        "user": "ok",
        "group_visibility": "ok",
    }


def test_only_reads() -> None:
    fake = FakeGitLab()

    diagnose(fake)

    assert {request.method for request in fake.requests} == {"GET"}
    assert fake.paths() == ["/version", "/personal_access_tokens/self", "/user", "/groups"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (httpx.Response(401), "unauthorized"),
        (httpx.Response(500), "server_error"),
        (httpx.ConnectError("refused"), "unreachable"),
    ],
)
def test_connectivity_failure_skips_the_rest_as_unknown(
    reply: httpx.Response | Exception, reason: str
) -> None:
    fake = FakeGitLab()
    fake.set("/version", reply)

    report = diagnose(fake)

    assert report.status == "failed"
    assert report.checks[0].details == {"reason": reason}
    assert statuses(report) == {
        "connectivity": "failed",
        "token": "unknown",
        "user": "unknown",
        "group_visibility": "unknown",
    }
    assert set(fake.paths()) == {"/version"}


def test_missing_version_is_unknown_not_ok() -> None:
    fake = FakeGitLab()
    fake.json("/version", body={})

    report = diagnose(fake)

    assert statuses(report)["connectivity"] == "unknown"
    assert report.status == "warning"


@pytest.mark.parametrize(
    ("body", "status", "summary"),
    [
        (token(scopes=["api"]), "warning", "write scopes (api)"),
        (token(scopes=["read_api", "write_repository"]), "warning", "write_repository"),
        (token(scopes=["read_user"]), "failed", "lacks the read_api scope"),
        (token(revoked=True), "failed", "revoked or inactive"),
        (token(active=False), "failed", "revoked or inactive"),
        (token(expires_at="2026-10-27"), "failed", "expired on 2026-10-27"),
        (token(expires_at="2026-11-20"), "warning", "expires on 2026-11-20"),
        (token(expires_at=None), "ok", "read_api only"),
        ([], "unknown", "Unexpected"),
    ],
)
def test_token_checks(body: Any, status: str, summary: str) -> None:
    fake = FakeGitLab()
    fake.json("/personal_access_tokens/self", body=body)

    check = diagnose(fake).checks[1]

    assert (check.id, check.status) == ("token", status)
    assert summary in check.summary


def test_token_details_unavailable_is_unknown() -> None:
    fake = FakeGitLab()
    fake.json("/personal_access_tokens/self", 404, {"message": "404 Not Found"})

    report = diagnose(fake)

    assert statuses(report)["token"] == "unknown"
    assert report.status == "warning"


def test_token_details_forbidden_is_failed() -> None:
    fake = FakeGitLab()
    fake.json("/personal_access_tokens/self", 403, {})

    assert statuses(diagnose(fake))["token"] == "failed"


@pytest.mark.parametrize(
    ("user", "status"),
    [
        ({"username": "root", "state": "active", "is_admin": True}, "warning"),
        ({"username": "gone", "state": "blocked"}, "failed"),
        ({"username": "plain", "state": "active"}, "ok"),
    ],
)
def test_user_checks(user: dict[str, Any], status: str) -> None:
    fake = FakeGitLab()
    fake.json("/user", body=user)

    assert statuses(diagnose(fake))["user"] == status


@pytest.mark.parametrize(
    ("status_code", "body", "status"),
    [(200, [], "warning"), (403, {}, "failed"), (200, {"x": 1}, "unknown")],
)
def test_group_visibility(status_code: int, body: Any, status: str) -> None:
    fake = FakeGitLab()
    fake.json("/groups", status_code, body)

    assert statuses(diagnose(fake))["group_visibility"] == status
