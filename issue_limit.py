"""T24 - max issue limit: the pure policy that bounds one run's issue selection.

Purpose
-------
T24 answers exactly one question:

    "may this run process the issue selection it has been handed?"

It is a **policy / validation layer only**. It retrieves nothing, selects
nothing, orchestrates nothing, retries nothing, and it touches no network, no
Git, no SonarQube, no Codex and no file. It is a small, auditable contract of
three immutable records:

* :class:`IssueLimitPolicy` - the operator's configuration,
* :class:`IssueSelection` - the caller-asserted counts and keys,
* :class:`IssueLimitEvaluation` - the deterministic, fail-closed verdict.

What the limit means
--------------------
``max_issue_limit`` is the maximum number of issues allowed to **enter the
processing pipeline for one run**, so an accepted selection always satisfies

    selected_issue_count <= max_issue_limit

The bound is applied to the *selected* count - never to the discovered/retrieved
count - and T24 **never truncates**: an oversized selection is refused, never
silently cut down to fit. Truncating would change which issues a run processes
without the caller deciding it, so truncation is deliberately not a policy here.

``discovered_issue_count`` and ``selected_issue_keys`` are separate inputs for
that reason: the count T24 bounds is ``len(selected_issue_keys)``, while
``discovered_issue_count`` is only the bound the selection cannot exceed.
Keeping them separate makes "retrieved N issues" impossible to mistake for
"processed N issues".

Trust boundary
--------------
Both inputs are **caller-asserted**. T24 re-runs nothing and authenticates
nothing: it validates the internal consistency of a selection the caller already
made (counts, ordering, uniqueness) and enforces the configured bound. A caller
that misreports how many issues it retrieved, or what it selected, is not
detected here. This is a policy boundary, not an evidence-authentication
boundary: it exists so a future orchestration layer cannot accidentally process
more issues than the operator configured.

Determinism
-----------
The verdict is a pure function of ``(policy, selection)``: the evaluation order
is fixed (:data:`PRECEDENCE`), the caller's key order is preserved verbatim in
:attr:`IssueLimitEvaluation.accepted_issue_keys`, no set or mapping ever decides
an order, nothing is ranked, and no clock, environment value, random source or
module state is read. Re-evaluating the same inputs returns an equal record, and
the caller's collection is copied rather than mutated.

Fail closed
-----------
An unusable limit, an unusable count, an unusable selection, a repeated issue
key and a self-contradictory pair of counts are all refusals - never a silent
pass, never a repair, never a normalisation:

    =========================== ============================================
    Input                       Result
    =========================== ============================================
    ``max_issue_limit=None``    refused: an unconfigured limit is not
                                "unlimited"
    ``max_issue_limit`` bool    refused: ``True``/``False`` are not integers
    ``max_issue_limit`` < 0     refused: no negative bound exists
    ``max_issue_limit`` = 0     valid but strict: only an empty selection fits
    ``max_issue_limit`` > cap   refused: above :data:`MAX_ISSUE_LIMIT`
    discovered count not a      refused: the selection cannot be shown to be
    non-negative integer        possible
    selection not an ordered    refused: a set/mapping/iterator/bare string has
    sequence of non-empty str   no deterministic order
    a repeated issue key        refused: the count would not describe distinct
                                issues
    selected > discovered       refused: the counts contradict each other
    selected > limit            refused (:data:`IssueLimitStatus.EXCEEDED`)
    selected == limit           accepted, but the pipeline is full
    selected < limit            accepted, and slots remain
    =========================== ============================================

:data:`PRECEDENCE` is the authoritative evaluation order: a configuration that
cannot bound anything refuses before any evidence is considered.

Secret safety
-------------
The evaluation publishes **counts**, never the caller's keys: a limit decision is
about *how many* issues may enter the pipeline, not *which* ones, so
:meth:`IssueLimitEvaluation.as_dict` cannot leak a caller-supplied (or
secret-looking) key. Every refusal reason names counts and type names only, and
:meth:`IssueLimitPolicy.as_dict` publishes the *validated* bound, so no
caller-authored text is ever serialized by this module.

Not wired
---------
T24 is a library with no caller: ``main.py`` is byte-for-byte unchanged, no
orchestration exists yet, and this module imports the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

#: Version of the T24 policy contract.
POLICY_VERSION = "t24.1"

#: The largest limit T24 accepts. No single run can plausibly be expected to
#: process more issues than this, so a larger value is a configuration error
#: rather than a bound - refusing it keeps the policy's domain total and
#: explicit.
MAX_ISSUE_LIMIT = 1_000_000

#: The default limit: ``None``. It is the conservative choice precisely because
#: it is *invalid* - an unconfigured limit is never read as "unlimited", so the
#: policy refuses until an operator states an explicit bound.
DEFAULT_MAX_ISSUE_LIMIT: Optional[int] = None


class IssueLimitStatus(Enum):
    """Why a selection was accepted or refused (fail closed)."""

    __test__ = False

    #: Accepted: the selection is smaller than the limit, so slots remain.
    WITHIN_LIMIT = "within-limit"
    #: Accepted: the selection exactly fills the limit.
    AT_LIMIT = "at-limit"
    #: Refused: the selection is larger than the limit (T24 never truncates).
    EXCEEDED = "exceeded"
    #: Refused: ``max_issue_limit`` is not a usable bound.
    INVALID_CONFIG = "invalid-config"
    #: Refused: ``discovered_issue_count`` is not a non-negative integer.
    INVALID_DISCOVERED_COUNT = "invalid-discovered-count"
    #: Refused: the selected keys are not an ordered sequence of non-empty text.
    INVALID_SELECTION = "invalid-selection"
    #: Refused: the selection repeats an issue key.
    DUPLICATE_ISSUES = "duplicate-issues"
    #: Refused: the selection is larger than the count of discovered issues.
    IMPOSSIBLE_COUNTS = "impossible-counts"


class IssueLimitDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    ALLOW = "allow"
    REFUSE = "refuse"


#: The statuses that accept a selection. A status outside this tuple always
#: yields :attr:`IssueLimitDecision.REFUSE`.
ALLOWED_STATUSES: Tuple[IssueLimitStatus, ...] = (
    IssueLimitStatus.WITHIN_LIMIT,
    IssueLimitStatus.AT_LIMIT,
)

#: The evaluation order: the first status whose condition holds decides the
#: result. Configuration is checked first, because a policy that cannot bound
#: anything refuses before any evidence is considered.
PRECEDENCE: Tuple[IssueLimitStatus, ...] = (
    IssueLimitStatus.INVALID_CONFIG,
    IssueLimitStatus.INVALID_DISCOVERED_COUNT,
    IssueLimitStatus.INVALID_SELECTION,
    IssueLimitStatus.DUPLICATE_ISSUES,
    IssueLimitStatus.IMPOSSIBLE_COUNTS,
    IssueLimitStatus.EXCEEDED,
    IssueLimitStatus.AT_LIMIT,
    IssueLimitStatus.WITHIN_LIMIT,
)


# ---------------------------------------------------------------------------
# Reading and validating the caller's values (pure, never repairs anything)
# ---------------------------------------------------------------------------


def _limit(value: object) -> Optional[int]:
    """The configured bound, or ``None`` when it is not a usable one.

    ``None`` (not configured), ``bool`` (never an ``int`` here), ``float``,
    ``str`` and every other non-integer are unusable. ``0`` **is** usable: it is
    the strictest explicit bound, and only an empty selection fits it. Valid
    bounds are the integers ``0`` to :data:`MAX_ISSUE_LIMIT` inclusive.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_ISSUE_LIMIT:
        return None
    return value


def _count(value: object) -> Optional[int]:
    """A non-negative integer count, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _keys(value: object) -> Optional[Tuple[str, ...]]:
    """The caller's issue keys as an ordered tuple, or ``None`` when unusable.

    The keys must arrive as an ordered sequence of non-empty strings. A set, a
    frozenset, a mapping, a bare string and a one-shot iterator are all refused:
    none of them carries a deterministic order, so none of them can describe
    which issues a run would process. The returned tuple is a copy, so the
    caller's collection is never mutated. Key *shape* is not validated here -
    identity belongs to T22/T23, and this policy only counts.
    """
    if isinstance(value, (str, bytes, bytearray, Mapping, set, frozenset)):
        return None
    if not isinstance(value, Sequence):
        return None
    items = tuple(value)
    if not all(isinstance(item, str) and item for item in items):
        return None
    return items


def _config_problem(value: object) -> str:
    """The deterministic reason an unusable limit was refused.

    Only integers are ever echoed; any other value is described by its type name,
    so a caller-supplied string can never be published through a reason.
    """
    if value is None:
        return (
            "No max_issue_limit is configured, and an unconfigured limit is "
            "not read as unlimited."
        )
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "max_issue_limit must be a non-negative integer, not "
            f"{type(value).__name__}."
        )
    if value < 0:
        return f"max_issue_limit must not be negative (got {value})."
    return (
        f"max_issue_limit {value} exceeds the supported maximum "
        f"{MAX_ISSUE_LIMIT}."
    )


def _discovered_problem(value: object) -> str:
    """The deterministic reason an unusable discovered count was refused."""
    if value is None:
        return (
            "No discovered_issue_count was supplied, so the selection cannot "
            "be shown to be a subset of what was retrieved."
        )
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "discovered_issue_count must be a non-negative integer, not "
            f"{type(value).__name__}."
        )
    return f"discovered_issue_count must not be negative (got {value})."


def _selection_problem(value: object) -> str:
    """The deterministic reason an unusable selection was refused."""
    if isinstance(value, (str, bytes, bytearray, Mapping, set, frozenset)) or (
        not isinstance(value, Sequence)
    ):
        return (
            "selected_issue_keys must be an ordered sequence of issue keys, "
            f"not {type(value).__name__}: a set, mapping, string or iterator "
            "has no deterministic order."
        )
    return (
        "Every selected issue key must be a non-empty string: the selection "
        "carries an entry of another kind."
    )


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueLimitPolicy:
    """T24 configuration: how many issues one run may process.

    Attributes:
        max_issue_limit: the bound, or ``None`` when no bound was configured.
            ``None`` is invalid (fail closed): an unconfigured limit must never
            be read as "unlimited". ``0`` is valid and strict - only an empty
            selection fits it. Usable bounds are the integers ``0`` to
            :data:`MAX_ISSUE_LIMIT` inclusive; a bool, a float, a string and a
            negative value are refused.
    """

    max_issue_limit: Optional[int] = DEFAULT_MAX_ISSUE_LIMIT

    @property
    def is_valid(self) -> bool:
        """True only when the configuration states a usable bound."""
        return _limit(self.max_issue_limit) is not None

    @property
    def validated_limit(self) -> Optional[int]:
        """The usable bound, or ``None`` when the configuration is invalid."""
        return _limit(self.max_issue_limit)

    @property
    def refusal_reason(self) -> Optional[str]:
        """Why the configuration is unusable, or ``None`` when it is usable."""
        if self.is_valid:
            return None
        return _config_problem(self.max_issue_limit)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the configuration.

        The *validated* bound is published: an unusable value could be
        caller-authored text, so it is never echoed here (the refusal reason
        names its type instead).
        """
        return {
            "policy_version": POLICY_VERSION,
            "max_issue_limit": _limit(self.max_issue_limit),
            "maximum_supported_limit": MAX_ISSUE_LIMIT,
            "is_valid": self.is_valid,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class IssueSelection:
    """One run's caller-asserted issue selection.

    T24 never repairs this record: an unusable field is refused, not fixed.

    Attributes:
        discovered_issue_count: how many open issues the caller retrieved.
            ``None``, a bool, a float, a string and a negative value are refused
            - without a usable count the selection cannot be shown to be
            possible.
        selected_issue_keys: the issue keys the caller selected, in the caller's
            deterministic order. ``()`` is an empty selection, which every valid
            policy accepts. A set, a mapping, a bare string and a non-sequence
            are refused, because none of them carries a deterministic order.
    """

    discovered_issue_count: Optional[int] = None
    selected_issue_keys: Sequence[str] = ()

    @property
    def selected_issue_key_count(self) -> int:
        """How many keys the record carries.

        ``0`` when the field is unusable (the evaluation refuses it separately,
        so this figure must never be read as "an accepted empty selection").
        """
        keys = _keys(self.selected_issue_keys)
        return 0 if keys is None else len(keys)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the selection.

        The keys themselves are deliberately absent: a limit policy has no
        reason to serialize the caller's text, so only their count is published.
        """
        return {
            "discovered_issue_count": _count(self.discovered_issue_count),
            "selected_issue_key_count": self.selected_issue_key_count,
        }


@dataclass(frozen=True)
class IssueLimitEvaluation:
    """The deterministic, fail-closed verdict for one policy/selection pair.

    Attributes:
        policy_version: the T24 contract version that produced the verdict.
        status: why the selection was accepted or refused.
        decision: ``ALLOW`` only for :data:`ALLOWED_STATUSES`, else ``REFUSE``.
        max_issue_limit: the validated bound, or ``None`` when it is unusable.
        discovered_issue_count: the validated caller-asserted count, or ``None``
            when it was unusable.
        selected_issue_count: how many keys the caller selected, or ``None`` when
            the selection itself was unusable.
        accepted_issue_count: how many issues may enter the pipeline
            (``0`` for every refusal).
        accepted_issue_keys: the caller's keys in the caller's order, or ``()``
            for every refusal. Never reordered, never truncated, and never
            published by :meth:`as_dict`.
        remaining_issue_slots: ``max(limit - accepted, 0)`` for an accepted
            selection, else ``None`` (a refusal has no slots).
        can_accept_more: True only when the selection was accepted and slots
            remain.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its context).
        policy: the configuration the verdict used, so it explains itself.
    """

    policy_version: str
    status: IssueLimitStatus
    decision: IssueLimitDecision
    max_issue_limit: Optional[int]
    discovered_issue_count: Optional[int]
    selected_issue_count: Optional[int]
    accepted_issue_count: int
    accepted_issue_keys: Tuple[str, ...]
    remaining_issue_slots: Optional[int]
    can_accept_more: bool
    reason: str
    reasons: Tuple[str, ...]
    policy: IssueLimitPolicy

    @property
    def is_allowed(self) -> bool:
        """True only when the selection may enter the pipeline."""
        return self.decision is IssueLimitDecision.ALLOW

    @property
    def limit_exceeded(self) -> bool:
        """True only when the selection is larger than the configured limit."""
        return self.status is IssueLimitStatus.EXCEEDED

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict.

        Counts only: ``accepted_issue_keys`` is deliberately absent, because a
        limit decision has no reason to publish the caller's keys.
        """
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "decision": self.decision.value,
            "is_allowed": self.is_allowed,
            "limit_exceeded": self.limit_exceeded,
            "max_issue_limit": self.max_issue_limit,
            "discovered_issue_count": self.discovered_issue_count,
            "selected_issue_count": self.selected_issue_count,
            "accepted_issue_count": self.accepted_issue_count,
            "remaining_issue_slots": self.remaining_issue_slots,
            "can_accept_more": self.can_accept_more,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "policy": self.policy.as_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's values)
# ---------------------------------------------------------------------------


def _evaluation(
    policy: IssueLimitPolicy,
    status: IssueLimitStatus,
    reason: str,
    reasons: Tuple[str, ...],
    *,
    limit: Optional[int],
    discovered: Optional[int],
    selected: Optional[int],
    accepted_keys: Tuple[str, ...] = (),
) -> IssueLimitEvaluation:
    """Assemble the verdict, deriving every flag from the status."""
    allowed = status in ALLOWED_STATUSES
    accepted = len(accepted_keys)
    remaining: Optional[int] = None
    if allowed and limit is not None:
        remaining = max(limit - accepted, 0)
    return IssueLimitEvaluation(
        policy_version=POLICY_VERSION,
        status=status,
        decision=(
            IssueLimitDecision.ALLOW if allowed else IssueLimitDecision.REFUSE
        ),
        max_issue_limit=limit,
        discovered_issue_count=discovered,
        selected_issue_count=selected,
        accepted_issue_count=accepted,
        accepted_issue_keys=accepted_keys,
        remaining_issue_slots=remaining,
        can_accept_more=remaining is not None and remaining > 0,
        reason=reason,
        reasons=reasons,
        policy=policy,
    )


def evaluate_issue_limit(
    *,
    policy: IssueLimitPolicy,
    selection: IssueSelection,
) -> IssueLimitEvaluation:
    """Decide whether ``selection`` may enter the processing pipeline.

    Pure and deterministic: it reads only its two arguments, copies the caller's
    keys, and never truncates, reorders, repairs or mutates anything. The check
    order is :data:`PRECEDENCE`.

    Args:
        policy: the operator's :class:`IssueLimitPolicy`.
        selection: the caller-asserted :class:`IssueSelection`.

    Returns:
        An immutable :class:`IssueLimitEvaluation`. For an accepted selection,
        ``accepted_issue_keys`` is exactly the caller's keys in the caller's
        order; for a refusal it is empty and the reason says why.

    Raises:
        TypeError: ``policy`` is not an :class:`IssueLimitPolicy`, or
            ``selection`` is not an :class:`IssueSelection`. That is a caller
            error, not an evidence problem - every evidence problem is a refusal
            in the returned evaluation.
    """
    if not isinstance(policy, IssueLimitPolicy):
        raise TypeError(
            "policy must be an IssueLimitPolicy, not "
            f"{type(policy).__name__}."
        )
    if not isinstance(selection, IssueSelection):
        raise TypeError(
            "selection must be an IssueSelection, not "
            f"{type(selection).__name__}."
        )

    limit = _limit(policy.max_issue_limit)
    discovered = _count(selection.discovered_issue_count)
    keys = _keys(selection.selected_issue_keys)
    selected = None if keys is None else len(keys)

    # 1. A policy that states no usable bound accepts nothing at all.
    if limit is None:
        reason = _config_problem(policy.max_issue_limit)
        return _evaluation(
            policy,
            IssueLimitStatus.INVALID_CONFIG,
            reason,
            (
                reason,
                "The selection was not evaluated: T24 fails closed when it "
                "cannot bound a run.",
            ),
            limit=None,
            discovered=discovered,
            selected=selected,
        )

    # 2. The discovered count is the bound the selection may not exceed.
    if discovered is None:
        reason = _discovered_problem(selection.discovered_issue_count)
        return _evaluation(
            policy,
            IssueLimitStatus.INVALID_DISCOVERED_COUNT,
            reason,
            (
                reason,
                "The selection cannot be shown to be possible, so the run is "
                "refused rather than started.",
            ),
            limit=limit,
            discovered=None,
            selected=selected,
        )

    # 3. The selection must state which issues, in a deterministic order.
    if keys is None:
        reason = _selection_problem(selection.selected_issue_keys)
        return _evaluation(
            policy,
            IssueLimitStatus.INVALID_SELECTION,
            reason,
            (
                reason,
                "The caller's order is preserved verbatim, so an unordered "
                "collection cannot be accepted.",
            ),
            limit=limit,
            discovered=discovered,
            selected=None,
        )

    # 4. A repeated key would make the count describe fewer distinct issues.
    if len(set(keys)) != len(keys):
        reason = (
            "The selection repeats an issue key, so its count would not "
            "describe distinct issues."
        )
        return _evaluation(
            policy,
            IssueLimitStatus.DUPLICATE_ISSUES,
            reason,
            (
                reason,
                "T24 does not repair a selection: pass each issue once.",
            ),
            limit=limit,
            discovered=discovered,
            selected=selected,
        )

    # 5. A selection larger than the discovered set contradicts itself.
    if selected > discovered:
        reason = (
            f"The selection names {selected} issue(s) but only {discovered} "
            "were discovered, so the counts contradict each other."
        )
        return _evaluation(
            policy,
            IssueLimitStatus.IMPOSSIBLE_COUNTS,
            reason,
            (
                reason,
                "A selection can never be larger than the set it was selected "
                "from.",
            ),
            limit=limit,
            discovered=discovered,
            selected=selected,
        )

    # 6. The bound itself, applied to the selected count only.
    if selected > limit:
        reason = (
            f"The selection of {selected} issue(s) exceeds the limit of "
            f"{limit} issue(s), so the run is refused."
        )
        return _evaluation(
            policy,
            IssueLimitStatus.EXCEEDED,
            reason,
            (
                reason,
                "T24 never truncates a selection: raise max_issue_limit "
                f"deliberately, or select at most {limit} issue(s).",
            ),
            limit=limit,
            discovered=discovered,
            selected=selected,
        )

    # 7/8. Accepted: the caller's own keys are echoed in the caller's own order.
    if selected == limit:
        reason = (
            f"The selection of {selected} issue(s) exactly reaches the limit "
            f"of {limit} issue(s), so no further issue may be added."
        )
        return _evaluation(
            policy,
            IssueLimitStatus.AT_LIMIT,
            reason,
            (
                reason,
                "The pipeline is full: T24 never accepts more than the "
                "configured bound.",
            ),
            limit=limit,
            discovered=discovered,
            selected=selected,
            accepted_keys=keys,
        )

    reason = (
        f"The selection of {selected} issue(s) is within the limit of "
        f"{limit} issue(s)."
    )
    return _evaluation(
        policy,
        IssueLimitStatus.WITHIN_LIMIT,
        reason,
        (
            reason,
            f"{limit - selected} issue slot(s) remain for this run.",
        ),
        limit=limit,
        discovered=discovered,
        selected=selected,
        accepted_keys=keys,
    )


__all__: Sequence[str] = (
    "ALLOWED_STATUSES",
    "DEFAULT_MAX_ISSUE_LIMIT",
    "IssueLimitDecision",
    "IssueLimitEvaluation",
    "IssueLimitPolicy",
    "IssueLimitStatus",
    "IssueSelection",
    "MAX_ISSUE_LIMIT",
    "POLICY_VERSION",
    "PRECEDENCE",
    "evaluate_issue_limit",
)
