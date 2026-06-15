# Tree Frog Streams — VPS Deployment Runbook

This is the full checklist to take the project from "code on GitHub" to
"channels streaming at `free.tfplus.stream`." Run these steps in order.

## TL;DR

```bash
# (VPS, first time) install Docker + clone + start engine:
curl -fsSL https://raw.githubusercontent.com/michaelrobgrove/treefrogfree/main/scripts/vps-setup.sh | bash

# (VPS, after editing .env):
cd /opt/treefrogfree/engine
docker compose up -d
docker compose logs -f tf-engine   # watch the first cycle

# (Local, whenever you change edge code):
cd edge && npm run deploy
```

> **Don't put real API tokens in your shell history or in any
> committed file.** Use a `.token` file (gitignored) and reference it
> from `.env`. Example layout:
>
> ```bash
> # /opt/treefrogfree/engine/.env
> CF_API_TOKEN_FILE=/root/.secrets/cf.token
> ADMIN_TOKEN_FILE=/root/.secrets/admin.token
> ```
>
> The engine reads `${VAR}_FILE` paths if set, before falling back to
> `${VAR}` inline values. Keep your real tokens in `chmod 600`
> files outside the repo.

That's it for the moving parts. The rest of this doc covers the one-time
Cloudflare + DNS + tunnel setup.

---

## 0. Prerequisites

You need:

- ✅ A Cloudflare account with `tfplus.stream` added as a zone.
- ✅ A VPS (your existing one with AIOstreams works — we cap the
  engine to 0.5 vCPU / 1 GB and AIOstreams is unaffected).
- ✅ Tailscale (or any other private network) reachable from the
  device(s) you'll use to access the admin UI.
- ✅ Local access to `wrangler` (already configured: `wrangler whoami` works).
- ✅ The KV namespace `STREAM_KV` (id `0fc0537c9a5642c0a327679b16a05128`).

## Architecture (one-line version)

The engine on the VPS writes **3 things to Cloudflare KV** every health
cycle: per-channel redirect tokens, the public catalog JSON, and the
public M3U playlist. The Worker reads them straight from KV — the
engine **never** needs a public URL, the admin UI talks to the engine
directly over Tailscale.

---

## 1. One-time Cloudflare setup

### 1.1 Get an API token (for the engine to write KV)

1. Go to <https://dash.cloudflare.com/profile/api-tokens>.
2. Click **Create Token** → use the **Edit Cloudflare Workers** template.
3. Permissions: `Workers KV Storage:Edit`, `Account.Workers Scripts:Read` (optional).
4. **Account Resources**: include your account; **Zone Resources**: include `tfplus.stream`.
5. Copy the token — paste it into `engine/.env` as `CF_API_TOKEN`.
6. Your `CF_ACCOUNT_ID` is the value `wrangler whoami` shows at the top.

### 1.2 Add the custom domains to the Worker

The Worker is deployed at `https://treefrog-streams.<your-sub>.workers.dev`.
To make it serve `free.tfplus.stream`:

1. In the Cloudflare dashboard, go to **Workers & Pages → treefrog-streams**.
2. Click **Settings → Triggers → Add Custom Domain**.
3. Add `free.tfplus.stream` (and `admin.free.tfplus.stream`).
4. Cloudflare will auto-create the DNS `CNAME` records.

Alternatively, edit `edge/wrangler.toml` and uncomment the `routes` block
(replacing `custom_domain = true` with the right zone), then run
`wrangler deploy` from `edge/`.

### 1.3 Lock down the admin subdomain (Cloudflare Access)

1. Go to **Zero Trust → Access → Applications**.
2. **Add an application** → Self-hosted.
3. Name: `Tree Frog Admin`. Domain: `admin.free.tfplus.stream`.
4. Policy: **Allow** — Emails — your email address only.
5. Identity provider: One-time PIN (built-in).
6. Save. Now visiting `admin.free.tfplus.stream` triggers an email OTP
   before the page loads.

(Public `free.tfplus.stream` is intentionally *not* behind Access.)

---

## 2. VPS setup

### 2.1 Install Docker + clone the repo

SSH into the VPS as a sudo user, then:

```bash
curl -fsSL https://raw.githubusercontent.com/michaelrobgrove/treefrogfree/main/scripts/vps-setup.sh | bash
```

This installs Docker (if missing), clones the repo to `/opt/treefrogfree`,
and starts the engine container. The first run will exit early and ask
you to fill in `.env`. Do that now:

```bash
sudo nano /opt/treefrogfree/engine/.env
```

Fill in at minimum:

```
CF_API_TOKEN=<from step 1.1>
CF_ACCOUNT_ID=db91f29588dc8d30fcee5fc934e97d1d
CF_KV_NAMESPACE_ID=0fc0537c9a5642c0a327679b16a05128
ADMIN_TOKEN=<32 bytes of randomness, e.g.  openssl rand -hex 32>
LOG_LEVEL=INFO
```

**Security note:** if you'd rather not have raw tokens in a dotenv
file, use the `${VAR}_FILE` pattern. The engine reads `${VAR}_FILE`
before `${VAR}`, so you can mix-and-match. The compose file already
bind-mounts `/root/.secrets` from the host into the container at
`/secrets:ro`, so:

```bash
# Create the token file on the HOST (not inside the container)
sudo mkdir -p /root/.secrets && sudo chmod 700 /root/.secrets
echo -n 'your-real-cf-token' | sudo tee /root/.secrets/cf.token >/dev/null
echo -n "$(openssl rand -hex 32)" | sudo tee /root/.secrets/admin.token >/dev/null
sudo chmod 600 /root/.secrets/*.token

# Reference it in .env — note the path is /secrets/... because that's
# the in-container mount point, not the host path.
echo 'CF_API_TOKEN_FILE=/secrets/cf.token' >> /opt/treefrogfree/engine/.env
echo 'ADMIN_TOKEN_FILE=/secrets/admin.token' >> /opt/treefrogfree/engine/.env
```

Then re-run the script (or just `docker compose up -d`).

### 2.2 Verify the engine is up

```bash
docker compose -f /opt/treefrogfree/engine/docker-compose.yml ps
docker compose -f /opt/treefrogfree/engine/docker-compose.yml logs -f tf-engine
```

You should see:

1. Migrations applied.
2. API server listening on `http://127.0.0.1:8000`.
3. First health cycle: 0 streams checked (empty DB) → published empty playlist/catalog.

### 2.3 Tailscale (required for admin access)

The admin API binds to `127.0.0.1:8000` and the admin UI talks to it
directly from your browser. The Worker does **not** proxy `/api/*` —
the engine has no public URL.

```bash
# On the VPS:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Visit https://login.tailscale.com/admin/machines to confirm.
```

You can now reach the admin API at `http://<vps-tailscale-ip>:8000`
from any device on your Tailscale network. The admin UI is configured
with that IP baked in (see `edge/public/admin/index.html`).

> If you'd rather not bake in an IP, edit the `ENGINE_API` line in
> `edge/public/admin/index.html` to your preferred hostname (Tailscale
> MagicDNS, Cloudflare Tunnel hostname, etc.), then `npm run deploy`
> from the `edge/` dir.

---

## 3. First data — import an M3U

The engine has an empty DB at this point. The easiest way to populate
it is from your laptop (or any Tailscale device) hitting the admin
API directly over Tailscale:

```bash
# From your laptop (replace with your VPS's Tailscale IP):
curl -X POST http://100.81.208.64:8000/api/admin/import \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-m3u-provider.com/list.m3u", "label": "My Source"}'
```

Or from the VPS itself:

```bash
cd /opt/treefrogfree/engine
docker compose exec tf-engine python -m engine seed --m3u https://your-m3u-provider.com/list.m3u --label "My Source"
```

Then trigger a health cycle + KV publish:

```bash
docker compose exec tf-engine python -m engine check-once
```

The engine will:
1. Download the M3U.
2. Consolidate into unique channels.
3. Run a health check on every stream (HEAD + manifest sniff).
4. Write winners (one KV key per channel) to Cloudflare KV.
5. Render the catalog and playlist, then push them to KV under
   `catalog:channels.json` and `catalog:playlist.m3u`.

After the cycle, visit `https://free.tfplus.stream/` — the public site
reads everything from KV, no engine reachability needed.

---

## 4. Verify end-to-end

### 4.1 Engine stats

```bash
docker compose -f /opt/treefrogfree/engine/docker-compose.yml exec tf-engine python -m engine stats
```

You should see non-zero `Online channels` and `Online streams`.

### 4.2 Public site

Visit:

- `https://free.tfplus.stream/` — should show channel count + grid.
- `https://free.tfplus.stream/playlist.m3u` — should download a valid M3U
  (the URL inside each entry is `/s/<token>`, not the source URL).

### 4.3 Redirect hot path

```bash
# Pick a token from the playlist you just downloaded.
TOKEN="abc123"   # replace with a real one
curl -sI https://free.tfplus.stream/s/$TOKEN
```

You should see `HTTP/1.1 302 Found` with `Location:` pointing at the
real source URL.

### 4.4 Admin

Visit `https://admin.free.tfplus.stream/` — Cloudflare Access will ask
for an OTP. After auth, you should see live stats from the engine.

---

## 5. Optional: set the playlist's public base URL

By default, the M3U uses relative URLs (`/s/abc123`). If you want
absolute URLs (some players prefer this), set `PUBLIC_BASE_URL` in
`engine/.env` to `https://free.tfplus.stream`, then restart:

```bash
docker compose -f /opt/treefrogfree/engine/docker-compose.yml restart tf-engine
```

---

## 6. Ongoing operations

### Update the engine

```bash
cd /opt/treefrogfree
git pull
docker compose -f engine/docker-compose.yml up -d --build
```

### Update the edge

```bash
cd edge
git pull
npm install
npm run deploy
```

### Tail logs

```bash
docker compose -f /opt/treefrogfree/engine/docker-compose.yml logs -f tf-engine
```

### Manual admin actions

```bash
# Force a health check now (don't wait 30 min)
docker compose -f /opt/treefrogfree/engine/docker-compose.yml exec tf-engine \
  python -m engine check-once

# Re-render the public playlist and catalog
docker compose -f /opt/treefrogfree/engine/docker-compose.yml exec tf-engine \
  python -m engine publish

# Force-republish ALL Cloudflare KV entries
docker compose -f /opt/treefrogfree/engine/docker-compose.yml exec tf-engine \
  python -m engine publish && python ../scripts/backfill-kv.py
```

### Backups

The SQLite DB lives at `/opt/treefrogfree/engine/data/treefrog.db`.
A nightly backup via cron is a 2-line addition to your existing
crontab. Suggested:

```bash
# /etc/cron.d/treefrog-backup
0 3 * * * root sqlite3 /opt/treefrogfree/engine/data/treefrog.db ".backup /var/backups/treefrog/treefrog-$(date +\%F).db" && find /var/backups/treefrog -mtime +14 -delete
```

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Worker returns 500 on `/` | Check `wrangler tail` for the exception. Most common: `ASSETS` binding missing — make sure `edge/wrangler.toml` has `[assets] directory = "./public"`. |
| `/s/<token>` returns 410 | Token isn't in KV. Check engine logs: did `publish_redirects` run? Did the channel have any online stream? |
| `/api/channels.json` or `/playlist.m3u` returns 503 | Engine hasn't published yet. Wait for the first health cycle. KV writes are eventually consistent — they may also take ~30s to propagate to all edge POPs. |
| Engine health checks all fail | The VPS probably can't reach the source provider. Test from inside the container: `docker compose exec tf-engine curl -I https://example.com/test.m3u8`. |
| Admin UI shows "Failed to load" | Engine API not reachable from your browser. Check Tailscale is up on both ends, the engine is listening on `127.0.0.1:8000`, and the `ENGINE_API` in `edge/public/admin/index.html` matches your VPS's Tailscale IP. |
| KV writes are 403 | `CF_API_TOKEN` is wrong or doesn't have `Workers KV Storage: Edit` permission. |
