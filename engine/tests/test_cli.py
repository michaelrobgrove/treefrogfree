"""Smoke test for the CLI dispatch layer (engine/__main__.py).

Regression test for the bug where `python -m engine` (no subcommand)
crashed with "the following arguments are required: cmd", and
`python -m engine serve` crashed with "asyncio.run() cannot be called
from a running event loop" because _cmd_serve called the sync
scheduler.main() which itself called asyncio.run().

We don't actually run the long-lived scheduler here (it would never
exit). Instead we verify:
  1. The argparse subparser is wired correctly (each known subcommand
     is recognized).
  2. The serve subcommand's handler is an async coroutine function
     that can be awaited without re-entering the event loop.
"""
import asyncio
import sys
import tempfile
import os
from pathlib import Path

# Force a temp DB so the test doesn't touch the real one.
TMPDIR = Path(tempfile.mkdtemp(prefix="treefrog-cli-"))
os.environ["DATA_DIR"] = str(TMPDIR)
os.environ["LOG_DIR"] = str(TMPDIR / "logs")
os.environ["DB_PATH"] = str(TMPDIR / "treefrog.db")
os.environ["CF_API_TOKEN"] = "fake"
os.environ["CF_ACCOUNT_ID"] = "fake"
os.environ["CF_KV_NAMESPACE_ID"] = "fake"
os.environ["ADMIN_TOKEN"] = "test-token"

import importlib
import engine.config
importlib.reload(engine.config)
import engine.db
importlib.reload(engine.db)
import engine.scheduler
importlib.reload(engine.scheduler)
import engine.__main__
importlib.reload(engine.__main__)


def test_subcommands_known():
    """All advertised subcommands are accepted by the parser.

    Note: `seed` requires `--m3u`, so we pass a placeholder. The other
    subcommands take no required args.
    """
    from engine.__main__ import _build_parser
    p = _build_parser()
    cases = [
        (["serve"], "serve"),
        (["seed", "--m3u", "https://example.com/x.m3u"], "seed"),
        (["check-once"], "check-once"),
        (["publish"], "publish"),
        (["migrate"], "migrate"),
        (["stats"], "stats"),
    ]
    for argv, expected in cases:
        ns = p.parse_args(argv)
        assert ns.cmd == expected, f"subcommand {expected!r} parsed as {ns.cmd!r}"


def test_serve_handler_is_coroutine():
    """The serve handler must be awaitable from an async context.

    Before the fix, _cmd_serve called the sync scheduler.main() which
    then called asyncio.run() — illegal from inside an already-running
    loop. The handler is now _cmd_serve which awaits _run_forever
    directly. This test pins that contract.
    """
    import inspect
    from engine.__main__ import _cmd_serve
    assert inspect.iscoroutinefunction(_cmd_serve), (
        "_cmd_serve must be an async coroutine function so the async "
        "dispatch in main() can await it without re-entering the loop"
    )


def test_serve_can_be_entered():
    """Verify the dispatch into _cmd_serve actually works without
    crashing on the asyncio.run() double-call bug.

    We don't let it run to completion (the scheduler loops forever);
    we just create the task and immediately cancel it. If the bug
    were back, this would raise RuntimeError before we even get to
    the cancel.
    """
    from engine.__main__ import _build_parser, _cmd_serve

    async def _probe():
        task = asyncio.create_task(_cmd_serve(_build_parser().parse_args(["serve"])))
        # Give it a moment to start the inner loop
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_probe())  # should not raise RuntimeError
