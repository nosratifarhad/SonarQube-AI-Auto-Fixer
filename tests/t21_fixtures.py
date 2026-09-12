"""Shared, non-test helpers for the T21 test suite.

This module is deliberately named so pytest does **not** collect it. It provides

* a recording Git runner that lets a test observe every argv T21 executes and
  inject faults (a timed-out push, a failing push, a read back that lies), and
* builders for the read-only records T21 consumes: the T20 ``CommitResult``
  view, the repository/remote/checkpoint observations and a fully consistent
  :class:`~push_policy.PushFacts` (so a policy test can flip exactly one fact).

No network, no live SonarQube server and no push outside ``tmp_path``: every
repository used here is a real local repository created by the tests.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import git_push as git_push_module
from push_policy import (
    GitEnvironmentReport,
    PushCheckpoint,
    PushFacts,
    PushObservation,
    PushPolicyConfig,
    PushRequest,
    PushVerificationStatus,
    RemoteObservation,
    RepositoryObservation,
    build_refspec,
)

#: The agent branch T07 creates and T21 is allowed to push (T21's namespace).
AGENT_BRANCH = "ai/sonar-fix/AX1"
#: A second destination branch inside the same namespace.
OTHER_AGENT_BRANCH = "ai/sonar-fix/AX1-followup"
#: The single remote T21 pushes to in the tests.
REMOTE = "origin"
#: A remote URL shape T21 accepts (never contacted by these tests).
REMOTE_URL = "https://git.example.invalid/acme/demo.git"
#: The repository root the synthetic facts point at.
REPO_ROOT = "C:/work/demo"
#: The commit T20 created (the one T21 must push).
COMMIT = "a" * 40
#: The commit T20 started from (the remote's value before the push).
PREVIOUS_COMMIT = "b" * 40
#: An unrelated commit that must never be pushed.
OUT_OF_BAND_COMMIT = "c" * 40


def git_run(cwd: Path, *args: str, check: bool = True) -> str:
    """Run ``git`` inside ``cwd`` and return trimmed stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)!r} failed in {cwd}:\n{result.stderr}"
        )
    return result.stdout.strip()


def real_runner(args: Sequence[str], cwd: str, env: Dict[str, str], timeout: float):
    """The production-shaped process runner used by the recording runner."""
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
    )


def subcommand_of(args: Sequence[str]) -> str:
    """The Git subcommand of a full argv (``git -C <root> <subcommand> ...``)."""
    return git_push_module._split_git_invocation(list(args)[3:])[0]


def fake_completed(returncode=0, stdout="", stderr=""):
    """A minimal ``subprocess.CompletedProcess`` look-alike."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class RecordingRunner:
    """Records every Git argv/environment and can inject faults or side effects.

    ``on_call`` receives ``(subcommand, argv, cwd, runner)`` and may return a
    ``CompletedProcess`` look-alike to replace the real execution, or ``None``
    to run the real command.
    """

    def __init__(self, on_call: Optional[Callable] = None) -> None:
        self.commands: List[List[str]] = []
        self.environments: List[Dict[str, str]] = []
        self._on_call = on_call

    def __call__(self, args, cwd, env, timeout):
        argv = [str(item) for item in args]
        self.commands.append(argv)
        self.environments.append(dict(env))
        subcommand = subcommand_of(argv)
        if self._on_call is not None:
            override = self._on_call(subcommand, argv, cwd, self)
            if override is not None:
                return override
        return real_runner(argv, cwd, dict(env), timeout)

    # -- assertion helpers used by the tests ----------------------------

    @property
    def subcommands(self) -> Tuple[str, ...]:
        """Every Git subcommand that was attempted, in order."""
        return tuple(subcommand_of(argv) for argv in self.commands)

    def subcommands_matching(self, name: str) -> List[List[str]]:
        """Every recorded argv whose subcommand is ``name``."""
        return [argv for argv in self.commands if subcommand_of(argv) == name]

    @property
    def push_commands(self) -> List[List[str]]:
        """Every recorded ``git push`` argv (there must be at most one)."""
        return self.subcommands_matching(git_push_module.PUSH_SUBCOMMAND)

    @property
    def push_count(self) -> int:
        """How many ``git push`` commands were launched."""
        return len(self.push_commands)


# ---------------------------------------------------------------------------
# Synthetic, internally consistent policy records
# ---------------------------------------------------------------------------


def repository_observation(**overrides) -> RepositoryObservation:
    """A usable non-bare repository identity rooted at :data:`REPO_ROOT`."""
    values = {
        "expected_root": REPO_ROOT,
        "worktree_root": REPO_ROOT,
        "git_dir": f"{REPO_ROOT}/.git",
        "common_dir": f"{REPO_ROOT}/.git",
        "index_path": f"{REPO_ROOT}/.git/index",
        "index_locked": False,
        "is_bare": False,
        "head_exists": True,
        "head_commit": COMMIT,
        "branch": AGENT_BRANCH,
        "problems": (),
    }
    values.update(overrides)
    return RepositoryObservation(**values)


def remote_observation(**overrides) -> RemoteObservation:
    """An unambiguous remote whose destination branch holds the previous commit."""
    values = {
        "name": REMOTE,
        "exists": True,
        "urls": (REMOTE_URL,),
        "push_urls": (),
        "fetch_url": REMOTE_URL,
        "push_url": REMOTE_URL,
        "url_well_formed": True,
        "url_userinfo": False,
        "alternate_push_url": False,
        "default_push_refspec": None,
        "rewrite_rules": (),
        "problems": (),
        "branch_present": True,
        "branch_commit": PREVIOUS_COMMIT,
    }
    values.update(overrides)
    return RemoteObservation(**values)


def checkpoint(**overrides) -> PushCheckpoint:
    """A clean pre/post-push checkpoint at the recorded commit."""
    values = {
        "head_commit": COMMIT,
        "branch": AGENT_BRANCH,
        "is_clean": True,
        "changed_files": (),
        "staged_files": (),
        "operation_in_progress": (),
        "index_locked": False,
    }
    values.update(overrides)
    return PushCheckpoint(**values)


def push_observation(**overrides) -> PushObservation:
    """The one successful ``git push`` exactly as the executor records it."""
    refspec = build_refspec(AGENT_BRANCH, AGENT_BRANCH)
    values = {
        "attempted": True,
        "launched": True,
        "argv": ("git", "-C", REPO_ROOT, "push", REMOTE, refspec),
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "launch_error": None,
    }
    values.update(overrides)
    return PushObservation(**values)


def happy_facts(**overrides) -> PushFacts:
    """Every fact T21 holds once a verified push has been read back.

    The record is internally consistent, so all 48 gates pass. A policy test
    flips one field with :func:`dataclasses.replace` to make one gate refuse.
    """
    refspec = build_refspec(AGENT_BRANCH, AGENT_BRANCH)
    request = PushRequest(
        repository_path=REPO_ROOT,
        expected_commit=COMMIT,
        branch=AGENT_BRANCH,
        remote=REMOTE,
        remote_branch=AGENT_BRANCH,
    )
    facts = PushFacts(
        request=request,
        config=PushPolicyConfig(default_branch="main"),
        expected_root=REPO_ROOT,
        t20_status="committed",
        t20_is_committed=True,
        t20_new_head=COMMIT,
        t20_previous_head=PREVIOUS_COMMIT,
        t20_commit_sha=COMMIT,
        t20_needs_attention=False,
        t20_repository_valid=True,
        t20_repository_root=REPO_ROOT,
        t20_repository_branch=AGENT_BRANCH,
        repository=repository_observation(),
        environment=GitEnvironmentReport(
            unsafe_variables=(),
            checked_variables=("GIT_DIR", "GIT_WORK_TREE"),
            reason="No redirecting Git environment variable is set.",
        ),
        head_commit=COMMIT,
        commit_exists=True,
        branch_tip=COMMIT,
        commit_parent=PREVIOUS_COMMIT,
        worktree_clean=True,
        staged_files=(),
        operation_in_progress=(),
        index_locked=False,
        resolved_default_branch="main",
        remote_name=REMOTE,
        remote_observation=remote_observation(),
        refspec=refspec,
        push_options=(),
        new_remote_branch=False,
        new_commit_count=1,
        remote_commits_are_ancestors=True,
        checkpoint=checkpoint(),
        final_checkpoint=checkpoint(),
        push=push_observation(),
        verification=PushVerificationStatus.VERIFIED,
        remote_after_commit=COMMIT,
        post_push_checkpoint=checkpoint(),
    )
    return replace(facts, **overrides) if overrides else facts


# ---------------------------------------------------------------------------
# The T20 result view T21 consumes
# ---------------------------------------------------------------------------


def t20_result(
    *,
    new_head: str = COMMIT,
    previous_head: str = PREVIOUS_COMMIT,
    status: str = "committed",
    is_committed: bool = True,
    branch: str = AGENT_BRANCH,
    worktree_root: str = REPO_ROOT,
    repository_valid: bool = True,
    needs_attention: bool = False,
) -> dict:
    """A faithful ``CommitResult.as_dict()`` view for one verified T20 commit.

    T20 stores the *resolved* worktree root, so callers that use a real
    repository must pass ``worktree_root=str(path.resolve())``.
    """
    return {
        "status": status,
        "is_committed": is_committed,
        "new_head": new_head,
        "previous_head": previous_head,
        "commit_sha": new_head,
        "needs_attention": needs_attention,
        "repository": {
            "worktree_root": worktree_root,
            "branch": branch,
            "is_valid": repository_valid,
        },
    }


@dataclass(frozen=True)
class PushFixture:
    """A real clone on an agent branch with exactly one unpublished commit.

    Attributes:
        remote: the bare remote repository the clone pushes to.
        work: the clone on ``AGENT_BRANCH`` (the repository T21 runs in).
        previous_head: the local commit T20 started from (the commit
            ``CommitResult.previous_head`` reports).
        published: the commit the remote's destination branch holds before the
            push (``""`` when the destination branch does not exist yet).
        head: the commit T20 created and T21 must push.
    """

    remote: Path
    work: Path
    previous_head: str
    published: str
    head: str

    @property
    def branch(self) -> str:
        """The agent branch both repositories use."""
        return AGENT_BRANCH

    @property
    def remote_before(self) -> Optional[str]:
        """The commit the remote's destination branch holds, or ``None``."""
        return self.published or None

    def t20_result(self, **overrides) -> dict:
        """The T20 result describing the unpublished commit in this fixture."""
        values = {
            "new_head": self.head,
            "previous_head": self.previous_head,
            "worktree_root": str(self.work.resolve()),
            "branch": self.branch,
        }
        values.update(overrides)
        return t20_result(**values)

    def remote_ref(self, remote_branch: Optional[str] = None) -> Optional[str]:
        """The commit the remote's branch points at (``None`` when absent)."""
        name = remote_branch or self.branch
        text = git_run(
            self.work, "ls-remote", REMOTE, f"refs/heads/{name}", check=False
        )
        return text.split("\t")[0].strip() if text else None


def build_push_repo(
    seed: Path,
    *,
    remote_branch: Optional[str] = None,
    publish: bool = True,
) -> PushFixture:
    """Clone ``seed`` into a bare remote and prepare one unpublished commit.

    Args:
        seed: an existing repository with at least one commit on ``main``. The
            tests pass the ``make_remote_repo`` fixture.
        remote_branch: the branch that is cloned and then checked out (defaults
            to :data:`AGENT_BRANCH`).
        publish: publish a first commit on the agent branch (so the destination
            branch already exists) before creating the commit T21 must push.

    Returns:
        A :class:`PushFixture` whose ``head`` is one commit ahead of the remote.
    """
    branch = remote_branch or AGENT_BRANCH
    remote = seed.parent / "t21-remote.git"
    work = seed.parent / "t21-work"
    git_run(seed, "clone", "--bare", str(seed), str(remote))
    git_run(seed.parent, "clone", str(remote), str(work))
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    git_run(work, "remote", "set-head", REMOTE, "-a")
    git_run(work, "checkout", "-q", "-b", branch)
    published = ""
    source = work / "src" / "app.py"
    if publish:
        source.parent.mkdir(exist_ok=True)
        source.write_text("fixed once\n", encoding="utf-8")
        git_run(work, "add", "--", "src/app.py")
        git_run(work, "commit", "-q", "-m", "fix: published agent commit")
        published = git_run(work, "rev-parse", "HEAD")
        git_run(
            work,
            "push",
            "-q",
            REMOTE,
            f"refs/heads/{branch}:refs/heads/{branch}",
        )
    previous_head = git_run(work, "rev-parse", "HEAD")
    source.parent.mkdir(exist_ok=True)
    source.write_text("fixed twice\n", encoding="utf-8")
    git_run(work, "add", "--", "src/app.py")
    git_run(work, "commit", "-q", "-m", "fix: the commit T20 verified")
    head = git_run(work, "rev-parse", "HEAD")
    return PushFixture(
        remote=remote,
        work=work,
        previous_head=previous_head,
        published=published,
        head=head,
    )


def snapshot(root: Path, *, remotes: bool = True) -> Dict[str, str]:
    """Capture the local state a T21 run must never change.

    Args:
        root: the repository to capture.
        remotes: include every remote-tracking ref. A successful push updates
            ``refs/remotes/<remote>/<branch>`` (Git's own bookkeeping), so a test
            about a *refused* push keeps this at its default ``True`` while a
            test about a successful push compares only the tracked local state.

    Returns:
        HEAD, the checked-out branch, the porcelain status, the index tree hash,
        the local configuration and - when requested - every remote-tracking ref,
        so a test can assert equality across a run.
    """
    state = {
        "head": git_run(root, "rev-parse", "HEAD"),
        "branch": git_run(root, "branch", "--show-current"),
        "status": git_run(root, "status", "--porcelain"),
        "index": git_run(root, "write-tree"),
        "config": git_run(root, "config", "--local", "--list"),
    }
    if remotes:
        state["remotes"] = git_run(
            root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"
        )
    return state
