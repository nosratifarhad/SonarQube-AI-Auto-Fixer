"""T28 - Sonar rule allowlist: only an explicitly allowlisted rule may be fixed.

Purpose
-------
T28 answers exactly one question:

    "is this SonarQube rule explicitly allowed to be fixed automatically?"

It is a **policy layer only**. It calls nothing, fixes nothing, classifies
nothing, orchestrates nothing, and it touches no network, no Git, no SonarQube,
no Codex and no file. It is a small, auditable contract of three immutable
records:

* :class:`RuleAllowlistPolicy` - the operator's configuration (the allowlist),
* :class:`RuleAllowlistInput` - the caller-asserted rule identity,
* :class:`RuleAllowlistEvaluation` - the deterministic, fail-closed verdict.

The one rule
------------
An automatic fix is authorised **only** when the rule ID the caller states is
*exactly* one of the rule IDs the operator configured:

    rule_id in allowed_rules   <=>   ALLOWED

There is no severity, no issue type, no rule family, no rule prefix, no
language, no file extension, no historical success, no confidence score, no
previous-fix count and no Sonar message text anywhere in this module - not as
an input, not as a field, not as a parameter - so none of them can ever widen
what is allowed. The default allowlist is empty, so the default behaviour is to
deny every rule until an operator states the exact IDs they want fixed.

Exact match, never a pattern
----------------------------
Matching is **exact string identity** through membership of a ``frozenset``
built from the validated configuration, and this module does not import ``re``
and contains no wildcard, glob, regex, prefix, suffix, substring or case-folding
logic at all. ``S1118`` is not ``S111``, ``S1118*``, ``S11*``, ``xS1118``,
``S1118x``, ``s1118`` or ``java:S1118``: only the exact configured string is
allowed. A pattern such as ``S11*`` is not a tolerance - it is a malformed rule
ID and is refused outright, in the configuration and in the input alike.
Wildcard support, if it is ever wanted, is a new version of this policy rather
than a flag in this one.

Rule ID grammar
---------------
A rule ID is **ASCII** and at most :data:`MAX_RULE_ID_LENGTH` characters:

* an optional SonarQube repository/language qualifier in front of one ``:``,
* each part starting with a letter or a digit,
* every remaining character from letters, digits, ``.``, ``_`` and ``-``.

Both forms this project's SonarQube model actually carries are therefore
accepted - the bare key (``S1118``) and the language-qualified key SonarQube
reports (``python:S1481``, ``java:S108``, ``javascript:S1854``, all of which
appear in this repository's own fixtures and commit-message layer) - and they
are *different identities*: a policy that allowlists ``S1118`` does not allow
``csharpsquid:S1118``, because T28 never rewrites the rule a caller stated.
Nothing is trimmed, case-folded, escaped, normalised or coerced: a leading,
trailing or embedded space, a newline, a tab, an empty string, a path, a URL, a
shell fragment, a wildcard, a regex metacharacter, a non-ASCII character, an
over-long value and a value that is not a string at all are all refusals.

Fail closed
-----------
=============================== =========================================
Input                           Result
=============================== =========================================
unusable allowlist              refused: ``INVALID_POLICY``
no rule stated (``None``)       refused: ``INVALID_INPUT``
rule is not a string            refused: ``INVALID_INPUT``
rule is not a usable rule ID    refused: ``INVALID_RULE_ID``
well-formed but not listed      refused: ``RULE_NOT_ALLOWED``
exactly listed in the allowlist allowed: ``ALLOWED``
=============================== =========================================

:data:`PRECEDENCE` is the authoritative evaluation order: a configuration that
cannot state an allowlist refuses before any rule is read, and an unusable rule
identity refuses before it is looked up - so a malformed policy or a malformed
rule ID can never be reported as ``RULE_NOT_ALLOWED``, and nothing but
``ALLOWED`` can ever authorise a fix.

Trust boundary
--------------
Both inputs are **caller-asserted**. T28 establishes no fact about SonarQube: it
does not know whether the rule exists, whether it is active in the quality
profile, whether an issue really carries it, or whether a fix for it would be
correct. It validates the *shape* of the rule identity and performs one exact
membership test against the operator's list. A caller that misreports which rule
an issue carries is not detected here. This is a policy boundary, not an
evidence-authentication boundary: it exists so a future orchestration layer
cannot *accidentally* let the AI fixer edit code for a rule the operator never
approved.

Determinism and purity
----------------------
The verdict is a pure function of ``(policy, rule_input)``: no clock, no
environment value, no random source, no cache, no counter and no mutable module
state is read or written, the configuration is consulted in its own order, and
the same two arguments always produce an equal record. The caller's records are
never mutated and no caller value is repaired. The diagnostic codes and both
reason sentences are fixed text (or, for the allowed and not-allowed verdicts,
the *validated* rule ID), so a report cannot leak arbitrary caller text,
environment variables, paths, tokens, credentials or issue messages.

Limits
------
* :data:`MAX_RULE_ID_LENGTH` = 64 characters for one rule ID (qualifier
  included), so no pathologically long value can enter a comparison or a log.
* :data:`MAX_ALLOWED_RULES` = 1000 entries for one allowlist, so no
  configuration can grow without bound.

Both are constants, both are enforced before any work is done, and both are
tested at max-1, max and max+1.

Scope of the answer
-------------------
T28 does not decide whether a Sonar issue is safe to fix based on severity,
issue type, language, confidence, historical success, or message content. It
only answers whether the exact Sonar rule ID is explicitly allowlisted.

Not wired
---------
T28 currently has no caller and is intentionally not wired into the execution
pipeline: ``main.py`` is byte-for-byte unchanged, T19-T27 are untouched, and
this module imports the Python standard library only. It has no dependency on
any other module in this repository, so nothing about the existing pipeline can
change because of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Set, Tuple

#: Version of the T28 policy contract.
POLICY_VERSION = "t28.1"

#: The longest accepted rule ID, in characters (the optional qualifier is
#: included in the count). The rule keys this project's SonarQube model actually
#: carries are short - the longest in this repository's own fixtures and commit
#: layer is 16 characters (``javascript:S1854``) - so 64 is roughly four times
#: the widest real value and still far below T20's generic 100-character token
#: cap, which means every rule ID T28 accepts can also be published in a T20
#: commit message. A longer value is a configuration or input error rather than
#: a rule ID, and refusing it keeps the policy's domain total and explicit.
MAX_RULE_ID_LENGTH = 64

#: The largest allowlist T28 accepts, in entries. A curated list of rules an AI
#: agent is trusted to fix by itself is expected to be short (a few dozen); a
#: list of thousands is not a curation but an attempt to allow everything, which
#: is exactly the failure mode this policy exists to prevent. The bound also
#: keeps every validation and comparison in this module strictly bounded work.
MAX_ALLOWED_RULES = 1_000

#: The default allowlist: empty. It is the conservative choice precisely because
#: it allows nothing - a policy that has not been configured denies every rule
#: instead of silently enabling one, and no default entry is ever added here.
DEFAULT_ALLOWED_RULES: Tuple[str, ...] = ()

#: The one character that may separate the optional SonarQube qualifier from the
#: rule key (``python:S1481``). At most one is supported: this project's Sonar
#: model carries ``<repository>:<key>`` pairs, never deeper nesting.
_QUALIFIER_SEPARATOR = ":"

#: The characters a rule ID may use after its first character: letters, digits
#: and the three separators SonarQube rule keys actually contain (``.`` in
#: ``common-java:DuplicatedBlocks``, ``_`` and ``-`` in
#: ``external_eslint_repo:no-unused-vars``). Whitespace, quotes, slashes,
#: backslashes, ``*``, ``?``, ``[``, ``]``, ``^``, ``$``, ``(``, ``)``, ``|``,
#: ``;``, ``#`` and every other punctuation mark are absent, so a wildcard, a
#: regex, a path, a URL and a shell fragment cannot be spelled as a rule ID.
_RULE_ID_TAIL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-"
)

#: The characters a rule ID may *start* with, in every part of it. A leading
#: ``.``, ``-`` or ``_`` would make the value look like a relative path, a
#: command-line option or a private name, so it is refused instead of being
#: rewritten.
_RULE_ID_LEAD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
)


class RuleAllowlistStatus(Enum):
    """Why a rule was allowed or refused (fail closed)."""

    __test__ = False

    #: Allowed: the rule ID is exactly one of the configured allowlist entries.
    ALLOWED = "allowed"
    #: Refused: the rule ID is a usable identity but is not in the allowlist.
    RULE_NOT_ALLOWED = "rule-not-allowed"
    #: Refused: a rule *string* was supplied and it is not a usable rule ID.
    INVALID_RULE_ID = "invalid-rule-id"
    #: Refused: the record carries no rule string at all (``None`` or another
    #: type). An absent rule identity is not a rule that is merely unlisted.
    INVALID_INPUT = "invalid-input"
    #: Refused: the allowlist configuration is not usable.
    INVALID_POLICY = "invalid-policy"


class RuleAllowlistDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    ALLOW = "allow"
    REFUSE = "refuse"


#: The statuses that authorise an automatic fix. A status outside this tuple
#: always yields :attr:`RuleAllowlistDecision.REFUSE` and therefore
#: ``can_auto_fix == False``; there is no other way to permit a fix.
ALLOWED_STATUSES: Tuple[RuleAllowlistStatus, ...] = (
    RuleAllowlistStatus.ALLOWED,
)

#: The evaluation order: the first status whose condition holds decides the
#: result. It is deliberately "policy, then identity, then lookup": an allowlist
#: that cannot say which rules are approved refuses first, a record that carries
#: no rule string refuses next, a rule string that is not a usable rule ID
#: refuses next, and only a well-formed rule is ever looked up - so a malformed
#: policy or a malformed rule ID can never be reported as ``RULE_NOT_ALLOWED``
#: (a judgement about a list T28 would not have been able to read), and nothing
#: but :attr:`RuleAllowlistStatus.ALLOWED` can ever authorise a fix.
PRECEDENCE: Tuple[RuleAllowlistStatus, ...] = (
    RuleAllowlistStatus.INVALID_POLICY,
    RuleAllowlistStatus.INVALID_INPUT,
    RuleAllowlistStatus.INVALID_RULE_ID,
    RuleAllowlistStatus.RULE_NOT_ALLOWED,
    RuleAllowlistStatus.ALLOWED,
)

#: The stable, machine-readable diagnostic code of each status. Codes are fixed
#: text chosen from :data:`PRECEDENCE`'s vocabulary: they carry no caller value,
#: so a report can be grouped and alerted on without parsing prose.
DIAGNOSTIC_CODES: Mapping[RuleAllowlistStatus, str] = MappingProxyType(
    {
        RuleAllowlistStatus.INVALID_POLICY: "T28_POLICY_INVALID",
        RuleAllowlistStatus.INVALID_INPUT: "T28_INPUT_INVALID",
        RuleAllowlistStatus.INVALID_RULE_ID: "T28_RULE_ID_INVALID",
        RuleAllowlistStatus.RULE_NOT_ALLOWED: "T28_RULE_NOT_ALLOWED",
        RuleAllowlistStatus.ALLOWED: "T28_RULE_ALLOWED",
    }
)

#: The decisive clause of each status, as a fixed sentence. A clause names the
#: rule *question* only: it never contains a caller value other than the rule ID
#: that the evaluation already validated.
_STATUS_REASONS: Mapping[RuleAllowlistStatus, str] = MappingProxyType(
    {
        RuleAllowlistStatus.ALLOWED: (
            "the rule ID is exactly one of the allowlisted rule IDs, so this "
            "rule is approved for an automatic fix"
        ),
        RuleAllowlistStatus.RULE_NOT_ALLOWED: (
            "the rule ID is a usable identity but is not one of the allowlisted "
            "rule IDs, and T28 denies every rule the operator did not approve"
        ),
        RuleAllowlistStatus.INVALID_RULE_ID: (
            "the supplied value is not a usable rule ID"
        ),
        RuleAllowlistStatus.INVALID_INPUT: (
            "the record carries no rule ID string to judge"
        ),
        RuleAllowlistStatus.INVALID_POLICY: (
            "the allowlist configuration is not usable, so no rule can be "
            "shown to be approved"
        ),
    }
)

#: The fail-closed consequence T28 attaches to each status, as the second
#: (contextual) reason. It is fixed text that restates the rule, so no refusal
#: can be read as a tolerance and no allowance can be read as a guarantee.
_STATUS_CONSEQUENCES: Mapping[RuleAllowlistStatus, str] = MappingProxyType(
    {
        RuleAllowlistStatus.INVALID_POLICY: (
            "No rule is judged: an unusable allowlist refuses before anything "
            "else is considered."
        ),
        RuleAllowlistStatus.INVALID_INPUT: (
            "No rule is judged: a record with no rule ID string is refused, "
            "never repaired, coerced or defaulted."
        ),
        RuleAllowlistStatus.INVALID_RULE_ID: (
            "No rule is judged: a malformed rule ID is refused, never trimmed, "
            "normalised, case-folded or pattern-matched into a usable one."
        ),
        RuleAllowlistStatus.RULE_NOT_ALLOWED: (
            "The fix is refused: T28 allows only rules that are explicitly "
            "listed, and it never infers approval."
        ),
        RuleAllowlistStatus.ALLOWED: (
            "T28 allows only because the exact rule ID is explicitly listed; it "
            "proves nothing else about the rule or the issue."
        ),
    }
)


# ---------------------------------------------------------------------------
# Reading and validating a rule ID (pure, never repairs or normalises anything)
# ---------------------------------------------------------------------------

#: Clause templates for the character classes a rule ID must not contain. They
#: are templates rather than messages so the same classification can describe an
#: input value and a configuration entry; ``{what}`` is always T28's own label
#: for the field being read, never the value itself.
_CHARACTER_PROBLEMS: Mapping[str, str] = MappingProxyType(
    {
        "control": (
            "{what} contains a control character (such as a newline or a tab)."
        ),
        "space": (
            "{what} contains whitespace, and nothing is trimmed: a rule ID must "
            "be exact."
        ),
        "non-ascii": (
            "{what} contains a character outside ASCII."
        ),
        "punctuation": (
            "{what} contains a character that is not allowed in a rule ID "
            "(letters, digits, '.', '_', '-' and at most one ':' separator), so "
            "a wildcard, a regex, a path, a URL or a shell fragment cannot be a "
            "rule identity."
        ),
    }
)


def _character_class(character: str) -> str:
    """The class of an unusable rule-ID character, or ``""`` when it is usable.

    Read-only and total: the classification is a pure function of the single
    character, it never echoes that character, and it is decided in a fixed
    order (control, whitespace, non-ASCII, punctuation) so the same value always
    produces the same clause.
    """
    code_point = ord(character)
    if code_point < 32 or code_point == 127:
        return "control"
    if character.isspace():
        return "space"
    if code_point > 126:
        return "non-ascii"
    if character not in _RULE_ID_TAIL_CHARS and character != _QUALIFIER_SEPARATOR:
        return "punctuation"
    return ""


def _rule_id_problem(value: str, what: str) -> str:
    """Why ``value`` is not a usable rule ID, as a fixed clause, or ``""``.

    ``value`` must already be a ``str``; ``what`` labels the field being read
    (``"rule_id"``, ``"allowed rule entry 2"``). The value is **never** echoed -
    not even one character of it - so an arbitrarily long or hostile value can
    only ever produce one of the fixed sentences above. Nothing is trimmed,
    lower-cased, escaped or otherwise repaired into a usable rule ID.
    """
    if not value:
        return f"{what} is empty: an empty string is not a rule ID."
    if len(value) > MAX_RULE_ID_LENGTH:
        return (
            f"{what} is longer than the supported maximum of "
            f"{MAX_RULE_ID_LENGTH} characters."
        )
    for character in value:
        character_class = _character_class(character)
        if character_class:
            return _CHARACTER_PROBLEMS[character_class].format(what=what)
    parts = value.split(_QUALIFIER_SEPARATOR)
    if len(parts) > 2:
        return (
            f"{what} contains more than one ':' separator: a rule ID is "
            "'<key>' or '<qualifier>:<key>'."
        )
    for part in parts:
        if not part or part[0] not in _RULE_ID_LEAD_CHARS:
            return (
                f"{what} is not a usable rule ID: every part of '<key>' or "
                "'<qualifier>:<key>' must be non-empty and must start with a "
                "letter or a digit."
            )
    return ""


def _is_rule_id(value: object) -> bool:
    """True when ``value`` is a string that is a usable rule ID, and nothing else.

    Deliberately string-only: ``True``, ``7``, ``1.5``, ``b"S1118"``, a ``Path``
    and every other object are not coerced with ``str()`` - they are simply not
    rule IDs.
    """
    return isinstance(value, str) and _rule_id_problem(value, "rule_id") == ""


# ---------------------------------------------------------------------------
# Reading and validating the allowlist configuration (pure, never repairs it)
# ---------------------------------------------------------------------------


def _policy_problem(policy: RuleAllowlistPolicy) -> Optional[str]:
    """Why ``policy`` is unusable, as a fixed clause, or ``None`` when usable.

    The allowlist is validated first, so its clause is decisive: shape, then
    size, then every entry in configuration order. Entries are never trimmed,
    normalised or coerced, a duplicate entry is an ambiguous configuration
    rather than an entry to drop silently, and the entry bound is enforced
    before a single entry is read.
    """
    value = policy.allowed_rules
    if value is None:
        return (
            "No allowed_rules tuple is configured, and None never means 'no "
            "rule is allowlisted', so an unusable allowlist is refused."
        )
    if not isinstance(value, tuple):
        return (
            "allowed_rules must be an immutable tuple of rule IDs, not "
            f"{type(value).__name__}: a list, set, mapping, string or iterator "
            "is mutable or unordered and therefore cannot state a curated "
            "allowlist."
        )
    if len(value) > MAX_ALLOWED_RULES:
        return (
            f"allowed_rules lists {len(value)} rule IDs, which exceeds the "
            f"supported maximum of {MAX_ALLOWED_RULES}: a configuration that "
            "large is an attempt to allow everything, not a curation."
        )
    seen: Set[str] = set()
    for index, item in enumerate(value, start=1):
        what = f"allowed rule entry {index}"
        if not isinstance(item, str):
            return f"{what} must be a rule ID string, not {type(item).__name__}."
        problem = _rule_id_problem(item, what)
        if problem:
            return problem
        if item in seen:
            return (
                f"allowed_rules lists '{item}' twice: a rule allowlisted twice "
                "is an ambiguous configuration, so it is refused instead of "
                "being deduplicated silently."
            )
        seen.add(item)
    return None


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAllowlistPolicy:
    """T28 configuration: exactly which Sonar rules may be fixed automatically.

    Attributes:
        allowed_rules: the rule IDs an automatic fix is approved for. Only an
            immutable ``tuple`` is accepted - a list, set, frozenset, mapping,
            string, bytes or iterator can be mutated or reordered *after* it was
            validated, so it is refused (``INVALID_POLICY``) rather than copied
            and trusted. ``None`` is refused too: an unconfigured allowlist
            never means "allow nothing in particular", and there is no hidden
            default entry. Every entry must be a usable rule ID (see
            :data:`MAX_RULE_ID_LENGTH`), no entry may repeat, and the tuple may
            hold at most :data:`MAX_ALLOWED_RULES` entries. The empty tuple is
            valid and maximally strict: it allows no rule at all, which is the
            default and the fail-closed choice.
    """

    allowed_rules: Tuple[str, ...] = DEFAULT_ALLOWED_RULES

    @property
    def refusal_reason(self) -> Optional[str]:
        """Why the configuration is unusable, or ``None`` when it is usable."""
        return _policy_problem(self)

    @property
    def is_valid(self) -> bool:
        """True only when the configuration states a usable allowlist."""
        return _policy_problem(self) is None

    @property
    def allowed_rule_keys(self) -> Tuple[str, ...]:
        """The configured rule IDs, verbatim, or ``()`` when unusable.

        The values are the *validated* entries in configuration order and are
        never rewritten: this is the exact identity every comparison in T28 uses
        (as membership of a set built from this tuple), which is why a rule that
        is merely *similar* to an entry is never matched. ``()`` is returned for
        an unusable configuration - which the evaluation reports as
        ``INVALID_POLICY``, never as "nothing is allowed".
        """
        if self.refusal_reason is not None:
            return ()
        return tuple(self.allowed_rules)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the configuration.

        Only *validated* rule IDs are published, in configuration order, so an
        unusable configuration is reported as ``None`` plus its refusal reason
        instead of echoing the entries that made it unusable. Rule IDs are not
        secrets, but they are still bounded here by validation.
        """
        keys = self.allowed_rule_keys
        is_valid = self.refusal_reason is None
        return {
            "policy_version": POLICY_VERSION,
            "allowed_rules": list(keys) if is_valid else None,
            "allowed_rule_count": len(keys) if is_valid else None,
            "maximum_supported_rules": MAX_ALLOWED_RULES,
            "maximum_rule_id_length": MAX_RULE_ID_LENGTH,
            "is_valid": is_valid,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class RuleAllowlistInput:
    """The caller-asserted rule identity T28 judges.

    T28 never repairs this record: an unusable value is refused, not fixed.

    Attributes:
        rule_id: the SonarQube rule key of the issue an automatic fix would
            address, as the caller states it - for example ``S1118`` or
            ``python:S1481``. T28 does not normalise it: a leading or trailing
            space, whitespace anywhere, a control character, a wildcard, a
            regex, a path, a URL, a shell fragment, a non-ASCII character, an
            empty string and an over-long value are all refused
            (``INVALID_RULE_ID``), ``None`` and every non-string are refused
            (``INVALID_INPUT``), and a usable identity that is simply not
            listed is refused (``RULE_NOT_ALLOWED``). Only the exact string is
            ever compared.
    """

    rule_id: Optional[str] = None

    @property
    def is_usable_rule_id(self) -> bool:
        """True only when ``rule_id`` is a usable rule ID string."""
        return _is_rule_id(self.rule_id)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the caller's fact.

        Only a *validated* rule ID is published; every unusable value is
        reported as ``None`` with ``is_usable_rule_id`` false, so caller-authored
        text (a path, a URL, a shell fragment, an over-long value) can never be
        serialized or logged through this record.
        """
        rule_id = self.rule_id
        usable = _is_rule_id(rule_id)
        return {
            "rule_id": rule_id if usable else None,
            "is_usable_rule_id": usable,
        }


@dataclass(frozen=True)
class RuleAllowlistEvaluation:
    """The deterministic, fail-closed verdict for one policy and rule identity.

    Attributes:
        policy_version: the T28 contract version that produced the verdict.
        status: why the rule was allowed or refused.
        decision: ``ALLOW`` only for :data:`ALLOWED_STATUSES`, else ``REFUSE``.
        rule_id: the caller's rule ID, verbatim, but only when it is a *usable*
            rule ID; every malformed or absent value is reported as ``None``, so
            this field can never carry an arbitrary caller string.
        diagnostic_code: the stable machine token of ``status`` - one of
            :data:`DIAGNOSTIC_CODES` - so a report, a log or an alert can be
            grouped without parsing prose.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its fail-closed
            consequence).
        policy: the configuration the verdict used, so it explains itself.

    ``can_auto_fix`` is the only permission flag and is *derived* from
    ``decision``, which is derived from ``status``: a status outside
    :data:`ALLOWED_STATUSES` cannot report permission, and an allowed verdict
    cannot carry a refusal status. The invariants that must hold are pinned by
    tests, for example ``status is ALLOWED`` implies ``decision is ALLOW`` and
    ``can_auto_fix is True``.
    """

    policy_version: str
    status: RuleAllowlistStatus
    decision: RuleAllowlistDecision
    rule_id: Optional[str]
    diagnostic_code: str
    reason: str
    reasons: Tuple[str, ...]
    policy: RuleAllowlistPolicy

    @property
    def can_auto_fix(self) -> bool:
        """True only when the exact rule ID is explicitly allowlisted.

        This is the permission flag: it is derived from ``decision`` and never
        stored separately, so no construction, no serialization and no caller
        can turn a refusal into permission.
        """
        return self.decision is RuleAllowlistDecision.ALLOW

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict.

        Only *validated* values are published: a malformed or absent rule ID is
        reported as ``None``, and the only caller-authored value that can appear
        anywhere is a rule ID that already passed the grammar and the length
        bound. T28 emits no command, no argv, no environment and no issue text,
        so nothing in this mapping can be executed.
        """
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "decision": self.decision.value,
            "can_auto_fix": self.can_auto_fix,
            "rule_id": self.rule_id,
            "diagnostic_code": self.diagnostic_code,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "policy": self.policy.as_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's records)
# ---------------------------------------------------------------------------


def _outcome(
    policy: RuleAllowlistPolicy,
    status: RuleAllowlistStatus,
    clause: str,
    rule_id: Optional[str] = None,
) -> RuleAllowlistEvaluation:
    """Assemble the verdict from the deciding status and the validated rule ID.

    The rule ID is echoed only after a second, independent check that it is a
    usable rule ID, so it is *structurally* impossible for this module to
    publish an unvalidated caller string: a malformed value passed here by
    mistake is reported as ``None``, never echoed. ``clause`` is either a fixed
    sentence or one built from this module's own labels, and the rationale trail
    is always ``(reason, consequence)`` so nothing can be appended to it.
    """
    echoed = rule_id if _is_rule_id(rule_id) else None
    verb = "Allowed" if status in ALLOWED_STATUSES else "Refused"
    if echoed is None:
        reason = f"{verb} ({status.value}): {clause}."
    else:
        reason = f"{verb} ({status.value}) for rule '{echoed}': {clause}."
    return RuleAllowlistEvaluation(
        policy_version=POLICY_VERSION,
        status=status,
        decision=(
            RuleAllowlistDecision.ALLOW
            if status in ALLOWED_STATUSES
            else RuleAllowlistDecision.REFUSE
        ),
        rule_id=echoed,
        diagnostic_code=DIAGNOSTIC_CODES[status],
        reason=reason,
        reasons=(reason, _STATUS_CONSEQUENCES[status]),
        policy=policy,
    )


def _input_problem(rule_id: object) -> str:
    """Why ``rule_id`` is not a rule ID *string*, as a fixed clause.

    ``None`` (nothing was stated) and a value of another type are different
    problems with different sentences, and both are refusals: absence is neither
    a rule identity nor a rule that is merely missing from the allowlist, and
    ``True``, ``7``, ``1.5``, ``b"S1118"``, a ``Path`` and every other object
    are never coerced into one.
    """
    if rule_id is None:
        return (
            "No rule_id was supplied, so there is no rule to judge: absence is "
            "not a rule identity and is never read as one"
        )
    return (
        "rule_id must be a rule ID string, not "
        f"{type(rule_id).__name__}, and nothing is coerced into a rule ID"
    )


def evaluate_rule_allowlist(
    *,
    policy: RuleAllowlistPolicy,
    rule_input: RuleAllowlistInput,
) -> RuleAllowlistEvaluation:
    """Decide whether one Sonar rule may be fixed automatically.

    Pure and deterministic: it reads only its two arguments, writes nothing,
    remembers nothing and counts nothing, and it performs no filesystem,
    network, subprocess or Git operation. The check order is :data:`PRECEDENCE`,
    and every uncertainty is a refusal: an unreadable allowlist refuses before
    any rule is read, and an unusable rule identity refuses before it is looked
    up, so nothing but an exact allowlist match can authorise a fix.

    Args:
        policy: the operator's :class:`RuleAllowlistPolicy` - the exact rule IDs
            an automatic fix is approved for.
        rule_input: the caller-asserted :class:`RuleAllowlistInput` - the rule ID
            of the issue an automatic fix would address, which T28 never
            discovers or normalises.

    Returns:
        An immutable :class:`RuleAllowlistEvaluation`. Evaluating the same pair
        again returns an equal verdict, and no caller value is mutated.

    Raises:
        TypeError: ``policy`` is not a :class:`RuleAllowlistPolicy`, or
            ``rule_input`` is not a :class:`RuleAllowlistInput`. That is a caller
            error, not a rule problem - every rule problem is a refusal in the
            returned evaluation.
    """
    if not isinstance(policy, RuleAllowlistPolicy):
        raise TypeError(
            "policy must be a RuleAllowlistPolicy, not "
            f"{type(policy).__name__}."
        )
    if not isinstance(rule_input, RuleAllowlistInput):
        raise TypeError(
            "rule_input must be a RuleAllowlistInput, not "
            f"{type(rule_input).__name__}."
        )

    # 1. An allowlist T28 cannot read states no approved rule, so nothing is
    #    judged - and, in particular, nothing can be allowed.
    problem = _policy_problem(policy)
    if problem is not None:
        return _outcome(policy, RuleAllowlistStatus.INVALID_POLICY, problem)

    rule_id = rule_input.rule_id

    # 2. A record that carries no rule *string* has nothing to look up, so it is
    #    refused before any content is considered.
    if not isinstance(rule_id, str):
        return _outcome(
            policy,
            RuleAllowlistStatus.INVALID_INPUT,
            _input_problem(rule_id),
        )

    # 3. A rule string that is not a usable rule identity is refused: never
    #    trimmed, never normalised, never read as a prefix or a pattern.
    problem = _rule_id_problem(rule_id, "rule_id")
    if problem:
        return _outcome(policy, RuleAllowlistStatus.INVALID_RULE_ID, problem)

    # 4. The one and only permission test: exact membership of the validated
    #    configuration. The set is built from the validated tuple, and the
    #    comparison is `in` on that set - never a prefix, suffix, substring,
    #    case-folded or pattern comparison - and this module imports no matching
    #    library, so no other comparison is even expressible here.
    allowed_rule_ids = frozenset(policy.allowed_rule_keys)
    if rule_id in allowed_rule_ids:
        return _outcome(
            policy,
            RuleAllowlistStatus.ALLOWED,
            _STATUS_REASONS[RuleAllowlistStatus.ALLOWED],
            rule_id=rule_id,
        )

    # 5. A usable rule identity that is not listed is denied: the default is
    #    deny, and nothing here infers approval from anything else.
    return _outcome(
        policy,
        RuleAllowlistStatus.RULE_NOT_ALLOWED,
        _STATUS_REASONS[RuleAllowlistStatus.RULE_NOT_ALLOWED],
        rule_id=rule_id,
    )


__all__: Sequence[str] = (
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
