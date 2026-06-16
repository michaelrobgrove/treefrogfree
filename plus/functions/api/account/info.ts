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
 *  DNS endpoints, pending_renewal pointer, and a
 *  `is_custom_bouquet` flag that drives the renew-menu
 *  show/hide on the dashboard.
 *
 *  Bouquet matching: site-signup customers have a standard
 *  bouquet (one of our 4) and can self-renew. GP-only
 *  customers default to a "custom" bouquet since the Gold
 *  Panel device_info API doesn't expose the line's
 *  bouquet id, and the operator has to set it by hand
 *  (or via a future admin endpoint) before self-serve
 *  renewal unlocks. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { decryptPanelPassword } = await import("../../_lib/kv");
    const plans = await import("../../_lib/plans");
    const {
        BOUQUET_LABELS,
        isStandardBouquet,
        bouquetDisplayLabel,
    } = plans;

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

    // Resolve the bouquet. Site-signup customers are always
    // on a standard bouquet (one of our 4). GP-only
    // customers default to "us_wo" at first sign-in; if
    // their actual line is on a different / non-standard
    // bouquet, the operator must set acct.bouquet and
    // acct.panel_bouquet_id in KV. Until then, we treat the
    // account as "custom" and the dashboard hides the
    // self-serve renew menu.
    const isCustom = !isStandardBouquet(acct.bouquet) || !acct.panel_bouquet_id;
    const bouquetKey: string = isCustom ? "custom" : acct.bouquet;
    const bouquetLabel = isCustom
        ? bouquetDisplayLabel("custom", acct.panel_bouquet_id || null)
        : BOUQUET_LABELS[acct.bouquet];
    const canSelfRenew = !isCustom && acct.status !== "refunded" && acct.status !== "expired";

    return json({
        name: acct.name || "",
        email: acct.email || "",
        contact: acct.contact || { discord: null, telegram: null, reddit: null },
        paypal_order_id: acct.paypal_order_id,
        plan_months: acct.plan_months,
        bouquet: bouquetKey,
        bouquet_label: bouquetLabel,
        panel_bouquet_id: acct.panel_bouquet_id || null,
        is_custom_bouquet: isCustom,
        can_self_renew: canSelfRenew,
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
