"""
brain/safety/tether.py — Tether & Safety Engine (Phase 3, Master Doc §20).

Three safeguards replace manual human approval:
  • Token auth   — see brain/api/routes/tether.py (kept there so this module stays FastAPI-free
                   for the lightweight edge daemon)
  • Kill-switch  — HALT / STOP / SHUTDOWN kills every tracked execution process tree
                   and blocks new executions until ``reset()``
  • Snapshots    — destructive file ops snapshot their target first; ``rollback(id)``
                   restores it (snapshotting the current state first, so rollback is undoable)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import structlog

log = structlog.get_logger(__name__)

BACKUP_ROOT = Path(__file__).parent / "backups"


# ── Task signing (Cloud Brain -> Edge Agent) ──────────────────────────────────
def sign_task(task: dict[str, Any], secret: str) -> str:
    """HMAC-SHA256 over the task (minus its ``sig``), so the edge runs only cloud-verified payloads."""
    body = json.dumps({k: v for k, v in task.items() if k != "sig"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify_task(task: dict[str, Any], secret: str) -> bool:
    return bool(secret) and isinstance(task.get("sig"), str) and hmac.compare_digest(task["sig"], sign_task(task, secret))


# ── Kill-switch ────────────────────────────────────────────────────────────────
def kill_tree(pid: int, timeout: float = 3.0) -> list[int]:
    """Kill a process and all its descendants; return the PIDs that were killed."""
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return []
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=timeout)
    return [p.pid for p in procs]


class KillSwitch:
    """Tracks running execution processes and terminates them all on command."""

    COMMANDS = frozenset({"HALT", "STOP", "SHUTDOWN"})

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.halted = False
        self.last_command: str | None = None

    def track(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
            halted = self.halted
        if halted:  # Spawned in the race window after a HALT — kill it too.
            kill_tree(proc.pid)

    def untrack(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    @property
    def active_pids(self) -> list[int]:
        with self._lock:
            return [p.pid for p in self._procs if p.poll() is None]

    def trigger(self, command: str) -> dict[str, Any]:
        """Engage the kill-switch: block new executions and kill every tracked process tree."""
        command = command.strip().upper()
        if command not in self.COMMANDS:
            raise ValueError(f"Unknown tether command {command!r}; expected one of {sorted(self.COMMANDS)}")
        with self._lock:
            self.halted = True
            self.last_command = command
            procs = list(self._procs)
        killed = [pid for p in procs for pid in kill_tree(p.pid)]
        log.warning("tether.kill_switch", command=command, killed=killed)
        return {"command": command, "halted": True, "killed_pids": killed}

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.halted = False
        log.info("tether.kill_switch_reset")
        return {"halted": False}


# ── Snapshot & rollback ────────────────────────────────────────────────────────
class SnapshotManager:
    """Copies files/directories aside before destructive operations, and restores them."""

    def __init__(self, root: Path | str = BACKUP_ROOT) -> None:
        self.root = Path(root)

    def snapshot(self, path: Path | str, reason: str = "") -> str:
        """Back up `path` (file, directory, or not-yet-existing) and return the snapshot id."""
        src = Path(path).expanduser().resolve()
        sid = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        dest = self.root / sid
        dest.mkdir(parents=True)
        if src.is_dir():
            shutil.copytree(src, dest / "data")
        elif src.exists():
            shutil.copy2(src, dest / "data")
        manifest = {
            "id": sid, "path": str(src), "existed": src.exists(), "is_dir": src.is_dir(),
            "reason": reason, "created": datetime.now(timezone.utc).isoformat(),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info("tether.snapshot", **manifest)
        return sid

    def manifest(self, sid: str) -> dict[str, Any]:
        return json.loads((self.root / sid / "manifest.json").read_text(encoding="utf-8"))

    def list_snapshots(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        return [self.manifest(d.name) for d in sorted(self.root.iterdir()) if (d / "manifest.json").exists()]

    def rollback(self, sid: str) -> dict[str, Any]:
        """Restore `path` to its state at snapshot `sid`. Returns the pre-rollback snapshot id."""
        m = self.manifest(sid)
        target, data = Path(m["path"]), self.root / sid / "data"
        pre = self.snapshot(target, reason=f"pre-rollback of {sid}")
        _remove(target)
        if m["existed"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            if m["is_dir"]:
                shutil.copytree(data, target)
            else:
                shutil.copy2(data, target)
        log.info("tether.rollback", snapshot=sid, path=str(target), pre_rollback=pre)
        return {"ok": True, "restored": str(target), "snapshot": sid, "pre_rollback_snapshot": pre}


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


# Process-wide singletons shared by the API and the execution agents.
kill_switch = KillSwitch()
snapshots = SnapshotManager()
