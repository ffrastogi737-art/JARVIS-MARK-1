"""
brain/research/research_engine.py — Technical problem investigation (Phase 5, Master Doc §12/§15).

Workflow:  error log / missing skill -> query Stack Overflow, docs pages, public GitHub
READMEs (via AgentReach) -> extract code blocks -> keep only those that are valid
Python (ast) or valid shell (shlex check, + ``bash -n`` on POSIX; parse-only) -> rank -> write each candidate to
disk and register it in the Skill Registry in the ``Discovered`` state.

Nothing found here is ever executed: candidates still have to pass the registry's
sandboxed promote() before they become Active.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import structlog

from brain.research.agent_reach import AgentReach, extract, markdown_code
from brain.skills.dependency_resolver import DEFAULT_ALLOWLIST
from brain.skills.registry import SkillRegistry

log = structlog.get_logger(__name__)

DEFAULT_DIR = Path(__file__).parent / "candidates"
_PACKAGE_FOR = {imp: pkg for pkg, imp in DEFAULT_ALLOWLIST.items()}  # import name -> PyPI name
_CMDS = r"(pip3?|python3? -m pip|apt(-get)?|sudo|npm|git|curl|export|cd|mkdir) "
_SHELL_START = re.compile(rf"^(\$\s|\$?{_CMDS})")
_PROMPT = re.compile(rf"^\$(\s+|(?={_CMDS}))", re.MULTILINE)  # "$ cmd" / "$cmd", but never "$VAR"
# Windows `bash` is often the WSL stub (fails with no distro installed), so only trust it on POSIX.
_BASH = shutil.which("bash") if os.name != "nt" else None
_ERROR_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*(Error|Exception)\b.*)$", re.MULTILINE)
_MISSING_MODULE = re.compile(r"No module named '([\w.]+)'")


def build_query(problem: str, error_log: str = "") -> str:
    """The problem statement plus the final `XxxError: message` line of the log, if any."""
    errors = _ERROR_LINE.findall(error_log)
    return " ".join(p for p in (problem.strip(), errors[-1][0].strip() if errors else "") if p)


def classify(code: str, lang_hint: str = "") -> tuple[str, str] | None:
    """Return (lang, cleaned_code) for valid Python or shell, else None. Never executes the code."""
    code = code.strip()
    if lang_hint not in ("sh", "bash", "shell", "console"):
        try:
            ast.parse(code)
            if not _SHELL_START.match(code):  # `pip install x` is not Python, even if it parsed
                return "python", code
        except SyntaxError:
            pass
    if lang_hint not in ("sh", "bash", "shell", "console") and not _SHELL_START.match(code):
        return None
    shell = _PROMPT.sub("", code)
    return ("shell", shell) if _valid_shell(shell) else None


def _valid_shell(script: str) -> bool:
    """Portable parse-only check: balanced quotes/parens, no dangling operator; plus `bash -n` on POSIX."""
    try:
        tokens = list(shlex.shlex(script, posix=True, punctuation_chars=True))
    except ValueError:  # unbalanced quotes / trailing escape
        return False
    depth = 0
    for t in tokens:
        if set(t) <= set("();<>|&"):
            depth += t.count("(") - t.count(")")
            if depth < 0:
                return False
    if depth or not tokens or (set(tokens[-1]) <= set("();<>|&") and tokens[-1][-1] in "|&(<>"):
        return False
    if _BASH:
        return subprocess.run([_BASH, "-n"], input=script, capture_output=True, text=True, timeout=10).returncode == 0
    return True


def dependencies(lang: str, code: str) -> list[str]:
    """Third-party PyPI packages a candidate needs (stdlib imports and pip flags are dropped)."""
    if lang == "shell":
        pkgs = [w for m in re.finditer(r"pip3? install ([^\n;&|]+)", code) for w in m.group(1).split()]
        return sorted({p for p in pkgs if not p.startswith("-")})
    mods = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.add(node.module.split(".")[0])
    return sorted(_PACKAGE_FOR.get(m, m) for m in mods - set(sys.stdlib_module_names) if m != "brain")


class ResearchEngine:
    def __init__(self, reach: AgentReach, registry: SkillRegistry, candidates_dir: Path | str = DEFAULT_DIR) -> None:
        self.reach = reach
        self.registry = registry
        self.candidates_dir = Path(candidates_dir)

    def investigate(
        self, problem: str, error_log: str = "",
        urls: tuple[str, ...] = (), repos: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Collect, validate and rank solution candidates. `repos` are "owner/name" strings."""
        query = build_query(problem, error_log)
        raw: list[tuple[str, str, str]] = []  # (code, lang_hint, source)
        sources = [
            (lambda: [(c, "", r["url"]) for r in self.reach.search_stackoverflow(query) for c in r["code"]]),
            *[(lambda u=u: [(c, "", u) for c in self.reach.fetch_page(u)["code"]]) for u in urls],
            *[(lambda r=r: [(b["code"], b["lang"], f"github:{r}")
                            for b in self.reach.github_readme(*r.split("/", 1))["code"]]) for r in repos],
        ]
        for source in sources:
            try:
                raw += source()
            except (httpx.HTTPError, PermissionError, ValueError) as exc:  # one bad source never sinks the rest
                log.warning("research.source_failed", error=str(exc))

        missing = _MISSING_MODULE.findall(error_log)
        terms = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
        seen, candidates = set(), []
        for code, hint, source in raw:
            hit = classify(code, hint)
            if not hit or hit[1] in seen:
                continue
            seen.add(hit[1])
            lang, clean = hit
            deps = dependencies(lang, clean)
            score = sum(t in clean.lower() for t in terms) + 2 * any(_PACKAGE_FOR.get(m, m) in deps for m in missing)
            candidates.append({"lang": lang, "code": clean, "source": source, "dependencies": deps, "score": score})
        candidates.sort(key=lambda c: -c["score"])
        log.info("research.investigated", query=query, raw=len(raw), valid=len(candidates))
        return candidates

    def research(
        self, skill_name: str, problem: str, error_log: str = "",
        urls: tuple[str, ...] = (), repos: tuple[str, ...] = (),
        category: str = "Research Discovered", max_candidates: int = 3,
    ) -> list[dict[str, Any]]:
        """Investigate, then register the top candidates as Discovered skills `<skill_name>.cN`."""
        registered = []
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(self.investigate(problem, error_log, urls, repos)[:max_candidates], 1):
            name = f"{skill_name}.c{i}"
            if any(s["name"] == name for s in self.registry.list()):
                log.info("research.already_registered", name=name)  # never overwrite a registered skill's code
                continue
            path = self.candidates_dir / f"{name}.{'py' if c['lang'] == 'python' else 'sh'}"
            path.write_text(c["code"] + "\n", encoding="utf-8")
            registered.append(self.registry.register(
                name, category, f"{problem} (source: {c['source']})", c["dependencies"], str(path)))
        return registered
