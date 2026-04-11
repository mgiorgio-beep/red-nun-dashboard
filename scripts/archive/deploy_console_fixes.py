#!/usr/bin/env python3
"""
Fix console errors:
1. Service Worker scope error - remove it (not needed for this)
2. Softball ESPN endpoint 400 - wrong slug
3. G-League ESPN endpoint 400 - wrong slug  
4. Missing logo 404s - fix ESPN IDs
5. Deprecated meta tag
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Remove Service Worker (not needed) ──
html = html.replace(
    """<script>
if("serviceWorker" in navigator){
    navigator.serviceWorker.register("/sports/static/sw.js",{scope:"/sports/"})
    .then(r=>console.log("SW:",r.scope)).catch(e=>console.log("SW err:",e));
}
</script>""",
    ""
)

# ── 2. Fix deprecated meta tag ──
html = html.replace(
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="mobile-web-app-capable" content="yes">'
)

# ── 3. Fix ESPN league slugs ──
html = html.replace(
    "{match:'softball',sport:'softball',league:'college-softball'},",
    "{match:'softball',sport:'baseball',league:'college-softball'},"
)
html = html.replace(
    "{match:'nbagl basketball',sport:'basketball',league:'nba-g-league'},",
    "{match:'nbagl basketball',sport:'basketball',league:'nba-development'},"
)

# ── 4. Fix league logo URLs ──
html = html.replace(
    '"golf":"https://a.espncdn.com/i/teamlogos/leagues/500/pga.png"',
    '"golf":"https://a.espncdn.com/combiner/i?img=/i/teamlogos/leagues/500/pga.png&w=40&h=40"'
)
html = html.replace(
    '"nbagl basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/nba-g-league.png"',
    '"nbagl basketball":"https://a.espncdn.com/combiner/i?img=/i/teamlogos/leagues/500-dark/nba_gleague.png&w=40&h=40"'
)

# ── 5. Fix NCAA team IDs that 404 ──
# High Point 2314 -> try 2314 is correct but might not exist, skip
# Villanova 2918 -> should be 222
html = html.replace('"villanova":2918', '"villanova":222')

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed console errors")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Fixed:")
print("   - Removed Service Worker (not needed)")
print("   - Fixed deprecated meta tag")
print("   - Fixed softball/G-League ESPN endpoints")
print("   - Fixed Villanova logo ID")
print("   - Fixed PGA/G-League league logos")
