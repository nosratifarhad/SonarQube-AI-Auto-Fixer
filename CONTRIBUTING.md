# Contributing to SonarQube AI Auto-Fixer

Thanks for your interest in the project. This guide is written so that a
first-time contributor can open a good Pull Request without asking the
maintainer basic questions.

If anything here is unclear, that is a documentation bug - please open an
issue or a PR to improve it.

---

## 1. The contribution workflow (the short version)

```
Fork  ->  create a branch  ->  make changes  ->  run tests  ->  open a Pull Request  ->  CI + review  ->  merge
```

`main` is protected. **You never push directly to `main`.** All changes reach
`main` through a Pull Request (PR).

---

## 2. Prerequisites

| Tool | Why | Check |
|------|-----|-------|
| Python 3.10+ | The application and tests | `python --version` |
| pip | Installing dependencies | `python -m pip --version` |
| Git | Clone/branch/commit | `git --version` |

Optional, only for a real end-to-end run (not needed for normal contributions):
the Codex CLI and the SonarScanner (or any configured analysis command).

---

## 3. Fork and clone

1. Click **Fork** on the GitHub page of the repository.
2. Clone **your fork** and add the original repository as `upstream`:

```bash
git clone https://github.com/<your-username>/SonarQube-AI-Auto-Fixer.git
cd SonarQube-AI-Auto-Fixer
git remote add upstream https://github.com/nosratifarhad/SonarQube-AI-Auto-Fixer.git
```

3. Keep your fork up to date before starting new work:

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
```

---

## 4. Local setup

Create a virtual environment and install the development requirements.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements.txt` is the runtime dependency set; `requirements-dev.txt`
includes it plus the test tooling.

### Environment configuration

The application reads configuration from a local `.env` file (and from real
environment variables, which take precedence). Never commit `.env`.

```bash
cp .env.example .env
```

Then edit `.env` and fill in your own values. The token variables must contain
**your** values; the example file contains placeholders only.

> **Never commit real credentials.** `.env` is git-ignored on purpose.

---

## 5. Run the project

The default invocation is **read-only** and safe:

```bash
python main.py          # connect, select issues, print them
```

Running the full pipeline (also read-only by default):

```bash
python main.py --run
```

See the [README](README.md) for the full configuration reference and the
`--commit` / `--push` opt-in flags.

---

## 6. Run the tests (required before a PR)

```bash
python -m pytest -q
```

With coverage:

```bash
python -m pytest -q --cov=. --cov-report=term-missing
```

The test suite never needs a live SonarQube server, a real Codex CLI, or a real
project toolchain - every I/O boundary is injected - so it runs anywhere.

---

## 7. Check for secrets before you push

This project handles tokens. Before opening a PR, make sure you did not commit
one:

```bash
git status
git diff --cached
```

* `.env` must never be staged.
* Do not paste tokens into code, tests, docs, issues, or PR descriptions.
* Use obvious placeholders such as `REPLACE_WITH_YOUR_TOKEN`.

---

## 8. Create a branch

Always branch off the latest `main`:

```bash
git fetch upstream
git switch -c <type>/<short-description> upstream/main
```

**Branch naming**

| Type | Use for | Example |
|------|---------|---------|
| `feat/` | New capability | `feat/add-rule-allowlist` |
| `fix/` | Bug fix | `fix/redact-in-error-message` |
| `docs/` | Documentation only | `docs/improve-readme` |
| `test/` | Tests only | `test/cover-timeout-path` |
| `chore/` | Tooling/maintenance | `chore/add-ci` |

Keep it short and lowercase, with dashes.

---

## 9. Commit expectations

* Small, focused commits - one logical change each.
* Write messages in the imperative mood, following the existing style:
  * `feat: add GitLab auth mode`
  * `fix: handle empty Sonar response`
  * `docs: clarify configuration`
  * `test: cover blocked-attempt path`
  * `chore: update dependencies`
* Keep the subject short (about 50 characters or fewer).
* Do not mix unrelated changes in one PR.

---

## 10. Open a Pull Request

Push your branch and open a PR against `nosratifarhad/SonarQube-AI-Auto-Fixer`
`main`:

```bash
git push -u origin <type>/<short-description>
```

The PR template will prompt you for what changed, why, how it was tested, and
the security impact.

---

## 11. What CI checks

Every PR runs GitHub Actions (see `.github/workflows/ci.yml`) on Linux and
Windows across supported Python versions. CI must be green before merge. The
checks are public and deliberately need **no secrets**, so they work for forks.

If CI fails, read the log, fix locally, and push again to the same branch.

---

## 12. Test expectations

* Bug fix -> add a test that fails before the fix and passes after it.
* New behaviour -> add tests for the success path **and** an important failure
  path.
* Refactor -> existing tests must keep passing unchanged.
* Do not lower coverage by deleting or weakening tests.

The project values fail-closed behaviour: when in doubt, a new path should
produce a clear "blocked"/error result rather than a silent success.

---

## 13. Documentation expectations

* If you change behaviour, update the [README](README.md) and, when relevant,
  the specification in [`docs/`](docs/).
* Keep language simple and scannable: short sections, tables, and examples.
* Do not document features that do not exist. Mark planned work as
  *not implemented*.

---

## 14. What maintainers look for

* The change solves the stated problem and nothing unrelated.
* Tests cover the new behaviour and important failure cases.
* No secrets, no credentials, no committed `.env`.
* Backward compatibility is preserved unless the change explicitly requires
  otherwise.
* Documentation matches the implementation.
* The PR is reasonably small and easy to review.

---

## 15. Common mistakes

| Mistake | What to do instead |
|---------|--------------------|
| Committing `.env` or a token | Keep secrets out of Git; rotate anything exposed |
| Branching from an old `main` | `git fetch upstream` and branch from `upstream/main` |
| One giant PR | Split into focused PRs |
| No tests for a behaviour change | Add tests for success and failure paths |
| Unrelated reformatting | Keep the diff focused |
| Force-pushing a shared branch | Only force-push your own fork branch, and only if necessary |
| Editing generated caches (`__pycache__`, `.pytest_cache`) | They are git-ignored; leave them out |

---

## 16. Reporting a bug or proposing a feature

Use the issue templates:

* **Bug report** - describe what happened and how to reproduce it.
* **Feature request** - describe the problem and your proposed solution.

For anything security-sensitive, **do not** open a public issue. Follow
[SECURITY.md](SECURITY.md).

---

## 17. Code of Conduct

By participating you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Be kind and constructive.

---

## 18. License

This project does not yet declare an open-source license (a maintainer decision
is pending). Until a license is added, contributions cannot be legally
redistributed by others - see the "License" section of the
[README](README.md). If you need this clarified before contributing, please
open an issue.
