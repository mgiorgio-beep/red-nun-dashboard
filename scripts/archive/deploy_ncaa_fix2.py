#!/usr/bin/env python3
"""
Fix updateRows section matching for NCAA and add debug logging.
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Fix updateRows — normalize section names before comparing ──
old_update = """function updateRows(secName,espnGames){
    var sections=document.querySelectorAll('[data-section]');
    sections.forEach(function(sec){
        if(sec.getAttribute('data-section').toLowerCase().indexOf(secName)===-1&&
           secName.indexOf(sec.getAttribute('data-section').toLowerCase())===-1)return;"""

new_update = """function normSec(s){return s.toLowerCase().replace(/[^a-z ]/g,' ').replace(/\\s+/g,' ').trim();}
function updateRows(secName,espnGames){
    var ns=normSec(secName);
    var sections=document.querySelectorAll('[data-section]');
    console.log('[ESPN] Updating "'+secName+'" with '+espnGames.length+' games');
    sections.forEach(function(sec){
        var sn=normSec(sec.getAttribute('data-section'));
        if(sn.indexOf(ns)===-1&&ns.indexOf(sn)===-1)return;"""

html = html.replace(old_update, new_update)

# ── 2. Add logging to matchGame ──
old_nomatch = """                    espnGames.splice(i,1);
                    break;
                }
            }
        });
    });"""

new_nomatch = """                    console.log('[ESPN] Matched: "'+away+'" vs "'+home+'" -> '+eg.away+' vs '+eg.home+' ('+eg.status+')');
                    espnGames.splice(i,1);
                    break;
                }
            }
        });
    });
    if(espnGames.length>0){
        console.log('[ESPN] Unmatched games in '+secName+':',espnGames.map(function(g){return g.away+' vs '+g.home}).join(', '));
    }"""

html = html.replace(old_nomatch, new_nomatch)

# ── 3. Also fix: fetchScores passes secName as the raw section name from first match
# but updateRows gets called once per API fetch — make sure ALL matching sections get updated
old_fetch_call = """updateRows(secName,games);
        }).catch(function(){});"""

new_fetch_call = """// Find all section names that map to this ESPN league
            var matchingSections=[];
            document.querySelectorAll('[data-section]').forEach(function(s){
                var sn=s.getAttribute('data-section').toLowerCase();
                var e2=findEspnLeague(sn);
                if(e2&&e2.sport===espn.sport&&e2.league===espn.league){
                    matchingSections.push(sn);
                }
            });
            // Also include Favorites and Streaming which may have games from this league
            matchingSections.push('favorites');
            matchingSections.forEach(function(ms){
                updateRows(ms,JSON.parse(JSON.stringify(games)));
            });
        }).catch(function(err){console.log('[ESPN] Fetch error:',err)});"""

html = html.replace(old_fetch_call, new_fetch_call)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed NCAA section matching and added debug logging")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Fixes:")
print("   - Section matching now normalizes special characters")
print("   - Scores update across ALL sections that share an ESPN league")
print("   - Favorites section now gets scores too")
print("   - Open browser console (F12) to see match/unmatch logging")
