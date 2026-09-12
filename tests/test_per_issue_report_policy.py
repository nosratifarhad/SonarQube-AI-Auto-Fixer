"""T23 structural tests: the gate catalogue, the outcome ladder and the helpers.

These tests pin the *contract* of ``per_issue_report`` rather than one report:
the catalogue and its phase map, the derivation ladder and its independently
recomputed double, the field and payload helpers, the failure-report shape, and
the module surface (including that it imports nothing that could execute
anything).
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError, fields, replace

import pytest

import per_issue_report as module
from commit_message import MAX_TOKEN_LENGTH
from commit_policy import GateStatus
from git_commit import CommitStatus
from git_push import PushStatus
from issue_status import IssueFinalStatus
from per_issue_report import (
    GATE_CATALOGUE,
    GATE_PHASE,
    AnalysisEvidence,
    PerIssueInput,
    PerIssueOutcome,
    PerIssueReport,
    PerIssueReportPhase,
    VerificationEvidence,
    build_per_issue_report,
    resolve_issue_outcome,
    serialize_report,
    verify_per_issue_report,
)

from t23_fixtures import (
    ISSUE_KEY,
    build,
    commit_result,
    commit_view,
    entry,
    happy_entry,
    issue,
    push_result,
    push_view,
    t19_result,
    t19_view,
)

#: The phases in their documented order (the enum's declaration order).
PHASE_ORDER = tuple(PerIssueReportPhase)
#: Every phase that owns at least one gate.
GATED_PHASES = tuple(dict.fromkeys(GATE_PHASE.values()))


class TestGateCatalogue:
    """The catalogue and the phase map are complete and in agreement."""

    def test_every_gate_is_in_the_catalogue_and_the_map(self):
        catalogue_ids = tuple(gate_id for gate_id, _ in GATE_CATALOGUE)
        assert catalogue_ids == tuple(
            f"G{index}" for index in range(1, len(GATE_CATALOGUE) + 1)
        )
        assert len(catalogue_ids) == 21
        assert set(GATE_PHASE) == set(catalogue_ids)
        assert all(title for _, title in GATE_CATALOGUE)

    def test_the_catalogue_titles_match_the_documented_contract(self):
        assert GATE_CATALOGUE[0] == ("G1", "input is a usable per-issue input")
        assert GATE_CATALOGUE[-1] == (
            "G21",
            "report contains no secret material",
        )

    def test_every_phase_owns_a_contiguous_run_of_gates(self):
        blocks = {}
        for gate_id, phase in GATE_PHASE.items():
            blocks.setdefault(phase, []).append(int(gate_id[1:]))
        for phase, numbers in blocks.items():
            assert numbers == list(range(min(numbers), max(numbers) + 1)), (
                f"{phase} owns {numbers}, which is not a contiguous run"
            )

    def test_the_phase_map_runs_in_declaration_order(self):
        indexes = [
            PHASE_ORDER.index(GATE_PHASE[gate_id])
            for gate_id, _ in GATE_CATALOGUE
        ]
        assert indexes == sorted(indexes)

    def test_the_gated_phases_are_real_phases(self):
        assert all(phase in PHASE_ORDER for phase in GATED_PHASES)
        assert PerIssueReportPhase.INPUT not in GATED_PHASES
        assert PerIssueReportPhase.DERIVED_OUTCOME_RESOLVED not in GATED_PHASES
        assert PerIssueReportPhase.COMPLETE not in GATED_PHASES
        assert GATE_PHASE["G1"] is PerIssueReportPhase.INPUT_VALIDATED
        assert GATE_PHASE["G15"] is (
            PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED
        )
        assert GATE_PHASE["G21"] is PerIssueReportPhase.REPORT_VERIFIED

    def test_every_phase_of_a_complete_run_is_walked_in_order(self):
        assert build().state.reached_phases == PHASE_ORDER

    @pytest.mark.parametrize(
        "entry_factory, failed_phase",
        (
            (lambda: None, PerIssueReportPhase.INPUT_VALIDATED),
            (
                lambda: entry(issue_status={"status": "banana"}),
                PerIssueReportPhase.T19_VALIDATED,
            ),
            (
                lambda: entry(commit=commit_view(commit_sha="c" * 40)),
                PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
            ),
        ),
    )
    def test_a_failure_leaves_every_later_phase_gate_unreached(
        self, entry_factory, failed_phase
    ):
        report = build_per_issue_report(entry=entry_factory())
        assert report.state.failed_phase is failed_phase
        failed_index = PHASE_ORDER.index(failed_phase)
        for gate_id, phase in GATE_PHASE.items():
            if PHASE_ORDER.index(phase) > failed_index:
                assert report.state.status_of(gate_id) is (
                    GateStatus.NOT_REACHED
                ), gate_id

    def test_the_failed_gate_is_the_first_one_reported(self):
        report = build(entry=entry(issue_status={"status": "banana"}))
        assert report.state.first_failure.gate_id == "G5"
        assert report.state.first_failure.title == "T19 status recognised"
        assert report.state.first_failure.failed is True
        assert report.state.first_failure.passed is False


#: The derivation ladder, one row per documented branch.
OUTCOME_LADDER = (
    ("fixed", None, None, PerIssueOutcome.NOT_COMPLETED),
    ("fixed", "committed", None, PerIssueOutcome.NOT_COMPLETED),
    ("fixed", "committed", "pushed", PerIssueOutcome.SUCCESS),
    ("fixed", "committed", "refused", PerIssueOutcome.NOT_COMPLETED),
    ("fixed", "committed", "push-failed", PerIssueOutcome.NOT_COMPLETED),
    ("fixed", "committed", "push-unverified", PerIssueOutcome.REVIEW_REQUIRED),
    ("fixed", "commit-unverified", None, PerIssueOutcome.REVIEW_REQUIRED),
    ("fixed", "refused", None, PerIssueOutcome.NOT_COMPLETED),
    ("fixed", "commit-failed", None, PerIssueOutcome.NOT_COMPLETED),
    ("still-open", None, None, PerIssueOutcome.FAILED),
    ("analysis-failed", None, None, PerIssueOutcome.FAILED),
    ("tests-failed", None, None, PerIssueOutcome.FAILED),
    ("scope-invalid", None, None, PerIssueOutcome.FAILED),
    ("codex-failed", None, None, PerIssueOutcome.FAILED),
    ("review-required", None, None, PerIssueOutcome.REVIEW_REQUIRED),
    ("review-required", "committed", "pushed", PerIssueOutcome.REVIEW_REQUIRED),
)


class TestOutcomeResolution:
    """``resolve_issue_outcome`` is the documented, fail-closed ladder."""

    @pytest.mark.parametrize("t19, t20, t21, expected", OUTCOME_LADDER)
    def test_the_ladder_matches_the_documented_branches(
        self, t19, t20, t21, expected
    ):
        assert (
            resolve_issue_outcome(
                is_valid=True, t19_status=t19, t20_status=t20, t21_status=t21
            )
            is expected
        )

    @pytest.mark.parametrize("t19", sorted(module.ISSUE_STATUS_ORDER))
    @pytest.mark.parametrize("t20", sorted(module.COMMIT_STATUS_ORDER))
    def test_success_requires_exactly_the_full_lifecycle(self, t19, t20):
        for t21 in sorted(module.PUSH_STATUS_ORDER):
            outcome = resolve_issue_outcome(
                is_valid=True,
                t19_status=t19,
                t20_status=t20,
                t21_status=t21,
            )
            assert (outcome is PerIssueOutcome.SUCCESS) is (
                t19 == "fixed" and t20 == "committed" and t21 == "pushed"
            ), (t19, t20, t21, outcome)

    @pytest.mark.parametrize("t19", sorted(module.ISSUE_STATUS_ORDER))
    def test_an_unverified_commit_or_push_is_never_a_success(self, t19):
        assert resolve_issue_outcome(
            is_valid=True,
            t19_status=t19,
            t20_status="commit-unverified",
            t21_status="pushed",
        ) in (PerIssueOutcome.REVIEW_REQUIRED, PerIssueOutcome.FAILED)
        assert resolve_issue_outcome(
            is_valid=True,
            t19_status=t19,
            t20_status="committed",
            t21_status="push-unverified",
        ) in (PerIssueOutcome.REVIEW_REQUIRED, PerIssueOutcome.FAILED)

    @pytest.mark.parametrize("t19", sorted(module.ISSUE_STATUS_ORDER))
    def test_an_invalid_report_is_always_review_required(self, t19):
        for t20 in (None, "committed"):
            for t21 in (None, "pushed"):
                assert (
                    resolve_issue_outcome(
                        is_valid=False,
                        t19_status=t19,
                        t20_status=t20,
                        t21_status=t21,
                    )
                    is PerIssueOutcome.REVIEW_REQUIRED
                )

    @pytest.mark.parametrize("t19", sorted(module.ISSUE_STATUS_ORDER))
    def test_a_missing_stage_is_never_a_success(self, t19):
        assert resolve_issue_outcome(
            is_valid=True, t19_status=t19, t20_status=None, t21_status=None
        ) is not PerIssueOutcome.SUCCESS

    def test_every_production_status_is_understood(self):
        assert module.ISSUE_STATUS_ORDER == tuple(
            status.value for status in IssueFinalStatus
        )
        assert module.COMMIT_STATUS_ORDER == tuple(
            status.value for status in CommitStatus
        )
        assert module.PUSH_STATUS_ORDER == tuple(
            status.value for status in PushStatus
        )
        assert module.MAX_ISSUE_KEY_LENGTH == MAX_TOKEN_LENGTH == 100
        assert module.SOURCES == ("T19", "T20", "T21")
        assert module.REPORT_VERSION == "t23.1"


class TestInputAndEvidenceDTOs:
    """The input and evidence DTOs are immutable and self-describing."""

    def test_the_input_dto_is_frozen_and_defaults_to_absent_evidence(self):
        entry_ = PerIssueInput(issue_key=ISSUE_KEY)
        assert entry_.issue is None
        assert entry_.issue_status is None
        assert entry_.commit_result is None
        assert entry_.push_result is None
        with pytest.raises(FrozenInstanceError):
            entry_.issue_key = "other"

    def test_the_input_dto_explains_what_it_carries(self):
        assert PerIssueInput(issue_key=ISSUE_KEY).as_dict() == {
            "issue_key": ISSUE_KEY,
            "has_issue": False,
            "has_issue_status": False,
            "has_commit_result": False,
            "has_push_result": False,
        }
        assert happy_entry().as_dict() == {
            "issue_key": ISSUE_KEY,
            "has_issue": True,
            "has_issue_status": True,
            "has_commit_result": True,
            "has_push_result": True,
        }

    def test_the_evidence_dtos_start_empty(self):
        assert VerificationEvidence().as_dict() == {
            "issue_key": None,
            "retrieval_succeeded": None,
            "is_present": None,
            "identity_reliable": None,
            "page_complete": None,
            "reliable_absence": None,
            "match_type": None,
            "correlation": None,
        }
        assert AnalysisEvidence().as_dict() == {
            "completion_status": None,
            "completion_task_id": None,
            "triggered": None,
            "trigger_task_id": None,
        }

    def test_the_report_evidence_carries_the_real_values(self):
        report = build()
        assert report.verification.as_dict()["match_type"] == "key"
        assert report.analysis.as_dict()["completion_task_id"] == "AY1"


class TestFieldHelpers:
    """The field helpers never repair, guess at or echo a supplied value."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("text", "text"),
            (" padded", None),
            ("", None),
            ("  ", None),
            (7, None),
            (None, None),
        ],
    )
    def test_strict_text(self, value, expected):
        assert module._strict_text(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [("text", "text"), ("", None), ("  ", None), (7, None), (None, None)],
    )
    def test_text(self, value, expected):
        assert module._text(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            (True, True),
            (False, False),
            (1, None),
            (0, None),
            ("true", None),
            (None, None),
        ],
    )
    def test_as_bool(self, value, expected):
        assert module._as_bool(value) is expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("A" * 40, "a" * 40),
            (" " + "A" * 40 + " ", "a" * 40),
            ("short", None),
            ("z" * 40, None),
            (None, None),
            (7, None),
        ],
    )
    def test_full_id(self, value, expected):
        assert module._full_id(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [(3, 3), (0, None), (-1, None), (True, None), (1.5, None), (None, None)],
    )
    def test_positive_int(self, value, expected):
        assert module._positive_int(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [("G1", "G1"), ("G45", "G45"), ("G0", None), ("G99x", None), (7, None)],
    )
    def test_gate_id(self, value, expected):
        assert module._gate_id(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [(None, False), ("", False), (0, True), (False, True)],
    )
    def test_present(self, value, expected):
        assert module._present(value) is expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("rule", "rule"),
            ("x" * 400, "x" * 400),
            ("x" * 401, None),
            ("two\nlines", None),
            ("bell\u0007", None),
            (" padded", None),
            ("", None),
            (7, None),
            (None, None),
        ],
    )
    def test_bounded_text(self, value, expected):
        assert module._bounded_text(value) == expected

    def test_tagged_sorts_and_deduplicates(self):
        problems = (
            ("G10", "b"),
            ("G11", "ignored"),
            ("G10", "a"),
            ("G10", "a"),
        )
        assert module._tagged(problems, "G10") == ("a", "b")
        assert module._tagged(problems, "G99") == ()


class TestReaderHelpers:
    """The readers project only the fields the report publishes."""

    def test_a_minimal_t19_view_reads_as_absent_evidence(self):
        values, problems = module._read_t19({"status": "fixed"})
        assert problems == ()
        assert values["t19_status"] == "fixed"
        assert values["t19_is_fixed"] is None
        assert values["t19_needs_review"] is None
        assert values["verification"] == VerificationEvidence()
        assert values["analysis"] == AnalysisEvidence()
        assert values["t19_scope_valid"] is None
        assert values["t19_scope_modified"] is None
        assert values["t19_tests_passed"] is None
        assert values["t19_codex_succeeded"] is None

    def test_a_minimal_t20_view_reads_as_absent_evidence(self):
        values, problems = module._read_t20({"status": "refused"})
        assert problems == ()
        assert values["t20_status"] == "refused"
        assert values["t20_new_head"] is None
        assert values["t20_commit_sha"] is None
        assert values["t20_previous_head"] is None
        assert values["t20_branch"] is None
        assert values["t20_message_issue_key"] is None
        assert values["t20_gate_failure"] is None
        assert values["t20_is_committed"] is None
        assert values["t20_needs_attention"] is None

    def test_a_minimal_t21_view_reads_as_absent_evidence(self):
        values, problems = module._read_t21({"status": "refused"})
        assert problems == ()
        assert values["t21_status"] == "refused"
        assert values["t21_is_pushed"] is None
        assert values["t21_is_refusal"] is None
        assert values["t21_needs_attention"] is None
        assert values["t21_push_attempted"] is None
        assert values["t21_expected_commit"] is None
        assert values["t21_remote_before_commit"] is None
        assert values["t21_remote_after_commit"] is None
        assert values["t21_branch"] is None
        assert values["t21_remote_branch"] is None
        assert values["t21_refspec"] is None
        assert values["t21_remote_name"] is None
        assert values["t21_remote_exists"] is None
        assert values["t21_gate_failure"] is None

    def test_a_published_gate_failure_is_read_when_it_is_a_gate_id(self):
        values, problems = module._read_t20(
            {"status": "refused", "gates": {"first_failure": "G12"}}
        )
        assert problems == ()
        assert values["t20_gate_failure"] == "G12"

    def test_the_readings_merge_into_one_facts_bundle(self):
        identity, identity_problems = module._identity_projection(issue())
        assert identity_problems == ()
        t19, _ = module._read_t19(t19_result("fixed").as_dict())
        t20, _ = module._read_t20(commit_result().as_dict())
        t21, _ = module._read_t21(push_result().as_dict())
        facts = module._facts(identity, t19, t20, t21)
        assert facts.issue_key == ISSUE_KEY
        assert facts.issue_fixed is True
        assert facts.change_committed is True
        assert facts.change_pushed is True
        assert facts.has_commit is True
        assert facts.has_push is True
        assert facts.push_attempted is True
        assert facts.commit_sha == facts.head_commit == facts.push_commit
        assert facts.t20_needs_attention_flag is False
        assert facts.t21_needs_attention_flag is False

    def test_the_identity_projection_reports_every_problem(self):
        identity, problems = module._identity_projection(
            issue(key="", rule="", component="")
        )
        assert identity is None
        assert len(problems) == 3
        assert all("usable" in problem for problem in problems)

    def test_a_block_without_its_auxiliary_values_is_accepted(self):
        """An absent auxiliary value is "not reported", never a contradiction."""
        view = t19_view()
        del view["verification"]["match_type"]
        del view["verification"]["correlation"]
        del view["analysis"]["status"]
        report = build(
            entry=entry(
                issue_status=view,
                commit=commit_result(),
                push=push_result(),
            )
        )
        assert report.is_valid is True
        assert report.verification.match_type is None
        assert report.verification.correlation is None
        assert report.analysis.completion_status is None


class TestDerivationInternals:
    """The ladder, the attention rule and the independent recomputation."""

    def make_facts(self, *, t19="fixed", t20=None, t21=None, **overrides):
        identity = module._identity_projection(issue())[0]
        values = {"t19_status": t19}
        if t20 is not None:
            values["t20_status"] = t20
        if t21 is not None:
            values["t21_status"] = t21
        values.update(overrides)
        return module._facts(identity, values, None, None)

    @pytest.mark.parametrize(
        "t19, t20, t21, expected",
        (
            ("review-required", None, None, "could not decide"),
            ("fixed", "commit-unverified", None, "commit whose correctness"),
            ("fixed", "committed", "push-unverified", "push whose effect"),
            ("still-open", None, None, "not a verified fix"),
            ("fixed", None, None, "no T20 commit result"),
            ("fixed", "refused", None, "T20 reported 'refused'"),
            ("fixed", "committed", None, "no T21 push result"),
            ("fixed", "committed", "push-failed", "T21 reported 'push-failed'"),
            ("fixed", "committed", "pushed", "verified the push"),
        ),
    )
    def test_the_reason_ladder_explains_every_branch(
        self, t19, t20, t21, expected
    ):
        _outcome, reason = module._classify(
            self.make_facts(t19=t19, t20=t20, t21=t21)
        )
        assert expected in reason

    @pytest.mark.parametrize(
        "outcome, flags, expected",
        (
            (PerIssueOutcome.SUCCESS, {}, False),
            (PerIssueOutcome.FAILED, {}, False),
            (PerIssueOutcome.NOT_COMPLETED, {}, True),
            (PerIssueOutcome.REVIEW_REQUIRED, {}, True),
            (PerIssueOutcome.SUCCESS, {"t20_needs_attention": True}, True),
            (PerIssueOutcome.SUCCESS, {"t21_needs_attention": True}, True),
        ),
    )
    def test_needs_attention_is_fail_closed(self, outcome, flags, expected):
        assert (
            module._needs_attention(self.make_facts(**flags), outcome)
            is expected
        )

    def test_the_reasons_never_copy_upstream_text(self):
        facts = self.make_facts(t19="still-open")
        outcome, reason = module._classify(facts)
        lines = module._reasons(facts, outcome, reason)
        assert lines[0] == (
            "Reported one SonarQube issue ('AX1') with its T19, T20 and T21 "
            "evidence."
        )
        assert lines[1] == (
            "T19 status: 'still-open'; T20 result: 'not supplied'; T21 "
            "result: 'not supplied'."
        )
        assert any("issue_fixed=False" in line for line in lines)
        assert not any("still reported as open" in line for line in lines)

    def test_the_attention_line_is_added_only_when_it_is_needed(self):
        outcome, reason = module._classify(self.make_facts(t19="still-open"))
        assert (
            len(
                module._reasons(
                    self.make_facts(t19="still-open"), outcome, reason
                )
            )
            == 5
        )
        outcome, reason = module._classify(self.make_facts())
        assert len(module._reasons(self.make_facts(), outcome, reason)) == 6

    def test_the_independent_outcome_recomputation_matches(self):
        for entry_ in (
            happy_entry(),
            entry(),
            entry(commit=commit_result()),
            entry(issue_status=t19_result("still-open")),
        ):
            report = build(entry=entry_)
            assert module._expected_outcome(report) is (
                module.resolve_issue_outcome(
                    is_valid=report.is_valid,
                    t19_status=report.t19_status,
                    t20_status=report.t20_status,
                    t21_status=report.t21_status,
                )
            )
            assert (
                module._expected_attention(report) is report.needs_attention
            )


class TestGateRun:
    """The gate bookkeeping always covers the complete catalogue."""

    def test_a_pass_is_recorded_with_its_reason(self):
        run = module._GateRun()
        assert run.record("G1", (), ok_reason="ok") is GateStatus.PASS
        records = run.records()
        assert len(records) == len(GATE_CATALOGUE)
        assert records[0].passed is True
        assert records[0].reason == "ok"
        assert records[0].as_dict() == {
            "gate_id": "G1",
            "title": GATE_CATALOGUE[0][1],
            "status": "pass",
            "reason": "ok",
            "passed": True,
            "failed": False,
        }
        assert records[1].not_reached is True
        assert module._GateRun().records()[0].reason == module._NOT_REACHED_REASON

    def test_problems_fail_the_gate(self):
        run = module._GateRun()
        assert run.record("G1", ("a", "b"), ok_reason="ok") is GateStatus.FAIL
        assert run.records()[0].failed is True
        assert run.records()[0].reason == "a; b"

    def test_a_verdict_can_be_overwritten_and_dropped(self):
        run = module._GateRun()
        run.record("G1", (), ok_reason="ok")
        run.fail("G1", "later")
        assert run.records()[0].reason == "later"
        run.unrecord("G1")
        assert run.records()[0].not_reached is True
        run.unrecord("G99")


def _as_subclass(report, cls):
    """Rebuild ``report`` as ``cls``, keeping every field value."""
    return cls(**{field.name: getattr(report, field.name) for field in fields(report)})


class ExplodingReport(PerIssueReport):
    """A report whose payload cannot be rendered at all."""

    def as_dict(self):
        raise RuntimeError("no payload")


class TuplePayloadReport(PerIssueReport):
    """A report whose payload carries a tuple (JSON turns it into a list)."""

    def as_dict(self):
        payload = super().as_dict()
        payload["generated_from"] = tuple(payload["generated_from"])
        return payload


class TestPayloadHelpers:
    """The serialization and secret gates are executable on their own."""

    def test_a_valid_report_has_no_json_problems(self):
        assert module._json_problems(build()) == ()

    def test_a_payload_with_a_non_json_value_is_reported(self):
        problems = module._json_problems(
            replace(build(), reasons=({1, 2},))
        )
        assert problems
        assert "not JSON data" in problems[0]

    def test_an_unrenderable_report_is_reported(self):
        broken = _as_subclass(build(), ExplodingReport)
        assert module._json_problems(broken) == (
            "The report could not be rendered into a JSON payload.",
        )

    def test_a_payload_that_does_not_round_trip_is_reported(self):
        broken = _as_subclass(build(), TuplePayloadReport)
        assert any(
            "round trip" in problem
            for problem in module._json_problems(broken)
        )

    def test_the_secret_scan_passes_a_clean_report(self):
        assert module._secret_problems(serialize_report(build()), ()) == ()

    def test_the_secret_scan_refuses_a_configured_secret(self):
        problems = module._secret_problems("token s3cr3t", ("s3cr3t",))
        assert problems
        assert "s3cr3t" not in problems[0]

    def test_the_secret_scan_refuses_a_credential_url(self):
        assert module._secret_problems("see https://user:pw@host/x", ())


class TestFailureReportContract:
    """A failure report is explicit, unclassified and self-consistent."""

    def failure(self):
        return build(entry=entry(issue_status=None))

    def test_a_failure_report_never_classifies_anything(self):
        report = self.failure()
        assert report.is_valid is False
        assert report.outcome is PerIssueOutcome.REVIEW_REQUIRED
        assert report.needs_attention is True
        assert report.t19_status is None
        assert report.t20_status is None
        assert report.t21_status is None
        assert report.verification == VerificationEvidence()
        assert report.analysis == AnalysisEvidence()
        assert report.commit_sha is None
        assert report.expected_commit is None
        assert report.commit_result_supplied is False
        assert report.push_result_supplied is False
        assert report.issue_fixed is False
        assert report.change_committed is False
        assert report.change_pushed is False
        assert report.end_to_end_success is False
        assert report.is_fixed is False

    def test_a_failure_report_explains_itself(self):
        report = self.failure()
        assert report.validation_errors
        assert any("G4" in error for error in report.validation_errors)
        assert any("stopped at" in line for line in report.reasons)
        assert report.state.failed_phase is PerIssueReportPhase.T19_VALIDATED
        assert report.state.first_failure is not None

    def test_a_failure_report_keeps_the_validated_identity(self):
        report = self.failure()
        assert report.issue_key == ISSUE_KEY
        assert report.file_path == "src/app.py"
        assert report.rule == "python:S1481"

    def test_a_failure_report_blanks_an_unvalidated_identity(self):
        report = build(entry=None)
        assert report.issue_key == ""
        assert report.rule is None
        assert report.component is None
        assert report.message is None
        assert report.line is None

    def test_a_failure_report_is_internally_consistent(self):
        assert verify_per_issue_report(self.failure()) == ()

    def test_the_blank_identity_is_shared_and_immutable(self):
        assert module._BLANK_IDENTITY.key == ""
        assert module._BLANK_IDENTITY.rule is None
        with pytest.raises(FrozenInstanceError):
            module._BLANK_IDENTITY.key = "x"


class TestMalformedEvidence:
    """Every malformed upstream shape reaches its own fail-closed gate."""

    def failed(self, entry_) -> tuple:
        report = build_per_issue_report(entry=entry_)
        assert report.is_valid is False
        assert report.outcome is PerIssueOutcome.REVIEW_REQUIRED
        return tuple(gate.gate_id for gate in report.state.failed_gates)

    @pytest.mark.parametrize(
        "mutate",
        (
            lambda view: view.update(verification="nope"),
            lambda view: view.update(analysis="nope"),
            lambda view: view.update(scope="nope"),
            lambda view: view.update(tests="nope"),
            lambda view: view.update(codex="nope"),
            lambda view: view.update(is_fixed="yes"),
            lambda view: view.update(needs_review=0),
            lambda view: view["verification"].update(original_issue_key=5),
            lambda view: view["verification"].update(retrieval_succeeded=1),
            lambda view: view["analysis"].update(task_id=5),
            lambda view: view["scope"].update(is_valid=1),
            lambda view: view["tests"].update(passed=1),
            lambda view: view["codex"].update(execution_succeeded=1),
        ),
    )
    def test_a_malformed_t19_block_fails_g4(self, mutate):
        view = t19_view()
        mutate(view)
        assert self.failed(entry(issue_status=view)) == ("G4",)

    @pytest.mark.parametrize(
        "mutate",
        (
            lambda view: view.update(is_committed=1),
            lambda view: view.update(needs_attention="no"),
        ),
    )
    def test_a_malformed_t20_flag_fails_g6(self, mutate):
        view = commit_view()
        mutate(view)
        assert self.failed(entry(commit=view)) == ("G6",)

    @pytest.mark.parametrize(
        "mutate",
        (
            lambda view: view.update(is_pushed=1),
            lambda view: view.update(is_refusal="no"),
            lambda view: view.update(needs_attention=1),
            lambda view: view.update(push_attempted="yes"),
        ),
    )
    def test_a_malformed_t21_flag_fails_g8(self, mutate):
        view = push_view()
        mutate(view)
        assert self.failed(
            entry(commit=commit_result(), push=view)
        ) == ("G8",)


class TestModuleSurface:
    """The public surface is complete and free of execution dependencies."""

    def test_every_exported_name_exists(self):
        assert module.__all__
        for name in module.__all__:
            assert hasattr(module, name), name

    def test_the_public_surface_is_the_documented_one(self):
        assert set(module.__all__) == {
            "ANALYSIS_STATE_ORDER",
            "AnalysisEvidence",
            "COMMIT_STATUS_ORDER",
            "CORRELATION_ORDER",
            "GATE_CATALOGUE",
            "GATE_PHASE",
            "ISSUE_STATUS_ORDER",
            "MATCH_TYPE_ORDER",
            "MAX_IDENTITY_TEXT_LENGTH",
            "MAX_ISSUE_KEY_LENGTH",
            "PUSH_STATUS_ORDER",
            "PerIssueGate",
            "PerIssueInput",
            "PerIssueOutcome",
            "PerIssueReport",
            "PerIssueReportError",
            "PerIssueReportPhase",
            "PerIssueReportState",
            "PerIssueStageRecord",
            "REPORT_VERSION",
            "SOURCES",
            "VerificationEvidence",
            "build_per_issue_report",
            "resolve_issue_outcome",
            "serialize_report",
            "verify_per_issue_report",
        }

    @pytest.mark.parametrize(
        "name",
        (
            "subprocess",
            "os",
            "shutil",
            "socket",
            "time",
            "pathlib",
            "tempfile",
            "requests",
        ),
    )
    def test_no_execution_dependency_is_imported(self, name):
        assert not hasattr(module, name)

    def test_the_module_imports_only_production_evidence_modules(self):
        """The report layer may read evidence and nothing else."""
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert imported == {
            "__future__",
            "json",
            "re",
            "dataclasses",
            "enum",
            "typing",
            "analysis_correlation",
            "commit_message",
            "commit_policy",
            "git_commit",
            "git_push",
            "issue_status",
            "models",
            "push_policy",
            "secret_scan",
            "sonar_analysis_waiter",
            "sonar_issue_verification",
        }

    @pytest.mark.parametrize(
        "forbidden",
        (
            "subprocess",
            "system",
            "Popen",
            "socket",
            "urlopen",
            "open",
            "shutil",
            "tempfile",
            "asdict",
            "__dict__",
            "eval",
            "exec",
        ),
    )
    def test_the_module_code_never_executes_or_introspects(self, forbidden):
        """The module's *code* may not reference any execution primitive.

        The check walks the AST instead of the raw text, so the module docstring
        can keep explaining that T23 executes nothing.
        """
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        referenced = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        assert forbidden not in referenced

    def test_the_report_version_is_stable(self):
        assert module.REPORT_VERSION == "t23.1"
        report = build()
        assert report.report_version == module.REPORT_VERSION
        assert report.generated_from == module.SOURCES
