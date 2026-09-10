"""Filtering of SonarQube issues (T03) and conversion to internal models (T04).

Responsibility boundary
-----------------------
* Selecting issues according to the POC filtering rules.
* Converting the accepted raw issues into :class:`models.SonarIssue`.

This module performs no HTTP communication; that lives in ``sonar_client``.
"""

from typing import Any, Dict, Iterable, List, Optional

from models import SonarIssue

# Issue types allowed for the initial POC. INFO/other types are excluded by
# only whitelisting these two values.
ALLOWED_ISSUE_TYPES = {"CODE_SMELL", "BUG"}

# Severities allowed for the initial POC. "INFO" is deliberately excluded.
ALLOWED_SEVERITIES = {"BLOCKER", "CRITICAL", "MAJOR", "MINOR"}

# A raw SonarQube issue as returned by the issues/search API.
RawIssue = Dict[str, Any]


def is_allowed(raw_issue: RawIssue) -> bool:
    """Return True when ``raw_issue`` passes the T03 filtering rules."""
    return (
        raw_issue.get("type") in ALLOWED_ISSUE_TYPES
        and raw_issue.get("severity") in ALLOWED_SEVERITIES
    )


def to_sonar_issue(raw_issue: RawIssue) -> SonarIssue:
    """Map one raw SonarQube issue dictionary onto the internal model."""
    return SonarIssue(
        key=str(raw_issue.get("key") or ""),
        rule=str(raw_issue.get("rule") or ""),
        severity=str(raw_issue.get("severity") or ""),
        issue_type=str(raw_issue.get("type") or ""),
        message=str(raw_issue.get("message") or ""),
        component=str(raw_issue.get("component") or ""),
        line=_as_optional_int(raw_issue.get("line")),
        status=str(raw_issue.get("status") or ""),
    )


def _as_optional_int(value: Any) -> Optional[int]:
    """Convert ``value`` to int, tolerating None and non-numeric input."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def filter_issues(raw_issues: Iterable[RawIssue]) -> List[SonarIssue]:
    """Select the accepted issues and return them as ``SonarIssue`` objects.

    The filtering rules are the T03 rules (allowed types/severities) and are
    unchanged by the T04 conversion.
    """
    return [
        to_sonar_issue(raw_issue)
        for raw_issue in raw_issues
        if is_allowed(raw_issue)
    ]
