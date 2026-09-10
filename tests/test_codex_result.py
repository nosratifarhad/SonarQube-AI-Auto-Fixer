"""T11 tests: pure interpretation of a typed Codex execution result."""

from codex_executor import CodexExecutionStatus as Status
from codex_executor import CodexResult
from codex_result import (
    UNCERTAINTY_MARKERS,
    analyze_codex_result,
    uncertainty_markers_in,
)


def make_result(
    status=Status.SUCCESS,
    exit_code=0,
    stdout="",
    stderr="",
    error=None,
) -> CodexResult:
    return CodexResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command=("codex", "exec"),
        error=error,
    )


def test_successful_result_is_clean_success():
    result = make_result(stdout="I fixed the function.\n")
    analysis = analyze_codex_result(result)
    assert analysis.execution_succeeded is True
    assert analysis.process_failed is False
    assert analysis.timed_out is False
    assert analysis.executable_not_found is False
    assert analysis.exit_code == 0
    assert analysis.output_suggests_uncertainty is False
    assert analysis.matched_markers == ()
    assert analysis.needs_review is False
    assert "successfully" in analysis.reason
    assert analysis.stdout == result.stdout
    assert analysis.stderr == result.stderr


def test_failed_result_with_nonzero_exit_code():
    result = make_result(status=Status.FAILED, exit_code=4, stderr="boom")
    analysis = analyze_codex_result(result)
    assert analysis.execution_succeeded is False
    assert analysis.process_failed is True
    assert analysis.exit_code == 4
    assert analysis.needs_review is True
    assert "exit code 4" in analysis.reason


def test_timeout_result():
    result = make_result(
        status=Status.TIMEOUT,
        exit_code=None,
        error="Codex timed out after 30 seconds.",
    )
    analysis = analyze_codex_result(result)
    assert analysis.timed_out is True
    assert analysis.execution_succeeded is False
    assert analysis.process_failed is False
    assert analysis.exit_code is None
    assert analysis.needs_review is True
    assert "timeout" in analysis.reason


def test_executable_not_found_result():
    result = make_result(status=Status.NOT_FOUND, exit_code=None)
    analysis = analyze_codex_result(result)
    assert analysis.executable_not_found is True
    assert analysis.timed_out is False
    assert analysis.execution_succeeded is False
    assert analysis.needs_review is True
    assert "could not be launched" in analysis.reason


def test_uncertainty_marker_in_stdout_flags_review():
    result = make_result(stdout="I am uncertain whether this is the right fix.\n")
    analysis = analyze_codex_result(result)
    assert analysis.execution_succeeded is True  # process itself was fine
    assert analysis.output_suggests_uncertainty is True
    assert "uncertain" in analysis.matched_markers
    assert analysis.needs_review is True
    assert "uncertainty" in analysis.reason


def test_uncertainty_markers_are_case_insensitive_in_stderr():
    result = make_result(stderr="I Could Not Fix this error automatically.\n")
    analysis = analyze_codex_result(result)
    assert analysis.output_suggests_uncertainty is True
    assert "could not fix" in analysis.matched_markers


def test_multiple_markers_are_sorted():
    result = make_result(stdout="Unsure and unable to fix it here.\n")
    analysis = analyze_codex_result(result)
    assert analysis.matched_markers == ("unable to fix", "unsure")


def test_no_markers_on_plain_output():
    result = make_result(stdout="Removed the empty function.\n")
    analysis = analyze_codex_result(result)
    assert analysis.output_suggests_uncertainty is False
    assert analysis.needs_review is False


def test_failed_process_output_is_not_scanned_for_markers():
    result = make_result(
        status=Status.FAILED,
        exit_code=2,
        stdout="I am uncertain but also the process failed.",
    )
    analysis = analyze_codex_result(result)
    # Marker scanning is only meaningful for completed processes.
    assert analysis.matched_markers == ()
    assert analysis.output_suggests_uncertainty is False
    assert analysis.process_failed is True


def test_uncertainty_markers_in_returns_sorted_tuple():
    result = make_result(stdout="unable to fix - also uncertain")
    assert uncertainty_markers_in(result) == ("unable to fix", "uncertain")


def test_analysis_as_dict_summary():
    result = make_result(stdout="uncertain about edge case")
    analysis = analyze_codex_result(result)
    summary = analysis.as_dict()
    assert summary["execution_succeeded"] is True
    assert summary["output_suggests_uncertainty"] is True
    assert summary["matched_markers"] == ["uncertain"]
    assert summary["needs_review"] is True
    assert summary["reason"]


def test_marker_table_is_nonempty_and_documented():
    assert UNCERTAINTY_MARKERS
    assert "uncertain" in UNCERTAINTY_MARKERS
