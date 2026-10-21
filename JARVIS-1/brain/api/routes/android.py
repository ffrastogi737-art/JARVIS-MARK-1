"""
brain/api/routes/android.py — Android Companion API (Phase 9).

All endpoints need the tether token (X-Tether-Token header; WebSockets may use ?token=).

REST
  GET  /android/control                 24/7 status: cloud uptime, kill-switch, edge heartbeat + vitals
  POST /android/control                 {"action": ...}  (actions below)
  GET  /android/notifications?since=N   notifications with seq > N (poll from a background job)
WebSocket
  /android/control   send {"action": ...} -> {"type": "result", ...}; the cloud pushes
                     {"type": "notification", ...} as they happen
  /android/voice     the phone's mic/speaker relay: same protocol as /live/voice
                     (16 kHz PCM16 up, 24 kHz PCM16 + text down, Gemini Live with text fallback)

Actions
  {"action": "status"}
  {"action": "command", "command": "...", "shell": "powershell"|"cmd"}  -> signed task for the laptop
  {"action": "halt"} | {"action": "reset"}                             -> tether, cloud + laptop
  {"action": "chat", "text": "..."}                                    -> LLM Router reply (Hindi/English/Hinglish)

shortcut: notifications live in process memory (last 200); add FCM push + storage when the
phone must be woken while its app is closed.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from brain.api.routes import agent, edge, live_voice
from brain.api.routes.tether import accept_tethered_ws, require_tether_token
from brain.models.schemas import ChatMessage
from brain.safety.tether import kill_switch

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/android")
_auth = [Depends(require_tether_token)]

STARTED = time.time()
_seq = itertools.count(1)
_notifications: deque[dict[str, Any]] = deque(maxlen=200)
_pending: dict[str, str] = {}  # edge task id -> command, until its result arrives


class AndroidAction(BaseModel):
    action: Literal["status", "command", "halt", "reset", "chat"]
    command: str = ""
    shell: Literal["powershell", "cmd"] = "powershell"
    text: str = ""


def notify(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    n = {"seq": next(_seq), "at": time.time(), "kind": kind, "text": text, **extra}
    _notifications.append(n)
    return n


def _collect_results() -> None:
    """Turn finished laptop tasks started from the phone into notifications."""
    for tid in [t for t in _pending if t in edge._results]:
        r, command = edge._results[tid], _pending.pop(tid)
        out = (r.get("stdout") or r.get("stderr") or r.get("error") or "").strip()
        notify("task_done", f"{'OK' if r.get('ok') else 'FAILED'}: {command[:60]}",
               task_id=tid, ok=bool(r.get("ok")), output=out[-500:])


def status() -> dict[str, Any]:
    _collect_results()
    seen = edge._last_seen
    age = time.time() - seen["at"] if seen["at"] else None
    return {
        "cloud": {"online": True, "uptime_s": round(time.time() - STARTED)},
        "tether": {"halted": kill_switch.halted, "last_command": kill_switch.last_command},
        "edge": {**seen, "online": age is not None and age < 60, "age_s": None if age is None else round(age, 1)},
        "queued": len(edge._queue),
        "pending_tasks": len(_pending),
        "last_seq": _notifications[-1]["seq"] if _notifications else 0,
    }


async def perform(act: AndroidAction) -> dict[str, Any]:
    if act.action == "status":
        return status()
    if act.action == "command":
        if not act.command.strip():
            raise ValueError("command is empty")
        task = await edge.queue_task(edge.EdgeTask(op=act.shell, command=act.command))
        _pending[task["id"]] = act.command
        log.info("android.command_queued", task_id=task["id"])
        return {"task_id": task["id"], "queued": True}
    if act.action == "halt":
        res = await agent.halt()
        notify("tether", "TETHER HALT engaged from phone", killed_pids=res["killed_pids"])
        return res
    if act.action == "reset":
        kill_switch.reset()
        await edge.queue_task(edge.EdgeTask(op="tether", command="RESET"))
        notify("tether", "Tether reset from phone: executions re-enabled")
        return {"halted": False}
    if not act.text.strip():
        raise ValueError("text is empty")
    history = [ChatMessage(role="system", content=live_voice.LANGUAGE_INSTRUCTION),
               ChatMessage(role="user", content=act.text)]
    reply, _, _, provider, model_id, _ = await live_voice._llm_router.route(history)
    return {"reply": reply.content, "model": f"{provider}/{model_id}"}


@router.get("/control", dependencies=_auth, summary="Phone status poll (cloud, tether, laptop)")
async def get_control() -> dict:
    return status()


@router.post("/control", dependencies=_auth, summary="Phone remote action")
async def post_control(act: AndroidAction) -> dict:
    try:
        return await perform(act)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("android.action_failed", action=act.action, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Action failed: {exc}") from exc


@router.get("/notifications", dependencies=_auth, summary="Notifications newer than `since`")
async def notifications(since: int = 0) -> dict:
    _collect_results()
    return {"notifications": [n for n in _notifications if n["seq"] > since]}


@router.websocket("/control")
async def control_ws(ws: WebSocket, token: str = "") -> None:
    if not await accept_tethered_ws(ws, token):
        return
    pusher = asyncio.create_task(_push_notifications(ws))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                act = AndroidAction.model_validate_json(raw)
                result = await perform(act)
                await ws.send_json({"type": "result", "action": act.action, "ok": True, **result})
            except Exception as exc:  # a bad message or provider error must not drop the phone's link
                await ws.send_json({"type": "result", "ok": False, "error": str(exc) or type(exc).__name__})
    except WebSocketDisconnect:
        pass
    finally:
        pusher.cancel()


async def _push_notifications(ws: WebSocket) -> None:
    last = _notifications[-1]["seq"] if _notifications else 0
    while True:
        _collect_results()
        for n in [n for n in _notifications if n["seq"] > last]:
            await ws.send_json({"type": "notification", **n})
            last = n["seq"]
        await asyncio.sleep(0.5)


@router.websocket("/voice")
async def voice_ws(ws: WebSocket, token: str = "") -> None:
    # Own endpoint (not live_voice's function) so hot-reloading live_voice keeps this route.
    await live_voice.live_voice(ws, token)
