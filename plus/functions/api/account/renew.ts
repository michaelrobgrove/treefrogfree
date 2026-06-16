/** POST /api/account/renew
 *
 *  Body: { months: 1|3|6|12 }
 *  Returns: { approval_url, order_id, amount_usd }
 *
 *  Generates a fresh PayPal Order for the renewal amount
 *  and emails the customer a payment link. The customer
 *  pays, the order is captured, and on the
 *  PAYMENT.CAPTURE.COMPLETED webhook we call Gold Panel
 *  `action=renew` to add the months to the line.
 *
 *  We don't extend the Gold Panel line yet — the line is
 *  only extended when the capture webhook fires. If the
 *  customer never pays, nothing changes. */

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
    const months = parseInt(body?.months, 10) as 1 | 3 | 6 | 12;
    if (![1, 3, 6, 12].includes(months)) {
        return json({ error: "months must be 1, 3, 6, or 12" }, 400);
    }

    const { getSessionAccount } = await import("../../_lib/session");
    const { putAccount } = await import("../../_lib/kv");
    const { BOUQUET_LABELS, priceForRenewal } = await import("../../_lib/plans");
    const { createOrder } = await import("../../_lib/paypal");
    const { renewalLinkEmail, sendEmail } = await import("../../_lib/email");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct = sess.account;
    if (!acct.panel_username || !acct.panel_password) {
        return json({ error: "Account not yet activated" }, 400);
    }
    if (acct.status === "expired" || acct.status === "refunded") {
        return json({ error: "Account is not active" }, 400);
    }
    if (acct.pending_renewal_order_id) {
        return json({
            error: "A renewal payment link is already outstanding. Complete or cancel it first.",
        }, 409);
    }

    // Custom-id is the KEY that lets the webhook find this
    // account when the order captures. We encode
    // "renew|{account_order_id}|{months}" so the dispatcher
    // can tell renewals apart from initial checkouts
    // ("{months}|{bouquet}") and route them correctly.
    const customId = `renew|${acct.paypal_order_id}|${months}`;

    const base = String(ctx.env.PUBLIC_BASE_URL || "https://beta.tfplus.stream");
    const amount = priceForRenewal(months as 1 | 3 | 6 | 12);
    const description =
        `Tree Frog Plus renewal — ${months} months (${BOUQUET_LABELS[acct.bouquet]})`;

    let order: { id: string; links: Array<{ rel: string; href: string }> };
    try {
        order = await createOrder(kv, {
            amount_usd: amount,
            custom_id: customId,
            description,
            return_url: `${base}/dashboard.html?renewed=1`,
            cancel_url: `${base}/dashboard.html?renewal_cancelled=1`,
        });
    } catch (e) {
        console.error("renew: createOrder failed:", (e as Error).message);
        return json({ error: "Could not create renewal link. Please try again." }, 502);
    }

    // Record the pending renewal on the account so the webhook
    // can find it.
    acct.pending_renewal_order_id = order.id;
    acct.pending_renewal_months = months as 1 | 3 | 6 | 12;
    // A fresh renewal means the customer has decided NOT to
    // cancel — clear the flag.
    if (acct.cancel_at_period_end) {
        acct.cancel_at_period_end = false;
        if (acct.status === "cancel_at_period_end") acct.status = "active";
    }
    await putAccount(kv, acct);

    const approve = order.links?.find((l: any) => l.rel === "approve");
    if (!approve?.href) {
        return json({ error: "No approval URL from PayPal" }, 502);
    }

    // Email the customer the link so they can pay from any
    // device. The dashboard also surfaces it directly.
    const tmpl = renewalLinkEmail({
        email: acct.email,
        months,
        amount_usd: amount,
        payment_url: approve.href,
        dashboard_url: `${base}/dashboard.html`,
    });
    try { await sendEmail({ to: acct.email, ...tmpl }); }
    catch (e) { console.error("renew: email send failed:", (e as Error).message); }

    return json({
        approval_url: approve.href,
        order_id: order.id,
        amount_usd: amount,
        months,
    });
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
