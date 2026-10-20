"""
brain/research/agent_reach.py — Agent Reach web-access layer (Phase 5, Master Doc §13).

Inspired by https://github.com/Panniantong/Agent-Reach; free-tier, standard tooling only:
  • fetch_page()     — GET an HTML page (honours robots.txt) -> title, text, code blocks
  • get_json()       — GET a public JSON API
  • github_readme()  — README of a public GitHub repo -> Markdown + fenced code blocks
  • github_file()    — raw source of a file in a public GitHub repo
  • search_stackoverflow() — Stack Exchange public API search -> questions with code

Every request identifies itself with one honest User-Agent; pages whose robots.txt
disallows us are skipped, not worked around.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger(__name__)

USER_AGENT = "JARVIS-AgentReach/1.0 (+https://github.com/ffrastogi737-art/JARVIS-MARK-1)"
_FENCE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)```", re.DOTALL)


class AgentReach:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.client.headers["User-Agent"] = USER_AGENT
        self._robots: dict[str, RobotFileParser] = {}

    def _get(self, url: str, **kw: Any) -> httpx.Response:
        resp = self.client.get(url, **kw)
        resp.raise_for_status()
        log.info("reach.get", url=url, status=resp.status_code, bytes=len(resp.content))
        return resp

    def allowed(self, url: str) -> bool:
        """robots.txt check; a missing robots.txt (4xx) means allowed, an unreachable one means not."""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(origin + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.disallow_all = True
            self._robots[origin] = rp
        return self._robots[origin].can_fetch(USER_AGENT, url)

    def fetch_page(self, url: str) -> dict[str, Any]:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        return extract(self._get(url).text)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._get(url, params=params).json()

    def github_file(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str:
        return self._get(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path.lstrip('/')}").text

    def github_readme(self, owner: str, repo: str) -> dict[str, Any]:
        md = self.github_file(owner, repo, "README.md")
        return {"markdown": md, "code": markdown_code(md)}

    def search_stackoverflow(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Stack Exchange public API (no key needed, ~300 req/day/IP) -> answered questions + answer code."""
        api = "https://api.stackexchange.com/2.3"
        questions = self.get_json(f"{api}/search/advanced", params={
            "q": query, "site": "stackoverflow", "order": "desc", "sort": "relevance",
            "answers": 1, "pagesize": limit,
        }).get("items", [])
        if not questions:
            return []
        ids = ";".join(str(q["question_id"]) for q in questions)
        answers = self.get_json(f"{api}/questions/{ids}/answers", params={
            "site": "stackoverflow", "order": "desc", "sort": "votes", "filter": "withbody",
        }).get("items", [])
        return [{
            "title": q.get("title", ""), "url": q.get("link", ""),
            "code": [c for a in answers if a["question_id"] == q["question_id"] for c in extract(a["body"])["code"]],
        } for q in questions]


def extract(html: str) -> dict[str, Any]:
    """Readable text plus code snippets; inline <code> outside <pre> is kept only if multi-line."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    code = [pre.get_text() for pre in soup.find_all("pre")]
    code += [c.get_text() for c in soup.find_all("code") if not c.find_parent("pre") and "\n" in c.get_text()]
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n")).strip()
    return {"title": title, "text": text, "code": [c.strip("\n") for c in code if c.strip()]}


def markdown_code(markdown: str) -> list[dict[str, str]]:
    return [{"lang": m.group(1).lower(), "code": m.group(2).strip("\n")} for m in _FENCE.finditer(markdown)]
