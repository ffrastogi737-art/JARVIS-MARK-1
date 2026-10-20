"""
test_router_failover.py -- Offline check of LLMRouter failover (no network, no keys).

Run with:
    python test_router_failover.py
"""
from __future__ import annotations

import asyncio

import httpx

from brain.models.schemas import ChatMessage, TaskComplexity
from brain.router import llm_router
from brain.router.llm_router import LLMRouter

_OK = {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.test")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))


async def _route_with(failures: dict[str, Exception]) -> tuple[str, list[str]]:
    """Patch _dispatch so the providers named in `failures` raise; return (provider, call order)."""
    calls: list[str] = []

    async def fake_dispatch(provider, model_id, messages, *a, **kw):
        calls.append(provider)
        if provider in failures:
            raise failures[provider]
        return _OK

    original = llm_router._dispatch
    llm_router._dispatch = fake_dispatch
    try:
        _, _, _, provider, _, _ = await LLMRouter().route(
            [ChatMessage(role="user", content="hi")], force_complexity=TaskComplexity.COMPLEX
        )
    finally:
        llm_router._dispatch = original
    return provider, calls


async def main() -> None:
    # Primary (google) rate-limited -> first fallback (cerebras) answers
    provider, calls = await _route_with({"google": _http_error(429)})
    assert (provider, calls) == ("cerebras", ["google", "cerebras"]), calls
    print("[1] 429 failover            [OK] PASSED")

    # Timeout on primary and first fallback -> sambanova answers
    provider, calls = await _route_with(
        {"google": httpx.ReadTimeout("t"), "cerebras": httpx.ConnectTimeout("t")}
    )
    assert provider == "sambanova", calls
    print("[2] timeout failover        [OK] PASSED")

    # 400 is a caller bug, not a provider outage -> no failover
    try:
        await _route_with({"google": _http_error(400)})
        raise AssertionError("400 should not fail over")
    except httpx.HTTPStatusError:
        pass
    print("[3] 400 not retried         [OK] PASSED")

    # Every provider down -> last error surfaces
    try:
        await _route_with({p: _http_error(503) for p in llm_router.PROVIDER_API_KEY_ATTR})
        raise AssertionError("all-down should raise")
    except httpx.HTTPStatusError:
        pass
    print("[4] all providers down      [OK] PASSED")

    print("\nAll failover tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
