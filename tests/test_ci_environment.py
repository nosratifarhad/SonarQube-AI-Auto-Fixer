"""Regression tests for CI-environment hermeticity.

These guard two real CI failures that unit behaviour did not catch:

1. The shared test repository was built with commits that relied on an ambient
   global/system Git identity. GitHub-hosted runners have none, so
   ``git commit`` failed with ``fatal: empty ident name ... not allowed``.
2. The real-repository safety test inspects ``HEAD^``, which a shallow
   ``actions/checkout`` does not contain.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_make_remote_repo_is_hermetic_without_a_global_identity(
    tmp_path, monkeypatch, make_remote_repo, git
):
    """The fixture must build its commits without any ambient Git identity."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)

    remote = make_remote_repo()

    # The empty-history root plus the demo-files commit.
    assert git(remote, "rev-list", "--count", "main") == "2"
    # The commits were authored by the fixture's explicit identity, not by
    # whatever ambient identity the machine happens to provide.
    assert (
        git(remote, "log", "-1", "--format=%an <%ae>", "main")
        == "Test User <test@example.com>"
    )


def test_ci_workflow_checks_out_full_history():
    """CI must not use a shallow checkout: a test reads the repo's ``HEAD^``."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "fetch-depth: 0" in workflow
