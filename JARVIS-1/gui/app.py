"""
gui/app.py — J.A.R.V.I.S. Desktop Control Center (Phase 8).

    python -m gui.app            (env: JARVIS_CLOUD_URL, TETHER_SECRET_TOKEN)

A PySide6 window hosting gui/web/jarvis_dashboard.html in QWebEngine, plus a system tray
icon (Show / TETHER HALT / Quit; closing the window hides it to the tray).

Python <-> page wiring goes over QWebChannel as the `jarvis` object (the Bridge below):
  page -> Python   halt(), sendText(text), setMic(on), replay()
  Python -> page   audioLevel, micLevel, textChunk, message, vitals, skills, haltResult

Networking (gui/client.py) runs on its own asyncio thread so the UI never blocks; mic
capture (16 kHz) and reply playback (24 kHz) use QtMultimedia.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QByteArray, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

from gui.client import LiveClient, pcm_level

DASHBOARD = Path(__file__).parent / "web" / "jarvis_dashboard.html"
STATUS_INTERVAL = 2.0


def _pcm_format(rate: int) -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


class Bridge(QObject):
    """The `jarvis` object the dashboard talks to. Signals may be emitted from the network thread."""

    audioLevel = Signal(float)
    micLevel = Signal(float)
    textChunk = Signal(str, str)
    message = Signal(str)
    vitals = Signal(str)
    skills = Signal(str)
    haltResult = Signal(str)
    _pcm = Signal(QByteArray)  # reply audio -> speaker, hopped onto the UI thread

    def __init__(self, cloud_url: str, token: str) -> None:
        super().__init__()
        self._encrypted = cloud_url.startswith("https://")
        self.loop = asyncio.new_event_loop()
        self.stop = asyncio.Event()
        self.client = LiveClient(cloud_url, token, on_audio=self._on_audio, on_message=self._on_message)
        self.speaker = QAudioSink(QMediaDevices.defaultAudioOutput(), _pcm_format(24000))
        self.speaker_io = self.speaker.start()
        self._pcm.connect(lambda data: self.speaker_io and self.speaker_io.write(data))
        self._latest: dict[str, tuple[Signal, str]] = {}  # replayed to a page that (re)connects
        self.mic: QAudioSource | None = None
        self.mic_io = None
        self.thread = threading.Thread(target=self._run_loop, name="jarvis-net", daemon=True)
        self.thread.start()

    encrypted = Property(bool, lambda self: self._encrypted, constant=True)

    # ── Network thread ────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._poll_status())
        self.loop.run_until_complete(self.client.run(self.stop))

    def _submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _poll_status(self) -> None:
        tick = 0
        while not self.stop.is_set():
            try:
                self._emit("vitals", self.vitals, (await self.client.edge_status())["last_seen"])
                if tick % 15 == 0:
                    self._emit("skills", self.skills, await self.client.skills())
            except Exception as exc:  # cloud unreachable: show it, keep polling
                self._emit("vitals", self.vitals, {"error": str(exc) or type(exc).__name__})
            tick += 1
            await asyncio.sleep(STATUS_INTERVAL)

    def _on_audio(self, pcm: bytes, level: float) -> None:
        self.audioLevel.emit(level)
        self._pcm.emit(QByteArray(pcm))

    def _on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "text":
            self.textChunk.emit(msg.get("text", ""), msg.get("model", ""))
        elif msg.get("type") in ("ready", "fallback"):
            self._emit(msg["type"], self.message, msg)
        else:
            self.message.emit(json.dumps(msg))

    def _emit(self, key: str, signal: Signal, payload: Any) -> None:
        self._latest[key] = (signal, json.dumps(payload))
        signal.emit(self._latest[key][1])

    async def _halt(self) -> None:
        t0 = time.perf_counter()
        try:
            res = await self.client.halt()
        except Exception as exc:
            res = {"halted": False, "error": str(exc) or type(exc).__name__}
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.haltResult.emit(json.dumps(res))

    # ── Called from the page ──────────────────────────────────────────────────
    @Slot()
    def replay(self) -> None:
        """Re-send the latest link state, vitals and skills (events before the page loaded are lost)."""
        for signal, payload in list(self._latest.values()):
            signal.emit(payload)

    @Slot()
    def halt(self) -> None:
        self._submit(self._halt())

    @Slot(str)
    def sendText(self, text: str) -> None:
        self._submit(self.client.send_text(text))

    @Slot(bool)
    def setMic(self, on: bool) -> None:
        if on and self.mic is None:
            self.mic = QAudioSource(QMediaDevices.defaultAudioInput(), _pcm_format(16000))
            self.mic_io = self.mic.start()
            if self.mic_io is None:
                self.mic = None
                self.message.emit(json.dumps({"type": "error", "error": "No microphone available"}))
                return
            self.mic_io.readyRead.connect(self._read_mic)
        elif not on and self.mic is not None:
            self.mic.stop()
            self.mic = self.mic_io = None
            self._submit(self.client.end_turn())

    def _read_mic(self) -> None:
        data = self.mic_io.readAll().data() if self.mic_io else b""
        if data:
            self.micLevel.emit(pcm_level(data))
            self._submit(self.client.send_audio(data))

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(timeout=5)


class Page(QWebEnginePage):
    """Keeps the page's console output (diagnostics + tests)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.console: list[tuple[str, str]] = []

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:
        self.console.append((level.name, message))


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S. Control Center")
        self.resize(1440, 900)
        self.bridge = bridge
        self.view = QWebEngineView(self)
        self.view.setPage(Page(self.view))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("jarvis", bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.setUrl(QUrl.fromLocalFile(str(DASHBOARD)))
        self.setCentralWidget(self.view)
        self.tray = self._make_tray() if QSystemTrayIcon.isSystemTrayAvailable() else None

    def _make_tray(self) -> QSystemTrayIcon:
        pix = QPixmap(64, 64)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#22d3ee"))
        p.setPen(QColor("#0e7490"))
        p.drawEllipse(8, 8, 48, 48)
        p.end()
        tray = QSystemTrayIcon(QIcon(pix), self)
        tray.setToolTip("J.A.R.V.I.S.")
        menu = QMenu()
        for label, fn in (("Show HUD", self.showNormal), ("TETHER HALT", self.bridge.halt),
                          ("Quit", QApplication.quit)):
            action = QAction(label, menu)
            action.triggered.connect(fn)
            menu.addAction(action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        self._tray_menu = menu  # keep a reference; Qt does not own it
        return tray

    def closeEvent(self, event) -> None:
        if self.tray is not None:
            event.ignore()
            self.hide()  # keep running in the tray
        else:
            event.accept()


def create_window(cloud_url: str, token: str) -> MainWindow:
    return MainWindow(Bridge(cloud_url, token))


def main() -> int:
    app = QApplication(sys.argv)
    window = create_window(os.environ.get("JARVIS_CLOUD_URL", "http://127.0.0.1:8000"),
                           os.environ.get("TETHER_SECRET_TOKEN", ""))
    app.setQuitOnLastWindowClosed(window.tray is None)
    window.show()
    app.aboutToQuit.connect(window.bridge.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
