"""Entry point for the SonarQube AI Auto-Fixer.

Two modes:

* **default** - the original T01-T04 flow: connect to SonarQube, verify the
  project, retrieve the open issues, apply the T03 filters and print the
  selected issues. It changes nothing.
* **``--run``** - the full orchestrated pipeline (T05-T30 in the ``pipeline``
  package): clone/branch/context, Codex, diff/scope, tests, re-analysis,
  verification, classification and reporting. It is still read-only unless
  ``--commit`` (T20) and ``--push`` (T21) are additionally enabled, and every
  irreversible step stays behind the branch-protection and uncertainty
  policies.
"""

import argparse
import json
import sys
from typing import Optional, Sequence


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sonar-ai-fixer",
        description="Select SonarQube issues and optionally run the AI fix pipeline.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="run the full fix pipeline (read-only unless --commit/--push)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="allow the pipeline to commit a verified fix (T20)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="allow the pipeline to push a verified commit (T21; requires --commit)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the pipeline result as JSON instead of a human summary",
    )
    return parser


def _select_issues(client, config, filter_issues):
    """Connect, verify the project and return ``(project, total, selected)``."""
    project = client.verify_project()
    search_result = client.get_open_issues()
    total = search_result["total"]
    if total is None:
        total = len(search_result["issues"])
    selected = filter_issues(search_result["issues"])
    return project, total, selected


def _policy_flag_values(*, commit: bool, push: bool) -> dict:
    """Map the CLI flags onto the pipeline's irreversible-step gates.

    ``--push`` deliberately does **not** imply ``--commit``: a push is only
    ever attempted for a commit that actually succeeded, so enabling push
    without enabling commit can never produce a push.
    """
    return {"commit_fixes": bool(commit), "push_fixes": bool(push)}


def _run_pipeline(
    client,
    config,
    selected,
    total: int,
    *,
    commit: bool,
    push: bool,
    as_json: bool,
) -> int:
    """Run the T05-T30 pipeline for the selected issues."""
    from dataclasses import replace

    from pipeline import FixPipeline, load_pipeline_config

    try:
        pipeline_config = load_pipeline_config()
    except Exception as exc:
        print(f"Pipeline configuration error: {exc}", file=sys.stderr)
        return 3

    pipeline_config = replace(
        pipeline_config,
        **_policy_flag_values(commit=commit, push=push),
    )

    try:
        pipeline = FixPipeline(client=client, config=pipeline_config)
        result = pipeline.run(selected, discovered_issue_count=total)
    except Exception as exc:
        print(f"Pipeline error: {exc}", file=sys.stderr)
        return 4

    if as_json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0

    print(f"Pipeline processed {len(result.issue_results)} issue(s).")
    for entry in result.issue_results:
        if entry.issue_status is not None:
            status = entry.issue_status.status.value
        else:
            status = f"blocked at {entry.blocked_stage}"
        print(f"- {entry.issue_key}: {status}")
    print(f"Overall status: {result.overall_report.status.value}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

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
        project, total, selected = _select_issues(client, config, filter_issues)
    except SonarQubeError as exc:
        print(f"SonarQube error: {exc}", file=sys.stderr)
        return 2

    if args.run:
        return _run_pipeline(
            client,
            config,
            selected,
            total,
            commit=args.commit,
            push=args.push,
            as_json=args.json,
        )

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

    print("Run with --run to execute the fix pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
