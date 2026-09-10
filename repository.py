"""Git repository operations for the SonarQube AI-fixer POC (T05-T07).

Responsibility boundaries
-------------------------
* T05 - clone a Git repository into a local working directory.
* T06 - check out the source branch where an issue lives.
* T07 - create an isolated AI-agent branch from the verified source branch.

This module talks to the system ``git`` executable through ``subprocess``
using argument lists (never ``shell=True``). All Git execution is isolated
here; the rest of the application never runs Git directly.

Security notes
--------------
* Remote URLs may embed credentials (``https://user:token@host/...``).
  Every error message produced here is scrubbed with
  :func:`redact_credentials` so tokens never reach logs/exceptions.
* Branch names are validated before being passed to Git
  (:func:`branch_naming.validate_branch_name`) to prevent option injection
  and Git-ref violations.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from branch_naming import BranchNameError, validate_branch_name

PathLike = Union[str, Path]


def redact_credentials(text: str) -> str:
    """Remove URL userinfo (``user`` / ``user:password``) from ``text``.

    Git error messages echo the remote URL, so credentials embedded in a URL
    must never appear in exceptions or logs. Two shapes are handled:

    * Scheme URLs:  ``https://user:pass@host/...``
    * SCP-like remotes: ``user@host:path/to/repo.git``
    """
    if not text:
        return ""
    scrubbed = re.sub(r"(?<=://)[^/@\s]+@", "***@", text)
    scrubbed = re.sub(
        r"(?<![A-Za-z0-9._@-])([A-Za-z0-9._-]+)(?::[^/@\s]*)?@",
        "***@",
        scrubbed,
    )
    return scrubbed


class RepositoryError(Exception):
    """Raised when a repository/Git operation fails or cannot be verified."""


class RepositoryManager:
    """Clone/checkout/branch operations executed with the system Git.

    Args:
        git_executable: name or path of the Git executable (default ``"git"``).
        timeout_seconds: per-command timeout in seconds.
    """

    def __init__(
        self, git_executable: str = "git", timeout_seconds: float = 300.0
    ) -> None:
        self._git_executable = git_executable
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clone(self, repository_url: str, destination: PathLike) -> Path:
        """Clone ``repository_url`` into ``destination``.

        Behavior:
            * The parent directory of ``destination`` must already exist
              (Git creates the destination itself).
            * An existing *empty* ``destination`` is accepted (Git clones
              into it). An existing non-empty ``destination`` fails with a
              clear error; nothing is deleted or overwritten.

        Returns:
            The absolute path of the cloned repository.

        Raises:
            RepositoryError: empty URL or the Git clone command failed.
        """
        url = str(repository_url or "").strip()
        if not url:
            raise RepositoryError("Repository URL must not be empty.")
        destination_path = Path(destination).expanduser()
        self._run_git(
            ["clone", "--", url, str(destination_path)], cwd=None
        )
        return destination_path.absolute()

    def checkout_source_branch(
        self, repository_path: PathLike, branch_name: str
    ) -> str:
        """Check out ``branch_name`` in the repository at ``repository_path``.

        The branch must exist as a *local* branch (``refs/heads/...``).
        Remote-tracking names such as ``origin/main`` are deliberately not
        auto-created, so this method never silently checks out another branch.

        Returns:
            The name of the branch that is checked out afterwards.

        Raises:
            RepositoryError: path/repository problems, invalid or missing
                branch, checkout failure, or verification failure.
        """
        name = self._validated_branch_name(branch_name, role="branch")
        repo = self._ensure_repository(repository_path)
        if not self._branch_exists(repo, name):
            raise RepositoryError(
                f"Branch '{name}' does not exist in repository '{repo}'."
            )
        self._run_git(["-C", str(repo), "checkout", name], cwd=None)
        current = self.current_branch(repo)
        if current != name:
            raise RepositoryError(
                f"Checkout of branch '{name}' could not be verified "
                f"(current branch is {current!r})."
            )
        return current

    def create_agent_branch(
        self,
        repository_path: PathLike,
        agent_branch_name: str,
        expected_source_branch: str,
    ) -> str:
        """Create ``agent_branch_name`` from the checked-out source branch.

        Preconditions verified before anything is modified:
            * ``expected_source_branch`` is currently checked out.
            * ``agent_branch_name`` does not already exist (a collision
              aborts instead of silently reusing an existing branch).

        The new branch is created with ``git checkout -b`` from the current
        HEAD, i.e. exactly from the verified source branch, and the result is
        verified afterwards.

        Returns:
            The created agent branch name.

        Raises:
            RepositoryError: on any precondition or Git failure.
        """
        name = self._validated_branch_name(
            agent_branch_name, role="agent branch"
        )
        expected = self._validated_branch_name(
            expected_source_branch, role="source branch"
        )
        repo = self._ensure_repository(repository_path)
        current = self.current_branch(repo)
        if current != expected:
            raise RepositoryError(
                f"Cannot create agent branch '{name}': expected source "
                f"branch '{expected}' to be checked out, but current branch "
                f"is {current!r}."
            )
        if self._branch_exists(repo, name):
            raise RepositoryError(
                f"Agent branch '{name}' already exists in repository "
                f"'{repo}'. Aborting instead of reusing an existing branch; "
                f"use a unique branch name (e.g. include a timestamp)."
            )
        self._run_git(
            ["-C", str(repo), "checkout", "-b", name], cwd=None
        )
        if self.current_branch(repo) != name:
            raise RepositoryError(
                f"Agent branch '{name}' was created but could not be "
                f"verified as the checked-out branch."
            )
        return name

    def current_branch(self, repository_path: PathLike) -> Optional[str]:
        """Return the checked-out local branch name, or None when detached.

        Raises:
            RepositoryError: if the path does not exist or is not a Git repo.
        """
        repo = self._ensure_repository(repository_path)
        stdout = self._run_git(
            ["-C", str(repo), "branch", "--show-current"], cwd=None
        )
        return stdout or None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_git(self, args: List[str], cwd: Optional[Path]) -> str:
        """Run Git with an argument list; return stripped stdout on success.

        Raises:
            RepositoryError: Git is missing, the command timed out, or Git
                exited non-zero (with credentials redacted from the details).
        """
        try:
            completed = subprocess.run(
                [self._git_executable, *args],
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepositoryError(
                "Git command timed out after "
                f"{self._timeout_seconds} seconds."
            ) from exc
        except OSError as exc:
            raise RepositoryError(
                f"Failed to run Git executable "
                f"{self._git_executable!r}: {exc}"
            ) from exc
        if completed.returncode != 0:
            details = redact_credentials(
                (completed.stderr or completed.stdout or "").strip()
            )
            raise RepositoryError(
                "Git command failed with exit code "
                f"{completed.returncode}: {details}"
            )
        return (completed.stdout or "").strip()

    def _git_ok(self, args: List[str], cwd: Optional[Path]) -> bool:
        """Return True when the Git command exits 0 (best-effort probe)."""
        try:
            completed = subprocess.run(
                [self._git_executable, *args],
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def _validated_branch_name(self, raw_name: object, *, role: str) -> str:
        name = raw_name if isinstance(raw_name, str) else str(raw_name)
        try:
            validate_branch_name(name)
        except BranchNameError as exc:
            raise RepositoryError(
                f"Invalid {role} name: {exc}"
            ) from exc
        return name

    def _ensure_repository(self, repository_path: PathLike) -> Path:
        repo = Path(repository_path).expanduser()
        if not repo.exists():
            raise RepositoryError(
                f"Repository path does not exist: '{repo}'."
            )
        if not repo.is_dir():
            raise RepositoryError(
                f"Repository path is not a directory: '{repo}'."
            )
        if not (repo / ".git").exists():
            raise RepositoryError(
                f"'{repo}' is not a Git repository (missing '.git')."
            )
        return repo

    def _branch_exists(self, repo: Path, branch_name: str) -> bool:
        """Return True when a local branch ``refs/heads/<branch_name>`` exists."""
        return self._git_ok(
            [
                "-C",
                str(repo),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch_name}",
            ],
            cwd=None,
        )
