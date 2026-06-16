/** POST /api/account/cancel
 *
 *  Cancel the PayPal subscription at the end of the current
 *  billing period. The Gold Panel account stays alive until
 *  the period ends, at which point PayPal sends EXPIRED and
 *  our webhook disables it. */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { getSessionAccount } = await import("../../_lib/session");
    const { cancelSubscription } = await import("../../_lib/paypal");
    const { putAccount } = await import("../../_lib/kv");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;

    if (acct.cancel_at_period_end) {
        return json({ error: "Subscription is already cancelled" }, 400);
    }

    try {
        await cancelSubscription(kv, acct.subscription_id, "Customer requested cancellation");
    } catch (e) {
        return json({ error: "Could not cancel with PayPal. Please try again." }, 502);
    }
    acct.cancel_at_period_end = true;
    acct.status = "cancel_at_period_end";
    await putAccount(kv, acct);
    return json({ ok: true });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
