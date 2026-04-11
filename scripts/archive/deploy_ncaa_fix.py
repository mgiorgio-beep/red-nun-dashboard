#!/usr/bin/env python3
"""
Fix NCAA scores not loading.
Issues:
1. Dash mismatch: FANZO uses - but ESPN_MAP had – (em dash)
2. Section name matching needs to be more flexible
3. NCAA team matching needs to handle "NCAA: Temple" format
"""

import subprocess, time

TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# ── 1. Replace ESPN_MAP with normalized keys and better matching ──
old_map = '''var ESPN_MAP={
    'nba basketball':{sport:'basketball',league:'nba'},
    'mlb baseball':{sport:'baseball',league:'mlb'},
    'nhl hockey':{sport:'hockey',league:'nhl'},
    'nfl football':{sport:'football',league:'nfl'},
    "ncaa basketball \u2013 men's":{sport:'basketball',league:'mens-college-basketball'},
    "ncaa basketball \u2013 women's":{sport:'basketball',league:'womens-college-basketball'},
    'ncaa football':{sport:'football',league:'college-football'},
    'ncaa hockey':{sport:'hockey',league:'mens-college-hockey'},
    'ncaa baseball':{sport:'baseball',league:'college-baseball'},
    'softball':{sport:'softball',league:'college-softball'},
    'nbagl basketball':{sport:'basketball',league:'nba-g-league'},
};'''

new_map = '''var ESPN_MAP=[
    {match:'nba basketball',sport:'basketball',league:'nba'},
    {match:'mlb baseball',sport:'baseball',league:'mlb'},
    {match:'nhl hockey',sport:'hockey',league:'nhl'},
    {match:'nfl football',sport:'football',league:'nfl'},
    {match:'ncaa basketball',sub:'women',sport:'basketball',league:'womens-college-basketball'},
    {match:'ncaa basketball',sport:'basketball',league:'mens-college-basketball'},
    {match:'ncaa football',sport:'football',league:'college-football'},
    {match:'ncaa hockey',sport:'hockey',league:'mens-college-hockey'},
    {match:'ncaa baseball',sport:'baseball',league:'college-baseball'},
    {match:'softball',sport:'softball',league:'college-softball'},
    {match:'nbagl basketball',sport:'basketball',league:'nba-g-league'},
];
function findEspnLeague(secName){
    var s=secName.toLowerCase().replace(/[^a-z ]/g,' ');
    for(var i=0;i<ESPN_MAP.length;i++){
        var m=ESPN_MAP[i];
        if(s.indexOf(m.match)!==-1){
            if(m.sub&&s.indexOf(m.sub)===-1) continue;
            return {sport:m.sport,league:m.league};
        }
    }
    return null;
}'''

html = html.replace(old_map, new_map)

# ── 2. Replace the lookup in fetchScores ──
old_lookup = '''var espn=null;
        for(var k in ESPN_MAP){if(secName.indexOf(k)!==-1||k.indexOf(secName)!==-1){espn=ESPN_MAP[k];break;}}
        if(!espn)return;'''

new_lookup = '''var espn=findEspnLeague(secName);
        if(!espn)return;'''

html = html.replace(old_lookup, new_lookup)

# ── 3. Improve normTeam to strip more NCAA prefixes and handle rankings ──
old_norm = '''function normTeam(t){
    return t.toLowerCase()
        .replace(/^\\(w\\)/,'').replace(/^ncaa[a-z]*:\\s*/i,'')
        .replace(/^\\(\\d+\\)/,'').replace(/[^a-z ]/g,' ').replace(/\\s+/g,' ').trim();
}'''

new_norm = '''function normTeam(t){
    return t.toLowerCase()
        .replace(/^\\(w\\)/i,'')
        .replace(/^\\(?w\\)?ncaa[a-z]*:\\s*/i,'')
        .replace(/^ncaa[a-z]*:\\s*/i,'')
        .replace(/^\\(\\d+\\)/,'')
        .replace(/[^a-z ]/g,' ')
        .replace(/\\s+/g,' ').trim();
}'''

html = html.replace(old_norm, new_norm)

# ── 4. Make matchGame more flexible for NCAA — match on last word (mascot or school name) ──
old_match = '''function matchGame(espnGame, awayText, homeText){
    var ea=normTeam(espnGame.away);
    var eh=normTeam(espnGame.home);
    var ga=normTeam(awayText);
    var gh=normTeam(homeText);
    // Check if any word from our data matches ESPN
    var awayMatch=ga.split(' ').some(function(w){return w.length>2&&ea.indexOf(w)!==-1});
    var homeMatch=gh.split(' ').some(function(w){return w.length>2&&eh.indexOf(w)!==-1});
    return awayMatch&&homeMatch;
}'''

new_match = '''function matchGame(espnGame, awayText, homeText){
    var ea=normTeam(espnGame.away);
    var eh=normTeam(espnGame.home);
    var ga=normTeam(awayText);
    var gh=normTeam(homeText);
    // Try full substring match first
    if(ea.indexOf(ga)!==-1||ga.indexOf(ea)!==-1){
        if(eh.indexOf(gh)!==-1||gh.indexOf(eh)!==-1) return true;
    }
    // Then try word-level match (any word >2 chars)
    var awayMatch=ga.split(' ').some(function(w){return w.length>2&&ea.indexOf(w)!==-1});
    var homeMatch=gh.split(' ').some(function(w){return w.length>2&&eh.indexOf(w)!==-1});
    return awayMatch&&homeMatch;
}'''

html = html.replace(old_match, new_match)

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Fixed NCAA score matching")

subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted")
print("\n✅ Fixes:")
print("   - Section matching strips all dashes/special chars")
print("   - Women's NCAA matches before Men's (more specific first)")
print("   - Team matching tries full name first, then word-level")
print("   - NCAA: prefix and (W) prefix stripped properly")
