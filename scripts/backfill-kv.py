#!/usr/bin/env python3
"""Backfill Cloudflare KV from the SQLite redirects table.

Run this ONCE on the VPS after the engine has populated `redirects`
but before relying on the public /s/* redirects. Idempotent: it diffs
against the current KV state and only writes on change.

Usage (on the VPS):
    cd /opt/treefrogfree/engine
    CF_API_TOKEN=... CF_ACCOUNT_ID=... CF_KV_NAMESPACE_ID=... \\
        python ../scripts/backfill-kv.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Allow importing the engine package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from engine.publisher.kv import publish_redirects  # noqa: E402


async def main() -> None:
    summary = await publish_redirects(force=True)
    print(f"Backfill complete: {summary}")


if __name__ == "__main__":
    asyncio.run(main())
