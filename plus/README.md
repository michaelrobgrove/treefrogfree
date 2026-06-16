# Tree Frog Plus

Premium IPTV reseller site. White-label Strong8K — customers
should never see "Strong8K" or "Gold Panel" anywhere.

**Stack:** Cloudflare Pages (static + Functions) + PayPal
Subscriptions + Gold Panel reseller API + Resend (email).

---

## File layout

```
plus/
├── index.html              # Landing
├── pricing.html            # 3 plans × 4 bouquets
├── setup.html              # Android / Fire Stick / Roku
├── login.html              # Site sign-in
├── dashboard.html          # Account mgmt
├── thanks.html             # Polls for activation
├── assets/                 # CSS, JS, logo
├── functions/
│   ├── _lib/               # Shared modules (goldpanel, paypal, session, …)
│   └── api/                # Pages Functions (POST /api/checkout, /api/paypal-webhook, …)
└── wrangler.toml           # Pages project config
```

---

## Setup checklist (operator)

### 1. One-time: paypal products

In PayPal's dashboard (sandbox first, then live), create **12
subscription products** — one per (plan, bouquet) pair:

| Plan      | US | US+ | US/CA | US/CA+ |
|-----------|----|----|-------|--------|
| 3 months  | ✓  | ✓  | ✓     | ✓      |
| 6 months  | ✓  | ✓  | ✓     | ✓      |
| 12 months | ✓  | ✓  | ✓     | ✓      |

Each product becomes a PayPal `plan_id` (`P-XXXX`). Paste those
into `plus/wrangler.toml` (or set them as Cloudflare env vars
later):

```
PAYPAL_PLAN_3M_US          = "P-..."
PAYPAL_PLAN_3M_US_ADULT    = "P-..."
PAYPAL_PLAN_3M_US_CA       = "P-..."
PAYPAL_PLAN_3M_US_CA_ADULT = "P-..."
PAYPAL_PLAN_6M_US          = "P-..."
...
```

The webhook reads the `plan_id` from the subscription and maps
back to the (months, bouquet) pair automatically — so the
customer's exact purchase survives the round-trip.

### 2. One-time: gold panel bouquets

In the Gold Panel admin, create 4 bouquets named:

- `US`
- `US + Adult`
- `US & Canada`
- `US & Canada + Adult`

Then list them:

```
curl 'https://8k.cms-only.ru/api/api.php?action=bouquet&api_key=YOUR_KEY'
```

Copy the numeric `id` for each into `plus/wrangler.toml`:

```
BOUQUET_US          = "132"
BOUQUET_US_ADULT    = "152"
BOUQUET_US_CA       = "162"
BOUQUET_US_CA_ADULT = "172"
```

### 3. Cloudflare setup

Create the KV namespace:

```bash
wrangler kv:namespace create PLUS_KV
```

Paste the printed `id` into `plus/wrangler.toml` →
`[[kv_namespaces]]` block.

Set non-secret vars in the Cloudflare dashboard
(treefrogplus project → Settings → Environment variables), or
add them to `wrangler.toml` under `[vars]`. The defaults are
in `wrangler.toml` (commented out for safety):

```
PAYPAL_API_BASE       = "https://api-m.sandbox.paypal.com"
PUBLIC_BASE_URL       = "https://beta.tfplus.stream"
DNS_PRIMARY           = "https://apex.tfplus.stream"
DNS_SECONDARY         = "http://comet.tfplus.stream"
RESEND_FROM_ADDRESS   = "Tree Frog Plus <noreply@tfplus.stream>"
```

Set the secrets:

```bash
wrangler pages secret put PAYPAL_CLIENT_ID
wrangler pages secret put PAYPAL_CLIENT_SECRET
wrangler pages secret put PAYPAL_WEBHOOK_ID
wrangler pages secret put GOLDPANEL_API_KEY
wrangler pages secret put RESEND_API_KEY
```

### 4. PayPal webhook

In the PayPal dashboard, register a webhook URL:

```
https://beta.tfplus.stream/api/paypal-webhook
```

Subscribe to these events:
- `BILLING.SUBSCRIPTION.CREATED`
- `BILLING.SUBSCRIPTION.ACTIVATED`
- `BILLING.SUBSCRIPTION.CANCELLED`
- `BILLING.SUBSCRIPTION.EXPIRED`
- `BILLING.SUBSCRIPTION.SUSPENDED`
- `BILLING.SUBSCRIPTION.PAYMENT.FAILED`
- `PAYMENT.SALE.COMPLETED`

Copy the webhook ID it gives you — that's `PAYPAL_WEBHOOK_ID`.

### 5. DNS

Point `beta.tfplus.stream` (or whatever beta subdomain you
choose) at the Pages project. Cloudflare auto-creates
`treefrogplus-beta.pages.dev`; the user's `tfplus.stream` zone
needs a CNAME in the Cloudflare DNS.

### 6. Email

Sign up at [resend.com](https://resend.com), add a domain,
verify it, and paste the API key. Set `RESEND_FROM_ADDRESS`
to a verified sender (e.g. `Tree Frog Plus <noreply@tfplus.stream>`).

---

## Deploy

```bash
# Local: build + serve
cd plus
wrangler pages dev . --port 8788

# Production: deploy beta
wrangler pages deploy . --branch beta --project-name treefrogplus

# When ready to ship:
#   1. flip Pages project's production_branch to "beta" via the
#      Cloudflare API (same as the free site did earlier), OR
#   2. merge beta → production and run wrangler pages deploy on
#      production
```

---

## Flow

1. Customer picks plan + bouquet on `pricing.html`.
2. `POST /api/checkout` creates a PayPal subscription; the
   response is the approval URL the page redirects to.
3. Customer approves on PayPal → PayPal sends
   `BILLING.SUBSCRIPTION.CREATED` (we record the subscription
   in `pending` status), then `BILLING.SUBSCRIPTION.ACTIVATED`
   once the first payment clears.
4. ACTIVATED handler calls Gold Panel
   `action=new&type=m3u&sub=N&pack=<bouquet_id>&...`, stores
   the username + password in KV, and emails the customer.
5. `thanks.html` polls `/api/checkout/status?sub_id=...` and
   redirects to login once the account is ready.
6. Customer signs in with their email + the password from the
   welcome email → dashboard shows status, DNS, Xtream Codes
   block, and the "Open web player" button.
7. Web player fetches `/api/player/channels` (proxied through
   Gold Panel XC), shows the list, plays the chosen channel
   via HLS.js with apex (HTTPS) and comet (HTTP) as failover.

---

## Open issues

- **Gold Panel `createM3U` response shape.** The PDF docs
  truncate the response — it shows
  `{status, user_id, notes, country, message}` but not
  `username` / `password`. The real response almost certainly
  includes them (otherwise renewals and `device_info` calls
  are impossible), but the webhook handler logs the full
  payload and fails loudly if they're missing. Verify on
  the first real account creation.
- **Password reset flow.** Not implemented in v1. Customer
  replies to the welcome email and we reset manually.
- **Site password change.** Not implemented in v1. The
  password in the welcome email is the customer's site
  password for life, unless we change it for them.
- **TiviMate premium APK.** Setup page has a "coming soon"
  card. Not implemented.
