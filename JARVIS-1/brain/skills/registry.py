"""
brain/skills/registry.py — Capability Registry & Skill Lifecycle (Phase 4, Master Doc §14).

Lifecycle:  Discovered -> Testing (Sandbox) -> Assimilated -> Active
                               └──> Failed ──> (re-test)

``promote()`` drives a skill from Discovered to Active with no human in the loop:
its dependencies are verified in the sandbox (DependencyResolver) and the skill is
assimilated and activated only if every one passes. Metadata persists to JSON.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from brain.skills.dependency_resolver import DependencyResolver

log = structlog.get_logger(__name__)

DEFAULT_PATH = Path(__file__).parent / "registry.json"


class SkillStatus(str, Enum):
    DISCOVERED = "discovered"
    TESTING = "testing"
    ASSIMILATED = "assimilated"
    ACTIVE = "active"
    FAILED = "failed"


_TRANSITIONS: dict[SkillStatus, set[SkillStatus]] = {
    SkillStatus.DISCOVERED: {SkillStatus.TESTING},
    SkillStatus.TESTING: {SkillStatus.ASSIMILATED, SkillStatus.FAILED},
    SkillStatus.ASSIMILATED: {SkillStatus.ACTIVE},
    SkillStatus.ACTIVE: {SkillStatus.TESTING},  # re-test after an update
    SkillStatus.FAILED: {SkillStatus.TESTING},
}


class SkillRegistry:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._skills: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def register(
        self, name: str, category: str, description: str = "",
        dependencies: list[str] | None = None, entrypoint: str = "",
    ) -> dict[str, Any]:
        if name in self._skills:
            raise ValueError(f"Skill {name!r} is already registered")
        self._skills[name] = {
            "name": name, "category": category, "description": description,
            "dependencies": list(dependencies or []), "entrypoint": entrypoint,
            "status": SkillStatus.DISCOVERED.value,
            "history": [_event(SkillStatus.DISCOVERED, "registered")],
        }
        self._save()
        log.info("skills.registered", name=name, category=category)
        return self._skills[name]

    def get(self, name: str) -> dict[str, Any]:
        if name not in self._skills:
            raise KeyError(f"Unknown skill {name!r}")
        return self._skills[name]

    def list(self, status: SkillStatus | None = None) -> list[dict[str, Any]]:
        return [s for s in self._skills.values() if status is None or s["status"] == status.value]

    def transition(self, name: str, status: SkillStatus, note: str = "") -> dict[str, Any]:
        skill = self.get(name)
        current = SkillStatus(skill["status"])
        if status not in _TRANSITIONS[current]:
            raise ValueError(f"Illegal transition for {name!r}: {current.value} -> {status.value}")
        skill["status"] = status.value
        skill["history"].append(_event(status, note))
        self._save()
        log.info("skills.transition", name=name, from_=current.value, to=status.value, note=note)
        return skill

    def promote(self, name: str, resolver: DependencyResolver) -> dict[str, Any]:
        """Sandbox-test a skill's dependencies; Assimilate + Activate on pass, Fail otherwise."""
        self.transition(name, SkillStatus.TESTING, "sandbox verification started")
        for dep in self.get(name)["dependencies"]:
            r = resolver.verify_package(dep)
            if not r["ok"]:
                return self.transition(name, SkillStatus.FAILED, f"{dep}: {r['stage']} failed — {r['error']}")
        self.transition(name, SkillStatus.ASSIMILATED, "all dependencies verified in sandbox")
        return self.transition(name, SkillStatus.ACTIVE, "auto-activated")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._skills, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash mid-write never corrupts the registry


def _event(status: SkillStatus, note: str) -> dict[str, str]:
    return {"status": status.value, "at": datetime.now(timezone.utc).isoformat(), "note": note}
