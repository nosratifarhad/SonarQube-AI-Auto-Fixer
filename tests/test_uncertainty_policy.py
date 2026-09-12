"""T27 unit tests: the pure, fail-closed "no mutation when uncertain" policy.

This file pins the whole contract of ``uncertainty_policy``:

* the tables - every enum value, the exact required-evidence policy per phase,
  the contradiction pairs, ``PRECEDENCE``, ``ALLOWED_STATUSES`` and
  ``REVIEW_STATUSES``;
* the one rule - a mutation is allowed only when every dimension the phase
  requires is ``CONFIRMED``, and every other state (missing, unknown,
  unverified, ambiguous, negative, contradictory, invalid) refuses;
* the mandated matrix - the T19/T20/T21/T24/T25/T26 cases (an unknown issue
  outcome, a failed Codex run, an invalid change scope, failed tests, an
  incomplete analysis, an unverified issue verification, a missing correlation,
  refused/unknown branch protection, an unverified commit, contradictory commit
  evidence, a missing worktree/scope record, an unknown push result and an
  unverified remote tip) all refuse;
* the exact ``PRECEDENCE`` with one conflict per adjacent pair, including the
  documented hard cases (a missing dimension beats a contradiction, and a
  confirmed claim with a present-but-unconfirmed proof is a contradiction rather
  than that proof's own doubt state);
* "negative is not uncertain" - a known failure, a doubt, an unreadable call and
  a contradiction stay distinguishable through the status, the blocking state
  and ``requires_review``;
* fail-closed input handling - an unknown phase, a non-tuple evidence field, an
  unknown dimension or state, a duplicate dimension and an ill-formed diagnostic
  code all refuse, and nothing is ever repaired, trimmed or inferred;
* determinism, purity, canonical ordering, caller-value safety, frozen records
  and the exact, JSON-native, secret-free ``as_dict()`` serialization;
* the module surface: standard library only (no repository module, so no
  T19-T26 dependency), no I/O, no command, no mutating API and "not wired".

The tests never touch Git, the network, a subprocess or the filesystem.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import types
from dataclasses import FrozenInstanceError

import pytest

import uncertainty_policy as module
from uncertainty_policy import (
    ALLOWED_STATUSES,
    CONTRADICTION_PAIRS,
    POLICY_VERSION,
    PRECEDENCE,
    REQUIRED_DIMENSIONS,
    REVIEW_STATUSES,
    EvidenceDimension,
    EvidenceItem,
    EvidenceState,
    MutationPhase,
    UncertaintyDecision,
    UncertaintyEvaluation,
    UncertaintyInput,
    UncertaintyStatus,
    evaluate_uncertainty,
)

#: Behavioural aliases, so the tests read as policy rather than as plumbing.
D = EvidenceDimension
E = EvidenceState
P = MutationPhase
S = UncertaintyStatus

PRE = P.PRE_COMMIT
POST_COMMIT = P.POST_COMMIT
PRE_PUSH = P.PRE_PUSH
POST_PUSH = P.POST_PUSH

CONFIRMED = E.CONFIRMED
MISSING = E.MISSING
UNVERIFIED = E.UNVERIFIED
UNKNOWN = E.UNKNOWN
AMBIGUOUS = E.AMBIGUOUS
NEGATIVE = E.NEGATIVE
CONTRADICTORY = E.CONTRADICTORY
INVALID = E.INVALID

#: Every state that must refuse, in the order T27 documents them.
REFUSING_STATES = (
    MISSING,
    CONTRADICTORY,
    NEGATIVE,
    UNKNOWN,
    UNVERIFIED,
    AMBIGUOUS,
    INVALID,
)

#: The state -> status mapping for a dimension that is *only* a claim, so a
#: single non-confirmed state on that dimension is reported as that state.
CLAIM_STATE_STATUS = {
    MISSING: S.MISSING_EVIDENCE,
    CONTRADICTORY: S.CONTRADICTORY_EVIDENCE,
    NEGATIVE: S.NEGATIVE_EVIDENCE,
    UNKNOWN: S.UNKNOWN_EVIDENCE,
    UNVERIFIED: S.UNVERIFIED_EVIDENCE,
    AMBIGUOUS: S.AMBIGUOUS_EVIDENCE,
    INVALID: S.INVALID_INPUT,
}

#: The dimensions no other dimension depends on, so they are never the *proof*
#: of a confirmed claim and can be judged on their own state.
CLAIM_DIMENSIONS = (
    D.ISSUE_OUTCOME,
    D.CODEX_EXECUTION,
    D.CHANGE_SCOPE,
    D.TESTS,
    D.SONAR_ANALYSIS,
    D.CROSS_STAGE_CONSISTENCY,
    D.BRANCH_PROTECTION,
    D.COMMIT_RESULT,
    D.BRANCH_CONSISTENCY,
    D.PUSH_RESULT,
)

#: The dimensions that a confirmed claim depends on, so a present-but-
#: unconfirmed state on one of them is a contradiction.
PROOF_DIMENSIONS = (
    D.ISSUE_VERIFICATION,
    D.ANALYSIS_CORRELATION,
    D.COMMIT_IDENTITY,
    D.COMMIT_REPOSITORY,
    D.REMOTE_TIP,
    D.EXPECTED_COMMIT,
)

#: The phase -> number of required dimensions, for the readable assertions.
REQUIRED_COUNTS = {PRE: 9, POST_COMMIT: 12, PRE_PUSH: 13, POST_PUSH: 16}

#: A caller-authored decoy that must never appear in a T27 reason or output.
DECOY = "S3CRET-DECOY-TOKEN-8f2a"

#: A state-like value that is truthy: T27 must refuse it, not believe it.
class _AlwaysTruthy:
    def __bool__(self) -> bool:
        return True


def evidence(phase: MutationPhase, **overrides: EvidenceState):
    """A complete evidence tuple for ``phase``: all confirmed, but overridden.

    ``overrides`` is keyed by the dimension *value* (``tests=NEGATIVE``), and an
    unknown key is an error rather than a silently ignored dimension.
    """
    items = []
    for dimension in REQUIRED_DIMENSIONS[phase]:
        state = overrides.pop(dimension.value, CONFIRMED)
        items.append(EvidenceItem(dimension, state))
    assert not overrides, f"unknown dimension(s): {sorted(overrides)}"
    return tuple(items)


def evaluate(phase: MutationPhase = PRE, items=()) -> UncertaintyEvaluation:
    """Evaluate one T27 input."""
    return evaluate_uncertainty(
        uncertainty_input=UncertaintyInput(phase=phase, evidence=items)
    )


def verdict(phase: MutationPhase = PRE, **overrides) -> UncertaintyEvaluation:
    """Evaluate a complete, all-confirmed record with per-dimension overrides."""
    return evaluate(phase, evidence(phase, **overrides))


# ---------------------------------------------------------------------------
# The pinned tables
# ---------------------------------------------------------------------------


def test_the_policy_version_is_pinned():
    assert POLICY_VERSION == "t27.1"


def test_the_evidence_state_values_are_pinned():
    assert {state.value for state in EvidenceState} == {
        "confirmed",
        "missing",
        "unverified",
        "unknown",
        "ambiguous",
        "negative",
        "contradictory",
        "invalid",
    }


def test_the_evidence_dimension_values_are_pinned():
    assert {dimension.value for dimension in EvidenceDimension} == {
        "issue-outcome",
        "codex-execution",
        "change-scope",
        "tests",
        "sonar-analysis",
        "issue-verification",
        "analysis-correlation",
        "cross-stage-consistency",
        "branch-protection",
        "commit-result",
        "commit-identity",
        "commit-repository",
        "branch-consistency",
        "push-result",
        "remote-tip",
        "expected-commit",
    }


def test_the_mutation_phase_values_are_pinned():
    assert {phase.value for phase in MutationPhase} == {
        "pre-commit",
        "post-commit",
        "pre-push",
        "post-push",
    }


def test_the_status_values_are_pinned():
    assert {status.value for status in UncertaintyStatus} == {
        "allowed",
        "invalid-policy",
        "invalid-input",
        "missing-evidence",
        "contradictory-evidence",
        "negative-evidence",
        "unknown-evidence",
        "unverified-evidence",
        "ambiguous-evidence",
    }


def test_the_decision_values_are_pinned():
    assert {decision.value for decision in UncertaintyDecision} == {
        "allow",
        "refuse",
    }


def test_the_precedence_is_the_documented_order():
    assert PRECEDENCE == (
        S.INVALID_POLICY,
        S.INVALID_INPUT,
        S.MISSING_EVIDENCE,
        S.CONTRADICTORY_EVIDENCE,
        S.NEGATIVE_EVIDENCE,
        S.UNKNOWN_EVIDENCE,
        S.UNVERIFIED_EVIDENCE,
        S.AMBIGUOUS_EVIDENCE,
        S.ALLOWED,
    )
    assert set(PRECEDENCE) == set(UncertaintyStatus)
    assert len(PRECEDENCE) == len(set(PRECEDENCE))
    assert PRECEDENCE[0] is S.INVALID_POLICY
    assert PRECEDENCE[-1] is S.ALLOWED


def test_the_allowed_and_review_statuses_are_pinned():
    assert ALLOWED_STATUSES == (S.ALLOWED,)
    assert REVIEW_STATUSES == (
        S.MISSING_EVIDENCE,
        S.UNKNOWN_EVIDENCE,
        S.UNVERIFIED_EVIDENCE,
        S.AMBIGUOUS_EVIDENCE,
    )
    assert set(ALLOWED_STATUSES) <= set(PRECEDENCE)
    assert set(REVIEW_STATUSES) <= set(PRECEDENCE)
    assert set(ALLOWED_STATUSES) & set(REVIEW_STATUSES) == set()
    for known in (S.NEGATIVE_EVIDENCE, S.CONTRADICTORY_EVIDENCE):
        assert known not in REVIEW_STATUSES


def test_the_required_evidence_table_is_exact():
    assert REQUIRED_DIMENSIONS[PRE] == (
        D.ISSUE_OUTCOME,
        D.CODEX_EXECUTION,
        D.CHANGE_SCOPE,
        D.TESTS,
        D.SONAR_ANALYSIS,
        D.ISSUE_VERIFICATION,
        D.ANALYSIS_CORRELATION,
        D.CROSS_STAGE_CONSISTENCY,
        D.BRANCH_PROTECTION,
    )
    assert REQUIRED_DIMENSIONS[POST_COMMIT] == REQUIRED_DIMENSIONS[PRE] + (
        D.COMMIT_RESULT,
        D.COMMIT_IDENTITY,
        D.COMMIT_REPOSITORY,
    )
    assert REQUIRED_DIMENSIONS[PRE_PUSH] == (
        REQUIRED_DIMENSIONS[POST_COMMIT] + (D.BRANCH_CONSISTENCY,)
    )
    assert REQUIRED_DIMENSIONS[POST_PUSH] == REQUIRED_DIMENSIONS[PRE_PUSH] + (
        D.PUSH_RESULT,
        D.REMOTE_TIP,
        D.EXPECTED_COMMIT,
    )
    assert set(REQUIRED_DIMENSIONS) == set(MutationPhase)
    assert {
        phase: len(REQUIRED_DIMENSIONS[phase]) for phase in MutationPhase
    } == REQUIRED_COUNTS


def test_each_phase_extends_the_previous_phase():
    phases = (PRE, POST_COMMIT, PRE_PUSH, POST_PUSH)
    for earlier, later in zip(phases, phases[1:]):
        earlier_dims = REQUIRED_DIMENSIONS[earlier]
        later_dims = REQUIRED_DIMENSIONS[later]
        assert len(later_dims) > len(earlier_dims)
        assert later_dims[: len(earlier_dims)] == earlier_dims


def test_the_contradiction_pairs_are_pinned():
    assert CONTRADICTION_PAIRS == (
        (D.ISSUE_OUTCOME, D.ISSUE_VERIFICATION),
        (D.ISSUE_OUTCOME, D.ANALYSIS_CORRELATION),
        (D.COMMIT_RESULT, D.COMMIT_IDENTITY),
        (D.COMMIT_RESULT, D.COMMIT_REPOSITORY),
        (D.PUSH_RESULT, D.REMOTE_TIP),
        (D.PUSH_RESULT, D.EXPECTED_COMMIT),
    )
    assert len(CONTRADICTION_PAIRS) == len(set(CONTRADICTION_PAIRS))
    for claim, proof in CONTRADICTION_PAIRS:
        assert claim is not proof
        assert any(
            claim in REQUIRED_DIMENSIONS[phase]
            and proof in REQUIRED_DIMENSIONS[phase]
            for phase in MutationPhase
        )


# ---------------------------------------------------------------------------
# The one rule: every required dimension must be CONFIRMED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", list(MutationPhase))
def test_every_phase_allows_when_all_required_evidence_is_confirmed(phase):
    result = evaluate(phase, evidence(phase))
    count = REQUIRED_COUNTS[phase]
    assert result.status is S.ALLOWED
    assert result.decision is UncertaintyDecision.ALLOW
    assert result.is_allowed is True
    assert result.requires_review is False
    assert result.is_contradiction is False
    assert result.blocking_dimension is None
    assert result.blocking_state is None
    assert result.phase is phase
    assert result.required_dimensions == REQUIRED_DIMENSIONS[phase]
    assert result.reason == (
        f"Allowed (allowed): all {count} dimensions this phase requires "
        "are confirmed."
    )
    assert result.reasons == (
        result.reason,
        "T27 allows only because every required dimension is confirmed; it "
        "proves nothing beyond that.",
    )


def test_empty_evidence_refuses_every_phase():
    for phase in MutationPhase:
        result = evaluate(phase, ())
        assert result.status is S.MISSING_EVIDENCE
        assert result.blocking_dimension is REQUIRED_DIMENSIONS[phase][0]
        assert result.blocking_state is MISSING
        assert result.requires_review is True
        assert result.is_allowed is False


@pytest.mark.parametrize("phase", list(MutationPhase))
@pytest.mark.parametrize("state", REFUSING_STATES)
def test_no_single_non_confirmed_required_dimension_ever_allows(phase, state):
    """The fail-closed core: one bad dimension anywhere in the set refuses."""
    for dimension in REQUIRED_DIMENSIONS[phase]:
        result = verdict(phase, **{dimension.value: state})
        assert result.status is not S.ALLOWED
        assert result.decision is UncertaintyDecision.REFUSE
        assert result.is_allowed is False
        assert result.blocking_dimension is not None
        assert result.blocking_state is state
        assert result.reason.startswith("Refused (")
        assert len(result.reasons) == 2


@pytest.mark.parametrize("phase", list(MutationPhase))
@pytest.mark.parametrize("state", REFUSING_STATES)
def test_a_claim_dimension_reports_its_own_state(phase, state):
    for dimension in REQUIRED_DIMENSIONS[phase]:
        if dimension not in CLAIM_DIMENSIONS:
            continue
        result = verdict(phase, **{dimension.value: state})
        assert result.status is CLAIM_STATE_STATUS[state]
        assert result.blocking_dimension is dimension
        assert result.blocking_state is state


@pytest.mark.parametrize("phase", list(MutationPhase))
def test_an_explicitly_contradictory_dimension_is_a_contradiction(phase):
    for dimension in REQUIRED_DIMENSIONS[phase]:
        result = verdict(phase, **{dimension.value: CONTRADICTORY})
        assert result.status is S.CONTRADICTORY_EVIDENCE
        assert result.is_contradiction is True
        assert result.blocking_dimension is dimension
        assert result.blocking_state is CONTRADICTORY
        assert result.requires_review is False


@pytest.mark.parametrize("phase", list(MutationPhase))
@pytest.mark.parametrize("state", [NEGATIVE, UNKNOWN, UNVERIFIED, AMBIGUOUS])
def test_a_confirmed_claim_with_an_unconfirmed_proof_is_a_contradiction(
    phase, state
):
    for dimension in REQUIRED_DIMENSIONS[phase]:
        if dimension not in PROOF_DIMENSIONS:
            continue
        result = verdict(phase, **{dimension.value: state})
        assert result.status is S.CONTRADICTORY_EVIDENCE
        assert result.is_contradiction is True
        assert result.blocking_dimension is dimension
        assert result.blocking_state is state
        assert result.requires_review is False
        assert "is confirmed while its required proof is not" in result.reason


@pytest.mark.parametrize("phase", list(MutationPhase))
def test_an_invalid_state_refuses_the_whole_record(phase):
    for dimension in REQUIRED_DIMENSIONS[phase]:
        result = verdict(phase, **{dimension.value: INVALID})
        assert result.status is S.INVALID_INPUT
        assert result.decision is UncertaintyDecision.REFUSE
        assert result.blocking_dimension is dimension
        assert result.blocking_state is INVALID
        assert result.requires_review is False


# ---------------------------------------------------------------------------
# The mandated matrix (the T19/T20/T21/T24/T25/T26 cases)
# ---------------------------------------------------------------------------


def test_an_unknown_issue_outcome_refuses():
    result = verdict(PRE, **{D.ISSUE_OUTCOME.value: UNKNOWN})
    assert result.status is S.UNKNOWN_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME
    assert result.requires_review is True
    assert result.is_allowed is False


def test_a_failed_codex_run_refuses():
    result = verdict(PRE, **{D.CODEX_EXECUTION.value: NEGATIVE})
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION
    assert result.requires_review is False


def test_an_invalid_change_scope_refuses():
    result = verdict(PRE, **{D.CHANGE_SCOPE.value: NEGATIVE})
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.CHANGE_SCOPE


def test_a_missing_worktree_and_scope_record_refuses():
    result = verdict(PRE, **{D.CHANGE_SCOPE.value: MISSING})
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.CHANGE_SCOPE
    assert result.requires_review is True


def test_failed_tests_refuse():
    result = verdict(PRE, **{D.TESTS.value: NEGATIVE})
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.TESTS


def test_tests_that_did_not_produce_a_result_refuse():
    result = verdict(PRE, **{D.TESTS.value: UNKNOWN})
    assert result.status is S.UNKNOWN_EVIDENCE
    assert result.blocking_dimension is D.TESTS
    assert result.requires_review is True


def test_an_incomplete_sonar_analysis_refuses():
    result = verdict(PRE, **{D.SONAR_ANALYSIS.value: UNVERIFIED})
    assert result.status is S.UNVERIFIED_EVIDENCE
    assert result.blocking_dimension is D.SONAR_ANALYSIS
    assert result.requires_review is True


def test_an_unverified_issue_verification_refuses():
    """T19 says fixed while T18 could not verify: refused, a contradiction."""
    result = verdict(PRE, **{D.ISSUE_VERIFICATION.value: UNVERIFIED})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_VERIFICATION
    assert result.blocking_state is UNVERIFIED
    assert result.is_allowed is False


def test_an_unverified_issue_verification_without_a_fixed_outcome_refuses():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: UNVERIFIED,
            D.ISSUE_VERIFICATION.value: UNVERIFIED,
        },
    )
    assert result.status is S.UNVERIFIED_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME
    assert result.requires_review is True


def test_an_issue_still_open_refuses():
    result = verdict(PRE, **{D.ISSUE_VERIFICATION.value: NEGATIVE})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_VERIFICATION
    assert result.blocking_state is NEGATIVE


def test_a_missing_analysis_correlation_refuses():
    result = verdict(PRE, **{D.ANALYSIS_CORRELATION.value: MISSING})
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.ANALYSIS_CORRELATION


def test_a_refused_branch_protection_refuses():
    result = verdict(PRE, **{D.BRANCH_PROTECTION.value: NEGATIVE})
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.BRANCH_PROTECTION


def test_an_unknown_branch_protection_refuses():
    result = verdict(PRE, **{D.BRANCH_PROTECTION.value: UNKNOWN})
    assert result.status is S.UNKNOWN_EVIDENCE
    assert result.blocking_dimension is D.BRANCH_PROTECTION
    assert result.requires_review is True


def test_a_cross_stage_contradiction_refuses():
    result = verdict(PRE, **{D.CROSS_STAGE_CONSISTENCY.value: CONTRADICTORY})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.CROSS_STAGE_CONSISTENCY


def test_an_unverified_commit_refuses():
    result = verdict(POST_COMMIT, **{D.COMMIT_IDENTITY.value: UNVERIFIED})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.COMMIT_IDENTITY
    assert result.blocking_state is UNVERIFIED


def test_a_mismatched_commit_identity_refuses():
    result = verdict(POST_COMMIT, **{D.COMMIT_IDENTITY.value: NEGATIVE})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.COMMIT_IDENTITY
    assert result.is_allowed is False


def test_a_missing_commit_record_refuses():
    result = verdict(POST_COMMIT, **{D.COMMIT_RESULT.value: MISSING})
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.COMMIT_RESULT
    assert result.requires_review is True


def test_an_unknown_repository_state_after_the_commit_refuses():
    result = verdict(POST_COMMIT, **{D.COMMIT_REPOSITORY.value: UNKNOWN})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.COMMIT_REPOSITORY


def test_a_missing_branch_consistency_record_refuses():
    result = verdict(PRE_PUSH, **{D.BRANCH_CONSISTENCY.value: MISSING})
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.BRANCH_CONSISTENCY


def test_an_unknown_push_result_refuses():
    result = verdict(POST_PUSH, **{D.PUSH_RESULT.value: UNKNOWN})
    assert result.status is S.UNKNOWN_EVIDENCE
    assert result.blocking_dimension is D.PUSH_RESULT
    assert result.requires_review is True


def test_an_unverified_remote_tip_refuses():
    result = verdict(POST_PUSH, **{D.REMOTE_TIP.value: UNVERIFIED})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.REMOTE_TIP
    assert result.blocking_state is UNVERIFIED


def test_an_unknown_expected_commit_refuses():
    result = verdict(POST_PUSH, **{D.EXPECTED_COMMIT.value: UNKNOWN})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.EXPECTED_COMMIT


# ---------------------------------------------------------------------------
# Negative is not uncertain
# ---------------------------------------------------------------------------


def test_a_known_failure_and_a_doubt_stay_distinguishable():
    failure = verdict(PRE, **{D.TESTS.value: NEGATIVE})
    doubt = verdict(PRE, **{D.TESTS.value: UNKNOWN})
    assert failure.status is S.NEGATIVE_EVIDENCE
    assert doubt.status is S.UNKNOWN_EVIDENCE
    assert failure.status is not doubt.status
    assert failure.requires_review is False
    assert doubt.requires_review is True
    assert failure.is_allowed is False
    assert doubt.is_allowed is False
    assert failure.blocking_dimension is D.TESTS
    assert doubt.blocking_dimension is D.TESTS


def test_a_known_failure_is_never_reported_as_doubt():
    for phase in MutationPhase:
        for dimension in REQUIRED_DIMENSIONS[phase]:
            result = verdict(phase, **{dimension.value: NEGATIVE})
            assert result.status is not S.MISSING_EVIDENCE
            assert result.status is not S.UNKNOWN_EVIDENCE
            assert result.status is not S.UNVERIFIED_EVIDENCE
            assert result.status is not S.AMBIGUOUS_EVIDENCE
            assert result.status is not S.ALLOWED
            assert result.requires_review is False


def test_requires_review_matches_the_review_statuses():
    for phase in MutationPhase:
        for dimension in REQUIRED_DIMENSIONS[phase]:
            for state in REFUSING_STATES:
                result = verdict(phase, **{dimension.value: state})
                assert result.requires_review is (
                    result.status in REVIEW_STATUSES
                )
        allowed = evaluate(phase, evidence(phase))
        assert allowed.requires_review is False


def test_every_status_is_reachable():
    reachable = set()
    for phase in MutationPhase:
        reachable.add(evaluate(phase, evidence(phase)).status)
        for dimension in REQUIRED_DIMENSIONS[phase]:
            for state in REFUSING_STATES:
                reachable.add(verdict(phase, **{dimension.value: state}).status)
    reachable.add(evaluate("not-a-phase", ()).status)
    reachable.add(evaluate(PRE, "not-a-tuple").status)
    assert reachable == set(UncertaintyStatus)


# ---------------------------------------------------------------------------
# Precedence (one conflict per adjacent pair)
# ---------------------------------------------------------------------------


def test_invalid_policy_beats_invalid_input():
    result = evaluate("not-a-phase", ("not-an-item",))
    assert result.status is S.INVALID_POLICY


def test_invalid_input_beats_missing_evidence():
    result = evaluate(PRE, ["not-a-tuple"])
    assert result.status is S.INVALID_INPUT


def test_missing_evidence_beats_contradiction():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: MISSING,
            D.CODEX_EXECUTION.value: CONTRADICTORY,
        },
    )
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME


def test_missing_evidence_beats_negative_evidence():
    result = verdict(
        PRE,
        **{D.ISSUE_OUTCOME.value: MISSING, D.CODEX_EXECUTION.value: NEGATIVE},
    )
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME


def test_contradiction_beats_negative_evidence():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: CONTRADICTORY,
            D.CODEX_EXECUTION.value: NEGATIVE,
        },
    )
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME


def test_contradiction_beats_unknown_evidence():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: UNKNOWN,
            D.CODEX_EXECUTION.value: CONTRADICTORY,
        },
    )
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_negative_evidence_beats_unknown_evidence():
    result = verdict(
        PRE,
        **{D.ISSUE_OUTCOME.value: UNKNOWN, D.CODEX_EXECUTION.value: NEGATIVE},
    )
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_negative_evidence_beats_unverified_evidence():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: UNVERIFIED,
            D.CODEX_EXECUTION.value: NEGATIVE,
        },
    )
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_unknown_evidence_beats_unverified_evidence():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: UNVERIFIED,
            D.CODEX_EXECUTION.value: UNKNOWN,
        },
    )
    assert result.status is S.UNKNOWN_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_unverified_evidence_beats_ambiguous_evidence():
    result = verdict(
        PRE,
        **{
            D.ISSUE_OUTCOME.value: AMBIGUOUS,
            D.CODEX_EXECUTION.value: UNVERIFIED,
        },
    )
    assert result.status is S.UNVERIFIED_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_ambiguous_evidence_beats_allowed():
    result = verdict(PRE, **{D.TESTS.value: AMBIGUOUS})
    assert result.status is S.AMBIGUOUS_EVIDENCE
    assert result.is_allowed is False


def test_the_blocking_dimension_is_the_earliest_in_policy_order():
    """The reported dimension follows the policy order, not the tuple order."""
    items = evidence(
        PRE,
        **{D.ISSUE_OUTCOME.value: NEGATIVE, D.TESTS.value: NEGATIVE},
    )
    result = evaluate(PRE, tuple(reversed(items)))
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


def test_a_broken_proof_pair_beats_the_proofs_own_doubt_state():
    """The documented hard case: a contradiction outranks the proof's doubt."""
    result = verdict(POST_COMMIT, **{D.COMMIT_IDENTITY.value: UNKNOWN})
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.status is not S.UNKNOWN_EVIDENCE
    assert result.blocking_state is UNKNOWN


def test_missing_evidence_beats_a_broken_proof_pair():
    result = verdict(POST_COMMIT, **{D.COMMIT_IDENTITY.value: MISSING})
    assert result.status is S.MISSING_EVIDENCE
    assert result.blocking_dimension is D.COMMIT_IDENTITY


def test_a_broken_proof_pair_is_skipped_when_the_claim_is_not_confirmed():
    result = verdict(PRE, **{D.ISSUE_OUTCOME.value: NEGATIVE})
    assert result.status is S.NEGATIVE_EVIDENCE
    assert result.blocking_dimension is D.ISSUE_OUTCOME


def test_an_explicit_contradiction_beats_a_broken_proof_pair():
    result = verdict(
        POST_COMMIT,
        **{
            D.CODEX_EXECUTION.value: CONTRADICTORY,
            D.COMMIT_IDENTITY.value: UNKNOWN,
        },
    )
    assert result.status is S.CONTRADICTORY_EVIDENCE
    assert result.blocking_dimension is D.CODEX_EXECUTION


def test_a_dimension_this_phase_does_not_require_is_ignored():
    result = evaluate(
        PRE,
        evidence(PRE) + (EvidenceItem(D.COMMIT_RESULT, NEGATIVE),),
    )
    assert result.status is S.ALLOWED


def test_a_contradiction_needs_both_dimensions_required_by_the_phase():
    result = evaluate(
        PRE,
        evidence(PRE)
        + (
            EvidenceItem(D.COMMIT_RESULT, CONFIRMED),
            EvidenceItem(D.COMMIT_IDENTITY, UNVERIFIED),
        ),
    )
    assert result.status is S.ALLOWED


# ---------------------------------------------------------------------------
# Fail closed: an unusable phase or evidence record authorises nothing
# ---------------------------------------------------------------------------


def test_the_default_input_is_refused():
    result = evaluate_uncertainty(uncertainty_input=UncertaintyInput())
    assert result.status is S.INVALID_POLICY
    assert result.decision is UncertaintyDecision.REFUSE
    assert result.phase is None
    assert result.required_dimensions == ()
    assert result.blocking_dimension is None
    assert result.blocking_state is None
    assert result.evidence == ()
    assert result.reason == (
        "Refused (invalid-policy): the phase does not select a mutation phase "
        "T27 knows."
    )


@pytest.mark.parametrize(
    "phase",
    ["pre-commit", "PRE_COMMIT", "", 1, 0, True, False, None, object(), D.TESTS],
)
def test_an_unusable_phase_is_refused(phase):
    result = evaluate(phase, evidence(PRE))
    assert result.status is S.INVALID_POLICY
    assert result.is_allowed is False
    assert result.phase is None
    assert result.required_dimensions == ()
    assert result.evidence == ()


@pytest.mark.parametrize(
    "items",
    [
        None,
        [],
        ["not-a-tuple"],
        {},
        {"tests": CONFIRMED},
        set(),
        "tests",
        1,
        1.5,
        b"tests",
        object(),
    ],
)
def test_evidence_must_be_a_tuple(items):
    result = evaluate(PRE, items)
    assert result.status is S.INVALID_INPUT
    assert result.is_allowed is False
    assert result.phase is PRE
    assert result.required_dimensions == REQUIRED_DIMENSIONS[PRE]
    assert result.evidence == ()
    assert result.blocking_dimension is None
    assert result.blocking_state is None
    assert result.reason.endswith("EvidenceItem records.")


@pytest.mark.parametrize(
    "entry",
    [None, "tests", 1, True, object(), (D.TESTS, CONFIRMED), ["tests"]],
)
def test_every_evidence_entry_must_be_an_item(entry):
    result = evaluate(PRE, (entry,))
    assert result.status is S.INVALID_INPUT
    assert result.evidence == ()
    assert result.reason.endswith("not an EvidenceItem record.")


@pytest.mark.parametrize("dimension", ["tests", "", 1, True, None, object()])
def test_an_unknown_dimension_is_refused(dimension):
    result = evaluate(PRE, (EvidenceItem(dimension, CONFIRMED),))
    assert result.status is S.INVALID_INPUT
    assert result.evidence == ()
    assert result.reason.endswith("dimension T27 does not know.")


@pytest.mark.parametrize(
    "state",
    [
        "confirmed",
        "",
        None,
        0,
        1,
        True,
        False,
        [],
        {},
        object(),
        _AlwaysTruthy(),
    ],
)
def test_an_unknown_state_is_refused(state):
    result = evaluate(PRE, (EvidenceItem(D.TESTS, state),))
    assert result.status is S.INVALID_INPUT
    assert result.evidence == ()
    assert result.reason.endswith("state T27 does not know.")


def test_a_truthy_non_state_is_never_believed():
    result = evaluate(PRE, (EvidenceItem(D.TESTS, _AlwaysTruthy()),))
    assert result.status is S.INVALID_INPUT
    assert result.is_allowed is False


def test_a_duplicate_dimension_is_refused():
    result = evaluate(
        PRE,
        (
            EvidenceItem(D.TESTS, CONFIRMED),
            EvidenceItem(D.TESTS, NEGATIVE),
        ),
    )
    assert result.status is S.INVALID_INPUT
    assert result.evidence == ()
    assert result.reason.endswith("would be ambiguous.")


def test_an_invalid_state_outside_the_required_set_still_refuses():
    result = evaluate(PRE, (EvidenceItem(D.COMMIT_RESULT, INVALID),))
    assert result.status is S.INVALID_INPUT
    assert result.blocking_dimension is D.COMMIT_RESULT
    assert result.blocking_state is INVALID


# ---------------------------------------------------------------------------
# The diagnostic code (bounded, echoed, never consulted)
# ---------------------------------------------------------------------------

VALID_CODES = (
    "",
    "still-open",
    "TESTS_FAILED",
    "sha-mismatch",
    "a" * 48,
    "a.b:c_d-e",
    "0" * 48,
)

BAD_CODES = (
    1,
    None,
    True,
    " ",
    "a" * 49,
    "two words",
    "line\nbreak",
    "carriage\rreturn",
    "quote'",
    'double"quote',
    "semi;colon",
    "slash/colon",
    "back\\slash",
    "tab\tcode",
)


@pytest.mark.parametrize("code", BAD_CODES)
def test_an_ill_formed_code_is_refused(code):
    result = evaluate(PRE, (EvidenceItem(D.TESTS, CONFIRMED, code),))
    assert result.status is S.INVALID_INPUT
    assert result.evidence == ()
    assert result.reason.endswith("not a bounded machine token.")


@pytest.mark.parametrize("code", VALID_CODES)
def test_a_bounded_code_is_accepted_and_echoed(code):
    items = tuple(
        EvidenceItem(
            dimension,
            CONFIRMED,
            code if dimension is D.TESTS else "",
        )
        for dimension in REQUIRED_DIMENSIONS[PRE]
    )
    result = evaluate(PRE, items)
    assert result.status is S.ALLOWED
    published = result.as_dict()["evidence"]
    tests_item = next(e for e in published if e["dimension"] == "tests")
    assert tests_item["code"] == code
    assert tests_item["is_confirmed"] is True


def test_a_code_never_changes_a_decision():
    base = verdict(PRE, **{D.TESTS.value: NEGATIVE})
    coded = evaluate(
        PRE,
        tuple(
            EvidenceItem(
                item.dimension,
                item.state,
                "sha-mismatch" if item.dimension is D.TESTS else "",
            )
            for item in evidence(PRE, **{D.TESTS.value: NEGATIVE})
        ),
    )
    assert coded.status is base.status
    assert coded.decision is base.decision
    assert coded.blocking_dimension is base.blocking_dimension
    assert coded.blocking_state is base.blocking_state
    assert coded.reason == base.reason
    assert any(
        item["code"] == "sha-mismatch"
        for item in coded.as_dict()["evidence"]
    )


# ---------------------------------------------------------------------------
# Secret safety: no caller-authored text, no fabricated output
# ---------------------------------------------------------------------------


def test_ill_formed_caller_values_are_never_echoed():
    records = (
        UncertaintyInput(phase=DECOY),
        UncertaintyInput(PRE, (EvidenceItem(D.TESTS, DECOY),)),
        UncertaintyInput(PRE, (EvidenceItem(DECOY, CONFIRMED),)),
        UncertaintyInput(PRE, (EvidenceItem(D.TESTS, CONFIRMED, DECOY + "!"),)),
        UncertaintyInput(PRE, (DECOY,)),
        UncertaintyInput(PRE, [EvidenceItem(D.TESTS, CONFIRMED)]),
        UncertaintyInput(DECOY, (DECOY,)),
        UncertaintyInput(D.TESTS, ()),
    )
    for record in records:
        result = evaluate_uncertainty(uncertainty_input=record)
        assert result.is_allowed is False
        assert DECOY not in result.reason
        assert DECOY not in " ".join(result.reasons)
        for payload in (record.as_dict(), result.as_dict()):
            assert DECOY not in json.dumps(payload, sort_keys=True)


def test_a_valid_code_is_the_only_caller_text_that_is_echoed():
    record = UncertaintyInput(
        PRE,
        (EvidenceItem(D.TESTS, CONFIRMED, DECOY),),
    )
    result = evaluate_uncertainty(uncertainty_input=record)
    published = json.dumps(result.as_dict(), sort_keys=True)
    assert DECOY in published
    assert result.status is S.MISSING_EVIDENCE


def test_a_non_input_is_a_caller_error():
    with pytest.raises(TypeError) as excinfo:
        evaluate_uncertainty(uncertainty_input="pre-commit")
    assert "must be an UncertaintyInput" in str(excinfo.value)


def test_the_evaluation_takes_one_keyword_only_parameter():
    parameters = list(inspect.signature(evaluate_uncertainty).parameters.values())
    assert [parameter.name for parameter in parameters] == ["uncertainty_input"]
    assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        evaluate_uncertainty(UncertaintyInput(phase=PRE))


# ---------------------------------------------------------------------------
# Determinism, purity and caller-value safety
# ---------------------------------------------------------------------------


def test_evaluating_the_same_input_twice_returns_an_equal_verdict():
    items = evidence(PRE, **{D.TESTS.value: UNVERIFIED})
    first = evaluate(PRE, items)
    second = evaluate(PRE, items)
    assert first == second
    assert first.as_dict() == second.as_dict()
    assert json.dumps(first.as_dict(), sort_keys=True) == json.dumps(
        second.as_dict(), sort_keys=True
    )


def test_the_verdict_is_stable_over_many_evaluations():
    items = evidence(POST_PUSH, **{D.REMOTE_TIP.value: UNVERIFIED})
    statuses = {evaluate(POST_PUSH, items).status for _ in range(50)}
    assert statuses == {S.CONTRADICTORY_EVIDENCE}


def test_the_caller_values_are_never_mutated():
    items = list(evidence(PRE))
    snapshot = list(items)
    record = UncertaintyInput(phase=PRE, evidence=items)
    result = evaluate_uncertainty(uncertainty_input=record)
    assert result.status is S.INVALID_INPUT
    assert items == snapshot
    assert record.evidence is items

    frozen = evidence(PRE)
    record = UncertaintyInput(phase=PRE, evidence=frozen)
    assert evaluate_uncertainty(uncertainty_input=record).status is S.ALLOWED
    assert record.phase is PRE
    assert record.evidence is frozen


def test_the_verdict_does_not_depend_on_the_tuple_order():
    items = evidence(
        PRE,
        **{D.TESTS.value: UNKNOWN, D.SONAR_ANALYSIS.value: AMBIGUOUS},
    )
    forward = evaluate(PRE, items)
    backward = evaluate(PRE, tuple(reversed(items)))
    assert forward == backward
    assert forward.as_dict() == backward.as_dict()
    assert [
        item["dimension"] for item in forward.as_dict()["evidence"]
    ] == [dimension.value for dimension in REQUIRED_DIMENSIONS[PRE]]


def test_evaluation_reads_no_mutable_module_state():
    def snapshot() -> tuple:
        return (
            dict(REQUIRED_DIMENSIONS),
            ALLOWED_STATUSES,
            REVIEW_STATUSES,
            PRECEDENCE,
            CONTRADICTION_PAIRS,
            module._DIMENSION_ORDER,
        )

    before = snapshot()
    verdict(PRE, **{D.TESTS.value: NEGATIVE})
    evaluate(POST_PUSH, ())
    evaluate("not-a-phase", [])
    assert snapshot() == before


def test_the_records_are_frozen():
    item = EvidenceItem(D.TESTS, CONFIRMED)
    with pytest.raises(FrozenInstanceError):
        item.state = NEGATIVE
    record = UncertaintyInput(phase=PRE)
    with pytest.raises(FrozenInstanceError):
        record.phase = None
    evaluation = evaluate(PRE, ())
    with pytest.raises(FrozenInstanceError):
        evaluation.status = S.ALLOWED


def test_the_required_evidence_table_is_read_only():
    with pytest.raises(TypeError):
        REQUIRED_DIMENSIONS[PRE] = ()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

#: The exact keys of :meth:`UncertaintyEvaluation.as_dict`.
EVALUATION_KEYS = {
    "policy_version",
    "phase",
    "status",
    "decision",
    "is_allowed",
    "requires_review",
    "is_contradiction",
    "blocking_dimension",
    "blocking_state",
    "required_dimensions",
    "evidence",
    "reason",
    "reasons",
}


def test_the_evaluation_serialization_is_exact():
    result = verdict(
        PRE,
        **{D.CODEX_EXECUTION.value: NEGATIVE, D.TESTS.value: NEGATIVE},
    )
    payload = result.as_dict()
    assert set(payload) == EVALUATION_KEYS
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["phase"] == "pre-commit"
    assert payload["status"] == "negative-evidence"
    assert payload["decision"] == "refuse"
    assert payload["is_allowed"] is False
    assert payload["requires_review"] is False
    assert payload["is_contradiction"] is False
    assert payload["blocking_dimension"] == "codex-execution"
    assert payload["blocking_state"] == "negative"
    assert payload["required_dimensions"] == [
        dimension.value for dimension in REQUIRED_DIMENSIONS[PRE]
    ]
    assert len(payload["evidence"]) == 9
    assert payload["reasons"] == list(result.reasons)
    assert payload["reason"] == result.reason
    assert json.loads(json.dumps(payload)) == payload


def test_the_allowed_serialization_has_no_blocking_dimension():
    payload = verdict(POST_COMMIT).as_dict()
    assert payload["status"] == "allowed"
    assert payload["decision"] == "allow"
    assert payload["is_allowed"] is True
    assert payload["requires_review"] is False
    assert payload["phase"] == "post-commit"
    assert payload["blocking_dimension"] is None
    assert payload["blocking_state"] is None


def test_an_unstructured_evidence_field_serializes_as_empty():
    payload = evaluate(PRE, "not-a-tuple").as_dict()
    assert payload["status"] == "invalid-input"
    assert payload["blocking_dimension"] is None
    assert payload["blocking_state"] is None
    assert payload["evidence"] == []


def test_a_missing_dimension_serializes_no_evidence():
    payload = evaluate(PRE, ()).as_dict()
    assert payload["status"] == "missing-evidence"
    assert payload["blocking_dimension"] == "issue-outcome"
    assert payload["blocking_state"] == "missing"
    assert payload["evidence"] == []
    assert payload["required_dimensions"] == [
        dimension.value for dimension in REQUIRED_DIMENSIONS[PRE]
    ]


def test_the_input_serialization_is_exact():
    payload = UncertaintyInput(PRE, evidence(PRE)).as_dict()
    assert set(payload) == {"phase", "evidence", "evidence_is_usable"}
    assert payload["phase"] == "pre-commit"
    assert payload["evidence_is_usable"] is True
    assert len(payload["evidence"]) == 9
    assert payload["evidence"][0] == {
        "dimension": "issue-outcome",
        "state": "confirmed",
        "code": "",
        "is_confirmed": True,
    }


def test_the_input_serialization_flags_an_unusable_record():
    mixed = UncertaintyInput(PRE, evidence(PRE) + ("junk",)).as_dict()
    assert mixed["evidence_is_usable"] is False
    assert len(mixed["evidence"]) == 9
    listy = UncertaintyInput(
        PRE, [EvidenceItem(D.TESTS, CONFIRMED)]
    ).as_dict()
    assert listy["evidence_is_usable"] is False
    assert listy["evidence"] == []
    stringy = UncertaintyInput("pre-commit", ["junk"]).as_dict()
    assert stringy["phase"] is None
    assert stringy["evidence"] == []
    assert stringy["evidence_is_usable"] is False


def test_an_item_serializes_only_usable_values():
    assert EvidenceItem(D.TESTS, CONFIRMED).as_dict() == {
        "dimension": "tests",
        "state": "confirmed",
        "code": "",
        "is_confirmed": True,
    }
    assert EvidenceItem(D.TESTS, UNKNOWN, "still-open").as_dict() == {
        "dimension": "tests",
        "state": "unknown",
        "code": "still-open",
        "is_confirmed": False,
    }
    assert EvidenceItem("tests", CONFIRMED).as_dict() == {
        "dimension": None,
        "state": "confirmed",
        "code": "",
        "is_confirmed": True,
    }
    assert EvidenceItem(D.TESTS, "confirmed").as_dict() == {
        "dimension": "tests",
        "state": None,
        "code": "",
        "is_confirmed": False,
    }
    assert EvidenceItem(D.TESTS, CONFIRMED, "two words").as_dict() == {
        "dimension": "tests",
        "state": "confirmed",
        "code": None,
        "is_confirmed": True,
    }


# ---------------------------------------------------------------------------
# The module surface (standard library only, no I/O, no command, not wired)
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "uncertainty_policy.py"

#: The only modules T27 may import.
STDLIB_IMPORTS = {"__future__", "dataclasses", "enum", "types", "typing"}

#: The top-level definitions of the module, in declaration order.
TOP_LEVEL_DEFINITIONS = (
    "EvidenceState",
    "EvidenceDimension",
    "MutationPhase",
    "UncertaintyStatus",
    "UncertaintyDecision",
    "_is_code",
    "_input_problem",
    "_canonical",
    "_first_with_state",
    "_broken_pair",
    "EvidenceItem",
    "UncertaintyInput",
    "UncertaintyEvaluation",
    "_outcome",
    "_refuse",
    "evaluate_uncertainty",
)

#: Calls T27 must never make: I/O, execution, network and process control.
FORBIDDEN_CALLS = {
    "open",
    "eval",
    "exec",
    "compile",
    "input",
    "print",
    "__import__",
    "system",
    "popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "getenv",
    "connect",
    "urlopen",
}


def imported_modules(source: str) -> set:
    """The top-level modules ``source`` imports (both import forms)."""
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported


def called_names(source: str) -> set:
    """The bare names of every function or method ``source`` calls."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_the_module_imports_only_the_standard_library():
    imported = imported_modules(MODULE_PATH.read_text(encoding="utf-8"))
    assert imported == STDLIB_IMPORTS


def test_the_module_imports_no_repository_module():
    imported = imported_modules(MODULE_PATH.read_text(encoding="utf-8"))
    repository_modules = {path.stem for path in REPO_ROOT.glob("*.py")}
    assert imported & repository_modules == set()


def test_no_repository_module_imports_t27():
    """T27 is a library with no caller: it stays unwired until it is asked for."""
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name == MODULE_PATH.name:
            continue
        imported = imported_modules(path.read_text(encoding="utf-8"))
        assert "uncertainty_policy" not in imported, path.name


def test_the_module_calls_nothing_dangerous():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert not (called_names(source) & FORBIDDEN_CALLS)


def test_the_module_defines_exactly_the_documented_surface():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.AsyncFunctionDef) for node in tree.body
    )
    assert tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ) == TOP_LEVEL_DEFINITIONS


def test_every_exported_name_exists_and_is_sorted():
    assert module.__all__ == (
        "ALLOWED_STATUSES",
        "CONTRADICTION_PAIRS",
        "EvidenceDimension",
        "EvidenceItem",
        "EvidenceState",
        "MutationPhase",
        "POLICY_VERSION",
        "PRECEDENCE",
        "REQUIRED_DIMENSIONS",
        "REVIEW_STATUSES",
        "UncertaintyDecision",
        "UncertaintyEvaluation",
        "UncertaintyInput",
        "UncertaintyStatus",
        "evaluate_uncertainty",
    )
    assert list(module.__all__) == sorted(module.__all__)
    for name in module.__all__:
        assert hasattr(module, name), name


def test_the_module_holds_no_mutable_module_state():
    for name in (
        "_DIMENSION_ORDER",
        "_PRE_COMMIT_DIMENSIONS",
        "_COMMIT_DIMENSIONS",
        "_PUSH_DIMENSIONS",
        "_PRE_PUSH_DIMENSIONS",
        "ALLOWED_STATUSES",
        "REVIEW_STATUSES",
        "PRECEDENCE",
        "CONTRADICTION_PAIRS",
    ):
        assert isinstance(getattr(module, name), tuple), name
    for name in (
        "REQUIRED_DIMENSIONS",
        "_STATUS_REASONS",
        "_STATUS_CONSEQUENCES",
    ):
        assert isinstance(getattr(module, name), types.MappingProxyType), name


def test_the_enum_classes_are_not_collected_by_pytest():
    for enum_class in (
        EvidenceState,
        EvidenceDimension,
        MutationPhase,
        UncertaintyStatus,
        UncertaintyDecision,
    ):
        assert enum_class.__test__ is False
