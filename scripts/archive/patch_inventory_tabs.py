"""
Consolidate Inventory into one sidebar item with sub-tabs:
  - Count Sheet
  - Storage (assign + reorder)
  - Count History

Removes: old Storage Locations, Storage Layout, Count Sheet sidebar items
Replaces: view-inventory, view-storage, view-storagelayout, view-countsheet

Run: python3 patch_inventory_tabs.py && systemctl restart rednun
"""
import re, subprocess, shutil, sys

# Backup first
shutil.copy('static/manage.html', 'static/manage.html.bak')
print("📦 Backed up to manage.html.bak")

html = open('static/manage.html').read()

# ============================================
# STEP 1: Remove old sidebar nav items we're replacing
# ============================================

# Remove Inventory nav item
html = re.sub(
    r'      <div class="nav-item"[^>]*onclick="showView\(\'inventory\'\)"[^>]*>.*?Inventory\s*</div>\n',
    '', html, flags=re.DOTALL
)

# Remove Storage Locations nav item
html = re.sub(
    r'      <div class="nav-item"[^>]*onclick="showView\(\'storage\'\)"[^>]*>.*?Storage Locations\s*</div>\n',
    '', html, flags=re.DOTALL
)

# Remove Storage Layout nav item
html = re.sub(
    r'      <div class="nav-item"[^>]*onclick="showView\(\'storagelayout\'\)"[^>]*>.*?Storage Layout\s*</div>\n',
    '', html, flags=re.DOTALL
)

# Remove Count Sheet nav item
html = re.sub(
    r'      <div class="nav-item"[^>]*onclick="showView\(\'countsheet\'\)"[^>]*>.*?Count Sheet\s*</div>\n',
    '', html, flags=re.DOTALL
)

# Add single Inventory nav item before Recipes
recipes_nav = '      <div class="nav-item" onclick="showView(\'recipes\')">'
inv_nav = """      <div class="nav-item" onclick="showView('inv')">
        <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
        Inventory
      </div>
"""
if recipes_nav in html:
    html = html.replace(recipes_nav, inv_nav + recipes_nav)
print("✅ Sidebar nav updated")

# ============================================
# STEP 2: Fix mobile nav
# ============================================
# Remove old mobile nav items
for old in ['Inventory', 'Storage', 'Layout', 'Count']:
    html = re.sub(
        r'      <div class="mobile-nav-item"[^>]*onclick="showView\(\'' + 
        {'Inventory':'inventory','Storage':'storage','Layout':'storagelayout','Count':'countsheet'}[old] + 
        r'\'\)"[^>]*>' + old + r'</div>\n',
        '', html
    )

# Add single mobile nav item
old_recipes_mobile = '<div class="mobile-nav-item" onclick="showView(\'recipes\')">Recipes</div>'
new_inv_mobile = '<div class="mobile-nav-item" onclick="showView(\'inv\')">Inventory</div>\n      '
if old_recipes_mobile in html:
    html = html.replace(old_recipes_mobile, new_inv_mobile + old_recipes_mobile)
print("✅ Mobile nav updated")

# ============================================
# STEP 3: Remove old view divs
# ============================================
# Remove view-inventory
html = re.sub(r'      <!-- Inventory View -->.*?</div>\s*</div>\s*</div>\n', '', html, flags=re.DOTALL, count=1)
# Remove view-storage (Storage Locations)
html = re.sub(r'      <!-- Storage Locations View -->.*?</div>\s*</div>\s*</div>\n', '', html, flags=re.DOTALL, count=1)
# Remove view-storagelayout
html = re.sub(r'      <!-- Storage Layout View -->.*?</div>\s*</div>\n', '', html, flags=re.DOTALL, count=1)
# Remove view-countsheet
html = re.sub(r'      <!-- Count Sheet View -->.*?</div>\s*</div>\n', '', html, flags=re.DOTALL, count=1)

# ============================================
# STEP 4: Add new consolidated Inventory view before Recipes
# ============================================
NEW_VIEW = '''      <!-- Inventory View (Consolidated) -->
      <div class="view" id="view-inv">
        <!-- Sub-tabs -->
        <div style="display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:12px">
          <button class="btn inv-tab active" data-tab="countsheet" onclick="invTab(this)">Count Sheet</button>
          <button class="btn inv-tab" data-tab="storagesetup" onclick="invTab(this)">Storage</button>
          <button class="btn inv-tab" data-tab="counthistory" onclick="invTab(this)">Count History</button>
        </div>

        <!-- COUNT SHEET TAB -->
        <div class="inv-panel active" id="panel-countsheet">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <div style="font-size:18px;font-weight:700">Enter Count</div>
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
              <div>Select a location to load count sheet.<br>Products are organized by storage area in shelf order.</div>
            </div>
          </div>
        </div>

        <!-- STORAGE TAB -->
        <div class="inv-panel" id="panel-storagesetup">
          <div style="display:flex;gap:16px;height:calc(100vh - 240px);min-height:400px">
            <div style="width:250px;background:var(--card-bg);border:1px solid var(--border);border-radius:12px;overflow-y:auto;flex-shrink:0">
              <div style="padding:10px 14px;border-bottom:1px solid var(--border)">
                <div style="display:flex;gap:6px;margin-bottom:8px">
                  <button class="btn btn-secondary sl-rest-btn" data-loc="dennis" onclick="slPickRest(this)" style="flex:1;padding:6px;font-size:11px;border-color:var(--red);background:rgba(255,69,58,0.1);color:var(--red)">Dennis</button>
                  <button class="btn btn-secondary sl-rest-btn" data-loc="chatham" onclick="slPickRest(this)" style="flex:1;padding:6px;font-size:11px">Chatham</button>
                </div>
                <div style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Storage Areas</div>
              </div>
              <div id="sl-loc-list"></div>
            </div>
            <div style="flex:1;overflow-y:auto">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div>
                  <div style="font-size:16px;font-weight:700" id="sl-main-title">Select a storage area</div>
                  <div style="font-size:12px;color:var(--text2)" id="sl-main-sub">Click a location on the left</div>
                </div>
                <input class="search-bar" id="sl-search" placeholder="Search..." oninput="slRenderMain()" style="width:200px">
              </div>
              <div id="sl-main-content"></div>
            </div>
          </div>
        </div>

        <!-- COUNT HISTORY TAB -->
        <div class="inv-panel" id="panel-counthistory">
          <div style="font-size:18px;font-weight:700;margin-bottom:16px">Count History</div>
          <div id="cs-history">
            <div style="padding:20px;text-align:center;color:var(--text3)">Loading...</div>
          </div>
        </div>
      </div>

'''

old_recipes_view = '      <!-- Recipes View -->'
if old_recipes_view in html:
    html = html.replace(old_recipes_view, NEW_VIEW + old_recipes_view)
print("✅ Added consolidated inventory view")

# ============================================
# STEP 5: Add CSS for sub-tabs
# ============================================
EXTRA_CSS = """
/* Inventory sub-tabs */
.inv-tab{background:var(--card-bg);color:var(--text2);border:1px solid var(--border);padding:8px 16px;font-size:13px}
.inv-tab.active{background:rgba(255,69,58,0.1);border-color:rgba(255,69,58,0.3);color:var(--red)}
.inv-panel{display:none}
.inv-panel.active{display:block}
"""
if '.inv-tab' not in html:
    html = html.replace('</style>', EXTRA_CSS + '</style>')
print("✅ Added CSS")

# ============================================
# STEP 6: Update showView function
# ============================================
# Add 'inv' to titles
if "'inv'" not in html.split('const titles')[1].split('}')[0] if 'const titles' in html else '':
    html = html.replace(
        "    recipes: 'Recipes'",
        "    inv: 'Inventory',\n    recipes: 'Recipes'"
    )

# Add inv case to switch  
if "case 'inv'" not in html:
    html = html.replace(
        "    case 'recipes': loadRecipes(); break;",
        "    case 'inv': invInit(); break;\n    case 'recipes': loadRecipes(); break;"
    )

# Remove old cases that no longer have views
for old_case in ['inventory', 'storage', 'storagelayout', 'countsheet']:
    html = re.sub(r"    case '" + old_case + r"':.*?break;\n", '', html)

print("✅ Updated showView")

# ============================================
# STEP 7: Add/update JavaScript
# ============================================
# Remove old SL and CS code blocks if they exist, we'll replace
# Find and remove old slInit through end of CS code
# Actually safer to just add the tab switcher and init, keep existing SL/CS functions

NEW_JS = """
// ============================================
// INVENTORY TAB SWITCHER
// ============================================
function invTab(btn) {
  document.querySelectorAll('.inv-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.inv-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  var panel = document.getElementById('panel-' + btn.dataset.tab);
  if (panel) panel.classList.add('active');
  if (btn.dataset.tab === 'storagesetup') slInit();
  if (btn.dataset.tab === 'counthistory') csLoadHistory();
}

function invInit() {
  // Default to count sheet tab
  csLoadHistory();
}
"""

if 'function invTab' not in html:
    html = html.replace('</script>\n</body>', NEW_JS + '</script>\n</body>')
print("✅ Added tab JS")

# ============================================
# VALIDATE JS BEFORE SAVING
# ============================================
js = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[0]
open('/tmp/check.js', 'w').write(js)
result = subprocess.run(['node', '--check', '/tmp/check.js'], capture_output=True, text=True)
if result.returncode != 0:
    print("\n❌ JS VALIDATION FAILED:")
    print(result.stderr)
    print("\n⚠️  NOT saving changes. Restoring backup.")
    shutil.copy('static/manage.html.bak', 'static/manage.html')
    sys.exit(1)
else:
    print("✅ JS validation passed")

# Save
open('static/manage.html', 'w').write(html)
print("\n🎉 Done! Restart: systemctl restart rednun")
