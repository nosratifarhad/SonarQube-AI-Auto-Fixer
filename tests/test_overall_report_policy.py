"""T22 policy, gate-map and helper unit tests.

``tests/test_overall_report.py`` covers the aggregation contract end to end. This
file pins the parts a reviewer needs to be able to audit mechanically:

* the gate catalogue and the gate -> phase map (and that the map matches the
  phases the pipeline actually walks);
* the policy defaults and their derived evidence requirements;
* the pure helpers the report is built from (rejecting every malformed shape);
* the fail-closed contract of a failure report;
* that T22 has no execution dependency at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import overall_report as module
from commit_policy import GateStatus
from git_commit import CommitStatus
from git_push import PushStatus
from overall_report import (
    GATE_CATALOGUE,
    GATE_PHASE,
    ExpectedEndState,
    IssueLifecycleInput,
    OverallReportPhase,
    OverallReportPolicy,
    OverallStatus,
    ReportDecision,
    build_overall_report,
    serialize_report,
)
from t22_fixtures import (
    COMMIT,
    ISSUE_KEY,
    commit_result,
    commit_view,
    happy_entry,
    issue_entry,
    push_result,
    push_view,
    t19_result,
)

#: The phases in their documented order (the enum's declaration order).
PHASE_ORDER = tuple(OverallReportPhase)
#: Every phase that owns at least one gate.
GATED_PHASES = tuple(dict.fromkeys(GATE_PHASE.values()))


class TestGateCatalogue:
    """The catalogue and the phase map are complete and in agreement."""

    def test_every_gate_is_in_the_catalogue_and_the_map(self):
        catalogue_ids = tuple(gate_id for gate_id, _ in GATE_CATALOGUE)
        assert catalogue_ids == tuple(
            f"G{index}" for index in range(1, len(GATE_CATALOGUE) + 1)
        )
        assert set(GATE_PHASE) == set(catalogue_ids)
        assert all(title for _, title in GATE_CATALOGUE)

    def test_every_phase_owns_a_contiguous_run_of_gates(self):
        blocks = {}
        for gate_id, phase in GATE_PHASE.items():
            blocks.setdefault(phase, []).append(int(gate_id[1:]))
        for phase, numbers in blocks.items():
            assert numbers == list(range(min(numbers), max(numbers) + 1)), (
                f"{phase} owns {numbers}, which is not a contiguous run"
            )
        # The authored gate numbers are not the execution order: the cross-stage
        # gates keep the numbers G10-G14 but run in S7, after the aggregation
        # phases that own G15-G17 - exactly as T20's G28 (STAGING) runs before
        # G29-G37 (REPOSITORY). The phase map is the execution order.
        assert GATE_PHASE["G10"] is OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED
        assert GATE_PHASE["G15"] is OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED

    def test_the_gated_phases_are_real_phases(self):
        assert all(phase in PHASE_ORDER for phase in GATED_PHASES)
        assert OverallReportPhase.INPUT not in GATED_PHASES
        assert OverallReportPhase.COMPLETE not in GATED_PHASES
        assert OverallReportPhase.OVERALL_STATUS_RESOLVED not in GATED_PHASES

    def test_every_phase_of_a_complete_run_is_walked_in_order(self):
        report = build_overall_report(entries=[happy_entry()])
        assert report.state.reached_phases == PHASE_ORDER

    @pytest.mark.parametrize(
        "entries, failed_phase",
        (
            (None, OverallReportPhase.INPUT_VALIDATED),
            (
                [issue_entry(commit=commit_view(commit_sha="c" * 40))],
                OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
            ),
        ),
    )
    def test_a_failure_leaves_every_later_phase_gate_unreached(
        self, entries, failed_phase
    ):
        report = build_overall_report(entries=entries)
        assert report.state.failed_phase is failed_phase
        failed_index = PHASE_ORDER.index(failed_phase)
        for gate_id, phase in GATE_PHASE.items():
            if PHASE_ORDER.index(phase) > failed_index:
                assert report.state.status_of(gate_id) is (
                    GateStatus.NOT_REACHED
                ), gate_id

    def test_the_failed_gate_is_the_first_one_reported(self):
        report = build_overall_report(
            entries=[issue_entry(issue_status={"status": "banana"})]
        )
        assert report.state.first_failure.gate_id == "G5"
        assert report.state.first_failure.title == "T19 status recognised"
        assert report.state.first_failure.failed is True
        assert report.state.first_failure.passed is False


class TestPolicy:
    """The policy is conservative by default and self-describing."""

    def test_the_default_requires_a_pushed_lifecycle(self):
        policy = OverallReportPolicy()
        assert policy.expected_end_state is ExpectedEndState.PUSHED
        assert policy.requires_commit_evidence is True
        assert policy.requires_push_evidence is True

    @pytest.mark.parametrize(
        "end_state, commit_evidence, push_evidence",
        (
            (ExpectedEndState.PUSHED, True, True),
            (ExpectedEndState.COMMITTED, True, False),
            (ExpectedEndState.ISSUE_FIXED, False, False),
        ),
    )
    def test_the_derived_evidence_requirements(
        self, end_state, commit_evidence, push_evidence
    ):
        policy = OverallReportPolicy(expected_end_state=end_state)
        assert policy.requires_commit_evidence is commit_evidence
        assert policy.requires_push_evidence is push_evidence

    def test_the_policy_serializes_deterministically(self):
        policy = OverallReportPolicy(ExpectedEndState.ISSUE_FIXED)
        assert policy.as_dict() == {
            "expected_end_state": "issue-fixed",
            "requires_commit_evidence": False,
            "requires_push_evidence": False,
        }

    def test_an_issue_fixed_policy_still_reviews_uncertainty(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=commit_result(),
                    push=push_result(status=PushStatus.PUSH_UNVERIFIED),
                )
            ],
            policy=OverallReportPolicy(ExpectedEndState.ISSUE_FIXED),
        )
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.review_required_issues == 1


class TestCollections:
    """``_collect_entries`` / ``_coerce_entry`` refuse every unusable shape."""

    def test_a_sequence_is_materialised(self):
        entry = happy_entry()
        assert module._collect_entries([entry]) == (entry,)
        assert module._collect_entries((entry,)) == (entry,)
        assert module._collect_entries([]) == ()

    def test_an_iterable_is_materialised(self):
        entry = happy_entry()
        collected = module._collect_entries(iter([entry]))
        assert collected == (entry,)

    @pytest.mark.parametrize("value", (None, "AX1", b"AX1", 42, {"AX1": None}))
    def test_an_unusable_collection_is_refused(self, value):
        assert module._collect_entries(value) is None

    def test_an_iterable_that_raises_is_refused(self):
        class Hostile:
            def __iter__(self):
                raise RuntimeError("nope")

        assert module._collect_entries(Hostile()) is None

    def test_an_entry_object_passes_through(self):
        entry = happy_entry()
        assert module._coerce_entry(entry) is entry

    def test_a_complete_mapping_is_coerced(self):
        entry = module._coerce_entry(
            {
                "issue_key": ISSUE_KEY,
                "issue_status": t19_result("fixed"),
                "commit_result": commit_result(),
                "push_result": push_result(),
            }
        )
        assert isinstance(entry, IssueLifecycleInput)
        assert entry.issue_key == ISSUE_KEY
        assert entry.commit_result is not None

    def test_a_mapping_with_an_unknown_field_is_refused(self):
        assert module._coerce_entry({"issue_key": "AX1", "extra": 1}) is None

    def test_a_mapping_without_a_key_is_coerced_but_rejected_later(self):
        entry = module._coerce_entry({"issue_status": t19_result("fixed")})
        assert entry is not None
        assert entry.issue_key is None

    @pytest.mark.parametrize("value", (None, 42, "AX1", ["AX1"]))
    def test_a_non_mapping_is_refused(self, value):
        assert module._coerce_entry(value) is None

    def test_a_mapping_that_raises_is_refused(self):
        class Hostile(dict):
            def __iter__(self):
                raise RuntimeError("nope")

        assert module._coerce_entry(Hostile(issue_key="AX1")) is None


class TestFieldHelpers:
    """The value readers never repair, coerce or guess a value."""

    @pytest.mark.parametrize(
        "value, expected",
        (
            ("fixed", "fixed"),
            ("", None),
            (" fixed", None),
            ("fixed ", None),
            (42, None),
            (None, None),
            (["fixed"], None),
        ),
    )
    def test_strict_text(self, value, expected):
        assert module._strict_text(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            ("origin", "origin"),
            ("", None),
            (" ", None),
            (42, None),
            (None, None),
        ),
    )
    def test_text(self, value, expected):
        assert module._text(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            (True, True),
            (False, False),
            (1, None),
            ("true", None),
            (None, None),
        ),
    )
    def test_as_bool(self, value, expected):
        assert module._as_bool(value) is expected

    def test_full_id(self):
        assert module._full_id(COMMIT) == COMMIT
        assert module._full_id(COMMIT.upper()) == COMMIT
        assert module._full_id(f"  {COMMIT}  ") == COMMIT
        assert module._full_id("deadbeef") is None
        assert module._full_id("") is None
        assert module._full_id(None) is None
        assert module._full_id(42) is None

    def test_present(self):
        assert module._present("x") is True
        assert module._present("") is False
        assert module._present(None) is False

    def test_view_of_a_mapping(self):
        payload = commit_view()
        assert module._view(payload) is payload

    def test_view_of_an_object_with_as_dict(self):
        assert module._view(commit_result())["status"] == "committed"

    @pytest.mark.parametrize(
        "value",
        (
            None,
            42,
            "text",
            object(),
        ),
    )
    def test_view_of_an_unusable_object(self, value):
        assert module._view(value) is None

    def test_view_of_a_raising_as_dict(self):
        class Hostile:
            def as_dict(self):
                raise RuntimeError("nope")

        assert module._view(Hostile()) is None

    def test_view_of_an_as_dict_that_is_not_a_mapping(self):
        class Odd:
            def as_dict(self):
                return ["not", "a", "mapping"]

        assert module._view(Odd()) is None



class TestPayloadHelpers:
    """The serialization and secret gates are exact and fail closed."""

    def test_unserializable_accepts_plain_json_data(self):
        assert module._unserializable(
            {"a": [1, None, True, "x"], "b": {"c": 1.5}}
        ) is None

    @pytest.mark.parametrize(
        "value",
        (object(), {1: "text"}, {"a": {1, 2}}, (object(),), b"bytes"),
    )
    def test_unserializable_finds_the_offending_path(self, value):
        problem = module._unserializable(value)
        assert problem is not None
        assert "payload" in problem

    def test_json_problems_accepts_a_healthy_report(self):
        report = build_overall_report(entries=[happy_entry()])
        assert module._json_problems(report) == ()

    def test_json_problems_rejects_a_non_json_field(self):
        report = build_overall_report(entries=[happy_entry()])
        broken = replace(report, reasons=({1, 2},))
        problems = module._json_problems(broken)
        assert problems and "not JSON data" in problems[0]

    def test_json_problems_rejects_an_unrenderable_report(self):
        class Hostile:
            def as_dict(self):
                raise RuntimeError("nope")

        assert module._json_problems(Hostile()) == (
            "The report could not be rendered into a JSON payload.",
        )

    def test_secret_problems_accepts_a_clean_report(self):
        report = build_overall_report(entries=[happy_entry()])
        assert module._secret_problems(serialize_report(report), ()) == ()

    def test_secret_problems_finds_a_configured_secret(self):
        report = build_overall_report(entries=[happy_entry()])
        problems = module._secret_problems(serialize_report(report), (COMMIT,))
        assert problems
        assert COMMIT not in problems[0]
        assert "configured-secret" in problems[0]

    def test_secret_problems_refuses_text_it_cannot_scan(self):
        problems = module._secret_problems(42, ())
        assert problems
        assert "free of secret" in problems[0]

    def test_count_problems(self):
        assert module._count_problems("T19", 2, 2) == ()
        assert "T19" in module._count_problems("T19", 2, 3)[0]

    def test_ordered_counts_fills_and_orders(self):
        counts = module._ordered_counts({"fixed": 2}, module.ISSUE_STATUS_ORDER)
        assert tuple(counts) == module.ISSUE_STATUS_ORDER
        assert counts["fixed"] == 2
        assert counts["still-open"] == 0

    def test_tagged_sorts_and_de_duplicates(self):
        tagged = (("G4", "b"), ("G4", "a"), ("G4", "a"), ("G5", "c"))
        assert module._tagged(tagged, "G4") == ("a", "b")
        assert module._tagged(tagged, "G5") == ("c",)
        assert module._tagged(tagged, "G6") == ()

    def test_counts_phrase_is_deterministic(self):
        phrase = module._counts_phrase(
            {"committed": 1}, module.COMMIT_STATUS_ORDER
        )
        assert phrase == (
            "committed=1, refused=0, commit-failed=0, commit-unverified=0"
        )

    def test_identity_problems_report_duplicates(self):
        entries = (happy_entry(), happy_entry(issue_key=ISSUE_KEY))
        problems = module._identity_problems(entries)
        assert problems["G2"] == ()
        assert len(problems["G3"]) == 1

    def test_identity_problems_report_every_shape(self):
        entries = (
            None,
            IssueLifecycleInput(issue_key=""),
            IssueLifecycleInput(issue_key=" AX1"),
            IssueLifecycleInput(issue_key="AX1/2"),
        )
        problems = module._identity_problems(entries)
        assert len(problems["G2"]) == 4



class TestFailureReportContract:
    """A failure report is explicit, unclassified and self-consistent."""

    def failure(self):
        return build_overall_report(entries=[happy_entry(), happy_entry()])

    def test_a_failure_report_never_classifies_anything(self):
        report = self.failure()
        assert report.is_valid is False
        assert report.status is OverallStatus.REVIEW_REQUIRED
        assert report.total_issues == 0
        assert report.issue_outcomes == ()
        assert report.successful_issues == 0
        assert report.failed_issues == 0
        assert report.review_required_issues == 0
        assert set(report.issue_counts.values()) == {0}
        assert set(report.commit_counts.values()) == {0}
        assert set(report.push_counts.values()) == {0}
        assert report.commit_result_count == 0
        assert report.push_result_count == 0

    def test_a_failure_report_keeps_the_input_size(self):
        assert self.failure().input_entries == 2

    def test_a_failure_report_explains_itself(self):
        report = self.failure()
        assert report.decision is ReportDecision.REVIEW_REQUIRED
        assert report.needs_attention is True
        assert report.validation_errors
        assert any("G3" in error for error in report.validation_errors)
        assert any("stopped at" in line for line in report.reasons)

    def test_a_failure_report_is_internally_consistent(self):
        assert module.verify_overall_report(self.failure()) == ()

    def test_one_bad_entry_invalidates_the_whole_report(self):
        report = build_overall_report(
            entries=[happy_entry(), issue_entry(issue_status={"status": "?"})]
        )
        assert report.input_entries == 2
        assert report.total_issues == 0
        assert report.issue_outcomes == ()
        assert report.is_valid is False


class TestModuleSurface:
    """The public surface is complete and free of execution dependencies."""

    def test_every_exported_name_exists(self):
        assert module.__all__
        for name in module.__all__:
            assert hasattr(module, name), name

    @pytest.mark.parametrize(
        "name", ("subprocess", "os", "shutil", "socket", "time", "pathlib")
    )
    def test_no_execution_dependency_is_imported(self, name):
        assert not hasattr(module, name)

    def test_the_report_version_is_stable(self):
        assert module.REPORT_VERSION == "t22.1"
        report = build_overall_report(entries=[])
        assert report.report_version == module.REPORT_VERSION
        assert report.generated_from == module.SOURCES



class TestMalformedEvidence:
    """Every malformed upstream shape reaches its own fail-closed gate."""

    def failed(self, entries):
        report = build_overall_report(entries=entries)
        assert report.is_valid is False
        assert report.status is OverallStatus.REVIEW_REQUIRED
        return tuple(item.gate_id for item in report.state.failed_gates)

    def committed(self, status="committed"):
        return commit_result(status=CommitStatus(status))

    def test_a_missing_t19_result(self):
        entry = IssueLifecycleInput(issue_key=ISSUE_KEY, issue_status=None)
        assert self.failed([entry]) == ("G4",)

    def test_a_t19_result_that_cannot_be_read(self):
        assert self.failed([issue_entry(issue_status=object())]) == ("G4",)

    def test_a_t20_result_that_cannot_be_read(self):
        assert self.failed([issue_entry(commit=object())]) == ("G6",)

    def test_a_t20_result_without_a_status(self):
        assert self.failed([issue_entry(commit=commit_view(status=""))]) == (
            "G6",
        )

    def test_a_t20_result_without_an_is_committed_flag(self):
        view = {
            name: value
            for name, value in commit_view().items()
            if name != "is_committed"
        }
        assert self.failed([issue_entry(commit=view)]) == ("G6",)

    def test_a_t20_result_with_a_non_boolean_needs_attention_flag(self):
        assert self.failed(
            [issue_entry(commit=commit_view(needs_attention="maybe"))]
        ) == ("G6",)

    def test_a_t20_result_with_an_unusable_commit_id(self):
        assert self.failed(
            [issue_entry(commit=commit_view(new_head="deadbeef"))]
        ) == ("G6",)

    def test_a_t21_result_that_cannot_be_read(self):
        assert self.failed([issue_entry(push=object())]) == ("G8",)

    def test_a_t21_result_without_a_status(self):
        assert self.failed([issue_entry(push=push_view(status=""))]) == ("G8",)

    def test_a_t21_result_with_a_non_boolean_flag(self):
        assert self.failed([issue_entry(push=push_view(is_pushed="yes"))]) == (
            "G8",
        )

    def test_a_t21_result_with_an_unusable_commit_id(self):
        assert self.failed(
            [issue_entry(push=push_view(expected_commit="deadbeef"))]
        ) == ("G8",)

    def test_a_t21_result_with_an_unusable_text_field(self):
        assert self.failed([issue_entry(push=push_view(branch=42))]) == ("G8",)

    def test_a_t20_result_without_a_commit_message(self):
        """A partial T20 view simply carries no message key to cross-check."""
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=commit_view(commit_message=None), push=push_view()
                )
            ]
        )
        assert report.status is OverallStatus.SUCCESS

    def test_a_verified_push_without_the_commit_it_pushed(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed(),
                    push=push_view(
                        expected_commit=None, remote_after_commit=None
                    ),
                )
            ]
        ) == ("G11",)

    def test_a_committed_t20_without_an_id_is_still_checked_against_the_push(self):
        report = build_overall_report(
            entries=[
                issue_entry(
                    commit=commit_view(new_head=None, commit_sha=None),
                    push=push_view(),
                )
            ]
        )
        assert tuple(item.gate_id for item in report.state.failed_gates) == (
            "G10",
        )

    def test_a_refusal_that_claims_itself_refused_incorrectly(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed("refused"),
                    push=push_view(
                        status="refused",
                        is_pushed=False,
                        is_refusal=False,
                        push_attempted=False,
                        remote_after_commit=None,
                    ),
                )
            ]
        ) == ("G11",)

    def test_a_t21_result_with_a_mismatched_needs_attention_flag(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed(),
                    push=push_view(
                        status="push-failed",
                        needs_attention=False,
                        push_attempted=True,
                        remote_after_commit=None,
                    ),
                )
            ]
        ) == ("G11",)

    def test_a_verified_push_that_was_not_attempted(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed(),
                    push=push_view(push_attempted=False),
                )
            ]
        ) == ("G11",)

    def test_an_unverified_push_that_was_not_attempted(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed(),
                    push=push_view(
                        status="push-unverified",
                        is_pushed=False,
                        push_attempted=False,
                        remote_after_commit=None,
                    ),
                )
            ]
        ) == ("G11",)

    def test_an_unverified_push_without_the_commit_it_pushed(self):
        assert self.failed(
            [
                issue_entry(
                    commit=self.committed(),
                    push=push_view(
                        status="push-unverified",
                        is_pushed=False,
                        expected_commit=None,
                        remote_after_commit=None,
                    ),
                )
            ]
        ) == ("G11",)

    def test_a_push_attempt_without_a_usable_t20_commit_id(self):
        assert self.failed(
            [
                issue_entry(
                    commit=commit_view(
                        status="commit-failed", new_head="deadbeef"
                    ),
                    push=push_view(),
                )
            ]
        ) == ("G6",)

    def test_a_push_attempt_although_t20_did_not_verify_a_commit(self):
        assert self.failed(
            [
                issue_entry(
                    commit=commit_view(
                        status="commit-failed",
                        new_head=COMMIT,
                        commit_sha=COMMIT,
                    ),
                    push=push_view(),
                )
            ]
        ) == ("G13",)



class TestVerificationBranches:
    """Every self-check branch fires when a report is corrupted by hand."""

    def healthy(self):
        return build_overall_report(entries=[happy_entry()])

    def empty(self):
        return build_overall_report(entries=[])

    def test_more_classified_than_collected(self):
        problems = module.verify_overall_report(
            replace(self.healthy(), input_entries=0)
        )
        assert any("More issues were classified" in item for item in problems)

    def test_an_empty_report_with_collected_entries(self):
        problems = module.verify_overall_report(
            replace(self.empty(), input_entries=3)
        )
        assert any("must not carry collected entries" in item for item in problems)

    def test_a_valid_report_with_no_entries_must_be_empty(self):
        problems = module.verify_overall_report(
            replace(self.empty(), status=OverallStatus.SUCCESS)
        )
        assert any(
            "must be EMPTY" in item for item in problems
        )

    def test_counts_that_are_not_a_mapping(self):
        problems = module.verify_overall_report(
            replace(self.healthy(), issue_counts=[1, 2])
        )
        assert any("not a mapping" in item for item in problems)

    def test_duplicate_issue_keys_in_the_summaries(self):
        report = self.healthy()
        summary = report.issue_outcomes[0]
        problems = module.verify_overall_report(
            replace(report, issue_outcomes=(summary, summary))
        )
        assert any("repeat an issue key" in item for item in problems)

    def test_the_states_decision_must_agree(self):
        report = self.healthy()
        problems = module.verify_overall_report(
            replace(
                report,
                state=replace(
                    report.state, decision=ReportDecision.REVIEW_REQUIRED
                ),
            )
        )
        assert any("state's decision" in item for item in problems)

    def test_a_valid_report_must_not_have_a_failed_phase(self):
        report = self.healthy()
        problems = module.verify_overall_report(
            replace(
                report,
                state=replace(
                    report.state, failed_phase=OverallReportPhase.INPUT
                ),
            )
        )
        assert any("failed phase" in item for item in problems)

    def test_a_gate_list_that_does_not_match_the_catalogue(self):
        report = self.healthy()
        problems = module.verify_overall_report(
            replace(report, state=replace(report.state, gates=()))
        )
        assert any("gate catalogue" in item for item in problems)

    def test_a_broken_t20_executed_count_invariant(self):
        problems = module.verify_overall_report(
            replace(self.healthy(), push_not_executed_count=2)
        )
        assert any("T21 executed" in item for item in problems)

    def test_a_buggy_commit_count_is_caught_by_g16(self, monkeypatch):
        monkeypatch.setattr(
            module,
            "_commit_counts",
            lambda facts: {name: 0 for name in module.COMMIT_STATUS_ORDER},
        )
        report = build_overall_report(entries=[happy_entry()])
        assert tuple(item.gate_id for item in report.state.failed_gates) == (
            "G16",
        )
        assert report.state.failed_phase is (
            OverallReportPhase.COMMIT_OUTCOMES_AGGREGATED
        )

    def test_a_buggy_push_count_is_caught_by_g17(self, monkeypatch):
        monkeypatch.setattr(
            module,
            "_push_counts",
            lambda facts: {name: 0 for name in module.PUSH_STATUS_ORDER},
        )
        report = build_overall_report(entries=[happy_entry()])
        assert tuple(item.gate_id for item in report.state.failed_gates) == (
            "G17",
        )
        assert report.state.failed_phase is (
            OverallReportPhase.PUSH_OUTCOMES_AGGREGATED
        )

    def test_a_serializer_that_raises_is_a_g19_failure(self, monkeypatch):
        def explode(report):
            raise RuntimeError("nope")

        monkeypatch.setattr(module, "serialize_report", explode)
        problems = module._json_problems(self.healthy())
        assert problems == (
            "The report payload could not be serialized to JSON.",
        )

    def test_a_serializer_that_returns_non_json_is_a_g19_failure(self, monkeypatch):
        monkeypatch.setattr(module, "serialize_report", lambda report: "{{{")
        problems = module._json_problems(self.healthy())
        assert problems == (
            "The serialized report could not be parsed back as JSON.",
        )

    def test_a_payload_that_does_not_round_trip_is_a_g19_failure(self, monkeypatch):
        monkeypatch.setattr(module, "serialize_report", lambda report: "{}")
        problems = module._json_problems(self.healthy())
        assert problems == (
            "The report payload does not survive a JSON round trip.",
        )

    def test_the_input_dto_serializes(self):
        entry = happy_entry()
        assert entry.as_dict() == {
            "issue_key": ISSUE_KEY,
            "has_issue_status": True,
            "has_commit_result": True,
            "has_push_result": True,
        }

    def test_a_raising_secret_iterable_raises(self):
        def secrets():
            raise RuntimeError("nope")
            yield "never"

        with pytest.raises(module.OverallReportError):
            build_overall_report(
                entries=[happy_entry()], forbidden_secrets=secrets()
            )

