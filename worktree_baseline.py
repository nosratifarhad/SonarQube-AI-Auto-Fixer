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

T20 additions (still read-only)
-------------------------------
5. ``capture(repo, include_ignored=True)`` additionally records
   ``ignored_files``. An AI agent can create an ignored file that never shows up
   in an ordinary untracked-file listing, so T20 compares the baseline ignored
   set against the post-run ignored set and refuses on an unexpected addition.
6. ``content_id_of(path)`` / ``index_equivalent_blobs`` expose the **Git**
   content identity of a worktree path:
   ``git hash-object --path=<relative-path> -- <relative-path>``. That command
   applies the same ``.gitattributes`` / clean filters / CRLF normalisation that
   ``git add`` applies, so the value equals the blob id the index will hold once
   the path is staged (``git ls-files -s -- <relative-path>``). Hashing raw
   worktree bytes is *not* Git identity and is never used for it.
7. All of the above stays read-only: ``rev-parse``, ``status``, ``diff``,
   ``hash-object`` (never with ``-w``), always argument arrays, never a shell.
"""

from __future__ import annotations

import hashlib
import os
import re
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
#: Porcelain v1 status for an ignored entry (only reported with ``--ignored``).
_IGNORED = "!!"
#: Windows drive prefix: a path like ``C:/x`` can never be repository-relative.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


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
        index_equivalent_blobs: per-path Git **content identity** - the blob id
            ``git hash-object --path=<path> -- <path>`` reports, i.e. exactly the
            object staging those worktree bytes would store. Paths that do not
            exist in the working tree (deletions) cannot be hashed and are
            therefore absent. This is Git identity, never a raw byte hash.
        ignored_files: repository-relative paths Git reports as ignored. Only
            populated for ``capture(..., include_ignored=True)``; an ignored file
            is reported here and never placed in ``changed_files``.
        ignored_scan: ``True`` only when ``include_ignored=True`` was requested,
            so an *empty* ignored set can never be confused with "the ignored
            set was never read". T20 fails closed when this is ``False``.
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
    index_equivalent_blobs: Mapping[str, str] = field(default_factory=dict)
    ignored_files: Tuple[str, ...] = ()
    ignored_scan: bool = False

    @property
    def changed_set(self) -> frozenset:
        """The changed paths as a set (convenience for comparisons)."""
        return frozenset(self.changed_files)

    def fingerprint_of(self, path: str) -> Optional[str]:
        """The recorded fingerprint for ``path``, or ``None`` when unknown."""
        return self.fingerprints.get(path)

    def content_id_of(self, path: str) -> Optional[str]:
        """Git content identity of ``path``, or ``None`` when it was not captured.

        The value is the blob id ``git hash-object --path=<path> -- <path>``
        returns, so it equals the index blob id once the path is staged and is
        directly comparable with ``git ls-files -s -- <path>``. ``None`` means
        "unknown", which callers must treat as a refusal, never as a match.
        """
        return self.index_equivalent_blobs.get(path)

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
            "index_equivalent_blobs": dict(self.index_equivalent_blobs),
            "ignored_files": list(self.ignored_files),
            "ignored_scan": self.ignored_scan,
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

    def capture(
        self,
        repository_path: PathLike,
        *,
        include_ignored: bool = False,
    ) -> WorktreeSnapshot:
        """Snapshot the working tree of ``repository_path``.

        Args:
            repository_path: existing Git working copy to inspect.
            include_ignored: additionally record the ignored-file set
                (``ignored_files``) and the Git content identity of every
                changed path that exists (``index_equivalent_blobs``). Ignored
                paths are *reported only* - they never enter ``changed_files``
                and nothing is ever staged. T20 requires this so an
                agent-created ignored file cannot slip past the baseline
                comparison. ``False`` (the default) keeps the previous
                behaviour for every T01-T19 caller.

        Returns:
            A :class:`WorktreeSnapshot`. Nothing is staged, committed, reset,
            cleaned, deleted or otherwise modified.

        Raises:
            WorktreeBaselineError: the path is missing / is not a Git working
                copy, or a Git command fails, times out, or cannot be launched.
                A failed ignored-file or content-identity read fails closed
                (never a silently empty result).
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
        ignored_files = (
            self._ignored_files(repository) if include_ignored else ()
        )
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
                index_equivalent_blobs={},
                ignored_files=ignored_files,
                ignored_scan=include_ignored,
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
        content_ids = self._content_ids(repository, changed_files)

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
            index_equivalent_blobs=content_ids,
            ignored_files=ignored_files,
            ignored_scan=include_ignored,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ignored_files(self, repository: Path) -> Tuple[str, ...]:
        """Return the sorted repository-relative paths Git reports as ignored.

        ``--ignored=matching`` lists individual ignored *files* (not just the
        containing directory), so an agent-created ignored file cannot hide
        behind a directory summary. The result is reported only - T20 never
        stages an ignored path, it refuses when one is approved.

        Raises:
            WorktreeBaselineError: Git could not produce the list (fail closed:
                "could not read" must never look like "nothing is ignored").
        """
        completed = self._run_raw(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if completed is None:
            raise WorktreeBaselineError(
                f"Failed to run Git executable '{self._git_command}'."
            )
        if int(completed.returncode) != 0:
            detail = redact_credentials(str(completed.stderr or "").strip())
            raise WorktreeBaselineError(
                "Failed to read the ignored-file set from Git"
                f"{': ' + detail if detail else '.'}"
            )
        ignored = set()
        for xy, path, _origin in _parse_porcelain_z(str(completed.stdout or "")):
            if xy == _IGNORED and path:
                ignored.add(path)
        return tuple(sorted(ignored))

    def _content_ids(
        self, repository: Path, changed_files: Sequence[str]
    ) -> Dict[str, str]:
        """Git content identity (blob id) for every changed path that exists.

        ``git hash-object --path=<path> -- <path>`` is used - never a raw byte
        hash - because ``--path`` makes Git apply exactly the ``.gitattributes``
        clean filters, CRLF normalisation and external filters it applies when
        the path is staged. The value therefore equals the blob id the index
        will hold after ``git add -- <path>``.

        Paths that do not exist in the working tree (deletions) are skipped:
        Git cannot hash a missing file and T20 refuses a deletion outright.

        Raises:
            WorktreeBaselineError: Git could not be launched, or could not hash
                an existing path (fail closed - an unreadable file must never
                silently lose its content identity).
        """
        content_ids: Dict[str, str] = {}
        for path in changed_files:
            if not _is_safe_relative_path(path):
                continue
            if not (repository / path).is_file():
                continue
            completed = self._run_raw(
                repository, "hash-object", f"--path={path}", "--", path
            )
            if completed is None:  # pragma: no cover - defensive
                raise WorktreeBaselineError(
                    f"Failed to run Git executable '{self._git_command}'."
                )
            if int(completed.returncode) != 0:
                detail = redact_credentials(str(completed.stderr or "").strip())
                raise WorktreeBaselineError(
                    f"Failed to compute the Git content identity of {path!r}"
                    f"{': ' + detail if detail else '.'}"
                )
            value = str(completed.stdout or "").strip()
            if value:
                content_ids[path] = value
        return content_ids

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


def _is_safe_relative_path(path: object) -> bool:
    """True when ``path`` is a plain repository-relative path.

    Git reports repository-relative paths, but they are still checked before
    they are used to touch the filesystem: absolute paths (POSIX, Windows drive
    or UNC), ``..`` components, empty components, ``.`` and NUL can never be a
    safe repository-relative path.
    """
    value = "" if path is None else str(path)
    if not value or "\0" in value:
        return False
    if value.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(value):
        return False
    parts = [part for part in value.replace("\\", "/").split("/") if part != ""]
    if not parts:
        return False
    return all(part != ".." for part in parts) and not any(
        part == "." for part in parts
    )


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
