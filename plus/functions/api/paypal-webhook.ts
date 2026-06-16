/** POST /api/paypal-webhook
 *
 *  Receives PayPal lifecycle events for the ORDERS API
 *  (one-time payments — no subscriptions). Each event is
 *  verified against PayPal's webhook signature, deduplicated
 *  by event_id, and dispatched to the right handler.
 *
 *  Events we handle:
 *   CHECKOUT.ORDER.APPROVED       — buyer approved, we capture
 *   PAYMENT.CAPTURE.COMPLETED     — money in → provision/extend
 *   PAYMENT.CAPTURE.DENIED        — payment failed
 *   PAYMENT.CAPTURE.REFUNDED      — capture was refunded
 *
 *  The custom_id we set on the order tells us what to do:
 *    "{months}|{bouquet}"   — initial checkout, create the line
 *    "renew|{order_id}|{months}" — renewal, extend the line
 *
 *  Flow for the initial checkout:
 *   APPROVED → CAPTURE (server-side)
 *   CAPTURE.COMPLETED → create Gold Panel M3U line, store
 *                       creds, send welcome email
 *
 *  Flow for a renewal:
 *   APPROVED → CAPTURE
 *   CAPTURE.COMPLETED → Gold Panel `action=renew`, update
 *                       expires_at, send renewal receipt
 */

import { verifyWebhookSignature, captureOrder } from "../_lib/paypal";
import { claimEventId, getAccountBySub, putAccount, type Account } from "../_lib/kv";
import { bouquetToPanelId, type BouquetId } from "../_lib/plans";
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
            captureOrder: (id: string) => captureOrder(kv, id),
            getAccount: (id: string) => getAccountBySub(kv, id),
            putAccount: (a: Account) => putAccount(kv, a),
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
    captureOrder: (id: string) => Promise<any>;
    getAccount: (id: string) => Promise<Account | null>;
    putAccount: (a: Account) => Promise<void>;
    bouquetToPanelId: (b: BouquetId) => string;
    createM3U: (opts: { sub: 3 | 6 | 12; pack: string; country?: string; notes?: string }) => Promise<any>;
    renewM3U: (opts: { username: string; password: string; sub: 1 | 3 | 6 | 12 }) => Promise<any>;
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
    // The order id lives in different places depending on the event:
    //   CHECKOUT.ORDER.APPROVED         → resource.id
    //   PAYMENT.CAPTURE.*               → resource.supplementary_data.related_ids.order_id
    const orderId: string =
        resource.id
        || resource?.supplementary_data?.related_ids?.order_id
        || "";
    if (!orderId) {
        console.warn("Webhook event missing order id", eventType, event.id);
        return;
    }

    // The custom_id we set on the order tells us if this is
    // an initial checkout or a renewal, and what to do.
    const customId: string = resource.custom_id
        || resource.purchase_units?.[0]?.custom_id
        || "";
    const custom = parseCustomId(customId);

    switch (eventType) {
        case "CHECKOUT.ORDER.APPROVED": {
            // Buyer approved. Capture server-side. The actual
            // provisioning happens on the CAPTURE.COMPLETED
            // event that follows.
            console.log("ORDER.APPROVED for", orderId, "→ capturing");
            try {
                await d.captureOrder(orderId);
            } catch (e) {
                console.error("ORDER.APPROVED: capture failed:", (e as Error).message);
                throw e;
            }
            return;
        }

        case "PAYMENT.CAPTURE.COMPLETED": {
            const captureId: string = resource.id || "";
            const buyerEmail: string =
                resource.payer?.email_address
                || resource?.payee?.email_address
                || "";
            if (!buyerEmail) {
                console.warn("CAPTURE.COMPLETED: no payer email", orderId);
                // Don't throw — still record the capture id.
            }
            if (custom.kind === "initial") {
                await handleInitialCapture(d, orderId, captureId, custom, buyerEmail);
            } else if (custom.kind === "renewal") {
                await handleRenewalCapture(d, orderId, captureId, custom, buyerEmail);
            } else {
                console.warn("CAPTURE.COMPLETED: unparseable custom_id", customId);
            }
            return;
        }

        case "PAYMENT.CAPTURE.DENIED": {
            // Capture failed (e.g. card declined at capture time).
            const acct = await d.getAccount(orderId);
            if (!acct) {
                console.warn("CAPTURE.DENIED: unknown order", orderId);
                return;
            }
            acct.status = "payment_failed";
            await d.putAccount(acct);
            try {
                const tmpl = d.paymentFailedEmail({
                    email: acct.email,
                    update_url: "https://www.paypal.com/myaccount/autopay/",
                });
                await d.sendEmail({ to: acct.email, ...tmpl });
            } catch (e) { /* non-fatal */ }
            console.log("CAPTURE.DENIED: flagged", orderId);
            return;
        }

        case "PAYMENT.CAPTURE.REFUNDED": {
            const acct = await d.getAccount(orderId);
            if (!acct) {
                console.warn("CAPTURE.REFUNDED: unknown order", orderId);
                return;
            }
            acct.status = "refunded";
            // Disable the Gold Panel line.
            if (acct.panel_user_id) {
                try {
                    await d.setDeviceStatus({ user_id: acct.panel_user_id, status: "disable" });
                } catch (e) {
                    console.warn("CAPTURE.REFUNDED: disable failed:", (e as Error).message);
                }
            }
            // If this was a pending renewal, clear it.
            if (acct.pending_renewal_order_id === orderId) {
                acct.pending_renewal_order_id = null;
                acct.pending_renewal_months = null;
            }
            await d.putAccount(acct);
            console.log("CAPTURE.REFUNDED: account", orderId, "marked refunded");
            return;
        }

        default:
            console.log("Webhook: ignoring event_type", eventType);
            return;
    }
}

/** Parse the custom_id we set on the order.
 *  Returns `{ kind: "initial", months, bouquet }` or
 *  `{ kind: "renewal", account_order_id, months }` or
 *  `{ kind: "unknown" }`. */
function parseCustomId(customId: string):
    | { kind: "initial"; months: 3 | 6 | 12; bouquet: BouquetId }
    | { kind: "renewal"; account_order_id: string; months: 1 | 3 | 6 | 12 }
    | { kind: "unknown" }
{
    if (!customId) return { kind: "unknown" };
    const parts = customId.split("|");
    if (parts.length === 2) {
        const months = parseInt(parts[0], 10) as 3 | 6 | 12;
        const bouquet = parts[1] as BouquetId;
        if (![3, 6, 12].includes(months)) return { kind: "unknown" };
        if (!["us_wo", "us_w", "ca_wo", "ca_w"].includes(bouquet)) return { kind: "unknown" };
        return { kind: "initial", months, bouquet };
    }
    if (parts.length === 3 && parts[0] === "renew") {
        const months = parseInt(parts[2], 10) as 1 | 3 | 6 | 12;
        if (![1, 3, 6, 12].includes(months)) return { kind: "unknown" };
        return { kind: "renewal", account_order_id: parts[1], months };
    }
    return { kind: "unknown" };
}

async function handleInitialCapture(
    d: DispatchDeps,
    orderId: string,
    captureId: string,
    custom: { kind: "initial"; months: 3 | 6 | 12; bouquet: BouquetId },
    buyerEmail: string,
): Promise<void> {
    // If we already provisioned (webhook retry), skip.
    const existing = await d.getAccount(orderId);
    if (existing && existing.status === "active") {
        console.log("CAPTURE.COMPLETED: account already active, skipping", orderId);
        return;
    }
    if (!existing) {
        // Brand-new account. We need a buyer's email to send
        // the welcome message. If it's missing, fetch the
        // order from PayPal.
        let email = buyerEmail;
        if (!email) {
            // We can't pull the order here without making
            // another API call. Log and bail — admin can
            // recover manually by inserting the account
            // record into KV.
            console.error("CAPTURE.COMPLETED: missing buyer email for", orderId);
            return;
        }
        const sitePw = generateSitePassword();
        const panelBouquet = d.bouquetToPanelId(custom.bouquet);
        const acct: Account = {
            paypal_order_id: orderId,
            latest_capture_id: captureId || null,
            renewal_order_ids: [orderId],
            pending_renewal_order_id: null,
            pending_renewal_months: null,
            email: email.toLowerCase(),
            panel_user_id: null,
            panel_username: null,
            panel_password: null,
            site_password: sitePw,
            password_auth: await d.hashPassword(sitePw),
            plan_months: custom.months,
            bouquet: custom.bouquet,
            panel_bouquet_id: panelBouquet,
            created_at: new Date().toISOString(),
            expires_at: null,
            status: "pending",
            cancel_at_period_end: false,
        };
        await d.putAccount(acct);
        // Now create the Gold Panel M3U line.
        await provisionGoldPanel(d, acct);
        return;
    }
    // Pending account that finally got captured.
    if (existing.status === "pending") {
        existing.latest_capture_id = captureId || existing.latest_capture_id;
        if (!existing.renewal_order_ids.includes(orderId)) {
            existing.renewal_order_ids.push(orderId);
        }
        await d.putAccount(existing);
        await provisionGoldPanel(d, existing);
        return;
    }
    console.warn("CAPTURE.COMPLETED: account in unexpected state", orderId, existing.status);
}

async function provisionGoldPanel(d: DispatchDeps, acct: Account): Promise<void> {
    try {
        const created = await d.createM3U({
            sub: acct.plan_months,
            pack: acct.panel_bouquet_id,
            country: "",
            notes: `tfplus:${acct.email}`,
        });
        acct.panel_user_id    = String(created.user_id);
        acct.panel_username   = created.username;
        acct.panel_password   = created.password;
        acct.panel_bouquet_id = acct.panel_bouquet_id || d.bouquetToPanelId(acct.bouquet);
        acct.status = "active";
        // Pull the expiry.
        try {
            const info = await d.getDeviceInfo({
                username: created.username,
                password: created.password,
            });
            acct.expires_at = info.expire || null;
        } catch (e) {
            console.warn("provision: device_info follow-up failed:", (e as Error).message);
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
        console.log("provision: account ready for", acct.email);
    } catch (e) {
        console.error("provision: Gold Panel createM3U failed:", (e as Error).message);
        // The account stays in `pending` — admin can retry by
        // re-issuing the same PayPal Order (it'll be captured
        // again and the dedup will see a fresh event id).
    }
}

async function handleRenewalCapture(
    d: DispatchDeps,
    orderId: string,
    captureId: string,
    custom: { kind: "renewal"; account_order_id: string; months: 1 | 3 | 6 | 12 },
    _buyerEmail: string,
): Promise<void> {
    const acct = await d.getAccount(custom.account_order_id);
    if (!acct) {
        console.warn("renewal: account", custom.account_order_id, "not found for order", orderId);
        return;
    }
    if (!acct.panel_username || !acct.panel_password) {
        console.warn("renewal: account", custom.account_order_id, "not yet provisioned");
        return;
    }
    try {
        await d.renewM3U({
            username: acct.panel_username,
            password: acct.panel_password,
            sub: custom.months,
        });
    } catch (e) {
        console.error("renewal: Gold Panel renewM3U failed:", (e as Error).message);
        throw e; // PayPal will retry.
    }
    try {
        const info = await d.getDeviceInfo({
            username: acct.panel_username,
            password: acct.panel_password,
        });
        acct.expires_at = info.expire || acct.expires_at;
    } catch (e) {
        console.warn("renewal: device_info follow-up failed:", (e as Error).message);
    }
    acct.status = "active";
    acct.cancel_at_period_end = false;
    acct.latest_capture_id = captureId || acct.latest_capture_id;
    if (!acct.renewal_order_ids.includes(orderId)) {
        acct.renewal_order_ids.push(orderId);
    }
    // Clear the pending pointer.
    if (acct.pending_renewal_order_id === orderId) {
        acct.pending_renewal_order_id = null;
        acct.pending_renewal_months = null;
    }
    await d.putAccount(acct);
    if (acct.expires_at) {
        try {
            const tmpl = d.renewalReceiptEmail({
                email: acct.email,
                new_expire: acct.expires_at,
                months_added: custom.months,
            });
            await d.sendEmail({ to: acct.email, ...tmpl });
        } catch (e) { /* non-fatal */ }
    }
    console.log("renewal: account", acct.email, "extended by", custom.months, "→", acct.expires_at);
}

/** Random 12-char site password (alphanumeric). The customer
 *  gets this in the welcome email; they can change it later. */
function generateSitePassword(): string {
    const alpha = "abcdefghijkmnpqrstuvwxyz23456789"; // no confusing chars
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += alpha[bytes[i] % alpha.length];
    return s;
}
