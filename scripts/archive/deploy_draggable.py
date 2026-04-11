#!/usr/bin/env python3
"""
Make league sections draggable with persistent ordering.
- SortableJS for drag/drop on frontend
- POST order to /sports/api/section-order
- Saved to data/section_order.json
- Template sorts sections by saved order on render
"""

import subprocess, time, os, json

# ── 1. Add section order route to sports.py ──
SPORTS_PY = 'sports_guide/sports.py'
with open(SPORTS_PY, 'r') as f:
    spy = f.read()

if 'section-order' not in spy:
    order_route = '''

@sports_bp.route('/sports/api/section-order', methods=['GET', 'POST'])
def section_order():
    """Get or set section display order."""
    import json, os
    from flask import request
    order_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'section_order.json')

    if request.method == 'POST':
        data = request.get_json()
        if data and 'order' in data:
            os.makedirs(os.path.dirname(order_file), exist_ok=True)
            with open(order_file, 'w') as f:
                json.dump({'order': data['order']}, f)
            return {'status': 'ok'}
        return {'error': 'missing order'}, 400

    if os.path.exists(order_file):
        with open(order_file, 'r') as f:
            return json.load(f)
    return {'order': []}
'''
    with open(SPORTS_PY, 'a') as f:
        f.write(order_route)
    print("✓ Added /sports/api/section-order route")
else:
    print("✓ section-order route already exists")


# ── 2. Update the template render to sort sections ──
# We need to modify the route that renders sports_guide.html
# to sort sections based on saved order
with open(SPORTS_PY, 'r') as f:
    spy = f.read()

# Find the render route and add sorting logic
if 'section_order.json' not in spy.split('def sports_guide')[0] if 'def sports_guide' in spy else '':
    # Add sorting to the main route - we need to find where sections are passed to template
    # Look for where guide data is loaded and sections passed
    if "sections" in spy and "render_template" in spy:
        # Add import at top if needed
        if 'from flask import request' not in spy:
            spy = spy.replace('from flask import ', 'from flask import request, ')

        # Add section sorting before render_template
        sort_code = '''
    # Sort sections by saved order
    order_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'section_order.json')
    if os.path.exists(order_file):
        try:
            with open(order_file, 'r') as of:
                saved = json.load(of).get('order', [])
            if saved:
                order_map = {name.lower(): i for i, name in enumerate(saved)}
                guide['sections'].sort(key=lambda s: order_map.get(s['name'].lower(), 999))
        except:
            pass
'''
        # Insert before render_template call
        if 'return render_template' in spy:
            spy = spy.replace(
                "    return render_template('sports_guide.html'",
                sort_code + "\n    return render_template('sports_guide.html'"
            )
            with open(SPORTS_PY, 'w') as f:
                f.write(spy)
            print("✓ Added section sorting to render route")


# ── 3. Update template with SortableJS and drag handles ──
TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'
with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Add SortableJS from CDN
if 'SortableJS' not in html:
    html = html.replace(
        '</head>',
        '<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>\n</head>'
    )

# Add drag handle CSS
drag_css = """
/* Drag handle for sections */
.sec-header { cursor:grab; user-select:none; -webkit-user-select:none; }
.sec-header:active { cursor:grabbing; }
.drag-handle { opacity:0.5; margin-right:6px; font-size:12px; }
.section-card.sortable-ghost { opacity:0.3; }
.section-card.sortable-drag { box-shadow:0 8px 24px rgba(0,0,0,0.2); transform:scale(1.02); }
.drag-toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
    background:#1B2838; color:#fff; padding:10px 20px; border-radius:8px;
    font-size:13px; font-weight:600; z-index:9999; opacity:0; transition:opacity 0.3s; }
.drag-toast.show { opacity:1; }
"""
html = html.replace('</style>', drag_css + '</style>')

# Add drag handle icon to section headers
html = html.replace(
    '<span class="sec-title">',
    '<span class="drag-handle">☰</span><span class="sec-title">'
)

# Wrap all section cards in a sortable container
# Find where sections loop starts
html = html.replace(
    '{% for section in sections %}',
    '<div id="sections-container">\n    {% for section in sections %}'
)
html = html.replace(
    '{% endfor %}<!-- end sections -->',
    '{% endfor %}<!-- end sections -->\n    </div>'
)

# If the endfor doesn't have that comment, try without it
if 'sections-container' not in html:
    # Try a different approach - find the section card div
    html = html.replace(
        '{% for section in sections %}',
        '<div id="sections-container">\n    {% for section in sections %}'
    )

# Add data attribute for section name on the card
html = html.replace(
    '<div class="section-card">',
    '<div class="section-card" data-section="{{ section.name }}">'
)

# Add sortable JS initialization
sortable_js = """
// ═══════════════════════════════════════════════════
// DRAG & DROP SECTION ORDERING
// ═══════════════════════════════════════════════════
(function(){
    var container = document.getElementById('sections-container');
    if(!container || typeof Sortable === 'undefined') return;

    var toast = document.createElement('div');
    toast.className = 'drag-toast';
    document.body.appendChild(toast);

    function showToast(msg) {
        toast.textContent = msg;
        toast.classList.add('show');
        setTimeout(function(){ toast.classList.remove('show'); }, 2000);
    }

    Sortable.create(container, {
        animation: 200,
        handle: '.sec-header',
        ghostClass: 'sortable-ghost',
        dragClass: 'sortable-drag',
        onEnd: function() {
            var cards = container.querySelectorAll('.section-card');
            var order = [];
            cards.forEach(function(c) {
                var name = c.getAttribute('data-section');
                if(name) order.push(name);
            });

            fetch('/sports/api/section-order', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({order: order})
            }).then(function(r) {
                if(r.ok) showToast('✓ Order saved');
                else showToast('✗ Failed to save');
            }).catch(function() {
                showToast('✗ Failed to save');
            });
        }
    });
})();
"""

html = html.replace('</body>', '<script>' + sortable_js + '</script>\n</body>')

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Added drag/drop to template")

# ── 4. Restart ──
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Sections are now draggable!")
print("   - Grab the ☰ handle or section header to drag")
print("   - New order saves automatically")
print("   - Persists across scrapes")
print("   - Toast notification confirms save")
