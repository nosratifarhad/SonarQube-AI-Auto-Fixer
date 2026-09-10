"""Wait for the SonarQube analysis triggered by T16 to finish (T17).

:class:`SonarAnalysisWaiter` polls the SonarQube **compute-engine task** whose
id was returned by T16 until that task reaches a terminal state. It answers one
question only: *"did the analysis for this run complete?"* - it never looks at
issues (T18) and never decides whether the original issue is fixed (T19).

Task identity (why polling the CE task and not "is the project green?")
----------------------------------------------------------------------
Several analyses for the same project can be queued or running at the same
time, so "the project looks green" says nothing about *this* analysis. The wait
therefore polls ``/api/ce/task?id=<task id>`` for the task id produced by T16.

When no task id is available (T16 could not extract one), the waiter refuses to
guess: it optionally consults an injected ``task_id_resolver``, and otherwise
returns ``UNKNOWN`` immediately. It deliberately does **not** fall back to
"latest analysis for the project" or a greenness check.

Polling contract
----------------
* Configurable ``timeout_seconds`` and ``poll_interval_seconds`` (both must be
  positive).
* The clock and the sleeper are injectable, so tests never wait in real time.
* The loop is bounded twice - by the deadline and by a maximum poll count - so
  it can never spin forever, even with a clock that never advances.
* A bounded number of consecutive API/network errors is tolerated (the CE API
  can be briefly unavailable); after that the wait stops as ``UNKNOWN``.
* Malformed/unexpected CE responses stop the wait as ``UNKNOWN`` instead of
  looping.

States
------
``SUCCESS`` (terminal, the analysis completed), ``FAILED`` and ``CANCELED``
(terminal, the analysis did not complete successfully), ``TIMEOUT`` (no
terminal state inside the wait budget) and ``UNKNOWN`` (no task id, task not
found, CE API unusable, or an uninterpretable response).

Non-behaviour (by design)
-------------------------
No retry of the analysis, no re-triggering, no issue resolution, no commits,
pushes, branch changes, or source edits.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from repository import redact_credentials
from sonar_analysis import is_safe_task_id
from sonar_client import SonarQubeError, SonarQubeNotFoundError

#: CE task statuses that mean "keep polling" (the analysis is still queued or
#: running).
NON_TERMINAL_TASK_STATUSES = frozenset({"PENDING", "IN_PROGRESS"})

#: Maximum number of characters kept from an untrusted CE error message.
_MAX_ERROR_TEXT = 500


class AnalysisWaitError(Exception):
    """Raised when the wait cannot be performed (caller error)."""


class SonarAnalysisState(Enum):
    """Terminal (or final) state of a waited-on analysis."""

    __test__ = False

    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SonarAnalysisCompletion:
    """Typed record of waiting for one SonarQube analysis to finish.

    Attributes:
        status: final state (:class:`SonarAnalysisState`).
        task_id: compute-engine task id that was waited on (``None`` when no
            usable id was available).
        elapsed_seconds: wall-clock time spent waiting, as measured by the
            injected clock.
        poll_count: number of status polls performed.
        reason: short human-readable summary of the outcome.
        failure_reason: why the analysis must not be treated as successful, or
            ``None`` when the analysis completed successfully.
        analysis_id: SonarQube analysis id reported by the CE task, if any.
        component_key: project/component key reported by the CE task, if any.
    """

    # Not a pytest test class.
    __test__ = False

    status: SonarAnalysisState
    task_id: Optional[str]
    elapsed_seconds: float
    poll_count: int
    reason: str
    failure_reason: Optional[str] = None
    analysis_id: Optional[str] = None
    component_key: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """True when the analysis reached ``SUCCESS`` (completion, not a fix)."""
        return self.status is SonarAnalysisState.SUCCESS

    @property
    def terminal(self) -> bool:
        """True when the wait ended in a definitive terminal state."""
        return self.status in (
            SonarAnalysisState.SUCCESS,
            SonarAnalysisState.FAILED,
            SonarAnalysisState.CANCELED,
        )

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging."""
        return {
            "status": self.status.value,
            "task_id": self.task_id,
            "elapsed_seconds": self.elapsed_seconds,
            "poll_count": self.poll_count,
            "analysis_id": self.analysis_id,
            "component_key": self.component_key,
            "succeeded": self.succeeded,
            "reason": self.reason,
            "failure_reason": self.failure_reason,
        }



def _safe_text(value: object, limit: int = _MAX_ERROR_TEXT) -> Optional[str]:
    """Return credential-redacted, length-capped text for external values."""
    if value is None:
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = redact_credentials(text).strip()
    if not text:
        return None
    return text[:limit]


class SonarAnalysisWaiter:
    """Wait for one SonarQube compute-engine task to reach a terminal state.

    Args:
        client: object exposing ``get_ce_task(task_id)`` (normally the T02
            :class:`sonar_client.SonarClient`). Only this call is used, so a
            fake client is enough in tests - no real SonarQube is required.
        timeout_seconds: overall wait budget; must be positive.
        poll_interval_seconds: delay between polls; must be positive.
        clock: injectable monotonic clock returning seconds.
        sleep: injectable sleeper ``sleep(seconds) -> None``.
        max_polls: hard upper bound on polls; defaults to the deadline budget
            plus one, so the loop is bounded even with a frozen clock.
        max_consecutive_api_errors: consecutive CE API/network errors tolerated
            before the wait stops as ``UNKNOWN``.
    """

    def __init__(
        self,
        client: Any,
        *,
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_polls: Optional[int] = None,
        max_consecutive_api_errors: int = 3,
    ) -> None:
        if client is None or not callable(getattr(client, "get_ce_task", None)):
            raise AnalysisWaitError(
                "A SonarQube client exposing get_ce_task(task_id) is required."
            )
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise AnalysisWaitError(
                f"Wait timeout must be positive (got {timeout_seconds!r})."
            )
        if (
            not isinstance(poll_interval_seconds, (int, float))
            or poll_interval_seconds <= 0
        ):
            raise AnalysisWaitError(
                f"Poll interval must be positive (got {poll_interval_seconds!r})."
            )
        if (
            not isinstance(max_consecutive_api_errors, int)
            or max_consecutive_api_errors < 1
        ):
            raise AnalysisWaitError(
                "max_consecutive_api_errors must be a positive integer "
                f"(got {max_consecutive_api_errors!r})."
            )
        if max_polls is not None and (
            not isinstance(max_polls, int) or max_polls < 1
        ):
            raise AnalysisWaitError(
                f"max_polls must be a positive integer (got {max_polls!r})."
            )

        self._client = client
        self._timeout_seconds = float(timeout_seconds)
        self._interval = float(poll_interval_seconds)
        self._clock = clock
        self._sleep = sleep
        self._max_polls = (
            max_polls
            if max_polls is not None
            else max(1, int(math.ceil(self._timeout_seconds / self._interval)) + 1)
        )
        self._max_consecutive_api_errors = max_consecutive_api_errors

    def wait(
        self,
        task_id: Optional[str],
        *,
        task_id_resolver: Optional[Callable[[], Optional[str]]] = None,
    ) -> SonarAnalysisCompletion:
        """Poll until the analysis task reaches a terminal state.

        Args:
            task_id: compute-engine task id from T16 (may be ``None``).
            task_id_resolver: optional callable used only when ``task_id`` is
                unusable; it must return the id of the analysis started by this
                run. It is never used to ask "is the project green?".

        Returns:
            A typed :class:`SonarAnalysisCompletion`. Failure, timeout, missing
            identity and malformed responses are all represented in the result;
            only caller errors (bad construction/arguments) raise.

        Raises:
            AnalysisWaitError: the supplied ``task_id`` or resolver result is
                not a safe task id, or the resolver itself failed.
        """
        started = self._clock()
        resolved = self._resolve_task_id(task_id, task_id_resolver)
        if resolved is None:
            return self._completion(
                status=SonarAnalysisState.UNKNOWN,
                task_id=None,
                started=started,
                poll_count=0,
                reason=(
                    "No compute-engine task id is available, so the analysis "
                    "triggered by this run cannot be identified or waited on."
                ),
                failure_reason=(
                    "The triggered analysis cannot be attributed to this run "
                    "(no CE task id); analysis completion is unverified."
                ),
            )

        polls = 0
        consecutive_errors = 0
        last_api_error: Optional[str] = None

        while True:
            elapsed = self._clock() - started
            if elapsed >= self._timeout_seconds:
                return self._timeout_completion(resolved, started, polls)
            if polls >= self._max_polls:
                return self._timeout_completion(resolved, started, polls)

            polls += 1
            try:
                payload = self._client.get_ce_task(resolved)
                consecutive_errors = 0
            except SonarQubeNotFoundError as exc:
                return self._completion(
                    status=SonarAnalysisState.UNKNOWN,
                    task_id=resolved,
                    started=started,
                    poll_count=polls,
                    reason=(
                        f"Compute-engine task '{resolved}' was not found, so the "
                        "analysis completion cannot be verified."
                    ),
                    failure_reason=_safe_text(str(exc))
                    or f"Compute-engine task '{resolved}' was not found.",
                )
            except SonarQubeError as exc:
                consecutive_errors += 1
                last_api_error = _safe_text(str(exc))
                if consecutive_errors >= self._max_consecutive_api_errors:
                    return self._completion(
                        status=SonarAnalysisState.UNKNOWN,
                        task_id=resolved,
                        started=started,
                        poll_count=polls,
                        reason=(
                            "The SonarQube compute-engine API could not be "
                            f"queried {consecutive_errors} times in a row; the "
                            "analysis completion is unknown."
                        ),
                        failure_reason=last_api_error,
                    )
                remaining = self._timeout_seconds - (self._clock() - started)
                if remaining <= 0:
                    return self._timeout_completion(resolved, started, polls)
                self._sleep(min(self._interval, remaining))
                continue

            interpreted = self._interpret(payload)
            if interpreted is None:
                return self._completion(
                    status=SonarAnalysisState.UNKNOWN,
                    task_id=resolved,
                    started=started,
                    poll_count=polls,
                    reason=(
                        "The SonarQube compute-engine response could not be "
                        "interpreted; the analysis completion is unknown."
                    ),
                    failure_reason=(
                        "Unexpected or malformed /api/ce/task response."
                    ),
                )
            state, analysis_id, component_key, detail = interpreted
            if state is not None:
                return self._completion(
                    status=state,
                    task_id=resolved,
                    started=started,
                    poll_count=polls,
                    reason=self._terminal_reason(state),
                    failure_reason=self._terminal_failure(state, detail),
                    analysis_id=analysis_id,
                    component_key=component_key,
                )

            remaining = self._timeout_seconds - (self._clock() - started)
            if remaining <= 0:
                return self._timeout_completion(resolved, started, polls)
            self._sleep(min(self._interval, remaining))


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_task_id(
        self,
        task_id: Optional[str],
        resolver: Optional[Callable[[], Optional[str]]],
    ) -> Optional[str]:
        """Return a safe task id from the argument or the injected resolver."""
        if is_safe_task_id(task_id):
            return str(task_id).strip()
        if task_id is not None and str(task_id).strip():
            raise AnalysisWaitError(
                f"Unsafe task id {task_id!r}: expected a short id containing "
                "only letters, digits, '.', '_' or '-'."
            )
        if resolver is None:
            return None
        try:
            candidate = resolver()
        except SonarQubeError as exc:
            raise AnalysisWaitError(
                f"The task-id resolver could not identify the analysis: {exc}"
            ) from exc
        if candidate is None or not str(candidate).strip():
            return None
        if not is_safe_task_id(candidate):
            raise AnalysisWaitError(
                f"The task-id resolver returned an unsafe id {candidate!r}."
            )
        return str(candidate).strip()

    @staticmethod
    def _interpret(payload: object) -> Optional[Tuple[Optional[SonarAnalysisState], Optional[str], Optional[str], Optional[str]]]:
        """Interpret one ``/api/ce/task`` payload.

        Returns:
            ``(state, analysis_id, component_key, detail)`` with ``state`` set to
            ``None`` while the task is still pending/in progress, or ``None``
            when the payload is malformed/unexpected (``state`` would then be
            indistinguishable from "still running", so the whole tuple is
            ``None``).
        """
        if not isinstance(payload, Mapping):
            return None
        task = payload.get("task")
        if not isinstance(task, Mapping):
            return None
        raw_status = task.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            return None
        status = raw_status.strip().upper()
        analysis_id = _safe_text(task.get("analysisId"))
        component_key = _safe_text(task.get("componentKey"))
        detail = _safe_text(task.get("errorMessage")) or _safe_text(task.get("error"))
        if status == "SUCCESS":
            return (SonarAnalysisState.SUCCESS, analysis_id, component_key, detail)
        if status == "FAILED":
            return (SonarAnalysisState.FAILED, analysis_id, component_key, detail)
        if status in ("CANCELED", "CANCELLED"):
            return (SonarAnalysisState.CANCELED, analysis_id, component_key, detail)
        if status in NON_TERMINAL_TASK_STATUSES:
            return (None, analysis_id, component_key, detail)
        return None

    @staticmethod
    def _terminal_reason(state: SonarAnalysisState) -> str:
        if state is SonarAnalysisState.SUCCESS:
            return (
                "SonarQube analysis completed successfully (completion of the "
                "analysis does not mean the original issue is fixed)."
            )
        if state is SonarAnalysisState.FAILED:
            return "SonarQube analysis failed."
        return "SonarQube analysis was canceled."

    @staticmethod
    def _terminal_failure(
        state: SonarAnalysisState, detail: Optional[str]
    ) -> Optional[str]:
        if state is SonarAnalysisState.SUCCESS:
            return None
        if state is SonarAnalysisState.FAILED:
            return detail or "The SonarQube compute-engine task failed."
        return detail or "The SonarQube compute-engine task was canceled."



    def _timeout_completion(
        self, task_id: str, started: float, polls: int
    ) -> SonarAnalysisCompletion:
        """Build the ``TIMEOUT`` completion for an unfinished analysis."""
        return self._completion(
            status=SonarAnalysisState.TIMEOUT,
            task_id=task_id,
            started=started,
            poll_count=polls,
            reason=(
                "SonarQube analysis did not reach a terminal state within "
                f"{self._timeout_seconds:g} seconds."
            ),
            failure_reason=(
                "The analysis is unverified: it was still pending/running when "
                "the wait budget expired, and it is not retried here."
            ),
        )

    def _completion(
        self,
        *,
        status: SonarAnalysisState,
        task_id: Optional[str],
        started: float,
        poll_count: int,
        reason: str,
        failure_reason: Optional[str],
        analysis_id: Optional[str] = None,
        component_key: Optional[str] = None,
    ) -> SonarAnalysisCompletion:
        """Assemble a completion record with the measured elapsed time."""
        return SonarAnalysisCompletion(
            status=status,
            task_id=task_id,
            elapsed_seconds=max(0.0, self._clock() - started),
            poll_count=poll_count,
            reason=reason,
            failure_reason=failure_reason,
            analysis_id=analysis_id,
            component_key=component_key,
        )


__all__: Sequence[str] = (
    "AnalysisWaitError",
    "NON_TERMINAL_TASK_STATUSES",
    "SonarAnalysisCompletion",
    "SonarAnalysisState",
    "SonarAnalysisWaiter",
)
