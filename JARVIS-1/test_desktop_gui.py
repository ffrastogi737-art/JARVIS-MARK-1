"""
test_desktop_gui.py -- Verifies Phase 8: Gemini Live voice relay + desktop HUD Control Center.

Runs a real Cloud Brain (uvicorn) wired to a local fake of the Gemini Live WebSocket API,
and the real PySide6/QWebEngine HUD (offscreen on headless machines):
  [1] Gemini Live WebSocket route: auth, handshake, bidirectional audio, quota fallback
  [2] Live audio amplitude parser
  [3] GUI launcher, dashboard asset integrity, live vitals & skills panels
  [4] Gemini audio driving the 3D orb pulse, text stream under the orb
  [5] TETHER HALT button: < 200 ms process termination, edge notified

Run with:
    python test_desktop_gui.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TOKEN = "test-gui-secret"
PORT, GEMINI_PORT = _free_port(), _free_port()
CLOUD = f"http://127.0.0.1:{PORT}"
os.environ.update(  # before settings are cached
    TETHER_SECRET_TOKEN=TOKEN, GOOGLE_API_KEY="fake-gemini-key", GROQ_API_KEY="",
    GEMINI_LIVE_URL=f"ws://127.0.0.1:{GEMINI_PORT}/live",
)
if sys.platform != "win32" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(os, "geteuid") and os.geteuid() == 0:
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")  # Chromium refuses root otherwise

import httpx
import psutil
import uvicorn
import websockets

from brain.api.app import create_app
from brain.config import get_settings
from brain.safety.tether import kill_switch, verify_task
from gui.client import pcm_level

get_settings.cache_clear()
AUTH = {"X-Tether-Token": TOKEN}
HTML = Path(__file__).parent / "gui" / "web" / "jarvis_dashboard.html"


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<56} [OK] PASSED")


def tone(amplitude: float, n: int = 2400, rate: int = 24000) -> bytes:
    return struct.pack(f"<{n}h", *(int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))


# ── Fake Gemini Live API ───────────────────────────────────────────────────────
GEMINI = {"mode": "ok", "setup": None, "audio_in": [], "text_in": []}
REPLY_AUDIO = tone(0.9)


async def fake_gemini(conn) -> None:
    try:
        await _fake_gemini(conn)
    except websockets.exceptions.ConnectionClosed:
        pass  # the Brain hung up first (end of a test session)


async def _fake_gemini(conn) -> None:
    GEMINI["setup"] = json.loads(await conn.recv())
    await conn.send(json.dumps({"setupComplete": {}}))
    async for raw in conn:
        msg = json.loads(raw)
        if GEMINI["mode"] == "quota":
            await conn.close(1011, "RESOURCE_EXHAUSTED: quota exceeded for native audio")
            return  # the async-for then ends: the peer acknowledges the close
        if "realtimeInput" in msg and "audio" in msg["realtimeInput"]:
            GEMINI["audio_in"].append(msg["realtimeInput"]["audio"])
        elif "clientContent" in msg:
            GEMINI["text_in"].append(msg["clientContent"]["turns"][0]["parts"][0]["text"])
        else:
            continue
        for _ in range(4):  # a few loud chunks so the orb has something to pulse with
            await conn.send(json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(REPLY_AUDIO).decode()}}]}}}))
            await asyncio.sleep(0.05)
        await conn.send(json.dumps({"serverContent": {"outputTranscription": {"text": "At your service, sir."}}}))
        await conn.send(json.dumps({"serverContent": {"turnComplete": True}}))


def start_fake_gemini() -> None:
    async def serve() -> None:
        async with websockets.serve(fake_gemini, "127.0.0.1", GEMINI_PORT):
            await asyncio.Future()
    threading.Thread(target=lambda: asyncio.run(serve()), daemon=True).start()


# ── [1] WebSocket route ────────────────────────────────────────────────────────
async def _recv_json(ws, want: str, timeout: float = 5.0) -> dict:
    """Next JSON message of type `want`, skipping audio frames."""
    deadline = time.monotonic() + timeout
    while True:
        frame = await asyncio.wait_for(ws.recv(), max(0.01, deadline - time.monotonic()))
        if isinstance(frame, str) and (msg := json.loads(frame))["type"] == want:
            return msg


async def ws_checks() -> None:
    url = f"ws://127.0.0.1:{PORT}/api/v1/live/voice"
    for bad in ({}, {"X-Tether-Token": "wrong"}):
        try:
            async with websockets.connect(url, additional_headers=bad):
                status = "accepted"
        except websockets.exceptions.InvalidStatus as exc:
            status = exc.response.status_code
        check(f"{'no' if not bad else 'wrong'} token -> upgrade refused (HTTP 403)", status == 403, status)

    GEMINI["mode"] = "ok"
    async with websockets.connect(url, additional_headers=AUTH) as ws:
        ready = await _recv_json(ws, "ready")
        check("handshake -> ready / live / Gemini badge",
              ready["mode"] == "live" and ready["model"] == "Gemini Live Native Audio", ready)
        setup = GEMINI["setup"]["setup"]
        check("setup sent model + AUDIO response modality",
              setup["model"] == f"models/{get_settings().gemini_live_model}"
              and setup["generationConfig"]["responseModalities"] == ["AUDIO"], setup)
        mic = tone(0.3, 1600, 16000)
        await ws.send(mic)
        audio = await asyncio.wait_for(ws.recv(), 5)
        sent = GEMINI["audio_in"][-1]
        check("mic PCM relayed as 16 kHz realtimeInput",
              base64.b64decode(sent["data"]) == mic and sent["mimeType"] == "audio/pcm;rate=16000", sent["mimeType"])
        check("Gemini native audio relayed back as binary PCM", audio == REPLY_AUDIO, len(audio))
        text = await _recv_json(ws, "text")
        check("transcription streamed as text with model badge",
              text["text"] == "At your service, sir." and text["model"] == "Gemini Live Native Audio", text)
        check("turn_complete forwarded", (await _recv_json(ws, "turn_complete"))["type"] == "turn_complete")

    GEMINI["mode"] = "quota"
    async with websockets.connect(url, additional_headers=AUTH) as ws:
        await _recv_json(ws, "ready")
        await ws.send(tone(0.3, 1600, 16000))
        fb = await _recv_json(ws, "fallback")
        check("quota close (1011) -> fallback with reason", "RESOURCE_EXHAUSTED" in fb["reason"], fb)
        ready = await _recv_json(ws, "ready")
        check("session continues in text fallback mode", ready["mode"] == "fallback", ready)
        await ws.send(json.dumps({"type": "text", "text": "status report"}))
        reply = await _recv_json(ws, "text", timeout=15)
        check("text turn answered by LLM Router fallback",
              "status report" in reply["text"] and "/" in reply["model"], reply)
        await ws.send(b"\x00\x01" * 800)
        await ws.send(json.dumps({"type": "end_turn"}))
        err = await _recv_json(ws, "error")
        check("audio in fallback without STT key -> clear error", "speech-to-text" in err["error"], err)
    GEMINI["mode"] = "ok"


def test_ws_route() -> None:
    print("\n[1] Gemini Live WebSocket route handshake & fallback pipeline")
    asyncio.run(ws_checks())


# ── [2] Amplitude parser (pure) ───────────────────────────────────────────────
def test_amplitude_parser() -> None:
    print("\n[2] Live audio amplitude streaming parser")
    check("silence -> 0", pcm_level(b"\x00\x00" * 512) == 0.0)
    check("empty chunk -> 0", pcm_level(b"") == 0.0)
    full = struct.pack("<4h", 32767, -32768, 32767, -32768)
    check("full-scale square -> ~1", pcm_level(full) > 0.99, pcm_level(full))
    lvl = pcm_level(tone(0.5))
    check(f"half-amplitude sine -> sqrt(RMS) = {lvl:.3f}", abs(lvl - math.sqrt(0.5 / math.sqrt(2))) < 0.01, lvl)
    check("louder audio -> larger level", pcm_level(tone(0.2)) < pcm_level(tone(0.6)) < pcm_level(tone(0.9)))
    check("odd-length chunk tolerated", 0 < pcm_level(tone(0.5) + b"\x01") <= 1)


# ── Qt helpers ─────────────────────────────────────────────────────────────────
def spin(app, cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    return bool(cond())


def js(app, window, expr: str, timeout: float = 5.0):
    box: list = []
    window.view.page().runJavaScript(expr, 0, box.append)
    assert spin(app, lambda: box, timeout), f"JS timed out: {expr}"
    return box[0]


def test_gui() -> None:
    print("\n[3] PySide6 GUI launcher & dashboard asset integrity")
    html = HTML.read_text(encoding="utf-8")
    for needle in ("J.A.R.V.I.S.", "#020912", 'id="halt"', 'id="orb"', 'id="response"', 'id="model-badge"',
                   "Gemini Live Native Audio", 'id="cpu"', 'id="ram"', 'id="radar"', 'id="wave"',
                   'id="skills"', 'id="log"', 'id="clock"', "qrc:///qtwebchannel/qwebchannel.js"):
        assert needle in html, needle
    check("dashboard has every HUD element", True)
    for pill in ("ONLINE", "SECURE", "ENCRYPTED", "LIVE-GEMINI"):
        assert f">{pill}<" in html, pill
    check("status pills ONLINE/SECURE/ENCRYPTED/LIVE-GEMINI", True)
    external = re.findall(r"""(?:src|href)=["']https?://""", html) + re.findall(r"url\(\s*['\"]?https?://", html)
    check("no external network assets (works offline)", not external, external)

    from PySide6.QtWidgets import QApplication
    from gui.app import create_window
    app = QApplication.instance() or QApplication(sys.argv)
    window = create_window(CLOUD, TOKEN)
    window.show()
    loaded: list = []
    window.view.loadFinished.connect(loaded.append)
    check("dashboard loads in QWebEngine", spin(app, lambda: loaded, 30) and loaded[0])
    check("QWebChannel bridge connected", spin(app, lambda: js(app, window, "!!window.hud.bridge"), 15))
    check("live link: ONLINE + LIVE-GEMINI pills lit", spin(app, lambda: js(
        app, window, "document.getElementById('pill-live').className.includes('on') && "
                     "document.getElementById('pill-online').className.includes('on')"), 15))
    check("plain-http cloud flagged UNENCRYPTED, not faked",
          js(app, window, "document.getElementById('pill-encrypted').textContent") == "UNENCRYPTED")
    check("live clock ticking", re.fullmatch(r"\d\d:\d\d:\d\d", js(app, window, "document.getElementById('clock').textContent")))

    httpx.get(CLOUD + "/api/v1/edge/poll?status=idle&cpu=12.5&ram=41.0", headers=AUTH, timeout=5.0)
    check("edge daemon CPU/RAM rendered in vitals", spin(app, lambda: js(
        app, window, "document.getElementById('cpu').textContent + '|' + document.getElementById('ram').textContent")
        == "13%|41%", 10))
    check("skill registry panel populated", spin(app, lambda: js(
        app, window, "document.getElementById('skills').textContent") != "Loading…", 10))
    errors = [m for lvl, m in window.view.page().console if "Error" in lvl]
    check("no JavaScript errors", not errors, errors)

    launcher = subprocess.Popen([sys.executable, "-m", "gui.app"], cwd=Path(__file__).parent,
                                env={**os.environ, "JARVIS_CLOUD_URL": CLOUD},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    try:
        alive = launcher.poll() is None
        proc = psutil.Process(launcher.pid)
        main_mb = proc.memory_info().rss / 2**20
        helpers_mb = sum(c.memory_info().rss for c in proc.children(recursive=True)) / 2**20
    finally:
        launcher.kill()
        launcher.wait()
    check(f"`python -m gui.app` launches and stays up", alive)
    print(f"      (measured: UI process {main_mb:.0f} MB + QtWebEngine helpers {helpers_mb:.0f} MB RSS)")

    test_orb_pulse(app, window)
    test_halt(app, window)
    window.bridge.shutdown()


# ── [4] Orb pulse end-to-end ──────────────────────────────────────────────────
def test_orb_pulse(app, window) -> None:
    print("\n[4] Gemini audio -> 3D orb pulse, text stream")
    check("orb at rest before speech", js(app, window, "hud.level") < 0.05)
    js(app, window, "document.getElementById('prompt').value='hello jarvis';"
                    "document.getElementById('composer').requestSubmit(); true")
    check("typed turn reached Gemini Live", spin(app, lambda: "hello jarvis" in GEMINI["text_in"], 10))
    check("orb pulses with incoming audio amplitude", spin(app, lambda: js(
        app, window, "Math.max(...hud.levels, 0) > 0.4 && hud.orbRadius > 1.0"), 10))
    check("reply text streamed under orb", spin(app, lambda: "At your service, sir." in js(
        app, window, "document.getElementById('response').textContent"), 10))
    check("model badge shows Gemini Live Native Audio",
          js(app, window, "document.getElementById('model-badge').textContent") == "Gemini Live Native Audio")
    check("orb decays back after speech", spin(app, lambda: js(app, window, "hud.level") < 0.05, 10))


# ── [5] TETHER HALT ────────────────────────────────────────────────────────────
def test_halt(app, window) -> None:
    print("\n[5] TETHER HALT button")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    kill_switch.track(proc)
    httpx.get(CLOUD + "/api/v1/edge/poll", headers=AUTH, timeout=5.0)  # drain the edge queue
    try:
        js(app, window, "true")  # page idle before timing
        t0 = time.perf_counter()
        window.view.page().runJavaScript("document.getElementById('halt').click()")
        spin(app, lambda: proc.poll() is not None, 5)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        check(f"click -> process terminated in {elapsed_ms:.0f} ms (< 200 ms)",
              proc.poll() is not None and elapsed_ms < 200, elapsed_ms)
        check("kill-switch engaged", kill_switch.halted)
        spin(app, lambda: js(app, window, "hud.lastHalt !== null"), 5)
        res = json.loads(js(app, window, "JSON.stringify(hud.lastHalt)"))
        check(f"HUD shows halt round-trip ({res and res['ms']} ms)",
              res and res["halted"] and proc.pid in res["killed_pids"] and res["ms"] < 200, res)
        tasks = httpx.get(CLOUD + "/api/v1/edge/poll", headers=AUTH, timeout=5.0).json()["tasks"]
        check("signed HALT queued for the edge daemon",
              len(tasks) == 1 and tasks[0]["command"] == "HALT" and verify_task(tasks[0], TOKEN), tasks)
        r = httpx.post(CLOUD + "/api/v1/agent/tether/halt", timeout=5.0)
        check("halt endpoint rejects missing token", r.status_code == 401, r.status_code)
    finally:
        kill_switch.untrack(proc)
        proc.kill()
        kill_switch.reset()


if __name__ == "__main__":
    print("=" * 64)
    print("  JARVIS Phase 8 -- Gemini Live Audio Engine & 3D Orb HUD Tests")
    print("=" * 64)
    start_fake_gemini()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_config=None,
                                           access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "cloud server did not start"
    try:
        test_ws_route()
        test_amplitude_parser()
        test_gui()
    finally:
        kill_switch.reset()
        server.should_exit = True
    print("\nAll Phase 8 Desktop GUI tests passed.")
