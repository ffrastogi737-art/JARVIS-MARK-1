"""
brain/agents/windows_agent.py — Windows Root Execution Agent (Master Doc §7).

Executes PowerShell commands, writes files and reads system output with no
human approval step. Every action is logged via structlog and appended to a
JSONL audit trail (execution history for the §6 dashboard).

Shell selection:
  Windows        → powershell.exe -NoProfile -NonInteractive -Command
  other + pwsh   → pwsh (PowerShell Core)
  other, no pwsh → sh -c   (lets the agent run in Linux CI / the cloud Brain)

Run with Administrator rights on Windows for full-privilege workflows.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ExecutionResult:
    action: str
    target: str
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    if pwsh := shutil.which("pwsh"):
        return [pwsh, "-NoProfile", "-NonInteractive", "-Command", command]
    return ["sh", "-c", command]


class WindowsAgent:
    """Autonomous executor. All methods return an ExecutionResult and never raise."""

    def __init__(self, audit_log: str | Path = "logs/windows_agent.jsonl", timeout: float = 120.0) -> None:
        self.audit_log = Path(audit_log)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def _record(self, result: ExecutionResult) -> ExecutionResult:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **asdict(result)}
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.info(
            "windows_agent.executed",
            action=result.action,
            target=result.target[:200],
            success=result.success,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )
        return result

    async def run_powershell(self, command: str, timeout: float | None = None) -> ExecutionResult:
        """Run a shell command and capture stdout/stderr/exit code."""
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *_shell_argv(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout or self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                out, err = b"", f"Timed out after {timeout or self.timeout}s".encode()
            code = proc.returncode
        except OSError as exc:  # shell binary missing, permission denied, …
            out, err, code = b"", str(exc).encode(), None
        return self._record(ExecutionResult(
            action="shell",
            target=command,
            success=code == 0,
            exit_code=code,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_ms=round((time.monotonic() - t0) * 1000, 1),
        ))

    async def write_file(self, path: str | Path, content: str) -> ExecutionResult:
        """Create or overwrite a file (parent folders created as needed)."""
        t0 = time.monotonic()
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            ok, out, err = True, f"Wrote {len(content)} chars to {p.resolve()}", ""
        except OSError as exc:
            ok, out, err = False, "", str(exc)
        return self._record(ExecutionResult(
            "write_file", str(path), ok, None, out, err, round((time.monotonic() - t0) * 1000, 1)
        ))

    async def read_file(self, path: str | Path) -> ExecutionResult:
        """Read a text file; content is returned in stdout."""
        t0 = time.monotonic()
        try:
            ok, out, err = True, Path(path).read_text(encoding="utf-8", errors="replace"), ""
        except OSError as exc:
            ok, out, err = False, "", str(exc)
        return self._record(ExecutionResult(
            "read_file", str(path), ok, None, out, err, round((time.monotonic() - t0) * 1000, 1)
        ))
