"""Run the repository's project tests through a safe abstraction (T14).

:class:`ProjectTestRunner` executes an *explicitly configured* test command in
the local repository and returns a typed :class:`TestResult`. It is
language-agnostic on purpose: the command comes from configuration (for
example ``["pytest", "-q"]`` or ``["dotnet", "test", "--no-restore"]``), is
never hard-coded, and is never derived from SonarQube issue text or Codex
output.

Design notes
------------
* Subprocesses are launched with argument arrays and never with ``shell=True``.
* ``repository_path`` is the working directory; the command runs only inside
  the clone.
* stdout/stderr/exit code are captured and the timeout is enforced by the
  runner.
* Statuses clearly distinguish ``PASSED``, ``FAILED`` (tests ran and failed),
  ``TIMEOUT``, ``NOT_FOUND`` (executable missing), and ``EXECUTION_ERROR``
  (any other launch problem).
* Every captured or error string passes through :func:`redact_credentials`.
* The process runner is injectable, so tests need no real project toolchain.

Non-behaviour (by design)
-------------------------
The runner only runs the supplied command. It never modifies source code or
dependencies, never installs packages, never commits or pushes, never calls
SonarQube or Codex, and never retries. Interpreting the outcome is T15.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple, Union

from repository import redact_credentials

PathLike = Union[str, Path]


class TestStatus(Enum):
    """Outcome category for a single project-test run."""

    # Pytest's default ``python_classes = Test*`` would otherwise attempt to
    # collect this public, non-test class whenever it is imported into a test
    # module. ``__test__ = False`` is the documented opt-out.
    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NOT_FOUND = "executable-not-found"
    EXECUTION_ERROR = "execution-error"


@dataclass(frozen=True)
class TestResult:
    """Typed, secret-redacted record of one project-test execution.

    Attributes:
        status: coarse outcome category (:class:`TestStatus`).
        exit_code: process exit code, or ``None`` when the process never ran
            to completion (timeout / not found / launch error).
        stdout: captured standard output (credentials redacted).
        stderr: captured standard error (credentials redacted).
        command: the exact argument array that was used.
        error: human-readable failure detail, or ``None`` on success.
    """

    # Not a pytest test class (see ``TestStatus`` above).
    __test__ = False

    status: TestStatus
    exit_code: Optional[int]
    stdout: str
    stderr: str
    command: Tuple[str, ...]
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        """True when the test command ran and exited 0."""
        return self.status is TestStatus.PASSED

    @property
    def failed(self) -> bool:
        """True when the test command ran and exited non-zero."""
        return self.status is TestStatus.FAILED

    @property
    def timed_out(self) -> bool:
        """True when the test command did not finish inside the timeout."""
        return self.status is TestStatus.TIMEOUT

    @property
    def executable_not_found(self) -> bool:
        """True when the test executable could not be launched."""
        return self.status is TestStatus.NOT_FOUND

    @property
    def execution_error(self) -> bool:
        """True when the test command failed to launch for another reason."""
        return self.status is TestStatus.EXECUTION_ERROR

    def as_dict(self) -> dict:
        """Secret-free, compact summary for logging (no raw output)."""
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "executable_not_found": self.executable_not_found,
            "execution_error": self.execution_error,
            "error": self.error,
        }


#: A runner takes ``(args, cwd, timeout_seconds)`` and returns an object with
#: ``returncode``/``stdout``/``stderr`` attributes, or raises ``OSError`` /
#: ``subprocess.TimeoutExpired``.
TestProcessRunner = Callable[[Sequence[str], str, float], object]


class TestRunnerError(Exception):
    """Raised for caller errors (bad repository path or test command)."""

    # Not a pytest test class (see ``TestStatus`` above).
    __test__ = False


def _subprocess_runner(
    args: Sequence[str], cwd: str, timeout: float
) -> subprocess.CompletedProcess:
    """Default runner: ``subprocess.run`` with argument arrays."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _as_text(value: object) -> str:
    """Coerce captured output to text (``None`` becomes an empty string)."""
    return "" if value is None else str(value)


class ProjectTestRunner:
    """Run an explicitly configured project test command in a repository.

    Args:
        timeout_seconds: per-run timeout in seconds.
        runner: optional injectable process runner (tests / alternative
            process backends). Defaults to :func:`_subprocess_runner`.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 600.0,
        runner: Optional[TestProcessRunner] = None,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _subprocess_runner if runner is None else runner

    @property
    def timeout_seconds(self) -> float:
        """Configured per-run timeout in seconds."""
        return self._timeout_seconds

    @staticmethod
    def validate_command(test_command: Sequence[str]) -> Tuple[str, ...]:
        """Validate and freeze an explicit test command.

        The command must be a non-empty sequence of non-empty strings whose
        first element names an executable (it must not start with ``-``, which
        would look like an option). This is the only thing that ever reaches
        the process runner; nothing is built from issue data at runtime.

        Raises:
            TestRunnerError: the command is missing or malformed.
        """
        if isinstance(test_command, (str, bytes)) or not isinstance(
            test_command, (list, tuple)
        ):
            raise TestRunnerError(
                "test_command must be an explicit sequence of strings, "
                "for example ['pytest', '-q']."
            )
        if not test_command:
            raise TestRunnerError("test_command must not be empty.")
        parts = []
        for part in test_command:
            if not isinstance(part, str):
                raise TestRunnerError(
                    "Every test_command element must be a string."
                )
            if part == "":
                raise TestRunnerError(
                    "test_command elements must not be empty strings."
                )
            parts.append(part)
        if parts[0].startswith("-"):
            raise TestRunnerError(
                "test_command must name an executable first; "
                "it must not start with '-'."
            )
        return tuple(parts)

    def run(
        self, repository_path: PathLike, test_command: Sequence[str]
    ) -> TestResult:
        """Execute ``test_command`` inside ``repository_path``.

        Args:
            repository_path: existing working copy; used as the working
                directory.
            test_command: explicit argument-array command, e.g.
                ``["pytest", "-q"]``.

        Returns:
            A typed :class:`TestResult`; execution failures are represented in
            the result, not raised.

        Raises:
            TestRunnerError: the repository path is not an existing directory,
                or the test command is missing/malformed. Both are checked
                before any process is launched.
        """
        repository = Path(repository_path).resolve()
        if not repository.is_dir():
            raise TestRunnerError(
                "Repository path is not an existing directory: "
                f"'{repository}'."
            )
        command = self.validate_command(test_command)

        try:
            outcome = self._runner(
                list(command), str(repository), self._timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            return TestResult(
                status=TestStatus.TIMEOUT,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=(
                    "Project tests timed out after "
                    f"{self._timeout_seconds:g} seconds. ({exc})"
                ),
            )
        except FileNotFoundError as exc:
            return TestResult(
                status=TestStatus.NOT_FOUND,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=redact_credentials(
                    "Test command executable not found: "
                    f"'{command[0]}' ({exc})."
                ),
            )
        except OSError as exc:
            return TestResult(
                status=TestStatus.EXECUTION_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=redact_credentials(
                    f"Failed to launch the test command: {exc}"
                ),
            )

        exit_code = int(getattr(outcome, "returncode", -1))
        stdout = redact_credentials(_as_text(getattr(outcome, "stdout", "")))
        stderr = redact_credentials(_as_text(getattr(outcome, "stderr", "")))
        if exit_code == 0:
            return TestResult(
                status=TestStatus.PASSED,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                command=command,
                error=None,
            )
        return TestResult(
            status=TestStatus.FAILED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=command,
            error=f"Project tests exited with code {exit_code}.",
        )


__all__: Sequence[str] = (
    "ProjectTestRunner",
    "TestProcessRunner",
    "TestResult",
    "TestRunnerError",
    "TestStatus",
)
