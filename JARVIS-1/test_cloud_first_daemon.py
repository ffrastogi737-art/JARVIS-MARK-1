"""
test_cloud_first_daemon.py -- Verifies Phase 7: Cloud-First Brain + ultra-light Edge Agent.

A real Cloud Brain (uvicorn, this process) talks to real Edge Agents over HTTP:
  [1] cloud payload dispatch (signed) and rejection of forged tasks
  [2] edge footprint: < 30 MB RSS, ~0 idle CPU, no heavy cloud modules loaded
  [3] offline buffering, reconnection sync and verified hot-reload updates
  [4] local Tether HALT override on the Edge Agent

Run with:
    python test_cloud_first_daemon.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TOKEN = "test-edge-secret"
os.environ["TETHER_SECRET_TOKEN"] = TOKEN  # before settings are cached

import httpx
import psutil
import uvicorn

from brain.api.app import create_app
from brain.config import get_settings
from brain.daemon.cloud_sync import CloudSync
from brain.daemon.edge_agent import EdgeAgent
from brain.safety.tether import kill_switch, sign_task

get_settings.cache_clear()
logging.getLogger("httpx").setLevel(logging.WARNING)
AUTH = {"X-Tether-Token": TOKEN}
TMP = Path(tempfile.mkdtemp(prefix="jarvis_edge_"))

# PowerShell is the edge's shell. Off Windows without pwsh, a shim runs the same commands in sh;
# the tests only use `echo` and `sleep`, which mean the same in both.
if not (shutil.which("powershell") or shutil.which("pwsh")):
    shim = TMP / "bin" / "pwsh"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nexec sh -c "$4"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{shim.parent}{os.pathsep}{os.environ['PATH']}"


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<52} [OK] PASSED")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.05)
    return cond()


PORT, DEAD_PORT = _free_port(), _free_port()
CLOUD = f"http://127.0.0.1:{PORT}"
cloud = httpx.Client(base_url=CLOUD + "/api/v1/edge", headers=AUTH)


def queue(**task) -> str:
    r = cloud.post("/tasks", json=task)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def result(tid: str, timeout: float = 10.0) -> dict | None:
    _wait(lambda: tid in cloud.get("/status").json()["results"], timeout)
    return cloud.get("/status").json()["results"].get(tid)


def new_agent(url: str = CLOUD) -> EdgeAgent:
    return EdgeAgent(url, TOKEN, sync=CloudSync(TMP / f"buf-{time.monotonic_ns()}.jsonl"),
                     halt_file=TMP / "HALT", request_timeout=2)


def test_dispatch() -> None:
    print("\n[1] Cloud payload dispatch to Edge Agent")
    check("cloud rejects edge calls without token", httpx.get(CLOUD + "/api/v1/edge/poll").status_code == 401)
    agent = new_agent()
    tid = queue(op="powershell", command="echo edge-ok")
    check("poll (heartbeat) receives the task", agent.poll_once() == 1)
    r = result(tid)
    check("edge executed it and reported back", r and r["ok"] and "edge-ok" in r["stdout"], r)
    seen = cloud.get("/status").json()["last_seen"]
    check("cloud recorded the heartbeat", seen["at"] and seen["status"] == "idle", seen)

    forged = {"id": "forged", "op": "powershell", "command": "echo pwned"}
    forged["sig"] = sign_task(forged, "wrong-secret")
    agent.dispatch(forged)
    r = result("forged")
    check("forged signature rejected, never executed", r and not r["ok"] and "stdout" not in r, r)
    tampered = cloud.post("/tasks", json={"op": "powershell", "command": "echo a"}).json()["id"]
    task = agent._request("GET", "/api/v1/edge/poll")["tasks"][0]
    task["command"] = "echo tampered"
    agent.dispatch(task)
    check("tampered payload rejected", not result(tampered)["ok"])


CHILD = r"""
import json, sys, threading, time
import structlog
structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))  # keep stdout for the protocol
from brain.daemon.edge_agent import EdgeAgent
from brain.daemon.cloud_sync import CloudSync
stop = threading.Event()
agent = EdgeAgent(sys.argv[1], sys.argv[2], sync=CloudSync(sys.argv[3]), poll_interval=0.1, max_interval=1.0,
                  halt_file=sys.argv[3] + ".halt")
threading.Thread(target=agent.run, args=(stop,), daemon=True).start()
print("ready", flush=True)
sys.stdin.readline()
heavy = sorted({m for m in sys.modules if m.split(".")[0] in ("fastapi", "pydantic", "httpx", "uvicorn", "starlette")}
               | {m for m in sys.modules if m.startswith(("brain.router", "brain.research", "brain.skills",
                                                          "brain.evolution", "brain.api", "brain.config"))})
print(json.dumps({"heavy": heavy, "online": agent.online}), flush=True)
"""


def test_footprint() -> None:
    print("\n[2] Low memory footprint & idle CPU protection")
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, CLOUD, TOKEN, str(TMP / "child.jsonl")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=Path(__file__).parent,
    )
    try:
        check("edge daemon started", child.stdout.readline().strip() == "ready")
        tid = queue(op="powershell", command="echo from-daemon")
        r = result(tid)
        check("background daemon executes cloud tasks", r and "from-daemon" in r["stdout"], r)
        time.sleep(3)  # let it back off to max_interval
        p = psutil.Process(child.pid)
        cpu0, t0 = sum(p.cpu_times()[:2]), time.monotonic()
        time.sleep(5)
        cpu_pct = (sum(p.cpu_times()[:2]) - cpu0) / (time.monotonic() - t0) * 100
        rss = p.memory_info().rss / 2**20
        check(f"RSS {rss:.1f} MB < 30 MB", rss < 30, rss)
        check(f"idle CPU {cpu_pct:.2f}% < 1%", cpu_pct < 1, cpu_pct)
        out, _ = child.communicate("\n", timeout=10)
        info = json.loads(out)
        check("no heavy cloud modules (LLM/research/skills/FastAPI)", not info["heavy"], info["heavy"])
        check("daemon online via heartbeat", info["online"])
    finally:
        child.kill()


def test_offline_sync() -> None:
    print("\n[3] Offline buffering and reconnection sync")
    agent = new_agent(f"http://127.0.0.1:{DEAD_PORT}")
    check("unreachable cloud -> offline, no crash", agent.poll_once() == 0 and not agent.online)
    queued_while_offline = queue(op="powershell", command="echo queued-in-cloud")

    offline = {"id": "offline-1", "op": "powershell", "command": "echo ran-offline"}
    offline["sig"] = sign_task(offline, TOKEN)
    agent.dispatch(offline)
    check("result buffered locally while offline",
          _wait(lambda: [r["id"] for r in agent.sync.pending()] == ["offline-1"]), agent.sync.pending())
    check("buffer is on disk (survives reboot)", agent.sync.buffer_path.exists())

    agent.cloud_url = CLOUD  # laptop reconnects
    check("reconnect poll delivers task queued in cloud", agent.poll_once() == 1 and agent.online)
    r = result("offline-1")
    check("buffered result synced to cloud", r and "ran-offline" in r["stdout"], r)
    check("local buffer cleared after sync", not agent.sync.pending())
    r = result(queued_while_offline)
    check("cloud-queued task executed after reconnect", r and "queued-in-cloud" in r["stdout"], r)

    probe = Path(__file__).parent / "brain" / "daemon" / "_hotreload_probe.py"
    probe.write_text("VALUE = 1\n")
    try:
        import brain.daemon._hotreload_probe as mod
        tid = queue(op="update", module="brain.daemon._hotreload_probe", source="VALUE = 2\n")
        agent.poll_once()
        r = result(tid)
        check("verified code update hot-reloaded in place", r and r["ok"] and mod.VALUE == 2, r)
        tid = queue(op="update", module="os", source="x = 1\n")
        agent.poll_once()
        check("update outside brain/ refused", not result(tid)["ok"])
        tid = queue(op="update", module="brain.daemon._hotreload_probe", source="def (:\n")
        agent.poll_once()
        check("syntactically broken update refused", not result(tid)["ok"] and mod.VALUE == 2)
    finally:
        probe.unlink(missing_ok=True)


def test_halt() -> None:
    print("\n[4] Local Tether emergency HALT override")
    agent = new_agent()
    long_id = queue(op="powershell", command="sleep 60", timeout=90)
    agent.poll_once()
    check("long command running on edge", _wait(lambda: bool(kill_switch.active_pids)))

    t0 = time.monotonic()
    halt_id = queue(op="tether", command="HALT")
    agent.poll_once()
    r = result(long_id)
    elapsed = time.monotonic() - t0
    check(f"HALT killed running command ({elapsed:.2f}s)", r and r["halted"] and not r["ok"] and elapsed < 5, r)
    check("HALT acknowledged", result(halt_id)["halted"])
    blocked = queue(op="powershell", command="echo should-not-run")
    agent.poll_once()
    r = result(blocked)
    check("new tasks blocked while halted", r and not r["ok"] and r["halted"], r)

    behind = queue(op="powershell", command="echo after-reset")
    reset_id = queue(op="tether", command="RESET")
    agent.poll_once()
    check("cloud RESET releases the kill-switch", result(reset_id)["ok"] and not kill_switch.halted)
    r = result(behind)
    check("tether task jumps the queue (runs first)", r and r["ok"] and "after-reset" in r["stdout"], r)

    agent.cloud_url = f"http://127.0.0.1:{DEAD_PORT}"  # cloud gone: local override must still work
    agent.halt_file.write_text("")
    agent.poll_once()
    check("local HALT file engages kill-switch offline", kill_switch.halted)
    check("executions refused under local HALT", agent.agent.run_powershell("echo nope")["halted"])
    agent.execute({"op": "tether", "command": "RESET"})
    check("RESET clears HALT file and kill-switch", not agent.halt_file.exists() and not kill_switch.halted)


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 7 -- Cloud-First Brain & Edge Daemon Tests")
    print("=" * 60)
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_config=None,
                                          access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    assert _wait(lambda: server.started, 15), "cloud server did not start"
    try:
        test_dispatch()
        test_footprint()
        test_offline_sync()
        test_halt()
    finally:
        kill_switch.reset()
        server.should_exit = True
        shutil.rmtree(TMP, ignore_errors=True)
    print("\nAll Phase 7 Cloud-First Daemon tests passed.")
