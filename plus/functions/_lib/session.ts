/** Session helpers.
 *
 *  We use a 32-byte random hex token stored in a cookie
 *  (`tfp_sess`) and looked up in KV under `session:{token}`.
 *  This keeps the cookie opaque and lets us revoke individual
 *  sessions by deleting the KV key. The KV value has a TTL
 *  matching `exp`, so abandoned sessions are auto-cleaned.
 *
 *  Password hashing uses PBKDF2-SHA-256 with 100k iterations
 *  and a per-account random 16-byte salt. The cleartext password
 *  is only ever needed once: the welcome email. The KV record
 *  stores both the cleartext (for Gold Panel renew) and the
 *  PBKDF2 hash (for site login). Treat the cleartext as a
 *  sensitive secret — leak only via the welcome email.
 */

import { getAccountBySub, type Account, type Session } from "./kv";

const COOKIE_NAME = "tfp_sess";
const SESSION_TTL_SEC = 60 * 60 * 24; // 24h
const PBKDF2_ITERATIONS = 100_000;

/** Hex-encode a byte array. */
function toHex(buf: ArrayBuffer): string {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) {
        s += bytes[i].toString(16).padStart(2, "0");
    }
    return s;
}

/** Cryptographically-strong random hex string of N bytes. */
export function randomToken(bytes = 32): string {
    const buf = new Uint8Array(bytes);
    crypto.getRandomValues(buf);
    return toHex(buf.buffer);
}

export async function hashPassword(password: string, saltHex?: string): Promise<{
    salt: string;
    hash: string;
    iterations: number;
}> {
    const iterations = PBKDF2_ITERATIONS;
    const enc = new TextEncoder();
    const saltBytes = saltHex
        ? Uint8Array.from(saltHex.match(/.{2}/g)!.map((h) => parseInt(h, 16)))
        : crypto.getRandomValues(new Uint8Array(16));
    const keyMaterial = await crypto.subtle.importKey(
        "raw",
        enc.encode(password),
        "PBKDF2",
        false,
        ["deriveBits"],
    );
    const bits = await crypto.subtle.deriveBits(
        {
            name: "PBKDF2",
            salt: saltBytes,
            iterations,
            hash: "SHA-256",
        },
        keyMaterial,
        256,
    );
    return {
        salt: toHex(saltBytes.buffer),
        hash: toHex(bits),
        iterations,
    };
}

export async function verifyPassword(
    password: string,
    auth: { salt: string; hash: string; iterations: number },
): Promise<boolean> {
    const { hash } = await hashPassword(password, auth.salt);
    return timingSafeEqual(hash, auth.hash);
}

/** Constant-time string comparison (small strings, but still). */
function timingSafeEqual(a: string, b: string): boolean {
    if (a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
}

/** Read the session token from the request's `Cookie` header. */
function readCookie(req: Request): string | null {
    const header = req.headers.get("Cookie") || "";
    for (const part of header.split(/;\s*/)) {
        const eq = part.indexOf("=");
        if (eq < 0) continue;
        const k = part.slice(0, eq).trim();
        if (k === COOKIE_NAME) return decodeURIComponent(part.slice(eq + 1));
    }
    return null;
}

/** Build a Set-Cookie header value for a fresh session. */
export function buildSessionCookie(token: string, maxAge: number): string {
    return [
        `${COOKIE_NAME}=${encodeURIComponent(token)}`,
        `HttpOnly`,
        `Secure`,
        `SameSite=Lax`,
        `Path=/`,
        `Max-Age=${maxAge}`,
    ].join("; ");
}

/** Build a Set-Cookie that clears the session. */
export function clearSessionCookie(): string {
    return [
        `${COOKIE_NAME}=`,
        `HttpOnly`,
        `Secure`,
        `SameSite=Lax`,
        `Path=/`,
        `Max-Age=0`,
    ].join("; ");
}

/** Read the session, look up the account, return both. */
export async function getSessionAccount(
    req: Request,
    kv: KVNamespace,
): Promise<{ account: Account; session: Session } | null> {
    const token = readCookie(req);
    if (!token) return null;
    const raw = await kv.get(`session:${token}`);
    if (!raw) return null;
    const session = JSON.parse(raw) as Session;
    if (session.exp * 1000 < Date.now()) {
        await kv.delete(`session:${token}`);
        return null;
    }
    const account = await getAccountBySub(kv, session.order_id);
    if (!account) return null;
    return { account, session };
}

/** Issue a fresh session for the given account. Returns the token
 *  and the Set-Cookie header the caller should attach. */
export async function createSession(
    kv: KVNamespace,
    orderId: string,
): Promise<{ token: string; cookie: string }> {
    const token = randomToken(32);
    const exp = Math.floor(Date.now() / 1000) + SESSION_TTL_SEC;
    const session: Session = { order_id: orderId, exp };
    await kv.put(`session:${token}`, JSON.stringify(session), {
        expirationTtl: SESSION_TTL_SEC,
    });
    return { token, cookie: buildSessionCookie(token, SESSION_TTL_SEC) };
}

/** Delete the current session and return the clearing cookie. */
export async function destroySession(req: Request, kv: KVNamespace): Promise<string> {
    const token = readCookie(req);
    if (token) await kv.delete(`session:${token}`);
    return clearSessionCookie();
}
