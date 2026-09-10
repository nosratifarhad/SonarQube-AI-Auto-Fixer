"""Shared fixtures for the T04-T19 test suite.

All Git work happens in ``tmp_path`` directories against real local Git
repositories. No network access, no SonarQube server, no real Codex CLI, and no
real project toolchain (T14 tests inject a process runner, T16 injects an
analysis runner, T17 injects a clock/sleeper and a fake CE client, and T18 uses a
fake issues client) are used.
"""

import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from repository import RepositoryManager

_GIT_IDENTITY = ["-c", "user.name=Test User", "-c", "user.email=test@example.com"]


def git_run(cwd: Path, *args: str, check: bool = True) -> str:
    """Run ``git`` inside ``cwd`` and return trimmed stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)!r} failed in {cwd}:\n{result.stderr}"
        )
    return result.stdout.strip()


def write_default_files(repo_root: Path) -> None:
    """Write the standard POC demo files (20-line Python file, README)."""
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 21)) + "\n",
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text("# demo\n", encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _git_available() -> None:
    """Skip the whole suite when the system Git is not usable."""
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"System git is not available: {exc}")


@pytest.fixture
def rm() -> RepositoryManager:
    """A fresh :class:`RepositoryManager` for each test."""
    return RepositoryManager(timeout_seconds=60.0)


@pytest.fixture
def git():
    """Runs ``git`` inside a directory: ``git(cwd, *args) -> stdout``."""
    return git_run


@pytest.fixture
def make_remote_repo(tmp_path: Path):
    """Build a bare remote repository with ``main`` and ``develop``."""

    def _make(
        *, branches: Tuple[str, ...] = ("main", "develop")
    ) -> Path:
        seed = tmp_path / "seed"
        seed.mkdir()
        write_default_files(seed)
        git_run(seed, "init", "-b", "main")
        git_run(seed, *_GIT_IDENTITY, "commit", "--allow-empty", "-m",
               "chore: empty history root")
        git_run(seed, "add", ".")
        git_run(seed, "commit", "-m", "feat: demo files")
        for branch in branches:
            if branch != "main":
                git_run(seed, "branch", branch)
        remote = tmp_path / "remote.git"
        git_run(seed, "clone", "--bare", str(seed), str(remote))
        return remote

    return _make


@pytest.fixture
def clone_destination(tmp_path: Path):
    """Pre-create a destination directory so clones land under tmp_path."""

    def _destination(name: str = "work") -> Path:
        dest = tmp_path / name
        dest.mkdir()
        return dest

    return _destination
