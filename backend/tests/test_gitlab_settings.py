from pathlib import Path

import pytest

from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.logs import REDACTED, redact
from baluarte.settings import ConfigurationError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("URL", "TOKEN", "TOKEN_FILE", "TIMEOUT_SECONDS"):
        monkeypatch.delenv(f"BALUARTE_GITLAB_{name}", raising=False)
    token = tmp_path / "gitlab-token"
    token.write_text("settings-test-token-1\n")
    return token


def test_not_configured_without_url() -> None:
    assert load_gitlab_settings() is None


def test_loads_url_token_and_timeout(monkeypatch: pytest.MonkeyPatch, clean_env: Path) -> None:
    monkeypatch.setenv("BALUARTE_GITLAB_URL", "https://example.test/gitlab/")
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(clean_env))
    monkeypatch.setenv("BALUARTE_GITLAB_TIMEOUT_SECONDS", "30")

    settings = load_gitlab_settings()

    assert settings is not None
    assert settings.url == "https://example.test/gitlab"
    assert settings.api_url == "https://example.test/gitlab/api/v4"
    assert settings.token == "settings-test-token-1"
    assert settings.timeout_seconds == 30.0
    assert "settings-test-token-1" not in repr(settings)
    assert redact("sent settings-test-token-1") == f"sent {REDACTED}"


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"BALUARTE_GITLAB_URL": "http://gitlab.example.test"}, "must be an https URL"),
        ({"BALUARTE_GITLAB_URL": "gitlab.example.test"}, "must be an https URL"),
        ({"BALUARTE_GITLAB_URL": "https://root:pw@gitlab.example.test"}, "must not contain"),
        ({"BALUARTE_GITLAB_URL": "https://gitlab.example.test/?x=1"}, "must not contain"),
        ({"BALUARTE_GITLAB_URL": "https://gitlab.example.test"}, "BALUARTE_GITLAB_TOKEN_FILE"),
        (
            {"BALUARTE_GITLAB_URL": "https://gitlab.example.test", "BALUARTE_GITLAB_TOKEN": "x"},
            "must not be set directly",
        ),
        (
            {"BALUARTE_GITLAB_URL": "https://g.test", "BALUARTE_GITLAB_TIMEOUT_SECONDS": "soon"},
            "must be a number",
        ),
        (
            {"BALUARTE_GITLAB_URL": "https://g.test", "BALUARTE_GITLAB_TIMEOUT_SECONDS": "600"},
            "between 1 and 120",
        ),
    ],
)
def test_rejects_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, variables: dict[str, str], message: str
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    if "TIMEOUT" in str(variables):
        monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(clean_env))

    with pytest.raises(ConfigurationError, match=message):
        load_gitlab_settings()
