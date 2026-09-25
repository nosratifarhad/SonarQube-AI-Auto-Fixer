# SonarQube AI Auto-Fixer

A fail-closed pipeline that turns SonarQube issues into reviewed, tested,
AI-generated fixes in a local clone of a repository.

The tool connects to SonarQube, selects the issues worth fixing, prepares an
isolated working copy, asks an AI coding agent (Codex CLI) to fix **exactly one
issue**, then verifies the result against the project's own tests **and a new
SonarQube analysis** before anything is committed. Every irreversible step is
guarded by a standalone policy and is opt-in.

> **Scope note.** This repository grew as a sequence of independently tested
> "task" units (T01–T29). The composition layer that wires them into an
> end-to-end run lives in the `pipeline/` package (T30) and is invoked through
> `main.py --run`. Nothing is committed or pushed unless explicitly enabled.

## What it does

```
SonarQube -> issue selection -> clone/branch -> issue context
    -> Codex prompt -> Codex execution -> result analysis
    -> working-tree diff -> change scope -> project tests
    -> analysis trigger -> analysis wait -> issue verification
    -> final status -> (branch/uncertainty policy) -> commit -> push
    -> per-issue report -> overall report
```

A fix is only accepted (`FIXED`) when **all** of the following hold:

* Codex executed successfully,
* only the issue's own file changed (change scope),
* the project tests passed,
* the re-analysis completed successfully and is attributable to this run
  (analysis correlation),
* the original issue is **reliably absent** from the correlated post-analysis
  snapshot.

A successful analysis alone is never treated as a fix.

## Safety model

* **Policy modules are pure.** `issue_limit`, `iteration_limit`,
  `branch_protection`, `uncertainty_policy`, `sonar_rule_allowlist`,
  `logging_policy` and the reporting layers perform no I/O; they only decide.
* **Fail closed.** Missing, ambiguous or contradictory evidence is never read as
  permission.
* **Deterministic and secret-free.** Commit messages, reports and logs are
  derived from validated values only; credentials are redacted and never
  published.
* **Two Git writes, at most.** T20 performs exactly one exact-path `git add`
  and one `git commit`; T21 performs exactly one `git push`. Both are behind
  allow-lists and deny-lists and are enabled only with `--commit` / `--push`.

## Requirements

* Python 3.10+
* `pip install -r requirements.txt`
* A local `git` executable and, for a real run, the SonarScanner (or any
  configured analysis command) and the Codex CLI.

## Configuration

Start from the checked-in template and fill in your own values:

```bash
cp .env.example .env
```

The T01 connection settings live in `.env` next to the code:

```dotenv
SONAR_URL=https://sonar.example.com
SONAR_TOKEN=your-token
PROJECT_KEY=your-project
```

The pipeline adds `FIXER_*` settings. Commands accept a JSON array
(preferred: `["pytest", "-q"]`) or a whitespace-separated string.

```dotenv
FIXER_REPOSITORY_URL=https://git.example.com/team/repo.git
FIXER_SOURCE_BRANCH=main
FIXER_DEFAULT_BRANCH=main
FIXER_REMOTE_NAME=origin
# FIXER_DESTINATION_BRANCH=          # defaults to the agent branch
FIXER_WORK_DIR=.auto-fixer-work
FIXER_TEST_COMMAND=["pytest", "-q"]
FIXER_ANALYSIS_COMMAND=["sonar-scanner"]
FIXER_CODEX_COMMAND=codex
FIXER_CODEX_ARGS=["exec"]
FIXER_MAX_ISSUES=3                   # required by T24; unconfigured is refused
FIXER_MAX_ITERATIONS=1               # required by T25; unconfigured is refused
FIXER_ALLOWED_RULES=python:S1481,python:S1192
FIXER_PROTECTED_BRANCHES=main,master,develop,trunk
FIXER_EXCLUSIVE_ANALYSIS=true        # single-analysis precondition for T18
FIXER_COMMIT_FIXES=false
FIXER_PUSH_FIXES=false
```

`FIXER_MAX_ISSUES` and `FIXER_MAX_ITERATIONS` must be set to usable positive
integers before a run: an unconfigured limit is refused, never treated as
"unlimited". `FIXER_EXCLUSIVE_ANALYSIS=true` may only be declared when nothing
else analyses the project during the run (see the analysis-correlation note
below).

## Usage

```bash
# Read-only: select and print the issues (T01-T04).
python main.py

# Run the full pipeline read-only (no commit, no push).
python main.py --run

# Allow a verified fix to be committed (T20) but not pushed.
python main.py --run --commit

# Allow a verified fix to be committed and pushed (T20 + T21).
# --push does not imply --commit: a push only ever follows a real commit.
python main.py --run --commit --push

# Machine-readable result.
python main.py --run --json
```

The commit/push flags only enable the steps; the branch-protection,
uncertainty, change-scope, secret-scan and commit/push gates still decide
whether anything is actually written.

## Analysis correlation (important)

SonarQube's `/api/issues/search` cannot be filtered by analysis id, so the
post-analysis issue snapshot can only be attributed to this run under the
**single-analysis precondition**. That is an environmental guarantee, declared
with `FIXER_EXCLUSIVE_ANALYSIS=true`; without it, a verified absence stays
`REVIEW_REQUIRED` rather than becoming `FIXED`.

## Roadmap: future integrations & authentication

> **This is a plan, not a feature list.** Nothing below is implemented. The
> current release uses token-based SonarQube authentication only. Full detail,
> evidence, and prerequisites are in the specification:
> [`docs/sonarqube-ai-fixer-spec.md`](docs/sonarqube-ai-fixer-spec.md) **section 23**.

| Capability | Today | Future | Status |
|------------|-------|--------|--------|
| SonarQube token auth | Supported (HTTP Basic `token:`) | Keep as default | Current |
| SonarQube scanner auth | `SONAR_HOST_URL` + `SONAR_TOKEN` | Keep unchanged | Current |
| SonarQube username/password | Not supported | Considered | Planned, needs verification |
| GitLab integration (any) | None (generic Git remote only) | Considered | Planned, needs verification |
| GitLab username/password | Not supported | Decision point | Needs verification |
| SonarQube MCP | Not supported | Considered | Planned, not implemented |
| GitLab MCP | Not supported | Candidate | Needs verification |

**Why these are future items.** They are useful only if the target deployments
support them. The specification marks each as `Current`, `Planned`,
`Candidate`, or `Needs verification` so nothing is presented as working when it
is not.

**What does NOT change.** Token authentication stays the default, the scanner
keeps using its own token/environment mechanism, the CLI and its safety gates
are unchanged, and secret redaction is unchanged.

**Before any implementation, these must be verified** against the real
environments: the SonarQube version (MCP requires Server 2025.1+ / Community
Build 25.1+), the token type (a User token is required for MCP), the
SSO/SAML/MFA posture, and the target GitLab version/edition plus its supported
automation authentication method.

**MCP snapshot (verified).** The official `sonarsource/sonarqube-mcp` server
requires a User token and a recent SonarQube version, supports Stdio and
Streamable HTTP, offers a read-only mode, and can be configured for the Codex
CLI. It would sit *alongside* the existing REST client, never replace it.

## Tests

```bash
.venv\Scripts\python -m pytest -q --cov=. --cov-report=term-missing
```

The suite never needs a live SonarQube server, a real Codex CLI or a real
project toolchain: every I/O boundary is injected.

## Documentation

The full domain and behaviour specification is in
[`docs/sonarqube-ai-fixer-spec.md`](docs/sonarqube-ai-fixer-spec.md).
