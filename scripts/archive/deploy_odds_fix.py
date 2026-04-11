#!/usr/bin/env python3
"""
Fix: Insert odds column HTML into the actual table structure.
The previous deploy_odds.py string matches didn't find the right patterns.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Add odds header after Home th, before closing </tr> or ch-col ──
html = html.replace(
    '<th class="score-col"></th><th>Home</th>',
    '<th class="score-col"></th><th>Home</th><th class="odds-col">Line</th>'
)

# ── 2. Add odds cell after {% endif %} before <td class="td-ch"> ──
html = html.replace(
    '                {% endif %}\n                <td class="td-ch">',
    '                {% endif %}\n                <td class="td-odds"></td>\n                <td class="td-ch">'
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Inserted odds column into table HTML")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
