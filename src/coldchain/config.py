"""Application configuration (12-factor, env-driven)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COLDCHAIN_", extra="ignore")

    database_url: str = "sqlite:///./coldchain.db"
    api_key_pepper: str = "dev-pepper-change-me"
    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 8
    default_page_size: int = 50
    max_page_size: int = 200
    batch_max_items: int = 500


settings = Settings()
