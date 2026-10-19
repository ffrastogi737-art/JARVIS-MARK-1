"""
brain/api/routes/chat.py — /api/v1/chat endpoint.

Receives a ChatRequest, runs it through the LLM Router, and returns a ChatResponse.
The router handles complexity classification and model selection transparently.
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException

from brain.models.schemas import ChatResponse, ChatRequest
from brain.router.llm_router import LLMRouter

log = structlog.get_logger(__name__)
router = APIRouter()

# One shared router instance for the lifetime of the process
_llm_router = LLMRouter()


@router.post("/chat", response_model=ChatResponse, summary="Send a chat message to JARVIS")
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Primary chat endpoint.

    Flow:
    1. Task Complexity Classifier analyses the latest user message.
    2. LLM Router selects the most cost-effective model for the detected tier.
    3. Response is returned with full classification metadata so the caller
       can see which model was used and why.
    """
    conversation_id = request.conversation_id or str(uuid4())

    log.info(
        "chat.received",
        conversation_id=conversation_id,
        num_messages=len(request.messages),
        force_complexity=request.force_complexity,
    )

    try:
        reply, classification, usage, provider, model_id, latency_ms = (
            await _llm_router.route(
                messages=request.messages,
                force_complexity=request.force_complexity,
                conversation_id=conversation_id,
            )
        )
    except Exception as exc:
        log.error("chat.error", exc=str(exc), conversation_id=conversation_id)
        raise HTTPException(status_code=502, detail=f"LLM provider error: {exc}") from exc

    return ChatResponse(
        conversation_id=conversation_id,
        message=reply,
        classification=classification,
        provider=provider,
        model=model_id,
        usage=usage,
        latency_ms=round(latency_ms, 2),
    )
