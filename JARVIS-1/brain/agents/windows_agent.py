"""
brain/agents/windows_agent.py — Windows Execution Agent (Phase 2).

Gives the Brain hands on the host machine:
  • Shell     — PowerShell / CMD with stdout+stderr capture and timeouts
  • Processes — list, inspect, terminate (via psutil)
  • Files     — read, write, delete, list directory, metadata
  • Tether    — executions are tracked by the kill-switch; overwrites/deletes are snapshotted

Every operation returns a plain dict and is logged with structlog. Errors are
captured into the result (``ok: False``) instead of raised, so the caller (an
LLM tool loop) always gets something it can reason about.

Runs on Windows natively; on Linux/macOS PowerShell works via ``pwsh`` and CMD
is reported as unavailable.

shortcut: methods are synchronous; call them via asyncio.to_thread from async code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import structlog

from brain.safety.tether import SnapshotManager, kill_switch, kill_tree, snapshots

log = structlog.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TIMEOUT = 30.0


class WindowsAgent:
    """Executes shell, process and file operations on the local machine."""

    def __init__(
        self, default_timeout: float = DEFAULT_TIMEOUT, snapshot_manager: SnapshotManager | None = None
    ) -> None:
        self.default_timeout = default_timeout
        self.snapshots = snapshot_manager or snapshots
        # Windows PowerShell 5.1 ships with Windows; pwsh is PowerShell 7+.
        self.powershell = shutil.which("powershell") or shutil.which("pwsh")

    # ── Shell execution ────────────────────────────────────────────────────────
    def _run(self, op: str, argv: list[str], timeout: float | None, cwd: str | None) -> dict[str, Any]:
        timeout = timeout or self.default_timeout
        t0 = time.monotonic()
        result: dict[str, Any] = {
            "ok": False, "returncode": None, "stdout": "", "stderr": "", "timed_out": False,
            "halted": False,
        }
        if kill_switch.halted:
            result["stderr"] = f"Tether kill-switch engaged ({kill_switch.last_command}); execution blocked"
            result.update(halted=True, duration_ms=0.0)
            log.warning(f"windows_agent.{op}", command=argv[-1][:200], ok=False, halted=True)
            return result
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", cwd=cwd,
            )
        except OSError as exc:
            result["stderr"] = str(exc)
        else:
            kill_switch.track(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the whole tree: orphaned grandchildren would hold the pipes open.
                kill_tree(proc.pid)
                stdout, _ = proc.communicate()
                stderr = f"Timed out after {timeout}s"
                result["timed_out"] = True
            finally:
                kill_switch.untrack(proc)
            halted = kill_switch.halted
            result.update(
                ok=proc.returncode == 0 and not result["timed_out"] and not halted,
                returncode=proc.returncode, stdout=stdout, stderr=stderr, halted=halted,
            )
        result["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)

        log.info(
            f"windows_agent.{op}",
            command=argv[-1][:200], ok=result["ok"], returncode=result["returncode"],
            timed_out=result["timed_out"], halted=result["halted"], duration_ms=result["duration_ms"],
        )
        return result

    def run_powershell(self, command: str, timeout: float | None = None, cwd: str | None = None) -> dict[str, Any]:
        """Run a PowerShell command; returns ok/returncode/stdout/stderr/timed_out/duration_ms."""
        if not self.powershell:
            return _unavailable("powershell", "PowerShell not found on PATH")
        return self._run(
            "powershell",
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            timeout, cwd,
        )

    def run_cmd(self, command: str, timeout: float | None = None, cwd: str | None = None) -> dict[str, Any]:
        """Run a CMD command (Windows only)."""
        if not IS_WINDOWS:
            return _unavailable("cmd", "CMD is only available on Windows")
        return self._run("cmd", ["cmd.exe", "/d", "/c", command], timeout, cwd)

    # ── Process management ─────────────────────────────────────────────────────
    def list_processes(self, name_filter: str = "") -> dict[str, Any]:
        """List running processes, optionally filtered by case-insensitive name substring."""
        needle = name_filter.lower()
        procs = [
            p.info for p in psutil.process_iter(["pid", "name", "status", "username"])
            if needle in (p.info["name"] or "").lower()
        ]
        log.info("windows_agent.list_processes", filter=name_filter, count=len(procs))
        return {"ok": True, "processes": procs}

    def process_status(self, pid: int) -> dict[str, Any]:
        """Return status details for one PID."""
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                info = {
                    "pid": pid, "name": p.name(), "status": p.status(),
                    "running": p.is_running(),
                    "cpu_percent": p.cpu_percent(interval=None),
                    "memory_rss": p.memory_info().rss,
                    "created": datetime.fromtimestamp(p.create_time(), timezone.utc).isoformat(),
                }
            result = {"ok": True, **info}
        except psutil.NoSuchProcess:
            result = {"ok": False, "pid": pid, "running": False, "error": "No such process"}
        except psutil.AccessDenied:
            result = {"ok": False, "pid": pid, "error": "Access denied"}
        log.info("windows_agent.process_status", pid=pid, ok=result["ok"])
        return result

    def terminate_process(self, pid: int, timeout: float = 5.0) -> dict[str, Any]:
        """Terminate a PID gracefully, force-killing it if it outlives `timeout`."""
        if pid == os.getpid():
            result = {"ok": False, "pid": pid, "error": "Refusing to terminate JARVIS itself"}
        else:
            try:
                p = psutil.Process(pid)
                p.terminate()
                try:
                    p.wait(timeout=timeout)
                    forced = False
                except psutil.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=timeout)
                    forced = True
                result = {"ok": True, "pid": pid, "forced": forced}
            except psutil.NoSuchProcess:
                result = {"ok": False, "pid": pid, "error": "No such process"}
            except (psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                result = {"ok": False, "pid": pid, "error": type(exc).__name__}
        log.info("windows_agent.terminate_process", **result)
        return result

    # ── File & directory I/O ───────────────────────────────────────────────────
    def read_file(self, path: str, encoding: str = "utf-8") -> dict[str, Any]:
        """Read a text file."""
        return self._file_op("read_file", path, lambda p: {"content": p.read_text(encoding=encoding)})

    def write_file(self, path: str, content: str, encoding: str = "utf-8", append: bool = False) -> dict[str, Any]:
        """Write (or append) text to a file. An existing file is snapshotted first (Tether)."""
        def op(p: Path) -> dict[str, Any]:
            sid = self.snapshots.snapshot(p, reason="write_file") if p.exists() else None
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a" if append else "w", encoding=encoding) as f:
                written = f.write(content)
            return {"chars_written": written, "snapshot_id": sid}
        return self._file_op("write_file", path, op)

    def delete_path(self, path: str) -> dict[str, Any]:
        """Delete a file or directory tree after snapshotting it (Tether)."""
        def op(p: Path) -> dict[str, Any]:
            if not p.exists():
                raise FileNotFoundError(str(p))
            sid = self.snapshots.snapshot(p, reason="delete_path")
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            return {"snapshot_id": sid}
        return self._file_op("delete_path", path, op)

    def list_directory(self, path: str) -> dict[str, Any]:
        """List the entries of a directory."""
        def op(p: Path) -> dict[str, Any]:
            return {"entries": [
                {"name": e.name, "is_dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else None}
                for e in sorted(p.iterdir())
            ]}
        return self._file_op("list_directory", path, op)

    def file_info(self, path: str) -> dict[str, Any]:
        """Return existence, type, size and timestamps for a path."""
        def op(p: Path) -> dict[str, Any]:
            if not p.exists():
                return {"exists": False}
            st = p.stat()
            return {
                "exists": True, "is_file": p.is_file(), "is_dir": p.is_dir(), "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "created": datetime.fromtimestamp(st.st_ctime, timezone.utc).isoformat(),
            }
        return self._file_op("file_info", path, op)

    def _file_op(self, op: str, path: str, fn) -> dict[str, Any]:
        p = Path(path).expanduser().resolve()
        try:
            result = {"ok": True, "path": str(p), **fn(p)}
        except (OSError, UnicodeError) as exc:
            result = {"ok": False, "path": str(p), "error": f"{type(exc).__name__}: {exc}"}
        log.info(f"windows_agent.{op}", path=str(p), ok=result["ok"], error=result.get("error"))
        return result


def _unavailable(op: str, reason: str) -> dict[str, Any]:
    log.warning(f"windows_agent.{op}", ok=False, reason=reason)
    return {
        "ok": False, "returncode": None, "stdout": "", "stderr": reason,
        "timed_out": False, "halted": False, "duration_ms": 0.0,
    }
