"""T07 tests: agent-branch naming and Git branch-name validation."""

import re

import pytest

from branch_naming import (
    AGENT_BRANCH_PREFIX,
    BranchNameError,
    is_valid_branch_name,
    make_agent_branch_name,
    utc_compact_timestamp,
    validate_branch_name,
)


class TestMakeAgentBranchName:
    def test_basic_shape_for_plain_key(self):
        name = make_agent_branch_name("AX1abcDeF2")
        assert name == f"{AGENT_BRANCH_PREFIX}/AX1abcDeF2"

    def test_prefix_is_shared_constant(self):
        assert AGENT_BRANCH_PREFIX == "ai/sonar-fix"

    def test_unsafe_characters_are_slugged(self):
        name = make_agent_branch_name("issue / 1:2 [x]")
        assert name == f"{AGENT_BRANCH_PREFIX}/issue-1-2-x"

    def test_whitespace_is_slugged(self):
        name = make_agent_branch_name("  my issue  key  ")
        assert name == f"{AGENT_BRANCH_PREFIX}/my-issue-key"

    def test_timestamp_suffix_is_appended(self):
        name = make_agent_branch_name("AX1", timestamp="20260909-101530")
        assert name == f"{AGENT_BRANCH_PREFIX}/AX1-20260909-101530"

    def test_result_always_valid_git_branch(self):
        for key in [
            "AX1abcDeF2",
            "a:b?c*[d]",
            "back\\slash",
            "spaces here",
        ]:
            name = make_agent_branch_name(key, timestamp="20260909-101530")
            validate_branch_name(name)

    def test_key_that_slugs_to_nothing_raises(self):
        for key in ["..", "??", "***"]:
            with pytest.raises(BranchNameError):
                make_agent_branch_name(key)

    def test_empty_key_raises(self):
        with pytest.raises(BranchNameError):
            make_agent_branch_name("   ")

    def test_only_unusable_characters_raise(self):
        with pytest.raises(BranchNameError):
            make_agent_branch_name("?*[\\")

    def test_non_string_key_is_coerced(self):
        assert make_agent_branch_name(12345) == f"{AGENT_BRANCH_PREFIX}/12345"


class TestValidateBranchName:
    @pytest.mark.parametrize(
        "invalid",
        [
            "",
            "HEAD",
            "@",
            "-leading-dash",
            "/leading-slash",
            "trailing-slash/",
            "trailing-dot.",
            "has..double-dot",
            "has//double-slash",
            "has @{",
            "has~tilde",
            "has^caret",
            "has:colon",
            "has?question",
            "has*star",
            "has[open-bracket",
            "has\\backslash",
            ".hidden",
            "a.lock",
            "component/.lock",
            "a/@/b",
            "a space",
        ],
    )
    def test_invalid_names_rejected(self, invalid):
        assert not is_valid_branch_name(invalid)
        with pytest.raises(BranchNameError):
            validate_branch_name(invalid)

    def test_non_string_name_rejected(self):
        with pytest.raises(BranchNameError, match="must be a string"):
            validate_branch_name(12345)

    @pytest.mark.parametrize(
        "valid",
        [
            "main",
            "develop",
            "feature/login",
            "feature/JIRA-123_description",
            "ai/sonar-fix/AX1abcDeF2",
            "ai/sonar-fix/AX1abc-20260909-101530",
            "a/b/c-d_e",
        ],
    )
    def test_valid_names_accepted(self, valid):
        assert is_valid_branch_name(valid)
        validate_branch_name(valid)  # must not raise


class TestUtcCompactTimestamp:
    def test_format_is_compact_and_branch_safe(self):
        stamp = utc_compact_timestamp()
        assert re.fullmatch(r"\d{8}-\d{6}", stamp)

    def test_fixed_now_is_stable(self):
        import datetime

        moment = datetime.datetime(
            2026, 9, 9, 10, 15, 30, tzinfo=datetime.timezone.utc
        )
        assert utc_compact_timestamp(moment) == "20260909-101530"

    def test_timestamp_can_disambiguate_repeated_issues(self):
        a = make_agent_branch_name("AX1", timestamp="20260909-000000")
        b = make_agent_branch_name("AX1", timestamp="20260909-000001")
        assert a != b
