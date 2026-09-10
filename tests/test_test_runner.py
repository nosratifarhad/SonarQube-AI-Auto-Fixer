"""T14 tests: safe project-test execution.

Most tests inject a fake process runner (no real toolchain needed); a few use
the default runner with ``sys.executable`` to prove real subprocess handling.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_runner
from test_runner import (
    ProjectTestRunner,
    TestResult,
    TestRunnerError,
    TestStatus,
)


class FakeRunner:
    """Records calls and returns/raises a canned outcome."""

    def __init__(self, returncode=0, stdout="", stderr="", error=None):
        self.outcome = SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
        self.error = error
        self.calls = []

    def __call__(self, args, cwd, timeout):
        self.calls.append((list(args), str(cwd), float(timeout)))
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_passing_tests_are_status_passed(repo):
    runner = FakeRunner(returncode=0, stdout="3 passed", stderr="warn")
    result = ProjectTestRunner(runner=runner).run(repo, ["pytest", "-q"])
    assert isinstance(result, TestResult)
    assert result.status is TestStatus.PASSED
    assert result.exit_code == 0
    assert result.stdout == "3 passed"
    assert result.stderr == "warn"
    assert result.command == ("pytest", "-q")
    assert result.error is None
    assert result.passed is True
    assert result.failed is False


def test_failing_tests_are_status_failed(repo):
    runner = FakeRunner(returncode=7, stdout="", stderr="boom")
    result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
    assert result.status is TestStatus.FAILED
    assert result.exit_code == 7
    assert result.failed is True
    assert "7" in result.error


def test_timeout_is_typed_not_raised(repo):
    runner = FakeRunner(
        error=subprocess.TimeoutExpired(cmd=["pytest"], timeout=1.0)
    )
    result = ProjectTestRunner(timeout_seconds=1.0, runner=runner).run(
        repo, ["pytest"]
    )
    assert result.status is TestStatus.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert "timed out" in result.error


def test_missing_executable_is_status_not_found(repo):
    runner = FakeRunner(error=FileNotFoundError("no such file"))
    result = ProjectTestRunner(runner=runner).run(repo, ["nope-xyz"])
    assert result.status is TestStatus.NOT_FOUND
    assert result.executable_not_found is True
    assert result.exit_code is None


def test_other_launch_error_is_status_execution_error(repo):
    runner = FakeRunner(error=OSError("cannot execute"))
    result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
    assert result.status is TestStatus.EXECUTION_ERROR
    assert result.execution_error is True
    assert result.exit_code is None


class TestCommandValidation:
    def test_tuple_is_accepted_and_frozen_to_tuple(self):
        assert ProjectTestRunner.validate_command(("pytest", "-q")) == (
            "pytest",
            "-q",
        )

    @pytest.mark.parametrize(
        "bad", [None, "", "pytest -q", b"pytest", 5, {"cmd": "pytest"}]
    )
    def test_malformed_command_rejected(self, bad):
        with pytest.raises(TestRunnerError):
            ProjectTestRunner.validate_command(bad)

    def test_empty_command_rejected(self):
        with pytest.raises(TestRunnerError, match="not be empty"):
            ProjectTestRunner.validate_command([])

    def test_empty_element_rejected(self):
        with pytest.raises(TestRunnerError, match="empty strings"):
            ProjectTestRunner.validate_command(["pytest", ""])

    def test_non_string_element_rejected(self):
        with pytest.raises(TestRunnerError, match="must be a string"):
            ProjectTestRunner.validate_command(["pytest", 3])

    def test_option_like_executable_rejected(self):
        with pytest.raises(TestRunnerError, match="executable"):
            ProjectTestRunner.validate_command(["--version"])

    def test_invalid_command_never_launches_a_process(self, repo):
        runner = FakeRunner()
        with pytest.raises(TestRunnerError):
            ProjectTestRunner(runner=runner).run(repo, [])
        assert runner.calls == []


class TestInvocationContract:
    def test_command_cwd_and_timeout_reach_the_runner(self, repo):
        runner = FakeRunner()
        ProjectTestRunner(timeout_seconds=42.0, runner=runner).run(
            repo, ["pytest", "-q"]
        )
        args, cwd, timeout = runner.calls[0]
        assert args == ["pytest", "-q"]
        assert all(isinstance(arg, str) for arg in args)
        assert cwd == str(repo.resolve())
        assert timeout == 42.0

    def test_default_timeout_is_used_when_not_configured(self, repo):
        runner = FakeRunner()
        ProjectTestRunner(runner=runner).run(repo, ["pytest"])
        assert runner.calls[0][2] == 600.0

    def test_timeout_seconds_property_reflects_configuration(self):
        assert ProjectTestRunner().timeout_seconds == 600.0
        assert ProjectTestRunner(timeout_seconds=12.5).timeout_seconds == 12.5

    def test_missing_repository_raises_before_launch(self, tmp_path):
        runner = FakeRunner()
        with pytest.raises(TestRunnerError, match="not an existing directory"):
            ProjectTestRunner(runner=runner).run(tmp_path / "ghost", ["pytest"])
        assert runner.calls == []

    def test_repository_that_is_a_file_raises(self, tmp_path):
        a_file = tmp_path / "file.txt"
        a_file.write_text("x", encoding="utf-8")
        with pytest.raises(TestRunnerError, match="not an existing directory"):
            ProjectTestRunner(runner=FakeRunner()).run(a_file, ["pytest"])

    def test_default_runner_never_uses_a_shell(self, repo, monkeypatch):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(test_runner.subprocess, "run", fake_run)
        ProjectTestRunner().run(repo, ["pytest", "-q"])
        assert captured["args"] == ["pytest", "-q"]
        assert captured["kwargs"].get("shell", False) is False
        assert captured["kwargs"]["cwd"] == str(repo.resolve())
        assert captured["kwargs"]["capture_output"] is True


class TestRedactionAndSummary:
    def test_captured_output_is_redacted(self, repo):
        runner = FakeRunner(
            returncode=1,
            stdout="clone https://bob:hunter2@example.com/x failed",
            stderr="token https://alice:secret@example.com/y",
        )
        result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
        assert "hunter2" not in result.stdout
        assert "secret" not in result.stderr
        assert "***@" in result.stdout

    def test_launch_error_message_is_redacted(self, repo):
        runner = FakeRunner(
            error=OSError("failed https://bob:hunter2@example.com/git")
        )
        result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
        assert "hunter2" not in result.error
        assert result.status is TestStatus.EXECUTION_ERROR

    def test_as_dict_omits_raw_output(self, repo):
        runner = FakeRunner(returncode=3, stdout="secret-output", stderr="x")
        result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
        payload = result.as_dict()
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 3
        assert "secret-output" not in str(payload)

    def test_result_is_frozen(self, repo):
        runner = FakeRunner()
        result = ProjectTestRunner(runner=runner).run(repo, ["pytest"])
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = TestStatus.FAILED


class TestDefaultRunnerWithRealProcess:
    def test_real_success(self, repo):
        result = ProjectTestRunner(timeout_seconds=60.0).run(
            repo,
            [sys.executable, "-c", "import sys; sys.stdout.write('ok')"],
        )
        assert result.status is TestStatus.PASSED
        assert result.exit_code == 0
        assert result.stdout.strip() == "ok"

    def test_real_nonzero_exit(self, repo):
        result = ProjectTestRunner(timeout_seconds=60.0).run(
            repo, [sys.executable, "-c", "import sys; sys.exit(3)"]
        )
        assert result.status is TestStatus.FAILED
        assert result.exit_code == 3

    def test_real_timeout(self, repo):
        result = ProjectTestRunner(timeout_seconds=0.5).run(
            repo, [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        assert result.status is TestStatus.TIMEOUT
        assert result.timed_out is True

    def test_real_missing_executable(self, repo):
        result = ProjectTestRunner(timeout_seconds=60.0).run(
            repo, ["test-runner-that-does-not-exist-xyz"]
        )
        assert result.status is TestStatus.NOT_FOUND
