#!/usr/bin/env python3
"""
Deploy Mockup D template with live ESPN scores and betting odds.
Run on droplet: cd /opt/rednun && /opt/rednun/venv/bin/python3 deploy_mockup_d.py
"""

TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="RN Sports">
<link rel="manifest" href="/sports/static/manifest.json">
<link rel="apple-touch-icon" sizes="180x180" href="/sports/static/apple-touch-icon.png">
<meta name="theme-color" content="#C41E2A">
<title>Red Nun Sports Guide</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700;800&family=Barlow:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg: #F0F0F0;
    --card: #FFFFFF;
    --red: #C41E2A;
    --red-dark: #8B1A1A;
    --navy: #1B2838;
    --gold: #C89B3C;
    --gold-light: rgba(200,155,60,0.1);
    --green: #1B7340;
    --live-green: #00C853;
    --text: #1A1A1A;
    --text2: #5A5A5A;
    --text3: #9A9A9A;
    --border: #E5E5E5;
    --sat: env(safe-area-inset-top);
    --sab: env(safe-area-inset-bottom);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:'Barlow',-apple-system,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;min-height:100vh}

/* Header */
.header{
    position:sticky;top:0;z-index:100;
    background:var(--red);color:#fff;
    padding:calc(var(--sat) + 8px) 16px 8px;
}
.header-inner{display:flex;align-items:center;justify-content:space-between;max-width:700px;margin:0 auto}
.h-brand{display:flex;align-items:center;gap:10px}
.h-logo{
    border:2px solid rgba(255,255,255,0.4);border-radius:6px;
    padding:3px 8px;font-family:'Barlow Condensed',sans-serif;
    font-weight:800;font-size:16px;letter-spacing:1px;white-space:nowrap;
}
.h-title-wrap{}
.h-title{font-family:'Barlow Condensed',sans-serif;font-size:20px;font-weight:700;letter-spacing:2px;text-transform:uppercase}
.h-sub{font-size:10px;opacity:0.7;letter-spacing:0.5px}
.h-date-wrap{text-align:right}
.h-date{font-family:'Barlow Condensed',sans-serif;font-size:18px;font-weight:700;letter-spacing:1px}
.h-dt-sub{font-size:9px;opacity:0.6;letter-spacing:0.5px}

.content{max-width:700px;margin:0 auto;padding:12px 12px;padding-bottom:calc(var(--sab)+60px)}

/* Stale banner */
.stale-banner{background:#FFF3CD;color:#856404;padding:10px 16px;text-align:center;font-size:12px;font-weight:600;max-width:700px;margin:8px auto 0;border-radius:8px}

/* Section */
.section{margin-bottom:16px}
.sec-head{
    display:flex;align-items:center;gap:8px;
    padding:10px 12px;
    background:var(--navy);color:#fff;
    border-radius:10px 10px 0 0;
}
.sec-logo{width:20px;height:20px;object-fit:contain}
.sec-title{font-family:'Barlow Condensed',sans-serif;font-size:15px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
.sec-head-fav{background:linear-gradient(135deg,#B8860B,#DAA520)}
.sec-head-stream{background:linear-gradient(135deg,#1a3a5c,#2563EB)}

/* Table */
.game-table{
    width:100%;background:var(--card);
    border-radius:0 0 10px 10px;
    overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);
}
.game-table table{width:100%;border-collapse:collapse}
.game-table th{
    font-size:9px;font-weight:700;text-transform:uppercase;
    letter-spacing:1px;color:var(--text3);
    padding:8px 10px;text-align:left;
    border-bottom:2px solid var(--border);
    background:#FAFAFA;
}
.game-table th.ch-col{text-align:center;width:70px}
.game-table th.time-col{width:70px}
.game-table th.score-col{text-align:center;width:70px}
.game-table th.odds-col{text-align:center;width:60px}
.game-table th.vs-col{width:30px}
.game-table td{padding:10px;font-size:13px;vertical-align:middle;border-bottom:1px solid #F2F2F2}
.game-table tr:last-child td{border-bottom:none}

.td-time{font-weight:600;color:var(--text2);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.td-team{display:flex;align-items:center;gap:6px}
.td-team img{width:20px;height:20px;object-fit:contain;border-radius:4px;flex-shrink:0}
.td-team span{font-weight:600}
.td-vs{font-size:10px;color:var(--text3);text-align:center;font-weight:700;letter-spacing:1px}

.td-ch{text-align:center}
.ch-net{font-size:9px;color:var(--text3);font-weight:600;text-transform:uppercase}
.ch-num{font-family:'Barlow Condensed',sans-serif;font-size:22px;font-weight:800;color:var(--red);line-height:1}
.ch-badge{display:inline-block;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:700}
.ch-app{font-size:9px;color:var(--text3);margin-top:2px}
.ch-multi{margin-top:4px;padding-top:4px;border-top:1px solid var(--border)}

/* Score column */
.td-score{text-align:center;font-variant-numeric:tabular-nums}
.score-live{
    display:flex;flex-direction:column;align-items:center;gap:1px;
}
.score-num{font-family:'Barlow Condensed',sans-serif;font-size:16px;font-weight:800;line-height:1.2}
.score-away{color:var(--text2)}
.score-home{color:var(--text)}
.score-status{font-size:9px;color:var(--text3);font-weight:600;margin-top:2px}
.score-final{font-size:9px;color:var(--text3);font-weight:700;text-transform:uppercase}
.score-pre{font-size:10px;color:var(--text3)}

/* Live indicator */
.live-badge{
    display:inline-flex;align-items:center;gap:4px;
    background:#E8F5E9;color:#2E7D32;
    font-size:9px;font-weight:700;letter-spacing:0.5px;
    padding:2px 6px;border-radius:4px;
}
.live-dot{
    width:6px;height:6px;background:var(--live-green);
    border-radius:50%;animation:livePulse 1.5s infinite;
}
@keyframes livePulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.4;transform:scale(0.8)}}

/* Odds column */
.td-odds{text-align:center}
.odds-spread{font-size:11px;font-weight:600;color:var(--text)}
.odds-ou{font-size:9px;color:var(--text3);margin-top:1px}

/* Fav row */
tr.fav-row{background:linear-gradient(90deg,rgba(200,155,60,0.1),transparent)}
tr.fav-row td:first-child{border-left:3px solid var(--gold)}

/* Event rows (golf etc) */
.td-event{font-weight:600}
.td-detail{font-size:12px;color:var(--text2)}

/* Footer */
.footer{text-align:center;padding:20px 12px;max-width:700px;margin:0 auto}
.f-line{font-size:10px;color:var(--text3);display:flex;align-items:center;justify-content:center;gap:5px}
.f-dot{width:5px;height:5px;background:var(--green);border-radius:50%;animation:livePulse 2s infinite}
.f-note{font-size:9px;color:var(--text3);margin-top:6px}
{% if show_nav %}
.refresh-btn{background:transparent;border:1px solid var(--border);color:var(--text2);padding:6px 14px;border-radius:6px;font-size:12px;cursor:pointer;margin-top:8px}
.refresh-btn:hover{border-color:var(--red);color:var(--red)}
{% endif %}

/* No data */
.no-data{text-align:center;padding:80px 20px;color:var(--text2)}
.no-data h2{font-family:'Barlow Condensed',sans-serif;font-size:28px;color:var(--text);margin-bottom:8px}

@media(max-width:480px){
    .td-team img{width:16px;height:16px}
    .td-team span{font-size:12px}
    .td-time{font-size:11px}
    .ch-num{font-size:18px}
    .game-table td{padding:8px 6px}
    .game-table th{padding:6px 6px;font-size:8px}
    .sec-title{font-size:13px}
    .score-num{font-size:14px}
    .h-logo{font-size:14px;padding:2px 6px}
    .h-title{font-size:16px}
    .h-date{font-size:15px}
    .content{padding:8px 6px}
    .header-inner{gap:6px}
}
</style>
</head>
<body>

<div class="header">
    <div class="header-inner">
        <div class="h-brand">
            <div class="h-logo">RED NUN</div>
            <div class="h-title-wrap">
                <div class="h-title">Sports Guide</div>
                <div class="h-sub">Dennis Port & Chatham</div>
            </div>
        </div>
        <div class="h-date-wrap">
            {% if data %}<div class="h-date">{{ data.date }}</div>{% endif %}
            <div class="h-dt-sub">All Times ET</div>
        </div>
    </div>
</div>

{% if stale %}
<div class="stale-banner">&#9888; Guide may be outdated — last updated {{ data.updated_at[:10] if data else 'never' }}</div>
{% endif %}

{% if not data %}
<div class="no-data">
    <h2>Guide Unavailable</h2>
    <p>The sports guide hasn't been loaded yet. It updates automatically at 5:00 AM daily.</p>
</div>
{% else %}
<div class="content">

    {# ── SECTIONS LOOP ── #}
    {% for section in data.sections %}
    <div class="section" data-section="{{ section.name }}">
        <div class="sec-head{% if section.name|lower == 'favorites' %} sec-head-fav{% endif %}">
            <img class="sec-logo" src="" data-league="{{ section.name }}" onerror="this.style.display='none'" style="display:none">
            {% if section.name|lower == 'favorites' %}<span style="font-size:14px">⭐</span>{% endif %}
            <span class="sec-title">{% if section.name|lower == 'favorites' %}Local Games{% else %}{{ section.name|upper }}{% endif %}</span>
        </div>
        <div class="game-table"><table>
            <tr>
                <th class="time-col">Time</th>
                {% if section.name|lower in ['golf', 'nascar auto racing', 'olympics', 'other sports'] %}
                <th>Event</th><th>Detail</th>
                {% else %}
                <th>Away</th><th class="vs-col"></th><th>Home</th>
                <th class="score-col">Score</th>
                <th class="odds-col">Line</th>
                {% endif %}
                <th class="ch-col">Ch</th>
            </tr>
            {% for game in section.games %}
            <tr class="game-data{% if game.is_favorite %} fav-row{% endif %}"
                data-away="{{ game.event }}" data-home="{{ game.detail }}"
                data-time="{{ game.time }}" data-sport="{{ section.name }}">
                <td class="td-time">
                    <span class="game-time-text">{{ game.time }}</span>
                </td>
                {% if section.name|lower in ['golf', 'nascar auto racing', 'olympics', 'other sports'] %}
                <td class="td-event">{{ game.event }}</td>
                <td class="td-detail">{{ game.detail }}</td>
                {% else %}
                <td><div class="td-team" data-team="{{ game.event }}"><span>{{ game.event }}</span></div></td>
                <td class="td-vs">VS</td>
                <td><div class="td-team" data-team="{{ game.detail }}"><span>{{ game.detail }}</span></div></td>
                <td class="td-score"><span class="score-pre">—</span></td>
                <td class="td-odds"><span class="odds-spread">—</span></td>
                {% endif %}
                <td class="td-ch">
                    {% if game.streaming %}
                    <span class="ch-badge" style="background:{{ game.streaming.color }};color:#fff">{{ game.streaming.display }}</span>
                    <div class="ch-app">{{ game.streaming.app }}</div>
                    {% else %}
                    {% for ch in game.channels %}
                    <div{% if loop.index > 1 %} class="ch-multi"{% endif %}>
                        <div class="ch-net">{{ ch.name }}</div>
                        <div class="ch-num">{{ ch.drtv }}</div>
                    </div>
                    {% endfor %}
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table></div>
    </div>
    {% endfor %}

    <div class="footer">
        <div class="f-line">
            <span class="f-dot"></span>
            <span id="update-status">Auto-Updated · {{ data.updated_at[:16]|replace('T', ' ') }} ET</span>
        </div>
        <div class="f-note">Scores update every 45s · Streaming: ESPN+ · Peacock · Apple TV+ · Prime</div>
        {% if show_nav %}
        <button class="refresh-btn" onclick="refreshGuide()">&#8635; Refresh Schedule</button>
        {% endif %}
    </div>
</div>
{% endif %}

{% if show_nav %}
<script>
function refreshGuide(){
    var b=document.querySelector('.refresh-btn');b.textContent='Refreshing...';b.disabled=true;
    fetch('/sports/refresh',{method:'POST'}).then(r=>r.json()).then(d=>{
        if(d.status==='ok')window.location.reload();
        else{alert('Failed: '+d.message);b.textContent='Refresh Schedule';b.disabled=false}
    }).catch(()=>{alert('Failed.');b.textContent='Refresh Schedule';b.disabled=false});
}
</script>
{% endif %}

<script>
(function(){
// ═══════════════════════════════════════════════════
// TEAM LOGO ENGINE
// ═══════════════════════════════════════════════════
var PRO={
    "hawks":"nba/500/atl","celtics":"nba/500/bos","nets":"nba/500/bkn","hornets":"nba/500/cha",
    "bulls":"nba/500/chi","cavaliers":"nba/500/cle","cavs":"nba/500/cle",
    "mavericks":"nba/500/dal","mavs":"nba/500/dal","nuggets":"nba/500/den",
    "pistons":"nba/500/det","warriors":"nba/500/gs","rockets":"nba/500/hou","pacers":"nba/500/ind",
    "clippers":"nba/500/lac","lakers":"nba/500/lal","grizzlies":"nba/500/mem","heat":"nba/500/mia",
    "bucks":"nba/500/mil","timberwolves":"nba/500/min","pelicans":"nba/500/no","knicks":"nba/500/ny",
    "thunder":"nba/500/okc","magic":"nba/500/orl","76ers":"nba/500/phi","suns":"nba/500/phx",
    "trail blazers":"nba/500/por","blazers":"nba/500/por","kings":"nba/500/sac","spurs":"nba/500/sa",
    "raptors":"nba/500/tor","jazz":"nba/500/utah","wizards":"nba/500/wsh",
    "diamondbacks":"mlb/500/ari","d-backs":"mlb/500/ari","braves":"mlb/500/atl","orioles":"mlb/500/bal",
    "red sox":"mlb/500/bos","cubs":"mlb/500/chc","white sox":"mlb/500/chw","reds":"mlb/500/cin",
    "guardians":"mlb/500/cle","rockies":"mlb/500/col","tigers":"mlb/500/det","astros":"mlb/500/hou",
    "royals":"mlb/500/kc","angels":"mlb/500/laa","dodgers":"mlb/500/lad","marlins":"mlb/500/mia",
    "brewers":"mlb/500/mil","twins":"mlb/500/min","mets":"mlb/500/nym","yankees":"mlb/500/nyy",
    "athletics":"mlb/500/oak","phillies":"mlb/500/phi","pirates":"mlb/500/pit","padres":"mlb/500/sd",
    "giants":"mlb/500/sf","mariners":"mlb/500/sea","cardinals":"mlb/500/stl","rays":"mlb/500/tb",
    "rangers":"mlb/500/tex","blue jays":"mlb/500/tor","nationals":"mlb/500/wsh",
    "ducks":"nhl/500/ana","coyotes":"nhl/500/ari","bruins":"nhl/500/bos","sabres":"nhl/500/buf",
    "flames":"nhl/500/cgy","hurricanes":"nhl/500/car","blackhawks":"nhl/500/chi","avalanche":"nhl/500/col",
    "blue jackets":"nhl/500/cbj","stars":"nhl/500/dal","red wings":"nhl/500/det","oilers":"nhl/500/edm",
    "panthers":"nhl/500/fla","kraken":"nhl/500/sea","wild":"nhl/500/min","canadiens":"nhl/500/mtl",
    "predators":"nhl/500/nsh","devils":"nhl/500/njd","islanders":"nhl/500/nyi",
    "senators":"nhl/500/ott","flyers":"nhl/500/phi","penguins":"nhl/500/pit","sharks":"nhl/500/sj",
    "blues":"nhl/500/stl","lightning":"nhl/500/tb","maple leafs":"nhl/500/tor","canucks":"nhl/500/van",
    "golden knights":"nhl/500/vgk","capitals":"nhl/500/wsh","jets":"nhl/500/wpg",
    "falcons":"nfl/500/atl","ravens":"nfl/500/bal","bills":"nfl/500/buf","bears":"nfl/500/chi",
    "bengals":"nfl/500/cin","browns":"nfl/500/cle","cowboys":"nfl/500/dal","broncos":"nfl/500/den",
    "lions":"nfl/500/det","packers":"nfl/500/gb","texans":"nfl/500/hou","colts":"nfl/500/ind",
    "jaguars":"nfl/500/jax","chiefs":"nfl/500/kc","chargers":"nfl/500/lac","rams":"nfl/500/lar",
    "dolphins":"nfl/500/mia","vikings":"nfl/500/min","patriots":"nfl/500/ne","saints":"nfl/500/no",
    "commanders":"nfl/500/wsh","eagles":"nfl/500/phi","steelers":"nfl/500/pit","49ers":"nfl/500/sf",
    "seahawks":"nfl/500/sea","buccaneers":"nfl/500/tb","titans":"nfl/500/ten"
};
// NHL special: "Rangers" conflicts with MLB/NHL — context needed
var NCAA={
    "alabama":333,"arizona":12,"arizona st":9,"arkansas":8,"auburn":2,"baylor":239,
    "boston college":103,"boston col.":103,"byu":252,"cal":25,"charlotte":2429,
    "cincinnati":2132,"clemson":228,"colorado":38,"uconn":41,"creighton":156,
    "dartmouth":159,"delaware":48,"duke":150,"e. illinois":2197,"florida":57,
    "florida atlantic":2226,"florida int.":2229,"florida int":2229,"florida st":52,
    "georgetown":46,"georgia":61,"georgia tech":59,"gonzaga":2250,"harvard":108,
    "high point":2314,"houston":248,"illinois":356,"indiana":84,"iowa":2294,"iowa st":66,
    "iowa state":66,"jacksonville st":55,"kansas":2305,"kansas st":2306,"kentucky":96,
    "lsu":99,"louisville":97,"marquette":269,"maryland":120,"memphis":235,
    "miami":2390,"michigan":130,"michigan st":127,"michigan state":127,"minnesota":135,
    "mississippi st":344,"missouri":142,"nc state":152,"nebraska":158,"north carolina":153,
    "northwestern":77,"notre dame":87,"ohio st":194,"ohio state":194,"oklahoma":201,
    "oklahoma st":197,"ole miss":145,"oregon":2483,"oregon st":204,"penn st":213,
    "penn state":213,"pittsburgh":221,"presbyterian":2506,"providence":2507,
    "purdue":2509,"rice":242,"rutgers":164,"sam houston":2534,"sam houston state":2534,
    "seton hall":2550,"smu":2567,"south carolina":2579,"southern illinois":79,
    "st. john's":2599,"st. mary's":2608,"stanford":24,"syracuse":183,"tcu":2628,
    "temple":218,"tennessee":2633,"texas":251,"texas a&m":245,"texas tech":2641,
    "tulane":2655,"ucf":2116,"ucla":26,"umass":113,"unc":153,"unlv":2439,"usc":30,
    "utsa":2636,"vanderbilt":238,"villanova":2918,"virginia":258,"virginia tech":259,
    "wake forest":154,"washington":264,"washington st":265,"west virginia":277,
    "wichita st":2724,"wichita state":2724,"wisconsin":275,"xavier":2752,
    "bradley":71,"abilene christian":2000,"colorado st":36
};
var LEAGUE_LOGOS={
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
    "soccer":"https://a.espncdn.com/i/teamlogos/leagues/500/mls.png",
    "boxing":"https://a.espncdn.com/i/teamlogos/leagues/500/pbc.png"
};

function findLogo(text, isNcaa){
    var t=text.toLowerCase().replace(/^\(w\)/,'').replace(/^ncaa[a-z]*:\s*/i,'').replace(/^\(\d+\)/,'').trim();
    if(isNcaa){
        var keys=Object.keys(NCAA).sort(function(a,b){return b.length-a.length});
        for(var i=0;i<keys.length;i++){
            if(NCAA[keys[i]]&&t.indexOf(keys[i])!==-1)
                return "https://a.espncdn.com/i/teamlogos/ncaa/500/"+NCAA[keys[i]]+".png";
        }
        return null;
    }
    for(var name in PRO){
        if(t.indexOf(name)!==-1)
            return "https://a.espncdn.com/i/teamlogos/"+PRO[name]+".png";
    }
    return null;
}
function mkImg(url){
    var img=document.createElement('img');
    img.src=url;img.loading='lazy';
    img.style.cssText='width:20px;height:20px;object-fit:contain;border-radius:4px;flex-shrink:0';
    img.onerror=function(){this.style.display='none'};
    return img;
}

// Apply league logos
document.querySelectorAll('.sec-logo').forEach(function(el){
    var name=el.getAttribute('data-league').toLowerCase();
    for(var k in LEAGUE_LOGOS){
        if(name.indexOf(k)!==-1||k.indexOf(name.replace(/[^a-z ]/g,' ').trim())!==-1){
            el.src=LEAGUE_LOGOS[k];el.style.display='';break;
        }
    }
});

// Apply team logos
document.querySelectorAll('.td-team').forEach(function(el){
    var text=el.getAttribute('data-team')||el.textContent.trim();
    var sec=el.closest('[data-section]');
    var secName=sec?sec.getAttribute('data-section').toLowerCase():'';
    var isNcaa=secName.indexOf('ncaa')!==-1||secName.indexOf('softball')!==-1;
    var url=findLogo(text,isNcaa);
    if(url)el.insertBefore(mkImg(url),el.firstChild);
});

// ═══════════════════════════════════════════════════
// ESPN LIVE SCORES ENGINE
// ═══════════════════════════════════════════════════
var ESPN_MAP={
    'nba basketball':{sport:'basketball',league:'nba'},
    'mlb baseball':{sport:'baseball',league:'mlb'},
    'nhl hockey':{sport:'hockey',league:'nhl'},
    'nfl football':{sport:'football',league:'nfl'},
    "ncaa basketball – men's":{sport:'basketball',league:'mens-college-basketball'},
    "ncaa basketball – women's":{sport:'basketball',league:'womens-college-basketball'},
    'ncaa football':{sport:'football',league:'college-football'},
    'ncaa hockey':{sport:'hockey',league:'mens-college-hockey'},
    'ncaa baseball':{sport:'baseball',league:'college-baseball'},
    'softball':{sport:'softball',league:'college-softball'},
    'nbagl basketball':{sport:'basketball',league:'nba-g-league'},
};

// Normalize team name for matching
function normTeam(t){
    return t.toLowerCase()
        .replace(/^\(w\)ncaa:\s*/,'').replace(/^ncaa:\s*/,'')
        .replace(/^\(\d+\)/,'').replace(/[^a-z ]/g,' ').replace(/\s+/g,' ').trim();
}

function matchGame(espnGame, awayText, homeText){
    var ea=normTeam(espnGame.away);
    var eh=normTeam(espnGame.home);
    var ga=normTeam(awayText);
    var gh=normTeam(homeText);
    // Check if any word from our data matches ESPN
    var awayMatch=ga.split(' ').some(function(w){return w.length>2&&ea.indexOf(w)!==-1});
    var homeMatch=gh.split(' ').some(function(w){return w.length>2&&eh.indexOf(w)!==-1});
    return awayMatch&&homeMatch;
}

function fetchScores(){
    var sections=document.querySelectorAll('[data-section]');
    var fetched={};

    sections.forEach(function(sec){
        var secName=sec.getAttribute('data-section').toLowerCase();
        var espn=null;
        for(var k in ESPN_MAP){if(secName.indexOf(k)!==-1||k.indexOf(secName)!==-1){espn=ESPN_MAP[k];break;}}
        if(!espn)return;
        var key=espn.sport+'/'+espn.league;
        if(fetched[key])return;
        fetched[key]=true;

        var url='https://site.api.espn.com/apis/site/v2/sports/'+espn.sport+'/'+espn.league+'/scoreboard';
        fetch(url).then(function(r){return r.json()}).then(function(data){
            if(!data.events)return;
            var games=data.events.map(function(ev){
                var comp=ev.competitions[0];
                var away=comp.competitors.find(function(c){return c.homeAway==='away'});
                var home=comp.competitors.find(function(c){return c.homeAway==='home'});
                var odds=comp.odds&&comp.odds[0]?comp.odds[0]:null;
                return {
                    away:away?away.team.displayName:'',
                    home:home?home.team.displayName:'',
                    awayScore:away?away.score:'',
                    homeScore:home?home.score:'',
                    status:ev.status.type.name,
                    statusDetail:ev.status.type.shortDetail||ev.status.type.detail||'',
                    clock:ev.status.displayClock||'',
                    period:ev.status.period||0,
                    spread:odds?odds.details||'':'',
                    overUnder:odds?odds.overUnder||'':''
                };
            });
            updateRows(secName,games);
        }).catch(function(){});
    });
}

function updateRows(secName,espnGames){
    var sections=document.querySelectorAll('[data-section]');
    sections.forEach(function(sec){
        if(sec.getAttribute('data-section').toLowerCase().indexOf(secName)===-1&&
           secName.indexOf(sec.getAttribute('data-section').toLowerCase())===-1)return;

        sec.querySelectorAll('.game-data').forEach(function(row){
            var away=row.getAttribute('data-away')||'';
            var home=row.getAttribute('data-home')||'';
            if(!away||!home)return;

            for(var i=0;i<espnGames.length;i++){
                var eg=espnGames[i];
                if(matchGame(eg,away,home)){
                    var scoreCell=row.querySelector('.td-score');
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
                    }

                    if(oddsCell&&eg.spread){
                        oddsCell.innerHTML='<div class="odds-spread">'+eg.spread+'</div>'+
                            (eg.overUnder?'<div class="odds-ou">O/U '+eg.overUnder+'</div>':'');
                    }

                    espnGames.splice(i,1);
                    break;
                }
            }
        });
    });

    // Update timestamp
    var ts=document.getElementById('update-status');
    if(ts){
        var now=new Date();
        var h=now.getHours(),m=now.getMinutes();
        var ampm=h>=12?'PM':'AM';
        h=h%12||12;
        m=m<10?'0'+m:m;
        ts.textContent='Scores updated · '+h+':'+m+' '+ampm;
    }
}

// Initial fetch + poll every 45 seconds
fetchScores();
setInterval(fetchScores, 45000);

})();
</script>

<script>
if("serviceWorker" in navigator){
    navigator.serviceWorker.register("/sports/static/sw.js",{scope:"/sports/"})
    .then(r=>console.log("SW:",r.scope)).catch(e=>console.log("SW err:",e));
}
</script>
</body>
</html>'''

import os

# Write the template
os.makedirs('sports_guide/templates', exist_ok=True)

# Backup old template
if os.path.exists('sports_guide/templates/sports_guide.html'):
    import shutil
    shutil.copy2('sports_guide/templates/sports_guide.html',
                  'sports_guide/templates/sports_guide_backup.html')
    print("✓ Backed up old template")

with open('sports_guide/templates/sports_guide.html', 'w') as f:
    f.write(TEMPLATE)
print("✓ Wrote new Mockup D template with live scores")

# Restart gunicorn
import subprocess, time
subprocess.run(['pkill', '-f', 'gunicorn.*server:app'], capture_output=True)
time.sleep(2)
subprocess.Popen(
    ['/opt/rednun/venv/bin/gunicorn', '--bind', '127.0.0.1:8080',
     '--workers', '1', '--timeout', '120', 'server:app', '--daemon'],
    cwd='/opt/rednun'
)
print("✓ Restarted gunicorn")
print("\n✅ Done! Refresh https://dashboard.rednun.com/guide")
print("   - Mockup D design (light theme, red header, navy sections)")
print("   - Team & league logos from ESPN CDN")
print("   - Live scores from ESPN API (polls every 45 seconds)")
print("   - Betting odds/spreads from ESPN (when available)")
print("   - Green pulsing LIVE badge for in-progress games")
print("   - Old template backed up as sports_guide_backup.html")
