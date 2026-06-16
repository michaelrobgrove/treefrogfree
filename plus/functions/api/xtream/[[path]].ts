/** GET /api/xtream/<path>
 *
 *  Xtream Codes API proxy. Looks up the customer's session,
 *  appends their username/password to the request, and
 *  forwards to the panel. We allow a small allowlist of
 *  read-only actions (the player only needs `get_live_*` and
 *  the EPG endpoints).
 *
 *  Usage from the page (rare — the player uses
 *  /api/player/channels directly):
 *    fetch('/api/xtream/player_api.php?action=get_live_categories')
 */

const ALLOWED_ACTIONS = new Set([
    "get_live_categories",
    "get_live_streams",
    "get_vod_categories",
    "get_vod_streams",
    "get_series_categories",
    "get_series",
    "get_short_epg",
    "get_simple_data_table",
]);

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { decryptPanelPassword } = await import("../../_lib/kv");
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

    const url = new URL(ctx.request.url);
    // Pull the upstream path from /api/xtream/<...>. Pages passes
    // the rest of the URL through `ctx.params.path` but in practice
    // the function receives a full Request — we just strip the
    // /api/xtream prefix.
    const upstreamPath = url.pathname.replace(/^\/api\/xtream\//, "");
    if (upstreamPath.includes("..") || !upstreamPath.startsWith("player_api.php")) {
        return json({ error: "Invalid path" }, 400);
    }
    const action = url.searchParams.get("action") || "";
    if (action && !ALLOWED_ACTIONS.has(action)) {
        return json({ error: "Action not allowed" }, 400);
    }

    // Forward to apex (HTTPS) with the customer's credentials.
    const apex = String((globalThis as any).DNS_PRIMARY || "https://apex.tfplus.stream");
    const fwd = new URL(`${apex}/${upstreamPath}`);
    fwd.searchParams.set("username", acct.panel_username);
    fwd.searchParams.set("password", password);
    // Pass through any caller-provided query params (except the
    // auth ones we set above). workers-types URLSearchParams is
    // missing `.keys()`/`.entries()` so we go via toString.
    const passthrough = url.searchParams.toString();
    if (passthrough) {
        for (const part of passthrough.split("&")) {
            const eq = part.indexOf("=");
            if (eq < 0) continue;
            const k = part.slice(0, eq);
            if (k === "username" || k === "password") continue;
            fwd.searchParams.set(k, decodeURIComponent(part.slice(eq + 1)));
        }
    }
    try {
        const resp = await fetch(fwd.toString(), {
            headers: { "User-Agent": "TreeFrogPlus/1.0" },
        });
        const body = await resp.text();
        return new Response(body, {
            status: resp.status,
            headers: {
                "Content-Type": resp.headers.get("Content-Type") || "application/json",
                "Access-Control-Allow-Origin": String((globalThis as any).PUBLIC_BASE_URL || "*"),
                "Cache-Control": "no-store",
            },
        });
    } catch (e) {
        return json({ error: "Upstream fetch failed" }, 502);
    }
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
