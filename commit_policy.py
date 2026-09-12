"""Pure commit-gate policy for T20 (no subprocess, no Git, no I/O).

This module is the *decision* half of T20. It answers one question:

    "may this exact change be staged and committed, and if not, which gate
    refused it?"

Everything it needs is passed in as an immutable record (:class:`CommitFacts`)
that the executor (`git_commit.py`, the only module allowed to write to Git)
filled from already-collected facts: the T19 result, the T11/T13/T15/T16/T17/T18
sub-results, the T20 baseline/attribution snapshots, the secret scans and the
Git observation records. Nothing here runs a command, reads a file or mutates
any state, so a gate evaluation can be replayed and unit tested in isolation.

Fail closed
-----------
A gate produces an explicit :class:`GateStatus` and a human-readable reason.
Missing, unknown, unreadable, ambiguous or unexpected information is a ``FAIL``
- never a silent pass. Every gate whose phase has been reached is evaluated, so
the report identifies *every* refusal instead of only the first one. The
:class:`CommitDecision` is ``REFUSE`` as soon as one evaluated gate fails.

The 45 gates (see :data:`GATES` for the authoritative titles):

* ``G1`` inputs, ``G2``-``G3`` T19 status and review condition, ``G4``-``G6``
  Codex execution/uncertainty/review-request, ``G7``-``G9`` scope/target/tests,
  ``G10``-``G14`` analysis and verification, ``G15``-``G18`` attribution and
  baseline, ``G19``-``G24`` approved-set resolution and path safety,
  ``G25``-``G28`` binary/secret scans, ``G29``-``G36`` repository, environment,
  branch, identity, in-progress state and hooks, ``G37``-``G41`` HEAD and staged
  content identity, ``G42`` commit message, ``G43``-``G45`` commit outcome and
  post-commit proof.

Gate ids are stable and part of the T20 contract (the report and the tests use
them).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from change_scope import normalise_relative_path
from commit_message import CommitMessage, validate_commit_message
from issue_status import IssueFinalStatus
from secret_scan import SecretScanResult
from worktree_baseline import BaselineAttribution, WorktreeSnapshot

#: Every gate, in evaluation order: ``(gate id, short title)``.
GATES: Tuple[Tuple[str, str], ...] = (
    ("G1", "inputs complete"),
    ("G2", "T19 status == FIXED"),
    ("G3", "no review required"),
    ("G4", "Codex succeeded"),
    ("G5", "no Codex uncertainty"),
    ("G6", "no Codex review request"),
    ("G7", "change scope valid"),
    ("G8", "target changed"),
    ("G9", "tests passed"),
    ("G10", "analysis succeeded"),
    ("G11", "analysis task id matches T16"),
    ("G12", "analysis correlated"),
    ("G13", "reliable absence proven"),
    ("G14", "no positive finding"),
    ("G15", "change attribution valid"),
    ("G16", "baseline clean"),
    ("G17", "no pre-existing target modification"),
    ("G18", "whole-tree changes accounted for"),
    ("G19", "approved change is non-empty"),
    ("G20", "approved file set exactly resolved"),
    ("G21", "target deletion rejected"),
    ("G22", "rename rejected"),
    ("G23", "all paths safe"),
    ("G24", "approved paths are not ignored"),
    ("G25", "approved files are not binary"),
    ("G26", "approved file content secret scan passes"),
    ("G27", "approved worktree diff secret scan passes"),
    ("G28", "staged diff secret scan passes"),
    ("G29", "repository identity valid"),
    ("G30", "Git environment safe"),
    ("G31", "HEAD is not detached"),
    ("G32", "current branch is not protected/default/reserved"),
    ("G33", "explicit Git identity exists"),
    ("G34", "no Git operation in progress"),
    ("G35", "index has no unmerged entries"),
    ("G36", "no unaccounted non-bypassable hooks"),
    ("G37", "HEAD unchanged since baseline"),
    ("G38", "worktree matches approved content"),
    ("G39", "staged path set exact"),
    ("G40", "staged content == approved worktree content"),
    ("G41", "staged content unchanged since staging"),
    ("G42", "commit message valid"),
    ("G43", "commit succeeds"),
    ("G44", "post-commit state verified"),
    ("G45", "no unexpected Git error"),
)

#: Branches T20 must never commit to, regardless of configuration.
PROTECTED_BRANCH_NAMES: Tuple[str, ...] = ("main", "master", "develop", "trunk")

#: Namespace T07 creates agent branches in. A commit on any other branch means
#: T20 is running somewhere it was not invited, so it refuses - T07 owns branch
#: creation and T20 never creates or checks out a branch.
DEFAULT_AGENT_BRANCH_PREFIX = "ai/sonar-fix"

#: Phrases that mean "a human must review this before it becomes history".
REVIEW_REQUEST_MARKERS: Tuple[str, ...] = (
    "request review",
    "requested review",
    "requesting review",
    "please review",
    "review required",
    "requires review",
    "needs review",
    "human review",
    "manual review",
    "needs approval",
    "requires approval",
)

#: Git environment variables that can redirect the commit to another
#: repository/index/object store, move the commit identity, or execute code
#: while Git runs. Any of these being set (non-empty) is an unsafe environment
#: for T20.
#:
#: Deliberately *not* listed: helpers that cannot redirect or forge anything
#: (``GIT_ASKPASS``, ``GIT_EDITOR``, ``GIT_SEQUENCE_EDITOR``, ``GIT_PAGER``).
#: Those are commonly set by IDEs and shells, and T20 never takes an interactive
#: or network path that could reach them - it always passes ``-m`` and performs
#: no fetch/push/pull. Refusing on them would only produce false refusals.
UNSAFE_GIT_ENVIRONMENT_VARIABLES: Tuple[str, ...] = (
    # Redirect the repository / index / object store.
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    # Inject configuration or execute a program while Git runs.
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_SSH_COMMAND",
    "GIT_SSH",
    "GIT_EXTERNAL_DIFF",
    # Forge or override the commit identity.
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)

#: Hooks that can change the commit, the message or the tree. T20 never passes
#: ``--no-verify``, so an active hook it cannot account for is a refusal.
ACCOUNTED_HOOK_NAMES: Tuple[str, ...] = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
)

#: Pathspec metacharacters. A legitimate SonarQube file path never needs them,
#: and Git would interpret them as patterns instead of literal paths.
_PATHSPEC_MAGIC_CHARS = frozenset("*?[]{}!\\")

#: Git state files that mean another operation is in progress.
OPERATION_IN_PROGRESS_MARKERS: Tuple[str, ...] = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)

#: Default cap on how many files one T20 commit may contain. A SonarQube fix is
#: a one-issue change; a bigger set means the approved scope was not resolved.
DEFAULT_MAX_APPROVED_FILES = 50

#: Reason recorded for a gate whose state-machine stage has not run yet.
NOT_REACHED_REASON = (
    "Not reached: this gate belongs to a later T20 stage than the one "
    "evaluated."
)


class GateStatus(Enum):
    """Outcome of a single gate."""

    __test__ = False

    PASS = "pass"
    FAIL = "fail"
    #: The gate belongs to a later state-machine stage than the one evaluated.
    NOT_REACHED = "not-reached"


class CommitDecision(Enum):
    """Overall decision of one gate evaluation (fail closed)."""

    __test__ = False

    PROCEED = "proceed"
    REFUSE = "refuse"


class CommitPhase(Enum):
    """State-machine stage that supplies the facts for a group of gates."""

    __test__ = False

    #: S0 - the inputs T20 was handed.
    INPUTS = "inputs"
    #: S0/S7 - the T19 fix evidence and the worktree baseline/attribution.
    FIX_EVIDENCE = "fix-evidence"
    #: S1-S6 - the resolved repository, environment, branch, identity, state,
    #: hooks and HEAD. Established (and enforced) *before* the approved set is
    #: resolved, exactly as S1-S5 precede S7 in the state machine.
    REPOSITORY = "repository"
    #: S7/S8 - the immutable approved file set and its content scans.
    APPROVAL = "approved-set"
    #: S9-S11 - exact staging, index verification and the staged diff scan.
    STAGING = "staging"
    #: S12 - the final pre-commit check (message + staged content re-verified).
    COMMIT = "commit"
    #: S13/S14 - the single commit attempt and the post-commit proof.
    VERIFY = "verify"


#: Which phase introduces each gate. A gate is ``NOT_REACHED`` until its phase
#: has been evaluated, and ``FAIL`` (fail closed) once it has.
_GATE_PHASE: Mapping[str, CommitPhase] = {
    "G1": CommitPhase.INPUTS,
    **{f"G{i}": CommitPhase.FIX_EVIDENCE for i in range(2, 19)},
    **{f"G{i}": CommitPhase.APPROVAL for i in range(19, 28)},
    **{f"G{i}": CommitPhase.REPOSITORY for i in range(29, 38)},
    "G28": CommitPhase.STAGING,
    **{f"G{i}": CommitPhase.STAGING for i in range(38, 41)},
    # G41 re-verifies the staged/worktree content immediately before the commit,
    # so it belongs to the final pre-commit check, not to the staging stage.
    "G41": CommitPhase.COMMIT,
    "G42": CommitPhase.COMMIT,
    **{f"G{i}": CommitPhase.VERIFY for i in range(43, 46)},
}

#: Phase evaluation order (definition order of :class:`CommitPhase`).
_PHASE_ORDER: Tuple[CommitPhase, ...] = tuple(CommitPhase)


class CommitPolicyError(ValueError):
    """Raised when the policy is asked to evaluate something impossible."""


def _phase_index(phase: CommitPhase) -> int:
    """Position of ``phase`` in the evaluation order."""
    return _PHASE_ORDER.index(phase)


@dataclass(frozen=True)
class CommitPolicyConfig:
    """Tunable, explicit T20 policy. Every default is the conservative choice.

    Attributes:
        protected_branches: branch names T20 never commits to.
        default_branch: the repository's default branch (refused as well, so a
            rename of ``main`` is still refused).
        required_branch_prefix: the agent-branch namespace T07 creates; a branch
            outside it is refused (T20 never creates or switches branches).
        require_explicit_identity: refuse unless ``user.name``/``user.email``
            are explicitly configured (never fall back to OS/domain identity).
        require_clean_baseline: refuse unless the pre-run tree was clean.
        allow_ignored_approved_paths: never ``True`` in production; exists only
            so a test can prove the ignored-file gate fires. ``False`` refuses
            ignored approved paths.
        max_approved_files: cap on the size of the approved set.
        max_message_length: cap handed to the commit-message validator.
        extra_allowed_files: additional repository-relative files the change
            scope explicitly proved approved (empty by default: a T20 commit is
            the issue's own file unless T13 proved more).
    """

    protected_branches: Tuple[str, ...] = PROTECTED_BRANCH_NAMES
    default_branch: Optional[str] = None
    required_branch_prefix: str = DEFAULT_AGENT_BRANCH_PREFIX
    require_explicit_identity: bool = True
    require_clean_baseline: bool = True
    allow_ignored_approved_paths: bool = False
    max_approved_files: int = DEFAULT_MAX_APPROVED_FILES
    max_message_length: int = 200
    extra_allowed_files: Tuple[str, ...] = ()

    def is_protected_branch(self, branch: Optional[str]) -> bool:
        """True when ``branch`` may never receive a T20 commit."""
        if not branch:
            return True
        name = str(branch).strip()
        protected = {item.casefold() for item in self.protected_branches}
        if self.default_branch:
            protected.add(str(self.default_branch).casefold())
        return name.casefold() in protected

    def is_agent_branch(self, branch: Optional[str]) -> bool:
        """True when ``branch`` is inside the configured agent namespace."""
        if not branch or not self.required_branch_prefix:
            return False
        prefix = self.required_branch_prefix.rstrip("/") + "/"
        return str(branch).startswith(prefix)


@dataclass(frozen=True)
class RepositoryIdentity:
    """What T20 resolved about the repository before any mutation.

    ``matches_expected`` is ``True`` only when the resolved worktree root, Git
    directory and index location all agree with the caller-supplied path, so a
    ``GIT_DIR``-style redirect or a symlinked path cannot move the commit
    somewhere else.
    """

    expected_root: str = ""
    worktree_root: Optional[str] = None
    git_dir: Optional[str] = None
    common_dir: Optional[str] = None
    index_path: Optional[str] = None
    head_commit: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = True
    matches_expected: bool = False
    index_locked: bool = False
    problems: Tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """True only when every identity part was resolved and agrees."""
        return (
            not self.problems
            and self.matches_expected
            and bool(self.worktree_root)
            and bool(self.git_dir)
            and bool(self.common_dir)
            and bool(self.index_path)
            and bool(self.head_commit)
            and not self.detached
            and bool(self.branch)
        )

    def as_dict(self) -> dict:
        """Secret-free summary."""
        return {
            "expected_root": self.expected_root,
            "worktree_root": self.worktree_root,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "index_path": self.index_path,
            "head_commit": self.head_commit,
            "branch": self.branch,
            "detached": self.detached,
            "matches_expected": self.matches_expected,
            "index_locked": self.index_locked,
            "is_valid": self.is_valid,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class GitEnvironmentReport:
    """Which Git environment variables were inspected and which are unsafe."""

    unsafe_variables: Tuple[str, ...] = ()
    checked_variables: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_safe(self) -> bool:
        """True only when no redirecting/executing variable was set."""
        return not self.unsafe_variables

    def as_dict(self) -> dict:
        """Secret-free summary (variable *names* only, never their values)."""
        return {
            "unsafe_variables": list(self.unsafe_variables),
            "checked_variables": list(self.checked_variables),
            "is_safe": self.is_safe,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GitIdentityReport:
    """The Git identity T20 verified before staging.

    ``explicit`` is ``True`` only when ``user.name`` and ``user.email`` are
    configured *and* the identity Git would actually use (``git var``) is
    exactly those values. A fabricated OS/domain identity therefore never
    counts as explicit.
    """

    name: Optional[str] = None
    email: Optional[str] = None
    actor_name: Optional[str] = None
    actor_email: Optional[str] = None
    committer_name: Optional[str] = None
    committer_email: Optional[str] = None
    use_config_only: Optional[str] = None
    explicit: bool = False
    problems: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        """Secret-free summary.

        The identity is intentionally reported: ``user.email`` is repository
        configuration, not a credential, and T20's proof depends on it. No
        credential (token, password, URL userinfo) is ever part of it.
        """
        return {
            "name": self.name,
            "email": self.email,
            "actor_name": self.actor_name,
            "actor_email": self.actor_email,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "use_config_only": self.use_config_only,
            "explicit": self.explicit,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class HookReport:
    """The repository hooks T20 found, and whether it can account for them.

    T20 never bypasses hooks (no ``--no-verify``), so an active hook in
    :data:`ACCOUNTED_HOOK_NAMES` is a refusal: it could rewrite the message, the
    index or the tree in a way T20 cannot verify.
    """

    hooks_path: Optional[str] = None
    active_hooks: Tuple[str, ...] = ()
    inspected_hooks: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_accounted_for(self) -> bool:
        """True only when no active hook T20 cannot control was found."""
        return not self.active_hooks

    def as_dict(self) -> dict:
        """Secret-free summary."""
        return {
            "hooks_path": self.hooks_path,
            "active_hooks": list(self.active_hooks),
            "inspected_hooks": list(self.inspected_hooks),
            "is_accounted_for": self.is_accounted_for,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ApprovedFileSet:
    """The immutable result of resolving the approved file set (once).

    Attributes:
        paths: the approved repository-relative paths (sorted, normalized).
        expected: the allowed scope T13/T19 proved (the issue's file plus any
            explicitly proven extra file).
        agent_files: the paths the run introduced (from the T20 attribution).
        outside_scope: agent files that are *not* in ``expected``.
        missing_files: expected files that the run did not change.
        problems: why the set is not exactly resolvable (extra/missing file,
            unsafe path, size cap, ...).
        reasons: ordered, human-readable explanations.
    """

    paths: Tuple[str, ...] = ()
    expected: Tuple[str, ...] = ()
    agent_files: Tuple[str, ...] = ()
    outside_scope: Tuple[str, ...] = ()
    missing_files: Tuple[str, ...] = ()
    problems: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when nothing may be committed."""
        return not self.paths

    @property
    def exactly_resolved(self) -> bool:
        """True only when the set is non-empty, safe and exactly the change."""
        return (
            bool(self.paths)
            and not self.problems
            and not self.outside_scope
            and set(self.paths) == set(self.agent_files)
            and set(self.paths).issubset(set(self.expected))
        )

    @property
    def reason_text(self) -> str:
        """All reasons joined into a single readable paragraph."""
        return " ".join(self.reasons)

    def as_dict(self) -> dict:
        """Secret-free summary."""
        return {
            "paths": list(self.paths),
            "expected": list(self.expected),
            "agent_files": list(self.agent_files),
            "outside_scope": list(self.outside_scope),
            "missing_files": list(self.missing_files),
            "problems": list(self.problems),
            "is_empty": self.is_empty,
            "exactly_resolved": self.exactly_resolved,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class StageObservation:
    """What the staging stage produced (S9-S11), recorded for the gates.

    ``staged_blob_ids`` are the index blob ids read back with
    ``git ls-files -s -- <path>``; ``worktree_content_ids`` are re-read with
    ``git hash-object --path=<path> -- <path>`` *after* staging, and
    ``approved_content_ids`` were recorded when the set was approved. G38-G41
    compare those three maps.
    """

    attempted: bool = False
    error: Optional[str] = None
    approved_paths: Tuple[str, ...] = ()
    staged_paths: Tuple[str, ...] = ()
    staged_blob_ids: Mapping[str, str] = field(default_factory=dict)
    worktree_content_ids: Mapping[str, str] = field(default_factory=dict)
    approved_content_ids: Mapping[str, str] = field(default_factory=dict)
    head_before: Optional[str] = None
    head_after: Optional[str] = None
    unmerged_paths: Tuple[str, ...] = ()
    secret_scan: Optional[SecretScanResult] = None
    problems: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        """Secret-free summary."""
        return {
            "attempted": self.attempted,
            "error": self.error,
            "approved_paths": list(self.approved_paths),
            "staged_paths": list(self.staged_paths),
            "staged_blob_ids": dict(self.staged_blob_ids),
            "worktree_content_ids": dict(self.worktree_content_ids),
            "approved_content_ids": dict(self.approved_content_ids),
            "head_before": self.head_before,
            "head_after": self.head_after,
            "unmerged_paths": list(self.unmerged_paths),
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class CommitObservation:
    """What the single commit attempt produced (S13/S14).

    Every ``expected_*`` field is what T20 proved *before* the commit; the
    post-commit verification compares observed values against them. Anything
    unobservable stays ``None``, which fails closed.
    """

    attempted: bool = False
    error: Optional[str] = None
    committed: bool = False
    previous_head: Optional[str] = None
    expected_previous_head: Optional[str] = None
    new_head: Optional[str] = None
    parent_head: Optional[str] = None
    new_commit_count: Optional[int] = None
    message: Optional[str] = None
    expected_message: Optional[str] = None
    author_name: Optional[str] = None
    author_email: Optional[str] = None
    committer_name: Optional[str] = None
    committer_email: Optional[str] = None
    expected_identity_name: Optional[str] = None
    expected_identity_email: Optional[str] = None
    committed_paths: Tuple[str, ...] = ()
    expected_paths: Tuple[str, ...] = ()
    committed_blob_ids: Mapping[str, str] = field(default_factory=dict)
    expected_blob_ids: Mapping[str, str] = field(default_factory=dict)
    deleted_paths: Tuple[str, ...] = ()
    renamed_paths: Tuple[str, ...] = ()
    branch_after: Optional[str] = None
    expected_branch: Optional[str] = None
    worktree_root_after: Optional[str] = None
    expected_worktree_root: Optional[str] = None
    unexpected_errors: Tuple[str, ...] = ()
    problems: Tuple[str, ...] = ()

    @property
    def identity_matches(self) -> bool:
        """True when author and committer are exactly the verified identity."""
        return (
            bool(self.expected_identity_name)
            and bool(self.expected_identity_email)
            and self.author_name == self.expected_identity_name
            and self.author_email == self.expected_identity_email
            and self.committer_name == self.expected_identity_name
            and self.committer_email == self.expected_identity_email
        )

    def as_dict(self) -> dict:
        """Secret-free summary."""
        return {
            "attempted": self.attempted,
            "committed": self.committed,
            "error": self.error,
            "previous_head": self.previous_head,
            "new_head": self.new_head,
            "parent_head": self.parent_head,
            "new_commit_count": self.new_commit_count,
            "expected_message": self.expected_message,
            "message_matches": self.message == self.expected_message,
            "identity_matches": self.identity_matches,
            "committed_paths": list(self.committed_paths),
            "expected_paths": list(self.expected_paths),
            "deleted_paths": list(self.deleted_paths),
            "renamed_paths": list(self.renamed_paths),
            "branch_after": self.branch_after,
            "unexpected_errors": list(self.unexpected_errors),
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class CommitFacts:
    """Everything the gates may look at, as recorded by the executor.

    Every field is optional: the executor fills only what its current stage has
    established, and a gate whose fact is missing fails closed. The type
    annotations name the real T01-T19 records (resolved lazily as strings, so
    this module never depends on the HTTP layer at import time).
    """

    __test__ = False

    # --- inputs (G1) -------------------------------------------------------
    issue_status: Optional["IssueStatusResult"] = None
    codex: Optional["CodexResultAnalysis"] = None
    tests: Optional["TestOutcome"] = None
    analysis: Optional["SonarAnalysisCompletion"] = None
    # The compact T16 evidence (T19's ``trigger_evidence``): the executor fills
    # it from the T19 result, and G11 fails closed when it is absent.
    trigger: Optional["AnalysisTriggerEvidence"] = None
    verification: Optional["SonarIssueVerificationResult"] = None
    scope: Optional["ChangeScopeResult"] = None
    attribution: Optional[BaselineAttribution] = None
    baseline: Optional[WorktreeSnapshot] = None
    after: Optional[WorktreeSnapshot] = None
    issue_key: str = ""
    rule: str = ""
    target_file: str = ""
    config: CommitPolicyConfig = field(default_factory=CommitPolicyConfig)

    # --- approved set (G19-G28) -------------------------------------------
    approved: Optional[ApprovedFileSet] = None
    binary_paths: Tuple[str, ...] = ()
    content_scan: Optional[SecretScanResult] = None
    worktree_diff_scan: Optional[SecretScanResult] = None
    staged_diff_scan: Optional[SecretScanResult] = None

    # --- repository facts (G29-G37) ---------------------------------------
    repository: Optional[RepositoryIdentity] = None
    environment: Optional[GitEnvironmentReport] = None
    identity: Optional[GitIdentityReport] = None
    hooks: Optional[HookReport] = None
    operation_in_progress: Tuple[str, ...] = ()
    unmerged_paths: Tuple[str, ...] = ()

    # --- staging facts (G38-G41) ------------------------------------------
    stage: Optional[StageObservation] = None
    pre_commit_stage: Optional[StageObservation] = None

    # --- commit facts (G42-G45) -------------------------------------------
    commit_message: Optional[CommitMessage] = None
    commit: Optional[CommitObservation] = None


@dataclass(frozen=True)
class CommitGate:
    """One evaluated gate.

    Attributes:
        gate_id: stable identifier, e.g. ``"G20"``.
        title: short human-readable title.
        status: ``PASS`` / ``FAIL`` / ``NOT_REACHED``.
        reason: why the gate has that status (never contains a secret value).
    """

    __test__ = False

    gate_id: str
    title: str
    status: GateStatus
    reason: str

    @property
    def passed(self) -> bool:
        """True only for an explicit pass."""
        return self.status is GateStatus.PASS

    @property
    def failed(self) -> bool:
        """True only for an explicit failure."""
        return self.status is GateStatus.FAIL

    def as_dict(self) -> dict:
        """Plain summary."""
        return {
            "gate_id": self.gate_id,
            "title": self.title,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GateEvaluation:
    """The outcome of evaluating every gate up to ``phase``.

    All 45 gates are always present: gates whose stage has not run are
    ``NOT_REACHED``, so the report is complete and comparable between stages.
    """

    __test__ = False

    phase: CommitPhase
    gates: Tuple[CommitGate, ...]
    decision: CommitDecision

    def status_of(self, gate_id: str) -> Optional[GateStatus]:
        """The status of ``gate_id``, or ``None`` when it is unknown."""
        for gate in self.gates:
            if gate.gate_id == gate_id:
                return gate.status
        return None

    def gate(self, gate_id: str) -> Optional[CommitGate]:
        """The :class:`CommitGate` record for ``gate_id``, or ``None``."""
        for gate in self.gates:
            if gate.gate_id == gate_id:
                return gate
        return None

    @property
    def failed_gates(self) -> Tuple[CommitGate, ...]:
        """Every gate that failed (in evaluation order)."""
        return tuple(gate for gate in self.gates if gate.failed)

    @property
    def passed_gates(self) -> Tuple[CommitGate, ...]:
        """Every gate that passed (in evaluation order)."""
        return tuple(gate for gate in self.gates if gate.passed)

    @property
    def not_reached_gates(self) -> Tuple[CommitGate, ...]:
        """Every gate whose stage has not run yet."""
        return tuple(
            gate for gate in self.gates if gate.status is GateStatus.NOT_REACHED
        )

    @property
    def first_failure(self) -> Optional[CommitGate]:
        """The first failing gate, or ``None`` when nothing failed."""
        failed = self.failed_gates
        return failed[0] if failed else None

    @property
    def refusal_reason(self) -> Optional[str]:
        """A single-line explanation of the first failure, or ``None``."""
        failure = self.first_failure
        if failure is None:
            return None
        return f"{failure.gate_id} ({failure.title}): {failure.reason}"

    @property
    def is_refusal(self) -> bool:
        """True when the decision is ``REFUSE``."""
        return self.decision is CommitDecision.REFUSE

    def as_dict(self) -> dict:
        """Secret-free, JSON-friendly report payload."""
        return {
            "phase": self.phase.value,
            "decision": self.decision.value,
            "first_failure": (
                self.first_failure.gate_id if self.first_failure else None
            ),
            "refusal_reason": self.refusal_reason,
            "passed": [gate.gate_id for gate in self.passed_gates],
            "failed": [gate.gate_id for gate in self.failed_gates],
            "not_reached": [gate.gate_id for gate in self.not_reached_gates],
            "gates": [gate.as_dict() for gate in self.gates],
        }


def normalise_approved_paths(
    paths: object,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Normalize and validate candidate approved paths (pure).

    Args:
        paths: any iterable of candidate repository-relative paths.

    Returns:
        ``(accepted, problems)`` where ``accepted`` is a sorted, de-duplicated
        tuple of normalized paths and ``problems`` explains every rejected
        entry. An entry is rejected when it is not text, empty, absolute
        (POSIX, Windows drive or UNC), points at the repository root, contains a
        ``..`` component, contains a NUL/newline/control character, starts with
        ``-`` (option injection), starts with ``:`` or contains pathspec magic
        (``*?[]{}!\\``), or points at a directory-like path (trailing ``/``).

    Raises:
        CommitPolicyError: ``paths`` is not iterable, or is a bare string (a
            single string is almost always a bug: the caller meant one path).
    """
    if isinstance(paths, (str, bytes)) or paths is None:
        raise CommitPolicyError(
            "normalise_approved_paths expects an iterable of paths, not a bare "
            f"{type(paths).__name__}."
        )
    try:
        candidates = list(paths)
    except TypeError as exc:
        raise CommitPolicyError(
            f"normalise_approved_paths expects an iterable of paths: {exc}"
        ) from exc

    accepted: Dict[str, str] = {}
    problems: list = []
    for raw in candidates:
        display = "" if raw is None else str(raw)
        if not isinstance(raw, str):
            problems.append(
                f"Candidate path {display!r} is not text and cannot be "
                "approved."
            )
            continue
        stripped = raw.strip()
        if stripped != raw:
            problems.append(
                f"Candidate path {display!r} has surrounding whitespace, so it "
                "is not a literal repository-relative path."
            )
            continue
        if not stripped:
            problems.append("Candidate path is empty.")
            continue
        if any(
            character in stripped
            for character in ("\0", "\n", "\r")
        ) or re.search(r"[\x00-\x1f\x7f]", stripped):
            problems.append(
                f"Candidate path {display!r} contains a control character."
            )
            continue
        if stripped.startswith("-"):
            problems.append(
                f"Candidate path {display!r} starts with '-' and could be read "
                "as a Git option."
            )
            continue
        if stripped.startswith(":") or ":" in stripped.split("/")[0]:
            problems.append(
                f"Candidate path {display!r} uses Git pathspec magic."
            )
            continue
        if any(character in _PATHSPEC_MAGIC_CHARS for character in stripped):
            problems.append(
                f"Candidate path {display!r} contains pathspec metacharacters."
            )
            continue
        if stripped.endswith("/"):
            problems.append(
                f"Candidate path {display!r} ends with '/' and is not a file."
            )
            continue
        normalized = normalise_relative_path(stripped)
        if normalized is None:
            problems.append(
                f"Candidate path {display!r} is not a safe "
                "repository-relative path."
            )
            continue
        # Two different candidates normalizing to the same path are fine (the
        # set is a set), but the *comparison* must be platform-aware.
        accepted.setdefault(_path_key(normalized), normalized)

    return tuple(sorted(accepted.values())), tuple(problems)


def _path_key(path: str) -> str:
    """Comparison key for an approved path (case-folded on Windows only)."""
    return path.casefold() if os.name == "nt" else path


def resolve_approved_files(
    *,
    target_file: object,
    scope: Optional["ChangeScopeResult"] = None,
    attribution: Optional[BaselineAttribution] = None,
    baseline: Optional[WorktreeSnapshot] = None,
    after: Optional[WorktreeSnapshot] = None,
    config: Optional[CommitPolicyConfig] = None,
) -> ApprovedFileSet:
    """Resolve the approved file set exactly once (pure, deterministic).

    Policy (conservative by default):

    1. The allowed scope is the issue's own target file plus any file the T13
       result explicitly proved (``scope.expected_files``) plus
       ``config.extra_allowed_files``.
    2. The only files that may be approved are the paths the T20 attribution
       credits to **this run** (``attribution.agent_files``) - never the
       post-run ``changed_files`` of the whole tree.
    3. An agent path outside the allowed scope is a problem (it is also reported
       in ``outside_scope``), so the set is *not* exactly resolved and G20
       refuses.
    4. Deleted/renamed/unreadable agents paths have no worktree content identity
       and are therefore excluded, recorded as problems (G21/G22/G38 refuse).

    Args:
        target_file: the issue's file (``IssueContext.file_path``).
        scope: the T13 result, when available.
        attribution: the T20 baseline attribution for this run.
        baseline: the pre-run snapshot (used for ignored-file accounting).
        after: the post-run snapshot (content identity evidence).
        config: policy configuration (defaults are conservative).

    Returns:
        An immutable :class:`ApprovedFileSet`. Callers must treat
        ``is_empty``/``exactly_resolved`` as the G19/G20 verdict and must not
        re-derive the set later.
    """
    policy = config or CommitPolicyConfig()
    problems: list = []
    reasons: list = []

    expected_candidates = [target_file, *policy.extra_allowed_files]
    if scope is not None and bool(getattr(scope, "is_valid", False)):
        expected_candidates.extend(getattr(scope, "expected_files", ()) or ())
    expected, expected_problems = normalise_approved_paths(expected_candidates)
    problems.extend(expected_problems)
    if not expected:
        problems.append(
            "No allowed scope could be resolved: the issue's target file is "
            "missing or unsafe."
        )

    if attribution is None:
        return ApprovedFileSet(
            paths=(),
            expected=expected,
            problems=tuple(problems)
            + ("No baseline attribution was supplied, so no change can be "
               "attributed to this run.",),
            reasons=("Without an attribution the approved set cannot be "
                     "resolved.",),
        )

    agent_raw = tuple(getattr(attribution, "agent_files", ()) or ())
    agent_files, agent_problems = normalise_approved_paths(agent_raw)
    problems.extend(agent_problems)

    expected_keys = {_path_key(path) for path in expected}
    agent_keys = {_path_key(path): path for path in agent_files}

    outside_scope = sorted(
        agent_keys[key] for key in agent_keys if key not in expected_keys
    )

    existing: Dict[str, str] = {}
    for key, path in agent_keys.items():
        if key not in expected_keys:
            continue
        if after is None:
            problems.append(
                "No post-run snapshot was supplied, so the content identity of "
                f"{path!r} cannot be proven."
            )
            continue
        content_id = after.content_id_of(path)
        if not content_id:
            problems.append(
                f"{path!r} has no Git content identity in the post-run "
                "snapshot (deleted, unreadable, or not a file), so it cannot "
                "be approved."
            )
            continue
        existing[path] = content_id

    paths = tuple(sorted(existing))

    if not paths:
        problems.append("The approved change is empty: no file may be "
                        "committed.")
    if len(paths) > policy.max_approved_files:
        problems.append(
            f"The approved set has {len(paths)} files, above the configured "
            f"maximum of {policy.max_approved_files}."
        )

    missing = sorted(
        path for path in expected if _path_key(path) not in agent_keys
    )
    if outside_scope:
        reasons.append(
            f"{len(outside_scope)} path(s) the run changed are outside the "
            f"allowed scope: {', '.join(repr(p) for p in outside_scope)}."
        )
    if paths:
        reasons.append(
            f"The approved set is exactly the run's change inside the allowed "
            f"scope: {', '.join(repr(p) for p in paths)}."
        )
    else:
        reasons.append("The approved set is empty.")

    return ApprovedFileSet(
        paths=paths,
        expected=expected,
        agent_files=agent_files,
        outside_scope=tuple(outside_scope),
        missing_files=tuple(missing),
        problems=tuple(problems),
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------

_PASS = GateStatus.PASS
_FAIL = GateStatus.FAIL


def _has_text(value: object) -> bool:
    """True when ``value`` is a non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _gate_inputs(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G1 - every required T20 input is present."""
    missing = [
        name
        for name, value in (
            ("issue_status", facts.issue_status),
            ("codex", facts.codex),
            ("tests", facts.tests),
            ("analysis", facts.analysis),
            ("verification", facts.verification),
            ("scope", facts.scope),
            ("attribution", facts.attribution),
            ("baseline", facts.baseline),
            ("after", facts.after),
        )
        if value is None
    ]
    for name, value in (
        ("issue_key", facts.issue_key),
        ("rule", facts.rule),
        ("target_file", facts.target_file),
    ):
        if not _has_text(value):
            missing.append(name)
    if missing:
        return _FAIL, (
            "Missing or unusable T20 input(s): " + ", ".join(sorted(missing)) + "."
        )
    return _PASS, "Every required T20 input is present."


def _gate_t19_fixed(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G2 - the T19 result is ``FIXED``."""
    status = getattr(facts.issue_status, "status", None)
    if status is not IssueFinalStatus.FIXED:
        rendered = getattr(status, "value", status)
        return _FAIL, (
            f"The T19 final status is {rendered!r}, not 'fixed'; T20 only "
            "commits a verified fix."
        )
    return _PASS, "The T19 final status is 'fixed'."


def _gate_no_review_required(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G3 - no unresolved review condition."""
    if bool(getattr(facts.issue_status, "needs_review", False)):
        return _FAIL, "T19 flagged the attempt as requiring human review."
    blocking = getattr(facts.issue_status, "blocking_reason", None)
    if blocking:
        return _FAIL, f"T19 reported a blocking reason: {blocking}"
    return _PASS, "T19 reported no review requirement and no blocking reason."


def _gate_codex_succeeded(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G4 - the Codex process ran and exited 0."""
    if not bool(getattr(facts.codex, "execution_succeeded", False)):
        return _FAIL, (
            "The Codex execution did not succeed "
            f"({getattr(facts.codex, 'reason', 'no detail')})."
        )
    return _PASS, "The Codex execution succeeded."


def _gate_no_codex_uncertainty(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G5 - no uncertainty marker in the Codex output."""
    if bool(getattr(facts.codex, "output_suggests_uncertainty", False)):
        markers = ", ".join(getattr(facts.codex, "matched_markers", ()) or ())
        return _FAIL, (
            "The Codex output contains uncertainty markers"
            f"{' (' + markers + ')' if markers else ''}."
        )
    return _PASS, "The Codex output contains no uncertainty markers."


def _match_review_markers(text: object) -> Tuple[str, ...]:
    """Return the review-request markers found in ``text`` (case-folded)."""
    haystack = str(text or "").casefold()
    return tuple(
        sorted(marker for marker in REVIEW_REQUEST_MARKERS if marker in haystack)
    )


def _gate_no_codex_review_request(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G6 - Codex did not ask for a human review."""
    haystack = (
        f"{getattr(facts.codex, 'stdout', '')}\n"
        f"{getattr(facts.codex, 'stderr', '')}"
    )
    markers = _match_review_markers(haystack)
    if markers:
        return _FAIL, (
            "The Codex output asks for a human review "
            f"(markers: {', '.join(markers)})."
        )
    return _PASS, "The Codex output requests no human review."


def _gate_scope_valid(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G7 - the T13 change scope is valid."""
    if not bool(getattr(facts.scope, "is_valid", False)):
        unexpected = getattr(facts.scope, "unexpected_files", ()) or ()
        return _FAIL, (
            "T13 change-scope validation failed with "
            f"{len(unexpected)} unexpected file(s)."
        )
    if tuple(getattr(facts.scope, "unexpected_files", ()) or ()):
        return _FAIL, "T13 reported unexpected files outside the scope."
    return _PASS, "The T13 change scope is valid."


def _gate_target_changed(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G8 - the issue's own file really changed."""
    if not bool(getattr(facts.scope, "expected_file_modified", False)):
        return _FAIL, (
            "The issue's own file did not change, so a fix cannot be "
            "attributed to this run."
        )
    return _PASS, "The issue's own file changed."


def _gate_tests_passed(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G9 - the project tests passed."""
    if not bool(getattr(facts.tests, "passed", False)):
        return _FAIL, (
            "The project tests did not pass "
            f"({getattr(facts.tests, 'reason', 'no detail')})."
        )
    if getattr(facts.tests, "blocking_reason", None):
        return _FAIL, str(facts.tests.blocking_reason)
    return _PASS, "The project tests passed."


def _gate_analysis_succeeded(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G10 - the SonarQube analysis completed successfully."""
    if not bool(getattr(facts.analysis, "succeeded", False)):
        return _FAIL, (
            "The SonarQube analysis did not complete successfully "
            f"({getattr(facts.analysis, 'failure_reason', None) or 'no detail'})."
        )
    return _PASS, "The SonarQube analysis completed successfully."


def _gate_analysis_task_id(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G11 - a compute-engine task id identifies the analysis and agrees with T16.

    Fail closed, not fail open: the T16 evidence is **required**. A missing or
    unusable T16 record, a trigger that did not succeed, a trigger with no task
    id and a task id that disagrees with the completion are all refusals. An
    absent trigger must never let the cross-check be skipped silently, because
    the whole point of the gate is that the completion belongs to the analysis
    *this* run triggered.
    """
    task_id = getattr(facts.analysis, "task_id", None)
    if not _has_text(task_id):
        return _FAIL, "The analysis completion carries no compute-engine task id."
    evidence = facts.trigger
    if evidence is None:
        return _FAIL, (
            "The T16 analysis-trigger evidence is missing, so the compute-engine "
            "task id cannot be cross-checked against the task this run triggered."
        )
    if not bool(getattr(evidence, "triggered", False)):
        return _FAIL, (
            "The T16 analysis trigger did not report a successful trigger, so "
            "the analysis completion cannot be attributed to this run."
        )
    trigger_task_id = getattr(evidence, "task_id", None)
    if not _has_text(trigger_task_id):
        return _FAIL, "The T16 analysis trigger carries no compute-engine task id."
    if str(trigger_task_id).strip() != str(task_id).strip():
        return _FAIL, (
            "The waited-on task id does not match the task id the analysis "
            "trigger reported."
        )
    return _PASS, (
        "A compute-engine task id identifies the analysis and matches the T16 "
        "trigger."
    )


def _gate_analysis_correlated(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G12 - the issue snapshot belongs to the verified analysis."""
    if not bool(getattr(facts.verification, "analysis_correlated", False)):
        reason = getattr(facts.verification, "correlation_reason", "") or ""
        return _FAIL, (
            "The issue snapshot is not correlated with the verified analysis"
            f"{': ' + reason if reason else '.'}"
        )
    return _PASS, "The issue snapshot is correlated with the verified analysis."


def _gate_reliable_absence(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G13 - the absence of the original issue is reliable."""
    if not bool(getattr(facts.verification, "reliable_absence", False)):
        return _FAIL, (
            "The original issue's absence is not reliable "
            f"({getattr(facts.verification, 'reason', 'no detail')})."
        )
    return _PASS, "The original issue is reliably absent."


def _gate_no_positive_finding(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G14 - the original issue is not still reported (no positive finding)."""
    if bool(getattr(facts.verification, "is_present", True)):
        return _FAIL, (
            "A matching issue is still open after the analysis, so nothing is "
            "fixed."
        )
    return _PASS, "No matching issue was reported after the analysis."


def _gate_attribution_valid(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G15 - the T20 attribution credits the change to this run."""
    if not bool(getattr(facts.attribution, "attributable", False)):
        blocked = getattr(facts.attribution, "blocked_reasons", ()) or ()
        return _FAIL, (
            "The change cannot be attributed to this run: "
            + (" ".join(str(reason) for reason in blocked) or "no detail")
        )
    return _PASS, "Every change is attributable to this run."


def _gate_baseline_clean(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G16 - the pre-run baseline was clean and fully captured."""
    if facts.baseline is None or facts.after is None:
        return _FAIL, (
            "The pre-run baseline and/or the post-run snapshot was not supplied, "
            "so the baseline cannot be verified."
        )
    problems: list = []
    if facts.config.require_clean_baseline:
        if not bool(getattr(facts.attribution, "clean_baseline", False)):
            problems.append("the working tree was not clean before the run")
        if not facts.baseline.is_clean:
            problems.append(
                "the pre-run snapshot reported "
                f"{len(facts.baseline.changed_files)} change(s)"
            )
    # The index must be empty before T20 stages anything: T20 never resets or
    # unstages, so pre-staged entries of unknown provenance can never be
    # committed. This is always required, whatever the configuration says.
    if facts.baseline.staged_files:
        problems.append(
            f"the index already contained {len(facts.baseline.staged_files)} "
            "staged path(s)"
        )
    if not facts.baseline.ignored_scan:
        problems.append("the ignored-file set was not captured for the baseline")
    if problems:
        return _FAIL, "The baseline is not usable for a safe commit: " + (
            "; ".join(problems)
        ) + "."
    return _PASS, "The pre-run baseline was clean and fully captured."


def _gate_no_pre_existing_target(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G17 - the target set was not already modified before the run."""
    if facts.baseline is None or facts.attribution is None:
        return _FAIL, (
            "The baseline and/or the attribution was not supplied, so a "
            "pre-existing target modification cannot be excluded."
        )
    pre_existing = {
        _path_key(path)
        for path in (
            tuple(getattr(facts.attribution, "pre_existing_files", ()) or ())
            + tuple(
                getattr(facts.attribution, "pre_existing_and_changed_files", ())
                or ()
            )
        )
    }
    target_keys = {
        _path_key(path)
        for path in (
            (facts.target_file,)
            + tuple(getattr(facts.approved, "expected", ()) or ())
        )
    }
    baseline_keys = {_path_key(path) for path in facts.baseline.changed_files}
    blocked = sorted((pre_existing & target_keys) or (baseline_keys & target_keys))
    if blocked:
        return _FAIL, (
            "The target file(s) were already changed before this run: "
            + ", ".join(repr(path) for path in blocked)
            + "."
        )
    return _PASS, "No target file was already modified before this run."


def _gate_whole_tree_accounted(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G18 - every post-run change is explained and no ignored file appeared."""
    if facts.baseline is None or facts.after is None or facts.attribution is None:
        return _FAIL, (
            "The baseline, the post-run snapshot and the attribution are all "
            "required to account for the whole tree."
        )
    after = facts.after
    baseline = facts.baseline
    attribution = facts.attribution
    explained = (
        set(getattr(attribution, "agent_files", ()) or ())
        | set(getattr(attribution, "pre_existing_files", ()) or ())
        | set(
            getattr(attribution, "pre_existing_and_changed_files", ()) or ()
        )
    )
    unexplained = sorted(set(after.changed_files) - explained)
    new_ignored = sorted(set(after.ignored_files) - set(baseline.ignored_files))

    problems: list = []
    if unexplained:
        problems.append(
            "unexplained change(s): " + ", ".join(repr(p) for p in unexplained)
        )
    if new_ignored:
        problems.append(
            "ignored file(s) appeared during the run: "
            + ", ".join(repr(p) for p in new_ignored)
        )
    if not after.ignored_scan:
        problems.append("the post-run ignored-file set was not captured")
    if not baseline.ignored_scan:
        problems.append("the baseline ignored-file set was not captured")
    if problems:
        return _FAIL, (
            "The whole-tree change set is not fully accounted for: "
            + "; ".join(problems)
            + "."
        )
    return _PASS, (
        "Every change is explained by the run or by the pre-run baseline, and "
        "no ignored file was created."
    )


def _gate_approved_non_empty(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G19 - at least one file was approved."""
    if facts.approved is None:
        return _FAIL, "The approved file set was never resolved."
    if facts.approved.is_empty:
        return _FAIL, (
            "The approved change is empty, so there is nothing to commit."
        )
    return _PASS, (
        f"{len(facts.approved.paths)} approved path(s): "
        + ", ".join(repr(path) for path in facts.approved.paths)
        + "."
    )


def _gate_approved_exact(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G20 - the approved set is exactly the run's in-scope change."""
    if facts.approved is None:
        return _FAIL, "The approved file set was never resolved."
    approved = facts.approved
    if approved.exactly_resolved:
        return _PASS, "The approved file set is exactly resolved."
    problems: list = []
    if approved.problems:
        problems.append(" ".join(approved.problems))
    if approved.outside_scope:
        problems.append(
            "outside the allowed scope: "
            + ", ".join(repr(path) for path in approved.outside_scope)
        )
    if not approved.paths:
        problems.append("no path was approved")
    if set(approved.paths) != set(approved.agent_files):
        problems.append(
            "the approved paths are not exactly the paths the run changed"
        )
    return _FAIL, (
        "The approved file set could not be resolved exactly: "
        + " ".join(problems or ["no detail"])
    )


def _gate_no_deletion(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G21 - no approved or expected path was deleted."""
    if facts.after is None:
        return _FAIL, (
            "The post-run snapshot was not supplied, so no deletion can be "
            "excluded."
        )
    deleted_agent = tuple(getattr(facts.attribution, "agent_deleted_files", ()) or ())
    if deleted_agent:
        return _FAIL, (
            "The run deleted file(s), which T20 v1 never commits: "
            + ", ".join(repr(path) for path in deleted_agent)
            + "."
        )
    deleted_keys = {_path_key(path) for path in facts.after.deleted_files}
    target_keys = {
        _path_key(path)
        for path in (
            (facts.target_file,)
            + tuple(getattr(facts.approved, "expected", ()) or ())
            + tuple(getattr(facts.approved, "paths", ()) or ())
        )
    }
    deleted_targets = sorted(deleted_keys & target_keys)
    if deleted_targets:
        return _FAIL, (
            "A target file is deleted in the working tree: "
            + ", ".join(repr(path) for path in deleted_targets)
            + ". T20 performs no recovery."
        )
    return _PASS, "No approved or expected path is deleted."


def _gate_no_rename(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G22 - no rename happened during the run."""
    if facts.after is None:
        return _FAIL, (
            "The post-run snapshot was not supplied, so no rename can be "
            "excluded."
        )
    renamed_agent = tuple(getattr(facts.attribution, "agent_renamed_files", ()) or ())
    if renamed_agent:
        rendered = ", ".join(
            f"{new!r} <- {old!r}" for new, old in renamed_agent
        )
        return _FAIL, (
            "The run renamed file(s), which T20 v1 never commits: "
            + rendered
            + "."
        )
    expected_keys = {
        _path_key(path)
        for path in (
            (facts.target_file,)
            + tuple(getattr(facts.approved, "expected", ()) or ())
        )
    }
    renamed_targets = sorted(
        new
        for new, _old in facts.after.renamed_files
        if _path_key(new) in expected_keys
    )
    if renamed_targets:
        return _FAIL, (
            "A target file is involved in a rename: "
            + ", ".join(repr(path) for path in renamed_targets)
            + "."
        )
    return _PASS, "No rename is involved in this change."


def _gate_paths_safe(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G23 - every approved path is safe and literal."""
    if facts.approved is None:
        return _FAIL, "The approved file set was never resolved."
    accepted, problems = normalise_approved_paths(facts.approved.paths)
    if problems:
        return _FAIL, (
            "Approved path(s) failed the safety check: " + " ".join(problems)
        )
    if accepted != tuple(facts.approved.paths):
        return _FAIL, (
            "The approved paths do not survive normalization unchanged, so "
            "they are not literal repository-relative paths."
        )
    return _PASS, "Every approved path is a safe repository-relative path."


def _gate_not_ignored(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G24 - no approved path is ignored by Git."""
    if facts.baseline is None or facts.after is None:
        return _FAIL, (
            "The baseline and/or the post-run snapshot was not supplied, so no "
            "approved path can be proven to be trackable."
        )
    if facts.config.allow_ignored_approved_paths:
        return _PASS, (
            "Ignored-path refusal is disabled by configuration (test-only "
            "override)."
        )
    ignored = set(facts.after.ignored_files) | set(facts.baseline.ignored_files)
    if not facts.after.ignored_scan or not facts.baseline.ignored_scan:
        return _FAIL, (
            "The ignored-file set was not captured, so no approved path can be "
            "proven to be trackable."
        )
    approved_keys = {
        _path_key(path) for path in getattr(facts.approved, "paths", ()) or ()
    }
    ignored_keys = {_path_key(path): path for path in ignored}
    clashes = sorted(
        ignored_keys[key] for key in ignored_keys if key in approved_keys
    )
    if clashes:
        return _FAIL, (
            "Approved path(s) are ignored by Git and must never be staged: "
            + ", ".join(repr(path) for path in clashes)
            + "."
        )
    return _PASS, "No approved path is ignored by Git."


def _gate_not_binary(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G25 - every approved file is text."""
    if facts.binary_paths:
        return _FAIL, (
            "Approved file(s) are binary and are never committed by T20 v1: "
            + ", ".join(repr(path) for path in facts.binary_paths)
            + "."
        )
    return _PASS, "Every approved file is text."


def _scan_verdict(
    scan: Optional[SecretScanResult], label: str
) -> Tuple[GateStatus, str]:
    """Shared fail-closed verdict for one secret scan."""
    if scan is None:
        return _FAIL, f"The {label} was never scanned."
    if scan.ok:
        return _PASS, f"The {label} contains no credential-shaped content."
    return _FAIL, (
        f"The {label} failed its secret scan: {scan.reason}"
    )


def _gate_content_scan(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G26 - the approved file content passes the secret scan."""
    return _scan_verdict(facts.content_scan, "approved file content")


def _gate_worktree_diff_scan(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G27 - the approved worktree diff passes the secret scan."""
    return _scan_verdict(facts.worktree_diff_scan, "approved worktree diff")


def _gate_staged_diff_scan(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G28 - the staged diff passes the secret scan."""
    stage = facts.stage
    if stage is None:
        return _FAIL, "Staging never ran, so the staged diff was never scanned."
    if stage.error:
        return _FAIL, f"Staging failed before the staged diff could be scanned: {stage.error}"
    return _scan_verdict(facts.staged_diff_scan, "staged diff")


def _gate_repository_identity(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G29 - the resolved repository identity is valid and as expected."""
    repository = facts.repository
    if repository is None:
        return _FAIL, "The repository identity was never resolved."
    if repository.is_valid:
        return _PASS, (
            f"Repository '{repository.worktree_root}' resolved as expected on "
            f"branch '{repository.branch}'."
        )
    problems: list = list(repository.problems)
    if not repository.matches_expected:
        problems.append(
            "the resolved worktree/git-dir does not match the requested "
            "repository path"
        )
    if repository.detached or not repository.branch:
        problems.append("no branch is checked out (detached HEAD)")
    for name, value in (
        ("worktree root", repository.worktree_root),
        ("git dir", repository.git_dir),
        ("common dir", repository.common_dir),
        ("index path", repository.index_path),
        ("HEAD", repository.head_commit),
    ):
        if not value:
            problems.append(f"{name} could not be resolved")
    return _FAIL, (
        "The repository identity is not valid: "
        + "; ".join(problems or ["no detail"])
    )


def _gate_environment_safe(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G30 - the Git environment cannot redirect the commit."""
    environment = facts.environment
    if environment is None:
        return _FAIL, "The Git environment was never inspected."
    if environment.is_safe:
        return _PASS, (
            "No redirecting or code-executing Git environment variable is set "
            f"({len(environment.checked_variables)} inspected)."
        )
    return _FAIL, (
        "Unsafe Git environment variable(s) are set: "
        + ", ".join(environment.unsafe_variables)
        + "."
    )


def _gate_head_not_detached(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G31 - HEAD is not detached."""
    if facts.repository is None:
        return _FAIL, "HEAD could not be inspected: the identity is unknown."
    if facts.repository.detached:
        return _FAIL, "HEAD is detached, so a commit would not update a branch."
    return _PASS, f"HEAD is attached to branch '{facts.repository.branch}'."


def _gate_branch_safe(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G32 - the branch is not protected/reserved and is an agent branch."""
    repository = facts.repository
    if repository is None or not repository.branch:
        return _FAIL, "The current branch could not be determined."
    branch = repository.branch
    config = facts.config
    if config.is_protected_branch(branch):
        return _FAIL, (
            f"'{branch}' is a protected/default/reserved branch and never "
            "receives a T20 commit."
        )
    if not config.is_agent_branch(branch):
        return _FAIL, (
            f"'{branch}' is outside the agent branch namespace "
            f"'{config.required_branch_prefix}/'; T07 owns branch creation and "
            "T20 never creates or checks out a branch."
        )
    return _PASS, f"Branch '{branch}' may receive a T20 commit."


def _gate_identity_explicit(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G33 - an explicit Git identity is configured."""
    identity = facts.identity
    if identity is None:
        return _FAIL, "The Git identity was never inspected."
    if not facts.config.require_explicit_identity:
        return _PASS, (
            "Explicit-identity refusal is disabled by configuration "
            "(test-only override)."
        )
    if identity.explicit:
        return _PASS, (
            f"An explicit Git identity is configured ({identity.name} "
            f"<{identity.email}>)."
        )
    problems = list(identity.problems) or [
        "user.name and/or user.email are not explicitly configured"
    ]
    return _FAIL, (
        "No explicit Git identity is configured: " + "; ".join(problems) + "."
    )


def _gate_no_operation_in_progress(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G34 - no other Git operation is in progress (including a held lock)."""
    if facts.operation_in_progress:
        return _FAIL, (
            "Another Git operation is in progress: "
            + ", ".join(facts.operation_in_progress)
            + "."
        )
    if facts.repository is None:
        return _FAIL, (
            "The repository was never inspected, so an in-progress operation "
            "cannot be excluded."
        )
    if facts.repository.index_locked:
        return _FAIL, "The Git index lock (index.lock) is held."
    return _PASS, "No Git operation is in progress and the index is not locked."


def _gate_no_unmerged(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G35 - the index has no unmerged entries."""
    if facts.repository is not None and not facts.repository.is_valid:
        # The index could not be read at all, so "no unmerged entries" is not a
        # fact that may be assumed.
        return _FAIL, (
            "The index state is unknown because the repository identity is not "
            "valid."
        )
    unmerged = tuple(facts.unmerged_paths)
    if not unmerged and facts.stage is not None:
        unmerged = tuple(facts.stage.unmerged_paths)
    if unmerged:
        return _FAIL, (
            "The index has unmerged entries: "
            + ", ".join(repr(path) for path in unmerged)
            + "."
        )
    return _PASS, "The index has no unmerged entries."


def _gate_hooks_accounted(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G36 - no active hook T20 cannot account for."""
    hooks = facts.hooks
    if hooks is None:
        return _FAIL, "The repository hooks were never inspected."
    if hooks.is_accounted_for:
        return _PASS, (
            "No active commit-affecting hook is installed "
            f"({len(hooks.inspected_hooks)} inspected)."
        )
    return _FAIL, (
        "Active commit hook(s) that T20 cannot account for are installed: "
        + ", ".join(hooks.active_hooks)
        + ". T20 never bypasses hooks (no --no-verify)."
    )


def _gate_head_unchanged(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G37 - HEAD is still the baseline HEAD."""
    if facts.baseline is None or facts.after is None:
        return _FAIL, (
            "The baseline and/or the post-run snapshot was not supplied, so HEAD "
            "movement cannot be excluded."
        )
    baseline_head = facts.baseline.head_commit
    after_head = facts.after.head_commit
    if not baseline_head or not after_head:
        return _FAIL, "HEAD could not be read for the baseline or the run."
    if baseline_head != after_head:
        return _FAIL, (
            f"HEAD moved during the run ({baseline_head} -> {after_head}); the "
            "change cannot be committed against the reviewed revision."
        )
    if facts.repository is not None and facts.repository.head_commit:
        if facts.repository.head_commit != baseline_head:
            return _FAIL, (
                f"HEAD is {facts.repository.head_commit} but the baseline "
                f"recorded {baseline_head}."
            )
    if facts.stage is not None and facts.stage.head_after:
        if facts.stage.head_after != baseline_head:
            return _FAIL, (
                f"HEAD moved while staging ({baseline_head} -> "
                f"{facts.stage.head_after})."
            )
    return _PASS, f"HEAD is unchanged at {baseline_head}."


def _gate_stage_matches_worktree(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G38 - the worktree still matches the approved content after staging."""
    stage = facts.stage
    if stage is None or not stage.attempted:
        return _FAIL, "Exact-path staging never ran."
    if stage.error:
        return _FAIL, f"Exact-path staging failed: {stage.error}"
    approved_ids = dict(stage.approved_content_ids)
    current_ids = dict(stage.worktree_content_ids)
    missing = sorted(set(approved_ids) - set(current_ids))
    changed = sorted(
        path
        for path, blob_id in approved_ids.items()
        if path in current_ids and current_ids[path] != blob_id
    )
    if missing or changed or not approved_ids:
        problems: list = []
        if missing:
            problems.append(
                "content identity missing after staging for "
                + ", ".join(repr(p) for p in missing)
            )
        if changed:
            problems.append(
                "worktree content changed during the commit for "
                + ", ".join(repr(p) for p in changed)
            )
        if not approved_ids:
            problems.append("no approved content identity was recorded")
        if stage.problems:
            problems.append(" ".join(stage.problems))
        return _FAIL, (
            "The worktree no longer matches the approved content: "
            + "; ".join(problems)
            + "."
        )
    return _PASS, "The worktree still matches the approved content."


def _gate_staged_set_exact(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G39 - the staged path set is exactly the approved path set."""
    stage = facts.stage
    if stage is None or not stage.attempted:
        return _FAIL, "Exact-path staging never ran."
    if stage.error:
        return _FAIL, f"Exact-path staging failed: {stage.error}"
    approved = tuple(stage.approved_paths)
    staged = tuple(stage.staged_paths)
    if not approved:
        return _FAIL, "No approved path was recorded for the staging step."
    extra = sorted(set(staged) - set(approved))
    absent = sorted(set(approved) - set(staged))
    if extra or absent:
        problems: list = []
        if extra:
            problems.append(
                "unexpected staged path(s): "
                + ", ".join(repr(path) for path in extra)
            )
        if absent:
            problems.append(
                "approved path(s) were not staged: "
                + ", ".join(repr(path) for path in absent)
            )
        return _FAIL, (
            "The staged path set is not exactly the approved set: "
            + "; ".join(problems)
            + "."
        )
    return _PASS, (
        f"The staged path set is exactly the {len(approved)} approved path(s)."
    )


def _gate_staged_content_matches(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G40 - the staged blob ids equal the approved worktree content ids."""
    stage = facts.stage
    if stage is None or not stage.attempted:
        return _FAIL, "Exact-path staging never ran."
    if stage.error:
        return _FAIL, f"Exact-path staging failed: {stage.error}"
    approved_ids = dict(stage.approved_content_ids)
    staged_ids = dict(stage.staged_blob_ids)
    if not approved_ids:
        return _FAIL, "No approved content identity was recorded."
    mismatched = sorted(
        path
        for path, blob_id in approved_ids.items()
        if staged_ids.get(path) != blob_id
    )
    missing = sorted(path for path in approved_ids if path not in staged_ids)
    if mismatched or missing:
        problems: list = []
        if mismatched:
            problems.append(
                "staged content differs from the approved content for "
                + ", ".join(repr(path) for path in mismatched)
            )
        if missing:
            problems.append(
                "no staged blob id was found for "
                + ", ".join(repr(path) for path in missing)
            )
        return _FAIL, (
            "The staged content does not match the approved content "
            "(STAGED_MISMATCH): " + "; ".join(problems) + "."
        )
    return _PASS, "Every staged blob id equals the approved content id."


def _gate_staged_unchanged(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G41 - the staged content is unchanged since it was staged."""
    stage = facts.stage
    recheck = facts.pre_commit_stage
    if recheck is None or not recheck.attempted:
        return _FAIL, (
            "The staged content was not re-verified immediately before the "
            "commit."
        )
    if recheck.error:
        return _FAIL, f"The pre-commit staged re-check failed: {recheck.error}"
    if stage is None or not stage.attempted:
        return _FAIL, "Exact-path staging never ran."
    if tuple(recheck.staged_paths) != tuple(stage.staged_paths):
        return _FAIL, (
            "The staged path set changed between staging and the commit "
            f"({list(stage.staged_paths)} -> {list(recheck.staged_paths)})."
        )
    before = dict(stage.staged_blob_ids)
    after = dict(recheck.staged_blob_ids)
    differing = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    if differing:
        return _FAIL, (
            "The staged content changed after it was staged: "
            + ", ".join(repr(path) for path in differing)
            + "."
        )
    # The worktree content the staged bytes came from must also be unchanged:
    # otherwise the commit would record content that no longer exists.
    before_worktree = dict(stage.worktree_content_ids)
    after_worktree = dict(recheck.worktree_content_ids)
    worktree_differing = sorted(
        path
        for path in set(before_worktree) | set(after_worktree)
        if before_worktree.get(path) != after_worktree.get(path)
    )
    if worktree_differing:
        return _FAIL, (
            "The worktree content changed after staging, so the approved change "
            "is no longer the change on disk: "
            + ", ".join(repr(path) for path in worktree_differing)
            + "."
        )
    return _PASS, "The staged content is unchanged since it was staged."


def _gate_message_valid(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G42 - the commit message is valid and matches the approved change."""
    message = facts.commit_message
    if message is None:
        return _FAIL, "No commit message was built."
    if (
        validate_commit_message(
            message.text, max_message_length=facts.config.max_message_length
        )
        is None
    ):
        return _FAIL, "The commit message failed validation."
    approved_paths = tuple(getattr(facts.approved, "paths", ()) or ())
    if len(approved_paths) == 1 and message.file_path != approved_paths[0]:
        return _FAIL, (
            f"The commit message names {message.file_path!r} but the approved "
            f"file is {approved_paths[0]!r}."
        )
    if facts.rule and message.rule != facts.rule:
        return _FAIL, (
            f"The commit message names rule {message.rule!r} but the issue "
            f"rule is {facts.rule!r}."
        )
    if facts.issue_key and message.issue_key != facts.issue_key:
        return _FAIL, (
            f"The commit message names issue {message.issue_key!r} but the "
            f"issue is {facts.issue_key!r}."
        )
    return _PASS, f"The commit message is valid: {message.subject!r}."


def _gate_commit_succeeds(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G43 - exactly one commit attempt succeeded."""
    commit = facts.commit
    if commit is None or not commit.attempted:
        return _FAIL, "No commit was attempted."
    if not commit.committed:
        return _FAIL, (
            "The commit did not succeed"
            f"{': ' + commit.error if commit.error else ''}."
        )
    return _PASS, (
        f"The commit succeeded ({commit.previous_head} -> {commit.new_head})."
    )


def _gate_post_commit_verified(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G44 - the post-commit state proves exactly the approved change."""
    commit = facts.commit
    if commit is None or not commit.committed:
        return _FAIL, "There is no commit to verify."
    problems: list = list(commit.problems)
    if commit.new_commit_count != 1:
        problems.append(
            f"{commit.new_commit_count!r} new commit(s) were created instead of "
            "exactly one"
        )
    if commit.parent_head != commit.expected_previous_head:
        problems.append(
            f"the new commit's parent is {commit.parent_head!r}, not the "
            f"expected {commit.expected_previous_head!r}"
        )
    if commit.message != commit.expected_message:
        problems.append("the committed message is not the expected message")
    if not commit.identity_matches:
        problems.append("the commit author/committer is not the verified identity")
    if tuple(commit.committed_paths) != tuple(commit.expected_paths):
        problems.append(
            "the commit does not contain exactly the approved paths "
            f"({list(commit.committed_paths)})"
        )
    for path, blob_id in dict(commit.expected_blob_ids).items():
        if dict(commit.committed_blob_ids).get(path) != blob_id:
            problems.append(
                f"the committed blob for {path!r} is not the approved content"
            )
    if commit.deleted_paths:
        problems.append(
            "the commit deletes path(s): "
            + ", ".join(repr(path) for path in commit.deleted_paths)
        )
    if commit.renamed_paths:
        problems.append(
            "the commit renames path(s): "
            + ", ".join(repr(path) for path in commit.renamed_paths)
        )
    if commit.branch_after != commit.expected_branch:
        problems.append(
            f"the branch is {commit.branch_after!r} but was expected to be "
            f"{commit.expected_branch!r}"
        )
    if commit.worktree_root_after != commit.expected_worktree_root:
        problems.append("the repository identity changed during the commit")
    if problems:
        return _FAIL, (
            "The commit exists but its correctness could not be proven: "
            + "; ".join(problems)
            + "."
        )
    return _PASS, (
        "Exactly one commit was created with the expected parent, message, "
        "identity, paths and blob ids."
    )


def _gate_no_unexpected_git_error(facts: CommitFacts) -> Tuple[GateStatus, str]:
    """G45 - no unexpected Git error was observed."""
    commit = facts.commit
    errors: list = list(getattr(commit, "unexpected_errors", ()) or ())
    if commit is not None and commit.error and not commit.committed:
        errors.append(commit.error)
    if facts.stage is not None and facts.stage.error:
        errors.append(facts.stage.error)
    if errors:
        return _FAIL, "Unexpected Git error(s) were observed: " + "; ".join(errors)
    return _PASS, "No unexpected Git error was observed."


#: Gate evaluator dispatch table. Every gate in :data:`GATES` must appear here;
#: this is asserted when the module is imported.
_GATE_EVALUATORS: Dict[str, Callable[[CommitFacts], Tuple[GateStatus, str]]] = {
    "G1": _gate_inputs,
    "G2": _gate_t19_fixed,
    "G3": _gate_no_review_required,
    "G4": _gate_codex_succeeded,
    "G5": _gate_no_codex_uncertainty,
    "G6": _gate_no_codex_review_request,
    "G7": _gate_scope_valid,
    "G8": _gate_target_changed,
    "G9": _gate_tests_passed,
    "G10": _gate_analysis_succeeded,
    "G11": _gate_analysis_task_id,
    "G12": _gate_analysis_correlated,
    "G13": _gate_reliable_absence,
    "G14": _gate_no_positive_finding,
    "G15": _gate_attribution_valid,
    "G16": _gate_baseline_clean,
    "G17": _gate_no_pre_existing_target,
    "G18": _gate_whole_tree_accounted,
    "G19": _gate_approved_non_empty,
    "G20": _gate_approved_exact,
    "G21": _gate_no_deletion,
    "G22": _gate_no_rename,
    "G23": _gate_paths_safe,
    "G24": _gate_not_ignored,
    "G25": _gate_not_binary,
    "G26": _gate_content_scan,
    "G27": _gate_worktree_diff_scan,
    "G28": _gate_staged_diff_scan,
    "G29": _gate_repository_identity,
    "G30": _gate_environment_safe,
    "G31": _gate_head_not_detached,
    "G32": _gate_branch_safe,
    "G33": _gate_identity_explicit,
    "G34": _gate_no_operation_in_progress,
    "G35": _gate_no_unmerged,
    "G36": _gate_hooks_accounted,
    "G37": _gate_head_unchanged,
    "G38": _gate_stage_matches_worktree,
    "G39": _gate_staged_set_exact,
    "G40": _gate_staged_content_matches,
    "G41": _gate_staged_unchanged,
    "G42": _gate_message_valid,
    "G43": _gate_commit_succeeds,
    "G44": _gate_post_commit_verified,
    "G45": _gate_no_unexpected_git_error,
}

if set(_GATE_EVALUATORS) != {gate_id for gate_id, _ in GATES}:  # pragma: no cover
    raise CommitPolicyError(
        "The gate evaluator table does not match the declared gate list."
    )


def evaluate_commit_gates(
    facts: CommitFacts,
    *,
    phase: CommitPhase = CommitPhase.INPUTS,
) -> GateEvaluation:
    """Evaluate every gate whose stage has been reached (pure, deterministic).

    Args:
        facts: the immutable record of everything the executor observed.
        phase: the furthest state-machine stage that has run. Gates belonging to
            later stages are reported as ``NOT_REACHED``; every other gate is
            evaluated and produces ``PASS`` or ``FAIL``.

    Returns:
        A :class:`GateEvaluation` containing all 45 gates in evaluation order.
        The decision is ``REFUSE`` as soon as one evaluated gate fails, so a
        caller that mutates Git only when the decision is ``PROCEED`` cannot be
        tricked by a partially-filled :class:`CommitFacts`.

    Raises:
        CommitPolicyError: ``facts`` is not a :class:`CommitFacts`.
    """
    if not isinstance(facts, CommitFacts):
        raise CommitPolicyError(
            "evaluate_commit_gates expects a CommitFacts record, got "
            f"{type(facts).__name__}."
        )
    reached = _phase_index(phase)

    gates: list = []
    for gate_id, title in GATES:
        if _phase_index(_GATE_PHASE[gate_id]) > reached:
            gates.append(
                CommitGate(
                    gate_id=gate_id,
                    title=title,
                    status=GateStatus.NOT_REACHED,
                    reason=NOT_REACHED_REASON,
                )
            )
            continue
        try:
            status, reason = _GATE_EVALUATORS[gate_id](facts)
        except Exception as exc:  # pragma: no cover - defensive: fail closed
            status, reason = _FAIL, (
                "The gate could not be evaluated, which fails closed: "
                f"{type(exc).__name__}: {exc}"
            )
        gates.append(
            CommitGate(
                gate_id=gate_id, title=title, status=status, reason=reason
            )
        )

    ordered = tuple(gates)
    decision = (
        CommitDecision.PROCEED
        if not any(gate.failed for gate in ordered)
        else CommitDecision.REFUSE
    )
    return GateEvaluation(phase=phase, gates=ordered, decision=decision)


__all__: Sequence[str] = (
    "ACCOUNTED_HOOK_NAMES",
    "ApprovedFileSet",
    "CommitDecision",
    "CommitFacts",
    "CommitGate",
    "CommitObservation",
    "CommitPhase",
    "CommitPolicyConfig",
    "CommitPolicyError",
    "DEFAULT_AGENT_BRANCH_PREFIX",
    "DEFAULT_MAX_APPROVED_FILES",
    "GATES",
    "GateEvaluation",
    "GateStatus",
    "GitEnvironmentReport",
    "GitIdentityReport",
    "HookReport",
    "NOT_REACHED_REASON",
    "OPERATION_IN_PROGRESS_MARKERS",
    "PROTECTED_BRANCH_NAMES",
    "REVIEW_REQUEST_MARKERS",
    "RepositoryIdentity",
    "StageObservation",
    "UNSAFE_GIT_ENVIRONMENT_VARIABLES",
    "evaluate_commit_gates",
    "normalise_approved_paths",
    "resolve_approved_files",
)

















