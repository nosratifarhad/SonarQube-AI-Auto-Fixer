# Governance

This is a small, early-stage open-source project. This document describes how
decisions are made **today** and how that can grow later. It is intentionally
lightweight - no committees, no bureaucracy.

---

## Roles

| Role | What they do |
|------|--------------|
| **Contributor** | Anyone who opens an issue or a Pull Request. Follows [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). |
| **Reviewer** | A trusted contributor who reviews PRs and gives feedback. Reviewers may not merge unless they are also maintainers. |
| **Maintainer** | Has merge rights, manages releases and repository settings, and makes final decisions when consensus is unclear. |

The project is currently maintained by a single maintainer.

---

## How decisions are made

* **Small changes** (bug fixes, docs, tests, small refactors): decided by normal
  Pull Request review. The maintainer merges when CI is green and the change is
  sound.
* **Significant changes** (new features, behaviour changes, new dependencies,
  security-relevant changes, public interfaces): open an **issue first** to
  discuss the approach before investing in a large PR. This avoids wasted work.
* **Anything with legal, licensing, or credential implications**: decided by the
  maintainer, not by a PR.

The default is **lazy consensus**: if nobody objects and the maintainer agrees,
the change proceeds. When consensus is unclear, the maintainer decides and
documents the reasoning in the issue or PR.

---

## Adding reviewers or maintainers

There is no fixed formula; sustained, high-quality contribution is what
matters:

* **Reviewer**: a contributor who has landed several good PRs and gives helpful
  reviews may be invited to review.
* **Maintainer**: a reviewer who has shown good judgement over time may be
  invited to become a maintainer. This is a deliberate invitation by the
  existing maintainer, not an automatic promotion.

New maintainers are added by updating `.github/CODEOWNERS` and the repository
permissions on GitHub (a manual, maintainer-only action).

---

## Changing this document

Governance changes are made through a Pull Request like any other change, and
are noted in the [CHANGELOG](CHANGELOG.md).

---

## Related documents

* [CONTRIBUTING.md](CONTRIBUTING.md) - how to contribute.
* [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) - expected behaviour.
* [SECURITY.md](SECURITY.md) - how to report vulnerabilities.
* [README.md](README.md) - project overview.
