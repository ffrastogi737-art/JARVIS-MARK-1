"""
test_server.py -- Smoke-test script for JARVIS Phase 1.

Starts the server, waits for it to be ready, runs a set of test requests
against the live API, prints results, and exits.

Run with:
    python test_server.py
"""
from __future__ import annotations

# Reconfigure stdout/stderr to UTF-8 before any prints so that Unicode
# characters don't crash on Windows cp1252 terminals.
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import os
import socket
import subprocess
import tempfile
import time

import httpx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A fresh port, so a JARVIS already running on 8000 is neither clashed with nor tested by mistake.
PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
TIMEOUT = 30  # seconds to wait for server to become ready


async def wait_for_server() -> bool:
    """Poll /health until the server is up or we time out."""
    deadline = time.monotonic() + TIMEOUT
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{BASE_URL}/health", timeout=2.0)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def run_tests() -> None:
    print("\n" + "=" * 60)
    print("  JARVIS Phase 1 -- API Smoke Tests")
    print("=" * 60)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:

        # -- Test 1: Health ------------------------------------------------
        print("\n[1] GET /health")
        resp = await client.get("/health")
        print(f"    Status : {resp.status_code}")
        data = resp.json()
        print(f"    Body   : {data}")
        assert resp.status_code == 200, "Health check failed!"
        assert data["status"] == "ok"
        print("    [OK] PASSED")

        # -- Test 2: Router info -------------------------------------------
        print("\n[2] GET /api/v1/router/info")
        resp = await client.get("/api/v1/router/info")
        print(f"    Status : {resp.status_code}")
        data = resp.json()
        print(f"    Classifier model : {data['classifier_model']}")
        print(f"    Tier routing     : {data['tier_routing']}")
        assert resp.status_code == 200
        print("    [OK] PASSED")

        # -- Test 3: Classify -- simple ------------------------------------
        print("\n[3] POST /api/v1/router/classify  (simple task)")
        resp = await client.post(
            "/api/v1/router/classify",
            json={"message": "Hi"},
        )
        data = resp.json()
        print(f"    Complexity : {data['complexity']}")
        print(f"    Confidence : {data['confidence']}")
        print(f"    Reasoning  : {data['reasoning']}")
        print(f"    Routed to  : {data['routed_model']}")
        assert resp.status_code == 200
        print("    [OK] PASSED")

        # -- Test 4: Classify -- complex -----------------------------------
        print("\n[4] POST /api/v1/router/classify  (complex task)")
        complex_msg = (
            "Design a fault-tolerant, horizontally scalable microservices "
            "architecture for a real-time financial trading platform that "
            "processes 1 million events per second with sub-millisecond latency, "
            "including data consistency guarantees, disaster recovery strategy, "
            "observability stack, and cost optimisation approach."
        )
        resp = await client.post(
            "/api/v1/router/classify",
            json={"message": complex_msg},
        )
        data = resp.json()
        print(f"    Complexity : {data['complexity']}")
        print(f"    Confidence : {data['confidence']}")
        print(f"    Reasoning  : {data['reasoning']}")
        print(f"    Routed to  : {data['routed_model']}")
        assert resp.status_code == 200
        print("    [OK] PASSED")

        # -- Test 5: Full chat (mock mode) ---------------------------------
        print("\n[5] POST /api/v1/chat  (full pipeline)")
        resp = await client.post(
            "/api/v1/chat",
            json={
                "messages": [
                    {"role": "user", "content": "What is 2 + 2?"},
                ],
            },
        )
        data = resp.json()
        print(f"    Status      : {resp.status_code}")
        print(f"    Complexity  : {data['classification']['complexity']}")
        print(f"    Routed to   : {data['model']}  (via {data['provider']})")
        print(f"    Latency     : {data['latency_ms']} ms")
        print(f"    Reply       : {data['message']['content'][:100]}")
        assert resp.status_code == 200
        print("    [OK] PASSED")

        # -- Test 6: Forced complexity -------------------------------------
        print("\n[6] POST /api/v1/chat  (force_complexity=expert)")
        resp = await client.post(
            "/api/v1/chat",
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "force_complexity": "expert",
            },
        )
        data = resp.json()
        print(f"    Complexity  : {data['classification']['complexity']}")
        print(f"    Routed to   : {data['model']}")
        assert data["classification"]["complexity"] == "expert"
        print("    [OK] PASSED")

    print("\n" + "=" * 60)
    print("  All tests passed! JARVIS Phase 1 is operational.")
    print("=" * 60 + "\n")


async def main() -> None:
    # Server output goes to a temp file, not a PIPE: nobody reads the pipe while tests run,
    # and once its buffer fills (~4 KB on Windows) the server blocks on its next log write.
    server_log = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=".",
        stdout=server_log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PORT": str(PORT)},  # env beats .env in pydantic-settings
    )

    print(f"Server PID: {proc.pid} -- waiting for /health ...")
    ready = await wait_for_server()

    if not ready:
        print("\n[FAIL] Server did not become ready in time. Captured output:")
        proc.terminate()
        proc.wait(timeout=5)
        server_log.seek(0)
        print(server_log.read())
        sys.exit(1)

    print("[OK] Server is up!\n")

    try:
        await run_tests()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        print("Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
