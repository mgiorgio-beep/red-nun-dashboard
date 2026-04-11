#!/usr/bin/env python3
"""
Add betting odds from The Odds API (the-odds-api.com).
- Server-side fetch, cached to JSON
- Displayed in Line column on desktop only (hidden on mobile)
- Free tier: 500 requests/month

SETUP: 
1. Get free API key at https://the-odds-api.com/#get-access
2. Add to .env: ODDS_API_KEY=your_key_here
3. Run this script to deploy
"""

import subprocess, time, os

# ── 1. Create the odds fetcher script ──
ODDS_FETCHER = r'''#!/usr/bin/env python3
"""Fetch betting odds from The Odds API and cache to JSON."""
import json, os, requests, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('odds_fetcher')

API_KEY = os.environ.get('ODDS_API_KEY', '')
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
ODDS_FILE = os.path.join(DATA_DIR, 'odds.json')

# Maps our section names to Odds API sport keys
SPORT_MAP = {
    'americanfootball_nfl': 'NFL Football',
    'basketball_nba': 'NBA Basketball',
    'baseball_mlb': 'MLB Baseball',
    'icehockey_nhl': 'NHL Hockey',
    'basketball_ncaab': 'NCAA Basketball',
    'americanfootball_ncaaf': 'NCAA Football',
}

def fetch_odds():
    if not API_KEY:
        log.warning('No ODDS_API_KEY set in environment')
        return

    all_odds = {}
    sports = ['americanfootball_nfl', 'basketball_nba', 'baseball_mlb',
              'icehockey_nhl', 'basketball_ncaab']

    for sport_key in sports:
        try:
            url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/'
            params = {
                'apiKey': API_KEY,
                'regions': 'us',
                'markets': 'spreads,totals',
                'oddsFormat': 'american',
                'bookmakers': 'draftkings,fanduel',
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                games = resp.json()
                for game in games:
                    away = game.get('away_team', '')
                    home = game.get('home_team', '')
                    key = _make_key(away, home)

                    spread = ''
                    over_under = ''
                    for bm in game.get('bookmakers', []):
                        for market in bm.get('markets', []):
                            if market['key'] == 'spreads' and not spread:
                                for outcome in market['outcomes']:
                                    if outcome['name'] == home:
                                        pt = outcome['point']
                                        spread = f"{home.split()[-1]} {'+' if pt > 0 else ''}{pt}"
                                        break
                            if market['key'] == 'totals' and not over_under:
                                for outcome in market['outcomes']:
                                    if outcome['name'] == 'Over':
                                        over_under = str(outcome['point'])
                                        break
                        if spread and over_under:
                            break

                    if spread or over_under:
                        all_odds[key] = {
                            'spread': spread,
                            'overUnder': over_under,
                            'away': away,
                            'home': home,
                        }

                remaining = resp.headers.get('x-requests-remaining', '?')
                log.info(f'{sport_key}: {len(games)} games, {remaining} API calls left')
            elif resp.status_code == 401:
                log.error('Invalid API key')
                return
            else:
                log.warning(f'{sport_key}: HTTP {resp.status_code}')
        except Exception as e:
            log.error(f'{sport_key}: {e}')

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ODDS_FILE, 'w') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'odds': all_odds
        }, f, indent=2)
    log.info(f'Saved {len(all_odds)} odds to {ODDS_FILE}')


def _make_key(away, home):
    """Normalize team names to create a matchup key."""
    def norm(t):
        return t.lower().replace('.', '').replace("'", '').strip()
    return f"{norm(away)}@{norm(home)}"


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    API_KEY = os.environ.get('ODDS_API_KEY', '')
    fetch_odds()
'''

with open('sports_guide/odds_fetcher.py', 'w') as f:
    f.write(ODDS_FETCHER)
print("✓ Created sports_guide/odds_fetcher.py")


# ── 2. Update template — add odds column (desktop only) + JS to load odds ──
TEMPLATE_PATH = 'sports_guide/templates/sports_guide.html'

with open(TEMPLATE_PATH, 'r') as f:
    html = f.read()

# Add CSS for desktop-only odds column
odds_css = """
/* Odds column - desktop only */
.odds-col { text-align:center; width:80px; }
.td-odds { text-align:center; }
.odds-spread { font-size:11px; font-weight:600; color:var(--text); white-space:nowrap; }
.odds-ou { font-size:9px; color:var(--text3); margin-top:1px; }
body.dark-mobile .odds-spread { color:var(--text); }
body.dark-mobile .odds-ou { color:var(--text3); }
@media(max-width:768px){
    .odds-col, .td-odds { display:none !important; }
}
"""
html = html.replace('</style>', odds_css + '</style>')

# Add odds header to sport tables (after the score columns, before CH)
html = html.replace(
    """<th class="score-col"></th><th>Home</th>
                <th class="ch-col">Ch</th>""",
    """<th class="score-col"></th><th>Home</th>
                <th class="odds-col">Line</th>
                <th class="ch-col">Ch</th>"""
)

# Add odds cell to game rows (after home team, before channel)
# Find the home team td followed by the odds td (which we removed earlier)
# Now add it back
html = html.replace(
    """<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>
                <td class="td-ch">""",
    """<td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>
                <td class="td-odds"></td>
                <td class="td-ch">"""
)

# Add JS to fetch and display odds from cached JSON
odds_js = """
// ═══════════════════════════════════════════════════
// ODDS ENGINE (desktop only)
// ═══════════════════════════════════════════════════
(function(){
    if(window.innerWidth<=768) return; // Skip on mobile entirely

    function normOdds(t){
        return t.toLowerCase().replace(/[^a-z ]/g,' ').replace(/\\s+/g,' ').trim();
    }

    fetch('/sports/api/odds').then(function(r){return r.json()}).then(function(data){
        if(!data.odds) return;
        var odds=data.odds;
        var keys=Object.keys(odds);
        if(keys.length===0) return;

        document.querySelectorAll('.game-data').forEach(function(row){
            var away=row.getAttribute('data-away')||'';
            var home=row.getAttribute('data-home')||'';
            if(!away||!home) return;
            var na=normOdds(away);
            var nh=normOdds(home);

            for(var i=0;i<keys.length;i++){
                var o=odds[keys[i]];
                var oa=normOdds(o.away);
                var oh=normOdds(o.home);

                // Word match
                var awayMatch=na.split(' ').some(function(w){return w.length>2&&oa.indexOf(w)!==-1});
                var homeMatch=nh.split(' ').some(function(w){return w.length>2&&oh.indexOf(w)!==-1});

                if(awayMatch&&homeMatch){
                    var cell=row.querySelector('.td-odds');
                    if(cell){
                        var h='';
                        if(o.spread) h+='<div class="odds-spread">'+o.spread+'</div>';
                        if(o.overUnder) h+='<div class="odds-ou">O/U '+o.overUnder+'</div>';
                        cell.innerHTML=h;
                    }
                    break;
                }
            }
        });
    }).catch(function(){});
})();
"""

# Insert before closing </body>
html = html.replace('</body>', '<script>' + odds_js + '</script>\n</body>')

with open(TEMPLATE_PATH, 'w') as f:
    f.write(html)
print("✓ Updated template with desktop-only odds column")


# ── 3. Add API route to serve cached odds ──
# We need to add a route to sports.py
SPORTS_PY = 'sports_guide/sports.py'
with open(SPORTS_PY, 'r') as f:
    spy = f.read()

if '/api/odds' not in spy:
    # Find the last route and add after it
    odds_route = '''

@sports_bp.route('/sports/api/odds')
def api_odds():
    """Serve cached odds data."""
    import json, os
    odds_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'odds.json')
    if os.path.exists(odds_file):
        with open(odds_file, 'r') as f:
            return json.load(f)
    return {'odds': {}}
'''
    # Append to file
    with open(SPORTS_PY, 'a') as f:
        f.write(odds_route)
    print("✓ Added /sports/api/odds route to sports.py")
else:
    print("✓ /sports/api/odds route already exists")


# ── 4. Add odds fetch to scheduler ──
# Check if it's already in server.py
SERVER_PY = 'server.py'
with open(SERVER_PY, 'r') as f:
    srv = f.read()

if 'odds_fetcher' not in srv:
    # Add import
    srv = srv.replace(
        'from sports_guide.fanzo_scraper import scrape_fanzo_guide',
        'from sports_guide.fanzo_scraper import scrape_fanzo_guide\nfrom sports_guide.odds_fetcher import fetch_odds'
    )
    # Add scheduler job - run 3x/day (6am, 12pm, 6pm)
    if 'setup_scheduler' in srv:
        srv = srv.replace(
            "id='fanzo_scrape'",
            "id='fanzo_scrape')\n    scheduler.add_job(fetch_odds, 'cron', hour='6,12,18', id='odds_fetch'"
        )
    with open(SERVER_PY, 'w') as f:
        f.write(srv)
    print("✓ Added odds fetch to scheduler (6am, 12pm, 6pm)")
else:
    print("✓ Odds fetcher already in server.py")

print("\n✅ Odds integration complete!")
print("\nNEXT STEPS:")
print("1. Get free API key at https://the-odds-api.com/#get-access")
print("2. Add to /opt/rednun/.env: ODDS_API_KEY=your_key_here")
print("3. Test: cd /opt/rednun && /opt/rednun/venv/bin/python3 -m sports_guide.odds_fetcher")
print("4. Restart gunicorn")
print("\nOdds column visible on desktop only — hidden on mobile/iPad via CSS")

# Restart
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted gunicorn")
