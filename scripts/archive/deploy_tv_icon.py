"""
deploy_tv_icon.py — Replace RED NUN text badge with TV icon SVG in sports guide header
Run on server: /opt/rednun/venv/bin/python3 deploy_tv_icon.py
"""
import re

TEMPLATE = '/opt/rednun/sports_guide/templates/sports_guide.html'

with open(TEMPLATE, 'r') as f:
    html = f.read()

# --- 1. Replace .logo-badge CSS with .logo-icon CSS ---
# Find the existing logo-badge CSS rule(s) and replace
# Handle both possible class names: .logo-badge and .logo

# Remove old logo-badge CSS
html = re.sub(
    r'\.logo-badge\{[^}]+\}',
    '.logo-icon{height:44px;width:auto;display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.3))}',
    html
)

# Also handle if it's just .logo { ... } in the dark mode section
html = re.sub(
    r'(\.logo\{)[^}]*(font-family:[^}]*Oswald[^}]*color:var\(--gold\)\})',
    r'.logo-icon{height:44px;width:auto;display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.3))}',
    html
)

# --- 2. Add dark mode override for icon ---
# In dark mode the icon is already white so it works great
# But if there's a dark mode .logo-badge override, clean it up
html = re.sub(r'\.dark \.logo-badge\{[^}]+\}', '', html)
html = re.sub(r'\.dark \.logo\{[^}]+\}', '', html)

# --- 3. Replace the HTML element ---
# The inline SVG icon (white, works on red header)
TV_ICON_SVG = '''<img class="logo-icon" alt="Red Nun" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512' fill='none'%3E%3Cline x1='224' y1='138' x2='185' y2='60' stroke='white' stroke-width='11' stroke-linecap='round'/%3E%3Cline x1='288' y1='138' x2='327' y2='60' stroke='white' stroke-width='11' stroke-linecap='round'/%3E%3Ccircle cx='185' cy='56' r='7' fill='white'/%3E%3Ccircle cx='327' cy='56' r='7' fill='white'/%3E%3Crect x='120' y='130' width='272' height='215' rx='24' ry='24' stroke='white' stroke-width='11' fill='none'/%3E%3Crect x='150' y='157' width='212' height='161' rx='14' ry='14' stroke='white' stroke-width='7' fill='none'/%3E%3Cline x1='198' y1='345' x2='185' y2='372' stroke='white' stroke-width='9' stroke-linecap='round'/%3E%3Cline x1='314' y1='345' x2='327' y2='372' stroke='white' stroke-width='9' stroke-linecap='round'/%3E%3Ctext x='256' y='440' text-anchor='middle' font-family='Arial Black,Arial,sans-serif' font-weight='900' font-size='68' fill='white' letter-spacing='5'%3ERED NUN%3C/text%3E%3C/svg%3E">'''

# Replace logo-badge div with img
html = re.sub(
    r'<div class="logo-badge">[^<]*</div>',
    TV_ICON_SVG,
    html
)

# Also try .logo class
html = re.sub(
    r'<div class="logo">[^<]*</div>',
    TV_ICON_SVG,
    html
)

# --- 4. Also update the embed template if it exists ---
with open(TEMPLATE, 'w') as f:
    f.write(html)

print('✅ Sports guide header updated with TV icon')

# Try to update embed template too
try:
    EMBED = '/opt/rednun/sports_guide/templates/sports_embed.html'
    with open(EMBED, 'r') as f:
        embed_html = f.read()
    
    embed_html = re.sub(
        r'<div class="logo-badge">[^<]*</div>',
        TV_ICON_SVG.replace('height:44px', 'height:36px'),
        embed_html
    )
    embed_html = re.sub(
        r'<div class="logo">[^<]*</div>',
        TV_ICON_SVG.replace('height:44px', 'height:36px'),
        embed_html
    )
    
    with open(EMBED, 'w') as f:
        f.write(embed_html)
    print('✅ Embed template updated too')
except FileNotFoundError:
    print('ℹ️  No embed template found, skipping')

print('\nRestart gunicorn:')
print('pkill -f "gunicorn.*server:app" && sleep 2 && cd /opt/rednun && /opt/rednun/venv/bin/gunicorn --bind 127.0.0.1:8080 --workers 1 --timeout 120 server:app --daemon')
