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
    # Free-tier providers only — no paid key is ever required.
    groq_api_key: str = Field(default="")
    google_api_key: str = Field(default="")  # Gemini (AI Studio free tier)
    cerebras_api_key: str = Field(default="")
    sambanova_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")

    # ── Tether (Phase 3) ───────────────────────────────────────────────────────
    # Required by /api/v1/tether/*; when empty those endpoints refuse every request.
    tether_secret_token: str = Field(default="")

    # ── Gemini Live voice (Phase 8) ────────────────────────────────────────────
    # Uses google_api_key. Model names churn; override with GEMINI_LIVE_MODEL.
    gemini_live_model: str = Field(default="gemini-2.5-flash-native-audio-preview-12-2025")
    gemini_live_url: str = Field(
        default="wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )

    # ── Router / Classifier ────────────────────────────────────────────────────
    # The classifier must be cheap & fast — it runs on *every* request.
    classifier_model: str = Field(default="groq/llama-3.1-8b-instant")

    # Tier model assignments (provider/model-id)
    tier_simple_model: str = Field(default="groq/llama-3.1-8b-instant")
    tier_moderate_model: str = Field(default="groq/llama-3.3-70b-versatile")
    tier_complex_model: str = Field(default="google/gemini-2.5-flash")
    tier_expert_model: str = Field(default="openrouter/deepseek/deepseek-r1:free")

    # Tried in order when the primary model hits a 429 / 5xx / timeout.
    fallback_models: list[str] = Field(
        default=[
            "cerebras/llama-3.3-70b",
            "sambanova/Meta-Llama-3.3-70B-Instruct",
            "groq/llama-3.1-8b-instant",
        ]
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings object (cached after first call)."""
    return Settings()
