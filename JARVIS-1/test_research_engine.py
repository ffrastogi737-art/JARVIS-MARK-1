"""
test_research_engine.py -- Verifies the Phase 5 Agent Reach layer & Research Engine.

All HTTP traffic is served by an in-process httpx.MockTransport (Stack Exchange API,
a docs site with robots.txt, raw.githubusercontent.com), so the suite is deterministic
and needs no network.

Run with:
    python test_research_engine.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx

from brain.research.agent_reach import USER_AGENT, AgentReach, extract, markdown_code
from brain.research.research_engine import ResearchEngine, build_query, classify, dependencies
from brain.skills.registry import SkillRegistry

DOCS_HTML = """<html><head><title>YAML Guide</title><script>alert('x')</script></head>
<body><nav>menu</nav><h1>Loading YAML</h1><p>Use safe_load for config files.</p>
<pre><code>import yaml
with open("config.yaml") as f:
    config = yaml.safe_load(f)</code></pre>
<p>Inline <code>yaml.safe_load</code> call.</p></body></html>"""

README_MD = """# yamltools
Install:
```bash
pip install --upgrade pyyaml
```
Use:
```python
import json, yaml
print(json.dumps(yaml.safe_load("a: 1")))
```
```text
not code at all {{{
```
"""

SO_ANSWER = """<p>Install the package:</p><pre><code>$ pip install pyyaml
</code></pre><p>then</p><pre><code>import yaml
data = yaml.safe_load(open("config.yaml"))
</code></pre><p>Broken attempt:</p><pre><code>def broken(:
    pass
</code></pre>"""

requests_seen: list[httpx.Request] = []


def handler(req: httpx.Request) -> httpx.Response:
    requests_seen.append(req)
    host, path = req.url.host, req.url.path
    if host == "api.stackexchange.com" and path == "/2.3/search/advanced":
        return httpx.Response(200, json={"items": [
            {"question_id": 101, "title": "ModuleNotFoundError yaml", "link": "https://stackoverflow.com/q/101"}]})
    if host == "api.stackexchange.com" and path == "/2.3/questions/101/answers":
        return httpx.Response(200, json={"items": [{"question_id": 101, "body": SO_ANSWER}]})
    if host == "docs.example.com" and path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    if host == "docs.example.com" and path in ("/guide", "/private"):
        return httpx.Response(200, text=DOCS_HTML)
    if host == "noro.example.com" and path == "/robots.txt":
        return httpx.Response(404)
    if host == "noro.example.com":
        return httpx.Response(200, text="<p>hi</p>")
    if host == "raw.githubusercontent.com" and path == "/acme/yamltools/HEAD/README.md":
        return httpx.Response(200, text=README_MD)
    if host == "raw.githubusercontent.com" and path == "/acme/yamltools/v1/src/load.py":
        return httpx.Response(200, text="def load():\n    return 1\n")
    return httpx.Response(404)


def make_reach() -> AgentReach:
    return AgentReach(httpx.Client(transport=httpx.MockTransport(handler)))


def check(label: str, cond: bool, detail: object = "") -> None:
    assert cond, f"{label} FAILED: {detail}"
    print(f"    {label:<52} [OK] PASSED")


def test_parsing() -> None:
    print("\n[1] HTML & Markdown parsing")
    page = extract(DOCS_HTML)
    check("title extracted", page["title"] == "YAML Guide", page["title"])
    check("<script>/<nav> stripped from text", "alert" not in page["text"] and "menu" not in page["text"])
    check("readable text kept", "Use safe_load for config files." in page["text"])
    check("<pre> code block extracted", len(page["code"]) == 1 and page["code"][0].startswith("import yaml"), page["code"])
    blocks = markdown_code(README_MD)
    check("fenced Markdown blocks + languages", [b["lang"] for b in blocks] == ["bash", "python", "text"], blocks)


def test_reach() -> None:
    print("\n[2] Agent Reach fetching (robots.txt, GitHub, Stack Exchange)")
    reach = make_reach()
    check("docs page allowed by robots.txt", reach.fetch_page("https://docs.example.com/guide")["title"] == "YAML Guide")
    try:
        reach.fetch_page("https://docs.example.com/private")
        raise AssertionError("robots.txt ignored")
    except PermissionError:
        check("robots.txt Disallow respected (no fetch)", all(r.url.path != "/private" for r in requests_seen))
    check("missing robots.txt (404) -> allowed", reach.fetch_page("https://noro.example.com/x")["text"] == "hi")
    check("honest User-Agent on every request", all(r.headers["user-agent"] == USER_AGENT for r in requests_seen))

    readme = reach.github_readme("acme", "yamltools")
    check("GitHub README code blocks", len(readme["code"]) == 3 and "safe_load" in readme["code"][1]["code"])
    check("GitHub raw file at ref", reach.github_file("acme", "yamltools", "/src/load.py", "v1").startswith("def load"))
    try:
        reach.github_readme("acme", "missing")
        raise AssertionError("404 swallowed")
    except httpx.HTTPStatusError:
        check("HTTP errors surface as HTTPStatusError", True)

    so = reach.search_stackoverflow("yaml")
    check("Stack Exchange answers -> code snippets", len(so) == 1 and len(so[0]["code"]) == 3, so)
    check("JSON API helper", reach.get_json("https://api.stackexchange.com/2.3/search/advanced")["items"][0]["question_id"] == 101)


def test_validation() -> None:
    print("\n[3] Query building & syntax validation")
    log = 'Traceback (most recent call last):\n  File "x.py", line 1\nModuleNotFoundError: No module named \'yaml\''
    q = build_query("load config file", log)
    check("query = problem + final error line", q == "load config file ModuleNotFoundError: No module named 'yaml'", q)
    check("valid Python accepted", classify("import yaml\nyaml.safe_load('a: 1')") is not None
          and classify("import yaml\nyaml.safe_load('a: 1')")[0] == "python")
    check("invalid Python rejected", classify("def broken(:\n    pass") is None)
    check("shell detected, `$ ` prompt stripped", classify("$ pip install pyyaml") == ("shell", "pip install pyyaml"))
    check("bad shell rejected (bash -n)", classify("pip install x && (", "bash") is None)
    check("prose in a code fence rejected", classify("not code at all {{{", "text") is None)
    check("deps: imports -> PyPI names, stdlib dropped",
          dependencies("python", "import os, json\nimport yaml\nfrom bs4 import BeautifulSoup") == ["beautifulsoup4", "pyyaml"])
    check("deps: pip flags dropped", dependencies("shell", "pip install --upgrade pyyaml six") == ["pyyaml", "six"])


def test_research(tmp: Path) -> None:
    print("\n[4] Research -> Skill Registry (Discovered)")
    reg = SkillRegistry(tmp / "registry.json")
    engine = ResearchEngine(make_reach(), reg, tmp / "candidates")
    log = "ModuleNotFoundError: No module named 'yaml'"
    cands = engine.investigate("load yaml config", log, urls=("https://docs.example.com/guide",),
                               repos=("acme/yamltools", "acme/missing"))
    check("only syntax-valid candidates kept", all(classify(c["code"]) or c["lang"] == "shell" for c in cands)
          and not any("broken" in c["code"] for c in cands), cands)
    check("duplicates removed", len({c["code"] for c in cands}) == len(cands))
    check("failing source (404 repo) skipped, others used", {c["source"] for c in cands} >= {
        "https://stackoverflow.com/q/101", "https://docs.example.com/guide", "github:acme/yamltools"})
    check("ranked: best candidate needs pyyaml", "pyyaml" in cands[0]["dependencies"], cands[0])

    skills = engine.research("config.yaml_loader", "load yaml config", log,
                             urls=("https://docs.example.com/guide",), repos=("acme/yamltools",))
    check("top 3 registered", [s["name"] for s in skills] == [f"config.yaml_loader.c{i}" for i in (1, 2, 3)])
    check("all in Discovered state", all(s["status"] == "discovered" for s in skills))
    check("candidate code written to entrypoint", all(Path(s["entrypoint"]).read_text().strip() for s in skills))
    check("dependencies recorded for promote()", "pyyaml" in skills[0]["dependencies"], skills[0])
    saved = json.loads((tmp / "registry.json").read_text())
    check("registry persisted to JSON", set(saved) == {s["name"] for s in skills})
    before = [Path(s["entrypoint"]).read_text() for s in skills]
    again = engine.research("config.yaml_loader", "load yaml config", log)
    check("re-research does not duplicate registrations", again == [] and len(reg.list()) == 3)
    check("re-research leaves registered code untouched", [Path(s["entrypoint"]).read_text() for s in skills] == before)


if __name__ == "__main__":
    print("=" * 60)
    print("  JARVIS Phase 5 -- Agent Reach & Research Engine Tests")
    print("=" * 60)
    test_parsing()
    test_reach()
    test_validation()
    with tempfile.TemporaryDirectory(prefix="jarvis_research_") as tmp:
        test_research(Path(tmp))
    print("\nAll Research Engine tests passed.")
