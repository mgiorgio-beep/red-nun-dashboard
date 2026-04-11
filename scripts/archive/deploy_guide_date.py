#!/usr/bin/env python3
"""
Fix: Use the guide's date for ESPN API, not the current date.
After midnight the guide still shows yesterday's games but ESPN returns today's.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace the JS date logic that uses new Date() with the guide's date from the page
old_date = """var now=new Date();
        var yyyy=now.getFullYear();
        var mm=String(now.getMonth()+1).padStart(2,'0');
        var dd=String(now.getDate()).padStart(2,'0');
        var dateStr=yyyy+mm+dd;
        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+dateStr+'&limit=100';"""

new_date = """var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard?dates='+GUIDE_DATE+'&limit=100';"""

html = html.replace(old_date, new_date)

# Add GUIDE_DATE variable at the top of the script block, parsed from the page
# The date is in the h-date element like "Thursday 2/26/26"
guide_date_js = """
// Parse guide date from page for ESPN API
var GUIDE_DATE=(function(){
    var el=document.querySelector('.h-date');
    if(!el)return '';
    var txt=el.textContent.trim(); // e.g. "Thursday 2/26/26" or "THU 2/26"
    var m=txt.match(/(\\d{1,2})\\/(\\d{1,2})(?:\\/(\\d{2,4}))?/);
    if(!m)return '';
    var month=m[1].padStart(2,'0');
    var day=m[2].padStart(2,'0');
    var year=m[3]?m[3]:'';
    if(year.length===2) year='20'+year;
    if(!year) year=String(new Date().getFullYear());
    return year+month+day;
})();
console.log('[ESPN] Guide date: '+GUIDE_DATE);
"""

# Insert before the ESPN_MAP definition
html = html.replace(
    "var ESPN_MAP=[",
    guide_date_js + "\nvar ESPN_MAP=["
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ ESPN now uses guide date instead of today's date")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Fix: ESPN queries now match the guide's date")
print("   After midnight, guide shows Thursday -> ESPN fetches Thursday scores")
print("   After 5am scrape, guide shows Friday -> ESPN fetches Friday scores")
