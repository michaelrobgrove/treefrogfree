# Tree Frog Streams — Optional proxy stack

Four free, ad-supported IPTV source proxies that can sit alongside
the main `tf-engine` to add ~500 channels (Samsung TV Plus + Pluto
TV + DistroTV + Cabernet's plugin bundle) to the catalog. They are
**strictly optional** — the engine works without them — but they're
the easiest way to bulk-up the lineup without paying for an M3U
provider.

## What's in the box

| Container | Image | Port | ~Channels | Notes |
|---|---|---|---|---|
| `tf-samsung-tvplus` | `matthuisman/samsung-tvplus-for-channels:latest` | 80 | 400 | Wraps Samsung TV Plus. Widevine DRM streams filtered out — HLS only. |
| `tf-pluto-for-channels` | `jonmaddox/pluto-for-channels:latest` | 8080 | 250 | Pluto TV, regional unlocks with a free account. |
| `tf-distrotv-proxy` | `ghcr.io/kineticman/distrotv-proxy:latest` | 8787 | 150 | Pure pass-through; no auth. |
| `tf-cabernet` | `ghcr.io/cabernetwork/cabernet:latest` | 6077 | 300+ (via plugins) | Plugin-based "all-in-one" — Pluto/Xumo/Plex/Stirr/Local Now/custom M3U. |

All four bind to the host with `network_mode: host` and the
`engine/docker-compose.proxies.yml` file caps them at a combined
~1.1 GB RAM / ~1.1 vCPU so they coexist with `tf-engine` and
AIOstreams.

## 1. Bring it up

```bash
cd /opt/treefrogfree
git pull                                       # pick up the new compose file
docker compose -f engine/docker-compose.proxies.yml up -d
docker compose -f engine/docker-compose.proxies.yml ps
```

You should see four containers in `running` state within ~10 seconds.
First-time boot for Cabernet and Samsung TV Plus takes 30-60 s — they
fetch plugin/source lists on cold start.

### Verifying each proxy responds

```bash
# 1. Samsung TV Plus — Channels DVR-style M3U
curl -sI http://127.0.0.1:80/devices/ANY/channels.m3u

# 2. Pluto for Channels
curl -sI http://127.0.0.1:8080/m3u/channels.m3u

# 3. DistroTV proxy
curl -sI http://127.0.0.1:8787/playlist.m3u

# 4. Cabernet — its M3U path is plugin-dependent, but a health probe
#    is always at the root.
curl -sI http://127.0.0.1:6077/
```

All four should return `200 OK`. If any returns `404` or `000`, check
the container log: `docker compose -f engine/docker-compose.proxies.yml
logs -f <name>`.

## 2. Wire them into the engine

The proxies expose M3U + XMLTV HTTP endpoints. The engine already
imports M3U from URLs (see `python -m engine seed --m3u <url>
--label ...`) and XMLTV separately (see `python -m engine epg-import
--url <url>`). Each proxy becomes one `seed` call, one `epg-import`
call, one `check-once` to populate the health-check rows, and one
`publish` to push to Cloudflare KV.

```bash
cd /opt/treefrogfree/engine

# 1. Samsung TV Plus — M3U + EPG
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:80/devices/ANY/channels.m3u \
  --label "Samsung TV Plus"
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:80/devices/ANY/xmltv.xml

# 2. Pluto TV — M3U + EPG
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:8080/m3u/channels.m3u \
  --label "Pluto TV"
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:8080/xmltv.xml

# 3. DistroTV — M3U + EPG
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:8787/playlist.m3u \
  --label "DistroTV"
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:8787/xmltv.xml

# 4. Cabernet — M3U + EPG. Cab's plugin bundle publishes a
#    consolidated EPG; its M3U path is set in the Cab admin UI
#    (default http://127.0.0.1:6077/m3u/channels.m3u).
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:6077/m3u/channels.m3u \
  --label "Cabernet"
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:6077/xmltv.xml

# Kick a health cycle so the engine can decide which streams
# actually work right now.
docker compose exec tf-engine python -m engine check-once

# Republish KV (catalog, playlist, redirects, stream lists, EPG
# now/next).
docker compose exec tf-engine python -m engine publish
```

Within ~2 minutes the public site at `https://free.tfplus.stream/`
will show the new channels, deduplicated against each other and
against any M3U providers you already had configured (PBS Kids
Alaska + Pluto's "PBS Kids" feed become one channel with two
stream URLs for failover — see `engine/consolidator.py:
canonical_channel_name`).

## 3. Auto-refresh

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
itself; this cron is for cases where you want a tighter cycle than
the in-process scheduler, or to force a re-evaluation after a
proxy-side refresh.)

## 4. Resource budget

Combined with the main engine on a 4-vCPU / 4 GB VPS, the full
"with proxies" stack looks like this:

| Service | CPU cap | RAM cap |
|---|---|---|
| `tf-engine` | 0.5 | 1 GB |
| `tf-cabernet` | 0.5 | 768 MB |
| `tf-samsung-tvplus` | 0.25 | 256 MB |
| `tf-pluto-for-channels` | 0.25 | 256 MB |
| `tf-distrotv-proxy` | 0.10 | 128 MB |
| **Subtotal** | **1.60** | **2.42 GB** |
| AIOstreams (already running) | 1.0 | 1.0 GB |
| **Total worst-case** | **2.60** | **3.42 GB** |

That fits a 4-vCPU / 4 GB VPS with headroom. If your AIOstreams is
heavier (e.g. multiple bouquets), drop the engine cap to `cpus:
0.25` and the proxies by 25% — the engine only does a health
round every 30 minutes, not sustained work.

## 5. Optional: Pluto regional unlocks

Pluto's free tier gives US-only. If you create a free Pluto account
(you don't even need a TV — the web signup is fine), put the
credentials in a `.token` file the same way the CF and admin tokens
are stored:

```bash
sudo mkdir -p /root/.secrets
echo -n 'your-pluto-email' | sudo tee /root/.secrets/pluto_user >/dev/null
echo -n "$(openssl rand -hex 16)" | sudo tee /root/.secrets/pluto_password >/dev/null
sudo chmod 600 /root/.secrets/pluto_*
```

Then add the two env vars to `engine/docker-compose.proxies.yml`
under the `pluto-for-channels` service (uncomment the
`*_FILE` lines in the template), restart, and re-import:

```bash
docker compose -f engine/docker-compose.proxies.yml up -d pluto-for-channels
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:8080/m3u/channels.m3u \
  --xmltv http://127.0.0.1:8080/xmltv.xml \
  --label "Pluto TV (regional)"
```

The `stale` flag in the consolidator will refresh the channels,
the `seed` command will reuse the existing channel rows, and the
new regional streams will land in the per-channel failover list.

## 6. Tearing it back down

If you decide you don't want the proxies:

```bash
cd /opt/treefrogfree/engine
docker compose -f docker-compose.proxies.yml down            # stop + remove
# Optional: also strip the channels they added from the engine DB.
docker compose exec tf-engine sqlite3 /app/data/treefrog.db \
  "DELETE FROM streams WHERE source_label IN
     ('Samsung TV Plus', 'Pluto TV', 'DistroTV', 'Cabernet');"
docker compose exec tf-engine python -m engine publish
```

## 7. Why not import the proxies' M3U on a schedule instead?

The engine's `seed` command is idempotent: re-running it against the
same URL is a no-op for unchanged channels. The proxies' M3U changes
whenever their source adds or removes a channel (a couple of times
per month for the stable ones, more often for Cab as its plugin
bundle updates). For most operators, the manual `check-once` flow on
the admin UI is fine — the next health cycle will pick up any new
streams and demote any that went away.

If you want a fully hands-off path, the 30-minute cron in §3 is the
simplest answer. A longer-term enhancement would be a "subscribe to
URL" admin endpoint that adds the URL to the next health cycle
automatically, but that's a v2 feature.
