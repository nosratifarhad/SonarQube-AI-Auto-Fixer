"""T20 integration tests: safe Git commit against real temporary repositories.

Every test runs against a **real** Git repository under ``tmp_path`` (a clone of
the shared demo remote, checked out on an agent branch with an explicit fixture
identity). Faults are injected through a recording runner, never by weakening
the production code. The real repository is never touched: there is no commit,
no branch, no checkout, no push, no fetch and no configuration change outside
``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from commit_message import build_commit_message
from commit_policy import (
    CommitDecision,
    CommitPhase,
    CommitPolicyConfig,
    GateStatus,
    evaluate_commit_gates,
)
from git_commit import (
    ALLOWED_GIT_SUBCOMMANDS,
    FORBIDDEN_GIT_SUBCOMMANDS,
    CommitStatus,
    GitCommitError,
    GitCommitExecutor,
)
from analysis_correlation import AnalysisCorrelation
from codex_executor import CodexExecutionStatus
from issue_status import IssueFinalStatus
from secret_scan import SecretScanner
from sonar_analysis_waiter import SonarAnalysisState
from test_result import TestOutcome
from test_runner import TestStatus
from worktree_baseline import (
    BaselineAttribution,
    WorktreeBaselineInspector,
    WorktreeSnapshot,
    attribute_changes,
)
from t20_fixtures import (
    AGENT_BRANCH,
    TARGET,
    RecordingRunner,
    assert_fixed,
    fake_completed,
    git_run,
    green_status,
    make_analysis,
    make_codex,
    make_scope,
    make_tests,
    make_trigger,
    make_verification,
    subcommand_of,
)

import git_commit as git_commit_module

NESTED = "nested/dir/deep.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def inspector() -> WorktreeBaselineInspector:
    """A baseline inspector that reads ignored files and content identity."""
    return WorktreeBaselineInspector(timeout_seconds=60.0)


@pytest.fixture
def repo(make_remote_repo, rm, clone_destination, git) -> Path:
    """A real clone on an agent branch with explicit identity and ignore rules."""
    remote = make_remote_repo()
    work = rm.clone(str(remote), clone_destination())
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    (work / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (work / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    git_run(work, "add", "--", ".gitignore", ".gitattributes")
    git_run(work, "commit", "-m", "chore: ignore rules")
    git_run(work, "checkout", "-q", "-b", AGENT_BRANCH)
    return work


def write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` (creating parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def agent_edit(
    repo: Path,
    inspector: WorktreeBaselineInspector,
    text: str = "fixed\n",
    relative: str = TARGET,
):
    """Simulate the agent edit and return ``(baseline, after, attribution)``."""
    baseline = inspector.capture(repo, include_ignored=True)
    write(repo / relative, text)
    after = inspector.capture(repo, include_ignored=True)
    return baseline, after, attribute_changes(baseline, after)


def run_t20(
    repo: Path,
    inspector: WorktreeBaselineInspector,
    *,
    text: str = "fixed\n",
    relative: str = TARGET,
    status=None,
    secrets=(),
    config=None,
    runner=None,
    environment=None,
    message=None,
    edit: bool = True,
):
    """Run T20 for one agent edit and return the :class:`CommitResult`."""
    baseline = inspector.capture(repo, include_ignored=True)
    if edit:
        write(repo / relative, text)
    after = inspector.capture(repo, include_ignored=True)
    attribution = attribute_changes(baseline, after)
    if status is None:
        status = green_status()
        assert_fixed(status)
    executor = GitCommitExecutor(
        timeout_seconds=60.0,
        runner=runner,
        config=config,
        environment=environment,
    )
    return executor.commit_safely(
        repository_path=repo,
        issue_status=status,
        baseline=baseline,
        after=after,
        attribution=attribution,
        secrets=secrets,
        message=message,
    )


# ---------------------------------------------------------------------------
# 1. The happy path
# ---------------------------------------------------------------------------


class TestSuccessfulCommit:
    def test_commits_exactly_the_agent_change(self, repo, inspector):
        before = git_run(repo, "rev-parse", "HEAD")
        result = run_t20(repo, inspector)
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.is_committed is True
        assert result.gates.decision is CommitDecision.PROCEED
        assert result.gates.failed_gates == ()
        after = git_run(repo, "rev-parse", "HEAD")
        assert after != before
        assert result.previous_head == before
        assert result.new_head == after
        assert result.approved_paths == (TARGET,)

    def test_commit_message_is_deterministic_and_bounded(self, repo, inspector):
        result = run_t20(repo, inspector)
        expected = build_commit_message(
            rule="python:S1481", file_path=TARGET, issue_key="AX1"
        )
        assert result.commit_message.text == expected.text
        assert result.commit_message.subject == (
            "fix(sonar): resolve python:S1481 in src/app.py"
        )
        assert result.commit_message.line_count == 4

    def test_commit_contains_exactly_the_approved_paths_and_blobs(
        self, repo, inspector
    ):
        result = run_t20(repo, inspector)
        committed = git_run(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).split("\n")
        assert committed == [TARGET]
        blob = git_run(repo, "rev-parse", f"HEAD:{TARGET}")
        assert blob == result.approved_content_ids[TARGET] == result.staged_blob_ids[TARGET]

    def test_history_is_exactly_one_commit_with_the_expected_parent(
        self, repo, inspector
    ):
        before = git_run(repo, "rev-parse", "HEAD")
        run_t20(repo, inspector)
        assert git_run(repo, "rev-parse", "HEAD^") == before
        assert git_run(repo, "rev-list", "--count", f"{before}..HEAD") == "1"
        assert git_run(repo, "log", "-1", "--format=%s") == (
            "fix(sonar): resolve python:S1481 in src/app.py"
        )

    def test_commit_uses_the_verified_identity(self, repo, inspector):
        run_t20(repo, inspector)
        assert git_run(repo, "log", "-1", "--format=%an") == "Test User"
        assert git_run(repo, "log", "-1", "--format=%ae") == "test@example.com"
        assert git_run(repo, "log", "-1", "--format=%cn") == "Test User"

    def test_branch_and_worktree_are_unchanged_by_the_commit(self, repo, inspector):
        before_branch = git_run(repo, "symbolic-ref", "--short", "HEAD")
        run_t20(repo, inspector)
        assert git_run(repo, "symbolic-ref", "--short", "HEAD") == before_branch
        assert git_run(repo, "status", "--porcelain") == ""

    def test_stage_records_document_every_state_machine_stage(self, repo, inspector):
        result = run_t20(repo, inspector)
        stages = [record.stage for record in result.stage_records]
        for expected in (
            "S0-validate-inputs",
            "S1-identify-repository",
            "S2-check-environment",
            "S3-check-branch",
            "S4-check-identity",
            "S5-check-repo-state",
            "S6-verify-baseline",
            "S7-resolve-approved-set",
            "S8-scan-approved-content",
            "S9-stage-exact-files",
            "S10-verify-index",
            "S11-scan-staged-content",
            "S12-final-pre-commit-check",
            "S13-commit",
            "S14-verify-commit",
        ):
            assert expected in stages, stages

    def test_result_is_serialisable_and_secret_free(self, repo, inspector):
        payload = run_t20(repo, inspector).as_dict()
        assert payload["status"] == "committed"
        assert payload["gates"]["decision"] == "proceed"
        assert payload["approved_paths"] == [TARGET]
        assert payload["new_head"] == payload["commit_sha"]

    def test_commit_of_a_nested_file_works(self, repo, inspector):
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / NESTED, "deep fixed\n")
        after = inspector.capture(repo, include_ignored=True)
        result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
            repository_path=repo,
            issue_status=green_status(scope=make_scope(expected=(NESTED,))),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.approved_paths == (NESTED,)
        assert git_run(repo, "log", "-1", "--format=%s") == (
            "fix(sonar): resolve python:S1481 in nested/dir/deep.py"
        )



# ---------------------------------------------------------------------------
# 2. Refusals that happen before the first Git write
# ---------------------------------------------------------------------------


def head_of(repo: Path) -> str:
    """The current HEAD commit id."""
    return git_run(repo, "rev-parse", "HEAD")


def assert_refused(result, *, gate: str = "", needle: str = "") -> None:
    """Assert an executable refusal (never a partial or silent commit)."""
    assert result.status is CommitStatus.REFUSED, result.reason
    assert result.new_head is None
    if gate:
        failing = {item.gate_id for item in result.gates.failed_gates}
        assert gate in failing, sorted(failing)
    if needle:
        detail = result.reason
        if gate:
            detail = result.gates.gate(gate).reason
        assert needle.casefold() in detail.casefold(), detail


def assert_nothing_was_written(repo: Path, runner: RecordingRunner, before: str):
    """No staging, no commit, no history or index movement at all."""
    assert "add" not in runner.subcommands, runner.subcommands
    assert "commit" not in runner.subcommands, runner.subcommands
    for forbidden in FORBIDDEN_GIT_SUBCOMMANDS:
        assert forbidden not in runner.subcommands, (forbidden, runner.subcommands)
    assert head_of(repo) == before
    assert git_run(repo, "diff", "--cached", "--name-only") == ""


class TestPreGitRefusals:
    def test_a_non_fixed_t19_status_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(codex=make_codex(status=CodexExecutionStatus.FAILED))
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G2")
        assert_nothing_was_written(repo, runner, before)

    def test_a_codex_review_request_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(
            codex=make_codex(
                stdout="The fix is complete. Please review this change before it "
                "is merged."
            )
        )
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G6", needle="human review")
        assert_nothing_was_written(repo, runner, before)

    def test_codex_uncertainty_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(codex=make_codex(uncertain=True))
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G5", needle="uncertain")
        assert_nothing_was_written(repo, runner, before)

    def test_failed_project_tests_are_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(tests=make_tests(TestStatus.FAILED))
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G9", needle="tests")
        assert_nothing_was_written(repo, runner, before)

    def test_an_unverified_analysis_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(analysis=make_analysis(SonarAnalysisState.TIMEOUT))
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G10", needle="analysis")
        assert_nothing_was_written(repo, runner, before)

    def test_an_issue_that_is_still_open_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        status = green_status(verification=make_verification(present=True))
        result = run_t20(repo, inspector, status=status, runner=runner)
        assert_refused(result, gate="G14", needle="still open")
        assert_nothing_was_written(repo, runner, before)


    def test_a_dirty_baseline_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        write(repo / "src" / "pre_existing.py", "left over from before\n")
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner)
        assert_refused(result, gate="G16", needle="baseline")
        assert_nothing_was_written(repo, runner, before)

    def test_a_change_outside_the_scope_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "fixed\n")
        write(repo / "src" / "other.py", "an unexpected change\n")
        after = inspector.capture(repo, include_ignored=True)
        result = GitCommitExecutor(
            timeout_seconds=60.0, runner=runner
        ).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert_refused(result, gate="G20", needle="scope")
        assert_nothing_was_written(repo, runner, before)

    def test_a_deleted_target_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        baseline = inspector.capture(repo, include_ignored=True)
        (repo / TARGET).unlink()
        after = inspector.capture(repo, include_ignored=True)
        result = GitCommitExecutor(
            timeout_seconds=60.0, runner=runner
        ).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert_refused(result, gate="G21", needle="delet")
        assert_nothing_was_written(repo, runner, before)

    def test_a_renamed_target_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        (repo / TARGET).rename(repo / "src" / "renamed.py")
        result = run_t20(repo, inspector, runner=runner, edit=False)
        assert_refused(result)
        assert_nothing_was_written(repo, runner, before)

    def test_a_run_that_changes_nothing_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner, edit=False)
        assert_refused(result, gate="G19", needle="empty")
        assert_nothing_was_written(repo, runner, before)

    def test_missing_baseline_evidence_is_refused_at_the_input_gate(
        self, repo, inspector
    ):
        runner = RecordingRunner()
        before = head_of(repo)
        result = GitCommitExecutor(timeout_seconds=60.0, runner=runner).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
        )
        assert_refused(result, gate="G1", needle="attribution")
        assert_nothing_was_written(repo, runner, before)
        stages = [record.stage for record in result.stage_records]
        assert "S9-stage-exact-files" not in stages
        assert "S13-commit" not in stages



# ---------------------------------------------------------------------------
# 3. Repository, environment, branch, identity and state refusals
# ---------------------------------------------------------------------------


@pytest.fixture
def main_repo(make_remote_repo, rm, clone_destination) -> Path:
    """A clone that is still on the protected default branch."""
    remote = make_remote_repo()
    work = rm.clone(str(remote), clone_destination())
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    return work


@pytest.fixture
def feature_repo(main_repo) -> Path:
    """A clone on a branch outside the agent namespace."""
    git_run(main_repo, "checkout", "-q", "-b", "feature/something")
    return main_repo


@pytest.fixture
def detached_repo(main_repo) -> Path:
    """A clone with a detached HEAD."""
    git_run(main_repo, "checkout", "-q", "--detach")
    return main_repo


def global_identity_is_configured() -> bool:
    """True when a *global*/*system* Git identity exists on this machine."""
    for scope in ("--global", "--system"):
        for key in ("user.name", "user.email"):
            probe = subprocess.run(
                ["git", "config", scope, key],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                return True
    return False


@pytest.fixture
def no_identity_repo(repo) -> Path:
    """A clone whose local Git identity was removed."""
    git_run(repo, "config", "--local", "--unset", "user.name")
    git_run(repo, "config", "--local", "--unset", "user.email")
    return repo


def synthetic_evidence(path: Path):
    """``(snapshot, attribution)`` for a directory the inspector cannot read."""
    snapshot = WorktreeSnapshot(
        repository_path=path,
        head_commit="a" * 40,
        is_clean=True,
        changed_files=(TARGET,),
        tracked_modified_files=(TARGET,),
        staged_files=(),
        untracked_files=(),
        deleted_files=(),
        renamed_files=(),
        fingerprints={},
        index_equivalent_blobs={TARGET: "c" * 40},
        ignored_files=(),
        ignored_scan=True,
    )
    attribution = BaselineAttribution(
        repository_path=path,
        clean_baseline=True,
        agent_files=(TARGET,),
        agent_untracked_files=(),
        agent_deleted_files=(),
        agent_renamed_files=(),
        pre_existing_files=(),
        pre_existing_and_changed_files=(),
    )
    return snapshot, attribution


class TestRepositoryAndEnvironmentRefusals:
    def test_an_unsafe_git_environment_is_refused_before_any_git_process(
        self, repo, inspector
    ):
        runner = RecordingRunner()
        before = head_of(repo)
        result = run_t20(
            repo,
            inspector,
            runner=runner,
            environment={"GIT_DIR": str(repo / ".git")},
        )
        assert_refused(result, gate="G30", needle="GIT_DIR")
        # S2 runs before S1, so not a single Git process was launched.
        assert runner.commands == []
        assert_nothing_was_written(repo, runner, before)

    def test_the_caller_environment_is_never_mutated(self, repo, inspector):
        runner = RecordingRunner()
        caller = {
            "PATH": os.environ.get("PATH", ""),
            "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"),
            "GIT_ASKPASS": "C:/tools/askpass.exe",
            "MY_MARKER": "untouched",
        }
        snapshot = dict(caller)
        result = run_t20(repo, inspector, runner=runner, environment=caller)
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert caller == snapshot
        assert runner.environments
        for child in runner.environments:
            assert child["GIT_TERMINAL_PROMPT"] == "0"
            assert child["GIT_OPTIONAL_LOCKS"] == "0"
            assert child.get("GIT_ASKPASS") == "C:/tools/askpass.exe"
            assert child["MY_MARKER"] == "untouched"
            assert "GIT_DIR" not in child
            assert "GIT_WORK_TREE" not in child


    def test_a_protected_branch_is_refused(self, main_repo, inspector):
        runner = RecordingRunner()
        before = head_of(main_repo)
        assert git_run(main_repo, "symbolic-ref", "--short", "HEAD") == "main"
        result = run_t20(main_repo, inspector, runner=runner)
        assert_refused(result, gate="G32", needle="protected")
        assert_nothing_was_written(main_repo, runner, before)
        assert git_run(main_repo, "symbolic-ref", "--short", "HEAD") == "main"

    def test_a_branch_outside_the_agent_namespace_is_refused(
        self, feature_repo, inspector
    ):
        runner = RecordingRunner()
        before = head_of(feature_repo)
        result = run_t20(feature_repo, inspector, runner=runner)
        assert_refused(result, gate="G32", needle="namespace")
        assert_nothing_was_written(feature_repo, runner, before)

    def test_a_detached_head_is_refused(self, detached_repo, inspector):
        runner = RecordingRunner()
        before = head_of(detached_repo)
        result = run_t20(detached_repo, inspector, runner=runner)
        assert_refused(result)
        failing = {item.gate_id for item in result.gates.failed_gates}
        assert {"G31", "G29"} & failing, sorted(failing)
        assert_nothing_was_written(detached_repo, runner, before)

    def test_a_missing_git_identity_is_refused(self, no_identity_repo, inspector):
        if global_identity_is_configured():
            pytest.skip(
                "this machine has a global/system Git identity, so the "
                "missing-identity case cannot be reproduced"
            )
        runner = RecordingRunner()
        before = head_of(no_identity_repo)
        empty_home = no_identity_repo.parent / "empty-home"
        empty_home.mkdir(exist_ok=True)
        result = run_t20(
            no_identity_repo,
            inspector,
            runner=runner,
            environment={
                "PATH": os.environ.get("PATH", ""),
                "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"),
                "HOME": str(empty_home),
                "USERPROFILE": str(empty_home),
                "XDG_CONFIG_HOME": str(empty_home),
            },
        )
        assert_refused(result, gate="G33", needle="identity")
        assert_nothing_was_written(no_identity_repo, runner, before)

    def test_a_held_index_lock_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        lock = repo / ".git" / "index.lock"
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "fixed\n")
        after = inspector.capture(repo, include_ignored=True)
        lock.write_text("", encoding="utf-8")
        before = head_of(repo)
        try:
            result = GitCommitExecutor(
                timeout_seconds=60.0, runner=runner
            ).commit_safely(
                repository_path=repo,
                issue_status=green_status(),
                baseline=baseline,
                after=after,
                attribution=attribute_changes(baseline, after),
            )
        finally:
            assert lock.exists(), "T20 must never remove another process's lock"
            lock.unlink()
        assert_refused(result, gate="G34", needle="index lock")
        assert_nothing_was_written(repo, runner, before)

    def test_a_merge_in_progress_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "fixed\n")
        after = inspector.capture(repo, include_ignored=True)
        marker = repo / ".git" / "MERGE_HEAD"
        marker.write_text("0" * 40 + "\n", encoding="utf-8")
        before = head_of(repo)
        try:
            result = GitCommitExecutor(
                timeout_seconds=60.0, runner=runner
            ).commit_safely(
                repository_path=repo,
                issue_status=green_status(),
                baseline=baseline,
                after=after,
                attribution=attribute_changes(baseline, after),
            )
        finally:
            assert marker.exists(), "T20 must never clear another operation's state"
            marker.unlink()
        assert_refused(result, gate="G34", needle="MERGE_HEAD")
        assert_nothing_was_written(repo, runner, before)

    def test_a_repository_path_outside_git_is_refused(self, tmp_path):
        runner = RecordingRunner()
        outside = tmp_path / "not-a-repository"
        outside.mkdir()
        # Synthetic evidence: a directory that is not a Git repository cannot be
        # snapshotted by the real inspector, so the snapshots are constructed.
        snapshot, attribution = synthetic_evidence(outside)
        result = GitCommitExecutor(timeout_seconds=60.0, runner=runner).commit_safely(
            repository_path=outside,
            issue_status=green_status(),
            baseline=snapshot,
            after=snapshot,
            attribution=attribution,
        )
        assert_refused(result, gate="G29", needle="worktree")
        assert "add" not in runner.subcommands
        assert "commit" not in runner.subcommands

    def test_a_missing_repository_path_raises_a_caller_error(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        snapshot, attribution = synthetic_evidence(missing)
        with pytest.raises(GitCommitError):
            GitCommitExecutor(timeout_seconds=60.0).commit_safely(
                repository_path=missing,
                issue_status=green_status(),
                baseline=snapshot,
                after=snapshot,
                attribution=attribution,
            )



# ---------------------------------------------------------------------------
# 4. Faults injected around the two Git writes
# ---------------------------------------------------------------------------


class TestStagingAndCommitFaults:
    def test_a_failed_stage_is_refused_and_never_committed(self, repo, inspector):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "add":
                return fake_completed(1, "", "fatal: unable to write new index file")
            return None

        runner = RecordingRunner(on_call)
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner)
        assert_refused(result, needle="staging")
        assert "commit" not in runner.subcommands, runner.subcommands
        assert head_of(repo) == before
        assert git_run(repo, "diff", "--cached", "--name-only") == ""

    def test_a_failed_commit_is_reported_and_nothing_is_recovered(
        self, repo, inspector
    ):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "commit":
                return fake_completed(1, "", "fatal: a hook declined the commit")
            return None

        runner = RecordingRunner(on_call)
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner)
        assert result.status is CommitStatus.COMMIT_FAILED, result.reason
        assert result.new_head is None
        assert result.gates.status_of("G43") is GateStatus.FAIL
        # Exactly one attempt, and no automatic recovery of any kind.
        assert len(runner.subcommands_matching("commit")) == 1
        for forbidden in ("reset", "restore", "stash", "checkout", "clean", "rm"):
            assert forbidden not in runner.subcommands, forbidden
        assert head_of(repo) == before
        # The staged state is left exactly as it was for a human to inspect.
        assert git_run(repo, "diff", "--cached", "--name-only") == TARGET

    def test_a_worktree_change_after_staging_is_refused_before_the_commit(
        self, repo, inspector
    ):
        state = {"hash_calls": 0}

        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "hash-object":
                state["hash_calls"] += 1
                if state["hash_calls"] == 2:
                    write(repo / TARGET, "changed after staging\n")
            return None

        runner = RecordingRunner(on_call)
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner)
        assert state["hash_calls"] >= 2
        assert_refused(result, gate="G41")
        assert "commit" not in runner.subcommands, runner.subcommands
        assert head_of(repo) == before

    def test_a_message_that_does_not_match_the_issue_is_refused(
        self, repo, inspector
    ):
        runner = RecordingRunner()
        message = build_commit_message(
            rule="python:S1481", file_path=TARGET, issue_key="AX-OTHER"
        )
        before = head_of(repo)
        result = run_t20(repo, inspector, runner=runner, message=message)
        assert_refused(result, gate="G42", needle="names issue")
        assert "commit" not in runner.subcommands, runner.subcommands
        # The final pre-commit check happens after staging; the staged change is
        # left exactly as it is (T20 never restores or unstages).
        assert git_run(repo, "diff", "--cached", "--name-only") == TARGET
        assert head_of(repo) == before

    def test_a_commit_that_cannot_be_proven_becomes_unverified(
        self, repo, inspector
    ):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "log" and any(
                "--format=%B" in str(token) for token in argv
            ):
                return fake_completed(0, "a tampered message body\n")
            return None

        runner = RecordingRunner(on_call)
        result = run_t20(repo, inspector, runner=runner)
        assert result.status is CommitStatus.COMMIT_UNVERIFIED, result.reason
        assert result.gates.status_of("G44") is GateStatus.FAIL
        # The commit exists and was not undone; the failure is loud, not silent.
        assert result.new_head == head_of(repo)
        assert result.new_head != result.previous_head
        assert len(runner.subcommands_matching("commit")) == 1
        for forbidden in ("reset", "restore", "stash", "checkout"):
            assert forbidden not in runner.subcommands, forbidden


    def test_an_ignored_file_created_by_the_run_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "fixed\n")
        write(repo / "agent-notes.log", "an ignored artefact\n")
        after = inspector.capture(repo, include_ignored=True)
        result = GitCommitExecutor(
            timeout_seconds=60.0, runner=runner
        ).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert_refused(result, gate="G18", needle="ignored")
        assert_nothing_was_written(repo, runner, before)
        # T20 never cleans or deletes: the artefact is still there.
        assert (repo / "agent-notes.log").exists()

    def test_a_binary_approved_file_is_refused(self, repo, inspector):
        runner = RecordingRunner()
        before = head_of(repo)
        baseline = inspector.capture(repo, include_ignored=True)
        (repo / TARGET).write_bytes(b"\x00\x01\x02\x03binary\x00content")
        after = inspector.capture(repo, include_ignored=True)
        result = GitCommitExecutor(
            timeout_seconds=60.0, runner=runner
        ).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert_refused(result, gate="G25", needle="binary")
        assert_nothing_was_written(repo, runner, before)

    def test_a_secret_in_the_approved_content_is_refused(self, repo, inspector):
        secret = "AKIAIOSFODNN7EXAMPLE"
        runner = RecordingRunner()
        before = head_of(repo)
        result = run_t20(
            repo,
            inspector,
            runner=runner,
            text=f'AWS_KEY = "{secret}"\n',
            secrets=(secret,),
        )
        assert_refused(result, gate="G26", needle="secret")
        assert_nothing_was_written(repo, runner, before)
        assert secret not in repr(result.as_dict())
        assert secret not in result.reason

    def test_a_second_run_cannot_commit_the_same_change_twice(
        self, repo, inspector
    ):
        first = run_t20(repo, inspector)
        assert first.status is CommitStatus.COMMITTED, first.reason
        runner = RecordingRunner()
        before = head_of(repo)
        second = run_t20(repo, inspector, runner=runner, edit=False)
        assert_refused(second, gate="G19")
        assert_nothing_was_written(repo, runner, before)
        assert git_run(repo, "rev-list", "--count", "HEAD^..HEAD") == "1"



# ---------------------------------------------------------------------------
# 5. Content identity is Git identity, never a raw byte hash
# ---------------------------------------------------------------------------


def commit_once(repo: Path, inspector: WorktreeBaselineInspector):
    """Run T20 for the current worktree state and return ``(result, after)``."""
    baseline = inspector.capture(repo, include_ignored=True)
    after = inspector.capture(repo, include_ignored=True)
    result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
        repository_path=repo,
        issue_status=green_status(),
        baseline=baseline,
        after=after,
        attribution=attribute_changes(baseline, after),
    )
    return result, after


class TestContentIdentity:
    def test_crlf_content_is_committed_exactly_as_git_normalizes_it(
        self, repo, inspector
    ):
        baseline = inspector.capture(repo, include_ignored=True)
        (repo / TARGET).write_bytes(b"first line\r\nsecond line\r\n")
        after = inspector.capture(repo, include_ignored=True)
        approved_id = after.content_id_of(TARGET)
        raw_id = git_run(repo, "hash-object", "--no-filters", TARGET)
        assert approved_id and raw_id
        assert approved_id != raw_id, "text=auto must normalize CRLF to LF"
        result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.approved_content_ids[TARGET] == approved_id
        assert result.staged_blob_ids[TARGET] == approved_id
        assert git_run(repo, "rev-parse", f"HEAD:{TARGET}") == approved_id
        assert approved_id != raw_id

    def test_a_gitattributes_override_governs_the_content_identity(
        self, repo, inspector
    ):
        attributes = repo / ".gitattributes"
        attributes.write_text("* text=auto\nsrc/app.py -text\n", encoding="utf-8")
        # Re-stage the target too: the override changes how Git sees the bytes
        # already in the index, so the tree must be committed consistently.
        git_run(repo, "add", "--", ".gitattributes", TARGET)
        git_run(repo, "commit", "-m", "chore: pin src/app.py as-is")
        baseline = inspector.capture(repo, include_ignored=True)
        (repo / TARGET).write_bytes(b"first line\r\nsecond line\r\n")
        after = inspector.capture(repo, include_ignored=True)
        approved_id = after.content_id_of(TARGET)
        raw_id = git_run(repo, "hash-object", "--no-filters", TARGET)
        assert approved_id == raw_id, "-text must disable CRLF conversion"
        result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert git_run(repo, "rev-parse", f"HEAD:{TARGET}") == approved_id

    def test_a_clean_filter_is_applied_identically_by_identity_and_staging(
        self, repo, inspector
    ):
        git_run(repo, "config", "filter.tidy.clean", "git hash-object --stdin")
        attributes = repo / ".gitattributes"
        attributes.write_text(
            "* text=auto\nsrc/app.py filter=tidy\n", encoding="utf-8"
        )
        # Re-stage the target too: the new clean filter must be applied to the
        # bytes already in the index, or Git would report the file as modified.
        git_run(repo, "add", "--", ".gitattributes", TARGET)
        git_run(repo, "commit", "-m", "chore: install the tidy filter")
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "filtered content\n")
        after = inspector.capture(repo, include_ignored=True)
        approved_id = after.content_id_of(TARGET)
        result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.staged_blob_ids[TARGET] == approved_id
        assert git_run(repo, "rev-parse", f"HEAD:{TARGET}") == approved_id



# ---------------------------------------------------------------------------
# 6. The mutation boundary is enforced in code, not by convention
# ---------------------------------------------------------------------------

FORBIDDEN_INVOCATIONS = (
    ("reset", "--hard"),
    ("checkout", "--", "."),
    ("checkout", "-b", "other"),
    ("switch", "main"),
    ("push", "origin", "main"),
    ("fetch", "--all"),
    ("pull"),
    ("clean", "-fdx"),
    ("stash", "push"),
    ("branch", "-D", "ai/sonar-fix/AX1"),
    ("tag", "v1"),
    ("update-ref", "-d", "HEAD"),
    ("update-ref", "HEAD", "0" * 40),
    ("rm", "-r", "src"),
    ("mv", "src/app.py", "src/other.py"),
    ("rebase", "-i", "HEAD~2"),
    ("merge", "other"),
    ("cherry-pick", "abc"),
    ("revert", "abc"),
    ("am", "patch"),
    ("apply", "patch"),
    ("clone", "https://example.invalid/x.git"),
    ("remote", "add", "origin", "https://example.invalid/x.git"),
    ("gc", "--prune=now"),
    ("prune",),
    ("repack",),
    ("filter-branch", "--all"),
    ("filter-repo",),
    ("replace", "-d", "abc"),
    ("notes", "add", "-m", "x"),
    ("worktree", "add", "x"),
    ("submodule", "update"),
    ("commit-tree", "abc"),
    ("daemon",),
    ("archive", "--format=tar", "HEAD"),
    ("bundle", "create", "x.bundle"),
    ("fsck",),
    ("",),
)

REFUSED_SHAPES = (
    ("add", "-A"),
    ("add", "-u", "--", TARGET),
    ("add", "--all", "--", TARGET),
    ("add", "."),
    ("add", "--", "."),
    ("add", "--", "-A"),
    ("add", "--", ":/*"),
    ("add", TARGET),
    ("add", "--"),
    ("add", "--", TARGET, "--", TARGET),
    ("commit", "a message"),
    ("commit", "-m", "one", "-m", "two"),
    ("commit", "-m", "message", "--amend"),
    ("commit", "--amend", "-m", "message"),
    ("commit", "-m", "message", "--no-verify"),
    ("commit", "-m", "message", "--allow-empty"),
    ("commit", "-m", "message", "--allow-empty-message"),
    ("commit", "-m", "message", "-a"),
    ("commit", "-m", "message", "--", TARGET),
    ("commit", "-F", "message.txt"),
    ("commit", "-C", "HEAD"),
    ("commit", "-m", "message", "-e"),
    ("-c", "core.hooksPath=C:/tmp/hooks", "commit", "-m", "message"),
    ("-c", "core.fsmonitor=C:/evil.exe", "commit", "-m", "message"),
    ("--git-dir=C:/elsewhere", "commit", "-m", "message"),
    ("--work-tree=C:/elsewhere", "commit", "-m", "message"),
    ("-C", "C:/elsewhere", "commit", "-m", "message"),
    ("--exec-path=C:/elsewhere", "rev-parse", "HEAD"),
    ("--namespace=ns", "commit", "-m", "message"),
    ("config", "user.name", "Attacker"),
    ("config", "--unset", "user.name"),
    ("config", "-e"),
)




class TestGitCommandAllowList:
    def test_the_allow_list_and_the_deny_list_are_disjoint(self):
        assert ALLOWED_GIT_SUBCOMMANDS
        assert FORBIDDEN_GIT_SUBCOMMANDS
        assert ALLOWED_GIT_SUBCOMMANDS & FORBIDDEN_GIT_SUBCOMMANDS == frozenset()

    @pytest.mark.parametrize("argv", FORBIDDEN_INVOCATIONS)
    def test_a_forbidden_subcommand_is_refused_before_a_process_launches(self, argv):
        with pytest.raises(GitCommitError):
            git_commit_module._validate_git_invocation(argv)

    @pytest.mark.parametrize("argv", REFUSED_SHAPES)
    def test_a_dangerous_argument_shape_is_refused(self, argv):
        with pytest.raises(GitCommitError):
            git_commit_module._validate_git_invocation(argv)

    def test_the_exact_shapes_t20_uses_are_allowed(self):
        validate = git_commit_module._validate_git_invocation
        assert validate(("add", "--", TARGET)) == "add"
        assert validate(("add", "--", "src/a.py", "src/b.py")) == "add"
        assert (
            validate(
                (
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "user.useConfigOnly=true",
                    "commit",
                    "-m",
                    "fix(sonar): resolve python:S1481 in src/app.py",
                )
            )
            == "commit"
        )
        assert validate(("ls-files", "-u", "-z")) == "ls-files"
        assert validate(("ls-files", "-s", "-z", "--", TARGET)) == "ls-files"
        assert (
            validate(("hash-object", f"--path={TARGET}", "--", TARGET))
            == "hash-object"
        )
        assert validate(("config", "--get", "user.name")) == "config"
        assert validate(("var", "GIT_AUTHOR_IDENT")) == "var"
        assert validate(("rev-parse", "HEAD")) == "rev-parse"
        assert validate(("rev-list", "--count", "a..HEAD")) == "rev-list"
        assert validate(("diff", "--cached", "--no-ext-diff", "--", TARGET)) == "diff"
        assert validate(("log", "-1", "--format=%B")) == "log"
        assert validate(("symbolic-ref", "--short", "-q", "HEAD")) == "symbolic-ref"
        assert validate(("ls-tree", "-r", "-z", "HEAD", "--", TARGET)) == "ls-tree"
        assert validate(("diff-tree", "--no-commit-id", "-r", "-z", "HEAD")) == "diff-tree"
        assert validate(("status", "--porcelain=v1", "-z", "-uall")) == "status"
        assert validate(("check-ignore", TARGET)) == "check-ignore"
        assert validate(("show-ref",)) == "show-ref"
        assert validate(("cat-file", "-p", "HEAD")) == "cat-file"

    def test_the_subcommand_splitter_finds_the_subcommand_after_globals(self):
        assert subcommand_of(["git", "-C", "x", "commit", "-m", "m"]) == "commit"
        assert (
            subcommand_of(
                ["git", "-C", "x", "-c", "user.name=T", "commit", "-m", "m"]
            )
            == "commit"
        )
        assert git_commit_module._split_git_invocation(["-c", "k=v", "status"]) == (
            "status",
            (),
        )
        assert git_commit_module._split_git_invocation([]) == ("", ())

    def test_a_real_run_only_ever_uses_allow_listed_commands(self, repo, inspector):
        runner = RecordingRunner()
        result = run_t20(repo, inspector, runner=runner)
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert runner.commands
        for argv in runner.commands:
            subcommand = subcommand_of(argv)
            assert subcommand in ALLOWED_GIT_SUBCOMMANDS, argv
            assert subcommand not in FORBIDDEN_GIT_SUBCOMMANDS, argv
            # Every command T20 really ran would pass its own validator.
            assert git_commit_module._validate_git_invocation(argv[3:]) == subcommand
        adds = runner.subcommands_matching("add")
        commits = runner.subcommands_matching("commit")
        assert len(adds) == 1, adds
        assert len(commits) == 1, commits
        assert adds[0][-1] == TARGET and "--" in adds[0]
        assert commits[0].count("-m") == 1


class TestModuleLevelApiAndTrail:
    def test_the_wrapper_commits_through_the_production_defaults(
        self, repo, inspector
    ):
        baseline = inspector.capture(repo, include_ignored=True)
        write(repo / TARGET, "fixed\n")
        after = inspector.capture(repo, include_ignored=True)
        result = git_commit_module.commit_safely(
            repository_path=repo,
            issue_status=green_status(),
            baseline=baseline,
            after=after,
            attribution=attribute_changes(baseline, after),
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.commit_message is not None
        assert git_run(repo, "log", "-1", "--format=%s") == (
            result.commit_message.subject
        )

    def test_the_wrapper_requires_the_t19_result(self, repo):
        with pytest.raises(GitCommitError):
            git_commit_module.commit_safely(
                repository_path=repo, issue_status=None
            )

    def test_a_refusal_is_a_result_and_a_missing_path_is_an_error(
        self, repo, inspector, tmp_path
    ):
        refused = run_t20(repo, inspector, edit=False)
        assert refused.status is CommitStatus.REFUSED
        assert refused.is_refusal is True
        assert refused.needs_attention is False
        assert refused.as_dict()["status"] != "committed"
        # Without the baseline evidence the wrapper refuses at S0, before it
        # even looks at the path; with evidence an unusable path is an error.
        unproven = git_commit_module.commit_safely(
            repository_path=tmp_path / "no-such-repository",
            issue_status=green_status(),
        )
        assert unproven.status is CommitStatus.REFUSED
        with pytest.raises(GitCommitError):
            git_commit_module.commit_safely(
                repository_path=tmp_path / "no-such-repository",
                issue_status=None,
            )

    def test_the_result_exposes_the_whole_state_machine_trail(self, repo, inspector):
        result = run_t20(repo, inspector)
        stages = [record.stage for record in result.stage_records]
        assert stages[0] == "S0-validate-inputs"
        assert stages[-1] == "S14-verify-commit"
        assert all(record.detail for record in result.stage_records)
        payload = result.as_dict()
        assert payload["stage_records"]
        assert payload["gates"]["failed"] == []
        assert payload["gates"]["decision"] == "proceed"
        assert payload["approved_paths"] == [TARGET]
        assert payload["reason"]
        assert payload["new_head"] == payload["commit_sha"]

