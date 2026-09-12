"""T20 unit tests: the pure commit-gate policy (no Git, no I/O, no subprocess).

Every test builds an immutable :class:`CommitFacts` record from the real T01-T19
records (``tests/t20_fixtures.py``) plus synthetic Git observations, then flips
exactly one fact so a single gate must refuse. The happy-path record proves the
other direction: all 45 gates pass.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from commit_message import CommitMessage, build_commit_message
from analysis_correlation import AnalysisCorrelation
from codex_executor import CodexExecutionStatus
from commit_policy import (
    ACCOUNTED_HOOK_NAMES,
    DEFAULT_AGENT_BRANCH_PREFIX,
    GATES,
    NOT_REACHED_REASON,
    OPERATION_IN_PROGRESS_MARKERS,
    PROTECTED_BRANCH_NAMES,
    UNSAFE_GIT_ENVIRONMENT_VARIABLES,
    ApprovedFileSet,
    CommitDecision,
    CommitFacts,
    CommitObservation,
    CommitPhase,
    CommitPolicyConfig,
    CommitPolicyError,
    GateStatus,
    GitEnvironmentReport,
    GitIdentityReport,
    HookReport,
    RepositoryIdentity,
    StageObservation,
    evaluate_commit_gates,
    normalise_approved_paths,
    resolve_approved_files,
)
from secret_scan import SecretScanResult
from sonar_analysis import SonarAnalysisStatus
from sonar_analysis_waiter import SonarAnalysisState
from test_runner import TestStatus
from worktree_baseline import BaselineAttribution, WorktreeSnapshot

from t20_fixtures import (
    TARGET,
    assert_fixed,
    green_status,
    make_analysis,
    make_codex,
    make_scope,
    make_tests,
    make_trigger,
    make_verification,
)

AGENT_BRANCH = "ai/sonar-fix/AX1"
ISSUE_KEY = "AX1"
RULE = "python:S1481"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
BLOB = "c" * 40
OTHER_BLOB = "d" * 40
REPO = Path(tempfile.gettempdir()) / "t20-policy-repo"
IDENT = ("Test User", "test@example.com")


def clean_scan(reason: str = "no credential-shaped content") -> SecretScanResult:
    """A completed, clean secret scan of nothing suspicious."""
    return SecretScanResult(
        scanned=True, clean=True, findings=(), reason=reason, secret_count=0
    )


def snapshot(
    *,
    head: str = HEAD,
    clean: bool = False,
    changed=(),
    content_ids=None,
    ignored=(),
    ignored_scan: bool = True,
    staged=(),
) -> WorktreeSnapshot:
    """A synthetic snapshot for the gates (the inspector is tested elsewhere)."""
    changed = tuple(changed)
    return WorktreeSnapshot(
        repository_path=REPO,
        head_commit=head,
        is_clean=clean,
        changed_files=changed,
        tracked_modified_files=changed,
        staged_files=tuple(staged),
        untracked_files=(),
        deleted_files=(),
        renamed_files=(),
        fingerprints={path: f"worktree:{path}" for path in changed},
        index_equivalent_blobs=dict(content_ids or {}),
        ignored_files=tuple(ignored),
        ignored_scan=ignored_scan,
    )



def passing_facts(**overrides) -> CommitFacts:
    """A record on which every one of the 45 gates passes.

    The T16 evidence is mandatory for G11, so the happy-path record carries it,
    exactly as the executor fills it from a real T19 ``IssueStatusResult``.
    """
    status = green_status()
    assert_fixed(status)
    message = build_commit_message(
        rule=RULE, file_path=TARGET, issue_key=ISSUE_KEY
    )
    baseline = snapshot(clean=True)
    after = snapshot(changed=(TARGET,), content_ids={TARGET: BLOB})
    attribution = BaselineAttribution(
        repository_path=REPO,
        clean_baseline=True,
        agent_files=(TARGET,),
        agent_untracked_files=(),
        agent_deleted_files=(),
        agent_renamed_files=(),
        pre_existing_files=(),
        pre_existing_and_changed_files=(),
    )
    stage = StageObservation(
        attempted=True,
        approved_paths=(TARGET,),
        staged_paths=(TARGET,),
        staged_blob_ids={TARGET: BLOB},
        worktree_content_ids={TARGET: BLOB},
        approved_content_ids={TARGET: BLOB},
        head_before=HEAD,
        head_after=HEAD,
    )
    facts = CommitFacts(
        issue_status=status,
        codex=status.codex,
        tests=status.tests,
        analysis=status.analysis,
        trigger=make_trigger(),
        verification=status.verification,
        scope=status.scope,
        attribution=attribution,
        baseline=baseline,
        after=after,
        issue_key=ISSUE_KEY,
        rule=RULE,
        target_file=TARGET,
        config=CommitPolicyConfig(),
        approved=ApprovedFileSet(
            paths=(TARGET,), expected=(TARGET,), agent_files=(TARGET,)
        ),
        content_scan=clean_scan(),
        worktree_diff_scan=clean_scan(),
        staged_diff_scan=clean_scan(),
        repository=RepositoryIdentity(
            expected_root=str(REPO),
            worktree_root=str(REPO),
            git_dir=str(REPO / ".git"),
            common_dir=str(REPO / ".git"),
            index_path=str(REPO / ".git" / "index"),
            head_commit=HEAD,
            branch=AGENT_BRANCH,
            detached=False,
            matches_expected=True,
        ),
        environment=GitEnvironmentReport(
            checked_variables=UNSAFE_GIT_ENVIRONMENT_VARIABLES, reason="safe"
        ),
        identity=GitIdentityReport(
            name=IDENT[0],
            email=IDENT[1],
            actor_name=IDENT[0],
            actor_email=IDENT[1],
            committer_name=IDENT[0],
            committer_email=IDENT[1],
            explicit=True,
        ),
        hooks=HookReport(
            inspected_hooks=ACCOUNTED_HOOK_NAMES, reason="no active hook"
        ),
        stage=stage,
        pre_commit_stage=stage,
        commit_message=message,
        commit=CommitObservation(
            attempted=True,
            committed=True,
            previous_head=HEAD,
            expected_previous_head=HEAD,
            new_head=NEW_HEAD,
            parent_head=HEAD,
            new_commit_count=1,
            message=message.text,
            expected_message=message.text,
            author_name=IDENT[0],
            author_email=IDENT[1],
            committer_name=IDENT[0],
            committer_email=IDENT[1],
            expected_identity_name=IDENT[0],
            expected_identity_email=IDENT[1],
            committed_paths=(TARGET,),
            expected_paths=(TARGET,),
            committed_blob_ids={TARGET: BLOB},
            expected_blob_ids={TARGET: BLOB},
            branch_after=AGENT_BRANCH,
            expected_branch=AGENT_BRANCH,
            worktree_root_after=str(REPO),
            expected_worktree_root=str(REPO),
        ),
    )
    return replace(facts, **overrides) if overrides else facts


def evaluate(facts: CommitFacts, phase: CommitPhase = CommitPhase.VERIFY):
    """Evaluate every gate up to ``phase``."""
    return evaluate_commit_gates(facts, phase=phase)


def status_of(facts: CommitFacts, gate_id: str) -> GateStatus:
    """The status of one gate at the final (VERIFY) phase."""
    return evaluate(facts).status_of(gate_id)


# ---------------------------------------------------------------------------
# The gate catalogue and phase reporting
# ---------------------------------------------------------------------------


class TestGateCatalogue:
    def test_the_45_gates_are_declared_in_order_and_without_gaps(self):
        ids = [gate_id for gate_id, _title in GATES]
        assert ids == [f"G{index}" for index in range(1, 46)]
        assert len(set(ids)) == 45

    def test_every_gate_has_a_short_title(self):
        for gate_id, title in GATES:
            assert title.strip(), gate_id

    def test_the_evaluator_table_matches_the_declared_gates(self):
        import commit_policy

        assert set(commit_policy._GATE_EVALUATORS) == {
            gate_id for gate_id, _title in GATES
        }

    def test_every_gate_belongs_to_exactly_one_phase(self):
        import commit_policy

        assert set(commit_policy._GATE_PHASE) == {
            gate_id for gate_id, _title in GATES
        }
        assert all(
            isinstance(phase, CommitPhase)
            for phase in commit_policy._GATE_PHASE.values()
        )

    def test_evaluation_requires_a_facts_record(self):
        for candidate in ({}, None, 5, "commit-facts"):
            with pytest.raises(CommitPolicyError):
                evaluate_commit_gates(candidate)


class TestPhaseReporting:
    def test_the_happy_path_passes_all_45_gates(self):
        evaluation = evaluate(passing_facts())
        assert evaluation.decision is CommitDecision.PROCEED
        assert evaluation.is_refusal is False
        assert evaluation.failed_gates == ()
        assert evaluation.first_failure is None
        assert evaluation.refusal_reason is None
        assert len(evaluation.gates) == 45
        assert len(evaluation.passed_gates) == 45
        assert evaluation.not_reached_gates == ()

    def test_later_gates_are_not_reached_until_their_stage_has_run(self):
        evaluation = evaluate(passing_facts(), phase=CommitPhase.INPUTS)
        assert evaluation.status_of("G1") is GateStatus.PASS
        for gate_id in ("G2", "G19", "G29", "G38", "G42", "G44"):
            assert evaluation.status_of(gate_id) is GateStatus.NOT_REACHED
        not_reached = evaluation.not_reached_gates
        assert len(not_reached) == 44
        assert {gate.reason for gate in not_reached} == {NOT_REACHED_REASON}
        assert evaluation.decision is CommitDecision.PROCEED

    def test_each_phase_evaluates_a_monotonic_prefix(self):
        counts = [
            len(evaluate(passing_facts(), phase=phase).passed_gates)
            for phase in CommitPhase
        ]
        assert counts == sorted(counts)
        assert counts[0] == 1
        assert counts[-1] == 45

    def test_every_evaluated_failure_is_reported_not_just_the_first(self):
        base = passing_facts()
        facts = replace(
            base,
            repository=replace(base.repository, branch="main", detached=True),
            hooks=HookReport(active_hooks=("commit-msg",)),
        )
        evaluation = evaluate(facts)
        failed = {gate.gate_id for gate in evaluation.failed_gates}
        assert {"G31", "G32", "G36"} <= failed
        assert evaluation.decision is CommitDecision.REFUSE
        assert evaluation.is_refusal is True
        assert evaluation.first_failure.gate_id == "G29"
        assert "detached" in (evaluation.refusal_reason or "")

    def test_the_evaluation_serialises_every_gate(self):
        payload = evaluate(passing_facts()).as_dict()
        assert payload["phase"] == "verify"
        assert payload["decision"] == "proceed"
        assert len(payload["gates"]) == 45
        assert payload["failed"] == []
        assert payload["not_reached"] == []
        assert payload["refusal_reason"] is None

    def test_gate_lookup_helpers_are_safe_for_unknown_ids(self):
        evaluation = evaluate(passing_facts())
        assert evaluation.status_of("G99") is None
        assert evaluation.gate("G99") is None
        gate = evaluation.gate("G32")
        assert gate is not None
        assert gate.passed is True
        assert gate.failed is False
        assert gate.status is GateStatus.PASS
        assert gate.title == "current branch is not protected/default/reserved"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

#: Gates that must refuse when the fact they need was never observed.
FAIL_CLOSED_GATES = (
    "G1", "G2", "G4", "G7", "G8", "G9", "G10", "G11", "G12", "G13", "G14",
    "G15", "G16", "G17", "G18", "G19", "G20", "G21", "G22", "G23", "G24",
    "G26", "G27", "G28", "G29", "G30", "G31", "G32", "G33", "G34", "G36",
    "G37", "G38", "G39", "G40", "G41", "G42", "G43", "G44",
)

#: Gates that assert the *absence* of something, so an empty record cannot
#: contradict them. They never stand alone: G19/G20/G29/G38/G43 refuse on the
#: same record, so the overall decision is still ``REFUSE``.
NOT_OBSERVED_GATES = ("G3", "G5", "G6", "G25", "G35", "G45")


def assert_gate_fails(facts: CommitFacts, gate_id: str, needle: str = ""):
    """Assert one gate refused (and the overall decision refuses too)."""
    evaluation = evaluate(facts)
    gate = evaluation.gate(gate_id)
    assert gate is not None, gate_id
    assert gate.status is GateStatus.FAIL, f"{gate_id}: {gate.reason}"
    assert evaluation.decision is CommitDecision.REFUSE
    assert evaluation.is_refusal is True
    if needle:
        assert needle.casefold() in gate.reason.casefold(), gate.reason
    return evaluation


class TestFailClosed:
    @pytest.mark.parametrize("gate_id", FAIL_CLOSED_GATES)
    def test_a_missing_fact_fails_closed(self, gate_id):
        assert status_of(CommitFacts(), gate_id) is GateStatus.FAIL

    @pytest.mark.parametrize("gate_id", NOT_OBSERVED_GATES)
    def test_absence_only_gates_do_not_fail_on_an_empty_record(self, gate_id):
        assert status_of(CommitFacts(), gate_id) is GateStatus.PASS
        assert evaluate(CommitFacts()).decision is CommitDecision.REFUSE

    def test_no_input_means_refusal_not_a_partial_proceed(self):
        evaluation = evaluate(CommitFacts())
        assert evaluation.first_failure.gate_id == "G1"
        assert len(evaluation.failed_gates) >= 30




# ---------------------------------------------------------------------------
# G1-G18: inputs, T19 evidence, baseline and attribution
# ---------------------------------------------------------------------------


class TestInputAndEvidenceGates:
    def test_g1_missing_identity_inputs_are_refused(self):
        assert_gate_fails(passing_facts(target_file=""), "G1", "target_file")
        assert_gate_fails(passing_facts(rule="  "), "G1", "rule")
        assert_gate_fails(passing_facts(issue_key=""), "G1", "issue_key")

    def test_g2_a_non_fixed_t19_status_is_refused(self):
        status = green_status(verification=make_verification(present=True))
        assert status.status.value != "fixed"
        facts = replace(
            passing_facts(),
            issue_status=status,
            verification=status.verification,
        )
        assert_gate_fails(facts, "G2", "not 'fixed'")

    def test_g3_a_review_required_status_is_refused(self):
        status = green_status(verification=make_verification(retrieved=False))
        assert status.status.value == "review-required"
        facts = replace(
            passing_facts(),
            issue_status=status,
            verification=status.verification,
        )
        assert_gate_fails(facts, "G3", "review")

    def test_g4_a_failed_codex_execution_is_refused(self):
        assert_gate_fails(
            passing_facts(codex=make_codex(status=CodexExecutionStatus.FAILED)),
            "G4",
            "did not succeed",
        )

    def test_g5_codex_uncertainty_is_refused(self):
        assert_gate_fails(
            passing_facts(codex=make_codex(uncertain=True)),
            "G5",
            "uncertain",
        )

    def test_g6_a_codex_review_request_is_refused(self):
        facts = passing_facts(
            codex=make_codex(stdout="I could not decide; please review this change")
        )
        assert_gate_fails(facts, "G6", "human review")

    def test_g7_an_invalid_change_scope_is_refused(self):
        assert_gate_fails(
            passing_facts(scope=make_scope(valid=False)), "G7", "scope"
        )

    def test_g8_an_unchanged_target_file_is_refused(self):
        assert_gate_fails(
            passing_facts(scope=make_scope(modified=False)),
            "G8",
            "did not change",
        )

    def test_g9_failed_project_tests_are_refused(self):
        assert_gate_fails(
            passing_facts(tests=make_tests(TestStatus.FAILED)),
            "G9",
            "tests",
        )

    def test_g10_a_failed_analysis_is_refused(self):
        assert_gate_fails(
            passing_facts(analysis=make_analysis(SonarAnalysisState.FAILED)),
            "G10",
            "analysis",
        )

    def test_g11_a_missing_or_mismatched_task_id_is_refused(self):
        assert_gate_fails(
            passing_facts(analysis=make_analysis(task_id=None)),
            "G11",
            "task id",
        )
        assert_gate_fails(
            passing_facts(
                analysis=make_analysis(task_id="AY1"),
                trigger=make_trigger(task_id="AY2"),
            ),
            "G11",
            "does not match",
        )

    def test_g11_missing_t16_evidence_fails_closed(self):
        """The cross-check is never skipped: no T16 evidence means no commit."""
        evaluation = assert_gate_fails(
            passing_facts(trigger=None),
            "G11",
            "evidence is missing",
        )
        assert evaluation.gate("G11").status is GateStatus.FAIL

    def test_g11_a_trigger_without_a_task_id_fails_closed(self):
        assert_gate_fails(
            passing_facts(
                analysis=make_analysis(task_id="AY1"),
                trigger=make_trigger(task_id=None),
            ),
            "G11",
            "carries no compute-engine task id",
        )

    def test_g11_a_contradictory_trigger_fails_closed(self):
        """A trigger that did not succeed can never vouch for a completion."""
        assert_gate_fails(
            passing_facts(
                trigger=make_trigger(SonarAnalysisStatus.FAILED, task_id="AY1")
            ),
            "G11",
            "did not report a successful trigger",
        )

    def test_g12_an_uncorrelated_snapshot_is_refused(self):
        facts = passing_facts(
            verification=make_verification(
                correlation=AnalysisCorrelation.NOT_CORRELATED
            )
        )
        assert_gate_fails(facts, "G12", "correlated")


    def test_g13_an_unreliable_absence_is_refused(self):
        assert_gate_fails(
            passing_facts(verification=make_verification(reliable=False)),
            "G13",
            "not reliable",
        )

    def test_g14_a_still_open_issue_is_refused(self):
        assert_gate_fails(
            passing_facts(verification=make_verification(present=True)),
            "G14",
            "still open",
        )

    def test_g15_a_run_that_is_not_attributable_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            attribution=replace(
                base.attribution, blocked_reasons=("pre-existing change",)
            ),
        )
        assert_gate_fails(facts, "G15", "attributed")

    def test_g16_a_dirty_baseline_is_refused(self):
        assert_gate_fails(
            passing_facts(
                baseline=snapshot(clean=False, changed=("src/other.py",))
            ),
            "G16",
            "not usable",
        )

    def test_g16_a_pre_populated_index_is_refused(self):
        assert_gate_fails(
            passing_facts(baseline=snapshot(clean=True, staged=("src/other.py",))),
            "G16",
            "staged",
        )

    def test_g16_an_uncaptured_ignored_set_is_refused(self):
        assert_gate_fails(
            passing_facts(baseline=snapshot(clean=True, ignored_scan=False)),
            "G16",
            "ignored",
        )

    def test_g17_a_pre_existing_target_modification_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            attribution=replace(base.attribution, pre_existing_files=(TARGET,)),
        )
        assert_gate_fails(facts, "G17", "already changed")

    def test_g18_an_unexplained_change_is_refused(self):
        facts = passing_facts(
            after=snapshot(
                changed=(TARGET, "src/new.py"), content_ids={TARGET: BLOB}
            )
        )
        assert_gate_fails(facts, "G18", "unexplained")

    def test_g18_an_ignored_file_created_by_the_run_is_refused(self):
        facts = passing_facts(
            after=snapshot(
                changed=(TARGET,),
                content_ids={TARGET: BLOB},
                ignored=("build/agent.log",),
            )
        )
        assert_gate_fails(facts, "G18", "ignored")



# ---------------------------------------------------------------------------
# G19-G28: the approved set, path safety and secret scans
# ---------------------------------------------------------------------------


class TestApprovedSetGates:
    def test_g19_an_empty_approved_set_is_refused(self):
        facts = passing_facts(
            approved=ApprovedFileSet(paths=(), expected=(TARGET,))
        )
        assert_gate_fails(facts, "G19", "empty")

    def test_g20_a_path_outside_the_allowed_scope_is_refused(self):
        facts = passing_facts(
            approved=ApprovedFileSet(
                paths=("src/other.py",),
                expected=(TARGET,),
                agent_files=("src/other.py",),
                outside_scope=("src/other.py",),
            )
        )
        assert_gate_fails(facts, "G20", "outside the allowed scope")

    def test_g20_a_set_the_run_did_not_change_is_refused(self):
        facts = passing_facts(
            approved=ApprovedFileSet(paths=(TARGET,), expected=(TARGET,))
        )
        assert_gate_fails(facts, "G20", "exactly the paths the run changed")

    def test_g21_a_deleted_file_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            attribution=replace(
                base.attribution, agent_deleted_files=(TARGET,)
            ),
        )
        assert_gate_fails(facts, "G21", "deleted")

    def test_g21_a_target_deleted_in_the_worktree_is_refused(self):
        facts = passing_facts(
            after=replace(
                snapshot(changed=(TARGET,), content_ids={TARGET: BLOB}),
                deleted_files=(TARGET,),
            )
        )
        assert_gate_fails(facts, "G21", "deleted")

    def test_g22_a_rename_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            attribution=replace(
                base.attribution, agent_renamed_files=((TARGET, "src/old.py"),)
            ),
        )
        assert_gate_fails(facts, "G22", "renamed")

    def test_g22_a_target_involved_in_a_rename_is_refused(self):
        facts = passing_facts(
            after=replace(
                snapshot(changed=(TARGET,), content_ids={TARGET: BLOB}),
                renamed_files=((TARGET, "src/old.py"),),
            )
        )
        assert_gate_fails(facts, "G22", "rename")

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../outside.py",
            "src/../../outside.py",
            "src/*.py",
            "src/app?.py",
            "src/[ab].py",
            "src/!app.py",
            "",
        ],
    )
    def test_g23_hostile_or_non_literal_paths_are_refused(self, path):
        facts = passing_facts(
            approved=ApprovedFileSet(
                paths=(path,), expected=(path,), agent_files=(path,)
            )
        )
        assert_gate_fails(facts, "G23")

    def test_g24_an_ignored_approved_path_is_refused(self):
        facts = passing_facts(
            after=snapshot(
                changed=(TARGET,),
                content_ids={TARGET: BLOB},
                ignored=("build/agent.log",),
            ),
            approved=ApprovedFileSet(
                paths=("build/agent.log",),
                expected=("build/agent.log",),
                agent_files=("build/agent.log",),
            ),
        )
        assert_gate_fails(facts, "G24", "ignored")

    def test_g24_an_uncaptured_ignored_set_is_refused(self):
        facts = passing_facts(
            after=snapshot(
                changed=(TARGET,), content_ids={TARGET: BLOB}, ignored_scan=False
            )
        )
        assert_gate_fails(facts, "G24", "not captured")

    def test_g24_the_refusal_can_only_be_disabled_explicitly(self):
        facts = passing_facts(
            after=snapshot(
                changed=(TARGET,),
                content_ids={TARGET: BLOB},
                ignored=("build/agent.log",),
            ),
            config=CommitPolicyConfig(allow_ignored_approved_paths=True),
        )
        assert status_of(facts, "G24") is GateStatus.PASS


    def test_g25_a_binary_approved_file_is_refused(self):
        assert_gate_fails(passing_facts(binary_paths=(TARGET,)), "G25", "binary")

    def test_g26_a_secret_in_the_approved_content_is_refused(self):
        facts = passing_facts(
            content_scan=SecretScanResult(
                scanned=True,
                clean=False,
                findings=(),
                reason="1 credential-shaped finding in the approved content.",
                secret_count=1,
            )
        )
        assert_gate_fails(facts, "G26", "secret scan")

    def test_g26_a_scan_that_could_not_run_is_refused(self):
        facts = passing_facts(
            content_scan=SecretScanResult(
                scanned=False,
                clean=False,
                findings=(),
                reason="'src/app.py' could not be read (denied).",
                secret_count=0,
            )
        )
        assert_gate_fails(facts, "G26", "could not be")

    def test_g27_a_secret_in_the_worktree_diff_is_refused(self):
        facts = passing_facts(
            worktree_diff_scan=SecretScanResult(
                scanned=True,
                clean=False,
                findings=(),
                reason="1 credential-shaped finding in the worktree diff.",
                secret_count=1,
            )
        )
        assert_gate_fails(facts, "G27", "worktree diff")

    def test_g28_a_secret_in_the_staged_diff_is_refused(self):
        facts = passing_facts(
            staged_diff_scan=SecretScanResult(
                scanned=True,
                clean=False,
                findings=(),
                reason="1 credential-shaped finding in the staged diff.",
                secret_count=1,
            )
        )
        assert_gate_fails(facts, "G28", "staged diff")



# ---------------------------------------------------------------------------
# G29-G37: repository, environment, branch, identity, state, hooks, HEAD
# ---------------------------------------------------------------------------


class TestRepositoryGates:
    def test_g29_a_redirected_or_unresolved_repository_is_refused(self):
        base = passing_facts()
        for repository in (
            None,
            replace(base.repository, matches_expected=False),
            replace(base.repository, worktree_root=None),
            replace(base.repository, git_dir=None),
            replace(base.repository, common_dir=None),
            replace(base.repository, index_path=None),
            replace(base.repository, head_commit=None),
            replace(base.repository, problems=("boom",)),
        ):
            assert_gate_fails(replace(base, repository=repository), "G29")

    def test_g30_a_redirecting_environment_variable_is_refused(self):
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_NAMESPACE",
            "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_SSH_COMMAND",
            "GIT_EXTERNAL_DIFF",
            "GIT_AUTHOR_NAME",
            "GIT_COMMITTER_DATE",
        ):
            facts = passing_facts(
                environment=GitEnvironmentReport(
                    unsafe_variables=(name,),
                    checked_variables=UNSAFE_GIT_ENVIRONMENT_VARIABLES,
                    reason=f"{name} is set.",
                )
            )
            assert_gate_fails(facts, "G30", name)

    def test_g30_a_helper_variable_a_shell_or_ide_sets_is_not_refused(self):
        # GIT_ASKPASS/GIT_EDITOR/GIT_PAGER cannot redirect this commit: T20
        # always passes -m, never prompts and never fetches. Refusing them would
        # only produce false refusals on a normal developer machine.
        facts = passing_facts(
            environment=GitEnvironmentReport(
                unsafe_variables=(),
                checked_variables=UNSAFE_GIT_ENVIRONMENT_VARIABLES
                + ("GIT_ASKPASS", "GIT_EDITOR", "GIT_PAGER", "GIT_SEQUENCE_EDITOR"),
                reason="none of the redirecting variables is set.",
            )
        )
        assert status_of(facts, "G30") is GateStatus.PASS

    def test_g31_a_detached_head_is_refused(self):
        base = passing_facts()
        facts = replace(base, repository=replace(base.repository, detached=True))
        assert_gate_fails(facts, "G31", "detached")

    @pytest.mark.parametrize(
        "branch", list(PROTECTED_BRANCH_NAMES) + ["Main", "release/1.0"]
    )
    def test_g32_a_protected_or_non_agent_branch_is_refused(self, branch):
        base = passing_facts()
        facts = replace(base, repository=replace(base.repository, branch=branch))
        assert_gate_fails(facts, "G32")

    def test_g32_the_default_branch_is_refused_even_when_renamed(self):
        base = passing_facts()
        facts = replace(
            base,
            repository=replace(base.repository, branch="deploy/prod"),
            config=CommitPolicyConfig(default_branch="deploy/prod"),
        )
        assert_gate_fails(facts, "G32", "protected")

    def test_g32_an_agent_branch_is_allowed(self):
        assert status_of(passing_facts(), "G32") is GateStatus.PASS

    def test_g33_a_missing_identity_is_refused(self):
        base = passing_facts()
        for identity in (
            None,
            GitIdentityReport(),
            GitIdentityReport(
                name=IDENT[0],
                email=IDENT[1],
                actor_name="someone-else",
                actor_email=IDENT[1],
                committer_name=IDENT[0],
                committer_email=IDENT[1],
                explicit=False,
                problems=("the identity Git would use is not user.name",),
            ),
            GitIdentityReport(name=" ", email=""),
        ):
            assert_gate_fails(replace(base, identity=identity), "G33")

    def test_g33_the_refusal_can_only_be_disabled_explicitly(self):
        base = passing_facts()
        facts = replace(
            base,
            identity=GitIdentityReport(),
            config=CommitPolicyConfig(require_explicit_identity=False),
        )
        assert status_of(facts, "G33") is GateStatus.PASS

    @pytest.mark.parametrize("marker", OPERATION_IN_PROGRESS_MARKERS)
    def test_g34_an_in_progress_operation_is_refused(self, marker):
        facts = passing_facts(operation_in_progress=(marker,))
        assert_gate_fails(facts, "G34", marker)

    def test_g34_a_held_index_lock_is_refused(self):
        base = passing_facts()
        facts = replace(
            base, repository=replace(base.repository, index_locked=True)
        )
        assert_gate_fails(facts, "G34", "index lock")

    def test_g35_unmerged_index_entries_are_refused(self):
        assert_gate_fails(
            passing_facts(unmerged_paths=(TARGET,)), "G35", "unmerged"
        )

    def test_g35_unmerged_entries_seen_while_staging_are_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            unmerged_paths=(),
            stage=replace(base.stage, unmerged_paths=(TARGET,)),
        )
        assert_gate_fails(facts, "G35", "unmerged")

    def test_g36_an_active_commit_hook_is_refused(self):
        for hook in ACCOUNTED_HOOK_NAMES:
            facts = passing_facts(hooks=HookReport(active_hooks=(hook,)))
            assert_gate_fails(facts, "G36", hook)

    def test_g36_a_clean_hooks_directory_is_allowed(self):
        assert status_of(passing_facts(), "G36") is GateStatus.PASS

    def test_g37_head_moving_during_the_run_is_refused(self):
        facts = passing_facts(
            after=snapshot(
                head=NEW_HEAD, changed=(TARGET,), content_ids={TARGET: BLOB}
            )
        )
        assert_gate_fails(facts, "G37", "HEAD moved")

    def test_g37_head_moving_while_staging_is_refused(self):
        base = passing_facts()
        facts = replace(base, stage=replace(base.stage, head_after=NEW_HEAD))
        assert_gate_fails(facts, "G37", "while staging")

    def test_g37_a_repository_head_that_differs_from_the_baseline_is_refused(self):
        base = passing_facts()
        facts = replace(
            base, repository=replace(base.repository, head_commit=NEW_HEAD)
        )
        assert_gate_fails(facts, "G37", "baseline recorded")


    def test_g32_a_missing_branch_is_refused(self):
        base = passing_facts()
        facts = replace(base, repository=replace(base.repository, branch=None))
        assert_gate_fails(facts, "G32")



# ---------------------------------------------------------------------------
# G38-G41: staging, index verification and the final pre-commit re-check
# ---------------------------------------------------------------------------


class TestStagingGates:
    def test_g38_a_worktree_that_changed_after_staging_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            stage=replace(
                base.stage, worktree_content_ids={TARGET: OTHER_BLOB}
            ),
        )
        assert_gate_fails(facts, "G38", "no longer matches")

    def test_g38_missing_approved_content_identity_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            stage=replace(
                base.stage,
                approved_content_ids={},
                worktree_content_ids={},
            ),
        )
        assert_gate_fails(facts, "G38", "no approved content identity")

    def test_g39_an_unexpected_staged_path_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            stage=replace(
                base.stage,
                staged_paths=(TARGET, "src/extra.py"),
                staged_blob_ids={TARGET: BLOB, "src/extra.py": OTHER_BLOB},
            ),
        )
        assert_gate_fails(facts, "G39", "unexpected staged path")

    def test_g39_an_approved_path_that_was_not_staged_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            stage=replace(
                base.stage, staged_paths=(), staged_blob_ids={}
            ),
        )
        assert_gate_fails(facts, "G39", "were not staged")

    def test_g40_staged_content_that_differs_from_the_approved_content(self):
        base = passing_facts()
        facts = replace(
            base,
            stage=replace(base.stage, staged_blob_ids={TARGET: OTHER_BLOB}),
        )
        assert_gate_fails(facts, "G40", "STAGED_MISMATCH")

    def test_g40_a_missing_staged_blob_id_is_refused(self):
        base = passing_facts()
        facts = replace(
            base, stage=replace(base.stage, staged_blob_ids={})
        )
        assert_gate_fails(facts, "G40", "no staged blob id")

    def test_g41_a_commit_without_a_final_recheck_is_refused(self):
        facts = passing_facts(pre_commit_stage=None)
        assert_gate_fails(facts, "G41", "not re-verified")

    def test_g41_a_staged_set_that_changed_before_the_commit_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            pre_commit_stage=replace(
                base.stage,
                staged_paths=(TARGET, "src/extra.py"),
                staged_blob_ids={TARGET: BLOB, "src/extra.py": OTHER_BLOB},
            ),
        )
        assert_gate_fails(facts, "G41", "path set changed")

    def test_g41_staged_content_that_changed_before_the_commit_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            pre_commit_stage=replace(
                base.stage,
                staged_blob_ids={TARGET: OTHER_BLOB},
                worktree_content_ids={TARGET: OTHER_BLOB},
            ),
        )
        assert_gate_fails(facts, "G41", "changed after it was staged")

    def test_g41_worktree_content_that_changed_before_the_commit_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            pre_commit_stage=replace(
                base.stage, worktree_content_ids={TARGET: OTHER_BLOB}
            ),
        )
        assert_gate_fails(facts, "G41", "worktree content changed")

    def test_g41_a_failed_recheck_is_refused(self):
        base = passing_facts()
        facts = replace(
            base, pre_commit_stage=replace(base.stage, error="ls-files failed")
        )
        assert_gate_fails(facts, "G41", "re-check failed")



# ---------------------------------------------------------------------------
# G42-G45: the message, the commit attempt and the post-commit proof
# ---------------------------------------------------------------------------


class TestCommitGates:
    def test_g42_a_missing_message_is_refused(self):
        assert_gate_fails(
            passing_facts(commit_message=None), "G42", "No commit message"
        )

    def test_g42_a_message_naming_another_file_is_refused(self):
        message = build_commit_message(
            rule=RULE, file_path="src/other.py", issue_key=ISSUE_KEY
        )
        assert_gate_fails(
            passing_facts(commit_message=message), "G42", "approved file"
        )

    def test_g42_a_message_naming_another_rule_is_refused(self):
        message = build_commit_message(
            rule="java:S108", file_path=TARGET, issue_key=ISSUE_KEY
        )
        assert_gate_fails(passing_facts(commit_message=message), "G42", "rule")

    def test_g42_a_message_naming_another_issue_is_refused(self):
        message = build_commit_message(
            rule=RULE, file_path=TARGET, issue_key="AX-OTHER"
        )
        assert_gate_fails(
            passing_facts(commit_message=message), "G42", "names issue"
        )

    def test_g42_a_message_that_fails_validation_is_refused(self):
        message = build_commit_message(
            rule=RULE, file_path=TARGET, issue_key=ISSUE_KEY
        )
        tampered = CommitMessage(
            subject=message.subject,
            issue_key=message.issue_key,
            rule=message.rule,
            file_path=message.file_path,
            body=("\x07injected",),
        )
        assert_gate_fails(
            passing_facts(commit_message=tampered), "G42", "failed validation"
        )

    def test_g43_a_commit_that_was_never_attempted_is_refused(self):
        base = passing_facts()
        facts = replace(
            base, commit=replace(base.commit, attempted=False, committed=False)
        )
        assert_gate_fails(facts, "G43", "No commit was attempted")

    def test_g43_a_failed_commit_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            commit=replace(
                base.commit,
                committed=False,
                error="git commit exited with code 1",
            ),
        )
        assert_gate_fails(facts, "G43", "did not succeed")

    @pytest.mark.parametrize(
        "overrides, needle",
        [
            ({"new_commit_count": 2}, "instead of"),
            ({"new_commit_count": None}, "instead of"),
            ({"parent_head": NEW_HEAD}, "parent"),
            ({"message": "a different message"}, "expected message"),
            ({"author_name": "Someone Else"}, "verified identity"),
            ({"committer_email": "other@example.com"}, "verified identity"),
            ({"committed_paths": (TARGET, "src/extra.py")}, "exactly the approved"),
            ({"expected_paths": (TARGET, "src/extra.py")}, "exactly the approved"),
            ({"committed_blob_ids": {TARGET: OTHER_BLOB}}, "not the approved content"),
            ({"deleted_paths": (TARGET,)}, "deletes path"),
            ({"renamed_paths": (TARGET,)}, "renames path"),
            ({"branch_after": "main"}, "branch is"),
            ({"worktree_root_after": "C:/somewhere/else"}, "identity changed"),
            ({"problems": ("a post-commit hook modified the file",)}, "could not be proven"),
        ],
    )
    def test_g44_an_unproven_post_commit_state_is_refused(self, overrides, needle):
        base = passing_facts()
        facts = replace(base, commit=replace(base.commit, **overrides))
        assert_gate_fails(facts, "G44", needle)

    def test_g44_a_commit_that_never_existed_is_refused(self):
        base = passing_facts()
        assert_gate_fails(
            replace(base, commit=None), "G44", "no commit to verify"
        )

    def test_g45_an_unexpected_git_error_is_refused(self):
        base = passing_facts()
        facts = replace(
            base,
            commit=replace(
                base.commit, unexpected_errors=("HEAD^ could not be read",)
            ),
        )
        assert_gate_fails(facts, "G45", "HEAD^ could not be read")

    def test_g45_a_staging_error_is_reported_through_g45(self):
        base = passing_facts()
        facts = replace(base, stage=replace(base.stage, error="git add failed"))
        assert_gate_fails(facts, "G45", "git add failed")



# ---------------------------------------------------------------------------
# Configuration, path normalisation and approved-set resolution
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def test_the_defaults_are_the_conservative_choices(self):
        config = CommitPolicyConfig()
        assert config.require_explicit_identity is True
        assert config.require_clean_baseline is True
        assert config.allow_ignored_approved_paths is False
        assert config.required_branch_prefix == DEFAULT_AGENT_BRANCH_PREFIX
        assert DEFAULT_AGENT_BRANCH_PREFIX == "ai/sonar-fix"
        assert set(config.protected_branches) == set(PROTECTED_BRANCH_NAMES)
        assert config.max_approved_files > 0

    @pytest.mark.parametrize(
        "branch", ["main", "MAIN", "Master", "develop", "trunk", None, ""]
    )
    def test_protected_default_and_reserved_branches(self, branch):
        assert CommitPolicyConfig().is_protected_branch(branch) is True

    def test_a_blank_branch_is_never_an_agent_branch(self):
        # A whitespace-only checkout name is not "protected", but it is also not
        # inside the agent namespace, so G32 refuses it either way.
        config = CommitPolicyConfig()
        assert config.is_agent_branch("   ") is False

    def test_a_renamed_default_branch_is_still_protected(self):
        config = CommitPolicyConfig(default_branch="deploy/prod")
        assert config.is_protected_branch("deploy/prod") is True
        assert config.is_protected_branch("Deploy/Prod") is True
        assert config.is_protected_branch("ai/sonar-fix/AX1") is False

    def test_the_agent_branch_namespace_is_exact(self):
        config = CommitPolicyConfig()
        assert config.is_agent_branch("ai/sonar-fix/AX1") is True
        assert config.is_agent_branch("ai/sonar-fix/scope/AX-1") is True
        for branch in (
            "ai/sonar-fix",
            "ai/sonar-fixX",
            "AI/sonar-fix/AX1",
            "feature/x",
            "main",
            None,
            "",
        ):
            assert config.is_agent_branch(branch) is False


class TestNormaliseApprovedPaths:
    def test_paths_are_normalized_deduplicated_and_sorted(self):
        accepted, problems = normalise_approved_paths(
            ["src/app.py", "./src/app.py", "src//app.py", "z.py"]
        )
        assert accepted == ("src/app.py", "z.py")
        assert problems == ()

    def test_hostile_paths_are_reported_never_silently_dropped(self):
        accepted, problems = normalise_approved_paths(
            ["/etc/passwd", "../x.py", "", "src/*.py", "-rf", "C:/x.py"]
        )
        assert accepted == ()
        assert len(problems) == 6

    def test_a_bare_string_is_a_caller_bug(self):
        for candidate in ("src/app.py", None, 5):
            with pytest.raises(CommitPolicyError):
                normalise_approved_paths(candidate)



def resolve(**overrides):
    """Resolve the approved set from the passing facts, overriding as needed."""
    base = passing_facts()
    kwargs = {
        "target_file": TARGET,
        "scope": make_scope(),
        "attribution": base.attribution,
        "baseline": base.baseline,
        "after": base.after,
        "config": CommitPolicyConfig(),
    }
    kwargs.update(overrides)
    return resolve_approved_files(**kwargs)


class TestResolveApprovedFiles:
    def test_the_runs_change_inside_the_scope_is_resolved_exactly(self):
        approved = resolve()
        assert approved.paths == (TARGET,)
        assert approved.agent_files == (TARGET,)
        assert approved.exactly_resolved is True
        assert approved.is_empty is False
        assert approved.outside_scope == ()
        assert approved.missing_files == ()
        assert approved.problems == ()
        assert TARGET in approved.reason_text

    def test_without_an_attribution_nothing_is_approved(self):
        approved = resolve(attribution=None)
        assert approved.is_empty is True
        assert approved.exactly_resolved is False
        assert any("attribution" in problem for problem in approved.problems)

    def test_a_change_outside_the_scope_is_never_approved(self):
        base = passing_facts()
        approved = resolve(
            attribution=replace(
                base.attribution, agent_files=(TARGET, "src/other.py")
            ),
            after=snapshot(
                changed=(TARGET, "src/other.py"),
                content_ids={TARGET: BLOB, "src/other.py": OTHER_BLOB},
            ),
        )
        assert approved.paths == (TARGET,)
        assert approved.outside_scope == ("src/other.py",)
        assert approved.exactly_resolved is False

    def test_a_deleted_agent_file_has_no_content_identity_and_is_refused(self):
        approved = resolve(after=snapshot(changed=(TARGET,), content_ids={}))
        assert approved.paths == ()
        assert approved.exactly_resolved is False
        assert any("content identity" in p for p in approved.problems)

    def test_an_expected_file_the_run_did_not_change_is_missing(self):
        base = passing_facts()
        approved = resolve(
            attribution=replace(base.attribution, agent_files=()),
            after=snapshot(clean=True),
        )
        assert approved.missing_files == (TARGET,)
        assert approved.is_empty is True
        assert approved.exactly_resolved is False

    def test_a_second_explicitly_proven_file_can_be_approved(self):
        extra = "src/helper.py"
        base = passing_facts()
        approved = resolve(
            scope=make_scope(expected=(TARGET, extra)),
            attribution=replace(base.attribution, agent_files=(TARGET, extra)),
            after=snapshot(
                changed=(TARGET, extra),
                content_ids={TARGET: BLOB, extra: OTHER_BLOB},
            ),
        )
        assert approved.paths == (TARGET, extra)
        assert approved.exactly_resolved is True

    def test_config_extra_allowed_files_widen_the_scope(self):
        extra = "src/helper.py"
        base = passing_facts()
        approved = resolve(
            scope=make_scope(expected=(TARGET,)),
            config=CommitPolicyConfig(extra_allowed_files=(extra,)),
            attribution=replace(base.attribution, agent_files=(TARGET, extra)),
            after=snapshot(
                changed=(TARGET, extra),
                content_ids={TARGET: BLOB, extra: OTHER_BLOB},
            ),
        )
        assert approved.paths == (TARGET, extra)
        assert approved.exactly_resolved is True

    def test_the_approved_file_cap_is_enforced(self):
        paths = tuple(f"src/mod_{index}.py" for index in range(3))
        base = passing_facts()
        approved = resolve(
            scope=make_scope(expected=paths),
            config=CommitPolicyConfig(max_approved_files=2),
            attribution=replace(base.attribution, agent_files=paths),
            after=snapshot(
                changed=paths, content_ids={path: BLOB for path in paths}
            ),
        )
        assert approved.exactly_resolved is False
        assert any(
            "above the configured maximum" in problem
            for problem in approved.problems
        )

    def test_an_unsafe_target_file_is_a_problem_never_a_silent_pass(self):
        approved = resolve(target_file="../outside.py")
        assert approved.exactly_resolved is False
        assert any(
            "../outside.py" in problem and "safe" in problem
            for problem in approved.problems
        )

    def test_no_resolvable_scope_at_all_approves_nothing(self):
        approved = resolve(target_file="../outside.py", scope=None)
        assert approved.is_empty is True
        assert approved.exactly_resolved is False
        assert approved.problems

