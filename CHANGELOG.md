# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project intends to follow [Semantic Versioning](https://semver.org/)
once it reaches its first tagged release.

> **Status:** the project is pre-1.0 and has no tagged releases yet. Everything
> below currently lives on `main` and is tracked under **Unreleased**.

## [Unreleased]

### Added

* End-to-end orchestration of the fix lifecycle (`pipeline/`): clone/branch,
  issue context, Codex execution, diff/scope validation, project tests,
  SonarQube re-analysis, verification, final status, reporting.
* `main.py --run` entry point with opt-in, fail-closed `--commit` and `--push`
  gates (read-only by default).
* Policy modules: max-issue limit, max-iteration limit, main/default branch
  protection, uncertainty policy, Sonar rule allowlist, bounded logging policy.
* Safe Git commit (T20) and push (T21) executors with allow-lists and deny-lists.
* Overall and per-issue reporting.
* `.env.example` configuration template.
* Community-readiness documentation and automation:
  * `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `GOVERNANCE.md`.
  * GitHub issue forms, a pull request template, `CODEOWNERS`, Dependabot, and a
    public-secret-free CI workflow.
  * A future-integrations specification (SonarQube authentication options,
    GitLab, and MCP) in `docs/sonarqube-ai-fixer-spec.md` section 23.

### Security

* Credential redaction in logs, diagnostics, and reports; literal secrets are
  scrubbed from pipeline error output.
* `push` is only ever attempted after a real, verified commit
  (`push => commit succeeded`); `--push` does not imply `--commit`.
* AI-generated changes are treated as untrusted and verified before commit.

---

## Notes on future releases

When the first release is tagged, entries will move from **Unreleased** into a
versioned section, and the project will document its release process
(SemVer, Git tags, and GitHub Releases). No release automation exists yet.
