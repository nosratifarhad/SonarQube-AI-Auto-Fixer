"""Tests for the T17 -> T18 analysis-correlation precondition.

Pure logic only: no SonarQube, no network, no Git. The end-to-end cases
reuse the T18 verifier (with a fake issues client) and the T19 decision.
"""

import dataclasses

import pytest

from analysis_correlation import (
    AnalysisCorrelation,
    AnalysisCorrelationResult,
    correlate_analysis,
)
from change_scope import ChangeScopeResult
from codex_executor import CodexExecutionStatus, CodexResult
from codex_result import analyze_codex_result
from issue_status import IssueFinalStatus, determine_issue_status
from models import SonarIssue
from sonar_analysis import SonarAnalysisStatus, SonarAnalysisTriggerResult
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_issue_verification import SonarIssueVerifier
from test_result import analyze_test_result
from test_runner import TestResult, TestStatus

PROJECT = "demo"
TASK_ID = "AY1abcDEF"
ANALYSIS_ID = "A-1"


class FakeIssuesClient:
    """Returns a canned ``get_open_issues()`` payload."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {"total": 0, "issues": []}
        self.error = error
        self.calls = 0

    def get_open_issues(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def make_completion(
    status=SonarAnalysisState.SUCCESS,
    *,
    task_id=TASK_ID,
    analysis_id=ANALYSIS_ID,
    component_key=PROJECT,
):
    return SonarAnalysisCompletion(
        status=status,
        task_id=task_id,
        elapsed_seconds=3.0,
        poll_count=2,
        reason="fixture",
        failure_reason=None if status is SonarAnalysisState.SUCCESS else "fixture",
        analysis_id=analysis_id,
        component_key=component_key,
    )


class TestCorrelationDecision:
    def test_exact_correlation_succeeds(self):
        result = correlate_analysis(
            make_completion(), project_key=PROJECT, exclusive_analysis=True
        )
        assert isinstance(result, AnalysisCorrelationResult)
        assert result.correlation is AnalysisCorrelation.CORRELATED
        assert result.is_correlated is True
        assert result.is_unknown is False
        assert result.is_not_correlated is False
        assert ANALYSIS_ID in result.reason

    def test_missing_completion_is_unknown(self):
        result = correlate_analysis(None, project_key=PROJECT, exclusive_analysis=True)
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert result.is_correlated is False
        assert result.task_id is None
        assert result.analysis_id is None

    def test_unknown_analysis_state_is_not_correlated(self):
        result = correlate_analysis(
            make_completion(SonarAnalysisState.UNKNOWN),
            project_key=PROJECT,
            exclusive_analysis=True,
        )
        assert result.correlation is AnalysisCorrelation.NOT_CORRELATED

    @pytest.mark.parametrize(
        "status",
        [
            SonarAnalysisState.FAILED,
            SonarAnalysisState.CANCELED,
            SonarAnalysisState.TIMEOUT,
        ],
    )
    def test_unfinished_analysis_is_not_correlated(self, status):
        result = correlate_analysis(
            make_completion(status), project_key=PROJECT, exclusive_analysis=True
        )
        assert result.correlation is AnalysisCorrelation.NOT_CORRELATED
        assert result.is_correlated is False

    @pytest.mark.parametrize("task_id", [None, "", "   "])
    def test_missing_task_id_is_unknown(self, task_id):
        result = correlate_analysis(
            make_completion(task_id=task_id),
            project_key=PROJECT,
            exclusive_analysis=True,
        )
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "task id" in result.reason

    @pytest.mark.parametrize("analysis_id", [None, "", "   "])
    def test_missing_analysis_id_is_unknown(self, analysis_id):
        result = correlate_analysis(
            make_completion(analysis_id=analysis_id),
            project_key=PROJECT,
            exclusive_analysis=True,
        )
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "analysisId" in result.reason

    def test_wrong_component_is_not_correlated(self):
        result = correlate_analysis(
            make_completion(component_key="another-project"),
            project_key=PROJECT,
            exclusive_analysis=True,
        )
        assert result.correlation is AnalysisCorrelation.NOT_CORRELATED
        assert "another-project" in result.reason

    def test_absent_component_is_accepted(self):
        result = correlate_analysis(
            make_completion(component_key=None),
            project_key=PROJECT,
            exclusive_analysis=True,
        )
        assert result.correlation is AnalysisCorrelation.CORRELATED

    def test_missing_project_key_is_unknown(self):
        result = correlate_analysis(
            make_completion(component_key="whatever"), exclusive_analysis=True
        )
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "not configured" in result.reason

    def test_single_analysis_precondition_is_required(self):
        result = correlate_analysis(make_completion(), project_key=PROJECT)
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert result.exclusive_analysis is False

    def test_precondition_default_is_fail_closed(self):
        assert correlate_analysis(make_completion()).is_correlated is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"exclusive_analysis": True},
            {"project_key": PROJECT},
            {"project_key": "other", "exclusive_analysis": True},
        ],
    )
    @pytest.mark.parametrize("status", list(SonarAnalysisState))
    def test_unknown_is_never_reported_as_success(self, kwargs, status):
        result = correlate_analysis(make_completion(status), **kwargs)
        if result.correlation is AnalysisCorrelation.CORRELATED:
            assert status is SonarAnalysisState.SUCCESS
            assert kwargs.get("exclusive_analysis") is True
        else:
            assert result.is_correlated is False

    def test_result_is_frozen_and_serialisable(self):
        result = correlate_analysis(
            make_completion(), project_key=PROJECT, exclusive_analysis=True
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.correlation = AnalysisCorrelation.UNKNOWN
        payload = result.as_dict()
        assert payload["correlation"] == "correlated"
        assert payload["task_id"] == TASK_ID
        assert payload["analysis_id"] == ANALYSIS_ID

    def test_reason_text_joins_reasons(self):
        result = correlate_analysis(None)
        assert result.reason_text.startswith(result.reasons[0])

    def test_tokens_are_never_leaked_from_metadata(self):
        completion = make_completion(
            component_key="https://bob:hunter2@example.com/sonar"
        )
        result = correlate_analysis(
            completion, project_key=PROJECT, exclusive_analysis=True
        )
        assert "hunter2" not in str(result.as_dict())
        assert "hunter2" not in result.reason

    def test_overlong_metadata_is_capped(self):
        completion = make_completion(analysis_id="x" * 5000)
        result = correlate_analysis(
            completion, project_key=PROJECT, exclusive_analysis=True
        )
        assert len(result.analysis_id) <= 200

    def test_decision_is_deterministic(self):
        first = correlate_analysis(
            make_completion(), project_key=PROJECT, exclusive_analysis=True
        )
        second = correlate_analysis(
            make_completion(), project_key=PROJECT, exclusive_analysis=True
        )
        assert first == second


def green_codex():
    return analyze_codex_result(
        CodexResult(
            status=CodexExecutionStatus.SUCCESS,
            exit_code=0,
            stdout="fixed the issue",
            stderr="",
            command=("codex", "exec"),
            error=None,
        )
    )


def green_scope():
    return ChangeScopeResult(
        is_valid=True,
        expected_files=("src/app.py",),
        changed_files=("src/app.py",),
        unexpected_files=(),
        has_changes=True,
        expected_file_modified=True,
        reasons=("Scope policy: only 'src/app.py' may change for this issue.",),
    )


def green_tests():
    return analyze_test_result(
        TestResult(
            status=TestStatus.PASSED,
            exit_code=0,
            stdout="",
            stderr="",
            command=("pytest", "-q"),
            error=None,
        )
    )


def green_trigger():
    return SonarAnalysisTriggerResult(
        status=SonarAnalysisStatus.TRIGGERED,
        exit_code=0,
        stdout="",
        stderr="",
        command=("sonar-scanner",),
        task_id=TASK_ID,
        error=None,
    )


def decide_with(verification, completion_value):
    return determine_issue_status(
        codex=green_codex(),
        scope=green_scope(),
        tests=green_tests(),
        analysis=completion_value,
        verification=verification,
        trigger=green_trigger(),
    )


class TestEndToEndCorrelationGate:
    """The verifier's correlation verdict decides whether T19 may say FIXED."""

    def test_correlated_clean_snapshot_reaches_fixed(self):
        client = FakeIssuesClient()
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        completion_value = make_completion()
        verification = verifier.verify(
            SonarIssue(
                key="AX1",
                rule="python:S108",
                severity="MAJOR",
                issue_type="CODE_SMELL",
                message="define a constant",
                component=f"{PROJECT}:src/app.py",
                line=3,
                status="OPEN",
            ),
            completion=completion_value,
        )
        result = decide_with(verification, completion_value)
        assert result.status is IssueFinalStatus.FIXED

    @pytest.mark.parametrize(
        "verifier_kwargs",
        [
            {},
            {"project_key": PROJECT},
            {"exclusive_analysis": True},
            {"project_key": "another-project", "exclusive_analysis": True},
        ],
    )
    def test_uncorrelated_snapshot_can_never_be_fixed(self, verifier_kwargs):
        client = FakeIssuesClient()
        verifier = SonarIssueVerifier(client, **verifier_kwargs)
        issue = SonarIssue(
            key="AX1",
            rule="python:S108",
            severity="MAJOR",
            issue_type="CODE_SMELL",
            message="define a constant",
            component=f"{PROJECT}:src/app.py",
            line=3,
            status="OPEN",
        )
        completion_value = make_completion()
        verification = verifier.verify(issue, completion=completion_value)
        result = decide_with(verification, completion_value)
        assert result.is_fixed is False
        assert result.status is IssueFinalStatus.REVIEW_REQUIRED

    def test_wrong_component_snapshot_can_never_be_fixed(self):
        client = FakeIssuesClient()
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        completion_value = make_completion(component_key="another-project")
        verification = verifier.verify(
            SonarIssue(
                key="AX1",
                rule="python:S108",
                severity="MAJOR",
                issue_type="CODE_SMELL",
                message="define a constant",
                component=f"{PROJECT}:src/app.py",
                line=3,
                status="OPEN",
            ),
            completion=completion_value,
        )
        result = decide_with(verification, completion_value)
        assert result.is_fixed is False
