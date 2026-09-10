"""Change-scope validation for one SonarQube issue (T13).

:class:`ChangeScopeValidator` decides whether the files that actually changed
(the read-only :class:`git_diff.GitDiffResult` snapshot from T12) stay inside
the allowed scope of the single issue being processed (its
:class:`context.IssueContext` target file).

This is pure policy over already-collected facts: no Git, no subprocess, no
file writes. Path *normalization* is performed here, but the paths themselves
came from T12 (which reads them with NUL-separated Git output).

Scope boundary
--------------
T13 validates *which files changed*, never *whether the change is correct*.
It does not judge code quality, does not decide whether tests pass, and does
not decide whether the SonarQube issue is actually fixed. Verifying the fix
requires a new SonarQube analysis (T16-T19) and is deliberately absent here.
T13 never runs tests, commits, pushes, or modifies anything.

Default policy (conservative, single issue)
-------------------------------------------
For one issue only the issue's target file (``IssueContext.file_path``) may
change. Any changed file outside that set - tracked or untracked, in any
number - fails validation and is reported in ``unexpected_files``. Nothing is
ever silently ignored.

Security notes
--------------
Changed paths are treated as untrusted. Entries that are absolute, empty, or
contain a ``..`` component can never be a valid repository-relative path, so
they are reported as unexpected (with an explicit reason) and always fail
validation. Case is folded only on case-insensitive platforms (Windows), so a
wrongly-cased path is not accepted on case-sensitive file systems.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from context import IssueContext
from git_diff import GitDiffResult

PathLike = Union[str, Path]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ChangeScopeError(Exception):
    """Raised when scope validation cannot be performed (caller error)."""


def _is_case_insensitive_platform() -> bool:
    """True on platforms whose native file systems ignore path case."""
    return os.name == "nt"


def _comparison_key(path: str) -> str:
    """Comparison key for a normalized repository-relative path.

    Case is folded only where the platform treats paths case-insensitively,
    so a case mismatch is rejected on case-sensitive file systems.
    """
    return path.casefold() if _is_case_insensitive_platform() else path


def _parse_relative_path(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """Interpret ``raw`` as a repository-relative path.

    Returns:
        ``(normalized, None)`` when ``raw`` is a usable relative path, or
        ``(None, problem)`` where ``problem`` explains why it is not (empty,
        absolute, the repository root, or ``..`` traversal).
    """
    value = "" if raw is None else str(raw).strip()
    if not value:
        return None, "path is empty"
    value = value.replace("\\", "/")
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        return None, "absolute paths are not allowed"
    parts: List[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None, "path traversal ('..') is not allowed"
        parts.append(part)
    if not parts:
        return None, "path points at the repository root, not a file"
    return "/".join(parts), None


def normalise_relative_path(raw: object) -> Optional[str]:
    """Return the normalized repository-relative form of ``raw``, or ``None``.

    Backslashes become ``/``; empty/``.`` components and duplicate separators
    collapse. ``None`` is returned for empty, absolute, root-level, or
    traversal entries, because those can never be repository-relative paths.
    """
    normalized, _ = _parse_relative_path(raw)
    return normalized


def _repository_key(repository_path: object) -> str:
    """Comparable key for a repository path (absolute, platform-normalized)."""
    return os.path.normcase(os.path.normpath(str(repository_path)))


@dataclass(frozen=True)
class ChangeScopeResult:
    """Structured outcome of T13 change-scope validation.

    Attributes:
        is_valid: True when no changed file falls outside the expected scope.
        expected_files: normalized repository-relative files allowed to change
            (the single issue target file for the default policy).
        changed_files: normalized, de-duplicated, sorted repository-relative
            files reported by T12.
        unexpected_files: sorted changed entries outside the expected scope.
            Malformed entries (absolute / traversal) are reported verbatim
            because they cannot be normalized.
        has_changes: the diff snapshot reported at least one changed file.
        expected_file_modified: at least one expected file really changed.
        reasons: ordered, human-readable explanations of the outcome.
    """

    is_valid: bool
    expected_files: Tuple[str, ...]
    changed_files: Tuple[str, ...]
    unexpected_files: Tuple[str, ...]
    has_changes: bool
    expected_file_modified: bool
    reasons: Tuple[str, ...]

    @property
    def reason(self) -> str:
        """The primary (first) human-readable reason for the outcome."""
        return self.reasons[0] if self.reasons else ""

    @property
    def reason_text(self) -> str:
        """All reasons joined into a single readable paragraph."""
        return " ".join(self.reasons)

    def as_dict(self) -> dict:
        """Plain, JSON-friendly summary for logging/reporting."""
        return {
            "is_valid": self.is_valid,
            "expected_files": list(self.expected_files),
            "changed_files": list(self.changed_files),
            "unexpected_files": list(self.unexpected_files),
            "has_changes": self.has_changes,
            "expected_file_modified": self.expected_file_modified,
            "reasons": list(self.reasons),
        }


class ChangeScopeValidator:
    """Apply the single-issue target-file scope policy (T13).

    The validator is stateless and pure: identical inputs always produce an
    identical :class:`ChangeScopeResult`.
    """

    def validate(
        self,
        context: IssueContext,
        diff_result: GitDiffResult,
    ) -> ChangeScopeResult:
        """Validate the T12 diff against the T08 issue context.

        Args:
            context: verified issue context whose ``file_path`` is the one
                file allowed to change.
            diff_result: read-only working-tree snapshot produced by T12.

        Returns:
            A :class:`ChangeScopeResult` describing files inside and outside
            the expected scope. Scope failures are represented in the result,
            not raised.

        Raises:
            ChangeScopeError: when the inputs are inconsistent (the diff was
                taken from a different repository, or the context file path is
                not a safe repository-relative path).
        """
        if _repository_key(context.repository_path) != _repository_key(
            diff_result.repository_path
        ):
            raise ChangeScopeError(
                "Change-scope validation received a diff for a different "
                f"repository ('{diff_result.repository_path}') than the issue "
                f"context ('{context.repository_path}')."
            )

        expected_norm, expected_problem = _parse_relative_path(context.file_path)
        if expected_norm is None:
            raise ChangeScopeError(
                "The issue context target file is not a safe "
                f"repository-relative path ({expected_problem})."
            )

        expected_files: Tuple[str, ...] = (expected_norm,)
        expected_keys = {_comparison_key(expected_norm)}

        changed: List[str] = []
        unexpected: List[str] = []
        reasons: List[str] = [
            f"Scope policy: only '{expected_norm}' may change for this issue."
        ]

        seen_keys = set()
        for raw in diff_result.changed_files:
            normalized, problem = _parse_relative_path(raw)
            if normalized is None:
                display = str(raw).strip()
                unexpected.append(display)
                reasons.append(
                    f"Changed path {display!r} is not a valid "
                    f"repository-relative path ({problem}); it fails scope "
                    "validation."
                )
                continue
            key = _comparison_key(normalized)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            changed.append(normalized)
            if key not in expected_keys:
                unexpected.append(normalized)
                reasons.append(
                    f"Unexpected changed file: '{normalized}' is outside the "
                    "allowed scope for this issue."
                )

        changed.sort()
        unexpected = sorted(set(unexpected))
        is_valid = not unexpected
        expected_file_modified = any(
            _comparison_key(path) in expected_keys for path in changed
        )

        if not diff_result.changed_files:
            reasons.append(
                "No files changed; nothing falls outside the allowed scope "
                "(whether the issue is fixed is decided by later analysis)."
            )
        elif is_valid:
            reasons.append(
                "All changed files are inside the allowed scope for this "
                "issue."
            )
        else:
            reasons.append(
                "Change scope validation failed: "
                f"{len(unexpected)} unexpected file(s) changed."
            )

        return ChangeScopeResult(
            is_valid=is_valid,
            expected_files=expected_files,
            changed_files=tuple(changed),
            unexpected_files=tuple(unexpected),
            has_changes=len(diff_result.changed_files) > 0,
            expected_file_modified=expected_file_modified,
            reasons=tuple(reasons),
        )


__all__: Sequence[str] = (
    "ChangeScopeError",
    "ChangeScopeResult",
    "ChangeScopeValidator",
    "normalise_relative_path",
)
