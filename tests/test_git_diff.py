"""T12 tests: read-only Git working-tree inspection.

Uses real local Git repositories under ``tmp_path``; never a real Codex run.
"""

import subprocess
from pathlib import Path

import pytest

from git_diff import GitDiffError, GitDiffInspector, GitDiffResult

_GIT_IDENTITY = ["-c", "user.name=Test User", "-c", "user.email=test@example.com"]


class FakeOutcome:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome if outcome is not None else FakeOutcome()
        self.error = error
        self.calls = []

    def __call__(self, args, cwd, timeout):
        self.calls.append((list(args), str(cwd), float(timeout)))
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture
def repo(make_remote_repo, rm, clone_destination) -> Path:
    """A clean local clone of the standard demo remote."""
    remote = make_remote_repo()
    destination = clone_destination()
    return rm.clone(str(remote), destination)


@pytest.fixture
def inspector() -> GitDiffInspector:
    return GitDiffInspector()


def test_clean_working_tree(repo, inspector):
    result = inspector.inspect(repo)
    assert result.repository_path == repo.resolve()
    assert result.is_clean is True
    assert result.has_changes is False
    assert result.changed_files == ()
    assert result.untracked_files == ()
    assert result.diff_stat == ""
    assert result.diff_text == ""


def test_modified_tracked_file(repo, inspector):
    (repo / "README.md").write_text("# demo\nchanged\n", encoding="utf-8")
    result = inspector.inspect(repo)
    assert result.is_clean is False
    assert result.has_changes is True
    assert result.changed_files == ("README.md",)
    assert result.untracked_files == ()
    assert "README.md" in result.diff_stat
    assert "+changed" in result.diff_text


def test_new_untracked_file_is_reported_without_diff(repo, inspector):
    (repo / "src" / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    result = inspector.inspect(repo)
    assert result.has_changes is True
    assert "src/new_file.py" in result.changed_files
    assert result.untracked_files == ("src/new_file.py",)
    # Untracked files have no diff/stat text (nothing staged or tracked yet).
    assert result.diff_stat == ""
    assert result.diff_text == ""


def test_staged_and_unstaged_changes_combined(repo, inspector, git):
    (repo / "README.md").write_text("# demo\nunstaged change\n", encoding="utf-8")
    (repo / "src" / "added.py").write_text("added = True\n", encoding="utf-8")
    git(repo, "add", "src/added.py")

    result = inspector.inspect(repo)
    assert result.changed_files == ("README.md", "src/added.py")
    assert "unstaged change" in result.diff_text
    assert "added = True" in result.diff_text
    assert "src/added.py" in result.diff_stat


def test_paths_containing_spaces(repo, inspector, git):
    (repo / "docs with spaces").mkdir()
    untracked = repo / "docs with spaces" / "notes file.txt"
    untracked.write_text("hello\n", encoding="utf-8")
    git(repo, "mv", "src/app.py", "src/my app.py")

    result = inspector.inspect(repo)
    assert "src/my app.py" in result.changed_files
    assert "docs with spaces/notes file.txt" in result.changed_files
    assert "docs with spaces/notes file.txt" in result.untracked_files
    assert "src/my app.py" in result.diff_text


def test_empty_diff_after_commit(repo, inspector, git):
    (repo / "README.md").write_text("# demo\ncommitted\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, *_GIT_IDENTITY, "commit", "-m", "chore: readme")
    result = inspector.inspect(repo)
    assert result.is_clean is True
    assert result.diff_text == ""
    assert result.diff_stat == ""


def test_non_git_directory_raises(tmp_path, inspector):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(GitDiffError, match="Git command failed"):
        inspector.inspect(plain)


def test_missing_directory_raises(tmp_path, inspector):
    with pytest.raises(GitDiffError, match="not an existing directory"):
        inspector.inspect(tmp_path / "nope")


def test_missing_git_executable_raises(repo):
    bad = GitDiffInspector(git_command="git-executable-that-does-not-exist-123")
    with pytest.raises(GitDiffError, match="not found"):
        bad.inspect(repo)


def test_timeout_raises_typed_error(repo):
    runner = FakeRunner(error=subprocess.TimeoutExpired(cmd=["git"], timeout=5.0))
    slow = GitDiffInspector(runner=runner)
    with pytest.raises(GitDiffError, match="timed out"):
        slow.inspect(repo)


def test_git_failure_redacts_credentials(repo):
    runner = FakeRunner(
        outcome=FakeOutcome(
            returncode=128,
            stdout="",
            stderr="fatal: unable to access "
            "'https://bob:hunter2@example.com/git/repo.git/': denied",
        )
    )
    failing = GitDiffInspector(runner=runner)
    with pytest.raises(GitDiffError) as excinfo:
        failing.inspect(repo)
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "Git command failed" in message


def test_git_failure_with_empty_stderr_reports_exit_code(repo):
    runner = FakeRunner(
        outcome=FakeOutcome(returncode=128, stdout="", stderr="")
    )
    failing = GitDiffInspector(runner=runner)
    with pytest.raises(GitDiffError, match="git exited with code 128"):
        failing.inspect(repo)


def test_argument_arrays_and_working_directory(repo):
    runner = FakeRunner()
    instrumented = GitDiffInspector(runner=runner)
    instrumented.inspect(repo)
    args, cwd, timeout = runner.calls[0]
    assert all(isinstance(arg, str) for arg in args)
    assert args == ["git", "-C", str(repo.resolve()), "status",
                    "--porcelain", "--untracked-files=all"]
    assert cwd == str(repo.resolve())
    assert timeout == 300.0


def test_inspector_result_is_typed_frozen(repo, inspector):
    result = inspector.inspect(repo)
    assert isinstance(result, GitDiffResult)
