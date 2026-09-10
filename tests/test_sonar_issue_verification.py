"""T18 tests: verifying the original issue against the new analysis.

All SonarQube interaction is faked; the identity ladder, ambiguity handling and
malformed-response behaviour are exercised directly. No real server is used and
no SonarQube state is mutated.
"""

import dataclasses
import os

import pytest

from analysis_correlation import AnalysisCorrelation
from models import SonarIssue
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_client import SonarQubeError
from sonar_issue_verification import (
    IssueMatchType,
    SonarIssueVerificationError,
    SonarIssueVerificationResult,
    SonarIssueVerifier,
    _page_is_complete,
)

PROJECT = "demo"
DEFAULT_RULE = "python:S108"
DEFAULT_FILE = "src/app.py"


class FakeIssuesClient:
    """Returns a canned ``get_open_issues()`` payload or raises."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_open_issues(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def raw_issue(
    key="AX1",
    rule=DEFAULT_RULE,
    file=DEFAULT_FILE,
    line=3,
    message="define a constant",
    severity="MAJOR",
    issue_type="CODE_SMELL",
    component=None,
):
    return {
        "key": key,
        "rule": rule,
        "type": issue_type,
        "severity": severity,
        "message": message,
        "component": component if component is not None else f"{PROJECT}:{file}",
        "line": line,
    }


def payload(issues=(), total=None):
    issues = list(issues)
    return {
        "total": len(issues) if total is None else total,
        "issues": issues,
    }


def completion(
    status=SonarAnalysisState.SUCCESS,
    *,
    task_id="AY1",
    analysis_id="A-1",
    component_key=PROJECT,
):
    """A T17 completion carrying the metadata T18 correlates against."""
    return SonarAnalysisCompletion(
        status=status,
        task_id=task_id,
        elapsed_seconds=1.0,
        poll_count=1,
        reason="fixture completion",
        failure_reason=None if status is SonarAnalysisState.SUCCESS else "failed",
        analysis_id=analysis_id,
        component_key=component_key,
    )


def original_issue(
    key="AX1", rule=DEFAULT_RULE, file=DEFAULT_FILE, line=3, message="define a constant"
):
    return SonarIssue(
        key=key,
        rule=rule,
        severity="MAJOR",
        issue_type="CODE_SMELL",
        message=message,
        component=f"{PROJECT}:{file}",
        line=line,
        status="OPEN",
    )


def correlated_verifier(client):
    """A verifier wired for a correlated analysis (the green path)."""
    return SonarIssueVerifier(
        client,
        project_key=PROJECT,
        exclusive_analysis=True,
    )


def verify(payload_value, issue=None, *, correlated=True, **kwargs):
    client = FakeIssuesClient(payload=payload_value)
    kwargs.setdefault("project_key", PROJECT)
    kwargs.setdefault("exclusive_analysis", correlated)
    verifier = SonarIssueVerifier(client, **kwargs)
    kwargs_completion = {"completion": completion()} if correlated else {}
    return (
        verifier.verify(
            issue if issue is not None else original_issue(), **kwargs_completion
        ),
        client,
    )


class TestKeyIdentity:
    def test_original_key_still_open_is_present(self):
        result, client = verify(payload([raw_issue(key="AX1")]))
        assert isinstance(result, SonarIssueVerificationResult)
        assert result.match_type is IssueMatchType.KEY
        assert result.is_present is True
        assert result.absent is False
        assert result.identity_reliable is True
        assert result.matching_current_issue is not None
        assert result.match_keys == ("AX1",)
        assert client.calls == 1

    def test_original_key_absent_and_unrelated_issues_only(self):
        result, _client = verify(
            payload([raw_issue(key="ZZ9", rule="python:S106", file="src/other.py")])
        )
        assert result.match_type is IssueMatchType.KEY
        assert result.is_present is False
        assert result.identity_reliable is True
        assert result.matching_current_issue is None
        assert result.matches == ()

    def test_empty_current_issue_list_is_a_reliable_absence(self):
        result, _client = verify(payload([]))
        assert result.is_present is False
        assert result.identity_reliable is True
        assert result.page_complete is True
        assert result.retrieved_count == 0

    def test_same_message_different_file_is_not_the_original_issue(self):
        result, _client = verify(
            payload(
                [
                    raw_issue(
                        key="AX2",
                        file="src/other.py",
                        message="define a constant",
                    )
                ]
            )
        )
        assert result.is_present is False
        assert result.identity_reliable is True

    def test_same_rule_different_file_is_not_the_original_issue(self):
        result, _client = verify(
            payload([raw_issue(key="AX3", file="docs/readme.md")])
        )
        assert result.is_present is False
        assert result.identity_reliable is True

    def test_absent_original_key_with_equivalent_issue_is_ambiguous(self):
        result, _client = verify(payload([raw_issue(key="AX2")]))
        assert result.is_present is True
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE
        assert result.identity_reliable is False
        assert result.match_keys == ("AX2",)

    def test_absent_original_key_with_same_rule_and_file_but_other_line(self):
        result, _client = verify(payload([raw_issue(key="AX2", line=99)]))
        assert result.is_present is True
        assert result.match_type is IssueMatchType.COMPONENT_RULE
        assert result.identity_reliable is False

    def test_component_prefix_and_separators_are_normalized(self):
        result, _client = verify(
            payload([raw_issue(key="AX5", component=r"demo:src\app.py")]),
            issue=original_issue(key=""),
        )
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE
        assert result.is_present is True


class TestFallbackIdentityWithoutAKey:
    def test_unique_rule_file_line_match_is_present(self):
        result, _client = verify(
            payload([raw_issue(key="AX9")]), issue=original_issue(key="")
        )
        assert result.original_issue_key == ""
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE
        assert result.is_present is True
        assert result.identity_reliable is True

    def test_multiple_rule_file_line_matches_are_ambiguous(self):
        result, _client = verify(
            payload([raw_issue(key="AX9"), raw_issue(key="AX8")]),
            issue=original_issue(key=""),
        )
        assert result.is_present is True
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE
        assert result.identity_reliable is False
        assert result.matching_current_issue is None
        assert len(result.matches) == 2

    def test_unique_rule_file_match_without_line_is_present(self):
        result, _client = verify(
            payload([raw_issue(key="AX9", line=None)]),
            issue=original_issue(key="", line=None),
        )
        assert result.match_type is IssueMatchType.COMPONENT_RULE
        assert result.is_present is True
        assert result.identity_reliable is True

    def test_multiple_rule_file_matches_without_line_are_ambiguous(self):
        result, _client = verify(
            payload([raw_issue(key="AX9", line=None), raw_issue(key="AX8", line=None)]),
            issue=original_issue(key="", line=None),
        )
        assert result.match_type is IssueMatchType.COMPONENT_RULE
        assert result.is_present is True
        assert result.identity_reliable is False

    def test_no_key_and_no_match_is_unreliable_absence(self):
        result, _client = verify(payload([]), issue=original_issue(key=""))
        assert result.match_type is IssueMatchType.NONE
        assert result.is_present is False
        assert result.identity_reliable is False

    @pytest.mark.parametrize("bad_key", ["   ", "a\nb", "x" * 401, "tab\there"])
    def test_unusable_keys_fall_back_to_location(self, bad_key):
        result, _client = verify(
            payload([raw_issue(key="AX9")]), issue=original_issue(key=bad_key)
        )
        assert result.original_issue_key == ""
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE
        assert result.is_present is True


class TestRetrievalFailures:
    def test_sonar_error_is_reported_not_raised(self):
        verifier = SonarIssueVerifier(
            FakeIssuesClient(error=SonarQubeError("connection refused"))
        )
        result = verifier.verify(original_issue())
        assert result.retrieval_succeeded is False
        assert result.is_present is False
        assert result.identity_reliable is False
        assert result.match_type is IssueMatchType.NONE
        assert "connection refused" in result.error

    def test_retrieval_error_is_credential_redacted(self):
        verifier = SonarIssueVerifier(
            FakeIssuesClient(
                error=SonarQubeError("GET https://bob:hunter2@example.com/x failed")
            )
        )
        result = verifier.verify(original_issue())
        assert "hunter2" not in result.error
        assert "***@example.com" in result.error

    def test_unexpected_client_exception_is_contained(self):
        verifier = SonarIssueVerifier(FakeIssuesClient(error=RuntimeError("boom")))
        result = verifier.verify(original_issue())
        assert result.retrieval_succeeded is False
        assert "RuntimeError" in result.error

    @pytest.mark.parametrize(
        "bad_payload",
        [None, [], "text", 42, {}, {"issues": None}, {"issues": {"key": "AX1"}}],
    )
    def test_malformed_payloads_are_contained(self, bad_payload):
        result, _client = verify(bad_payload)
        assert result.retrieval_succeeded is False
        assert result.is_present is False
        assert result.identity_reliable is False
        assert result.error is not None

    def test_unusable_entries_are_skipped_and_counted(self):
        result, _client = verify(payload(["oops", 7, raw_issue(key="AX1")], total=3))
        assert result.retrieval_succeeded is True
        assert result.skipped_entries == 2
        assert result.retrieved_count == 3
        assert result.is_present is True


class TestCompletenessAndFiltering:
    def test_truncated_page_makes_absence_unreliable(self):
        result, _client = verify(
            payload([raw_issue(key="ZZ9", file="src/other.py")], total=150)
        )
        assert result.page_complete is False
        assert result.is_present is False
        assert result.identity_reliable is False

    def test_truncated_page_still_reports_a_present_issue_reliably(self):
        result, _client = verify(payload([raw_issue(key="AX1")], total=150))
        assert result.page_complete is False
        assert result.is_present is True
        assert result.identity_reliable is True

    def test_non_numeric_total_is_not_treated_as_complete(self):
        result, _client = verify({"issues": [raw_issue(key="AX1")], "total": "many"})
        assert result.page_complete is False
        assert result.reported_total is None

    def test_missing_total_is_incomplete(self):
        result, _client = verify({"issues": [raw_issue(key="ZZ9", file="src/o.py")]})
        assert result.page_complete is False
        assert result.reported_total is None
        assert result.is_present is False
        assert result.identity_reliable is False

    @pytest.mark.parametrize("total", [None, "150", "unknown", 1.5, True, False, [], {}])
    def test_malformed_totals_are_incomplete(self, total):
        result, _client = verify({"issues": [raw_issue(key="ZZ9", file="src/o.py")], "total": total})
        assert result.page_complete is False
        assert result.reported_total is None
        assert result.identity_reliable is False

    def test_negative_total_is_incomplete(self):
        result, _client = verify(payload([raw_issue(key="ZZ9", file="src/o.py")], total=-1))
        assert result.page_complete is False
        assert result.identity_reliable is False

    def test_total_greater_than_retrieved_is_incomplete(self):
        result, _client = verify(
            payload([raw_issue(key="ZZ9", file="src/o.py")], total=2)
        )
        assert result.page_complete is False
        assert result.identity_reliable is False

    def test_zero_total_with_no_issues_is_complete(self):
        result, _client = verify(payload([], total=0))
        assert result.page_complete is True
        assert result.reported_total == 0
        assert result.is_present is False
        assert result.identity_reliable is True

    def test_total_equal_to_retrieved_is_complete(self):
        result, _client = verify(payload([raw_issue(key="AX1")], total=1))
        assert result.page_complete is True
        assert result.reported_total == 1

    def test_paginated_page_holding_the_full_total_is_complete(self):
        issues = [raw_issue(key=f"AX{i}") for i in range(5)]
        result, _client = verify(payload(issues, total=5))
        assert result.page_complete is True
        assert result.retrieved_count == 5

    def test_truncated_page_is_incomplete_and_absence_unreliable(self):
        issues = [raw_issue(key=f"ZZ{i}", file="src/o.py") for i in range(3)]
        result, _client = verify(payload(issues, total=250))
        assert result.page_complete is False
        assert result.is_present is False
        assert result.identity_reliable is False
        assert result.reliable_absence is False

    def test_incomplete_page_still_reports_a_present_issue_reliably(self):
        result, _client = verify({"issues": [raw_issue(key="AX1")]})
        assert result.page_complete is False
        assert result.is_present is True
        assert result.identity_reliable is True


class TestPageCompletenessPolicy:
    """The fail-closed completeness policy itself, including impossible inputs."""

    @pytest.mark.parametrize(
        "total, retrieved, expected",
        [
            (0, 0, True),
            (1, 1, True),
            (5, 5, True),
            (5, 7, True),
            (5, 4, False),
            (0, 1, True),
            (-1, 5, False),
            (None, 5, False),
            ("5", 5, False),
            (True, 5, False),
            (False, 5, False),
            (1.5, 5, False),
        ],
    )
    def test_policy_is_fail_closed(self, total, retrieved, expected):
        assert _page_is_complete(total, retrieved) is expected

    @pytest.mark.parametrize("retrieved", [-1, "5", None, 1.5])
    def test_impossible_retrieved_counts_are_never_complete(self, retrieved):
        assert _page_is_complete(1, retrieved) is False


    def test_t03_filters_are_off_by_default_so_nothing_hides(self):
        """A still-open issue is never filtered away by default."""
        info_issue = raw_issue(key="AX1", severity="INFO", issue_type="VULNERABILITY")
        result, _client = verify(payload([info_issue]))
        assert result.current_issues != ()
        assert result.is_present is True

    def test_opt_in_filter_drops_issues_outside_the_t03_subset(self):
        info_issue = raw_issue(key="AX1", severity="INFO", issue_type="VULNERABILITY")
        result, _client = verify(payload([info_issue]), apply_issue_filter=True)
        assert result.current_issues == ()
        assert result.is_present is False

    def test_opt_in_filter_keeps_allowed_issues(self):
        result, _client = verify(
            payload([raw_issue(key="AX1"), raw_issue(key="BX2", severity="INFO")]),
            apply_issue_filter=True,
        )
        assert [issue.key for issue in result.current_issues] == ["AX1"]
        assert result.is_present is True

    @pytest.mark.skipif(
        os.name != "nt", reason="path case folding only happens on Windows"
    )
    def test_case_differences_are_folded_where_the_platform_does(self):
        result, _client = verify(
            payload([raw_issue(key="AX2", file="SRC/APP.PY")])
        )
        assert result.is_present is True
        assert result.match_type is IssueMatchType.COMPONENT_RULE_LINE


class TestVerifierContract:
    def test_client_without_issue_support_is_rejected(self):
        with pytest.raises(SonarIssueVerificationError, match="get_open_issues"):
            SonarIssueVerifier(object())

    def test_missing_client_is_rejected(self):
        with pytest.raises(SonarIssueVerificationError, match="get_open_issues"):
            SonarIssueVerifier(None)

    def test_missing_original_issue_is_rejected(self):
        with pytest.raises(SonarIssueVerificationError, match="original issue"):
            SonarIssueVerifier(FakeIssuesClient(payload([]))).verify(None)

    def test_verification_retrieves_issues_exactly_once(self):
        result, client = verify(payload([raw_issue(key="AX1")]))
        assert client.calls == 1
        assert result.current_issues == (result.matching_current_issue,)

    def test_result_is_frozen(self):
        result, _client = verify(payload([raw_issue(key="AX1")]))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.is_present = False

    def test_as_dict_omits_issue_messages(self):
        secret = "define a constant with hunter2@example.com"
        result, _client = verify(payload([raw_issue(key="AX1", message=secret)]))
        payload_dict = result.as_dict()
        assert "hunter2" not in str(payload_dict)
        assert payload_dict["match_keys"] == ["AX1"]
        assert payload_dict["match_type"] == "key"
        assert payload_dict["retrieval_succeeded"] is True
        assert payload_dict["error"] is None

    def test_reason_text_joins_all_reasons(self):
        result, _client = verify(payload([]))
        assert result.reason_text.startswith(result.reasons[0])
        assert len(result.reasons) > 1
        assert result.reason == "The original issue is not reported as open by the new analysis."

    def test_ambiguity_reason_mentions_review(self):
        result, _client = verify(payload([raw_issue(key="AX2")]))
        assert "review" in result.reason.lower()

    def test_unknown_absence_reason_mentions_review(self):
        result, _client = verify(payload([]), issue=original_issue(key=""))
        assert "review" in result.reason.lower()


class TestAnalysisCorrelation:
    """T18 must expose whether its snapshot is attributable to the analysis."""

    def test_exact_correlation_succeeds(self):
        result, _client = verify(payload([]))
        assert result.correlation is AnalysisCorrelation.CORRELATED
        assert result.analysis_correlated is True
        assert result.reliable_absence is True

    def test_absent_correlation_metadata_is_unknown(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        result = verifier.verify(original_issue())
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert result.analysis_correlated is False
        assert result.reliable_absence is False
        assert "No analysis completion" in result.correlation_reason

    def test_missing_analysis_id_is_unknown(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        result = verifier.verify(
            original_issue(), completion=completion(analysis_id=None)
        )
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "analysisId" in result.correlation_reason
        assert result.reliable_absence is False

    def test_missing_task_id_is_unknown(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        result = verifier.verify(original_issue(), completion=completion(task_id=None))
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "task id" in result.correlation_reason

    def test_wrong_analysis_context_is_not_correlated(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        result = verifier.verify(
            original_issue(), completion=completion(component_key="other-project")
        )
        assert result.correlation is AnalysisCorrelation.NOT_CORRELATED
        assert "other-project" in result.correlation_reason
        assert result.reliable_absence is False

    def test_failed_analysis_cannot_be_correlated(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(
            client, project_key=PROJECT, exclusive_analysis=True
        )
        result = verifier.verify(
            original_issue(), completion=completion(SonarAnalysisState.FAILED)
        )
        assert result.correlation is AnalysisCorrelation.NOT_CORRELATED

    def test_concurrent_analysis_deployment_is_unknown(self):
        """Without the single-analysis precondition the snapshot is unattributable."""
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(client, project_key=PROJECT)
        result = verifier.verify(original_issue(), completion=completion())
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert "single-analysis precondition" in result.correlation_reason
        assert result.reliable_absence is False

    def test_correlation_reason_is_included_in_the_trail(self):
        client = FakeIssuesClient(payload=payload([]))
        verifier = SonarIssueVerifier(client, project_key=PROJECT)
        result = verifier.verify(original_issue(), completion=completion())
        assert result.reasons[0] == result.correlation_reason

    def test_correlation_is_reported_in_the_summary(self):
        result, _client = verify(payload([]), correlated=False)
        summary = result.as_dict()
        assert summary["correlation"] == "unknown"
        assert summary["analysis_correlated"] is False
        assert summary["reliable_absence"] is False

    def test_present_issue_is_reported_even_when_uncorrelated(self):
        result, _client = verify(payload([raw_issue(key="AX1")]), correlated=False)
        assert result.correlation is AnalysisCorrelation.UNKNOWN
        assert result.is_present is True
        assert result.identity_reliable is True

    def test_retrieval_failure_still_records_the_correlation_verdict(self):
        client = FakeIssuesClient(error=SonarQubeError("boom"))
        verifier = correlated_verifier(client)
        result = verifier.verify(original_issue(), completion=completion())
        assert result.retrieval_succeeded is False
        assert result.correlation is AnalysisCorrelation.CORRELATED
        assert result.reliable_absence is False

