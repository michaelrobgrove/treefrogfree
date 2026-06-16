/** POST /api/checkout
 *
 *  Body: { plan_months: 3|6|12, bouquet: "us"|"us_adult"|"us_ca"|"us_ca_adult" }
 *  Returns: { approval_url: "https://www.paypal.com/..." }
 *
 *  Creates a PayPal Subscription against the pre-configured plan
 *  ID for the (plan, bouquet) pair. The customer's selection is
 *  re-derived on the webhook from the plan_id, so the front-end
 *  doesn't need to round-trip any custom data.
 */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    if (ctx.request.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
    }
    const kv = ctx.env.PLUS_KV as KVNamespace;
    let body: any;
    try { body = await ctx.request.json(); }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }

    const plan_months = parseInt(body?.plan_months, 10);
    const bouquet = String(body?.bouquet || "");
    if (![3, 6, 12].includes(plan_months)) {
        return json({ error: "plan_months must be 3, 6, or 12" }, 400);
    }
    if (!["us", "us_adult", "us_ca", "us_ca_adult"].includes(bouquet)) {
        return json({ error: "Unknown bouquet" }, 400);
    }

    const { skuToPaypalPlan } = await import("../_lib/plans");
    const { createSubscription } = await import("../_lib/paypal");

    let planId: string;
    try {
        planId = skuToPaypalPlan(plan_months as 3 | 6 | 12, bouquet as any);
    } catch (e) {
        return json({ error: (e as Error).message }, 500);
    }

    const base = String(ctx.env.PUBLIC_BASE_URL || "https://beta.tfplus.stream");
    const returnUrl = `${base}/thanks.html`;
    const cancelUrl = `${base}/pricing.html?cancelled=1`;

    try {
        const sub = await createSubscription(kv, {
            plan_id: planId,
            custom_id: `${plan_months}|${bouquet}`,
            return_url: returnUrl,
            cancel_url: cancelUrl,
        });
        const approve = sub.links?.find((l: any) => l.rel === "approve");
        if (!approve?.href) {
            return json({ error: "No approval URL from PayPal" }, 502);
        }
        return json({
            approval_url: approve.href,
            subscription_id: sub.id,
        });
    } catch (e) {
        console.error("checkout error:", (e as Error).message);
        return json({ error: "Could not start checkout. Please try again." }, 502);
    }
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
