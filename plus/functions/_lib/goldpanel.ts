/** Gold Panel reseller API client.
 *
 *  Reseller base: `https://8k.cms-only.ru/api/api.php`
 *  Every call is a GET with the API key as a query parameter
 *  (per the panel's documented behavior). Responses are a JSON
 *  array; the first element carries the data. We unwrap it.
 *
 *  Xtream Codes API (used by the web player and the proxy) is
 *  served at `https://<server>/player_api.php` — note the path
 *  is the panel's domain, not the `/api/` prefix. The server
 *  host is configured per-account by the Gold Panel admin; in
 *  our setup the customer-facing DNS (`apex.tfplus.stream`,
 *  `comet.tfplus.stream`) is what end-users / the web player
 *  see. The XC API here uses the apex DNS for read operations.
 *
 *  **Critical detail:** the `createM3U` response documented in
 *  the panel's PDF is truncated — it shows
 *  `{status, user_id, notes, country, message}` without
 *  `username` or `password`. The real response *must* include
 *  them (otherwise `renewM3U` and `getDeviceInfo` are
 *  impossible). On every create call we log the *entire*
 *  response (sanitized) and FAIL LOUDLY if username/password
 *  are missing — that way the operator notices immediately
 *  rather than at customer sign-in time.
 */

const PANEL_BASE = "https://8k.cms-only.ru/api/api.php";

export interface M3UCredentials {
    user_id: string;
    username: string;
    password: string;
    /** The M3U URL itself (e.g. http://m3u-domain.com/get.php?...). */
    url?: string;
    /** What the panel returns — kept for logging/debugging. */
    raw: unknown;
}

export interface DeviceInfo {
    username: string;
    password: string;
    /** YYYY-MM-DD. The panel sometimes returns the empty string
     *  for never-expiring lines; treat as null in that case. */
    expire: string | null;
    country: string;
    user_id: string;
    note: string;
    url: string;
}

export interface Bouquet {
    id: string;
    name: string;
}

/** Get the Gold Panel API key from the environment. */
export function getApiKey(env: Record<string, unknown>): string {
    const v = (env as any).GOLDPANEL_API_KEY;
    if (typeof v !== "string" || !v) {
        console.error("GOLDPANEL_API_KEY not found in env. Available keys:", Object.keys(env));
        throw new Error("GOLDPANEL_API_KEY is not configured.");
    }
    return v;
}

/** Lower-level GET against the reseller API. */
async function panelGet<T = unknown>(params: Record<string, string>, env: Record<string, unknown>): Promise<T> {
    const qs = new URLSearchParams({ ...params, api_key: getApiKey(env) });
    const url = `${PANEL_BASE}?${qs.toString()}`;
    let resp: Response;
    try {
        resp = await fetch(url, { method: "GET" });
    } catch (e) {
        throw new Error(`Gold Panel network error: ${(e as Error).message}`);
    }
    if (!resp.ok) {
        throw new Error(`Gold Panel HTTP ${resp.status}`);
    }
    let parsed: unknown;
    try {
        parsed = await resp.json();
    } catch (e) {
        throw new Error("Gold Panel returned non-JSON");
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
        throw new Error("Gold Panel returned empty response");
    }
    const first = parsed[0] as Record<string, unknown>;
    if (first.status && first.status !== "true" && first.status !== true) {
        throw new Error(`Gold Panel error: ${first.message || first.status}`);
    }
    return first as T;
}

/** List custom bouquets configured by the reseller in the panel. */
export async function listBouquets(env: Record<string, unknown>): Promise<Bouquet[]> {
    // The `action=bouquet` response is an array of bouquets, not a
    // single object with `status: true` — fetch it directly.
    const all = await fetch(`${PANEL_BASE}?action=bouquet&api_key=${encodeURIComponent(getApiKey(env))}`);
    const arr = await all.json();
    return (arr as Array<{ id: string; name: string }>).map((b) => ({
        id: String(b.id),
        name: b.name,
    }));
}

/** Reseller info — for monitoring credits. */
export async function resellerInfo(env: Record<string, unknown>): Promise<{ username: string; credits: string; enabled: string }> {
    const data = await panelGet<{ username: string; credits: string; enabled: string }>({
        action: "reseller",
    }, env);
    return data;
}

/** Create a new M3U line.
 *  @param sub  1, 3, 6, or 12
 *  @param pack numeric bouquet id (from listBouquets)
 */
export async function createM3U(opts: {
    sub: 1 | 3 | 6 | 12;
    pack: string;
    country?: string;
    notes?: string;
}, env: Record<string, unknown>): Promise<M3UCredentials> {
    const raw = await panelGet<Record<string, string>>({
        action: "new",
        type: "m3u",
        sub: String(opts.sub),
        pack: opts.pack,
        country: opts.country || "",
        notes: opts.notes || "",
    }, env);
    if (!raw.user_id) {
        throw new Error("Gold Panel createM3U: missing user_id in response");
    }
    // The PDF only documents {status, user_id, notes, country, message};
    // the real response *should* include username + password.
    // If they're missing, surface the raw response so we can see what
    // was actually returned.
    if (!raw.username || !raw.password) {
        console.error(
            "GOLDPANEL: createM3U response missing username/password",
            JSON.stringify(raw),
        );
        throw new Error(
            "Gold Panel createM3U: response missing username/password. " +
            "See Worker logs for the full response shape.",
        );
    }
    return {
        user_id: raw.user_id,
        username: raw.username,
        password: raw.password,
        url: raw.url,
        raw,
    };
}

/** Renew an existing M3U line — adds `sub` months. */
export async function renewM3U(opts: {
    username: string;
    password: string;
    sub: 1 | 3 | 6 | 12;
}, env: Record<string, unknown>): Promise<{ ok: true; raw: unknown }> {
    const raw = await panelGet<Record<string, string>>({
        action: "renew",
        type: "m3u",
        username: opts.username,
        password: opts.password,
        sub: String(opts.sub),
    }, env);
    return { ok: true, raw };
}

export async function getDeviceInfo(opts: {
    username: string;
    password: string;
}, env: Record<string, unknown>): Promise<DeviceInfo> {
    const raw = await panelGet<Record<string, string>>({
        action: "device_info",
        username: opts.username,
        password: opts.password,
    }, env);
    return {
        username:   raw.username,
        password:   raw.password,
        expire:     raw.expire && raw.expire.length > 0 ? raw.expire : null,
        country:    raw.country || "",
        user_id:    raw.user_id,
        note:       raw.note || "",
        url:        raw.url || "",
    };
}

export async function setDeviceStatus(opts: {
    user_id: string;
    status: "enable" | "disable";
}, env: Record<string, unknown>): Promise<{ ok: true; raw: unknown }> {
    const raw = await panelGet<Record<string, string>>({
        action: "device_status",
        id: opts.user_id,
        status: opts.status,
    }, env);
    return { ok: true, raw };
}

// ---------------- Xtream Codes API (for the web player) ----------------

const XC_BASE = "https://apex.tfplus.stream";

/** GET /player_api.php as a customer. Returns parsed JSON. */
export async function xcGet(
    username: string,
    password: string,
    params: Record<string, string> = {},
): Promise<unknown> {
    const qs = new URLSearchParams({
        username,
        password,
        ...params,
    });
    const url = `${XC_BASE}/player_api.php?${qs.toString()}`;
    const resp = await fetch(url, {
        headers: { "User-Agent": "TreeFrogPlus/1.0" },
    });
    if (!resp.ok) {
        throw new Error(`XC API HTTP ${resp.status}`);
    }
    return await resp.json();
}

export interface XCLiveStream {
    num: number;
    name: string;
    stream_type: string;
    stream_id: number;
    stream_icon?: string;
    epg_channel_id?: string;
    category_id?: string;
}

export interface XCLiveCategory {
    category_id: string;
    category_name: string;
}

/** Build a list of live channels the customer can watch.
 *  Returns one entry per channel with one or two playable URLs
 *  (apex primary, comet fallback). */
export async function listLiveChannels(
    username: string,
    password: string,
): Promise<{ channels: Array<{
    id: number;
    name: string;
    logo: string;
    group: string;
    urls: string[];
}> }> {
    const cats = (await xcGet(username, password, {
        action: "get_live_categories",
    })) as XCLiveCategory[];
    const catName = new Map(cats.map((c) => [c.category_id, c.category_name]));
    const streams = (await xcGet(username, password, {
        action: "get_live_streams",
    })) as XCLiveStream[];
    const channels = streams
        .filter((s) => s.stream_type === "live")
        .map((s) => {
            const id = s.stream_id;
            const apex = `${XC_BASE}/live/${encodeURIComponent(username)}/${encodeURIComponent(password)}/${id}.m3u8`;
            const comet = `http://comet.tfplus.stream/live/${encodeURIComponent(username)}/${encodeURIComponent(password)}/${id}.m3u8`;
            return {
                id,
                name: s.name,
                logo: s.stream_icon || "",
                group: catName.get(String(s.category_id || "")) || "",
                urls: [apex, comet],
            };
        })
        // Stable order: group name, then channel name.
        .sort((a, b) => {
            const g = a.group.localeCompare(b.group);
            return g !== 0 ? g : a.name.localeCompare(b.name);
        });
    return { channels };
}
