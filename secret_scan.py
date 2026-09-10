"""Secret scanning for a future Git commit (T20 safety primitive).

Purpose
-------
T20 will commit exactly one change set, so it must inspect *that* change before
it becomes history. This module provides the reusable, pure, deterministic
primitive: give it the diff text (or any equivalent text) plus the secrets this
project can actually know about, and it reports structured findings.

Detected
--------
1. **Configured secrets** - the literal values in
   :attr:`sonar_analysis.AnalysisConfig.secrets` (for example the SonarQube
   token). Matching is exact and case-sensitive.
2. **URL userinfo** - ``scheme://user@host`` and ``scheme://user:password@host``
   (the same shape :func:`repository.redact_credentials` already treats as
   credential-bearing). Plain URLs such as ``https://example.com/x`` are not
   findings.

Deliberately out of scope
-------------------------
This is **not** a generic secret-detection engine. It does not guess at
entropy, API-key shapes, private keys, or SCP-like ``user@host:path`` remotes
(which normally carry no password). It only knows what this project can
configure, which keeps false positives near zero and the result explainable.

Safety properties
-----------------
* The matched value is **never** stored, logged, or included in a reason or
  exception: findings carry a kind, a line number, an optional path, and - for
  configured secrets - the index of the configured entry.
* The scan fails closed: unusable input (not text, or larger than
  :data:`DEFAULT_MAX_TEXT_LENGTH`) is reported as *not scanned*, and
  ``SecretScanResult.ok`` is ``False`` for anything that is not a completed,
  finding-free scan.
* No I/O, no Git, no network, no commit: T20 is not wired here.

How T20 is expected to use it
-----------------------------
Scan the *staged* diff (``git diff --cached``) - never ``True``-by-default -
and refuse to commit unless ``result.ok`` is ``True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

from repository import redact_credentials

#: Refuse to scan absurdly large text instead of silently reporting "clean".
DEFAULT_MAX_TEXT_LENGTH = 1_048_576

#: ``scheme://userinfo@host`` with no whitespace, ``/``, ``@``, ``?`` or ``#``
#: inside the userinfo (so ``?email=user@host`` query values cannot match). The
#: userinfo is redacted before it is ever reported.
_URL_USERINFO_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@?#]{1,256}@[^\s/]{1,256}"
)

#: Maximum number of findings reported (a runaway diff must not blow up memory).
_MAX_FINDINGS = 500


class SecretFindingKind(Enum):
    """What kind of credential-shaped content was found."""

    __test__ = False

    #: One of the configured secret values appears verbatim.
    CONFIGURED_SECRET = "configured-secret"
    #: A URL carries userinfo (``user`` or ``user:password``).
    URL_USERINFO = "url-userinfo"


@dataclass(frozen=True)
class SecretFinding:
    """One detection. Never carries the secret value itself.

    Attributes:
        kind: what was found.
        line: 1-based line number where the match starts (``0`` when unknown).
        detail: redacted, human-readable description.
        secret_index: 1-based index of the configured secret that matched
            (``None`` for URL userinfo).
        path: file the line belongs to, when the input looks like a unified
            diff and a path could be derived.
    """

    __test__ = False

    kind: SecretFindingKind
    line: int
    detail: str
    secret_index: Optional[int] = None
    path: Optional[str] = None

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "kind": self.kind.value,
            "line": self.line,
            "detail": self.detail,
            "secret_index": self.secret_index,
            "path": self.path,
        }


@dataclass(frozen=True)
class SecretScanResult:
    """Typed outcome of one scan.

    Attributes:
        scanned: the input was usable and was actually scanned.
        clean: no finding was reported (meaningless unless ``scanned``).
        findings: ordered findings.
        reason: short human-readable summary.
        secret_count: how many non-empty configured secrets were used.
    """

    __test__ = False

    scanned: bool
    clean: bool
    findings: Tuple[SecretFinding, ...]
    reason: str
    secret_count: int = 0

    @property
    def ok(self) -> bool:
        """True only for a completed scan with no finding (fail closed)."""
        return bool(self.scanned and self.clean)

    def as_dict(self) -> dict:
        """Secret-free summary suitable for logging/reporting."""
        return {
            "scanned": self.scanned,
            "clean": self.clean,
            "ok": self.ok,
            "finding_count": len(self.findings),
            "findings": [finding.as_dict() for finding in self.findings],
            "reason": self.reason,
            "secret_count": self.secret_count,
        }


def _normalise_secrets(secrets: Optional[Iterable[object]]) -> Tuple[str, ...]:
    """Return the distinct, non-empty configured secrets, in order.

    Duplicates are collapsed and blank/whitespace-only values are dropped;
    anything else is kept verbatim (secrets are case-sensitive and are never
    trimmed).
    """
    if secrets is None:
        return ()
    if isinstance(secrets, (str, bytes)):
        raise TypeError(
            "secrets must be an iterable of secret strings, not a single string."
        )
    ordered: List[str] = []
    for value in secrets:
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        if not text.strip():
            continue
        if text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def _path_for_line(lines: Sequence[str], line_number: int) -> Optional[str]:
    """Best-effort file path for a 1-based line in a unified diff.

    Looks backwards for the nearest ``+++ b/<path>`` target header, which is how
    ``git diff`` names the file a hunk belongs to.
    """
    if line_number <= 0:
        return None
    for candidate in range(min(line_number, len(lines)) - 1, -1, -1):
        text = lines[candidate]
        if text.startswith("+++ "):
            target = text[4:].strip()
            if target in ("", "/dev/null"):
                return None
            if target.startswith("b/"):
                target = target[2:]
            return target
    return None


class SecretScanner:
    """Pure, deterministic scanner for configured secrets and URL userinfo.

    Args:
        secrets: the literal secret values this project knows about (normally
            ``AnalysisConfig.secrets``).
        detect_url_userinfo: also flag ``scheme://user[:password]@host`` shapes.
        max_text_length: refuse (fail closed) to scan longer inputs.
    """

    def __init__(
        self,
        secrets: Optional[Iterable[object]] = (),
        *,
        detect_url_userinfo: bool = True,
        max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    ) -> None:
        if not isinstance(max_text_length, int) or max_text_length <= 0:
            raise ValueError(
                "max_text_length must be a positive integer "
                f"(got {max_text_length!r})."
            )
        self._secrets = _normalise_secrets(secrets)
        self._detect_url_userinfo = bool(detect_url_userinfo)
        self._max_text_length = max_text_length

    @property
    def secret_count(self) -> int:
        """How many distinct non-empty secrets this scanner looks for."""
        return len(self._secrets)

    def scan_text(self, text: object) -> SecretScanResult:
        """Scan ``text`` for configured secrets and URL userinfo.

        Returns:
            A typed :class:`SecretScanResult`. Unusable input is reported as
            ``scanned=False``/``clean=False`` (fail closed); nothing is raised
            and no secret value is ever included in the result.
        """
        if not isinstance(text, str):
            return SecretScanResult(
                scanned=False,
                clean=False,
                findings=(),
                reason=(
                    "No text was supplied to scan (expected a string), so the "
                    "content cannot be declared free of secrets."
                ),
                secret_count=len(self._secrets),
            )
        if len(text) > self._max_text_length:
            return SecretScanResult(
                scanned=False,
                clean=False,
                findings=(),
                reason=(
                    f"The content is larger than the {self._max_text_length} "
                    "character scan limit, so it cannot be declared free of "
                    "secrets."
                ),
                secret_count=len(self._secrets),
            )

        lines = text.splitlines()
        findings: List[SecretFinding] = []
        seen: set = set()

        for index, secret in enumerate(self._secrets, start=1):
            if len(findings) >= _MAX_FINDINGS:
                break
            start = text.find(secret)
            while start != -1:
                line_number = text.count("\n", 0, start) + 1
                key = (SecretFindingKind.CONFIGURED_SECRET, line_number, index)
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        SecretFinding(
                            kind=SecretFindingKind.CONFIGURED_SECRET,
                            line=line_number,
                            detail=(
                                "A configured secret value appears in the "
                                f"content (configured entry #{index})."
                            ),
                            secret_index=index,
                            path=_path_for_line(lines, line_number),
                        )
                    )
                if len(findings) >= _MAX_FINDINGS:
                    break
                start = text.find(secret, start + len(secret))

        if self._detect_url_userinfo:
            for match in _URL_USERINFO_RE.finditer(text):
                if len(findings) >= _MAX_FINDINGS:
                    break
                line_number = text.count("\n", 0, match.start()) + 1
                findings.append(
                    SecretFinding(
                        kind=SecretFindingKind.URL_USERINFO,
                        line=line_number,
                        detail=(
                            "A URL with embedded userinfo was found: "
                            f"{redact_credentials(match.group(0))}."
                        ),
                        secret_index=None,
                        path=_path_for_line(lines, line_number),
                    )
                )

        findings.sort(key=lambda finding: (finding.line, finding.kind.value))
        ordered = tuple(findings)
        clean = not ordered
        if clean:
            reason = (
                "No configured secret and no URL userinfo was found in the "
                "scanned content."
            )
        else:
            kinds = sorted({finding.kind.value for finding in ordered})
            reason = (
                f"{len(ordered)} credential-shaped finding(s) in the scanned "
                f"content ({', '.join(kinds)})."
            )
        return SecretScanResult(
            scanned=True,
            clean=clean,
            findings=ordered,
            reason=reason,
            secret_count=len(self._secrets),
        )

    def scan_diff(self, diff_text: object) -> SecretScanResult:
        """Scan unified diff text (added, removed and context lines alike).

        Identical to :meth:`scan_text`; a diff is text, so there is no separate
        line classification that could hide a removed or context occurrence.
        """
        return self.scan_text(diff_text)


def scan_for_secrets(
    text: object,
    *,
    secrets: Optional[Iterable[object]] = (),
    detect_url_userinfo: bool = True,
) -> SecretScanResult:
    """Convenience wrapper around :class:`SecretScanner`."""
    return SecretScanner(
        secrets, detect_url_userinfo=detect_url_userinfo
    ).scan_text(text)


__all__: Sequence[str] = (
    "DEFAULT_MAX_TEXT_LENGTH",
    "SecretFinding",
    "SecretFindingKind",
    "SecretScanResult",
    "SecretScanner",
    "scan_for_secrets",
)
