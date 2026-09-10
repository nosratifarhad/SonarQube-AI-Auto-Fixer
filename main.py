"""Entry point demonstrating the T01-T04 flow.

Flow (current scope):

    SonarQube
        |
        v
    SonarClient (sonar_client.py)
        |  raw issue dictionaries
        v
    issue_filter.py
        |  list[SonarIssue]
        v
    main.py

`main.py` only orchestrates and displays results; it does not depend on raw
SonarQube dictionaries after filtering.
"""

import sys


_SEPARATOR = "-" * 60


def _print_issue(issue: "SonarIssue") -> None:
    """Print one selected issue in the agreed summary format."""
    print(_SEPARATOR)
    print(f"{'Key:':<11}{issue.key}")
    print(f"{'Rule:':<11}{issue.rule}")
    print(f"{'Type:':<11}{issue.issue_type}")
    print(f"{'Severity:':<11}{issue.severity}")
    print(f"{'Message:':<11}{issue.message}")
    print(f"{'File:':<11}{issue.file_path}")
    print(f"{'Line:':<11}{issue.line if issue.line is not None else '-'}")
    print(f"{'Status:':<11}{issue.status}")
    print(_SEPARATOR)


def main() -> int:
    # Imported inside a function so a missing/empty configuration produces a
    # clear, single error message instead of a raw traceback.
    try:
        import config  # validated at import time; raises ConfigurationError
        from issue_filter import filter_issues
        from sonar_client import SonarClient, SonarQubeError
    except Exception as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 1

    try:
        client = SonarClient(
            sonar_url=config.SONAR_URL,
            sonar_token=config.SONAR_TOKEN,
            project_key=config.PROJECT_KEY,
        )
        project = client.verify_project()
        search_result = client.get_open_issues()
    except SonarQubeError as exc:
        print(f"SonarQube error: {exc}", file=sys.stderr)
        return 2

    total = search_result["total"]
    if total is None:
        total = len(search_result["issues"])

    selected = filter_issues(search_result["issues"])

    print(f"Connected to SonarQube at {config.SONAR_URL}")
    print(
        f"Verified project: {project.get('name') or config.PROJECT_KEY} "
        f"({config.PROJECT_KEY})"
    )
    print()
    print(f"Total open issues: {total}")
    print(f"Selected issues: {len(selected)}")
    print()

    for issue in selected:
        _print_issue(issue)

    if not selected:
        print("No issues matched the current filtering rules.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
