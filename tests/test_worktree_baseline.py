"""Tests for the T20 clean-baseline safety primitive.

All Git work happens in real local repositories under ``tmp_path`` (the shared
``conftest`` fixtures). No network, no real Codex run, no commit and no push.
"""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from worktree_baseline import (
    BaselineAttribution,
    WorktreeBaselineError,
    WorktreeBaselineInspector,
    WorktreeSnapshot,
    _parse_porcelain_z,
    attribute_changes,
)

GIT_IDENTITY = ["-c", "user.name=Test User", "-c", "user.email=test@example.com"]
TARGET = "src/app.py"


class FakeOutcome:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def repo(make_remote_repo, rm, clone_destination) -> Path:
    """A clean local clone of the standard demo remote."""
    remote = make_remote_repo()
    return rm.clone(str(remote), clone_destination())


@pytest.fixture
def inspector() -> WorktreeBaselineInspector:
    return WorktreeBaselineInspector(timeout_seconds=60.0)


def touch(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class TestCapture:
    def test_clean_baseline(self, repo, inspector):
        snapshot = inspector.capture(repo)
        assert isinstance(snapshot, WorktreeSnapshot)
        assert snapshot.repository_path == repo.resolve()
        assert snapshot.is_clean is True
        assert snapshot.changed_files == ()
        assert snapshot.staged_files == ()
        assert snapshot.untracked_files == ()
        assert snapshot.fingerprints == {}
        assert snapshot.head_commit and len(snapshot.head_commit) == 40

    def test_capture_is_read_only(self, repo, inspector, git):
        touch(repo, TARGET, "changed\n")
        before = git(repo, "status", "--porcelain")
        first = inspector.capture(repo)
        second = inspector.capture(repo)
        after = git(repo, "status", "--porcelain")
        assert before == after
        assert first == second

    def test_modified_tracked_file(self, repo, inspector):
        touch(repo, TARGET, "changed\n")
        snapshot = inspector.capture(repo)
        assert snapshot.changed_files == (TARGET,)
        assert snapshot.changed_set == {TARGET}
        assert snapshot.tracked_modified_files == (TARGET,)
        assert snapshot.staged_files == ()
        assert snapshot.untracked_files == ()

    def test_staged_file_is_classified_as_staged(self, repo, inspector, git):
        touch(repo, "src/added.py", "x = 1\n")
        git(repo, "add", "src/added.py")
        snapshot = inspector.capture(repo)
        assert snapshot.staged_files == ("src/added.py",)
        assert snapshot.tracked_modified_files == ()

    def test_untracked_file_is_classified_as_untracked(self, repo, inspector):
        touch(repo, "src/new.py", "x = 1\n")
        snapshot = inspector.capture(repo)
        assert snapshot.untracked_files == ("src/new.py",)
        assert snapshot.changed_files == ("src/new.py",)

    def test_deleted_file_is_classified_as_deleted(self, repo, inspector):
        (repo / TARGET).unlink()
        snapshot = inspector.capture(repo)
        assert snapshot.deleted_files == (TARGET,)
        assert snapshot.changed_files == (TARGET,)

    def test_renamed_file_keeps_the_origin(self, repo, inspector, git):
        git(repo, "mv", TARGET, "src/renamed.py")
        snapshot = inspector.capture(repo)
        assert snapshot.changed_files == ("src/renamed.py",)
        assert snapshot.renamed_files == (("src/renamed.py", TARGET),)

    def test_paths_with_spaces_are_kept_intact(self, repo, inspector, git):
        git(repo, "mv", TARGET, "src/my app.py")
        touch(repo, "docs with spaces/notes file.txt", "hello\n")
        snapshot = inspector.capture(repo)
        assert "src/my app.py" in snapshot.changed_files
        assert "docs with spaces/notes file.txt" in snapshot.untracked_files

    def test_ignored_files_are_not_reported(self, repo, inspector):
        touch(repo, ".gitignore", "*.log\n")
        touch(repo, "debug.log", "noise\n")
        snapshot = inspector.capture(repo)
        assert snapshot.changed_files == (".gitignore",)

    def test_fingerprint_changes_with_content(self, repo, inspector):
        touch(repo, TARGET, "one\n")
        first = inspector.capture(repo)
        touch(repo, TARGET, "two\n")
        second = inspector.capture(repo)
        assert first.fingerprint_of(TARGET) != second.fingerprint_of(TARGET)
        assert first.fingerprint_of(TARGET).startswith("worktree:")

    def test_untracked_fingerprint_is_a_blob_id(self, repo, inspector):
        touch(repo, "src/new.py", "x = 1\n")
        snapshot = inspector.capture(repo)
        assert snapshot.fingerprint_of("src/new.py").startswith("blob:")

    def test_snapshot_is_frozen_and_serialisable(self, repo, inspector):
        snapshot = inspector.capture(repo)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.is_clean = False
        payload = snapshot.as_dict()
        assert payload["is_clean"] is True
        assert payload["changed_files"] == []


class TestErrors:
    def test_missing_repository_path_is_rejected(self, tmp_path, inspector):
        with pytest.raises(WorktreeBaselineError, match="not an existing directory"):
            inspector.capture(tmp_path / "missing")

    def test_non_git_directory_is_rejected(self, tmp_path, inspector):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorktreeBaselineError, match="Git command failed"):
            inspector.capture(plain)

    def test_missing_git_executable_is_reported(self, repo):
        def runner(args, cwd, timeout):
            raise FileNotFoundError("git not found")

        inspector = WorktreeBaselineInspector(runner=runner)
        with pytest.raises(WorktreeBaselineError, match="not found"):
            inspector.capture(repo)

    def test_timeout_is_reported(self, repo):
        def runner(args, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

        inspector = WorktreeBaselineInspector(runner=runner)
        with pytest.raises(WorktreeBaselineError, match="timed out"):
            inspector.capture(repo)

    def test_failing_git_command_is_reported(self, repo):
        def runner(args, cwd, timeout):
            return FakeOutcome(returncode=128, stderr="fatal: boom")

        inspector = WorktreeBaselineInspector(runner=runner)
        with pytest.raises(WorktreeBaselineError, match="boom"):
            inspector.capture(repo)

    def test_git_error_output_is_credential_redacted(self, repo):
        def runner(args, cwd, timeout):
            return FakeOutcome(
                returncode=1,
                stderr="fatal: unable to access https://bob:hunter2@example.com/x",
            )

        inspector = WorktreeBaselineInspector(runner=runner)
        with pytest.raises(WorktreeBaselineError) as excinfo:
            inspector.capture(repo)
        assert "hunter2" not in str(excinfo.value)
        assert "***@example.com" in str(excinfo.value)

    def test_runner_is_called_with_argument_arrays(self, repo):
        calls = []

        def runner(args, cwd, timeout):
            calls.append(list(args))
            return FakeOutcome(returncode=0, stdout="")

        inspector = WorktreeBaselineInspector(runner=runner)
        inspector.capture(repo)
        assert calls
        for call in calls:
            assert isinstance(call, list)
            assert "-C" in call
            assert all(isinstance(part, str) for part in call)
        assert any("status" in call for call in calls)


class TestDegradedGitResponses:
    """Read-only capture must tolerate a partially unusable Git response."""

    @staticmethod
    def scripted(status_stdout, *, fail=()):
        def runner(args, cwd, timeout):
            subcommand = args[3] if len(args) > 3 else ""
            if subcommand in fail:
                return FakeOutcome(returncode=1, stderr="")
            if subcommand == "status":
                return FakeOutcome(returncode=0, stdout=status_stdout)
            return FakeOutcome(returncode=0, stdout="")

        return runner

    def test_untracked_file_without_a_usable_hash_is_still_reported(self, repo):
        inspector = WorktreeBaselineInspector(
            runner=self.scripted("?? notes.txt\0", fail=("hash-object",))
        )
        snapshot = inspector.capture(repo)
        assert snapshot.untracked_files == ("notes.txt",)
        assert snapshot.fingerprint_of("notes.txt") is None

    def test_tracked_file_without_a_usable_diff_is_still_reported(self, repo):
        inspector = WorktreeBaselineInspector(
            runner=self.scripted(" M src/app.py\0", fail=("diff",))
        )
        snapshot = inspector.capture(repo)
        assert snapshot.changed_files == ("src/app.py",)
        assert snapshot.fingerprint_of("src/app.py") is None

    def test_malformed_status_entries_are_ignored(self, repo):
        inspector = WorktreeBaselineInspector(runner=self.scripted("M\0"))
        snapshot = inspector.capture(repo)
        assert snapshot.changed_files == ()
        assert snapshot.is_clean is True

    def test_git_failure_without_stderr_names_the_exit_code(self, repo):
        inspector = WorktreeBaselineInspector(
            runner=self.scripted(" M src/app.py\0", fail=("status",))
        )
        with pytest.raises(WorktreeBaselineError, match="exited with code 1"):
            inspector.capture(repo)

    def test_unreadable_head_is_reported_as_none(self, repo):
        inspector = WorktreeBaselineInspector(
            runner=self.scripted("", fail=("rev-parse",))
        )
        snapshot = inspector.capture(repo)
        assert snapshot.head_commit is None


class TestPorcelainParsing:
    def test_rename_keeps_the_original_path(self):
        assert _parse_porcelain_z("R  a2.txt\0a.txt\0") == [("R ", "a2.txt", "a.txt")]

    def test_unstaged_modification(self):
        assert _parse_porcelain_z(" M b.txt\0") == [(" M", "b.txt", None)]

    def test_untracked_entry_with_spaces(self):
        assert _parse_porcelain_z("?? d new.txt\0") == [("??", "d new.txt", None)]

    def test_malformed_short_entries_are_tolerated(self):
        assert _parse_porcelain_z("M\0XY\0") == [("M", "", None), ("XY", "", None)]

    def test_empty_input_yields_no_entries(self):
        assert _parse_porcelain_z("") == []

    def test_entry_without_a_space_separator_is_still_parsed(self):
        assert _parse_porcelain_z("M  src/app.py\0") == [("M ", "src/app.py", None)]


class TestAttribution:
    def test_clean_baseline_and_agent_change_is_attributable(self, repo, inspector):
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "fixed\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert isinstance(attribution, BaselineAttribution)
        assert attribution.clean_baseline is True
        assert attribution.attributable is True
        assert attribution.agent_files == (TARGET,)
        assert attribution.has_agent_changes is True
        assert attribution.pre_existing_files == ()
        assert attribution.blocked_reasons == ()

    def test_no_changes_at_all_is_attributable_and_empty(self, repo, inspector):
        baseline = inspector.capture(repo)
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == ()
        assert attribution.has_agent_changes is False
        assert attribution.attributable is True

    def test_unrelated_agent_change_is_still_attributed(self, repo, inspector):
        baseline = inspector.capture(repo)
        touch(repo, "README.md", "# demo\nagent edit\n")
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == ("README.md",)
        assert attribution.attributable is True

    def test_agent_untracked_file_is_attributed(self, repo, inspector):
        baseline = inspector.capture(repo)
        touch(repo, "src/new.py", "x = 1\n")
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == ("src/new.py",)
        assert attribution.agent_untracked_files == ("src/new.py",)

    def test_agent_deletion_is_attributed(self, repo, inspector):
        baseline = inspector.capture(repo)
        (repo / TARGET).unlink()
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == (TARGET,)
        assert attribution.agent_deleted_files == (TARGET,)
        assert attribution.attributable is True

    def test_agent_rename_is_attributed(self, repo, inspector, git):
        baseline = inspector.capture(repo)
        git(repo, "mv", TARGET, "src/renamed.py")
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == ("src/renamed.py",)
        assert attribution.agent_renamed_files == (("src/renamed.py", TARGET),)

    def test_pre_existing_modified_target_is_not_attributed(self, repo, inspector):
        touch(repo, TARGET, "pre-existing\n")
        baseline = inspector.capture(repo)
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == ()
        assert attribution.pre_existing_files == (TARGET,)
        assert attribution.clean_baseline is False
        assert attribution.attributable is False
        assert any("not clean" in reason for reason in attribution.blocked_reasons)

    def test_pre_existing_unrelated_file_is_never_attributed(self, repo, inspector):
        touch(repo, "README.md", "# demo\npre-existing\n")
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "agent fix\n")
        after = inspector.capture(repo)
        attribution = attribute_changes(baseline, after)
        assert attribution.agent_files == (TARGET,)
        assert attribution.pre_existing_files == ("README.md",)
        assert attribution.clean_baseline is False
        assert attribution.attributable is False


class TestAttributionEdgeCases:
    def test_pre_existing_staged_file_is_never_attributed(self, repo, inspector, git):
        touch(repo, "src/staged.py", "staged = True\n")
        git(repo, "add", "src/staged.py")
        baseline = inspector.capture(repo)
        assert baseline.staged_files == ("src/staged.py",)
        touch(repo, TARGET, "agent fix\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert attribution.agent_files == (TARGET,)
        assert attribution.pre_existing_files == ("src/staged.py",)

    def test_pre_existing_untracked_file_is_never_attributed(self, repo, inspector):
        touch(repo, "notes.txt", "pre-existing\n")
        baseline = inspector.capture(repo)
        assert baseline.untracked_files == ("notes.txt",)
        touch(repo, TARGET, "agent fix\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert attribution.agent_files == (TARGET,)
        assert attribution.pre_existing_files == ("notes.txt",)

    def test_pre_existing_change_touched_again_is_not_attributed(self, repo, inspector):
        touch(repo, TARGET, "pre-existing\n")
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "pre-existing and agent changed\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert attribution.agent_files == ()
        assert attribution.pre_existing_and_changed_files == (TARGET,)
        assert attribution.attributable is False
        assert any(
            "already changed" in reason for reason in attribution.blocked_reasons
        )

    def test_pre_existing_change_reverted_is_not_attributed(self, repo, inspector, git):
        touch(repo, TARGET, "pre-existing\n")
        baseline = inspector.capture(repo)
        git(repo, "checkout", "--", TARGET)
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert attribution.agent_files == ()
        assert attribution.pre_existing_and_changed_files == (TARGET,)
        assert attribution.attributable is False

    def test_agent_change_alongside_untouched_pre_existing_change(
        self, repo, inspector
    ):
        touch(repo, "README.md", "# demo\npre-existing\n")
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "agent fix\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert attribution.agent_files == (TARGET,)
        assert TARGET not in attribution.pre_existing_and_changed_files
        assert attribution.pre_existing_files == ("README.md",)

    def test_different_repositories_are_rejected(self, tmp_path, inspector, repo):
        other = tmp_path / "other"
        other.mkdir()
        snapshot_a = inspector.capture(repo)
        snapshot_b = WorktreeSnapshot(
            repository_path=other,
            head_commit=None,
            is_clean=True,
            changed_files=(),
            tracked_modified_files=(),
            staged_files=(),
            untracked_files=(),
            deleted_files=(),
            renamed_files=(),
            fingerprints={},
        )
        with pytest.raises(WorktreeBaselineError, match="different"):
            attribute_changes(snapshot_a, snapshot_b)

    def test_attribution_is_frozen_and_serialisable(self, repo, inspector):
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "agent fix\n")
        attribution = attribute_changes(baseline, inspector.capture(repo))
        with pytest.raises(dataclasses.FrozenInstanceError):
            attribution.agent_files = ()
        payload = attribution.as_dict()
        assert payload["agent_files"] == [TARGET]
        assert payload["attributable"] is True
        assert payload["has_agent_changes"] is True

    def test_attribution_is_deterministic(self, repo, inspector):
        baseline = inspector.capture(repo)
        touch(repo, TARGET, "agent fix\n")
        after = inspector.capture(repo)
        assert attribute_changes(baseline, after) == attribute_changes(baseline, after)

    def test_blocked_reasons_explain_a_dirty_baseline(self, repo, inspector):
        touch(repo, "README.md", "# demo\npre-existing\n")
        baseline = inspector.capture(repo)
        attribution = attribute_changes(baseline, inspector.capture(repo))
        assert len(attribution.blocked_reasons) == 1
        assert "clean" in attribution.blocked_reasons[0]
        assert attribution.reasons
