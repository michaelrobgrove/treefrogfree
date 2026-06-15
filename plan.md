# Tree Frog Streams — Implementation Plan

A unified, action-ready plan synthesizing the architecture from `chatgpt_plan.md` and the concrete scaffolding from `gemini.md`. Read top-to-bottom or jump to any phase.

---

## 0. Mission & Mindset

> **Tree Frog Streams is not an IPTV service.** It is a **channel registry**, **health monitor**, **failover engine**, **EPG manager**, and **playlist publisher**.

Keeping that framing leads to a system that runs on a tiny VPS while pushing public traffic through Cloudflare's free tier. It also makes the consolidation rules (Section 3) non-negotiable.

**Core product surface**
- `free.tfplus.stream` — public site, channel browser, playlist download, EPG.
- `admin.free.tfplus.stream` — admin UI for M3U import, bouquets, dead streams, uptime.
- `/s/<token>` — short redirect tokens that resolve to the live source URL.

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────┐
│   Cloudflare (free.tfplus.stream)            │
│   ── Pages: static site + admin SPA          │
│   ── Workers: /s/* redirects (KV lookup)     │
│   ── KV: short_token → active_stream_url     │
│   ── Cache: HTML/JSON/API responses          │
└──────────────────────────────────────────────┘
                    ▲
                    │ KV writes (PUT via API)
                    │ API reads (cached JSON)
                    │
┌──────────────────────────────────────────────┐
│   Proxmox LXC / Docker (tf-engine)           │
│   ── M3U importer                             │
│   ── Channel consolidator                     │
│   ── Health checker (30m loop)                │
│   ── Failover selector → KV writer            │
│   ── EPG fetcher + XMLTV store                │
│   ── Playlist generator (M3U + EPG)           │
│   ── Static JSON publisher (cached at edge)   │
│   ── SQLite database                          │
└──────────────────────────────────────────────┘
```

**Why this split** (from `chatgpt_plan.md`):
- Public traffic and redirects live at the edge → near-zero VPS bandwidth (the VPS has 12 TB/mo, but we don't spend it on public traffic anyway — we never proxy or restream).
- Heavy lifting (imports, parsing, health checks, ffprobe) lives on the VPS where it has free CPU and disk.
- The engine is a separate Docker container with **hard resource caps (0.5 vCPU, 1 GB RAM)** so it coexists with the host's primary workload (AIOstreams) without contention.
- SQLite is fine for the catalog size (~thousands of streams); no Postgres needed for v1.

**What `gemini.md` got right vs. wrong**
- ✅ Worker + KV redirect pattern is correct and production-ready.
- ✅ KV is the right primitive for hot redirect lookups.
- ⚠️ `engine.py` is MVP scaffolding — it inlines a `CHANNELS` dict and writes one stream per token. We must replace this with a real DB-backed pipeline.
- ⚠️ Health check via `requests.head` will give false positives (many streams return 200 on the manifest endpoint while being broken). We need a more robust probe (see §6).
- ⚠️ `schedule.every(30).minutes` will drift on long checks. We need an explicit loop with a fixed cadence.

---

## 2. Repository Layout

Monorepo so the engine and the edge stay in sync. The final layout is a superset of `gemini.md`'s tree:

```
treefrogfree/
├── plan.md                        ← this file
├── README.md
│
├── engine/                        ← runs on Proxmox (Docker)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── pyproject.toml             ← or requirements.txt
│   ├── .env.example
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── __main__.py            ← entrypoint
│   │   ├── config.py              ← env loading
│   │   ├── db.py                  ← SQLite + migrations
│   │   ├── models.py              ← dataclasses / schema
│   │   ├── importers/
│   │   │   ├── m3u.py             ← M3U parser
│   │   │   ├── epg.py             ← XMLTV parser
│   │   │   └── logos.py           ← logo fetch/dedup
│   │   ├── consolidator.py        ← channel normalization + dedup
│   │   ├── health.py              ← stream probe (with FFprobe fallback)
│   │   ├── failover.py            ← pick primary/backup winners
│   │   ├── publisher/
│   │   │   ├── kv.py              ← push token→url to CF KV
│   │   │   ├── playlist.py        ← render M3U output
│   │   │   ├── epg_xml.py         ← render XMLTV
│   │   │   └── json_catalog.py    ← public channel/category JSON
│   │   ├── api/
│   │   │   └── server.py          ← admin REST API (FastAPI or aiohttp)
│   │   └── scheduler.py           ← 30-min loop driver
│   ├── migrations/
│   │   └── 0001_init.sql
│   └── tests/
│       ├── test_consolidator.py
│       ├── test_m3u_parser.py
│       └── test_health.py
│
├── edge/                          ← deploys to Cloudflare
│   ├── wrangler.toml
│   ├── package.json
│   ├── tsconfig.json
│   ├── src/
│   │   ├── worker.ts              ← edge router (redirects, API cache)
│   │   └── api/
│   │       ├── channels.ts        ← public catalog JSON
│   │       └── search.ts          ← /api/search?q=
│   └── public/                    ← static site (or Nuxt build output)
│       ├── index.html
│       ├── channel.html
│       ├── playlist.html
│       ├── admin/                 ← admin SPA build
│       └── assets/
│
├── scripts/
│   ├── seed_db.py                 ← one-off seed from a real M3U
│   ├── backfill_kv.py             ← rebuild KV from DB
│   └── deploy.sh                  ← wrangler + docker compose helpers
│
└── docs/
    ├── architecture.md
    ├── operations.md              ← runbook
    └── brand-rules.md
```

**Languages chosen for fit, not familiarity**
- **Engine: Python 3.11** — best-in-class M3U/XMLTV parsing libs, fast iteration, runs comfortably in a 512MB container.
- **Edge: TypeScript (Workers)** — same language on both sides of CF, type-safe wrangler config.
- **Site: static HTML + vanilla JS for MVP** — no Nuxt/Vite needed until the site grows past a few pages. Lift from `gemini.md` directly.

---

## 3. Channel Consolidation Rules (the heart of the system)

Implemented in `engine/consolidator.py`. Test-first.

### 3.1 Normalization
```
"BBC News HD"  →  "bbc news"
"BBC NEWS"     →  "bbc news"
"BBC-News"     →  "bbc news"
"  CNN  "      →  "cnn"
```
Algorithm: lowercase → strip → drop punctuation → collapse whitespace → drop common suffixes (`hd`, `fhd`, `uhd`, `+1`, `east`, `west`, `24/7`, etc.).

### 3.2 Match strategy (in order)
1. `tvg-id` exact match (authoritative).
2. `tvg-name` normalized match.
3. Fuzzy match (rapidfuzz, threshold ≥ 88) gated on logo equality.
4. Manual review queue when confidence is in 70–87.

### 3.3 Storage shape
A `channels` row is the *logical* channel. Streams live as a separate list:

```sql
channels(id, normalized_name, display_name, tvg_id, group_id, logo_url, primary_stream_id, status, availability_pct, last_checked_at)
streams(id, channel_id, source_url, source_label, priority, status, last_ok_at, offline_since, response_ms)
bouquets(id, name, description)
bouquet_channels(bouquet_id, channel_id, position)
redirects(token PK, stream_id, updated_at)
epg_channels(tvg_id PK, display_name, icon_url)
health_logs(stream_id, checked_at, ok, latency_ms, error)
```

### 3.4 Branding rules (non-negotiable)
- **Do NOT rename channels.** `CNN`, `BBC News`, `Fox Weather` stay exactly as the source provides them.
- **DO rename groups.** Output groups prefixed with `🐸 Tree Frog Free | `:
  - `Kids` → `🐸 Tree Frog Free | Kids`
  - `News` → `🐸 Tree Frog Free | News`
  - `Sports` → `🐸 Tree Frog Free | Sports`
  - Unknown group → `🐸 Tree Frog Free | Other`
- **Logo priority:**
  1. Existing channel logo from source.
  ### 2. Known library (`logos/bbc-news.png`, etc.). NO. Source logos and then tree frog logo. 
  3. `treefrog-default.png`.
- **Tvg-rename:** never override the channel's own `tvg-id`; only set one if the source omits it.

### 3.5 Failover
- Primary = lowest `priority` value with `status = 'online'`.
- On primary failure, the next online stream in priority order becomes primary.
- All KV writes are atomic: one PUT replaces the previous URL for that token.
- If all streams are offline, the token maps to a 404 sentinel URL (so the Worker returns 410 Gone with `Retry-After: 1800`).

---

## 4. Database (SQLite, v1)

**Why SQLite, not Postgres**
- Single-writer engine process — no concurrency win from Postgres.
- A few thousand rows fit trivially in a 50 MB file.
- Backups = `cp treefrog.db backup/`. Done.
- Migration path to Postgres later is straightforward (SQLAlchemy / Drizzle make this a one-week job, not a v1 blocker).

**Settings that matter**
```sql
PRAGMA journal_mode = WAL;        -- concurrent reads while engine writes
PRAGMA synchronous = NORMAL;      -- durability vs speed trade we accept
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

**Backups**
- Nightly `sqlite3 .backup` to a date-stamped file.
- Retain 7 daily, 4 weekly.
- Replicate to Backblaze B2 or equivalent (rclone).

---

## 5. M3U Import Flow

Triggered from admin UI or CLI: `python -m engine seed --m3u https://provider.com/list.m3u`.

```
1. Download (HEAD first to check size, then GET with streaming)
2. Parse with igenetic/iptv-py or iptv-parser
3. For each entry:
   a. Normalize display name → hash key
   b. Look up existing channel by tvg-id, then by normalized name
   c. If new: create channel + stream
   d. If existing: append as new stream (dedupe on source_url)
4. Run initial health check on new streams (async, batched)
5. Rebuild KV redirect map for affected channels
6. Re-render public catalog JSON
7. Emit admin diff report (X new, Y dup, Z dead)
```

**Idempotency:** re-importing the same M3U must produce zero new rows. Store a SHA-256 of the source URL + the import's row signature; short-circuit on exact match.

**Bouquet assignment:** on import, channels go into an `Auto` bouquet. Admin moves them into named bouquets later.

---

## 6. Health Monitoring

### 6.1 Probe strategy
- `requests.head(url, timeout=5)` is the *first* filter — fast and cheap.
- A stream passes HEAD with 200/204/206 within 5s but might still be broken (most `.m3u8` hosts return 200 even when the playlist is empty or auth-failed).
- **Second filter:** GET the first 16 KB of the manifest. Must contain `#EXTM3U` and at least one `#EXTINF`.
- **Optional third filter (slow path):** `ffprobe -v error -show_entries format=duration` for ~3 seconds. Off by default; toggle per-channel when debugging.

### 6.2 Cadence
- All streams: every 30 minutes.
- Recently-failed streams: every 5 minutes for 1 hour after a failure (exponential backoff after).
- Never-online streams: every 6 hours until first success, then standard cadence.

### 6.3 Auto-disable
- Stream offline for 72 consecutive hours → `status = 'disabled'`, removed from playlist.
- Channel with zero enabled streams → `status = 'offline'`, hidden from public site.
- Admin gets a notification (webhook / email) on auto-disable.

### 6.4 Recovery
- Disabled stream passes two consecutive health checks → re-enabled automatically.
- Channel flips back to `online` → JSON catalog re-published, KV re-populated.

### 6.5 Implementation
- `asyncio` + `aiohttp` for concurrent probes (semaphore = 50 to avoid hammering).
- A single `scheduler.py` driver loops with an explicit 30-minute tick; no `schedule` library drift.
- Latency tracked in `health_logs`; rolling 7-day availability percentage computed in views or a `channels.availability_pct` column updated on each cycle.

---

## 7. Stream Obfuscation (Edge Redirects)

The Worker is intentionally tiny. A request to `/s/abc123` does one KV read and one 302. No DB, no parsing, no Python.

### 7.1 Token format
- 6-character base62 (`[a-z0-9]`) for the public v1 → 56 billion keyspace, plenty.
- Stable per channel: token never changes for the lifetime of a channel.
- Generated on channel create; never reused if a channel is deleted (kill the KV key).

### 7.2 Worker logic (TypeScript)
```ts
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname.startsWith('/s/')) {
      const token = url.pathname.slice(3);
      const target = await env.STREAM_KV.get(token);
      if (!target) {
        return new Response('Stream offline', {
          status: 410,
          headers: { 'Retry-After': '1800' },
        });
      }
      return Response.redirect(target, 302);  // 302 — players cache less aggressively
    }
    if (url.pathname === '/playlist.m3u') return env.ASSETS.fetch(req);
    return env.ASSETS.fetch(req);
  },
};
```

### 7.3 KV write cadence
- The engine pushes to KV every 30 minutes *only when the winner changes* — KV writes are free up to 100K/day on the free tier, but we want headroom for stats writes later.
- For failover events detected between health checks, an HTTP endpoint `POST /api/failover` is exposed by the engine and called by the Worker (Workers can make subrequests on a circuit-breaker) — implementation deferred to v2.

### 7.4 What the worker does NOT do
- No content serving, no proxying, no bandwidth — ever. We redirect; we don't stream.
- No logging of full URLs to Cloudflare — only token + status code (privacy + cost).

---

## 8. Public Website

Built as static HTML for v1 (lifted from `gemini.md` `index.html` and split into per-page templates). Move to Nuxt only when the page count grows past ~5.

### 8.1 Pages
| Path | Purpose |
|---|---|
| `/` | Hero + stats + search + featured channels + Plus CTA |
| `/channel/<id>` | Logo, name, current/next program, availability, "Watch" (m3u link), playlist download |
| `/playlist` | Big download button + setup guide per player (TiviMate, IPTV Smarters, Onn boxes) |
| `/epg` | EPG browser (table of now/next) |
| `/admin` | Login (Cloudflare Access in front) + dashboard |
| `/api/channels.json` | Public catalog, cached 5 min at edge |
| `/api/search?q=` | Public search index, cached 5 min at edge |

### 8.2 Home stats (auto-generated by engine)
- Working channel count
- Average availability (rolling 7 days)
- Category count
- "Updated every 30 minutes" (literally the last successful health-check timestamp)

### 8.3 Search
- Client-side fuzzy search against `/api/channels.json` (full catalog is < 200 KB gzipped at v1 scale).
- Instant filtering, no server round trip.

### 8.4 Plus CTA
- Small banners only — "Want more channels, better reliability, premium EPG? Visit TFPlus.stream."
- Never modal, never blocks the playlist download, never obscures the M3U button.

---

## 9. Admin UI

`admin.free.tfplus.stream` is the same static site under a separate Cloudflare Access policy (email OTP or a single admin email).

**MVP features**
- Add M3U URL (paste + import)
- Upload M3U file (drag/drop)
- List bouquets (create/rename/reorder)
- Duplicate review queue (channels with 70–87% match confidence)
- Dead streams list (sorted by `offline_since`)
- Uptime reports (per channel, per bouquet)
- Manual recheck button per stream
- Manual disable / enable per stream
- "Force re-publish KV" button (nuclear option)

**Implementation:** vanilla JS + small REST API exposed by the engine over a Tailscale or WireGuard tunnel (no public admin port). Cloudflare Access is the only path.

---

## 10. EPG

### 10.1 Sources
- Admin supplies one or more XMLTV URLs in admin UI.
- Engine fetches them on the same 30-min cadence (or 6h — EPG changes slower than channel status).

### 10.2 Mapping
- Primary: `tvg-id` exact match.
- Fallback: normalized `display-name` against `channels.normalized_name`.
- Unmatched programs are dropped silently (no garbage in the EPG output).

### 10.3 Output
- Single `epg.xml.gz` published alongside `playlist.m3u`.
- Cached at Cloudflare edge for 1 hour with stale-while-revalidate = 6 hours.

---

## 11. API Surface (engine admin)

FastAPI on a Unix socket behind an nginx stream (or a Caddy reverse proxy on Tailscale). No public exposure.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/channels` | list + filter |
| `POST` | `/api/import/m3u` | start import (returns job id) |
| `GET`  | `/api/import/:id` | import progress |
| `POST` | `/api/streams/:id/recheck` | force recheck |
| `POST` | `/api/streams/:id/disable` | manual disable |
| `POST` | `/api/streams/:id/enable` | manual enable |
| `GET`  | `/api/bouquets` | list |
| `PUT`  | `/api/bouquets/:id/channels` | set channel order |
| `GET`  | `/api/health/dead` | dead streams view |
| `GET`  | `/api/health/uptime` | availability report |
| `POST` | `/api/publish/kv` | force KV republish |
| `POST` | `/api/publish/catalog` | force catalog republish |

Auth: bearer token from `.env` (single admin v1). SSO with Cloudflare Access for v2.

---

## 12. Deployment

### 12.1 Proxmox LXC / Docker
**Resource budget (hard caps to coexist with the host's AIOstreams instance):**
- **CPU: 0.5 vCPU** (hard cap via `cpus: 0.5` in compose)
- **Memory: 1 GB** (hard cap via `mem_limit: 1g`)
- **Disk: 20 GB** (SQLite DB + cached downloads + logs)
- **Bandwidth: negligible** — only HEAD requests + 16 KB manifest snippets + KV PUTs

The caps matter: AIOstreams on the same host is bursty and needs headroom. A misbehaving health-check loop must not be able to starve it. Docker's `--cpus` and `mem_limit` give us a hard wall.

**`docker-compose.yml` shape** (synthesized from both sources — do **not** copy `gemini.md` verbatim; the in-memory `CHANNELS` dict and the `requests.head`-only health check are the gaps that need closing):

```yaml
version: '3.8'
services:
  tf-engine:
    build: ./engine
    container_name: treefrog-engine
    restart: unless-stopped
    cpus: 0.5                 # hard cap, AIOstreams must not be starved
    mem_limit: 1g             # hard cap
    mem_reservation: 512m     # guaranteed floor
    pids_limit: 200           # fork-bomb guard
    volumes:
      - ./data:/app/data      # SQLite + cached downloads
      - ./logs:/app/logs
    environment:
      - CF_API_TOKEN=${CF_API_TOKEN}
      - CF_ACCOUNT_ID=${CF_ACCOUNT_ID}
      - CF_KV_NAMESPACE_ID=${CF_KV_NAMESPACE_ID}
      - ADMIN_TOKEN=${ADMIN_TOKEN}
      - LOG_LEVEL=INFO
      - HEALTH_CONCURRENCY=50   # cap concurrent probes (see §6)
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 8000))"]
      interval: 30s
      retries: 3
      start_period: 60s
```

### 12.2 Cloudflare
- `free.tfplus.stream` → Pages project bound to `edge/public/`.
- Custom domain for admin: `admin.free.tfplus.stream` → same Pages project under a Cloudflare Access policy.
- Worker deployed via `wrangler deploy`.
- KV namespace `STREAM_KV` created in dashboard, ID goes in `wrangler.toml` and `docker-compose.yml` env.

### 12.3 First-time setup checklist
1. Cloudflare: create KV namespace `STREAM_KV`. Record namespace ID.
2. Cloudflare: create API token with `Workers KV Storage: Edit`.
3. Cloudflare: add `free.tfplus.stream` and `admin.free.tfplus.stream` to the zone.
4. Cloudflare Access: create an Access policy for `admin.free.*` (email OTP for one address).
5. Proxmox: provision LXC, install Docker, copy `engine/`.
6. `cp .env.example .env`, fill in CF credentials + `ADMIN_TOKEN`.
7. `docker compose up -d` — engine starts, runs first health check, publishes KV.
8. Local: `wrangler login`, `wrangler deploy` from `edge/`.
9. Smoke test: `curl -I https://free.tfplus.stream/s/<token>` should 302 to the real source.

---

## 13. MVP Milestones (concrete, ordered)

Each milestone ends with something the user can actually use.

### Phase 1 — Core engine (week 1)
- [ ] Repo skeleton + Docker compose
- [ ] SQLite schema + migrations
- [ ] M3U importer
- [ ] Consolidator + tests (the test suite here is non-negotiable)
- [ ] Health checker (HEAD + manifest sniff)
- [ ] Playlist generator
- [ ] CLI: `python -m engine seed --m3u <url>` and `python -m engine serve`
- [ ] Smoke: paste an M3U, get a valid `playlist.m3u` out

### Phase 2 — Edge (week 2)
- [ ] Cloudflare Worker + KV binding
- [ ] Static site (lift `index.html` from `gemini.md`, split into 3 pages)
- [ ] Engine → KV publisher
- [ ] `free.tfplus.stream` serves the playlist and the site
- [ ] `/s/<token>` redirects work end-to-end

### Phase 3 — Reliability (week 3)
- [ ] Failover logic
- [ ] Auto-disable after 72h offline
- [ ] Auto-recover on stream return
- [ ] Availability % per channel
- [ ] Dead-streams admin view

### Phase 4 — Admin + EPG (week 4)
- [ ] Admin API (FastAPI)
- [ ] Admin UI (vanilla JS, behind Cloudflare Access)
- [ ] Bouquet CRUD
- [ ] XMLTV import + EPG mapping
- [ ] Duplicate review queue

### Phase 5 — Polish (week 5+)
- [ ] Search
- [ ] Channel detail pages
- [ ] "Now playing" badge on cards
- [ ] Plus CTA banners
- [ ] Logos library (seed 200 popular channels manually)

### Phase 6 — Stretch (post-launch)
- [ ] User accounts + favorites
- [ ] Custom playlists per user
- [ ] Statistics dashboard
- [ ] Migration to Postgres if catalog > 10k streams

---

## 14. Observability

- **Engine logs:** structured JSON to stdout, picked up by `docker logs` and shipped to a tiny Loki/Vector if available. Otherwise just `journalctl -u docker`.
- **Metrics that matter:**
  - `streams_checked_total` (counter)
  - `health_check_duration_seconds` (histogram)
  - `streams_online` / `streams_offline` (gauge)
  - `kv_writes_total` (counter)
  - `playlist_publish_duration_seconds` (histogram)
- **Alerts (Telegram / Discord webhook, no PagerDuty needed at this scale):**
  - Engine container not running for > 5 min
  - > 20% of streams went offline in one cycle (provider dropped)
  - KV writes failing for > 15 min
  - Disk > 80% (DB bloat)
  - Container CPU throttled > 50% of wall time for > 10 min (means the 0.5 cap is too tight — raise it or lower `HEALTH_CONCURRENCY`)
  - Memory > 80% of 1 GB cap for > 5 min (memory leak in a parser)

---

## 15. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Source providers DMCA / shut down | Multi-source consolidation + per-stream priorities. Public site never proxies — it redirects. Streams can be swapped without touching the edge. |
| KV free-tier limits (100K writes/day) | Engine only writes on winner change. Daily budget headroom = ~5x at v1 scale. |
| `HEAD` returns 200 for broken `.m3u8` | Second filter reads first 16 KB and validates `#EXTM3U` + `#EXTINF`. |
| SQLite write contention under load | WAL mode + single-writer engine process. No contention at v1 scale. |
| Engine container starves AIOstreams on the same host | Hard caps: `cpus: 0.5`, `mem_limit: 1g`, `pids_limit: 200`, `HEALTH_CONCURRENCY=50`. Alert on sustained throttling. Worst case the engine skips a cycle; AIOstreams never notices. |
| Cloudflare Access misconfig locks out admin | Recovery: SSH to VPS, `docker exec`, rotate token. Add second admin email on day 1. |
| Brand confusion with TF+ | `free.tfplus.stream` and `tfplus.stream` are clearly separate products; CTA on free site drives Plus traffic without ever blocking the playlist. |

---

## 16. Open Questions (decide before Phase 2)

1. **Domain ownership** — is `tfplus.stream` already in the same Cloudflare account?
2. **Source M3U origin** — is the v1 catalog curated (paste a few known-good providers) or auto-scraped? Auto-scraping is out of scope for v1.
3. **Logo hosting** — Cloudflare R2 (free egress from CF) or local `/logos/`? Recommend R2 from day 1.
4. **EPG source** — XMLTV from a single provider, or admin-supplied URLs only? Recommend admin-supplied URLs only.
5. **Tunnel choice** — Cloudflare Tunnel or Tailscale for the admin API? Tailscale is simpler; Cloudflare Tunnel is one fewer moving part. Pick Tailscale v1.
6. **Single-tenant or multi-tenant** — v1 is unambiguously single-tenant (one operator, one VPS). Multi-tenant is a v3 problem.

---

## 17. Definition of Done (v1)

The MVP ships when:
- [ ] Operator can paste an M3U URL and get a working public playlist within 60 seconds.
- [ ] `/s/<token>` redirects resolve for every channel that has at least one online stream.
- [ ] Channel consolidation produces zero visible duplicates on `free.tfplus.stream`.
- [ ] Health checks run every 30 minutes and update KV within 5 minutes of a state change.
- [ ] Auto-disable + auto-recover work end-to-end (verified by a synthetic test stream).
- [ ] Admin can create a bouquet, assign channels, and re-publish the playlist.
- [ ] Home page shows real channel count, real availability %, and real "last updated" timestamp.
- [ ] Public site loads in < 1.5 s LCP on a cold cache, < 0.5 s on a warm cache.
- [ ] Total monthly cost: $0 (Cloudflare free tier) + VPS slice (already paid for, hard-capped at 0.5 vCPU / 1 GB to coexist with AIOstreams).
- [ ] Engine stays within its 0.5 vCPU / 1 GB caps under full-load health checks (verify with `docker stats` during a synthetic 1,000-stream burst test).
- [ ] No measurable impact on AIOstreams latency during engine cron ticks (verify with a 24h `docker stats` overlap window).

Once those are green, ship it. Then start on user accounts.
