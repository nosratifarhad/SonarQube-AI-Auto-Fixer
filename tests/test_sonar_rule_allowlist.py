"""T28 unit tests: the pure, fail-closed Sonar rule allowlist policy (no I/O).

This file pins the whole contract of ``sonar_rule_allowlist``:

* the tables - every status, the diagnostic code map, ``PRECEDENCE`` and
  ``ALLOWED_STATUSES``;
* the one rule - an automatic fix is authorised only when the rule ID is
  *exactly* one of the configured allowlist entries, and the default
  configuration (an empty allowlist) denies everything;
* the rule ID grammar - the accepted bare and language-qualified forms, and
  every refused input (empty, whitespace, control characters, non-ASCII,
  wildcards, regexes, paths, URLs, shell fragments, separators, over-long values
  and every non-string value);
* exact matching - prefix, suffix, substring, case and qualifier collisions, and
  the property ``rule_id in allowed_rules <=> ALLOWED``;
* configuration validation - ``None``, every non-tuple container, non-string
  entries, malformed entries, duplicate entries, oversized configurations and
  the boundary sizes and lengths;
* fail closed - only ``ALLOWED`` reports ``can_auto_fix``, and the API cannot
  even express severity, issue type, language, confidence, history or message
  text;
* the exact ``PRECEDENCE`` with a conflict case per condition, precedence over
  determinism, purity, immutability and the deterministic, secret-free
  ``as_dict()`` serialization;
* the module surface: standard library only (no ``re``, no repository module),
  no I/O, no command, no wildcard/prefix/substring logic and "not wired".

The tests never touch Git, the network, a subprocess or the filesystem (the only
file read is this module's own source, for the static audit).
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import types
from dataclasses import FrozenInstanceError

import pytest

from commit_message import build_commit_message

import sonar_rule_allowlist as module
from sonar_rule_allowlist import (
    ALLOWED_STATUSES,
    DEFAULT_ALLOWED_RULES,
    DIAGNOSTIC_CODES,
    MAX_ALLOWED_RULES,
    MAX_RULE_ID_LENGTH,
    POLICY_VERSION,
    PRECEDENCE,
    RuleAllowlistDecision,
    RuleAllowlistEvaluation,
    RuleAllowlistInput,
    RuleAllowlistPolicy,
    RuleAllowlistStatus,
    evaluate_rule_allowlist,
)

#: The status vocabulary, aliased so the tests read as policy.
S = RuleAllowlistStatus

#: The allowlist used by most tests (the three rules named in the T28 task).
ALLOWED_RULES = ("S1118", "S1192", "S3776")

#: The configuration every test that does not care about it uses.
POLICY = RuleAllowlistPolicy(allowed_rules=ALLOWED_RULES)


def evaluate(rule_id, allowed_rules=ALLOWED_RULES):
    """Evaluate one rule ID against one allowlist (the public API only)."""
    return evaluate_rule_allowlist(
        policy=RuleAllowlistPolicy(allowed_rules=allowed_rules),
        rule_input=RuleAllowlistInput(rule_id=rule_id),
    )


def show(policy, rule_id):
    """Evaluate one rule ID against one already-built policy."""
    return evaluate_rule_allowlist(
        policy=policy, rule_input=RuleAllowlistInput(rule_id=rule_id)
    )


def dataclass_fields(record):
    """The field names of one of this module's frozen records, in order."""
    return tuple(record.__dataclass_fields__)


#: Rule IDs the grammar accepts: the bare Sonar key (``S1118``) and the
#: repository-qualified key this project's Sonar model actually carries
#: (``python:S1481``, ``javascript:S1854``).
VALID_RULE_IDS = (
    "S1118",
    "S1192",
    "S3776",
    "S1",
    "S0",
    "S1234567890123456789",
    "python:S1481",
    "java:S108",
    "javascript:S1854",
    "csharpsquid:S1118",
    "Web:S1234",
    "common-java:DuplicatedBlocks",
    "external_eslint_repo:no-unused-vars",
    "S1118.S1",
    "S1118_1",
    "S1118-1",
    "py.thon:S1481",
    "a",
    "A",
    "9",
    "Rule_1.2-3:key_4.5-6",
)

#: Strings that are *not* usable rule IDs: every one of these is refused with
#: ``INVALID_RULE_ID``, and the value is never echoed back.
INVALID_RULE_ID_CASES = {
    "empty": "",
    "space only": " ",
    "tab only": "\t",
    "newline only": "\n",
    "leading space": " S1118",
    "trailing space": "S1118 ",
    "embedded space": "S 1118",
    "trailing newline": "S1118\n",
    "trailing tab": "S1118\t",
    "embedded newline": "S11\n18",
    "carriage return": "S1118\r",
    "null byte": "S1118\x00",
    "bell": "S1118\x07",
    "delete": "S1118\x7f",
    "non-ascii letter": "S1118\u00e9",
    "fullwidth digits": "\uff19\uff11\uff11\uff18",
    "non-breaking space": "S1118\u00a0",
    "wildcard star": "S1118*",
    "wildcard prefix": "S11*",
    "wildcard alone": "*",
    "wildcard double": "**",
    "wildcard question": "S11?",
    "character class": "S1[0-9]",
    "regex anchor start": "^S11",
    "regex anchor end": "S11$",
    "regex group": "(?i)S1118",
    "regex any": ".*",
    "regex suffix": "S.*",
    "regex plus": "S11+",
    "path absolute": "/etc/passwd",
    "path relative": "../../S1118",
    "path windows": "C:\\Windows\\S1118",
    "path like": "src/S1118",
    "url": "https://sonar.example.com/api/rules",
    "url short": "http://S1118",
    "shell and remove": "S1118; rm -rf /",
    "shell substitution": "$(id)",
    "shell backticks": "`id`",
    "shell pipe": "S1118|id",
    "shell and": "S1118&&id",
    "shell redirect": "S1118>out",
    "equals": "S1118=1",
    "at sign": "S1118@x",
    "hash": "S1118#x",
    "only separator": ":",
    "empty qualifier": ":S1118",
    "empty key": "S1118:",
    "double separator": "S1118::S1",
    "three parts": "a:b:c",
    "leading dot": ".S1118",
    "leading dotdot": "..",
    "leading hyphen": "-S1118",
    "leading underscore": "_S1118",
}

#: Values that are not strings at all: every one of these is refused with
#: ``INVALID_INPUT``, and none of them is ever coerced with ``str()``.
INVALID_INPUT_CASES = {
    "none": None,
    "true": True,
    "false": False,
    "int": 7,
    "zero": 0,
    "float": 1.5,
    "bytes": b"S1118",
    "bytearray": bytearray(b"S1118"),
    "list": ["S1118"],
    "tuple": ("S1118",),
    "set": {"S1118"},
    "frozenset": frozenset({"S1118"}),
    "dict": {"rule_id": "S1118"},
    "path": pathlib.Path("S1118"),
    "complex": complex(1, 0),
    "object": object(),
    "range": range(1),
    "type": str,
}


# ---------------------------------------------------------------------------
# A. The rule ID grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", VALID_RULE_IDS)
def test_a_grammar_valid_rule_id_is_a_usable_identity(rule_id):
    assert RuleAllowlistInput(rule_id=rule_id).is_usable_rule_id is True


@pytest.mark.parametrize("rule_id", VALID_RULE_IDS)
def test_a_grammar_valid_rule_that_is_not_listed_is_refused(rule_id):
    result = evaluate(rule_id, allowed_rules=())
    assert result.status is S.RULE_NOT_ALLOWED
    assert result.can_auto_fix is False
    assert result.rule_id == rule_id


@pytest.mark.parametrize("rule_id", VALID_RULE_IDS)
def test_a_grammar_valid_rule_is_never_reported_as_malformed(rule_id):
    assert evaluate(rule_id).status is not S.INVALID_RULE_ID


@pytest.mark.parametrize(
    "rule_id", INVALID_RULE_ID_CASES.values(), ids=list(INVALID_RULE_ID_CASES)
)
def test_an_unusable_rule_id_is_refused(rule_id):
    result = evaluate(rule_id)
    assert result.status is S.INVALID_RULE_ID
    assert result.can_auto_fix is False
    assert result.decision is RuleAllowlistDecision.REFUSE
    assert result.diagnostic_code == "T28_RULE_ID_INVALID"


@pytest.mark.parametrize(
    "rule_id", INVALID_RULE_ID_CASES.values(), ids=list(INVALID_RULE_ID_CASES)
)
def test_an_unusable_rule_id_is_never_echoed(rule_id):
    result = evaluate(rule_id)
    assert result.rule_id is None
    assert RuleAllowlistInput(rule_id=rule_id).as_dict() == {
        "rule_id": None,
        "is_usable_rule_id": False,
    }


@pytest.mark.parametrize(
    "rule_id", INVALID_INPUT_CASES.values(), ids=list(INVALID_INPUT_CASES)
)
def test_a_non_string_rule_id_is_refused(rule_id):
    result = evaluate(rule_id)
    assert result.status is S.INVALID_INPUT
    assert result.can_auto_fix is False
    assert result.rule_id is None
    assert result.diagnostic_code == "T28_INPUT_INVALID"


def test_a_clever_object_is_never_coerced_into_a_rule_id():
    """``str(value)`` is never called: a lie about being a rule ID is refused."""

    class Impostor:
        def __str__(self):  # pragma: no cover - must never be called
            return "S1118"

        def __repr__(self):  # pragma: no cover - must never be called
            return "S1118"

        def __bool__(self):  # pragma: no cover - must never be called
            return True

        def __eq__(self, other):  # pragma: no cover - must never be called
            return other == "S1118"

    result = evaluate(Impostor(), allowed_rules=("S1118",))
    assert result.status is S.INVALID_INPUT
    assert result.can_auto_fix is False
    assert result.rule_id is None


def test_the_maximum_rule_id_length_boundaries():
    listed = "S" + "1" * (MAX_RULE_ID_LENGTH - 1)
    assert len(listed) == MAX_RULE_ID_LENGTH
    assert evaluate(listed, allowed_rules=(listed,)).status is S.ALLOWED

    one_under = "S" + "1" * (MAX_RULE_ID_LENGTH - 2)
    assert len(one_under) == MAX_RULE_ID_LENGTH - 1
    assert evaluate(one_under, allowed_rules=(one_under,)).status is S.ALLOWED
    assert evaluate(one_under, allowed_rules=()).status is S.RULE_NOT_ALLOWED

    one_over = "S" + "1" * MAX_RULE_ID_LENGTH
    assert len(one_over) == MAX_RULE_ID_LENGTH + 1
    assert evaluate(one_over).status is S.INVALID_RULE_ID
    assert RuleAllowlistPolicy(allowed_rules=(one_over,)).is_valid is False


def test_every_non_ascii_character_class_is_refused():
    for character in ("\u00e9", "\u00a0", "\uff19", "\u2028", "\u0085", "\u20ac"):
        assert evaluate(f"S1118{character}").status is S.INVALID_RULE_ID
        assert evaluate(f"{character}S1118").status is S.INVALID_RULE_ID


# ---------------------------------------------------------------------------
# B. Exact matching only (never a prefix, suffix, substring or case variant)
# ---------------------------------------------------------------------------

#: ``(listed, other)`` pairs whose two members are similar but never equal.
COLLISION_PAIRS = (
    ("S1118", "S111"),  # prefix of the listed rule
    ("S1118", "S11"),
    ("S1118", "S1"),
    ("S1118", "S"),
    ("S1118", "1118"),  # suffix of the listed rule
    ("S1118", "118"),
    ("S1192", "S119"),
    ("S3776", "S377"),
    ("S1118", "S11188"),  # extension: the listed rule is its prefix
    ("S1118", "xS1118"),  # substring with a prefix
    ("S1118", "S1118x"),  # substring with a suffix
    ("S1118", "s1118"),  # case variant
    ("S1118", "java:S1118"),  # qualified variant
    ("S1118", "csharpsquid:S1118"),
    ("S1118", "python:S1118"),
    ("python:S1481", "S1481"),  # the qualified rule vs its bare key
    ("python:S1481", "python:S148"),
    ("python:S1481", "Python:S1481"),  # case variant of the qualifier
    ("python:S1481", "python:s1481"),
    ("javascript:S1854", "js:S1854"),
)


@pytest.mark.parametrize("listed, other", COLLISION_PAIRS)
def test_a_similar_rule_is_never_the_listed_rule(listed, other):
    result = evaluate(other, allowed_rules=(listed,))
    assert result.status is S.RULE_NOT_ALLOWED
    assert result.can_auto_fix is False
    assert result.rule_id == other  # a usable identity is echoed verbatim


@pytest.mark.parametrize("listed, other", COLLISION_PAIRS)
def test_the_listed_rule_is_allowed_and_the_similar_one_is_not(listed, other):
    assert evaluate(listed, allowed_rules=(listed,)).status is S.ALLOWED
    assert evaluate(listed, allowed_rules=(other,)).status is S.RULE_NOT_ALLOWED


@pytest.mark.parametrize(
    "rule_id",
    ("S111", "S1118*", "S11*", "xS1118", "S1118x", "s1118", "java:S1118"),
)
def test_the_task_documented_inequalities(rule_id):
    result = evaluate(rule_id, allowed_rules=("S1118",))
    assert result.status is not S.ALLOWED
    assert result.can_auto_fix is False


#: The rules and the configurations used by the exhaustive membership matrix.
MATRIX_RULES = (
    "S1118",
    "S111",
    "S1192",
    "python:S1481",
    "S1481",
    "S1118x",
    "java:S1192",
)
MATRIX_ALLOWLISTS = (
    (),
    ("S1118",),
    ("S1118", "S1192"),
    ("python:S1481",),
    ("S111",),
    ("S1118", "python:S1481", "java:S1192"),
)


@pytest.mark.parametrize("allowlist", MATRIX_ALLOWLISTS)
@pytest.mark.parametrize("rule_id", MATRIX_RULES)
def test_the_membership_iff_allowed_matrix(rule_id, allowlist):
    """``rule_id in allowed_rules <=> ALLOWED`` - the whole contract."""
    result = evaluate(rule_id, allowed_rules=allowlist)
    expected = rule_id in allowlist
    assert (result.status is S.ALLOWED) is expected
    assert result.can_auto_fix is expected
    if expected:
        assert result.diagnostic_code == "T28_RULE_ALLOWED"
        assert result.rule_id == rule_id
    else:
        assert result.status is S.RULE_NOT_ALLOWED
        assert result.diagnostic_code == "T28_RULE_NOT_ALLOWED"


def test_membership_does_not_depend_on_the_configured_order():
    for rule_id in MATRIX_RULES:
        forward = evaluate(rule_id, allowed_rules=("S1118", "S1192"))
        reverse = evaluate(rule_id, allowed_rules=("S1192", "S1118"))
        assert forward.status is reverse.status
        assert forward.can_auto_fix is reverse.can_auto_fix


# ---------------------------------------------------------------------------
# C. Configuration validation (the allowlist is data, and data is validated)
# ---------------------------------------------------------------------------

#: Containers that are not an immutable tuple: every one is refused, because a
#: mutable or unordered structure could change after it was validated.
NON_TUPLE_ALLOWLISTS = {
    "list": ["S1118"],
    "set": {"S1118"},
    "frozenset": frozenset({"S1118"}),
    "dict": {"S1118": True},
    "string": "S1118",
    "bytes": b"S1118",
    "bytearray": bytearray(b"S1118"),
    "generator": (value for value in ("S1118",)),
    "iterator": iter(["S1118"]),
    "range": range(1),
    "int": 1,
    "bool": True,
    "float": 1.0,
    "object": object(),
    "path": pathlib.Path("S1118"),
}


def test_the_default_configuration_denies_everything():
    policy = RuleAllowlistPolicy()
    assert policy.allowed_rules == DEFAULT_ALLOWED_RULES == ()
    assert policy.is_valid is True
    assert policy.refusal_reason is None
    assert policy.allowed_rule_keys == ()
    assert show(policy, "S1118").status is S.RULE_NOT_ALLOWED
    assert show(policy, "S1118").can_auto_fix is False


def test_an_empty_allowlist_is_valid_and_maximally_strict():
    policy = RuleAllowlistPolicy(allowed_rules=())
    assert policy.is_valid is True
    assert policy.allowed_rule_keys == ()
    for rule_id in VALID_RULE_IDS:
        result = show(policy, rule_id)
        assert result.status is S.RULE_NOT_ALLOWED
        assert result.can_auto_fix is False


def test_none_never_means_that_nothing_is_allowlisted():
    policy = RuleAllowlistPolicy(allowed_rules=None)
    assert policy.is_valid is False
    assert policy.allowed_rule_keys == ()
    assert policy.as_dict()["allowed_rules"] is None
    assert show(policy, "S1118").status is S.INVALID_POLICY


@pytest.mark.parametrize(
    "value", NON_TUPLE_ALLOWLISTS.values(), ids=list(NON_TUPLE_ALLOWLISTS)
)
def test_a_non_tuple_allowlist_is_refused(value):
    policy = RuleAllowlistPolicy(allowed_rules=value)
    assert policy.is_valid is False
    assert policy.refusal_reason is not None
    assert policy.allowed_rule_keys == ()
    result = show(policy, "S1118")
    assert result.status is S.INVALID_POLICY
    assert result.can_auto_fix is False
    assert result.rule_id is None
    assert result.diagnostic_code == "T28_POLICY_INVALID"


@pytest.mark.parametrize(
    "entry", INVALID_INPUT_CASES.values(), ids=list(INVALID_INPUT_CASES)
)
def test_a_non_string_entry_makes_the_policy_unusable(entry):
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", entry))
    assert policy.is_valid is False
    assert show(policy, "S1118").status is S.INVALID_POLICY


@pytest.mark.parametrize(
    "entry", INVALID_RULE_ID_CASES.values(), ids=list(INVALID_RULE_ID_CASES)
)
def test_a_malformed_entry_makes_the_policy_unusable(entry):
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", entry))
    assert policy.is_valid is False
    assert show(policy, entry).status is S.INVALID_POLICY
    assert show(policy, entry).can_auto_fix is False


@pytest.mark.parametrize(
    "entry", INVALID_RULE_ID_CASES.values(), ids=list(INVALID_RULE_ID_CASES)
)
def test_a_malformed_entry_is_never_echoed(entry):
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", entry))
    assert policy.as_dict()["allowed_rules"] is None
    assert policy.allowed_rule_keys == ()
    if len(entry) > 1:
        assert entry not in json.dumps(policy.as_dict())


def test_a_duplicate_entry_is_refused_not_deduplicated():
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", "S1192", "S1118"))
    assert policy.is_valid is False
    assert "twice" in policy.refusal_reason
    assert "'S1118'" in policy.refusal_reason
    result = show(policy, "S1118")
    assert result.status is S.INVALID_POLICY
    assert result.can_auto_fix is False


def test_a_case_variant_is_a_different_rule_not_a_duplicate():
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", "s1118"))
    assert policy.is_valid is True
    assert policy.allowed_rule_keys == ("S1118", "s1118")
    assert show(policy, "S1118").status is S.ALLOWED
    assert show(policy, "s1118").status is S.ALLOWED
    assert show(policy, "S1119").status is S.RULE_NOT_ALLOWED


def test_the_maximum_allowlist_size_boundaries():
    at_max = tuple(f"S{index}" for index in range(MAX_ALLOWED_RULES))
    assert len(at_max) == MAX_ALLOWED_RULES
    policy = RuleAllowlistPolicy(allowed_rules=at_max)
    assert policy.is_valid is True
    assert show(policy, "S0").status is S.ALLOWED
    assert show(policy, f"S{MAX_ALLOWED_RULES - 1}").status is S.ALLOWED
    assert show(policy, "S9999999").status is S.RULE_NOT_ALLOWED

    one_under = at_max[:-1]
    assert len(one_under) == MAX_ALLOWED_RULES - 1
    assert RuleAllowlistPolicy(allowed_rules=one_under).is_valid is True

    one_over = at_max + ("SX",)
    assert len(one_over) == MAX_ALLOWED_RULES + 1
    oversized = RuleAllowlistPolicy(allowed_rules=one_over)
    assert oversized.is_valid is False
    assert str(MAX_ALLOWED_RULES) in oversized.refusal_reason
    assert show(oversized, "S0").status is S.INVALID_POLICY


def test_the_size_bound_is_reported_before_any_entry_problem():
    oversized = tuple(f"S{index}" for index in range(MAX_ALLOWED_RULES)) + ("S*",)
    policy = RuleAllowlistPolicy(allowed_rules=oversized)
    assert policy.is_valid is False
    assert "exceeds the supported maximum" in policy.refusal_reason
    assert "'S*'" not in policy.refusal_reason


def test_the_rule_id_length_boundary_is_applied_to_entries():
    listed = "S" + "1" * (MAX_RULE_ID_LENGTH - 1)
    assert RuleAllowlistPolicy(allowed_rules=(listed,)).is_valid is True
    too_long = listed + "1"
    assert len(too_long) == MAX_RULE_ID_LENGTH + 1
    assert RuleAllowlistPolicy(allowed_rules=(listed, too_long)).is_valid is False


def test_the_refusal_reason_identifies_the_offending_entry_only():
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", 7))
    assert "allowed rule entry 2" in policy.refusal_reason
    assert "int" in policy.refusal_reason

    policy = RuleAllowlistPolicy(allowed_rules=("S1118", "S11*"))
    assert "allowed rule entry 2" in policy.refusal_reason
    assert "S11*" not in policy.refusal_reason


#: The exact key set of the policy's serialization.
POLICY_DICT_KEYS = {
    "policy_version",
    "allowed_rules",
    "allowed_rule_count",
    "maximum_supported_rules",
    "maximum_rule_id_length",
    "is_valid",
    "refusal_reason",
}


def test_the_policy_serializes_its_validated_configuration_in_order():
    payload = POLICY.as_dict()
    assert set(payload) == POLICY_DICT_KEYS
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["allowed_rules"] == list(ALLOWED_RULES)
    assert payload["allowed_rule_count"] == len(ALLOWED_RULES)
    assert payload["maximum_supported_rules"] == MAX_ALLOWED_RULES
    assert payload["maximum_rule_id_length"] == MAX_RULE_ID_LENGTH
    assert payload["is_valid"] is True
    assert payload["refusal_reason"] is None


def test_an_unusable_policy_serializes_no_entries():
    payload = RuleAllowlistPolicy(allowed_rules=["S1118"]).as_dict()
    assert set(payload) == POLICY_DICT_KEYS
    assert payload["allowed_rules"] is None
    assert payload["allowed_rule_count"] is None
    assert payload["is_valid"] is False
    assert isinstance(payload["refusal_reason"], str)


# ---------------------------------------------------------------------------
# D. Fail closed: only ALLOWED can authorise, and nothing else is expressible
# ---------------------------------------------------------------------------


def test_the_allowing_statuses_are_exactly_the_allowed_one():
    assert ALLOWED_STATUSES == (S.ALLOWED,)
    assert S.ALLOWED in PRECEDENCE


@pytest.mark.parametrize("status", list(S))
def test_every_status_has_a_stable_t28_diagnostic_code(status):
    code = DIAGNOSTIC_CODES[status]
    assert code.startswith("T28_")
    assert code == code.upper()
    assert code == DIAGNOSTIC_CODES[status]


@pytest.mark.parametrize(
    "policy, rule_id",
    (
        (POLICY, "S1118"),
        (POLICY, "S9999"),
        (POLICY, "S11*"),
        (POLICY, None),
        (RuleAllowlistPolicy(), "S1118"),
        (RuleAllowlistPolicy(allowed_rules=None), "S1118"),
        (RuleAllowlistPolicy(allowed_rules=["S1118"]), "S1118"),
        (RuleAllowlistPolicy(allowed_rules=("S1118", "S1118")), "S1118"),
        (RuleAllowlistPolicy(allowed_rules=("S1118*",)), "S1118"),
    ),
    ids=(
        "allowed",
        "unlisted",
        "malformed",
        "absent",
        "empty allowlist",
        "none allowlist",
        "list allowlist",
        "duplicate entry",
        "pattern entry",
    ),
)
def test_can_auto_fix_is_true_only_for_the_allowed_status(policy, rule_id):
    result = show(policy, rule_id)
    assert result.can_auto_fix is (result.status is S.ALLOWED)
    assert result.can_auto_fix is (result.status in ALLOWED_STATUSES)
    assert result.decision is (
        RuleAllowlistDecision.ALLOW
        if result.status is S.ALLOWED
        else RuleAllowlistDecision.REFUSE
    )
    assert result.can_auto_fix is (result.decision is RuleAllowlistDecision.ALLOW)


@pytest.mark.parametrize(
    "policy, rule_id",
    (
        (POLICY, "S1118"),
        (POLICY, "S9999"),
        (POLICY, "S11*"),
        (POLICY, None),
        (RuleAllowlistPolicy(), "S1118"),
        (RuleAllowlistPolicy(allowed_rules=None), "S1118"),
    ),
)
def test_an_echoed_rule_id_is_always_a_usable_one(policy, rule_id):
    result = show(policy, rule_id)
    if result.rule_id is not None:
        assert RuleAllowlistInput(rule_id=result.rule_id).is_usable_rule_id


def test_the_api_cannot_express_severity_type_language_or_confidence():
    """There is no parameter through which anything else could widen a verdict."""
    parameters = inspect.signature(evaluate_rule_allowlist).parameters
    assert set(parameters) == {"policy", "rule_input"}
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )
    assert dataclass_fields(RuleAllowlistInput) == ("rule_id",)
    assert dataclass_fields(RuleAllowlistPolicy) == ("allowed_rules",)

    forbidden = (
        "severity",
        "confidence",
        "score",
        "history",
        "message",
        "language",
        "issue",
        "fixable",
        "trusted",
    )
    names = [name.lower() for name in dir(module)]
    assert not [
        name for name in names if any(word in name for word in forbidden)
    ]


# ---------------------------------------------------------------------------
# E. Precedence
# ---------------------------------------------------------------------------


def test_the_precedence_is_the_documented_order():
    assert PRECEDENCE == (
        S.INVALID_POLICY,
        S.INVALID_INPUT,
        S.INVALID_RULE_ID,
        S.RULE_NOT_ALLOWED,
        S.ALLOWED,
    )
    assert len(PRECEDENCE) == len(set(PRECEDENCE)) == len(S)
    assert set(PRECEDENCE) == set(S)
    assert set(DIAGNOSTIC_CODES) == set(S)


@pytest.mark.parametrize(
    "allowlist",
    (None, ["S1118"], {"S1118"}, "S1118", ("S1118", "S1118"), ("S1118*",)),
    ids=(
        "none",
        "list",
        "set",
        "string",
        "duplicate",
        "pattern",
    ),
)
def test_an_unusable_policy_refuses_before_any_rule_is_read(allowlist):
    policy = RuleAllowlistPolicy(allowed_rules=allowlist)
    for rule_id in ("S1118", "S1118 ", None, 7, "S11*", "S9999"):
        result = show(policy, rule_id)
        assert result.status is S.INVALID_POLICY
        assert result.can_auto_fix is False


@pytest.mark.parametrize(
    "policy, rule_id, expected",
    (
        (RuleAllowlistPolicy(allowed_rules=None), "S11*", S.INVALID_POLICY),
        (RuleAllowlistPolicy(allowed_rules=None), None, S.INVALID_POLICY),
        (RuleAllowlistPolicy(allowed_rules=["S1118"]), "S1118", S.INVALID_POLICY),
        (RuleAllowlistPolicy(allowed_rules=("S1118*",)), "S1118", S.INVALID_POLICY),
        (POLICY, None, S.INVALID_INPUT),
        (POLICY, 7, S.INVALID_INPUT),
        (POLICY, True, S.INVALID_INPUT),
        (POLICY, "S11*", S.INVALID_RULE_ID),
        (POLICY, "", S.INVALID_RULE_ID),
        (POLICY, "S1118 ", S.INVALID_RULE_ID),
        (POLICY, "S9999", S.RULE_NOT_ALLOWED),
        (POLICY, "S111", S.RULE_NOT_ALLOWED),
        (POLICY, "java:S1118", S.RULE_NOT_ALLOWED),
        (POLICY, "S1118", S.ALLOWED),
        (POLICY, "S1192", S.ALLOWED),
    ),
)
def test_each_condition_is_decided_by_the_precedence_order(policy, rule_id, expected):
    assert show(policy, rule_id).status is expected


def test_the_two_malformed_input_statuses_are_disjoint_by_construction():
    """A *type* problem is INVALID_INPUT; a *content* problem is INVALID_RULE_ID."""
    for value in INVALID_INPUT_CASES.values():
        assert show(POLICY, value).status is S.INVALID_INPUT
    for value in INVALID_RULE_ID_CASES.values():
        assert show(POLICY, value).status is S.INVALID_RULE_ID


@pytest.mark.parametrize(
    "value", NON_TUPLE_ALLOWLISTS.values(), ids=list(NON_TUPLE_ALLOWLISTS)
)
def test_a_malformed_policy_never_allows_any_valid_rule(value):
    policy = RuleAllowlistPolicy(allowed_rules=value)
    for rule_id in VALID_RULE_IDS:
        assert show(policy, rule_id).can_auto_fix is False


# ---------------------------------------------------------------------------
# F. Immutability (frozen records, no mutable state exposed)
# ---------------------------------------------------------------------------


def test_the_policy_is_frozen_and_holds_an_immutable_configuration():
    policy = RuleAllowlistPolicy(allowed_rules=("S1118",))
    with pytest.raises(FrozenInstanceError):
        policy.allowed_rules = ("S1119",)
    with pytest.raises(FrozenInstanceError):
        del policy.allowed_rules
    assert isinstance(policy.allowed_rules, tuple)
    assert policy.allowed_rules == ("S1118",)
    assert policy.allowed_rule_keys == ("S1118",)


def test_the_input_is_frozen():
    rule_input = RuleAllowlistInput(rule_id="S1118")
    with pytest.raises(FrozenInstanceError):
        rule_input.rule_id = "S1192"
    assert rule_input.rule_id == "S1118"


def test_the_evaluation_is_frozen_and_derives_permission():
    result = evaluate("S1118")
    with pytest.raises(FrozenInstanceError):
        result.status = S.RULE_NOT_ALLOWED
    with pytest.raises(FrozenInstanceError):
        result.decision = RuleAllowlistDecision.REFUSE
    with pytest.raises(FrozenInstanceError):
        result.rule_id = "S9999"
    assert isinstance(result.reasons, tuple)
    assert result.can_auto_fix is True


def test_a_mutated_as_dict_copy_cannot_change_the_policy_or_the_verdict():
    payload = evaluate("S1118").as_dict()
    payload["reasons"].append("tampered")
    payload["policy"]["allowed_rules"].append("S9999")
    fresh = evaluate("S1118")
    assert fresh.status is S.ALLOWED
    assert fresh.reasons == evaluate("S1118").reasons
    assert POLICY.allowed_rule_keys == ALLOWED_RULES
    assert show(POLICY, "S9999").status is S.RULE_NOT_ALLOWED


def test_the_evaluation_keeps_the_policy_it_used():
    result = show(POLICY, "S1118")
    assert result.policy is POLICY
    assert result.policy_version == POLICY_VERSION


def test_a_caller_owned_list_can_never_be_smuggled_in_as_the_allowlist():
    values = ["S1118"]
    policy = RuleAllowlistPolicy(allowed_rules=values)
    values.append("S9999")
    assert policy.is_valid is False
    assert show(policy, "S1118").status is S.INVALID_POLICY
    assert show(policy, "S9999").status is S.INVALID_POLICY


def test_the_evaluation_never_mutates_the_caller_values():
    allowed = ("S1118",)
    policy = RuleAllowlistPolicy(allowed_rules=allowed)
    rule_input = RuleAllowlistInput(rule_id="S1118")
    evaluate_rule_allowlist(policy=policy, rule_input=rule_input)
    assert allowed == ("S1118",)
    assert rule_input.rule_id == "S1118"
    assert policy.allowed_rules == ("S1118",)


# ---------------------------------------------------------------------------
# G. Determinism and purity
# ---------------------------------------------------------------------------


def test_the_same_arguments_always_produce_an_equal_verdict():
    for rule_id in MATRIX_RULES:
        first = evaluate(rule_id)
        second = evaluate(rule_id)
        assert first == second
        assert first is not second
        assert first.as_dict() == second.as_dict()


def test_repeated_evaluation_changes_no_module_state():
    before = {name: id(value) for name, value in vars(module).items()}
    snapshot = POLICY.as_dict()
    for rule_id in ("S1118", "S9999", "S11*", None, 7):
        evaluate(rule_id)
    evaluate("S1118", allowed_rules=())
    after = {name: id(value) for name, value in vars(module).items()}
    assert before == after
    assert POLICY.as_dict() == snapshot


def test_every_verdict_is_json_native():
    for rule_id in ("S1118", "S9999", "S11*", None, 7):
        payload = evaluate(rule_id).as_dict()
        assert json.loads(json.dumps(payload)) == payload
    assert json.loads(json.dumps(POLICY.as_dict())) == POLICY.as_dict()


# ---------------------------------------------------------------------------
# H. Serialization (deterministic, documented, secret-free)
# ---------------------------------------------------------------------------

#: The exact key set of the verdict's serialization.
EVALUATION_DICT_KEYS = {
    "policy_version",
    "status",
    "decision",
    "can_auto_fix",
    "rule_id",
    "diagnostic_code",
    "reason",
    "reasons",
    "policy",
}


def test_the_allowed_verdict_serializes_the_documented_shape():
    result = evaluate("S1118")
    payload = result.as_dict()
    assert set(payload) == EVALUATION_DICT_KEYS
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["status"] == "allowed"
    assert payload["decision"] == "allow"
    assert payload["can_auto_fix"] is True
    assert payload["rule_id"] == "S1118"
    assert payload["diagnostic_code"] == "T28_RULE_ALLOWED"
    assert payload["reason"] == result.reason
    assert payload["reasons"] == list(result.reasons)
    assert payload["policy"] == POLICY.as_dict()


def test_the_refusal_verdict_serializes_the_documented_shape():
    payload = evaluate("S9999").as_dict()
    assert set(payload) == EVALUATION_DICT_KEYS
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["status"] == "rule-not-allowed"
    assert payload["decision"] == "refuse"
    assert payload["can_auto_fix"] is False
    assert payload["rule_id"] == "S9999"
    assert payload["diagnostic_code"] == "T28_RULE_NOT_ALLOWED"


def test_the_serialized_rationale_is_the_reason_plus_its_consequence():
    for rule_id in ("S1118", "S9999", "S11*", None, 7):
        result = evaluate(rule_id)
        assert len(result.reasons) == 2
        assert result.reasons[0] == result.reason
        assert result.as_dict()["reasons"] == [result.reason, result.reasons[1]]


def test_an_unusable_verdict_serializes_no_rule_id():
    values = list(INVALID_INPUT_CASES.values())
    values += list(INVALID_RULE_ID_CASES.values())
    for value in values:
        assert evaluate(value).as_dict()["rule_id"] is None


def test_the_input_serializes_only_a_usable_rule_id():
    assert RuleAllowlistInput(rule_id="python:S1481").as_dict() == {
        "rule_id": "python:S1481",
        "is_usable_rule_id": True,
    }
    assert RuleAllowlistInput().as_dict() == {
        "rule_id": None,
        "is_usable_rule_id": False,
    }


# ---------------------------------------------------------------------------
# I. The public API boundary (a caller error is not a rule verdict)
# ---------------------------------------------------------------------------


def test_a_non_dto_policy_is_a_programming_error_not_a_refusal():
    with pytest.raises(TypeError) as excinfo:
        evaluate_rule_allowlist(
            policy={}, rule_input=RuleAllowlistInput(rule_id="S1118")
        )
    assert "RuleAllowlistPolicy" in str(excinfo.value)
    assert "dict" in str(excinfo.value)


def test_a_non_dto_input_is_a_programming_error_not_a_refusal():
    with pytest.raises(TypeError) as excinfo:
        evaluate_rule_allowlist(policy=POLICY, rule_input="S1118")
    assert "RuleAllowlistInput" in str(excinfo.value)
    assert "str" in str(excinfo.value)


def test_the_evaluator_accepts_keywords_only():
    result = evaluate_rule_allowlist(
        policy=POLICY, rule_input=RuleAllowlistInput(rule_id="S1118")
    )
    assert result.can_auto_fix is True
    with pytest.raises(TypeError):
        evaluate_rule_allowlist(POLICY, RuleAllowlistInput(rule_id="S1118"))


# ---------------------------------------------------------------------------
# J. Secret and data safety (bounded diagnostics, no echo of hostile values)
# ---------------------------------------------------------------------------


def test_a_huge_rule_id_is_refused_and_never_published():
    huge = "S" + "1" * 100_000
    result = evaluate(huge)
    assert result.status is S.INVALID_RULE_ID
    assert result.rule_id is None
    assert huge not in result.reason
    assert huge not in json.dumps(result.as_dict())


def test_a_huge_policy_entry_is_refused_and_never_published():
    huge = "S" + "1" * 100_000
    policy = RuleAllowlistPolicy(allowed_rules=(huge,))
    assert policy.is_valid is False
    assert policy.allowed_rule_keys == ()
    assert huge not in policy.refusal_reason
    assert huge not in json.dumps(policy.as_dict())


@pytest.mark.parametrize(
    "value",
    (
        "S1118; rm -rf /",
        "/etc/passwd",
        "https://user:token@sonar.example.com/api",
        "$(id)",
        "S1118`id`",
        "S1118\nSONAR_TOKEN",
        "..\\..\\secrets.env",
        "s" * 4096,
    ),
    ids=(
        "shell",
        "path",
        "url-with-credentials",
        "substitution",
        "backticks",
        "newline-plus-token",
        "windows-traversal",
        "long",
    ),
)
def test_a_hostile_value_is_never_published(value):
    result = evaluate(value)
    assert result.rule_id is None
    assert value not in json.dumps(result.as_dict())


def test_the_verdict_names_no_environment_value_no_token_and_no_path():
    payload = json.dumps(evaluate("S1118").as_dict())
    for token in ("SONAR", "TOKEN", "PROJECT_KEY", "environ", "getenv", "C:\\", "api/"):
        assert token not in payload


# ---------------------------------------------------------------------------
# K. No wildcard, prefix, suffix, substring or case-folding behaviour
# ---------------------------------------------------------------------------

#: Wildcards, regexes and padded values: never a rule, never a tolerance.
PATTERN_ENTRIES = (
    "S*",
    "S11*",
    "*1118",
    "*",
    "**",
    "S1118*",
    "S11?",
    "S111[8]",
    "^S1118$",
    "(?i)S1118",
    ".*",
    "S1118|S1192",
    " S1118",
    "S1118 ",
)


@pytest.mark.parametrize("pattern", PATTERN_ENTRIES)
def test_a_pattern_is_refused_as_a_configuration_and_as_an_input(pattern):
    policy = RuleAllowlistPolicy(allowed_rules=("S1118", pattern))
    assert policy.is_valid is False
    assert show(policy, "S1118").status is S.INVALID_POLICY
    assert show(policy, "S1118").can_auto_fix is False

    result = evaluate(pattern)
    assert result.status is S.INVALID_RULE_ID
    assert result.can_auto_fix is False


@pytest.mark.parametrize(
    "near_miss",
    ("S111", "1118", "S1118x", "xS1118", "s1118", "java:S1118", "S1119", "S11180"),
)
def test_a_well_formed_near_miss_never_matches_another_rule(near_miss):
    assert evaluate("S1118", allowed_rules=(near_miss,)).status is S.RULE_NOT_ALLOWED
    assert evaluate(near_miss, allowed_rules=("S1118",)).status is S.RULE_NOT_ALLOWED
    assert evaluate(near_miss, allowed_rules=(near_miss,)).status is S.ALLOWED


# ---------------------------------------------------------------------------
# L. The module surface (standard library only, no I/O, no matching library)
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "sonar_rule_allowlist.py"
SOURCE = MODULE_PATH.read_text(encoding="utf-8")

#: The only modules T28 may import.
STDLIB_IMPORTS = {"__future__", "dataclasses", "enum", "types", "typing"}

#: The matching/pattern libraries T28 must never import (it must contain no
#: wildcard, glob, regex or fuzzy behaviour at all).
MATCHING_IMPORTS = {"re", "fnmatch", "glob", "difflib", "unicodedata", "string"}

#: The top-level definitions of the module, in declaration order.
TOP_LEVEL_DEFINITIONS = (
    "RuleAllowlistStatus",
    "RuleAllowlistDecision",
    "_character_class",
    "_rule_id_problem",
    "_is_rule_id",
    "_policy_problem",
    "RuleAllowlistPolicy",
    "RuleAllowlistInput",
    "RuleAllowlistEvaluation",
    "_outcome",
    "_input_problem",
    "evaluate_rule_allowlist",
)

#: Calls T28 must never make: I/O, execution, network and process control.
FORBIDDEN_CALLS = {
    "open",
    "eval",
    "exec",
    "compile",
    "input",
    "print",
    "__import__",
    "system",
    "popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "getenv",
    "connect",
    "urlopen",
}

#: String inspection methods that could implement prefix/suffix/substring or
#: case-insensitive matching. T28 must call none of them.
FORBIDDEN_STRING_METHODS = {
    "startswith",
    "endswith",
    "casefold",
    "lower",
    "upper",
    "strip",
    "lstrip",
    "rstrip",
    "removeprefix",
    "removesuffix",
    "find",
    "rfind",
    "index",
    "rindex",
    "count",
    "replace",
    "partition",
    "translate",
    "expandtabs",
    "title",
    "capitalize",
    "swapcase",
    "format_map",
}


def imported_modules(source):
    """The top-level modules ``source`` imports (both import forms)."""
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported


def called_names(source):
    """The bare names of every function or method ``source`` calls."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def called_attributes(source):
    """The attribute names of every method call in ``source``."""
    attributes = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attributes.add(node.func.attr)
    return attributes


def test_the_module_imports_only_the_standard_library():
    assert imported_modules(SOURCE) == STDLIB_IMPORTS


def test_the_module_never_imports_a_matching_library():
    assert imported_modules(SOURCE).isdisjoint(MATCHING_IMPORTS)


def test_the_module_imports_no_repository_module():
    repository_modules = {path.stem for path in REPO_ROOT.glob("*.py")}
    assert imported_modules(SOURCE) & repository_modules == set()


def test_no_repository_module_imports_t28():
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name == MODULE_PATH.name:
            continue
        assert "sonar_rule_allowlist" not in imported_modules(
            path.read_text(encoding="utf-8")
        ), path.name


def test_t28_is_not_wired_into_any_other_module():
    """T28 has no caller: no stage, and not ``main.py``, mentions it."""
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name == MODULE_PATH.name:
            continue
        source = path.read_text(encoding="utf-8")
        assert "sonar_rule_allowlist" not in source, path.name
        assert "RuleAllowlistPolicy" not in source, path.name
        assert "RuleAllowlistInput" not in source, path.name
        assert "evaluate_rule_allowlist" not in source, path.name


def test_the_module_calls_nothing_dangerous():
    assert not (called_names(SOURCE) & FORBIDDEN_CALLS)


def test_the_module_calls_no_string_inspection_method():
    assert not (called_attributes(SOURCE) & FORBIDDEN_STRING_METHODS)


def test_every_membership_test_is_an_exact_membership_of_a_name():
    """No ``x in "literal"`` (substring) test exists anywhere in the module."""
    comparisons = 0
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Compare):
            continue
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, (ast.In, ast.NotIn)):
                comparisons += 1
                assert isinstance(comparator, ast.Name), ast.dump(comparator)
    assert comparisons >= 5


def test_the_module_defines_exactly_the_documented_surface():
    tree = ast.parse(SOURCE)
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in tree.body)
    assert tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ) == TOP_LEVEL_DEFINITIONS


def test_every_exported_name_exists_and_is_sorted():
    assert module.__all__ == (
        "ALLOWED_STATUSES",
        "DEFAULT_ALLOWED_RULES",
        "DIAGNOSTIC_CODES",
        "MAX_ALLOWED_RULES",
        "MAX_RULE_ID_LENGTH",
        "POLICY_VERSION",
        "PRECEDENCE",
        "RuleAllowlistDecision",
        "RuleAllowlistEvaluation",
        "RuleAllowlistInput",
        "RuleAllowlistPolicy",
        "RuleAllowlistStatus",
        "evaluate_rule_allowlist",
    )
    assert list(module.__all__) == sorted(module.__all__)
    for name in module.__all__:
        assert hasattr(module, name), name


def test_the_module_holds_no_mutable_module_state():
    for name in ("ALLOWED_STATUSES", "PRECEDENCE", "DEFAULT_ALLOWED_RULES"):
        assert isinstance(getattr(module, name), tuple), name
    for name in (
        "DIAGNOSTIC_CODES",
        "_STATUS_REASONS",
        "_STATUS_CONSEQUENCES",
        "_CHARACTER_PROBLEMS",
    ):
        assert isinstance(getattr(module, name), types.MappingProxyType), name


def test_the_enum_classes_are_not_collected_by_pytest():
    assert RuleAllowlistStatus.__test__ is False
    assert RuleAllowlistDecision.__test__ is False


def test_the_limits_are_the_documented_constants():
    assert POLICY_VERSION == "t28.1"
    assert MAX_RULE_ID_LENGTH == 64
    assert MAX_ALLOWED_RULES == 1000


# ---------------------------------------------------------------------------
# M. Compatibility with the repository's own rule-key contract (T20)
# ---------------------------------------------------------------------------


def test_every_rule_id_t28_accepts_is_a_rule_key_t20_can_publish():
    """T28's grammar is a subset of the repository's rule-key contract."""
    for rule_id in VALID_RULE_IDS:
        message = build_commit_message(
            rule=rule_id, file_path="src/app.py", issue_key="AX1"
        )
        assert f"Sonar-Rule: {rule_id}" in message.text
