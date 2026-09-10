"""Project-test outcome classification (T15).

:func:`analyze_test_result` takes the typed :class:`test_runner.TestResult`
produced by T14 and classifies what it means for later orchestration: did the
tests pass, fail, time out, fail to launch, or fail to execute?

Scope boundary
--------------
T15 answers *"what happened when the project tests ran?"*. It does **not**
decide whether the SonarQube issue is fixed - that needs a new analysis
(T16-T19) - and it does **not** act on the outcome. In particular T15 never
retries the tests, never re-invokes Codex, never changes a prompt, never
commits, and never pushes. Retry/iteration loops are T25 and are deliberately
not implemented. The typed
:class:`TestOutcome` is handed to later orchestration, which is not part of
this POC.

The classification is a pure, deterministic mapping over a :class:`TestResult`
(which was already produced by T14). stdout/stderr are preserved on the
outcome (as produced by T14, i.e. already credential-redacted) so later
reporting or AI feedback has the failure detail without re-running anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from test_runner import TestResult, TestStatus


@dataclass(frozen=True)
class TestOutcome:
    """Interpretation of one :class:`TestResult` for later orchestration.

    Attributes:
        result: the underlying typed execution record.
        passed: the tests ran and exited 0.
        failed: the tests ran and exited non-zero.
        timed_out: the tests did not finish inside the timeout.
        executable_not_found: the test executable could not be launched.
        execution_error: the test command failed to launch for another reason.
        exit_code: the process exit code (``None`` if it never completed).
        needs_review: the outcome is not a clean pass and requires
            orchestration/reporting (never a retry here).
        reason: short human-readable summary.
        blocking_reason: the reason the outcome must not be treated as a
            successful verification, or ``None`` when tests passed.
    """

    # Pytest's default ``python_classes = Test*`` would otherwise attempt to
    # collect this public, non-test class whenever it is imported into a test
    # module. ``__test__ = False`` is the documented opt-out.
    __test__ = False

    result: TestResult
    passed: bool
    failed: bool
    timed_out: bool
    executable_not_found: bool
    execution_error: bool
    exit_code: Optional[int]
    needs_review: bool
    reason: str
    blocking_reason: Optional[str]

    @property
    def stdout(self) -> str:
        """Captured standard output of the underlying run (redacted)."""
        return self.result.stdout

    @property
    def stderr(self) -> str:
        """Captured standard error of the underlying run (redacted)."""
        return self.result.stderr

    @property
    def status(self) -> TestStatus:
        """The coarse status carried by the underlying result."""
        return self.result.status

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging (no raw output)."""
        return {
            "passed": self.passed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "executable_not_found": self.executable_not_found,
            "execution_error": self.execution_error,
            "exit_code": self.exit_code,
            "needs_review": self.needs_review,
            "reason": self.reason,
            "blocking_reason": self.blocking_reason,
        }


def _reason_text(
    *,
    passed: bool,
    failed: bool,
    timed_out: bool,
    executable_not_found: bool,
    execution_error: bool,
    exit_code: Optional[int],
) -> str:
    if passed:
        return "Project tests passed."
    if failed:
        return f"Project tests failed with exit code {exit_code}."
    if timed_out:
        return "Project tests timed out before finishing."
    if executable_not_found:
        return "The configured test command could not be launched (not found)."
    if execution_error:
        return "The configured test command failed to execute."
    return "The project-test outcome could not be classified."


def _blocking_reason(
    *,
    passed: bool,
    failed: bool,
    timed_out: bool,
    executable_not_found: bool,
    execution_error: bool,
    exit_code: Optional[int],
) -> Optional[str]:
    """Return the reason a non-passing outcome blocks verification, else None."""
    if passed:
        return None
    if failed:
        return (
            f"Project tests failed with exit code {exit_code}; the fix cannot "
            "be treated as verified."
        )
    if timed_out:
        return (
            "Project tests timed out; the fix cannot be treated as verified "
            "and the tests are not retried here."
        )
    if executable_not_found:
        return (
            "The configured test command could not be launched (executable "
            "not found), so the fix cannot be treated as verified."
        )
    if execution_error:
        return (
            "The configured test command failed to execute, so the fix cannot "
            "be treated as verified."
        )
    return "The project-test outcome is unknown, so the fix cannot be verified."


def analyze_test_result(result: TestResult) -> TestOutcome:
    """Classify one typed :class:`TestResult` (pure, deterministic).

    No retry and no Codex invocation: the caller receives a structured
    :class:`TestOutcome` describing the single run it supplied.
    """
    status = result.status
    passed = status is TestStatus.PASSED
    failed = status is TestStatus.FAILED
    timed_out = status is TestStatus.TIMEOUT
    executable_not_found = status is TestStatus.NOT_FOUND
    execution_error = status is TestStatus.EXECUTION_ERROR

    return TestOutcome(
        result=result,
        passed=passed,
        failed=failed,
        timed_out=timed_out,
        executable_not_found=executable_not_found,
        execution_error=execution_error,
        exit_code=result.exit_code,
        needs_review=not passed,
        reason=_reason_text(
            passed=passed,
            failed=failed,
            timed_out=timed_out,
            executable_not_found=executable_not_found,
            execution_error=execution_error,
            exit_code=result.exit_code,
        ),
        blocking_reason=_blocking_reason(
            passed=passed,
            failed=failed,
            timed_out=timed_out,
            executable_not_found=executable_not_found,
            execution_error=execution_error,
            exit_code=result.exit_code,
        ),
    )


__all__ = ("TestOutcome", "analyze_test_result")
