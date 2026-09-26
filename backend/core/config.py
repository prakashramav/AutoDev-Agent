"""Application settings loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Load .env.dev first (local dev), then .env (docker / production).
        # Values in later files override earlier ones.
        env_file=(".env.dev", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


    # ── Application ──────────────────────────────────────────────
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # ── Database ─────────────────────────────────────────────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://autodev:autodev_secret@postgres:5432/autodev_db"
    )

    # ── Redis ────────────────────────────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"

    # ── GitHub ───────────────────────────────────────────────────
    GITHUB_TOKEN: str = ""

    # ── LLM ──────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""

    # ── Sandbox ──────────────────────────────────────────────────
    SANDBOX_CPU_QUOTA: int = 50_000          # 50 % of one CPU
    SANDBOX_MEMORY_LIMIT: str = "512m"
    SANDBOX_TIMEOUT_SECONDS: int = 300
    SANDBOX_NETWORK: str = "sandbox_net"
    SANDBOX_IMAGE: str = "python:3.12-slim"  # base image for the sandbox

    # ── Safety ───────────────────────────────────────────────────
    REQUIRE_GH_CONFIRMATION: bool = True
    AUTO_PUSH_TO_GITHUB: bool = False


settings = Settings()
