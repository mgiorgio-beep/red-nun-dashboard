#!/usr/bin/env python3
"""
Add groups=100 to NCAA ESPN API calls to get ALL D1 games.
ESPN caps default response to nationally televised games only.
groups=100 returns every game.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace the URL builder to add groups param for NCAA leagues
old_url = "var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+GUIDE_DATE+'&limit=100';"

new_url = """var isNcaa=espn.league.indexOf('college')!==-1||espn.league.indexOf('mens-')!==-1||espn.league.indexOf('womens-')!==-1;
        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+GUIDE_DATE+'&limit=300'+(isNcaa?'&groups=100':'');"""

html = html.replace(old_url, new_url)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Added groups=100 for NCAA endpoints")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ ESPN NCAA endpoints now request ALL D1 games")
print("   Temple, High Point, Delaware, Wichita State, Florida Int. should all get scores now")
