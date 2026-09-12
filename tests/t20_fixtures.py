"""Shared, non-test helpers for the T20 test suite.

This module is deliberately named so pytest does **not** collect it. It builds
the T01-T19 records T20 consumes (so every T20 test starts from a real,
``FIXED`` T19 result instead of a hand-rolled stub) and it provides a recording
Git runner that lets a test observe every argv T20 executes and inject faults.

No network, no real Codex CLI, no live SonarQube server and no commit in the
real repository: all Git work happens in real repositories under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from analysis_correlation import AnalysisCorrelation
from change_scope import ChangeScopeResult
from codex_executor import CodexExecutionStatus, CodexResult
from codex_result import analyze_codex_result
from issue_status import IssueFinalStatus, IssueStatusResult, determine_issue_status
from models import SonarIssue
from sonar_analysis import SonarAnalysisStatus, SonarAnalysisTriggerResult
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_issue_verification import IssueMatchType, SonarIssueVerificationResult
from test_result import analyze_test_result
from test_runner import TestResult, TestStatus

import git_commit as git_commit_module

#: The demo target file used by the shared ``conftest`` fixtures.
TARGET = "src/app.py"
#: The agent branch the T20 tests run on (T07's namespace).
AGENT_BRANCH = "ai/sonar-fix/AX1"


def git_run(cwd: Path, *args: str, check: bool = True) -> str:
    """Run ``git`` inside ``cwd`` and return trimmed stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)!r} failed in {cwd}:\n{result.stderr}"
        )
    return result.stdout.strip()


def make_codex(
    *, status=CodexExecutionStatus.SUCCESS, uncertain=False, stdout=None
):
    """A T11 Codex analysis (successful and certain by default)."""
    text = (
        stdout
        if stdout is not None
        else ("I am uncertain about this fix" if uncertain else "fixed the issue")
    )
    if status is CodexExecutionStatus.SUCCESS:
        exit_code, error = 0, None
    elif status is CodexExecutionStatus.FAILED:
        exit_code, error = 2, "Codex exited with code 2."
    else:
        exit_code, error = None, "Codex did not complete."
    return analyze_codex_result(
        CodexResult(
            status=status,
            exit_code=exit_code,
            stdout=text,
            stderr="",
            command=("codex", "exec"),
            error=error,
        )
    )


def make_scope(*, valid=True, modified=True, expected=(TARGET,)):
    """A T13 change-scope result (valid, target modified, by default)."""
    changed = (TARGET,) if modified else ()
    return ChangeScopeResult(
        is_valid=valid,
        expected_files=tuple(expected),
        changed_files=changed,
        unexpected_files=() if valid else ("src/other.py",),
        has_changes=modified or not valid,
        expected_file_modified=modified,
        reasons=("Scope policy: only 'src/app.py' may change for this issue.",),
    )


def make_tests(status=TestStatus.PASSED):
    """A T15 test outcome (passed by default)."""
    if status is TestStatus.PASSED:
        exit_code, error = 0, None
    elif status is TestStatus.FAILED:
        exit_code, error = 1, "Project tests exited with code 1."
    else:
        exit_code, error = None, "Project tests did not run."
    return analyze_test_result(
        TestResult(
            status=status,
            exit_code=exit_code,
            stdout="",
            stderr="",
            command=("pytest", "-q"),
            error=error,
        )
    )


def make_analysis(status=SonarAnalysisState.SUCCESS, **kwargs):
    """A T17 analysis completion (successful by default)."""
    failure = None
    if status is SonarAnalysisState.FAILED:
        failure = "The SonarQube compute-engine task failed."
    elif status is SonarAnalysisState.CANCELED:
        failure = "The SonarQube compute-engine task was canceled."
    elif status is SonarAnalysisState.TIMEOUT:
        failure = "The analysis is unverified."
    elif status is SonarAnalysisState.UNKNOWN:
        failure = "The triggered analysis cannot be attributed to this run."
    kwargs.setdefault("task_id", "AY1")
    kwargs.setdefault("elapsed_seconds", 2.0)
    kwargs.setdefault("poll_count", 1)
    kwargs.setdefault("reason", f"analysis {status.value}")
    return SonarAnalysisCompletion(status=status, failure_reason=failure, **kwargs)


def make_trigger(
    status=SonarAnalysisStatus.TRIGGERED, *, task_id="AY1", error=None
):
    """A T16 analysis trigger (successful, with a task id, by default)."""
    return SonarAnalysisTriggerResult(
        status=status,
        exit_code=0 if status is SonarAnalysisStatus.TRIGGERED else 1,
        stdout="",
        stderr="",
        command=("sonar-scanner",),
        task_id=task_id,
        error=error,
    )


def make_verification(
    *,
    present=False,
    reliable=True,
    retrieved=True,
    match_type=IssueMatchType.KEY,
    correlation=AnalysisCorrelation.CORRELATED,
):
    """A T18 verification result (a reliable, correlated absence by default)."""
    issue = SonarIssue(
        key="AX1",
        rule="python:S1481",
        severity="MAJOR",
        issue_type="CODE_SMELL",
        message="remove this unused variable",
        component=f"demo:{TARGET}",
        line=3,
        status="OPEN",
    )
    error = None if retrieved else "SonarQube issue retrieval failed."
    return SonarIssueVerificationResult(
        original_issue=issue,
        original_issue_key="AX1",
        retrieval_succeeded=retrieved,
        current_issues=(issue,) if present else (),
        matches=(issue,) if present else (),
        matching_current_issue=issue if present and reliable else None,
        match_type=match_type if (present or retrieved) else IssueMatchType.NONE,
        is_present=present,
        identity_reliable=reliable,
        page_complete=True,
        retrieved_count=1 if present else 0,
        skipped_entries=0,
        reason=(
            "The original issue is still reported as open."
            if present
            else "The original issue is not reported as open by the new analysis."
        ),
        reasons=(
            "Identity uses the original SonarQube issue key 'AX1'.",
            "The original issue is still reported as open."
            if present
            else "The original issue key is not among the current open issues.",
        ),
        error=error,
        correlation=correlation,
        correlation_reason="The issue snapshot belongs to the verified analysis.",
    )


def green_status(**overrides) -> IssueStatusResult:
    """A real T19 result that is ``FIXED`` unless a test overrides an input."""
    inputs = {
        "codex": make_codex(),
        "scope": make_scope(),
        "tests": make_tests(),
        "analysis": make_analysis(),
        "verification": make_verification(),
        "trigger": make_trigger(),
    }
    inputs.update(overrides)
    return determine_issue_status(**inputs)


def assert_fixed(result: IssueStatusResult) -> None:
    """Guard the fixtures: every T20 test must start from a real ``FIXED``."""
    assert result.status is IssueFinalStatus.FIXED, result.reason_text


def real_runner(args: Sequence[str], cwd: str, env: Dict[str, str], timeout: float):
    """The production-shaped runner used by the recording runner."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def subcommand_of(args: Sequence[str]) -> str:
    """The Git subcommand of a full argv (``git -C <root> <subcommand> ...``)."""
    return git_commit_module._split_git_invocation(list(args)[3:])[0]


def fake_completed(returncode=0, stdout="", stderr=""):
    """A minimal ``subprocess.CompletedProcess`` look-alike."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class RecordingRunner:
    """Records every Git argv/environment and can inject faults or side effects.

    ``on_call`` receives ``(subcommand, argv, cwd, runner)`` and may return a
    ``CompletedProcess`` look-alike to replace the real execution, or ``None``
    to run the real command.
    """

    def __init__(self, on_call: Optional[Callable] = None) -> None:
        self.commands: List[List[str]] = []
        self.environments: List[Dict[str, str]] = []
        self._on_call = on_call

    def __call__(self, args, cwd, env, timeout):
        argv = [str(item) for item in args]
        self.commands.append(argv)
        self.environments.append(dict(env))
        subcommand = subcommand_of(argv)
        if self._on_call is not None:
            override = self._on_call(subcommand, argv, cwd, self)
            if override is not None:
                return override
        return real_runner(argv, cwd, dict(env), timeout)

    # -- assertion helpers used by the tests ----------------------------

    @property
    def subcommands(self) -> Tuple[str, ...]:
        """Every Git subcommand that was attempted, in order."""
        return tuple(subcommand_of(argv) for argv in self.commands)

    def subcommands_matching(self, name: str) -> List[List[str]]:
        """Every recorded argv whose subcommand is ``name``."""
        return [argv for argv in self.commands if subcommand_of(argv) == name]

