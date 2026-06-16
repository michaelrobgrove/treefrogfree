"""aiohttp admin + public API server.

Two surface areas on one port:

  Public (no auth — Worker proxies these):
    GET  /api/channels.json          → public channel catalog
    GET  /playlist.m3u               → public M3U
    GET  /api/stats                  → home page stats

  Admin (Bearer token):
    GET  /api/admin/stats            → engine stats
    GET  /api/admin/dead-streams     → streams offline > 1 cycle
    GET  /api/admin/channels         → full channel list
    POST /api/admin/import           → {url, label} → import
    POST /api/admin/check-once       → run a single health cycle
    POST /api/admin/publish          → re-render playlist + catalog
    POST /api/admin/rebuild-kv       → force-republish all KV
    POST /api/admin/prune[?dry_run=1] → drop dead playlists
    POST /api/admin/streams/{id}/recheck
    POST /api/admin/streams/{id}/disable
    POST /api/admin/streams/{id}/enable

  EPG:
    GET  /api/epg.xml                → XMLTV
    GET  /api/epg.xml.gz             → gzipped XMLTV
    POST /api/admin/epg/import       → {url} → fetch + map

The server binds to ADMIN_HOST:ADMIN_PORT. Behind Tailscale or a
Cloudflare Tunnel, never the public internet. See plan.md §11.
"""
from __future__ import annotations

import asyncio
import functools
import gzip
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from ..config import CONFIG
from ..db import open_db, run_migrations
from ..health import run_health_cycle
from ..importers.importer import import_m3u
from ..pruner import prune_dead_playlists
from ..publisher.json_catalog import build_catalog, write_catalog
from ..publisher.kv import publish_public_assets, publish_redirects
from ..publisher.playlist import render_playlist, write_playlist
from ..publisher.streams_kv import publish_stream_lists
from .epg import import_epg_url, render_epg_xml

log = logging.getLogger("treefrog.api")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _check_admin(request: web.Request) -> bool:
    """Bearer-token check for admin endpoints.

    Uses hmac.compare_digest to avoid timing attacks. If ADMIN_TOKEN is
    unset, refuse all admin traffic (fail closed).
    """
    if not CONFIG.admin_token or CONFIG.admin_token == "change-me":
        log.error("ADMIN_TOKEN is unset or default; refusing admin request")
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer "):]
    return secrets.compare_digest(provided, CONFIG.admin_token)


@web.middleware
async def admin_auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if request.path.startswith("/api/admin/") or request.path.startswith("/api/epg/import"):
        if not _check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


async def handle_channels_json(request: web.Request) -> web.Response:
    catalog = await build_catalog()
    return web.json_response(catalog)


async def handle_playlist(request: web.Request) -> web.Response:
    body = await render_playlist()
    return web.Response(
        body=body,
        content_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": 'inline; filename="treefrog.m3u"',
            "Cache-Control": f"public, max-age=300",
        },
    )


async def handle_stats(request: web.Request) -> web.Response:
    catalog = await build_catalog()
    return web.json_response(catalog["stats"])


async def handle_root(_request: web.Request) -> web.Response:
    """Friendly landing page so / doesn't 404 when someone types the
    engine's Tailscale IP into a browser. The admin UI lives at
    https://free.tfplus.stream/admin/ (served by the Cloudflare Worker
    static assets); the engine itself is a JSON API only."""
    return web.json_response(
        {
            "service": "treefrog-engine",
            "version": "1.0",
            "note": "JSON API only. Browse the public catalog at "
                    "https://free.tfplus.stream/. Admin UI at "
                    "https://free.tfplus.stream/admin/.",
            "endpoints": {
                "public": [
                    "/api/channels.json",
                    "/api/stats",
                    "/playlist.m3u",
                    "/api/epg.xml",
                    "/api/epg.xml.gz",
                    "/healthz",
                ],
                "admin": [
                    "GET  /api/admin/stats",
                    "GET  /api/admin/dead-streams",
                    "GET  /api/admin/channels",
                    "POST /api/admin/import",
                    "POST /api/admin/check-once",
                    "POST /api/admin/publish",
                    "POST /api/admin/rebuild-kv",
                    "POST /api/admin/prune[?dry_run=1]",
                ],
            },
        }
    )


# Path to the admin UI's static assets inside the container. Bound in
# via docker-compose.yml from ../edge/public/admin (relative to the
# engine/ directory). The admin UI talks to the engine at the same
# origin (no CORS, no mixed-content). See handle_admin_ui().
_ADMIN_STATIC_DIR = Path("/app/admin_static")


async def handle_epg_xml(request: web.Request) -> web.Response:
    xml = await render_epg_xml()
    if not xml:
        return web.Response(status=503, text="EPG not yet imported")
    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" in accept_encoding:
        body = gzip.compress(xml.encode("utf-8"))
        return web.Response(
            body=body,
            content_type="application/gzip",
            headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=3600"},
        )
    return web.Response(
        body=xml, content_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def handle_epg_xml_gz(request: web.Request) -> web.Response:
    xml = await render_epg_xml()
    if not xml:
        return web.Response(status=503, text="EPG not yet imported")
    body = gzip.compress(xml.encode("utf-8"))
    return web.Response(
        body=body, content_type="application/gzip",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------


async def handle_admin_stats(request: web.Request) -> web.Response:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM channels WHERE status='online')   AS online_channels,
                (SELECT COUNT(*) FROM channels WHERE status='offline')  AS offline_channels,
                (SELECT COUNT(*) FROM streams  WHERE status='online')   AS online_streams,
                (SELECT COUNT(*) FROM streams  WHERE status='offline')  AS offline_streams,
                (SELECT COUNT(*) FROM streams  WHERE status='disabled') AS disabled_streams,
                (SELECT COUNT(*) FROM imports) AS import_count,
                (SELECT MAX(last_checked_at) FROM channels) AS last_check
            """
        ) as cur:
            row = await cur.fetchone()
        return web.json_response(dict(row))
    finally:
        await db.close()


async def handle_dead_streams(request: web.Request) -> web.Response:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT s.id AS stream_id, s.source_url, s.offline_since, s.last_error,
                   s.last_checked_at, c.id AS channel_id, c.display_name
            FROM streams s
            JOIN channels c ON c.id = s.channel_id
            WHERE s.status = 'offline'
            ORDER BY s.offline_since DESC NULLS LAST
            LIMIT 200
            """
        ) as cur:
            rows = await cur.fetchall()
        return web.json_response([dict(r) for r in rows])
    finally:
        await db.close()


async def handle_admin_channels(request: web.Request) -> web.Response:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT id, display_name, group_title, bouquet, status, availability_pct, logo_url
            FROM channels
            ORDER BY group_title, display_name
            LIMIT 5000
            """
        ) as cur:
            rows = await cur.fetchall()
        return web.json_response([dict(r) for r in rows])
    finally:
        await db.close()


async def handle_admin_import(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"error": f"invalid JSON body: {e}"}, status=400)
    url = body.get("url")
    label = body.get("label")
    if not url:
        return web.json_response({"error": "url required"}, status=400)
    try:
        summary = await import_m3u(url, source_label=label)
    except Exception as e:
        # Return a clean JSON error so the admin UI can display it,
        # rather than aiohttp's default 500 HTML page.
        log.exception("M3U import failed for url=%s", url)
        return web.json_response(
            {"error": f"import failed: {type(e).__name__}: {e}", "url": url},
            status=502,
        )
    # Republish artifacts so the change is visible immediately, both
    # on-disk and in Cloudflare KV (which the public Worker reads).
    # `write_playlist` mints redirect tokens for any new online channels
    # by populating the `redirects` table; `publish_redirects` then
    # pushes those tokens (with their stream URLs) to KV so /s/<token>
    # 302s resolve. `publish_public_assets` pushes the catalog/playlist
    # snapshot.
    await write_playlist()
    await write_catalog()
    redir = await publish_redirects(force=True)
    pub = await publish_public_assets(force=True)
    log.info("import: KV redirects=%s, public=%s", redir, pub)
    return web.json_response(summary)


async def handle_admin_check_once(_request: web.Request) -> web.Response:
    summary = await run_health_cycle()
    await write_playlist()
    await write_catalog()
    # `write_playlist` mints redirect tokens for any channels that just
    # came online; `publish_redirects` pushes them to KV so /s/<token>
    # lookups on the public site 302 correctly.
    redir = await publish_redirects(force=True)
    pub = await publish_public_assets(force=True)
    # Per-channel stream lists for the HLS player. Best-effort — if
    # the module is mid-deploy, the player will 404 and fall back to
    # the existing /s/<token> 302 redirect.
    try:
        sl = await publish_stream_lists(force=True)
    except Exception as e:
        log.warning("stream lists publish failed in check-once: %s", e)
        sl = {"error": str(e)}
    try:
        from ..publisher.epg_kv import publish_nownext
        nn = await publish_nownext(force=True)
    except Exception as e:
        log.warning("epg now/next publish failed in check-once: %s", e)
        nn = {"error": str(e)}
    log.info("check-once: KV redirects=%s, public=%s, streams=%s, epg=%s",
             redir, pub, sl, nn)
    return web.json_response(summary)


async def handle_admin_publish(_request: web.Request) -> web.Response:
    p = await write_playlist()
    c = await write_catalog()
    # Re-rendering the playlist may have minted new redirect tokens for
    # channels whose winner just changed; push them too.
    redir = await publish_redirects(force=True)
    pub = await publish_public_assets(force=True)
    try:
        sl = await publish_stream_lists(force=True)
    except Exception as e:
        log.warning("stream lists publish failed in publish: %s", e)
        sl = {"error": str(e)}
    try:
        from ..publisher.epg_kv import publish_nownext
        nn = await publish_nownext(force=True)
    except Exception as e:
        log.warning("epg now/next publish failed in publish: %s", e)
        nn = {"error": str(e)}
    return web.json_response({
        "playlist": p,
        "catalog": c,
        "kv_redirects": redir,
        "kv_public": pub,
        "kv_stream_lists": sl,
        "kv_epg_nownext": nn,
    })


async def handle_admin_rebuild_kv(_request: web.Request) -> web.Response:
    redirects = await publish_redirects(force=True)
    public = await publish_public_assets(force=True)
    try:
        sl = await publish_stream_lists(force=True)
    except Exception as e:
        sl = {"error": str(e)}
    try:
        from ..publisher.epg_kv import publish_nownext
        nn = await publish_nownext(force=True)
    except Exception as e:
        nn = {"error": str(e)}
    return web.json_response({
        "redirects": redirects,
        "public": public,
        "stream_lists": sl,
        "epg_nownext": nn,
    })


async def handle_admin_prune(request: web.Request) -> web.Response:
    """Drop any source_label whose streams are all offline.

    Query params:
      dry_run=1  → report the kill list without writing.

    The scheduler also runs this at the end of every tick; this
    endpoint is for an operator-driven sweep (e.g. after a
    bulk import goes sideways and the operator wants to clean
    up without waiting 30 minutes for the next tick).
    """
    dry_run = request.query.get("dry_run") in ("1", "true", "yes")
    db = await open_db()
    try:
        summary = await prune_dead_playlists(db, dry_run=dry_run)
    finally:
        await db.close()
    # If anything was actually deleted, republish the public catalog
    # + KV so the change is visible immediately.
    if not dry_run and summary["dead_labels"] > 0:
        try:
            await write_playlist()
            await write_catalog()
            await publish_redirects(force=True)
            await publish_public_assets(force=True)
        except Exception as e:
            log.warning("prune: post-prune republish failed: %s", e)
    return web.json_response(summary)


async def handle_stream_recheck(request: web.Request) -> web.Response:
    stream_id = int(request.match_info["id"])
    db = await open_db()
    try:
        # Force the next health cycle to consider this stream: clear
        # last_checked_at, then run a cycle. Simpler than a per-stream
        # inline check (which would race with the scheduler's parallelism).
        await db.execute(
            "UPDATE streams SET last_checked_at = NULL WHERE id = ?", (stream_id,)
        )
        await db.commit()
    finally:
        await db.close()
    return web.json_response({"queued": True, "stream_id": stream_id})


async def handle_stream_disable(request: web.Request) -> web.Response:
    stream_id = int(request.match_info["id"])
    db = await open_db()
    try:
        await db.execute(
            "UPDATE streams SET status = 'disabled' WHERE id = ?", (stream_id,)
        )
        await db.commit()
    finally:
        await db.close()
    return web.json_response({"disabled": stream_id})


async def handle_stream_enable(request: web.Request) -> web.Response:
    stream_id = int(request.match_info["id"])
    db = await open_db()
    try:
        await db.execute(
            """
            UPDATE streams
            SET status = 'unknown', offline_since = NULL, last_checked_at = NULL
            WHERE id = ?
            """,
            (stream_id,),
        )
        await db.commit()
    finally:
        await db.close()
    return web.json_response({"enabled": stream_id})


async def handle_epg_import(request: web.Request) -> web.Response:
    body = await request.json()
    url = body.get("url")
    if not url:
        return web.json_response({"error": "url required"}, status=400)
    summary = await import_epg_url(url)
    return web.json_response(summary)


async def handle_admin_ui(_request: web.Request) -> web.Response:
    """Serve the admin UI's index.html. Bound in via docker-compose
    from ../edge/public/admin (see docker-compose.yml). Serving the UI
    from the engine itself (over Tailscale) means the UI's fetch()
    calls go to the same origin — no CORS, no mixed-content, no
    proxy. Visiting http://<engine>:8000/admin/ is the canonical URL
    for the admin dashboard.

    The admin API requires a Bearer token, so we inject the engine's
    configured ADMIN_TOKEN into the page via a <meta> tag. The UI
    reads it and sends it on every /api/admin/* call. The token is
    the same one the operator uses for curl; this is a single-user
    admin surface, not a multi-tenant login.
    """
    index = _ADMIN_STATIC_DIR / "index.html"
    if not index.is_file():
        return web.Response(
            status=503,
            text=(
                "Admin UI assets are not mounted into the container. "
                "On the VPS, fix this with:\n"
                "  cd /opt/treefrogfree && git pull\n"
                "  docker compose -f engine/docker-compose.yml up -d --build\n"
                "Note: `docker compose restart` does NOT re-bind volumes — "
                "you must `up -d --build` (or stop+rm+up).\n"
                "The bind mount is `../edge/public/admin -> /app/admin_static` "
                "in engine/docker-compose.yml."
            ),
        )
    body = index.read_text(encoding="utf-8")
    # Inject the token as a <meta> tag just before </head> so the UI
    # can read it via document.querySelector('meta[name="admin-token"]').
    # The Cache-Control: no-store below ensures the injected token
    # never gets cached at any layer — a new engine start with a new
    # token must take effect on the next request.
    meta = f'<meta name="admin-token" content="{CONFIG.admin_token}">'
    body = body.replace("</head>", f"  {meta}\n  </head>", 1)
    return web.Response(
        body=body,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# App factory + runner
# ---------------------------------------------------------------------------


def build_app() -> web.Application:
    app = web.Application(middlewares=[admin_auth_middleware])
    # Public
    app.router.add_get("/", handle_root)
    app.router.add_get("/admin", handle_admin_ui)
    app.router.add_get("/admin/", handle_admin_ui)
    app.router.add_get("/api/channels.json", handle_channels_json)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/playlist.m3u", handle_playlist)
    app.router.add_get("/api/epg.xml", handle_epg_xml)
    app.router.add_get("/api/epg.xml.gz", handle_epg_xml_gz)
    # Admin
    app.router.add_get("/api/admin/stats", handle_admin_stats)
    app.router.add_get("/api/admin/dead-streams", handle_dead_streams)
    app.router.add_get("/api/admin/channels", handle_admin_channels)
    app.router.add_post("/api/admin/import", handle_admin_import)
    app.router.add_post("/api/admin/check-once", handle_admin_check_once)
    app.router.add_post("/api/admin/publish", handle_admin_publish)
    app.router.add_post("/api/admin/rebuild-kv", handle_admin_rebuild_kv)
    app.router.add_post("/api/admin/prune", handle_admin_prune)
    app.router.add_post("/api/admin/streams/{id}/recheck", handle_stream_recheck)
    app.router.add_post("/api/admin/streams/{id}/disable", handle_stream_disable)
    app.router.add_post("/api/admin/streams/{id}/enable", handle_stream_enable)
    app.router.add_post("/api/admin/epg/import", handle_epg_import)
    # Health
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
    return app


async def _run_server() -> None:
    # Run migrations on startup so a fresh VPS container is ready to go.
    db = await open_db()
    await run_migrations(db)
    await db.close()

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG.admin_host, CONFIG.admin_port)
    await site.start()
    log.info("API server listening on http://%s:%d", CONFIG.admin_host, CONFIG.admin_port)
    # Block forever (the caller manages the event loop).
    while True:
        await asyncio.sleep(3600)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
