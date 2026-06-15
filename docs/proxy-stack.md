# Tree Frog Streams — Optional proxy stack

Three free, ad-supported IPTV source proxies that can sit alongside
the main `tf-engine` to add ~700 channels (Samsung TV Plus +
DistroTV + Cabernet's plugin bundle, which includes Pluto/Xumo/Plex
through one admin UI) to the catalog. **Optional** — the engine
works without them — but they're the easiest way to bulk-up the
lineup without paying for an M3U provider.

## What's in the box

| Container | Image | Port | ~Channels | Notes |
|---|---|---|---|---|
| `tf-distrotv-proxy` | `ghcr.io/kineticman/distrotv-proxy:latest` | **8787** | 150 | Pure pass-through, no auth, no DB. |
| `tf-samsung-tvplus` | `matthuisman/samsung-tvplus-for-channels:latest` | **3001** | 400 | Wraps Samsung TV Plus. Widevine DRM streams filtered out — HLS only. |
| `tf-cabernet` | `ghcr.io/cabernetwork/cabernet:latest` | **6077** | 300+ (via plugins) | Plugin-based "all-in-one" — subsumes the standalone Pluto container (see §3). |

All three bind to the host with `network_mode: host` and the
`engine/docker-compose.proxies.yml` file caps them at a combined
~1.15 GB RAM / ~0.85 vCPU so they coexist with `tf-engine` and
AIOstreams.

### ⚠️ Host port allocation — read before binding anything

The proxies use host networking, so their container ports land
directly on the host. **Port collisions will crash the second
container with `OSError: [Errno 98] Address already in use`.** The
table below is the live state of one operator's host; if your
host differs, edit the `PORT` env var in
`engine/docker-compose.proxies.yml` to match a free port.

| Host port | What's on it | Action |
|---|---|---|
| 80 | (reserved — see §3 below) | Available for a future matthuisman image if you want one. Currently free. |
| 3000 | **AIOstreams — operator's most important service** | **RESERVED. Never bind a container here, never curl 127.0.0.1:3000 to test a proxy.** |
| 3001 | Samsung TV Plus (this file) | Pinned via `PORT=3001` in the compose. |
| 6077 | Cabernet (this file) | Cab's default. |
| 8000 | Tree Frog engine (main compose) | The admin API and the engine-side endpoints. |
| 8787 | DistroTV (this file) | The image's only port. |
| 50005 | SSH (already on the host) | Non-standard SSH port. Never bind here. |

The original proxy stack in commit `090d2f5` had Samsung defaulting
to port 80 and Pluto defaulting to port 8080. The 80 collision
made Samsung crash-loop silently, and a diagnostic curl against
`127.0.0.1:3000` briefly targeted AIOstreams by mistake. The
fixed stack pins Samsung to 3001 and drops the standalone Pluto
container (Cab's Pluto plugin covers the same source — see §3).

## 1. DistroTV — bring it up first

DistroTV is the simplest of the three. It has no auth, no DB, and
no admin UI — just a Python BaseHTTP listener. Good for verifying
the host-network + secret-mount + resource-cap wiring before
adding the more complex proxies.

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d distrotv-proxy
# (Note the relative path: this file lives in engine/, so from
# /opt/treefrogfree the path is engine/docker-compose.proxies.yml.
# If you're already cd'd into engine/, drop the "engine/" prefix.)

# Verify it serves M3U. Use a real GET (the image only implements
# GET, so curl -I returns 501 which is a method-not-allowed error,
# not a problem with the M3U).
curl -s -o /tmp/distrotv.m3u -w 'HTTP %{http_code}, %{size_download}B, type=%{content_type}\n' \
  http://127.0.0.1:8787/playlist.m3u
head -3 /tmp/distrotv.m3u
# Expect: HTTP 200, ~14 KB, type=audio/x-mpegurl, body starts with
# "#EXTM3U".

# Import it. The engine's `seed` is idempotent — re-running it
# merges with whatever's already in the DB rather than duplicating.
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:8787/playlist.m3u \
  --label "DistroTV"
# DistroTV doesn't publish XMLTV, so no epg-import call here.
```

The expected result on the engine side:

```
INFO treefrog.importer: Import done: 73 entries, 0 new channels, 73 new streams, 0 duplicates
INFO treefrog.playlist: Wrote 3429-line playlist
INFO treefrog.catalog: Wrote catalog: 1714 channels, 29 categories
```

The 0 new channels is correct: all 73 DistroTV streams were
deduplicated against your existing 1714 by the
`canonical_channel_name()` consolidator. The streams were added
as failover URLs to the existing channel rows.

## 2. Samsung TV Plus — port 3001

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d samsung-tvplus
docker compose -f engine/docker-compose.proxies.yml logs --tail=20 samsung-tvplus
# Look for "Starting server on port 3001" — if you see 80
# instead, the env var didn't take. Check
# `docker inspect tf-samsung-tvplus | grep -A2 PORT`.

# Verify with GET (not HEAD — the BaseHTTP backend only handles GET):
curl -s -o /tmp/samsung.m3u -w 'HTTP %{http_code}, %{size_download}B, type=%{content_type}\n' \
  http://127.0.0.1:3001/tuner-1-playlist.m3u
head -3 /tmp/samsung.m3u
wc -l /tmp/samsung.m3u
# Expect: HTTP 200, several hundred KB, type=audio/x-mpegurl,
# body starts with "#EXTM3U", ~400-500 lines.

# Import the full channel list (tuner-1 has them all):
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:3001/tuner-1-playlist.m3u \
  --label "Samsung TV Plus"

# EPG is at /epg.xml (singular, not /xmltv.xml).
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:3001/epg.xml
```

Don't bother importing tuners 2-12 — they're the same channels
split into 12 buckets. Tuner 1 is the full lineup.

## 3. Why no standalone Pluto container

The original stack included `jonmaddox/pluto-for-channels:latest`
on port 80. It worked, but two issues made it not worth the extra
container:

1. **The image doesn't dereference `${VAR}_FILE` paths.** Its
   entrypoint script reads `PLUTO_USERNAME` / `PLUTO_PASSWORD`
   directly, so the secure-credential pattern from
   `docker-compose.yml` (the main engine) doesn't apply. To run
   it, you'd have to inline the password in the compose file —
   exactly the leak path we worked to avoid with the
   `${VAR}_FILE` convention.

2. **Cab's Pluto plugin covers the same source.** Cabernet ships
   a "Pluto" plugin under Settings → Plugins that pulls from the
   same Pluto API, but the credential lifecycle is handled
   inside Cab's admin UI (one place, not two), and the channels
   are deduped against every other plugin Cab has enabled
   (Xumo, Plex, Stirr, Local Now, …). The result is a single
   M3U that contains the same Pluto lineup, with no second
   container to maintain.

If you later decide you want the standalone Pluto back, the
`docker-compose.proxies.yml` history has the block in commit
`090d2f5`. To re-add it: set `MODE=docker` plus the inline
`PLUTO_USERNAME` / `PLUTO_PASSWORD` env vars (NOT `_FILE` —
this image won't read them), give it port 80 (Samsung is
relocated to 3001), and import from `http://127.0.0.1:80/tuner-1-playlist.m3u`.

## 4. Cabernet — admin UI first, then import

Cab is the most useful of the three because of its plugin
ecosystem, but it requires the most setup. The admin UI must be
opened in a browser once on first boot to set the admin password
and pick which plugins to enable.

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d cabernet
docker compose -f engine/docker-compose.proxies.yml logs --tail=20 cabernet
# Wait for the line that says the admin UI is up. Cab takes 30-60s
# on first boot to load its plugin bundle.

# Open the admin UI from any browser. From your local machine,
# the easiest path is an SSH port-forward:
#   ssh -L 6077:127.0.0.1:6077 aio
# Then visit http://127.0.0.1:6077/ in your browser.
#
# Steps in the admin UI:
#   1. Set the admin password (Cab defaults to admin/admin — change
#      it on first login).
#   2. Go to Settings → Plugins.
#   3. Enable the plugins you want: Pluto, Xumo, Plex, Stirr, Local
#      Now, etc. (Pluto requires a free Pluto account; the others
#      are anonymous.)
#   4. Click "Save" — Cab will start pulling channel lists and
#      EPG for the enabled plugins. First sync takes 1-2 minutes.

# Verify the M3U is real now (not the 8-byte stub):
sleep 90    # give plugins time to sync
curl -s -o /tmp/cab.m3u -w 'HTTP %{http_code}, %{size_download}B, type=%{content_type}\n' \
  http://127.0.0.1:6077/m3u/channels.m3u
head -3 /tmp/cab.m3u
wc -l /tmp/cab.m3u
# Expect: HTTP 200, several hundred KB, type=audio/x-mpegurl,
# body starts with "#EXTM3U", ~300-1500 lines depending on how
# many plugins you enabled.

# Import:
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:6077/m3u/channels.m3u \
  --label "Cabernet"

# Cab publishes a consolidated EPG. The XMLTV path in Cab's
# admin UI is shown under "XMLTV URL" — typically /xmltv.xml:
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:6077/xmltv.xml
```

## 5. Auto-refresh

The proxies refresh their own M3U/EPG every 15-60 minutes (Cab is
the most aggressive). The engine needs a `check-once` to pick up
those new URLs. Two options:

**a) Manual.** Run `python -m engine check-once` from the admin UI
("Run health check" button) once after each proxy refresh.

**b) Scheduled.** Add a 30-minute cron entry on the VPS host that
triggers the engine's health cycle:

```bash
# /etc/cron.d/treefrog-proxy-refresh
*/30 * * * * root cd /opt/treefrogfree/engine && \
  docker compose exec -T tf-engine python -m engine check-once >/dev/null 2>&1
```

(You already have a 30-minute `HEALTH_CADENCE_SEC` in the engine
itself; this cron is for cases where you want a tighter cycle
than the in-process scheduler, or to force a re-evaluation after
a proxy-side refresh.)

## 6. Resource budget

Combined with the main engine and AIOstreams on a 4-vCPU / 4 GB
VPS, the full "with proxies" stack looks like this:

| Service | CPU cap | RAM cap |
|---|---|---|
| `tf-engine` | 0.5 | 1 GB |
| `tf-cabernet` | 0.5 | 768 MB |
| `tf-samsung-tvplus` | 0.25 | 256 MB |
| `tf-distrotv-proxy` | 0.10 | 128 MB |
| **Subtotal** | **1.35** | **2.16 GB** |
| AIOstreams (already running) | 1.0 | 1.0 GB |
| **Total worst-case** | **2.35** | **3.16 GB** |

That fits a 4-vCPU / 4 GB VPS with headroom. If AIOstreams is
heavier, drop the engine cap to `cpus: 0.25` and the proxies by
25% — the engine only does a health round every 30 minutes, not
sustained work.

## 7. Tearing it back down

```bash
cd /opt/treefrogfree/engine
docker compose -f docker-compose.proxies.yml down            # stop + remove

# Optional: also strip the channels the proxies added from the
# engine DB. This only touches rows where the source_label
# matches one of the proxies' labels.
docker compose exec tf-engine sqlite3 /app/data/treefrog.db \
  "DELETE FROM streams WHERE source_label IN
     ('Samsung TV Plus', 'DistroTV', 'Cabernet');"
docker compose exec tf-engine python -m engine publish
```

The distrotv-proxy and samsung-tvplus services do not bind a port
other than their assigned one, so removing them frees the port
back to the host immediately. The bind mounts in `./data/` are
preserved — re-running `up -d` later will resume from where the
proxy left off (cached channel lists, EPG, plugin state).

## 8. Why not import the proxies' M3U on a schedule instead?

The engine's `seed` command is idempotent: re-running it against
the same URL is a no-op for unchanged channels. The proxies' M3U
changes whenever their source adds or removes a channel (a
couple of times per month for the stable ones, more often for
Cab as its plugin bundle updates). For most operators, the
manual `check-once` flow on the admin UI is fine — the next
health cycle will pick up any new streams and demote any that
went away.

If you want a fully hands-off path, the 30-minute cron in §5 is
the simplest answer. A longer-term enhancement would be a
"subscribe to URL" admin endpoint that adds the URL to the next
health cycle automatically, but that's a v2 feature.
