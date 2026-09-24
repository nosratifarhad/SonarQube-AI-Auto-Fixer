# SonarQube AI Auto-Fixer — Domain & Behaviour Specification (T04–T30)

Status: **POC implementation complete (T01–T30)** · T20 (safe, fail-closed,
exactly-one-commit Git commit), T21 (safe, fail-closed, exactly-one-push Git
push of a T20 `COMMITTED` result), T22/T23 (the overall and per-issue reports),
T24/T25 (the max-issue / max-iteration policy limits), T26 (the standalone
main/default branch-protection policy, §18), T27 (the standalone fail-closed
uncertainty policy, §19), T28 (the standalone exact-match Sonar rule
allowlist, §20) and T29 (the standalone bounded logging policy, §21) are
implemented. **T30 (§22) wires them together**: the `pipeline/` package is the
orchestration layer, and `main.py --run` invokes it. The T20 commit and T21
push remain opt-in (`--commit` / `--push`) and every irreversible step is still
gated by T26/T27 · No
real SonarQube credentials and no real push are ever made by this tool (every
T21 push targets a bare repository under `tmp_path`) · Tests never invoke a real
Codex CLI, never need a real project toolchain, never need a live SonarQube
server, and never touch the real repository (every T20/T21 test uses a real
repository under `tmp_path`). The T30 tests inject every I/O boundary, so the
whole pipeline is exercised without Git, SonarQube, Codex or a toolchain.

> **Reading the "unwired" statements in §12–§21.** They describe the library
> modules *in isolation*, and they remain true as a layering guarantee: no
> root-level library module imports another one, and `main.py` never imports a
> policy or mutation module directly. Since T30 (§22) the `pipeline/` package is
> the **only** consumer that composes them; where an older section says "no
> caller", read it as "no caller inside the library and none from `main.py`".
> The composition, and the mutation gates it drives, are documented in §22.

## 1. Purpose

The SonarQube AI Auto-Fixer prepares everything an AI coding agent needs to
fix one SonarQube issue in a **local clone** of the analysed repository:

1. selects SonarQube issues worth fixing (T03),
2. normalizes them into an internal model (T04),
3. clones the repository and checks out the source branch (T05–T06),
4. creates an isolated agent branch (T07),
5. builds a verified "issue context" (file + line + branches) (T08),
6. builds a deterministic single-issue instruction prompt for Codex (T09),
7. executes Codex as an external CLI process and captures its typed result
   (T10),
8. interprets the execution outcome only (T11),
9. inspects the resulting Git working-tree diff (T12),
10. validates that the diff stays inside the single issue's change scope (T13),
11. runs the project's own tests through a configured, safe command (T14),
12. classifies the test outcome for later orchestration (T15),
13. triggers a SonarQube re-analysis of the modified working copy (T16),
14. waits for that specific analysis to finish, identified by its compute-engine
    task id (T17),
15. retrieves the new open issues, verifies the **original** issue against them
    and records whether that snapshot is attributable to the waited-on analysis
    (T18), and
16. classifies the attempt as `FIXED`, `STILL_OPEN`, `ANALYSIS_FAILED`,
    `TESTS_FAILED`, `SCOPE_INVALID`, `CODEX_FAILED` or `REVIEW_REQUIRED` (T19).

T20–T28 (commits §11, pushes §12, reporting §13–§14, the two policy limits
§15–§16, branch protection §18, the uncertainty policy §19 and the Sonar rule
allowlist §20) are implemented
and documented below. PR
creation, retry/iteration **loops**, orchestration, branch/rule *enforcement*
and container/CI wiring remain **out of scope** for this document and for the
codebase — T26 decides whether a branch is safe to mutate, T27 decides whether
the evidence authorises the mutation and T28 decides whether the rule may be
fixed by an AI agent at all, but nothing enforces any of those decisions yet,
and none of them touches Git. T16–T19 deliberately
stop at *verifying and classifying* the attempt: nothing is committed, nothing
is pushed, nothing is retried, no SonarQube issue is resolved or closed, and no
source code is modified by these stages.

## 2. Scope of behaviour covered

| ID | Behaviour | Module | Covered here |
|----|-----------|--------|--------------|
| T01 | Configuration from environment / `.env` | `config.py` | pre-existing |
| T02 | SonarQube HTTP client (token auth, normalized paging) | `sonar_client.py` | pre-existing |
| T03 | Issue filtering rules | `issue_filter.py` | pre-existing |
| T04 | `SonarIssue` internal model + conversion | `models.py`, `issue_filter.py` | ✔ (verified/refined) |
| T05 | Repository clone via system Git | `repository.py` | ✔ |
| T06 | Source-branch checkout + verification | `repository.py` | ✔ |
| T07 | AI-agent branch creation + collision handling | `repository.py`, `branch_naming.py` | ✔ |
| T08 | `IssueContext` preparation (safe paths, file/line checks) | `context.py` | ✔ |
| T09 | Deterministic single-issue Codex prompt (untrusted message handling) | `codex_prompt.py` | ✔ |
| T10 | Codex execution: external CLI, argument arrays, typed result | `codex_executor.py` | ✔ |
| T11 | Codex result interpretation (execution outcome only, not "is it fixed") | `codex_result.py` | ✔ |
| T12 | Git working-tree diff inspection (read-only, no policy enforcement) | `git_diff.py` | ✔ |
| T13 | Change-scope validation for one issue (target file only; never judges the fix) | `change_scope.py` | ✔ |
| T14 | Project-test execution: configured command, argument arrays, typed result | `test_runner.py` | ✔ |
| T15 | Project-test outcome classification (no retry, no Codex re-invocation) | `test_result.py` | ✔ |
| T16 | Trigger SonarQube re-analysis: configured scanner command, argument arrays, typed trigger result | `sonar_analysis.py` | ✔ |
| T17 | Wait for analysis completion: CE-task polling, injectable clock, bounded timeout | `sonar_analysis_waiter.py` | ✔ |
| T18 | Retrieve/inspect the new open issues and verify the original issue's identity | `sonar_issue_verification.py` | ✔ |
| T18 | Analysis correlation (T17 → T18): is the issue snapshot attributable to the waited-on analysis? | `analysis_correlation.py` | ✔ |
| T19 | Final issue status decision (pure precedence over T11/T13/T15/T16/T17/T18) | `issue_status.py` | ✔ |
| — | Pre-T20 safety primitive: clean-baseline capture + change attribution | `worktree_baseline.py` | ✔ (primitive only, no commit) |
| — | Pre-T20 safety primitive: secret scan of a future commit's content | `secret_scan.py` | ✔ (primitive only, no commit) |
| T20 | Safe, fail-closed, exactly-one-commit Git commit of a verified fix | `commit_message.py`, `commit_policy.py`, `git_commit.py` | ✔ |
| T21 | Safe, fail-closed, exactly-one-push Git push of a T20 `COMMITTED` result | `push_policy.py`, `git_push.py` | ✔ |
| T22–T27 | Reporting (§13–§14), the two policy limits (§15–§16), branch protection (§18) and the uncertainty policy (§19) | `overall_report.py`, `per_issue_report.py`, `issue_limit.py`, `iteration_limit.py`, `branch_protection.py`, `uncertainty_policy.py` | ✔ (wired by T30) |
| T28 | Sonar rule allowlist policy (exact match, fail-closed, §20) | `sonar_rule_allowlist.py` | ✔ (wired by T30) |
| T29 | Bounded logging policy (§21) | `logging_policy.py` | ✔ (wired by T30) |
| T30 | End-to-end orchestration: configuration, policy-bound logging, lifecycle wiring, commit/push opt-in | `pipeline/` | ✔ |
| T31+ | PRs, retry loops, CI, containers | — | ✖ not implemented |

## 3. Actors and system context

```
                ┌─────────────────────────────┐
  operator ───▶ │  main.py / orchestration    │
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  sonar_client.py (HTTP)     │  ──▶ SonarQube (token auth)
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  issue_filter.py / models   │  pure logic, no I/O
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  repository.py (git, fork)  │  ──▶ local clone on disk
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  context.py (IssueContext)  │  read-only path/branch checks
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  codex_prompt.py (T09)      │  deterministic prompt text
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  codex_executor.py (T10)    │  ──▶ external Codex CLI
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  codex_result.py (T11)      │  pure interpretation
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  git_diff.py (T12)          │  read-only `git status/diff`
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  change_scope.py (T13)      │  pure scope policy, no I/O
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  test_runner.py (T14)       │  ──▶ project test command
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  test_result.py (T15)       │  pure interpretation, no retry
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  sonar_analysis.py (T16)    │  ──▶ configured scanner command
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  sonar_analysis_waiter (T17) │  ──▶ /api/ce/task (poll)
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  sonar_issue_verification   │  ──▶ /api/issues/search (read-only)
                │  (T18)                      │
                └─────────────┬───────────────┘
                ┌─────────────▼───────────────┐
                │  issue_status.py (T19)      │  pure decision, no I/O, no retry
                └─────────────────────────────┘
```

Both new SonarQube touch points (T17's `/api/ce/task` read and T18's
`/api/issues/search` read) go through the existing `sonar_client.py`, which
remains the only module that talks to SonarQube over HTTP; T16 talks to the
project's scanner through `subprocess` only. **No new endpoint is introduced for
correlation**: `/api/issues/search` is a project-scoped read with no
analysis-id filter, so `analysis_correlation.py` turns that limitation into an
explicit, fail-closed precondition instead of an invented selector (§4.13).


## 4. Domain model

### 4.1 `SonarIssue` (T04)

A SonarQube-version-neutral representation of one issue.

| Field | Meaning |
|-------|---------|
| `key` | SonarQube issue key |
| `rule` | rule id, e.g. `python:S108` |
| `severity` | one of `BLOCKER/CRITICAL/MAJOR/MINOR/INFO` |
| `issue_type` | `BUG`, `CODE_SMELL`, `VULNERABILITY`, `SECURITY_HOTSPOT`, … |
| `message` | human-readable description |
| `component` | raw SonarQube component, e.g. `my-project:src/a.py` (never mutated) |
| `line` | `Optional[int]` — `None` means the issue has no line |
| `status` | issue lifecycle status |

Derived: `file_path` strips the `<project key>:` prefix from `component`
via a single `split(":", 1)`; if there is no `:`, the component is returned
unchanged. Conversion tolerates missing/non-numeric `line` (→ `None`).
Conversion is pure (no HTTP), and the rules from T03 are applied before
conversion, unchanged.

### 4.2 `IssueContext` (T08)

| Field | Meaning |
|-------|---------|
| `issue` | the normalized `SonarIssue` |
| `repository_path` | absolute path of the local clone |
| `source_branch` | branch the issue was found on (verified in T06) |
| `agent_branch` | isolated fix branch (created in T07, verified checked out) |
| `file_path` | repository-relative POSIX path |
| `absolute_file_path` | resolved absolute path of the target file |
| `line` | `Optional[int]` target line (`None` = whole file) |

`has_file` reports whether the file exists on disk. `as_dict()` returns a
secret-free summary suitable for logging.

### 4.3 `CodexResult` + `CodexResultAnalysis` (T10/T11)

`CodexResult` (from `codex_executor.py`) is the typed, secret-free record of
one Codex process run: `status` (one of `success`, `failed`, `timeout`,
`executable-not-found`), `exit_code` (`None` when the process never ran to
completion), redacted `stdout`/`stderr`, the exact `command` argument array,
and an `error` detail. Convenience flags: `timed_out`,
`executable_not_found`, `process_failed`; `as_dict()` is logging-safe.

`CodexResultAnalysis` (from `codex_result.py`, T11) is a pure interpretation
of that record. It answers **only** "did the process run and does its output
suggest the attempt failed or is uncertain?" via `execution_succeeded`,
`process_failed`, `timed_out`, `executable_not_found`, `exit_code`,
`output_suggests_uncertainty`, `matched_markers`, `needs_review`, and a
`reason` string. Uncertainty detection is a deterministic, best-effort marker
heuristic over the captured text (see `UNCERTAINTY_MARKERS`).

T11 never decides whether the SonarQube issue is actually fixed; that belongs
to the re-analysis stages (T16–T19), which are not implemented.

### 4.4 `GitDiffResult` (T12)

`GitDiffResult` (from `git_diff.py`) is a read-only snapshot of a working
tree: `repository_path`, `is_clean`, `has_changes`, `changed_files`
(tracked modifications + untracked new files, sorted), `untracked_files`,
`diff_stat`, and `diff_text`. File lists are NUL-separated (`-z`) so paths
with spaces or newlines survive verbatim. It describes *what changed*, not
whether the change is acceptable — that judgement starts with T13.

### 4.5 `ChangeScopeResult` (T13)

`ChangeScopeResult` (from `change_scope.py`) is the outcome of validating the
T12 diff against one issue's allowed scope: `is_valid`, `expected_files`
(repository-relative POSIX paths that may change), `changed_files` (normalized,
de-duplicated, sorted), `unexpected_files` (changed entries outside the scope),
`has_changes`, `expected_file_modified`, and `reasons` with `reason` /
`reason_text` / `as_dict()` helpers. Frozen and JSON-friendly.

`ChangeScopeValidator.validate(context, diff_result)` is pure policy: the
default rule is that **only** `context.file_path` may change. It never decides
whether the change is correct (that needs T16+), never runs Git or tests, and
never writes anything. A diff from a different repository, or an issue
`file_path` that is not a safe repository-relative path, raises
`ChangeScopeError`; scope violations themselves are reported in the result.

### 4.6 `TestResult` (T14)

`TestResult` (from `test_runner.py`) is the typed, credential-redacted record of
one project-test run: `status` (`PASSED`, `FAILED`, `TIMEOUT`, `NOT_FOUND`,
`EXECUTION_ERROR`), `exit_code` (`None` when the process never completed),
`stdout`, `stderr`, the exact `command` argument array, and an `error` detail.
Convenience flags: `passed`, `failed`, `timed_out`, `executable_not_found`,
`execution_error`; `as_dict()` is logging-safe (no raw output).

`ProjectTestRunner(timeout_seconds=..., runner=...)` runs an explicitly
configured command inside the repository directory using argument arrays (never
`shell=True`). The command is always supplied by configuration — never derived
from SonarQube issue text or Codex output. The process runner is injectable, so
the test suite needs no real project toolchain. Caller errors (missing
repository directory, empty/malformed command) raise `TestRunnerError` before
any process launch.

### 4.7 `TestOutcome` (T15)

`TestOutcome` (from `test_result.py`) is the pure classification of one
`TestResult`: `passed`, `failed`, `timed_out`, `executable_not_found`,
`execution_error`, `exit_code`, `needs_review`, `reason`, and `blocking_reason`
(`None` only for a clean pass), plus pass-through `stdout`/`stderr`.
`analyze_test_result(result)` is deterministic and side-effect free: it performs
**no retry**, re-invokes **no Codex**, and runs **no** process. It only describes
what happened, so later orchestration (out of scope) can decide what to do.

### 4.8 `AnalysisConfig` + `SonarAnalysisTriggerResult` (T16)

`AnalysisConfig` (frozen, from `sonar_analysis.py`) describes the analysis
mechanism: `command` (argument array, default `("sonar-scanner",)`),
`timeout_seconds`, `environment` (extra child env, e.g. `SONAR_HOST_URL` /
`SONAR_TOKEN`), `secrets` (literal values scrubbed from captured output), and
`task_id_patterns` (regexes used to extract the compute-engine task id).

`SonarAnalysisTriggerResult` records one trigger attempt: `status`
(`TRIGGERED`, `FAILED`, `TIMEOUT`, `NOT_FOUND`, `EXECUTION_ERROR`), `exit_code`
(`None` when the process never completed), redacted `stdout`/`stderr`, the exact
`command`, the extracted `task_id` (or `None`), and an `error` detail. Flags:
`triggered`, `timed_out`, `executable_not_found`, `execution_error`,
`has_task_id`; `as_dict()` is logging-safe and never contains the environment.

`SonarAnalysisTrigger(config).trigger(repository_path, config=None)` runs the
configured command inside the clone with `subprocess` argument arrays (never
`shell=True`) and returns the typed result. A `TRIGGERED` status means **only**
that the analysis was successfully requested — never that it finished, and never
that the issue is gone. Credentials travel through the environment
(`build_sonar_environment`), never as command-line arguments.

`SonarAnalysisTriggerResult.evidence()` projects a trigger result onto the
frozen `AnalysisTriggerEvidence`: the trigger outcome plus the compute-engine
task id, and nothing else. That compact record — never the captured output, the
argv or the exit code — is what T19 carries forward in
`IssueStatusResult.trigger_evidence` (§4.11) so T20 can cross-check it (§11.2,
G11). The projection exists precisely so the T16 evidence cannot be dropped,
duplicated wholesale or leaked on the way.

### 4.9 `SonarAnalysisCompletion` (T17)

`SonarAnalysisCompletion` (frozen, from `sonar_analysis_waiter.py`) records
waiting for one analysis: `status` (`SUCCESS`, `FAILED`, `CANCELED`, `TIMEOUT`,
`UNKNOWN`), `task_id`, `elapsed_seconds`, `poll_count`, `analysis_id`,
`component_key`, `reason`, and `failure_reason` (`None` only on `SUCCESS`).
Flags: `succeeded`, `terminal`; `as_dict()` is logging-safe. `task_id`,
`analysis_id` and `component_key` are the metadata that §4.13 consumes to decide
whether the T18 issue snapshot is attributable to *this* analysis.

`SonarAnalysisWaiter(client).wait(task_id, task_id_resolver=None)` polls
`client.get_ce_task(task_id)` (`/api/ce/task`, added to `sonar_client.py`)
until the task reaches a terminal state. The clock and sleeper are injectable,
the loop is bounded both by the deadline and by a maximum poll count, a bounded
number of consecutive API errors is tolerated, and no task id means `UNKNOWN`
(the waiter never falls back to "is the project green?").

### 4.10 `SonarIssueVerificationResult` (T18)

`SonarIssueVerificationResult` (frozen, from `sonar_issue_verification.py`)
compares the original issue with the issues SonarQube reports after the new
analysis: `original_issue`, `original_issue_key`, `retrieval_succeeded`,
`current_issues`, `matches`, `matching_current_issue`, `match_type`
(`KEY`, `COMPONENT_RULE_LINE`, `COMPONENT_RULE`, `NONE`), `is_present`,
`identity_reliable`, `page_complete`, `retrieved_count`, `reported_total`,
`skipped_entries`, `correlation` + `correlation_reason` (§4.13), `reason`,
`reasons`, and `error`. Flags: `absent`, `analysis_correlated`,
`reliable_absence`, `match_keys`, `reason_text`; `as_dict()` omits issue
messages.

`SonarIssueVerifier(client, apply_issue_filter=False, project_key=None,
exclusive_analysis=False).verify(original_issue, completion=None)` retrieves the
project's open issues once (read-only, reusing the T02 client and T04 model),
records the analysis-correlation verdict for `completion` (the T17 record) and
applies the identity ladder below. It never resolves, closes, re-opens, or
fabricates an issue, and it never re-runs the analysis. Omitting `completion`
makes the correlation `UNKNOWN`, which fails closed.

### 4.11 `IssueStatusResult` (T19)

`IssueStatusResult` (frozen, from `issue_status.py`) is the final decision for
one attempt: `status` (`IssueFinalStatus`), `decisive_stage`, `reason`,
`reasons`, plus the inputs it used (`codex`, `scope`, `tests`, `analysis`,
`verification`, `trigger_evidence`). Flags: `is_fixed`, `needs_review`,
`blocking_reason` (`None` only for `FIXED`), `reason_text`; `as_dict()` nests the
(already sanitized) sub-summaries.

`trigger_evidence` is the compact T16 record (§4.8) — or `None` when no T16
result was supplied — and it is carried on **every** verdict, not only `FIXED`.
It is what makes the T16 → T19 → T20 chain provable: T20 reads it and refuses
(G11) when the evidence is absent, unusable or contradicts the T17 completion.

`determine_issue_status(codex=…, scope=…, tests=…, analysis=…, verification=…,
trigger=None)` is a pure, deterministic function of those typed results — no
HTTP, no subprocess, no files, no retry, no Codex re-invocation, no commit, no
push, and no branch change. `trigger` stays optional for *classification*: the
T17-only rows of §4.14 continue to work, and requiring the T16 evidence is T20's
decision at the irreversible step.

### 4.12 Issue identity strategy (T18) and its limitations

The original issue from T08 stays the reference point. Matching uses the
strongest available identity, in this order:

| Tier | `match_type` | Used when | Reliability |
|------|--------------|-----------|-------------|
| 1 | `KEY` | the original issue has a usable key | reliable — SonarQube keys are stable while an issue is open |
| 2 | `COMPONENT_RULE_LINE` | no usable key, or the key disappeared while the same rule at the same normalized file + line is still open | reliable only for a unique match on a complete page; the "key disappeared" case is always reported as ambiguity |
| 3 | `COMPONENT_RULE` | the line is unknown or has moved | reliable only for a unique match on a complete page |
| — | `NONE` | nothing matched | never reliable when there was no key at all |

Rules that prevent a false "fixed":

* an unrelated issue that merely shares the `message` or the `rule` is **not**
  the original issue (message is never used as identity);
* the original key disappearing while an equivalent issue is still open is
  reported as `is_present=True` with `identity_reliable=False`
  (`REVIEW_REQUIRED`, not `FIXED`);
* an absence is only trustworthy on a **provably complete** page
  (`page_complete`), and only when the tier allows it;
* without any usable key, an absence can never be reported as reliable;
* an absence additionally requires the analysis-correlation verdict to be
  `CORRELATED` (§4.13): `reliable_absence` is `True` only when the retrieval
  succeeded, nothing matched, the identity is reliable, the page is complete
  **and** the snapshot is correlated.

#### Page completeness (fail closed)

`page_complete` requires positive evidence: `total` must be a real integer
(booleans are rejected), must not be negative, and must not exceed the number of
issue entries returned. A missing, `null`, non-numeric, malformed, negative or
larger-than-retrieved `total` therefore yields `page_complete=False`. An
incomplete/unknown page can never establish a reliable absence and can therefore
never lead to `FIXED`. (`reported_total` records the integer total when there
was one, else `None`.)

Documented limitations:

* Component comparison uses the T04 `file_path` (project-key prefix stripped)
  plus `os.path.normcase`, so it follows the host platform's case rules; on
  Windows a case-only difference therefore compares equal.
* SonarQube can re-key an issue; tier 1 sees that as "absent" and tier 2/3
  therefore produce `REVIEW_REQUIRED` instead of an assumption either way.
* Only a single page of open issues is considered, so any page that is not
  provably complete downgrades a potential absence to `REVIEW_REQUIRED`.
* The retrieval uses the project's *open* issues only (the T02 query); a
  "resolved/closed" issue is simply no longer open.
* The retrieval cannot be filtered by analysis: it is a project-scoped read, so
  attribution relies on the correlation precondition in §4.13.

### 4.13 Analysis correlation (T17 → T18) and its limitation

T17 waits for the exact compute-engine (CE) task that T16 triggered, so it knows
*which* analysis finished. T18 then reads the open issues project-scoped
(`componentKeys` + `resolved=false`), and **`/api/issues/search` has no
parameter that pins the result to one analysis**. Inventing such a selector is
forbidden, so the limitation is turned into an explicit, enforceable
precondition instead of a silent assumption.

`analysis_correlation.correlate_analysis(completion, project_key=…,
exclusive_analysis=False)` returns one of three verdicts:

| Verdict | Meaning |
|---------|---------|
| `CORRELATED` | The analysis reached `SUCCESS`, the CE task reported both its task id and its `analysisId`, the analysed `componentKey` equals the component whose issues are read, **and** the run declared the single-analysis precondition |
| `NOT_CORRELATED` | Correlation was positively disproved (the analysis did not succeed, or the CE task belongs to another component) |
| `UNKNOWN` | Correlation could not be established: no completion, no task id, no `analysisId`, no configured component, or the single-analysis precondition was not declared |

The **single-analysis precondition** is the POC's substitute for an
analysis-id filter: because the issue read cannot be pinned to one analysis, the
snapshot is only attributable when nothing else analyses that component during
the run. That is an environmental guarantee, so it must be *declared*
(`exclusive_analysis=True`); the default is `False`, so an undeclared deployment
fails closed into `UNKNOWN`.

`UNKNOWN` is never treated as success. T18 stores the verdict on the
verification result (`correlation`, `correlation_reason`, `analysis_correlated`)
and T19 refuses `FIXED` unless the verdict is `CORRELATED`.

### 4.14 Final-status precedence (T19)

| Order | Status | Decisive stage | Meaning |
|-------|--------|----------------|---------|
| 1 | `CODEX_FAILED` | `codex_execution` | Codex did not execute successfully |
| 2 | `SCOPE_INVALID` | `change_scope` | files outside the issue's scope changed |
| 3 | `TESTS_FAILED` | `project_tests` | the project tests did not pass |
| 4 | `ANALYSIS_FAILED` | `sonar_analysis` | the analysis was not triggered, its CE task could not be identified, the completion disagreed with the trigger, or the analysis failed / was canceled / did not finish in time |
| 5 | `REVIEW_REQUIRED` | `sonar_analysis` / `sonar_verification` / `fix_attribution` | indeterminate analysis state, failed retrieval, **uncorrelated snapshot**, ambiguous identity, unreliable absence, or no change to the issue's file |
| 6 | `STILL_OPEN` | `sonar_verification` | the original issue is reliably still open |
| 7 | `FIXED` | `fix_attribution` | every precondition passed and the original issue is reliably absent |

Deterministic analysis-stage translation (T16 → T17 → T19). The mapping is
implemented explicitly by `issue_status.resolve_analysis_stage` and
`issue_status.ANALYSIS_STAGE_STATUS`, never as ad-hoc branches:

| T16 trigger | T17 completion | Analysis stage | Final status |
|-------------|----------------|----------------|--------------|
| not `TRIGGERED` | (any) | `TRIGGER_FAILED` | `ANALYSIS_FAILED` |
| `TRIGGERED` without a task id | (any) | `ANALYSIS_UNIDENTIFIED` | `ANALYSIS_FAILED` |
| `TRIGGERED` with a task id | task id disagrees | `ANALYSIS_IDENTITY_MISMATCH` | `ANALYSIS_FAILED` |
| `TRIGGERED` with a task id | `FAILED` / `CANCELED` / `TIMEOUT` | `ANALYSIS_FAILED` | `ANALYSIS_FAILED` |
| `TRIGGERED` with a task id | `UNKNOWN` | `ANALYSIS_INCOMPLETE` | `REVIEW_REQUIRED` |
| `TRIGGERED` with a task id | `SUCCESS` | `COMPLETED` | continue to T18 |
| not supplied (`None`) | `SUCCESS` | `COMPLETED` | continue to T18 |
| not supplied (`None`) | `FAILED` / `CANCELED` / `TIMEOUT` | `ANALYSIS_FAILED` | `ANALYSIS_FAILED` |
| not supplied (`None`) | `UNKNOWN` | `ANALYSIS_INCOMPLETE` | `REVIEW_REQUIRED` |

Supplying the T16 result is strictly stricter; the `None` rows exist because a
`SUCCESS` completion is itself proof that a real compute-engine task was polled
to a successful end.

Deterministic verification translation (T18 → T19), implemented by
`issue_status.resolve_verification_outcome` and `VERIFICATION_OUTCOME_STATUS`:

| Verification outcome | Condition | Final status |
|----------------------|-----------|--------------|
| `NOT_RETRIEVED` | the open issues could not be retrieved | `REVIEW_REQUIRED` |
| `STILL_OPEN` | a matching issue is open and the identity is reliable | `STILL_OPEN` |
| `PRESENCE_AMBIGUOUS` | an equivalent issue is open but unconfirmed | `REVIEW_REQUIRED` |
| `NOT_CORRELATED` | the issue is absent but `correlation is not CORRELATED` | `REVIEW_REQUIRED` |
| `ABSENCE_UNRELIABLE` | absent, correlated, but the identity match is not reliable | `REVIEW_REQUIRED` |
| `ABSENT_VERIFIED` | absent, correlated, reliable, complete page | continue to attribution |

A still-open issue is reported as `STILL_OPEN` regardless of the correlation
verdict: presence is a positive finding that can never produce `FIXED`, and
hiding it behind `REVIEW_REQUIRED` would lose information. Correlation is
required to attribute an *absence*, which is the only claim that can yield a
fix.

`FIXED` acceptance criteria (all mandatory): Codex executed successfully, scope
validation passed, project tests passed, the SonarQube analysis completed
successfully (with a matching T16 task id), the issue's own file changed, the
snapshot is **correlated** (`CORRELATED`), and the original issue is reliably
absent from the new SonarQube result.

A `FIXED` verdict is a *claim*, not permission to commit. It also carries the
T16 evidence in `trigger_evidence`, and T20 (§11) re-checks that evidence and
fails closed when it is absent, unusable or contradictory. When no T16 result
was supplied, T19 can still reach `FIXED` from T17 evidence alone (the last
three rows of the table above), but `trigger_evidence` is then `None` and T20
refuses: that path is a *classification* path, never a *commit* path.

`REVIEW_REQUIRED` is used whenever any required piece of evidence is missing or
ambiguous. T19 never retries, never re-invokes Codex, never starts another
attempt, and never changes the prompt or branch (iteration limits are T25).

## 5. Behaviours (flows)

### B1 — Clone repository (T05)

1. Validate the repository URL is non-empty.
2. Run `git clone -- <url> <destination>` as an argument array
   (no `shell=True`, no secrets in command lines).
3. If Git fails, wrap stdout/stderr in `RepositoryError` **after redacting
   URL credentials** (`https://user:pass@host/...` and SCP-style
   `user@host:...` shapes are scrubbed to `***@`).

Rules: the parent directory must exist; cloning into an existing **empty**
directory succeeds; cloning into a non-empty directory fails with a clear
error (nothing is deleted or overwritten).

### B2 — Check out source branch (T06)

1. Validate the branch name (`branch_naming.validate_branch_name`).
2. Verify the repository exists and contains `.git`.
3. Verify a **local** branch `refs/heads/<name>` exists; otherwise fail.
4. Run `git checkout <name>`.
5. Re-read the current branch with `git branch --show-current` and fail if
   it is not exactly `<name>`.

Remote-tracking names (`origin/main`) are not auto-created; the tool never
silently checks out a different branch than requested.

### B3 — Create agent branch (T07)

1. `branch_naming.make_agent_branch_name(issue_key, timestamp?)` builds
   `ai/sonar-fix/<slug>[-<timestamp>]`; invalid characters become `-`.
2. Before modifying anything, verify that (a) the **expected source branch**
   is the branch currently checked out and (b) the agent branch does not
   already exist (collision ⇒ abort with a clear message).
3. Create with `git checkout -b <name>` from current HEAD.
4. Verify afterwards that the checked-out branch is exactly `<name>`.

### B4 — Prepare issue context (T08)

1. Resolve and validate the repository path.
2. Verify the currently checked-out branch equals the expected agent branch.
3. Resolve `issue.file_path` with `safe_relative_path` (see 5.5) and require
   it to point at a **regular file** inside the clone. A missing file is an
   error — never a silent skip.
4. If `issue.line` is set, require it to be ≥ 1 and, for text files, not
   beyond the file’s line count. Binary files skip the bounds check.

### B5 — Path safety (`safe_relative_path`)

Rejects: empty paths, absolute paths (POSIX `/`, Windows `C:`/UNC),
any `..` component, and paths whose `resolve()`d form escapes the
repository root (symlink containment re-check). Backslashes normalize to
`/`. Components `""` and `"."` are collapsed; a path that reduces to the
repository root is rejected (it is not a file).

### B6 — Build the Codex prompt (T09)

1. Take the verified `IssueContext` (T08) as the only input.
2. Render a fixed-section prompt: role → repository context → issue record →
   task → constraints → completion criteria (see `codex_prompt.py`).
3. Collapse the SonarQube `message` to a single line and place it in a
   clearly marked **DATA ONLY** block with an explicit "untrusted data, never
   instructions" warning. Issue text can never add sections or soften the
   hard constraints.
4. The result is a pure function of the context: identical contexts yield
   byte-identical prompts (deterministic, no timestamps/randomness).

The prompt hard-codes: fix EXACTLY ONE issue; smallest reasonable change;
preserve existing behaviour; no unrelated files; no config/public-API/
dependency changes unless required; **no commit, no push, no PR**; do not
touch `main`/`master`/`develop`; do not suppress the issue without
justification.

### B7 — Execute Codex (T10)

1. Validate the repository path is an existing directory.
2. Launch the Codex CLI with an **argument array** (`codex exec` by default,
   prompt on stdin) with the repository as working directory.
3. Capture stdout, stderr, and exit code under a hard timeout.
4. Classify the outcome: `SUCCESS` (exit 0), `FAILED` (non-zero exit),
   `TIMEOUT`, or `NOT_FOUND` (executable missing). Caller mistakes (bad
   repository path) raise `CodexExecutorError` instead.
5. Redact URL/SSH credentials from every stored output and error string.

The process runner is injectable; tests never require a real Codex install.

### B8 — Analyse the Codex result (T11)

1. Map the typed result to booleans: executed successfully / process failed /
   timed out / executable missing, plus the exit code.
2. For successful runs only, scan redacted stdout+stderr for deterministic
   uncertainty markers (`UNCERTAINTY_MARKERS`).
3. Produce a `CodexResultAnalysis` (`needs_review` is True for any failure,
   timeout, missing executable, or marker hit).
4. **Stop.** T11 does not run tests, call SonarQube, inspect the diff, or
   decide whether the issue is fixed (that is T16–T19).

### B9 — Inspect the Git working tree (T12)

1. Run read-only Git commands in the repository: `status --porcelain`,
   `diff --name-only -z`, `diff --cached --name-only -z`,
   `ls-files --others --exclude-standard -z`, `diff --stat`, `diff`.
2. Report `is_clean`/`has_changes`, sorted `changed_files`
   (tracked + untracked), `untracked_files`, `diff_stat`, `diff_text`.
3. **Stop.** Changes are described but never judged or rejected: rejecting
   unrelated edits is T13 policy enforcement and is intentionally absent.

Git commands use argument arrays (`git -C <repo> ...`), never `shell=True`;
Git failures are wrapped in `GitDiffError` after credential redaction.

### B10 — Validate the change scope (T13)

1. Take the T08 `IssueContext` and the T12 `GitDiffResult` as the only inputs.
2. Normalize every reported changed path to a repository-relative POSIX path;
   absolute, empty, root-level, and `..`-traversal entries are unusable and are
   reported as unexpected (they can never be in scope).
3. Allow **only** the single issue's target file (`context.file_path`) to
   change; every other changed file (tracked or untracked, in any number) is
   listed in `unexpected_files` and makes `is_valid` False. Nothing is silently
   ignored.
4. Report `has_changes` and `expected_file_modified` so callers can distinguish
   "nothing changed" from "the target changed inside scope".
5. **Stop.** No fix-correctness judgement, no test run, no commit, no push.

Deterministic and pure: identical inputs yield an identical result. A diff from
a different repository, or an unsafe context `file_path`, raises
`ChangeScopeError`.

### B11 — Run the project tests (T14)

1. Validate that the repository path is an existing directory.
2. Validate the configured test command (non-empty argument array, non-empty
   string elements, first element names an executable) — the command is never
   built from issue text, Codex output, or a shell string.
3. Launch it with `subprocess` in argument-array form, repository as working
   directory, under a configurable timeout, capturing stdout/stderr/exit code.
4. Classify: `PASSED` (exit 0), `FAILED` (non-zero exit), `TIMEOUT`,
   `NOT_FOUND` (executable missing), `EXECUTION_ERROR` (any other launch
   failure). Execution failures are returned as typed results, not raised.
5. Redact credentials from all captured output and error strings.
6. **Stop.** The runner only runs the configured command: it never installs
   dependencies, edits source, retries, commits, pushes, or calls Codex.

### B12 — Classify the test outcome (T15)

1. Map the typed `TestResult` to booleans plus the exit code.
2. Preserve redacted `stdout`/`stderr` on the outcome for later reporting.
3. Set `needs_review` and `blocking_reason` for anything that is not a clean
   pass; `blocking_reason` is `None` only when the tests passed.
4. **Stop.** T15 does not retry the tests, does not invoke Codex again, and does
   not decide whether the issue is fixed (that is T16+).

### B13 — Trigger the SonarQube re-analysis (T16)

1. Validate the repository path is an existing directory and the configured
   analysis command is a non-empty argument array naming an executable (the
   command is configuration only — never derived from issue text or Codex
   output).
2. Build the child environment from the current environment plus the configured
   entries (`SONAR_HOST_URL`, `SONAR_TOKEN`, …); credentials are never placed on
   the command line.
3. Launch the command with `subprocess` in argument-array form inside the clone,
   under a configurable timeout, capturing stdout/stderr/exit code.
4. Classify: `TRIGGERED` (exit 0 — the analysis was *requested*), `FAILED`
   (non-zero exit), `TIMEOUT`, `NOT_FOUND`, `EXECUTION_ERROR`.
5. Scrub configured secrets and URL credentials from the captured output, then
   extract the compute-engine task id from the scanner's report-processing URL
   (validating it as untrusted data). If no task id can be extracted, `task_id`
   is `None` — T17 must not guess.
6. **Stop.** A `TRIGGERED` result never means the analysis finished and never
   means the issue is fixed. No waiting, no polling, no retry, no commit.

### B14 — Wait for the analysis to finish (T17)

1. Resolve the task id: use T16's id if it is a safe id; otherwise consult the
   optional injected resolver; otherwise stop as `UNKNOWN` immediately (never
   "is the project green?").
2. Poll `/api/ce/task?id=<task id>` through the existing client:
   * `SUCCESS` → terminal success (`succeeded`). Completion only — the issue is
     not judged here.
   * `FAILED` / `CANCELED` → terminal failure with the engine's message
     (redacted, capped).
   * `PENDING` / `IN_PROGRESS` → sleep the polling interval and continue.
   * uninterpretable payload → `UNKNOWN` (stop; do not spin).
   * task not found (HTTP 404) → `UNKNOWN`.
   * API/network error → tolerated for a bounded number of consecutive errors,
     then `UNKNOWN`.
3. Stop when the deadline (`timeout_seconds`) or the maximum poll count is
   reached → `TIMEOUT`; report `elapsed_seconds` and `poll_count`.
4. **Stop.** No re-triggering, no re-analysis, no issue resolution, no commit.

### B15 — Verify the original issue against the new analysis (T18)

1. Retrieve the project's open issues once through the existing client
   (read-only; no state is changed).
2. Record the **analysis-correlation verdict** for the T17 completion that is
   handed in (`verify(..., completion=...)`): `CORRELATED`,
   `NOT_CORRELATED` or `UNKNOWN` (see §4.13). Omitting the completion yields
   `UNKNOWN`, which fails closed.
3. Reject an unusable response (non-object payload, missing/non-list `issues`)
   as `retrieval_succeeded=False` with `identity_reliable=False`; skip and count
   unusable entries.
4. Apply the identity ladder to the **original** issue:
   1. **Key** — the original key is still open → present, reliable identity.
      Key gone → an equivalent issue (same rule + normalized file + line, or
      same rule + normalized file) still open means ambiguity (`is_present=True`,
      `identity_reliable=False`); otherwise absent, reliable only when the
      retrieved page was provably complete.
   2. **Rule + normalized file + line** — fallback when the original issue has
      no usable key: exactly one candidate → present and reliable (page
      complete); several candidates → ambiguous.
   3. **Rule + normalized file** — last resort when the line is unknown/moved.
5. Record `page_complete` fail closed: an absence is only trustworthy when
   `total` was an integer, non-negative and not larger than the retrieved entry
   count. Keep the T03 filters **off by default** so a still-open issue whose
   severity changed can never be filtered away and look fixed.
6. **Stop.** No issue is resolved, closed, re-opened, or fabricated; the
   decision itself is T19.

### B16 — Determine the final status (T19)

1. Translate the analysis stage explicitly (`resolve_analysis_stage`):
   a failed T16 trigger, a missing task id, a task-id mismatch, or a
   failed/canceled/timed-out/unknown T17 completion can never be `COMPLETED`.
   See the table in §4.14 for every combination.
2. Translate the verification stage explicitly (`resolve_verification_outcome`):
   `NOT_RETRIEVED`, `NOT_CORRELATED`, `STILL_OPEN`, `PRESENCE_AMBIGUOUS`,
   `ABSENCE_UNRELIABLE` or `ABSENT_VERIFIED`.
3. Evaluate the stages in the documented precedence, each one short-circuiting:
   1. `CODEX_FAILED`
   2. `SCOPE_INVALID`
   3. `TESTS_FAILED`
   4. `ANALYSIS_FAILED` (trigger/unidentified/mismatch/definitive failure) /
      `REVIEW_REQUIRED` (indeterminate analysis state)
   5. `REVIEW_REQUIRED` (retrieval failed, snapshot not correlated, identity
      ambiguous, absence unreliable, or nothing changed in the issue's file)
   6. `STILL_OPEN`
   7. `FIXED`
4. Grant `FIXED` only when **all** of the following hold: Codex executed
   successfully, scope validation passed, the project tests passed, the
   SonarQube analysis completed successfully (with a matching T16 task id), the
   analysis is `CORRELATED`, the issue's own file changed, and the original
   issue is reliably absent from the new SonarQube result. The T16 evidence is
   carried out of this stage as `trigger_evidence` (or `None`, which T20 refuses).
5. **Stop.** T19 only classifies the current attempt: no retry, no second
   attempt, no prompt change, no new branch, no commit, no push.

> **Successful analysis completion does not imply that the original SonarQube
> issue is fixed.**
>
> **The issue is `FIXED` only when the original issue is reliably verified as
> absent from a *correlated* analysis and all required preconditions have
> passed.**

### B17 — Commit the verified fix (T20)

1. Input: the T19 `IssueStatusResult` (which must be `FIXED`), the pre-run and
   post-run `WorktreeSnapshot`s taken with `include_ignored=True`, the
   `BaselineAttribution` of this run, and the configured secret values.
2. Evaluate `G1`; derive the commit message deterministically from the validated
   rule key, the approved path and the issue key (`commit_message`).
3. Inspect the environment (S2, before any Git process), resolve the repository
   identity (S1), and check the branch, the explicit identity and the repository
   state (S3-S5). Enforce `G29-G37` **here**, before anything is resolved or
   staged.
4. Verify the baseline and the whole-tree attribution (S6, `G2-G18`), resolve the
   approved file set exactly once (S7, `G19-G24`), and scan the approved content
   and the worktree diff (S8, `G25-G27`).
5. Stage exactly the approved paths (`git add -- <paths>`, S9), verify the index
   (`G38-G40`), scan the staged diff (`G28`), and re-verify the staged and
   worktree content immediately before committing (`G41-G42`, S12).
6. Attempt exactly one `git commit -m <message>` (S13) and prove the result
   (S14, `G43-G45`): one new commit, the expected parent, message, identity,
   paths, blob ids, branch and worktree root, and a clean approved path set.
7. **Stop.** One commit, or a refusal/failure result - never a push, never a
   fetch, never a reset, never a retry, never a second commit.

## 6. Failure scenarios

| # | Scenario | Expected behaviour |
|---|----------|--------------------|
| F01 | Repository URL empty | `RepositoryError: Repository URL must not be empty.` |
| F02 | Clone destination parent missing / Git network/auth failure | `RepositoryError` with redacted Git stderr |
| F03 | Clone into non-empty directory | `RepositoryError` (Git refuses; nothing deleted) |
| F04 | `git` executable missing | `RepositoryError` naming the executable, exit code path never reached |
| F05 | Repository path missing / not a Git repo | `RepositoryError` |
| F06 | Source branch does not exist locally | `RepositoryError: Branch ... does not exist ...` (before any `git checkout`) |
| F07 | Source branch name invalid (`-x`, `a..b`, ``, `HEAD`, …) | `RepositoryError` from name validation |
| F08 | Checkout exits non-zero (e.g. local changes conflict) | `RepositoryError`; verification is skipped |
| F09 | Agent branch name already exists | `RepositoryError` (collision aborts; branch NOT reused) |
| F10 | Expected source branch not actually checked out | `RepositoryError` before any modification |
| F11 | Issue `file_path` empty | `ContextError` |
| F12 | `file_path` absolute / Windows drive / contains `..` | `ContextError` (traversal attempt rejected) |
| F13 | `file_path` resolves outside root via symlink | `ContextError` |
| F14 | File missing in clone | `ContextError` (documented; suggests re-clone to match analysis revision) |
| F15 | Issue line negative / zero | `ContextError` |
| F16 | Issue line beyond file end (text file) | `ContextError` |
| F17 | Agent branch not checked out when preparing context | `ContextError` |
| F18 | `codex` executable not installed | `CodexResult.status == NOT_FOUND`, no exception escapes |
| F19 | Codex run exceeds timeout | `CodexResult.status == TIMEOUT`, `exit_code` None |
| F20 | Codex exits non-zero | `CodexResult.status == FAILED` with exit code; stdout/stderr kept (redacted) |
| F21 | Codex output says "unable to fix / uncertain …" | `needs_review=True`, marker names in `reason` (pure heuristic) |
| F22 | Repository path missing for execution | `CodexExecutorError` raised before any process launch |
| F23 | Inspected path missing / not a Git repo / Git fails | `GitDiffError` with credential-redacted message |
| F24 | Git inspection times out / executable missing | `GitDiffError` naming the timeout or executable |
| F25 | Diff taken from a different repository than the issue context | `ChangeScopeError` before any comparison |
| F26 | Context `file_path` unusable (empty / absolute / traversal) | `ChangeScopeError` |
| F27 | Unrelated tracked file changed | `is_valid=False`, file listed in `unexpected_files` |
| F28 | New untracked file outside the scope | `is_valid=False`, file listed in `unexpected_files` |
| F29 | Diff reports an absolute or `..` path | Entry reported as unexpected; `is_valid=False` |
| F30 | Test command empty/malformed, or repo path missing | `TestRunnerError` raised before any process launch |
| F31 | Test executable missing | `TestResult.status == NOT_FOUND`, `exit_code` None |
| F32 | Project tests exceed the timeout | `TestResult.status == TIMEOUT`; T15 marks it blocking and does **not** retry |
| F33 | Project tests exit non-zero | `TestResult.status == FAILED` with exit code; T15 sets `blocking_reason` |
| F34 | Analysis command missing/malformed, or repository path missing | `SonarAnalysisError` raised before any process launch |
| F35 | Analysis scanner executable missing | `SonarAnalysisTriggerResult.status == NOT_FOUND`, `task_id is None` |
| F36 | Analysis command exceeds its timeout | `status == TIMEOUT`; the analysis stays unverified (no retry) |
| F37 | Analysis command exits non-zero | `status == FAILED` with exit code; stdout/stderr kept (scrubbed) |
| F38 | Scanner output has no CE task id | `task_id is None`; T17 returns `UNKNOWN` and does not guess |
| F39 | Compute-engine task not found (HTTP 404) | `SonarAnalysisState.UNKNOWN` with the not-found detail |
| F40 | CE API/network unavailable | Tolerated for a bounded number of consecutive errors, then `UNKNOWN` |
| F41 | Malformed/unexpected `/api/ce/task` response | Wait stops immediately as `UNKNOWN` (no spin) |
| F42 | Analysis still pending when the wait budget expires | `TIMEOUT` with `elapsed_seconds`/`poll_count`; never treated as success |
| F43 | Open-issue retrieval fails after a successful analysis | `retrieval_succeeded=False`; T19 → `REVIEW_REQUIRED` (never `FIXED`) |
| F44 | Original key gone but an equivalent issue is still open | `is_present=True`, `identity_reliable=False`; T19 → `REVIEW_REQUIRED` |
| F45 | Absence based on a truncated page or an unusable key | `identity_reliable=False`; T19 → `REVIEW_REQUIRED`, never `FIXED` |
| F46 | `total` is missing, `null`, non-numeric, boolean or otherwise malformed | `page_complete=False`, `reported_total=None`; absence unreliable → `REVIEW_REQUIRED` |
| F47 | `total` is negative, or larger than the number of returned issues | `page_complete=False`; absence unreliable → `REVIEW_REQUIRED` |
| F48 | Analysis correlation cannot be established (no completion, no task id, no `analysisId`, no configured component) | `correlation == UNKNOWN`; T19 → `REVIEW_REQUIRED`, never `FIXED` |
| F49 | The CE task belongs to a different component than the one read | `correlation == NOT_CORRELATED`; T19 → `REVIEW_REQUIRED` |
| F50 | Other analyses of the same component may run concurrently (precondition not declared) | `correlation == UNKNOWN`; T19 → `REVIEW_REQUIRED`, never `FIXED` |
| F51 | T16 did not trigger the analysis (non-zero exit / timeout / missing scanner / launch error) | T19 → `ANALYSIS_FAILED` (decisive stage `sonar_analysis`) |
| F52 | T16 triggered the analysis but reported no CE task id | T19 → `ANALYSIS_FAILED` (the completion cannot be established) |
| F53 | T17 waited on a different task id than T16 reported | T19 → `ANALYSIS_FAILED` (identity mismatch) |
| F54 | A path was already changed before Codex ran and changed again during it | `pre_existing_and_changed_files`; never attributed to Codex, `attributable=False` |
| F55 | The working tree was not clean when the baseline was captured | `clean_baseline=False`, `attributable=False` (a future T20 must refuse) |
| F56 | A future commit would contain a configured secret or URL userinfo | `SecretScanResult.ok is False` with structured findings; the value itself is never stored or logged |
| F57 | Secret-scan input is unusable (not text) or larger than the scan limit | `scanned=False`, `ok=False` (fail closed: the content cannot be declared clean) |

## 7. Acceptance criteria (Given / When / Then)

T04 — `SonarIssue`
- **Given** a raw issue `component = "my-project:src/Services/UserService.cs"`
  and `line = 42`, **when** converted with `to_sonar_issue`, **then**
  `file_path == "src/Services/UserService.cs"` and `line == 42`, and
  `component` is unchanged.
- **Given** a raw issue without `line`, **when** converted, **then**
  `line is None`.
- **Given** a raw issue whose component has no `:`, **when** converted,
  **then** `file_path == component`.

T05 — clone
- **Given** a local Git repository reachable by a file URL, **when**
  `RepositoryManager.clone(url, dest)` runs with an empty `dest`, **then**
  `dest/.git` exists and `dest` is a working copy of the source.
- **Given** an unusable URL, **when** clone runs, **then**
  `RepositoryError` is raised and its message contains no credential text.

T06 — source-branch checkout
- **Given** a clone whose checked-out branch is `main` and that has a local
  branch `develop`, **when** `checkout_source_branch(repo, "develop")`
  runs, **then** the returned branch equals `"develop"` and
  `git branch --show-current` reports `develop`.
- **Given** the requested branch does not exist, **when** the checkout runs,
  **then** `RepositoryError` is raised and the previously checked-out branch
  is unchanged.

T07 — agent branch
- **Given** `source` checked out and agent branch `ai/sonar-fix/x-20260909-101530`
  free, **when** `create_agent_branch(repo, name, "source")` runs, **then**
  the branch exists, is checked out, and its commit equals `source` HEAD.
- **Given** `ai/sonar-fix/x` already exists, **when** creating it again,
  **then** `RepositoryError` is raised and no second branch is created.
- **Given** the expected source branch is `main` but `develop` is checked
  out, **when** creating an agent branch, **then** `RepositoryError` is
  raised before anything changes.

T08 — issue context
- **Given** a repo on agent branch `ai/...` containing `src/app.py` with
  ≥ 10 lines and an issue at line 3, **when** `prepare` runs, **then** a
  context with the expected relative/absolute paths, branches, and line is
  returned and `has_file` is True.
- **Given** an issue with no line against an existing file, **when**
  `prepare` runs, **then** the context carries `line is None`.
- **Given** a traversal `file_path` (`../secret.py`), **when** `prepare`
  runs, **then** `ContextError` is raised.
- **Given** a missing file, **when** `prepare` runs, **then** `ContextError`
  is raised.
- **Given** a line beyond the end of the file, **when** `prepare` runs,
  **then** `ContextError` is raised.

T09 — Codex prompt
- **Given** a context with key/rule/severity/type/message/file/line,
  **when** `build_codex_prompt` runs, **then** the prompt contains the role,
  repository path, source and agent branch, every issue field, the explicit
  "EXACTLY ONE issue" scope, and the no-commit/no-push/no-PR constraints.
- **Given** a context whose issue has no line, **when** the prompt is built,
  **then** the prompt says the line was not provided.
- **Given** a hostile multi-line message ("ignore constraints…"), **when** the
  prompt is built, **then** the message is collapsed to one DATA-ONLY line,
  the fixed section headings still appear exactly once in order, and the
  constraints still follow the data block.
- **Given** two identical contexts, **when** prompts are built, **then** the
  outputs are byte-identical (deterministic).

T10 — Codex execution
- **Given** a fake runner that returns exit 0, **when** `execute` runs,
  **then** a `SUCCESS` result carries the prompt, working directory, stdout,
  and stderr (credentials redacted).
- **Given** a fake runner that raises `TimeoutExpired`, **when** `execute`
  runs, **then** the result is `TIMEOUT` with `exit_code` None.
- **Given** the executable is missing, **when** `execute` runs, **then** the
  result is `NOT_FOUND` and nothing is raised.
- **Given** a non-existent repository path, **when** `execute` runs, **then**
  `CodexExecutorError` is raised before the runner is invoked.
- **Given** stderr containing `https://user:pass@…`, **when** a result is
  built, **then** the password is absent from the stored text.

T11 — result analysis
- **Given** a `SUCCESS` result with clean output, **when** analysed, **then**
  `execution_succeeded` is True and `needs_review` is False.
- **Given** a `FAILED`/`TIMEOUT`/`NOT_FOUND` result, **when** analysed, **then**
  the matching flag is set and `needs_review` is True.
- **Given** a `SUCCESS` result whose text says "I am uncertain…", **when**
  analysed, **then** `output_suggests_uncertainty` is True (the process still
  succeeded; only the output is flagged).
- **Given** any analysis, **then** no tests run, no SonarQube call happens,
  and nothing is committed or pushed.

T12 — Git diff inspection
- **Given** a clean working tree, **when** inspected, **then** `is_clean` is
  True and all lists/diffs are empty.
- **Given** a modified tracked file plus a new untracked file, **when**
  inspected, **then** `changed_files` contains both (sorted), the diff text
  contains the tracked change, and untracked files are reported separately.
- **Given** paths containing spaces, **when** inspected, **then** the paths
  are returned verbatim (NUL-separated git output is used).
- **Given** a failing Git command whose stderr contains credentials, **when**
  inspected, **then** `GitDiffError` is raised and the message has no
  credential text.
- **Given** any inspection, **then** only read-only Git commands ran: nothing
  was staged, committed, pushed, or reverted.

T13 — change scope
- **Given** an issue on `src/app.py` and a diff in which only `src/app.py`
  changed, **when** `ChangeScopeValidator.validate` runs, **then**
  `is_valid is True`, `expected_files == ("src/app.py",)` and
  `unexpected_files == ()`.
- **Given** a clean working tree, **when** validated, **then** `is_valid is
  True` but `has_changes is False` and `expected_file_modified is False`
  (whether the issue is fixed is decided later, not here).
- **Given** an issue on `src/app.py` and a diff that also changes
  `src/other.py` (tracked or untracked), **when** validated, **then**
  `is_valid is False` and `"src/other.py"` appears in `unexpected_files` with
  an explanatory reason.
- **Given** a changed path reported with Windows separators (`src\app.py`),
  **when** validated, **then** it normalizes to `src/app.py` and stays in scope.
- **Given** a changed path that is absolute (`/etc/passwd`, `C:\...`) or
  contains `..`, **when** validated, **then** it is reported as unexpected and
  `is_valid is False`.
- **Given** a diff taken from a different repository, **when** validated,
  **then** `ChangeScopeError` is raised before any comparison.
- **Given** identical inputs, **when** validated twice, **then** the two
  results are equal (pure and deterministic).

T14 — project tests
- **Given** a configured command `["pytest", "-q"]` and a repository directory,
  **when** `ProjectTestRunner.run` runs, **then** the command reaches the
  process runner as that same argument array with the repository as working
  directory, and a zero exit code yields `status == PASSED`.
- **Given** a non-zero exit code, **when** the tests run, **then**
  `status == FAILED` and the exit code is preserved.
- **Given** a command that exceeds the timeout, **when** it runs, **then**
  `status == TIMEOUT` and `exit_code is None` (no exception escapes).
- **Given** a missing executable, **when** it runs, **then**
  `status == NOT_FOUND`; any other launch failure yields `EXECUTION_ERROR`.
- **Given** captured output or an error message containing URL credentials,
  **when** the result is built, **then** no credential text is present.
- **Given** an empty/malformed command, or a repository path that is not an
  existing directory, **when** `run` is called, **then** `TestRunnerError` is
  raised and no process is launched.
- **Given** the default runner, **when** it launches a process, **then**
  argument arrays are used and a shell is never enabled.

T15 — test outcome
- **Given** `TestResult.status == PASSED`, **when** analysed, **then**
  `passed is True`, `needs_review is False` and `blocking_reason is None`.
- **Given** `FAILED` / `TIMEOUT` / `NOT_FOUND` / `EXECUTION_ERROR`, **when**
  analysed, **then** `needs_review is True` and `blocking_reason` explains why
  the run cannot be treated as verified.
- **Given** any `TestResult`, **when** analysed, **then** redacted
  `stdout`/`stderr` are preserved on the outcome and the result is not mutated.
- **Given** a T14 run of a failing command, **when** T15 analyses it, **then**
  the test command ran exactly once: T15 performs no retry and invokes no Codex.

T16 — analysis trigger
- **Given** a configured command `["sonar-scanner"]` and a repository directory,
  **when** `SonarAnalysisTrigger.trigger` runs, **then** the command reaches the
  process runner as that same argument array with the repository as working
  directory, and a zero exit code yields `status == TRIGGERED`.
- **Given** a trigger attempt, **when** the result is inspected, **then**
  `triggered` only ever means "the analysis was requested" (nothing in the
  result or its summary claims completion or a fix).
- **Given** non-zero exit / timeout / missing executable / other launch failure,
  **when** the trigger runs, **then** the status is
  `FAILED` / `TIMEOUT` / `NOT_FOUND` / `EXECUTION_ERROR` respectively and
  `exit_code is None` whenever the process never completed.
- **Given** scanner output containing
  `…/api/ce/task?id=AY1abcDEF-2xyz`, **when** the trigger completes, **then**
  `task_id == "AY1abcDEF-2xyz"`; **given** output with no such line, **then**
  `task_id is None`; **given** a crafted/over-long id, **then** it is discarded.
- **Given** credentials in `AnalysisConfig.environment` (built with
  `build_sonar_environment`), **when** the trigger runs, **then** the child
  process receives them through the environment, the argument array contains no
  credential, and no result/summary field contains the token.
- **Given** captured output or a launch error containing a secret or URL
  credentials, **when** the result is built, **then** the text is scrubbed.
- **Given** a malformed command or a repository path that is not an existing
  directory, **when** `trigger` is called, **then** `SonarAnalysisError` is
  raised and no process is launched.
- **Given** the default runner, **when** it launches a process, **then**
  argument arrays are used and a shell is never enabled.

T17 — waiting for the analysis
- **Given** a CE task that reports `SUCCESS` on the first poll, **when** the
  waiter runs, **then** `status == SUCCESS`, `poll_count == 1` and no sleeping
  happened.
- **Given** `PENDING`, `IN_PROGRESS`, then `SUCCESS`, **when** the waiter runs,
  **then** it polls three times, sleeps the configured interval twice and ends
  in `SUCCESS` while asking the client exactly `poll_count` times.
- **Given** `FAILED` or `CANCELED`, **when** the waiter runs, **then** the state
  is terminal and `failure_reason` carries the (redacted, capped) engine
  message.
- **Given** a task that never leaves `PENDING`, **when** the wait budget is
  exhausted, **then** `status == TIMEOUT` with `elapsed_seconds` and
  `poll_count` recorded, and the outcome is deterministic across runs.
- **Given** a 404 (task not found), repeated API/network errors, or a
  malformed/unexpected payload, **when** the waiter runs, **then** it ends as
  `UNKNOWN` with an explanatory reason instead of looping.
- **Given** no usable task id and no resolver result, **when** `wait` is called,
  **then** it returns `UNKNOWN`, performs **no** poll, and never falls back to
  asking whether the project is green.
- **Given** a frozen clock (or a very small `max_polls`), **when** the waiter
  runs, **then** it still terminates (bounded polls, no infinite loop).
- **Given** an unsafe task id (traversal, whitespace, absurd length),
  **when** `wait` is called, **then** `AnalysisWaitError` is raised.

T18 — verifying the new issues
- **Given** the original key is still among the open issues, **when**
  verification runs, **then** `match_type == KEY`, `is_present is True` and
  `identity_reliable is True`.
- **Given** the original key is gone and no equivalent issue is open, **when**
  verification runs, **then** `is_present is False` and (for a complete page)
  `identity_reliable is True`.
- **Given** an unrelated open issue with the same message or the same rule in a
  different file, **when** verification runs, **then** it is not treated as the
  original issue.
- **Given** the original key is gone but an issue with the same rule at the same
  file and line (or the same file) is open, **when** verification runs, **then**
  `is_present is True` with `identity_reliable is False` (ambiguity, never a
  fix).
- **Given** an original issue without a usable key, **when** exactly one open
  issue matches rule + file + line, **then** that match is reported as reliable;
  **when** several match, **then** the result is ambiguous.
- **Given** a retrieval failure, a non-object payload, or a payload without an
  `issues` list, **when** verification runs, **then** `retrieval_succeeded is
  False`, `identity_reliable is False` and `error` is set (redacted) instead of
  raising.
- **Given** a truncated page (`total` greater than the returned issues),
  **when** the issue appears absent, **then** `page_complete is False` and the
  absence is not reliable.
- **Given** a missing, `null`, non-numeric, boolean, malformed or negative
  `total`, **when** verification runs, **then** `page_complete is False`,
  `reported_total is None` and no absence can be reliable.
- **Given** `total` equal to the number of returned issues (including `0`),
  **when** verification runs, **then** `page_complete is True`.
- **Given** the T17 completion is supplied with a matching component and the
  single-analysis precondition is declared, **when** verification runs, **then**
  `correlation == CORRELATED` and `analysis_correlated is True`.
- **Given** no completion, no task id, no `analysisId`, no configured component,
  or an undeclared precondition, **when** verification runs, **then**
  `correlation == UNKNOWN` and `reliable_absence is False`.
- **Given** a completion for a different component (or a non-successful
  analysis), **when** verification runs, **then** `correlation ==
  NOT_CORRELATED` and `reliable_absence is False`.
- **Given** any verification, **then** exactly one read-only retrieval happened,
  no SonarQube state changed, no issue was closed/resolved, and the T03 filters
  were not applied unless explicitly opted in.

T19 — final status
- **Given** Codex success + valid scope + passing tests + successful analysis +
  original issue reliably absent + the issue's file changed, **then** the status
  is `FIXED`, `is_fixed is True` and `blocking_reason is None`.
- **Given** Codex success + valid scope + passing tests + successful analysis
  but the original issue is still reliably open, **then** the status is
  `STILL_OPEN` — **never** `FIXED` (a green analysis is not a fix).
- **Given** any failing stage, **then** the earlier stage wins:
  `CODEX_FAILED` → `SCOPE_INVALID` → `TESTS_FAILED` → `ANALYSIS_FAILED`.
- **Given** a T16 trigger that did not succeed (or reported no usable task id,
  or a task id that disagrees with the waited-on task), **then** the status is
  `ANALYSIS_FAILED` — never `REVIEW_REQUIRED` and never `FIXED`.
- **Given** failing tests together with an absent issue, **then** the status is
  `TESTS_FAILED` (a broken run is never presented as a fix).
- **Given** an indeterminate analysis state (`UNKNOWN`), a failed retrieval, an
  ambiguous identity, an unreliable absence, **an uncorrelated snapshot**, or a
  disappearing issue with no change to its own file, **then** the status is
  `REVIEW_REQUIRED`.
- **Given** a reliable absence but a correlation verdict that is not
  `CORRELATED`, **then** the status is `REVIEW_REQUIRED` and `is_fixed is False`.
- **Given** the same inputs, **when** the decision runs twice, **then** the
  results are equal (pure and deterministic) and nothing was retrieved, run, or
  modified by the decision.
- **Given** every combination of T16 trigger state and T17 completion state,
  **when** the decision runs, **then** the result always equals
  `ANALYSIS_STAGE_STATUS[outcome]`, and `FIXED` is possible only from
  `COMPLETED`.

Pre-T20 safety primitives
- **Given** a clean working tree, **when** a baseline is captured and the run
  changes the issue's file, **then** `agent_files == (that file,)`,
  `clean_baseline is True` and `attributable is True`.
- **Given** a file that was already modified/staged/untracked before the run,
  **when** the run changes a different file, **then** only the run's file is in
  `agent_files` and the pre-existing change is in `pre_existing_files`.
- **Given** a path that was already changed before the run and changed again
  during it, **then** it is reported in `pre_existing_and_changed_files`,
  **never** in `agent_files`, and `attributable is False`.
- **Given** a baseline capture, **then** no Git command stages, commits, pushes,
  resets or cleans anything, and the working tree is byte-for-byte unchanged.
- **Given** a future commit's text containing a configured secret or a URL with
  userinfo, **when** the scanner runs, **then** it reports structured findings,
  `ok is False`, and the secret value appears nowhere in the result.
- **Given** unusable or oversized scanner input, **then** `scanned is False` and
  `ok is False` (fail closed).

## 8. Security and hygiene rules


1. Secrets/tokens never appear in URLs, logs, or exception messages.
   Git stderr is passed through `redact_credentials` before being wrapped.
2. Git and Codex are always invoked with argument lists — never `shell=True`
   — so no value can be interpreted by a shell.
3. Branch names are validated before reaching Git (option injection guard:
   must not start with `-`).
4. Repository-relative paths are validated (absolute/`..`/symlink-escape all
   rejected) before any file access.
5. Nothing is ever pushed, committed, or force-pushed by T01–T19 code.
6. SonarQube `message` text is treated as untrusted input: it is collapsed to
   one line and quarantined in a DATA-ONLY block, and the prompt states that
   it can never override the fixed constraints.
7. The prompt itself is only ever *text*; it is never executed by a shell,
   and no prompt content can alter the executor's subprocess/argument-array
   behaviour.
8. Codex output is credential-redacted before it is stored on a typed result;
   analysis summaries never include raw output.
9. Codex is executed with the cloned repository as the working directory and
   only read-only Git inspection (T12) follows: nothing is committed, pushed,
   or merged by this scope.
10. No API keys, tokens, or secrets exist in source code, and none are added
    by T09–T15 (Codex is invoked as an external CLI only).
11. Changed paths are treated as untrusted: absolute, empty, and `..`-containing
    entries can never be in scope and always fail T13 validation; case is folded
    only on case-insensitive platforms.
12. The project test command is configuration only: it is never derived from
    SonarQube issue text, Codex output, or any other untrusted value, and it is
    always passed as an argument array (never a shell string) — so no value can
    be interpreted by a shell.
13. Captured test stdout/stderr and launch errors are credential-redacted before
    they are stored on a typed result, and summaries omit raw output.
14. T13–T15 never write, commit, push, retry, or re-invoke Codex; T14 runs only
    the explicitly configured command inside the cloned repository.
15. T16 runs only the configured analysis command, with argument arrays and no
    shell; the command is never derived from SonarQube issue text, Codex output,
    or any other untrusted value.
16. SonarQube credentials are passed to the scanner through the environment
    (never on the command line), are never stored on a result or in a summary,
    and are scrubbed (together with URL userinfo) from captured output and error
    messages.
17. T17 validates compute-engine task ids before using them (strict charset and
    length) and treats every CE response as untrusted data; uninterpretable
    responses are reported as `UNKNOWN` instead of being acted on.
18. T18 reuses the existing read-only retrieval path: it never calls an issue
    mutation endpoint, never resolves/closes/re-opens an issue, and never
    fabricates issue state. Issue keys, components, messages and paths are
    normalized and validated before comparison, and issue text is never used to
    build a command.
19. T16–T19 never modify source code, never create branches, never commit, never
    push, never retry, and never re-invoke Codex; T19 is pure decision logic.
20. Analysis correlation is fail closed: `correlation` reaches `CORRELATED`
    only when every precondition is proven (successful analysis, task id,
    `analysisId`, matching component, declared single-analysis precondition).
    `UNKNOWN`/`NOT_CORRELATED` are never treated as success, and the
    correlation reasons embed only credential-redacted, length-capped metadata.
21. The worktree baseline primitive is read-only: it runs only `rev-parse`,
    `status`, `diff` and `hash-object` (without `-w`), never stages, commits,
    pushes, resets, cleans or deletes, and always uses argument arrays. A
    pre-existing change is never attributed to the agent.
22. The secret scanner never stores, logs or raises the matched value: findings
    carry only a kind, a line number, an optional path and the index of the
    configured entry. Unusable or oversized input is reported as *not scanned*
    (`ok is False`) rather than as clean.
23. Neither new primitive is wired into a commit, a push, or any production
    workflow: they are safety foundations for T20 only.
24. T20 is the **only** component allowed to write to Git, and it performs exactly
    two writes: one exact-path `git add -- <paths>` and one `git commit -m <text>`.
    Any other subcommand raises `GitCommitError` before a process is launched
    (allow-list), and the destructive/network subcommands are additionally named
    in an explicit deny-list.
25. T20 never resets, restores, unstages, stashes, checks out, retries or amends:
    a failed commit leaves the staged state exactly as it is for a human to
    inspect, and a commit that cannot be proven becomes `COMMIT_UNVERIFIED`
    (loud), never a silent pass.
26. T20 refuses to commit on a protected/reserved branch (`main`, `master`,
    `develop`, `trunk`, the configured default branch) or on any branch outside
    the agent namespace (`ai/sonar-fix/`): T07 owns branch creation and T20 never
    creates, switches or deletes a branch.
27. T20 requires an explicit `user.name`/`user.email` that is exactly what
    `git var` reports; a fabricated OS/domain identity is never accepted, and the
    commit is executed with `-c user.name=… -c user.email=… -c
    user.useConfigOnly=true` so Git cannot guess one.
28. The validated command line may not contain any repository redirect
    (`-C`, `--git-dir`, `--work-tree`, `--namespace`, `--exec-path`) and `git
    config` is read-only: T20 can never operate on another repository and can
    never write configuration.
29. Content identity is Git identity, never a raw byte hash: the approved ids come
    from `git hash-object --path=<path> -- <path>`, so CRLF normalisation,
    `.gitattributes` and clean filters are applied exactly as staging applies
    them, and the ids are compared against `git ls-files -s`, the committed tree
    and a final pre-commit re-check.
30. Commit messages are derived, bounded and injection-proof: no free-form text
    ever reaches Git, control/terminal-escape characters and NUL are removed or
    rejected, the subject is a single line, and the message is passed as one
    `-m` argv item (never interpolated into a shell).
31. The secret scan covers the approved file content, the worktree diff and the
    staged diff; a secret in any of the three (or a scan that could not run) is a
    refusal, and the matched value never appears in a reason, a result or a log.

## 9. Automated test mapping

| Test file | Covers |
|-----------|--------|
| `tests/test_models.py` | T04 `SonarIssue` fields, optional `line`, `file_path` |
| `tests/test_branch_naming.py` | T07 naming, slugging, validation |
| `tests/test_repository.py` | T05 clone, T06 checkout, T07 branch creation (+ failures) |
| `tests/test_context.py` | T08 context prep, path safety, missing file/line failures |
| `tests/test_codex_prompt.py` | T09 prompt contents, one-issue scope, constraints, hostile message, determinism |
| `tests/test_codex_executor.py` | T10 success/failure/timeout/not-found, capture, redaction, injectable runner |
| `tests/test_codex_result.py` | T11 success/failure/timeout/not-found, uncertainty markers, typed analysis |
| `tests/test_git_diff.py` | T12 clean/changed trees, file lists, spaces, failure + redaction |
| `tests/test_change_scope.py` | T13 in/out of scope, untracked files, normalization, traversal/absolute rejection, unsafe inputs |
| `tests/test_test_runner.py` | T14 pass/fail/timeout/not-found/exec-error, command validation, argument arrays, no shell, redaction |
| `tests/test_test_result.py` | T15 status mapping, blocking reasons, preserved output, defensive branch, no-retry guarantee |
| `tests/test_sonar_analysis.py` | T16 trigger statuses, command arrays, no shell, env-only credentials, task-id extraction, scrubbing, injectable runner |
| `tests/test_sonar_client.py` | T16/T17 client addition (`get_ce_task`: success, 404, HTTP error, non-JSON, blank id) + T01/T02 regression checks |
| `tests/test_sonar_analysis_waiter.py` | T17 immediate/late success, failed, canceled, timeout, not found, API errors, malformed payloads, identity/resolver, bounded polls |
| `tests/test_sonar_issue_verification.py` | T18 identity ladder, ambiguity, message/rule non-matches, fail-closed page completeness, malformed payloads, retrieval failure, correlation verdicts, opt-in filtering |
| `tests/test_analysis_correlation.py` | T17→T18 correlation verdicts (exact/missing/wrong-component/undeclared), metadata redaction, determinism, end-to-end refusal of `FIXED` |
| `tests/test_issue_status.py` | T19 every stage and combination, precedence, T16→T17→T19 matrix, correlation gate, `FIXED` acceptance, `REVIEW_REQUIRED` cases, determinism, carried `trigger_evidence` |
| `tests/test_worktree_baseline.py` | pre-T20 baseline capture (staged/unstaged/untracked/deleted/renamed/ignored), read-only guarantees, attribution of pre-existing vs agent changes, failure modes |
| `tests/test_secret_scan.py` | pre-T20 secret scan: configured secrets (added/removed/context), URL userinfo, false-positive-safe URLs, fail-closed input handling, no secret leakage |
| `tests/test_commit_message.py` | T20 message: deterministic format, bounds, shortening ladder, argv safety, control/escape/NUL/newline injection, unusable rule/path/issue keys, validator and sanitizer contracts |
| `tests/test_commit_policy.py` | T20 gates: gate catalogue/phase map integrity, all 45 gates pass on the happy-path record, fail-closed on missing facts, every gate's refusal condition (G1–G45), phase `NOT_REACHED` prefixes, config policy, approved-set resolution, path normalisation |
| `tests/test_git_commit.py` | T20 executor against real `tmp_path` repositories: the happy path (exactly one commit, expected parent/message/identity/blobs), pre-Git refusals (T19 status, Codex, tests, analysis, verification, dirty baseline, out-of-scope/deleted/renamed/unchanged target), environment/branch/identity/index-lock/merge/detached refusals, injected `add`/`commit` failures, staged-content change before the commit, `COMMIT_UNVERIFIED`, ignored/binary/secret changes, CRLF/`.gitattributes`/clean-filter content identity, allow-list and hostile-argv refusals |
| `tests/test_push_policy.py` | T21 gates: gate catalogue/phase-map integrity, all 48 gates pass on the happy-path record, fail-closed on unknown facts, one adversarial break per gate (G1–G48), phase `NOT_REACHED` prefixes, cascade reporting, refspec/remote-name/remote-URL/commit-id/path primitives, `PushRequest` completeness, policy configuration |
| `tests/test_git_push.py` | T21 executor against real `tmp_path` repositories with a bare remote: the happy path (exactly one push, exact argv, read-back proof, unchanged local state, new remote branch, module helper), pre-push refusals (non-`COMMITTED` T20 result, out-of-band commit, missing/protected/non-agent destination, protected source, missing/credential-bearing/unreadable remote, alternate push URL, default push refspec, dirty/staged/detached/merge states, already-pushed commit, remote moved out of band, bare or non-repository path, unsafe environment, caller errors), faults (rejected, timed-out, "successful" but unmoved, unreadable read-back, missing Git), the command audit (allow-list, argv, environment hardening, read-only `config`), the pre-push checkpoint race, the real-repository safety check, and the module API |
| `tests/test_trigger_evidence.py` | T21.1.a provenance: T16 `evidence()` is the minimal frozen projection (no stdout/argv/exit-code leakage), T19 carries it on **every** verdict and exposes it in `as_dict()`, and the real T20 executor commits using the evidence the T19 result carried while refusing (G11 `FAIL`, no commit created) when it is missing, unusable or mismatched |
| `tests/test_t20_t21_integration.py` | T21.1.c real T20→T21 handoff: a genuine `GitCommitExecutor` commit in a `tmp_path` clone feeds a genuine `GitPushExecutor.push_safely` both as the `CommitResult` object and as its `as_dict()` view (exactly one push, local bare remote read back at `T20.new_head`, local state unchanged), plus the pinned `CommitResult.as_dict()` → `_project_t20` → `PushFacts` key contract |
| `tests/test_overall_report.py` | T22 (§13): pure aggregation of T19/T20/T21 evidence, every source status and count, the end-to-end/partial/failed/review classifications, every fail-closed gate (G1-G20), determinism, ordering, secret safety, immutability, the S0-S11 state machine, canonical serialization, and a genuine `GitCommitExecutor`/`GitPushExecutor` handoff in a `tmp_path` clone whose repository is provably untouched by the report |
| `tests/test_overall_report_policy.py` | T22 (§13): gate catalogue/`GATE_PHASE` integrity, policy defaults and derived evidence requirements, the collection/identity/value/payload helpers, the fail-closed failure-report contract, and the module's freedom from any execution dependency |
| `tests/test_pipeline_config.py` | T30 (§22): configuration required/malformed values, command parsing, limit defaults, token secret-safety, work-dir override |
| `tests/test_pipeline_run.py` | T30 (§22): T24/T25/T26/T28 blocks, the FIXED happy path, Codex/test/scope failures, unavailable SonarQube, commit/push wiring, repository failure as a blocked attempt, blocked issues excluded from the overall report, and the T23 outcome for a FIXED-but-not-committed issue |
| `tests/pipeline_fakes.py` | T30 (§22): non-collected fakes for every injected I/O boundary |

Run: `.venv\Scripts\python -m pytest -q --cov=. --cov-report=term-missing`

## 10. Non-goals (unchanged for T22+, now that T30 wires the stages)

* No PR creation, no reporting beyond the T22 structured overall report (§13)
  and the T23 per-issue report (§14), and no retry or iteration **loops**. T30
  (§22) now orchestrates the existing stages, but it adds no PR, no retry loop
  and no CI/container wiring. The T24/T25 limits (§15, §16) bound a run's issue
  selection and one issue's retry counter; T30 calls them but never loops. T26
  (§18) decides whether a branch is safe to mutate; T30 passes its verdict to the
  mutation gates, but T26 itself still runs no Git command, discovers no default
  branch and creates no branch. No production logging beyond the T29-bound
  pipeline logger, no container and no GitLab CI wiring.
* T20 performs exactly two Git writes and T21 exactly one (see §11, §12), and
  nothing else: they never push more than once, fetch, pull, clone, reset,
  restore, clean, stash, check out, switch, amend, retry, or write
  configuration, and they never create or delete a branch (T07 owns branch
  creation).
* T13–T19 perform no Git mutations and no orchestration: T13 is pure policy,
  T14 runs only the configured project-test command, T15 is pure
  classification, T16 only *triggers* the analysis, T17 only *waits*, T18 only
  *reads* issues back, and T19 is a pure decision. No stage retries, and no
  stage re-invokes Codex.
* T16–T19 do not resolve, close, re-open, or otherwise mutate SonarQube issues,
  and do not re-trigger an analysis after a failure (that would be a retry,
  which is T25).
* No real Codex invocation and no live-SonarQube validation in the test suite:
  `pytest` uses injected runners, injected clocks/sleepers, fake clients, local
  Git repositories, and `tmp_path` only. The single symlink-escape test
  auto-skips where the OS lacks the required privilege.
* No support for writing files outside the cloned repository.
* T12 describes the working tree and T13 only checks that the change stayed in
  the issue's scope; neither judges whether the fix is correct. T18/T19 verify
  the fix against a **new analysis**, and `FIXED` additionally requires a
  reliable identity match, a **correlated** snapshot, plus a change to the
  issue's own file.
* T14 does not install dependencies, build the project, or run anything other
  than the single configured test command, and T15 never acts on an outcome
  (no retry, no Codex re-invocation, no branching logic).
* `main.py` keeps its original read-only T01–T04 behaviour by default and adds
  `--run` (T30 orchestration), `--commit`, `--push` and `--json`. The library
  stages remain validated as units with injectable boundaries; the `pipeline/`
  package is the composition layer. T21 is **not** called unless `--push` is
  passed *and* a real T20 commit reports `is_committed`, so nothing pushes
  automatically.

## 11. T20 — safe, fail-closed, exactly-one-commit Git commit (implemented)

T20 turns a T19 `FIXED` attempt into **at most one** commit that contains
exactly the change this run made. It is three modules, with the mutation
boundary enforced in code rather than by convention:

| Module | Role | Writes to Git |
|--------|------|---------------|
| `commit_message.py` | deterministic, bounded, injection-proof commit message (pure) | never |
| `commit_policy.py` | the 45 gates, approved-set resolution and path normalisation (pure, no I/O) | never |
| `git_commit.py` | the executor: collects evidence, evaluates gates, performs the two writes | exactly two commands |

### 11.1 The two and only two Git writes

1. **exact-path staging** - `git add -- <exact approved paths>`
2. **exactly one commit** - `git commit -m <validated message>`

Everything else T20 runs is read-only (`rev-parse`, `rev-list`, `status`,
`diff`, `diff-tree`, `ls-files`, `ls-tree`, `hash-object` without `-w`, `log`,
`config --get…`, `var`, `symbolic-ref`, `show-ref`, `cat-file`,
`check-ignore`).

* `ALLOWED_GIT_SUBCOMMANDS` is an allow-list: any other subcommand raises
  `GitCommitError` **before** a process is launched.
* `FORBIDDEN_GIT_SUBCOMMANDS` names the destructive and network operations
  explicitly (`reset`, `restore`, `clean`, `stash`, `checkout`, `switch`,
  `update-ref`, `reflog`, `rm`, `mv`, `merge`, `rebase`, `cherry-pick`,
  `revert`, `am`, `apply`, `push`, `fetch`, `pull`, `remote`, `clone`,
  `branch`, `tag`, `gc`, `prune`, `repack`, `filter-branch`, `filter-repo`,
  `replace`, `notes`, `worktree`, `submodule`, `commit-tree`,
  `update-server-info`, `daemon`, `archive`, `bundle`, `fsck`), so the refusal
  is intentional and testable.
* `add` must carry `--` followed by exact paths: `.`, `-A`, `--all`, `-u`,
  `--update`, `-f`, `--force`, `-N`, `-p`, `-i`, `-e`, a token starting with
  `-`, and a `:`-prefixed pathspec are all refused.
* `commit` must carry exactly one `-m` and no pathspec; `--amend`,
  `--no-verify`, `--allow-empty`, `--allow-empty-message`, `-a`, `-o`, `-i`,
  `-p`, `-F`, `-C`, `-c`, `-e`, `--fixup`, `--squash`, `--edit`,
  `--reuse-message`, `--pathspec-from-file` and `--pathspec-file-nul` are
  refused.
* `-c` overrides are limited to `user.name=`, `user.email=` and
  `user.useConfigOnly=`; anything else (a hook path, a filter, a template, an
  fsmonitor program) is refused.
* Repository/work-tree/program redirects - `-C`, `--git-dir`, `--work-tree`,
  `--namespace`, `--exec-path`, in both the separate and the `=` form - are
  refused inside the validated arguments: T20 fixes the repository with its own
  single `-C <root>`.
* `git config` is read-only (`--get`, `--get-all`, `--get-regexp`, `--list`,
  `-l` only), so T20 can never write configuration.
* Git runs with a sanitized environment: the unsafe `GIT_*` variables
  (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`,
  `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`,
  `GIT_CONFIG`/`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`/`GIT_CONFIG_COUNT`,
  `GIT_SSH_COMMAND`/`GIT_SSH`, `GIT_EXTERNAL_DIFF`, `GIT_AUTHOR_*`,
  `GIT_COMMITTER_*`) are a refusal **and** are removed from every child;
  `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0` and `GIT_PAGER=cat` are always
  forced. Shell/IDE helpers that cannot redirect this commit (`GIT_ASKPASS`,
  `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`, `GIT_PAGER`) are deliberately *not*
  refusals - T20 always passes `-m`, never prompts and never fetches - so a
  normal developer machine cannot produce a false refusal. The caller's
  environment mapping is never mutated.
* `shell=False` everywhere (argument arrays only), and Git stderr is
  credential-redacted before it reaches a reason or an exception.




### 11.2 The 45 gates (G1-G45)

`commit_policy.GATES` is the authoritative list; every gate has exactly one
evaluator (`_GATE_EVALUATORS`, asserted against `GATES` on import) and exactly
one phase (`_GATE_PHASE`). A gate is `NOT_REACHED` until its phase has run,
`PASS` when its fact proves the condition, and `FAIL` (fail closed) otherwise;
the decision is `REFUSE` as soon as any evaluated gate fails, so the report
identifies **every** refusal instead of only the first one.

| Gates | Phase (state-machine stage) | Condition |
|-------|-----------------------------|-----------|
| G1 | `INPUTS` (S0) | every required input is present and usable |
| G2-G3 | `FIX_EVIDENCE` (S0/S6) | T19 status is `FIXED`; no review condition or blocking reason |
| G4-G6 | `FIX_EVIDENCE` | Codex succeeded; no uncertainty marker; no review request |
| G7-G9 | `FIX_EVIDENCE` | T13 scope valid; the issue's own file changed; tests passed |
| G10-G14 | `FIX_EVIDENCE` | analysis succeeded; task id present **and** matching the required T16 evidence (a missing, unusable or contradictory T16 record fails closed, so the cross-check can never be skipped silently); snapshot correlated; reliable absence; no positive finding |
| G15-G18 | `FIX_EVIDENCE` | attribution valid; baseline clean and fully captured; no pre-existing target modification; whole tree accounted for (no unexplained change, no ignored file created) |
| G19-G24 | `APPROVAL` (S7/S8) | approved set non-empty and exactly resolved; no deletion; no rename; literal safe paths; no ignored approved path |
| G25-G28 | `APPROVAL`/`STAGING` | no binary approved file; content, worktree-diff and staged-diff secret scans passed |
| G29-G37 | `REPOSITORY` (S1-S5) | repository identity valid; environment safe; HEAD not detached; branch not protected/reserved and inside the agent namespace; explicit identity; no operation in progress and no index lock; no unmerged entries; no active hook T20 cannot account for; HEAD unchanged since the baseline |
| G38-G41 | `STAGING`/`COMMIT` (S10-S12) | worktree still matches the approved content; staged set exactly the approved set; staged content equals the approved content; staged/worktree content unchanged immediately before the commit |
| G42 | `COMMIT` (S12) | the commit message is valid and names exactly the approved file/rule/issue |
| G43-G45 | `VERIFY` (S13/S14) | the single commit attempt succeeded; the post-commit state proves exactly one commit with the expected parent/message/identity/paths/blobs/branch/root; no unexpected Git error |

### 11.3 The state machine (each stage runs only when every earlier gate passed)

```
S0  VALIDATE_INPUTS        G1                       (build/validate the message, too)
S1  IDENTIFY_REPOSITORY    G29                      (read-only)
S2  CHECK_ENVIRONMENT      G30                      (before any Git process)
S3  CHECK_BRANCH           G31, G32
S4  CHECK_IDENTITY         G33
S5  CHECK_REPO_STATE       G34-G37                  -> repository checkpoint
S6  VERIFY_BASELINE        G2-G18
S7  RESOLVE_APPROVED_SET   G19-G24                  (once; then immutable)
S8  SCAN_APPROVED_CONTENT  G25-G27                  -> approval checkpoint
S9  STAGE_EXACT_FILES      -                        (first Git write)
S10 VERIFY_INDEX           G38-G40
S11 SCAN_STAGED_CONTENT    G28
S12 FINAL_PRE_COMMIT_CHECK G41, G42                  -> commit checkpoint
S13 COMMIT                 -                        (second and last Git write, one attempt)
S14 VERIFY_COMMIT          G43-G45                  -> final checkpoint
```

The repository checkpoint (S5) runs **before** the approved set is resolved and
long before the first Git write, so a protected branch, a redirected
environment, a missing identity, a held index lock or an in-progress operation
can never even reach `git add`.


### 11.4 The commit message (T20.4)

`commit_message.build_commit_message(rule=…, file_path=…, issue_key=…)` is pure
and deterministic - no clock, no random value, no environment lookup - and
produces a conventional commit that preserves the SonarQube identity:

```
fix(sonar): resolve python:S1481 in src/app.py

Sonar-Issue: AX1abcDefG
Sonar-Rule: python:S1481
```

* Only three validated values may enter the text: the Sonar rule key, the
  repository-relative target path and the issue key. Nothing else - least of all
  free-form Codex or SonarQube message text - is ever interpolated.
* The subject is a single line, never starts with `-`, and is shortened
  deterministically (full path -> basename -> rule only -> hard truncation) when
  the configured caps require it; the whole message honours
  `CommitPolicyConfig.max_message_length` and the subject
  `DEFAULT_MAX_SUBJECT_LENGTH`.
* NUL, C0/C1 controls, `\r`, terminal escape sequences and newline injection are
  removed (sanitizer) or rejected (validator/builder); an unusable rule key, an
  unsafe path, an over-long value or a non-text value raises
  `CommitMessageError` instead of being repaired.
* The text is handed to Git as one argv item (`("commit", "-m", text)`); the
  subject can never be split into options.
* `validate_commit_message` is the G42 verdict and returns `None` for anything
  unsafe; the builder re-validates its own output before returning.

### 11.5 Outcomes and failure semantics

| `CommitStatus` | Meaning | Git state |
|----------------|---------|-----------|
| `COMMITTED` | exactly one verified commit was created | HEAD advanced by exactly one commit from the recorded baseline |
| `REFUSED` | a gate refused; no commit was attempted | nothing was staged **unless** the refusal came from the pre-commit re-check (S12), in which case the approved paths are staged and left exactly as they are |
| `COMMIT_FAILED` | the single `git commit` attempt failed | the staged state is left untouched; no reset/restore/unstage/stash/retry |
| `COMMIT_UNVERIFIED` | a commit exists but its correctness could not be proven | the commit is left in place and reported loudly for a human |

Failure handling is deliberately **passive**: T20 never resets, restores,
unstages, stashes, checks out, cleans, amends or retries. Only genuine
caller/environment errors (a missing `IssueStatusResult`, a repository path that
does not exist, a forbidden command shape, a Git launch failure) raise
`GitCommitError`; an unusable *Git working copy* is a `REFUSED` result with the
G29 verdict.

`CommitResult` is typed, immutable and secret-free (`as_dict()` carries the
status, reason, the full state-machine trail, the approved paths, the content
ids, the message, the head boundary and all 45 gate verdicts), so T21+ can
report it without leaking anything.

### 11.6 Policy configuration (defaults are the conservative choice)

| `CommitPolicyConfig` | Default | Effect |
|----------------------|---------|--------|
| `protected_branches` | `main`, `master`, `develop`, `trunk` | never receive a T20 commit |
| `default_branch` | `None` | additionally refused (so a renamed default branch is still refused) |
| `required_branch_prefix` | `ai/sonar-fix` | a branch outside this namespace is refused (T07 owns branch creation) |
| `require_explicit_identity` | `True` | refuse unless `user.name`/`user.email` are explicitly configured |
| `require_clean_baseline` | `True` | refuse unless the pre-run tree was clean (an empty index is always required) |
| `allow_ignored_approved_paths` | `False` | test-only override; production never approves an ignored path |
| `max_approved_files` | `50` | a bigger set means the scope was not resolved |
| `max_message_length` | `200` | handed to the message validator |
| `extra_allowed_files` | `()` | files T13 explicitly proved in addition to the issue's own file |

### 11.7 Acceptance criteria

1. **Given** a T19 `FIXED` result **when** T20 runs on an agent branch with an
   explicit identity, a clean baseline and one exactly-attributed in-scope file,
   **then** exactly one commit is created, its parent is the recorded baseline
   HEAD, it contains only the approved path(s), its author and committer are the
   verified identity, its message is the derived message, and no other branch,
   commit or configuration value changed.
2. **Given** any refusal condition (a non-`FIXED` T19 status, uncertainty or a
   review request, failed tests, an unverified/uncorrelated analysis, a dirty or
   incomplete baseline, a pre-existing target change, an out-of-scope, deleted,
   renamed, ignored, binary or secret-bearing change, a protected or non-agent
   branch, a detached HEAD, a missing identity, an unsafe environment, a held
   index lock, an in-progress operation, an unmerged index, an active hook, a
   moved HEAD, or an unprovable message) **then** the result is `REFUSED`, no
   commit reaches history, and - for everything before S9 - not even `git add`
   is launched.
3. **Given** a commit that fails **then** the status is `COMMIT_FAILED`, the
   staged state is untouched, exactly one attempt was made, and no recovery
   command ran.
4. **Given** a commit that cannot be proven correct afterwards **then** the
   status is `COMMIT_UNVERIFIED` and the commit is left in place for a human.
5. **Given** an argv T20 is not allowed to run (a forbidden subcommand, a broad
   staging form, `--amend`, `--no-verify`, an unapproved `-c` override, a
   repository redirect) **then** `GitCommitError` is raised **before** any
   process is launched.
6. **Given** CRLF content, a `.gitattributes` override or a clean filter
   **then** the approved, staged and committed content identity agree exactly
   (Git identity, never a raw byte hash) and the commit succeeds.

## 12. T21 — safe, fail-closed, exactly-one-push Git push (implemented)

T21 turns a T20 `COMMITTED` result into **at most one** push that moves exactly
the one recorded commit to exactly one destination branch. It is two modules,
with the mutation boundary enforced in code rather than by convention:

| Module | Role | Writes to Git | Contacts the remote |
|--------|------|---------------|---------------------|
| `push_policy.py` | the 48 gates, the phase map, the observation/fact records and the refspec/remote validators (pure, no I/O) | never | never |
| `git_push.py` | the executor: collects evidence, evaluates gates, performs the one push, reads the result back | exactly one command | read-only until S11 (`ls-remote`), read-only again after it |

### 12.1 The one and only Git write

1. **exactly one push** - `git push <validated remote> refs/heads/<branch>:refs/heads/<remote_branch>`

Everything else T21 runs is read-only (`rev-parse`, `rev-list`, `status`,
`diff`, `symbolic-ref`, `show-ref`, `config --get-*`, `var`, `merge-base`,
`ls-remote`, `log`, `ls-files`, `ls-tree`, `cat-file`). The boundary is enforced
in code:

* `ALLOWED_GIT_SUBCOMMANDS` is an allow-list; any other subcommand raises
  `GitPushError` **before** a process is launched.
* `FORBIDDEN_GIT_SUBCOMMANDS` names the destructive/network operations
  explicitly - including `add`/`commit` (T20 owns them) and `fetch`/`pull`/
  `clone` - so T21 can never perform T20's write or fetch objects.
* A `git push` must carry exactly `<remote> <refspec>`: no option, no `--`, no
  force token, no wildcard, no deletion, and both refspec sides fully qualified,
  so neither `push.default` nor a `remote.<name>.push` rule can redirect it.
* `-c` overrides are never passed (`_ALLOWED_CONFIG_OVERRIDES` is empty), and
  `git config` is accepted only with a **read verb** (`--get`, `--get-all`,
  `--get-regexp`, `--list`), so a scope option alone (`--local`) can never be a
  disguised configuration write.
* Repository-redirecting options (`-C`, `--git-dir`, `--work-tree`,
  `--namespace`, `--exec-path`) are refused in every spelling, including the
  `--option=value` and attached (`-C/path`) forms.
* The argv is validated **again** immediately before the launch, so the command
  that runs is provably the command the gates approved.
* Git children run with a sanitized environment: every unsafe `GIT_*` variable
  is refused *and* removed, and `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`
  and `GIT_PAGER=cat` are forced. The caller's environment mapping is never
  mutated, `shell=False` is used everywhere, and Git output is
  credential-redacted before it reaches a reason, an observation or an exception.

### 12.2 The 48 gates (G1-G48)

`push_policy.GATES` is the authoritative list; every gate has exactly one
evaluator (`_GATE_EVALUATORS`, asserted against `GATES` on import) and exactly
one phase (`_GATE_PHASE`). A gate is `NOT_REACHED` until its phase has run,
`PASS` when its fact proves the condition, and `FAIL` (fail closed) otherwise;
the decision is `REFUSE` as soon as any evaluated gate fails, so the report
identifies **every** refusal instead of only the first one.

| Gates | Phase (state-machine stage) | Condition |
|-------|-----------------------------|-----------|
| G1 | `INPUTS` (S0) | the request names a repository, an expected commit, a source branch, a remote and a destination branch |
| G2-G5 | `CONTRACT` (S1) | the supplied T20 result reports `COMMITTED` with a full commit id, the request expects exactly that commit, and the T20 result is internally consistent (status, flags, heads, repository root and branch) |
| G6-G9 | `REPOSITORY` (S3) | a usable, non-bare working copy whose resolved root is the expected repository, whose index lives inside its own Git directory, and whose HEAD is not detached |
| G10 | `ENVIRONMENT` (S2) | no unsafe Git environment variable is set (checked before any Git process) |
| G11-G15 | `BRANCH` (S4) | a valid branch is checked out, it is the expected source branch, it is inside the agent namespace, and it is neither protected nor the default branch |
| G16-G18 | `COMMIT` (S5) | HEAD is exactly the recorded commit, that commit exists locally, and it is the tip of the source branch (so a later commit cannot ride along) |
| G19-G21 | `WORKTREE` (S6) | the working tree is clean, nothing is staged, and no other Git operation is in progress (no index lock) |
| G22-G26 | `REMOTE` (S7) | the destination remote name is well formed, the remote exists, its push configuration is unambiguous (one URL, no alternate push URL, no rewrite rule, no default push refspec), its URL is readable/well formed, and it embeds no credentials |
| G27-G36 | `TARGET` (S8) | the destination branch is valid, not protected, not the default and inside the agent namespace; the refspec is exactly the one T21 builds, fully qualified, wildcard-free, deletion-free, force-free, option-free and maps the source branch to the destination branch |
| G37-G38 | `ANCESTRY` (S9) | the push adds exactly the one recorded commit (or creates the destination branch), and the remote's current value is a proven ancestor, so the push cannot rewrite remote history |
| G39-G42 | `CHECKPOINT` (S10) | HEAD, the branch and the worktree/index are unchanged across two consecutive pre-push readings, and every pre-push gate (G1-G41) passed in the final checkpoint |
| G43 | `VERIFY` (S11) | the single push command reported success |
| G44-G46 | `VERIFY` (S12) | the remote was read back with a read-only command, the destination branch points at the recorded commit, and the update is exactly the expected fast-forward |
| G47-G48 | `VERIFY` (S13) | the local repository is unchanged after the push, and the push result is internally consistent (one `push` token, no broadening option, the remote and the refspec present, and verification in step with the push outcome) |

### 12.3 The state machine (each stage runs only when every earlier gate passed)

| Stage | Name | Gates | What it does |
|-------|------|-------|--------------|
| S0 | `VALIDATE_INPUTS` | G1 | requires an existing repository directory (otherwise `GitPushError`) and builds the immutable `PushRequest`; a destination branch is **never** defaulted |
| S1 | `CHECK_T20_CONTRACT` | G2-G5 | projects the T20 result read-only (it is never mutated) |
| S2 | `CHECK_ENVIRONMENT` | G10 | inspects the process environment *before* any Git process |
| S3 | `IDENTIFY_REPOSITORY` | G6-G9 | resolves the root, Git dir, index and HEAD with read-only Git |
| S4 | `CHECK_BRANCH` | G11-G15 | reads the checked-out branch and resolves the default branch from `refs/remotes/<remote>/HEAD` (never from the network) |
| S5 | `CHECK_COMMIT` | G16-G18 | proves HEAD is the recorded commit and is the branch tip |
| S6 | `CHECK_WORKTREE` | G19-G21 | reads `status --porcelain -z`, the staged set and the operation markers |
| S7 | `INSPECT_REMOTE` | G22-G26 | reads the remote configuration and the destination ref with `ls-remote` (no fetch, no object transfer) |
| S8 | `RESOLVE_TARGET` | G27-G36 | fixes the exact branch and refspec; the option list is empty by construction |
| S9 | `PROVE_ANCESTRY` | G37-G38 | counts the commits the push would add and proves a fast-forward with `merge-base --is-ancestor` |
| S10 | `FINAL_PRE_PUSH_CHECK` | G39-G42 | takes two consecutive checkpoints; if anything moved, the push is not launched |
| S11 | `PUSH` | G43 | the single and only mutating command |
| S12 | `VERIFY_REMOTE` | G44-G46 | reads the destination ref back with `ls-remote` |
| S13 | `VERIFY_LOCAL` | G47-G48 | re-reads the local state and checks the result is internally consistent |

### 12.4 The push argv and the refspec

* The command is always `[git, "-C", <resolved root>, "push", <remote>, <refspec>]`
  - `shell=False`, argv-only, and the `-C <root>` prefix is built *outside* the
  validated argument list.
* The refspec is built, never accepted:
  `refs/heads/<branch>:refs/heads/<remote_branch>`. `build_refspec` refuses an
  invalid branch name, and `parse_refspec` *reports* (rather than hides) a `+`
  force prefix or a `*` wildcard so the gates can refuse them by name.
* `HEAD`, a short branch name, a single ref, a deletion (`src:`/`:dst`), a tag, a
  remote name, a wildcard, a forcing refspec and more than one mapping are all
  refused.
* The destination remote is contacted read-only until S11 and read-only again
  after it: `ls-remote --heads <remote> <ref>` transfers no objects and writes
  nothing, so T21 never fetches.

### 12.5 Outcomes and failure semantics

| `PushStatus` | Meaning | Remote state |
|--------------|---------|--------------|
| `PUSHED` | exactly one push ran and the destination branch was read back at the recorded commit | the destination branch moved from its recorded value to the recorded commit |
| `REFUSED` | a gate refused before the push; not a single `git push` was attempted | untouched |
| `PUSH_FAILED` | the single `git push` attempt ran (or could not be launched) and failed | untouched by T21; T21 never forces, retries or fetches afterwards |
| `PUSH_UNVERIFIED` | a push ran but its effect on the remote could not be proven locally | possibly updated; a human must inspect it |

Failure handling is deliberately **passive**: on a push failure T21 does not
force, retry, fetch, pull, reset, clean or unstage. The remote and the working
copy are left exactly as they are and the result says so. Only genuine
caller/environment errors (a missing repository path, a path that is not a
directory, a forbidden command shape, a Git launch failure) raise
`GitPushError`; an unusable repository, remote or commit is a `REFUSED` result.

`PushResult` is typed, immutable and secret-free (`as_dict()` carries the status,
reason, the full state-machine trail, the request, the repository, the remote,
the recorded push attempt, the pre/post checkpoints, the commit boundary, the
refspec and all 48 gate verdicts), so T22+ can report it without leaking
anything.

### 12.6 Policy configuration (defaults are the conservative choice)

| `PushPolicyConfig` | Default | Effect |
|--------------------|---------|--------|
| `protected_branches` | `main`, `master`, `develop`, `trunk` | never pushed to **or** from |
| `default_branch` | `None` | additionally refused as a destination; when `None` the executor resolves it from `refs/remotes/<remote>/HEAD` and still refuses a mismatch (an unresolvable default branch refuses rather than guesses) |
| `required_branch_prefix` | `ai/sonar-fix` | both the source and the destination branch must live in this namespace (T07 owns branch creation) |
| `require_clean_worktree` | `True` | refuse unless the worktree and index are clean after the T20 commit |
| `allow_new_remote_branch` | `True` | the destination branch may be created at the recorded commit; an existing destination must fast-forward by exactly the one recorded commit |

### 12.7 Integration contract with T20 (and why T21 does not call it)

* T21 is handed three things: the repository path T20 worked in, the T20
  `CommitResult` object (or its `as_dict()` view) and the destination remote
  branch. Everything else is derived from the T20 result, so the push cannot
  target anything else.
* T21 **does not call T20** and does not re-derive the fix: it *reads* the
  result. A bare commit id is never accepted as proof - G2, G3 and G4 read the
  T20 status, the T20 commit id and the T20 repository/branch and refuse
  otherwise, so an out-of-band commit cannot ride along.
* T20 stores the *resolved* worktree root; T21 resolves the same path before
  comparing it, and G7 refuses when the resolved root is not the expected
  repository.
* The boundary is one-way: `git_push`/`push_policy` never import `git_commit`,
  and neither is wired into `main.py`. Nothing pushes automatically.
* **Both shapes are contractual and are exercised against real code**
  (`tests/test_t20_t21_integration.py`): a genuine `GitCommitExecutor` run in a
  temporary repository produces a genuine `CommitResult`, which is then handed to
  `GitPushExecutor.push_safely` **twice** — once as the `CommitResult` object and
  once as `CommitResult.as_dict()` — and both times exactly one push runs and the
  local bare remote's destination branch is read back at the T20 commit.
* The same file **pins the projection**: the exact key paths
  `git_push._project_t20` reads out of `CommitResult.as_dict()` (`status`,
  `is_committed`, `new_head`, `commit_sha`, `previous_head`, `needs_attention`,
  `repository.is_valid`, `repository.worktree_root` / `repository.expected_root`,
  `repository.branch`) are asserted to exist and to project onto the documented
  `PushFacts` fields, so renaming or dropping one without updating T21 fails
  loudly instead of silently turning every push into a refusal.
* **Still the orchestrator's responsibility (open):** nothing in T20 or T21
  *derives* the repository path, the source branch, the destination branch, the
  remote name or the destination branch name from T07/T19. They are supplied
  out-of-band, and the future orchestrator (T22+) must supply them consistently.
  T21 only bounds them (an agent-namespace source **and** destination branch that
  is neither protected nor the default branch) — no shared run context exists,
  and none is claimed here.

### 12.8 Acceptance criteria

1. **Given** a T20 `COMMITTED` result on an agent branch, a clean worktree, a
   resolvable remote and one commit ahead of the destination branch, **when**
   T21 runs, **then** exactly one `git push` is launched, the destination branch
   moves from the recorded value to the recorded commit, the remote is read back
   and confirmed, and the local repository (HEAD, branch, index, worktree, local
   configuration, other remote-tracking refs) is unchanged.
2. **Given** a destination branch that does not exist yet **when** T21 runs
   **then** exactly one push creates it at the recorded commit.
3. **Given** any refusal condition (a non-`COMMITTED` T20 result, an expected
   commit that is not the T20 commit, a bare or non-repository path, a detached
   HEAD, a protected, non-agent or default source/destination branch, a dirty or
   staged worktree, an in-progress operation, a missing, ambiguous,
   credential-bearing or unreadable remote, a hostile refspec, an already-pushed
   commit, a remote that moved out of band, an unsafe environment, or a move
   detected between the two pre-push checkpoints) **then** the result is
   `REFUSED`, the remote is untouched, and **not a single** `git push` (nor any
   other mutating command) is launched.
4. **Given** a push that fails or times out **then** the status is
   `PUSH_FAILED`, exactly one attempt was made, and no force, retry, fetch, pull,
   reset or clean command ran.
5. **Given** a push that reports success but cannot be confirmed **then** the
   status is `PUSH_UNVERIFIED` and the remote is reported loudly for a human.
6. **Given** an argv T21 is not allowed to run (a forbidden subcommand, a
   redirecting global option, an unapproved `-c` override, a configuration write,
   a push option, a force token, a wildcard, a deletion or a
   non-fully-qualified refspec) **then** `GitPushError` is raised **before** any
   process is launched.
7. **Given** a hostile process environment (`GIT_DIR`, `GIT_WORK_TREE`, ...)
   **then** the run refuses with `REFUSED` before launching any Git process, and
   the caller's environment mapping is not mutated.


## 13. T22 — overall report (implemented, not wired)

T22 answers one question: *what happened overall after processing one or more
SonarQube issues?* It is a **pure aggregation/reporting layer** over the evidence
T19, T20 and T21 already produced:

```
T19 IssueStatusResult  ->  T20 CommitResult  ->  T21 PushResult  ->  T22 OverallReport
```

| Module | Role | Side effects |
|--------|------|--------------|
| `overall_report.py` | immutable input/result DTOs, the state machine, the 20 gates, the aggregation, `as_dict()`/`serialize_report` | **none** |

### 13.1 Purpose, scope and non-goals

* T22 consumes the results of T19/T20/T21; it never produces evidence itself.
* It does **not** decide whether an issue *should* have been fixed, and it never
  infers success from missing evidence.
* It runs **no** Git command, no SonarQube call, no Codex run, no project test,
  no subprocess, no network request, and it never touches the filesystem or a
  repository. `overall_report` imports `json` and `re` only, and projects the
  results through the same `as_dict()` views T21 uses (never `asdict()`, never a
  private attribute).
* **It is not wired into `main.py`**: `main.py` is byte-for-byte unchanged.
* It does **not** implement T23 (per-issue reporting), the T24/T25 limits (which
  are separate policy modules, §15–§16) or T26–T28 (protections), it does not
  implement T29–T32 (logging/containers/CI), and it introduces no `RunContext`.

### 13.2 Inputs

```python
IssueLifecycleInput(issue_key, issue_status, commit_result=None, push_result=None)
```

Neither the T19 result nor the T20/T21 results carry the issue key, so the caller
pairs them explicitly; T22 never derives a key from a commit message, a branch or
a path. `build_overall_report(entries=..., policy=..., forbidden_secrets=...)`
accepts any iterable of `IssueLifecycleInput` **or** of mappings that carry
exactly `issue_key`, `issue_status`, `commit_result`, `push_result` (an unknown
field is refused), so the serialized `as_dict()` views can be fed back in.

Each result may be the production DTO **or** its `as_dict()` view; a value that
projects to neither is a gate failure, never a guess.

### 13.3 The state machine (S0-S11; a failure stops the run)

| Phase | Gates | Invariant established |
|-------|-------|-----------------------|
| S0 `INPUT` | — | the input container was received |
| S1 `INPUT_VALIDATED` | G1-G3 | every entry has a safe, unique issue key |
| S2 `ISSUE_RESULTS_VALIDATED` | G4, G5 | every T19 result is usable |
| S3 `LIFECYCLE_RESULTS_VALIDATED` | G6-G9 | every T20/T21 result is usable |
| S4 `ISSUE_OUTCOMES_AGGREGATED` | G15 | the T19 counts sum to the total |
| S5 `COMMIT_OUTCOMES_AGGREGATED` | G16 | the T20 counts sum to the T20 results |
| S6 `PUSH_OUTCOMES_AGGREGATED` | G17 | the T21 counts sum to the T21 results |
| S7 `CROSS_STAGE_CONSISTENCY_CHECKED` | G10-G14 | the T20/T21 evidence is not contradictory |
| S8 `OVERALL_STATUS_RESOLVED` | — | the overall status is derived from the counts |
| S9 `REPORT_BUILT` | G18 | the built report satisfies its own invariants |
| S10 `REPORT_VERIFIED` | G19, G20 | the report is JSON-safe and secret-free |
| S11 `COMPLETE` | — | the report is returned |

The trail is part of the report (`OverallReportState.stage_records`,
`reached_phases`, `phase`, `failed_phase`, `gates`, `status_of`, `first_failure`).
Every gate is **always** present: a gate whose phase did not run is
`NOT_REACHED`, and a phase failure never lets a later phase run. The authored gate
numbers are not the execution order — the cross-stage gates kept the numbers
G10-G14 but run in S7, after the aggregation phases that own G15-G17 (exactly as
T20's G28/STAGING runs before G29-G37/REPOSITORY); `GATE_PHASE` is the executable
record of the execution order.


### 13.4 The 20 gates (G1-G20)

| Gate | Checks |
|------|--------|
| G1 | the input is an iterable collection of entries (not `None`, a bare string/bytes, a mapping or a number) |
| G2 | every entry is an `IssueLifecycleInput`/entry mapping and carries a safe, non-empty, ≤100-character SonarQube-shaped issue key |
| G3 | no two entries declare the same issue key |
| G4 | every T19 result projects to a mapping with a well-formed `status`, and its `is_fixed`/`needs_review` flags (when present) agree with it |
| G5 | every T19 `status` is one of the seven production `IssueFinalStatus` values |
| G6 | every supplied T20 result projects to a mapping with a well-formed `status`, a boolean `is_committed`/`needs_attention`, and full-commit-id `new_head`/`commit_sha`/`previous_head` when present |
| G7 | every supplied T20 `status` is one of the four production `CommitStatus` values |
| G8 | every supplied T21 result projects to a mapping with a well-formed `status`, boolean flags, and full-commit-id `expected_commit`/`remote_before_commit`/`remote_after_commit` when present |
| G9 | every supplied T21 `status` is one of the four production `PushStatus` values |
| G10 | T20 agrees with itself: `is_committed`/`needs_attention` match the status, a `COMMITTED` result carries `new_head == commit_sha` and `new_head != previous_head` |
| G11 | T21 agrees with itself: `is_pushed`/`is_refusal`/`needs_attention` match the status, a `PUSHED` result was attempted and moved the remote to exactly `expected_commit`, an unverified push was attempted, a refusal was not, and the refspec matches its source and destination branches |
| G12 | a push attempt names exactly the commit T20 created |
| G13 | a push attempt is rooted in a `COMMITTED` T20 result with a usable id and the same branch |
| G14 | the lifecycle is coherent: no T20 evidence for an issue T19 did not verify as fixed, no T21 evidence without a T20 result, and no commit message that names a different issue key |
| G15 | the T19 status counts sum to the number of classified issues |
| G16 | the T20 status counts sum to the number of supplied T20 results |
| G17 | the T21 status counts sum to the number of supplied T21 results |
| G18 | `verify_overall_report` confirms every count, ordering and classification invariant of the **built** report |
| G19 | the payload is plain JSON data and survives a `json.dumps`/`json.loads` round trip |
| G20 | the serialized report matches none of the caller's configured secrets (and no credential-bearing URL) |

An unrecognised status, a malformed result, a contradiction, a count violation, an
unserializable payload and a secret are all **refusals**: they are never silently
counted, never repaired and never mapped onto the nearest status.

### 13.5 Overall status semantics

| `OverallStatus` | Meaning |
|-----------------|---------|
| `EMPTY` | no entry was supplied — never `SUCCESS` |
| `SUCCESS` | every entry reached the policy's required end state |
| `PARTIAL_SUCCESS` | at least one entry did, and at least one did not |
| `FAILED` | nothing reached it and nothing needs review |
| `REVIEW_REQUIRED` | the report is invalid, or nothing succeeded and at least one entry could not be classified safely |

`is_valid` describes the *report*, not the run: it is `True` whenever every gate
passed and every count invariant held (a single `PUSH_UNVERIFIED` issue is a valid
report whose status is `REVIEW_REQUIRED`). A gate failure always yields
`is_valid = False`, `status = REVIEW_REQUIRED` and the failed gates named in
`validation_errors`. `needs_attention` is `True` for any report that is not a
clean `SUCCESS`/`EMPTY`, and for any issue that asked for it. `ReportDecision` is
`PROCEED` only for `SUCCESS`.


### 13.6 Issue outcomes and the count invariants

Each classified issue gets one compact `IssueOverallSummary` (deliberately **not**
the T23 per-issue report): the issue key, the three *source* statuses verbatim,
T22's outcome, the three derived booleans, `needs_attention`, the commit id, the
pushed commit and T22's own reason sentence. Summaries are sorted by issue key.

| `IssueOutcome` | Meaning |
|----------------|---------|
| `DELIVERED` | the issue reached the policy's required end state |
| `NOT_DELIVERED` | it is fixed, but a required later stage was refused or failed |
| `NOT_FIXED` | T19 did not verify the fix (a definite, unambiguous failure) |
| `REVIEW_REQUIRED` | the evidence is ambiguous, missing or unverified |

The classification ladder is: **uncertainty first** (a `REVIEW_REQUIRED` T19, a
`COMMIT_UNVERIFIED` T20 or a `PUSH_UNVERIFIED` T21 is never a success and never a
failure) → a non-`FIXED` T19 is `NOT_FIXED` → otherwise the policy's required end
state decides, where a required stage that produced **no** result is
`REVIEW_REQUIRED` (missing evidence is never assumed) while a stage that ran and
resolved is `NOT_DELIVERED` → only a proven end state is `DELIVERED`.

The report validates itself (`verify_overall_report`, the body of G18) before it
is returned:

* `total_issues == successful_issues + failed_issues + review_required_issues ==
  len(issue_outcomes)`, and `total_issues <= input_entries`;
* `sum(issue_counts) == total_issues`, `sum(commit_counts) == commit_result_count`,
  `sum(push_counts) == push_result_count`, and each count map always covers the
  **complete** production status vocabulary;
* `commit_result_count + commit_not_executed_count == total_issues` (and the same
  for push) — "not executed" is the *absence* of a T20/T21 result, never a T20/T21
  status;
* the summaries are sorted by issue key and unique;
* the decision, `needs_attention`, the state's decision and the overall status all
  agree with the counts.

### 13.7 Cross-stage consistency (the lifecycle cases)

"Fixed" is not "committed" and "committed" is not "pushed": an issue can be
`FIXED` but not `COMMITTED`, or `FIXED` + `COMMITTED` but not `PUSHED`, and those
are reported as such (`issue_fixed`, `change_committed`, `change_pushed`).

| Case | Evidence | T22 |
|------|----------|-----|
| A | `FIXED` + `COMMITTED` + `PUSHED` | `DELIVERED`; with the default policy the run is `SUCCESS` |
| B | `FIXED` + `COMMITTED` + `PUSH_UNVERIFIED` | `REVIEW_REQUIRED` for that issue (never `SUCCESS`) |
| C | `FIXED` + T20 `REFUSED` + no T21 | valid lifecycle, `NOT_DELIVERED` |
| D | a T20 result for a T19 status that is not `FIXED` | contradictory → G14 → the report is refused |
| E | T20 `COMMITTED` without a usable `new_head`/`commit_sha` | G10 → refused |
| F | T21 `PUSHED` without `expected_commit` | G11 → refused |
| G | T21 push evidence although T20 did not verify a commit | G13 → refused |
| H | `push.expected_commit != t20.new_head` | G12 → refused |
| I | `remote_after_commit != expected_commit` | G11 → refused |
| — | T21 evidence without any T20 result | G14 → refused |
| — | `FIXED` + missing T20 while the policy requires commit evidence | `REVIEW_REQUIRED` (never an assumed commit) |
| — | `FIXED` + `COMMITTED` + missing T21 while the policy requires push evidence | `REVIEW_REQUIRED` (never an assumed push) |

No contradiction is ever resolved by choosing the more optimistic reading, and no
result is ever rewritten: the source statuses are preserved verbatim in every
summary and in `issue_counts`/`commit_counts`/`push_counts`.

### 13.8 Policy configuration (defaults are the conservative choice)

| `OverallReportPolicy` | Default | Effect |
|-----------------------|---------|--------|
| `expected_end_state` | `PUSHED` | an issue only reaches `DELIVERED` when `FIXED` + `COMMITTED` + `PUSHED`; `requires_commit_evidence`/`requires_push_evidence` are derived from it (`COMMITTED` needs a T20 result, `ISSUE_FIXED` needs neither) |

The policy is part of the report (`policy.as_dict()`), so a report always explains
which end state its classification used.

### 13.9 Determinism, serialization and secret safety

* The same input always produces the same report, the same `as_dict()` and the
  same `serialize_report()` text. Issue summaries are sorted by issue key; gate
  problems are de-duplicated and sorted per gate; there is no timestamp and no
  unordered iteration anywhere in the output.
* `as_dict()` returns only approved report fields as plain JSON data (str/int/
  bool/None/list/dict), so `json.dumps(report.as_dict())` always works.
  `serialize_report()` is canonical (`sort_keys=True`, no cosmetic whitespace).
* **Secret safety is structural**: T22 never copies free-form upstream text (no
  reasons, no stage trails, no gate verdicts, no remote URLs), only statuses,
  counts, full commit ids and issue keys that matched the strict key shape. G20
  then scans the *serialized* report against the caller's configured secrets
  (`forbidden_secrets`, normally `AnalysisConfig.secrets`) plus URL-userinfo
  shapes, and a match refuses the report — the returned failure report never
  echoes the offending value.
* An unexpected internal error is converted into the same fail-closed refusal
  (naming only the exception *type*), never into a success and never into a
  traceback.


### 13.10 Output DTO

```python
OverallReport(
    status, is_valid, decision, policy,
    input_entries, total_issues,
    successful_issues, failed_issues, review_required_issues,
    issue_counts, commit_counts, push_counts,
    commit_result_count, commit_not_executed_count,
    push_result_count, push_not_executed_count,
    issue_outcomes, reasons, validation_errors, state, report_version,
)
```

`status`/`decision`/`needs_attention`/`end_to_end_*_count`/`issue_keys`/
`generated_from` are derived, so they can never disagree with the counts. Every
DTO is a frozen dataclass and every sequence is a tuple; `state` carries all 20
gate verdicts and the phase trail. `report_version` is `t22.1`.

### 13.11 Test mapping

| Test file | Covers |
|-----------|--------|
| `tests/test_overall_report.py` | aggregation (none/one/many/mixed), every T19/T20/T21 status and count, the end-to-end success path, partial success, fixed-but-not-committed, committed-but-not-pushed, missing T20/T21 evidence under each policy, every cross-stage contradiction (G10-G14), unknown statuses, duplicate/invalid identities, gap-1 input shapes, the count invariants and their monkeypatched self-check seams (G15/G18/G19/G20), determinism and ordering, secret safety, immutability, the full state machine, canonical serialization, and the **real** `GitCommitExecutor`/`GitPushExecutor` handoff in a `tmp_path` clone (report built from genuine DTOs, repository provably untouched) |
| `tests/test_overall_report_policy.py` | gate catalogue and `GATE_PHASE` integrity (including that a failure leaves every later phase's gates `NOT_REACHED`), policy defaults/derived requirements, the collection/identity/value/payload helpers, the failure-report contract, and that the module imports no execution dependency |
| `tests/t22_fixtures.py` | non-collected fixtures: real T19 statuses, real T20/T21 DTOs, their `as_dict()` views and the corrupt variants a real DTO cannot express |

### 13.12 Integration status

* T20 is called by nothing in production: `git_commit` is exercised only by tests.
* T21 is handed a T20 result explicitly and is called by nothing in production.
* **T22 is a library with no caller**: `main.py` is byte-for-byte unchanged, no
  orchestrator exists, `RunContext` remains deferred (F2), and T23+ is untouched.
* T22 performs no Git/Sonar/Codex/network/filesystem mutation of any kind; it
  consumes already-produced evidence only.

### 13.13 Acceptance criteria

1. **Given** the results of a completed T19/T20/T21 run for one issue, **when**
   `build_overall_report` is called, **then** it returns an immutable,
   deterministic, JSON-serializable, secret-free `OverallReport` and executes
   nothing.
2. **Given** `FIXED` + `COMMITTED` + `PUSHED` for every issue, **then** the status
   is `SUCCESS`; **given** a `PUSH_UNVERIFIED`, `COMMIT_UNVERIFIED`, unknown
   status, missing required evidence or contradictory evidence anywhere, **then**
   the status is never `SUCCESS`.
3. **Given** no entries, **then** the status is `EMPTY` with `is_valid = True` and
   it is not `SUCCESS`.
4. **Given** malformed, duplicate or unknown-status input, **then** the status is
   `REVIEW_REQUIRED`, `is_valid` is `False`, the failed gate is named in
   `validation_errors`, and no issue is classified (the counts are all zero and
   `input_entries` still records the input size).
5. **Given** a report that violates its own count/classification invariants,
   **then** `verify_overall_report` reports it and the report is never returned as
   valid.


## 14. T23 — per-issue report (implemented, not wired)

T23 answers one question: *what exactly happened to THIS SonarQube issue?* It is a
**pure reporting layer** over the evidence one issue's lifecycle already produced:

```
SonarIssue (T04/T08) + T19 IssueStatusResult -> T20 CommitResult -> T21 PushResult
                                             -> T23 PerIssueReport
```

T22 aggregates many issues; T23 explains exactly one. They are independent
reporting layers: T22 does not depend on T23, T23 does not depend on T22 (and
neither one is imported by the other).

| Module | Role | Side effects |
|--------|------|--------------|
| `per_issue_report.py` | immutable input/result DTOs, the state machine, the 21 gates, the derivation, `as_dict()`/`serialize_report` | **none** |

### 14.1 Purpose, scope and non-goals

* T23 consumes the `SonarIssue` identity plus the results of T19/T20/T21 for **one**
  issue; it never produces evidence itself and never decides whether an issue
  *should* have been fixed.
* It runs **no** Git command, no SonarQube call, no Codex run, no project test, no
  subprocess, no network request, and it never touches the filesystem or a
  repository. The module imports `json` and `re` plus the production evidence
  modules only, and projects the results through the same `as_dict()` views
  T21/T22 use (never `asdict()`, never a private attribute, never a `repr()`).
* It never rewrites a source status: `t19_status`, `t20_status` and `t21_status`
  are the stages' own values, published verbatim next to the derived outcome.
* **It is not wired into `main.py`**: `main.py` is byte-for-byte unchanged.
* It does **not** implement the T24/T25 limits (separate policy modules, §15–§16),
  it does not implement T26–T28 (protections) or T29–T32
  (logging/containers/CI), it introduces no `RunContext`, and it adds no
  orchestration, no policy object, no email/HTML/Markdown reporting and no
  dashboard.

### 14.2 Inputs

```python
PerIssueInput(issue_key, issue=None, issue_status=None,
              commit_result=None, push_result=None)
```

`issue` is the real `models.SonarIssue` (or a mapping carrying exactly its eight
fields) — the T04/T08 identity record T23 reports as `rule`, `file_path`,
`line`, `severity`, `issue_type` and `message`. `issue_status`/`commit_result`/
`push_result` are the production DTOs **or** their `as_dict()` views; `None` means
the stage produced no result.

`build_per_issue_report(entry=..., forbidden_secrets=...)` accepts a
`PerIssueInput` **or** a mapping that carries exactly `issue_key`, `issue`,
`issue_status`, `commit_result`, `push_result` (an unknown field is refused, so a
duck-typed blob cannot slip through). A value that is neither is a fail-closed
refusal (`G1`), never a guess. `forbidden_secrets` that is a bare string or not
an iterable raises `PerIssueReportError` (a caller error, not evidence).

**Trust boundary.** T23 accepts the production result DTOs **or** their projected
`as_dict()` mappings, and it treats the statuses those records carry as
*caller-asserted evidence*: it never re-runs T19/T20/T21, and it never
authenticates the provenance of a mapping. What it validates is the supplied
evidence's own internal consistency, its lifecycle consistency and its cross-stage
coherence — so T23 is a **validation/reporting boundary, not an
evidence-authentication boundary**, and a fully self-consistent fabricated mapping
can technically satisfy it. That does not weaken the contract: `SUCCESS` still
requires `FIXED` + `COMMITTED` + `PUSHED` with every gate passing, so no fabricated
report is ever a *cheaper* success than the evidence it mimics. A future
orchestration or security layer that needs trusted provenance remains responsible
for establishing it outside T23.

### 14.3 The state machine (S0-S10; a failure stops the run)

| Phase | Gates | Invariant established |
|-------|-------|-----------------------|
| S0 `INPUT` | — | the input container was received |
| S1 `INPUT_VALIDATED` | G1 | the input is a usable per-issue input |
| S2 `ISSUE_IDENTITY_VALIDATED` | G2, G3 | the issue identity is safe and unambiguous |
| S3 `T19_VALIDATED` | G4, G5 | the T19 result is usable |
| S4 `T20_VALIDATED` | G6, G7 | the T20 result is usable (or absent) |
| S5 `T21_VALIDATED` | G8, G9 | the T21 result is usable (or absent) |
| S6 `CROSS_STAGE_CONSISTENCY_CHECKED` | G10-G15 | the T19/T20/T21 evidence is not contradictory |
| S7 `DERIVED_OUTCOME_RESOLVED` | — | the outcome is derived from the evidence |
| S8 `REPORT_BUILT` | G16-G19 | the built report satisfies its own invariants |
| S9 `REPORT_VERIFIED` | G20, G21 | the report is JSON-safe and secret-free |
| S10 `COMPLETE` | — | the report is returned |

The trail is part of the report (`PerIssueReportState.stage_records`,
`reached_phases`, `phase`, `failed_phase`, `gates`, `status_of`, `first_failure`).
Every gate is **always** present: a gate whose phase did not run is
`NOT_REACHED`, a phase failure never lets a later phase run, and `phase` is always
the last *completed* phase while `failed_phase` is the phase that aborted the
report (so `failed_phase` never appears in `reached_phases`). `GATE_PHASE` is the
executable record of the ownership, and it runs in declaration order.

### 14.4 The 21 gates (G1-G21)

| Gate | Checks |
|------|--------|
| G1 | the input is a `PerIssueInput` or a mapping of exactly the input contract's fields (not `None`, a bare string, a number or an unknown-field blob) |
| G2 | the declared key is a safe, non-empty, ≤100-character SonarQube-shaped key, and the issue record is a `SonarIssue`/mapping of exactly its eight fields with a usable `key`, `rule`, `component` and a strictly positive `line` (when set); `severity`/`issue_type`/`status`/`message` must be single-line, control-character-free, ≤400-character text when present |
| G3 | the declared key and the issue record name the same issue |
| G4 | the T19 result projects to a mapping with a well-formed `status`, boolean `is_fixed`/`needs_review` flags and well-formed nested evidence: a mapping `verification` (booleans, `original_issue_key` text, a recognised `match_type`/`correlation`), a mapping `analysis` (a recognised completion `status` and `task_id`), a mapping `trigger_evidence` (a boolean `triggered` and `task_id`), and boolean `scope`/`tests`/`codex` blocks |
| G5 | the T19 `status` is one of the seven production `IssueFinalStatus` values |
| G6 | a supplied T20 result projects to a mapping with a well-formed `status`, boolean flags, full-commit-id `new_head`/`commit_sha`/`previous_head` when present, a mapping `commit_message` (text `issue_key`), a mapping `repository` (text `branch`) and a mapping `gates` whose `first_failure` is a `G<number>` identifier |
| G7 | a supplied T20 `status` is one of the four production `CommitStatus` values |
| G8 | a supplied T21 result projects to a mapping with a well-formed `status`, boolean `is_pushed`/`is_refusal`/`needs_attention`/`push_attempted`, full-commit-id `expected_commit`/`remote_before_commit`/`remote_after_commit` when present, text `branch`/`remote_branch`/`refspec`, a mapping `remote` (text `name`, boolean `exists`) and a `gates.first_failure` |
| G9 | a supplied T21 `status` is one of the four production `PushStatus` values |
| G10 | the T19 result agrees with itself: the flags match the status, `reliable_absence` follows from its own inputs, the verification key matches the reported issue, and a `FIXED` result cannot coexist with negative Codex/scope/test/verification evidence, an open issue, an uncorrelated snapshot, a failed analysis or an unidentifiable trigger (each terminal failure status is checked against the one piece of evidence that must not say the opposite) |
| G11 | T20 agrees with itself: `is_committed`/`needs_attention` match the status, and a `COMMITTED` result carries `new_head == commit_sha` and `new_head != previous_head` |
| G12 | T21 agrees with itself: `is_pushed`/`is_refusal`/`needs_attention`/`push_attempted` match the status, and a `PUSHED` result was attempted, carries the commit it pushed, and reports a final remote tip equal to `expected_commit`; when both the before and after remote tips are supplied, they must differ (a verified push that did not move the remote is a contradiction, while an absent `remote_before_commit` is not) |
| G13 | a push attempt is rooted in a `COMMITTED` T20 result with a usable id and names exactly the commit T20 created; the T20 commit message names the reported issue |
| G14 | the branch and remote evidence is coherent: the T20 and T21 branches agree, the refspec matches its source and destination branches, and a push attempt carries a refspec and an existing remote |
| G15 | the lifecycle order is respected: no T21 result without the T20 result it consumes, and no commit claimed for an issue T19 did not verify as fixed (a refusal or a failed commit *is* coherent with a non-`FIXED` T19 and stays reportable) |
| G16 | the published `issue_fixed` follows from the published T19 status |
| G17 | the published `change_committed` follows from the published T20 status |
| G18 | the published `change_pushed` follows from the published T21 status |
| G19 | the published outcome, `end_to_end_success`, `needs_attention`, report version and gate catalogue all follow from the evidence (independently recomputed) |
| G20 | the payload is plain JSON data and survives a `json.dumps`/`json.loads` round trip |
| G21 | the serialized report matches none of the caller's configured secrets (and no credential-bearing URL) |

An unrecognised status, a malformed result, a contradiction, a broken derivation,
an unserializable payload and a secret are all **refusals**: they are never
silently counted, never repaired and never mapped onto the nearest status.

### 14.5 Lifecycle outcome semantics

| `PerIssueOutcome` | Meaning |
|-------------------|---------|
| `SUCCESS` | `FIXED` + `COMMITTED` + `PUSHED`, and every gate passed |
| `FAILED` | T19 definitively did not verify a fix (`STILL_OPEN`, `ANALYSIS_FAILED`, `TESTS_FAILED`, `SCOPE_INVALID`, `CODEX_FAILED`) |
| `NOT_COMPLETED` | the fix was verified, but the lifecycle did not reach the end |
| `REVIEW_REQUIRED` | the evidence is ambiguous — or the whole report is invalid |

`issue_fixed`, `change_committed` and `change_pushed` stay separate: `FIXED` is
not `COMMITTED`, `COMMITTED` is not `PUSHED`, and `end_to_end_success` is `True`
only for `SUCCESS`. A `COMMIT_UNVERIFIED`/`PUSH_UNVERIFIED` result is always
`REVIEW_REQUIRED`, never a success and never a failure. `needs_attention` is
`True` for `REVIEW_REQUIRED` and `NOT_COMPLETED`, and also whenever the T20/T21
result's own `needs_attention` flag is `True`; a definite `FAILED` does not need a
human (T19 already decided it).

### 14.6 Missing evidence, ambiguity and the source statuses

A stage that produced no result is reported as **not executed**, and is never
assumed to have succeeded or to have been refused:

* `t20_status`/`t21_status` are `None`, `commit_result_supplied`/
  `push_result_supplied` are `False`, the matching `reasons` line says the stage
  supplied no result, and the phase trail records "not executed";
* a `FIXED` issue whose commit or push stage produced no result is
  `NOT_COMPLETED` — never `SUCCESS`, and never a definite `FAILED` either, because
  the absence is unambiguous but proves nothing about what the stages did;
* an *ambiguous* stage (`REVIEW_REQUIRED`, `COMMIT_UNVERIFIED`,
  `PUSH_UNVERIFIED`) is `REVIEW_REQUIRED`.

An invalid report is the strongest fail-closed state: `outcome` is
`REVIEW_REQUIRED`, `is_valid` is `False`, `needs_attention` is `True`, **no**
per-stage status is published (all three are `None`), every derived flag is
`False`, and `validation_errors` names the failed gates. The only facts such a
report keeps are the *validated* issue identity fields, so it still says which
issue it is about; an identity that could not be validated (or that could not be
proven secret-free) is published blank (`issue_key=""`, every other field `None`).

### 14.7 Cross-stage consistency (the lifecycle cases)

| Case | Evidence | T23 |
|------|----------|-----|
| A | `FIXED` + `COMMITTED` + `PUSHED` | `SUCCESS`, `end_to_end_success = True` |
| B | `FIXED` + `COMMITTED` + `PUSH_UNVERIFIED` | `REVIEW_REQUIRED` (never `SUCCESS`) |
| C | `FIXED` + `COMMIT_UNVERIFIED` | `REVIEW_REQUIRED` |
| D | `FIXED` + `REFUSED` / `COMMIT_FAILED` | `NOT_COMPLETED` |
| E | `STILL_OPEN` (or any definite failure) + `REFUSED` | `FAILED`, both statuses preserved verbatim |
| F | `FIXED` + no T20 result | `NOT_COMPLETED` (never an assumed commit) |
| G | `FIXED` + `COMMITTED` + no T21 result | `NOT_COMPLETED` (never an assumed push) |
| H | `push.expected_commit != t20.new_head` | G13 → refused |
| I | `remote_after_commit != expected_commit` | G12 → refused |
| J | `COMMITTED` without a usable `new_head`/`commit_sha` | G11 → refused |
| K | T21 push evidence although T20 did not verify a commit | G13 → refused |
| L | T21 evidence without any T20 result | G15 → refused |
| M | a commit claimed for an issue T19 did not verify as fixed | G15 → refused |
| N | a T20/T21 result whose flags contradict its status | G11/G12 → refused |

No contradiction is ever resolved by choosing the more optimistic reading, and no
result is ever rewritten: the source statuses are preserved verbatim in every
report.

### 14.8 Determinism, serialization and secret safety

* The same evidence always produces the same report, the same `as_dict()` and the
  same `serialize_report()` text. Gate problems are de-duplicated and sorted per
  gate; there is no timestamp, no random id and no unordered iteration anywhere in
  the output; the issue identity is reported verbatim.
* `as_dict()` returns only approved report fields as plain JSON data
  (str/int/bool/None/list/dict), so `json.dumps(report.as_dict())` always works.
  `serialize_report()` is canonical (`sort_keys=True`, no cosmetic whitespace).
* **Secret safety is structural**: T23 is built by explicit field projection and
  never copies free-form upstream text — no T19/T20/T21 reasons, no stage trails,
  no gate reasons, no remote URLs, no captured output. The only *unclassifiable*
  free-form field it publishes is the Sonar issue `message`, which is bounded
  (≤400 characters), single-line and control-character-free. The other externally
  supplied identity/status fields it publishes (`rule`, `component`, `severity`,
  `issue_type`) and the Sonar `status` it validates (`G2`) are bounded and
  validated in exactly the same way, but they are classified values — rule ids,
  component names, severities, types and statuses — rather than free-form text;
  `line` is a strictly positive integer. `G21` then scans the *serialized* report
  against the caller's configured secrets (`forbidden_secrets`, normally
  `AnalysisConfig.secrets`) plus URL-userinfo shapes, and a match refuses the
  report — and because a failure report skips the serialization gates, the
  identity is only published on that path when it can be proven secret-free (a
  credential-bearing identity is dropped and the reason is recorded). No message,
  gate reason or validation error ever echoes the offending value.
* An unexpected internal error is converted into the same fail-closed refusal
  (naming only the exception *type*), never into a success and never into a
  traceback.

### 14.9 Output DTO

```python
PerIssueReport(
    outcome, is_valid, needs_attention,
    issue_key, rule, file_path, component, line, severity, issue_type, message,
    t19_status, t20_status, t21_status,
    verification, analysis,
    commit_sha, previous_head, branch, commit_message_issue_key,
    t20_gate_failure, commit_needs_attention,
    expected_commit, remote_before_commit, remote_after_commit,
    remote_branch, remote, refspec, t21_gate_failure, push_needs_attention,
    issue_fixed, change_committed, change_pushed, end_to_end_success,
    reasons, validation_errors, state, report_version,
)
```

`VerificationEvidence` carries the T18 evidence (`issue_key`,
`retrieval_succeeded`, `is_present`, `identity_reliable`, `page_complete`,
`reliable_absence`, `match_type`, `correlation`); `AnalysisEvidence` carries the
T17/T16 analysis evidence (`completion_status`, `completion_task_id`, `triggered`,
`trigger_task_id`). `commit_result_supplied`/`push_result_supplied`, `is_fixed`
and `generated_from` are derived, so they can never disagree with the statuses.
Every DTO is a frozen dataclass with tuple sequences; `state` carries all 21 gate
verdicts and the phase trail. `report_version` is `t23.1`.

### 14.10 Test mapping

| Test file | Covers |
|-----------|--------|
| `tests/test_per_issue_report.py` | the success path and every derived state, every T19/T20/T21 status and the outcome each implies, missing T20/T21 evidence, unknown statuses, malformed and ambiguous identities, every cross-stage contradiction (G10-G15) including conflicting commit ids, conflicting T20/T21 evidence and invalid branch/remote evidence, the derivation seams (G16-G19) and the serialization/secret gates (G20/G21), canonical serialization, secret safety (a configured secret, a credential URL and their failure-path dropping), immutability, determinism, the full state machine, a matrix that exercises every fail-closed gate, and the **real** `GitCommitExecutor`/`GitPushExecutor` handoff in a `tmp_path` clone (a report built from genuine DTOs, with the repository provably untouched afterwards) |
| `tests/test_per_issue_report_policy.py` | gate catalogue and `GATE_PHASE` integrity (including that a failure leaves every later phase's gates `NOT_REACHED`), the outcome ladder and its 112-combination sweep, the field/reader/payload helpers, the failure-report contract, the gate bookkeeping, the module surface, and an AST check that the module's code references no execution primitive |
| `tests/t23_fixtures.py` | non-collected fixtures: real T19 statuses, real T20/T21 DTOs, a real `SonarIssue`, their `as_dict()` views and the corrupt variants a real DTO cannot express |

### 14.11 Integration status

* **T23 is a library with no caller**: `main.py` is byte-for-byte unchanged, no
  orchestrator exists, `RunContext` remains deferred (F2), and T26+ is untouched
  (the T24/T25 limits are separate, independent policy layers — §15, §16 — and
  nothing imports them either).
* T23 consumes already-produced evidence only: it performs no Git/Sonar/Codex/
  network/filesystem mutation of any kind, and the integration test proves a real
  clone is unchanged after a report is built from it.
* T22 and T23 remain independent report layers: neither imports the other, and
  T22's behaviour is untouched.

### 14.12 Acceptance criteria

1. **Given** a completed T19/T20/T21 lifecycle for one issue, **when**
   `build_per_issue_report` is called, **then** it returns an immutable,
   deterministic, JSON-serializable, secret-free `PerIssueReport` and executes
   nothing.
2. **Given** `FIXED` + `COMMITTED` + `PUSHED`, **then** the outcome is `SUCCESS`;
   **given** `COMMIT_UNVERIFIED`, `PUSH_UNVERIFIED`, an unknown status, missing
   required evidence or contradictory evidence anywhere, **then** the outcome is
   never `SUCCESS`.
3. **Given** a missing T20 or T21 result for a `FIXED` issue, **then** the outcome
   is `NOT_COMPLETED` and the stage is reported as not executed (never an assumed
   commit or push, and never `SUCCESS`).
4. **Given** malformed, contradictory or unknown evidence, **then** `is_valid` is
   `False`, the outcome is `REVIEW_REQUIRED`, no per-stage status is published, the
   failed gate is named in `validation_errors`, and the state machine's
   `failed_phase` records where it stopped.
5. **Given** a report that violates its own derivation invariants, **then**
   `verify_per_issue_report` reports it and the report is never returned as valid.

## 15. T24 — max issue limit (implemented policy layer, not wired)

T24 answers exactly one question and nothing else:

> "may this run process the issue selection it has been handed?"

It is a **pure policy / validation layer**, not orchestration: it retrieves no
issue, selects no issue, mutates nothing, and it performs no Git, SonarQube,
Codex, subprocess, network or filesystem operation. `main.py` is byte-for-byte
unchanged and **nothing calls T24 yet**.

| Module | Role | Side effects |
|--------|------|--------------|
| `issue_limit.py` | the immutable policy / selection / verdict records, the validation, the decision ladder and `as_dict()` | **none** |

### 15.1 Purpose, scope and non-goals

* **Purpose**: give a future orchestration layer one deterministic, fail-closed
  answer to "how many issues may this run process?", so a run can never process
  more issues than the operator configured.
* **In scope**: configuration validation, selection validation, the limit
  decision, the deterministic order of the checks, and the exact reason.
* **Out of scope**: retrieving issues (T02/T18), filtering them (T03/T04),
  choosing *which* issues to fix (no ranking, no scoring, no prioritisation —
  the caller's order is preserved verbatim), truncating a selection, processing
  an issue, retrying (T25, §16), reporting (T22/T23) and orchestration.
* **Trust boundary**: both inputs are **caller-asserted**. T24 re-runs nothing
  and authenticates nothing: it validates the internal consistency of a
  selection the caller already made and enforces the configured bound. A caller
  that misreports how many issues it retrieved, or what it selected, is not
  detected here. This is a policy boundary, not an evidence-authentication
  boundary — the same distinction §14.2 documents for T23.

### 15.2 The contract

```python
IssueLimitPolicy(max_issue_limit=None)                               # configuration
IssueSelection(discovered_issue_count=None, selected_issue_keys=())  # caller input
evaluate_issue_limit(*, policy, selection) -> IssueLimitEvaluation   # verdict
```

`max_issue_limit` is the maximum number of issues allowed to **enter the
processing pipeline for one run**, so an accepted selection always satisfies

```
selected_issue_count <= max_issue_limit
```

The bound is applied to the *selected* count (`len(selected_issue_keys)`) and
never to the discovered/retrieved count, which is only the bound the selection
may not exceed. Keeping the two inputs separate is what makes "retrieved N
issues" impossible to mistake for "processed N issues".

T24 **never truncates**: an oversized selection is refused
(`IssueLimitStatus.EXCEEDED`). For an accepted selection the caller's own keys
are echoed back in a fresh tuple, in the caller's own order, so the verdict
states exactly which issues may enter the pipeline. Truncating would change
which issues a run processes without the caller deciding it; the caller must
either select fewer issues or raise the limit deliberately.

`policy_version` is `t24.1`; the module exposes `POLICY_VERSION`,
`DEFAULT_MAX_ISSUE_LIMIT` (`None`), `MAX_ISSUE_LIMIT` (`1_000_000`),
`ALLOWED_STATUSES` and `PRECEDENCE`.

### 15.3 Configuration validation (exact behaviour)

`None` means **not configured, and therefore invalid** (option B). It is *not*
read as "unlimited": an unconfigured safety bound must not widen what a run may
do, so T24 refuses (`INVALID_CONFIG`) until an operator states an explicit
number. `DEFAULT_MAX_ISSUE_LIMIT` is `None` for exactly that reason.

| `max_issue_limit` | Usable? | Reason (deterministic) |
|-------------------|---------|------------------------|
| `None` | no — `INVALID_CONFIG` | "No max_issue_limit is configured, and an unconfigured limit is not read as unlimited." |
| `True` / `False` | no — `INVALID_CONFIG` | "max_issue_limit must be a non-negative integer, not bool." |
| `1.5`, `"3"`, `[]`, any non-integer | no — `INVALID_CONFIG` | "max_issue_limit must be a non-negative integer, not float." (the type name is published, never the value) |
| `-1`, `-100` | no — `INVALID_CONFIG` | "max_issue_limit must not be negative (got -1)." |
| `0` | **yes** | validated: the strictest explicit bound — only an empty selection fits |
| `1` … `1_000_000` | yes | validated |
| `1_000_001`, `10 ** 9` | no — `INVALID_CONFIG` | "max_issue_limit 1000001 exceeds the supported maximum 1000000." |

A `bool` is checked *before* `int` (`isinstance(value, bool) or not
isinstance(value, int)`), so Python's `bool`-is-`int` behaviour can never let
`True` mean "one issue". Exactly the same rule is used for every count in this
module and for T25's iteration numbers.

### 15.4 Selection validation (exact behaviour)

| Field | Usable? | Reason |
|-------|---------|--------|
| `discovered_issue_count=None` | no — `INVALID_DISCOVERED_COUNT` | "No discovered_issue_count was supplied, so the selection cannot be shown to be a subset of what was retrieved." |
| `True`/`False`, `1.5`, `"5"` | no — `INVALID_DISCOVERED_COUNT` | "discovered_issue_count must be a non-negative integer, not str." |
| negative | no — `INVALID_DISCOVERED_COUNT` | "discovered_issue_count must not be negative (got -3)." |
| `0`, any positive integer | yes | validated (counts are never capped: only the *limit* is a policy knob) |
| `selected_issue_keys` a `set`/`frozenset`/`dict`/`str`/`bytes`/iterator | no — `INVALID_SELECTION` | "selected_issue_keys must be an ordered sequence of issue keys, not set: a set, mapping, string or iterator has no deterministic order." |
| `selected_issue_keys` a list/tuple with a non-string or empty entry | no — `INVALID_SELECTION` | "Every selected issue key must be a non-empty string: the selection carries an entry of another kind." |
| a repeated key | no — `DUPLICATE_ISSUES` | "The selection repeats an issue key, so its count would not describe distinct issues." |
| a list/tuple of non-empty strings, each key once | yes | validated, in the caller's order (key *shape* is not validated here: identity belongs to T22/T23) |

An unordered collection is refused because T24 must preserve an order it cannot
invent; nothing in this module sorts, deduplicates or ranks.

### 15.5 Decision model (statuses, precedence and the state machine)

T24 has no multi-stage state machine: one call walks a fixed ladder of eight
checks and stops at the first one whose condition holds. That order is
`PRECEDENCE`, and it is the authoritative contract:

| # | Status | Condition | Decision |
|---|--------|-----------|----------|
| 1 | `INVALID_CONFIG` | the policy states no usable bound | `REFUSE` |
| 2 | `INVALID_DISCOVERED_COUNT` | the discovered count is unusable | `REFUSE` |
| 3 | `INVALID_SELECTION` | the keys are not an ordered sequence of non-empty strings | `REFUSE` |
| 4 | `DUPLICATE_ISSUES` | a key appears twice | `REFUSE` |
| 5 | `IMPOSSIBLE_COUNTS` | `selected_issue_count > discovered_issue_count` | `REFUSE` |
| 6 | `EXCEEDED` | `selected_issue_count > max_issue_limit` | `REFUSE` |
| 7 | `AT_LIMIT` | `selected_issue_count == max_issue_limit` | `ALLOW` |
| 8 | `WITHIN_LIMIT` | `selected_issue_count < max_issue_limit` | `ALLOW` |

`ALLOWED_STATUSES` is exactly `(AT_LIMIT, WITHIN_LIMIT)`; every other status is
`REFUSE`, and a configuration that cannot bound anything is refused *before* any
evidence is considered. When several conditions hold at once the earliest row
decides: an invalid policy wins over an invalid selection, an unordered
selection wins over a repeated key (which cannot even be read), a repeated key
wins over an impossible pair of counts, and impossible counts win over the limit.

### 15.6 Edge cases (all pinned by tests)

* **Empty selection** — accepted by every valid policy, including
  `max_issue_limit=0`: `accepted_issue_count` is `0` and
  `remaining_issue_slots == max_issue_limit`.
* **Zero limit** — valid and strict. With `max_issue_limit=0` the empty
  selection is `AT_LIMIT` and *any* non-empty selection is `EXCEEDED`; issues can
  never pass a zero bound.
* **Limit exactly reached** — `AT_LIMIT`: accepted, `remaining_issue_slots == 0`
  and `can_accept_more is False`, so an orchestrator knows the pipeline is full.
* **Selection above the limit** — `EXCEEDED`, `accepted_issue_count == 0`,
  `accepted_issue_keys == ()`, and the trail names the whole selection — a
  refusal never hides how many issues were offered.
* **Discovered == selected** — valid (the selection is the whole retrieved set).
* **Discovered > selected** — valid; the retrieved volume is never counted as
  processed.
* **Selected > discovered** — impossible, `IMPOSSIBLE_COUNTS`.
* **Huge values** — the *limit* is capped at `MAX_ISSUE_LIMIT` (a larger value is
  a configuration error), while `discovered_issue_count` is uncapped: it is
  caller-asserted evidence, not a policy knob.
* **`None`, `bool`, `float`, `str`** — never coerced, never accepted as `0`.

### 15.7 Fail-closed behaviour

An unusable bound, an unusable count, an unusable selection, a repeated key and
a contradictory pair of counts are all **refusals**: no silent pass, no repair,
no normalisation (`"3"` is not `3`, `True` is not `1`, `0` is not "the first
issue", an unordered set is not sorted for convenience). A refusal always carries
`decision == REFUSE`, `accepted_issue_count == 0`, `accepted_issue_keys == ()`,
`remaining_issue_slots is None`, `can_accept_more is False` and a decisive
`reason`, so a caller that ignores the fields still cannot mistake a refusal for
an acceptance.

A non-DTO argument (`policy=None`, `selection=None`) raises `TypeError` instead:
that is a programming error, not an evidence problem.

### 15.8 Determinism, ordering and caller safety

* The verdict is a pure function of `(policy, selection)`: no clock, environment
  value, random source, ordering of a set/dict or module state is read, so
  re-evaluating the same inputs returns an **equal** record (`==`) and an equal
  `as_dict()`.
* The caller's key order is preserved verbatim — `accepted_issue_keys` is
  `("ZZZ-2", "AAA-1")` for that input, never a sorted copy — and no ranking is
  invented.
* The caller's collection is copied into a tuple; it is never mutated, and the
  returned tuple is never the caller's object.
* Every record (`IssueLimitPolicy`, `IssueSelection`, `IssueLimitEvaluation`) is a
  frozen dataclass: assigning a field raises `FrozenInstanceError`, the policy is
  unchanged after an evaluation, and `evaluate_issue_limit` has exactly two
  keyword-only parameters, so no third input (a status, an engine, a pipeline
  object) can influence it.

### 15.9 Serialization and secret safety

`as_dict()` is JSON-safe, deterministic and **counts only** —
`accepted_issue_keys` is deliberately absent, because a limit decision is about
*how many* issues may enter the pipeline, not *which* ones. Consequently no
caller-supplied key (or a secret-looking value that was passed as one) can be
published by this module, and that is structural rather than a filter:

* the evaluation publishes counts, booleans, the validated bound, the reason and
  the policy view;
* an **unusable** policy value is never echoed — `IssueLimitPolicy(max_issue_limit="squ_...").as_dict()`
  publishes `max_issue_limit: None` and a refusal reason that names the *type*;
* refusal reasons name counts and type names only, never caller text;
* `IssueSelection.as_dict()` publishes `discovered_issue_count` (validated) and
  `selected_issue_key_count`, never the keys.

### 15.10 Output DTO

`IssueLimitEvaluation` carries: `policy_version`, `status`, `decision`,
`is_allowed`, `limit_exceeded`, `max_issue_limit`, `discovered_issue_count`,
`selected_issue_count`, `accepted_issue_count`, `accepted_issue_keys`,
`remaining_issue_slots`, `can_accept_more`, `reason`, `reasons` and `policy`
(the configuration, so the verdict explains itself). Derived fields
(`is_allowed`, `limit_exceeded`) are properties, so they can never disagree with
`status`.

```json
{
  "policy_version": "t24.1",
  "status": "at-limit",
  "decision": "allow",
  "is_allowed": true,
  "limit_exceeded": false,
  "max_issue_limit": 2,
  "discovered_issue_count": 7,
  "selected_issue_count": 2,
  "accepted_issue_count": 2,
  "remaining_issue_slots": 0,
  "can_accept_more": false,
  "reason": "The selection of 2 issue(s) exactly reaches the limit of 2 issue(s), so no further issue may be added.",
  "reasons": [
    "The selection of 2 issue(s) exactly reaches the limit of 2 issue(s), so no further issue may be added.",
    "The pipeline is full: T24 never accepts more than the configured bound."
  ],
  "policy": {
    "policy_version": "t24.1",
    "max_issue_limit": 2,
    "maximum_supported_limit": 1000000,
    "is_valid": true,
    "refusal_reason": null
  }
}
```

### 15.11 Safety guarantees

1. `accepted_issue_count <= max_issue_limit` for **every** accepted selection
   (pinned by a sweep over limits, counts and selection sizes).
2. A zero limit never lets an issue through.
3. A negative, `None`, bool, float, string or oversized limit is refused.
4. Invalid configuration fails closed even for an empty selection.
5. A missing configuration follows the documented safe policy: refuse.
6. `bool` is never silently accepted as an integer.
7. The discovered count can never be mistaken for the selected count.
8. Impossible counts (`selected > discovered`) are rejected.
9. Serialization is deterministic and JSON-safe.
10. The caller's collection is never mutated and never reordered.
11. No network, Git, SonarQube, Codex, subprocess or filesystem operation exists
    in the module (asserted by an AST inspection of its own source).
12. No caller-supplied value (or secret) is ever serialized.

### 15.12 Examples

A future orchestrator holds the numbers `main.py` already produces today
(`total` and the T03/T04-selected issues) and asks once per run:

```python
>>> from issue_limit import (IssueLimitPolicy, IssueSelection,
...                          evaluate_issue_limit)
>>> decision = evaluate_issue_limit(
...     policy=IssueLimitPolicy(max_issue_limit=2),
...     selection=IssueSelection(discovered_issue_count=7,
...                              selected_issue_keys=("P:1", "P:2")),
... )
>>> decision.status
<IssueLimitStatus.AT_LIMIT: 'at-limit'>
>>> decision.is_allowed, decision.accepted_issue_keys
(True, ('P:1', 'P:2'))
>>> evaluate_issue_limit(
...     policy=IssueLimitPolicy(max_issue_limit=2),
...     selection=IssueSelection(discovered_issue_count=7,
...                              selected_issue_keys=("P:1", "P:2", "P:3")),
... ).reason
'The selection of 3 issue(s) exceeds the limit of 2 issue(s), so the run is refused.'
>>> evaluate_issue_limit(
...     policy=IssueLimitPolicy(),
...     selection=IssueSelection(discovered_issue_count=7,
...                              selected_issue_keys=("P:1",)),
... ).status
<IssueLimitStatus.INVALID_CONFIG: 'invalid-config'>
>>> evaluate_issue_limit(
...     policy=IssueLimitPolicy(max_issue_limit=1),
...     selection=IssueSelection(discovered_issue_count=7,
...                              selected_issue_keys=["P:1", "P:2"]),
... ).status
<IssueLimitStatus.EXCEEDED: 'exceeded'>
>>> evaluate_issue_limit(
...     policy=IssueLimitPolicy(max_issue_limit=1),
...     selection=IssueSelection(discovered_issue_count=7,
...                              selected_issue_keys={"P:1", "P:2"}),
... ).status
<IssueLimitStatus.INVALID_SELECTION: 'invalid-selection'>
```

### 15.13 Test mapping

| Test file | Covers |
|-----------|--------|
| `tests/test_issue_limit.py` | configuration validation (usable bounds, `0`, negative, `None`, bool, float, string, oversized, default), selection validation (unusable discovered counts, set/mapping/bare-string/iterator selections, non-text keys, repeated keys, impossible counts), the acceptance rule (below/at/above the limit, `limit=1`, zero limit, empty selection, a sweep proving `accepted <= limit`), the discovered-vs-selected confusion cases, the capped limit versus the uncapped counts, the exact `PRECEDENCE` and one conflict per row, status reachability and the `ALLOWED_STATUSES` <=> `ALLOW` equivalence, determinism, ordering, caller-collection safety, immutability, canonical JSON serialization, secret safety (a decoy secret can never be published), the helper tables, and the module surface (exact `__all__`, standard-library-only imports, no execution primitive, exactly two keyword-only parameters) |

### 15.14 Integration status

* **T24 is a library with no caller.** `main.py` is byte-for-byte unchanged and
  no production path imports `issue_limit`.
* The module imports the standard library only (`dataclasses`, `enum`, `typing`)
  and has no dependency on T19–T23, so nothing about the existing pipeline can
  change because of it.
* T24 is a limit/policy boundary only: it is **not** orchestration, and it
  executes no Git/SonarQube/Codex operation.

### 15.15 Acceptance criteria

1. **Given** a configured positive limit and a selection at or below it, **when**
   `evaluate_issue_limit` is called, **then** the verdict is `ALLOW` with
   `accepted_issue_keys` equal to the caller's keys in the caller's order.
2. **Given** a selection larger than the limit, **then** the verdict is
   `EXCEEDED` and the selection is refused — never truncated.
3. **Given** `None`, a bool, a float, a string, a negative or an oversized
   limit, **then** the verdict is `INVALID_CONFIG` (fail closed) whatever the
   selection is.
4. **Given** an unordered, non-text, repeated or self-contradictory selection,
   **then** the verdict is `INVALID_SELECTION`, `DUPLICATE_ISSUES` or
   `IMPOSSIBLE_COUNTS` respectively.
5. **Given** identical inputs, **then** the verdict, its `as_dict()` and the
   caller's collection are byte-for-byte identical after the call.

## 16. T25 — max iteration limit (implemented policy layer, not wired)

T25 answers exactly one question and nothing else:

> "may the caller execute this iteration of this issue's fix attempt?"

It is a **pure policy / validation layer**: it owns no counter, runs nothing,
retries nothing, and performs no Git, SonarQube, Codex, subprocess, network or
filesystem operation. `main.py` is byte-for-byte unchanged and **nothing calls
T25 yet**.

| Module | Role | Side effects |
|--------|------|--------------|
| `iteration_limit.py` | the immutable policy / attempt / verdict records, the validation, the decision ladder and `as_dict()` | **none** |

### 16.1 Purpose, scope and non-goals

* **Purpose**: prevent one SonarQube issue from entering an unbounded AI-fix
  retry loop, by answering "may this attempt run?" deterministically.
* **In scope**: configuration validation, attempt validation, the exact
  eligibility rule, the deterministic order of the checks and the exact reason.
* **Out of scope**: executing Codex (T10), interpreting its result (T11),
  classifying an attempt (T19), deciding *whether* to retry (no orchestration),
  counting attempts (the caller owns the counter) and reporting (T22/T23).
* **Trust boundary**: both inputs are **caller-asserted**. T25 counts nothing and
  authenticates nothing: a caller that misreports which iteration it is on is not
  detected here. This is a policy boundary, not an evidence-authentication
  boundary.

### 16.2 The contract

```python
IterationLimitPolicy(max_iterations=None)                # configuration
IterationAttempt(current_iteration=None)                 # caller input
evaluate_iteration_limit(*, policy, attempt) -> IterationLimitEvaluation
```

`max_iterations` is the maximum number of attempts one issue may receive, so

```
1 <= current_iteration <= max_iterations    is eligible for execution
current_iteration > max_iterations          is refused
```

`policy_version` is `t25.1`; the module exposes `POLICY_VERSION`,
`DEFAULT_MAX_ITERATIONS` (`None`), `MAX_ITERATIONS` (`1_000`),
`ALLOWED_STATUSES` and `PRECEDENCE`.

### 16.3 Validation (exact behaviour)

| `max_iterations` | Usable? | Reason |
|------------------|---------|--------|
| `None` | no — `INVALID_CONFIG` | "No max_iterations is configured, and an unconfigured retry limit is not read as unlimited." |
| `True` / `False` | no — `INVALID_CONFIG` | "max_iterations must be a non-negative integer, not bool." |
| `1.5`, `"3"`, any non-integer | no — `INVALID_CONFIG` | "max_iterations must be a non-negative integer, not float." (type name only) |
| `-1`, `-100` | no — `INVALID_CONFIG` | "max_iterations must not be negative (got -1)." |
| `0` | **yes** | validated: the strictest explicit bound — no attempt is ever allowed |
| `1` … `1_000` | yes | validated |
| `1_001`, `10 ** 9` | no — `INVALID_CONFIG` | "max_iterations 1001 exceeds the supported maximum 1000." |

`None` again means **not configured, therefore invalid** — the same safe
interpretation T24 documents (§15.3), so neither limit can ever be read as
"unlimited". A `bool` is rejected before the `int` check.

| `current_iteration` | Usable? | Reason |
|---------------------|---------|--------|
| `None` | no — `INVALID_ITERATION` | "No current_iteration was supplied, so no attempt is authorised." |
| `True`/`False`, `1.5`, `"1"` | no — `INVALID_ITERATION` | "current_iteration must be an integer of at least 1, not str." |
| `0`, `-1`, `-5` | no — `INVALID_ITERATION` | "current_iteration 0 is not an attempt: iteration numbers start at 1." |
| `1`, `2`, … | yes | validated (never capped: a huge attempt number is simply beyond the limit) |

### 16.4 Decision model (statuses, precedence and the exact truth table)

One call walks a fixed ladder of four checks and stops at the first whose
condition holds — `PRECEDENCE` is the authoritative order:

| # | Status | Condition | Decision |
|---|--------|-----------|----------|
| 1 | `INVALID_CONFIG` | the policy states no usable bound | `REFUSE` |
| 2 | `INVALID_ITERATION` | the attempt number is unusable | `REFUSE` |
| 3 | `EXCEEDED` | `current_iteration > max_iterations` | `REFUSE` |
| 4 | `WITHIN_LIMIT` | `current_iteration < max_iterations` | `ALLOW` |
| 5 | `AT_LIMIT` | `current_iteration == max_iterations` | `ALLOW` |

`ALLOWED_STATUSES` is exactly `(WITHIN_LIMIT, AT_LIMIT)`. Rows 4 and 5 are
mutually exclusive, so five statuses cover the whole domain. The verdict's flags
are derived, so they cannot contradict the status:

| Case | `status` | `decision` | `attempt_allowed` | `next_iteration_allowed` | `limit_reached` | `remaining_iterations` |
|------|----------|-----------|-------------------|--------------------------|-----------------|------------------------|
| `max=2, current=1` | `WITHIN_LIMIT` | `ALLOW` | `True` | `True` | `False` | `1` |
| `max=2, current=2` | `AT_LIMIT` | `ALLOW` | `True` | `False` | `True` | `0` |
| `max=2, current=3` | `EXCEEDED` | `REFUSE` | `False` | `False` | `True` | `0` |
| `max=1, current=1` | `AT_LIMIT` | `ALLOW` | `True` | `False` | `True` | `0` |
| `max=1, current=2` | `EXCEEDED` | `REFUSE` | `False` | `False` | `True` | `0` |
| `max=0, current=1` | `EXCEEDED` | `REFUSE` | `False` | `False` | `True` | `0` |
| `max=3, current=0` | `INVALID_ITERATION` | `REFUSE` | `False` | `False` | `False` | `None` |
| `max=None, current=1` | `INVALID_CONFIG` | `REFUSE` | `False` | `False` | `False` | `None` |

`next_iteration_allowed` is exactly `attempt_allowed` for
`current_iteration + 1`, which the tests assert for every boundary, so
"may I loop again?" has one unambiguous answer.

### 16.5 Edge cases (all pinned by tests)

* **`max_iterations = 1`** — attempt 1 is `AT_LIMIT` (allowed) and attempt 2 is
  `EXCEEDED`. No off-by-one.
* **`max_iterations = 2`** — attempts 1 and 2 are allowed, attempt 3 is refused.
* **`max_iterations = 0`** — valid and strict: *no* attempt is ever allowed, and
  every attempt number `>= 1` is `EXCEEDED`.
* **`current_iteration = 0` or negative** — refused as "not an attempt", never
  normalised to attempt 1 and never silently reset to 0 progress.
* **`current_iteration > max_iterations`** — refused, and it stays refused however
  many times it is asked.
* **A huge `current_iteration`** — refused (`EXCEEDED`); the attempt number is
  caller-asserted evidence, so it is not capped.
* **`None`, `bool`, `float`, `str`** — never coerced, never accepted as `0`.

### 16.6 Retry safety (the guarantees this module exists for)

* **Reaching the limit prevents another attempt** — `max_iterations + 1` is
  `EXCEEDED`, permanently.
* **Failures never reset the counter** — `TESTS_FAILED`, `ANALYSIS_FAILED`,
  `CODEX_FAILED`, `STILL_OPEN` and `REVIEW_REQUIRED` carry no power here: T25
  imports no status vocabulary, has no parameter for an outcome, and refuses any
  value that could not be an attempt number.
* **A success does not raise the limit** — `max_iterations` is a frozen field and
  the verdict never writes to it, so attempt 3 stays refused after a successful
  attempt 2.
* **The policy cannot be bypassed by passing a different status** — there is no
  status input at all (`evaluate_iteration_limit(*, policy, attempt)` has exactly
  two keyword-only parameters), and the tests sweep every T19/T20/T21 status
  value proving that none of them is accepted as a bound or as an iteration.
* **No automatic reset, no implicit retry, no hidden increment** — the module
  exposes no `advance()`/`next()`/`reset()`/`increment()`, keeps no state between
  calls, and the counter is the caller's; T25 only judges the value it is given.

### 16.7 Fail-closed, determinism and secret safety

* An unusable bound or attempt is a **refusal**: no silent pass, no repair, no
  normalisation (`"1"` is not `1`, `True` is not `1`, `0` is not the first
  attempt). A refusal always carries `decision == REFUSE`, `attempt_allowed is
  False`, `next_iteration_allowed is False`, `remaining_iterations is None` and a
  decisive `reason`.
* A non-DTO argument raises `TypeError` (a programming error, not evidence).
* The verdict is a pure function of `(policy, attempt)`: no clock, environment
  value, random source or module state is read, the same inputs always produce an
  equal record (`==`) and an equal `as_dict()`, and repeated evaluation after any
  number of refusals changes nothing.
* `as_dict()` is JSON-safe and secret-free: it publishes the validated integers,
  the enums, the derived flags, the reason trail and the policy view. An unusable
  value is described by its **type name** only (never echoed), so no
  caller-authored text — and no secret passed where a number belonged — can be
  serialized by this module.

### 16.8 Output DTO

`IterationLimitEvaluation` carries: `policy_version`, `status`, `decision`,
`is_allowed`, `attempt_allowed`, `next_iteration_allowed`, `limit_reached`,
`max_iterations`, `current_iteration`, `remaining_iterations`, `reason`,
`reasons` and `policy`. `attempt_allowed` **is** `decision == ALLOW` and
`is_allowed` is its property alias, so a caller cannot read them as disagreeing.

### 16.9 Examples

```python
>>> from iteration_limit import (IterationAttempt, IterationLimitPolicy,
...                              evaluate_iteration_limit)
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=2),
...     attempt=IterationAttempt(current_iteration=1),
... ).status
<IterationLimitStatus.WITHIN_LIMIT: 'within-limit'>
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=2),
...     attempt=IterationAttempt(current_iteration=2),
... ).status
<IterationLimitStatus.AT_LIMIT: 'at-limit'>
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=2),
...     attempt=IterationAttempt(current_iteration=3),
... ).reason
'Iteration 3 exceeds the limit of 2 permitted iteration(s), so it is refused.'
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=1),
...     attempt=IterationAttempt(current_iteration=2),
... ).status
<IterationLimitStatus.EXCEEDED: 'exceeded'>
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=3),
...     attempt=IterationAttempt(current_iteration=0),
... ).status
<IterationLimitStatus.INVALID_ITERATION: 'invalid-iteration'>
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(),
...     attempt=IterationAttempt(current_iteration=1),
... ).status
<IterationLimitStatus.INVALID_CONFIG: 'invalid-config'>
>>> evaluate_iteration_limit(
...     policy=IterationLimitPolicy(max_iterations=2),
...     attempt=IterationAttempt(current_iteration=1),
... ).as_dict()["next_iteration_allowed"]
True
```

### 16.10 Test mapping

| Test file | Covers |
|-----------|--------|
| `tests/test_iteration_limit.py` | configuration validation (usable bounds, `0`, negative, `None`, bool, float, string, oversized, default), attempt validation (`None`, `0`, negative, bool, float, string), the exact boundary semantics for `max=1`/`max=2`, the full truth table of `attempt_allowed`/`next_iteration_allowed`/`limit_reached`/`remaining_iterations`, the `next_iteration_allowed == attempt(next)` invariant, the zero bound, huge attempt numbers, the exact `PRECEDENCE` and one conflict per row, status reachability, the retry guarantees (a scripted retry loop, a success that cannot raise the limit, repeated refusals, a sweep over **every** T19/T20/T21 status value proving none is accepted as a bound or an iteration), determinism, immutability, no counter/advance API, canonical JSON serialization, secret safety, the helper tables, and the module surface (exact `__all__`, standard-library-only imports with no status vocabulary, no execution primitive, exactly two keyword-only parameters) |

### 16.11 Integration status

* **T25 is a library with no caller.** `main.py` is byte-for-byte unchanged and
  no production path imports `iteration_limit`.
* The module imports the standard library only (`dataclasses`, `enum`, `typing`),
  so it is independent of T19's implementation and cannot change any existing
  behaviour. T19 remains the stage that *classifies* one attempt; T25 only
  answers whether a further attempt may start.
* T25 is a limit/policy boundary only: it is **not** orchestration, it does not
  re-invoke Codex, and it executes no Git/SonarQube/Codex operation.

### 16.12 Acceptance criteria

1. **Given** a configured bound and an attempt within it, **when**
   `evaluate_iteration_limit` is called, **then** `attempt_allowed is True` (and
   `AT_LIMIT` when it is the last attempt).
2. **Given** `current_iteration > max_iterations`, **then** the verdict is
   `EXCEEDED` however many times it is asked, with no counter mutation.
3. **Given** `None`, a bool, a float, a string, a negative or an oversized bound,
   **then** the verdict is `INVALID_CONFIG` for every attempt.
4. **Given** `None`, a bool, a float, a string, `0` or a negative attempt number,
   **then** the verdict is `INVALID_ITERATION` and the value is never normalised.
5. **Given** any T19/T20/T21 status value, **then** it can neither authorise an
   attempt beyond the limit nor be accepted as a bound or an iteration.
6. **Given** identical inputs, **then** the verdict is equal, its `as_dict()` is
   byte-for-byte identical and neither input record changed.

## 17. T24/T25 at a glance (both implemented; neither is wired)

* `issue_limit.py` (T24, `t24.1`) bounds **how many issues** one run may process;
  `iteration_limit.py` (T25, `t25.1`) bounds **how many attempts** one issue may
  receive. Both are pure: standard library only, frozen records, no I/O, no
  execution, no orchestration.
* Both take their values from the caller and validate *internal consistency and
  safety* only — neither authenticates anything (§15.1, §16.1).
* Both fail closed on `None`: an unconfigured limit is never "unlimited".
* Both refuse `bool`/`float`/`str`, never normalise an invalid value, and never
  echo caller-authored text in a reason or a serialized view.
* Neither is imported by `main.py`, T19–T23, or by the other one: T24 and T25 are
  independent, and `RunContext`/orchestration remain deferred.

## 18. T26 — main/default branch protection (implemented policy layer, not wired)

T26 answers exactly one question and nothing else:

> "is this Git branch safe to mutate for an AI-generated SonarQube fix?"

It is a **pure policy / decision layer**: it discovers nothing, runs no Git
command, authenticates nothing and mutates nothing. `main.py` is byte-for-byte
unchanged, T20/T21 keep their own inline branch gates (unchanged), and **nothing
calls T26 yet**.

| Module | Role | Side effects |
|--------|------|--------------|
| `branch_protection.py` | the immutable policy / input / verdict records, the branch-identity validation, the decision ladder and `as_dict()` | **none** |

### 18.1 Purpose, scope and non-goals

* **Purpose**: give a future orchestration layer one deterministic, fail-closed
  answer to "may this branch receive an AI-generated commit or push?", so the
  tool can never *accidentally* target `main`, `master`, `develop`, `trunk`, the
  repository's own default branch or a malformed branch identity.
* **In scope**: configuration validation (the protected set and the namespace
  flag), candidate/default-branch identity validation, the protection decision,
  the deterministic order of the checks, and the exact reason.
* **Out of scope**: discovering the default branch (that needs Git, so it belongs
  to orchestration — §18.4), creating/renaming/checking out a branch (T07 owns
  branch creation), committing (T20, §11), pushing (T21, §12), retrying,
  reporting (§13/§14), the limits (§15/§16) and `RunContext`.
* **Trust boundary**: both inputs are **caller-asserted**; T26 authenticates
  nothing and proves nothing (§18.11).

### 18.2 Security model — three rules plus one identity requirement

An AI-generated fix may never be committed to, or pushed to, a branch that is

1. a **protected** name (default: `main`, `master`, `develop`, `trunk`) — §18.3;
2. the repository's **default** branch, whatever it is called — §18.4;
3. an **unusable branch identity** (missing, empty, whitespace-padded, malformed
   or ref-like) — §18.6;

and, by default, the candidate must additionally live inside the T07 AI-fix
namespace `ai/sonar-fix/` — §18.5. A branch that clears every rule is allowed.

| Rule | The question it answers |
|------|-------------------------|
| identity | "is this a real branch name at all?" |
| protected set | "is this one of the names the operator protected?" |
| default branch | "is this the branch the repository considers primary?" |
| namespace | "was this branch created by T07 for this tool?" |

| `candidate` | `default` | prefix | protected | Result |
|-------------|-----------|--------|-----------|--------|
| `main` | `main` | on | standard | `PROTECTED_BRANCH` |
| `develop` | `main` | on | standard | `PROTECTED_BRANCH` |
| `feature/x` | `main` | on | standard | `WRONG_BRANCH_NAMESPACE` |
| `ai/sonar-fix/x` | `main` | on | standard | `ALLOWED` |
| `ai/sonar-fix/x` | `ai/sonar-fix/x` | on | standard | `DEFAULT_BRANCH` |
| `main/` | `main` | on | standard | `INVALID_BRANCH` |
| `main` | *(missing)* | on | standard | `MISSING_DEFAULT_BRANCH` |
| `ai/sonar-fix/x` | `main` | off | standard | `ALLOWED` |
| `feature/x` | `main` | off | `("feature/x",)` | `PROTECTED_BRANCH` |
| `feature/x` | `main` | off | `("main","MAIN")` | `INVALID_POLICY` |

T26 judges one **already-existing** branch: it creates nothing, renames nothing,
deletes nothing, checks nothing out and writes nothing — to Git or to any file.

### 18.3 Protected branches

`ProtectedBranchPolicy.protected_branches` defaults to `PROTECTED_BRANCH_NAMES` =
`("main", "master", "develop", "trunk")` — the same four names T20/T21 use
(§18.14 pins the equality). Requirements, all pinned by tests:

* **immutable internally**: the policy is a frozen dataclass and
  `protected_branch_keys` returns a fresh tuple of comparison keys, so an
  evaluation can never change the configuration and the configuration can never
  change an evaluation;
* **ordered and deterministic**: the configured order is preserved and no set
  decides a comparison — a `set`, `frozenset`, `Mapping`, bare `str` or `bytes`
  is refused as `INVALID_POLICY` (it has no deterministic order);
* `None` **never means "protect nothing"** — it is refused (`INVALID_POLICY`),
  because an unstated protection set must not widen what may be mutated;
* **an empty tuple is valid but strict**: it protects no *named* branch, while
  the default-branch rule (always) and the namespace rule (by default) still
  apply — so `main` with an empty set is still refused (`DEFAULT_BRANCH`), and
  `main` with an empty set *and* another default branch is still refused
  (`WRONG_BRANCH_NAMESPACE`);
* every entry must be a **usable branch name** (§18.6), and **no two entries may
  share a comparison key**: `("main", "MAIN")` is refused
  (`protected_branches repeats 'main'`) instead of being silently deduplicated;
* a malformed entry is named by **position** (`protected branch entry 2 ...`)
  and the offending value itself is never echoed.

### 18.4 The default branch (never inferred, never discovered)

`BranchProtectionInput.default_branch` is the repository's actual default branch
and is **supplied explicitly by the caller**. Every case is pinned:

| `default_branch` | Result |
|------------------|--------|
| a usable name equal (case-folded) to the candidate | refused `DEFAULT_BRANCH` |
| a usable name different from the candidate | not a refusal *for this reason* |
| `None` | refused `MISSING_DEFAULT_BRANCH` |
| `""`, whitespace-only, non-`str`, or a malformed/ref-like name | refused `MISSING_DEFAULT_BRANCH` |

There is deliberately **no switch to disable the default-branch requirement**: a
switch would turn missing information into permission to mutate, which is exactly
what T26 exists to prevent. `default_branch=None` is a refusal, never "anything
goes", and the default branch is never inferred from the candidate.

T26 **never** runs `git symbolic-ref`, `git remote`, `git config`, `git branch`
or `git ls-remote`, and never reads a ref file: discovering the default branch is
a Git operation and therefore belongs to a future orchestration layer. T21's
executor already resolves it from `refs/remotes/<remote>/HEAD`
(`git_push.py::_resolve_default_branch`, local refs only, no network); an
integration would compute it there, pass it to T26, and refuse to commit or push
on any refusal.

### 18.5 The AI-fix branch namespace

T07 creates agent branches in `ai/sonar-fix/` (`branch_naming.AGENT_BRANCH_PREFIX`,
re-exported by T26 as `AI_FIX_BRANCH_PREFIX`).
`ProtectedBranchPolicy.require_ai_fix_branch_prefix` defaults to `True`, and then
only a branch inside that namespace is eligible:

| candidate | prefix required (`True`, default) | prefix disabled (`False`) |
|-----------|-----------------------------------|---------------------------|
| `ai/sonar-fix/x` | eligible | eligible |
| `ai/sonar-fix/a/b` (nested, Git-valid) | eligible | eligible |
| `ai/sonar-fix` (the bare prefix) | `WRONG_BRANCH_NAMESPACE` | eligible |
| `feature/x`, `bugfix/x`, `user/x`, `ai/other/x`, `ai/sonar-fix2/x` | `WRONG_BRANCH_NAMESPACE` | eligible |
| `AI/sonar-fix/x` (case variant) | `WRONG_BRANCH_NAMESPACE` | eligible |

* The test is an **exact, case-sensitive** prefix followed by `/`, because Git
  refs are case-sensitive and T07 only ever emits the lowercase prefix.
  Protection comparison is case-*in*sensitive while namespace membership is not:
  keeping membership case-sensitive makes T26 strictly stricter than T20/T21's
  `is_agent_branch`, never looser (§18.14).
* The namespace is **not a policy field**: it is the T07 constant. A configurable
  namespace would be a way to widen what may be mutated, and T07 owns naming.
* Disabling the requirement is an explicit operator opt-out: any valid branch
  that is neither protected nor the default branch becomes eligible. It disables
  nothing else — tests pin the protected/default/missing-default/invalid-policy
  refusals with the flag off.
* This is **not** a branch-creation policy: T26 evaluates an existing branch and
  never creates, renames, deletes or checks out anything.

### 18.6 Branch identity validation (one rule for every name)

Every name T26 reads — the candidate, the default branch and each configured
protected entry — must pass the *same* identity rule:

1. it must be a `str` (`None`, `int`, `bool`, `float`, `bytes`, a collection …
   are refused);
2. it must be valid according to T07's `branch_naming.validate_branch_name`,
   which mirrors `git check-ref-format` and additionally rejects `HEAD`, `@` and
   a leading `-`;
3. it must not be **ref-like**: a value starting with `refs/` names a reference,
   not a branch, so its identity is ambiguous and it is refused.

Refused at minimum (every case pinned by a test): `None`; empty and
whitespace-only; leading or trailing whitespace (**never trimmed** — an identity
with padding is refused, not silently rewritten); `..`; a trailing `.`; a
trailing `.lock`; a component beginning with `.`; `//`; a leading `-`; `/` at
either end; `@{`; `HEAD`; `@`; every forbidden ref character (space, `~`, `^`,
`:`, `?`, `*`, `[`, `\`) and every control character including NUL; and
`refs/...`.

Names that are **not** refused: anything T07's validator accepts and that is not
ref-like — including `release/1.0`, `a/b/c`, `origin/main` (a *different* branch
from `main`, never a suffix match) and `ai/sonar-fix/a/b`. T26 performs **no
substring, prefix or suffix matching** for protection: only the whole comparison
key is compared.

The comparison key is `name.strip().casefold()`, applied identically to the
candidate, the default branch and every protected entry. The trim is idempotent
for every name that reaches a comparison (a padded name was refused first), and a
key is used **for comparison only** — T26 never rewrites a branch name. That is
precisely what makes `main`, `MAIN`, `Main` and `main/` all unusable as mutation
targets.

### 18.7 The contract

```python
ProtectedBranchPolicy(protected_branches=PROTECTED_BRANCH_NAMES,
                      require_ai_fix_branch_prefix=True)       # configuration
BranchProtectionInput(candidate_branch=None,
                      default_branch=None)                     # caller facts
evaluate_branch_protection(*, policy, branch) -> BranchProtectionEvaluation
```

`policy_version` is `t26.1`. The module exposes `POLICY_VERSION`,
`PROTECTED_BRANCH_NAMES`, `AI_FIX_BRANCH_PREFIX`, `ALLOWED_STATUSES`,
`PRECEDENCE`, `BranchProtectionStatus`, `BranchProtectionDecision`,
`ProtectedBranchPolicy`, `BranchProtectionInput`, `BranchProtectionEvaluation`
and `evaluate_branch_protection` — and it imports the standard library plus the
pure T07 validator `branch_naming`, nothing else.

### 18.8 Statuses

| Status | Meaning |
|--------|---------|
| `ALLOWED` | the branch may receive an AI-generated fix |
| `PROTECTED_BRANCH` | the branch is one of the configured protected names |
| `DEFAULT_BRANCH` | the branch **is** the repository's default branch |
| `INVALID_BRANCH` | the candidate is not a usable branch identity |
| `INVALID_POLICY` | the protection configuration is not usable |
| `MISSING_DEFAULT_BRANCH` | no usable default branch was supplied |
| `WRONG_BRANCH_NAMESPACE` | the branch is outside `ai/sonar-fix/` while required |

`BranchProtectionDecision` is `ALLOW` only for `ALLOWED_STATUSES`
(`(ALLOWED,)`) and `REFUSE` for every other status, so a refusal can never report
`allowed == True` and an allowed verdict can never carry a refusal status.

### 18.9 Precedence (explicit, documented and pinned)

`PRECEDENCE` is the authoritative order; the first applicable status decides:

1. `INVALID_POLICY` — a policy that cannot state what is protected refuses
   before anything else is considered;
2. `INVALID_BRANCH` — an unusable candidate is refused before any classification;
3. `MISSING_DEFAULT_BRANCH` — without a default branch the candidate cannot be
   shown to differ from it;
4. `PROTECTED_BRANCH`;
5. `DEFAULT_BRANCH`;
6. `WRONG_BRANCH_NAMESPACE`;
7. `ALLOWED`.

**The documented hard case**: `candidate="main"` with `default_branch=None` is
refused as `MISSING_DEFAULT_BRANCH`, **not** as `PROTECTED_BRANCH` — the missing
precondition is reported first, exactly as T24 checks its configuration before
its evidence. The branch is refused either way, so the choice affects only the
reported reason, and it is pinned by a test
(`test_a_missing_default_branch_takes_precedence_over_protected`).

The tests pin one conflict per adjacent pair, e.g. invalid policy beats an
invalid candidate; an invalid candidate beats a missing default branch;
`MISSING_DEFAULT_BRANCH` beats both `PROTECTED_BRANCH` and
`WRONG_BRANCH_NAMESPACE`; `PROTECTED_BRANCH` beats `DEFAULT_BRANCH` (a branch can
be both); `DEFAULT_BRANCH` beats `WRONG_BRANCH_NAMESPACE`.

### 18.10 Fail-closed behaviour

| Input | Result |
|-------|--------|
| `protected_branches=None` | `INVALID_POLICY` — never "protect nothing" |
| unusable protected set (`str`/`bytes`/`set`/`mapping`/other) | `INVALID_POLICY` |
| malformed or duplicate protected entry | `INVALID_POLICY` |
| `require_ai_fix_branch_prefix` not a real `bool` | `INVALID_POLICY` |
| unusable candidate | `INVALID_BRANCH` |
| unusable or absent default branch | `MISSING_DEFAULT_BRANCH` |
| protected candidate (any case or spacing) | `PROTECTED_BRANCH` / `INVALID_BRANCH` |
| candidate equal to the default branch (any case) | `DEFAULT_BRANCH` |
| candidate outside `ai/sonar-fix/` while required | `WRONG_BRANCH_NAMESPACE` |
| otherwise | `ALLOWED` |

There is no "probably safe" outcome and no configuration that turns T26 into
"always allowed": with `protected_branches=()` *and*
`require_ai_fix_branch_prefix=False` the only remaining rule is the default
branch, which is still enforced (pinned by
`test_the_default_branch_rule_cannot_be_configured_away`). A `TypeError` is raised
only when a caller passes something that is not one of the two records — a
programming error, not evidence.

### 18.11 Trust boundary

Documented in the module and pinned by a test. T26 **is** a policy decision layer
and it **does not**:

* authenticate the caller, the branch or any evidence — both inputs are
  caller-asserted;
* discover the repository's default branch (§18.4); a future integration does
  that, and T26 trusts the value it is handed;
* prove the branch exists locally or remotely, or that it is the checked-out
  branch;
* mutate Git, the filesystem, the environment or any global state;
* prevent a malicious caller from bypassing the policy by simply not calling it.

Future orchestration must enforce the returned verdict; T26 cannot enforce
anything by itself. This is intentional and is the same "policy boundary, not an
evidence-authentication boundary" distinction §14.2/§15.1 document for T23/T24.

### 18.12 Purity and no Git I/O

`branch_protection.py` accesses no filesystem, no network, no subprocess, no Git
and no configuration file; it mutates no global state, keeps no counter and no
cache, and reads its inputs only from its two arguments. It builds no shell
command and no argv. The tests assert this three ways: the module's imports are
exactly `__future__`, `dataclasses`, `enum`, `typing` and `branch_naming`; the
module's AST references none of `subprocess`/`socket`/`open`/`eval`/`exec`/
`print`/`input`/`__dict__`/`getattr`/`setattr`, and its source contains no Git
subcommand string; and an evaluation still succeeds with `builtins.open` and
`pathlib.Path.open` replaced by an exception, and with `GIT_DIR`/`DEFAULT_BRANCH`
set in the environment.

### 18.13 The result DTO, determinism and secret safety

`BranchProtectionEvaluation` is frozen and carries `policy_version`, `status`,
`decision`, `candidate_branch`, `default_branch`, `branch_in_ai_namespace`,
`branch_is_default`, `branch_is_protected`, `reason`, `reasons` and `policy`.
`is_allowed` is a **property derived from `status`** (never a stored field), so
the invariant `is_allowed == (status is ALLOWED)` cannot be broken by
construction — there is no second source of truth to diverge.

The three booleans are *facts about the inputs*, not restatements of the status:
a branch can be both protected and the default branch (`develop` with
`default_branch="develop"` is `PROTECTED_BRANCH` with `branch_is_default is True`
as well), and `PRECEDENCE` names only the first applicable refusal. The pinned
implications are: `ALLOWED ⇒ ¬protected ∧ ¬default ∧ (in namespace ∨ rule off)`;
`PROTECTED_BRANCH ⇒ branch_is_protected`; `DEFAULT_BRANCH ⇒ branch_is_default ∧
¬branch_is_protected`; `INVALID_BRANCH ⇒ candidate_branch is None`;
`MISSING_DEFAULT_BRANCH ⇒ default_branch is None`.

`as_dict()` is JSON-native, deterministic (same keys, same order, same values on
every call) and **publishes only validated values**: a name that failed
validation is `None`, an unusable protected set is `None`, a non-bool flag is
`None`, and a refusal reason names the *type* or the T07 rule instead of echoing
the value (a forbidden character can appear only through its `repr`, e.g.
`'\x00'`). The only caller text that can ever be published is a **validated** Git
branch name — a ref, not arbitrary text — and T26 emits no command, no argv and
no environment, so nothing in the mapping can be executed. Tests prove a decoy
secret placed in an invalid candidate, an invalid protected set and an invalid
flag never reaches a reason or a serialized view.

Repeated evaluation of the same pair returns an **equal** record with a
byte-for-byte identical `as_dict()`, no caller value is mutated (including a
caller-supplied list of protected names), and no state is kept between calls.

### 18.14 Compatibility with T20/T21 (no contradiction, and the differences)

T26 is the standalone formalisation of the branch rule T20/T21 already apply
inline (`commit_policy.py::_gate_branch_safe`, gate `G32`, and T21's `G14`/`G28`).
It **changes neither module**: no T20/T21 file was touched, neither imports T26,
and the compatibility tests only *read* them.

Same values: `PROTECTED_BRANCH_NAMES` (`main`, `master`, `develop`, `trunk`) and
the `ai/sonar-fix` namespace are identical in T07, T20, T21 and T26, and a test
pins all four to each other. Safety direction: **a T26 allow never widens a T20
allow** (pinned over a matrix of branch names) — T26 can only ever be stricter.

| Behaviour | T20/T21 | T26 | Effect |
|-----------|---------|-----|--------|
| `default_branch=None` | "no extra branch is protected"; the executor resolves the real default branch first | refused `MISSING_DEFAULT_BRANCH` | T26 is stricter |
| a padded name (`" main"`) | stripped, then compared — so `" main"` counts as protected | refused as an invalid identity | both refuse; only the *reason* differs |
| `required_branch_prefix` | any truthy value enables the rule | must be a real `bool` | T26 is stricter |
| namespace membership | `startswith(prefix + "/")`, case-sensitive | same, case-sensitive | identical |
| protection comparison | strip + `casefold` | strip + `casefold`, one key for every name | identical |
| the default branch | merged into `is_protected_branch` | a separate `DEFAULT_BRANCH` status and separate booleans | a reporting difference, never a widening |

The only semantic differences are *stricter* in T26 (a missing default branch is
a refusal, a padded identity is refused, the flag must be a bool); none of them
loosens a T20/T21 rule, so T20/T21 behaviour is unchanged and uncontradicted.

### 18.15 Tests

| Test file | Covers |
|-----------|--------|
| `tests/test_branch_protection.py` | policy configuration (default set, custom set, empty set, `None`, `str`/`bytes`/`set`/`frozenset`/`mapping`/scalar sets, malformed entries, duplicate normalized entries, non-`bool` flags, frozen records, the non-configurable namespace), candidate validation (all 43 pinned invalid identities and the valid ones), protection (all four names, case and whitespace variants, slashed names, no pattern/substring matching, unusable sets), the default branch (equal/different/case-folded/invalid/missing/itself protected/outside the set), the AI namespace (valid, nested, bare prefix, near-prefix, case variants, malformed prefix, disabled requirement), the exact `PRECEDENCE` plus one conflict per adjacent pair and a 455-case candidate × default × policy matrix, the result invariants (`is_allowed == (status is ALLOWED)`, no refusal reports allowed, `ALLOWED ⇒ ¬protected ∧ ¬default ∧ (namespace ∨ off)`), determinism, repeated evaluation, caller-value safety, frozen records, exact and JSON-native serialization, secret safety (decoys in an invalid candidate, set and flag), the helper tables, the T07/T20/T21 compatibility pins and documented differences, and the module surface (exact `__all__`, imports = stdlib + `branch_naming`, no I/O/execution/orchestration module, no Git command string, no branch-mutating API, exactly two keyword-only parameters, trust-boundary documentation) |

`branch_protection.py` is at **100% statement and 100% branch coverage** from the
focused run (`pytest tests/test_branch_protection.py --cov=branch_protection
--cov-branch`): 147 statements, 38 branches, 0 missing, 0 partial. The file
contains 343 tests; adding `tests/test_branch_naming.py` (386 tests) still
reports 100% for both modules.

### 18.16 Integration status — and `main.py` remains unwired

* **T26 is a library with no caller.** `main.py` is byte-for-byte unchanged, no
  production path imports `branch_protection`, and neither T20 nor T21 depends on
  it: `commit_policy.py`, `push_policy.py`, `git_commit.py` and `git_push.py` are
  untouched and keep their own inline branch gates.
* The module imports the standard library plus the pure T07 validator
  `branch_naming`; it has no dependency on T19–T25, no Git, no subprocess, no
  network and no configuration file, so nothing about the existing pipeline can
  change because of it.
* T26 is a policy boundary only: it is **not** orchestration, it executes no
  Git/SonarQube/Codex operation, and it performs no branch mutation.
* A future integration would: resolve the default branch through T21's executor
  (`_resolve_default_branch`, local refs only), pass it together with the
  checked-out branch to `evaluate_branch_protection`, and refuse to stage,
  commit or push on any refusal. That wiring is deliberately **not** part of T26 —
  T26 must be independently testable first, and no orchestration is created
  prematurely.

### 18.17 Acceptance criteria

1. **Given** a candidate equal to a protected name in any case (or a padded
   variant of one), **then** the verdict is `PROTECTED_BRANCH` (or
   `INVALID_BRANCH` for the padded form) and `is_allowed` is `False`.
2. **Given** a candidate equal (case-folded) to the supplied default branch,
   **then** the verdict is `DEFAULT_BRANCH`, whatever the branch is called.
3. **Given** no usable default branch, **then** every candidate is refused with
   `MISSING_DEFAULT_BRANCH`; `None` is never read as "there is no default branch".
4. **Given** an unusable candidate (missing, empty, padded, malformed,
   ref-like), **then** the verdict is `INVALID_BRANCH` and the value is never
   trimmed, repaired or rewritten.
5. **Given** an unusable protected set, a malformed or duplicate entry, or a
   non-`bool` namespace flag, **then** every candidate is refused with
   `INVALID_POLICY`.
6. **Given** a valid, non-protected, non-default candidate outside the namespace
   while the requirement is enabled, **then** the verdict is
   `WRONG_BRANCH_NAMESPACE`.
7. **Given** `ai/sonar-fix/<x>` and a default branch that differs from it,
   **then** the verdict is `ALLOWED`.
8. **Given** identical inputs, **then** the verdict is equal, `as_dict()` is
   byte-for-byte identical and no caller value (policy, input, list) changed.
9. **Given** any inputs, **then** the policy performs no I/O, no Git operation
   and no branch mutation, and constructs no command.

## 19. T27 — uncertainty policy (implemented policy layer, not wired)

T27 answers exactly one question and nothing else:

> "is the evidence that is supposed to authorise this mutation *explicitly
> confirmed*?"

It is a **pure policy / decision layer**: it discovers nothing, runs no Git
command, reads no file, queries no SonarQube, executes no Codex, authenticates
nothing and mutates nothing. `main.py` is byte-for-byte unchanged, T19–T26 keep
their own (unchanged) logic, and **nothing calls T27 yet**.

| Module | Role | Side effects |
|--------|------|--------------|
| `uncertainty_policy.py` | the fixed required-evidence policy, the immutable input/verdict records, the state→status mapping, the decision ladder and `as_dict()` | **none** |

`POLICY_VERSION` is `t27.1`. The module exposes `POLICY_VERSION`,
`ALLOWED_STATUSES`, `REVIEW_STATUSES`, `PRECEDENCE`, `REQUIRED_DIMENSIONS`,
`CONTRADICTION_PAIRS`, `EvidenceState`, `EvidenceDimension`, `MutationPhase`,
`UncertaintyStatus`, `UncertaintyDecision`, `EvidenceItem`,
`UncertaintyInput`, `UncertaintyEvaluation` and `evaluate_uncertainty` — and it
imports the standard library only (`__future__`, `dataclasses`, `enum`,
`types`, `typing`), not a single module of this repository.

### 19.1 Purpose, scope and non-goals

* **Purpose**: give a future orchestration layer one deterministic, fail-closed
  answer to "does the evidence authorise this commit/push?", so partial, absent,
  stale or contradictory evidence can never be read as permission.
* **In scope**: the fixed required-evidence policy per phase, evidence-record
  validation, the state→status mapping, the decision order, contradiction
  detection, the exact reason and the canonical serialization.
* **Out of scope**: gathering evidence (T10–T19 produce it, T20/T21 add to it),
  reading Git (T12/T20/T21), branch creation (T07), branch protection (T26,
  §18), committing (T20, §11), pushing (T21, §12), retrying/looping,
  orchestration and `RunContext`.
* **Trust boundary**: the input record is **caller-asserted**; T27 authenticates
  nothing and proves nothing (§19.10).

### 19.2 The one rule and the four phases

A mutation is allowed **only** when every dimension the phase requires is
`CONFIRMED`. There is no weighting, no scoring, no threshold, no confidence
value and no "probably fine" path: any other state refuses the mutation. Every
phase requires everything the earlier phases required, so the policy can only
ever get stricter as a run advances.

| Phase | Asked before | Requires |
|-------|--------------|----------|
| `PRE_COMMIT` | the T20 commit | the pre-commit evidence (9 dimensions) |
| `POST_COMMIT` | the T21 push, with the commit re-verified | pre-commit + commit evidence (12) |
| `PRE_PUSH` | the T21 push, with the branch re-verified | post-commit + branch consistency (13) |
| `POST_PUSH` | ending the run, with the push verified | pre-push + push evidence (16) |

### 19.3 The evidence model (states and dimensions)

Evidence is **explicit and typed**. The caller states one `EvidenceItem` — a
`dimension` plus an `EvidenceState` — per dimension it knows about, and a
dimension it omits is `MISSING`, never assumed confirmed.

| State | Meaning |
|-------|---------|
| `CONFIRMED` | the dimension is explicitly, positively established |
| `MISSING` | the dimension was not supplied at all |
| `UNVERIFIED` | the dimension was reported but not verified |
| `UNKNOWN` | the dimension was not established |
| `AMBIGUOUS` | the dimension is ambiguous |
| `NEGATIVE` | the dimension is a *known* negative result, not merely uncertain |
| `CONTRADICTORY` | the dimension contradicts itself or the claim that depends on it |
| `INVALID` | the stated value is not usable at all |

| Dimension | Where it comes from | `CONFIRMED` means |
|-----------|---------------------|-------------------|
| `issue-outcome` | T19 | the verified fix is `FIXED` |
| `codex-execution` | T10/T11 | Codex ran and its result is analysable |
| `change-scope` | T13 | the diff stayed inside the issue's scope |
| `tests` | T14/T15 | the project test run passed |
| `sonar-analysis` | T16/T17 | the re-analysis completed |
| `issue-verification` | T18 | the original issue is reliably absent |
| `analysis-correlation` | T17→T18 | the snapshot is attributable to the waited-on analysis |
| `cross-stage-consistency` | T19/T20/T21 | the lifecycle stages agree with one another |
| `branch-protection` | T26 | the branch was `ALLOWED` |
| `commit-result` | T20 | the commit was created (`COMMITTED`) |
| `commit-identity` | T20 | the recorded commit is the one that was created |
| `commit-repository` | T20 | the repository state after the commit is expected |
| `branch-consistency` | T20/T21 | source and destination branch agree |
| `push-result` | T21 | the push was created (`PUSHED`) |
| `remote-tip` | T21 | the remote tip is the expected one |
| `expected-commit` | T21 | the pushed commit is the expected one |

Everything else maps to a non-confirming state. T19's known failures
(`STILL_OPEN`, `TESTS_FAILED`, `SCOPE_INVALID`, `CODEX_FAILED`,
`ANALYSIS_FAILED`) are `NEGATIVE`; T19's `REVIEW_REQUIRED`, a T17 timeout and an
unattributable T18 snapshot are `UNKNOWN`/`UNVERIFIED` (doubt); a T26 `REFUSE` is
`NEGATIVE` while a T26 that was never evaluated is `UNKNOWN`. T27 does **not**
encode that mapping: it owns no T19–T26 constant, imports none of them (§19.13)
and accepts any of the eight states for any dimension, so the mapping is
documentation for the future integration rather than a T27 dependency.

Each `EvidenceItem` may additionally carry a `code`: a bounded machine token
(letters, digits and `-`, `_`, `.`, `:`; at most 48 characters) that names the
*source* condition — `still-open` or `sha-mismatch`, for example. A code is
echoed for diagnostics and is **never consulted**: a test pins that two inputs
differing only in a code produce byte-identical status, decision, blocking
dimension and reason. No free-form message is accepted anywhere, and a code that
is not a bounded token makes the whole record unusable instead of being trimmed.

### 19.4 The required-evidence policy (fixed, not configurable)

| Phase | Required dimensions, in policy order |
|-------|--------------------------------------|
| `PRE_COMMIT` | `issue-outcome`, `codex-execution`, `change-scope`, `tests`, `sonar-analysis`, `issue-verification`, `analysis-correlation`, `cross-stage-consistency`, `branch-protection` |
| `POST_COMMIT` | pre-commit + `commit-result`, `commit-identity`, `commit-repository` |
| `PRE_PUSH` | post-commit + `branch-consistency` |
| `POST_PUSH` | pre-push + `push-result`, `remote-tip`, `expected-commit` |

`REQUIRED_DIMENSIONS` is a **read-only** mapping (`types.MappingProxyType`), and
there is no constructor, flag or keyword that can shorten a phase, so the
contract cannot be weakened by a caller (a test pins the `TypeError` a mutation
raises). A dimension a phase does **not** require is ignored rather than
trusted: it can neither soften nor harden that phase's verdict, which is what
lets a caller accumulate evidence across a run and simply ask the phase it is
about to reach.

The single exception is `INVALID`: an unusable state *anywhere* in the record
refuses the whole record (`INVALID_INPUT`), because a record that contains
something T27 cannot read is not trustworthy at all — even when the unusable
dimension is one the phase does not require.

### 19.5 Statuses

| Status | Meaning |
|--------|---------|
| `ALLOWED` | every required dimension is confirmed |
| `INVALID_POLICY` | the phase does not select a mutation phase T27 knows |
| `INVALID_INPUT` | the evidence record itself is not usable |
| `MISSING_EVIDENCE` | a required dimension was not supplied (absence) |
| `CONTRADICTORY_EVIDENCE` | a required dimension contradicts itself or its claim |
| `NEGATIVE_EVIDENCE` | a required dimension is a *known* negative result |
| `UNKNOWN_EVIDENCE` | a required dimension was not established (doubt) |
| `UNVERIFIED_EVIDENCE` | a required dimension was reported but not verified (doubt) |
| `AMBIGUOUS_EVIDENCE` | a required dimension is ambiguous (doubt) |

`UncertaintyDecision` is `ALLOW` only for `ALLOWED_STATUSES` (`(ALLOWED,)`) and
`REFUSE` for every other status, and `UncertaintyEvaluation.is_allowed` is
derived from the decision and never stored separately — so a refusal can never
report `is_allowed == True` and an allowed verdict can never carry a refusal
status. `REVIEW_STATUSES` is `(MISSING_EVIDENCE, UNKNOWN_EVIDENCE,
UNVERIFIED_EVIDENCE, AMBIGUOUS_EVIDENCE)`; `requires_review` is True only for
those, and it never changes the decision.

### 19.6 Precedence (explicit, documented and pinned)

`PRECEDENCE` is the authoritative order; the first applicable status decides, and
the blocking dimension is the first *required* dimension, in the phase's policy
order, that carries the decisive state (a test reverses the caller's tuple and
pins that the reported dimension does not move):

1. `INVALID_POLICY` — a phase that selects no policy refuses before anything else;
2. `INVALID_INPUT` — a record that cannot be read refuses before any judgement;
3. `MISSING_EVIDENCE` — absence is reported before any state of the evidence that
   *was* supplied;
4. `CONTRADICTORY_EVIDENCE` — an explicit contradiction first, then a confirmed
   claim whose required proof is present but not confirmed;
5. `NEGATIVE_EVIDENCE` — a known failure;
6. `UNKNOWN_EVIDENCE`;
7. `UNVERIFIED_EVIDENCE`;
8. `AMBIGUOUS_EVIDENCE`;
9. `ALLOWED`.

**The documented hard cases**, each pinned by a test:

* `PRE_COMMIT` with `commit-identity` unverified and `commit-result` absent is
  `MISSING_EVIDENCE`, **not** `CONTRADICTORY_EVIDENCE`: a missing dimension is a
  gap in the call, and absence is reported first.
* `POST_COMMIT` with `commit-result` confirmed and `commit-identity` unknown is
  `CONTRADICTORY_EVIDENCE`, **not** `UNKNOWN_EVIDENCE`: a present-but-
  unconfirmed proof contradicts the confirmed claim that depends on it. The
  proof's own state (`unknown`) is still reported in `blocking_state`, so the
  diagnosis stays precise.
* `PRE_COMMIT` with the whole commit evidence supplied but not required is
  `ALLOWED`: a contradiction is only checked between dimensions the phase
  actually requires.

The tests pin one conflict per adjacent pair: invalid policy beats invalid input;
invalid input beats missing evidence; missing evidence beats both contradiction
and negative evidence; contradiction beats negative and unknown evidence;
negative beats unknown and unverified evidence; unknown beats unverified;
unverified beats ambiguous; ambiguous beats allowed.

### 19.7 Contradiction detection

Contradictions are refused, never resolved optimistically. T27 reports
`CONTRADICTORY_EVIDENCE` when

* a required dimension is explicitly `CONTRADICTORY`, or
* a **confirmed** claim is paired with a required proof that is *present but not
  confirmed* (`CONTRADICTION_PAIRS`): `issue-outcome`/`issue-verification`,
  `issue-outcome`/`analysis-correlation`, `commit-result`/`commit-identity`,
  `commit-result`/`commit-repository`, `push-result`/`remote-tip` and
  `push-result`/`expected-commit`. The pair is only evaluated in a phase that
  requires **both** dimensions, so the check never invents a requirement.

`UncertaintyEvaluation.is_contradiction` exposes the distinction. A contradiction
is *not* a doubt: it reports `requires_review == False`, because it is a known
inconsistency rather than a question for a human.

### 19.8 Negative is not uncertain

`NEGATIVE` (a known failure such as `STILL_OPEN`, `TESTS_FAILED` or a `REFUSE`
from T26) is deliberately separate from the doubt states (`MISSING`, `UNKNOWN`,
`UNVERIFIED`, `AMBIGUOUS`). Both refuse, and both are *diagnosable*: the status
keeps "proven bad" apart from "not proven either way" (`NEGATIVE_EVIDENCE`
versus `UNKNOWN_EVIDENCE`), the blocking dimension keeps the *source* apart
(`tests` versus `issue-verification`), and `requires_review` is True only for
doubt. A known failure is never downgraded to doubt and a doubt is never upgraded
to a failure.

### 19.9 Fail-closed behaviour

| Input | Result |
|-------|--------|
| `phase=None` or any non-`MutationPhase` (a `str`, an int, `True`, another enum) | `INVALID_POLICY` |
| `evidence` is not a `tuple` (`None`, a list, a set, a mapping, `str`, `bytes`, an object) | `INVALID_INPUT` |
| an entry is not an `EvidenceItem` | `INVALID_INPUT` |
| an unknown `dimension` or `state` | `INVALID_INPUT` |
| a duplicate dimension (two states for one dimension) | `INVALID_INPUT` |
| an ill-formed diagnostic code (not a bounded token) | `INVALID_INPUT` |
| a required dimension omitted / supplied as `MISSING` | `MISSING_EVIDENCE` |
| a required dimension explicitly `CONTRADICTORY` | `CONTRADICTORY_EVIDENCE` |
| a confirmed claim whose required proof is present but not confirmed | `CONTRADICTORY_EVIDENCE` |
| a required dimension `NEGATIVE` | `NEGATIVE_EVIDENCE` |
| a required dimension `UNKNOWN` | `UNKNOWN_EVIDENCE` |
| a required dimension `UNVERIFIED` | `UNVERIFIED_EVIDENCE` |
| a required dimension `AMBIGUOUS` | `AMBIGUOUS_EVIDENCE` |
| an `INVALID` state anywhere in the record | `INVALID_INPUT` |
| every required dimension `CONFIRMED` | `ALLOWED` |

Nothing is repaired: no value is trimmed, coerced, defaulted or re-ordered, and
no missing dimension is filled in. `None` never means "confirmed", a falsy value
is never treated as absent-but-fine, and a truthy object is never believed — a
state is only ever read by identity, so a value with a clever `__bool__` is
simply an unusable entry.

### 19.10 Trust boundary

The input is **caller-asserted**. T27 authenticates nothing: it does not read
Git, run the tests, query SonarQube or verify that the states it is handed are
true. It is a policy boundary, not an evidence-authentication boundary — a
caller that misstates a state, or that simply does not call T27 at all, is not
detected here. T27 exists so that a future orchestration layer can no longer
*accidentally* commit or push on partial, absent or contradictory evidence, and
so that the "is this safe?" decision is stated once instead of being re-derived
(and possibly relaxed) at each call site.

### 19.11 Purity, determinism and diagnostics

* **Pure**: `evaluate_uncertainty` reads only its argument. No clock, environment
  value, random source, counter or mutable module state is read; no filesystem,
  network, subprocess or Git operation is performed; no shell command or argv is
  ever constructed. Every table is a `tuple` or a read-only mapping, and a test
  snapshots them before and after evaluating and pins that nothing changed.
* **Deterministic**: the same input always yields an equal record and a
  byte-for-byte identical `as_dict()`; the caller's tuple order does not matter
  (evidence is republished in the policy order), and the blocking dimension is
  the earliest required dimension rather than the first one supplied.
* **Caller-safe**: the immutable records are never mutated, and a mutable
  container is refused rather than iterated (a test passes a list, evaluates, and
  pins that the list is untouched).
* **Diagnosable**: the exact reason is a pure function of
  `(status, dimension, state)` — `Refused (missing-evidence): tests is missing -
  …` — and `reason` plus the status's fixed fail-closed consequence form
  `reasons`. No caller-authored text and not even a type name can reach a
  reason, so a reason cannot leak a secret or fabricate a value.

### 19.12 The contract, the result DTO and secret safety

```python
EvidenceItem(dimension, state, code="")             # one evidence dimension
UncertaintyInput(phase=None, evidence=())          # caller-asserted facts
evaluate_uncertainty(*, uncertainty_input) -> UncertaintyEvaluation
```

`evaluate_uncertainty` takes exactly one keyword-only parameter. A non-
`UncertaintyInput` raises `TypeError` (a caller error, not an evidence problem);
every evidence problem is a refusal in the returned record. `UncertaintyInput`
and `EvidenceItem` are frozen, and `evidence` must be a `tuple`.

`UncertaintyEvaluation` (frozen) carries `policy_version`, `phase`, `status`,
`decision`, `blocking_dimension`, `blocking_state`, `required_dimensions`,
`evidence`, `reason` and `reasons`, plus the derived `is_allowed`,
`is_contradiction` and `requires_review` properties. `as_dict()` is JSON-native
and deterministic: enum values are published as their strings, tuples as lists,
the required-dimension table and the evidence in policy order, and
`required_dimensions` names the exact contract the verdict was measured
against. Only *validated* values are published — an unusable phase, dimension or
state is reported as `None`, an unusable evidence record as `[]` plus
`evidence_is_usable: false`, and a bad code as `None` — so the value that made a
record ill-formed is never echoed. The one caller-authored value that *is*
published is a valid, bounded `code`, and only for diagnostics.

### 19.13 Compatibility with T19–T26 (and why T27 imports nothing)

* T27 is **standalone and self-contained**: it imports the standard library only
  (`__future__`, `dataclasses`, `enum`, `types`, `typing`) and imports **no**
  repository module — not `issue_status`, not `commit_policy`/`push_policy`, not
  `branch_protection`, not `overall_report`/`per_issue_report`. A test asserts
  the imported set exactly, and asserts that no other module in the repository
  imports `uncertainty_policy` (§19.15).
* T27 **restates the T19–T26 vocabulary in its own terms** rather than importing
  it, exactly as T26 keeps its own copy of `PROTECTED_BRANCH_NAMES`. The
  correspondence is documented in §19.3 and adds no runtime coupling: a change to
  a T19–T26 status name cannot change a T27 verdict, and a change to T27 cannot
  change T19–T26.
* T27 **never contradicts** T19–T26: it consumes their outcomes as evidence and
  refuses whenever they are absent, doubtful or contradictory. It cannot widen an
  allow: T20/T21 keep their own gates, T24/T25 keep their bounds and T26 keeps its
  branch rule — T27 only adds the requirement that the *evidence* be confirmed.
* The phases mirror the existing lifecycle: `PRE_COMMIT` guards §11 (T20),
  `POST_COMMIT`/`PRE_PUSH` guard §12 (T21), and `POST_PUSH` closes the run after
  §12's verification. The `branch-protection` dimension is exactly T26's verdict
  (§18.8): `ALLOWED` → `CONFIRMED`, `REFUSE` → `NEGATIVE`, and an unevaluated T26
  → `UNKNOWN` (never `CONFIRMED`).

### 19.14 Tests

`tests/test_uncertainty_policy.py` pins the enums and every table, the one rule,
the mandated matrix, the full status/every-dimension matrix, one conflict per
adjacent `PRECEDENCE` pair, both documented hard cases, contradiction detection
(explicit, paired, and the "both dimensions required" restriction), the
negative-versus-doubt distinction, every fail-closed input, the code rules, the
secret-safety decoys, determinism, caller-value safety, canonical ordering,
frozen records, the exact serializations, and the module surface (imports,
declared definitions, `__all__`, read-only tables, `__test__ = False`, no
dangerous call, and "no repository module imports T27").

`uncertainty_policy.py` is at **100% statement and 100% branch coverage** from
the focused run (`pytest tests/test_uncertainty_policy.py
--cov=uncertainty_policy --cov-branch`): 214 statements, 64 branches, 0 missing,
0 partial. The file contains 237 tests.

### 19.15 Integration status — and `main.py` remains unwired

* **T27 is a library with no caller.** `main.py` and T01–T26 are untouched; no
  production path imports `uncertainty_policy`, and no existing module gained a
  dependency on it. A test pins the absence of such an import.
* T27 executes nothing: no Git, no subprocess, no network, no file and no
  orchestration. It is a decision function and nothing else.
* A future integration would: map the T19–T26 outcomes onto `EvidenceItem`
  records (§19.3), call `evaluate_uncertainty` for the phase it is about to
  enter, and refuse to stage, commit or push on any refusal — reporting
  `requires_review` distinctly so a doubt can be escalated to a human while a
  known failure is reported as a failure. That wiring is deliberately **not**
  part of T27: the policy must be independently testable first, and no
  orchestration is created prematurely.

### 19.16 Acceptance criteria

1. **Given** every dimension a phase requires is `CONFIRMED`, **then** the
   verdict is `ALLOWED` with `decision == ALLOW`, `is_allowed == True` and no
   blocking dimension; **given** any other state for any required dimension,
   **then** the verdict is a refusal with `is_allowed == False`.
2. **Given** an omitted required dimension, **then** the verdict is
   `MISSING_EVIDENCE` and absence is never read as a confirmation.
3. **Given** a known failure (`NEGATIVE`) on a required dimension, **then** the
   verdict is `NEGATIVE_EVIDENCE` and `requires_review` is `False`; **given** a
   doubt (`UNKNOWN`/`UNVERIFIED`/`AMBIGUOUS`), **then** the verdict is the
   matching doubt status and `requires_review` is `True`. Both refuse.
4. **Given** a contradiction — an explicit `CONTRADICTORY` dimension, or a
   confirmed claim whose required proof is present but not confirmed — **then**
   the verdict is `CONTRADICTORY_EVIDENCE` and `is_contradiction` is `True`.
5. **Given** a phase that is not a `MutationPhase`, **then** the verdict is
   `INVALID_POLICY`; **given** an evidence field that is not a tuple of
   `EvidenceItem` records (wrong type, unknown dimension or state, duplicate
   dimension, ill-formed code), **then** the verdict is `INVALID_INPUT` and
   nothing is repaired, trimmed or inferred.
6. **Given** any of the mandated cases (an unknown issue outcome, a failed
   Codex run, an invalid scope, failed tests, an incomplete analysis, an
   unverified issue verification, a missing correlation, a refused or unknown
   branch protection, a missing worktree/scope record, an unverified or
   mismatched commit, contradictory commit evidence, an unknown push result and
   an unverified remote tip), **then** the mutation is refused.
7. **Given** identical inputs, **then** the verdict is equal, `as_dict()` is
   byte-for-byte identical, the evidence is published in policy order whatever
   the caller's tuple order was, and no caller value changed.
8. **Given** any inputs, **then** no I/O, no Git operation, no command and no
   mutation occurs, no caller-authored text (other than a valid diagnostic code)
   is published, and the required-evidence table cannot be weakened.

## 20. T28 — Sonar rule allowlist (implemented policy layer, not wired)

Status: **implemented** (`sonar_rule_allowlist.py`, version `t28.1`) · standalone
· pure · deterministic · immutable · fail-closed · **unwired** (`main.py` and
T19–T27 unchanged).

### 20.1 Purpose, scope and non-goals

T28 answers exactly one question:

    "is this SonarQube rule explicitly allowed to be fixed automatically?"

The default answer is **no**. The default configuration is an empty allowlist, so
every rule is denied until an operator states the exact rule IDs they approve.
T28 does not decide whether a Sonar issue is safe to fix based on severity,
issue type, language, confidence, historical success, or message content. It
only answers whether the exact Sonar rule ID is explicitly allowlisted.

**In scope:** the rule ID grammar (§20.3); the allowlist configuration contract
(§20.4); validation and refusal clauses (§20.5); the bounds (§20.6); the statuses
(§20.7); the precedence (§20.8); the fail-closed rule (§20.9); the trust boundary
(§20.10); compatibility with the repository's own rule-key contract (§20.11);
immutability, purity, determinism and secret safety (§20.12); serialization
(§20.13); the tests (§20.14); the integration boundary (§20.15); the acceptance
criteria (§20.16).

**Non-goals (explicit):**

* no wildcards, no globs, no regexes and no prefix/suffix/substring/family
  matching — only exact string identity;
* no "allow all" switch, no implicit default entry and no configuration that
  permits more than the listed IDs;
* no severity, issue-type, language, file-extension, confidence, success-rate or
  history input — those values cannot even be expressed to T28 (§20.10);
* no SonarQube lookup (T28 never asks whether a rule exists, whether it is
  active in a quality profile, or whether an issue carries it);
* no code modification, no Git, no commit, no push, no retry, no orchestration,
  no reporting, no PR creation;
* no wiring into `main.py` (or any other module) in this task (§20.16).

### 20.2 The one rule — exact match, and nothing else

An automatic fix is authorised **only** when the rule ID the caller states is
*exactly* one of the rule IDs the operator configured:

    rule_id in allowed_rules   <=>   status is ALLOWED   <=>   can_auto_fix

The implementation makes that structural rather than conventional: the validated
configuration is turned into a `frozenset` and the only permission test in the
module is membership of that set. The module does not import `re`, `fnmatch`,
`glob`, `difflib` or `unicodedata`, and it calls no `startswith`, `endswith`,
`casefold`, `lower`, `upper`, `strip`, `find`, `count` or `replace` — tests
assert both facts, and an AST test asserts that every `in`/`not in` comparison in
the module compares against a *name* (a set/tuple), never a string literal, so no
substring test can exist anywhere.

Consequences that the tests pin explicitly:

| Value the caller states | Allowlist | Result |
|-------------------------|-----------|--------|
| `S1118` | `("S1118",)` | `ALLOWED` |
| `S1118` | `("S111",)` | `RULE_NOT_ALLOWED` |
| `S1118` | `("S1118x",)` | `RULE_NOT_ALLOWED` |
| `S1118` | `("xS1118",)` | `RULE_NOT_ALLOWED` |
| `S1118` | `("s1118",)` | `RULE_NOT_ALLOWED` (case is identity) |
| `S1118` | `("java:S1118",)` | `RULE_NOT_ALLOWED` |
| `java:S1118` | `("S1118",)` | `RULE_NOT_ALLOWED` (the qualifier is identity) |
| `S1118*` | `("S1118",)` or `("S1118*",)` | `INVALID_RULE_ID` / `INVALID_POLICY` |
| `S9999` | `("S1118",)` | `RULE_NOT_ALLOWED` (unknown is not an error) |

A wildcard is therefore never a tolerance: `S11*` is not a "rule family", it is a
malformed rule ID when supplied as input and an unusable configuration entry when
supplied in the allowlist. Wildcard support, if it is ever wanted, is a new
version of this policy rather than a flag in this one.

### 20.3 Supported rule grammar

A rule ID is deliberately narrow. It is **ASCII only** and at most
`MAX_RULE_ID_LENGTH` = **64 characters**, and it has this shape:

```
rule-id     := key | qualifier ":" key
key         := lead tail*
qualifier   := lead tail*
lead        := [A-Za-z0-9]                 # one ASCII letter or digit
tail        := [A-Za-z0-9._-]              # letters, digits, ".", "_", "-"
```

The two forms cover exactly what this repository's SonarQube model actually
carries: the bare rule key SonarQube reports for the languages in this project
(`S1118`, `S1192`, `S3776`) and the language/repository-qualified key it reports
as well (`python:S1481`, `java:S108`, `javascript:S1854`, `csharpsquid:S1118` —
all of which already appear in this repository's fixtures, its commit-message
layer and the `SonarIssue` model). The three separators `"."`, `"_"` and `"-"`
are supported because real SonarQube rule keys contain them
(`common-java:DuplicatedBlocks`, `external_eslint_repo:no-unused-vars`); nothing
else is.

The grammar is deliberately **not** a general "Sonar rule syntax" parser: there
is exactly one optional `:` separator, at most two parts, no nesting, no URI
form, no plugin/module form and no version syntax.

Every one of the following is **refused** (`INVALID_RULE_ID` when supplied as
input, `INVALID_POLICY` when supplied as a configuration entry):

| Class | Examples |
|-------|----------|
| empty | `` (the empty string) |
| whitespace-only / padded | `" "`, `"\t"`, `"S1118 "`, `" S1118"`, `"S 1118"` |
| control characters | `"S1118\n"`, `"S1118\r"`, `"S1118\t"`, `"S1118\x00"` |
| non-ASCII | `"S1118é"`, `"１１１８"`, `"S1118\u00a0"` |
| wildcards | `"*"`, `"**"`, `"S11*"`, `"S1118*"`, `"S11?"`, `"S111[8]"` |
| regex syntax | `"^S11"`, `"S11$"`, `"(?i)S1118"`, `".*"`, `"S.*"`, `"S11+"` |
| path-like | `"/etc/passwd"`, `"../../S1118"`, `"C:\Windows\S1118"`, `"src/S1118"` |
| URL-like | `"https://sonar.example.com/api/rules"`, `"http://S1118"` |
| shell fragments | `"S1118; rm -rf /"`, `"$(id)"`, `` "`id`" ``, `"S1118\|id"` |
| separator errors | `":"`, `":S1118"`, `"S1118:"`, `"S1118::S1"`, `"a:b:c"` |
| bad leading character | `".S1118"`, `".."`, `"-S1118"`, `"_S1118"` |
| too long | any value longer than 64 characters |

Nothing is ever repaired: T28 does **not** strip whitespace, does not lowercase,
does not escape, does not truncate and does not coerce (`True`, `7`, `1.5`,
`b"S1118"`, `pathlib.Path("S1118")` and arbitrary objects are refused as
`INVALID_INPUT`, never converted with `str()`). The only normalisation anywhere
in T28 is none at all: the caller's exact string is either an exact entry or it
is not.

### 20.4 Configuration contract

```python
RuleAllowlistPolicy(allowed_rules=("S1118", "S1192", "S3776"))
```

* `allowed_rules` is the list of rule IDs an automatic fix is approved for.
  Only an **immutable `tuple`** is accepted. `None`, a `list`, a `set`, a
  `frozenset`, a `dict`, a bare string, `bytes`, a generator/iterator and every
  other non-tuple are refused (`INVALID_POLICY`): a structure that can be
  mutated or reordered *after* it was validated cannot state a curated allowlist,
  and a bare string would silently be iterated character by character.
* The **default is the empty tuple**, which is valid and maximally strict: it
  allows no rule at all. That is the fail-closed default — nothing is enabled by
  omitting configuration, and there is no hidden default entry.
* Every entry must be a `str` that satisfies the grammar of §20.3. An entry of
  another type (`7`, `True`, `None`, `1.5`, `b"S1118"`, a `Path`, a nested
  tuple) is refused as well.
* **A duplicate entry is an error, not a deduplication.** `("S1118", "S1118")`
  is `INVALID_POLICY`, because silently collapsing it would hide a configuration
  mistake. Duplicates are compared as *exact* strings, so `("S1118", "s1118")` is
  a valid two-entry allowlist (they are two different rule identities) while
  `("S1118", "S1118")` is refused.
* The refusal reason names the offending entry by **index** (`allowed rule entry
  2`) and describes the class of the problem; the offending value itself is never
  echoed (§20.12).

### 20.5 Validation (what is refused, and in which order)

The configuration is validated first, and in this order: **shape**, then
**size**, then every entry in configuration order. Which means, for example, that
an oversized tuple is reported as oversized even when it also contains a wildcard
entry — the bound is enforced before any entry is read.

The caller's rule identity is validated next: the value must be a `str` (else
`INVALID_INPUT`) that satisfies the grammar of §20.3 (else `INVALID_RULE_ID`).
Both refusals are produced for exactly the cases listed in §20.3, and the
distinction is deliberate: a **type** problem is `INVALID_INPUT` and a
**content** problem is `INVALID_RULE_ID`, so a log can separate "the caller sent
the wrong kind of value" from "the caller sent a value that is not a rule ID".
The two sets are disjoint by construction, and tests pin that every non-string
value lands on the first and every malformed string on the second.

### 20.6 Limits (explicit, constant, tested at max-1 / max / max+1)

| Constant | Value | Why |
|----------|-------|-----|
| `MAX_RULE_ID_LENGTH` | `64` characters (qualifier included) | The longest rule key in this repository's own fixtures is 16 characters (`javascript:S1854`) and the widest real SonarQube analyzer key is well under 40, so 64 is roughly four times the widest real value. It also stays below T20's generic 100-character token cap, so every rule ID T28 accepts can still be published in a T20 commit message (§20.11). |
| `MAX_ALLOWED_RULES` | `1000` entries | A curated list of rules an AI agent is trusted to fix by itself is expected to be a few dozen long. A list of thousands is not a curation but an attempt to allow everything — the exact failure mode this policy exists to prevent. The bound also keeps every validation and comparison in T28 strictly bounded work. |

Both bounds are enforced *before* the work they bound, both are published in the
serialized policy, and both are covered by boundary tests.

### 20.7 Statuses

| Status | Value | Meaning |
|--------|-------|---------|
| `ALLOWED` | `allowed` | The rule ID is exactly one of the allowlisted rule IDs. The **only** status that authorises a fix. |
| `RULE_NOT_ALLOWED` | `rule-not-allowed` | The rule ID is a usable identity but is not listed — the ordinary, expected answer. |
| `INVALID_RULE_ID` | `invalid-rule-id` | A rule *string* was supplied and it is not a usable rule ID (content problem). |
| `INVALID_INPUT` | `invalid-input` | The record carries no rule ID *string* at all (`None` or another type). Absence is not "a rule that is merely unlisted". |
| `INVALID_POLICY` | `invalid-policy` | The allowlist configuration is not usable, so no rule can be shown to be approved. |

A `RuleAllowlistDecision` (`ALLOW`/`REFUSE`) is derived from the status exactly
like T24–T26, and the stable machine tokens are `T28_RULE_ALLOWED`,
`T28_RULE_NOT_ALLOWED`, `T28_RULE_ID_INVALID`, `T28_INPUT_INVALID` and
`T28_POLICY_INVALID`.

### 20.8 Precedence (explicit, documented and pinned)

```
INVALID_POLICY  ->  INVALID_INPUT  ->  INVALID_RULE_ID  ->  RULE_NOT_ALLOWED  ->  ALLOWED
```

The first status whose condition holds decides the verdict. It is deliberately
"policy, then identity, then lookup":

* an allowlist T28 cannot read refuses **before** any rule is read, so a
  configuration error can never be reported as `RULE_NOT_ALLOWED` (a judgement
  about a list that could not be read) and, above all, can never be reported as
  `ALLOWED`;
* a record that carries no rule string refuses **before** the value is examined;
* a malformed rule string refuses **before** the allowlist is consulted;
* only a well-formed rule identity is ever looked up, and only exact membership
  decides `ALLOWED` versus `RULE_NOT_ALLOWED`.

`PRECEDENCE` is exported as a tuple, a test pins it to exactly that order, and
every adjacent pair has at least one test case that proves which condition wins
(when both could apply, and — for `INVALID_INPUT` versus `INVALID_RULE_ID` —
that they are disjoint by construction).

### 20.9 Fail-closed behaviour

`can_auto_fix` is the only permission flag. It is a *derived* property
(`decision is ALLOW`, and `decision` is `ALLOW` only when `status` is in
`ALLOWED_STATUSES`, which contains exactly `ALLOWED`), so a refusal cannot
report permission and an `ALLOWED` verdict cannot carry a refusal status. A test
asserts `can_auto_fix is (status is ALLOWED)` for allowed, unlisted, malformed,
absent, empty-allowlist, `None`-allowlist, list-allowlist, duplicate-entry and
pattern-entry cases alike.

| Input | Result | `can_auto_fix` |
|-------|--------|----------------|
| unusable allowlist (anything but a valid tuple) | `INVALID_POLICY` | `False` |
| no rule stated (`None`) | `INVALID_INPUT` | `False` |
| rule is not a string | `INVALID_INPUT` | `False` |
| rule is not a usable rule ID | `INVALID_RULE_ID` | `False` |
| well-formed rule that is not listed | `RULE_NOT_ALLOWED` | `False` |
| exactly listed rule | `ALLOWED` | `True` |

Nothing but exact membership can reach the last row. T28 has no parameter, no
field, no constant and no branch through which severity, issue type, rule
family, rule prefix, file extension, language, historical success, confidence, a
previous-fix count or Sonar message text could influence the verdict — those
values cannot even be supplied (a test asserts the two public call parameters
and the single field of each DTO, and that no module-level name mentions them).

### 20.10 Trust boundary

Both inputs are **caller-asserted**. T28 establishes no fact about SonarQube: it
does not know whether the rule exists, whether it is active in the quality
profile, whether an issue really carries it, or whether a fix for it would be
correct. It validates the *shape* of the rule identity and performs one exact
membership test against the operator's list. A caller that misreports which rule
an issue carries is not detected here — and a caller that wants a rule allowed
must state that rule ID in the configuration explicitly.

This is a **policy boundary**, not an evidence-authentication boundary: it exists
so that a future orchestration layer cannot *accidentally* let the AI fixer edit
code for a rule the operator never approved, and so that the deny-by-default
behaviour does not depend on any other layer remembering to check something.
T28 never calls SonarQube, never reads a quality profile, never reads a file and
never inspects an issue: the rule ID is whatever the caller says it is.

### 20.11 Compatibility with the repository's own rule-key contract (T20)

The rule IDs T28 accepts are a strict **subset** of the rule keys the existing
commit layer accepts: T20's `commit_message` validates a rule key as
`[A-Za-z0-9][A-Za-z0-9._:-]*` with a 100-character cap, and T28 accepts an
ASCII-only, at-most-64-character, at-most-one-separator subset of that same
alphabet. A test iterates every rule ID from the T28 valid set and builds a real
T20 commit message with it, asserting that the rule ID is published in the
`Sonar-Rule` trailer — so a rule T28 allows can never be impossible to commit.

The two layers do not contradict each other, and they answer different
questions: T20 asks "is this message safe to hand to Git?", T28 asks "was this
rule approved for an automatic fix at all?". Neither imports the other, so the
policy stays independently testable (§20.15).

### 20.12 Immutability, purity, determinism and secret safety

* **Immutable records.** `RuleAllowlistPolicy`, `RuleAllowlistInput` and
  `RuleAllowlistEvaluation` are frozen dataclasses; assignment and deletion raise
  `FrozenInstanceError`, `reasons` is a tuple, and `as_dict()` returns freshly
  built containers so mutating a serialized copy cannot change a policy or a
  verdict. The configuration itself must already be a `tuple`, so T28 holds no
  reference to a structure a caller can still mutate.
* **No exposed internals.** The lookup set used by the evaluation is built inside
  the call from the validated tuple and is never published or stored; the public
  DTOs expose data, not internal sets.
* **Pure and deterministic.** The verdict is a pure function of
  `(policy, rule_input)`: no clock, no environment value, no random source, no
  cache, no counter, no global mutable state, no filesystem, no network, no
  subprocess and no Git. The same two arguments always produce an equal record
  (a test pins it, together with "no module state changed" and JSON-native
  output), and the configuration is consulted in its own order, so the
  serialized `allowed_rules` list is stable for a given configuration.
* **Secret and data safety.** Diagnostics are static, bounded and
  machine-readable. The two positions where caller text could leak are closed:
  (a) an *unusable* rule ID is never echoed — `rule_id` is reported as `None` and
  the refusal sentence is fixed text that names no value (a 100 000-character
  rule ID, a shell fragment, a URL carrying credentials, a path traversal and an
  over-long entry are all refused without any of them appearing anywhere in the
  serialized verdict); (b) an *accepted* rule ID is echoed only after a second,
  independent validation inside the result builder, so the field can never carry
  an unvalidated string. T28 reads no environment variable, no token, no
  credential and no issue message, and emits no command or argv.

### 20.13 Serialization

`RuleAllowlistEvaluation.as_dict()` is deterministic, JSON-native and
secret-free:

```json
{
  "policy_version": "t28.1",
  "status": "allowed",
  "decision": "allow",
  "can_auto_fix": true,
  "rule_id": "S1118",
  "diagnostic_code": "T28_RULE_ALLOWED",
  "reason": "Allowed (allowed) for rule 'S1118': the rule ID is exactly one of the allowlisted rule IDs, so this rule is approved for an automatic fix.",
  "reasons": ["<the same sentence>", "T28 allows only because the exact rule ID is explicitly listed; it proves nothing else about the rule or the issue."],
  "policy": {
    "policy_version": "t28.1",
    "allowed_rules": ["S1118", "S1192", "S3776"],
    "allowed_rule_count": 3,
    "maximum_supported_rules": 1000,
    "maximum_rule_id_length": 64,
    "is_valid": true,
    "refusal_reason": null
  }
}
```

* `RuleAllowlistEvaluation.rule_id` is the caller's rule ID **only when it is a
  usable rule ID** (`null` otherwise); `reasons` is always
  `(reason, fixed consequence)`.
* `RuleAllowlistPolicy.as_dict()` publishes the validated entries **in
  configuration order** (`null` plus a refusal reason when the configuration is
  unusable, so no malformed entry is echoed) together with both bounds.
* `RuleAllowlistInput.as_dict()` publishes the rule ID only when it is usable,
  plus `is_usable_rule_id`.
* No internal implementation detail (no set, no lookup structure, no counter), no
  command, no argv, no environment value, no path and no arbitrary input object
  appears in any of them.

### 20.14 Test guarantees

`tests/test_sonar_rule_allowlist.py` pins, at least:

* every valid rule ID form (bare and qualified) and every refused rule ID class
  (empty, whitespace-only, padded, control characters, non-ASCII, wildcards,
  regexes, paths, URLs, shell fragments, separator errors, bad leading
  characters, over-long values) and every refused non-string value;
* the maximum-length boundary at max-1 / max / max+1, for an input and for a
  configuration entry;
* exact matching: prefix, suffix, substring, extension, case and qualifier
  collisions in **both** directions, plus the exhaustive
  `rule_id in allowed_rules <=> ALLOWED` matrix over seven rules × six
  configurations;
* configuration validation: the default (empty) allowlist, `None`, every
  non-tuple container, non-string entries, malformed entries, duplicate entries,
  the entry bound at max-1 / max / max+1, the entry-length boundary, and the
  documented order of the refusal checks (size before entries);
* fail closed: `can_auto_fix is (status is ALLOWED)` and
  `decision is ALLOW <=> status is ALLOWED` for every case, and that no public
  parameter or DTO field can carry severity, type, language, confidence, history
  or message text;
* precedence: `PRECEDENCE` pinned to the documented order, one conflict case per
  condition, and the disjointness of the two malformed-input statuses;
* immutability: frozen assignment and deletion, the immutable configuration, a
  mutated serialized copy and a mutated caller-owned list;
* determinism: equal verdicts, unchanged module state, JSON-native output;
* serialization: the exact key set and values of all three `as_dict()`s;
* secret/data safety: huge and hostile inputs are refused and never published,
  and no environment/token/path token appears in a verdict;
* no wildcard or fuzzy behaviour: pattern entries and well-formed near-misses
  never match, and the module source contains no matching library import, no
  string-inspection method call (`startswith`, `casefold`, `strip`, `find`, …)
  and no string-literal membership test (AST-checked);
* the module surface: standard library only, no repository import in either
  direction, no dangerous call, the declared top-level definitions, a sorted
  `__all__`, read-only module tables and `__test__ = False`;
* not wired: no other production module (including `main.py`) mentions T28.

`sonar_rule_allowlist.py` is at **100% statement and 100% branch coverage** from
the focused run (`pytest tests/test_sonar_rule_allowlist.py
--cov=sonar_rule_allowlist --cov-branch`): 151 statements, 54 branches, 0 missing,
0 partial. The full test suite passes with the T28 tests included.

### 20.15 Integration status — and `main.py` remains unwired

* **T28 is a library with no caller.** `main.py` and T01–T27 are untouched; no
  production path imports `sonar_rule_allowlist`, and no existing module gained a
  dependency on it. Tests pin the absence of such an import **and** the absence
  of the names `sonar_rule_allowlist`, `RuleAllowlistPolicy`,
  `RuleAllowlistInput` and `evaluate_rule_allowlist` in every other production
  module.
* T28 executes nothing: no Git, no subprocess, no network, no SonarQube call, no
  Codex call, no file access and no orchestration. It is a decision function and
  nothing else.
* A future integration (a later task — **not** T28) would combine the stages that
  already exist: the T19 issue outcome, the T26 branch protection, the T27
  uncertainty policy and the T28 rule allowlist. Concretely, it would map the
  rule identity an issue carries (T04/T18) onto a `RuleAllowlistInput`, call
  `evaluate_rule_allowlist` with the operator's configured policy, and refuse to
  let the fixer touch a rule whose `can_auto_fix` is `False` — reporting the
  stable `diagnostic_code` so the refusal is auditable. That wiring is
  deliberately **not** part of T28: the policy must be independently testable
  first, and no orchestration is created prematurely.
* Until that integration exists, T28 is documentation plus a tested contract: it
  cannot change what the pipeline does, and the pipeline cannot change what T28
  answers.

### 20.16 Acceptance criteria

1. **Given** a rule ID that is exactly one of the configured allowlist entries,
   **then** the verdict is `ALLOWED` with `decision == ALLOW` and
   `can_auto_fix == True`.
2. **Given** a well-formed rule ID that is *not* listed — including a prefix,
   suffix, substring, extension, case variant or the bare/qualified counterpart
   of a listed rule — **then** the verdict is `RULE_NOT_ALLOWED` with
   `can_auto_fix == False`.
3. **Given** a wildcard, glob-like, regex-like, path-like, URL-like,
   shell-like, padded, whitespace-containing, control-character, non-ASCII,
   separator-malformed or over-long value, **then** it is refused
   (`INVALID_RULE_ID` as an input, `INVALID_POLICY` as a configuration entry)
   with `can_auto_fix == False`.
4. **Given** a non-string rule value (`None`, `True`/`False`, an integer, a
   float, `bytes`, a `Path`, a list, a mapping, an arbitrary object), **then**
   the verdict is `INVALID_INPUT`, nothing is coerced with `str()`, and
   `can_auto_fix == False`.
5. **Given** an unusable allowlist (`None`, a non-tuple, a malformed entry, a
   duplicate entry, an oversized tuple), **then** the verdict is
   `INVALID_POLICY`, no rule is judged at all — not even a perfectly listed one —
   and `can_auto_fix == False`.
6. **Given** both a malformed policy and a malformed rule, **then** the verdict
   is `INVALID_POLICY`: precedence (§20.8) is exact and documented.
7. **Given** any inputs, **then** only `ALLOWED` reports `can_auto_fix == True`;
   every other status reports `False`.
8. **Given** identical inputs, **then** the verdict is equal, `as_dict()` is
   identical, the caller's values are unchanged and no module state changed.
9. **Given** any inputs, **then** no I/O, no Git, no network, no command and no
   mutation occurs; no caller-authored value other than a *validated* rule ID
   appears anywhere in the verdict; and no configuration can make a merely
   similar rule match.
10. **Given** the default configuration (an empty allowlist), **then** every
    rule is denied.


## 21. T29 - bounded logging policy (implemented policy layer, not wired)

Status: **implemented** (`logging_policy.py`, version `t29.1`) - standalone -
pure - deterministic - immutable - fail-closed - secret-safe - **unwired**
(`main.py` and T01-T28 unchanged).

### 21.1 Purpose, scope and non-goals

T29 answers exactly one question:

    "may this log record be emitted, and if so, in exactly what safe form?"

It is a **policy layer only**. It writes nothing - no file, no stream, no standard
output and no `logging` record - it calls nothing, it orchestrates nothing, and it
touches no network, no Git, no SonarQube, no Codex and no file. Its whole output
is one immutable verdict per record, and that verdict is the *only* thing T29
publishes.

**In scope:** the record grammar (§21.3); the configuration contract (§21.4);
validation and refusal clauses (§21.5); the limits (§21.6); the statuses and
diagnostic codes (§21.7); the precedence (§21.8); the fail-closed contract - what
a refusal publishes, which is nothing but a fixed clause and, at most, the
record's own validated event code (§21.9); the trust boundary (§21.10);
redaction and secret safety (§21.11); immutability, purity and determinism
(§21.12); serialization (§21.13); the tests (§21.14); the integration boundary
(§21.15); the acceptance criteria (§21.16).

**Non-goals (explicit):**

* no log sink, no handler installation, no `logging` configuration and no
  formatting of a log line: T29 decides, the caller writes;
* no "log everything" switch, no implicit allowlist entry and no configuration
  that admits a record the operator did not approve; and no configuration that
  can switch redaction, bounding, fail-closed behaviour or unknown-field
  rejection **off** - the only configurable knobs are the two allowlists (which
  can only name what is permitted) and the level floor (which can only drop);
* no severity policy, no sampling, no rate limiting, no deduplication, no
  rotation, no structured-log schema, no tracing and no metrics;
* no secret *discovery*: T29 does not read the environment, a file, a vault or an
  `AnalysisConfig`, so it cannot know this project's configured secret values - it
  redacts credential-shaped text by shape and refuses credential-shaped
  identities;
* no Git, no commit, no push, no SonarQube call, no Codex call, no retry and no
  orchestration;
* no wiring into `main.py` (or any other module) in this task (§21.15).

### 21.2 The one rule - every condition must hold

A record may be emitted **only** when all of the following hold:

    event_code in allowed_event_codes
    and every field name in allowed_field_names
    and LEVEL_ORDER[level] >= LEVEL_ORDER[minimum_level]
    and every part is printable ASCII (or a permitted scalar) within its bound
    and no part published verbatim is credential-shaped

Anything else is refused, and a refused record publishes no message and no field at
all: not its text, not a field name, not a field value, not a length of a text, no
hash and no partial value. Two statuses can emit - `ACCEPTED` and `REDACTED` -
`EMITTABLE_STATUSES` is exactly that pair, and `can_emit` is *derived* from
`decision`, which is derived from `status`, so no construction, no serialization
and no caller can turn a refusal into an emission. A refusal is therefore a drop
with a fixed diagnostic: the only caller string it can repeat is the record's own
event code, and only when that code has already proved to be a well-formed,
bounded, credential-free *identity* (§21.9).

The implementation makes that structural rather than conventional: the validated
allowlists are turned into `frozenset`s and the only approval tests in the module
are membership of those sets, so a code or a field name that is merely *similar*
to an entry - a prefix, a suffix, a substring, a case variant, a qualified or
dotted spelling - is never approved. The default configuration approves T29's own
three verdict event codes and **no field name at all**, so the default behaviour
publishes no caller-supplied field until an operator names the fields they want
logged.

### 21.3 The record grammar

A `LogEvent` has exactly four parts, and each has one accepted shape:

| Part | Accepted | Refused (`INVALID_EVENT` unless stated) |
|------|----------|------------------------------------------|
| `event_code` | one upper-case letter, then upper-case letters, digits and `_`, at most 64 characters | absent, non-`str`, empty, whitespace, lower case, a leading digit/`_`/punctuation, `-`, `.`, `:`, `/`, `\`, `*`, `?`, `[`, `]`, `^`, `$`, `(`, `)`, `\|`, `;`, `#`, `=`, a control character, a non-ASCII character, a `str` subclass |
| `level` | a `LogLevel` member (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | absent, `"info"`, `10`, `2.5`, `True`, `bytes`, a tuple, an arbitrary object - no raw value is ever mapped onto a level |
| `message` | non-empty printable ASCII (code points `0x20`-`0x7E`), at most 500 characters | absent, non-`str`, empty, `\n`, `\r`, `\t`, `\x00`, an ANSI escape, `\x7f`, a non-ASCII character, an over-long value (`VALUE_TOO_LARGE`) |
| `fields` | an immutable `tuple` of at most 20 immutable `(name, value)` pairs | absent/`None`, a list, a set, a frozenset, a mapping, a string, `bytes`, a generator, any other container - none of them is ever iterated |
| `fields[i][0]` (name) | one lower-case letter, then lower-case letters, digits and `_`, at most 64 characters, unique within the record | empty, upper case, a leading digit/`_`, `-`, ` `, `.`, `:`, `/`, `!`, a control character, a non-ASCII character, a repeat, an over-long name (`VALUE_TOO_LARGE`) |
| `fields[i][1]` (value) | `None`, a `bool`, an `int` of magnitude at most 2 ** 53, a finite `float`, or printable ASCII `str` of at most 200 characters | every other object (`bytes`, `bytearray`, a list, a tuple, a mapping, a set, a `complex`, an enum, an arbitrary object), a non-finite `float` (`nan`, `inf`, `-inf`), a control or non-ASCII character in a text value, an out-of-range integer (`VALUE_TOO_LARGE`), an over-long text value (`VALUE_TOO_LARGE`) |

Nothing is ever repaired: T29 does **not** trim, case-fold, escape, encode,
transliterate, truncate or coerce. There is no `str()`, no `repr()`, no
`bytes()`, no `format()` and no f-string interpolation of a caller value anywhere
in the module, so a value that is an arbitrary object is refused without a single
method of it ever being called. Every type check is an **exact** type check except
for the immutable `LogLevel` enum, so a `str`, `int` or `float` subclass whose
`__len__`, `__eq__` or `__index__` the caller overrides is refused as well: a
subclass can change what "measuring" means, so it is not a string T29 can measure.

Event codes and field names are *identities*, and case is identity: `MY_EVENT` is
not `my_event`, and `T29_LOG_ACCEPTED` is not `t29_log_accepted`. The two
vocabularies use opposite case on purpose, so an event code and a field name can
never be confused for one another.

### 21.4 Configuration contract

```python
LoggingPolicy(
    allowed_event_codes=("T29_LOG_ACCEPTED", "MY_EVENT"),
    allowed_field_names=("issue_key", "attempt"),
    minimum_level=LogLevel.INFO,
)
```

* `allowed_event_codes` is the set of event codes that may be logged. Only an
  **immutable `tuple`** is accepted: `None`, a list, a set, a frozenset, a
  mapping, a bare string, `bytes` and a generator are refused
  (`INVALID_POLICY`), because a structure that can be mutated or reordered *after*
  validation cannot state what may be logged, and a bare string would silently be
  read character by character. Every entry must be a usable event code (§21.3)
  that is **not** credential-shaped, no entry may repeat, and the tuple may hold
  at most 100 entries. The **default** is T29's own three verdict codes
  (`T29_LOG_ACCEPTED`, `T29_LOG_REDACTED`, `T29_LOG_REJECTED`).
* `allowed_field_names` follows the same rules with the field-name grammar, the
  64-character entry length and a 100-entry bound. The **default is the empty
  tuple**, which publishes no caller-supplied field at all: that is the
  fail-closed default, and no default entry is ever added.
* `minimum_level` is the least severe level that may be logged, as a `LogLevel`
  member (`DEBUG` by default, which drops nothing). A record below the floor is
  refused (`REJECTED`). The knob can only **tighten**: raising it drops records,
  and no value of it admits a record whose code or field is not allowlisted.
* An entry that **repeats** is an error, not a deduplication, and an entry that
  is credential-shaped is refused too, so a policy can never make T29 publish a
  credential-shaped identity even through its own configuration.
* The refusal reason names the offending entry by **index** (`allowed event code
  entry 2`) and never echoes the entry, because a refused entry may be anything at
  all, including a credential.

### 21.5 Validation (what is refused, and in which order)

The configuration is validated first: **shape**, then **size**, then every entry
in configuration order - so an oversized tuple is reported as oversized even when
it also contains a wildcard entry, because the bound is enforced before any entry
is read.

The record is validated next, part by part, in reading order: **event code**,
**level**, **message**, the **fields container**, then **each field entry** in
order. Within one part the refusal classes are applied in `PRECEDENCE` order
(§21.8), which means:

* a part must be *usable* (right type, right grammar, printable) before it is
  measured (`INVALID_EVENT` / `INVALID_FIELD` before `VALUE_TOO_LARGE`);
* every part must be usable and bounded before any credential shape is looked for
  (`VALUE_TOO_LARGE` before `SECRET_DETECTED`);
* a credential-shaped *identity* refuses before the allowlist is consulted
  (`SECRET_DETECTED` before `REJECTED`), because an identity is never rewritten
  and there is nothing to approve;
* the operator's approval is consulted before any text is redacted (`REJECTED`
  before `REDACTED`), because a record the operator did not approve is not
  redacted - it is dropped.

Two negative clauses matter as much as the positive ones. Absence is never a
default: a `None` level, message, event code or fields container is a refusal, not
an empty value. And nothing is ever echoed: a refusal names the offending part
(`the event code`, `the message`, `field entry 2`) and, at most, the *category* of
a credential shape - never the value, never its type name, never its length and
never a partial form of it.

### 21.6 Limits (explicit, constant, tested around the boundary)

| Constant | Value | What it bounds |
|----------|-------|----------------|
| `MAX_EVENT_CODE_LENGTH` | 64 | one event code |
| `MAX_MESSAGE_LENGTH` | 500 | one message |
| `MAX_FIELD_NAME_LENGTH` | 64 | one field name |
| `MAX_FIELD_VALUE_LENGTH` | 200 | one text field value |
| `MAX_FIELDS` | 20 | field entries per record |
| `MAX_FIELD_INTEGER` | 2 ** 53 | the magnitude of an integer value |
| `MAX_ALLOWED_EVENT_CODES` | 100 | the allowlisted event codes |
| `MAX_ALLOWED_FIELD_NAMES` | 100 | the allowlisted field names |
| `REDACTION_MARKER` | `"[REDACTED]"` | the whole replacement for a credential span |

Every bound is a fixed module constant that **no configuration can widen, raise or
switch off**. A bound violation is `VALUE_TOO_LARGE`, and the offender is never
truncated: an over-long value is refused, never silently shortened, because a
truncated value is a different value. The two "bounded, not truncated" rules that
follow from this are:

* `MAX_FIELD_INTEGER` keeps every published integer inside the range a JSON
  consumer round-trips exactly, so an integer is refused rather than published
  lossily;
* redaction can only *grow* text, so the redacted result is measured again
  (§21.11) and refused (`VALUE_TOO_LARGE`) rather than published over-long.

### 21.7 Statuses and diagnostic codes

| Status | Value | Diagnostic code | Decision |
|--------|-------|-----------------|----------|
| `ACCEPTED` | `accepted` | `T29_LOG_ACCEPTED` | `EMIT` |
| `REDACTED` | `redacted` | `T29_LOG_REDACTED` | `EMIT` |
| `INVALID_POLICY` | `invalid-policy` | `T29_POLICY_INVALID` | `DROP` |
| `INVALID_EVENT` | `invalid-event` | `T29_EVENT_INVALID` | `DROP` |
| `INVALID_FIELD` | `invalid-field` | `T29_FIELD_INVALID` | `DROP` |
| `VALUE_TOO_LARGE` | `value-too-large` | `T29_VALUE_TOO_LARGE` | `DROP` |
| `SECRET_DETECTED` | `secret-detected` | `T29_SECRET_DETECTED` | `DROP` |
| `REJECTED` | `rejected` | `T29_LOG_REJECTED` | `DROP` |

Every status has exactly one diagnostic code and every code belongs to exactly one
status, so a report can be grouped and alerted on without parsing prose. The code
carries no caller value: it is a fixed token.

### 21.8 Precedence

`PRECEDENCE` is exactly:

```python
PRECEDENCE = (
    LogStatus.INVALID_POLICY,
    LogStatus.INVALID_EVENT,
    LogStatus.INVALID_FIELD,
    LogStatus.VALUE_TOO_LARGE,
    LogStatus.SECRET_DETECTED,
    LogStatus.REJECTED,
    LogStatus.REDACTED,
    LogStatus.ACCEPTED,
)
```

The rule is one-dimensional and testable: **the record's parts are read in order
(event code, level, message, the fields container, then each field entry), and for
the first part that is refused the first applicable class of `PRECEDENCE` decides
the status.** Consequences, each pinned by a conflict test:

| Conflict | Reported status | Why |
|----------|-----------------|-----|
| malformed event code + malformed message + malformed field | `INVALID_EVENT` | the code is read first |
| well-formed but over-long code + a malformed message | `INVALID_EVENT` | content is judged before size, so the message is still "the record" |
| malformed message + malformed field | `INVALID_EVENT` | the record is read before its fields |
| malformed field + an over-long message | `INVALID_FIELD` | the entry is judged before any size is measured |
| over-long field name + a credential-shaped field name | `VALUE_TOO_LARGE` | the bound is measured before credentials are searched for |
| credential-shaped event code that is also unapproved | `SECRET_DETECTED` | an identity is never rewritten, so it is refused before approval is consulted |
| unapproved event code + credential-shaped text | `REJECTED` | approval precedes redaction, so nothing is redacted for a record that is dropped anyway |
| approved record with credential-shaped text | `REDACTED` | approval precedes redaction, and redaction precedes acceptance |

The last two rows are the fail-closed heart of the ordering: redaction is a
*publishing* step, so it can only ever turn an already-approved record from
`ACCEPTED` into `REDACTED` - it can never rescue a record that would otherwise be
refused, and it can never turn a refusal into an emission.

### 21.9 Fail closed - what a refusal publishes

The verdict `LogEvaluation` is the only thing T29 produces, and for a refusal its
two text fields are empty:

| Field | Emitting verdict | Refused verdict |
|-------|------------------|-----------------|
| `message` | the validated, bounded, redacted message | `None` |
| `fields` | the validated, bounded, redacted pairs | `()` |
| `event_code` | the caller's code (well-formed and credential-free) | the caller's code **only** when the refusal was reached *after* the code was validated as a well-formed, bounded, credential-free identity; otherwise `None` |
| `level` | the caller's `LogLevel` | the caller's `LogLevel` when it was usable, else `None` |
| `can_emit` | `True` | `False` |

So a refusal publishes **no message and no field at all**: not the message, not a
field name, not a field value, not a length, not a hash and not a partial value -
and it does not publish the *shape* of what was refused either, beyond a fixed
category label when a credential shape matched an identity. The rationale trail is
always `(reason, consequence)`, so nothing can be appended to it.

One caller string is deliberately exempt, and it is the narrowest possible one:
the record's **own event code**, once it has proved to be a usable, credential-free
identity (`PRECEDENCE` runs `INVALID_EVENT`, `VALUE_TOO_LARGE` and
`SECRET_DETECTED` *before* `REJECTED`, so a code reaches a refusal's `event_code`
only when it is well-formed, within `MAX_EVENT_CODE_LENGTH` and free of every
shape in `SECRET_PATTERNS`). That is what makes a `REJECTED` or `INVALID_FIELD`
verdict actionable - it says *which* identity was refused - and it follows the
convention the sibling policies already use for a validated identity (T26's
`candidate_branch`, T28's `rule_id`). Absent, malformed, over-long and
credential-shaped codes are never echoed, by any status; a credential-shaped code
and a credential-shaped field name are refused with `SECRET_DETECTED` and are
absent from the verdict entirely.

`can_emit` is the only permission flag, it is derived from `decision`, and
`decision` is derived from `status`; a status outside `EMITTABLE_STATUSES` cannot
report permission and an emitting verdict cannot carry a refusal status. The
invariants `status in EMITTABLE_STATUSES <=> can_emit`, `decision is EMIT <=>
can_emit` and `was_redacted <=> status is REDACTED` hold for every verdict the
module can produce, and the tests pin them for every status.

### 21.10 Trust boundary

Everything a caller hands T29 is untrusted, and the boundary is drawn explicitly:

* **No coercion.** No `str()`, `repr()`, `bytes()`, `format()`, f-string
  interpolation of a caller value, `__format__`, `__str__` or `__iter__` is ever
  invoked on a caller value. A value of the wrong type is refused at once, so an
  object with a hostile `__str__`, `__len__`, `__eq__` or `__hash__` is refused
  *without a single method of it being called* - the tests prove it with an object
  whose methods raise and a counter that must stay empty.
* **No iteration of an unvalidated container.** A list, set, mapping, string,
  bytes or generator offered as `fields` is refused by type, never walked; a
  generator handed to the policy is refused without being consumed.
* **No arbitrary serialization.** A published value is only ever `None`, a `bool`,
  a bounded `int`, a finite `float` or bounded printable `str` - all of them
  scalar, immutable and JSON-native. No `__dict__`, no mapping, no dataclass and no
  caller object is ever copied into a verdict.
* **No I/O and no global state.** T29 imports the standard library only (`re`,
  `dataclasses`, `enum`, `types`, `typing`) and no repository module; it contains
  no `logging`, `os`, `sys`, `io`, `subprocess`, `socket`, `random`, `time`,
  `uuid`, `hashlib` or `json` import; it opens no file, starts no process, makes no
  network call, reads no environment variable and does not read `sys.argv`. It
  never calls `logging.basicConfig`, never acquires a logger, never installs a
  handler and never writes anywhere: configuring a process's logging is the
  caller's job, and a policy that could reconfigure it could hide what it refused.
* **No exception swallowing.** The module contains no `try`/`except` and raises
  exactly one exception type (`TypeError`) for the two caller errors - a wrong
  `policy` or `event` type - because a caller error is not a record problem: made
  through the wrong door, it must be loud rather than silently logged. Even those
  two messages name nothing the caller wrote.

### 21.11 Redaction and secret safety

Text is published only after every credential-shaped span in it has been replaced
by the single marker `[REDACTED]`:

* the marker is the **whole** replacement: no prefix, no suffix, no length, no
  hash, no truncation and no partial form of a credential is ever published, and
  the text around a span is kept verbatim;
* detections are collected as spans from every pattern in `SECRET_PATTERNS`,
  sorted and merged, and the text is rebuilt once - so overlapping or adjacent
  detections collapse into one marker and inserted text is never re-scanned;
* a credential shape found in a part that must be published **verbatim** - the
  event code or a field name, which are the identities the allowlist is consulted
  for - refuses the whole record (`SECRET_DETECTED`) instead of rewriting an
  identity: an identity that changes is not an identity;
* a credential shape found in **text** (a message, a text field value) redacts
  that span and emits the rest of the record (`REDACTED`);
* redaction can only grow text, so the redacted result is measured again and a
  value that no longer fits its bound is refused (`VALUE_TOO_LARGE`) rather than
  published over-long;
* diagnostics never echo the offending material: they name the part by *index*
  and, at most, the fixed category label that matched, never the matched text.

The category table is `SECRET_PATTERNS`, eight labelled patterns, in the order
that decides which label is reported when several match:

| Label | Shape |
|-------|-------|
| `url-userinfo` | `scheme://user[:password]@host` (the same shape `secret_scan` already treats as a credential) |
| `credential-pair` | `password=`/`token:`/`api_key=`/`authorization:`/`cookie`/`session`/`private_key`/`client_secret`/`signing_key` followed by a value |
| `auth-scheme` | `Bearer <token>` / `Basic <blob>` |
| `json-web-token` | three base64url segments separated by dots |
| `prefixed-token` | `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_`/`github_pat_`, `sk-`, `AKIA`/`ASIA`, `xox[bpasr]-`, `AIza`, `ya29.` |
| `long-hex-run` | 32 or more hexadecimal characters |
| `long-base64-run` | 40 or more base64 characters, optionally padded |
| `private-key-marker` | a `-----BEGIN ... PRIVATE KEY-----` / `-----END ...` banner |

Three properties of the table are deliberate and tested:

* **it is over-inclusive.** A 40-character commit SHA, a UUID-looking hex run or a
  long identifier is redacted too, because the policy cannot tell a credential
  from an identifier by shape alone. Publishing a false positive costs a marker in
  a log line; publishing a false negative costs a credential.
* **the caps are above the policy's own text bounds.** Every run cap is 4096, far
  above `MAX_MESSAGE_LENGTH` (500) and `MAX_FIELD_VALUE_LENGTH` (200), so a
  credential that fills an entire publishable text is matched *in full*. A cap that
  stopped short of a text's own bound would leave the tail of a long credential
  unredacted, which is the one failure mode the table exists to prevent.
* **no pattern requires zero characters, and every quantifier is bounded except
  the two whitespace runs of `credential-pair`**, so a match is linear work on an
  in-memory string - no nested quantifier, no backreference, no catastrophic
  backtracking and no zero-length span that could make redaction loop. The
  exception is deliberate: a keyword separated from its `=`/`:` by whitespace must
  still be redacted, and inside the text this policy validates that run is bounded
  by the text's own bound anyway - the only whitespace printable ASCII allows is
  the space character, and a message is at most 500 characters and a text field
  value at most 200. A test pins the exception and its reason.

The two public helpers expose exactly the redaction the policy performs, and
nothing else: `sanitize_log_value(text)` returns `(safe_text, redacted)` and
`sanitize_log_fields(fields)` returns a new tuple with every text value redacted
and every scalar passed through unchanged. Neither helper enforces a bound or a
grammar: it is not a validator, and the policy is what validates *before* it
redacts - a caller that holds untrusted input calls `evaluate_log_event`, not a
helper. A non-string through `sanitize_log_value` and a malformed entry through
`sanitize_log_fields` are `TypeError`s (caller errors), and their messages name the
offending entry by index only.

### 21.12 Immutability, purity and determinism

* The three records are frozen dataclasses: assignment and deletion raise
  `FrozenInstanceError`, their fields are reachable but not rebindable, and the
  configuration holds only tuples and enum members.
* Module-level tables are immutable: `EMITTABLE_STATUSES`, `PRECEDENCE`,
  `DEFAULT_ALLOWED_EVENT_CODES`, `DEFAULT_ALLOWED_FIELD_NAMES` and
  `SECRET_PATTERNS` are tuples (or tuples of pairs), and `DIAGNOSTIC_CODES`,
  `LEVEL_ORDER`, `_STATUS_REASONS` and `_STATUS_CONSEQUENCES` are
  `MappingProxyType`s that raise `TypeError` on assignment.
* `evaluate_log_event` is pure: it reads only its two arguments, writes nothing,
  remembers nothing, counts nothing and performs no filesystem, network,
  subprocess, Git or logging call. The same pair always produces an equal verdict,
  the caller's records are never mutated (not even a mutable list offered as
  `fields`), and the module's own namespace is unchanged after any number of
  evaluations.
* `as_dict()` returns fresh containers on every call, so mutating a published copy
  cannot change the verdict it came from, and a caller that mutates its own input
  afterwards cannot change what a verdict carries - published fields are immutable
  pairs.
* Every decision is deterministic: no clock, no random source, no hash iteration
  order, no locale, no environment and no `set` ordering reaches a published value.
  The pattern table order decides the reported category, and spans are sorted
  independently of the order in which patterns matched.

### 21.13 Serialization

`LogEvaluation.as_dict()` is deterministic, JSON-native and secret-free:

```json
{
  "policy_version": "t29.1",
  "status": "redacted",
  "decision": "emit",
  "can_emit": true,
  "was_redacted": true,
  "event_code": "T29_LOG_REDACTED",
  "level": "warning",
  "diagnostic_code": "T29_LOG_REDACTED",
  "reason": "Emitted (redacted) for event 'T29_LOG_REDACTED': the record is approved, but credential-shaped text was found ...",
  "reasons": ["<the same sentence>", "Consequence: the record is emitted with the redaction marker standing in for every credential-shaped span, and no prefix, length, hash or partial form of a credential is published."],
  "message": "calling [REDACTED] now",
  "fields": [["issue_key", "ACV2-642"], ["attempt", 3]],
  "policy": {
    "policy_version": "t29.1",
    "allowed_event_codes": ["T29_LOG_ACCEPTED", "T29_LOG_REDACTED", "T29_LOG_REJECTED"],
    "allowed_event_code_count": 3,
    "allowed_field_names": ["issue_key", "attempt"],
    "allowed_field_name_count": 2,
    "minimum_level": "debug",
    "maximum_supported_event_codes": 100,
    "maximum_supported_field_names": 100,
    "is_valid": true,
    "refusal_reason": null
  }
}
```

* `LogEvaluation.message` and `LogEvaluation.fields` hold *only* values that
  passed the whole policy: `null` and `[]` for a refusal, the redacted text for an
  emitted record, and `fields` as ordered `[name, value]` pairs (names are unique
  by validation, so the list is unambiguous and order-preserving).
* `LoggingPolicy.as_dict()` publishes the validated entries in configuration order
  (`null` plus a refusal reason when the configuration is unusable, so no malformed
  entry is echoed) together with both bounds and the level floor.
* `LogEvent.as_dict()` publishes **no caller text other than the code itself**:
  only the event code (and only when it is usable *and* credential-free), the level
  when it is a member, and the *count* of field entries - never the message, a
  field name, a field value or even the length of a text.
* No internal implementation detail (no span, no set, no lookup structure, no
  counter), no command, no argv, no environment value, no path and no arbitrary
  input object appears in any of them.

### 21.14 Test guarantees

`tests/test_logging_policy.py` pins, at least:

* the tables: the version and marker, every limit, the five levels and the total
  level order, the eight statuses, the two decisions, `EMITTABLE_STATUSES`,
  `PRECEDENCE` (exact order, no repeats, all statuses), the one-to-one diagnostic
  code map, the two defaults, and the eight credential-shape labels in table order
  (each with a positive sample whose expected label is the first match, and the
  property that no pattern matches an empty string - plus the pattern quantifiers:
  every one is bounded except the two documented `credential-pair` whitespace runs,
  which a spaced credential pair is still redacted for);
* the record grammar: every accepted form and every refused class for the event
  code, level, message, field container, field name and field value - including
  wildcards, regexes, paths, URLs, shell fragments, separators, case variants,
  control characters, non-ASCII characters, `str`/`int`/`float` subclasses,
  non-finite floats and enum values used as data;
* absence everywhere it can occur (a `None` code, level, message or container),
  each with its own clause and its own status, and the fact that absence is never
  read as an empty value or a default;
* the bounds, at max-1 / max / max+1: event-code length, message length, field
  count, field-name length, text-value length and integer magnitude, plus the
  "not truncated" guarantee (an over-long value is refused and nothing of it is
  published);
* redaction: one marker per merged span, two markers for two spans, no prefix,
  length, hash or partial value surviving, the surrounding text preserved,
  over-redaction thresholds pinned at 31/32 hex and 39/40 base64 characters, a run
  that fills a whole publishable text matched in full, and the redaction-expansion
  bound for both a message and a field value;
* identity refusals: a credential-shaped event code and a credential-shaped field
  name refuse the record with the fixed category label and never echo the identity,
  and no credential material reaches a diagnostic or a serialized verdict;
* fail closed: `status in EMITTABLE_STATUSES <=> can_emit`,
  `decision is EMIT <=> can_emit` and `was_redacted <=> status is REDACTED` for
  every status; a refusal publishes `message is None` and `fields == ()` across
  seven refusal classes; only an approved record can be redacted; and the refusal
  echo rule is pinned directly - a refusal repeats the record's own event code only
  when that code is a usable, credential-free identity, and an absent, malformed,
  over-long or credential-shaped code is absent from `event_code`, from the reason
  and from the serialized verdict;
* precedence: `PRECEDENCE` pinned to the documented order plus a conflict case per
  adjacent pair (record before fields, content before size, entry before size, size
  before credentials, credentials before approval, approval before redaction);
* approval: the default policy approves only its three codes and no field, an empty
  vocabulary approves nothing, a merely similar code is refused, the floor only
  drops, the floor is consulted before the code and the fields, and the code is
  consulted before the field names;
* the configuration contract: `None` and every non-tuple container, non-string and
  malformed entries, credential-shaped entries, duplicate entries, the entry bound
  and the entry length at their boundaries, an unconsumed generator, and the
  documented check order (size before entries);
* immutability and determinism: frozen assignment and deletion of all three
  records, read-only module tables, an unchanged module namespace, an unmutated
  caller (including a mutable list and an unconsumed generator), equal verdicts for
  equal inputs, and a mutated published copy that cannot change a verdict;
* serialization: the exact key set and values of all three `as_dict()`s, a JSON
  round trip for emitted records (including `None`, a maximal integer, a float and
  a bool), and the fact that `LogEvent.as_dict()` publishes no caller text other
  than the code itself;
* adversarial input: a million-character message is refused quickly, pathological
  repetition does not backtrack, and objects whose `__len__`, `__eq__`, `__hash__`,
  `__str__`, `__repr__` or `__format__` raise are refused **without any of them
  being called** (the counter stays empty), as are hostile configuration and field
  containers;
* the module surface: standard-library-only imports, no repository import, no
  forbidden module, no dangerous call, no logger and no string-inspection method,
  `re` used only for `compile`, no `try`/`except`, only `TypeError` raised, every
  import at module level, the exact public surface, and the documented contract in
  the module docstring;
* that T29 is **not wired**: no other module imports `logging_policy` or mentions
  any of its names, and `main.py` mentions neither the task nor the policy.

Result on the implementation this section describes: **428 statements, 220
branches, 0 missed, 0 partial** (`pytest --cov=logging_policy --cov-branch`), 433
tests. The full suite passes with the T29 tests included.

### 21.15 Integration status - and `main.py` remains unwired

* **T29 is a library with no caller.** `main.py` and T01-T28 are untouched; no
  production path imports `logging_policy`, and no existing module gained a
  dependency on it. Tests pin the absence of such an import **and** the absence of
  the names `logging_policy`, `LoggingPolicy`, `LogEvent`, `LogEvaluation`,
  `evaluate_log_event`, `sanitize_log_value` and `sanitize_log_fields` in every
  other production module.
* T29 executes nothing: no Git, no subprocess, no network, no SonarQube call, no
  Codex call, no file access, no logger and no orchestration. It is a decision
  function and nothing else.
* A future integration (a later task - **not** T29) would be the point where the
  pipeline's own logging is replaced by a call to this policy: an operation that
  wants to log would build a `LogEvent` from what it actually knows (its stage's
  event code, the level, a fixed message and a few structured field values), call
  `evaluate_log_event` with the operator's configured `LoggingPolicy`, and act on
  the verdict - emit the returned `message` and `fields` when `can_emit` is `True`,
  and drop the record when it is `False`. Because the verdict carries the *bound,
  sanitized* text, the emitting caller never touches an unvalidated value:
  `message` and `fields` are the only things it may publish, and they are already
  redacted and bounded. That wiring is deliberately **not** part of T29: the policy
  must be independently testable first, and no orchestration is created
  prematurely. In particular, T29 does not replace `secret_scan` (which prevents a
  *commit* from carrying a configured secret) and does not replace the existing
  credential redaction in `repository.py` (which rewrites external tool output); it
  is the contract a *log record* would have to satisfy.
* Until that integration exists, T29 is documentation plus a tested contract: it
  cannot change what the pipeline does, and the pipeline cannot change what T29
  answers.

### 21.16 Acceptance criteria

1. **Given** an allowlisted event code, an allowlisted field name and a level at or
   above the floor, **then** the verdict is `ACCEPTED` with `decision == EMIT` and
   `can_emit == True`, and the verdict carries the validated message and pairs.
2. **Given** credential-shaped text in a message or in a text field value of an
   approved record, **then** the verdict is `REDACTED`, `can_emit == True`, every
   credential span is exactly `[REDACTED]`, and no prefix, length, hash or partial
   form of it appears anywhere in the verdict.
3. **Given** an event code, a field name or a level the operator did not approve,
   **then** the verdict is `REJECTED` with `can_emit == False`, `message is None`
   and `fields == ()`; no message, field name, field value or length is published,
   and the only caller string the verdict may repeat is the record's own event code
   - and only because it was already validated as a usable, credential-free
   identity (§21.9). A credential-shaped code, a malformed code and an over-long
   code are never named by the refusal that refuses them.
4. **Given** a `not allowlisted` record that also carries credential-shaped text,
   **then** the verdict is `REJECTED` and nothing is redacted: approval precedes
   redaction, so a dropped record is not sanitized, it is dropped.
5. **Given** a wildcard-like, regex-like, path-like, URL-like, shell-like,
   padded, whitespace-containing, lower-case, control-character, non-ASCII or
   over-long event code, **then** it is refused (`INVALID_EVENT`, or
   `VALUE_TOO_LARGE` when it is merely too long) with `can_emit == False`.
6. **Given** a non-string message, event code or a non-`LogLevel` level
   (`None`, `True`, an integer, a float, `bytes`, a container, an arbitrary
   object), **then** the verdict is `INVALID_EVENT`, nothing is coerced, and
   `can_emit == False`.
7. **Given** fields that are not an immutable tuple of immutable `(name, value)`
   pairs of permitted scalars, **then** the verdict is `INVALID_EVENT` (bad
   container) or `INVALID_FIELD` (bad entry), the offending entry is named by
   index, and `can_emit == False`.
8. **Given** a credential-shaped event code or field name, **then** the verdict is
   `SECRET_DETECTED`, the identity is never echoed, and `can_emit == False`.
9. **Given** an unusable configuration (`None`, a non-tuple, a malformed entry, a
   credential-shaped entry, a duplicate entry, an oversized tuple, a
   non-`LogLevel` floor), **then** the verdict is `INVALID_POLICY`, no record is
   judged at all - not even a perfectly allowlisted one - and `can_emit == False`.
10. **Given** any input, **then** only `ACCEPTED` and `REDACTED` report
    `can_emit == True`; every other status reports `False`.
11. **Given** two problems in one record, **then** the verdict is the first class
    of `PRECEDENCE` for the first part that is refused (§21.8) - the order is exact
    and documented.
12. **Given** identical inputs, **then** the verdict is equal, `as_dict()` is
    identical, the caller's records are unchanged and no module state changed.
13. **Given** any input, **then** no I/O, no Git, no network, no logger, no command
    and no mutation occurs; no caller-authored value other than a *validated* part
    appears anywhere in the verdict; and no configuration can widen a bound or
    switch redaction, fail-closed behaviour or unknown-field rejection off.
14. **Given** the default configuration, **then** only T29's own three verdict
    event codes may be logged and **no** caller-supplied field may be published.

## 22. T30 — end-to-end orchestration (implemented)

Status: **implemented** (`pipeline/config.py`, `pipeline/logging.py`,
`pipeline/run.py`, version `t30.1`) · the first and only composition layer ·
fail-closed · every I/O boundary injectable · invoked by `main.py --run`.

### 22.1 Purpose, scope and non-goals

T30 wires the existing stages (T01–T29) into one run, in the documented order:

```
SonarQube -> issue selection -> clone/branch -> issue context
    -> Codex prompt -> Codex execution -> result analysis
    -> working-tree diff -> change scope -> project tests
    -> analysis trigger -> analysis wait -> issue verification
    -> final status -> (branch/uncertainty policy) -> commit -> push
    -> per-issue report -> overall report
```

* **In scope**: configuration loading; per-issue lifecycle execution; the T24
  selection bound; the T25 attempt bound; the T26/T27/T28 gates; the T20/T21
  irreversible steps (opt-in); T22/T23 reporting; T29-bound logging.
* **Non-goals**: no retry/iteration loop (T25 decides whether one attempt may
  run; the pipeline runs exactly one), no PR creation, no CI/container wiring,
  no re-implementation of any policy (every decision stays in its module).

### 22.2 The orchestration package

| Module | Role |
|--------|------|
| `pipeline/config.py` | `PipelineConfig` + fail-closed `load_pipeline_config` |
| `pipeline/logging.py` | `PolicyLogger` and the default `LoggingPolicy` (T29) |
| `pipeline/run.py` | `FixPipeline`, `PipelineDependencies`, the run DTOs |

The package lives under `pipeline/` deliberately: the top-level modules are the
pure, dependency-free library, and T30 is the composition boundary that is
allowed to depend on several of them. The root-level "not wired" guarantees
(§19.15, §20.15, §21.15) remain true of the library modules; T30 is their
first consumer.

### 22.3 Configuration (fail closed)

`load_pipeline_config(env)` reads `FIXER_*` values (plus `SONAR_URL` /
`SONAR_TOKEN` / `PROJECT_KEY`). Required: repository URL, source branch, work
directory, test command, analysis command, Sonar URL and project key. The T24
`max_issue_limit` and T25 `max_iterations` bounds are optional at load time but
their absence is refused by the owning policy at run time (an unconfigured
limit is **not** read as "unlimited"). A missing or malformed value raises
`PipelineConfigError` naming the variable. Commands accept a JSON array or a
whitespace-separated string and must name an executable first. Numeric values
are integers or **finite** numbers: `nan`, `inf` and `-inf` are refused because
a non-finite timeout would silently disable the bound it exists to enforce.
`PipelineConfig.as_dict()` never contains the token and credential-redacts the
repository URL, and `.env.example` documents every variable with placeholders
only (no secret, and the irreversible steps default to `false`).

### 22.4 Per-issue lifecycle and gates

For each accepted issue, in order:

1. **T28** rule allowlist - a rule that is not exactly allowlisted is blocked
   before any repository work.
2. **T25** iteration limit - a disallowed attempt is blocked.
3. The agent branch is derived (T07 naming) and **T26** decides whether it may
   be mutated (the default branch is supplied by configuration; `None` refuses).
4. **T05/T06/T07** clone, check out the source branch, create the agent branch.
5. **T08** verified issue context; the **clean baseline is captured before
   Codex** with `include_ignored=True`.
6. **T09/T10/T11** prompt, execution, interpretation.
7. **T12/T13** diff + change scope.
8. **T14/T15** project tests.
9. **T16/T17** analysis trigger + wait (the scanner environment carries the
   Sonar credentials; the client is used only for the CE-task read).
10. **T18** issue verification with the T17 completion (correlation requires the
    declared single-analysis precondition).
11. **T19** final classification (the T16 trigger is carried in).
12. post-run snapshot + attribution;
13. when `FIXED` and `commit_fixes` is enabled: **T27 `PRE_COMMIT`** → **T20**
    commit; when committed and `push_fixes` is enabled: **T27 `PRE_PUSH`** →
    **T21** push;
14. **T23** per-issue report; the run ends with the **T22** overall report.

Defaults are `commit_fixes=False` and `push_fixes=False`, so the pipeline is
read-only unless an operator opts in. `push_fixes` is only ever attempted for a
commit whose result reports `is_committed`.

### 22.5 Failure handling

* A stage error (a `RepositoryError`, `ContextError`, `GitDiffError`,
  `ChangeScopeError`, `TestRunnerError`, `CodexExecutorError`,
  `SonarAnalysisError`, `AnalysisWaitError`, `GitCommitError`, `GitPushError`,
  `WorktreeBaselineError`, …) is caught and recorded as a **blocked attempt**
  with the exception *type* and message; it never crashes the run.
* An unexpected exception is handled the same way (fail closed), never turned
  into a success.
* The stored `blocked_reason` and the `diagnostics` are redacted first: the
  configured literal token is replaced, then T29's credential-shape redaction is
  applied. `diagnostics` additionally preserves the (redacted) traceback so a
  blocked attempt stays debuggable, but it is **excluded from `as_dict()`** - a
  report, a log or a `--json` dump never publishes a traceback.
* A blocked issue contributes **no** lifecycle entry to the T22 report, so the
  aggregate is never inflated by an attempt that did not happen.
* A refusal from T24/T25/T26/T27/T28 produces a typed result with a decisive
  reason; it is never a silent skip.

### 22.6 Logging (T29)

`PolicyLogger.emit(event_code, message, level, fields)` builds a
`logging_policy.LogEvent`, evaluates it and only publishes the **validated,
bounded, redacted** message and fields to a sink. The default policy approves
the pipeline's own event codes and six structured field names; anything else is
dropped. The decided records (emitted *and* dropped) are attached to the run
result for audit, and no credential can reach a sink.

### 22.7 Public surface

`PipelineConfig`, `load_pipeline_config`, `PipelineConfigError`, `FixPipeline`,
`PipelineDependencies`, `build_default_dependencies`, `IssueRunResult`,
`PipelineRunResult`, `PolicyLogger`, `build_default_logging_policy`.

### 22.8 Tests

| Test file | Covers |
|-----------|--------|
| `tests/test_pipeline_config.py` | required/malformed values, JSON and string command parsing, limit defaults, non-finite numeric refusal, the token never being published while driving secret scanning, repository-URL redaction, the work-dir override, and the `.env.example` contract (every variable documented, no secret, irreversible steps off) |
| `tests/test_pipeline_run.py` | T24 refusal, T28/T25/T26 blocks, the FIXED happy path, Codex failure, test failure, scope violation, unavailable SonarQube, commit/push wiring and the "no push without a commit" rule, repository failure as a blocked attempt, redacted diagnostics that are preserved in-process but excluded from every serialized view, blocked issues excluded from the overall report, and no token in the serialized result |
| `tests/pipeline_fakes.py` | non-collected fakes for every injected boundary |

### 22.9 Integration status

* `main.py` keeps its original read-only T01–T04 behaviour with no arguments and
  adds `--run`, `--commit`, `--push` and `--json`. The Windows-style default path
  is byte-for-byte behaviourally unchanged.
* The library modules at the repository root remain free of any dependency on
  T30; the pipeline depends on them, never the reverse.
* Nothing is pushed automatically: T21 runs only with `--push` and only for a
  commit T20 proved.

### 22.10 Acceptance criteria

1. **Given** a configured run and successful stages for one issue, **when**
   `FixPipeline.run` is called, **then** the issue is classified `FIXED`, a
   per-issue report and an overall report are produced, and nothing is written
   unless `commit_fixes`/`push_fixes` are enabled and approved.
2. **Given** any stage error or policy refusal, **then** the attempt is recorded
   as blocked with a reason, the run continues, and no success is inferred.
3. **Given** a non-allowlisted rule, a refused iteration, or an unsafe/default
   branch, **then** no repository work happens for that issue.
4. **Given** `push_fixes` enabled but a commit that is not `is_committed`,
   **then** no push is attempted.
5. **Given** the token, **then** it is never present in any log, report or
   serialized result.
