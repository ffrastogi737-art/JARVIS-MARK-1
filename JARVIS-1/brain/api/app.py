"""
brain/api/app.py — FastAPI application factory.

Call `create_app()` to obtain the configured FastAPI instance.
This module is intentionally separate from main.py so tests can import the
app without running uvicorn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from brain.api.routes import agent, android, chat, edge, health, live_voice, router_info, skills, tether
from brain.config import get_settings
from brain.logging_config import setup_logging

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup / shutdown logic."""
    settings = get_settings()
    log.info(
        "jarvis.startup",
        version="1.0.0-phase1",
        environment=settings.environment,
        host=settings.host,
        port=settings.port,
    )
    log.info(
        "jarvis.router_config",
        classifier=settings.classifier_model,
        simple=settings.tier_simple_model,
        moderate=settings.tier_moderate_model,
        complex=settings.tier_complex_model,
        expert=settings.tier_expert_model,
    )
    yield
    log.info("jarvis.shutdown")


def create_app() -> FastAPI:
    """Construct and return the fully configured FastAPI application."""
    setup_logging()
    settings = get_settings()

    app = FastAPI(
        title="JARVIS — AGI Brain API",
        description=(
            "Phase 1: Intelligent LLM Router with Task Complexity Classifier. "
            "Routes requests to the most cost-effective model based on task complexity."
        ),
        version="1.0.0-phase1",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # In production, restrict allow_origins to known clients.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.environment == "development" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routes ─────────────────────────────────────────────────────────────────
    app.include_router(health.router, tags=["Health"])
    app.include_router(chat.router, prefix="/api/v1", tags=["Chat"])
    app.include_router(router_info.router, prefix="/api/v1", tags=["Router"])
    app.include_router(tether.router, prefix="/api/v1", tags=["Tether"])
    app.include_router(edge.router, prefix="/api/v1", tags=["Edge"])
    app.include_router(agent.router, prefix="/api/v1", tags=["Tether"])
    app.include_router(live_voice.router, prefix="/api/v1", tags=["Voice"])
    app.include_router(skills.router, prefix="/api/v1", tags=["Skills"])
    app.include_router(android.router, prefix="/api/v1", tags=["Android"])

    return app
