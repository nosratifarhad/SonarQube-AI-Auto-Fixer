"""T23 per-issue report - deterministic, fail-closed detail for ONE issue.

Why this module exists
----------------------
T19 decides whether *one* issue is ``FIXED``, T20 commits that fix and T21 pushes
the commit. T22 answers ``what happened overall?`` for many issues; T23 answers
the other question - ``what exactly happened to THIS issue?`` - by reporting the
evidence those stages already produced, without collapsing the stages::

    SonarIssue (T04/T08) + T19 IssueStatusResult -> T20 CommitResult
                         -> T21 PushResult -> T23 PerIssueReport

``FIXED`` is not ``COMMITTED`` and ``COMMITTED`` is not ``PUSHED``: the report
publishes the three source statuses verbatim (``t19_status``, ``t20_status``,
``t21_status``) next to the derived lifecycle outcome, so the answer stays
auditable instead of being flattened into one word.

Purity and side effects
-----------------------
T23 is a **pure reporting layer**:

* it runs no Git command, no SonarQube call, no Codex run, no project test, no
  subprocess, no network I/O and no filesystem access;
* it only *reads* the supplied results, through the same ``as_dict()``
  projection T21/T22 use - never ``asdict()``, never a private attribute;
* it never mutates its inputs, never mutates a repository and never rewrites a
  source status;
* nothing here is wired into ``main.py``: T23 is a library, not a pipeline step.

Trust boundary
--------------
T23 accepts the production result DTOs **or** their projected ``as_dict()``
mappings, and it treats the statuses they carry as *caller-asserted evidence*: it
never re-runs T19/T20/T21 and never authenticates where a mapping came from. What
it proves is the supplied evidence's internal consistency, its lifecycle
consistency and its cross-stage coherence - so T23 is a validation/reporting
boundary, not an evidence-authentication boundary, and a fully self-consistent
fabricated mapping can satisfy it. ``SUCCESS`` still means ``FIXED`` +
``COMMITTED`` + ``PUSHED`` with every gate passing; a layer that needs *trusted*
provenance must establish it outside this pure transform.

Fail-closed rules
-----------------
Nothing is ever inferred, repaired or optimistically re-read:

* an unknown/unrecognised T19/T20/T21 status aborts the report (``G5``/``G7``/
  ``G9``) instead of being mapped onto the nearest status;
* malformed or contradictory evidence aborts the report (``G2``-``G4``,
  ``G6``, ``G8``, ``G10``-``G15``) - a disagreement is a problem, never a choice
  between two readings;
* a derived flag that does not follow from its source status, an outcome that
  does not follow from the evidence, an unserializable payload or a secret
  aborts the report (``G16``-``G21``).

A report that aborts is a *failure report*: ``outcome = REVIEW_REQUIRED``,
``is_valid = False`` and every failed gate named in ``validation_errors``. It
deliberately carries **no** per-stage status and **no** identity description, so
an invalid report can never be mistaken for a partial result; the gate verdicts
and the phase trail explain why it stopped.

Missing evidence
----------------
A stage that produced no result is reported as *not executed* and is never
assumed to have succeeded or to have been refused:

* ``t19_status``/``t20_status``/``t21_status`` are ``None`` and the matching
  ``reasons`` line says the stage supplied no result;
* a ``FIXED`` issue whose commit/push stage produced no result is
  ``NOT_COMPLETED`` (never ``SUCCESS``, never ``FAILED``) and needs attention;
* an *ambiguous* stage (``REVIEW_REQUIRED``, ``COMMIT_UNVERIFIED``,
  ``PUSH_UNVERIFIED``) is ``REVIEW_REQUIRED``.

Secret safety
-------------
The report is built by explicit field projection only: statuses, booleans,
commit ids that matched a full-object-id shape, branch names and gate
identifiers that matched a strict gate-id shape. Free-form upstream text
(reasons, stage trails, gate reasons, remote URLs, captured output) is never
copied. The only *unclassifiable* free-form field T23 publishes is the Sonar
issue ``message``; the other externally supplied identity/status fields it
publishes or validates (``rule``, ``component``, ``severity``, ``issue_type`` and
the issue ``status``) are bounded and validated in exactly the same way, but they
are classified values rather than free-form text. Every published text field is
bounded, single-line and control-character free - and ``G21`` scans the
*serialized* report against the caller's configured secrets before it is
returned, so a secret that reached any field aborts the report instead of
leaking it.

State machine (S0-S10; every phase only runs when every earlier gate passed)
---------------------------------------------------------------------------
=============================== ========= ====================================
Phase                            Gates     Invariant established
=============================== ========= ====================================
S0  INPUT                        -         the input container was received
S1  INPUT_VALIDATED              G1        the input is usable
S2  ISSUE_IDENTITY_VALIDATED     G2, G3    the identity is safe and unambiguous
S3  T19_VALIDATED                G4, G5    the T19 result is usable
S4  T20_VALIDATED                G6, G7    the T20 result is usable (or absent)
S5  T21_VALIDATED                G8, G9    the T21 result is usable (or absent)
S6  CROSS_STAGE_CONSISTENCY_...  G10-G15   the evidence is not contradictory
S7  DERIVED_OUTCOME_RESOLVED     -         the outcome is derived from evidence
S8  REPORT_BUILT                 G16-G19   the report satisfies its invariants
S9  REPORT_VERIFIED              G20, G21  the report is JSON-safe and clean
S10 COMPLETE                     -         the report is returned
=============================== ========= ====================================

A phase failure returns immediately: a later phase is never entered and the gates
it owns stay ``NOT_REACHED``. As in T20/T21/T22 the gate numbers are authored
order; ``GATE_PHASE`` is the executable record of the execution order.

Lifecycle outcome semantics
---------------------------
=================== ==========================================================
Outcome             Meaning
=================== ==========================================================
``SUCCESS``         ``FIXED`` + ``COMMITTED`` + ``PUSHED``, every gate passed
``FAILED``          T19 definitively did not verify a fix (a known, non-ambiguous
                    failure such as ``CODEX_FAILED`` or ``STILL_OPEN``)
``NOT_COMPLETED``   the fix was verified but the lifecycle did not reach the end:
                    a later stage produced no result (never assumed) or did not
                    succeed (refused/failed)
``REVIEW_REQUIRED`` the evidence is ambiguous - or the whole report is invalid
=================== ==========================================================

T23 is a library: it is **not** wired into ``main.py`` and it depends on neither
T22 nor T24+.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from analysis_correlation import AnalysisCorrelation
from commit_message import MAX_TOKEN_LENGTH
from commit_policy import GateStatus
from git_commit import CommitStatus
from git_push import PushStatus
from issue_status import IssueFinalStatus
from models import SonarIssue
from push_policy import build_refspec, is_full_commit_id, normalize_commit_id
from secret_scan import scan_for_secrets
from sonar_analysis_waiter import SonarAnalysisState
from sonar_issue_verification import IssueMatchType

#: Version of the T23 report contract.
REPORT_VERSION = "t23.1"

#: The evidence stages this report is built from, in lifecycle order.
SOURCES: Tuple[str, ...] = ("T19", "T20", "T21")

#: Longest issue key the report accepts. T20 validates its commit-message tokens
#: with the same bound, so a key T20 accepted can always be reported here.
MAX_ISSUE_KEY_LENGTH = MAX_TOKEN_LENGTH

#: A SonarQube issue key, in the shape T20's ``_validated_token`` accepts (no
#: whitespace, no control characters, no quotes), so it can always be rendered as
#: bare JSON text.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: Longest identity/message text the report accepts from the Sonar issue record.
#: The SonarQube issue is untrusted external input, so it stays bounded.
MAX_IDENTITY_TEXT_LENGTH = 400

#: The exact fields a ``SonarIssue`` mapping may carry (anything else is
#: rejected, so a duck-typed blob cannot slip through as identity evidence).
_ISSUE_FIELDS: Tuple[str, ...] = (
    "key",
    "rule",
    "severity",
    "issue_type",
    "message",
    "component",
    "line",
    "status",
)

#: The exact fields a ``PerIssueInput`` mapping may carry.
_ENTRY_FIELDS: Tuple[str, ...] = (
    "issue_key",
    "issue",
    "issue_status",
    "commit_result",
    "push_result",
)

#: Text that may not appear in any identity/message field (control characters).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: A gate identifier shape; the only gate information T23 ever republishes.
_GATE_ID_RE = re.compile(r"^G[1-9][0-9]{0,2}$")

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

#: The auxiliary vocabularies T23 recognises inside the T19 evidence.
MATCH_TYPE_ORDER: Tuple[str, ...] = tuple(match.value for match in IssueMatchType)
CORRELATION_ORDER: Tuple[str, ...] = tuple(
    correlation.value for correlation in AnalysisCorrelation
)
ANALYSIS_STATE_ORDER: Tuple[str, ...] = tuple(
    state.value for state in SonarAnalysisState
)

_T19_FIXED = IssueFinalStatus.FIXED.value
_T19_STILL_OPEN = IssueFinalStatus.STILL_OPEN.value
_T19_TESTS_FAILED = IssueFinalStatus.TESTS_FAILED.value
_T19_SCOPE_INVALID = IssueFinalStatus.SCOPE_INVALID.value
_T19_CODEX_FAILED = IssueFinalStatus.CODEX_FAILED.value
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

_CORRELATED = AnalysisCorrelation.CORRELATED.value
_ANALYSIS_SUCCESS = SonarAnalysisState.SUCCESS.value


class PerIssueReportError(ValueError):
    """A caller error: an argument T23 cannot interpret at all.

    Raised only for programming errors (``forbidden_secrets`` that is a bare
    string or not an iterable), never for evidence problems - those always
    become a fail-closed ``REVIEW_REQUIRED`` report instead.
    """


class PerIssueOutcome(Enum):
    """The derived lifecycle outcome of one issue (T23's one classification)."""

    __test__ = False

    #: ``FIXED`` + ``COMMITTED`` + ``PUSHED``, every gate passed.
    SUCCESS = "success"
    #: T19 definitively did not verify a fix.
    FAILED = "failed"
    #: The fix was verified, but the lifecycle did not reach the end.
    NOT_COMPLETED = "not-completed"
    #: The evidence is ambiguous - or the report itself is invalid.
    REVIEW_REQUIRED = "review-required"


class PerIssueReportPhase(Enum):
    """Explicit T23 state machine (see the module docstring)."""

    __test__ = False

    INPUT = "input"
    INPUT_VALIDATED = "input-validated"
    ISSUE_IDENTITY_VALIDATED = "issue-identity-validated"
    T19_VALIDATED = "t19-validated"
    T20_VALIDATED = "t20-validated"
    T21_VALIDATED = "t21-validated"
    CROSS_STAGE_CONSISTENCY_CHECKED = "cross-stage-consistency-checked"
    DERIVED_OUTCOME_RESOLVED = "derived-outcome-resolved"
    REPORT_BUILT = "report-built"
    REPORT_VERIFIED = "report-verified"
    COMPLETE = "complete"


#: Gate catalogue: ``(gate_id, title)`` in authored order. Every gate is always
#: present in a report; gates whose phase did not run are ``NOT_REACHED``.
GATE_CATALOGUE: Tuple[Tuple[str, str], ...] = (
    ("G1", "input is a usable per-issue input"),
    ("G2", "issue identity valid"),
    ("G3", "issue identity unambiguous"),
    ("G4", "T19 result structurally valid"),
    ("G5", "T19 status recognised"),
    ("G6", "T20 result structurally valid"),
    ("G7", "T20 status recognised"),
    ("G8", "T21 result structurally valid"),
    ("G9", "T21 status recognised"),
    ("G10", "T19 evidence internally consistent"),
    ("G11", "T20 commit evidence internally consistent"),
    ("G12", "T21 push evidence internally consistent"),
    ("G13", "T20/T21 commit identity consistent"),
    ("G14", "branch and remote evidence consistent"),
    ("G15", "lifecycle ordering valid"),
    ("G16", "derived issue_fixed state valid"),
    ("G17", "derived committed state valid"),
    ("G18", "derived pushed state valid"),
    ("G19", "end-to-end outcome valid"),
    ("G20", "serialized report valid"),
    ("G21", "report contains no secret material"),
)

#: Which phase owns each gate. A gate is recorded only while its own phase runs,
#: so a gate whose phase never ran stays ``NOT_REACHED`` in the report.
GATE_PHASE: Mapping[str, PerIssueReportPhase] = {
    "G1": PerIssueReportPhase.INPUT_VALIDATED,
    "G2": PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED,
    "G3": PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED,
    "G4": PerIssueReportPhase.T19_VALIDATED,
    "G5": PerIssueReportPhase.T19_VALIDATED,
    "G6": PerIssueReportPhase.T20_VALIDATED,
    "G7": PerIssueReportPhase.T20_VALIDATED,
    "G8": PerIssueReportPhase.T21_VALIDATED,
    "G9": PerIssueReportPhase.T21_VALIDATED,
    "G10": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G11": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G12": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G13": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G14": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G15": PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED,
    "G16": PerIssueReportPhase.REPORT_BUILT,
    "G17": PerIssueReportPhase.REPORT_BUILT,
    "G18": PerIssueReportPhase.REPORT_BUILT,
    "G19": PerIssueReportPhase.REPORT_BUILT,
    "G20": PerIssueReportPhase.REPORT_VERIFIED,
    "G21": PerIssueReportPhase.REPORT_VERIFIED,
}


@dataclass(frozen=True)
class VerificationEvidence:
    """The T18 verification evidence the T19 result carries (never text).

    Only the fields T23 reads are published, and every one of them is ``None``
    when the T19 result did not supply it: "not reported" is never confused with
    ``False``. Free-form verification text (reasons, error detail) is never read.
    """

    __test__ = False

    issue_key: Optional[str] = None
    retrieval_succeeded: Optional[bool] = None
    is_present: Optional[bool] = None
    identity_reliable: Optional[bool] = None
    page_complete: Optional[bool] = None
    reliable_absence: Optional[bool] = None
    match_type: Optional[str] = None
    correlation: Optional[str] = None

    def as_dict(self) -> dict:
        """Plain, deterministic summary."""
        return {
            "issue_key": self.issue_key,
            "retrieval_succeeded": self.retrieval_succeeded,
            "is_present": self.is_present,
            "identity_reliable": self.identity_reliable,
            "page_complete": self.page_complete,
            "reliable_absence": self.reliable_absence,
            "match_type": self.match_type,
            "correlation": self.correlation,
        }


@dataclass(frozen=True)
class AnalysisEvidence:
    """The T16/T17 analysis evidence the T19 result carries (never text)."""

    __test__ = False

    completion_status: Optional[str] = None
    completion_task_id: Optional[str] = None
    triggered: Optional[bool] = None
    trigger_task_id: Optional[str] = None

    def as_dict(self) -> dict:
        """Plain, deterministic summary."""
        return {
            "completion_status": self.completion_status,
            "completion_task_id": self.completion_task_id,
            "triggered": self.triggered,
            "trigger_task_id": self.trigger_task_id,
        }


@dataclass(frozen=True)
class PerIssueInput:
    """One issue's already-produced evidence (T23's input).

    The T19 result carries the issue key only inside its verification evidence,
    and the T20/T21 results do not carry it at all, so the caller pairs the
    evidence here. T23 never derives a key from a commit message, a branch or a
    path - it only *cross-checks* the published copies against this declaration.

    Attributes:
        issue_key: the SonarQube issue key the evidence belongs to.
        issue: the T04/T08 :class:`models.SonarIssue` record (or a mapping that
            carries exactly its fields) that identifies the issue: rule, file,
            line, severity, type and message.
        issue_status: the T19 ``IssueStatusResult`` object or ``as_dict()`` view,
            or ``None`` when T19 produced no result.
        commit_result: the T20 ``CommitResult`` object or view, or ``None`` when
            T20 produced no result.
        push_result: the T21 ``PushResult`` object or view, or ``None`` when T21
            produced no result.
    """

    __test__ = False

    issue_key: str
    issue: object = None
    issue_status: object = None
    commit_result: object = None
    push_result: object = None

    def as_dict(self) -> dict:
        """The declared identity and which results were supplied."""
        return {
            "issue_key": self.issue_key,
            "has_issue": self.issue is not None,
            "has_issue_status": self.issue_status is not None,
            "has_commit_result": self.commit_result is not None,
            "has_push_result": self.push_result is not None,
        }


@dataclass(frozen=True)
class PerIssueGate:
    """One evaluated T23 gate (same shape as the T20/T21/T22 gate records)."""

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
class PerIssueStageRecord:
    """One line of the T23 state-machine trail (never contains a secret)."""

    __test__ = False

    phase: PerIssueReportPhase
    detail: str

    def as_dict(self) -> dict:
        """Plain summary."""
        return {"phase": self.phase.value, "detail": self.detail}


@dataclass(frozen=True)
class PerIssueReportState:
    """Where the state machine stopped and what every gate decided.

    Attributes:
        phase: the last phase that completed.
        failed_phase: the phase that aborted the report (``None`` when complete).
        gates: all 21 gates, in catalogue order.
        stage_records: the phase trail, in order.
    """

    __test__ = False

    phase: PerIssueReportPhase
    failed_phase: Optional[PerIssueReportPhase]
    gates: Tuple[PerIssueGate, ...]
    stage_records: Tuple[PerIssueStageRecord, ...]

    @property
    def is_complete(self) -> bool:
        """True only when the report reached ``COMPLETE``."""
        return self.phase is PerIssueReportPhase.COMPLETE

    def status_of(self, gate_id: str) -> Optional[GateStatus]:
        """The status of ``gate_id``, or ``None`` when it is unknown."""
        return next(
            (gate.status for gate in self.gates if gate.gate_id == gate_id), None
        )

    def gate(self, gate_id: str) -> Optional[PerIssueGate]:
        """The :class:`PerIssueGate` record for ``gate_id``, or ``None``."""
        return next(
            (gate for gate in self.gates if gate.gate_id == gate_id), None
        )

    @property
    def passed_gates(self) -> Tuple[PerIssueGate, ...]:
        """Every gate that passed."""
        return tuple(gate for gate in self.gates if gate.passed)

    @property
    def failed_gates(self) -> Tuple[PerIssueGate, ...]:
        """Every gate that failed."""
        return tuple(gate for gate in self.gates if gate.failed)

    @property
    def not_reached_gates(self) -> Tuple[PerIssueGate, ...]:
        """Every gate whose phase never ran."""
        return tuple(gate for gate in self.gates if gate.not_reached)

    @property
    def first_failure(self) -> Optional[PerIssueGate]:
        """The first gate that failed, or ``None``."""
        remaining = self.failed_gates
        return remaining[0] if remaining else None

    @property
    def reached_phases(self) -> Tuple[PerIssueReportPhase, ...]:
        """Every phase that completed, in order."""
        return tuple(record.phase for record in self.stage_records)

    def as_dict(self) -> dict:
        """Plain, deterministic summary."""
        return {
            "phase": self.phase.value,
            "failed_phase": (
                self.failed_phase.value if self.failed_phase else None
            ),
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
class PerIssueReport:
    """Immutable, deterministic, secret-free report for ONE SonarQube issue.

    The three source statuses are preserved verbatim and never collapsed into
    the derived outcome, so the answer to "what exactly happened to this issue?"
    stays auditable.

    Attributes:
        outcome: the derived lifecycle outcome (:class:`PerIssueOutcome`).
        is_valid: whether every gate passed (``False`` for a failure report).
        needs_attention: whether a human must look at this issue.
        issue_key: the validated SonarQube issue key.
        rule / file_path / component / line / severity / issue_type / message:
            the issue identity T23 was given (``None`` for a failure report).
        t19_status / t20_status / t21_status: the exact source statuses, or
            ``None`` when that stage produced no result.
        verification / analysis: the T19 evidence that was read.
        commit_sha / previous_head / branch: the T20 commit boundary.
        commit_message_issue_key: the issue key the T20 commit message carries.
        t20_gate_failure / t21_gate_failure: the first T20/T21 gate that failed.
        commit_needs_attention / push_needs_attention: the T20/T21 results' own
            "a human must look" flags.
        expected_commit / remote_before_commit / remote_after_commit /
            remote_branch / remote / refspec: the T21 push boundary.
        issue_fixed / change_committed / change_pushed: the three stages, kept
            separate (``FIXED`` is not ``COMMITTED`` is not ``PUSHED``).
        end_to_end_success: True only for :attr:`PerIssueOutcome.SUCCESS`.
        reasons: T23's own deterministic explanation (never upstream text).
        validation_errors: the failed gates and problems of an invalid report.
        state: the phase trail and all 21 gate verdicts.
        report_version: the version of this report contract.
    """

    __test__ = False

    outcome: PerIssueOutcome
    is_valid: bool
    needs_attention: bool
    issue_key: str
    rule: Optional[str]
    file_path: Optional[str]
    component: Optional[str]
    line: Optional[int]
    severity: Optional[str]
    issue_type: Optional[str]
    message: Optional[str]
    t19_status: Optional[str]
    t20_status: Optional[str]
    t21_status: Optional[str]
    verification: VerificationEvidence
    analysis: AnalysisEvidence
    commit_sha: Optional[str]
    previous_head: Optional[str]
    branch: Optional[str]
    commit_message_issue_key: Optional[str]
    t20_gate_failure: Optional[str]
    commit_needs_attention: Optional[bool]
    expected_commit: Optional[str]
    remote_before_commit: Optional[str]
    remote_after_commit: Optional[str]
    remote_branch: Optional[str]
    remote: Optional[str]
    refspec: Optional[str]
    t21_gate_failure: Optional[str]
    push_needs_attention: Optional[bool]
    issue_fixed: bool
    change_committed: bool
    change_pushed: bool
    end_to_end_success: bool
    reasons: Tuple[str, ...]
    validation_errors: Tuple[str, ...]
    state: PerIssueReportState
    report_version: str = REPORT_VERSION

    @property
    def generated_from(self) -> Tuple[str, ...]:
        """The evidence stages this report was built from."""
        return SOURCES

    @property
    def commit_result_supplied(self) -> bool:
        """True when a T20 result was supplied (``False`` = stage not executed)."""
        return self.t20_status is not None

    @property
    def push_result_supplied(self) -> bool:
        """True when a T21 result was supplied (``False`` = stage not executed)."""
        return self.t21_status is not None

    @property
    def is_fixed(self) -> bool:
        """True only when T19 verified the fix."""
        return self.issue_fixed

    def gate(self, gate_id: str) -> Optional[PerIssueGate]:
        """The gate record for ``gate_id`` in this report's state, or ``None``."""
        return self.state.gate(gate_id)

    def as_dict(self) -> dict:
        """JSON-safe, deterministic, secret-free view of the whole report."""
        return {
            "report_version": self.report_version,
            "outcome": self.outcome.value,
            "is_valid": self.is_valid,
            "needs_attention": self.needs_attention,
            "generated_from": list(self.generated_from),
            "issue": {
                "issue_key": self.issue_key,
                "rule": self.rule,
                "file_path": self.file_path,
                "component": self.component,
                "line": self.line,
                "severity": self.severity,
                "issue_type": self.issue_type,
                "message": self.message,
            },
            "statuses": {
                "t19_status": self.t19_status,
                "t20_status": self.t20_status,
                "t21_status": self.t21_status,
                "commit_result_supplied": self.commit_result_supplied,
                "push_result_supplied": self.push_result_supplied,
            },
            "t19_evidence": {
                "verification": self.verification.as_dict(),
                "analysis": self.analysis.as_dict(),
            },
            "t20_evidence": {
                "commit_sha": self.commit_sha,
                "previous_head": self.previous_head,
                "commit_message_issue_key": self.commit_message_issue_key,
                "gate_failure": self.t20_gate_failure,
                "needs_attention": self.commit_needs_attention,
            },
            "t21_evidence": {
                "expected_commit": self.expected_commit,
                "remote_before_commit": self.remote_before_commit,
                "remote_after_commit": self.remote_after_commit,
                "remote_branch": self.remote_branch,
                "remote": self.remote,
                "refspec": self.refspec,
                "gate_failure": self.t21_gate_failure,
                "needs_attention": self.push_needs_attention,
            },
            "branch": self.branch,
            "derived": {
                "issue_fixed": self.issue_fixed,
                "change_committed": self.change_committed,
                "change_pushed": self.change_pushed,
                "end_to_end_success": self.end_to_end_success,
            },
            "reasons": list(self.reasons),
            "validation_errors": list(self.validation_errors),
            "state": self.state.as_dict(),
        }


# ---------------------------------------------------------------------------
# Reading the evidence (pure, no I/O, never mutates the input)
# ---------------------------------------------------------------------------


def _view(result: object) -> Optional[Mapping]:
    """Project a supplied result into its ``as_dict()`` mapping, or ``None``.

    Mirrors T21/T22 exactly: a mapping is used as-is, an object must expose a
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


def _positive_int(value: object) -> Optional[int]:
    """A positive integer, or ``None`` when absent/not one (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _gate_id(value: object) -> Optional[str]:
    """A gate identifier (``G<number>``), or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    return value if _GATE_ID_RE.match(value) else None


def _present(value: object) -> bool:
    """True when a value is present at all (not ``None`` and not empty text)."""
    return value is not None and value != ""


def _bounded_text(value: object) -> Optional[str]:
    """Untrusted identity text that is safe to publish, or ``None``.

    A value is usable only when it is non-empty text without surrounding
    whitespace, without control characters and within
    :data:`MAX_IDENTITY_TEXT_LENGTH`. Nothing is ever repaired: an over-long or
    multi-line value is not evidence, it is a gate failure.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > MAX_IDENTITY_TEXT_LENGTH or _CONTROL_RE.search(value):
        return None
    return value


@dataclass(frozen=True)
class _Identity:
    """The validated SonarQube issue identity T23 reports (never free-form).

    A failure report carries the blank identity (``key=""`` and every other
    field ``None``) when nothing could be validated.
    """

    key: str
    rule: Optional[str]
    file_path: Optional[str]
    component: Optional[str]
    line: Optional[int]
    severity: Optional[str]
    issue_type: Optional[str]
    message: Optional[str]
    status: Optional[str]


def _identity_projection(
    issue: object,
) -> Tuple[Optional[_Identity], Tuple[str, ...]]:
    """Project the supplied issue record into a validated :class:`_Identity`.

    Accepts the production :class:`models.SonarIssue` (whose ``file_path``
    property strips the SonarQube component prefix) or a mapping that carries
    only the documented SonarQube issue fields. Returns ``(identity, problems)``;
    a value that is neither - or whose fields are not usable identity evidence -
    yields ``None`` and the reasons, never a guess.
    """
    if isinstance(issue, SonarIssue):
        raw: Mapping = {name: getattr(issue, name) for name in _ISSUE_FIELDS}
    elif isinstance(issue, Mapping):
        if any(name not in _ISSUE_FIELDS for name in issue):
            return None, (
                "The issue record carries a field outside the SonarQube issue "
                "contract.",
            )
        raw = issue
    else:
        return None, (
            "The issue record is neither a SonarIssue nor a mapping of the "
            "SonarQube issue fields.",
        )

    problems: List[str] = []
    key = _strict_text(raw.get("key"))
    if key is None:
        problems.append("The issue record does not carry a usable issue key.")
    rule = _bounded_text(raw.get("rule"))
    if rule is None:
        problems.append("The issue record does not carry a usable rule.")
    component = _bounded_text(raw.get("component"))
    if component is None:
        problems.append(
            "The issue record does not carry a usable component (file path)."
        )
    for name in ("severity", "issue_type", "status", "message"):
        value = raw.get(name)
        if value is not None and _bounded_text(value) is None:
            problems.append(
                f"The issue record reports a {name} that is not usable text."
            )
    line = raw.get("line")
    if line is not None and _positive_int(line) is None:
        problems.append(
            "The issue record reports a line that is not a positive integer."
        )
    if problems:
        return None, tuple(problems)

    record = SonarIssue(
        key=key,
        rule=rule,
        severity=_text(raw.get("severity")) or "",
        issue_type=_text(raw.get("issue_type")) or "",
        message=_text(raw.get("message")) or "",
        component=component,
        line=line,
        status=_text(raw.get("status")) or "",
    )
    return (
        _Identity(
            key=key,
            rule=rule,
            file_path=record.file_path,
            component=component,
            line=line,
            severity=_text(raw.get("severity")),
            issue_type=_text(raw.get("issue_type")),
            message=_text(raw.get("message")),
            status=_text(raw.get("status")),
        ),
        (),
    )


def _sub_view(value: object) -> Optional[Mapping]:
    """A nested evidence block when it is a mapping, else ``None``."""
    return value if isinstance(value, Mapping) else None


def _bool_problems(
    view: Mapping,
    gate_id: str,
    stage: str,
    names: Sequence[str],
) -> List[Tuple[str, str]]:
    """Problems for present-but-unusable boolean flags (never echoes a value)."""
    problems: List[Tuple[str, str]] = []
    for name in names:
        if _present(view.get(name)) and _as_bool(view.get(name)) is None:
            problems.append(
                (
                    gate_id,
                    f"The {stage} evidence carries a {name} flag that is not "
                    "a boolean.",
                )
            )
    return problems


def _text_problems(
    view: Mapping,
    gate_id: str,
    stage: str,
    names: Sequence[str],
) -> List[Tuple[str, str]]:
    """Problems for present-but-unusable text fields (never echoes a value)."""
    problems: List[Tuple[str, str]] = []
    for name in names:
        if _present(view.get(name)) and _text(view.get(name)) is None:
            problems.append(
                (
                    gate_id,
                    f"The {stage} evidence reports a {name} that is not text.",
                )
            )
    return problems


def _id_problems(
    view: Mapping,
    gate_id: str,
    stage: str,
    names: Sequence[str],
) -> List[Tuple[str, str]]:
    """Problems for present-but-unusable commit ids (never echoes the id)."""
    problems: List[Tuple[str, str]] = []
    for name in names:
        if _present(view.get(name)) and _full_id(view.get(name)) is None:
            problems.append(
                (
                    gate_id,
                    f"The {stage} evidence reports a {name} that is not a "
                    "full commit id.",
                )
            )
    return problems


def _vocabulary_problem(
    value: object,
    gate_id: str,
    stage: str,
    name: str,
    vocabulary: Sequence[str],
) -> Optional[Tuple[str, str]]:
    """A problem when a present value is outside its production vocabulary."""
    if not _present(value):
        return None
    text = _strict_text(value)
    if text is not None and text in vocabulary:
        return None
    return (
        gate_id,
        f"The {stage} evidence reports a {name} T23 does not recognise.",
    )


def _read_verification(
    value: object,
) -> Tuple[VerificationEvidence, List[Tuple[str, str]]]:
    """Read the T19 result's T18 verification evidence (``G4``)."""
    view = _sub_view(value)
    if value is not None and view is None:
        return VerificationEvidence(), [
            ("G4", "The T19 result carries a verification block that is not a mapping.")
        ]
    if view is None:
        return VerificationEvidence(), []
    problems = _bool_problems(
        view,
        "G4",
        "verification",
        (
            "retrieval_succeeded",
            "is_present",
            "identity_reliable",
            "page_complete",
            "reliable_absence",
        ),
    )
    problems.extend(
        _text_problems(view, "G4", "verification", ("original_issue_key",))
    )
    for name, vocabulary in (
        ("match_type", MATCH_TYPE_ORDER),
        ("correlation", CORRELATION_ORDER),
    ):
        problem = _vocabulary_problem(
            view.get(name), "G4", "verification", name, vocabulary
        )
        if problem is not None:
            problems.append(problem)
    return (
        VerificationEvidence(
            issue_key=_text(view.get("original_issue_key")),
            retrieval_succeeded=_as_bool(view.get("retrieval_succeeded")),
            is_present=_as_bool(view.get("is_present")),
            identity_reliable=_as_bool(view.get("identity_reliable")),
            page_complete=_as_bool(view.get("page_complete")),
            reliable_absence=_as_bool(view.get("reliable_absence")),
            match_type=_text(view.get("match_type")),
            correlation=_text(view.get("correlation")),
        ),
        problems,
    )


def _read_analysis(
    value: object,
) -> Tuple[AnalysisEvidence, List[Tuple[str, str]]]:
    """Read the T19 result's T17 analysis-completion evidence (``G4``).

    The T16 trigger evidence is a *separate* top-level field of the T19 result
    (``IssueStatusResult.trigger_evidence``), so it is read by
    :func:`_read_trigger` instead of being looked for inside this block.
    """
    view = _sub_view(value)
    if value is not None and view is None:
        return AnalysisEvidence(), [
            ("G4", "The T19 result carries an analysis block that is not a mapping.")
        ]
    if view is None:
        return AnalysisEvidence(), []
    problems: List[Tuple[str, str]] = []
    problem = _vocabulary_problem(
        view.get("status"), "G4", "analysis", "status", ANALYSIS_STATE_ORDER
    )
    if problem is not None:
        problems.append(problem)
    problems.extend(_text_problems(view, "G4", "analysis", ("task_id",)))
    return (
        AnalysisEvidence(
            completion_status=_text(view.get("status")),
            completion_task_id=_text(view.get("task_id")),
        ),
        problems,
    )


def _read_trigger(
    value: object,
) -> Tuple[Tuple[Optional[bool], Optional[str]], List[Tuple[str, str]]]:
    """Read the T19 result's T16 trigger evidence (``G4``)."""
    view = _sub_view(value)
    if value is not None and view is None:
        return (None, None), [
            (
                "G4",
                "The T19 result carries trigger evidence that is not a mapping.",
            )
        ]
    if view is None:
        return (None, None), []
    problems = _bool_problems(view, "G4", "trigger evidence", ("triggered",))
    problems.extend(
        _text_problems(view, "G4", "trigger evidence", ("task_id",))
    )
    return (
        (_as_bool(view.get("triggered")), _text(view.get("task_id"))),
        problems,
    )


def _read_flags(
    value: object,
    gate_id: str,
    stage: str,
    validated: Sequence[str],
    mapped: Mapping[str, str],
) -> Tuple[Dict[str, object], List[Tuple[str, str]]]:
    """Read a nested T19 evidence block's boolean flags.

    Every ``validated`` field must be a boolean when it is present (structural
    validity); only the fields in ``mapped`` are carried forward. Returns
    ``(values, problems)`` where ``values`` always covers the mapped targets
    (``None`` = not reported, which is never treated as ``False``).
    """
    view = _sub_view(value)
    if value is not None and view is None:
        return {}, [
            (
                gate_id,
                f"The T19 result carries a {stage} block that is not a mapping.",
            )
        ]
    values: Dict[str, object] = {target: None for target in mapped.values()}
    if view is None:
        return values, []
    problems = _bool_problems(view, gate_id, stage, validated)
    for name, target in mapped.items():
        values[target] = _as_bool(view.get(name))
    return values, problems


def _gate_failure(
    view: Mapping, gate_id: str, stage: str
) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """The first failed gate a result publishes, when it is a gate identifier.

    Only the ``G<number>`` identifier is ever copied - never the gate reason,
    which is free-form upstream text.
    """
    if view.get("gates") is None:
        return None, []
    gates = _sub_view(view.get("gates"))
    if gates is None:
        return None, [
            (
                gate_id,
                f"The {stage} result carries a gate evaluation that is not a "
                "mapping.",
            )
        ]
    value = gates.get("first_failure")
    if not _present(value):
        return None, []
    gate = _gate_id(value)
    if gate is None:
        return None, [
            (
                gate_id,
                f"The {stage} result reports a first_failure that is not a "
                "gate identifier.",
            )
        ]
    return gate, []


def _read_t19(
    view: Mapping,
) -> Tuple[Dict[str, object], Tuple[Tuple[str, str], ...]]:
    """Read the T19 result (``G4``/``G5``): status plus the evidence it carries.

    Only the fields T23 publishes are read; the T19 reasons, its decision trail
    and every nested reason/error string stay unread, so no upstream sentence
    can reach the report.
    """
    problems: List[Tuple[str, str]] = []
    status = _strict_text(view.get("status"))
    if status is None:
        problems.append(
            ("G4", "The T19 result does not carry a well-formed status.")
        )
    values: Dict[str, object] = {"t19_status": status or ""}
    for name, target in (
        ("is_fixed", "t19_is_fixed"),
        ("needs_review", "t19_needs_review"),
    ):
        if _present(view.get(name)) and _as_bool(view.get(name)) is None:
            problems.append(
                (
                    "G4",
                    f"The T19 result carries a {name} flag that is not a "
                    "boolean.",
                )
            )
        values[target] = _as_bool(view.get(name))
    verification, verification_problems = _read_verification(
        view.get("verification")
    )
    analysis, analysis_problems = _read_analysis(view.get("analysis"))
    (triggered, trigger_task_id), trigger_problems = _read_trigger(
        view.get("trigger_evidence")
    )
    analysis = replace(
        analysis, triggered=triggered, trigger_task_id=trigger_task_id
    )
    values["verification"] = verification
    values["analysis"] = analysis
    problems.extend(verification_problems)
    problems.extend(analysis_problems)
    problems.extend(trigger_problems)
    for stage, validated, mapped in (
        (
            "scope",
            ("is_valid", "has_changes", "expected_file_modified"),
            {
                "is_valid": "t19_scope_valid",
                "expected_file_modified": "t19_scope_modified",
            },
        ),
        (
            "tests",
            (
                "passed",
                "failed",
                "timed_out",
                "executable_not_found",
                "execution_error",
                "needs_review",
            ),
            {"passed": "t19_tests_passed"},
        ),
        (
            "codex",
            (
                "execution_succeeded",
                "process_failed",
                "timed_out",
                "executable_not_found",
                "output_suggests_uncertainty",
                "needs_review",
            ),
            {"execution_succeeded": "t19_codex_succeeded"},
        ),
    ):
        block, block_problems = _read_flags(
            view.get(stage), "G4", stage, validated, mapped
        )
        values.update(block)
        problems.extend(block_problems)
    if status is not None and status not in ISSUE_STATUS_ORDER:
        problems.append(
            (
                "G5",
                "The T19 result reports a status T23 does not recognise.",
            )
        )
    return values, tuple(problems)


def _read_t20(
    view: Mapping,
) -> Tuple[Dict[str, object], Tuple[Tuple[str, str], ...]]:
    """Read the T20 result (``G6``/``G7``): status plus the commit boundary."""
    problems: List[Tuple[str, str]] = []
    status = _strict_text(view.get("status"))
    if status is None:
        problems.append(
            ("G6", "The T20 result does not carry a well-formed status.")
        )
    values: Dict[str, object] = {"t20_status": status or ""}
    for name, target in (
        ("is_committed", "t20_is_committed"),
        ("needs_attention", "t20_needs_attention"),
    ):
        if _present(view.get(name)) and _as_bool(view.get(name)) is None:
            problems.append(
                (
                    "G6",
                    f"The T20 result carries a {name} flag that is not a "
                    "boolean.",
                )
            )
        values[target] = _as_bool(view.get(name))
    problems.extend(
        _id_problems(
            view, "G6", "T20", ("new_head", "previous_head", "commit_sha")
        )
    )
    values["t20_new_head"] = _full_id(view.get("new_head"))
    values["t20_previous_head"] = _full_id(view.get("previous_head"))
    values["t20_commit_sha"] = _full_id(view.get("commit_sha"))

    message = _sub_view(view.get("commit_message"))
    if view.get("commit_message") is not None and message is None:
        problems.append(
            (
                "G6",
                "The T20 result carries a commit message that is not a "
                "mapping.",
            )
        )
    values["t20_message_issue_key"] = None if message is None else _text(
        message.get("issue_key")
    )
    if message is not None:
        problems.extend(
            _text_problems(message, "G6", "commit-message", ("issue_key",))
        )

    repository = _sub_view(view.get("repository"))
    if view.get("repository") is not None and repository is None:
        problems.append(
            (
                "G6",
                "The T20 result carries a repository block that is not a "
                "mapping.",
            )
        )
    values["t20_branch"] = (
        None if repository is None else _text(repository.get("branch"))
    )
    if repository is not None:
        problems.extend(
            _text_problems(repository, "G6", "repository", ("branch",))
        )

    gate_failure, gate_problems = _gate_failure(view, "G6", "T20")
    values["t20_gate_failure"] = gate_failure
    problems.extend(gate_problems)
    if status is not None and status not in COMMIT_STATUS_ORDER:
        problems.append(
            (
                "G7",
                "The T20 result reports a status T23 does not recognise.",
            )
        )
    return values, tuple(problems)


def _read_t21(
    view: Mapping,
) -> Tuple[Dict[str, object], Tuple[Tuple[str, str], ...]]:
    """Read the T21 result (``G8``/``G9``): status plus the push boundary."""
    problems: List[Tuple[str, str]] = []
    status = _strict_text(view.get("status"))
    if status is None:
        problems.append(
            ("G8", "The T21 result does not carry a well-formed status.")
        )
    values: Dict[str, object] = {"t21_status": status or ""}
    for name, target in (
        ("is_pushed", "t21_is_pushed"),
        ("is_refusal", "t21_is_refusal"),
        ("needs_attention", "t21_needs_attention"),
        ("push_attempted", "t21_push_attempted"),
    ):
        if _present(view.get(name)) and _as_bool(view.get(name)) is None:
            problems.append(
                (
                    "G8",
                    f"The T21 result carries a {name} flag that is not a "
                    "boolean.",
                )
            )
        values[target] = _as_bool(view.get(name))
    problems.extend(
        _id_problems(
            view,
            "G8",
            "T21",
            ("expected_commit", "remote_before_commit", "remote_after_commit"),
        )
    )
    values["t21_expected_commit"] = _full_id(view.get("expected_commit"))
    values["t21_remote_before_commit"] = _full_id(
        view.get("remote_before_commit")
    )
    values["t21_remote_after_commit"] = _full_id(
        view.get("remote_after_commit")
    )
    problems.extend(
        _text_problems(
            view, "G8", "T21", ("branch", "remote_branch", "refspec")
        )
    )
    values["t21_branch"] = _text(view.get("branch"))
    values["t21_remote_branch"] = _text(view.get("remote_branch"))
    values["t21_refspec"] = _text(view.get("refspec"))

    remote = _sub_view(view.get("remote"))
    if view.get("remote") is not None and remote is None:
        problems.append(
            (
                "G8",
                "The T21 result carries a remote block that is not a mapping.",
            )
        )
    values["t21_remote_name"] = None if remote is None else _text(
        remote.get("name")
    )
    values["t21_remote_exists"] = (
        None if remote is None else _as_bool(remote.get("exists"))
    )
    if remote is not None:
        problems.extend(
            _text_problems(remote, "G8", "remote", ("name",))
        )
        problems.extend(
            _bool_problems(remote, "G8", "remote", ("exists",))
        )

    gate_failure, gate_problems = _gate_failure(view, "G8", "T21")
    values["t21_gate_failure"] = gate_failure
    problems.extend(gate_problems)
    if status is not None and status not in PUSH_STATUS_ORDER:
        problems.append(
            (
                "G9",
                "The T21 result reports a status T23 does not recognise.",
            )
        )
    return values, tuple(problems)


@dataclass(frozen=True)
class _Facts:
    """The exact evidence T23 reads out of one input (never free-form text).

    ``t19_status``, its evidence and the two later results are all the report is
    built from; every optional field is ``None`` when the corresponding result
    did not supply it, so "reported as false" and "not reported" stay distinct.
    """

    identity: _Identity
    t19_status: str
    verification: VerificationEvidence = VerificationEvidence()
    analysis: AnalysisEvidence = AnalysisEvidence()
    t19_is_fixed: Optional[bool] = None
    t19_needs_review: Optional[bool] = None
    t19_scope_valid: Optional[bool] = None
    t19_scope_modified: Optional[bool] = None
    t19_tests_passed: Optional[bool] = None
    t19_codex_succeeded: Optional[bool] = None
    t20_status: Optional[str] = None
    t20_is_committed: Optional[bool] = None
    t20_needs_attention: Optional[bool] = None
    t20_new_head: Optional[str] = None
    t20_previous_head: Optional[str] = None
    t20_commit_sha: Optional[str] = None
    t20_branch: Optional[str] = None
    t20_message_issue_key: Optional[str] = None
    t20_gate_failure: Optional[str] = None
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
    t21_remote_name: Optional[str] = None
    t21_remote_exists: Optional[bool] = None
    t21_gate_failure: Optional[str] = None

    @property
    def issue_key(self) -> str:
        """The declared (and cross-checked) SonarQube issue key."""
        return self.identity.key

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
        counts only when the result's own ``push_attempted`` flag says so (T21
        also reports ``PUSH_FAILED`` when the command could not be launched).
        """
        if self.t21_push_attempted is True:
            return True
        return self.t21_status in (_T21_PUSHED, _T21_UNVERIFIED)

    @property
    def commit_sha(self) -> Optional[str]:
        """The commit T20 published as the one it created."""
        return self.t20_commit_sha

    @property
    def head_commit(self) -> Optional[str]:
        """The commit T20 left at HEAD (``None`` when T20 did not commit)."""
        return self.t20_new_head

    @property
    def push_commit(self) -> Optional[str]:
        """The commit T21 was asked to push."""
        return self.t21_expected_commit

    @property
    def t20_needs_attention_flag(self) -> bool:
        """True when T20 itself asked for a human."""
        return self.t20_needs_attention is True

    @property
    def t21_needs_attention_flag(self) -> bool:
        """True when T21 itself asked for a human."""
        return self.t21_needs_attention is True


def _facts(
    identity: _Identity,
    t19: Mapping[str, object],
    t20: Optional[Mapping[str, object]],
    t21: Optional[Mapping[str, object]],
) -> _Facts:
    """Merge the per-stage readings into one :class:`_Facts` bundle."""
    values: Dict[str, object] = dict(t19)
    if t20 is not None:
        values.update(t20)
    if t21 is not None:
        values.update(t21)
    return _Facts(identity=identity, **values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cross-stage consistency (pure): the gates G3 and G10-G15
# ---------------------------------------------------------------------------


def _declared_identity_problems(
    identity: _Identity, declared_key: object
) -> Tuple[str, ...]:
    """The ``G3`` verdict: the declared key and the issue record must agree."""
    if declared_key == identity.key:
        return ()
    return (
        "The input declares an issue key that differs from the issue record it "
        "carries.",
    )


def _t19_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G10`` verdict: the T19 result must agree with itself.

    Every check is conditional on the evidence actually being published: a field
    T19 did not report is never treated as a contradiction. Nothing here repairs
    or re-reads a status - a disagreement is a problem.
    """
    problems: List[Tuple[str, str]] = []
    verification = facts.verification
    if facts.t19_is_fixed is not None and facts.t19_is_fixed != facts.issue_fixed:
        problems.append(
            (
                "G10",
                "The T19 result contradicts itself: is_fixed does not match "
                "its status.",
            )
        )
    if facts.t19_needs_review is not None and facts.t19_needs_review != (
        facts.t19_status == _T19_REVIEW
    ):
        problems.append(
            (
                "G10",
                "The T19 result contradicts itself: needs_review does not "
                "match its status.",
            )
        )
    absence_inputs = (
        verification.retrieval_succeeded,
        verification.is_present,
        verification.identity_reliable,
        verification.page_complete,
        verification.correlation,
    )
    if (
        verification.reliable_absence is not None
        and all(value is not None for value in absence_inputs)
    ):
        expected_absence = bool(
            verification.retrieval_succeeded
            and not verification.is_present
            and verification.identity_reliable
            and verification.page_complete
            and verification.correlation == _CORRELATED
        )
        if verification.reliable_absence is not expected_absence:
            problems.append(
                (
                    "G10",
                    "The T19 verification evidence contradicts itself: "
                    "reliable_absence does not follow from its own flags.",
                )
            )
    if (
        verification.issue_key is not None
        and verification.issue_key != facts.issue_key
    ):
        problems.append(
            (
                "G10",
                "The T19 verification evidence names a different issue key "
                "than the issue this report is built for.",
            )
        )
    problems.extend(_t19_fix_problems(facts))
    return tuple(problems)


def _t19_fix_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G10`` checks that only apply to a specific T19 status.

    A ``FIXED`` status has documented preconditions in T19, and each terminal
    failure status has one piece of evidence that must not say the opposite. A
    status whose preconditions cannot be expressed as a single necessary
    condition (``REVIEW_REQUIRED``, ``ANALYSIS_FAILED``) is deliberately not
    constrained here: invention is not evidence.
    """
    problems: List[Tuple[str, str]] = []
    verification = facts.verification
    if facts.issue_fixed:
        for label, value in (
            ("codex.execution_succeeded", facts.t19_codex_succeeded),
            ("scope.is_valid", facts.t19_scope_valid),
            ("scope.expected_file_modified", facts.t19_scope_modified),
            ("tests.passed", facts.t19_tests_passed),
            (
                "verification.retrieval_succeeded",
                verification.retrieval_succeeded,
            ),
            ("verification.identity_reliable", verification.identity_reliable),
            ("verification.reliable_absence", verification.reliable_absence),
        ):
            if value is False:
                problems.append(
                    (
                        "G10",
                        "The T19 result reports a verified fix although its "
                        f"{label} evidence is negative.",
                    )
                )
        if verification.is_present is True:
            problems.append(
                (
                    "G10",
                    "The T19 result reports a verified fix although the issue "
                    "is still reported as open.",
                )
            )
        if (
            verification.correlation is not None
            and verification.correlation != _CORRELATED
        ):
            problems.append(
                (
                    "G10",
                    "The T19 result reports a verified fix although the issue "
                    "snapshot is not correlated with the analysis.",
                )
            )
        if (
            facts.analysis.completion_status is not None
            and facts.analysis.completion_status != _ANALYSIS_SUCCESS
        ):
            problems.append(
                (
                    "G10",
                    "The T19 result reports a verified fix although the "
                    "SonarQube analysis did not complete successfully.",
                )
            )
        if facts.analysis.triggered is False:
            problems.append(
                (
                    "G10",
                    "The T19 result reports a verified fix although the "
                    "analysis was not triggered successfully.",
                )
            )
        if (
            facts.analysis.triggered is True
            and not facts.analysis.trigger_task_id
        ):
            problems.append(
                (
                    "G10",
                    "The T19 result reports a verified fix although the "
                    "analysis trigger reported no task id.",
                )
            )
    if (
        facts.t19_status == _T19_STILL_OPEN
        and verification.is_present is False
    ):
        problems.append(
            (
                "G10",
                "The T19 result reports STILL_OPEN although its verification "
                "evidence says the issue is absent.",
            )
        )
    if facts.t19_status == _T19_SCOPE_INVALID and facts.t19_scope_valid is True:
        problems.append(
            (
                "G10",
                "The T19 result reports SCOPE_INVALID although its change-scope "
                "evidence is valid.",
            )
        )
    if facts.t19_status == _T19_TESTS_FAILED and facts.t19_tests_passed is True:
        problems.append(
            (
                "G10",
                "The T19 result reports TESTS_FAILED although its test evidence "
                "passed.",
            )
        )
    if (
        facts.t19_status == _T19_CODEX_FAILED
        and facts.t19_codex_succeeded is True
    ):
        problems.append(
            (
                "G10",
                "The T19 result reports CODEX_FAILED although its Codex "
                "evidence reports success.",
            )
        )
    return tuple(problems)


def _t20_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G11`` verdict: the T20 result must agree with itself."""
    if not facts.has_commit:
        return ()
    problems: List[Tuple[str, str]] = []
    status = facts.t20_status
    if (
        facts.t20_is_committed is not None
        and facts.t20_is_committed != facts.change_committed
    ):
        problems.append(
            (
                "G11",
                "The T20 result contradicts itself: is_committed does not "
                "match its status.",
            )
        )
    if facts.t20_needs_attention is not None and facts.t20_needs_attention != (
        status in _T20_ATTENTION
    ):
        problems.append(
            (
                "G11",
                "The T20 result contradicts itself: needs_attention does not "
                "match its status.",
            )
        )
    if facts.change_committed:
        if facts.t20_new_head is None:
            problems.append(
                (
                    "G11",
                    "The T20 result reports a commit without a usable commit "
                    "id.",
                )
            )
        if facts.t20_commit_sha is None:
            problems.append(
                (
                    "G11",
                    "The T20 result reports a commit without a usable "
                    "commit_sha.",
                )
            )
        if (
            facts.t20_new_head is not None
            and facts.t20_commit_sha is not None
            and facts.t20_commit_sha != facts.t20_new_head
        ):
            problems.append(
                (
                    "G11",
                    "The T20 result reports a commit_sha that differs from its "
                    "new_head.",
                )
            )
        if (
            facts.t20_new_head is not None
            and facts.t20_previous_head == facts.t20_new_head
        ):
            problems.append(
                (
                    "G11",
                    "The T20 result reports a commit that equals its own "
                    "previous head.",
                )
            )
    return tuple(problems)


def _t21_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G12`` verdict: the T21 result must agree with itself.

    A ``PUSHED`` result must name the commit it pushed and the remote tip it
    verified, and when *both* remote tips are supplied the "before" tip must
    differ from the "after" tip: a verified push that did not move the remote is
    a contradiction. An absent ``remote_before_commit`` is never a contradiction
    on its own, because T21 can verify the final tip without the old one.
    """
    if not facts.has_push:
        return ()
    problems: List[Tuple[str, str]] = []
    status = facts.t21_status
    if (
        facts.t21_is_pushed is not None
        and facts.t21_is_pushed != facts.change_pushed
    ):
        problems.append(
            (
                "G12",
                "The T21 result contradicts itself: is_pushed does not match "
                "its status.",
            )
        )
    if facts.t21_is_refusal is not None and facts.t21_is_refusal != (
        status == _T21_REFUSED
    ):
        problems.append(
            (
                "G12",
                "The T21 result contradicts itself: is_refusal does not match "
                "its status.",
            )
        )
    if facts.t21_needs_attention is not None and facts.t21_needs_attention != (
        status in _T21_ATTENTION
    ):
        problems.append(
            (
                "G12",
                "The T21 result contradicts itself: needs_attention does not "
                "match its status.",
            )
        )
    if (
        facts.t21_push_attempted is not None
        and facts.t21_push_attempted != facts.push_attempted
    ):
        problems.append(
            (
                "G12",
                "The T21 result contradicts itself: push_attempted does not "
                "match its status.",
            )
        )
    if facts.change_pushed:
        if facts.t21_expected_commit is None:
            problems.append(
                (
                    "G12",
                    "The T21 result reports a verified push without the commit "
                    "it pushed.",
                )
            )
        if facts.t21_remote_after_commit is None:
            problems.append(
                (
                    "G12",
                    "The T21 result reports a verified push without the remote "
                    "value it verified.",
                )
            )
        elif (
            facts.t21_expected_commit is not None
            and facts.t21_remote_after_commit != facts.t21_expected_commit
        ):
            problems.append(
                (
                    "G12",
                    "The T21 result reports a verified push of a different "
                    "commit than the one it was asked to push.",
                )
            )
        if (
            facts.t21_remote_before_commit is not None
            and facts.t21_remote_after_commit == facts.t21_remote_before_commit
        ):
            problems.append(
                (
                    "G12",
                    "The T21 result reports a verified push whose remote tip "
                    "did not change.",
                )
            )
    return tuple(problems)


def _commit_identity_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G13`` verdict: a push must be provably rooted in the T20 commit.

    This is the gate that refuses the optimistic reading: a ``PUSHED`` result
    whose T20 side did not verify a commit, or that names a different commit, is
    a contradiction and never a success.
    """
    problems: List[Tuple[str, str]] = []
    if facts.push_attempted:
        if not facts.has_commit:
            problems.append(
                (
                    "G13",
                    "T21 push evidence was supplied without the T20 commit "
                    "result it consumes.",
                )
            )
        elif not facts.change_committed:
            problems.append(
                (
                    "G13",
                    "T21 push evidence was supplied although T20 did not verify "
                    "a commit.",
                )
            )
        elif facts.t21_expected_commit is None:
            problems.append(
                (
                    "G13",
                    "T21 push evidence was supplied without the commit it was "
                    "asked to push.",
                )
            )
        elif facts.t21_expected_commit != facts.t20_new_head:
            problems.append(
                (
                    "G13",
                    "T21 pushed a different commit than the one T20 created.",
                )
            )
    if (
        facts.t20_message_issue_key is not None
        and facts.t20_message_issue_key != facts.issue_key
    ):
        problems.append(
            (
                "G13",
                "The T20 commit message names a different issue key than the "
                "issue this report is built for.",
            )
        )
    return tuple(problems)


def _branch_remote_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G14`` verdict: the branch and remote evidence must be coherent."""
    problems: List[Tuple[str, str]] = []
    if (
        facts.t20_branch is not None
        and facts.t21_branch is not None
        and facts.t20_branch != facts.t21_branch
    ):
        problems.append(
            (
                "G14",
                "The T21 result reports a different branch than the T20 commit.",
            )
        )
    if (
        facts.t21_refspec is not None
        and facts.t21_branch is not None
        and facts.t21_remote_branch is not None
    ):
        expected_refspec = build_refspec(
            facts.t21_branch, facts.t21_remote_branch
        )
        if facts.t21_refspec != expected_refspec:
            problems.append(
                (
                    "G14",
                    "The T21 refspec does not match its source and destination "
                    "branches.",
                )
            )
    if facts.push_attempted:
        if facts.t21_refspec is None:
            problems.append(
                (
                    "G14",
                    "A push was attempted without a refspec naming what to "
                    "push.",
                )
            )
        if facts.t21_remote_exists is False:
            problems.append(
                (
                    "G14",
                    "A push was attempted although the destination remote does "
                    "not exist.",
                )
            )
    return tuple(problems)


def _ordering_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """The ``G15`` verdict: the lifecycle order must be respected.

    A later stage can never be reported without the earlier stage it consumes,
    and a commit can never be claimed for an issue T19 did not verify as fixed.
    A refusal or a failed commit *is* coherent with a non-``FIXED`` T19 (T20
    refuses exactly that input), so those stay reportable instead of becoming
    contradictions. A *push* without a verified commit is caught by ``G13``.
    """
    problems: List[Tuple[str, str]] = []
    if facts.has_push and not facts.has_commit:
        problems.append(
            (
                "G15",
                "A T21 push result was supplied without the T20 commit result "
                "it consumes.",
            )
        )
    if (
        facts.t20_status in (_T20_COMMITTED, _T20_UNVERIFIED)
        and not facts.issue_fixed
    ):
        problems.append(
            (
                "G15",
                "T20 claims a commit although T19 did not verify the issue as "
                "fixed.",
            )
        )
    return tuple(problems)


def _cross_stage_problems(facts: _Facts) -> Tuple[Tuple[str, str], ...]:
    """Every ``G10``-``G15`` problem for one issue's evidence.

    Each problem is tagged with its gate; the pipeline never picks between two
    readings, it refuses.
    """
    problems: List[Tuple[str, str]] = []
    problems.extend(_t19_problems(facts))
    problems.extend(_t20_problems(facts))
    problems.extend(_t21_problems(facts))
    problems.extend(_commit_identity_problems(facts))
    problems.extend(_branch_remote_problems(facts))
    problems.extend(_ordering_problems(facts))
    return tuple(problems)


# ---------------------------------------------------------------------------
# Deriving the outcome (pure)
# ---------------------------------------------------------------------------


def resolve_issue_outcome(
    *,
    is_valid: bool,
    t19_status: Optional[str],
    t20_status: Optional[str],
    t21_status: Optional[str],
) -> PerIssueOutcome:
    """Derive the lifecycle outcome from the three source statuses (pure).

    The ladder is deliberately explicit and fail closed:

    1. an invalid report is ``REVIEW_REQUIRED`` - never a partial result;
    2. ambiguity first: a ``REVIEW_REQUIRED`` T19, or an unverified T20 commit or
       T21 push, is never a success and never a failure;
    3. a definite non-``FIXED`` T19 is ``FAILED`` (T19 said no, unambiguously);
    4. a ``FIXED`` issue whose commit or push stage did not produce a verified
       result is ``NOT_COMPLETED`` - missing evidence is never an assumed success
       and never an assumed refusal;
    5. only ``FIXED`` + ``COMMITTED`` + ``PUSHED`` is ``SUCCESS``.
    """
    if not is_valid:
        return PerIssueOutcome.REVIEW_REQUIRED
    if t19_status == _T19_REVIEW:
        return PerIssueOutcome.REVIEW_REQUIRED
    if t20_status == _T20_UNVERIFIED:
        return PerIssueOutcome.REVIEW_REQUIRED
    if t21_status == _T21_UNVERIFIED:
        return PerIssueOutcome.REVIEW_REQUIRED
    if t19_status != _T19_FIXED:
        return PerIssueOutcome.FAILED
    if t20_status != _T20_COMMITTED or t21_status != _T21_PUSHED:
        return PerIssueOutcome.NOT_COMPLETED
    return PerIssueOutcome.SUCCESS


def _classify(facts: _Facts) -> Tuple[PerIssueOutcome, str]:
    """Classify one issue and explain it in T23's own words.

    The outcome comes from :func:`resolve_issue_outcome`; the ladder below only
    picks the matching sentence, and every sentence is built from statuses, so no
    upstream text is ever copied into the report.
    """
    outcome = resolve_issue_outcome(
        is_valid=True,
        t19_status=facts.t19_status,
        t20_status=facts.t20_status,
        t21_status=facts.t21_status,
    )
    if outcome is PerIssueOutcome.REVIEW_REQUIRED:
        if facts.t19_status == _T19_REVIEW:
            reason = "T19 could not decide this issue and asked for a review."
        elif facts.t20_status == _T20_UNVERIFIED:
            reason = (
                "T20 reported a commit whose correctness could not be proven."
            )
        else:
            reason = (
                "T21 reported a push whose effect on the remote could not be "
                "proven."
            )
    elif outcome is PerIssueOutcome.FAILED:
        reason = (
            "T19 reported '" + facts.t19_status + "', which is not a verified "
            "fix."
        )
    elif outcome is PerIssueOutcome.NOT_COMPLETED:
        if not facts.has_commit:
            reason = (
                "T19 verified the fix, but no T20 commit result was supplied, "
                "so the commit stage cannot be established."
            )
        elif not facts.change_committed:
            reason = (
                "T19 verified the fix, but T20 reported '"
                + str(facts.t20_status)
                + "', so no commit was verified."
            )
        elif not facts.has_push:
            reason = (
                "The fix is committed, but no T21 push result was supplied, so "
                "the push stage cannot be established."
            )
        else:
            reason = (
                "The fix is committed, but T21 reported '"
                + str(facts.t21_status)
                + "', so no push was verified."
            )
    else:
        reason = (
            "T19 verified the fix, T20 verified the commit and T21 verified "
            "the push to the recorded commit."
        )
    return outcome, reason


def _needs_attention(facts: _Facts, outcome: PerIssueOutcome) -> bool:
    """True when a human must look at this issue (fail closed).

    Anything ambiguous or incomplete needs a human, as does a result whose own
    T20/T21 evidence asked for one. A definite ``FAILED`` lifecycle does not: T19
    already decided it, unambiguously and without anything left to restore.
    """
    return bool(
        outcome
        in (PerIssueOutcome.REVIEW_REQUIRED, PerIssueOutcome.NOT_COMPLETED)
        or facts.t20_needs_attention_flag
        or facts.t21_needs_attention_flag
    )


def _reasons(
    facts: _Facts, outcome: PerIssueOutcome, reason: str
) -> Tuple[str, ...]:
    """The deterministic explanation of one issue's lifecycle."""
    commit_status = (
        facts.t20_status if facts.has_commit else "not supplied"
    )
    push_status = facts.t21_status if facts.has_push else "not supplied"
    lines = [
        "Reported one SonarQube issue ('"
        + facts.issue_key
        + "') with its T19, T20 and T21 evidence.",
        "T19 status: '" + facts.t19_status + "'; T20 result: '"
        + commit_status + "'; T21 result: '" + push_status + "'.",
        "issue_fixed="
        + str(facts.issue_fixed)
        + ", change_committed="
        + str(facts.change_committed)
        + ", change_pushed="
        + str(facts.change_pushed)
        + ", end_to_end_success="
        + str(outcome is PerIssueOutcome.SUCCESS)
        + ".",
        "The T19/T20/T21 statuses are the stages' own; T23 never rewrites "
        "them.",
        "Outcome: '" + outcome.value + "' - " + reason,
    ]
    if _needs_attention(facts, outcome):
        lines.append(
            "A human must look at this issue before anything else happens."
        )
    return tuple(lines)


def _expected_outcome(report: PerIssueReport) -> PerIssueOutcome:
    """An independent recomputation of the outcome (the ``G19`` check).

    Written separately from :func:`resolve_issue_outcome` on purpose: a bug in
    one is caught by the other instead of being confirmed by it.
    """
    if not report.is_valid:
        return PerIssueOutcome.REVIEW_REQUIRED
    if report.issue_fixed and report.change_committed and report.change_pushed:
        return PerIssueOutcome.SUCCESS
    if report.t19_status == _T19_REVIEW:
        return PerIssueOutcome.REVIEW_REQUIRED
    if report.t20_status == _T20_UNVERIFIED:
        return PerIssueOutcome.REVIEW_REQUIRED
    if report.t21_status == _T21_UNVERIFIED:
        return PerIssueOutcome.REVIEW_REQUIRED
    if report.t19_status == _T19_FIXED:
        return PerIssueOutcome.NOT_COMPLETED
    return PerIssueOutcome.FAILED


def _expected_attention(report: PerIssueReport) -> bool:
    """An independent recomputation of ``needs_attention`` (the ``G19`` check)."""
    return bool(
        report.outcome
        in (PerIssueOutcome.REVIEW_REQUIRED, PerIssueOutcome.NOT_COMPLETED)
        or report.commit_needs_attention is True
        or report.push_needs_attention is True
    )


def _derived_problems(
    report: PerIssueReport,
) -> Tuple[Tuple[str, str], ...]:
    """The ``G16``-``G19`` verdicts for a built report.

    ``G16``/``G17``/``G18`` re-derive the three stage flags from the published
    source statuses, and ``G19`` re-derives the outcome, the attention state and
    the report's own structural invariants. Nothing here trusts the pipeline:
    every value is recomputed from the fields the report publishes.
    """
    problems: List[Tuple[str, str]] = []
    if report.issue_fixed != (report.t19_status == _T19_FIXED):
        problems.append(
            ("G16", "issue_fixed does not follow from the T19 status.")
        )
    if report.change_committed != (report.t20_status == _T20_COMMITTED):
        problems.append(
            ("G17", "change_committed does not follow from the T20 status.")
        )
    if report.change_pushed != (report.t21_status == _T21_PUSHED):
        problems.append(
            ("G18", "change_pushed does not follow from the T21 status.")
        )
    if report.end_to_end_success != (
        report.issue_fixed
        and report.change_committed
        and report.change_pushed
    ):
        problems.append(
            (
                "G19",
                "end_to_end_success does not follow from the three derived "
                "states.",
            )
        )
    if report.outcome is not _expected_outcome(report):
        problems.append(
            ("G19", "The outcome does not follow from the report's evidence.")
        )
    if report.needs_attention != _expected_attention(report):
        problems.append(
            (
                "G19",
                "needs_attention does not follow from the outcome and the "
                "stage flags.",
            )
        )
    if report.is_valid:
        if report.validation_errors:
            problems.append(
                ("G19", "A valid report must not carry validation errors.")
            )
        if report.state.failed_phase is not None:
            problems.append(
                ("G19", "A valid report must not have a failed phase.")
            )
    else:
        if report.outcome is not PerIssueOutcome.REVIEW_REQUIRED:
            problems.append(
                ("G19", "An invalid report must be REVIEW_REQUIRED.")
            )
        if not report.needs_attention:
            problems.append(
                ("G19", "An invalid report must ask for attention.")
            )
    if report.report_version != REPORT_VERSION:
        problems.append(
            ("G19", "The report does not carry the current report version.")
        )
    gate_ids = tuple(gate.gate_id for gate in report.state.gates)
    if gate_ids != tuple(gate_id for gate_id, _ in GATE_CATALOGUE):
        problems.append(
            (
                "G19",
                "The gate records do not cover the complete gate catalogue in "
                "order.",
            )
        )
    return tuple(problems)


def verify_per_issue_report(report: PerIssueReport) -> Tuple[str, ...]:
    """Re-check every derived invariant of a built per-issue report.

    Returns an ordered tuple of problems (empty when the report is internally
    consistent). This is the executable body of ``G16``-``G19`` and the
    self-check T23 runs on its own output, so a derivation bug can never be
    returned as a valid report.
    """
    return tuple(message for _gate, message in _derived_problems(report))


# ---------------------------------------------------------------------------
# Serialization and secret safety (the gates G20 and G21)
# ---------------------------------------------------------------------------


def serialize_report(report: PerIssueReport) -> str:
    """The canonical, deterministic JSON text of a report.

    Sorted keys and no cosmetic whitespace, so the same evidence always produces
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


def _json_problems(report: PerIssueReport) -> Tuple[str, ...]:
    """The ``G20`` verdict: is the payload plain, round-trippable JSON data?"""
    try:
        payload = report.as_dict()
    except Exception:  # defensive: an unrenderable report cannot be published
        return ("The report could not be rendered into a JSON payload.",)
    path = _unserializable(payload)
    if path is not None:
        return (f"The report payload is not JSON data: {path}.",)
    try:
        text = serialize_report(report)
    except Exception:  # pragma: no cover - defensive: a plain payload dumps
        return ("The report payload could not be serialized to JSON.",)
    try:
        round_tripped = json.loads(text)
    except Exception:  # pragma: no cover - defensive: our own dumps parses
        return ("The serialized report could not be parsed back as JSON.",)
    if round_tripped != payload:
        return ("The report payload does not survive a JSON round trip.",)
    return ()


def _secret_problems(text: str, secrets: Sequence[object]) -> Tuple[str, ...]:
    """The ``G21`` verdict: is the serialized report free of secret material?

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


# ---------------------------------------------------------------------------
# The failure report and the gate bookkeeping
# ---------------------------------------------------------------------------


def _failure_report(
    *,
    identity: _Identity,
    phase: PerIssueReportPhase,
    trail: Sequence[PerIssueStageRecord],
    gates: "_GateRun",
    problems: Sequence[str] = (),
) -> PerIssueReport:
    """Build the fail-closed report: ``REVIEW_REQUIRED``, invalid, unclassified.

    The report keeps the gate verdicts it reached and carries *no* per-stage
    status and no derived stage flag, so an invalid report can never be read as a
    partial result. The issue key and its identity are the only facts it keeps -
    they are what the report is *about* - and even they are blank when they could
    not be validated.
    """
    records = gates.records()
    failures = tuple(
        f"{gate.gate_id} {gate.title}: {gate.reason}"
        for gate in records
        if gate.failed
    )
    last = trail[-1].phase if trail else PerIssueReportPhase.INPUT
    return PerIssueReport(
        outcome=PerIssueOutcome.REVIEW_REQUIRED,
        is_valid=False,
        needs_attention=True,
        issue_key=identity.key,
        rule=identity.rule,
        file_path=identity.file_path,
        component=identity.component,
        line=identity.line,
        severity=identity.severity,
        issue_type=identity.issue_type,
        message=identity.message,
        t19_status=None,
        t20_status=None,
        t21_status=None,
        verification=VerificationEvidence(),
        analysis=AnalysisEvidence(),
        commit_sha=None,
        previous_head=None,
        branch=None,
        commit_message_issue_key=None,
        t20_gate_failure=None,
        commit_needs_attention=None,
        expected_commit=None,
        remote_before_commit=None,
        remote_after_commit=None,
        remote_branch=None,
        remote=None,
        refspec=None,
        t21_gate_failure=None,
        push_needs_attention=None,
        issue_fixed=False,
        change_committed=False,
        change_pushed=False,
        end_to_end_success=False,
        reasons=(
            "The report was not produced: the evidence could not be verified.",
            "No outcome was derived, because an unverified report must never "
            "look like a partial result.",
            f"The state machine stopped at the {phase.value} phase.",
        ),
        validation_errors=failures + tuple(problems),
        state=PerIssueReportState(
            phase=last,
            failed_phase=phase,
            gates=records,
            stage_records=tuple(trail),
        ),
    )


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

    def records(self) -> Tuple[PerIssueGate, ...]:
        """All 21 gate records, in catalogue order."""
        records: List[PerIssueGate] = []
        for gate_id, title in GATE_CATALOGUE:
            verdict = self._verdicts.get(gate_id)
            if verdict is None:
                records.append(
                    PerIssueGate(
                        gate_id,
                        title,
                        GateStatus.NOT_REACHED,
                        _NOT_REACHED_REASON,
                    )
                )
            else:
                records.append(
                    PerIssueGate(gate_id, title, verdict[0], verdict[1])
                )
        return tuple(records)


def _tagged(
    problems: Sequence[Tuple[str, str]], gate_id: str
) -> Tuple[str, ...]:
    """The de-duplicated, sorted messages recorded against ``gate_id``."""
    return tuple(
        sorted(
            {
                message
                for problem_gate, message in problems
                if problem_gate == gate_id
            }
        )
    )


def _coerce_entry(item: object) -> Optional[PerIssueInput]:
    """Turn the supplied item into a :class:`PerIssueInput`, or ``None``.

    A ``PerIssueInput`` is used as-is; a mapping is accepted only when every key
    it carries is part of the documented input contract, so a duck-typed blob
    with extra fields cannot slip through.
    """
    if isinstance(item, PerIssueInput):
        return item
    if not isinstance(item, Mapping):
        return None
    try:
        if any(name not in _ENTRY_FIELDS for name in item):
            return None
        return PerIssueInput(
            issue_key=item.get("issue_key"),
            issue=item.get("issue"),
            issue_status=item.get("issue_status"),
            commit_result=item.get("commit_result"),
            push_result=item.get("push_result"),
        )
    except Exception:  # a mapping that raises is not a usable input
        return None


_BLANK_IDENTITY = _Identity(
    key="",
    rule=None,
    file_path=None,
    component=None,
    line=None,
    severity=None,
    issue_type=None,
    message=None,
    status=None,
)


class _Pipeline:
    """One run of the T23 state machine over one issue's evidence.

    Private: the public surface is :func:`build_per_issue_report`. The pipeline
    holds only the phase trail, the gate verdicts, the evidence it read and the
    identity it validated, and it never touches anything outside the input.
    """

    def __init__(
        self, *, entry_input: object, secrets: Tuple[object, ...]
    ) -> None:
        self._input = entry_input
        self._secrets = secrets
        self._trail: List[PerIssueStageRecord] = []
        self._gates = _GateRun()
        self._entry: Optional[PerIssueInput] = None
        self._identity: Optional[_Identity] = None
        self._t19: Dict[str, object] = {}
        self._t20: Optional[Dict[str, object]] = None
        self._t21: Optional[Dict[str, object]] = None
        self._facts: Optional[_Facts] = None

    # -- plumbing ---------------------------------------------------------

    def _record(self, phase: PerIssueReportPhase, detail: str) -> None:
        """Append one phase record to the trail."""
        self._trail.append(PerIssueStageRecord(phase, detail))

    def _failure_for(
        self,
        phase: PerIssueReportPhase,
        identity: Optional[_Identity],
        problems: Sequence[str],
    ) -> PerIssueReport:
        """A failure report carrying ``identity`` (or nothing) and ``problems``."""
        return _failure_report(
            identity=identity if identity is not None else _BLANK_IDENTITY,
            phase=phase,
            trail=self._trail,
            gates=self._gates,
            problems=problems,
        )

    def _abort(
        self, phase: PerIssueReportPhase, problems: Sequence[str] = ()
    ) -> PerIssueReport:
        """Return the fail-closed report for the phase that stopped the run.

        A failure report never runs the serialization gates, so the issue
        identity is only published when it can be proven free of secret
        material; otherwise it is dropped and the reason is recorded. External
        text is never published unscanned, not even on the failure path.
        """
        report = self._failure_for(phase, self._identity, problems)
        if self._identity is None:
            return report
        if not _secret_problems(serialize_report(report), self._secrets):
            return report
        return self._failure_for(
            phase,
            None,
            tuple(problems)
            + (
                "The issue identity was not published: it could not be "
                "verified as free of secret material.",
            ),
        )

    def _require(
        self,
        gate_id: str,
        problems: Sequence[str],
        ok_reason: str,
        phase: PerIssueReportPhase,
    ) -> Optional[PerIssueReport]:
        """Record a gate; return the failure report when it did not pass."""
        if (
            self._gates.record(gate_id, problems, ok_reason=ok_reason)
            is GateStatus.FAIL
        ):
            return self._abort(phase)
        return None

    def _state(
        self,
        *,
        trail: Optional[Sequence[PerIssueStageRecord]] = None,
    ) -> PerIssueReportState:
        """The state record for the current trail and gate verdicts."""
        records = tuple(self._trail if trail is None else trail)
        return PerIssueReportState(
            phase=records[-1].phase if records else PerIssueReportPhase.INPUT,
            failed_phase=None,
            gates=self._gates.records(),
            stage_records=records,
        )

    def run(self) -> PerIssueReport:
        """Advance the state machine; an unexpected error fails closed."""
        try:
            return self._advance()
        except Exception as exc:  # fail closed: never a success, never a raise
            return self._abort_unexpected(exc)

    def _abort_unexpected(self, exc: BaseException) -> PerIssueReport:
        """Convert an unexpected internal error into an invalid report.

        Only the exception's *type* is named: its message and repr could carry
        upstream data, and no gate is marked failed because no gate produced the
        failure. The trail keeps the phases that did complete.
        """
        return self._abort(
            self._trail[-1].phase if self._trail else PerIssueReportPhase.INPUT,
            (
                f"An unexpected {type(exc).__name__} interrupted the report, so "
                "nothing could be verified.",
            ),
        )

    def _advance(self) -> PerIssueReport:
        """Run every phase in order; the first failure ends the report."""
        for step in (
            self._collect,
            self._validate_identity,
            self._read_t19,
            self._read_t20,
            self._read_t21,
            self._cross_check,
        ):
            failure = step()
            if failure is not None:
                return failure
        return self._finish()

    # -- S0/S1: the input --------------------------------------------------

    def _collect(self) -> Optional[PerIssueReport]:
        """S0/S1 - the input must be one usable per-issue entry (``G1``)."""
        phase = PerIssueReportPhase.INPUT_VALIDATED
        entry = _coerce_entry(self._input)
        if entry is None:
            self._record(
                PerIssueReportPhase.INPUT,
                "The input container was received but is not a usable "
                "per-issue input.",
            )
            self._gates.fail(
                "G1",
                "The input is not a PerIssueInput and not a mapping of the "
                "input contract's fields (received a "
                f"{type(self._input).__name__}).",
            )
            return self._abort(phase)
        self._entry = entry
        self._gates.record(
            "G1",
            (),
            ok_reason="The input was received as one per-issue evidence entry.",
        )
        self._record(
            PerIssueReportPhase.INPUT,
            "One per-issue evidence entry was collected from the supplied input.",
        )
        self._record(
            PerIssueReportPhase.INPUT_VALIDATED,
            "G1 passed: the input is a usable per-issue evidence entry.",
        )
        return None

    # -- S2: the issue identity --------------------------------------------

    def _validate_identity(self) -> Optional[PerIssueReport]:
        """S2 - the identity must be valid and unambiguous (``G2``/``G3``)."""
        phase = PerIssueReportPhase.ISSUE_IDENTITY_VALIDATED
        entry = self._entry
        declared = None if entry is None else entry.issue_key
        identity_problems: List[str] = []
        agreement_problems: List[str] = []
        if not isinstance(declared, str) or not declared:
            identity_problems.append(
                "The input does not carry a usable issue key."
            )
        elif declared != declared.strip():
            identity_problems.append(
                "The declared issue key has surrounding whitespace."
            )
        elif len(declared) > MAX_ISSUE_KEY_LENGTH:
            identity_problems.append(
                "The declared issue key is longer than "
                f"{MAX_ISSUE_KEY_LENGTH} characters."
            )
        elif not _ISSUE_KEY_RE.match(declared):
            identity_problems.append(
                "The declared issue key is not a valid SonarQube issue key."
            )
        issue_record = None if entry is None else entry.issue
        if issue_record is None:
            identity_problems.append(
                "The input carries no SonarQube issue record."
            )
        else:
            identity, record_problems = _identity_projection(issue_record)
            self._identity = identity
            identity_problems.extend(record_problems)
            if identity is not None:
                agreement_problems.extend(
                    _declared_identity_problems(identity, declared)
                )
        failure = self._require(
            "G2",
            tuple(identity_problems),
            "The input carries a usable SonarQube issue identity.",
            phase,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G3",
            tuple(agreement_problems),
            "The declared issue key and the issue record agree.",
            phase,
        )
        if failure is not None:
            return failure
        self._record(
            phase,
            "G2, G3 passed: the issue identity is valid and unambiguous.",
        )
        return None

    # -- S3/S4/S5: the three stage results ---------------------------------

    def _read_t19(self) -> Optional[PerIssueReport]:
        """S3 - the T19 result must be usable (``G4``/``G5``)."""
        phase = PerIssueReportPhase.T19_VALIDATED
        entry = self._entry
        if entry.issue_status is None:
            self._gates.fail(
                "G4", "The input carries no T19 issue status result."
            )
            return self._abort(phase)
        view = _view(entry.issue_status)
        if view is None:
            self._gates.fail(
                "G4",
                "The T19 result could not be read as a mapping of fields.",
            )
            return self._abort(phase)
        values, tagged = _read_t19(view)
        self._t19 = values
        failure = self._require(
            "G4",
            _tagged(tagged, "G4"),
            "The T19 result is structurally valid.",
            phase,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G5",
            _tagged(tagged, "G5"),
            "The T19 result reports a recognised status.",
            phase,
        )
        if failure is not None:
            return failure
        self._record(
            phase,
            "G4, G5 passed: the T19 result is usable (status '"
            + str(values["t19_status"])
            + "').",
        )
        return None

    def _read_t20(self) -> Optional[PerIssueReport]:
        """S4 - a supplied T20 result must be usable (``G6``/``G7``).

        No T20 result is not a failure: the commit stage is reported as not
        executed, which is exactly what the evidence says.
        """
        phase = PerIssueReportPhase.T20_VALIDATED
        entry = self._entry
        if entry.commit_result is None:
            self._record(
                phase,
                "No T20 commit result was supplied; the commit stage is "
                "reported as not executed.",
            )
            return None
        view = _view(entry.commit_result)
        if view is None:
            self._gates.fail(
                "G6", "The T20 result could not be read as a mapping of fields."
            )
            return self._abort(phase)
        values, tagged = _read_t20(view)
        self._t20 = values
        failure = self._require(
            "G6",
            _tagged(tagged, "G6"),
            "The T20 result is structurally valid.",
            phase,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G7",
            _tagged(tagged, "G7"),
            "The T20 result reports a recognised status.",
            phase,
        )
        if failure is not None:
            return failure
        self._record(
            phase,
            "G6, G7 passed: the T20 result is usable (status '"
            + str(values["t20_status"])
            + "').",
        )
        return None

    def _read_t21(self) -> Optional[PerIssueReport]:
        """S5 - a supplied T21 result must be usable (``G8``/``G9``).

        Like T20, a missing T21 result is not a failure: the push stage is
        reported as not executed.
        """
        phase = PerIssueReportPhase.T21_VALIDATED
        entry = self._entry
        if entry.push_result is None:
            self._record(
                phase,
                "No T21 push result was supplied; the push stage is reported "
                "as not executed.",
            )
            return None
        view = _view(entry.push_result)
        if view is None:
            self._gates.fail(
                "G8", "The T21 result could not be read as a mapping of fields."
            )
            return self._abort(phase)
        values, tagged = _read_t21(view)
        self._t21 = values
        failure = self._require(
            "G8",
            _tagged(tagged, "G8"),
            "The T21 result is structurally valid.",
            phase,
        )
        if failure is not None:
            return failure
        failure = self._require(
            "G9",
            _tagged(tagged, "G9"),
            "The T21 result reports a recognised status.",
            phase,
        )
        if failure is not None:
            return failure
        self._record(
            phase,
            "G8, G9 passed: the T21 result is usable (status '"
            + str(values["t21_status"])
            + "').",
        )
        return None

    # -- S6: the cross-stage gates -----------------------------------------

    def _cross_check(self) -> Optional[PerIssueReport]:
        """S6 - the evidence must not contradict itself (``G10``-``G15``)."""
        phase = PerIssueReportPhase.CROSS_STAGE_CONSISTENCY_CHECKED
        identity = self._identity
        if identity is None:  # pragma: no cover - defensive: G2 aborts first
            self._gates.fail(
                "G10", "The issue identity was never validated."
            )
            return self._abort(phase)
        facts = _facts(identity, self._t19, self._t20, self._t21)
        self._facts = facts
        tagged = _cross_stage_problems(facts)
        checks = (
            ("G10", "The T19 evidence agrees with itself."),
            ("G11", "The T20 evidence agrees with itself."),
            ("G12", "The T21 evidence agrees with itself."),
            ("G13", "The push is rooted in the verified T20 commit."),
            ("G14", "The branch and remote evidence is coherent."),
            ("G15", "The lifecycle order is respected."),
        )
        for gate_id, ok_reason in checks:
            failure = self._require(
                gate_id, _tagged(tagged, gate_id), ok_reason, phase
            )
            if failure is not None:
                return failure
        self._record(
            phase,
            "G10-G15 passed: the T19, T20 and T21 evidence is internally "
            "consistent.",
        )
        return None

    # -- S7-S10: the outcome, the report and its verification --------------

    def _assemble(self, facts: _Facts) -> PerIssueReport:
        """S7/S8 - derive the outcome and build the candidate report (pure)."""
        outcome, reason = _classify(facts)
        self._record(
            PerIssueReportPhase.DERIVED_OUTCOME_RESOLVED,
            "The lifecycle outcome resolved to '" + outcome.value + "'.",
        )
        branch = (
            facts.t20_branch
            if facts.t20_branch is not None
            else facts.t21_branch
        )
        identity = facts.identity
        return PerIssueReport(
            outcome=outcome,
            is_valid=True,
            needs_attention=_needs_attention(facts, outcome),
            issue_key=identity.key,
            rule=identity.rule,
            file_path=identity.file_path,
            component=identity.component,
            line=identity.line,
            severity=identity.severity,
            issue_type=identity.issue_type,
            message=identity.message,
            t19_status=facts.t19_status,
            t20_status=facts.t20_status,
            t21_status=facts.t21_status,
            verification=facts.verification,
            analysis=facts.analysis,
            commit_sha=facts.t20_commit_sha,
            previous_head=facts.t20_previous_head,
            branch=branch,
            commit_message_issue_key=facts.t20_message_issue_key,
            t20_gate_failure=facts.t20_gate_failure,
            commit_needs_attention=facts.t20_needs_attention,
            expected_commit=facts.t21_expected_commit,
            remote_before_commit=facts.t21_remote_before_commit,
            remote_after_commit=facts.t21_remote_after_commit,
            remote_branch=facts.t21_remote_branch,
            remote=facts.t21_remote_name,
            refspec=facts.t21_refspec,
            t21_gate_failure=facts.t21_gate_failure,
            push_needs_attention=facts.t21_needs_attention,
            issue_fixed=facts.issue_fixed,
            change_committed=facts.change_committed,
            change_pushed=facts.change_pushed,
            end_to_end_success=outcome is PerIssueOutcome.SUCCESS,
            reasons=_reasons(facts, outcome, reason),
            validation_errors=(),
            state=self._state(),
        )

    def _finish(self) -> PerIssueReport:
        """S7-S10 - build, self-verify, then verify the JSON and the secrets."""
        facts = self._facts
        if facts is None:  # pragma: no cover - defensive: G10 aborts first
            self._gates.fail(
                "G16", "No evidence was collected for this issue."
            )
            return self._abort(PerIssueReportPhase.REPORT_BUILT)
        built = self._assemble(facts)
        tagged = _derived_problems(built)
        checks = (
            ("G16", "The issue_fixed state follows from the T19 status."),
            ("G17", "The committed state follows from the T20 status."),
            ("G18", "The pushed state follows from the T21 status."),
            ("G19", "The end-to-end outcome follows from the evidence."),
        )
        for gate_id, ok_reason in checks:
            failure = self._require(
                gate_id,
                _tagged(tagged, gate_id),
                ok_reason,
                PerIssueReportPhase.REPORT_BUILT,
            )
            if failure is not None:
                return failure
        self._record(
            PerIssueReportPhase.REPORT_BUILT,
            "G16-G19 passed: every derived state and the outcome follow from "
            "the evidence.",
        )

        # S9/S10: the report that is verified is the report that is returned, so
        # its final state (including the G20/G21 pass verdicts) is built first.
        # When a gate then fails, its verdict is overwritten and the failure
        # report is returned instead, so the trail never claims a pass that did
        # not happen.
        self._gates.record(
            "G20", (), ok_reason="The serialized report is JSON-safe."
        )
        self._gates.record(
            "G21",
            (),
            ok_reason=(
                "The serialized report matched none of the configured secrets."
            ),
        )
        verified = replace(
            built,
            state=self._state(
                trail=tuple(self._trail)
                + (
                    PerIssueStageRecord(
                        PerIssueReportPhase.REPORT_VERIFIED,
                        "The serialized report was verified against the JSON "
                        "and secret gates.",
                    ),
                    PerIssueStageRecord(
                        PerIssueReportPhase.COMPLETE,
                        "The report is complete.",
                    ),
                )
            ),
        )
        problems = _json_problems(verified)
        if problems:
            self._gates.unrecord("G21")
            self._gates.fail("G20", "; ".join(problems))
            return self._abort(PerIssueReportPhase.REPORT_VERIFIED)
        problems = _secret_problems(serialize_report(verified), self._secrets)
        if problems:
            self._gates.fail("G21", "; ".join(problems))
            return self._abort(PerIssueReportPhase.REPORT_VERIFIED)
        return verified


def build_per_issue_report(
    *,
    entry: object = None,
    forbidden_secrets: Iterable[object] = (),
) -> PerIssueReport:
    """Report exactly what happened to one SonarQube issue (pure).

    Args:
        entry: the issue's already-produced evidence - a :class:`PerIssueInput`,
            or a mapping that carries exactly the input contract's fields
            (``issue_key``, ``issue``, ``issue_status``, ``commit_result``,
            ``push_result``). A value that is neither is a fail-closed refusal.
        forbidden_secrets: the literal secret values this project knows about
            (normally ``AnalysisConfig.secrets``). The serialized report is
            scanned for them before it is returned.

    Returns:
        An immutable :class:`PerIssueReport`. Evidence problems never raise: they
        produce ``outcome = REVIEW_REQUIRED``, ``is_valid = False`` and the
        failed gates named in ``validation_errors``.

    Raises:
        PerIssueReportError: ``forbidden_secrets`` is a bare string or is not an
            iterable. That is a caller error, not an evidence problem.
    """
    if isinstance(forbidden_secrets, (str, bytes)) or not isinstance(
        forbidden_secrets, Iterable
    ):
        raise PerIssueReportError(
            "forbidden_secrets must be an iterable of secret strings, not a "
            f"{type(forbidden_secrets).__name__}."
        )
    try:
        secrets = tuple(forbidden_secrets)
    except Exception:
        raise PerIssueReportError(
            "forbidden_secrets must be an iterable of secret strings."
        ) from None
    return _Pipeline(entry_input=entry, secrets=secrets).run()


__all__: Sequence[str] = (
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
)
