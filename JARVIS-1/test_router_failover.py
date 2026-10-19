"""
test_router_failover.py -- Offline check that a rate-limited free provider
fails over to the next one. No network, no real keys.

Run with:
    python test_router_failover.py
"""
from __future__ import annotations

import asyncio
import os

os.environ.update(GROQ_API_KEY="test-groq", GOOGLE_API_KEY="test-google", OPENROUTER_API_KEY="")

import httpx

from brain.router import llm_router

calls: list[str] = []


async def fake_call(provider, model_id, messages, max_tokens=2048, temperature=0.7):
    calls.append(f"{provider}/{model_id}")
    if provider == "groq":
        req = httpx.Request("POST", llm_router._PROVIDER_URLS[provider])
        raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))
    return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


async def main() -> None:
    llm_router._call_openai_compatible = fake_call
    raw, provider, model_id = await llm_router._complete_with_fallback(
        "groq/llama-3.3-70b-versatile", [{"role": "user", "content": "hi"}]
    )
    # groq 429 -> skips keyless openrouter -> lands on google
    assert (provider, model_id) == ("google", "gemini-2.5-flash"), (provider, model_id)
    assert calls == ["groq/llama-3.3-70b-versatile", "google/gemini-2.5-flash"], calls
    print(f"[OK] failover path: {' -> '.join(calls)}")


if __name__ == "__main__":
    asyncio.run(main())
