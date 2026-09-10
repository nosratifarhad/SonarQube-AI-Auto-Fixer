"""T10 tests: Codex execution abstraction.

Real Codex is never invoked. The runner is injected (or, for three tests,
the real subprocess runner is driven with ``python`` so the default runner's
success / not-found / timeout mappings stay covered).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from codex_executor import (
    CodexExecutionStatus,
    CodexExecutor,
    CodexExecutorError,
    CodexResult,
)


class FakeOutcome:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    """Injected runner: records calls, then returns or raises as configured."""

    def __init__(self, outcome=None, error=None):
        self.outcome = outcome if outcome is not None else FakeOutcome()
        self.error = error
        self.calls = []

    def __call__(self, args, cwd, prompt, timeout):
        self.calls.append((list(args), str(cwd), prompt, float(timeout)))
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    destination = tmp_path / "repo"
    destination.mkdir()
    return destination


def test_successful_execution_captures_output_and_invocation(repo):
    runner = RecordingRunner(outcome=FakeOutcome(0, stdout="Fixed line 7\n", stderr=""))
    executor = CodexExecutor(timeout_seconds=30.0, runner=runner)
    result = executor.execute("Fix the issue", repo)

    assert result.status is CodexExecutionStatus.SUCCESS
    assert result.exit_code == 0
    assert result.error is None
    assert result.stdout == "Fixed line 7\n"
    assert result.stderr == ""
    assert result.command == ("codex", "exec")

    (args, cwd, prompt, timeout) = runner.calls[0]
    assert args == ["codex", "exec"]
    assert cwd == str(repo.resolve())
    assert prompt == "Fix the issue"
    assert timeout == 30.0


def test_custom_command_and_args_construction(repo):
    runner = RecordingRunner()
    executor = CodexExecutor(
        codex_command="codex-beta",
        codex_args=("exec", "--full-auto", "--json"),
        runner=runner,
    )
    executor.execute("prompt", repo)
    args = runner.calls[0][0]
    assert args == ["codex-beta", "exec", "--full-auto", "--json"]


def test_nonzero_exit_code_is_process_failure(repo):
    runner = RecordingRunner(
        outcome=FakeOutcome(returncode=3, stdout="partial", stderr="boom")
    )
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert result.status is CodexExecutionStatus.FAILED
    assert result.process_failed is True
    assert result.exit_code == 3
    assert result.stdout == "partial"
    assert result.stderr == "boom"
    assert "exited with code 3" in (result.error or "")


def test_stdout_and_stderr_are_credential_redacted(repo):
    runner = RecordingRunner(
        outcome=FakeOutcome(
            returncode=0,
            stdout="done https://alice:s3cr3t@example.com/repo.git",
            stderr="fatal https://bob:hunter2@example.com/x.git",
        )
    )
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert "s3cr3t" not in result.stdout
    assert "hunter2" not in result.stderr


def test_timeout_is_typed_timeout(repo):
    runner = RecordingRunner(
        error=subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=30.0)
    )
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert result.status is CodexExecutionStatus.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.error is not None
    assert "timed out" in result.error


def test_executable_not_found_is_typed(repo):
    runner = RecordingRunner(
        error=FileNotFoundError(2, "No such file or directory", "codex")
    )
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert result.status is CodexExecutionStatus.NOT_FOUND
    assert result.executable_not_found is True
    assert result.exit_code is None
    assert "not found" in (result.error or "")


def test_missing_repository_path_raises_before_launch(tmp_path):
    runner = RecordingRunner()
    executor = CodexExecutor(runner=runner)
    with pytest.raises(CodexExecutorError):
        executor.execute("prompt", tmp_path / "does-not-exist")
    assert runner.calls == []


def test_fake_runner_never_needs_codex_installed(repo):
    runner = RecordingRunner(outcome=FakeOutcome(returncode=0, stdout="ok"))
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert result.status is CodexExecutionStatus.SUCCESS


def test_real_default_runner_success(repo):
    script = (
        "import sys; data = sys.stdin.read(); "
        "sys.stdout.write('got %d chars' % len(data))"
    )
    executor = CodexExecutor(codex_command=sys.executable, codex_args=("-c", script))
    result = executor.execute("hello-codex", repo)
    assert result.status is CodexExecutionStatus.SUCCESS
    assert result.stdout == "got 11 chars"


def test_real_default_runner_executable_not_found(repo):
    executor = CodexExecutor(codex_command="codex-cli-definitely-missing-42")
    result = executor.execute("prompt", repo)
    assert result.status is CodexExecutionStatus.NOT_FOUND


def test_real_default_runner_timeout(repo):
    executor = CodexExecutor(
        codex_command=sys.executable,
        codex_args=("-c", "import time; time.sleep(60)"),
        timeout_seconds=1.0,
    )
    result = executor.execute("prompt", repo)
    assert result.status is CodexExecutionStatus.TIMEOUT
    assert result.exit_code is None


def test_codex_result_helpers_and_summary(repo):
    runner = RecordingRunner(outcome=FakeOutcome(returncode=0, stdout="ok"))
    result = CodexExecutor(runner=runner).execute("prompt", repo)
    assert isinstance(result, CodexResult)
    summary = result.as_dict()
    assert summary["status"] == "success"
    assert summary["exit_code"] == 0
    assert summary["timed_out"] is False
    assert summary["executable_not_found"] is False
    assert summary["error"] is None

