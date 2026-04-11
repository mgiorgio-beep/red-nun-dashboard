"""
Patch manage.html to add Storage Layout and Count Sheet views
as tabs within the existing management interface.

Run: python3 patch_manage_views.py && systemctl restart rednun
"""

import re

with open('static/manage.html', 'r') as f:
    html = f.read()

# ============================================
# 1. Add nav items to sidebar (after Storage Locations)
# ============================================
STORAGE_LAYOUT_NAV = '''      <div class="nav-item" onclick="showView('storagelayout')">
        <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm0 8a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zm10 0a1 1 0 011-1h4a1 1 0 011 1v6a1 1 0 01-1 1h-4a1 1 0 01-1-1v-6z"/></svg>
        Storage Layout
      </div>
      <div class="nav-item" onclick="showView('countsheet')">
        <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/></svg>
        Count Sheet
      </div>'''

# Insert after Storage Locations nav item
old_recipes_nav = '''      <div class="nav-item" onclick="showView('recipes')">
        <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" d="M12 6.253v13'''

if 'storagelayout' not in html:
    html = html.replace(old_recipes_nav, STORAGE_LAYOUT_NAV + '\n' + old_recipes_nav)
    print("✅ Added sidebar nav items")
else:
    print("⏭️  Sidebar nav items already exist")

# ============================================
# 2. Add mobile nav items
# ============================================
OLD_MOBILE_STORAGE = '''<div class="mobile-nav-item" onclick="showView('storage')">Storage</div>'''
NEW_MOBILE = '''<div class="mobile-nav-item" onclick="showView('storage')">Storage</div>
      <div class="mobile-nav-item" onclick="showView('storagelayout')">Layout</div>
      <div class="mobile-nav-item" onclick="showView('countsheet')">Count</div>'''

if 'storagelayout' not in html.split('mobile-nav')[1] if 'mobile-nav' in html else '':
    html = html.replace(OLD_MOBILE_STORAGE, NEW_MOBILE)
    print("✅ Added mobile nav items")
else:
    print("⏭️  Mobile nav items already exist")

# ============================================
# 3. Add view divs (before Recipes View)
# ============================================
STORAGE_LAYOUT_VIEW = '''
      <!-- Storage Layout View -->
      <div class="view" id="view-storagelayout">
        <div style="display:flex;gap:16px;height:calc(100vh - 180px);min-height:500px">
          <!-- Left: Location list -->
          <div style="width:260px;background:var(--card-bg);border:1px solid var(--border);border-radius:12px;overflow-y:auto;flex-shrink:0">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
              <div style="display:flex;gap:6px;margin-bottom:10px">
                <button class="btn btn-secondary sl-rest-btn" data-loc="dennis" onclick="slPickRest(this)" style="flex:1;padding:7px;font-size:12px;border-color:var(--red);background:rgba(255,69,58,0.1);color:var(--red)">Dennis</button>
                <button class="btn btn-secondary sl-rest-btn" data-loc="chatham" onclick="slPickRest(this)" style="flex:1;padding:7px;font-size:12px">Chatham</button>
              </div>
              <div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.6px">Storage Areas</div>
            </div>
            <div id="sl-loc-list"></div>
          </div>
          <!-- Right: Products -->
          <div style="flex:1;overflow-y:auto">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <div>
                <div style="font-size:18px;font-weight:700" id="sl-main-title">Select a storage area</div>
                <div style="font-size:12px;color:var(--text2)" id="sl-main-sub">Click a location on the left</div>
              </div>
              <input class="search-bar" id="sl-search" placeholder="Search..." oninput="slRenderMain()" style="width:220px">
            </div>
            <div id="sl-main-content"></div>
          </div>
        </div>
      </div>

      <!-- Count Sheet View -->
      <div class="view" id="view-countsheet">
        <div class="card">
          <div class="card-header">
            <div class="card-title">Inventory Count</div>
            <div style="display:flex;gap:10px;align-items:center">
              <select class="form-select" id="cs-location" onchange="csLoadSheet()" style="max-width:160px">
                <option value="">Select Location...</option>
                <option value="dennis">Dennis Port</option>
                <option value="chatham">Chatham</option>
              </select>
              <button class="btn btn-primary" id="cs-save-btn" onclick="csSaveCount()" disabled>Save Count</button>
            </div>
          </div>
          <div id="cs-stats" style="display:flex;gap:12px;margin-bottom:16px"></div>
          <div id="cs-content">
            <div style="padding:40px;text-align:center;color:var(--text3)">
              <div style="font-size:36px;margin-bottom:8px;opacity:0.4">📋</div>
              <div>Select a location to start counting</div>
            </div>
          </div>
        </div>
        <!-- Count History -->
        <div class="card" style="margin-top:16px">
          <div class="card-header"><div class="card-title">Recent Counts</div></div>
          <div id="cs-history"></div>
        </div>
      </div>
'''

OLD_RECIPES_VIEW = '      <!-- Recipes View -->'
if 'view-storagelayout' not in html:
    html = html.replace(OLD_RECIPES_VIEW, STORAGE_LAYOUT_VIEW + '\n' + OLD_RECIPES_VIEW)
    print("✅ Added view divs")
else:
    print("⏭️  View divs already exist")

# ============================================
# 4. Add to showView titles + switch cases
# ============================================
OLD_TITLES = """    storagelayout: 'Storage Layout',
    countsheet: 'Count Sheet',"""

# Check if already added
if 'storagelayout' not in html.split('const titles')[1].split('}')[0] if 'const titles' in html else '':
    html = html.replace(
        "    storage: 'Storage Locations',\n    recipes: 'Recipes'",
        "    storage: 'Storage Locations',\n    storagelayout: 'Storage Layout',\n    countsheet: 'Count Sheet',\n    recipes: 'Recipes'"
    )
    print("✅ Added view titles")

    # Add switch cases
    html = html.replace(
        "    case 'storage': loadStorageLocations(); break;\n    case 'recipes': loadRecipes(); break;",
        "    case 'storage': loadStorageLocations(); break;\n    case 'storagelayout': slInit(); break;\n    case 'countsheet': csInit(); break;\n    case 'recipes': loadRecipes(); break;"
    )
    print("✅ Added switch cases")
else:
    print("⏭️  Titles/switch already added")

# ============================================
# 5. Add CSS for storage layout and count sheet
# ============================================
EXTRA_CSS = '''
/* Storage Layout */
.sl-loc-item{padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:all 0.15s}
.sl-loc-item:hover{background:rgba(255,255,255,0.03)}
.sl-loc-item.active{background:rgba(10,132,255,0.1);border-left:3px solid var(--blue)}
.sl-loc-item.unassigned{background:rgba(255,69,58,0.06)}
.sl-loc-item.unassigned.active{background:rgba(255,69,58,0.12);border-left-color:var(--red)}
.sl-count{font-size:11px;color:var(--text3);font-weight:600;background:var(--card-bg);padding:2px 8px;border-radius:8px}
.sl-count.warn{background:rgba(255,69,58,0.15);color:var(--red)}
.sl-badge{display:inline-block;background:var(--red);color:#fff;font-size:10px;font-weight:800;padding:1px 6px;border-radius:6px;margin-left:4px}
.sl-product{display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;margin-bottom:4px;cursor:grab;user-select:none}
.sl-product:active{cursor:grabbing}
.sl-product.dragging{opacity:0.4;border-color:var(--blue)}
.sl-product.drag-over{border-color:var(--green);background:rgba(48,209,88,0.05)}
.sl-order{font-size:10px;color:var(--text3);font-weight:700;width:20px;text-align:center;flex-shrink:0}
.sl-handle{color:var(--text3);font-size:14px;flex-shrink:0}
.sl-name{font-size:13px;font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sl-remove{background:none;border:none;color:var(--text3);cursor:pointer;font-size:16px;padding:2px 6px;border-radius:4px;flex-shrink:0}
.sl-remove:hover{color:var(--red);background:rgba(255,69,58,0.1)}
.sl-unassigned{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;margin-bottom:4px}
.sl-assign-btn{padding:5px 10px;border-radius:6px;background:rgba(10,132,255,0.1);color:var(--blue);font-size:11px;font-weight:700;border:1px solid rgba(10,132,255,0.25);cursor:pointer;flex-shrink:0}
.sl-assign-btn:hover{background:rgba(10,132,255,0.2)}
/* Count Sheet */
.cs-section{margin-bottom:20px}
.cs-section-hdr{padding:10px 14px;background:rgba(10,132,255,0.08);border:1px solid rgba(10,132,255,0.15);border-radius:8px 8px 0 0;display:flex;justify-content:space-between;align-items:center;font-size:14px;font-weight:700;color:var(--blue)}
.cs-section-count{font-size:11px;font-weight:600;color:var(--text3)}
.cs-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);border-left:1px solid var(--border);border-right:1px solid var(--border)}
.cs-row:last-child{border-radius:0 0 8px 8px}
.cs-name{flex:1;min-width:0;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cs-prev{font-size:10px;color:var(--text3)}
.cs-input{width:70px;padding:8px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;color:var(--text);font-size:15px;font-weight:700;text-align:center;outline:none;-moz-appearance:textfield}
.cs-input::-webkit-inner-spin-button,.cs-input::-webkit-outer-spin-button{-webkit-appearance:none}
.cs-input:focus{border-color:var(--blue)}
.cs-input.has-val{border-color:var(--green);background:rgba(48,209,88,0.06)}
.cs-unit{font-size:10px;color:var(--text3);width:70px;text-align:center}
.cs-stat{background:var(--card-bg);border:1px solid var(--border);border-radius:8px;padding:10px 16px;flex-shrink:0}
.cs-stat-label{font-size:10px;color:var(--text3);font-weight:600;text-transform:uppercase}
.cs-stat-val{font-size:18px;font-weight:700;margin-top:2px}
'''

if '.sl-loc-item' not in html:
    # Insert before </style>
    html = html.replace('</style>', EXTRA_CSS + '</style>')
    print("✅ Added CSS")
else:
    print("⏭️  CSS already added")

# ============================================
# 6. Add JavaScript for both views
# ============================================
EXTRA_JS = '''
// ============================================
// STORAGE LAYOUT
// ============================================
const SL = { restaurant: 'dennis', locations: [], selectedLoc: null, unassignedCount: 0 };

function slInit() { slLoadLocations(); }

function slPickRest(el) {
  document.querySelectorAll('.sl-rest-btn').forEach(b => { b.style.borderColor='var(--border)'; b.style.background='var(--card-bg)'; b.style.color='var(--text2)'; });
  el.style.borderColor='var(--red)'; el.style.background='rgba(255,69,58,0.1)'; el.style.color='var(--red)';
  SL.restaurant = el.dataset.loc;
  SL.selectedLoc = null;
  slLoadLocations();
}

async function slLoadLocations() {
  try {
    const [locsRes, unRes] = await Promise.all([
      fetch('/api/storage/locations'),
      fetch('/api/storage/unassigned?location=' + SL.restaurant)
    ]);
    const allLocs = await locsRes.json();
    const unassigned = await unRes.json();
    SL.locations = allLocs.filter(l => l.location === SL.restaurant);
    SL.unassignedCount = unassigned.length;

    const counts = await Promise.all(SL.locations.map(l => fetch('/api/storage/locations/' + l.id + '/products').then(r => r.json())));
    const list = document.getElementById('sl-loc-list');

    let h = '<div class="sl-loc-item unassigned' + (SL.selectedLoc === 'unassigned' ? ' active' : '') + '" onclick="slSelectUnassigned()">' +
      '<div style="font-size:13px;font-weight:600">⚠️ Unassigned' + (SL.unassignedCount > 0 ? '<span class="sl-badge">' + SL.unassignedCount + '</span>' : '') + '</div>' +
      '<div class="sl-count' + (SL.unassignedCount > 0 ? ' warn' : '') + '">' + SL.unassignedCount + '</div></div>';

    SL.locations.forEach((loc, i) => {
      h += '<div class="sl-loc-item' + (SL.selectedLoc === loc.id ? ' active' : '') + '" onclick="slSelectLoc(' + loc.id + ',\'' + loc.name.replace(/'/g,"\\'") + '\')">' +
        '<div style="font-size:13px;font-weight:600">' + loc.name + '</div>' +
        '<div class="sl-count">' + counts[i].length + '</div></div>';
    });
    list.innerHTML = h;

    if (!SL.selectedLoc && SL.unassignedCount > 0) slSelectUnassigned();
    else if (SL.selectedLoc === 'unassigned') slSelectUnassigned();
    else if (SL.selectedLoc) { const loc = SL.locations.find(l => l.id === SL.selectedLoc); if (loc) slSelectLoc(loc.id, loc.name); }
  } catch(e) { console.error('slLoadLocations error:', e); }
}

async function slSelectLoc(locId, locName) {
  SL.selectedLoc = locId;
  document.getElementById('sl-main-title').textContent = locName;
  document.getElementById('sl-main-sub').textContent = 'Drag to reorder • Click ✕ to remove';
  document.querySelectorAll('.sl-loc-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.sl-loc-item').forEach(el => { if (el.onclick && el.onclick.toString().includes(locId)) el.classList.add('active'); });
  const res = await fetch('/api/storage/locations/' + locId + '/products');
  const products = await res.json();
  const search = (document.getElementById('sl-search').value || '').toLowerCase();
  let filtered = search ? products.filter(p => p.name.toLowerCase().includes(search)) : products;
  const main = document.getElementById('sl-main-content');
  if (!filtered.length) { main.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)"><div style="font-size:36px;opacity:0.4;margin-bottom:8px">📦</div>No products here yet. Select Unassigned to add.</div>'; return; }
  main.innerHTML = filtered.map((p, i) => {
    const tc = 'tag-' + (p.category||'other').toLowerCase();
    return '<div class="sl-product" draggable="true" data-pid="' + p.id + '">' +
      '<div class="sl-order">' + (i+1) + '</div><div class="sl-handle">⠿</div>' +
      '<div class="sl-name">' + p.name + '</div>' +
      '<span class="pc-tag ' + tc + '" style="font-size:9px;padding:2px 6px;border-radius:4px">' + slCatLabel(p.category) + '</span>' +
      '<button class="sl-remove" onclick="slRemove(' + locId + ',' + p.id + ')">✕</button></div>';
  }).join('');
  slSetupDrag(locId);
}

async function slSelectUnassigned() {
  SL.selectedLoc = 'unassigned';
  document.getElementById('sl-main-title').textContent = '⚠️ Unassigned Products';
  document.getElementById('sl-main-sub').textContent = 'Select products then click a storage area button to assign';
  document.querySelectorAll('.sl-loc-item').forEach(el => el.classList.remove('active'));
  document.querySelector('.sl-loc-item.unassigned')?.classList.add('active');
  const res = await fetch('/api/storage/unassigned?location=' + SL.restaurant);
  const products = await res.json();
  const main = document.getElementById('sl-main-content');
  if (!products.length) { main.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)"><div style="font-size:36px;opacity:0.4;margin-bottom:8px">✅</div>All products assigned!</div>'; return; }
  const locBtns = SL.locations.map(l => '<button class="sl-assign-btn" onclick="slBatchAssign(' + l.id + ',this)">' + l.name + '</button>').join(' ');
  let h = '<div style="padding:12px;background:var(--card-bg);border:1px solid rgba(255,69,58,0.2);border-radius:8px;margin-bottom:12px">' +
    '<div style="font-size:12px;font-weight:700;color:var(--red);margin-bottom:8px">Select products, then click a storage area:</div>' +
    '<div style="display:flex;flex-wrap:wrap;gap:4px">' + locBtns + '</div>' +
    '<label style="display:block;margin-top:8px;font-size:12px;color:var(--text2);cursor:pointer"><input type="checkbox" id="sl-select-all" onchange="document.querySelectorAll(\'.sl-check\').forEach(c=>c.checked=this.checked)" style="margin-right:6px">Select All</label></div>';
  const cats = {};
  products.forEach(p => { const c = p.category||'OTHER'; if (!cats[c]) cats[c]=[]; cats[c].push(p); });
  for (const [cat, prods] of Object.entries(cats).sort()) {
    h += '<div style="font-size:11px;font-weight:700;color:var(--text3);padding:10px 0 4px;text-transform:uppercase">' + slCatLabel(cat) + ' (' + prods.length + ')</div>';
    h += prods.map(p => '<div class="sl-unassigned"><input type="checkbox" class="sl-check" value="' + p.id + '">' +
      '<div style="flex:1;font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + p.name + '</div>' +
      '<span class="pc-tag tag-' + (cat||'other').toLowerCase() + '" style="font-size:9px;padding:2px 6px;border-radius:4px">' + slCatLabel(cat) + '</span></div>').join('');
  }
  main.innerHTML = h;
}

async function slBatchAssign(locId, btn) {
  const ids = [...document.querySelectorAll('.sl-check:checked')].map(c => parseInt(c.value));
  if (!ids.length) { alert('Select products first'); return; }
  await fetch('/api/storage/locations/' + locId + '/products/batch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_ids:ids}) });
  slLoadLocations();
}

async function slRemove(locId, pid) {
  await fetch('/api/storage/locations/' + locId + '/products/' + pid, { method:'DELETE' });
  slSelectLoc(locId, document.getElementById('sl-main-title').textContent);
  slLoadLocations();
}

function slSetupDrag(locId) {
  let dragEl = null;
  document.querySelectorAll('.sl-product').forEach(card => {
    card.addEventListener('dragstart', () => { dragEl = card; card.classList.add('dragging'); });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      document.querySelectorAll('.sl-product').forEach(c => c.classList.remove('drag-over'));
      const order = [...document.querySelectorAll('.sl-product')].map(c => parseInt(c.dataset.pid));
      fetch('/api/storage/locations/' + locId + '/reorder', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_ids:order}) });
      document.querySelectorAll('.sl-product .sl-order').forEach((el, i) => el.textContent = i + 1);
    });
    card.addEventListener('dragover', e => {
      e.preventDefault();
      if (card !== dragEl) {
        card.classList.add('drag-over');
        const rect = card.getBoundingClientRect();
        if (e.clientY < rect.top + rect.height/2) card.parentNode.insertBefore(dragEl, card);
        else card.parentNode.insertBefore(dragEl, card.nextSibling);
      }
    });
    card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
  });
}

function slRenderMain() { if (SL.selectedLoc === 'unassigned') slSelectUnassigned(); else if (SL.selectedLoc) slSelectLoc(SL.selectedLoc, document.getElementById('sl-main-title').textContent); }
function slCatLabel(c) { return {BEER:'Beer',LIQUOR:'Liquor',WINE:'Wine',FOOD:'Food',NA_BEVERAGES:'NA Bev',OTHER:'Other',SUPPLIES:'Supplies'}[c]||c; }

// ============================================
// COUNT SHEET
// ============================================
const CS = { location: null, sections: [], counts: {}, countId: null, total: 0 };

function csInit() { csLoadHistory(); }

async function csLoadSheet() {
  CS.location = document.getElementById('cs-location').value;
  if (!CS.location) return;

  const res = await fetch('/api/storage/count-sheet?location=' + CS.location);
  CS.sections = await res.json();
  CS.counts = {};
  CS.total = CS.sections.reduce((s, sec) => s + sec.products.length, 0);

  if (!CS.sections.length) {
    document.getElementById('cs-content').innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)"><div style="font-size:36px;opacity:0.4;margin-bottom:8px">📦</div>No products assigned to storage areas yet.<br>Go to Storage Layout to set up.</div>';
    document.getElementById('cs-save-btn').disabled = true;
    return;
  }

  // Create count session
  try {
    const r = await fetch('/api/inventory/counts', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({location:CS.location, notes:'Full count'}) });
    CS.countId = (await r.json()).id;
  } catch(e) {}

  document.getElementById('cs-save-btn').disabled = false;
  csRender();
  csUpdateStats();
}

function csRender() {
  const el = document.getElementById('cs-content');
  const icons = n => { n = n.toLowerCase(); if (n.includes('walk-in')||n.includes('cooler')) return '❄️'; if (n.includes('freezer')) return '🧊'; if (n.includes('bar')) return '🍸'; if (n.includes('dry')) return '📦'; return '📍'; };

  el.innerHTML = CS.sections.map((sec, si) => {
    const counted = sec.products.filter(p => CS.counts[p.id] !== undefined).length;
    return '<div class="cs-section">' +
      '<div class="cs-section-hdr">' + icons(sec.location_name) + ' ' + sec.location_name +
      '<span class="cs-section-count" id="cs-sec-' + si + '">' + counted + '/' + sec.products.length + '</span></div>' +
      sec.products.map(p => {
        const val = CS.counts[p.id] !== undefined ? CS.counts[p.id] : '';
        const prev = p.current_qty;
        return '<div class="cs-row">' +
          '<div class="cs-name">' + p.name + '</div>' +
          (prev !== null && prev !== undefined ? '<div class="cs-prev">Last: ' + prev + '</div>' : '') +
          '<div style="text-align:center"><input class="cs-input' + (val !== '' ? ' has-val' : '') + '" type="number" inputmode="decimal" step="0.5" placeholder="—" value="' + val + '" ' +
            'onfocus="this.select()" oninput="csSetQty(' + p.id + ',' + si + ',this)">' +
          '<div class="cs-unit">' + (p.inventory_unit || p.unit || 'ea') + '</div></div></div>';
      }).join('') + '</div>';
  }).join('');
}

function csSetQty(pid, si, el) {
  if (el.value.trim() === '') { delete CS.counts[pid]; el.classList.remove('has-val'); }
  else { CS.counts[pid] = parseFloat(el.value); el.classList.add('has-val'); }
  csUpdateStats();
  const sec = CS.sections[si];
  const cel = document.getElementById('cs-sec-' + si);
  if (sec && cel) cel.textContent = sec.products.filter(p => CS.counts[p.id] !== undefined).length + '/' + sec.products.length;
}

function csUpdateStats() {
  const counted = Object.keys(CS.counts).length;
  const pct = CS.total > 0 ? Math.round(counted / CS.total * 100) : 0;
  let val = 0;
  for (const [pid, qty] of Object.entries(CS.counts)) {
    for (const sec of CS.sections) { const p = sec.products.find(x => x.id == pid); if (p && p.current_price) { val += qty * p.current_price; break; } }
  }
  document.getElementById('cs-stats').innerHTML =
    '<div class="cs-stat"><div class="cs-stat-label">Progress</div><div class="cs-stat-val">' + pct + '%</div></div>' +
    '<div class="cs-stat"><div class="cs-stat-label">Counted</div><div class="cs-stat-val">' + counted + '/' + CS.total + '</div></div>' +
    '<div class="cs-stat"><div class="cs-stat-label">Est. Value</div><div class="cs-stat-val">$' + val.toLocaleString(undefined,{maximumFractionDigits:0}) + '</div></div>';
}

async function csSaveCount() {
  const counted = Object.keys(CS.counts).length;
  if (!counted) { alert('No items counted'); return; }
  const btn = document.getElementById('cs-save-btn');
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    const items = Object.entries(CS.counts).map(([pid, qty]) => {
      let unit = 'ea';
      for (const sec of CS.sections) { const p = sec.products.find(x => x.id == pid); if (p) { unit = p.inventory_unit || p.unit || 'ea'; break; } }
      return { product_id: parseInt(pid), quantity: qty, unit };
    });
    const res = await fetch('/api/inventory/counts/batch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({count_id:CS.countId, location:CS.location, items}) });
    if (!res.ok) throw new Error();
    alert('✓ ' + counted + ' items saved!');
    CS.counts = {};
    document.getElementById('cs-location').value = '';
    document.getElementById('cs-content').innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)"><div style="font-size:36px;opacity:0.4;margin-bottom:8px">✅</div>Count saved!</div>';
    document.getElementById('cs-stats').innerHTML = '';
    btn.textContent = 'Save Count';
    csLoadHistory();
  } catch(e) { alert('Error saving'); btn.disabled = false; btn.textContent = 'Save Count'; }
}

async function csLoadHistory() {
  try {
    const data = await fetch('/api/inventory/counts/history').then(r => r.json());
    const el = document.getElementById('cs-history');
    if (!data.length) { el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3)">No counts yet</div>'; return; }
    el.innerHTML = '<table class="data-table"><thead><tr><th>Date</th><th>Location</th><th>Items</th><th>Status</th></tr></thead><tbody>' +
      data.slice(0,10).map(c => {
        const d = new Date(c.created_at);
        return '<tr><td>' + d.toLocaleDateString('en-US',{month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}) + '</td>' +
          '<td>' + (c.location==='dennis'?'Dennis':'Chatham') + '</td>' +
          '<td>' + (c.item_count||0) + '</td>' +
          '<td><span style="color:var(--green);font-weight:600">Complete</span></td></tr>';
      }).join('') + '</tbody></table>';
  } catch(e) {}
}
'''

if 'function slInit' not in html:
    # Insert before closing </script>
    html = html.replace('</script>\n</body>', EXTRA_JS + '\n</script>\n</body>')
    print("✅ Added JavaScript")
else:
    print("⏭️  JavaScript already added")

# Write
with open('static/manage.html', 'w') as f:
    f.write(html)

print("\n🎉 manage.html patched!")
print("   Restart: systemctl restart rednun")
print("   Then go to /manage → Storage Layout or Count Sheet in sidebar")
