"""
test_cloud_computer_agent.py -- Verifies the cloud-VM agent skeleton.

Run with:
    python test_cloud_computer_agent.py
"""
from __future__ import annotations

import sys

from brain.agents import CloudComputerAgent
from brain.daemon.edge_agent import EdgeAgent

agent = CloudComputerAgent()


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<42} [OK] PASSED")


def test_bash() -> None:
    if not agent.bash:
        print("    bash not available; skipped")
        return
    r = agent.run_bash("echo jarvis-vm; exit 0")
    check("bash stdout captured", r["ok"] and "jarvis-vm" in r["stdout"], r)
    r = agent.run_bash("sleep 10", timeout=1)
    check("bash timeout enforced", r["timed_out"], r)


def test_browse_reports_instead_of_raising() -> None:
    r = agent.browse("http://127.0.0.1:1/")
    check("browse failure returns ok=False", r["ok"] is False and "error" in r, r)


def test_edge_ops() -> None:
    edge = EdgeAgent("http://127.0.0.1:1", "tok", agent=agent)
    r = edge.execute({"id": "t1", "op": "bash", "command": "echo hi"})
    check("EdgeAgent routes bash op", r["op"] == "bash" and ("hi" in r.get("stdout", "") or not agent.bash), r)
    r = edge.execute({"id": "t2", "op": "browse", "url": "http://127.0.0.1:1/"})
    check("EdgeAgent routes browse op", r["op"] == "browse" and r["ok"] is False, r)


if __name__ == "__main__":
    test_bash()
    test_browse_reports_instead_of_raising()
    test_edge_ops()
    print("\nAll cloud computer agent checks passed.")
    sys.exit(0)
