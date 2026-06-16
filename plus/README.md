# Tree Frog Plus

Premium IPTV reseller site. White-label Strong8K — customers
should never see "Strong8K" or "Gold Panel" anywhere.

**Stack:** Cloudflare Pages (static + Functions) + PayPal
Orders (one-time payments) + Gold Panel reseller API + Resend
(email).

**Billing model:** Customers pay once for a chosen term (1, 3,
6, or 12 months). PayPal does NOT auto-bill. The site generates
a fresh PayPal Order (payment link) whenever a renewal is
needed — the customer can trigger this from the dashboard or
the operator can email them one.

**Auth model:** The site signs customers in with their **Gold
Panel username + password** — the same credentials they use in
their IPTV app. New customers get a Gold Panel account created
on first payment; the welcome email contains the username and
password, and those creds double as their site login. Existing
Gold Panel customers can sign in immediately with the creds
they already have — no PayPal history required; the site
verifies them against Gold Panel's `device_info` endpoint on
first login and creates a local record on the fly.

---

## File layout

```
plus/
├── index.html              # Landing
├── pricing.html            # 4 plans × 4 bouquets + signup form
├── setup.html              # Android / Fire Stick / Roku
├── login.html              # Site sign-in (Gold Panel creds)
├── dashboard.html          # Account mgmt + profile editor
├── thanks.html             # Polls for activation
├── assets/                 # CSS, JS, logo
├── functions/
│   ├── _lib/               # Shared modules (goldpanel, paypal, session, …)
│   └── api/                # Pages Functions
└── wrangler.toml           # Pages project config
```

---

## Plans and bouquets

Pricing is a 4×4 matrix: 4 plan lengths × 4 bouquets.

| Months | Price | Per month |
| ------ | ----- | --------- |
| 1      | $15   | $15       |
| 3      | $36   | $12       |
| 6      | $60   | $10       |
| 12     | $96   | $8        |

Bouquets (all pre-created in the operator's Gold Panel):

| Key      | Label          | Gold Panel ID |
| -------- | -------------- | ------------- |
| `us_wo`  | US             | 66496         |
| `us_w`   | US + Adult     | 67000         |
| `ca_wo`  | Canada         | 67006         |
| `ca_w`   | Canada + Adult | 67007         |

Prices and bouquet IDs live in `functions/_lib/plans.ts`.
Hard-coded fallbacks matching the operator's Gold Panel are
already there; the Cloudflare env vars `BOUQUET_US_WO`,
`BOUQUET_US_W`, `BOUQUET_CA_WO`, `BOUQUET_CA_W` override
them at runtime.

---

## Setup checklist (operator)

### 1. One-time: gold panel bouquets

The 4 bouquet IDs above are already wired in. If the operator
ever renames or recreates them, list the new ones with:

```
curl 'https://8k.cms-only.ru/api/api.php?action=bouquet&api_key=YOUR_KEY'
```

…and update `functions/_lib/plans.ts` (or set the
`BOUQUET_*` env vars).

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

### Initial checkout (new customer)

1. Customer visits `pricing.html`, picks a plan + bouquet, and
   fills in the **Your details** form: name (required), email
   (required), and any of discord / telegram / reddit
   usernames (optional — these are stored on the account and
   may be used for account-update notifications, future
   community channels, etc.).
2. `POST /api/checkout` validates the form (name non-empty,
   valid email, no existing active account with that email),
   creates a PayPal Order with the price from `plans.ts` and
   `purchase_units[0].custom_id = "{months}|{bouquet}"`, and
   stashes the form fields at `checkout:pending:{order_id}`
   in KV. Returns the approval URL.
3. Customer approves on PayPal → PayPal sends
   `CHECKOUT.ORDER.APPROVED`. We capture server-side.
4. `PAYMENT.CAPTURE.COMPLETED` arrives. The webhook reads the
   form fields from `checkout:pending:{order_id}`, calls Gold
   Panel `action=new&type=m3u&sub=N&pack=<bouquet_id>&...`,
   stores the Gold Panel `username` + `password` on the
   account (and a local PBKDF2 hash of that password for
   site login), drops the pending-intent KV key, and emails
   the customer their credentials.
5. `thanks.html` polls `/api/checkout/status?order_id=...`
   and redirects to login once the account is ready.
6. Customer signs in on `login.html` with their Gold Panel
   username + password (the same creds in the welcome email
   and the same creds they use in TiviMate / Sparkle / etc.)
   → `dashboard.html` shows status, DNS, Xtream Codes block,
   and the "Open web player" button.
7. Web player fetches `/api/player/channels` (proxied
   through Gold Panel XC), shows the list, plays the chosen
   channel via HLS.js with apex (HTTPS) and comet (HTTP) as
   failover.

### Initial sign-in (existing Gold Panel customer)

1. The customer already has Gold Panel creds from a previous
   purchase (operator, retail, etc.). They go to
   `login.html` and enter the same username + password.
2. `POST /api/auth/login` first checks the local
   `password_auth` PBKDF2 hash. On a miss, it falls back to
   Gold Panel `action=device_info&username=...&password=...`.
3. If Gold Panel returns a matching line, the site creates a
   local account record on the fly with a synthetic order ID
   of `GP-{panel_user_id}` (no PayPal history), stores the
   panel creds and a fresh hash, mints a session cookie, and
   redirects to the dashboard. The customer is now a fully
   provisioned site user.
4. From the dashboard, they can edit their name / email /
   contact handles, view the XC login info for their IPTV
   app, and (if their Gold Panel line is nearing expiry) hit
   "Extend +N months" to pay a fresh one-time renewal via
   PayPal.

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

### Profile edit

From the dashboard, the customer can hit **Edit** in the
account block to update:

- `name` (required, 1–100 chars)
- `email` (required, valid format, unique among active
  accounts — the lookup index at `account:by_email:*` is
  updated in place)
- `discord` / `telegram` / `reddit` (optional, each ≤ 64
  chars; empty string clears it)

Submitted via `PUT /api/account/contact` (POST also
accepted). The Gold Panel creds, plan, bouquet, and
billing state are never touched by this endpoint.

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

### Contact Support

Customers can submit a contact form on the landing page (`index.html`). The form
collects name, email, subject, and message, and posts to `POST /api/account/contact`
with `kind: "contact"`. The handler validates the input and sends an email to the
operator's support address. The submit button is disabled during submission to
prevent duplicate submissions.

### Renewal Nudges

A cron endpoint (`GET /api/cron/renewal-nudges`) sends renewal reminder emails to
accounts whose lines are expiring soon. The endpoint requires an `X-Cron-Key` header
matching the `CRON_SECRET` environment variable for authentication.

The cron checks each account and sends emails at three thresholds:
- **7 days before expiry**: First reminder
- **3 days before expiry**: Second reminder  
- **On expiry day**: Final reminder

Set the cron in Cloudflare with a schedule like `0 9 * * *` (daily at 9 AM UTC).
Add the secret:

```bash
wrangler pages secret put CRON_SECRET
```

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
- **Password reset flow.** Not implemented in v1. Because
  the site password IS the Gold Panel password, a reset
  means regenerating the Gold Panel line — which we don't
  support programmatically. Customer replies to the welcome
  email and the operator resets manually.
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
- **TiviMate/Sparkle disclaimer.** The setup page notes
  that any payment required by TiviMate or Sparkle is from
  the developers of those apps, not Tree Frog Plus. The
  host's own unlocked APK (downloader code `4527163`) is
  hosted on Google Drive; that URL and the
  "free for subscribers" framing are baked into setup.html.