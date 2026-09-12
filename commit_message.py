"""Deterministic, bounded, injection-proof commit messages (T20).

Purpose
-------
T20 commits exactly one agent change, so the commit message must be *derived*
from data T20 already validated and must never be free-form input. A commit
message is executed by Git (hooks, filters, terminals, CI) and is echoed into
logs, so the safety properties are:

* **pure and deterministic** - the same validated inputs always produce byte
  identical text; there is no clock, no random value, no environment lookup;
* **bounded** - the subject, every line and the whole message have hard limits;
* **no control characters** - C0/C1 controls, NUL and terminal escape
  sequences (CSI/OSC/SS2/SS3) are removed or rejected, so no message can
  reprogram a terminal, forge a log line or inject a NUL byte;
* **no newline injection** - the subject is always a single line; body lines can
  only come from validated trailers built by this module;
* **no arbitrary user text** - only a validated Sonar rule key, a validated
  repository-relative file path and a validated issue key enter the message;
* **safe for argv** - the message is handed to Git as ``("commit", "-m", text)``
  with an argument array and ``shell=False``; no shell interpolation happens and
  the value can never be split into options because it is a single argv item
  (a subject starting with ``-`` is refused anyway).

Default format (conventional commit, SonarQube identity preserved)::

    fix(sonar): resolve python:S1481 in src/app.py

    Sonar-Issue: AX1abcDefG
    Sonar-Rule: python:S1481

Rejected, never silently repaired: non-text input, NUL, empty/whitespace-only
text, oversized input, an unusable rule key, an unsafe file path and an
unusable issue key. :class:`CommitMessageError` is raised instead.

Scope boundary
--------------
This module performs no I/O, no Git, no subprocess, no file access and no
secrets handling (a message never contains a credential because only the rule
key, the path and the issue key are used).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from change_scope import normalise_relative_path

#: Hard cap on the whole message (subject + body).
DEFAULT_MAX_MESSAGE_LENGTH = 200
#: Hard cap on the single-line subject.
DEFAULT_MAX_SUBJECT_LENGTH = 100
#: Hard cap on a validated token (rule key / issue key).
MAX_TOKEN_LENGTH = 100
#: Inputs longer than this are rejected outright instead of being truncated:
#: such a payload is never a legitimate SonarQube value.
MAX_INPUT_LENGTH = 4096

#: Conventional-commit scope for every message this project produces.
SUBJECT_PREFIX = "fix(sonar)"
#: Trailer keys used to preserve the SonarQube identity of the change.
TRAILER_ISSUE_KEY = "Sonar-Issue"
TRAILER_RULE_KEY = "Sonar-Rule"

#: Terminal escape sequences: CSI, OSC and the two/three byte SSx/ESC forms.
_ESCAPE_SEQUENCES = re.compile(
    chr(27)
    + r"(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)
#: C0 controls (except TAB and LF), DEL and C1 controls.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: Any run of whitespace (including TAB/LF/CR) collapses to one space.
_WHITESPACE = re.compile(r"\s+")
#: Characters allowed in a validated token (Sonar rule keys / issue keys).
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class CommitMessageError(ValueError):
    """Raised when a commit message cannot be built or validated safely."""


def sanitize_commit_message(
    raw: object,
    *,
    max_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
    allow_newlines: bool = False,
) -> str:
    """Return a safe, bounded, single-purpose text form of ``raw``.

    Args:
        raw: the text to sanitize. Anything that is not ``str`` is rejected.
        max_length: hard cap applied to the sanitized result.
        allow_newlines: when ``False`` (the default) every newline collapses
            into a space, so the result can only ever be one line.

    Returns:
        The sanitized text, stripped and non-empty.

    Raises:
        CommitMessageError: ``raw`` is not text, contains a NUL byte, is
            empty/whitespace-only after sanitisation, or is longer than
            :data:`MAX_INPUT_LENGTH` (a payload that large is never a
            legitimate SonarQube value, so it is refused rather than trimmed).
    """
    if not isinstance(raw, str):
        raise CommitMessageError(
            f"Commit message text must be a string (got {type(raw).__name__})."
        )
    if "\x00" in raw:
        raise CommitMessageError("Commit message text must not contain NUL.")
    if len(raw) > MAX_INPUT_LENGTH:
        raise CommitMessageError(
            "Commit message text is implausibly long "
            f"({len(raw)} characters > {MAX_INPUT_LENGTH})."
        )
    if isinstance(max_length, bool) or not isinstance(max_length, int):
        raise CommitMessageError("max_length must be an integer.")
    if max_length < 1:
        raise CommitMessageError("max_length must be at least 1.")

    text = _ESCAPE_SEQUENCES.sub("", raw)
    text = _CONTROL_CHARS.sub("", text)
    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(part.strip() for part in text.split("\n"))
    else:
        text = _WHITESPACE.sub(" ", text)
    text = text.strip()
    if not text:
        raise CommitMessageError(
            "Commit message text contains no usable characters."
        )
    if len(text) > max_length:
        text = text[:max_length].strip()
        if not text:
            raise CommitMessageError(
                "Commit message text contains no usable characters."
            )
    return text


def _validated_token(raw: object, *, what: str) -> str:
    """Return ``raw`` as a validated token (rule key / issue key).

    Raises:
        CommitMessageError: the value is missing, unusable, longer than
            :data:`MAX_TOKEN_LENGTH`, or contains characters that have no place
            in a SonarQube identifier.
    """
    if raw is None:
        raise CommitMessageError(f"{what} is required to build a commit message.")
    text = sanitize_commit_message(raw, max_length=MAX_TOKEN_LENGTH + 1)
    if len(text) > MAX_TOKEN_LENGTH:
        raise CommitMessageError(
            f"{what} is longer than {MAX_TOKEN_LENGTH} characters."
        )
    if not _TOKEN_CHARS.match(text):
        raise CommitMessageError(
            f"{what} {text!r} is not a valid SonarQube identifier."
        )
    return text


def _validated_file_path(raw: object) -> str:
    """Return ``raw`` as a safe, normalized repository-relative path.

    Reuses the T13 path rules (:func:`change_scope.normalise_relative_path`), so
    an absolute path, a ``..`` component, the repository root or an empty value
    can never reach a commit message.

    Raises:
        CommitMessageError: the path is missing or unsafe.
    """
    if raw is None:
        raise CommitMessageError("A file path is required to build a message.")
    if not isinstance(raw, str):
        # ``normalise_relative_path`` deliberately stringifies whatever Git
        # reported; a commit message must instead fail closed on a value that is
        # not text at all (an int would otherwise become the path "5").
        raise CommitMessageError(
            f"A file path must be a string (got {type(raw).__name__})."
        )
    normalized = normalise_relative_path(raw)
    if normalized is None:
        raise CommitMessageError(
            f"File path {str(raw).strip()!r} is not a safe "
            "repository-relative path."
        )
    try:
        normalized = sanitize_commit_message(
            normalized, max_length=MAX_INPUT_LENGTH
        )
    except CommitMessageError as exc:
        raise CommitMessageError(
            f"File path {str(raw).strip()!r} cannot be used in a commit "
            f"message: {exc}"
        ) from exc
    return normalized


def _build_subject(rule: str, file_path: str, *, budget: int) -> str:
    """Build the single-line subject within ``budget`` characters.

    Deterministic shortening ladder (never a random or clock-based value):

    1. ``fix(sonar): resolve <rule> in <path>``
    2. ``fix(sonar): resolve <rule> in <basename>``
    3. ``fix(sonar): resolve <rule>``
    4. a hard truncation of step 1

    Raises:
        CommitMessageError: even the shortened forms do not fit the budget.
    """
    candidates = (
        f"{SUBJECT_PREFIX}: resolve {rule} in {file_path}",
        f"{SUBJECT_PREFIX}: resolve {rule} in {file_path.rsplit('/', 1)[-1]}",
        f"{SUBJECT_PREFIX}: resolve {rule}",
    )
    for candidate in candidates:
        if len(candidate) <= budget:
            return candidate
    subject = candidates[0][:budget].strip()
    if not subject:
        raise CommitMessageError(
            "No commit subject fits the configured length budget."
        )
    return subject


@dataclass(frozen=True)
class CommitMessage:
    """An immutable, validated commit message.

    Attributes:
        subject: the single-line header (already sanitized and bounded).
        issue_key: the validated SonarQube issue key used for the trailer.
        rule: the validated SonarQube rule key.
        file_path: the validated repository-relative path the change belongs to.
        body: validated trailer lines (``Sonar-Issue``, optionally
            ``Sonar-Rule``), or ``()`` when they did not fit the length cap.
    """

    subject: str
    issue_key: str
    rule: str
    file_path: str
    body: Tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The exact text passed to ``git commit -m``."""
        if not self.body:
            return self.subject
        return f"{self.subject}\n\n" + "\n".join(self.body)

    @property
    def argv(self) -> Tuple[str, ...]:
        """The Git arguments for this message (``-m`` plus the text)."""
        return ("-m", self.text)

    @property
    def line_count(self) -> int:
        """Number of lines in :attr:`text` (1 for a subject-only message)."""
        return self.text.count("\n") + 1

    def as_dict(self) -> dict:
        """Summary for logging/reporting (a commit message is not a secret)."""
        return {
            "subject": self.subject,
            "issue_key": self.issue_key,
            "rule": self.rule,
            "file_path": self.file_path,
            "body": list(self.body),
            "text": self.text,
        }


def validate_commit_message(
    message: object,
    *,
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
    max_subject_length: int = DEFAULT_MAX_SUBJECT_LENGTH,
) -> Optional[str]:
    """Validate an already-built message (the T20 ``G42`` gate).

    Returns the validated text unchanged, or ``None`` when anything at all is
    wrong. Callers must treat ``None`` as a refusal: this helper never repairs,
    truncates or re-encodes its input.

    Rejected: non-text, NUL, control characters other than single line feeds,
    terminal escapes, carriage returns, an empty subject, a subject longer than
    ``max_subject_length``, indented/trailing whitespace in any line, and a
    whole message longer than ``max_message_length``.
    """
    if not isinstance(message, str) or not message:
        return None
    if "\x00" in message:
        return None
    if len(message) > max_message_length:
        return None
    if _ESCAPE_SEQUENCES.search(message):
        return None
    if _CONTROL_CHARS.search(message):
        return None
    if "\r" in message:
        return None
    lines = message.split("\n")
    subject = lines[0]
    if not subject or len(subject) > max_subject_length:
        return None
    if any(line != line.strip() for line in lines):
        return None
    return message


def build_commit_message(
    *,
    rule: object,
    file_path: object,
    issue_key: object,
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
    max_subject_length: int = DEFAULT_MAX_SUBJECT_LENGTH,
) -> CommitMessage:
    """Build the deterministic commit message for one SonarQube fix.

    Args:
        rule: SonarQube rule key, e.g. ``python:S1481``.
        file_path: repository-relative file the change belongs to.
        issue_key: SonarQube issue key (preserved in the ``Sonar-Issue``
            trailer).
        max_message_length: hard cap for the whole message.
        max_subject_length: hard cap for the subject line.

    Returns:
        A frozen :class:`CommitMessage` whose :attr:`CommitMessage.text` is safe
        to hand to Git as a single argv item.

    Raises:
        CommitMessageError: any input is missing, unsafe or unusable, or the
            configured caps cannot hold the shortest valid message.
    """
    validated_rule = _validated_token(rule, what="Rule key")
    validated_issue = _validated_token(issue_key, what="Issue key")
    validated_path = _validated_file_path(file_path)

    # The identity trailers are part of the contract, so the subject is never
    # allowed to consume the whole budget.
    reserve = (
        len("\n\n")
        + len(f"{TRAILER_ISSUE_KEY}: {validated_issue}")
        + 1
        + len(f"{TRAILER_RULE_KEY}: {validated_rule}")
        + 1
    )
    budget = min(max_subject_length, max_message_length - reserve)
    if budget < 10:
        raise CommitMessageError(
            "The configured commit-message length caps cannot hold the "
            "required SonarQube identity trailers."
        )
    subject = _build_subject(validated_rule, validated_path, budget=budget)

    candidates = (
        (
            f"{TRAILER_ISSUE_KEY}: {validated_issue}",
            f"{TRAILER_RULE_KEY}: {validated_rule}",
        ),
        (f"{TRAILER_ISSUE_KEY}: {validated_issue}",),
        (),
    )
    body: Tuple[str, ...] = ()
    for candidate in candidates:
        text = subject if not candidate else f"{subject}\n\n" + "\n".join(candidate)
        if len(text) <= max_message_length:
            body = candidate
            break
    else:  # pragma: no cover - defensive: the budget above guarantees a fit
        raise CommitMessageError(
            "No valid commit message fits the configured length cap."
        )

    message = CommitMessage(
        subject=subject,
        issue_key=validated_issue,
        rule=validated_rule,
        file_path=validated_path,
        body=body,
    )
    if (
        validate_commit_message(
            message.text,
            max_message_length=max_message_length,
            max_subject_length=max_subject_length,
        )
        is None
    ):  # pragma: no cover - defensive: the builder never emits invalid text
        raise CommitMessageError(
            "The built commit message failed its own validation."
        )
    return message


__all__: Sequence[str] = (
    "DEFAULT_MAX_MESSAGE_LENGTH",
    "DEFAULT_MAX_SUBJECT_LENGTH",
    "MAX_INPUT_LENGTH",
    "MAX_TOKEN_LENGTH",
    "SUBJECT_PREFIX",
    "TRAILER_ISSUE_KEY",
    "TRAILER_RULE_KEY",
    "CommitMessage",
    "CommitMessageError",
    "build_commit_message",
    "sanitize_commit_message",
    "validate_commit_message",
)
