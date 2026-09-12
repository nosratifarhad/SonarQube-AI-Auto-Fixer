"""T27 - uncertainty policy: never commit or push when the evidence is unsure.

Purpose
-------
T27 answers exactly one question, once per stage that mutates a repository:

    "is the evidence that is supposed to authorise this mutation
     *explicitly confirmed*?"

It is a **central policy layer only**: it discovers nothing, authenticates
nothing, mutates nothing, and it touches no network, no Git, no SonarQube, no
Codex and no file. It is a small, auditable contract of two immutable records:

* :class:`UncertaintyInput` - the caller-asserted phase plus one
  :class:`EvidenceItem` per evidence dimension the caller knows about,
* :class:`UncertaintyEvaluation` - the deterministic, fail-closed verdict.

The one rule
------------
A mutation is allowed **only** when every dimension the phase requires is
:attr:`EvidenceState.CONFIRMED`. There is no weighting, no scoring, no
threshold, no confidence value to configure and no "probably fine" path. A
dimension that is missing, unknown, unverified, ambiguous, negative,
contradictory or invalid refuses the mutation. Absence is never read as a
confirmation, and a doubt is never resolved in favour of the mutation.

The four phases
---------------
The same rule is asked at four points, and every point requires everything the
earlier points required (each phase is a prefix of the next one):

================================ =========================================
Phase                            Asked before
================================ =========================================
:attr:`MutationPhase.PRE_COMMIT`  the T20 commit
:attr:`MutationPhase.POST_COMMIT` the T21 push, with the commit re-verified
:attr:`MutationPhase.PRE_PUSH`    the T21 push, with the branch re-verified
:attr:`MutationPhase.POST_PUSH`   ending the run, with the push verified
================================ =========================================

Evidence model
--------------
Evidence is **explicit and typed**. The caller states one
:class:`EvidenceItem` (a dimension plus an :class:`EvidenceState`) per
dimension, and a dimension the caller omits is
:attr:`EvidenceState.MISSING` - it is never assumed to be confirmed. Nothing is
read for its truthiness, ``None`` never means "confirmed", and an object whose
``__bool__`` is clever is just an unusable entry (``INVALID_INPUT``).

Negative is not uncertain
-------------------------
:attr:`EvidenceState.NEGATIVE` (a *known* failure, such as ``STILL_OPEN`` or
``TESTS_FAILED``) is deliberately separate from the doubt states
(:attr:`EvidenceState.MISSING`, :attr:`EvidenceState.UNKNOWN`,
:attr:`EvidenceState.UNVERIFIED`, :attr:`EvidenceState.AMBIGUOUS`). Both
refuse; the reported status keeps them apart, and
:attr:`UncertaintyEvaluation.requires_review` is True only for doubt. A known
failure is never downgraded to "unknown" and a doubt is never upgraded to a
failure.

Diagnostics
-----------
An :class:`EvidenceItem` may carry a ``code``: a bounded machine token (letters,
digits and ``-``, ``_``, ``.``, ``:``; at most ``48`` characters) that names the
*source* condition - ``still-open`` or ``sha-mismatch``, for example. A code is
echoed for diagnostics and is **never read** when deciding anything: it cannot
change a status, a decision or the blocking dimension, and a code that is not a
bounded token makes the whole input unusable instead of being trimmed or
ignored. No free-form message is accepted anywhere, and no caller-authored value
other than a valid code is ever published.

Contradiction detection
-----------------------
Contradictions are refused, never resolved optimistically. T27 detects a
contradiction when

* a required dimension is explicitly
  :attr:`EvidenceState.CONTRADICTORY`, or
* a *confirmed* claim is paired with a required proof that is present but not
  confirmed - ``commit-result`` confirmed while ``commit-identity`` is
  unverified, for example (see :data:`CONTRADICTION_PAIRS`).

A proof that is *absent* is reported as
:attr:`UncertaintyStatus.MISSING_EVIDENCE` instead (see :data:`PRECEDENCE`),
because a missing dimension is a gap in the call, while a present-but-
unconfirmed proof contradicts the confirmed claim that depends on it.

Trust boundary
--------------
The input is **caller-asserted**. T27 authenticates nothing: it does not read
Git, run the tests, query SonarQube or check that the states it is handed are
true. It is a policy boundary, not an evidence-authentication boundary: a
caller that misstates a state, or that simply does not call T27 at all, is not
detected here. T27 exists so that a future orchestration layer can no longer
*accidentally* commit or push on partial, absent or contradictory evidence.

Determinism and purity
----------------------
:data:`PRECEDENCE` is the authoritative evaluation order, and the blocking
dimension is always the first required dimension (in the phase's documented
order) that carries the decisive state, so overlapping refusals are
deterministic. The verdict is a pure function of the input record: no clock,
environment value, random source, cache, counter or mutable module state is
read; no filesystem, network, subprocess or Git operation is ever performed;
the same input always produces an equal record; and the caller's records are
never mutated.

Fail closed
-----------
=================================== =======================================
Input                               Result
=================================== =======================================
phase is not a ``MutationPhase``    refused: ``INVALID_POLICY``
evidence is not a tuple of          refused: ``INVALID_INPUT``
``EvidenceItem`` records
duplicate, unknown or ill-formed    refused: ``INVALID_INPUT``
entry
required dimension omitted          refused: ``MISSING_EVIDENCE``
required dimension *known* bad      refused: ``NEGATIVE_EVIDENCE``
required dimension contradicted     refused: ``CONTRADICTORY_EVIDENCE``
required dimension unverified       refused: ``UNVERIFIED_EVIDENCE``
required dimension unknown          refused: ``UNKNOWN_EVIDENCE``
required dimension ambiguous        refused: ``AMBIGUOUS_EVIDENCE``
every required dimension confirmed  allowed: ``ALLOWED``
=================================== =======================================

Not wired
---------
T27 is a library with no caller: ``main.py`` is byte-for-byte unchanged, T19-T26
are untouched, and this module imports the Python standard library only. It has
no dependency on any other module in this repository, so nothing about the
existing pipeline can change because of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Sequence, Tuple

#: Version of the T27 policy contract.
POLICY_VERSION = "t27.1"

#: The longest accepted diagnostic code. A code is a machine token that names a
#: source condition; it is never prose and never influences a decision.
_CODE_MAX_LENGTH = 48

#: The characters a diagnostic code may use: letters, digits and the four
#: separators that machine tokens use. Whitespace, quotes and every control
#: character are excluded, so a code can neither break a log line nor smuggle a
#: free-form message into a report.
_CODE_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_.:"
)


class EvidenceState(Enum):
    """How trustworthy one evidence dimension is (fail closed)."""

    __test__ = False

    #: The dimension is explicitly, positively established.
    CONFIRMED = "confirmed"
    #: The dimension was not supplied at all. Absence is never confirmation.
    MISSING = "missing"
    #: The dimension was reported but not verified.
    UNVERIFIED = "unverified"
    #: The dimension was not established.
    UNKNOWN = "unknown"
    #: The dimension is ambiguous.
    AMBIGUOUS = "ambiguous"
    #: The dimension is a *known* negative result, not merely uncertain.
    NEGATIVE = "negative"
    #: The dimension contradicts itself or the claim that depends on it.
    CONTRADICTORY = "contradictory"
    #: The stated value is not usable at all.
    INVALID = "invalid"


class EvidenceDimension(Enum):
    """The dimensions a mutation can rest on, in policy order.

    The declaration order is the order T27 uses to pick the *blocking*
    dimension, so a refusal with several candidates is always reported against
    the same (earliest) dimension.
    """

    __test__ = False

    #: T19's final issue outcome (``FIXED`` is the confirming value).
    ISSUE_OUTCOME = "issue-outcome"
    #: T10/T11: Codex ran and produced an analysable result.
    CODEX_EXECUTION = "codex-execution"
    #: T13: the change stayed inside the allowed scope.
    CHANGE_SCOPE = "change-scope"
    #: T14/T15: the project test run passed.
    TESTS = "tests"
    #: T16/T17: the SonarQube re-analysis completed.
    SONAR_ANALYSIS = "sonar-analysis"
    #: T18: the original issue is reliably absent from the new analysis.
    ISSUE_VERIFICATION = "issue-verification"
    #: T17-T18: the new analysis is the one the fix produced.
    ANALYSIS_CORRELATION = "analysis-correlation"
    #: The lifecycle stages agree with one another.
    CROSS_STAGE_CONSISTENCY = "cross-stage-consistency"
    #: T26: the target branch is safe to mutate.
    BRANCH_PROTECTION = "branch-protection"
    #: T20: the commit was created.
    COMMIT_RESULT = "commit-result"
    #: T20: the recorded commit identity is the one that was created.
    COMMIT_IDENTITY = "commit-identity"
    #: T20: the repository state after the commit is the expected one.
    COMMIT_REPOSITORY = "commit-repository"
    #: T20-T21: the source and destination branches agree.
    BRANCH_CONSISTENCY = "branch-consistency"
    #: T21: the push was created.
    PUSH_RESULT = "push-result"
    #: T21: the remote tip is the expected one.
    REMOTE_TIP = "remote-tip"
    #: T21: the expected commit is the one that was pushed.
    EXPECTED_COMMIT = "expected-commit"


class MutationPhase(Enum):
    """The point of the lifecycle at which T27 is asked (fail closed)."""

    __test__ = False

    #: Asked before the T20 commit; requires the pre-commit evidence only.
    PRE_COMMIT = "pre-commit"
    #: Asked before the T21 push; also requires the commit evidence.
    POST_COMMIT = "post-commit"
    #: Asked before the T21 push; also requires branch consistency.
    PRE_PUSH = "pre-push"
    #: Asked after the T21 push; also requires the push evidence.
    POST_PUSH = "post-push"


class UncertaintyStatus(Enum):
    """Why a mutation was allowed or refused (fail closed)."""

    __test__ = False

    #: Allowed: every required dimension is confirmed.
    ALLOWED = "allowed"
    #: Refused: the phase does not select a mutation phase T27 knows.
    INVALID_POLICY = "invalid-policy"
    #: Refused: the evidence record itself is not usable.
    INVALID_INPUT = "invalid-input"
    #: Refused: a required dimension was not supplied (absence).
    MISSING_EVIDENCE = "missing-evidence"
    #: Refused: a required dimension contradicts itself or its claim.
    CONTRADICTORY_EVIDENCE = "contradictory-evidence"
    #: Refused: a required dimension is a *known* negative result.
    NEGATIVE_EVIDENCE = "negative-evidence"
    #: Refused: a required dimension was not established (doubt).
    UNKNOWN_EVIDENCE = "unknown-evidence"
    #: Refused: a required dimension was reported but not verified (doubt).
    UNVERIFIED_EVIDENCE = "unverified-evidence"
    #: Refused: a required dimension is ambiguous (doubt).
    AMBIGUOUS_EVIDENCE = "ambiguous-evidence"


class UncertaintyDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    ALLOW = "allow"
    REFUSE = "refuse"


#: The statuses that allow a mutation. A status outside this tuple always yields
#: :attr:`UncertaintyDecision.REFUSE` and can never be bypassed.
ALLOWED_STATUSES: Tuple[UncertaintyStatus, ...] = (
    UncertaintyStatus.ALLOWED,
)

#: The statuses that report a *doubt* rather than a known failure. They refuse
#: exactly like every other refusal, but
#: :attr:`UncertaintyEvaluation.requires_review` is True for them, so an
#: operator can tell "not proven either way" apart from "proven bad".
REVIEW_STATUSES: Tuple[UncertaintyStatus, ...] = (
    UncertaintyStatus.MISSING_EVIDENCE,
    UncertaintyStatus.UNKNOWN_EVIDENCE,
    UncertaintyStatus.UNVERIFIED_EVIDENCE,
    UncertaintyStatus.AMBIGUOUS_EVIDENCE,
)

#: The evaluation order: the first status whose condition holds decides the
#: result. It is deliberately "preconditions first": a phase that selects no
#: policy and an evidence record that cannot be read both refuse before any
#: dimension is judged, and an absent dimension refuses before a doubtful one -
#: so an unreadable call can never be reported as a judgement about the fix.
PRECEDENCE: Tuple[UncertaintyStatus, ...] = (
    UncertaintyStatus.INVALID_POLICY,
    UncertaintyStatus.INVALID_INPUT,
    UncertaintyStatus.MISSING_EVIDENCE,
    UncertaintyStatus.CONTRADICTORY_EVIDENCE,
    UncertaintyStatus.NEGATIVE_EVIDENCE,
    UncertaintyStatus.UNKNOWN_EVIDENCE,
    UncertaintyStatus.UNVERIFIED_EVIDENCE,
    UncertaintyStatus.AMBIGUOUS_EVIDENCE,
    UncertaintyStatus.ALLOWED,
)

#: Every dimension, in policy order. Used only to look a dimension up
#: deterministically and to publish evidence in a canonical order.
_DIMENSION_ORDER: Tuple[EvidenceDimension, ...] = tuple(EvidenceDimension)

#: The pre-commit evidence, in policy order: everything a T20 commit rests on.
_PRE_COMMIT_DIMENSIONS: Tuple[EvidenceDimension, ...] = (
    EvidenceDimension.ISSUE_OUTCOME,
    EvidenceDimension.CODEX_EXECUTION,
    EvidenceDimension.CHANGE_SCOPE,
    EvidenceDimension.TESTS,
    EvidenceDimension.SONAR_ANALYSIS,
    EvidenceDimension.ISSUE_VERIFICATION,
    EvidenceDimension.ANALYSIS_CORRELATION,
    EvidenceDimension.CROSS_STAGE_CONSISTENCY,
    EvidenceDimension.BRANCH_PROTECTION,
)

#: The commit evidence T20 produces and T21 re-verifies, in policy order.
_COMMIT_DIMENSIONS: Tuple[EvidenceDimension, ...] = (
    EvidenceDimension.COMMIT_RESULT,
    EvidenceDimension.COMMIT_IDENTITY,
    EvidenceDimension.COMMIT_REPOSITORY,
)

#: The push evidence T21 produces, in policy order.
_PUSH_DIMENSIONS: Tuple[EvidenceDimension, ...] = (
    EvidenceDimension.PUSH_RESULT,
    EvidenceDimension.REMOTE_TIP,
    EvidenceDimension.EXPECTED_COMMIT,
)

#: Everything that must be confirmed before the T21 push, in policy order.
_PRE_PUSH_DIMENSIONS: Tuple[EvidenceDimension, ...] = (
    _PRE_COMMIT_DIMENSIONS
    + _COMMIT_DIMENSIONS
    + (EvidenceDimension.BRANCH_CONSISTENCY,)
)

#: The fixed required-evidence policy: phase -> every dimension that phase
#: requires, in policy order. It is a *fixed* table: there is no configuration
#: hook, no way to shorten a phase and no way to make a phase require less, so
#: the contract cannot be weakened from the outside. It is wrapped in a
#: read-only mapping so it cannot be altered at runtime either.
REQUIRED_DIMENSIONS: Mapping[
    MutationPhase, Tuple[EvidenceDimension, ...]
] = MappingProxyType(
    {
        MutationPhase.PRE_COMMIT: _PRE_COMMIT_DIMENSIONS,
        MutationPhase.POST_COMMIT: (
            _PRE_COMMIT_DIMENSIONS + _COMMIT_DIMENSIONS
        ),
        MutationPhase.PRE_PUSH: _PRE_PUSH_DIMENSIONS,
        MutationPhase.POST_PUSH: _PRE_PUSH_DIMENSIONS + _PUSH_DIMENSIONS,
    }
)

#: The claims T27 cross-checks, as ``(claim, proof)``. Within a phase that
#: requires both, a *confirmed* claim whose required proof is present but not
#: confirmed is a contradiction: a success cannot be confirmed while the
#: evidence that would make it a success is unverified, unknown, ambiguous or
#: negative.
CONTRADICTION_PAIRS: Tuple[
    Tuple[EvidenceDimension, EvidenceDimension], ...
] = (
    (EvidenceDimension.ISSUE_OUTCOME, EvidenceDimension.ISSUE_VERIFICATION),
    (EvidenceDimension.ISSUE_OUTCOME, EvidenceDimension.ANALYSIS_CORRELATION),
    (EvidenceDimension.COMMIT_RESULT, EvidenceDimension.COMMIT_IDENTITY),
    (EvidenceDimension.COMMIT_RESULT, EvidenceDimension.COMMIT_REPOSITORY),
    (EvidenceDimension.PUSH_RESULT, EvidenceDimension.REMOTE_TIP),
    (EvidenceDimension.PUSH_RESULT, EvidenceDimension.EXPECTED_COMMIT),
)


# ---------------------------------------------------------------------------
# The fixed wording and the input checks (pure, never repairs anything)
# ---------------------------------------------------------------------------

#: Why an evidence record is unusable. Each clause is *static text*: no
#: caller-authored value, and not even a type name, can reach a reason.
_EVIDENCE_NOT_TUPLE = (
    "the evidence field is not an immutable tuple of EvidenceItem records"
)
_EVIDENCE_NOT_ITEM = "an evidence entry is not an EvidenceItem record"
_EVIDENCE_BAD_DIMENSION = (
    "an evidence entry names a dimension T27 does not know"
)
_EVIDENCE_BAD_STATE = "an evidence entry states a state T27 does not know"
_EVIDENCE_BAD_CODE = (
    "an evidence entry carries a diagnostic code that is not a bounded "
    "machine token"
)
_EVIDENCE_DUPLICATE = (
    "the same evidence dimension is stated more than once, so its value would "
    "be ambiguous"
)
_PHASE_NOT_A_PHASE = "the phase does not select a mutation phase T27 knows"

#: The decisive clause each status contributes to its reason.
_STATUS_REASONS: Mapping[UncertaintyStatus, str] = MappingProxyType(
    {
        UncertaintyStatus.INVALID_POLICY: _PHASE_NOT_A_PHASE,
        UncertaintyStatus.INVALID_INPUT: (
            "the evidence record is not usable, so nothing in it can be trusted"
        ),
        UncertaintyStatus.MISSING_EVIDENCE: (
            "the phase requires this dimension and it was not supplied, and "
            "absence is never read as confirmation"
        ),
        UncertaintyStatus.CONTRADICTORY_EVIDENCE: (
            "the evidence contradicts itself, and T27 never resolves a "
            "contradiction in favour of the mutation"
        ),
        UncertaintyStatus.NEGATIVE_EVIDENCE: (
            "the evidence is a *known* negative result, which is not the same "
            "thing as doubt"
        ),
        UncertaintyStatus.UNKNOWN_EVIDENCE: (
            "the dimension was not established, which is not a confirmation"
        ),
        UncertaintyStatus.UNVERIFIED_EVIDENCE: (
            "the dimension was reported but not verified, which is not a "
            "confirmation"
        ),
        UncertaintyStatus.AMBIGUOUS_EVIDENCE: (
            "the dimension is ambiguous, and T27 never breaks a tie in favour "
            "of the mutation"
        ),
        UncertaintyStatus.ALLOWED: (
            "every dimension this phase requires is confirmed"
        ),
    }
)

#: The fail-closed consequence T27 attaches to each status, as the second
#: (contextual) reason. It is fixed text and repeats the rule, so a refusal can
#: never read as a tolerance.
_STATUS_CONSEQUENCES: Mapping[UncertaintyStatus, str] = MappingProxyType(
    {
        UncertaintyStatus.INVALID_POLICY: (
            "No evidence is judged: an unusable policy refuses before "
            "anything else is considered."
        ),
        UncertaintyStatus.INVALID_INPUT: (
            "No evidence is judged: an unusable evidence record is refused, "
            "never repaired, trimmed or ignored."
        ),
        UncertaintyStatus.MISSING_EVIDENCE: (
            "The mutation is refused: a dimension that was never supplied "
            "proves nothing."
        ),
        UncertaintyStatus.CONTRADICTORY_EVIDENCE: (
            "The mutation is refused: T27 never picks the optimistic side of "
            "a contradiction."
        ),
        UncertaintyStatus.NEGATIVE_EVIDENCE: (
            "The mutation is refused and reported as a known failure, not as "
            "doubt."
        ),
        UncertaintyStatus.UNKNOWN_EVIDENCE: (
            "The mutation is refused: this is doubt, not a failure, and every "
            "doubt refuses."
        ),
        UncertaintyStatus.UNVERIFIED_EVIDENCE: (
            "The mutation is refused: this is doubt, not a failure, and every "
            "doubt refuses."
        ),
        UncertaintyStatus.AMBIGUOUS_EVIDENCE: (
            "The mutation is refused: this is doubt, not a failure, and every "
            "doubt refuses."
        ),
        UncertaintyStatus.ALLOWED: (
            "T27 allows only because every required dimension is confirmed; it "
            "proves nothing beyond that."
        ),
    }
)


def _is_code(value: object) -> bool:
    """True when ``value`` is a usable diagnostic code.

    An empty string is the default and means "no code"; a non-empty code must
    be a bounded machine token. A value that is not a string, is too long or
    contains a character outside :data:`_CODE_ALPHABET` is refused - the code
    is never trimmed, truncated or escaped into something usable.
    """
    if not isinstance(value, str):
        return False
    if not value:
        return True
    if len(value) > _CODE_MAX_LENGTH:
        return False
    return all(character in _CODE_ALPHABET for character in value)


def _input_problem(evidence: object) -> Optional[str]:
    """Why ``evidence`` is not a usable tuple of items, or ``None``.

    Read-only and total: it inspects types and identity only, never truthiness,
    and it returns one of the static clauses above, so no caller-authored value
    can reach a reason. A duplicate dimension is an unusable record, not a
    "last one wins" conflict: two states for one dimension would make the
    decision depend on the order of the tuple.
    """
    if not isinstance(evidence, tuple):
        return _EVIDENCE_NOT_TUPLE
    seen: Dict[EvidenceDimension, bool] = {}
    for item in evidence:
        if not isinstance(item, EvidenceItem):
            return _EVIDENCE_NOT_ITEM
        if not isinstance(item.dimension, EvidenceDimension):
            return _EVIDENCE_BAD_DIMENSION
        if not isinstance(item.state, EvidenceState):
            return _EVIDENCE_BAD_STATE
        if not _is_code(item.code):
            return _EVIDENCE_BAD_CODE
        if item.dimension in seen:
            return _EVIDENCE_DUPLICATE
        seen[item.dimension] = True
    return None


def _canonical(
    evidence: Tuple[EvidenceItem, ...]
) -> Tuple[EvidenceItem, ...]:
    """The validated items in :data:`_DIMENSION_ORDER` (a total, stable order).

    Duplicates are impossible here (``_input_problem`` refused them), so the
    order is total, and the published evidence never depends on the order in
    which the caller happened to build its tuple.
    """
    position = {
        dimension: index for index, dimension in enumerate(_DIMENSION_ORDER)
    }
    return tuple(sorted(evidence, key=lambda item: position[item.dimension]))


def _first_with_state(
    required: Tuple[EvidenceDimension, ...],
    supplied: Mapping[EvidenceDimension, EvidenceState],
    state: EvidenceState,
) -> Optional[EvidenceDimension]:
    """The first required dimension (in policy order) whose state is ``state``.

    A required dimension that is absent from ``supplied`` counts as
    :attr:`EvidenceState.MISSING`, so absence is never silently skipped.
    """
    for dimension in required:
        if supplied.get(dimension, EvidenceState.MISSING) is state:
            return dimension
    return None


def _broken_pair(
    required: Tuple[EvidenceDimension, ...],
    supplied: Mapping[EvidenceDimension, EvidenceState],
) -> Optional[Tuple[EvidenceDimension, EvidenceDimension]]:
    """The first confirmed claim whose required proof is not confirmed.

    Only pairs whose *both* dimensions this phase requires are considered, so
    the check never invents a requirement. An absent proof is not reported here
    (it is ``MISSING_EVIDENCE``, which precedes contradiction in
    :data:`PRECEDENCE`), and neither is an explicit contradiction, which is
    reported against the dimension that carries it.
    """
    required_set = frozenset(required)
    for claim, proof in CONTRADICTION_PAIRS:
        if claim not in required_set or proof not in required_set:
            continue
        if supplied.get(claim) is not EvidenceState.CONFIRMED:
            continue
        if supplied.get(proof) is not EvidenceState.CONFIRMED:
            return claim, proof
    return None


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    """One caller-asserted evidence dimension and the state it is in.

    Attributes:
        dimension: the :class:`EvidenceDimension` this item speaks about.
        state: the :class:`EvidenceState` the caller asserts for that
            dimension. Nothing is inferred: an item that does not state a
            known state is not "probably confirmed", it makes the whole record
            unusable (``INVALID_INPUT``).
        code: an optional, bounded machine token that names the *source*
            condition (``still-open``, ``sha-mismatch``). It is echoed for
            diagnostics only: it is never compared, never trusted and never
            allowed to change a status or a decision. The default empty string
            means "no code".
    """

    dimension: EvidenceDimension
    state: EvidenceState
    code: str = ""

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of one evidence item.

        Only *usable* values are published: an unknown dimension or state is
        reported as ``None`` and an unusable code as ``None``, so an ill-formed
        record never leaks the value that made it ill-formed.
        """
        dimension = (
            self.dimension
            if isinstance(self.dimension, EvidenceDimension)
            else None
        )
        state = self.state if isinstance(self.state, EvidenceState) else None
        code = self.code if _is_code(self.code) else None
        return {
            "dimension": dimension.value if dimension is not None else None,
            "state": state.value if state is not None else None,
            "code": code,
            "is_confirmed": state is EvidenceState.CONFIRMED,
        }


@dataclass(frozen=True)
class UncertaintyInput:
    """The caller-asserted phase plus the evidence the caller can state.

    T27 never repairs this record: an unusable phase or an unusable evidence
    field is refused, not fixed. Both fields are **caller-asserted** - see the
    trust boundary in the module docstring.

    Attributes:
        phase: the :class:`MutationPhase` being authorised. ``None`` and every
            non-:class:`MutationPhase` value are refused (``INVALID_POLICY``):
            T27 cannot judge a mutation whose required-evidence set it cannot
            establish.
        evidence: a tuple of :class:`EvidenceItem` records. Only a ``tuple`` is
            accepted - a list, a set, a mapping, ``None`` and any other type
            are refused (``INVALID_INPUT``), because a mutable or unordered
            container makes the input depend on state T27 does not control. A
            dimension that is not mentioned is simply absent, which is
            :attr:`EvidenceState.MISSING` (never confirmed).
    """

    phase: Optional[MutationPhase] = None
    evidence: Tuple[EvidenceItem, ...] = ()

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the caller's facts.

        Only *validated* items are published, and ``evidence_is_usable`` states
        whether the record as a whole could be read at all (it is exactly the
        check the evaluation performs). An ill-formed record is therefore
        reported as ``[]`` + ``False`` rather than by echoing the values that
        made it ill-formed.
        """
        phase = self.phase if isinstance(self.phase, MutationPhase) else None
        raw = self.evidence
        usable = _input_problem(raw) is None
        items = []
        if isinstance(raw, tuple):
            for item in raw:
                if isinstance(item, EvidenceItem):
                    items.append(item.as_dict())
        return {
            "phase": phase.value if phase is not None else None,
            "evidence": items,
            "evidence_is_usable": usable,
        }


@dataclass(frozen=True)
class UncertaintyEvaluation:
    """The deterministic, fail-closed verdict for one phase and evidence record.

    Attributes:
        policy_version: the T27 contract version that produced the verdict.
        phase: the validated phase, or ``None`` when the caller's phase was
            unusable.
        status: why the mutation was allowed or refused.
        decision: ``ALLOW`` only for :data:`ALLOWED_STATUSES`, else ``REFUSE``.
        blocking_dimension: the required dimension that decided the verdict, or
            ``None`` when no dimension was judged (``INVALID_POLICY`` and
            ``INVALID_INPUT``).
        blocking_state: the state of ``blocking_dimension``, or ``None`` for the
            same reason.
        required_dimensions: every dimension the phase requires, in policy
            order - the exact contract the verdict was measured against.
        evidence: the validated items the verdict read, in
            :data:`_DIMENSION_ORDER`, or ``()`` when the record was unusable.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its
            fail-closed consequence).

    ``blocking_dimension``, ``blocking_state`` and ``evidence`` are *facts about
    the evaluation*, not restatements of ``status``: several dimensions can
    carry the same decisive state, and :data:`PRECEDENCE` names only the first
    applicable refusal. The invariants that must hold are pinned by tests, for
    example ``status is ALLOWED`` implies ``decision is ALLOW`` and every
    required dimension is ``CONFIRMED``.
    """

    policy_version: str
    phase: Optional[MutationPhase]
    status: UncertaintyStatus
    decision: UncertaintyDecision
    blocking_dimension: Optional[EvidenceDimension]
    blocking_state: Optional[EvidenceState]
    required_dimensions: Tuple[EvidenceDimension, ...]
    evidence: Tuple[EvidenceItem, ...]
    reason: str
    reasons: Tuple[str, ...]

    @property
    def is_allowed(self) -> bool:
        """True only when the mutation is authorised by confirmed evidence.

        This is the "allowed" flag, derived from ``decision`` and never stored
        separately, so a refusal can never report ``allowed == True`` and an
        allowed verdict can never carry a refusal status.
        """
        return self.decision is UncertaintyDecision.ALLOW

    @property
    def is_contradiction(self) -> bool:
        """True when the verdict is a contradiction refusal."""
        return self.status is UncertaintyStatus.CONTRADICTORY_EVIDENCE

    @property
    def requires_review(self) -> bool:
        """True when the refusal is a *doubt* rather than a known failure.

        Only :data:`REVIEW_STATUSES` (``MISSING``, ``UNKNOWN``, ``UNVERIFIED``,
        ``AMBIGUOUS``) report ``True``. A known negative result, an
        unreadable call and a contradiction report ``False``: they are known
        problems, not questions for a human. The flag never changes the
        decision - every one of these statuses refuses.
        """
        return self.status in REVIEW_STATUSES

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict.

        Only *validated* values are published: an unusable phase, dimension or
        state is reported as ``None``, an unusable evidence record as ``[]``,
        and no caller-authored value other than a bounded diagnostic code
        appears anywhere. T27 emits no command, no argv and no environment, so
        nothing in this mapping can be executed.
        """
        return {
            "policy_version": self.policy_version,
            "phase": self.phase.value if self.phase is not None else None,
            "status": self.status.value,
            "decision": self.decision.value,
            "is_allowed": self.is_allowed,
            "requires_review": self.requires_review,
            "is_contradiction": self.is_contradiction,
            "blocking_dimension": (
                self.blocking_dimension.value
                if self.blocking_dimension is not None
                else None
            ),
            "blocking_state": (
                self.blocking_state.value
                if self.blocking_state is not None
                else None
            ),
            "required_dimensions": [
                dimension.value for dimension in self.required_dimensions
            ],
            "evidence": [item.as_dict() for item in self.evidence],
            "reason": self.reason,
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's records)
# ---------------------------------------------------------------------------


def _outcome(
    *,
    phase: object,
    status: UncertaintyStatus,
    clause: str,
    blocking_dimension: Optional[EvidenceDimension] = None,
    blocking_state: Optional[EvidenceState] = None,
    required: Tuple[EvidenceDimension, ...] = (),
    evidence: Tuple[EvidenceItem, ...] = (),
) -> UncertaintyEvaluation:
    """Assemble the verdict from validated values and the deciding status.

    ``clause`` is the decisive reason fragment and the status's fixed
    consequence is appended as the contextual reason. Every clause T27 can
    reach is either a static string or is built from T27's own enum values, so
    no caller-authored text can appear in a reason.
    """
    verb = "Allowed" if status in ALLOWED_STATUSES else "Refused"
    if blocking_dimension is None:
        reason = f"{verb} ({status.value}): {clause}."
    else:
        reason = (
            f"{verb} ({status.value}): {blocking_dimension.value} is "
            f"{blocking_state.value} - {clause}."
        )
    return UncertaintyEvaluation(
        policy_version=POLICY_VERSION,
        phase=phase if isinstance(phase, MutationPhase) else None,
        status=status,
        decision=(
            UncertaintyDecision.ALLOW
            if status in ALLOWED_STATUSES
            else UncertaintyDecision.REFUSE
        ),
        blocking_dimension=(
            blocking_dimension
            if isinstance(blocking_dimension, EvidenceDimension)
            else None
        ),
        blocking_state=(
            blocking_state
            if isinstance(blocking_state, EvidenceState)
            else None
        ),
        required_dimensions=required,
        evidence=evidence,
        reason=reason,
        reasons=(reason, _STATUS_CONSEQUENCES[status]),
    )


def _refuse(
    *,
    phase: MutationPhase,
    required: Tuple[EvidenceDimension, ...],
    evidence: Tuple[EvidenceItem, ...],
    status: UncertaintyStatus,
    dimension: EvidenceDimension,
    state: EvidenceState,
) -> UncertaintyEvaluation:
    """Refuse on one required dimension, stating ``(status, dimension, state)``.

    The three values are stated explicitly at every call site instead of being
    looked up in a table, so a reader (and a test) can see, on the line that
    decides, exactly which state produces exactly which status.
    """
    return _outcome(
        phase=phase,
        status=status,
        clause=_STATUS_REASONS[status],
        blocking_dimension=dimension,
        blocking_state=state,
        required=required,
        evidence=evidence,
    )


def evaluate_uncertainty(
    *,
    uncertainty_input: UncertaintyInput,
) -> UncertaintyEvaluation:
    """Decide whether a mutation may proceed on the evidence it is given.

    Pure and deterministic: it reads only its argument, writes nothing,
    remembers nothing and counts nothing, and it performs no filesystem,
    network, subprocess or Git operation. The check order is
    :data:`PRECEDENCE`, and every uncertainty is a refusal: a phase that
    selects no policy, an evidence record that cannot be read, and every
    required dimension that is not explicitly confirmed all refuse the
    mutation.

    Args:
        uncertainty_input: the caller-asserted :class:`UncertaintyInput` - the
            phase being authorised plus one :class:`EvidenceItem` per dimension
            the caller can state.

    Returns:
        An immutable :class:`UncertaintyEvaluation`. Evaluating the same input
        again returns an equal verdict, and no caller value is mutated.

    Raises:
        TypeError: ``uncertainty_input`` is not an :class:`UncertaintyInput`.
            That is a caller error, not an evidence problem - every evidence
            problem is a refusal in the returned evaluation.
    """
    if not isinstance(uncertainty_input, UncertaintyInput):
        raise TypeError(
            "uncertainty_input must be an UncertaintyInput, not "
            f"{type(uncertainty_input).__name__}."
        )

    phase = uncertainty_input.phase
    raw_evidence = uncertainty_input.evidence

    # 1. A phase that selects no policy cannot establish a required-evidence
    #    set, so nothing is authorised - and nothing is judged either.
    if not isinstance(phase, MutationPhase):
        return _outcome(
            phase=phase,
            status=UncertaintyStatus.INVALID_POLICY,
            clause=_STATUS_REASONS[UncertaintyStatus.INVALID_POLICY],
        )

    required = REQUIRED_DIMENSIONS[phase]

    # 2. An evidence record T27 cannot read authorises nothing. A list, a
    #    mapping, a duplicate dimension, an unknown dimension or state and an
    #    ill-formed code are refused: never repaired, trimmed or skipped.
    problem = _input_problem(raw_evidence)
    if problem is not None:
        return _outcome(
            phase=phase,
            status=UncertaintyStatus.INVALID_INPUT,
            clause=problem,
            required=required,
        )

    evidence = _canonical(raw_evidence)
    supplied: Dict[EvidenceDimension, EvidenceState] = {
        item.dimension: item.state for item in evidence
    }

    # 3. An INVALID state is a caller bug, not a state T27 can reason about, so
    #    the whole record is refused - even when the ill-formed dimension is
    #    not one this phase requires, because nothing else in the record can
    #    then be trusted either. The scan is over _DIMENSION_ORDER, so the
    #    reported dimension never depends on the order of the tuple.
    for dimension in _DIMENSION_ORDER:
        if supplied.get(dimension) is EvidenceState.INVALID:
            return _outcome(
                phase=phase,
                status=UncertaintyStatus.INVALID_INPUT,
                clause=_STATUS_REASONS[UncertaintyStatus.INVALID_INPUT],
                blocking_dimension=dimension,
                blocking_state=EvidenceState.INVALID,
                required=required,
                evidence=evidence,
            )

    # 4. Absence first: a required dimension that was not supplied is a gap in
    #    the call, and a gap is never read as a confirmation.
    dimension = _first_with_state(
        required, supplied, EvidenceState.MISSING
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.MISSING_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.MISSING,
        )

    # 5a. A dimension that says it contradicts itself is refused as stated:
    #     T27 never resolves a contradiction in favour of the mutation.
    dimension = _first_with_state(
        required, supplied, EvidenceState.CONTRADICTORY
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.CONTRADICTORY_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.CONTRADICTORY,
        )

    # 5b. A confirmed claim whose required proof is present but not confirmed
    #     is a contradiction too: both facts cannot be true. The weak proof is
    #     the blocking dimension, because that is the evidence an operator has
    #     to repair. (An *absent* proof was already reported as
    #     MISSING_EVIDENCE above, which precedes this check.)
    pair = _broken_pair(required, supplied)
    if pair is not None:
        claim, proof = pair
        return _outcome(
            phase=phase,
            status=UncertaintyStatus.CONTRADICTORY_EVIDENCE,
            clause=(
                f"{claim.value} is confirmed while its required proof is not"
            ),
            blocking_dimension=proof,
            blocking_state=supplied[proof],
            required=required,
            evidence=evidence,
        )

    # 6. A *known* negative result is a failure, not doubt: it is refused and
    #    reported as a failure, so it is never confused with "not proven".
    dimension = _first_with_state(
        required, supplied, EvidenceState.NEGATIVE
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.NEGATIVE_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.NEGATIVE,
        )

    # 7. Not established, so the dimension is a doubt: refused as unknown.
    dimension = _first_with_state(
        required, supplied, EvidenceState.UNKNOWN
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.UNKNOWN_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.UNKNOWN,
        )

    # 8. Reported but not verified, so it is a doubt, not a confirmation.
    dimension = _first_with_state(
        required, supplied, EvidenceState.UNVERIFIED
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.UNVERIFIED_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.UNVERIFIED,
        )

    # 9. Ambiguous, and a tie is never broken in favour of the mutation.
    dimension = _first_with_state(
        required, supplied, EvidenceState.AMBIGUOUS
    )
    if dimension is not None:
        return _refuse(
            phase=phase,
            required=required,
            evidence=evidence,
            status=UncertaintyStatus.AMBIGUOUS_EVIDENCE,
            dimension=dimension,
            state=EvidenceState.AMBIGUOUS,
        )

    # 10. Allowed: CONFIRMED is the only state left once every state above has
    #     been excluded, so every required dimension is explicitly confirmed.
    #     T27 proves nothing beyond that - the caller must still enforce it.
    return _outcome(
        phase=phase,
        status=UncertaintyStatus.ALLOWED,
        clause=(
            f"all {len(required)} dimensions this phase requires are confirmed"
        ),
        blocking_dimension=None,
        blocking_state=None,
        required=required,
        evidence=evidence,
    )


__all__: Sequence[str] = (
    "ALLOWED_STATUSES",
    "CONTRADICTION_PAIRS",
    "EvidenceDimension",
    "EvidenceItem",
    "EvidenceState",
    "MutationPhase",
    "POLICY_VERSION",
    "PRECEDENCE",
    "REQUIRED_DIMENSIONS",
    "REVIEW_STATUSES",
    "UncertaintyDecision",
    "UncertaintyEvaluation",
    "UncertaintyInput",
    "UncertaintyStatus",
    "evaluate_uncertainty",
)
