"""
brain/api/routes/router_info.py — /api/v1/router/info and /api/v1/router/classify endpoints.

Exposes the classifier as a standalone endpoint so you can test it
independently of the full chat pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from brain.config import get_settings
from brain.models.schemas import ChatMessage, ClassificationResult, TaskComplexity
from brain.router.llm_router import TaskComplexityClassifier

router = APIRouter()
_classifier = TaskComplexityClassifier()


class RouterInfoResponse(BaseModel):
    classifier_model: str
    tier_routing: dict[str, str]
    description: str


class ClassifyRequest(BaseModel):
    message: str = Field(min_length=1, description="The text to classify.")
    force_complexity: TaskComplexity | None = None


@router.get("/router/info", response_model=RouterInfoResponse, summary="Show router configuration")
async def router_info() -> RouterInfoResponse:
    """Return the current routing table and classifier model."""
    s = get_settings()
    return RouterInfoResponse(
        classifier_model=s.classifier_model,
        tier_routing={
            "simple": s.tier_simple_model,
            "moderate": s.tier_moderate_model,
            "complex": s.tier_complex_model,
            "expert": s.tier_expert_model,
        },
        description=(
            "The classifier runs on the cheapest model and decides which tier "
            "model handles the actual task — saving tokens on simple requests."
        ),
    )


@router.post("/router/classify", response_model=ClassificationResult, summary="Classify task complexity")
async def classify(body: ClassifyRequest) -> ClassificationResult:
    """
    Classify the complexity of a single message without sending it to a model.
    Useful for debugging the router's decisions.
    """
    messages = [ChatMessage(role="user", content=body.message)]
    return await _classifier.classify(messages, force=body.force_complexity)
