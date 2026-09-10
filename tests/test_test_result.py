"""T15 tests: project-test outcome classification (no retry, no Codex).

The analyzer is a pure mapping over a :class:`TestResult`, so most tests build
results directly; one test wires T14 -> T15 to prove no retry happens.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from test_result import TestOutcome, analyze_test_result
from test_runner import ProjectTestRunner, TestResult, TestStatus


def make_result(status, exit_code=None, stdout="", stderr="", error=None):
    return TestResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command=("pytest", "-q"),
        error=error,
    )


def test_passed_outcome_is_clean():
    outcome = analyze_test_result(
        make_result(TestStatus.PASSED, exit_code=0, stdout="3 passed")
    )
    assert isinstance(outcome, TestOutcome)
    assert outcome.passed is True
    assert outcome.failed is False
    assert outcome.timed_out is False
    assert outcome.executable_not_found is False
    assert outcome.execution_error is False
    assert outcome.exit_code == 0
    assert outcome.needs_review is False
    assert outcome.blocking_reason is None
    assert outcome.reason == "Project tests passed."


def test_failed_outcome_blocks_with_exit_code():
    outcome = analyze_test_result(
        make_result(TestStatus.FAILED, exit_code=7, stderr="boom")
    )
    assert outcome.failed is True
    assert outcome.passed is False
    assert outcome.exit_code == 7
    assert outcome.needs_review is True
    assert "7" in outcome.blocking_reason
    assert "7" in outcome.reason
    assert outcome.stderr == "boom"


def test_timeout_outcome_blocks_and_does_not_retry():
    outcome = analyze_test_result(
        make_result(TestStatus.TIMEOUT, error="Project tests timed out.")
    )
    assert outcome.timed_out is True
    assert outcome.exit_code is None
    assert outcome.needs_review is True
    assert "timed out" in outcome.blocking_reason
    assert "not retried" in outcome.blocking_reason or \
        "cannot be treated as verified" in outcome.blocking_reason


def test_not_found_outcome_blocks():
    outcome = analyze_test_result(
        make_result(TestStatus.NOT_FOUND, error="executable not found")
    )
    assert outcome.executable_not_found is True
    assert outcome.needs_review is True
    assert outcome.exit_code is None
    assert "could not be launched" in outcome.blocking_reason


def test_execution_error_outcome_blocks():
    outcome = analyze_test_result(
        make_result(TestStatus.EXECUTION_ERROR, error="cannot execute")
    )
    assert outcome.execution_error is True
    assert outcome.needs_review is True
    assert "failed to execute" in outcome.blocking_reason


def test_status_and_stdout_are_forwarded_from_the_result():
    result = make_result(
        TestStatus.FAILED, exit_code=1, stdout="out", stderr="err"
    )
    outcome = analyze_test_result(result)
    assert outcome.status is TestStatus.FAILED
    assert outcome.result is result
    assert outcome.stdout == "out"
    assert outcome.stderr == "err"


def test_analysis_is_pure_and_deterministic():
    result = make_result(TestStatus.FAILED, exit_code=2)
    assert analyze_test_result(result) == analyze_test_result(result)
    # The analyzer never mutates the result it was given.
    assert result.exit_code == 2
    assert result.status is TestStatus.FAILED


def test_outcome_is_frozen():
    outcome = analyze_test_result(make_result(TestStatus.PASSED, exit_code=0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.passed = False


def test_as_dict_is_compact_and_secret_free():
    outcome = analyze_test_result(
        make_result(TestStatus.FAILED, exit_code=4, stdout="SECRET")
    )
    payload = outcome.as_dict()
    assert payload["failed"] is True
    assert payload["exit_code"] == 4
    assert payload["blocking_reason"]
    assert "SECRET" not in str(payload)


class FakeRunner:
    """Runnable fake process runner that counts how often it is invoked."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.outcome = SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
        self.calls = []

    def __call__(self, args, cwd, timeout):
        self.calls.append((list(args), str(cwd), float(timeout)))
        return self.outcome


def test_t14_to_t15_bridge_never_retries_the_tests(tmp_path):
    """T15 classifies the one run it is given; it must not run tests again."""
    runner = FakeRunner(returncode=1, stderr="1 failed")
    result = ProjectTestRunner(timeout_seconds=30.0, runner=runner).run(
        tmp_path, ["pytest", "-q"]
    )
    assert len(runner.calls) == 1

    outcome = analyze_test_result(result)

    assert outcome.failed is True
    assert outcome.exit_code == 1
    assert outcome.stderr == "1 failed"
    # No retry: the runner was not invoked a second time by the analyzer.
    assert len(runner.calls) == 1


def test_unclassifiable_status_uses_defensive_messages():
    """Defensive branch: an unrecognized status is never treated as a pass."""
    outcome = analyze_test_result(make_result(status=object()))
    assert outcome.passed is False
    assert outcome.failed is False
    assert outcome.timed_out is False
    assert outcome.executable_not_found is False
    assert outcome.execution_error is False
    assert outcome.needs_review is True
    assert outcome.blocking_reason == (
        "The project-test outcome is unknown, so the fix cannot be verified."
    )
    assert outcome.reason == "The project-test outcome could not be classified."
