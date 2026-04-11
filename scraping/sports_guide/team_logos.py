
# Team and league logo mappings for sports guide
# ESPN CDN URLs

LEAGUE_LOGOS = {
    "favorites": "/sports/static/star-gold.svg",
    "streaming": "/sports/static/streaming-icon.svg",
    "mlb baseball": "https://a.espncdn.com/i/teamlogos/leagues/500/mlb.png",
    "nba basketball": "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png",
    "nhl hockey": "https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png",
    "ncaa hockey": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "nfl football": "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png",
    "ncaa basketball – men\u2019s": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "ncaa basketball – women\u2019s": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "ncaa baseball": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "ncaa football": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "nbagl basketball": "https://a.espncdn.com/i/teamlogos/leagues/500/nba-g-league.png",
    "golf": "https://a.espncdn.com/i/teamlogos/leagues/500/pga.png",
    "nascar auto racing": "https://a.espncdn.com/i/teamlogos/leagues/500/nascar.png",
    "softball": "https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
    "boxing": "https://a.espncdn.com/i/teamlogos/leagues/500/pbc.png",
    "olympics": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5c/Olympic_rings_without_rims.svg/200px-Olympic_rings_without_rims.svg.png",
    "soccer": "https://a.espncdn.com/i/teamlogos/leagues/500/mls.png",
}

# Pro team name fragments -> (league, abbreviation)
# ESPN CDN: https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png
PRO_TEAMS = {
    # NBA
    "Hawks": ("nba", "atl"), "Celtics": ("nba", "bos"), "Nets": ("nba", "bkn"),
    "Hornets": ("nba", "cha"), "Bulls": ("nba", "chi"), "Cavaliers": ("nba", "cle"),
    "Mavericks": ("nba", "dal"), "Nuggets": ("nba", "den"), "Pistons": ("nba", "det"),
    "Warriors": ("nba", "gs"), "Rockets": ("nba", "hou"), "Pacers": ("nba", "ind"),
    "Clippers": ("nba", "lac"), "Lakers": ("nba", "lal"), "Grizzlies": ("nba", "mem"),
    "Heat": ("nba", "mia"), "Bucks": ("nba", "mil"), "Timberwolves": ("nba", "min"),
    "Pelicans": ("nba", "no"), "Knicks": ("nba", "ny"), "Thunder": ("nba", "okc"),
    "Magic": ("nba", "orl"), "76ers": ("nba", "phi"), "Suns": ("nba", "phx"),
    "Trail Blazers": ("nba", "por"), "Blazers": ("nba", "por"),
    "Kings": ("nba", "sac"), "Spurs": ("nba", "sa"),
    "Raptors": ("nba", "tor"), "Jazz": ("nba", "utah"), "Wizards": ("nba", "wsh"),
    # MLB
    "Diamondbacks": ("mlb", "ari"), "D-backs": ("mlb", "ari"),
    "Braves": ("mlb", "atl"), "Orioles": ("mlb", "bal"),
    "Red Sox": ("mlb", "bos"), "Cubs": ("mlb", "chc"), "White Sox": ("mlb", "chw"),
    "Reds": ("mlb", "cin"), "Guardians": ("mlb", "cle"), "Rockies": ("mlb", "col"),
    "Tigers": ("mlb", "det"), "Astros": ("mlb", "hou"), "Royals": ("mlb", "kc"),
    "Angels": ("mlb", "laa"), "Dodgers": ("mlb", "lad"), "Marlins": ("mlb", "mia"),
    "Brewers": ("mlb", "mil"), "Twins": ("mlb", "min"), "Mets": ("mlb", "nym"),
    "Yankees": ("mlb", "nyy"), "Athletics": ("mlb", "oak"),
    "Phillies": ("mlb", "phi"), "Pirates": ("mlb", "pit"), "Padres": ("mlb", "sd"),
    "Giants": ("mlb", "sf"), "Mariners": ("mlb", "sea"), "Cardinals": ("mlb", "stl"),
    "Rays": ("mlb", "tb"), "Rangers": ("mlb", "tex"), "Blue Jays": ("mlb", "tor"),
    "Nationals": ("mlb", "wsh"),
    # NHL
    "Ducks": ("nhl", "ana"), "Coyotes": ("nhl", "ari"), "Bruins": ("nhl", "bos"),
    "Sabres": ("nhl", "buf"), "Flames": ("nhl", "cgy"), "Hurricanes": ("nhl", "car"),
    "Blackhawks": ("nhl", "chi"), "Avalanche": ("nhl", "col"),
    "Blue Jackets": ("nhl", "cbj"), "Stars": ("nhl", "dal"),
    "Red Wings": ("nhl", "det"), "Oilers": ("nhl", "edm"),
    "Panthers": ("nhl", "fla"), "Kraken": ("nhl", "sea"),
    "Wild": ("nhl", "min"), "Canadiens": ("nhl", "mtl"),
    "Predators": ("nhl", "nsh"), "Devils": ("nhl", "njd"),
    "Islanders": ("nhl", "nyi"), "Rangers": ("nhl", "nyr"),
    "Senators": ("nhl", "ott"), "Flyers": ("nhl", "phi"),
    "Penguins": ("nhl", "pit"), "Sharks": ("nhl", "sj"),
    "Blues": ("nhl", "stl"), "Lightning": ("nhl", "tb"),
    "Maple Leafs": ("nhl", "tor"), "Canucks": ("nhl", "van"),
    "Golden Knights": ("nhl", "vgk"), "Capitals": ("nhl", "wsh"),
    "Jets": ("nhl", "wpg"), "Utah Hockey Club": ("nhl", "utah"),
    # NFL
    "Cardinals": ("nfl", "ari"), "Falcons": ("nfl", "atl"), "Ravens": ("nfl", "bal"),
    "Bills": ("nfl", "buf"), "Panthers": ("nfl", "car"), "Bears": ("nfl", "chi"),
    "Bengals": ("nfl", "cin"), "Browns": ("nfl", "cle"), "Cowboys": ("nfl", "dal"),
    "Broncos": ("nfl", "den"), "Lions": ("nfl", "det"), "Packers": ("nfl", "gb"),
    "Texans": ("nfl", "hou"), "Colts": ("nfl", "ind"), "Jaguars": ("nfl", "jax"),
    "Chiefs": ("nfl", "kc"), "Chargers": ("nfl", "lac"), "Rams": ("nfl", "lar"),
    "Dolphins": ("nfl", "mia"), "Vikings": ("nfl", "min"),
    "Patriots": ("nfl", "ne"), "Saints": ("nfl", "no"),
    "Commanders": ("nfl", "wsh"), "Eagles": ("nfl", "phi"),
    "Steelers": ("nfl", "pit"), "49ers": ("nfl", "sf"),
    "Seahawks": ("nfl", "sea"), "Buccaneers": ("nfl", "tb"),
    "Titans": ("nfl", "ten"),
}

# NCAA team name fragments -> ESPN numeric ID
# ESPN CDN: https://a.espncdn.com/i/teamlogos/ncaa/500/{id}.png
NCAA_TEAMS = {
    "Alabama": 333, "Arizona": 12, "Arizona St": 9, "Arkansas": 8,
    "Auburn": 2, "Baylor": 239, "Boston College": 103, "Boston Col.": 103,
    "BYU": 252, "Cal": 25, "California": 25, "Cincinnati": 2132,
    "Clemson": 228, "Colorado": 38, "UConn": 41, "Connecticut": 41,
    "Creighton": 156, "Dartmouth": 159, "Delaware": 48, "Duke": 150,
    "E. Illinois": 2197, "Florida": 57, "Florida Atlantic": 2226,
    "Florida Int.": 2229, "Florida St": 52, "Georgetown": 46,
    "Georgia": 61, "Georgia Tech": 59, "Gonzaga": 2250,
    "Harvard": 108, "High Point": 2314, "Houston": 248,
    "Illinois": 356, "Indiana": 84, "Iowa": 2294, "Iowa St": 66,
    "Jacksonville St": 55, "Kansas": 2305, "Kansas St": 2306,
    "Kentucky": 96, "LSU": 99, "Louisville": 97,
    "Marquette": 269, "Maryland": 120, "Memphis": 235,
    "Miami": 2390, "Michigan": 130, "Michigan St": 127,
    "Minnesota": 135, "Mississippi St": 344, "Missouri": 142,
    "NC State": 152, "Nebraska": 158, "North Carolina": 153,
    "Northwestern": 77, "Notre Dame": 87, "Ohio St": 194,
    "Oklahoma": 201, "Oklahoma St": 197, "Ole Miss": 145,
    "Oregon": 2483, "Oregon St": 204, "Penn St": 213,
    "Pittsburgh": 221, "Presbyterian": 2506, "Providence": 2507,
    "Purdue": 2509, "Rice": 242, "Rutgers": 164,
    "Sam Houston St": 2534, "Sam Houston State": 2534,
    "Seton Hall": 2550, "SMU": 2567, "South Carolina": 2579,
    "Southern Illinois": 79, "St. John's": 2599, "St. Mary's": 2608,
    "Stanford": 24, "Syracuse": 183, "TCU": 2628,
    "Temple": 218, "Tennessee": 2633, "Texas": 251,
    "Texas A&M": 245, "Texas Tech": 2641, "Tulane": 2655,
    "UCF": 2116, "UCLA": 26, "UMass": 113,
    "UNC": 153, "UNLV": 2439, "USC": 30,
    "UTSA": 2636, "Vanderbilt": 238, "Villanova": 2918,
    "Virginia": 258, "Virginia Tech": 259, "Wake Forest": 154,
    "Washington": 264, "Washington St": 265, "West Virginia": 277,
    "Wichita St": 2724, "Wichita State": 2724, "Wisconsin": 275,
    "Xavier": 2752, "Charlotte": 2429, "Bradley": 71,
    "Abilene Christian": 2000, "Texas Tech": 2641,
    "Colorado St": 36, "(1)": None, "(2)": None, "(3)": None,
}

def get_pro_team_logo(name):
    """Given a team name string, return ESPN logo URL or None."""
    if not name:
        return None
    for fragment, (league, abbr) in PRO_TEAMS.items():
        if fragment.lower() in name.lower():
            return f"https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png"
    return None

def get_ncaa_team_logo(name):
    """Given an NCAA team name, return ESPN logo URL or None."""
    if not name:
        return None
    # Strip common prefixes
    clean = name
    for prefix in ["NCAA: ", "(W)NCAA: ", "NCAAW: ", "NCAAM: "]:
        clean = clean.replace(prefix, "")
    # Strip rankings like (13) or (2)
    import re
    clean = re.sub(r'^\(\d+\)', '', clean).strip()
    for fragment, espn_id in NCAA_TEAMS.items():
        if espn_id and fragment.lower() in clean.lower():
            return f"https://a.espncdn.com/i/teamlogos/ncaa/500/{espn_id}.png"
    return None

def get_league_logo(section_name):
    """Given a section name, return league logo URL or None."""
    name = section_name.lower()
    for key, url in LEAGUE_LOGOS.items():
        if key in name:
            return url
    return None
