# Tree Frog Streams

> Clean, reliable live TV — a channel registry, health monitor, failover engine, EPG manager, and playlist publisher.

This is **not** an IPTV service. It's the deduplication + health layer that sits between a (potentially messy) set of upstream M3U sources and a polished, public-facing IPTV playlist.

## What you get

- **Consolidated channels** — `BBC News`, `BBC NEWS HD`, `BBC-News` collapse into one logical channel; source stream names are preserved.
- **Two-stage health checks** every 30 minutes (HEAD + manifest sniff), with auto-disable for streams offline > 72h and auto-recovery.
- **Cloudflare edge redirects** — `/s/<token>` resolves in 1 KV read at the edge, never touching the origin.
- **Branded playlist** with `🐸 Tree Frog Free | <group>` group titles, ready for TiviMate, IPTV Smarters, Onn boxes, etc.
- **EPG** via XMLTV with tvg-id matching and gzip-served output.
- **Tiny footprint** — engine runs in 0.5 vCPU / 1 GB on the same VPS as AIOstreams.

## Architecture

```
┌──────────────────────────────────────────┐
│  Cloudflare (free.tfplus.stream)         │
│  ── Pages / Workers Assets: static site  │
│  ── Worker: /s/* 302 redirects (KV)      │
│  ── KV: token → live source URL          │
└──────────────────────────────────────────┘
                ▲
                │ KV writes
                │ /api/* proxy
                │
┌──────────────────────────────────────────┐
│  VPS (Proxmox LXC, Docker)               │
│  ── Engine: M3U import + health loop     │
│  ── Admin API: aiohttp on Tailscale      │
│  ── SQLite catalog + EPG cache           │
└──────────────────────────────────────────┘
```

See [plan.md](plan.md) for the full design doc.

## Repo layout

```
.
├── engine/             # Python 3.11 — runs in Docker on the VPS
├── edge/               # TypeScript — Cloudflare Worker + static site
├── scripts/            # deploy.sh + vps-setup.sh + backfill helpers
├── docs/               # architecture, runbook, brand rules
├── plan.md             # the full implementation plan
├── chatgpt_plan.md     # original architecture source
└── gemini.md           # original scaffolding source
```

## Quick start

### Edge (one-time)

```bash
cd edge
npm install
wrangler login
wrangler kv:namespace create STREAM_KV    # paste the id into wrangler.toml
wrangler deploy
```

The Worker is live at `https://treefrog-streams.<your-subdomain>.workers.dev`.
Add custom domains (`free.tfplus.stream`, `admin.free.tfplus.stream`) by
uncommenting the `routes` block in `wrangler.toml` and re-running `wrangler deploy`.

### Engine (VPS)

```bash
# On the VPS, after uploading the repo:
cd engine
cp .env.example .env
# Fill in CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, ADMIN_TOKEN.
docker compose up -d
docker compose logs -f tf-engine
```

See [docs/vps-deploy.md](docs/vps-deploy.md) for the full checklist.

## Tests

```bash
cd engine
python -m pytest -q                # 31 unit tests
python -m tests.test_smoke         # end-to-end pipeline against a local server
```

## Status

- ✅ Phase 1: engine (import, consolidate, health, publish)
- ✅ Phase 2: edge Worker + static site
- ✅ Phase 3: admin API + EPG
- ⏳ Phase 4: production deploy (waiting on VPS access + custom domain DNS)
