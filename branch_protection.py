"""T26 - main/default branch protection: is this branch safe to mutate?

Purpose
-------
T26 answers exactly one question:

    "is this Git branch safe to mutate for an AI-generated SonarQube fix?"

It is a **policy / decision layer only**. It discovers nothing, authenticates
nothing, mutates nothing, and it touches no network, no Git, no SonarQube, no
Codex and no file. It is a small, auditable contract of three immutable records:

* :class:`ProtectedBranchPolicy` - the operator's configuration,
* :class:`BranchProtectionInput` - the caller-asserted candidate branch and the
  repository's default branch,
* :class:`BranchProtectionEvaluation` - the deterministic, fail-closed verdict.

An AI-generated fix may never be committed to, or pushed to, a branch that is

1. a **protected** branch name (default: ``main``, ``master``, ``develop``,
   ``trunk``),
2. the repository's **default** branch, whatever it happens to be named, or
3. an **unusable branch identity** (missing, empty, malformed, ref-like, or a
   whitespace/case variant of one of the above).

Optionally (and by default) the candidate must additionally live inside the T07
AI-fix namespace ``ai/sonar-fix/``, so a human branch such as ``feature/x`` is
refused even when it is neither protected nor the default branch.

Normalisation (the one rule, applied to every name)
---------------------------------------------------
Every name T26 compares - the candidate, the default branch and each configured
protected name - must first be a **usable branch name**: a ``str``, valid
according to T07's :func:`branch_naming.validate_branch_name`, and not a fully
qualified ref (``refs/...``). The *comparison key* of a usable name is then
``name.strip().casefold()``, and protection is decided by comparing keys.

The guarantees this buys are the point of the module:

* ``main``, ``MAIN``, ``Main`` and ``main/`` can never be mutation targets: the
  first three are the same key as ``main`` (refused as protected/default) and
  ``main/`` is not a valid ref at all (refused as an invalid identity);
* a configured ``develop`` still refuses a candidate spelled ``DeVeLoP``, and a
  default branch spelled ``MAIN`` still refuses a candidate ``main``;
* T26 **never rewrites** a branch name: normalisation produces a comparison key
  only, and the name that is judged (and echoed) is the caller's verbatim string.

Trust boundary
--------------
Both inputs are **caller-asserted**. T26 authenticates nothing: it does not
authenticate the caller, it does not discover the repository's default branch
(that is a future orchestration/integration concern - see §18 of the spec), it
does not prove the branch exists locally or remotely, and it does not prove that
the current checkout is on the branch it evaluates. A caller that misstates the
default branch, or simply does not call T26 at all, is not detected here. This
is a policy boundary, not an evidence-authentication boundary, and not a Git
security control: it exists so a future orchestration layer cannot *accidentally*
point an AI-generated commit or push at ``main``.

Determinism and purity
----------------------
:data:`PRECEDENCE` is the authoritative evaluation order, so overlapping
refusals are deterministic. The verdict is a pure function of
``(policy, branch)``: no clock, environment value, random source, cache, counter
or module state is read, no filesystem/network/subprocess/Git operation is ever
performed, the same inputs always produce an equal record, and the caller's
values are never mutated. T26 constructs no shell command and no argv.

Fail closed
-----------
Uncertainty about a branch is always a refusal - never "probably safe":

    ================================ ======================================
    Input                            Result
    ================================ ======================================
    unusable protected-branch set    refused: ``INVALID_POLICY``; ``None``
                                     never means "protect nothing"
    unusable AI-prefix flag          refused: ``INVALID_POLICY``; the flag
                                     must be a real ``bool``
    missing/malformed candidate      refused: ``INVALID_BRANCH``
    missing/malformed default branch refused: ``MISSING_DEFAULT_BRANCH``
    protected candidate              refused: ``PROTECTED_BRANCH``
    candidate == default branch      refused: ``DEFAULT_BRANCH``
    candidate outside ``ai/sonar-fix/``
                                     refused: ``WRONG_BRANCH_NAMESPACE``
    otherwise                        allowed: ``ALLOWED``
    ================================ ======================================

Not wired
---------
T26 is a library with no caller: ``main.py`` is byte-for-byte unchanged, the
T20/T21 gates keep their own (unchanged) inline branch checks, no orchestration
exists yet, and this module imports the standard library plus the pure T07
validator ``branch_naming`` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Mapping, Optional, Sequence, Tuple

from branch_naming import (
    AGENT_BRANCH_PREFIX,
    BranchNameError,
    validate_branch_name,
)

#: Version of the T26 policy contract.
POLICY_VERSION = "t26.1"

#: Branches T26 never allows an AI-generated fix to mutate. The values mirror
#: T20's ``commit_policy.PROTECTED_BRANCH_NAMES`` and T21's default (the only
#: other places this project states the list); a compatibility test pins the
#: equality, and T26 keeps its own copy because it must not depend on T20/T21.
PROTECTED_BRANCH_NAMES: Tuple[str, ...] = ("main", "master", "develop", "trunk")

#: The namespace T07 creates AI-fix branches in. T26 re-exports T07's own
#: constant instead of restating it, so the namespace has a single owner.
AI_FIX_BRANCH_PREFIX: str = AGENT_BRANCH_PREFIX

#: A value with this prefix names a *reference*, not a branch name, so its
#: identity is ambiguous and T26 refuses it.
_REF_NAMESPACE_PREFIX = "refs/"


class BranchProtectionStatus(Enum):
    """Why a candidate branch was allowed or refused (fail closed)."""

    __test__ = False

    #: Allowed: the branch may receive an AI-generated fix.
    ALLOWED = "allowed"
    #: Refused: the branch is one of the configured protected names.
    PROTECTED_BRANCH = "protected-branch"
    #: Refused: the branch **is** the repository's default branch.
    DEFAULT_BRANCH = "default-branch"
    #: Refused: the candidate is not a usable branch identity.
    INVALID_BRANCH = "invalid-branch"
    #: Refused: the protected-branch configuration is not usable.
    INVALID_POLICY = "invalid-policy"
    #: Refused: no usable default branch was supplied.
    MISSING_DEFAULT_BRANCH = "missing-default-branch"
    #: Refused: the branch is outside the AI-fix namespace while it is required.
    WRONG_BRANCH_NAMESPACE = "wrong-branch-namespace"


class BranchProtectionDecision(Enum):
    """Overall verdict of one evaluation (fail closed)."""

    __test__ = False

    ALLOW = "allow"
    REFUSE = "refuse"


#: The statuses that allow a mutation. A status outside this tuple always yields
#: :attr:`BranchProtectionDecision.REFUSE` and can never be bypassed.
ALLOWED_STATUSES: Tuple[BranchProtectionStatus, ...] = (
    BranchProtectionStatus.ALLOWED,
)

#: The evaluation order: the first status whose condition holds decides the
#: result. It is deliberately "preconditions first": a policy that cannot state
#: what is protected, a candidate whose identity is not established, and a
#: default branch that was not stated all refuse before any classification is
#: attempted - so an unusable configuration or a missing default branch can
#: never be reported as "protected" or, worse, as "allowed".
#:
#: The documented consequence is that ``candidate="main"`` with
#: ``default_branch=None`` is refused as
#: :attr:`BranchProtectionStatus.MISSING_DEFAULT_BRANCH` (the missing
#: precondition), not as ``PROTECTED_BRANCH``: T26 refuses to classify a
#: candidate against a repository whose default branch it does not know.
PRECEDENCE: Tuple[BranchProtectionStatus, ...] = (
    BranchProtectionStatus.INVALID_POLICY,
    BranchProtectionStatus.INVALID_BRANCH,
    BranchProtectionStatus.MISSING_DEFAULT_BRANCH,
    BranchProtectionStatus.PROTECTED_BRANCH,
    BranchProtectionStatus.DEFAULT_BRANCH,
    BranchProtectionStatus.WRONG_BRANCH_NAMESPACE,
    BranchProtectionStatus.ALLOWED,
)


# ---------------------------------------------------------------------------
# Reading and validating the caller's names (pure, never repairs anything)
# ---------------------------------------------------------------------------


def _problem(value: object, what: str) -> str:
    """The deterministic reason ``value`` is not a usable branch name, or ``""``.

    T07's validator owns the Git ref rules, so its message is reused verbatim: it
    names the rule and never echoes the whole value (only the offending character
    can appear, and only through the ``repr()`` of that single character), so no
    caller-authored text can reach a reason through this path. ``what`` labels the
    field being read (``"candidate_branch"``, ``"default_branch"``,
    ``"protected branch entry 2"``).
    """
    if value is None:
        return f"{what} is missing."
    if not isinstance(value, str):
        return f"{what} must be a string, not {type(value).__name__}."
    if value.casefold().startswith(_REF_NAMESPACE_PREFIX):
        return (
            f"{what} names a reference ('{_REF_NAMESPACE_PREFIX}...'), not a "
            "branch, so its identity would be ambiguous."
        )
    try:
        validate_branch_name(value)
    except BranchNameError as exc:
        return f"{what} is not a valid Git branch name: {exc}"
    return ""


def _name(value: object, what: str) -> Optional[str]:
    """The usable branch name, verbatim, or ``None`` when it is not one."""
    return None if _problem(value, what) else str(value)


def _key(name: str) -> str:
    """The comparison key of a usable branch name: ``strip().casefold()``.

    The trim is idempotent for every name that reaches a comparison, because a
    name with surrounding whitespace is refused as an invalid identity first; it
    is here so the one normalisation rule is stated in exactly one place. A key
    is only ever compared - a branch name is never rewritten into another name.
    """
    return name.strip().casefold()


def _normalized(value: Sequence[str]) -> Tuple[str, ...]:
    """The comparison keys of an already-validated protected-branch sequence."""
    return tuple(_key(str(item)) for item in value)


def _policy_problem(policy: ProtectedBranchPolicy) -> Optional[str]:
    """The deterministic reason a policy is unusable, or ``None`` when usable.

    The protected-branch set is validated first (so its message is decisive),
    then the AI-prefix flag. Every entry must be a usable branch name and no two
    entries may share a comparison key: a duplicate is an ambiguous
    configuration, not a tolerance to ignore.
    """
    value = policy.protected_branches
    if value is None:
        return (
            "No protected_branches sequence is configured: None never means "
            "'nothing is protected', so an unusable set is refused."
        )
    if isinstance(value, (str, bytes, bytearray, Mapping, set, frozenset)) or (
        not isinstance(value, Sequence)
    ):
        return (
            "protected_branches must be an ordered sequence of branch names, "
            f"not {type(value).__name__}: a set or mapping has no "
            "deterministic order."
        )
    seen: List[str] = []
    for index, item in enumerate(value, start=1):
        problem = _problem(item, f"protected branch entry {index}")
        if problem:
            return problem
        key = _key(str(item))
        if key in seen:
            return (
                f"protected_branches repeats '{key}': a protected branch "
                "listed twice is an ambiguous configuration, so it is refused "
                "instead of being deduplicated silently."
            )
        seen.append(key)
    flag = policy.require_ai_fix_branch_prefix
    if not isinstance(flag, bool):
        return (
            "require_ai_fix_branch_prefix must be a bool, not "
            f"{type(flag).__name__}."
        )
    return None


# ---------------------------------------------------------------------------
# The immutable contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedBranchPolicy:
    """T26 configuration: which branches an AI-generated fix may never touch.

    Attributes:
        protected_branches: the protected branch names. The default mirrors
            T20/T21's list. ``None``, a bare string, a mapping, a set and every
            non-sequence are refused (``INVALID_POLICY``); ``None`` in particular
            never means "protect nothing". Every entry must be a usable branch
            name and no two entries may share a comparison key. An empty tuple is
            valid and strict: it protects no *named* branch, while the default
            branch and (by default) the AI-fix namespace are still enforced.
        require_ai_fix_branch_prefix: when True (the default) only a branch
            inside the T07 namespace ``ai/sonar-fix/`` may be mutated. A real
            ``bool`` is required, because any other value would silently decide
            a safety rule by truthiness.
    """

    protected_branches: Sequence[str] = PROTECTED_BRANCH_NAMES
    require_ai_fix_branch_prefix: bool = True

    @property
    def protected_branch_keys(self) -> Tuple[str, ...]:
        """The comparison keys of the configured protected branches.

        ``()`` when the configuration is unusable - which the evaluation reports
        as ``INVALID_POLICY``, never as "nothing is protected".
        """
        if self.refusal_reason is not None:
            return ()
        return _normalized(self.protected_branches)

    @property
    def is_valid(self) -> bool:
        """True only when the configuration states a usable protection rule."""
        return self.refusal_reason is None

    @property
    def refusal_reason(self) -> Optional[str]:
        """Why the configuration is unusable, or ``None`` when it is usable."""
        return _policy_problem(self)

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the configuration.

        Only *validated* values are published: an unusable set is reported as
        ``None`` and a non-bool flag as ``None``, so caller-authored text can
        never be serialized here (the refusal reason names its type instead).
        """
        flag = self.require_ai_fix_branch_prefix
        return {
            "policy_version": POLICY_VERSION,
            "protected_branches": (
                list(self.protected_branch_keys) if self.is_valid else None
            ),
            "require_ai_fix_branch_prefix": (
                flag if isinstance(flag, bool) else None
            ),
            "ai_fix_branch_prefix": AI_FIX_BRANCH_PREFIX,
            "is_valid": self.is_valid,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class BranchProtectionInput:
    """The caller-asserted branch facts T26 judges.

    T26 never repairs this record: an unusable field is refused, not fixed. Both
    fields are **caller-asserted** - see the trust boundary in the module
    docstring.

    Attributes:
        candidate_branch: the branch an AI operation intends to mutate, as the
            caller names it. ``None``, a non-string, an empty string, a
            whitespace-padded or whitespace-only value, a ref-like value
            (``refs/...``), ``HEAD``/``@`` and every malformed Git ref are
            refused (``INVALID_BRANCH``); nothing is trimmed, repaired or
            rewritten.
        default_branch: the repository's actual default branch, supplied
            explicitly by the caller because T26 does not discover it. ``None``,
            a non-string and every unusable name are refused
            (``MISSING_DEFAULT_BRANCH``): a missing default branch is never read
            as "there is no default branch" and is never inferred.
    """

    candidate_branch: Optional[str] = None
    default_branch: Optional[str] = None

    @property
    def is_usable_candidate(self) -> bool:
        """True only when ``candidate_branch`` is a usable branch identity."""
        return _name(self.candidate_branch, "candidate_branch") is not None

    @property
    def is_usable_default_branch(self) -> bool:
        """True only when ``default_branch`` is a usable branch identity."""
        return _name(self.default_branch, "default_branch") is not None

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free view of the caller's facts.

        Only *validated* names are published; an unusable value is reported as
        ``None`` with its usability flag ``False``, so caller-authored text can
        never be serialized here.
        """
        candidate = _name(self.candidate_branch, "candidate_branch")
        default = _name(self.default_branch, "default_branch")
        return {
            "candidate_branch": candidate,
            "default_branch": default,
            "is_usable_candidate": candidate is not None,
            "is_usable_default_branch": default is not None,
        }


@dataclass(frozen=True)
class BranchProtectionEvaluation:
    """The deterministic, fail-closed verdict for one policy/branch pair.

    Attributes:
        policy_version: the T26 contract version that produced the verdict.
        status: why the branch was allowed or refused.
        decision: ``ALLOW`` only for :data:`ALLOWED_STATUSES`, else ``REFUSE``.
        candidate_branch: the validated candidate, verbatim, or ``None`` when the
            caller's value was unusable.
        default_branch: the validated default branch, verbatim, or ``None`` when
            it was unusable (a missing default branch is a refusal).
        branch_in_ai_namespace: True when the candidate is usable and starts with
            ``ai/sonar-fix/``.
        branch_is_default: True when both names are usable and share a comparison
            key.
        branch_is_protected: True when the candidate is usable and the (usable)
            protected set contains its comparison key.
        reason: the decisive, deterministic explanation.
        reasons: the ordered explanation trail (``reason`` plus its context).
        policy: the configuration the verdict used, so it explains itself.

    ``branch_in_ai_namespace``, ``branch_is_default`` and ``branch_is_protected``
    are *facts about the inputs*, not restatements of ``status``: a branch can be
    both protected and the default branch, and :data:`PRECEDENCE` names only the
    first applicable refusal. The invariants that must hold are pinned by tests,
    for example ``status is ALLOWED`` implies all three are False except the
    namespace flag (which may only be False when the namespace rule is disabled).
    """

    policy_version: str
    status: BranchProtectionStatus
    decision: BranchProtectionDecision
    candidate_branch: Optional[str]
    default_branch: Optional[str]
    branch_in_ai_namespace: bool
    branch_is_default: bool
    branch_is_protected: bool
    reason: str
    reasons: Tuple[str, ...]
    policy: ProtectedBranchPolicy

    @property
    def is_allowed(self) -> bool:
        """True only when the branch may receive an AI-generated fix.

        This is the "allowed" flag. It is derived from ``status`` and never
        stored separately, so a refusal can never report ``allowed == True`` and
        an allowed verdict can never carry a refusal status.
        """
        return self.decision is BranchProtectionDecision.ALLOW

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the verdict.

        Only validated branch names are published; a name that failed validation
        is reported as ``None``, so caller-authored text can never be serialized
        here. T26 emits no command, no argv and no environment, so nothing in
        this mapping can be executed.
        """
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "decision": self.decision.value,
            "is_allowed": self.is_allowed,
            "candidate_branch": self.candidate_branch,
            "default_branch": self.default_branch,
            "branch_in_ai_namespace": self.branch_in_ai_namespace,
            "branch_is_default": self.branch_is_default,
            "branch_is_protected": self.branch_is_protected,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "policy": self.policy.as_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluation (pure, no I/O, never mutates the caller's values)
# ---------------------------------------------------------------------------


def _evaluation(
    policy: ProtectedBranchPolicy,
    status: BranchProtectionStatus,
    reason: str,
    reasons: Tuple[str, ...],
    *,
    candidate: Optional[str],
    default: Optional[str],
    keys: Tuple[str, ...],
) -> BranchProtectionEvaluation:
    """Assemble the verdict from the validated names and the status."""
    allowed = status in ALLOWED_STATUSES
    return BranchProtectionEvaluation(
        policy_version=POLICY_VERSION,
        status=status,
        decision=(
            BranchProtectionDecision.ALLOW
            if allowed
            else BranchProtectionDecision.REFUSE
        ),
        candidate_branch=candidate,
        default_branch=default,
        branch_in_ai_namespace=(
            candidate is not None
            and candidate.startswith(AI_FIX_BRANCH_PREFIX + "/")
        ),
        branch_is_default=(
            candidate is not None
            and default is not None
            and _key(candidate) == _key(default)
        ),
        branch_is_protected=(
            candidate is not None and _key(candidate) in keys
        ),
        reason=reason,
        reasons=reasons,
        policy=policy,
    )


def evaluate_branch_protection(
    *,
    policy: ProtectedBranchPolicy,
    branch: BranchProtectionInput,
) -> BranchProtectionEvaluation:
    """Decide whether a branch may receive an AI-generated fix.

    Pure and deterministic: it reads only its two arguments, writes nothing,
    remembers nothing and counts nothing, and it performs no filesystem,
    network, subprocess or Git operation. The check order is
    :data:`PRECEDENCE`, and every uncertainty is a refusal: an unusable
    configuration, an unusable candidate and a missing default branch all refuse
    before any classification is attempted.

    Args:
        policy: the operator's :class:`ProtectedBranchPolicy`.
        branch: the caller-asserted :class:`BranchProtectionInput` - the branch
            the operation intends to mutate plus the repository's default branch
            (which T26 never discovers).

    Returns:
        An immutable :class:`BranchProtectionEvaluation`. Evaluating the same
        pair again returns an equal verdict, and no caller value is mutated.

    Raises:
        TypeError: ``policy`` is not a :class:`ProtectedBranchPolicy`, or
            ``branch`` is not a :class:`BranchProtectionInput`. That is a caller
            error, not a branch problem - every branch problem is a refusal in
            the returned evaluation.
    """
    if not isinstance(policy, ProtectedBranchPolicy):
        raise TypeError(
            "policy must be a ProtectedBranchPolicy, not "
            f"{type(policy).__name__}."
        )
    if not isinstance(branch, BranchProtectionInput):
        raise TypeError(
            "branch must be a BranchProtectionInput, not "
            f"{type(branch).__name__}."
        )

    # The names are read once, verbatim, and validated once. Nothing below
    # repairs, trims, renames or reorders them.
    candidate = _name(branch.candidate_branch, "candidate_branch")
    default = _name(branch.default_branch, "default_branch")
    refusal = policy.refusal_reason
    keys = () if refusal is not None else policy.protected_branch_keys
    require_prefix = (
        refusal is None and policy.require_ai_fix_branch_prefix is True
    )

    # 1. A policy that cannot state what is protected authorises no mutation.
    if refusal is not None:
        return _evaluation(
            policy,
            BranchProtectionStatus.INVALID_POLICY,
            refusal,
            (
                refusal,
                "The branch was not judged: T26 fails closed when its "
                "protection configuration is unusable.",
            ),
            candidate=candidate,
            default=default,
            keys=(),
        )

    # 2. Without a usable candidate branch there is nothing to authorise - and
    #    an unusable identity is refused, never repaired.
    if candidate is None:
        reason = _problem(branch.candidate_branch, "candidate_branch")
        return _evaluation(
            policy,
            BranchProtectionStatus.INVALID_BRANCH,
            reason,
            (
                reason,
                "T26 never repairs a branch name: an unusable or ambiguous "
                "identity is refused, not normalised.",
            ),
            candidate=None,
            default=default,
            keys=keys,
        )

    # 3. Without a usable default branch the candidate cannot be shown to be a
    #    different branch, so nothing is authorised. Precedence note: this is
    #    checked *before* the protected/default classification, so a candidate
    #    named 'main' with a missing default branch is reported as
    #    MISSING_DEFAULT_BRANCH rather than PROTECTED_BRANCH - see PRECEDENCE.
    if default is None:
        reason = _problem(branch.default_branch, "default_branch")
        return _evaluation(
            policy,
            BranchProtectionStatus.MISSING_DEFAULT_BRANCH,
            reason,
            (
                reason,
                "T26 does not discover the default branch: it must be supplied "
                "explicitly, and a missing default branch is never read as "
                "permission to mutate.",
            ),
            candidate=candidate,
            default=None,
            keys=keys,
        )

    # 4. A configured protected branch: one comparison key decides it, so case
    #    and spacing variants cannot slip past the set.
    if _key(candidate) in keys:
        reason = (
            f"'{candidate}' is a protected branch, so an AI-generated fix must "
            "never be committed or pushed to it."
        )
        return _evaluation(
            policy,
            BranchProtectionStatus.PROTECTED_BRANCH,
            reason,
            (
                reason,
                "Only the operator's protected_branches configuration can "
                "change this; T26 never rewrites the branch name to escape it.",
            ),
            candidate=candidate,
            default=default,
            keys=keys,
        )

    # 5. The repository's own default branch, whatever it is called.
    if _key(candidate) == _key(default):
        reason = (
            f"'{candidate}' is the repository's default branch, so an "
            "AI-generated fix must never be committed or pushed to it."
        )
        return _evaluation(
            policy,
            BranchProtectionStatus.DEFAULT_BRANCH,
            reason,
            (
                reason,
                "The default branch is refused however it is named: T26 "
                "compares the candidate against the supplied default branch.",
            ),
            candidate=candidate,
            default=default,
            keys=keys,
        )

    # 6. The T07 AI-fix namespace, when the operator requires it (the default).
    if require_prefix and not candidate.startswith(AI_FIX_BRANCH_PREFIX + "/"):
        reason = (
            f"'{candidate}' is outside the AI-fix branch namespace "
            f"'{AI_FIX_BRANCH_PREFIX}/', which T26 requires, so it is not a "
            "branch this tool may mutate."
        )
        return _evaluation(
            policy,
            BranchProtectionStatus.WRONG_BRANCH_NAMESPACE,
            reason,
            (
                reason,
                "T26 creates no branch: T07 owns branch creation, and an "
                "existing branch outside the namespace is simply refused.",
            ),
            candidate=candidate,
            default=default,
            keys=keys,
        )

    # 7. Allowed: a valid, non-protected, non-default branch inside the
    #    namespace - or anywhere, when the namespace requirement is disabled.
    if require_prefix:
        rule = (
            f"it is inside the AI-fix branch namespace "
            f"'{AI_FIX_BRANCH_PREFIX}/'."
        )
    else:
        rule = (
            "the AI-fix namespace requirement is disabled by configuration, "
            "so any valid branch that is neither protected nor the default "
            "branch is eligible."
        )
    reason = (
        f"'{candidate}' is not a protected branch and is not the default "
        f"branch '{default}': {rule}"
    )
    return _evaluation(
        policy,
        BranchProtectionStatus.ALLOWED,
        reason,
        (
            reason,
            "T26 does not prove the branch exists or is checked out: the "
            "caller must still enforce this verdict (see the trust boundary).",
        ),
        candidate=candidate,
        default=default,
        keys=keys,
    )


__all__: Sequence[str] = (
    "AI_FIX_BRANCH_PREFIX",
    "ALLOWED_STATUSES",
    "BranchProtectionDecision",
    "BranchProtectionEvaluation",
    "BranchProtectionInput",
    "BranchProtectionStatus",
    "POLICY_VERSION",
    "PRECEDENCE",
    "PROTECTED_BRANCH_NAMES",
    "ProtectedBranchPolicy",
    "evaluate_branch_protection",
)

