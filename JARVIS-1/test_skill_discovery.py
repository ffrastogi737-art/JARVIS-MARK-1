"""
test_skill_discovery.py -- Verifies the Phase 4 Skill Registry & Safe Dependency Resolver.

Test 2 creates throwaway virtualenvs and installs a tiny package (`six`) from PyPI
into them, so it needs network access. The main environment is never modified.

Run with:
    python test_skill_discovery.py
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from brain.skills.dependency_resolver import DependencyResolver
from brain.skills.registry import SkillRegistry, SkillStatus

resolver = DependencyResolver(
    allowlist={"six": "six", "jarvis-nonexistent-pkg-zz9": "jarvis_nonexistent_pkg_zz9"}
)


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<48} [OK] PASSED")


def test_lifecycle(tmp: Path) -> None:
    print("\n[1] Skill registration & lifecycle")
    path = tmp / "registry.json"
    reg = SkillRegistry(path)
    s = reg.register("os.list_procs", "Core OS Control", "List processes")
    check("registered as Discovered", s["status"] == "discovered")
    try:
        reg.register("os.list_procs", "Core OS Control")
        raise AssertionError("duplicate allowed")
    except ValueError:
        check("duplicate registration rejected", True)
    try:
        reg.transition("os.list_procs", SkillStatus.ACTIVE)
        raise AssertionError("skip allowed")
    except ValueError:
        check("illegal jump Discovered -> Active rejected", True)

    for st in (SkillStatus.TESTING, SkillStatus.ASSIMILATED, SkillStatus.ACTIVE):
        reg.transition("os.list_procs", st, "manual")
    check("manual Discovered->Testing->Assimilated->Active",
          [h["status"] for h in reg.get("os.list_procs")["history"]]
          == ["discovered", "testing", "assimilated", "active"])

    reloaded = SkillRegistry(path)
    check("metadata persisted to JSON and reloaded", reloaded.get("os.list_procs")["status"] == "active")
    check("list() filters by status", len(reloaded.list(SkillStatus.ACTIVE)) == 1 and not reloaded.list(SkillStatus.FAILED))

    reg.register("web.scrape", "Web Scraping", dependencies=["six"])
    s = reg.promote("web.scrape", resolver)
    check("promote(): sandbox pass -> auto Active",
          s["status"] == "active" and [h["status"] for h in s["history"]][-3:] == ["testing", "assimilated", "active"], s)

    reg.register("bad.skill", "API Integration", dependencies=["evil-package"])
    s = reg.promote("bad.skill", resolver)
    check("promote(): non-allowlisted dep -> Failed", s["status"] == "failed" and "allowlist" in s["history"][-1]["note"], s)
    check("failed skill can be re-tested", reg.transition("bad.skill", SkillStatus.TESTING)["status"] == "testing")


def test_resolver() -> None:
    print("\n[2] Isolated import verification & allowlist")
    check("verify_import(json) in isolated subprocess", resolver.verify_import("json")["ok"])
    r = resolver.verify_import("definitely_not_a_module_zz9")
    check("verify_import(missing) fails cleanly", not r["ok"] and "ModuleNotFoundError" in r["error"], r)
    r = resolver.verify_import("os; import shutil")
    check("verify_import rejects code injection", not r["ok"] and r["error"] == "Invalid import name", r)

    for bad in ("evil-package", "six @ https://evil.example/six.whl", "--index-url=https://evil", "../six"):
        r = resolver.verify_package(bad)
        check(f"allowlist rejects {bad[:28]!r}", not r["ok"] and r["stage"] == "allowlist", r)
    check("names are PEP 503-normalised", resolver.is_allowed("SIX") and resolver.is_allowed("Jarvis_Nonexistent.Pkg-zz9"))

    before = _main_has("six")
    r = resolver.verify_package("six")
    check("allowlisted pkg installs + imports in sandbox venv", r["ok"] and r["stage"] == "import", r)
    check("sandbox did not touch main environment", _main_has("six") == before)

    r = resolver.verify_package("jarvis-nonexistent-pkg-zz9")
    check("allowlisted but uninstallable -> install stage fails", not r["ok"] and r["stage"] == "install", r)

    r = resolver.install("evil-package")
    check("install() refuses non-allowlisted pkg", not r["ok"] and r["stage"] == "allowlist", r)


def _main_has(module: str) -> bool:
    return subprocess.run([sys.executable, "-I", "-c", f"import {module}"], capture_output=True).returncode == 0


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 4 -- Skill Registry & Dependency Resolver Tests")
    print("=" * 60)
    with tempfile.TemporaryDirectory(prefix="jarvis_skills_") as tmp:
        test_lifecycle(Path(tmp))
    test_resolver()
    print("\nAll Skill Discovery tests passed.")
