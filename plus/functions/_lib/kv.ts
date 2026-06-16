/** Typed helpers for the PLUS_KV namespace.
 *
 *  KV is a flat key-value store, so we serialize JSON into a few
 *  well-known keys. The schema lives here so all the functions
 *  agree on the shape.
 *
 *  Keys:
 *    account:{paypal_order_id}              -> Account
 *    account:by_email:{email}               -> { order_id }
 *    account:by_panel_user:{panel_username} -> { order_id }
 *    account:by_panel:{panel_user_id}       -> { order_id }
 *    session:{token}                        -> { order_id, exp }   (TTL = exp-now)
 *    event:{paypal_event_id}                -> "1"                 (TTL = 30d)
 *    checkout:pending:{paypal_order_id}     -> { name, email, contact_handles, ... }
 *
 *  We key accounts by the PayPal Order ID of the FIRST payment
 *  (the initial checkout). The dashboard polls status against
 *  that same ID, and the account record carries the most recent
 *  capture id for receipts/refunds. Subsequent renewals create
 *  their own Order IDs — we record them in `renewal_order_ids`
 *  for history, and a "pending renewal" pointer so the webhook
 *  knows which account to extend.
 *
 *  AUTH MODEL: The Gold Panel username + password ARE the site
 *  login. There is no separate site password. We keep a PBKDF2
 *  hash of the Gold Panel password in `password_auth` for fast
 *  login verification. The Gold Panel password is also stored
 *  encrypted-at-rest in `panel_password_ct` (AES-GCM, key in
 *  the `PANEL_PASSWORD_ENC_KEY` env) because Gold Panel
 *  `action=renew` requires the cleartext at request time.
 *
 *  Site accounts are indexed by:
 *    - `paypal_order_id`   (the initial Order ID)
 *    - `email`             (for account recovery / re-send)
 *    - `panel_username`    (for sign-in lookup)
 *    - `panel_user_id`     (the Gold Panel numeric user_id)
 */

import type { PlanMonths, BouquetId } from "./plans";

/** Plan months for an initial signup (1, 3, 6, or 12). */
export type InitialPlanMonths = PlanMonths;
/** Plan months for a renewal (1, 3, 6, or 12). */
export type RenewalPlanMonths = PlanMonths;

/** Optional contact handles for outreach. The site doesn't
 *  validate that these resolve on Discord / Telegram / Reddit —
 *  the customer is responsible for spelling them right. Stored
 *  exactly as the customer typed them, minus leading @ on
 *  reddit/telegram if present. */
export interface ContactHandles {
    discord:  string | null;
    telegram: string | null;
    reddit:   string | null;
}

export interface Account {
    /** PayPal Order ID (e.g. "O-XXXX") of the initial checkout.
     *  This is the KV primary key — it never changes for the
     *  lifetime of the account. */
    paypal_order_id: string;
    /** Most recent PayPal capture id (for receipts/refunds). */
    latest_capture_id: string | null;
    /** History of all order IDs ever paid against this account
     *  (initial + renewals). */
    renewal_order_ids: string[];
    /** While a renewal payment link is outstanding (created via
     *  /api/account/renew, awaiting webhook), we record the
     *  new order id here. Cleared on CAPTURE.COMPLETED. */
    pending_renewal_order_id: string | null;
    /** Months that will be added when the pending renewal
     *  captures. Null if no renewal is in flight. */
    pending_renewal_months: RenewalPlanMonths | null;

    /** Customer's real name (for receipts, support). */
    name: string;
    /** Customer's email (for receipts, support, and account
     *  recovery). Lowercased. */
    email: string;
    /** Optional contact handles. At least one is null; customers
     *  may fill in zero, one, two, or all three. */
    contact: ContactHandles;

    /** Gold Panel `user_id` once activated. Null until then. */
    panel_user_id: string | null;
    /** Gold Panel M3U username. Null until activated. The same
     *  value is used as the site login username. */
    panel_username: string | null;
    /** Gold Panel M3U password, encrypted at rest with AES-GCM
     *  using the `PANEL_PASSWORD_ENC_KEY` env var. Required for
     *  renew calls and the welcome email. Also the site login
     *  password (cleartext form). Decrypt via
     *  `decryptPanelPassword(env, acct)`. */
    panel_password_ct: string | null;
    /** PBKDF2 hash of the cleartext panel password — used by
     *  /api/auth/login to verify the site login without
     *  round-tripping to Gold Panel on every request. */
    password_auth: { salt: string; hash: string; iterations: number } | null;

    /** Plan length the customer is on. */
    plan_months: PlanMonths;
    /** Bouquet key. */
    bouquet: BouquetId;
    /** Gold Panel bouquet id at time of activation. */
    panel_bouquet_id: string;
    /** Account start (ISO). */
    created_at: string;
    /** When the Gold Panel account expires (ISO). Refreshed
     *  on sign-in if `last_login_at` is more than 30 days
     *  stale. */
    expires_at: string | null;
    /** Last successful sign-in (ISO). Null until first
     *  sign-in. Drives the 30-day expiry-refresh heuristic
     *  in /api/auth/login. */
    last_login_at: string | null;
    /** Timestamp of the most recent PayPal capture on this
     *  account (ISO). Null for GP-only customers who never
     *  paid through the site, and for the brief window
     *  between the initial checkout and the first
     *  CAPTURE.COMPLETED webhook. The dashboard computes
     *  "Next charge" as `last_paypal_charge_at + plan_months`
     *  and hides the row when this is null. */
    last_paypal_charge_at: string | null;

    /** Lifecycle status. */
    status:
        | "pending"             // order created, payment not yet captured
        | "active"              // paid, Gold Panel account ready
        | "cancel_at_period_end" // user marked as not renewing, runs until expires_at
        | "expired"             // Gold Panel line disabled (auto-canceled at expiry)
        | "refunded"            // capture was refunded
        | "payment_failed";     // last payment failed/denied
    /** Set to true when the customer clicks "Don't renew" on
     *  the dashboard. The Gold Panel account keeps running
     *  until `expires_at`, but no renewal payment link will
     *  be generated automatically. */
    cancel_at_period_end: boolean;
}

export interface Session {
    order_id: string;
    exp: number; // unix seconds
}

/** Form fields collected on /api/checkout and stashed in
 *  `checkout:pending:{order_id}` until the webhook fires. */
export interface CheckoutIntent {
    name: string;
    email: string;
    contact: ContactHandles;
    created_at: string;
}

export function accountKey(orderId: string): string {
    return `account:${orderId}`;
}

export function accountByEmailKey(email: string): string {
    return `account:by_email:${email.toLowerCase()}`;
}

export function accountByPanelUserKey(panelUsername: string): string {
    // Lowercased so login is case-insensitive on the username.
    return `account:by_panel_user:${panelUsername.toLowerCase()}`;
}

export function accountByPanelKey(panelUserId: string): string {
    return `account:by_panel:${panelUserId}`;
}

export function sessionKey(token: string): string {
    return `session:${token}`;
}

export function eventKey(eventId: string): string {
    return `event:${eventId}`;
}

export function checkoutPendingKey(orderId: string): string {
    return `checkout:pending:${orderId}`;
}

/** Fetch an account by its PayPal Order ID; returns null if missing. */
export async function getAccountBySub(
    kv: KVNamespace,
    orderId: string,
): Promise<Account | null> {
    const raw = await kv.get(accountKey(orderId));
    return raw ? (JSON.parse(raw) as Account) : null;
}

export async function getAccountByEmail(
    kv: KVNamespace,
    email: string,
): Promise<Account | null> {
    const idx = await kv.get(accountByEmailKey(email));
    if (!idx) return null;
    const { order_id } = JSON.parse(idx) as { order_id: string };
    return getAccountBySub(kv, order_id);
}

export async function getAccountByPanelUsername(
    kv: KVNamespace,
    panelUsername: string,
): Promise<Account | null> {
    const idx = await kv.get(accountByPanelUserKey(panelUsername));
    if (!idx) return null;
    const { order_id } = JSON.parse(idx) as { order_id: string };
    return getAccountBySub(kv, order_id);
}

export async function getAccountByPanel(
    kv: KVNamespace,
    panelUserId: string,
): Promise<Account | null> {
    const idx = await kv.get(accountByPanelKey(panelUserId));
    if (!idx) return null;
    const { order_id } = JSON.parse(idx) as { order_id: string };
    return getAccountBySub(kv, order_id);
}

export async function putAccount(kv: KVNamespace, acct: Account): Promise<void> {
    await kv.put(accountKey(acct.paypal_order_id), JSON.stringify(acct));
    await kv.put(accountByEmailKey(acct.email), JSON.stringify({ order_id: acct.paypal_order_id }));
    if (acct.panel_username) {
        await kv.put(
            accountByPanelUserKey(acct.panel_username),
            JSON.stringify({ order_id: acct.paypal_order_id }),
        );
    }
    if (acct.panel_user_id) {
        await kv.put(
            accountByPanelKey(acct.panel_user_id),
            JSON.stringify({ order_id: acct.paypal_order_id }),
        );
    }
}

export async function putCheckoutIntent(
    kv: KVNamespace,
    orderId: string,
    intent: CheckoutIntent,
): Promise<void> {
    // Pending intents TTL out after 24h — far longer than any
    // sane PayPal Order can stay open. The webhook deletes it
    // explicitly once consumed.
    await kv.put(checkoutPendingKey(orderId), JSON.stringify(intent), {
        expirationTtl: 60 * 60 * 24,
    });
}

export async function getCheckoutIntent(
    kv: KVNamespace,
    orderId: string,
): Promise<CheckoutIntent | null> {
    const raw = await kv.get(checkoutPendingKey(orderId));
    return raw ? (JSON.parse(raw) as CheckoutIntent) : null;
}

export async function deleteCheckoutIntent(
    kv: KVNamespace,
    orderId: string,
): Promise<void> {
    await kv.delete(checkoutPendingKey(orderId));
}

/** Returns true if this is a brand-new event id (and we should
 *  process it). Returns false if we've already seen it. */
export async function claimEventId(kv: KVNamespace, eventId: string): Promise<boolean> {
    const k = eventKey(eventId);
    const existing = await kv.get(k);
    if (existing) return false;
    await kv.put(k, "1", { expirationTtl: 60 * 60 * 24 * 30 });
    return true;
}

/** Decrypts `acct.panel_password_ct` using the env's
 *  `PANEL_PASSWORD_ENC_KEY`. Returns null if the account
 *  has no encrypted password yet (not provisioned), the
 *  key is missing/invalid, or the ciphertext is tampered.
 *  Callers MUST treat null as "no cleartext available" and
 *  fall back to re-asking Gold Panel (e.g. on a fresh
 *  sign-in) rather than crashing. */
export async function decryptPanelPassword(
    env: Record<string, unknown>,
    acct: Pick<Account, "panel_password_ct">,
): Promise<string | null> {
    if (!acct.panel_password_ct) return null;
    const { decryptSecret } = await import("./crypto");
    return decryptSecret(env, acct.panel_password_ct);
}
