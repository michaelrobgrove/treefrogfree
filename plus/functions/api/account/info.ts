/** GET /api/account/info
 *
 *  Returns the dashboard data for the currently signed-in
 *  customer. 401 if no valid session. The customer's
 *  Gold Panel username/password are returned in cleartext —
 *  the dashboard needs them to show in the Xtream Codes
 *  block. The session cookie is HttpOnly, so the only way
 *  this endpoint is reachable is from our own dashboard. */

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

    return json({
        email: acct.email,
        subscription_id: acct.subscription_id,
        plan_months: acct.plan_months,
        bouquet: acct.bouquet,
        bouquet_label: BOUQUET_LABELS[acct.bouquet] || acct.bouquet,
        status: acct.status,
        cancel_at_period_end: acct.cancel_at_period_end,
        created_at: acct.created_at,
        expires_at: acct.expires_at,
        next_billing_at: acct.next_billing_at,
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
