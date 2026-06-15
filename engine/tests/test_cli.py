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


def test_env_file_fallback_on_missing_file(caplog):
    """Regression: a missing *_FILE path used to crash the engine on
    startup. It should now log a warning and fall through to the
    inline value (or empty, if not set).
    """
    import logging
    os.environ["MISSING_FILE"] = "/no/such/path/anywhere"
    os.environ["MISSING"] = "inline-value"
    try:
        with caplog.at_level(logging.WARNING, logger="treefrog.config"):
            got = engine.config._env("MISSING")
        assert got == "inline-value", f"expected fallback to inline, got {got!r}"
        # Confirm the warning was actually logged so the operator sees
        # the misconfiguration rather than a silent degradation.
        assert any("MISSING_FILE" in r.message for r in caplog.records), (
            "expected a warning about MISSING_FILE"
        )
    finally:
        os.environ.pop("MISSING_FILE", None)
        os.environ.pop("MISSING", None)


def test_env_file_existing_file_still_works():
    """Positive case: when the *_FILE path actually points at a real
    file, its content is returned (not the inline value).
    """
    f = Path(tempfile.mkdtemp()) / "secret.txt"
    f.write_text("file-content\n")
    os.environ["PRESENT_FILE"] = str(f)
    os.environ["PRESENT"] = "should-be-ignored"
    try:
        got = engine.config._env("PRESENT")
        assert got == "file-content"
    finally:
        os.environ.pop("PRESENT_FILE", None)
        os.environ.pop("PRESENT", None)
