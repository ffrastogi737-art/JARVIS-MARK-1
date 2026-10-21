"""
brain/api/routes/agent.py — POST /agent/tether/halt: the HUD/CUI/phone panic button (Phase 8).

HALTs the cloud's running executions and queues a signed HALT for the laptop's Edge Agent.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from brain.api.routes import edge
from brain.api.routes.tether import require_tether_token
from brain.safety.tether import kill_switch

router = APIRouter(prefix="/agent/tether", dependencies=[Depends(require_tether_token)])


@router.post("/halt", summary="Panic button: HALT cloud executions and queue HALT for the edge")
async def halt() -> dict:
    res = await asyncio.to_thread(kill_switch.trigger, "HALT")
    task = await edge.queue_task(edge.EdgeTask(op="tether", command="HALT"))
    return {**res, "edge_task": task["id"]}
