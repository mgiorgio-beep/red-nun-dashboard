#!/usr/bin/env python3
"""
Add auto dark/light mode for mobile only, based on time of day.
Light 6am-7pm, dark 7pm-6am. Desktop stays light always.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Add dark theme CSS variables ──
dark_css = """
/* Auto dark mode (mobile only) */
body.dark-mobile {
    --bg: #0C0C0E;
    --card: #1A1A1E;
    --text: #F0F0F0;
    --text2: #A0A0A0;
    --text3: #606060;
    --border: #2A2A2E;
    --navy: #0D1520;
}
body.dark-mobile .header {
    background: #8B1A1A;
}
body.dark-mobile .sec-head {
    background: #0D1520;
}
body.dark-mobile .game-table th {
    background: #151518;
    color: #606060;
    border-bottom-color: #2A2A2E;
}
body.dark-mobile .game-table td {
    border-bottom-color: #222226;
}
body.dark-mobile .ch-num {
    color: #D4A843;
}
body.dark-mobile .td-vs {
    color: #505050;
}
body.dark-mobile .td-time {
    color: #909090;
}
body.dark-mobile tr.fav-row {
    background: linear-gradient(90deg, rgba(200,155,60,0.15), transparent);
}
body.dark-mobile .game-table {
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
body.dark-mobile .stale-banner {
    background: #3D2E00;
    color: #FFD060;
}
body.dark-mobile .ch-badge {
    opacity: 0.9;
}
body.dark-mobile .score-winner { color: #F0F0F0; }
body.dark-mobile .score-loser { color: #606060; }
body.dark-mobile .score-num { color: #F0F0F0; }
body.dark-mobile .live-badge { background: #1B3A1B; color: #4ADE80; }
body.dark-mobile .footer { color: #606060; }
body.dark-mobile .f-note { color: #505050; }
"""

# Insert before the closing </style>
html = html.replace('</style>', dark_css + '\n</style>')

# ── 2. Add JS for time-based toggle (mobile only) ──
dark_js = """
<script>
(function(){
    function checkDarkMode(){
        var isMobile = window.innerWidth <= 768;
        var hour = new Date().getHours();
        var isDark = hour < 6 || hour >= 19; // 7pm-6am
        if(isMobile && isDark){
            document.body.classList.add('dark-mobile');
        } else {
            document.body.classList.remove('dark-mobile');
        }
        // Also update theme-color meta for iOS status bar
        var meta = document.querySelector('meta[name="theme-color"]');
        if(meta){
            meta.content = (isMobile && isDark) ? '#8B1A1A' : '#C41E2A';
        }
    }
    // Check on load
    checkDarkMode();
    // Re-check every 5 minutes (in case phone sits open across sunset)
    setInterval(checkDarkMode, 300000);
    // Re-check on resize (rotation, etc)
    window.addEventListener('resize', checkDarkMode);
})();
</script>
"""

# Insert before the closing </body>
html = html.replace('</body>', dark_js + '</body>')

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Added mobile auto dark mode (7pm-6am)")

# Restart
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted gunicorn")
print("\n✅ Mobile auto dark/light mode:")
print("   Light: 6:00 AM - 6:59 PM")
print("   Dark:  7:00 PM - 5:59 AM")
print("   Desktop: always light")
print("   Re-checks every 5 min + on screen rotation")
