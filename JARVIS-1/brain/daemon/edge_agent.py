"""
brain/daemon/edge_agent.py — Ultra-light local Edge Agent (Phase 7).

Cloud-first split: the Cloud Brain (main.py / FastAPI) runs LLM routing, research,
skills and self-evolution. This daemon is all that runs on the laptop. It imports only
the stdlib, psutil, structlog, the Windows agent and the tether (no FastAPI, pydantic,
httpx or LLM code), so it idles well under 30 MB RSS.

Loop:
  • polls GET /api/v1/edge/poll (the poll is the heartbeat), backing off to `max_interval`
    while idle so it costs ~no CPU
  • runs only tasks whose HMAC signature verifies against TETHER_SECRET_TOKEN
  • executes shell tasks on a single worker thread via WindowsAgent, so polling (and a
    HALT) keeps flowing while a long command runs
  • tether tasks (HALT/STOP/SHUTDOWN/RESET) run inline, at once; a local HALT file engages
    the kill-switch even while the cloud is unreachable
  • results that cannot be sent are buffered by CloudSync and flushed on reconnect
"""

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil
import structlog

from brain.agents.windows_agent import WindowsAgent
from brain.daemon.cloud_sync import CloudSync
from brain.safety.tether import kill_switch, verify_task

log = structlog.get_logger(__name__)

DEFAULT_HALT_FILE = Path.home() / ".jarvis" / "HALT"


class EdgeAgent:
    def __init__(
        self, cloud_url: str, token: str, sync: CloudSync | None = None, agent: WindowsAgent | None = None,
        poll_interval: float = 2.0, max_interval: float = 30.0, halt_file: Path | str = DEFAULT_HALT_FILE,
        request_timeout: float = 10.0,
    ) -> None:
        if not token:
            raise ValueError("TETHER_SECRET_TOKEN is required: the edge must not run unverified tasks")
        self.cloud_url = cloud_url.rstrip("/")
        self.token = token
        self.sync = sync or CloudSync()
        self.agent = agent or WindowsAgent()
        self.poll_interval, self.max_interval = poll_interval, max_interval
        self.halt_file = Path(halt_file)
        self.request_timeout = request_timeout
        self.online = False
        self.busy = False
        self._work: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._worker, name="edge-worker", daemon=True).start()

    # ── Cloud I/O ──────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(
            self.cloud_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Tether-Token": self.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            return json.loads(resp.read() or b"null")

    def _send_results(self, results: list[dict[str, Any]]) -> None:
        self._request("POST", "/api/v1/edge/results", {"results": results})

    def _report(self, result: dict[str, Any]) -> None:
        try:
            self._send_results([result])
        except (urllib.error.URLError, OSError, ValueError):
            self.online = False
            self.sync.buffer(result)

    # ── Main loop ──────────────────────────────────────────────────────────────
    def poll_once(self) -> int:
        """One heartbeat: check the local HALT file, fetch tasks, dispatch them. Returns tasks received."""
        if self.halt_file.exists() and not kill_switch.halted:
            kill_switch.trigger("HALT")
        status = "halted" if kill_switch.halted else "busy" if self.busy else "idle"
        try:
            vitals = f"cpu={psutil.cpu_percent(interval=None)}&ram={psutil.virtual_memory().percent}"
            tasks = self._request("GET", f"/api/v1/edge/poll?status={status}&{vitals}")["tasks"]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            if self.online:
                log.warning("edge.offline", error=str(exc))
            self.online = False
            return 0
        if not self.online:
            self.online = True
            log.info("edge.online", cloud=self.cloud_url)
            try:
                self.sync.flush(self._send_results)
            except (urllib.error.URLError, OSError, ValueError):
                self.online = False
        for task in tasks:
            self.dispatch(task)
        return len(tasks)

    def dispatch(self, task: dict[str, Any]) -> None:
        if not isinstance(task, dict) or not verify_task(task, self.token):
            log.warning("edge.rejected_unsigned_task", id=task.get("id") if isinstance(task, dict) else None)
            if isinstance(task, dict):
                self._report({"id": task.get("id"), "ok": False, "error": "Invalid signature; task rejected"})
            return
        if task["op"] == "tether":
            self._report(self.execute(task))  # inline: never waits behind a running command
        else:
            self._work.put(task)

    def run(self, stop: threading.Event) -> None:
        interval = self.poll_interval
        log.info("edge.start", cloud=self.cloud_url)
        while not stop.is_set():
            got_work = self.poll_once() > 0 or self.busy or not self._work.empty()
            interval = self.poll_interval if got_work else min(interval * 2, self.max_interval)
            stop.wait(interval)
        log.info("edge.stop")

    # ── Execution ──────────────────────────────────────────────────────────────
    def _worker(self) -> None:
        while True:
            task = self._work.get()
            self.busy = True
            try:
                self._report(self.execute(task))
            finally:
                self.busy = False

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        op = task.get("op")
        try:
            if op == "tether":
                cmd = task.get("command", "").strip().upper()
                if cmd == "RESET":
                    self.halt_file.unlink(missing_ok=True)
                    res = kill_switch.reset()
                else:
                    res = kill_switch.trigger(cmd)
                res = {"ok": True, **res}
            elif kill_switch.halted:
                res = {"ok": False, "halted": True, "error": f"Tether kill-switch engaged ({kill_switch.last_command})"}
            elif op == "powershell":
                res = self.agent.run_powershell(task["command"], timeout=task.get("timeout"))
            elif op == "cmd":
                res = self.agent.run_cmd(task["command"], timeout=task.get("timeout"))
            elif op in ("bash", "browse") and hasattr(self.agent, "run_bash"):  # cloud-VM agent only
                res = (self.agent.run_bash(task["command"], timeout=task.get("timeout")) if op == "bash"
                       else self.agent.browse(task["url"], timeout=task.get("timeout")))
            elif op == "update":
                res = self.sync.apply_update(task)
            else:
                res = {"ok": False, "error": f"Unknown op {op!r}"}
        except Exception as exc:  # a bad task must never kill the daemon
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        log.info("edge.executed", id=task.get("id"), op=op, ok=res.get("ok"))
        return {"id": task.get("id"), "op": op, **res}
