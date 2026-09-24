"""Tests for the T30 orchestration configuration loader.

The loader is fail-closed: a missing required value or a malformed value is
rejected with a message that names the variable, and the SonarQube token is
never published in the serialized configuration.
"""

from pathlib import Path

import pytest

from pipeline import config as config_module
from pipeline.config import (
    DEFAULT_PROTECTED_BRANCHES,
    PipelineConfigError,
    load_pipeline_config,
)


def base_env(**overrides):
    values = {
        config_module.ENV_REPOSITORY_URL: "https://example.invalid/demo.git",
        config_module.ENV_SOURCE_BRANCH: "main",
        config_module.ENV_WORK_DIR: str(Path("work")),
        config_module.ENV_TEST_COMMAND: "pytest -q",
        config_module.ENV_ANALYSIS_COMMAND: '["sonar-scanner"]',
        config_module.ENV_SONAR_URL: "https://sonar.example.invalid",
        config_module.ENV_PROJECT_KEY: "demo",
    }
    values.update(overrides)
    return values


def test_loads_a_valid_environment():
    env = base_env(
        FIXER_MAX_ISSUES="3",
        FIXER_MAX_ITERATIONS="2",
        FIXER_ALLOWED_RULES="python:S1481, python:S1192",
        FIXER_CODEX_ARGS='["exec", "--full-auto"]',
        FIXER_COMMIT_FIXES="yes",
        PROJECT_KEY="demo",
    )
    config = load_pipeline_config(env)

    assert config.repository_url.endswith("demo.git")
    assert config.source_branch == "main"
    assert config.test_command == ("pytest", "-q")
    assert config.analysis_command == ("sonar-scanner",)
    assert config.max_issues == 3
    assert config.max_iterations == 2
    assert config.allowed_rules == ("python:S1481", "python:S1192")
    assert config.codex_args == ("exec", "--full-auto")
    assert config.commit_fixes is True
    assert config.push_fixes is False
    assert config.protected_branches == DEFAULT_PROTECTED_BRANCHES


def test_missing_required_value_is_rejected():
    env = base_env()
    env.pop(config_module.ENV_REPOSITORY_URL)
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_REPOSITORY_URL in str(exc.value)


def test_missing_sonar_configuration_is_rejected():
    env = base_env()
    env.pop(config_module.ENV_PROJECT_KEY)
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_PROJECT_KEY in str(exc.value)


def test_bad_boolean_is_rejected():
    env = base_env(FIXER_COMMIT_FIXES="maybe")
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_COMMIT_FIXES in str(exc.value)


def test_non_numeric_limit_is_rejected():
    env = base_env(FIXER_MAX_ISSUES="three")
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_MAX_ISSUES in str(exc.value)


def test_negative_timeout_is_rejected():
    env = base_env(FIXER_CODEX_TIMEOUT="-1")
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_CODEX_TIMEOUT in str(exc.value)


def test_command_that_starts_with_an_option_is_rejected():
    env = base_env(FIXER_TEST_COMMAND="-q pytest")
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_TEST_COMMAND in str(exc.value)


def test_json_command_must_be_an_array_of_strings():
    env = base_env(FIXER_TEST_COMMAND="[1, 2, 3]")
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_TEST_COMMAND in str(exc.value)


def test_work_dir_can_be_overridden_without_an_environment_value():
    env = base_env()
    env.pop(config_module.ENV_WORK_DIR)
    config = load_pipeline_config(env, work_dir=Path("elsewhere"))
    assert config.work_dir == Path("elsewhere")


def test_missing_work_dir_is_rejected_when_not_overridden():
    env = base_env()
    env.pop(config_module.ENV_WORK_DIR)
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_WORK_DIR in str(exc.value)


def test_token_is_never_published_but_drives_secret_scanning():
    env = base_env(SONAR_TOKEN="top-secret-value")
    config = load_pipeline_config(env)

    assert config.forbidden_secrets == ("top-secret-value",)
    assert config.as_dict()["has_token"] is True
    assert "top-secret-value" not in repr(config.as_dict())


def test_as_dict_redacts_credentials_in_the_repository_url():
    env = base_env(
        **{
            config_module.ENV_REPOSITORY_URL: (
                "https://alice:s3cr3t@git.example.com/team/repo.git"
            )
        }
    )
    config = load_pipeline_config(env)
    view = config.as_dict()

    assert "s3cr3t" not in view["repository_url"]
    assert "alice" not in view["repository_url"]
    assert view["repository_url"].endswith("repo.git")


def test_limits_default_to_unconfigured_so_policies_fail_closed():
    config = load_pipeline_config(base_env())
    assert config.max_issues is None
    assert config.max_iterations is None


def test_non_finite_timeout_is_rejected():
    # ``nan`` compares false against every bound and ``inf`` would disable the
    # bound entirely, so both must be refused rather than accepted.
    for value in ("nan", "inf", "-inf", "NaN", "Infinity"):
        env = base_env(**{config_module.ENV_CODEX_TIMEOUT: value})
        with pytest.raises(PipelineConfigError) as exc:
            load_pipeline_config(env)
        assert config_module.ENV_CODEX_TIMEOUT in str(exc.value)


def test_non_finite_poll_interval_is_rejected():
    env = base_env(**{config_module.ENV_ANALYSIS_POLL_INTERVAL: "nan"})
    with pytest.raises(PipelineConfigError) as exc:
        load_pipeline_config(env)
    assert config_module.ENV_ANALYSIS_POLL_INTERVAL in str(exc.value)


def test_env_example_documents_every_configuration_variable():
    example_path = Path(__file__).resolve().parents[1] / ".env.example"
    example = example_path.read_text(encoding="utf-8")

    names = {
        value
        for name, value in vars(config_module).items()
        if name.startswith("ENV_") and isinstance(value, str)
    }
    missing = sorted(name for name in names if name not in example)
    assert missing == []

    # The example must never contain a real-looking credential.
    assert "REPLACE_WITH_YOUR_SONAR_TOKEN" in example
    for forbidden in ("ghp_", "github_pat_", "sk-", "AKIA", "-----BEGIN"):
        assert forbidden not in example


def test_env_example_defaults_keep_irreversible_steps_off():
    example = (
        Path(__file__).resolve().parents[1] / ".env.example"
    ).read_text(encoding="utf-8")
    assert f"{config_module.ENV_COMMIT_FIXES}=false" in example
    assert f"{config_module.ENV_PUSH_FIXES}=false" in example


def _direct_config_kwargs(**overrides):
    values = dict(
        repository_url="https://example.invalid/demo.git",
        source_branch="main",
        work_dir=Path("work"),
        test_command=("pytest", "-q"),
        analysis_command=("sonar-scanner",),
        sonar_url="https://sonar.example.invalid",
        project_key="demo",
        max_issues=1,
        max_iterations=1,
    )
    values.update(overrides)
    return values


def test_direct_construction_rejects_truthy_non_bool_commit_flag():
    from pipeline.config import PipelineConfig

    for flag in ("commit_fixes", "push_fixes", "keep_work_dir"):
        with pytest.raises(PipelineConfigError):
            PipelineConfig(**_direct_config_kwargs(**{flag: "yes"}))
        with pytest.raises(PipelineConfigError):
            PipelineConfig(**_direct_config_kwargs(**{flag: 1}))


def test_direct_construction_rejects_non_finite_timeouts():
    from pipeline.config import PipelineConfig

    for value in (float("nan"), float("inf"), float("-inf"), 0, -1):
        with pytest.raises(PipelineConfigError):
            PipelineConfig(
                **_direct_config_kwargs(codex_timeout_seconds=value)
            )


def test_direct_construction_rejects_bool_or_negative_limits():
    from pipeline.config import PipelineConfig

    for value in (True, False, -1, "3"):
        with pytest.raises(PipelineConfigError):
            PipelineConfig(**_direct_config_kwargs(max_issues=value))


def test_direct_construction_accepts_the_loader_defaults():
    from pipeline.config import PipelineConfig

    config = PipelineConfig(**_direct_config_kwargs(max_issues=None))
    assert config.max_issues is None
    assert config.commit_fixes is False
