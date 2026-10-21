"""
cli.py — Launch the J.A.R.V.I.S. terminal Control Center (Phase 9).

    python cli.py [--url https://jarvis-brain.onrender.com] [--token ...]

Defaults come from JARVIS_CLOUD_URL and TETHER_SECRET_TOKEN. Keys: Ctrl+T HALT,
Ctrl+R voice, Enter send, Ctrl+Q quit.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from cui.app import CUI


def main() -> None:
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. terminal Control Center")
    parser.add_argument("--url", default=os.environ.get("JARVIS_CLOUD_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("TETHER_SECRET_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        parser.error("a tether token is required (--token or TETHER_SECRET_TOKEN)")
    asyncio.run(CUI(args.url, args.token).run())


if __name__ == "__main__":
    main()
