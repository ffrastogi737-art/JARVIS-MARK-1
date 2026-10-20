"""
brain/api/routes/tether.py — Token-protected Tether control endpoints.
"""

from __future__ import annotations

import asyncio
import hmac
import re

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from brain.config import get_settings
from brain.safety.tether import kill_switch, snapshots

log = structlog.get_logger(__name__)


# ── Token authentication ───────────────────────────────────────────────────────
def require_tether_token(x_tether_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject requests without the correct X-Tether-Token."""
    expected = get_settings().tether_secret_token
    if not expected:
        # Fail closed: an unset secret must never mean "open to everyone".
        raise HTTPException(status_code=503, detail="TETHER_SECRET_TOKEN is not configured")
    if not x_tether_token or not hmac.compare_digest(x_tether_token.encode(), expected.encode()):
        log.warning("tether.auth_failed")
        raise HTTPException(status_code=401, detail="Invalid or missing X-Tether-Token")


router = APIRouter(prefix="/tether", dependencies=[Depends(require_tether_token)])


class TetherCommand(BaseModel):
    command: str


@router.get("/status", summary="Kill-switch state and snapshot count")
async def status() -> dict:
    return {
        "halted": kill_switch.halted,
        "last_command": kill_switch.last_command,
        "active_pids": kill_switch.active_pids,
        "snapshots": len(snapshots.list_snapshots()),
    }


@router.post("/command", summary="HALT / STOP / SHUTDOWN: kill all execution processes")
async def command(body: TetherCommand) -> dict:
    try:
        # Killing and reaping process trees blocks; keep the event loop free.
        return await asyncio.to_thread(kill_switch.trigger, body.command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reset", summary="Release the kill-switch so executions may run again")
async def reset() -> dict:
    return kill_switch.reset()


@router.post("/rollback/{snapshot_id}", summary="Restore a file or directory from a snapshot")
async def rollback(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[\w-]+", snapshot_id) or not (snapshots.root / snapshot_id / "manifest.json").is_file():
        raise HTTPException(status_code=404, detail="Unknown snapshot")
    return await asyncio.to_thread(snapshots.rollback, snapshot_id)
