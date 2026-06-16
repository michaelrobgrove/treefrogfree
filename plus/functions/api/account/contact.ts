/** PUT/POST /api/account/contact
 *
 *  Two flows, chosen by the `kind` field in the body:
 *
 *  1) kind omitted (the default — the existing "Edit profile"
 *     form on the dashboard):
 *     Body: {
 *       name?:    string,
 *       email?:   string,         // changes the by_email index
 *       discord?: string | null,
 *       telegram?:string | null,
 *       reddit?:  string | null,
 *     }
 *     Updates the customer's profile. Never touches the
 *     Gold Panel creds, plan, or status.
 *
 *  2) kind: "renewal_custom" (the "Contact support to renew"
 *     CTA on the dashboard for accounts on a non-standard
 *     Gold Panel bouquet):
 *     Body: {
 *       kind:    "renewal_custom",
 *       message: string,          // what the customer wrote
 *     }
 *     Sends an operator notification to admin@tfplus.stream
 *     with the customer's account info + their message.
 *     Records a "renewal intent" in KV under
 *     `renewal_intent:{order_id}` so the operator can
 *     review the queue. Does NOT modify the Account record
 *     — the dashboard's renew menu stays hidden and the
 *     customer can't self-serve.
 *
 *  Returns: { ok: true } on success, or an error. */

import type { Account, ContactHandles } from "../../_lib/kv";

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

interface ProfileBody {
    name?: string;
    email?: string;
    discord?: string | null;
    telegram?: string | null;
    reddit?: string | null;
}

interface CustomRenewalBody {
    kind: "renewal_custom";
    message: string;
}

type ContactBody = ProfileBody | CustomRenewalBody;

function strip(s: unknown, max: number): string | null {
    if (s === null) return null;
    if (typeof s !== "string") return null;
    const t = s.trim();
    if (!t) return null;
    return t.slice(0, max);
}

function isValidEmail(s: string): boolean {
    if (typeof s !== "string") return false;
    const t = s.trim();
    if (t.length < 3 || t.length > 254) return false;
    const at = t.indexOf("@");
    if (at < 1 || at === t.length - 1) return false;
    if (!t.slice(at + 1).includes(".")) return false;
    return true;
}

export const onRequestPut = async (ctx: PagesContext): Promise<Response> => {
    const kv = ctx.env.PLUS_KV as KVNamespace;
    let body: ContactBody;
    try { body = await ctx.request.json() as ContactBody; }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }

    const { getSessionAccount } = await import("../../_lib/session");

    const sess = await getSessionAccount(ctx.request, kv);
    if (!sess) return json({ error: "Not signed in" }, 401);
    const acct: Account = sess.account;

    // Custom-bouquet renewal request path.
    if ((body as CustomRenewalBody).kind === "renewal_custom") {
        return handleCustomRenewal(ctx, kv, acct, (body as CustomRenewalBody).message);
    }

    return handleProfileUpdate(kv, acct, body as ProfileBody);
};

async function handleProfileUpdate(
    kv: KVNamespace,
    acct: Account,
    body: ProfileBody,
): Promise<Response> {
    const {
        getAccountByEmail,
        putAccount,
        accountByEmailKey,
    } = await import("../../_lib/kv");

    if (typeof body.name === "string") {
        const n = body.name.trim().slice(0, 100);
        if (!n) return json({ error: "Name cannot be empty" }, 400);
        acct.name = n;
    }

    if (typeof body.email === "string") {
        const e = body.email.trim().toLowerCase();
        if (!isValidEmail(e)) {
            return json({ error: "A valid email is required" }, 400);
        }
        if (e !== acct.email) {
            // Don't let the customer take an email that's
            // already in use by a different account.
            const existing = await getAccountByEmail(kv, e);
            if (existing && existing.paypal_order_id !== acct.paypal_order_id) {
                return json({ error: "That email is already in use." }, 409);
            }
            // Drop the old email index, write the new one.
            await kv.delete(accountByEmailKey(acct.email));
            acct.email = e;
        }
    }

    if (body.discord !== undefined || body.telegram !== undefined || body.reddit !== undefined) {
        const next: ContactHandles = {
            discord:  body.discord  !== undefined ? strip(body.discord, 64)  : (acct.contact?.discord  ?? null),
            telegram: body.telegram !== undefined ? strip(body.telegram, 64) : (acct.contact?.telegram ?? null),
            reddit:   body.reddit   !== undefined ? strip(body.reddit, 64)   : (acct.contact?.reddit   ?? null),
        };
        acct.contact = next;
    }

    await putAccount(kv, acct);
    return json({ ok: true });
}

async function handleCustomRenewal(
    ctx: PagesContext,
    kv: KVNamespace,
    acct: Account,
    rawMessage: string,
): Promise<Response> {
    const message = String(rawMessage || "").trim().slice(0, 2000);
    if (!message) {
        return json({ error: "Please include a message describing what you need." }, 400);
    }

    // Record the intent in KV for the operator's review
    // queue. Keyed by the account's order id so a single
    // intent per account is the norm; the operator can
    // delete the key after handling the request.
    const intentKey = `renewal_intent:${acct.paypal_order_id}`;
    const intent = {
        order_id: acct.paypal_order_id,
        customer_email: acct.email,
        customer_name: acct.name,
        panel_username: acct.panel_username,
        panel_user_id: acct.panel_user_id,
        panel_bouquet_id: acct.panel_bouquet_id,
        expires_at: acct.expires_at,
        message,
        created_at: new Date().toISOString(),
    };
    await kv.put(intentKey, JSON.stringify(intent));

    // Email the operator. We don't fail the request if the
    // email send fails — the KV record is the source of
    // truth, the email is just a notification.
    try {
        const email = await import("../../_lib/email");
        const base = String((ctx.env as any).PUBLIC_BASE_URL || "https://beta.tfplus.stream");
        const tmpl = email.customRenewalRequestEmail({
            account_paypal_order_id: acct.paypal_order_id,
            customer_email: acct.email,
            customer_name: acct.name,
            panel_username: acct.panel_username || "(unknown)",
            panel_user_id: acct.panel_user_id || "",
            panel_bouquet_id: acct.panel_bouquet_id || "",
            expires_at: acct.expires_at,
            message,
            dashboard_url: `${base}/dashboard.html`,
        });
        await email.sendEmail({ to: email.adminAddress(), ...tmpl });
    } catch (e) {
        console.error("contact: custom-renewal email failed:", (e as Error).message);
    }

    return json({ ok: true });
}

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}

// Allow POST as an alias for PUT (some fetch helpers only do POST).
export const onRequestPost = onRequestPut;
