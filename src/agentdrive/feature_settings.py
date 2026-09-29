"""Runtime feature settings that do not require the full service config.

The v0 manifest is imported by build and compatibility tooling that has no
database, bucket, or session-secret configuration. Keep environment-only
surface gates in this small settings model so those tools can still derive the
same active contract as the application without weakening ``Settings``'s
required production configuration. The OpenAPI compatibility harness imports
this module with only the Python standard library installed, so its fallback
must remain free of application dependencies. That fallback intentionally
reads the process environment only: dependency-free CI must not acquire
machine-local ``.env`` behavior.
"""

from __future__ import annotations

import os


def _environment_bool(name: str, *, default: bool) -> bool:
    # BaseSettings is case-insensitive by default. Build the same normalized
    # view here so dependency-free contract tooling cannot select a different
    # surface from the application for the same process environment.
    raw = {key.lower(): value for key, value in os.environ.items()}.get(name.lower())
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "t", "on", "yes", "y"}:
        return True
    if normalized in {"0", "false", "f", "off", "no", "n"}:
        return False
    raise ValueError(f"{name} must be a boolean")


try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ModuleNotFoundError as exc:
    if exc.name != "pydantic_settings":
        raise

    class FeatureSettings:
        """Stdlib-only view used by manifest and compatibility tooling."""

        def __init__(self) -> None:
            self.sheet_sessions_enabled = _environment_bool(
                "SHEET_SESSIONS_ENABLED", default=False
            )

else:


    class FeatureSettings(BaseSettings):
        """Feature gates that determine which public routes exist at startup."""

        # Private-beta default is deliberately fail-closed. Terraform enables
        # the eight edit-session operations only in staging.
        sheet_sessions_enabled: bool = False

        model_config = SettingsConfigDict(
            env_file=".env", env_file_encoding="utf-8", extra="ignore"
        )


feature_settings = FeatureSettings()
