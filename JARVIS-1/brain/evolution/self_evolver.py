"""
brain/evolution/self_evolver.py — AST code analysis & test-gated self-modification (Phase 6, §16/§25).

  • analyze() / audit()        — per-file AST report: functions, length, branch count,
                                 missing docstrings, unused imports
  • remove_unused_imports()    — safe refactor: drops whole import lines whose names are
                                 never used (line-based, so comments/formatting survive)
  • SelfEvolver.apply()        — the guardrail. Proposed changes must:
        1. target .py files inside brain/ only,
        2. compile,
        3. pass the FULL regression suite, run in subprocesses against a sandbox copy
           of the project with the changes applied.
    Only then are they written to the live tree (atomically, originals kept in
    brain/evolution/backups/). Any failure leaves the live tree untouched.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGRESSION_SUITE = (
    "test_server.py", "test_router_failover.py", "test_windows_agent.py", "test_tether.py",
    "test_skill_discovery.py", "test_research_engine.py", "test_self_evolution.py",
)
_BRANCHES = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.IfExp, ast.Match,
             ast.AsyncFor, ast.AsyncWith, ast.comprehension)
_SANDBOX_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".venv", "backups", "*.pyc")


def _unused_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Top-level import statements none of whose bound names are referenced (``__all__`` aware)."""
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    for node in tree.body:  # names exported via __all__ count as used
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets):
            used |= {e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)}
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "module", "") != "__future__":
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
            if "*" not in names and not any(n in used for n in names):
                out.append(node)
    return out


def analyze(source: str, filename: str = "<string>") -> dict[str, Any]:
    tree = ast.parse(source, filename)
    functions = [{
        "name": f.name, "lineno": f.lineno, "length": f.end_lineno - f.lineno + 1,
        "branches": sum(isinstance(n, _BRANCHES) for n in ast.walk(f)),
        "has_docstring": ast.get_docstring(f) is not None,
    } for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return {
        "file": filename,
        "functions": functions,
        "classes": [c.name for c in ast.walk(tree) if isinstance(c, ast.ClassDef)],
        "unused_imports": [ast.unparse(n) for n in _unused_imports(tree)],
        "complex_functions": [f["name"] for f in functions if f["branches"] > 10 or f["length"] > 80],
    }


def audit(root: Path | str = PROJECT_ROOT / "brain") -> list[dict[str, Any]]:
    """Analyze every module under `root`; unparsable files are reported, not raised."""
    report = []
    for path in sorted(Path(root).rglob("*.py")):
        try:
            report.append(analyze(path.read_text(encoding="utf-8"), str(path)))
        except SyntaxError as exc:
            report.append({"file": str(path), "error": f"SyntaxError: {exc}"})
    return report


def remove_unused_imports(source: str) -> str:
    """Delete unused top-level import statements, leaving every other line byte-for-byte intact."""
    drop = {ln for n in _unused_imports(ast.parse(source)) for ln in range(n.lineno, n.end_lineno + 1)}
    lines = source.splitlines(keepends=True)
    return "".join(line for i, line in enumerate(lines, 1) if i not in drop)


class SelfEvolver:
    def __init__(
        self, root: Path | str = PROJECT_ROOT, tests: tuple[str, ...] = REGRESSION_SUITE, timeout: float = 900.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.tests = tests
        self.timeout = timeout

    def _target(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root / "brain") or path.suffix != ".py":
            raise ValueError(f"Self-modification is limited to brain/**/*.py, got {rel!r}")
        return path

    def apply(self, changes: dict[str, str], reason: str = "") -> dict[str, Any]:
        """Gate `changes` ({relative path: new source}) behind the regression suite, then write them."""
        try:
            targets = {self._target(rel): src for rel, src in changes.items()}
            for path, src in targets.items():
                compile(src, str(path), "exec")
        except (ValueError, SyntaxError) as exc:
            log.warning("evolver.rejected", stage="validate", error=str(exc))
            return {"ok": False, "stage": "validate", "error": str(exc)}

        with tempfile.TemporaryDirectory(prefix="jarvis-evolve-") as tmp:
            sandbox = Path(tmp) / "project"
            shutil.copytree(self.root, sandbox, ignore=_SANDBOX_IGNORE)
            for path, src in targets.items():
                dest = sandbox / path.relative_to(self.root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(src, encoding="utf-8")
            for test in self.tests:
                try:
                    r = subprocess.run(
                        [sys.executable, test], cwd=sandbox, capture_output=True, text=True,
                        timeout=self.timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                    ok, output = r.returncode == 0, r.stdout + r.stderr
                except subprocess.TimeoutExpired:
                    ok, output = False, f"timed out after {self.timeout}s"
                if not ok:
                    log.warning("evolver.rejected", stage="tests", test=test)
                    return {"ok": False, "stage": "tests", "failed": test, "output": output[-4000:]}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backups = self.root / "brain" / "evolution" / "backups" / stamp
        for path, src in targets.items():
            rel = path.relative_to(self.root)
            if path.exists():
                (backups / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backups / rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = path.with_suffix(".evolve.tmp")
            tmp_file.write_text(src, encoding="utf-8")
            os.replace(tmp_file, path)  # atomic per file
        modules = [".".join(p.relative_to(self.root).with_suffix("").parts).removesuffix(".__init__") for p in targets]
        log.info("evolver.applied", modules=modules, reason=reason, backup=str(backups))
        return {"ok": True, "modules": modules, "backup": str(backups)}
