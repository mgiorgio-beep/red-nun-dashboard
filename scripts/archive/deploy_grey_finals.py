#!/usr/bin/env python3
"""
Grey out finished games. Add 'game-final' class to rows via JS,
then dim everything in that row.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Add CSS for greyed-out final rows ──
final_css = """
/* Finished games - greyed out */
tr.game-final td { opacity: 0.4; }
tr.game-final:hover td { opacity: 0.7; }
body.dark-mobile tr.game-final td { opacity: 0.3; }
body.dark-mobile tr.game-final:hover td { opacity: 0.6; }
"""
html = html.replace('</style>', final_css + '</style>')

# ── 2. Add class to row when game is final ──
# After the score update block, add the class
html = html.replace(
    """if(eg.status==='STATUS_FINAL'){
                            var aw=aScore>=hScore?'score-winner':'score-loser';
                            var hw=hScore>=aScore?'score-winner':'score-loser';
                            awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span>';""",
    """if(eg.status==='STATUS_FINAL'){
                            var aw=aScore>=hScore?'score-winner':'score-loser';
                            var hw=hScore>=aScore?'score-winner':'score-loser';
                            awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span>';
                            row.classList.add('game-final');"""
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Finished games now greyed out")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Final games dimmed to 40% opacity")
print("   Hover brings them back to 70%")
print("   Live + upcoming stay full brightness")
