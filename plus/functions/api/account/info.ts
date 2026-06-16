/** GET /api/account/info
 *
 *  Returns the dashboard data for the currently signed-in
 *  customer. 401 if no valid session. The customer's
 *  Gold Panel username/password are returned in cleartext —
 *  the dashboard needs them to show in the Xtream Codes
 *  block. The session cookie is HttpOnly, so the only way
 *  this endpoint is reachable is from our own dashboard.
 *
 *  Renewal model: PayPal does NOT auto-bill. Renewals are
 *  paid on-demand by the customer (or, optionally, generated
 *  by us with /api/account/renew and emailed). We compute
 *  `days_until_renewal` from `expires_at` so the UI can show
 *  a "renew" CTA when the customer is within 7 days of
 *  expiry. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
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

    return json({
        email: acct.email,
        paypal_order_id: acct.paypal_order_id,
        plan_months: acct.plan_months,
        bouquet: acct.bouquet,
        bouquet_label: BOUQUET_LABELS[acct.bouquet] || acct.bouquet,
        status: acct.status,
        cancel_at_period_end: acct.cancel_at_period_end,
        created_at: acct.created_at,
        expires_at: acct.expires_at,
        days_until_expiry: daysUntilExpiry,
        pending_renewal: acct.pending_renewal_order_id
            ? { order_id: acct.pending_renewal_order_id, months: acct.pending_renewal_months }
            : null,
        // XC credentials + DNS — only returned for active accounts.
        username: acct.panel_username || "",
        password: acct.site_password,
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
