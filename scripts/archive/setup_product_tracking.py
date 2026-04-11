"""
Product Setup Tracking System
1. Adds setup_complete column to products
2. Auto-assigns products to default storage locations by category
3. Patches Products view to show setup status

Run: python3 setup_product_tracking.py && systemctl restart rednun
"""
import sqlite3, re, subprocess, shutil, os

DB_PATH = os.getenv("DB_PATH", "toast_data.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

# ============================================
# STEP 1: Add setup_complete column
# ============================================
conn = get_connection()

# Check if column exists
cols = [c[1] for c in conn.execute("PRAGMA table_info(products)").fetchall()]
if 'setup_complete' not in cols:
    conn.execute("ALTER TABLE products ADD COLUMN setup_complete INTEGER DEFAULT 0")
    conn.commit()
    print("✅ Added setup_complete column")
else:
    print("⏭️  setup_complete column exists")

# ============================================
# STEP 2: Auto-assign products to storage locations
# ============================================
assigned = conn.execute("SELECT COUNT(DISTINCT product_id) FROM product_storage_locations").fetchone()[0]
if assigned == 0:
    print("\n⏳ Auto-assigning products to storage locations...")
    
    # Get storage locations for each restaurant
    locations = {}
    for loc in conn.execute("SELECT id, name, location FROM storage_locations").fetchall():
        key = (loc['location'], loc['name'])
        locations[key] = loc['id']
    
    # Category -> default storage location name mapping
    cat_map = {
        'BEER': 'Walk-in Cooler',
        'WINE': 'Bar',
        'LIQUOR': 'Bar',
        'FOOD': 'Walk-in Cooler',
        'NA_BEVERAGES': 'Walk-in Cooler',
        'OTHER': 'Dry Storage',
        'SUPPLIES': 'Dry Storage',
    }
    
    products = conn.execute("SELECT id, name, category, location FROM products WHERE active=1").fetchall()
    count = 0
    
    for p in products:
        cat = p['category'] or 'OTHER'
        loc_name = cat_map.get(cat, 'Dry Storage')
        
        # Try to assign to the product's own location first, fall back to dennis
        restaurant = p['location'] if p['location'] in ('dennis', 'chatham') else 'dennis'
        loc_id = locations.get((restaurant, loc_name))
        
        if not loc_id:
            # Fallback to dennis
            loc_id = locations.get(('dennis', loc_name))
        
        if loc_id:
            try:
                conn.execute("""
                    INSERT INTO product_storage_locations (product_id, storage_location_id, sort_order)
                    VALUES (?, ?, ?)
                """, (p['id'], loc_id, count))
                count += 1
            except:
                pass
    
    conn.commit()
    print(f"✅ Auto-assigned {count} products to storage locations")
    
    # Show summary
    for loc in conn.execute("""
        SELECT sl.name, sl.location, COUNT(*) as cnt
        FROM product_storage_locations psl
        JOIN storage_locations sl ON psl.storage_location_id = sl.id
        GROUP BY sl.id
        ORDER BY sl.location, sl.name
    """).fetchall():
        print(f"   {loc['location']}: {loc['name']} = {loc['cnt']} products")
else:
    print(f"⏭️  {assigned} products already assigned")

conn.close()

# ============================================
# STEP 3: Add API endpoint for setup status
# ============================================
with open('inventory_routes.py', 'r') as f:
    inv_code = f.read()

SETUP_ENDPOINT = '''

@inventory_bp.route('/products/<int:product_id>/setup', methods=['POST'])
def update_setup_status(product_id):
    """Mark a product's setup as complete or incomplete"""
    data = request.json
    conn = get_connection()
    conn.execute("UPDATE products SET setup_complete = ? WHERE id = ?", 
                 (1 if data.get('complete') else 0, product_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Updated', 'setup_complete': data.get('complete')})


@inventory_bp.route('/products/setup-status', methods=['GET'])
def get_setup_status():
    """Get setup status for all products"""
    conn = get_connection()
    products = conn.execute("""
        SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.setup_complete,
               COUNT(psl.id) as storage_count
        FROM products p
        LEFT JOIN product_storage_locations psl ON psl.product_id = p.id
        WHERE p.active = 1
        GROUP BY p.id
        ORDER BY p.setup_complete ASC, p.category, p.name
    """).fetchall()
    conn.close()
    
    result = []
    for p in products:
        needs = []
        if not p['unit'] or p['unit'] in ('', 'Other'):
            needs.append('purchase_unit')
        if not p['inventory_unit'] or p['inventory_unit'] in ('', 'each'):
            needs.append('inventory_unit')
        if p['storage_count'] == 0:
            needs.append('storage')
        
        result.append({
            **dict(p),
            'needs_setup': needs,
            'is_ready': len(needs) == 0 and p['setup_complete'] == 1
        })
    
    return jsonify(result)
'''

if '/products/setup-status' not in inv_code:
    inv_code += SETUP_ENDPOINT
    with open('inventory_routes.py', 'w') as f:
        f.write(inv_code)
    print("✅ Added setup API endpoints")
else:
    print("⏭️  Setup endpoints exist")

# ============================================
# STEP 4: Patch manage.html Products view
# ============================================
shutil.copy('static/manage.html', 'static/manage.html.bak2')

html = open('static/manage.html').read()

# Add CSS for setup badges
SETUP_CSS = """
/* Product setup tracking */
.setup-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:700;text-transform:uppercase}
.setup-new{background:rgba(255,159,10,0.15);color:var(--orange)}
.setup-ready{background:rgba(48,209,88,0.15);color:var(--green)}
.setup-check{font-size:11px;margin-right:2px}
.setup-item{font-size:11px;color:var(--text3);display:inline-flex;align-items:center;gap:2px}
.setup-item.done{color:var(--green)}
.setup-item.missing{color:var(--orange)}
.setup-star{cursor:pointer;font-size:16px;background:none;border:none;padding:2px;transition:all 0.15s}
.sort-btn{padding:4px 10px;border-radius:6px;background:var(--card-bg);border:1px solid var(--border);color:var(--text2);font-size:11px;font-weight:600;cursor:pointer}
.sort-btn.active{background:rgba(255,159,10,0.1);border-color:rgba(255,159,10,0.3);color:var(--orange)}
"""

if '.setup-badge' not in html:
    html = html.replace('</style>', SETUP_CSS + '</style>')
    print("✅ Added setup CSS")

# Add "Needs Setup" sort button to Products view header
OLD_PRODUCTS_HEADER = '<button class="btn btn-primary" onclick="openProductModal()">Add Product</button>'
NEW_PRODUCTS_HEADER = '<button class="sort-btn" id="sort-setup-btn" onclick="toggleSetupSort()">⭐ Needs Setup</button>\n              <button class="btn btn-primary" onclick="openProductModal()">Add Product</button>'

if 'sort-setup-btn' not in html:
    html = html.replace(OLD_PRODUCTS_HEADER, NEW_PRODUCTS_HEADER)
    print("✅ Added sort button")

# Update Products table header to include Setup column
OLD_TH = '<th>Name</th>\n                <th>Category</th>\n                <th>Storage Location</th>'
NEW_TH = '<th>Status</th>\n                <th>Name</th>\n                <th>Category</th>\n                <th>Storage</th>'

if '<th>Status</th>' not in html:
    html = html.replace(OLD_TH, NEW_TH)
    print("✅ Added Status column header")

# Now update the loadProducts function to include setup status
# Find the loadProducts function and enhance it
OLD_LOAD_PRODUCTS_START = 'async function loadProducts'
if OLD_LOAD_PRODUCTS_START in html:
    # Find the function
    idx = html.find(OLD_LOAD_PRODUCTS_START)
    # Find the end of the function (next async function or named function)
    func_start = idx
    
    # We need to replace the entire loadProducts function
    # Find its closing brace by tracking brace depth
    js_from_func = html[func_start:]
    brace_depth = 0
    func_end = 0
    in_func = False
    for i, ch in enumerate(js_from_func):
        if ch == '{':
            brace_depth += 1
            in_func = True
        elif ch == '}':
            brace_depth -= 1
            if in_func and brace_depth == 0:
                func_end = func_start + i + 1
                break
    
    old_func = html[func_start:func_end]
    
    NEW_LOAD_PRODUCTS = '''async function loadProducts(forceRefresh) {
  try {
    const categoryFilter = document.getElementById('products-category-filter').value;
    let url = '/api/inventory/products?active=true';
    if (categoryFilter) url += '&category=' + categoryFilter;
    
    const [products, setupData] = await Promise.all([
      fetch(url).then(r => r.json()),
      fetch('/api/inventory/products/setup-status').then(r => r.json())
    ]);
    
    // Build setup map
    const setupMap = {};
    setupData.forEach(s => { setupMap[s.id] = s; });
    
    // Sort: if setup sort active, put needs-setup first
    let sorted = products;
    if (document.getElementById('sort-setup-btn') && document.getElementById('sort-setup-btn').classList.contains('active')) {
      sorted = [...products].sort((a, b) => {
        const sa = setupMap[a.id], sb = setupMap[b.id];
        const aNeeds = sa ? sa.needs_setup.length + (sa.setup_complete ? 0 : 1) : 0;
        const bNeeds = sb ? sb.needs_setup.length + (sb.setup_complete ? 0 : 1) : 0;
        if (aNeeds !== bNeeds) return bNeeds - aNeeds;
        return a.name.localeCompare(b.name);
      });
    }
    
    document.getElementById('products-table').innerHTML = sorted.map(p => {
      const s = setupMap[p.id] || {};
      const needs = s.needs_setup || [];
      const isReady = s.is_ready;
      const isComplete = s.setup_complete === 1;
      
      let statusHtml;
      if (isReady) {
        statusHtml = '<span class="setup-badge setup-ready">✓ Ready</span>';
      } else {
        const checks = [];
        checks.push('<span class="setup-item ' + (needs.includes('purchase_unit') ? 'missing' : 'done') + '">' + (needs.includes('purchase_unit') ? '○' : '●') + ' Unit</span>');
        checks.push('<span class="setup-item ' + (needs.includes('inventory_unit') ? 'missing' : 'done') + '">' + (needs.includes('inventory_unit') ? '○' : '●') + ' Count</span>');
        checks.push('<span class="setup-item ' + (needs.includes('storage') ? 'missing' : 'done') + '">' + (needs.includes('storage') ? '○' : '●') + ' Storage</span>');
        statusHtml = '<span class="setup-badge setup-new">⭐ Setup</span><br><div style="margin-top:3px">' + checks.join(' ') + '</div>';
      }
      
      const starBtn = '<button class="setup-star" onclick="toggleSetup(' + p.id + ',' + (isComplete ? 'false' : 'true') + ')" title="' + (isComplete ? 'Mark incomplete' : 'Mark complete') + '">' + (isComplete ? '✅' : '☐') + '</button>';
      
      const conversion = p.unit_conversion && p.unit_conversion > 1 ? ' (' + p.unit_conversion + 'x)' : '';
      const tagClass = 'tag-' + (p.category || 'other').toLowerCase();
      
      return '<tr>' +
        '<td>' + starBtn + ' ' + statusHtml + '</td>' +
        '<td><strong>' + (p.name || '') + '</strong></td>' +
        '<td><span class="tag ' + tagClass + '">' + (p.category || '--') + '</span></td>' +
        '<td style="font-size:12px">' + (s.storage_count || 0) + ' area(s)</td>' +
        '<td>' + (p.unit || '--') + '</td>' +
        '<td>' + (p.inventory_unit || '--') + conversion + '</td>' +
        '<td>' + (p.current_price ? '$' + Number(p.current_price).toFixed(2) : '--') + '</td>' +
        '<td><button class="btn btn-secondary" style="padding:5px 10px;font-size:11px" onclick="editProduct(' + p.id + ')">Edit</button></td>' +
        '</tr>';
    }).join('');
    
    // Update dashboard stat
    const needsSetup = setupData.filter(s => !s.is_ready).length;
    const statEl = document.getElementById('stat-products');
    if (statEl) statEl.textContent = products.length + (needsSetup > 0 ? ' (' + needsSetup + ' need setup)' : '');
    
  } catch(e) {
    console.error('Error loading products:', e);
  }
}

let _setupSortActive = false;
function toggleSetupSort() {
  const btn = document.getElementById('sort-setup-btn');
  _setupSortActive = !_setupSortActive;
  btn.classList.toggle('active', _setupSortActive);
  loadProducts();
}

async function toggleSetup(productId, complete) {
  await fetch('/api/inventory/products/' + productId + '/setup', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ complete: complete })
  });
  loadProducts();
}'''
    
    html = html[:func_start] + NEW_LOAD_PRODUCTS + html[func_end:]
    print("✅ Replaced loadProducts function")

# Validate JS
js = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)[0]
open('/tmp/check.js', 'w').write(js)
result = subprocess.run(['node', '--check', '/tmp/check.js'], capture_output=True, text=True)
if result.returncode != 0:
    print("\n❌ JS VALIDATION FAILED:")
    print(result.stderr)
    print("Restoring backup...")
    shutil.copy('static/manage.html.bak2', 'static/manage.html')
else:
    open('static/manage.html', 'w').write(html)
    print("✅ JS validation passed")
    print("\n🎉 Done! Restart: systemctl restart rednun")

# Final summary
conn = get_connection()
total = conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
assigned = conn.execute("SELECT COUNT(DISTINCT product_id) FROM product_storage_locations").fetchone()[0]
complete = conn.execute("SELECT COUNT(*) FROM products WHERE active=1 AND setup_complete=1").fetchone()[0]
print(f"\n📊 Summary:")
print(f"   Total products: {total}")
print(f"   In storage areas: {assigned}")
print(f"   Setup complete: {complete}")
print(f"   Needs setup: {total - complete}")
conn.close()
