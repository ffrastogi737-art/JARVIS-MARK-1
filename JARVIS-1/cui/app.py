"""
cui/app.py — J.A.R.V.I.S. terminal Control Center (Phase 9).

    python cli.py            (env: JARVIS_CLOUD_URL, TETHER_SECRET_TOKEN)
    python cli.py --simple   plain scrolling log, line input (low-end PCs, old consoles)

A full-screen Rich HUD for the laptop, no Qt/Chromium: Cloud Brain link, Edge Agent
heartbeat, local CPU/RAM, and a streaming chat with JARVIS over /api/v1/live/voice
(Hindi, English or Hinglish — the Brain answers in the user's language).

Keys
  Ctrl+T   TETHER HALT (cloud + laptop). Not Ctrl+H: terminals send Ctrl+H for Backspace.
  Ctrl+R   voice on/off (mic -> Gemini Live; reply audio played back); off = end of turn
  Enter    send the typed line          Ctrl+Q / Ctrl+C   quit
  /halt  /voice  /quit  typed commands also work (the only controls in --simple)

Networking reuses gui/client.py (stdlib HTTP + websockets).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import deque
from typing import Any

import psutil
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gui.client import LiveClient, pcm_level

CTRL_C, CTRL_Q, CTRL_R, CTRL_T = "\x03", "\x11", "\x12", "\x14"
BACKSPACE = ("\x7f", "\x08")
STATUS_INTERVAL = 2.0
ACCENT, DIM, GOOD, WARN, CRIT = "cyan", "grey50", "green", "yellow", "bold red"


class CUI:
    def __init__(self, cloud_url: str, token: str, console: Console | None = None) -> None:
        self.console = console or Console()
        self.client = LiveClient(cloud_url, token, on_audio=self._on_audio, on_message=self._on_message)
        self.cloud_url = cloud_url
        self.link = "connecting"     # connecting | live | fallback | down
        self.model = "—"
        self.edge: dict[str, Any] = {}
        self.chat: deque[list[str]] = deque(maxlen=200)  # [who, text]; JARVIS lines grow while streaming
        self.log: deque[tuple[str, str]] = deque(maxlen=50)
        self.buffer = ""
        self.streaming = False
        self.voice = False
        self.level = 0.0
        self.halt_result: dict[str, Any] | None = None
        self.stop = asyncio.Event()
        self.stream = False          # --simple: print events as lines instead of drawing the HUD
        self._edge_said, self._edge_said_at = "", 0.0
        self._live: Live | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic = self._speaker = None
        self._playback = bytearray()

    # ── Events from the Brain ─────────────────────────────────────────────────
    def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ready":
            self.link, self.model = msg["mode"], msg["model"]
            self._log(f"Link up · {msg['mode']} · {msg['model']}", GOOD)
        elif kind == "fallback":
            self._log(f"Gemini Live unavailable → text fallback ({msg['reason']})", WARN)
        elif kind == "text":
            if not self.streaming:
                self.chat.append(["JARVIS", ""])
                self.streaming = True
            self.chat[-1][1] += msg.get("text", "")
            if msg.get("model"):
                self.model = msg["model"]
        elif kind == "turn_complete":
            self.streaming = False
            if self.stream and self.chat and self.chat[-1][0] == "JARVIS":
                self.console.print(Text.assemble(("JARVIS › ", f"bold {ACCENT}"), self.chat[-1][1]))
        elif kind == "error":
            if "Cloud link down" in msg.get("error", ""):
                self.link = "down"
            self._log(msg.get("error", "error"), CRIT)

    def _on_audio(self, pcm: bytes, level: float) -> None:
        self.level = max(self.level, level)
        if self._speaker is not None:
            self._playback += pcm

    def _log(self, text: str, style: str = "") -> None:
        self.log.append((time.strftime("%H:%M:%S"), text, style))
        if self.stream:
            self.console.print(Text.assemble((self.log[-1][0] + " ", DIM), (text, style)))

    # ── Keys ──────────────────────────────────────────────────────────────────
    def handle_key(self, key: str) -> None:
        if key in (CTRL_Q, CTRL_C):
            self.stop.set()
        elif key == CTRL_T:
            self._spawn(self.halt())
        elif key == CTRL_R:
            self.toggle_voice()
        elif key in ("\r", "\n"):
            self._submit()
        elif key in BACKSPACE:
            self.buffer = self.buffer[:-1]
        elif key.isprintable():
            self.buffer += key

    def feed(self, chunk: str) -> None:
        """Raw terminal input -> keys. Escape sequences (arrows, F-keys) are ignored."""
        if chunk.startswith("\x1b"):
            return
        for ch in chunk:
            self.handle_key(ch)

    def _submit(self) -> None:
        text, self.buffer = self.buffer.strip(), ""
        if not text:
            return
        if text == "/halt":
            self._spawn(self.halt())
            return
        if text == "/voice":
            self.toggle_voice()
            return
        if text == "/quit":
            self.stop.set()
            return
        self.chat.append(["YOU", text])
        self.streaming = False
        if not self.client.connected:
            self._log("Not connected to the Cloud Brain; message not sent", CRIT)
            return
        self._spawn(self.client.send_text(text))

    def _spawn(self, coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    async def halt(self) -> None:
        t0 = time.perf_counter()
        self._log("TETHER HALT requested…", CRIT)
        try:
            res = await self.client.halt()
            res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self._log(f"TETHER HALT engaged in {res['ms']} ms · killed {len(res['killed_pids'])} · edge notified", CRIT)
        except Exception as exc:
            res = {"halted": False, "error": str(exc) or type(exc).__name__}
            self._log(f"TETHER HALT FAILED: {res['error']}", CRIT)
        self.halt_result = res

    # ── Voice (lazy: sounddevice/PortAudio only load when first used) ──────────
    def toggle_voice(self) -> None:
        if self.voice:
            for stream in (self._mic, self._speaker):
                if stream is not None:
                    stream.stop()
                    stream.close()
            self._mic = self._speaker = None
            self.voice = False
            self._spawn(self.client.end_turn())
            self._log("Voice off · turn sent")
            return
        try:
            import sounddevice as sd
            self._mic = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                          blocksize=1600, callback=self._mic_callback)
            self._speaker = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16",
                                               callback=self._speaker_callback)
            self._mic.start()
            self._speaker.start()
        except Exception as exc:  # no PortAudio, no device, permission denied...
            self._mic = self._speaker = None
            self._log(f"Voice unavailable: {exc}", CRIT)
            return
        self.voice = True
        self._log("Voice live: speak (Hindi / English / Hinglish); Ctrl+R to finish", GOOD)

    def _mic_callback(self, data, frames, time_info, status) -> None:  # PortAudio thread
        pcm = bytes(data)
        self.level = max(self.level, pcm_level(pcm))
        asyncio.run_coroutine_threadsafe(self.client.send_audio(pcm), self._loop)

    def _speaker_callback(self, outdata, frames, time_info, status) -> None:  # PortAudio thread
        n = len(outdata)
        chunk = bytes(self._playback[:n])
        del self._playback[:n]
        outdata[:] = chunk + b"\x00" * (n - len(chunk))

    # ── Rendering ─────────────────────────────────────────────────────────────
    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(Layout(self._header(), size=3), Layout(name="body"), Layout(self._input(), size=3))
        layout["body"].split_row(Layout(self._status(), size=38), Layout(self._chat()))
        self.level *= 0.6  # decays per frame so the meter falls back after speech
        return layout

    def _header(self) -> Panel:
        link_style = {"live": GOOD, "fallback": WARN}.get(self.link, CRIT)
        t = Text.assemble(
            ("J.A.R.V.I.S.", f"bold {ACCENT}"), "   ",
            (f"● {self.link.upper()}", link_style), "   ",
            ("● ENCRYPTED" if self.cloud_url.startswith("https") else "▲ UNENCRYPTED",
             GOOD if self.cloud_url.startswith("https") else WARN), "   ",
            (f"VOICE {'ON ' + '▮' * int(self.level * 10) if self.voice else 'off'}", GOOD if self.voice else DIM),
            "   ", (time.strftime("%H:%M:%S"), DIM),
        )
        hint = Text("Ctrl+T HALT · Ctrl+R voice · Ctrl+Q quit", style=DIM, justify="right")
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(t, hint)
        return Panel(grid, border_style=ACCENT)

    def _status(self) -> Panel:
        rows = Table.grid(padding=(0, 1))
        rows.add_column(style=DIM)
        rows.add_column()
        rows.add_row("CLOUD BRAIN", "")
        rows.add_row(" link", Text(self.link, style={"live": GOOD, "fallback": WARN}.get(self.link, CRIT)))
        rows.add_row(" model", Text(self.model, overflow="fold"))
        rows.add_row("", "")
        e = self.edge
        age = time.time() - e["at"] if e.get("at") else None
        rows.add_row("EDGE AGENT", "")
        if e.get("error"):
            rows.add_row(" state", Text("cloud unreachable", style=CRIT))
        else:
            rows.add_row(" state", Text(e.get("status") or "never seen", style=GOOD if age is not None and age < 60 else WARN))
            rows.add_row(" heartbeat", "—" if age is None else f"{age:.0f}s ago")
            rows.add_row(" laptop cpu", _meter(e.get("cpu")))
            rows.add_row(" laptop ram", _meter(e.get("ram")))
        rows.add_row("", "")
        rows.add_row("THIS MACHINE", "")
        rows.add_row(" cpu", _meter(psutil.cpu_percent(interval=None)))
        rows.add_row(" ram", _meter(psutil.virtual_memory().percent))
        rows.add_row(" cui rss", f"{psutil.Process().memory_info().rss / 2**20:.1f} MB")
        if self.halt_result:
            rows.add_row("", "")
            ok = self.halt_result.get("halted")
            rows.add_row("TETHER", Text(f"HALTED in {self.halt_result.get('ms')} ms" if ok else "HALT FAILED", style=CRIT))
        log = Text()
        for ts, line, style in list(self.log)[-8:]:
            log.append(f"{ts} ", DIM).append(line + "\n", style)
        return Panel(Group(rows, Text(""), Text("DIAGNOSTICS", style=DIM), log), title="Status", border_style=ACCENT)

    def _chat(self) -> Panel:
        body = Text()
        for who, text in list(self.chat)[-40:]:
            body.append(f"{who:>6} ", f"bold {ACCENT}" if who == "JARVIS" else "bold")
            body.append(text + ("▌" if who == "JARVIS" and self.streaming and [who, text] == self.chat[-1] else "") + "\n")
        if not self.chat:
            body.append("Type or press Ctrl+R to talk — Hindi, English or Hinglish.", DIM)
        return Panel(body, title=f"Chat · {self.model}", border_style=ACCENT)

    def _input(self) -> Panel:
        return Panel(Text.assemble(("› ", ACCENT), self.buffer, ("▌", ACCENT)), border_style=ACCENT)

    # ── Run ───────────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Network side only (used by run() and by tests): live link + status polling."""
        self._loop = asyncio.get_running_loop()
        psutil.cpu_percent(interval=None)  # prime: the first reading is meaningless
        await asyncio.gather(self.client.run(self.stop), self._poll_status())

    async def _poll_status(self) -> None:
        while not self.stop.is_set():
            try:
                self.edge = (await self.client.edge_status())["last_seen"]
            except Exception as exc:
                self.edge = {"error": str(exc)}
            if self.stream:
                self._say_edge()
            try:
                await asyncio.wait_for(self.stop.wait(), STATUS_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        net = asyncio.create_task(self.connect())
        # One repaint path at 2 Hz, drawn in place on the alternate screen (cursor home +
        # overwrite, never a clear); a keypress repaints at once so typing never lags.
        with Live(get_renderable=self.render, console=self.console, screen=True,
                  refresh_per_second=2, transient=True) as self._live:
            with _raw_terminal(self._on_keys):
                await self.stop.wait()
        self._live = None
        await self._shutdown(net)

    def _on_keys(self, chunk: str) -> None:
        self.feed(chunk)
        if self._live is not None:
            self._live.refresh()

    async def run_simple(self) -> None:
        """--simple: no HUD, no cursor tricks — a scrolling log and line input."""
        self.stream = True
        url = self.cloud_url
        self.console.print(Text.assemble(("J.A.R.V.I.S.", f"bold {ACCENT}"), f" stream mode · {url}"))
        self.console.print(Text("Type and press Enter to chat · /halt · /voice · /quit (or Ctrl+C)", style=DIM))
        net = asyncio.create_task(self.connect())
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_stdin() -> None:  # daemon thread: a blocked readline must not hold up exit
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line.rstrip("\r\n"))
            loop.call_soon_threadsafe(lines.put_nowait, None)  # EOF

        threading.Thread(target=read_stdin, name="cui-stdin", daemon=True).start()
        stopper = asyncio.create_task(self.stop.wait())
        while not self.stop.is_set():
            getter = asyncio.create_task(lines.get())
            await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if not getter.done():
                getter.cancel()
                break
            line = getter.result()
            if line is None:
                self.stop.set()
                break
            self.buffer = line
            self._submit()
        await self._shutdown(net)

    def _say_edge(self) -> None:
        """Stream mode: one line when the laptop's daemon changes state, else every ~30 s."""
        e = self.edge
        if e.get("error"):
            line = "Edge: cloud unreachable"
        elif not e.get("at"):
            line = "Edge: daemon never seen"
        else:
            line = f"Edge: {e.get('status')} · laptop cpu {e.get('cpu') or 0:.0f}% · ram {e.get('ram') or 0:.0f}%"
        state = line.split(" · ")[0]
        if state != self._edge_said.split(" · ")[0] or time.time() - self._edge_said_at > 30:
            self._edge_said, self._edge_said_at = line, time.time()
            self._log(line)

    async def _shutdown(self, net: asyncio.Task) -> None:
        if self.voice:
            self.toggle_voice()
        await asyncio.wait_for(net, 5)


def _meter(pct: float | None, width: int = 12) -> Text:
    if pct is None:
        return Text("--", style=DIM)
    filled = round(pct / 100 * width)
    return Text.assemble(("█" * filled, WARN if pct >= 85 else ACCENT), ("░" * (width - filled), DIM), f" {pct:4.0f}%")


class _raw_terminal:
    """Deliver keystrokes unbuffered to `on_input` while the HUD runs; restore the terminal after."""

    def __init__(self, on_input) -> None:
        self.on_input = on_input

    def __enter__(self):
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            self.task = loop.create_task(self._poll_windows())
            return self
        import termios
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        # No line buffering/echo; Ctrl+C/Q/T/R arrive as keys (no signals, flow control or SIGINFO).
        mode[0] &= ~(termios.IXON | termios.ICRNL)
        mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, mode)
        loop.add_reader(self.fd, lambda: self.on_input(os.read(self.fd, 1024).decode("utf-8", "ignore")))
        return self

    async def _poll_windows(self) -> None:
        import msvcrt
        while True:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # arrow/F-key prefix: drop the key code too
                    msvcrt.getwch()
                else:
                    self.on_input(ch)
            await asyncio.sleep(0.03)

    def __exit__(self, *exc) -> None:
        if sys.platform == "win32":
            self.task.cancel()
            return
        import termios
        asyncio.get_running_loop().remove_reader(self.fd)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
