"""Parse and validate baseline YAML without executing anything.

YAML is read with a restricted ``SafeLoader``:

- only plain mappings, sequences, strings, numbers, booleans and null; any explicit tag
  (``!!python/...``, ``!!binary``, ``!custom``) is rejected, so nothing is constructed
  beyond those types
- anchors and aliases are rejected, which also rules out "billion laughs" expansion
- duplicate keys are rejected instead of silently keeping the last one
- exactly one document, at most ``MAX_SOURCE_BYTES`` of UTF-8 and ``MAX_DEPTH`` levels of nesting

Errors carry a path such as ``spec.rules[1].allowed`` and a message. Paths name keys as
written, but messages never repeat input values, so a validation response cannot echo a secret
pasted into a policy by mistake.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.composer import Composer
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from baluarte.policy.schema import RULE_TYPES, GitLabBaseline

MAX_SOURCE_BYTES = 64 * 1024
MAX_DEPTH = 20

_STANDARD_TAGS = {
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
}


@dataclass(frozen=True)
class PolicyError:
    path: str
    message: str


class PolicyInvalid(Exception):
    def __init__(self, errors: list[PolicyError]) -> None:
        super().__init__("; ".join(f"{e.path}: {e.message}" for e in errors))
        self.errors = errors


@dataclass(frozen=True)
class ParsedPolicy:
    document: GitLabBaseline
    digest: str

    @property
    def name(self) -> str:
        return self.document.metadata.name


class _Rejected(Exception):
    def __init__(self, message: str, line: int) -> None:
        super().__init__(message)
        self.line = line


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without aliases, anchors, tags, merge keys, duplicate keys or deep nesting."""

    def compose_node(self, parent: Node | None, index: Any) -> Node | None:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        line = event.start_mark.line + 1
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _Rejected("anchors and aliases are not allowed", line)
        if getattr(event, "tag", None) is not None:
            raise _Rejected("YAML tags are not allowed", line)
        return Composer.compose_node(self, parent, index)

    def construct_document(self, node: Node) -> Any:
        _check(node, 0)
        return super().construct_document(node)


# Dates stay strings and "<<" is an ordinary key, so only the types listed above resolve.
_UNRESOLVED = {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:merge", "tag:yaml.org,2002:value"}
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _UNRESOLVED]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _check(node: Node, depth: int) -> None:
    line = node.start_mark.line + 1
    if depth > MAX_DEPTH:
        raise _Rejected(f"nesting is deeper than {MAX_DEPTH} levels", line)
    if node.tag not in _STANDARD_TAGS:
        raise _Rejected("YAML tags are not allowed", line)
    if isinstance(node, MappingNode):
        keys: set[Any] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise _Rejected("mapping keys must be plain values", key_node.start_mark.line + 1)
            if key_node.value in keys:
                raise _Rejected(f"duplicate key {key_node.value!r}", key_node.start_mark.line + 1)
            keys.add(key_node.value)
            _check(key_node, depth + 1)
            _check(value_node, depth + 1)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _check(item, depth + 1)


def _yaml_error(exc: yaml.YAMLError) -> PolicyError:
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    parts = [getattr(exc, "context", None), getattr(exc, "problem", None) or "invalid YAML"]
    return PolicyError("(document)", "; ".join(p for p in parts if p) + where)


def _path(location: tuple[int | str, ...]) -> str:
    path = ""
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part in RULE_TYPES:
            # The discriminated union adds the rule type to the location; it is not a field.
            continue
        else:
            path += f".{part}" if path else part
    return path or "(document)"


def _message(error: dict[str, Any]) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown field"
    if kind == "missing":
        return "required field is missing"
    if kind == "union_tag_invalid":
        return f"unknown rule type; expected one of: {', '.join(RULE_TYPES)}"
    if kind == "union_tag_not_found":
        return "rule type is missing"
    message = str(error["msg"])
    return message.removeprefix("Value error, ")


def digest(source: str) -> str:
    """SHA-256 of the exact UTF-8 source, as stored in ``policy.yaml_digest``."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_policy(source: str) -> ParsedPolicy:
    """Parse and validate ``source``; raise PolicyInvalid with every error found."""
    try:
        encoded = source.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise PolicyInvalid([PolicyError("(document)", "policy is not valid UTF-8")]) from None
    if len(encoded) > MAX_SOURCE_BYTES:
        raise PolicyInvalid(
            [PolicyError("(document)", f"policy is larger than {MAX_SOURCE_BYTES} bytes")]
        )
    try:
        data = yaml.load(source, Loader=_StrictLoader)  # noqa: S506 (restricted SafeLoader)
    except _Rejected as exc:
        raise PolicyInvalid([PolicyError("(document)", f"{exc} (line {exc.line})")]) from None
    except ConstructorError as exc:
        raise PolicyInvalid([_yaml_error(exc)]) from None
    except yaml.YAMLError as exc:
        raise PolicyInvalid([_yaml_error(exc)]) from None
    if not isinstance(data, dict):
        raise PolicyInvalid([PolicyError("(document)", "the document must be a mapping")])
    try:
        document = GitLabBaseline.model_validate(data)
    except ValidationError as exc:
        errors = [
            PolicyError(_path(tuple(error["loc"])), _message(dict(error)))
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise PolicyInvalid(errors) from None
    return ParsedPolicy(document=document, digest=digest(source))
