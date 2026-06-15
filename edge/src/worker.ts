/**
 * Tree Frog Streams — edge router.
 *
 * Hot path:
 *   GET /s/<token>   → 302 redirect to the live source URL stored in KV.
 *                      1 KV read, no parsing, no Python, no DB.
 *
 * Public read path (served from KV, no engine needed at request time):
 *   GET /api/channels.json  → KV.get("catalog:channels.json")
 *   GET /playlist.m3u       → KV.get("catalog:playlist.m3u")
 *
 * Static site (served from the ASSETS binding):
 *   /                  → index.html
 *   /channel.html      → channel detail page
 *   /playlist.html     → playlist + setup guide
 *   /admin/            → admin SPA
 *   /assets/*          → JS, SVG
 *
 * The admin UI talks to the engine directly over Tailscale (the engine
 * binds to 127.0.0.1:8000 and is never reachable from the public internet).
 * The Worker does not proxy any /api/* traffic.
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

    // ---- Public read path: served from KV, cached at the edge ----
    if (path === "/api/channels.json") {
      return handlePublicAsset("catalog:channels.json", "application/json; charset=utf-8", env);
    }
    if (path === "/playlist.m3u") {
      return handlePublicAsset("catalog:playlist.m3u", "audio/x-mpegurl; charset=utf-8", env);
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
