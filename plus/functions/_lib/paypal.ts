/** PayPal Subscriptions API client.
 *
 *  Uses the REST API at `${PAYPAL_API_BASE}/v1/...` (sandbox or
 *  live, controlled by env). Auth is a short-lived OAuth2 token
 *  issued by `/v1/oauth2/token`; we cache it in KV under
 *  `paypal:oauth_token` with a 9-hour TTL (PayPal's own expiry
 *  minus a 5-min safety margin).
 *
 *  Webhook signature verification goes through PayPal's REST
 *  endpoint `/v1/notifications/verify-webhook-signature` rather
 *  than re-implementing the cert+sig dance. PayPal only returns
 *  "SUCCESS" or "FAILURE", and the response is what we trust.
 */

export interface PayPalSubscription {
    id: string;
    plan_id: string;
    status: string;
    custom_id?: string;
    subscriber?: { email_address?: string };
    billing_info?: {
        next_billing_time?: string;
        last_payment?: { time?: string };
    };
    create_time?: string;
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

/** Create a subscription. Returns the full response so the
 *  caller can pluck out the `links[rel=approve].href`. */
export async function createSubscription(
    kv: KVNamespace,
    opts: { plan_id: string; custom_id: string; return_url: string; cancel_url: string },
): Promise<PayPalSubscription & { links: Array<{ href: string; rel: string }> }> {
    return pp(kv, "/v1/billing/subscriptions", {
        method: "POST",
        body: JSON.stringify({
            plan_id: opts.plan_id,
            custom_id: opts.custom_id,
            application_context: {
                brand_name: "Tree Frog Plus",
                shipping_preference: "NO_SHIPPING",
                user_action: "SUBSCRIBE_NOW",
                return_url: opts.return_url,
                cancel_url: opts.cancel_url,
            },
        }),
    });
}

/** Cancel a subscription (cancel-at-period-end). */
export async function cancelSubscription(
    kv: KVNamespace,
    subId: string,
    reason: string,
): Promise<void> {
    await pp(kv, `/v1/billing/subscriptions/${encodeURIComponent(subId)}/cancel`, {
        method: "POST",
        body: JSON.stringify({ reason }),
    });
}

export async function getSubscription(
    kv: KVNamespace,
    subId: string,
): Promise<PayPalSubscription> {
    return pp<PayPalSubscription>(kv, `/v1/billing/subscriptions/${encodeURIComponent(subId)}`);
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
