/**
 * Tree Frog Streams — edge router.
 *
 * Hot path:
 *   GET /s/<token>   → 302 redirect to the live source URL stored in KV.
 *                      1 KV read, no parsing, no Python, no DB.
 *
 * Player JSON (served from KV, no engine needed at request time):
 *   GET /api/streams/<token>         → KV.get("streams:<token>")
 *                                      (channel meta + ordered list of
 *                                      online stream URLs for failover
 *                                      + per-URL cors_ok flags so the
 *                                      player knows which to fetch
 *                                      direct vs. via the proxy)
 *   GET /api/epg/nownext/<tvg_id>    → KV.get("epg:nownext:<tvg_id>")
 *                                      (on-now / up-next program JSON)
 *
 * CORS proxy (for streams whose origin doesn't return an
 * Access-Control-Allow-Origin header that allows free.tfplus.stream):
 *   GET /api/proxy?u=<encoded upstream URL>
 *     → fetches the upstream, re-emits it with CORS headers. For
 *       m3u8 manifests, rewrites relative segment URLs to also go
 *       through the proxy so the segments themselves are reachable
 *       from the browser. For binary TS segments, streams them
 *       through as-is with the right content-type.
 *
 * Public read path (served from KV, no engine needed at request time):
 *   GET /api/channels.json  → KV.get("catalog:channels.json")
 *   GET /playlist.m3u       → KV.get("catalog:playlist.m3u")
 *
 * Static site (served from the ASSETS binding):
 *   /                  → index.html
 *   /channel.html      → channel detail page
 *   /playlist.html     → playlist + setup guide
 *   /assets/*          → JS, SVG, hls.js pinned at 1.5.0
 *
 * The admin UI used to live at /admin/ in the static assets, but the
 * engine now serves it itself (Tailscale-only, with the bearer token
 * injected as a <meta> tag). The Worker cannot reach Tailscale from
 * the edge, so we 410 Gone any /admin* path here — anyone who lands
 * on the public URL gets a clear "moved" message instead of a broken
 * page that tries to call /api/admin/* over the public Worker.
 *
 * See plan.md §7 for the design rationale.
 */

export interface Env {
  STREAM_KV: KVNamespace;
  // Workers Assets binding — auto-injected by the [assets] config in wrangler.toml.
  // It's a Fetcher that serves files from the public/ directory.
  ASSETS: Fetcher;
  CACHE_TTL_CATALOG: string;
  CACHE_TTL_PLAYLIST: string;
  CACHE_TTL_STATIC: string;
}

async function serveStatic(
  request: Request,
  env: Env,
): Promise<Response> {
  // The modern Assets binding is a Fetcher. Pass the original request
  // through; the binding handles content-type, ETags, range requests,
  // and the 404 fallback itself.
  const resp = await env.ASSETS.fetch(request);
  // If we got a 404, add a small cache header so we don't hammer the
  // asset binding for missing files. Real assets get the static TTL.
  if (resp.status === 404) {
    return new Response("Not found", { status: 404, headers: { "Cache-Control": "public, max-age=60" } });
  }
  // Add caching for successful responses. Don't override any existing
  // cache-control from the asset binding.
  const headers = new Headers(resp.headers);
  if (!headers.has("Cache-Control")) {
    headers.set("Cache-Control", `public, max-age=${env.CACHE_TTL_STATIC ?? "3600"}`);
  }
  return new Response(resp.body, { status: resp.status, headers });
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // ---- /s/<token>: the redirect hot path ----
    if (path.startsWith("/s/")) {
      return handleStreamRedirect(path, env);
    }

    // ---- /admin*: the admin UI moved to the engine (Tailscale only) ----
    if (path === "/admin" || path === "/admin/" || path.startsWith("/admin/")) {
      return new Response(
        "The admin UI is no longer served from the public Worker. " +
        "Reach it over Tailscale at http://100.81.208.64:8000/admin/ " +
        "(or whichever Tailscale IP the engine reports). " +
        "The engine injects the bearer token as a <meta> tag so the " +
        "dashboard is authenticated via the same ADMIN_TOKEN you put " +
        "in engine/.env.",
        {
          status: 410,
          headers: {
            "Content-Type": "text/plain; charset=utf-8",
            // Cache the Gone for a day so we don't keep re-handling it
            // (and so a stale browser tab stops retrying).
            "Cache-Control": "public, max-age=86400",
            "Retry-After": "86400",
          },
        },
      );
    }

    // ---- Public read path: served from KV, cached at the edge ----
    if (path === "/api/channels.json") {
      return handlePublicAsset("catalog:channels.json", "application/json; charset=utf-8", env);
    }
    if (path === "/playlist.m3u") {
      return handlePublicAsset("catalog:playlist.m3u", "audio/x-mpegurl; charset=utf-8", env);
    }

    // ---- Player JSON endpoints ----
    if (path.startsWith("/api/streams/")) {
      return handlePlayerJson(
        path.slice("/api/streams/".length),
        "streams:",
        env,
        (token) => isValidStreamToken(token),
        "Invalid token",
      );
    }
    if (path.startsWith("/api/epg/nownext/")) {
      return handlePlayerJson(
        path.slice("/api/epg/nownext/".length),
        "epg:nownext:",
        env,
        (id) => isValidTvgId(id),
        "Invalid tvg_id",
      );
    }

    // ---- CORS proxy ----
    // Player-side fallback for streams whose origin doesn't allow
    // cross-origin fetches from free.tfplus.stream. The player uses
    // the per-URL `cors_ok` flag in the streams:<token> payload to
    // decide which URLs to route through here. See handleCorsProxy.
    if (path === "/api/proxy") {
      return handleCorsProxy(request, env);
    }

    // ---- Static site ----
    return await serveStatic(request, env);
  },
} satisfies ExportedHandler<Env>;

/**
 * Look up the active source URL for a redirect token and 302 to it.
 * On miss, return 410 Gone with a Retry-After so clients (and scrapers)
 * know it's a dead channel, not a typo.
 */
async function handleStreamRedirect(path: string, env: Env): Promise<Response> {
  const token = path.slice("/s/".length);
  if (!isValidToken(token)) {
    return new Response("Invalid token", { status: 400 });
  }
  const target = await env.STREAM_KV.get(token);
  if (!target) {
    return new Response("Stream offline", {
      status: 410,
      headers: {
        "Retry-After": "1800",
        "Cache-Control": "no-store",
      },
    });
  }
  // 302 (not 301) — players cache less aggressively, so a stream swap
  // propagates to existing clients on the next poll.
  return new Response(null, {
    status: 302,
    headers: {
      Location: target,
      "Cache-Control": "no-cache, no-store, must-revalidate",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

/**
 * Serve a public read asset that the engine has pushed to KV. The engine
 * writes these on every cycle (with diff-and-skip), so the Worker never
 * needs to contact the engine at request time.
 */
async function handlePublicAsset(
  kvKey: string,
  contentType: string,
  env: Env,
): Promise<Response> {
  const value = await env.STREAM_KV.get(kvKey);
  if (!value) {
    return new Response(
      "Not yet published. The engine writes this on its first cycle after a channel is discovered.",
      {
        status: 503,
        headers: {
          "Retry-After": "60",
          "Cache-Control": "no-store",
        },
      },
    );
  }
  // Use the catalog TTL for JSON, playlist TTL for M3U. KV reads are
  // uncached at the CF level for free-tier namespaces, so this header
  // lets the browser cache between cycles.
  const ttl = contentType.startsWith("application/json")
    ? (env.CACHE_TTL_CATALOG ?? "300")
    : (env.CACHE_TTL_PLAYLIST ?? "300");
  return new Response(value, {
    status: 200,
    headers: {
      "Content-Type": contentType,
      "Cache-Control": `public, max-age=${ttl}`,
      "Access-Control-Allow-Origin": "*",
    },
  });
}

/**
 * Tokens are 6 chars, lowercase alphanumeric. Reject anything else
 * before even hitting KV.
 */
function isValidToken(token: string): boolean {
  return /^[a-z0-9]{6}$/.test(token);
}

/**
 * /api/streams/<token> uses the same token shape as /s/<token>.
 */
function isValidStreamToken(token: string): boolean {
  return isValidToken(token);
}

/**
 * /api/epg/nownext/<tvg_id> — XMLTV tvg-ids can contain a wide
 * variety of characters (parens, dots, spaces, slashes, etc.). We
 * require non-empty and a sane length cap so a malicious caller
 * can't blow up the KV key prefix.
 */
function isValidTvgId(id: string): boolean {
  if (!id) return false;
  if (id.length > 256) return false;
  // Reject control characters and path-traversal attempts. The key
  // is `epg:nownext:<id>` so the only structural risk is `..` or
  // embedded `/` (which would be a different Worker route, not a
  // KV traversal — but defense in depth).
  if (/[\x00-\x1f]/.test(id)) return false;
  if (id.includes("/") || id.includes("..")) return false;
  return true;
}

/**
 * Generic KV-JSON lookup for the player's two endpoints.
 *
 *   <keyPrefix><param>  → JSON value at that key in STREAM_KV
 *
 * On miss: 404 with a short body and Cache-Control: no-store (so the
 * client retries, but we don't keep returning the same 404 from
 * CF's edge cache for a token that may not exist for long).
 *
 * On hit: JSON with the catalog TTL — same as the existing
 * /api/channels.json path.
 *
 * `validate` is run before we touch KV to avoid giving an attacker
 * a cheap reflection probe.
 */
async function handlePlayerJson(
  param: string,
  keyPrefix: string,
  env: Env,
  validate: (s: string) => boolean,
  invalidMessage: string,
): Promise<Response> {
  if (!validate(param)) {
    return new Response(invalidMessage, { status: 400 });
  }
  const key = keyPrefix + param;
  const value = await env.STREAM_KV.get(key);
  if (!value) {
    return new Response("Not found", {
      status: 404,
      headers: { "Cache-Control": "no-store" },
    });
  }
  const ttl = parseInt(env.CACHE_TTL_CATALOG ?? "300", 10);
  return new Response(value, {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": `public, max-age=${ttl}`,
      "Access-Control-Allow-Origin": "*",
    },
  });
}

/**
 * Maximum upstream URL length the proxy will accept. Real HLS segment
 * URLs are typically 200-500 chars; 2048 covers the long tail and
 * blocks obviously-attack-shaped inputs before we touch `fetch()`.
 */
const PROXY_MAX_URL_LENGTH = 2048;

/**
 * Handle a request to /api/proxy?u=<encoded upstream URL>.
 *
 * Use case: a stream's origin doesn't return
 * `Access-Control-Allow-Origin: https://free.tfplus.stream`, so the
 * browser blocks the manifest/segment fetch even though the bytes
 * are fine. The player's stream list (`streams:<token>`) carries a
 * per-URL `cors_ok` flag; when it's false (or null — unknown is the
 * safe default), the player rewrites the URL to go through here.
 *
 * What this does:
 *  1. Validates `u` is http(s) and within the size cap.
 *  2. Forwards the request (with a few realistic browser-shaped
 *     headers so a CDN that gates on User-Agent doesn't 403 us).
 *  3. Streams the response body back with CORS headers.
 *  4. For m3u8 manifests: rewrites every relative URL line to also
 *     go through the proxy, so segment fetches are reachable from
 *     the browser too. Absolute URLs are passed through unchanged
 *     (hls.js will fetch them; if they're also CORS-blocked, the
 *     player will see a per-segment error and surface it).
 *
 * Cost note: every segment on every play goes through the Worker.
 * For the free tier (100k req/day) this caps concurrent plays at
 * ~1-2 channels; if the operator wants more, they upgrade to
 * Workers Paid ($5/mo, 10M req). The per-URL `cors_ok` flag means
 * only CORS-blocked sources pay this cost — direct-OK URLs go
 * straight to the origin.
 */
async function handleCorsProxy(
  request: Request,
  _env: Env,
): Promise<Response> {
  // CORS preflight: if a browser asks "can I POST/GET here with
  // these headers?" we answer before the real request. hls.js
  // doesn't send a preflight for m3u8 GETs, but the public site
  // itself might.
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type",
        "Access-Control-Max-Age": "86400",
      },
    });
  }

  const url = new URL(request.url);
  const u = url.searchParams.get("u");
  if (!u) {
    return new Response("Missing ?u=", {
      status: 400,
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  }
  if (u.length > PROXY_MAX_URL_LENGTH) {
    return new Response("URL too long", {
      status: 414,
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  }
  let upstream: URL;
  try {
    upstream = new URL(u);
  } catch {
    return new Response("Invalid URL", {
      status: 400,
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  }
  if (upstream.protocol !== "http:" && upstream.protocol !== "https:") {
    return new Response("Unsupported protocol", {
      status: 400,
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  }

  // Range header passthrough for TS segments (hls.js uses
  // Range: bytes=N- on some players). We never strip headers the
  // browser sent — just add ours on the response.
  const reqHeaders = new Headers();
  reqHeaders.set(
    "User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  );
  reqHeaders.set("Accept", "*/*");
  if (request.headers.has("Range")) {
    reqHeaders.set("Range", request.headers.get("Range")!);
  }

  let upstreamResp: Response;
  try {
    upstreamResp = await fetch(upstream.toString(), {
      method: "GET",
      headers: reqHeaders,
      // Don't auto-follow redirects to private network addresses —
      // CF's fetch() already blocks loopback/private IPs by default
      // in production, so this is defense in depth.
      redirect: "follow",
    });
  } catch (e) {
    return new Response(
      `Upstream fetch failed: ${(e as Error).message}`,
      {
        status: 502,
        headers: { "Access-Control-Allow-Origin": "*" },
      },
    );
  }

  const contentType = upstreamResp.headers.get("Content-Type") ?? "";
  const isM3U8 =
    contentType.includes("mpegurl") ||
    upstream.pathname.endsWith(".m3u8") ||
    upstream.pathname.endsWith(".m3u");

  // For m3u8: rewrite URLs inside the manifest so segments also
  // proxy through us (and get CORS headers). For everything else
  // (TS segments, fMP4 init segments, etc.) just stream it through.
  let body: ReadableStream<Uint8Array> | string;
  if (isM3U8 && upstreamResp.body) {
    const text = await upstreamResp.text();
    body = rewriteM3U8(text, upstream);
  } else if (upstreamResp.body) {
    body = upstreamResp.body;
  } else {
    body = "";
  }

  // Mirror useful upstream headers, but override CORS.
  const outHeaders = new Headers();
  if (isM3U8) {
    outHeaders.set("Content-Type", "application/vnd.apple.mpegurl");
  } else if (contentType) {
    outHeaders.set("Content-Type", contentType);
  } else {
    outHeaders.set("Content-Type", "application/octet-stream");
  }
  if (upstreamResp.headers.has("Content-Length") && !isM3U8) {
    outHeaders.set(
      "Content-Length",
      upstreamResp.headers.get("Content-Length")!,
    );
  }
  if (upstreamResp.headers.has("Content-Range")) {
    outHeaders.set(
      "Content-Range",
      upstreamResp.headers.get("Content-Range")!,
    );
  }
  outHeaders.set("Access-Control-Allow-Origin", "*");
  outHeaders.set("Access-Control-Allow-Methods", "GET, OPTIONS");
  outHeaders.set("Access-Control-Allow-Headers", "Range, Content-Type");
  // m3u8s are small and time-sensitive; cache them briefly so we
  // don't hammer the origin on every segment fetch. Segments are
  // huge and time-sensitive too, but the upstream usually returns
  // its own Cache-Control — let it through.
  if (isM3U8) {
    outHeaders.set("Cache-Control", "public, max-age=10");
  } else if (upstreamResp.headers.has("Cache-Control")) {
    outHeaders.set(
      "Cache-Control",
      upstreamResp.headers.get("Cache-Control")!,
    );
  }

  return new Response(body, {
    status: upstreamResp.status,
    headers: outHeaders,
  });
}

/**
 * Rewrite an m3u8 manifest so every URL line (segment, key, etc.)
 * is either left alone (if it's already an absolute http(s) URL)
 * or wrapped to also go through the Worker's CORS proxy. The base
 * URL is the upstream manifest URL — used to resolve relative
 * paths to absolute ones, which is what the browser would do if
 * it were fetching directly.
 *
 * We rewrite EVERY URL (absolute and relative) through the proxy.
 * An absolute URL that already has CORS would technically not need
 * it, but routing uniformly is simpler and the proxy is a thin
 * pass-through with the same Content-Type/CORS headers every time.
 * The player's `cors_ok` flag decides which URLs come here in the
 * first place, so this only sees URLs that needed help.
 *
 * Line types we touch:
 *   - Lines starting with `#` (and not `#EXTM3U`) are directives.
 *     Most pass through; `#EXT-X-KEY` URIs need rewriting too if
 *     the key server is also CORS-blocked. We rewrite the URI
 *     attribute inline.
 *   - Other lines are URLs (segment, key, variant playlist, etc.)
 *     and always need absolute resolution.
 */
function rewriteM3U8(manifest: string, base: URL): string {
  const proxiedBase = `/api/proxy?u=`;
  const lines = manifest.split(/\r?\n/);
  const out: string[] = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (line === "" || line.startsWith("#EXTM3U")) {
      out.push(raw);
      continue;
    }
    if (line.startsWith("#")) {
      // #EXT-X-KEY URI="https://..." — rewrite the URI attribute
      // if present, leave METHOD / IV etc. alone.
      if (/URI="([^"]+)"/.test(line)) {
        out.push(
          raw.replace(/URI="([^"]+)"/g, (_match, uri: string) => {
            const abs = makeAbsolute(uri, base);
            return `URI="${proxiedBase}${encodeURIComponent(abs)}"`;
          }),
        );
        continue;
      }
      out.push(raw);
      continue;
    }
    // Plain URL line (segment, key, etc.)
    const abs = makeAbsolute(line, base);
    out.push(`${proxiedBase}${encodeURIComponent(abs)}`);
  }
  return out.join("\n");
}

/**
 * Resolve a possibly-relative URL against the manifest's base. If
 * the input is already absolute, return it verbatim. Otherwise,
 * resolve against `base` the way a browser would.
 */
function makeAbsolute(maybe: string, base: URL): string {
  if (/^https?:\/\//i.test(maybe)) return maybe;
  return new URL(maybe, base).toString();
}
