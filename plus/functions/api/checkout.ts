/** POST /api/checkout
 *
 *  Body: { plan_months: 3|6|12, bouquet: "us_wo"|"us_w"|"ca_wo"|"ca_w" }
 *  Returns: { approval_url, order_id }
 *
 *  Creates a one-time PayPal Order for the selected plan ×
 *  bouquet, then redirects the buyer to PayPal. The (plan,
 *  bouquet) selection is encoded into
 *  `purchase_units[0].custom_id` as "{months}|{bouquet}" so
 *  the webhook can recover it without trusting the client.
 *
 *  The amount is taken from `priceFor(months)` in
 *  `_lib/plans.ts` — it's authoritative here, the client only
 *  picks the SKU.
 */

import { BOUQUET_IDS, BOUQUET_LABELS, priceFor, type PlanMonths, type BouquetId } from "../_lib/plans";

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

    const plan_months = parseInt(body?.plan_months, 10) as PlanMonths;
    const bouquet = String(body?.bouquet || "") as BouquetId;
    if (![3, 6, 12].includes(plan_months)) {
        return json({ error: "plan_months must be 3, 6, or 12" }, 400);
    }
    if (!BOUQUET_IDS.includes(bouquet)) {
        return json({ error: "Unknown bouquet" }, 400);
    }

    const { createOrder } = await import("../_lib/paypal");

    const base = String(ctx.env.PUBLIC_BASE_URL || "https://beta.tfplus.stream");
    const returnUrl = `${base}/thanks.html`;
    const cancelUrl = `${base}/pricing.html?cancelled=1`;

    const amount = priceFor(plan_months);
    const customId = `${plan_months}|${bouquet}`;
    const description =
        `Tree Frog Plus — ${plan_months} months (${BOUQUET_LABELS[bouquet]})`;

    try {
        const order = await createOrder(kv, {
            amount_usd: amount,
            custom_id: customId,
            description,
            return_url: returnUrl,
            cancel_url: cancelUrl,
        });
        const approve = order.links?.find((l: any) => l.rel === "approve");
        if (!approve?.href) {
            return json({ error: "No approval URL from PayPal" }, 502);
        }
        return json({
            approval_url: approve.href,
            order_id: order.id,
            amount_usd: amount,
            months: plan_months,
            bouquet: bouquet,
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
