#!/usr/bin/env python3
"""
Fix: groups=100 returned 0 games for all NCAA endpoints.
Use groups=50 for basketball only (standard 'all conferences' value).
Remove groups for hockey/softball/other where it breaks.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace the broken groups logic
old_url = """var isNcaa=espn.league.indexOf('college')!==-1||espn.league.indexOf('mens-')!==-1||espn.league.indexOf('womens-')!==-1;
        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+GUIDE_DATE+'&limit=300'+(isNcaa?'&groups=100':'');"""

new_url = """var grp='';
        if(espn.league==='mens-college-basketball'||espn.league==='womens-college-basketball') grp='&groups=50';
        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+GUIDE_DATE+'&limit=300'+grp;"""

html = html.replace(old_url, new_url)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed groups param — groups=50 for basketball only")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ groups=50 for NCAA basketball (all conferences)")
print("   No groups param for hockey/softball (was breaking them)")
