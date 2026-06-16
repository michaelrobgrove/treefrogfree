/* Tree Frog Plus — shared front-end helpers
 *
 * Tiny utilities used across pages. No framework; just a handful of
 * functions exposed on `window.tf` for inline <script> tags and
 * button onclick handlers.
 *
 * Convention: every API call is a fetch() with credentials:'include'
 * so the tfp_sess cookie is sent. Errors are thrown and surfaced via
 * toast() — callers wrap in try/catch and let the helper render.
 */

(function () {
    "use strict";

    const tf = {};

    /** Escape a string for safe insertion into innerHTML. */
    tf.escapeHtml = function (s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    };

    /** Format an ISO date string as "Jan 15, 2026". Falls back to "—". */
    tf.formatDate = function (iso) {
        if (!iso) return "—";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleDateString("en-US", {
            year: "numeric", month: "short", day: "numeric",
        });
    };

    /** Copy a value to the clipboard; flips the trigger button briefly. */
    tf.copyToClipboard = async function (text, btn) {
        try {
            await navigator.clipboard.writeText(text);
        } catch (e) {
            // Fallback for older browsers / non-secure contexts.
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand("copy"); }
            finally { document.body.removeChild(ta); }
        }
        if (btn) {
            const original = btn.textContent;
            btn.textContent = "Copied!";
            btn.classList.add("is-copied");
            setTimeout(() => {
                btn.textContent = original;
                btn.classList.remove("is-copied");
            }, 1200);
        }
    };

    /** Show a transient toast. kind = "success" | "error" | undefined. */
    tf.toast = function (msg, kind) {
        let el = document.querySelector(".tf-toast");
        if (!el) {
            el = document.createElement("div");
            el.className = "tf-toast";
            document.body.appendChild(el);
        }
        el.className = "tf-toast" +
            (kind === "success" ? " is-success" :
             kind === "error"   ? " is-error"   : "");
        el.textContent = msg;
        // Force reflow so the transition triggers.
        void el.offsetWidth;
        el.classList.add("is-visible");
        clearTimeout(el._timer);
        el._timer = setTimeout(() => el.classList.remove("is-visible"), 2600);
    };

    /** Wrap a fetch call and return parsed JSON. Throws on !ok with
     *  a useful message. Always includes credentials so the session
     *  cookie is sent. */
    tf.api = async function (path, opts) {
        opts = opts || {};
        const init = {
            method: opts.method || "GET",
            credentials: "include",
            headers: Object.assign(
                { "Accept": "application/json" },
                opts.body ? { "Content-Type": "application/json" } : {},
                opts.headers || {},
            ),
        };
        if (opts.body) init.body = JSON.stringify(opts.body);
        let resp;
        try {
            resp = await fetch(path, init);
        } catch (e) {
            throw new Error("Network error — please try again.");
        }
        const text = await resp.text();
        let data = null;
        if (text) {
            try { data = JSON.parse(text); }
            catch (e) { /* server returned non-JSON */ }
        }
        if (!resp.ok) {
            const msg = (data && data.error) || `Request failed (${resp.status})`;
            const err = new Error(msg);
            err.status = resp.status;
            err.data = data;
            throw err;
        }
        return data === null ? {} : data;
    };

    /** Wire a button to copy its data-copy attribute into the clipboard. */
    tf.bindCopyButtons = function (root) {
        (root || document).querySelectorAll("[data-copy]").forEach((btn) => {
            if (btn._tfBound) return;
            btn._tfBound = true;
            btn.addEventListener("click", (e) => {
                e.preventDefault();
                tf.copyToClipboard(btn.getAttribute("data-copy"), btn);
            });
        });
    };

    /** Wire a form so it does fetch on submit and renders the
     *  response's `error` field as a toast. Used by login + dashboard
     *  cancel/renew buttons. */
    tf.bindForm = function (form, fn) {
        if (!form) return;
        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            const submit = form.querySelector("[type=submit]");
            const originalText = submit ? submit.textContent : "";
            if (submit) {
                submit.disabled = true;
                submit.innerHTML = '<span class="tf-spinner"></span> Working…';
            }
            try {
                await fn(form);
            } catch (err) {
                tf.toast(err.message || "Something went wrong.", "error");
            } finally {
                if (submit) {
                    submit.disabled = false;
                    submit.textContent = originalText;
                }
            }
        });
    };

    window.tf = tf;

    // Auto-wire copy buttons on page load.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded",
            () => tf.bindCopyButtons());
    } else {
        tf.bindCopyButtons();
    }
})();
