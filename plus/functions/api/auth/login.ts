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
 *  password_auth hash.
 *
 *  Staleness check: if the customer hasn't signed in for
 *  30+ days (or has never signed in), we deny access with
 *  a 401 error. The session cookie is cleared. This ensures
 *  inactive accounts are locked out and require operator
 *  intervention to reactivate.
 *
 *  Password-change handling: customers cannot change their
 *  Gold Panel password themselves (only the operator can).
 *  If the local hash doesn't match what they typed, we try
 *  the Gold Panel fallback. If that also fails, we deny
 *  with a "contact support" message — never a silent fall-
 *  through to a stale local record. */

import { getAccountByPanelUsername, putAccount, type Account, type ContactHandles } from "../../_lib/kv";
import { getDeviceInfo } from "../../_lib/goldpanel";
import { clearSessionCookie } from "../../_lib/session";

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

interface LoginBody {
    username: string;
    password: string;
}

const STALENESS_MS = 30 * 24 * 60 * 60 * 1000;  // 30 days

/** Returns true if the account's last_login_at is older than 30 days
 *  or if the account has never signed in. This is used to deny
 *  access for stale accounts. */
function isStale(acct: Account | null): boolean {
    if (!acct) return true;
    if (!acct.last_login_at) return true;
    const last = Date.parse(acct.last_login_at);
    if (isNaN(last)) return true;
    return (Date.now() - last) > STALENESS_MS;
}

/** Refresh `expires_at` from Gold Panel. Best-effort: any
 *  failure is logged but doesn't block the sign-in. The
 *  customer is told their dashboard expires date is best-
 *  effort. */
async function refreshExpiresAt(
    kv: KVNamespace,
    acct: Account,
    username: string,
    password: string,
): Promise<void> {
    try {
        const info = await getDeviceInfo({ username, password });
        if (info.expire) {
            acct.expires_at = info.expire;
            await putAccount(kv, acct);
        }
    } catch (e) {
        console.warn("login: stale-refresh getDeviceInfo failed:", (e as Error).message);
    }
}

/** Build a 401 response for stale account with cleared session cookie. */
function staleResponse(requestUrl: string): Response {
    return new Response(JSON.stringify({
        error: "Account inactive. Please contact support to reactivate your account.",
    }), {
        status: 401,
        headers: {
            "Content-Type": "application/json",
            "Set-Cookie": clearSessionCookie(requestUrl),
        },
    });
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

    // 2. If we have a local record, verify the hash.
    if (acct && acct.password_auth) {
        const ok = await verifyPassword(password, acct.password_auth);
        if (ok) {
            // 30-day staleness check: deny access if the
            // account hasn't been used for 30+ days.
            if (isStale(acct)) {
                return staleResponse(ctx.request.url);
            }
            // Refresh expires_at from the panel as a best-effort
            // operation. This keeps the dashboard accurate without
            // an API call on every sign-in.
            await refreshExpiresAt(kv, acct, username, password);
            acct.last_login_at = new Date().toISOString();
            await putAccount(kv, acct);
            const { cookie } = await createSession(kv, acct.paypal_order_id, ctx.request.url);
            return sessionResponse(cookie);
        }
        // Local hash miss — fall through to the Gold Panel
        // check below. This is the "operator reset the
        // password" case.
    }

    // 3. Verify against Gold Panel directly.
    let info: any;
    try {
        info = await getDeviceInfo({ username, password });
    } catch (e) {
        // Constant-time-ish: still hash to avoid timing oracle
        // when there's no local record.
        if (!acct) {
            await hashPassword(password);
        }
        // We never silently fall through to a stale local
        // record. If both the local hash and the panel say
        // "no", deny. Phrase the response so the customer
        // knows to contact support for password-reset help
        // (the operator can reset their Gold Panel line).
        return json({
            error: "Invalid username or password. If you recently had your account password reset, please contact support.",
        }, 401);
    }

    const newAuth = await hashPassword(password);
    const newCt = await encryptSecret(ctx.env, password);

    if (acct) {
        // 30-day staleness check: deny access if the
        // account hasn't been used for 30+ days.
        if (isStale(acct)) {
            return staleResponse(ctx.request.url);
        }
        // Refresh the local hash, the encrypted panel
        // password, and the panel_user_id (in case the
        // operator reset the password in Gold Panel).
        acct.password_auth = newAuth;
        acct.panel_password_ct = newCt;
        acct.panel_user_id = String(info.user_id || acct.panel_user_id || "");
        acct.panel_username = username;
        // Pull the live expiry — the operator may have
        // extended or disabled the line, and we want the
        // dashboard to reflect the current state.
        if (info.expire) acct.expires_at = info.expire;
        acct.last_login_at = new Date().toISOString();
        await putAccount(kv, acct);
        const { cookie } = await createSession(kv, acct.paypal_order_id, ctx.request.url);
        return sessionResponse(cookie);
    }

    // 4. Brand-new local record for an existing Gold Panel
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
        // work via the 1-month rate. Step 4 (bouquet
        // matching) will overwrite this once we can compare
        // info.bouquet against our 4 known IDs.
        plan_months: 1,
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
    const { cookie } = await createSession(kv, newAcct.paypal_order_id, ctx.request.url);
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
