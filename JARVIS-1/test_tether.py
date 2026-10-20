"""
test_tether.py -- Verifies the Phase 3 Tether & Safety Engine.

Run with:
    python test_tether.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

os.environ["TETHER_SECRET_TOKEN"] = "test-tether-secret"  # before settings are cached

import psutil
from fastapi.testclient import TestClient

from brain.agents.windows_agent import WindowsAgent
from brain.api.app import create_app
from brain.config import get_settings
from brain.safety.tether import SnapshotManager, kill_switch

get_settings.cache_clear()
client = TestClient(create_app())
AUTH = {"X-Tether-Token": "test-tether-secret"}


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<46} [OK] PASSED")


def _dead(pid: int) -> bool:
    # An orphan killed in a container may linger as a zombie until init reaps it.
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_token_auth() -> None:
    print("\n[1] Token authentication")
    check("no token -> 401", client.get("/api/v1/tether/status").status_code == 401)
    r = client.get("/api/v1/tether/status", headers={"X-Tether-Token": "wrong"})
    check("wrong token -> 401", r.status_code == 401, r.text)
    r = client.post("/api/v1/tether/command", json={"command": "HALT"})
    check("HALT without token -> 401, not engaged", r.status_code == 401 and not kill_switch.halted)
    r = client.get("/api/v1/tether/status", headers=AUTH)
    check("correct token -> 200", r.status_code == 200 and r.json()["halted"] is False, r.text)
    r = client.post("/api/v1/tether/command", json={"command": "DANCE"}, headers=AUTH)
    check("unknown command -> 400", r.status_code == 400, r.text)
    check("public /health still open", client.get("/health").status_code == 200)

    os.environ["TETHER_SECRET_TOKEN"] = ""
    get_settings.cache_clear()
    r = client.get("/api/v1/tether/status", headers={"X-Tether-Token": ""})
    check("unset secret fails closed -> 503", r.status_code == 503, r.text)
    os.environ["TETHER_SECRET_TOKEN"] = "test-tether-secret"
    get_settings.cache_clear()


def test_kill_switch() -> None:
    print("\n[2] HALT / STOP / SHUTDOWN kill-switch")
    agent = WindowsAgent()

    # (a) A long agent command is killed by HALT via the API
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(agent.run_powershell("Start-Sleep -Seconds 60", timeout=90)))
    t.start()
    deadline = time.monotonic() + 15
    while not kill_switch.active_pids and time.monotonic() < deadline:
        time.sleep(0.05)
    check("agent process tracked by kill-switch", bool(kill_switch.active_pids))

    t0 = time.monotonic()
    r = client.post("/api/v1/tether/command", json={"command": "halt"}, headers=AUTH)
    t.join(timeout=10)
    elapsed = time.monotonic() - t0
    check("HALT accepted", r.status_code == 200 and r.json()["killed_pids"], r.text)
    check(f"agent command terminated ({elapsed:.2f}s)", not t.is_alive() and elapsed < 5, out)
    check("result flagged halted, not ok", out.get("halted") and not out.get("ok"), out)

    r = agent.run_powershell("Write-Output should-not-run")
    check("new executions blocked while halted", r["halted"] and "should-not-run" not in r["stdout"], r)
    client.post("/api/v1/tether/reset", headers=AUTH)
    check("reset re-enables execution", agent.run_powershell("Write-Output ok")["ok"])

    # (b) STOP / SHUTDOWN kill a whole process tree (child + grandchild)
    for cmd in ("STOP", "SHUTDOWN"):
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,sys,time;"
             "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
             "print(c.pid,flush=True);time.sleep(60)"],
            stdout=subprocess.PIPE, text=True,
        )
        grandchild = int(parent.stdout.readline())
        kill_switch.track(parent)
        res = kill_switch.trigger(cmd)
        kill_switch.untrack(parent)
        check(f"{cmd} kills process tree",
              parent.poll() is not None and _dead(grandchild) and grandchild in res["killed_pids"], res)
        kill_switch.reset()


def test_snapshots() -> None:
    print("\n[3] Snapshot, recovery & rollback")
    with tempfile.TemporaryDirectory(prefix="jarvis_tether_") as tmp:
        snaps = SnapshotManager(Path(tmp) / "backups")
        agent = WindowsAgent(snapshot_manager=snaps)
        f = Path(tmp) / "work" / "notes.txt"

        r = agent.write_file(str(f), "v1")
        check("new file -> no snapshot needed", r["ok"] and r["snapshot_id"] is None, r)
        r = agent.write_file(str(f), "v2")
        sid_overwrite = r["snapshot_id"]
        check("overwrite auto-snapshots", sid_overwrite and snaps.manifest(sid_overwrite)["existed"], r)

        snaps.rollback(sid_overwrite)
        check("rollback restores file content", f.read_text() == "v1")

        d = Path(tmp) / "work"
        (d / "sub").mkdir()
        (d / "sub" / "deep.txt").write_text("deep")
        r = agent.delete_path(str(d))
        check("delete_path snapshots then deletes dir", r["ok"] and not d.exists(), r)
        res = snaps.rollback(r["snapshot_id"])
        check("rollback recovers deleted directory tree",
              (d / "notes.txt").read_text() == "v1" and (d / "sub" / "deep.txt").read_text() == "deep")

        snaps.rollback(res["pre_rollback_snapshot"])
        check("rollback is itself undoable", not d.exists())

        new = Path(tmp) / "created.txt"
        sid = snaps.snapshot(new, reason="before create")
        new.write_text("x")
        snaps.rollback(sid)
        check("rollback of not-yet-existing path removes it", not new.exists())

        check("snapshots listed", len(snaps.list_snapshots()) >= 5)
        r = client.post("/api/v1/tether/rollback/bad.id", headers=AUTH)
        check("rollback endpoint rejects bad id", r.status_code == 404, r.text)


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 3 -- Tether & Safety Engine Tests")
    print("=" * 60)
    try:
        test_token_auth()
        test_kill_switch()
        test_snapshots()
    finally:
        kill_switch.reset()
    print("\nAll Tether tests passed.")
