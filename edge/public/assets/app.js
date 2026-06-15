// Tree Frog Streams — public site logic.
// Loads /api/channels.json, renders the channel grid, handles search/filter.

const state = {
  catalog: null,
  filter: '',
  category: null,
  // Category list UI: when false, only the top MAX_VISIBLE_PILLS
  // (sorted by channel count desc) are shown, with a "Show all"
  // toggle at the end. The selected category stays visible even
  // if it falls outside the top-N.
  showAllCategories: false,
};

// How many category pills to show by default. Beyond this, the
// user must click "Show all" — protects the layout from the 80+
// raw M3U categories that show up before canonicalization, and
// from the 20+ canonical pills that still add up to a long row
// on small screens.
const MAX_VISIBLE_PILLS = 10;

const el = {
  search: document.getElementById('search'),
  categories: document.getElementById('categories'),
  grid: document.getElementById('grid'),
  empty: document.getElementById('empty'),
  emptyClear: document.getElementById('empty-clear'),
  channelCount: document.getElementById('stat-channel-count'),
  availability: document.getElementById('stat-availability'),
  categoriesCount: document.getElementById('stat-categories'),
  lastUpdated: document.getElementById('last-updated'),
};

async function loadCatalog() {
  try {
    const resp = await fetch('/api/channels.json', { cache: 'no-cache' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    state.catalog = await resp.json();
  } catch (e) {
    console.error('Failed to load catalog:', e);
    el.grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-12">Catalog unavailable. Check back in a few minutes.</div>';
    return;
  }
  renderStats();
  renderCategories();
  renderGrid();
}

function renderStats() {
  const s = state.catalog.stats;
  el.channelCount.textContent = s.channel_count.toLocaleString();
  el.availability.textContent = `${s.average_availability_pct.toFixed(1)}%`;
  el.categoriesCount.textContent = s.category_count;
  el.lastUpdated.textContent = `Last updated ${formatTime(s.last_health_check)}`;
}

function renderCategories() {
  // Categories are already sorted alphabetically in the catalog
  // payload, but for "show by default" we want biggest first.
  const allCats = [...state.catalog.categories].sort(
    (a, b) => b.count - a.count
  );
  // Always include the active category, even if it's outside the
  // top-N — so the user can see why they're filtered.
  let visible;
  if (state.showAllCategories || allCats.length <= MAX_VISIBLE_PILLS) {
    visible = allCats;
  } else {
    const top = allCats.slice(0, MAX_VISIBLE_PILLS);
    const activeIsInTop = top.some((c) => c.slug === state.category);
    if (state.category && !activeIsInTop) {
      // Show the top-N plus the active one. The active pill
      // gets a subtle marker so the user knows it's outside the
      // default set.
      const active = allCats.find((c) => c.slug === state.category);
      visible = [...top, { ...active, _outOfDefault: true }];
    } else {
      visible = top;
    }
  }
  // Re-sort the visible list alphabetically for the chip row, so
  // the layout doesn't shuffle when the user toggles a category.
  visible.sort((a, b) => a.name.localeCompare(b.name));

  const all = [
    { name: 'All', slug: null, count: state.catalog.channels.length },
    ...visible,
  ];
  // Build the "Show all" / "Show less" toggle if needed.
  const moreBtn = allCats.length > MAX_VISIBLE_PILLS ? `
    <button id="cats-toggle"
            class="cat-pill text-xs px-3 py-1.5 rounded-full border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors">
      ${state.showAllCategories ? '‹ Show fewer' : `Show all (${allCats.length}) ›`}
    </button>
  ` : '';

  el.categories.innerHTML = all.map((c) => `
    <button data-slug="${c.slug ?? ''}"
            class="cat-pill text-xs px-3 py-1.5 rounded-full border transition-colors ${
              state.category === c.slug
                ? 'bg-frog border-frog text-gray-900 font-semibold'
                : 'bg-gray-800 border-gray-700 text-gray-300 hover:border-gray-600'
            }"
            ${c._outOfDefault ? 'title="Active category — outside the top ' + MAX_VISIBLE_PILLS + '"' : ''}>
      ${escapeHtml(c.name)} <span class="opacity-60">(${c.count})</span>
    </button>
  `).join('') + moreBtn;

  el.categories.querySelectorAll('.cat-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.category = btn.dataset.slug || null;
      renderCategories();
      renderGrid();
    });
  });
  const toggle = el.categories.querySelector('#cats-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      state.showAllCategories = !state.showAllCategories;
      renderCategories();
    });
  }
}

function renderGrid() {
  const q = state.filter.trim().toLowerCase();
  const filtered = state.catalog.channels.filter(ch => {
    if (state.category && ch.category !== state.category) return false;
    if (q && !ch.name.toLowerCase().includes(q)) return false;
    return true;
  });
  // Sort by availability desc so the working channels appear at
  // the top of the grid. The user typically scrolls the first
  // screenful — this is what they see. Stable secondary sort on
  // name so the order is deterministic across reloads.
  filtered.sort((a, b) => {
    if (b.availability_pct !== a.availability_pct) {
      return b.availability_pct - a.availability_pct;
    }
    return a.name.localeCompare(b.name);
  });
  if (filtered.length === 0) {
    el.grid.innerHTML = '';
    el.empty.classList.remove('hidden');
    return;
  }
  el.empty.classList.add('hidden');
  el.grid.innerHTML = filtered.map(ch => `
    <a href="/channel.html?id=${ch.id}"
       class="channel-card block bg-gray-800 rounded-xl border border-gray-700 hover:border-frog overflow-hidden">
      <div class="aspect-video bg-gray-900 flex items-center justify-center">
        ${ch.logo
          ? `<img src="${escapeAttr(ch.logo)}" alt="" class="max-h-full max-w-full object-contain" loading="lazy" onerror="this.replaceWith(makePlaceholder())">`
          : makePlaceholderHtml()}
      </div>
      <div class="p-3">
        <h3 class="font-semibold text-sm truncate">${escapeHtml(ch.name)}</h3>
        <div class="flex items-center gap-2 mt-1 text-xs text-gray-400">
          <span class="flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full ${ch.availability_pct >= 95 ? 'bg-green-500' : ch.availability_pct >= 80 ? 'bg-yellow-500' : 'bg-red-500'}"></span>
            ${ch.availability_pct.toFixed(1)}%
          </span>
        </div>
      </div>
    </a>
  `).join('');
}

function makePlaceholder() {
  const d = document.createElement('div');
  d.className = 'text-gray-600 text-3xl';
  d.textContent = '📺';
  return d;
}

function makePlaceholderHtml() {
  return '<div class="text-gray-600 text-3xl">📺</div>';
}

function formatTime(iso) {
  if (!iso || iso.startsWith('1970')) return 'never';
  try {
    const d = new Date(iso);
    const now = Date.now();
    const diff = (now - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
    return d.toLocaleDateString();
  } catch { return iso; }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

el.search.addEventListener('input', (e) => {
  state.filter = e.target.value;
  renderGrid();
});

// "Clear filters" button shown in the empty state. Resets the
// search box, the category filter, and the show-all toggle so the
// user can recover from a dead-end (e.g. a category that no
// channels currently match).
if (el.emptyClear) {
  el.emptyClear.addEventListener('click', () => {
    state.filter = '';
    state.category = null;
    state.showAllCategories = false;
    if (el.search) el.search.value = '';
    renderCategories();
    renderGrid();
  });
}

// Delegated click handler: open the HLS player modal on a plain
// left-click of a channel card. Modifier-clicks (cmd/ctrl/shift),
// middle-click, and right-click fall through to the existing
// `/channel.html?id=N` navigation. Channels with no `token` (no
// online streams) also fall through — the detail page is the right
// place to show that.
el.grid.addEventListener('click', (e) => {
  const a = e.target.closest('a.channel-card');
  if (!a) return;
  if (e.button !== 0) return;        // middle / right click → browser default
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // open in new tab
  const id = parseInt(new URL(a.href, location.origin).searchParams.get('id'), 10);
  if (!id) return;
  const channel = state.catalog?.channels?.find((c) => c.id === id);
  if (!channel || !channel.token || !window.TreefrogPlayer) return;
  e.preventDefault();
  window.TreefrogPlayer.open(channel);
});

loadCatalog();
