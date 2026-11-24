"""Parse and validate template YAML with the baseline loader's restricted YAML reader.

Errors name a path such as ``spec.settings.merge_requests.min_approvals`` and never repeat input
values, as for baselines (``baluarte.policy.loader``).
"""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from baluarte.policy.loader import PolicyError, PolicyInvalid, digest, load_yaml
from baluarte.templates.schema import ConfigurationTemplateDocument, Settings

TemplateInvalid = PolicyInvalid
TemplateError = PolicyError


@dataclass(frozen=True)
class ParsedTemplate:
    document: ConfigurationTemplateDocument
    digest: str

    @property
    def name(self) -> str:
        return self.document.metadata.name


def _path(prefix: str, location: tuple[int | str, ...]) -> str:
    path = prefix
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or "(document)"


def _message(error: dict[str, Any]) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown field"
    if kind == "missing":
        return "required field is missing"
    if kind == "string_pattern_mismatch":
        return "does not have the expected form"
    return str(error["msg"]).removeprefix("Value error, ")


def _errors(exc: ValidationError, prefix: str = "") -> list[PolicyError]:
    return [
        PolicyError(_path(prefix, tuple(error["loc"])), _message(dict(error)))
        for error in exc.errors(include_input=False, include_url=False)
    ]


def parse_template(source: str) -> ParsedTemplate:
    """Parse and validate ``source``; raise TemplateInvalid with every error found."""
    data = load_yaml(source)
    if not isinstance(data, dict):
        raise TemplateInvalid([PolicyError("(document)", "the document must be a mapping")])
    try:
        document = ConfigurationTemplateDocument.model_validate(data)
    except ValidationError as exc:
        raise TemplateInvalid(_errors(exc)) from None
    return ParsedTemplate(document=document, digest=digest(source))


def parse_settings(data: Any, prefix: str = "overrides") -> Settings:
    """Validate overrides, which use the template's ``settings`` format."""
    if not isinstance(data, dict):
        raise TemplateInvalid([PolicyError(prefix, "must be a mapping of settings")])
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise TemplateInvalid(_errors(exc, prefix)) from None
