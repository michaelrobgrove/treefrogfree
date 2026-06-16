/** POST /api/checkout
 *
 *  Body: {
 *    plan_months: 1|3|6|12,
 *    bouquet:     "us_wo"|"us_w"|"ca_wo"|"ca_w",
 *    name:        string,         // required, customer's real name
 *    email:       string,         // required, contact email
 *    discord?:    string,         // optional, e.g. "username" or "username#1234"
 *    telegram?:   string,         // optional, e.g. "@handle" or "handle"
 *    reddit?:     string,         // optional, e.g. "u/handle" or "handle"
 *  }
 *  Returns: { approval_url, order_id, amount_usd }
 *
 *  We:
 *   1. Validate the form fields.
 *   2. Stash them in KV under `checkout:pending:{order_id}` so
 *      the webhook can recover them on CAPTURE.COMPLETED.
 *   3. Create a PayPal Order with the (plan, bouquet) selection
 *      encoded in `purchase_units[0].custom_id =
 *      "{months}|{bouquet}"` and `amount.value` from
 *      `priceFor(months)`. PayPal does the rest.
 *
 *  Note: we do NOT create the Gold Panel account here — that
 *  happens on the CAPTURE.COMPLETED webhook, AFTER the money
 *  has cleared. */

import {
    BOUQUET_IDS,
    BOUQUET_LABELS,
    priceFor,
    type PlanMonths,
    type BouquetId,
} from "../_lib/plans";
import { putCheckoutIntent, type CheckoutIntent, type ContactHandles } from "../_lib/kv";

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

interface CheckoutBody {
    plan_months?: number;
    bouquet?: string;
    name?: string;
    email?: string;
    discord?: string;
    telegram?: string;
    reddit?: string;
}

// Basic shape check — we intentionally don't use a regex
// strict-mode email check; the welcome email will fail to
// send if the address is bogus and the operator can recover.
function isValidEmail(s: string): boolean {
    if (typeof s !== "string") return false;
    const t = s.trim();
    if (t.length < 3 || t.length > 254) return false;
    // one @, non-empty local and domain, at least one dot in domain
    const at = t.indexOf("@");
    if (at < 1) return false;
    if (at === t.length - 1) return false;
    const domain = t.slice(at + 1);
    if (!domain.includes(".")) return false;
    return true;
}

function strip(s: unknown, max: number): string | null {
    if (typeof s !== "string") return null;
    const t = s.trim();
    if (!t) return null;
    return t.slice(0, max);
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    if (ctx.request.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
    }
    const kv = ctx.env.PLUS_KV as KVNamespace;
    let body: CheckoutBody;
    try { body = await ctx.request.json() as CheckoutBody; }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }

    // Validate plan + bouquet.
    const plan_months = parseInt(String(body?.plan_months ?? ""), 10) as PlanMonths;
    if (![1, 3, 6, 12].includes(plan_months)) {
        return json({ error: "plan_months must be 1, 3, 6, or 12" }, 400);
    }
    const bouquet = String(body?.bouquet || "") as BouquetId;
    if (!BOUQUET_IDS.includes(bouquet)) {
        return json({ error: "Unknown bouquet" }, 400);
    }

    // Validate form fields.
    const name  = strip(body?.name, 100);
    const email = strip(body?.email, 254);
    if (!name)  return json({ error: "Name is required" }, 400);
    if (!email || !isValidEmail(email)) {
        return json({ error: "A valid email is required" }, 400);
    }
    const contact: ContactHandles = {
        discord:  strip(body?.discord, 64),
        telegram: strip(body?.telegram, 64),
        reddit:   strip(body?.reddit, 64),
    };

    // Check there's not already an active account for this
    // email (avoid double-signup confusion).
    const { getAccountByEmail } = await import("../_lib/kv");
    const existing = await getAccountByEmail(kv, email!);
    if (existing && existing.status === "active") {
        return json({
            error: "An account with this email is already active. Sign in instead.",
            code: "already_active",
        }, 409);
    }

    const { createOrder } = await import("../_lib/paypal");

    const base = String(ctx.env.PUBLIC_BASE_URL || "https://beta.tfplus.stream");
    const returnUrl = `${base}/thanks.html`;
    const cancelUrl = `${base}/pricing.html?cancelled=1`;

    const amount = priceFor(plan_months);
    const customId = `${plan_months}|${bouquet}`;
    const description =
        `Tree Frog Plus — ${plan_months} months (${BOUQUET_LABELS[bouquet]})`;

    let order: { id: string; links: Array<{ rel: string; href: string }> };
    try {
        order = await createOrder(kv, {
            amount_usd: amount,
            custom_id: customId,
            description,
            return_url: returnUrl,
            cancel_url: cancelUrl,
        });
    } catch (e) {
        console.error("checkout: createOrder failed:", (e as Error).message);
        return json({ error: "Could not start checkout. Please try again." }, 502);
    }

    // Stash the form fields so the webhook can build the
    // Account record with the customer's name + contact
    // handles. We do this AFTER the order is created so the
    // pending key always has a matching order.
    const intent: CheckoutIntent = {
        name: name!,
        email: email!.toLowerCase(),
        contact,
        created_at: new Date().toISOString(),
    };
    await putCheckoutIntent(kv, order.id, intent);

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
};

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
