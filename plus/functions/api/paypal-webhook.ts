/** POST /api/paypal-webhook
 *
 *  Receives PayPal lifecycle events. Each event is verified
 *  against PayPal's webhook signature, deduplicated by event_id,
 *  and dispatched to the right handler.
 *
 *  Flow:
 *   CREATED  → record subscription in KV as "pending"
 *   ACTIVATED → create Gold Panel M3U line, store creds,
 *               send welcome email
 *   SALE.COMPLETED → renew Gold Panel line, update expires_at,
 *                    send renewal receipt
 *   CANCELLED → mark cancel_at_period_end = true (line stays live
 *               until EXPIRED)
 *   EXPIRED / SUSPENDED → call Gold Panel device_status disable
 *   PAYMENT.FAILED → mark account, email customer
 */

import { verifyWebhookSignature, getSubscription } from "../_lib/paypal";
import { claimEventId, getAccountBySub, putAccount, type Account } from "../_lib/kv";
import { paypalPlanToSku, bouquetToPanelId } from "../_lib/plans";
import { createM3U, renewM3U, getDeviceInfo, setDeviceStatus } from "../_lib/goldpanel";
import { welcomeEmail, paymentFailedEmail, renewalReceiptEmail, sendEmail } from "../_lib/email";
import { hashPassword } from "../_lib/session";

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;

    // 1. Read raw body — needed verbatim for signature verification.
    const rawBody = await ctx.request.text();

    // 2. Verify the signature.
    const headers = {
        transmission_id:   ctx.request.headers.get("PAYPAL-TRANSMISSION-ID")   || "",
        transmission_time: ctx.request.headers.get("PAYPAL-TRANSMISSION-TIME") || "",
        transmission_sig:  ctx.request.headers.get("PAYPAL-TRANSMISSION-SIG")  || "",
        cert_url:          ctx.request.headers.get("PAYPAL-CERT-URL")          || "",
        auth_algo:         ctx.request.headers.get("PAYPAL-AUTH-ALGO")         || "",
    };
    const ok = await verifyWebhookSignature(kv, rawBody, headers);
    if (!ok) {
        return new Response("invalid signature", { status: 401 });
    }

    // 3. Parse the event.
    let event: any;
    try {
        event = JSON.parse(rawBody);
    } catch (e) {
        return new Response("invalid json", { status: 400 });
    }
    const eventId = event.id;
    const eventType = event.event_type;
    if (!eventId || !eventType) {
        return new Response("missing event fields", { status: 400 });
    }

    // 4. Deduplicate. PayPal re-delivers on 5xx, so we MUST be idempotent.
    const fresh = await claimEventId(kv, eventId);
    if (!fresh) {
        return new Response("ok (duplicate)", { status: 200 });
    }

    // 5. Dispatch.
    try {
        await dispatch(event, eventType, {
            kv,
            getSubscription: (id: string) => getSubscription(kv, id),
            getAccount: (id: string) => getAccountBySub(kv, id),
            putAccount: (a: Account) => putAccount(kv, a),
            paypalPlanToSku,
            bouquetToPanelId,
            createM3U,
            renewM3U,
            getDeviceInfo,
            setDeviceStatus,
            welcomeEmail,
            paymentFailedEmail,
            renewalReceiptEmail,
            sendEmail,
            hashPassword,
            publicBaseUrl: String(ctx.env.PUBLIC_BASE_URL || "https://beta.tfplus.stream"),
        });
    } catch (e) {
        console.error("Webhook handler error:", (e as Error).message, e);
        // Return 500 so PayPal retries — the dedup key will be re-checked.
        return new Response("handler error", { status: 500 });
    }
    return new Response("ok", { status: 200 });
};

interface DispatchDeps {
    kv: KVNamespace;
    getSubscription: (id: string) => Promise<any>;
    getAccount: (id: string) => Promise<Account | null>;
    putAccount: (a: Account) => Promise<void>;
    paypalPlanToSku: (planId: string) => { months: any; bouquet: any } | null;
    bouquetToPanelId: (b: any) => string;
    createM3U: (opts: { sub: any; pack: string; country?: string; notes?: string }) => Promise<any>;
    renewM3U: (opts: { username: string; password: string; sub: any }) => Promise<any>;
    getDeviceInfo: (opts: { username: string; password: string }) => Promise<any>;
    setDeviceStatus: (opts: { user_id: string; status: "enable" | "disable" }) => Promise<any>;
    welcomeEmail: (opts: any) => { subject: string; html: string; text: string };
    paymentFailedEmail: (opts: any) => { subject: string; html: string; text: string };
    renewalReceiptEmail: (opts: any) => { subject: string; html: string; text: string };
    sendEmail: (opts: any) => Promise<void>;
    hashPassword: (password: string) => Promise<{ salt: string; hash: string; iterations: number }>;
    publicBaseUrl: string;
}

async function dispatch(event: any, eventType: string, d: DispatchDeps): Promise<void> {
    const resource = event.resource || {};
    const subId: string = resource.id || resource.billing_agreement_id;
    if (!subId) {
        console.warn("Webhook event missing subscription id", eventType, event.id);
        return;
    }

    switch (eventType) {
        case "BILLING.SUBSCRIPTION.CREATED": {
            const sku = resource.plan_id ? d.paypalPlanToSku(resource.plan_id) : null;
            const sub = await d.getSubscription(subId);
            const email: string = sub.subscriber?.email_address
                || resource.subscriber?.email_address
                || "";
            if (!email) {
                console.warn("CREATED: no email on subscription", subId);
                return;
            }
            const sitePw = generateSitePassword();
            const acct: Account = {
                subscription_id: subId,
                email: email.toLowerCase(),
                panel_user_id: null,
                panel_username: null,
                panel_password: null,
                site_password: sitePw,
                password_auth: await d.hashPassword(sitePw),
                plan_months: sku?.months ?? 12,
                bouquet: sku?.bouquet ?? "us",
                panel_bouquet_id: sku ? d.bouquetToPanelId(sku.bouquet) : "",
                created_at: new Date().toISOString(),
                expires_at: null,
                next_billing_at: null,
                status: "pending",
                cancel_at_period_end: false,
            };
            await d.putAccount(acct);
            console.log("CREATED recorded for sub", subId, "email", email);
            return;
        }

        case "BILLING.SUBSCRIPTION.ACTIVATED": {
            const acct = await d.getAccount(subId);
            if (!acct) {
                console.warn("ACTIVATED for unknown sub", subId);
                return;
            }
            if (acct.status === "active") {
                console.log("ACTIVATED: sub already active, skipping", subId);
                return;
            }
            const panelBouquet = d.bouquetToPanelId(acct.bouquet);
            const created = await d.createM3U({
                sub: acct.plan_months,
                pack: panelBouquet,
                country: "",
                notes: `tfplus:${acct.email}`,
            });
            acct.panel_user_id    = String(created.user_id);
            acct.panel_username   = created.username;
            acct.panel_password   = created.password;
            acct.panel_bouquet_id = panelBouquet;
            acct.status = "active";
            acct.next_billing_at = resource.billing_info?.next_billing_time || null;
            // Pull the new expiry.
            try {
                const info = await d.getDeviceInfo({
                    username: created.username,
                    password: created.password,
                });
                acct.expires_at = info.expire || null;
            } catch (e) {
                console.warn("ACTIVATED: device_info follow-up failed:", (e as Error).message);
            }
            await d.putAccount(acct);
            // Send the welcome email.
            const dnsPrimary   = String((globalThis as any).DNS_PRIMARY   || "https://apex.tfplus.stream");
            const dnsSecondary = String((globalThis as any).DNS_SECONDARY || "http://comet.tfplus.stream");
            const tmpl = d.welcomeEmail({
                email: acct.email,
                username: created.username,
                password: acct.site_password,
                dns_primary: dnsPrimary,
                dns_secondary: dnsSecondary,
                xc_server: dnsPrimary,
                setup_url: `${d.publicBaseUrl}/setup.html`,
            });
            await d.sendEmail({ to: acct.email, ...tmpl });
            console.log("ACTIVATED: account ready for", acct.email);
            return;
        }

        case "PAYMENT.SALE.COMPLETED": {
            const acct = await d.getAccount(subId);
            if (!acct || !acct.panel_username || !acct.panel_password) {
                console.warn("SALE.COMPLETED: missing account or creds", subId);
                return;
            }
            await d.renewM3U({
                username: acct.panel_username,
                password: acct.panel_password,
                sub: acct.plan_months,
            });
            try {
                const info = await d.getDeviceInfo({
                    username: acct.panel_username,
                    password: acct.panel_password,
                });
                acct.expires_at = info.expire || acct.expires_at;
            } catch (e) {
                console.warn("SALE.COMPLETED: device_info failed", (e as Error).message);
            }
            acct.status = "active";
            await d.putAccount(acct);
            if (acct.expires_at) {
                const tmpl = d.renewalReceiptEmail({
                    email: acct.email,
                    new_expire: acct.expires_at,
                    months_added: acct.plan_months,
                });
                try { await d.sendEmail({ to: acct.email, ...tmpl }); }
                catch (e) { /* non-fatal */ }
            }
            console.log("SALE.COMPLETED: renewed", subId, "→", acct.expires_at);
            return;
        }

        case "BILLING.SUBSCRIPTION.CANCELLED": {
            const acct = await d.getAccount(subId);
            if (!acct) return;
            acct.cancel_at_period_end = true;
            acct.status = "cancel_at_period_end";
            await d.putAccount(acct);
            console.log("CANCELLED: cancel_at_period_end set for", subId);
            return;
        }

        case "BILLING.SUBSCRIPTION.EXPIRED":
        case "BILLING.SUBSCRIPTION.SUSPENDED": {
            const acct = await d.getAccount(subId);
            if (!acct || !acct.panel_user_id) {
                console.warn(`${eventType}: missing account or panel user_id`, subId);
                return;
            }
            try {
                await d.setDeviceStatus({ user_id: acct.panel_user_id, status: "disable" });
            } catch (e) {
                console.warn(`${eventType}: disable failed:`, (e as Error).message);
            }
            acct.status = "expired";
            acct.cancel_at_period_end = false;
            await d.putAccount(acct);
            console.log(`${eventType}: disabled panel account for`, subId);
            return;
        }

        case "BILLING.SUBSCRIPTION.PAYMENT.FAILED": {
            const acct = await d.getAccount(subId);
            if (!acct) return;
            acct.status = "payment_failed";
            await d.putAccount(acct);
            const tmpl = d.paymentFailedEmail({
                email: acct.email,
                update_url: "https://www.paypal.com/myaccount/autopay/",
            });
            try { await d.sendEmail({ to: acct.email, ...tmpl }); }
            catch (e) { /* non-fatal */ }
            console.log("PAYMENT.FAILED: flagged", subId);
            return;
        }

        default:
            console.log("Webhook: ignoring event_type", eventType);
            return;
    }
}

/** Random 12-char site password (alphanumeric). The customer gets
 *  this in the welcome email; they can change it later (TODO). */
function generateSitePassword(): string {
    const alpha = "abcdefghijkmnpqrstuvwxyz23456789"; // no confusing chars
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += alpha[bytes[i] % alpha.length];
    return s;
}
