"""Read-only Git working-tree baseline capture and change attribution (pre-T20).

Why this module exists
----------------------
T13 can tell that the working tree changed, but it cannot tell whether a change
to the issue's file was made *by Codex* or was already there before Codex ran. A
future T20 (Git commit) must never attribute a pre-existing edit to the agent, so
this module provides the missing primitive: capture the working-tree state
**before** Codex executes, capture it **again afterwards**, and compute exactly
which changes were introduced by the run.

Baseline identity is derived from reliable Git state - never from timestamps:

* the porcelain status of every path (staged vs unstaged vs untracked, added,
  modified, deleted, renamed),
* a content fingerprint per changed path (``git diff HEAD`` for tracked paths,
  ``git hash-object`` for untracked ones).

Attribution rules (deterministic, conservative)
----------------------------------------------
============================ ============================================
Situation                    Attribution
============================ ============================================
changed after, not before    ``agent_files`` (introduced by the run)
changed before and after,    ``pre_existing_and_changed_files`` - *never*
fingerprint differs          attributed to the agent
changed before and after,    ``pre_existing_files`` (untouched by the run)
fingerprint identical
changed before, gone after   ``pre_existing_and_changed_files``
============================ ============================================

``BaselineAttribution.attributable`` is ``True`` only when the baseline was
clean and no pre-existing change was touched: that is the condition a future T20
must require before it may stage anything.

Safety properties (why this is safe to introduce now)
----------------------------------------------------
* This module is a **safety primitive only**. It never commits, never pushes,
  never creates a PR, never stages files, never resets or cleans the working
  tree, and never deletes or rewrites a user file.
* It only runs read-only Git commands (``rev-parse``, ``status``, ``diff``,
  ``hash-object`` without ``-w``), always as argument arrays - never
  ``shell=True``, never a shell string.
* Paths and file lists are read with the ``-z`` NUL separator, so paths with
  spaces, quotes or newlines cannot be misparsed.
* Git error output is passed through ``redact_credentials`` before it reaches an
  exception message, and the process runner is injectable so tests never need a
  real Codex run.

How T20 is expected to use it
-----------------------------
1. ``baseline = inspector.capture(repo)`` immediately before T10 runs Codex.
2. ``after = inspector.capture(repo)`` after Codex finished (next to T12).
3. ``attribution = attribute_changes(baseline, after)``.
4. T20 must require ``attribution.attributable`` and stage exactly
   ``attribution.agent_files`` - never ``after.changed_files`` - after its other
   gates (FIXED status, no unresolved review condition, Codex uncertainty gate,
   secret scan) have passed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from repository import redact_credentials

PathLike = Union[str, Path]

#: Status codes that mark an entry as changed in the index ("staged").
_STAGED_CODES = frozenset({"M", "A", "D", "R", "C", "T"})
#: Status codes that mark an entry as changed in the working tree.
_UNSTAGED_CODES = frozenset({"M", "D", "T"})
#: Porcelain v1 status for an untracked entry.
_UNTRACKED = "??"


class WorktreeBaselineError(Exception):
    """Raised when a baseline cannot be captured or attributed."""


@dataclass(frozen=True)
class WorktreeSnapshot:
    """Read-only snapshot of a working tree at one point in time.

    Attributes:
        repository_path: absolute path that was inspected.
        head_commit: ``HEAD`` commit id, or ``None`` when it could not be read.
        is_clean: no staged, unstaged or untracked changes at all.
        changed_files: sorted repository-relative paths of every changed entry.
        tracked_modified_files: paths modified in the working tree (unstaged).
        staged_files: paths changed in the index.
        untracked_files: paths Git does not track.
        deleted_files: paths reported as deleted (staged or unstaged).
        renamed_files: ``(new_path, original_path)`` pairs, sorted.
        fingerprints: per-path content fingerprint (``"worktree:<sha256>"`` for
            tracked changes, ``"blob:<object id>"`` for untracked files). A path
            without a fingerprint is still reported through the status lists.
    """

    __test__ = False

    repository_path: Path
    head_commit: Optional[str]
    is_clean: bool
    changed_files: Tuple[str, ...]
    tracked_modified_files: Tuple[str, ...]
    staged_files: Tuple[str, ...]
    untracked_files: Tuple[str, ...]
    deleted_files: Tuple[str, ...]
    renamed_files: Tuple[Tuple[str, str], ...]
    fingerprints: Mapping[str, str] = field(default_factory=dict)

    @property
    def changed_set(self) -> frozenset:
        """The changed paths as a set (convenience for comparisons)."""
        return frozenset(self.changed_files)

    def fingerprint_of(self, path: str) -> Optional[str]:
        """The recorded fingerprint for ``path``, or ``None`` when unknown."""
        return self.fingerprints.get(path)

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "repository_path": str(self.repository_path),
            "head_commit": self.head_commit,
            "is_clean": self.is_clean,
            "changed_files": list(self.changed_files),
            "tracked_modified_files": list(self.tracked_modified_files),
            "staged_files": list(self.staged_files),
            "untracked_files": list(self.untracked_files),
            "deleted_files": list(self.deleted_files),
            "renamed_files": [list(pair) for pair in self.renamed_files],
        }


@dataclass(frozen=True)
class BaselineAttribution:
    """Which working-tree changes belong to the run and which do not.

    Attributes:
        repository_path: the repository both snapshots describe.
        clean_baseline: the working tree was clean before the run.
        agent_files: paths whose change exists only after the run.
        agent_untracked_files: subset of ``agent_files`` Git does not track.
        agent_deleted_files: subset of ``agent_files`` that are deletions.
        agent_renamed_files: renames introduced by the run.
        pre_existing_files: pre-existing changes the run did not touch.
        pre_existing_and_changed_files: pre-existing paths whose state changed
            after the run (or whose change vanished) - never attributed.
        blocked_reasons: why a commit must not proceed on this attribution.
        attributable: no ``blocked_reasons``; the condition T20 must require.
    """

    __test__ = False

    repository_path: Path
    clean_baseline: bool
    agent_files: Tuple[str, ...]
    agent_untracked_files: Tuple[str, ...]
    agent_deleted_files: Tuple[str, ...]
    agent_renamed_files: Tuple[Tuple[str, str], ...]
    pre_existing_files: Tuple[str, ...]
    pre_existing_and_changed_files: Tuple[str, ...]
    blocked_reasons: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    @property
    def attributable(self) -> bool:
        """True only when no blocking reason was recorded."""
        return not self.blocked_reasons

    @property
    def has_agent_changes(self) -> bool:
        """True when the run introduced at least one change."""
        return bool(self.agent_files)

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "repository_path": str(self.repository_path),
            "clean_baseline": self.clean_baseline,
            "agent_files": list(self.agent_files),
            "agent_untracked_files": list(self.agent_untracked_files),
            "agent_deleted_files": list(self.agent_deleted_files),
            "agent_renamed_files": [list(pair) for pair in self.agent_renamed_files],
            "pre_existing_files": list(self.pre_existing_files),
            "pre_existing_and_changed_files": list(
                self.pre_existing_and_changed_files
            ),
            "blocked_reasons": list(self.blocked_reasons),
            "attributable": self.attributable,
            "has_agent_changes": self.has_agent_changes,
        }


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


def _parse_porcelain_z(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """Parse ``git status --porcelain=v1 -z`` into ``(xy, path, origin)`` tuples.

    In ``-z`` form a rename/copy entry is followed by the original path as a
    separate NUL-terminated field.
    """
    fields = str(text or "").split("\0")
    entries: List[Tuple[str, str, Optional[str]]] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        if len(raw) < 4:
            entries.append((raw[:2], raw[2:].lstrip(), None))
            continue
        xy = raw[:2]
        path = raw[3:] if raw[2] == " " else raw[2:].lstrip()
        origin: Optional[str] = None
        if "R" in xy or "C" in xy:
            if index < len(fields) and fields[index] != "":
                origin = fields[index]
                index += 1
        entries.append((xy, path, origin))
    return entries


class WorktreeBaselineInspector:
    """Capture read-only working-tree baselines through the system Git.

    Args:
        git_command: Git executable name or path (default ``"git"``).
        timeout_seconds: per-command timeout.
        runner: optional injectable process runner.
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture(self, repository_path: PathLike) -> WorktreeSnapshot:
        """Snapshot the working tree of ``repository_path``.

        Returns:
            A :class:`WorktreeSnapshot`. Nothing is staged, committed, reset,
            cleaned, deleted or otherwise modified.

        Raises:
            WorktreeBaselineError: the path is missing / is not a Git working
                copy, or a Git command fails, times out, or cannot be launched.
        """
        repository = Path(repository_path).expanduser()
        if not repository.is_dir():
            raise WorktreeBaselineError(
                f"Repository path is not an existing directory: '{repository}'."
            )
        repository = repository.resolve()

        head_commit = self._head_commit(repository)
        porcelain = self._run(repository, "status", "--porcelain=v1", "-z",
                              "--untracked-files=all")
        text = str(porcelain.stdout or "")
        if text.strip("\0") == "":
            return WorktreeSnapshot(
                repository_path=repository,
                head_commit=head_commit,
                is_clean=True,
                changed_files=(),
                tracked_modified_files=(),
                staged_files=(),
                untracked_files=(),
                deleted_files=(),
                renamed_files=(),
                fingerprints={},
            )

        staged: set = set()
        modified: set = set()
        untracked: set = set()
        deleted: set = set()
        renamed: List[Tuple[str, str]] = []
        changed: set = set()

        for xy, path, origin in _parse_porcelain_z(text):
            if not path:
                continue
            changed.add(path)
            if xy == _UNTRACKED:
                untracked.add(path)
                continue
            index_code, worktree_code = xy[0], xy[1:]
            if index_code in _STAGED_CODES:
                staged.add(path)
            if worktree_code in _UNSTAGED_CODES:
                modified.add(path)
            if index_code == "D" or worktree_code == "D":
                deleted.add(path)
            if origin is not None:
                renamed.append((path, origin))

        changed_files = tuple(sorted(changed))
        fingerprints = self._fingerprints(repository, changed_files, untracked)

        return WorktreeSnapshot(
            repository_path=repository,
            head_commit=head_commit,
            is_clean=not changed_files,
            changed_files=changed_files,
            tracked_modified_files=tuple(sorted(modified)),
            staged_files=tuple(sorted(staged)),
            untracked_files=tuple(sorted(untracked)),
            deleted_files=tuple(sorted(deleted)),
            renamed_files=tuple(sorted(renamed)),
            fingerprints=fingerprints,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _head_commit(self, repository: Path) -> Optional[str]:
        """Return the current ``HEAD`` commit id, or ``None`` when unreadable."""
        completed = self._run_raw(repository, "rev-parse", "HEAD")
        if completed is None or int(completed.returncode) != 0:
            return None
        value = str(completed.stdout or "").strip()
        return value or None

    def _fingerprints(
        self,
        repository: Path,
        changed_files: Sequence[str],
        untracked: Sequence[str],
    ) -> Dict[str, str]:
        """Fingerprint every changed path from reliable Git state.

        Tracked paths are fingerprinted from ``git diff HEAD -- <path>`` (the
        index and the working tree compared with ``HEAD``); untracked paths are
        fingerprinted with ``git hash-object`` (no object is written).
        """
        untracked_set = set(untracked)
        fingerprints: Dict[str, str] = {}
        for path in changed_files:
            if path in untracked_set:
                completed = self._run_raw(repository, "hash-object", "--", path)
                if completed is None or int(completed.returncode) != 0:
                    continue
                value = str(completed.stdout or "").strip()
                if value:
                    fingerprints[path] = f"blob:{value}"
                continue
            completed = self._run_raw(
                repository,
                "diff",
                "HEAD",
                "--no-color",
                "--no-ext-diff",
                "-U0",
                "--",
                path,
            )
            if completed is None or int(completed.returncode) != 0:
                continue
            digest = hashlib.sha256(
                str(completed.stdout or "").encode("utf-8", "replace")
            ).hexdigest()
            fingerprints[path] = f"worktree:{digest}"
        return fingerprints

    def _run(self, repository: Path, *args: str) -> object:
        completed = self._run_raw(repository, *args)
        if completed is None:  # pragma: no cover - defensive
            raise WorktreeBaselineError(
                f"Failed to run Git executable '{self._git_command}'."
            )
        if int(completed.returncode) != 0:
            detail = redact_credentials(str(completed.stderr or "").strip())
            if not detail:
                detail = f"git exited with code {completed.returncode}."
            raise WorktreeBaselineError(f"Git command failed: {detail}")
        return completed

    def _run_raw(
        self, repository: Path, *args: str
    ) -> Optional[object]:
        """Run Git read-only; return ``None`` when it could not be launched."""
        command = [self._git_command, "-C", str(repository), *args]
        try:
            return self._runner(command, str(repository), self._timeout_seconds)
        except FileNotFoundError as exc:
            raise WorktreeBaselineError(
                f"Git executable not found (configured '{self._git_command}'): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeBaselineError(
                f"Git inspection timed out after {self._timeout_seconds:g} seconds."
            ) from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise WorktreeBaselineError(
                redact_credentials(f"Failed to run git: {exc}")
            ) from exc


def _repository_key(repository_path: object) -> str:
    """Comparable key for a repository path (absolute, platform-normalized)."""
    return os.path.normcase(os.path.normpath(str(repository_path)))


def attribute_changes(
    baseline: WorktreeSnapshot, after: WorktreeSnapshot
) -> BaselineAttribution:
    """Decide which post-run changes were introduced by the run itself.

    Pure and deterministic: no Git, no filesystem access, no I/O. A path that
    was already changed before the run is never credited to the run, even when
    the run modified it further - that case is reported as
    ``pre_existing_and_changed_files`` and blocks the attribution.

    Args:
        baseline: snapshot taken before the run.
        after: snapshot taken after the run.

    Returns:
        A typed :class:`BaselineAttribution`.

    Raises:
        WorktreeBaselineError: the two snapshots describe different repositories.
    """
    if _repository_key(baseline.repository_path) != _repository_key(
        after.repository_path
    ):
        raise WorktreeBaselineError(
            "Baseline attribution received snapshots for different "
            f"repositories ('{baseline.repository_path}' and "
            f"'{after.repository_path}')."
        )

    baseline_paths = set(baseline.changed_files)
    after_paths = set(after.changed_files)

    introduced = sorted(after_paths - baseline_paths)
    both = sorted(after_paths & baseline_paths)

    untouched_pre_existing = [
        path
        for path in both
        if baseline.fingerprint_of(path) == after.fingerprint_of(path)
    ]
    touched_pre_existing = sorted(
        set(path for path in both if path not in untouched_pre_existing)
        | (baseline_paths - after_paths)
    )

    agent_untracked = sorted(p for p in introduced if p in after.untracked_files)
    agent_deleted = sorted(p for p in introduced if p in after.deleted_files)
    agent_renamed = tuple(
        sorted(pair for pair in after.renamed_files if pair not in baseline.renamed_files)
    )

    blocked: List[str] = []
    reasons: List[str] = [
        f"The baseline contained {len(baseline_paths)} pre-existing change(s) "
        f"and the run left {len(introduced)} new change(s)."
    ]
    if not baseline.is_clean:
        blocked.append(
            "The working tree was not clean before the run "
            f"({len(baseline_paths)} pre-existing change(s)), so a clean "
            "baseline cannot be guaranteed."
        )
    if touched_pre_existing:
        blocked.append(
            f"{len(touched_pre_existing)} path(s) were already changed before "
            "the run and changed again during it, so those changes cannot be "
            "attributed to the run: "
            + ", ".join(repr(path) for path in touched_pre_existing)
            + "."
        )
    if not blocked:
        reasons.append(
            "Every change in the working tree was introduced by the run and "
            "the baseline was clean."
        )

    return BaselineAttribution(
        repository_path=after.repository_path,
        clean_baseline=baseline.is_clean,
        agent_files=tuple(introduced),
        agent_untracked_files=tuple(agent_untracked),
        agent_deleted_files=tuple(agent_deleted),
        agent_renamed_files=agent_renamed,
        pre_existing_files=tuple(sorted(untouched_pre_existing)),
        pre_existing_and_changed_files=tuple(touched_pre_existing),
        blocked_reasons=tuple(blocked),
        reasons=tuple(reasons),
    )


__all__: Sequence[str] = (
    "BaselineAttribution",
    "GitRunner",
    "WorktreeBaselineError",
    "WorktreeBaselineInspector",
    "WorktreeSnapshot",
    "attribute_changes",
)
