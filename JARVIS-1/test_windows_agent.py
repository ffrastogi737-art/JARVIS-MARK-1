"""
test_windows_agent.py -- Verifies the Phase 2 Windows Execution Agent on the local machine.

Run with:
    python test_windows_agent.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from brain.agents.windows_agent import IS_WINDOWS, WindowsAgent

agent = WindowsAgent()


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<42} [OK] PASSED")


def test_shell() -> None:
    print("\n[a] Shell execution")
    r = agent.run_powershell("Get-Date -Format o; Write-Output 'jarvis-echo'")
    check("PowerShell Get-Date + echo", r["ok"] and "jarvis-echo" in r["stdout"], r)

    r = agent.run_powershell("Write-Error 'boom'; exit 3")
    check("PowerShell stderr + exit code captured", not r["ok"] and r["returncode"] == 3 and "boom" in r["stderr"], r)

    r = agent.run_powershell("Start-Sleep -Seconds 10", timeout=2)
    check("PowerShell timeout enforced", r["timed_out"] and r["duration_ms"] < 9000, r)

    r = agent.run_cmd("echo jarvis-cmd")
    if IS_WINDOWS:
        check("CMD echo", r["ok"] and "jarvis-cmd" in r["stdout"], r)
    else:
        check("CMD reported unavailable off Windows", not r["ok"] and "only available" in r["stderr"], r)


def test_files() -> None:
    print("\n[b] File I/O")
    with tempfile.TemporaryDirectory(prefix="jarvis_test_") as tmp:
        path = Path(tmp) / "sub" / "hello.txt"
        text = "Hello from JARVIS — ✓ unicode\n"

        check("write_file (creates parent dirs)", agent.write_file(str(path), text)["ok"])
        r = agent.read_file(str(path))
        check("read_file round-trip", r["ok"] and r["content"] == text, r)

        agent.write_file(str(path), "line2\n", append=True)
        check("write_file append", agent.read_file(str(path))["content"] == text + "line2\n")

        r = agent.list_directory(str(path.parent))
        check("list_directory", r["ok"] and [e["name"] for e in r["entries"]] == ["hello.txt"], r)

        r = agent.file_info(str(path))
        check("file_info metadata", r["exists"] and r["is_file"] and r["size"] > 0, r)

        check("file_info on missing path", agent.file_info(str(Path(tmp) / "nope"))["exists"] is False)
        r = agent.read_file(str(Path(tmp) / "nope"))
        check("read_file missing -> error, no raise", not r["ok"] and "FileNotFoundError" in r["error"], r)
    check("temp files cleaned up", not path.exists())


def test_processes() -> None:
    print("\n[c] Process management")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        r = agent.list_processes()
        check("list_processes includes child", any(p["pid"] == child.pid for p in r["processes"]))

        r = agent.process_status(child.pid)
        check("process_status running", r["ok"] and r["running"], r)

        r = agent.terminate_process(child.pid)
        check("terminate_process", r["ok"], r)
        check("child actually exited", child.wait(timeout=5) is not None)

        check("process_status after exit", not agent.process_status(child.pid)["ok"])
        check("terminate missing PID -> error", not agent.terminate_process(child.pid)["ok"])

        check("refuses to terminate itself", not agent.terminate_process(os.getpid())["ok"])
    finally:
        if child.poll() is None:
            child.kill()


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 2 -- Windows Execution Agent Tests")
    print(f"  Platform: {sys.platform}   PowerShell: {agent.powershell}")
    print("=" * 60)
    test_shell()
    test_files()
    test_processes()
    print("\nAll Windows Agent tests passed.")
