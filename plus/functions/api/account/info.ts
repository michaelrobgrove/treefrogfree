/** GET /api/account/info
 *
 *  Returns the dashboard data for the currently signed-in
 *  customer. 401 if no valid session. The customer's
 *  Gold Panel username/password are returned in cleartext —
 *  the dashboard needs them to show in the Xtream Codes
 *  block. The session cookie is HttpOnly, so the only way
 *  this endpoint is reachable is from our own dashboard.
 *
 *  Returns: email, name, contact handles, plan, bouquet,
 *  status, expires_at, days_until_expiry, last login
 *  timestamp, last PayPal charge timestamp + derived
 *  "next charge" date, panel login (username + password),
 *  DNS endpoints, and the pending_renewal pointer if any. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { decryptPanelPassword } = await import("../../_lib/kv");
    const plans = await import("../../_lib/plans");
    const BOUQUET_LABELS = plans.BOUQUET_LABELS;

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;

    const dnsPrimary   = String((globalThis as any).DNS_PRIMARY   || "https://apex.tfplus.stream");
    const dnsSecondary = String((globalThis as any).DNS_SECONDARY || "http://comet.tfplus.stream");

    // Days until expiry (negative = already past). Used by the
    // dashboard to surface a renew CTA.
    let daysUntilExpiry: number | null = null;
    if (acct.expires_at) {
        const ms = Date.parse(acct.expires_at) - Date.now();
        daysUntilExpiry = Math.ceil(ms / (1000 * 60 * 60 * 24));
    }

    // Decrypt the panel password for the dashboard. The
    // cleartext only lives in this response body and in the
    // local variable — never logged, never persisted.
    const cleartextPassword = await decryptPanelPassword(ctx.env, acct);

    // Derive the "next charge" date from the last PayPal
    // capture. Only meaningful for site-signup customers —
    // GP-only accounts have last_paypal_charge_at = null and
    // we hide the row.
    let nextChargeAt: string | null = null;
    if (acct.last_paypal_charge_at && acct.plan_months) {
        const d = new Date(acct.last_paypal_charge_at);
        if (!isNaN(d.getTime())) {
            d.setUTCMonth(d.getUTCMonth() + Number(acct.plan_months));
            nextChargeAt = d.toISOString();
        }
    }

    return json({
        name: acct.name || "",
        email: acct.email || "",
        contact: acct.contact || { discord: null, telegram: null, reddit: null },
        paypal_order_id: acct.paypal_order_id,
        plan_months: acct.plan_months,
        bouquet: acct.bouquet,
        bouquet_label: BOUQUET_LABELS[acct.bouquet] || acct.bouquet,
        status: acct.status,
        cancel_at_period_end: acct.cancel_at_period_end,
        created_at: acct.created_at,
        expires_at: acct.expires_at,
        days_until_expiry: daysUntilExpiry,
        last_login_at: acct.last_login_at,
        last_paypal_charge_at: acct.last_paypal_charge_at,
        next_charge_at: nextChargeAt,
        pending_renewal: acct.pending_renewal_order_id
            ? { order_id: acct.pending_renewal_order_id, months: acct.pending_renewal_months }
            : null,
        // Gold Panel creds — also the site login. Returned
        // in cleartext because the dashboard needs to show
        // them in the XC block and copy buttons. Only safe
        // because the session cookie is HttpOnly.
        username: acct.panel_username || "",
        password: cleartextPassword || "",
        xc_server: dnsPrimary,
        dns_primary: dnsPrimary,
        dns_secondary: dnsSecondary,
    });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
