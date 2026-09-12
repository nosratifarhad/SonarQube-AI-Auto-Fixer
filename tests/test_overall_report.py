"""T22 overall report tests - aggregation, fail-closed gates and determinism.

Every test here uses the **production** DTOs: real T19 ``IssueStatusResult``
objects (built by ``determine_issue_status``), real T20 ``CommitResult`` objects
and real T21 ``PushResult`` objects (see ``tests/t22_fixtures.py``). The module
under test is pure, so no Git command, no network and no repository is touched -
the last test in the file proves that against a real temporary repository.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

import overall_report as module
from commit_message import CommitMessage
from commit_policy import GateStatus
from git_commit import CommitStatus, GitCommitExecutor
from git_push import GitPushExecutor, PushStatus
from issue_status import IssueFinalStatus
from push_policy import PushPolicyConfig, RemoteObservation
from worktree_baseline import WorktreeBaselineInspector, attribute_changes
from overall_report import (
    COMMIT_STATUS_ORDER,
    GATE_CATALOGUE,
    ISSUE_STATUS_ORDER,
    PUSH_STATUS_ORDER,
    ExpectedEndState,
    IssueLifecycleInput,
    IssueOutcome,
    OverallReportError,
    OverallReportPhase,
    OverallReportPolicy,
    OverallStatus,
    ReportDecision,
    build_overall_report,
    resolve_overall_status,
    serialize_report,
    verify_overall_report,
)
from t22_fixtures import (
    COMMIT,
    ISSUE_KEY,
    PREVIOUS_COMMIT,
    commit_result,
    commit_view,
    happy_entry,
    issue_entry,
    push_result,
    push_view,
    review_needed_entry,
    t19_result,
)
from t20_fixtures import TARGET, AGENT_BRANCH, assert_fixed, git_run, green_status
from t21_fixtures import REMOTE, snapshot

#: The exact phase sequence a complete run must walk.
COMPLETE_PHASES = (
    OverallReportPhase.INPUT,
    OverallReportPhase.INPUT_VALIDATED,
    OverallReportPhase.ISSUE_RESULTS_VALIDATED,
    OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
    OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED,
    OverallReportPhase.COMMIT_OUTCOMES_AGGREGATED,
    OverallReportPhase.PUSH_OUTCOMES_AGGREGATED,
    OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    OverallReportPhase.OVERALL_STATUS_RESOLVED,
    OverallReportPhase.REPORT_BUILT,
    OverallReportPhase.REPORT_VERIFIED,
    OverallReportPhase.COMPLETE,
)

#: The statuses T19 can report.
T19_STATUSES = tuple(status.value for status in IssueFinalStatus)
#: The statuses T20 can report.
T20_STATUSES = tuple(status.value for status in CommitStatus)
#: The statuses T21 can report.
T21_STATUSES = tuple(status.value for status in PushStatus)

#: A secret-shaped value that upstream records carry in free-form fields.
SECRET = "hunter2-hunter2-hunter2"
#: A credential-bearing remote URL that upstream records carry.
CREDENTIAL_URL = "https://user:token@example.invalid/repo.git"


def gate(report, gate_id):
    """The recorded verdict for ``gate_id``."""
    return report.state.gate(gate_id)


def failed_gate_ids(report):
    """The ids of every failed gate, in catalogue order."""
    return tuple(item.gate_id for item in report.state.failed_gates)


def committed(*, status="committed", **kwargs):
    """A T20 result for one status (``committed`` unless told otherwise)."""
    return commit_result(status=CommitStatus(status), **kwargs)


def pushed(*, status="pushed", **kwargs):
    """A T21 result for one status (``pushed`` unless told otherwise)."""
    return push_result(status=PushStatus(status), **kwargs)


class TestBasicAggregation:
    """A - one, many, none and mixed outcomes."""

    def test_empty_input_is_empty_and_valid(self):
        report = build_overall_report(entries=())
        assert report.status is OverallStatus.EMPTY
        assert report.is_valid is True
        assert report.input_entries == 0
        assert report.total_issues == 0
        assert report.needs_attention is False
        assert report.issue_outcomes == ()
        assert report.state.phase is OverallReportPhase.COMPLETE
        assert verify_overall_report(report) == ()

    def test_empty_input_is_never_success(self):
        report = build_overall_report(entries=())
        assert report.status is not OverallStatus.SUCCESS
        assert report.is_success is False
        assert report.is_empty is True

    def test_one_successful_issue(self):
        report = build_overall_report(entries=[happy_entry()])
        assert report.status is OverallStatus.SUCCESS
        assert report.is_valid is True
        assert report.total_issues == 1
        assert report.successful_issues == 1
        assert report.failed_issues == 0
        assert report.review_required_issues == 0
        assert report.input_entries == 1
        assert report.decision is ReportDecision.PROCEED
        assert report.needs_attention is False

    def test_three_successful_issues(self):
        entries = [happy_entry(issue_key=f"AX{i}") for i in range(1, 4)]
        report = build_overall_report(entries=entries)
        assert report.status is OverallStatus.SUCCESS
        assert report.total_issues == 3
        assert report.end_to_end_success_count == 3
        assert report.issue_keys == ("AX1", "AX2", "AX3")

    def test_all_issues_failed_is_failed(self):
        entries = [
            issue_entry(
                issue_key=f"AX{i}", issue_status=t19_result("still-open")
            )
            for i in range(1, 4)
        ]
        report = build_overall_report(entries=entries)
        assert report.status is OverallStatus.FAILED
        assert report.successful_issues == 0
        assert report.failed_issues == 3
        assert report.needs_attention is True
        assert report.issue_counts["still-open"] == 3
        assert verify_overall_report(report) == ()

    def test_mixed_outcomes_give_partial_success(self):
        entries = [
            happy_entry(issue_key="AX1"),
            happy_entry(issue_key="AX2"),
            issue_entry(issue_key="AX3", issue_status=t19_result("still-open")),
            issue_entry(
                issue_key="AX4", issue_status=t19_result("review-required")
            ),
        ]
        report = build_overall_report(entries=entries)
        assert report.status is OverallStatus.PARTIAL_SUCCESS
        assert (report.successful_issues, report.failed_issues) == (2, 1)
        assert report.review_required_issues == 1
        assert report.needs_attention is True
        assert report.end_to_end_review_count == 1
        assert verify_overall_report(report) == ()

    def test_partial_success_never_hides_the_review_count(self):
        entries = [
            happy_entry(issue_key="AX1"),
            issue_entry(
                issue_key="AX2",
                commit=commit_result(issue_key="AX2"),
                push=pushed(status="push-unverified"),
            ),
        ]
        report = build_overall_report(entries=entries)
        assert report.status is OverallStatus.PARTIAL_SUCCESS
        assert report.review_required_issues == 1
        assert report.issue_outcomes[1].outcome is IssueOutcome.REVIEW_REQUIRED
        assert report.issue_outcomes[1].t21_status == "push-unverified"

    def test_a_single_failed_entry_is_failed(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result("tests-failed"))]
        )
        assert report.status is OverallStatus.FAILED
        assert report.end_to_end_failure_count == 1

    def test_outcome_buckets_are_mutually_exclusive(self):
        entries = [
            happy_entry(issue_key="AX1"),
            issue_entry(issue_key="AX2", issue_status=t19_result("still-open")),
            review_needed_entry(issue_key="AX3"),
        ]
        report = build_overall_report(entries=entries)
        assert (
            report.successful_issues
            + report.failed_issues
            + report.review_required_issues
            == report.total_issues
            == report.input_entries
            == 3
        )



class TestT19StatusCounts:
    """B - every T19 status is counted exactly, and preserved verbatim."""

    @pytest.mark.parametrize("status", T19_STATUSES)
    def test_each_status_is_counted_once(self, status):
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result(status))]
        )
        assert report.issue_counts[status] == 1
        assert sum(report.issue_counts.values()) == report.total_issues == 1
        assert report.issue_counts == {
            name: (1 if name == status else 0) for name in ISSUE_STATUS_ORDER
        }
        assert report.issue_outcomes[0].t19_status == status
        assert verify_overall_report(report) == ()

    def test_every_status_at_once_keeps_every_count(self):
        entries = [
            issue_entry(issue_key=f"AX{i}", issue_status=t19_result(status))
            for i, status in enumerate(T19_STATUSES, start=1)
        ]
        report = build_overall_report(entries=entries)
        assert report.total_issues == len(T19_STATUSES)
        assert report.issue_counts == {
            name: 1 for name in ISSUE_STATUS_ORDER
        }
        assert tuple(
            summary.t19_status for summary in report.issue_outcomes
        ) == T19_STATUSES
        assert sum(report.issue_counts.values()) == report.total_issues

    def test_the_status_vocabulary_is_the_production_one(self):
        assert ISSUE_STATUS_ORDER == T19_STATUSES
        assert COMMIT_STATUS_ORDER == T20_STATUSES
        assert PUSH_STATUS_ORDER == T21_STATUSES
        report = build_overall_report(entries=[happy_entry()])
        assert set(report.issue_counts) == set(T19_STATUSES)
        assert set(report.commit_counts) == set(T20_STATUSES)
        assert set(report.push_counts) == set(T21_STATUSES)

    def test_a_non_fixed_status_is_never_a_success(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result("analysis-failed"))]
        )
        assert report.issue_outcomes[0].outcome is IssueOutcome.NOT_FIXED
        assert report.issue_outcomes[0].issue_fixed is False
        assert report.issue_outcomes[0].end_to_end_success is False
        assert report.status is OverallStatus.FAILED
        assert report.successful_issues == 0

    def test_a_review_required_status_is_never_a_failure(self):
        report = build_overall_report(entries=[review_needed_entry()])
        assert report.issue_outcomes[0].outcome is IssueOutcome.REVIEW_REQUIRED
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.failed_issues == 0
        assert report.review_required_issues == 1
        assert report.is_valid is True

    def test_not_executed_stages_are_counted_separately(self):
        report = build_overall_report(entries=[happy_entry()])
        assert report.commit_result_count == 1
        assert report.commit_not_executed_count == 0
        assert report.push_result_count == 1
        assert report.push_not_executed_count == 0
        assert sum(report.commit_counts.values()) == report.commit_result_count
        assert sum(report.push_counts.values()) == report.push_result_count

    def test_missing_stages_are_not_counted_as_results(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result("still-open"))]
        )
        assert report.commit_result_count == 0
        assert report.commit_not_executed_count == 1
        assert report.push_result_count == 0
        assert report.push_not_executed_count == 1
        assert sum(report.commit_counts.values()) == 0
        assert sum(report.push_counts.values()) == 0
        assert set(report.commit_counts.values()) == {0}



class TestT20StatusCounts:
    """C - every T20 status, exactly, on top of a real ``FIXED`` T19 result."""

    @pytest.mark.parametrize("status", T20_STATUSES)
    def test_each_status_is_counted_once(self, status):
        report = build_overall_report(
            entries=[issue_entry(commit=committed(status=status))]
        )
        assert report.commit_counts[status] == 1
        assert sum(report.commit_counts.values()) == 1
        assert report.commit_result_count == 1
        assert report.issue_outcomes[0].t20_status == status
        assert verify_overall_report(report) == ()

    @pytest.mark.parametrize(
        "status, outcome",
        (
            ("committed", IssueOutcome.REVIEW_REQUIRED),
            ("refused", IssueOutcome.NOT_DELIVERED),
            ("commit-failed", IssueOutcome.NOT_DELIVERED),
            ("commit-unverified", IssueOutcome.REVIEW_REQUIRED),
        ),
    )
    def test_each_status_maps_to_the_documented_outcome(self, status, outcome):
        report = build_overall_report(
            entries=[issue_entry(commit=committed(status=status))]
        )
        assert report.issue_outcomes[0].outcome is outcome
        assert report.issue_outcomes[0].change_committed is (
            status == "committed"
        )

    def test_committed_without_a_push_is_review_required(self):
        report = build_overall_report(entries=[issue_entry(commit=committed())])
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.review_required_issues == 1
        assert report.issue_outcomes[0].change_committed is True
        assert report.issue_outcomes[0].change_pushed is False

    def test_commit_unverified_is_never_success(self):
        report = build_overall_report(
            entries=[issue_entry(commit=committed(status="commit-unverified"))]
        )
        assert report.status is not OverallStatus.SUCCESS
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.issue_outcomes[0].outcome is IssueOutcome.REVIEW_REQUIRED
        assert report.needs_attention is True

    def test_commit_failed_needs_attention(self):
        report = build_overall_report(
            entries=[issue_entry(commit=committed(status="commit-failed"))]
        )
        assert report.issue_outcomes[0].outcome is IssueOutcome.NOT_DELIVERED
        assert report.issue_outcomes[0].needs_attention is True


class TestT21StatusCounts:
    """D - every T21 status, exactly, on a ``FIXED`` + ``COMMITTED`` issue."""

    @pytest.mark.parametrize("status", T21_STATUSES)
    def test_each_status_is_counted_once(self, status):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=pushed(status=status))
            ]
        )
        assert report.push_counts[status] == 1
        assert sum(report.push_counts.values()) == 1
        assert report.push_result_count == 1
        assert report.issue_outcomes[0].t21_status == status
        assert verify_overall_report(report) == ()

    @pytest.mark.parametrize(
        "status, outcome",
        (
            ("pushed", IssueOutcome.DELIVERED),
            ("refused", IssueOutcome.NOT_DELIVERED),
            ("push-failed", IssueOutcome.NOT_DELIVERED),
            ("push-unverified", IssueOutcome.REVIEW_REQUIRED),
        ),
    )
    def test_each_status_maps_to_the_documented_outcome(self, status, outcome):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=pushed(status=status))
            ]
        )
        assert report.issue_outcomes[0].outcome is outcome
        assert report.issue_outcomes[0].change_pushed is (status == "pushed")

    @pytest.mark.parametrize(
        "status", ("refused", "push-failed", "push-unverified")
    )
    def test_a_non_pushed_status_is_never_success(self, status):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=pushed(status=status))
            ]
        )
        assert report.status is not OverallStatus.SUCCESS
        assert report.successful_issues == 0
        assert report.issue_outcomes[0].end_to_end_success is False

    def test_push_unverified_is_review_required_not_success(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(), push=pushed(status="push-unverified")
                )
            ]
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.review_required_issues == 1
        assert report.failed_issues == 0
        assert report.issue_outcomes[0].t21_status == "push-unverified"
        assert report.issue_outcomes[0].outcome is IssueOutcome.REVIEW_REQUIRED

    def test_push_failed_needs_attention_but_is_not_review(self):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=pushed(status="push-failed"))
            ]
        )
        assert report.issue_outcomes[0].outcome is IssueOutcome.NOT_DELIVERED
        assert report.issue_outcomes[0].needs_attention is True
        assert report.status is OverallStatus.FAILED
        assert report.needs_attention is True



class TestEndToEndLifecycle:
    """E-K, V - the three stages are never collapsed."""

    def test_fixed_committed_pushed_is_success(self):
        report = build_overall_report(entries=[happy_entry()])
        assert report.status is OverallStatus.SUCCESS
        summary = report.issue_outcomes[0]
        assert (summary.issue_fixed, summary.change_committed) == (True, True)
        assert summary.change_pushed is True
        assert summary.end_to_end_success is True
        assert summary.commit_sha == COMMIT
        assert summary.push_commit == COMMIT
        assert summary.outcome is IssueOutcome.DELIVERED
        assert report.end_to_end_success_count == 1

    def test_the_statuses_are_the_production_ones(self):
        entry = happy_entry()
        report = build_overall_report(entries=[entry])
        summary = report.issue_outcomes[0]
        assert summary.t19_status == entry.issue_status.status.value
        assert summary.t20_status == entry.commit_result.status.value
        assert summary.t21_status == entry.push_result.status.value

    def test_fixed_but_not_committed_is_not_success(self):
        report = build_overall_report(
            entries=[issue_entry(commit=committed(status="refused"))]
        )
        summary = report.issue_outcomes[0]
        assert summary.issue_fixed is True
        assert summary.change_committed is False
        assert summary.change_pushed is False
        assert summary.end_to_end_success is False
        assert summary.outcome is IssueOutcome.NOT_DELIVERED
        assert report.status is OverallStatus.FAILED

    def test_fixed_committed_but_push_failed_is_not_success(self):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=pushed(status="push-failed"))
            ]
        )
        summary = report.issue_outcomes[0]
        assert (summary.issue_fixed, summary.change_committed) == (True, True)
        assert summary.change_pushed is False
        assert summary.end_to_end_success is False
        assert report.status is OverallStatus.FAILED
        assert report.status is not OverallStatus.SUCCESS

    def test_missing_commit_evidence_is_review_required(self):
        report = build_overall_report(entries=[issue_entry()])
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.review_required_issues == 1
        assert report.issue_outcomes[0].t20_status is None
        assert report.issue_outcomes[0].outcome is IssueOutcome.REVIEW_REQUIRED
        assert report.commit_not_executed_count == 1

    def test_missing_push_evidence_is_not_end_to_end_success(self):
        report = build_overall_report(entries=[issue_entry(commit=committed())])
        summary = report.issue_outcomes[0]
        assert summary.t21_status is None
        assert summary.end_to_end_success is False
        assert report.status is not OverallStatus.SUCCESS
        assert report.push_not_executed_count == 1

    def test_caller_cannot_infer_commit_from_a_missing_result(self):
        report = build_overall_report(entries=[issue_entry()])
        assert report.commit_result_count == 0
        assert report.commit_counts["committed"] == 0
        assert report.issue_outcomes[0].change_committed is False

    @pytest.mark.parametrize(
        "end_state",
        (
            ExpectedEndState.PUSHED,
            ExpectedEndState.COMMITTED,
            ExpectedEndState.ISSUE_FIXED,
        ),
    )
    def test_the_policy_decides_the_required_end_state(self, end_state):
        policy = OverallReportPolicy(expected_end_state=end_state)
        fixed_only = build_overall_report(
            entries=[issue_entry()], policy=policy
        )
        committed_only = build_overall_report(
            entries=[issue_entry(commit=committed())], policy=policy
        )
        fully_delivered = build_overall_report(
            entries=[happy_entry()], policy=policy
        )
        assert fully_delivered.status is OverallStatus.SUCCESS
        if end_state is ExpectedEndState.ISSUE_FIXED:
            assert fixed_only.status is OverallStatus.SUCCESS
            assert committed_only.status is OverallStatus.SUCCESS
        elif end_state is ExpectedEndState.COMMITTED:
            assert fixed_only.status is OverallStatus.REVIEW_REQUIRED
            assert committed_only.status is OverallStatus.SUCCESS
        else:
            assert fixed_only.status is OverallStatus.REVIEW_REQUIRED
            assert committed_only.status is OverallStatus.REVIEW_REQUIRED

    def test_the_policy_is_reported_so_the_report_explains_itself(self):
        policy = OverallReportPolicy(ExpectedEndState.COMMITTED)
        report = build_overall_report(
            entries=[issue_entry(commit=committed())], policy=policy
        )
        assert report.policy is policy
        payload = report.as_dict()
        assert payload["policy"] == {
            "expected_end_state": "committed",
            "requires_commit_evidence": True,
            "requires_push_evidence": False,
        }



class TestCrossStageContradictions:
    """L - contradictory T20/T21 evidence fails closed."""

    def test_t20_new_head_and_commit_sha_must_agree(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(commit_sha="c" * 40))]
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert "G10" in failed_gate_ids(report)
        assert report.total_issues == 0
        assert report.issue_outcomes == ()

    def test_t20_is_committed_must_match_its_status(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(is_committed=False))]
        )
        assert "G10" in failed_gate_ids(report)
        assert report.is_valid is False

    def test_t20_commit_must_differ_from_its_previous_head(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(previous_head=COMMIT))]
        )
        assert "G10" in failed_gate_ids(report)

    def test_t20_needs_attention_must_match_its_status(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(needs_attention=True))]
        )
        assert "G10" in failed_gate_ids(report)

    def test_t20_committed_without_a_usable_commit_id(self):
        report = build_overall_report(
            entries=[
                issue_entry(commit=commit_view(new_head=None, commit_sha=None))
            ]
        )
        assert report.is_valid is False
        assert set(failed_gate_ids(report)) & {"G6", "G10"}

    def test_t21_remote_after_must_be_the_expected_commit(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(remote_after_commit="c" * 40),
                )
            ]
        )
        assert "G11" in failed_gate_ids(report)

    def test_t21_verified_push_must_have_moved_the_remote(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(remote_before_commit=COMMIT),
                )
            ]
        )
        assert "G11" in failed_gate_ids(report)

    def test_t21_is_pushed_must_match_its_status(self):
        report = build_overall_report(
            entries=[
                issue_entry(commit=committed(), push=push_view(is_pushed=False))
            ]
        )
        assert "G11" in failed_gate_ids(report)

    def test_t21_refusal_cannot_have_been_attempted(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(status="refused"),
                    push=push_view(
                        status="refused",
                        is_pushed=False,
                        is_refusal=True,
                        push_attempted=True,
                    ),
                )
            ]
        )
        assert "G11" in failed_gate_ids(report)

    def test_t21_refspec_must_match_its_branches(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(refspec="refs/heads/other:refs/heads/other"),
                )
            ]
        )
        assert "G11" in failed_gate_ids(report)


    def test_a_push_of_a_different_commit_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(
                        expected_commit="c" * 40,
                        remote_after_commit="c" * 40,
                    ),
                )
            ]
        )
        assert "G12" in failed_gate_ids(report)
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.successful_issues == 0

    def test_a_push_without_a_verified_commit_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=commit_view(
                        status="commit-failed",
                        new_head=COMMIT,
                        commit_sha=COMMIT,
                    ),
                    push=push_view(),
                )
            ]
        )
        assert "G13" in failed_gate_ids(report)

    def test_a_push_from_another_branch_fails_closed(self):
        repository = dict(commit_view()["repository"])
        repository["branch"] = "ai/sonar-fix/OTHER"
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=commit_view(repository=repository), push=pushed()
                )
            ]
        )
        assert "G13" in failed_gate_ids(report)

    def test_push_evidence_without_a_commit_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    push=push_view(
                        status="refused",
                        push_attempted=False,
                        is_pushed=False,
                        is_refusal=True,
                        remote_after_commit=None,
                    )
                )
            ]
        )
        assert "G14" in failed_gate_ids(report)

    def test_an_attempted_push_without_a_commit_fails_closed(self):
        report = build_overall_report(entries=[issue_entry(push=push_view())])
        assert set(failed_gate_ids(report)) & {"G12", "G14"}
        assert report.is_valid is False
        assert report.successful_issues == 0


    def test_commit_evidence_for_a_non_fixed_issue_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    issue_status=t19_result("still-open"), commit=committed()
                )
            ]
        )
        assert "G14" in failed_gate_ids(report)
        assert report.status is OverallStatus.REVIEW_REQUIRED

    def test_a_commit_message_for_another_issue_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    issue_status=t19_result("fixed"),
                    commit=commit_view(
                        commit_message={
                            "subject": "fix(sonar): OTHER src/app.py",
                            "issue_key": "OTHER",
                            "rule": "python:S1481",
                            "file_path": "src/app.py",
                            "body": [],
                            "text": "fix(sonar): OTHER src/app.py",
                        }
                    ),
                )
            ]
        )
        assert "G14" in failed_gate_ids(report)

    def test_a_correct_commit_message_issue_key_is_accepted(self):
        view = commit_view()
        assert view["commit_message"]["issue_key"] == ISSUE_KEY
        report = build_overall_report(
            entries=[issue_entry(commit=view, push=pushed())]
        )
        assert report.status is OverallStatus.SUCCESS

    def test_an_actually_attempted_failed_push_is_consistent(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(
                        status="push-failed",
                        push_attempted=True,
                        remote_after_commit=None,
                    ),
                )
            ]
        )
        assert report.is_valid is True
        assert report.issue_outcomes[0].outcome is IssueOutcome.NOT_DELIVERED
        assert report.push_counts["push-failed"] == 1

    def test_a_refused_push_with_the_expected_commit_is_consistent(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=push_view(
                        status="refused",
                        push_attempted=False,
                        is_pushed=False,
                        is_refusal=True,
                        remote_after_commit=None,
                    ),
                )
            ]
        )
        assert report.is_valid is True
        assert report.issue_outcomes[0].outcome is IssueOutcome.NOT_DELIVERED



class TestUnknownStatuses:
    """M - an unrecognised status is refused, never counted as a failure."""

    def test_an_unknown_t19_status_fails_closed(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status={"status": "banana"})]
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert failed_gate_ids(report) == ("G5",)
        assert report.total_issues == 0
        assert report.issue_counts == {
            name: 0 for name in ISSUE_STATUS_ORDER
        }

    def test_a_t20_status_from_another_vocabulary_fails_closed(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(status="pushed"))]
        )
        assert failed_gate_ids(report) == ("G7",)
        assert report.is_valid is False

    def test_a_t21_status_from_another_vocabulary_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(), push=push_view(status="committed")
                )
            ]
        )
        assert failed_gate_ids(report) == ("G9",)

    def test_a_missing_status_fails_closed(self):
        report = build_overall_report(entries=[issue_entry(issue_status={})])
        assert failed_gate_ids(report) == ("G4",)
        assert report.is_valid is False

    def test_a_padded_status_fails_closed(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status={"status": " fixed "})]
        )
        assert failed_gate_ids(report) == ("G4",)

    def test_a_t19_result_that_contradicts_itself_fails_closed(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status={"status": "fixed", "is_fixed": False})]
        )
        assert failed_gate_ids(report) == ("G4",)

    def test_a_needs_review_flag_that_contradicts_its_status_fails_closed(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    issue_status={"status": "fixed", "needs_review": True}
                )
            ]
        )
        assert failed_gate_ids(report) == ("G4",)

    @pytest.mark.parametrize(
        "status", ("analysis-failed", "tests-failed", "scope-invalid")
    )
    def test_every_status_is_recognised(self, status):
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result(status))]
        )
        assert report.is_valid is True
        assert report.issue_counts[status] == 1



class TestInputAndIdentity:
    """N/O - the input collection and the issue identities."""

    def test_a_duplicate_issue_key_fails_closed(self):
        report = build_overall_report(entries=[happy_entry(), happy_entry()])
        assert failed_gate_ids(report) == ("G3",)
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert report.total_issues == 0
        assert report.input_entries == 2

    @pytest.mark.parametrize(
        "issue_key",
        (
            "",
            " AX1",
            "AX1 ",
            "AX1\t",
            "x" * (module.MAX_ISSUE_KEY_LENGTH + 1),
            "AX 1",
            "AX#1",
            "-AX1",
            "AX@1",
        ),
    )
    def test_an_invalid_issue_key_fails_closed(self, issue_key):
        report = build_overall_report(
            entries=[issue_entry(issue_key=issue_key)]
        )
        assert failed_gate_ids(report) == ("G2",)
        assert report.is_valid is False

    @pytest.mark.parametrize("issue_key", (None, 42, [], {}))
    def test_a_non_text_issue_key_fails_closed(self, issue_key):
        report = build_overall_report(
            entries=[issue_entry(issue_key=issue_key)]
        )
        assert failed_gate_ids(report) == ("G2",)

    def test_a_long_but_valid_key_is_accepted(self):
        key = "a" * module.MAX_ISSUE_KEY_LENGTH
        report = build_overall_report(entries=[happy_entry(issue_key=key)])
        assert report.status is OverallStatus.SUCCESS
        assert report.issue_keys == (key,)

    @pytest.mark.parametrize("value", (None, "AX1", 42, {"AX1": None}))
    def test_an_unusable_input_collection_fails_g1(self, value):
        report = build_overall_report(entries=value)
        assert failed_gate_ids(report) == ("G1",)
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert report.input_entries == 0
        assert report.state.failed_phase is OverallReportPhase.INPUT_VALIDATED

    def test_an_entry_that_is_not_an_entry_fails_g2(self):
        report = build_overall_report(entries=[object()])
        assert failed_gate_ids(report) == ("G2",)

    def test_an_entry_mapping_with_an_unknown_field_fails_g2(self):
        report = build_overall_report(
            entries=[
                {
                    "issue_key": "AX1",
                    "issue_status": t19_result("fixed"),
                    "secret": "nope",
                }
            ]
        )
        assert failed_gate_ids(report) == ("G2",)

    def test_an_entry_mapping_is_accepted(self):
        report = build_overall_report(
            entries=[
                {
                    "issue_key": "AX9",
                    "issue_status": t19_result("fixed"),
                    "commit_result": commit_result(issue_key="AX9"),
                    "push_result": push_result(),
                }
            ]
        )
        assert report.status is OverallStatus.SUCCESS
        assert report.issue_keys == ("AX9",)

    def test_a_tuple_input_is_accepted(self):
        report = build_overall_report(entries=(happy_entry(),))
        assert report.status is OverallStatus.SUCCESS

    def test_a_generator_input_is_accepted(self):
        report = build_overall_report(
            entries=(entry for entry in [happy_entry()])
        )
        assert report.status is OverallStatus.SUCCESS

    def test_the_failed_gates_are_named_in_validation_errors(self):
        report = build_overall_report(entries=[happy_entry(), happy_entry()])
        assert report.validation_errors
        assert any("G3" in error for error in report.validation_errors)



class TestCountInvariants:
    """P - the report validates its own counts and classification."""

    def test_verify_accepts_a_healthy_report(self):
        report = build_overall_report(entries=[happy_entry()])
        assert verify_overall_report(report) == ()

    def test_verify_accepts_a_failure_report(self):
        report = build_overall_report(entries=[happy_entry(), happy_entry()])
        assert report.is_valid is False
        assert report.total_issues == 0
        assert verify_overall_report(report) == ()

    def test_verify_detects_a_broken_outcome_count(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, successful_issues=7)
        problems = verify_overall_report(broken)
        assert any("do not sum to total_issues" in problem for problem in problems)

    def test_verify_detects_a_broken_summary_count(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, issue_outcomes=())
        assert any(
            "issue summaries" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_broken_status_count(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(
            report,
            issue_counts={name: 0 for name in ISSUE_STATUS_ORDER},
        )
        assert any(
            "status counts do not sum" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_missing_status_key(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, commit_counts={"committed": 1})
        assert any(
            "complete status vocabulary" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_broken_not_executed_count(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, commit_not_executed_count=4)
        assert any(
            "T20 executed and not-executed" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_broken_classification(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, status=OverallStatus.FAILED)
        assert any(
            "does not match the outcome counts" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_broken_decision(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, decision=ReportDecision.REVIEW_REQUIRED)
        assert any(
            "decision does not match the overall status" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_unsorted_summaries(self):
        entries = [happy_entry(issue_key="AX1"), happy_entry(issue_key="AX0")]
        report = build_overall_report(entries=entries)
        broken = replace(
            report, issue_outcomes=tuple(reversed(report.issue_outcomes))
        )
        assert any(
            "not sorted" in problem for problem in verify_overall_report(broken)
        )

    def test_verify_detects_a_non_integer_count(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, total_issues="1")
        assert any(
            "not a non-negative integer" in problem
            for problem in verify_overall_report(broken)
        )

    def test_verify_detects_validation_errors_on_a_valid_report(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, validation_errors=("nope",))
        assert any(
            "must not carry validation errors" in problem
            for problem in verify_overall_report(broken)
        )

    def test_a_buggy_resolution_is_caught_by_g18(self, monkeypatch):
        monkeypatch.setattr(
            module, "resolve_overall_status", lambda **kwargs: OverallStatus.SUCCESS
        )
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19_result("still-open"))]
        )
        assert failed_gate_ids(report) == ("G18",)
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert report.state.failed_phase is OverallReportPhase.REPORT_BUILT

    def test_a_buggy_aggregation_is_caught_by_g15(self, monkeypatch):
        monkeypatch.setattr(
            module,
            "_issue_counts",
            lambda facts: {name: 0 for name in ISSUE_STATUS_ORDER},
        )
        report = build_overall_report(entries=[happy_entry()])
        assert failed_gate_ids(report) == ("G15",)
        assert report.state.failed_phase is (
            OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED
        )

    def test_an_unexpected_error_fails_closed(self, monkeypatch):
        def explode(*args, **kwargs):
            raise TypeError("boom")

        monkeypatch.setattr(module, "_summaries", explode)
        report = build_overall_report(entries=[happy_entry()])
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert any(
            "unexpected TypeError" in error
            for error in report.validation_errors
        )
        assert report.total_issues == 0
        assert "boom" not in serialize_report(report)



class TestStatusResolution:
    """The overall-status table, branch by branch."""

    @pytest.mark.parametrize(
        "kwargs, expected",
        (
            (dict(is_valid=False, input_entries=3, total_issues=0,
                  successful_issues=0, failed_issues=0,
                  review_required_issues=0), OverallStatus.REVIEW_REQUIRED),
            (dict(is_valid=True, input_entries=0, total_issues=0,
                  successful_issues=0, failed_issues=0,
                  review_required_issues=0), OverallStatus.EMPTY),
            (dict(is_valid=True, input_entries=2, total_issues=2,
                  successful_issues=2, failed_issues=0,
                  review_required_issues=0), OverallStatus.SUCCESS),
            (dict(is_valid=True, input_entries=2, total_issues=2,
                  successful_issues=1, failed_issues=1,
                  review_required_issues=0), OverallStatus.PARTIAL_SUCCESS),
            (dict(is_valid=True, input_entries=2, total_issues=2,
                  successful_issues=1, failed_issues=0,
                  review_required_issues=1), OverallStatus.PARTIAL_SUCCESS),
            (dict(is_valid=True, input_entries=1, total_issues=1,
                  successful_issues=0, failed_issues=0,
                  review_required_issues=1), OverallStatus.REVIEW_REQUIRED),
            (dict(is_valid=True, input_entries=1, total_issues=1,
                  successful_issues=0, failed_issues=1,
                  review_required_issues=0), OverallStatus.FAILED),
        ),
    )
    def test_every_branch(self, kwargs, expected):
        assert resolve_overall_status(**kwargs) is expected

    def test_an_invalid_report_is_never_success(self):
        status = resolve_overall_status(
            is_valid=False,
            input_entries=5,
            total_issues=5,
            successful_issues=5,
            failed_issues=0,
            review_required_issues=0,
        )
        assert status is OverallStatus.REVIEW_REQUIRED


class TestCallerErrors:
    """A caller error raises; an evidence problem fails closed."""

    def test_a_wrong_policy_type_raises(self):
        with pytest.raises(OverallReportError) as excinfo:
            build_overall_report(entries=(), policy="pushed")
        assert "OverallReportPolicy" in str(excinfo.value)

    @pytest.mark.parametrize("value", ("secret", b"secret"))
    def test_a_bare_secret_string_raises(self, value):
        with pytest.raises(OverallReportError):
            build_overall_report(
                entries=[happy_entry()], forbidden_secrets=value
            )

    def test_a_non_iterable_secret_collection_raises(self):
        with pytest.raises(OverallReportError):
            build_overall_report(entries=[], forbidden_secrets=42)

    def test_a_usable_secret_collection_is_accepted(self):
        report = build_overall_report(
            entries=[happy_entry()], forbidden_secrets=["s3cret", "other"]
        )
        assert report.status is OverallStatus.SUCCESS


class TestDeterminismAndOrdering:
    """Q/R - the same input always produces the same report."""

    def make_entries(self, order):
        builders = {
            "AX1": lambda: happy_entry(issue_key="AX1"),
            "AX2": lambda: issue_entry(
                issue_key="AX2", issue_status=t19_result("still-open")
            ),
            "AX3": lambda: issue_entry(
                issue_key="AX3",
                commit=commit_result(issue_key="AX3"),
                push=pushed(status="push-unverified"),
            ),
        }
        return [builders[key]() for key in order]

    def test_the_same_input_gives_the_same_report(self):
        entries = self.make_entries(("AX3", "AX1", "AX2"))
        first = build_overall_report(entries=entries)
        second = build_overall_report(entries=entries)
        assert first.as_dict() == second.as_dict()
        assert serialize_report(first) == serialize_report(second)

    def test_the_input_order_does_not_change_the_report(self):
        one = build_overall_report(entries=self.make_entries(("AX1", "AX2", "AX3")))
        two = build_overall_report(entries=self.make_entries(("AX3", "AX2", "AX1")))
        assert one.as_dict() == two.as_dict()
        assert serialize_report(one) == serialize_report(two)

    def test_the_summaries_are_sorted_by_issue_key(self):
        report = build_overall_report(
            entries=self.make_entries(("AX3", "AX1", "AX2"))
        )
        assert report.issue_keys == ("AX1", "AX2", "AX3")

    def test_a_failure_report_is_deterministic_too(self):
        entries = [happy_entry(), happy_entry()]
        first = build_overall_report(entries=entries)
        second = build_overall_report(entries=entries)
        assert first.as_dict() == second.as_dict()
        assert serialize_report(first) == serialize_report(second)

    def test_the_reasons_are_deterministic(self):
        entries = self.make_entries(("AX1", "AX2", "AX3"))
        first = build_overall_report(entries=entries)
        second = build_overall_report(entries=entries)
        assert first.reasons == second.reasons
        assert first.reasons[0].startswith("Aggregated 3 issue")
        assert first.validation_errors == second.validation_errors == ()



class TestSecretSafety:
    """S - no upstream secret ever reaches the report."""

    def test_upstream_secrets_in_free_form_fields_are_never_copied(self):
        t19 = replace(
            t19_result("fixed"),
            reason=f"token={SECRET}",
            reasons=(f"Authorization: Bearer {SECRET}", "note"),
        )
        t20 = replace(
            commit_result(),
            reason=f"password={SECRET}",
            commit_message=CommitMessage(
                subject=f"fix(sonar): {SECRET}",
                issue_key=ISSUE_KEY,
                rule="python:S1481",
                file_path="src/app.py",
                body=(SECRET,),
            ),
        )
        t21 = replace(
            push_result(),
            reason=f"{SECRET}",
            remote=RemoteObservation(
                name="origin",
                exists=True,
                urls=(CREDENTIAL_URL,),
                fetch_url=CREDENTIAL_URL,
                url_userinfo=True,
                branch_present=True,
            ),
        )
        report = build_overall_report(
            entries=[issue_entry(issue_status=t19, commit=t20, push=t21)]
        )
        assert report.status is OverallStatus.SUCCESS
        text = serialize_report(report)
        assert SECRET not in text
        assert CREDENTIAL_URL not in text
        assert "user:token" not in text
        for line in report.reasons + report.validation_errors:
            assert SECRET not in line
        assert SECRET not in json.dumps(report.as_dict())

    def test_the_configured_secrets_are_scanned_for(self):
        report = build_overall_report(
            entries=[happy_entry()], forbidden_secrets=["s3cret", SECRET]
        )
        assert report.status is OverallStatus.SUCCESS
        assert verify_overall_report(report) == ()

    def test_a_secret_inside_a_projected_field_fails_closed(self):
        report = build_overall_report(
            entries=[happy_entry()], forbidden_secrets=[COMMIT]
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert failed_gate_ids(report) == ("G20",)

    def test_a_secret_issue_key_fails_closed_without_leaking(self):
        key = "S3CR3T-T0KEN-abcdef"
        report = build_overall_report(
            entries=[happy_entry(issue_key=key)], forbidden_secrets=[key]
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert failed_gate_ids(report) == ("G20",)
        assert report.issue_outcomes == ()
        assert report.issue_keys == ()
        assert key not in serialize_report(report)

    def test_an_invalid_issue_key_is_never_echoed(self):
        key = "not/a/valid/key"
        report = build_overall_report(entries=[issue_entry(issue_key=key)])
        assert report.is_valid is False
        assert key not in serialize_report(report)

    def test_the_report_never_carries_a_remote_url(self):
        report = build_overall_report(entries=[happy_entry()])
        text = serialize_report(report)
        assert "://" not in text
        assert "password" not in text.lower()
        assert "token" not in text.lower()


class TestImmutability:
    """T - the result DTOs cannot be mutated."""

    def test_the_report_is_frozen(self):
        report = build_overall_report(entries=[happy_entry()])
        with pytest.raises(FrozenInstanceError):
            report.status = OverallStatus.FAILED
        with pytest.raises(FrozenInstanceError):
            report.total_issues = 5

    def test_the_nested_records_are_frozen(self):
        report = build_overall_report(entries=[happy_entry()])
        with pytest.raises(FrozenInstanceError):
            report.state.phase = OverallReportPhase.INPUT
        with pytest.raises(FrozenInstanceError):
            report.state.gates[0].status = GateStatus.FAIL
        with pytest.raises(FrozenInstanceError):
            report.issue_outcomes[0].outcome = IssueOutcome.NOT_FIXED
        with pytest.raises(FrozenInstanceError):
            report.policy.expected_end_state = ExpectedEndState.ISSUE_FIXED
        with pytest.raises(FrozenInstanceError):
            report.state.stage_records[0].detail = "changed"

    def test_the_input_dto_is_frozen(self):
        entry = happy_entry()
        with pytest.raises(FrozenInstanceError):
            entry.issue_key = "OTHER"

    def test_the_sequences_are_tuples(self):
        report = build_overall_report(entries=[happy_entry()])
        assert isinstance(report.issue_outcomes, tuple)
        assert isinstance(report.reasons, tuple)
        assert isinstance(report.validation_errors, tuple)
        assert isinstance(report.state.gates, tuple)
        assert isinstance(report.state.stage_records, tuple)
        assert isinstance(report.policy.expected_end_state, ExpectedEndState)
        assert report.generated_from == ("T19", "T20", "T21")



class TestStateMachine:
    """U - every phase and every stop position is explicit and testable."""

    def test_a_complete_run_walks_every_phase(self):
        report = build_overall_report(entries=[happy_entry()])
        assert report.state.reached_phases == COMPLETE_PHASES
        assert report.state.phase is OverallReportPhase.COMPLETE
        assert report.state.is_complete is True
        assert report.state.failed_phase is None
        assert report.state.first_failure is None
        assert report.state.not_reached_gates == ()

    def test_the_gate_catalogue_is_always_complete_and_in_order(self):
        report = build_overall_report(entries=[happy_entry()])
        assert tuple(item.gate_id for item in report.state.gates) == tuple(
            gate_id for gate_id, _ in GATE_CATALOGUE
        )
        assert report.state.passed_gates == report.state.gates
        assert report.state.status_of("G1") is GateStatus.PASS
        assert report.state.status_of("unknown-gate") is None
        assert report.state.gate("unknown-gate") is None

    def test_a_g1_failure_stops_at_the_first_phase(self):
        report = build_overall_report(entries=None)
        assert report.state.reached_phases == (OverallReportPhase.INPUT,)
        assert report.state.failed_phase is OverallReportPhase.INPUT_VALIDATED
        assert report.state.first_failure.gate_id == "G1"

    def test_a_g5_failure_stops_before_the_lifecycle_phases(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status={"status": "banana"})]
        )
        assert report.state.reached_phases == (
            OverallReportPhase.INPUT,
            OverallReportPhase.INPUT_VALIDATED,
        )
        assert report.state.failed_phase is (
            OverallReportPhase.ISSUE_RESULTS_VALIDATED
        )
        assert report.state.status_of("G5") is GateStatus.FAIL
        assert report.state.status_of("G6") is GateStatus.NOT_REACHED
        assert report.state.status_of("G20") is GateStatus.NOT_REACHED
        assert report.state.first_failure.gate_id == "G5"

    def test_a_cross_stage_failure_discards_the_aggregate(self):
        report = build_overall_report(
            entries=[issue_entry(commit=commit_view(commit_sha="c" * 40))]
        )
        assert report.state.failed_phase is (
            OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED
        )
        assert report.state.status_of("G15") is GateStatus.PASS
        assert report.state.status_of("G12") is GateStatus.NOT_REACHED
        assert report.state.status_of("G18") is GateStatus.NOT_REACHED
        assert report.total_issues == 0
        assert report.issue_counts == {
            name: 0 for name in ISSUE_STATUS_ORDER
        }
        assert report.issue_outcomes == ()

    @pytest.mark.parametrize(
        "entries",
        (
            None,
            [happy_entry(), happy_entry()],
            [issue_entry(issue_status={"status": "banana"})],
            [issue_entry(commit=commit_view(commit_sha="c" * 40))],
            [issue_entry(push=push_view(status="pushed"))],
        ),
    )
    def test_a_failed_run_is_never_a_success(self, entries):
        report = build_overall_report(entries=entries)
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.is_valid is False
        assert report.is_success is False
        assert report.decision is ReportDecision.REVIEW_REQUIRED
        assert report.validation_errors
        assert report.state.failed_phase is not None
        assert report.state.failed_phase not in report.state.reached_phases

    def test_the_last_reached_phase_precedes_the_failed_phase(self):
        report = build_overall_report(entries=None)
        assert report.state.phase is report.state.reached_phases[-1]
        assert COMPLETE_PHASES.index(report.state.phase) < COMPLETE_PHASES.index(
            report.state.failed_phase
        )

    def test_a_json_verification_failure_stops_at_s10(self, monkeypatch):
        monkeypatch.setattr(
            module, "_json_problems", lambda report: ("the payload is broken",)
        )
        report = build_overall_report(entries=[happy_entry()])
        assert failed_gate_ids(report) == ("G19",)
        assert report.state.status_of("G20") is GateStatus.NOT_REACHED
        assert report.state.failed_phase is OverallReportPhase.REPORT_VERIFIED
        assert report.state.phase is OverallReportPhase.REPORT_BUILT
        assert OverallReportPhase.REPORT_VERIFIED not in (
            report.state.reached_phases
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED

    def test_a_secret_verification_failure_stops_at_s10(self, monkeypatch):
        monkeypatch.setattr(
            module,
            "_secret_problems",
            lambda text, secrets: ("the payload carries a secret",),
        )
        report = build_overall_report(entries=[happy_entry()])
        assert failed_gate_ids(report) == ("G20",)
        assert report.state.status_of("G19") is GateStatus.PASS
        assert report.state.failed_phase is OverallReportPhase.REPORT_VERIFIED
        assert report.state.phase is OverallReportPhase.REPORT_BUILT

    def test_the_recorded_verdicts_are_readable_per_gate(self):
        report = build_overall_report(entries=[happy_entry()])
        assert gate(report, "G1").status is GateStatus.PASS
        assert gate(report, "G6").status is GateStatus.PASS
        assert gate(report, "G20").status is GateStatus.PASS
        assert gate(report, "G21") is None

    def test_every_gate_is_documented_and_unique(self):
        gate_ids = [gate_id for gate_id, _ in GATE_CATALOGUE]
        assert len(gate_ids) == len(set(gate_ids)) == 20
        assert gate_ids[0] == "G1" and gate_ids[-1] == "G20"
        assert all(
            title and isinstance(title, str) for _, title in GATE_CATALOGUE
        )


def _non_json_leaves(value, path="payload"):
    """Every value in ``value`` that is not plain JSON data."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return []
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            if not isinstance(key, str):
                found.append(f"{path}: non-text key")
            found.extend(_non_json_leaves(item, f"{path}.{key}"))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for index, item in enumerate(value):
            found.extend(_non_json_leaves(item, f"{path}[{index}]"))
        return found
    return [f"{path}: {type(value).__name__}"]


class TestSerialization:
    """W - the payload is JSON-safe, canonical and complete."""

    def test_the_payload_is_plain_json_data(self):
        report = build_overall_report(entries=[happy_entry()])
        payload = report.as_dict()
        assert _non_json_leaves(payload) == []
        json.dumps(payload)

    def test_the_json_leaf_scanner_flags_foreign_values(self):
        """The scanner is fail-closed: anything not plain JSON is reported."""
        assert _non_json_leaves({1: "text"}) == ["payload: non-text key"]
        assert _non_json_leaves({"a": {1, 2}}) == ["payload.a: set"]
        assert _non_json_leaves([{"a": None}, 2, "3"]) == []

    def test_serialize_report_is_canonical_json(self):
        report = build_overall_report(entries=[happy_entry()])
        text = serialize_report(report)
        assert text == json.dumps(
            report.as_dict(), sort_keys=True, separators=(",", ":")
        )
        assert json.loads(text) == report.as_dict()

    def test_the_payload_has_the_documented_shape(self):
        report = build_overall_report(entries=[happy_entry()])
        payload = report.as_dict()
        assert set(payload) == {
            "report_version",
            "status",
            "is_valid",
            "decision",
            "needs_attention",
            "generated_from",
            "input_entries",
            "total_issues",
            "successful_issues",
            "failed_issues",
            "review_required_issues",
            "end_to_end_success_count",
            "end_to_end_failure_count",
            "end_to_end_review_count",
            "issue_counts",
            "commit_counts",
            "push_counts",
            "commit_result_count",
            "commit_not_executed_count",
            "push_result_count",
            "push_not_executed_count",
            "issue_outcomes",
            "reasons",
            "validation_errors",
            "policy",
            "state",
        }
        assert payload["report_version"] == module.REPORT_VERSION
        assert payload["generated_from"] == ["T19", "T20", "T21"]
        assert payload["issue_counts"] == {
            name: (1 if name == "fixed" else 0)
            for name in ISSUE_STATUS_ORDER
        }
        assert payload["state"]["gates"][0]["gate_id"] == "G1"
        assert payload["state"]["reached_phases"] == [
            phase.value for phase in COMPLETE_PHASES
        ]
        assert payload["issue_outcomes"][0]["issue_key"] == ISSUE_KEY

    def test_the_payload_statuses_are_the_source_statuses(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=committed(),
                    push=pushed(status="push-unverified"),
                )
            ]
        )
        outcome = report.as_dict()["issue_outcomes"][0]
        assert outcome["t19_status"] == "fixed"
        assert outcome["t20_status"] == "committed"
        assert outcome["t21_status"] == "push-unverified"
        assert outcome["outcome"] == "review-required"
        assert report.as_dict()["push_counts"]["push-unverified"] == 1

    def test_a_failure_payload_serializes_too(self):
        report = build_overall_report(entries=[happy_entry(), happy_entry()])
        payload = report.as_dict()
        assert _non_json_leaves(payload) == []
        assert json.loads(serialize_report(report)) == payload
        assert payload["validation_errors"]
        assert payload["total_issues"] == 0
        assert payload["input_entries"] == 2

    def test_the_end_to_end_aliases_mirror_the_buckets(self):
        report = build_overall_report(
            entries=[
                happy_entry(issue_key="AX1"),
                issue_entry(
                    issue_key="AX2", issue_status=t19_result("still-open")
                ),
            ]
        )
        assert report.end_to_end_success_count == report.successful_issues == 1
        assert report.end_to_end_failure_count == report.failed_issues == 1
        assert report.end_to_end_review_count == report.review_required_issues



@pytest.fixture
def delivered(make_remote_repo, rm, clone_destination):
    """A genuine T20 commit and a genuine T21 push in a temporary clone.

    Nothing is faked: ``GitCommitExecutor.commit_safely`` creates the commit in a
    ``tmp_path`` clone of a local **bare** remote and
    ``GitPushExecutor.push_safely`` pushes it. T22 is then handed the two real
    DTOs (and their ``as_dict()`` views) - the project's own working copy is never
    a commit or push target, and no network is used.
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

    (work / TARGET).write_text("fixed once\n", encoding="utf-8")
    git_run(work, "add", "--", TARGET)
    git_run(work, "commit", "-q", "-m", "fix: published agent commit")
    git_run(
        work,
        "push",
        "-q",
        REMOTE,
        f"refs/heads/{AGENT_BRANCH}:refs/heads/{AGENT_BRANCH}",
    )

    inspector = WorktreeBaselineInspector(timeout_seconds=60.0)
    baseline = inspector.capture(work, include_ignored=True)
    (work / TARGET).write_text("fixed twice\n", encoding="utf-8")
    after = inspector.capture(work, include_ignored=True)
    attribution = attribute_changes(baseline, after)
    status = green_status()
    assert_fixed(status)
    commit = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
        repository_path=work,
        issue_status=status,
        baseline=baseline,
        after=after,
        attribution=attribution,
    )
    push = GitPushExecutor(
        config=PushPolicyConfig(default_branch="main")
    ).push_safely(
        repository_path=str(work),
        commit_result=commit,
        remote=REMOTE,
        remote_branch=AGENT_BRANCH,
    )
    return SimpleNamespace(
        remote=remote, work=work, status=status, commit=commit, push=push
    )


class TestProductionDtoIntegration:
    """V/X - real T19/T20/T21 DTOs, and no side effect from building a report."""

    def test_a_real_lifecycle_reports_end_to_end_success(self, delivered):
        assert delivered.commit.status is CommitStatus.COMMITTED
        assert delivered.push.status is PushStatus.PUSHED
        entry = IssueLifecycleInput(
            issue_key=ISSUE_KEY,
            issue_status=delivered.status,
            commit_result=delivered.commit,
            push_result=delivered.push,
        )
        before = snapshot(delivered.work, remotes=False)

        report = build_overall_report(entries=[entry])

        assert report.status is OverallStatus.SUCCESS
        assert report.is_valid is True
        assert verify_overall_report(report) == ()
        summary = report.issue_outcomes[0]
        assert summary.issue_key == ISSUE_KEY
        assert summary.t19_status == delivered.status.status.value
        assert summary.t20_status == delivered.commit.status.value
        assert summary.t21_status == delivered.push.status.value
        assert summary.commit_sha == delivered.commit.new_head
        assert summary.push_commit == delivered.push.expected_commit
        assert summary.commit_sha == summary.push_commit
        assert summary.end_to_end_success is True
        assert report.commit_counts["committed"] == 1
        assert report.push_counts["pushed"] == 1
        assert report.issue_counts["fixed"] == 1

        # X: building a report must not touch the repository at all.
        assert snapshot(delivered.work, remotes=False) == before
        assert git_run(delivered.work, "rev-parse", "HEAD") == (
            delivered.commit.new_head
        )
        assert git_run(delivered.work, "branch", "--show-current") == (
            AGENT_BRANCH
        )
        assert git_run(delivered.work, "status", "--porcelain") == ""
        assert git_run(
            delivered.remote, "rev-parse", f"refs/heads/{AGENT_BRANCH}"
        ) == delivered.commit.new_head


    def test_the_serialized_views_are_accepted_too(self, delivered):
        entry = {
            "issue_key": ISSUE_KEY,
            "issue_status": delivered.status.as_dict(),
            "commit_result": delivered.commit.as_dict(),
            "push_result": delivered.push.as_dict(),
        }
        report = build_overall_report(entries=[entry])
        assert report.status is OverallStatus.SUCCESS
        assert report.issue_outcomes[0].commit_sha == delivered.commit.new_head
        assert verify_overall_report(report) == ()

    def test_a_real_commit_without_a_push_is_review_required(self, delivered):
        entry = IssueLifecycleInput(
            issue_key=ISSUE_KEY,
            issue_status=delivered.status,
            commit_result=delivered.commit,
        )
        report = build_overall_report(entries=[entry])
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.review_required_issues == 1
        assert report.issue_outcomes[0].change_committed is True
        assert report.issue_outcomes[0].change_pushed is False
        assert report.push_not_executed_count == 1

    def test_the_configured_secrets_do_not_flag_a_real_report(self, delivered):
        """A real report is scanned against the configured secrets and passes."""
        entry = IssueLifecycleInput(
            issue_key=ISSUE_KEY,
            issue_status=delivered.status,
            commit_result=delivered.commit,
            push_result=delivered.push,
        )
        report = build_overall_report(
            entries=[entry], forbidden_secrets=["sonar-token", "g1tlab-token"]
        )
        assert report.status is OverallStatus.SUCCESS
        assert failed_gate_ids(report) == ()


class TestNoSideEffects:
    """X - the module cannot execute anything: it has no such dependency."""

    @pytest.mark.parametrize(
        "name",
        (
            "subprocess",
            "os",
            "shutil",
            "socket",
            "requests",
            "urllib",
            "pathlib",
        ),
    )
    def test_the_module_has_no_execution_dependency(self, name):
        assert not hasattr(module, name)

    def test_building_a_report_runs_no_git(self, delivered, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess, "run", lambda *args, **kwargs: calls.append(args)
        )
        report = build_overall_report(
            entries=[
                IssueLifecycleInput(
                    issue_key=ISSUE_KEY,
                    issue_status=delivered.status,
                    commit_result=delivered.commit,
                    push_result=delivered.push,
                )
            ]
        )
        assert report.status is OverallStatus.SUCCESS
        assert calls == []

    def test_the_input_results_are_not_mutated(self):
        entry = happy_entry()
        before = entry.commit_result.as_dict()
        build_overall_report(entries=[entry])
        assert entry.commit_result.as_dict() == before
        assert entry.issue_status.as_dict() == t19_result("fixed").as_dict()

