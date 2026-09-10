"""Configuration for the SonarQube AI-fixer POC (T01).

Loads the connection settings from the environment and from the ``.env``
file located next to this module, then exposes them as validated,
module-level constants:

    SONAR_URL      Base URL of the SonarQube server (no trailing slash).
    SONAR_TOKEN    SonarQube access token (kept private, never printed).
    PROJECT_KEY    Key of the SonarQube project to analyse.

Importing this module fails with :class:`ConfigurationError` when a required
setting is missing or empty, so misconfiguration is detected early and with a
clear message.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(Exception):
    """Raised when a required configuration value is missing or invalid."""


# Load variables from ``.env`` into ``os.environ``. Existing real environment
# variables take precedence (load_dotenv does not override by default).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_FILE)


def _read_required(name: str, *, strip_trailing_slash: bool = False) -> str:
    """Read and validate one required configuration value.

    Raises:
        ConfigurationError: if the value is missing or empty.
    """
    value = os.getenv(name, "").strip()
    if strip_trailing_slash:
        value = value.rstrip("/")
    if not value:
        raise ConfigurationError(
            f"Missing required configuration '{name}'. "
            f"Set it in your environment or in '{_ENV_FILE.name}' "
            f"(next to {_ENV_FILE})."
        )
    return value


SONAR_URL = _read_required("SONAR_URL", strip_trailing_slash=True)
SONAR_TOKEN = _read_required("SONAR_TOKEN")
PROJECT_KEY = _read_required("PROJECT_KEY")
