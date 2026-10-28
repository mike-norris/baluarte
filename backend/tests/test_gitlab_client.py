import ssl

import httpx
import pytest
from gitlab_fake import TOKEN, FakeGitLab, paged, settings

from baluarte.integrations.gitlab.client import (
    MAX_RETRY_AFTER_SECONDS,
    Forbidden,
    GitLabClient,
    GitLabError,
    InvalidResponse,
    NotFound,
    RateLimited,
    ServerError,
    TlsError,
    Unauthorized,
    Unreachable,
)


def client(fake: FakeGitLab, sleeps: list[float] | None = None) -> GitLabClient:
    record = sleeps if sleeps is not None else []
    return GitLabClient(settings(), transport=fake.transport, sleep=record.append)


def test_sends_token_header_and_only_gets() -> None:
    fake = FakeGitLab()

    body = client(fake).get_json("/groups", {"per_page": 1})

    [request] = fake.requests
    assert body == [{"id": 1, "full_path": "platform", "visibility": "private"}]
    assert request.method == "GET"
    assert str(request.url) == "https://gitlab.example.test/api/v4/groups?per_page=1"
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert request.headers["User-Agent"].startswith("baluarte/")


def test_relative_url_install() -> None:
    fake = FakeGitLab({"/gitlab/api/v4/version": [httpx.Response(200, json={"version": "17.0"})]})

    with GitLabClient(
        settings("https://gitlab.example.test/gitlab"), transport=fake.transport
    ) as c:
        assert c.get_json("/version") == {"version": "17.0"}


def test_redirects_are_not_followed() -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(302, headers={"Location": "https://evil.example/collect"}))

    with pytest.raises(InvalidResponse, match="redirected"):
        client(fake).get("/version")
    assert [r.url.host for r in fake.requests] == ["gitlab.example.test"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, Unauthorized),
        (403, Forbidden),
        (404, NotFound),
        (500, ServerError),
        (418, InvalidResponse),
    ],
)
def test_status_mapping(status: int, error: type[GitLabError]) -> None:
    fake = FakeGitLab()
    fake.json("/user", status, {"message": f"secret detail {TOKEN}"})

    with pytest.raises(error) as raised:
        client(fake).get("/user")
    assert raised.value.status == status
    assert TOKEN not in str(raised.value)
    assert "secret detail" not in str(raised.value)
    assert len(fake.requests) == 1


def test_retries_transient_statuses_honouring_retry_after() -> None:
    fake = FakeGitLab()
    fake.set(
        "/version",
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"version": "17.5.1"}),
    )
    sleeps: list[float] = []

    assert client(fake, sleeps).get_json("/version") == {"version": "17.5.1"}
    assert sleeps == [7.0, 1.0]


def test_retry_after_is_capped() -> None:
    fake = FakeGitLab()
    fake.set(
        "/version",
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(200, json={}),
    )
    sleeps: list[float] = []

    client(fake, sleeps).get("/version")

    assert sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_gives_up_after_bounded_attempts() -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(429))
    sleeps: list[float] = []

    with pytest.raises(RateLimited):
        client(fake, sleeps).get("/version")
    assert len(fake.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_network_errors_retry_then_fail_as_unreachable() -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.ConnectTimeout("timed out"))

    with pytest.raises(Unreachable, match="ConnectTimeout"):
        client(fake).get("/version")
    assert len(fake.requests) == 3


def test_network_error_then_success() -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.ConnectError("refused"), httpx.Response(200, json={"version": "1"}))

    assert client(fake).get_json("/version") == {"version": "1"}


def test_tls_failures_are_not_retried() -> None:
    error = httpx.ConnectError("certificate verify failed")
    error.__cause__ = ssl.SSLCertVerificationError("self-signed certificate")
    fake = FakeGitLab()
    fake.set("/version", error)

    with pytest.raises(TlsError, match="SSL_CERT_FILE"):
        client(fake).get("/version")
    assert len(fake.requests) == 1


def test_non_json_body() -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(200, text="<html>sign in</html>"))

    with pytest.raises(InvalidResponse, match="did not return JSON"):
        client(fake).get_json("/version")


def test_paginate_follows_next_links() -> None:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", [[{"id": 1}, {"id": 2}], [{"id": 3}], [{"id": 4}]]))

    pages = list(client(fake).paginate("/groups", {"all_available": "true"}))

    assert pages == [[{"id": 1}, {"id": 2}], [{"id": 3}], [{"id": 4}]]
    first, second, _third = fake.requests
    assert first.url.params["per_page"] == "100"
    assert first.url.params["all_available"] == "true"
    assert str(second.url) == "https://gitlab.example.test/api/v4/groups?page=2&per_page=100"


def test_paginate_single_page_without_link() -> None:
    fake = FakeGitLab()
    fake.json("/groups", body=[])

    assert list(client(fake).paginate("/groups")) == [[]]


def test_paginate_refuses_a_next_link_to_another_host() -> None:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", [[{"id": 1}], [{"id": 2}]], base="https://evil.example"))

    with pytest.raises(InvalidResponse, match="outside the GitLab API"):
        list(client(fake).paginate("/groups"))
    assert {request.url.host for request in fake.requests} == {"gitlab.example.test"}


def test_paginate_refuses_a_repeating_link() -> None:
    fake = FakeGitLab()
    loop = "https://gitlab.example.test/api/v4/groups?page=2"
    fake.set("/groups", httpx.Response(200, json=[], headers={"Link": f'<{loop}>; rel="next"'}))

    with pytest.raises(InvalidResponse, match="repeats"):
        list(client(fake).paginate("/groups"))


@pytest.mark.parametrize("body", [{"id": 1}, [1, 2]])
def test_paginate_requires_a_list_of_objects(body: object) -> None:
    fake = FakeGitLab()
    fake.json("/groups", body=body)

    with pytest.raises(InvalidResponse, match="list of objects"):
        list(client(fake).paginate("/groups"))


def test_paginate_error_on_a_later_page_names_the_relative_path() -> None:
    pages = paged("/groups", [[{"id": 1}], [{"id": 2}]])

    def second_fails(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(403)
        assert callable(pages)
        return pages(request)

    fake = FakeGitLab()
    fake.set("/groups", second_fails)
    iterator = client(fake).paginate("/groups")

    assert next(iterator) == [{"id": 1}]
    with pytest.raises(Forbidden) as raised:
        next(iterator)
    assert str(raised.value) == "the token may not read /groups (403)"
