/** GET /api/player/channels
 *
 *  Returns the customer's live channel list, each with one or
 *  two ready-to-play HLS URLs (apex primary, comet fallback).
 *  We use the customer's Gold Panel XC credentials to fetch
 *  the live streams from the panel — the page itself never
 *  sees the password.
 *
 *  The result is cached in KV under `player:channels:{order_id}`
 *  for 5 minutes; the player can re-fetch on open. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { decryptPanelPassword } = await import("../../_lib/kv");
    const { listLiveChannels } = await import("../../_lib/goldpanel");
    const { panelIdToBouquet } = await import("../../_lib/plans");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;
    if (!acct.panel_username || !acct.panel_password_ct) {
        return json({ error: "Account not yet activated" }, 400);
    }
    const password = await decryptPanelPassword(ctx.env, acct);
    if (!password) {
        return json({ error: "Could not decrypt credentials; sign in again to refresh." }, 401);
    }

    // Determine if the user is on a custom bouquet
    const bouquet = panelIdToBouquet(acct.panel_bouquet_id);
    const isCustomBouquet = bouquet === null;

    const cacheKey = `player:channels:${acct.paypal_order_id}`;
    const cached = await kv.get(cacheKey);
    if (cached) {
        return new Response(cached, {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    }
    try {
        const data = await listLiveChannels(acct.panel_username, password);
        // For custom bouquets, the panel already returns only the channels
        // for that custom bouquet, so we pass through the data as-is.
        // For standard bouquets, existing matching behavior is retained.
        const body = JSON.stringify({ ...data, is_custom_bouquet: isCustomBouquet });
        await kv.put(cacheKey, body, { expirationTtl: 300 });
        return new Response(body, {
            status: 200,
            headers: { "Content-Type": "application/json" },
        });
    } catch (e) {
        return json({ error: "Could not load channel list" }, 502);
    }
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}