"""T16 tests: triggering the SonarQube analysis.

Most tests inject a fake process runner (no real scanner needed); a few use the
default runner with ``sys.executable`` to prove real subprocess handling. No
real SonarQube server is ever contacted.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonar_analysis
from sonar_analysis import (
    DEFAULT_TASK_ID_PATTERNS,
    AnalysisConfig,
    SonarAnalysisError,
    SonarAnalysisStatus,
    SonarAnalysisTrigger,
    SonarAnalysisTriggerResult,
    build_sonar_environment,
    extract_task_id,
    is_safe_task_id,
)

SCANNER_CE_LINE = (
    "INFO: More about the report processing at "
    "https://sonar.example.com/api/ce/task?id=AY1abcDEF-2xyz"
)


class FakeRunner:
    """Records calls and returns/raises a canned outcome."""

    def __init__(self, returncode=0, stdout="", stderr="", error=None):
        self.outcome = SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
        self.error = error
        self.calls = []

    def __call__(self, args, cwd, env, timeout):
        self.calls.append((list(args), str(cwd), dict(env), float(timeout)))
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_successful_trigger_is_status_triggered(repo):
    runner = FakeRunner(returncode=0, stdout=f"scanning\n{SCANNER_CE_LINE}", stderr="warn")
    result = SonarAnalysisTrigger(runner=runner).trigger(repo)
    assert isinstance(result, SonarAnalysisTriggerResult)
    assert result.status is SonarAnalysisStatus.TRIGGERED
    assert result.exit_code == 0
    assert "scanning" in result.stdout
    assert result.stderr == "warn"
    assert result.command == ("sonar-scanner",)
    assert result.error is None
    assert result.triggered is True
    assert result.task_id == "AY1abcDEF-2xyz"
    assert result.has_task_id is True


def test_nonzero_exit_is_status_failed(repo):
    runner = FakeRunner(returncode=3, stdout="boom", stderr="scanner failed")
    result = SonarAnalysisTrigger(runner=runner).trigger(repo)
    assert result.status is SonarAnalysisStatus.FAILED
    assert result.exit_code == 3
    assert result.triggered is False
    assert "3" in result.error
    assert result.stderr == "scanner failed"


def test_timeout_is_typed_not_raised(repo):
    runner = FakeRunner(
        error=subprocess.TimeoutExpired(cmd=["sonar-scanner"], timeout=1.0)
    )
    result = SonarAnalysisTrigger(runner=runner).trigger(
        repo, AnalysisConfig(timeout_seconds=1.0)
    )
    assert result.status is SonarAnalysisStatus.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.task_id is None
    assert "timed out" in result.error


def test_missing_executable_is_status_not_found(repo):
    runner = FakeRunner(error=FileNotFoundError("no such file"))
    result = SonarAnalysisTrigger(runner=runner).trigger(repo)
    assert result.status is SonarAnalysisStatus.NOT_FOUND
    assert result.executable_not_found is True
    assert result.exit_code is None


def test_other_launch_error_is_status_execution_error(repo):
    runner = FakeRunner(error=OSError("cannot execute"))
    result = SonarAnalysisTrigger(runner=runner).trigger(repo)
    assert result.status is SonarAnalysisStatus.EXECUTION_ERROR
    assert result.execution_error is True
    assert result.exit_code is None


def test_trigger_is_not_a_completion_claim(repo):
    """A TRIGGERED result must never claim the analysis or the fix is done."""
    runner = FakeRunner(returncode=0, stdout=SCANNER_CE_LINE, stderr="")
    result = SonarAnalysisTrigger(runner=runner).trigger(repo)
    assert result.triggered is True
    payload = result.as_dict()
    assert payload["triggered"] is True
    assert "completed" not in payload


class TestTaskIdExtraction:
    def test_ce_task_url_is_extracted(self):
        assert extract_task_id(f"x\n{SCANNER_CE_LINE}\n", "") == "AY1abcDEF-2xyz"

    def test_stderr_is_searched_too(self):
        assert extract_task_id("", SCANNER_CE_LINE) == "AY1abcDEF-2xyz"

    def test_last_match_of_the_first_matching_pattern_wins(self):
        text = (
            "api/ce/task?id=FIRST111\n"
            "api/ce/task?id=SECOND222\n"
        )
        assert extract_task_id(text, "") == "SECOND222"

    def test_task_id_label_fallback_pattern(self):
        assert extract_task_id("Task id: AY9zyx", "") == "AY9zyx"

    def test_no_match_returns_none(self):
        assert extract_task_id("nothing useful here", "") is None

    def test_unsafe_characters_are_discarded(self):
        assert extract_task_id("api/ce/task?id=%2Fetc%2Fpasswd", "") is None

    def test_overlong_candidate_is_discarded(self):
        assert extract_task_id("Task id: " + "a" * 400, "") is None

    def test_custom_patterns_are_used(self):
        assert (
            extract_task_id("ce=MYTASK-1", "", (r"ce=([A-Za-z0-9\-]+)",))
            == "MYTASK-1"
        )

    def test_invalid_pattern_is_ignored(self):
        assert extract_task_id("Task id: AY1", "", ("([unclosed",)) is None

    def test_default_patterns_exported(self):
        assert DEFAULT_TASK_ID_PATTERNS == sonar_analysis.DEFAULT_TASK_ID_PATTERNS

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("AY1abc-DEF_2", True),
            ("", False),
            ("a" * 201, False),
            ("has space", False),
            ("semi;colon", False),
            (None, False),
            (123, False),
        ],
    )
    def test_is_safe_task_id(self, value, expected):
        assert is_safe_task_id(value) is expected


class TestCommandAndRepositoryHandling:
    def test_command_reaches_runner_as_argument_array(self, repo):
        runner = FakeRunner()
        SonarAnalysisTrigger(runner=runner).trigger(
            repo, AnalysisConfig(command=("mvn", "-B", "sonar:sonar"))
        )
        args, cwd, _env, timeout = runner.calls[0]
        assert args == ["mvn", "-B", "sonar:sonar"]
        assert cwd == str(repo.resolve())
        assert timeout == 900.0

    def test_instance_config_is_used_when_call_config_is_omitted(self, repo):
        runner = FakeRunner()
        trigger = SonarAnalysisTrigger(
            AnalysisConfig(command=("my-scanner", "--ci"), timeout_seconds=12.0),
            runner=runner,
        )
        result = trigger.trigger(repo)
        assert result.command == ("my-scanner", "--ci")
        assert runner.calls[0][3] == 12.0
        assert trigger.analysis_config.command == ("my-scanner", "--ci")

    def test_call_config_overrides_instance_config(self, repo):
        runner = FakeRunner()
        trigger = SonarAnalysisTrigger(
            AnalysisConfig(command=("default-scanner",)), runner=runner
        )
        result = trigger.trigger(
            repo, AnalysisConfig(command=("override-scanner",))
        )
        assert result.command == ("override-scanner",)

    def test_missing_repository_directory_raises_before_launch(self, tmp_path):
        runner = FakeRunner()
        with pytest.raises(SonarAnalysisError, match="not an existing directory"):
            SonarAnalysisTrigger(runner=runner).trigger(tmp_path / "missing")
        assert runner.calls == []

    def test_repository_path_may_be_a_relative_string(self, repo, monkeypatch):
        runner = FakeRunner()
        monkeypatch.chdir(repo)
        SonarAnalysisTrigger(runner=runner).trigger(".")
        assert runner.calls[0][1] == str(repo.resolve())

    @pytest.mark.parametrize(
        "bad",
        [None, "", "sonar-scanner -Dx", b"sonar-scanner", 5, (), ("",), ("--flag",), ("ok", 3)],
    )
    def test_invalid_command_is_rejected(self, bad):
        with pytest.raises(SonarAnalysisError):
            SonarAnalysisTrigger.validate_command(bad)

    def test_tuple_command_is_returned_as_tuple(self):
        assert SonarAnalysisTrigger.validate_command(
            ["sonar-scanner", "-Dsonar.branch=x"]
        ) == ("sonar-scanner", "-Dsonar.branch=x")

    def test_non_positive_timeout_is_rejected(self, repo):
        runner = FakeRunner()
        with pytest.raises(SonarAnalysisError, match="timeout must be positive"):
            SonarAnalysisTrigger(runner=runner).trigger(
                repo, AnalysisConfig(timeout_seconds=0)
            )
        assert runner.calls == []



class TestEnvironmentAndCredentials:
    def test_build_sonar_environment_includes_url_and_token(self):
        env = build_sonar_environment("https://sonar.example.com", "tok-123")
        assert env == {
            "SONAR_HOST_URL": "https://sonar.example.com",
            "SONAR_TOKEN": "tok-123",
        }

    def test_build_sonar_environment_omits_missing_token(self):
        env = build_sonar_environment("https://sonar.example.com")
        assert "SONAR_TOKEN" not in env

    def test_build_sonar_environment_merges_extra(self):
        env = build_sonar_environment(
            "https://sonar.example.com", extra={"SONAR_SCANNER_OPTS": "-Xmx1g"}
        )
        assert env["SONAR_SCANNER_OPTS"] == "-Xmx1g"

    def test_credentials_are_passed_via_environment_not_arguments(self, repo):
        runner = FakeRunner(returncode=0, stdout=SCANNER_CE_LINE)
        config = AnalysisConfig(
            command=("sonar-scanner",),
            environment=build_sonar_environment("https://sonar.example.com", "tok-123"),
            secrets=("tok-123",),
        )
        result = SonarAnalysisTrigger(runner=runner).trigger(repo, config)
        args, _cwd, env, _timeout = runner.calls[0]
        assert not any("tok-123" in arg for arg in args)
        assert env["SONAR_TOKEN"] == "tok-123"
        assert env["SONAR_HOST_URL"] == "https://sonar.example.com"
        assert "tok-123" not in str(result.as_dict())

    def test_secrets_are_scrubbed_from_captured_output(self, repo):
        runner = FakeRunner(
            returncode=0,
            stdout="using token tok-123 to talk to the server",
            stderr="login https://bob:hunter2@example.com failed",
        )
        config = AnalysisConfig(secrets=("tok-123",))
        result = SonarAnalysisTrigger(runner=runner).trigger(repo, config)
        assert "tok-123" not in result.stdout
        assert "***" in result.stdout
        assert "hunter2" not in result.stderr
        assert "***@example.com" in result.stderr

    def test_launch_error_message_is_redacted(self, repo):
        runner = FakeRunner(
            error=FileNotFoundError("failed https://bob:hunter2@example.com/x")
        )
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        assert result.status is SonarAnalysisStatus.NOT_FOUND
        assert "hunter2" not in result.error

    def test_execution_error_message_is_redacted(self, repo):
        runner = FakeRunner(
            error=OSError("spawn failed for https://bob:hunter2@example.com/x")
        )
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        assert result.status is SonarAnalysisStatus.EXECUTION_ERROR
        assert "hunter2" not in result.error

    def test_environment_is_not_present_in_the_result_summary(self, repo):
        runner = FakeRunner()
        config = AnalysisConfig(environment={"SONAR_TOKEN": "tok-123"})
        result = SonarAnalysisTrigger(runner=runner).trigger(repo, config)
        assert "tok-123" not in str(result.as_dict())
        assert "SONAR_TOKEN" not in str(result.as_dict())


class TestResultModel:
    def test_as_dict_omits_raw_output_and_command(self, repo):
        runner = FakeRunner(returncode=2, stdout="secret-output", stderr="raw-err")
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        payload = result.as_dict()
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 2
        assert "secret-output" not in str(payload)
        assert "raw-err" not in str(payload)
        assert "command" not in payload

    def test_result_is_frozen(self, repo):
        result = SonarAnalysisTrigger(runner=FakeRunner()).trigger(repo)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = SonarAnalysisStatus.FAILED

    def test_analysis_config_is_frozen(self):
        config = AnalysisConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.command = ("x",)



class TestDefaultRunnerWithRealProcess:
    def test_default_runner_never_uses_a_shell(self, repo, monkeypatch):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=SCANNER_CE_LINE, stderr="")

        monkeypatch.setattr(sonar_analysis.subprocess, "run", fake_run)
        result = SonarAnalysisTrigger().trigger(repo)
        assert captured["args"] == ["sonar-scanner"]
        assert captured["kwargs"].get("shell", False) is False
        assert captured["kwargs"]["cwd"] == str(repo.resolve())
        assert captured["kwargs"]["capture_output"] is True
        assert isinstance(captured["kwargs"]["env"], dict)
        assert result.task_id == "AY1abcDEF-2xyz"

    def test_real_subprocess_success_extracts_task_id(self, repo):
        script = f"import sys; sys.stdout.write({SCANNER_CE_LINE!r})"
        result = SonarAnalysisTrigger(
            AnalysisConfig(
                command=(sys.executable, "-c", script), timeout_seconds=60.0
            )
        ).trigger(repo)
        assert result.status is SonarAnalysisStatus.TRIGGERED
        assert result.exit_code == 0
        assert result.task_id == "AY1abcDEF-2xyz"

    def test_real_nonzero_exit(self, repo):
        result = SonarAnalysisTrigger(
            AnalysisConfig(
                command=(sys.executable, "-c", "import sys; sys.exit(5)"),
                timeout_seconds=60.0,
            )
        ).trigger(repo)
        assert result.status is SonarAnalysisStatus.FAILED
        assert result.exit_code == 5

    def test_real_timeout(self, repo):
        result = SonarAnalysisTrigger(
            AnalysisConfig(
                command=(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_seconds=0.5,
            )
        ).trigger(repo)
        assert result.status is SonarAnalysisStatus.TIMEOUT
        assert result.timed_out is True

    def test_real_missing_executable(self, repo):
        result = SonarAnalysisTrigger(
            AnalysisConfig(command=("scanner-that-does-not-exist-xyz",))
        ).trigger(repo)
        assert result.status is SonarAnalysisStatus.NOT_FOUND



class TestCapturedOutputShapes:
    def test_none_output_is_treated_as_empty(self, repo):
        runner = FakeRunner(returncode=0, stdout=None, stderr=None)
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        assert result.status is SonarAnalysisStatus.TRIGGERED
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.task_id is None

    def test_bytes_output_is_decoded(self, repo):
        runner = FakeRunner(
            returncode=0,
            stdout=SCANNER_CE_LINE.encode("utf-8"),
            stderr=b"warn",
        )
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        assert "report processing" in result.stdout
        assert result.stderr == "warn"
        assert result.task_id == "AY1abcDEF-2xyz"

    def test_non_string_output_is_stringified(self, repo):
        runner = FakeRunner(returncode=0, stdout=12345, stderr=("x",))
        result = SonarAnalysisTrigger(runner=runner).trigger(repo)
        assert result.stdout == "12345"
        assert result.task_id is None

