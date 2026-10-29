import json
from pathlib import Path

import pytest

from baluarte.policy.loader import (
    MAX_DEPTH,
    MAX_SOURCE_BYTES,
    PolicyError,
    PolicyInvalid,
    digest,
    parse_policy,
)
from baluarte.policy.schema import (
    GROUP_PATTERN,
    MAX_RULES,
    RULE_TYPES,
    DefaultBranchProtectedRule,
    ProjectVisibilityRule,
    published_schema,
)

SCHEMA_FILE = Path(__file__).parents[2] / "docs" / "schemas" / "gitlab-baseline.v1alpha1.json"

BASELINE = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: default-gitlab-baseline
spec:
  scope:
    groups: ["*"]
  rules:
    - id: project-visibility
      type: gitlab.project.visibility
      allowed: [private, internal]
    - id: default-branch-protected
      type: gitlab.branch.default_protected
      required: true
"""


def errors(source: str) -> list[PolicyError]:
    with pytest.raises(PolicyInvalid) as raised:
        parse_policy(source)
    return raised.value.errors


def only_error(source: str) -> PolicyError:
    found = errors(source)
    assert len(found) == 1, found
    return found[0]


def test_documented_example_is_valid() -> None:
    parsed = parse_policy(BASELINE)

    assert parsed.name == "default-gitlab-baseline"
    assert parsed.digest == digest(BASELINE)
    assert parsed.document.spec.scope.groups == ["*"]
    visibility, protected = parsed.document.spec.rules
    assert isinstance(visibility, ProjectVisibilityRule)
    assert visibility.allowed == ["private", "internal"]
    assert visibility.severity == "medium"
    assert isinstance(protected, DefaultBranchProtectedRule)
    assert protected.required is True


def test_digest_is_of_the_exact_source() -> None:
    assert parse_policy(BASELINE).digest != parse_policy(BASELINE + "# comment\n").digest


def test_published_schema_matches_the_models() -> None:
    assert json.loads(SCHEMA_FILE.read_text()) == published_schema()


@pytest.mark.parametrize(
    ("old", "new", "path", "message"),
    [
        (
            "allowed: [private, internal]",
            "allowed: [private, secret]",
            "spec.rules[0].allowed[1]",
            "Input should be 'private', 'internal' or 'public'",
        ),
        (
            "allowed: [private, internal]",
            "allowed: []",
            "spec.rules[0].allowed",
            "List should have at least 1 item after validation, not 0",
        ),
        (
            "allowed: [private, internal]",
            "allowed: [private, private]",
            "spec.rules[0]",
            "allowed must not repeat a visibility",
        ),
        (
            "required: true",
            'required: "yes"',
            "spec.rules[1].required",
            "Input should be a valid boolean",
        ),
        (
            "required: true",
            "required: 1",
            "spec.rules[1].required",
            "Input should be a valid boolean",
        ),
        (
            "type: gitlab.branch.default_protected",
            "type: gitlab.runner.shell",
            "spec.rules[1]",
            "unknown rule type; expected one of: " + ", ".join(RULE_TYPES),
        ),
        (
            "      type: gitlab.branch.default_protected\n",
            "",
            "spec.rules[1]",
            "rule type is missing",
        ),
        ("      required: true\n", "", "spec.rules[1].required", "required field is missing"),
        (
            "      required: true\n",
            "      required: true\n      exec: rm -rf /\n",
            "spec.rules[1].exec",
            "unknown field",
        ),
        (
            "  name: default-gitlab-baseline\n",
            "  name: default-gitlab-baseline\n  labels: {}\n",
            "metadata.labels",
            "unknown field",
        ),
        ("kind: GitLabBaseline\n", "kind: GitLabBaseline\nstatus: {}\n", "status", "unknown field"),
        (
            "apiVersion: baluarte.io/v1alpha1",
            "apiVersion: baluarte.io/v2",
            "apiVersion",
            "Input should be 'baluarte.io/v1alpha1'",
        ),
        ("kind: GitLabBaseline", "kind: Baseline", "kind", "Input should be 'GitLabBaseline'"),
        (
            "name: default-gitlab-baseline",
            "name: Default Baseline",
            "metadata.name",
            "String should match pattern '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'",
        ),
        (
            'groups: ["*"]',
            "groups: []",
            "spec.scope.groups",
            "List should have at least 1 item after validation, not 0",
        ),
        ('groups: ["*"]', 'groups: ["*", "*"]', "spec.scope", "groups must not repeat"),
        (
            'groups: ["*"]',
            'groups: ["../etc"]',
            "spec.scope.groups[0]",
            f"String should match pattern '{GROUP_PATTERN}'",
        ),
        (
            "id: default-branch-protected",
            "id: project-visibility",
            "spec",
            "rule id 'project-visibility' is used more than once",
        ),
    ],
)
def test_invalid_documents_report_a_path(old: str, new: str, path: str, message: str) -> None:
    assert old in BASELINE
    assert only_error(BASELINE.replace(old, new, 1)) == PolicyError(path, message)


def test_every_error_is_reported() -> None:
    source = BASELINE.replace("allowed: [private, internal]", "allowed: [secret]").replace(
        "required: true", "required: maybe"
    )

    assert [e.path for e in errors(source)] == [
        "spec.rules[0].allowed[0]",
        "spec.rules[1].required",
    ]


def test_messages_do_not_echo_input_values() -> None:
    secret = "glpat" + "-" + "Ab_" + "x" * 17
    source = BASELINE.replace("  name: default-gitlab-baseline\n", f"  name: {secret}\n")
    source = source.replace("required: true", f"required: {secret}")
    source = source.replace("kind: GitLabBaseline\n", f"kind: GitLabBaseline\nextra: {secret}\n")

    found = errors(source)

    assert sorted(e.path for e in found) == ["extra", "metadata.name", "spec.rules[1].required"]
    assert all(secret not in e.path and secret not in e.message for e in found)


def test_rule_count_is_bounded() -> None:
    rules = "".join(
        f"    - id: r{i}\n      type: gitlab.branch.default_protected\n      required: true\n"
        for i in range(MAX_RULES + 1)
    )
    source = BASELINE.split("  rules:\n")[0] + "  rules:\n" + rules

    assert only_error(source).path == "spec.rules"


def test_document_size_is_bounded() -> None:
    source = BASELINE + "# " + "x" * MAX_SOURCE_BYTES + "\n"

    assert only_error(source) == PolicyError(
        "(document)", f"policy is larger than {MAX_SOURCE_BYTES} bytes"
    )


def test_size_limit_counts_utf8_bytes() -> None:
    source = BASELINE + "# " + "é" * (MAX_SOURCE_BYTES // 2) + "\n"

    assert "larger than" in only_error(source).message


def test_nesting_depth_is_bounded() -> None:
    source = BASELINE + "extra: " + "[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1) + "\n"

    assert only_error(source).message.startswith(f"nesting is deeper than {MAX_DEPTH} levels")


@pytest.mark.parametrize(
    "source",
    [
        "!!python/object/apply:os.system ['true']\n",
        "apiVersion: !!python/name:os.system ''\n",
        "kind: !!binary aGVsbG8=\n",
        "kind: !custom GitLabBaseline\n",
        "kind: !!str GitLabBaseline\n",
        "--- !!map\nkind: GitLabBaseline\n",
    ],
)
def test_tags_are_rejected(source: str) -> None:
    assert only_error(source).message.startswith("YAML tags are not allowed")


def test_python_tags_execute_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = (
        "!!python/object/apply:pathlib.Path.touch "
        f"[!!python/object/apply:pathlib.Path ['{marker}']]\n"
    )

    errors(source)

    assert not marker.exists()


@pytest.mark.parametrize(
    "source",
    [
        BASELINE.replace('groups: ["*"]', 'groups: &all ["*"]'),
        BASELINE.replace('groups: ["*"]', 'groups: &all ["*"]\n    other: *all'),
        'a: &a ["x", "x"]\nb: &b [*a, *a]\nc: [*b, *b]\n',
        "base: &base {kind: GitLabBaseline}\n<<: *base\n",
    ],
)
def test_anchors_and_aliases_are_rejected(source: str) -> None:
    assert only_error(source).message.startswith("anchors and aliases are not allowed")


def test_merge_keys_are_ordinary_keys() -> None:
    source = BASELINE.replace("kind: GitLabBaseline\n", "kind: GitLabBaseline\n<<: {}\n")

    assert only_error(source) == PolicyError("<<", "unknown field")


def test_duplicate_keys_are_rejected() -> None:
    source = BASELINE.replace("required: true", "required: true\n      required: false")

    assert only_error(source) == PolicyError("(document)", "duplicate key 'required' (line 15)")


def test_complex_keys_are_rejected() -> None:
    assert "mapping keys must be plain values" in only_error("? [a, b]\n: c\n").message


def test_dates_are_not_converted() -> None:
    source = BASELINE.replace("  name: default-gitlab-baseline\n", "  name: 2026-01-01\n")

    # Stays the string "2026-01-01", which is a valid name.
    assert parse_policy(source).name == "2026-01-01"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "the document must be a mapping"),
        ("- a\n- b\n", "the document must be a mapping"),
        ("just text\n", "the document must be a mapping"),
        (
            BASELINE + "---\n" + BASELINE,
            "expected a single document in the stream; but found another document at line 15",
        ),
        (
            "kind: [unclosed\n",
            "while parsing a flow sequence; expected ',' or ']', but got '<stream end>' at line 2",
        ),
    ],
)
def test_malformed_documents(source: str, message: str) -> None:
    error = only_error(source)
    assert error.path == "(document)"
    assert error.message.startswith(message)


def test_unencodable_text_is_rejected() -> None:
    assert only_error("kind: \ud800\n") == PolicyError("(document)", "policy is not valid UTF-8")
