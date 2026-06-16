/** GET /api/checkout/status?sub_id=I-XXXX
 *
 *  The thanks page polls this every 4 seconds. Returns
 *  `{ ready: true }` once the account exists in KV with
 *  status === "active" (i.e. the ACTIVATED webhook has
 *  fired and the Gold Panel account is ready).
 *
 *  We accept an unauthenticated request and just look up by
 *  subscription id. This is fine: the only data returned is
 *  "ready: true|false". The customer proves their identity
 *  with email + password at the login page. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    const url = new URL(ctx.request.url);
    const subId = url.searchParams.get("sub_id") || "";
    if (!subId) return json({ ready: false, error: "missing sub_id" });
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getAccountBySub } = await import("../../_lib/kv");
    const acct = await getAccountBySub(kv, subId);
    return json({
        ready: !!acct && acct.status === "active",
        status: acct?.status || "unknown",
    });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
