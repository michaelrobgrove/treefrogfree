# Tree Frog Plus

Premium IPTV reseller site. White-label Strong8K — customers
should never see "Strong8K" or "Gold Panel" anywhere.

**Stack:** Cloudflare Pages (static + Functions) + PayPal
Orders (one-time payments) + Gold Panel reseller API + Resend
(email).

**Billing model:** Customers pay once for a chosen term (3, 6,
or 12 months). PayPal does NOT auto-bill. The site generates
a fresh PayPal Order (payment link) whenever a renewal is
needed — the customer can trigger this from the dashboard or
the operator can email them one.

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
│   └── api/                # Pages Functions
└── wrangler.toml           # Pages project config
```

---

## Setup checklist (operator)

### 1. One-time: gold panel bouquets

In the Gold Panel admin, create 4 bouquets:

- `US`
- `US + Adult`
- `Canada`
- `Canada + Adult`

Then list them:

```
curl 'https://8k.cms-only.ru/api/api.php?action=bouquet&api_key=YOUR_KEY'
```

Copy the numeric `id` for each into `plus/wrangler.toml` (or
set as Cloudflare env vars):

```
BOUQUET_US_WO  = "66496"
BOUQUET_US_W   = "67000"
BOUQUET_CA_WO  = "67006"
BOUQUET_CA_W   = "67007"
```

Hard-coded fallbacks matching the operator's Gold Panel are
already in `functions/_lib/plans.ts`, so this step is only
required if those IDs change.

### 2. Cloudflare setup

Create the KV namespace:

```bash
wrangler kv:namespace create PLUS_KV
```

Paste the printed `id` into `plus/wrangler.toml` →
`[[kv_namespaces]]` block.

Set non-secret vars in the Cloudflare dashboard
(treefrogplus project → Settings → Environment variables), or
add them to `wrangler.toml` under `[vars]`:

```
PAYPAL_API_BASE     = "https://api-m.sandbox.paypal.com"   # live: api-m.paypal.com
PUBLIC_BASE_URL     = "https://beta.tfplus.stream"
DNS_PRIMARY         = "https://apex.tfplus.stream"
DNS_SECONDARY       = "http://comet.tfplus.stream"
RESEND_FROM_ADDRESS = "Tree Frog Plus <noreply@tfplus.stream>"
```

Set the secrets:

```bash
wrangler pages secret put PAYPAL_CLIENT_ID
wrangler pages secret put PAYPAL_CLIENT_SECRET
wrangler pages secret put PAYPAL_WEBHOOK_ID
wrangler pages secret put GOLDPANEL_API_KEY
wrangler pages secret put RESEND_API_KEY
```

### 3. PayPal webhook

In the PayPal dashboard, register a webhook URL:

```
https://beta.tfplus.stream/api/paypal-webhook
```

Subscribe to these events:
- `CHECKOUT.ORDER.APPROVED`
- `PAYMENT.CAPTURE.COMPLETED`
- `PAYMENT.CAPTURE.DENIED`
- `PAYMENT.CAPTURE.REFUNDED`

(Not needed: any `BILLING.SUBSCRIPTION.*` events — we don't
use Subscriptions anymore.)

Copy the webhook ID it gives you — that's `PAYPAL_WEBHOOK_ID`.

### 4. DNS

Point `beta.tfplus.stream` (or whatever beta subdomain you
choose) at the Pages project. Cloudflare auto-creates
`treefrogplus-beta.pages.dev`; the user's `tfplus.stream` zone
needs a CNAME in the Cloudflare DNS.

### 5. Email

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

### Initial checkout

1. Customer picks plan + bouquet on `pricing.html`.
2. `POST /api/checkout` creates a PayPal Order with the
   price from `plans.ts` and `purchase_units[0].custom_id =
   "{months}|{bouquet}"`; returns the approval URL.
3. Customer approves on PayPal → PayPal sends
   `CHECKOUT.ORDER.APPROVED`. We capture server-side.
4. `PAYMENT.CAPTURE.COMPLETED` arrives. We call Gold Panel
   `action=new&type=m3u&sub=N&pack=<bouquet_id>&...`, store
   the username + password in KV, and email the customer.
5. `thanks.html` polls `/api/checkout/status?order_id=...`
   and redirects to login once the account is ready.
6. Customer signs in with their email + the password from
   the welcome email → dashboard shows status, DNS, Xtream
   Codes block, and the "Open web player" button.
7. Web player fetches `/api/player/channels` (proxied
   through Gold Panel XC), shows the list, plays the chosen
   channel via HLS.js with apex (HTTPS) and comet (HTTP) as
   failover.

### Renewal

1. Customer clicks "Extend +3 months" on the dashboard (or
   the operator emails them a link).
2. `POST /api/account/renew` creates a fresh PayPal Order
   with `custom_id = "renew|{account_order_id}|{months}"`,
   stores it as `pending_renewal_order_id` on the account,
   and emails the customer a payment link.
3. Customer pays → `CHECKOUT.ORDER.APPROVED` → `capture` →
   `PAYMENT.CAPTURE.COMPLETED` with the renewal custom_id.
4. Webhook dispatches to the renewal handler: calls Gold
   Panel `action=renew`, updates `expires_at`, clears the
   pending pointer, sends renewal receipt.
5. Dashboard re-renders with the new expiry.

### Cancellation

The customer can click "Don't renew" on the dashboard. We
just set `cancel_at_period_end = true` locally — there's no
PayPal subscription to cancel. The Gold Panel line runs
until `expires_at` and then naturally expires (the operator
can run a daily cron to disable expired accounts). Any
future manual renewal clears the cancel flag.

### Refund

If a capture is refunded (in PayPal's dashboard), PayPal
sends `PAYMENT.CAPTURE.REFUNDED`. We mark the account
`refunded` and call Gold Panel `device_status disable` to
shut the line off.

---

## Open issues

- **Gold Panel `createM3U` response shape.** The PDF docs
  truncate the response — it shows
  `{status, user_id, notes, country, message}` but not
  `username` / `password`. The real response almost
  certainly includes them (otherwise renewals and
  `device_info` calls are impossible), but the webhook
  handler logs the full payload and fails loudly if they're
  missing. Verify on the first real account creation.
- **Password reset flow.** Not implemented in v1. Customer
  replies to the welcome email and we reset manually.
- **Site password change.** Not implemented in v1. The
  password in the welcome email is the customer's site
  password for life, unless we change it for them.
- **TiviMate premium APK.** Setup page has a "coming soon"
  card. Not implemented.
- **Auto-disable of expired lines.** No cron yet. The
  dashboard flags `cancel_at_period_end` accounts, but the
  Gold Panel line stays live until the operator (or a
  future cron) calls `device_status disable`. The customer
  won't be able to renew an expired line, so it self-
  heals as long as we add the cron before any churn.
- **Reminder email before expiry.** Operator can manually
  email a renewal link via `POST /api/account/renew` from
  the dashboard; an automated "your plan expires in 7 days,
  here's a one-click link" cron is a future nice-to-have.
