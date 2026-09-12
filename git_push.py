"""T21 safe Git push executor - the only module that writes to a remote.

This module owns the one and only Git mutation T21 is allowed to perform:

1. **exactly one push** -
   ``git push <remote> refs/heads/<branch>:refs/heads/<remote_branch>``

Everything else it runs is read-only (``rev-parse``, ``rev-list``, ``status``,
``diff``, ``ls-remote``, ``merge-base``, ``config``, ``var``, ``symbolic-ref``,
``show-ref``, ``log``, ``ls-files``, ``ls-tree``, ``cat-file``). The mutation
boundary is enforced in code, not by convention:

* :data:`ALLOWED_GIT_SUBCOMMANDS` is an allow-list; any other subcommand raises
  :class:`GitPushError` *before* a process is launched.
* :data:`FORBIDDEN_GIT_SUBCOMMANDS` names the destructive/network operations
  explicitly - including ``add``/``commit`` (T20 owns them) and ``fetch`` /
  ``pull`` / ``clone`` - so an accidental rewrite or fetch can never run, even
  if a future edit tries to.
* ``git push`` must carry exactly ``<remote> <refspec>``: no option, no ``--``,
  no force token, no wildcard, no deletion, and both refspec sides must be fully
  qualified. Nothing on the command line can be redirected by configuration.
* ``-c`` overrides are never passed (see :data:`_ALLOWED_CONFIG_OVERRIDES`), so
  no command can inject ``core.hooksPath``, ``core.sshCommand`` or another
  hook/exec vector.

State machine (each ``S`` only runs when every earlier gate passed):

    S0  VALIDATE_INPUTS       G1
    S1  CHECK_T20_CONTRACT    G2-G5 (the T20 result is only projected/read)
    S2  CHECK_ENVIRONMENT     G10 (before any Git process)
    S3  IDENTIFY_REPOSITORY   G6-G9 (read-only)
    S4  CHECK_BRANCH          G11-G15
    S5  CHECK_COMMIT          G16-G18
    S6  CHECK_WORKTREE        G19-G21
    S7  INSPECT_REMOTE        G22-G26 (read-only ``ls-remote``)
    S8  RESOLVE_TARGET        G27-G36
    S9  PROVE_ANCESTRY        G37, G38
    S10 FINAL_PRE_PUSH_CHECK  G39-G42 (two readings, then the checkpoint)
    S11 PUSH                  G43 (the single and only Git mutation)
    S12 VERIFY_REMOTE         G44-G46 (read-only read back)
    S13 VERIFY_LOCAL          G47, G48

The push itself is a plain, fully qualified, non-forced fast-forward, so remote
history can never be rewritten: if the destination moved since it was inspected,
Git rejects the push and T21 reports ``PUSH_FAILED``. T21 never forces, never
retries and never fetches.

Failure handling is deliberately passive: on a push failure T21 does **not**
force, retry, fetch, pull, reset, clean or unstage. The remote and the working
copy are left exactly as they are and the result says so. A push that ran but
cannot be proven becomes ``PUSH_UNVERIFIED`` - a loud failure for a human, never
a silent pass.

Safety notes
------------
* ``shell=False`` everywhere; every command is an argv array.
* Git runs with a sanitized environment (unsafe ``GIT_*`` variables are refused
  and removed) plus ``GIT_TERMINAL_PROMPT=0``, so no command can block on an
  interactive prompt or be redirected to another repository.
* The destination remote is contacted read-only until ``S11``: ``ls-remote`` and
  the post-push read back never write, and the push never fetches objects.
* Remote URLs and Git output are credential-redacted before they reach a reason,
  an observation or an exception, and secret values are never logged.
* No ``--force``/``--mirror``/``--tags``, no automatic ``git config`` write and
  no remote change other than the one destination branch.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from commit_policy import (
    OPERATION_IN_PROGRESS_MARKERS,
    UNSAFE_GIT_ENVIRONMENT_VARIABLES,
    GitEnvironmentReport,
)
from push_policy import (
    PUSH_SUBCOMMAND,
    REFSPEC_FORCE_PREFIX,
    REFSPEC_NAMESPACE,
    REFSPEC_WILDCARD,
    PushCheckpoint,
    PushFacts,
    PushGateEvaluation,
    PushObservation,
    PushPhase,
    PushPolicyConfig,
    PushRequest,
    PushVerificationStatus,
    RemoteObservation,
    RepositoryObservation,
    evaluate_push_gates,
    is_full_commit_id,
    normalize_commit_id,
    parse_refspec,
    url_has_userinfo,
    validate_remote_url,
)
from repository import redact_credentials

#: Read-only Git subcommands T21 may run, plus the one mutation (``push``).
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "rev-list",
        "status",
        "diff",
        "ls-files",
        "ls-tree",
        "show-ref",
        "log",
        "cat-file",
        "config",
        "var",
        "symbolic-ref",
        "merge-base",
        "ls-remote",
        "push",
    }
)

#: Destructive/network subcommands that must never run. This is T20's forbidden
#: list without ``push`` (T21 owns it now) plus ``add``/``commit`` (T20's
#: mutations), so neither executor can ever perform the other's write.
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
        "add",
        "commit",
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
#: executes. T21 builds its own single ``-C <root>`` *outside* the validated
#: arguments, so none of these may appear among the arguments T21 validates;
#: otherwise a future edit could push from another repository.
_FORBIDDEN_REDIRECT_OPTIONS = frozenset(
    {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

#: Long options that are never acceptable in any T21 command.
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
        "--force",
        "--force-with-lease",
        "--force-if-includes",
        "--delete",
        "--mirror",
        "--all",
        "--tags",
        "--follow-tags",
        "--prune",
        "--dry-run",
        "--atomic",
        "--push-option",
        "--repo",
        "--receive-pack",
        "--upload-pack",
        "--exec",
        "--server-option",
        "--recurse-submodules",
    }
)

#: Options that are only dangerous for the subcommands that accept them. The
#: ``push`` entry documents the shapes T21 refuses even before the argv-shape
#: rule below rejects them; the other entries keep the read-only probes narrow.
_FORBIDDEN_OPTIONS_BY_SUBCOMMAND: Mapping[str, frozenset] = {
    "push": frozenset(
        {
            "-f",
            "-d",
            "-u",
            "-o",
            "-v",
            "-q",
            "--force",
            "--force-with-lease",
            "--force-if-includes",
            "--delete",
            "--all",
            "--mirror",
            "--tags",
            "--follow-tags",
            "--prune",
            "--dry-run",
            "--set-upstream",
            "--set-upstream-to",
            "--repo",
            "--receive-pack",
            "--exec",
            "--upload-pack",
            "--no-verify",
            "--atomic",
            "--porcelain",
            "--verbose",
            "--progress",
            "--quiet",
            "--thin",
            "--no-thin",
            "--ipv4",
            "--ipv6",
            "--signed",
            "--no-signed",
            "--push-option",
            "--no-tags",
            "--recurse-submodules",
        }
    ),
    "ls-remote": frozenset(
        {"--upload-pack", "--exec", "--server-option", "--get-url", "--symref"}
    ),
    "merge-base": frozenset(
        {"--octopus", "--independent", "--fork-point", "-a", "--all"}
    ),
    "status": frozenset({"--ignored", "--untracked-files", "-u", "--short"}),
    "diff": frozenset({"--no-index", "--output", "--ext-diff", "--textconv"}),
}

#: ``-c`` overrides T21 is allowed to pass. The tuple is empty on purpose: T21
#: never overrides Git configuration, so no command can move a hook, a filter, a
#: credential helper or a template.
_ALLOWED_CONFIG_OVERRIDES: Tuple[str, ...] = ()

#: ``git config`` verbs T21 may use. Each one only *reads* configuration, so a
#: command without one of them can only be a write and is refused.
_CONFIG_READ_VERBS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
)

#: ``git config`` options T21 accepts *in addition to* a read verb. They narrow
#: which file or value is read; on their own (``git config --local user.name x``)
#: they are the scope of a **write**, which is why a read verb is always required.
_CONFIG_READER_OPTIONS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
        "--show-origin",
        "--show-scope",
        "--name-only",
        "--null",
        "-z",
        "--type",
        "--default",
        "--local",
        "--global",
        "--system",
        "--worktree",
        "--file",
        "-f",
        "--blob",
    }
)

#: ``git config`` options that change configuration. None of them may ever run.
_CONFIG_WRITER_OPTIONS = frozenset(
    {
        "--add",
        "--unset",
        "--unset-all",
        "--replace-all",
        "--rename-section",
        "--remove-section",
        "--edit",
        "-e",
    }
)

#: Environment T21 always forces for Git children (no prompt, no pager, no lock).
_SAFE_GIT_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
}

#: Maximum number of characters of Git output kept in an observation.
MAX_PUSH_OUTPUT_CHARS = 4000

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


class GitPushError(Exception):
    """Raised when T21 cannot even attempt its state machine safely.

    This is a *caller/environment* error (missing inputs, an unusable repository
    path, broken Git, a forbidden command attempt). Refusals, push failures and
    unverified pushes are results, not exceptions.
    """


def _split_git_invocation(args: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
    """Split ``args`` into ``(subcommand, args after it)``.

    Global options that take a separate value (``-C <path>``, ``-c <k=v>``, ...)
    are skipped, so the subcommand is found even when T21 passes ``-C``.
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


def _redact(value: object) -> str:
    """Redact credentials from any value before it becomes text T21 keeps."""
    if value is None:
        return ""
    return redact_credentials(str(value))


def _clip(text: str) -> str:
    """Keep captured output bounded, so a result can never explode in size."""
    if len(text) <= MAX_PUSH_OUTPUT_CHARS:
        return text
    return text[:MAX_PUSH_OUTPUT_CHARS] + "...<truncated>"


def _absolutize(base: Optional[str], value: Optional[str]) -> Optional[str]:
    """Make a Git-reported path absolute against ``base`` (never touches disk).

    ``git rev-parse --git-common-dir`` and ``--git-path`` report a path relative
    to the working tree, so an un-absolutized value would compare wrongly against
    the expected root. Git reports forward slashes on every platform; the result
    is textual, and :func:`push_policy.same_path` normalizes it for comparison.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or (
        len(normalized) > 1 and normalized[1] == ":"
    ):
        return text
    root = str(base or "").replace("\\", "/").rstrip("/")
    if not root:
        return text
    return root + "/" + normalized


def _index_is_locked(index_path: Optional[str]) -> bool:
    """True when Git's own index lock file exists (a read-only ``stat``)."""
    if not index_path:
        return False
    return os.path.exists(str(index_path) + ".lock")


def _as_bool(value: object) -> Optional[bool]:
    """Return ``value`` when it is a real ``bool``, else ``None`` (fail closed)."""
    return value if isinstance(value, bool) else None


def _opt_text(value: object) -> Optional[str]:
    """Return the stripped text of ``value``, or ``None`` when it is empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _t20_view(commit_result: object) -> Mapping[str, object]:
    """Project a T20 result into a read-only mapping (it is never mutated).

    ``git_commit.CommitResult.as_dict()`` is used when available; a plain mapping
    is accepted as-is so a serialized T20 report can drive T21 too. Anything else
    (or a broken projection) yields ``{}``, which makes G2 refuse - a silent pass
    is impossible.
    """
    if isinstance(commit_result, Mapping):
        return commit_result
    as_dict = getattr(commit_result, "as_dict", None)
    if not callable(as_dict):
        return {}
    try:
        view = as_dict()
    except Exception:  # pragma: no cover - defensive: unprojectable T20 result
        return {}
    return view if isinstance(view, Mapping) else {}


def _validate_push_body(rest: Sequence[str]) -> None:
    """Enforce the exact ``git push <remote> <refspec>`` shape.

    Raises:
        GitPushError: the push carries options, extra operands, a force token, a
            wildcard, a deletion or a non-fully-qualified refspec.
    """
    if len(rest) != 2:
        raise GitPushError(
            "T21 pushes with exactly '<remote> <refspec>' and nothing else; "
            f"refusing the push arguments {tuple(rest)!r}."
        )
    remote, refspec = str(rest[0]), str(rest[1])
    for token in (remote, refspec):
        if token.startswith("-"):
            raise GitPushError(
                f"T21 never passes an option to 'git push' (got {token!r})."
            )
    if not remote.strip():
        raise GitPushError("T21 requires a non-empty remote name for 'git push'.")
    parts = parse_refspec(refspec)
    if parts is None:
        raise GitPushError(f"The push refspec {refspec!r} is malformed.")
    if parts.force or refspec.startswith(REFSPEC_FORCE_PREFIX):
        raise GitPushError(
            f"The push refspec {refspec!r} carries a force token; T21 never "
            "rewrites remote history."
        )
    if not parts.source or not parts.destination:
        raise GitPushError(
            f"The push refspec {refspec!r} has an empty side, which would delete "
            "a remote branch."
        )
    if parts.is_wildcard or REFSPEC_WILDCARD in refspec:
        raise GitPushError(
            f"The push refspec {refspec!r} contains a wildcard, which would push "
            "more than one branch."
        )
    if not parts.is_fully_qualified:
        raise GitPushError(
            f"The push refspec {refspec!r} is not fully qualified (both sides "
            f"must start with {REFSPEC_NAMESPACE!r}), so a push.default or "
            "remote.<name>.push rule could redirect it."
        )


def _validate_config_body(rest: Sequence[str]) -> None:
    """Allow ``git config`` for reading only.

    T21 reads configuration; it never writes it, so a push can never change a
    hook path, a remote URL or a credential helper.
    """
    if not rest:
        raise GitPushError(
            "T21 reads Git configuration only with an explicit reader option "
            "(for example 'config --get <key>')."
        )
    for token in rest:
        if token in _CONFIG_WRITER_OPTIONS:
            raise GitPushError(
                f"T21 never writes Git configuration (refusing 'config {token}')."
            )
    if not any(token in _CONFIG_READ_VERBS for token in rest):
        raise GitPushError(
            "T21 reads Git configuration only with a read verb such as "
            "'--get', '--get-all', '--get-regexp' or '--list'; a scope option "
            "alone would be a configuration write."
        )


def _validate_git_invocation(args: Sequence[str]) -> str:
    """Validate one Git argv and return its subcommand.

    This is the mutation boundary: it runs *before* any process is launched, so
    a forbidden subcommand, an option T21 never uses, a repository-redirecting
    global option or an invalid push shape fails as a :class:`GitPushError`
    without touching the repository or the remote.

    Raises:
        GitPushError: the command is not one T21 may run.
    """
    tokens = tuple(str(token) for token in args)
    if not tokens:
        raise GitPushError("Refusing to run Git without arguments.")
    subcommand, rest = _split_git_invocation(tokens)
    if not subcommand:
        raise GitPushError(
            f"Refusing to run Git without a subcommand: {list(tokens)}."
        )
    if subcommand in FORBIDDEN_GIT_SUBCOMMANDS:
        raise GitPushError(
            f"Refusing to run the forbidden Git subcommand {subcommand!r}."
        )
    if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
        raise GitPushError(
            f"Refusing to run the Git subcommand {subcommand!r}; T21 allows only "
            + ", ".join(sorted(ALLOWED_GIT_SUBCOMMANDS))
            + "."
        )
    for index, token in enumerate(tokens):
        if not token.startswith("-") or token == "--":
            continue
        head = token.split("=", 1)[0] if token.startswith("--") else token
        redirect = head in _FORBIDDEN_REDIRECT_OPTIONS or (
            len(head) > 2 and head[:2] in _FORBIDDEN_REDIRECT_OPTIONS
        )
        if redirect:
            raise GitPushError(
                f"Refusing the repository-redirecting Git option {token!r}; T21 "
                "always targets the repository it discovered itself."
            )
        if head in _FORBIDDEN_OPTIONS_GLOBAL:
            raise GitPushError(f"Refusing the Git option {token!r}.")
        if token == "-c" or (token.startswith("-c") and not token.startswith("--")):
            value = (
                tokens[index + 1]
                if token == "-c" and index + 1 < len(tokens)
                else token[2:]
            )
            if value not in _ALLOWED_CONFIG_OVERRIDES:
                raise GitPushError(
                    "Refusing to pass the Git configuration override "
                    f"'-c {value}'; T21 never overrides Git configuration."
                )
    forbidden = _FORBIDDEN_OPTIONS_BY_SUBCOMMAND.get(subcommand, frozenset())
    for token in rest:
        if token in forbidden:
            raise GitPushError(f"Refusing the '{subcommand}' option {token!r}.")
        if token.startswith("--") and "=" in token:
            if token.split("=", 1)[0] in forbidden:
                raise GitPushError(f"Refusing the '{subcommand}' option {token!r}.")
    if subcommand == PUSH_SUBCOMMAND:
        _validate_push_body(rest)
    elif subcommand == "config":
        _validate_config_body(rest)
    return subcommand


class PushStatus(Enum):
    """Executable outcome of one T21 attempt."""

    __test__ = False

    #: Exactly one verified push happened and the remote was read back.
    PUSHED = "pushed"
    #: A gate refused before the push; not a single Git push was attempted.
    REFUSED = "refused"
    #: The single ``git push`` attempt ran (or could not be launched) and failed.
    PUSH_FAILED = "push-failed"
    #: A push was attempted but its effect on the remote could not be proven.
    PUSH_UNVERIFIED = "push-unverified"


@dataclass(frozen=True)
class PushStageRecord:
    """One line of the T21 state-machine trail (never contains a secret)."""

    stage: str
    detail: str

    def as_dict(self) -> dict:
        """Plain summary."""
        return {"stage": self.stage, "detail": self.detail}


@dataclass(frozen=True)
class PushResult:
    """Typed, secret-free outcome of one T21 attempt.

    Attributes:
        status: the executable outcome (:class:`PushStatus`).
        reason: short human-readable explanation.
        gates: the final gate evaluation (all 48 gates).
        stage_records: the state-machine trail.
        request: the :class:`PushRequest` that was executed (never mutated).
        repository: the resolved repository identity.
        remote: the destination remote as it was inspected *before* the push.
        push: the single push attempt, recorded verbatim.
        checkpoint: the local state captured before the push was authorised.
        final_checkpoint: the local state captured immediately before the push.
        expected_commit: the recorded commit T21 was asked to push.
        remote_before_commit / remote_after_commit: the destination branch on the
            remote before and after the push (``None`` when it did not exist).
        branch / remote_branch / refspec: the exact source, destination and refspec.
    """

    __test__ = False

    status: PushStatus
    reason: str
    gates: PushGateEvaluation
    stage_records: Tuple[PushStageRecord, ...] = ()
    request: Optional[PushRequest] = None
    repository: Optional[RepositoryObservation] = None
    remote: Optional[RemoteObservation] = None
    push: Optional[PushObservation] = None
    checkpoint: Optional[PushCheckpoint] = None
    final_checkpoint: Optional[PushCheckpoint] = None
    expected_commit: Optional[str] = None
    remote_before_commit: Optional[str] = None
    remote_after_commit: Optional[str] = None
    branch: Optional[str] = None
    remote_branch: Optional[str] = None
    refspec: Optional[str] = None

    @property
    def is_pushed(self) -> bool:
        """True only for a fully verified push."""
        return self.status is PushStatus.PUSHED

    @property
    def is_refusal(self) -> bool:
        """True when a gate refused before any push was attempted."""
        return self.status is PushStatus.REFUSED

    @property
    def push_attempted(self) -> bool:
        """True when the one push command was actually attempted."""
        return bool(self.push is not None and self.push.attempted)

    @property
    def needs_attention(self) -> bool:
        """True when a human must look at the remote (no auto-recovery)."""
        return self.status in (
            PushStatus.PUSH_FAILED,
            PushStatus.PUSH_UNVERIFIED,
        )

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "stage_records": [record.as_dict() for record in self.stage_records],
            "request": self.request.as_dict() if self.request else None,
            "repository": (
                self.repository.as_dict() if self.repository else None
            ),
            "remote": self.remote.as_dict() if self.remote else None,
            "push": self.push.as_dict() if self.push else None,
            "checkpoint": (
                self.checkpoint.as_dict() if self.checkpoint else None
            ),
            "final_checkpoint": (
                self.final_checkpoint.as_dict() if self.final_checkpoint else None
            ),
            "expected_commit": self.expected_commit,
            "remote_before_commit": self.remote_before_commit,
            "remote_after_commit": self.remote_after_commit,
            "branch": self.branch,
            "remote_branch": self.remote_branch,
            "refspec": self.refspec,
            "push_attempted": self.push_attempted,
            "is_pushed": self.is_pushed,
            "is_refusal": self.is_refusal,
            "needs_attention": self.needs_attention,
            "gates": self.gates.as_dict(),
        }


class GitPushExecutor:
    """Push exactly one verified commit, exactly once (T21).

    Args:
        git_command: Git executable name or path (default ``"git"``).
        timeout_seconds: per-command timeout. The push gets the same budget,
            because the destination remote must answer in bounded time.
        runner: optional injectable process runner
            ``(args, cwd, env, timeout) -> completed``. Tests use it to observe
            the argv of the one push without touching a real remote.
        config: the :class:`PushPolicyConfig` (conservative defaults).
        environment: the environment mapping to inspect (defaults to
            ``os.environ``). The caller's environment is never mutated.
    """

    def __init__(
        self,
        *,
        git_command: str = "git",
        timeout_seconds: float = 60.0,
        runner: Optional[GitRunner] = None,
        config: Optional[PushPolicyConfig] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._git_command = git_command
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _subprocess_git_runner if runner is None else runner
        self._config = config or PushPolicyConfig()
        self._environment = dict(
            os.environ if environment is None else environment
        )
        self._sanitized_environment = self._build_sanitized_environment()

    # ------------------------------------------------------------------
    # Environment and process plumbing
    # ------------------------------------------------------------------

    def _build_sanitized_environment(self) -> Dict[str, str]:
        """Return the environment Git children receive.

        ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``/``GIT_SSH_COMMAND``/...
        are removed, so a child can never be redirected to another repository,
        index, object store or program, and ``GIT_TERMINAL_PROMPT=0`` guarantees
        no command can block on or answer an interactive prompt. The caller's
        environment mapping is left untouched.
        """
        sanitized = {
            name: value
            for name, value in self._environment.items()
            if name not in UNSAFE_GIT_ENVIRONMENT_VARIABLES
        }
        sanitized.update(_SAFE_GIT_ENVIRONMENT)
        return sanitized

    def _invoke(self, root: Path, args: Sequence[str]) -> object:
        """Launch one already-validated Git argv (``shell=False``, argv array)."""
        command = [self._git_command, "-C", str(root), *args]
        try:
            return self._runner(
                command,
                str(root),
                dict(self._sanitized_environment),
                self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GitPushError(
                f"Git executable not found (configured '{self._git_command}'): "
                f"{_redact(exc)}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitPushError(
                f"Git command timed out after {self._timeout_seconds:g} seconds: "
                f"{list(args)}"
            ) from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise GitPushError(_redact(f"Failed to run Git: {exc}")) from exc

    def _run(self, root: Path, *args: str, allow_failure: bool = False) -> object:
        """Run one validated, read-only Git command with a sanitized environment.

        Raises:
            GitPushError: the command is not allowed, Git cannot be launched, it
                times out, or it fails and ``allow_failure`` is ``False``.
        """
        _validate_git_invocation(args)
        completed = self._invoke(root, args)
        if not allow_failure and int(getattr(completed, "returncode", 1)) != 0:
            detail = _redact(
                str(getattr(completed, "stderr", "") or "").strip()
            )
            raise GitPushError(
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

    def _lines(self, root: Path, *args: str) -> Tuple[str, ...]:
        """Run a read-only Git command and return its non-empty stdout lines."""
        text = self._text(root, *args)
        if not text:
            return ()
        return tuple(
            line.strip() for line in text.splitlines() if line.strip()
        )

    def _config_values(self, root: Path, key: str) -> Tuple[str, ...]:
        """Read every value of one Git configuration key (read-only)."""
        return self._lines(root, "config", "--get-all", key)

    def _inspect_environment(self) -> GitEnvironmentReport:
        """G10 evidence: which Git environment variables are unsafe here."""
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
            reason = (
                "No redirecting or code-executing Git environment variable is set."
            )
        return GitEnvironmentReport(
            unsafe_variables=unsafe,
            checked_variables=UNSAFE_GIT_ENVIRONMENT_VARIABLES,
            reason=reason,
        )
    def _project_t20(self, commit_result: object) -> Dict[str, object]:
        """Project the T20 result into ``PushFacts`` fields (read-only, G2-G5).

        Nothing is inferred: a field the T20 result does not carry stays ``None``,
        which every gate treats as a failure. The result object is never mutated
        and never executed - only ``as_dict()`` is read.
        """
        view = _t20_view(commit_result)
        repository = view.get("repository")
        repository_view: Mapping[str, object] = (
            repository if isinstance(repository, Mapping) else {}
        )
        status = view.get("status")
        repository_root = (
            repository_view.get("worktree_root")
            or repository_view.get("expected_root")
        )
        branch = repository_view.get("branch")
        return {
            "t20_status": None if status is None else str(status),
            "t20_is_committed": _as_bool(view.get("is_committed")),
            "t20_new_head": _opt_text(view.get("new_head")),
            "t20_previous_head": _opt_text(view.get("previous_head")),
            "t20_commit_sha": _opt_text(view.get("commit_sha")),
            "t20_needs_attention": _as_bool(view.get("needs_attention")),
            "t20_repository_valid": _as_bool(repository_view.get("is_valid")),
            "t20_repository_root": _opt_text(repository_root),
            "t20_repository_branch": _opt_text(branch),
        }

    def _identify_repository(
        self, root: Path, expected_root: str
    ) -> RepositoryObservation:
        """Resolve the repository identity with read-only Git (G6-G9 evidence)."""
        worktree_root = self._text(root, "rev-parse", "--show-toplevel")
        if not worktree_root:
            return RepositoryObservation(
                expected_root=expected_root,
                problems=(
                    "the repository root could not be resolved with "
                    "'git rev-parse --show-toplevel': the path is not a Git "
                    "working copy",
                ),
            )
        problems: List[str] = []
        git_dir = _absolutize(
            worktree_root, self._text(root, "rev-parse", "--absolute-git-dir")
        )
        common_dir = _absolutize(
            worktree_root, self._text(root, "rev-parse", "--git-common-dir")
        )
        index_path = _absolutize(
            worktree_root, self._text(root, "rev-parse", "--git-path", "index")
        )
        if not git_dir:
            problems.append("the Git directory could not be resolved")
        if not common_dir:
            problems.append("the Git common directory could not be resolved")
        if not index_path:
            problems.append("the index location could not be resolved")
        bare_text = self._text(root, "rev-parse", "--is-bare-repository")
        head_commit = self._text(root, "rev-parse", "--verify", "HEAD")
        return RepositoryObservation(
            expected_root=expected_root,
            worktree_root=worktree_root,
            git_dir=git_dir,
            common_dir=common_dir,
            index_path=index_path,
            index_locked=_index_is_locked(index_path),
            is_bare=(
                None if bare_text is None else bare_text.strip().casefold() == "true"
            ),
            head_exists=head_commit is not None,
            head_commit=head_commit,
            branch=self._text(root, "symbolic-ref", "--short", "-q", "HEAD"),
            problems=tuple(problems),
        )

    def _operation_markers(self, repository: RepositoryObservation) -> Tuple[str, ...]:
        """Names of in-progress Git operations, read from marker files (G21).

        Some markers are files (``MERGE_HEAD``) and some are directories
        (``rebase-merge``), so existence - never file-ness - decides.
        """
        directories: List[str] = []
        for candidate in (repository.git_dir, repository.common_dir):
            text = str(candidate or "").strip()
            if text and text not in directories:
                directories.append(text)
        found: List[str] = []
        for marker in OPERATION_IN_PROGRESS_MARKERS:
            for directory in directories:
                if os.path.exists(os.path.join(directory, marker)):
                    if marker not in found:
                        found.append(marker)
                    break
        return tuple(found)

    def _status_entries(self, root: Path) -> Tuple[Tuple[str, ...], bool]:
        """Read the worktree status once (G19/G20/G39-G41 evidence).

        ``git status --porcelain -z`` reports every staged, unstaged and
        untracked entry, NUL-separated, so no path can be mis-parsed. A rename or
        a copy also reports its original path as the next record; that record is
        consumed here instead of being mistaken for a second entry.

        Returns:
            ``(changed_files, is_clean)``. ``is_clean`` is ``True`` only when the
            status was read *and* reported nothing, which matches T20's notion of
            a clean worktree (nothing staged, unstaged or untracked).
        """
        completed = self._run(
            root, "status", "--porcelain", "-z", allow_failure=True
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            return (), False
        records = [
            chunk
            for chunk in str(getattr(completed, "stdout", "") or "").split("\0")
            if chunk
        ]
        changed: List[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            code = record[:2]
            path = record[3:] if len(record) > 3 else ""
            if path:
                changed.append(path)
            if code[:1] in ("R", "C") or code[1:2] in ("R", "C"):
                index += 2
                continue
            index += 1
        return tuple(changed), not changed

    def _staged_files(self, root: Path) -> Tuple[str, ...]:
        """Paths staged in the index, NUL-separated (G20 evidence, fail closed)."""
        completed = self._run(
            root, "diff", "--cached", "--name-only", "-z", allow_failure=True
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            return ("<the index could not be read>",)
        return tuple(
            chunk
            for chunk in str(getattr(completed, "stdout", "") or "").split("\0")
            if chunk
        )

    def _resolve_default_branch(self, root: Path, remote: str) -> Optional[str]:
        """Resolve the default branch from ``refs/remotes/<remote>/HEAD``.

        Only repository-local refs are read, so resolving the default branch
        never contacts the remote and never fetches. ``None`` means "not
        resolved" and G15/G29 turn that into a refusal rather than a guess;
        :attr:`PushPolicyConfig.default_branch` is the explicit override.
        """
        name = str(remote or "").strip()
        if not name:
            return None
        head = self._text(
            root, "symbolic-ref", "--short", "-q", f"refs/remotes/{name}/HEAD"
        )
        if not head:
            return None
        prefix = name + "/"
        candidate = (
            head[len(prefix):] if head.startswith(prefix) else head.rsplit("/", 1)[-1]
        )
        candidate = candidate.strip()
        if not candidate or candidate.casefold() == "head":
            return None
        return candidate

    def _rewrite_rules(
        self, root: Path, urls: Sequence[str], push_urls: Sequence[str]
    ) -> Tuple[str, ...]:
        """`url.*.insteadOf` rules that would rewrite this remote's URL (G24).

        Only rules whose base actually prefixes one of the remote's own URLs are
        reported, so an unrelated global rule cannot cause a false refusal. The
        rule text is credential-redacted before it is stored.
        """
        candidates = [str(item) for item in (*urls, *push_urls) if str(item or "").strip()]
        rules: List[str] = []
        for line in self._lines(root, "config", "--get-regexp", "^url\\."):
            key, _, value = line.partition(" ")
            if not key.casefold().endswith(".insteadof"):
                continue
            base = value.strip()
            if not base:
                continue
            if any(url.startswith(base) for url in candidates):
                rules.append(_redact(line))
        return tuple(rules)

    def _remote_branch_state(
        self, root: Path, remote: str, remote_branch: str
    ) -> Tuple[Optional[bool], Optional[str], Tuple[str, ...]]:
        """Read the destination branch on the remote, read-only (G23/G45/G46).

        ``git ls-remote --heads <remote> refs/heads/<remote_branch>`` only reads
        the remote; it never fetches objects and never writes. A read that fails
        (no network, no authentication) is reported as a problem, and the branch
        state stays unproven - which the gates turn into a refusal.

        Returns:
            ``(present, commit, problems)``; ``present`` is ``None`` when the
            remote could not be read.
        """
        ref = f"{REFSPEC_NAMESPACE}{remote_branch}"
        commits, problem = self._read_remote_ref(root, remote, ref)
        if commits is None:
            return None, None, (problem or "the destination ref could not be read",)
        if not commits:
            return False, None, ()
        if len(commits) > 1:
            return (
                True,
                None,
                (
                    f"the destination ref {ref!r} matched {len(commits)} entries on "
                    "the remote, so its value is ambiguous",
                ),
            )
        return True, commits[0], ()

    def _read_remote_ref(
        self, root: Path, remote: str, ref: str
    ) -> Tuple[Optional[Tuple[str, ...]], Optional[str]]:
        """Read one fully qualified ref on the remote, read-only (G23, G44-G46).

        ``git ls-remote --heads <remote> <ref>`` only reads: it transfers no
        objects and writes nothing, so it can never mutate the remote. A read that
        fails (no network, no authentication) is returned as data - the gates must
        see "unproven", never a crash.

        Returns:
            ``(commits, problem)``. ``commits`` is ``None`` when the read failed
            (``problem`` then explains why); otherwise it holds the commit ids the
            remote reported for ``ref``, which is empty when the ref does not
            exist.
        """
        completed = self._run(
            root, "ls-remote", "--heads", remote, ref, allow_failure=True
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            detail = _redact(str(getattr(completed, "stderr", "") or "").strip())
            return (
                None,
                "the destination ref could not be read with "
                f"'git ls-remote --heads {remote} {ref}'"
                + (f": {_clip(detail)}" if detail else ""),
            )
        commits: List[str] = []
        for line in str(getattr(completed, "stdout", "") or "").splitlines():
            payload = line.strip()
            if not payload:
                continue
            sha, _, name = payload.partition("\t")
            if name.strip() and name.strip() != ref:
                continue
            if sha.strip():
                commits.append(sha.strip())
        return tuple(commits), None

    def _inspect_remote(
        self, root: Path, request: PushRequest
    ) -> RemoteObservation:
        """Inspect the destination remote read-only, before anything is pushed.

        Nothing here writes: the remote's configuration is *read* with ``git
        config``, its refs are read with ``git ls-remote`` and the URLs are
        credential-redacted before they are stored, so an observation can never
        leak a secret.
        """
        name = str(request.remote or "").strip()
        remote_branch = str(request.remote_branch or "").strip()
        urls = self._config_values(root, f"remote.{name}.url")
        push_urls = self._config_values(root, f"remote.{name}.pushurl")
        push_refspecs = self._config_values(root, f"remote.{name}.push")
        raw_fetch = urls[0] if urls else None
        raw_push = push_urls[0] if push_urls else raw_fetch
        well_formed = (
            None
            if not raw_fetch
            else (
                validate_remote_url(raw_fetch) is None
                and (
                    raw_push == raw_fetch
                    or validate_remote_url(raw_push) is None
                )
            )
        )
        present, commit, problems = (None, None, ())
        if name and remote_branch:
            present, commit, problems = self._remote_branch_state(
                root, name, remote_branch
            )
        return RemoteObservation(
            name=name,
            exists=bool(urls or push_urls),
            urls=tuple(_redact(url) for url in urls),
            push_urls=tuple(_redact(url) for url in push_urls),
            fetch_url=_redact(raw_fetch) if raw_fetch else None,
            push_url=_redact(raw_push) if raw_push else None,
            url_well_formed=well_formed,
            url_userinfo=bool(
                (raw_fetch and url_has_userinfo(raw_fetch))
                or (raw_push and url_has_userinfo(raw_push))
            ),
            alternate_push_url=bool(push_urls),
            default_push_refspec=(
                _redact(push_refspecs[0]) if push_refspecs else None
            ),
            rewrite_rules=self._rewrite_rules(root, urls, push_urls),
            problems=tuple(problems),
            branch_present=present,
            branch_commit=commit,
        )

    def _is_ancestor(
        self, root: Path, ancestor: str, descendant: str
    ) -> Optional[bool]:
        """Prove ``ancestor`` is reachable from ``descendant`` (G37/G38/G46).

        ``git merge-base --is-ancestor`` exits ``0`` for "yes" and ``1`` for "no";
        any other exit code means one of the objects is unknown or unreadable.
        Those two cases are reported as ``None`` ("not proven"), which every gate
        treats as a failure, so T21 never assumes a fast-forward.
        """
        if not is_full_commit_id(ancestor) or not is_full_commit_id(descendant):
            return None
        completed = self._run(
            root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            allow_failure=True,
        )
        code = int(getattr(completed, "returncode", 1))
        if code == 0:
            return True
        if code == 1:
            return False
        return None

    def _count_commits(self, root: Path, *args: str) -> Optional[int]:
        """Read a commit count with ``git rev-list --count`` (G37 evidence).

        A count T21 cannot read stays ``None``: it is never rounded to "zero" or
        "one", because either guess would authorise the wrong push.
        """
        text = self._text(root, "rev-list", "--count", *args)
        if not text:
            return None
        try:
            return int(text.splitlines()[0].strip())
        except (IndexError, ValueError):
            return None

    def _count_new_commits(
        self,
        root: Path,
        request: PushRequest,
        observation: Optional[RemoteObservation],
    ) -> Optional[int]:
        """Count exactly the commits the push would add (G37).

        An existing destination branch is counted as ``<remote>..<expected>``; a
        new branch is counted as every commit the remote is not known to have
        (``--not --remotes=<remote>``), falling back to the whole reachable
        history when the repository has no remote-tracking refs. Nothing is
        fetched: only objects already present are counted, and an existing branch
        whose remote value was not resolved yields ``None``.
        """
        if observation is None:
            return None
        expected = str(request.expected_commit or "").strip()
        if observation.branch_present is True:
            before = str(observation.branch_commit or "").strip()
            if not is_full_commit_id(before):
                return None
            return self._count_commits(root, f"{before}..{expected}")
        if observation.branch_present is False:
            count = self._count_commits(
                root, expected, "--not", f"--remotes={observation.name}"
            )
            if count is not None:
                return count
            return self._count_commits(root, expected)
        return None

    def _capture_checkpoint(
        self, root: Path, repository: Optional[RepositoryObservation]
    ) -> PushCheckpoint:
        """Read the local state that must stay unchanged (G39-G41, G47).

        Both checkpoint readings are taken with the same read-only commands, so a
        race - a commit landing between the readings, a file being staged - shows
        up as a difference and stops the push before it is launched.
        """
        changed_files, is_clean = self._status_entries(root)
        return PushCheckpoint(
            head_commit=self._text(root, "rev-parse", "--verify", "HEAD"),
            branch=self._text(root, "symbolic-ref", "--short", "-q", "HEAD"),
            is_clean=is_clean,
            changed_files=changed_files,
            staged_files=self._staged_files(root),
            operation_in_progress=(
                self._operation_markers(repository) if repository is not None else ()
            ),
            index_locked=bool(getattr(repository, "index_locked", False)),
        )

    def _read_remote_after(
        self, root: Path, request: PushRequest
    ) -> Tuple[PushVerificationStatus, Optional[str], Optional[str]]:
        """Read the destination ref back after the push (G44-G46, read-only).

        Returns:
            ``(status, commit, problem)``. ``status`` is ``VERIFIED`` only when the
            remote reports the recorded commit; a failed read, a ref that
            disappeared and an ambiguous answer are all reported as themselves, so
            a push is never confirmed by assumption.
        """
        ref = f"{REFSPEC_NAMESPACE}{request.remote_branch}"
        commits, problem = self._read_remote_ref(root, request.remote, ref)
        if commits is None:
            return PushVerificationStatus.UNAVAILABLE, None, problem
        if not commits:
            return (
                PushVerificationStatus.MISMATCH,
                None,
                f"the destination ref {ref!r} does not exist on the remote after "
                "the push",
            )
        if len(commits) > 1:
            return (
                PushVerificationStatus.AMBIGUOUS,
                None,
                f"the destination ref {ref!r} matched {len(commits)} entries on "
                "the remote",
            )
        observed = commits[0]
        if normalize_commit_id(observed) != normalize_commit_id(
            request.expected_commit
        ):
            return (
                PushVerificationStatus.MISMATCH,
                observed,
                f"the destination branch {request.remote_branch!r} points at "
                f"{observed} on the remote, not the recorded commit "
                f"{request.expected_commit!r}",
            )
        return PushVerificationStatus.VERIFIED, observed, None

    def _push_argv(self, root: Path, request: PushRequest) -> Tuple[str, ...]:
        """Build - and re-validate - the argv of the one and only push.

        The body is validated here *again*, immediately before the launch, so the
        command that runs is provably the command the gates approved.
        """
        body = (PUSH_SUBCOMMAND, request.remote, request.refspec)
        _validate_git_invocation(body)
        return (self._git_command, "-C", str(root), *body)

    def _push_once(self, root: Path, request: PushRequest) -> PushObservation:
        """Execute the single ``git push`` and record what it did, once (S11).

        This is the only place in T21 that launches a mutating command, and it is
        called at most once per attempt. A launch failure, a timeout and a
        non-zero exit are all recorded as data - the push failed - and are never
        retried, forced or repaired with a fetch.
        """
        argv = self._push_argv(root, request)
        try:
            completed = self._runner(
                list(argv),
                str(root),
                dict(self._sanitized_environment),
                self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            return PushObservation(
                attempted=True,
                launched=False,
                argv=argv,
                launch_error=_clip(
                    _redact(f"the Git executable was not found: {exc}")
                ),
            )
        except subprocess.TimeoutExpired as exc:
            return PushObservation(
                attempted=True,
                launched=True,
                argv=argv,
                stdout=_clip(_redact(str(getattr(exc, "stdout", "") or ""))),
                stderr=_clip(_redact(str(getattr(exc, "stderr", "") or ""))),
                timed_out=True,
            )
        except OSError as exc:  # pragma: no cover - defensive
            return PushObservation(
                attempted=True,
                launched=False,
                argv=argv,
                launch_error=_clip(
                    _redact(f"the push could not be launched: {exc}")
                ),
            )
        return PushObservation(
            attempted=True,
            launched=True,
            argv=argv,
            returncode=int(getattr(completed, "returncode", 1)),
            stdout=_clip(_redact(str(getattr(completed, "stdout", "") or ""))),
            stderr=_clip(_redact(str(getattr(completed, "stderr", "") or ""))),
        )

    def _result(
        self,
        status: PushStatus,
        facts: PushFacts,
        phase: PushPhase,
        reason: str,
        records: Sequence[PushStageRecord],
    ) -> PushResult:
        """Build a :class:`PushResult` with a fresh gate evaluation (T20 style).

        The evaluation is recomputed for the phase T21 actually reached, so the
        gates, the decision and the reason in the result always describe the same
        facts. Nothing is inferred from a partial state: a fact T21 could not
        observe stays ``None`` and therefore fails its gate.
        """
        evaluation = evaluate_push_gates(facts, phase=phase)
        request = facts.request if isinstance(facts.request, PushRequest) else None
        return PushResult(
            status=status,
            reason=reason,
            gates=evaluation,
            stage_records=tuple(records),
            request=request,
            repository=facts.repository,
            remote=facts.remote_observation,
            push=facts.push,
            checkpoint=facts.checkpoint,
            final_checkpoint=facts.final_checkpoint,
            expected_commit=(
                None if request is None else _opt_text(request.expected_commit)
            ),
            remote_before_commit=facts.remote_before_commit,
            remote_after_commit=facts.remote_after_commit,
            branch=None if request is None else _opt_text(request.branch),
            remote_branch=None if request is None else _opt_text(request.remote_branch),
            refspec=facts.refspec,
        )

    def _refuse(
        self,
        facts: PushFacts,
        phase: PushPhase,
        reason: str,
        records: Sequence[PushStageRecord],
    ) -> PushResult:
        """Return a refusal detected at ``phase``: no push has been attempted."""
        return self._result(PushStatus.REFUSED, facts, phase, reason, records)

    def push_safely(
        self,
        *,
        repository_path: object,
        commit_result: object,
        branch: object = None,
        remote: object = "origin",
        remote_branch: object = None,
        expected_commit: object = None,
        config: Optional[PushPolicyConfig] = None,
    ) -> PushResult:
        """Run the T21 state machine: exactly one verified push, and only one.

        Args:
            repository_path: the working copy T20 committed into. A path that is
                not an existing directory is a caller error (:class:`GitPushError`),
                never a silent push from another directory.
            commit_result: the T20 ``CommitResult`` (or its ``as_dict()`` view).
                It is only projected into read-only facts - never mutated.
            branch: the source branch; defaults to the branch T20 reported.
            remote: the destination remote name (default ``"origin"``).
            remote_branch: the destination branch. It has **no default**: T21
                never guesses a destination, so leaving it empty makes G1 refuse.
            expected_commit: the commit to push; defaults to the commit T20
                created. G1 refuses a value that is not a full commit id.
            config: the :class:`PushPolicyConfig` to evaluate with (defaults to
                the executor's).

        Returns:
            A :class:`PushResult`. A refusal, a failed push and an unverified
            push are all *results*; only caller/environment errors raise
            :class:`GitPushError`.
        """
        records: List[PushStageRecord] = []

        def record(stage: str, detail: str) -> None:
            """Append one secret-free line to the state-machine trail."""
            records.append(PushStageRecord(stage=stage, detail=_redact(detail)))

        def stop(
            stage: str,
            phase: PushPhase,
            fallback: str,
            evaluation: PushGateEvaluation,
        ) -> PushResult:
            """Record the gate refusal at ``stage`` and return it unchanged."""
            reason = evaluation.refusal_reason or fallback
            record(stage, reason)
            return self._refuse(facts, phase, reason, records)

        policy = config or self._config

        # --- S0 VALIDATE_INPUTS (G1) --------------------------------------
        requested = str(repository_path or "").strip()
        if not requested:
            raise GitPushError(
                "T21 requires the repository path T20 committed in."
            )
        candidate = Path(requested).expanduser()
        if not candidate.is_dir():
            raise GitPushError(
                f"Repository path is not an existing directory: '{candidate}'."
            )
        root_text = str(candidate.resolve())
        root = Path(root_text)
        projection = self._project_t20(commit_result)
        request = PushRequest(
            repository_path=root_text,
            expected_commit=str(
                expected_commit
                or projection.get("t20_new_head")
                or projection.get("t20_commit_sha")
                or ""
            ).strip(),
            branch=str(
                branch or projection.get("t20_repository_branch") or ""
            ).strip(),
            remote=str(remote or "").strip(),
            remote_branch=str(remote_branch or "").strip(),
            commit_result=commit_result,
        )
        facts = PushFacts(
            request=request,
            config=policy,
            expected_root=root_text,
            refspec=request.refspec,
            **projection,
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.INPUTS)
        if evaluation.is_refusal:
            return stop(
                "S0-validate-inputs",
                PushPhase.INPUTS,
                "The T21 request is incomplete.",
                evaluation,
            )
        record(
            "S0-validate-inputs",
            f"branch={request.branch!r}, remote={request.remote!r}, "
            f"remote_branch={request.remote_branch!r}, "
            f"expected_commit={request.expected_commit!r}, "
            f"refspec={request.refspec!r}.",
        )

        # --- S1 CHECK_T20_CONTRACT (G2-G5, projection only) ---------------
        evaluation = evaluate_push_gates(facts, phase=PushPhase.CONTRACT)
        if evaluation.is_refusal:
            return stop(
                "S1-check-t20-contract",
                PushPhase.CONTRACT,
                "The T20 result does not describe a commit T21 may push.",
                evaluation,
            )
        record(
            "S1-check-t20-contract",
            f"T20 reported status={facts.t20_status!r} with new HEAD "
            f"{facts.t20_new_head!r} on branch {facts.t20_repository_branch!r}.",
        )

        # --- S2 CHECK_ENVIRONMENT (G10, before any Git process) -----------
        environment = self._inspect_environment()
        facts = replace(facts, environment=environment)
        record("S2-check-environment", environment.reason)
        if not environment.is_safe:
            return self._refuse(
                facts,
                PushPhase.ENVIRONMENT,
                "The Git environment is unsafe: "
                + ", ".join(environment.unsafe_variables)
                + ".",
                records,
            )

        # --- S3 IDENTIFY_REPOSITORY (G6-G9) -------------------------------
        repository = self._identify_repository(root, root_text)
        facts = replace(facts, repository=repository)
        record(
            "S3-identify-repository",
            f"worktree={repository.worktree_root!r}, "
            f"git_dir={repository.git_dir!r}, "
            f"index={repository.index_path!r}, "
            f"HEAD={repository.head_commit!r}, branch={repository.branch!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.REPOSITORY)
        if evaluation.is_refusal:
            return stop(
                "S3-identify-repository",
                PushPhase.REPOSITORY,
                "The repository cannot be used for a push.",
                evaluation,
            )

        # --- S4 CHECK_BRANCH (G11-G15) ------------------------------------
        resolved_default = self._resolve_default_branch(root, request.remote)
        facts = replace(facts, resolved_default_branch=resolved_default)
        record(
            "S4-check-branch",
            f"branch={repository.branch!r}, "
            f"default_branch={facts.default_branch!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.BRANCH)
        if evaluation.is_refusal:
            return stop(
                "S4-check-branch",
                PushPhase.BRANCH,
                "The checked-out branch is not a branch T21 may push.",
                evaluation,
            )

        # --- S5 CHECK_COMMIT (G16-G18) ------------------------------------
        head_commit = repository.head_commit or self._text(
            root, "rev-parse", "--verify", "HEAD"
        )
        expected = request.expected_commit
        commit_exists = (
            self._text(
                root, "rev-parse", "--verify", "--quiet", f"{expected}^{{commit}}"
            )
            is not None
        )
        branch_tip = self._text(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{REFSPEC_NAMESPACE}{request.branch}",
        )
        commit_parent = self._text(
            root, "rev-parse", "--verify", "--quiet", f"{expected}^"
        )
        facts = replace(
            facts,
            head_commit=head_commit,
            commit_exists=commit_exists,
            branch_tip=branch_tip,
            commit_parent=commit_parent,
        )
        record(
            "S5-check-commit",
            f"HEAD={head_commit!r}, expected={expected!r}, "
            f"exists={commit_exists!r}, branch_tip={branch_tip!r}, "
            f"parent={commit_parent!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.COMMIT)
        if evaluation.is_refusal:
            return stop(
                "S5-check-commit",
                PushPhase.COMMIT,
                "The local commit is not the commit T21 was asked to push.",
                evaluation,
            )

        # --- S6 CHECK_WORKTREE (G19-G21) ----------------------------------
        changed_files, is_clean = self._status_entries(root)
        staged_files = self._staged_files(root)
        operations = self._operation_markers(repository)
        facts = replace(
            facts,
            worktree_clean=is_clean,
            staged_files=staged_files,
            operation_in_progress=operations,
            index_locked=bool(repository.index_locked),
        )
        record(
            "S6-check-worktree",
            f"clean={is_clean!r}, changed={list(changed_files)!r}, "
            f"staged={list(staged_files)!r}, operations={list(operations)!r}, "
            f"index_locked={bool(repository.index_locked)!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.WORKTREE)
        if evaluation.is_refusal:
            return stop(
                "S6-check-worktree",
                PushPhase.WORKTREE,
                "The working tree is not in a state T21 may push from.",
                evaluation,
            )

        # --- S7 INSPECT_REMOTE (G22-G26, read-only) -----------------------
        observation = self._inspect_remote(root, request)
        facts = replace(
            facts, remote_name=request.remote, remote_observation=observation
        )
        record(
            "S7-inspect-remote",
            f"remote={observation.name!r}, exists={observation.exists!r}, "
            f"fetch_url={observation.fetch_url!r}, "
            f"branch_present={observation.branch_present!r}, "
            f"branch_commit={observation.branch_commit!r}, "
            f"problems={list(observation.problems)!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.REMOTE)
        if evaluation.is_refusal:
            return stop(
                "S7-inspect-remote",
                PushPhase.REMOTE,
                "The destination remote is not a remote T21 may push to.",
                evaluation,
            )

        # --- S8 RESOLVE_TARGET (G27-G36) ----------------------------------
        # T21 carries no push option at all, so the validated option list is empty
        # by construction: G35 turns any future non-empty value into a refusal.
        facts = replace(facts, push_options=())
        record(
            "S8-resolve-target",
            f"refspec={facts.refspec!r}, "
            f"protected={list(policy.protected_branches)!r}, "
            f"agent_prefix={policy.required_branch_prefix!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.TARGET)
        if evaluation.is_refusal:
            return stop(
                "S8-resolve-target",
                PushPhase.TARGET,
                "The destination the request names is not one T21 may push to.",
                evaluation,
            )

        # --- S9 PROVE_ANCESTRY (G37, G38) ---------------------------------
        present = observation.branch_present
        new_remote_branch = None if present is None else present is False
        before_commit = str(observation.branch_commit or "").strip()
        ancestors: Optional[bool] = None
        if present is True and is_full_commit_id(before_commit):
            ancestors = self._is_ancestor(root, before_commit, expected)
        facts = replace(
            facts,
            new_remote_branch=new_remote_branch,
            new_commit_count=self._count_new_commits(root, request, observation),
            remote_commits_are_ancestors=ancestors,
        )
        record(
            "S9-prove-ancestry",
            f"new_remote_branch={facts.new_remote_branch!r}, "
            f"remote_before_commit={facts.remote_before_commit!r}, "
            f"new_commit_count={facts.new_commit_count!r}, "
            f"remote_commits_are_ancestors={facts.remote_commits_are_ancestors!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.ANCESTRY)
        if evaluation.is_refusal:
            return stop(
                "S9-prove-ancestry",
                PushPhase.ANCESTRY,
                "The push is not provably a one-commit fast-forward.",
                evaluation,
            )

        # --- S10 FINAL_PRE_PUSH_CHECK (G39-G42) ---------------------------
        # Two readings in a row: if anything moves between them (a commit lands,
        # a file is staged, an operation starts) the two snapshots differ and G41
        # refuses before the push is launched.
        checkpoint = self._capture_checkpoint(root, repository)
        final_checkpoint = self._capture_checkpoint(root, repository)
        facts = replace(
            facts,
            checkpoint=checkpoint,
            final_checkpoint=final_checkpoint,
            head_commit=final_checkpoint.head_commit,
            branch_tip=branch_tip,
            worktree_clean=final_checkpoint.is_clean,
            staged_files=final_checkpoint.staged_files,
            operation_in_progress=final_checkpoint.operation_in_progress,
            index_locked=bool(final_checkpoint.index_locked),
        )
        record(
            "S10-final-pre-push-check",
            f"HEAD={final_checkpoint.head_commit!r}, "
            f"branch={final_checkpoint.branch!r}, "
            f"clean={final_checkpoint.is_clean!r}, "
            f"staged={list(final_checkpoint.staged_files)!r}, "
            f"two_readings_match={checkpoint.matches(final_checkpoint)!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.CHECKPOINT)
        if evaluation.is_refusal:
            return stop(
                "S10-final-pre-push-check",
                PushPhase.CHECKPOINT,
                "The local state did not stay still long enough to push.",
                evaluation,
            )

        # --- S11 PUSH (the one and only mutating command) -----------------
        attempt = self._push_once(root, request)
        facts = replace(facts, push=attempt)
        if attempt.succeeded:
            record(
                "S11-push",
                "the single push succeeded: "
                f"{' '.join(attempt.argv)!r}.",
            )
        else:
            record(
                "S11-push",
                f"the single push failed (returncode={attempt.returncode!r}, "
                f"timed_out={attempt.timed_out!r}): "
                f"{_clip(attempt.launch_error or attempt.stderr or 'no output')}",
            )

        # --- S12 VERIFY_REMOTE (G43-G46, read-only read back) -------------
        if attempt.succeeded:
            verification, after_commit, problem = self._read_remote_after(
                root, request
            )
        else:
            verification = PushVerificationStatus.SKIPPED
            after_commit = None
            problem = (
                "the push did not report success, so the remote was not read back"
            )
        facts = replace(
            facts, verification=verification, remote_after_commit=after_commit
        )
        record(
            "S12-verify-remote",
            f"verification={verification.value!r}, "
            f"remote_after_commit={after_commit!r}"
            + (f", {problem}." if problem else "."),
        )

        # --- S13 VERIFY_LOCAL (G47, G48) ----------------------------------
        post_push = self._capture_checkpoint(root, repository)
        facts = replace(facts, post_push_checkpoint=post_push)
        record(
            "S13-verify-local",
            f"HEAD={post_push.head_commit!r}, branch={post_push.branch!r}, "
            f"clean={post_push.is_clean!r}, "
            f"unchanged={final_checkpoint.matches(post_push)!r}.",
        )
        evaluation = evaluate_push_gates(facts, phase=PushPhase.VERIFY)
        if attempt.succeeded and not evaluation.is_refusal:
            return self._result(
                PushStatus.PUSHED,
                facts,
                PushPhase.VERIFY,
                "Exactly one push ran, and the remote "
                f"{request.remote}/{request.remote_branch} was read back at the "
                f"recorded commit {facts.remote_after_commit!r}.",
                records,
            )
        if not attempt.succeeded:
            return self._result(
                PushStatus.PUSH_FAILED,
                facts,
                PushPhase.VERIFY,
                "The single push attempt did not succeed; T21 never forces, "
                "retries or fetches afterwards, so the remote is left as it is. "
                + (evaluation.refusal_reason or "See the recorded push output."),
                records,
            )
        return self._result(
            PushStatus.PUSH_UNVERIFIED,
            facts,
            PushPhase.VERIFY,
            "The push reported success but its effect on the remote could not be "
            "proven locally; a human must inspect the remote. "
            + (evaluation.refusal_reason or "See the recorded verification."),
            records,
        )

def push_safely(
    *,
    repository_path: object,
    commit_result: object,
    **kwargs: object,
) -> PushResult:
    """One-call wrapper around :meth:`GitPushExecutor.push_safely`.

    The executor is the injectable, testable unit; this helper exists so a caller
    can express "push the commit T20 proved, exactly once" in one line using the
    production runner, the system Git and the conservative default policy.
    """
    executor = GitPushExecutor()
    return executor.push_safely(
        repository_path=repository_path, commit_result=commit_result, **kwargs
    )
