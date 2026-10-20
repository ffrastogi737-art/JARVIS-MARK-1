"""
brain/skills/dependency_resolver.py — Allowlist-based Safe Dependency Resolver (Phase 4, §15).

A package reaches the main environment only after:
  1. it is on the allowlist (exact, normalised name — no URLs, paths or pip flags), and
  2. it installs AND imports cleanly inside a throwaway virtualenv, in a separate
     isolated (``python -I``) subprocess.

shortcut: no version pinning — the latest release is verified and installed; add
``package==version`` support when reproducible builds matter.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# PyPI package name -> import name
DEFAULT_ALLOWLIST: dict[str, str] = {
    "requests": "requests",
    "httpx": "httpx",
    "beautifulsoup4": "bs4",
    "lxml": "lxml",
    "numpy": "numpy",
    "pandas": "pandas",
    "psutil": "psutil",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "pillow": "PIL",
    "rich": "rich",
    "six": "six",
}

_IMPORT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def normalize(package: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", package.strip()).lower()


class DependencyResolver:
    def __init__(self, allowlist: dict[str, str] | None = None, timeout: float = 300.0) -> None:
        self.allowlist = {normalize(k): v for k, v in (allowlist or DEFAULT_ALLOWLIST).items()}
        self.timeout = timeout

    def is_allowed(self, package: str) -> bool:
        return normalize(package) in self.allowlist

    def verify_import(self, import_name: str, python: str = sys.executable) -> dict[str, Any]:
        """Import `import_name` in a separate isolated interpreter (no cwd/user-site/env leakage)."""
        if not _IMPORT_NAME.fullmatch(import_name):
            return {"ok": False, "import": import_name, "error": "Invalid import name"}
        with tempfile.TemporaryDirectory(prefix="jarvis_import_") as cwd:
            r = self._run([python, "-I", "-c", f"import importlib; importlib.import_module({import_name!r})"], cwd)
        result = {"ok": r["ok"], "import": import_name, "error": r["error"]}
        log.info("resolver.verify_import", **result)
        return result

    def verify_package(self, package: str) -> dict[str, Any]:
        """Install `package` into a throwaway venv and import it there. Never touches the main env."""
        name = normalize(package)
        if name not in self.allowlist:
            result = {"ok": False, "package": name, "stage": "allowlist", "error": "Package not on allowlist"}
            log.warning("resolver.rejected", **result)
            return result

        with tempfile.TemporaryDirectory(prefix="jarvis_sandbox_") as tmp:
            env_dir = Path(tmp) / "venv"
            venv.create(env_dir, with_pip=True)
            py = str(env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
            r = self._run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", name], tmp)
            if not r["ok"]:
                result = {"ok": False, "package": name, "stage": "install", "error": r["error"]}
            else:
                imp = self.verify_import(self.allowlist[name], python=py)
                result = {"ok": imp["ok"], "package": name, "stage": "import", "error": imp["error"]}
        log.info("resolver.verify_package", **result)
        return result

    def install(self, package: str) -> dict[str, Any]:
        """Verify in the sandbox first; only on success install into the main environment."""
        verified = self.verify_package(package)
        if not verified["ok"]:
            return verified
        name = normalize(package)
        r = self._run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", name], None)
        result = {"ok": r["ok"], "package": name, "stage": "main_install", "error": r["error"]}
        log.info("resolver.install", **result)
        return result

    def _run(self, argv: list[str], cwd: str | None) -> dict[str, Any]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Timed out after {self.timeout}s"}
        return {"ok": p.returncode == 0, "error": None if p.returncode == 0 else (p.stderr or p.stdout)[-1000:]}
