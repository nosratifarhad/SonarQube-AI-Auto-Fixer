"""T08 tests: ``IssueContext`` preparation, path safety, file/line checks."""

import os

import pytest

from context import ContextError, IssueContextPreparer, safe_relative_path
from models import SonarIssue

AGENT = "ai/sonar-fix/AX1abcDeF2"
SRC = "src/app.py"


def make_issue(
    component="demo:" + SRC,
    line=3,
    key="AX1abcDeF2",
):
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


@pytest.fixture
def agent_repo(rm, git, make_remote_repo, clone_destination):
    """A clone of the demo repo sitting on the agent branch."""
    remote = make_remote_repo()
    repo = clone_destination("context-work")
    rm.clone(str(remote), repo)
    rm.create_agent_branch(repo, AGENT, "main")
    return repo


@pytest.fixture
def preparer(rm):
    return IssueContextPreparer(rm)


class TestSafeRelativePath:
    def test_plain_relative_path_is_kept(self, tmp_path):
        relative = safe_relative_path(tmp_path, "src/app.py")
        assert relative.as_posix() == "src/app.py"

    def test_dot_segments_are_collapsed(self, tmp_path):
        assert safe_relative_path(tmp_path, "./src/./app.py").as_posix() == \
            "src/app.py"

    def test_backslashes_are_normalized(self, tmp_path):
        assert safe_relative_path(tmp_path, "src\\app.py").as_posix() == \
            "src/app.py"

    def test_empty_path_rejected(self, tmp_path):
        for value in ["", "   "]:
            with pytest.raises(ContextError, match="no usable file path"):
                safe_relative_path(tmp_path, value)

    def test_absolute_paths_rejected(self, tmp_path):
        for value in ["/etc/passwd", "C:/Windows/win.ini", "C:\\Windows\\x"]:
            with pytest.raises(ContextError, match="Absolute"):
                safe_relative_path(tmp_path, value)

    def test_traversal_rejected(self, tmp_path):
        for value in ["../secret.py", "src/../../secret.py",
                      "..\\..\\secret.py"]:
            with pytest.raises(ContextError, match="traverse"):
                safe_relative_path(tmp_path, value)

    def test_root_only_path_rejected(self, tmp_path):
        for value in [".", "./", "src/.."]:
            with pytest.raises(ContextError):
                safe_relative_path(tmp_path, value)

    def test_symlink_escape_rejected(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable on this platform: {exc}")

        with pytest.raises(ContextError, match="outside the repository"):
            safe_relative_path(repo, "escape/secret.txt")


class TestPrepareHappyPaths:
    def test_full_context_is_built_and_verified(self, agent_repo, preparer):
        ctx = preparer.prepare(
            issue=make_issue(),
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        assert ctx.issue.key == "AX1abcDeF2"
        assert ctx.repository_path == agent_repo.resolve()
        assert ctx.source_branch == "main"
        assert ctx.agent_branch == AGENT
        assert ctx.file_path == "src/app.py"
        assert ctx.absolute_file_path == (agent_repo / "src" / "app.py").resolve()
        assert ctx.line == 3
        assert ctx.has_file is True

    def test_context_without_line_keeps_none(self, agent_repo, preparer):
        ctx = preparer.prepare(
            issue=make_issue(line=None),
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        assert ctx.line is None

    def test_windows_style_separators_in_component(self, agent_repo,
                                                   preparer):
        issue = make_issue(component="demo:src\\app.py")
        ctx = preparer.prepare(
            issue=issue,
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        assert ctx.file_path == "src/app.py"
        assert ctx.has_file is True

    def test_as_dict_is_secret_free_and_summarises(self, agent_repo,
                                                   preparer):
        ctx = preparer.prepare(
            issue=make_issue(),
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        summary = ctx.as_dict()

        assert summary == {
            "issue_key": "AX1abcDeF2",
            "rule": "python:S108",
            "severity": "MAJOR",
            "source_branch": "main",
            "agent_branch": AGENT,
            "file_path": "src/app.py",
            "line": 3,
        }

    def test_binary_file_skips_line_bounds_check(self, agent_repo,
                                                 preparer):
        (agent_repo / "blob.bin").write_bytes(b"\x00\x01\x02\nrest")

        ctx = preparer.prepare(
            issue=make_issue(component="demo:blob.bin", line=999999),
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        assert ctx.has_file is True
        assert ctx.line == 999999

    def test_non_utf8_file_skips_line_bounds_check(self, agent_repo,
                                                   preparer):
        (agent_repo / "latin.txt").write_bytes(b"caf\xe9\n\xff line")

        ctx = preparer.prepare(
            issue=make_issue(component="demo:latin.txt", line=999999),
            repository_path=agent_repo,
            source_branch="main",
            agent_branch=AGENT,
        )

        assert ctx.line == 999999


class TestPrepareFailurePaths:
    def test_missing_file_raises(self, agent_repo, preparer):
        with pytest.raises(ContextError, match="does not exist in repository"):
            preparer.prepare(
                issue=make_issue(component="demo:src/nope.py"),
                repository_path=agent_repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    def test_empty_component_raises(self, agent_repo, preparer):
        with pytest.raises(ContextError, match="no usable file path"):
            preparer.prepare(
                issue=make_issue(component="demo:"),
                repository_path=agent_repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    def test_traversal_attempt_raises(self, agent_repo, preparer):
        with pytest.raises(ContextError, match="traverse"):
            preparer.prepare(
                issue=make_issue(component="demo:../secret.py"),
                repository_path=agent_repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    def test_line_beyond_file_end_raises(self, agent_repo, preparer):
        with pytest.raises(ContextError, match="beyond the end of file"):
            preparer.prepare(
                issue=make_issue(line=9999),
                repository_path=agent_repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    @pytest.mark.parametrize("bad_line", [0, -3])
    def test_non_positive_line_raises(self, agent_repo, preparer, bad_line):
        with pytest.raises(ContextError, match="positive"):
            preparer.prepare(
                issue=make_issue(line=bad_line),
                repository_path=agent_repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    def test_wrong_checked_out_branch_raises(self, rm, git,
                                             make_remote_repo,
                                             clone_destination, preparer):
        remote = make_remote_repo()
        repo = clone_destination("wrong-branch-context")
        rm.clone(str(remote), repo)  # sits on main, not AGENT

        with pytest.raises(ContextError, match="[Ee]xpected agent branch"):
            preparer.prepare(
                issue=make_issue(),
                repository_path=repo,
                source_branch="main",
                agent_branch=AGENT,
            )

    def test_nonexistent_repository_path_raises(self, tmp_path, preparer):
        with pytest.raises(ContextError, match="does not exist"):
            preparer.prepare(
                issue=make_issue(),
                repository_path=tmp_path / "ghost",
                source_branch="main",
                agent_branch=AGENT,
            )


class TestIssueContextHelpers:
    def test_has_file_false_for_missing_path(self, tmp_path):
        from context import IssueContext

        ctx = IssueContext(
            issue=make_issue(),
            repository_path=tmp_path,
            source_branch="main",
            agent_branch=AGENT,
            file_path="missing.py",
            absolute_file_path=tmp_path / "missing.py",
            line=None,
        )

        assert ctx.has_file is False

    def test_count_text_lines_returns_none_when_read_fails(self, tmp_path):
        directory = tmp_path / "a-directory"
        directory.mkdir()

        assert IssueContextPreparer._count_text_lines(directory) is None

