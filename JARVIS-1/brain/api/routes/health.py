"""
brain/api/routes/health.py — /health endpoint for liveness / readiness checks.
"""

from __future__ import annotations

from fastapi import APIRouter

from brain.config import get_settings
from brain.models.schemas import HealthResponse

router = APIRouter()

VERSION = "1.0.0-phase1"


@router.get("/health", response_model=HealthResponse, summary="Liveness / readiness check")
async def health() -> HealthResponse:
    """Return service health status and which providers have API keys configured."""
    settings = get_settings()

    configured: list[str] = []
    if settings.openai_api_key:
        configured.append("openai")
    if settings.anthropic_api_key:
        configured.append("anthropic")
    if settings.google_api_key:
        configured.append("google")
    if settings.groq_api_key:
        configured.append("groq")
    if settings.deepseek_api_key:
        configured.append("deepseek")

    return HealthResponse(
        status="ok",
        version=VERSION,
        environment=settings.environment,
        providers_configured=configured,
    )
