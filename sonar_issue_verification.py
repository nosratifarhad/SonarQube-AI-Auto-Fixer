"""Verify the original SonarQube issue against the new analysis (T18).

:class:`SonarIssueVerifier` answers one question after T17 confirms that the
SonarQube analysis of this run finished successfully: *"what does SonarQube
report about the original issue now?"*.

It never decides the final status (that is T19) and never mutates SonarQube:
issues are not resolved, closed, re-opened, or edited, and no fake state is
invented. Retrieval reuses the existing T02 client and the T04 model, and
reuses the T13 path normalization for comparisons.

Issue identity (the original issue is the reference point)
---------------------------------------------------------
A re-analysis can change keys, so identity uses a documented ladder:

1. **Original issue key** (primary). SonarQube keys are stable while an issue
   stays open; if the key is still in the open-issue set it is the same issue.
   If it is gone, the issue is no longer reported as open.
2. **Rule + normalized component/file + line** (fallback). Used when the
   original issue has no usable key, or when its key disappeared but an
   equivalent issue is now open.
3. **Rule + normalized component/file** (last resort). Used when the line is
   unknown or has moved.

Matching must never be sloppy: an unrelated issue that merely shares a rule or
message is **not** the original issue, and a disappearing key with an equivalent
issue still open is ambiguity, not a fix. In those cases ``identity_reliable``
is ``False`` and T19 must return ``REVIEW_REQUIRED`` instead of ``FIXED``.

Completeness (fail closed)
--------------------------
``is_present is False`` is only trustworthy when the retrieval actually saw the
whole open-issue set. The default page size can truncate a large project, so the
verifier records ``page_complete`` and refuses to call an absence reliable when
the response shows more issues than were returned.

Completeness requires *positive evidence*: ``total`` must be an actual integer
(booleans are rejected), must not be negative, and must not exceed the number of
issue entries that were retrieved. A missing, ``null``, non-numeric, malformed,
negative or truncated ``total`` therefore yields ``page_complete=False`` (an
unknown/incomplete result), which can never establish a reliable absence and can
therefore never lead to ``FIXED``.

Analysis correlation
--------------------
A complete page still says nothing about *which* analysis produced it: T18 reads
``/api/issues/search`` project-scoped, with no way to filter by analysis. The
verifier therefore also records a :class:`analysis_correlation.AnalysisCorrelation`
verdict for the T17 completion it is given (``verify(..., completion=...)``):

* ``CORRELATED`` - the snapshot provably belongs to the analysis T17 waited for;
* ``NOT_CORRELATED`` - correlation was positively disproved;
* ``UNKNOWN`` - correlation could not be established (missing metadata, or the
  single-analysis precondition was not declared).

``UNKNOWN`` and ``NOT_CORRELATED`` are never treated as success: T19 refuses
``FIXED`` unless the correlation is ``CORRELATED``. When no completion is
supplied the verdict is ``UNKNOWN``, so the default behaviour fails closed.

Filtering
---------
The T03 type/severity filters are **off by default** here on purpose: a
still-open issue whose severity changed (for example to ``INFO``) would
otherwise be filtered out and look absent, which could produce a false
``FIXED``. ``apply_issue_filter=True`` opts back into the T03 rules for callers
that want the same subset T03 selects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

from analysis_correlation import (
    AnalysisCorrelation,
    AnalysisCorrelationResult,
    correlate_analysis,
)
from change_scope import normalise_relative_path
from issue_filter import filter_issues, to_sonar_issue
from models import SonarIssue
from repository import redact_credentials
from sonar_client import SonarQubeError

#: Issue keys are external data: accept only a conservative length.
_MAX_KEY_LENGTH = 400


class SonarIssueVerificationError(Exception):
    """Raised when verification cannot be performed (caller error)."""


class IssueMatchType(Enum):
    """How the original issue was matched against the current open issues."""

    __test__ = False

    #: Matched by the original SonarQube issue key (strongest identity).
    KEY = "key"
    #: Matched by rule + normalized file + line (fallback identity).
    COMPONENT_RULE_LINE = "component-rule-line"
    #: Matched by rule + normalized file only (weaker fallback identity).
    COMPONENT_RULE = "component-rule"
    #: Not matched at all.
    NONE = "none"



@dataclass(frozen=True)
class SonarIssueVerificationResult:
    """Typed outcome of comparing the original issue with the new analysis.

    Attributes:
        original_issue: the T08 reference issue.
        original_issue_key: the key used for identity (``""`` when the original
            issue carried no usable key).
        retrieval_succeeded: the open-issue retrieval completed and its payload
            was interpretable.
        current_issues: the normalized open issues considered (after optional
            T03 filtering).
        matches: every current issue that matched the identity strategy.
        matching_current_issue: the single match, or ``None`` when there is no
            match or the match is ambiguous (more than one candidate).
        match_type: which identity tier produced the matches.
        is_present: a matching open issue still exists.
        identity_reliable: the match/absence can be trusted (see module
            docstring); ``False`` forces ``REVIEW_REQUIRED`` in T19.
        page_complete: the retrieval saw the whole open-issue set (positive
            evidence only; see the module docstring).
        retrieved_count: raw issue entries returned by SonarQube.
        skipped_entries: returned entries that were not usable issue objects.
        reason: short human-readable summary.
        reasons: ordered, detailed explanations.
        error: retrieval failure detail (credential-redacted), else ``None``.
        reported_total: the ``total`` SonarQube reported, when it was an
            integer (``None`` otherwise, which is itself an incomplete result).
        correlation: whether the retrieved snapshot could be attributed to the
            analysis waited on by T17 (:class:`AnalysisCorrelation`).
        correlation_reason: human-readable explanation of ``correlation``.
    """

    __test__ = False

    original_issue: SonarIssue
    original_issue_key: str
    retrieval_succeeded: bool
    current_issues: Tuple[SonarIssue, ...]
    matches: Tuple[SonarIssue, ...]
    matching_current_issue: Optional[SonarIssue]
    match_type: IssueMatchType
    is_present: bool
    identity_reliable: bool
    page_complete: bool
    retrieved_count: int
    skipped_entries: int
    reason: str
    reasons: Tuple[str, ...]
    error: Optional[str] = None
    reported_total: Optional[int] = None
    correlation: AnalysisCorrelation = AnalysisCorrelation.UNKNOWN
    correlation_reason: str = ""

    @property
    def absent(self) -> bool:
        """True when no matching open issue exists (not a fix on its own)."""
        return not self.is_present

    @property
    def analysis_correlated(self) -> bool:
        """True only when the snapshot provably belongs to the T17 analysis."""
        return self.correlation is AnalysisCorrelation.CORRELATED

    @property
    def reliable_absence(self) -> bool:
        """True only when an absence may be used as evidence of a fix.

        Requires a successful retrieval, no matching open issue, a reliable
        identity match, a complete page, *and* a correlated analysis.
        """
        return (
            self.retrieval_succeeded
            and self.absent
            and self.identity_reliable
            and self.page_complete
            and self.analysis_correlated
        )

    @property
    def match_keys(self) -> Tuple[str, ...]:
        """Keys of the matching current issues (for reporting)."""
        return tuple(issue.key for issue in self.matches)

    @property
    def reason_text(self) -> str:
        """All reasons joined into a single readable paragraph."""
        return " ".join(self.reasons)

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting.

        Raw issue messages are deliberately omitted (untreated external text).
        """
        return {
            "original_issue_key": self.original_issue_key,
            "retrieval_succeeded": self.retrieval_succeeded,
            "match_type": self.match_type.value,
            "is_present": self.is_present,
            "absent": self.absent,
            "identity_reliable": self.identity_reliable,
            "page_complete": self.page_complete,
            "reliable_absence": self.reliable_absence,
            "retrieved_count": self.retrieved_count,
            "reported_total": self.reported_total,
            "skipped_entries": self.skipped_entries,
            "current_issue_count": len(self.current_issues),
            "match_count": len(self.matches),
            "match_keys": list(self.match_keys),
            "correlation": self.correlation.value,
            "analysis_correlated": self.analysis_correlated,
            "correlation_reason": self.correlation_reason,
            "reason": self.reason,
            "error": self.error,
        }


def _usable_key(raw: object) -> str:
    """Return a usable original issue key, or ``""`` when it is unusable.

    Keys are external data: whitespace, control characters, and absurd lengths
    are rejected rather than compared.
    """
    value = "" if raw is None else str(raw).strip()
    if not value or len(value) > _MAX_KEY_LENGTH:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in value):
        return ""
    return value


def _file_key(raw: object) -> str:
    """Comparable key for a component/file path.

    Reuses the T13 normalization (backslashes to ``/``, ``./`` collapsed) and
    then folds case with :func:`os.path.normcase`, so the comparison matches the
    host platform's path semantics.
    """
    text = "" if raw is None else str(raw)
    normalized = normalise_relative_path(text)
    return os.path.normcase(normalized if normalized else text.strip())


def _same_rule_and_file(original: SonarIssue, candidate: SonarIssue) -> bool:
    """True when both issues share a non-empty rule and the same file."""
    return (
        bool(original.rule)
        and original.rule == candidate.rule
        and _file_key(original.file_path) == _file_key(candidate.file_path)
    )


def _same_line(original: SonarIssue, candidate: SonarIssue) -> bool:
    """True when both issues have a known, identical line."""
    return (
        original.line is not None
        and candidate.line is not None
        and original.line == candidate.line
    )


def _page_is_complete(total: object, retrieved_count: int) -> bool:
    """Decide (fail closed) whether a single page held the whole issue set.

    Completeness needs positive evidence: an integer ``total`` that is not
    negative and does not exceed the number of entries that were retrieved.
    Anything else - a missing, ``null``, string, boolean, negative or malformed
    total, or a page that shows fewer entries than the total - is treated as
    incomplete/unknown, so an absence can never be called reliable from it.
    """
    if isinstance(total, bool) or not isinstance(total, int):
        return False
    if total < 0:
        return False
    if not isinstance(retrieved_count, int) or retrieved_count < 0:
        return False
    return retrieved_count >= total


class SonarIssueVerifier:
    """Compare the original issue with SonarQube's current open issues.

    Args:
        client: object exposing ``get_open_issues()`` (normally the T02
            :class:`sonar_client.SonarClient`). Only that call is used, so a
            fake client suffices in tests - no real SonarQube is required.
        apply_issue_filter: when True, the T03 type/severity rules are applied
            to the retrieved issues before matching. Defaults to False so a
            still-open issue can never be filtered away and look fixed.
        project_key: component key whose open issues are read. It is used for
            the analysis-correlation component check; when omitted, the check
            is skipped and correlation cannot reach ``CORRELATED``.
        exclusive_analysis: explicit attestation of the single-analysis
            precondition required by the correlation check (see
            :mod:`analysis_correlation`). Defaults to False, which fails closed.
    """

    def __init__(
        self,
        client: Any,
        *,
        apply_issue_filter: bool = False,
        project_key: Optional[str] = None,
        exclusive_analysis: bool = False,
    ) -> None:
        if client is None or not callable(getattr(client, "get_open_issues", None)):
            raise SonarIssueVerificationError(
                "A SonarQube client exposing get_open_issues() is required."
            )
        self._client = client
        self._apply_filter = bool(apply_issue_filter)
        self._project_key = project_key
        self._exclusive_analysis = bool(exclusive_analysis)

    def verify(
        self,
        original_issue: SonarIssue,
        *,
        completion: Optional[Any] = None,
    ) -> SonarIssueVerificationResult:
        """Verify ``original_issue`` against the current open issues.

        The client is asked once for the project's open issues; no SonarQube
        state is changed and no issue is closed or resolved.

        Args:
            original_issue: the reference issue (from T08).
            completion: the T17 :class:`SonarAnalysisCompletion` this
                verification belongs to. It is used only for the
                analysis-correlation check; omitting it makes the correlation
                ``UNKNOWN`` (fail closed), so T19 can never grant ``FIXED``.

        Returns:
            A typed :class:`SonarIssueVerificationResult`. Retrieval failures and
            malformed payloads are represented in the result, not raised.

        Raises:
            SonarIssueVerificationError: ``original_issue`` is missing.
        """
        if original_issue is None:
            raise SonarIssueVerificationError(
                "An original issue is required to verify the fix."
            )

        correlation: AnalysisCorrelationResult = correlate_analysis(
            completion,
            project_key=self._project_key,
            exclusive_analysis=self._exclusive_analysis,
        )

        try:
            payload = self._client.get_open_issues()
        except SonarQubeError as exc:
            return self._failed(
                original_issue,
                "SonarQube issue retrieval failed: "
                + redact_credentials(str(exc)),
                correlation=correlation,
            )
        except Exception as exc:  # defensive: a broken client must not crash T19
            return self._failed(
                original_issue,
                "SonarQube issue retrieval failed unexpectedly: "
                + redact_credentials(f"{type(exc).__name__}: {exc}"),
                correlation=correlation,
            )

        if not isinstance(payload, Mapping):
            return self._failed(
                original_issue,
                "SonarQube returned an unexpected (non-object) issue payload.",
                correlation=correlation,
            )
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, (list, tuple)):
            return self._failed(
                original_issue,
                "SonarQube's issue response contained no usable 'issues' list, "
                "so the current state cannot be determined.",
                correlation=correlation,
            )

        skipped = sum(1 for entry in raw_issues if not isinstance(entry, Mapping))
        usable = [entry for entry in raw_issues if isinstance(entry, Mapping)]
        current: Tuple[SonarIssue, ...] = tuple(self._convert(usable))
        retrieved_count = len(raw_issues)

        total = payload.get("total")
        page_complete = _page_is_complete(total, retrieved_count)
        reported_total = (
            total if isinstance(total, int) and not isinstance(total, bool) else None
        )

        key = _usable_key(original_issue.key)
        matches, match_type, is_present, reliable, reasons = self._identify(
            original_issue, current, key, page_complete
        )
        matches = tuple(matches)
        if not page_complete:
            reasons = [
                *reasons,
                "Page completeness could not be established (the reported "
                "total is missing, malformed or larger than the retrieved "
                "entries), so the retrieved set may be truncated.",
            ]
        return self._build(
            original_issue=original_issue,
            key=key,
            current=current,
            matches=matches,
            match_type=match_type,
            is_present=is_present,
            identity_reliable=reliable,
            page_complete=page_complete,
            retrieved_count=retrieved_count,
            skipped_entries=skipped,
            reasons=reasons,
            error=None,
            reported_total=reported_total,
            correlation=correlation,
        )

    def _convert(self, entries: Sequence[Mapping[str, Any]]) -> list:
        """Map raw issues onto the T04 model, optionally applying T03 rules."""
        if self._apply_filter:
            return list(filter_issues(entries))
        return [to_sonar_issue(entry) for entry in entries]


    @staticmethod
    def _identify(
        original: SonarIssue,
        current: Tuple[SonarIssue, ...],
        key: str,
        page_complete: bool,
    ) -> Tuple[list, IssueMatchType, bool, bool, list]:
        """Apply the identity ladder documented in the module docstring.

        Returns:
            ``(matches, match_type, is_present, identity_reliable, reasons)``.
        """
        reasons: list = []

        if key:
            reasons.append(
                f"Identity uses the original SonarQube issue key '{key}'."
            )
            exact = [issue for issue in current if issue.key == key]
            if exact:
                reasons.append(
                    "The original issue key is still reported as open, so the "
                    "issue is still present."
                )
                return exact, IssueMatchType.KEY, True, True, reasons

            line_hits = [
                issue
                for issue in current
                if _same_rule_and_file(original, issue) and _same_line(original, issue)
            ]
            if line_hits:
                reasons.append(
                    "The original issue key is no longer open, but a different "
                    "issue with the same rule at the same file and line is open: "
                    "the original may have been re-keyed rather than fixed."
                )
                return (
                    line_hits,
                    IssueMatchType.COMPONENT_RULE_LINE,
                    True,
                    False,
                    reasons,
                )

            file_hits = [
                issue for issue in current if _same_rule_and_file(original, issue)
            ]
            if file_hits:
                reasons.append(
                    "The original issue key is no longer open, but a different "
                    "issue with the same rule in the same file is open; the fix "
                    "cannot be confirmed from its key."
                )
                return file_hits, IssueMatchType.COMPONENT_RULE, True, False, reasons

            reasons.append(
                "The original issue key is not among the current open issues."
            )
            if not page_complete:
                reasons.append(
                    "The open-issue retrieval was truncated, so the absence "
                    "cannot be trusted."
                )
            return [], IssueMatchType.KEY, False, page_complete, reasons

        reasons.append(
            "The original issue has no usable key, so identity falls back to "
            "rule and location."
        )

        line_hits = [
            issue
            for issue in current
            if _same_rule_and_file(original, issue) and _same_line(original, issue)
        ]
        if len(line_hits) == 1:
            reasons.append(
                "Exactly one open issue matches the original rule, file and line."
            )
            return (
                line_hits,
                IssueMatchType.COMPONENT_RULE_LINE,
                True,
                page_complete,
                reasons,
            )
        if len(line_hits) > 1:
            reasons.append(
                f"{len(line_hits)} open issues match the original rule, file and "
                "line, so the identity is ambiguous."
            )
            return (line_hits, IssueMatchType.COMPONENT_RULE_LINE, True, False, reasons)

        file_hits = [issue for issue in current if _same_rule_and_file(original, issue)]
        if len(file_hits) == 1:
            reasons.append("Exactly one open issue matches the original rule and file.")
            return file_hits, IssueMatchType.COMPONENT_RULE, True, page_complete, reasons
        if len(file_hits) > 1:
            reasons.append(
                f"{len(file_hits)} open issues match the original rule and file, "
                "so the identity is ambiguous."
            )
            return file_hits, IssueMatchType.COMPONENT_RULE, True, False, reasons

        reasons.append(
            "No open issue matches the original rule in the original file, and "
            "the original issue had no usable key: the absence cannot be "
            "verified reliably."
        )
        return [], IssueMatchType.NONE, False, False, reasons


    def _failed(
        self,
        original: SonarIssue,
        message: str,
        *,
        correlation: Optional[AnalysisCorrelationResult] = None,
    ) -> SonarIssueVerificationResult:
        """Build the result for an unusable retrieval (never a fix)."""
        return self._build(
            original_issue=original,
            key=_usable_key(original.key),
            current=(),
            matches=(),
            match_type=IssueMatchType.NONE,
            is_present=False,
            identity_reliable=False,
            page_complete=False,
            retrieved_count=0,
            skipped_entries=0,
            reasons=[message],
            error=message,
            correlation=correlation,
        )

    @staticmethod
    def _build(
        *,
        original_issue: SonarIssue,
        key: str,
        current: Tuple[SonarIssue, ...],
        matches: Tuple[SonarIssue, ...],
        match_type: IssueMatchType,
        is_present: bool,
        identity_reliable: bool,
        page_complete: bool,
        retrieved_count: int,
        skipped_entries: int,
        reasons: list,
        error: Optional[str],
        reported_total: Optional[int] = None,
        correlation: Optional[AnalysisCorrelationResult] = None,
    ) -> SonarIssueVerificationResult:
        """Assemble the verification result with a derived summary reason."""
        correlation_state = (
            AnalysisCorrelation.UNKNOWN if correlation is None else correlation.correlation
        )
        correlation_reason = "" if correlation is None else correlation.reason
        if correlation is not None:
            reasons = [correlation.reason, *reasons]

        if error is not None:
            summary = error
        elif is_present and identity_reliable:
            summary = "The original issue is still reported as open."
        elif is_present:
            summary = (
                "An equivalent issue is still open, but it cannot be confirmed "
                "as the original issue, so the result needs review."
            )
        elif identity_reliable and correlation_state is AnalysisCorrelation.CORRELATED:
            summary = "The original issue is not reported as open by the new analysis."
        elif identity_reliable:
            summary = (
                "The original issue is not reported as open, but the snapshot "
                "cannot be attributed to the verified analysis, so the result "
                "needs review."
            )
        else:
            summary = (
                "The original issue is not reported as open, but the absence "
                "cannot be trusted, so the result needs review."
            )
        return SonarIssueVerificationResult(
            original_issue=original_issue,
            original_issue_key=key,
            retrieval_succeeded=error is None,
            current_issues=tuple(current),
            matches=tuple(matches),
            matching_current_issue=(matches[0] if len(matches) == 1 else None),
            match_type=match_type,
            is_present=is_present,
            identity_reliable=identity_reliable,
            page_complete=page_complete,
            retrieved_count=retrieved_count,
            skipped_entries=skipped_entries,
            reason=summary,
            reasons=tuple(reasons),
            error=error,
            reported_total=reported_total,
            correlation=correlation_state,
            correlation_reason=correlation_reason,
        )


__all__: Sequence[str] = (
    "IssueMatchType",
    "SonarIssueVerificationError",
    "SonarIssueVerificationResult",
    "SonarIssueVerifier",
)

