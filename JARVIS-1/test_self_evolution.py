"""
test_self_evolution.py -- Verifies the Phase 6 Self-Evolution & Hot-Reload Engine.

The test-gate checks run SelfEvolver against a throwaway mini-project (its own brain/
package + test file), so the real source tree is never modified. Hot-reloading is
verified against a live uvicorn server running in this process.

Run with:
    python test_self_evolution.py
"""
from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brain.evolution.hot_reloader import HotReloader
from brain.evolution.self_evolver import SelfEvolver, analyze, audit, remove_unused_imports

SAMPLE = '''"""Sample module."""
from __future__ import annotations

import os  # unused
import json
from typing import Any, Optional
from pathlib import (
    Path,
)


def dump(x: Any) -> str:
    """Serialise x."""
    return json.dumps(x)


def busy(n):
    total = 0
    for i in range(n):
        if i % 2:
            total += i
        elif i % 3:
            total -= i
    return total
'''


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<52} [OK] PASSED")


def test_analysis() -> None:
    print("\n[1] AST analysis & safe refactoring")
    rep = analyze(SAMPLE, "sample.py")
    fns = {f["name"]: f for f in rep["functions"]}
    check("functions found with line spans", set(fns) == {"dump", "busy"} and fns["busy"]["length"] == 8, fns)
    check("docstring detection", fns["dump"]["has_docstring"] and not fns["busy"]["has_docstring"])
    check("branch counting", fns["busy"]["branches"] == 3, fns["busy"])
    check("unused imports (incl. multi-line) detected",
          rep["unused_imports"] == ["import os", "from pathlib import Path"], rep["unused_imports"])
    check("partially-used import kept (Any used)", not any("typing" in u for u in rep["unused_imports"]))

    new = remove_unused_imports(SAMPLE)
    compile(new, "sample.py", "exec")
    check("refactor output compiles", True)
    check("unused imports removed", "import os" not in new and "from pathlib" not in new and "Path," not in new)
    check("other lines byte-for-byte intact", new.replace("\n", "") in SAMPLE.replace("\n", "").replace(
        "import os  # unused", "").replace("from pathlib import (    Path,)", ""))
    check("__future__ import preserved", "from __future__ import annotations" in new)
    check("idempotent", remove_unused_imports(new) == new)

    report = audit()
    check("audit covers all brain/ modules, all parse", len(report) >= 20 and not any("error" in r for r in report),
          [r for r in report if "error" in r])


def _mini_project(root: Path) -> None:
    (root / "brain").mkdir()
    (root / "brain" / "__init__.py").write_text("")
    (root / "brain" / "calc.py").write_text("import os\n\n# keep me\ndef add(a, b):\n    return a + b\n")
    # The test also leaves a marker in its cwd, so we can prove it ran in the sandbox.
    (root / "test_calc.py").write_text(
        "from pathlib import Path\nfrom brain.calc import add\nPath('ran.marker').write_text('x')\n"
        "assert add(2, 2) == 4, 'add broken'\nprint('calc ok')\n")


def test_guardrail(tmp: Path) -> None:
    print("\n[2] Test-gated self-modification guardrail")
    _mini_project(tmp)
    evolver = SelfEvolver(tmp, tests=("test_calc.py",), timeout=60)
    calc = tmp / "brain" / "calc.py"
    original = calc.read_text()

    r = evolver.apply({"brain/calc.py": "def add(a, b):\n    return a - b\n"}, reason="bad change")
    check("regression failure -> rejected at tests stage", not r["ok"] and r["stage"] == "tests", r)
    check("failing test output reported", "add broken" in r["output"])
    check("live file untouched after failed tests", calc.read_text() == original)
    check("tests ran in sandbox copy, not live tree", not (tmp / "ran.marker").exists())

    r = evolver.apply({"brain/calc.py": "def add(a, b:\n    return a + b\n"})
    check("syntax error -> rejected before tests", not r["ok"] and r["stage"] == "validate", r)
    for bad in ("test_calc.py", "brain/../evil.py", "../outside/brain/x.py", "brain/notes.txt", "/etc/brain/x.py"):
        r = evolver.apply({bad: "x = 1\n"})
        check(f"path outside brain/**/*.py rejected: {bad[:20]}", not r["ok"] and r["stage"] == "validate", r)
    check("live file still untouched", calc.read_text() == original)

    r = evolver.apply({"brain/calc.py": remove_unused_imports(original)}, reason="drop unused imports")
    check("passing change applied", r["ok"] and r["modules"] == ["brain.calc"], r)
    check("live file updated (comment preserved)", calc.read_text() == "\n# keep me\ndef add(a, b):\n    return a + b\n")
    check("original backed up", (Path(r["backup"]) / "brain" / "calc.py").read_text() == original)

    r = evolver.apply({"brain/new_skill.py": "def hello():\n    return 'hi'\n"})
    check("new module under brain/ can be added", r["ok"] and (tmp / "brain" / "new_skill.py").exists(), r)


ROUTES_SRC = """from fastapi import APIRouter
from {pkg}.logic import answer

router = APIRouter()


@router.get("/answer")
def get_answer() -> dict:
    return {{"answer": answer()}}
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    t = time.time() + 5  # guarantee a new mtime even on coarse-grained filesystems
    os.utime(path, (t, t))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_hot_reload(tmp: Path) -> None:
    print("\n[3] Hot-reload into a running FastAPI server")
    pkg = "jarvis_hot_demo"
    (tmp / pkg).mkdir()
    (tmp / pkg / "__init__.py").write_text("")
    _write(tmp / pkg / "logic.py", "def answer():\n    return 1\n")
    _write(tmp / pkg / "routes.py", ROUTES_SRC.format(pkg=pkg))
    sys.path.insert(0, str(tmp))
    import jarvis_hot_demo.routes as routes  # noqa: E402

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1", tags=["Demo"])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/api/v1/answer"
    try:
        check("server up, v1 logic served", httpx.get(url).json() == {"answer": 1})
        reloader = HotReloader(pkg)
        check("no changes detected initially", reloader.changed() == [])

        _write(tmp / pkg / "logic.py", "def answer():\n    return 2  # evolved\n")
        check("modified module detected via mtime", reloader.changed() == [f"{pkg}.logic"], reloader.changed())
        done = reloader.reload_changed(app)
        check("dependent route module reloaded too", done == [f"{pkg}.logic", f"{pkg}.routes"], done)
        check("live server now serves v2 logic", httpx.get(url).json() == {"answer": 2})
        check("same server, never restarted", thread.is_alive() and server.started)
        paths = [r.path for r in app.router.routes if r.path.endswith("/answer")]
        check("route replaced, not duplicated", paths == ["/api/v1/answer"], paths)
        check("tags preserved", next(r for r in app.router.routes if r.path == "/api/v1/answer").tags == ["Demo"])

        _write(tmp / pkg / "routes.py", ROUTES_SRC.format(pkg=pkg).replace('"answer": answer()', '"answer": answer() * 10'))
        reloader.reload_changed(app)
        check("route handler itself hot-swapped", httpx.get(url).json() == {"answer": 20})
        check("OpenAPI schema regenerated", "/api/v1/answer" in httpx.get(f"http://127.0.0.1:{port}/openapi.json").json()["paths"])
        check("nothing pending after reload", reloader.changed() == [])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sys.path.remove(str(tmp))


def test_brain_reload() -> None:
    print("\n[4] Hot-reload of the real JARVIS brain")
    os.environ.setdefault("TETHER_SECRET_TOKEN", "test-tether-secret")
    from brain.api.app import create_app

    app = create_app()
    client = TestClient(app)
    routes_before = sorted(r.path for r in app.router.routes)
    check("/health OK before reload", client.get("/health").status_code == 200)
    done = HotReloader().reload(["brain.router.llm_router"], app)
    check("router reload cascades to chat route", "brain.api.routes.chat" in done, done)
    check("route table unchanged after swap", sorted(r.path for r in app.router.routes) == routes_before)
    check("/health OK after reload", client.get("/health").status_code == 200)
    check("/api/v1/router/info OK after reload", client.get("/api/v1/router/info").status_code == 200)


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 6 -- Self-Evolution & Hot-Reload Tests")
    print("=" * 60)
    test_analysis()
    with tempfile.TemporaryDirectory(prefix="jarvis_evolve_") as tmp:
        test_guardrail(Path(tmp))
    with tempfile.TemporaryDirectory(prefix="jarvis_hot_") as tmp:
        test_hot_reload(Path(tmp))
    test_brain_reload()
    print("\nAll Self-Evolution tests passed.")
