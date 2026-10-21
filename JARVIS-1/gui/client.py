"""
gui/client.py — Async client from the desktop HUD to the Cloud Brain (Phase 8).

  • LiveClient.run()   keeps a WebSocket to /api/v1/live/voice open (reconnecting with
                       backoff), forwarding mic PCM up and Gemini audio/text chunks down
  • halt()             POST /api/v1/agent/tether/halt — the HUD's panic button
  • edge_status()      GET /api/v1/edge/status — laptop CPU/RAM from the Edge Daemon heartbeat
  • skills()           GET /api/v1/skills
  • pcm_level(chunk)   PCM16 chunk -> 0..1 loudness that drives the orb pulse

No Qt and no httpx here (stdlib HTTP keeps it light), so the terminal CUI (cui/) reuses it
and it is testable headless; gui/app.py runs it on a background asyncio loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import urllib.request
from array import array
from typing import Any, Callable

import websockets


def pcm_level(chunk: bytes) -> float:
    """RMS loudness of little-endian mono PCM16, scaled to 0..1 (sqrt-curved so speech is visible)."""
    samples = array("h", chunk[: len(chunk) & ~1])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return min(1.0, math.sqrt(rms))


async def _close_on(stop: asyncio.Event, ws) -> None:
    await stop.wait()
    await ws.close()


class LiveClient:
    def __init__(
        self, cloud_url: str, token: str,
        on_audio: Callable[[bytes, float], None] = lambda pcm, level: None,
        on_message: Callable[[dict[str, Any]], None] = lambda msg: None,
    ) -> None:
        self.cloud_url = cloud_url.rstrip("/")
        self.ws_url = self.cloud_url.replace("http", "ws", 1) + "/api/v1/live/voice"
        self.headers = {"X-Tether-Token": token}
        self.on_audio, self.on_message = on_audio, on_message
        self._ws = None

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.ws_url, additional_headers=self.headers, max_size=None) as ws:
                    self._ws, delay = ws, 1.0
                    closer = asyncio.create_task(_close_on(stop, ws))  # else a quiet link ignores stop
                    try:
                        async for frame in ws:
                            if isinstance(frame, bytes):
                                self.on_audio(frame, pcm_level(frame))
                            else:
                                self.on_message(json.loads(frame))
                    finally:
                        closer.cancel()
            except (websockets.exceptions.WebSocketException, OSError) as exc:
                self.on_message({"type": "error", "error": f"Cloud link down: {exc}"})
            finally:
                self._ws = None
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws:
            await self._ws.send(pcm)

    async def send_text(self, text: str) -> None:
        if self._ws:
            await self._ws.send(json.dumps({"type": "text", "text": text}))

    async def end_turn(self) -> None:
        if self._ws:
            await self._ws.send(json.dumps({"type": "end_turn"}))

    def _request(self, method: str, path: str) -> Any:
        req = urllib.request.Request(self.cloud_url + "/api/v1" + path, method=method, headers=self.headers)
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # raises HTTPError on 4xx/5xx
            return json.loads(resp.read())

    async def halt(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "POST", "/agent/tether/halt")

    async def edge_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", "/edge/status")

    async def skills(self) -> list[dict[str, Any]]:
        return (await asyncio.to_thread(self._request, "GET", "/skills"))["skills"]
