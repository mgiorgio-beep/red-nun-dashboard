#!/usr/bin/env python3
"""
Deploy score layout fix: scores next to each team.
Away 3 VS 7 Home instead of stacked in one column.
"""

import os, shutil, subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Replace CSS ──
# Remove old score column styles
html = html.replace(
    ".game-table th.score-col{text-align:center;width:70px}",
    ".game-table th.score-col{text-align:center;width:36px;padding:8px 2px}"
)

html = html.replace(
    """.td-score{text-align:center;font-variant-numeric:tabular-nums}
.score-live{
    display:flex;flex-direction:column;align-items:center;gap:1px;
}
.score-num{font-family:'Barlow Condensed',sans-serif;font-size:16px;font-weight:800;line-height:1.2}
.score-away{color:var(--text2)}
.score-home{color:var(--text)}
.score-status{font-size:9px;color:var(--text3);font-weight:600;margin-top:2px}
.score-final{font-size:9px;color:var(--text3);font-weight:700;text-transform:uppercase}
.score-pre{font-size:10px;color:var(--text3)}""",
    """.td-score{text-align:center;font-variant-numeric:tabular-nums;width:36px;padding:8px 2px !important}
.score-num{font-family:'Barlow Condensed',sans-serif;font-size:18px;font-weight:800;color:var(--text)}
.score-pre{font-size:13px;color:var(--text3)}
.score-final-label{font-size:8px;color:var(--text3);font-weight:700;text-transform:uppercase;display:block}
.score-winner{color:var(--text)}
.score-loser{color:var(--text3)}"""
)

# Update mobile score size
html = html.replace(
    ".score-num{font-size:14px}",
    ".score-num{font-size:15px}"
)

# ── 2. Replace table header ──
html = html.replace(
    """<th>Away</th><th class="vs-col"></th><th>Home</th>
                <th class="score-col">Score</th>
                <th class="odds-col">Line</th>""",
    """<th>Away</th><th class="score-col"></th><th class="vs-col"></th><th class="score-col"></th><th>Home</th>
                <th class="odds-col">Line</th>"""
)

# ── 3. Replace table body rows ──
html = html.replace(
    """<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event }}</span></div></td>
                <td class="td-vs">VS</td>
                <td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>
                <td class="td-score"><span class="score-pre">—</span></td>""",
    """<td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event }}</span></div></td>
                <td class="td-score td-score-away"><span class="score-pre"></span></td>
                <td class="td-vs">VS</td>
                <td class="td-score td-score-home"><span class="score-pre"></span></td>
                <td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>"""
)

# ── 4. Replace JS score update logic ──
old_js = """                    var scoreCell=row.querySelector('.td-score');
                    var oddsCell=row.querySelector('.td-odds');
                    var timeCell=row.querySelector('.game-time-text');

                    if(scoreCell){
                        if(eg.status==='STATUS_IN_PROGRESS'){
                            scoreCell.innerHTML='<div class="score-live">'+
                                '<span class="score-num score-away">'+eg.awayScore+'</span>'+
                                '<span class="score-num score-home">'+eg.homeScore+'</span>'+
                                '</div>';
                            // Replace time with live badge
                            if(timeCell){
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>LIVE</span>';
                            }
                        } else if(eg.status==='STATUS_FINAL'){
                            scoreCell.innerHTML='<div class="score-live">'+
                                '<span class="score-num score-away">'+eg.awayScore+'</span>'+
                                '<span class="score-num score-home">'+eg.homeScore+'</span>'+
                                '<span class="score-final">Final</span>'+
                                '</div>';
                        } else if(eg.status==='STATUS_HALFTIME'){
                            scoreCell.innerHTML='<div class="score-live">'+
                                '<span class="score-num score-away">'+eg.awayScore+'</span>'+
                                '<span class="score-num score-home">'+eg.homeScore+'</span>'+
                                '<span class="score-status">Half</span>'+
                                '</div>';
                            if(timeCell) timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>HALF</span>';
                        } else {
                            // Pre-game — show dash
                            scoreCell.innerHTML='<span class="score-pre">—</span>';
                        }
                    }"""

new_js = """                    var awayScoreCell=row.querySelector('.td-score-away');
                    var homeScoreCell=row.querySelector('.td-score-home');
                    var oddsCell=row.querySelector('.td-odds');
                    var timeCell=row.querySelector('.game-time-text');

                    if(awayScoreCell&&homeScoreCell){
                        var aScore=parseInt(eg.awayScore)||0;
                        var hScore=parseInt(eg.homeScore)||0;
                        if(eg.status==='STATUS_IN_PROGRESS'){
                            awayScoreCell.innerHTML='<span class="score-num">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num">'+eg.homeScore+'</span>';
                            if(timeCell){
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>LIVE</span>';
                            }
                        } else if(eg.status==='STATUS_FINAL'){
                            var aw=aScore>=hScore?'score-winner':'score-loser';
                            var hw=hScore>=aScore?'score-winner':'score-loser';
                            awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span><span class="score-final-label">F</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span><span class="score-final-label">F</span>';
                        } else if(eg.status==='STATUS_HALFTIME'){
                            awayScoreCell.innerHTML='<span class="score-num">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num">'+eg.homeScore+'</span>';
                            if(timeCell) timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>HALF</span>';
                        } else {
                            awayScoreCell.innerHTML='';
                            homeScoreCell.innerHTML='';
                        }
                    }"""

html = html.replace(old_js, new_js)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Updated score layout — scores now next to each team")

# Restart
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted gunicorn")
print("\n✅ Layout: Away [score] VS [score] Home")
print("   Winner bold/dark, loser dimmed on final games")
print("   'F' label under score for finished games")
