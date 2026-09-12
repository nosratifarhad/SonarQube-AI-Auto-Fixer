"""T22 overall report - deterministic, fail-closed aggregation of T19/T20/T21.

Why this module exists
----------------------
T19 decides whether *one* issue is ``FIXED``, T20 commits that fix and T21 pushes
the commit. Each task owns exactly one stage of the lifecycle and the stages must
never be collapsed: ``FIXED`` is not ``COMMITTED`` and ``COMMITTED`` is not
``PUSHED``. This module answers the remaining question - *what happened overall?* -
by aggregating the evidence those stages already produced::

    T19 ``IssueStatusResult`` -> T20 ``CommitResult`` -> T21 ``PushResult``
                              -> T22 ``OverallReport``

T22 does not decide whether an issue *should* have been fixed and it never
executes anything: it reports what the preceding stages proved, and nothing more.

Purity and side effects
-----------------------
T22 is a **pure reporting layer**:

* it runs no Git command, no SonarQube call, no Codex run, no project test, no
  subprocess, no network I/O and no filesystem access;
* it only *reads* the supplied results, through the same ``as_dict()`` projection
  T21 uses - never ``asdict()``, never private attributes;
* it never mutates its inputs and never mutates a repository;
* nothing here is wired into ``main.py``: T22 is a library, not a pipeline step.

Fail-closed rules
-----------------
Nothing is ever inferred. A missing result is *not* a success, an unknown status
is *not* a failure, and an unverifiable stage is *not* a pass:

* an unknown/unrecognised T19/T20/T21 status aborts the report (``G5``/``G7``/
  ``G9``) instead of being counted as a failure;
* a malformed, mispaired or contradictory piece of evidence aborts the report
  (``G4``, ``G6``, ``G8``, ``G10``-``G14``);
* a count, serialization or secret invariant violation aborts the report
  (``G15``-``G20``), so an internally inconsistent or secret-bearing report can
  never be returned as valid.

A report that aborts is a *failure report*: ``status = REVIEW_REQUIRED``,
``is_valid = False`` and every failed gate named in ``validation_errors``. It
deliberately carries **no classification at all** (``total_issues = 0`` and no
issue summaries), so an invalid report can never be mistaken for a partial
result; ``input_entries`` still records how much input was collected.

Secret safety
-------------
The report never echoes free-form upstream text - only statuses, counts, commit
ids that matched a full-object-id shape and issue keys that matched the strict
key shape below. ``G20`` scans the *serialized* report (with the caller's
configured secrets) before it is returned, so a secret that reached an issue key
or any other report field aborts the report instead of leaking it.

State machine (S0-S11; every phase only runs when every earlier gate passed)
---------------------------------------------------------------------------
=================================== ========= ========================================
Phase                               Gates     Invariant established
=================================== ========= ========================================
S0  INPUT                           -         the input container was received
S1  INPUT_VALIDATED                 G1-G3     every entry has a safe, unique key
S2  ISSUE_RESULTS_VALIDATED         G4, G5    every T19 result is usable
S3  LIFECYCLE_RESULTS_VALIDATED     G6-G9     every T20/T21 result is usable
S4  ISSUE_OUTCOMES_AGGREGATED       G15       the T19 counts sum to the total
S5  COMMIT_OUTCOMES_AGGREGATED      G16       the T20 counts sum to the T20 results
S6  PUSH_OUTCOMES_AGGREGATED        G17       the T21 counts sum to the T21 results
S7  CROSS_STAGE_CONSISTENCY_CHECKED G10-G14   T20/T21 evidence is not contradictory
S8  OVERALL_STATUS_RESOLVED         -         the overall status is derived from counts
S9  REPORT_BUILT                    G18       the built report satisfies its invariants
S10 REPORT_VERIFIED                 G19, G20  the report is JSON-safe and secret-free
S11 COMPLETE                        -         the report is returned
=================================== ========= ========================================

A phase failure returns immediately: a later phase is never entered and the gates
it owns stay ``NOT_REACHED``. ``G18`` is evaluated on the *built* report
(:func:`verify_overall_report`), which is why it belongs to S9.

Overall status semantics
------------------------
=================================== =====================================================
Status                              Meaning
=================================== =====================================================
``EMPTY``                           no entry was supplied (never ``SUCCESS``)
``SUCCESS``                         every entry reached the policy's required end state
``PARTIAL_SUCCESS``                 at least one entry did, and at least one did not
``FAILED``                          nothing reached it and nothing needed review
``REVIEW_REQUIRED``                 the report is invalid, or nothing succeeded and at
                                    least one entry could not be classified safely
=================================== =====================================================

A ``PUSH_UNVERIFIED`` or ``COMMIT_UNVERIFIED`` issue is never a success: it is
counted in ``review_required_issues`` and its outcome is
:attr:`IssueOutcome.REVIEW_REQUIRED`. ``is_valid`` describes the *report*, not the
run: it is ``True`` whenever every gate passed and every count invariant held,
even when the classified status is ``REVIEW_REQUIRED`` (for example a single
``PUSH_UNVERIFIED`` issue). A gate failure always yields ``is_valid = False``.
"""


from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from commit_message import MAX_TOKEN_LENGTH
from commit_policy import GateStatus
from git_commit import CommitStatus
from git_push import PushStatus
from issue_status import IssueFinalStatus
from push_policy import build_refspec, is_full_commit_id, normalize_commit_id
from secret_scan import scan_for_secrets

#: Version of the T22 report contract.
REPORT_VERSION = "t22.1"

#: The evidence stages this report is built from, in lifecycle order.
SOURCES: Tuple[str, ...] = ("T19", "T20", "T21")

#: Longest issue key the report accepts. T20 validates its commit-message tokens
#: with the same bound, so a key T20 accepted can always be reported here.
MAX_ISSUE_KEY_LENGTH = MAX_TOKEN_LENGTH

#: A SonarQube issue key, in the shape T20's ``_validated_token`` accepts (no
#: whitespace, no control characters, no quotes), so it can always be rendered as
#: bare JSON text.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: The exact fields an entry mapping may carry (anything else is rejected).
_ENTRY_FIELDS: Tuple[str, ...] = (
    "issue_key",
    "issue_status",
    "commit_result",
    "push_result",
)

#: Reason a gate that was never reached carries.
_NOT_REACHED_REASON = (
    "This gate belongs to a later phase than the one the report reached."
)

#: The status vocabularies, taken from the production enums - never re-declared.
ISSUE_STATUS_ORDER: Tuple[str, ...] = tuple(
    status.value for status in IssueFinalStatus
)
COMMIT_STATUS_ORDER: Tuple[str, ...] = tuple(
    status.value for status in CommitStatus
)
PUSH_STATUS_ORDER: Tuple[str, ...] = tuple(status.value for status in PushStatus)

_T19_FIXED = IssueFinalStatus.FIXED.value
_T19_REVIEW = IssueFinalStatus.REVIEW_REQUIRED.value
_T20_COMMITTED = CommitStatus.COMMITTED.value
_T20_UNVERIFIED = CommitStatus.COMMIT_UNVERIFIED.value
_T20_ATTENTION: Tuple[str, ...] = (
    CommitStatus.COMMIT_FAILED.value,
    CommitStatus.COMMIT_UNVERIFIED.value,
)
_T21_PUSHED = PushStatus.PUSHED.value
_T21_REFUSED = PushStatus.REFUSED.value
_T21_UNVERIFIED = PushStatus.PUSH_UNVERIFIED.value
_T21_ATTENTION: Tuple[str, ...] = (
    PushStatus.PUSH_FAILED.value,
    PushStatus.PUSH_UNVERIFIED.value,
)

#: The gate catalogue: ``(gate_id, title)`` in evaluation order. Every gate is
#: always present in a report; gates whose phase did not run are ``NOT_REACHED``.
GATE_CATALOGUE: Tuple[Tuple[str, str], ...] = (
    ("G1", "input collection valid"),
    ("G2", "issue identity valid"),
    ("G3", "issue identities unique"),
    ("G4", "T19 result structurally valid"),
    ("G5", "T19 status recognised"),
    ("G6", "T20 result structurally valid"),
    ("G7", "T20 status recognised"),
    ("G8", "T21 result structurally valid"),
    ("G9", "T21 status recognised"),
    ("G10", "T20 commit evidence internally consistent"),
    ("G11", "T21 push evidence internally consistent"),
    ("G12", "T20/T21 commit identity consistent"),
    ("G13", "T21 push provenance consistent with T20"),
    ("G14", "no contradictory lifecycle states"),
    ("G15", "T19 status counts sum to the total"),
    ("G16", "T20 status counts sum to the T20 results"),
    ("G17", "T21 status counts sum to the T21 results"),
    ("G18", "overall classification invariant holds"),
    ("G19", "report serialization valid"),
    ("G20", "report contains no secret material"),
)


class OverallReportError(ValueError):
    """A caller error: an argument T22 cannot interpret at all.

    Raised only for programming errors (a ``policy`` that is not an
    :class:`OverallReportPolicy`, or a ``forbidden_secrets`` value that is a bare
    string), never for evidence problems - those always become a fail-closed
    ``REVIEW_REQUIRED`` report instead.
    """


class OverallStatus(Enum):
    """How the whole run ended (T22's one summary classification)."""

    __test__ = False

    EMPTY = "empty"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial-success"
    FAILED = "failed"
    REVIEW_REQUIRED = "review-required"


class OverallReportPhase(Enum):
    """Explicit T22 state machine (see the module docstring)."""

    __test__ = False

    INPUT = "input"
    INPUT_VALIDATED = "input-validated"
    ISSUE_RESULTS_VALIDATED = "issue-results-validated"
    LIFECYCLE_RESULTS_VALIDATED = "lifecycle-results-validated"
    ISSUE_OUTCOMES_AGGREGATED = "issue-outcomes-aggregated"
    COMMIT_OUTCOMES_AGGREGATED = "commit-outcomes-aggregated"
    PUSH_OUTCOMES_AGGREGATED = "push-outcomes-aggregated"
    CROSS_STAGE_CONSISTENCY_CHECKED = "cross-stage-consistency-checked"
    OVERALL_STATUS_RESOLVED = "overall-status-resolved"
    REPORT_BUILT = "report-built"
    REPORT_VERIFIED = "report-verified"
    COMPLETE = "complete"


class ExpectedEndState(Enum):
    """How far a successful lifecycle must go for the active T22 policy."""

    __test__ = False

    #: ``FIXED`` + ``COMMITTED`` + ``PUSHED`` (the default, most complete).
    PUSHED = "pushed"
    #: ``FIXED`` + ``COMMITTED`` (a local-only delivery).
    COMMITTED = "committed"
    #: ``FIXED`` only (this run's scope ends at the verified fix).
    ISSUE_FIXED = "issue-fixed"


class ReportDecision(Enum):
    """What the caller is expected to do with the report (fail closed)."""

    __test__ = False

    #: The run is complete and successful; no human is required.
    PROCEED = "proceed"
    #: A human must look at the run before anything else happens.
    REVIEW_REQUIRED = "review-required"


class IssueOutcome(Enum):
    """Per-issue T22 outcome (a lifecycle summary, not a T23 report)."""

    __test__ = False

    #: The issue reached the end state the policy requires.
    DELIVERED = "delivered"
    #: The issue is fixed, but a required later stage did not complete.
    NOT_DELIVERED = "not-delivered"
    #: T19 did not verify the issue as fixed (a definite, non-ambiguous failure).
    NOT_FIXED = "not-fixed"
    #: The evidence is ambiguous, missing or contradictory; a human must look.
    REVIEW_REQUIRED = "review-required"


#: Which phase owns each gate. A gate is recorded only while its own phase runs,
#: so a gate whose phase never ran stays ``NOT_REACHED`` in the report.
GATE_PHASE: Mapping[str, OverallReportPhase] = {
    "G1": OverallReportPhase.INPUT_VALIDATED,
    "G2": OverallReportPhase.INPUT_VALIDATED,
    "G3": OverallReportPhase.INPUT_VALIDATED,
    "G4": OverallReportPhase.ISSUE_RESULTS_VALIDATED,
    "G5": OverallReportPhase.ISSUE_RESULTS_VALIDATED,
    "G6": OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
    "G7": OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
    "G8": OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
    "G9": OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
    "G10": OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G11": OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G12": OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G13": OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G14": OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G15": OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED,
    "G16": OverallReportPhase.COMMIT_OUTCOMES_AGGREGATED,
    "G17": OverallReportPhase.PUSH_OUTCOMES_AGGREGATED,
    "G18": OverallReportPhase.REPORT_BUILT,
    "G19": OverallReportPhase.REPORT_VERIFIED,
    "G20": OverallReportPhase.REPORT_VERIFIED,
}



@dataclass(frozen=True)
class OverallReportPolicy:
    """How far a lifecycle must go before T22 counts an issue as successful.

    The default is the most complete (and therefore most conservative) choice:
    an issue only ``SUCCESS`` when the fix was verified, committed *and* pushed.

    Attributes:
        expected_end_state: the end state every ``FIXED`` issue must reach.
    """

    __test__ = False

    expected_end_state: ExpectedEndState = ExpectedEndState.PUSHED

    @property
    def requires_commit_evidence(self) -> bool:
        """True when a ``FIXED`` issue must carry a T20 result."""
        return self.expected_end_state in (
            ExpectedEndState.COMMITTED,
            ExpectedEndState.PUSHED,
        )

    @property
    def requires_push_evidence(self) -> bool:
        """True when a ``FIXED``/``COMMITTED`` issue must carry a T21 result."""
        return self.expected_end_state is ExpectedEndState.PUSHED

    def as_dict(self) -> dict:
        """Plain, deterministic summary."""
        return {
            "expected_end_state": self.expected_end_state.value,
            "requires_commit_evidence": self.requires_commit_evidence,
            "requires_push_evidence": self.requires_push_evidence,
        }


@dataclass(frozen=True)
class IssueLifecycleInput:
    """One issue's already-produced lifecycle evidence (T22's input).

    Neither the T19 result (a status plus its evidence) nor the T20/T21 results
    (repository/remote evidence) carry the issue key, so the caller pairs them
    here. T22 never derives a key from a commit message, a branch or a path.

    Attributes:
        issue_key: the SonarQube issue key the evidence belongs to.
        issue_status: the T19 ``IssueStatusResult`` object or ``as_dict()`` view.
        commit_result: the T20 ``CommitResult`` object or view, or ``None`` when
            T20 did not run.
        push_result: the T21 ``PushResult`` object or view, or ``None`` when T21
            did not run.
    """

    __test__ = False

    issue_key: str
    issue_status: object = None
    commit_result: object = None
    push_result: object = None

    def as_dict(self) -> dict:
        """The declared identity only; the results are projected separately."""
        return {
            "issue_key": self.issue_key,
            "has_issue_status": self.issue_status is not None,
            "has_commit_result": self.commit_result is not None,
            "has_push_result": self.push_result is not None,
        }


@dataclass(frozen=True)
class OverallGate:
    """One evaluated T22 gate (same shape as the T20/T21 gate records)."""

    __test__ = False

    gate_id: str
    title: str
    status: GateStatus
    reason: str

    @property
    def passed(self) -> bool:
        """True only for an explicit pass."""
        return self.status is GateStatus.PASS

    @property
    def failed(self) -> bool:
        """True only for an explicit failure."""
        return self.status is GateStatus.FAIL

    @property
    def not_reached(self) -> bool:
        """True when the gate's phase never ran."""
        return self.status is GateStatus.NOT_REACHED

    def as_dict(self) -> dict:
        """Plain, deterministic summary (never contains a secret value)."""
        return {
            "gate_id": self.gate_id,
            "title": self.title,
            "status": self.status.value,
            "reason": self.reason,
            "passed": self.passed,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class ReportStageRecord:
    """One line of the T22 state-machine trail (never contains a secret)."""

    __test__ = False

    phase: OverallReportPhase
    detail: str

    def as_dict(self) -> dict:
        """Plain summary."""
        return {"phase": self.phase.value, "detail": self.detail}



@dataclass(frozen=True)
class OverallReportState:
    """Where the state machine stopped and what every gate decided.

    Attributes:
        phase: the last phase that completed.
        failed_phase: the phase that aborted the report (``None`` when complete).
        decision: ``PROCEED`` only for a ``SUCCESS`` report.
        gates: all 20 gates, in catalogue order.
        stage_records: the phase trail, in order.
    """

    __test__ = False

    phase: OverallReportPhase
    failed_phase: Optional[OverallReportPhase]
    decision: ReportDecision
    gates: Tuple[OverallGate, ...]
    stage_records: Tuple[ReportStageRecord, ...]

    @property
    def reached_phases(self) -> Tuple[OverallReportPhase, ...]:
        """Every phase that completed, in order."""
        return tuple(record.phase for record in self.stage_records)

    @property
    def is_complete(self) -> bool:
        """True only when the report reached ``COMPLETE``."""
        return self.phase is OverallReportPhase.COMPLETE

    def status_of(self, gate_id: str) -> Optional[GateStatus]:
        """The status of ``gate_id``, or ``None`` when it is unknown."""
        return next(
            (gate.status for gate in self.gates if gate.gate_id == gate_id), None
        )

    def gate(self, gate_id: str) -> Optional[OverallGate]:
        """The :class:`OverallGate` record for ``gate_id``, or ``None``."""
        return next(
            (gate for gate in self.gates if gate.gate_id == gate_id), None
        )

    @property
    def passed_gates(self) -> Tuple[OverallGate, ...]:
        """Every gate that passed."""
        return tuple(gate for gate in self.gates if gate.passed)

    @property
    def failed_gates(self) -> Tuple[OverallGate, ...]:
        """Every gate that failed."""
        return tuple(gate for gate in self.gates if gate.failed)

    @property
    def not_reached_gates(self) -> Tuple[OverallGate, ...]:
        """Every gate whose phase never ran."""
        return tuple(gate for gate in self.gates if gate.not_reached)

    @property
    def first_failure(self) -> Optional[OverallGate]:
        """The first gate that failed, or ``None``."""
        remaining = self.failed_gates
        return remaining[0] if remaining else None

    def as_dict(self) -> dict:
        """Plain, deterministic summary."""
        return {
            "phase": self.phase.value,
            "failed_phase": (
                self.failed_phase.value if self.failed_phase else None
            ),
            "decision": self.decision.value,
            "is_complete": self.is_complete,
            "reached_phases": [
                phase.value for phase in self.reached_phases
            ],
            "gates": [gate.as_dict() for gate in self.gates],
            "stage_records": [
                record.as_dict() for record in self.stage_records
            ],
        }


@dataclass(frozen=True)
class IssueOverallSummary:
    """Compact per-issue lifecycle summary (T22's explanation of its counts).

    This is deliberately *not* T23: it carries only the lifecycle facts T22 needs
    to explain an aggregation, never a copy of the T19/T20/T21 DTOs and never
    free-form upstream text.

    Attributes:
        issue_key: the validated SonarQube issue key.
        t19_status / t20_status / t21_status: the exact source statuses, or
            ``None`` when that stage produced no result.
        outcome: T22's classification for this issue.
        issue_fixed / change_committed / change_pushed: the three stages, kept
            separate (``FIXED`` is not ``COMMITTED`` is not ``PUSHED``).
        end_to_end_success: True only for :attr:`IssueOutcome.DELIVERED`.
        needs_attention: True when a human must look at this issue.
        commit_sha: the T20 commit (``None`` when T20 did not commit).
        push_commit: the commit T21 was asked to push.
        reason: T22's own deterministic explanation (built from statuses only).
    """

    __test__ = False

    issue_key: str
    t19_status: str
    t20_status: Optional[str]
    t21_status: Optional[str]
    outcome: IssueOutcome
    issue_fixed: bool
    change_committed: bool
    change_pushed: bool
    end_to_end_success: bool
    needs_attention: bool
    commit_sha: Optional[str]
    push_commit: Optional[str]
    reason: str

    def as_dict(self) -> dict:
        """Plain, deterministic, secret-free summary."""
        return {
            "issue_key": self.issue_key,
            "t19_status": self.t19_status,
            "t20_status": self.t20_status,
            "t21_status": self.t21_status,
            "outcome": self.outcome.value,
            "issue_fixed": self.issue_fixed,
            "change_committed": self.change_committed,
            "change_pushed": self.change_pushed,
            "end_to_end_success": self.end_to_end_success,
            "needs_attention": self.needs_attention,
            "commit_sha": self.commit_sha,
            "push_commit": self.push_commit,
            "reason": self.reason,
        }



def _ordered_counts(
    counts: Mapping[str, int], order: Sequence[str]
) -> Dict[str, int]:
    """The complete, canonically ordered count map for one status vocabulary."""
    return {status: int(counts.get(status, 0)) for status in order}


@dataclass(frozen=True)
class OverallReport:
    """Immutable, deterministic, secret-free aggregation of one T22 run.

    Attributes:
        status: the overall classification (:class:`OverallStatus`).
        is_valid: True only when every gate passed and every invariant held.
        decision: what the caller should do next (fail closed).
        policy: the policy the classification used, so the report explains itself.
        input_entries: how many lifecycle entries were collected.
        total_issues: how many entries were safely classified.
        successful_issues / failed_issues / review_required_issues: mutually
            exclusive buckets that sum to ``total_issues``.
        issue_counts / commit_counts / push_counts: exact per-status counts over
            the production status vocabularies (always the complete key set).
        commit_result_count / push_result_count: how many T20/T21 results were
            supplied, i.e. the stages that actually ran.
        commit_not_executed_count / push_not_executed_count: how many classified
            entries carried no T20/T21 result. "Not executed" is *not* a T20/T21
            status - it is the absence of one - so it is counted separately and
            is never folded into a status count.
        issue_outcomes: the per-issue summaries, sorted by issue key.
        reasons: ordered, deterministic explanation lines.
        validation_errors: every gate/invariant violation (empty when valid).
        state: the state machine's final position plus all 20 gate verdicts.
        report_version: the T22 report contract version.
    """

    __test__ = False

    status: OverallStatus
    is_valid: bool
    decision: ReportDecision
    policy: OverallReportPolicy
    input_entries: int
    total_issues: int
    successful_issues: int
    failed_issues: int
    review_required_issues: int
    issue_counts: Mapping[str, int]
    commit_counts: Mapping[str, int]
    push_counts: Mapping[str, int]
    commit_result_count: int
    commit_not_executed_count: int
    push_result_count: int
    push_not_executed_count: int
    issue_outcomes: Tuple[IssueOverallSummary, ...]
    reasons: Tuple[str, ...]
    validation_errors: Tuple[str, ...]
    state: OverallReportState
    report_version: str = REPORT_VERSION

    @property
    def end_to_end_success_count(self) -> int:
        """Issues that reached the policy's required end state."""
        return self.successful_issues

    @property
    def end_to_end_failure_count(self) -> int:
        """Issues that were classified as a definite delivery failure."""
        return self.failed_issues

    @property
    def end_to_end_review_count(self) -> int:
        """Issues whose outcome could not be classified safely."""
        return self.review_required_issues

    @property
    def generated_from(self) -> Tuple[str, ...]:
        """The evidence stages this report type is built from."""
        return SOURCES

    @property
    def issue_keys(self) -> Tuple[str, ...]:
        """The classified issue keys, in report order."""
        return tuple(summary.issue_key for summary in self.issue_outcomes)

    @property
    def is_success(self) -> bool:
        """True only for an overall ``SUCCESS``."""
        return self.status is OverallStatus.SUCCESS

    @property
    def is_empty(self) -> bool:
        """True only when nothing was supplied."""
        return self.status is OverallStatus.EMPTY

    @property
    def needs_attention(self) -> bool:
        """True when a human must look at the run.

        A clean ``SUCCESS`` or ``EMPTY`` report only needs attention when an
        individual issue asked for it (for example a ``PUSH_FAILED`` issue whose
        remote T21 could not touch); every other status needs a human by
        definition.
        """
        if any(summary.needs_attention for summary in self.issue_outcomes):
            return True
        return self.status not in (OverallStatus.SUCCESS, OverallStatus.EMPTY)


    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the whole report."""
        return {
            "report_version": self.report_version,
            "status": self.status.value,
            "is_valid": self.is_valid,
            "decision": self.decision.value,
            "needs_attention": self.needs_attention,
            "generated_from": list(self.generated_from),
            "input_entries": self.input_entries,
            "total_issues": self.total_issues,
            "successful_issues": self.successful_issues,
            "failed_issues": self.failed_issues,
            "review_required_issues": self.review_required_issues,
            "end_to_end_success_count": self.end_to_end_success_count,
            "end_to_end_failure_count": self.end_to_end_failure_count,
            "end_to_end_review_count": self.end_to_end_review_count,
            "issue_counts": _ordered_counts(
                self.issue_counts, ISSUE_STATUS_ORDER
            ),
            "commit_counts": _ordered_counts(
                self.commit_counts, COMMIT_STATUS_ORDER
            ),
            "push_counts": _ordered_counts(
                self.push_counts, PUSH_STATUS_ORDER
            ),
            "commit_result_count": self.commit_result_count,
            "commit_not_executed_count": self.commit_not_executed_count,
            "push_result_count": self.push_result_count,
            "push_not_executed_count": self.push_not_executed_count,
            "issue_outcomes": [
                summary.as_dict() for summary in self.issue_outcomes
            ],
            "reasons": list(self.reasons),
            "validation_errors": list(self.validation_errors),
            "policy": self.policy.as_dict(),
            "state": self.state.as_dict(),
        }



# ---------------------------------------------------------------------------
# Reading the evidence (pure, no I/O, never mutates the input)
# ---------------------------------------------------------------------------


def _view(result: object) -> Optional[Mapping]:
    """Project a supplied result into its ``as_dict()`` mapping, or ``None``.

    Mirrors T21's projection: a mapping is used as-is, an object must expose a
    callable ``as_dict()`` that returns a mapping. ``asdict()`` is never used and
    private attributes are never read, so a result can only contribute the fields
    it deliberately publishes in its own secret-free view.
    """
    try:
        if isinstance(result, Mapping):
            return result
        as_dict = getattr(result, "as_dict", None)
        if not callable(as_dict):
            return None
        payload = as_dict()
    except Exception:  # defensive: an unusable view is a gate failure
        return None
    return payload if isinstance(payload, Mapping) else None


def _strict_text(value: object) -> Optional[str]:
    """A non-empty text value with no surrounding whitespace, else ``None``.

    No value is ever repaired, trimmed or re-encoded: a padded status is a
    malformed status, not a status to normalise.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


def _text(value: object) -> Optional[str]:
    """A non-empty text value (never trimmed), or ``None`` when absent."""
    return value if isinstance(value, str) and value.strip() else None


def _as_bool(value: object) -> Optional[bool]:
    """A real ``bool``, or ``None`` when absent/not a boolean.

    ``1``/``0``/``"true"`` are rejected: a truthy stand-in is not evidence.
    """
    return value if isinstance(value, bool) else None


def _full_id(value: object) -> Optional[str]:
    """The normalized full commit id, or ``None`` when it is not one.

    Case folding and surrounding whitespace follow T21's
    :func:`push_policy.normalize_commit_id`, so evidence T21 accepted is accepted
    here too.
    """
    if not isinstance(value, str):
        return None
    return normalize_commit_id(value) if is_full_commit_id(value) else None


def _present(value: object) -> bool:
    """True when a value is present at all (not ``None`` and not empty text)."""
    return value is not None and value != ""


@dataclass(frozen=True)
class _LifecycleFacts:
    """The exact evidence T22 reads out of one entry (never free-form text)."""

    issue_key: str
    t19_status: str
    t20_status: Optional[str] = None
    t20_is_committed: Optional[bool] = None
    t20_needs_attention: Optional[bool] = None
    t20_new_head: Optional[str] = None
    t20_previous_head: Optional[str] = None
    t20_commit_sha: Optional[str] = None
    t20_branch: Optional[str] = None
    t20_message_issue_key: Optional[str] = None
    t21_status: Optional[str] = None
    t21_is_pushed: Optional[bool] = None
    t21_is_refusal: Optional[bool] = None
    t21_needs_attention: Optional[bool] = None
    t21_push_attempted: Optional[bool] = None
    t21_expected_commit: Optional[str] = None
    t21_remote_before_commit: Optional[str] = None
    t21_remote_after_commit: Optional[str] = None
    t21_branch: Optional[str] = None
    t21_remote_branch: Optional[str] = None
    t21_refspec: Optional[str] = None

    @property
    def has_commit(self) -> bool:
        """True when a T20 result was supplied."""
        return self.t20_status is not None

    @property
    def has_push(self) -> bool:
        """True when a T21 result was supplied."""
        return self.t21_status is not None

    @property
    def issue_fixed(self) -> bool:
        """True only when T19 verified the issue as fixed."""
        return self.t19_status == _T19_FIXED

    @property
    def change_committed(self) -> bool:
        """True only when T20 reported a fully verified commit."""
        return self.t20_status == _T20_COMMITTED

    @property
    def change_pushed(self) -> bool:
        """True only when T21 reported a fully verified push."""
        return self.t21_status == _T21_PUSHED

    @property
    def push_attempted(self) -> bool:
        """True when a push is proven to have been attempted.

        A verified or unverified push is proof by definition; ``PUSH_FAILED``
        counts only when the result its own ``push_attempted`` flag says so (T21
        also reports ``PUSH_FAILED`` when the command could not be launched).
        """
        if self.t21_push_attempted is True:
            return True
        return self.t21_status in (_T21_PUSHED, _T21_UNVERIFIED)

    @property
    def commit_sha(self) -> Optional[str]:
        """The T20 commit this run created (``None`` when T20 did not commit)."""
        return self.t20_new_head

    @property
    def push_commit(self) -> Optional[str]:
        """The commit T21 was asked to push."""
        return self.t21_expected_commit



def _collect_entries(value: object) -> Optional[Tuple[object, ...]]:
    """Materialise the supplied collection, or ``None`` when it is not one.

    A bare string/bytes (almost always a caller bug), a mapping (an entry keyed
    by something else) and a non-iterable are all refused instead of being
    guessed at.
    """
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        return None
    if not isinstance(value, Iterable):
        return None
    try:
        return tuple(value)
    except Exception:  # a hostile/one-shot iterable is not usable input
        return None


def _coerce_entry(item: object) -> Optional[IssueLifecycleInput]:
    """Turn one supplied item into an :class:`IssueLifecycleInput`, or ``None``.

    An ``IssueLifecycleInput`` is used as-is; a mapping is accepted only when
    every key it carries is part of the documented entry contract, so a
    duck-typed blob with extra fields cannot slip through.
    """
    if isinstance(item, IssueLifecycleInput):
        return item
    if not isinstance(item, Mapping):
        return None
    try:
        if any(name not in _ENTRY_FIELDS for name in item):
            return None
        return IssueLifecycleInput(
            issue_key=item.get("issue_key"),
            issue_status=item.get("issue_status"),
            commit_result=item.get("commit_result"),
            push_result=item.get("push_result"),
        )
    except Exception:  # a mapping that raises is not a usable entry
        return None


def _identity_problems(
    coerced: Sequence[Optional[IssueLifecycleInput]],
) -> Dict[str, Tuple[str, ...]]:
    """The ``G2``/``G3`` problems for the supplied entries.

    Messages are de-duplicated and sorted, so the same input always produces the
    same report regardless of the order the entries arrived in - and no supplied
    value is ever echoed.
    """
    shape: List[str] = []
    keys: Dict[str, int] = {}
    for entry in coerced:
        if entry is None:
            shape.append(
                "An entry is not an IssueLifecycleInput and not a mapping of "
                "the entry contract's fields."
            )
            continue
        key = entry.issue_key
        if not isinstance(key, str):
            shape.append(
                "An entry has an issue key that is not text (received a "
                f"{type(key).__name__})."
            )
            continue
        if not key:
            shape.append("An entry has an empty issue key.")
            continue
        if key != key.strip():
            shape.append("An entry has an issue key with surrounding whitespace.")
            continue
        if len(key) > MAX_ISSUE_KEY_LENGTH:
            shape.append(
                "An entry has an issue key longer than "
                f"{MAX_ISSUE_KEY_LENGTH} characters."
            )
            continue
        if not _ISSUE_KEY_RE.match(key):
            shape.append(
                "An entry has an issue key that is not a valid SonarQube key."
            )
            continue
        keys[key] = keys.get(key, 0) + 1

    duplicates = tuple(
        "Two entries declare the same issue key (duplicate issue identity)."
        for count in keys.values()
        if count > 1
    )
    return {"G2": tuple(sorted(set(shape))), "G3": duplicates}


def _id_problems(
    view: Mapping,
    gate_id: str,
    stage: str,
    field_names: Sequence[str],
) -> List[Tuple[str, str]]:
    """Problems for present-but-unusable commit ids (never echoes the id)."""
    problems: List[Tuple[str, str]] = []
    for name in field_names:
        if _present(view.get(name)) and _full_id(view.get(name)) is None:
            problems.append(
                (
                    gate_id,
                    f"An entry's {stage} result reports {name} that is not a "
                    "full commit id.",
                )
            )
    return problems


def _text_problems(
    view: Mapping,
    gate_id: str,
    stage: str,
    field_names: Sequence[str],
) -> List[Tuple[str, str]]:
    """Problems for present-but-unusable text fields (never echoes the value)."""
    problems: List[Tuple[str, str]] = []
    for name in field_names:
        if _present(view.get(name)) and _text(view.get(name)) is None:
            problems.append(
                (
                    gate_id,
                    f"An entry's {stage} result reports {name} that is not text.",
                )
            )
    return problems


def _commit_message_issue_key(view: Mapping) -> Optional[str]:
    """The issue key a T20 commit message carries, when one is published."""
    message = view.get("commit_message")
    if not isinstance(message, Mapping):
        return None
    return _text(message.get("issue_key"))



def _examine_entry(
    entry: IssueLifecycleInput,
) -> Tuple[Optional[_LifecycleFacts], Tuple[Tuple[str, str], ...]]:
    """Read one entry's T19/T20/T21 evidence into typed facts.

    Returns ``(facts, problems)`` where ``problems`` is a list of
    ``(gate_id, message)`` pairs. ``facts`` is ``None`` when the T19 result is
    unusable, because nothing can be classified from it; the T20/T21 blocks are
    then not examined (the pipeline aborts at ``G4``/``G5`` anyway).

    Only statuses, booleans, commit ids and branch names are read. Free-form
    upstream text (reasons, stage trails, gate verdicts) is never copied, so no
    upstream sentence can reach the report.
    """
    problems: List[Tuple[str, str]] = []

    # ---- T19 ------------------------------------------------------------
    t19_view: Optional[Mapping] = None
    if entry.issue_status is None:
        problems.append(("G4", "An entry carries no T19 issue status result."))
    else:
        t19_view = _view(entry.issue_status)
        if t19_view is None:
            problems.append(
                (
                    "G4",
                    "An entry's T19 result could not be read as a mapping of "
                    "fields.",
                )
            )
    t19_status = None if t19_view is None else _strict_text(t19_view.get("status"))
    if t19_view is not None and t19_status is None:
        problems.append(
            ("G4", "An entry's T19 result does not carry a well-formed status.")
        )
    if t19_status is not None:
        is_fixed = _as_bool(t19_view.get("is_fixed"))
        if is_fixed is not None and is_fixed != (t19_status == _T19_FIXED):
            problems.append(
                (
                    "G4",
                    "An entry's T19 result contradicts itself: is_fixed does "
                    "not match its status.",
                )
            )
        needs_review = _as_bool(t19_view.get("needs_review"))
        if needs_review is not None and needs_review != (t19_status == _T19_REVIEW):
            problems.append(
                (
                    "G4",
                    "An entry's T19 result contradicts itself: needs_review "
                    "does not match its status.",
                )
            )
        if t19_status not in ISSUE_STATUS_ORDER:
            problems.append(
                (
                    "G5",
                    "An entry's T19 result reports a status T22 does not "
                    "recognise.",
                )
            )
    if t19_status is None:
        return None, tuple(problems)

    # ---- T20 ------------------------------------------------------------
    t20_status: Optional[str] = None
    t20_is_committed: Optional[bool] = None
    t20_needs_attention: Optional[bool] = None
    t20_new_head: Optional[str] = None
    t20_previous_head: Optional[str] = None
    t20_commit_sha: Optional[str] = None
    t20_branch: Optional[str] = None
    t20_message_issue_key: Optional[str] = None
    if entry.commit_result is not None:
        view = _view(entry.commit_result)
        if view is None:
            problems.append(
                (
                    "G6",
                    "An entry's T20 result could not be read as a mapping of "
                    "fields.",
                )
            )
        else:
            t20_status = _strict_text(view.get("status"))
            if t20_status is None:
                problems.append(
                    (
                        "G6",
                        "An entry's T20 result does not carry a well-formed "
                        "status.",
                    )
                )
            t20_is_committed = _as_bool(view.get("is_committed"))
            if t20_is_committed is None:
                problems.append(
                    (
                        "G6",
                        "An entry's T20 result does not carry a boolean "
                        "is_committed flag.",
                    )
                )
            t20_needs_attention = _as_bool(view.get("needs_attention"))
            if _present(view.get("needs_attention")) and t20_needs_attention is None:
                problems.append(
                    (
                        "G6",
                        "An entry's T20 result carries a needs_attention flag "
                        "that is not a boolean.",
                    )
                )
            t20_new_head = _full_id(view.get("new_head"))
            t20_previous_head = _full_id(view.get("previous_head"))
            t20_commit_sha = _full_id(view.get("commit_sha"))
            problems.extend(
                _id_problems(
                    view,
                    "G6",
                    "T20",
                    ("new_head", "commit_sha", "previous_head"),
                )
            )
            repository = view.get("repository")
            if isinstance(repository, Mapping):
                t20_branch = _text(repository.get("branch"))
            t20_message_issue_key = _commit_message_issue_key(view)
            if t20_status is not None and t20_status not in COMMIT_STATUS_ORDER:
                problems.append(
                    (
                        "G7",
                        "An entry's T20 result reports a status T22 does not "
                        "recognise.",
                    )
                )


    # ---- T21 ------------------------------------------------------------
    t21_status: Optional[str] = None
    t21_is_pushed: Optional[bool] = None
    t21_is_refusal: Optional[bool] = None
    t21_needs_attention: Optional[bool] = None
    t21_push_attempted: Optional[bool] = None
    t21_expected_commit: Optional[str] = None
    t21_remote_before_commit: Optional[str] = None
    t21_remote_after_commit: Optional[str] = None
    t21_branch: Optional[str] = None
    t21_remote_branch: Optional[str] = None
    t21_refspec: Optional[str] = None
    if entry.push_result is not None:
        view = _view(entry.push_result)
        if view is None:
            problems.append(
                (
                    "G8",
                    "An entry's T21 result could not be read as a mapping of "
                    "fields.",
                )
            )
        else:
            t21_status = _strict_text(view.get("status"))
            if t21_status is None:
                problems.append(
                    (
                        "G8",
                        "An entry's T21 result does not carry a well-formed "
                        "status.",
                    )
                )
            for name, target in (
                ("is_pushed", "is_pushed"),
                ("is_refusal", "is_refusal"),
                ("needs_attention", "needs_attention"),
                ("push_attempted", "push_attempted"),
            ):
                if _present(view.get(name)) and _as_bool(view.get(name)) is None:
                    problems.append(
                        (
                            "G8",
                            f"An entry's T21 result carries a {target} flag "
                            "that is not a boolean.",
                        )
                    )
            t21_is_pushed = _as_bool(view.get("is_pushed"))
            t21_is_refusal = _as_bool(view.get("is_refusal"))
            t21_needs_attention = _as_bool(view.get("needs_attention"))
            t21_push_attempted = _as_bool(view.get("push_attempted"))
            t21_expected_commit = _full_id(view.get("expected_commit"))
            t21_remote_before_commit = _full_id(view.get("remote_before_commit"))
            t21_remote_after_commit = _full_id(view.get("remote_after_commit"))
            t21_branch = _text(view.get("branch"))
            t21_remote_branch = _text(view.get("remote_branch"))
            t21_refspec = _text(view.get("refspec"))
            problems.extend(
                _id_problems(
                    view,
                    "G8",
                    "T21",
                    (
                        "expected_commit",
                        "remote_before_commit",
                        "remote_after_commit",
                    ),
                )
            )
            problems.extend(
                _text_problems(
                    view,
                    "G8",
                    "T21",
                    ("branch", "remote_branch", "refspec"),
                )
            )
            if t21_status is not None and t21_status not in PUSH_STATUS_ORDER:
                problems.append(
                    (
                        "G9",
                        "An entry's T21 result reports a status T22 does not "
                        "recognise.",
                    )
                )

    facts = _LifecycleFacts(
        issue_key=entry.issue_key,
        t19_status=t19_status,
        t20_status=t20_status,
        t20_is_committed=t20_is_committed,
        t20_needs_attention=t20_needs_attention,
        t20_new_head=t20_new_head,
        t20_previous_head=t20_previous_head,
        t20_commit_sha=t20_commit_sha,
        t20_branch=t20_branch,
        t20_message_issue_key=t20_message_issue_key,
        t21_status=t21_status,
        t21_is_pushed=t21_is_pushed,
        t21_is_refusal=t21_is_refusal,
        t21_needs_attention=t21_needs_attention,
        t21_push_attempted=t21_push_attempted,
        t21_expected_commit=t21_expected_commit,
        t21_remote_before_commit=t21_remote_before_commit,
        t21_remote_after_commit=t21_remote_after_commit,
        t21_branch=t21_branch,
        t21_remote_branch=t21_remote_branch,
        t21_refspec=t21_refspec,
    )
    return facts, tuple(problems)



def _cross_stage_problems(
    facts: _LifecycleFacts,
) -> Tuple[Tuple[str, str], ...]:
    """The ``G10``-``G14`` problems for one examined entry.

    These gates compare the evidence *within* T20 (``G10``), *within* T21
    (``G11``) and *between* the two stages (``G12``-``G14``). None of them ever
    tries to recover, re-interpret or choose the more optimistic reading: a
    disagreement is a problem, and a human must look at it.
    """
    problems: List[Tuple[str, str]] = []

    # ---- G10: T20 must agree with itself --------------------------------
    if facts.has_commit:
        status = facts.t20_status
        if facts.t20_is_committed is not None and facts.t20_is_committed != (
            status == _T20_COMMITTED
        ):
            problems.append(
                (
                    "G10",
                    "An entry's T20 result contradicts itself: is_committed "
                    "does not match its status.",
                )
            )
        if facts.t20_needs_attention is not None and facts.t20_needs_attention != (
            status in _T20_ATTENTION
        ):
            problems.append(
                (
                    "G10",
                    "An entry's T20 result contradicts itself: needs_attention "
                    "does not match its status.",
                )
            )
        if facts.change_committed:
            if facts.t20_new_head is None:
                problems.append(
                    (
                        "G10",
                        "An entry's T20 result reports a commit without a "
                        "usable commit id.",
                    )
                )
            if facts.t20_commit_sha is None:
                problems.append(
                    (
                        "G10",
                        "An entry's T20 result reports a commit without a "
                        "usable commit_sha.",
                    )
                )
            if (
                facts.t20_new_head is not None
                and facts.t20_commit_sha is not None
                and facts.t20_commit_sha != facts.t20_new_head
            ):
                problems.append(
                    (
                        "G10",
                        "An entry's T20 result reports a commit_sha that "
                        "differs from its new_head.",
                    )
                )
            if (
                facts.t20_new_head is not None
                and facts.t20_previous_head == facts.t20_new_head
            ):
                problems.append(
                    (
                        "G10",
                        "An entry's T20 result reports a commit that equals "
                        "its own previous head.",
                    )
                )


    # ---- G11: T21 must agree with itself --------------------------------
    if facts.has_push:
        status = facts.t21_status
        if facts.t21_is_pushed is not None and facts.t21_is_pushed != (
            status == _T21_PUSHED
        ):
            problems.append(
                (
                    "G11",
                    "An entry's T21 result contradicts itself: is_pushed does "
                    "not match its status.",
                )
            )
        if facts.t21_is_refusal is not None and facts.t21_is_refusal != (
            status == _T21_REFUSED
        ):
            problems.append(
                (
                    "G11",
                    "An entry's T21 result contradicts itself: is_refusal does "
                    "not match its status.",
                )
            )
        if facts.t21_needs_attention is not None and facts.t21_needs_attention != (
            status in _T21_ATTENTION
        ):
            problems.append(
                (
                    "G11",
                    "An entry's T21 result contradicts itself: needs_attention "
                    "does not match its status.",
                )
            )
        if status == _T21_PUSHED:
            if facts.t21_push_attempted is False:
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports a verified push that "
                        "was not attempted.",
                    )
                )
            if facts.t21_expected_commit is None:
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports a verified push without "
                        "the commit it pushed.",
                    )
                )
            if facts.t21_expected_commit is None or (
                facts.t21_remote_after_commit != facts.t21_expected_commit
            ):
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports a verified push whose "
                        "remote value is not the expected commit.",
                    )
                )
            if (
                facts.t21_remote_before_commit is not None
                and facts.t21_remote_before_commit == facts.t21_expected_commit
            ):
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports a verified push whose "
                        "remote value did not change.",
                    )
                )
        elif status == _T21_UNVERIFIED:
            if facts.t21_push_attempted is False:
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports an unverified push that "
                        "was not attempted.",
                    )
                )
            if facts.t21_expected_commit is None:
                problems.append(
                    (
                        "G11",
                        "An entry's T21 result reports an unverified push "
                        "without the commit it pushed.",
                    )
                )
        elif status == _T21_REFUSED and facts.t21_push_attempted is True:
            problems.append(
                (
                    "G11",
                    "An entry's T21 result reports a refused push that was "
                    "attempted.",
                )
            )
        if (
            facts.t21_refspec
            and facts.t21_branch
            and facts.t21_remote_branch
            and facts.t21_refspec
            != build_refspec(facts.t21_branch, facts.t21_remote_branch)
        ):
            problems.append(
                (
                    "G11",
                    "An entry's T21 result reports a refspec that does not "
                    "match its source and destination branches.",
                )
            )


    # ---- G12: both stages must name the same commit ----------------------
    if facts.push_attempted:
        if facts.t21_expected_commit is None:
            problems.append(
                (
                    "G12",
                    "An entry's T21 result reports a push attempt without the "
                    "commit it pushed.",
                )
            )
        elif facts.t20_new_head is None:
            problems.append(
                (
                    "G12",
                    "An entry's T21 result reports a push attempt although the "
                    "T20 commit has no usable id.",
                )
            )
        elif facts.t21_expected_commit != facts.t20_new_head:
            problems.append(
                (
                    "G12",
                    "An entry's T21 result reports a push of a different "
                    "commit than the T20 commit.",
                )
            )

    # ---- G13: a push must be provably rooted in the T20 commit -----------
    if facts.push_attempted:
        if not facts.has_commit:
            problems.append(
                (
                    "G13",
                    "An entry carries T21 push evidence without the T20 commit "
                    "result it consumes.",
                )
            )
        elif not facts.change_committed:
            problems.append(
                (
                    "G13",
                    "An entry carries T21 push evidence although T20 did not "
                    "verify a commit.",
                )
            )
        elif facts.t20_new_head is None:
            problems.append(
                (
                    "G13",
                    "An entry carries T21 push evidence although the T20 commit "
                    "has no usable id.",
                )
            )
        if (
            facts.t20_branch is not None
            and facts.t21_branch is not None
            and facts.t20_branch != facts.t21_branch
        ):
            problems.append(
                (
                    "G13",
                    "An entry's T21 result reports a different branch than the "
                    "T20 commit.",
                )
            )

    # ---- G14: the lifecycle itself must be coherent ----------------------
    if facts.has_commit and not facts.issue_fixed:
        problems.append(
            (
                "G14",
                "An entry carries T20 commit evidence although T19 did not "
                "verify the issue as fixed.",
            )
        )
    if facts.has_push and not facts.has_commit:
        problems.append(
            (
                "G14",
                "An entry carries T21 push evidence without a T20 commit "
                "result.",
            )
        )
    if (
        facts.t20_message_issue_key is not None
        and facts.t20_message_issue_key != facts.issue_key
    ):
        problems.append(
            (
                "G14",
                "An entry's T20 commit message names a different issue key than "
                "the entry declares.",
            )
        )

    return tuple(problems)



# ---------------------------------------------------------------------------
# Aggregation (pure): counts, classification, resolution
# ---------------------------------------------------------------------------


class _GateRun:
    """Collects gate verdicts for one run (later phases stay ``NOT_REACHED``)."""

    def __init__(self) -> None:
        self._verdicts: Dict[str, Tuple[GateStatus, str]] = {}

    def record(
        self, gate_id: str, problems: Sequence[str], *, ok_reason: str
    ) -> GateStatus:
        """Record ``gate_id``: ``FAIL`` with the joined problems, else ``PASS``."""
        if problems:
            return self.fail(gate_id, "; ".join(problems))
        self._verdicts[gate_id] = (GateStatus.PASS, ok_reason)
        return GateStatus.PASS

    def fail(self, gate_id: str, problem: str) -> GateStatus:
        """Record an explicit failure for ``gate_id`` (overwrites a verdict)."""
        self._verdicts[gate_id] = (GateStatus.FAIL, problem)
        return GateStatus.FAIL

    def unrecord(self, gate_id: str) -> None:
        """Drop a verdict, so the gate reports ``NOT_REACHED`` again.

        Used when a gate was provisionally recorded as a pass for the report that
        was about to be returned but the verification then failed: the gate never
        actually ran, and a failure report must not claim it passed.
        """
        self._verdicts.pop(gate_id, None)

    def records(self) -> Tuple[OverallGate, ...]:
        """All 20 gate records, in catalogue order."""
        records: List[OverallGate] = []
        for gate_id, title in GATE_CATALOGUE:
            verdict = self._verdicts.get(gate_id)
            if verdict is None:
                records.append(
                    OverallGate(
                        gate_id, title, GateStatus.NOT_REACHED, _NOT_REACHED_REASON
                    )
                )
            else:
                records.append(OverallGate(gate_id, title, verdict[0], verdict[1]))
        return tuple(records)


def _tagged(
    problems: Sequence[Tuple[str, str]], gate_id: str
) -> Tuple[str, ...]:
    """The de-duplicated, sorted messages recorded against ``gate_id``.

    Sorting and de-duplicating keeps the report byte-identical no matter which
    order the entries were supplied in.
    """
    return tuple(
        sorted({message for problem_gate, message in problems if problem_gate == gate_id})
    )


def _count_problems(label: str, summed: int, expected: int) -> Tuple[str, ...]:
    """A count-invariant problem, or ``()`` when the invariant holds."""
    if summed == expected:
        return ()
    return (
        f"The {label} status counts sum to {summed}, not to the {expected} "
        "supplied result(s).",
    )


def _issue_counts(facts: Sequence[_LifecycleFacts]) -> Dict[str, int]:
    """Exact T19 status counts (every status key is always present)."""
    counts = {status: 0 for status in ISSUE_STATUS_ORDER}
    for item in facts:
        counts[item.t19_status] += 1
    return counts


def _commit_counts(facts: Sequence[_LifecycleFacts]) -> Dict[str, int]:
    """Exact T20 status counts for the T20 results that were supplied."""
    counts = {status: 0 for status in COMMIT_STATUS_ORDER}
    for item in facts:
        if item.t20_status is not None:
            counts[item.t20_status] += 1
    return counts


def _push_counts(facts: Sequence[_LifecycleFacts]) -> Dict[str, int]:
    """Exact T21 status counts for the T21 results that were supplied."""
    counts = {status: 0 for status in PUSH_STATUS_ORDER}
    for item in facts:
        if item.t21_status is not None:
            counts[item.t21_status] += 1
    return counts



def _classify(
    facts: _LifecycleFacts, policy: OverallReportPolicy
) -> Tuple[IssueOutcome, str]:
    """Classify one issue's lifecycle, and explain it in T22's own words.

    The ladder is deliberately explicit and fail closed:

    1. uncertainty first - a ``REVIEW_REQUIRED`` T19, or an unverified T20/T21
       commit/push, is never a success and never a failure;
    2. a non-``FIXED`` T19 is a definite ``NOT_FIXED``;
    3. otherwise the policy's required end state decides: a stage the policy
       requires but that produced *no* result is ``REVIEW_REQUIRED`` (missing
       evidence is never assumed), while a stage that ran and resolved
       (refused/failed) is ``NOT_DELIVERED``;
    4. only the required end state being *proven* yields ``DELIVERED``.

    The reason is built from statuses only, so no upstream sentence is ever
    copied into the report.
    """
    if facts.t19_status == _T19_REVIEW:
        return (
            IssueOutcome.REVIEW_REQUIRED,
            "T19 could not decide this issue and asked for a review.",
        )
    if facts.t20_status == _T20_UNVERIFIED:
        return (
            IssueOutcome.REVIEW_REQUIRED,
            "T20 reported a commit whose correctness could not be proven.",
        )
    if facts.t21_status == _T21_UNVERIFIED:
        return (
            IssueOutcome.REVIEW_REQUIRED,
            "T21 reported a push whose effect on the remote could not be proven.",
        )
    if not facts.issue_fixed:
        return (
            IssueOutcome.NOT_FIXED,
            "T19 reported '" + facts.t19_status + "', which is not a verified fix.",
        )
    if policy.expected_end_state is ExpectedEndState.ISSUE_FIXED:
        return (
            IssueOutcome.DELIVERED,
            "T19 verified the fix, which is the end state this policy requires.",
        )
    if not facts.change_committed:
        if facts.t20_status is None:
            return (
                IssueOutcome.REVIEW_REQUIRED,
                "The issue is fixed but no T20 commit result was supplied, so "
                "the commit stage cannot be verified.",
            )
        return (
            IssueOutcome.NOT_DELIVERED,
            "T19 verified the fix but T20 reported '" + facts.t20_status + "'.",
        )
    if policy.expected_end_state is ExpectedEndState.COMMITTED:
        return (
            IssueOutcome.DELIVERED,
            "T19 verified the fix and T20 verified the commit, which is the "
            "end state this policy requires.",
        )
    if facts.change_pushed:
        return (
            IssueOutcome.DELIVERED,
            "T19 verified the fix, T20 verified the commit and T21 verified "
            "the push to the recorded commit.",
        )
    if facts.t21_status is None:
        return (
            IssueOutcome.REVIEW_REQUIRED,
            "The fix is committed but no T21 push result was supplied, so the "
            "delivery cannot be verified.",
        )
    return (
        IssueOutcome.NOT_DELIVERED,
        "The fix is committed but T21 reported '" + facts.t21_status + "'.",
    )


def _issue_needs_attention(
    facts: _LifecycleFacts, outcome: IssueOutcome
) -> bool:
    """True when a human must look at one issue (mirrors T20/T21's own flags)."""
    return bool(
        outcome is IssueOutcome.REVIEW_REQUIRED
        or facts.t20_needs_attention is True
        or facts.t21_needs_attention is True
    )


def _summaries(
    facts: Sequence[_LifecycleFacts], policy: OverallReportPolicy
) -> Tuple[IssueOverallSummary, ...]:
    """The per-issue summaries, sorted by issue key (deterministic order)."""
    summaries: List[IssueOverallSummary] = []
    for item in sorted(facts, key=lambda fact: fact.issue_key):
        outcome, reason = _classify(item, policy)
        summaries.append(
            IssueOverallSummary(
                issue_key=item.issue_key,
                t19_status=item.t19_status,
                t20_status=item.t20_status,
                t21_status=item.t21_status,
                outcome=outcome,
                issue_fixed=item.issue_fixed,
                change_committed=item.change_committed,
                change_pushed=item.change_pushed,
                end_to_end_success=outcome is IssueOutcome.DELIVERED,
                needs_attention=_issue_needs_attention(item, outcome),
                commit_sha=item.commit_sha,
                push_commit=item.push_commit,
                reason=reason,
            )
        )
    return tuple(summaries)



def resolve_overall_status(
    *,
    is_valid: bool,
    input_entries: int,
    total_issues: int,
    successful_issues: int,
    failed_issues: int,
    review_required_issues: int,
) -> OverallStatus:
    """Derive the overall status from the outcome buckets (fail closed).

    An invalid report is always ``REVIEW_REQUIRED``; an empty input is always
    ``EMPTY`` (never ``SUCCESS``); a report with anything to review is never
    ``SUCCESS``, but it stays ``PARTIAL_SUCCESS`` as long as something did
    succeed, so the successful and the unresolved parts remain visible in one
    status instead of the unresolved part hiding the successful one. Only a run
    where nothing succeeded and something needs review is ``REVIEW_REQUIRED``.
    """
    if not is_valid:
        return OverallStatus.REVIEW_REQUIRED
    if input_entries == 0 or total_issues == 0:
        return OverallStatus.EMPTY
    if successful_issues == total_issues:
        return OverallStatus.SUCCESS
    if successful_issues > 0:
        return OverallStatus.PARTIAL_SUCCESS
    if review_required_issues > 0:
        return OverallStatus.REVIEW_REQUIRED
    return OverallStatus.FAILED


def _expected_status(
    *,
    total_issues: int,
    successful_issues: int,
    failed_issues: int,
    review_required_issues: int,
) -> OverallStatus:
    """An independent recomputation of the classification (the ``G18`` check).

    Written separately from :func:`resolve_overall_status` on purpose: a bug in
    one is caught by the other instead of being confirmed by it.
    """
    if total_issues == 0:
        return OverallStatus.EMPTY
    if (
        successful_issues == total_issues
        and failed_issues == 0
        and review_required_issues == 0
    ):
        return OverallStatus.SUCCESS
    if successful_issues > 0:
        return OverallStatus.PARTIAL_SUCCESS
    if review_required_issues > 0:
        return OverallStatus.REVIEW_REQUIRED
    return OverallStatus.FAILED



# ---------------------------------------------------------------------------
# Self-verification and serialization
# ---------------------------------------------------------------------------


def verify_overall_report(report: OverallReport) -> Tuple[str, ...]:
    """Re-check every count and classification invariant of a built report.

    Returns an ordered tuple of problems (empty when the report is internally
    consistent). This is the executable body of ``G18`` and the self-check T22
    runs on its own output, so a counting or classification bug can never be
    returned as a valid report.
    """
    problems: List[str] = []

    numbers = (
        ("input_entries", report.input_entries),
        ("total_issues", report.total_issues),
        ("successful_issues", report.successful_issues),
        ("failed_issues", report.failed_issues),
        ("review_required_issues", report.review_required_issues),
        ("commit_result_count", report.commit_result_count),
        ("commit_not_executed_count", report.commit_not_executed_count),
        ("push_result_count", report.push_result_count),
        ("push_not_executed_count", report.push_not_executed_count),
    )
    for name, value in numbers:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{name} is not a non-negative integer.")
    if problems:
        return tuple(problems)

    total = report.total_issues
    if total != (
        report.successful_issues
        + report.failed_issues
        + report.review_required_issues
    ):
        problems.append("The outcome counts do not sum to total_issues.")
    if total != len(report.issue_outcomes):
        problems.append(
            "The number of issue summaries does not match total_issues."
        )
    if total > report.input_entries:
        problems.append("More issues were classified than were collected.")
    if report.status is OverallStatus.EMPTY and report.input_entries != 0:
        problems.append("An EMPTY report must not carry collected entries.")
    if (
        report.input_entries == 0
        and report.is_valid
        and report.status is not OverallStatus.EMPTY
    ):
        problems.append("A valid report with no collected entries must be EMPTY.")

    for label, counts, order, expected in (
        (
            "issue",
            report.issue_counts,
            ISSUE_STATUS_ORDER,
            total,
        ),
        (
            "commit",
            report.commit_counts,
            COMMIT_STATUS_ORDER,
            report.commit_result_count,
        ),
        (
            "push",
            report.push_counts,
            PUSH_STATUS_ORDER,
            report.push_result_count,
        ),
    ):
        if not isinstance(counts, Mapping):
            problems.append(f"The {label} counts are not a mapping.")
            continue
        if set(counts) != set(order):
            problems.append(
                f"The {label} counts do not cover the complete status vocabulary."
            )
            continue
        if sum(counts.values()) != expected:
            problems.append(
                f"The {label} status counts do not sum to {expected}."
            )

    if report.commit_result_count + report.commit_not_executed_count != total:
        problems.append(
            "The T20 executed and not-executed counts do not sum to total_issues."
        )
    if report.push_result_count + report.push_not_executed_count != total:
        problems.append(
            "The T21 executed and not-executed counts do not sum to total_issues."
        )

    keys = report.issue_keys
    if keys != tuple(sorted(keys)):
        problems.append("The issue summaries are not sorted by issue key.")
    if len(set(keys)) != len(keys):
        problems.append("The issue summaries repeat an issue key.")

    expected_decision = (
        ReportDecision.PROCEED
        if report.status is OverallStatus.SUCCESS
        else ReportDecision.REVIEW_REQUIRED
    )
    if report.decision is not expected_decision:
        problems.append("The decision does not match the overall status.")
    if report.state.decision is not report.decision:
        problems.append(
            "The state's decision does not match the report's decision."
        )
    gate_ids = tuple(gate.gate_id for gate in report.state.gates)
    if gate_ids != tuple(gate_id for gate_id, _ in GATE_CATALOGUE):
        problems.append(
            "The gate records do not cover the complete gate catalogue in order."
        )

    if report.is_valid:
        if report.validation_errors:
            problems.append("A valid report must not carry validation errors.")
        if report.state.failed_phase is not None:
            problems.append("A valid report must not have a failed phase.")
        expected_status = _expected_status(
            total_issues=total,
            successful_issues=report.successful_issues,
            failed_issues=report.failed_issues,
            review_required_issues=report.review_required_issues,
        )
        if report.status is not expected_status:
            problems.append("The overall status does not match the outcome counts.")

    return tuple(problems)



def serialize_report(report: OverallReport) -> str:
    """The canonical, deterministic JSON text of a report.

    Sorted keys and no cosmetic whitespace, so the same input always produces
    byte-identical text and a diff of two reports is meaningful.
    """
    return json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))


def _unserializable(value: object, path: str = "payload") -> Optional[str]:
    """The path of the first value that is not plain JSON data, or ``None``.

    Paths are built from the report's own keys and list indices, never from a
    supplied value, so a violation can be reported without echoing anything.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} carries a non-text key"
            found = _unserializable(item, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _unserializable(item, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    return f"{path} carries a {type(value).__name__}"


def _json_problems(report: OverallReport) -> Tuple[str, ...]:
    """The ``G19`` verdict: is the payload plain, round-trippable JSON data?"""
    try:
        payload = report.as_dict()
    except Exception:  # defensive: an unrenderable report cannot be published
        return ("The report could not be rendered into a JSON payload.",)
    path = _unserializable(payload)
    if path is not None:
        return (f"The report payload is not JSON data: {path}.",)
    try:
        text = serialize_report(report)
    except Exception:
        return ("The report payload could not be serialized to JSON.",)
    try:
        round_tripped = json.loads(text)
    except Exception:
        return ("The serialized report could not be parsed back as JSON.",)
    if round_tripped != payload:
        return ("The report payload does not survive a JSON round trip.",)
    return ()


def _secret_problems(
    text: str, secrets: Sequence[object]
) -> Tuple[str, ...]:
    """The ``G20`` verdict: is the serialized report free of secret material?

    The scan covers the *whole* serialized payload (keys included) and reuses
    :mod:`secret_scan`, so a configured secret and a credential-bearing URL are
    both refused. A scan that could not run is not a pass.
    """
    result = scan_for_secrets(text, secrets=secrets)
    if result.ok:
        return ()
    return (
        "The serialized report could not be verified as free of secret "
        f"material: {result.reason}",
    )



def _counts_phrase(counts: Mapping[str, int], order: Sequence[str]) -> str:
    """A deterministic ``status=count`` phrase over one status vocabulary."""
    return ", ".join(
        f"{status}={int(counts.get(status, 0))}" for status in order
    )


def _failure_report(
    *,
    policy: OverallReportPolicy,
    input_entries: int,
    phase: OverallReportPhase,
    trail: Sequence[ReportStageRecord],
    gates: _GateRun,
    problems: Sequence[str] = (),
) -> OverallReport:
    """Build the fail-closed report: ``REVIEW_REQUIRED``, invalid, unclassified.

    The report keeps the gate verdicts it reached and carries *no* issue
    summaries and *no* status counts, so an invalid report can never be read as a
    partial result; ``input_entries`` still records how much input was collected
    before the state machine stopped.
    """
    records = gates.records()
    failures = tuple(
        f"{gate.gate_id} {gate.title}: {gate.reason}"
        for gate in records
        if gate.failed
    )
    last = trail[-1].phase if trail else OverallReportPhase.INPUT
    status = resolve_overall_status(
        is_valid=False,
        input_entries=input_entries,
        total_issues=0,
        successful_issues=0,
        failed_issues=0,
        review_required_issues=0,
    )
    if status is not OverallStatus.REVIEW_REQUIRED:
        # A resolver that does not honour ``is_valid=False`` is a bug; a failure
        # report must never be anything but ``REVIEW_REQUIRED``, so fail closed
        # here instead of publishing the buggy answer.
        status = OverallStatus.REVIEW_REQUIRED
    return OverallReport(
        status=status,
        is_valid=False,
        decision=ReportDecision.REVIEW_REQUIRED,
        policy=policy,
        input_entries=input_entries,
        total_issues=0,
        successful_issues=0,
        failed_issues=0,
        review_required_issues=0,
        issue_counts=_ordered_counts({}, ISSUE_STATUS_ORDER),
        commit_counts=_ordered_counts({}, COMMIT_STATUS_ORDER),
        push_counts=_ordered_counts({}, PUSH_STATUS_ORDER),
        commit_result_count=0,
        commit_not_executed_count=0,
        push_result_count=0,
        push_not_executed_count=0,
        issue_outcomes=(),
        reasons=(
            "The report was not produced: the evidence could not be verified.",
            "No issue was classified, because an unverified report must never "
            "look like a partial result.",
            f"The state machine stopped at the {phase.value} phase.",
        ),
        validation_errors=failures + tuple(problems),
        state=OverallReportState(
            phase=last,
            failed_phase=phase,
            decision=ReportDecision.REVIEW_REQUIRED,
            gates=records,
            stage_records=tuple(trail),
        ),
    )



class _Pipeline:
    """One run of the T22 state machine over one set of evidence.

    Private: the public surface is :func:`build_overall_report`. The pipeline
    holds only the phase trail, the gate verdicts and the aggregates it derived,
    and it never touches anything outside the supplied results.
    """

    def __init__(
        self,
        *,
        entries_input: object,
        policy: OverallReportPolicy,
        secrets: Tuple[object, ...],
    ) -> None:
        self._input = entries_input
        self._policy = policy
        self._secrets = secrets
        self._trail: List[ReportStageRecord] = []
        self._gates = _GateRun()
        self._entries_count = 0
        self._coerced: Tuple[Optional[IssueLifecycleInput], ...] = ()
        self._entries: Tuple[IssueLifecycleInput, ...] = ()
        self._facts: Tuple[_LifecycleFacts, ...] = ()
        self._issue_counts: Dict[str, int] = _ordered_counts(
            {}, ISSUE_STATUS_ORDER
        )
        self._commit_counts: Dict[str, int] = _ordered_counts(
            {}, COMMIT_STATUS_ORDER
        )
        self._push_counts: Dict[str, int] = _ordered_counts(
            {}, PUSH_STATUS_ORDER
        )

    # -- plumbing ---------------------------------------------------------

    def _record(self, phase: OverallReportPhase, detail: str) -> None:
        """Append one phase record to the trail."""
        self._trail.append(ReportStageRecord(phase, detail))

    def _abort(
        self, phase: OverallReportPhase, problems: Sequence[str] = ()
    ) -> OverallReport:
        """Return the fail-closed report for the phase that stopped the run."""
        return _failure_report(
            policy=self._policy,
            input_entries=self._entries_count,
            phase=phase,
            trail=self._trail,
            gates=self._gates,
            problems=problems,
        )

    def _require(
        self,
        gate_id: str,
        problems: Sequence[str],
        ok_reason: str,
        phase: OverallReportPhase,
    ) -> Optional[OverallReport]:
        """Record a gate; return the failure report when it did not pass."""
        if (
            self._gates.record(gate_id, problems, ok_reason=ok_reason)
            is GateStatus.FAIL
        ):
            return self._abort(phase)
        return None

    def run(self) -> OverallReport:
        """Advance the state machine; an unexpected error fails closed."""
        try:
            return self._advance()
        except Exception as exc:  # fail closed: never a success, never a raise
            return self._abort_unexpected(exc)

    def _abort_unexpected(self, exc: BaseException) -> OverallReport:
        """Convert an unexpected internal error into an invalid report.

        Only the exception's *type* is named: its message and repr could carry
        upstream data, and no gate is marked failed because no gate produced the
        failure. The trail keeps the phases that did complete.
        """
        return self._abort(
            self._trail[-1].phase if self._trail else OverallReportPhase.INPUT,
            (
                f"An unexpected {type(exc).__name__} interrupted the report, so "
                "nothing could be verified.",
            ),
        )

    def _advance(self) -> OverallReport:
        """Run every phase in order; the first failure ends the report."""
        for step in (
            self._collect,
            self._validate_identities,
            self._examine,
            self._aggregate,
            self._cross_check,
        ):
            failure = step()
            if failure is not None:
                return failure
        return self._finish()


    # -- S0/S1: the input and its identities ------------------------------

    def _collect(self) -> Optional[OverallReport]:
        """S0 - collect the entries (``G1`` refuses an unusable collection)."""
        collected = _collect_entries(self._input)
        if collected is None:
            self._record(
                OverallReportPhase.INPUT,
                "The input container was received but is not a usable "
                "collection of entries.",
            )
            self._gates.fail(
                "G1",
                "The input is not an iterable collection of issue lifecycle "
                f"entries (received a {type(self._input).__name__}).",
            )
            return self._abort(OverallReportPhase.INPUT_VALIDATED)
        self._entries_count = len(collected)
        noun = "entry" if self._entries_count == 1 else "entries"
        verb = "was" if self._entries_count == 1 else "were"
        self._gates.record(
            "G1",
            (),
            ok_reason=(
                "The input collection was usable "
                f"({self._entries_count} lifecycle {noun})."
            ),
        )
        self._record(
            OverallReportPhase.INPUT,
            f"{self._entries_count} lifecycle {noun} {verb} collected from the "
            "supplied collection.",
        )
        self._coerced = tuple(_coerce_entry(item) for item in collected)
        return None

    def _validate_identities(self) -> Optional[OverallReport]:
        """S1 - every entry must carry a safe, unique issue key (``G2``/``G3``)."""
        identity = _identity_problems(self._coerced)
        failure = self._require(
            "G2",
            identity["G2"],
            f"Every entry carries a valid SonarQube issue key "
            f"({self._entries_count} entries).",
            OverallReportPhase.INPUT_VALIDATED,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G3",
            identity["G3"],
            f"Every issue key is unique ({self._entries_count} entries).",
            OverallReportPhase.INPUT_VALIDATED,
        )
        if failure is not None:
            return failure
        self._entries = tuple(
            entry for entry in self._coerced if entry is not None
        )
        self._record(
            OverallReportPhase.INPUT_VALIDATED,
            f"G1-G3 passed: {self._entries_count} entries carry a safe, unique "
            "issue key.",
        )
        return None

    # -- S2/S3: the T19/T20/T21 results -----------------------------------

    def _examine(self) -> Optional[OverallReport]:
        """S2/S3 - read every result into typed facts (``G4``-``G9``)."""
        tagged: List[Tuple[str, str]] = []
        facts: List[_LifecycleFacts] = []
        for entry in self._entries:
            item, problems = _examine_entry(entry)
            tagged.extend(problems)
            if item is not None:
                facts.append(item)
        self._facts = tuple(facts)
        count = self._entries_count

        failure = self._require(
            "G4",
            _tagged(tagged, "G4"),
            f"Every entry's T19 result is structurally valid ({count} entries).",
            OverallReportPhase.ISSUE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G5",
            _tagged(tagged, "G5"),
            f"Every T19 result reports a recognised status ({count} entries).",
            OverallReportPhase.ISSUE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.ISSUE_RESULTS_VALIDATED,
            f"G4, G5 passed: {count} T19 results are usable.",
        )

        commits = sum(1 for item in self._facts if item.has_commit)
        pushes = sum(1 for item in self._facts if item.has_push)
        failure = self._require(
            "G6",
            _tagged(tagged, "G6"),
            f"Every supplied T20 result is structurally valid ({commits} "
            "supplied).",
            OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G7",
            _tagged(tagged, "G7"),
            f"Every supplied T20 result reports a recognised status ({commits} "
            "supplied).",
            OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G8",
            _tagged(tagged, "G8"),
            f"Every supplied T21 result is structurally valid ({pushes} "
            "supplied).",
            OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G9",
            _tagged(tagged, "G9"),
            f"Every supplied T21 result reports a recognised status ({pushes} "
            "supplied).",
            OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.LIFECYCLE_RESULTS_VALIDATED,
            f"G6-G9 passed: T20 evidence for {commits} issue(s) and T21 "
            f"evidence for {pushes} issue(s) are usable.",
        )
        return None


    # -- S4/S5/S6: aggregation --------------------------------------------

    def _aggregate(self) -> Optional[OverallReport]:
        """S4-S6 - aggregate the three stages (``G15``-``G17``).

        "Not executed" is never a status: a stage that produced no result is
        counted separately, so the status counts always sum to the results that
        were actually supplied.
        """
        total = len(self._facts)
        self._issue_counts = _issue_counts(self._facts)
        failure = self._require(
            "G15",
            _count_problems(
                "T19", sum(self._issue_counts.values()), total
            ),
            f"The T19 status counts sum to {total}.",
            OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.ISSUE_OUTCOMES_AGGREGATED,
            f"G15 passed: the T19 status counts sum to {total} classified "
            "issue(s).",
        )

        commits = sum(1 for item in self._facts if item.has_commit)
        self._commit_counts = _commit_counts(self._facts)
        failure = self._require(
            "G16",
            _count_problems(
                "T20", sum(self._commit_counts.values()), commits
            ),
            f"The T20 status counts sum to the {commits} supplied result(s).",
            OverallReportPhase.COMMIT_OUTCOMES_AGGREGATED,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.COMMIT_OUTCOMES_AGGREGATED,
            f"G16 passed: the T20 status counts sum to the {commits} supplied "
            f"result(s); {total - commits} issue(s) did not run T20.",
        )

        pushes = sum(1 for item in self._facts if item.has_push)
        self._push_counts = _push_counts(self._facts)
        failure = self._require(
            "G17",
            _count_problems("T21", sum(self._push_counts.values()), pushes),
            f"The T21 status counts sum to the {pushes} supplied result(s).",
            OverallReportPhase.PUSH_OUTCOMES_AGGREGATED,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.PUSH_OUTCOMES_AGGREGATED,
            f"G17 passed: the T21 status counts sum to the {pushes} supplied "
            f"result(s); {total - pushes} issue(s) did not run T21.",
        )
        return None

    # -- S7: the cross-stage gates ----------------------------------------

    def _cross_check(self) -> Optional[OverallReport]:
        """S7 - the T20/T21 evidence must not contradict itself (``G10``-``G14``)."""
        tagged: List[Tuple[str, str]] = []
        for item in self._facts:
            tagged.extend(_cross_stage_problems(item))
        total = len(self._facts)
        checks = (
            ("G10", "Every supplied T20 result agrees with itself."),
            ("G11", "Every supplied T21 result agrees with itself."),
            ("G12", "Every push names the commit T20 created."),
            ("G13", "Every push is rooted in a verified T20 commit."),
            ("G14", "The lifecycle evidence is coherent."),
        )
        for gate_id, ok_reason in checks:
            failure = self._require(
                gate_id,
                _tagged(tagged, gate_id),
                ok_reason,
                OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
            )
            if failure is not None:
                return failure
        self._record(
            OverallReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
            f"G10-G14 passed: no contradiction was found in the T20/T21 "
            f"evidence for {total} issue(s).",
        )
        return None


    # -- S8-S11: resolution, the report and its verification ---------------

    def _decision(self, status: OverallStatus) -> ReportDecision:
        """The decision that belongs to an overall status (fail closed)."""
        if status is OverallStatus.SUCCESS:
            return ReportDecision.PROCEED
        return ReportDecision.REVIEW_REQUIRED

    def _state(
        self,
        *,
        decision: ReportDecision,
        trail: Optional[Sequence[ReportStageRecord]] = None,
        failed_phase: Optional[OverallReportPhase] = None,
    ) -> OverallReportState:
        """The state record for the current trail and gate verdicts."""
        records = tuple(self._trail if trail is None else trail)
        return OverallReportState(
            phase=records[-1].phase if records else OverallReportPhase.INPUT,
            failed_phase=failed_phase,
            decision=decision,
            gates=self._gates.records(),
            stage_records=records,
        )

    def _reasons(
        self,
        *,
        status: OverallStatus,
        summaries: Sequence[IssueOverallSummary],
        successful: int,
        failed: int,
        review: int,
    ) -> Tuple[str, ...]:
        """The deterministic explanation of the classification."""
        commits = sum(1 for item in self._facts if item.has_commit)
        pushes = sum(1 for item in self._facts if item.has_push)
        lines = [
            f"Aggregated {len(summaries)} issue lifecycle observation(s) from "
            "the T19, T20 and T21 evidence.",
            "The active policy requires every fixed issue to reach "
            f"'{self._policy.expected_end_state.value}'.",
            f"{successful} issue(s) reached the required end state, {failed} "
            f"did not, and {review} need review.",
            "T19 statuses: "
            + _counts_phrase(self._issue_counts, ISSUE_STATUS_ORDER)
            + ".",
            f"T20 results supplied: {commits} ("
            + _counts_phrase(self._commit_counts, COMMIT_STATUS_ORDER)
            + ").",
            f"T21 results supplied: {pushes} ("
            + _counts_phrase(self._push_counts, PUSH_STATUS_ORDER)
            + ").",
            f"Overall status: '{status.value}'.",
        ]
        if any(summary.needs_attention for summary in summaries) or status not in (
            OverallStatus.SUCCESS,
            OverallStatus.EMPTY,
        ):
            lines.append(
                "A human must look at this run before anything else happens."
            )
        return tuple(lines)

    def _assemble(
        self,
        *,
        status: OverallStatus,
        summaries: Tuple[IssueOverallSummary, ...],
        successful: int,
        failed: int,
        review: int,
    ) -> OverallReport:
        """S9 - build the candidate report from the aggregates (pure)."""
        total = len(summaries)
        commits = sum(1 for item in self._facts if item.has_commit)
        pushes = sum(1 for item in self._facts if item.has_push)
        decision = self._decision(status)
        return OverallReport(
            status=status,
            is_valid=True,
            decision=decision,
            policy=self._policy,
            input_entries=self._entries_count,
            total_issues=total,
            successful_issues=successful,
            failed_issues=failed,
            review_required_issues=review,
            issue_counts=dict(self._issue_counts),
            commit_counts=dict(self._commit_counts),
            push_counts=dict(self._push_counts),
            commit_result_count=commits,
            commit_not_executed_count=total - commits,
            push_result_count=pushes,
            push_not_executed_count=total - pushes,
            issue_outcomes=summaries,
            reasons=self._reasons(
                status=status,
                summaries=summaries,
                successful=successful,
                failed=failed,
                review=review,
            ),
            validation_errors=(),
            state=self._state(decision=decision),
        )


    def _finish(self) -> OverallReport:
        """S8-S11 - resolve the status, build, self-verify, verify the JSON."""
        summaries = _summaries(self._facts, self._policy)
        successful = sum(
            1 for item in summaries if item.outcome is IssueOutcome.DELIVERED
        )
        review = sum(
            1
            for item in summaries
            if item.outcome is IssueOutcome.REVIEW_REQUIRED
        )
        failed = len(summaries) - successful - review
        status = resolve_overall_status(
            is_valid=True,
            input_entries=self._entries_count,
            total_issues=len(summaries),
            successful_issues=successful,
            failed_issues=failed,
            review_required_issues=review,
        )
        self._record(
            OverallReportPhase.OVERALL_STATUS_RESOLVED,
            f"The overall status resolved to '{status.value}' from "
            f"{len(summaries)} classified issue(s).",
        )

        built = self._assemble(
            status=status,
            summaries=summaries,
            successful=successful,
            failed=failed,
            review=review,
        )
        failure = self._require(
            "G18",
            verify_overall_report(built),
            "The counts and the classification are consistent "
            f"({len(summaries)} classified issue(s)).",
            OverallReportPhase.REPORT_BUILT,
        )
        if failure is not None:
            return failure
        self._record(
            OverallReportPhase.REPORT_BUILT,
            "G18 passed: the built report satisfies every count and "
            "classification invariant.",
        )

        # S10: the report that is verified is the report that is returned, so its
        # final state (including the G19/G20 pass verdicts) is built first. When a
        # gate then fails, its verdict is overwritten and the failure report is
        # returned instead, so the trail never claims a pass that did not happen.
        self._gates.record(
            "G19", (), ok_reason="The serialized report is JSON-safe."
        )
        self._gates.record(
            "G20",
            (),
            ok_reason=(
                "The serialized report matched none of the configured secrets."
            ),
        )
        verified = replace(
            built,
            state=self._state(
                decision=built.decision,
                trail=tuple(self._trail)
                + (
                    ReportStageRecord(
                        OverallReportPhase.REPORT_VERIFIED,
                        "The serialized report was verified against the JSON "
                        "and secret gates.",
                    ),
                    ReportStageRecord(
                        OverallReportPhase.COMPLETE,
                        "The report is complete.",
                    ),
                ),
            ),
        )
        problems = _json_problems(verified)
        if problems:
            self._gates.unrecord("G20")
            self._gates.fail("G19", "; ".join(problems))
            return self._abort(OverallReportPhase.REPORT_VERIFIED)
        problems = _secret_problems(serialize_report(verified), self._secrets)
        if problems:
            self._gates.fail("G20", "; ".join(problems))
            return self._abort(OverallReportPhase.REPORT_VERIFIED)
        return verified



def build_overall_report(
    *,
    entries: Iterable[object] = (),
    policy: Optional[OverallReportPolicy] = None,
    forbidden_secrets: Iterable[object] = (),
) -> OverallReport:
    """Aggregate supplied T19/T20/T21 evidence into one overall report (pure).

    Args:
        entries: the per-issue lifecycle evidence - any iterable of
            :class:`IssueLifecycleInput`, or of mappings that carry exactly the
            entry contract's fields. ``()`` is valid and yields an ``EMPTY``
            report; a value that is not a collection (``None``, a bare string, a
            mapping, a number) is a fail-closed refusal.
        policy: how far a successful lifecycle must go
            (:class:`OverallReportPolicy`). The default requires ``PUSHED``.
        forbidden_secrets: the literal secret values this project knows about
            (normally ``AnalysisConfig.secrets``). The serialized report is
            scanned for them before it is returned.

    Returns:
        An immutable :class:`OverallReport`. Evidence problems never raise: they
        produce ``status = REVIEW_REQUIRED``, ``is_valid = False`` and the failed
        gates named in ``validation_errors``.

    Raises:
        OverallReportError: ``policy`` is not an :class:`OverallReportPolicy`, or
            ``forbidden_secrets`` is not an iterable of secrets. These are caller
            errors, not evidence problems.
    """
    active = OverallReportPolicy() if policy is None else policy
    if not isinstance(active, OverallReportPolicy):
        raise OverallReportError(
            "policy must be an OverallReportPolicy, not a "
            f"{type(policy).__name__}."
        )
    if isinstance(forbidden_secrets, (str, bytes)) or not isinstance(
        forbidden_secrets, Iterable
    ):
        raise OverallReportError(
            "forbidden_secrets must be an iterable of secret strings, not a "
            f"{type(forbidden_secrets).__name__}."
        )
    try:
        secrets = tuple(forbidden_secrets)
    except Exception:
        raise OverallReportError(
            "forbidden_secrets must be an iterable of secret strings."
        ) from None
    return _Pipeline(
        entries_input=entries, policy=active, secrets=secrets
    ).run()


__all__: Sequence[str] = (
    "COMMIT_STATUS_ORDER",
    "ExpectedEndState",
    "GATE_CATALOGUE",
    "GATE_PHASE",
    "ISSUE_STATUS_ORDER",
    "IssueLifecycleInput",
    "IssueOutcome",
    "IssueOverallSummary",
    "MAX_ISSUE_KEY_LENGTH",
    "OverallGate",
    "OverallReport",
    "OverallReportError",
    "OverallReportPhase",
    "OverallReportPolicy",
    "OverallReportState",
    "OverallStatus",
    "PUSH_STATUS_ORDER",
    "REPORT_VERSION",
    "ReportDecision",
    "ReportStageRecord",
    "SOURCES",
    "build_overall_report",
    "resolve_overall_status",
    "serialize_report",
    "verify_overall_report",
)

