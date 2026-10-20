"""
brain/daemon/cloud_sync.py — Offline buffer & verified code updates for the Edge Agent (Phase 7).

  • buffer(result)    — a result that could not reach the Cloud Brain is appended to a
                        JSON-lines file, so it survives a reboot while offline
  • flush(send)       — on reconnect, sends the buffered results in one batch; keeps them
                        on disk if the send fails
  • apply_update(t)   — writes a cloud-signed module update under brain/ (snapshotted first,
                        syntax-checked) and hot-reloads it in the running daemon

Tasks themselves are buffered on the Cloud Brain while the laptop is offline
(see brain/api/routes/edge.py) and arrive on the first poll after reconnecting.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import structlog

from brain.safety.tether import snapshots

log = structlog.get_logger(__name__)

BRAIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUFFER = Path.home() / ".jarvis" / "edge_buffer.jsonl"


class CloudSync:
    def __init__(self, buffer_path: Path | str = DEFAULT_BUFFER, brain_root: Path | str = BRAIN_ROOT) -> None:
        self.buffer_path = Path(buffer_path)
        self.brain_root = Path(brain_root).resolve()
        self._lock = threading.Lock()

    def buffer(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.buffer_path.parent.mkdir(parents=True, exist_ok=True)
            with self.buffer_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result) + "\n")
        log.info("cloud_sync.buffered", id=result.get("id"))

    def pending(self) -> list[dict[str, Any]]:
        if not self.buffer_path.exists():
            return []
        lines = self.buffer_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def flush(self, send: Callable[[list[dict[str, Any]]], None]) -> int:
        """Send every buffered result via `send`; the buffer is cleared only if `send` succeeds."""
        with self._lock:
            items = self.pending()
            if not items:
                return 0
            send(items)  # raises when still offline -> buffer kept
            self.buffer_path.unlink()
        log.info("cloud_sync.flushed", count=len(items))
        return len(items)

    def apply_update(self, task: dict[str, Any]) -> dict[str, Any]:
        """Install a signed module update (caller verifies the signature) and hot-reload it."""
        module, source = task.get("module", ""), task.get("source", "")
        parts = module.split(".")
        if parts[0] != "brain" or len(parts) < 2 or not all(p.isidentifier() for p in parts):
            return {"ok": False, "error": f"Refusing update outside the brain package: {module!r}"}
        path = self.brain_root.joinpath(*parts[1:]).with_suffix(".py")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            return {"ok": False, "error": f"SyntaxError: {exc}"}
        sid = snapshots.snapshot(path, reason=f"edge update {module}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        # A same-size rewrite within one mtime tick would otherwise reload the stale .pyc.
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
        reloaded: list[str] = []
        if module in sys.modules:
            # Lazy: HotReloader pulls in FastAPI, which the idle daemon should not pay for.
            from brain.evolution.hot_reloader import HotReloader
            reloaded = HotReloader().reload([module])
        log.info("cloud_sync.update_applied", module=module, snapshot_id=sid, reloaded=reloaded)
        return {"ok": True, "path": str(path), "snapshot_id": sid, "reloaded": reloaded}
