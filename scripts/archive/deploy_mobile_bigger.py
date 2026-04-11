#!/usr/bin/env python3
"""
Increase font sizes on mobile for better readability on iOS.
Bumps team names, times, scores, headers, channel numbers all up ~2px.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace the existing mobile media query font sizes
# Current mobile sizes → New sizes
replacements = [
    # Team names: 12px → 14px
    ('    .td-team span{font-size:12px}', '    .td-team span{font-size:14px}'),
    # Time: 11px → 13px
    ('    .td-time{font-size:11px}', '    .td-time{font-size:13px}'),
    # Channel number: 18px → 20px
    ('    .ch-num{font-size:18px}', '    .ch-num{font-size:20px}'),
    # Table headers: 8px → 10px
    ('    .game-table th{padding:6px 6px;font-size:8px}', '    .game-table th{padding:7px 6px;font-size:10px}'),
    # Section titles: 13px → 14px
    ('    .sec-title{font-size:13px}', '    .sec-title{font-size:14px}'),
    # Score numbers: 13px → 15px
    ('    .score-num{font-size:13px}', '    .score-num{font-size:15px}'),
    # Header title: 16px → 18px
    ('    .h-title{font-size:16px}', '    .h-title{font-size:18px}'),
    # Header date: 15px → 17px
    ('    .h-date{font-size:15px}', '    .h-date{font-size:17px}'),
]

for old, new in replacements:
    html = html.replace(old, new)

# Also bump the base td font size for mobile
# Current: .game-table td{padding:10px;font-size:13px...}
# Add mobile override
mobile_extra = """
    .game-table td{padding:10px 6px;font-size:14px}
    .td-vs{font-size:11px}
    .ch-net{font-size:10px}
    .ch-app{font-size:10px}
    .ch-badge{font-size:11px}
    .td-detail{font-size:13px}
    .td-event{font-size:13px}
    .live-badge{font-size:10px;padding:3px 8px}
"""

# Insert after the .h-date mobile rule
html = html.replace(
    '    .h-date{font-size:17px}',
    '    .h-date{font-size:17px}' + mobile_extra
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Mobile font sizes increased")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Mobile font size increases:")
print("   Team names: 12→14px")
print("   Times: 11→13px")
print("   Scores: 13→15px")
print("   Table cells: 13→14px")
print("   Section titles: 13→14px")
print("   Channel #: 18→20px")
print("   Headers: 8→10px")
print("   Network labels: 9→10px")
print("   Streaming badges: 10→11px")
print("   VS: 10→11px")
