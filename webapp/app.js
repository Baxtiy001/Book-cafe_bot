/* Book Cafe Mini App — application logic (injected into webapp/index.html) */
'use strict';

/* ============================ bootstrap & state ============================ */
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();
}

const API = {
  token: (tg && tg.initData) ? tg.initData : '',
  branchId: null,
  async req(method, path, body, extraHeaders) {
    const headers = Object.assign({ 'Authorization': 'tma ' + this.token }, extraHeaders || {});
    let payload;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    const res = await fetch(path, { method, headers, body: payload });
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      const msg = data && data.error ? data.error : ('HTTP ' + res.status);
      if (res.status === 401 && typeof showError === 'function') showError('Auth xatosi: ' + msg);
      throw new Error(msg);
    }
    return data;
  },
  get(p, h) { return this.req('GET', p, undefined, h); },
  post(p, b, h) { return this.req('POST', p, b, h); },
  postForm(p, fd) { return this.req('POST', p, fd); },
};

const state = {
  role: 'client',
  me: null,
  branches: [],
  branchId: null,
  branding: { theme: {}, logo_url: null, banner_url: null },
  catalog: { categories: [], dishes: [], tree: {} },
  rules: [],
  upsell: {},
  cart: {},                 // dishId -> qty
  activeCategory: null,
  myOrders: [],
  courier: { registered: false },
  courierStatus: 'offline',
  admin: { branchId: null },
};

/* ============================ helpers ============================ */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const fmt = (n) => Number(n || 0).toLocaleString('ru-RU').replace(/\u00A0/g, ' ') + " so'm";

let toastTimer = null;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

function showError(msg) { toast('⚠️ ' + msg); if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error'); }

function dishImage(d) {
  if (d.image_key) return `<img class="img" src="/api/media/${esc(d.image_key)}" loading="lazy">`;
  return `<div class="img ph">🍽</div>`;
}

function confirmDialog(text) {
  return tg && tg.showConfirm ? new Promise(res => { tg.showConfirm(text, res); })
    : Promise.resolve(window.confirm(text));
}

/* ============================ theming ============================ */
function applyTheme(theme) {
  const r = document.documentElement.style;
  const map = {
    '--primary': theme.primary, '--accent': theme.accent, '--bg': theme.bg,
    '--card': theme.card, '--text': theme.text, '--radius': theme.radius ? theme.radius + 'px' : null,
    '--font': theme.font,
  };
  Object.entries(map).forEach(([k, v]) => { if (v) r.setProperty(k, v); });
  if (tg && tg.setHeaderColor) {
    try { tg.setHeaderColor(theme.header || theme.bg || '#FAF6F0'); } catch (e) {}
    try { tg.setBackgroundColor(theme.bg || '#FAF6F0'); } catch (e) {}
  }
}

function applyBranding(b) {
  state.branding = b;
  applyTheme(b.theme || {});
  $('#brandName').textContent = b.theme && b.theme.name ? b.theme.name : 'Book Cafe';
  document.title = b.theme && b.theme.name ? b.theme.name : 'Book Cafe';
  if (b.logo_url) {
    $('#brandLogo').src = b.logo_url; $('#brandLogo').classList.remove('hidden');
    $('#brandLogoFallback').classList.add('hidden');
  } else {
    $('#brandLogoFallback').classList.remove('hidden');
  }
  if (b.banner_url) {
    $('#brandBanner').src = b.banner_url; $('#brandBanner').classList.remove('hidden');
  }
}

/* ============================ tabs & routing ============================ */
const TABS = [
  { id: 'menu',    label: '📋 Menyu',    roles: ['client', 'admin', 'courier'] },
  { id: 'cart',    label: '🛒 Savat',    roles: ['client', 'admin'] },
  { id: 'orders',  label: '📦 Buyurtmalarim', roles: ['client', 'admin'] },
  { id: 'courier', label: '🛵 Kuryer',   roles: ['courier', 'admin'] },
  { id: 'admin',   label: '⚙️ Admin',    roles: ['admin'] },
  { id: 'profile', label: '👤 Profil',   roles: ['client', 'courier', 'admin'] },
];
let activeTab = 'menu';

function renderTabs() {
  const tabs = TABS.filter(t => t.roles.includes(state.role));
  $('#tabs').innerHTML = tabs.map(t =>
    `<button class="tab ${t.id === activeTab ? 'active' : ''}" data-tab="${t.id}">${t.label}</button>`
  ).join('');
  $$('#tabs .tab').forEach(btn => btn.onclick = () => switchTab(btn.dataset.tab));
}

function switchTab(id) {
  activeTab = id;
  renderTabs();
  renderView();
  window.scrollTo(0, 0);
}

function renderView() {
  const v = $('#view');
  v.innerHTML = '';
  const cartBar = $('#cartBar');
  const showCart = (state.role === 'client' || state.role === 'admin') &&
    ['menu', 'cart'].includes(activeTab);
  cartBar.classList.toggle('hidden', !showCart);

  if (activeTab === 'menu') return renderMenu();
  if (activeTab === 'cart') return renderCart();
  if (activeTab === 'orders') { loadOrdersTab().catch(() => {}); return; }
  if (typeof renderCourier === 'function' && activeTab === 'courier') return renderCourier();
  if (typeof renderAdmin === 'function' && activeTab === 'admin') return renderAdmin();
  if (activeTab === 'profile') return renderProfile();
}

/* ============================ menu (client) ============================ */
async function renderMenu() {
  const v = $('#view');
  v.innerHTML = '<div class="spin"></div>';
  const data = await API.get('/api/bootstrap' + (state.branchId ? '?branch_id=' + state.branchId : ''));
  state.me = data.me; state.role = data.me.role;
  state.branches = data.branches;
  state.branchId = data.branch.id;
  applyBranding(data.branding);
  state.catalog = data.catalog;
  state.rules = data.delivery_rules;
  state.upsell = data.upsell || {};
  state.myOrders = data.my_orders || [];
  renderHeader();
  renderTabs();
  v.innerHTML = `
    <div class="chips" id="catChips"></div>
    <div id="dishGrid" class="grid"></div>`;
  renderCategoryChips();
  renderDishes();
}

function renderHeader() {
  const b = state.branches.find(x => x.id === state.branchId);
  $('#branchName').textContent = b ? `${b.name} · ${b.work_from}-${b.work_to}` : '';
  const sel = $('#branchSelect');
  if (state.branches.length > 1) {
    sel.classList.remove('hidden');
    sel.innerHTML = state.branches.map(x =>
      `<option value="${x.id}" ${x.id === state.branchId ? 'selected' : ''}>${esc(x.name)}</option>`
    ).join('');
    sel.onchange = () => { state.branchId = parseInt(sel.value, 10); state.cart = {}; activeTab = 'menu'; renderMenu(); };
  } else {
    sel.classList.add('hidden');
  }
  const chip = $('#roleChip');
  chip.classList.remove('hidden');
  chip.textContent = state.role;
}

function renderCategoryChips() {
  const cats = state.catalog.categories.filter(c => !c.parent_id);
  const chips = [{ id: null, title: '⭐️ Barchasi' }].concat(cats);
  $('#catChips').innerHTML = chips.map(c =>
    `<button class="chip ${state.activeCategory === c.id ? 'active' : ''}" data-id="${c.id ?? ''}">${esc(c.title)}</button>`
  ).join('');
  $$('#catChips .chip').forEach(btn => btn.onclick = () => {
    state.activeCategory = btn.dataset.id ? parseInt(btn.dataset.id, 10) : null;
    renderCategoryChips(); renderDishes();
  });
}

function dishQty(dishId) { return state.cart[dishId] || 0; }

function renderDishes() {
  const grid = $('#dishGrid');
  let dishes = state.catalog.dishes;
  if (state.activeCategory) {
    const subIds = state.catalog.categories.filter(c => c.parent_id === state.activeCategory).map(c => c.id);
    dishes = dishes.filter(d => d.category_id === state.activeCategory || subIds.includes(d.category_id));
  }
  if (!dishes.length) {
    grid.innerHTML = '<div class="empty"><div class="big">📭</div>Hozircha taomlar yo\'q.<br>Admin panelda qo\'shing.</div>';
    return;
  }
  grid.innerHTML = dishes.map(d => {
    const q = dishQty(d.id);
    const out = !d.is_available;
    return `
    <div class="dish-card">
      ${dishImage(d)}
      <div class="body">
        <h4>${esc(d.title)} ${d.is_hit ? '<span class="badge hit">HIT</span>' : ''} ${d.is_vegan ? '<span class="badge vegan">V</span>' : ''}</h4>
        <div class="desc">${esc(d.description)}</div>
        <div class="price-row">
          <span class="price">${fmt(d.price)}${d.old_price ? `<span class="old">${fmt(d.old_price)}</span>` : ''}</span>
          ${out ? '<span class="badge out">Tugagan</span>' : (q
            ? `<span class="qty"><button data-minus="${d.id}">−</button><span>${q}</span><button data-plus="${d.id}">+</button></span>`
            : `<button class="add-btn" data-add="${d.id}">+ Qo'shish</button>`)}
        </div>
      </div>
    </div>`;
  }).join('');
  $$('[data-add]').forEach(b => b.onclick = () => changeQty(parseInt(b.dataset.add, 10), 1));
  $$('[data-plus]').forEach(b => b.onclick = () => changeQty(parseInt(b.dataset.plus, 10), 1));
  $$('[data-minus]').forEach(b => b.onclick = () => changeQty(parseInt(b.dataset.minus, 10), -1));
}

function changeQty(dishId, delta) {
  const cur = state.cart[dishId] || 0;
  const next = Math.max(0, cur + delta);
  if (next === 0) delete state.cart[dishId]; else state.cart[dishId] = next;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  renderDishes();
  updateCartBar();
}

function cartItems() {
  return Object.entries(state.cart).map(([id, qty]) => {
    const d = state.catalog.dishes.find(x => x.id === parseInt(id, 10));
    return d ? Object.assign({}, d, { qty }) : null;
  }).filter(Boolean);
}

function cartTotal() {
  return cartItems().reduce((s, d) => s + d.price * d.qty, 0);
}

function updateCartBar() {
  const items = cartItems();
  $('#cartCount').textContent = items.reduce((s, d) => s + d.qty, 0) + ' ta mahsulot';
  $('#cartSum').textContent = fmt(cartTotal());
}

/* ============================ cart & checkout ============================ */
function renderCart() {
  const v = $('#view');
  const items = cartItems();
  if (!items.length) {
    v.innerHTML = '<div class="empty"><div class="big">🛒</div>Savat bo\'sh. Menyudan tanlang.</div>';
    return;
  }
  v.innerHTML = `
    <div class="card">
      ${items.map(d => `
        <div class="row">
          ${d.image_key ? `<img class="thumb" src="/api/media/${esc(d.image_key)}">` : '<div class="thumb ph">🍽</div>'}
          <div class="grow">
            <div class="title">${esc(d.title)}</div>
            <div class="sub">${fmt(d.price)}</div>
          </div>
          <span class="qty"><button data-minus="${d.id}">−</button><span>${d.qty}</span><button data-plus="${d.id}">+</button></span>
        </div>`).join('')}
    </div>
    <div id="upsellBox"></div>
    <button class="btn btn-primary btn-block" id="toCheckout">Davom etish — ${fmt(cartTotal())}</button>
    <div style="height:12px"></div>`;
  $$('[data-plus]').forEach(b => b.onclick = () => changeQty(parseInt(b.dataset.plus, 10), 1));
  $$('[data-minus]').forEach(b => b.onclick = () => changeQty(parseInt(b.dataset.minus, 10), -1));
  $('#toCheckout').onclick = openCheckout;
  renderUpsell(items);
}

function renderUpsell(items) {
  const box = $('#upsellBox');
  const cfg = state.upsell || {};
  if (cfg.enabled === false || !cfg.groups) { box.innerHTML = ''; return; }
  const suggestions = [];
  Object.entries(cfg.groups || {}).forEach(([group, gcfg]) => {
    const inCart = items.some(d => (d.upsell_group || '') === group);
    if (!inCart) return;
    state.catalog.dishes
      .filter(d => (d.upsell_group || '') === group && d.is_available && !dishQty(d.id))
      .slice(0, 3)
      .forEach(d => suggestions.push({ dish: d, gcfg }));
  });
  if (!suggestions.length) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <div class="card">
      <div class="section-title">${esc(cfg.title || 'Birga buyurtma qilasizmi?')}</div>
      ${suggestions.map(({ dish }) => `
        <div class="upell-card">
          ${dish.image_key ? `<img class="thumb" src="/api/media/${esc(dish.image_key)}">` : '<div class="thumb ph">🥤</div>'}
          <div class="grow">
            <div class="title" style="font-size:13px">${esc(dish.title)}</div>
            <div class="sub">${fmt(dish.price)}</div>
          </div>
          <button class="btn btn-accent btn-sm" data-upsell="${dish.id}">+</button>
        </div>`).join('')}
    </div>`;
  $$('[data-upsell]').forEach(b => b.onclick = () => {
    const id = parseInt(b.dataset.upsell, 10);
    state.cart[id] = (state.cart[id] || 0) + 1;
    toast('Savatga qo\'shildi');
    renderCart();
    updateCartBar();
  });
}

async function openCheckout() {
  const items = cartItems();
  if (!items.length) return;
  const branch = state.branches.find(b => b.id === state.branchId);
  const itemsTotal = cartTotal();
  const sheet = $('#sheet');
  const backdrop = $('#sheetBackdrop');

  sheet.innerHTML = `
    <div class="grabber"></div>
    <h3>📦 Buyurtmani rasmiylashtirish</h3>
    <div class="form-grid">
      <div>
        <label class="f">Buyurtma turi</label>
        <div style="display:flex; gap:6px">
          <button class="chip active" data-kind="delivery">🛵 Yetkazish</button>
          <button class="chip" data-kind="pickup">🏪 Olib ketish</button>
        </div>
      </div>
      <div id="addrBlock">
        <label class="f">Manzil</label>
        <input id="addrInput" placeholder="Manzilingizni kiriting">
        <button class="btn btn-ghost btn-sm" id="geoBtn" style="margin-top:6px">📍 GPS yuborish</button>
      </div>
      <div>
        <label class="f">Telefon</label>
        <input id="phoneInput" placeholder="+998 90 123 45 67" value="${esc(state.me.phone || '')}">
      </div>
      <div>
        <label class="f">To'lov</label>
        <div style="display:flex; gap:6px">
          <button class="chip active" data-pay="cash">💵 Naqd</button>
          <button class="chip" data-pay="card">💳 Karta (kursatka)</button>
        </div>
      </div>
      <div>
        <label class="f">Izoh</label>
        <textarea id="commentInput" rows="2" placeholder="Qo'shimcha izoh..."></textarea>
      </div>
      <div class="card" style="margin:0; background:var(--bg)">
        <div class="row" style="border:none">
          <div class="grow"><div class="title" style="font-size:13px">Taomlar</div></div>
          <div class="title">${fmt(itemsTotal)}</div>
        </div>
        <div class="row" style="border:none">
          <div class="grow"><div class="title" style="font-size:13px">Yetkazish</div></div>
          <div class="title" id="feePreview">—</div>
        </div>
        <div class="row" style="border:none">
          <div class="grow"><div class="title" style="font-size:15px">Jami</div></div>
          <div class="title" id="grandTotal">${fmt(itemsTotal)}</div>
        </div>
      </div>
      <button class="btn btn-primary btn-block" id="placeOrder">✅ Buyurtma berish</button>
      <div class="muted" style="text-align:center">Yetkazish manzili va telefon aniqligini tekshiring</div>
    </div>`;

  let kind = 'delivery', payment = 'cash';
  let lat = null, lon = null;

  function bindChips(attr, setter) {
    $$(`[data-${attr}]`).forEach(c => c.onclick = () => {
      $$(`[data-${attr}]`).forEach(x => x.classList.remove('active'));
      c.classList.add('active');
      setter(c.dataset[attr]);
    });
  }
  bindChips('kind', (k) => {
    kind = k;
    $('#addrBlock').classList.toggle('hidden', k !== 'delivery');
    previewFee();
  });
  bindChips('pay', (p) => { payment = p; });

  $('#geoBtn').onclick = () => {
    if (!tg || !tg.GestureEvent) {
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          (pos) => { lat = pos.coords.latitude; lon = pos.coords.longitude; previewFee(); toast('GPS olindi'); },
          () => toast('GPS ruxsati berilmadi'));
      }
      return;
    }
    tg.requestLocation((ok, res) => {
      if (ok) { lat = res.latitude; lon = res.longitude; previewFee(); toast('GPS olindi'); }
      else toast('GPS ruxsati berilmadi');
    });
  };

  async function previewFee() {
    // client-side estimation only; server is authoritative
    let fee = 0;
    if (kind === 'delivery') {
      const freeOver = state.rules.find(r => r.rule_type === 'free_over' && r.free_over && itemsTotal >= r.free_over);
      if (freeOver) fee = 0;
      else {
        const flat = state.rules.find(r => r.rule_type === 'flat');
        fee = flat ? flat.amount : 0;
      }
    }
    $('#feePreview').textContent = fee === 0 ? 'Bepul 🎉' : fmt(fee);
    $('#grandTotal').textContent = fmt(itemsTotal + fee);
  }
  previewFee();

  $('#placeOrder').onclick = async () => {
    const address = $('#addrInput').value.trim();
    const phone = $('#phoneInput').value.trim();
    if (kind === 'delivery' && !address) { showError('Manzilni kiriting'); return; }
    if (!phone) { showError('Telefon raqamini kiriting'); return; }
    const btn = $('#placeOrder');
    btn.disabled = true; btn.textContent = 'Yuborilmoqda…';
    try {
      const res = await API.post('/api/orders', {
        branch_id: state.branchId,
        kind, payment,
        items: items.map(d => ({ id: d.id, qty: d.qty })),
        address, phone,
        lat, lon,
        comment: $('#commentInput').value.trim(),
      });
      state.cart = {};
      closeSheet();
      toast('✅ Buyurtma qabul qilindi!');
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      activeTab = 'orders';
      renderView();  // refreshes orders via loadOrdersTab
    } catch (e) {
      btn.disabled = false; btn.textContent = '✅ Buyurtma berish';
      showError(e.message);
    }
  };

  openSheet();
}

/* ============================ sheet utils ============================ */
function openSheet() {
  $('#sheetBackdrop').classList.add('show');
  $('#sheet').classList.add('show');
}
function closeSheet() {
  $('#sheetBackdrop').classList.remove('show');
  $('#sheet').classList.remove('show');
}
$('#sheetBackdrop').onclick = closeSheet;

/* ============================ my orders ============================ */
async function loadOrdersTab() {
  const res = await API.get('/api/orders/my');
  state.myOrders = res.orders;
  renderMyOrders();
}

const STATUS_LABELS = {
  new: '🆕 Yangi', accepted: '👩‍🍳 Qabul qilindi', cooking: '🔥 Tayyorlanmoqda',
  ready: '🛎 Tayyor', courier_assigned: '🛵 Kuryer olindi',
  delivering: '🚀 Yetkazilmoqda', delivered: '✅ Yetkazildi',
  cancelled: '❌ Bekor qilindi',
};

function renderMyOrders() {
  const v = $('#view');
  const orders = state.myOrders;
  if (!orders.length) {
    v.innerHTML = '<div class="empty"><div class="big">📦</div>Buyurtmalar tarixi bo\'sh.</div>';
    return;
  }
  v.innerHTML = orders.map(o => `
    <div class="card">
      <div class="row" style="border:none">
        <div class="grow">
          <div class="title">#${esc(o.public_code)} · ${fmt(o.total)}</div>
          <div class="sub">${esc(o.branch ? o.branch.name : '')} · ${new Date(o.created_at * 1000).toLocaleString('ru-RU')}</div>
        </div>
        <span class="status-chip st-${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
      </div>
      ${o.courier ? `<div class="sub" style="margin-top:4px">🛵 ${esc(o.courier.name)} · ${esc(o.courier.phone || '')}</div>` : ''}
      <div class="actions">
        <button class="btn btn-ghost btn-sm" data-track="${o.id}">Batafsil</button>
        ${['new', 'accepted'].includes(o.status) ? `<button class="btn btn-danger btn-sm" data-cancel="${o.id}">Bekor qilish</button>` : ''}
      </div>
    </div>`).join('');
  $$('[data-track]').forEach(b => b.onclick = () => trackOrder(parseInt(b.dataset.track, 10)));
  $$('[data-cancel]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog('Buyurtmani bekor qilasizmi?'))) return;
    try {
      await API.post(`/api/orders/${b.dataset.cancel}/cancel`);
      toast('Bekor qilindi');
      loadOrdersTab();
    } catch (e) { showError(e.message); }
  });
}

async function trackOrder(orderId) {
  const res = await API.get('/api/orders/' + orderId);
  const o = res.order;
  const sheet = $('#sheet');
  sheet.innerHTML = `
    <div class="grabber"></div>
    <h3>📦 Buyurtma #${esc(o.public_code)}</h3>
    <span class="status-chip st-${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
    <div class="card" style="margin-top:10px">
      ${(o.items || []).map(i => `<div class="row" style="border:none">
        <div class="grow"><div class="title" style="font-size:13.5px">${esc(i.title)} × ${i.qty}</div></div>
        <div class="title" style="font-size:13px">${fmt(i.price * i.qty)}</div>
      </div>`).join('')}
      <div class="row" style="border:none"><div class="grow muted">Yetkazish</div><div>${fmt(o.delivery_fee)}</div></div>
      <div class="row" style="border:none"><div class="grow"><b>Jami</b></div><div><b>${fmt(o.total)}</b></div></div>
    </div>
    <div class="muted">📍 ${esc(o.address || '—')} · ☎️ ${esc(o.phone || '—')}</div>`;
  openSheet();
}

/* ============================ courier board ============================ */
async function renderCourier() {
  const v = $('#view');
  v.innerHTML = '<div class="spin"></div>';
  const data = await API.get('/api/courier/board');
  if (!data.registered) {
    v.innerHTML = `
      <div class="card">
        <div class="section-title">🛵 Kuryer bo'limiga ulanish</div>
        <p class="muted">Admin sizga PIN kod bergan bo'lsa kiriting yoki /courier buyrug'ini yuboring.</p>
        <div class="form-grid" style="margin-top:10px">
          <input id="pinInput" placeholder="PIN kod (masalan A1B2C3)">
          <button class="btn btn-primary btn-block" id="pinBtn">Ulanish</button>
        </div>
      </div>`;
    $('#pinBtn').onclick = async () => {
      try {
        await API.post('/api/courier/pin', { pin: $('#pinInput').value.trim() });
        toast('Ulandi! Endi navbatga chiqing.');
        renderCourier();
      } catch (e) { showError(e.message); }
    };
    return;
  }
  state.courier = data;
  state.courierStatus = data.profile.status;
  const online = data.profile.status === 'online' || data.profile.status === 'delivering';
  v.innerHTML = `
    <div class="card" style="display:flex; align-items:center; gap:12px">
      <div class="grow">
        <div class="title">${esc(data.profile.first_name || 'Kuryer')}</div>
        <div class="sub">📦 ${data.profile.total_orders} yetkazish · ⭐️ ${Number(data.profile.rating).toFixed(1)} · PIN: <code class="pin">${esc(data.profile.pin_code)}</code></div>
      </div>
      <button class="toggle ${online ? 'on' : ''}" id="statusToggle"></button>
    </div>
    <div class="section-title">🔥 Faol buyurtmalar</div>
    <div id="activeOrders"></div>
    <div class="section-title">📋 Mavjud buyurtmalar</div>
    <div id="availableOrders"></div>
    <div class="section-title">🗂 Oxirgi yetkazishlar</div>
    <div id="historyOrders"></div>`;

  $('#statusToggle').onclick = async () => {
    const next = state.courierStatus === 'online' || state.courierStatus === 'delivering' ? 'offline' : 'online';
    try {
      await API.post('/api/courier/status', { status: next });
      state.courierStatus = next;
      toast(next === 'online' ? 'Siz navbatdasiz' : 'Navbatdan chiqdingiz');
      renderCourier();
    } catch (e) { showError(e.message); }
  };

  const renderList = (sel, orders, emptyText) => {
    const el = $(sel);
    if (!orders.length) { el.innerHTML = `<div class="empty" style="padding:18px">${emptyText}</div>`; return; }
    el.innerHTML = orders.map(o => `
      <div class="card">
        <div class="row" style="border:none">
          <div class="grow">
            <div class="title">#${esc(o.public_code)} · ${fmt(o.total)}</div>
            <div class="sub">📍 ${esc(o.address || '—')} · ☎️ ${esc(o.phone || '—')}</div>
            <div class="sub">${(o.items || []).map(i => `${esc(i.title)} ×${i.qty}`).join(', ')}</div>
          </div>
          <span class="status-chip st-${esc(o.status)}">${STATUS_LABELS[o.status] || o.status}</span>
        </div>
        <div class="actions">
          ${o.status === 'delivering' && o.courier && o.courier.id === data.profile.id
            ? '<button class="btn btn-accent btn-sm" data-deliver="' + o.id + '">✅ Yetkazildi</button>' : ''}
          ${o.courier_id == null
            ? '<button class="btn btn-primary btn-sm" data-claim="' + o.id + '">✅ Olib boraman</button>' : ''}
        </div>
      </div>`).join('');
    $$('[data-claim]', el).forEach(b => b.onclick = async () => {
      try {
        const res = await API.post('/api/courier/claim/' + b.dataset.claim);
        toast(res.message || 'Olindi');
        renderCourier();
      } catch (e) { showError(e.message); renderCourier(); }
    });
    $$('[data-deliver]', el).forEach(b => b.onclick = async () => {
      try {
        await API.post('/api/courier/deliver/' + b.dataset.deliver);
        toast('✅ Yetkazildi!');
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
        renderCourier();
      } catch (e) { showError(e.message); }
    });
  };
  renderList('#activeOrders', data.active_orders, 'Faol buyurtma yo\'q');
  renderList('#availableOrders', data.available_orders, 'Hozir yangi buyurtma yo\'q');
  renderList('#historyOrders', data.history, 'Hali yetkazilmagan');
}

/* ============================ admin dashboard ============================ */
async function renderAdmin() {
  const v = $('#view');
  v.innerHTML = '<div class="spin"></div>';
  if (!state.branches.length) { v.innerHTML = '<div class="empty">Filial topilmadi</div>'; return; }
  if (!state.admin.branchId) state.admin.branchId = state.branchId;
  await renderAdminHome();
}

async function renderAdminHome() {
  const v = $('#view');
  const data = await API.get('/api/admin/stats?branch_id=' + state.admin.branchId,
    { 'X-Branch-Id': state.admin.branchId });
  const byDay = data.by_day || [];
  const maxRev = Math.max(1, ...byDay.map(d => d.revenue));
  v.innerHTML = `
    <div class="card" style="display:flex; align-items:center; gap:10px">
      <div class="grow">
        <div class="title">Filial: ${esc(data.branch.name)}</div>
        <div class="sub">${esc(data.branch.address)}</div>
      </div>
      <select id="adminBranch" style="width:auto">
        ${state.branches.map(b => `<option value="${b.id}" ${b.id === state.admin.branchId ? 'selected' : ''}>${esc(b.name)}</option>`).join('')}
      </select>
    </div>
    <div class="kpi-grid">
      <div class="kpi"><b>${fmt(data.today.revenue)}</b><span>bugun</span></div>
      <div class="kpi"><b>${data.today.orders}</b><span>buyurtma</span></div>
      <div class="kpi"><b>${data.couriers.filter(c => c.status !== 'offline').length}/${data.couriers.length}</b><span>kuryer onlayn</span></div>
    </div>
    <div class="card">
      <div class="section-title">📈 14 kunlik tushum</div>
      <div class="bar-chart">
        ${byDay.map(d => `<div class="bar" style="height:${Math.max(4, 100 * d.revenue / maxRev)}%" title="${esc(d.day)}: ${fmt(d.revenue)}"><span>${esc(String(d.day).slice(8))}</span></div>`).join('') || '<div class="muted">Ma\'lumot yo\'q</div>'}
      </div>
    </div>
    <div class="card">
      <div class="section-title">🏆 Top taomlar</div>
      ${data.top_dishes.length ? data.top_dishes.map((d, i) => `
        <div class="row" style="border:none">
          <div class="grow"><div class="title" style="font-size:13.5px">${i + 1}. ${esc(d.title)}</div></div>
          <div class="sub">${d.qty} ta</div>
        </div>`).join('') : '<div class="muted">Hali sotuv yo\'q</div>'}
    </div>
    <div class="section-title">Boshqaruv</div>
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <button class="btn btn-ghost" data-go="branches">🏪 Filiallar</button>
      <button class="btn btn-ghost" data-go="menu">📋 Menyu</button>
      <button class="btn btn-ghost" data-go="delivery">🛵 Yetkazish narxi</button>
      <button class="btn btn-ghost" data-go="branding">🎨 Dizayn</button>
      <button class="btn btn-ghost" data-go="couriers">🛵 Kuryerlar</button>
      <button class="btn btn-ghost" data-go="integrations">🔄 1C / Integratsiya</button>
    </div>`;
  $('#adminBranch').onchange = (e) => { state.admin.branchId = parseInt(e.target.value, 10); renderAdminHome(); };
  $$('[data-go]').forEach(b => b.onclick = () => {
    const pages = {
      branches: adminBranchesPage, menu: adminMenuPage, delivery: adminDeliveryPage,
      branding: adminBrandingPage, couriers: adminCouriersPage, integrations: adminIntegrationsPage,
    };
    pages[b.dataset.go]();
  });
}

/* ---------- branches ---------- */
async function adminBranchesPage() {
  const v = $('#view');
  const data = await API.get('/api/admin/branches');
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">🏪 Filiallar va brendlar</div>
    ${data.brands.map(br => `<div class="card"><div class="section-title" style="margin:0">🏷 ${esc(br.name)}</div>
      <div class="muted">${br.description || ''}</div>
      ${data.branches.filter(b => b.brand_id === br.id).map(b => `
        <div class="row">
          <div class="grow">
            <div class="title">${esc(b.name)} ${b.is_active ? '' : '<span class="badge out">off</span>'}</div>
            <div class="sub">${esc(b.city)} · ${esc(b.address)}</div>
          </div>
          <button class="btn btn-ghost btn-sm" data-edit-branch="${b.id}">✏️</button>
          <button class="btn btn-danger btn-sm" data-del-branch="${b.id}">🗑</button>
        </div>`).join('')}
    </div>`).join('')}
    <button class="btn btn-primary btn-block" id="addBranch">+ Yangi filial</button>
    <div style="height:10px"></div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);

  function branchForm(existing) {
    const sheet = $('#sheet');
    sheet.innerHTML = `
      <div class="grabber"></div>
      <h3>${existing ? 'Filialni tahrirlash' : 'Yangi filial'}</h3>
      <div class="form-grid">
        <div><label class="f">Nomi</label><input id="bfName" value="${existing ? esc(existing.name) : ''}"></div>
        <div class="two">
          <div><label class="f">Shahar</label><input id="bfCity" value="${existing ? esc(existing.city) : ''}"></div>
          <div><label class="f">Telefon</label><input id="bfPhone" value="${existing ? esc(existing.phone) : ''}"></div>
        </div>
        <div><label class="f">Manzil</label><input id="bfAddr" value="${existing ? esc(existing.address) : ''}"></div>
        <div class="two">
          <div><label class="f">Ochilish</label><input id="bfFrom" value="${existing ? esc(existing.work_from) : '09:00'}"></div>
          <div><label class="f">Yopilish</label><input id="bfTo" value="${existing ? esc(existing.work_to) : '22:00'}"></div>
        </div>
        <div class="two">
          <div><label class="f">Kenglik (lat)</label><input id="bfLat" value="${existing && existing.lat != null ? existing.lat : ''}"></div>
          <div><label class="f">Uzunlik (lon)</label><input id="bfLon" value="${existing && existing.lon != null ? existing.lon : ''}"></div>
        </div>
        <label class="check-row"><input type="checkbox" id="bfActive" ${!existing || existing.is_active ? 'checked' : ''}> Faol</label>
        <button class="btn btn-primary btn-block" id="bfSave">Saqlash</button>
      </div>`;
    $('#bfSave').onclick = async () => {
      const payload = {
        brand_id: existing ? existing.brand_id : (data.brands[0] ? data.brands[0].id : null),
        name: $('#bfName').value.trim(),
        city: $('#bfCity').value.trim(), phone: $('#bfPhone').value.trim(),
        address: $('#bfAddr').value.trim(),
        work_from: $('#bfFrom').value.trim(), work_to: $('#bfTo').value.trim(),
        lat: $('#bfLat').value ? parseFloat($('#bfLat').value) : null,
        lon: $('#bfLon').value ? parseFloat($('#bfLon').value) : null,
        is_active: $('#bfActive').checked,
      };
      if (!payload.name) { showError('Nomni kiriting'); return; }
      try {
        if (existing) await API.post('/api/admin/branches/' + existing.id, payload);
        else await API.post('/api/admin/branches', payload);
        closeSheet(); toast('Saqlandi'); adminBranchesPage();
      } catch (e) { showError(e.message); }
    };
    openSheet();
  }

  $('#addBranch').onclick = () => branchForm(null);
  $$('[data-edit-branch]').forEach(b => b.onclick = () => {
    const br = data.branches.find(x => x.id === parseInt(b.dataset.editBranch, 10));
    branchForm(br);
  });
  $$('[data-del-branch]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog('Filial va uning menyusi o\'chiriladi. Davom etamizmi?'))) return;
    try {
      await API.post('/api/admin/branches/' + b.dataset.delBranch + '/delete');
      toast('O\'chirildi'); adminBranchesPage();
    } catch (e) { showError(e.message); }
  });
}

/* ---------- menu builder ---------- */
async function adminMenuPage() {
  const v = $('#view');
  const data = await API.get('/api/catalog?branch_id=' + state.admin.branchId);
  const cats = data.categories, dishes = data.dishes;
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">📋 Menyu quruvchi</div>
    <div class="card">
      <div class="section-title">Kategoriyalar</div>
      ${cats.filter(c => !c.parent_id).map(c => `
        <div class="row">
          <div class="grow"><div class="title">${esc(c.emoji)} ${esc(c.title)}</div></div>
          <button class="btn btn-ghost btn-sm" data-add-sub="${c.id}">+ Sub</button>
          <button class="btn btn-ghost btn-sm" data-edit-cat="${c.id}">✏️</button>
          <button class="btn btn-danger btn-sm" data-del-cat="${c.id}">🗑</button>
        </div>
        ${cats.filter(s => s.parent_id === c.id).map(s => `
          <div class="row" style="padding-left:18px">
            <div class="grow"><div class="title" style="font-size:13px">↳ ${esc(s.title)}</div></div>
            <button class="btn btn-ghost btn-sm" data-edit-cat="${s.id}">✏️</button>
            <button class="btn btn-danger btn-sm" data-del-cat="${s.id}">🗑</button>
          </div>`).join('')}
      `).join('')}
      <button class="btn btn-primary btn-block btn-sm" id="addCat">+ Kategoriya</button>
    </div>
    <div class="card">
      <div class="section-title">Taomlar (${dishes.length})</div>
      ${dishes.map(d => `
        <div class="row">
          ${d.image_key ? `<img class="thumb" src="/api/media/${esc(d.image_key)}">` : '<div class="thumb ph">🍽</div>'}
          <div class="grow">
            <div class="title" style="font-size:13.5px">${esc(d.title)} ${d.is_hit ? '<span class="badge hit">HIT</span>' : ''}</div>
            <div class="sub">${fmt(d.price)} ${d.is_available ? '' : '· <b>tugagan</b>'}</div>
          </div>
          <button class="btn btn-ghost btn-sm" data-edit-dish="${d.id}">✏️</button>
          <button class="btn btn-danger btn-sm" data-del-dish="${d.id}">🗑</button>
        </div>`).join('')}
      <button class="btn btn-primary btn-block btn-sm" id="addDish">+ Taom qo'shish</button>
    </div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);

  function categoryForm(existing, parentId) {
    const sheet = $('#sheet');
    sheet.innerHTML = `
      <div class="grabber"></div>
      <h3>${existing ? 'Kategoriyani tahrirlash' : 'Yangi kategoriya'}</h3>
      <div class="form-grid">
        <div><label class="f">Nomi</label><input id="cfTitle" value="${existing ? esc(existing.title) : ''}"></div>
        <div><label class="f">Emoji</label><input id="cfEmoji" value="${existing ? esc(existing.emoji) : ''}" placeholder="🍕"></div>
        <button class="btn btn-primary btn-block" id="cfSave">Saqlash</button>
      </div>`;
    $('#cfSave').onclick = async () => {
      const payload = {
        branch_id: state.admin.branchId,
        title: $('#cfTitle').value.trim(), emoji: $('#cfEmoji').value.trim(),
        parent_id: existing ? existing.parent_id : (parentId || null),
      };
      if (!payload.title) { showError('Nomni kiriting'); return; }
      try {
        if (existing) await API.post('/api/admin/categories/' + existing.id, payload);
        else await API.post('/api/admin/categories', payload);
        closeSheet(); toast('Saqlandi'); adminMenuPage();
      } catch (e) { showError(e.message); }
    };
    openSheet();
  }

  function dishForm(existing) {
    const sheet = $('#sheet');
    sheet.innerHTML = `
      <div class="grabber"></div>
      <h3>${existing ? 'Taomni tahrirlash' : "Yangi taom"}</h3>
      <div class="form-grid">
        <div><label class="f">Nomi</label><input id="dfTitle" value="${existing ? esc(existing.title) : ''}"></div>
        <div><label class="f">Tavsif</label><textarea id="dfDesc" rows="2">${existing ? esc(existing.description) : ''}</textarea></div>
        <div class="two">
          <div><label class="f">Narx (so'm)</label><input id="dfPrice" type="number" value="${existing ? existing.price : ''}"></div>
          <div><label class="f">Eski narx</label><input id="dfOld" type="number" value="${existing && existing.old_price ? existing.old_price : ''}"></div>
        </div>
        <div><label class="f">Kategoriya</label>
          <select id="dfCat">${cats.map(c => `<option value="${c.id}" ${existing && existing.category_id === c.id ? 'selected' : ''}>${esc(c.parent_id ? '↳ ' : '')}${esc(c.title)}</option>`).join('')}</select>
        </div>
        <div><label class="f">Rasm</label><input type="file" id="dfImage" accept="image/*"></div>
        <div><label class="f">Upsell guruh</label><input id="dfGroup" value="${existing ? esc(existing.upsell_group || '') : ''}" placeholder="masalan _drink"></div>
        <label class="check-row"><input type="checkbox" id="dfAvail" ${!existing || existing.is_available ? 'checked' : ''}> Sotuvda</label>
        <label class="check-row"><input type="checkbox" id="dfHit" ${existing && existing.is_hit ? 'checked' : ''}> HIT belgisi</label>
        <label class="check-row"><input type="checkbox" id="dfVegan" ${existing && existing.is_vegan ? 'checked' : ''}> Vegetarian</label>
        <button class="btn btn-primary btn-block" id="dfSave">Saqlash</button>
      </div>`;
    $('#dfSave').onclick = async () => {
      const payload = {
        branch_id: state.admin.branchId,
        category_id: parseInt($('#dfCat').value, 10),
        title: $('#dfTitle').value.trim(),
        description: $('#dfDesc').value.trim(),
        price: parseInt($('#dfPrice').value || '0', 10),
        old_price: $('#dfOld').value ? parseInt($('#dfOld').value, 10) : null,
        is_available: $('#dfAvail').checked,
        is_hit: $('#dfHit').checked,
        is_vegan: $('#dfVegan').checked,
        upsell_group: $('#dfGroup').value.trim(),
      };
      if (!payload.title) { showError('Nomni kiriting'); return; }
      if (!payload.price) { showError('Narxni kiriting'); return; }
      try {
        const img = $('#dfImage').files[0];
        if (img) {
          const fd = new FormData();
          fd.append('file', img); fd.append('kind', 'dish');
          const up = await API.postForm('/api/admin/media', fd);
          payload.image_key = up.key;
        }
        if (existing) await API.post('/api/admin/dishes/' + existing.id, payload);
        else await API.post('/api/admin/dishes', payload);
        closeSheet(); toast('Saqlandi'); adminMenuPage();
      } catch (e) { showError(e.message); }
    };
    openSheet();
  }

  $('#addCat').onclick = () => categoryForm(null, null);
  $$('[data-add-sub]').forEach(b => b.onclick = () => categoryForm(null, parseInt(b.dataset.addSub, 10)));
  $$('[data-edit-cat]').forEach(b => b.onclick = () => {
    categoryForm(cats.find(c => c.id === parseInt(b.dataset.editCat, 10)));
  });
  $$('[data-del-cat]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog('Kategoriya va ichidagi taomlar o\'chiriladi!'))) return;
    try { await API.post('/api/admin/categories/' + b.dataset.delCat + '/delete'); toast('O\'chirildi'); adminMenuPage(); }
    catch (e) { showError(e.message); }
  });
  $('#addDish').onclick = () => dishForm(null);
  $$('[data-edit-dish]').forEach(b => b.onclick = () => {
    dishForm(dishes.find(d => d.id === parseInt(b.dataset.editDish, 10)));
  });
  $$('[data-del-dish]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog('Taomni o\'chirilsinmi?'))) return;
    try { await API.post('/api/admin/dishes/' + b.dataset.delDish + '/delete'); toast('O\'chirildi'); adminMenuPage(); }
    catch (e) { showError(e.message); }
  });
}

/* ---------- delivery rules ---------- */
async function adminDeliveryPage() {
  const v = $('#view');
  const bid = state.admin.branchId;
  const data = await API.get('/api/admin/delivery-rules?branch_id=' + bid, { 'X-Branch-Id': bid });
  const RULE_LABELS = {
    free_over: 'Bepul chegarasi bilan', flat: 'Doimiy narx',
    per_km: 'Har km uchun', percent: 'Foiz',
  };
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">🛵 Yetkazish narxlari</div>
    <div class="muted" style="margin-bottom:10px">Yuqoridan pastga tekshiriladi — birinchi mos qoida g'alaba qozonadi.</div>
    ${data.rules.map(r => `
      <div class="card">
        <div class="row" style="border:none">
          <div class="grow">
            <div class="title">${RULE_LABELS[r.rule_type] || r.rule_type}</div>
            <div class="sub">${r.free_over ? 'Bepul ' + fmt(r.free_over) + ' dan: ' + fmt(r.amount || 0)
              : r.rule_type === 'percent' ? r.percent + '%'
              : r.rule_type === 'per_km' ? fmt(r.amount) + '/km'
              : fmt(r.amount)}${r.max_km ? ' · maks ' + r.max_km + 'km' : ''}${r.min_total ? ' · min ' + fmt(r.min_total) : ''}</div>
          </div>
          <button class="toggle ${r.is_active ? 'on' : ''}" data-tgl="${r.id}"></button>
          <button class="btn btn-ghost btn-sm" data-edit="${r.id}">✏️</button>
          <button class="btn btn-danger btn-sm" data-del="${r.id}">🗑</button>
        </div>
      </div>`).join('')}
    <button class="btn btn-primary btn-block" id="addRule">+ Qoida qo'shish</button>
    <div style="height:10px"></div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);

  function ruleForm(existing) {
    const sheet = $('#sheet');
    sheet.innerHTML = `
      <div class="grabber"></div>
      <h3>${existing ? 'Qoidani tahrirlash' : "Yangi qoida"}</h3>
      <div class="form-grid">
        <div><label class="f">Turi</label>
          <select id="rfType">
            <option value="free_over" ${existing && existing.rule_type === 'free_over' ? 'selected' : ''}>free_over — X so'mdan yuqorida bepul</option>
            <option value="flat" ${existing && existing.rule_type === 'flat' ? 'selected' : ''}>flat — doimiy narx</option>
            <option value="per_km" ${existing && existing.rule_type === 'per_km' ? 'selected' : ''}>per_km — km boshiga</option>
            <option value="percent" ${existing && existing.rule_type === 'percent' ? 'selected' : ''}>percent — foiz</option>
          </select>
        </div>
        <div class="two">
          <div><label class="f">Summa (so'm)</label><input id="rfAmount" type="number" value="${existing ? existing.amount : 15000}"></div>
          <div><label class="f">Bepul chegarasi</label><input id="rfFree" type="number" value="${existing && existing.free_over ? existing.free_over : ''}"></div>
        </div>
        <div class="two">
          <div><label class="f">Min savat</label><input id="rfMin" type="number" value="${existing ? existing.min_total : 0}"></div>
          <div><label class="f">Maks km</label><input id="rfMaxKm" type="number" step="0.1" value="${existing && existing.max_km != null ? existing.max_km : ''}"></div>
        </div>
        <div><label class="f">Foiz (%)</label><input id="rfPercent" type="number" step="0.1" value="${existing ? existing.percent : 0}"></div>
        <button class="btn btn-primary btn-block" id="rfSave">Saqlash</button>
      </div>`;
    $('#rfSave').onclick = async () => {
      const payload = {
        rule_type: $('#rfType').value,
        amount: parseInt($('#rfAmount').value || '0', 10),
        free_over: $('#rfFree').value ? parseInt($('#rfFree').value, 10) : null,
        min_total: parseInt($('#rfMin').value || '0', 10),
        max_km: $('#rfMaxKm').value ? parseFloat($('#rfMaxKm').value) : null,
        percent: parseFloat($('#rfPercent').value || '0'),
        is_active: existing ? existing.is_active : true,
      };
      try {
        if (existing) await API.post('/api/admin/delivery-rules/' + existing.id, payload);
        else await API.post('/api/admin/delivery-rules?branch_id=' + bid, payload, { 'X-Branch-Id': bid });
        closeSheet(); toast('Saqlandi'); adminDeliveryPage();
      } catch (e) { showError(e.message); }
    };
    openSheet();
  }

  $('#addRule').onclick = () => ruleForm(null);
  $$('[data-edit]').forEach(b => b.onclick = () => ruleForm(data.rules.find(r => r.id === parseInt(b.dataset.edit, 10))));
  $$('[data-tgl]').forEach(b => b.onclick = async () => {
    const r = data.rules.find(x => x.id === parseInt(b.dataset.tgl, 10));
    try {
      await API.post('/api/admin/delivery-rules/' + r.id, { is_active: !r.is_active });
      adminDeliveryPage();
    } catch (e) { showError(e.message); }
  });
  $$('[data-del]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog("Qoidani o'chirilsinmi?"))) return;
    try { await API.post('/api/admin/delivery-rules/' + b.dataset.del + '/delete'); toast("O'chirildi"); adminDeliveryPage(); }
    catch (e) { showError(e.message); }
  });
}

/* ---------- branding ---------- */
async function adminBrandingPage() {
  const v = $('#view');
  const bid = state.admin.branchId;
  const data = await API.get('/api/admin/branding?branch_id=' + bid, { 'X-Branch-Id': bid });
  const t = data.theme || {};
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">🎨 Dizayn va brending</div>
    <div class="card">
      <div class="form-grid two">
        <div><label class="f">Asosiy rang</label><input type="color" id="thPrimary" value="${t.primary || '#B4552D'}"></div>
        <div><label class="f">Aksent rang</label><input type="color" id="thAccent" value="${t.accent || '#2D6A4F'}"></div>
        <div><label class="f">Fon</label><input type="color" id="thBg" value="${t.bg || '#FAF6F0'}"></div>
        <div><label class="f">Karta rangi</label><input type="color" id="thCard" value="${t.card || '#FFFFFF'}"></div>
        <div><label class="f">Matn rangi</label><input type="color" id="thText" value="${t.text || '#2B2118'}"></div>
        <div><label class="f">Burchak radiusi</label><input type="number" id="thRadius" value="${t.radius || 16}"></div>
      </div>
      <div style="margin-top:10px"><label class="f">Shrift</label>
        <select id="thFont">
          <option value="system-ui, sans-serif" ${t.font && t.font.includes('system-ui') ? 'selected' : ''}>Zamonaviy (system-ui)</option>
          <option value="Georgia, serif" ${t.font && t.font.includes('Georgia') ? 'selected' : ''}>Klassik (Georgia)</option>
          <option value="'Courier New', monospace" ${t.font && t.font.includes('Courier') ? 'selected' : ''}>Monospace</option>
        </select>
      </div>
      <div class="actions">
        <button class="btn btn-ghost btn-sm" id="thPreset1">☎️ Klassik</button>
        <button class="btn btn-ghost btn-sm" id="thPreset2">🌿 Yashil</button>
        <button class="btn btn-ghost btn-sm" id="thPreset3">🌙 Tungi</button>
        <button class="btn btn-primary btn-sm" id="thSave">Saqlash</button>
      </div>
      <div class="muted" style="margin-top:8px">Saqlanganda barcha mijozlarning ilovasi yangi dizayn bilan ochiladi.</div>
    </div>
    <div class="card">
      <div class="section-title">Logotip va banner</div>
      <div style="display:flex; gap:10px; align-items:center; margin-bottom:10px">
        ${data.logo_url ? `<img class="logo" style="width:56px;height:56px;border-radius:12px" src="${data.logo_url}">` : '<div class="thumb ph" style="width:56px;height:56px;border-radius:12px">📚</div>'}
        ${data.banner_url ? `<img style="width:120px;height:56px;object-fit:cover;border-radius:12px" src="${data.banner_url}">` : ''}
      </div>
      <div class="form-grid two">
        <div><label class="f">Logotip yuklash</label><input type="file" id="logoFile" accept="image/*"></div>
        <div><label class="f">Banner yuklash</label><input type="file" id="bannerFile" accept="image/*"></div>
      </div>
      <div class="actions"><button class="btn btn-primary btn-block" id="mediaSave">Yuklash va saqlash</button></div>
    </div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);

  const presets = {
    thPreset1: { primary: '#B4552D', accent: '#2D6A4F', bg: '#FAF6F0', card: '#FFFFFF', text: '#2B2118' },
    thPreset2: { primary: '#2D6A4F', accent: '#95D5B2', bg: '#F3F9F4', card: '#FFFFFF', text: '#1B2A22' },
    thPreset3: { primary: '#7C3AED', accent: '#22D3EE', bg: '#0F172A', card: '#1E293B', text: '#E2E8F0' },
  };
  Object.entries(presets).forEach(([id, p]) => {
    $('#' + id).onclick = () => {
      $('#thPrimary').value = p.primary; $('#thAccent').value = p.accent;
      $('#thBg').value = p.bg; $('#thCard').value = p.card; $('#thText').value = p.text;
      applyTheme(Object.assign({}, p, { radius: parseInt($('#thRadius').value, 10) }));
    };
  });

  $('#thSave').onclick = async () => {
    const theme = {
      primary: $('#thPrimary').value, accent: $('#thAccent').value,
      bg: $('#thBg').value, card: $('#thCard').value, text: $('#thText').value,
      radius: parseInt($('#thRadius').value, 10) || 16,
      font: $('#thFont').value,
    };
    try {
      await API.post('/api/admin/branding?branch_id=' + bid, { theme }, { 'X-Branch-Id': bid });
      toast('Dizayn saqlandi'); applyTheme(theme);
    } catch (e) { showError(e.message); }
  };

  $('#mediaSave').onclick = async () => {
    try {
      const logo = $('#logoFile').files[0], banner = $('#bannerFile').files[0];
      if (!logo && !banner) { toast('Fayl tanlanmagan'); return; }
      const payload = {};
      if (logo) {
        const fd = new FormData(); fd.append('file', logo); fd.append('kind', 'logo');
        const up = await API.postForm('/api/admin/media', fd);
        payload.logo_key = up.key;
      }
      if (banner) {
        const fd = new FormData(); fd.append('file', banner); fd.append('kind', 'banner');
        const up = await API.postForm('/api/admin/media', fd);
        payload.banner_key = up.key;
      }
      await API.post('/api/admin/branding?branch_id=' + bid, payload, { 'X-Branch-Id': bid });
      toast('Yuklandi!'); adminBrandingPage();
    } catch (e) { showError(e.message); }
  };
}

/* ---------- couriers ---------- */
async function adminCouriersPage() {
  const v = $('#view');
  const data = await API.get('/api/admin/couriers');
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">🛵 Kuryerlar</div>
    ${data.couriers.length ? data.couriers.map(c => `
      <div class="card">
        <div class="row" style="border:none">
          <div class="grow">
            <div class="title">${esc(c.first_name || c.username || c.user_id)}</div>
            <div class="sub">📦 ${c.total_orders} · ⭐️ ${Number(c.rating).toFixed(1)} · PIN: <code class="pin">${esc(c.pin_code)}</code></div>
          </div>
          <span class="status-chip st-${c.status === 'online' ? 'ready' : c.status === 'delivering' ? 'delivering' : 'cancelled'}">${c.status}</span>
        </div>
      </div>`).join('') : '<div class="empty">Kuryerlar yo\'q. Kuryerning Telegram ID sini kiriting.</div>'}
    <div class="card">
      <div class="section-title">+ Yangi kuryer</div>
      <div class="form-grid">
        <div><label class="f">Kuryer Telegram ID</label><input id="courTg" placeholder="masalan 6123456789"></div>
        <div><label class="f">Transport</label>
          <select id="courTransport">
            <option value="foot">Piyoda</option><option value="bike">Velosiped</option><option value="car">Avtomobil</option>
          </select>
        </div>
        <button class="btn btn-primary btn-block" id="courAdd">Qo'shish</button>
        <div class="muted">Kuryer ilovaga kirib, shu PIN orqali ulanadi yoki /courier buyrug'ini yuboradi.</div>
      </div>
    </div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);
  $('#courAdd').onclick = async () => {
    const tgId = parseInt($('#courTg').value, 10);
    if (!tgId) { showError('Telegram ID kiriting'); return; }
    try {
      await API.post('/api/admin/couriers', { tg_id: tgId, transport: $('#courTransport').value });
      toast("Kuryer qo'shildi"); adminCouriersPage();
    } catch (e) { showError(e.message); }
  };
}

/* ---------- integrations (1C) ---------- */
async function adminIntegrationsPage() {
  const v = $('#view');
  const data = await API.get('/api/admin/integrations?branch_id=' + state.admin.branchId);
  v.innerHTML = `
    <button class="btn btn-ghost btn-sm" data-back>← Orqaga</button>
    <div class="section-title" style="margin-top:10px">🔄 Ma'lumot almashinuvi (1C / 1UZ)</div>
    <div class="muted" style="margin-bottom:10px">
      Eksport: buyurtmalar webhook yoki JSON export sifatida uzatiladi.
      Import: 1C taomlar/narxlar/qoldiqlarni JSON hujjat sifatida yuboradi.
    </div>
    ${data.integrations.length ? data.integrations.map(i => `
      <div class="card">
        <div class="row" style="border:none">
          <div class="grow">
            <div class="title">${esc(i.name || i.system.toUpperCase())} ${i.is_active ? '' : '<span class="badge out">off</span>'}</div>
            <div class="sub">${esc(i.system)} · ${esc(i.direction)} · ${i.webhook_url ? esc(i.webhook_url.slice(0, 40)) + '…' : 'webhook yo\'q'}</div>
            <div class="sub">${i.last_sync_at ? 'Oxirgi sinx: ' + new Date(i.last_sync_at * 1000).toLocaleString('ru-RU') + ' (' + i.last_status + ')' : 'hali sinxronlashuv yo\'q'}</div>
          </div>
          <button class="btn btn-ghost btn-sm" data-edit="${i.id}">✏️</button>
          <button class="btn btn-danger btn-sm" data-del="${i.id}">🗑</button>
        </div>
        <div class="actions">
          <a class="btn btn-ghost btn-sm" href="/api/admin/integrations/${i.id}/export?branch_id=${state.admin.branchId}" target="_blank" style="text-decoration:none;display:inline-block">⬇️ Export JSON</a>
          <button class="btn btn-ghost btn-sm" data-logs="${i.id}">📜 Loglar</button>
        </div>
      </div>`).join('') : '<div class="empty">Hozircha integratsiya yo\'q</div>'}
    <div class="card">
      <div class="section-title">+ Yangi integratsiya</div>
      <div class="form-grid">
        <div><label class="f">Tizim</label>
          <select id="igSys"><option value="1c">1C</option><option value="1uz">1UZ</option><option value="custom">Boshqa</option></select>
        </div>
        <div><label class="f">Nomi</label><input id="igName" placeholder="Buxgalteriya 1"></div>
        <div><label class="f">Yo'nalish</label>
          <select id="igDir"><option value="export">Eksport (biz -> 1C)</option><option value="import">Import (1C -> biz)</option><option value="both">Ikkala</option></select>
        </div>
        <div><label class="f">Webhook URL (eksport uchun)</label><input id="igUrl" placeholder="https://1c.example.com/bookcafe"></div>
        <div><label class="f">Maydonlar xaritasi (JSON)</label><textarea id="igMap" rows="3" placeholder='{"DocNumber":"public_code","Amount":"total"}'></textarea></div>
        <button class="btn btn-primary btn-block" id="igAdd">Qo'shish</button>
      </div>
    </div>
    <div class="card">
      <div class="section-title">📥 Import (1C -> biz)</div>
      <div class="muted">1C tomonidan so'rov yuboriladigan manzil (token bilan):</div>
      <div class="mono" style="margin:6px 0">POST /api/integration/import?integration_id=&lt;id&gt;</div>
      <div class="mono">Header: X-Integration-Token: ****</div>
      <div class="muted" style="margin-top:6px">Hujjat namunasi:</div>
      <pre class="mono" style="white-space:pre-wrap">{
  "type": "dishes",
  "items": [
    {"code": "A-001", "title": "Osh", "price": 45000, "available": true}
  ]
}</pre>
    </div>`;
  $$('[data-back]').forEach(b => b.onclick = renderAdminHome);

  $('#igAdd').onclick = async () => {
    let mapping = {};
    try { mapping = $('#igMap').value.trim() ? JSON.parse($('#igMap').value) : {}; }
    catch (e) { showError("JSON xaritasi noto'g'ri"); return; }
    try {
      await API.post('/api/admin/integrations?branch_id=' + state.admin.branchId, {
        system: $('#igSys').value, name: $('#igName').value.trim(),
        direction: $('#igDir').value, webhook_url: $('#igUrl').value.trim(),
        mapping,
      }, { 'X-Branch-Id': state.admin.branchId });
      toast("Qo'shildi"); adminIntegrationsPage();
    } catch (e) { showError(e.message); }
  };
  $$('[data-del]').forEach(b => b.onclick = async () => {
    if (!(await confirmDialog("O'chirilsinmi?"))) return;
    try { await API.post('/api/admin/integrations/' + b.dataset.del + '/delete'); toast("O'chirildi"); adminIntegrationsPage(); }
    catch (e) { showError(e.message); }
  });
  $$('[data-logs]').forEach(b => b.onclick = async () => {
    try {
      const res = await API.get('/api/admin/integrations/' + b.dataset.logs + '/logs');
      const sheet = $('#sheet');
      sheet.innerHTML = `<div class="grabber"></div><h3>📜 Sinxronlash loglari</h3>
        ${res.logs.length ? res.logs.map(l => `
          <div class="row">
            <div class="grow">
              <div class="title" style="font-size:13px">${l.direction} · ${l.status === 'ok' ? '✅' : '❌'}</div>
              <div class="sub">${new Date(l.created_at * 1000).toLocaleString('ru-RU')}</div>
              <div class="sub mono">${esc((l.response || '').slice(0, 120))}</div>
            </div>
          </div>`).join('') : '<div class="muted">Loglar bo\'sh</div>'}`;
      openSheet();
    } catch (e) { showError(e.message); }
  });
}

/* ---------- profile ---------- */
function renderProfile() {
  const v = $('#view');
  const branch = state.branches.find(b => b.id === state.branchId);
  v.innerHTML = `
    <div class="card" style="text-align:center">
      <div style="font-size:40px">${state.role === 'admin' ? '🛡' : state.role === 'courier' ? '🛵' : '👤'}</div>
      <div class="title" style="font-size:16px">${esc(state.me ? state.me.name : '')}</div>
      <div class="sub">${esc(state.me ? 'ID: ' + state.me.id : '')} · rol: ${state.role}</div>
      ${state.role === 'admin' ? '<div class="muted" style="margin-top:6px">Siz admin — filiallar, menyu va brending sizga bo\'ysunadi 😉</div>' : ''}
    </div>
    <div class="card">
      <div class="section-title">📖 Biz haqimizda</div>
      <div class="sub">Book Cafe — kitob, qahva va mazali taomlar makoni.</div>
      ${branch ? `<div class="sub" style="margin-top:6px">📍 ${esc(branch.address)}, ${esc(branch.city)}<br>☎️ ${esc(branch.phone)}<br>🕘 ${esc(branch.work_from)}-${esc(branch.work_to)}</div>` : ''}
    </div>`;
}

/* ============================ boot ============================ */
(async function init() {
  try {
    await renderMenu();
  } catch (e) {
    $('#view').innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}<br><span class="muted">Bot tokeni va branch sozlamalarini tekshiring.</span></div>`;
  }
  if (tg && tg.BackButton) {
    tg.BackButton.onClick(() => { if (activeTab !== 'menu') switchTab('menu'); });
  }
  setInterval(() => {
    if (['orders', 'courier'].includes(activeTab) && document.visibilityState === 'visible') {
      if (activeTab === 'orders') loadOrdersTab().catch(() => {});
      else renderCourier().catch(() => {});
    }
  }, 20000);
})();
