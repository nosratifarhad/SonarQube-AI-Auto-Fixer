"""Orchestration package for the SonarQube AI Auto-Fixer (T30).

The repository's policy and stage modules live at the top level and are pure,
dependency-free units. This package is the composition layer: it is the only
place that imports several stages together, and the only place that performs
the irreversible lifecycle steps (T20 commit, T21 push) - behind explicit,
fail-closed policy gates.

Public surface:

* :class:`~pipeline.config.PipelineConfig` / :func:`~pipeline.config.load_pipeline_config`
* :class:`~pipeline.run.FixPipeline` / :class:`~pipeline.run.PipelineRunResult`
* :class:`~pipeline.logging.PolicyLogger` (T29-bound logging)
"""

from pipeline.config import (
    PipelineConfig,
    PipelineConfigError,
    load_pipeline_config,
)
from pipeline.logging import PolicyLogger, build_default_logging_policy
from pipeline.run import (
    FixPipeline,
    IssueRunResult,
    PipelineDependencies,
    PipelineRunResult,
    build_default_dependencies,
)

__all__ = (
    "FixPipeline",
    "IssueRunResult",
    "PipelineConfig",
    "PipelineConfigError",
    "PipelineDependencies",
    "PipelineRunResult",
    "PolicyLogger",
    "build_default_dependencies",
    "build_default_logging_policy",
    "load_pipeline_config",
)
