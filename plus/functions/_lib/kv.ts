/** Typed helpers for the PLUS_KV namespace.
 *
 *  KV is a flat key-value store, so we serialize JSON into a few
 *  well-known keys. The schema lives here so all the functions
 *  agree on the shape.
 *
 *  Keys:
 *    account:{paypal_order_id}              -> Account
 *    account:by_email:{email}               -> { order_id }
 *    account:by_panel:{panel_user_id}       -> { order_id }
 *    session:{token}                        -> { order_id, exp }   (TTL = exp-now)
 *    event:{paypal_event_id}                -> "1"                 (TTL = 30d)
 *
 *  We key accounts by the PayPal Order ID of the FIRST payment
 *  (the initial checkout). The dashboard polls status against
 *  that same ID, and the account record carries the most recent
 *  capture id for receipts/refunds. Subsequent renewals create
 *  their own Order IDs — we record them in `renewal_order_ids`
 *  for history, and a "pending renewal" pointer so the webhook
 *  knows which account to extend.
 */

import type { PlanMonths, BouquetId } from "./plans";

/** Plan months for an initial signup (3, 6, or 12). */
export type InitialPlanMonths = 3 | 6 | 12;
/** Plan months for a renewal (1, 3, 6, or 12 — the dashboard
 *  lets customers add a single month, but new signups are
 *  always 3+). */
export type RenewalPlanMonths = 1 | 3 | 6 | 12;

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
    /** Customer's email (lowercased). */
    email: string;
    /** Gold Panel `user_id` once activated. Null until then. */
    panel_user_id: string | null;
    /** Gold Panel M3U username. Null until activated. */
    panel_username: string | null;
    /** Gold Panel M3U password (cleartext — required for renew). */
    panel_password: string | null;
    /** Our 1-connection site login password (cleartext — used in
     *  the welcome email so the customer can sign in for the first
     *  time, then compared as a hash thereafter). */
    site_password: string;
    /** PBKDF2 hash of site_password, used for auth-login. */
    password_auth: { salt: string; hash: string; iterations: number };
    /** Plan length the customer is on. */
    plan_months: PlanMonths;
    /** Bouquet key. */
    bouquet: BouquetId;
    /** Gold Panel bouquet id at time of activation. */
    panel_bouquet_id: string;
    /** Account start (ISO). */
    created_at: string;
    /** When the Gold Panel account expires (ISO). */
    expires_at: string | null;
    /** Lifecycle status. */
    status:
        | "pending"            // order created, payment not yet captured
        | "active"             // paid, Gold Panel account ready
        | "cancel_at_period_end" // user marked as not renewing, runs until expires_at
        | "expired"            // Gold Panel line disabled (auto-canceled at expiry)
        | "refunded"           // capture was refunded
        | "payment_failed";    // last payment failed/denied
    /** Set to true when the customer clicks "Cancel" on the dashboard.
     *  The Gold Panel account keeps running until `expires_at`, but
     *  no renewal payment link will be generated automatically. */
    cancel_at_period_end: boolean;
}

export interface Session {
    order_id: string;
    exp: number; // unix seconds
}

export function accountKey(orderId: string): string {
    return `account:${orderId}`;
}

export function accountByEmailKey(email: string): string {
    return `account:by_email:${email.toLowerCase()}`;
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
    if (acct.panel_user_id) {
        await kv.put(
            accountByPanelKey(acct.panel_user_id),
            JSON.stringify({ order_id: acct.paypal_order_id }),
        );
    }
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
