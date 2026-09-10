"""Git working-tree inspection after a Codex run (T12).

:class:`GitDiffInspector` reports *what changed* in a repository working tree:
clean/dirty state, changed file list, diff/stat text, and the raw diff. It
exposes structured information so T13 (change-scope validation, not
implemented here) can later decide whether the changes are acceptable.

Inspection-only guarantee
-------------------------
This module never stages, commits, pushes, reverts, or rewrites history. It
only runs read-only Git commands (``status``, ``diff``, ``ls-files``).
Changes are never rejected here: rejecting unrelated edits is T13 policy
enforcement and is deliberately absent from T12.

Safety notes
------------
* All Git calls use argument arrays - never ``shell=True``.
* File lists are read with the ``-z`` NUL separator so paths containing
  spaces, quotes, or newlines cannot be misparsed.
* Git error output is passed through ``redact_credentials`` before it reaches
  an exception message.
* The process runner is injectable so tests never need a real Codex run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

from repository import redact_credentials

PathLike = Union[str, Path]


class GitDiffError(Exception):
    """Raised when the working-tree inspection cannot be completed."""


@dataclass(frozen=True)
class GitDiffResult:
    """Read-only snapshot of a repository working tree.

    Attributes:
        repository_path: absolute path that was inspected.
        is_clean: no tracked modifications and no untracked files.
        has_changes: ``not is_clean``.
        changed_files: sorted repository-relative paths of every changed or
            new file (tracked modifications plus untracked files).
        untracked_files: sorted repository-relative paths of new, untracked
            files (subset of ``changed_files``).
        diff_stat: ``git diff``/``git diff --cached`` stat text (tracked
            changes only; empty string when there are none).
        diff_text: full unified diff text (tracked changes only; empty string
            when there are none).
    """

    repository_path: Path
    is_clean: bool
    has_changes: bool
    changed_files: Tuple[str, ...]
    untracked_files: Tuple[str, ...]
    diff_stat: str
    diff_text: str


#: A runner takes ``(args, cwd, timeout_seconds)`` and returns an object with
#: ``returncode``/``stdout``/``stderr``, or raises ``OSError`` /
#: ``subprocess.TimeoutExpired``.
GitRunner = Callable[[Sequence[str], str, float], object]


def _subprocess_git_runner(
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


def _split_nul(text: str) -> List[str]:
    """Split NUL-separated git output, dropping empty trailing entries."""
    return [entry for entry in text.split("\0") if entry != ""]


def _merge_text(parts: List[str]) -> str:
    """Join non-empty command outputs with single newlines between them."""
    cleaned = [part.rstrip("\n") for part in parts if part.strip() != ""]
    return "\n".join(cleaned)


class GitDiffInspector:
    """Inspect (read-only) the working tree of a local Git repository.

    Args:
        git_command: Git executable name or path (default ``"git"``).
        timeout_seconds: per-command timeout.
        runner: optional injectable process runner. Defaults to
            :func:`_subprocess_git_runner`.
    """

    def __init__(
        self,
        git_command: str = "git",
        *,
        timeout_seconds: float = 300.0,
        runner: Optional[GitRunner] = None,
    ) -> None:
        self._git_command = git_command
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _subprocess_git_runner if runner is None else runner

    def _run(self, repository: Path, *args: str) -> object:
        command = [self._git_command, "-C", str(repository), *args]
        try:
            outcome = self._runner(command, str(repository), self._timeout_seconds)
        except FileNotFoundError as exc:
            raise GitDiffError(
                f"Git executable not found (configured '{self._git_command}'): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitDiffError(
                f"Git inspection timed out after {self._timeout_seconds:g} seconds."
            ) from exc
        except OSError as exc:  # pragma: no cover - defensive, platform edge cases
            raise GitDiffError(
                redact_credentials(f"Failed to run git: {exc}")
            ) from exc
        if int(outcome.returncode) != 0:
            detail = redact_credentials(
                "" if outcome.stderr is None else str(outcome.stderr)
            )
            if not detail.strip():
                detail = f"git exited with code {outcome.returncode}."
            raise GitDiffError(f"Git command failed: {detail.strip()}")
        return outcome

    def inspect(self, repository_path: PathLike) -> GitDiffResult:
        """Snapshot the working tree of ``repository_path``.

        Args:
            repository_path: existing working copy to inspect.

        Returns:
            A :class:`GitDiffResult`. No changes are made or rejected.

        Raises:
            GitDiffError: the path is missing / is not a Git working copy, a
                Git command fails, times out, or the executable is missing.
        """
        repository = Path(repository_path).resolve()
        if not repository.is_dir():
            raise GitDiffError(
                f"Repository path is not an existing directory: '{repository}'."
            )

        porcelain = self._run(
            repository, "status", "--porcelain", "--untracked-files=all"
        )
        is_clean = str(porcelain.stdout or "").strip() == ""
        if is_clean:
            return GitDiffResult(
                repository_path=repository,
                is_clean=True,
                has_changes=False,
                changed_files=(),
                untracked_files=(),
                diff_stat="",
                diff_text="",
            )

        unstaged_names = self._run(repository, "diff", "--name-only", "-z")
        staged_names = self._run(repository, "diff", "--cached", "--name-only", "-z")
        untracked_list = self._run(
            repository, "ls-files", "--others", "--exclude-standard", "-z"
        )

        tracked = sorted(
            set(_split_nul(str(unstaged_names.stdout or "")))
            | set(_split_nul(str(staged_names.stdout or "")))
        )
        untracked = sorted(_split_nul(str(untracked_list.stdout or "")))
        changed_files = sorted(set(tracked) | set(untracked))

        stat_parts: List[str] = []
        diff_parts: List[str] = []
        for extra in ((), ("--cached",)):
            stat_out = self._run(repository, "diff", *extra, "--stat")
            stat_parts.append(str(stat_out.stdout or ""))
            diff_out = self._run(repository, "diff", *extra)
            diff_parts.append(str(diff_out.stdout or ""))

        return GitDiffResult(
            repository_path=repository,
            is_clean=False,
            has_changes=True,
            changed_files=tuple(changed_files),
            untracked_files=tuple(untracked),
            diff_stat=_merge_text(stat_parts),
            diff_text=_merge_text(diff_parts),
        )

