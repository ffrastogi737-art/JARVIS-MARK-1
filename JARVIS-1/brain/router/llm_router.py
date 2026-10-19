"""
brain/router/llm_router.py — Intelligent LLM Router with Task Complexity Classifier.

Architecture
────────────
                        ┌─────────────────────┐
  Inbound Request  ───▶ │  Complexity          │
                        │  Classifier          │  (cheap/fast model — always)
                        └────────┬────────────┘
                                 │  TaskComplexity tier
                        ┌────────▼────────────┐
                        │  Model Selector      │  maps tier → provider/model
                        └────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
      ┌───────▼──────┐  ┌────────▼──────┐  ┌────────▼──────┐
      │ Groq Provider│  │ Anthropic Prov│  │ OpenAI Prov   │  …
      └──────────────┘  └───────────────┘  └───────────────┘

Cost tiers (cheapest → most expensive):
  SIMPLE   → Groq Llama-3.1-8B
  MODERATE → Groq Llama-3.3-70B
  COMPLEX  → Claude Sonnet
  EXPERT   → GPT-4o

The Classifier itself always runs on the TIER_SIMPLE model to minimise cost.
If no real API keys are configured, the router operates in MOCK mode for
local development and testing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import structlog

from brain.config import get_settings
from brain.models.schemas import (
    ChatMessage,
    ClassificationResult,
    TaskComplexity,
)

log = structlog.get_logger(__name__)

# ── Classifier system prompt ───────────────────────────────────────────────────
_CLASSIFIER_SYSTEM_PROMPT = """You are a task complexity classifier for an AI routing system.
Analyse the user's latest message and classify it into exactly one of these tiers:

  simple   — Trivial lookup, single fact, basic arithmetic, yes/no, short translation.
  moderate — Multi-step reasoning, code generation (<100 lines), summarisation, data extraction.
  complex  — Architecture design, deep analysis, long documents, sophisticated debugging, research.
  expert   — Novel research, frontier science, adversarial reasoning, very large context tasks.

Reply with ONLY valid JSON (no markdown fences), matching this exact schema:
{
  "complexity": "<simple|moderate|complex|expert>",
  "confidence": <float 0.0–1.0>,
  "reasoning": "<one sentence>",
  "estimated_tokens": <int>
}"""


# ── Provider base URLs ─────────────────────────────────────────────────────────
_PROVIDER_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "google": "https://generativelanguage.googleapis.com/v1beta/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}

_PROVIDER_API_KEY_ATTR: dict[str, str] = {
    "groq": "groq_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "deepseek": "deepseek_api_key",
}


def _split_provider_model(model_str: str) -> tuple[str, str]:
    """Split 'provider/model-id' into ('provider', 'model-id')."""
    parts = model_str.split("/", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Model string must be 'provider/model-id', got: {model_str!r}"
        )
    return parts[0].lower(), parts[1]


def _get_api_key(provider: str) -> str:
    settings = get_settings()
    attr = _PROVIDER_API_KEY_ATTR.get(provider, "")
    return getattr(settings, attr, "") if attr else ""


# ── Mock response (used when no API key is present) ────────────────────────────
def _mock_classification(task_text: str) -> dict[str, Any]:
    """Heuristic complexity estimation without calling any API."""
    word_count = len(task_text.split())
    if word_count < 15:
        complexity, confidence = "simple", 0.92
        reasoning = "Short query — heuristic: simple lookup."
        est_tokens = word_count * 2
    elif word_count < 60:
        complexity, confidence = "moderate", 0.80
        reasoning = "Medium-length query — likely multi-step."
        est_tokens = word_count * 3
    elif word_count < 200:
        complexity, confidence = "complex", 0.75
        reasoning = "Long query — deep analysis expected."
        est_tokens = word_count * 4
    else:
        complexity, confidence = "expert", 0.70
        reasoning = "Very long prompt — expert-level reasoning required."
        est_tokens = word_count * 5

    return {
        "complexity": complexity,
        "confidence": confidence,
        "reasoning": reasoning,
        "estimated_tokens": est_tokens,
    }


def _mock_chat_response(model_id: str, messages: list[ChatMessage]) -> dict[str, Any]:
    """Return a plausible mock response when no API key is configured."""
    last_user = next(
        (m.content for m in reversed(messages) if m.role == "user"),
        "Hello",
    )
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        f"[MOCK — {model_id}] I received your message: '{last_user[:80]}'. "
                        "Configure a real API key in .env to get genuine responses."
                    ),
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90},
    }


# ── Low-level HTTP call (OpenAI-compatible) ────────────────────────────────────
async def _call_openai_compatible(
    provider: str,
    model_id: str,
    messages: list[dict],
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> dict[str, Any]:
    api_key = _get_api_key(provider)
    if not api_key:
        log.warning("llm_router.no_api_key", provider=provider, mode="mock")
        return _mock_chat_response(model_id, [ChatMessage(**m) for m in messages])

    url = _PROVIDER_URLS[provider]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()


# ── Anthropic-specific call (different schema) ─────────────────────────────────
async def _call_anthropic(
    model_id: str,
    messages: list[dict],
    max_tokens: int = 2048,
) -> dict[str, Any]:
    api_key = _get_api_key("anthropic")
    if not api_key:
        log.warning("llm_router.no_api_key", provider="anthropic", mode="mock")
        return _mock_chat_response(model_id, [ChatMessage(**m) for m in messages])

    # Separate system messages for Anthropic v1/messages API
    system_msgs = [m["content"] for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] != "system"]

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": user_msgs,
        "max_tokens": max_tokens,
    }
    if system_msgs:
        payload["system"] = "\n\n".join(system_msgs)

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            _PROVIDER_URLS["anthropic"], headers=headers, json=payload
        )
        resp.raise_for_status()
        raw = resp.json()

    # Normalise to OpenAI-compatible shape
    content_text = ""
    if raw.get("content"):
        for block in raw["content"]:
            if block.get("type") == "text":
                content_text += block["text"]

    return {
        "choices": [{"message": {"role": "assistant", "content": content_text}}],
        "usage": {
            "prompt_tokens": raw.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": raw.get("usage", {}).get("output_tokens", 0),
            "total_tokens": (
                raw.get("usage", {}).get("input_tokens", 0)
                + raw.get("usage", {}).get("output_tokens", 0)
            ),
        },
    }


async def _dispatch(
    provider: str,
    model_id: str,
    messages: list[dict],
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> dict[str, Any]:
    """Route to the correct provider-specific call."""
    if provider == "anthropic":
        return await _call_anthropic(model_id, messages, max_tokens)
    return await _call_openai_compatible(
        provider, model_id, messages, max_tokens, temperature
    )


# ── Task Complexity Classifier ─────────────────────────────────────────────────
class TaskComplexityClassifier:
    """
    Gatekeeper that runs on EVERY request using the cheapest configured model.

    Returns a ClassificationResult with the recommended complexity tier and
    the model that should handle the actual task.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._tier_model_map: dict[TaskComplexity, str] = {
            TaskComplexity.SIMPLE: self.settings.tier_simple_model,
            TaskComplexity.MODERATE: self.settings.tier_moderate_model,
            TaskComplexity.COMPLEX: self.settings.tier_complex_model,
            TaskComplexity.EXPERT: self.settings.tier_expert_model,
        }

    def _select_model_for_tier(self, complexity: TaskComplexity) -> str:
        return self._tier_model_map[complexity]

    async def classify(
        self, messages: list[ChatMessage], force: TaskComplexity | None = None
    ) -> ClassificationResult:
        """
        Classify the task complexity.

        If `force` is provided the classifier is bypassed entirely (useful for
        testing or when the caller knows the tier in advance).
        """
        if force is not None:
            log.info(
                "classifier.forced_tier",
                tier=force.value,
            )
            model_str = self._select_model_for_tier(force)
            return ClassificationResult(
                complexity=force,
                confidence=1.0,
                reasoning="Tier manually forced by caller.",
                estimated_tokens=0,
                routed_model=model_str,
            )

        # Build a minimal context: only the last user message (keep cost minimal)
        last_user_content = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "",
        )

        classifier_provider, classifier_model_id = _split_provider_model(
            self.settings.classifier_model
        )

        raw_json: dict[str, Any] = {}

        api_key = _get_api_key(classifier_provider)
        if not api_key:
            # No key → heuristic mock
            log.info(
                "classifier.mock_heuristic",
                reason="no_api_key",
                provider=classifier_provider,
            )
            raw_json = _mock_classification(last_user_content)
        else:
            payload_messages = [
                {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": last_user_content},
            ]
            resp = await _dispatch(
                classifier_provider,
                classifier_model_id,
                payload_messages,
                max_tokens=256,
                temperature=0.1,  # Low temp → deterministic classification
            )
            reply_text: str = resp["choices"][0]["message"]["content"]

            # Robust JSON extraction (handle occasional markdown fences)
            json_match = re.search(r"\{.*\}", reply_text, re.DOTALL)
            if json_match:
                raw_json = json.loads(json_match.group())
            else:
                log.warning(
                    "classifier.parse_failed",
                    raw=reply_text[:200],
                    fallback="moderate",
                )
                raw_json = {
                    "complexity": "moderate",
                    "confidence": 0.5,
                    "reasoning": "Classifier response unparseable; defaulting to moderate.",
                    "estimated_tokens": 500,
                }

        complexity = TaskComplexity(raw_json.get("complexity", "moderate"))
        routed_model = self._select_model_for_tier(complexity)

        result = ClassificationResult(
            complexity=complexity,
            confidence=float(raw_json.get("confidence", 0.8)),
            reasoning=str(raw_json.get("reasoning", "")),
            estimated_tokens=int(raw_json.get("estimated_tokens", 0)),
            routed_model=routed_model,
        )

        log.info(
            "classifier.result",
            complexity=result.complexity.value,
            confidence=result.confidence,
            routed_model=result.routed_model,
            reasoning=result.reasoning,
        )
        return result


# ── Main Router ────────────────────────────────────────────────────────────────
class LLMRouter:
    """
    Orchestrates the full pipeline:
      classify → select model → call provider → return response.

    Usage:
        router = LLMRouter()
        response, classification, usage = await router.route(chat_request)
    """

    def __init__(self) -> None:
        self.classifier = TaskComplexityClassifier()

    async def route(
        self,
        messages: list[ChatMessage],
        force_complexity: TaskComplexity | None = None,
        conversation_id: str = "",
    ) -> tuple[ChatMessage, ClassificationResult, dict[str, int], str, str, float]:
        """
        Route a conversation to the appropriate model.

        Returns:
            (reply_message, classification, usage_dict, provider, model_id, latency_ms)
        """
        t0 = time.monotonic()

        # ── Step 1: Classify ─────────────────────────────────────────────────
        classification = await self.classifier.classify(messages, force=force_complexity)

        # ── Step 2: Resolve provider / model ─────────────────────────────────
        provider, model_id = _split_provider_model(classification.routed_model)

        log.info(
            "router.dispatching",
            conversation_id=conversation_id,
            provider=provider,
            model=model_id,
            complexity=classification.complexity.value,
        )

        # ── Step 3: Build message payload ─────────────────────────────────────
        msg_payload = [{"role": m.role, "content": m.content} for m in messages]

        # ── Step 4: Call the provider ─────────────────────────────────────────
        try:
            raw = await _dispatch(provider, model_id, msg_payload)
        except httpx.HTTPStatusError as exc:
            log.error(
                "router.provider_error",
                provider=provider,
                model=model_id,
                status=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise
        except Exception as exc:
            log.error("router.unexpected_error", exc=str(exc))
            raise

        # ── Step 5: Extract reply ─────────────────────────────────────────────
        reply_content: str = raw["choices"][0]["message"]["content"]
        reply_message = ChatMessage(role="assistant", content=reply_content)

        usage: dict[str, int] = raw.get("usage", {})
        latency_ms = (time.monotonic() - t0) * 1000

        log.info(
            "router.completed",
            conversation_id=conversation_id,
            latency_ms=round(latency_ms, 1),
            total_tokens=usage.get("total_tokens", 0),
        )

        return reply_message, classification, usage, provider, model_id, latency_ms
