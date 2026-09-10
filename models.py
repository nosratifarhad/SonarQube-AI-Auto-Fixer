"""Internal domain models for SonarQube issues (T04).

The rest of the application should depend on :class:`SonarIssue` instead of
raw SonarQube JSON dictionaries.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SonarIssue:
    """A SonarQube issue represented in a SonarQube-version-neutral shape."""

    key: str
    rule: str
    severity: str
    issue_type: str
    message: str
    component: str
    line: Optional[int]
    status: str

    @property
    def file_path(self) -> str:
        """Path of the file affected by the issue.

        SonarQube components use the form ``<project key>:<file path>``.
        This property strips the project key prefix so consumers get a plain
        repository-relative path, e.g. ``my-project:src/Services/UserService.cs``
        becomes ``src/Services/UserService.cs``.

        The original :attr:`component` value is never modified. If the
        component does not contain ``:`` it is returned unchanged.
        """
        if ":" in self.component:
            return self.component.split(":", 1)[1]
        return self.component
