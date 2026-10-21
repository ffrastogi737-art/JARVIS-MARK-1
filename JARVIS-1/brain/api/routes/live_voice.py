"""
brain/api/routes/live_voice.py — Gemini Live voice relay (Phase 8).

WS /api/v1/live/voice  (auth: X-Tether-Token header or ?token=)

Client -> Brain
  binary frame                 16 kHz mono PCM16 mic audio
  {"type": "text", "text": s}  a typed turn
  {"type": "end_turn"}         the user stopped speaking
Brain -> Client
  binary frame                 24 kHz mono PCM16 reply audio (live mode)
  {"type": "ready", "mode": "live"|"fallback", "model": s}
  {"type": "text", "text": s, "model": s}     reply text / transcription chunk
  {"type": "turn_complete"} | {"type": "fallback", "reason": s} | {"type": "error", "error": s}

Live mode relays to the Gemini Live API (bidirectional native audio). If Gemini Live is
unavailable — no key, rate limit/quota close, network error — the session falls back to
text: typed turns, or mic audio transcribed by Groq Whisper on end_turn, go through the
LLMRouter (Groq/Gemini text models with their own failover) and the reply comes back as text.

shortcut: once fallen back, a session stays on text; reconnect to retry Gemini Live.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave

import httpx
import structlog
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from brain.api.routes.tether import accept_tethered_ws
from brain.config import get_settings
from brain.models.schemas import ChatMessage
from brain.router.llm_router import LLMRouter

log = structlog.get_logger(__name__)
router = APIRouter()

LIVE_BADGE = "Gemini Live Native Audio"
LANGUAGE_INSTRUCTION = (
    "You are JARVIS. Reply in the language the user speaks: Hindi, English, or Hinglish "
    "(Hindi and English mixed, Latin script) — mirror their mix and script."
)
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_llm_router = LLMRouter()


class LiveUnavailable(Exception):
    """Gemini Live could not start or ended the session; switch to text fallback."""


@router.websocket("/live/voice")
async def live_voice(ws: WebSocket, token: str = "") -> None:
    if not await accept_tethered_ws(ws, token):
        return
    settings = get_settings()
    # One reader for the whole session: cancelling a pending ws.receive() (when Gemini drops
    # mid-turn) would wedge the socket, so both modes consume this queue instead.
    inbox: asyncio.Queue[dict] = asyncio.Queue()
    reader = asyncio.create_task(_read_client(ws, inbox))
    try:
        try:
            if not settings.google_api_key:
                raise LiveUnavailable("GOOGLE_API_KEY not configured")
            await _live_session(ws, inbox)
            return
        except (LiveUnavailable, websockets.exceptions.WebSocketException, OSError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
        log.warning("live_voice.fallback", reason=reason)
        await ws.send_json({"type": "fallback", "reason": reason})
        await _fallback_session(ws, inbox)
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
    log.info("live_voice.closed")


async def _read_client(ws: WebSocket, inbox: asyncio.Queue) -> None:
    while True:
        msg = await ws.receive()
        await inbox.put(msg)
        if msg["type"] == "websocket.disconnect":
            return


# ── Live mode: relay to Gemini Live ────────────────────────────────────────────
async def _live_session(ws: WebSocket, inbox: asyncio.Queue) -> None:
    s = get_settings()
    async with websockets.connect(f"{s.gemini_live_url}?key={s.google_api_key}", max_size=None,
                                  open_timeout=10) as gem:
        await gem.send(json.dumps({"setup": {
            "model": f"models/{s.gemini_live_model}",
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "systemInstruction": {"parts": [{"text": LANGUAGE_INSTRUCTION}]},
            "outputAudioTranscription": {},
        }}))
        reply = json.loads(await asyncio.wait_for(gem.recv(), timeout=10))
        if "setupComplete" not in reply:
            raise LiveUnavailable(f"Gemini Live setup rejected: {str(reply)[:200]}")
        await ws.send_json({"type": "ready", "mode": "live", "model": LIVE_BADGE})
        log.info("live_voice.live", model=s.gemini_live_model)
        tasks = {asyncio.create_task(_client_to_gemini(inbox, gem)), asyncio.create_task(_gemini_to_client(gem, ws))}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            t.result()  # re-raise: WebSocketDisconnect ends the session, LiveUnavailable falls back


async def _client_to_gemini(inbox: asyncio.Queue, gem) -> None:
    while True:
        msg = await inbox.get()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1000))
        if msg.get("bytes"):
            await gem.send(json.dumps({"realtimeInput": {"audio": {
                "data": base64.b64encode(msg["bytes"]).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
            continue
        data = json.loads(msg.get("text") or "{}")
        if data.get("type") == "text":
            await gem.send(json.dumps({"clientContent": {
                "turns": [{"role": "user", "parts": [{"text": data["text"]}]}], "turnComplete": True}}))
        elif data.get("type") == "end_turn":
            await gem.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))


async def _gemini_to_client(gem, ws: WebSocket) -> None:
    try:
        async for raw in gem:
            content = json.loads(raw).get("serverContent") or {}
            for part in (content.get("modelTurn") or {}).get("parts", []):
                if "inlineData" in part:
                    await ws.send_bytes(base64.b64decode(part["inlineData"]["data"]))
                elif part.get("text"):
                    await ws.send_json({"type": "text", "text": part["text"], "model": LIVE_BADGE})
            if (content.get("outputTranscription") or {}).get("text"):
                await ws.send_json({"type": "text", "text": content["outputTranscription"]["text"], "model": LIVE_BADGE})
            if content.get("turnComplete"):
                await ws.send_json({"type": "turn_complete"})
    except websockets.exceptions.ConnectionClosedError:
        pass  # reported below with the close code
    # Quota / rate-limit errors arrive as a close frame (e.g. 1011 RESOURCE_EXHAUSTED).
    raise LiveUnavailable(f"Gemini Live closed ({gem.close_code}): {gem.close_reason or 'no reason'}")


# ── Fallback mode: text models via the LLMRouter ───────────────────────────────
async def _fallback_session(ws: WebSocket, inbox: asyncio.Queue) -> None:
    await ws.send_json({"type": "ready", "mode": "fallback", "model": "LLM Router (text)"})
    history: list[ChatMessage] = [ChatMessage(role="system", content=LANGUAGE_INSTRUCTION)]
    audio = bytearray()
    while True:
        msg = await inbox.get()
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("bytes"):
            audio += msg["bytes"]
            continue
        data = json.loads(msg.get("text") or "{}")
        if data.get("type") == "text":
            text = data.get("text", "")
        elif data.get("type") == "end_turn" and audio:
            text = await _transcribe(bytes(audio))
            audio.clear()
        else:
            continue
        if not text.strip():
            await ws.send_json({"type": "error", "error": "No speech-to-text available (set GROQ_API_KEY) — type instead"})
            continue
        history.append(ChatMessage(role="user", content=text))
        try:
            reply, _, _, provider, model_id, _ = await _llm_router.route(history)
        except Exception as exc:
            history.pop()
            await ws.send_json({"type": "error", "error": f"LLM provider error: {exc}"})
            continue
        history.append(reply)
        await ws.send_json({"type": "text", "text": reply.content, "model": f"{provider}/{model_id}"})
        await ws.send_json({"type": "turn_complete"})


async def _transcribe(pcm: bytes) -> str:
    """16 kHz PCM16 -> text via Groq Whisper; '' when no key or on failure."""
    key = get_settings().groq_api_key
    if not key:
        return ""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(GROQ_STT_URL, headers={"Authorization": f"Bearer {key}"},
                                  data={"model": "whisper-large-v3-turbo"},
                                  files={"file": ("speech.wav", buf.getvalue(), "audio/wav")})
            r.raise_for_status()
            return r.json().get("text", "")
    except httpx.HTTPError as exc:
        log.warning("live_voice.stt_failed", error=str(exc))
        return ""
