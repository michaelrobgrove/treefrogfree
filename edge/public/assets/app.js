// Tree Frog Streams — public site logic.
// Loads /api/channels.json, renders the channel grid, handles search/filter.

const state = {
  catalog: null,
  filter: '',
  category: null,
};

const el = {
  search: document.getElementById('search'),
  categories: document.getElementById('categories'),
  grid: document.getElementById('grid'),
  empty: document.getElementById('empty'),
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
  const cats = state.catalog.categories;
  const all = [{ name: 'All', slug: null, count: state.catalog.channels.length }, ...cats];
  el.categories.innerHTML = all.map(c => `
    <button data-slug="${c.slug ?? ''}"
            class="cat-pill text-xs px-3 py-1.5 rounded-full border transition-colors ${
              state.category === c.slug
                ? 'bg-frog border-frog text-gray-900 font-semibold'
                : 'bg-gray-800 border-gray-700 text-gray-300 hover:border-gray-600'
            }">
      ${escapeHtml(c.name)} <span class="opacity-60">(${c.count})</span>
    </button>
  `).join('');
  el.categories.querySelectorAll('.cat-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      state.category = btn.dataset.slug || null;
      renderCategories();
      renderGrid();
    });
  });
}

function renderGrid() {
  const q = state.filter.trim().toLowerCase();
  const filtered = state.catalog.channels.filter(ch => {
    if (state.category && ch.category !== state.category) return false;
    if (q && !ch.name.toLowerCase().includes(q)) return false;
    return true;
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

loadCatalog();
