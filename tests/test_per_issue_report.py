"""T23 per-issue report tests.

Every test builds its evidence from the *real* production DTOs (a real T19
``IssueStatusResult`` produced by ``determine_issue_status``, a real T20
``CommitResult``, a real T21 ``PushResult`` and a real ``models.SonarIssue``), or
from their ``as_dict()`` views when the point of the test is a corrupt record a
real DTO cannot express. No test shells out, and nothing here touches the
project's own working copy - except the one integration test, which runs the
real T20/T21 executors inside a ``tmp_path`` clone with a local bare remote.

Coverage (the brief's list A-X):

A  ``FIXED`` + ``COMMITTED`` + ``PUSHED`` -> ``SUCCESS``
B  ``FIXED`` + ``COMMITTED`` + ``PUSH_UNVERIFIED`` -> ``REVIEW_REQUIRED``
C  ``FIXED`` + ``COMMIT_UNVERIFIED`` -> ``REVIEW_REQUIRED``
D  ``FIXED`` + ``REFUSED`` -> ``NOT_COMPLETED``
E-J every T19 status (``STILL_OPEN`` ... ``REVIEW_REQUIRED``)
K/L missing T20 / missing T21 -> ``NOT_COMPLETED`` (never an assumed stage)
M  unknown statuses -> refused
N/O malformed and ambiguous issue identities -> refused
P/Q conflicting commit ids and conflicting T20/T21 evidence -> refused
R  invalid branch/remote evidence -> refused
S  serialization, T secret safety, U immutability, V determinism
W  state-machine transitions, X every fail-closed gate
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

import per_issue_report as t23
from commit_policy import GateStatus
from git_commit import CommitStatus, GitCommitExecutor
from git_push import GitPushExecutor, PushStatus
from issue_status import IssueFinalStatus
from models import SonarIssue
from per_issue_report import (
    GATE_CATALOGUE,
    MAX_ISSUE_KEY_LENGTH,
    PerIssueInput,
    PerIssueOutcome,
    PerIssueReport,
    PerIssueReportPhase,
    build_per_issue_report,
    serialize_report,
    verify_per_issue_report,
)
from push_policy import PushPolicyConfig
from worktree_baseline import WorktreeBaselineInspector, attribute_changes

from t20_fixtures import green_status, git_run
from t21_fixtures import snapshot
from t23_fixtures import (
    AGENT_BRANCH,
    COMMIT,
    ISSUE_KEY,
    OTHER_COMMIT,
    PREVIOUS_COMMIT,
    REMOTE,
    TARGET,
    build,
    commit_result,
    commit_view,
    entry,
    failed_gates,
    gate_ids,
    happy_entry,
    issue,
    not_reached_gates,
    push_result,
    push_view,
    review_needed_entry,
    t19_result,
    t19_view,
)

#: Every phase the state machine completes for a valid report, in order.
ALL_PHASES = (
    PerIssueReportPhase.INPUT,
    PerIssueReportPhase.INPUT_VALIDATED,
    PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED,
    PerIssueReportPhase.T19_VALIDATED,
    PerIssueReportPhase.T20_VALIDATED,
    PerIssueReportPhase.T21_VALIDATED,
    PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    PerIssueReportPhase.DERIVED_OUTCOME_RESOLVED,
    PerIssueReportPhase.REPORT_BUILT,
    PerIssueReportPhase.REPORT_VERIFIED,
    PerIssueReportPhase.COMPLETE,
)


def assert_invalid(report, *gate_ids_expected: str) -> None:
    """The fail-closed contract every refused report must satisfy."""
    assert isinstance(report, PerIssueReport)
    assert report.is_valid is False
    assert report.outcome is PerIssueOutcome.REVIEW_REQUIRED
    assert report.needs_attention is True
    assert report.issue_fixed is False
    assert report.change_committed is False
    assert report.change_pushed is False
    assert report.end_to_end_success is False
    assert report.t19_status is None
    assert report.t20_status is None
    assert report.t21_status is None
    assert report.commit_result_supplied is False
    assert report.push_result_supplied is False
    assert report.validation_errors
    assert report.state.is_complete is False
    assert report.state.failed_phase is not None
    assert report.state.failed_phase not in report.state.reached_phases
    for gate_id in gate_ids_expected:
        assert report.state.status_of(gate_id) is GateStatus.FAIL, gate_id
        assert any(
            gate_id in message for message in report.validation_errors
        ), gate_id


class TestSuccessPath:
    def test_the_happy_path_is_a_success(self):
        report = build()
        assert report.outcome is PerIssueOutcome.SUCCESS
        assert report.is_valid is True
        assert report.needs_attention is False
        assert report.issue_fixed is True
        assert report.change_committed is True
        assert report.change_pushed is True
        assert report.end_to_end_success is True
        assert report.t19_status == "fixed"
        assert report.t20_status == "committed"
        assert report.t21_status == "pushed"
        assert report.validation_errors == ()
        assert report.reasons

    def test_a_valid_report_passes_its_own_verification(self):
        report = build()
        assert verify_per_issue_report(report) == ()
        assert report.state.failed_phase is None
        assert report.state.is_complete is True
        assert report.state.phase is PerIssueReportPhase.COMPLETE
        assert report.state.reached_phases == ALL_PHASES
        assert report.state.failed_gates == ()
        assert report.state.not_reached_gates == ()
        assert report.state.passed_gates == report.state.gates
        assert report.state.first_failure is None
        assert report.state.status_of("G1") is GateStatus.PASS
        assert report.gate("G1") is not None
        assert report.gate("G99") is None
        assert report.state.status_of("G99") is None
        assert report.report_version == "t23.1"
        assert report.generated_from == ("T19", "T20", "T21")

    def test_the_issue_identity_is_reported_verbatim(self):
        report = build()
        assert report.issue_key == ISSUE_KEY
        assert report.rule == "python:S1481"
        assert report.component == f"demo:{TARGET}"
        assert report.file_path == TARGET
        assert report.line == 3
        assert report.severity == "MAJOR"
        assert report.issue_type == "CODE_SMELL"
        assert report.message == "remove this unused variable"
        assert report.verification.issue_key == ISSUE_KEY
        assert report.verification.reliable_absence is True
        assert report.verification.correlation == "correlated"
        assert report.analysis.completion_status == "success"
        assert report.analysis.trigger_task_id == "AY1"

    def test_the_commit_and_push_boundaries_are_reported(self):
        report = build()
        assert report.commit_sha == COMMIT
        assert report.previous_head == PREVIOUS_COMMIT
        assert report.branch == AGENT_BRANCH
        assert report.commit_message_issue_key == ISSUE_KEY
        assert report.commit_needs_attention is False
        assert report.expected_commit == COMMIT
        assert report.remote_before_commit == PREVIOUS_COMMIT
        assert report.remote_after_commit == COMMIT
        assert report.remote_branch == AGENT_BRANCH
        assert report.remote == REMOTE
        assert report.refspec == (
            f"refs/heads/{AGENT_BRANCH}:refs/heads/{AGENT_BRANCH}"
        )
        assert report.push_needs_attention is False
        assert report.t20_gate_failure is None
        assert report.t21_gate_failure is None

    def test_views_and_objects_produce_the_identical_report(self):
        objects = build(entry=happy_entry())
        views = build(
            entry={
                "issue_key": ISSUE_KEY,
                "issue": issue(),
                "issue_status": t19_result("fixed").as_dict(),
                "commit_result": commit_result().as_dict(),
                "push_result": push_result().as_dict(),
            }
        )
        assert serialize_report(views) == serialize_report(objects)
        assert views.as_dict() == objects.as_dict()

    def test_an_issue_mapping_needs_only_the_documented_fields(self):
        record = {
            "key": ISSUE_KEY,
            "rule": "python:S1481",
            "severity": "MAJOR",
            "issue_type": "CODE_SMELL",
            "message": "remove this unused variable",
            "component": f"demo:{TARGET}",
            "line": 3,
            "status": "OPEN",
        }
        report = build(
            entry=entry(
                issue_record=record,
                commit=commit_result(),
                push=push_result(),
            )
        )
        assert report.outcome is PerIssueOutcome.SUCCESS
        assert report.file_path == TARGET

    def test_a_commit_and_push_stage_reported_during_a_failed_t19(self):
        """The production shape: T19 says no, so T20 refuses and T21 refuses."""
        report = build(
            entry=entry(
                issue_status=t19_result("still-open"),
                commit=commit_result(status=CommitStatus.REFUSED),
                push=push_result(status=PushStatus.REFUSED),
            )
        )
        assert report.outcome is PerIssueOutcome.FAILED
        assert report.is_valid is True
        assert report.needs_attention is False
        assert report.t19_status == "still-open"
        assert report.t20_status == "refused"
        assert report.t21_status == "refused"
        assert report.issue_fixed is False
        assert report.change_committed is False
        assert report.change_pushed is False
        assert report.end_to_end_success is False


#: Every T19 status T23 must report, and the outcome it implies for an issue
#: with no later evidence at all.
T19_CASES = (
    ("still-open", PerIssueOutcome.FAILED),
    ("analysis-failed", PerIssueOutcome.FAILED),
    ("tests-failed", PerIssueOutcome.FAILED),
    ("scope-invalid", PerIssueOutcome.FAILED),
    ("codex-failed", PerIssueOutcome.FAILED),
    ("review-required", PerIssueOutcome.REVIEW_REQUIRED),
)

#: ``(T20 status, T21 status or None, outcome)`` for a ``FIXED`` issue.
T20_CASES = (
    (CommitStatus.COMMITTED, PushStatus.PUSHED, PerIssueOutcome.SUCCESS),
    (CommitStatus.COMMITTED, None, PerIssueOutcome.NOT_COMPLETED),
    (
        CommitStatus.COMMITTED,
        PushStatus.PUSH_UNVERIFIED,
        PerIssueOutcome.REVIEW_REQUIRED,
    ),
    (CommitStatus.COMMITTED, PushStatus.REFUSED, PerIssueOutcome.NOT_COMPLETED),
    (
        CommitStatus.COMMITTED,
        PushStatus.PUSH_FAILED,
        PerIssueOutcome.NOT_COMPLETED,
    ),
    (
        CommitStatus.COMMIT_UNVERIFIED,
        None,
        PerIssueOutcome.REVIEW_REQUIRED,
    ),
    (CommitStatus.REFUSED, None, PerIssueOutcome.NOT_COMPLETED),
    (CommitStatus.COMMIT_FAILED, None, PerIssueOutcome.NOT_COMPLETED),
)

#: ``(T21 status, outcome)`` for a ``FIXED`` + ``COMMITTED`` issue.
T21_CASES = (
    (PushStatus.PUSHED, PerIssueOutcome.SUCCESS),
    (PushStatus.PUSH_UNVERIFIED, PerIssueOutcome.REVIEW_REQUIRED),
    (PushStatus.REFUSED, PerIssueOutcome.NOT_COMPLETED),
    (PushStatus.PUSH_FAILED, PerIssueOutcome.NOT_COMPLETED),
)


class TestT19StatusCoverage:
    @pytest.mark.parametrize("status, expected", T19_CASES)
    def test_every_non_fixed_status_is_preserved_verbatim(self, status, expected):
        report = build(entry=entry(issue_status=t19_result(status)))
        assert report.outcome is expected
        assert report.is_valid is True
        assert report.t19_status == status
        assert report.t20_status is None
        assert report.t21_status is None
        assert report.issue_fixed is False
        assert report.end_to_end_success is False
        assert report.needs_attention is (
            expected is PerIssueOutcome.REVIEW_REQUIRED
        )
        assert status in report.reasons[1]

    def test_the_cases_cover_every_production_t19_status(self):
        covered = {status for status, _ in T19_CASES} | {"fixed"}
        assert covered == set(t23.ISSUE_STATUS_ORDER)
        assert covered == {member.value for member in IssueFinalStatus}

    def test_a_review_required_issue_with_no_later_evidence(self):
        report = build(entry=review_needed_entry())
        assert report.outcome is PerIssueOutcome.REVIEW_REQUIRED
        assert report.is_valid is True
        assert report.needs_attention is True
        assert report.t19_status == "review-required"
        assert report.commit_result_supplied is False
        assert report.push_result_supplied is False
        assert report.end_to_end_success is False


class TestT20StatusCoverage:
    @pytest.mark.parametrize("t20_status, t21_status, expected", T20_CASES)
    def test_every_t20_status_is_preserved_and_classified(
        self, t20_status, t21_status, expected
    ):
        report = build(
            entry=entry(
                commit=commit_result(status=t20_status),
                push=(
                    None
                    if t21_status is None
                    else push_result(status=t21_status)
                ),
            )
        )
        assert report.outcome is expected
        assert report.is_valid is True
        assert report.t19_status == "fixed"
        assert report.t20_status == t20_status.value
        assert report.t21_status is (
            None if t21_status is None else t21_status.value
        )
        assert report.issue_fixed is True
        assert report.change_committed is (
            t20_status is CommitStatus.COMMITTED
        )
        assert report.change_pushed is (t21_status is PushStatus.PUSHED)
        assert report.end_to_end_success is (
            expected is PerIssueOutcome.SUCCESS
        )
        assert report.commit_result_supplied is True
        assert report.push_result_supplied is (t21_status is not None)

    def test_the_cases_cover_every_production_t20_status(self):
        covered = {status.value for status, _, _ in T20_CASES}
        assert covered == set(t23.COMMIT_STATUS_ORDER)

    def test_the_cases_cover_every_production_t21_status(self):
        covered = {
            status.value for _, status, _ in T20_CASES if status is not None
        }
        covered |= {status.value for status, _ in T21_CASES}
        assert covered == set(t23.PUSH_STATUS_ORDER)


class TestT21StatusCoverage:
    @pytest.mark.parametrize("t21_status, expected", T21_CASES)
    def test_every_t21_status_is_preserved_and_classified(
        self, t21_status, expected
    ):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=push_result(status=t21_status),
            )
        )
        assert report.outcome is expected
        assert report.is_valid is True
        assert report.t21_status == t21_status.value
        assert report.t20_status == "committed"
        assert report.issue_fixed is True
        assert report.change_committed is True
        assert report.change_pushed is (t21_status is PushStatus.PUSHED)
        assert report.end_to_end_success is (
            expected is PerIssueOutcome.SUCCESS
        )

    def test_the_cases_cover_every_production_t21_status(self):
        assert {status.value for status, _ in T21_CASES} == set(
            t23.PUSH_STATUS_ORDER
        )

    @pytest.mark.parametrize(
        "t21_status",
        [
            PushStatus.PUSH_UNVERIFIED,
            PushStatus.REFUSED,
            PushStatus.PUSH_FAILED,
        ],
    )
    def test_no_unverified_or_failed_push_is_ever_a_success(self, t21_status):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=push_result(status=t21_status),
            )
        )
        assert report.outcome is not PerIssueOutcome.SUCCESS
        assert report.end_to_end_success is False
        assert report.needs_attention is True


class TestMissingEvidence:
    def test_a_fixed_issue_without_a_t20_result_is_not_completed(self):
        report = build(entry=entry())
        assert report.outcome is PerIssueOutcome.NOT_COMPLETED
        assert report.is_valid is True
        assert report.t20_status is None
        assert report.t21_status is None
        assert report.commit_result_supplied is False
        assert report.push_result_supplied is False
        assert report.change_committed is False
        assert report.end_to_end_success is False
        assert report.needs_attention is True
        assert any(
            "no T20 commit result was supplied" in line
            for line in report.reasons
        )

    def test_a_missing_stage_is_never_reported_as_a_status(self):
        report = build(entry=entry())
        assert report.t20_status not in t23.COMMIT_STATUS_ORDER
        assert report.t21_status not in t23.PUSH_STATUS_ORDER
        assert report.commit_sha is None
        assert report.previous_head is None
        assert report.expected_commit is None
        assert report.remote_after_commit is None
        assert any(
            "not executed" in record.detail
            for record in report.state.stage_records
        )
        assert "not supplied" in report.reasons[1]

    def test_a_committed_fix_without_a_t21_result_is_not_completed(self):
        report = build(entry=entry(commit=commit_result()))
        assert report.outcome is PerIssueOutcome.NOT_COMPLETED
        assert report.is_valid is True
        assert report.change_committed is True
        assert report.change_pushed is False
        assert report.t21_status is None
        assert report.needs_attention is True
        assert report.commit_sha == COMMIT
        assert any(
            "no T21 push result was supplied" in line
            for line in report.reasons
        )

    def test_a_missing_t19_result_is_a_refusal(self):
        report = build(entry=entry(issue_status=None))
        assert_invalid(report, "G4")
        assert report.state.phase is PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED
        assert report.state.failed_phase is PerIssueReportPhase.T19_VALIDATED
        # The identity was validated before the T19 result was read, so a
        # failure report still says which issue it is about.
        assert report.issue_key == ISSUE_KEY


class TestUnknownStatuses:
    def test_an_unknown_t19_status_is_refused(self):
        assert_invalid(build(entry=entry(issue_status=t19_view(status="mystery"))), "G5")

    def test_an_unknown_t20_status_is_refused(self):
        assert_invalid(build(entry=entry(commit=commit_view(status="mystery"))), "G7")

    def test_an_unknown_t21_status_is_refused(self):
        assert_invalid(build(entry=entry(push=push_view(status="mystery"))), "G9")

    @pytest.mark.parametrize("field, value", [
        ("match_type", "weird"),
        ("correlation", "maybe"),
    ])
    def test_an_unknown_verification_value_is_refused(self, field, value):
        view = t19_view()
        view["verification"][field] = value
        assert_invalid(build(entry=entry(issue_status=view)), "G4")

    def test_an_unknown_analysis_state_is_refused(self):
        view = t19_view()
        view["analysis"]["status"] = "weird"
        assert_invalid(build(entry=entry(issue_status=view)), "G4")

    @pytest.mark.parametrize("status", [7, None, " mystery", ["mystery"]])
    def test_a_status_that_is_not_well_formed_text_is_refused(self, status):
        view = t19_view()
        view["status"] = status
        assert_invalid(build(entry=entry(issue_status=view)), "G4")


class TestIssueIdentity:
    @pytest.mark.parametrize(
        "declared",
        [
            "",
            " AX1",
            "AX1 ",
            "x" * (MAX_ISSUE_KEY_LENGTH + 1),
            "AX1!",
            "AX1#1",
            5,
            None,
        ],
    )
    def test_an_unusable_declared_key_is_refused(self, declared):
        assert_invalid(build(entry=entry(issue_key=declared)), "G2")

    def test_a_missing_issue_record_is_refused(self):
        report = build(
            entry=entry(issue_record=None, issue_status=t19_result("fixed"))
        )
        assert_invalid(report, "G2")
        # No identity was validated, so none is published: an invalid report
        # never echoes unvalidated external text.
        assert report.issue_key == ""
        assert report.rule is None
        assert report.file_path is None
        assert report.message is None

    @pytest.mark.parametrize("record", ["not-an-issue", 5, object()])
    def test_a_non_issue_record_is_refused(self, record):
        assert_invalid(build(entry=entry(issue_record=record)), "G2")

    def test_an_issue_record_with_an_unknown_field_is_refused(self):
        record = {
            "key": ISSUE_KEY,
            "rule": "python:S1481",
            "severity": "MAJOR",
            "issue_type": "CODE_SMELL",
            "message": "remove this unused variable",
            "component": f"demo:{TARGET}",
            "line": 3,
            "status": "OPEN",
            "surprise": "extra",
        }
        report = build(entry=entry(issue_record=record))
        assert_invalid(report, "G2")
        assert any(
            "outside the SonarQube issue contract" in message
            for message in report.validation_errors
        )

    @pytest.mark.parametrize("field", ["key", "rule", "component"])
    def test_a_missing_required_identity_field_is_refused(self, field):
        record = {
            "key": ISSUE_KEY,
            "rule": "python:S1481",
            "component": f"demo:{TARGET}",
        }
        del record[field]
        assert_invalid(build(entry=entry(issue_record=record)), "G2")

    @pytest.mark.parametrize("line", [0, -3, "3", 1.5, True])
    def test_an_unusable_line_is_refused(self, line):
        assert_invalid(build(entry=entry(issue_record=issue(line=line))), "G2")

    @pytest.mark.parametrize(
        "field, value",
        [
            ("message", "line one\nline two"),
            ("rule", "x" * 401),
            ("message", "secret\u0007bell"),
            ("severity", 5),
            ("issue_type", ""),
            ("status", " OPEN"),
        ],
    )
    def test_an_unusable_identity_field_is_refused(self, field, value):
        record = issue(**{field: value})
        assert_invalid(build(entry=entry(issue_record=record)), "G2")

    def test_a_declared_key_that_disagrees_with_the_record_is_refused(self):
        assert_invalid(build(entry=entry(issue_key="AX2")), "G3")

    def test_a_verification_key_that_disagrees_with_the_record_is_refused(self):
        view = t19_view()
        view["verification"]["original_issue_key"] = "AX2"
        assert_invalid(build(entry=entry(issue_status=view)), "G10")

    def test_a_commit_message_key_that_disagrees_is_refused(self):
        report = build(entry=entry(commit=commit_result(issue_key="AX2")))
        assert_invalid(report, "G13")

    def test_the_component_prefix_is_stripped_from_the_file_path(self):
        report = build(entry=entry(issue_record=issue(component="demo:src/x/y.py")))
        assert report.component == "demo:src/x/y.py"
        assert report.file_path == "src/x/y.py"

    def test_a_plain_path_component_is_reported_unchanged(self):
        report = build(entry=entry(issue_record=issue(component="src/app.py")))
        assert report.file_path == "src/app.py"

    def test_the_message_is_the_only_free_form_field(self):
        report = build(entry=entry(issue_record=issue(message="unused import")))
        assert report.message == "unused import"
        assert report.rule == "python:S1481"


def patched(view, **fields):
    """Apply ``block.field``-style (or flat) values to a result view in place."""
    for key, value in fields.items():
        if "." in key:
            block, name = key.split(".", 1)
            view[block][name] = value
        else:
            view[key] = value
    return view


class TestT19EvidenceConsistency:
    def test_a_minimal_t19_mapping_is_accepted(self):
        """Absence is never a contradiction: only published evidence is checked."""
        report = build(entry=entry(issue_status={"status": "fixed"}))
        assert report.is_valid is True
        assert report.outcome is PerIssueOutcome.NOT_COMPLETED

    def test_a_minimal_t20_mapping_still_fails_closed(self):
        """A commit claim without a commit id is refused, never assumed."""
        report = build(entry=entry(commit={"status": "committed"}))
        assert_invalid(report, "G11")

    @pytest.mark.parametrize(
        "fields",
        [
            {"is_fixed": False},
            {"verification.is_present": True},
            {"verification.identity_reliable": False},
            {"verification.reliable_absence": False},
            {"verification.retrieval_succeeded": False},
            {"verification.correlation": "unknown"},
            {"verification.page_complete": False},
            {"scope.is_valid": False},
            {"scope.expected_file_modified": False},
            {"tests.passed": False},
            {"codex.execution_succeeded": False},
            {"analysis.status": "failed"},
        ],
    )
    def test_a_fixed_status_whose_evidence_contradicts_it_is_refused(self, fields):
        report = build(entry=entry(issue_status=patched(t19_view(), **fields)))
        assert_invalid(report, "G10")

    def test_a_trigger_that_failed_is_never_a_fixed_issue(self):
        view = t19_view()
        view["trigger_evidence"]["triggered"] = False
        assert_invalid(build(entry=entry(issue_status=view)), "G10")

    def test_a_trigger_without_a_task_id_is_never_a_fixed_issue(self):
        view = t19_view()
        view["trigger_evidence"]["task_id"] = None
        assert_invalid(build(entry=entry(issue_status=view)), "G10")

    @pytest.mark.parametrize(
        "value",
        ["triggered", 7, {"triggered": "yes"}, {"task_id": 5}],
    )
    def test_a_malformed_trigger_evidence_block_is_refused(self, value):
        view = t19_view()
        view["trigger_evidence"] = value
        assert_invalid(build(entry=entry(issue_status=view)), "G4")

    def test_a_trigger_evidence_block_that_is_absent_is_accepted(self):
        """T19 itself allows a missing trigger record; absence proves nothing."""
        view = t19_view()
        view["trigger_evidence"] = None
        report = build(entry=entry(issue_status=view))
        assert report.is_valid is True
        assert report.analysis.triggered is None
        assert report.analysis.trigger_task_id is None

    def test_a_review_flag_that_disagrees_is_refused(self):
        view = t19_view(status="review-required")
        view["needs_review"] = False
        assert_invalid(build(entry=entry(issue_status=view)), "G10")

    @pytest.mark.parametrize(
        "status, fields",
        [
            ("still-open", {"verification.is_present": False}),
            ("scope-invalid", {"scope.is_valid": True}),
            ("tests-failed", {"tests.passed": True}),
            ("codex-failed", {"codex.execution_succeeded": True}),
        ],
    )
    def test_a_terminal_status_whose_evidence_says_the_opposite_is_refused(
        self, status, fields
    ):
        view = patched(t19_view(status=status), **fields)
        assert_invalid(build(entry=entry(issue_status=view)), "G10")


class TestT20EvidenceConsistency:
    @pytest.mark.parametrize(
        "fields",
        [
            {"is_committed": False},
            {"needs_attention": True},
            {"commit_sha": None},
            {"new_head": None},
            {"commit_sha": OTHER_COMMIT},
            {"previous_head": COMMIT},
        ],
    )
    def test_a_committed_result_that_contradicts_itself_is_refused(self, fields):
        report = build(entry=entry(commit=patched(commit_view(), **fields)))
        assert_invalid(report, "G11")

    def test_an_unverified_commit_that_claims_to_be_committed_is_refused(self):
        view = patched(commit_view(status="commit-unverified"), is_committed=True)
        assert_invalid(build(entry=entry(commit=view)), "G11")

    @pytest.mark.parametrize(
        "fields",
        [
            {"new_head": "not-a-sha"},
            {"commit_sha": "zz"},
            {"previous_head": 7},
            {"is_committed": "yes"},
            {"commit_message": "nope"},
            {"commit_message.issue_key": 5},
            {"repository": "nope"},
            {"repository.branch": 5},
            {"gates": "nope"},
            {"gates.first_failure": "ZZ"},
        ],
    )
    def test_a_malformed_t20_field_is_refused(self, fields):
        report = build(entry=entry(commit=patched(commit_view(), **fields)))
        assert_invalid(report, "G6")

    @pytest.mark.parametrize("status", [7, None, " committed"])
    def test_a_malformed_t20_status_is_refused(self, status):
        view = patched(commit_view(), status=status)
        assert_invalid(build(entry=entry(commit=view)), "G6")


class TestT21EvidenceConsistency:
    @pytest.mark.parametrize(
        "fields",
        [
            {"is_pushed": False},
            {"is_refusal": True},
            {"needs_attention": True},
            {"push_attempted": False},
            {"expected_commit": None},
            {"remote_after_commit": None},
            {"remote_after_commit": OTHER_COMMIT},
            {"remote_before_commit": COMMIT},
        ],
    )
    def test_a_pushed_result_that_contradicts_itself_is_refused(self, fields):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=patched(push_view(), **fields),
            )
        )
        assert_invalid(report, "G12")

    def test_a_verified_push_whose_remote_tip_did_not_change_is_refused(self):
        """``before == after == expected_commit`` is a contradiction, not a push."""
        view = patched(
            push_view(),
            remote_before_commit=COMMIT,
            remote_after_commit=COMMIT,
        )
        report = build(entry=entry(commit=commit_result(), push=view))
        assert failed_gates(report) == ("G12",)
        assert_invalid(report, "G12")
        assert report.outcome is not PerIssueOutcome.SUCCESS
        assert report.end_to_end_success is False
        assert any(
            "remote tip" in message for message in report.validation_errors
        )

    def test_the_remote_tips_are_compared_after_normalization(self):
        """T21's own normalization applies, so case must not hide a non-move."""
        view = patched(
            push_view(),
            remote_before_commit=COMMIT.upper(),
            remote_after_commit=COMMIT,
        )
        report = build(entry=entry(commit=commit_result(), push=view))
        assert failed_gates(report) == ("G12",)
        assert_invalid(report, "G12")
        assert report.end_to_end_success is False

    @pytest.mark.parametrize("before", [None, PREVIOUS_COMMIT, OTHER_COMMIT])
    def test_a_pushed_result_whose_remote_tip_moved_stays_valid(self, before):
        """A verified push only needs a *different* (or absent) old tip."""
        view = patched(
            push_view(),
            remote_before_commit=before,
            remote_after_commit=COMMIT,
        )
        report = build(entry=entry(commit=commit_result(), push=view))
        assert report.is_valid is True
        assert report.outcome is PerIssueOutcome.SUCCESS
        assert report.change_pushed is True
        assert report.end_to_end_success is True
        assert report.remote_before_commit == before
        assert report.remote_after_commit == COMMIT
        assert verify_per_issue_report(report) == ()

    def test_a_refusal_that_claims_to_be_a_push_is_refused(self):
        view = patched(push_view(status="refused"), is_refusal=False)
        assert_invalid(
            build(entry=entry(commit=commit_result(), push=view)), "G12"
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"remote": "nope"},
            {"remote.name": 5},
            {"remote.exists": "yes"},
            {"gates": "nope"},
            {"gates.first_failure": "ZZ"},
            {"expected_commit": "zz"},
            {"expected_commit": 7},
            {"remote_before_commit": "zz"},
            {"remote_after_commit": 7},
            {"branch": 5},
            {"remote_branch": 5},
            {"refspec": 5},
        ],
    )
    def test_a_malformed_t21_field_is_refused(self, fields):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=patched(push_view(), **fields),
            )
        )
        assert_invalid(report, "G8")

    @pytest.mark.parametrize("status", [7, None, " pushed"])
    def test_a_malformed_t21_status_is_refused(self, status):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=patched(push_view(), status=status),
            )
        )
        assert_invalid(report, "G8")


class TestCommitIdentityConsistency:
    def test_a_push_without_the_t20_result_it_consumes_is_refused(self):
        report = build(entry=entry(commit=None, push=push_result()))
        assert_invalid(report, "G13")

    def test_a_push_although_t20_did_not_verify_a_commit_is_refused(self):
        report = build(
            entry=entry(
                commit=commit_result(status=CommitStatus.REFUSED),
                push=push_result(),
            )
        )
        assert_invalid(report, "G13")

    def test_a_push_without_the_commit_it_pushed_is_refused(self):
        view = patched(push_view(status="push-unverified"), expected_commit=None)
        report = build(entry=entry(commit=commit_result(), push=view))
        assert_invalid(report, "G13")

    def test_a_push_of_a_different_commit_is_refused(self):
        view = patched(
            push_view(status="push-unverified"), expected_commit=OTHER_COMMIT
        )
        report = build(entry=entry(commit=commit_result(), push=view))
        assert_invalid(report, "G13")

    def test_the_first_failing_gate_wins_and_later_gates_stay_not_reached(self):
        report = build(
            entry=entry(
                commit=commit_result(status=CommitStatus.REFUSED),
                push=push_result(),
            )
        )
        assert failed_gates(report) == ("G13",)
        assert "G15" in not_reached_gates(report)
        assert report.state.first_failure.gate_id == "G13"

    def test_a_pushed_commit_is_accepted_when_both_stages_agree(self):
        report = build(entry=entry(commit=commit_result(), push=push_result()))
        assert report.is_valid is True
        assert report.commit_sha == report.expected_commit == COMMIT


class TestBranchAndRemoteConsistency:
    def test_a_push_on_a_different_branch_than_the_commit_is_refused(self):
        report = build(
            entry=entry(
                commit=commit_result(branch="ai/sonar-fix/other"),
                push=push_result(),
            )
        )
        assert_invalid(report, "G14")

    def test_a_refspec_that_does_not_match_its_branches_is_refused(self):
        view = patched(push_view(), refspec="refs/heads/a:refs/heads/b")
        assert_invalid(
            build(entry=entry(commit=commit_result(), push=view)), "G14"
        )

    def test_a_push_without_a_refspec_is_refused(self):
        view = patched(push_view(), refspec=None)
        assert_invalid(
            build(entry=entry(commit=commit_result(), push=view)), "G14"
        )

    def test_a_push_to_a_missing_remote_is_refused(self):
        view = patched(push_view(), **{"remote.exists": False})
        assert_invalid(
            build(entry=entry(commit=commit_result(), push=view)), "G14"
        )

    def test_a_push_of_another_remote_branch_is_reported_verbatim(self):
        report = build(
            entry=entry(
                commit=commit_result(),
                push=push_result(remote_branch="ai/sonar-fix/AX1-next"),
            )
        )
        assert report.is_valid is True
        assert report.remote_branch == "ai/sonar-fix/AX1-next"
        assert report.refspec.endswith("refs/heads/ai/sonar-fix/AX1-next")


class TestLifecycleOrdering:
    def test_a_push_result_without_any_commit_result_is_refused(self):
        report = build(
            entry=entry(commit=None, push=push_result(status=PushStatus.REFUSED))
        )
        assert_invalid(report, "G15")

    def test_a_commit_claimed_for_a_non_fixed_issue_is_refused(self):
        report = build(
            entry=entry(
                issue_status=t19_result("still-open"), commit=commit_view()
            )
        )
        assert_invalid(report, "G15")

    def test_an_unverified_commit_for_a_non_fixed_issue_is_refused(self):
        report = build(
            entry=entry(
                issue_status=t19_result("review-required"),
                commit=commit_result(status=CommitStatus.COMMIT_UNVERIFIED),
            )
        )
        assert_invalid(report, "G15")

    def test_a_refused_commit_for_a_non_fixed_issue_is_reportable(self):
        report = build(
            entry=entry(
                issue_status=t19_result("still-open"),
                commit=commit_result(status=CommitStatus.REFUSED),
            )
        )
        assert report.is_valid is True
        assert report.outcome is PerIssueOutcome.FAILED
        assert report.t20_status == "refused"


class TestInputContract:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "AX1",
            7,
            3.5,
            {"issue_key": "AX1", "surprise": 1},
            object(),
        ],
    )
    def test_an_unusable_input_is_refused(self, value):
        report = build_per_issue_report(entry=value)
        assert_invalid(report, "G1")
        assert report.state.phase is PerIssueReportPhase.INPUT
        assert report.state.failed_phase is PerIssueReportPhase.INPUT_VALIDATED
        assert report.issue_key == ""

    def test_a_hostile_mapping_is_refused_not_raised(self):
        class Hostile(dict):
            def __iter__(self):
                raise RuntimeError("hostile mapping")

        assert_invalid(build_per_issue_report(entry=Hostile()), "G1")

    def test_a_mapping_input_with_exactly_the_contract_fields_is_accepted(self):
        report = build(
            entry={
                "issue_key": ISSUE_KEY,
                "issue": issue(),
                "issue_status": t19_result("fixed").as_dict(),
                "commit_result": commit_result().as_dict(),
                "push_result": push_result().as_dict(),
            }
        )
        assert report.is_valid is True
        assert report.outcome is PerIssueOutcome.SUCCESS

    @pytest.mark.parametrize("broken", ["issue_status", "commit", "push"])
    def test_a_result_whose_view_raises_is_refused(self, broken):
        class Broken:
            def as_dict(self):
                raise RuntimeError("no usable view")

        entry_ = entry(**{broken: Broken()})
        expected = {
            "issue_status": "G4",
            "commit": "G6",
            "push": "G8",
        }[broken]
        assert_invalid(build(entry=entry_), expected)

    @pytest.mark.parametrize("broken", ["commit", "push"])
    def test_a_result_without_any_view_is_refused(self, broken):
        entry_ = entry(**{broken: object()})
        expected = {"commit": "G6", "push": "G8"}[broken]
        assert_invalid(build(entry=entry_), expected)

    @pytest.mark.parametrize("secrets", ["abc", 7, 3.5])
    def test_forbidden_secrets_must_be_an_iterable_of_secrets(self, secrets):
        with pytest.raises(t23.PerIssueReportError):
            build_per_issue_report(entry=happy_entry(), forbidden_secrets=secrets)

    def test_a_failing_secret_mapping_is_a_caller_error(self):
        class Raising:
            def __iter__(self):
                raise RuntimeError("no secrets")

        with pytest.raises(t23.PerIssueReportError):
            build_per_issue_report(
                entry=happy_entry(), forbidden_secrets=Raising()
            )


class TestDerivedStateGates:
    """``G16``-``G19``: the derived state must follow from the evidence."""

    def test_a_failure_report_also_passes_its_own_verification(self):
        report = build(entry=entry(issue_status=None))
        assert report.is_valid is False
        assert verify_per_issue_report(report) == ()

    @pytest.mark.parametrize(
        "field, value, expected",
        [
            ("issue_fixed", False, "issue_fixed does not follow"),
            ("change_committed", False, "change_committed does not follow"),
            ("change_pushed", False, "change_pushed does not follow"),
            (
                "end_to_end_success",
                False,
                "end_to_end_success does not follow",
            ),
            ("outcome", PerIssueOutcome.FAILED, "outcome does not follow"),
            ("needs_attention", True, "needs_attention does not follow"),
            (
                "validation_errors",
                ("boom",),
                "must not carry validation errors",
            ),
            ("report_version", "t23.0", "current report version"),
        ],
    )
    def test_the_verifier_catches_a_corrupted_report(self, field, value, expected):
        report = replace(build(), **{field: value})
        problems = verify_per_issue_report(report)
        assert any(expected in problem for problem in problems), problems

    def test_the_verifier_catches_a_valid_report_with_a_failed_phase(self):
        report = build()
        broken = replace(
            report,
            state=replace(
                report.state, failed_phase=PerIssueReportPhase.T19_VALIDATED
            ),
        )
        assert any(
            "failed phase" in problem
            for problem in verify_per_issue_report(broken)
        )

    def test_the_verifier_catches_an_incomplete_gate_catalogue(self):
        report = build()
        broken = replace(
            report, state=replace(report.state, gates=report.state.gates[:-1])
        )
        assert any(
            "gate catalogue" in problem
            for problem in verify_per_issue_report(broken)
        )

    def test_the_verifier_catches_an_invalid_report_that_claims_success(self):
        broken = replace(
            build(entry=entry(issue_status=None)),
            outcome=PerIssueOutcome.SUCCESS,
            needs_attention=False,
        )
        problems = verify_per_issue_report(broken)
        assert any("must be REVIEW_REQUIRED" in problem for problem in problems)

    def test_the_verifier_catches_an_invalid_report_that_needs_no_attention(self):
        broken = replace(
            build(entry=entry(issue_status=None)), needs_attention=False
        )
        assert any(
            "must ask for attention" in problem
            for problem in verify_per_issue_report(broken)
        )

    def test_a_broken_derivation_is_refused_by_the_pipeline(self, monkeypatch):
        monkeypatch.setattr(
            t23, "_derived_problems", lambda report: (("G16", "seam"),)
        )
        report = build()
        assert_invalid(report, "G16")
        assert report.state.phase is PerIssueReportPhase.DERIVED_OUTCOME_RESOLVED
        assert report.state.failed_phase is PerIssueReportPhase.REPORT_BUILT

    def test_a_broken_serialization_gate_is_refused_by_the_pipeline(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            t23, "_json_problems", lambda report: ("seam",)
        )
        report = build()
        assert_invalid(report, "G20")
        assert report.state.status_of("G21") is GateStatus.NOT_REACHED
        assert report.state.phase is PerIssueReportPhase.REPORT_BUILT
        assert report.state.failed_phase is PerIssueReportPhase.REPORT_VERIFIED

    def test_a_secret_gate_failure_is_refused_by_the_pipeline(self, monkeypatch):
        monkeypatch.setattr(
            t23, "_secret_problems", lambda text, secrets: ("seam",)
        )
        report = build()
        assert_invalid(report, "G21")
        assert report.issue_key == ""

    def test_an_unexpected_error_fails_closed(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(t23, "_cross_stage_problems", boom)
        report = build()
        assert report.is_valid is False
        assert report.outcome is PerIssueOutcome.REVIEW_REQUIRED
        assert any(
            "RuntimeError" in message for message in report.validation_errors
        )
        assert "boom" not in serialize_report(report)
        assert report.state.failed_phase is PerIssueReportPhase.T21_VALIDATED


class TestSerialization:
    def test_the_payload_is_json_safe(self):
        report = build()
        payload = report.as_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert serialize_report(report) == json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

    def test_the_report_publishes_every_documented_section(self):
        payload = build().as_dict()
        assert set(payload) == {
            "report_version",
            "outcome",
            "is_valid",
            "needs_attention",
            "generated_from",
            "issue",
            "statuses",
            "t19_evidence",
            "t20_evidence",
            "t21_evidence",
            "branch",
            "derived",
            "reasons",
            "validation_errors",
            "state",
        }
        assert set(payload["issue"]) == {
            "issue_key",
            "rule",
            "file_path",
            "component",
            "line",
            "severity",
            "issue_type",
            "message",
        }
        assert set(payload["statuses"]) == {
            "t19_status",
            "t20_status",
            "t21_status",
            "commit_result_supplied",
            "push_result_supplied",
        }
        assert set(payload["derived"]) == {
            "issue_fixed",
            "change_committed",
            "change_pushed",
            "end_to_end_success",
        }
        assert len(payload["state"]["gates"]) == len(GATE_CATALOGUE)
        assert [
            gate["gate_id"] for gate in payload["state"]["gates"]
        ] == [gate_id for gate_id, _ in GATE_CATALOGUE]

    def test_a_failure_report_serializes_too(self):
        report = build(entry=entry(issue_status=t19_view(status="mystery")))
        payload = report.as_dict()
        assert json.loads(serialize_report(report)) == payload
        assert payload["statuses"]["t19_status"] is None
        assert payload["validation_errors"]

    def test_the_report_never_hands_out_internal_state(self):
        report = build()
        payload = report.as_dict()
        payload["derived"]["issue_fixed"] = False
        payload["issue"]["rule"] = "tampered"
        assert report.as_dict()["derived"]["issue_fixed"] is True
        assert report.rule == "python:S1481"

    @pytest.mark.parametrize(
        "value, expected",
        [
            ({1, 2}, "payload carries a set"),
            (object(), "payload carries a object"),
            ({1: "x"}, "payload carries a non-text key"),
            ([{"a": object()}], "payload[0].a carries a object"),
            ((1, {2: object()}), "payload[1] carries a non-text key"),
        ],
    )
    def test_the_leaf_scanner_flags_non_json_values(self, value, expected):
        assert t23._unserializable(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "text", 7, 3.5, True, {"a": [1, 2]}, (None, "x")]
    )
    def test_the_leaf_scanner_accepts_plain_json_data(self, value):
        assert t23._unserializable(value) is None


#: A literal secret the tests configure as a forbidden value.
SECRET = "s3cr3t-token-value"
#: A credential-bearing URL (detected without any configuration).
CREDENTIAL_URL = "https://user:sw0rdf1sh@example.invalid/repo.git"


class TestSecretSafety:
    def test_a_configured_secret_in_the_message_refuses_the_report(self):
        report = build(
            entry=entry(issue_record=issue(message=f"remove {SECRET}")),
            forbidden_secrets=(SECRET,),
        )
        assert_invalid(report, "G21")
        text = serialize_report(report)
        assert SECRET not in text
        for line in report.reasons + report.validation_errors:
            assert SECRET not in line

    def test_a_credential_url_in_the_message_refuses_the_report(self):
        report = build(
            entry=entry(issue_record=issue(message=f"see {CREDENTIAL_URL}"))
        )
        assert_invalid(report, "G21")
        text = serialize_report(report)
        assert "sw0rdf1sh" not in text
        assert CREDENTIAL_URL not in text

    def test_a_secret_in_the_issue_key_refuses_the_report(self):
        declared = "AX" + SECRET
        view = t19_view()
        view["verification"]["original_issue_key"] = declared
        report = build(
            entry=entry(
                issue_key=declared,
                issue_record=issue(key=declared),
                issue_status=view,
            ),
            forbidden_secrets=(SECRET,),
        )
        assert_invalid(report, "G21")
        assert SECRET not in serialize_report(report)
        assert report.issue_key == ""

    def test_upstream_free_text_is_never_copied_into_the_report(self):
        view = t19_view(reason=f"token {SECRET}", reasons=[f"token {SECRET}"])
        view["verification"]["reason"] = f"token {SECRET}"
        view["verification"]["error"] = f"token {SECRET}"
        commit = patched(
            commit_view(),
            reason=f"token {SECRET}",
            stage_records=[{"stage": "verify", "detail": SECRET}],
        )
        push = patched(
            push_view(),
            reason=f"token {SECRET}",
            stage_records=[{"stage": "verify", "detail": SECRET}],
        )
        report = build(
            entry=entry(issue_status=view, commit=commit, push=push),
            forbidden_secrets=(SECRET,),
        )
        assert report.outcome is PerIssueOutcome.SUCCESS
        text = serialize_report(report)
        assert SECRET not in text
        assert "token" not in text

    def test_a_secret_on_the_failure_path_is_never_published(self):
        report = build(
            entry=entry(
                issue_record=issue(message=f"remove {SECRET}"),
                commit=patched(commit_view(), status="mystery"),
            ),
            forbidden_secrets=(SECRET,),
        )
        assert_invalid(report, "G7")
        assert SECRET not in serialize_report(report)
        assert report.issue_key == ""
        assert any(
            "could not be verified as free of secret material" in message
            for message in report.validation_errors
        )

    def test_a_credential_url_on_the_failure_path_is_never_published(self):
        report = build(
            entry=entry(
                issue_record=issue(message=f"see {CREDENTIAL_URL}"),
                commit=patched(commit_view(), status="mystery"),
            )
        )
        assert_invalid(report, "G7")
        assert "sw0rdf1sh" not in serialize_report(report)
        assert report.issue_key == ""

    def test_a_clean_report_is_not_refused_by_the_secret_gate(self):
        report = build(forbidden_secrets=(SECRET, CREDENTIAL_URL))
        assert report.is_valid is True
        assert report.state.status_of("G21") is GateStatus.PASS


class TestImmutability:
    def test_the_report_is_frozen(self):
        report = build()
        with pytest.raises(FrozenInstanceError):
            report.outcome = PerIssueOutcome.FAILED
        with pytest.raises(FrozenInstanceError):
            report.issue_key = "tampered"

    def test_the_input_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            happy_entry().issue_key = "tampered"

    def test_every_sequence_is_a_tuple(self):
        report = build(entry=entry(issue_status=t19_view(status="mystery")))
        assert isinstance(report.reasons, tuple)
        assert isinstance(report.validation_errors, tuple)
        assert isinstance(report.state.gates, tuple)
        assert isinstance(report.state.stage_records, tuple)
        assert isinstance(report.verification.as_dict(), dict)

    def test_building_never_mutates_the_supplied_evidence(self):
        status = t19_result("fixed")
        commit = commit_result()
        push = push_result()
        record = issue()
        before = (
            status.as_dict(),
            commit.as_dict(),
            push.as_dict(),
            (
                record.key,
                record.rule,
                record.severity,
                record.issue_type,
                record.message,
                record.component,
                record.line,
                record.status,
            ),
        )
        build(
            entry=entry(
                issue_record=record,
                issue_status=status,
                commit=commit,
                push=push,
            )
        )
        after = (
            status.as_dict(),
            commit.as_dict(),
            push.as_dict(),
            (
                record.key,
                record.rule,
                record.severity,
                record.issue_type,
                record.message,
                record.component,
                record.line,
                record.status,
            ),
        )
        assert after == before


class TestDeterminism:
    def test_the_same_evidence_produces_the_same_report(self):
        first = build()
        second = build()
        assert first == second
        assert serialize_report(first) == serialize_report(second)
        assert first.as_dict() == second.as_dict()

    def test_the_canonical_text_is_sorted_and_stable(self):
        text = serialize_report(build())
        assert text == json.dumps(
            json.loads(text), sort_keys=True, separators=(",", ":")
        )

    @pytest.mark.parametrize(
        "suspicious",
        ["timestamp", "generated_at", "created", "uuid", "random", "0x"],
    )
    def test_no_volatile_field_is_published(self, suspicious):
        assert suspicious not in serialize_report(build())

    def test_two_issues_produce_two_different_reports(self):
        first = build(entry=entry(issue_record=issue(message="first problem")))
        second = build(entry=entry(issue_record=issue(message="second problem")))
        assert serialize_report(first) != serialize_report(second)
        assert first.message == "first problem"
        assert second.message == "second problem"
        assert first.issue_key == second.issue_key == ISSUE_KEY

    def test_the_gate_problems_are_deduplicated_and_sorted(self):
        view = t19_view()
        view["verification"]["original_issue_key"] = "AX2"
        view["verification"]["identity_reliable"] = False
        first = build(entry=entry(issue_status=view))
        second = build(entry=entry(issue_status=view))
        assert first.validation_errors == second.validation_errors


class TestStateMachine:
    @pytest.mark.parametrize(
        "label, entry_factory, phase, failed_phase",
        [
            (
                "input",
                lambda: None,
                PerIssueReportPhase.INPUT,
                PerIssueReportPhase.INPUT_VALIDATED,
            ),
            (
                "identity",
                lambda: entry(issue_key=""),
                PerIssueReportPhase.INPUT_VALIDATED,
                PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED,
            ),
            (
                "t19",
                lambda: entry(issue_status=None),
                PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED,
                PerIssueReportPhase.T19_VALIDATED,
            ),
            (
                "t20",
                lambda: entry(
                    commit=patched(commit_view(), status="mystery")
                ),
                PerIssueReportPhase.T19_VALIDATED,
                PerIssueReportPhase.T20_VALIDATED,
            ),
            (
                "t21",
                lambda: entry(push=patched(push_view(), status="mystery")),
                PerIssueReportPhase.T20_VALIDATED,
                PerIssueReportPhase.T21_VALIDATED,
            ),
            (
                "cross-stage",
                lambda: entry(
                    commit=commit_result(status=CommitStatus.REFUSED),
                    push=push_result(),
                ),
                PerIssueReportPhase.T21_VALIDATED,
                PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
            ),
        ],
    )
    def test_every_failure_stops_at_its_own_phase(
        self, label, entry_factory, phase, failed_phase
    ):
        report = build_per_issue_report(entry=entry_factory())
        assert report.state.phase is phase
        assert report.state.failed_phase is failed_phase
        assert report.state.reached_phases == ALL_PHASES[
            : ALL_PHASES.index(phase) + 1
        ]
        assert report.state.is_complete is False
        assert report.state.first_failure is not None

    def test_a_gate_after_a_failure_stays_not_reached(self):
        report = build(entry=entry(issue_status=t19_view(status="mystery")))
        assert failed_gates(report) == ("G5",)
        assert not_reached_gates(report) == (
            "G6",
            "G7",
            "G8",
            "G9",
            "G10",
            "G11",
            "G12",
            "G13",
            "G14",
            "G15",
            "G16",
            "G17",
            "G18",
            "G19",
            "G20",
            "G21",
        )
        for gate_id in not_reached_gates(report):
            gate = report.gate(gate_id)
            assert gate.not_reached is True
            assert gate.failed is False
            assert gate.passed is False
            assert gate.reason == t23._NOT_REACHED_REASON

    def test_a_valid_report_walks_every_phase_in_order(self):
        report = build()
        assert report.state.reached_phases == ALL_PHASES
        assert report.state.phase is PerIssueReportPhase.COMPLETE
        assert report.state.failed_phase is None
        assert report.state.not_reached_gates == ()
        for gate_id, gate in zip(gate_ids(report), report.state.gates):
            assert gate.gate_id == gate_id
            assert gate.passed is True
            assert gate.reason


#: One hostile or corrupt input per fail-closed gate, so every gate is exercised.
#: ``G16``-``G19`` are covered by the derivation seams and ``G20``/``G21`` by the
#: serialization and secret tests, because they cannot be reached by input alone.
GATE_ATTACKS = {
    "G1": lambda: None,
    "G2": lambda: entry(issue_key=""),
    "G3": lambda: entry(issue_key="AX2"),
    "G4": lambda: entry(issue_status={"status": None}),
    "G5": lambda: entry(issue_status=t19_view(status="mystery")),
    "G6": lambda: entry(commit=patched(commit_view(), new_head="zz")),
    "G7": lambda: entry(commit=patched(commit_view(), status="mystery")),
    "G8": lambda: entry(push=patched(push_view(), expected_commit="zz")),
    "G9": lambda: entry(push=patched(push_view(), status="mystery")),
    "G10": lambda: entry(issue_status=patched(t19_view(), is_fixed=False)),
    "G11": lambda: entry(commit=patched(commit_view(), commit_sha=None)),
    "G12": lambda: entry(push=patched(push_view(), is_pushed=False)),
    "G13": lambda: entry(commit=None, push=push_result()),
    "G14": lambda: entry(
        commit=commit_result(),
        push=patched(push_view(), refspec="refs/heads/a:refs/heads/b"),
    ),
    "G15": lambda: entry(
        commit=None, push=push_result(status=PushStatus.REFUSED)
    ),
}


class TestFailClosedGateMatrix:
    @pytest.mark.parametrize("gate_id", sorted(GATE_ATTACKS))
    def test_the_failure_is_attributed_to_exactly_one_gate(self, gate_id):
        report = build_per_issue_report(entry=GATE_ATTACKS[gate_id]())
        assert_invalid(report, gate_id)
        assert failed_gates(report) == (gate_id,)
        assert report.state.failed_phase is not None
        assert report.state.phase in ALL_PHASES

    def test_every_gate_in_the_catalogue_is_exercised(self):
        covered = set(GATE_ATTACKS) | {
            "G16",
            "G17",
            "G18",
            "G19",
            "G20",
            "G21",
        }
        assert covered == {gate_id for gate_id, _ in GATE_CATALOGUE}


@pytest.fixture
def executed_clone(make_remote_repo, rm, clone_destination):
    """Run the *real* T19/T20/T21 stages in a *real* clone under ``tmp_path``.

    Topology (everything local; no network, no SonarQube, no Codex):

    * a bare remote repository and a clone of it, checked out on the agent
      branch with one already-published commit;
    * a real ``determine_issue_status`` result (``FIXED``);
    * a real ``GitCommitExecutor.commit_safely`` commit;
    * a real ``GitPushExecutor.push_safely`` push of that commit.
    """
    remote = make_remote_repo()
    work = rm.clone(str(remote), clone_destination())
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    git_run(work, "remote", "set-head", REMOTE, "-a")
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
    commit = GitCommitExecutor(timeout_seconds=60.0).commit_safely(
        repository_path=work,
        issue_status=status,
        baseline=baseline,
        after=after,
        attribution=attribution,
    )
    pushed = GitPushExecutor(
        config=PushPolicyConfig(default_branch="main")
    ).push_safely(
        repository_path=str(work),
        commit_result=commit,
        remote=REMOTE,
        remote_branch=AGENT_BRANCH,
    )
    return SimpleNamespace(
        remote=remote,
        work=work,
        status=status,
        commit=commit,
        push=pushed,
    )


class TestRealExecutorIntegration:
    """The production contract: real DTOs, produced by the real executors."""

    def test_a_real_t19_t20_t21_run_reports_a_success(self, executed_clone):
        clone = executed_clone
        record = clone.status.verification.original_issue
        assert isinstance(record, SonarIssue)
        before = snapshot(clone.work, remotes=False)

        report = build_per_issue_report(
            entry=PerIssueInput(
                issue_key=record.key,
                issue=record,
                issue_status=clone.status,
                commit_result=clone.commit,
                push_result=clone.push,
            )
        )

        assert report.outcome is PerIssueOutcome.SUCCESS
        assert report.is_valid is True
        assert report.needs_attention is False
        assert report.end_to_end_success is True
        assert report.t19_status == clone.status.status.value
        assert report.t20_status == clone.commit.status.value
        assert report.t21_status == clone.push.status.value
        assert report.issue_key == record.key
        assert report.rule == record.rule
        assert report.file_path == record.file_path
        assert report.line == record.line
        assert report.commit_sha == clone.commit.new_head
        assert report.previous_head == clone.commit.previous_head
        assert report.expected_commit == clone.commit.new_head
        assert report.remote_before_commit == clone.commit.previous_head
        assert report.remote_after_commit == clone.commit.new_head
        assert report.remote == REMOTE
        assert report.branch == AGENT_BRANCH
        assert report.commit_message_issue_key == record.key
        assert verify_per_issue_report(report) == ()
        assert report.state.is_complete is True
        assert failed_gates(report) == ()

        # T23 executed nothing: the clone is exactly where the real T21 left it.
        assert snapshot(clone.work, remotes=False) == before
        assert git_run(clone.work, "rev-parse", "HEAD") == clone.commit.new_head
        assert git_run(clone.work, "branch", "--show-current") == AGENT_BRANCH

    def test_the_executor_views_produce_the_identical_report(self, executed_clone):
        clone = executed_clone
        record = clone.status.verification.original_issue
        objects = build_per_issue_report(
            entry=PerIssueInput(
                issue_key=record.key,
                issue=record,
                issue_status=clone.status,
                commit_result=clone.commit,
                push_result=clone.push,
            )
        )
        views = build_per_issue_report(
            entry={
                "issue_key": record.key,
                "issue": record,
                "issue_status": clone.status.as_dict(),
                "commit_result": clone.commit.as_dict(),
                "push_result": clone.push.as_dict(),
            }
        )
        assert views == objects
        assert serialize_report(views) == serialize_report(objects)

    def test_a_real_refused_push_is_never_reported_as_a_success(
        self, executed_clone
    ):
        """A genuine T21 refusal (the remote already holds the commit)."""
        clone = executed_clone
        record = clone.status.verification.original_issue
        second = GitPushExecutor(
            config=PushPolicyConfig(default_branch="main")
        ).push_safely(
            repository_path=str(clone.work),
            commit_result=clone.commit,
            remote=REMOTE,
            remote_branch=AGENT_BRANCH,
        )
        # The remote already points at the commit, so T21 refuses this second
        # push: the report must say exactly that instead of claiming success.
        assert second.status is not PushStatus.PUSHED
        report = build_per_issue_report(
            entry=PerIssueInput(
                issue_key=record.key,
                issue=record,
                issue_status=clone.status,
                commit_result=clone.commit,
                push_result=second,
            )
        )
        assert report.outcome is not PerIssueOutcome.SUCCESS
        assert report.end_to_end_success is False
        assert report.t21_status == second.status.value
        assert report.needs_attention is True
        assert verify_per_issue_report(report) == ()
