/** POST /api/auth/login
 *
 *  Body: { username, password }
 *  Returns: { ok: true } + Set-Cookie: tfp_sess=...
 *
 *  The username and password ARE the Gold Panel credentials.
 *  We verify locally against a PBKDF2 hash of the panel
 *  password (stored in KV on signup). If the local hash is
 *  missing or doesn't match (e.g. the customer had their
 *  Gold Panel password reset by the operator), we fall back
 *  to a Gold Panel `action=device_info` call — if that
 *  succeeds, we accept the login, refresh the local hash
 *  and the encrypted panel password, and (if there was no
 *  local account) create one.
 *
 *  This means existing Gold Panel customers can sign in
 *  with their existing creds without going through PayPal
 *  — but a PayPal-paid signup is the only path that
 *  *creates* a Gold Panel account. If the operator wants
 *  to seed accounts by hand, they can pre-populate an
 *  Account record in KV with the right panel_username +
 *  password_auth hash. */

import { getAccountByPanelUsername, putAccount, type Account, type ContactHandles } from "../../_lib/kv";
import { getDeviceInfo } from "../../_lib/goldpanel";

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

interface LoginBody {
    username: string;
    password: string;
}

export const onRequestPost = async (ctx: PagesContext): Promise<Response> => {
    if (ctx.request.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
    }
    let body: LoginBody;
    try { body = await ctx.request.json() as LoginBody; }
    catch (e) { return json({ error: "Invalid JSON" }, 400); }
    const username = String(body?.username || "").trim();
    const password = String(body?.password || "");
    if (!username || !password) {
        return json({ error: "Username and password are required" }, 400);
    }

    const kv = ctx.env.PLUS_KV as KVNamespace;
    const { verifyPassword, hashPassword, createSession } = await import("../../_lib/session");
    const { encryptSecret } = await import("../../_lib/crypto");

    // 1. Look up locally by Gold Panel username.
    let acct = await getAccountByPanelUsername(kv, username);

    // 2. If we have a local record, verify the hash. If it
    //    matches, sign in. If it doesn't match, try the
    //    Gold Panel fallback (the customer may have had
    //    their panel password reset by the operator).
    if (acct && acct.password_auth) {
        const ok = await verifyPassword(password, acct.password_auth);
        if (ok) {
            const { cookie } = await createSession(kv, acct.paypal_order_id);
            return sessionResponse(cookie);
        }
        // Local hash miss — fall through to Gold Panel check.
    }

    // 3. Verify against Gold Panel directly. If it works,
    //    either refresh the local hash (acct exists) or
    //    create a new local record (existing Gold Panel
    //    customer, first sign-in to the site).
    let info: any;
    try {
        info = await getDeviceInfo({ username, password });
    } catch (e) {
        // Constant-time-ish: still hash to avoid timing oracle
        // when there's no local record.
        if (!acct) {
            await hashPassword(password);
        }
        return json({ error: "Invalid username or password" }, 401);
    }

    const newAuth = await hashPassword(password);
    const newCt = await encryptSecret(ctx.env, password);

    if (acct) {
        // Refresh the local hash, the encrypted panel
        // password, and the panel_user_id (in case the
        // operator reset the password in Gold Panel).
        acct.password_auth = newAuth;
        acct.panel_password_ct = newCt;
        acct.panel_user_id = String(info.user_id || acct.panel_user_id || "");
        acct.panel_username = username;
        // If the panel still says the line is alive, mark
        // the local status as active; otherwise leave it
        // alone (admin may have explicitly expired it).
        if (info.expire && !acct.expires_at) {
            acct.expires_at = info.expire;
        }
        // Stash the login time. Step 3 will add the
        // 30-day-staleness refresh logic on top of this.
        acct.last_login_at = new Date().toISOString();
        await putAccount(kv, acct);
        const { cookie } = await createSession(kv, acct.paypal_order_id);
        return sessionResponse(cookie);
    }

    // Brand-new local record for an existing Gold Panel
    // customer. We don't have a PayPal order id, so we
    // generate a stable synthetic one from the panel
    // user_id. This lets the rest of the system (which
    // keys by `paypal_order_id`) work without special
    // cases.
    const syntheticOrderId = `GP-${info.user_id}`;
    const newAcct: Account = {
        paypal_order_id: syntheticOrderId,
        latest_capture_id: null,
        renewal_order_ids: [],
        pending_renewal_order_id: null,
        pending_renewal_months: null,
        name: "",         // unknown — operator can fill in if needed
        email: "",        // unknown — same
        contact: { discord: null, telegram: null, reddit: null } as ContactHandles,
        panel_user_id: String(info.user_id || ""),
        panel_username: username,
        panel_password_ct: newCt,
        password_auth: newAuth,
        // The plan is technically unknown for an existing
        // customer; default to 1 month so renewals still
        // work via the 1-month rate. The operator can fix
        // this if it matters.
        plan_months: 1,
        // Same for bouquet — leave "us_wo" as a placeholder.
        // Step 4 (bouquet matching) will overwrite this on
        // first sign-in once we can compare info.bouquet
        // against our 4 known IDs.
        bouquet: "us_wo",
        panel_bouquet_id: "",
        created_at: new Date().toISOString(),
        expires_at: info.expire || null,
        last_login_at: new Date().toISOString(),
        last_paypal_charge_at: null,    // GP-only customer — no PayPal history
        status: "active",
        cancel_at_period_end: false,
    };
    await putAccount(kv, newAcct);
    const { cookie } = await createSession(kv, newAcct.paypal_order_id);
    return sessionResponse(cookie);
};

function sessionResponse(cookie: string): Response {
    return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie": cookie,
        },
    });
}

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
