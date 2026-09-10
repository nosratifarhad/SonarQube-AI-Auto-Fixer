"""Codex result interpretation (T11).

:func:`analyze_codex_result` takes the typed :class:`CodexResult` produced by
T10 and interprets what happened during the Codex run.

Scope boundary
--------------
T11 answers *"did the Codex process run, and does its output signal that the
attempt may have failed or is uncertain?"*. It does **not** decide whether the
SonarQube issue is actually fixed: inspecting the working tree is T12 and
verifying the fix belongs to T16-T19. This module never runs tests, calls
SonarQube, commits, or pushes.

The uncertainty scan is a deterministic, best-effort marker heuristic over
captured stdout/stderr. A marker hit never blocks anything on its own; it only
flags the result as needing review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from codex_executor import CodexExecutionStatus, CodexResult

#: Phrases that suggest the model could not complete the fix with confidence.
#: Matching is case-insensitive substring matching (after lowercasing).
UNCERTAINTY_MARKERS: Tuple[str, ...] = (
    "uncertain",
    "unsure",
    "not sure",
    "not certain",
    "not confident",
    "unable to fix",
    "unable to determine",
    "unable to complete",
    "could not fix",
    "couldn't fix",
    "cannot fix",
    "can't fix",
    "failed to fix",
    "not possible to fix",
    "could not reproduce",
    "need more information",
    "needs more information",
    "needs further investigation",
    "requires further investigation",
    "requires more context",
)


def uncertainty_markers_in(result: CodexResult) -> Tuple[str, ...]:
    """Return the sorted markers found in ``result`` stdout/stderr.

    Only meaningful when the process itself succeeded (exit code 0); timeout
    and launch failures are reported through the status fields instead.
    """
    haystack = f"{result.stdout}\n{result.stderr}".casefold()
    found = [marker for marker in UNCERTAINTY_MARKERS if marker in haystack]
    return tuple(sorted(found))


@dataclass(frozen=True)
class CodexResultAnalysis:
    """Interpretation of one :class:`CodexResult`.

    Attributes:
        result: the underlying typed execution record.
        execution_succeeded: the process ran and exited 0 (regardless of what
            the output text says).
        process_failed: the process ran and exited non-zero.
        timed_out: the process was killed by the timeout.
        executable_not_found: the executable could not be launched.
        exit_code: the process exit code (``None`` if it never completed).
        output_suggests_uncertainty: stdout/stderr contained uncertainty or
            failure markers (only scanned for successful runs).
        matched_markers: the sorted markers that were found.
        needs_review: True when anything above suggests the result should be
            looked at before its changes are trusted.
        reason: short human-readable summary.
    """

    result: CodexResult
    execution_succeeded: bool
    process_failed: bool
    timed_out: bool
    executable_not_found: bool
    exit_code: Optional[int]
    output_suggests_uncertainty: bool
    matched_markers: Tuple[str, ...]
    needs_review: bool
    reason: str

    @property
    def stdout(self) -> str:
        """Captured standard output of the underlying run."""
        return self.result.stdout

    @property
    def stderr(self) -> str:
        """Captured standard error of the underlying run."""
        return self.result.stderr

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging."""
        return {
            "execution_succeeded": self.execution_succeeded,
            "process_failed": self.process_failed,
            "timed_out": self.timed_out,
            "executable_not_found": self.executable_not_found,
            "exit_code": self.exit_code,
            "output_suggests_uncertainty": self.output_suggests_uncertainty,
            "matched_markers": list(self.matched_markers),
            "needs_review": self.needs_review,
            "reason": self.reason,
        }


def _reason_text(
    *,
    timed_out: bool,
    executable_not_found: bool,
    process_failed: bool,
    exit_code: Optional[int],
    markers: Tuple[str, ...],
) -> str:
    if timed_out:
        return "Codex did not finish inside the configured timeout."
    if executable_not_found:
        return "The Codex executable could not be launched."
    if process_failed:
        return f"Codex process failed with exit code {exit_code}."
    if markers:
        return (
            "Codex output suggests uncertainty or an incomplete fix "
            f"(markers: {', '.join(markers)})."
        )
    return "Codex executed successfully with no uncertainty markers detected."


def analyze_codex_result(result: CodexResult) -> CodexResultAnalysis:
    """Interpret one typed :class:`CodexResult`.

    Pure function: no I/O, no side effects, deterministic for a given result.
    """
    status = result.status
    timed_out = status is CodexExecutionStatus.TIMEOUT
    executable_not_found = status is CodexExecutionStatus.NOT_FOUND
    process_failed = status is CodexExecutionStatus.FAILED
    execution_succeeded = status is CodexExecutionStatus.SUCCESS

    markers: Tuple[str, ...] = ()
    if execution_succeeded:
        markers = uncertainty_markers_in(result)
    output_suggests_uncertainty = bool(markers)

    needs_review = not execution_succeeded or output_suggests_uncertainty
    return CodexResultAnalysis(
        result=result,
        execution_succeeded=execution_succeeded,
        process_failed=process_failed,
        timed_out=timed_out,
        executable_not_found=executable_not_found,
        exit_code=result.exit_code,
        output_suggests_uncertainty=output_suggests_uncertainty,
        matched_markers=markers,
        needs_review=needs_review,
        reason=_reason_text(
            timed_out=timed_out,
            executable_not_found=executable_not_found,
            process_failed=process_failed,
            exit_code=result.exit_code,
            markers=markers,
        ),
    )

