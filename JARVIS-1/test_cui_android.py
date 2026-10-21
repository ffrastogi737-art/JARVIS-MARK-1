"""
test_cui_android.py -- Verifies Phase 9: 24/7 cloud deployment, terminal CUI, Android companion API.

No PySide6 / Chromium involved. A real Cloud Brain (uvicorn) runs against a local fake of the
Gemini Live API:
  [1] cloud deployment config (render.yaml + Dockerfile) and a production-mode boot on $PORT
  [2] CUI rendering integrity: no Qt/Chromium, Hindi/Hinglish text, keys, real terminal launch,
      flicker-free 2 Hz in-place repaint; --simple log stream mode
  [3] CUI <-> Cloud Brain: live chat, fallback, vitals, Ctrl+T HALT, voice toggle
  [4] Android API: auth, status, remote command -> laptop, chat, halt/reset, notifications,
      control WebSocket, phone audio relay

Run with:
    python test_cui_android.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TOKEN = "test-phase9-secret"
PORT, GEMINI_PORT = _free_port(), _free_port()
CLOUD = f"http://127.0.0.1:{PORT}"
os.environ.update(  # before settings are cached
    TETHER_SECRET_TOKEN=TOKEN, GOOGLE_API_KEY="fake-gemini-key", GROQ_API_KEY="",
    GEMINI_LIVE_URL=f"ws://127.0.0.1:{GEMINI_PORT}/live",
)

import httpx
import uvicorn
import websockets
import yaml

from brain.api.app import create_app
from brain.config import get_settings
from brain.safety.tether import kill_switch, verify_task

get_settings.cache_clear()
AUTH = {"X-Tether-Token": TOKEN}
api = httpx.Client(base_url=CLOUD + "/api/v1", headers=AUTH, timeout=5.0)


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<58} [OK] PASSED")


def wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return bool(cond())


async def await_cond(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return bool(cond())


def sleeper() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    kill_switch.track(proc)
    return proc


# ── Fake Gemini Live API ───────────────────────────────────────────────────────
GEMINI = {"mode": "ok", "setup": None, "text_in": [], "audio_in": 0}
REPLY_AUDIO = struct.pack("<4h", 9000, -9000, 9000, -9000) * 600


async def fake_gemini(conn) -> None:
    try:
        GEMINI["setup"] = json.loads(await conn.recv())["setup"]
        await conn.send(json.dumps({"setupComplete": {}}))
        async for raw in conn:
            msg = json.loads(raw)
            if GEMINI["mode"] == "quota":
                await conn.close(1011, "RESOURCE_EXHAUSTED: quota exceeded")
                return
            if "clientContent" in msg:
                text = msg["clientContent"]["turns"][0]["parts"][0]["text"]
                GEMINI["text_in"].append(text)
                reply = f"Ji sir, samajh gaya: {text}"
            elif "audio" in msg.get("realtimeInput", {}):
                GEMINI["audio_in"] += 1
                reply = "सुन रहा हूँ, sir."
            else:
                continue
            await conn.send(json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(REPLY_AUDIO).decode()}}]}}}))
            await conn.send(json.dumps({"serverContent": {"outputTranscription": {"text": reply}}}))
            await conn.send(json.dumps({"serverContent": {"turnComplete": True}}))
    except websockets.exceptions.ConnectionClosed:
        pass


def start_servers() -> uvicorn.Server:
    async def serve() -> None:
        async with websockets.serve(fake_gemini, "127.0.0.1", GEMINI_PORT):
            await asyncio.Future()
    threading.Thread(target=lambda: asyncio.run(serve()), daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_config=None,
                                           access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    assert wait(lambda: server.started, 15), "cloud server did not start"
    return server


# ── [1] Deployment config ──────────────────────────────────────────────────────
def test_deployment() -> None:
    print("\n[1] 24/7 cloud deployment config")
    blueprint = yaml.safe_load((ROOT.parent / "render.yaml").read_text(encoding="utf-8"))
    svc = blueprint["services"][0]
    check("render.yaml: docker web service rooted at JARVIS-1",
          svc["type"] == "web" and svc["runtime"] == "docker" and (ROOT.parent / svc["rootDir"]) == ROOT, svc)
    check("always-on plan (free tier sleeps) + single instance",
          svc["plan"] not in ("free", None) and svc["numInstances"] == 1, (svc["plan"], svc["numInstances"]))
    env = {e["key"]: e for e in svc["envVars"]}
    check("tether token generated, API keys kept out of git",
          env["TETHER_SECRET_TOKEN"].get("generateValue") is True
          and all(env[k].get("sync") is False for k in env if k.endswith("_API_KEY")), list(env))
    paths = {getattr(r, "path", None) for r in create_app().routes}
    check(f"healthCheckPath {svc['healthCheckPath']} is a real route", svc["healthCheckPath"] in paths)

    dockerfile = (ROOT / svc["dockerfilePath"]).read_text(encoding="utf-8")
    lines = [ln.split() for ln in dockerfile.splitlines() if ln and not ln.startswith(("#", " "))]
    copies = [ln[1] for ln in lines if ln[0] == "COPY"]
    check("Dockerfile COPY sources exist", all((ROOT / c).exists() for c in copies), copies)
    check("Dockerfile runs as non-root and starts main.py",
          any(ln[0] == "USER" and ln[1] != "root" for ln in lines) and 'CMD ["python", "main.py"]' in dockerfile)
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    check("cloud image stays free of GUI/CUI deps", not any(d in reqs for d in ("pyside6", "rich", "sounddevice")))
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    check(".dockerignore keeps secrets and laptop code out", all(x in ignored for x in (".env", "gui/", "cui/")))

    port = _free_port()
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "PORT": str(port), "ENVIRONMENT": "production", "HOST": "127.0.0.1"})
    try:
        def healthy() -> bool:
            try:
                return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200
            except httpx.HTTPError:
                return False
        check(f"container command boots in production mode on $PORT ({port})", wait(healthy, 20))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ── [2] CUI rendering integrity ────────────────────────────────────────────────
RENDER_CHILD = textwrap.dedent("""
    import io, json, sys, time, psutil
    from rich.console import Console
    from cui.app import CUI
    c = CUI("https://brain.example", "t", Console(file=io.StringIO(), width=110, height=34, record=True, color_system=None))
    c.link, c.model = "live", "Gemini Live Native Audio"
    c.chat.append(["YOU", "नमस्ते JARVIS, aaj ka plan kya hai?"])
    c.chat.append(["JARVIS", "Namaste sir! आज तीन meetings हैं।"])
    c.edge = {"at": time.time() - 3, "status": "idle", "cpu": 23.0, "ram": 61.0}
    c.console.print(c.render())
    heavy = [m for m in sys.modules if m.split(".")[0] in ("PySide6", "shiboken6", "httpx", "fastapi", "brain")]
    print(json.dumps({"screen": c.console.export_text(), "heavy": heavy,
                      "rss": psutil.Process().memory_info().rss / 2**20}))
""")


def test_cui_render() -> None:
    print("\n[2] Terminal CUI rendering integrity (no PySide6/Chromium)")
    out = subprocess.run([sys.executable, "-c", RENDER_CHILD], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    info = json.loads(out.stdout.strip().splitlines()[-1])
    screen = info["screen"]
    check("no Qt / Chromium / server modules loaded", not info["heavy"], info["heavy"])
    for needle in ("J.A.R.V.I.S.", "LIVE", "ENCRYPTED", "CLOUD BRAIN", "EDGE AGENT", "THIS MACHINE",
                   "Ctrl+T HALT", "Ctrl+R voice", "Gemini Live Native Audio", "DIAGNOSTICS"):
        assert needle in screen, needle
    check("HUD shows link, edge, metrics, chat and key hints", True)
    check("Hindi + Hinglish text rendered intact",
          "नमस्ते JARVIS, aaj ka plan kya hai?" in screen and "आज तीन meetings हैं।" in screen)
    check("edge laptop vitals rendered", "23%" in screen and "61%" in screen and "3s ago" in screen)
    print(f"      (measured: CUI process {info['rss']:.1f} MB RSS)")

    from cui.app import CTRL_R, CTRL_T, CUI
    c = CUI("http://x", "t")
    c.feed("Kya haal hai")
    c.feed("\x7f\x7f\x7f")
    c.feed("\x1b[A")  # arrow key: ignored
    check("typing, Backspace, escape sequences handled", c.buffer == "Kya haal ")
    c.feed("\x08")
    check("Ctrl+H acts as Backspace (why HALT is Ctrl+T)", c.buffer == "Kya haal")
    check("control keys never land in the input line", not any(k in c.buffer for k in (CTRL_T, CTRL_R)))

    if sys.platform == "win32":
        print("      (real-terminal launch check is POSIX-only; skipped)")
        return
    import pty
    import select
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execvpe(sys.executable, [sys.executable, "cli.py", "--url", CLOUD, "--token", TOKEN],
                   {**os.environ, "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "36"})
    screen = b""
    deadline = time.monotonic() + 20
    while b"Ctrl+T HALT" not in screen and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            screen += os.read(fd, 65536)
    check("`python cli.py` draws the HUD in a real terminal", b"Ctrl+T HALT" in screen)
    check("HUD uses the alternate screen buffer", b"\x1b[?1049h" in screen)
    idle = b""
    t_end = time.monotonic() + 4.0
    while time.monotonic() < t_end:  # idle: only the 2 Hz clock may repaint
        if select.select([fd], [], [], 0.1)[0]:
            idle += os.read(fd, 65536)
    frames = idle.count(b"Ctrl+T HALT")
    check(f"idle repaint locked to ~2 Hz ({frames} frames in 4 s)", 4 <= frames <= 10, frames)
    check("frames overwrite in place: no full-screen clears", b"\x1b[2J" not in screen + idle)
    os.write(fd, b"abc")
    typed = b""
    t_end = time.monotonic() + 0.4
    while b"abc" not in typed and time.monotonic() < t_end:
        if select.select([fd], [], [], 0.02)[0]:
            typed += os.read(fd, 65536)
    check("keystrokes repaint at once (no 2 Hz input lag)", b"abc" in typed)
    os.write(fd, b"\x11")  # Ctrl+Q
    deadline = time.monotonic() + 10
    status = None
    while status is None and time.monotonic() < deadline:
        done, code = os.waitpid(pid, os.WNOHANG)
        status = code if done else None
        if select.select([fd], [], [], 0.1)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                pass
    os.close(fd)
    check("Ctrl+Q exits cleanly", status is not None and os.waitstatus_to_exitcode(status) == 0, status)


def test_cui_simple() -> None:
    print("\n[2b] CUI --simple log stream mode")
    GEMINI["mode"] = "ok"
    proc = subprocess.Popen([sys.executable, "cli.py", "--simple", "--url", CLOUD, "--token", TOKEN], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "120"})
    out: list[str] = []
    threading.Thread(target=lambda: out.extend(iter(proc.stdout.readline, b"")), daemon=True).start()
    seen = lambda needle: any(needle in line.decode("utf-8", "replace") for line in out)  # noqa: E731
    try:
        check("stream mode connects and logs the live link", wait(lambda: seen("Link up · live"), 15), out)
        proc.stdin.write("Namaste JARVIS, kya haal hai?\n".encode())
        proc.stdin.flush()
        check("typed Hinglish line answered as one log line",
              wait(lambda: seen("JARVIS › Ji sir, samajh gaya: Namaste JARVIS, kya haal hai?"), 10), out)
        api.get("/edge/poll?status=idle&cpu=21&ram=37")
        check("edge daemon state streamed", wait(lambda: seen("Edge: idle · laptop cpu 21% · ram 37%"), 10), out)
        sleeper_proc = sleeper()
        proc.stdin.write(b"/halt\n")
        proc.stdin.flush()
        check("/halt engages the tether from stream mode",
              wait(lambda: seen("TETHER HALT engaged") and sleeper_proc.poll() is not None, 10), out)
        kill_switch.untrack(sleeper_proc)
        kill_switch.reset()
        proc.stdin.write(b"/quit\n")
        proc.stdin.flush()
        check("/quit exits cleanly", proc.wait(timeout=10) == 0, proc.stderr.read().decode()[-500:])
        raw = b"".join(out)
        check("plain stream: no cursor moves, clears or alternate screen",
              not any(seq in raw for seq in (b"\x1b[?1049h", b"\x1b[2J", b"\x1b[H", b"\x1b[A")), raw[:300])
    finally:
        proc.kill()


# ── [3] CUI <-> Cloud Brain ────────────────────────────────────────────────────
async def cui_session() -> None:
    from rich.console import Console
    from cui.app import CTRL_Q, CTRL_R, CTRL_T, CUI

    def fresh() -> CUI:
        return CUI(CLOUD, TOKEN, Console(file=io.StringIO(), width=110, height=34, record=True, color_system=None))

    GEMINI["mode"] = "ok"
    c = fresh()
    net = asyncio.create_task(c.connect())
    check("CUI connects: live link to Gemini Live", await await_cond(lambda: c.link == "live"), c.link)
    lang = GEMINI["setup"]["systemInstruction"]["parts"][0]["text"]
    check("Gemini told to answer in Hindi / English / Hinglish",
          all(w in lang for w in ("Hindi", "English", "Hinglish")), lang)
    c.feed("Lights band karo please\r")
    check("typed Hinglish turn reaches Gemini", await await_cond(lambda: "Lights band karo please" in GEMINI["text_in"]))
    check("reply streamed into the chat log", await await_cond(
        lambda: c.chat and c.chat[-1] == ["JARVIS", "Ji sir, samajh gaya: Lights band karo please"] and not c.streaming),
        list(c.chat))
    check("incoming audio drives the voice meter", c.level > 0.1, c.level)

    api.get("/edge/poll?status=idle&cpu=33&ram=44")
    check("edge agent status polled from the cloud", await await_cond(lambda: c.edge.get("cpu") == 33, 5), c.edge)

    proc = sleeper()
    api.get("/edge/poll")  # drain
    c.feed(CTRL_T)
    check("Ctrl+T -> TETHER HALT, process killed",
          await await_cond(lambda: c.halt_result is not None and proc.poll() is not None, 5), c.halt_result)
    check(f"halt round-trip {c.halt_result.get('ms')} ms, edge notified",
          c.halt_result["halted"] and api.get("/edge/poll").json()["tasks"][0]["command"] == "HALT", c.halt_result)
    kill_switch.untrack(proc)
    kill_switch.reset()

    c.feed(CTRL_R)
    check("Ctrl+R voice: on, or a clear error without a mic (no crash)",
          c.voice or any("Voice unavailable" in line for _, line, _ in c.log), list(c.log)[-2:])
    if c.voice:
        c.feed(CTRL_R)
    c.console.print(c.render())
    check("HUD renders the live session", "Ji sir, samajh gaya" in c.console.export_text())
    c.feed(CTRL_Q)
    await asyncio.wait_for(net, 10)
    check("Ctrl+Q closes the link cleanly", net.done() and not c.client.connected)

    GEMINI["mode"] = "quota"
    c = fresh()
    net = asyncio.create_task(c.connect())
    await await_cond(lambda: c.link == "live")
    c.feed("मौसम कैसा है?\r")
    check("Gemini quota -> CUI switches to text fallback", await await_cond(lambda: c.link == "fallback"), c.link)
    c.feed("मौसम कैसा है?\r")
    check("Hindi turn answered by the LLM Router fallback",
          await await_cond(lambda: c.chat[-1][0] == "JARVIS" and "मौसम कैसा है?" in c.chat[-1][1], 15), list(c.chat))
    c.stop.set()
    await asyncio.wait_for(net, 10)
    GEMINI["mode"] = "ok"


def test_cui_live() -> None:
    print("\n[3] CUI <-> Cloud Brain (Gemini Live WebSocket, tether, vitals)")
    asyncio.run(cui_session())


# ── [4] Android API ────────────────────────────────────────────────────────────
async def android_ws() -> None:
    base = f"ws://127.0.0.1:{PORT}/api/v1/android"
    for path in ("/control", "/voice"):
        try:
            async with websockets.connect(base + path, additional_headers={"X-Tether-Token": "nope"}):
                status = "accepted"
        except websockets.exceptions.InvalidStatus as exc:
            status = exc.response.status_code
        check(f"WS {path}: bad token -> upgrade refused (403)", status == 403, status)

    async with websockets.connect(f"{base}/control?token={TOKEN}") as ws:
        await ws.send(json.dumps({"action": "status"}))
        r = json.loads(await ws.recv())
        check("WS control handshake (?token=) + status action", r["type"] == "result" and r["ok"] and "edge" in r, r)
        await ws.send("{not json")
        r = json.loads(await ws.recv())
        check("malformed message -> error, link stays up", r["ok"] is False, r)
        await ws.send(json.dumps({"action": "chat", "text": "Good morning JARVIS"}))
        r = json.loads(await ws.recv())
        check("WS chat action answered", r["ok"] and "Good morning JARVIS" in r["reply"], r)

        proc = sleeper()
        api.post("/android/control", json={"action": "halt"})
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        check("background notification pushed over WS",
              msg["type"] == "notification" and msg["kind"] == "tether" and proc.pid in msg["killed_pids"], msg)
        kill_switch.untrack(proc)
        api.post("/android/control", json={"action": "reset"})

    async with websockets.connect(f"{base}/voice", additional_headers=AUTH) as ws:
        ready = json.loads(await ws.recv())
        check("phone voice relay: handshake -> Gemini Live", ready["mode"] == "live", ready)
        await ws.send(struct.pack("<4h", 1000, -1000, 1000, -1000) * 400)  # phone mic, 16 kHz PCM16
        audio = await asyncio.wait_for(ws.recv(), 5)
        text = json.loads(await asyncio.wait_for(ws.recv(), 5))
        check("phone mic audio relayed -> speaker audio + text back",
              audio == REPLY_AUDIO and GEMINI["audio_in"] >= 1 and text["text"] == "सुन रहा हूँ, sir.", text)


def test_android() -> None:
    print("\n[4] Android companion API")
    for headers in ({}, {"X-Tether-Token": "nope"}):
        r = httpx.get(CLOUD + "/api/v1/android/control", headers=headers, timeout=5.0)
        check(f"REST {'no' if not headers else 'wrong'} token -> 401", r.status_code == 401, r.status_code)

    api.get("/edge/poll?status=idle&cpu=18&ram=52")  # laptop heartbeat
    s = api.get("/android/control").json()
    check("24/7 status poll: cloud, tether, laptop vitals",
          s["cloud"]["online"] and s["tether"]["halted"] is False and s["edge"]["online"]
          and s["edge"]["cpu"] == 18, s)

    since = s["last_seq"]
    r = api.post("/android/control", json={"action": "command", "command": "Get-Date", "shell": "powershell"})
    tid = r.json()["task_id"]
    task = api.get("/edge/poll").json()["tasks"][0]
    check("remote command -> signed task for the laptop",
          r.status_code == 200 and task["id"] == tid and task["command"] == "Get-Date"
          and verify_task(task, TOKEN), task)
    api.post("/edge/results", json={"results": [{"id": tid, "ok": True, "stdout": "Tuesday 21 October 2026"}]})
    notes = api.get(f"/android/notifications?since={since}").json()["notifications"]
    check("laptop result arrives as a phone notification",
          any(n["kind"] == "task_done" and n["task_id"] == tid and "2026" in n["output"] for n in notes), notes)

    r = api.post("/android/control", json={"action": "chat", "text": "Kal ka weather batao"})
    check("chat action answered via LLM Router", r.status_code == 200 and "Kal ka weather batao" in r.json()["reply"], r.text)
    check("empty command rejected (400)",
          api.post("/android/control", json={"action": "command", "command": "  "}).status_code == 400)
    check("unknown action rejected (422)", api.post("/android/control", json={"action": "format_c"}).status_code == 422)

    proc = sleeper()
    api.get("/edge/poll")  # drain
    t0 = time.perf_counter()
    r = api.post("/android/control", json={"action": "halt"})
    ms = (time.perf_counter() - t0) * 1000
    check(f"phone HALT kills processes in {ms:.0f} ms", r.json()["halted"] and proc.poll() is not None, r.json())
    kill_switch.untrack(proc)
    check("phone HALT also sent to the laptop", api.get("/edge/poll").json()["tasks"][0]["command"] == "HALT")
    api.post("/android/control", json={"action": "reset"})
    check("phone RESET re-enables execution (cloud + laptop)",
          not kill_switch.halted and api.get("/edge/poll").json()["tasks"][0]["command"] == "RESET")

    asyncio.run(android_ws())


if __name__ == "__main__":
    print("=" * 66)
    print("  JARVIS Phase 9 -- Cloud Deployment, Terminal CUI & Android API Tests")
    print("=" * 66)
    server = start_servers()
    try:
        test_deployment()
        test_cui_render()
        test_cui_simple()
        test_cui_live()
        test_android()
    finally:
        kill_switch.reset()
        server.should_exit = True
    print("\nAll Phase 9 tests passed.")
