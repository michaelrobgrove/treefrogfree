/** GET /api/cron/renewal-nudges
 *
 *  Cron endpoint to send renewal reminder emails.
 *  Protected by X-Cron-Key header matching CRON_SECRET from env.
 *
 *  Queries all accounts where latest_capture_id !== null
 *  (site-signup customers, not GP-only).
 *
 *  For each account, computes days until expires_at.
 *  If 7, 3, or 0 days remain, sends a renewal reminder email.
 *
 *  Returns 200 on success, 401 on auth failure.
 */

interface PagesContext {
    request: Request;
    env: Record<string, unknown>;
}

export const onRequestGet = async (ctx: PagesContext): Promise<Response> => {
    // Verify cron secret
    const cronSecret = (ctx.env as any).CRON_SECRET;
    if (!cronSecret) {
        console.error("CRON_SECRET not configured");
        return json({ error: "Server misconfiguration" }, 500);
    }

    const providedKey = ctx.request.headers.get("X-Cron-Key");
    if (!providedKey || providedKey !== cronSecret) {
        return json({ error: "Unauthorized" }, 401);
    }

    const kv = ctx.env.PLUS_KV as KVNamespace;

    // List all account keys
    const accounts: any[] = [];
    const listResult = await kv.list({ prefix: "account:" });
    
    for (const item of listResult.keys) {
        const raw = await kv.get(item.name);
        if (raw) {
            try {
                const acct = JSON.parse(raw);
                // Only process site-signup customers (have latest_capture_id)
                if (acct.latest_capture_id !== null) {
                    accounts.push(acct);
                }
            } catch (e) {
                console.error(`Failed to parse account ${item.name}:`, (e as Error).message);
            }
        }
    }

    const base = String((ctx.env as any).PUBLIC_BASE_URL || "https://beta.tfplus.stream");
    let sentCount = 0;

    for (const acct of accounts) {
        const daysRemaining = computeDaysRemaining(acct.expires_at);
        
        if (daysRemaining === 7 || daysRemaining === 3 || daysRemaining === 0) {
            try {
                const email = await import("../../_lib/email");
                const tmpl = email.renewalNudgeEmail({
                    customer_name: acct.name,
                    customer_email: acct.email,
                    expires_at: acct.expires_at,
                    days_remaining: daysRemaining,
                    dashboard_url: `${base}/dashboard.html`,
                });
                await email.sendEmail({ to: acct.email, ...tmpl });
                sentCount++;
            } catch (e) {
                console.error(`Failed to send renewal nudge to ${acct.email}:`, (e as Error).message);
            }
        }
    }

    return json({ ok: true, sent: sentCount });
};

function computeDaysRemaining(expiresAt: string | null): number {
    if (!expiresAt) return -1;
    const expiry = new Date(expiresAt);
    const now = new Date();
    const diffMs = expiry.getTime() - now.getTime();
    return Math.floor(diffMs / (1000 * 60 * 60 * 24));
}

function json(obj: unknown, status = 200): Response {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}