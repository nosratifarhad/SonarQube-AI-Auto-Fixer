"""T24 unit tests: the pure max-issue-limit policy (no I/O, no orchestration).

This file pins the whole contract of ``issue_limit``:

* configuration validation - including that a ``bool`` is never read as an
  integer and that anything unusable (``None`` above all) refuses instead of
  meaning "unlimited";
* selection validation - including that an unordered collection, a repeated key
  and a self-contradictory pair of counts are all refusals;
* the acceptance rule ``selected_issue_count <= max_issue_limit``, with no
  truncation and no way to mistake the discovered count for the selected one;
* the deterministic evaluation order (:data:`issue_limit.PRECEDENCE`);
* determinism, immutability, caller-collection safety and canonical
  serialization;
* the module surface: standard library only, no execution primitive, and no way
  to ask the policy anything but (policy, selection).
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from dataclasses import FrozenInstanceError

import pytest

import issue_limit as module
from issue_limit import (
    ALLOWED_STATUSES,
    DEFAULT_MAX_ISSUE_LIMIT,
    MAX_ISSUE_LIMIT,
    POLICY_VERSION,
    PRECEDENCE,
    IssueLimitDecision,
    IssueLimitEvaluation,
    IssueLimitPolicy,
    IssueLimitStatus,
    IssueSelection,
    evaluate_issue_limit,
)

#: Two clearly ordered keys, so an accidental sort would be visible.
ORDERED_KEYS = ("ZZZ-2", "AAA-1")
#: A decoy that must never appear in anything this module serializes.
DECOY_SECRET = "squ_do_not_publish_me"


def evaluate(
    max_issue_limit=2,
    discovered=3,
    keys=(),
) -> IssueLimitEvaluation:
    """Evaluate one ``(limit, discovered, keys)`` triple."""
    return evaluate_issue_limit(
        policy=IssueLimitPolicy(max_issue_limit=max_issue_limit),
        selection=IssueSelection(
            discovered_issue_count=discovered,
            selected_issue_keys=keys,
        ),
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


#: One input triple that produces each status, so every status is reachable.
STATUS_INPUTS = {
    IssueLimitStatus.INVALID_CONFIG: (None, 0, ()),
    IssueLimitStatus.INVALID_DISCOVERED_COUNT: (5, None, ()),
    IssueLimitStatus.INVALID_SELECTION: (5, 5, {"A"}),
    IssueLimitStatus.DUPLICATE_ISSUES: (5, 5, ("A", "A")),
    IssueLimitStatus.IMPOSSIBLE_COUNTS: (5, 1, ("A", "B")),
    IssueLimitStatus.EXCEEDED: (1, 5, ("A", "B")),
    IssueLimitStatus.AT_LIMIT: (2, 5, ORDERED_KEYS),
    IssueLimitStatus.WITHIN_LIMIT: (5, 5, ("A",)),
}


class TestConfigurationValidation:
    """T24 accepts exactly the integer bounds ``0``..``MAX_ISSUE_LIMIT``."""

    @pytest.mark.parametrize("value", [0, 1, 2, 50, MAX_ISSUE_LIMIT])
    def test_a_usable_bound_is_valid(self, value):
        policy = IssueLimitPolicy(max_issue_limit=value)
        assert policy.is_valid is True
        assert policy.validated_limit == value
        assert policy.refusal_reason is None

    @pytest.mark.parametrize(
        "value",
        [None, -1, -100, True, False, 1.0, 2.5, "3", [], {}, object()],
    )
    def test_an_unusable_bound_is_refused(self, value):
        policy = IssueLimitPolicy(max_issue_limit=value)
        assert policy.is_valid is False
        assert policy.validated_limit is None
        assert policy.refusal_reason

    def test_the_default_is_not_configured_and_therefore_invalid(self):
        assert DEFAULT_MAX_ISSUE_LIMIT is None
        policy = IssueLimitPolicy()
        assert policy.max_issue_limit is None
        assert policy.is_valid is False
        assert "not read as unlimited" in policy.refusal_reason

    def test_a_bool_is_never_read_as_an_integer(self):
        for value in (True, False):
            policy = IssueLimitPolicy(max_issue_limit=value)
            assert policy.is_valid is False
            assert "bool" in policy.refusal_reason

    def test_a_negative_bound_is_named_in_the_reason(self):
        reason = IssueLimitPolicy(max_issue_limit=-2).refusal_reason
        assert "negative" in reason
        assert "-2" in reason

    def test_a_bound_above_the_supported_maximum_is_refused(self):
        policy = IssueLimitPolicy(max_issue_limit=MAX_ISSUE_LIMIT + 1)
        assert policy.is_valid is False
        assert str(MAX_ISSUE_LIMIT) in policy.refusal_reason

    def test_the_policy_is_frozen(self):
        policy = IssueLimitPolicy(max_issue_limit=1)
        with pytest.raises(FrozenInstanceError):
            policy.max_issue_limit = 5

    @pytest.mark.parametrize("value", [None, -1, True, 1.5, "3"])
    def test_an_invalid_policy_refuses_even_an_empty_selection(self, value):
        evaluation = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=value),
            selection=IssueSelection(discovered_issue_count=0),
        )
        assert evaluation.status is IssueLimitStatus.INVALID_CONFIG
        assert evaluation.decision is IssueLimitDecision.REFUSE
        assert evaluation.accepted_issue_count == 0
        assert evaluation.accepted_issue_keys == ()
        assert evaluation.remaining_issue_slots is None
        assert evaluation.can_accept_more is False


class TestSelectionValidation:
    """The caller-asserted counts and keys are validated, never repaired."""

    @pytest.mark.parametrize("value", [None, -1, -5, True, False, 1.5, "3"])
    def test_an_unusable_discovered_count_is_refused(self, value):
        evaluation = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=5),
            selection=IssueSelection(
                discovered_issue_count=value,
                selected_issue_keys=("A",),
            ),
        )
        assert evaluation.status is IssueLimitStatus.INVALID_DISCOVERED_COUNT
        assert evaluation.decision is IssueLimitDecision.REFUSE
        assert evaluation.discovered_issue_count is None
        assert evaluation.selected_issue_count == 1

    @pytest.mark.parametrize("value", [None, -1, True, 1.5, "3"])
    def test_the_discovered_count_reason_is_deterministic(self, value):
        assert evaluate(2, value, ("A",)).reason == (
            module._discovered_problem(value)
        )

    @pytest.mark.parametrize(
        "value",
        [
            "AAA-1",
            b"AAA-1",
            {"AAA-1", "BBB-2"},
            frozenset({"AAA-1"}),
            {"AAA-1": 1},
            range(2),
            {"AAA-1", "BBB-2"} | {"CCC-3"},
        ],
    )
    def test_an_unordered_or_unusable_selection_is_refused(self, value):
        evaluation = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=5),
            selection=IssueSelection(
                discovered_issue_count=5,
                selected_issue_keys=value,
            ),
        )
        assert evaluation.status is IssueLimitStatus.INVALID_SELECTION
        assert evaluation.selected_issue_count is None
        assert evaluation.accepted_issue_keys == ()

    def test_a_one_shot_iterator_is_refused_because_it_has_no_order(self):
        evaluation = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=5),
            selection=IssueSelection(
                discovered_issue_count=5,
                selected_issue_keys=iter(("A", "B")),
            ),
        )
        assert evaluation.status is IssueLimitStatus.INVALID_SELECTION
        assert "deterministic order" in evaluation.reason

    @pytest.mark.parametrize(
        "value",
        [("A", 1), ("A", ""), ("A", None), (1, 2), (b"A",)],
    )
    def test_a_non_text_or_empty_key_is_refused(self, value):
        evaluation = evaluate(5, 5, value)
        assert evaluation.status is IssueLimitStatus.INVALID_SELECTION
        assert "non-empty string" in evaluation.reason

    def test_a_repeated_key_is_refused(self):
        evaluation = evaluate(5, 5, ("A", "B", "A"))
        assert evaluation.status is IssueLimitStatus.DUPLICATE_ISSUES
        assert evaluation.decision is IssueLimitDecision.REFUSE
        assert evaluation.selected_issue_count == 3
        assert evaluation.accepted_issue_keys == ()

    def test_an_impossible_pair_of_counts_is_refused(self):
        evaluation = evaluate(5, 2, ("A", "B", "C"))
        assert evaluation.status is IssueLimitStatus.IMPOSSIBLE_COUNTS
        assert evaluation.selected_issue_count == 3
        assert evaluation.discovered_issue_count == 2


class TestAcceptanceAndRefusal:
    """``selected_issue_count <= max_issue_limit`` is the whole rule."""

    def test_a_selection_below_the_limit_is_accepted_with_room_left(self):
        evaluation = evaluate(3, 10, ("A", "B"))
        assert evaluation.status is IssueLimitStatus.WITHIN_LIMIT
        assert evaluation.decision is IssueLimitDecision.ALLOW
        assert evaluation.is_allowed is True
        assert evaluation.accepted_issue_count == 2
        assert evaluation.accepted_issue_keys == ("A", "B")
        assert evaluation.remaining_issue_slots == 1
        assert evaluation.can_accept_more is True
        assert evaluation.limit_exceeded is False

    def test_a_selection_exactly_at_the_limit_is_accepted_and_full(self):
        evaluation = evaluate(2, 2, ORDERED_KEYS)
        assert evaluation.status is IssueLimitStatus.AT_LIMIT
        assert evaluation.is_allowed is True
        assert evaluation.accepted_issue_count == 2
        assert evaluation.remaining_issue_slots == 0
        assert evaluation.can_accept_more is False

    def test_a_selection_above_the_limit_is_refused_not_truncated(self):
        evaluation = evaluate(1, 5, ORDERED_KEYS)
        assert evaluation.status is IssueLimitStatus.EXCEEDED
        assert evaluation.decision is IssueLimitDecision.REFUSE
        assert evaluation.limit_exceeded is True
        assert evaluation.accepted_issue_count == 0
        assert evaluation.accepted_issue_keys == ()
        assert evaluation.selected_issue_count == 2
        assert evaluation.remaining_issue_slots is None
        assert "never truncates" in evaluation.reasons[1]

    def test_the_single_issue_limit_accepts_exactly_one_issue(self):
        assert evaluate(1, 5, ("A",)).status is IssueLimitStatus.AT_LIMIT
        assert evaluate(1, 5, ()).status is IssueLimitStatus.WITHIN_LIMIT
        assert evaluate(1, 5, ("A", "B")).status is IssueLimitStatus.EXCEEDED

    def test_zero_limit_can_never_let_an_issue_through(self):
        assert evaluate(0, 0, ()).status is IssueLimitStatus.AT_LIMIT
        assert evaluate(0, 1, ("A",)).status is IssueLimitStatus.EXCEEDED
        for count in range(1, 5):
            keys = tuple(f"K{index}" for index in range(count))
            evaluation = evaluate(0, count, keys)
            assert evaluation.decision is IssueLimitDecision.REFUSE
            assert evaluation.accepted_issue_count == 0

    def test_an_empty_selection_is_accepted_by_every_valid_policy(self):
        for limit in (0, 1, 5):
            evaluation = evaluate(limit, 0, ())
            assert evaluation.is_allowed is True
            assert evaluation.accepted_issue_count == 0
            assert evaluation.remaining_issue_slots == limit

    @pytest.mark.parametrize("limit", [0, 1, 2, 3])
    @pytest.mark.parametrize("discovered", [0, 1, 3, 6])
    def test_the_accepted_selection_never_exceeds_the_limit(
        self, limit, discovered
    ):
        for size in range(0, discovered + 1):
            keys = tuple(f"K{index}" for index in range(size))
            evaluation = evaluate(limit, discovered, keys)
            assert evaluation.accepted_issue_count <= limit
            assert evaluation.status in ALLOWED_STATUSES or (
                evaluation.accepted_issue_count == 0
            )
            if evaluation.is_allowed:
                assert evaluation.accepted_issue_count == size
                assert evaluation.accepted_issue_count <= discovered

    def test_discovered_equal_to_selected_is_accepted(self):
        evaluation = evaluate(3, 3, ("A", "B", "C"))
        assert evaluation.status is IssueLimitStatus.AT_LIMIT
        assert evaluation.decision is IssueLimitDecision.ALLOW

    def test_discovered_greater_than_selected_is_accepted(self):
        evaluation = evaluate(3, 9, ("A",))
        assert evaluation.status is IssueLimitStatus.WITHIN_LIMIT

    def test_the_discovered_count_is_never_confused_with_the_selected_one(self):
        # The bound applies to the *selection*, so a small limit still refuses a
        # large selection even though many issues were retrieved ...
        refused = evaluate(1, 100, ("A", "B"))
        assert refused.status is IssueLimitStatus.EXCEEDED
        assert refused.selected_issue_count == 2
        assert refused.discovered_issue_count == 100
        # ... and a selection smaller than what was retrieved is accepted
        # without the retrieved volume ever counting as processed.
        accepted = evaluate(100, 100, ("A",))
        assert accepted.is_allowed is True
        assert accepted.accepted_issue_count == 1
        assert accepted.selected_issue_count == 1
        assert accepted.discovered_issue_count == 100
        assert accepted.accepted_issue_count != accepted.discovered_issue_count

    def test_the_limit_is_capped_but_the_counts_are_not(self):
        evaluation = evaluate(MAX_ISSUE_LIMIT, 10 ** 9, ())
        assert evaluation.status is IssueLimitStatus.WITHIN_LIMIT
        assert IssueLimitPolicy(max_issue_limit=MAX_ISSUE_LIMIT).is_valid
        assert not IssueLimitPolicy(max_issue_limit=10 ** 9).is_valid
        assert not IssueLimitPolicy(
            max_issue_limit=MAX_ISSUE_LIMIT + 1
        ).is_valid


class TestPrecedence:
    """The documented evaluation order decides contradictory inputs."""

    def test_the_precedence_is_the_documented_order(self):
        assert PRECEDENCE == (
            IssueLimitStatus.INVALID_CONFIG,
            IssueLimitStatus.INVALID_DISCOVERED_COUNT,
            IssueLimitStatus.INVALID_SELECTION,
            IssueLimitStatus.DUPLICATE_ISSUES,
            IssueLimitStatus.IMPOSSIBLE_COUNTS,
            IssueLimitStatus.EXCEEDED,
            IssueLimitStatus.AT_LIMIT,
            IssueLimitStatus.WITHIN_LIMIT,
        )
        assert set(PRECEDENCE) == set(IssueLimitStatus)

    def test_every_status_is_reachable(self):
        assert set(STATUS_INPUTS) == set(IssueLimitStatus)
        for status, (limit, discovered, keys) in STATUS_INPUTS.items():
            assert evaluate(limit, discovered, keys).status is status

    @pytest.mark.parametrize(
        "limit, discovered, keys, expected",
        (
            (None, None, {"A"}, IssueLimitStatus.INVALID_CONFIG),
            (None, -1, ("A",), IssueLimitStatus.INVALID_CONFIG),
            (5, None, {"A"}, IssueLimitStatus.INVALID_DISCOVERED_COUNT),
            (5, 5, {"A", "A"}, IssueLimitStatus.INVALID_SELECTION),
            (5, 1, ("A", "A"), IssueLimitStatus.DUPLICATE_ISSUES),
            (1, 1, ("A", "B"), IssueLimitStatus.IMPOSSIBLE_COUNTS),
        ),
    )
    def test_a_conflict_is_decided_by_the_earliest_status(
        self, limit, discovered, keys, expected
    ):
        assert evaluate(limit, discovered, keys).status is expected

    def test_only_the_allowed_statuses_allow(self):
        for status, (limit, discovered, keys) in STATUS_INPUTS.items():
            evaluation = evaluate(limit, discovered, keys)
            expected = (
                IssueLimitDecision.ALLOW
                if status in ALLOWED_STATUSES
                else IssueLimitDecision.REFUSE
            )
            assert evaluation.decision is expected, status
            assert evaluation.is_allowed is (status in ALLOWED_STATUSES)

    def test_every_refusal_carries_no_acceptance_and_a_reason(self):
        for status, (limit, discovered, keys) in STATUS_INPUTS.items():
            if status in ALLOWED_STATUSES:
                continue
            evaluation = evaluate(limit, discovered, keys)
            assert evaluation.accepted_issue_count == 0
            assert evaluation.accepted_issue_keys == ()
            assert evaluation.remaining_issue_slots is None
            assert evaluation.can_accept_more is False
            assert evaluation.reason
            assert evaluation.reasons[0] == evaluation.reason


class TestDeterminismAndImmutability:
    """Nothing here sorts, caches, counts between calls or mutates a caller."""

    def test_the_callers_order_is_preserved_and_never_sorted(self):
        evaluation = evaluate(5, 5, ORDERED_KEYS)
        assert evaluation.accepted_issue_keys == ORDERED_KEYS
        assert evaluation.accepted_issue_keys != tuple(sorted(ORDERED_KEYS))

    def test_the_callers_collection_is_never_mutated(self):
        keys = ["ZZZ-2", "AAA-1"]
        snapshot = list(keys)
        evaluation = evaluate(5, 5, keys)
        assert keys == snapshot
        assert evaluation.accepted_issue_keys == ("ZZZ-2", "AAA-1")
        assert evaluation.accepted_issue_keys is not keys

    def test_the_same_inputs_always_produce_an_equal_verdict(self):
        first = evaluate(3, 4, ("A", "B"))
        second = evaluate(3, 4, ("A", "B"))
        assert first == second
        assert first.as_dict() == second.as_dict()

    def test_the_policy_is_unchanged_by_an_evaluation(self):
        policy = IssueLimitPolicy(max_issue_limit=2)
        evaluate_issue_limit(
            policy=policy,
            selection=IssueSelection(
                discovered_issue_count=3,
                selected_issue_keys=ORDERED_KEYS,
            ),
        )
        assert policy == IssueLimitPolicy(max_issue_limit=2)
        assert policy.max_issue_limit == 2
        assert policy.is_valid is True
        assert policy.refusal_reason is None

    def test_the_verdict_policy_and_selection_are_all_frozen(self):
        with pytest.raises(FrozenInstanceError):
            evaluate().status = IssueLimitStatus.EXCEEDED
        with pytest.raises(FrozenInstanceError):
            IssueLimitPolicy(max_issue_limit=1).max_issue_limit = 2
        with pytest.raises(FrozenInstanceError):
            IssueSelection().discovered_issue_count = 1

    def test_a_refusal_is_repeatable(self):
        for _ in range(3):
            evaluation = evaluate(1, 5, ("A", "B"))
            assert evaluation.status is IssueLimitStatus.EXCEEDED
            assert evaluation.reason == evaluate(1, 5, ("A", "B")).reason


class TestSerialization:
    """The serialized verdict is deterministic, secret-free and countable."""

    def test_the_verdict_serializes_deterministically(self):
        evaluation = evaluate(3, 4, ("A",))
        payload = evaluation.as_dict()
        assert json.dumps(payload) == json.dumps(evaluation.as_dict())
        assert json.loads(json.dumps(payload)) == payload

    def test_the_verdict_never_publishes_the_callers_keys(self):
        evaluation = evaluate(5, 5, (DECOY_SECRET,))
        serialized = json.dumps(evaluation.as_dict())
        assert DECOY_SECRET not in serialized
        assert "accepted_issue_keys" not in evaluation.as_dict()
        assert evaluation.accepted_issue_keys == (DECOY_SECRET,)
        assert evaluation.as_dict()["accepted_issue_count"] == 1

    def test_an_unusable_policy_value_is_never_echoed(self):
        policy = IssueLimitPolicy(max_issue_limit=DECOY_SECRET)
        assert DECOY_SECRET not in json.dumps(policy.as_dict())
        assert policy.as_dict()["max_issue_limit"] is None

    def test_a_refusal_reason_never_echoes_caller_text(self):
        evaluation = evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=DECOY_SECRET),
            selection=IssueSelection(discovered_issue_count=1),
        )
        assert DECOY_SECRET not in json.dumps(evaluation.as_dict())
        assert "str" in evaluation.reason

    def test_the_selection_view_publishes_the_count_only(self):
        selection = IssueSelection(
            discovered_issue_count=4,
            selected_issue_keys=("A", "B"),
        )
        assert selection.as_dict() == {
            "discovered_issue_count": 4,
            "selected_issue_key_count": 2,
        }
        assert selection.selected_issue_key_count == 2

    def test_an_unusable_selection_field_is_not_read_as_an_empty_selection(self):
        selection = IssueSelection(
            discovered_issue_count=4,
            selected_issue_keys={"A"},
        )
        assert selection.selected_issue_key_count == 0
        assert selection.as_dict()["selected_issue_key_count"] == 0
        assert not evaluate_issue_limit(
            policy=IssueLimitPolicy(max_issue_limit=4),
            selection=selection,
        ).is_allowed


class TestHelpers:
    """The validators accept exactly what the contract documents."""

    @pytest.mark.parametrize(
        "value, expected",
        (
            (0, 0),
            (5, 5),
            (MAX_ISSUE_LIMIT, MAX_ISSUE_LIMIT),
            (-1, None),
            (MAX_ISSUE_LIMIT + 1, None),
            (True, None),
            (False, None),
            (1.5, None),
            ("3", None),
            (None, None),
        ),
    )
    def test_limit(self, value, expected):
        assert module._limit(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        ((0, 0), (7, 7), (-1, None), (True, None), (0.0, None), ("5", None)),
    )
    def test_count(self, value, expected):
        assert module._count(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            ((), ()),
            (["A"], ("A",)),
            (("A", "B"), ("A", "B")),
            (["A", 1], None),
            ({"A"}, None),
            ("A", None),
            (iter(["A"]), None),
            (None, None),
        ),
    )
    def test_keys(self, value, expected):
        assert module._keys(value) == expected

    def test_keys_returns_a_copy(self):
        keys = ["A"]
        assert module._keys(keys) == ("A",)
        assert keys == ["A"]


class TestModuleSurface:
    """T24 is a standard-library-only policy: no I/O and no second input."""

    def test_the_module_surface_is_exactly_the_documented_one(self):
        assert tuple(module.__all__) == (
            "ALLOWED_STATUSES",
            "DEFAULT_MAX_ISSUE_LIMIT",
            "IssueLimitDecision",
            "IssueLimitEvaluation",
            "IssueLimitPolicy",
            "IssueLimitStatus",
            "IssueSelection",
            "MAX_ISSUE_LIMIT",
            "POLICY_VERSION",
            "PRECEDENCE",
            "evaluate_issue_limit",
        )
        assert POLICY_VERSION == "t24.1"
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
        ),
    )
    def test_no_execution_dependency_is_imported(self, name):
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

    def test_the_policy_can_only_be_asked_with_a_policy_and_a_selection(self):
        signature = inspect.signature(evaluate_issue_limit)
        assert tuple(signature.parameters) == ("policy", "selection")
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )

    def test_the_inputs_carry_nothing_but_the_documented_fields(self):
        assert list(IssueLimitPolicy.__dataclass_fields__) == [
            "max_issue_limit"
        ]
        assert list(IssueSelection.__dataclass_fields__) == [
            "discovered_issue_count",
            "selected_issue_keys",
        ]
        assert list(IssueLimitEvaluation.__dataclass_fields__) == [
            "policy_version",
            "status",
            "decision",
            "max_issue_limit",
            "discovered_issue_count",
            "selected_issue_count",
            "accepted_issue_count",
            "accepted_issue_keys",
            "remaining_issue_slots",
            "can_accept_more",
            "reason",
            "reasons",
            "policy",
        ]

    @pytest.mark.parametrize("value", ["engine", "pipeline", "orchestrator"])
    def test_an_unexpected_keyword_is_rejected(self, value):
        with pytest.raises(TypeError):
            evaluate_issue_limit(**{value: 1})

    def test_a_non_dto_caller_is_a_programming_error_not_a_refusal(self):
        with pytest.raises(TypeError, match="IssueLimitPolicy"):
            evaluate_issue_limit(policy=None, selection=IssueSelection())
        with pytest.raises(TypeError, match="IssueSelection"):
            evaluate_issue_limit(
                policy=IssueLimitPolicy(max_issue_limit=1),
                selection=None,
            )
