"""
Centralized configuration loaded from environment variables / .env file.

Uses pydantic-settings so every value can be overridden via env vars
without touching code. Secrets (GitHub PAT, Redis password) never appear
in source control.
"""

from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings sourced from the .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Redis ───────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"
    redis_stream: str = "sentinel:crashes"
    redis_consumer_group: str = "sentinel-workers"
    redis_consumer_name: str = "worker-1"

    # ── GitHub ──────────────────────────────────────────────────────────
    github_token: str = ""
    github_repo: str = ""  # e.g. "owner/repo-name"

    # ── Docker Sandbox ──────────────────────────────────────────────────
    sandbox_image: str = "sentinel-sandbox:latest"
    sandbox_timeout_seconds: int = 60

    # ── Agent ───────────────────────────────────────────────────────────
    # Maximum retry attempts when the sandbox rejects a fix
    max_fix_attempts: int = 3
    groq_api_key: str = ""
    groq_model: str = "llama3-70b-8192"

    # ── API ─────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
