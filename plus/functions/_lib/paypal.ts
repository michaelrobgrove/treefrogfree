/** PayPal Orders API client.
 *
 *  We use Orders (one-time payments) — NOT Subscriptions — and
 *  generate a fresh payment link whenever the customer needs
 *  to pay (initial checkout, manual renewal, etc.). PayPal
 *  auto-bills nothing; the site owns the renewal schedule.
 *
 *  Auth is a short-lived OAuth2 token from `/v1/oauth2/token`,
 *  cached in KV under `paypal:oauth_token` with a 9-hour TTL
 *  (PayPal's own expiry minus a 5-min safety margin).
 *
 *  Webhook signature verification goes through PayPal's REST
 *  endpoint `/v1/notifications/verify-webhook-signature`. PayPal
 *  only returns "SUCCESS" or "FAILURE", and the response is
 *  what we trust.
 */

export interface PayPalOrder {
    id: string;
    status: string;
    custom_id?: string;
    purchase_units?: Array<{
        custom_id?: string;
        amount?: { currency_code: string; value: string };
        payments?: {
            captures?: Array<{ id: string; status: string }>;
        };
    }>;
    links: Array<{ href: string; rel: string }>;
}

export interface PayPalCapture {
    id: string;
    status: string;
    custom_id?: string;
    amount?: { currency_code: string; value: string };
    supplementary_data?: { related_ids?: { order_id?: string } };
    create_time?: string;
    update_time?: string;
}

function apiBase(): string {
    const v = (globalThis as any).PAYPAL_API_BASE;
    if (typeof v !== "string" || !v) {
        throw new Error("PAYPAL_API_BASE is not configured.");
    }
    return v.replace(/\/+$/, "");
}

function clientId(): string {
    const v = (globalThis as any).PAYPAL_CLIENT_ID;
    if (typeof v !== "string" || !v) {
        throw new Error("PAYPAL_CLIENT_ID is not configured.");
    }
    return v;
}

function clientSecret(): string {
    const v = (globalThis as any).PAYPAL_CLIENT_SECRET;
    if (typeof v !== "string" || !v) {
        throw new Error("PAYPAL_CLIENT_SECRET is not configured.");
    }
    return v;
}

/** Returns a cached OAuth token, refreshing if needed. */
export async function getAccessToken(kv: KVNamespace): Promise<string> {
    const cached = await kv.get("paypal:oauth_token");
    if (cached) {
        try {
            const parsed = JSON.parse(cached) as { token: string; exp: number };
            if (parsed.exp > Date.now() / 1000 + 60) return parsed.token;
        } catch (e) { /* fall through */ }
    }
    const basic = btoa(`${clientId()}:${clientSecret()}`);
    const resp = await fetch(`${apiBase()}/v1/oauth2/token`, {
        method: "POST",
        headers: {
            "Authorization": `Basic ${basic}`,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body: "grant_type=client_credentials",
    });
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`PayPal OAuth failed: ${resp.status} ${text}`);
    }
    const data = await resp.json() as { access_token: string; expires_in: number };
    const exp = Math.floor(Date.now() / 1000) + data.expires_in - 300; // 5-min safety
    await kv.put(
        "paypal:oauth_token",
        JSON.stringify({ token: data.access_token, exp }),
        { expirationTtl: Math.max(60, data.expires_in - 300) },
    );
    return data.access_token;
}

async function pp<T>(kv: KVNamespace, path: string, init: RequestInit = {}): Promise<T> {
    const token = await getAccessToken(kv);
    const resp = await fetch(`${apiBase()}${path}`, {
        ...init,
        headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json",
            ...(init.headers || {}),
        },
    });
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`PayPal ${path} → ${resp.status}: ${text.slice(0, 200)}`);
    }
    return await resp.json() as T;
}

/** Create a one-time Order. Returns the full response so the
 *  caller can pluck out the `links[rel=approve].href`.
 *
 *  The order carries the (plan, bouquet) selection in
 *  `purchase_units[0].custom_id` as "{months}|{bouquet}" so
 *  the webhook knows what was bought without trusting the
 *  client. The order is created with `intent: CAPTURE` and
 *  is captured server-side on the
 *  `CHECKOUT.ORDER.APPROVED` webhook (or here, see the
 *  `capture` flag). */
export async function createOrder(
    kv: KVNamespace,
    opts: {
        amount_usd: number;
        custom_id: string;
        description: string;
        return_url: string;
        cancel_url: string;
    },
): Promise<PayPalOrder> {
    return pp<PayPalOrder>(kv, "/v2/checkout/orders", {
        method: "POST",
        body: JSON.stringify({
            intent: "CAPTURE",
            purchase_units: [
                {
                    custom_id: opts.custom_id,
                    description: opts.description,
                    amount: {
                        currency_code: "USD",
                        value: opts.amount_usd.toFixed(2),
                    },
                },
            ],
            application_context: {
                brand_name: "Tree Frog Plus",
                shipping_preference: "NO_SHIPPING",
                user_action: "PAY_NOW",
                return_url: opts.return_url,
                cancel_url: opts.cancel_url,
            },
        }),
    });
}

/** Capture an approved order. Normally triggered by the
 *  CHECKOUT.ORDER.APPROVED webhook, but exposed for tests
 *  and recovery flows. */
export async function captureOrder(
    kv: KVNamespace,
    orderId: string,
): Promise<PayPalOrder> {
    return pp<PayPalOrder>(kv, `/v2/checkout/orders/${encodeURIComponent(orderId)}/capture`, {
        method: "POST",
        body: "{}",
    });
}

/** Refund a captured payment. Pass the full or partial amount
 *  in `amount_usd`. If omitted, refunds the full capture. */
export async function refundCapture(
    kv: KVNamespace,
    captureId: string,
    opts: { amount_usd?: number; note?: string } = {},
): Promise<unknown> {
    const body: Record<string, unknown> = {};
    if (opts.note) body.note_to_payer = opts.note;
    if (typeof opts.amount_usd === "number") {
        body.amount = {
            currency_code: "USD",
            value: opts.amount_usd.toFixed(2),
        };
    }
    return pp<unknown>(kv, `/v2/payments/captures/${encodeURIComponent(captureId)}/refund`, {
        method: "POST",
        body: JSON.stringify(body),
    });
}

/** Look up a previously-created order (used by the thanks-page
 *  poller as a fallback if the webhook is slow). */
export async function getOrder(
    kv: KVNamespace,
    orderId: string,
): Promise<PayPalOrder> {
    return pp<PayPalOrder>(kv, `/v2/checkout/orders/${encodeURIComponent(orderId)}`);
}

/** Look up a capture (used to confirm a webhook event). */
export async function getCapture(
    kv: KVNamespace,
    captureId: string,
): Promise<PayPalCapture> {
    return pp<PayPalCapture>(kv, `/v2/payments/captures/${encodeURIComponent(captureId)}`);
}

/** Verify a webhook delivery. Returns true if the signature
 *  matches PayPal's records for our webhook id. */
export async function verifyWebhookSignature(
    kv: KVNamespace,
    body: string,
    headers: {
        transmission_id: string;
        transmission_time: string;
        transmission_sig: string;
        cert_url: string;
        auth_algo: string;
    },
): Promise<boolean> {
    const webhookId = (globalThis as any).PAYPAL_WEBHOOK_ID;
    if (typeof webhookId !== "string" || !webhookId) {
        console.error("PAYPAL_WEBHOOK_ID is not configured — rejecting webhook");
        return false;
    }
    try {
        const result = await pp<{ verification_status: string }>(kv, "/v1/notifications/verify-webhook-signature", {
            method: "POST",
            body: JSON.stringify({
                auth_algo:         headers.auth_algo,
                cert_url:          headers.cert_url,
                transmission_id:   headers.transmission_id,
                transmission_sig:  headers.transmission_sig,
                transmission_time: headers.transmission_time,
                webhook_id:        webhookId,
                webhook_event:     JSON.parse(body),
            }),
        });
        return result.verification_status === "SUCCESS";
    } catch (e) {
        console.error("PayPal verify-webhook-signature failed:", (e as Error).message);
        return false;
    }
}
