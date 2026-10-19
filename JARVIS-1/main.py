"""
main.py — JARVIS Brain entry point.

Run locally:
    python main.py

Or via uvicorn directly:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import uvicorn

from brain.api.app import create_app
from brain.config import get_settings

app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,  # Keep off for stability; enable manually with --reload flag
        log_config=None,  # Structlog handles all logging
    )
