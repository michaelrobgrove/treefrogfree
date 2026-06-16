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
    name: string;
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
        <h2 style="margin:0 0 12px;font-size:20px;">Welcome aboard${opts.name ? ", " + escapeHtml(opts.name.split(" ")[0]) : ""}.</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          Your subscription is active. Your login is the same for the dashboard
          and your IPTV app &mdash; keep this email handy.
        </p>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Login (dashboard &amp; IPTV apps)</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Username", opts.username)}
          ${credRow("Password", opts.password)}
        </table>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Xtream Codes (use in IPTV Smarters, TiviMate, etc.)</h3>
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
          Lost your password? Reply to this email and we'll help you reset it.
        </p>
    `);
    const text = `Welcome to Tree Frog Plus${opts.name ? ", " + opts.name.split(" ")[0] : ""}.

Your subscription is active. Your login is the same for the dashboard and your IPTV app — keep this email handy.

Login (dashboard & IPTV apps)
  Username: ${opts.username}
  Password: ${opts.password}

Xtream Codes (use in IPTV Smarters, TiviMate, etc.)
  Server:   ${opts.xc_server}
  Username: ${opts.username}
  Password: ${opts.password}

DNS endpoints
  Primary:  ${opts.dns_primary}
  Failover: ${opts.dns_secondary}

Setup guide: ${opts.setup_url}

Lost your password? Reply to this email and we'll help you reset it.
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

export function renewalLinkEmail(opts: {
    email: string;
    months: number;
    amount_usd: number;
    payment_url: string;
    dashboard_url: string;
}): { subject: string; html: string; text: string } {
    const subject = `Your Tree Frog Plus renewal link ($${opts.amount_usd.toFixed(2)})`;
    const html = layout(`
        <h2 style="margin:0 0 12px;font-size:20px;">Ready to renew?</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          Your ${opts.months}-month renewal link is ready. Pay securely with
          PayPal — your Gold Panel line extends the moment the payment clears.
        </p>
        <p style="margin:0 0 4px;color:#94a3b8;font-size:13px;">Amount</p>
        <p style="margin:0 0 24px;color:#f9fafb;font-size:28px;font-weight:700;">
          $${opts.amount_usd.toFixed(2)}
        </p>
        <div style="text-align:center;margin:8px 0 24px;">
          <a href="${escapeHtml(opts.payment_url)}"
             style="display:inline-block;background:#22c55e;color:#111827;font-weight:600;text-decoration:none;padding:14px 32px;border-radius:8px;">
            Pay with PayPal
          </a>
        </div>
        <p style="margin:0 0 4px;color:#94a3b8;font-size:12px;">
          Or open your dashboard: <a href="${escapeHtml(opts.dashboard_url)}" style="color:#22c55e;">${escapeHtml(opts.dashboard_url)}</a>
        </p>
        <p style="margin:24px 0 0;color:#94a3b8;font-size:12px;">
          The link is good for one payment. Didn't request this? Reply to this email.
        </p>
    `);
    const text = `Ready to renew?

Your ${opts.months}-month renewal link is ready. Pay securely with PayPal.

Amount: $${opts.amount_usd.toFixed(2)}

Pay: ${opts.payment_url}

Or open your dashboard: ${opts.dashboard_url}
`;
    return { subject, html, text };
}

/** Operator notification when a customer on a custom (non-
 *  standard) Gold Panel bouquet asks to renew. The customer
 *  can't self-serve these — the operator has to look up
 *  their line in the panel admin, quote a price, and email
 *  a payment link. Sent to admin@tfplus.stream (configurable
 *  via the ADMIN_EMAIL env var, with a hardcoded fallback
 *  for the operator's primary admin address). */
export function customRenewalRequestEmail(opts: {
    account_paypal_order_id: string;
    customer_email: string;
    customer_name: string;
    panel_username: string;
    panel_user_id: string;
    panel_bouquet_id: string;
    expires_at: string | null;
    message: string;
    dashboard_url: string;
}): { subject: string; html: string; text: string } {
    const subject = `Custom-bouquet renewal request from ${opts.customer_email}`;
    const safeMsg = escapeHtml(opts.message || "(no message)");
    const html = layout(`
        <h2 style="margin:0 0 12px;font-size:20px;">Custom-bouquet renewal request</h2>
        <p style="margin:0 0 16px;color:#cbd5e1;">
          A customer on a non-standard Gold Panel bouquet clicked
          "Contact support to renew" on the dashboard. They cannot
          self-serve renewals; you need to look up their line and
          email a payment link manually.
        </p>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Customer</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Name", opts.customer_name || "(not provided)")}
          ${credRow("Email", opts.customer_email || "(not provided)")}
          ${credRow("Site account", opts.account_paypal_order_id)}
        </table>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Gold Panel line</h3>
        <table style="width:100%;border-collapse:collapse;">
          ${credRow("Username", opts.panel_username)}
          ${credRow("User ID", opts.panel_user_id || "(unknown)")}
          ${credRow("Bouquet ID (custom)", opts.panel_bouquet_id || "(unknown)")}
          ${credRow("Expires", opts.expires_at || "(unknown)")}
        </table>

        <h3 style="margin:24px 0 8px;font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">Message from customer</h3>
        <div style="background:#0f172a;border-left:3px solid #22c55e;padding:12px 16px;border-radius:4px;color:#cbd5e1;white-space:pre-wrap;">${safeMsg}</div>

        <div style="text-align:center;margin:28px 0 8px;">
          <a href="${escapeHtml(opts.dashboard_url)}"
             style="display:inline-block;background:#22c55e;color:#111827;font-weight:600;text-decoration:none;padding:12px 24px;border-radius:8px;">
            Open dashboard
          </a>
        </div>

        <p style="margin:24px 0 0;color:#94a3b8;font-size:12px;">
          To email the customer a renewal payment link, run <code style="color:#22c55e;">POST /api/account/renew</code> from the
          operator tooling, or use the dashboard's "Generate renewal link" action once
          the bouquet has been updated to a standard one.
        </p>
    `);
    const text = `Custom-bouquet renewal request

Customer
  Name:   ${opts.customer_name || "(not provided)"}
  Email:  ${opts.customer_email || "(not provided)"}
  Site:   ${opts.account_paypal_order_id}

Gold Panel line
  Username:  ${opts.panel_username}
  User ID:   ${opts.panel_user_id || "(unknown)"}
  Bouquet:   ${opts.panel_bouquet_id || "(unknown)"}
  Expires:   ${opts.expires_at || "(unknown)"}

Message
${opts.message || "(no message)"}

Open the dashboard: ${opts.dashboard_url}
`;
    return { subject, html, text };
}

/** Returns the operator's admin email for "contact support
 *  to renew" submissions and similar operator-bound
 *  notifications. Configurable via the ADMIN_EMAIL env var;
 *  defaults to admin@tfplus.stream. */
export function adminAddress(): string {
    const v = (globalThis as any).ADMIN_EMAIL;
    if (typeof v === "string" && v) return v;
    return "admin@tfplus.stream";
}
