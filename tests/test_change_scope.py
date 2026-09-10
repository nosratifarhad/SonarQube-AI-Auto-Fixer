"""T13 tests: single-issue change-scope validation.

Unit tests build ``IssueContext``/``GitDiffResult`` values directly (the
validator is pure policy), plus two integration tests that use a real local
Git clone through T08/T12.
"""

import dataclasses
import os
from pathlib import Path

import pytest

from change_scope import (
    ChangeScopeError,
    ChangeScopeResult,
    ChangeScopeValidator,
    normalise_relative_path,
)
from context import IssueContext
from git_diff import GitDiffInspector, GitDiffResult
from models import SonarIssue

AGENT = "ai/sonar-fix/AX1abcDeF2"
TARGET = "src/app.py"
CASE_INSENSITIVE = os.name == "nt"


def make_issue(component="demo:" + TARGET, line=3, key="AX1abcDeF2"):
    return SonarIssue(
        key=key,
        rule="python:S108",
        severity="MAJOR",
        issue_type="CODE_SMELL",
        message="define a constant",
        component=component,
        line=line,
        status="OPEN",
    )


def make_context(repo, file_path=TARGET, **overrides):
    fields = {
        "issue": make_issue(),
        "repository_path": Path(repo),
        "source_branch": "main",
        "agent_branch": AGENT,
        "file_path": file_path,
        "absolute_file_path": Path(repo) / file_path,
        "line": 3,
    }
    fields.update(overrides)
    return IssueContext(**fields)


def make_diff(repo, changed=(), untracked=()):
    """Build a real ``GitDiffResult`` from explicit file lists (T12 shape)."""
    changed = tuple(changed)
    untracked = tuple(untracked)
    clean = not changed and not untracked
    return GitDiffResult(
        repository_path=Path(repo),
        is_clean=clean,
        has_changes=not clean,
        changed_files=changed,
        untracked_files=untracked,
        diff_stat="",
        diff_text="",
    )


@pytest.fixture
def validator():
    return ChangeScopeValidator()


class TestNormaliseRelativePath:
    def test_backslashes_become_posix(self):
        assert normalise_relative_path("src\\app.py") == "src/app.py"

    def test_dot_segments_and_duplicate_separators_collapse(self):
        assert normalise_relative_path("./src//./app.py") == "src/app.py"

    def test_trailing_slash_is_stripped(self):
        assert normalise_relative_path("src/app.py/") == "src/app.py"

    @pytest.mark.parametrize(
        "value",
        ["", "   ", None, ".", "/", "..", "../secret.py",
         "src/../../secret.py", "/etc/passwd", "C:/Windows/system32",
         "C:\\Windows\\system32", "\\\\server\\share\\x.py"],
    )
    def test_unusable_entries_return_none(self, value):
        assert normalise_relative_path(value) is None

    def test_unicode_paths_are_kept(self):
        assert normalise_relative_path("src/\u00e9t\u00e9.py") == \
            "src/\u00e9t\u00e9.py"


class TestChangeScopeValidator:
    def test_target_file_only_change_is_valid(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path), make_diff(tmp_path, ["src/app.py"])
        )
        assert isinstance(result, ChangeScopeResult)
        assert result.is_valid is True
        assert result.expected_files == ("src/app.py",)
        assert result.changed_files == ("src/app.py",)
        assert result.unexpected_files == ()
        assert result.has_changes is True
        assert result.expected_file_modified is True

    def test_clean_tree_is_valid_but_reports_no_changes(self, tmp_path, validator):
        result = validator.validate(make_context(tmp_path), make_diff(tmp_path))
        assert result.is_valid is True
        assert result.has_changes is False
        assert result.changed_files == ()
        assert result.expected_file_modified is False
        assert "No files changed" in result.reason_text

    def test_unrelated_file_fails_and_is_reported(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["src/other.py"]),
        )
        assert result.is_valid is False
        assert result.unexpected_files == ("src/other.py",)
        assert result.expected_file_modified is False
        assert "src/other.py" in result.reason_text

    def test_target_plus_unrelated_file_fails(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["src/app.py", "src/other.py"]),
        )
        assert result.is_valid is False
        assert result.changed_files == ("src/app.py", "src/other.py")
        assert result.unexpected_files == ("src/other.py",)
        assert result.expected_file_modified is True

    def test_untracked_file_outside_scope_fails(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["notes.md"], untracked=["notes.md"]),
        )
        assert result.is_valid is False
        assert result.unexpected_files == ("notes.md",)

    def test_windows_separators_for_the_target_file_are_accepted(
        self, tmp_path, validator
    ):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["src\\app.py"]),
        )
        assert result.is_valid is True
        assert result.changed_files == ("src/app.py",)
        assert result.expected_file_modified is True

    def test_duplicate_entries_are_deduplicated(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["src/app.py", "./src/app.py", "src\\app.py"]),
        )
        assert result.changed_files == ("src/app.py",)
        assert result.is_valid is True

    def test_output_is_sorted_and_deterministic(self, tmp_path, validator):
        diff = make_diff(tmp_path, ["b/2.py", "a/1.py", "b/2.py"])
        first = validator.validate(make_context(tmp_path), diff)
        second = validator.validate(make_context(tmp_path), diff)
        assert first.unexpected_files == ("a/1.py", "b/2.py")
        assert first == second

    def test_result_is_frozen_and_reports_as_dict(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path), make_diff(tmp_path, ["src/app.py"])
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.is_valid = False
        payload = result.as_dict()
        assert payload["is_valid"] is True
        assert payload["expected_files"] == ["src/app.py"]
        assert payload["unexpected_files"] == []

    def test_reason_and_reason_text_are_exposed(self, tmp_path, validator):
        result = validator.validate(make_context(tmp_path), make_diff(tmp_path))
        assert result.reason
        assert result.reason == result.reasons[0]


class TestChangeScopeSecurity:
    def test_absolute_path_always_fails_with_reason(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["/etc/passwd"]),
        )
        assert result.is_valid is False
        assert "/etc/passwd" in result.unexpected_files
        assert "absolute" in result.reason_text

    def test_windows_absolute_path_always_fails(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["C:\\Windows\\system32\\drivers"]),
        )
        assert result.is_valid is False
        assert "absolute" in result.reason_text

    def test_traversal_entry_always_fails(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["../outside.py"]),
        )
        assert result.is_valid is False
        assert "../outside.py" in result.unexpected_files
        assert "traversal" in result.reason_text

    def test_empty_entry_always_fails(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path), make_diff(tmp_path, ["   "])
        )
        assert result.is_valid is False

    def test_unsafe_context_file_path_raises(self, tmp_path, validator):
        with pytest.raises(ChangeScopeError, match="not a safe"):
            validator.validate(
                make_context(tmp_path, file_path="../../etc/passwd"),
                make_diff(tmp_path, ["../../etc/passwd"]),
            )

    def test_diff_from_another_repository_raises(self, tmp_path, validator):
        other = tmp_path / "other-repo"
        other.mkdir()
        with pytest.raises(ChangeScopeError, match="different"):
            validator.validate(
                make_context(tmp_path),
                make_diff(other, ["src/app.py"]),
            )

    def test_case_matching_follows_platform(self, tmp_path, validator):
        result = validator.validate(
            make_context(tmp_path),
            make_diff(tmp_path, ["src/App.py"]),
        )
        if CASE_INSENSITIVE:
            assert result.is_valid is True
        else:
            assert result.is_valid is False
            assert result.unexpected_files == ("src/App.py",)


class TestChangeScopeIntegration:
    """T08 + T12 + T13 wired together against a real local Git clone."""

    @pytest.fixture
    def agent_repo(self, rm, make_remote_repo, clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("scope-work")
        rm.clone(str(remote), repo)
        rm.create_agent_branch(repo, AGENT, "main")
        return repo

    def test_target_only_change_is_in_scope(self, agent_repo, validator):
        (agent_repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        diff = GitDiffInspector().inspect(agent_repo)
        result = validator.validate(make_context(agent_repo), diff)
        assert result.is_valid is True
        assert result.expected_file_modified is True

    def test_extra_untracked_file_is_out_of_scope(self, agent_repo, validator):
        (agent_repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        (agent_repo / "extra.txt").write_text("oops\n", encoding="utf-8")
        diff = GitDiffInspector().inspect(agent_repo)
        result = validator.validate(make_context(agent_repo), diff)
        assert result.is_valid is False
        assert result.unexpected_files == ("extra.txt",)
        assert result.expected_file_modified is True

