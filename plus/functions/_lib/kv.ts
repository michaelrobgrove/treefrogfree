/** Typed helpers for the PLUS_KV namespace.
 *
 *  KV is a flat key-value store, so we serialize JSON into a few
 *  well-known keys. The schema lives here so all the functions
 *  agree on the shape.
 *
 *  Keys:
 *    account:{paypal_subscription_id}     -> Account
 *    account:by_email:{email}             -> { sub_id }
 *    account:by_panel:{panel_user_id}     -> { sub_id }
 *    session:{token}                      -> { sub_id, exp }   (TTL = exp-now)
 *    event:{paypal_event_id}              -> "1"               (TTL = 30d)
 */

import type { PlanMonths, BouquetId } from "./plans";

export interface Account {
    /** PayPal subscription id (e.g. "I-XXXX"). */
    subscription_id: string;
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
    /** Plan length in months. */
    plan_months: PlanMonths;
    /** Bouquet key. */
    bouquet: BouquetId;
    /** Gold Panel bouquet id at time of activation. */
    panel_bouquet_id: string;
    /** Subscription start (ISO). */
    created_at: string;
    /** When the Gold Panel account expires (ISO). */
    expires_at: string | null;
    /** Next PayPal billing date (ISO). */
    next_billing_at: string | null;
    /** Lifecycle status. */
    status:
        | "pending"            // sub created, first payment not cleared
        | "active"             // active, paid, Gold Panel account ready
        | "cancel_at_period_end" // user cancelled, runs until expires_at
        | "expired"            // PayPal reported EXPIRED, Gold Panel disabled
        | "payment_failed";    // last payment failed
    /** Set to true when the customer clicks "Cancel" on the dashboard. */
    cancel_at_period_end: boolean;
}

export interface Session {
    sub_id: string;
    exp: number; // unix seconds
}

export function accountKey(subId: string): string {
    return `account:${subId}`;
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

/** Fetch an account by subscription id; returns null if missing. */
export async function getAccountBySub(
    kv: KVNamespace,
    subId: string,
): Promise<Account | null> {
    const raw = await kv.get(accountKey(subId));
    return raw ? (JSON.parse(raw) as Account) : null;
}

export async function getAccountByEmail(
    kv: KVNamespace,
    email: string,
): Promise<Account | null> {
    const idx = await kv.get(accountByEmailKey(email));
    if (!idx) return null;
    const { sub_id } = JSON.parse(idx) as { sub_id: string };
    return getAccountBySub(kv, sub_id);
}

export async function getAccountByPanel(
    kv: KVNamespace,
    panelUserId: string,
): Promise<Account | null> {
    const idx = await kv.get(accountByPanelKey(panelUserId));
    if (!idx) return null;
    const { sub_id } = JSON.parse(idx) as { sub_id: string };
    return getAccountBySub(kv, sub_id);
}

export async function putAccount(kv: KVNamespace, acct: Account): Promise<void> {
    await kv.put(accountKey(acct.subscription_id), JSON.stringify(acct));
    await kv.put(accountByEmailKey(acct.email), JSON.stringify({ sub_id: acct.subscription_id }));
    if (acct.panel_user_id) {
        await kv.put(
            accountByPanelKey(acct.panel_user_id),
            JSON.stringify({ sub_id: acct.subscription_id }),
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
