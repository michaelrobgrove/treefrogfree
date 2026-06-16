/** POST /api/account/cancel
 *
 *  Mark the account as "do not renew at expiry". Since we use
 *  one-time PayPal Orders (no auto-billing), there's no PayPal
 *  subscription to cancel — the Gold Panel line simply keeps
 *  running until `expires_at` and then expires naturally.
 *
 *  The customer can pay a fresh renewal Order at any time
 *  before expiry to extend the line; the cancel flag is just
 *  advisory and is reset on the next successful renewal. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { putAccount } = await import("../../_lib/kv");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;

    if (acct.cancel_at_period_end) {
        return json({ error: "Subscription is already cancelled" }, 400);
    }
    if (acct.status === "expired") {
        return json({ error: "Account is already expired" }, 400);
    }

    acct.cancel_at_period_end = true;
    acct.status = "cancel_at_period_end";
    await putAccount(kv, acct);
    return json({ ok: true, expires_at: acct.expires_at });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
