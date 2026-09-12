"""T21 unit tests: the pure push-gate policy (no Git, no I/O, no subprocess).

Every test builds an immutable :class:`~push_policy.PushFacts` record from the
synthetic, internally consistent observations in ``tests/t21_fixtures.py``, then
flips exactly one fact so that one gate must refuse. The happy-path record
proves the other direction: all 48 gates pass.

The mutation boundary itself (the one ``git push``) is *not* exercised here: that
is ``tests/test_git_push.py``, which runs against real ``tmp_path`` repositories.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Tuple

import pytest

import git_commit
import push_policy
from push_policy import (
    BROADENING_PUSH_OPTIONS,
    GATES,
    NOT_REACHED_REASON,
    PROTECTED_BRANCH_NAMES,
    PUSH_SUBCOMMAND,
    T20_COMMITTED_STATUS,
    GateStatus,
    GitEnvironmentReport,
    PushDecision,
    PushFacts,
    PushPhase,
    PushPolicyConfig,
    PushPolicyError,
    PushVerificationStatus,
    build_refspec,
    evaluate_push_gates,
    is_full_commit_id,
    normalize_commit_id,
    parse_refspec,
    same_path,
    url_has_userinfo,
    validate_remote_name,
    validate_remote_url,
)

from t21_fixtures import (
    AGENT_BRANCH,
    COMMIT,
    OTHER_AGENT_BRANCH,
    OUT_OF_BAND_COMMIT,
    PREVIOUS_COMMIT,
    REMOTE,
    REMOTE_URL,
    REPO_ROOT,
    checkpoint,
    happy_facts,
    push_observation,
    remote_observation,
    repository_observation,
)

#: Every gate id, in evaluation order.
GATE_IDS: Tuple[str, ...] = tuple(GATES)


def evaluate(
    facts: PushFacts, phase: PushPhase = PushPhase.VERIFY
) -> "push_policy.PushGateEvaluation":
    """Evaluate every gate up to ``phase``."""
    return evaluate_push_gates(facts, phase=phase)


def failed_ids(
    facts: PushFacts, phase: PushPhase = PushPhase.VERIFY
) -> Tuple[str, ...]:
    """The ids of every failing gate at ``phase``, in evaluation order."""
    return tuple(gate.gate_id for gate in evaluate(facts, phase).failed_gates)


def assert_gate_fails(
    facts: PushFacts, gate_id: str, phase: PushPhase = PushPhase.VERIFY
) -> None:
    """Assert ``gate_id`` fails (fail closed) and the decision is a refusal."""
    evaluation = evaluate(facts, phase)
    gate = evaluation.gate(gate_id)
    assert gate is not None, gate_id
    assert gate.status is GateStatus.FAIL, (
        f"{gate_id} did not fail: {gate.status} - {gate.reason}"
    )
    assert gate.reason.strip(), gate_id
    assert evaluation.decision is PushDecision.REFUSE
    assert evaluation.is_refusal is True
    assert gate_id in {gate.gate_id for gate in evaluation.failed_gates}


def with_request(facts: PushFacts, **overrides) -> PushFacts:
    """Return ``facts`` with one field of its :class:`PushRequest` flipped."""
    return replace(facts, request=replace(facts.request, **overrides))


# ---------------------------------------------------------------------------
# One adversarial break per gate: gate id -> (what it breaks, how to break it)
# ---------------------------------------------------------------------------

GATE_BREAKS: Dict[str, Tuple[str, Callable[[PushFacts], PushFacts]]] = {
    "G1": (
        "the request omits the destination branch",
        lambda facts: with_request(facts, remote_branch=""),
    ),
    "G2": (
        "the T20 result reports a refusal instead of a commit",
        lambda facts: replace(
            facts, t20_status="refused", t20_is_committed=False
        ),
    ),
    "G3": (
        "the T20 result carries only an abbreviated commit id",
        lambda facts: replace(facts, t20_commit_sha=COMMIT[:8]),
    ),
    "G4": (
        "the request expects an out-of-band commit",
        lambda facts: with_request(facts, expected_commit=OUT_OF_BAND_COMMIT),
    ),
    "G5": (
        "the T20 result committed on a different branch",
        lambda facts: replace(facts, t20_repository_branch=OTHER_AGENT_BRANCH),
    ),
    "G6": (
        "the repository could not be resolved",
        lambda facts: replace(
            facts,
            repository=repository_observation(
                problems=("the repository could not be resolved",)
            ),
        ),
    ),
    "G7": (
        "the resolved worktree root is a different directory",
        lambda facts: replace(
            facts,
            repository=repository_observation(worktree_root="C:/work/elsewhere"),
        ),
    ),
    "G8": (
        "the repository is bare",
        lambda facts: replace(facts, repository=repository_observation(is_bare=True)),
    ),
    "G9": (
        "HEAD is detached, so no branch is checked out",
        lambda facts: replace(facts, repository=repository_observation(branch="")),
    ),
    "G10": (
        "GIT_DIR is set in the process environment",
        lambda facts: replace(
            facts,
            environment=GitEnvironmentReport(
                unsafe_variables=("GIT_DIR",),
                checked_variables=("GIT_DIR", "GIT_WORK_TREE"),
                reason="GIT_DIR is set.",
            ),
        ),
    ),
    "G11": (
        "the checked-out branch is only whitespace",
        lambda facts: replace(facts, repository=repository_observation(branch="   ")),
    ),
    "G12": (
        "a different branch is checked out than the request expects",
        lambda facts: replace(
            facts, repository=repository_observation(branch=OTHER_AGENT_BRANCH)
        ),
    ),
    "G13": (
        "the source branch is outside the agent namespace",
        lambda facts: replace(
            facts,
            repository=repository_observation(branch="topic/other"),
            t20_repository_branch="topic/other",
            request=replace(facts.request, branch="topic/other"),
        ),
    ),
    "G14": (
        "the source branch is a protected branch",
        lambda facts: replace(
            facts,
            repository=repository_observation(branch="develop"),
            t20_repository_branch="develop",
            request=replace(facts.request, branch="develop"),
        ),
    ),
    "G15": (
        "the source branch is the repository's default branch",
        lambda facts: replace(
            facts,
            config=PushPolicyConfig(),
            resolved_default_branch=AGENT_BRANCH,
        ),
    ),
    "G16": (
        "HEAD is not the recorded commit",
        lambda facts: replace(facts, head_commit=OUT_OF_BAND_COMMIT),
    ),
    "G17": (
        "the recorded commit does not exist in this repository",
        lambda facts: replace(facts, commit_exists=False),
    ),
    "G18": (
        "the source branch tip is a later, unrecorded commit",
        lambda facts: replace(facts, branch_tip=OUT_OF_BAND_COMMIT),
    ),
    "G19": (
        "the working tree is not clean",
        lambda facts: replace(facts, worktree_clean=False),
    ),
    "G20": (
        "a path is still staged in the index",
        lambda facts: replace(facts, staged_files=("src/app.py",)),
    ),
    "G21": (
        "a merge is in progress",
        lambda facts: replace(facts, operation_in_progress=("MERGE_HEAD",)),
    ),
    "G22": (
        "the resolved remote name is a different remote",
        lambda facts: replace(facts, remote_name="upstream"),
    ),
    "G23": (
        "the destination remote does not exist in this repository",
        lambda facts: replace(
            facts, remote_observation=remote_observation(exists=False)
        ),
    ),
    "G24": (
        "the remote sets an alternate push URL",
        lambda facts: replace(
            facts, remote_observation=remote_observation(alternate_push_url=True)
        ),
    ),
    "G25": (
        "the remote URL is not a URL shape T21 accepts",
        lambda facts: replace(
            facts, remote_observation=remote_observation(url_well_formed=False)
        ),
    ),
    "G26": (
        "the remote URL embeds credentials",
        lambda facts: replace(
            facts, remote_observation=remote_observation(url_userinfo=True)
        ),
    ),
    "G27": (
        "the destination branch name is not a valid Git branch name",
        lambda facts: with_request(facts, remote_branch="bad..branch"),
    ),
    "G28": (
        "the destination branch is a protected branch",
        lambda facts: with_request(facts, remote_branch="main"),
    ),
    "G29": (
        "the destination branch is the repository's default branch",
        lambda facts: replace(
            facts,
            config=PushPolicyConfig(),
            resolved_default_branch=AGENT_BRANCH,
        ),
    ),
    "G30": (
        "the destination branch is outside the agent namespace",
        lambda facts: with_request(facts, remote_branch="release/1.0"),
    ),
    "G31": (
        "the recorded refspec is not the refspec T21 builds",
        lambda facts: replace(facts, refspec=f"refs/heads/{AGENT_BRANCH}"),
    ),
    "G32": (
        "the refspec contains a wildcard",
        lambda facts: replace(facts, refspec="refs/heads/*:refs/heads/*"),
    ),
    "G33": (
        "the refspec deletes a remote branch",
        lambda facts: replace(facts, refspec=f"refs/heads/{AGENT_BRANCH}:"),
    ),
    "G34": (
        "the push command carries a force option",
        lambda facts: replace(facts, push_options=("--force",)),
    ),
    "G35": (
        "the push command carries an option T21 never uses",
        lambda facts: replace(facts, push_options=("--porcelain",)),
    ),
    "G36": (
        "the refspec maps a different source branch",
        lambda facts: replace(
            facts, refspec=build_refspec(OTHER_AGENT_BRANCH, AGENT_BRANCH)
        ),
    ),
    "G37": (
        "the push would add two commits instead of one",
        lambda facts: replace(facts, new_commit_count=2),
    ),
    "G38": (
        "the remote history is not an ancestor of the recorded commit",
        lambda facts: replace(facts, remote_commits_are_ancestors=False),
    ),
    "G39": (
        "HEAD moved between the two pre-push checkpoints",
        lambda facts: replace(
            facts, final_checkpoint=checkpoint(head_commit=OUT_OF_BAND_COMMIT)
        ),
    ),
    "G40": (
        "the branch changed between the two pre-push checkpoints",
        lambda facts: replace(
            facts, final_checkpoint=checkpoint(branch=OTHER_AGENT_BRANCH)
        ),
    ),
    "G41": (
        "a path changed between the two pre-push checkpoints",
        lambda facts: replace(
            facts,
            final_checkpoint=checkpoint(
                is_clean=False, changed_files=("src/app.py",)
            ),
        ),
    ),
    "G42": (
        "a pre-push gate fails, so the roll-up cannot authorise the push",
        lambda facts: replace(facts, index_locked=True),
    ),
    "G43": (
        "the single push attempt exited non-zero",
        lambda facts: replace(
            facts, push=push_observation(returncode=1, stderr="rejected")
        ),
    ),
    "G44": (
        "the remote could not be read back after the push",
        lambda facts: replace(
            facts, verification=PushVerificationStatus.UNAVAILABLE
        ),
    ),
    "G45": (
        "the remote branch points at another commit",
        lambda facts: replace(facts, remote_after_commit=OUT_OF_BAND_COMMIT),
    ),
    "G46": (
        "the remote branch already pointed at the recorded commit",
        lambda facts: replace(
            facts, remote_observation=remote_observation(branch_commit=COMMIT)
        ),
    ),
    "G47": (
        "HEAD changed during the push",
        lambda facts: replace(
            facts,
            post_push_checkpoint=checkpoint(head_commit=OUT_OF_BAND_COMMIT),
        ),
    ),
    "G48": (
        "the recorded command line invokes push twice",
        lambda facts: replace(
            facts,
            push=push_observation(
                argv=(
                    "git",
                    "-C",
                    REPO_ROOT,
                    PUSH_SUBCOMMAND,
                    PUSH_SUBCOMMAND,
                    REMOTE,
                    build_refspec(AGENT_BRANCH, AGENT_BRANCH),
                )
            ),
        ),
    ),
}


# ---------------------------------------------------------------------------
# The gate catalogue and phase reporting
# ---------------------------------------------------------------------------


class TestGateCatalogue:
    def test_the_48_gates_are_declared_in_order_and_without_gaps(self):
        ids = list(GATES)
        assert ids == [f"G{index}" for index in range(1, 49)]
        assert len(set(ids)) == 48

    def test_every_gate_has_a_short_title(self):
        for gate_id, title in GATES.items():
            assert title.strip(), gate_id

    def test_the_evaluator_table_matches_the_declared_gates(self):
        assert set(push_policy._GATE_EVALUATORS) == set(GATES)

    def test_every_gate_belongs_to_exactly_one_phase(self):
        assert set(push_policy._GATE_PHASE) == set(GATES)
        assert all(
            isinstance(phase, PushPhase)
            for phase in push_policy._GATE_PHASE.values()
        )

    def test_every_gate_has_an_adversarial_break_in_this_suite(self):
        assert set(GATE_BREAKS) == set(GATES)
        for gate_id, (description, break_it) in GATE_BREAKS.items():
            assert description.strip(), gate_id
            assert callable(break_it), gate_id

    def test_the_t20_status_constant_matches_the_commit_executor_enum(self):
        # T21 pins its authorising status as a literal so the pure policy module
        # never imports the executor; this test keeps the two in step.
        assert T20_COMMITTED_STATUS == git_commit.CommitStatus.COMMITTED.value

    def test_evaluation_requires_a_push_facts_record(self):
        for candidate in ({}, None, 5, "push-facts"):
            with pytest.raises(PushPolicyError):
                evaluate_push_gates(candidate)

    def test_the_two_boundaries_do_not_accept_each_others_facts(self):
        import commit_policy

        with pytest.raises(commit_policy.CommitPolicyError):
            commit_policy.evaluate_commit_gates(happy_facts())

    def test_the_mutating_subcommand_is_allowed_and_never_forbidden(self):
        assert PUSH_SUBCOMMAND == "push"
        import git_push

        assert PUSH_SUBCOMMAND in git_push.ALLOWED_GIT_SUBCOMMANDS
        assert PUSH_SUBCOMMAND not in git_push.FORBIDDEN_GIT_SUBCOMMANDS

    def test_every_gate_id_is_reported_even_when_its_phase_has_not_run(self):
        evaluation = evaluate(happy_facts(), phase=PushPhase.INPUTS)
        assert [gate.gate_id for gate in evaluation.gates] == list(GATES)


class TestPhaseReporting:
    def test_the_happy_path_passes_all_48_gates(self):
        evaluation = evaluate(happy_facts())
        assert evaluation.decision is PushDecision.PROCEED
        assert evaluation.is_refusal is False
        assert evaluation.failed_gates == ()
        assert evaluation.first_failure is None
        assert evaluation.refusal_reason is None
        assert len(evaluation.gates) == 48
        assert len(evaluation.passed_gates) == 48
        assert evaluation.not_reached_gates == ()

    def test_later_gates_are_not_reached_until_their_stage_has_run(self):
        evaluation = evaluate(happy_facts(), phase=PushPhase.INPUTS)
        assert evaluation.status_of("G1") is GateStatus.PASS
        for gate_id in ("G2", "G12", "G20", "G23", "G30", "G38", "G42", "G48"):
            assert evaluation.status_of(gate_id) is GateStatus.NOT_REACHED, gate_id
        not_reached = evaluation.not_reached_gates
        assert len(not_reached) == 47
        assert {gate.reason for gate in not_reached} == {NOT_REACHED_REASON}
        assert evaluation.decision is PushDecision.PROCEED

    def test_each_phase_evaluates_a_monotonic_prefix(self):
        counts = [
            len(evaluate(happy_facts(), phase=phase).passed_gates)
            for phase in PushPhase
        ]
        assert counts == [1, 5, 9, 10, 15, 18, 21, 26, 36, 38, 42, 48]
        assert counts == sorted(counts)

    def test_every_evaluated_failure_is_reported_not_just_the_first(self):
        base = happy_facts()
        facts = replace(
            base,
            t20_status="refused",
            t20_is_committed=False,
            staged_files=("src/app.py",),
            push=push_observation(returncode=1, stderr="rejected"),
        )
        evaluation = evaluate(facts)
        failed = {gate.gate_id for gate in evaluation.failed_gates}
        assert {"G2", "G20", "G42", "G43", "G48"} <= failed
        assert evaluation.decision is PushDecision.REFUSE
        assert evaluation.is_refusal is True
        assert evaluation.first_failure.gate_id == "G2"
        assert (evaluation.refusal_reason or "").startswith("G2 ")

    def test_the_evaluation_serialises_every_gate(self):
        payload = evaluate(happy_facts()).as_dict()
        assert payload["phase"] == "verify"
        assert payload["decision"] == "proceed"
        assert payload["first_failure"] is None
        assert payload["refusal_reason"] is None
        assert len(payload["gates"]) == 48
        assert payload["failed"] == []
        assert payload["not_reached"] == []

    def test_gate_lookup_helpers_are_safe_for_unknown_ids(self):
        evaluation = evaluate(happy_facts())
        assert evaluation.status_of("G99") is None
        assert evaluation.gate("G99") is None
        gate = evaluation.gate("G34")
        assert gate is not None
        assert gate.passed is True
        assert gate.failed is False
        assert gate.status is GateStatus.PASS
        assert gate.title == GATES["G34"]


# ---------------------------------------------------------------------------
# One broken fact, one refusal (fail closed)
# ---------------------------------------------------------------------------


class TestGateRefusals:
    @pytest.mark.parametrize("gate_id", GATE_IDS, ids=GATE_IDS)
    def test_a_single_broken_fact_makes_the_gate_refuse(self, gate_id):
        _description, break_it = GATE_BREAKS[gate_id]
        assert_gate_fails(break_it(happy_facts()), gate_id)

    def test_each_break_actually_changes_the_record(self):
        for gate_id, (_description, break_it) in GATE_BREAKS.items():
            assert break_it(happy_facts()) != happy_facts(), gate_id

    def test_an_unknown_fact_fails_closed(self):
        # Every field the executor has not observed yet stays None, and every
        # gate that reads it must refuse rather than assume a pass.
        facts = PushFacts(
            request=happy_facts().request,
            config=PushPolicyConfig(),
        )
        evaluation = evaluate(facts)
        assert evaluation.decision is PushDecision.REFUSE
        assert evaluation.status_of("G1") is GateStatus.FAIL
        assert evaluation.status_of("G2") is GateStatus.FAIL
        assert evaluation.status_of("G10") is GateStatus.FAIL
        assert evaluation.status_of("G23") is GateStatus.FAIL
        assert evaluation.status_of("G38") is GateStatus.FAIL
        assert evaluation.status_of("G43") is GateStatus.FAIL

    def test_the_policy_module_performs_no_io(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(push_policy))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {
            "__future__",
            "re",
            "dataclasses",
            "enum",
            "typing",
            "branch_naming",
            "commit_policy",
        }
        assert "git_push" not in imported

    def test_a_missing_expected_root_or_config_refuses(self):
        base = happy_facts()
        assert_gate_fails(replace(base, expected_root=""), "G1")
        assert_gate_fails(replace(base, config=None), "G1")

    def test_a_bare_commit_id_is_never_proof_on_its_own(self):
        # A caller that hands T21 only a SHA (no T20 result) cannot push: the
        # contract gates have nothing to read and fail closed.
        facts = replace(
            happy_facts(),
            t20_status=None,
            t20_is_committed=None,
            t20_new_head=None,
            t20_commit_sha=None,
        )
        evaluation = evaluate(facts)
        assert evaluation.decision is PushDecision.REFUSE
        for gate_id in ("G2", "G3", "G4"):
            assert evaluation.status_of(gate_id) is GateStatus.FAIL, gate_id

    def test_the_t20_result_object_is_only_read(self):
        view = {
            "status": "committed",
            "is_committed": True,
            "new_head": COMMIT,
            "previous_head": PREVIOUS_COMMIT,
            "commit_sha": COMMIT,
            "needs_attention": False,
            "repository": {
                "worktree_root": REPO_ROOT,
                "branch": AGENT_BRANCH,
                "is_valid": True,
            },
        }
        snapshot = {key: value for key, value in view.items()}
        request = replace(happy_facts().request, commit_result=view)
        evaluate(replace(happy_facts(), request=request))
        assert view == snapshot


# ---------------------------------------------------------------------------
# The cascade a refusal reports
# ---------------------------------------------------------------------------


class TestCascadeReporting:
    def test_a_staged_path_refuses_the_index_and_the_roll_up(self):
        facts = replace(happy_facts(), staged_files=("src/app.py",))
        assert failed_ids(facts) == ("G20", "G42")

    def test_a_late_failure_does_not_touch_the_pre_push_roll_up(self):
        facts = replace(
            happy_facts(), push=push_observation(returncode=1, stderr="rejected")
        )
        assert failed_ids(facts) == ("G43", "G48")

    def test_an_out_of_band_commit_is_refused_everywhere_it_is_read(self):
        facts = with_request(happy_facts(), expected_commit=OUT_OF_BAND_COMMIT)
        assert failed_ids(facts) == (
            "G4",
            "G16",
            "G18",
            "G39",
            "G42",
            "G45",
            "G46",
            "G47",
        )

    def test_the_roll_up_names_every_pre_push_gate_that_failed(self):
        facts = replace(
            happy_facts(),
            staged_files=("src/app.py",),
            operation_in_progress=("MERGE_HEAD",),
        )
        evaluation = evaluate(facts)
        reason = evaluation.gate("G42").reason
        assert "G20" in reason
        assert "G21" in reason


# ---------------------------------------------------------------------------
# The refspec, remote and identity primitives
# ---------------------------------------------------------------------------


class TestRefspecPrimitives:
    def test_the_built_refspec_is_always_fully_qualified_and_exact(self):
        refspec = build_refspec(AGENT_BRANCH, OTHER_AGENT_BRANCH)
        assert refspec == (
            f"refs/heads/{AGENT_BRANCH}:refs/heads/{OTHER_AGENT_BRANCH}"
        )
        assert refspec.count(":") == 1
        assert "+" not in refspec
        assert "*" not in refspec
        parts = parse_refspec(refspec)
        assert parts is not None
        assert parts.is_fully_qualified is True
        assert parts.force is False
        assert parts.is_wildcard is False
        assert parts.source == f"refs/heads/{AGENT_BRANCH}"
        assert parts.destination == f"refs/heads/{OTHER_AGENT_BRANCH}"

    def test_the_builder_refuses_to_build_a_hostile_refspec(self):
        for branch, remote_branch in (
            ("", AGENT_BRANCH),
            (AGENT_BRANCH, ""),
            (None, None),
            ("bad..branch", AGENT_BRANCH),
            (AGENT_BRANCH, "bad..branch"),
            ("*", AGENT_BRANCH),
        ):
            assert build_refspec(branch, remote_branch) == "", (branch, remote_branch)

    def test_a_force_token_is_reported_not_hidden(self):
        parts = parse_refspec(f"+refs/heads/{AGENT_BRANCH}:refs/heads/{AGENT_BRANCH}")
        assert parts is not None
        assert parts.force is True

    def test_a_wildcard_is_reported_not_hidden(self):
        parts = parse_refspec("refs/heads/*:refs/heads/*")
        assert parts is not None
        assert parts.is_wildcard is True

    def test_malformed_and_hostile_refspecs_are_rejected(self):
        for refspec in (
            None,
            "",
            "   ",
            f"refs/heads/{AGENT_BRANCH}",
            f"refs/heads/{AGENT_BRANCH}:",
            f":refs/heads/{AGENT_BRANCH}",
            "refs/heads/a:refs/heads/b:refs/heads/c",
            "HEAD",
        ):
            assert parse_refspec(refspec) is None, refspec

    def test_a_deletion_and_a_single_ref_are_not_exact_mappings(self):
        single = parse_refspec(f"refs/heads/{AGENT_BRANCH}")
        assert single is None
        deletion = parse_refspec(f"refs/heads/{AGENT_BRANCH}:")
        assert deletion is None

    def test_a_short_refspec_is_parsed_but_not_fully_qualified(self):
        parts = parse_refspec(f"{AGENT_BRANCH}:{OTHER_AGENT_BRANCH}")
        assert parts is not None
        assert parts.is_fully_qualified is False


class TestRemotePrimitives:
    def test_valid_remote_names_are_accepted(self):
        for name in ("origin", "upstream", "remote-1", "a.b_c-d", "x"):
            assert validate_remote_name(name) is None, name

    def test_hostile_remote_names_are_rejected(self):
        for name in (
            None,
            "",
            " origin",
            "origin ",
            "ori gin",
            "origin/x",
            "-origin",
            "a" * 65,
        ):
            problem = validate_remote_name(name)
            assert problem, name

    def test_supported_remote_urls_are_accepted(self):
        for url in (
            REMOTE_URL,
            "https://github.com/acme/demo.git",
            "http://localhost:3000/acme/demo.git",
            "ssh://git@host:2222/acme/demo.git",
            "git@github.com:acme/demo.git",
            "git://host/acme/demo.git",
            "file:///C:/remotes/demo.git",
            "C:/remotes/demo.git",
            "/srv/git/demo.git",
            "./local-remote.git",
            "../local-remote.git",
        ):
            assert validate_remote_url(url) is None, url

    def test_hostile_remote_urls_are_rejected(self):
        for url in (
            None,
            "",
            " https://host/x.git",
            "https://host/x.git ",
            "ftp://host/x.git",
            "https://",
            "not a url",
            "relative/path",
            "a" * 2049,
        ):
            problem = validate_remote_url(url)
            assert problem, url

    def test_userinfo_credentials_are_detected(self):
        assert url_has_userinfo("https://user:secret@host/x.git") is True
        assert url_has_userinfo("https://token@host/x.git") is False
        assert url_has_userinfo("git@host:acme/demo.git") is False
        assert url_has_userinfo("file:///C:/remotes/x.git") is False
        assert url_has_userinfo(None) is False
        assert url_has_userinfo(REMOTE_URL) is False


# ---------------------------------------------------------------------------
# Identity, request and configuration
# ---------------------------------------------------------------------------


class TestIdentityPrimitives:
    def test_only_a_full_commit_id_is_accepted(self):
        assert is_full_commit_id(COMMIT) is True
        assert is_full_commit_id("A" * 40) is True
        assert is_full_commit_id("f" * 64) is True
        for value in (None, "", "abc", COMMIT[:39], COMMIT + "a", "z" * 40, "HEAD"):
            assert is_full_commit_id(value) is False, value

    def test_commit_ids_are_compared_case_folded(self):
        assert normalize_commit_id(None) == ""
        assert normalize_commit_id("  ABC  ") == "abc"
        assert normalize_commit_id(COMMIT.upper()) == COMMIT

    def test_paths_compare_across_separators_and_case(self):
        assert same_path(REPO_ROOT, "c:/WORK/demo") is True
        assert same_path(REPO_ROOT, "C:\\work\\demo\\") is True
        assert same_path(REPO_ROOT, "C:/work/other") is False
        assert same_path(None, REPO_ROOT) is False
        assert same_path(REPO_ROOT, None) is False


class TestPushRequestValidation:
    def test_a_complete_request_has_no_problems(self):
        request = happy_facts().request
        assert request.problems() == ()
        assert request.refspec == build_refspec(AGENT_BRANCH, AGENT_BRANCH)

    @pytest.mark.parametrize(
        "overrides, expected",
        [
            ({"repository_path": ""}, "the repository path is required"),
            (
                {"expected_commit": ""},
                "the expected commit must be a full commit id",
            ),
            (
                {"expected_commit": "abc"},
                "the expected commit must be a full commit id",
            ),
            ({"branch": ""}, "the source branch is required"),
            (
                {"branch": "bad..branch"},
                "the source branch is not a valid Git branch name",
            ),
            ({"remote": ""}, "the remote name is required"),
            ({"remote": "ori gin"}, "the remote name is not a plain remote name"),
            ({"remote_branch": ""}, "the destination branch is required"),
            (
                {"remote_branch": "bad..branch"},
                "the destination branch is not a valid Git branch name",
            ),
        ],
    )
    def test_an_incomplete_request_names_what_is_missing(self, overrides, expected):
        request = replace(happy_facts().request, **overrides)
        assert expected in request.problems()

    def test_the_serialised_request_carries_no_t20_object(self):
        request = replace(happy_facts().request, commit_result=object())
        payload = request.as_dict()
        assert "commit_result" not in payload
        assert payload["remote"] == REMOTE
        assert payload["expected_commit"] == COMMIT

    def test_a_request_that_yields_no_refspec_cannot_be_pushed(self):
        request = replace(happy_facts().request, remote_branch="")
        assert request.refspec == ""
        assert_gate_fails(replace(happy_facts(), request=request), "G1")


class TestPolicyConfiguration:
    def test_the_defaults_are_the_conservative_choice(self):
        config = PushPolicyConfig()
        assert config.protected_branches == PROTECTED_BRANCH_NAMES
        assert config.protected_branches == ("main", "master", "develop", "trunk")
        assert config.default_branch is None
        assert (
            config.required_branch_prefix
            == push_policy.DEFAULT_AGENT_BRANCH_PREFIX
        )
        assert config.require_clean_worktree is True
        assert config.allow_new_remote_branch is True

    def test_every_protected_branch_is_refused_without_a_default(self):
        config = PushPolicyConfig()
        for branch in PROTECTED_BRANCH_NAMES:
            assert config.is_protected_branch(branch) is True
            assert config.is_protected_branch(branch.upper()) is True
        assert config.is_protected_branch(None) is True
        assert config.is_protected_branch("") is True
        assert config.is_protected_branch(AGENT_BRANCH) is False

    def test_an_explicit_default_branch_is_refused_too(self):
        config = PushPolicyConfig(default_branch="release")
        assert config.is_protected_branch("release") is True
        assert config.is_protected_branch("RELEASE") is True
        assert config.is_protected_branch(AGENT_BRANCH) is False

    def test_only_the_agent_namespace_is_pushable(self):
        config = PushPolicyConfig()
        assert config.is_agent_branch(AGENT_BRANCH) is True
        assert config.is_agent_branch("ai/sonar-fix") is False
        assert config.is_agent_branch("ai/sonar-fixX/AX1") is False
        assert config.is_agent_branch("main") is False
        assert config.is_agent_branch(None) is False
        renamed = PushPolicyConfig(required_branch_prefix="ai/agents")
        assert renamed.is_agent_branch("ai/agents/AX1") is True

    def test_a_configuration_can_relax_the_clean_worktree_requirement(self):
        base = happy_facts()
        facts = replace(
            base,
            config=replace(base.config, require_clean_worktree=False),
            worktree_clean=False,
        )
        evaluation = evaluate(facts)
        assert evaluation.status_of("G19") is GateStatus.PASS
        assert evaluation.decision is PushDecision.PROCEED

    def test_a_new_remote_branch_may_carry_the_whole_history(self):
        facts = replace(
            happy_facts(),
            remote_observation=remote_observation(
                branch_present=False, branch_commit=None
            ),
            new_remote_branch=True,
            new_commit_count=3,
        )
        evaluation = evaluate(facts)
        assert evaluation.status_of("G37") is GateStatus.PASS
        assert evaluation.status_of("G38") is GateStatus.PASS
        assert evaluation.status_of("G46") is GateStatus.PASS
        assert evaluation.decision is PushDecision.PROCEED

    def test_the_hostile_push_options_are_named(self):
        for option in (
            "--force",
            "-f",
            "--mirror",
            "--tags",
            "--delete",
            "-d",
            "--all",
            "--follow-tags",
            "--force-with-lease",
            "--prune",
        ):
            assert option in BROADENING_PUSH_OPTIONS, option
