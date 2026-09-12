"""T20 safe Git commit executor - the only module that writes to Git.

This module owns the two and only two Git mutations T20 is allowed to perform:

1. **exact-path staging** - ``git add -- <exact-approved-paths>``
2. **exactly one commit** - ``git commit -m <validated-message>``

Everything else it runs is read-only (``rev-parse``, ``status``, ``diff``,
``ls-files``, ``hash-object`` without ``-w``, ``log``, ``ls-tree``, ``config``,
``var``, ``symbolic-ref``, ``rev-list``, ``diff-tree``). The mutation boundary is
enforced in code, not by convention:

* :data:`ALLOWED_GIT_SUBCOMMANDS` is an allow-list; any other subcommand raises
  :class:`GitCommitError` *before* a process is launched.
* :data:`FORBIDDEN_GIT_SUBCOMMANDS` names the destructive/network operations
  explicitly, so an accidental ``reset``, ``checkout``, ``push`` or ``amend``
  can never run - even if a future edit tries to.
* ``git add`` must carry ``--`` and may only receive exact paths (never ``.``,
  ``-A``, ``--all``, ``-u`` or ``-f``).
* ``git commit`` must carry exactly one ``-m`` and may never carry
  ``--amend``, ``--no-verify`` or ``--allow-empty``.
* ``-c`` overrides are restricted to ``user.name``/``user.email``/
  ``user.useConfigOnly``, so no command can inject ``core.hooksPath``,
  ``core.fsmonitor`` or another hook/exec vector.

State machine (each ``S`` only runs when every earlier gate passed):

    S0  VALIDATE_INPUTS        G1
    S1  IDENTIFY_REPOSITORY    G29 (read-only)
    S2  CHECK_ENVIRONMENT      G30 (before any Git process)
    S3  CHECK_BRANCH           G31, G32
    S4  CHECK_IDENTITY         G33
    S5  CHECK_REPO_STATE       G34-G37 ("repository checkpoint": every
                               repository/environment/branch/identity/state/
                               hook/HEAD gate G29-G37 must pass here, before S6
                               resolves a set and long before S9 writes)
    S6  VERIFY_BASELINE        G2-G18
    S7  RESOLVE_APPROVED_SET   G19-G24
    S8  SCAN_APPROVED_CONTENT  G25-G27
    S9  STAGE_EXACT_FILES      (first Git write)
    S10 VERIFY_INDEX           G38-G40
    S11 SCAN_STAGED_CONTENT    G28
    S12 FINAL_PRE_COMMIT_CHECK G41, G42
    S13 COMMIT                 G43 (second and last Git write)
    S14 VERIFY_COMMIT          G44, G45

Failure handling is deliberately passive: on a commit failure T20 does **not**
reset, restore, unstage, stash or retry. The staged state is left exactly as it
is and the result says so. A commit that exists but cannot be proven becomes
``COMMIT_UNVERIFIED`` - a loud failure for a human, never a silent pass.

Safety notes
------------
* ``shell=False`` everywhere; every command is an argv array.
* Git runs with a sanitized environment (unsafe ``GIT_*`` variables are refused
  and removed) plus ``GIT_TERMINAL_PROMPT=0``, so no command can reach a
  network, a prompt or another repository/index/object store.
* Git stderr is credential-redacted before it reaches a reason or an exception,
  and secret values are never logged.
* No ``git add .``, no ``--no-verify``, no automatic ``git config`` write.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from change_scope import normalise_relative_path
from commit_message import (
    CommitMessage,
    CommitMessageError,
    build_commit_message,
    validate_commit_message,
)
from commit_policy import (
    ACCOUNTED_HOOK_NAMES,
    ApprovedFileSet,
    CommitDecision,
    CommitFacts,
    CommitObservation,
    CommitPhase,
    CommitPolicyConfig,
    GateEvaluation,
    GateStatus,
    GitEnvironmentReport,
    GitIdentityReport,
    HookReport,
    OPERATION_IN_PROGRESS_MARKERS,
    RepositoryIdentity,
    StageObservation,
    UNSAFE_GIT_ENVIRONMENT_VARIABLES,
    evaluate_commit_gates,
    resolve_approved_files,
)
from repository import redact_credentials
from secret_scan import SecretFinding, SecretScanResult, SecretScanner
from worktree_baseline import (
    BaselineAttribution,
    WorktreeBaselineError,
    WorktreeBaselineInspector,
    WorktreeSnapshot,
    attribute_changes,
)

#: Read-only Git subcommands T20 may run.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "rev-list",
        "status",
        "diff",
        "diff-tree",
        "ls-files",
        "ls-tree",
        "hash-object",
        "config",
        "var",
        "symbolic-ref",
        "show-ref",
        "log",
        "cat-file",
        "check-ignore",
        "add",
        "commit",
    }
)

#: Destructive/network subcommands that must never run. Listed explicitly so the
#: refusal is intentional and testable rather than implied by the allow-list.
FORBIDDEN_GIT_SUBCOMMANDS = frozenset(
    {
        "reset",
        "restore",
        "clean",
        "stash",
        "checkout",
        "switch",
        "update-ref",
        "reflog",
        "rm",
        "mv",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "am",
        "apply",
        "push",
        "fetch",
        "pull",
        "remote",
        "clone",
        "branch",
        "tag",
        "gc",
        "prune",
        "repack",
        "filter-branch",
        "filter-repo",
        "replace",
        "notes",
        "worktree",
        "submodule",
        "commit-tree",
        "update-server-info",
        "daemon",
        "archive",
        "bundle",
        "fsck",
    }
)

#: Global Git options that take a separate value (so the subcommand search can
#: skip them).
_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

#: Global options that redirect the repository, the work tree or the program Git
#: executes. T20 builds its own single ``-C <root>`` *outside* the validated
#: arguments, so none of these may ever appear among the arguments T20 validates;
#: otherwise a future edit could commit into another repository.
_FORBIDDEN_REDIRECT_OPTIONS = frozenset(
    {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

#: Long options that are never acceptable in any T20 command.
_FORBIDDEN_OPTIONS_GLOBAL = frozenset(
    {
        "--no-verify",
        "--amend",
        "--allow-empty",
        "--allow-empty-message",
        "--fixup",
        "--squash",
        "--edit",
        "--reuse-message",
        "--pathspec-from-file",
        "--pathspec-file-nul",
        "--unsafe-paths",
        "--ignore-errors",
        "--renormalize",
        "--chmod",
        "--sparse",
        "--interactive",
    }
)

#: Options that are only dangerous for the subcommands that accept them. Keeping
#: this per-subcommand means ``git ls-files -u`` (unmerged entries) stays legal
#: while ``git add -u`` (stage everything) never is.
_FORBIDDEN_OPTIONS_BY_SUBCOMMAND: Mapping[str, frozenset] = {
    "add": frozenset(
        {
            "-A",
            "--all",
            "-u",
            "--update",
            "-f",
            "--force",
            "-N",
            "--intent-to-add",
            "-p",
            "--patch",
            "-i",
            "--include",
            "-e",
            "--edit",
        }
    ),
    "commit": frozenset(
        {
            "-a",
            "--all",
            "-i",
            "--include",
            "-o",
            "--only",
            "-p",
            "--patch",
            "-e",
            "--edit",
            "-F",
            "--file",
            "-C",
            "--reuse-message",
            "-c",
            "--reedit-message",
        }
    ),
}

#: ``-c`` overrides T20 is allowed to pass (anything else could move a hook, a
#: filter, a path or a template).
_ALLOWED_CONFIG_OVERRIDES: Tuple[str, ...] = (
    "user.name=",
    "user.email=",
    "user.useConfigOnly=",
)

#: Pathspec tokens that would broaden a staging command.
_BROAD_STAGING_TOKENS = frozenset(
    {".", "-A", "--all", "-u", "--update", "-f", "--force", ":/", ":/*"}
)

#: Environment T20 always forces for Git children (no prompt, no pager).
_SAFE_GIT_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
}

#: Maximum size of an approved file T20 will read and scan.
MAX_APPROVED_FILE_BYTES = 8 * 1024 * 1024

#: Bytes inspected when guessing whether a file is binary.
_BINARY_SNIFF_BYTES = 8192

#: Maximum accepted length of a Git identity value.
_MAX_IDENTITY_LENGTH = 200

#: A runner takes ``(args, cwd, env, timeout_seconds)`` and returns an object
#: with ``returncode``/``stdout``/``stderr``, or raises ``OSError`` /
#: ``subprocess.TimeoutExpired``.
GitRunner = Callable[[Sequence[str], str, Mapping[str, str], float], object]


def _subprocess_git_runner(
    args: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Default runner: ``subprocess.run`` with argv, ``shell=False`` and an env."""
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
        shell=False,
    )


class GitCommitError(Exception):
    """Raised when T20 cannot even attempt its state machine safely.

    This is a *caller/environment* error (missing inputs, an unusable repository
    path, broken Git, a forbidden command attempt). Refusals, commit failures
    and unverified commits are results, not exceptions.
    """


def _split_git_invocation(args: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
    """Split ``args`` into ``(subcommand, args after it)``.

    Global options that take a separate value (``-C <path>``, ``-c <k=v>``, ...)
    are skipped, so the subcommand is found even when T20 passes ``-C``.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token.startswith("-"):
            index += 2 if token in _GLOBAL_OPTIONS_WITH_VALUE else 1
            continue
        return token, tuple(args[index + 1 :])
    return "", ()


def _validate_git_invocation(args: Sequence[str]) -> str:
    """Validate one Git argv (without the executable) and return the subcommand.

    Raises:
        GitCommitError: the command is forbidden, outside the allow-list, or
            shaped in a way T20 must never use (broad staging, ``--amend``,
            ``--no-verify``, an unapproved ``-c`` override, ...).
    """
    subcommand, rest = _split_git_invocation(args)
    if not subcommand:
        raise GitCommitError(
            f"Refusing to run an unrecognised Git command: {list(args)}"
        )
    if subcommand in FORBIDDEN_GIT_SUBCOMMANDS:
        raise GitCommitError(
            f"T20 must never run 'git {subcommand}': it is a forbidden "
            "destructive or network operation."
        )
    if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
        raise GitCommitError(
            f"'git {subcommand}' is not on the T20 allow-list: {list(args)}"
        )

    tokens = list(args)
    body = list(rest)
    scoped_forbidden = _FORBIDDEN_OPTIONS_BY_SUBCOMMAND.get(subcommand, frozenset())
    for position, token in enumerate(tokens):
        if token in _FORBIDDEN_OPTIONS_GLOBAL:
            raise GitCommitError(
                f"Refusing to pass the forbidden Git option {token!r}."
            )
        if token in _FORBIDDEN_REDIRECT_OPTIONS:
            raise GitCommitError(
                f"Refusing to pass {token!r}: T20 fixes the repository with its "
                "own single -C argument."
            )
        if token.startswith("--") and "=" in token:
            name = token.split("=", 1)[0]
            if name in _FORBIDDEN_OPTIONS_GLOBAL or name in _FORBIDDEN_REDIRECT_OPTIONS:
                raise GitCommitError(
                    f"Refusing to pass the forbidden Git option {name!r}."
                )
        if token == "-c":
            override = tokens[position + 1] if position + 1 < len(tokens) else ""
            if not override.startswith(_ALLOWED_CONFIG_OVERRIDES):
                raise GitCommitError(
                    "Refusing to pass the Git configuration override "
                    f"{override!r}: only "
                    f"{', '.join(_ALLOWED_CONFIG_OVERRIDES)} may be set."
                )
    for token in body:
        if token in scoped_forbidden:
            raise GitCommitError(
                f"Refusing to pass {token!r} to 'git {subcommand}': it would "
                "broaden or bypass the exact-path operation."
            )

    if subcommand == "add":
        if "--" not in rest:
            raise GitCommitError(
                "git add must use the '--' separator before the paths."
            )
        paths = list(rest[rest.index("--") + 1 :])
        if not paths:
            raise GitCommitError("git add was given no explicit path.")
        for path in paths:
            if path in _BROAD_STAGING_TOKENS or path.startswith("-"):
                raise GitCommitError(
                    f"Refusing to stage {path!r}: only exact approved paths may "
                    "be staged."
                )
            if path.startswith(":"):
                raise GitCommitError(
                    f"Refusing to stage {path!r}: pathspec magic is not allowed."
                )
    if subcommand == "commit":
        if "-m" not in rest:
            raise GitCommitError("git commit must carry an explicit -m message.")
        if rest.count("-m") != 1:
            raise GitCommitError("git commit must carry exactly one -m message.")
        if "--" in rest:
            raise GitCommitError(
                "git commit must not receive pathspecs: the index is committed."
            )
    if subcommand == "config":
        # Configuration is read-only for T20: writing config (including
        # user.name/user.email) is forbidden.
        readers = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
        if not rest or rest[0] not in readers:
            raise GitCommitError(
                "T20 may only read Git configuration "
                f"({'/'.join(sorted(readers))}); got {list(rest)!r}."
            )
    return subcommand


class CommitStatus(Enum):
    """Executable outcome of one T20 attempt."""

    __test__ = False

    #: Exactly one verified commit was created.
    COMMITTED = "committed"
    #: A gate refused before the commit; nothing was committed.
    REFUSED = "refused"
    #: The single ``git commit`` attempt failed. No cleanup was performed.
    COMMIT_FAILED = "commit-failed"
    #: A commit exists but its correctness could not be proven.
    COMMIT_UNVERIFIED = "commit-unverified"


@dataclass(frozen=True)
class StageRecord:
    """One line of the T20 state-machine trail (never contains a secret)."""

    stage: str
    detail: str

    def as_dict(self) -> dict:
        """Plain summary."""
        return {"stage": self.stage, "detail": self.detail}


@dataclass(frozen=True)
class CommitResult:
    """Typed, secret-free outcome of one T20 attempt.

    Attributes:
        status: the executable outcome (:class:`CommitStatus`).
        reason: short human-readable explanation.
        gates: the final gate evaluation (all 45 gates).
        stage_records: the state-machine trail.
        approved_paths: the immutable approved path set.
        approved_content_ids: Git content identities recorded at approval time.
        staged_blob_ids: index blob ids read back after staging.
        commit_message: the message that was (or would have been) used.
        previous_head / new_head / commit_sha: the commit boundary.
        identity: the verified Git identity.
        repository: the resolved repository identity.
    """

    __test__ = False

    status: CommitStatus
    reason: str
    gates: GateEvaluation
    stage_records: Tuple[StageRecord, ...] = ()
    approved_paths: Tuple[str, ...] = ()
    approved_content_ids: Mapping[str, str] = field(default_factory=dict)
    staged_blob_ids: Mapping[str, str] = field(default_factory=dict)
    commit_message: Optional[CommitMessage] = None
    previous_head: Optional[str] = None
    new_head: Optional[str] = None
    commit_sha: Optional[str] = None
    identity: Optional[GitIdentityReport] = None
    repository: Optional[RepositoryIdentity] = None

    @property
    def is_committed(self) -> bool:
        """True only for a fully verified commit."""
        return self.status is CommitStatus.COMMITTED

    @property
    def is_refusal(self) -> bool:
        """True when a gate refused before any commit."""
        return self.status is CommitStatus.REFUSED

    @property
    def needs_attention(self) -> bool:
        """True when a human must look at the repository (no auto-recovery)."""
        return self.status in (
            CommitStatus.COMMIT_FAILED,
            CommitStatus.COMMIT_UNVERIFIED,
        )

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "stage_records": [record.as_dict() for record in self.stage_records],
            "approved_paths": list(self.approved_paths),
            "approved_content_ids": dict(self.approved_content_ids),
            "staged_blob_ids": dict(self.staged_blob_ids),
            "commit_message": (
                self.commit_message.as_dict() if self.commit_message else None
            ),
            "previous_head": self.previous_head,
            "new_head": self.new_head,
            "commit_sha": self.commit_sha,
            "is_committed": self.is_committed,
            "needs_attention": self.needs_attention,
            "identity": self.identity.as_dict() if self.identity else None,
            "repository": self.repository.as_dict() if self.repository else None,
            "gates": self.gates.as_dict(),
        }


def _same_path(left: object, right: object) -> bool:
    """True when two paths denote the same location (case/8.3 tolerant)."""
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except OSError:  # pragma: no cover - defensive
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
            os.path.normpath(str(right))
        )


def _is_within(candidate: object, parent: object) -> bool:
    """True when ``candidate`` is inside ``parent`` (path-component aware)."""
    candidate_text = os.path.normcase(os.path.normpath(str(candidate)))
    parent_text = os.path.normcase(os.path.normpath(str(parent)))
    if candidate_text == parent_text:
        return True
    return candidate_text.startswith(parent_text.rstrip(os.sep) + os.sep)


#: ``Name <email> <epoch> <timezone>`` as produced by ``git var``/``git log``.
_IDENT_RE = re.compile(r"^(?P<name>.+?) <(?P<email>[^<>]*)> \d+ [+-]\d{4}$")

#: A ``git diff-tree --name-status`` status field (``M``, ``A``, ``D``, ``R100``).
_NAME_STATUS_RE = re.compile(r"^[A-Z][0-9]*$")


def _parse_name_status_z(text: str) -> Tuple[Tuple[str, str, Optional[str]], ...]:
    """Parse ``git diff-tree --name-status -r -z`` output defensively.

    Handles both shapes Git can emit with ``-z`` (``<status>\\0<path>\\0`` and
    ``<status>\\t<path>\\0``) and the two-path rename/copy form. Only the first
    letter of the status is kept.

    Returns:
        Sorted ``(status, path, origin)`` tuples; ``origin`` is the rename/copy
        source when present.
    """
    fields = str(text or "").split("\0")
    records: List[Tuple[str, str, Optional[str]]] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if raw == "":
            continue
        status: Optional[str] = None
        path = ""
        if "\t" in raw:
            head, _, tail = raw.partition("\t")
            if _NAME_STATUS_RE.match(head):
                status, path = head, tail
            else:
                path = raw
        elif _NAME_STATUS_RE.match(raw):
            status = raw
            if index < len(fields):
                path = fields[index]
                index += 1
        else:
            path = raw
        if status is None or path == "":
            continue
        origin: Optional[str] = None
        if status[:1] in ("R", "C") and index < len(fields) and fields[index] != "":
            # For renames/copies Git emits the source first and the destination
            # second; the record is normalized to ``(status, destination,
            # source)`` so it matches ``WorktreeSnapshot.renamed_files``.
            origin = path
            path = fields[index]
            index += 1
        records.append((status[:1], path, origin))
    return tuple(sorted(records))


def _parse_ident(text: object) -> Tuple[Optional[str], Optional[str]]:
    """Parse ``Name <email> <epoch> <tz>`` into ``(name, email)``."""
    match = _IDENT_RE.match(str(text or ""))
    if match is None:
        return None, None
    return match.group("name"), match.group("email")


def _is_executable_file(path: str) -> bool:
    """True when Git could run ``path`` as a hook.

    Git for Windows decides by content/shebang rather than the POSIX permission
    bit, so on Windows any existing file counts as executable. Elsewhere the
    executable bit is required. Being conservative here only produces more
    refusals, never fewer.
    """
    if not os.path.isfile(path):
        return False
    if os.name == "nt":  # pragma: no cover - platform dependent
        return True
    return os.access(path, os.X_OK)


def _looks_binary(data: bytes) -> bool:
    """True when ``data`` looks like a binary file (NUL byte or bad UTF-8)."""
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return True
    return False


class GitCommitExecutor:
    """Perform at most one exact-path staging and one verified commit (T20).

    Args:
        git_command: Git executable name or path (default ``"git"``).
        timeout_seconds: per-command timeout.
        runner: optional injectable process runner
            ``(args, cwd, env, timeout) -> completed``.
        secret_scanner: optional :class:`SecretScanner` override. When omitted,
            a scanner is built per call from the supplied secret values.
        config: the :class:`CommitPolicyConfig` (conservative defaults).
        baseline_inspector: optional :class:`WorktreeBaselineInspector`.
        environment: the environment mapping to inspect (defaults to
            ``os.environ``). The caller's environment is never mutated.
    """

    def __init__(
        self,
        *,
        git_command: str = "git",
        timeout_seconds: float = 60.0,
        runner: Optional[GitRunner] = None,
        secret_scanner: Optional[SecretScanner] = None,
        config: Optional[CommitPolicyConfig] = None,
        baseline_inspector: Optional[WorktreeBaselineInspector] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._git_command = git_command
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _subprocess_git_runner if runner is None else runner
        self._scanner = secret_scanner
        self._config = config or CommitPolicyConfig()
        self._environment = dict(
            os.environ if environment is None else environment
        )
        self._sanitized_environment = self._build_sanitized_environment()
        self._inspector = (
            baseline_inspector
            if baseline_inspector is not None
            else WorktreeBaselineInspector(
                git_command=git_command,
                timeout_seconds=self._timeout_seconds,
                runner=self._baseline_runner,
            )
        )

    # ------------------------------------------------------------------
    # Environment and process plumbing
    # ------------------------------------------------------------------

    def _build_sanitized_environment(self) -> Dict[str, str]:
        """Return the environment Git children receive.

        ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``/... are removed, so a
        child can never be redirected to another repository, index or object
        store, and ``GIT_TERMINAL_PROMPT=0`` guarantees no command can block on
        or answer an interactive prompt. The caller's environment mapping is
        left untouched.
        """
        sanitized = {
            name: value
            for name, value in self._environment.items()
            if name not in UNSAFE_GIT_ENVIRONMENT_VARIABLES
        }
        sanitized.update(_SAFE_GIT_ENVIRONMENT)
        return sanitized

    def _baseline_runner(
        self, args: Sequence[str], cwd: str, timeout: float
    ) -> object:
        """Adapt the 4-arg runner to the baseline inspector's 3-arg API."""
        return self._runner(args, cwd, dict(self._sanitized_environment), timeout)

    def _run(self, root: Path, *args: str, allow_failure: bool = False) -> object:
        """Run one validated Git command with a sanitized environment.

        Raises:
            GitCommitError: the command is not allowed, Git cannot be launched,
                it times out, or it fails and ``allow_failure`` is ``False``.
        """
        _validate_git_invocation(args)
        command = [self._git_command, "-C", str(root), *args]
        try:
            completed = self._runner(
                command,
                str(root),
                dict(self._sanitized_environment),
                self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GitCommitError(
                f"Git executable not found (configured "
                f"'{self._git_command}'): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommitError(
                f"Git command timed out after {self._timeout_seconds:g} seconds: "
                f"{list(args)}"
            ) from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise GitCommitError(
                redact_credentials(f"Failed to run Git: {exc}")
            ) from exc
        if not allow_failure and int(getattr(completed, "returncode", 1)) != 0:
            detail = redact_credentials(
                str(getattr(completed, "stderr", "") or "").strip()
            )
            raise GitCommitError(
                f"Git command failed ({' '.join(args)})"
                f"{': ' + detail if detail else '.'}"
            )
        return completed

    def _text(self, root: Path, *args: str) -> Optional[str]:
        """Run a read-only Git command and return stripped stdout, or ``None``."""
        completed = self._run(root, *args, allow_failure=True)
        if int(getattr(completed, "returncode", 1)) != 0:
            return None
        value = str(getattr(completed, "stdout", "") or "").strip()
        return value or None

    def _config_value(self, root: Path, key: str) -> Optional[str]:
        """Read one Git configuration value, or ``None`` when it is unset."""
        completed = self._run(root, "config", "--get", key, allow_failure=True)
        if int(getattr(completed, "returncode", 1)) != 0:
            return None
        value = str(getattr(completed, "stdout", "") or "").strip()
        return value or None

    # ------------------------------------------------------------------
    # Read-only evidence collection
    # ------------------------------------------------------------------

    def _inspect_environment(self) -> GitEnvironmentReport:
        """G30 evidence: which Git environment variables are unsafe here."""
        unsafe = tuple(
            name
            for name in UNSAFE_GIT_ENVIRONMENT_VARIABLES
            if str(self._environment.get(name, "") or "").strip()
        )
        if unsafe:
            reason = (
                "Unsafe Git environment variable(s) are set: "
                + ", ".join(unsafe)
                + "."
            )
        else:
            reason = "No redirecting or code-executing Git environment variable is set."
        return GitEnvironmentReport(
            unsafe_variables=unsafe,
            checked_variables=UNSAFE_GIT_ENVIRONMENT_VARIABLES,
            reason=reason,
        )

    def _identify_repository(
        self, repository_path: object, *, pre_staging: bool = True
    ) -> RepositoryIdentity:
        """Resolve the repository identity (S1) with read-only Git commands."""
        requested = Path(str(repository_path)).expanduser()
        if not requested.is_dir():
            raise GitCommitError(
                f"Repository path is not an existing directory: '{requested}'."
            )
        root = requested.resolve()

        worktree = self._text(root, "rev-parse", "--show-toplevel")
        git_dir = self._text(root, "rev-parse", "--absolute-git-dir")
        common_dir = self._text(root, "rev-parse", "--git-common-dir")
        index_path = self._text(root, "rev-parse", "--git-path", "index")
        head_commit = self._text(root, "rev-parse", "HEAD")
        branch = self._text(root, "symbolic-ref", "--short", "-q", "HEAD")

        problems: List[str] = []
        if not worktree:
            problems.append("the worktree root could not be resolved")
        if not git_dir:
            problems.append("the Git directory could not be resolved")
        if not common_dir:
            problems.append("the common Git directory could not be resolved")
        if not index_path:
            problems.append("the index path could not be resolved")
        if head_commit is None:
            problems.append("HEAD could not be read")
        if not branch:
            problems.append("no branch is checked out (detached HEAD)")

        resolved_index: Optional[str] = None
        if index_path:
            candidate = Path(index_path)
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            resolved_index = str(candidate)

        index_locked = False
        if resolved_index:
            index_locked = os.path.exists(resolved_index + ".lock")
        lock_probe = self._text(root, "rev-parse", "--git-path", "index.lock")
        if lock_probe:
            lock_path = Path(lock_probe)
            if not lock_path.is_absolute():
                lock_path = (root / lock_path).resolve()
            index_locked = index_locked or os.path.exists(str(lock_path))

        matches_expected = worktree is not None and _same_path(root, worktree)
        if not matches_expected:
            problems.append(
                "the resolved worktree root does not match the requested "
                "repository path"
            )
        if resolved_index and git_dir and not (
            _is_within(resolved_index, git_dir)
            or (common_dir and _is_within(resolved_index, common_dir))
        ):
            problems.append(
                "the index path is not inside this repository's Git directory"
            )

        # A pre-populated index is a repository state T20 cannot work with: it
        # never resets or unstages, so entries of unknown provenance must never
        # reach the commit. After T20 stages its own approved paths the index is
        # legitimately populated, so this check runs before staging only.
        if pre_staging:
            staged_probe = self._text(
                root, "diff", "--cached", "--name-only", "-z"
            )
            if staged_probe:
                problems.append(
                    "the index already contains staged change(s), which T20 never "
                    "resets or unstages"
                )

        return RepositoryIdentity(
            expected_root=str(root),
            worktree_root=str(Path(worktree).resolve()) if worktree else None,
            git_dir=str(Path(git_dir).resolve()) if git_dir else None,
            common_dir=str(Path(common_dir).resolve()) if common_dir else None,
            index_path=resolved_index,
            head_commit=head_commit,
            branch=branch,
            detached=not branch,
            matches_expected=matches_expected,
            index_locked=index_locked,
            problems=tuple(problems),
        )

    def _operations_in_progress(
        self, git_dir: Optional[str], common_dir: Optional[str]
    ) -> Tuple[str, ...]:
        """Return the Git state markers that mean another operation is running."""
        bases = {base for base in (git_dir, common_dir) if base}
        found = set()
        for base in bases:
            for marker in OPERATION_IN_PROGRESS_MARKERS:
                if os.path.exists(os.path.join(base, marker)):
                    found.add(marker)
        return tuple(sorted(found))

    def _unmerged_paths(self, root: Path) -> Tuple[str, ...]:
        """Return the index entries that are unmerged (G35 evidence)."""
        completed = self._run(root, "ls-files", "-u", "-z")
        paths = set()
        for entry in str(getattr(completed, "stdout", "") or "").split("\0"):
            if not entry:
                continue
            _meta, _separator, path = entry.partition("\t")
            if path:
                paths.add(path)
        return tuple(sorted(paths))

    def _inspect_identity(self, root: Path) -> GitIdentityReport:
        """G33 evidence: is ``user.name``/``user.email`` explicit config?"""
        name = self._config_value(root, "user.name")
        email = self._config_value(root, "user.email")
        use_config_only = self._config_value(root, "user.useConfigOnly")
        actor_name, actor_email = _parse_ident(
            self._text(root, "var", "GIT_AUTHOR_IDENT")
        )
        committer_name, committer_email = _parse_ident(
            self._text(root, "var", "GIT_COMMITTER_IDENT")
        )

        problems: List[str] = []
        for label, value in (("user.name", name), ("user.email", email)):
            if not value:
                problems.append(f"{label} is not configured")
                continue
            if len(value) > _MAX_IDENTITY_LENGTH:
                problems.append(
                    f"{label} is longer than {_MAX_IDENTITY_LENGTH} characters"
                )
            if any(character in value for character in ("\n", "\r", "\0")):
                problems.append(f"{label} contains a control character")
        explicit = bool(
            name
            and email
            and not problems
            and actor_name == name
            and actor_email == email
            and committer_name == name
            and committer_email == email
        )
        if name and email and not explicit and not problems:
            problems.append(
                "the identity Git would use (git var) is not the configured "
                "user.name/user.email, so the identity is not explicit"
            )
        return GitIdentityReport(
            name=name,
            email=email,
            actor_name=actor_name,
            actor_email=actor_email,
            committer_name=committer_name,
            committer_email=committer_email,
            use_config_only=use_config_only,
            explicit=explicit,
            problems=tuple(problems),
        )

    def _inspect_hooks(
        self, root: Path, git_dir: Optional[str], common_dir: Optional[str]
    ) -> HookReport:
        """G36 evidence: are commit-affecting hooks installed and active?"""
        configured = self._config_value(root, "core.hooksPath")
        if configured:
            base = configured
            if not os.path.isabs(base):
                base = os.path.join(str(root), base)
        else:
            base = os.path.join(str(common_dir or git_dir or root), "hooks")

        inspected: List[str] = []
        active: List[str] = []
        if os.path.isdir(base):
            for name in ACCOUNTED_HOOK_NAMES:
                inspected.append(name)
                if _is_executable_file(os.path.join(base, name)):
                    active.append(name)

        if active:
            reason = (
                "Active hook(s) that T20 cannot account for: "
                + ", ".join(active)
                + "."
            )
        elif configured:
            reason = (
                f"core.hooksPath is set to '{configured}' and contains no active "
                "commit-affecting hook."
            )
        else:
            reason = "No active commit-affecting hook is installed."
        return HookReport(
            hooks_path=configured,
            active_hooks=tuple(sorted(active)),
            inspected_hooks=tuple(inspected),
            reason=reason,
        )

    def _staged_blob_ids(
        self, root: Path, paths: Sequence[str]
    ) -> Dict[str, str]:
        """Read the index blob ids of ``paths`` (``git ls-files -s``)."""
        completed = self._run(root, "ls-files", "-s", "-z", "--", *paths)
        blobs: Dict[str, str] = {}
        for entry in str(getattr(completed, "stdout", "") or "").split("\0"):
            if not entry:
                continue
            meta, _separator, path = entry.partition("\t")
            fields = meta.split()
            if len(fields) >= 2 and path:
                blobs[path] = fields[1]
        return blobs

    def _worktree_content_ids(
        self, root: Path, paths: Sequence[str]
    ) -> Dict[str, str]:
        """Read the Git content identity of ``paths`` from the worktree.

        Uses ``git hash-object --path=<path> -- <path>`` - the primitive proven
        in T20 Step 1 - never a raw byte hash, so CRLF normalisation,
        ``.gitattributes`` and filters are applied exactly as staging does.
        """
        content_ids: Dict[str, str] = {}
        for path in paths:
            value = self._text(root, "hash-object", f"--path={path}", "--", path)
            if value:
                content_ids[path] = value
        return content_ids

    def _scan_text(self, scanner: SecretScanner, text: str) -> SecretScanResult:
        """Scan text with the configured scanner (never logs a value)."""
        return scanner.scan_text(text)

    def _read_approved_content(
        self, root: Path, paths: Sequence[str], scanner: SecretScanner
    ) -> Tuple[Dict[str, str], Tuple[str, ...], SecretScanResult]:
        """Read, validate and scan the approved worktree files.

        Returns:
            ``(texts, binary_paths, scan)``. ``texts`` holds the decoded content
            of every readable text file, ``binary_paths`` the files that are not
            text (G25 refuses those), and ``scan`` the merged secret scan of the
            content (``scanned`` is ``False`` whenever any file could not be
            scanned, so the gate fails closed).
        """
        texts: Dict[str, str] = {}
        binary: List[str] = []
        findings: List[SecretFinding] = []
        failures: List[str] = []
        secret_count = 0

        for path in paths:
            candidate = root / path
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                failures.append(f"{path!r} could not be read ({exc.strerror})")
                continue
            if len(data) > MAX_APPROVED_FILE_BYTES:
                failures.append(
                    f"{path!r} is larger than {MAX_APPROVED_FILE_BYTES} bytes"
                )
                continue
            if _looks_binary(data):
                binary.append(path)
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover - _looks_binary guard
                failures.append(f"{path!r} could not be decoded as text")
                continue
            texts[path] = text
            result = self._scan_text(scanner, text)
            secret_count = max(secret_count, result.secret_count)
            if not result.scanned:
                failures.append(
                    f"{path!r} could not be scanned ({result.reason})"
                )
                continue
            findings.extend(result.findings)

        scanned = not binary and not failures
        clean = not findings and not failures
        if failures:
            reason = "Content could not be scanned: " + "; ".join(failures) + "."
        elif binary:
            reason = (
                "Binary content is not scanned and is refused: "
                + ", ".join(sorted(binary))
                + "."
            )
        elif findings:
            reason = (
                f"{len(findings)} credential-shaped finding(s) in the approved "
                "content."
            )
        else:
            reason = (
                "No configured secret and no URL userinfo was found in the "
                "approved content."
            )
        return (
            texts,
            tuple(sorted(binary)),
            SecretScanResult(
                scanned=scanned,
                clean=clean,
                findings=tuple(findings),
                reason=reason,
                secret_count=secret_count,
            ),
        )

    def _diff_text(self, root: Path, paths: Sequence[str], *, cached: bool) -> str:
        """Return the diff text for ``paths`` (worktree vs HEAD, or staged).

        ``--no-ext-diff`` and ``--no-textconv`` stop Git from invoking an
        external diff/textconv driver, so scanning cannot execute anything.
        """
        args: List[str] = ["diff"]
        if cached:
            args.append("--cached")
        else:
            args.append("HEAD")
        args += ["--no-color", "--no-ext-diff", "--no-textconv", "--", *paths]
        completed = self._run(root, *args)
        return str(getattr(completed, "stdout", "") or "")

    def _scan_diff(
        self, scanner: SecretScanner, text: str
    ) -> SecretScanResult:
        """Scan diff text with the configured scanner."""
        return scanner.scan_diff(text)


    def _empty_scan(self, reason: str) -> SecretScanResult:
        """A completed, clean scan of nothing (there is nothing to scan)."""
        return SecretScanResult(
            scanned=True, clean=True, findings=(), reason=reason, secret_count=0
        )

    def _result(
        self,
        status: CommitStatus,
        facts: CommitFacts,
        phase: CommitPhase,
        reason: str,
        records: Sequence[StageRecord],
        *,
        approved_content_ids: Optional[Mapping[str, str]] = None,
        staged_blob_ids: Optional[Mapping[str, str]] = None,
        new_head: Optional[str] = None,
        identity: Optional[GitIdentityReport] = None,
    ) -> CommitResult:
        """Build a :class:`CommitResult` with a fresh gate evaluation."""
        evaluation = evaluate_commit_gates(facts, phase=phase)
        return CommitResult(
            status=status,
            reason=reason,
            gates=evaluation,
            stage_records=tuple(records),
            approved_paths=tuple(getattr(facts.approved, "paths", ()) or ()),
            approved_content_ids=dict(approved_content_ids or {}),
            staged_blob_ids=dict(staged_blob_ids or {}),
            commit_message=facts.commit_message,
            previous_head=(
                facts.baseline.head_commit if facts.baseline is not None else None
            ),
            new_head=new_head,
            commit_sha=new_head,
            identity=identity if identity is not None else facts.identity,
            repository=facts.repository,
        )

    # ------------------------------------------------------------------
    # The T20 state machine
    # ------------------------------------------------------------------

    def commit_safely(
        self,
        *,
        repository_path: object,
        issue_status: object,
        baseline: Optional[WorktreeSnapshot] = None,
        after: Optional[WorktreeSnapshot] = None,
        attribution: Optional[BaselineAttribution] = None,
        scope: object = None,
        codex: object = None,
        tests: object = None,
        analysis: object = None,
        verification: object = None,
        trigger: object = None,
        issue_key: object = None,
        rule: object = None,
        target_file: object = None,
        secrets: Sequence[str] = (),
        message: Optional[CommitMessage] = None,
        config: Optional[CommitPolicyConfig] = None,
    ) -> CommitResult:
        """Run T20 for one T19 ``FIXED`` attempt.

        The T11/T13/T15/T16/T17/T18 records default to the ones carried by
        ``issue_status`` (the T19 result), so T20 consumes the existing contract
        instead of re-deriving it.

        Args:
            repository_path: the repository the agent worked in.
            issue_status: the T19 :class:`issue_status.IssueStatusResult`.
            baseline: the pre-run :class:`WorktreeSnapshot` captured with
                ``include_ignored=True``.
            after: the post-run snapshot (same flag).
            attribution: ``worktree_baseline.attribute_changes(baseline, after)``.
            scope/codex/tests/analysis/verification/trigger: optional explicit
                overrides for the records carried by ``issue_status``.
                ``trigger`` defaults to the T19 result's ``trigger_evidence``
                (the compact T16 evidence: trigger outcome + compute-engine task
                id); G11 refuses when that evidence is absent, so a T19 result
                that did not carry it can never be committed.
            issue_key/rule/target_file: optional overrides for the identity and
                target derived from the T19 result.
            secrets: literal secret values to scan for
                (``AnalysisConfig.secrets``).
            message: an already-validated :class:`CommitMessage`; otherwise one
                is built deterministically from the validated rule/path/issue.
            config: policy configuration override.

        Returns:
            A :class:`CommitResult`. Refusals, commit failures and unverified
            commits are results; only caller/environment errors raise
            :class:`GitCommitError`.
        """
        if issue_status is None:
            raise GitCommitError("T20 requires the T19 IssueStatusResult.")
        policy = config or self._config
        scanner = (
            self._scanner
            if self._scanner is not None
            else SecretScanner(tuple(secrets))
        )
        records: List[StageRecord] = []

        def record(stage: str, detail: str) -> None:
            records.append(StageRecord(stage=stage, detail=detail))

        # --- S0 VALIDATE_INPUTS -------------------------------------------
        return self._state_machine(
            records=records,
            record=record,
            scanner=scanner,
            policy=policy,
            repository_path=repository_path,
            issue_status=issue_status,
            baseline=baseline,
            after=after,
            attribution=attribution,
            scope=(
                scope if scope is not None
                else getattr(issue_status, "scope", None)
            ),
            codex=(
                codex if codex is not None
                else getattr(issue_status, "codex", None)
            ),
            tests=(
                tests if tests is not None
                else getattr(issue_status, "tests", None)
            ),
            analysis=(
                analysis if analysis is not None
                else getattr(issue_status, "analysis", None)
            ),
            verification=(
                verification if verification is not None
                else getattr(issue_status, "verification", None)
            ),
            trigger=(
                trigger if trigger is not None
                else getattr(issue_status, "trigger_evidence", None)
            ),
            issue_key=issue_key,
            rule=rule,
            target_file=target_file,
            message=message,
        )

    def _state_machine(
        self,
        *,
        records: List[StageRecord],
        record: Callable[[str, str], None],
        scanner: SecretScanner,
        policy: CommitPolicyConfig,
        repository_path: object,
        issue_status: object,
        baseline: Optional[WorktreeSnapshot],
        after: Optional[WorktreeSnapshot],
        attribution: Optional[BaselineAttribution],
        scope: object,
        codex: object,
        tests: object,
        analysis: object,
        verification: object,
        trigger: object,
        issue_key: object,
        rule: object,
        target_file: object,
        message: Optional[CommitMessage],
    ) -> CommitResult:
        """S0-S14: the documented T20 state machine (see the module docstring)."""
        original_issue = getattr(verification, "original_issue", None)
        if not issue_key:
            issue_key = (
                getattr(verification, "original_issue_key", None)
                or getattr(original_issue, "key", None)
                or ""
            )
        if not rule:
            rule = getattr(original_issue, "rule", None) or ""
        if not target_file:
            expected_files = tuple(getattr(scope, "expected_files", ()) or ())
            target_file = (
                expected_files[0]
                if expected_files
                else (getattr(original_issue, "file_path", "") or "")
            )
        normalized_target = normalise_relative_path(target_file) or ""

        facts = CommitFacts(
            issue_status=issue_status,
            codex=codex,
            tests=tests,
            analysis=analysis,
            trigger=trigger,
            verification=verification,
            scope=scope,
            attribution=attribution,
            baseline=baseline,
            after=after,
            issue_key=str(issue_key or ""),
            rule=str(rule or ""),
            target_file=normalized_target,
            config=policy,
        )
        record(
            "S0-validate-inputs",
            "T19 status="
            f"{getattr(getattr(issue_status, 'status', None), 'value', None)!r}, "
            f"issue={str(issue_key) or '<missing>'!r}, "
            f"rule={str(rule) or '<missing>'!r}, "
            f"target={normalized_target or '<missing>'!r}.",
        )

        if message is None:
            try:
                message = build_commit_message(
                    rule=rule,
                    file_path=normalized_target or target_file,
                    issue_key=issue_key,
                    max_message_length=policy.max_message_length,
                )
                record("S0-message", f"Built commit message {message.subject!r}.")
            except CommitMessageError as exc:
                record("S0-message", f"Commit message could not be built: {exc}")
                return self._result(
                    CommitStatus.REFUSED,
                    facts,
                    CommitPhase.FIX_EVIDENCE,
                    f"The commit message could not be built: {exc}",
                    records,
                )
        else:
            record(
                "S0-message", f"Using the supplied commit message {message.subject!r}."
            )
        facts = replace(facts, commit_message=message)

        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.INPUTS)
        if evaluation.is_refusal:
            record("S0-validate-inputs", evaluation.refusal_reason or "Refused.")
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.INPUTS,
                evaluation.refusal_reason or "The T20 inputs are incomplete.",
                records,
            )

        # --- S2 CHECK_ENVIRONMENT (before any Git process) ----------------
        environment = self._inspect_environment()
        facts = replace(facts, environment=environment)
        record("S2-check-environment", environment.reason)
        if not environment.is_safe:
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.REPOSITORY,
                "The Git environment is unsafe: "
                + ", ".join(environment.unsafe_variables)
                + ".",
                records,
                identity=None,
            )

        # --- S1 IDENTIFY_REPOSITORY / S3-S5 -------------------------------
        repository = self._identify_repository(repository_path)
        root = Path(repository.expected_root)
        record(
            "S1-identify-repository",
            f"worktree={repository.worktree_root!r}, git_dir={repository.git_dir!r}, "
            f"index={repository.index_path!r}, HEAD={repository.head_commit!r}, "
            f"branch={repository.branch!r}.",
        )
        operations = self._operations_in_progress(
            repository.git_dir, repository.common_dir
        )
        hooks = self._inspect_hooks(root, repository.git_dir, repository.common_dir)
        # These two probes need a usable repository: in a directory that is not a
        # Git working copy the commands fail, and the correct outcome is a G29
        # refusal - not a crash. Leaving the identity unset keeps G33 fail closed.
        if repository.is_valid:
            unmerged = self._unmerged_paths(root)
            identity = self._inspect_identity(root)
        else:
            unmerged = ()
            identity = GitIdentityReport(
                problems=(
                    "the repository identity is not usable, so the Git identity "
                    "and the index state could not be read",
                )
            )
        facts = replace(
            facts,
            repository=repository,
            identity=identity,
            hooks=hooks,
            operation_in_progress=operations,
            unmerged_paths=unmerged,
        )
        record("S3-check-branch", f"branch={repository.branch!r}, detached={repository.detached}.")
        record("S4-check-identity", f"explicit={identity.explicit}, name={identity.name!r}.")
        record(
            "S5-check-repo-state",
            f"in_progress={list(operations)}, unmerged={list(unmerged)}, "
            f"index_locked={repository.index_locked}.",
        )

        # --- S5 REPOSITORY CHECKPOINT (G29-G37) ---------------------------
        # Every repository/environment/branch/identity/state/hook/HEAD gate must
        # pass here, before S6 resolves an approved set and long before S9
        # performs the first Git write. Deferring them to the staging checkpoint
        # would mean the index had already been mutated on a protected branch, in
        # a redirected environment or without an explicit identity - exactly what
        # these gates exist to prevent.
        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.REPOSITORY)
        if evaluation.is_refusal:
            record("S5-check-repo-state", evaluation.refusal_reason or "Refused.")
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.REPOSITORY,
                evaluation.refusal_reason
                or "The repository, environment, branch, identity or state is "
                "not safe to commit in. Nothing was staged.",
                records,
                identity=identity,
            )

        # --- S6 VERIFY_BASELINE -------------------------------------------
        if baseline is None or after is None or attribution is None:
            record(
                "S6-verify-baseline",
                "The pre-run baseline, the post-run snapshot and the attribution "
                "are all required to prove that the change belongs to this run.",
            )
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.FIX_EVIDENCE,
                "T20 requires the pre-run baseline, the post-run snapshot and the "
                "attribution of this run; it never guesses what the agent changed.",
                records,
                identity=identity,
            )
        record(
            "S6-verify-baseline",
            f"clean_baseline={getattr(attribution, 'clean_baseline', None)}, "
            f"attributable={getattr(attribution, 'attributable', None)}, "
            f"agent_files={list(getattr(attribution, 'agent_files', ()) or ())}.",
        )

        # --- S7 RESOLVE_APPROVED_SET (resolved once, then immutable) ------
        approved = resolve_approved_files(
            target_file=normalized_target,
            scope=scope,
            attribution=attribution,
            baseline=baseline,
            after=after,
            config=policy,
        )
        facts = replace(facts, approved=approved)
        record(
            "S7-resolve-approved-set",
            f"paths={list(approved.paths)}, expected={list(approved.expected)}, "
            f"outside_scope={list(approved.outside_scope)}, "
            f"problems={list(approved.problems)}.",
        )

        approved_ids: Dict[str, str] = {
            path: (after.content_id_of(path) or "") for path in approved.paths
        }

        # --- S8 SCAN_APPROVED_CONTENT -------------------------------------
        if approved.paths:
            _texts, binary_paths, content_scan = self._read_approved_content(
                root, approved.paths, scanner
            )
            worktree_diff_scan = self._scan_diff(
                scanner, self._diff_text(root, approved.paths, cached=False)
            )
        else:
            binary_paths = ()
            content_scan = self._empty_scan("No approved content to scan.")
            worktree_diff_scan = self._empty_scan("No approved content to scan.")
        facts = replace(
            facts,
            binary_paths=tuple(binary_paths),
            content_scan=content_scan,
            worktree_diff_scan=worktree_diff_scan,
        )
        record(
            "S8-scan-approved-content",
            f"binary={list(binary_paths)}, content_scan_ok={content_scan.ok}, "
            f"worktree_diff_scan_ok={worktree_diff_scan.ok}.",
        )

        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.APPROVAL)
        if evaluation.is_refusal:
            record("S8-scan-approved-content", evaluation.refusal_reason or "Refused.")
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.APPROVAL,
                evaluation.refusal_reason or "The approved change was refused.",
                records,
                approved_content_ids=approved_ids,
                identity=identity,
            )

        head_before_staging = self._text(root, "rev-parse", "HEAD")

        # --- S9 STAGE_EXACT_FILES (first Git write) -----------------------
        staging = self._run(root, "add", "--", *approved.paths, allow_failure=True)
        stage_error: Optional[str] = None
        if int(getattr(staging, "returncode", 1)) != 0:
            stage_error = redact_credentials(
                str(getattr(staging, "stderr", "") or "").strip()
            ) or f"git add exited with code {getattr(staging, 'returncode', '?')}"
        record(
            "S9-stage-exact-files",
            "git add -- " + " ".join(approved.paths)
            + (f" failed: {stage_error}" if stage_error else " succeeded."),
        )

        # --- S10 VERIFY_INDEX ---------------------------------------------
        staged_paths: Tuple[str, ...] = ()
        staged_blob_ids: Dict[str, str] = {}
        worktree_content_ids: Dict[str, str] = {}
        head_after_staging: Optional[str] = None
        if stage_error is None:
            staged_blob_ids = self._staged_blob_ids(root, approved.paths)
            staged_paths = tuple(sorted(staged_blob_ids))
            worktree_content_ids = self._worktree_content_ids(root, approved.paths)
            head_after_staging = self._text(root, "rev-parse", "HEAD")
            record(
                "S10-verify-index",
                f"staged={list(staged_paths)}, blobs={staged_blob_ids}.",
            )

        # --- S11 SCAN_STAGED_CONTENT --------------------------------------
        if stage_error is None:
            staged_diff_scan = self._scan_diff(
                scanner, self._diff_text(root, approved.paths, cached=True)
            )
        else:
            staged_diff_scan = None
        facts = replace(
            facts,
            stage=StageObservation(
                attempted=True,
                error=stage_error,
                approved_paths=tuple(approved.paths),
                staged_paths=staged_paths,
                staged_blob_ids=dict(staged_blob_ids),
                worktree_content_ids=dict(worktree_content_ids),
                approved_content_ids=dict(approved_ids),
                head_before=head_before_staging,
                head_after=head_after_staging,
                unmerged_paths=tuple(self._unmerged_paths(root)),
            ),
            staged_diff_scan=staged_diff_scan,
        )
        record(
            "S11-scan-staged-content",
            "The staged diff was scanned."
            if staged_diff_scan is not None
            else "The staged diff could not be scanned because staging failed.",
        )

        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.STAGING)
        if evaluation.is_refusal:
            record("S11-scan-staged-content", evaluation.refusal_reason or "Refused.")
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.STAGING,
                evaluation.refusal_reason
                or "The staged change could not be verified.",
                records,
                approved_content_ids=approved_ids,
                staged_blob_ids=staged_blob_ids,
                identity=identity,
            )

        # --- S12 FINAL_PRE_COMMIT_CHECK -----------------------------------
        if message is None:  # pragma: no cover - defensive
            raise GitCommitError("T20 reached the commit stage without a message.")
        recheck_repository = self._identify_repository(root, pre_staging=False)
        recheck_identity = self._inspect_identity(root)
        recheck_operations = self._operations_in_progress(
            recheck_repository.git_dir, recheck_repository.common_dir
        )
        recheck_unmerged = self._unmerged_paths(root)
        recheck_staged = self._staged_blob_ids(root, approved.paths)
        recheck_worktree = self._worktree_content_ids(root, approved.paths)
        facts = replace(
            facts,
            repository=recheck_repository,
            identity=recheck_identity,
            operation_in_progress=recheck_operations,
            unmerged_paths=recheck_unmerged,
            pre_commit_stage=StageObservation(
                attempted=True,
                approved_paths=tuple(approved.paths),
                staged_paths=tuple(sorted(recheck_staged)),
                staged_blob_ids=dict(recheck_staged),
                worktree_content_ids=dict(recheck_worktree),
                approved_content_ids=dict(approved_ids),
                head_before=recheck_repository.head_commit,
                head_after=recheck_repository.head_commit,
                unmerged_paths=recheck_unmerged,
            ),
        )
        record(
            "S12-final-pre-commit-check",
            f"HEAD={recheck_repository.head_commit!r}, "
            f"staged={list(sorted(recheck_staged))}, "
            f"in_progress={list(recheck_operations)}, "
            f"unmerged={list(recheck_unmerged)}.",
        )
        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.COMMIT)
        if evaluation.is_refusal:
            record("S12-final-pre-commit-check", evaluation.refusal_reason or "Refused.")
            return self._result(
                CommitStatus.REFUSED,
                facts,
                CommitPhase.COMMIT,
                evaluation.refusal_reason
                or "The final pre-commit check refused the commit."
                " Nothing was committed and nothing was restaged.",
                records,
                approved_content_ids=approved_ids,
                staged_blob_ids=recheck_staged,
                identity=identity,
            )

        # --- S13 COMMIT (the second and last Git write, one attempt) -------
        expected_name = identity.name or identity.actor_name
        expected_email = identity.email or identity.actor_email
        overrides: List[str] = []
        if identity.name:
            overrides += ["-c", f"user.name={identity.name}"]
        if identity.email:
            overrides += ["-c", f"user.email={identity.email}"]
        if identity.name and identity.email:
            # Force Git to refuse a fabricated identity rather than guess one.
            overrides += ["-c", "user.useConfigOnly=true"]
        attempt = self._run(
            root, *overrides, "commit", "-m", message.text, allow_failure=True
        )
        commit_error: Optional[str] = None
        if int(getattr(attempt, "returncode", 1)) != 0:
            detail = redact_credentials(
                str(getattr(attempt, "stderr", "") or "").strip()
            )
            commit_error = detail or (
                f"git commit exited with code {getattr(attempt, 'returncode', '?')}"
            )
        record(
            "S13-commit",
            "Exactly one git commit attempt was made and failed: " + commit_error
            if commit_error
            else "Exactly one git commit attempt was made and succeeded.",
        )

        if commit_error is not None:
            # No cleanup, no reset, no restore, no unstage, no retry: the staged
            # state is left exactly as it is for a human to inspect.
            facts = replace(
                facts,
                commit=CommitObservation(
                    attempted=True,
                    error=commit_error,
                    committed=False,
                    previous_head=recheck_repository.head_commit,
                    expected_previous_head=recheck_repository.head_commit,
                    expected_message=message.text,
                    expected_identity_name=expected_name,
                    expected_identity_email=expected_email,
                    expected_paths=tuple(approved.paths),
                    expected_blob_ids=dict(recheck_staged),
                    expected_branch=recheck_repository.branch,
                    expected_worktree_root=recheck_repository.worktree_root,
                    unexpected_errors=(commit_error,),
                ),
            )
            return self._result(
                CommitStatus.COMMIT_FAILED,
                facts,
                CommitPhase.VERIFY,
                "The single git commit attempt failed; the staged state was left "
                "untouched and no automatic recovery was attempted.",
                records,
                approved_content_ids=approved_ids,
                staged_blob_ids=recheck_staged,
                identity=identity,
            )

        # --- S14 VERIFY_COMMIT --------------------------------------------
        previous_head = recheck_repository.head_commit
        new_head = self._text(root, "rev-parse", "HEAD")
        parent_head = self._text(root, "rev-parse", "HEAD^")
        count_text = self._text(root, "rev-list", "--count", f"{previous_head}..HEAD")
        new_commit_count: Optional[int] = None
        if count_text is not None and count_text.isdigit():
            new_commit_count = int(count_text)

        message_probe = self._run(
            root, "log", "-1", "--format=%B", allow_failure=True
        )
        message_out: Optional[str] = None
        if int(getattr(message_probe, "returncode", 1)) == 0:
            message_out = str(getattr(message_probe, "stdout", "") or "").rstrip("\n")
        author_name = self._text(root, "log", "-1", "--format=%an")
        author_email = self._text(root, "log", "-1", "--format=%ae")
        committer_name = self._text(root, "log", "-1", "--format=%cn")
        committer_email = self._text(root, "log", "-1", "--format=%ce")
        branch_after = self._text(root, "symbolic-ref", "--short", "-q", "HEAD")
        worktree_root_text = self._text(root, "rev-parse", "--show-toplevel")
        # Normalise exactly like RepositoryIdentity does, so the post-commit
        # comparison is a like-for-like string comparison.
        worktree_root_after = (
            str(Path(worktree_root_text).resolve()) if worktree_root_text else None
        )

        committed_blob_ids: Dict[str, str] = {}
        tree_probe = self._run(
            root, "ls-tree", "-r", "-z", "HEAD", "--", *approved.paths
        )
        for entry in str(getattr(tree_probe, "stdout", "") or "").split("\0"):
            if not entry:
                continue
            meta, _separator, path = entry.partition("\t")
            fields = meta.split()
            if len(fields) >= 3 and path:
                committed_blob_ids[path] = fields[2]

        change_probe = self._run(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "HEAD",
        )
        change_records = _parse_name_status_z(
            str(getattr(change_probe, "stdout", "") or "")
        )
        committed_paths = tuple(
            sorted(path for status, path, _origin in change_records if status != "D")
        )
        deleted_paths = tuple(
            sorted(path for status, path, _origin in change_records if status == "D")
        )
        renamed_paths = tuple(
            sorted(
                path
                for status, path, _origin in change_records
                if status in ("R", "C")
            )
        )

        # Post-commit consistency: a post-commit hook (or any other process)
        # that rewrites an approved file must be visible and must never be
        # silently accepted.
        post_ids = self._worktree_content_ids(root, approved.paths)
        post_status_probe = self._run(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *approved.paths,
            allow_failure=True,
        )
        post_status = str(getattr(post_status_probe, "stdout", "") or "").strip(
            "\0 \n"
        )

        problems: List[str] = []
        unexpected_errors: List[str] = []
        if parent_head is None:
            unexpected_errors.append(
                "the new commit has no readable parent (history may have been "
                "rewritten)"
            )
        if new_commit_count is None:
            unexpected_errors.append("the number of new commits could not be determined")
        if message_out is None:
            unexpected_errors.append("the committed message could not be read")
        if not committed_blob_ids:
            unexpected_errors.append(
                "no committed tree entry could be read for the approved paths"
            )
        for path, blob_id in dict(recheck_staged).items():
            if post_ids.get(path) != blob_id:
                problems.append(
                    f"{path!r} no longer matches the committed content after the "
                    "commit (a hook or another process modified it)"
                )
        if post_status:
            problems.append(
                "the approved path(s) are not clean after the commit: " + post_status
            )

        facts = replace(
            facts,
            commit=CommitObservation(
                attempted=True,
                committed=True,
                previous_head=previous_head,
                expected_previous_head=previous_head,
                new_head=new_head,
                parent_head=parent_head,
                new_commit_count=new_commit_count,
                message=message_out,
                expected_message=message.text,
                author_name=author_name,
                author_email=author_email,
                committer_name=committer_name,
                committer_email=committer_email,
                expected_identity_name=expected_name,
                expected_identity_email=expected_email,
                committed_paths=committed_paths,
                expected_paths=tuple(approved.paths),
                committed_blob_ids=dict(committed_blob_ids),
                expected_blob_ids=dict(recheck_staged),
                deleted_paths=deleted_paths,
                renamed_paths=renamed_paths,
                branch_after=branch_after,
                expected_branch=recheck_repository.branch,
                worktree_root_after=worktree_root_after,
                expected_worktree_root=recheck_repository.worktree_root,
                unexpected_errors=tuple(unexpected_errors),
                problems=tuple(problems),
            ),
        )
        record(
            "S14-verify-commit",
            f"HEAD {previous_head} -> {new_head}, parent={parent_head!r}, "
            f"new_commits={new_commit_count}, paths={list(committed_paths)}.",
        )
        evaluation = evaluate_commit_gates(facts, phase=CommitPhase.VERIFY)
        if evaluation.is_refusal:
            return self._result(
                CommitStatus.COMMIT_UNVERIFIED,
                facts,
                CommitPhase.VERIFY,
                "A commit exists but its correctness could not be proven ("
                + (evaluation.refusal_reason or "no detail")
                + "). No recovery was attempted.",
                records,
                approved_content_ids=approved_ids,
                staged_blob_ids=recheck_staged,
                new_head=new_head,
                identity=identity,
            )
        return self._result(
            CommitStatus.COMMITTED,
            facts,
            CommitPhase.VERIFY,
            f"Exactly one verified commit was created ({previous_head} -> "
            f"{new_head}) containing only the approved paths.",
            records,
            approved_content_ids=approved_ids,
            staged_blob_ids=recheck_staged,
            new_head=new_head,
            identity=identity,
        )


def commit_safely(
    *,
    repository_path: object,
    issue_status: object,
    **kwargs: object,
) -> CommitResult:
    """One-call wrapper around :meth:`GitCommitExecutor.commit_safely`.

    The executor is the injectable, testable unit; this helper exists so a
    caller can express "commit this verified fix safely" in one line using the
    production runner, the system Git and the conservative default policy.
    """
    executor = GitCommitExecutor()
    return executor.commit_safely(
        repository_path=repository_path, issue_status=issue_status, **kwargs
    )


__all__: Sequence[str] = (
    "ALLOWED_GIT_SUBCOMMANDS",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "MAX_APPROVED_FILE_BYTES",
    "CommitResult",
    "CommitStatus",
    "GitCommitError",
    "GitCommitExecutor",
    "GitRunner",
    "StageRecord",
    "commit_safely",
)











