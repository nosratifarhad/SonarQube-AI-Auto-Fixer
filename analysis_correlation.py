"""Correlate the waited-on analysis with the verification snapshot (T17 -> T18).

Why this module exists
----------------------
T17 waits for the exact compute-engine (CE) task that T16 triggered, so T17 knows
*which* analysis finished. T18 then reads the project's open issues through
``/api/issues/search`` - a **project-scoped** read that the T02 client issues with
``componentKeys`` + ``resolved=false`` (+ page size) only. That endpoint has no
parameter that pins the result to one analysis, so an *unattributable* snapshot
would otherwise be indistinguishable from an attributable one and a concurrent
analysis could produce a false ``FIXED``.

Instead of inventing an API selector, this module makes the verification
precondition explicit and enforceable. It answers exactly one question:

    "May the open-issue snapshot read by T18 be attributed to the analysis that
    T17 waited for?"

with three possible answers (:class:`AnalysisCorrelation`):

``CORRELATED``
    Every required precondition held: the analysis reached ``SUCCESS``, its CE
    task id and its ``analysisId`` were both reported, the CE task analysed the
    component whose issues are being read, and the run was executed under the
    documented **single-analysis precondition**. Only then may T19 consider an
    absence of the original issue as evidence of a fix.

``NOT_CORRELATED``
    Correlation was *positively disproved*: the analysis did not succeed, or the
    CE task belongs to a different component than the one being read.

``UNKNOWN``
    Correlation could not be established at all: no completion was supplied, the
    CE task reported no task id / no ``analysisId``, or the deployment is not
    under the single-analysis precondition. ``UNKNOWN`` is never treated as
    success (fail closed): T19 maps it to ``REVIEW_REQUIRED``.

The single-analysis precondition (explicit, not merely documented)
-----------------------------------------------------------------
Because ``/api/issues/search`` cannot be filtered by analysis, the *only* way to
attribute the snapshot in this POC is to guarantee that no other analysis of the
same component can be the current one. That is an environmental guarantee, so it
must be declared explicitly by the caller: ``exclusive_analysis=True`` is the
attestation "this runner owns the analyses of this component for the duration of
the run". The default is ``False``, which fails closed into ``UNKNOWN``.

Non-behaviour (by design)
-------------------------
No HTTP, no subprocess, no filesystem access, no retry, no analysis re-trigger,
no commit and no push. External values (task id, analysis id, component key) are
credential-redacted and length-capped before they are stored or embedded in a
reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

from repository import redact_credentials
from sonar_analysis_waiter import SonarAnalysisCompletion, SonarAnalysisState

#: External CE metadata is untrusted text: keep only a conservative length.
_MAX_TEXT = 200


class AnalysisCorrelation(Enum):
    """Whether the T18 issue snapshot can be attributed to the T17 analysis."""

    __test__ = False

    #: The snapshot provably belongs to the analysis this run waited for.
    CORRELATED = "correlated"
    #: Correlation was positively disproved (wrong component / failed analysis).
    NOT_CORRELATED = "not-correlated"
    #: Correlation could not be established (missing evidence). Never success.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AnalysisCorrelationResult:
    """Typed, secret-free outcome of the analysis-correlation check.

    Attributes:
        correlation: the three-valued outcome (:class:`AnalysisCorrelation`).
        project_key: component key whose issues T18 reads (``None`` when the
            verifier was not configured with one).
        task_id: CE task id waited on by T17, if any.
        analysis_id: SonarQube analysis id reported by that CE task, if any.
        component_key: component the CE task analysed, if the server reported it.
        exclusive_analysis: whether the single-analysis precondition was declared.
        reason: short human-readable summary (never contains a credential).
        reasons: ordered, detailed explanations.
    """

    __test__ = False

    correlation: AnalysisCorrelation
    project_key: Optional[str]
    task_id: Optional[str]
    analysis_id: Optional[str]
    component_key: Optional[str]
    exclusive_analysis: bool
    reason: str
    reasons: Tuple[str, ...] = ()

    @property
    def is_correlated(self) -> bool:
        """True only when correlation was positively established."""
        return self.correlation is AnalysisCorrelation.CORRELATED

    @property
    def is_not_correlated(self) -> bool:
        """True when correlation was positively disproved."""
        return self.correlation is AnalysisCorrelation.NOT_CORRELATED

    @property
    def is_unknown(self) -> bool:
        """True when correlation could not be established at all."""
        return self.correlation is AnalysisCorrelation.UNKNOWN

    @property
    def reason_text(self) -> str:
        """All reasons joined into a single readable paragraph."""
        return " ".join(self.reasons) if self.reasons else self.reason

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "correlation": self.correlation.value,
            "is_correlated": self.is_correlated,
            "project_key": self.project_key,
            "task_id": self.task_id,
            "analysis_id": self.analysis_id,
            "component_key": self.component_key,
            "exclusive_analysis": self.exclusive_analysis,
            "reason": self.reason,
        }


def _safe_text(value: object) -> Optional[str]:
    """Return credential-redacted, length-capped text for an external value."""
    if value is None:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = redact_credentials(text).strip()
    if not text:
        return None
    return text[:_MAX_TEXT]


def correlate_analysis(
    completion: Optional[SonarAnalysisCompletion],
    *,
    project_key: Optional[str] = None,
    exclusive_analysis: bool = False,
) -> AnalysisCorrelationResult:
    """Decide whether ``completion`` may be used to attribute the T18 snapshot.

    Pure, deterministic and fail-closed: an input that does not *prove*
    correlation yields ``NOT_CORRELATED`` or ``UNKNOWN``, never ``CORRELATED``.

    Args:
        completion: the T17 result that was waited on (may be ``None``).
        project_key: component key whose open issues T18 reads. It is required
            to reach ``CORRELATED``: without it the analysed component cannot be
            compared with the component being read, so the check fails closed
            into ``UNKNOWN``.
        exclusive_analysis: explicit attestation of the single-analysis
            precondition (see the module docstring). Defaults to ``False`` so an
            undeclared deployment fails closed.

    Returns:
        A typed :class:`AnalysisCorrelationResult`.
    """
    task_id = _safe_text(getattr(completion, "task_id", None))
    analysis_id = _safe_text(getattr(completion, "analysis_id", None))
    component_key = _safe_text(getattr(completion, "component_key", None))
    key = _safe_text(project_key)
    exclusive = bool(exclusive_analysis)

    def outcome(
        correlation: AnalysisCorrelation, reason: str, *further: str
    ) -> AnalysisCorrelationResult:
        return AnalysisCorrelationResult(
            correlation=correlation,
            project_key=key,
            task_id=task_id,
            analysis_id=analysis_id,
            component_key=component_key,
            exclusive_analysis=exclusive,
            reason=reason,
            reasons=(reason,) + tuple(further),
        )

    if completion is None:
        return outcome(
            AnalysisCorrelation.UNKNOWN,
            "No analysis completion was supplied, so the issue snapshot cannot "
            "be attributed to a specific analysis.",
        )

    status = getattr(completion, "status", None)
    if not isinstance(status, SonarAnalysisState) or (
        status is not SonarAnalysisState.SUCCESS
    ):
        return outcome(
            AnalysisCorrelation.NOT_CORRELATED,
            "The analysis did not complete successfully, so there is no "
            "completed analysis to attribute the issue snapshot to.",
        )

    if not task_id:
        return outcome(
            AnalysisCorrelation.UNKNOWN,
            "The completed analysis carries no compute-engine task id, so the "
            "issue snapshot cannot be attributed to it.",
        )

    if not analysis_id:
        return outcome(
            AnalysisCorrelation.UNKNOWN,
            "The compute-engine task reported no analysisId, so the issue "
            "snapshot cannot be attributed to a specific analysis.",
            "Without an analysisId there is no analysis identity to correlate "
            "the retrieved issues with (the SonarQube issue API cannot filter "
            "by analysis).",
        )

    if key is None:
        return outcome(
            AnalysisCorrelation.UNKNOWN,
            "The component whose issues are read was not configured, so the "
            "analysed component cannot be compared with it and the snapshot "
            "cannot be attributed to this analysis.",
        )

    if component_key is not None and component_key != key:
        return outcome(
            AnalysisCorrelation.NOT_CORRELATED,
            f"The completed analysis belongs to component '{component_key}', "
            f"but the issues were read for component '{key}', so the snapshot "
            "is not attributable to it.",
        )

    if not exclusive:
        return outcome(
            AnalysisCorrelation.UNKNOWN,
            "The SonarQube issue API cannot be filtered by analysis, and the "
            "single-analysis precondition for this component was not declared, "
            "so the issue snapshot cannot be attributed to this analysis.",
            "Declare the single-analysis precondition (exclusive_analysis=True) "
            "only when nothing else analyses this component during the run, or "
            "use an API capability that filters issues by analysis.",
        )

    return outcome(
        AnalysisCorrelation.CORRELATED,
        f"The issue snapshot is attributed to analysis '{analysis_id}' "
        f"(compute-engine task '{task_id}') under the declared single-analysis "
        "precondition.",
    )


__all__: Sequence[str] = (
    "AnalysisCorrelation",
    "AnalysisCorrelationResult",
    "correlate_analysis",
)
