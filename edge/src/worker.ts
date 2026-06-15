/**
 * Tree Frog Streams — edge router.
 *
 * Hot path:
 *   GET /s/<token>   → 302 redirect to the live source URL stored in KV.
 *                      1 KV read, no parsing, no Python, no DB.
 *
 * Static site:
 *   /                  → index.html
 *   /channel.html      → channel detail page
 *   /playlist.html     → playlist + setup guide
 *   /admin/            → admin SPA
 *   /assets/*          → JS, SVG
 *
 * The worker intentionally does NOT:
 *   - Proxy or restream (zero bandwidth cost).
 *   - Log full URLs (privacy + cost).
 *   - Run any business logic (consolidation, health checks, etc.).
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
  // Optional override; when unset we proxy /api/* to the same origin
  // (which works once a Cloudflare Tunnel routes /api/* to the engine).
  ENGINE_API_URL?: string;
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

    // ---- /api/*: pass through to the engine admin API
    if (path.startsWith("/api/")) {
      return handleApiProxy(request, url, env);
    }

    // ---- Static site ----
    return await serveStatic(request, env);

    return new Response("Not found", { status: 404 });
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
 * Proxy /api/* requests to the engine admin API.
 *
 * When ENGINE_API_URL is set (env var), we use it as the upstream.
 * Otherwise we use the request's own origin — this works when a
 * Cloudflare Tunnel routes /api/* traffic to the engine container.
 */
async function handleApiProxy(request: Request, url: URL, env: Env): Promise<Response> {
  const upstream = env.ENGINE_API_URL || url.origin;
  const target = new URL(url.pathname + url.search, upstream);
  const init: RequestInit = {
    method: request.method,
    headers: request.headers,
    body: request.body,
    redirect: "follow",
  };
  try {
    const resp = await fetch(target.toString(), init);
    const headers = new Headers(resp.headers);
    if (target.pathname === "/api/channels.json") {
      headers.set("Cache-Control", `public, max-age=${env.CACHE_TTL_CATALOG ?? "300"}`);
    } else if (target.pathname.endsWith(".m3u")) {
      headers.set("Cache-Control", `public, max-age=${env.CACHE_TTL_PLAYLIST ?? "300"}`);
    }
    return new Response(resp.body, { status: resp.status, headers });
  } catch (e) {
    return new Response(`Upstream unavailable: ${(e as Error).message}`, { status: 502 });
  }
}

/**
 * Tokens are 6 chars, lowercase alphanumeric. Reject anything else
 * before even hitting KV.
 */
function isValidToken(token: string): boolean {
  return /^[a-z0-9]{6}$/.test(token);
}
