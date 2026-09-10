"""T05-T07 tests: repository clone, source-branch checkout, agent branch.

These tests exercise the real system Git against local repositories built
inside ``tmp_path``. Every scenario in the failure table of the spec that
can be reproduced locally is covered here.
"""

import pytest

from repository import RepositoryError, RepositoryManager, redact_credentials

AGENT_NAME = "ai/sonar-fix/AX1abcDeF2"


def _sha(git, repo, ref):
    return git(repo, "rev-parse", ref)


def _local_branches(git, repo):
    """Return local branch names via ``git for-each-ref``."""
    stdout = git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    )
    return [line for line in stdout.splitlines() if line.strip()]


class TestClone:
    def test_clone_happy_path(self, rm, git, make_remote_repo,
                              clone_destination):
        remote = make_remote_repo()
        dest = clone_destination("work")

        cloned = rm.clone(str(remote), dest)

        assert cloned == dest.absolute()
        assert (dest / ".git").is_dir()
        assert (dest / "src" / "app.py").is_file()
        assert (dest / "README.md").is_file()
        assert git(dest, "branch", "--show-current") == "main"
        assert _sha(git, dest, "HEAD") == _sha(git, str(remote), "main")

    def test_clone_into_existing_empty_directory(self, rm, make_remote_repo,
                                                 tmp_path):
        remote = make_remote_repo()
        dest = tmp_path / "existing-empty"
        dest.mkdir()

        rm.clone(str(remote), dest)

        assert (dest / ".git").is_dir()

    def test_empty_url_rejected(self, rm, clone_destination):
        with pytest.raises(RepositoryError, match="must not be empty"):
            rm.clone("   ", clone_destination("unused"))

    def test_non_empty_destination_fails_cleanly(self, rm, make_remote_repo,
                                                 tmp_path):
        remote = make_remote_repo()
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "precious.txt").write_text("keep me", encoding="utf-8")

        with pytest.raises(RepositoryError):
            rm.clone(str(remote), dest)

        assert (dest / "precious.txt").read_text(encoding="utf-8") == "keep me"
        assert not (dest / ".git").exists()

    def test_clone_failure_never_leaks_credentials(self, tmp_path):
        manager = RepositoryManager(timeout_seconds=15.0)
        url = "https://alice:super-sekret-token@127.0.0.1:1/team/repo.git"

        with pytest.raises(RepositoryError) as excinfo:
            manager.clone(url, tmp_path / "out")

        message = str(excinfo.value)
        assert "super-sekret-token" not in message
        assert "alice" not in message

class TestCheckoutSourceBranch:
    def _clone_with_local_develop(self, rm, git, make_remote_repo,
                                  clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("checkout-work")
        rm.clone(str(remote), repo)
        git(repo, "branch", "develop", "origin/develop")
        return repo

    def test_checkout_happy_path(self, rm, git, make_remote_repo,
                                 clone_destination):
        repo = self._clone_with_local_develop(rm, git, make_remote_repo,
                                              clone_destination)

        returned = rm.checkout_source_branch(repo, "develop")

        assert returned == "develop"
        assert rm.current_branch(repo) == "develop"
        assert git(repo, "branch", "--show-current") == "develop"
        assert (repo / "src" / "app.py").is_file()

    def test_missing_branch_raises_and_branch_unchanged(
            self, rm, git, make_remote_repo, clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("no-branch-work")
        rm.clone(str(remote), repo)

        with pytest.raises(RepositoryError, match="does not exist"):
            rm.checkout_source_branch(repo, "does-not-exist")

        assert rm.current_branch(repo) == "main"

    @pytest.mark.parametrize(
        "bad_name", ["", "-nope", "a..b", "HEAD", "bad name"]
    )
    def test_invalid_branch_name_rejected(self, rm, make_remote_repo,
                                          clone_destination, bad_name):
        remote = make_remote_repo()
        repo = clone_destination("invalid-name-work")
        rm.clone(str(remote), repo)

        with pytest.raises(RepositoryError):
            rm.checkout_source_branch(repo, bad_name)

        assert rm.current_branch(repo) == "main"

    def test_non_repository_path_rejected(self, rm, tmp_path):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()

        with pytest.raises(RepositoryError, match="not a Git repository"):
            rm.checkout_source_branch(plain_dir, "main")

    def test_missing_repository_path_rejected(self, rm, tmp_path):
        with pytest.raises(RepositoryError, match="does not exist"):
            rm.checkout_source_branch(tmp_path / "nope", "main")


class TestCreateAgentBranch:
    def test_create_agent_branch_from_verified_source(
            self, rm, git, make_remote_repo, clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("agent-work")
        rm.clone(str(remote), repo)
        source_sha = _sha(git, repo, "refs/heads/main")

        returned = rm.create_agent_branch(repo, AGENT_NAME, "main")

        assert returned == AGENT_NAME
        assert rm.current_branch(repo) == AGENT_NAME
        assert AGENT_NAME in _local_branches(git, repo)
        assert _sha(git, repo, f"refs/heads/{AGENT_NAME}") == source_sha

    def test_collision_aborts_and_leaves_one_branch(
            self, rm, git, make_remote_repo, clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("collision-work")
        rm.clone(str(remote), repo)
        rm.create_agent_branch(repo, AGENT_NAME, "main")
        # Return to the source branch so the only remaining precondition
        # violation is the branch-name collision itself.
        git(repo, "checkout", "main")

        with pytest.raises(RepositoryError, match="already exists"):
            rm.create_agent_branch(repo, AGENT_NAME, "main")

        assert _local_branches(git, repo).count(AGENT_NAME) == 1

    def test_expected_source_branch_must_be_checked_out(
            self, rm, git, make_remote_repo, clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("wrong-source-work")
        rm.clone(str(remote), repo)

        with pytest.raises(RepositoryError, match="expected source"):
            rm.create_agent_branch(repo, AGENT_NAME, "not-checked-out")

        assert rm.current_branch(repo) == "main"
        assert AGENT_NAME not in _local_branches(git, repo)

    @pytest.mark.parametrize("bad_name", ["", "-bad", "a..b", "HEAD"])
    def test_invalid_agent_branch_name_rejected(
            self, rm, make_remote_repo, clone_destination, bad_name):
        remote = make_remote_repo()
        repo = clone_destination("bad-agent-work")
        rm.clone(str(remote), repo)

        with pytest.raises(RepositoryError):
            rm.create_agent_branch(repo, bad_name, "main")

        assert rm.current_branch(repo) == "main"


class TestCurrentBranch:
    def test_detached_head_returns_none(self, rm, git, make_remote_repo,
                                        clone_destination):
        remote = make_remote_repo()
        repo = clone_destination("detached-work")
        rm.clone(str(remote), repo)
        git(repo, "checkout", "--detach")

        assert rm.current_branch(repo) is None

    def test_repository_path_that_is_a_file_rejected(self, rm, tmp_path):
        a_file = tmp_path / "just-a-file.txt"
        a_file.write_text("x", encoding="utf-8")

        with pytest.raises(RepositoryError, match="not a directory"):
            rm.current_branch(a_file)


class TestGitFailureTranslation:
    def test_timeout_yields_clear_repository_error(self, monkeypatch):
        import subprocess

        import repository as repository_module

        def _explode(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=0.0)

        monkeypatch.setattr(repository_module.subprocess, "run", _explode)
        manager = RepositoryManager(timeout_seconds=0.01)

        with pytest.raises(RepositoryError, match="timed out"):
            manager._run_git(["rev-parse", "HEAD"], cwd=None)

    def test_git_ok_survives_executable_failure(self, monkeypatch):
        import repository as repository_module

        def _explode(*args, **kwargs):
            raise OSError("no git here")

        monkeypatch.setattr(repository_module.subprocess, "run", _explode)
        manager = RepositoryManager()

        assert manager._git_ok(["rev-parse", "HEAD"], cwd=None) is False

    def test_missing_executable_yields_clear_error(self, monkeypatch):
        import repository as repository_module

        def _explode(*args, **kwargs):
            raise FileNotFoundError("no such git")

        monkeypatch.setattr(repository_module.subprocess, "run", _explode)
        manager = RepositoryManager()

        with pytest.raises(RepositoryError, match="Failed to run Git"):
            manager._run_git(["status"], cwd=None)


class TestDefensiveVerification:
    def test_checkout_verification_detects_unexpected_branch(
            self, rm, make_remote_repo, clone_destination, monkeypatch):
        remote = make_remote_repo()
        repo = clone_destination("verif-checkout")
        rm.clone(str(remote), repo)
        monkeypatch.setattr(
            rm, "current_branch", lambda _repo: "unexpected"
        )

        with pytest.raises(RepositoryError, match="could not be verified"):
            rm.checkout_source_branch(repo, "main")

    def test_create_verification_detects_unexpected_branch(
            self, rm, make_remote_repo, clone_destination, monkeypatch):
        remote = make_remote_repo()
        repo = clone_destination("verif-create")
        rm.clone(str(remote), repo)
        state = {"calls": 0}

        def fake_current_branch(_repo):
            state["calls"] += 1
            return "main" if state["calls"] == 1 else "unexpected"

        monkeypatch.setattr(rm, "current_branch", fake_current_branch)

        with pytest.raises(RepositoryError, match="could not be verified"):
            rm.create_agent_branch(repo, AGENT_NAME, "main")


class TestRedactCredentials:
    def test_scheme_url_user_and_password_removed(self):
        text = (
            "fatal: unable to access "
            "'https://alice:s3cret@example.com/team/repo.git/': "
            "Connection refused"
        )
        scrubbed = redact_credentials(text)

        assert "s3cret" not in scrubbed
        assert "alice" not in scrubbed
        assert "***@example.com" in scrubbed

    def test_scheme_url_user_only_removed(self):
        scrubbed = redact_credentials(
            "https://robot@example.com/team/repo.git failed"
        )
        assert "robot" not in scrubbed
        assert "***@example.com" in scrubbed

    def test_scp_style_remote_scrubbed(self):
        scrubbed = redact_credentials(
            "Permission denied (publickey) for "
            "'git@github.com:team/repo.git'"
        )
        assert "git@github.com" not in scrubbed

    def test_plain_text_without_credentials_unchanged(self):
        text = "fatal: reference is not a tree: abc123"
        assert redact_credentials(text) == text

    def test_empty_and_none_safe(self):
        assert redact_credentials("") == ""
        assert redact_credentials(None) == ""


