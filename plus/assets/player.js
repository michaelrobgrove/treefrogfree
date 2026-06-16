/* Tree Frog Plus — web player
 *
 * Two-list UX: a scrollable channel list on the left, a video pane
 * on the right. Click a channel to play; the same Hls.js engine
 * (v1.5.0) used on the free site handles the manifest.
 *
 * Source of channels: `GET /api/player/channels` — a Pages Function
 * that proxies the Xtream Codes API under the customer's own
 * credentials and returns one ready-to-play URL per channel. We
 * never expose the customer's username/password to the page; only
 * the pre-built stream URLs, which the panel can rotate by
 * reissuing credentials.
 *
 * CORS: the apex/comet DNS endpoints are our own infra, so they
 * should send the right `Access-Control-Allow-Origin` headers
 * already. If a particular stream doesn't (e.g. it 302s through a
 * CDN that strips them), we route through the Worker's /api/proxy
 * route on the free site — same pattern as the free-site player.
 *
 * Failover: each channel entry from the server already includes a
 * primary URL (apex, HTTPS) and a secondary (comet, HTTP). On
 * Hls.js error we advance to the next URL automatically.
 */
(function () {
    "use strict";

    const HLS_VERSION = "1.5.0";
    const PLAYER_ORIGIN = location.origin;

    class TreefrogPlayer {
        constructor(root) {
            this.root = root;
            this.channels = [];
            this.filtered = [];
            this.activeChannel = null;
            this.hls = null;
            this.video = null;
            this.dialog = null;
            this.list = null;
            this.search = null;
            this.status = null;
        }

        /** Lazy-inject the dialog template into the page. Idempotent. */
        ensureDialog() {
            if (this.dialog) return;
            const tpl = document.createElement("template");
            tpl.innerHTML = `
                <dialog id="tfp-player-modal" class="tfp-modal">
                    <div class="bg-gray-900 border border-gray-700 rounded-2xl overflow-hidden flex flex-col md:flex-row max-h-[90vh]">
                        <aside class="md:w-80 lg:w-96 bg-gray-800 border-r border-gray-700 flex flex-col max-h-[40vh] md:max-h-[90vh]">
                            <div class="p-3 border-b border-gray-700">
                                <input type="search" id="tfp-player-search"
                                       placeholder="Search channels…"
                                       class="w-full bg-gray-900 border border-gray-700 text-white placeholder-gray-500 text-sm rounded-lg px-3 py-2 tf-focus"/>
                            </div>
                            <div id="tfp-player-list" class="flex-1 overflow-y-auto"></div>
                        </aside>
                        <section class="flex-1 flex flex-col min-w-0">
                            <div class="flex items-center justify-between px-4 py-2 border-b border-gray-700">
                                <div id="tfp-player-title"
                                     class="text-white font-semibold truncate">Pick a channel</div>
                                <button id="tfp-player-close" type="button"
                                        class="text-gray-400 hover:text-white text-2xl leading-none tf-focus"
                                        aria-label="Close">×</button>
                            </div>
                            <div class="relative bg-black flex-1 flex items-center justify-center min-h-[260px]">
                                <video id="tfp-player-video" controls playsinline
                                       class="w-full h-full max-h-[70vh] bg-black"></video>
                                <div id="tfp-player-status"
                                     class="absolute inset-0 flex items-center justify-center text-gray-300 text-sm pointer-events-none">
                                    Click a channel on the left to start.
                                </div>
                            </div>
                        </section>
                    </div>
                </dialog>
            `;
            document.body.appendChild(tpl.content.cloneNode(true));
            this.dialog   = document.getElementById("tfp-player-modal");
            this.list     = document.getElementById("tfp-player-list");
            this.search   = document.getElementById("tfp-player-search");
            this.status   = document.getElementById("tfp-player-status");
            this.titleEl  = document.getElementById("tfp-player-title");
            this.video    = document.getElementById("tfp-player-video");
            this.closeBtn = document.getElementById("tfp-player-close");

            this.closeBtn.addEventListener("click", () => this.close());
            this.dialog.addEventListener("click", (e) => {
                if (e.target === this.dialog) this.close();
            });
            this.search.addEventListener("input", () => this.applyFilter());
            this.video.addEventListener("error", () => this.advanceOrFail());
        }

        /** Public: open the player. Loads the channel list, shows the
         *  modal. Called from a button on the dashboard. */
        async open() {
            this.ensureDialog();
            this.dialog.showModal();
            document.body.style.overflow = "hidden";
            this.setStatus("Loading channels…");
            try {
                const data = await window.tf.api("/api/player/channels");
                this.channels = (data.channels || []).map((c) => ({
                    id: c.id,
                    name: c.name || `Channel ${c.id}`,
                    logo: c.logo || "",
                    group: c.group || "",
                    urls: c.urls || [c.url].filter(Boolean),
                }));
                if (this.channels.length === 0) {
                    this.setStatus("No channels available in your bouquet yet.");
                    return;
                }
                this.applyFilter();
                this.setStatus("Click a channel on the left to start.");
            } catch (e) {
                this.setStatus(e.message || "Could not load channels.");
            }
        }

        close() {
            this.teardownStream();
            if (this.dialog && this.dialog.open) this.dialog.close();
            document.body.style.overflow = "";
        }

        applyFilter() {
            const q = (this.search.value || "").trim().toLowerCase();
            this.filtered = q
                ? this.channels.filter((c) => c.name.toLowerCase().includes(q))
                : this.channels;
            this.renderList();
        }

        renderList() {
            const html = this.filtered.slice(0, 500).map((c) => {
                const logo = c.logo
                    ? `<img src="${window.tf.escapeHtml(c.logo)}" alt="" loading="lazy" class="w-10 h-10 rounded object-cover bg-gray-700 flex-shrink-0"/>`
                    : `<div class="w-10 h-10 rounded bg-gray-700 flex-shrink-0"></div>`;
                return `
                    <button type="button" data-cid="${c.id}"
                            class="w-full text-left flex items-center gap-3 p-2 hover:bg-gray-700/60 transition-colors tf-focus">
                        ${logo}
                        <div class="min-w-0">
                            <div class="text-sm text-white truncate">${window.tf.escapeHtml(c.name)}</div>
                            ${c.group ? `<div class="text-xs text-gray-500 truncate">${window.tf.escapeHtml(c.group)}</div>` : ""}
                        </div>
                    </button>
                `;
            }).join("");
            this.list.innerHTML = html ||
                `<div class="p-4 text-sm text-gray-500">No matches.</div>`;
            this.list.querySelectorAll("button[data-cid]").forEach((btn) => {
                btn.addEventListener("click", () => {
                    const cid = parseInt(btn.getAttribute("data-cid"), 10);
                    this.play(cid);
                });
            });
        }

        async play(channelId) {
            const ch = this.channels.find((c) => c.id === channelId);
            if (!ch) return;
            this.teardownStream();
            this.activeChannel = ch;
            this.urlIndex = 0;
            this.titleEl.textContent = ch.name;
            this.setStatus(`Loading ${ch.name}…`);
            await this.tryCurrent();
        }

        async tryCurrent() {
            if (!this.activeChannel) return;
            const urls = this.activeChannel.urls;
            if (this.urlIndex >= urls.length) {
                this.setStatus("All sources failed.");
                return;
            }
            const url = urls[this.urlIndex];
            try {
                if (window.Hls && window.Hls.isSupported()) {
                    this.hls = new window.Hls({
                        // Match free-site settings. We don't go through
                        // a proxy by default; apex/comet are our DNS.
                        xhrSetup: (xhr) => { xhr.withCredentials = false; },
                    });
                    this.hls.loadSource(url);
                    this.hls.attachMedia(this.video);
                    this.hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
                        this.video.play().catch(() => { /* autoplay blocked */ });
                        this.setStatus("");
                    });
                    this.hls.on(window.Hls.Events.ERROR, (_e, data) => {
                        if (data.fatal) this.advanceOrFail(data);
                    });
                } else if (this.video.canPlayType("application/vnd.apple.mpegurl")) {
                    this.video.src = url;
                    this.video.play().catch(() => { /* autoplay blocked */ });
                    this.setStatus("");
                } else {
                    this.setStatus("This browser can't play HLS streams.");
                }
            } catch (e) {
                this.advanceOrFail();
            }
        }

        advanceOrFail(errData) {
            if (this.hls) {
                try { this.hls.destroy(); } catch (e) {}
                this.hls = null;
            }
            this.urlIndex += 1;
            if (this.activeChannel && this.urlIndex < this.activeChannel.urls.length) {
                this.setStatus(`Source ${this.urlIndex} failed — trying backup…`);
                this.tryCurrent();
            } else {
                this.setStatus("All sources failed.");
            }
        }

        teardownStream() {
            if (this.hls) {
                try { this.hls.destroy(); } catch (e) {}
                this.hls = null;
            }
            if (this.video) {
                try { this.video.pause(); } catch (e) {}
                this.video.removeAttribute("src");
                this.video.load();
            }
            this.activeChannel = null;
            this.urlIndex = 0;
        }

        setStatus(msg) {
            if (this.status) this.status.textContent = msg || "";
        }
    }

    // Expose.
    window.TreefrogPlayer = TreefrogPlayer;

    // Auto-wire any [data-action=open-player] buttons (dashboard CTA).
    document.addEventListener("DOMContentLoaded", () => {
        const btns = document.querySelectorAll("[data-action=open-player]");
        if (!btns.length) return;
        const player = new TreefrogPlayer();
        btns.forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.preventDefault();
                player.open();
            });
        });
    });
})();
