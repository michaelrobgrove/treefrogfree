# Tree Frog Streams — Detailed Implementation Plan

## Executive Summary

This plan breaks down the remaining work for Tree Frog Streams based on the analysis of `plan.md` and the current codebase state. The system is a channel registry, health monitor, failover engine, EPG manager, and playlist publisher.

---

## 1. Current State Assessment

### Already Implemented (✅)

| Component | Status | Location |
|-----------|--------|----------|
| **Database Schema** | ✅ Complete | `engine/migrations/0001_init.sql`, `0002_browser_ok.sql`, `0003_cors_ok.sql` |
| **M3U Importer** | ✅ Complete | `engine/engine/importers/importer.py`, `m3u.py` |
| **Channel Consolidator** | ✅ Complete | `engine/engine/consolidator.py` |
| **Health Checker** | ✅ Complete | `engine/engine/health.py` (2-stage probe with browser UA) |
| **Scheduler** | ✅ Complete | `engine/engine/scheduler.py` (30-min drift-free loop) |
| **Playlist Generator** | ✅ Complete | `engine/engine/publisher/playlist.py` |
| **JSON Catalog** | ✅ Complete | `engine/engine/publisher/json_catalog.py` |
| **KV Publisher** | ✅ Complete | `engine/engine/publisher/kv.py`, `streams_kv.py` |
| **EPG Integration** | ✅ Complete | `engine/engine/admin/epg.py`, `publisher/epg_kv.py` |
| **Admin API Server** | ✅ Complete | `engine/engine/admin/server.py` |
| **Cloudflare Worker** | ✅ Complete | `edge/src/worker.ts` |
| **Static Site (Public)** | ✅ Complete | `edge/public/index.html`, `app.js`, `player.js` |
| **Static Site (Admin)** | ✅ Complete | `edge/public/admin/index.html` |
| **CLI Entrypoint** | ✅ Complete | `engine/engine/__main__.py` |
| **Docker Setup** | ✅ Complete | `engine/Dockerfile`, `engine/docker-compose.yml` |
| **Tests** | ✅ Complete | `engine/tests/test_consolidator.py`, `test_m3u_parser.py`, etc. |

### Partially Implemented (⚠️)

| Component | Status | Notes |
|-----------|--------|-------|
| **EPG XMLTV Output** | ⚠️ Needs verification | `render_epg_xml()` exists but needs testing |
| **CORS OK column** | ⚠️ Migration exists | `0003_cors_ok.sql` exists, but health.py needs to set it |
| **Bouquet CRUD** | ⚠️ Not in admin UI | Admin API has no bouquet endpoints |
| **Duplicate Review Queue** | ⚠️ Not in admin UI | No UI for 70-87% match confidence |
| **Channel Detail Pages** | ⚠️ Not in static site | Only index.html exists |
| **Search API** | ⚠️ Not in Worker | Worker has no `/api/search` endpoint |

### Missing (❌)

| Component | Status | Notes |
|-----------|--------|-------|
| **Bouquet Management API** | ❌ Missing | No endpoints for create/rename/reorder bouquets |
| **Duplicate Review UI** | ❌ Missing | No admin interface for reviewing potential duplicates |
| **Channel Detail Pages** | ❌ Missing | No `channel.html` template or data |
| **Playlist Setup Guide Page** | ❌ Missing | No `playlist.html` template |
| **EPG Browser Page** | ❌ Missing | No `/epg` page |
| **Search API Endpoint** | ❌ Missing | No `/api/search?q=` in Worker |
| **Tailscale Setup Script** | ❌ Missing | No automated tunnel setup |
| **Backup/Restore Scripts** | ❌ Missing | No database backup automation |

---

## 2. Implementation Phases

### Phase 1: Core Engine Verification & Fixes

**Goal:** Ensure the engine runs correctly and all components work together.

**Subtasks:**
- [ ] Verify database migrations apply correctly
- [ ] Fix CORS OK column population in health.py
- [ ] Add `streams:cors_ok` column to streams table if missing
- [ ] Test M3U import with a real source
- [ ] Test health check cycle
- [ ] Test KV publishing

**Verification Steps:**
```bash
cd engine
python -m engine migrate
python -m engine seed --m3u "https://example.com/list.m3u"
python -m engine check-once
python -m engine publish
```

**Commit Guidance:**
- Commit each fix separately with clear messages
- Tag as `v0.1-core-engine` when complete

---

### Phase 2: Edge Worker & Static Site Completion

**Goal:** Complete the public-facing edge components.

**Subtasks:**
- [ ] Add `/api/search?q=` endpoint to Worker
- [ ] Create `channel.html` template
- [ ] Create `playlist.html` template with setup guide
- [ ] Create `epg.html` template
- [ ] Update `app.js` to fetch channel details
- [ ] Add search functionality to public site

**Verification Steps:**
```bash
cd edge
npm run typecheck
wrangler deploy --dry-run
```

**Commit Guidance:**
- Commit static site pages together
- Tag as `v0.2-edge-site` when complete

---

### Phase 3: Admin UI Enhancement

**Goal:** Add missing admin features.

**Subtasks:**
- [ ] Add Bouquet CRUD API endpoints
- [ ] Add Bouquet management UI
- [ ] Add Duplicate Review Queue API
- [ ] Add Duplicate Review UI
- [ ] Add Channel enable/disable UI
- [ ] Add manual recheck buttons

**Verification Steps:**
- Test all admin endpoints with curl
- Verify UI interactions work

**Commit Guidance:**
- Tag as `v0.3-admin-ui` when complete

---

### Phase 4: Deployment Pipeline

**Goal:** Create production-ready deployment automation.

**Subtasks:**
- [ ] Create `.env.example` with all required variables
- [ ] Create `scripts/vps-setup.sh` for initial VPS config
- [ ] Create `scripts/backfill-kv.py` for KV rebuild
- [ ] Add backup script for SQLite database
- [ ] Document deployment runbook

**Verification Steps:**
```bash
./scripts/deploy-edge.sh
./scripts/vps-setup.sh
```

**Commit Guidance:**
- Tag as `v0.4-deployment` when complete

---

### Phase 5: Testing & Documentation

**Goal:** Ensure system reliability and document usage.

**Subtasks:**
- [ ] Run full test suite
- [ ] Add integration tests
- [ ] Create `docs/architecture.md`
- [ ] Create `docs/operations.md`
- [ ] Create `docs/brand-rules.md`
- [ ] Update README.md

**Verification Steps:**
```bash
cd engine
pytest -v
```

**Commit Guidance:**
- Tag as `v1.0-release` when complete

---

## 3. Detailed Task Breakdown

### 3.1 Database Schema Updates

**File:** `engine/migrations/0004_streams_cors_ok.sql`

```sql
-- Add cors_ok column to streams table
ALTER TABLE streams ADD COLUMN cors_ok INTEGER;
```

**File:** `engine/migrations/0005_imports_table.sql`

Already exists in `0001_init.sql`.

### 3.2 Health Checker Enhancement

**File:** `engine/engine/health.py`

Update `_probe_browser_ok()` to also set `cors_ok`:

```python
# In _check_one(), after browser_ok probe:
cors_ok = await _check_cors(url, session, timeout)
return ProbeResult(
    stream_id, True, latency_ms, None,
    browser_ok=browser_ok, cors_ok=cors_ok,
)
```

### 3.3 Admin API Endpoints

**File:** `engine/engine/admin/server.py`

Add bouquet endpoints:

```python
# GET /api/admin/bouquets - list bouquets
# PUT /api/admin/bouquets/:id/channels - reorder channels
# POST /api/admin/bouquets - create bouquet
# DELETE /api/admin/bouquets/:id - delete bouquet
```

### 3.4 Worker Search Endpoint

**File:** `edge/src/worker.ts`

Add search handler:

```typescript
if (path === "/api/search") {
  const q = url.searchParams.get("q") || "";
  const catalog = await env.STREAM_KV.get("catalog:channels.json");
  // Filter and return matching channels
}
```

### 3.5 Static Site Pages

**Files:** `edge/public/channel.html`, `edge/public/playlist.html`, `edge/public/epg.html`

Create templates following the pattern of `index.html`.

---

## 4. Verification Checklist

### Pre-Deployment
- [ ] All migrations apply cleanly
- [ ] Engine starts without errors
- [ ] Health check cycle completes
- [ ] KV publishes successfully
- [ ] Worker deploys without errors
- [ ] Public site loads correctly
- [ ] Admin UI is accessible
- [ ] All tests pass

### Post-Deployment
- [ ] Import an M3U successfully
- [ ] Verify channels appear on public site
- [ ] Verify /s/<token> redirects work
- [ ] Verify health checks update availability
- [ ] Verify EPG displays correctly
- [ ] Verify admin can manage bouquets
- [ ] Verify backup/restore works

---

## 5. Milestones

| Milestone | Target | Deliverable |
|-----------|--------|-------------|
| v0.1 | Core Engine | Engine runs, imports M3U, health checks work |
| v0.2 | Edge Site | Public site live, Worker redirects work |
| v0.3 | Admin UI | Full admin features available |
| v0.4 | Deployment | Automated deployment ready |
| v1.0 | Production | System in production, documented |

---

## 6. Risk Mitigation

| Risk | Mitigation |
|------|------------|
| KV write limits | Only write on change, use diff check |
| Health check timeouts | Use semaphore, timeout config |
| Database corruption | Use WAL mode, regular backups |
| CORS issues | Browser UA probe + proxy endpoint |
| Resource caps | Docker hard limits, monitoring |

---

## 7. Open Questions

1. **Domain ownership** — Is `tfplus.stream` in the same Cloudflare account?
2. **Source M3U origin** — Curated or auto-scraped? (Recommend curated for v1)
3. **Logo hosting** — Cloudflare R2 or local? (Recommend R2)
4. **EPG source** — Admin-supplied URLs only? (Recommend yes)
5. **Tunnel choice** — Tailscale or Cloudflare Tunnel? (Recommend Tailscale)
6. **Single-tenant** — v1 is single-tenant (confirmed)