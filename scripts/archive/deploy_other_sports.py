#!/usr/bin/env python3
"""
Fix Other Sports section - show game.sport as the detail column.
Data has: sport='Horse Racing:', event="America's Day At the Races"
Template was showing game.detail which doesn't exist.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# The "other sports" type sections use event/detail columns
# Replace to show sport as detail, falling back to detail if it exists
html = html.replace(
    '<td class="td-event">{{ game.event }}</td>\n                <td class="td-detail">{{ game.detail }}</td>',
    '<td class="td-event">{{ game.event }}</td>\n                <td class="td-detail">{{ game.sport if game.sport else game.detail }}</td>'
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Other Sports now shows sport type (Horse Racing, Aussie Rules FB, etc)")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
