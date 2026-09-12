"""Shared, non-test helpers for the T22 test suite.

This module is deliberately named so pytest does **not** collect it. It builds
the T01-T21 records T22 consumes:

* real T19 ``IssueStatusResult`` objects (via ``t20_fixtures.green_status``, so
  every status is produced by the real decision code - never hand-rolled);
* real T20 ``CommitResult`` / T21 ``PushResult`` objects, plus their
  ``as_dict()`` views, so the T22 contract is pinned against the production DTOs.

No Git command is run here, no repository is touched and no network is used: T22
is a pure reporting layer, so its fixtures are pure too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from codex_executor import CodexExecutionStatus
from commit_message import CommitMessage
from commit_policy import (
    CommitDecision,
    CommitPhase,
    GateEvaluation,
    GitIdentityReport,
    RepositoryIdentity,
)
from git_commit import CommitResult, CommitStatus
from git_push import PushResult, PushStatus
from push_policy import (
    PushCheckpoint,
    PushDecision,
    PushGateEvaluation,
    PushObservation,
    PushPhase,
    PushRequest,
    RemoteObservation,
    RepositoryObservation,
    build_refspec,
)
from sonar_analysis_waiter import SonarAnalysisState
from test_runner import TestStatus

from t20_fixtures import (
    green_status,
    make_analysis,
    make_codex,
    make_scope,
    make_tests,
    make_verification,
)

#: The issue key every fixture uses (the key T19's fixtures verify).
ISSUE_KEY = "AX1"
#: The agent branch T20 commits on and T21 pushes from and to.
AGENT_BRANCH = "ai/sonar-fix/AX1"
#: The destination remote name.
REMOTE = "origin"
#: The commit T20 created (what T21 must push).
COMMIT = "a" * 40
#: The commit T20 started from (and the remote's value before the push).
PREVIOUS_COMMIT = "b" * 40
#: A repository root that is never the real project working copy.
REPO_ROOT = Path("C:/tmp/t22-fixture-repo")
#: The remote URL the fixtures report (a local file URL - never a credential).
REMOTE_URL = "file:///C:/tmp/t22-fixture-remote.git"

#: T19 status -> the real inputs that make ``determine_issue_status`` report it.
T19_INPUTS: Dict[str, Dict[str, object]] = {
    "fixed": {},
    "still-open": {"verification": make_verification(present=True)},
    "analysis-failed": {"analysis": make_analysis(SonarAnalysisState.FAILED)},
    "tests-failed": {"tests": make_tests(TestStatus.FAILED)},
    "scope-invalid": {"scope": make_scope(valid=False)},
    "codex-failed": {"codex": make_codex(status=CodexExecutionStatus.FAILED)},
    "review-required": {"verification": make_verification(retrieved=False)},
}


def t19_result(status: str = "fixed"):
    """A real T19 result for one of the seven production statuses.

    The status is produced by ``determine_issue_status`` itself and then
    asserted, so a fixture can never drift away from the T19 contract.
    """
    if status not in T19_INPUTS:  # pragma: no cover - a broken fixture call
        raise AssertionError(f"unknown T19 status fixture {status!r}")
    result = green_status(**T19_INPUTS[status])
    assert result.status.value == status, (
        f"fixture for {status!r} produced {result.status.value!r}: "
        f"{result.reason_text}"
    )
    return result


def commit_result(
    *,
    status: CommitStatus = CommitStatus.COMMITTED,
    commit_sha: str = COMMIT,
    previous_head: str = PREVIOUS_COMMIT,
    new_head: Optional[str] = None,
    branch: str = AGENT_BRANCH,
    issue_key: str = ISSUE_KEY,
) -> CommitResult:
    """A real T20 ``CommitResult`` for one status.

    Every field T22 reads is populated the way T20 populates it: ``commit_sha``
    equals ``new_head`` for a verified commit and ``needs_attention`` follows
    T20's own rule (``COMMIT_FAILED``/``COMMIT_UNVERIFIED``).
    """
    committed = status is CommitStatus.COMMITTED
    head = new_head if new_head is not None else (commit_sha if committed else None)
    return CommitResult(
        status=status,
        reason=f"T20 fixture: {status.value}.",
        gates=GateEvaluation(
            phase=CommitPhase.VERIFY,
            gates=(),
            decision=CommitDecision.PROCEED,
        ),
        stage_records=(),
        approved_paths=("src/app.py",),
        previous_head=previous_head,
        new_head=head,
        commit_sha=head,
        commit_message=CommitMessage(
            subject="fix(sonar): AX1 src/app.py",
            issue_key=issue_key,
            rule="python:S1481",
            file_path="src/app.py",
            body=(f"Sonar-Issue: {issue_key}",),
        ),
        identity=GitIdentityReport(
            name="Test User",
            email="test@example.com",
            actor_name="Test User",
            actor_email="test@example.com",
            committer_name="Test User",
            committer_email="test@example.com",
            explicit=True,
            problems=(),
        ),
        repository=RepositoryIdentity(
            expected_root=str(REPO_ROOT),
            worktree_root=str(REPO_ROOT),
            git_dir=str(REPO_ROOT / ".git"),
            common_dir=str(REPO_ROOT / ".git"),
            index_path=str(REPO_ROOT / ".git" / "index"),
            head_commit=head,
            branch=branch,
            detached=False,
            matches_expected=True,
        ),
    )


def _known_status(enum_cls, value, default):
    """``enum_cls(value)`` when it is a real member, else ``default``.

    Lets a fixture build a faithful base record and then hand the raw (possibly
    unknown) status string through, which is exactly how a hostile or corrupted
    upstream record would arrive.
    """
    try:
        return enum_cls(value)
    except ValueError:
        return default


def commit_view(**overrides) -> Dict[str, object]:
    """The ``as_dict()`` view of a T20 result, with fields overridable.

    Used for the hostile/corrupt records a real DTO cannot express (a
    ``new_head`` that disagrees with ``commit_sha``, an ``is_committed`` flag
    that contradicts the status, a closed commit-message key, and so on).
    """
    status = _known_status(
        CommitStatus, overrides.get("status", "committed"), CommitStatus.COMMITTED
    )
    view: Dict[str, object] = commit_result(status=status).as_dict()
    view.update(overrides)
    return view



def push_result(
    *,
    status: PushStatus = PushStatus.PUSHED,
    commit: str = COMMIT,
    previous_commit: str = PREVIOUS_COMMIT,
    branch: str = AGENT_BRANCH,
    remote_branch: str = AGENT_BRANCH,
) -> PushResult:
    """A real T21 ``PushResult`` for one status.

    ``push_attempted`` follows T21's own rule: the recorded
    :class:`PushObservation` says whether the one push command was attempted, and
    a verified/unverified push always recorded one attempt.
    """
    attempted = status in (PushStatus.PUSHED, PushStatus.PUSH_UNVERIFIED)
    refspec = build_refspec(branch, remote_branch)
    remote_after = commit if status is PushStatus.PUSHED else None
    return PushResult(
        status=status,
        reason=f"T21 fixture: {status.value}.",
        gates=PushGateEvaluation(
            phase=PushPhase.VERIFY,
            gates=(),
            decision=(
                PushDecision.PROCEED
                if status is PushStatus.PUSHED
                else PushDecision.REFUSE
            ),
        ),
        stage_records=(),
        request=PushRequest(
            repository_path=str(REPO_ROOT),
            expected_commit=commit,
            branch=branch,
            remote=REMOTE,
            remote_branch=remote_branch,
        ),
        repository=RepositoryObservation(
            expected_root=str(REPO_ROOT),
            worktree_root=str(REPO_ROOT),
            git_dir=str(REPO_ROOT / ".git"),
            common_dir=str(REPO_ROOT / ".git"),
            index_path=str(REPO_ROOT / ".git" / "index"),
            head_commit=commit,
            branch=branch,
            is_bare=False,
            head_exists=True,
        ),
        remote=RemoteObservation(
            name=REMOTE,
            exists=True,
            urls=(REMOTE_URL,),
            fetch_url=REMOTE_URL,
            url_well_formed=True,
            branch_present=True,
            branch_commit=previous_commit,
        ),
        push=PushObservation(
            attempted=attempted,
            launched=attempted,
            argv=("git", "push", REMOTE, refspec) if attempted else (),
            returncode=(
                0
                if status is PushStatus.PUSHED
                else (128 if attempted else None)
            ),
        ),
        checkpoint=_checkpoint(commit, branch),
        final_checkpoint=_checkpoint(commit, branch),
        expected_commit=commit,
        remote_before_commit=previous_commit,
        remote_after_commit=remote_after,
        branch=branch,
        remote_branch=remote_branch,
        refspec=refspec,
    )


def _checkpoint(commit: str, branch: str) -> PushCheckpoint:
    """A clean pre-push checkpoint at ``commit``."""
    return PushCheckpoint(
        head_commit=commit,
        branch=branch,
        is_clean=True,
        changed_files=(),
        staged_files=(),
    )


def push_view(**overrides) -> Dict[str, object]:
    """The ``as_dict()`` view of a T21 result, with fields overridable."""
    status = _known_status(
        PushStatus, overrides.get("status", "pushed"), PushStatus.PUSHED
    )
    view: Dict[str, object] = push_result(status=status).as_dict()
    view.update(overrides)
    return view


def issue_entry(
    *,
    issue_key: str = ISSUE_KEY,
    issue_status: object = None,
    commit: object = None,
    push: object = None,
):
    """One ``IssueLifecycleInput`` for a test (``FIXED`` unless overridden)."""
    from overall_report import IssueLifecycleInput

    return IssueLifecycleInput(
        issue_key=issue_key,
        issue_status=(
            t19_result("fixed") if issue_status is None else issue_status
        ),
        commit_result=commit,
        push_result=push,
    )


def happy_entry(*, issue_key: str = ISSUE_KEY):
    """The end-to-end happy path: ``FIXED`` + ``COMMITTED`` + ``PUSHED``.

    The T20 commit message carries ``issue_key`` (as the real T20 would), so the
    T19/T20 identity link stays consistent for non-default keys.
    """
    return issue_entry(
        issue_key=issue_key,
        commit=commit_result(issue_key=issue_key),
        push=push_result(),
    )


def review_needed_entry(*, issue_key: str = ISSUE_KEY):
    """A real T19 ``REVIEW_REQUIRED`` result with no later evidence."""
    return issue_entry(
        issue_key=issue_key, issue_status=t19_result("review-required")
    )

