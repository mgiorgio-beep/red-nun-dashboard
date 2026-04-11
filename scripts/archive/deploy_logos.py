#!/usr/bin/env python3
"""
Deploy league logos (section headers) and team logos (game rows) to sports guide.
Run on droplet: cd /opt/rednun && /opt/rednun/venv/bin/python3 deploy_logos.py
"""

# ── Step 1: Create team_logos.py config ──────────────────────────────

TEAM_LOGOS_PY = r'''
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
'''

# ── Step 2: Write team_logos.py ──────────────────────────────────────

with open('sports_guide/team_logos.py', 'w') as f:
    f.write(TEAM_LOGOS_PY)
print("✓ Created sports_guide/team_logos.py")

# ── Step 3: Create SVG icons for Favorites and Streaming ─────────────

import os
os.makedirs('sports_guide/static', exist_ok=True)

with open('sports_guide/static/star-gold.svg', 'w') as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#D4A843"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>')

with open('sports_guide/static/streaming-icon.svg', 'w') as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#3B82F6"><path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zm0 8c-1.65 0-3-1.35-3-3s1.35-3 3-3 3 1.35 3 3-1.35 3-3 3z"/><path d="M6.34 17.66A8.96 8.96 0 013 12c0-2.39.94-4.63 2.63-6.32l1.41 1.41A6.96 6.96 0 005 12c0 1.86.73 3.6 2.04 4.91l-1.41 1.41-.29-.66zM17.66 6.34A8.96 8.96 0 0121 12c0 2.39-.94 4.63-2.63 6.32l-1.41-1.41A6.96 6.96 0 0019 12c0-1.86-.73-3.6-2.04-4.91l1.41-1.41.29.66z"/></svg>')

print("✓ Created static SVG icons")

# ── Step 4: Patch the template ───────────────────────────────────────

with open('sports_guide/templates/sports_guide.html', 'r') as f:
    html = f.read()

# 4a: Add CSS for logos
logo_css = """
        .section-logo { width: 24px; height: 24px; margin-right: 10px; object-fit: contain; vertical-align: middle; }
        .team-logo { width: 18px; height: 18px; object-fit: contain; vertical-align: middle; margin-right: 5px; }
        .team-logo-detail { width: 18px; height: 18px; object-fit: contain; vertical-align: middle; margin-right: 5px; }
"""
html = html.replace(
    '.section-icon { font-size: 20px; margin-right: 10px; }',
    '.section-icon { font-size: 20px; margin-right: 10px; display: none; }\n' + logo_css
)

# 4b: Fix the broken script tag at bottom
html = html.replace("""    <script>
    }
    </script>""", "")

# 4c: Add the JavaScript team logo engine before </body>
team_logo_js = """
    <script>
    // Team logo engine
    (function() {
        var PRO = {
            // NBA
            "hawks":"nba/atl","celtics":"nba/bos","nets":"nba/bkn","hornets":"nba/cha",
            "bulls":"nba/chi","cavaliers":"nba/cle","mavericks":"nba/dal","nuggets":"nba/den",
            "pistons":"nba/det","warriors":"nba/gs","rockets":"nba/hou","pacers":"nba/ind",
            "clippers":"nba/lac","lakers":"nba/lal","grizzlies":"nba/mem","heat":"nba/mia",
            "bucks":"nba/mil","timberwolves":"nba/min","pelicans":"nba/no","knicks":"nba/ny",
            "thunder":"nba/okc","magic":"nba/orl","76ers":"nba/phi","suns":"nba/phx",
            "trail blazers":"nba/por","blazers":"nba/por","kings":"nba/sac","spurs":"nba/sa",
            "raptors":"nba/tor","jazz":"nba/utah","wizards":"nba/wsh",
            // MLB
            "diamondbacks":"mlb/ari","d-backs":"mlb/ari","braves":"mlb/atl","orioles":"mlb/bal",
            "red sox":"mlb/bos","cubs":"mlb/chc","white sox":"mlb/chw","reds":"mlb/cin",
            "guardians":"mlb/cle","rockies":"mlb/col","tigers":"mlb/det","astros":"mlb/hou",
            "royals":"mlb/kc","angels":"mlb/laa","dodgers":"mlb/lad","marlins":"mlb/mia",
            "brewers":"mlb/mil","twins":"mlb/min","mets":"mlb/nym","yankees":"mlb/nyy",
            "athletics":"mlb/oak","phillies":"mlb/phi","pirates":"mlb/pit","padres":"mlb/sd",
            "giants":"mlb/sf","mariners":"mlb/sea","cardinals":"mlb/stl","rays":"mlb/tb",
            "rangers":"mlb/tex","blue jays":"mlb/tor","nationals":"mlb/wsh",
            // NHL
            "ducks":"nhl/ana","coyotes":"nhl/ari","bruins":"nhl/bos","sabres":"nhl/buf",
            "flames":"nhl/cgy","hurricanes":"nhl/car","blackhawks":"nhl/chi","avalanche":"nhl/col",
            "blue jackets":"nhl/cbj","stars":"nhl/dal","red wings":"nhl/det","oilers":"nhl/edm",
            "panthers":"nhl/fla","kraken":"nhl/sea","wild":"nhl/min","canadiens":"nhl/mtl",
            "predators":"nhl/nsh","devils":"nhl/njd","islanders":"nhl/nyi","rangers":"nhl/nyr",
            "senators":"nhl/ott","flyers":"nhl/phi","penguins":"nhl/pit","sharks":"nhl/sj",
            "blues":"nhl/stl","lightning":"nhl/tb","maple leafs":"nhl/tor","canucks":"nhl/van",
            "golden knights":"nhl/vgk","capitals":"nhl/wsh","jets":"nhl/wpg",
            // NFL
            "falcons":"nfl/atl","ravens":"nfl/bal","bills":"nfl/buf","bears":"nfl/chi",
            "bengals":"nfl/cin","browns":"nfl/cle","cowboys":"nfl/dal","broncos":"nfl/den",
            "lions":"nfl/det","packers":"nfl/gb","texans":"nfl/hou","colts":"nfl/ind",
            "jaguars":"nfl/jax","chiefs":"nfl/kc","chargers":"nfl/lac","rams":"nfl/lar",
            "dolphins":"nfl/mia","vikings":"nfl/min","patriots":"nfl/ne","saints":"nfl/no",
            "commanders":"nfl/wsh","eagles":"nfl/phi","steelers":"nfl/pit","49ers":"nfl/sf",
            "seahawks":"nfl/sea","buccaneers":"nfl/tb","titans":"nfl/ten"
        };
        var NCAA = {
            "alabama":333,"arizona":12,"arizona st":9,"arkansas":8,"auburn":2,
            "baylor":239,"boston college":103,"boston col.":103,"byu":252,
            "cal":25,"california":25,"charlotte":2429,"cincinnati":2132,
            "clemson":228,"colorado":38,"uconn":41,"connecticut":41,
            "creighton":156,"dartmouth":159,"delaware":48,"duke":150,
            "e. illinois":2197,"florida":57,"florida atlantic":2226,
            "florida int.":2229,"florida int":2229,"florida st":52,
            "georgetown":46,"georgia":61,"georgia tech":59,"gonzaga":2250,
            "harvard":108,"high point":2314,"houston":248,
            "illinois":356,"indiana":84,"iowa":2294,"iowa st":66,
            "kansas":2305,"kansas st":2306,"kentucky":96,
            "lsu":99,"louisville":97,"marquette":269,"maryland":120,
            "memphis":235,"miami":2390,"michigan":130,"michigan st":127,
            "minnesota":135,"mississippi st":344,"missouri":142,
            "nc state":152,"nebraska":158,"north carolina":153,
            "northwestern":77,"notre dame":87,"ohio st":194,
            "oklahoma":201,"oklahoma st":197,"ole miss":145,
            "oregon":2483,"oregon st":204,"penn st":213,
            "pittsburgh":221,"presbyterian":2506,"providence":2507,
            "purdue":2509,"rice":242,"rutgers":164,
            "sam houston":2534,"seton hall":2550,"smu":2567,
            "south carolina":2579,"southern illinois":79,
            "st. john's":2599,"st. mary's":2608,"stanford":24,
            "syracuse":183,"tcu":2628,"temple":218,
            "tennessee":2633,"texas":251,"texas a&m":245,
            "texas tech":2641,"tulane":2655,"ucf":2116,
            "ucla":26,"umass":113,"unc":153,"unlv":2439,"usc":30,
            "utsa":2636,"vanderbilt":238,"villanova":2918,
            "virginia":258,"virginia tech":259,"wake forest":154,
            "washington":264,"washington st":265,"west virginia":277,
            "wichita st":2724,"wichita state":2724,"wisconsin":275,
            "xavier":2752,"bradley":71,"abilene christian":2000,
            "colorado st":36,"iowa state":66,"michigan state":127,
            "ohio state":194,"oklahoma state":197,"penn state":213,
            "texas a\\u0026m":245,"go-go":null,"knicks":null
        };
        // NBAGL / G-League teams
        var NBAGL = {
            "go-go":"nba-g-league/cggo","blue coats":"nba-g-league/dcbc",
            "charge":"nba-g-league/clvc","gold":"nba-g-league/ergd",
            "mad ants":"nba-g-league/fwma","vipers":"nba-g-league/rgvv",
            "hustle":"nba-g-league/memh","skyhawks":"nba-g-league/cpsk",
            "swarm":"nba-g-league/gbsw","herd":"nba-g-league/wih",
            "wolves":"nba-g-league/iaw","legends":"nba-g-league/txl",
            "ignite":"nba-g-league/nign","stars":"nba-g-league/slcs",
            "squadron":"nba-g-league/bhsq","raptors 905":"nba-g-league/rap9",
            "westchester knicks":"nba-g-league/wck","capital city":"nba-g-league/cggo",
            "long island nets":"nba-g-league/lin","maine celtics":"nba-g-league/mec",
            "windy city":"nba-g-league/wcb","lakeland magic":"nba-g-league/lkm",
            "osceola magic":"nba-g-league/lkm","santa cruz":"nba-g-league/scw",
            "south bay":"nba-g-league/sbl","stockton":"nba-g-league/stk",
            "austin spurs":"nba-g-league/asp","sioux falls":"nba-g-league/sfs",
            "mexico city":"nba-g-league/mxcc","cleveland charge":"nba-g-league/clvc",
            "greensboro":"nba-g-league/gbsw","birmingham":"nba-g-league/bhsq",
            "rio grande":"nba-g-league/rgvv","delaware":"nba-g-league/dcbc",
            "fort wayne":"nba-g-league/fwma","iowa wolves":"nba-g-league/iaw",
            "rip city":"nba-g-league/rcr","valley suns":"nba-g-league/vps",
            "wisconsin herd":"nba-g-league/wih","indiana mad ants":"nba-g-league/fwma"
        };
        var LEAGUE_LOGOS = {
            "mlb baseball":"https://a.espncdn.com/i/teamlogos/leagues/500/mlb.png",
            "nba basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/nba.png",
            "nhl hockey":"https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png",
            "ncaa hockey":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
            "nfl football":"https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png",
            "ncaa basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
            "ncaa baseball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
            "ncaa football":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
            "nbagl basketball":"https://a.espncdn.com/i/teamlogos/leagues/500/nba-g-league.png",
            "golf":"https://a.espncdn.com/i/teamlogos/leagues/500/pga.png",
            "nascar auto racing":"https://a.espncdn.com/i/teamlogos/leagues/500/nascar.png",
            "softball":"https://a.espncdn.com/i/teamlogos/leagues/500/ncaa.png",
            "boxing":"https://a.espncdn.com/i/teamlogos/leagues/500/pbc.png",
            "soccer":"https://a.espncdn.com/i/teamlogos/leagues/500/mls.png"
        };

        function findProLogo(text) {
            var t = text.toLowerCase();
            for (var name in PRO) {
                if (t.indexOf(name) !== -1) {
                    return "https://a.espncdn.com/i/teamlogos/" + PRO[name] + ".png";
                }
            }
            return null;
        }
        function findNcaaLogo(text) {
            var t = text.toLowerCase().replace(/^\(w\)/,'').replace(/^ncaa[a-z]*:\s*/i,'').replace(/^\(\d+\)/,'').trim();
            // Try longest matches first
            var keys = Object.keys(NCAA).sort(function(a,b){return b.length-a.length;});
            for (var i=0; i<keys.length; i++) {
                if (NCAA[keys[i]] && t.indexOf(keys[i]) !== -1) {
                    return "https://a.espncdn.com/i/teamlogos/ncaa/500/" + NCAA[keys[i]] + ".png";
                }
            }
            return null;
        }
        function findNbaglLogo(text) {
            var t = text.toLowerCase();
            for (var name in NBAGL) {
                if (NBAGL[name] && t.indexOf(name) !== -1) {
                    return "https://a.espncdn.com/i/teamlogos/" + NBAGL[name] + ".png";
                }
            }
            return null;
        }
        function makeImg(url, cls) {
            var img = document.createElement('img');
            img.src = url;
            img.className = cls;
            img.loading = 'lazy';
            img.onerror = function(){ this.style.display='none'; };
            return img;
        }

        // Apply league logos to section headers
        document.querySelectorAll('.section-header').forEach(function(hdr) {
            var title = hdr.querySelector('.section-title');
            if (!title) return;
            var name = title.textContent.trim().toLowerCase();
            // Check for LOCAL GAMES (favorites)
            if (name === 'local games') {
                var icon = hdr.querySelector('.section-icon');
                if (icon) icon.style.display = 'inline';
                return;
            }
            if (name === 'streaming') {
                var icon = hdr.querySelector('.section-icon');
                if (icon) icon.style.display = 'inline';
                return;
            }
            for (var key in LEAGUE_LOGOS) {
                if (name.indexOf(key) !== -1 || key.indexOf(name.replace(/[^a-z ]/g,'').trim()) !== -1) {
                    var img = makeImg(LEAGUE_LOGOS[key], 'section-logo');
                    var iconEl = hdr.querySelector('.section-icon');
                    if (iconEl) {
                        iconEl.parentNode.insertBefore(img, iconEl);
                    }
                    break;
                }
            }
            // Also try matching without special chars
            if (!hdr.querySelector('.section-logo')) {
                var clean = name.replace(/[^a-z ]/g,' ').replace(/\s+/g,' ').trim();
                for (var key in LEAGUE_LOGOS) {
                    if (clean.indexOf(key) !== -1) {
                        var img = makeImg(LEAGUE_LOGOS[key], 'section-logo');
                        var iconEl = hdr.querySelector('.section-icon');
                        if (iconEl) iconEl.parentNode.insertBefore(img, iconEl);
                        break;
                    }
                }
            }
        });

        // Determine section type for each table
        document.querySelectorAll('.section').forEach(function(sec) {
            var titleEl = sec.querySelector('.section-title');
            if (!titleEl) return;
            var secName = titleEl.textContent.trim().toLowerCase();
            var isNcaa = secName.indexOf('ncaa') !== -1 || secName.indexOf('softball') !== -1;
            var isNbagl = secName.indexOf('nbagl') !== -1 || secName.indexOf('g-league') !== -1 || secName.indexOf('g league') !== -1;

            sec.querySelectorAll('.event-cell').forEach(function(cell) {
                var text = cell.textContent.trim();
                var logo = null;
                if (isNbagl) logo = findNbaglLogo(text) || findProLogo(text);
                else if (isNcaa) logo = findNcaaLogo(text);
                else logo = findProLogo(text);
                if (logo) cell.insertBefore(makeImg(logo, 'team-logo'), cell.firstChild);
            });
            sec.querySelectorAll('.detail-cell, .detail-team').forEach(function(cell) {
                var text = cell.textContent.trim();
                if (!text) return;
                var logo = null;
                if (isNbagl) logo = findNbaglLogo(text) || findProLogo(text);
                else if (isNcaa) logo = findNcaaLogo(text);
                else logo = findProLogo(text);
                if (logo) cell.insertBefore(makeImg(logo, 'team-logo-detail'), cell.firstChild);
            });
        });
    })();
    </script>
"""

html = html.replace('</body>', team_logo_js + '\n</body>')

with open('sports_guide/templates/sports_guide.html', 'w') as f:
    f.write(html)

print("✓ Updated sports_guide.html with logo CSS + JS engine")

# ── Step 5: Restart gunicorn ─────────────────────────────────────────
import subprocess
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
import time; time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted gunicorn")
print("\nDone! Refresh https://dashboard.rednun.com/guide to see logos.")
