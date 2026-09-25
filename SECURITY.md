# Security Policy

This project automates changes to source code, handles SonarQube credentials,
and (in future) may integrate with more services. Security is taken seriously.

---

## Reporting a vulnerability

**Please do not report security issues in public GitHub issues, discussions,
or pull requests.** Public reports can put users at risk before a fix exists.

### Preferred channel: GitHub private vulnerability reporting

Use GitHub's private advisory flow:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue and, if possible, how to reproduce it.

This creates a **private** advisory visible only to the maintainers and you.

> **Maintainer action required:** private vulnerability reporting must be
> enabled for this repository (GitHub -> **Settings -> Security -> Code
> security -> Private vulnerability reporting -> Enable**). If the *Report a
> vulnerability* button is not available, the feature has not been enabled yet.

### If private reporting is unavailable

Open a **minimal** public issue that says only that you have a security concern
and asks the maintainer to contact you privately. **Do not include** technical
details, exploit steps, tokens, or logs in that issue.

---

## What to include in a report

* A clear description of the problem and its impact.
* Steps to reproduce, if known.
* The affected version/commit (for example the `main` commit hash).
* Any relevant configuration, **with all secrets removed**.

---

## What NOT to include anywhere public

* SonarQube tokens or any API tokens.
* Git credentials, SSH keys, or passwords.
* Session cookies or `Authorization` headers.
* `.env` contents.
* Private repository URLs that embed credentials.
* Logs that contain any of the above.

If you accidentally expose a credential, **revoke/rotate it immediately** - do
not wait for a code fix.

---

## Supported versions

The project is pre-1.0. Only the latest `main` is supported. There are no
long-term-support branches yet.

| Version | Supported |
|---------|-----------|
| `main` (latest) | Yes |
| Older commits/branches | No |

---

## Response process

Reports are handled on a best-effort basis by the maintainer(s):

1. Acknowledge the report and confirm the affected area.
2. Investigate and determine severity and impact.
3. Prepare and test a fix.
4. Decide on coordinated disclosure timing with the reporter.
5. Publish a fix and, where appropriate, a GitHub Security Advisory.

Please do not disclose the issue publicly until a fix is available, or until
the maintainer and reporter agree on a disclosure date.

---

## Security-relevant areas of this project

Contributors should treat these as security-sensitive:

* **Credential handling** - `config.py`, `pipeline/config.py` and the SonarQube
  client. Tokens must never be logged, serialized, or passed on command lines.
* **Redaction** - `logging_policy.py`, `secret_scan.py`, and the diagnostics in
  `pipeline/run.py`. Redaction must not be weakened.
* **Command execution** - test/analysis/Codex commands are configuration only
  and are always run as argument arrays (`shell=False`). AI output must never
  become a shell command.
* **Git mutation** - `git_commit.py` and `git_push.py` enforce allow-lists and
  fail-closed gates. Do not bypass them.
* **Generated changes** - AI-generated edits are treated as untrusted and are
  verified (scope, tests, re-analysis) before any commit.

---

## Local configuration safety

* Copy `.env.example` to `.env` and fill in **your own** local values.
* `.env` is git-ignored. Never commit it.
* Use placeholders in examples; never use real-looking credentials.
* Prefer tokens scoped to the minimum permissions you need.
