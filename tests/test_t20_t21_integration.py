"""T20 -> T21 integration tests (T21.1.c).

The T21.1 audit found that every T21 test fed ``push_safely`` a hand-built
dictionary fixture, so the real producer/consumer contract between T20 and T21
was never exercised: nothing proved that a genuine ``git_commit.CommitResult`` -
or its ``as_dict()`` view - satisfies ``git_push._project_t20``.

These tests close that gap. A real T20 commit is created in a temporary
repository and its real result is handed to the real T21 executor twice:

    GitCommitExecutor.commit_safely(...) -> CommitResult           -> push_safely
    GitCommitExecutor.commit_safely(...) -> CommitResult.as_dict() -> push_safely

The remote is a local bare repository under ``tmp_path``: no network, no GitHub,
and the project's own working copy is never a commit or push target.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from commit_policy import (
    CommitFacts,
    CommitPhase,
    RepositoryIdentity,
    evaluate_commit_gates,
)
from git_commit import CommitResult, CommitStatus, GitCommitExecutor
from git_push import GitPushExecutor, PushStatus
from push_policy import (
    PROTECTED_BRANCH_NAMES,
    GateStatus,
    PushDecision,
    PushPolicyConfig,
    build_refspec,
)
from worktree_baseline import WorktreeBaselineInspector, attribute_changes

from t20_fixtures import (
    AGENT_BRANCH,
    TARGET,
    assert_fixed,
    git_run,
    green_status,
)
from t21_fixtures import REMOTE, snapshot

#: The commit id the synthetic contract record reports as its new HEAD.
NEW_HEAD = "a" * 40
#: The commit id the synthetic contract record reports as its previous HEAD.
PREVIOUS_HEAD = "b" * 40
#: The repository root the synthetic contract record points at.
REPO_ROOT = Path(tempfile.gettempdir()) / "t20-t21-contract-repo"


@pytest.fixture
def inspector() -> WorktreeBaselineInspector:
    """A baseline inspector that reads ignored files and content identity."""
    return WorktreeBaselineInspector(timeout_seconds=60.0)


@pytest.fixture
def committed(make_remote_repo, rm, clone_destination, inspector):
    """Run the *real* T20 in a *real* clone and return what the tests need.

    Topology (everything under ``tmp_path``):

    * ``remote.git``  - a local **bare** repository (never on the network);
    * ``work``        - a clone of it, checked out on ``ai/sonar-fix/AX1`` with
      one published commit on that branch;
    * ``work`` afterwards - the branch tip is the commit the real T20 created.
    """
    remote = make_remote_repo()
    work = rm.clone(str(remote), clone_destination())
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    git_run(work, "remote", "set-head", REMOTE, "-a")
    (work / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git_run(work, "add", "--", ".gitignore")
    git_run(work, "commit", "-q", "-m", "chore: ignore rules")
    git_run(work, "checkout", "-q", "-b", AGENT_BRANCH)

    # Publish one commit on the agent branch so the destination branch already
    # exists, exactly one commit behind the commit T20 is about to create.
    (work / TARGET).write_text("fixed once\n", encoding="utf-8")
    git_run(work, "add", "--", TARGET)
    git_run(work, "commit", "-q", "-m", "fix: published agent commit")
    published = git_run(work, "rev-parse", "HEAD")
    git_run(
        work,
        "push",
        "-q",
        REMOTE,
        f"refs/heads/{AGENT_BRANCH}:refs/heads/{AGENT_BRANCH}",
    )

    # T20: a genuine, gated commit of the agent's second edit.
    baseline = inspector.capture(work, include_ignored=True)
    (work / TARGET).write_text("fixed twice\n", encoding="utf-8")
    after = inspector.capture(work, include_ignored=True)
    attribution = attribute_changes(baseline, after)
    status = green_status()
    assert_fixed(status)
    result = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
        repository_path=work,
        issue_status=status,
        baseline=baseline,
        after=after,
        attribution=attribution,
    )
    return SimpleNamespace(
        remote=remote, work=work, published=published, result=result
    )


# ---------------------------------------------------------------------------
# The real T20 -> T21 handoff
# ---------------------------------------------------------------------------


def push(committed, commit_result):
    """Call the production ``GitPushExecutor.push_safely`` API (nothing faked).

    ``branch`` and ``expected_commit`` are deliberately omitted: T21 must derive
    both from the T20 result it was handed.
    """
    executor = GitPushExecutor(config=PushPolicyConfig(default_branch="main"))
    return executor.push_safely(
        repository_path=str(committed.work),
        commit_result=commit_result,
        remote=REMOTE,
        remote_branch=AGENT_BRANCH,
    )


def assert_pushed(committed, pushed) -> None:
    """Every assertion the T20 -> T21 handoff must satisfy."""
    t20 = committed.result

    # -- T20 produced a genuine, verified commit ---------------------------
    assert t20.status is CommitStatus.COMMITTED, t20.reason
    assert t20.is_committed is True
    assert t20.new_head
    assert t20.commit_sha == t20.new_head
    assert t20.previous_head == committed.published
    assert t20.gates.failed_gates == ()

    # -- exactly one commit was created by T20 -----------------------------
    assert (
        git_run(
            committed.work,
            "rev-list",
            "--count",
            f"{t20.previous_head}..{t20.new_head}",
        )
        == "1"
    )

    # -- T21 pushed exactly that commit, and only it -----------------------
    assert pushed.status is PushStatus.PUSHED, pushed.reason
    assert pushed.is_pushed is True
    assert pushed.is_refusal is False
    assert pushed.push_attempted is True
    assert pushed.expected_commit == t20.new_head
    assert pushed.branch == AGENT_BRANCH
    assert pushed.remote_branch == AGENT_BRANCH
    assert pushed.refspec == build_refspec(AGENT_BRANCH, AGENT_BRANCH)
    assert pushed.remote_before_commit == t20.previous_head
    assert pushed.remote_after_commit == t20.new_head
    assert pushed.gates.decision is PushDecision.PROCEED
    assert pushed.gates.failed_gates == ()
    # G43/G48 prove exactly one push ran; G44/G46 prove the remote was read back
    # at that commit.
    for gate_id in ("G43", "G44", "G45", "G46", "G47", "G48"):
        assert pushed.gates.status_of(gate_id) is GateStatus.PASS, gate_id

    # -- the destination is a safe AI branch, never a protected one --------
    assert AGENT_BRANCH.startswith("ai/sonar-fix/")
    assert AGENT_BRANCH not in PROTECTED_BRANCH_NAMES
    assert AGENT_BRANCH != "main"

    # -- independent proof: read the BARE remote directly ------------------
    assert (
        git_run(committed.remote, "rev-parse", f"refs/heads/{AGENT_BRANCH}")
        == t20.new_head
    )

    # -- the local repository still holds exactly the T20 commit -----------
    assert git_run(committed.work, "rev-parse", "HEAD") == t20.new_head
    assert git_run(committed.work, "branch", "--show-current") == AGENT_BRANCH


@pytest.mark.parametrize("shape", ["object", "dict"])
def test_a_real_t20_commit_reaches_a_real_t21_push(committed, shape):
    """PATH A: ``CommitResult``; PATH B: ``CommitResult.as_dict()``."""
    t20 = committed.result
    before = snapshot(committed.work, remotes=False)

    pushed = push(committed, t20 if shape == "object" else t20.as_dict())

    assert_pushed(committed, pushed)
    # T21 must not mutate the local repository beyond Git's own remote-tracking
    # bookkeeping (which ``remotes=False`` deliberately excludes).
    assert snapshot(committed.work, remotes=False) == before


# ---------------------------------------------------------------------------
# The pinned contract: CommitResult.as_dict() -> _project_t20 -> PushFacts
# ---------------------------------------------------------------------------

#: Every key path ``git_push._project_t20`` reads out of the T20 ``as_dict()``
#: view. Renaming or dropping one of these without updating T21 must fail here.
CONSUMED_KEY_PATHS = (
    ("status",),
    ("is_committed",),
    ("new_head",),
    ("commit_sha",),
    ("previous_head",),
    ("needs_attention",),
    ("repository", "is_valid"),
    ("repository", "worktree_root"),
    ("repository", "expected_root"),
    ("repository", "branch"),
)


def real_commit_result() -> CommitResult:
    """A real (production-typed) T20 ``CommitResult``; needs no Git."""
    return CommitResult(
        status=CommitStatus.COMMITTED,
        reason="fixture",
        gates=evaluate_commit_gates(CommitFacts(), phase=CommitPhase.VERIFY),
        previous_head=PREVIOUS_HEAD,
        new_head=NEW_HEAD,
        commit_sha=NEW_HEAD,
        repository=RepositoryIdentity(
            expected_root=str(REPO_ROOT),
            worktree_root=str(REPO_ROOT),
            git_dir=str(REPO_ROOT / ".git"),
            common_dir=str(REPO_ROOT / ".git"),
            index_path=str(REPO_ROOT / ".git" / "index"),
            head_commit=NEW_HEAD,
            branch=AGENT_BRANCH,
            detached=False,
            matches_expected=True,
        ),
    )


class TestCommitResultProjectionContract:
    def test_as_dict_still_carries_every_key_t21_projects(self):
        view = real_commit_result().as_dict()
        for path in CONSUMED_KEY_PATHS:
            node = view
            for key in path:
                assert isinstance(node, Mapping), ".".join(path)
                assert key in node, (
                    "CommitResult.as_dict() no longer carries "
                    f"'{'.'.join(path)}', which git_push._project_t20 reads."
                )
                node = node[key]

    def test_the_projection_maps_every_consumed_value(self):
        executor = GitPushExecutor()
        expected = {
            "t20_status": "committed",
            "t20_is_committed": True,
            "t20_new_head": NEW_HEAD,
            "t20_previous_head": PREVIOUS_HEAD,
            "t20_commit_sha": NEW_HEAD,
            "t20_needs_attention": False,
            "t20_repository_valid": True,
            "t20_repository_root": str(REPO_ROOT),
            "t20_repository_branch": AGENT_BRANCH,
        }
        result = real_commit_result()
        assert executor._project_t20(result) == expected
        # Both supported shapes project identically.
        assert executor._project_t20(result.as_dict()) == expected

    def test_the_repository_root_falls_back_to_expected_root(self):
        executor = GitPushExecutor()
        view = real_commit_result().as_dict()
        del view["repository"]["worktree_root"]
        assert executor._project_t20(view)["t20_repository_root"] == str(REPO_ROOT)

    def test_a_missing_key_degrades_to_a_refusal_not_an_inference(self):
        """A dropped key becomes ``None`` (fail closed), never a guessed value."""
        executor = GitPushExecutor()
        view = real_commit_result().as_dict()
        del view["status"]
        assert executor._project_t20(view)["t20_status"] is None
        view = real_commit_result().as_dict()
        view["repository"] = {}
        projection = executor._project_t20(view)
        assert projection["t20_repository_valid"] is None
        assert projection["t20_repository_branch"] is None
