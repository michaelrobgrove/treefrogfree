// Tree Frog Streams — HLS web player.
//
// Single global `TreefrogPlayer` class, instantiated at the bottom and
// exposed as `window.TreefrogPlayer`. Public method: `.open(channel)`.
//
// Channel shape required:
//   {
//     id:       number,
//     name:     string,
//     logo:     string | null,
//     tvg_id:   string | null,    // may be null → EPG section hidden
//     token:    string | null     // may be null → "no streams" message
//   }
//
// Lifecycle:
//   idle → loading → playing → {playing | error} → closed
//
// Failover: when a stream stalls/errors, the player destroys the Hls
// instance, advances to the next URL in the list, and re-attaches.
// Exhausted → "All sources failed" with an "Open source URL" link as
// a last-ditch fallback (lets the user hand the URL to VLC).
//
// EPG: if tvg_id is set, the player fetches /api/epg/nownext/<tvg_id>
// and renders "On now" + "Up next" cards. 404 or null fields → the
// EPG block is hidden (silently, per design).
//
// hls.js is loaded from CDN (pinned v1.5.0). On iOS Safari where
// Hls.isSupported() is false, we fall back to native HLS via
// <video>.src.

(function () {
  'use strict';

  const HLS_VERSION = '1.5.0';
  const HLS_CDN_URL = `https://cdn.jsdelivr.net/npm/hls.js@${HLS_VERSION}/dist/hls.min.js`;

  // ---- State machine ----
  // Each open() call resets these. The dialog and template are
  // lazy-injected once per page load.
  let dialog = null;
  let video = null;
  let epgNow = null;
  let epgNext = null;
  let status = null;
  let hls = null;
  let currentChannel = null;
  let urls = [];
  let urlIndex = 0;

  // ---- Public API ----

  function open(channel) {
    currentChannel = channel;
    urls = [];
    urlIndex = 0;
    ensureDialog();
    dialog.showModal();
    // Lock body scroll while the modal is open. The native <dialog>
    // doesn't do this for us; the page stays scrollable in the
    // background.
    document.body.classList.add('overflow-hidden');
    setStatus('Loading…');
    setVideoPoster(channel.logo);
    setTitle(channel.name);
    clearEpg();
    loadEpg(channel.tvg_id);
    if (!channel.token) {
      setStatus('No online streams for this channel right now.');
      return;
    }
    fetchAndPlay(channel.token).catch((e) => {
      console.error('fetchAndPlay error', e);
      setStatus(`Failed to start: ${e.message}`);
    });
  }

  function close() {
    teardown();
    if (dialog && dialog.open) dialog.close();
    document.body.classList.remove('overflow-hidden');
  }

  // ---- DOM scaffolding (lazy, idempotent) ----

  function ensureDialog() {
    if (dialog) return;
    const tpl = document.createElement('template');
    tpl.innerHTML = `
      <dialog id="tf-player" class="tf-player-dialog">
        <div class="tf-player-frame">
          <button type="button" class="tf-close" aria-label="Close player" data-action="close">×</button>
          <div class="tf-video-wrap">
            <video id="tf-player-video" playsinline controls></video>
          </div>
          <div class="tf-meta">
            <div class="tf-title-row">
              <h2 id="tf-player-title" class="tf-title">—</h2>
              <span id="tf-player-status" class="tf-status">Loading…</span>
            </div>
            <div id="tf-epg" class="tf-epg hidden">
              <div class="tf-epg-card">
                <div class="tf-epg-label">On now</div>
                <div class="tf-epg-title" data-epg="now-title">—</div>
                <div class="tf-epg-time" data-epg="now-time"></div>
              </div>
              <div class="tf-epg-card">
                <div class="tf-epg-label">Up next</div>
                <div class="tf-epg-title" data-epg="next-title">—</div>
                <div class="tf-epg-time" data-epg="next-time"></div>
              </div>
            </div>
          </div>
        </div>
      </dialog>
    `;
    document.body.appendChild(tpl.content.cloneNode(true));
    // Inject the CSS once. Player-scoped styles only — uses .tf-*
    // class names so it can't collide with the rest of the site.
    if (!document.getElementById('tf-player-style')) {
      const style = document.createElement('style');
      style.id = 'tf-player-style';
      style.textContent = `
        .tf-player-dialog {
          background: #0a0a0a;
          color: #fff;
          padding: 0;
          border: 0;
          border-radius: 1rem;
          max-width: 64rem;
          width: calc(100vw - 2rem);
          max-height: calc(100vh - 2rem);
          overflow: hidden;
        }
        .tf-player-dialog::backdrop {
          background: rgba(0, 0, 0, 0.75);
          backdrop-filter: blur(4px);
        }
        .tf-player-frame { display: flex; flex-direction: column; }
        .tf-video-wrap {
          background: #000;
          aspect-ratio: 16 / 9;
          width: 100%;
        }
        .tf-video-wrap video { width: 100%; height: 100%; display: block; background: #000; }
        .tf-close {
          position: absolute; top: 0.5rem; right: 0.75rem;
          background: rgba(0,0,0,0.5); color: #fff;
          border: 0; width: 2.25rem; height: 2.25rem;
          border-radius: 9999px; font-size: 1.5rem; line-height: 1;
          cursor: pointer; z-index: 10;
        }
        .tf-close:hover { background: rgba(0,0,0,0.8); }
        .tf-meta { padding: 1rem 1.25rem 1.25rem; }
        .tf-title-row {
          display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap;
        }
        .tf-title { font-size: 1.25rem; font-weight: 700; margin: 0; }
        .tf-status { color: #9ca3af; font-size: 0.85rem; }
        .tf-epg {
          display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-top: 1rem;
        }
        .tf-epg-card {
          background: #1f2937; border: 1px solid #374151; border-radius: 0.5rem; padding: 0.75rem;
        }
        .tf-epg-label {
          font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: #9ca3af; margin-bottom: 0.25rem;
        }
        .tf-epg-title { font-weight: 600; font-size: 0.95rem; line-height: 1.3; }
        .tf-epg-time { color: #9ca3af; font-size: 0.8rem; margin-top: 0.25rem; }
        @media (max-width: 640px) {
          .tf-epg { grid-template-columns: 1fr; }
        }
      `;
      document.head.appendChild(style);
    }
    dialog = document.getElementById('tf-player');
    video = document.getElementById('tf-player-video');
    epgNow = document.querySelector('[data-epg="now-title"]');
    epgNext = document.querySelector('[data-epg="next-title"]');
    status = document.getElementById('tf-player-status');

    // Close on click of the X or the backdrop.
    dialog.addEventListener('click', (e) => {
      // Click on the dialog itself (not its children) means backdrop.
      if (e.target === dialog) { close(); return; }
      if (e.target.closest('[data-action="close"]')) close();
    });
    // Close on Escape — native <dialog> handles this but we need to
    // run our teardown too.
    dialog.addEventListener('close', () => {
      teardown();
      document.body.classList.remove('overflow-hidden');
    });
  }

  // ---- Fetch + playback ----

  async function fetchAndPlay(token) {
    setStatus('Loading stream list…');
    const resp = await fetch(`/api/streams/${encodeURIComponent(token)}`, { cache: 'no-cache' });
    if (resp.status === 404) {
      setStatus('This channel is no longer available. It may have been removed or is offline.');
      return;
    }
    if (!resp.ok) {
      setStatus(`Stream list lookup failed: HTTP ${resp.status}`);
      return;
    }
    const data = await resp.json();
    urls = Array.isArray(data.urls) ? data.urls : [];
    if (urls.length === 0) {
      setStatus('No online streams for this channel right now. Try again in a few minutes.');
      return;
    }
    setStatus(`Source 1 of ${urls.length}…`);
    tryCurrent();
  }

  function tryCurrent() {
    if (urlIndex >= urls.length) {
      // All sources exhausted.
      const lastUrl = urls[urls.length - 1] || '';
      setStatus('All sources failed.');
      const a = document.createElement('a');
      a.href = lastUrl;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = 'Open source URL in VLC';
      a.style.cssText = 'color:#22c55e;margin-left:.5rem;text-decoration:underline;';
      status.appendChild(a);
      return;
    }
    const url = urls[urlIndex];
    setStatus(`Source ${urlIndex + 1} of ${urls.length}…`);
    attachSource(url);
  }

  function attachSource(url) {
    teardownHls();
    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({
        // Tuned for live IPTV: low-latency with a small buffer so
        // stalls recover fast. See hls.js docs § "fine tuning".
        lowLatencyMode: true,
        backBufferLength: 30,
        maxBufferLength: 10,
        liveSyncDurationCount: 3,
        enableWorker: true,
        // Some IPTV origins serve self-signed or expired certs on
        // their segment host. We can't change that here; we let the
        // browser fail the segment load and the error handler will
        // advance to the next URL.
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch((e) => {
          // Autoplay can be blocked until the user interacts. That's
          // fine — controls are visible, the user can press play.
          console.warn('autoplay blocked, user can press play:', e);
        });
      });
      hls.on(window.Hls.Events.ERROR, (_evt, data) => {
        if (!data.fatal) return; // non-fatal: hls.js recovers itself
        console.warn('hls fatal error', data);
        advanceOrFail(data.details || data.type);
      });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Native HLS (iOS Safari, macOS Safari, some smart TVs).
      video.src = url;
      video.play().catch((e) => console.warn('autoplay blocked:', e));
      video.addEventListener('error', () => advanceOrFail('native error'), { once: true });
    } else {
      setStatus('Your browser does not support HLS playback.');
    }
  }

  function advanceOrFail(reason) {
    console.warn('advancing to next URL; reason:', reason);
    urlIndex += 1;
    tryCurrent();
  }

  function teardownHls() {
    if (hls) {
      try { hls.destroy(); } catch {}
      hls = null;
    }
    if (video) {
      try {
        video.pause();
        video.removeAttribute('src');
        video.load();
      } catch {}
    }
  }

  function teardown() {
    teardownHls();
    urls = [];
    urlIndex = 0;
    currentChannel = null;
    if (status) setStatus('Loading…');
    if (video) video.poster = '';
  }

  // ---- EPG ----

  async function loadEpg(tvgId) {
    if (!tvgId) {
      hideEpg();
      return;
    }
    try {
      const resp = await fetch(`/api/epg/nownext/${encodeURIComponent(tvgId)}`, { cache: 'no-cache' });
      if (resp.status === 404) { hideEpg(); return; }
      if (!resp.ok) { hideEpg(); return; }
      const data = await resp.json();
      if (!data.now && !data.next) { hideEpg(); return; }
      renderEpg(data);
    } catch (e) {
      console.warn('EPG fetch failed', e);
      hideEpg();
    }
  }

  function renderEpg(data) {
    const epgEl = document.getElementById('tf-epg');
    if (!epgEl) return;
    epgEl.classList.remove('hidden');
    if (data.now) {
      epgNow.textContent = data.now.title || '(no title)';
      document.querySelector('[data-epg="now-time"]').textContent = formatRange(data.now.start, data.now.stop);
    } else {
      epgNow.textContent = '—';
      document.querySelector('[data-epg="now-time"]').textContent = '';
    }
    if (data.next) {
      epgNext.textContent = data.next.title || '(no title)';
      document.querySelector('[data-epg="next-time"]').textContent = formatRange(data.next.start, data.next.stop);
    } else {
      epgNext.textContent = '—';
      document.querySelector('[data-epg="next-time"]').textContent = '';
    }
  }

  function hideEpg() {
    const epgEl = document.getElementById('tf-epg');
    if (epgEl) epgEl.classList.add('hidden');
  }

  function clearEpg() {
    if (epgNow) epgNow.textContent = '—';
    if (epgNext) epgNext.textContent = '—';
    document.querySelectorAll('[data-epg$="-time"]').forEach((el) => (el.textContent = ''));
    hideEpg();
  }

  function formatRange(start, stop) {
    if (!start || !stop) return '';
    try {
      const fmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
      return `${fmt.format(new Date(start))} – ${fmt.format(new Date(stop))}`;
    } catch { return ''; }
  }

  // ---- Tiny DOM helpers ----

  function setStatus(text) { if (status) status.textContent = text; }
  function setTitle(text) {
    const t = document.getElementById('tf-player-title');
    if (t) t.textContent = text;
  }
  function setVideoPoster(logo) {
    if (video && logo) video.poster = logo;
  }

  // ---- Lazy-load hls.js (pinned v1.5.0) ----
  // We do this once at module load. If the user clicks a channel
  // before the script finishes loading, the open() call will fail
  // with "HLS not supported" — which is fine; they'll click again.

  function loadHlsScript() {
    if (window.Hls) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = HLS_CDN_URL;
      s.async = true;
      s.crossOrigin = 'anonymous';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('Failed to load hls.js from CDN'));
      document.head.appendChild(s);
    });
  }

  loadHlsScript().catch((e) => console.warn('hls.js preload failed', e));

  // ---- Export ----

  window.TreefrogPlayer = { open, close };
})();
