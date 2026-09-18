from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "postgresql+asyncpg://app:app@localhost:5432/hubspot_ai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "hubspot-ai-platform"
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = DEFAULT_DATABASE_URL
    request_timeout_seconds: float = 15.0
    max_retries: int = 3

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-20250514"
    hubspot_client_id: str | None = None
    hubspot_client_secret: str | None = None
    hubspot_redirect_uri: str | None = None
    hubspot_webhook_secret: str | None = None
    tavily_api_key: str | None = None
    bright_data_api_key: str | None = None
    slack_signing_secret: str | None = None

    @model_validator(mode="after")
    def validate_deployment_settings(self) -> "Settings":
        if self.app_env.lower() in {"staging", "production"}:
            if self.database_url == DEFAULT_DATABASE_URL:
                raise ValueError("DATABASE_URL must be explicitly configured outside development")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
