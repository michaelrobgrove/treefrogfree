# Tree Frog Streams — Optional proxy stack

Two free, ad-supported IPTV source proxies that sit alongside the
main `tf-engine` to add ~550 channels (Samsung TV Plus + DistroTV),
plus twelve remote-M3U sources (Pluto US/CA, Local Now, Plex
US/CA, Xumo, Tubi, Samsung TV Plus KR, Airy, TCL, DistroTV US/CA
with EPG) imported directly from public GitHub raw URLs with no
containers.

## What's in the box

| Source | Type | Port / URL | ~Channels | Auth | EPG |
|---|---|---|---|---|---|
| DistroTV (container) | Container | **8787** | 150 | None | none |
| Samsung TV Plus US (container) | Container | **3001** | 400 | None | `/epg.xml` |
| Pluto US | Remote M3U | `BuddyChewChew/pluto/.../pluto_us.m3u` | 250 | None | M3U header |
| Pluto CA | Remote M3U | `BuddyChewChew/pluto/.../pluto_ca.m3u` | 75 | None | M3U header |
| Local Now | Remote M3U | `apsattv.com/localnow.m3u` | 30 | None | explicit (`i.mjh.nz`) |
| Plex US | Remote M3U | `BuddyChewChew/plex-alt-fast-channels/.../plex_us.m3u` | 400 | None | M3U header |
| Plex CA | Remote M3U | `BuddyChewChew/plex-alt-fast-channels/.../plex_ca.m3u` | 75 | None | M3U header |
| Xumo | Remote M3U | `BuddyChewChew/xumo-playlist-generator/.../xumo_playlist.m3u` | 85 | None | M3U header |
| Tubi | Remote M3U | `BuddyChewChew/tubi-scraper/.../tubi_playlist.m3u` | 300 | None | M3U header |
| Samsung TV Plus KR | Remote M3U | `BuddyChewChew/samsungtvplus/.../samsung_tvplus.m3u` | 200 | None | none (i.mjh.nz if you want) |
| Airy | Remote M3U | `BuddyChewChew/airy-playlist-generator/.../airy_channels.m3u` | 60 | None | `x-tvg-url=` header |
| TCL | Remote M3U | `BuddyChewChew/tcl-playlist-generator/.../tcl.m3u8` | 200 | None | `x-tvg-url=` header |
| DistroTV US (remote) | Remote M3U | `BuddyChewChew/distro-playlist-generator/.../distrotv_US.m3u` | 150 | None | `distrotv.xml` |
| DistroTV CA (remote) | Remote M3U | `BuddyChewChew/distro-playlist-generator/.../distrotv_CA.m3u` | 30 | None | `distrotv.xml` |

The two containers bind to the host with `network_mode: host` and
the `engine/docker-compose.proxies.yml` file caps them at a
combined ~0.35 vCPU / 384 MB RAM so they coexist with `tf-engine`
and AIOstreams. The twelve remote M3Us are zero-cost — no extra
container, no port, no CPU.

### ⚠️ Host port allocation — read before binding anything

The proxies use host networking, so their container ports land
directly on the host. **Port collisions will crash the second
container with `OSError: [Errno 98] Address already in use`.** The
table below is the live state of one operator's host; if your
host differs, edit the `PORT` env var in
`engine/docker-compose.proxies.yml` to match a free port.

| Host port | What's on it | Action |
|---|---|---|
| 3000 | **AIOstreams** — operator's most important service | **RESERVED. Never bind a container here, never curl 127.0.0.1:3000 to test a proxy.** |
| 3001 | Samsung TV Plus (this file) | Pinned via `PORT=3001`. |
| 8000 | Tree Frog engine (main compose) | The admin API and the engine-side endpoints. |
| 8787 | DistroTV (this file) | The image's only port. |
| 50005 | SSH (already on the host) | Non-standard SSH port. Never bind here. |

> **Pluto TV — geo-blocked from cloud IPs.** A previous revision
> of this stack ran the `jonmaddox/pluto-for-channels` container.
> Auth succeeded but `https://api.pluto.tv/v2/channels` returned
> `[]` (empty array) from this VPS's IP. The container wrote
> 9-byte stub M3U and 12-byte stub EPG files and logged
> `[SUCCESS] Wrote the M3U8 playlist` as if the fetch had worked.
> Diagnosis: the catch on `[ERROR]` never tripped because the API
> returned an empty JSON array rather than throwing. The
> `BuddyChewChew/pluto` repo serves a regenerated M3U that hits
> Pluto's CDN through a different egress path and works from
> cloud IPs. That's what we use now.

## 1. DistroTV container — bring it up first

The DistroTV container is the simplest of the two containers. It
has no auth, no DB, and no admin UI — just a Python BaseHTTP
listener. It serves the combined DistroTV channel list (US + CA
mixed). Use this container as the primary DistroTV source; the
remote DistroTV US/CA M3Us (§3) are a useful supplement because
they have an EPG the container doesn't.

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d distrotv-proxy

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
# The DistroTV container doesn't publish XMLTV. If you want an
# EPG for DistroTV, import the remote distrotv.xml from §3
# (DistroTV US + CA remote M3U + EPG).
```

Expected result on the engine side:

```
INFO treefrog.importer: Import done: 73 entries, 0 new channels, 73 new streams, 0 duplicates
INFO treefrog.catalog: Wrote catalog: 1714 channels, 29 categories
```

The 0 new channels is correct: all 73 DistroTV streams were
deduplicated against your existing 1714 by the
`canonical_channel_name()` consolidator.

## 2. Samsung TV Plus container — port 3001

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d samsung-tvplus
docker compose -f engine/docker-compose.proxies.yml logs --tail=20 samsung-tvplus
# Look for "Starting server on port 3001" — if you see 80
# instead, the env var didn't take. Check
# `docker inspect tf-samsung-tvplus | grep -A2 PORT`.

# Verify with GET (not HEAD — the BaseHTTP backend only handles GET).
# Note the path: /playlist.m3u8 (with the .m3u8 extension), NOT
# /tuner-1-playlist.m3u. The `?regions=us` filter is also set as
# REGIONS=us in the compose, so it's redundant but explicit here.
curl -s -o /tmp/samsung.m3u -w 'HTTP %{http_code}, %{size_download}B, type=%{content_type}\n' \
  'http://127.0.0.1:3001/playlist.m3u8?regions=us'
head -3 /tmp/samsung.m3u
wc -l /tmp/samsung.m3u
# Expect: HTTP 200, several hundred KB, type=audio/x-mpegurl,
# body starts with "#EXTM3U", ~400-500 lines.

# Import the full channel list:
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u 'http://127.0.0.1:3001/playlist.m3u8?regions=us' \
  --label "Samsung TV Plus"

# EPG is at /epg.xml (the image's EPG endpoint).
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:3001/epg.xml
```

## 3. Twelve remote M3U sources (no container)

These are public raw M3U files on GitHub (plus one on
`apsattv.com`). They don't need a container, a port, or a process
— the engine's `seed` command downloads the M3U the same way it
imports any other URL. The maintainer regenerates the M3U on a
schedule; the engine re-imports the freshest copy on each `seed`
call.

Run them all in one block. Skip any source whose label you'd
rather not have (the engine dedupes, so overlapping sources are
safe but waste import time).

```bash
cd /opt/treefrogfree/engine

# ── Pluto US (250+ channels) ──────────────────────────────────
# EPG is the M3U's own `url-tvg=` header — the importer reads
# it automatically, no need to call `epg-import` separately.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/pluto/main/pluto_us.m3u \
  --label "Pluto US"

# ── Pluto CA (~75 channels) ──────────────────────────────────
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/pluto/main/pluto_ca.m3u \
  --label "Pluto CA"

# ── Local Now (apsatt, ~30 channels) ─────────────────────────
# The apsatt M3U does NOT include a `url-tvg=` header, so we
# pin the EPG URL explicitly. Local Now's EPG is at
# i.mjh.nz (same matthuisman generator that backs Pluto).
docker compose exec tf-engine python -m engine seed \
  --m3u https://www.apsattv.com/localnow.m3u \
  --label "Local Now"
docker compose exec tf-engine python -m engine epg-import \
  --url https://github.com/matthuisman/i.mjh.nz/raw/master/LocalNow/us.xml.gz

# ── Plex Fast (US) — ~400 channels ───────────────────────────
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/plex-alt-fast-channels/main/playlists/plex_us.m3u \
  --label "Plex US"

# ── Plex Fast (CA) — ~75 channels ────────────────────────────
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/plex-alt-fast-channels/main/playlists/plex_ca.m3u \
  --label "Plex CA"

# ── Xumo (~85 channels) ──────────────────────────────────────
# EPG is the M3U's own `url-tvg=` header.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_playlist.m3u \
  --label "Xumo"

# ── Tubi (~300 free movies + channels) ───────────────────────
# EPG is the M3U's own `url-tvg=` header (points to
# tubi_epg.xml in the same repo). Tubi is a movie+TV service
# with heavy ad breaks; not all engines deduplicate Tubi
# entries against other FAST services, so you may see some
# overlap with Pluto/Plex.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/main/tubi_playlist.m3u \
  --label "Tubi"

# ── Samsung TV Plus (Korean) — ~200 channels ─────────────────
# The Korean lineup. Complement to the US container (the
# container is pinned REGIONS=us). The M3U does NOT include
# a `url-tvg=` header; pin i.mjh.nz's SamsungTV epg explicitly
# if you want EPG for the Korean channels.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/samsungtvplus/main/output/samsung_tvplus.m3u \
  --label "Samsung TV Plus KR"
docker compose exec tf-engine python -m engine epg-import \
  --url https://github.com/matthuisman/i.mjh.nz/raw/master/SamsungTV/kr.xml.gz

# ── Airy (AiryTV) — ~60 channels ─────────────────────────────
# EPG is the M3U's `x-tvg-url=` header. The engine's importer
# reads both `url-tvg=` and `x-tvg-url=`; either is fine.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/airy-playlist-generator/main/airy_channels.m3u \
  --label "Airy"

# ── TCL (TCL TV Plus) — ~200 channels ────────────────────────
# EPG is the M3U's `x-tvg-url=` header (tcl_epg.xml in the
# same repo).
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/tcl-playlist-generator/main/tcl.m3u8 \
  --label "TCL"

# ── DistroTV US (remote M3U with EPG) — ~150 channels ────────
# BuddyChewChew's DistroTV generator hits the upstream API
# directly and includes an EPG that the container does not.
# Pair this with the container's import (which has a more
# stable playlist shape) for both regions + EPG.
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv_US.m3u \
  --label "DistroTV US (remote)"
docker compose exec tf-engine python -m engine epg-import \
  --url https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv.xml

# ── DistroTV CA (remote M3U, same EPG) — ~30 channels ────────
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv_CA.m3u \
  --label "DistroTV CA (remote)"
```

**About `apsattv.com/localnow.m3u`:** This is a maintained M3U
file that re-publishes Local Now's linear streams. It's not on
GitHub but it's been stable for years. If the URL ever 404s, the
Local Now channels silently drop on the next import — restart
with the comment-out line below to remove the source entirely.

**About overlapping sources (Tubi, Pluto, Plex):** All four are
FAST services, and the same show sometimes appears on two
services under different names. The engine's
`canonical_channel_name()` consolidator handles name-level
deduplication, but not "same show on different services with
different metadata" — so you'll see some near-duplicate entries
in the grid. The `availability_pct` in the grid sorts higher-
quality sources to the top, so users tend to pick the more
reliable stream naturally.

**Tradeoffs of the remote-M3U approach:**
- **Pro:** zero extra container, zero extra port, zero extra CPU.
- **Pro:** the upstream generator refreshes the playlist on a
  schedule — the engine re-imports with each `seed` call.
- **Con:** if the upstream repo disappears, your channels vanish.
  **Pin a fork** if you want durability: fork the repo to your
  own GitHub, point the URLs at your fork.
- **Con:** some M3Us (Pluto, Plex, Tubi) contain signed CloudFront
  URLs that may expire. The generators refresh daily; the
  channels will start failing within ~24h of a missing refresh.

If the remote M3Us' reliability is a problem, the only stable
solution is to host your own generator. The
`BuddyChewChew/xumo-playlist-generator` repo is a small Python
script; wrapping it in a ~30-line Dockerfile is a 30-minute job.
For Pluto and Plex, the upstream is more complex (it talks to
Pluto's stitcher APIs and Plex's EPG provider), so a self-host
is a real project rather than a 30-minute job. Treat all twelve
remote sources as "best effort" alongside the more reliable
Samsung + DistroTV container pair.

## 4. Auto-refresh

The two containers refresh their own M3U/EPG every 15-60 minutes
(Samsung is server-pushed; DistroTV is stable). The remote M3U
generators refresh on a schedule maintained by their upstream
maintainers. The engine needs a `check-once` to pick up the new
URLs. Two options:

**a) Manual.** Run `python -m engine check-once` from the admin
UI ("Run health check" button) once after each refresh.

**b) Scheduled.** Add a 30-minute cron entry on the VPS host:

```bash
# /etc/cron.d/treefrog-proxy-refresh
*/30 * * * * root cd /opt/treefrogfree/engine && \
  docker compose exec -T tf-engine python -m engine check-once >/dev/null 2>&1
```

(You already have a 30-minute `HEALTH_CADENCE_SEC` in the engine
itself; this cron is for cases where you want a tighter cycle
than the in-process scheduler, or to force a re-evaluation after
a proxy-side refresh.)

## 5. Resource budget

Combined with the main engine and AIOstreams on a 4-vCPU / 4 GB
VPS:

| Service | CPU cap | RAM cap |
|---|---|---|
| `tf-engine` | 0.5 | 1 GB |
| `tf-samsung-tvplus` | 0.25 | 256 MB |
| `tf-distrotv-proxy` | 0.10 | 128 MB |
| **Subtotal** | **0.85** | **1.38 GB** |
| AIOstreams (already running) | 1.0 | 1.0 GB |
| **Total worst-case** | **1.85** | **2.38 GB** |

The twelve remote M3Us add nothing — the engine's `seed`
downloads the M3U the same way it downloads any HTTP URL.

## 6. Tearing it back down

```bash
cd /opt/treefrogfree/engine
docker compose -f docker-compose.proxies.yml down            # stop + remove

# Optional: also strip the channels the proxies added from the
# engine DB. The remote-M3U sources use the labels you gave
# them in the `--label` argument above.
docker compose exec tf-engine sqlite3 /app/data/treefrog.db \
  "DELETE FROM streams WHERE source_label IN
     ('Samsung TV Plus', 'DistroTV',
      'Pluto US', 'Pluto CA', 'Local Now',
      'Plex US', 'Plex CA', 'Xumo', 'Tubi',
      'Samsung TV Plus KR', 'Airy', 'TCL',
      'DistroTV US (remote)', 'DistroTV CA (remote)');"
docker compose exec tf-engine python -m engine publish
```

The bind mounts in `./data/` are preserved — re-running
`up -d` later will resume from where the proxies left off (cached
channel lists, EPG, plugin state).

## 7. Why not import the proxies' M3U on a schedule instead?

The engine's `seed` command is idempotent: re-running it against
the same URL is a no-op for unchanged channels. The proxies' M3U
changes whenever their source adds or removes a channel (a
couple of times per month for the stable ones). For most
operators, the manual `check-once` flow on the admin UI is fine
— the next health cycle will pick up any new streams and demote
any that went away.

If you want a fully hands-off path, the 30-minute cron in §4 is
the simplest answer. A longer-term enhancement would be a
"subscribe to URL" admin endpoint that adds the URL to the next
health cycle automatically, but that's a v2 feature.
