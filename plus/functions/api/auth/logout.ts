/** POST /api/auth/logout
 *
 *  Clears the session cookie and deletes the KV session entry.
 *  Always returns 200 — even if the cookie was missing. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { destroySession } = await import("../../_lib/session");
    const cookie = await destroySession(ctx.request, kv);
    return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie": cookie,
        },
    });
};
