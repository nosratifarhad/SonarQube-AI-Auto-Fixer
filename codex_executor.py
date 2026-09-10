"""Codex execution as an external process (T10).

:class:`CodexExecutor` runs the Codex CLI in the target repository and
returns a typed :class:`CodexResult`. Codex is treated as an external
command-line tool; there is no OpenAI SDK and no API-key management here.

Design notes
------------
* Subprocesses are launched with argument arrays and never with ``shell=True``.
* The prompt is written to the child's stdin (``codex exec`` reads its prompt
  from stdin when none is given on the command line).
* stdout/stderr/exit code are captured and the timeout is enforced by the
  runner.
* The process runner is injectable so tests can exercise every failure path
  without the Codex CLI being installed.
* Every error/output string is passed through ``redact_credentials`` before it
  is stored on a result or raised, so credentials can never leak into logs.

Results clearly distinguish: successful process execution (``SUCCESS``),
process failure (``FAILED``), timeout (``TIMEOUT``), and an executable that
could not be launched (``NOT_FOUND``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple, Union

from repository import redact_credentials

PathLike = Union[str, Path]


class CodexExecutionStatus(Enum):
    """Outcome categories for a single Codex process run."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NOT_FOUND = "executable-not-found"


@dataclass(frozen=True)
class CodexResult:
    """Typed, secret-free record of one Codex execution.

    Attributes:
        status: coarse outcome category (:class:`CodexExecutionStatus`).
        exit_code: child process exit code, or ``None`` when the process
            never ran to completion (timeout / not found).
        stdout: captured standard output (credentials redacted).
        stderr: captured standard error (credentials redacted).
        command: the exact argument array used.
        error: human-readable failure detail, or ``None`` on success.
    """

    status: CodexExecutionStatus
    exit_code: Optional[int]
    stdout: str
    stderr: str
    command: Tuple[str, ...]
    error: Optional[str] = None

    @property
    def timed_out(self) -> bool:
        """True when Codex did not finish inside the configured timeout."""
        return self.status is CodexExecutionStatus.TIMEOUT

    @property
    def executable_not_found(self) -> bool:
        """True when the Codex executable could not be launched."""
        return self.status is CodexExecutionStatus.NOT_FOUND

    @property
    def process_failed(self) -> bool:
        """True when the process ran but exited non-zero."""
        return self.status is CodexExecutionStatus.FAILED

    def as_dict(self) -> dict:
        """Secret-free, compact summary for logging."""
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "executable_not_found": self.executable_not_found,
            "error": self.error,
        }


#: A runner takes ``(args, cwd, stdin_text, timeout_seconds)`` and returns an
#: object exposing ``returncode``/``stdout``/``stderr``, or raises
#: ``OSError``/``subprocess.TimeoutExpired``.
Runner = Callable[[Sequence[str], str, str, float], object]


def _subprocess_runner(
    args: Sequence[str],
    cwd: str,
    prompt: str,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Default runner: ``subprocess.run`` with argument arrays."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _as_text(value: object) -> str:
    return "" if value is None else str(value)


class CodexExecutorError(Exception):
    """Raised for caller errors (for example a bad repository path)."""


class CodexExecutor:
    """Execute the external Codex CLI inside a repository.

    Args:
        codex_command: Codex executable name or path (default ``"codex"``).
        codex_args: argument array inserted after ``codex_command``; the
            default is ``("exec",)`` which asks Codex to run non-interactively
            and read the prompt from stdin. Additional CLI flags may be
            appended here for site-specific setups.
        timeout_seconds: per-process timeout; a longer prompt/fix run may need
            a large value.
        runner: optional injectable process runner (testing/alternative
            process backends). Defaults to :func:`_subprocess_runner`.
    """

    def __init__(
        self,
        codex_command: str = "codex",
        *,
        codex_args: Sequence[str] = ("exec",),
        timeout_seconds: float = 900.0,
        runner: Optional[Runner] = None,
    ) -> None:
        self._codex_command = codex_command
        self._codex_args = tuple(codex_args)
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _subprocess_runner if runner is None else runner

    def execute(self, prompt: str, repository_path: PathLike) -> CodexResult:
        """Run Codex with ``prompt`` inside ``repository_path``.

        Args:
            prompt: deterministic prompt produced by T09.
            repository_path: existing directory that is the working copy where
                Codex should make its edits.

        Returns:
            A typed :class:`CodexResult`; every failure mode is represented in
            the result, not raised.

        Raises:
            CodexExecutorError: when ``repository_path`` is not an existing
                directory (caller error, detected before any process launch).
        """
        repository = Path(repository_path).resolve()
        if not repository.is_dir():
            raise CodexExecutorError(
                f"Repository path is not an existing directory: '{repository}'."
            )

        command = (self._codex_command, *self._codex_args)
        try:
            outcome = self._runner(
                list(command), str(repository), prompt, self._timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            return CodexResult(
                status=CodexExecutionStatus.TIMEOUT,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=(
                    f"Codex timed out after {self._timeout_seconds:g} seconds. "
                    f"({exc})"
                ),
            )
        except FileNotFoundError as exc:
            return CodexResult(
                status=CodexExecutionStatus.NOT_FOUND,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=redact_credentials(
                    f"Codex executable not found: '{self._codex_command}' ({exc})."
                ),
            )
        except OSError as exc:  # pragma: no cover - defensive, platform edge cases
            return CodexResult(
                status=CodexExecutionStatus.FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                error=redact_credentials(f"Failed to launch Codex: {exc}"),
            )

        exit_code = int(getattr(outcome, "returncode", -1))
        stdout = redact_credentials(_as_text(getattr(outcome, "stdout", "")))
        stderr = redact_credentials(_as_text(getattr(outcome, "stderr", "")))
        if exit_code == 0:
            return CodexResult(
                status=CodexExecutionStatus.SUCCESS,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                command=command,
                error=None,
            )
        return CodexResult(
            status=CodexExecutionStatus.FAILED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=command,
            error=f"Codex exited with code {exit_code}.",
        )

