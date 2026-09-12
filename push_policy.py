"""T21 push policy - the pure half of the safe Git push boundary.

This module is the T21 counterpart of :mod:`commit_policy`: it holds everything
that can be decided **without touching Git, the filesystem or the network**.

* the gate catalog :data:`GATES` (48 gates, "G1"-"G48") and one evaluator per
  gate (:data:`_GATE_EVALUATORS`, asserted against :data:`GATES` on import);
* the phase of every gate (:data:`_GATE_PHASE`) and
  :func:`evaluate_push_gates`, which reports ``NOT_REACHED`` for gates whose
  state-machine stage has not run yet and fails closed for every other gate;
* the typed, secret-free observation records the executor fills in
  (:class:`RepositoryObservation`, :class:`RemoteObservation`,
  :class:`PushCheckpoint`, :class:`PushObservation`) and the fact bundle
  :class:`PushFacts` the gates read;
* the refspec primitives (:func:`build_refspec`, :func:`parse_refspec`) and the
  remote-name / remote-URL validators.

A gate is ``PASS`` when its fact *proves* the condition, ``FAIL`` (fail closed)
otherwise, and ``NOT_REACHED`` until its stage has been evaluated. The decision
is ``REFUSE`` as soon as any evaluated gate fails, so the report names **every**
refusal instead of only the first one.

Nothing here performs I/O: no subprocess, no Git, no network, no clock, no
environment lookup. The mutation itself lives in :mod:`git_push`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional, Sequence, Tuple

from branch_naming import is_valid_branch_name
from commit_policy import (
    DEFAULT_AGENT_BRANCH_PREFIX,
    PROTECTED_BRANCH_NAMES,
    GateStatus,
    GitEnvironmentReport,
)

#: The one Git subcommand T21 is allowed to mutate with.
PUSH_SUBCOMMAND = "push"

#: Remote names T21 accepts (``git remote add`` accepts the same shape).
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Maximum accepted length of a remote name.
MAX_REMOTE_NAME_LENGTH = 64

#: URL schemes T21 accepts for a remote URL.
ALLOWED_REMOTE_URL_SCHEMES: Tuple[str, ...] = (
    "https",
    "http",
    "ssh",
    "git",
    "file",
)

#: Maximum accepted length of a remote URL.
MAX_REMOTE_URL_LENGTH = 2048

#: The fully qualified branch namespace a refspec uses.
REFSPEC_NAMESPACE = "refs/heads/"

#: A force token inside a refspec (``+refs/heads/x:refs/heads/y``).
REFSPEC_FORCE_PREFIX = "+"

#: A wildcard inside a refspec (``refs/heads/*:refs/heads/*``).
REFSPEC_WILDCARD = "*"

#: The T20 commit status that authorises a push (``CommitStatus.COMMITTED.value``).
#: Kept as a literal so this pure module never imports the executor just for an
#: enum; a test asserts it still matches :class:`git_commit.CommitStatus`.
T20_COMMITTED_STATUS = "committed"

#: Reason recorded for a gate whose state-machine stage has not run yet.
NOT_REACHED_REASON = (
    "Not reached: this gate belongs to a later T21 stage than the one evaluated."
)

#: Push options that would broaden or rewrite the push. T21 never builds one; the
#: list exists so a hostile option is *named* in the refusal instead of being
#: reported as an unqualified "unexpected option".
BROADENING_PUSH_OPTIONS = frozenset(
    {
        "--all",
        "--delete",
        "--follow-tags",
        "--force",
        "--force-with-lease",
        "--mirror",
        "--prune",
        "--tags",
        "-d",
        "-f",
    }
)

_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://")
_SCP_LIKE_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>[^\s]+)$"
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_HEX_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class PushPolicyError(ValueError):
    """Raised when the policy is asked to evaluate something impossible."""


class PushPhase(Enum):
    """State-machine stage that supplies the facts for a group of gates."""

    __test__ = False

    #: S0 - the inputs T21 was handed.
    INPUTS = "inputs"
    #: S1 - the T20 commit contract (status, commit id, consistency).
    CONTRACT = "contract"
    #: S2 - the resolved repository identity.
    REPOSITORY = "repository"
    #: S3 - the Git environment T21 refuses to run in.
    ENVIRONMENT = "environment"
    #: S4 - the checked-out branch and the agent namespace it lives in.
    BRANCH = "branch"
    #: S5 - the recorded commit: HEAD, existence and branch tip.
    COMMIT = "commit"
    #: S6 - the worktree/index/operation state after the commit.
    WORKTREE = "worktree"
    #: S7 - the destination remote (configuration and current remote value).
    REMOTE = "remote"
    #: S8 - the exact push target (branch, refspec, options).
    TARGET = "target"
    #: S9 - the provable fast-forward relationship with the remote.
    ANCESTRY = "ancestry"
    #: S10 - the final pre-push checkpoint.
    CHECKPOINT = "checkpoint"
    #: S11/S12 - the single push attempt and the post-push proof.
    VERIFY = "verify"


class PushDecision(Enum):
    """Overall decision of one gate evaluation (fail closed)."""

    __test__ = False

    PROCEED = "proceed"
    REFUSE = "refuse"


#: Which phase introduces each gate. A gate is ``NOT_REACHED`` until its phase
#: has been evaluated, and ``FAIL`` (fail closed) once it has.
_GATE_PHASE = {
    "G1": PushPhase.INPUTS,
    **{f"G{i}": PushPhase.CONTRACT for i in range(2, 6)},
    **{f"G{i}": PushPhase.REPOSITORY for i in range(6, 10)},
    "G10": PushPhase.ENVIRONMENT,
    **{f"G{i}": PushPhase.BRANCH for i in range(11, 16)},
    **{f"G{i}": PushPhase.COMMIT for i in range(16, 19)},
    **{f"G{i}": PushPhase.WORKTREE for i in range(19, 22)},
    **{f"G{i}": PushPhase.REMOTE for i in range(22, 27)},
    **{f"G{i}": PushPhase.TARGET for i in range(27, 37)},
    **{f"G{i}": PushPhase.ANCESTRY for i in range(37, 39)},
    **{f"G{i}": PushPhase.CHECKPOINT for i in range(39, 43)},
    **{f"G{i}": PushPhase.VERIFY for i in range(43, 49)},
}

#: Phase evaluation order (definition order of :class:`PushPhase`).
_PHASE_ORDER: Tuple[PushPhase, ...] = tuple(PushPhase)


def _phase_index(phase: PushPhase) -> int:
    """Position of ``phase`` in the evaluation order."""
    return _PHASE_ORDER.index(phase)


@dataclass(frozen=True)
class PushPolicyConfig:
    """Tunable, explicit T21 policy. Every default is the conservative choice.

    Attributes:
        protected_branches: branch names T21 never pushes to. Checked for the
            source **and** the destination branch.
        default_branch: the repository's default branch, refused as a
            destination as well (so a rename of ``main`` is still refused).
            When ``None`` the executor resolves the default branch from
            ``refs/remotes/<remote>/HEAD`` and still refuses a mismatch.
        required_branch_prefix: the agent-branch namespace T07 creates. Both the
            source and the destination branch must live inside it, because T21
            pushes agent work and never a human's branch.
        require_clean_worktree: refuse unless the worktree and the index are
            clean after the commit (nothing unstaged, nothing staged).
        allow_new_remote_branch: allow the push to create the destination branch
            when the remote does not have it yet. Never rewrites a remote
            branch: an existing destination must fast-forward by exactly the one
            recorded commit.
    """

    protected_branches: Tuple[str, ...] = PROTECTED_BRANCH_NAMES
    default_branch: Optional[str] = None
    required_branch_prefix: str = DEFAULT_AGENT_BRANCH_PREFIX
    require_clean_worktree: bool = True
    allow_new_remote_branch: bool = True

    def is_protected_branch(self, branch: Optional[str]) -> bool:
        """True when ``branch`` may never be pushed to or pushed from."""
        if not branch:
            return True
        name = str(branch).strip()
        protected = {item.casefold() for item in self.protected_branches}
        if self.default_branch:
            protected.add(str(self.default_branch).casefold())
        return name.casefold() in protected

    def is_agent_branch(self, branch: Optional[str]) -> bool:
        """True when ``branch`` is inside the configured agent namespace."""
        if not branch or not self.required_branch_prefix:
            return False
        prefix = self.required_branch_prefix.rstrip("/") + "/"
        return str(branch).startswith(prefix)


def normalize_commit_id(value: Optional[str]) -> str:
    """Return the case-folded, stripped commit id (``""`` when absent)."""
    return "" if value is None else str(value).strip().casefold()


def is_full_commit_id(value: Optional[str]) -> bool:
    """True when ``value`` looks like a complete SHA-1/SHA-256 object id."""
    return bool(_HEX_RE.match(normalize_commit_id(value)))


def same_path(left: Optional[str], right: Optional[str]) -> bool:
    """True when two filesystem paths are the same path.

    Comparison is case-folded and separator-insensitive because the platform is
    case-insensitive and Git reports forward slashes while Windows reports
    backslashes. It is intentionally textual (no filesystem access) so the
    policy stays pure; the executor resolves the real roots first.
    """
    if not left or not right:
        return False
    def _normalize(item: str) -> str:
        return str(item).strip().replace("\\", "/").rstrip("/")

    return _normalize(left).casefold() == _normalize(right).casefold()


@dataclass(frozen=True)
class RefspecParts:
    """The two fully qualified sides of one non-forcing refspec."""

    __test__ = False

    source: str
    destination: str
    force: bool = False

    @property
    def is_fully_qualified(self) -> bool:
        """True when both sides name a branch under ``refs/heads/``."""
        prefix = REFSPEC_NAMESPACE
        return (
            self.source.startswith(prefix)
            and self.destination.startswith(prefix)
            and len(self.source) > len(prefix)
            and len(self.destination) > len(prefix)
        )

    @property
    def is_wildcard(self) -> bool:
        """True when either side contains a refspec wildcard."""
        return REFSPEC_WILDCARD in self.source or REFSPEC_WILDCARD in self.destination


def build_refspec(branch: Optional[str], remote_branch: Optional[str]) -> str:
    """Return the one refspec T21 ever pushes, or ``""`` when it cannot.

    The result is always fully qualified - ``refs/heads/<branch>:refs/heads/
    <remote_branch>`` - so neither Git's ``push.default`` nor a leftover
    ``remote.<name>.push`` can influence what is pushed. It never carries a
    ``+`` (force), a wildcard or an empty source side (deletion).
    """
    source = str(branch or "").strip()
    destination = str(remote_branch or "").strip()
    if not source or not destination:
        return ""
    if not is_valid_branch_name(source) or not is_valid_branch_name(destination):
        return ""
    return f"{REFSPEC_NAMESPACE}{source}:{REFSPEC_NAMESPACE}{destination}"


def parse_refspec(refspec: Optional[str]) -> Optional[RefspecParts]:
    """Split ``refspec`` into :class:`RefspecParts`, or ``None`` when malformed.

    Only a single ``<src>:<dst>`` mapping is accepted. A leading ``+`` is
    accepted so the policy can *report* a forcing refspec as a failure; a
    wildcard is accepted for the same reason. ``refspec`` with zero or more than
    one ``:`` is malformed.
    """
    text = "" if refspec is None else str(refspec).strip()
    if not text:
        return None
    force = text.startswith(REFSPEC_FORCE_PREFIX)
    if force:
        text = text[len(REFSPEC_FORCE_PREFIX):]
    parts = text.split(":")
    if len(parts) != 2:
        return None
    source, destination = parts[0].strip(), parts[1].strip()
    if not source or not destination:
        return None
    return RefspecParts(source=source, destination=destination, force=force)


def validate_remote_name(name: Optional[str]) -> Optional[str]:
    """Return a problem description for ``name``, or ``None`` when it is valid."""
    text = "" if name is None else str(name)
    if not text:
        return "the remote name is required"
    if text != text.strip():
        return "the remote name has leading or trailing whitespace"
    if len(text) > MAX_REMOTE_NAME_LENGTH:
        return f"the remote name is longer than {MAX_REMOTE_NAME_LENGTH} characters"
    if not REMOTE_NAME_PATTERN.match(text):
        return "the remote name is not a plain remote name"
    return None


def validate_remote_url(url: Optional[str]) -> Optional[str]:
    """Return a problem description for ``url``, or ``None`` when it is valid.

    Accepts a scheme URL (``https://``, ``ssh://``, ``file://``, ...), an
    SCP-like SSH location (``git@host:owner/repo.git``) and a plain filesystem
    path (local remotes), which is what the test suite uses.
    """
    text = "" if url is None else str(url)
    if not text:
        return "the remote URL is empty"
    if text != text.strip():
        return "the remote URL has leading or trailing whitespace"
    if len(text) > MAX_REMOTE_URL_LENGTH:
        return f"the remote URL is longer than {MAX_REMOTE_URL_LENGTH} characters"
    if any(character in text for character in ("\x00", "\n", "\r", "\t", " ")):
        return "the remote URL contains whitespace or control characters"
    scheme_match = _SCHEME_RE.match(text)
    if scheme_match:
        scheme = scheme_match.group("scheme").casefold()
        if scheme not in ALLOWED_REMOTE_URL_SCHEMES:
            return f"the remote URL scheme '{scheme}' is not supported by T21"
        remainder = text[scheme_match.end():]
        if not remainder or remainder.startswith("/") and not remainder.strip("/"):
            return "the remote URL has no host or path"
        return None
    if _SCP_LIKE_RE.match(text):
        return None
    if _WINDOWS_DRIVE_RE.match(text) or text.startswith("/") or text.startswith("./"):
        return None
    if text.startswith("../") or text.startswith(".\\"):
        return None
    return "the remote URL is neither a supported URL, an SSH location nor a path"


def url_has_userinfo(url: Optional[str]) -> bool:
    """True when a URL-style remote embeds credentials (``user:secret@host``)."""
    text = "" if url is None else str(url)
    scheme_match = _SCHEME_RE.match(text)
    if not scheme_match:
        return False
    authority = text[scheme_match.end():].split("/", 1)[0]
    if "@" not in authority:
        return False
    userinfo = authority.rsplit("@", 1)[0]
    return ":" in userinfo


class PushVerificationStatus(Enum):
    """Outcome of the read-only post-push verification (never a mutation)."""

    __test__ = False

    #: The push stage was never reached, so nothing was read back.
    NOT_ATTEMPTED = "not-attempted"
    #: The push command failed, so T21 deliberately skipped the read back.
    SKIPPED = "skipped"
    #: The remote was read and points at the expected commit.
    VERIFIED = "verified"
    #: The remote was read and points somewhere else (or has no such branch).
    MISMATCH = "mismatch"
    #: The remote could not be read at all (Git missing, network, auth).
    UNAVAILABLE = "unavailable"
    #: The remote answered with more than one matching ref.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RepositoryObservation:
    """Resolved repository identity, filled in from read-only Git output."""

    __test__ = False

    expected_root: str = ""
    worktree_root: Optional[str] = None
    git_dir: Optional[str] = None
    common_dir: Optional[str] = None
    index_path: Optional[str] = None
    index_locked: bool = False
    is_bare: Optional[bool] = None
    head_exists: Optional[bool] = None
    head_commit: Optional[str] = None
    branch: Optional[str] = None
    problems: Tuple[str, ...] = ()

    @property
    def matches_expected(self) -> bool:
        """True when the resolved worktree root is the expected repository."""
        return same_path(self.worktree_root, self.expected_root)

    @property
    def index_inside_git_dir(self) -> bool:
        """True when the index belongs to this repository's Git directory."""
        if not self.index_path or not self.git_dir:
            return False
        return same_path(self.index_path, self.git_dir + "/index")

    @property
    def is_usable(self) -> bool:
        """True when every part of the identity was resolved without problems."""
        return (
            not self.problems
            and bool(self.worktree_root)
            and bool(self.git_dir)
            and self.is_bare is False
        )

    def as_dict(self) -> dict:
        """JSON-friendly, secret-free view."""
        return {
            "expected_root": self.expected_root,
            "worktree_root": self.worktree_root,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "index_path": self.index_path,
            "index_locked": self.index_locked,
            "is_bare": self.is_bare,
            "head_exists": self.head_exists,
            "head_commit": self.head_commit,
            "branch": self.branch,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class RemoteObservation:
    """Configuration and state of the destination remote (never a secret)."""

    __test__ = False

    name: str = ""
    exists: bool = False
    urls: Tuple[str, ...] = ()
    push_urls: Tuple[str, ...] = ()
    fetch_url: Optional[str] = None
    push_url: Optional[str] = None
    url_well_formed: Optional[bool] = None
    url_userinfo: bool = False
    alternate_push_url: bool = False
    default_push_refspec: Optional[str] = None
    rewrite_rules: Tuple[str, ...] = ()
    problems: Tuple[str, ...] = ()
    #: Whether the destination branch exists on the remote *before* the push.
    branch_present: Optional[bool] = None
    #: Value of the destination branch on the remote *before* the push.
    branch_commit: Optional[str] = None

    @property
    def is_unambiguous(self) -> bool:
        """True when nothing can redirect the push to another URL or ref."""
        return (
            not self.problems
            and len(self.urls) == 1
            and not self.alternate_push_url
            and not self.rewrite_rules
            and not self.default_push_refspec
        )

    def as_dict(self) -> dict:
        """JSON-friendly, secret-free view."""
        return {
            "name": self.name,
            "exists": self.exists,
            "urls": list(self.urls),
            "push_urls": list(self.push_urls),
            "fetch_url": self.fetch_url,
            "push_url": self.push_url,
            "url_well_formed": self.url_well_formed,
            "url_userinfo": self.url_userinfo,
            "alternate_push_url": self.alternate_push_url,
            "default_push_refspec": self.default_push_refspec,
            "rewrite_rules": list(self.rewrite_rules),
            "problems": list(self.problems),
            "branch_present": self.branch_present,
            "branch_commit": self.branch_commit,
        }


@dataclass(frozen=True)
class PushCheckpoint:
    """Local state captured immediately before and after the single push."""

    __test__ = False

    head_commit: Optional[str] = None
    branch: Optional[str] = None
    is_clean: Optional[bool] = None
    changed_files: Tuple[str, ...] = ()
    staged_files: Tuple[str, ...] = ()
    operation_in_progress: Tuple[str, ...] = ()
    index_locked: bool = False

    def matches(self, other: Optional["PushCheckpoint"]) -> bool:
        """True when the two checkpoints describe identical local state."""
        if not isinstance(other, PushCheckpoint):
            return False
        return (
            normalize_commit_id(self.head_commit)
            == normalize_commit_id(other.head_commit)
            and self.branch == other.branch
            and self.is_clean == other.is_clean
            and tuple(self.changed_files) == tuple(other.changed_files)
            and tuple(self.staged_files) == tuple(other.staged_files)
        )

    def as_dict(self) -> dict:
        """JSON-friendly, secret-free view."""
        return {
            "head_commit": self.head_commit,
            "branch": self.branch,
            "is_clean": self.is_clean,
            "changed_files": list(self.changed_files),
            "staged_files": list(self.staged_files),
            "operation_in_progress": list(self.operation_in_progress),
            "index_locked": self.index_locked,
        }


@dataclass(frozen=True)
class PushObservation:
    """What the single ``git push`` attempt did - recorded verbatim, once."""

    __test__ = False

    attempted: bool = False
    launched: bool = False
    argv: Tuple[str, ...] = ()
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    launch_error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """True only when the one push ran and reported success."""
        return (
            self.attempted
            and self.launched
            and not self.timed_out
            and self.launch_error is None
            and self.returncode == 0
        )

    def as_dict(self) -> dict:
        """JSON-friendly, secret-free view."""
        return {
            "attempted": self.attempted,
            "launched": self.launched,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
        }


@dataclass(frozen=True)
class PushRequest:
    """Everything T21 needs to push one already-verified commit.

    Attributes:
        repository_path: the working copy T20 committed in (the boundary never
            resolves a repository on its own - T19/T20 must pass the same path).
        expected_commit: the full commit id T20 created. T21 refuses to push
            anything else, so an out-of-band commit cannot ride along.
        branch: the checked-out source branch; must be the agent branch T07
            created.
        remote: the destination remote name (e.g. ``origin``).
        remote_branch: the destination branch name; always required, because
            T21 never guesses a destination.
        commit_result: the T20 result object this request is derived from. It is
            only projected into read-only attributes, never mutated.
    """

    __test__ = False

    repository_path: str = ""
    expected_commit: str = ""
    branch: str = ""
    remote: str = ""
    remote_branch: str = ""
    commit_result: object = None

    def problems(self) -> Tuple[str, ...]:
        """Return the reasons this request is incomplete (empty when complete)."""
        found = []
        if not str(self.repository_path or "").strip():
            found.append("the repository path is required")
        if not is_full_commit_id(self.expected_commit):
            found.append("the expected commit must be a full commit id")
        branch = str(self.branch or "")
        if not branch:
            found.append("the source branch is required")
        elif not is_valid_branch_name(branch):
            found.append("the source branch is not a valid Git branch name")
        remote_problem = validate_remote_name(self.remote)
        if remote_problem:
            found.append(remote_problem)
        remote_branch = str(self.remote_branch or "")
        if not remote_branch:
            found.append("the destination branch is required")
        elif not is_valid_branch_name(remote_branch):
            found.append("the destination branch is not a valid Git branch name")
        return tuple(found)

    @property
    def refspec(self) -> str:
        """The one refspec derived from this request (``""`` when invalid)."""
        return build_refspec(self.branch, self.remote_branch)

    def as_dict(self) -> dict:
        """JSON-friendly, secret-free view (the T20 result object is omitted)."""
        return {
            "repository_path": self.repository_path,
            "expected_commit": self.expected_commit,
            "branch": self.branch,
            "remote": self.remote,
            "remote_branch": self.remote_branch,
        }


@dataclass(frozen=True)
class PushFacts:
    """Every fact the gates read, and nothing else.

    The executor fills this bundle in stage by stage and calls
    :func:`evaluate_push_gates` with the phase it has reached. Unknown facts
    stay ``None``, which every gate treats as a failure.
    """

    __test__ = False

    request: Optional[PushRequest] = None
    config: Optional[PushPolicyConfig] = None
    expected_root: str = ""
    #: Compact view of the T20 commit result (``commit_result.as_dict()``).
    t20_status: Optional[str] = None
    t20_is_committed: Optional[bool] = None
    t20_new_head: Optional[str] = None
    t20_previous_head: Optional[str] = None
    t20_commit_sha: Optional[str] = None
    t20_needs_attention: Optional[bool] = None
    t20_repository_valid: Optional[bool] = None
    t20_repository_root: Optional[str] = None
    t20_repository_branch: Optional[str] = None
    repository: Optional[RepositoryObservation] = None
    environment: Optional[GitEnvironmentReport] = None
    head_commit: Optional[str] = None
    commit_exists: Optional[bool] = None
    branch_tip: Optional[str] = None
    commit_parent: Optional[str] = None
    worktree_clean: Optional[bool] = None
    staged_files: Tuple[str, ...] = ()
    operation_in_progress: Tuple[str, ...] = ()
    index_locked: bool = False
    resolved_default_branch: Optional[str] = None
    remote_name: Optional[str] = None
    remote_observation: Optional[RemoteObservation] = None
    refspec: Optional[str] = None
    push_options: Tuple[str, ...] = ()
    new_remote_branch: Optional[bool] = None
    new_commit_count: Optional[int] = None
    remote_commits_are_ancestors: Optional[bool] = None
    checkpoint: Optional[PushCheckpoint] = None
    final_checkpoint: Optional[PushCheckpoint] = None
    push: Optional[PushObservation] = None
    verification: PushVerificationStatus = PushVerificationStatus.NOT_ATTEMPTED
    remote_after_commit: Optional[str] = None
    post_push_checkpoint: Optional[PushCheckpoint] = None

    @property
    def default_branch(self) -> Optional[str]:
        """The default branch T21 compares against (explicit, then resolved)."""
        if self.config is not None and self.config.default_branch:
            return self.config.default_branch
        return self.resolved_default_branch

    @property
    def remote_branch_present(self) -> Optional[bool]:
        """Whether the destination branch existed on the remote before the push."""
        if self.remote_observation is None:
            return None
        return self.remote_observation.branch_present

    @property
    def remote_before_commit(self) -> Optional[str]:
        """Value of the destination branch on the remote before the push."""
        if self.remote_observation is None:
            return None
        return self.remote_observation.branch_commit


#: The ordered gate catalog: gate id -> the condition the gate proves.
GATES = {
    "G1": "the request names a repository, an expected commit, a source branch, a remote and a destination branch",
    "G2": "the supplied T20 result reports that it committed",
    "G3": "the T20 result carries a full commit id for its new HEAD",
    "G4": "the expected commit is exactly the commit T20 created",
    "G5": "the T20 result is internally consistent (status, heads, flags, repository and branch agree)",
    "G6": "the repository is a usable, non-bare working copy with an in-repository index",
    "G7": "the resolved repository root is exactly the expected repository",
    "G8": "the repository is not bare",
    "G9": "HEAD is not detached",
    "G10": "no unsafe Git environment variable is set",
    "G11": "a branch is checked out",
    "G12": "the checked-out branch is the expected source branch",
    "G13": "the source branch is inside the agent namespace",
    "G14": "the source branch is not a protected branch",
    "G15": "the source branch is not the default branch",
    "G16": "HEAD is exactly the expected commit",
    "G17": "the expected commit exists in this repository",
    "G18": "the expected commit is the tip of the source branch",
    "G19": "the working tree is clean",
    "G20": "nothing is staged in the index",
    "G21": "no other Git operation is in progress",
    "G22": "the destination remote name is well formed",
    "G23": "the destination remote exists in this repository",
    "G24": "the remote push configuration is unambiguous (one URL, no alternate push URL, no rewrite rule, no default refspec)",
    "G25": "the remote URL is readable and well formed",
    "G26": "the remote URL embeds no credentials",
    "G27": "the destination branch name is valid",
    "G28": "the destination branch is not a protected branch",
    "G29": "the destination branch is not the default branch",
    "G30": "the destination branch is inside the agent namespace",
    "G31": "the refspec is exactly the fully qualified refspec T21 builds",
    "G32": "the refspec contains no wildcard",
    "G33": "the refspec is not a deletion",
    "G34": "the refspec and the command carry no force token",
    "G35": "the push command carries no push option",
    "G36": "the refspec maps exactly the source branch to the destination branch",
    "G37": "the push adds exactly the one recorded commit (or creates the destination branch)",
    "G38": "the push is a fast-forward that cannot rewrite remote history",
    "G39": "HEAD is unchanged since the final pre-push checkpoint",
    "G40": "the branch is unchanged since the final pre-push checkpoint",
    "G41": "the worktree and the index are unchanged since the final pre-push checkpoint",
    "G42": "every pre-push gate (G1-G41) passed in the final checkpoint",
    "G43": "the single push command reported success",
    "G44": "the remote state could be read back with a read-only command",
    "G45": "the destination branch on the remote points at the expected commit",
    "G46": "the remote update is exactly the expected fast-forward",
    "G47": "the local repository is unchanged after the push",
    "G48": "the push result is internally consistent",
}

_PASS = GateStatus.PASS
_FAIL = GateStatus.FAIL


def _join(problems: Sequence[str]) -> str:
    """Render a problem list as one sentence fragment."""
    return "; ".join(str(item) for item in problems)


@dataclass(frozen=True)
class PushGate:
    """One evaluated T21 gate.

    Attributes:
        gate_id: stable identifier, e.g. ``"G20"``.
        title: short human-readable title.
        status: ``PASS`` / ``FAIL`` / ``NOT_REACHED``.
        reason: why the gate has that status (never contains a secret value).
    """

    __test__ = False

    gate_id: str
    title: str
    status: GateStatus
    reason: str

    @property
    def passed(self) -> bool:
        """True only for an explicit pass."""
        return self.status is GateStatus.PASS

    @property
    def failed(self) -> bool:
        """True only for an explicit failure."""
        return self.status is GateStatus.FAIL

    def as_dict(self) -> dict:
        """Plain summary."""
        return {
            "gate_id": self.gate_id,
            "title": self.title,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PushGateEvaluation:
    """The outcome of evaluating every gate up to ``phase``.

    All 48 gates are always present: gates whose stage has not run are
    ``NOT_REACHED``, so the report is complete and comparable between stages.
    """

    __test__ = False

    phase: PushPhase
    gates: Tuple[PushGate, ...]
    decision: PushDecision

    def status_of(self, gate_id: str) -> Optional[GateStatus]:
        """The status of ``gate_id``, or ``None`` when it is unknown."""
        for gate in self.gates:
            if gate.gate_id == gate_id:
                return gate.status
        return None

    def gate(self, gate_id: str) -> Optional[PushGate]:
        """The :class:`PushGate` record for ``gate_id``, or ``None``."""
        for gate in self.gates:
            if gate.gate_id == gate_id:
                return gate
        return None

    @property
    def failed_gates(self) -> Tuple[PushGate, ...]:
        """Every gate that failed (in evaluation order)."""
        return tuple(gate for gate in self.gates if gate.failed)

    @property
    def passed_gates(self) -> Tuple[PushGate, ...]:
        """Every gate that passed (in evaluation order)."""
        return tuple(gate for gate in self.gates if gate.passed)

    @property
    def not_reached_gates(self) -> Tuple[PushGate, ...]:
        """Every gate whose stage has not run yet."""
        return tuple(
            gate for gate in self.gates if gate.status is GateStatus.NOT_REACHED
        )

    @property
    def first_failure(self) -> Optional[PushGate]:
        """The first failing gate, or ``None`` when nothing failed."""
        failed = self.failed_gates
        return failed[0] if failed else None

    @property
    def refusal_reason(self) -> Optional[str]:
        """A single-line explanation of the first failure, or ``None``."""
        failure = self.first_failure
        if failure is None:
            return None
        return f"{failure.gate_id} ({failure.title}): {failure.reason}"

    @property
    def is_refusal(self) -> bool:
        """True when the decision is ``REFUSE``."""
        return self.decision is PushDecision.REFUSE

    def as_dict(self) -> dict:
        """Secret-free, JSON-friendly report payload."""
        return {
            "phase": self.phase.value,
            "decision": self.decision.value,
            "first_failure": (
                self.first_failure.gate_id if self.first_failure else None
            ),
            "refusal_reason": self.refusal_reason,
            "passed": [gate.gate_id for gate in self.passed_gates],
            "failed": [gate.gate_id for gate in self.failed_gates],
            "not_reached": [gate.gate_id for gate in self.not_reached_gates],
            "gates": [gate.as_dict() for gate in self.gates],
        }


def _has_text(value: object) -> bool:
    """True when ``value`` carries non-whitespace text."""
    return bool(str(value if value is not None else "").strip())


def _request(facts: PushFacts) -> Optional[PushRequest]:
    """The request inside ``facts``, or ``None`` when it is missing."""
    return facts.request if isinstance(facts.request, PushRequest) else None


def _config(facts: PushFacts) -> Optional[PushPolicyConfig]:
    """The config inside ``facts``, or ``None`` when it is missing."""
    return facts.config if isinstance(facts.config, PushPolicyConfig) else None


def _failures(facts: PushFacts, gate_ids: Sequence[str]) -> Tuple[str, ...]:
    """Gate ids among ``gate_ids`` that fail right now (fail closed on error)."""
    failed = []
    for gate_id in gate_ids:
        evaluator = _GATE_EVALUATORS.get(gate_id)
        if evaluator is None:  # pragma: no cover - table is asserted on import
            failed.append(gate_id)
            continue
        try:
            status, _reason = evaluator(facts)
        except Exception:  # pragma: no cover - defensive: fail closed
            failed.append(gate_id)
            continue
        if status is GateStatus.FAIL:
            failed.append(gate_id)
    return tuple(failed)


def _pre_push_gate_ids() -> Tuple[str, ...]:
    """The gate ids G1-G41 (every gate evaluated before the push itself)."""
    return tuple(gate_id for gate_id in GATES if _gate_number(gate_id) <= 41)


def _gate_number(gate_id: str) -> int:
    """The numeric part of a gate id (``"G17"`` -> ``17``)."""
    return int(str(gate_id)[1:])


def _gate_request_complete(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G1 - the request names everything T21 needs."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    missing = request.problems()
    if missing:
        return _FAIL, "The request is incomplete: " + _join(missing) + "."
    if not _has_text(facts.expected_root):
        return _FAIL, "The expected repository root was not supplied."
    if _config(facts) is None:
        return _FAIL, "No PushPolicyConfig was supplied."
    return _PASS, (
        "The request names a repository, an expected commit, a source "
        "branch, a remote and a destination branch."
    )


def _gate_t20_committed(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G2 - the supplied T20 result reports a verified commit."""
    if facts.t20_status is None or facts.t20_is_committed is None:
        return _FAIL, "The T20 commit result was not projected into the facts."
    if facts.t20_status != T20_COMMITTED_STATUS or facts.t20_is_committed is not True:
        return _FAIL, (
            f"The T20 result reports {facts.t20_status!r} "
            f"(is_committed={facts.t20_is_committed!r}), not a verified commit. "
            "T21 only pushes a commit T20 proved."
        )
    return _PASS, "The T20 result reports exactly one verified commit."


def _gate_t20_commit_id(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G3 - the T20 result carries a full commit id for its new HEAD."""
    commit_sha = normalize_commit_id(facts.t20_commit_sha)
    new_head = normalize_commit_id(facts.t20_new_head)
    if not is_full_commit_id(commit_sha) or not is_full_commit_id(new_head):
        return _FAIL, (
            "The T20 result does not carry a full commit id for its new HEAD "
            f"(commit_sha={facts.t20_commit_sha!r}, new_head={facts.t20_new_head!r})."
        )
    if commit_sha != new_head:
        return _FAIL, (
            "The T20 result disagrees with itself: commit_sha "
            f"{facts.t20_commit_sha!r} is not the new HEAD "
            f"{facts.t20_new_head!r}."
        )
    return _PASS, f"The T20 result carries the full commit id {commit_sha}."


def _gate_expected_commit_matches(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G4 - the expected commit is exactly the commit T20 created."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied, so the commit cannot be matched."
    expected = normalize_commit_id(request.expected_commit)
    if not is_full_commit_id(expected):
        return _FAIL, (
            f"The expected commit {request.expected_commit!r} is not a full "
            "commit id."
        )
    created = normalize_commit_id(facts.t20_commit_sha)
    if expected != created or expected != normalize_commit_id(facts.t20_new_head):
        return _FAIL, (
            f"The request expects {request.expected_commit!r} but T20 committed "
            f"{facts.t20_commit_sha!r}; an out-of-band commit would ride along."
        )
    return _PASS, "The expected commit is exactly the commit T20 created."


def _gate_t20_consistent(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G5 - the T20 result is internally consistent."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied, so T20 cannot be cross-checked."
    problems = []
    if facts.t20_needs_attention is not False:
        problems.append("the T20 result asks for attention or was not projected")
    previous = normalize_commit_id(facts.t20_previous_head)
    if not is_full_commit_id(previous):
        problems.append("the T20 previous HEAD is not a full commit id")
    elif previous == normalize_commit_id(facts.t20_new_head):
        problems.append("the T20 previous HEAD equals the new HEAD")
    if facts.t20_repository_valid is not True:
        problems.append("T20 reports the repository identity as invalid")
    if not _has_text(facts.t20_repository_root):
        problems.append("T20 did not report the repository it committed in")
    elif not same_path(facts.t20_repository_root, facts.expected_root):
        problems.append(
            "T20 committed in "
            f"{facts.t20_repository_root!r}, not in {facts.expected_root!r}"
        )
    if not _has_text(facts.t20_repository_branch):
        problems.append("T20 did not report the branch it committed on")
    elif request.branch and facts.t20_repository_branch != request.branch:
        problems.append(
            f"T20 committed on branch {facts.t20_repository_branch!r}, not on "
            f"{request.branch!r}"
        )
    if problems:
        return _FAIL, "The T20 result is inconsistent: " + _join(problems) + "."
    return _PASS, "The T20 result is internally consistent."


def _gate_repository_usable(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G6 - the repository is a usable, non-bare working copy."""
    repository = facts.repository
    if not isinstance(repository, RepositoryObservation):
        return _FAIL, "The repository was never resolved."
    if repository.problems:
        return _FAIL, (
            "The repository could not be resolved: "
            + _join(repository.problems)
            + "."
        )
    if not repository.is_usable:
        return _FAIL, (
            "The repository identity is incomplete or unusable (worktree "
            f"root={repository.worktree_root!r}, git dir={repository.git_dir!r}, "
            f"is_bare={repository.is_bare!r})."
        )
    if not repository.index_inside_git_dir:
        return _FAIL, (
            f"The index {repository.index_path!r} does not live inside this "
            f"repository's Git directory {repository.git_dir!r}."
        )
    if repository.index_locked:
        return _FAIL, "The repository index is locked."
    if repository.head_exists is not True:
        return _FAIL, "HEAD does not resolve to any commit in this repository."
    return _PASS, (
        "The repository is a usable, non-bare working copy with an "
        "in-repository index."
    )


def _gate_repository_root(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G7 - the resolved repository root is the expected repository."""
    repository = facts.repository
    if not isinstance(repository, RepositoryObservation):
        return _FAIL, "The repository was never resolved."
    if not _has_text(repository.worktree_root):
        return _FAIL, "The repository worktree root was not resolved."
    if not same_path(repository.worktree_root, facts.expected_root):
        return _FAIL, (
            f"The resolved repository root {repository.worktree_root!r} is not "
            f"the expected repository {facts.expected_root!r}."
        )
    if not repository.matches_expected:
        return _FAIL, (
            "The resolved repository does not report itself as the expected "
            "repository."
        )
    return _PASS, f"The repository root is exactly {facts.expected_root!r}."


def _gate_repository_not_bare(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G8 - the repository is not bare."""
    repository = facts.repository
    if not isinstance(repository, RepositoryObservation):
        return _FAIL, "The repository was never resolved."
    if repository.is_bare is not False:
        return _FAIL, (
            f"The repository is bare or its bare-ness is unknown "
            f"(is_bare={repository.is_bare!r})."
        )
    return _PASS, "The repository is a non-bare working copy."


def _gate_head_not_detached(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G9 - HEAD is not detached."""
    repository = facts.repository
    if not isinstance(repository, RepositoryObservation):
        return _FAIL, "The repository was never resolved."
    if not _has_text(repository.branch):
        return _FAIL, "HEAD is detached: no branch is checked out."
    if not is_valid_branch_name(repository.branch):
        return _FAIL, (
            f"The checked-out branch {repository.branch!r} is not a valid Git "
            "branch name."
        )
    return _PASS, f"HEAD is on branch {repository.branch!r} (not detached)."


def _gate_environment_safe(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G10 - no unsafe Git environment variable is set."""
    report = facts.environment
    if not isinstance(report, GitEnvironmentReport):
        return _FAIL, "The Git environment was not inspected."
    if not report.is_safe:
        return _FAIL, (
            "Unsafe Git environment variable(s) are set: "
            + _join(report.unsafe_variables)
            + ". T21 refuses to run Git with a redirected repository or "
            "program."
        )
    if not report.checked_variables:
        return _FAIL, (
            "No Git environment variable was checked, so the environment is "
            "unproven."
        )
    return _PASS, "No redirecting or code-executing Git environment variable is set."


def _default_branch(facts: PushFacts) -> Tuple[Optional[str], str]:
    """The default branch T21 excludes, plus a problem text when it is unknown."""
    default = facts.default_branch
    if not _has_text(default):
        return None, (
            "the default branch could not be resolved, so it cannot be excluded"
        )
    return str(default).strip(), ""


def _gate_branch_checked_out(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G11 - a branch is checked out."""
    repository = facts.repository
    branch = "" if repository is None else str(repository.branch or "")
    if not _has_text(branch):
        return _FAIL, "No branch is checked out, so there is nothing to push."
    if not is_valid_branch_name(branch):
        return _FAIL, f"The checked-out branch {branch!r} is not a valid branch name."
    return _PASS, f"The branch {branch!r} is checked out."


def _gate_branch_is_expected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G12 - the checked-out branch is the expected source branch."""
    request = _request(facts)
    repository = facts.repository
    if request is None or repository is None:
        return _FAIL, "The request or the repository observation is missing."
    branch = str(repository.branch or "")
    if branch != request.branch:
        return _FAIL, (
            f"The checked-out branch {branch!r} is not the expected source "
            f"branch {request.branch!r}."
        )
    return _PASS, f"The checked-out branch is the expected branch {branch!r}."


def _gate_branch_agent_namespace(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G13 - the source branch is inside the agent namespace."""
    request = _request(facts)
    config = _config(facts)
    if request is None or config is None:
        return _FAIL, "The request or the policy configuration is missing."
    if not config.is_agent_branch(request.branch):
        return _FAIL, (
            f"The source branch {request.branch!r} is outside the agent "
            f"namespace {config.required_branch_prefix!r}; T21 pushes agent work "
            "only."
        )
    return _PASS, (
        f"The source branch {request.branch!r} is inside the agent namespace."
    )


def _gate_branch_not_protected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G14 - the source branch is not a protected branch."""
    request = _request(facts)
    config = _config(facts)
    if request is None or config is None:
        return _FAIL, "The request or the policy configuration is missing."
    if config.is_protected_branch(request.branch):
        return _FAIL, (
            f"The source branch {request.branch!r} is a protected branch, which "
            "T21 never pushes."
        )
    return _PASS, f"The source branch {request.branch!r} is not protected."


def _gate_branch_not_default(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G15 - the source branch is not the default branch."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    default, problem = _default_branch(facts)
    if default is None:
        return _FAIL, "The source branch cannot be checked: " + problem + "."
    if str(request.branch).casefold() == default.casefold():
        return _FAIL, (
            f"The source branch {request.branch!r} is the repository's default "
            "branch."
        )
    return _PASS, (
        f"The source branch {request.branch!r} is not the default branch "
        f"{default!r}."
    )


def _gate_head_is_expected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G16 - HEAD is exactly the expected commit."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    head = normalize_commit_id(facts.head_commit)
    if not is_full_commit_id(head):
        return _FAIL, "HEAD does not resolve to a full commit id."
    if head != normalize_commit_id(request.expected_commit):
        return _FAIL, (
            f"HEAD is {facts.head_commit!r}, not the recorded commit "
            f"{request.expected_commit!r}."
        )
    return _PASS, f"HEAD is exactly the recorded commit {head}."


def _gate_commit_exists(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G17 - the expected commit exists in this repository."""
    if facts.commit_exists is not True:
        return _FAIL, (
            "The recorded commit does not exist in this repository, so it "
            "cannot be pushed."
        )
    return _PASS, "The recorded commit exists in this repository."


def _gate_branch_tip_is_expected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G18 - the expected commit is the tip of the source branch."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    tip = normalize_commit_id(facts.branch_tip)
    if not is_full_commit_id(tip):
        return _FAIL, "The source branch tip could not be resolved."
    if tip != normalize_commit_id(request.expected_commit):
        return _FAIL, (
            f"The source branch tip {facts.branch_tip!r} is not the recorded "
            f"commit {request.expected_commit!r}, so a later commit would be "
            "pushed as well."
        )
    return _PASS, f"The source branch tip is the recorded commit {tip}."


def _gate_worktree_clean(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G19 - the working tree is clean."""
    config = _config(facts)
    if config is None:
        return _FAIL, "No PushPolicyConfig was supplied."
    if not config.require_clean_worktree:
        return _PASS, "A clean working tree is not required by this configuration."
    if facts.worktree_clean is not True:
        return _FAIL, (
            "The working tree is not clean after the commit "
            f"(worktree_clean={facts.worktree_clean!r}), so T21 cannot prove the "
            "push state is the one T20 produced."
        )
    return _PASS, "The working tree is clean."


def _gate_nothing_staged(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G20 - nothing is staged in the index."""
    staged = tuple(facts.staged_files or ())
    if staged:
        return _FAIL, (
            "The index still has staged path(s): "
            + ", ".join(repr(path) for path in staged)
            + "."
        )
    return _PASS, "Nothing is staged in the index."


def _gate_no_operation_in_progress(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G21 - no other Git operation is in progress."""
    markers = tuple(facts.operation_in_progress or ())
    if markers:
        return _FAIL, (
            "Another Git operation is in progress ("
            + ", ".join(markers)
            + "); T21 refuses to push from a repository mid-operation."
        )
    if facts.index_locked:
        return _FAIL, "The repository index is locked."
    return _PASS, "No other Git operation is in progress."


def _gate_remote_name_valid(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G22 - the destination remote name is well formed."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    problem = validate_remote_name(request.remote)
    if problem:
        return _FAIL, "The destination remote is unusable: " + problem + "."
    if not _has_text(facts.remote_name):
        return _FAIL, "The resolved remote name was not recorded."
    if str(facts.remote_name).strip() != str(request.remote).strip():
        return _FAIL, (
            f"The resolved remote name {facts.remote_name!r} is not the "
            f"requested remote {request.remote!r}."
        )
    return _PASS, f"The remote name {request.remote!r} is well formed."


def _gate_remote_exists(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G23 - the destination remote exists in this repository."""
    request = _request(facts)
    observation = facts.remote_observation
    if request is None or not isinstance(observation, RemoteObservation):
        return _FAIL, "The destination remote was never inspected."
    if observation.problems:
        return _FAIL, (
            "The destination remote could not be inspected: "
            + _join(observation.problems)
            + "."
        )
    if not observation.exists:
        return _FAIL, (
            f"The remote {request.remote!r} does not exist in this repository."
        )
    if observation.name and observation.name != request.remote:
        return _FAIL, (
            f"The inspected remote {observation.name!r} is not the requested "
            f"remote {request.remote!r}."
        )
    return _PASS, f"The remote {request.remote!r} exists in this repository."


def _gate_remote_unambiguous(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G24 - the remote push configuration is unambiguous."""
    observation = facts.remote_observation
    if not isinstance(observation, RemoteObservation):
        return _FAIL, "The destination remote was never inspected."
    if len(tuple(observation.urls)) != 1:
        return _FAIL, (
            f"The remote has {len(tuple(observation.urls))} URL(s); T21 requires "
            "exactly one, because the others would be mirrors."
        )
    if observation.alternate_push_url:
        return _FAIL, "The remote sets a separate push URL, which T21 refuses."
    if observation.rewrite_rules:
        return _FAIL, (
            "The remote is rewritten by a url.*.insteadOf/insteadOf rule: "
            + _join(observation.rewrite_rules)
            + "."
        )
    if observation.default_push_refspec:
        return _FAIL, (
            "The remote configures a default push refspec "
            f"({observation.default_push_refspec!r}) that could redirect the push."
        )
    if not observation.is_unambiguous:
        return _FAIL, "The remote push configuration is not unambiguous."
    return _PASS, (
        "The remote has one URL, no alternate push URL, no rewrite rule and no "
        "default push refspec."
    )


def _gate_remote_url_readable(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G25 - the remote URL is readable and well formed."""
    observation = facts.remote_observation
    if not isinstance(observation, RemoteObservation):
        return _FAIL, "The destination remote was never inspected."
    if not _has_text(observation.fetch_url) or not _has_text(observation.push_url):
        return _FAIL, (
            "The remote has no readable fetch URL and push URL "
            f"(fetch={observation.fetch_url!r}, push={observation.push_url!r})."
        )
    if observation.url_well_formed is not True:
        return _FAIL, (
            f"The remote URL {observation.fetch_url!r} is not a URL shape T21 "
            "accepts."
        )
    problem = validate_remote_url(observation.fetch_url)
    if problem:
        return _FAIL, "The remote URL is unusable: " + problem + "."
    if observation.push_url != observation.fetch_url:
        problem = validate_remote_url(observation.push_url)
        if problem:
            return _FAIL, "The remote push URL is unusable: " + problem + "."
    return _PASS, "The remote URL is readable and well formed."


def _gate_remote_url_no_credentials(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G26 - the remote URL embeds no credentials."""
    observation = facts.remote_observation
    if not isinstance(observation, RemoteObservation):
        return _FAIL, "The destination remote was never inspected."
    if observation.url_userinfo:
        return _FAIL, (
            "The remote URL embeds credentials (user:secret@host), which T21 "
            "refuses to send."
        )
    for url in (observation.fetch_url, observation.push_url):
        if url_has_userinfo(url):
            return _FAIL, (
                "The remote URL embeds credentials (user:secret@host), which "
                "T21 refuses to send."
            )
    return _PASS, "The remote URL embeds no credentials."


def _parsed_refspec(facts: PushFacts) -> Tuple[Optional[RefspecParts], str]:
    """The parsed refspec inside ``facts``, plus a problem text when unusable."""
    text = facts.refspec
    if not _has_text(text):
        return None, "no refspec was recorded"
    parts = parse_refspec(text)
    if parts is None:
        return None, f"the refspec {text!r} is malformed"
    return parts, ""


def _gate_destination_branch_valid(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G27 - the destination branch name is valid."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    if not is_valid_branch_name(request.remote_branch):
        return _FAIL, (
            f"The destination branch {request.remote_branch!r} is not a valid "
            "Git branch name."
        )
    return _PASS, f"The destination branch {request.remote_branch!r} is valid."


def _gate_destination_not_protected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G28 - the destination branch is not a protected branch."""
    request = _request(facts)
    config = _config(facts)
    if request is None or config is None:
        return _FAIL, "The request or the policy configuration is missing."
    if config.is_protected_branch(request.remote_branch):
        return _FAIL, (
            f"The destination branch {request.remote_branch!r} is a protected "
            "branch, which T21 never pushes to."
        )
    return _PASS, f"The destination branch {request.remote_branch!r} is not protected."


def _gate_destination_not_default(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G29 - the destination branch is not the default branch."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    default, problem = _default_branch(facts)
    if default is None:
        return _FAIL, "The destination branch cannot be checked: " + problem + "."
    if str(request.remote_branch).casefold() == default.casefold():
        return _FAIL, (
            f"The destination branch {request.remote_branch!r} is the "
            "repository's default branch."
        )
    return _PASS, (
        f"The destination branch {request.remote_branch!r} is not the default "
        f"branch {default!r}."
    )


def _gate_destination_agent_namespace(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G30 - the destination branch is inside the agent namespace."""
    request = _request(facts)
    config = _config(facts)
    if request is None or config is None:
        return _FAIL, "The request or the policy configuration is missing."
    if not config.is_agent_branch(request.remote_branch):
        return _FAIL, (
            f"The destination branch {request.remote_branch!r} is outside the "
            f"agent namespace {config.required_branch_prefix!r}; T21 never pushes "
            "into a human's branch."
        )
    return _PASS, (
        f"The destination branch {request.remote_branch!r} is inside the agent "
        "namespace."
    )


def _gate_refspec_is_exact(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G31 - the refspec is exactly the fully qualified refspec T21 builds."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    expected = request.refspec
    if not _has_text(expected):
        return _FAIL, (
            "The request does not yield a refspec "
            f"(branch={request.branch!r}, remote branch={request.remote_branch!r})."
        )
    if str(facts.refspec) != expected:
        return _FAIL, (
            f"The recorded refspec {facts.refspec!r} is not the refspec T21 "
            f"builds {expected!r}."
        )
    parts, problem = _parsed_refspec(facts)
    if parts is None:
        return _FAIL, "The recorded refspec is unusable: " + problem + "."
    if not parts.is_fully_qualified:
        return _FAIL, (
            f"The refspec {facts.refspec!r} is not fully qualified "
            f"(both sides must start with {REFSPEC_NAMESPACE!r}), so "
            "push.default or a remote.<name>.push rule could redirect it."
        )
    return _PASS, f"The refspec is exactly {expected}."


def _gate_refspec_no_wildcard(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G32 - the refspec contains no wildcard."""
    parts, problem = _parsed_refspec(facts)
    if parts is None:
        return _FAIL, "The refspec cannot be checked: " + problem + "."
    if parts.is_wildcard or REFSPEC_WILDCARD in str(facts.refspec):
        return _FAIL, (
            f"The refspec {facts.refspec!r} contains a wildcard, which would "
            "push more than one branch."
        )
    return _PASS, "The refspec contains no wildcard."


def _gate_refspec_not_deletion(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G33 - the refspec is not a deletion."""
    parts, problem = _parsed_refspec(facts)
    if parts is None:
        return _FAIL, "The refspec cannot be checked: " + problem + "."
    if not _has_text(parts.source) or not _has_text(parts.destination):
        return _FAIL, (
            f"The refspec {facts.refspec!r} has an empty side, which would "
            "delete a remote branch."
        )
    return _PASS, "The refspec pushes a branch and deletes nothing."


def _gate_no_force(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G34 - the refspec and the command carry no force token."""
    parts, problem = _parsed_refspec(facts)
    if parts is None:
        return _FAIL, "The refspec cannot be checked: " + problem + "."
    if parts.force or str(facts.refspec).startswith(REFSPEC_FORCE_PREFIX):
        return _FAIL, (
            f"The refspec {facts.refspec!r} carries a force token, which could "
            "rewrite remote history."
        )
    options = tuple(facts.push_options or ())
    broadening = sorted(
        option
        for option in options
        if option in BROADENING_PUSH_OPTIONS or option.startswith("--force")
    )
    if broadening:
        return _FAIL, (
            "The push command carries a forcing or broadening option: "
            + ", ".join(broadening)
            + "."
        )
    if any(
        option.startswith("-f") and option != "-f" for option in options
    ):  # pragma: no cover - defensive: "-f<value>" is not a real push option
        return _FAIL, "The push command carries a concatenated force flag."
    return _PASS, "Neither the refspec nor the command carries a force token."


def _gate_no_push_options(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G35 - the push command carries no push option."""
    options = tuple(facts.push_options or ())
    if options:
        return _FAIL, (
            "The push command carries option(s) T21 never uses: "
            + ", ".join(str(option) for option in options)
            + "."
        )
    return _PASS, "The push command carries no option."


def _gate_refspec_maps_expected(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G36 - the refspec maps the source branch to the destination branch."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    parts, problem = _parsed_refspec(facts)
    if parts is None:
        return _FAIL, "The refspec cannot be checked: " + problem + "."
    expected_source = REFSPEC_NAMESPACE + str(request.branch)
    expected_destination = REFSPEC_NAMESPACE + str(request.remote_branch)
    if (
        parts.source != expected_source
        or parts.destination != expected_destination
    ):
        return _FAIL, (
            f"The refspec {facts.refspec!r} maps {parts.source!r} to "
            f"{parts.destination!r}, not {expected_source!r} to "
            f"{expected_destination!r}."
        )
    return _PASS, (
        f"The refspec maps {expected_source!r} to {expected_destination!r}."
    )


def _gate_adds_one_commit(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G37 - the push adds exactly the one recorded commit."""
    if facts.new_remote_branch is None:
        return _FAIL, "Whether the push creates the destination branch was not recorded."
    if facts.remote_branch_present is None:
        return _FAIL, "Whether the destination branch already existed was not recorded."
    if facts.new_remote_branch is not (facts.remote_branch_present is False):
        return _FAIL, (
            "The recorded destination-branch state is inconsistent: "
            f"new_remote_branch={facts.new_remote_branch!r} but "
            f"branch_present={facts.remote_branch_present!r}."
        )
    count = facts.new_commit_count
    if count is None:
        return _FAIL, (
            "The number of commits the push would add was not recorded."
        )
    if count < 1:
        return _FAIL, (
            f"The push would add {count} commit(s); T21 only pushes the one "
            "recorded commit."
        )
    if not facts.new_remote_branch and count != 1:
        return _FAIL, (
            f"The push would add {count} commits to an existing remote branch; "
            "T21 pushes exactly one."
        )
    if facts.new_remote_branch:
        return _PASS, (
            f"The push creates the destination branch and adds {count} commit(s), "
            "ending at the recorded commit."
        )
    return _PASS, "The push adds exactly the one recorded commit."


def _gate_fast_forward(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G38 - the push is a fast-forward that cannot rewrite remote history."""
    if facts.remote_branch_present is False:
        return _PASS, (
            "The destination branch does not exist yet, so nothing is rewritten."
        )
    if facts.remote_branch_present is None:
        return _FAIL, "Whether the destination branch existed was not recorded."
    if facts.remote_commits_are_ancestors is not True:
        return _FAIL, (
            "The remote history is not proven to be an ancestor of the recorded "
            "commit, so the push could rewrite it "
            f"(remote_commits_are_ancestors="
            f"{facts.remote_commits_are_ancestors!r})."
        )
    before = normalize_commit_id(facts.remote_before_commit)
    if not is_full_commit_id(before):
        return _FAIL, "The remote's current commit was not resolved."
    return _PASS, (
        f"The remote's {before} is an ancestor of the recorded commit, so the "
        "push is a fast-forward."
    )


def _gate_checkpoint_head(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G39 - HEAD is unchanged since the final pre-push checkpoint."""
    request = _request(facts)
    checkpoint = facts.checkpoint
    final = facts.final_checkpoint
    if request is None or checkpoint is None or final is None:
        return _FAIL, "The pre-push checkpoint was not captured."
    expected = normalize_commit_id(request.expected_commit)
    if normalize_commit_id(checkpoint.head_commit) != expected:
        return _FAIL, (
            f"HEAD was {checkpoint.head_commit!r} at the pre-push checkpoint, "
            f"not the recorded commit {request.expected_commit!r}."
        )
    if normalize_commit_id(final.head_commit) != expected:
        return _FAIL, (
            f"HEAD changed to {final.head_commit!r} in the final checkpoint; "
            "T21 refuses to push a moving HEAD."
        )
    if normalize_commit_id(facts.head_commit) != expected:
        return _FAIL, (
            f"HEAD is now {facts.head_commit!r}, not the recorded commit "
            f"{request.expected_commit!r}."
        )
    return _PASS, f"HEAD is unchanged at the recorded commit {expected}."


def _gate_checkpoint_branch(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G40 - the branch is unchanged since the final pre-push checkpoint."""
    request = _request(facts)
    checkpoint = facts.checkpoint
    final = facts.final_checkpoint
    if request is None or checkpoint is None or final is None:
        return _FAIL, "The pre-push checkpoint was not captured."
    branches = {
        "the pre-push checkpoint": checkpoint.branch,
        "the final checkpoint": final.branch,
    }
    for label, branch in branches.items():
        if str(branch or "") != str(request.branch):
            return _FAIL, (
                f"The branch at {label} was {branch!r}, not the expected "
                f"branch {request.branch!r}."
            )
    return _PASS, f"The branch is unchanged at {request.branch!r}."


def _gate_checkpoint_worktree(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G41 - the worktree and the index are unchanged since the checkpoint."""
    checkpoint = facts.checkpoint
    final = facts.final_checkpoint
    if checkpoint is None or final is None:
        return _FAIL, "The pre-push checkpoint was not captured."
    for label, snapshot in (
        ("the pre-push checkpoint", checkpoint),
        ("the final checkpoint", final),
    ):
        if snapshot.is_clean is not True:
            return _FAIL, (
                f"The working tree was not clean at {label} "
                f"(is_clean={snapshot.is_clean!r})."
            )
        changed = tuple(snapshot.changed_files or ())
        if changed:
            return _FAIL, (
                f"Path(s) changed since {label}: "
                + ", ".join(repr(path) for path in changed)
                + "."
            )
        staged = tuple(snapshot.staged_files or ())
        if staged:
            return _FAIL, (
                f"Path(s) were staged at {label}: "
                + ", ".join(repr(path) for path in staged)
                + "."
            )
    if not checkpoint.matches(final):
        return _FAIL, (
            "The local state changed between the two pre-push checkpoints."
        )
    return _PASS, "The worktree and the index are unchanged since the checkpoint."


def _gate_pre_push_gates_passed(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G42 - every pre-push gate (G1-G41) passed in the final checkpoint."""
    failed = _failures(facts, _pre_push_gate_ids())
    if failed:
        shown = ", ".join(failed[:6])
        extra = "" if len(failed) <= 6 else f" (+{len(failed) - 6} more)"
        return _FAIL, (
            "The pre-push gates did not all pass: "
            f"{shown}{extra}. The final checkpoint cannot authorise a push."
        )
    return _PASS, "Every pre-push gate (G1-G41) passed in the final checkpoint."


def _gate_push_succeeded(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G43 - the single push command reported success."""
    push = facts.push
    if not isinstance(push, PushObservation):
        return _FAIL, "No push attempt was recorded."
    if push.timed_out:
        return _FAIL, "The single push attempt timed out."
    if push.launch_error is not None:
        return _FAIL, "The single push attempt could not be launched: " + str(
            push.launch_error
        ) + "."
    if push.returncode != 0:
        return _FAIL, (
            f"The single push attempt exited with {push.returncode!r}, so the "
            "push did not succeed."
        )
    if not push.succeeded:
        return _FAIL, "The single push attempt did not report success."
    return _PASS, "The single push attempt reported success."


def _gate_verification_available(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G44 - the remote state could be read back with a read-only command."""
    status = facts.verification
    if status is PushVerificationStatus.NOT_ATTEMPTED:
        return _FAIL, "The remote state was never read back after the push."
    if status is PushVerificationStatus.SKIPPED:
        return _FAIL, (
            "The remote state was not read back, because the push did not report "
            "success."
        )
    if status is PushVerificationStatus.UNAVAILABLE:
        return _FAIL, (
            "The remote state could not be read back with a read-only command "
            "(Git missing, network or authentication failure)."
        )
    if status is PushVerificationStatus.AMBIGUOUS:
        return _FAIL, "The read-only read back returned more than one matching ref."
    if status is not PushVerificationStatus.VERIFIED:
        return _FAIL, (
            f"The remote state read back reported {status!r}, so the push cannot "
            "be confirmed."
        )
    return _PASS, "The remote state was read back with a read-only command."


def _gate_remote_after_matches(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G45 - the destination branch on the remote points at the expected commit."""
    request = _request(facts)
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    if facts.verification is not PushVerificationStatus.VERIFIED:
        return _FAIL, (
            "The destination branch cannot be confirmed, because the read back "
            f"reported {facts.verification!r}."
        )
    after = normalize_commit_id(facts.remote_after_commit)
    if not is_full_commit_id(after):
        return _FAIL, (
            "The destination branch on the remote did not resolve to a commit."
        )
    expected = normalize_commit_id(request.expected_commit)
    if after != expected:
        return _FAIL, (
            f"The destination branch {request.remote_branch!r} points at {after} "
            f"on the remote, not the recorded commit {expected}."
        )
    return _PASS, (
        f"The destination branch {request.remote_branch!r} points at the recorded "
        f"commit {expected} on the remote."
    )


def _gate_remote_update_exact(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G46 - the remote update is exactly the expected fast-forward."""
    request = _request(facts)
    observation = facts.remote_observation
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    if not isinstance(observation, RemoteObservation):
        return _FAIL, "The destination remote was never inspected before the push."
    after = normalize_commit_id(facts.remote_after_commit)
    expected = normalize_commit_id(request.expected_commit)
    if after != expected:
        return _FAIL, (
            "The remote does not hold the recorded commit after the push, so the "
            "update is not the expected fast-forward."
        )
    if observation.branch_present is False:
        return _PASS, (
            f"The destination branch {request.remote_branch!r} was created at the "
            f"recorded commit {expected}."
        )
    if observation.branch_present is None:
        return _FAIL, "Whether the destination branch existed was not recorded."
    before = normalize_commit_id(observation.branch_commit)
    if not is_full_commit_id(before):
        return _FAIL, "The remote's value before the push was not resolved."
    if before == after:
        return _FAIL, (
            "The destination branch already pointed at the recorded commit before "
            "the push, so the push did not update the expected fast-forward."
        )
    if facts.remote_commits_are_ancestors is not True:
        return _FAIL, (
            "The remote history is not proven to be an ancestor of the recorded "
            "commit, so the update is not a fast-forward."
        )
    return _PASS, (
        f"The destination branch moved from {before} to {expected}, which is "
        "exactly the expected fast-forward."
    )


def _gate_local_unchanged_after_push(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G47 - the local repository is unchanged after the push."""
    request = _request(facts)
    final = facts.final_checkpoint
    post = facts.post_push_checkpoint
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    if final is None or post is None:
        return _FAIL, "The post-push checkpoint was not captured."
    expected = normalize_commit_id(request.expected_commit)
    if normalize_commit_id(post.head_commit) != expected:
        return _FAIL, (
            f"HEAD is {post.head_commit!r} after the push, not the recorded "
            f"commit {expected}."
        )
    if str(post.branch or "") != str(request.branch):
        return _FAIL, (
            f"The branch is {post.branch!r} after the push, not the expected "
            f"branch {request.branch!r}."
        )
    if not final.matches(post):
        return _FAIL, "The local repository changed during the push."
    return _PASS, "The local repository is unchanged after the push."


def _gate_push_result_consistent(facts: PushFacts) -> Tuple[GateStatus, str]:
    """G48 - the push result is internally consistent."""
    request = _request(facts)
    push = facts.push
    if request is None:
        return _FAIL, "No PushRequest was supplied."
    if not isinstance(push, PushObservation):
        return _FAIL, "No push attempt was recorded."
    argv = tuple(push.argv or ())
    if not argv:
        return _FAIL, "The executed command line was not recorded."
    if push.attempted is not True or push.launched is not True:
        return _FAIL, (
            "The push was recorded without a launched execution "
            f"(attempted={push.attempted!r}, launched={push.launched!r})."
        )
    subcommands = sum(1 for token in argv if token == PUSH_SUBCOMMAND)
    if subcommands != 1:
        return _FAIL, (
            f"The recorded command line invokes the {PUSH_SUBCOMMAND!r} "
            f"subcommand {subcommands} time(s); T21 pushes exactly once."
        )
    broadening = [
        token
        for token in argv
        if token in BROADENING_PUSH_OPTIONS or token.startswith("--force")
    ]
    if broadening:
        return _FAIL, (
            "The recorded command line carries a forcing or broadening option: "
            + ", ".join(broadening)
            + "."
        )
    if request.remote not in argv:
        return _FAIL, (
            f"The recorded command line does not name the remote "
            f"{request.remote!r}."
        )
    if request.refspec not in argv:
        return _FAIL, (
            f"The recorded command line does not carry the refspec "
            f"{request.refspec!r}."
        )
    if push.succeeded and facts.verification in (
        PushVerificationStatus.NOT_ATTEMPTED,
        PushVerificationStatus.SKIPPED,
    ):
        return _FAIL, (
            "The push reported success but the remote state was never read back "
            f"(verification={facts.verification!r})."
        )
    if not push.succeeded and facts.verification not in (
        PushVerificationStatus.NOT_ATTEMPTED,
        PushVerificationStatus.SKIPPED,
    ):
        return _FAIL, (
            "The push did not report success but the remote state was recorded "
            f"as {facts.verification!r}."
        )
    return _PASS, "The push result is internally consistent."


#: Gate evaluator dispatch table. Every gate in :data:`GATES` must appear here;
#: this is asserted when the module is imported.
_GATE_EVALUATORS: Dict[str, Callable[[PushFacts], Tuple[GateStatus, str]]] = {
    "G1": _gate_request_complete,
    "G2": _gate_t20_committed,
    "G3": _gate_t20_commit_id,
    "G4": _gate_expected_commit_matches,
    "G5": _gate_t20_consistent,
    "G6": _gate_repository_usable,
    "G7": _gate_repository_root,
    "G8": _gate_repository_not_bare,
    "G9": _gate_head_not_detached,
    "G10": _gate_environment_safe,
    "G11": _gate_branch_checked_out,
    "G12": _gate_branch_is_expected,
    "G13": _gate_branch_agent_namespace,
    "G14": _gate_branch_not_protected,
    "G15": _gate_branch_not_default,
    "G16": _gate_head_is_expected,
    "G17": _gate_commit_exists,
    "G18": _gate_branch_tip_is_expected,
    "G19": _gate_worktree_clean,
    "G20": _gate_nothing_staged,
    "G21": _gate_no_operation_in_progress,
    "G22": _gate_remote_name_valid,
    "G23": _gate_remote_exists,
    "G24": _gate_remote_unambiguous,
    "G25": _gate_remote_url_readable,
    "G26": _gate_remote_url_no_credentials,
    "G27": _gate_destination_branch_valid,
    "G28": _gate_destination_not_protected,
    "G29": _gate_destination_not_default,
    "G30": _gate_destination_agent_namespace,
    "G31": _gate_refspec_is_exact,
    "G32": _gate_refspec_no_wildcard,
    "G33": _gate_refspec_not_deletion,
    "G34": _gate_no_force,
    "G35": _gate_no_push_options,
    "G36": _gate_refspec_maps_expected,
    "G37": _gate_adds_one_commit,
    "G38": _gate_fast_forward,
    "G39": _gate_checkpoint_head,
    "G40": _gate_checkpoint_branch,
    "G41": _gate_checkpoint_worktree,
    "G42": _gate_pre_push_gates_passed,
    "G43": _gate_push_succeeded,
    "G44": _gate_verification_available,
    "G45": _gate_remote_after_matches,
    "G46": _gate_remote_update_exact,
    "G47": _gate_local_unchanged_after_push,
    "G48": _gate_push_result_consistent,
}

if set(_GATE_EVALUATORS) != set(GATES):  # pragma: no cover
    raise PushPolicyError(
        "The gate evaluator table does not match the declared gate list."
    )

if set(_GATE_PHASE) != set(GATES):  # pragma: no cover
    raise PushPolicyError(
        "The gate phase table does not match the declared gate list."
    )


def evaluate_push_gates(
    facts: PushFacts,
    *,
    phase: PushPhase = PushPhase.INPUTS,
) -> PushGateEvaluation:
    """Evaluate every gate whose stage has been reached (pure, deterministic).

    Args:
        facts: the immutable record of everything the executor observed.
        phase: the furthest state-machine stage that has run. Gates belonging to
            later stages are reported as ``NOT_REACHED``; every other gate is
            evaluated and produces ``PASS`` or ``FAIL``.

    Returns:
        A :class:`PushGateEvaluation` containing all 48 gates in evaluation
        order. The decision is ``REFUSE`` as soon as one evaluated gate fails, so
        a caller that pushes only when the decision is ``PROCEED`` cannot be
        tricked by a partially-filled :class:`PushFacts`.

    Raises:
        PushPolicyError: ``facts`` is not a :class:`PushFacts`.
    """
    if not isinstance(facts, PushFacts):
        raise PushPolicyError(
            "evaluate_push_gates expects a PushFacts record, got "
            f"{type(facts).__name__}."
        )
    reached = _phase_index(phase)

    gates: list = []
    for gate_id, title in GATES.items():
        if _phase_index(_GATE_PHASE[gate_id]) > reached:
            gates.append(
                PushGate(
                    gate_id=gate_id,
                    title=title,
                    status=GateStatus.NOT_REACHED,
                    reason=NOT_REACHED_REASON,
                )
            )
            continue
        try:
            status, reason = _GATE_EVALUATORS[gate_id](facts)
        except Exception as exc:  # pragma: no cover - defensive: fail closed
            status, reason = _FAIL, (
                "The gate could not be evaluated, which fails closed: "
                f"{type(exc).__name__}: {exc}"
            )
        gates.append(
            PushGate(gate_id=gate_id, title=title, status=status, reason=reason)
        )

    ordered = tuple(gates)
    decision = (
        PushDecision.PROCEED
        if not any(gate.failed for gate in ordered)
        else PushDecision.REFUSE
    )
    return PushGateEvaluation(phase=phase, gates=ordered, decision=decision)


__all__: Sequence[str] = (
    "ALLOWED_REMOTE_URL_SCHEMES",
    "BROADENING_PUSH_OPTIONS",
    "DEFAULT_AGENT_BRANCH_PREFIX",
    "GATES",
    "GateStatus",
    "GitEnvironmentReport",
    "MAX_REMOTE_NAME_LENGTH",
    "MAX_REMOTE_URL_LENGTH",
    "NOT_REACHED_REASON",
    "PROTECTED_BRANCH_NAMES",
    "PUSH_SUBCOMMAND",
    "PushCheckpoint",
    "PushDecision",
    "PushFacts",
    "PushGate",
    "PushGateEvaluation",
    "PushObservation",
    "PushPhase",
    "PushPolicyConfig",
    "PushPolicyError",
    "PushRequest",
    "PushVerificationStatus",
    "REFSPEC_FORCE_PREFIX",
    "REFSPEC_NAMESPACE",
    "REFSPEC_WILDCARD",
    "REMOTE_NAME_PATTERN",
    "RefspecParts",
    "RemoteObservation",
    "RepositoryObservation",
    "T20_COMMITTED_STATUS",
    "build_refspec",
    "evaluate_push_gates",
    "is_full_commit_id",
    "normalize_commit_id",
    "parse_refspec",
    "same_path",
    "url_has_userinfo",
    "validate_remote_name",
    "validate_remote_url",
)
