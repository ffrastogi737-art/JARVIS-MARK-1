"""
brain/daemon/service_wrapper.py — 24/7 entry point for the Edge Agent (Phase 7).

    python -m brain.daemon.service_wrapper

Configuration comes from environment variables (install_daemon.ps1 sets them machine-wide);
brain.config is deliberately not used, since pydantic-settings would double the footprint:
  JARVIS_CLOUD_URL        Cloud Brain base URL, e.g. https://jarvis.example.com
  TETHER_SECRET_TOKEN     shared secret for auth + task signatures
  JARVIS_EDGE_LOG         log file (default ~/.jarvis/edge.log)

Stops cleanly on SIGINT/SIGTERM (and Ctrl+Break on Windows). If the agent loop crashes it
is restarted after a pause; Task Scheduler restarts the whole process if it dies.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

import structlog

from brain.daemon.edge_agent import EdgeAgent

log = structlog.get_logger(__name__)


def main() -> None:
    log_file = Path(os.environ.get("JARVIS_EDGE_LOG", Path.home() / ".jarvis" / "edge.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_file, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    stop = threading.Event()
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: stop.set())

    # JARVIS_AGENT=cloud on the always-on VM adds bash + headless browser ops.
    from brain.agents import CloudComputerAgent, WindowsAgent
    local = CloudComputerAgent() if os.environ.get("JARVIS_AGENT") == "cloud" else WindowsAgent()
    agent = EdgeAgent(os.environ["JARVIS_CLOUD_URL"], os.environ.get("TETHER_SECRET_TOKEN", ""), agent=local)
    while not stop.is_set():
        try:
            agent.run(stop)
        except Exception:
            log.exception("edge.crashed")
            stop.wait(10)


if __name__ == "__main__":
    main()
