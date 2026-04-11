#!/usr/bin/env python3
"""
Fix: STATUS_END_PERIOD (and other live statuses) not showing scores.
Treat any non-final, non-scheduled status as a live game.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Replace the status handling block with a more inclusive approach
old_status = """                    if(awayScoreCell&&homeScoreCell){
                        var aScore=parseInt(eg.awayScore)||0;
                        var hScore=parseInt(eg.homeScore)||0;
                        if(eg.status==='STATUS_IN_PROGRESS'){
                            awayScoreCell.innerHTML='<span class="score-num">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num">'+eg.homeScore+'</span>';
                            if(timeCell){
                                var detail=eg.statusDetail||'LIVE';
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>'+detail+'</span>';
                            }
                        } else if(eg.status==='STATUS_FINAL'){
                            var aw=aScore>=hScore?'score-winner':'score-loser';
                            var hw=hScore>=aScore?'score-winner':'score-loser';
                            awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span>';
                        } else if(eg.status==='STATUS_HALFTIME'){
                            awayScoreCell.innerHTML='<span class="score-num">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num">'+eg.homeScore+'</span>';
                            if(timeCell) timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>Half</span>';
                        } else {
                            awayScoreCell.innerHTML='';
                            homeScoreCell.innerHTML='';
                        }
                    }"""

new_status = """                    if(awayScoreCell&&homeScoreCell){
                        var aScore=parseInt(eg.awayScore)||0;
                        var hScore=parseInt(eg.homeScore)||0;
                        if(eg.status==='STATUS_FINAL'){
                            var aw=aScore>=hScore?'score-winner':'score-loser';
                            var hw=hScore>=aScore?'score-winner':'score-loser';
                            awayScoreCell.innerHTML='<span class="score-num '+aw+'">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num '+hw+'">'+eg.homeScore+'</span>';
                        } else if(eg.status==='STATUS_SCHEDULED'||eg.status==='STATUS_POSTPONED'){
                            awayScoreCell.innerHTML='';
                            homeScoreCell.innerHTML='';
                        } else {
                            // Any other status = live game (IN_PROGRESS, END_PERIOD, HALFTIME, DELAYED, etc)
                            awayScoreCell.innerHTML='<span class="score-num">'+eg.awayScore+'</span>';
                            homeScoreCell.innerHTML='<span class="score-num">'+eg.homeScore+'</span>';
                            if(timeCell){
                                var detail=eg.statusDetail||'LIVE';
                                timeCell.innerHTML='<span class="live-badge"><span class="live-dot"></span>'+detail+'</span>';
                            }
                        }
                    }"""

html = html.replace(old_status, new_status)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed — all live statuses now show scores")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Now handles ALL live statuses:")
print("   STATUS_IN_PROGRESS, STATUS_END_PERIOD, STATUS_HALFTIME,")
print("   STATUS_DELAYED, STATUS_RAIN_DELAY, etc.")
print("   Only SCHEDULED and POSTPONED show blank scores")
