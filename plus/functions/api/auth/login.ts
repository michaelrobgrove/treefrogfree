/** POST /api/auth/login
 *
 *  Body: { email, password }
 *  Returns: { ok: true } + Set-Cookie: tfp_sess=...
 *
 *  The site password is the same one we send in the welcome email
 *  and hash in KV (PBKDF2-SHA-256, 100k iterations). On success,
 *  we mint a session token, store it in KV with a 24h TTL, and
 *  set the cookie.
 */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    if (ctx.request.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
    }
    let body: any;
    try { body = await ctx.request.json(); }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }
    const email = String(body?.email || "").trim().toLowerCase();
    const password = String(body?.password || "");
    if (!email || !password) {
        return json({ error: "Email and password are required" }, 400);
    }

    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getAccountByEmail } = await import("../../_lib/kv");
    const { verifyPassword, createSession } = await import("../../_lib/session");

    const acct = await getAccountByEmail(kv, email);
    if (!acct) {
        // Constant-time-ish: still hash to avoid trivial timing oracle.
        await verifyPassword(password, {
            salt: "00", hash: "00", iterations: 1,
        });
        return json({ error: "Invalid email or password" }, 401);
    }
    const ok = await verifyPassword(password, acct.password_auth);
    if (!ok) {
        return json({ error: "Invalid email or password" }, 401);
    }
    const { cookie } = await createSession(kv, acct.paypal_order_id);
    return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie": cookie,
        },
    });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
