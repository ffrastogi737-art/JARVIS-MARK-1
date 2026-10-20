"""
brain/evolution/hot_reloader.py — In-process module hot-reloading (Phase 6, Master Doc §16).

``reload(names, app)``:
  1. importlib.reload() each named module,
  2. follow stale references: any loaded module (under `package`) still holding a class or
     function from an older version of a reloaded module is reloaded too, until none remain
     (so reloading brain.router.llm_router also refreshes brain.api.routes.chat),
  3. for every reloaded module exposing an APIRouter as ``router`` that `app` already
     serves, swap its routes in place on the live app (same prefix and tags).
The uvicorn process keeps running; requests after the swap hit the new code.

``reload_changed(app)`` does the same for every module whose source file changed on disk.

shortcut: objects created from old classes before the reload (e.g. live instances held by
other objects) keep old behaviour until recreated; restart the process for deep refactors.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI

log = structlog.get_logger(__name__)


class HotReloader:
    def __init__(self, package: str = "brain") -> None:
        self.package = package
        self._mtimes = {name: _mtime(m) for name, m in self._modules().items()}

    def _modules(self) -> dict[str, ModuleType]:
        return {n: m for n, m in list(sys.modules.items())
                if (n == self.package or n.startswith(self.package + ".")) and getattr(m, "__file__", None)}

    def changed(self) -> list[str]:
        return [n for n, m in self._modules().items() if _mtime(m) != self._mtimes.get(n)]

    def reload_changed(self, app: FastAPI | None = None) -> list[str]:
        return self.reload(self.changed(), app)

    def reload(self, names: list[str], app: FastAPI | None = None) -> list[str]:
        importlib.invalidate_caches()
        done: list[str] = []
        queue = list(names)
        while queue:
            name = queue.pop(0)
            if name in done:
                continue
            importlib.reload(sys.modules[name])
            done.append(name)
            self._mtimes[name] = _mtime(sys.modules[name])
            queue += [n for n, m in self._modules().items() if n not in done and n not in queue and _stale(m, self.package)]
        if app is not None:
            for name in done:
                router = getattr(sys.modules[name], "router", None)
                if isinstance(router, APIRouter):
                    _swap_routes(app, name, router)
        log.info("hot_reload.done", modules=done)
        return done


def _mtime(module: ModuleType) -> float | None:
    try:
        return Path(module.__file__).stat().st_mtime_ns
    except (OSError, TypeError):
        return None


def _stale(module: ModuleType, package: str) -> bool:
    """True if `module` binds a top-level class/function of `package` that its defining module no longer exports."""
    for value in vars(module).values():
        owner_name = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", None)
        if isinstance(value, ModuleType) or not isinstance(owner_name, str) or not isinstance(qualname, str) \
                or not (owner_name == package or owner_name.startswith(package + ".")):
            continue
        owner = sys.modules.get(owner_name)
        if owner is not None and owner is not module and "." not in qualname \
                and getattr(owner, qualname, value) is not value:
            return True
    return False


def _swap_routes(app: FastAPI, module_name: str, router: APIRouter) -> None:
    old = [r for r in app.router.routes if getattr(getattr(r, "endpoint", None), "__module__", None) == module_name]
    if not old:
        return  # this router is not mounted on the app
    new_by_name = {r.name: r for r in router.routes}
    ref = next((r for r in old if r.name in new_by_name), old[0])
    prefix = ref.path.removesuffix(new_by_name[ref.name].path) if ref.name in new_by_name else ""
    tags: list[Any] = list(getattr(ref, "tags", []) or [])
    app.router.routes[:] = [r for r in app.router.routes if r not in old]
    app.include_router(router, prefix=prefix, tags=tags)
    app.openapi_schema = None  # regenerate /docs on next request
    log.info("hot_reload.routes_swapped", module=module_name, prefix=prefix, routes=len(router.routes))
