#!/usr/bin/env python3
"""
Show period/clock in live badge instead of just LIVE.
No team records.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace LIVE badge with period/clock detail
html = html.replace(
    """if(timeCell){
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>LIVE</span>';
                            }""",
    """if(timeCell){
                                var detail=eg.statusDetail||'LIVE';
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>'+detail+'</span>';
                            }"""
)

html = html.replace(
    """if(timeCell) timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>HALF</span>';""",
    """if(timeCell) timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>Half</span>';"""
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Live badge now shows period/clock")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
