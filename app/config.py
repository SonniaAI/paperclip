from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    The development default is intentionally rejected outside development, so a
    production deployment cannot silently use a shared signing secret.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MANAGER_")

    environment: str = "development"
    database_url: str = "postgresql+asyncpg://manager_app:change-me@localhost:5432/manager_sonnia"
    session_secret: str = "development-only-change-me"
    session_cookie_name: str = "manager_session"
    session_ttl_seconds: int = 28_800
    invite_ttl_seconds: int = 604_800
    secure_cookies: bool = True
    telnyx_webhook_secret: str | None = None
    storage_bucket: str | None = None
    storage_region: str | None = None
    storage_endpoint_url: str | None = None
    storage_max_copy_bytes: int = 268_435_456
    recording_signed_url_ttl_seconds: int = 300
    hindsight_base_url: str | None = None
    hindsight_api_key: str | None = None
    hindsight_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def require_real_production_secret(self) -> Settings:
        if (
            self.environment != "development"
            and self.session_secret == "development-only-change-me"
        ):
            raise ValueError("MANAGER_SESSION_SECRET must be set outside development")
        if not 1 <= self.recording_signed_url_ttl_seconds <= 900:
            raise ValueError("recording signed URLs must expire within 15 minutes")
        if self.storage_max_copy_bytes < 1:
            raise ValueError("MANAGER_STORAGE_MAX_COPY_BYTES must be positive")
        if not 0 < self.hindsight_timeout_seconds <= 15:
            raise ValueError("MANAGER_HINDSIGHT_TIMEOUT_SECONDS must be between 0 and 15")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
