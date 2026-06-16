/** Transactional email helper.
 *
 *  Cloudflare Email Workers can *receive* mail (via Email Routing)
 *  but they can't *send* it. For outbound, we use Resend — a
 *  simple REST API designed for Workers. If you'd rather use
 *  Mailgun / SendGrid / Postmark, swap the fetch() in `sendEmail`
 *  for that provider's API; the template functions stay the same.
 *
 *  All env (key, from address) are read from secrets set via
 *  `wrangler pages secret put`.
 */

function resendApiKey(): string {
    const v = (globalThis as any).RESEND_API_KEY;
    if (typeof v !== "string" || !v) {
        throw new Error("RESEND_API_KEY is not configured.");
    }
    return v;
}

function fromAddress(): string {
    const v = (globalThis as any).RESEND_FROM_ADDRESS;
    if (typeof v !== "string" || !v) {
        throw new Error("RESEND_FROM_ADDRESS is not configured.");
    }
    return v;
}

export async function sendEmail(opts: {
    to: string;
    subject: string;
    html: string;
    text: string;
}): Promise<void> {
    const resp = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: {
            "Authorization": `Bearer ${resendApiKey()}`,
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            from: fromAddress(),
            to: opts.to,
            subject: opts.subject,
            html: opts.html,
            text: opts.text,
        }),
    });
    if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`Resend ${resp.status}: ${body.slice(0, 200)}`);
    }
}

// ------------------ Templates ------------------

function layout(innerHtml: string): string {
    return `<!doctype html>
<html>
  <body style="background:#0f172a;color:#e2e8f0;font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;margin:0;padding:0;">
    <div style="max-width:560px;margin:0 auto;padding:24px;">
      <div style="text-align:center;padding:24px 0;">
        <div style="font-size:40px;">&#x1F438;</div>
        <h1 style="color:#22c55e;margin:8px 0 0;font-size:20px;">Tree Frog Plus</h1>
      </div>
      <div style="background:#111827;border:1px solid #1f2937;border-radius:12px;padding:24px;">
        ${innerHtml}
      </div>
      <p style="color:#64748b;font-size:12px;text-align:center;padding:24px 0 0;margin:0;">
        Tree Frog Plus &middot; <a href="https://tfplus.stream" style="color:#22c55e;">tfplus.stream</a>
      </p>
    </div>
  </body>
</html>`;
}

function credRow(label: string, value: string): string {
    return `<tr>
      <td style="padding:6px 0;color:#94a3b8;font-size:14px;">${label}</td>
      <td style="padding:6px 0;font-family:ui-monospace,Menlo,monospace;font-size:14px;color:#f9fafb;word-break:break-all;">${escapeHtml(value)}</td>
    </tr>`;
}

function escapeHtml(s: string): string {
    return s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

export function welcomeEmail(opts: {
    email: string;
    username: string;
    password: string;
    dns_primary: string;
    dns_secondary: string;
    xc_server: string;
    setup_url: string;
}): { subject: string; html: string; text: string } {
    const subject = "Your Tree Frog Plus account is ready";
    const html = layout(`
        <h2 style="margin:0 0 12px;font-size:20px;">Welcome aboard.</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          Your subscription is active. Sign in to the dashboard with the
          credentials below and follow the setup guide to start watching.
        </p>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Sign in</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Email", opts.email)}
          ${credRow("Password", opts.password)}
        </table>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Xtream Codes (IPTV apps)</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Server", opts.xc_server)}
          ${credRow("Username", opts.username)}
          ${credRow("Password", opts.password)}
        </table>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">DNS endpoints</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Primary", opts.dns_primary)}
          ${credRow("Failover", opts.dns_secondary)}
        </table>

        <div style="text-align:center;margin:28px 0 8px;">
          <a href="${escapeHtml(opts.setup_url)}"
             style="display:inline-block;background:#22c55e;color:#111827;font-weight:600;text-decoration:none;padding:12px 24px;border-radius:8px;">
            View setup guide
          </a>
        </div>

        <p style="margin:24px 0 0;color:#94a3b8;font-size:12px;">
          If you didn't subscribe, reply to this email and we'll sort it out.
        </p>
    `);
    const text = `Welcome to Tree Frog Plus.

Your subscription is active. Sign in to the dashboard and follow the setup guide to start watching.

Sign in
  Email:    ${opts.email}
  Password: ${opts.password}

Xtream Codes (use in IPTV Smarters, TiviMate, etc.)
  Server:   ${opts.xc_server}
  Username: ${opts.username}
  Password: ${opts.password}

DNS endpoints
  Primary:  ${opts.dns_primary}
  Failover: ${opts.dns_secondary}

Setup guide: ${opts.setup_url}
`;
    return { subject, html, text };
}

export function paymentFailedEmail(opts: {
    email: string;
    update_url: string;
}): { subject: string; html: string; text: string } {
    const subject = "Payment failed — please update your billing";
    const html = layout(`
        <h2 style="margin:0 0 12px;font-size:20px;">Payment didn't go through.</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          PayPal was unable to charge your card for this billing cycle. They'll
          retry over the next few days. To avoid interruption, please update
          your payment method or top up your PayPal balance.
        </p>
        <div style="text-align:center;margin:24px 0 8px;">
          <a href="${escapeHtml(opts.update_url)}"
             style="display:inline-block;background:#22c55e;color:#111827;font-weight:600;text-decoration:none;padding:12px 24px;border-radius:8px;">
            Update billing
          </a>
        </div>
    `);
    const text = `Payment failed.

PayPal was unable to charge your card for this billing cycle. They'll retry over the next few days. To avoid interruption, please update your payment method or top up your PayPal balance.

Update billing: ${opts.update_url}
`;
    return { subject, html, text };
}

export function renewalReceiptEmail(opts: {
    email: string;
    new_expire: string;
    months_added: number;
}): { subject: string; html: string; text: string } {
    const subject = `Tree Frog Plus renewed: new expiry ${opts.new_expire}`;
    const html = layout(`
        <h2 style="margin:0 0 12px;font-size:20px;">Renewal received.</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          Thanks — your ${opts.months_added}-month renewal cleared. Your new
          expiry date is <strong style="color:#22c55e;">${escapeHtml(opts.new_expire)}</strong>.
        </p>
    `);
    const text = `Renewal received.

Your ${opts.months_added}-month renewal cleared. New expiry: ${opts.new_expire}.
`;
    return { subject, html, text };
}
