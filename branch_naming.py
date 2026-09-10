"""Git branch naming for AI-fixer agent branches (T07).

Responsibility boundary
-----------------------
Pure naming logic. This module does not execute Git or touch the filesystem;
it only maps inputs (an issue key and an optional timestamp) onto a valid,
deterministic agent-branch name, and validates arbitrary branch names.

Naming strategy (deterministic and documented)
----------------------------------------------
* Prefix (constant):    ``ai/sonar-fix``
* Base agent branch:    ``ai/sonar-fix/<slug-of-issue-key>``
* Optional uniqueness:  ``ai/sonar-fix/<slug-of-issue-key>-<slug-of-timestamp>``
  (the ``timestamp`` argument is optional; production flows that may process
  the same issue more than once should pass a timestamp so repeated runs never
  accidentally collide)

``_slugify`` keeps only ``[A-Za-z0-9_-]`` and collapses separators, so the
resulting branch name never contains characters that are unsafe in Git refs.
"""

from datetime import datetime, timezone
from typing import Optional, Pattern
import re

#: Prefix shared by every AI-agent branch created by this POC.
AGENT_BRANCH_PREFIX = "ai/sonar-fix"

#: Characters that are unsafe in a Git ref are replaced during slugging.
_UNSAFE_CHARS: Pattern[str] = re.compile(r"[^A-Za-z0-9_-]")
_MULTI_DASH: Pattern[str] = re.compile(r"-{2,}")

#: Characters forbidden anywhere in a Git ref name (git-check-ref-format(1)).
_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")
_CONTROL_CHARS = frozenset(
    chr(code) for code in range(32)
) | frozenset(chr(127))


class BranchNameError(ValueError):
    """Raised when a branch name is empty or violates Git ref rules."""


def validate_branch_name(name: str) -> None:
    """Raise :class:`BranchNameError` when ``name`` is not a valid Git branch.

    The checks mirror the core ``git check-ref-format`` rules plus a guard
    against option injection (a branch name must not start with ``-``).
    """
    if not isinstance(name, str):
        raise BranchNameError("Branch name must be a string.")
    if not name:
        raise BranchNameError("Branch name must not be empty.")
    if name == "HEAD":
        raise BranchNameError("'HEAD' is reserved and cannot be a branch name.")
    if name == "@":
        raise BranchNameError("'@' is not a valid branch name.")
    if name.startswith("-"):
        raise BranchNameError("Branch name must not start with '-'.")
    if name.startswith("/") or name.endswith("/"):
        raise BranchNameError("Branch name must not start or end with '/'.")
    if name.endswith("."):
        raise BranchNameError("Branch name must not end with '.'.")
    if "//" in name or ".." in name or "@{" in name:
        raise BranchNameError(
            "Branch name must not contain '//', '..' or '@{'."
        )
    if any(part in ("", ".", "..", "@") for part in name.split("/")):
        raise BranchNameError(
            "Branch name contains an empty or reserved component."
        )
    if any(part.startswith(".") for part in name.split("/")):
        raise BranchNameError(
            "Branch name components must not begin with a dot."
        )
    if any(part.endswith(".lock") for part in name.split("/")):
        raise BranchNameError(
            "Branch name components must not end with '.lock'."
        )
    for char in name:
        if char in _FORBIDDEN_CHARS or char in _CONTROL_CHARS:
            raise BranchNameError(
                f"Branch name contains forbidden character {char!r}."
            )


def is_valid_branch_name(name: str) -> bool:
    """Return True when ``name`` is a valid Git branch name."""
    try:
        validate_branch_name(name)
    except BranchNameError:
        return False
    return True


def _slugify(value: str, *, what: str) -> str:
    """Map ``value`` onto a Git-safe component.

    Unsafe characters become ``-``, repeated dashes collapse, and leading /
    trailing dashes are trimmed. A value that produces no usable characters
    raises :class:`BranchNameError` instead of silently returning empty.
    """
    slug = _UNSAFE_CHARS.sub("-", value)
    slug = _MULTI_DASH.sub("-", slug).strip("-")
    if not slug:
        raise BranchNameError(
            f"{what} contains no characters usable in a Git branch name."
        )
    return slug


def make_agent_branch_name(
    issue_key: str, timestamp: Optional[str] = None
) -> str:
    """Build the deterministic agent-branch name for ``issue_key``.

    Args:
        issue_key: SonarQube issue key, e.g. ``AX1abcDefG``.
        timestamp: optional compact UTC timestamp (see
            :func:`utc_compact_timestamp`). When provided it is appended so
            that repeated runs against the same issue produce distinct names.

    Returns:
        A validated branch name of the form ``ai/sonar-fix/<slug>`` or
        ``ai/sonar-fix/<slug>-<timestamp>``.

    Raises:
        BranchNameError: if the issue key is empty or unusable.
    """
    key = str(issue_key)
    if not key.strip():
        raise BranchNameError("Issue key must not be empty.")
    slug = _slugify(key, what="Issue key")
    name = f"{AGENT_BRANCH_PREFIX}/{slug}"
    if timestamp is not None:
        timestamp_slug = _slugify(str(timestamp), what="Timestamp")
        name = f"{name}-{timestamp_slug}"
    validate_branch_name(name)
    return name


def utc_compact_timestamp(now: Optional[datetime] = None) -> str:
    """Return a compact UTC timestamp safe for branch-name suffixes.

    Example: ``20260909-101530`` (``YYYYmmdd-HHMMSS``).
    """
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%d-%H%M%S")
