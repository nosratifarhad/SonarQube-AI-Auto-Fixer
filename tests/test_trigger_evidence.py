"""T16 -> T19 -> T20 analysis-evidence tests (T21.1.a).

The T21.1 contract audit found that T19 dropped the T16 trigger, so T20's
analysis-task cross-check was inert: it read ``getattr(issue_status, "trigger",
None)`` from a result that never carried one, and G11 silently skipped the
comparison. These tests pin the fix end to end:

* T16 projects a compact, immutable :class:`AnalysisTriggerEvidence` record
  (trigger outcome + compute-engine task id) and nothing else;
* T19 carries it on ``IssueStatusResult.trigger_evidence`` on **every** verdict;
* the real T20 executor takes it from the T19 result and refuses - without
  creating a commit - when it is missing, unusable or contradicted.

Every Git interaction happens in a real ``tmp_path`` repository: no network, no
SonarQube server and no commit outside ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from commit_policy import GateStatus
from git_commit import CommitStatus, GitCommitExecutor
from issue_status import IssueFinalStatus, determine_issue_status
from sonar_analysis import (
    AnalysisTriggerEvidence,
    SonarAnalysisStatus,
    SonarAnalysisTriggerResult,
)
from worktree_baseline import WorktreeBaselineInspector, attribute_changes

from t20_fixtures import (
    AGENT_BRANCH,
    TARGET,
    assert_fixed,
    git_run,
    green_status,
    make_analysis,
    make_codex,
    make_trigger,
)

#: A credential-shaped value a verbose scanner could echo into its output.
SECRET = "sonar-token-0123456789abcdef"


# ---------------------------------------------------------------------------
# Fixtures (the shape the T20 integration suite uses)
# ---------------------------------------------------------------------------


@pytest.fixture
def inspector() -> WorktreeBaselineInspector:
    """A baseline inspector that reads ignored files and content identity."""
    return WorktreeBaselineInspector(timeout_seconds=60.0)


@pytest.fixture
def repo(make_remote_repo, rm, clone_destination) -> Path:
    """A real clone on the agent branch with an explicit fixture identity."""
    remote = make_remote_repo()
    work = rm.clone(str(remote), clone_destination())
    git_run(work, "config", "user.name", "Test User")
    git_run(work, "config", "user.email", "test@example.com")
    (work / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (work / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    git_run(work, "add", "--", ".gitignore", ".gitattributes")
    git_run(work, "commit", "-m", "chore: ignore rules")
    git_run(work, "checkout", "-q", "-b", AGENT_BRANCH)
    return work


def run_t20(repo: Path, inspector: WorktreeBaselineInspector, status, **overrides):
    """Run the real T20 executor for one agent edit."""
    baseline = inspector.capture(repo, include_ignored=True)
    (repo / TARGET).write_text("fixed\n", encoding="utf-8")
    after = inspector.capture(repo, include_ignored=True)
    attribution = attribute_changes(baseline, after)
    executor = GitCommitExecutor(timeout_seconds=60.0)
    return executor.commit_safely(
        repository_path=repo,
        issue_status=status,
        baseline=baseline,
        after=after,
        attribution=attribution,
        **overrides,
    )


# ---------------------------------------------------------------------------
# T16 - the compact evidence projection
# ---------------------------------------------------------------------------


class TestT16ProjectsEvidence:
    def test_the_evidence_is_the_minimal_projection(self):
        trigger = make_trigger(task_id="AY1")
        evidence = trigger.evidence()
        assert isinstance(evidence, AnalysisTriggerEvidence)
        assert evidence.triggered is True
        assert evidence.task_id == "AY1"
        assert evidence.has_task_id is True

    def test_the_evidence_never_exposes_the_bulky_or_secret_fields(self):
        trigger = SonarAnalysisTriggerResult(
            status=SonarAnalysisStatus.TRIGGERED,
            exit_code=0,
            stdout=f"analysis queued with token={SECRET}",
            stderr="",
            command=("sonar-scanner", "-Dsonar.token=" + SECRET),
            task_id="AY1",
        )
        evidence = trigger.evidence()
        payload = evidence.as_dict()
        assert set(payload) == {"triggered", "task_id", "has_task_id"}
        for leaked in ("stdout", "stderr", "command", "exit_code"):
            assert not hasattr(evidence, leaked), leaked
        assert SECRET not in json.dumps(payload)

    def test_a_trigger_without_a_task_id_projects_a_usable_negative(self):
        evidence = make_trigger(task_id=None).evidence()
        assert evidence.triggered is True
        assert evidence.task_id is None
        assert evidence.has_task_id is False

    def test_an_unsuccessful_trigger_projects_triggered_false(self):
        evidence = make_trigger(SonarAnalysisStatus.FAILED, task_id="AY1").evidence()
        assert evidence.triggered is False

    def test_the_evidence_is_frozen_and_equatable(self):
        first = AnalysisTriggerEvidence(triggered=True, task_id="AY1")
        second = AnalysisTriggerEvidence(triggered=True, task_id="AY1")
        assert first == second
        assert hash(first) == hash(second)
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.task_id = "AY2"


# ---------------------------------------------------------------------------
# T19 - the evidence is carried, on every verdict
# ---------------------------------------------------------------------------


class TestT19CarriesTheEvidence:
    def test_a_fixed_result_carries_the_t16_evidence(self):
        reference = green_status()
        result = determine_issue_status(
            codex=make_codex(),
            scope=reference.scope,
            tests=reference.tests,
            analysis=make_analysis(task_id="AY1"),
            verification=reference.verification,
            trigger=make_trigger(task_id="AY1"),
        )
        assert result.status is IssueFinalStatus.FIXED
        assert isinstance(result.trigger_evidence, AnalysisTriggerEvidence)
        assert result.trigger_evidence.task_id == "AY1"
        assert result.trigger_evidence.triggered is True

    def test_no_t16_result_leaves_the_evidence_absent(self):
        """T19 still classifies from T17 alone; the field simply stays ``None``."""
        status = green_status(trigger=None)
        assert status.status is IssueFinalStatus.FIXED
        assert status.trigger_evidence is None

    @pytest.mark.parametrize(
        "trigger",
        [
            make_trigger(task_id="AY1"),
            make_trigger(task_id=None),
            make_trigger(SonarAnalysisStatus.FAILED),
            make_trigger(SonarAnalysisStatus.TIMEOUT),
        ],
    )
    def test_every_verdict_carries_the_evidence(self, trigger):
        """The evidence survives a failure verdict too, so T20 can still see it."""
        status = green_status(trigger=trigger)
        assert status.trigger_evidence is not None
        assert status.trigger_evidence.task_id == trigger.task_id



# ---------------------------------------------------------------------------
# T20 - the executor consumes the carried evidence, and fails closed
# ---------------------------------------------------------------------------


class TestT20ConsumesTheEvidence:
    def test_the_executor_commits_using_the_evidence_t19_carried(
        self, repo, inspector
    ):
        status = green_status()
        assert_fixed(status)
        assert status.trigger_evidence is not None
        before = git_run(repo, "rev-parse", "HEAD")
        count_before = git_run(repo, "rev-list", "--count", "HEAD")

        result = run_t20(repo, inspector, status)

        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.is_committed is True
        assert result.gates.status_of("G11") is GateStatus.PASS
        assert result.gates.failed_gates == ()
        assert result.previous_head == before
        assert result.new_head != before
        assert int(git_run(repo, "rev-list", "--count", "HEAD")) == (
            int(count_before) + 1
        )

    def test_a_matching_explicit_override_is_still_accepted(self, repo, inspector):
        """The documented explicit override keeps working (backward compatible)."""
        result = run_t20(
            repo, inspector, green_status(), trigger=make_trigger(task_id="AY1")
        )
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert result.gates.status_of("G11") is GateStatus.PASS

    def test_missing_evidence_fails_closed_and_creates_no_commit(
        self, repo, inspector
    ):
        status = green_status(trigger=None)
        assert status.is_fixed is True  # T19 still reports a fix
        before = git_run(repo, "rev-parse", "HEAD")
        count_before = git_run(repo, "rev-list", "--count", "HEAD")

        result = run_t20(repo, inspector, status)

        assert result.status is CommitStatus.REFUSED, result.reason
        assert result.is_committed is False
        assert result.gates.status_of("G11") is GateStatus.FAIL
        assert "evidence is missing" in result.gates.gate("G11").reason
        assert git_run(repo, "rev-parse", "HEAD") == before
        assert git_run(repo, "rev-list", "--count", "HEAD") == count_before

    def test_a_stripped_t19_result_fails_closed(self, repo, inspector):
        """Proof that T20 reads ``trigger_evidence`` from the T19 result itself."""
        status = dataclasses.replace(green_status(), trigger_evidence=None)
        before = git_run(repo, "rev-parse", "HEAD")
        result = run_t20(repo, inspector, status)
        assert result.status is CommitStatus.REFUSED, result.reason
        assert result.gates.status_of("G11") is GateStatus.FAIL
        assert git_run(repo, "rev-parse", "HEAD") == before

    def test_a_mismatched_task_identity_fails_closed(self, repo, inspector):
        before = git_run(repo, "rev-parse", "HEAD")
        result = run_t20(
            repo, inspector, green_status(), trigger=make_trigger(task_id="AY2")
        )
        assert result.status is CommitStatus.REFUSED, result.reason
        assert result.gates.status_of("G11") is GateStatus.FAIL
        assert "does not match" in result.gates.gate("G11").reason
        assert git_run(repo, "rev-parse", "HEAD") == before

    def test_an_unsuccessful_trigger_override_fails_closed(self, repo, inspector):
        before = git_run(repo, "rev-parse", "HEAD")
        result = run_t20(
            repo,
            inspector,
            green_status(),
            trigger=make_trigger(SonarAnalysisStatus.FAILED, task_id="AY1"),
        )
        assert result.status is CommitStatus.REFUSED, result.reason
        assert result.gates.status_of("G11") is GateStatus.FAIL
        assert git_run(repo, "rev-parse", "HEAD") == before

    def test_a_trigger_without_a_task_id_fails_closed(self, repo, inspector):
        before = git_run(repo, "rev-parse", "HEAD")
        result = run_t20(
            repo, inspector, green_status(), trigger=make_trigger(task_id=None)
        )
        assert result.status is CommitStatus.REFUSED, result.reason
        assert result.gates.status_of("G11") is GateStatus.FAIL
        assert git_run(repo, "rev-parse", "HEAD") == before

    def test_a_secret_in_the_trigger_output_reaches_no_result(self, repo, inspector):
        trigger = SonarAnalysisTriggerResult(
            status=SonarAnalysisStatus.TRIGGERED,
            exit_code=0,
            stdout=f"queued token={SECRET}",
            stderr="",
            command=("sonar-scanner",),
            task_id="AY1",
        )
        result = run_t20(repo, inspector, green_status(), trigger=trigger)
        assert result.status is CommitStatus.COMMITTED, result.reason
        assert SECRET not in json.dumps(result.as_dict())

    def test_the_result_summary_exposes_the_evidence_without_secrets(self):
        status = green_status()
        payload = status.as_dict()
        assert payload["trigger_evidence"] == {
            "triggered": True,
            "task_id": "AY1",
            "has_task_id": True,
        }
        assert json.loads(json.dumps(payload))["trigger_evidence"]["task_id"] == "AY1"

    def test_the_field_can_still_be_absent_on_a_hand_built_result(self):
        """A result built without the field stays constructible (backward safe)."""
        status = green_status()
        replaced = dataclasses.replace(status, trigger_evidence=None)
        assert replaced.trigger_evidence is None
        assert replaced.is_fixed is True
