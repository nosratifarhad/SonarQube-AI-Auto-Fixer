# SonarQube AI Auto-Fixer — Domain & Behaviour Specification (T04–T19)

Status: **POC implementation complete (T01–T19), pre-T20 safety hardening
applied** · T20+ deliberately not implemented · No real SonarQube credentials or
pushes are ever made by this tool · Tests never invoke a real Codex CLI, never
need a real project toolchain, and never need a live SonarQube server.

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

T20+ (commits, pushes, PRs, retry/iteration loops, limits, reporting,
container/CI wiring) is **out of scope** for this document and for the codebase.
T16–T19 deliberately stop at *verifying and classifying* the attempt: nothing is
committed, nothing is pushed, nothing is retried, no SonarQube issue is resolved
or closed, and no source code is modified by these stages.

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
| T20+ | Commits, pushes, PRs, reporting, limits, retry loops, protection, CI | — | ✖ not implemented |

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
`reasons`, plus the five inputs it used (`codex`, `scope`, `tests`, `analysis`,
`verification`). Flags: `is_fixed`, `needs_review`, `blocking_reason`
(`None` only for `FIXED`), `reason_text`; `as_dict()` nests the (already
sanitized) sub-summaries.

`determine_issue_status(codex=…, scope=…, tests=…, analysis=…, verification=…)`
is a pure, deterministic function of those five typed results — no HTTP, no
subprocess, no files, no retry, no Codex re-invocation, no commit, no push, and
no branch change.

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
successfully (with a matching T16 task id when that evidence is supplied), the
issue's own file changed, the snapshot is **correlated** (`CORRELATED`), and the
original issue is reliably absent from the new SonarQube result.
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
   SonarQube analysis completed successfully (with a matching T16 task id when
   supplied), the analysis is `CORRELATED`, the issue's own file changed, and
   the original issue is reliably absent from the new SonarQube result.
5. **Stop.** T19 only classifies the current attempt: no retry, no second
   attempt, no prompt change, no new branch, no commit, no push.

> **Successful analysis completion does not imply that the original SonarQube
> issue is fixed.**
>
> **The issue is `FIXED` only when the original issue is reliably verified as
> absent from a *correlated* analysis and all required preconditions have
> passed.**

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
| `tests/test_issue_status.py` | T19 every stage and combination, precedence, T16→T17→T19 matrix, correlation gate, `FIXED` acceptance, `REVIEW_REQUIRED` cases, determinism |
| `tests/test_worktree_baseline.py` | pre-T20 baseline capture (staged/unstaged/untracked/deleted/renamed/ignored), read-only guarantees, attribution of pre-existing vs agent changes, failure modes |
| `tests/test_secret_scan.py` | pre-T20 secret scan: configured secrets (added/removed/context), URL userinfo, false-positive-safe URLs, fail-closed input handling, no secret leakage |

Run: `.venv\Scripts\python -m pytest -q --cov=. --cov-report=term-missing`

## 10. Non-goals (unchanged for T20+)

* No commit/push/PR creation (T20–T21), no reporting (T22–T23), no
  retry/iteration loops or limits (T24–T25), no branch/rule protection
  (T26–T28), no production logging, container, or GitLab CI wiring (T29–T32).
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
* `main.py` deliberately remains unwired: T05–T19 are validated as units with
  injectable boundaries, and full pipeline orchestration (including how the
  scanner command, test command, and branch names are configured for a real
  project) is not part of this scope.

## 11. Pre-T20 safety prerequisites (documented, not implemented)

T20 (Git commit) is **not implemented**. The primitives below exist only so that
T20 can be written safely later; they never commit, push, stage, reset, clean or
delete anything.

A future T20 must refuse to commit unless **all** of the following hold:

1. `IssueFinalStatus == FIXED` (`IssueStatusResult.is_fixed is True`).
2. No unresolved review condition: `needs_review is False` and
   `blocking_reason is None`; in particular the analysis must be
   `CORRELATED` and the absence `reliable_absence`-grade.
3. The Codex uncertainty gate is satisfied: `CodexResultAnalysis
   .output_suggests_uncertainty is False` (the marker scan is advisory for T19
   but must be a hard gate for a commit; the final gate itself is T27).
4. Clean baseline attribution: `worktree_baseline.BaselineAttribution
   .attributable is True` (the pre-Codex working tree was clean and no
   pre-existing change was touched).
5. The staged set is exactly the allowed changed-file set - built from
   `attribution.agent_files` (never from a whole-tree `changed_files` list) and
   intersected with the T13 allowed scope, which must equal the issue's target
   file.
6. Secret scan passed: `secret_scan.SecretScanner(secrets).scan_diff(...)`
   returns `ok is True` for the exact content being committed, including the
   SonarQube token(s) from `AnalysisConfig.secrets` and URL-userinfo shapes.
7. No change outside the intended scope: `ChangeScopeResult.is_valid is True`
   with `unexpected_files == ()`.

Any condition that cannot be evaluated must fail closed (no commit), and no
commit may be produced by a component other than T20.


