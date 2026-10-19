"""
test_windows_agent.py -- Safe smoke test for the Phase 2 Windows Execution Agent.

Only runs harmless commands (echo, dir) and writes inside a temp folder.

Run with:
    python test_windows_agent.py
"""
from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import asyncio
import tempfile
from pathlib import Path

from brain.agents import WindowsAgent
from brain.logging_config import setup_logging


def show(n: int, title: str, r) -> None:
    print(f"\n[{n}] {title}")
    print(f"    success={r.success} exit_code={r.exit_code} duration={r.duration_ms}ms")
    if r.stdout.strip():
        print("    stdout: " + r.stdout.strip().replace("\n", "\n            "))
    if r.stderr.strip():
        print("    stderr: " + r.stderr.strip()[:300])


async def main() -> None:
    setup_logging()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        agent = WindowsAgent(audit_log=tmp / "audit.jsonl")

        r = await agent.run_powershell("echo 'Hello from JARVIS'")
        show(1, "echo", r)
        assert r.success and "Hello from JARVIS" in r.stdout

        f = tmp / "sub" / "note.txt"
        r = await agent.write_file(f, "JARVIS Phase 2 test file")
        show(2, "write_file", r)
        assert r.success and f.exists()

        r = await agent.run_powershell(f'dir "{tmp / "sub"}"')
        show(3, "dir", r)
        assert r.success and "note.txt" in r.stdout

        r = await agent.read_file(f)
        show(4, "read_file", r)
        assert r.stdout == "JARVIS Phase 2 test file"

        r = await agent.run_powershell("exit 3")
        show(5, "failing command is reported, not raised", r)
        assert not r.success and r.exit_code == 3

        r = await agent.run_powershell("sleep 5", timeout=0.5)
        show(6, "timeout is enforced", r)
        assert not r.success and "Timed out" in r.stderr

        lines = (tmp / "audit.jsonl").read_text().splitlines()
        assert len(lines) == 6
        print(f"\nAudit log ({len(lines)} entries):")
        for line in lines:
            print("    " + line[:160])

    print("\nAll Windows Agent tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
