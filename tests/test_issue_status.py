"""T19 tests: the final-status decision.

Every important combination of the inputs is exercised, with special attention
to the rule that a still-open original issue can never be reported as ``FIXED``,
that a failed/unidentified analysis trigger maps to ``ANALYSIS_FAILED``, and
that ``FIXED`` requires a *correlated* verification. The decision layer is pure -
no client, subprocess or network is touched here.
"""

import dataclasses
import json

import pytest

from analysis_correlation import AnalysisCorrelation
from change_scope import ChangeScopeResult
from codex_executor import CodexExecutionStatus, CodexResult
from codex_result import analyze_codex_result
from issue_status import (
    ANALYSIS_STAGE_STATUS,
    PRECEDENCE,
    VERIFICATION_OUTCOME_STATUS,
    AnalysisStageOutcome,
    IssueFinalStatus,
    IssueStatusResult,
    VerificationOutcome,
    determine_issue_status,
    resolve_analysis_stage,
    resolve_verification_outcome,
)
from models import SonarIssue
from sonar_analysis import SonarAnalysisStatus, SonarAnalysisTriggerResult
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_issue_verification import (
    IssueMatchType,
    SonarIssueVerificationResult,
    SonarIssueVerifier,
)
from test_result import analyze_test_result
from test_runner import TestResult, TestStatus

TARGET = "src/app.py"


def make_codex(status=CodexExecutionStatus.SUCCESS, *, uncertain=False):
    text = "I am uncertain about this fix" if uncertain else "fixed the issue"
    if status is CodexExecutionStatus.SUCCESS:
        exit_code, error = 0, None
    elif status is CodexExecutionStatus.FAILED:
        exit_code, error = 2, "Codex exited with code 2."
    else:
        exit_code, error = None, "Codex did not complete."
    return analyze_codex_result(
        CodexResult(
            status=status,
            exit_code=exit_code,
            stdout=text,
            stderr="",
            command=("codex", "exec"),
            error=error,
        )
    )


def make_scope(*, valid=True, modified=True):
    changed = (TARGET,) if modified else ()
    return ChangeScopeResult(
        is_valid=valid,
        expected_files=(TARGET,),
        changed_files=changed,
        unexpected_files=() if valid else ("src/other.py",),
        has_changes=modified or not valid,
        expected_file_modified=modified,
        reasons=("Scope policy: only 'src/app.py' may change for this issue.",),
    )


def make_tests(status=TestStatus.PASSED):
    if status is TestStatus.PASSED:
        exit_code, error = 0, None
    elif status is TestStatus.FAILED:
        exit_code, error = 1, "Project tests exited with code 1."
    else:
        exit_code, error = None, "Project tests did not run."
    return analyze_test_result(
        TestResult(
            status=status,
            exit_code=exit_code,
            stdout="",
            stderr="",
            command=("pytest", "-q"),
            error=error,
        )
    )


def make_analysis(status=SonarAnalysisState.SUCCESS, **kwargs):
    failure = None
    if status is SonarAnalysisState.FAILED:
        failure = "The SonarQube compute-engine task failed."
    elif status is SonarAnalysisState.CANCELED:
        failure = "The SonarQube compute-engine task was canceled."
    elif status is SonarAnalysisState.TIMEOUT:
        failure = "The analysis is unverified."
    elif status is SonarAnalysisState.UNKNOWN:
        failure = "The triggered analysis cannot be attributed to this run."
    kwargs.setdefault("task_id", "AY1")
    kwargs.setdefault("elapsed_seconds", 2.0)
    kwargs.setdefault("poll_count", 1)
    kwargs.setdefault("reason", f"analysis {status.value}")
    return SonarAnalysisCompletion(
        status=status, failure_reason=failure, **kwargs
    )


def make_trigger(
    status=SonarAnalysisStatus.TRIGGERED, *, task_id="AY1", error=None
):
    """A T16 trigger result; defaults to a successful trigger with a task id."""
    return SonarAnalysisTriggerResult(
        status=status,
        exit_code=0 if status is SonarAnalysisStatus.TRIGGERED else 1,
        stdout="",
        stderr="",
        command=("sonar-scanner",),
        task_id=task_id,
        error=error,
    )


def make_verification(
    *,
    present=False,
    reliable=True,
    retrieved=True,
    match_type=IssueMatchType.KEY,
    correlation=AnalysisCorrelation.CORRELATED,
):
    issue = SonarIssue(
        key="AX1",
        rule="python:S108",
        severity="MAJOR",
        issue_type="CODE_SMELL",
        message="define a constant",
        component="demo:src/app.py",
        line=3,
        status="OPEN",
    )
    error = None if retrieved else "SonarQube issue retrieval failed."
    return SonarIssueVerificationResult(
        original_issue=issue,
        original_issue_key="AX1",
        retrieval_succeeded=retrieved,
        current_issues=(issue,) if present else (),
        matches=(issue,) if present else (),
        matching_current_issue=issue if present and reliable else None,
        match_type=match_type if (present or retrieved) else IssueMatchType.NONE,
        is_present=present,
        identity_reliable=reliable,
        page_complete=True,
        retrieved_count=1 if present else 0,
        skipped_entries=0,
        reason=(
            "The original issue is still reported as open."
            if present
            else "The original issue is not reported as open by the new analysis."
        ),
        reasons=(
            "Identity uses the original SonarQube issue key 'AX1'.",
            "The original issue is still reported as open."
            if present
            else "The original issue key is not among the current open issues.",
        ),
        error=error,
        correlation=correlation,
        correlation_reason=(
            "The issue snapshot could not be attributed to the verified "
            "analysis (fixture)."
        ),
    )


def decide(**overrides):
    """Run the decision with green inputs, overridable per test."""
    inputs = {
        "codex": make_codex(),
        "scope": make_scope(),
        "tests": make_tests(),
        "analysis": make_analysis(),
        "verification": make_verification(),
        "trigger": make_trigger(),
    }
    inputs.update(overrides)
    return determine_issue_status(**inputs)


class TestPrecedenceLadder:
    def test_documented_precedence_order(self):
        assert PRECEDENCE == (
            IssueFinalStatus.CODEX_FAILED,
            IssueFinalStatus.SCOPE_INVALID,
            IssueFinalStatus.TESTS_FAILED,
            IssueFinalStatus.ANALYSIS_FAILED,
            IssueFinalStatus.REVIEW_REQUIRED,
            IssueFinalStatus.STILL_OPEN,
            IssueFinalStatus.FIXED,
        )

    def test_all_statuses_are_named_in_the_ladder(self):
        assert set(PRECEDENCE) == set(IssueFinalStatus)


class TestCodexStage:
    @pytest.mark.parametrize(
        "status",
        [
            CodexExecutionStatus.FAILED,
            CodexExecutionStatus.TIMEOUT,
            CodexExecutionStatus.NOT_FOUND,
        ],
    )
    def test_codex_failure_wins_over_everything(self, status):
        result = decide(
            codex=make_codex(status),
            scope=make_scope(valid=False),
            tests=make_tests(TestStatus.FAILED),
            analysis=make_analysis(SonarAnalysisState.FAILED),
            verification=make_verification(present=True, reliable=False),
        )
        assert result.status is IssueFinalStatus.CODEX_FAILED
        assert result.decisive_stage == "codex_execution"
        assert result.is_fixed is False
        assert result.needs_review is False
        assert result.blocking_reason == result.reason

    def test_codex_failure_is_not_a_review_required(self):
        assert decide(codex=make_codex(CodexExecutionStatus.FAILED)).status is not (
            IssueFinalStatus.REVIEW_REQUIRED
        )


class TestScopeStage:
    def test_scope_invalid_wins_over_tests_and_verification(self):
        result = decide(
            scope=make_scope(valid=False),
            tests=make_tests(TestStatus.FAILED),
            verification=make_verification(present=False, reliable=True),
        )
        assert result.status is IssueFinalStatus.SCOPE_INVALID
        assert result.decisive_stage == "change_scope"
        assert "1 unexpected file" in result.reason

    def test_scope_invalid_after_successful_codex(self):
        result = decide(scope=make_scope(valid=False))
        assert result.status is IssueFinalStatus.SCOPE_INVALID
        assert result.reasons[0] == "Codex executed successfully."


class TestTestsStage:
    @pytest.mark.parametrize(
        "status",
        [
            TestStatus.FAILED,
            TestStatus.TIMEOUT,
            TestStatus.NOT_FOUND,
            TestStatus.EXECUTION_ERROR,
        ],
    )
    def test_tests_failure_wins_over_a_disappeared_issue(self, status):
        """A green SonarQube picture never overrides broken tests."""
        result = decide(
            tests=make_tests(TestStatus.FAILED)
            if status is TestStatus.FAILED
            else make_tests(status),
            verification=make_verification(present=False, reliable=True),
        )
        assert result.status is IssueFinalStatus.TESTS_FAILED
        assert result.decisive_stage == "project_tests"
        assert result.is_fixed is False

    def test_tests_failure_reason_comes_from_the_test_outcome(self):
        result = decide(tests=make_tests(TestStatus.FAILED))
        assert "exit code 1" in result.reason


class TestAnalysisStage:
    @pytest.mark.parametrize(
        "state",
        [
            SonarAnalysisState.FAILED,
            SonarAnalysisState.CANCELED,
            SonarAnalysisState.TIMEOUT,
        ],
    )
    def test_definitive_analysis_failure(self, state):
        result = decide(analysis=make_analysis(state))
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED
        assert result.decisive_stage == "sonar_analysis"
        assert result.is_fixed is False

    def test_unknown_analysis_state_requires_review(self):
        result = decide(analysis=make_analysis(SonarAnalysisState.UNKNOWN))
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.decisive_stage == "sonar_analysis"
        assert result.needs_review is True

    def test_analysis_failure_wins_over_still_open(self):
        result = decide(
            analysis=make_analysis(SonarAnalysisState.FAILED),
            verification=make_verification(present=True),
        )
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED



class TestVerificationStage:
    def test_retrieval_failure_requires_review(self):
        result = decide(verification=make_verification(retrieved=False))
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.decisive_stage == "sonar_verification"
        assert result.needs_review is True

    def test_issue_still_open_is_still_open_not_fixed(self):
        result = decide(verification=make_verification(present=True))
        assert result.status is IssueFinalStatus.STILL_OPEN
        assert result.is_fixed is False
        assert result.decisive_stage == "sonar_verification"
        assert "still open" in result.reason

    def test_ambiguous_presence_requires_review(self):
        result = decide(verification=make_verification(present=True, reliable=False))
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.needs_review is True

    def test_absent_but_unreliable_identity_requires_review(self):
        result = decide(
            verification=make_verification(
                present=False, reliable=False, match_type=IssueMatchType.NONE
            )
        )
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert "not reliable" in result.reason

    def test_absent_but_no_change_requires_review_for_attribution(self):
        result = decide(scope=make_scope(modified=False))
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.decisive_stage == "fix_attribution"
        assert "attributed" in result.reason


class TestFixed:
    def test_all_preconditions_pass_yields_fixed(self):
        result = decide()
        assert result.status is IssueFinalStatus.FIXED
        assert result.is_fixed is True
        assert result.needs_review is False
        assert result.blocking_reason is None
        assert result.decisive_stage == "fix_attribution"
        assert "Every precondition passed" in result.reason_text

    def test_fixed_requires_the_issue_to_be_absent(self):
        """The critical rule: a present issue can never be FIXED."""
        result = decide(verification=make_verification(present=True))
        assert result.status is not IssueFinalStatus.FIXED
        assert result.is_fixed is False

    def test_successful_analysis_alone_does_not_imply_fixed(self):
        """Analysis success plus a still-open issue is STILL_OPEN."""
        result = decide(
            analysis=make_analysis(SonarAnalysisState.SUCCESS),
            verification=make_verification(present=True),
        )
        assert result.status is IssueFinalStatus.STILL_OPEN

    def test_codex_uncertainty_is_advisory_only(self):
        result = decide(codex=make_codex(uncertain=True))
        assert result.status is IssueFinalStatus.FIXED
        assert any("Advisory" in reason for reason in result.reasons)

    def test_codex_uncertainty_does_not_rescue_a_failing_stage(self):
        result = decide(
            codex=make_codex(uncertain=True), tests=make_tests(TestStatus.FAILED)
        )
        assert result.status is IssueFinalStatus.TESTS_FAILED



class TestPurityAndModel:
    def test_decision_is_deterministic(self):
        codex, scope, tests = make_codex(), make_scope(), make_tests()
        analysis, verification = make_analysis(), make_verification(present=True)
        first = determine_issue_status(
            codex=codex,
            scope=scope,
            tests=tests,
            analysis=analysis,
            verification=verification,
        )
        second = determine_issue_status(
            codex=codex,
            scope=scope,
            tests=tests,
            analysis=analysis,
            verification=verification,
        )
        assert first == second

    def test_decision_does_not_retrieve_or_touch_anything(self):
        class CountingClient:
            def __init__(self):
                self.calls = 0

            def get_open_issues(self):
                self.calls += 1
                return {"total": 0, "issues": []}

        client = CountingClient()
        verification = SonarIssueVerifier(client).verify(
            make_verification().original_issue
        )
        assert client.calls == 1
        decide(verification=verification)
        decide(verification=verification)
        assert client.calls == 1

    def test_result_is_frozen(self):
        result = decide()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = IssueFinalStatus.STILL_OPEN

    def test_result_is_json_serializable(self):
        payload = decide().as_dict()
        assert json.loads(json.dumps(payload))["status"] == "fixed"
        assert payload["is_fixed"] is True
        assert payload["needs_review"] is False
        assert set(payload) == {
            "status",
            "decisive_stage",
            "is_fixed",
            "needs_review",
            "reason",
            "reasons",
            "codex",
            "scope",
            "tests",
            "analysis",
            "verification",
            "trigger_evidence",
        }

    @pytest.mark.parametrize(
        "overrides",
        [
            {"codex": make_codex(CodexExecutionStatus.FAILED)},
            {"scope": make_scope(valid=False)},
            {"tests": make_tests(TestStatus.FAILED)},
            {"analysis": make_analysis(SonarAnalysisState.FAILED)},
            {"analysis": make_analysis(SonarAnalysisState.UNKNOWN)},
            {"trigger": make_trigger(SonarAnalysisStatus.FAILED)},
            {"trigger": make_trigger(task_id=None)},
            {
                "trigger": make_trigger(task_id="AY1"),
                "analysis": make_analysis(task_id="AY2"),
            },
            {"verification": make_verification(present=True)},
            {"verification": make_verification(present=True, reliable=False)},
            {"verification": make_verification(retrieved=False)},
            {
                "verification": make_verification(
                    correlation=AnalysisCorrelation.UNKNOWN
                )
            },
            {
                "verification": make_verification(
                    correlation=AnalysisCorrelation.NOT_CORRELATED
                )
            },
            {"scope": make_scope(modified=False)},
        ],
    )
    def test_non_fixed_outcomes_always_expose_a_blocking_reason(self, overrides):
        result = decide(**overrides)
        assert isinstance(result, IssueStatusResult)
        assert result.is_fixed is False
        assert result.blocking_reason == result.reason
        assert result.reason


class TestAnalysisStageMapping:
    """Exhaustive T16/T17 -> T19 translation."""

    def test_status_maps_are_total_over_the_outcome_enums(self):
        assert set(ANALYSIS_STAGE_STATUS) == set(AnalysisStageOutcome)
        assert set(VERIFICATION_OUTCOME_STATUS) == set(VerificationOutcome)
        assert ANALYSIS_STAGE_STATUS[AnalysisStageOutcome.COMPLETED] is None
        assert (
            VERIFICATION_OUTCOME_STATUS[VerificationOutcome.ABSENT_VERIFIED] is None
        )

    @pytest.mark.parametrize(
        "status",
        [
            SonarAnalysisStatus.FAILED,
            SonarAnalysisStatus.TIMEOUT,
            SonarAnalysisStatus.NOT_FOUND,
            SonarAnalysisStatus.EXECUTION_ERROR,
        ],
    )
    def test_t16_failure_translates_to_trigger_failed(self, status):
        outcome, reason = resolve_analysis_stage(
            make_trigger(status, error="scanner could not run"),
            make_analysis(),
        )
        assert outcome is AnalysisStageOutcome.TRIGGER_FAILED
        assert "scanner could not run" in reason
        assert ANALYSIS_STAGE_STATUS[outcome] is IssueFinalStatus.ANALYSIS_FAILED

    def test_t16_failure_maps_to_analysis_failed(self):
        result = decide(trigger=make_trigger(SonarAnalysisStatus.FAILED))
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED
        assert result.decisive_stage == "sonar_analysis"
        assert result.is_fixed is False
        assert result.needs_review is False

    def test_t16_failure_beats_a_successful_later_analysis(self):
        """A failed trigger can never be rescued by a green-looking analysis."""
        result = decide(
            trigger=make_trigger(SonarAnalysisStatus.TIMEOUT),
            analysis=make_analysis(SonarAnalysisState.SUCCESS),
            verification=make_verification(),
        )
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED

    def test_trigger_without_task_id_maps_to_analysis_failed(self):
        outcome, reason = resolve_analysis_stage(
            make_trigger(task_id=None), make_analysis()
        )
        assert outcome is AnalysisStageOutcome.ANALYSIS_UNIDENTIFIED
        assert "task id" in reason
        assert (
            decide(trigger=make_trigger(task_id=None)).status
            is IssueFinalStatus.ANALYSIS_FAILED
        )

    def test_trigger_with_blank_task_id_maps_to_analysis_failed(self):
        assert (
            decide(trigger=make_trigger(task_id="   ")).status
            is IssueFinalStatus.ANALYSIS_FAILED
        )

    def test_task_id_mismatch_between_t16_and_t17_is_analysis_failed(self):
        outcome, reason = resolve_analysis_stage(
            make_trigger(task_id="AY1"), make_analysis(task_id="AY2")
        )
        assert outcome is AnalysisStageOutcome.ANALYSIS_IDENTITY_MISMATCH
        assert "does not match" in reason
        result = decide(
            trigger=make_trigger(task_id="AY1"),
            analysis=make_analysis(task_id="AY2"),
        )
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED

    def test_matching_task_ids_proceed(self):
        outcome, _reason = resolve_analysis_stage(
            make_trigger(task_id="AY1"), make_analysis(task_id="AY1")
        )
        assert outcome is AnalysisStageOutcome.COMPLETED

    def test_t17_terminal_failures_map_to_analysis_failed(self):
        for state in (
            SonarAnalysisState.FAILED,
            SonarAnalysisState.CANCELED,
            SonarAnalysisState.TIMEOUT,
        ):
            outcome, _reason = resolve_analysis_stage(
                make_trigger(), make_analysis(state)
            )
            assert outcome is AnalysisStageOutcome.ANALYSIS_FAILED
            assert (
                decide(trigger=make_trigger(), analysis=make_analysis(state)).status
                is IssueFinalStatus.ANALYSIS_FAILED
            )

    def test_t17_unknown_maps_to_review_required(self):
        outcome, _reason = resolve_analysis_stage(
            make_trigger(), make_analysis(SonarAnalysisState.UNKNOWN)
        )
        assert outcome is AnalysisStageOutcome.ANALYSIS_INCOMPLETE
        result = decide(
            trigger=make_trigger(),
            analysis=make_analysis(SonarAnalysisState.UNKNOWN),
        )
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.needs_review is True

    def test_missing_t16_evidence_uses_t17_only(self):
        assert decide(trigger=None).status is IssueFinalStatus.FIXED

    def test_missing_t16_evidence_with_failed_t17_is_analysis_failed(self):
        result = decide(trigger=None, analysis=make_analysis(SonarAnalysisState.FAILED))
        assert result.status is IssueFinalStatus.ANALYSIS_FAILED

    @pytest.mark.parametrize("kind", ["none", "triggered", "no-task-id", "failed"])
    @pytest.mark.parametrize("state", list(SonarAnalysisState))
    def test_full_t16_t17_matrix_is_deterministic_and_fail_closed(self, kind, state):
        triggers = {
            "none": None,
            "triggered": make_trigger(),
            "no-task-id": make_trigger(task_id=None),
            "failed": make_trigger(SonarAnalysisStatus.FAILED),
        }
        trigger = triggers[kind]
        completion = make_analysis(state)

        outcome, reason = resolve_analysis_stage(trigger, completion)
        assert reason

        if kind == "failed":
            assert outcome is AnalysisStageOutcome.TRIGGER_FAILED
        elif kind == "no-task-id":
            assert outcome is AnalysisStageOutcome.ANALYSIS_UNIDENTIFIED
        elif state is SonarAnalysisState.SUCCESS:
            assert outcome is AnalysisStageOutcome.COMPLETED
        elif state is SonarAnalysisState.UNKNOWN:
            assert outcome is AnalysisStageOutcome.ANALYSIS_INCOMPLETE
        else:
            assert outcome is AnalysisStageOutcome.ANALYSIS_FAILED

        result = decide(trigger=trigger, analysis=completion)
        mapped = ANALYSIS_STAGE_STATUS[outcome]
        if mapped is None:
            assert result.status is IssueFinalStatus.FIXED
        else:
            assert result.status is mapped
        assert result.status is not IssueFinalStatus.FIXED or (
            outcome is AnalysisStageOutcome.COMPLETED
        )


class TestVerificationMapping:
    def test_verification_outcomes_are_complete(self):
        cases = [
            (make_verification(retrieved=False), VerificationOutcome.NOT_RETRIEVED),
            (
                make_verification(correlation=AnalysisCorrelation.UNKNOWN),
                VerificationOutcome.NOT_CORRELATED,
            ),
            (
                make_verification(correlation=AnalysisCorrelation.NOT_CORRELATED),
                VerificationOutcome.NOT_CORRELATED,
            ),
            (make_verification(present=True), VerificationOutcome.STILL_OPEN),
            (
                make_verification(present=True, reliable=False),
                VerificationOutcome.PRESENCE_AMBIGUOUS,
            ),
            (
                make_verification(reliable=False, match_type=IssueMatchType.NONE),
                VerificationOutcome.ABSENCE_UNRELIABLE,
            ),
            (make_verification(), VerificationOutcome.ABSENT_VERIFIED),
        ]
        for verification, expected in cases:
            outcome, reason = resolve_verification_outcome(verification)
            assert outcome is expected
            assert reason

    def test_every_analysis_and_verification_combination_is_deterministic(self):
        completions = {
            "success": make_analysis(SonarAnalysisState.SUCCESS),
            "failed": make_analysis(SonarAnalysisState.FAILED),
            "canceled": make_analysis(SonarAnalysisState.CANCELED),
            "timeout": make_analysis(SonarAnalysisState.TIMEOUT),
            "unknown": make_analysis(SonarAnalysisState.UNKNOWN),
        }
        verifications = {
            "retrieval-failed": make_verification(retrieved=False),
            "uncorrelated": make_verification(
                correlation=AnalysisCorrelation.UNKNOWN
            ),
            "still-open": make_verification(present=True),
            "presence-ambiguous": make_verification(present=True, reliable=False),
            "absence-unreliable": make_verification(
                reliable=False, match_type=IssueMatchType.NONE
            ),
            "absent-verified": make_verification(),
        }

        for analysis_name, completion in completions.items():
            analysis_outcome, _ = resolve_analysis_stage(make_trigger(), completion)
            expected_analysis = ANALYSIS_STAGE_STATUS[analysis_outcome]
            for verification_name, verification in verifications.items():
                result = decide(analysis=completion, verification=verification)
                if expected_analysis is not None:
                    assert result.status is expected_analysis
                else:
                    verification_outcome, _ = resolve_verification_outcome(
                        verification
                    )
                    expected = VERIFICATION_OUTCOME_STATUS[verification_outcome]
                    if expected is None:
                        assert result.status is IssueFinalStatus.FIXED
                    else:
                        assert result.status is expected
                if result.status is IssueFinalStatus.FIXED:
                    assert analysis_name == "success"
                    assert verification_name == "absent-verified"
                else:
                    assert result.is_fixed is False
                    assert result.blocking_reason == result.reason


class TestCorrelationGate:
    """T19 must refuse FIXED whenever the snapshot is not correlated."""

    def test_unknown_correlation_refuses_fixed(self):
        result = decide(
            verification=make_verification(correlation=AnalysisCorrelation.UNKNOWN)
        )
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.is_fixed is False
        assert result.needs_review is True
        assert result.decisive_stage == "sonar_verification"
        assert result.blocking_reason == result.reason

    def test_not_correlated_refuses_fixed(self):
        result = decide(
            verification=make_verification(
                correlation=AnalysisCorrelation.NOT_CORRELATED
            )
        )
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.is_fixed is False

    def test_correlated_absence_can_be_fixed(self):
        result = decide(
            verification=make_verification(correlation=AnalysisCorrelation.CORRELATED)
        )
        assert result.status is IssueFinalStatus.FIXED

    def test_still_open_is_still_reported_without_correlation(self):
        """A positive finding stays visible; it can never become FIXED."""
        result = decide(
            verification=make_verification(
                present=True, correlation=AnalysisCorrelation.UNKNOWN
            )
        )
        assert result.status is IssueFinalStatus.STILL_OPEN
        assert result.is_fixed is False

    def test_retrieval_failure_still_wins_over_correlation(self):
        result = decide(
            verification=make_verification(
                retrieved=False, correlation=AnalysisCorrelation.UNKNOWN
            )
        )
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED
        assert result.decisive_stage == "sonar_verification"

    def test_reliable_absence_requires_correlation(self):
        assert make_verification().reliable_absence is True
        unknown = make_verification(correlation=AnalysisCorrelation.UNKNOWN)
        assert unknown.reliable_absence is False
        wrong = make_verification(correlation=AnalysisCorrelation.NOT_CORRELATED)
        assert wrong.reliable_absence is False
        assert make_verification(present=True).reliable_absence is False

    def test_correlation_reason_is_carried_into_the_decision(self):
        verification = make_verification(correlation=AnalysisCorrelation.UNKNOWN)
        result = decide(verification=verification)
        assert result.reason == verification.correlation_reason
        assert "attributed" in result.reason

