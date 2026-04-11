#!/usr/bin/env python3
import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

html = html.replace(
    ".score-num{font-family:'Barlow Condensed',sans-serif;font-size:18px;font-weight:800;color:var(--text)}",
    ".score-num{font-family:'Barlow Condensed',sans-serif;font-size:14px;font-weight:700;color:var(--text)}"
)

html = html.replace(
    ".score-num{font-size:15px}",
    ".score-num{font-size:13px}"
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Shrunk score font")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
