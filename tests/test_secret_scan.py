"""Tests for the pre-commit secret-scan safety primitive.

Pure logic only: no Git, no network, no real credentials. The scanner must
never leak a secret value and must fail closed on unusable input.
"""

import dataclasses

import pytest

from secret_scan import (
    SecretFinding,
    SecretFindingKind,
    SecretScanResult,
    SecretScanner,
    _path_for_line,
    scan_for_secrets,
)

TOKEN = "squ_0123456789abcdef"
OTHER = "ghp_ZYXWVUTSRQPONMLK"


def diff_with(content, *, path="src/app.py", prefix="+"):
    """A minimal unified diff whose body is ``prefix`` + ``content``."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " context line\n"
        f"{prefix}{content}\n"
    )


class TestConfiguredSecrets:
    def test_exact_configured_secret_is_found(self):
        result = scan_for_secrets(f'token = "{TOKEN}"\n', secrets=[TOKEN])
        assert isinstance(result, SecretScanResult)
        assert result.scanned is True
        assert result.clean is False
        assert result.ok is False
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.kind is SecretFindingKind.CONFIGURED_SECRET
        assert finding.secret_index == 1

    def test_absent_secret_is_clean(self):
        result = scan_for_secrets("print('hello')\n", secrets=[TOKEN])
        assert result.clean is True
        assert result.ok is True
        assert result.findings == ()

    def test_secret_like_content_without_the_exact_secret_is_clean(self):
        text = "token = 'squ_0000000000000000'\n"
        result = scan_for_secrets(text, secrets=[TOKEN])
        assert result.clean is True

    def test_multiple_configured_secrets_are_all_found(self):
        text = f"a={TOKEN}\n"
        result = scan_for_secrets(text, secrets=[TOKEN, OTHER])
        assert result.secret_count == 2
        assert len(result.findings) == 1

    def test_each_configured_secret_is_reported_separately(self):
        text = f"a={TOKEN}\nb={OTHER}\n"
        result = scan_for_secrets(text, secrets=[TOKEN, OTHER])
        indexes = [finding.secret_index for finding in result.findings]
        assert indexes == [1, 2]

    def test_duplicate_secrets_are_collapsed(self):
        scanner = SecretScanner([TOKEN, TOKEN, "", None, TOKEN])
        assert scanner.secret_count == 1

    def test_empty_secrets_are_ignored(self):
        result = scan_for_secrets("anything at all", secrets=["", "   ", None])
        assert result.secret_count == 0
        assert result.clean is True

    def test_secret_in_an_added_line(self):
        result = scan_for_secrets(diff_with(f'token = "{TOKEN}"'), secrets=[TOKEN])
        assert result.clean is False
        assert result.findings[0].path == "src/app.py"

    def test_secret_in_a_removed_line(self):
        result = scan_for_secrets(
            diff_with(f'token = "{TOKEN}"', prefix="-"), secrets=[TOKEN]
        )
        assert result.clean is False

    def test_secret_in_surrounding_context(self):
        text = diff_with("unrelated") + f" {TOKEN}\n"
        result = scan_for_secrets(text, secrets=[TOKEN])
        assert result.clean is False

    def test_repeated_secret_on_different_lines_is_reported_once_per_line(self):
        text = f"{TOKEN}\n{TOKEN}\n{TOKEN}\n"
        result = scan_for_secrets(text, secrets=[TOKEN])
        assert [finding.line for finding in result.findings] == [1, 2, 3]

    def test_a_single_string_is_not_accepted_as_a_secret_list(self):
        with pytest.raises(TypeError):
            SecretScanner(TOKEN)


class TestUrlUserinfo:
    @pytest.mark.parametrize(
        "url",
        [
            "https://user:password@example.com/path",
            "https://username@example.com",
            "http://bob:hunter2@sonar.example.com:9000/x",
            "ssh://git@example.com/repo.git",
            "postgres://app:s3cret@db.internal:5432/app",
        ],
    )
    def test_userinfo_urls_are_found(self, url):
        result = scan_for_secrets(f"remote = '{url}'\n")
        assert result.clean is False
        assert any(
            finding.kind is SecretFindingKind.URL_USERINFO
            for finding in result.findings
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/path",
            "http://sonar.example.com:9000/dashboard",
            "https://example.com?email=user@example.com",
            "ftp://files.example.com/pub",
            "mailto:someone@example.com",
        ],
    )
    def test_normal_urls_are_not_found(self, url):
        result = scan_for_secrets(f"link = '{url}'\n")
        assert result.clean is True

    def test_userinfo_detection_can_be_disabled(self):
        result = scan_for_secrets(
            "https://user:password@example.com/\n", detect_url_userinfo=False
        )
        assert result.clean is True

    def test_userinfo_detail_is_redacted(self):
        result = scan_for_secrets("https://bob:hunter2@example.com/x\n")
        assert "hunter2" not in str(result.as_dict())
        assert "***@example.com" in result.findings[0].detail


class TestNoSecretLeakage:
    def test_result_never_contains_the_secret(self):
        result = scan_for_secrets(f"token={TOKEN}\n", secrets=[TOKEN])
        payload = result.as_dict()
        assert TOKEN not in str(payload)
        assert TOKEN not in result.reason
        for finding in result.findings:
            assert TOKEN not in finding.detail
            assert TOKEN not in str(finding.as_dict())

    def test_reason_never_contains_the_secret(self):
        result = scan_for_secrets(f"{TOKEN}", secrets=[TOKEN])
        assert TOKEN not in result.reason
        assert "configured-secret" in result.reason


class TestFailClosed:
    @pytest.mark.parametrize("bad", [None, 42, b"bytes", ["a", "b"], {"k": "v"}])
    def test_unusable_input_is_not_scanned_and_not_clean(self, bad):
        result = scan_for_secrets(bad, secrets=[TOKEN])
        assert result.scanned is False
        assert result.clean is False
        assert result.ok is False
        assert result.findings == ()

    def test_oversized_input_is_not_scanned_and_not_clean(self):
        scanner = SecretScanner([TOKEN], max_text_length=10)
        result = scanner.scan_text("x" * 11)
        assert result.scanned is False
        assert result.ok is False

    def test_ok_requires_a_completed_clean_scan(self):
        assert scan_for_secrets("clean", secrets=[TOKEN]).ok is True
        assert scan_for_secrets(None).ok is False
        assert scan_for_secrets(TOKEN, secrets=[TOKEN]).ok is False

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "10", None])
    def test_invalid_scan_limit_is_rejected(self, bad):
        with pytest.raises(ValueError):
            SecretScanner([TOKEN], max_text_length=bad)


class TestModelAndPurity:
    def test_findings_are_frozen(self):
        result = scan_for_secrets(TOKEN, secrets=[TOKEN])
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.findings[0].line = 99

    def test_scan_is_deterministic(self):
        text = f"a={TOKEN}\nhttps://bob:hunter2@example.com/x\n"
        first = scan_for_secrets(text, secrets=[TOKEN, OTHER]).as_dict()
        second = scan_for_secrets(text, secrets=[TOKEN, OTHER]).as_dict()
        assert first == second

    def test_findings_are_ordered_by_line(self):
        text = "https://bob:hunter2@example.com/x\n" f"{TOKEN}\n"
        result = scan_for_secrets(text, secrets=[TOKEN])
        assert [finding.line for finding in result.findings] == [1, 2]

    def test_scan_diff_matches_scan_text(self):
        scanner = SecretScanner([TOKEN])
        text = diff_with(f'token = "{TOKEN}"')
        assert scanner.scan_diff(text) == scanner.scan_text(text)

    def test_empty_text_is_clean(self):
        assert scan_for_secrets("").ok is True

    def test_finding_dataclass_is_exported_and_typed(self):
        finding = SecretFinding(
            kind=SecretFindingKind.URL_USERINFO, line=1, detail="x"
        )
        assert finding.as_dict()["kind"] == "url-userinfo"


class TestScanLimits:
    def test_none_secret_list_is_accepted(self):
        assert SecretScanner(None).secret_count == 0

    def test_findings_are_capped_for_a_pathological_diff(self):
        text = "\n".join(f"line {i} {TOKEN}" for i in range(700)) + "\n"
        text += "https://bob:hunter2@example.com/x\n"
        result = SecretScanner([TOKEN, OTHER]).scan_text(text)
        assert result.clean is False
        assert len(result.findings) <= 500
        assert result.reason

    @pytest.mark.parametrize("line_number", [0, -1, -100])
    def test_unknown_line_numbers_have_no_path(self, line_number):
        assert _path_for_line(["+++ b/src/app.py"], line_number) is None

    def test_dev_null_target_has_no_path(self):
        lines = ["+++ /dev/null", "@@ -1 +1 @@", "+token"]
        assert _path_for_line(lines, 3) is None

    def test_header_without_a_target_has_no_path(self):
        assert _path_for_line(["+++   "], 1) is None

    def test_path_is_derived_from_the_diff_header(self):
        lines = ["+++ b/src/app.py", "@@ -1 +1 @@", "+token"]
        assert _path_for_line(lines, 3) == "src/app.py"

    def test_findings_without_a_diff_header_have_no_path(self):
        assert _path_for_line(["token = 1"], 1) is None
