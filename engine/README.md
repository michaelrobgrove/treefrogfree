# Tree Frog Streams — Engine

The engine is the long-running process that:
1. Imports M3U sources and consolidates them into a deduplicated channel catalog.
2. Polls every stream on a 30-minute cadence with a two-stage probe (HEAD + manifest sniff).
3. Maintains failover order and auto-disables streams that have been offline 72h.
4. Renders a public M3U playlist and a JSON channel catalog for the static site.
5. Exposes a small admin API for managing bouquets, dead streams, and re-checks.

See `../plan.md` for the full architecture, and `../edge/` for the Cloudflare
Worker that serves the public site and the `/s/<token>` redirects.

## Quick start (local dev)

```bash
cd engine
cp .env.example .env
pip install -r requirements.txt

# Apply migrations + create the SQLite DB
python -m engine migrate

# Import an M3U and see the consolidation
python -m engine seed --m3u https://example.com/list.m3u --label "My Source"

# Run the full pipeline once (health check + publish)
python -m engine check-once
python -m engine publish
python -m engine stats
```

## Run the long-lived scheduler

```bash
python -m engine serve
```

This runs forever: an initial cycle on startup, then a tick every
`HEALTH_CADENCE_SEC` (default 1800s = 30 min). Send SIGINT / SIGTERM to
stop cleanly.

## Tests

```bash
python -m pytest -q            # 31 unit tests
python -m tests.test_smoke     # end-to-end pipeline against a local server
```

The smoke test spins up a tiny aiohttp server on a random port that
serves a fake M3U manifest, runs the full import → consolidate → health
→ publish pipeline, and verifies the output. It uses a temp DB so it's
safe to run alongside a real engine.

## Docker (production)

```bash
cp .env.example .env
# Fill in CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, ADMIN_TOKEN.
docker compose up -d
docker compose logs -f tf-engine
```

Hard caps (`cpus: 0.5`, `mem_limit: 1g`, `pids_limit: 200`) coexist with
AIOstreams on the same host without contention. See `../plan.md` §12.1.

## Layout

```
engine/
├── engine/
│   ├── __main__.py     # CLI entrypoint (serve / seed / check-once / publish / stats)
│   ├── config.py       # env loading
│   ├── db.py           # SQLite + migrations
│   ├── models.py       # dataclasses
│   ├── scheduler.py    # 30-min loop driver
│   ├── health.py       # two-stage probe + auto-disable
│   ├── consolidator.py # channel name normalization
│   ├── importers/
│   │   ├── m3u.py      # streaming M3U parser
│   │   └── importer.py # M3U → DB orchestration
│   └── publisher/
│       ├── playlist.py # M3U output
│       └── json_catalog.py # public channel/category JSON
├── migrations/
│   └── 0001_init.sql
├── tests/
│   ├── test_consolidator.py
│   ├── test_m3u_parser.py
│   └── test_smoke.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Branding rules (non-negotiable)

From `../plan.md` §3.4. The engine enforces these at the playlist /
catalog level:

- **Channel display names are preserved exactly** as the source provided.
  We never rename `BBC News` to `British Broadcasting Corporation News`.
- **Group titles are prefixed** with `🐸 Tree Frog Free | `:
  `News` → `🐸 Tree Frog Free | News`.
- **Logo priority:** source-provided → `treefrog-default.png`. We do
  not host a known-library v1.

These rules are unit-tested in `tests/test_consolidator.py`.
