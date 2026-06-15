# Tree Frog Streams — Optional proxy stack

Three free, ad-supported IPTV source proxies that sit alongside the
main `tf-engine` to add ~700 channels (Samsung TV Plus + Pluto TV +
DistroTV), plus a fourth source (Xumo) imported as a remote M3U
without a container.

## What's in the box

| Source | Type | Port / URL | ~Channels | Auth |
|---|---|---|---|---|
| DistroTV | Container | **8787** | 150 | None |
| Samsung TV Plus | Container | **3001** | 400 | None |
| Pluto TV | Container | **80** | 250 | Pluto account (free) |
| Xumo | Remote M3U | `https://raw.githubusercontent.com/...xumo_playlist.m3u` | ~85 | None |

All three containers bind to the host with `network_mode: host` and
the `engine/docker-compose.proxies.yml` file caps them at a combined
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
| 80 | Pluto (this file) | Image default. Pinned via the `pluto-for-channels` service. |
| 3000 | **AIOstreams** — operator's most important service | **RESERVED. Never bind a container here, never curl 127.0.0.1:3000 to test a proxy.** |
| 3001 | Samsung TV Plus (this file) | Pinned via `PORT=3001`. |
| 8000 | Tree Frog engine (main compose) | The admin API and the engine-side endpoints. |
| 8787 | DistroTV (this file) | The image's only port. |
| 50005 | SSH (already on the host) | Non-standard SSH port. Never bind here. |

## 0. Pluto credentials (one-time setup)

Pluto requires a free Pluto TV account. The image's
`entrypoint.sh` reads `PLUTO_USERNAME` and `PLUTO_PASSWORD`
**directly from env vars** — it does not dereference the
`${VAR}_FILE` pattern used elsewhere in this repo. To keep the
creds out of the compose YAML, we use Compose's `${VAR}`
interpolation from a chmod-600 `.env` file.

```bash
# 0.1 Rotate the Pluto password first if you ever pasted it in
#     chat. Go to https://pluto.tv/account.

# 0.2 Put the creds in /root/.secrets (token files), then copy
#     them into engine/.env (compose interpolation source).
#     Either path works — the compose reads from engine/.env
#     and the token files are kept as backup.
sudo mkdir -p /root/.secrets
echo -n 'your-pluto-email' | sudo tee /root/.secrets/pluto_user >/dev/null
echo -n 'your-new-pluto-password' | sudo tee /root/.secrets/pluto_password >/dev/null
sudo chmod 600 /root/.secrets/pluto_*

# 0.3 Add the same values to engine/.env (gitignored, chmod 600).
#     Compose will interpolate ${PLUTO_USERNAME} and
#     ${PLUTO_PASSWORD} when starting the container. The values
#     are NEVER written into docker-compose.proxies.yml — only
#     the variable references are.
if [ ! -f /opt/treefrogfree/engine/.env ]; then
  sudo touch /opt/treefrogfree/engine/.env
  sudo chmod 600 /opt/treefrogfree/engine/.env
fi
echo "PLUTO_USERNAME=$(sudo cat /root/.secrets/pluto_user)" \
  | sudo tee -a /opt/treefrogfree/engine/.env >/dev/null
echo "PLUTO_PASSWORD=$(sudo cat /root/.secrets/pluto_password)" \
  | sudo tee -a /opt/treefrogfree/engine/.env >/dev/null
sudo chmod 600 /opt/treefrogfree/engine/.env

# 0.4 Verify the interpolation will work
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml config \
  | grep -A1 -E 'PLUTO_(USERNAME|PASSWORD):' | head
# Expect: PLUTO_USERNAME: <your-email>, PLUTO_PASSWORD: <something non-empty>
```

**Security caveats for this approach:**
- The values DO show up in `docker inspect tf-pluto-for-channels`
  on the host (this is unavoidable with this image). Keep
  `engine/.env` chmod 600 and limit who can SSH in.
- Anyone with read access to `engine/.env` (which is just `root`
  per the chmod 600) can see the password.

## 1. DistroTV — bring it up first

DistroTV is the simplest of the three. It has no auth, no DB, and
no admin UI — just a Python BaseHTTP listener.

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
# DistroTV doesn't publish XMLTV, so no epg-import call here.
```

Expected result on the engine side:

```
INFO treefrog.importer: Import done: 73 entries, 0 new channels, 73 new streams, 0 duplicates
INFO treefrog.catalog: Wrote catalog: 1714 channels, 29 categories
```

The 0 new channels is correct: all 73 DistroTV streams were
deduplicated against your existing 1714 by the
`canonical_channel_name()` consolidator.

## 2. Samsung TV Plus — port 3001

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

# EPG is at /epg.xml — confirmed working in production (a
# previous import of /xmltv.xml was wrong; the image actually
# serves EPG at /epg.xml, ~14 MB / ~2500 channels for the US
# region).
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:3001/epg.xml
```

## 3. Pluto for Channels — port 80

```bash
cd /opt/treefrogfree
docker compose -f engine/docker-compose.proxies.yml up -d pluto-for-channels
docker compose -f engine/docker-compose.proxies.yml logs --tail=20 pluto-for-channels
# Expect: NO MORE "PLUTO_USERNAME ... required" error.
# Expect: "Starting with 12 tuner(s)..." without the auth
# failure, followed by a successful login.

# Wait ~10s for the first login, then test.
sleep 10
curl -s -o /dev/null -w 'pluto m3u: HTTP %{http_code}, %{size_download}B\n' \
  http://127.0.0.1:80/tuner-1-playlist.m3u
# Expect: HTTP 200, several KB (the real M3U).

# If the curl returns 404, the most likely causes are:
#   1. engine/.env doesn't have PLUTO_USERNAME / PLUTO_PASSWORD.
#      Check: sudo cat /opt/treefrogfree/engine/.env | grep PLUTO_
#   2. The creds are wrong (rotated password didn't propagate).
#      Check: docker logs tf-pluto-for-channels | tail -20
#   3. Pluto's API rejected the login (rare — usually "wrong
#      password" surfaces as a 401 in the container log).

# Import.
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u http://127.0.0.1:80/tuner-1-playlist.m3u \
  --label "Pluto TV"
docker compose exec tf-engine python -m engine epg-import \
  --url http://127.0.0.1:80/epg.xml
```

## 4. (skipped) — no standalone Xumo container

There is no maintained standalone Xumo-for-channels Docker image.
The matthuisman channels-dvr family covers Samsung, Pluto, FrndlyTV,
and Kayo, but Xumo is not in the lineup. The next-best option is
the public Xumo M3U generator at
<https://github.com/BuddyChewChew/xumo-playlist-generator> — see
§5 below.

If you later want to host your own Xumo generator in a container,
the upstream generator is a small Python script; wrapping it in a
~30-line Dockerfile is a 30-minute job. But until then, the public
URL works.

## 5. Xumo — remote M3U (no container)

The
[BuddyChewChew/xumo-playlist-generator](https://github.com/BuddyChewChew/xumo-playlist-generator)
repo publishes a regenerated `xumo_playlist.m3u` (and matching
`xumo_epg.xml.gz`) on a schedule. The engine imports it the same
way it imports any other remote M3U.

```bash
# Verify the URL responds (sanity check, no import yet):
curl -sI https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_playlist.m3u
# Expect: HTTP 200, content-type text/plain or audio/x-mpegurl.

# Import:
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed \
  --m3u https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_playlist.m3u \
  --label "Xumo"

# The M3U's own `url-tvg=` header points to the matching XMLTV:
docker compose exec tf-engine python -m engine epg-import \
  --url https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_epg.xml.gz
```

**Tradeoffs of the remote-M3U approach:**
- **Pro:** zero extra container, zero extra port, zero extra CPU.
- **Pro:** the upstream generator refreshes the playlist on a
  schedule — the engine re-imports with each `seed` call.
- **Con:** if the upstream repo disappears, your Xumo channels
  vanish. **Pin a fork** if you want durability: fork the repo to
  your own GitHub, point the URLs at your fork, and update the
  URLs in your admin UI's "Sources" panel if the engine exposes
  one in v2.
- **Con:** the URLs in the M3U are signed CloudFront / Google CDN
  URLs that may expire. The generator refreshes daily; the
  channels will start failing within ~24h of a missing refresh.

If the Xumo M3U's reliability is a problem, the only stable
solution is a self-hosted Xumo generator. Until that exists, treat
Xumo as a "best effort" source alongside the more reliable
Samsung + Pluto + DistroTV trio.

## 6. Auto-refresh

The proxies refresh their own M3U/EPG every 15-60 minutes
(Pluto and Samsung are server-pushed; DistroTV is stable).
The Xumo generator refreshes on a schedule maintained by its
upstream maintainer. The engine needs a `check-once` to pick up
the new URLs. Two options:

**a) Manual.** Run `python -m engine check-once` from the admin UI
("Run health check" button) once after each refresh.

**b) Scheduled.** Add a 30-minute cron entry on the VPS host:

```bash
# /etc/cron.d/treefrog-proxy-refresh
*/30 * * * * root cd /opt/treefrogfree/engine && \
  docker compose exec -T tf-engine python -m engine check-once >/dev/null 2>&1
```

(You already have a 30-minute `HEALTH_CADENCE_SEC` in the engine
itself; this cron is for cases where you want a tighter cycle than
the in-process scheduler, or to force a re-evaluation after a
proxy-side refresh.)

## 7. Resource budget

Combined with the main engine and AIOstreams on a 4-vCPU / 4 GB
VPS:

| Service | CPU cap | RAM cap |
|---|---|---|
| `tf-engine` | 0.5 | 1 GB |
| `tf-samsung-tvplus` | 0.25 | 256 MB |
| `tf-pluto-for-channels` | 0.25 | 256 MB |
| `tf-distrotv-proxy` | 0.10 | 128 MB |
| **Subtotal** | **1.10** | **1.64 GB** |
| AIOstreams (already running) | 1.0 | 1.0 GB |
| **Total worst-case** | **2.10** | **2.64 GB** |

Plenty of headroom on a 4-vCPU / 4 GB VPS. Xumo adds nothing
since it's a remote M3U.

## 8. Tearing it back down

```bash
cd /opt/treefrogfree/engine
docker compose -f docker-compose.proxies.yml down            # stop + remove

# Optional: also strip the channels the proxies added from the
# engine DB.
docker compose exec tf-engine sqlite3 /app/data/treefrog.db \
  "DELETE FROM streams WHERE source_label IN
     ('Samsung TV Plus', 'DistroTV', 'Pluto TV', 'Xumo');"
docker compose exec tf-engine python -m engine publish
```

The bind mounts in `./data/` are preserved — re-running
`up -d` later will resume from where the proxy left off (cached
channel lists, EPG, plugin state).

## 9. Why not import the proxies' M3U on a schedule instead?

The engine's `seed` command is idempotent: re-running it against
the same URL is a no-op for unchanged channels. The proxies' M3U
changes whenever their source adds or removes a channel (a
couple of times per month for the stable ones). For most
operators, the manual `check-once` flow on the admin UI is fine
— the next health cycle will pick up any new streams and demote
any that went away.

If you want a fully hands-off path, the 30-minute cron in §6 is
the simplest answer. A longer-term enhancement would be a
"subscribe to URL" admin endpoint that adds the URL to the next
health cycle automatically, but that's a v2 feature.
