#!/usr/bin/env python3
"""
1. Strip NCAA: and (W)NCAA: prefixes from team names
2. Remove F final label under scores
3. Hide Line/odds column entirely
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Strip NCAA prefixes in template ──
# Away team
html = html.replace(
    '<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event }}</span></div></td>',
    '<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event|regex_replace("^\\\\(W\\\\)NCAA:\\s*|^NCAA:\\s*", "") }}</span></div></td>'
)
# Home team
html = html.replace(
    '<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>',
    '<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail|regex_replace("^\\\\(W\\\\)NCAA:\\s*|^NCAA:\\s*", "") }}</span></div></td>'
)

# Since Jinja doesn't have regex_replace by default, let's use a simpler approach
# Revert and just do it in JS after page load instead
html = html.replace(
    '<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event|regex_replace("^\\\\(W\\\\)NCAA:\\s*|^NCAA:\\s*", "") }}</span></div></td>',
    '<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event }}</span></div></td>'
)
html = html.replace(
    '<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail|regex_replace("^\\\\(W\\\\)NCAA:\\s*|^NCAA:\\s*", "") }}</span></div></td>',
    '<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>'
)

# Add JS to strip prefixes from displayed team names
strip_js = """
// Strip NCAA: and (W)NCAA: prefixes from displayed names
document.querySelectorAll('.td-team span').forEach(function(el){
    el.textContent = el.textContent.replace(/^\\(W\\)NCAA:\\s*/i, '').replace(/^NCAA:\\s*/i, '');
});
"""
html = html.replace(
    "// Apply league logos",
    strip_js + "\n// Apply league logos"
)

# ── 2. Remove F final label ──
html = html.replace(
    """awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span><span class="score-final-label">F</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span><span class="score-final-label">F</span>';""",
    """awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span>';"""
)

# Remove the F label CSS too
html = html.replace(
    ".score-final-label{font-size:8px;color:var(--text3);font-weight:700;text-transform:uppercase;display:block}",
    ""
)

# ── 3. Hide Line/odds column ──
# Remove odds header
html = html.replace(
    """<th class="odds-col">Line</th>""",
    ""
)
# Remove odds cells
html = html.replace(
    """<td class="td-odds"><span class="odds-spread">—</span></td>""",
    ""
)
# Remove odds CSS
html = html.replace(
    ".game-table th.odds-col{text-align:center;width:60px}",
    ""
)
html = html.replace(
    """/* Odds column */
.td-odds{text-align:center}
.odds-spread{font-size:11px;font-weight:600;color:var(--text)}
.odds-ou{font-size:9px;color:var(--text3);margin-top:1px}""",
    ""
)
# Remove odds JS update
html = html.replace(
    """                    if(oddsCell&&eg.spread){
                        oddsCell.innerHTML='<div class="odds-spread">'+eg.spread+'</div>'+
                            (eg.overUnder?'<div class="odds-ou">O/U '+eg.overUnder+'</div>':'');
                    }""",
    ""
)
# Clean up the oddsCell variable declaration
html = html.replace(
    "var oddsCell=row.querySelector('.td-odds');",
    ""
)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Stripped NCAA prefixes, removed F labels, hidden Line column")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
