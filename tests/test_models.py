"""T04 tests: the internal ``SonarIssue`` model and raw-JSON conversion."""

from models import SonarIssue
from issue_filter import to_sonar_issue, filter_issues, is_allowed


def _realistic_raw_issue() -> dict:
    """A realistic SonarQube ``issues/search`` issue (old+new API fields)."""
    return {
        "key": "AX1kD_9xT0abc123",
        "rule": "python:S108",
        "severity": "MAJOR",
        "component": "my-project:src/Services/UserService.cs",
        "project": "my-project",
        "line": 42,
        "hash": "f0a2c...",
        "textRange": {
            "startLine": 42,
            "endLine": 42,
            "startOffset": 4,
            "endOffset": 11,
        },
        "flows": [],
        "status": "OPEN",
        "message": "Define a constant instead of duplicating this literal "
        "'secret' 3 times.",
        "effort": "5min",
        "author": "dev@example.com",
        "tags": ["convention"],
        "creationDate": "2026-09-01T10:00:00+0000",
        "updateDate": "2026-09-02T10:00:00+0000",
        "type": "CODE_SMELL",
        "scope": "MAIN",
    }


def test_sonar_issue_fields_map_from_realistic_raw_issue():
    issue = to_sonar_issue(_realistic_raw_issue())

    assert issue.key == "AX1kD_9xT0abc123"
    assert issue.rule == "python:S108"
    assert issue.severity == "MAJOR"
    assert issue.issue_type == "CODE_SMELL"
    assert "duplicating this literal" in issue.message
    assert issue.component == "my-project:src/Services/UserService.cs"
    assert issue.line == 42
    assert issue.status == "OPEN"


def test_file_path_strips_project_key_prefix():
    issue = to_sonar_issue(_realistic_raw_issue())

    assert issue.file_path == "src/Services/UserService.cs"


def test_component_is_never_mutated():
    raw = _realistic_raw_issue()
    issue = to_sonar_issue(raw)

    assert issue.component == "my-project:src/Services/UserService.cs"
    assert raw["component"] == "my-project:src/Services/UserService.cs"


def test_file_path_returns_component_unchanged_when_no_colon():
    raw = _realistic_raw_issue()
    raw["component"] = "src/legacy/no-project-prefix.py"

    issue = to_sonar_issue(raw)

    assert issue.file_path == "src/legacy/no-project-prefix.py"
    assert issue.file_path == issue.component


def test_file_path_splits_only_on_first_colon():
    raw = _realistic_raw_issue()
    raw["component"] = "my-project:src/weird:name.py"

    issue = to_sonar_issue(raw)

    assert issue.file_path == "src/weird:name.py"


def test_line_is_none_when_raw_issue_has_no_line():
    raw = _realistic_raw_issue()
    raw.pop("line")

    issue = to_sonar_issue(raw)

    assert issue.line is None


def test_line_is_none_when_raw_line_is_not_numeric():
    raw = _realistic_raw_issue()
    raw["line"] = "not-a-number"

    issue = to_sonar_issue(raw)

    assert issue.line is None


def test_missing_fields_default_to_empty_strings():
    issue = to_sonar_issue({})

    assert issue.key == ""
    assert issue.rule == ""
    assert issue.severity == ""
    assert issue.issue_type == ""
    assert issue.message == ""
    assert issue.component == ""
    assert issue.file_path == ""
    assert issue.line is None
    assert issue.status == ""


def test_sonar_issue_is_a_plain_dataclass():
    issue = SonarIssue(
        key="K1",
        rule="r",
        severity="MAJOR",
        issue_type="BUG",
        message="m",
        component="p:src/a.py",
        line=1,
        status="OPEN",
    )

    assert issue.line == 1
    assert issue == SonarIssue(
        key="K1",
        rule="r",
        severity="MAJOR",
        issue_type="BUG",
        message="m",
        component="p:src/a.py",
        line=1,
        status="OPEN",
    )


def test_is_allowed_rules_and_filter_round_trip():
    good = _realistic_raw_issue()
    bad_type = dict(good, type="VULNERABILITY")
    bad_severity = dict(good, severity="INFO")

    assert is_allowed(good)
    assert not is_allowed(bad_type)
    assert not is_allowed(bad_severity)

    converted = filter_issues([good, bad_type, bad_severity])
    assert len(converted) == 1
    assert converted[0].key == good["key"]
    assert isinstance(converted[0], SonarIssue)
