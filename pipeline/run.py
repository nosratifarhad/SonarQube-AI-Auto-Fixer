"""End-to-end orchestration of the SonarQube AI Auto-Fixer (T30).

Every stage of the pipeline already exists as an independently tested unit
(T01-T29). This module is the **only** place that wires them together for a
real run, in the documented order:

    SonarQube -> issue selection -> clone/branch -> issue context
        -> Codex prompt -> Codex execution -> result analysis
        -> working-tree diff -> change scope -> project tests
        -> analysis trigger -> analysis wait -> issue verification
        -> final status -> (branch/uncertainty policy) -> commit -> push
        -> per-issue report -> overall report

Design rules
------------
* **No stage is re-implemented here.** The pipeline only adapts inputs and
  outputs of the existing modules; every policy decision stays in its module.
* **Fail closed.** A missing/ambiguous/malformed result from any stage is
  recorded as a blocked attempt, never turned into a success. An unexpected
  error is caught, named by type and recorded - it never crashes the whole run.
* **Irreversible steps are opt-in and gated.** A FIXED attempt is committed
  only when ``commit_fixes`` is enabled and T26 (branch protection) plus T27
  (uncertainty, ``PRE_COMMIT``) approve; it is pushed only when ``push_fixes``
  is enabled and T27 approves again (``POST_COMMIT`` + ``PRE_PUSH``). Defaults
  are ``False``, so the pipeline is read-only unless an operator says otherwise.
* **Every I/O boundary is injectable** (:class:`PipelineDependencies`), so the
  whole pipeline can be exercised without Git, SonarQube, Codex or a project
  toolchain.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from analysis_correlation import AnalysisCorrelation
from branch_naming import AGENT_BRANCH_PREFIX, make_agent_branch_name, utc_compact_timestamp
from branch_protection import (
    BranchProtectionInput,
    ProtectedBranchPolicy,
    evaluate_branch_protection,
)
from change_scope import ChangeScopeValidator
from codex_executor import CodexExecutor
from codex_prompt import build_codex_prompt
from codex_result import analyze_codex_result
from commit_policy import CommitPolicyConfig
from context import IssueContextPreparer
from git_commit import GitCommitExecutor
from git_diff import GitDiffInspector
from git_push import GitPushExecutor
from issue_limit import (
    IssueLimitPolicy,
    IssueSelection,
    evaluate_issue_limit,
)
from issue_status import determine_issue_status
from iteration_limit import (
    IterationAttempt,
    IterationLimitPolicy,
    evaluate_iteration_limit,
)
from models import SonarIssue
from overall_report import IssueLifecycleInput, build_overall_report
from per_issue_report import PerIssueInput, build_per_issue_report
from push_policy import PushPolicyConfig
from repository import RepositoryManager
from sonar_analysis import (
    AnalysisConfig,
    SonarAnalysisTrigger,
    build_sonar_environment,
)
from sonar_analysis_waiter import SonarAnalysisWaiter
from sonar_issue_verification import SonarIssueVerifier
from sonar_rule_allowlist import (
    RuleAllowlistInput,
    RuleAllowlistPolicy,
    evaluate_rule_allowlist,
)
from test_result import analyze_test_result
from test_runner import ProjectTestRunner
from uncertainty_policy import (
    EvidenceDimension,
    EvidenceItem,
    EvidenceState,
    MutationPhase,
    UncertaintyInput,
    evaluate_uncertainty,
)
from worktree_baseline import (
    WorktreeBaselineInspector,
    attribute_changes,
)

from pipeline.config import PipelineConfig
from pipeline.logging import (
    EVENT_COMMIT_RESULT,
    EVENT_ISSUE_ATTEMPTED,
    EVENT_ISSUE_SKIPPED,
    EVENT_ISSUE_STATUS,
    EVENT_PUSH_RESULT,
    EVENT_RUN_START,
    EVENT_RUN_SUMMARY,
    FIELD_DETAIL,
    FIELD_ISSUE_KEY,
    FIELD_RULE,
    FIELD_STAGE,
    FIELD_STATUS,
    PolicyLogger,
)
from logging_policy import sanitize_log_value


@dataclass
class PipelineDependencies:
    """The injectable I/O boundary of the pipeline.

    Every attribute is an object exposing the methods the pipeline calls; the
    default factory (:func:`build_default_dependencies`) supplies the real
    modules, while tests supply fakes. Duck typing is intentional: the
    pipeline validates the *results*, not the concrete classes.
    """

    repository: object
    context_preparer: object
    codex: object
    diff_inspector: object
    scope_validator: object
    test_runner: object
    analysis_trigger: object
    analysis_waiter: object
    verifier: object
    baseline_inspector: object
    commit_executor: object
    push_executor: object


def _analysis_config(config: PipelineConfig) -> AnalysisConfig:
    """Build the T16 analysis configuration from the pipeline configuration."""
    environment = build_sonar_environment(config.sonar_url, config.sonar_token)
    return AnalysisConfig(
        command=tuple(config.analysis_command),
        timeout_seconds=config.analysis_timeout_seconds,
        environment=environment,
        secrets=config.forbidden_secrets,
    )


def build_default_dependencies(
    client: object, config: PipelineConfig
) -> PipelineDependencies:
    """Build the production dependency bundle for a real run."""
    repository = RepositoryManager()
    commit_config = CommitPolicyConfig(
        protected_branches=tuple(config.protected_branches),
        default_branch=config.default_branch,
        required_branch_prefix=AGENT_BRANCH_PREFIX,
    )
    push_config = PushPolicyConfig(
        protected_branches=tuple(config.protected_branches),
        default_branch=config.default_branch,
        required_branch_prefix=AGENT_BRANCH_PREFIX,
    )
    return PipelineDependencies(
        repository=repository,
        context_preparer=IssueContextPreparer(repository),
        codex=CodexExecutor(
            codex_command=config.codex_command,
            codex_args=config.codex_args,
            timeout_seconds=config.codex_timeout_seconds,
        ),
        diff_inspector=GitDiffInspector(),
        scope_validator=ChangeScopeValidator(),
        test_runner=ProjectTestRunner(timeout_seconds=config.test_timeout_seconds),
        analysis_trigger=SonarAnalysisTrigger(_analysis_config(config)),
        analysis_waiter=SonarAnalysisWaiter(
            client,
            timeout_seconds=config.analysis_wait_timeout_seconds,
            poll_interval_seconds=config.analysis_poll_interval_seconds,
        ),
        verifier=SonarIssueVerifier(
            client,
            project_key=config.project_key,
            exclusive_analysis=config.exclusive_analysis,
        ),
        baseline_inspector=WorktreeBaselineInspector(),
        commit_executor=GitCommitExecutor(config=commit_config),
        push_executor=GitPushExecutor(config=push_config),
    )


@dataclass(frozen=True)
class IssueRunResult:
    """What happened to one issue during the run (typed, secret-free).

    ``diagnostics`` carries redacted internal detail (the exception type and a
    redacted traceback) for an in-process caller that needs to debug a blocked
    attempt. It is deliberately **excluded from** :meth:`as_dict`, so a report,
    a log or a ``--json`` dump never publishes a traceback.
    """

    issue_key: str
    issue: Optional[SonarIssue]
    fix_attempted: bool
    issue_status: Optional[object] = None
    commit_result: Optional[object] = None
    push_result: Optional[object] = None
    per_issue_report: Optional[object] = None
    branch_protection: Optional[object] = None
    uncertainty: Optional[object] = None
    blocked_stage: Optional[str] = None
    blocked_reason: Optional[str] = None
    diagnostics: Tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        """True when the attempt never produced a T19 classification."""
        return self.issue_status is None

    def as_dict(self) -> dict:
        def view(value: object) -> object:
            as_dict = getattr(value, "as_dict", None)
            return as_dict() if callable(as_dict) else None

        return {
            "issue_key": self.issue_key,
            "fix_attempted": self.fix_attempted,
            "blocked_stage": self.blocked_stage,
            "blocked_reason": self.blocked_reason,
            "issue_status": view(self.issue_status),
            "commit_result": view(self.commit_result),
            "push_result": view(self.push_result),
            "branch_protection": view(self.branch_protection),
            "uncertainty": view(self.uncertainty),
            "per_issue_report": view(self.per_issue_report),
        }


@dataclass(frozen=True)
class PipelineRunResult:
    """The outcome of one orchestrated run."""

    overall_report: object
    issue_results: Tuple[IssueRunResult, ...]
    issue_limit: object
    logs: Tuple[object, ...]

    def as_dict(self) -> dict:
        return {
            "overall_report": self.overall_report.as_dict(),
            "issue_results": [result.as_dict() for result in self.issue_results],
            "issue_limit": self.issue_limit.as_dict()
            if hasattr(self.issue_limit, "as_dict")
            else None,
            "logs": [
                log.as_dict() if hasattr(log, "as_dict") else log
                for log in self.logs
            ],
        }


class FixPipeline:
    """Run the complete fix lifecycle for a selection of SonarQube issues.

    Args:
        client: the SonarQube client (T02) used by the T17 waiter and T18
            verifier.
        config: the validated :class:`~pipeline.config.PipelineConfig`.
        dependencies: optional dependency bundle (defaults to the real
            modules). Tests inject fakes here.
        logger: optional :class:`~pipeline.logging.PolicyLogger`; a default
            one is created when omitted.
        clock: injectable monotonic clock (used only for unique work
            directories). Never used for a policy decision.
    """

    def __init__(
        self,
        *,
        client: object,
        config: PipelineConfig,
        dependencies: Optional[PipelineDependencies] = None,
        logger: Optional[PolicyLogger] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config is None:
            raise ValueError("A PipelineConfig is required.")
        self._client = client
        self._config = config
        self._deps = dependencies or build_default_dependencies(client, config)
        self._logger = logger or PolicyLogger()
        self._clock = clock

    @property
    def config(self) -> PipelineConfig:
        return self._config

    @property
    def logger(self) -> PolicyLogger:
        return self._logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        issues: Sequence[SonarIssue],
        *,
        discovered_issue_count: Optional[int] = None,
    ) -> PipelineRunResult:
        """Process ``issues`` (bounded by T24) and return the run result.

        Args:
            issues: the T03/T04-selected issues, in the caller's order.
            discovered_issue_count: how many issues SonarQube reported in
                total. Defaults to ``len(issues)``; it is only the bound the
                selection may not exceed (T24).

        Returns:
            A :class:`PipelineRunResult` with the overall report (T22), the
            per-issue results (each carrying its T23 report when available),
            the T24 limit evaluation and the emitted/dropped log records.
        """
        selected = list(issues)
        discovered = (
            discovered_issue_count
            if discovered_issue_count is not None
            else len(selected)
        )
        self._log(
            EVENT_RUN_START,
            f"Run started: {len(selected)} selected issue(s) of {discovered}.",
            fields=((FIELD_DETAIL, f"selected={len(selected)}"),),
        )

        limit = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=self._config.max_issues),
            selection=IssueSelection(
                discovered_issue_count=discovered,
                selected_issue_keys=tuple(issue.key for issue in selected),
            ),
        )

        if not limit.is_allowed:
            self._log(
                EVENT_RUN_START,
                "The issue limit policy refused the selection.",
                fields=((FIELD_STATUS, limit.status.value),),
            )
            results: Tuple[IssueRunResult, ...] = ()
        else:
            accepted = set(limit.accepted_issue_keys)
            ordered = [issue for issue in selected if issue.key in accepted]
            results = tuple(
                self._run_issue(issue, index)
                for index, issue in enumerate(ordered)
            )

        entries = [
            IssueLifecycleInput(
                issue_key=result.issue_key,
                issue_status=result.issue_status,
                commit_result=result.commit_result,
                push_result=result.push_result,
            )
            for result in results
            if result.issue_status is not None
        ]
        overall = build_overall_report(
            entries=entries,
            forbidden_secrets=self._config.forbidden_secrets,
        )
        self._log(
            EVENT_RUN_SUMMARY,
            f"Run finished with overall status {overall.status.value}.",
            fields=(
                (FIELD_STATUS, overall.status.value),
                (FIELD_DETAIL, f"issues={overall.total_issues}"),
            ),
        )
        return PipelineRunResult(
            overall_report=overall,
            issue_results=results,
            issue_limit=limit,
            logs=self._logger.records,
        )

    # ------------------------------------------------------------------
    # Per-issue pipeline
    # ------------------------------------------------------------------

    def _run_issue(self, issue: SonarIssue, index: int) -> IssueRunResult:
        work_root: Optional[Path] = None
        try:
            # T28 - may this rule be fixed automatically at all?
            allow = evaluate_rule_allowlist(
                policy=RuleAllowlistPolicy(self._config.allowed_rules),
                rule_input=RuleAllowlistInput(rule_id=issue.rule),
            )
            if not allow.can_auto_fix:
                return self._blocked(
                    issue,
                    "rule_allowlist",
                    allow.reason,
                    fix_attempted=False,
                )

            # T25 - may this attempt run?
            iteration = evaluate_iteration_limit(
                policy=IterationLimitPolicy(self._config.max_iterations),
                attempt=IterationAttempt(current_iteration=1),
            )
            if not iteration.is_allowed:
                return self._blocked(
                    issue, "iteration_limit", iteration.reason, fix_attempted=False
                )

            agent_branch = make_agent_branch_name(
                issue.key,
                self._config.branch_timestamp or utc_compact_timestamp(),
            )

            # T26 - may this (existing/soon-to-exist) branch be mutated?
            protection = evaluate_branch_protection(
                policy=ProtectedBranchPolicy(
                    protected_branches=tuple(self._config.protected_branches),
                    require_ai_fix_branch_prefix=self._config.require_ai_fix_branch_prefix,
                ),
                branch=BranchProtectionInput(
                    candidate_branch=agent_branch,
                    default_branch=self._config.default_branch,
                ),
            )
            if not protection.is_allowed:
                return self._blocked(
                    issue,
                    "branch_protection",
                    protection.reason,
                    fix_attempted=False,
                    branch_protection=protection,
                )

            # T05/T06/T07 - clone, check out the source branch, create the
            # isolated agent branch.
            work_root = self._prepare_work_directory(index)
            repository_path = self._deps.repository.clone(
                self._config.repository_url, work_root
            )
            self._deps.repository.checkout_source_branch(
                repository_path, self._config.source_branch
            )
            self._deps.repository.create_agent_branch(
                repository_path, agent_branch, self._config.source_branch
            )

            # T08 - verified issue context.
            context = self._deps.context_preparer.prepare(
                issue=issue,
                repository_path=repository_path,
                source_branch=self._config.source_branch,
                agent_branch=agent_branch,
            )

            # Pre-T20 - capture the clean baseline before anything can change.
            baseline = self._deps.baseline_inspector.capture(
                repository_path, include_ignored=True
            )

            self._log(
                EVENT_ISSUE_ATTEMPTED,
                f"Attempting issue {issue.key} on branch {agent_branch}.",
                fields=(
                    (FIELD_ISSUE_KEY, issue.key),
                    (FIELD_RULE, issue.rule),
                ),
            )

            # T09/T10/T11 - prompt, execution, interpretation.
            prompt = build_codex_prompt(context)
            codex_result = self._deps.codex.execute(prompt, repository_path)
            codex = analyze_codex_result(codex_result)

            # T12/T13 - diff + change scope.
            diff_result = self._deps.diff_inspector.inspect(repository_path)
            scope = self._deps.scope_validator.validate(context, diff_result)

            # T14/T15 - project tests.
            test_result = self._deps.test_runner.run(
                repository_path, self._config.test_command
            )
            tests = analyze_test_result(test_result)

            # T16/T17 - trigger and wait for the SonarQube analysis.
            trigger = self._deps.analysis_trigger.trigger(
                repository_path, self._analysis_config()
            )
            completion = self._deps.analysis_waiter.wait(trigger.task_id)

            # T18 - verify the original issue against the new analysis.
            verification = self._deps.verifier.verify(
                issue, completion=completion
            )

            # T19 - classify the attempt.
            status = determine_issue_status(
                codex=codex,
                scope=scope,
                tests=tests,
                analysis=completion,
                verification=verification,
                trigger=trigger,
            )

            # Post-run baseline for T20 attribution.
            after = self._deps.baseline_inspector.capture(
                repository_path, include_ignored=True
            )
            attribution = attribute_changes(baseline, after)

            commit_result = None
            push_result = None
            uncertainty = None
            if status.is_fixed and self._config.commit_fixes:
                base_items = self._pre_commit_evidence(
                    status=status,
                    codex=codex,
                    scope=scope,
                    tests=tests,
                    completion=completion,
                    verification=verification,
                    protection=protection,
                )
                uncertainty = evaluate_uncertainty(
                    uncertainty_input=UncertaintyInput(
                        phase=MutationPhase.PRE_COMMIT, evidence=base_items
                    )
                )
                if uncertainty.is_allowed:
                    commit_result = self._deps.commit_executor.commit_safely(
                        repository_path=repository_path,
                        issue_status=status,
                        baseline=baseline,
                        after=after,
                        attribution=attribution,
                        secrets=self._config.forbidden_secrets,
                    )
                    self._log(
                        EVENT_COMMIT_RESULT,
                        f"Issue {issue.key} commit status "
                        f"{commit_result.status.value}.",
                        fields=(
                            (FIELD_ISSUE_KEY, issue.key),
                            (FIELD_STATUS, commit_result.status.value),
                        ),
                    )
                    if (
                        getattr(commit_result, "is_committed", False)
                        and self._config.push_fixes
                    ):
                        push_uncertainty = self._push_uncertainty(
                            base_items=base_items,
                            agent_branch=agent_branch,
                            commit_result=commit_result,
                        )
                        if push_uncertainty.is_allowed:
                            push_result = self._deps.push_executor.push_safely(
                                repository_path=repository_path,
                                commit_result=commit_result,
                                remote=self._config.remote_name,
                                remote_branch=self._config.destination_branch
                                or agent_branch,
                            )
                            self._log(
                                EVENT_PUSH_RESULT,
                                f"Issue {issue.key} push status "
                                f"{push_result.status.value}.",
                                fields=(
                                    (FIELD_ISSUE_KEY, issue.key),
                                    (FIELD_STATUS, push_result.status.value),
                                ),
                            )

            report = build_per_issue_report(
                entry=PerIssueInput(
                    issue_key=issue.key,
                    issue=issue,
                    issue_status=status,
                    commit_result=commit_result,
                    push_result=push_result,
                ),
                forbidden_secrets=self._config.forbidden_secrets,
            )
            self._log(
                EVENT_ISSUE_STATUS,
                f"Issue {issue.key} classified as {status.status.value}.",
                fields=(
                    (FIELD_ISSUE_KEY, issue.key),
                    (FIELD_STATUS, status.status.value),
                    (FIELD_STAGE, status.decisive_stage),
                ),
            )
            return IssueRunResult(
                issue_key=issue.key,
                issue=issue,
                fix_attempted=True,
                issue_status=status,
                commit_result=commit_result,
                push_result=push_result,
                per_issue_report=report,
                branch_protection=protection,
                uncertainty=uncertainty,
            )
        except Exception as exc:  # fail closed: an attempt error is a blocked attempt
            error_type = type(exc).__name__
            self._log(
                EVENT_ISSUE_SKIPPED,
                f"Issue {issue.key} could not be processed.",
                fields=(
                    (FIELD_ISSUE_KEY, issue.key),
                    (FIELD_DETAIL, error_type),
                ),
            )
            return self._blocked(
                issue,
                "pipeline_error",
                f"{error_type}: {exc}",
                fix_attempted=False,
                diagnostics=(traceback.format_exc(),),
            )
        finally:
            self._cleanup_work_directory(work_root)

    # ------------------------------------------------------------------
    # Policy adapters
    # ------------------------------------------------------------------

    def _analysis_config(self) -> AnalysisConfig:
        return _analysis_config(self._config)

    def _pre_commit_evidence(
        self,
        *,
        status: object,
        codex: object,
        scope: object,
        tests: object,
        completion: object,
        verification: object,
        protection: object,
    ) -> Tuple[EvidenceItem, ...]:
        """T27 ``PRE_COMMIT`` evidence derived from the T10-T19/T26 outcomes.

        The mapping is documentation-driven (``uncertainty_policy`` §19.3): a
        known negative becomes ``NEGATIVE`` while a doubt becomes ``UNKNOWN``.
        The pipeline never passes a value T27 would have to interpret as
        confirmed unless the stage actually confirmed it.
        """
        correlation = getattr(verification, "correlation", None)
        return (
            self._evidence(
                EvidenceDimension.ISSUE_OUTCOME,
                bool(getattr(status, "is_fixed", False)),
            ),
            self._evidence(
                EvidenceDimension.CODEX_EXECUTION,
                bool(getattr(codex, "execution_succeeded", False)),
            ),
            self._evidence(
                EvidenceDimension.CHANGE_SCOPE,
                bool(getattr(scope, "is_valid", False)),
            ),
            self._evidence(
                EvidenceDimension.TESTS,
                bool(getattr(tests, "passed", False)),
            ),
            self._evidence(
                EvidenceDimension.SONAR_ANALYSIS,
                bool(getattr(completion, "succeeded", False)),
                negative_state=EvidenceState.UNKNOWN,
            ),
            self._evidence(
                EvidenceDimension.ISSUE_VERIFICATION,
                bool(getattr(verification, "reliable_absence", False)),
                negative_state=EvidenceState.UNKNOWN,
            ),
            self._evidence(
                EvidenceDimension.ANALYSIS_CORRELATION,
                correlation is AnalysisCorrelation.CORRELATED,
                negative_state=EvidenceState.UNKNOWN,
            ),
            self._evidence(
                EvidenceDimension.CROSS_STAGE_CONSISTENCY,
                bool(getattr(scope, "expected_file_modified", False)),
                negative_state=EvidenceState.CONTRADICTORY,
            ),
            self._evidence(
                EvidenceDimension.BRANCH_PROTECTION,
                bool(getattr(protection, "is_allowed", False)),
            ),
        )

    def _push_uncertainty(
        self,
        *,
        base_items: Sequence[EvidenceItem],
        agent_branch: str,
        commit_result: object,
    ):
        """T27 ``PRE_PUSH`` evidence: the pre-commit set plus the commit and
        branch-consistency facts a verified T20 commit must prove."""
        committed = bool(getattr(commit_result, "is_committed", False))
        repository = getattr(commit_result, "repository", None)
        branch = getattr(repository, "branch", None)
        branch_consistent = bool(committed and branch == agent_branch)
        items = tuple(base_items) + (
            self._evidence(EvidenceDimension.COMMIT_RESULT, committed),
            self._evidence(EvidenceDimension.COMMIT_IDENTITY, committed),
            self._evidence(EvidenceDimension.COMMIT_REPOSITORY, committed),
            self._evidence(
                EvidenceDimension.BRANCH_CONSISTENCY,
                branch_consistent,
                negative_state=EvidenceState.CONTRADICTORY,
            ),
        )
        return evaluate_uncertainty(
            uncertainty_input=UncertaintyInput(
                phase=MutationPhase.PRE_PUSH, evidence=items
            )
        )

    @staticmethod
    def _evidence(
        dimension: EvidenceDimension,
        confirmed: bool,
        *,
        negative_state: EvidenceState = EvidenceState.NEGATIVE,
    ) -> EvidenceItem:
        return EvidenceItem(
            dimension=dimension,
            state=EvidenceState.CONFIRMED if confirmed else negative_state,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prepare_work_directory(self, index: int) -> Path:
        base = Path(self._config.work_dir).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return Path(
            tempfile.mkdtemp(prefix=f"sonar-fix-{index}-", dir=str(base))
        )

    def _cleanup_work_directory(self, work_root: Optional[Path]) -> None:
        """Remove a temporary clone **only** when it is inside the work base.

        The guard is deliberate: a cleanup must never be able to delete a
        directory the pipeline did not create (for example the work base itself
        or a path outside it), so the resolved target must be strictly inside
        the resolved ``work_dir``. Anything else is left untouched.
        """
        if work_root is None or self._config.keep_work_dir:
            return
        try:
            base = Path(self._config.work_dir).expanduser().resolve()
            target = Path(work_root).resolve()
            if target == base or base not in target.parents:
                return
            shutil.rmtree(target, ignore_errors=True)
        except OSError:  # pragma: no cover - defensive
            pass

    def _blocked(
        self,
        issue: SonarIssue,
        stage: str,
        reason: str,
        *,
        fix_attempted: bool,
        branch_protection: Optional[object] = None,
        diagnostics: Sequence[str] = (),
    ) -> IssueRunResult:
        self._log(
            EVENT_ISSUE_SKIPPED,
            f"Issue {issue.key} blocked at {stage}.",
            fields=(
                (FIELD_ISSUE_KEY, issue.key),
                (FIELD_STAGE, stage),
            ),
        )
        return IssueRunResult(
            issue_key=issue.key,
            issue=issue,
            fix_attempted=fix_attempted,
            branch_protection=branch_protection,
            blocked_stage=stage,
            blocked_reason=self._sanitize_text(reason),
            diagnostics=tuple(
                self._sanitize_text(item) for item in diagnostics
            ),
        )

    def _sanitize_text(self, value: object) -> str:
        """Redact a diagnostic string before it is stored or published.

        Two layers, applied in order:

        1. the configured literal secrets (the SonarQube token) are replaced, so
           even a token that is not credential-*shaped* cannot survive if a
           third-party exception echoes it;
        2. T29's :func:`logging_policy.sanitize_log_value` replaces every
           credential-shaped span (URL userinfo, prefixed tokens, long runs) with
           the single ``[REDACTED]`` marker.

        The helper performs no validation and no bounding - it only redacts.
        """
        text = str(value)
        for secret in self._config.forbidden_secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        safe, _redacted = sanitize_log_value(text)
        return safe

    def _log(
        self,
        event_code: str,
        message: str,
        *,
        fields: Sequence[Tuple[str, object]] = (),
    ) -> None:
        self._logger.emit(event_code, message, fields=fields)


__all__ = (
    "FixPipeline",
    "IssueRunResult",
    "PipelineDependencies",
    "PipelineRunResult",
    "build_default_dependencies",
)
