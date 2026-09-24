"""Non-collected fakes for the T30 pipeline tests.

These objects implement only the methods :class:`pipeline.run.FixPipeline`
calls, so the whole orchestration can be exercised without Git, SonarQube, the
Codex CLI or a project toolchain. Result DTOs are the **real** production
types wherever the pipeline or a downstream report reads their fields.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from analysis_correlation import AnalysisCorrelation
from codex_executor import CodexExecutionStatus, CodexResult
from context import IssueContext
from git_diff import GitDiffResult
from models import SonarIssue
from sonar_analysis import (
    SonarAnalysisStatus,
    SonarAnalysisTriggerResult,
)
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_issue_verification import (
    IssueMatchType,
    SonarIssueVerificationResult,
)
from test_runner import TestResult, TestStatus
from worktree_baseline import WorktreeSnapshot


def make_issue(**overrides) -> SonarIssue:
    values = dict(
        key="AX1",
        rule="python:S1481",
        severity="MAJOR",
        issue_type="CODE_SMELL",
        message="Unused variable.",
        component="demo:src/app.py",
        line=3,
        status="OPEN",
    )
    values.update(overrides)
    return SonarIssue(**values)


def make_context(issue: SonarIssue, repository_path) -> IssueContext:
    root = Path(repository_path)
    return IssueContext(
        issue=issue,
        repository_path=root,
        source_branch="main",
        agent_branch="ai/sonar-fix/AX1",
        file_path="src/app.py",
        absolute_file_path=root / "src" / "app.py",
        line=3,
    )


class FakeRepository:
    def __init__(self):
        self.calls = []

    def clone(self, repository_url, destination):
        self.calls.append(("clone", repository_url, str(destination)))
        return Path(destination)

    def checkout_source_branch(self, repository_path, branch_name):
        self.calls.append(("checkout", branch_name))
        return branch_name

    def create_agent_branch(self, repository_path, agent_branch_name, expected_source_branch):
        self.calls.append(("branch", agent_branch_name, expected_source_branch))
        return agent_branch_name


class FakeContextPreparer:
    def __init__(self):
        self.calls = []

    def prepare(self, *, issue, repository_path, source_branch, agent_branch):
        self.calls.append((issue.key, agent_branch))
        context = make_context(issue, repository_path)
        return context


class FakeCodex:
    def __init__(self, result: Optional[CodexResult] = None):
        self.result = result or CodexResult(
            status=CodexExecutionStatus.SUCCESS,
            exit_code=0,
            stdout="",
            stderr="",
            command=("codex", "exec"),
        )
        self.prompts = []

    def execute(self, prompt, repository_path):
        self.prompts.append(prompt)
        return self.result


class FakeDiffInspector:
    def __init__(self, *, changed_files=("src/app.py",), is_clean=False):
        self.changed_files = tuple(changed_files)
        self.is_clean = is_clean
        self.calls = []

    def inspect(self, repository_path):
        self.calls.append(str(repository_path))
        return GitDiffResult(
            repository_path=Path(repository_path),
            is_clean=self.is_clean,
            has_changes=not self.is_clean,
            changed_files=self.changed_files,
            untracked_files=(),
            diff_stat="",
            diff_text="",
        )


class FakeTestRunner:
    def __init__(self, *, passed=True):
        self._passed = passed
        self.calls = []

    def run(self, repository_path, test_command):
        self.calls.append(tuple(test_command))
        if self._passed:
            return TestResult(
                status=TestStatus.PASSED,
                exit_code=0,
                stdout="",
                stderr="",
                command=tuple(test_command),
            )
        return TestResult(
            status=TestStatus.FAILED,
            exit_code=1,
            stdout="",
            stderr="",
            command=tuple(test_command),
            error="tests failed",
        )


class FakeTrigger:
    def __init__(self, *, task_id="TASK-1", status=SonarAnalysisStatus.TRIGGERED):
        self.task_id = task_id
        self.status = status
        self.calls = []

    def trigger(self, repository_path, analysis_config=None):
        self.calls.append(str(repository_path))
        return SonarAnalysisTriggerResult(
            status=self.status,
            exit_code=0 if self.status is SonarAnalysisStatus.TRIGGERED else 1,
            stdout="",
            stderr="",
            command=("sonar-scanner",),
            task_id=self.task_id,
        )


class FakeWaiter:
    def __init__(
        self,
        *,
        status=SonarAnalysisState.SUCCESS,
        task_id="TASK-1",
        analysis_id="ANALYSIS-1",
        component_key="demo",
    ):
        self.status = status
        self.task_id = task_id
        self.analysis_id = analysis_id
        self.component_key = component_key
        self.calls = []

    def wait(self, task_id, task_id_resolver=None):
        self.calls.append(task_id)
        return SonarAnalysisCompletion(
            status=self.status,
            task_id=self.task_id,
            elapsed_seconds=0.0,
            poll_count=1,
            reason="done",
            failure_reason=None
            if self.status is SonarAnalysisState.SUCCESS
            else "not successful",
            analysis_id=self.analysis_id,
            component_key=self.component_key,
        )


class FakeVerifier:
    def __init__(
        self,
        *,
        retrieval_succeeded=True,
        is_present=False,
        identity_reliable=True,
        page_complete=True,
        correlation=AnalysisCorrelation.CORRELATED,
    ):
        self.retrieval_succeeded = retrieval_succeeded
        self.is_present = is_present
        self.identity_reliable = identity_reliable
        self.page_complete = page_complete
        self.correlation = correlation
        self.calls = []

    def verify(self, original_issue, *, completion=None):
        self.calls.append(original_issue.key)
        return SonarIssueVerificationResult(
            original_issue=original_issue,
            original_issue_key=original_issue.key,
            retrieval_succeeded=self.retrieval_succeeded,
            current_issues=(),
            matches=(),
            matching_current_issue=None,
            match_type=IssueMatchType.NONE,
            is_present=self.is_present,
            identity_reliable=self.identity_reliable,
            page_complete=self.page_complete,
            retrieved_count=0,
            skipped_entries=0,
            reason="verified",
            reasons=("verified",),
            correlation=self.correlation,
            correlation_reason="correlated",
        )


class FakeBaseline:
    def __init__(self, *, clean=True):
        self.clean = clean
        self.calls = []

    def capture(self, repository_path, *, include_ignored=False):
        self.calls.append((str(repository_path), include_ignored))
        changed = () if self.clean else ("src/app.py",)
        return WorktreeSnapshot(
            repository_path=Path(repository_path),
            head_commit="a" * 40,
            is_clean=self.clean,
            changed_files=changed,
            tracked_modified_files=changed,
            staged_files=(),
            untracked_files=(),
            deleted_files=(),
            renamed_files=(),
            fingerprints={},
            index_equivalent_blobs={},
            ignored_files=(),
            ignored_scan=include_ignored,
        )


class FakeCommitExecutor:
    def __init__(self, *, committed=True, branch="ai/sonar-fix/AX1"):
        self.committed = committed
        self.branch = branch
        self.calls = []

    def commit_safely(self, **kwargs):
        self.calls.append(kwargs)
        result = SimpleNamespace(
            status=SimpleNamespace(value="committed" if self.committed else "refused"),
            is_committed=self.committed,
            is_refusal=not self.committed,
            needs_attention=not self.committed,
            repository=SimpleNamespace(branch=self.branch),
            as_dict=lambda: {
                "status": "committed" if self.committed else "refused",
                "is_committed": self.committed,
            },
        )
        return result


class FakePushExecutor:
    def __init__(self, *, pushed=True):
        self.pushed = pushed
        self.calls = []

    def push_safely(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status=SimpleNamespace(value="pushed" if self.pushed else "push-failed"),
            is_pushed=self.pushed,
            is_refusal=False,
            needs_attention=not self.pushed,
            as_dict=lambda: {
                "status": "pushed" if self.pushed else "push-failed",
                "is_pushed": self.pushed,
            },
        )


def build_dependencies(**overrides):
    """Build a full fake dependency bundle, overriding any attribute."""
    from pipeline.run import PipelineDependencies

    values = dict(
        repository=FakeRepository(),
        context_preparer=FakeContextPreparer(),
        codex=FakeCodex(),
        diff_inspector=FakeDiffInspector(),
        scope_validator=None,
        test_runner=FakeTestRunner(),
        analysis_trigger=FakeTrigger(),
        analysis_waiter=FakeWaiter(),
        verifier=FakeVerifier(),
        baseline_inspector=FakeBaseline(),
        commit_executor=FakeCommitExecutor(),
        push_executor=FakePushExecutor(),
    )
    values.update(overrides)
    if values["scope_validator"] is None:
        from change_scope import ChangeScopeValidator

        values["scope_validator"] = ChangeScopeValidator()
    return PipelineDependencies(**values)
