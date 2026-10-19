"""
brain/config.py — Centralised settings loaded once at startup.

Uses pydantic-settings to pull values from environment variables / .env file.
Every other module imports from here — never reads env vars directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime-tuneable knobs for JARVIS Phase 1."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Server ─────────────────────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    environment: Literal["development", "staging", "production"] = Field(
        default="development"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    secret_key: str = Field(default="jarvis-dev-secret-change-in-production")

    # ── API Keys ───────────────────────────────────────────────────────────────
    # Free-tier providers only: Groq, Google AI Studio, OpenRouter (":free" models).
    groq_api_key: str = Field(default="")
    google_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")

    # ── Router / Classifier ────────────────────────────────────────────────────
    # The classifier must be cheap & fast — it runs on *every* request.
    classifier_model: str = Field(default="groq/llama-3.1-8b-instant")

    # Tier model assignments (provider/model-id)
    tier_simple_model: str = Field(default="groq/llama-3.1-8b-instant")
    tier_moderate_model: str = Field(default="groq/llama-3.3-70b-versatile")
    tier_complex_model: str = Field(default="google/gemini-2.5-flash")
    tier_expert_model: str = Field(default="google/gemini-2.5-pro")

    # Tried in order when the tier model is rate-limited, errors, or has no key.
    fallback_models: str = Field(
        default=(
            "groq/llama-3.3-70b-versatile,"
            "google/gemini-2.5-flash,"
            "openrouter/meta-llama/llama-3.3-70b-instruct:free,"
            "groq/llama-3.1-8b-instant"
        )
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings object (cached after first call)."""
    return Settings()
