"""T21 integration and adversarial tests: the safe Git push executor.

Every test runs against real, local ``tmp_path`` repositories built by
``tests/t21_fixtures.py``: the remote is a bare repository on disk, so no test
reaches the network, and the project's own working copy is never a push target.

The suite covers four directions:

* the happy path - exactly one push, the exact argv, a read-back proof, and a
  local repository that is bit-for-bit unchanged;
* refusals - every destination, branch, remote and T20-contract precondition
  that must stop T21 *before* the one push is launched;
* faults - a rejected push, a timed-out push, a push that lies, an unreadable
  remote and a missing Git executable;
* the boundary itself - the command allow-list, the environment hardening, the
  pre-push checkpoint race and the module-level API.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from commit_policy import UNSAFE_GIT_ENVIRONMENT_VARIABLES

import git_push
import push_policy
from git_push import (
    ALLOWED_GIT_SUBCOMMANDS,
    FORBIDDEN_GIT_SUBCOMMANDS,
    GitPushError,
    GitPushExecutor,
    PushResult,
    PushStatus,
    _validate_git_invocation,
)
from push_policy import (
    GATES,
    PROTECTED_BRANCH_NAMES,
    GateStatus,
    PushDecision,
    PushPolicyConfig,
    PushVerificationStatus,
    build_refspec,
)

from t21_fixtures import (
    AGENT_BRANCH,
    OTHER_AGENT_BRANCH,
    OUT_OF_BAND_COMMIT,
    REMOTE,
    REMOTE_URL,
    RecordingRunner,
    build_push_repo,
    fake_completed,
    git_run,
    real_runner,
    snapshot,
)

#: The stages a complete run records, in order.
EXPECTED_STAGES = (
    "S0-validate-inputs",
    "S1-check-t20-contract",
    "S2-check-environment",
    "S3-identify-repository",
    "S4-check-branch",
    "S5-check-commit",
    "S6-check-worktree",
    "S7-inspect-remote",
    "S8-resolve-target",
    "S9-prove-ancestry",
    "S10-final-pre-push-check",
    "S11-push",
    "S12-verify-remote",
    "S13-verify-local",
)


@pytest.fixture
def fx(make_remote_repo):
    """A real clone on the agent branch with exactly one unpublished commit."""
    return build_push_repo(make_remote_repo())


def run_push(
    fx,
    runner=None,
    *,
    remote_branch=None,
    commit_result=None,
    branch=None,
    remote=REMOTE,
    expected_commit=None,
    config=None,
    environment=None,
):
    """Run one T21 attempt against the fixture and return ``(result, runner)``."""
    runner = runner if runner is not None else RecordingRunner()
    executor = GitPushExecutor(
        runner=runner,
        config=config if config is not None else PushPolicyConfig(default_branch="main"),
        environment=environment,
    )
    result = executor.push_safely(
        repository_path=str(fx.work),
        commit_result=commit_result if commit_result is not None else fx.t20_result(),
        branch=branch,
        remote=remote,
        remote_branch=remote_branch if remote_branch is not None else fx.branch,
        expected_commit=expected_commit,
    )
    return result, runner


def assert_refused(result: PushResult, runner: RecordingRunner, *gate_ids: str) -> None:
    """Assert a refusal happened, nothing was pushed, and ``gate_ids`` failed."""
    assert result.status is PushStatus.REFUSED, result.reason
    assert result.is_refusal is True
    assert result.is_pushed is False
    assert runner.push_count == 0
    failed = {gate.gate_id for gate in result.gates.failed_gates}
    for gate_id in gate_ids:
        assert gate_id in failed, f"{gate_id} did not fail: {sorted(failed)}"
    assert result.gates.decision is PushDecision.REFUSE


def remote_refs(root: Path) -> dict:
    """Every remote-tracking ref of ``root`` as ``{refname: commit}``."""
    text = git_run(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")
    refs = {}
    for line in text.splitlines():
        name, _, commit = line.partition(" ")
        if name.strip():
            refs[name.strip()] = commit.strip()
    return refs


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestSuccessfulPush:
    def test_exactly_one_verified_push_moves_only_the_destination_branch(self, fx):
        result, runner = run_push(fx)
        assert result.status is PushStatus.PUSHED, result.reason
        assert result.is_pushed is True
        assert result.is_refusal is False
        assert result.needs_attention is False
        assert result.push_attempted is True
        assert runner.push_count == 1
        assert result.expected_commit == fx.head
        assert result.branch == fx.branch
        assert result.remote_branch == fx.branch
        assert result.refspec == build_refspec(fx.branch, fx.branch)
        assert result.remote_before_commit == fx.remote_before
        assert result.remote_after_commit == fx.head
        assert result.gates.decision is PushDecision.PROCEED
        assert result.gates.failed_gates == ()
        for gate_id in ("G43", "G44", "G45", "G46", "G47", "G48"):
            assert result.gates.status_of(gate_id) is GateStatus.PASS, gate_id
        assert fx.remote_ref() == fx.head

    def test_the_single_push_argument_vector_is_exact(self, fx):
        _result, runner = run_push(fx)
        assert runner.push_commands == [
            [
                "git",
                "-C",
                str(fx.work.resolve()),
                "push",
                REMOTE,
                f"refs/heads/{fx.branch}:refs/heads/{fx.branch}",
            ]
        ]

    def test_the_run_records_every_stage_in_order(self, fx):
        result, _runner = run_push(fx)
        stages = [record.stage for record in result.stage_records]
        assert stages == list(EXPECTED_STAGES)
        assert all(record.detail for record in result.stage_records)

    def test_the_local_repository_is_unchanged_by_a_successful_push(self, fx):
        before = snapshot(fx.work, remotes=False)
        refs_before = remote_refs(fx.work)
        result, _runner = run_push(fx)
        assert result.status is PushStatus.PUSHED, result.reason
        assert snapshot(fx.work, remotes=False) == before
        refs_after = remote_refs(fx.work)
        assert refs_after[f"refs/remotes/{REMOTE}/{fx.branch}"] == fx.head
        for name, commit in refs_before.items():
            if name == f"refs/remotes/{REMOTE}/{fx.branch}":
                continue
            assert refs_after[name] == commit, name

    def test_a_new_remote_branch_is_created_at_the_recorded_commit(
        self, make_remote_repo
    ):
        fx = build_push_repo(make_remote_repo(), publish=False)
        assert fx.remote_before is None
        result, runner = run_push(fx)
        assert result.status is PushStatus.PUSHED, result.reason
        assert runner.push_count == 1
        assert result.remote_before_commit is None
        assert result.remote_after_commit == fx.head
        assert fx.remote_ref() == fx.head

    def test_the_default_branch_is_resolved_from_the_remote_head(self, fx):
        result, _runner = run_push(fx, config=PushPolicyConfig())
        assert result.status is PushStatus.PUSHED, result.reason
        detail = [
            record.detail
            for record in result.stage_records
            if record.stage == "S4-check-branch"
        ]
        assert detail and "default_branch='main'" in detail[0]

    def test_the_module_level_helper_pushes_once(self, fx):
        result = git_push.push_safely(
            repository_path=str(fx.work),
            commit_result=fx.t20_result(),
            remote_branch=fx.branch,
        )
        assert result.status is PushStatus.PUSHED, result.reason
        assert result.push_attempted is True
        assert fx.remote_ref() == fx.head

    def test_the_result_serialises_every_pushed_fact(self, fx):
        result, _runner = run_push(fx)
        payload = result.as_dict()
        assert payload["status"] == "pushed"
        assert payload["is_pushed"] is True
        assert payload["needs_attention"] is False
        assert payload["refspec"] == build_refspec(fx.branch, fx.branch)
        assert payload["remote_before_commit"] == fx.remote_before
        assert payload["remote_after_commit"] == fx.head
        assert len(payload["gates"]["gates"]) == len(GATES)


# ---------------------------------------------------------------------------
# Refusals: nothing is ever pushed
# ---------------------------------------------------------------------------


class TestPrePushRefusals:
    def test_an_out_of_band_expected_commit_is_refused(self, fx):
        result, runner = run_push(fx, expected_commit=OUT_OF_BAND_COMMIT)
        # G4 refuses at S1, so the later gates that would also fail are simply not
        # reached: T21 stops before it reads anything else.
        assert_refused(result, runner, "G4")
        assert result.gates.status_of("G16") is GateStatus.NOT_REACHED
        assert result.stage_records[-1].stage == "S1-check-t20-contract"
        assert fx.remote_ref() == fx.remote_before

    def test_a_t20_result_that_is_not_a_verified_commit_is_refused(self, fx):
        for status, is_committed in (
            ("refused", False),
            ("commit-failed", False),
            ("committed", None),
        ):
            result, runner = run_push(
                fx,
                commit_result=fx.t20_result(
                    status=status, is_committed=is_committed
                ),
            )
            assert_refused(result, runner, "G2")

    def test_a_missing_destination_branch_is_refused(self, fx):
        result, runner = run_push(fx, remote_branch="")
        assert_refused(result, runner, "G1")
        assert fx.remote_ref() == fx.remote_before

    def test_a_protected_destination_branch_is_refused(self, fx):
        for name in PROTECTED_BRANCH_NAMES:
            result, runner = run_push(fx, remote_branch=name)
            assert_refused(result, runner, "G28", "G30")

    def test_a_destination_outside_the_agent_namespace_is_refused(self, fx):
        result, runner = run_push(fx, remote_branch="release/1.0")
        assert_refused(result, runner, "G30")
        assert fx.remote_ref("release/1.0") is None

    def test_a_protected_source_branch_is_refused(self, fx):
        for name in ("main", "master"):
            result, runner = run_push(
                fx,
                branch=name,
                commit_result=fx.t20_result(branch=name),
            )
            assert_refused(result, runner, "G12", "G13")
        assert fx.remote_ref("main") is not None  # untouched, still the seed value

    def test_a_source_branch_outside_the_agent_namespace_is_refused(self, fx):
        result, runner = run_push(
            fx,
            branch="feature/other",
            commit_result=fx.t20_result(branch="feature/other"),
        )
        assert_refused(result, runner, "G12", "G13")

    def test_a_missing_remote_is_refused(self, fx):
        result, runner = run_push(fx, remote="upstream")
        assert_refused(result, runner, "G23")

    def test_a_remote_that_cannot_be_read_is_refused(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "ls-remote":
                return fake_completed(
                    returncode=128, stderr="Authentication failed"
                )
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert_refused(result, runner, "G23")
        assert "Authentication failed" in result.reason

    def test_a_remote_url_with_credentials_is_refused_and_redacted(self, fx):
        secret = "sup3rsecret"
        git_run(
            fx.work,
            "remote",
            "set-url",
            REMOTE,
            f"https://user:{secret}@example.invalid/acme/demo.git",
        )
        result, runner = run_push(fx)
        assert_refused(result, runner, "G26")
        assert secret not in result.reason
        assert secret not in str(result.as_dict())
        assert secret not in str(result.remote.as_dict())

    def test_an_alternate_push_url_is_refused(self, fx):
        git_run(
            fx.work,
            "remote",
            "set-url",
            "--add",
            "--push",
            REMOTE,
            REMOTE_URL,
        )
        result, runner = run_push(fx)
        assert_refused(result, runner, "G24")

    def test_a_default_push_refspec_is_refused(self, fx):
        git_run(fx.work, "config", f"remote.{REMOTE}.push", "refs/heads/*:refs/heads/*")
        result, runner = run_push(fx)
        assert_refused(result, runner, "G24")
        assert fx.remote_ref() == fx.remote_before

    def test_a_dirty_worktree_is_refused(self, fx):
        (fx.work / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        result, runner = run_push(fx)
        assert_refused(result, runner, "G19")

    def test_a_staged_path_is_refused(self, fx):
        (fx.work / "staged.txt").write_text("staged\n", encoding="utf-8")
        git_run(fx.work, "add", "--", "staged.txt")
        result, runner = run_push(fx)
        assert_refused(result, runner, "G19", "G20")

    def test_a_detached_head_is_refused(self, fx):
        git_run(fx.work, "checkout", "--detach", "-q")
        result, runner = run_push(fx)
        # G9 refuses at S3; the branch gates (G11/G12) are not reached.
        assert_refused(result, runner, "G9")
        assert result.gates.status_of("G12") is GateStatus.NOT_REACHED

    def test_a_merge_in_progress_is_refused(self, fx):
        merge_head = fx.work / ".git" / "MERGE_HEAD"
        merge_head.write_text(f"{fx.remote_before}\n", encoding="utf-8")
        result, runner = run_push(fx)
        assert_refused(result, runner, "G21")

    def test_an_already_pushed_commit_is_refused(self, fx):
        first, _runner = run_push(fx)
        assert first.status is PushStatus.PUSHED, first.reason
        result, runner = run_push(fx)
        # The second attempt refuses at S9: the push would add zero commits.
        assert_refused(result, runner, "G37")
        assert result.gates.status_of("G46") is GateStatus.NOT_REACHED
        assert fx.remote_ref() == fx.head

    def test_a_remote_that_moved_out_of_band_is_refused(self, fx):
        other = fx.remote.parent / "t21-other"
        git_run(fx.remote.parent, "clone", str(fx.remote), str(other))
        git_run(other, "config", "user.name", "Other User")
        git_run(other, "config", "user.email", "other@example.com")
        git_run(other, "checkout", "-q", fx.branch)
        (other / "other.txt").write_text("out of band\n", encoding="utf-8")
        git_run(other, "add", "--", "other.txt")
        git_run(other, "commit", "-q", "-m", "chore: out-of-band commit")
        git_run(
            other,
            "push",
            "-q",
            REMOTE,
            f"refs/heads/{fx.branch}:refs/heads/{fx.branch}",
        )
        diverged = fx.remote_ref()
        assert diverged not in (None, fx.head)
        result, runner = run_push(fx)
        assert_refused(result, runner, "G37", "G38")
        assert fx.remote_ref() == diverged

    def test_a_bare_repository_is_never_pushed_from(self, fx):
        runner = RecordingRunner()
        executor = GitPushExecutor(
            runner=runner, config=PushPolicyConfig(default_branch="main")
        )
        result = executor.push_safely(
            repository_path=str(fx.remote),
            commit_result=fx.t20_result(worktree_root=str(fx.remote.resolve())),
            remote_branch=fx.branch,
        )
        assert_refused(result, runner, "G6", "G7", "G8")

    def test_a_directory_that_is_not_a_repository_is_refused(self, fx, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        runner = RecordingRunner()
        executor = GitPushExecutor(
            runner=runner, config=PushPolicyConfig(default_branch="main")
        )
        result = executor.push_safely(
            repository_path=str(plain),
            commit_result=fx.t20_result(worktree_root=str(plain.resolve())),
            remote_branch=fx.branch,
        )
        assert_refused(result, runner, "G6")

    def test_an_unsafe_environment_refuses_before_any_git_process(self, fx):
        result, runner = run_push(
            fx, environment={"GIT_DIR": str(fx.remote), "PATH": ""}
        )
        assert_refused(result, runner, "G10")
        assert runner.commands == []

    def test_a_repository_path_that_is_not_a_directory_is_a_caller_error(self, fx):
        executor = GitPushExecutor(
            runner=RecordingRunner(), config=PushPolicyConfig(default_branch="main")
        )
        with pytest.raises(GitPushError):
            executor.push_safely(
                repository_path=str(fx.work / "nowhere"),
                commit_result=fx.t20_result(),
                remote_branch=fx.branch,
            )

    def test_a_missing_repository_path_is_a_caller_error(self, fx):
        executor = GitPushExecutor(
            runner=RecordingRunner(), config=PushPolicyConfig(default_branch="main")
        )
        with pytest.raises(GitPushError):
            executor.push_safely(
                repository_path="",
                commit_result=fx.t20_result(),
                remote_branch=fx.branch,
            )


# ---------------------------------------------------------------------------
# Faults: a push that fails, times out, lies or cannot be read back
# ---------------------------------------------------------------------------


class TestPushFaultsAndVerification:
    def test_a_rejected_push_is_failed_and_never_retried(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "push":
                return fake_completed(
                    returncode=1, stderr="! [remote rejected] protected branch"
                )
            return None

        before = snapshot(fx.work, remotes=False)
        result, runner = run_push(fx, RecordingRunner(on_call))
        assert result.status is PushStatus.PUSH_FAILED, result.reason
        assert result.push_attempted is True
        assert result.needs_attention is True
        assert runner.push_count == 1
        assert result.push.returncode == 1
        assert result.gates.status_of("G43") is GateStatus.FAIL
        assert result.gates.status_of("G42") is GateStatus.PASS
        for mutating in (
            "fetch",
            "pull",
            "reset",
            "clean",
            "stash",
            "checkout",
            "add",
            "commit",
            "tag",
            "branch",
        ):
            assert mutating not in runner.subcommands
        assert fx.remote_ref() == fx.remote_before
        assert snapshot(fx.work, remotes=False) == before

    def test_a_timed_out_push_is_failed_without_a_second_attempt(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "push":
                raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1.0)
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert result.status is PushStatus.PUSH_FAILED, result.reason
        assert result.push.timed_out is True
        assert result.push_attempted is True
        assert runner.push_count == 1
        assert fx.remote_ref() == fx.remote_before

    def test_a_push_that_reports_success_without_moving_the_remote_is_unverified(
        self, fx
    ):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "push":
                return fake_completed(returncode=0, stdout="Everything up-to-date")
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert result.status is PushStatus.PUSH_UNVERIFIED, result.reason
        assert result.needs_attention is True
        assert runner.push_count == 1
        assert result.gates.status_of("G45") is GateStatus.FAIL
        assert fx.remote_ref() == fx.remote_before

    def test_a_remote_that_cannot_be_read_back_is_unverified(self, fx):
        state = {"pushed": False}

        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "push":
                state["pushed"] = True
                return None
            if subcommand == "ls-remote" and state["pushed"]:
                return fake_completed(
                    returncode=128, stderr="fatal: could not read from remote"
                )
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert result.status is PushStatus.PUSH_UNVERIFIED, result.reason
        assert result.push.returncode == 0
        assert runner.push_count == 1
        assert result.gates.status_of("G44") is GateStatus.FAIL
        assert fx.remote_ref() == fx.head
        assert "human" in result.reason

    def test_a_missing_git_executable_is_a_caller_error(self, fx):
        executor = GitPushExecutor(
            git_command="definitely-not-a-git-binary",
            config=PushPolicyConfig(default_branch="main"),
        )
        with pytest.raises(GitPushError):
            executor.push_safely(
                repository_path=str(fx.work),
                commit_result=fx.t20_result(),
                remote_branch=fx.branch,
            )

    def test_the_argv_builder_revalidates_the_push_before_the_launch(self, fx):
        executor = GitPushExecutor(
            runner=RecordingRunner(), config=PushPolicyConfig(default_branch="main")
        )
        request = push_policy.PushRequest(
            repository_path=str(fx.work.resolve()),
            expected_commit=fx.head,
            branch=fx.branch,
            remote=REMOTE,
            remote_branch=fx.branch,
        )
        argv = executor._push_argv(fx.work.resolve(), request)
        assert argv == (
            "git",
            "-C",
            str(fx.work.resolve()),
            "push",
            REMOTE,
            build_refspec(fx.branch, fx.branch),
        )

    def test_a_request_that_cannot_yield_a_push_body_is_refused(self, fx):
        executor = GitPushExecutor(
            runner=RecordingRunner(), config=PushPolicyConfig(default_branch="main")
        )
        hostile = push_policy.PushRequest(
            repository_path=str(fx.work.resolve()),
            expected_commit=fx.head,
            branch="bad..name",
            remote=REMOTE,
            remote_branch=fx.branch,
        )
        assert hostile.refspec == ""
        with pytest.raises(GitPushError):
            executor._push_argv(fx.work.resolve(), hostile)


# ---------------------------------------------------------------------------
# The mutation boundary: one push, argv only, hardened environment
# ---------------------------------------------------------------------------


class TestPushBoundary:
    def test_every_launched_subcommand_is_read_only_except_the_one_push(self, fx):
        result, runner = run_push(fx)
        assert result.status is PushStatus.PUSHED, result.reason
        subcommands = runner.subcommands
        assert set(subcommands) <= ALLOWED_GIT_SUBCOMMANDS
        assert set(subcommands) & FORBIDDEN_GIT_SUBCOMMANDS == set()
        assert subcommands.count("push") == 1

    def test_the_command_set_of_a_successful_run_is_the_documented_one(self, fx):
        _result, runner = run_push(fx)
        assert set(runner.subcommands) == {
            "rev-parse",
            "rev-list",
            "symbolic-ref",
            "status",
            "diff",
            "config",
            "merge-base",
            "ls-remote",
            "push",
        }

    def test_every_launched_command_targets_the_discovered_repository(self, fx):
        _result, runner = run_push(fx)
        expected_root = str(fx.work.resolve())
        for argv in runner.commands:
            assert argv[0] == "git"
            assert argv[1] == "-C"
            assert argv[2] == expected_root

    def test_the_push_body_is_the_exact_three_token_command(self, fx):
        _result, runner = run_push(fx)
        argv = runner.push_commands[0]
        assert argv[3:] == [
            "push",
            REMOTE,
            build_refspec(fx.branch, fx.branch),
        ]
        assert not any(token.startswith("-") for token in argv[3:])

    def test_every_child_environment_is_hardened(self, fx):
        path = os.environ.get("PATH", "")
        _result, runner = run_push(fx, environment={"PATH": path})
        assert runner.environments
        for env in runner.environments:
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert env["GIT_OPTIONAL_LOCKS"] == "0"
            assert env["GIT_PAGER"] == "cat"
            for name in UNSAFE_GIT_ENVIRONMENT_VARIABLES:
                assert name not in env, name

    def test_a_hostile_environment_is_never_mutated_and_launches_nothing(self, fx):
        environment = {"PATH": os.environ.get("PATH", ""), "GIT_DIR": str(fx.remote)}
        snapshot_env = dict(environment)
        result, runner = run_push(fx, environment=environment)
        assert_refused(result, runner, "G10")
        assert environment == snapshot_env
        assert runner.commands == []
        assert runner.environments == []

    def test_configuration_is_only_read_never_written(self, fx):
        _result, runner = run_push(fx)
        config_commands = runner.subcommands_matching("config")
        assert config_commands
        for argv in config_commands:
            tokens = argv[3:]
            assert any(
                token in {"--get", "--get-all", "--get-regexp", "--list", "-l"}
                for token in tokens
            )
            for writer in (
                "--add",
                "--unset",
                "--unset-all",
                "--replace-all",
                "--rename-section",
                "--remove-section",
                "--edit",
            ):
                assert writer not in tokens

    def test_the_allow_list_and_the_forbidden_list_never_overlap(self):
        assert ALLOWED_GIT_SUBCOMMANDS & FORBIDDEN_GIT_SUBCOMMANDS == frozenset()
        assert "push" in ALLOWED_GIT_SUBCOMMANDS
        assert "push" not in FORBIDDEN_GIT_SUBCOMMANDS

    @pytest.mark.parametrize("subcommand", sorted(FORBIDDEN_GIT_SUBCOMMANDS))
    def test_every_forbidden_subcommand_is_refused_before_launch(self, subcommand):
        with pytest.raises(GitPushError):
            _validate_git_invocation((subcommand,))

    def test_an_unknown_or_empty_invocation_is_refused(self):
        for args in ((), ("lol",), ("--verbose",), ("--",)):
            with pytest.raises(GitPushError):
                _validate_git_invocation(args)

    def test_hostile_push_bodies_are_refused(self):
        refspec = build_refspec(AGENT_BRANCH, AGENT_BRANCH)
        hostile = (
            (),
            (REMOTE,),
            (REMOTE, refspec, "extra"),
            ("--force", REMOTE, refspec),
            ("-f", REMOTE, refspec),
            ("--force-with-lease", REMOTE, refspec),
            ("--mirror", REMOTE, refspec),
            ("--tags", REMOTE, refspec),
            ("--follow-tags", REMOTE, refspec),
            ("--delete", REMOTE, AGENT_BRANCH),
            ("-d", REMOTE, AGENT_BRANCH),
            ("--set-upstream", REMOTE, refspec),
            ("--push-option", "ci.skip", REMOTE, refspec),
            ("--no-verify", REMOTE, refspec),
            ("--porcelain", REMOTE, refspec),
            (REMOTE, "HEAD"),
            (REMOTE, "+" + refspec),
            (REMOTE, "refs/heads/*:refs/heads/*"),
            (REMOTE, ":refs/heads/main"),
            (REMOTE, "refs/heads/main"),
            (REMOTE, f"{AGENT_BRANCH}:{OTHER_AGENT_BRANCH}"),
            (REMOTE, f"refs/heads/{AGENT_BRANCH}:refs/heads/main:refs/heads/other"),
        )
        for body in hostile:
            with pytest.raises(GitPushError):
                _validate_git_invocation(("push", *body))

    def test_repository_redirecting_options_are_refused(self):
        for args in (
            ("-C", "/tmp/other", "status"),
            ("-C/tmp/other", "status"),
            ("--git-dir", "/tmp/other", "status"),
            ("--git-dir=/tmp/other", "status"),
            ("--work-tree", "/tmp/other", "status"),
            ("--work-tree=/tmp/other", "status"),
            ("--namespace=ns", "status"),
            ("--exec-path=/tmp/evil", "status"),
        ):
            with pytest.raises(GitPushError):
                _validate_git_invocation(args)

    def test_configuration_overrides_are_refused(self):
        for args in (
            ("-c", "core.hooksPath=/tmp/hooks", "status"),
            ("-c", "core.sshCommand=/tmp/evil", "status"),
            ("-c", "credential.helper=/tmp/evil", "status"),
            ("-ccore.hooksPath=/tmp/hooks", "status"),
        ):
            with pytest.raises(GitPushError):
                _validate_git_invocation(args)

    def test_configuration_writers_are_refused(self):
        for args in (
            ("config",),
            ("config", "user.name"),
            ("config", "--local", "user.name", "x"),
            ("config", "--global", "user.name", "x"),
            ("config", "--system", "user.name", "x"),
            ("config", "--worktree", "user.name", "x"),
            ("config", "--file", "/tmp/x", "user.name", "x"),
            ("config", "--add", "remote.x.url", "https://example.invalid/x"),
            ("config", "--unset-all", "user.name"),
            ("config", "--edit"),
            ("config", "--remove-section", "remote.origin"),
        ):
            with pytest.raises(GitPushError):
                _validate_git_invocation(args)

    def test_the_configuration_read_verbs_t21_uses_are_allowed(self):
        for args in (
            ("config", "--get-all", "remote.origin.url"),
            ("config", "--get-regexp", "^url\\."),
            ("config", "--local", "--get-all", "user.name"),
        ):
            assert _validate_git_invocation(args) == "config"


# ---------------------------------------------------------------------------
# The pre-push checkpoint race
# ---------------------------------------------------------------------------


class TestPrePushCheckpointRace:
    def test_a_path_created_between_the_checkpoints_stops_the_push(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "status" and runner.subcommands.count("status") == 2:
                completed = real_runner(
                    argv, cwd, dict(runner.environments[-1]), 60.0
                )
                (fx.work / "raced.txt").write_text("raced\n", encoding="utf-8")
                return completed
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert_refused(result, runner, "G41")
        assert fx.remote_ref() == fx.remote_before

    def test_a_commit_that_lands_between_the_checkpoints_stops_the_push(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "status" and runner.subcommands.count("status") == 2:
                completed = real_runner(
                    argv, cwd, dict(runner.environments[-1]), 60.0
                )
                (fx.work / "raced.txt").write_text("raced\n", encoding="utf-8")
                git_run(fx.work, "add", "--", "raced.txt")
                git_run(fx.work, "commit", "-q", "-m", "chore: raced commit")
                return completed
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert_refused(result, runner, "G16", "G39")
        assert fx.remote_ref() == fx.remote_before

    def test_a_staged_path_that_appears_late_stops_the_push(self, fx):
        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "status" and runner.subcommands.count("status") == 3:
                completed = real_runner(
                    argv, cwd, dict(runner.environments[-1]), 60.0
                )
                (fx.work / "late.txt").write_text("late\n", encoding="utf-8")
                git_run(fx.work, "add", "--", "late.txt")
                return completed
            return None

        result, runner = run_push(fx, RecordingRunner(on_call))
        assert_refused(result, runner, "G41")
        assert fx.remote_ref() == fx.remote_before


# ---------------------------------------------------------------------------
# The project's own working copy stays untouched
# ---------------------------------------------------------------------------


class TestRealRepositorySafety:
    def test_the_projects_own_working_copy_is_never_a_push_target(self):
        repo = Path(__file__).resolve().parents[1]
        assert (repo / "git_push.py").exists()
        before = snapshot(repo)
        head = git_run(repo, "rev-parse", "HEAD")
        branch = git_run(repo, "branch", "--show-current")
        previous = git_run(repo, "rev-parse", "HEAD^")

        def on_call(subcommand, argv, cwd, runner):
            if subcommand == "push":
                return fake_completed(
                    returncode=1, stderr="refused by the test runner"
                )
            return None

        runner = RecordingRunner(on_call)
        executor = GitPushExecutor(
            runner=runner, config=PushPolicyConfig(default_branch="main")
        )
        result = executor.push_safely(
            repository_path=str(repo),
            commit_result={
                "status": "committed",
                "is_committed": True,
                "new_head": head,
                "previous_head": previous,
                "commit_sha": head,
                "needs_attention": False,
                "repository": {
                    "worktree_root": str(repo),
                    "branch": branch,
                    "is_valid": True,
                },
            },
            remote_branch="ai/sonar-fix/never",
        )
        assert result.status is PushStatus.REFUSED, result.reason
        assert runner.push_count == 0
        assert snapshot(repo) == before
        assert before["head"] == head
        assert before["branch"] == branch


# ---------------------------------------------------------------------------
# The module-level API and the pipeline boundary
# ---------------------------------------------------------------------------


class TestModuleApiAndTrail:
    def test_the_four_outcomes_are_the_documented_ones(self):
        assert {status.value for status in PushStatus} == {
            "pushed",
            "refused",
            "push-failed",
            "push-unverified",
        }
        assert {status.value for status in PushVerificationStatus} == {
            "not-attempted",
            "skipped",
            "verified",
            "mismatch",
            "unavailable",
            "ambiguous",
        }

    def test_the_records_are_not_collected_by_pytest(self):
        assert PushStatus.__test__ is False
        assert PushResult.__test__ is False
        assert push_policy.PushGateEvaluation.__test__ is False
        assert push_policy.PushRequest.__test__ is False

    def test_a_refusal_is_never_flagged_for_attention(self, fx):
        result, runner = run_push(fx, remote_branch="")
        assert_refused(result, runner, "G1")
        assert result.needs_attention is False
        assert result.push_attempted is False
        assert result.push is None

    def test_the_executor_defaults_are_the_conservative_ones(self):
        executor = GitPushExecutor()
        assert executor._config.require_clean_worktree is True
        assert executor._config.protected_branches == PROTECTED_BRANCH_NAMES
        assert executor._config.required_branch_prefix == "ai/sonar-fix"

    def test_t21_is_not_wired_into_the_pipeline(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("main.py", "git_commit.py"):
            source = (root / name).read_text(encoding="utf-8")
            assert "git_push" not in source, name
            assert "push_policy" not in source, name

    def test_the_module_level_helper_needs_a_repository_and_a_t20_result(self):
        with pytest.raises(TypeError):
            git_push.push_safely()
        with pytest.raises(GitPushError):
            git_push.push_safely(
                repository_path="", commit_result={}, remote_branch="ai/sonar-fix/x"
            )
