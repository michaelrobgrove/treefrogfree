/** POST /api/account/renew
 *
 *  Body: { months: 1|3|6|12 }
 *  Returns: { ok, expires_at }
 *
 *  Adds `months` to the customer's Gold Panel subscription.
 *  The Gold Panel `sub` parameter is additive — renewing a
 *  line with 2 months left and `sub=3` lands at 5 months.
 *  We pull the resulting `expire` from a follow-up
 *  `device_info` call. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    if (ctx.request.method !== "POST") return json({ error: "Method not allowed" }, 405);
    const kv = ctx.env.PLUS_KV as KVNamespace;
    let body: any;
    try { body = await ctx.request.json(); }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }
    const months = parseInt(body?.months, 10);
    if (![1, 3, 6, 12].includes(months)) {
        return json({ error: "months must be 1, 3, 6, or 12" }, 400);
    }

    const { getSessionAccount } = await import("../../_lib/session");
    const { renewM3U, getDeviceInfo } = await import("../../_lib/goldpanel");
    const { putAccount } = await import("../../_lib/kv");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;
    if (!acct.panel_username || !acct.panel_password) {
        return json({ error: "Account not yet activated" }, 400);
    }

    try {
        await renewM3U({
            username: acct.panel_username,
            password: acct.panel_password,
            sub: months as 1 | 3 | 6 | 12,
        });
    } catch (e) {
        return json({ error: "Gold Panel renew failed. Please try again." }, 502);
    }
    // Pull the new expire.
    try {
        const info = await getDeviceInfo({
            username: acct.panel_username,
            password: acct.panel_password,
        });
        acct.expires_at = info.expire || acct.expires_at;
    } catch (e) { /* non-fatal */ }
    await putAccount(kv, acct);
    return json({ ok: true, expires_at: acct.expires_at });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
