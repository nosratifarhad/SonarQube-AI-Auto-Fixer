"""Policy-bound logging for the orchestration layer (T30 + T29).

The orchestration layer never writes log lines directly. It builds a
:class:`logging_policy.LogEvent`, asks :func:`logging_policy.evaluate_log_event`
whether the record may be emitted (and in what safe, redacted form) and only
then hands the *validated* message and fields to a sink.

That keeps the T29 guarantees true at the only place where the pipeline could
otherwise leak an untrusted value: a field or message that is not approved,
bounded and credential-free is dropped, and text is redacted before it is
published. The verdict is retained on a :class:`PipelineLog` record so callers
can inspect what was and was not emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from logging_policy import (
    LogEvent,
    LoggingPolicy,
    LogLevel,
    evaluate_log_event,
)

#: Event codes the pipeline emits (all upper-case identities).
EVENT_RUN_START = "FIXER_RUN_START"
EVENT_ISSUE_SKIPPED = "FIXER_ISSUE_SKIPPED"
EVENT_ISSUE_ATTEMPTED = "FIXER_ISSUE_ATTEMPTED"
EVENT_ISSUE_STATUS = "FIXER_ISSUE_STATUS"
EVENT_COMMIT_RESULT = "FIXER_COMMIT_RESULT"
EVENT_PUSH_RESULT = "FIXER_PUSH_RESULT"
EVENT_RUN_SUMMARY = "FIXER_RUN_SUMMARY"

#: Field names the default policy accepts (all lower-case identities).
FIELD_ISSUE_KEY = "issue_key"
FIELD_STAGE = "stage"
FIELD_STATUS = "status"
FIELD_RULE = "rule"
FIELD_DETAIL = "detail"

DEFAULT_EVENT_CODES: Tuple[str, ...] = (
    EVENT_RUN_START,
    EVENT_ISSUE_SKIPPED,
    EVENT_ISSUE_ATTEMPTED,
    EVENT_ISSUE_STATUS,
    EVENT_COMMIT_RESULT,
    EVENT_PUSH_RESULT,
    EVENT_RUN_SUMMARY,
)

DEFAULT_FIELD_NAMES: Tuple[str, ...] = (
    FIELD_ISSUE_KEY,
    FIELD_STAGE,
    FIELD_STATUS,
    FIELD_RULE,
    FIELD_DETAIL,
)


def build_default_logging_policy(
    *,
    minimum_level: LogLevel = LogLevel.INFO,
    extra_event_codes: Sequence[str] = (),
    extra_field_names: Sequence[str] = (),
) -> LoggingPolicy:
    """Build the pipeline's default :class:`LoggingPolicy`.

    The default approves exactly the pipeline's own event codes and six
    structured field names, so no untrusted value reaches a sink unless the
    operator widens the policy deliberately.
    """
    codes = tuple(DEFAULT_EVENT_CODES) + tuple(extra_event_codes)
    names = tuple(DEFAULT_FIELD_NAMES) + tuple(extra_field_names)
    return LoggingPolicy(
        allowed_event_codes=codes,
        allowed_field_names=names,
        minimum_level=minimum_level,
    )


@dataclass(frozen=True)
class PipelineLog:
    """One decided log record (emitted or dropped)."""

    event_code: str
    emitted: bool
    status: str
    level: Optional[str]
    message: Optional[str]
    fields: Tuple[Tuple[str, object], ...]

    def as_dict(self) -> dict:
        return {
            "event_code": self.event_code,
            "emitted": self.emitted,
            "status": self.status,
            "level": self.level,
            "message": self.message,
            "fields": [[name, value] for name, value in self.fields],
        }


#: A sink receives the *validated* text and fields (already redacted/bounded).
LogSink = Callable[[str, Optional[str], Tuple[Tuple[str, object], ...]], None]


def _collect(message: str, level: Optional[str], fields: object) -> None:
    """Default no-op sink: the verdict is still retained on the run result."""


class PolicyLogger:
    """Evaluate log records through T29 and hand approved ones to a sink.

    Args:
        policy: the :class:`LoggingPolicy` to apply. Defaults to
            :func:`build_default_logging_policy`.
        sink: called as ``sink(message, level, fields)`` for every emitted
            record. Defaults to a no-op; :class:`~pipeline.run.FixPipeline`
            uses the collected :class:`PipelineLog` list instead.
    """

    def __init__(
        self,
        policy: Optional[LoggingPolicy] = None,
        sink: Optional[LogSink] = None,
    ) -> None:
        self._policy = policy or build_default_logging_policy()
        self._sink: LogSink = sink if sink is not None else _collect
        self._records: List[PipelineLog] = []

    @property
    def policy(self) -> LoggingPolicy:
        return self._policy

    @property
    def records(self) -> Tuple[PipelineLog, ...]:
        return tuple(self._records)

    def emit(
        self,
        event_code: str,
        message: str,
        *,
        level: LogLevel = LogLevel.INFO,
        fields: Sequence[Tuple[str, object]] = (),
    ) -> PipelineLog:
        """Evaluate and (when approved) publish one log record."""
        evaluation = evaluate_log_event(
            policy=self._policy,
            event=LogEvent(
                event_code=event_code,
                level=level,
                message=message,
                fields=tuple(fields),
            ),
        )
        emitted = evaluation.can_emit
        record = PipelineLog(
            event_code=event_code,
            emitted=emitted,
            status=evaluation.status.value,
            level=evaluation.level.value if evaluation.level is not None else None,
            message=evaluation.message,
            fields=tuple(evaluation.fields),
        )
        self._records.append(record)
        if emitted:
            self._sink(evaluation.message or "", record.level, record.fields)
        return record


__all__ = (
    "DEFAULT_EVENT_CODES",
    "DEFAULT_FIELD_NAMES",
    "EVENT_COMMIT_RESULT",
    "EVENT_ISSUE_ATTEMPTED",
    "EVENT_ISSUE_SKIPPED",
    "EVENT_ISSUE_STATUS",
    "EVENT_PUSH_RESULT",
    "EVENT_RUN_START",
    "EVENT_RUN_SUMMARY",
    "FIELD_DETAIL",
    "FIELD_ISSUE_KEY",
    "FIELD_RULE",
    "FIELD_STAGE",
    "FIELD_STATUS",
    "PipelineLog",
    "PolicyLogger",
    "build_default_logging_policy",
)
