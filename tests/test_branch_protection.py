"""T26 unit tests: the pure main/default branch-protection policy (no I/O, no Git).

This file pins the whole contract of ``branch_protection``:

* configuration validation - the default protected set, a custom set, an empty
  set, ``None``, a bare string, a set/mapping, malformed entries, duplicate
  normalized entries and a non-``bool`` flag are all pinned;
* candidate validation - every refused identity (``None``, empty, whitespace,
  NUL, ``..``, trailing ``.``, trailing ``.lock``, ``//``, leading ``-``,
  forbidden ref characters, ``HEAD``/``@``, a ``refs/...`` reference) and the
  names that are accepted;
* protection - protected names, case and whitespace variants, the default branch
  (equal, case-folded, invalid, missing, itself protected) and the AI-fix
  namespace;
* the exact ``PRECEDENCE`` with one conflict per adjacent pair;
* the result invariants (``is_allowed`` is ``status is ALLOWED``), determinism,
  immutability and canonical, secret-free serialization;
* compatibility with T07 (the validator and the namespace), T20 and T21 (the same
  protected names, the same namespace, and a T26 allow never widening a T20
  allow), including the documented differences;
* the module surface: standard library plus the pure T07 validator only, and no
  Git, no subprocess, no filesystem and no branch-mutating API.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import json
import pathlib
from dataclasses import FrozenInstanceError

import pytest

import branch_naming
import branch_protection as module
import commit_policy
import push_policy
from branch_naming import AGENT_BRANCH_PREFIX, make_agent_branch_name
from branch_protection import (
    AI_FIX_BRANCH_PREFIX,
    ALLOWED_STATUSES,
    POLICY_VERSION,
    PRECEDENCE,
    PROTECTED_BRANCH_NAMES,
    BranchProtectionDecision,
    BranchProtectionEvaluation,
    BranchProtectionInput,
    BranchProtectionStatus,
    ProtectedBranchPolicy,
    evaluate_branch_protection,
)

#: The repository default branch used by most tests.
DEFAULT = "main"

#: A branch T07 can create and T26 must accept.
AGENT_BRANCH = f"{AI_FIX_BRANCH_PREFIX}/AX1abcDeF2"


def decide(
    candidate=None,
    default=DEFAULT,
    protected=PROTECTED_BRANCH_NAMES,
    require_prefix=True,
) -> BranchProtectionEvaluation:
    """Evaluate one candidate under one T26 policy."""
    return evaluate_branch_protection(
        policy=ProtectedBranchPolicy(
            protected_branches=protected,
            require_ai_fix_branch_prefix=require_prefix,
        ),
        branch=BranchProtectionInput(
            candidate_branch=candidate,
            default_branch=default,
        ),
    )


def status_of(candidate=None, **kwargs) -> BranchProtectionStatus:
    """The status of one evaluation (a readable shorthand)."""
    return decide(candidate, **kwargs).status


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


#: One input per status, so every status is provably reachable.
STATUS_INPUTS = {
    BranchProtectionStatus.ALLOWED: {"candidate": AGENT_BRANCH},
    BranchProtectionStatus.PROTECTED_BRANCH: {"candidate": "develop"},
    BranchProtectionStatus.DEFAULT_BRANCH: {
        "candidate": "release/1.0",
        "default": "release/1.0",
    },
    BranchProtectionStatus.INVALID_BRANCH: {"candidate": "main/"},
    BranchProtectionStatus.INVALID_POLICY: {"protected": None},
    BranchProtectionStatus.MISSING_DEFAULT_BRANCH: {
        "candidate": AGENT_BRANCH,
        "default": None,
    },
    BranchProtectionStatus.WRONG_BRANCH_NAMESPACE: {"candidate": "feature/x"},
}

#: Candidate values that are not usable branch identities.
INVALID_CANDIDATES = (
    None,
    "",
    "   ",
    "\t",
    "\n",
    " main",
    "main ",
    "main\n",
    "a..b",
    "a.",
    "a.lock",
    "component/.lock",
    "a//b",
    "/a",
    "a/",
    "-x",
    "@",
    "HEAD",
    "a b",
    "a~b",
    "a^b",
    "a:b",
    "a?b",
    "a*b",
    "a[b",
    "a\\b",
    ".hidden",
    "a/@/b",
    "@{-1}",
    "refs/heads/main",
    "REFS/HEADS/MAIN",
    "refs/",
    "a\x00b",
    f"{AI_FIX_BRANCH_PREFIX}/\x00",
    f"{AI_FIX_BRANCH_PREFIX}/a..b",
    f"{AI_FIX_BRANCH_PREFIX}/",
    123,
    1.5,
    True,
    b"main",
    ["main"],
    {"main"},
    frozenset({"main"}),
)

#: Protected-branch configurations that are not usable.
INVALID_PROTECTED_SETS = (
    None,
    "main",
    b"main",
    {"main"},
    frozenset({"main"}),
    {"main": 1},
    5,
    1.5,
    object(),
    ("main", None),
    ("main", 1),
    ("main", ""),
    ("main", "   "),
    ("main", "a."),
    ("main", "a.lock"),
    ("main", "refs/heads/main"),
    ("main", "HEAD"),
    ("main", "a b"),
    ("main", "main"),
    ("main", "MAIN"),
    ("Main", "main"),
    ("release/1.0", "Release/1.0"),
)

#: Values that are not usable AI-prefix flags.
INVALID_FLAGS = ("yes", "false", 1, 0, 1.0, None, [], {}, b"yes")


class TestPolicyConfiguration:
    """The protected set and the namespace flag are validated, never guessed."""

    def test_the_default_policy_is_the_conservative_one(self):
        policy = ProtectedBranchPolicy()
        assert policy.is_valid is True
        assert policy.refusal_reason is None
        assert policy.require_ai_fix_branch_prefix is True
        assert policy.protected_branches == PROTECTED_BRANCH_NAMES
        assert policy.protected_branch_keys == (
            "main",
            "master",
            "develop",
            "trunk",
        )

    def test_the_default_protected_names_are_the_four_standard_branches(self):
        assert PROTECTED_BRANCH_NAMES == ("main", "master", "develop", "trunk")

    def test_a_custom_protected_set_is_honoured(self):
        policy = ProtectedBranchPolicy(
            protected_branches=("release/1.0", "hotfix")
        )
        assert policy.is_valid is True
        assert policy.protected_branch_keys == ("release/1.0", "hotfix")
        assert status_of("hotfix") is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
        for candidate in ("hotfix", "release/1.0"):
            assert (
                decide(candidate, protected=("release/1.0", "hotfix")).status
                is BranchProtectionStatus.PROTECTED_BRANCH
            )

    def test_a_sequence_is_read_without_being_mutated(self):
        configured = ["release/1.0", "hotfix"]
        policy = ProtectedBranchPolicy(protected_branches=configured)
        assert policy.is_valid is True
        assert policy.protected_branch_keys == ("release/1.0", "hotfix")
        assert configured == ["release/1.0", "hotfix"]

    def test_an_empty_protected_set_is_valid_but_strict(self):
        policy = ProtectedBranchPolicy(protected_branches=())
        assert policy.is_valid is True
        assert policy.protected_branch_keys == ()
        assert policy.refusal_reason is None
        # The default branch is refused although no name is protected ...
        assert (
            status_of("main", protected=())
            is BranchProtectionStatus.DEFAULT_BRANCH
        )
        # ... and "main" is not accepted merely because the named set is empty.
        assert (
            status_of("main", default="develop", protected=())
            is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
        )

    def test_the_default_branch_rule_cannot_be_configured_away(self):
        # Even the most permissive explicit policy refuses the default branch, so
        # no configuration turns T26 into "always allowed".
        assert (
            decide(
                "main",
                default="develop",
                protected=(),
                require_prefix=False,
            ).is_allowed
            is True
        )
        for candidate in ("develop", "DeVeLoP", "deVeLop"):
            evaluation = decide(
                candidate,
                default="develop",
                protected=(),
                require_prefix=False,
            )
            assert evaluation.status is BranchProtectionStatus.DEFAULT_BRANCH

    @pytest.mark.parametrize("protected", INVALID_PROTECTED_SETS)
    def test_an_unusable_protected_set_refuses_everything(self, protected):
        evaluation = decide(AGENT_BRANCH, protected=protected)
        assert evaluation.status is BranchProtectionStatus.INVALID_POLICY
        assert evaluation.is_allowed is False
        assert evaluation.policy.is_valid is False
        assert evaluation.policy.protected_branch_keys == ()
        assert evaluation.reason == evaluation.policy.refusal_reason
        assert evaluation.reason

    def test_none_never_means_protect_nothing(self):
        policy = ProtectedBranchPolicy(protected_branches=None)
        assert policy.is_valid is False
        assert "None never means" in policy.refusal_reason
        assert policy.protected_branch_keys == ()

    def test_a_malformed_entry_names_its_position(self):
        policy = ProtectedBranchPolicy(protected_branches=("develop", "a."))
        assert policy.is_valid is False
        assert "protected branch entry 2" in policy.refusal_reason

    def test_a_duplicate_entry_is_refused_instead_of_deduplicated(self):
        policy = ProtectedBranchPolicy(protected_branches=("main", "MAIN"))
        assert policy.is_valid is False
        assert "repeats 'main'" in policy.refusal_reason

    def test_the_protected_set_is_validated_before_the_flag(self):
        policy = ProtectedBranchPolicy(
            protected_branches=None,
            require_ai_fix_branch_prefix="yes",
        )
        assert "protected_branches" in policy.refusal_reason

    @pytest.mark.parametrize("flag", INVALID_FLAGS)
    def test_an_unusable_flag_refuses_everything(self, flag):
        evaluation = decide(AGENT_BRANCH, require_prefix=flag)
        assert evaluation.status is BranchProtectionStatus.INVALID_POLICY
        assert evaluation.is_allowed is False
        assert "require_ai_fix_branch_prefix" in evaluation.reason

    def test_the_flag_must_be_a_real_bool(self):
        assert ProtectedBranchPolicy().require_ai_fix_branch_prefix is True
        assert (
            ProtectedBranchPolicy(require_ai_fix_branch_prefix=False).is_valid
            is True
        )

    def test_a_policy_is_frozen(self):
        policy = ProtectedBranchPolicy()
        with pytest.raises(FrozenInstanceError):
            policy.protected_branches = ()
        with pytest.raises(FrozenInstanceError):
            policy.require_ai_fix_branch_prefix = False

    def test_the_namespace_is_the_t07_constant_and_is_not_configurable(self):
        assert AI_FIX_BRANCH_PREFIX == AGENT_BRANCH_PREFIX == "ai/sonar-fix"
        assert list(ProtectedBranchPolicy.__dataclass_fields__) == [
            "protected_branches",
            "require_ai_fix_branch_prefix",
        ]
        with pytest.raises(TypeError):
            ProtectedBranchPolicy(required_branch_prefix="feature")


#: Candidate values that are valid Git branch names (whatever they then decide).
VALID_CANDIDATES = (
    f"{AI_FIX_BRANCH_PREFIX}/x",
    f"{AI_FIX_BRANCH_PREFIX}/a/b",
    "release/1.0",
    "feature/x",
    "a/b/c-d_e",
    "origin/main",
)


class TestCandidateValidation:
    """An unusable branch identity is refused, never repaired."""

    @pytest.mark.parametrize("candidate", INVALID_CANDIDATES)
    def test_an_unusable_candidate_is_refused(self, candidate):
        evaluation = decide(candidate)
        assert evaluation.status is BranchProtectionStatus.INVALID_BRANCH
        assert evaluation.is_allowed is False
        assert evaluation.candidate_branch is None
        assert evaluation.branch_is_protected is False
        assert evaluation.branch_is_default is False
        assert evaluation.branch_in_ai_namespace is False
        assert evaluation.reason

    @pytest.mark.parametrize("candidate", INVALID_CANDIDATES)
    def test_an_unusable_candidate_is_refused_whatever_else_is_wrong(
        self, candidate
    ):
        for kwargs in (
            {"protected": ()},
            {"default": None},
            {"default": "main", "protected": ()},
            {"require_prefix": False},
        ):
            assert (
                decide(candidate, **kwargs).status
                is BranchProtectionStatus.INVALID_BRANCH
            )

    @pytest.mark.parametrize("candidate", VALID_CANDIDATES)
    def test_a_valid_candidate_is_classified_not_rejected(self, candidate):
        evaluation = decide(candidate)
        assert evaluation.status is not BranchProtectionStatus.INVALID_BRANCH
        assert evaluation.candidate_branch == candidate

    def test_an_invalid_candidate_is_never_echoed(self):
        decoy = "squ_do_not_publish_me main"
        evaluation = decide(decoy)
        assert evaluation.status is BranchProtectionStatus.INVALID_BRANCH
        assert evaluation.candidate_branch is None
        assert "squ_do_not_publish_me" not in json.dumps(evaluation.as_dict())

    def test_a_whitespace_variant_is_refused_not_trimmed(self):
        # A trimming policy would have answered PROTECTED_BRANCH here; T26
        # refuses the identity itself instead, and never rewrites the name.
        for candidate in (" main", "main ", "  main  ", "\tmain", "main\n"):
            evaluation = decide(candidate)
            assert evaluation.status is BranchProtectionStatus.INVALID_BRANCH
            assert evaluation.candidate_branch is None

    def test_a_case_variant_of_a_protected_name_is_still_protected(self):
        for candidate in ("MAIN", "Main", "mAiN", "MASTER", "Develop", "TRUNK"):
            assert (
                status_of(candidate)
                is BranchProtectionStatus.PROTECTED_BRANCH
            )

    def test_a_name_that_merely_contains_a_protected_name_is_not_protected(self):
        # No substring, suffix or prefix matching: only the whole name counts.
        for candidate in ("my-main", "main-x", "x/main", "maintenance"):
            assert (
                status_of(candidate)
                is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
            )

    def test_every_name_t07_creates_is_accepted(self):
        for key in ("AX1abcDeF2", "issue / 1:2 [x]", "back\\slash", "12345"):
            name = make_agent_branch_name(key, timestamp="20260909-101530")
            evaluation = decide(name)
            assert evaluation.status is BranchProtectionStatus.ALLOWED
            assert evaluation.branch_in_ai_namespace is True
            assert evaluation.is_allowed is True


class TestProtectedBranches:
    """The protected names are refused in every spelling, and only by name."""

    @pytest.mark.parametrize("name", PROTECTED_BRANCH_NAMES)
    def test_every_default_protected_name_is_refused(self, name):
        evaluation = decide(name)
        assert evaluation.status is BranchProtectionStatus.PROTECTED_BRANCH
        assert evaluation.branch_is_protected is True
        assert evaluation.is_allowed is False

    @pytest.mark.parametrize("name", PROTECTED_BRANCH_NAMES)
    def test_no_case_variant_of_a_protected_name_is_ever_allowed(self, name):
        for variant in (name.upper(), name.title(), name.swapcase()):
            evaluation = decide(variant)
            assert evaluation.is_allowed is False
            assert (
                evaluation.status is BranchProtectionStatus.PROTECTED_BRANCH
            )

    @pytest.mark.parametrize("name", PROTECTED_BRANCH_NAMES)
    def test_no_whitespace_variant_of_a_protected_name_is_ever_allowed(
        self, name
    ):
        for variant in (
            f" {name}",
            f"{name} ",
            f" {name} ",
            f"\t{name}",
            f"{name}\n",
        ):
            evaluation = decide(variant)
            assert evaluation.is_allowed is False
            assert evaluation.status is BranchProtectionStatus.INVALID_BRANCH

    def test_a_protected_name_with_a_slash_is_protected(self):
        for candidate in ("release/1.0", "Release/1.0", "RELEASE/1.0"):
            assert (
                decide(candidate, protected=("release/1.0",)).status
                is BranchProtectionStatus.PROTECTED_BRANCH
            )

    def test_the_protected_set_is_a_list_of_names_not_patterns(self):
        for candidate in ("release/1", "release/1.0.1", "1.0", "release"):
            assert (
                decide(candidate, protected=("release/1.0",)).status
                is not BranchProtectionStatus.PROTECTED_BRANCH
            )

    def test_an_unusable_protected_set_never_falls_back_to_allowing(self):
        for protected in (None, (), {"main"}):
            evaluation = decide("main", protected=protected)
            assert evaluation.is_allowed is False


class TestDefaultBranch:
    """The repository's own default branch is refused even without a name."""

    @pytest.mark.parametrize(
        "default", ("main", "master", "release/2.0", "trunk-base")
    )
    def test_the_default_branch_is_refused_whatever_it_is_named(self, default):
        evaluation = decide(default, default=default, protected=())
        assert evaluation.status is BranchProtectionStatus.DEFAULT_BRANCH
        assert evaluation.branch_is_default is True
        assert evaluation.branch_is_protected is False
        assert evaluation.is_allowed is False

    def test_a_candidate_that_differs_from_the_default_is_not_refused(self):
        assert (
            decide(AGENT_BRANCH, default="main").status
            is BranchProtectionStatus.ALLOWED
        )
        assert (
            decide(
                f"{AI_FIX_BRANCH_PREFIX}/x",
                default="release/2.0",
                protected=(),
                require_prefix=False,
            ).is_allowed
            is True
        )

    def test_the_default_comparison_is_case_folded(self):
        for candidate, default in (
            ("MAIN", "main"),
            ("Main", "MAIN"),
            ("main", "Main"),
            ("Develop", "develop"),
            ("RELEASE/2.0", "release/2.0"),
        ):
            evaluation = decide(candidate, default=default, protected=())
            assert evaluation.status is BranchProtectionStatus.DEFAULT_BRANCH

    def test_the_default_comparison_is_never_a_substring_or_suffix_match(self):
        for candidate in ("my-main", "x/main", "maintenance", "origin/main"):
            assert (
                decide(candidate, default="main", protected=()).status
                is not BranchProtectionStatus.DEFAULT_BRANCH
            )

    @pytest.mark.parametrize(
        "default",
        (
            None,
            "",
            "   ",
            5,
            1.5,
            True,
            b"main",
            "a.",
            "a..b",
            "HEAD",
            "refs/heads/main",
            "REFS/heads/main",
            " main",
            "main ",
            "\tmain",
        ),
    )
    def test_an_unusable_default_branch_refuses_everything(self, default):
        evaluation = decide(AGENT_BRANCH, default=default)
        assert evaluation.status is BranchProtectionStatus.MISSING_DEFAULT_BRANCH
        assert evaluation.is_allowed is False
        assert evaluation.default_branch is None
        assert evaluation.branch_is_default is False
        assert "default_branch" in evaluation.reason

    def test_a_default_branch_that_is_itself_protected_is_refused_as_protected(
        self,
    ):
        evaluation = decide("develop", default="develop")
        assert evaluation.status is BranchProtectionStatus.PROTECTED_BRANCH
        assert evaluation.branch_is_protected is True
        assert evaluation.branch_is_default is True

    def test_a_missing_default_branch_takes_precedence_over_protected(self):
        # The documented precedence decision: the missing precondition is
        # reported, and the branch is still refused.
        evaluation = decide("main", default=None)
        assert (
            evaluation.status
            is BranchProtectionStatus.MISSING_DEFAULT_BRANCH
        )
        assert evaluation.branch_is_protected is True
        assert evaluation.is_allowed is False

    def test_a_default_branch_outside_the_protected_set_is_still_refused(self):
        for default in ("release/1.0", "project-default", "stable"):
            assert (
                decide(default, default=default, protected=("main",)).status
                is BranchProtectionStatus.DEFAULT_BRANCH
            )


class TestAiNamespace:
    """The T07 namespace is required by default and never widened by T26."""

    def test_a_valid_ai_branch_is_allowed(self):
        evaluation = decide(AGENT_BRANCH)
        assert evaluation.status is BranchProtectionStatus.ALLOWED
        assert evaluation.is_allowed is True
        assert evaluation.branch_in_ai_namespace is True
        assert evaluation.candidate_branch == AGENT_BRANCH
        assert evaluation.default_branch == "main"

    def test_every_other_namespace_is_refused(self):
        for candidate in (
            "feature/foo",
            "bugfix/foo",
            "user/foo",
            "ai/other/foo",
            "ai/sonar-fix2/foo",
            "sonar-fix/foo",
            "fixes/foo",
            "release/1.0",
        ):
            evaluation = decide(candidate, default="main")
            assert (
                evaluation.status
                is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
            )
            assert evaluation.is_allowed is False
            assert evaluation.branch_in_ai_namespace is False

    def test_the_bare_prefix_is_not_inside_the_namespace(self):
        evaluation = decide(AI_FIX_BRANCH_PREFIX)
        assert (
            evaluation.status is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
        )
        assert evaluation.branch_in_ai_namespace is False

    def test_a_nested_ai_branch_is_allowed_when_git_valid(self):
        evaluation = decide(f"{AI_FIX_BRANCH_PREFIX}/a/b")
        assert evaluation.status is BranchProtectionStatus.ALLOWED
        assert evaluation.branch_in_ai_namespace is True

    def test_a_malformed_prefix_like_branch_is_an_invalid_identity(self):
        for candidate in (
            f"{AI_FIX_BRANCH_PREFIX}/",
            f"{AI_FIX_BRANCH_PREFIX}//x",
            f"{AI_FIX_BRANCH_PREFIX}/x.",
            f"{AI_FIX_BRANCH_PREFIX}/x.lock",
            f"{AI_FIX_BRANCH_PREFIX}/../x",
        ):
            assert (
                status_of(candidate) is BranchProtectionStatus.INVALID_BRANCH
            )

    def test_the_namespace_match_is_case_sensitive_like_git_refs(self):
        for candidate in (
            "AI/sonar-fix/x",
            "Ai/Sonar-Fix/x",
            "ai/SONAR-FIX/x",
        ):
            evaluation = decide(candidate)
            assert (
                evaluation.status
                is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
            )
            assert evaluation.branch_in_ai_namespace is False

    def test_disabling_the_requirement_allows_other_valid_branches(self):
        evaluation = decide("feature/foo", require_prefix=False)
        assert evaluation.status is BranchProtectionStatus.ALLOWED
        assert evaluation.is_allowed is True
        assert evaluation.branch_in_ai_namespace is False
        assert "disabled" in evaluation.reason

    def test_disabling_the_requirement_disables_nothing_else(self):
        for candidate in ("main", "develop", "MAIN"):
            assert (
                decide(candidate, require_prefix=False).status
                is BranchProtectionStatus.PROTECTED_BRANCH
            )
        assert (
            decide(
                "release/1.0", default="release/1.0", require_prefix=False
            ).status
            is BranchProtectionStatus.DEFAULT_BRANCH
        )
        assert (
            decide("a.", require_prefix=False).status
            is BranchProtectionStatus.INVALID_BRANCH
        )
        assert (
            decide(AGENT_BRANCH, default=None, require_prefix=False).status
            is BranchProtectionStatus.MISSING_DEFAULT_BRANCH
        )
        assert (
            decide(AGENT_BRANCH, protected=None, require_prefix=False).status
            is BranchProtectionStatus.INVALID_POLICY
        )

    def test_the_namespace_decision_never_widens_t20s_agent_branch_rule(self):
        t20 = commit_policy.CommitPolicyConfig()
        t21 = push_policy.PushPolicyConfig()
        for candidate in (*VALID_CANDIDATES, AGENT_BRANCH, AI_FIX_BRANCH_PREFIX):
            if decide(candidate).branch_in_ai_namespace:
                assert t20.is_agent_branch(candidate) is True
                assert t21.is_agent_branch(candidate) is True


class TestPrecedenceAndStatuses:
    """Every status is reachable and overlapping refusals are deterministic."""

    def test_the_precedence_is_the_documented_order(self):
        assert tuple(PRECEDENCE) == (
            BranchProtectionStatus.INVALID_POLICY,
            BranchProtectionStatus.INVALID_BRANCH,
            BranchProtectionStatus.MISSING_DEFAULT_BRANCH,
            BranchProtectionStatus.PROTECTED_BRANCH,
            BranchProtectionStatus.DEFAULT_BRANCH,
            BranchProtectionStatus.WRONG_BRANCH_NAMESPACE,
            BranchProtectionStatus.ALLOWED,
        )
        assert set(PRECEDENCE) == set(BranchProtectionStatus)
        assert len(PRECEDENCE) == len(BranchProtectionStatus)

    def test_the_allowing_statuses_are_exactly_the_allowed_one(self):
        assert ALLOWED_STATUSES == (BranchProtectionStatus.ALLOWED,)
        assert set(ALLOWED_STATUSES) < set(PRECEDENCE)

    @pytest.mark.parametrize("status", list(BranchProtectionStatus))
    def test_every_status_is_reachable(self, status):
        assert decide(**STATUS_INPUTS[status]).status is status

    def test_a_conflict_is_decided_by_the_earliest_status(self):
        conflicts = (
            # invalid policy beats an invalid candidate and a missing default
            (
                BranchProtectionStatus.INVALID_POLICY,
                None,
                {"default": None, "protected": None},
            ),
            # invalid policy beats a protected candidate
            (BranchProtectionStatus.INVALID_POLICY, "main", {"protected": None}),
            # an invalid candidate beats a missing default branch
            (BranchProtectionStatus.INVALID_BRANCH, "main/", {"default": None}),
            # an invalid candidate beats a protected-looking name
            (BranchProtectionStatus.INVALID_BRANCH, " main", {"default": None}),
            # a missing default branch beats the protected classification
            (
                BranchProtectionStatus.MISSING_DEFAULT_BRANCH,
                "main",
                {"default": None},
            ),
            # a missing default branch beats the namespace classification
            (
                BranchProtectionStatus.MISSING_DEFAULT_BRANCH,
                "feature/x",
                {"default": None},
            ),
            # protected beats the default-branch classification
            (
                BranchProtectionStatus.PROTECTED_BRANCH,
                "main",
                {"default": "main"},
            ),
            (
                BranchProtectionStatus.PROTECTED_BRANCH,
                "develop",
                {"default": "develop"},
            ),
            # protected beats the namespace classification
            (BranchProtectionStatus.PROTECTED_BRANCH, "develop", {}),
            # the default branch beats the namespace classification
            (
                BranchProtectionStatus.DEFAULT_BRANCH,
                "feature/x",
                {"default": "feature/x"},
            ),
            # nothing applies: allowed
            (BranchProtectionStatus.ALLOWED, AGENT_BRANCH, {}),
        )
        for expected, candidate, kwargs in conflicts:
            assert decide(candidate, **kwargs).status is expected

    def test_the_status_is_always_one_catalogued_rule(self):
        candidates = (
            None,
            "",
            "main",
            "MAIN",
            "main ",
            "main/",
            "a.",
            "a..b",
            "refs/heads/main",
            "develop",
            "feature/x",
            "release/1.0",
            AGENT_BRANCH,
        )
        defaults = (None, "main", "release/1.0", "refs/heads/main", " main")
        policies = (
            (PROTECTED_BRANCH_NAMES, True),
            ((), True),
            (("release/1.0",), True),
            (PROTECTED_BRANCH_NAMES, False),
            (None, True),
            (("main", "MAIN"), True),
            (PROTECTED_BRANCH_NAMES, "yes"),
        )
        for candidate in candidates:
            for default in defaults:
                for protected, flag in policies:
                    evaluation = decide(
                        candidate,
                        default=default,
                        protected=protected,
                        require_prefix=flag,
                    )
                    assert evaluation.status in PRECEDENCE
                    assert evaluation.is_allowed == (
                        evaluation.status is BranchProtectionStatus.ALLOWED
                    )
                    if evaluation.is_allowed:
                        assert evaluation.default_branch is not None
                        assert evaluation.candidate_branch is not None
                        assert evaluation.branch_is_default is False
                        assert evaluation.branch_is_protected is False
                    else:
                        assert (
                            evaluation.decision
                            is BranchProtectionDecision.REFUSE
                        )
                    if evaluation.status is (
                        BranchProtectionStatus.PROTECTED_BRANCH
                    ):
                        assert evaluation.branch_is_protected is True
                    if evaluation.status is (
                        BranchProtectionStatus.DEFAULT_BRANCH
                    ):
                        assert evaluation.branch_is_default is True
                        assert evaluation.branch_is_protected is False
                    if evaluation.status is (
                        BranchProtectionStatus.MISSING_DEFAULT_BRANCH
                    ):
                        assert evaluation.default_branch is None
                    if evaluation.status is (
                        BranchProtectionStatus.INVALID_BRANCH
                    ):
                        assert evaluation.candidate_branch is None


class TestResultInvariants:
    """A refusal never reports allowed, and an ALLOWED result never refuses."""

    def test_the_allowed_flag_is_derived_and_never_stored(self):
        fields = BranchProtectionEvaluation.__dataclass_fields__
        assert "is_allowed" not in fields
        assert isinstance(
            inspect.getattr_static(BranchProtectionEvaluation, "is_allowed"),
            property,
        )

    @pytest.mark.parametrize("status", list(BranchProtectionStatus))
    def test_the_allowed_flag_is_exactly_the_allowed_status(self, status):
        evaluation = decide(**STATUS_INPUTS[status])
        allowed = status is BranchProtectionStatus.ALLOWED
        assert evaluation.is_allowed == allowed
        assert (
            evaluation.decision is BranchProtectionDecision.ALLOW
        ) == allowed
        assert (
            evaluation.decision is BranchProtectionDecision.REFUSE
        ) == (not allowed)

    @pytest.mark.parametrize("status", list(BranchProtectionStatus))
    def test_every_verdict_explains_itself_deterministically(self, status):
        evaluation = decide(**STATUS_INPUTS[status])
        assert evaluation.policy_version == POLICY_VERSION
        assert evaluation.reason
        assert evaluation.reasons[0] == evaluation.reason
        assert all(item for item in evaluation.reasons)
        assert evaluation.reasons == decide(**STATUS_INPUTS[status]).reasons

    def test_no_refusal_reports_allowed(self):
        for status, kwargs in STATUS_INPUTS.items():
            if status is BranchProtectionStatus.ALLOWED:
                continue
            evaluation = decide(**kwargs)
            assert evaluation.is_allowed is False
            payload = evaluation.as_dict()
            assert payload["is_allowed"] is False
            assert payload["status"] == status.value
            assert payload["decision"] == "refuse"

    def test_an_allowed_result_never_carries_a_refusal_status(self):
        evaluation = decide(AGENT_BRANCH)
        assert evaluation.status is BranchProtectionStatus.ALLOWED
        payload = evaluation.as_dict()
        assert payload["status"] == "allowed"
        assert payload["decision"] == "allow"
        assert payload["is_allowed"] is True

    def test_allowed_implies_not_protected_not_default_and_in_namespace(self):
        for candidate in (
            AGENT_BRANCH,
            "feature/x",
            "release/1.0",
            "origin/main",
            "a/b/c",
            "main",
        ):
            for require_prefix in (True, False):
                evaluation = decide(
                    candidate, default="main", require_prefix=require_prefix
                )
                if evaluation.is_allowed:
                    assert evaluation.branch_is_protected is False
                    assert evaluation.branch_is_default is False
                    assert (
                        evaluation.branch_in_ai_namespace
                        or require_prefix is False
                    )

    def test_a_refusal_is_never_a_silent_repair(self):
        for candidate in INVALID_CANDIDATES:
            evaluation = decide(candidate)
            assert evaluation.candidate_branch is None
            assert evaluation.as_dict()["candidate_branch"] is None


class TestDeterminismAndImmutability:
    """The verdict is a pure function of its inputs and nothing is mutated."""

    def test_the_same_inputs_always_produce_an_equal_verdict(self):
        first = decide("develop", default="main")
        second = decide("develop", default="main")
        assert first == second
        assert first.as_dict() == second.as_dict()

    def test_equal_records_are_equal_and_hashable(self):
        assert ProtectedBranchPolicy() == ProtectedBranchPolicy()
        assert hash(ProtectedBranchPolicy()) == hash(ProtectedBranchPolicy())
        first = BranchProtectionInput("main", "main")
        second = BranchProtectionInput("main", "main")
        assert first == second
        assert hash(first) == hash(second)
        assert ProtectedBranchPolicy() != ProtectedBranchPolicy(
            require_ai_fix_branch_prefix=False
        )

    def test_repeated_evaluation_of_the_same_pair_is_repeatable(self):
        policy = ProtectedBranchPolicy(protected_branches=("develop",))
        branch = BranchProtectionInput(
            candidate_branch="develop", default_branch="main"
        )
        payloads = {
            json.dumps(
                evaluate_branch_protection(
                    policy=policy, branch=branch
                ).as_dict()
            )
            for _ in range(50)
        }
        assert len(payloads) == 1

    def test_the_caller_values_are_never_mutated(self):
        configured = ["main", "develop"]
        policy = ProtectedBranchPolicy(protected_branches=configured)
        branch = BranchProtectionInput(
            candidate_branch="develop", default_branch="main"
        )
        evaluate_branch_protection(policy=policy, branch=branch)
        assert configured == ["main", "develop"]
        assert policy.protected_branches == ["main", "develop"]
        assert branch.candidate_branch == "develop"
        assert branch.default_branch == "main"

    def test_every_record_is_frozen(self):
        policy = ProtectedBranchPolicy()
        branch = BranchProtectionInput()
        evaluation = decide(AGENT_BRANCH)
        attempts = (
            (policy, "protected_branches", ()),
            (policy, "require_ai_fix_branch_prefix", False),
            (branch, "candidate_branch", "main"),
            (branch, "default_branch", "develop"),
            (evaluation, "status", BranchProtectionStatus.ALLOWED),
            (evaluation, "reason", "rewritten"),
            (evaluation, "candidate_branch", "main"),
            (evaluation, "branch_is_protected", True),
        )
        for record, field, value in attempts:
            with pytest.raises(FrozenInstanceError):
                setattr(record, field, value)

    def test_the_module_keeps_no_state_between_evaluations(self):
        evaluation = decide("develop")
        for _ in range(20):
            assert decide("develop") == evaluation
            assert (
                decide(AGENT_BRANCH).status
                is BranchProtectionStatus.ALLOWED
            )
        for kwargs in STATUS_INPUTS.values():
            decide(**kwargs)
        assert decide(AGENT_BRANCH).status is BranchProtectionStatus.ALLOWED
        assert (
            decide("main").status is BranchProtectionStatus.PROTECTED_BRANCH
        )


class TestSerialization:
    """The serialized verdict is deterministic, JSON-safe and secret-free."""

    def test_the_verdict_serializes_deterministically(self):
        evaluation = decide(AGENT_BRANCH)
        payload = evaluation.as_dict()
        assert json.dumps(payload) == json.dumps(evaluation.as_dict())
        assert json.loads(json.dumps(payload)) == payload

    def test_the_allowed_verdict_view_is_exact(self):
        reason = (
            f"'{AGENT_BRANCH}' is not a protected branch and is not the "
            "default branch 'main': it is inside the AI-fix branch namespace "
            f"'{AI_FIX_BRANCH_PREFIX}/'."
        )
        assert decide(AGENT_BRANCH).as_dict() == {
            "policy_version": POLICY_VERSION,
            "status": "allowed",
            "decision": "allow",
            "is_allowed": True,
            "candidate_branch": AGENT_BRANCH,
            "default_branch": "main",
            "branch_in_ai_namespace": True,
            "branch_is_default": False,
            "branch_is_protected": False,
            "reason": reason,
            "reasons": [
                reason,
                "T26 does not prove the branch exists or is checked out: the "
                "caller must still enforce this verdict (see the trust "
                "boundary).",
            ],
            "policy": {
                "policy_version": POLICY_VERSION,
                "protected_branches": ["main", "master", "develop", "trunk"],
                "require_ai_fix_branch_prefix": True,
                "ai_fix_branch_prefix": AI_FIX_BRANCH_PREFIX,
                "is_valid": True,
                "refusal_reason": None,
            },
        }

    def test_the_keys_are_always_the_same_and_in_the_same_order(self):
        keys = tuple(decide(AGENT_BRANCH).as_dict())
        assert keys == (
            "policy_version",
            "status",
            "decision",
            "is_allowed",
            "candidate_branch",
            "default_branch",
            "branch_in_ai_namespace",
            "branch_is_default",
            "branch_is_protected",
            "reason",
            "reasons",
            "policy",
        )
        for kwargs in STATUS_INPUTS.values():
            assert tuple(decide(**kwargs).as_dict()) == keys

    def test_the_serialized_view_is_json_native(self):
        for kwargs in STATUS_INPUTS.values():
            payload = decide(**kwargs).as_dict()
            assert json.loads(json.dumps(payload)) == payload
            for value in payload.values():
                assert value is None or isinstance(
                    value, (str, bool, int, list, dict)
                )

    def test_the_policy_view_reports_only_validated_values(self):
        assert ProtectedBranchPolicy(
            protected_branches=("main",)
        ).as_dict() == {
            "policy_version": POLICY_VERSION,
            "protected_branches": ["main"],
            "require_ai_fix_branch_prefix": True,
            "ai_fix_branch_prefix": AI_FIX_BRANCH_PREFIX,
            "is_valid": True,
            "refusal_reason": None,
        }
        empty = ProtectedBranchPolicy(protected_branches=()).as_dict()
        assert empty["protected_branches"] == []
        assert empty["is_valid"] is True
        duplicate = ProtectedBranchPolicy(
            protected_branches=("main", "MAIN")
        ).as_dict()
        assert duplicate["protected_branches"] is None
        assert duplicate["is_valid"] is False

    def test_the_input_view_reports_only_validated_names(self):
        assert BranchProtectionInput("main", "develop").as_dict() == {
            "candidate_branch": "main",
            "default_branch": "develop",
            "is_usable_candidate": True,
            "is_usable_default_branch": True,
        }
        assert BranchProtectionInput("main ", None).as_dict() == {
            "candidate_branch": None,
            "default_branch": None,
            "is_usable_candidate": False,
            "is_usable_default_branch": False,
        }

    def test_an_unusable_value_is_never_echoed(self):
        for secret in ("squ_do_not_publish_me", "ghp_do_not_publish_me"):
            policy = ProtectedBranchPolicy(protected_branches=secret)
            branch = BranchProtectionInput(
                candidate_branch=f"{secret} main",
                default_branch=f"{secret} main",
            )
            evaluation = evaluate_branch_protection(
                policy=policy, branch=branch
            )
            serialized = json.dumps(evaluation.as_dict())
            assert secret not in serialized
            assert policy.as_dict()["protected_branches"] is None
            assert evaluation.as_dict()["candidate_branch"] is None
            assert evaluation.as_dict()["default_branch"] is None

    def test_an_unusable_flag_is_never_echoed(self):
        policy = ProtectedBranchPolicy(
            require_ai_fix_branch_prefix="squ_do_not_publish_me"
        )
        payload = policy.as_dict()
        assert payload["require_ai_fix_branch_prefix"] is None
        assert "squ_do_not_publish_me" not in json.dumps(payload)
        assert "bool" in payload["refusal_reason"]

    def test_the_input_flags_are_pinned(self):
        assert BranchProtectionInput().is_usable_candidate is False
        assert BranchProtectionInput().is_usable_default_branch is False
        assert (
            BranchProtectionInput("main", "develop").is_usable_candidate
            is True
        )
        assert (
            BranchProtectionInput(
                "main", "develop"
            ).is_usable_default_branch
            is True
        )


class TestHelpers:
    """The private readers accept exactly what the contract documents."""

    @pytest.mark.parametrize(
        "value, expected",
        (
            ("main", "main"),
            ("a/b", "a/b"),
            (AGENT_BRANCH, AGENT_BRANCH),
            (None, None),
            ("", None),
            ("   ", None),
            (" main", None),
            ("main ", None),
            ("\tmain", None),
            ("a..b", None),
            ("a.", None),
            ("a.lock", None),
            ("a//b", None),
            ("-x", None),
            ("HEAD", None),
            ("@", None),
            ("refs/heads/main", None),
            ("REFS/heads/main", None),
            (123, None),
            (True, None),
            (1.5, None),
            (b"main", None),
            ({"main"}, None),
        ),
    )
    def test_name(self, value, expected):
        assert module._name(value, "candidate_branch") == expected

    def test_name_requires_a_label(self):
        with pytest.raises(TypeError):
            module._name("main")

    @pytest.mark.parametrize(
        "value, expected",
        (
            ("main", "main"),
            ("MAIN", "main"),
            ("Main", "main"),
            ("  main  ", "main"),
            ("a/B", "a/b"),
        ),
    )
    def test_key(self, value, expected):
        assert module._key(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            (("main", "MASTER"), ("main", "master")),
            ((), ()),
            (["a"], ("a",)),
        ),
    )
    def test_normalized(self, value, expected):
        assert module._normalized(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        (
            (None, "candidate_branch is missing."),
            (5, "candidate_branch must be a string, not int."),
            (True, "candidate_branch must be a string, not bool."),
            ("main", ""),
            (AGENT_BRANCH, ""),
        ),
    )
    def test_problem(self, value, expected):
        assert module._problem(value, "candidate_branch") == expected

    def test_the_problem_messages_are_rule_based(self):
        assert "refs/" in module._problem("refs/heads/main", "candidate_branch")
        assert "not a valid Git branch name" in module._problem(
            "a.", "candidate_branch"
        )
        assert "not a valid Git branch name" in module._problem(
            "", "candidate_branch"
        )
        # A forbidden character can only appear through a repr, never verbatim.
        assert repr("\x00") in module._problem("a\x00b", "candidate_branch")
        assert "a\x00b" not in module._problem("a\x00b", "candidate_branch")

    def test_policy_problem_messages(self):
        assert module._policy_problem(ProtectedBranchPolicy()) is None
        assert module._policy_problem(
            ProtectedBranchPolicy(protected_branches=())
        ) is None
        assert "None never means" in module._policy_problem(
            ProtectedBranchPolicy(protected_branches=None)
        )
        for value, kind in (
            ("main", "str"),
            (b"main", "bytes"),
            ({"main"}, "set"),
            (frozenset({"main"}), "frozenset"),
            ({"main": 1}, "dict"),
            (5, "int"),
            (1.5, "float"),
            (object(), "object"),
        ):
            reason = module._policy_problem(
                ProtectedBranchPolicy(protected_branches=value)
            )
            assert f"not {kind}" in reason
        assert "protected branch entry 1" in module._policy_problem(
            ProtectedBranchPolicy(protected_branches=(None,))
        )
        assert "protected branch entry 2" in module._policy_problem(
            ProtectedBranchPolicy(
                protected_branches=("develop", "refs/heads/main")
            )
        )
        assert "repeats 'main'" in module._policy_problem(
            ProtectedBranchPolicy(protected_branches=("main", "MAIN"))
        )
        assert "must be a bool" in module._policy_problem(
            ProtectedBranchPolicy(require_ai_fix_branch_prefix=1)
        )


class TestCompatibilityWithT20AndT21:
    """T26 mirrors T20/T21's branch rules and never widens them."""

    def test_the_protected_names_are_the_same_as_t20_and_t21(self):
        assert PROTECTED_BRANCH_NAMES == commit_policy.PROTECTED_BRANCH_NAMES
        assert PROTECTED_BRANCH_NAMES == (
            commit_policy.CommitPolicyConfig().protected_branches
        )
        assert PROTECTED_BRANCH_NAMES == (
            push_policy.PushPolicyConfig().protected_branches
        )

    def test_the_namespace_is_the_same_as_t07_t20_and_t21(self):
        assert AI_FIX_BRANCH_PREFIX == branch_naming.AGENT_BRANCH_PREFIX
        assert AI_FIX_BRANCH_PREFIX == commit_policy.DEFAULT_AGENT_BRANCH_PREFIX
        assert AI_FIX_BRANCH_PREFIX == (
            commit_policy.CommitPolicyConfig().required_branch_prefix
        )
        assert AI_FIX_BRANCH_PREFIX == (
            push_policy.PushPolicyConfig().required_branch_prefix
        )

    def test_a_t26_allow_never_widens_a_t20_allowed_branch(self):
        t20 = commit_policy.CommitPolicyConfig(default_branch="main")
        names = (
            "main",
            "master",
            "develop",
            "trunk",
            "MAIN",
            "Develop",
            "feature/x",
            "ai/sonar-fix/x",
            "ai/sonar-fix",
            AGENT_BRANCH,
            "AI/sonar-fix/x",
            "release/1.0",
            "",
            "main ",
            "a.",
        )
        for name in names:
            t20_allowed = (
                not t20.is_protected_branch(name) and t20.is_agent_branch(name)
            )
            t26_allowed = decide(name, default="main").is_allowed
            assert not (t26_allowed and not t20_allowed)

    def test_t26_allows_exactly_the_agent_branches_t20_allows(self):
        t20 = commit_policy.CommitPolicyConfig(default_branch="main")
        names = ("ai/sonar-fix/x", AGENT_BRANCH, f"{AI_FIX_BRANCH_PREFIX}/a/b")
        for name in names:
            assert decide(name, default="main").is_allowed is True
            assert t20.is_protected_branch(name) is False
            assert t20.is_agent_branch(name) is True

    def test_the_documented_difference_a_missing_default_branch(self):
        # T20 reads ``default_branch=None`` as "no extra branch is protected"
        # (its executor resolves the real default branch first); T26 refuses.
        # T26 is the stricter of the two, never the looser.
        t20 = commit_policy.CommitPolicyConfig(
            protected_branches=(), default_branch=None
        )
        assert t20.is_protected_branch("release/1.0") is False
        for candidate in (AGENT_BRANCH, "release/1.0"):
            evaluation = decide(candidate, default=None)
            assert (
                evaluation.status
                is BranchProtectionStatus.MISSING_DEFAULT_BRANCH
            )
            assert evaluation.is_allowed is False

    def test_the_documented_difference_whitespace_is_an_identity_problem(self):
        # T20 strips before comparing (so " main" counts as protected there);
        # T26 refuses the padded identity itself. Both refuse, so neither
        # widens what may be mutated.
        t20 = commit_policy.CommitPolicyConfig()
        assert t20.is_protected_branch(" main") is True
        assert decide(" main").status is BranchProtectionStatus.INVALID_BRANCH
        assert t20.is_agent_branch(f" {AGENT_BRANCH}") is False
        assert decide(f" {AGENT_BRANCH}").is_allowed is False

    def test_the_documented_difference_the_namespace_flag_must_be_a_bool(self):
        # T20/T21 decide the namespace by truthiness; T26 requires a real bool
        # and refuses anything else (fail closed).
        t20 = commit_policy.CommitPolicyConfig(
            required_branch_prefix="ai/sonar-fix"
        )
        assert t20.is_agent_branch(AGENT_BRANCH) is True
        assert (
            decide(AGENT_BRANCH, require_prefix="yes").status
            is BranchProtectionStatus.INVALID_POLICY
        )

    def test_the_namespace_rule_is_case_sensitive_in_both_modules(self):
        t20 = commit_policy.CommitPolicyConfig()
        t21 = push_policy.PushPolicyConfig()
        for name in ("AI/sonar-fix/x", "Ai/Sonar-Fix/x"):
            assert t20.is_agent_branch(name) is False
            assert t21.is_agent_branch(name) is False
            assert (
                decide(name).status
                is BranchProtectionStatus.WRONG_BRANCH_NAMESPACE
            )

    def test_t26_does_not_change_t20_or_t21_behaviour(self):
        # The pins above read T20/T21 only; neither module imports T26, and
        # their defaults are untouched by anything in this file.
        assert commit_policy.PROTECTED_BRANCH_NAMES == (
            "main",
            "master",
            "develop",
            "trunk",
        )
        assert commit_policy.DEFAULT_AGENT_BRANCH_PREFIX == "ai/sonar-fix"
        assert "branch_protection" not in dir(commit_policy)
        assert "branch_protection" not in dir(push_policy)


class TestModuleSurface:
    """T26 is a policy: no Git, no subprocess, no filesystem, no extra input."""

    def test_the_module_surface_is_exactly_the_documented_one(self):
        assert tuple(module.__all__) == (
            "AI_FIX_BRANCH_PREFIX",
            "ALLOWED_STATUSES",
            "BranchProtectionDecision",
            "BranchProtectionEvaluation",
            "BranchProtectionInput",
            "BranchProtectionStatus",
            "POLICY_VERSION",
            "PRECEDENCE",
            "PROTECTED_BRANCH_NAMES",
            "ProtectedBranchPolicy",
            "evaluate_branch_protection",
        )
        assert POLICY_VERSION == "t26.1"
        assert module.__all__ == tuple(sorted(module.__all__))

    def test_the_module_imports_the_standard_library_and_t07_only(self):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert imported_modules(source) == {
            "__future__",
            "dataclasses",
            "enum",
            "typing",
            "branch_naming",
        }

    def test_the_import_scanner_sees_both_import_forms(self):
        assert imported_modules("import json\nfrom re import match\n") == {
            "json",
            "re",
        }

    @pytest.mark.parametrize(
        "name",
        (
            "subprocess",
            "os",
            "sys",
            "shutil",
            "socket",
            "time",
            "pathlib",
            "tempfile",
            "json",
            "random",
            "logging",
            "requests",
            "commit_policy",
            "push_policy",
            "git_commit",
            "git_push",
            "repository",
            "context",
            "main",
        ),
    )
    def test_no_io_execution_or_orchestration_module_is_reachable(self, name):
        assert not hasattr(module, name)

    @pytest.mark.parametrize(
        "forbidden",
        (
            "subprocess",
            "system",
            "Popen",
            "check_output",
            "socket",
            "urlopen",
            "open",
            "eval",
            "exec",
            "__dict__",
            "print",
            "input",
            "getattr",
            "setattr",
            "globals",
            "locals",
            "compile",
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

    @pytest.mark.parametrize(
        "command",
        (
            "check-ref-format",
            "symbolic-ref",
            "ls-remote",
            "rev-parse",
            "for-each-ref",
            "remote add",
            "shell=True",
        ),
    )
    def test_no_git_command_is_ever_built(self, command):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert command not in source

    def test_the_policy_is_asked_with_exactly_a_policy_and_a_branch(self):
        signature = inspect.signature(evaluate_branch_protection)
        assert tuple(signature.parameters) == ("policy", "branch")
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )

    def test_no_parameter_could_carry_a_verdict_or_evidence(self):
        for parameter in inspect.signature(
            evaluate_branch_protection
        ).parameters.values():
            assert parameter.name not in (
                "status",
                "outcome",
                "result",
                "allowed",
                "verified",
                "evidence",
                "confirm",
            )

    def test_the_dataclass_fields_are_exactly_the_documented_ones(self):
        assert list(ProtectedBranchPolicy.__dataclass_fields__) == [
            "protected_branches",
            "require_ai_fix_branch_prefix",
        ]
        assert list(BranchProtectionInput.__dataclass_fields__) == [
            "candidate_branch",
            "default_branch",
        ]
        assert list(BranchProtectionEvaluation.__dataclass_fields__) == [
            "policy_version",
            "status",
            "decision",
            "candidate_branch",
            "default_branch",
            "branch_in_ai_namespace",
            "branch_is_default",
            "branch_is_protected",
            "reason",
            "reasons",
            "policy",
        ]

    def test_the_module_exposes_no_branch_mutating_api(self):
        forbidden = (
            "create",
            "create_branch",
            "checkout",
            "switch",
            "rename",
            "delete",
            "delete_branch",
            "commit",
            "push",
            "reset",
            "merge",
            "rebase",
            "fetch",
            "clone",
        )
        for name in forbidden:
            assert not hasattr(module, name)
            for record in (
                ProtectedBranchPolicy,
                BranchProtectionInput,
                BranchProtectionEvaluation,
            ):
                assert not hasattr(record, name)

    @pytest.mark.parametrize(
        "value", ("engine", "pipeline", "orchestrator", "repo")
    )
    def test_an_unexpected_keyword_is_rejected(self, value):
        with pytest.raises(TypeError):
            evaluate_branch_protection(**{value: 1})

    def test_a_non_dto_caller_is_a_programming_error_not_a_refusal(self):
        with pytest.raises(TypeError, match="ProtectedBranchPolicy"):
            evaluate_branch_protection(
                policy=None, branch=BranchProtectionInput()
            )
        with pytest.raises(TypeError, match="BranchProtectionInput"):
            evaluate_branch_protection(
                policy=ProtectedBranchPolicy(), branch=None
            )

    def test_the_module_documents_its_trust_boundary_and_purity(self):
        document = module.__doc__
        for phrase in (
            "caller-asserted",
            "policy boundary, not an evidence-authentication boundary",
            "does not discover the repository's default branch",
            "touches no network, no Git",
            "byte-for-byte unchanged",
            "never rewrites",
            "Fail closed",
        ):
            assert phrase in document


class TestPurity:
    """An evaluation performs no I/O, whatever the inputs are."""

    def test_evaluating_touches_no_file(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("T26 must not touch the filesystem")

        monkeypatch.setattr(builtins, "open", boom)
        monkeypatch.setattr(pathlib.Path, "open", boom)
        for kwargs in STATUS_INPUTS.values():
            decide(**kwargs)
        assert decide(AGENT_BRANCH).status is BranchProtectionStatus.ALLOWED

    def test_the_verdict_does_not_depend_on_the_environment(self, monkeypatch):
        before = decide("develop").as_dict()
        monkeypatch.setenv("T26_PROBE", "1")
        monkeypatch.setenv("DEFAULT_BRANCH", "trunk")
        monkeypatch.setenv("GIT_DIR", "/nowhere")
        assert decide("develop").as_dict() == before

    def test_a_thousand_evaluations_agree(self):
        payloads = {
            json.dumps(decide("develop", default="main").as_dict())
            for _ in range(200)
        }
        assert len(payloads) == 1

    def test_a_refusal_does_not_change_later_evaluations(self):
        for kwargs in STATUS_INPUTS.values():
            decide(**kwargs)
        assert decide(AGENT_BRANCH).status is BranchProtectionStatus.ALLOWED
        assert (
            decide("main").status is BranchProtectionStatus.PROTECTED_BRANCH
        )
