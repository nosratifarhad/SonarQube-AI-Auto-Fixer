"""End-to-end tests for the T30 orchestration layer.

All I/O is faked (see ``tests/pipeline_fakes.py``); the real production result
DTOs are used wherever a report or the T19 decision reads them, so the tests
verify the wiring rather than a stub. No Git, SonarQube, Codex or project
toolchain is required.
"""

import pytest

from codex_executor import CodexExecutionStatus, CodexResult
from issue_status import IssueFinalStatus
from overall_report import OverallStatus
from pipeline.config import PipelineConfig
from pipeline.run import FixPipeline
from per_issue_report import PerIssueOutcome
from repository import RepositoryError

from pipeline_fakes import (
    build_dependencies,
    make_issue,
    FakeCodex,
    FakeCommitExecutor,
    FakeDiffInspector,
    FakePushExecutor,
    FakeRepository,
    FakeTestRunner,
    FakeVerifier,
)


@pytest.fixture
def config(tmp_path):
    """A factory for a valid, fully-configured :class:`PipelineConfig`."""

    def _make(**overrides) -> PipelineConfig:
        values = dict(
            repository_url="https://example.invalid/demo.git",
            source_branch="main",
            work_dir=tmp_path / "work",
            test_command=("pytest", "-q"),
            analysis_command=("sonar-scanner",),
            sonar_url="https://sonar.example.invalid",
            project_key="demo",
            sonar_token="super-secret-token",
            default_branch="main",
            branch_timestamp="20260101-000000",
            max_issues=5,
            max_iterations=3,
            allowed_rules=("python:S1481",),
        )
        values.update(overrides)
        return PipelineConfig(**values)

    return _make


def run_pipeline(issue, config_value, dependencies):
    pipeline = FixPipeline(
        client=object(), config=config_value, dependencies=dependencies
    )
    return pipeline.run([issue], discovered_issue_count=1)


def test_happy_path_is_fixed_without_commit(config):
    issue = make_issue()
    deps = build_dependencies()
    result = run_pipeline(issue, config(commit_fixes=False), deps)

    assert len(result.issue_results) == 1
    entry = result.issue_results[0]
    assert entry.issue_status is not None
    assert entry.issue_status.status is IssueFinalStatus.FIXED
    assert deps.commit_executor.calls == []
    assert deps.push_executor.calls == []
    assert result.issue_limit.is_allowed is True
    # T23: a verified fix that was not committed is NOT_COMPLETED, never SUCCESS.
    assert entry.per_issue_report is not None
    assert entry.per_issue_report.is_valid is True
    assert entry.per_issue_report.outcome is PerIssueOutcome.NOT_COMPLETED
    assert entry.per_issue_report.issue_fixed is True
    assert entry.per_issue_report.change_committed is False


def test_issue_limit_refusal_processes_nothing(config):
    issue = make_issue()
    deps = build_dependencies()
    result = run_pipeline(issue, config(max_issues=0), deps)

    assert result.issue_results == ()
    assert result.issue_limit.is_allowed is False
    assert deps.repository.calls == []
    assert result.overall_report.status is OverallStatus.EMPTY


def test_rule_allowlist_blocks_before_any_repository_work(config):
    issue = make_issue(rule="python:S9999")
    deps = build_dependencies()
    result = run_pipeline(issue, config(), deps)

    entry = result.issue_results[0]
    assert entry.blocked_stage == "rule_allowlist"
    assert entry.issue_status is None
    assert deps.repository.calls == []


def test_iteration_limit_blocks_after_allowlist(config):
    issue = make_issue()
    deps = build_dependencies()
    result = run_pipeline(issue, config(max_iterations=0), deps)

    entry = result.issue_results[0]
    assert entry.blocked_stage == "iteration_limit"
    assert deps.repository.calls == []


def test_missing_default_branch_blocks_before_clone(config):
    issue = make_issue()
    deps = build_dependencies()
    result = run_pipeline(issue, config(default_branch=None), deps)

    entry = result.issue_results[0]
    assert entry.blocked_stage == "branch_protection"
    assert entry.branch_protection is not None
    assert deps.repository.calls == []


def test_codex_failure_classifies_as_codex_failed(config):
    issue = make_issue()
    failed = CodexResult(
        status=CodexExecutionStatus.FAILED,
        exit_code=1,
        stdout="",
        stderr="",
        command=("codex", "exec"),
        error="boom",
    )
    deps = build_dependencies(codex=FakeCodex(failed))
    result = run_pipeline(issue, config(), deps)

    assert result.issue_results[0].issue_status.status is IssueFinalStatus.CODEX_FAILED
    assert deps.commit_executor.calls == []


def test_tests_failure_classifies_as_tests_failed(config):
    issue = make_issue()
    deps = build_dependencies(test_runner=FakeTestRunner(passed=False))
    result = run_pipeline(issue, config(), deps)

    assert result.issue_results[0].issue_status.status is IssueFinalStatus.TESTS_FAILED


def test_scope_violation_classifies_as_scope_invalid(config):
    issue = make_issue()
    deps = build_dependencies(
        diff_inspector=FakeDiffInspector(changed_files=("src/app.py", "src/other.py"))
    )
    result = run_pipeline(issue, config(), deps)

    assert result.issue_results[0].issue_status.status is IssueFinalStatus.SCOPE_INVALID


def test_sonarqube_unavailable_requires_review(config):
    issue = make_issue()
    deps = build_dependencies(verifier=FakeVerifier(retrieval_succeeded=False))
    result = run_pipeline(issue, config(), deps)

    assert (
        result.issue_results[0].issue_status.status
        is IssueFinalStatus.REVIEW_REQUIRED
    )


def test_commit_and_push_are_wired_when_fixed_and_enabled(config):
    issue = make_issue()
    commit_executor = FakeCommitExecutor(
        committed=True, branch="ai/sonar-fix/AX1-20260101-000000"
    )
    push_executor = FakePushExecutor(pushed=True)
    deps = build_dependencies(
        commit_executor=commit_executor,
        push_executor=push_executor,
    )
    result = run_pipeline(
        issue, config(commit_fixes=True, push_fixes=True), deps
    )

    entry = result.issue_results[0]
    assert entry.issue_status.status is IssueFinalStatus.FIXED
    assert len(commit_executor.calls) == 1
    assert len(push_executor.calls) == 1
    assert entry.commit_result is not None
    assert entry.push_result is not None
    assert push_executor.calls[0]["commit_result"] is entry.commit_result


def test_push_is_not_attempted_when_commit_is_refused(config):
    issue = make_issue()
    commit_executor = FakeCommitExecutor(committed=False)
    push_executor = FakePushExecutor()
    deps = build_dependencies(
        commit_executor=commit_executor,
        push_executor=push_executor,
    )
    result = run_pipeline(
        issue, config(commit_fixes=True, push_fixes=True), deps
    )

    assert len(commit_executor.calls) == 1
    assert push_executor.calls == []
    assert result.issue_results[0].commit_result is not None
    assert result.issue_results[0].push_result is None


def test_repository_failure_is_a_blocked_attempt_not_a_crash(config):
    issue = make_issue()

    class BrokenRepository(FakeRepository):
        def clone(self, repository_url, destination):
            raise RepositoryError("clone failed")

    deps = build_dependencies(repository=BrokenRepository())
    result = run_pipeline(issue, config(), deps)

    entry = result.issue_results[0]
    assert entry.blocked_stage == "pipeline_error"
    assert "RepositoryError" in entry.blocked_reason
    assert entry.issue_status is None


def test_blocked_issue_is_excluded_from_the_overall_entries(config):
    issue = make_issue(rule="python:S9999")
    deps = build_dependencies()
    result = run_pipeline(issue, config(), deps)

    assert result.overall_report.total_issues == 0
    assert result.overall_report.status is OverallStatus.EMPTY


def test_logs_are_policy_bound_and_never_leak_the_token(config):
    issue = make_issue()
    deps = build_dependencies()
    result = run_pipeline(issue, config(), deps)

    assert result.logs
    serialized = repr(result.as_dict())
    assert "super-secret-token" not in serialized


def test_unexpected_error_keeps_redacted_diagnostics_but_never_publishes_them(config):
    issue = make_issue()
    secret = "ghp_" + "a" * 36

    class LeakyRepository(FakeRepository):
        def clone(self, repository_url, destination):
            raise RepositoryError(
                "clone of https://alice:s3cr3t-value@git.example/repo.git "
                f"failed for token {secret}"
            )

    deps = build_dependencies(repository=LeakyRepository())
    result = run_pipeline(issue, config(), deps)
    entry = result.issue_results[0]

    assert entry.blocked_stage == "pipeline_error"
    # The internal diagnostic is preserved for in-process debugging...
    assert entry.diagnostics
    assert "RepositoryError" in entry.diagnostics[0]
    # ...but every credential-shaped span is redacted in it and in the reason.
    assert "[REDACTED]" in entry.blocked_reason
    for text in (entry.blocked_reason, entry.diagnostics[0]):
        assert "s3cr3t-value" not in text
        assert secret not in text
    # Diagnostics never reach a serialized view or the JSON result.
    assert "diagnostics" not in entry.as_dict()
    serialized = repr(result.as_dict())
    assert "s3cr3t-value" not in serialized
    assert secret not in serialized


def test_literal_configured_secret_is_scrubbed_from_a_blocked_reason(config):
    issue = make_issue()

    class TokenLeakRepository(FakeRepository):
        def clone(self, repository_url, destination):
            raise RepositoryError("request rejected for token super-secret-token")

    deps = build_dependencies(repository=TokenLeakRepository())
    result = run_pipeline(issue, config(), deps)
    entry = result.issue_results[0]

    assert entry.blocked_stage == "pipeline_error"
    assert "super-secret-token" not in entry.blocked_reason
    assert "super-secret-token" not in repr(result.as_dict())
    assert "super-secret-token" not in repr(entry.diagnostics)


def test_push_flag_without_commit_flag_can_never_push(config):
    issue = make_issue()
    commit_executor = FakeCommitExecutor(committed=True)
    push_executor = FakePushExecutor()
    deps = build_dependencies(
        commit_executor=commit_executor,
        push_executor=push_executor,
    )
    result = run_pipeline(
        issue, config(commit_fixes=False, push_fixes=True), deps
    )

    assert commit_executor.calls == []
    assert push_executor.calls == []
    assert result.issue_results[0].push_result is None


def test_pre_commit_uncertainty_refusal_blocks_commit(config, monkeypatch):
    issue = make_issue()
    commit_executor = FakeCommitExecutor(committed=True)
    deps = build_dependencies(commit_executor=commit_executor)
    pipeline = FixPipeline(
        client=object(),
        config=config(commit_fixes=True),
        dependencies=deps,
    )
    # Force T27 PRE_COMMIT evidence to be absent, which fails closed.
    monkeypatch.setattr(pipeline, "_pre_commit_evidence", lambda **kwargs: ())

    result = pipeline.run([issue], discovered_issue_count=1)

    assert result.issue_results[0].issue_status.status is IssueFinalStatus.FIXED
    assert commit_executor.calls == []


def test_pre_push_uncertainty_refusal_blocks_push(config):
    issue = make_issue()
    # A committed result on a different branch contradicts branch consistency,
    # so T27 PRE_PUSH must refuse and no push may be attempted.
    commit_executor = FakeCommitExecutor(committed=True, branch="somewhere-else")
    push_executor = FakePushExecutor()
    deps = build_dependencies(
        commit_executor=commit_executor,
        push_executor=push_executor,
    )
    result = run_pipeline(
        issue, config(commit_fixes=True, push_fixes=True), deps
    )

    assert len(commit_executor.calls) == 1
    assert push_executor.calls == []
    assert result.issue_results[0].push_result is None


def test_cleanup_cannot_delete_outside_the_work_base(config, tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    pipeline = FixPipeline(
        client=object(),
        config=config(work_dir=base),
        dependencies=build_dependencies(),
    )

    pipeline._cleanup_work_directory(outside)
    assert keep.exists()
    pipeline._cleanup_work_directory(base)
    assert base.exists()

    child = base / "sonar-fix-x"
    child.mkdir()
    (child / "clone.txt").write_text("x", encoding="utf-8")
    pipeline._cleanup_work_directory(child)
    assert not child.exists()


def test_cleanup_respects_keep_work_dir(config, tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    child = base / "sonar-fix-x"
    child.mkdir()

    pipeline = FixPipeline(
        client=object(),
        config=config(work_dir=base, keep_work_dir=True),
        dependencies=build_dependencies(),
    )

    pipeline._cleanup_work_directory(child)
    assert child.exists()
