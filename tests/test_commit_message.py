"""T20 unit tests: the deterministic, bounded, injection-proof commit message.

No Git and no repository are involved: ``commit_message`` is a pure module, so
every property (determinism, bounds, argv safety, rejection of unsafe input) is
asserted directly.
"""

from __future__ import annotations

import os

import pytest

from commit_message import (
    DEFAULT_MAX_MESSAGE_LENGTH,
    DEFAULT_MAX_SUBJECT_LENGTH,
    MAX_INPUT_LENGTH,
    MAX_TOKEN_LENGTH,
    SUBJECT_PREFIX,
    TRAILER_ISSUE_KEY,
    TRAILER_RULE_KEY,
    CommitMessage,
    CommitMessageError,
    build_commit_message,
    sanitize_commit_message,
    validate_commit_message,
)

RULE = "python:S1481"
ISSUE = "AX1"
TARGET = "src/app.py"
SUBJECT = f"{SUBJECT_PREFIX}: resolve {RULE} in {TARGET}"


def build(**overrides) -> CommitMessage:
    """Build the default message, overriding only what a test cares about."""
    kwargs = {"rule": RULE, "file_path": TARGET, "issue_key": ISSUE}
    kwargs.update(overrides)
    return build_commit_message(**kwargs)


# ---------------------------------------------------------------------------
# The message itself
# ---------------------------------------------------------------------------


class TestBuildCommitMessage:
    def test_default_format_is_a_conventional_commit(self):
        message = build()
        assert message.subject == SUBJECT
        assert message.text == (
            f"{SUBJECT}\n\n"
            f"{TRAILER_ISSUE_KEY}: {ISSUE}\n"
            f"{TRAILER_RULE_KEY}: {RULE}"
        )
        assert message.line_count == 4

    def test_message_is_deterministic_and_independent_of_the_environment(
        self, monkeypatch
    ):
        first = build().text
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Somebody Else")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00+00:00")
        monkeypatch.setenv("TZ", "Pacific/Kiritimati")
        second = build().text
        # The environment really did change, and the message still did not.
        assert os.environ["GIT_AUTHOR_NAME"] == "Somebody Else"
        assert first == second

    def test_message_is_bounded(self):
        message = build()
        assert len(message.subject) <= DEFAULT_MAX_SUBJECT_LENGTH
        assert len(message.text) <= DEFAULT_MAX_MESSAGE_LENGTH

    def test_message_contains_only_the_validated_inputs(self):
        message = build()
        assert message.text.split("\n") == [
            SUBJECT,
            "",
            f"{TRAILER_ISSUE_KEY}: {ISSUE}",
            f"{TRAILER_RULE_KEY}: {RULE}",
        ]
        assert message.issue_key == ISSUE
        assert message.rule == RULE
        assert message.file_path == TARGET

    def test_subject_is_a_single_line_and_never_starts_with_a_dash(self):
        for rule, path, issue in (
            (RULE, TARGET, ISSUE),
            ("java:S1854", "a/b/c/d/e/very/deep/File.java", "AX2"),
            ("javascript:S1854", "index.js", "AX3"),
        ):
            message = build(rule=rule, file_path=path, issue_key=issue)
            assert "\n" not in message.subject
            assert not message.subject.startswith("-")

    def test_argv_is_safe_for_the_git_command_line(self):
        message = build()
        assert message.argv == ("-m", message.text)
        assert message.argv[0] == "-m"
        assert len(message.argv) == 2

    def test_shortening_ladder_falls_back_to_the_basename_within_the_budget(self):
        message = build(
            file_path="nested/dir/deep.py", max_subject_length=50
        )
        assert message.subject == f"{SUBJECT_PREFIX}: resolve {RULE} in deep.py"
        assert len(message.subject) <= 50
        assert validate_commit_message(message.text) == message.text

    def test_last_resort_truncation_is_still_valid_and_bounded(self):
        message = build(
            file_path="a/very/long/path/to/some/module/file.py",
            max_message_length=60,
        )
        assert len(message.text) <= 60
        assert message.subject
        assert validate_commit_message(message.text) == message.text

    def test_caps_that_cannot_hold_the_identity_trailers_are_rejected(self):
        with pytest.raises(CommitMessageError):
            build(max_message_length=10)
        with pytest.raises(CommitMessageError):
            build(max_subject_length=5)

    def test_as_dict_is_serialisable_and_carries_no_credential(self):
        payload = build().as_dict()
        assert payload["subject"] == SUBJECT
        assert payload["text"] == build().text
        rendered = repr(payload).casefold()
        for marker in ("password", "secret", "token", "api_key", "apikey"):
            assert marker not in rendered


# ---------------------------------------------------------------------------
# Rejected inputs (never silently repaired)
# ---------------------------------------------------------------------------


class TestRejectedInputs:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"rule": None},
            {"rule": ""},
            {"rule": "   "},
            {"rule": 7},
            {"rule": b"python:S1481"},
            {"rule": "python S1481"},
            {"rule": "-S1481"},
            {"rule": "python:S1481; rm -rf /"},
            {"rule": "r" * (MAX_TOKEN_LENGTH + 1)},
            {"file_path": None},
            {"file_path": ""},
            {"file_path": "   "},
            {"file_path": 5},
            {"file_path": "/etc/passwd"},
            {"file_path": "C:\\Windows\\system32\\evil.py"},
            {"file_path": "../outside.py"},
            {"file_path": "../../etc/passwd"},
            {"file_path": "."},
            {"issue_key": None},
            {"issue_key": ""},
            {"issue_key": "AX1\nSonar-Rule: evil"},
            {"issue_key": "AX1\x00AX2"},
            {"issue_key": "AX1; rm -rf /"},
            {"issue_key": "AX1 && curl http://evil"},
        ],
    )
    def test_unusable_inputs_are_rejected(self, overrides):
        with pytest.raises(CommitMessageError):
            build(**overrides)

    def test_implausibly_long_input_is_refused_rather_than_truncated(self):
        with pytest.raises(CommitMessageError):
            build(rule="python:" + "S" * MAX_INPUT_LENGTH)
        with pytest.raises(CommitMessageError):
            build(issue_key="A" * (MAX_INPUT_LENGTH + 1))

    def test_a_root_level_or_absolute_path_never_reaches_the_message(self):
        for path in ("/", "\\", "//server/share/x.py", "..", "a/../..", "  ..  "):
            with pytest.raises(CommitMessageError):
                build(file_path=path)

    def test_a_newline_can_never_forge_a_trailer(self):
        with pytest.raises(CommitMessageError):
            build(issue_key=f"{ISSUE}\n{TRAILER_ISSUE_KEY}: {ISSUE}2")
        with pytest.raises(CommitMessageError):
            build(issue_key=f"{ISSUE}\r\n{TRAILER_RULE_KEY}: forged")

    def test_backslash_paths_are_normalized_not_rejected(self):
        message = build(file_path="src\\app.py")
        assert message.file_path == "src/app.py"
        assert "/" in message.subject


# ---------------------------------------------------------------------------
# sanitize_commit_message
# ---------------------------------------------------------------------------


class TestSanitizeCommitMessage:
    def test_control_characters_and_escapes_are_removed(self):
        assert sanitize_commit_message("a\x1b[31mred\x1b[0m") == "ared"
        assert sanitize_commit_message("a\x07b") == "ab"
        assert sanitize_commit_message("a\x9bb") == "ab"

    def test_whitespace_is_collapsed_and_the_result_is_stripped(self):
        assert sanitize_commit_message("  a \t b \n c  ") == "a b c"
        assert sanitize_commit_message("a\r\nb") == "a b"

    def test_newlines_are_only_kept_when_explicitly_allowed(self):
        assert sanitize_commit_message("a\nb") == "a b"
        assert (
            sanitize_commit_message("a\nb", allow_newlines=True) == "a\nb"
        )

    def test_the_result_is_bounded_by_max_length(self):
        assert sanitize_commit_message("abcdef", max_length=3) == "abc"
        assert len(sanitize_commit_message("x" * 500, max_length=10)) == 10

    @pytest.mark.parametrize("raw", [None, 1, 1.5, b"bytes", ["a"], object()])
    def test_non_text_is_rejected(self, raw):
        with pytest.raises(CommitMessageError):
            sanitize_commit_message(raw)

    def test_nul_empty_and_unusable_text_are_rejected(self):
        for raw in ("a\x00b", "", "   ", "\x1b[0m", "\x07"):
            with pytest.raises(CommitMessageError):
                sanitize_commit_message(raw)

    def test_an_implausibly_long_payload_is_rejected(self):
        with pytest.raises(CommitMessageError):
            sanitize_commit_message("x" * (MAX_INPUT_LENGTH + 1))

    @pytest.mark.parametrize("max_length", [0, -1, True, "10", None])
    def test_invalid_max_length_is_rejected(self, max_length):
        with pytest.raises(CommitMessageError):
            sanitize_commit_message("abc", max_length=max_length)


# ---------------------------------------------------------------------------
# validate_commit_message (the G42 gate)
# ---------------------------------------------------------------------------


class TestValidateCommitMessage:
    def test_a_valid_message_is_returned_unchanged(self):
        message = build()
        assert validate_commit_message(message.text) == message.text
        assert (
            validate_commit_message(message.subject) == message.subject
        )

    @pytest.mark.parametrize(
        "candidate",
        [
            None,
            12,
            b"bytes",
            "",
            "a\x00b",
            "subject\rwith carriage return",
            "subject\x1b[31mwith escape",
            "subject\x07with bell",
            "subject\n indented continuation",
            "subject\ntrailing space ",
            "s" * (DEFAULT_MAX_SUBJECT_LENGTH + 1),
            "subj\n" + "x" * DEFAULT_MAX_MESSAGE_LENGTH,
        ],
    )
    def test_an_unsafe_message_is_refused_with_none(self, candidate):
        assert validate_commit_message(candidate) is None

    def test_a_message_longer_than_the_cap_is_refused(self):
        text = "a" * 100 + "\n\n" + "b" * 120
        assert validate_commit_message(text, max_message_length=120) is None
        assert validate_commit_message(text, max_message_length=len(text)) == text

    def test_the_validator_never_repairs_its_input(self):
        candidate = "fix(sonar): resolve python:S1481 in src/app.py\x00"
        assert validate_commit_message(candidate) is None
