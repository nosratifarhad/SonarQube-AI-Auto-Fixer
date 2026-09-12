"""T25 unit tests: the pure max-iteration-limit policy (no I/O, no retries).

This file pins the whole contract of ``iteration_limit``:

* configuration validation - ``None``/"anything unusable" refuses instead of
  meaning "unlimited", a ``bool`` is never read as an integer, ``0`` is valid and
  strict, and there is no accepted bound above ``MAX_ITERATIONS``;
* the exact semantics ``1 <= current_iteration <= max_iterations`` with no
  off-by-one, and the truth table of ``attempt_allowed`` /
  ``next_iteration_allowed`` / ``limit_reached`` / ``remaining_iterations``;
* the retry guarantees: a spent limit stays spent, a failure, a review, a test
  failure, an analysis failure, a Codex failure or a *success* can never reset,
  extend or bypass it, and no outcome can even be expressed to the policy;
* determinism, immutability, no mutation of the caller's records and canonical
  serialization;
* the module surface: standard library only and no execution primitive.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from dataclasses import FrozenInstanceError

import pytest

import iteration_limit as module
from git_commit import CommitStatus
from git_push import PushStatus
from issue_status import IssueFinalStatus
from iteration_limit import (
    ALLOWED_STATUSES,
    DEFAULT_MAX_ITERATIONS,
    MAX_ITERATIONS,
    POLICY_VERSION,
    PRECEDENCE,
    IterationAttempt,
    IterationLimitDecision,
    IterationLimitEvaluation,
    IterationLimitPolicy,
    IterationLimitStatus,
    evaluate_iteration_limit,
)

#: Every outcome value the lifecycle stages can produce. None of them is an input
#: of T25: they exist here only so the tests can prove they carry no power.
RETRY_OUTCOMES = (
    tuple(status.value for status in IssueFinalStatus)
    + tuple(status.value for status in CommitStatus)
    + tuple(status.value for status in PushStatus)
)


def evaluate(
    max_iterations=2,
    current_iteration=1,
) -> IterationLimitEvaluation:
    """Evaluate one ``(max_iterations, current_iteration)`` pair."""
    return evaluate_iteration_limit(
        policy=IterationLimitPolicy(max_iterations=max_iterations),
        attempt=IterationAttempt(current_iteration=current_iteration),
    )


def imported_modules(source: str) -> set:
    """The top-level modules ``source`` imports (both import forms)."""
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported


#: One input pair that produces each status, so every status is reachable.
STATUS_INPUTS = {
    IterationLimitStatus.INVALID_CONFIG: (None, 1),
    IterationLimitStatus.INVALID_ITERATION: (3, 0),
    IterationLimitStatus.EXCEEDED: (2, 3),
    IterationLimitStatus.WITHIN_LIMIT: (3, 1),
    IterationLimitStatus.AT_LIMIT: (3, 3),
}


class TestConfigurationValidation:
    """T25 accepts exactly the integer bounds ``0``..``MAX_ITERATIONS``."""

    @pytest.mark.parametrize("value", [0, 1, 2, 5, MAX_ITERATIONS])
    def test_a_usable_bound_is_valid(self, value):
        policy = IterationLimitPolicy(max_iterations=value)
        assert policy.is_valid is True
        assert policy.validated_max_iterations == value
        assert policy.refusal_reason is None

    @pytest.mark.parametrize(
        "value",
        [None, -1, -100, True, False, 1.0, 2.5, "3", [], {}, object()],
    )
    def test_an_unusable_bound_is_refused(self, value):
        policy = IterationLimitPolicy(max_iterations=value)
        assert policy.is_valid is False
        assert policy.validated_max_iterations is None
        assert policy.refusal_reason

    def test_the_default_is_not_configured_and_therefore_invalid(self):
        assert DEFAULT_MAX_ITERATIONS is None
        policy = IterationLimitPolicy()
        assert policy.max_iterations is None
        assert policy.is_valid is False
        assert "not read as unlimited" in policy.refusal_reason

    def test_a_bool_is_never_read_as_an_integer(self):
        for value in (True, False):
            policy = IterationLimitPolicy(max_iterations=value)
            assert policy.is_valid is False
            assert "bool" in policy.refusal_reason

    def test_a_negative_bound_is_named_in_the_reason(self):
        reason = IterationLimitPolicy(max_iterations=-2).refusal_reason
        assert "negative" in reason
        assert "-2" in reason

    def test_a_bound_above_the_supported_maximum_is_refused(self):
        policy = IterationLimitPolicy(max_iterations=MAX_ITERATIONS + 1)
        assert policy.is_valid is False
        assert str(MAX_ITERATIONS) in policy.refusal_reason
        assert not IterationLimitPolicy(max_iterations=10 ** 9).is_valid

    def test_the_policy_is_frozen(self):
        policy = IterationLimitPolicy(max_iterations=1)
        with pytest.raises(FrozenInstanceError):
            policy.max_iterations = 5

    @pytest.mark.parametrize("value", [None, -1, True, 1.5, "3", 10 ** 9])
    def test_an_invalid_policy_authorises_nothing(self, value):
        evaluation = evaluate_iteration_limit(
            policy=IterationLimitPolicy(max_iterations=value),
            attempt=IterationAttempt(current_iteration=1),
        )
        assert evaluation.status is IterationLimitStatus.INVALID_CONFIG
        assert evaluation.decision is IterationLimitDecision.REFUSE
        assert evaluation.attempt_allowed is False
        assert evaluation.next_iteration_allowed is False
        assert evaluation.limit_reached is False
        assert evaluation.remaining_iterations is None
        assert evaluation.max_iterations is None

    def test_a_zero_bound_never_authorises_an_attempt(self):
        policy = IterationLimitPolicy(max_iterations=0)
        assert policy.is_valid is True
        for current in range(1, 6):
            evaluation = evaluate_iteration_limit(
                policy=policy,
                attempt=IterationAttempt(current_iteration=current),
            )
            assert evaluation.status is IterationLimitStatus.EXCEEDED
            assert evaluation.decision is IterationLimitDecision.REFUSE
            assert evaluation.attempt_allowed is False


class TestIterationValidation:
    """The caller-asserted attempt number is validated, never repaired."""

    @pytest.mark.parametrize("value", [None, 0, -1, -5, True, False, 1.5, "1"])
    def test_an_unusable_attempt_number_is_refused(self, value):
        evaluation = evaluate_iteration_limit(
            policy=IterationLimitPolicy(max_iterations=3),
            attempt=IterationAttempt(current_iteration=value),
        )
        assert evaluation.status is IterationLimitStatus.INVALID_ITERATION
        assert evaluation.decision is IterationLimitDecision.REFUSE
        assert evaluation.current_iteration is None
        assert evaluation.attempt_allowed is False
        assert evaluation.next_iteration_allowed is False
        assert evaluation.limit_reached is False
        assert evaluation.remaining_iterations is None
        assert evaluation.max_iterations == 3

    @pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "1"])
    def test_the_attempt_reason_is_deterministic(self, value):
        evaluation = evaluate(3, value)
        assert evaluation.reason == module._iteration_problem(value)

    def test_zero_is_never_read_as_the_first_attempt(self):
        evaluation = evaluate(3, 0)
        assert evaluation.status is IterationLimitStatus.INVALID_ITERATION
        assert "start at 1" in evaluation.reason

    def test_the_module_never_resets_or_renumbers_a_counter(self):
        assert evaluate(3, 0).reasons[1] == (
            "T25 never resets, repairs or renumbers an iteration counter."
        )


class TestBoundarySemantics:
    """``1 <= current_iteration <= max_iterations``, with no off-by-one."""

    def test_one_iteration_allows_attempt_one_and_refuses_attempt_two(self):
        assert evaluate(1, 1).status is IterationLimitStatus.AT_LIMIT
        assert evaluate(1, 1).attempt_allowed is True
        assert evaluate(1, 2).status is IterationLimitStatus.EXCEEDED
        assert evaluate(1, 2).attempt_allowed is False

    def test_two_iterations_allow_attempts_one_and_two_and_refuse_three(self):
        assert evaluate(2, 1).status is IterationLimitStatus.WITHIN_LIMIT
        assert evaluate(2, 2).status is IterationLimitStatus.AT_LIMIT
        assert evaluate(2, 3).status is IterationLimitStatus.EXCEEDED
        assert evaluate(2, 3).decision is IterationLimitDecision.REFUSE

    @pytest.mark.parametrize("limit", [1, 2, 3, 5])
    def test_the_boundary_is_exact(self, limit):
        for current in range(1, limit + 1):
            evaluation = evaluate(limit, current)
            assert evaluation.is_allowed is True
            assert evaluation.attempt_allowed is True
            assert evaluation.limit_reached is (current == limit)
            assert evaluation.remaining_iterations == limit - current
            assert evaluation.next_iteration_allowed is (current < limit)
        refused = evaluate(limit, limit + 1)
        assert refused.status is IterationLimitStatus.EXCEEDED
        assert refused.is_allowed is False
        assert refused.attempt_allowed is False
        assert refused.next_iteration_allowed is False
        assert refused.limit_reached is True
        assert refused.remaining_iterations == 0

    def test_next_iteration_allowed_matches_the_next_evaluation(self):
        for limit in (1, 2, 4):
            for current in range(1, limit + 2):
                assert evaluate(limit, current).next_iteration_allowed is (
                    evaluate(limit, current + 1).attempt_allowed
                )

    def test_a_huge_attempt_number_is_refused_rather_than_capped(self):
        evaluation = evaluate(MAX_ITERATIONS, 10 ** 9)
        assert evaluation.status is IterationLimitStatus.EXCEEDED
        assert evaluation.current_iteration == 10 ** 9
        assert evaluation.reason

    def test_the_largest_accepted_bound_is_reachable(self):
        evaluation = evaluate(MAX_ITERATIONS, MAX_ITERATIONS)
        assert evaluation.status is IterationLimitStatus.AT_LIMIT
        assert evaluation.is_allowed is True

    @pytest.mark.parametrize("limit", [0, 1, 2, 3])
    def test_every_allowed_evaluation_comes_from_an_allowed_status(self, limit):
        for current in range(1, limit + 3):
            evaluation = evaluate(limit, current)
            assert evaluation.is_allowed is (
                evaluation.status in ALLOWED_STATUSES
            )
            if evaluation.is_allowed:
                assert 1 <= current <= limit
            else:
                assert evaluation.decision is IterationLimitDecision.REFUSE


class TestPrecedenceAndStatuses:
    """The documented evaluation order decides contradictory inputs."""

    def test_the_precedence_is_the_documented_order(self):
        assert PRECEDENCE == (
            IterationLimitStatus.INVALID_CONFIG,
            IterationLimitStatus.INVALID_ITERATION,
            IterationLimitStatus.EXCEEDED,
            IterationLimitStatus.WITHIN_LIMIT,
            IterationLimitStatus.AT_LIMIT,
        )
        assert set(PRECEDENCE) == set(IterationLimitStatus)

    def test_every_status_is_reachable(self):
        assert set(STATUS_INPUTS) == set(IterationLimitStatus)
        for status, (limit, current) in STATUS_INPUTS.items():
            assert evaluate(limit, current).status is status

    @pytest.mark.parametrize(
        "limit, current, expected",
        (
            (None, 0, IterationLimitStatus.INVALID_CONFIG),
            (None, None, IterationLimitStatus.INVALID_CONFIG),
            (True, 0, IterationLimitStatus.INVALID_CONFIG),
            (2, None, IterationLimitStatus.INVALID_ITERATION),
            (0, 0, IterationLimitStatus.INVALID_ITERATION),
            (2, 0, IterationLimitStatus.INVALID_ITERATION),
        ),
    )
    def test_a_conflict_is_decided_by_the_earliest_status(
        self, limit, current, expected
    ):
        assert evaluate(limit, current).status is expected

    def test_every_verdict_carries_a_reason_and_a_trail(self):
        for status, (limit, current) in STATUS_INPUTS.items():
            evaluation = evaluate(limit, current)
            assert evaluation.reason
            assert evaluation.reasons[0] == evaluation.reason
            assert evaluation.policy_version == POLICY_VERSION


class TestRetrySafety:
    """No outcome, and no amount of retrying, can reset or bypass the limit."""

    #: A scripted retry loop: (iteration, what happened, expected status).
    TRACE = (
        (1, "still-open", IterationLimitStatus.WITHIN_LIMIT),
        (2, "tests-failed", IterationLimitStatus.WITHIN_LIMIT),
        (3, "review-required", IterationLimitStatus.AT_LIMIT),
        (4, "fixed", IterationLimitStatus.EXCEEDED),
        (5, "still-open", IterationLimitStatus.EXCEEDED),
    )

    def test_a_retry_loop_stops_exactly_at_the_limit(self):
        policy = IterationLimitPolicy(max_iterations=3)
        snapshot = IterationLimitPolicy(max_iterations=3)
        for iteration, _outcome, expected in self.TRACE:
            evaluation = evaluate_iteration_limit(
                policy=policy,
                attempt=IterationAttempt(current_iteration=iteration),
            )
            assert evaluation.status is expected, iteration
            assert policy == snapshot  # nothing is counted or remembered

    def test_a_success_at_an_earlier_iteration_does_not_raise_the_limit(self):
        policy = IterationLimitPolicy(max_iterations=2)
        last = evaluate_iteration_limit(
            policy=policy,
            attempt=IterationAttempt(current_iteration=2),
        )
        assert last.is_allowed is True
        beyond = evaluate_iteration_limit(
            policy=policy,
            attempt=IterationAttempt(current_iteration=3),
        )
        assert beyond.status is IterationLimitStatus.EXCEEDED
        assert policy.max_iterations == 2

    def test_a_failure_never_resets_the_counter(self):
        # The counter is the caller's: every refusal repeats the same verdict,
        # however many times the caller asks after a failure.
        for _ in range(5):
            evaluation = evaluate(2, 3)
            assert evaluation.status is IterationLimitStatus.EXCEEDED
        assert evaluate(2, 1).status is IterationLimitStatus.WITHIN_LIMIT

    @pytest.mark.parametrize("outcome", RETRY_OUTCOMES)
    def test_no_outcome_can_change_the_verdict(self, outcome):
        baseline = evaluate(2, 3)
        assert baseline.status is IterationLimitStatus.EXCEEDED
        assert module._iteration(outcome) is None
        assert not IterationLimitPolicy(max_iterations=outcome).is_valid
        assert (
            evaluate_iteration_limit(
                policy=IterationLimitPolicy(max_iterations=2),
                attempt=IterationAttempt(current_iteration=outcome),
            ).status
            is IterationLimitStatus.INVALID_ITERATION
        )

    @pytest.mark.parametrize("outcome", RETRY_OUTCOMES)
    def test_a_spent_limit_stays_spent_whatever_the_outcome_was(self, outcome):
        # `outcome` is narrative for this test only: T25 has no parameter for it,
        # and the vocabulary cannot be smuggled in as a bound or an iteration.
        baseline = evaluate(2, 3)
        assert baseline.status is IterationLimitStatus.EXCEEDED
        assert baseline.attempt_allowed is False
        assert module._iterations(outcome) is None
        assert module._iteration(outcome) is None
        assert evaluate(2, 3).as_dict() == baseline.as_dict()

    def test_every_production_outcome_is_outside_the_contract(self):
        assert RETRY_OUTCOMES
        assert "fixed" in RETRY_OUTCOMES
        assert "still-open" in RETRY_OUTCOMES
        assert "review-required" in RETRY_OUTCOMES
        assert "pushed" in RETRY_OUTCOMES
        assert all(
            not isinstance(outcome, int) for outcome in RETRY_OUTCOMES
        )


class TestDeterminismAndImmutability:
    """Nothing here counts, caches or mutates between calls."""

    def test_the_same_inputs_always_produce_an_equal_verdict(self):
        first = evaluate(3, 2)
        second = evaluate(3, 2)
        assert first == second
        assert first.as_dict() == second.as_dict()

    def test_repeated_evaluation_of_the_same_attempt_is_repeatable(self):
        for _ in range(5):
            assert evaluate(1, 1).as_dict() == evaluate(1, 1).as_dict()

    def test_the_policy_and_the_attempt_are_never_mutated(self):
        policy = IterationLimitPolicy(max_iterations=2)
        attempt = IterationAttempt(current_iteration=1)
        evaluate_iteration_limit(policy=policy, attempt=attempt)
        assert policy == IterationLimitPolicy(max_iterations=2)
        assert attempt == IterationAttempt(current_iteration=1)
        assert attempt.current_iteration == 1

    def test_every_record_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            evaluate().status = IterationLimitStatus.EXCEEDED
        with pytest.raises(FrozenInstanceError):
            IterationLimitPolicy(max_iterations=1).max_iterations = 2
        with pytest.raises(FrozenInstanceError):
            IterationAttempt().current_iteration = 1

    def test_the_verdict_does_not_keep_the_counter(self):
        evaluation = evaluate(3, 2)
        assert evaluation.max_iterations == 3
        assert evaluation.current_iteration == 2
        assert not hasattr(evaluation, "next_iteration")
        assert not hasattr(evaluation, "iteration_count")


class TestSerialization:
    """The serialized verdict is deterministic, JSON-safe and secret-free."""

    def test_the_verdict_serializes_deterministically(self):
        evaluation = evaluate(3, 2)
        payload = evaluation.as_dict()
        assert json.dumps(payload) == json.dumps(evaluation.as_dict())
        assert json.loads(json.dumps(payload)) == payload

    def test_the_policy_view_reports_the_validated_bound(self):
        assert IterationLimitPolicy(max_iterations=4).as_dict() == {
            "policy_version": POLICY_VERSION,
            "max_iterations": 4,
            "maximum_supported_iterations": MAX_ITERATIONS,
            "is_valid": True,
            "refusal_reason": None,
        }

    def test_an_unusable_bound_is_never_echoed(self):
        policy = IterationLimitPolicy(max_iterations="squ_do_not_publish_me")
        serialized = json.dumps(policy.as_dict())
        assert "squ_do_not_publish_me" not in serialized
        assert policy.as_dict()["max_iterations"] is None
        assert "str" in policy.as_dict()["refusal_reason"]

    def test_the_attempt_view_publishes_only_a_usable_attempt(self):
        assert IterationAttempt(current_iteration=2).as_dict() == {
            "current_iteration": 2,
            "is_usable_attempt": True,
        }
        assert IterationAttempt(current_iteration="1").as_dict() == {
            "current_iteration": None,
            "is_usable_attempt": False,
        }

    def test_the_verdict_view_carries_the_truth_table(self):
        assert evaluate(3, 1).as_dict() == {
            "policy_version": POLICY_VERSION,
            "status": "within-limit",
            "decision": "allow",
            "attempt_allowed": True,
            "next_iteration_allowed": True,
            "limit_reached": False,
            "max_iterations": 3,
            "current_iteration": 1,
            "remaining_iterations": 2,
            "reason": "Iteration 1 of at most 3 permitted iteration(s) may run.",
            "reasons": [
                "Iteration 1 of at most 3 permitted iteration(s) may run.",
                "2 further iteration(s) remain before the limit is reached.",
            ],
            "policy": {
                "policy_version": POLICY_VERSION,
                "max_iterations": 3,
                "maximum_supported_iterations": MAX_ITERATIONS,
                "is_valid": True,
                "refusal_reason": None,
            },
        }


class TestHelpers:
    """The validators accept exactly what the contract documents."""

    @pytest.mark.parametrize(
        "value, expected",
        (
            (0, 0),
            (5, 5),
            (MAX_ITERATIONS, MAX_ITERATIONS),
            (-1, None),
            (MAX_ITERATIONS + 1, None),
            (True, None),
            (False, None),
            (1.5, None),
            ("3", None),
            (None, None),
        ),
    )
    def test_iterations(self, value, expected):
        assert module._iterations(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            (1, 1),
            (7, 7),
            (0, None),
            (-1, None),
            (True, None),
            (1.0, None),
            ("1", None),
            (None, None),
        ),
    )
    def test_iteration(self, value, expected):
        assert module._iteration(value) == expected


class TestModuleSurface:
    """T25 is a standard-library-only policy: no I/O and no third input."""

    def test_the_module_surface_is_exactly_the_documented_one(self):
        assert tuple(module.__all__) == (
            "ALLOWED_STATUSES",
            "DEFAULT_MAX_ITERATIONS",
            "IterationAttempt",
            "IterationLimitDecision",
            "IterationLimitEvaluation",
            "IterationLimitPolicy",
            "IterationLimitStatus",
            "MAX_ITERATIONS",
            "POLICY_VERSION",
            "PRECEDENCE",
            "evaluate_iteration_limit",
        )
        assert POLICY_VERSION == "t25.1"
        assert module.__all__ == tuple(sorted(module.__all__))

    @pytest.mark.parametrize(
        "name",
        (
            "subprocess",
            "os",
            "shutil",
            "socket",
            "time",
            "pathlib",
            "tempfile",
            "requests",
            "json",
            "random",
            "logging",
            "issue_status",
            "git_commit",
            "git_push",
        ),
    )
    def test_no_execution_or_status_dependency_is_imported(self, name):
        assert not hasattr(module, name)

    def test_the_module_imports_the_standard_library_only(self):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert imported_modules(source) == {
            "__future__",
            "dataclasses",
            "enum",
            "typing",
        }

    def test_the_import_scanner_sees_both_import_forms(self):
        assert imported_modules("import json\nfrom re import match\n") == {
            "json",
            "re",
        }

    @pytest.mark.parametrize(
        "forbidden",
        (
            "subprocess",
            "system",
            "Popen",
            "socket",
            "urlopen",
            "open",
            "shutil",
            "tempfile",
            "eval",
            "exec",
            "__dict__",
            "print",
            "input",
        ),
    )
    def test_the_module_code_never_executes_or_introspects(self, forbidden):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        referenced = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        assert forbidden not in referenced

    def test_the_policy_can_only_be_asked_with_a_policy_and_an_attempt(self):
        signature = inspect.signature(evaluate_iteration_limit)
        assert tuple(signature.parameters) == ("policy", "attempt")
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )

    def test_no_parameter_could_carry_an_outcome(self):
        for parameter in inspect.signature(
            evaluate_iteration_limit
        ).parameters.values():
            assert parameter.name not in (
                "status",
                "outcome",
                "result",
                "situation",
            )
        assert list(IterationLimitPolicy.__dataclass_fields__) == [
            "max_iterations"
        ]
        assert list(IterationAttempt.__dataclass_fields__) == [
            "current_iteration"
        ]

    def test_the_module_exposes_no_way_to_advance_the_counter(self):
        for name in ("advance", "reset", "increment", "record_result"):
            assert not hasattr(module, name)
        for name in ("advance", "reset"):
            assert not hasattr(IterationLimitPolicy, name)
            assert not hasattr(IterationAttempt, name)

    @pytest.mark.parametrize("value", ["engine", "pipeline", "orchestrator"])
    def test_an_unexpected_keyword_is_rejected(self, value):
        with pytest.raises(TypeError):
            evaluate_iteration_limit(**{value: 1})

    def test_a_non_dto_caller_is_a_programming_error_not_a_refusal(self):
        with pytest.raises(TypeError, match="IterationLimitPolicy"):
            evaluate_iteration_limit(policy=None, attempt=IterationAttempt())
        with pytest.raises(TypeError, match="IterationAttempt"):
            evaluate_iteration_limit(
                policy=IterationLimitPolicy(max_iterations=1),
                attempt=None,
            )
