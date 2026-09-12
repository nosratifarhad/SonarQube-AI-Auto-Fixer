# SonarQube AI Auto-Fixer — Domain & Behaviour Specification (T04–T21)

Status: **POC implementation complete (T01–T21)** · T20 (safe, fail-closed,
exactly-one-commit Git commit) and T21 (safe, fail-closed, exactly-one-push Git
push of a T20 `COMMITTED` result) are implemented; T22+ deliberately not
implemented · No real SonarQube credentials and no real push are ever made by
this tool (every T21 push targets a bare repository under `tmp_path`) · Tests
never invoke a real Codex CLI, never need a real project toolchain, never need a
live SonarQube server, and never touch the real repository (every T20/T21 test
uses a real repository under `tmp_path`).

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
| T20 | Safe, fail-closed, exactly-one-commit Git commit of a verified fix | `commit_message.py`, `commit_policy.py`, `git_commit.py` | ✔ |
| T21 | Safe, fail-closed, exactly-one-push Git push of a T20 `COMMITTED` result | `push_policy.py`, `git_push.py` | ✔ |
| T22+ | PRs, reporting, limits, retry loops, protection, CI | — | ✖ not implemented |

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

Run: `.venv\Scripts\python -m pytest -q --cov=. --cov-report=term-missing`

## 10. Non-goals (unchanged for T22+)

* No PR creation (T22+), no reporting beyond the T22 structured overall report
  (§13) and no T23 per-issue reporting,
  retry/iteration loops or limits (T24–T25), no branch/rule protection
  (T26–T28), no production logging, container, or GitLab CI wiring (T29–T32).
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
* `main.py` deliberately remains unwired: T05–T22 are validated as units with
  injectable boundaries, and full pipeline orchestration (including how the
  scanner command, test command, branch names and push remote are configured for
  a real project) is not part of this scope. T21 in particular is **not** called
  by any production path yet: nothing pushes automatically, and T22 is a library
  with no caller either (it consumes results, it never produces them).

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
* It does **not** implement T23 (per-issue reporting), T24–T25 (limits), T26–T28
  (protections), T29–T32 (logging/containers/CI) and it introduces no
  `RunContext`.

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
* It does **not** implement T24–T25 (limits), T26–T28 (protections), T29–T32
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
  orchestrator exists, `RunContext` remains deferred (F2), and T24+ is untouched.
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

