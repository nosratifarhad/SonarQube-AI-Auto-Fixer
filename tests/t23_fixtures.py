"""Shared, non-test helpers for the T23 test suite.

This module is deliberately named so pytest does **not** collect it. It builds
the real T01-T21 records T23 consumes:

* real T19 ``IssueStatusResult`` objects (via ``t20_fixtures.green_status``, so
  every status is produced by the real decision code - never hand-rolled);
* real T20 ``CommitResult`` / T21 ``PushResult`` objects, plus their
  ``as_dict()`` views and the corrupt variants a real DTO cannot express;
* a real :class:`models.SonarIssue` identity record.

No Git command is run here, no repository is touched and no network is used: T23
is a pure reporting layer, so its fixtures are pure too. (The one test that
exercises the *real* T20/T21 executors builds its own ``tmp_path`` clone.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from commit_message import CommitMessage
from commit_policy import (
    CommitDecision,
    CommitPhase,
    GateEvaluation,
    RepositoryIdentity,
)
from codex_executor import CodexExecutionStatus
from git_commit import CommitResult, CommitStatus
from git_push import PushResult, PushStatus
from models import SonarIssue
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
#: An unrelated commit that must never be reported as the pushed one.
OTHER_COMMIT = "c" * 40
#: A repository root that is never the real project working copy.
REPO_ROOT = Path("C:/tmp/t23-fixture-repo")
#: The remote URL the fixtures report (a local file URL - never a credential).
REMOTE_URL = "file:///C:/tmp/t23-fixture-remote.git"
#: The target file the fixtures' Sonar issue points at.
TARGET = "src/app.py"

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


def issue(**overrides) -> SonarIssue:
    """A real :class:`models.SonarIssue` identity record."""
    values = {
        "key": ISSUE_KEY,
        "rule": "python:S1481",
        "severity": "MAJOR",
        "issue_type": "CODE_SMELL",
        "message": "remove this unused variable",
        "component": f"demo:{TARGET}",
        "line": 3,
        "status": "OPEN",
    }
    values.update(overrides)
    return SonarIssue(**values)


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

    Every field T23 reads is populated the way T20 populates it: ``commit_sha``
    equals ``new_head`` for a verified commit and ``repository.branch`` and the
    commit message's issue key are the ones T20 recorded.
    """
    committed = status is CommitStatus.COMMITTED
    head = new_head if new_head is not None else (commit_sha if committed else None)
    return CommitResult(
        status=status,
        reason=f"T23 fixture: {status.value}.",
        gates=GateEvaluation(
            phase=CommitPhase.VERIFY,
            gates=(),
            decision=CommitDecision.PROCEED,
        ),
        stage_records=(),
        approved_paths=(TARGET,),
        previous_head=previous_head,
        new_head=head,
        commit_sha=head,
        commit_message=CommitMessage(
            subject=f"fix(sonar): {issue_key} {TARGET}",
            issue_key=issue_key,
            rule="python:S1481",
            file_path=TARGET,
            body=(f"Sonar-Issue: {issue_key}",),
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


def _checkpoint(commit: str, branch: str) -> PushCheckpoint:
    """A clean pre-push checkpoint at ``commit``."""
    return PushCheckpoint(
        head_commit=commit,
        branch=branch,
        is_clean=True,
        changed_files=(),
        staged_files=(),
    )


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
        reason=f"T23 fixture: {status.value}.",
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


def t19_view(**overrides) -> Dict[str, object]:
    """A fresh ``as_dict()`` view of a real T19 result, with overrides applied.

    Every nested block is rebuilt by ``as_dict()``, so a test can safely mutate
    the returned mapping to express the corrupt records a real DTO cannot. A
    status the production enum does not know is handed through as raw text,
    exactly how a corrupted upstream record would arrive.
    """
    status = overrides.pop("status", "fixed")
    base = status if status in T19_INPUTS else "fixed"
    view: Dict[str, object] = t19_result(base).as_dict()
    if status != base:
        view["status"] = status
    view.update(overrides)
    return view


def commit_view(**overrides) -> Dict[str, object]:
    """A fresh ``as_dict()`` view of a real T20 result, overridable.

    A status the production enum does not know is handed through as raw text,
    exactly how a corrupted upstream record would arrive.
    """
    raw = overrides.pop("status", "committed")
    status = _known_status(CommitStatus, raw, CommitStatus.COMMITTED)
    view: Dict[str, object] = commit_result(status=status).as_dict()
    if raw != status.value:
        view["status"] = raw
    view.update(overrides)
    return view


def push_view(**overrides) -> Dict[str, object]:
    """A fresh ``as_dict()`` view of a real T21 result, overridable.

    A status the production enum does not know is handed through as raw text.
    """
    raw = overrides.pop("status", "pushed")
    status = _known_status(PushStatus, raw, PushStatus.PUSHED)
    view: Dict[str, object] = push_result(status=status).as_dict()
    if raw != status.value:
        view["status"] = raw
    view.update(overrides)
    return view


#: Sentinel meaning "the test did not mention this input field at all".
_UNSET = object()


def entry(
    *,
    issue_key: str = ISSUE_KEY,
    issue_record: object = _UNSET,
    issue_status: object = _UNSET,
    commit: object = None,
    push: object = None,
):
    """One ``PerIssueInput`` for a test (``FIXED``, no T20/T21, unless set).

    ``issue_record`` and ``issue_status`` default to the fixtures; passing an
    explicit ``None`` means "this evidence was not supplied".
    """
    from per_issue_report import PerIssueInput

    return PerIssueInput(
        issue_key=issue_key,
        issue=issue() if issue_record is _UNSET else issue_record,
        issue_status=(
            t19_result("fixed") if issue_status is _UNSET else issue_status
        ),
        commit_result=commit,
        push_result=push,
    )


def happy_entry(*, issue_key: str = ISSUE_KEY):
    """The end-to-end happy path: ``FIXED`` + ``COMMITTED`` + ``PUSHED``.

    The T20 commit message carries ``issue_key`` (as the real T20 would), so the
    T19/T20 identity link stays consistent for non-default keys.
    """
    return entry(
        issue_key=issue_key,
        commit=commit_result(issue_key=issue_key),
        push=push_result(),
    )


def review_needed_entry(*, issue_key: str = ISSUE_KEY):
    """A real T19 ``REVIEW_REQUIRED`` result with no later evidence."""
    return entry(
        issue_key=issue_key, issue_status=t19_result("review-required")
    )


def build(**overrides):
    """Build a T23 report from fixture defaults; ``entry`` is overridable."""
    from per_issue_report import build_per_issue_report

    return build_per_issue_report(
        entry=overrides.pop("entry", happy_entry()), **overrides
    )


def failed_gates(report) -> Tuple[str, ...]:
    """Every gate of ``report`` that failed, in catalogue order."""
    return tuple(gate.gate_id for gate in report.state.failed_gates)


def not_reached_gates(report) -> Tuple[str, ...]:
    """Every gate of ``report`` whose phase never ran, in catalogue order."""
    return tuple(gate.gate_id for gate in report.state.not_reached_gates)


def gate_ids(report) -> Tuple[str, ...]:
    """Every gate id of ``report``, in catalogue order."""
    return tuple(gate.gate_id for gate in report.state.gates)
