"""T29 - bounded logging policy: what may be logged, in what form, and nothing else.

Purpose
-------
T29 answers exactly one question:

    "may this log record be emitted, and if so, in exactly what safe form?"

It is a **policy layer only**.  It writes nothing - no file, no stream, no
standard output, no ``logging`` record - it calls nothing, it orchestrates
nothing, and it touches no network, no Git, no SonarQube, no Codex and no file.
It is a small, auditable contract of three immutable records:

* :class:`LoggingPolicy` - the operator's configuration,
* :class:`LogEvent` - the caller-asserted record an operation wants to log,
* :class:`LogEvaluation` - the deterministic, fail-closed verdict, and the
  *only* thing T29 ever publishes.

The one rule
------------
A log record may be emitted **only** when all of these hold:

    the event code is allowlisted
    and every field name is allowlisted
    and the level is at or above the configured level floor
    and every part is bounded printable ASCII text (or a permitted scalar)
    and no part that is published verbatim is credential-shaped

Everything else is refused, and a refused record publishes no message and no field
at all: not its text, not a field name, not a field value, not a length, not a
hash and not a partial value.  The one caller string a refusal may name is the
record's own event code, and only once that code has proved to be a well-formed,
bounded and credential-free *identity*: a refusal reached before the code is
validated (an unusable policy, an absent, malformed or over-long code) names no
code at all, and a credential-shaped code is never echoed, not even by the
refusal that refuses it.  The default policy allowlists T29's own three verdict
event codes and **no field name at all**, so the default behaviour is to drop
every caller-supplied field until an operator states the exact names they want
logged.

Fail closed
-----------
================================= ==============================================
Input                             Result
================================= ==============================================
unusable policy                   refused: ``INVALID_POLICY``
absent/mistyped record part       refused: ``INVALID_EVENT``
unusable field entry              refused: ``INVALID_FIELD``
a part beyond its bound           refused: ``VALUE_TOO_LARGE``
credential shape in an identity   refused: ``SECRET_DETECTED``
event code, field name or level   refused: ``REJECTED``
  not approved by the operator
credential shape in text          emitted: ``REDACTED`` - the credential span is
                                  replaced by ``[REDACTED]`` and nothing else
everything approved and clean     emitted: ``ACCEPTED``
================================= ==============================================

Only two statuses can emit, :data:`EMITTABLE_STATUSES` is exactly those two, and
``can_emit`` is *derived* from the decision rather than stored, so no caller and
no serialization can turn a refusal into an emission.

Order of the checks
-------------------
The record's parts are examined in reading order - event code, level, message,
the fields container, then each field entry in order - and the refusal classes
are enforced in :data:`PRECEDENCE` order across the whole record:

1. the policy is read first, because an unreadable policy states nothing;
2. then the record's own parts must be usable (``INVALID_EVENT``);
3. then every field entry must be usable (``INVALID_FIELD``);
4. then every part must be within its bound (``VALUE_TOO_LARGE``) - sizes are
   measured only after the values are understood, so a malformed value is always
   reported as malformed rather than as merely large;
5. then no part that is published verbatim may be credential-shaped
   (``SECRET_DETECTED``);
6. then the operator's approval is consulted (``REJECTED``);
7. and only then is text redacted and published (``REDACTED`` / ``ACCEPTED``).

Bounds
------
============================ ========= ==========================================
Constant                     Value     What it bounds
============================ ========= ==========================================
``MAX_EVENT_CODE_LENGTH``    64        one event code
``MAX_MESSAGE_LENGTH``       500       one message
``MAX_FIELD_NAME_LENGTH``    64        one field name
``MAX_FIELD_VALUE_LENGTH``   200       one text field value
``MAX_FIELDS``               20        field entries per record
``MAX_FIELD_INTEGER``        2 ** 53   the magnitude of an integer value
``MAX_ALLOWED_EVENT_CODES``  100       the allowlisted event codes
``MAX_ALLOWED_FIELD_NAMES``  100       the allowlisted field names
============================ ========= ==========================================

Every bound is a fixed module constant that no configuration can widen, raise or
switch off.  A bound violation is ``VALUE_TOO_LARGE``, and the offender is never
truncated: an over-long value is refused, never silently shortened, because a
truncated value is a different value.

Redaction, and what is never published
--------------------------------------
Text is published only after every credential-shaped span in it has been
replaced by the single marker :data:`REDACTION_MARKER` (``[REDACTED]``):

* no prefix, suffix, length, hash or partial form of a credential is ever
  published - the marker is the whole replacement;
* a credential shape found in a part that must be published *verbatim* - the
  event code or a field name, which are the identities the allowlist is
  consulted for - refuses the whole record (``SECRET_DETECTED``) instead of
  rewriting an identity;
* a credential shape found in text (a message, a text field value) redacts that
  span and keeps the rest of the text;
* redaction can only *grow* text, so a redacted result is measured again and a
  redacted value that no longer fits its bound is refused (``VALUE_TOO_LARGE``)
  rather than published over-long;
* diagnostics never echo the offending material: they name the part *by index*
  and, at most, the category label of the credential shape that matched (a fixed
  string from :data:`SECRET_PATTERNS`), never the matched text.

What T29 never does
-------------------
* it never coerces a caller value: no ``str()``, no ``repr()``, no ``bytes()``,
  no ``format()``, so an object with a hostile ``__str__``/``__repr__`` is
  refused without ever being called;
* it never accepts a ``str`` *subclass*, a ``bool`` where an ``int`` is meant or
  any other subtype whose ``__len__``/``__eq__`` the caller could override -
  every type check is an exact type check, and the only exception is the
  immutable :class:`LogLevel` enum;
* it never serializes an arbitrary object: a value is published only when it is
  ``None``, a ``bool``, a bounded ``int``, a finite ``float`` or a bounded
  printable ``str``;
* it never reads the environment, ``sys.argv``, the clock, a file, the network,
  a Git repository or a SonarQube server, and it contains no ``random``,
  ``time``, ``uuid``, ``hashlib`` or ``logging`` import;
* it never installs, configures or reconciles a global logger, and it never
  writes anywhere.

Unwired
-------
T29 is standalone: it is **not** imported by ``main.py`` or by any other module
of this repository, so nothing about the existing pipeline can change because of
it, and ``main.py`` is untouched by this task.  It depends on the Python
standard library only, and therefore on no repository module either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import List, Mapping, Optional, Sequence, Set, Tuple

#: Version of the T29 policy contract.
POLICY_VERSION = "t29.1"

#: The longest accepted event code, in characters.  The codes this policy
#: actually carries are short (``T29_LOG_ACCEPTED`` is 16 characters), so 64
#: leaves room for a versioned, module-prefixed vocabulary and still refuses a
#: value that is not an identity but a payload.
MAX_EVENT_CODE_LENGTH = 64

#: The longest accepted message, in characters.  A log message states an
#: operation's outcome; it is not a place to copy a document, a diff or a stack
#: trace, and an unbounded message makes an unbounded log record.
MAX_MESSAGE_LENGTH = 500

#: The longest accepted field name, in characters.
MAX_FIELD_NAME_LENGTH = 64

#: The longest accepted *text* field value, in characters.  Bounded separately
#: from the message because one record may carry several values.
MAX_FIELD_VALUE_LENGTH = 200

#: The largest number of field entries one record may carry.  Bounded because
#: every entry is validated, allowlisted and redacted, so the number of entries
#: is part of the record's cost.
MAX_FIELDS = 20

#: The largest magnitude an integer field value may have (2 ** 53).  A bigger
#: integer does not survive a JSON round trip in every consumer, so it is
#: refused as a value that is too large rather than published lossily.
MAX_FIELD_INTEGER = 2 ** 53

#: The largest allowlist this policy accepts, in entries, for event codes and
#: for field names.  A curated logging vocabulary is short; a list of thousands
#: is not a curation but an attempt to allow everything, which is exactly the
#: failure mode this policy exists to prevent.
MAX_ALLOWED_EVENT_CODES = 100
MAX_ALLOWED_FIELD_NAMES = 100

#: The marker that replaces credential-shaped text.  It is the whole
#: replacement: no prefix, suffix, length, hash or partial value is published.
REDACTION_MARKER = "[REDACTED]"

#: The default event-code allowlist: T29's own three verdict codes, and nothing
#: else.  A policy that has not been configured therefore recognises exactly the
#: events this repository's own logging layer produces, and any other event code
#: has to be stated by an operator.
DEFAULT_ALLOWED_EVENT_CODES: Tuple[str, ...] = (
    "T29_LOG_ACCEPTED",
    "T29_LOG_REDACTED",
    "T29_LOG_REJECTED",
)

#: The default field-name allowlist: empty.  It is the conservative choice
#: precisely because it publishes no caller-supplied field at all until an
#: operator names the fields they want logged, and no default entry is ever
#: added here.
DEFAULT_ALLOWED_FIELD_NAMES: Tuple[str, ...] = ()

#: The characters an event code may *start* with.  A code is a name, never a
#: digit or a separator, so a leading digit, ``_`` or punctuation is refused
#: instead of being rewritten.
_EVENT_CODE_LEAD_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: The characters an event code may use after its first character: upper-case
#: letters, digits and ``_``.  Nothing else is expressible, so a wildcard, a
#: regex, a path, a URL, a shell fragment, a dotted key and a ``key=value`` pair
#: cannot be spelled as an event code.
_EVENT_CODE_TAIL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

#: The characters a field name may *start* with: a lower-case letter.
_FIELD_NAME_LEAD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz")

#: The characters a field name may use after its first character: lower-case
#: letters, digits and ``_``.  The vocabulary is deliberately the opposite case
#: of the event-code vocabulary, so a name and a code can never be confused for
#: one another.
_FIELD_NAME_TAIL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")

#: The lowest and highest printable ASCII code points.  T29 publishes text only
#: from this range: no control character (so no newline, carriage return, tab or
#: terminal escape that could forge a log line), no ``DEL`` and no non-ASCII
#: character (so no homoglyph, no right-to-left override and no encoding doubt).
_PRINTABLE_MIN = 0x20
_PRINTABLE_MAX = 0x7E


class LogLevel(Enum):
    """The level a log record may state (fail closed).

    The five members are the classic severity vocabulary, and nothing else is
    expressible: there is no ``NOTSET`` (an unset level states nothing), no
    ``TRACE`` and no numeric level.  A level is an :class:`Enum` member rather
    than a string or an integer, so ``"info"``, ``10`` and ``None`` are not
    levels but refusals - T29 never maps a raw value onto a level.  An enum
    member is immutable and an enum with members cannot be subclassed, which is
    why it is the one caller-supplied type T29 accepts besides a plain scalar.
    """

    __test__ = False

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


#: The order of the levels, from the most verbose (:attr:`LogLevel.DEBUG`) to
#: the most severe (:attr:`LogLevel.CRITICAL`).  It is a read-only mapping used
#: for exactly one comparison - ``level >= minimum_level`` - because the enum
#: itself defines no ordering.  Every member is present and every value is
#: distinct, which is pinned by tests.
LEVEL_ORDER: Mapping[LogLevel, int] = MappingProxyType(
    {
        LogLevel.DEBUG: 0,
        LogLevel.INFO: 1,
        LogLevel.WARNING: 2,
        LogLevel.ERROR: 3,
        LogLevel.CRITICAL: 4,
    }
)

#: The default level floor: :attr:`LogLevel.DEBUG`, which drops nothing.  The
#: floor is a *tightening* knob only - raising it can drop records, and no value
#: of it can admit a record whose code or field is not allowlisted.
DEFAULT_MINIMUM_LEVEL = LogLevel.DEBUG


class LogStatus(Enum):
    """Why a record was emitted, redacted or refused (fail closed)."""

    __test__ = False

    #: Emitted: the record is approved and no credential shape was found.
    ACCEPTED = "accepted"
    #: Emitted, with every credential-shaped span replaced by the marker.
    REDACTED = "redacted"
    #: Refused: the configuration is not usable, so nothing can be approved.
    INVALID_POLICY = "invalid-policy"
    #: Refused: the record's own parts are absent or of the wrong type or shape.
    INVALID_EVENT = "invalid-event"
    #: Refused: a field entry is not a usable ``(name, value)`` pair.
    INVALID_FIELD = "invalid-field"
    #: Refused: a part is beyond its bound; the offender is never truncated.
    VALUE_TOO_LARGE = "value-too-large"
    #: Refused: a part published verbatim is credential-shaped.
    SECRET_DETECTED = "secret-detected"
    #: Refused: the event code, a field name or the level is not approved.
    REJECTED = "rejected"


class LogDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    EMIT = "emit"
    DROP = "drop"


#: The statuses that authorise an emission.  A status outside this tuple always
#: yields :attr:`LogDecision.DROP` and therefore ``can_emit == False``; there is
#: no other way for a record to be emitted.
EMITTABLE_STATUSES: Tuple[LogStatus, ...] = (
    LogStatus.ACCEPTED,
    LogStatus.REDACTED,
)

#: The refusal classes, in the order in which they are enforced.  It is
#: deliberately "policy, then the record, then the fields, then sizes, then
#: credentials, then approval, then publication": an unreadable policy refuses
#: before anything is read, a record that is not usable refuses before its
#: fields are judged, a field entry that is not usable refuses before any size
#: is measured, a part beyond its bound refuses before credentials are searched
#: for, a credential shape in an identity refuses before the allowlist is
#: consulted, and nothing but :attr:`LogStatus.ACCEPTED` or
#: :attr:`LogStatus.REDACTED` can ever publish a record.
PRECEDENCE: Tuple[LogStatus, ...] = (
    LogStatus.INVALID_POLICY,
    LogStatus.INVALID_EVENT,
    LogStatus.INVALID_FIELD,
    LogStatus.VALUE_TOO_LARGE,
    LogStatus.SECRET_DETECTED,
    LogStatus.REJECTED,
    LogStatus.REDACTED,
    LogStatus.ACCEPTED,
)

#: The stable, machine-readable diagnostic code of each status.  Codes are fixed
#: text chosen from :data:`PRECEDENCE`'s vocabulary: they carry no caller value,
#: so a report can be grouped and alerted on without parsing prose.
DIAGNOSTIC_CODES: Mapping[LogStatus, str] = MappingProxyType(
    {
        LogStatus.ACCEPTED: "T29_LOG_ACCEPTED",
        LogStatus.REDACTED: "T29_LOG_REDACTED",
        LogStatus.INVALID_POLICY: "T29_POLICY_INVALID",
        LogStatus.INVALID_EVENT: "T29_EVENT_INVALID",
        LogStatus.INVALID_FIELD: "T29_FIELD_INVALID",
        LogStatus.VALUE_TOO_LARGE: "T29_VALUE_TOO_LARGE",
        LogStatus.SECRET_DETECTED: "T29_SECRET_DETECTED",
        LogStatus.REJECTED: "T29_LOG_REJECTED",
    }
)

#: The decisive clause of each status, as a fixed sentence.  A clause names the
#: *class* of the problem only: it never contains a caller value, never contains
#: a credential and never contains the length of caller text.
_STATUS_REASONS: Mapping[LogStatus, str] = MappingProxyType(
    {
        LogStatus.ACCEPTED: (
            "the event code is allowlisted, every field name is allowlisted and "
            "the level is at or above the configured floor, and no part is "
            "credential-shaped, so the record is emitted exactly as stated"
        ),
        LogStatus.REDACTED: (
            "the record is approved, but credential-shaped text was found in a "
            "message or in a text field value, so every such span is replaced by "
            "the redaction marker before the record is emitted"
        ),
        LogStatus.REJECTED: (
            "the operator's configuration does not approve this record: the "
            "event code, a field name or the level is outside what the policy "
            "allows, and T29 logs nothing the operator did not approve"
        ),
        LogStatus.SECRET_DETECTED: (
            "a part that would be published verbatim - the event code or a field "
            "name - is credential-shaped, and an identity is never rewritten, so "
            "the whole record is refused instead"
        ),
        LogStatus.VALUE_TOO_LARGE: (
            "a part of the record is larger than the bound this policy allows, "
            "and an over-large value is refused rather than truncated"
        ),
        LogStatus.INVALID_FIELD: (
            "a field entry is not a usable (name, value) pair of an allowed "
            "scalar type"
        ),
        LogStatus.INVALID_EVENT: (
            "the record's own parts are absent, of the wrong type, or not "
            "bounded printable ASCII text"
        ),
        LogStatus.INVALID_POLICY: (
            "the logging configuration is not usable, so no record can be shown "
            "to be approved"
        ),
    }
)

#: The fail-closed consequence of each status: what happens to a record that
#: receives it.  It is always the second element of the rationale trail, so the
#: trail is ``(reason, consequence)`` and nothing can be appended to it.
_STATUS_CONSEQUENCES: Mapping[LogStatus, str] = MappingProxyType(
    {
        LogStatus.ACCEPTED: (
            "Consequence: the record is emitted unchanged, and every value it "
            "publishes is a validated, bounded scalar."
        ),
        LogStatus.REDACTED: (
            "Consequence: the record is emitted with the redaction marker "
            "standing in for every credential-shaped span, and no prefix, "
            "length, hash or partial form of a credential is published."
        ),
        LogStatus.REJECTED: (
            "Consequence: the record is dropped and publishes no caller text at "
            "all - no message, no field name, no field value and no length."
        ),
        LogStatus.SECRET_DETECTED: (
            "Consequence: the record is dropped and publishes no caller text at "
            "all, so neither the credential nor the shape that matched it is "
            "echoed."
        ),
        LogStatus.VALUE_TOO_LARGE: (
            "Consequence: the record is dropped, and the over-large value is "
            "never truncated, shortened or published in part."
        ),
        LogStatus.INVALID_FIELD: (
            "Consequence: the record is dropped and the offending entry is "
            "named by index only; its name and its value are never echoed."
        ),
        LogStatus.INVALID_EVENT: (
            "Consequence: the record is dropped and nothing about the malformed "
            "part is echoed - not its text, not its length, and not the type it "
            "happened to be."
        ),
        LogStatus.INVALID_POLICY: (
            "Consequence: every record is refused until the configuration states "
            "a usable allowlist, because an unreadable policy approves nothing."
        ),
    }
)

# ---------------------------------------------------------------------------
# Credential shapes
# ---------------------------------------------------------------------------
#
# The table below is the *only* place in T29 that uses ``re``.  Each entry is a
# ``(label, pattern)`` pair:
#
# * the label is a fixed, module-owned category name.  It is the only thing a
#   diagnostic may quote about a credential, and it is never the matched text;
# * the pattern is matched against text that has already been bounded and
#   accepted as printable ASCII, so a match is linear work on an in-memory
#   string: no nested quantifier, no backreference and no catastrophic
#   backtracking is expressible here.  Every quantifier is bounded except the two
#   single-character-class whitespace runs of the ``credential-pair`` pattern,
#   which are left open on purpose so that a keyword separated from its
#   ``=``/``:`` by whitespace is still redacted: inside a validated text that run
#   is bounded by the text's own bound anyway, because the only whitespace
#   printable ASCII allows is the space character.  A test pins both halves of
#   that statement;
# * every run cap is far above the largest text this policy can publish
#   (``MAX_MESSAGE_LENGTH`` is 500 and ``MAX_FIELD_VALUE_LENGTH`` is 200), so a
#   credential run that fills an entire publishable text is matched *in full*: a
#   cap that stopped short of a text's own bound would leave the tail of a long
#   credential unredacted, which is the one failure mode this table exists to
#   prevent;
# * the table order is the *reported* order: when several shapes match, the
#   first label in this tuple names the reason, which keeps the diagnostic
#   deterministic;
# * every pattern requires at least one character, so no zero-length span can
#   exist and redaction can never loop - a test pins that.
#
# The table is deliberately *over*-inclusive.  Publishing a false positive costs
# a marker in a log line; publishing a false negative costs a credential, so
# long random-looking runs are treated as credentials even when they are
# probably identifiers, and that trade-off is documented in the spec.
SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        # scheme://user[:password]@host - the shape ``secret_scan`` already
        # treats as a credential, kept identical here so the two agree.
        "url-userinfo",
        re.compile(
            r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/@?#]{1,4096}@[^\s/]{1,4096}"
        ),
    ),
    (
        # password=..., token: ..., api_key=..., authorization=... and friends.
        # The key vocabulary is fixed here, and the value is the non-space run
        # that follows, so a value containing a space is redacted up to the space
        # rather than not at all.
        "credential-pair",
        re.compile(
            r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|"
            r"access[_-]?key|private[_-]?key|client[_-]?secret|credential|"
            r"credentials|authorization|auth|bearer|cookie|session|"
            r"signing[_-]?key)\s*[:=]\s*[^\s]{1,4096}"
        ),
    ),
    (
        # An HTTP authorization scheme: ``Bearer <token>`` / ``Basic <blob>``.
        "auth-scheme",
        re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{4,4096}"),
    ),
    (
        # A JSON Web Token: three base64url segments separated by dots.
        "json-web-token",
        re.compile(
            r"[A-Za-z0-9_\-]{8,512}\.[A-Za-z0-9_\-]{8,1024}\.[A-Za-z0-9_\-]{8,2048}"
        ),
    ),
    (
        # Well-known provider token prefixes, which are credentials by
        # construction whatever the entropy of the remainder is.
        "prefixed-token",
        re.compile(
            r"\b(?:ghp|gho|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{4,4096}\b"
            r"|\bsk-[A-Za-z0-9]{8,4096}\b"
            r"|\b(?:AKIA|ASIA)[0-9A-Z]{8,4096}\b"
            r"|\bxox[bpasr]-[A-Za-z0-9\-]{4,4096}\b"
            r"|\bAIza[0-9A-Za-z_\-]{8,4096}\b"
            r"|\bya29\.[0-9A-Za-z_\-]{8,4096}\b"
        ),
    ),
    (
        # A long hexadecimal run: an API key, a session id, a signing secret.
        # A 40-character commit SHA also matches, which is over-redaction on
        # purpose - the policy cannot tell the two apart from the text alone.
        "long-hex-run",
        re.compile(r"\b[0-9a-fA-F]{32,4096}\b"),
    ),
    (
        # A long base64 run, with or without padding: a secret key or an opaque
        # blob.  Guarded on both sides so it cannot match inside a longer
        # alphanumeric word.
        "long-base64-run",
        re.compile(
            r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,4096}={0,2}(?![A-Za-z0-9+/=])"
        ),
    ),
    (
        # A PEM private-key banner (the header and the trailer alike).  A
        # validated message is single-line printable ASCII, so the banner alone
        # is what can appear; the marker replaces it either way.
        "private-key-marker",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
            r"|-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Predicates (pure, no I/O, no coercion, no caller object ever touched)
# ---------------------------------------------------------------------------


def _is_printable_text(text: str) -> bool:
    """True only when every character of ``text`` is printable ASCII.

    Printable means code points ``0x20`` to ``0x7E``: an empty string is
    printable (it has nothing to refuse), and no control character, no ``DEL``
    and no non-ASCII character is accepted.
    """
    for character in text:
        if not _PRINTABLE_MIN <= ord(character) <= _PRINTABLE_MAX:
            return False
    return True


def _event_code_grammar_problem(value: str) -> Optional[str]:
    """Why ``value`` is not a well-formed event code, or ``None``.

    The grammar is one upper-case letter followed by upper-case letters, digits
    and underscores, and nothing else: no whitespace, no punctuation, no
    separator, no qualifier and no lower case (case is identity, so
    ``t29_log_accepted`` is a *different* identity, not a spelling of
    ``T29_LOG_ACCEPTED``).
    """
    if not value:
        return "the event code is empty, and an empty value is not an identity"
    if value[0] not in _EVENT_CODE_LEAD_CHARS:
        return (
            "the event code must start with an upper-case letter, and this one "
            "does not"
        )
    for character in value[1:]:
        if character not in _EVENT_CODE_TAIL_CHARS:
            return (
                "the event code may only use upper-case letters, digits and "
                "underscores after its first character"
            )
    return None


def _field_name_grammar_problem(value: str) -> Optional[str]:
    """Why ``value`` is not a well-formed field name, or ``None``.

    The grammar is one lower-case letter followed by lower-case letters, digits
    and underscores, and nothing else.  A field name is therefore a plain
    vocabulary word: no path, no URL, no dotted key, no ``key=value`` pair and
    no case-folded variant of an allowlisted name is expressible.
    """
    if not value:
        return "the field name is empty, and an empty value is not an identity"
    if value[0] not in _FIELD_NAME_LEAD_CHARS:
        return (
            "the field name must start with a lower-case letter, and this one "
            "does not"
        )
    for character in value[1:]:
        if character not in _FIELD_NAME_TAIL_CHARS:
            return (
                "the field name may only use lower-case letters, digits and "
                "underscores after its first character"
            )
    return None


def _is_usable_event_code(value: object) -> bool:
    """True only when ``value`` is a bounded, well-formed event code."""
    if type(value) is not str:
        return False
    if _event_code_grammar_problem(value) is not None:
        return False
    return len(value) <= MAX_EVENT_CODE_LENGTH


def _is_usable_field_name(value: object) -> bool:
    """True only when ``value`` is a bounded, well-formed field name."""
    if type(value) is not str:
        return False
    if _field_name_grammar_problem(value) is not None:
        return False
    return len(value) <= MAX_FIELD_NAME_LENGTH


def _is_safe_event_code(value: object) -> bool:
    """True only when ``value`` is a usable event code with no credential shape.

    This is the predicate every echo of an event code passes: an identity that
    is well-formed *but* credential-shaped is never published, so an event code
    cannot become a channel for a credential.
    """
    if not _is_usable_event_code(value):
        return False
    return _secret_category(value) is None


def _is_safe_field_name(value: object) -> bool:
    """True only when ``value`` is a usable field name with no credential shape."""
    if not _is_usable_field_name(value):
        return False
    return _secret_category(value) is None


def _is_publishable_message(value: object) -> bool:
    """True only when ``value`` is exactly a validated, bounded, non-empty message."""
    if type(value) is not str or not value:
        return False
    if len(value) > MAX_MESSAGE_LENGTH:
        return False
    return _is_printable_text(value)


def _text_type_problem(label: str, value: object) -> str:
    """Why ``value`` is not a ``label`` *string*, as a fixed clause.

    ``None`` (nothing was stated) and a value of another type are different
    problems with different sentences, and both are refusals: absence is neither
    a value nor an empty value.  The clause names neither the value nor the type
    it happened to have, so nothing the caller wrote can reach a diagnostic.
    """
    if value is None:
        return (
            f"the {label} is absent, and absence is not a value: the record "
            "states nothing to log"
        )
    return (
        f"the {label} must be a plain string, and no value is ever coerced into "
        "one"
    )


def _level_problem(value: object) -> str:
    """Why ``value`` is not a level, as a fixed clause."""
    if value is None:
        return (
            "no level was supplied, and a record without a level states no "
            "severity"
        )
    return (
        "the level must be a LogLevel member, and no string, integer or other "
        "value is ever mapped onto a level"
    )


def _fields_container_problem(value: object) -> str:
    """Why ``value`` is not a fields *container*, as a fixed clause."""
    if value is None:
        return (
            "the fields are absent, and absence is not an empty tuple of field "
            "entries"
        )
    return (
        "fields must be an immutable tuple of (name, value) pairs, and no other "
        "container, string or iterator is ever read as one"
    )


def _message_content_problem(value: str) -> Optional[str]:
    """Why ``value`` is not a usable message, or ``None``.

    A message must be non-empty printable ASCII: an empty message states nothing
    to log, and a control character (a newline, a carriage return, a tab, an
    escape) could forge or split a log line, so it is refused rather than
    escaped or removed.
    """
    if not value:
        return "the message is empty, and an empty message states nothing to log"
    if not _is_printable_text(value):
        return (
            "the message must be printable ASCII without control characters, so "
            "no newline, tab, escape sequence or non-ASCII character can forge "
            "or split a log line"
        )
    return None


def _field_pair_problem(pair: object) -> Optional[str]:
    """Why ``pair`` is not a usable ``(name, value)`` pair, or ``None``.

    Only an immutable two-element tuple whose first element is a *plain* field
    name string is accepted: a list, a set, a mapping, a three-element tuple, a
    bare string, a ``str`` subclass and a tuple whose name is not a string are
    all refusals.  The clause never echoes the pair or either element.
    """
    if type(pair) is not tuple:
        return (
            "every field entry must be an immutable tuple of a name and a value, "
            "and no other container is ever read as one"
        )
    if len(pair) != 2:
        return (
            "every field entry must hold exactly two elements, a field name and "
            "a value"
        )
    if type(pair[0]) is not str:
        return (
            "every field entry's first element must be a field name string, and "
            "no value is ever coerced into one"
        )
    return None


#: The two non-finite floats a float field value must not be.  They are module
#: constants rather than a ``math`` import, so the module stays dependency-free
#: and the comparison is exact.
_INFINITY = float("inf")
_NEGATIVE_INFINITY = float("-inf")


def _value_problem(value: object) -> Optional[str]:
    """Why ``value`` is not a permitted field value, or ``None``.

    A permitted value is ``None``, a ``bool``, a bounded ``int``, a finite
    ``float`` or printable ASCII ``str`` - and each of those is checked by exact
    type, so a ``str`` subclass whose ``__len__`` or ``__eq__`` the caller
    overrides, a ``bool`` passed as an ``int``, an ``int`` subclass, a
    ``Decimal``, a ``bytes``, a ``bytearray``, a ``list``, a ``dict``, a ``set``
    and every arbitrary object are refused without a single method of theirs
    ever being called, coerced or serialized.
    """
    if value is None:
        return None
    if type(value) is bool:
        return None
    if type(value) is int:
        return None
    if type(value) is float:
        if value != value or value == _INFINITY or value == _NEGATIVE_INFINITY:
            return (
                "a float field value must be finite, because a value that is not "
                "finite has no deterministic text form"
            )
        return None
    if type(value) is str:
        if not _is_printable_text(value):
            return (
                "a text field value must be printable ASCII without control "
                "characters, so no newline, tab, escape sequence or non-ASCII "
                "character can forge or split a log line"
            )
        return None
    return (
        "a field value must be null, a boolean, an integer, a finite float or "
        "printable ASCII text, and no other object is ever serialized"
    )


def _field_size_problem(index: int, name: str, value: object) -> Optional[str]:
    """Why field entry ``index`` is beyond a bound, or ``None``.

    The entry is named by its 1-based index and by nothing else: neither the
    over-long name nor the over-long value is echoed, and neither is a length of
    caller text.
    """
    if len(name) > MAX_FIELD_NAME_LENGTH:
        return (
            f"field entry {index} has a name longer than the "
            f"{MAX_FIELD_NAME_LENGTH}-character maximum, and an over-long name "
            "is refused rather than truncated"
        )
    if type(value) is str and len(value) > MAX_FIELD_VALUE_LENGTH:
        return (
            f"field entry {index} has a text value longer than the "
            f"{MAX_FIELD_VALUE_LENGTH}-character maximum, and an over-long value "
            "is refused rather than truncated"
        )
    if type(value) is int and not -MAX_FIELD_INTEGER <= value <= MAX_FIELD_INTEGER:
        return (
            f"field entry {index} has an integer value outside the "
            f"+/-{MAX_FIELD_INTEGER} range this policy accepts, and an "
            "out-of-range integer is refused rather than published lossily"
        )
    return None


def _record_size_problem(
    event_code: str,
    message: str,
    fields: Tuple[Tuple[str, object], ...],
) -> Optional[str]:
    """Why a part of the record is beyond a bound, or ``None``.

    Sizes are measured only after every part has been shown to be usable, so a
    malformed value is always reported as malformed rather than as merely large.
    Nothing here truncates: an over-large part refuses the whole record.
    """
    if len(event_code) > MAX_EVENT_CODE_LENGTH:
        return (
            f"the event code is longer than the {MAX_EVENT_CODE_LENGTH}-character "
            "maximum, and an over-long event code is refused rather than "
            "truncated"
        )
    if len(message) > MAX_MESSAGE_LENGTH:
        return (
            f"the message is longer than the {MAX_MESSAGE_LENGTH}-character "
            "maximum, and an over-long message is refused rather than truncated"
        )
    if len(fields) > MAX_FIELDS:
        return (
            f"the record carries more than {MAX_FIELDS} field entries, and a "
            "larger record is refused rather than reduced"
        )
    for index, (name, value) in enumerate(fields, start=1):
        problem = _field_size_problem(index, name, value)
        if problem is not None:
            return problem
    return None


def _published_size_problem(
    message: str,
    fields: Tuple[Tuple[str, object], ...],
) -> Optional[str]:
    """Why approved text can no longer be published, or ``None``.

    Redaction replaces a credential span by the marker, which can be longer than
    the span it replaces, so redaction can only *grow* text.  The published
    result is therefore measured again: a redacted value that no longer fits its
    bound refuses the record (``VALUE_TOO_LARGE``) instead of being published
    over-long, truncated or published in part.
    """
    if len(message) > MAX_MESSAGE_LENGTH:
        return (
            "redaction expanded the message beyond the "
            f"{MAX_MESSAGE_LENGTH}-character maximum, so the redacted message is "
            "refused instead of published over-long"
        )
    for index, (_name, value) in enumerate(fields, start=1):
        if type(value) is str and len(value) > MAX_FIELD_VALUE_LENGTH:
            return (
                f"redaction expanded the text value of field entry {index} beyond "
                f"the {MAX_FIELD_VALUE_LENGTH}-character maximum, so the redacted "
                "value is refused instead of published over-long"
            )
    return None


# ---------------------------------------------------------------------------
# Redaction (the only place a caller's text is rewritten, and never in part)
# ---------------------------------------------------------------------------


def _secret_spans(text: str) -> Tuple[Tuple[int, int], ...]:
    """The merged, ordered, non-overlapping spans of every credential shape.

    Every pattern in :data:`SECRET_PATTERNS` is searched, and the spans that
    overlap or touch are merged, so a text is rebuilt once and the marker can
    never be inserted inside a detection or scanned again.  The result is
    deterministic: equal text always yields equal spans, and the spans are
    sorted, which does not depend on the order in which the patterns matched.
    """
    found: List[Tuple[int, int]] = []
    for _label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.end()))
    if not found:
        return ()
    found.sort()
    merged: List[Tuple[int, int]] = [found[0]]
    for start, end in found[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return tuple(merged)


def _secret_category(text: str) -> Optional[str]:
    """The label of the *first* credential shape ``text`` matches, or ``None``.

    The label is a fixed, module-owned category name - the only thing about a
    credential that a diagnostic may quote.  The table order decides which label
    is reported when several shapes match, so the diagnostic is deterministic.
    """
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text) is not None:
            return label
    return None


def _sanitize(text: str) -> Tuple[str, bool]:
    """Return ``(safe_text, redacted)``: every credential span becomes the marker.

    The replacement is exactly :data:`REDACTION_MARKER`: no prefix, no suffix, no
    length, no hash and no partial form of the matched text survives, and the
    text around a span is kept verbatim.  Adjacent and overlapping detections
    collapse into a single marker because their spans were merged first.
    """
    spans = _secret_spans(text)
    if not spans:
        return (text, False)
    parts: List[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(REDACTION_MARKER)
        cursor = end
    parts.append(text[cursor:])
    return ("".join(parts), True)


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


def _policy_problem(policy: "LoggingPolicy") -> Optional[str]:
    """Why the logging configuration is unusable, or ``None``.

    The order is **shape, then size, then every entry in configuration order**:
    an over-large allowlist is reported as over-large even when it also contains
    an unusable entry, because the bound is enforced before any entry is read.
    An offending entry is named by its 1-based **index** only - the entry itself
    is never echoed, because an entry that is refused may be anything at all,
    including a credential.
    """
    codes = policy.allowed_event_codes
    if type(codes) is not tuple:
        return (
            "allowed_event_codes must be an immutable tuple of event codes, and "
            "no other container, string or iterator is ever read as one, because "
            "a structure that can change after validation cannot state what may "
            "be logged"
        )
    names = policy.allowed_field_names
    if type(names) is not tuple:
        return (
            "allowed_field_names must be an immutable tuple of field names, and "
            "no other container, string or iterator is ever read as one, because "
            "a structure that can change after validation cannot state what may "
            "be logged"
        )
    level = policy.minimum_level
    if not isinstance(level, LogLevel):
        return (
            "minimum_level must be a LogLevel member, and no string, integer or "
            "other value is ever mapped onto a level"
        )
    if len(codes) > MAX_ALLOWED_EVENT_CODES:
        return (
            f"allowed_event_codes holds more than {MAX_ALLOWED_EVENT_CODES} "
            "entries, and a list of thousands is not a curation but an attempt "
            "to allow everything"
        )
    if len(names) > MAX_ALLOWED_FIELD_NAMES:
        return (
            f"allowed_field_names holds more than {MAX_ALLOWED_FIELD_NAMES} "
            "entries, and a list of thousands is not a curation but an attempt "
            "to allow everything"
        )
    seen_codes: Set[str] = set()
    for index, code in enumerate(codes, start=1):
        if not _is_usable_event_code(code):
            return (
                f"allowed event code entry {index} is not a usable event code: an "
                "entry must be a bounded string of upper-case letters, digits and "
                "underscores, and the entry itself is never echoed"
            )
        if _secret_category(code) is not None:
            return (
                f"allowed event code entry {index} is credential-shaped, and an "
                "identity that looks like a credential is never allowlisted, so "
                "it can never be published either"
            )
        if code in seen_codes:
            return (
                f"allowed_event_codes lists entry {index} twice, and a code "
                "allowlisted twice is an ambiguous configuration rather than a "
                "harmless duplicate"
            )
        seen_codes.add(code)
    seen_names: Set[str] = set()
    for index, name in enumerate(names, start=1):
        if not _is_usable_field_name(name):
            return (
                f"allowed field name entry {index} is not a usable field name: an "
                "entry must be a bounded string of lower-case letters, digits and "
                "underscores, and the entry itself is never echoed"
            )
        if _secret_category(name) is not None:
            return (
                f"allowed field name entry {index} is credential-shaped, and an "
                "identity that looks like a credential is never allowlisted, so "
                "it can never be published either"
            )
        if name in seen_names:
            return (
                f"allowed_field_names lists entry {index} twice, and a name "
                "allowlisted twice is an ambiguous configuration rather than a "
                "harmless duplicate"
            )
        seen_names.add(name)
    return None


@dataclass(frozen=True)
class LoggingPolicy:
    """T29 configuration: exactly which records may be logged, and in what form.

    Attributes:
        allowed_event_codes: the event codes this process may log.  Only an
            immutable ``tuple`` is accepted - a list, set, frozenset, mapping,
            string, bytes or iterator can be mutated or reordered *after* it was
            validated, so it is refused (``INVALID_POLICY``) rather than copied
            and trusted.  ``None`` is refused too: an unconfigured allowlist
            never means "allow nothing in particular".  Every entry must be a
            usable event code (see :data:`MAX_EVENT_CODE_LENGTH`) that is not
            credential-shaped, no entry may repeat, and the tuple may hold at
            most :data:`MAX_ALLOWED_EVENT_CODES` entries.  The empty tuple is
            valid and maximally strict: it approves no event at all.
        allowed_field_names: the field names this process may log.  The same
            rules apply, with :data:`MAX_FIELD_NAME_LENGTH` and
            :data:`MAX_ALLOWED_FIELD_NAMES`.  It defaults to the empty tuple,
            which publishes no caller-supplied field at all.
        minimum_level: the least severe level this process may log, as a
            :class:`LogLevel` member (:attr:`LogLevel.DEBUG` by default, which
            drops nothing).  A record below the floor is refused
            (``REJECTED``); raising the floor can only drop records, so the knob
            can only tighten, never widen, what may be logged.
    """

    allowed_event_codes: Tuple[str, ...] = DEFAULT_ALLOWED_EVENT_CODES
    allowed_field_names: Tuple[str, ...] = DEFAULT_ALLOWED_FIELD_NAMES
    minimum_level: LogLevel = DEFAULT_MINIMUM_LEVEL

    @property
    def refusal_reason(self) -> Optional[str]:
        """Why the configuration is unusable, or ``None`` when it is usable."""
        return _policy_problem(self)

    @property
    def is_valid(self) -> bool:
        """True only when the configuration states a usable vocabulary."""
        return _policy_problem(self) is None

    @property
    def allowed_event_code_keys(self) -> Tuple[str, ...]:
        """The configured event codes, verbatim, or ``()`` when unusable.

        The values are the *validated* entries in configuration order and are
        never rewritten: this is the exact identity every approval in T29 uses
        (as membership of a set built from this tuple), which is why a code that
        is merely *similar* to an entry is never approved.  ``()`` is returned
        for an unusable configuration - which the evaluation reports as
        ``INVALID_POLICY``, never as "nothing is allowed".
        """
        if self.refusal_reason is not None:
            return ()
        return tuple(self.allowed_event_codes)

    @property
    def allowed_field_name_keys(self) -> Tuple[str, ...]:
        """The configured field names, verbatim, or ``()`` when unusable."""
        if self.refusal_reason is not None:
            return ()
        return tuple(self.allowed_field_names)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the configuration.

        Only *validated* entries are published, in configuration order.  An
        unusable configuration is reported as ``None`` plus its refusal reason
        instead of echoing the entries that made it unusable, so no caller- or
        operator-authored text can ever be serialized through this record.
        """
        keys = self.allowed_event_code_keys
        names = self.allowed_field_name_keys
        is_valid = self.refusal_reason is None
        return {
            "policy_version": POLICY_VERSION,
            "allowed_event_codes": list(keys) if is_valid else None,
            "allowed_event_code_count": len(keys) if is_valid else None,
            "allowed_field_names": list(names) if is_valid else None,
            "allowed_field_name_count": len(names) if is_valid else None,
            "minimum_level": self.minimum_level.value if is_valid else None,
            "maximum_supported_event_codes": MAX_ALLOWED_EVENT_CODES,
            "maximum_supported_field_names": MAX_ALLOWED_FIELD_NAMES,
            "is_valid": is_valid,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class LogEvent:
    """The caller-asserted record T29 judges.

    T29 never repairs this record: an unusable part is refused, not fixed.

    Attributes:
        event_code: the identity of the event, stated by the caller - for
            example ``T29_LOG_ACCEPTED``.  It is an upper-case, underscore
            separated name of at most :data:`MAX_EVENT_CODE_LENGTH` characters;
            T29 never trims, case-folds, escapes, encodes or normalises it, so an
            empty string, whitespace, a control character, a non-ASCII character,
            a wildcard, a regex, a path, a URL, a shell fragment, a qualified
            key, a ``key=value`` pair, a credential-shaped name and an over-long
            value are all refused.
        level: the severity, as a :class:`LogLevel` member (never a string and
            never an integer), or ``None`` when nothing was stated - an absent
            level is a refusal, because a record without a level states no
            severity.
        message: the human-readable text of the event, which must be non-empty
            printable ASCII of at most :data:`MAX_MESSAGE_LENGTH` characters.
            It is the only caller text T29 is willing to rewrite (credential
            spans become :data:`REDACTION_MARKER`) and never truncate.
        fields: the structured parts of the record, as an immutable tuple of
            immutable ``(name, value)`` pairs - at most :data:`MAX_FIELDS`
            entries, each name a field-name string and each value ``None``, a
            ``bool``, a bounded ``int``, a finite ``float`` or bounded printable
            ``str``.  The container is the empty tuple by default, which is a
            valid record that carries no field at all.
    """

    event_code: Optional[str] = None
    level: Optional[LogLevel] = None
    message: Optional[str] = None
    fields: Tuple[Tuple[str, object], ...] = ()

    @property
    def is_usable_event_code(self) -> bool:
        """True only when ``event_code`` is a bounded, well-formed event code."""
        return _is_usable_event_code(self.event_code)

    @property
    def is_usable_level(self) -> bool:
        """True only when ``level`` is a :class:`LogLevel` member."""
        return isinstance(self.level, LogLevel)

    def as_dict(self) -> dict:
        """Plain, deterministic, text-free view of the caller's own record.

        This record is *untrusted*, so it publishes no caller text at all: not
        the message, not a field name, not a field value, not the credential
        category of an identity, and not even the length of a text.  The event
        code is published only when it is well-formed, bounded *and* free of
        credential shapes, and the level only when it is a
        :class:`LogLevel` member; everything else is reported as ``None``.
        """
        code = self.event_code
        fields = self.fields
        return {
            "event_code": code if _is_safe_event_code(code) else None,
            "is_usable_event_code": self.is_usable_event_code,
            "level": self.level.value if self.is_usable_level else None,
            "is_usable_level": self.is_usable_level,
            "field_count": len(fields) if type(fields) is tuple else None,
        }


@dataclass(frozen=True)
class LogEvaluation:
    """The deterministic, fail-closed verdict for one policy and one record.

    Attributes:
        policy_version: the T29 contract version that produced the verdict.
        status: why the record was emitted, redacted or refused.
        decision: ``EMIT`` only for :data:`EMITTABLE_STATUSES`, else ``DROP``.
        event_code: the caller's event code, verbatim, but only when it is a
            usable event code that carries no credential shape; every absent,
            malformed or credential-shaped value is reported as ``None``, so this
            field can never become a channel for a caller string or a credential.
        level: the caller's level, or ``None`` when nothing usable was stated.
        diagnostic_code: the stable machine token of ``status`` - one of
            :data:`DIAGNOSTIC_CODES` - so a report, a log or an alert can be
            grouped without parsing prose.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its fail-closed
            consequence).
        message: the *safe* message - the validated, redacted text for an emitted
            record, and ``None`` for a refused one, which is why a refusal
            publishes no caller text at all.
        fields: the *safe* fields - the validated, redacted pairs for an emitted
            record, and ``()`` for a refused one.
        policy: the configuration the verdict used, so it explains itself.

    ``can_emit`` is the only permission flag and is *derived* from ``decision``,
    which is derived from ``status``: a status outside
    :data:`EMITTABLE_STATUSES` cannot report permission, and an emitted verdict
    cannot carry a refusal status.  The invariants that must hold are pinned by
    tests, for example ``status in EMITTABLE_STATUSES`` implies
    ``decision is EMIT`` and ``can_emit is True``.
    """

    policy_version: str
    status: LogStatus
    decision: LogDecision
    event_code: Optional[str]
    level: Optional[LogLevel]
    diagnostic_code: str
    reason: str
    reasons: Tuple[str, ...]
    message: Optional[str]
    fields: Tuple[Tuple[str, object], ...]
    policy: LoggingPolicy

    @property
    def can_emit(self) -> bool:
        """True only when the policy approved the record.

        This is the permission flag: it is derived from ``decision`` and never
        stored separately, so no construction, no serialization and no caller can
        turn a refusal into an emission.
        """
        return self.decision is LogDecision.EMIT

    @property
    def was_redacted(self) -> bool:
        """True only when an emitted record had credential text replaced."""
        return self.status is LogStatus.REDACTED

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict.

        Everything published here has already passed the policy: the event code
        and the level only when they are usable and credential-free, the message
        and the fields only for an emitted record (and only after redaction), and
        the configuration only through its own validated view.  T29 emits no
        command, no argv, no environment and no caller object, so nothing in this
        mapping can be executed or serialized back into a credential.
        """
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "decision": self.decision.value,
            "can_emit": self.can_emit,
            "was_redacted": self.was_redacted,
            "event_code": self.event_code,
            "level": self.level.value if self.level is not None else None,
            "diagnostic_code": self.diagnostic_code,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "message": self.message,
            "fields": [[name, value] for name, value in self.fields],
            "policy": self.policy.as_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's records)
# ---------------------------------------------------------------------------


def _is_publishable_fields(fields: object) -> bool:
    """True only when ``fields`` is exactly a validated, redacted field tuple.

    This is the guard every published field tuple passes.  It repeats the whole
    field contract independently of the evaluation - immutable container, bounded
    entry count, immutable two-element pairs, well-formed and credential-free
    names, permitted and bounded values - so it is *structurally* impossible for
    this module to publish a field entry that was not validated, even if a future
    edit passed one here by mistake.  A published pair is immutable, so a caller
    that mutates its own list afterwards cannot change what a verdict carries.
    """
    if type(fields) is not tuple or len(fields) > MAX_FIELDS:
        return False
    seen: Set[str] = set()
    for pair in fields:
        if type(pair) is not tuple or len(pair) != 2:
            return False
        name, value = pair[0], pair[1]
        # ``_is_safe_field_name`` already enforces the name bound, so the guard
        # stays complete without repeating it; the value bound is enforced here.
        if not _is_safe_field_name(name) or name in seen:
            return False
        if _value_problem(value) is not None:
            return False
        if type(value) is str and len(value) > MAX_FIELD_VALUE_LENGTH:
            return False
        if type(value) is int and not -MAX_FIELD_INTEGER <= value <= MAX_FIELD_INTEGER:
            return False
        seen.add(name)
    return True


def _outcome(
    policy: LoggingPolicy,
    status: LogStatus,
    clause: str,
    *,
    event_code: Optional[str] = None,
    level: Optional[LogLevel] = None,
    message: Optional[str] = None,
    fields: Tuple[Tuple[str, object], ...] = (),
) -> LogEvaluation:
    """Assemble the verdict from the deciding status and the validated parts.

    Every part is *re-validated here*, independently of the way the evaluation
    produced it: the event code is echoed only through
    :func:`_is_safe_event_code`, the level only when it is a :class:`LogLevel`
    member, and the message and the fields only for an emitting status and only
    when the publishing guards accept them.  It is therefore structurally
    impossible for this module to publish an unvalidated caller string or a
    credential-shaped identity, even if a future edit passed one here by mistake:
    a malformed value is reported as ``None`` (or ``()``), never echoed.
    ``clause`` is either a fixed sentence or one built from this module's own
    labels and indices, and the rationale trail is always
    ``(reason, consequence)`` so nothing can be appended to it.
    """
    emittable = status in EMITTABLE_STATUSES
    echoed_code = event_code if _is_safe_event_code(event_code) else None
    echoed_level = level if isinstance(level, LogLevel) else None
    if emittable and _is_publishable_message(message):
        published_message = message
    else:
        published_message = None
    if emittable and _is_publishable_fields(fields):
        published_fields = fields
    else:
        published_fields = ()
    verb = "Emitted" if emittable else "Refused"
    if echoed_code is None:
        reason = f"{verb} ({status.value}): {clause}."
    else:
        reason = f"{verb} ({status.value}) for event '{echoed_code}': {clause}."
    return LogEvaluation(
        policy_version=POLICY_VERSION,
        status=status,
        decision=LogDecision.EMIT if emittable else LogDecision.DROP,
        event_code=echoed_code,
        level=echoed_level,
        diagnostic_code=DIAGNOSTIC_CODES[status],
        reason=reason,
        reasons=(reason, _STATUS_CONSEQUENCES[status]),
        message=published_message,
        fields=published_fields,
        policy=policy,
    )


def evaluate_log_event(
    *,
    policy: LoggingPolicy,
    event: LogEvent,
) -> LogEvaluation:
    """Decide whether one log record may be emitted, and in what safe form.

    Pure and deterministic: it reads only its two arguments, writes nothing,
    remembers nothing, counts nothing and performs no filesystem, network,
    subprocess, Git or logging call.  The same pair always produces an equal
    verdict, and neither argument is ever mutated.

    The check order is :data:`PRECEDENCE`, applied to the record's parts in
    reading order (event code, level, message, the fields container, then each
    field entry in order), and every uncertainty is a refusal: an unreadable
    policy refuses before anything is read, a record that is not usable refuses
    before its fields are judged, an unusable field entry refuses before any size
    is measured, an over-large part refuses before credentials are searched for,
    a credential-shaped identity refuses before the allowlist is consulted, and
    text is redacted only for a record that is approved - so nothing but an
    approved, bounded, credential-free record can ever be emitted.

    Args:
        policy: the operator's :class:`LoggingPolicy` - which event codes and
            field names may be logged, and the level floor.
        event: the caller-asserted :class:`LogEvent` - the record an operation
            wants to log, which T29 never repairs and never rewrites apart from
            replacing credential-shaped spans in text.

    Returns:
        An immutable :class:`LogEvaluation`.  For an emitting status its
        ``message`` and ``fields`` are the *only* caller text T29 publishes, and
        they are validated, bounded and redacted; for a refusal both are empty,
        so a refused record publishes no caller text at all.

    Raises:
        TypeError: ``policy`` is not a :class:`LoggingPolicy`, or ``event`` is
            not a :class:`LogEvent`.  That is a caller error, not a record
            problem - every record problem is a refusal in the returned
            evaluation - and the message names nothing the caller wrote.
    """
    if not isinstance(policy, LoggingPolicy):
        raise TypeError(
            "policy must be a LoggingPolicy instance, and no other object is "
            "ever read as a logging configuration."
        )
    if not isinstance(event, LogEvent):
        raise TypeError(
            "event must be a LogEvent instance, and no other object is ever read "
            "as a log record."
        )

    # 1. A policy that cannot be read states no approved vocabulary, so nothing
    #    is judged - and, in particular, nothing can be emitted.
    problem = _policy_problem(policy)
    if problem is not None:
        return _outcome(policy, LogStatus.INVALID_POLICY, problem)

    event_code = event.event_code
    level = event.level
    message = event.message
    fields = event.fields

    # 2. The record's own parts must be usable before a single field is judged:
    #    a missing or mistyped part is a refusal, never a default.
    if type(event_code) is not str:
        return _outcome(
            policy,
            LogStatus.INVALID_EVENT,
            _text_type_problem("event code", event_code),
        )
    problem = _event_code_grammar_problem(event_code)
    if problem is not None:
        return _outcome(policy, LogStatus.INVALID_EVENT, problem)
    if not isinstance(level, LogLevel):
        return _outcome(policy, LogStatus.INVALID_EVENT, _level_problem(level))
    if type(message) is not str:
        return _outcome(
            policy,
            LogStatus.INVALID_EVENT,
            _text_type_problem("message", message),
        )
    problem = _message_content_problem(message)
    if problem is not None:
        return _outcome(policy, LogStatus.INVALID_EVENT, problem)
    if type(fields) is not tuple:
        return _outcome(
            policy,
            LogStatus.INVALID_EVENT,
            _fields_container_problem(fields),
        )

    # 3. Every field entry must be a usable immutable pair of an identity and a
    #    permitted scalar.  Entries are named by index only: a name or a value
    #    that is refused may be anything at all, so neither is ever echoed.
    seen_names: Set[str] = set()
    for index, pair in enumerate(fields, start=1):
        problem = _field_pair_problem(pair)
        if problem is not None:
            return _outcome(
                policy,
                LogStatus.INVALID_FIELD,
                f"field entry {index}: {problem}",
                event_code=event_code,
                level=level,
            )
        name, value = pair[0], pair[1]
        problem = _field_name_grammar_problem(name)
        if problem is not None:
            return _outcome(
                policy,
                LogStatus.INVALID_FIELD,
                f"field entry {index}: {problem}",
                event_code=event_code,
                level=level,
            )
        if name in seen_names:
            return _outcome(
                policy,
                LogStatus.INVALID_FIELD,
                f"field entry {index} repeats a name an earlier entry already "
                "stated, and a record with two entries for one name is ambiguous "
                "rather than merely redundant",
                event_code=event_code,
                level=level,
            )
        problem = _value_problem(value)
        if problem is not None:
            return _outcome(
                policy,
                LogStatus.INVALID_FIELD,
                f"field entry {index}: {problem}",
                event_code=event_code,
                level=level,
            )
        seen_names.add(name)

    # 4. Bounds are enforced only after every part is understood, so a malformed
    #    value is always reported as malformed rather than as merely large, and an
    #    offending part is named by index: the length of caller text is not
    #    something T29 publishes either.  Nothing here truncates.
    problem = _record_size_problem(event_code, message, fields)
    if problem is not None:
        return _outcome(
            policy,
            LogStatus.VALUE_TOO_LARGE,
            problem,
            event_code=event_code,
            level=level,
        )

    # 5. A part that is published verbatim - the event code and every field name,
    #    which are the identities the allowlist is consulted for - must not be
    #    credential-shaped: an identity is never rewritten, so such a record is
    #    refused whole.  Only the category label is reported, never the match, and
    #    a credential-shaped event code is deliberately not passed on: it is never
    #    echoed, not even by the refusal that refuses it.
    category = _secret_category(event_code)
    if category is not None:
        return _outcome(
            policy,
            LogStatus.SECRET_DETECTED,
            f"the event code is credential-shaped ({category}), and an event code "
            "is an identity that is never rewritten, so the record is refused "
            "instead of redacted",
            level=level,
        )
    for index, pair in enumerate(fields, start=1):
        category = _secret_category(pair[0])
        if category is not None:
            return _outcome(
                policy,
                LogStatus.SECRET_DETECTED,
                f"field entry {index} has a credential-shaped name ({category}), "
                "and a field name is an identity that is never rewritten, so the "
                "record is refused instead of redacted",
                event_code=event_code,
                level=level,
            )

    # 6. The operator's approval is consulted last, because it can only refuse:
    #    the level floor, then the event code, then each field name in order.
    if LEVEL_ORDER[level] < LEVEL_ORDER[policy.minimum_level]:
        return _outcome(
            policy,
            LogStatus.REJECTED,
            f"the level '{level.value}' is below the configured floor "
            f"'{policy.minimum_level.value}', and a record below the floor is "
            "never logged",
            event_code=event_code,
            level=level,
        )
    allowed_codes = frozenset(policy.allowed_event_code_keys)
    if event_code not in allowed_codes:
        return _outcome(
            policy,
            LogStatus.REJECTED,
            "the event code is usable but is not one of the allowlisted event "
            "codes, and T29 logs no event the operator did not approve",
            event_code=event_code,
            level=level,
        )
    allowed_names = frozenset(policy.allowed_field_name_keys)
    for index, pair in enumerate(fields, start=1):
        if pair[0] not in allowed_names:
            return _outcome(
                policy,
                LogStatus.REJECTED,
                f"field entry {index} is not one of the allowlisted field names, "
                "and T29 logs no field the operator did not approve",
                event_code=event_code,
                level=level,
            )

    # 7. The record is approved, so redact: every credential span in the message
    #    and in each text field value becomes the marker, and the record is
    #    published only when the redacted text still fits its bound.
    safe_message, redacted = _sanitize(message)
    safe_pairs: List[Tuple[str, object]] = []
    for name, value in fields:
        if type(value) is str:
            safe_value, hit = _sanitize(value)
            if hit:
                redacted = True
        else:
            safe_value = value
        safe_pairs.append((name, safe_value))
    safe_fields = tuple(safe_pairs)
    problem = _published_size_problem(safe_message, safe_fields)
    if problem is not None:
        return _outcome(
            policy,
            LogStatus.VALUE_TOO_LARGE,
            problem,
            event_code=event_code,
            level=level,
        )
    status = LogStatus.REDACTED if redacted else LogStatus.ACCEPTED
    return _outcome(
        policy,
        status,
        _STATUS_REASONS[status],
        event_code=event_code,
        level=level,
        message=safe_message,
        fields=safe_fields,
    )


def sanitize_log_value(text: str) -> Tuple[str, bool]:
    """Return ``(safe_text, redacted)`` for one already-validated text.

    Every credential-shaped span in ``text`` is replaced by exactly
    :data:`REDACTION_MARKER` and the rest of the text is kept verbatim, so the
    caller can log ``safe_text`` and use ``redacted`` to choose a diagnostic.

    This is the same redaction the evaluation performs on an approved record's
    message and text field values - one implementation, one marker - exposed for
    callers that already hold validated text.

    The helper deliberately enforces **no** bound and no grammar: it is not a
    validator.  The policy validates and bounds text *before* it redacts, because
    redaction must run on text the policy has understood; call
    :func:`evaluate_log_event` for untrusted input instead of this helper.

    Raises:
        TypeError: ``text`` is not a plain string.  A ``str`` subclass, a
            ``bytes``, an arbitrary object and every other non-string are
            refused rather than coerced, and the message names nothing the caller
            wrote.
    """
    if type(text) is not str:
        raise TypeError(
            "text must be a plain string, and no other object is ever coerced "
            "into one."
        )
    return _sanitize(text)


def sanitize_log_fields(
    fields: Tuple[Tuple[str, object], ...],
) -> Tuple[Tuple[str, object], ...]:
    """Redact the *text* values of already-validated field entries.

    Returns a new immutable tuple in which every ``str`` value has been through
    :func:`sanitize_log_value` and every non-text value (``None``, a ``bool``, an
    ``int``, a finite ``float``) is passed through unchanged.  Nothing is
    serialized: a value that is not text is copied by reference, never rendered.

    Like :func:`sanitize_log_value` this helper enforces no bound and no
    grammar - the policy validates and bounds an entry before it redacts it.

    Raises:
        TypeError: ``fields`` is not a tuple, or an entry is not an immutable
            two-element pair whose name is a plain string.  The message names the
            offending entry by index only.
    """
    if type(fields) is not tuple:
        raise TypeError(
            "fields must be an immutable tuple of (name, value) pairs, and no "
            "other container is ever read as one."
        )
    sanitized: List[Tuple[str, object]] = []
    for index, pair in enumerate(fields, start=1):
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str:
            raise TypeError(
                f"field entry {index} must be an immutable pair of a plain string "
                "name and a value."
            )
        name, value = pair[0], pair[1]
        if type(value) is str:
            safe_value, _hit = _sanitize(value)
            sanitized.append((name, safe_value))
        else:
            sanitized.append((name, value))
    return tuple(sanitized)


__all__: Sequence[str] = (
    "DEFAULT_ALLOWED_EVENT_CODES",
    "DEFAULT_ALLOWED_FIELD_NAMES",
    "DEFAULT_MINIMUM_LEVEL",
    "DIAGNOSTIC_CODES",
    "EMITTABLE_STATUSES",
    "LEVEL_ORDER",
    "LogDecision",
    "LogEvaluation",
    "LogEvent",
    "LogLevel",
    "LogStatus",
    "LoggingPolicy",
    "MAX_ALLOWED_EVENT_CODES",
    "MAX_ALLOWED_FIELD_NAMES",
    "MAX_EVENT_CODE_LENGTH",
    "MAX_FIELDS",
    "MAX_FIELD_INTEGER",
    "MAX_FIELD_NAME_LENGTH",
    "MAX_FIELD_VALUE_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "POLICY_VERSION",
    "PRECEDENCE",
    "REDACTION_MARKER",
    "SECRET_PATTERNS",
    "evaluate_log_event",
    "sanitize_log_fields",
    "sanitize_log_value",
)
