/** AES-GCM helpers for at-rest encryption of small secrets
 *  (the Gold Panel M3U password, primarily).
 *
 *  Why: the Gold Panel `action=renew` and `action=device_info`
 *  API calls take the customer's cleartext M3U password as a
 *  query-string parameter on every request. We need the
 *  cleartext to make those calls, but we don't want to store
 *  it cleartext in KV (KV compromise, accidental log line,
 *  a future migration script that writes JSON to a public
 *  bucket — all of these would leak customer credentials
 *  with the cleartext form).
 *
 *  Layout:
 *    - Key: 32 raw bytes, kept in the Cloudflare env as
 *      `PANEL_PASSWORD_ENC_KEY` (base64-encoded for the env,
 *      decoded once on first use inside the function). Set
 *      via `wrangler pages secret put`. The key is never
 *      logged, never written to disk on our side.
 *    - Per encryption: a fresh 12-byte random IV.
 *    - Ciphertext = IV || AES-GCM(plaintext) (GCM tag
 *      appended by SubtleCrypto automatically).
 *    - Stored in KV as a single base64 string, prefixed with
 *      "v1:" so we can rotate the format later without
 *      breaking existing records.
 *
 *  Module-level cache: Cloudflare Workers re-use the isolate
 *  across requests in the same region, so decoding the key
 *  once and caching the CryptoKey is a meaningful speedup
 *  and keeps us from leaking the base64 form into any per-
 *  request logs. */

const KEY_ENV = "PANEL_PASSWORD_ENC_KEY";
const KEY_VERSION = "v1:";

let cachedKey: { raw: string; key: CryptoKey } | null = null;

/** Returns the per-isolate CryptoKey, or null if the env var
 *  is missing/invalid. Callers must handle the null case
 *  (treat the account as un-provisioned rather than crash). */
export async function getOrCreateKey(env: Record<string, unknown>): Promise<CryptoKey | null> {
    const raw = String(env[KEY_ENV] || "");
    if (!raw) return null;
    if (cachedKey && cachedKey.raw === raw) return cachedKey.key;
    let bytes: Uint8Array;
    try {
        bytes = base64ToBytes(raw);
    } catch (e) {
        console.error("crypto: PANEL_PASSWORD_ENC_KEY is not valid base64");
        return null;
    }
    if (bytes.length !== 32) {
        console.error("crypto: PANEL_PASSWORD_ENC_KEY must decode to 32 bytes (got", bytes.length, ")");
        return null;
    }
    const key = await crypto.subtle.importKey(
        "raw",
        bytes,
        { name: "AES-GCM" },
        false,                       // not extractable
        ["encrypt", "decrypt"],
    );
    cachedKey = { raw, key };
    return key;
}

/** Encrypts plaintext. Returns the version-prefixed
 *  base64-encoded ciphertext (suitable for direct KV
 *  storage), or null if encryption failed. */
export async function encryptSecret(
    env: Record<string, unknown>,
    plaintext: string,
): Promise<string | null> {
    if (!plaintext) return null;
    const key = await getOrCreateKey(env);
    if (!key) return null;
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const enc = new TextEncoder().encode(plaintext);
    const ct = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        enc,
    );
    const out = new Uint8Array(iv.length + ct.byteLength);
    out.set(iv, 0);
    out.set(new Uint8Array(ct), iv.length);
    return KEY_VERSION + bytesToBase64(out);
}

/** Decrypts a value previously produced by `encryptSecret`.
 *  Returns the plaintext, or null if decryption failed (bad
 *  key, tampered ciphertext, wrong version, etc.). */
export async function decryptSecret(
    env: Record<string, unknown>,
    ciphertext: string | null,
): Promise<string | null> {
    if (!ciphertext) return null;
    if (!ciphertext.startsWith(KEY_VERSION)) {
        // Legacy cleartext value (from before encryption was
        // rolled out). We can't decrypt it, so the caller
        // should treat this as "force re-auth on next login
        // to refresh the encrypted form."
        return null;
    }
    const key = await getOrCreateKey(env);
    if (!key) return null;
    let blob: Uint8Array;
    try {
        blob = base64ToBytes(ciphertext.slice(KEY_VERSION.length));
    } catch (e) {
        return null;
    }
    if (blob.length < 12 + 16) return null; // IV + GCM tag minimum
    const iv = blob.slice(0, 12);
    const ct = blob.slice(12);
    try {
        const pt = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv },
            key,
            ct,
        );
        return new TextDecoder().decode(pt);
    } catch (e) {
        return null;
    }
}

/** Constant-time-ish string compare. Used for the cron
 *  shared-secret check (and any future secret-token compares).
 *  Not a substitute for a real MAC, but enough to remove the
 *  obvious timing oracle for short keys. */
export function timingSafeEqual(a: string, b: string): boolean {
    if (typeof a !== "string" || typeof b !== "string") return false;
    // Pad both sides to the same length so the loop always
    // runs the same number of iterations.
    const len = Math.max(a.length, b.length, 1);
    let diff = a.length ^ b.length;
    for (let i = 0; i < len; i++) {
        const ac = i < a.length ? a.charCodeAt(i) : 0;
        const bc = i < b.length ? b.charCodeAt(i) : 0;
        diff |= ac ^ bc;
    }
    return diff === 0;
}

function bytesToBase64(bytes: Uint8Array): string {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
}

function base64ToBytes(b64: string): Uint8Array {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}
