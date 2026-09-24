"""Orchestration configuration for the SonarQube AI Auto-Fixer (T30).

This module is the **single place** that turns operator configuration (an
environment mapping / ``.env`` values) into the immutable
:class:`PipelineConfig` the orchestration layer consumes. It is deliberately
fail-closed:

* every value the pipeline *cannot* invent is required; a missing or empty
  value raises :class:`PipelineConfigError` instead of silently defaulting;
* a value that is present but malformed (a bad boolean, a negative timeout, a
  non-numeric limit, an empty command) is rejected with a message that names
  the variable and the *shape* of the problem, never a secret value;
* the SonarQube token is accepted but is never echoed, never part of
  ``as_dict()`` and never logged - it exists only to build the scanner
  environment and the secret-scan list.

Command variables accept either a JSON array (``["pytest", "-q"]``) or a
whitespace-separated string (``pytest -q``). The JSON form is preferred because
it cannot mis-split an argument that contains a space.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from repository import redact_credentials

#: Environment variable names read by :func:`load_pipeline_config`.
ENV_REPOSITORY_URL = "FIXER_REPOSITORY_URL"
ENV_SOURCE_BRANCH = "FIXER_SOURCE_BRANCH"
ENV_DEFAULT_BRANCH = "FIXER_DEFAULT_BRANCH"
ENV_REMOTE_NAME = "FIXER_REMOTE_NAME"
ENV_DESTINATION_BRANCH = "FIXER_DESTINATION_BRANCH"
ENV_WORK_DIR = "FIXER_WORK_DIR"
ENV_CODEX_COMMAND = "FIXER_CODEX_COMMAND"
ENV_CODEX_ARGS = "FIXER_CODEX_ARGS"
ENV_CODEX_TIMEOUT = "FIXER_CODEX_TIMEOUT"
ENV_TEST_COMMAND = "FIXER_TEST_COMMAND"
ENV_TEST_TIMEOUT = "FIXER_TEST_TIMEOUT"
ENV_ANALYSIS_COMMAND = "FIXER_ANALYSIS_COMMAND"
ENV_ANALYSIS_TIMEOUT = "FIXER_ANALYSIS_TIMEOUT"
ENV_ANALYSIS_WAIT_TIMEOUT = "FIXER_ANALYSIS_WAIT_TIMEOUT"
ENV_ANALYSIS_POLL_INTERVAL = "FIXER_ANALYSIS_POLL_INTERVAL"
ENV_EXCLUSIVE_ANALYSIS = "FIXER_EXCLUSIVE_ANALYSIS"
ENV_MAX_ISSUES = "FIXER_MAX_ISSUES"
ENV_MAX_ITERATIONS = "FIXER_MAX_ITERATIONS"
ENV_ALLOWED_RULES = "FIXER_ALLOWED_RULES"
ENV_PROTECTED_BRANCHES = "FIXER_PROTECTED_BRANCHES"
ENV_REQUIRE_AI_BRANCH_PREFIX = "FIXER_REQUIRE_AI_BRANCH_PREFIX"
ENV_COMMIT_FIXES = "FIXER_COMMIT_FIXES"
ENV_PUSH_FIXES = "FIXER_PUSH_FIXES"
ENV_KEEP_WORK_DIR = "FIXER_KEEP_WORK_DIR"

ENV_SONAR_URL = "SONAR_URL"
ENV_SONAR_TOKEN = "SONAR_TOKEN"
ENV_PROJECT_KEY = "PROJECT_KEY"

#: Default protected branch names (kept equal to T07/T20/T21/T26).
DEFAULT_PROTECTED_BRANCHES: Tuple[str, ...] = (
    "main",
    "master",
    "develop",
    "trunk",
)


class PipelineConfigError(Exception):
    """Raised when the orchestration configuration is missing or malformed."""


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _required(env: Mapping[str, str], name: str) -> str:
    value = _clean(env.get(name))
    if not value:
        raise PipelineConfigError(
            f"Missing required configuration '{name}'."
        )
    return value


def _optional(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = _clean(env.get(name))
    return value if value else default


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _clean(env.get(name))
    if not raw:
        return default
    lowered = raw.casefold()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise PipelineConfigError(
        f"'{name}' must be a boolean (true/false), not {raw!r}."
    )


def _int(env: Mapping[str, str], name: str, *, minimum: int) -> int:
    raw = _required(env, name)
    try:
        value = int(raw)
    except ValueError:
        raise PipelineConfigError(
            f"'{name}' must be an integer, not {raw!r}."
        ) from None
    # ``int("1.0")`` already fails above; a bool cannot appear from the
    # environment as text, so no bool guard is needed here.
    if value < minimum:
        raise PipelineConfigError(
            f"'{name}' must be at least {minimum} (got {value})."
        )
    return value


def _float(env: Mapping[str, str], name: str, *, minimum: float) -> float:
    raw = _required(env, name)
    try:
        value = float(raw)
    except ValueError:
        raise PipelineConfigError(
            f"'{name}' must be a number, not {raw!r}."
        ) from None
    # ``float`` accepts the words ``nan``, ``inf`` and ``-inf``, and a NaN
    # compares false against every bound. A non-finite timeout would silently
    # disable the bound it exists to enforce (or poison an arithmetic result),
    # so it is refused rather than accepted.
    if not math.isfinite(value):
        raise PipelineConfigError(
            f"'{name}' must be a finite number, not {raw!r}."
        )
    if value < minimum:
        raise PipelineConfigError(
            f"'{name}' must be at least {minimum:g} (got {value:g})."
        )
    return value


def _csv(env: Mapping[str, str], name: str, default: Sequence[str] = ()) -> Tuple[str, ...]:
    raw = _clean(env.get(name))
    if not raw:
        return tuple(default)
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts


def _command(env: Mapping[str, str], name: str) -> Tuple[str, ...]:
    """Parse a command variable into an argument tuple.

    A value that starts with ``[`` is parsed as a JSON array of strings; any
    other value is split on whitespace. In both cases the result must be a
    non-empty sequence of non-empty strings, otherwise the configuration is
    rejected (never silently repaired).
    """
    raw = _required(env, name)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise PipelineConfigError(
                f"'{name}' is not valid JSON: {exc}."
            ) from None
        if not isinstance(parsed, list) or not all(
            isinstance(part, str) for part in parsed
        ):
            raise PipelineConfigError(
                f"'{name}' JSON form must be an array of strings."
            )
        parts = [part for part in parsed]
    else:
        parts = raw.split()
    if not parts or any(not part for part in parts):
        raise PipelineConfigError(
            f"'{name}' must be a non-empty command."
        )
    if parts[0].startswith("-"):
        raise PipelineConfigError(
            f"'{name}' must name an executable first."
        )
    return tuple(parts)


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable, validated orchestration configuration.

    Attributes are grouped by the stage that consumes them. ``sonar_token`` is
    deliberately excluded from :meth:`as_dict`.
    """

    repository_url: str
    source_branch: str
    work_dir: Path
    test_command: Tuple[str, ...]
    analysis_command: Tuple[str, ...]
    sonar_url: str
    project_key: str

    sonar_token: Optional[str] = None
    default_branch: Optional[str] = None
    remote_name: str = "origin"
    destination_branch: Optional[str] = None

    codex_command: str = "codex"
    codex_args: Tuple[str, ...] = ("exec",)
    codex_timeout_seconds: float = 900.0

    test_timeout_seconds: float = 600.0

    analysis_timeout_seconds: float = 900.0
    analysis_wait_timeout_seconds: float = 600.0
    analysis_poll_interval_seconds: float = 5.0

    exclusive_analysis: bool = False

    max_issues: Optional[int] = None
    max_iterations: Optional[int] = None

    allowed_rules: Tuple[str, ...] = ()
    protected_branches: Tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    require_ai_fix_branch_prefix: bool = True

    commit_fixes: bool = False
    push_fixes: bool = False
    keep_work_dir: bool = False

    branch_timestamp: Optional[str] = None

    @property
    def forbidden_secrets(self) -> Tuple[str, ...]:
        """Literal secrets that must never be published (the token only)."""
        return tuple(secret for secret in (self.sonar_token,) if secret)

    def __post_init__(self) -> None:
        """Validate the record even when it is built without the loader.

        :func:`load_pipeline_config` already validates every value, but
        :class:`PipelineConfig` is part of the public surface: a directly
        constructed record must not be able to bypass a safety knob. A truthy
        non-``bool`` ``commit_fixes`` would otherwise enable an irreversible
        step, and a non-finite timeout would silently disable its bound. The
        checks mirror the loader so both entry points agree.
        """
        for name in (
            "commit_fixes",
            "push_fixes",
            "keep_work_dir",
            "exclusive_analysis",
            "require_ai_fix_branch_prefix",
        ):
            if type(getattr(self, name)) is not bool:
                raise PipelineConfigError(f"'{name}' must be a bool.")
        for name in (
            "codex_timeout_seconds",
            "test_timeout_seconds",
            "analysis_timeout_seconds",
            "analysis_wait_timeout_seconds",
            "analysis_poll_interval_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise PipelineConfigError(
                    f"'{name}' must be a positive finite number."
                )
        for name in ("max_issues", "max_iterations"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise PipelineConfigError(
                    f"'{name}' must be None or a non-negative integer."
                )

    def as_dict(self) -> dict:
        """Configuration view for logging (never includes the token).

        The repository URL is credential-redacted because a Git remote may embed
        userinfo (``https://user:token@host/...``); the token itself is never
        published at all.
        """
        return {
            "repository_url": redact_credentials(self.repository_url),
            "source_branch": self.source_branch,
            "default_branch": self.default_branch,
            "remote_name": self.remote_name,
            "destination_branch": self.destination_branch,
            "work_dir": str(self.work_dir),
            "codex_command": self.codex_command,
            "codex_args": list(self.codex_args),
            "codex_timeout_seconds": self.codex_timeout_seconds,
            "test_command": list(self.test_command),
            "test_timeout_seconds": self.test_timeout_seconds,
            "analysis_command": list(self.analysis_command),
            "analysis_timeout_seconds": self.analysis_timeout_seconds,
            "analysis_wait_timeout_seconds": self.analysis_wait_timeout_seconds,
            "analysis_poll_interval_seconds": self.analysis_poll_interval_seconds,
            "exclusive_analysis": self.exclusive_analysis,
            "max_issues": self.max_issues,
            "max_iterations": self.max_iterations,
            "allowed_rules": list(self.allowed_rules),
            "protected_branches": list(self.protected_branches),
            "require_ai_fix_branch_prefix": self.require_ai_fix_branch_prefix,
            "commit_fixes": self.commit_fixes,
            "push_fixes": self.push_fixes,
            "keep_work_dir": self.keep_work_dir,
            "sonar_url": self.sonar_url,
            "project_key": self.project_key,
            "has_token": bool(self.sonar_token),
        }


def load_pipeline_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    work_dir: Optional[object] = None,
) -> PipelineConfig:
    """Build a :class:`PipelineConfig` from ``env`` (defaults to ``os.environ``).

    Args:
        env: the configuration mapping. ``None`` means ``os.environ``.
        work_dir: optional override for the clone base directory.

    Returns:
        A validated, immutable :class:`PipelineConfig`.

    Raises:
        PipelineConfigError: a required value is missing, or a present value is
            malformed. The message names the variable and never a secret.
    """
    if env is None:
        import os

        env = os.environ

    if work_dir is not None:
        work_directory = Path(work_dir)
    else:
        work_directory = Path(_required(env, ENV_WORK_DIR)).expanduser()

    sonar_token = _optional(env, ENV_SONAR_TOKEN) or None
    default_branch = _optional(env, ENV_DEFAULT_BRANCH) or None
    destination_branch = _optional(env, ENV_DESTINATION_BRANCH) or None

    return PipelineConfig(
        repository_url=_required(env, ENV_REPOSITORY_URL),
        source_branch=_required(env, ENV_SOURCE_BRANCH),
        work_dir=work_directory,
        test_command=_command(env, ENV_TEST_COMMAND),
        analysis_command=_command(env, ENV_ANALYSIS_COMMAND),
        sonar_url=_required(env, ENV_SONAR_URL),
        project_key=_required(env, ENV_PROJECT_KEY),
        sonar_token=sonar_token,
        default_branch=default_branch,
        remote_name=_optional(env, ENV_REMOTE_NAME, "origin"),
        destination_branch=destination_branch,
        codex_command=_optional(env, ENV_CODEX_COMMAND, "codex"),
        codex_args=_command_or_default(env, ENV_CODEX_ARGS, ("exec",)),
        codex_timeout_seconds=_float(env, ENV_CODEX_TIMEOUT, minimum=0.001)
        if _clean(env.get(ENV_CODEX_TIMEOUT))
        else 900.0,
        test_timeout_seconds=_float(env, ENV_TEST_TIMEOUT, minimum=0.001)
        if _clean(env.get(ENV_TEST_TIMEOUT))
        else 600.0,
        analysis_timeout_seconds=_float(env, ENV_ANALYSIS_TIMEOUT, minimum=0.001)
        if _clean(env.get(ENV_ANALYSIS_TIMEOUT))
        else 900.0,
        analysis_wait_timeout_seconds=_float(
            env, ENV_ANALYSIS_WAIT_TIMEOUT, minimum=0.001
        )
        if _clean(env.get(ENV_ANALYSIS_WAIT_TIMEOUT))
        else 600.0,
        analysis_poll_interval_seconds=_float(
            env, ENV_ANALYSIS_POLL_INTERVAL, minimum=0.001
        )
        if _clean(env.get(ENV_ANALYSIS_POLL_INTERVAL))
        else 5.0,
        exclusive_analysis=_bool(env, ENV_EXCLUSIVE_ANALYSIS, False),
        max_issues=_int(env, ENV_MAX_ISSUES, minimum=0)
        if _clean(env.get(ENV_MAX_ISSUES))
        else None,
        max_iterations=_int(env, ENV_MAX_ITERATIONS, minimum=0)
        if _clean(env.get(ENV_MAX_ITERATIONS))
        else None,
        allowed_rules=_csv(env, ENV_ALLOWED_RULES),
        protected_branches=_csv(
            env, ENV_PROTECTED_BRANCHES, DEFAULT_PROTECTED_BRANCHES
        ),
        require_ai_fix_branch_prefix=_bool(
            env, ENV_REQUIRE_AI_BRANCH_PREFIX, True
        ),
        commit_fixes=_bool(env, ENV_COMMIT_FIXES, False),
        push_fixes=_bool(env, ENV_PUSH_FIXES, False),
        keep_work_dir=_bool(env, ENV_KEEP_WORK_DIR, False),
    )


def _command_or_default(
    env: Mapping[str, str], name: str, default: Tuple[str, ...]
) -> Tuple[str, ...]:
    """Like :func:`_command` but falls back to ``default`` when unset."""
    if not _clean(env.get(name)):
        return tuple(default)
    return _command(env, name)


__all__ = (
    "DEFAULT_PROTECTED_BRANCHES",
    "PipelineConfig",
    "PipelineConfigError",
    "load_pipeline_config",
)
