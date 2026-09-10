"""Determine the final status of the current fix attempt (T19).

:func:`determine_issue_status` combines the structured results of the whole
attempt - T11 Codex analysis, T13 change scope, T15 test outcome, T17 analysis
completion and T18 SonarQube verification - into one typed
:class:`IssueStatusResult`.

The business rule that matters
------------------------------
``FIXED`` is **not** granted because Codex exited 0, files changed, the scope
was valid, the tests passed, or the analysis completed. The original issue must
be **absent from the post-analysis SonarQube result** *and* that absence must be
based on a reliable identity match *and* every earlier precondition must have
passed. A successful analysis completion alone proves nothing about the issue.

Decision precedence (deterministic, documented)
-----------------------------------------------
1. ``CODEX_FAILED`` - Codex did not execute successfully.
2. ``SCOPE_INVALID`` - the change touched files outside the issue's scope.
3. ``TESTS_FAILED`` - the project tests did not pass.
4. ``ANALYSIS_FAILED`` - the analysis was not triggered, its compute-engine
   task could not be identified, the completion disagreed with the trigger, or
   the analysis failed / was canceled / did not finish inside the wait budget.
5. ``REVIEW_REQUIRED`` - the analysis state is unknown (no task id, unusable
   API, malformed response), the issues could not be retrieved, the snapshot
   could not be attributed to the verified analysis (correlation), the identity
   match/absence is ambiguous, or nothing changed in the issue's file.
6. ``STILL_OPEN`` - the original issue is still reported as open.
7. ``FIXED`` - every precondition passed and the original issue is reliably
   absent.

Earlier stages always win: a broken test run is reported as ``TESTS_FAILED``
even if the issue also disappeared, so a failing run is never presented as a
fix.

Explicit stage translation
--------------------------
The mapping between the analysis/verification sub-results and the final status
is *not* hidden in ad-hoc branches. It is expressed by two small typed
translations that are individually testable:

* :func:`resolve_analysis_stage` turns the T16 trigger result plus the T17
  completion into one :class:`AnalysisStageOutcome`, mapped to a final status by
  :data:`ANALYSIS_STAGE_STATUS`.
* :func:`resolve_verification_outcome` turns the T18 verification result
  (including its correlation verdict) into one :class:`VerificationOutcome`,
  mapped by :data:`VERIFICATION_OUTCOME_STATUS`.

Deterministic summary of the analysis stage (T16 -> T17 -> T19):

============================== ====================== ======================
T16 trigger                    T17 completion         analysis stage
============================== ====================== ======================
not ``TRIGGERED``              (any)                  ``ANALYSIS_FAILED``
``TRIGGERED`` without task id  (any)                  ``ANALYSIS_FAILED``
``TRIGGERED`` with task id     task id disagrees      ``ANALYSIS_FAILED``
``TRIGGERED`` with task id     ``FAILED`` / ``CANCELED`` / ``TIMEOUT``
                                                      ``ANALYSIS_FAILED``
``TRIGGERED`` with task id     ``UNKNOWN``            ``REVIEW_REQUIRED``
``TRIGGERED`` with task id     ``SUCCESS``            continue to T18
not supplied (``None``)        ``SUCCESS``            continue to T18
not supplied (``None``)        ``FAILED`` / ``CANCELED`` / ``TIMEOUT``
                                                      ``ANALYSIS_FAILED``
not supplied (``None``)        ``UNKNOWN``            ``REVIEW_REQUIRED``
============================== ====================== ======================

When no T16 result is supplied, only T17 evidence exists; a ``SUCCESS``
completion is itself proof that a real compute-engine task was polled to a
successful end, so that path stays safe. Supplying the T16 result is strictly
stricter and is what the orchestration layer is expected to do.

Scope boundary
--------------
This module is pure decision logic: no HTTP, no subprocess, no file access, no
retry, no re-invocation of Codex, no new branch, no commit and no push. It only
classifies the attempt it is given (iteration limits and retries are T25).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

from analysis_correlation import AnalysisCorrelation
from change_scope import ChangeScopeResult
from codex_result import CodexResultAnalysis
from sonar_analysis import SonarAnalysisStatus, SonarAnalysisTriggerResult
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState
from sonar_issue_verification import SonarIssueVerificationResult
from test_result import TestOutcome


class IssueFinalStatus(Enum):
    """Final classification of the current attempt for one issue."""

    __test__ = False

    FIXED = "fixed"
    STILL_OPEN = "still-open"
    ANALYSIS_FAILED = "analysis-failed"
    TESTS_FAILED = "tests-failed"
    SCOPE_INVALID = "scope-invalid"
    CODEX_FAILED = "codex-failed"
    REVIEW_REQUIRED = "review-required"


#: Precedence for the ``FIXED``-candidate stages, in evaluation order.
#: ``REVIEW_REQUIRED`` is applied inside stage 4/5 whenever the evidence needed
#: to continue is missing, so it appears in the ladder between ``ANALYSIS_FAILED``
#: and ``STILL_OPEN``/``FIXED``.
PRECEDENCE: Tuple[IssueFinalStatus, ...] = (
    IssueFinalStatus.CODEX_FAILED,
    IssueFinalStatus.SCOPE_INVALID,
    IssueFinalStatus.TESTS_FAILED,
    IssueFinalStatus.ANALYSIS_FAILED,
    IssueFinalStatus.REVIEW_REQUIRED,
    IssueFinalStatus.STILL_OPEN,
    IssueFinalStatus.FIXED,
)


class AnalysisStageOutcome(Enum):
    """Typed translation of the T16 trigger + T17 completion (see docstring)."""

    __test__ = False

    #: T16 did not report a successful trigger.
    TRIGGER_FAILED = "trigger-failed"
    #: T16 triggered the analysis but no compute-engine task id was available.
    ANALYSIS_UNIDENTIFIED = "analysis-unidentified"
    #: The completion's task id does not match the id T16 provided.
    ANALYSIS_IDENTITY_MISMATCH = "analysis-identity-mismatch"
    #: T17 reached a definitive failure (failed / canceled / timeout).
    ANALYSIS_FAILED = "analysis-failed"
    #: T17 could not determine the analysis state at all.
    ANALYSIS_INCOMPLETE = "analysis-incomplete"
    #: The analysis completed successfully; verification may proceed.
    COMPLETED = "completed"


class VerificationOutcome(Enum):
    """Typed translation of the T18 verification result (see docstring)."""

    __test__ = False

    #: The open issues could not be retrieved at all.
    NOT_RETRIEVED = "not-retrieved"
    #: The snapshot is not provably attributable to the verified analysis.
    NOT_CORRELATED = "not-correlated"
    #: The original issue is reliably still open.
    STILL_OPEN = "still-open"
    #: An equivalent issue is open but cannot be confirmed as the original one.
    PRESENCE_AMBIGUOUS = "presence-ambiguous"
    #: The original issue is absent but the match/absence is not reliable.
    ABSENCE_UNRELIABLE = "absence-unreliable"
    #: The original issue is reliably absent from a correlated snapshot.
    ABSENT_VERIFIED = "absent-verified"


#: Final status for each analysis-stage outcome (``None`` = continue to T18).
ANALYSIS_STAGE_STATUS: Mapping[AnalysisStageOutcome, Optional[IssueFinalStatus]] = {
    AnalysisStageOutcome.TRIGGER_FAILED: IssueFinalStatus.ANALYSIS_FAILED,
    AnalysisStageOutcome.ANALYSIS_UNIDENTIFIED: IssueFinalStatus.ANALYSIS_FAILED,
    AnalysisStageOutcome.ANALYSIS_IDENTITY_MISMATCH: IssueFinalStatus.ANALYSIS_FAILED,
    AnalysisStageOutcome.ANALYSIS_FAILED: IssueFinalStatus.ANALYSIS_FAILED,
    AnalysisStageOutcome.ANALYSIS_INCOMPLETE: IssueFinalStatus.REVIEW_REQUIRED,
    AnalysisStageOutcome.COMPLETED: None,
}

#: Final status for each verification outcome (``None`` = continue to the
#: attribution stage).
VERIFICATION_OUTCOME_STATUS: Mapping[VerificationOutcome, Optional[IssueFinalStatus]] = {
    VerificationOutcome.NOT_RETRIEVED: IssueFinalStatus.REVIEW_REQUIRED,
    VerificationOutcome.NOT_CORRELATED: IssueFinalStatus.REVIEW_REQUIRED,
    VerificationOutcome.STILL_OPEN: IssueFinalStatus.STILL_OPEN,
    VerificationOutcome.PRESENCE_AMBIGUOUS: IssueFinalStatus.REVIEW_REQUIRED,
    VerificationOutcome.ABSENCE_UNRELIABLE: IssueFinalStatus.REVIEW_REQUIRED,
    VerificationOutcome.ABSENT_VERIFIED: None,
}


def resolve_analysis_stage(
    trigger: Optional[SonarAnalysisTriggerResult],
    completion: SonarAnalysisCompletion,
) -> Tuple[AnalysisStageOutcome, str]:
    """Translate the T16 trigger result and the T17 completion into one outcome.

    Pure and deterministic. Never returns ``COMPLETED`` unless the evidence
    proves that *this* run's analysis completed successfully.

    Args:
        trigger: the T16 record, or ``None`` when only T17 evidence is available.
        completion: the T17 record of waiting for the analysis.

    Returns:
        ``(outcome, reason)`` where ``reason`` explains the translation.
    """
    if trigger is not None:
        status = getattr(trigger, "status", None)
        if status is not SonarAnalysisStatus.TRIGGERED:
            detail = trigger.error or "no detail reported"
            return (
                AnalysisStageOutcome.TRIGGER_FAILED,
                "The SonarQube analysis was not triggered successfully "
                f"({getattr(status, 'value', status)}): {detail}",
            )
        trigger_task_id = getattr(trigger, "task_id", None)
        if trigger_task_id is None or not str(trigger_task_id).strip():
            return (
                AnalysisStageOutcome.ANALYSIS_UNIDENTIFIED,
                "The analysis was requested, but no compute-engine task id was "
                "reported, so its completion cannot be established.",
            )
        completion_task_id = getattr(completion, "task_id", None)
        if completion_task_id is not None and str(completion_task_id).strip() != str(
            trigger_task_id
        ).strip():
            return (
                AnalysisStageOutcome.ANALYSIS_IDENTITY_MISMATCH,
                "The waited-on compute-engine task does not match the task the "
                "analysis trigger reported, so the completion cannot be "
                "attributed to this run.",
            )

    if completion.status is SonarAnalysisState.SUCCESS:
        return (
            AnalysisStageOutcome.COMPLETED,
            "The SonarQube analysis completed successfully.",
        )
    if completion.status is SonarAnalysisState.UNKNOWN:
        return (
            AnalysisStageOutcome.ANALYSIS_INCOMPLETE,
            completion.failure_reason
            or completion.reason
            or "The SonarQube analysis state could not be determined.",
        )
    return (
        AnalysisStageOutcome.ANALYSIS_FAILED,
        completion.failure_reason
        or completion.reason
        or "The SonarQube analysis did not complete successfully.",
    )


def resolve_verification_outcome(
    verification: SonarIssueVerificationResult,
) -> Tuple[VerificationOutcome, str]:
    """Translate the T18 verification result into one outcome.

    Pure and deterministic. An absence is only ``ABSENT_VERIFIED`` when the
    retrieval succeeded, no matching issue is open, the identity match is
    reliable, the page was complete and the snapshot is correlated with the
    verified analysis.

    A still-open issue is reported as ``STILL_OPEN`` regardless of the
    correlation verdict: it is a positive finding that can never yield
    ``FIXED``, and losing it to ``REVIEW_REQUIRED`` would hide real information.
    Correlation is required to attribute an *absence*, which is the only claim
    that can produce a fix.
    """
    if not verification.retrieval_succeeded:
        return (
            VerificationOutcome.NOT_RETRIEVED,
            verification.reason
            or "The SonarQube issues could not be retrieved after the analysis.",
        )

    if verification.is_present:
        if verification.identity_reliable:
            return (
                VerificationOutcome.STILL_OPEN,
                "The original issue is still open after the new analysis.",
            )
        return (
            VerificationOutcome.PRESENCE_AMBIGUOUS,
            "An equivalent issue is still open but cannot be confirmed as the "
            "original issue, so a human must review the attempt.",
        )

    if verification.correlation is not AnalysisCorrelation.CORRELATED:
        return (
            VerificationOutcome.NOT_CORRELATED,
            verification.correlation_reason
            or "The issue snapshot cannot be attributed to the analysis that "
            "was verified, so the absence proves nothing about this run.",
        )

    if not verification.identity_reliable:
        return (
            VerificationOutcome.ABSENCE_UNRELIABLE,
            "The original issue is absent, but the identity match is not "
            "reliable enough to confirm the fix.",
        )

    return (
        VerificationOutcome.ABSENT_VERIFIED,
        "The original issue is reliably absent from the correlated snapshot.",
    )


@dataclass(frozen=True)
class IssueStatusResult:
    """Typed final status of one attempt, with all the evidence it used.

    Attributes:
        status: the final classification (:class:`IssueFinalStatus`).
        decisive_stage: which stage produced the status (``codex_execution``,
            ``change_scope``, ``project_tests``, ``sonar_analysis``,
            ``sonar_verification``, ``fix_attribution``).
        reason: short human-readable explanation of the decision.
        reasons: ordered reasoning trail (includes the advisory notes).
        codex: the T11 analysis that was used.
        scope: the T13 result that was used.
        tests: the T15 outcome that was used.
        analysis: the T17 completion that was used.
        verification: the T18 verification that was used.
    """

    __test__ = False

    status: IssueFinalStatus
    decisive_stage: str
    reason: str
    reasons: Tuple[str, ...]
    codex: CodexResultAnalysis
    scope: ChangeScopeResult
    tests: TestOutcome
    analysis: SonarAnalysisCompletion
    verification: SonarIssueVerificationResult

    @property
    def is_fixed(self) -> bool:
        """True only when the original issue was reliably verified as absent."""
        return self.status is IssueFinalStatus.FIXED

    @property
    def needs_review(self) -> bool:
        """True when the outcome is ambiguous and a human must look at it."""
        return self.status is IssueFinalStatus.REVIEW_REQUIRED

    @property
    def blocking_reason(self) -> Optional[str]:
        """Why the attempt is not a verified fix, or ``None`` when it is."""
        return None if self.is_fixed else self.reason

    @property
    def reason_text(self) -> str:
        """All reasons joined into a single readable paragraph."""
        return " ".join(self.reasons)

    def as_dict(self) -> dict:
        """Secret-free summary (nested sub-results are already sanitized)."""
        return {
            "status": self.status.value,
            "decisive_stage": self.decisive_stage,
            "is_fixed": self.is_fixed,
            "needs_review": self.needs_review,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "codex": self.codex.as_dict(),
            "scope": self.scope.as_dict(),
            "tests": self.tests.as_dict(),
            "analysis": self.analysis.as_dict(),
            "verification": self.verification.as_dict(),
        }


def determine_issue_status(
    *,
    codex: CodexResultAnalysis,
    scope: ChangeScopeResult,
    tests: TestOutcome,
    analysis: SonarAnalysisCompletion,
    verification: SonarIssueVerificationResult,
    trigger: Optional[SonarAnalysisTriggerResult] = None,
) -> IssueStatusResult:
    """Classify the current attempt using the documented precedence.

    Pure and deterministic: identical inputs always produce an identical
    result, with no I/O, no retry, and no Codex re-invocation.

    Args:
        codex: T11 analysis of the Codex execution.
        scope: T13 change-scope validation result.
        tests: T15 project-test outcome.
        analysis: T17 SonarQube analysis completion.
        verification: T18 verification of the original issue.
        trigger: optional T16 analysis-trigger result. Supplying it makes the
            analysis stage strictly stricter (a failed trigger, a missing task
            id, or a mismatched task id can never become anything but
            ``ANALYSIS_FAILED``).

    Returns:
        A typed :class:`IssueStatusResult`. ``FIXED`` is returned only when the
        original issue is reliably verified as absent after a successful,
        *correlated* analysis *and* every earlier precondition passed.
    """

    def decide(
        status: IssueFinalStatus, stage: str, reason: str, trail: list
    ) -> IssueStatusResult:
        return IssueStatusResult(
            status=status,
            decisive_stage=stage,
            reason=reason,
            reasons=tuple(trail),
            codex=codex,
            scope=scope,
            tests=tests,
            analysis=analysis,
            verification=verification,
        )

    trail: list = []

    # Stage 1 - Codex execution (T11).
    if not codex.execution_succeeded:
        trail.append(codex.reason)
        return decide(
            IssueFinalStatus.CODEX_FAILED,
            "codex_execution",
            "Codex did not complete successfully, so the attempt cannot be "
            "verified.",
            trail,
        )
    trail.append("Codex executed successfully.")
    if codex.output_suggests_uncertainty:
        trail.append(
            "Advisory: Codex output contained uncertainty markers ("
            + ", ".join(codex.matched_markers)
            + "); the decision below is still based on the verified evidence."
        )

    # Stage 2 - change scope (T13).
    if not scope.is_valid:
        trail.append(scope.reason)
        return decide(
            IssueFinalStatus.SCOPE_INVALID,
            "change_scope",
            "Change scope validation failed with "
            f"{len(scope.unexpected_files)} unexpected file(s), so the attempt "
            "is not accepted.",
            trail,
        )
    trail.append("Change scope validation passed.")

    # Stage 3 - project tests (T15).
    if not tests.passed:
        trail.append(tests.blocking_reason or tests.reason)
        return decide(
            IssueFinalStatus.TESTS_FAILED,
            "project_tests",
            tests.blocking_reason
            or "The project tests did not pass, so the attempt cannot be "
            "verified.",
            trail,
        )
    trail.append("Project tests passed.")


    # Stage 4 - SonarQube analysis (T16 + T17), translated explicitly.
    analysis_outcome, analysis_reason = resolve_analysis_stage(trigger, analysis)
    analysis_status = ANALYSIS_STAGE_STATUS[analysis_outcome]
    if analysis_status is not None:
        trail.append(analysis_reason)
        return decide(
            analysis_status,
            "sonar_analysis",
            analysis_reason,
            trail,
        )
    trail.append(analysis_reason)

    # Stage 5 - issue verification (T18), translated explicitly.
    verification_outcome, verification_reason = resolve_verification_outcome(
        verification
    )
    trail.append(verification_reason)
    verification_status = VERIFICATION_OUTCOME_STATUS[verification_outcome]
    if verification_status is not None:
        return decide(
            verification_status,
            "sonar_verification",
            verification_reason,
            trail,
        )

    # Stage 6 - attribution guard: a disappearing issue proves nothing when the
    # attempt changed nothing in the issue's own file.
    if not scope.expected_file_modified:
        trail.append(
            "No change was made to the issue's file, so the issue's "
            "disappearance cannot be attributed to this attempt."
        )
        return decide(
            IssueFinalStatus.REVIEW_REQUIRED,
            "fix_attribution",
            "Nothing changed in the issue's file, so the fix cannot be "
            "attributed to this attempt.",
            trail,
        )

    trail.append(
        "Every precondition passed: Codex executed successfully, the change "
        "scope was valid, the project tests passed, the SonarQube analysis "
        "completed, the issue's file changed, and the original issue is "
        "reliably absent from the new SonarQube result."
    )
    return decide(
        IssueFinalStatus.FIXED,
        "fix_attribution",
        "The original issue is reliably absent after a successful analysis "
        "and every required precondition passed.",
        trail,
    )


__all__: Sequence[str] = (
    "ANALYSIS_STAGE_STATUS",
    "AnalysisStageOutcome",
    "IssueFinalStatus",
    "IssueStatusResult",
    "PRECEDENCE",
    "VERIFICATION_OUTCOME_STATUS",
    "VerificationOutcome",
    "determine_issue_status",
    "resolve_analysis_stage",
    "resolve_verification_outcome",
)

