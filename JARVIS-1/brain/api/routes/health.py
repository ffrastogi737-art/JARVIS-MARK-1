"""
brain/api/routes/health.py — /health endpoint for liveness / readiness checks.
"""

from __future__ import annotations

from fastapi import APIRouter

from brain.config import get_settings
from brain.models.schemas import HealthResponse
from brain.router.llm_router import PROVIDER_API_KEY_ATTR

router = APIRouter()

VERSION = "1.0.0-phase1"


@router.get("/health", response_model=HealthResponse, summary="Liveness / readiness check")
async def health() -> HealthResponse:
    """Return service health status and which providers have API keys configured."""
    settings = get_settings()

    configured = [
        name for name, attr in PROVIDER_API_KEY_ATTR.items() if getattr(settings, attr)
    ]

    return HealthResponse(
        status="ok",
        version=VERSION,
        environment=settings.environment,
        providers_configured=configured,
    )
