"""Trigger a SonarQube analysis of the modified working copy (T16).

:class:`SonarAnalysisTrigger` asks SonarQube to analyse the repository state
*after* the AI change was applied. It executes a **configured** analysis
command (for a typical setup the SonarScanner CLI, e.g. ``sonar-scanner``)
inside the local clone, using argument arrays and never a shell.

Trigger is not completion
-------------------------
A ``TRIGGERED`` result means **"the analysis was successfully requested"**.
It does **not** mean the analysis finished, and it does **not** mean SonarQube
reports no issues. SonarQube's compute engine processes the submitted report
asynchronously, so waiting for a terminal state is T17 and deciding whether the
original issue is gone is T18/T19.

Mechanism (why a scanner command and not an invented endpoint)
--------------------------------------------------------------
The POC is designed around the analysis mechanism that a normal SonarQube
installation already supports: running the project's scanner/build analysis so
the server computes a new analysis for the project. No undocumented
``/api/ce/submit``-style endpoint is called and no report file is fabricated.
The command is fully configurable (:class:`AnalysisConfig`) so ``sonar-scanner``
can be replaced by ``mvn ... sonar:sonar``, ``dotnet sonarscanner ...``,
``gradle sonar``, a wrapper script, or a custom invocation.

Task identity
-------------
When the scanner reports the compute-engine task it also prints the report
processing URL, which contains the CE task id (``/api/ce/task?id=<task id>``).
That id is extracted from the captured output and returned as ``task_id`` so
T17 can wait for *this* analysis instead of asking "is the project green?".
Extraction is best-effort: ``task_id`` is ``None`` when no pattern matches, and
T17 must then refuse to guess.

Security notes
--------------
* Subprocesses use argument arrays - never ``shell=True``.
* The analysis command comes from configuration only; it is never built from
  SonarQube issue text, Codex output, or any other untrusted value.
* Credentials are passed through the **environment** (see
  :func:`build_sonar_environment`), never as command-line arguments, and the
  environment is never stored on a result or logged.
* Captured output is scrubbed of configured secrets and then credential-redacted
  (URL userinfo) before it is stored or embedded in an error message.
* Extracted task ids are treated as untrusted: they must match a strict safe
  charset and length limit or they are discarded.

Non-behaviour (by design)
-------------------------
This module never waits, polls, retries, resolves issues, commits, pushes,
creates branches, or edits source code.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from repository import redact_credentials

PathLike = Union[str, Path]

#: Scanner output patterns that carry the compute-engine task id. The SonarScanner
#: prints the report-processing URL as one of its informational lines
#: (``... More about the report processing at .../api/ce/task?id=<task id>``).
#: Patterns are ordered by specificity; the first pattern that matches wins and
#: the *last* match of that pattern is used (a run may submit several reports).
DEFAULT_TASK_ID_PATTERNS: Tuple[str, ...] = (
    r"/api/ce/task\?id=([A-Za-z0-9_.\-]+)",
    r"api/ce/task[^\s]*[?&]id=([A-Za-z0-9_.\-]+)",
    r"task[\s_-]?id\s*[:=]\s*([A-Za-z0-9_.\-]+)",
)

#: A task id is external data: accept only a conservative charset and length.
_TASK_ID_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,200}\Z")


class SonarAnalysisStatus(Enum):
    """Outcome category for one analysis trigger attempt."""

    TRIGGERED = "triggered"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NOT_FOUND = "executable-not-found"
    EXECUTION_ERROR = "execution-error"


class SonarAnalysisError(Exception):
    """Raised when the analysis cannot be triggered (caller error)."""


@dataclass(frozen=True)
class AnalysisConfig:
    """Configuration of the SonarQube analysis mechanism.

    Attributes:
        command: argument array of the analysis command (``("sonar-scanner",)``
            by default). Never a shell string, never built from untrusted text.
        timeout_seconds: wall-clock limit for the analysis process.
        environment: extra environment variables for the child process (for
            example ``{"SONAR_HOST_URL": ..., "SONAR_TOKEN": ...}``). These are
            merged over the current environment and are never logged.
        secrets: literal secret values to scrub from captured output (for
            example the configured token, which a verbose scanner could echo).
        task_id_patterns: regex patterns used to extract the compute-engine
            task id from the captured output.
    """

    command: Tuple[str, ...] = ("sonar-scanner",)
    timeout_seconds: float = 900.0
    environment: Mapping[str, str] = field(default_factory=dict)
    secrets: Tuple[str, ...] = ()
    task_id_patterns: Tuple[str, ...] = DEFAULT_TASK_ID_PATTERNS


@dataclass(frozen=True)
class SonarAnalysisTriggerResult:
    """Typed, secret-free record of one analysis trigger attempt.

    Attributes:
        status: coarse outcome category (:class:`SonarAnalysisStatus`).
        exit_code: process exit code, or ``None`` when the process never ran to
            completion (timeout / executable missing / launch error).
        stdout: captured standard output (secrets scrubbed, credentials redacted).
        stderr: captured standard error (secrets scrubbed, credentials redacted).
        command: the exact argument array that was used.
        task_id: compute-engine task id extracted from the output, or ``None``
            when it could not be determined (T17 must not guess in that case).
        error: human-readable failure detail, or ``None`` when triggered.
    """

    # Not a pytest test class (see ``TestStatus`` in ``test_runner``).
    __test__ = False

    status: SonarAnalysisStatus
    exit_code: Optional[int]
    stdout: str
    stderr: str
    command: Tuple[str, ...]
    task_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def triggered(self) -> bool:
        """True when the analysis was successfully *requested* (not completed)."""
        return self.status is SonarAnalysisStatus.TRIGGERED

    @property
    def timed_out(self) -> bool:
        """True when the analysis command did not finish inside the timeout."""
        return self.status is SonarAnalysisStatus.TIMEOUT

    @property
    def executable_not_found(self) -> bool:
        """True when the analysis executable could not be launched."""
        return self.status is SonarAnalysisStatus.NOT_FOUND

    @property
    def execution_error(self) -> bool:
        """True when the analysis process failed to launch for another reason."""
        return self.status is SonarAnalysisStatus.EXECUTION_ERROR

    @property
    def has_task_id(self) -> bool:
        """True when a compute-engine task id was extracted from the output."""
        return bool(self.task_id)

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging (no raw output, no env)."""
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "triggered": self.triggered,
            "timed_out": self.timed_out,
            "executable_not_found": self.executable_not_found,
            "execution_error": self.execution_error,
            "task_id": self.task_id,
            "error": self.error,
        }


#: A runner takes ``(args, cwd, env, timeout_seconds)`` and returns an object with
#: ``returncode``/``stdout``/``stderr``, or raises ``OSError`` /
#: ``subprocess.TimeoutExpired``.
AnalysisRunner = Callable[[Sequence[str], str, Mapping[str, str], float], object]


def _subprocess_analysis_runner(
    args: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Default runner: ``subprocess.run`` with argument arrays and no shell."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _as_text(value: object) -> str:
    """Best-effort conversion of captured output to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _scrub(text: str, secrets: Sequence[str]) -> str:
    """Remove configured secret values, then URL credentials, from ``text``."""
    scrubbed = text
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "***")
    return redact_credentials(scrubbed)


def build_sonar_environment(
    sonar_url: str,
    sonar_token: Optional[str] = None,
    *,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the standard scanner environment for ``sonar_url``/``sonar_token``.

    Reuses the existing configuration values (``config.SONAR_URL`` /
    ``config.SONAR_TOKEN``) but passes them to the child process through
    environment variables - the SonarScanner convention - so credentials never
    appear in the command line. The returned mapping is intended for
    ``AnalysisConfig.environment`` and is never logged.

    The project key is deliberately **not** exported here: it is not a secret
    and belongs in the repository's scanner configuration (for example
    ``sonar-project.properties``) or in an explicit command argument supplied by
    configuration - never in text derived from a SonarQube issue.
    """
    environment: Dict[str, str] = {"SONAR_HOST_URL": str(sonar_url)}
    if sonar_token:
        environment["SONAR_TOKEN"] = str(sonar_token)
    if extra:
        environment.update({str(key): str(value) for key, value in extra.items()})
    return environment


def is_safe_task_id(value: object) -> bool:
    """Return True when ``value`` looks like a usable compute-engine task id.

    Task ids are external data. Only a conservative charset and length are
    accepted, so a crafted value can never be injected into a request or an
    API path. Reused by T17 to validate ids it did not produce itself.
    """
    return bool(_TASK_ID_RE.match(value if isinstance(value, str) else ""))


def extract_task_id(
    stdout: str,
    stderr: str,
    patterns: Sequence[str] = DEFAULT_TASK_ID_PATTERNS,
) -> Optional[str]:
    """Extract the compute-engine task id from captured scanner output.

    The first pattern that matches wins and its *last* match is returned. The
    candidate is treated as untrusted data: values that do not match a strict
    safe charset/length are discarded and ``None`` is returned.

    Returns:
        A safe task id, or ``None`` when it cannot be determined.
    """
    haystack = f"{stdout or ''}\n{stderr or ''}"
    for pattern in patterns or ():
        try:
            candidates = re.findall(pattern, haystack, flags=re.IGNORECASE)
        except re.error:
            continue
        for candidate in reversed(candidates):
            value = candidate.strip() if isinstance(candidate, str) else ""
            if is_safe_task_id(value):
                return value
    return None


class SonarAnalysisTrigger:
    """Request a SonarQube analysis of a local working copy.

    Args:
        analysis_config: default :class:`AnalysisConfig` used when
            :meth:`trigger` is called without an explicit configuration.
        runner: injectable process runner; defaults to real ``subprocess``.

    The trigger is a thin, safe process wrapper: it validates its inputs, runs
    the configured command inside the repository, scrubs the captured output,
    and extracts the compute-engine task id. It never waits for the analysis to
    finish (T17) and never interprets issues (T18/T19).
    """

    def __init__(
        self,
        analysis_config: Optional[AnalysisConfig] = None,
        *,
        runner: Optional[AnalysisRunner] = None,
    ) -> None:
        self._config = analysis_config if analysis_config is not None else AnalysisConfig()
        self._runner = _subprocess_analysis_runner if runner is None else runner

    @property
    def analysis_config(self) -> AnalysisConfig:
        """The default analysis configuration used by :meth:`trigger`."""
        return self._config

    @staticmethod
    def validate_command(command: Sequence[str]) -> Tuple[str, ...]:
        """Validate an analysis command argument array.

        Rules: a non-empty sequence of non-empty strings whose first element
        names an executable (it must not start with ``-``, which would be an
        option rather than a program).

        Raises:
            SonarAnalysisError: the command is missing, malformed, or a string
                (a shell string is never accepted).
        """
        if command is None or isinstance(command, (str, bytes)):
            raise SonarAnalysisError(
                "Analysis command must be a non-empty argument array, "
                "not a string."
            )
        try:
            parts = list(command)
        except TypeError as exc:
            raise SonarAnalysisError(
                f"Analysis command must be a sequence of arguments: {exc}"
            ) from exc
        if not parts:
            raise SonarAnalysisError("Analysis command must not be empty.")
        cleaned = []
        for part in parts:
            if not isinstance(part, str) or not part.strip():
                raise SonarAnalysisError(
                    "Every analysis command argument must be a non-empty "
                    "string."
                )
            cleaned.append(part)
        if cleaned[0].startswith("-"):
            raise SonarAnalysisError(
                "Analysis command must name an executable first; it must not "
                "start with '-'."
            )
        return tuple(cleaned)

    def trigger(
        self,
        repository_path: PathLike,
        analysis_config: Optional[AnalysisConfig] = None,
    ) -> SonarAnalysisTriggerResult:
        """Run the configured analysis command inside ``repository_path``.

        Args:
            repository_path: existing working copy to analyse (used as the
                working directory).
            analysis_config: configuration for this call; falls back to the
                instance configuration.

        Returns:
            A typed :class:`SonarAnalysisTriggerResult`. A ``TRIGGERED`` status
            means the analysis was successfully *requested* - never that it
            completed. Execution failures are represented in the result.

        Raises:
            SonarAnalysisError: the repository path is not an existing
                directory, or the analysis command is missing/malformed. Both
                are checked before any process is launched.
        """
        config = self._config if analysis_config is None else analysis_config
        repository = Path(repository_path).resolve()
        if not repository.is_dir():
            raise SonarAnalysisError(
                f"Repository path is not an existing directory: '{repository}'."
            )
        command = self.validate_command(config.command)
        timeout = float(config.timeout_seconds)
        if timeout <= 0:
            raise SonarAnalysisError(
                f"Analysis timeout must be positive (got {config.timeout_seconds!r})."
            )

        environment: Dict[str, str] = {str(k): str(v) for k, v in os.environ.items()}
        environment.update(
            {str(k): str(v) for k, v in (config.environment or {}).items()}
        )
        secrets = tuple(str(s) for s in (config.secrets or ()) if str(s))

        try:
            outcome = self._runner(list(command), str(repository), environment, timeout)
        except subprocess.TimeoutExpired as exc:
            return SonarAnalysisTriggerResult(
                status=SonarAnalysisStatus.TIMEOUT,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                task_id=None,
                error=(
                    "SonarQube analysis did not finish inside the timeout of "
                    f"{timeout:g} seconds. ({exc})"
                ),
            )
        except FileNotFoundError as exc:
            return SonarAnalysisTriggerResult(
                status=SonarAnalysisStatus.NOT_FOUND,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                task_id=None,
                error=_scrub(
                    "Analysis command executable not found: "
                    f"'{command[0]}' ({exc}).",
                    secrets,
                ),
            )
        except OSError as exc:
            return SonarAnalysisTriggerResult(
                status=SonarAnalysisStatus.EXECUTION_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                command=command,
                task_id=None,
                error=_scrub(f"Failed to launch the analysis command: {exc}", secrets),
            )

        exit_code = int(getattr(outcome, "returncode", -1))
        stdout = _scrub(_as_text(getattr(outcome, "stdout", "")), secrets)
        stderr = _scrub(_as_text(getattr(outcome, "stderr", "")), secrets)
        task_id = extract_task_id(stdout, stderr, config.task_id_patterns)

        if exit_code == 0:
            return SonarAnalysisTriggerResult(
                status=SonarAnalysisStatus.TRIGGERED,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                command=command,
                task_id=task_id,
                error=None,
            )
        return SonarAnalysisTriggerResult(
            status=SonarAnalysisStatus.FAILED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=command,
            task_id=task_id,
            error=f"Analysis command exited with code {exit_code}.",
        )


__all__: Sequence[str] = (
    "AnalysisConfig",
    "AnalysisRunner",
    "DEFAULT_TASK_ID_PATTERNS",
    "SonarAnalysisError",
    "SonarAnalysisStatus",
    "SonarAnalysisTrigger",
    "SonarAnalysisTriggerResult",
    "build_sonar_environment",
    "extract_task_id",
    "is_safe_task_id",
)

