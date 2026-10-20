"""
brain/agents/cloud_computer_agent.py — JARVIS's own always-on computer (cloud VM).

Same contract as WindowsAgent (so EdgeAgent runs it unchanged), plus what a Linux VM
needs: a bash shell and a headless browser. Run the Edge daemon on the VM with
JARVIS_AGENT=cloud and it keeps working while the laptop is off.

shortcut: browse() launches a fresh browser per call and has no login/session state;
          keep a persistent context when multi-step web tasks are needed.
shortcut: no desktop/screenshot-driven computer-use loop yet; add one on top of browse().
"""

from __future__ import annotations

import shutil
from typing import Any

import structlog

from brain.agents.windows_agent import WindowsAgent, _unavailable

log = structlog.get_logger(__name__)


class CloudComputerAgent(WindowsAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bash = shutil.which("bash")

    def run_bash(self, command: str, timeout: float | None = None, cwd: str | None = None) -> dict[str, Any]:
        """Run a bash command; same result shape as run_powershell."""
        if not self.bash:
            return _unavailable("bash", "bash not found on PATH")
        return self._run("bash", [self.bash, "-lc", command], timeout, cwd)

    def browse(self, url: str, timeout: float | None = None) -> dict[str, Any]:
        """Load a page in headless Chromium and return its title and visible text."""
        try:
            from playwright.sync_api import sync_playwright  # optional: pip install playwright
        except ImportError:
            return {"ok": False, "url": url, "error": "playwright not installed"}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=(timeout or self.default_timeout) * 1000)
                    result = {"ok": True, "url": page.url, "title": page.title(), "text": page.inner_text("body")}
                finally:
                    browser.close()
        except Exception as exc:  # playwright raises its own Error types; report, never raise
            result = {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}
        log.info("cloud_agent.browse", url=url, ok=result["ok"])
        return result
