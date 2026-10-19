"""
brain/models/schemas.py — Shared Pydantic schemas used across the API.

Defines:
  • TaskComplexity   — enum of complexity tiers
  • ClassificationResult — output from the Task Complexity Classifier
  • ChatMessage       — a single conversation turn
  • ChatRequest        — inbound payload to /api/v1/chat
  • ChatResponse       — full response from /api/v1/chat
  • HealthResponse     — /health endpoint schema
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Task Complexity Tiers ──────────────────────────────────────────────────────

class TaskComplexity(str, Enum):
    """
    Complexity tiers used by the LLM Router to select the appropriate model.

    SIMPLE   → trivial Q&A, quick facts, single-step transforms          (cheapest)
    MODERATE → multi-step reasoning, code generation, summarisation
    COMPLEX  → deep analysis, architecture design, long-context tasks
    EXPERT   → frontier-level reasoning, novel research, largest context  (most expensive)
    """
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    EXPERT = "expert"


# ── Classifier Output ──────────────────────────────────────────────────────────

class ClassificationResult(BaseModel):
    """Result produced by the Task Complexity Classifier."""
    complexity: TaskComplexity
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence 0–1")
    reasoning: str = Field(description="One-sentence justification from the classifier")
    estimated_tokens: int = Field(ge=0, description="Rough token estimate for the task")
    routed_model: str = Field(description="The model ID selected for this task")


# ── Conversation Primitives ────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    """A single turn in a conversation."""
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    """Inbound request body for the /api/v1/chat endpoint."""
    messages: list[ChatMessage] = Field(min_length=1)
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    # Allow the caller to override the auto-routed tier (optional)
    force_complexity: TaskComplexity | None = Field(
        default=None,
        description="If set, skip the classifier and use this tier directly.",
    )
    stream: bool = Field(default=False)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """Full response returned by /api/v1/chat."""
    conversation_id: str
    message: ChatMessage
    classification: ClassificationResult
    provider: str = Field(description="Which LLM provider was called")
    model: str = Field(description="Exact model ID that generated the response")
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Token usage: prompt_tokens, completion_tokens, total_tokens",
    )
    latency_ms: float = Field(description="End-to-end latency in milliseconds")


# ── Health ─────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Response for the /health endpoint."""
    status: str
    version: str
    environment: str
    providers_configured: list[str]
