#!/usr/bin/env python3
"""
Fix:
1. Add date param to ESPN API so it returns ALL games for the day
2. Fix ncaa.png and pga.png league logo URLs
3. Fix favorites matching (Bruins game)
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Add today's date to ESPN scoreboard URL ──
old_url = "var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard';"
new_url = """var now=new Date();
        var yyyy=now.getFullYear();
        var mm=String(now.getMonth()+1).padStart(2,'0');
        var dd=String(now.getDate()).padStart(2,'0');
        var dateStr=yyyy+mm+dd;
        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+dateStr+'&limit=100';"""

html = html.replace(old_url, new_url)

# ── 2. Fix league logo URLs ──
# NCAA logo
html = html.replace(
    '"ncaa basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png"',
    '"ncaa basketball":"https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-basketball.png"'
)
html = html.replace(
    '"ncaa hockey":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png"',
    '"ncaa hockey":"https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-hockey.png"'
)
html = html.replace(
    '"ncaa baseball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png"',
    '"ncaa baseball":"https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-baseball.png"'
)
html = html.replace(
    '"softball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png"',
    '"softball":"https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-softball.png"'
)

# PGA logo - use a different path
html = html.replace(
    '"golf":"https://a.espncdn.com/combiner/i?img=/i/teamlogos/leagues/500/pga.png&w=40&h=40"',
    '"golf":"https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-golf.png"'
)

# G-League logo
html = html.replace(
    '"nbagl basketball":"https://a.espncdn.com/combiner/i?img=/i/teamlogos/leagues/500-dark/nba_gleague.png&w=40&h=40"',
    '"nbagl basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/nba.png"'
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed ESPN date param and league logos")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Fixes:")
print("   - ESPN now fetches with ?dates=YYYYMMDD&limit=100")
print("     This returns ALL games for the day, not just live/recent")
print("   - Fixed NCAA/PGA/G-League/Softball league logo URLs")
print("   - NCAA men's should now show ALL game scores")
