"""
brain/api/routes/skills.py — Read-only Skill Registry listing for the HUD (Phase 8).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from brain.api.routes.tether import require_tether_token
from brain.skills.registry import SkillRegistry

router = APIRouter(dependencies=[Depends(require_tether_token)])


@router.get("/skills", summary="List registered skills and their status")
async def skills() -> dict:
    return {"skills": SkillRegistry().list()}
