"""
ESPN Odds Fetcher — free replacement for The Odds API.
Fetches spread and O/U from ESPN's core API (DraftKings source).
Falls back to The Odds API for UFC only.
"""
import json, os, logging, urllib.request, time
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_FILE = '/opt/rednun/data/odds.json'

ESPN_LEAGUES = [
    ("football", "nfl"),
    ("basketball", "nba"),
    ("hockey", "nhl"),
    ("baseball", "mlb"),
    ("football", "college-football"),
    ("basketball", "mens-college-basketball"),
    ("basketball", "womens-college-basketball"),
    ("soccer", "usa.1"),
]

def fetch_espn_odds():
    """Fetch odds from ESPN's free API for all supported leagues."""
    all_odds = {}

    for sport, league in ESPN_LEAGUES:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={datetime.now().strftime('%Y%m%d')}"
            if "college" in league:
                url += "&groups=50"

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            events = data.get("events", [])

            for ev in events:
                eid = ev["id"]
                odds_url = f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{eid}/competitions/{eid}/odds"

                try:
                    req2 = urllib.request.Request(odds_url, headers={"User-Agent": "Mozilla/5.0"})
                    resp2 = urllib.request.urlopen(req2, timeout=10)
                    odds_data = json.loads(resp2.read())

                    items = odds_data.get("items", [])
                    if not items:
                        continue

                    item = items[0]
                    details = item.get("details", "")
                    over_under = item.get("overUnder", "")
                    provider = item.get("provider", {}).get("name", "")

                    competitors = ev.get("competitions", [{}])[0].get("competitors", [])
                    home_team = ""
                    away_team = ""
                    for c in competitors:
                        name = c.get("team", {}).get("displayName", "")
                        if c.get("homeAway") == "home":
                            home_team = name
                        else:
                            away_team = name

                    if details or over_under:
                        key = f"{away_team} vs {home_team}"
                        all_odds[key] = {
                            "spread": details,
                            "overUnder": float(over_under) if over_under else None,
                            "provider": provider,
                            "home": home_team,
                            "away": away_team,
                            "league": league,
                            "event_id": eid,
                            "source": "espn",
                        }
                except Exception:
                    continue

                time.sleep(0.05)

            time.sleep(0.3)
            logger.info(f"ESPN odds: {league} — {len([v for v in all_odds.values() if v['league']==league])} games with odds")

        except Exception as e:
            logger.error(f"ESPN odds error for {league}: {e}")
            continue

    return all_odds


def fetch_odds_api_ufc():
    """Fallback: fetch UFC odds from The Odds API (uses ~2 requests)."""
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return {}

    odds = {}
    try:
        url = f"https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/?apiKey={api_key}&regions=us&markets=h2h&oddsFormat=american"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        for game in data:
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                continue

            bk = bookmakers[0]
            for market in bk.get("markets", []):
                if market["key"] == "h2h":
                    outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                    home_ml = outcomes.get(home, "")
                    away_ml = outcomes.get(away, "")
                    spread_str = ""
                    if home_ml and away_ml:
                        fav = home if int(home_ml) < int(away_ml) else away
                        spread_str = f"{fav} ML {min(int(home_ml), int(away_ml)):+d}"

                    key = f"{away} vs {home}"
                    odds[key] = {
                        "spread": spread_str,
                        "overUnder": None,
                        "provider": bk.get("title", ""),
                        "home": home,
                        "away": away,
                        "league": "ufc",
                        "source": "odds-api",
                    }

        logger.info(f"Odds API: UFC — {len(odds)} fights with odds")
    except Exception as e:
        logger.error(f"Odds API UFC error: {e}")

    return odds


def fetch_all_odds():
    """Main entry: ESPN for everything + Odds API for UFC only."""
    all_odds = fetch_espn_odds()

    # Add UFC from The Odds API
    ufc_odds = fetch_odds_api_ufc()
    all_odds.update(ufc_odds)

    output = {
        "updated": datetime.now().isoformat(),
        "sources": "ESPN (DraftKings) + The Odds API (UFC)",
        "count": len(all_odds),
        "games": all_odds,
    }

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)

    logger.info(f"Odds saved: {len(all_odds)} total games")
    return output


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv("/opt/rednun/.env")
    result = fetch_all_odds()
    print(f"\n✅ Fetched odds for {result['count']} games")
    for key, val in list(result['games'].items())[:8]:
        src = val.get('source', '?')
        print(f"  [{src}] {key}: {val['spread']} | O/U {val.get('overUnder','—')}")
