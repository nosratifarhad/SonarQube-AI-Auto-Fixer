"""T25 - max iteration limit: the pure policy that bounds an issue's retries.

Purpose
-------
T25 answers exactly one question:

    "may the caller execute this iteration of this issue's fix attempt?"

It is a **policy / validation layer only**. It runs nothing, retries nothing,
defers nothing, orchestrates nothing, and it touches no network, no Git, no
SonarQube, no Codex and no file. It is a small, auditable contract of three
immutable records:

* :class:`IterationLimitPolicy` - the operator's configuration,
* :class:`IterationAttempt` - the caller-asserted attempt number,
* :class:`IterationLimitEvaluation` - the deterministic, fail-closed verdict.

Exact semantics (no off-by-one)
-------------------------------
``max_iterations`` is the maximum number of attempts one issue may receive, so

    1 <= current_iteration <= max_iterations     is eligible for execution
    current_iteration > max_iterations           is refused

Concretely: ``max_iterations=1`` allows attempt 1 and refuses attempt 2;
``max_iterations=2`` allows attempts 1 and 2 and refuses attempt 3. Attempt
numbers start at 1, so ``0`` and every negative value are refused as "not an
attempt" rather than silently normalised to 1.

Retry safety (the whole point)
------------------------------
The policy owns no counter and mutates nothing:

* ``max_iterations`` is a frozen field of :class:`IterationLimitPolicy` and the
  evaluation never writes to it, so a *successful* iteration cannot raise the
  limit and no failure can lower it;
* the current iteration is **caller-asserted** and passed in explicitly: there
  is no ``advance()``, no ``next()``, no ``reset()`` and no hidden increment.
  The caller owns the counter and T25 only judges the value it is handed;
* T25 never reads an attempt's outcome. It imports no status vocabulary and
  exposes no parameter that could carry one, so ``TESTS_FAILED``,
  ``ANALYSIS_FAILED``, ``CODEX_FAILED``, ``STILL_OPEN`` or ``REVIEW_REQUIRED``
  cannot reset, extend or bypass the limit - such a value cannot even be
  expressed to this module;
* re-evaluating the same attempt always returns an equal verdict: nothing is
  cached, counted or remembered between calls.

Once the limit is spent it stays spent: iteration ``max_iterations + 1`` is
refused however many times it is asked, and whatever happened to the earlier
attempts.

Trust boundary
--------------
Both inputs are **caller-asserted**. T25 counts nothing and authenticates
nothing: a caller that misreports which iteration it is on is not detected here.
This is a policy boundary, not an evidence-authentication boundary: it exists so
a future orchestration layer cannot enter an unbounded retry loop by accident.

Fail closed
-----------
An unusable limit and an unusable attempt number are refusals - never a silent
pass, never a repair, never a normalisation:

    =========================== ============================================
    Input                       Result
    =========================== ============================================
    ``max_iterations=None``     refused: an unconfigured retry limit is not
                                "unlimited"
    ``max_iterations`` bool     refused: ``True``/``False`` are not integers
    ``max_iterations`` < 0      refused: no negative bound exists
    ``max_iterations`` = 0      valid but strict: no attempt is ever allowed
    ``max_iterations`` > cap    refused: above :data:`MAX_ITERATIONS`
    ``current_iteration`` None  refused: no attempt was specified
    ``current_iteration`` < 1   refused: attempt numbers start at 1
    ``current_iteration`` > max refused (:data:`IterationLimitStatus.EXCEEDED`)
    ``current_iteration`` == max allowed, and it is the last attempt
    ``current_iteration`` < max allowed, and further attempts remain
    =========================== ============================================

:data:`PRECEDENCE` is the authoritative evaluation order: a configuration that
cannot bound anything refuses before any attempt is considered.

Determinism and secret safety
-----------------------------
The verdict is a pure function of ``(policy, attempt)``: no clock, environment
value, random source or module state is read, and the same inputs always produce
an equal record. Only integers, enums and this module's own sentences are ever
serialized - a value that is not a usable integer is described by its type name
and never echoed, so no caller-authored text can be published here.

Not wired
---------
T25 is a library with no caller: ``main.py`` is byte-for-byte unchanged, no
orchestration exists yet, and this module imports the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

#: Version of the T25 policy contract.
POLICY_VERSION = "t25.1"

#: The largest retry bound T25 accepts. A single issue cannot plausibly need more
#: AI-fix attempts than this, so a larger value is a configuration error rather
#: than a bound - refusing it keeps the policy's domain total and explicit.
MAX_ITERATIONS = 1_000

#: The default retry bound: ``None``. It is the conservative choice precisely
#: because it is *invalid* - an unconfigured retry limit is never read as
#: "unlimited", so the policy refuses until an operator states an explicit
#: bound.
DEFAULT_MAX_ITERATIONS: Optional[int] = None


class IterationLimitStatus(Enum):
    """Why one iteration was allowed or refused (fail closed)."""

    __test__ = False

    #: Allowed: at least one further iteration is still available.
    WITHIN_LIMIT = "within-limit"
    #: Allowed: this is the last permitted iteration.
    AT_LIMIT = "at-limit"
    #: Refused: the iteration is beyond the permitted number of attempts.
    EXCEEDED = "exceeded"
    #: Refused: ``max_iterations`` is not a usable bound.
    INVALID_CONFIG = "invalid-config"
    #: Refused: ``current_iteration`` is not an attempt number (an integer >= 1).
    INVALID_ITERATION = "invalid-iteration"


class IterationLimitDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    ALLOW = "allow"
    REFUSE = "refuse"


#: The statuses that allow an iteration. A status outside this tuple always
#: yields :attr:`IterationLimitDecision.REFUSE` and can never be bypassed.
ALLOWED_STATUSES: Tuple[IterationLimitStatus, ...] = (
    IterationLimitStatus.WITHIN_LIMIT,
    IterationLimitStatus.AT_LIMIT,
)

#: The evaluation order: the first status whose condition holds decides the
#: result. Configuration is checked first, because a policy that cannot bound
#: anything refuses before any attempt is considered.
PRECEDENCE: Tuple[IterationLimitStatus, ...] = (
    IterationLimitStatus.INVALID_CONFIG,
    IterationLimitStatus.INVALID_ITERATION,
    IterationLimitStatus.EXCEEDED,
    IterationLimitStatus.WITHIN_LIMIT,
    IterationLimitStatus.AT_LIMIT,
)


# ---------------------------------------------------------------------------
# Reading and validating the caller's values (pure, never repairs anything)
# ---------------------------------------------------------------------------


def _iterations(value: object) -> Optional[int]:
    """The configured bound, or ``None`` when it is not a usable one.

    ``None`` (not configured), ``bool`` (never an ``int`` here), ``float``,
    ``str`` and every other non-integer are unusable. ``0`` **is** usable: it is
    the strictest explicit bound, and it refuses every attempt. Usable bounds are
    the integers ``0`` to :data:`MAX_ITERATIONS` inclusive.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_ITERATIONS:
        return None
    return value


def _iteration(value: object) -> Optional[int]:
    """The attempt number, or ``None`` when it is not one.

    An attempt number is an integer of at least ``1``. ``bool``, ``float``,
    ``str``, ``0`` and every negative value are unusable, and they are never
    normalised: ``0`` is refused as "not an attempt" instead of being read as
    the first one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _config_problem(value: object) -> str:
    """The deterministic reason an unusable bound was refused.

    Only integers are ever echoed; any other value is described by its type name,
    so a caller-supplied string can never be published through a reason.
    """
    if value is None:
        return (
            "No max_iterations is configured, and an unconfigured retry limit "
            "is not read as unlimited."
        )
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "max_iterations must be a non-negative integer, not "
            f"{type(value).__name__}."
        )
    if value < 0:
        return f"max_iterations must not be negative (got {value})."
    return (
        f"max_iterations {value} exceeds the supported maximum "
        f"{MAX_ITERATIONS}."
    )


def _iteration_problem(value: object) -> str:
    """The deterministic reason an unusable attempt number was refused."""
    if value is None:
        return (
            "No current_iteration was supplied, so no attempt is authorised."
        )
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "current_iteration must be an integer of at least 1, not "
            f"{type(value).__name__}."
        )
    return (
        f"current_iteration {value} is not an attempt: iteration numbers "
        "start at 1."
    )


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationLimitPolicy:
    """T25 configuration: how many attempts one issue may receive.

    Attributes:
        max_iterations: the bound, or ``None`` when no bound was configured.
            ``None`` is invalid (fail closed): an unconfigured retry limit must
            never be read as "unlimited". ``0`` is valid and strict - it refuses
            every attempt. Usable bounds are the integers ``0`` to
            :data:`MAX_ITERATIONS` inclusive; a bool, a float, a string and a
            negative value are refused.
    """

    max_iterations: Optional[int] = DEFAULT_MAX_ITERATIONS

    @property
    def is_valid(self) -> bool:
        """True only when the configuration states a usable bound."""
        return _iterations(self.max_iterations) is not None

    @property
    def validated_max_iterations(self) -> Optional[int]:
        """The usable bound, or ``None`` when the configuration is invalid."""
        return _iterations(self.max_iterations)

    @property
    def refusal_reason(self) -> Optional[str]:
        """Why the configuration is unusable, or ``None`` when it is usable."""
        if self.is_valid:
            return None
        return _config_problem(self.max_iterations)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the configuration.

        The *validated* bound is published: an unusable value could be
        caller-authored text, so it is never echoed here (the refusal reason
        names its type instead).
        """
        return {
            "policy_version": POLICY_VERSION,
            "max_iterations": _iterations(self.max_iterations),
            "maximum_supported_iterations": MAX_ITERATIONS,
            "is_valid": self.is_valid,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class IterationAttempt:
    """One caller-asserted iteration of one issue's fix attempt.

    T25 never repairs this record: an unusable value is refused, not fixed.

    Attributes:
        current_iteration: the 1-based attempt number the caller is about to run.
            **Caller-asserted**: T25 does not count, remember, reset or
            increment it. ``None``, a bool, a float, a string, ``0`` and every
            negative value are refused.
    """

    current_iteration: Optional[int] = None

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the attempt.

        Only the *validated* attempt number is published: a value that is not a
        usable integer is never echoed, because it could be caller text.
        """
        validated = _iteration(self.current_iteration)
        return {
            "current_iteration": validated,
            "is_usable_attempt": validated is not None,
        }


@dataclass(frozen=True)
class IterationLimitEvaluation:
    """The deterministic, fail-closed verdict for one policy/attempt pair.

    Attributes:
        policy_version: the T25 contract version that produced the verdict.
        status: why the iteration was allowed or refused.
        decision: ``ALLOW`` only for :data:`ALLOWED_STATUSES`, else ``REFUSE``.
        max_iterations: the validated bound, or ``None`` when it is unusable.
        current_iteration: the validated attempt number, or ``None`` when it is
            unusable.
        attempt_allowed: True only when this attempt may execute
            (``1 <= current_iteration <= max_iterations``).
        next_iteration_allowed: True only when the *next* attempt
            (``current_iteration + 1``) would also be allowed, so it is ``True``
            exactly when a further attempt is available.
        limit_reached: True when this attempt may execute and is the last one,
            and also when it is already beyond the limit - i.e. the counter has
            reached the end of the budget either way.
        remaining_iterations: ``max(max_iterations - current_iteration, 0)`` when
            both values are usable, else ``None``.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its context).
        policy: the configuration the verdict used, so it explains itself.
    """

    policy_version: str
    status: IterationLimitStatus
    decision: IterationLimitDecision
    max_iterations: Optional[int]
    current_iteration: Optional[int]
    attempt_allowed: bool
    next_iteration_allowed: bool
    limit_reached: bool
    remaining_iterations: Optional[int]
    reason: str
    reasons: Tuple[str, ...]
    policy: IterationLimitPolicy

    @property
    def is_allowed(self) -> bool:
        """True only when this attempt may execute."""
        return self.decision is IterationLimitDecision.ALLOW

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict."""
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "decision": self.decision.value,
            "attempt_allowed": self.attempt_allowed,
            "next_iteration_allowed": self.next_iteration_allowed,
            "limit_reached": self.limit_reached,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "remaining_iterations": self.remaining_iterations,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "policy": self.policy.as_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's values)
# ---------------------------------------------------------------------------


def _evaluation(
    policy: IterationLimitPolicy,
    status: IterationLimitStatus,
    reason: str,
    reasons: Tuple[str, ...],
    *,
    limit: Optional[int],
    current: Optional[int],
) -> IterationLimitEvaluation:
    """Assemble the verdict, deriving every flag from the status."""
    allowed = status in ALLOWED_STATUSES
    usable = limit is not None and current is not None
    remaining: Optional[int] = None
    if usable:
        remaining = max(limit - current, 0)
    return IterationLimitEvaluation(
        policy_version=POLICY_VERSION,
        status=status,
        decision=(
            IterationLimitDecision.ALLOW
            if allowed
            else IterationLimitDecision.REFUSE
        ),
        max_iterations=limit,
        current_iteration=current,
        attempt_allowed=allowed,
        next_iteration_allowed=allowed and usable and current < limit,
        limit_reached=usable and current >= limit,
        remaining_iterations=remaining,
        reason=reason,
        reasons=reasons,
        policy=policy,
    )


def evaluate_iteration_limit(
    *,
    policy: IterationLimitPolicy,
    attempt: IterationAttempt,
) -> IterationLimitEvaluation:
    """Decide whether one iteration of one issue's fix attempt may run.

    Pure and deterministic: it reads only its two arguments, writes nothing,
    remembers nothing and counts nothing. The check order is
    :data:`PRECEDENCE`, and the rule is exactly

        1 <= current_iteration <= max_iterations

    with no off-by-one: ``max_iterations=1`` allows attempt 1 and refuses
    attempt 2.

    Args:
        policy: the operator's :class:`IterationLimitPolicy`.
        attempt: the caller-asserted :class:`IterationAttempt`.

    Returns:
        An immutable :class:`IterationLimitEvaluation`. A refusal never mutates
        the policy, so re-evaluating the same attempt always returns an equal
        verdict and the limit cannot be escaped by retrying.

    Raises:
        TypeError: ``policy`` is not an :class:`IterationLimitPolicy`, or
            ``attempt`` is not an :class:`IterationAttempt`. That is a caller
            error, not an evidence problem - every evidence problem is a refusal
            in the returned evaluation.
    """
    if not isinstance(policy, IterationLimitPolicy):
        raise TypeError(
            "policy must be an IterationLimitPolicy, not "
            f"{type(policy).__name__}."
        )
    if not isinstance(attempt, IterationAttempt):
        raise TypeError(
            "attempt must be an IterationAttempt, not "
            f"{type(attempt).__name__}."
        )

    limit = _iterations(policy.max_iterations)
    current = _iteration(attempt.current_iteration)

    # 1. A policy that states no usable bound authorises no attempt at all.
    if limit is None:
        reason = _config_problem(policy.max_iterations)
        return _evaluation(
            policy,
            IterationLimitStatus.INVALID_CONFIG,
            reason,
            (
                reason,
                "The attempt was not judged: T25 fails closed when it cannot "
                "bound a retry loop.",
            ),
            limit=None,
            current=current,
        )

    # 2. Without an attempt number there is nothing to authorise - and a value
    #    below 1 is not an attempt, so it is refused rather than normalised.
    if current is None:
        reason = _iteration_problem(attempt.current_iteration)
        return _evaluation(
            policy,
            IterationLimitStatus.INVALID_ITERATION,
            reason,
            (
                reason,
                "T25 never resets, repairs or renumbers an iteration counter.",
            ),
            limit=limit,
            current=None,
        )

    # 3. The bound itself.
    if current > limit:
        reason = (
            f"Iteration {current} exceeds the limit of {limit} permitted "
            "iteration(s), so it is refused."
        )
        return _evaluation(
            policy,
            IterationLimitStatus.EXCEEDED,
            reason,
            (
                reason,
                "The budget is spent: reaching the limit prevents another "
                "attempt, and no outcome can reset the counter.",
            ),
            limit=limit,
            current=current,
        )

    # 4. Allowed: at the limit it is the last attempt, below it retries remain.
    if current == limit:
        reason = (
            f"Iteration {current} is the last of the {limit} permitted "
            "iteration(s)."
        )
        return _evaluation(
            policy,
            IterationLimitStatus.AT_LIMIT,
            reason,
            (
                reason,
                "A further attempt will be refused.",
            ),
            limit=limit,
            current=current,
        )

    reason = (
        f"Iteration {current} of at most {limit} permitted iteration(s) may "
        "run."
    )
    return _evaluation(
        policy,
        IterationLimitStatus.WITHIN_LIMIT,
        reason,
        (
            reason,
            f"{limit - current} further iteration(s) remain before the limit "
            "is reached.",
        ),
        limit=limit,
        current=current,
    )


__all__: Sequence[str] = (
    "ALLOWED_STATUSES",
    "DEFAULT_MAX_ITERATIONS",
    "IterationAttempt",
    "IterationLimitDecision",
    "IterationLimitEvaluation",
    "IterationLimitPolicy",
    "IterationLimitStatus",
    "MAX_ITERATIONS",
    "POLICY_VERSION",
    "PRECEDENCE",
    "evaluate_iteration_limit",
)

