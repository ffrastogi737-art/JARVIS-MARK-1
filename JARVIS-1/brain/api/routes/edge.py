"""
brain/api/routes/edge.py — Cloud Brain side of the Edge Agent link (Phase 7).

The Cloud Brain does the heavy work (LLM routing, research, skills, self-evolution) and
hands the local Edge Agent only small, signed system tasks:
  POST /edge/tasks    queue a task; the cloud stamps an id and an HMAC signature
  GET  /edge/poll     the edge fetches pending tasks (tether tasks first); doubles as heartbeat
  POST /edge/results  the edge reports results (live, or a buffered batch after reconnecting)
  GET  /edge/status   last heartbeat (with laptop CPU/RAM %), queue depth, results
  POST /agent/tether/halt  HUD panic button: HALT the cloud's processes and the edge's

Tasks queue here while the laptop is offline and are delivered on its next poll.

shortcut: queue and results live in process memory; move to Redis/SQLite once the cloud
runs more than one worker or must survive restarts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from brain.api.routes.tether import require_tether_token
from brain.config import get_settings
from brain.safety.tether import kill_switch, sign_task

router = APIRouter(prefix="/edge", dependencies=[Depends(require_tether_token)])

_queue: list[dict[str, Any]] = []
_results: dict[str, dict[str, Any]] = {}
_last_seen: dict[str, Any] = {"at": None, "status": None, "cpu": None, "ram": None}


class EdgeTask(BaseModel):
    op: Literal["powershell", "cmd", "tether", "update"]
    command: str = ""      # shell command, or HALT/STOP/SHUTDOWN/RESET for op=tether
    module: str = ""       # op=update: dotted module under brain/, e.g. brain.agents.windows_agent
    source: str = ""       # op=update: full new source of that module
    timeout: float | None = None


class EdgeResults(BaseModel):
    results: list[dict[str, Any]]


@router.post("/tasks", summary="Queue a signed task for the Edge Agent")
async def queue_task(body: EdgeTask) -> dict:
    task = {"id": uuid4().hex, "created": time.time(), **body.model_dump()}
    task["sig"] = sign_task(task, get_settings().tether_secret_token)
    _queue.append(task)
    return {"id": task["id"], "queued": len(_queue)}


@router.get("/poll", summary="Edge Agent heartbeat: fetch pending tasks")
async def poll(status: str = "idle", cpu: float | None = None, ram: float | None = None) -> dict:
    _last_seen.update(at=time.time(), status=status, cpu=cpu, ram=ram)
    tasks = sorted(_queue, key=lambda t: t["op"] != "tether")  # stable: HALT jumps the queue
    _queue.clear()
    return {"tasks": tasks}


@router.post("/results", summary="Edge Agent reports task results")
async def results(body: EdgeResults) -> dict:
    for r in body.results:
        _results[str(r.get("id"))] = r
    return {"stored": len(body.results)}


@router.get("/status", summary="Edge heartbeat, queue depth and results")
async def status() -> dict:
    return {"last_seen": _last_seen, "queued": len(_queue), "results": _results}


halt_router = APIRouter(prefix="/agent/tether", dependencies=[Depends(require_tether_token)])


@halt_router.post("/halt", summary="Panic button: HALT cloud executions and queue HALT for the edge")
async def halt() -> dict:
    res = await asyncio.to_thread(kill_switch.trigger, "HALT")
    edge = await queue_task(EdgeTask(op="tether", command="HALT"))
    return {**res, "edge_task": edge["id"]}
