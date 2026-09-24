"""CLI flag safety: the irreversible-step gates must stay opt-in.

Imports ``main`` (which only imports the standard library at module level), so
this test never needs a live configuration.
"""

from main import _build_parser, _policy_flag_values


def test_push_never_implies_commit():
    assert _policy_flag_values(commit=False, push=True) == {
        "commit_fixes": False,
        "push_fixes": True,
    }


def test_each_flag_maps_to_exactly_its_gate():
    assert _policy_flag_values(commit=False, push=False) == {
        "commit_fixes": False,
        "push_fixes": False,
    }
    assert _policy_flag_values(commit=True, push=False) == {
        "commit_fixes": True,
        "push_fixes": False,
    }
    assert _policy_flag_values(commit=True, push=True) == {
        "commit_fixes": True,
        "push_fixes": True,
    }


def test_flags_are_coerced_to_real_booleans():
    values = _policy_flag_values(commit="yes", push=1)  # type: ignore[arg-type]
    assert values == {"commit_fixes": True, "push_fixes": True}
    assert type(values["commit_fixes"]) is bool
    assert type(values["push_fixes"]) is bool


def test_default_invocation_is_read_only():
    args = _build_parser().parse_args([])
    assert args.run is False
    assert args.commit is False
    assert args.push is False
