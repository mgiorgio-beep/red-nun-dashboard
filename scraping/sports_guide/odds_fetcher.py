#!/usr/bin/env python3
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
