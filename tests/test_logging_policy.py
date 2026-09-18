"""T29 unit tests: the pure, bounded, fail-closed logging policy (no I/O).

This file pins the whole contract of ``logging_policy``:

* the tables - every level and its order, every status, the diagnostic code map,
  ``PRECEDENCE``, ``EMITTABLE_STATUSES``, the limits and the credential-shape
  table;
* the one rule - a record may be emitted only when its event code is allowlisted,
  every field name is allowlisted, the level is at or above the floor, every part
  is bounded printable ASCII (or a permitted scalar) and no verbatim identity is
  credential-shaped; the default configuration allows three event codes and no
  field name at all;
* the record grammar - the accepted event codes, messages, field names and
  values, and every refused input (absent, mistyped, empty, whitespace, control
  characters, non-ASCII, wildcards, regexes, paths, URLs, shell fragments,
  separators, str subclasses, arbitrary objects, over-long and out-of-range
  values);
* the bounds - every limit tested around its boundary, and the guarantee that an
  over-large value is refused rather than truncated;
* redaction - the marker as the whole replacement, no prefix, length, hash or
  partial value, credential shapes in identities refusing the record, merged
  spans, and the redaction-expansion bound;
* fail closed - only ``ACCEPTED`` and ``REDACTED`` report ``can_emit``, a refusal
  publishes no message and no field at all (the record's own validated event code
  is the one caller string it may repeat), and the API cannot express anything
  else;
* the exact ``PRECEDENCE`` with conflict cases, plus determinism, purity,
  immutability and the deterministic, secret-free ``as_dict()`` serialization of
  all three records;
* the module surface: standard library only (no ``logging``, no ``os``, no
  repository module), no I/O, no coercion, no arbitrary serialization and
  "not wired".

The tests never touch Git, the network, a subprocess or a file other than this
module's own source and the repository's own ``*.py`` files, which are read
(never written) for the static audit.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import time
import types
from dataclasses import FrozenInstanceError

import pytest

import logging_policy as module
from logging_policy import (
    DEFAULT_ALLOWED_EVENT_CODES,
    DEFAULT_ALLOWED_FIELD_NAMES,
    DEFAULT_MINIMUM_LEVEL,
    DIAGNOSTIC_CODES,
    EMITTABLE_STATUSES,
    LEVEL_ORDER,
    MAX_ALLOWED_EVENT_CODES,
    MAX_ALLOWED_FIELD_NAMES,
    MAX_EVENT_CODE_LENGTH,
    MAX_FIELDS,
    MAX_FIELD_INTEGER,
    MAX_FIELD_NAME_LENGTH,
    MAX_FIELD_VALUE_LENGTH,
    MAX_MESSAGE_LENGTH,
    POLICY_VERSION,
    PRECEDENCE,
    REDACTION_MARKER,
    SECRET_PATTERNS,
    LogDecision,
    LogEvent,
    LogLevel,
    LogStatus,
    LoggingPolicy,
    evaluate_log_event,
    sanitize_log_fields,
    sanitize_log_value,
)

#: The vocabulary, aliased so the tests read as policy.
S = LogStatus
L = LogLevel
D = LogDecision

#: The event code most tests state.
EVENT = "T29_LOG_ACCEPTED"

#: The configuration most tests use: two codes, two field names, no floor.
POLICY = LoggingPolicy(
    allowed_event_codes=(EVENT, "MY_EVENT"),
    allowed_field_names=("issue_key", "attempt"),
)

#: The default configuration, pinned separately because it is the fail-closed
#: default a caller gets before configuring anything.
DEFAULT_POLICY = LoggingPolicy()

MODULE_PATH = pathlib.Path(module.__file__)
SOURCE = MODULE_PATH.read_text(encoding="utf-8")
REPO_ROOT = MODULE_PATH.parent

#: A message and fields that the configuration above approves.
CLEAN_MESSAGE = "an event happened"
CLEAN_FIELDS = (("issue_key", "ACV2-642"), ("attempt", 3))


def evaluate(**overrides):
    """Evaluate one record built from the approved defaults (public API only)."""
    parts = {
        "event_code": EVENT,
        "level": L.INFO,
        "message": CLEAN_MESSAGE,
        "fields": CLEAN_FIELDS,
    }
    parts.update(overrides)
    return evaluate_log_event(policy=POLICY, event=LogEvent(**parts))


def show(policy, event):
    """Evaluate one already-built record against one already-built policy."""
    return evaluate_log_event(policy=policy, event=event)


def serialized(evaluation) -> str:
    """The whole published verdict as text, for "never echoes" assertions."""
    return json.dumps(evaluation.as_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# A. The tables
# ---------------------------------------------------------------------------


def test_the_policy_version_and_marker_are_the_documented_constants():
    assert POLICY_VERSION == "t29.1"
    assert REDACTION_MARKER == "[REDACTED]"


def test_the_limits_are_the_documented_constants():
    assert MAX_EVENT_CODE_LENGTH == 64
    assert MAX_MESSAGE_LENGTH == 500
    assert MAX_FIELD_NAME_LENGTH == 64
    assert MAX_FIELD_VALUE_LENGTH == 200
    assert MAX_FIELDS == 20
    assert MAX_FIELD_INTEGER == 2 ** 53
    assert MAX_ALLOWED_EVENT_CODES == 100
    assert MAX_ALLOWED_FIELD_NAMES == 100


def test_the_levels_are_exactly_the_five_severities():
    assert [(level.name, level.value) for level in L] == [
        ("DEBUG", "debug"),
        ("INFO", "info"),
        ("WARNING", "warning"),
        ("ERROR", "error"),
        ("CRITICAL", "critical"),
    ]


def test_the_level_order_is_total_and_strictly_increasing():
    assert dict(LEVEL_ORDER) == {
        L.DEBUG: 0,
        L.INFO: 1,
        L.WARNING: 2,
        L.ERROR: 3,
        L.CRITICAL: 4,
    }
    assert set(LEVEL_ORDER) == set(L)
    assert len(set(LEVEL_ORDER.values())) == len(list(L))
    assert DEFAULT_MINIMUM_LEVEL is L.DEBUG


def test_the_statuses_are_exactly_the_eight_outcomes():
    assert [(status.name, status.value) for status in S] == [
        ("ACCEPTED", "accepted"),
        ("REDACTED", "redacted"),
        ("INVALID_POLICY", "invalid-policy"),
        ("INVALID_EVENT", "invalid-event"),
        ("INVALID_FIELD", "invalid-field"),
        ("VALUE_TOO_LARGE", "value-too-large"),
        ("SECRET_DETECTED", "secret-detected"),
        ("REJECTED", "rejected"),
    ]


def test_the_decisions_are_exactly_emit_and_drop():
    assert [(decision.name, decision.value) for decision in D] == [
        ("EMIT", "emit"),
        ("DROP", "drop"),
    ]


def test_only_accepted_and_redacted_can_emit():
    assert EMITTABLE_STATUSES == (S.ACCEPTED, S.REDACTED)
    assert isinstance(EMITTABLE_STATUSES, tuple)


def test_the_precedence_is_exactly_the_documented_order():
    assert PRECEDENCE == (
        S.INVALID_POLICY,
        S.INVALID_EVENT,
        S.INVALID_FIELD,
        S.VALUE_TOO_LARGE,
        S.SECRET_DETECTED,
        S.REJECTED,
        S.REDACTED,
        S.ACCEPTED,
    )
    assert set(PRECEDENCE) == set(S)
    assert len(PRECEDENCE) == len(set(PRECEDENCE))
    assert set(EMITTABLE_STATUSES) < set(PRECEDENCE)


def test_every_status_has_exactly_one_diagnostic_code():
    assert dict(DIAGNOSTIC_CODES) == {
        S.ACCEPTED: "T29_LOG_ACCEPTED",
        S.REDACTED: "T29_LOG_REDACTED",
        S.INVALID_POLICY: "T29_POLICY_INVALID",
        S.INVALID_EVENT: "T29_EVENT_INVALID",
        S.INVALID_FIELD: "T29_FIELD_INVALID",
        S.VALUE_TOO_LARGE: "T29_VALUE_TOO_LARGE",
        S.SECRET_DETECTED: "T29_SECRET_DETECTED",
        S.REJECTED: "T29_LOG_REJECTED",
    }
    assert set(DIAGNOSTIC_CODES) == set(S)
    assert len(set(DIAGNOSTIC_CODES.values())) == len(list(S))


def test_the_default_allowlists_are_the_documented_defaults():
    assert DEFAULT_ALLOWED_EVENT_CODES == (
        "T29_LOG_ACCEPTED",
        "T29_LOG_REDACTED",
        "T29_LOG_REJECTED",
    )
    assert DEFAULT_ALLOWED_FIELD_NAMES == ()
    assert isinstance(DEFAULT_ALLOWED_EVENT_CODES, tuple)
    assert isinstance(DEFAULT_ALLOWED_FIELD_NAMES, tuple)


def test_the_credential_shape_table_is_usable_and_documented():
    assert isinstance(SECRET_PATTERNS, tuple)
    assert len(SECRET_PATTERNS) == 8
    labels = [label for label, _pattern in SECRET_PATTERNS]
    assert labels == [
        "url-userinfo",
        "credential-pair",
        "auth-scheme",
        "json-web-token",
        "prefixed-token",
        "long-hex-run",
        "long-base64-run",
        "private-key-marker",
    ]
    assert len(set(labels)) == len(labels)
    for label, pattern in SECRET_PATTERNS:
        assert label
        assert pattern is not None
        # Every pattern requires at least one character, so no zero-length span
        # can exist and redaction can never loop.
        assert pattern.match("") is None
        assert pattern.search("") is None


def test_every_pattern_quantifier_is_bounded_except_the_documented_whitespace_runs():
    """The table's one deliberate exception is pinned, so it cannot spread.

    Every quantifier is bounded, and the only open ones are the two whitespace
    runs of ``credential-pair``: a keyword separated from its ``=``/``:`` by
    whitespace must still be redacted, and inside a validated text that run is
    bounded by the text's own bound anyway.
    """
    open_quantifiers = []
    for label, pattern in SECRET_PATTERNS:
        assert not re.search(r"\{\d+,\}", pattern.pattern), label
        for match in re.finditer(r"(?<!\\)\*", pattern.pattern):
            start = max(0, match.start() - 2)
            open_quantifiers.append((label, pattern.pattern[start:match.end()]))
    assert open_quantifiers == [("credential-pair", "\\s*"), ("credential-pair", "\\s*")]


def test_the_module_holds_no_mutable_module_state():
    for name in (
        "EMITTABLE_STATUSES",
        "PRECEDENCE",
        "DEFAULT_ALLOWED_EVENT_CODES",
        "DEFAULT_ALLOWED_FIELD_NAMES",
        "SECRET_PATTERNS",
    ):
        assert isinstance(getattr(module, name), tuple), name
    for name in (
        "DIAGNOSTIC_CODES",
        "LEVEL_ORDER",
        "_STATUS_REASONS",
        "_STATUS_CONSEQUENCES",
    ):
        assert isinstance(getattr(module, name), types.MappingProxyType), name
        with pytest.raises(TypeError):
            getattr(module, name)[S.ACCEPTED] = "changed"


def test_the_enum_classes_are_not_collected_by_pytest():
    assert L.__test__ is False
    assert S.__test__ is False
    assert D.__test__ is False


def test_every_exported_name_exists_and_is_sorted():
    assert len(module.__all__) == 27
    assert list(module.__all__) == sorted(module.__all__)
    for name in module.__all__:
        assert hasattr(module, name), name


# ---------------------------------------------------------------------------
# B. Levels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", list(L))
def test_every_level_is_accepted_at_or_above_the_floor(level):
    evaluation = evaluate(level=level)
    assert evaluation.status is S.ACCEPTED
    assert evaluation.level is level
    assert evaluation.can_emit is True


@pytest.mark.parametrize(
    "level",
    ["info", "INFO", "Info", 10, 2.5, True, None, b"info", ("info",), object()],
)
def test_a_value_that_is_not_a_log_level_is_refused(level):
    evaluation = evaluate(level=level)
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.decision is D.DROP
    assert evaluation.can_emit is False
    assert evaluation.level is None


def test_an_absent_level_and_a_mistyped_level_get_different_clauses():
    absent = evaluate(level=None)
    mistyped = evaluate(level="info")
    assert "no level was supplied" in absent.reason
    assert "no level was supplied" not in mistyped.reason
    assert "must be a LogLevel member" in mistyped.reason
    assert "info" not in mistyped.reason


@pytest.mark.parametrize(
    "floor, level, emitted",
    [
        (L.DEBUG, L.DEBUG, True),
        (L.INFO, L.DEBUG, False),
        (L.INFO, L.INFO, True),
        (L.INFO, L.WARNING, True),
        (L.WARNING, L.INFO, False),
        (L.CRITICAL, L.ERROR, False),
        (L.CRITICAL, L.CRITICAL, True),
    ],
)
def test_the_level_floor_only_drops(floor, level, emitted):
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,),
        allowed_field_names=("issue_key", "attempt"),
        minimum_level=floor,
    )
    evaluation = show(
        policy,
        LogEvent(
            event_code=EVENT,
            level=level,
            message=CLEAN_MESSAGE,
            fields=CLEAN_FIELDS,
        ),
    )
    assert evaluation.can_emit is emitted
    if not emitted:
        assert evaluation.status is S.REJECTED
        assert "below the configured floor" in evaluation.reason
        assert evaluation.message is None
        assert evaluation.fields == ()


# ---------------------------------------------------------------------------
# C. Event codes
# ---------------------------------------------------------------------------

#: Event codes the grammar accepts (none of them is credential-shaped).
VALID_EVENT_CODES = (
    "A",
    "A1",
    "A_1",
    "X9_9X",
    EVENT,
    "T29_LOG_REDACTED",
    "T29_LOG_REJECTED",
    "MY_EVENT",
    "ERROR_WHILE_RUNNING",
)

#: The longest event code the policy accepts: exactly the maximum length, with
#: the runs broken up by underscores so it is not credential-shaped.
MAX_LENGTH_CODE = "AB_CD_EF_" * 7 + "A"

#: Event code strings the grammar refuses (all of them land on ``INVALID_EVENT``).
INVALID_EVENT_CODES = (
    "",
    " ",
    " T29_LOG_ACCEPTED",
    "T29_LOG_ACCEPTED ",
    "t29_log_accepted",
    "T29_Log_Accepted",
    "T29-LOG-ACCEPTED",
    "T29.LOG.ACCEPTED",
    "T29:LOG",
    "T29 LOG",
    "_T29_LOG",
    "1T29_LOG",
    "T29_LOG_ACCEPTED!",
    "T29_LOG_ACCEPTED; rm -rf /",
    "$(id)",
    "`id`",
    "T29_LOG_ACCEPTED|id",
    "src/T29_LOG_ACCEPTED",
    "../../T29_LOG_ACCEPTED",
    "C:\\Windows\\T29_LOG_ACCEPTED",
    "https://sonar.example.com/api/rules",
    "S1118*",
    "^T29",
    "(?i)t29",
    "T29_LOG_ACCEPTED\x00",
    "T29_LOG_ACCEPTED\n",
    "T29_LOG_ACCEPTED\t",
    "T29_\x1b[31mLOG",
    "CAF\u00c9",
    "\u0130T29",
)


@pytest.mark.parametrize("code", VALID_EVENT_CODES)
def test_a_usable_event_code_is_judged_as_an_identity(code):
    evaluation = evaluate(event_code=code)
    assert evaluation.event_code == code
    assert evaluation.status in (S.ACCEPTED, S.REJECTED)
    assert evaluation.can_emit is (code in (EVENT, "MY_EVENT"))


@pytest.mark.parametrize("code", INVALID_EVENT_CODES)
def test_a_code_that_is_not_a_usable_identity_is_refused(code):
    evaluation = evaluate(event_code=code)
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.decision is D.DROP
    assert evaluation.event_code is None
    assert evaluation.can_emit is False
    assert evaluation.message is None
    assert evaluation.fields == ()


@pytest.mark.parametrize(
    "text",
    [
        "T29_LOG_ACCEPTED\n",
        "T29-LOG-ACCEPTED",
        "src/T29_LOG_ACCEPTED",
        "C:\\Windows\\T29_LOG_ACCEPTED",
        "https://bob:hunter2@sonar.example.com/api/rules",
        "$(id)",
        "T29_LOG_ACCEPTED; rm -rf /",
    ],
)
def test_a_refused_event_code_is_never_echoed(text):
    evaluation = evaluate(event_code=text)
    assert evaluation.status is S.INVALID_EVENT
    assert text not in evaluation.reason
    assert text not in serialized(evaluation)


def test_an_absent_event_code_is_refused_as_absence():
    evaluation = evaluate(event_code=None)
    assert evaluation.status is S.INVALID_EVENT
    assert "the event code is absent" in evaluation.reason
    assert evaluation.event_code is None


def test_the_maximum_length_event_code_is_usable():
    assert len(MAX_LENGTH_CODE) == MAX_EVENT_CODE_LENGTH
    evaluation = evaluate(event_code=MAX_LENGTH_CODE)
    assert evaluation.status is S.REJECTED
    assert evaluation.event_code == MAX_LENGTH_CODE


def test_an_over_long_event_code_is_refused_and_never_truncated():
    code = "A" * (MAX_EVENT_CODE_LENGTH + 1)
    evaluation = evaluate(event_code=code)
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.event_code is None
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert code not in evaluation.reason
    assert code[:40] not in serialized(evaluation)


def test_case_is_identity_and_not_a_spelling():
    """A case variant is a different identity, so it is refused as malformed."""
    lowercase = evaluate(event_code=EVENT.lower())
    uppercase_field = evaluate(fields=(("ISSUE_KEY", "ACV2-642"),))
    assert lowercase.status is S.INVALID_EVENT
    assert uppercase_field.status is S.INVALID_FIELD
    assert lowercase.can_emit is False
    assert uppercase_field.can_emit is False


@pytest.mark.parametrize("code", [EVENT + "S", EVENT[:-1], "MY_EVEN", "MY_EVENT_"])
def test_a_code_that_is_merely_similar_to_an_allowlisted_code_is_refused(code):
    evaluation = evaluate(event_code=code)
    assert evaluation.status is S.REJECTED
    assert evaluation.can_emit is False
    assert evaluation.event_code == code


def test_a_str_subclass_is_never_read_as_an_identity():
    """A ``str`` subclass can override ``__len__``/``__eq__``, so it is refused."""

    class SneakyString(str):
        def __len__(self):
            raise AssertionError("a str subclass must never be measured")

        def __eq__(self, other):
            raise AssertionError("a str subclass must never be compared")

        def __hash__(self):
            raise AssertionError("a str subclass must never be hashed")

    evaluation = evaluate(event_code=SneakyString(EVENT))
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.event_code is None
    assert evaluation.can_emit is False


# ---------------------------------------------------------------------------
# D. Messages
# ---------------------------------------------------------------------------

#: A message of exactly the maximum length that carries no credential shape.
MAX_LENGTH_MESSAGE = "ab cd " * 83 + "zz"

#: Messages the policy accepts: printable ASCII, no credential shape.
VALID_MESSAGES = (
    "a",
    "A",
    "1",
    "!",
    "=",
    "[]{}()<>",
    "-_.",
    CLEAN_MESSAGE,
    "  spaced out  ",
    "   ",
    "no control character here",
)

#: Messages the policy refuses as unusable text.
INVALID_MESSAGES = (
    "",
    "\n",
    "a\nb",
    "\r",
    "\r\n",
    "\t",
    "a\tb",
    "\x00",
    "\x01",
    "\x1b[31mred",
    "\x7f",
    "caf\u00e9",
    "\u00e9",
    "emoji \U0001f600",
    "\u200b",
    "\u2028",
)


@pytest.mark.parametrize("message", VALID_MESSAGES)
def test_a_printable_message_is_accepted(message):
    evaluation = evaluate(message=message)
    assert evaluation.status is S.ACCEPTED
    assert evaluation.message == message
    assert evaluation.can_emit is True


def test_a_message_of_exactly_the_maximum_length_is_accepted():
    assert len(MAX_LENGTH_MESSAGE) == MAX_MESSAGE_LENGTH
    evaluation = evaluate(message=MAX_LENGTH_MESSAGE)
    assert evaluation.status is S.ACCEPTED
    assert evaluation.message == MAX_LENGTH_MESSAGE


@pytest.mark.parametrize("message", INVALID_MESSAGES)
def test_a_message_that_is_not_printable_ascii_is_refused(message):
    evaluation = evaluate(message=message)
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert evaluation.can_emit is False


def test_an_empty_message_and_a_control_character_get_different_clauses():
    empty = evaluate(message="")
    control = evaluate(message="a\nb")
    assert "the message is empty" in empty.reason
    assert "printable ASCII" in control.reason
    assert "printable ASCII" not in empty.reason


@pytest.mark.parametrize(
    "message", [None, 7, 2.5, b"text", ["text"], {"a": 1}, L.INFO, object()]
)
def test_a_message_that_is_not_a_string_is_refused(message):
    evaluation = evaluate(message=message)
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.message is None
    assert evaluation.can_emit is False


def test_an_absent_message_and_a_mistyped_message_get_different_clauses():
    class Unusual:
        pass

    absent = evaluate(message=None)
    mistyped = evaluate(message=Unusual())
    assert "the message is absent" in absent.reason
    assert "must be a plain string" in mistyped.reason
    assert "Unusual" not in mistyped.reason



def test_an_over_long_message_is_refused_and_never_echoed():
    message = "z" * (MAX_MESSAGE_LENGTH + 1)
    evaluation = evaluate(message=message)
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert "z" * 40 not in serialized(evaluation)


def test_a_refused_record_publishes_no_caller_text():
    message = "DISTINCTIVE-CALLER-TEXT"
    cases = (
        ({"event_code": "NOT_APPROVED"}, S.REJECTED),
        ({"message": "a\nb " + message}, S.INVALID_EVENT),
        ({"message": message * 40}, S.VALUE_TOO_LARGE),
        ({"fields": (("not_approved", message),)}, S.REJECTED),
        ({"fields": ((message, 1),)}, S.INVALID_FIELD),
        ({"event_code": "AKIAIOSFODNN7EXAMPLE"}, S.SECRET_DETECTED),
        ({"event_code": None}, S.INVALID_EVENT),
    )
    for overrides, expected in cases:
        parts = {"message": message}
        parts.update(overrides)
        evaluation = evaluate(**parts)
        assert evaluation.status is expected, overrides
        assert evaluation.can_emit is False
        assert evaluation.decision is D.DROP
        assert evaluation.message is None
        assert evaluation.fields == ()
        assert message not in serialized(evaluation)


# ---------------------------------------------------------------------------
# E. Fields
# ---------------------------------------------------------------------------

#: A field name of exactly the maximum length, whose runs are broken up so it is
#: not credential-shaped.
MAX_LENGTH_NAME = "ab_cd_" * 10 + "abcd"

#: Field values the policy permits, one per type it accepts.
VALID_FIELD_VALUES = (
    None,
    True,
    False,
    0,
    1,
    -1,
    MAX_FIELD_INTEGER,
    -MAX_FIELD_INTEGER,
    0.0,
    0.5,
    -2.25,
    "",
    "ACV2-642",
    "text with spaces",
    "punctuation !@#$%^&*()",
)

#: Field names the grammar accepts.
VALID_FIELD_NAMES = (
    "a",
    "x1",
    "field_0",
    "issue_key",
    "a_1_b",
    "attempt",
)

#: Field names the grammar refuses.
INVALID_FIELD_NAMES = (
    "",
    "A",
    "Issue_Key",
    "ISSUE_KEY",
    "1issue",
    "_issue",
    "issue-key",
    "issue key",
    "issue.key",
    "issue:key",
    "issue/key",
    "src/issue_key",
    "issue_key!",
    "issue_key\x00",
    "issue_key\n",
    "caf\u00e9",
)


@pytest.mark.parametrize("value", VALID_FIELD_VALUES)
def test_every_permitted_scalar_value_is_published_unchanged(value):
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.ACCEPTED
    assert evaluation.fields == (("issue_key", value),)
    assert evaluation.fields[0][1] is value


@pytest.mark.parametrize("name", VALID_FIELD_NAMES)
def test_a_usable_field_name_is_judged_as_an_identity(name):
    policy = LoggingPolicy(allowed_event_codes=(EVENT,), allowed_field_names=(name,))
    event = LogEvent(
        event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE, fields=((name, 1),)
    )
    evaluation = show(policy, event)
    assert evaluation.status is S.ACCEPTED
    assert evaluation.fields == ((name, 1),)


def test_the_maximum_length_field_name_is_usable():
    assert len(MAX_LENGTH_NAME) == MAX_FIELD_NAME_LENGTH
    evaluation = evaluate(fields=((MAX_LENGTH_NAME, 1),))
    assert evaluation.status is S.REJECTED
    assert "not one of the allowlisted field names" in evaluation.reason


def test_an_over_long_field_name_is_refused_and_never_echoed():
    name = "ab_cd_" * 10 + "abcde"
    assert len(name) == MAX_FIELD_NAME_LENGTH + 1
    evaluation = evaluate(fields=((name, 1),))
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.fields == ()
    assert name not in evaluation.reason
    assert name[:40] not in serialized(evaluation)


@pytest.mark.parametrize("name", INVALID_FIELD_NAMES)
def test_a_field_name_that_is_not_usable_is_refused(name):
    evaluation = evaluate(fields=((name, 1),))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()
    assert evaluation.can_emit is False


@pytest.mark.parametrize(
    "name", [None, 7, b"issue_key", L.INFO, ("issue_key",), object()]
)
def test_a_field_name_that_is_not_a_string_is_refused(name):
    evaluation = evaluate(fields=((name, "ACV2-642"),))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()


@pytest.mark.parametrize(
    "pair",
    [
        ["issue_key", "ACV2-642"],
        ("issue_key",),
        ("issue_key", "ACV2-642", "extra"),
        (),
        "issue_key",
        7,
        None,
        {"issue_key": "ACV2-642"},
    ],
)
def test_a_field_entry_that_is_not_a_two_element_tuple_is_refused(pair):
    evaluation = evaluate(fields=(pair,))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()
    assert evaluation.can_emit is False


@pytest.mark.parametrize(
    "fields",
    [
        None,
        [("issue_key", "ACV2-642")],
        {"issue_key": "ACV2-642"},
        {"issue_key"},
        frozenset({("issue_key", "ACV2-642")}),
        "issue_key=ACV2-642",
        b"issue_key",
        L.INFO,
        7,
    ],
)
def test_a_fields_container_that_is_not_a_tuple_is_refused(fields):
    evaluation = evaluate(fields=fields)
    assert evaluation.status is S.INVALID_EVENT
    assert evaluation.fields == ()
    assert evaluation.can_emit is False


def test_absent_fields_and_a_mistyped_container_get_different_clauses():
    absent = evaluate(fields=None)
    mistyped = evaluate(fields=[("issue_key", "ACV2-642")])
    assert "the fields are absent" in absent.reason
    assert "immutable tuple of (name, value) pairs" in mistyped.reason


def test_an_iterator_is_never_consumed_as_a_fields_container():
    generator = (pair for pair in (("issue_key", "ACV2-642"),))
    evaluation = evaluate(fields=generator)
    assert evaluation.status is S.INVALID_EVENT
    # The generator was never read, so it still holds its single pair.
    assert next(generator) == ("issue_key", "ACV2-642")


def test_a_repeated_field_name_is_refused():
    evaluation = evaluate(fields=(("issue_key", "a"), ("issue_key", "b")))
    assert evaluation.status is S.INVALID_FIELD
    assert "field entry 2" in evaluation.reason
    assert "repeats a name an earlier entry" in evaluation.reason
    assert evaluation.fields == ()


def test_twenty_field_entries_are_allowed_and_twenty_one_are_not():
    names = tuple(f"field_{index}" for index in range(MAX_FIELDS + 1))
    policy = LoggingPolicy(allowed_event_codes=(EVENT,), allowed_field_names=names)
    at_the_bound = tuple((name, index) for index, name in enumerate(names[:-1]))
    one_too_many = tuple((name, index) for index, name in enumerate(names))
    allowed = show(
        policy,
        LogEvent(
            event_code=EVENT,
            level=L.INFO,
            message=CLEAN_MESSAGE,
            fields=at_the_bound,
        ),
    )
    refused = show(
        policy,
        LogEvent(
            event_code=EVENT,
            level=L.INFO,
            message=CLEAN_MESSAGE,
            fields=one_too_many,
        ),
    )
    assert allowed.status is S.ACCEPTED
    assert len(allowed.fields) == MAX_FIELDS
    assert refused.status is S.VALUE_TOO_LARGE
    assert refused.fields == ()


def test_an_unapproved_field_name_is_refused_by_index():
    evaluation = evaluate(fields=(("issue_key", "ACV2-642"), ("not_approved", 1)))
    assert evaluation.status is S.REJECTED
    assert "field entry 2" in evaluation.reason
    assert "not one of the allowlisted field names" in evaluation.reason
    assert "not_approved" not in evaluation.reason
    assert evaluation.message is None


# ---------------------------------------------------------------------------
# E.2 Field values that are not permitted scalars
# ---------------------------------------------------------------------------

#: Values the policy refuses for a field, one per class of problem.
FORBIDDEN_FIELD_VALUES = (
    b"text",
    bytearray(b"text"),
    ["text"],
    ("text",),
    {"name": "text"},
    {"text"},
    frozenset({"text"}),
    complex(1, 2),
    L.INFO,
    S.ACCEPTED,
    D.EMIT,
    object(),
    type("Dynamic", (), {})(),
)


@pytest.mark.parametrize("value", FORBIDDEN_FIELD_VALUES)
def test_a_value_that_is_not_a_permitted_scalar_is_refused(value):
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()
    assert evaluation.can_emit is False
    assert "no other object is ever serialized" in evaluation.reason


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_float_is_refused(value):
    evaluation = evaluate(fields=(("attempt", value),))
    assert evaluation.status is S.INVALID_FIELD
    assert "must be finite" in evaluation.reason
    assert evaluation.fields == ()


@pytest.mark.parametrize(
    "value",
    [
        "line\nbreak",
        "tab\there",
        "\x00",
        "\x1b[31m",
        "\x7f",
        "caf\u00e9",
        "\u200b",
        "emoji \U0001f600",
    ],
)
def test_a_text_value_that_is_not_printable_ascii_is_refused(value):
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()
    assert value not in serialized(evaluation)


def test_an_int_subclass_and_a_float_subclass_are_refused():
    class SneakyInt(int):
        def __index__(self):
            raise AssertionError("an int subclass must never be read")

    class SneakyFloat(float):
        def __float__(self):
            raise AssertionError("a float subclass must never be read")

    for value in (SneakyInt(3), SneakyFloat(0.5)):
        evaluation = evaluate(fields=(("issue_key", value),))
        assert evaluation.status is S.INVALID_FIELD, value
        assert evaluation.fields == ()


def test_bools_are_bools_and_not_integers():
    """A bool is published as itself, and is never measured as an integer."""
    evaluation = evaluate(fields=(("issue_key", True), ("attempt", False)))
    assert evaluation.status is S.ACCEPTED
    assert evaluation.fields == (("issue_key", True), ("attempt", False))
    assert evaluation.fields[0][1] is True


def test_the_integer_bound_is_inclusive():
    at_the_bound = evaluate(fields=(("attempt", MAX_FIELD_INTEGER),))
    over_the_bound = evaluate(fields=(("attempt", MAX_FIELD_INTEGER + 1),))
    under_the_bound = evaluate(fields=(("attempt", -MAX_FIELD_INTEGER - 1),))
    assert at_the_bound.status is S.ACCEPTED
    assert over_the_bound.status is S.VALUE_TOO_LARGE
    assert under_the_bound.status is S.VALUE_TOO_LARGE
    assert over_the_bound.fields == ()
    assert str(MAX_FIELD_INTEGER + 1) not in serialized(over_the_bound)
    assert str(-MAX_FIELD_INTEGER - 1) not in serialized(under_the_bound)


def test_a_text_value_of_exactly_the_maximum_length_is_accepted():
    value = "ab cd " * 33 + "zz"
    assert len(value) == MAX_FIELD_VALUE_LENGTH
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.ACCEPTED
    assert evaluation.fields == (("issue_key", value),)


def test_a_text_value_one_character_too_long_is_refused():
    value = "ab cd " * 33 + "zzz"
    assert len(value) == MAX_FIELD_VALUE_LENGTH + 1
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.fields == ()
    assert value[:40] not in serialized(evaluation)


# ---------------------------------------------------------------------------
# F. Credential shapes and redaction
# ---------------------------------------------------------------------------

#: One ``(label, text, fragment)`` per credential shape.  The text is chosen so
#: the expected label is the *first* shape in ``SECRET_PATTERNS`` order that
#: matches, and the fragment is the credential material itself, which must never
#: survive redaction in any form.
SECRET_SAMPLES = (
    ("url-userinfo", "https://bob:hunter2@sonar.example.com/api/rules", "hunter2"),
    ("credential-pair", "calling with pwd=hunter2 now", "hunter2"),
    (
        "auth-scheme",
        "sent Bearer eyJhbGciOiJIUzI1NiJ9 payload",
        "eyJhbGciOiJIUzI1NiJ9",
    ),
    ("json-web-token", "token ABCDEFGH.IJKLMNOP.QRSTUVWX sent", "IJKLMNOP"),
    (
        "prefixed-token",
        "the value ghp_abcdefghijklmnop here",
        "ghp_abcdefghijklmnop",
    ),
    (
        "long-hex-run",
        "hash 0123456789abcdef0123456789abcdef end",
        "0123456789abcdef0123456789abcdef",
    ),
    (
        "long-base64-run",
        "blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWYxMjM0NTY3OA== end",
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWYxMjM0NTY3OA==",
    ),
    (
        "private-key-marker",
        "found -----BEGIN RSA PRIVATE KEY----- here",
        "-----BEGIN RSA PRIVATE KEY-----",
    ),
)

#: Credential-shaped *event codes*: each satisfies the upper-case code grammar and
#: is still credential-shaped, so the identity check is what refuses the record.
CREDENTIAL_CODES = (
    ("prefixed-token", "AKIAIOSFODNN7EXAMPLE"),
    ("long-hex-run", "ABCDEF0123456789ABCDEF0123456789"),
    ("long-base64-run", "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOP"),
)

#: Credential-shaped *field names*: each satisfies the lower-case name grammar.
CREDENTIAL_NAMES = (
    ("long-hex-run", "abcdef0123456789abcdef0123456789"),
    ("long-base64-run", "abcdefghijklmnopqrstuvwxyzabcdefghijklmnop"),
)

#: Text that resembles nothing in the table and must stay verbatim.
CLEAN_TEXTS = (
    "the run finished",
    "issue ACV2-642 was fixed",
    "S1118",
    "python:S1481",
    "src/module.py",
    "rule_id=python:S1481",
    "attempt 3 of 5",
    "0123456789abcdef0123456789abcde",  # 31 hex characters: below the threshold
    "abcdefghijklmnopqrstuvwxyzabcdefghijklm",  # 39 characters: below it too
)


@pytest.mark.parametrize("label, text, fragment", SECRET_SAMPLES)
def test_every_credential_shape_has_a_category(label, text, fragment):
    assert fragment in text
    assert module._secret_category(text) == label


@pytest.mark.parametrize("label, text, fragment", SECRET_SAMPLES)
def test_credential_text_in_a_message_is_redacted(label, text, fragment):
    evaluation = evaluate(message="before " + text + " after")
    assert evaluation.status is S.REDACTED
    assert evaluation.was_redacted is True
    assert evaluation.can_emit is True
    assert evaluation.message is not None
    assert evaluation.message.startswith("before ")
    assert evaluation.message.endswith(" after")
    assert evaluation.message.count(REDACTION_MARKER) == 1
    assert fragment not in evaluation.message
    assert fragment not in serialized(evaluation)


@pytest.mark.parametrize("label, text, fragment", SECRET_SAMPLES)
def test_credential_text_in_a_field_value_is_redacted(label, text, fragment):
    evaluation = evaluate(fields=(("issue_key", text),))
    assert evaluation.status is S.REDACTED
    assert evaluation.fields[0][0] == "issue_key"
    assert REDACTION_MARKER in evaluation.fields[0][1]
    assert fragment not in evaluation.fields[0][1]
    assert fragment not in serialized(evaluation)


@pytest.mark.parametrize("label, text, fragment", SECRET_SAMPLES)
def test_credential_text_never_reaches_a_diagnostic(label, text, fragment):
    for evaluation in (
        evaluate(message=text),
        evaluate(fields=(("issue_key", text),)),
        evaluate(message=text, fields=(("issue_key", text),)),
    ):
        assert fragment not in evaluation.reason
        assert fragment not in " ".join(evaluation.reasons)
        assert fragment not in serialized(evaluation)


@pytest.mark.parametrize("text", CLEAN_TEXTS)
def test_text_without_a_credential_shape_is_not_redacted(text):
    evaluation = evaluate(message=text, fields=(("issue_key", text),))
    assert evaluation.status is S.ACCEPTED
    assert evaluation.was_redacted is False
    assert evaluation.message == text
    assert evaluation.fields == (("issue_key", text),)
    assert REDACTION_MARKER not in serialized(evaluation)


def test_the_redaction_thresholds_are_the_documented_ones():
    """Over-redaction is bounded, and the thresholds are pinned."""
    below_hex = evaluate(message="0123" + "456789abcdef0123456789abcde")
    at_hex = evaluate(message="0123" + "456789abcdef0123456789abcdef")
    # ``G`` is not a hexadecimal character, so it isolates the base64 threshold.
    below_base64 = evaluate(message="G" * 39)
    at_base64 = evaluate(message="G" * 40)
    assert below_hex.status is S.ACCEPTED
    assert at_hex.status is S.REDACTED
    assert below_base64.status is S.ACCEPTED
    assert at_base64.status is S.REDACTED


def test_a_short_run_inside_a_longer_word_is_not_redacted():
    """A run that is not delimited is an identifier, not a blob."""
    text = "0123456789abcdef0123456789abcdeZ"
    assert module._secret_category(text) is None
    assert evaluate(message=text).status is S.ACCEPTED


def test_a_keyword_separated_by_whitespace_is_still_redacted():
    """The open whitespace runs keep a spaced credential pair redacted.

    ``credential-pair`` leaves its two whitespace quantifiers open on purpose, so
    a keyword separated from its ``=``/``:`` by a long run of spaces still redacts
    the whole pair instead of publishing the value.
    """
    spaced = "password" + " " * 100 + "=hunter2"
    assert len(spaced) <= MAX_MESSAGE_LENGTH
    widest = "token" + " " * (MAX_MESSAGE_LENGTH - len("token") - len("=x")) + "=x"
    assert len(widest) == MAX_MESSAGE_LENGTH
    for text in ("password = hunter2", spaced, widest):
        evaluation = evaluate(message=text)
        assert evaluation.status is S.REDACTED
        assert evaluation.can_emit is True
        assert evaluation.message == REDACTION_MARKER
        assert "hunter2" not in serialized(evaluation)


@pytest.mark.parametrize("label, code", CREDENTIAL_CODES)
def test_a_credential_shaped_event_code_refuses_the_record(label, code):
    evaluation = evaluate(event_code=code)
    assert evaluation.status is S.SECRET_DETECTED
    assert evaluation.can_emit is False
    assert evaluation.decision is D.DROP
    assert evaluation.event_code is None
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert label in evaluation.reason
    assert code not in serialized(evaluation)


@pytest.mark.parametrize("label, name", CREDENTIAL_NAMES)
def test_a_credential_shaped_field_name_refuses_the_record(label, name):
    evaluation = evaluate(fields=((name, 1),))
    assert evaluation.status is S.SECRET_DETECTED
    assert evaluation.can_emit is False
    assert evaluation.fields == ()
    assert evaluation.message is None
    assert label in evaluation.reason
    assert name not in serialized(evaluation)


def test_the_reportable_category_is_the_first_match_in_table_order():
    assert (
        module._secret_category("https://bob:hunter2@sonar.example.com?token=abcdef123456")
        == "url-userinfo"
    )
    assert (
        module._secret_category("authorization=Bearer abcdef123456")
        == "credential-pair"
    )


def test_a_value_that_is_only_a_credential_becomes_the_marker_alone():
    assert evaluate(message="ghp_abcdefghijklmnop").message == REDACTION_MARKER
    field = evaluate(fields=(("issue_key", "abcdef0123456789abcdef0123456789"),))
    assert field.fields == (("issue_key", REDACTION_MARKER),)


def test_two_separated_credentials_give_two_markers():
    evaluation = evaluate(message="pwd=hunter2 and then ghp_abcdefghijklmnop")
    assert evaluation.status is S.REDACTED
    assert evaluation.message is not None
    assert evaluation.message.count(REDACTION_MARKER) == 2
    assert " and then " in evaluation.message


def test_overlapping_credentials_collapse_into_one_marker():
    text = "authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
    evaluation = evaluate(message=text)
    assert evaluation.status is S.REDACTED
    assert evaluation.message == REDACTION_MARKER


def test_redaction_within_the_bound_is_emitted():
    evaluation = evaluate(message="token=1 " * 10)
    assert evaluation.status is S.REDACTED
    assert evaluation.message is not None
    assert len(evaluation.message) <= MAX_MESSAGE_LENGTH


def test_redaction_that_expands_a_message_beyond_its_bound_is_refused():
    message = "token=1 " * 60
    assert len(message) <= MAX_MESSAGE_LENGTH
    evaluation = evaluate(message=message)
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert "redaction expanded the message" in evaluation.reason


def test_redaction_that_expands_a_field_value_beyond_its_bound_is_refused():
    value = "pwd=1 " * 33
    assert len(value) <= MAX_FIELD_VALUE_LENGTH
    evaluation = evaluate(fields=(("issue_key", value),))
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.fields == ()
    assert "redaction expanded the text value of field entry 1" in evaluation.reason


def test_sanitize_log_value_reports_what_it_redacted():
    assert sanitize_log_value("plain text") == ("plain text", False)
    assert sanitize_log_value("pwd=hunter2") == (REDACTION_MARKER, True)
    assert sanitize_log_value("") == ("", False)


@pytest.mark.parametrize("text", [None, 7, b"text", ["text"], L.INFO, object()])
def test_sanitize_log_value_refuses_a_non_string(text):
    with pytest.raises(TypeError) as error:
        sanitize_log_value(text)
    assert "plain string" in str(error.value)


def test_sanitize_log_value_enforces_no_bound_of_its_own():
    """The helper redacts; bounding is the policy's job."""
    text = "x" * (MAX_MESSAGE_LENGTH + 1)
    assert sanitize_log_value(text) == (REDACTION_MARKER, True)


def test_sanitize_log_fields_redacts_text_and_preserves_scalars():
    fields = (
        ("issue_key", "ACV2-642"),
        ("attempt", 3),
        ("ok", True),
        ("note", None),
        ("ratio", 0.5),
        ("secret", "pwd=hunter2"),
    )
    sanitized = sanitize_log_fields(fields)
    assert sanitized == (
        ("issue_key", "ACV2-642"),
        ("attempt", 3),
        ("ok", True),
        ("note", None),
        ("ratio", 0.5),
        ("secret", REDACTION_MARKER),
    )
    assert sanitized is not fields
    assert sanitize_log_fields(()) == ()


@pytest.mark.parametrize(
    "fields",
    [
        None,
        [("issue_key", "ACV2-642")],
        (["issue_key", "ACV2-642"],),
        (("issue_key",),),
        (("issue_key", "x", "y"),),
        ((7, "x"),),
        ((None, "x"),),
    ],
)
def test_sanitize_log_fields_refuses_a_malformed_entry(fields):
    with pytest.raises(TypeError) as error:
        sanitize_log_fields(fields)
    message = str(error.value)
    assert "field entry" in message or "immutable tuple" in message


# ---------------------------------------------------------------------------
# G. Approval (the operator's vocabulary)
# ---------------------------------------------------------------------------


def test_the_default_policy_approves_only_its_own_three_event_codes():
    for code in DEFAULT_ALLOWED_EVENT_CODES:
        evaluation = show(
            DEFAULT_POLICY,
            LogEvent(event_code=code, level=L.INFO, message=CLEAN_MESSAGE),
        )
        assert evaluation.status is S.ACCEPTED
        assert evaluation.can_emit is True
    other = show(
        DEFAULT_POLICY,
        LogEvent(
            event_code="SOME_OTHER_EVENT", level=L.INFO, message=CLEAN_MESSAGE
        ),
    )
    assert other.status is S.REJECTED
    assert other.can_emit is False


def test_the_default_policy_publishes_no_caller_field():
    for name in ("issue_key", "attempt", "any_field"):
        evaluation = show(
            DEFAULT_POLICY,
            LogEvent(
                event_code=EVENT,
                level=L.INFO,
                message=CLEAN_MESSAGE,
                fields=((name, 1),),
            ),
        )
        assert evaluation.status is S.REJECTED, name
        assert evaluation.fields == ()
    assert DEFAULT_POLICY.is_valid is True
    assert DEFAULT_POLICY.allowed_field_name_keys == ()


def test_an_empty_allowlist_approves_nothing():
    for policy in (
        LoggingPolicy(allowed_event_codes=()),
        LoggingPolicy(allowed_event_codes=(), allowed_field_names=()),
    ):
        evaluation = show(
            policy,
            LogEvent(event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE),
        )
        assert evaluation.status is S.REJECTED
        assert evaluation.can_emit is False
        assert evaluation.event_code == EVENT


def test_an_unapproved_event_code_is_refused():
    evaluation = evaluate(event_code="NOT_APPROVED")
    assert evaluation.status is S.REJECTED
    assert "not one of the allowlisted event codes" in evaluation.reason
    assert evaluation.message is None
    assert evaluation.fields == ()


def test_the_floor_is_consulted_before_the_code_and_the_fields():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,),
        allowed_field_names=("issue_key",),
        minimum_level=L.ERROR,
    )
    evaluation = show(
        policy,
        LogEvent(
            event_code="NOT_APPROVED",
            level=L.DEBUG,
            message=CLEAN_MESSAGE,
            fields=(("not_approved", 1),),
        ),
    )
    assert evaluation.status is S.REJECTED
    assert "below the configured floor" in evaluation.reason
    assert "allowlisted event codes" not in evaluation.reason
    assert "allowlisted field names" not in evaluation.reason


def test_the_event_code_is_consulted_before_the_field_names():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,), allowed_field_names=("issue_key",)
    )
    evaluation = show(
        policy,
        LogEvent(
            event_code="NOT_APPROVED",
            level=L.INFO,
            message=CLEAN_MESSAGE,
            fields=(("not_approved", 1),),
        ),
    )
    assert evaluation.status is S.REJECTED
    assert "not one of the allowlisted event codes" in evaluation.reason
    assert "allowlisted field names" not in evaluation.reason


def test_a_wider_vocabulary_still_refuses_what_it_does_not_list():
    policy = LoggingPolicy(
        allowed_event_codes=("A_EVENT", "B_EVENT"),
        allowed_field_names=("a_field",),
        minimum_level=L.WARNING,
    )
    approved = show(
        policy,
        LogEvent(
            event_code="B_EVENT",
            level=L.WARNING,
            message=CLEAN_MESSAGE,
            fields=(("a_field", "x"),),
        ),
    )
    refused = show(
        policy,
        LogEvent(
            event_code="C_EVENT",
            level=L.WARNING,
            message=CLEAN_MESSAGE,
            fields=(("a_field", "x"),),
        ),
    )
    assert approved.status is S.ACCEPTED
    assert refused.status is S.REJECTED


# ---------------------------------------------------------------------------
# H. Precedence
# ---------------------------------------------------------------------------


def test_the_record_is_read_before_its_fields():
    evaluation = evaluate(
        event_code="bad code!", message="a\nb", fields=(("Bad", 1),)
    )
    assert evaluation.status is S.INVALID_EVENT
    assert "the event code" in evaluation.reason


def test_the_event_code_content_is_judged_before_its_size():
    """Within one part, the class order of ``PRECEDENCE`` decides."""
    evaluation = evaluate(event_code="A" * 40 + "!")
    assert evaluation.status is S.INVALID_EVENT


def test_the_record_is_understood_before_any_size_is_measured():
    """Sizes are measured only after the whole record is usable."""
    evaluation = evaluate(event_code="A" * 65, message="a\nb")
    assert evaluation.status is S.INVALID_EVENT
    assert "the message" in evaluation.reason


def test_the_message_content_is_judged_before_the_fields():
    evaluation = evaluate(message="a\nb", fields=(("Bad", 1),))
    assert evaluation.status is S.INVALID_EVENT
    assert "the message" in evaluation.reason


def test_a_field_entry_is_judged_before_any_size_is_measured():
    evaluation = evaluate(message="z" * 501, fields=(("Bad", 1),))
    assert evaluation.status is S.INVALID_FIELD


def test_a_field_size_is_judged_before_a_credential_shape():
    name = "abcdef0123456789" * 4 + "a"
    assert len(name) == MAX_FIELD_NAME_LENGTH + 1
    assert module._secret_category(name) is not None
    evaluation = evaluate(fields=((name, 1),))
    assert evaluation.status is S.VALUE_TOO_LARGE


def test_a_credential_shaped_identity_is_judged_before_the_allowlist():
    evaluation = evaluate(event_code="AKIAIOSFODNN7EXAMPLE")
    assert evaluation.status is S.SECRET_DETECTED
    assert "not one of the allowlisted event codes" not in evaluation.reason


def test_approval_is_judged_before_redaction():
    """An unapproved record with credential text is refused, not redacted."""
    evaluation = evaluate(event_code="NOT_APPROVED", message="pwd=hunter2")
    assert evaluation.status is S.REJECTED
    assert evaluation.was_redacted is False
    assert evaluation.message is None


def test_an_approved_record_with_credential_text_is_redacted_and_not_accepted():
    evaluation = evaluate(message="pwd=hunter2")
    assert evaluation.status is S.REDACTED
    assert evaluation.status is not S.ACCEPTED
    assert evaluation.was_redacted is True


def test_every_status_is_reachable_and_consistent():
    cases = {
        S.ACCEPTED: evaluate(),
        S.REDACTED: evaluate(message="pwd=hunter2"),
        S.REJECTED: evaluate(event_code="NOT_APPROVED"),
        S.INVALID_EVENT: evaluate(event_code=None),
        S.INVALID_FIELD: evaluate(fields=(("Bad", 1),)),
        S.VALUE_TOO_LARGE: evaluate(event_code="A" * 65),
        S.SECRET_DETECTED: evaluate(event_code="AKIAIOSFODNN7EXAMPLE"),
        S.INVALID_POLICY: show(
            LoggingPolicy(allowed_event_codes=("bad code!",)),
            LogEvent(event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE),
        ),
    }
    assert set(cases) == set(S)
    for status, evaluation in cases.items():
        assert evaluation.status is status, status
        expected_decision = D.EMIT if status in EMITTABLE_STATUSES else D.DROP
        assert evaluation.decision is expected_decision, status
        assert evaluation.can_emit is (status in EMITTABLE_STATUSES), status
        assert evaluation.diagnostic_code == DIAGNOSTIC_CODES[status]
        assert evaluation.policy_version == POLICY_VERSION
        assert len(evaluation.reasons) == 2
        assert evaluation.reasons[0] == evaluation.reason
        assert evaluation.reasons[1] == module._STATUS_CONSEQUENCES[status]


# ---------------------------------------------------------------------------
# I. The configuration record
# ---------------------------------------------------------------------------


def test_a_configuration_with_no_arguments_is_valid_and_strict():
    policy = LoggingPolicy()
    assert policy.is_valid is True
    assert policy.refusal_reason is None
    assert policy.allowed_event_code_keys == DEFAULT_ALLOWED_EVENT_CODES
    assert policy.allowed_field_name_keys == ()
    assert policy.minimum_level is L.DEBUG


def test_a_documented_vocabulary_is_valid():
    policy = LoggingPolicy(
        allowed_event_codes=("A_EVENT", "B_EVENT"),
        allowed_field_names=("a_field", "b_field"),
        minimum_level=L.WARNING,
    )
    assert policy.is_valid is True
    assert policy.allowed_event_code_keys == ("A_EVENT", "B_EVENT")
    assert policy.allowed_field_name_keys == ("a_field", "b_field")
    assert policy.minimum_level is L.WARNING


@pytest.mark.parametrize(
    "codes",
    [
        None,
        "A_EVENT",
        ["A_EVENT"],
        {"A_EVENT"},
        frozenset({"A_EVENT"}),
        {"code": "A_EVENT"},
        b"A_EVENT",
    ],
)
def test_a_configuration_whose_event_codes_are_not_a_tuple_is_refused(codes):
    policy = LoggingPolicy(allowed_event_codes=codes)
    assert policy.is_valid is False
    assert "allowed_event_codes must be an immutable tuple" in policy.refusal_reason
    assert policy.allowed_event_code_keys == ()
    assert policy.as_dict()["allowed_event_codes"] is None


@pytest.mark.parametrize(
    "names", [None, "a_field", ["a_field"], {"a_field"}, {"a": 1}, b"a_field"]
)
def test_a_configuration_whose_field_names_are_not_a_tuple_is_refused(names):
    policy = LoggingPolicy(allowed_field_names=names)
    assert policy.is_valid is False
    assert "allowed_field_names must be an immutable tuple" in policy.refusal_reason
    assert policy.allowed_field_name_keys == ()


@pytest.mark.parametrize("level", ["info", 10, None, b"info", 2.5, True])
def test_a_configuration_whose_level_is_not_a_member_is_refused(level):
    policy = LoggingPolicy(minimum_level=level)
    assert policy.is_valid is False
    assert "minimum_level must be a LogLevel member" in policy.refusal_reason


def test_a_configuration_iterator_is_never_consumed():
    generator = (code for code in ("A_EVENT",))
    policy = LoggingPolicy(allowed_event_codes=generator)
    assert policy.is_valid is False
    assert next(generator) == "A_EVENT"


@pytest.mark.parametrize(
    "code",
    ["", "bad code!", "a_event", "T29-LOG", "A" * 65, 7, None, b"A", ("A",)],
)
def test_a_configuration_entry_that_is_not_a_usable_event_code_is_refused(code):
    policy = LoggingPolicy(allowed_event_codes=(code,))
    assert policy.is_valid is False
    assert "allowed event code entry 1 is not a usable event code" in (
        policy.refusal_reason
    )
    assert policy.allowed_event_code_keys == ()


def test_a_configuration_entry_that_is_credential_shaped_is_refused():
    code = "AKIAIOSFODNN7EXAMPLE"
    policy = LoggingPolicy(allowed_event_codes=(code,))
    assert policy.is_valid is False
    assert "allowed event code entry 1 is credential-shaped" in policy.refusal_reason
    assert code not in policy.refusal_reason
    assert code not in json.dumps(policy.as_dict(), sort_keys=True)


def test_a_configuration_event_code_that_repeats_is_refused():
    policy = LoggingPolicy(allowed_event_codes=("A_EVENT", "A_EVENT"))
    assert policy.is_valid is False
    assert "allowed_event_codes lists entry 2 twice" in policy.refusal_reason


@pytest.mark.parametrize(
    "name", ["", "Bad Name", "A_FIELD", "a-field", "a" * 65, 7, None, ("a",)]
)
def test_a_configuration_entry_that_is_not_a_usable_field_name_is_refused(name):
    policy = LoggingPolicy(allowed_field_names=(name,))
    assert policy.is_valid is False
    assert "allowed field name entry 1 is not a usable field name" in (
        policy.refusal_reason
    )
    assert policy.allowed_field_name_keys == ()


def test_a_configuration_field_entry_that_is_credential_shaped_is_refused():
    name = "abcdef0123456789abcdef0123456789"
    policy = LoggingPolicy(allowed_field_names=(name,))
    assert policy.is_valid is False
    assert "allowed field name entry 1 is credential-shaped" in policy.refusal_reason
    assert name not in json.dumps(policy.as_dict(), sort_keys=True)


def test_a_configuration_field_name_that_repeats_is_refused():
    policy = LoggingPolicy(allowed_field_names=("a_field", "a_field"))
    assert policy.is_valid is False
    assert "allowed_field_names lists entry 2 twice" in policy.refusal_reason


def test_the_configuration_bounds_are_enforced_before_the_entries():
    too_many_codes = LoggingPolicy(
        allowed_event_codes=("A_EVENT",) * (MAX_ALLOWED_EVENT_CODES + 1)
    )
    at_the_bound = tuple(f"EVENT_{index}" for index in range(MAX_ALLOWED_EVENT_CODES))
    too_many_names = LoggingPolicy(
        allowed_field_names=tuple(
            f"field_{index}" for index in range(MAX_ALLOWED_FIELD_NAMES + 1)
        )
    )
    assert too_many_codes.is_valid is False
    assert "allowed_event_codes holds more than 100 entries" in (
        too_many_codes.refusal_reason
    )
    assert "not a usable event code" not in too_many_codes.refusal_reason
    assert LoggingPolicy(allowed_event_codes=at_the_bound).is_valid is True
    assert too_many_names.is_valid is False
    assert "allowed_field_names holds more than 100 entries" in (
        too_many_names.refusal_reason
    )


def test_an_invalid_configuration_refuses_every_record():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,), allowed_field_names=("bad name",)
    )
    evaluation = show(
        policy, LogEvent(event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE)
    )
    assert evaluation.status is S.INVALID_POLICY
    assert evaluation.can_emit is False
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert evaluation.policy.is_valid is False
    assert "bad name" not in serialized(evaluation)


def test_the_published_configuration_is_a_copy():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,), allowed_field_names=("issue_key",)
    )
    published = policy.as_dict()
    published["allowed_event_codes"].append("INJECTED_EVENT")
    published["allowed_field_names"].append("injected")
    assert policy.allowed_event_code_keys == (EVENT,)
    assert policy.allowed_field_name_keys == ("issue_key",)
    assert policy.is_valid is True


def test_the_published_configuration_is_complete():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,),
        allowed_field_names=("issue_key",),
        minimum_level=L.ERROR,
    )
    assert policy.as_dict() == {
        "policy_version": POLICY_VERSION,
        "allowed_event_codes": [EVENT],
        "allowed_event_code_count": 1,
        "allowed_field_names": ["issue_key"],
        "allowed_field_name_count": 1,
        "minimum_level": "error",
        "maximum_supported_event_codes": MAX_ALLOWED_EVENT_CODES,
        "maximum_supported_field_names": MAX_ALLOWED_FIELD_NAMES,
        "is_valid": True,
        "refusal_reason": None,
    }


def test_the_published_configuration_of_an_invalid_policy_names_no_entry():
    policy = LoggingPolicy(allowed_event_codes=("bad code!",))
    published = policy.as_dict()
    assert published["allowed_event_codes"] is None
    assert published["allowed_event_code_count"] is None
    assert published["allowed_field_names"] is None
    assert published["allowed_field_name_count"] is None
    assert published["minimum_level"] is None
    assert published["is_valid"] is False
    assert "bad code!" not in published["refusal_reason"]


def test_the_configuration_is_frozen():
    policy = LoggingPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.minimum_level = L.ERROR
    with pytest.raises(FrozenInstanceError):
        policy.allowed_event_codes = ("A_EVENT",)
    with pytest.raises(FrozenInstanceError):
        del policy.allowed_field_names


def test_a_run_that_fills_the_whole_publishable_text_is_matched_in_full():
    """The pattern caps are above the policy's own text bounds."""
    evaluation = evaluate(message="A" * MAX_MESSAGE_LENGTH)
    assert evaluation.status is S.REDACTED
    assert evaluation.message == REDACTION_MARKER
    fielded = evaluate(fields=(("issue_key", "A" * MAX_FIELD_VALUE_LENGTH),))
    assert fielded.status is S.REDACTED
    assert fielded.fields == (("issue_key", REDACTION_MARKER),)


# ---------------------------------------------------------------------------
# J. Determinism, purity and immutability
# ---------------------------------------------------------------------------


def test_the_same_pair_always_produces_an_equal_verdict():
    first = evaluate()
    second = evaluate()
    third = evaluate()
    assert first == second == third
    assert first.as_dict() == second.as_dict()
    assert serialized(first) == serialized(second)


def test_evaluating_does_not_mutate_the_caller_records():
    fields = (("issue_key", "ACV2-642"), ("attempt", 3))
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,),
        allowed_field_names=("issue_key", "attempt"),
    )
    event = LogEvent(
        event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE, fields=fields
    )
    show(policy, event)
    assert event.event_code == EVENT
    assert event.level is L.INFO
    assert event.message == CLEAN_MESSAGE
    assert event.fields is fields
    assert policy.allowed_event_codes == (EVENT,)
    assert policy.allowed_field_names == ("issue_key", "attempt")


def test_evaluating_a_mutable_fields_list_leaves_it_untouched():
    fields = [("issue_key", "ACV2-642")]
    evaluate(fields=fields)
    assert fields == [("issue_key", "ACV2-642")]


def test_the_module_keeps_no_state_between_evaluations():
    before = sorted(vars(module))
    for _ in range(3):
        evaluate()
        evaluate(message="pwd=hunter2")
        evaluate(event_code="NOT_APPROVED")
    assert sorted(vars(module)) == before


def test_the_verdict_is_frozen():
    evaluation = evaluate()
    with pytest.raises(FrozenInstanceError):
        evaluation.status = S.REJECTED
    with pytest.raises(FrozenInstanceError):
        evaluation.can_emit = True
    with pytest.raises(FrozenInstanceError):
        del evaluation.message


def test_the_event_record_is_frozen():
    event = LogEvent(event_code=EVENT)
    with pytest.raises(FrozenInstanceError):
        event.message = "changed"
    with pytest.raises(FrozenInstanceError):
        del event.fields


def test_a_mutated_published_copy_does_not_change_the_verdict():
    evaluation = evaluate()
    published = evaluation.as_dict()
    published["status"] = "rejected"
    published["message"] = "changed"
    published["fields"].append(["injected", 1])
    published["can_emit"] = False
    assert evaluation.status is S.ACCEPTED
    assert evaluation.can_emit is True
    assert evaluation.message == CLEAN_MESSAGE
    assert evaluation.fields == CLEAN_FIELDS


def test_the_verdict_carries_the_policy_it_used():
    policy = LoggingPolicy(
        allowed_event_codes=(EVENT,),
        allowed_field_names=("issue_key", "attempt"),
        minimum_level=L.INFO,
    )
    evaluation = show(
        policy,
        LogEvent(
            event_code=EVENT,
            level=L.INFO,
            message=CLEAN_MESSAGE,
            fields=CLEAN_FIELDS,
        ),
    )
    assert evaluation.policy is policy
    assert evaluation.policy.as_dict() == policy.as_dict()


# ---------------------------------------------------------------------------
# K. Serialization
# ---------------------------------------------------------------------------


def test_the_published_verdict_round_trips_through_json():
    for evaluation in (
        evaluate(),
        evaluate(message="pwd=hunter2"),
        evaluate(fields=(("issue_key", None), ("attempt", 2 ** 53))),
        evaluate(event_code="NOT_APPROVED"),
        evaluate(fields=(("issue_key", 0.5), ("attempt", False))),
    ):
        published = evaluation.as_dict()
        assert json.loads(json.dumps(published, sort_keys=True)) == published


def test_the_verdict_publishes_every_documented_key():
    assert set(evaluate().as_dict()) == {
        "policy_version",
        "status",
        "decision",
        "can_emit",
        "was_redacted",
        "event_code",
        "level",
        "diagnostic_code",
        "reason",
        "reasons",
        "message",
        "fields",
        "policy",
    }


def test_the_verdict_publishes_the_message_and_fields_of_an_emitted_record():
    published = evaluate().as_dict()
    assert published["status"] == "accepted"
    assert published["message"] == CLEAN_MESSAGE
    assert published["fields"] == [["issue_key", "ACV2-642"], ["attempt", 3]]
    assert published["decision"] == "emit"
    assert published["can_emit"] is True
    assert published["was_redacted"] is False
    assert published["event_code"] == EVENT
    assert published["level"] == "info"
    assert published["reasons"] == list(evaluate().reasons)
    assert published["diagnostic_code"] == "T29_LOG_ACCEPTED"
    assert published["policy_version"] == POLICY_VERSION


def test_the_verdict_publishes_nothing_for_a_refusal():
    published = evaluate(event_code="NOT_APPROVED").as_dict()
    assert published["status"] == "rejected"
    assert published["message"] is None
    assert published["fields"] == []
    assert published["decision"] == "drop"
    assert published["can_emit"] is False


def test_the_verdict_publishes_the_redacted_text_of_a_redacted_record():
    published = evaluate(message="pwd=hunter2").as_dict()
    assert published["status"] == "redacted"
    assert published["message"] == REDACTION_MARKER
    assert published["was_redacted"] is True
    assert published["can_emit"] is True
    assert published["diagnostic_code"] == "T29_LOG_REDACTED"


def test_the_verdict_publishes_a_null_level_when_no_level_was_accepted():
    evaluation = evaluate(level=None)
    assert evaluation.level is None
    assert evaluation.as_dict()["level"] is None


def test_the_event_record_publishes_no_caller_text():
    event = LogEvent(
        event_code=EVENT,
        level=L.INFO,
        message="DISTINCTIVE-MESSAGE",
        fields=(("issue_key", "DISTINCTIVE-VALUE"),),
    )
    published = event.as_dict()
    assert published == {
        "event_code": EVENT,
        "is_usable_event_code": True,
        "level": "info",
        "is_usable_level": True,
        "field_count": 1,
    }
    text = json.dumps(published, sort_keys=True)
    assert "DISTINCTIVE-MESSAGE" not in text
    assert "DISTINCTIVE-VALUE" not in text
    assert "issue_key" not in text


def test_the_event_record_reports_an_unusable_identity_as_none():
    event = LogEvent(
        event_code="bad code!", level="info", message="m", fields=[("a", 1)]
    )
    assert event.as_dict() == {
        "event_code": None,
        "is_usable_event_code": False,
        "level": None,
        "is_usable_level": False,
        "field_count": None,
    }


def test_the_event_record_never_publishes_a_credential_shaped_code():
    code = "AKIAIOSFODNN7EXAMPLE"
    published = LogEvent(event_code=code).as_dict()
    assert published["event_code"] is None
    assert published["is_usable_event_code"] is True
    assert code not in json.dumps(published, sort_keys=True)


# ---------------------------------------------------------------------------
# L. The publishing guards (module invariants, pinned directly)
# ---------------------------------------------------------------------------


def test_the_outcome_assembler_publishes_what_was_validated():
    fields = (("issue_key", "ACV2-642"), ("attempt", 1))
    outcome = module._outcome(
        POLICY,
        S.ACCEPTED,
        "a clause",
        event_code=EVENT,
        level=L.INFO,
        message="text",
        fields=fields,
    )
    assert outcome.event_code == EVENT
    assert outcome.level is L.INFO
    assert outcome.message == "text"
    assert outcome.fields == fields
    assert outcome.can_emit is True


def test_the_outcome_assembler_never_publishes_an_unvalidated_message():
    outcome = module._outcome(POLICY, S.ACCEPTED, "a clause", message="bad\nmessage")
    assert outcome.status is S.ACCEPTED
    assert outcome.message is None


def test_the_outcome_assembler_never_publishes_an_over_long_message():
    outcome = module._outcome(
        POLICY, S.ACCEPTED, "a clause", message="x" * (MAX_MESSAGE_LENGTH + 1)
    )
    assert outcome.message is None


def test_the_outcome_assembler_never_publishes_an_unvalidated_identity():
    outcome = module._outcome(
        POLICY, S.REJECTED, "a clause", event_code="bad code!", level="info"
    )
    assert outcome.event_code is None
    assert outcome.level is None
    assert "bad code!" not in outcome.reason


@pytest.mark.parametrize(
    "fields",
    [
        [("issue_key", 1)],
        (("issue_key", 1),) * (MAX_FIELDS + 1),
        (["issue_key", 1],),
        (("issue_key",),),
        (("Bad-Name", 1),),
        (("issue_key", [1]),),
        (("issue_key", "x" * (MAX_FIELD_VALUE_LENGTH + 1)),),
        (("issue_key", MAX_FIELD_INTEGER + 1),),
        (("issue_key", 1), ("issue_key", 2)),
        (("abcdef0123456789abcdef0123456789", 1),),
    ],
)
def test_the_outcome_assembler_never_publishes_unvalidated_fields(fields):
    outcome = module._outcome(POLICY, S.REDACTED, "a clause", fields=fields)
    assert outcome.fields == ()


#: The refusal statuses that can only be reached *after* the event code has been
#: validated, with the input that reaches each one (§21.8 precedence).  Every case
#: keeps the record's event code at ``EVENT`` on purpose: the verdict is expected
#: to repeat that one identity, and nothing else the caller stated.
REFUSALS_AFTER_VALIDATION = (
    (S.INVALID_FIELD, {"fields": ([EVENT, "v"],)}),
    (S.VALUE_TOO_LARGE, {"message": "x" * (MAX_MESSAGE_LENGTH + 1)}),
    (S.SECRET_DETECTED, {"fields": ((CREDENTIAL_NAMES[0][1], 1),)}),
    (S.REJECTED, {"fields": (("not_allowed", "v"),)}),
)


@pytest.mark.parametrize("status, overrides", REFUSALS_AFTER_VALIDATION)
def test_a_refusal_after_validation_names_only_the_record_own_code(status, overrides):
    """A refusal repeats the caller's *identity*, never the caller's payload."""
    evaluation = evaluate(**overrides)
    assert evaluation.status is status
    assert evaluation.can_emit is False
    assert evaluation.decision is D.DROP
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert evaluation.event_code == EVENT
    assert EVENT in evaluation.reason
    assert EVENT in serialized(evaluation)
    assert CLEAN_MESSAGE not in evaluation.reason


@pytest.mark.parametrize(
    "code",
    (
        None,
        "",
        "bad code!",
        EVENT.lower(),
        "A" * (MAX_EVENT_CODE_LENGTH + 1),
        CREDENTIAL_CODES[0][1],
        CREDENTIAL_CODES[1][1],
        CREDENTIAL_CODES[2][1],
    ),
)
def test_a_code_refused_before_approval_is_never_named(code):
    """An absent, malformed, over-long or credential-shaped code is not echoed."""
    evaluation = evaluate(event_code=code)
    assert evaluation.status in (S.INVALID_EVENT, S.VALUE_TOO_LARGE, S.SECRET_DETECTED)
    assert evaluation.can_emit is False
    assert evaluation.event_code is None
    assert evaluation.message is None
    assert evaluation.fields == ()
    if code:
        assert code not in evaluation.reason
        assert code not in serialized(evaluation)


def test_an_unusable_policy_names_no_identity_at_all():
    """The policy is read first, so an unreadable one publishes nothing at all."""
    evaluation = show(
        LoggingPolicy(allowed_event_codes=None),
        LogEvent(event_code=EVENT, level=L.INFO, message=CLEAN_MESSAGE),
    )
    assert evaluation.status is S.INVALID_POLICY
    assert evaluation.event_code is None
    assert evaluation.level is None
    assert evaluation.message is None
    assert evaluation.fields == ()
    assert EVENT not in serialized(evaluation)


def test_a_refusal_reason_carries_no_field_name_or_value():
    payload = "DistinctivePayloadValue"
    evaluation = evaluate(fields=(("not_allowed", payload),))
    assert evaluation.status is S.REJECTED
    assert payload not in evaluation.reason
    assert payload not in serialized(evaluation)
    assert "not_allowed" not in evaluation.reason
    assert "not_allowed" not in serialized(evaluation)


# ---------------------------------------------------------------------------
# M. Adversarial input
# ---------------------------------------------------------------------------


def test_a_hostile_value_is_refused_without_a_single_call():
    calls = []

    class Hostile:
        def __len__(self):
            calls.append("__len__")
            raise AssertionError("must never be measured")

        def __eq__(self, other):
            calls.append("__eq__")
            raise AssertionError("must never be compared")

        def __hash__(self):
            calls.append("__hash__")
            raise AssertionError("must never be hashed")

        def __str__(self):
            calls.append("__str__")
            raise AssertionError("must never be rendered")

        def __repr__(self):
            calls.append("__repr__")
            raise AssertionError("must never be rendered")

        def __format__(self, spec):
            calls.append("__format__")
            raise AssertionError("must never be formatted")

    evaluation = evaluate(fields=(("issue_key", Hostile()),))
    assert evaluation.status is S.INVALID_FIELD
    assert evaluation.fields == ()
    assert calls == []


def test_a_hostile_object_is_never_read_as_a_configuration():
    calls = []

    class Hostile:
        def __len__(self):
            calls.append("__len__")
            raise AssertionError("must never be measured")

        def __iter__(self):
            calls.append("__iter__")
            raise AssertionError("must never be iterated")

    policy = LoggingPolicy(allowed_event_codes=Hostile())
    assert policy.is_valid is False
    assert calls == []


def test_a_hostile_container_is_never_read_as_fields():
    calls = []

    class Hostile:
        def __len__(self):
            calls.append("__len__")
            return 1

        def __iter__(self):
            calls.append("__iter__")
            return iter(())

    evaluation = evaluate(fields=Hostile())
    assert evaluation.status is S.INVALID_EVENT
    assert calls == []


def test_a_very_large_message_is_refused_quickly():
    started = time.perf_counter()
    evaluation = evaluate(message="x" * 1_000_000)
    elapsed = time.perf_counter() - started
    assert evaluation.status is S.VALUE_TOO_LARGE
    assert evaluation.message is None
    assert elapsed < 2.0


def test_pathological_text_does_not_backtrack():
    started = time.perf_counter()
    for text in (
        "a" * MAX_MESSAGE_LENGTH,
        "A" * MAX_MESSAGE_LENGTH,
        "=" * MAX_MESSAGE_LENGTH,
        "-" * MAX_MESSAGE_LENGTH,
        "a" * 100 + "." * 100 + "a" * 100,
        "Bearer " + "A" * (MAX_MESSAGE_LENGTH - 7),
        "token=" + "A" * (MAX_MESSAGE_LENGTH - 6),
    ):
        evaluation = evaluate(message=text)
        assert evaluation.status in (S.ACCEPTED, S.REDACTED)
    assert time.perf_counter() - started < 2.0


def test_a_wrong_argument_type_is_a_caller_error():
    class Unusual:
        pass

    with pytest.raises(TypeError) as policy_error:
        evaluate_log_event(policy=Unusual(), event=LogEvent())
    with pytest.raises(TypeError) as event_error:
        evaluate_log_event(policy=POLICY, event=Unusual())
    assert "Unusual" not in str(policy_error.value)
    assert "Unusual" not in str(event_error.value)
    assert "LoggingPolicy" in str(policy_error.value)
    assert "LogEvent" in str(event_error.value)


def test_a_policy_keyword_is_required():
    with pytest.raises(TypeError):
        evaluate_log_event(LogEvent())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# N. The module surface (static audit; repository files are only read)
# ---------------------------------------------------------------------------

#: The only modules T29 may import.
STDLIB_IMPORTS = (
    "__future__",
    "dataclasses",
    "enum",
    "re",
    "types",
    "typing",
)

#: Modules the policy must not need: no logger, no I/O, no process, no clock.
FORBIDDEN_MODULES = (
    "logging",
    "os",
    "sys",
    "io",
    "pathlib",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "random",
    "time",
    "datetime",
    "uuid",
    "hashlib",
    "tempfile",
    "shutil",
    "json",
    "secrets",
    "getpass",
    "platform",
    "traceback",
    "warnings",
    "codecs",
    "base64",
    "pickle",
    "sqlite3",
    "threading",
    "asyncio",
)

#: Bare calls the policy must never make (no coercion, no I/O, no dynamic code).
FORBIDDEN_CALLS = (
    "open",
    "print",
    "input",
    "eval",
    "exec",
    "compile",
    "breakpoint",
    "exit",
    "quit",
    "__import__",
    "str",
    "repr",
    "format",
    "vars",
    "globals",
    "locals",
    "getattr",
    "setattr",
    "delattr",
    "bytes",
    "bytearray",
    "help",
    "dir",
)

#: Methods the policy must never call: no logger, no stream, no string surgery.
FORBIDDEN_METHODS = (
    "write",
    "writelines",
    "flush",
    "print",
    "basicConfig",
    "getLogger",
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    "exception",
    "log",
    "system",
    "popen",
    "run",
    "Popen",
    "check_output",
    "check_call",
    "getenv",
    "environ",
    "startswith",
    "endswith",
    "strip",
    "lstrip",
    "rstrip",
    "casefold",
    "upper",
    "replace",
    "split",
    "splitlines",
    "partition",
    "find",
    "rfind",
    "index",
    "rindex",
    "count",
    "translate",
    "encode",
    "decode",
    "loads",
    "dumps",
    "dump",
    "load",
    "update",
    "pop",
    "popitem",
    "clear",
    "setdefault",
    "format",
    "format_map",
)

#: Names that would mean T29 had been wired into the pipeline.
WIRING_NAMES = (
    "logging_policy",
    "LoggingPolicy",
    "LogEvent",
    "LogEvaluation",
    "evaluate_log_event",
    "sanitize_log_value",
    "sanitize_log_fields",
)


def imported_modules(source):
    """The top-level module names ``source`` imports."""
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def called_names(source):
    """The bare function names ``source`` calls."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def called_attributes(source):
    """The attribute names ``source`` calls as methods."""
    attributes = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attributes.add(node.func.attr)
    return attributes


def test_the_module_imports_only_the_standard_library():
    assert imported_modules(SOURCE) == set(STDLIB_IMPORTS)


def test_the_module_imports_no_forbidden_module():
    assert imported_modules(SOURCE).isdisjoint(FORBIDDEN_MODULES)


def test_the_module_imports_no_repository_module():
    repository_modules = {path.stem for path in REPO_ROOT.glob("*.py")}
    assert imported_modules(SOURCE).isdisjoint(repository_modules)


def test_the_module_calls_nothing_dangerous():
    assert called_names(SOURCE).isdisjoint(FORBIDDEN_CALLS)


def test_the_module_calls_no_logger_and_no_string_inspection_method():
    assert called_attributes(SOURCE).isdisjoint(FORBIDDEN_METHODS)


def test_the_module_uses_a_regex_only_inside_its_pattern_table():
    uses = {
        node.func.attr
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    }
    assert uses == {"compile"}


def test_the_module_catches_nothing_and_raises_only_caller_errors():
    tree = ast.parse(SOURCE)
    assert [node for node in ast.walk(tree) if isinstance(node, ast.Try)] == []
    raised = {
        node.exc.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert raised == {"TypeError"}


def test_every_import_is_at_the_top_of_the_module():
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert node.col_offset == 0, ast.dump(node)


def test_the_module_defines_exactly_the_documented_public_surface():
    tree = ast.parse(SOURCE)
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in tree.body)
    public = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]
    assert public == [
        "LogLevel",
        "LogStatus",
        "LogDecision",
        "LoggingPolicy",
        "LogEvent",
        "LogEvaluation",
        "evaluate_log_event",
        "sanitize_log_value",
        "sanitize_log_fields",
    ]


def test_the_module_documents_the_contract():
    docstring = module.__doc__
    assert docstring
    for phrase in ("T29", "REDACTED", "Fail closed", "PRECEDENCE", "SECRET_PATTERNS"):
        assert phrase in docstring, phrase
    assert "unwired" in docstring.lower()


def test_no_repository_module_imports_t29():
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name == MODULE_PATH.name:
            continue
        assert "logging_policy" not in imported_modules(
            path.read_text(encoding="utf-8")
        ), path.name


def test_t29_is_not_wired_into_any_other_module():
    """T29 has no caller: no stage, and not ``main.py``, mentions it."""
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name == MODULE_PATH.name:
            continue
        source = path.read_text(encoding="utf-8")
        for name in WIRING_NAMES:
            assert name not in source, f"{path.name}: {name}"


def test_the_entry_point_is_untouched():
    main = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "T29" not in main
    for name in WIRING_NAMES:
        assert name not in main, name
