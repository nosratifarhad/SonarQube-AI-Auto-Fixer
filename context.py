"""Issue-context preparation (T08).

A :class:`IssueContext` bundles everything an AI agent needs to reason about
one SonarQube issue in a locally cloned repository:

* the normalized issue (``models.SonarIssue``),
* where the repository lives and which branches are in play,
* the exact file (repository-relative + absolute path) and line.

Responsibility boundary
------------------------
:class:`IssueContextPreparer` only *prepares* context: it resolves and
validates paths, verifies the agent branch is the one actually checked out,
and checks that the referenced file/line exist. It performs no fixing and
makes no source-code edits (those are out of scope, T09+).

Security note
-------------
File paths come from SonarQube ``component`` values and are treated as
untrusted input. :func:`safe_relative_path` rejects absolute paths and
``..`` traversal components, then re-verifies (with ``Path.resolve``) that
the final path stays inside the repository root, so a crafted component can
never make the tool read a file outside the clone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from models import SonarIssue
from repository import RepositoryManager

PathLike = Union[str, Path]


class ContextError(Exception):
    """Raised when an issue cannot be turned into a usable context."""


def safe_relative_path(root: PathLike, raw_path: str) -> Path:
    """Return a traversal-free, repository-root-relative ``Path``.

    Rules applied to ``raw_path`` (treated as untrusted input):

    * Empty paths are rejected.
    * Absolute paths (POSIX or Windows drive / UNC) are rejected.
    * Any ``..`` component is rejected (no escaping the repository root).
    * Symlink/containment is re-checked with ``Path.resolve``: the resolved
      file must still be inside the resolved repository root.
    * Backslashes are normalized to ``/`` so SonarQube paths that contain
      Windows-style separators still resolve predictably.

    Returns:
        A relative ``Path`` that is safe to join onto ``root``.

    Raises:
        ContextError: for empty, absolute, traversal, or escaping paths.
    """
    root_path = Path(root)
    value = str(raw_path or "").strip()
    if not value:
        raise ContextError("Issue has no usable file path.")
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ContextError(
            "Absolute file paths are not allowed "
            f"(got {raw_path!r})."
        )
    normalized = normalized.strip("/")
    parts = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ContextError(
                "File path must not traverse outside the repository "
                f"(got {raw_path!r})."
            )
        parts.append(part)
    if not parts:
        raise ContextError(
            "File path points at the repository root, not at a file "
            f"(got {raw_path!r})."
        )
    relative = Path(*parts)
    root_resolved = root_path.resolve()
    try:
        candidate = (root_resolved / relative).resolve()
        candidate.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ContextError(
            "File path resolves outside the repository root "
            f"(got {raw_path!r})."
        ) from exc
    return relative


@dataclass(frozen=True)
class IssueContext:
    """Everything an AI agent needs to work on one issue.

    Attributes:
        issue: the normalized SonarQube issue (T04 model).
        repository_path: absolute path of the cloned repository.
        source_branch: verified branch the issue was found on (T06).
        agent_branch: isolated agent branch created for the fix (T07).
        file_path: repository-relative file path (POSIX separators).
        absolute_file_path: absolute path of the target file on disk.
        line: target line within ``file_path`` (``None`` = whole file).
    """

    issue: SonarIssue
    repository_path: Path
    source_branch: str
    agent_branch: str
    file_path: str
    absolute_file_path: Path
    line: Optional[int]

    @property
    def has_file(self) -> bool:
        """True when the referenced file exists and is a regular file."""
        return self.absolute_file_path.is_file()

    def as_dict(self) -> dict:
        """Return a copy-safe summary suitable for logging (no secrets)."""
        return {
            "issue_key": self.issue.key,
            "rule": self.issue.rule,
            "severity": self.issue.severity,
            "source_branch": self.source_branch,
            "agent_branch": self.agent_branch,
            "file_path": self.file_path,
            "line": self.line,
        }


class IssueContextPreparer:
    """Turn one :class:`SonarIssue` into a verified :class:`IssueContext`.

    Args:
        repository: the :class:`RepositoryManager` used to verify the
            checked-out branch before the context is snapshotted.
    """

    def __init__(self, repository: RepositoryManager) -> None:
        self._repository = repository

    def prepare(
        self,
        *,
        issue: SonarIssue,
        repository_path: PathLike,
        source_branch: str,
        agent_branch: str,
    ) -> IssueContext:
        """Build and verify the context for ``issue``.

        Verification steps (each failure raises :class:`ContextError`):
            1. The repository exists and is a Git working copy.
            2. The branch actually checked out equals ``agent_branch``.
            3. ``issue.file_path`` is a safe, repository-relative path.
            4. The referenced file exists inside the clone.
            5. When ``issue.line`` is set, it is a positive number that does
               not exceed the number of lines in the file.

        Missing-file behavior: a missing file is an error, not a silent
        skip. The agent needs file content to fix an issue, and a missing
        file usually means the cloned revision does not match what
        SonarQube analysed, which the operator should know about.

        Returns:
            A verified :class:`IssueContext`.

        Raises:
            ContextError: any verification step fails.
        """
        repo_root = Path(repository_path).resolve()
        if not repo_root.exists() or not repo_root.is_dir():
            raise ContextError(
                f"Repository path does not exist or is not a directory: "
                f"'{repo_root}'."
            )
        current = self._repository.current_branch(repo_root)
        if current != agent_branch:
            raise ContextError(
                f"Expected agent branch '{agent_branch}' to be checked out, "
                f"but current branch is {current!r}."
            )

        relative = safe_relative_path(repo_root, issue.file_path)
        absolute_file = (repo_root / relative).resolve()
        if not absolute_file.is_file():
            raise ContextError(
                f"Issue file '{relative}' does not exist in repository "
                f"'{repo_root}'. The clone may not match the SonarQube "
                f"analysis revision; re-clone/refresh before fixing."
            )

        line = issue.line
        if line is not None:
            if line < 1:
                raise ContextError(
                    f"Issue line must be a positive integer (got {line})."
                )
            line_count = self._count_text_lines(absolute_file)
            if line_count is not None and line > line_count:
                raise ContextError(
                    f"Issue line {line} is beyond the end of file "
                    f"'{relative}' (file has {line_count} lines)."
                )

        return IssueContext(
            issue=issue,
            repository_path=repo_root,
            source_branch=source_branch,
            agent_branch=agent_branch,
            file_path=relative.as_posix(),
            absolute_file_path=absolute_file,
            line=line,
        )

    @staticmethod
    def _count_text_lines(path: Path) -> Optional[int]:
        """Return the number of lines, or None for binary files.

        Binary-file detection is best-effort: when the file cannot be
        decoded as UTF-8 (with a UTF-8 BOM fallback) it is treated as
        binary and the line-bounds check is skipped.
        """
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
        return len(text.splitlines())

