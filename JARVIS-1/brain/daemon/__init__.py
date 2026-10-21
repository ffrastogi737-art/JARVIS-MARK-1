"""brain/daemon/__init__.py — Phase 7 Edge Agent (the only JARVIS code that runs on the laptop)."""

import sys

# structlog pretty-prints tracebacks with `rich` whenever it is installed (it is, for the
# terminal CUI): ~4 MB the daemon never uses. Runs before any daemon module imports structlog.
sys.modules.setdefault("rich", None)
