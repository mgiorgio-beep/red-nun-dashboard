#!/bin/bash
# ============================================================
# Red Nun Sports Guide — Deploy Script
# Run this on the production server: bash deploy_sports.sh
# ============================================================
set -e
echo "=== Creating sports_guide directory ==="
mkdir -p /opt/rednun/sports_guide/templates
mkdir -p /opt/rednun/data

# ── __init__.py ─────────────────────────────────────────────
echo "Creating __init__.py..."
cat > /opt/rednun/sports_guide/__init__.py << 'PYEOF'
from .fanzo_scraper import scrape_fanzo_guide, load_sports_data
from .sports import sports_bp
PYEOF

# ── fanzo_config.py ─────────────────────────────────────────
echo "Creating fanzo_config.py..."
cat > /opt/rednun/sports_guide/fanzo_config.py << 'PYEOF'
"""
FANZO Sports Guide Configuration
Red Nun Bar & Grill - Dennis Port & Chatham
"""
import os

FANZO_CONFIG_ID = '239743'
FANZO_GUIDE_URL = f'https://guide.thedailyrail.com/guide/display/{FANZO_CONFIG_ID}'
FANZO_SESSION_COOKIE = os.getenv('FANZO_SESSION_COOKIE', '')

FAVORITE_TEAMS = [
    'Bruins', 'Celtics', 'Patriots', 'Red Sox',
    'Harvard', 'Dartmouth', 'Boston College', 'Boston Col.', 'UMass', 'BC ',
]

STREAMING_SERVICES = {
    'ESPNplus': {'display': 'ESPN+', 'app': 'ESPN App', 'color': '#3B82F6', 'text_color': '#FFFFFF'},
    'ESPN+':    {'display': 'ESPN+', 'app': 'ESPN App', 'color': '#3B82F6', 'text_color': '#FFFFFF'},
    'Peacock SP': {'display': 'Peacock', 'app': 'Peacock App', 'color': '#8B6CEF', 'text_color': '#FFFFFF'},
    'Peacock':    {'display': 'Peacock', 'app': 'Peacock App', 'color': '#8B6CEF', 'text_color': '#FFFFFF'},
    'Apple TV+':  {'display': 'Apple TV+', 'app': 'Apple TV App', 'color': '#333333', 'text_color': '#CCCCCC'},
    'Amazon Prime': {'display': 'Prime Video', 'app': 'Prime App', 'color': '#00A8E1', 'text_color': '#FFFFFF'},
    'Prime Video':  {'display': 'Prime Video', 'app': 'Prime App', 'color': '#00A8E1', 'text_color': '#FFFFFF'},
}

SPORT_ICONS = {
    'NBA Basketball': '\U0001f3c0', "NCAA Basketball \u2013 Men's": '\U0001f3c0',
    "NCAA Basketball \u2013 Women's": '\U0001f3c0', 'Basketball': '\U0001f3c0',
    'Golf': '\u26f3', 'NASCAR Auto Racing': '\U0001f3c1', 'Hockey': '\U0001f3d2',
    'NHL Hockey': '\U0001f3d2', 'Lacrosse': '\U0001f94d', 'Soccer': '\u26bd',
    'Olympics': '\U0001f3c5', 'Football': '\U0001f3c8', 'NFL Football': '\U0001f3c8',
    'Baseball': '\u26be', 'Softball': '\U0001f94e', 'Tennis': '\U0001f3be',
    'Volleyball': '\U0001f3d0', 'Other Sports': '\U0001f4fa', 'Streaming': '\U0001f4e1',
    'Favorites': '\u2b50',
}

DATA_DIR = '/opt/rednun/data'
SPORTS_GUIDE_JSON = os.path.join(DATA_DIR, 'sports_guide.json')
PYEOF

# ── fanzo_scraper.py ────────────────────────────────────────
echo "Creating fanzo_scraper.py..."
cat > /opt/rednun/sports_guide/fanzo_scraper.py << 'PYEOF'
"""
FANZO Sports TV Guide Scraper
"""
import json, logging, os, re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from .fanzo_config import (
    FANZO_GUIDE_URL, FANZO_SESSION_COOKIE, FAVORITE_TEAMS,
    SPORTS_GUIDE_JSON, SPORT_ICONS, STREAMING_SERVICES, DATA_DIR,
)

logger = logging.getLogger(__name__)


def fetch_guide_html():
    session = requests.Session()
    session.cookies.set('PHPSESSID', FANZO_SESSION_COOKIE, domain='guide.thedailyrail.com')
    response = session.get(FANZO_GUIDE_URL, timeout=30)
    if 'Get A New Link' in response.text or response.url != FANZO_GUIDE_URL:
        raise PermissionError('FANZO session expired. Update FANZO_SESSION_COOKIE in .env.')
    response.raise_for_status()
    return response.text


def extract_cell_text(td):
    for br in td.find_all('br'):
        br.replace_with('|||')
    raw = td.get_text(strip=False)
    return [p.strip() for p in raw.split('|||') if p.strip()]


def _first(lst):
    return lst[0] if lst else ''


def _is_streaming_channel(channels):
    return any(ch in STREAMING_SERVICES for ch in channels)


def normalize_game(section_name, headers, row, is_favorite):
    game = {'is_favorite': is_favorite, 'q_score': _first(row.get('Q', []))}

    if section_name == 'Favorites':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = _first(row.get('Sport', []))
        game['event'] = _first(row.get('Event', []))
        game['detail'] = _first(row.get('Details', []))
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = False

    elif section_name == 'Streaming':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = _first(row.get('Sport', []))
        game['event'] = _first(row.get('Event', []))
        game['detail'] = _first(row.get('Details', []))
        service_raw = _first(row.get('Service', []))
        game['streaming_service'] = service_raw
        svc = STREAMING_SERVICES.get(service_raw, {})
        game['streaming_display'] = svc.get('display', service_raw)
        game['streaming_color'] = svc.get('color', '#666')
        game['streaming_text_color'] = svc.get('text_color', '#FFF')
        game['streaming_app'] = svc.get('app', service_raw)
        game['channels'] = []
        game['drtv'] = []
        game['is_streaming'] = True

    elif section_name == 'Golf':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = 'Golf'
        game['event'] = _first(row.get('Tour: Event', []))
        game['detail'] = _first(row.get('Location Round', []))
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = _is_streaming_channel(game['channels'])

    elif section_name == 'NASCAR Auto Racing':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = 'NASCAR'
        game['event'] = _first(row.get('Race', []))
        game['detail'] = _first(row.get('Track/Circuit', []))
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = _is_streaming_channel(game['channels'])

    elif section_name == 'Olympics':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = 'Olympics'
        game['event'] = _first(row.get('Event', []))
        game['detail'] = _first(row.get('Description', []))
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = _is_streaming_channel(game['channels'])

    elif section_name == 'Other Sports':
        game['time'] = _first(row.get('Time', []))
        game['sport'] = _first(row.get('Sport', []))
        game['event'] = _first(row.get('Event', []))
        game['detail'] = ''
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = _is_streaming_channel(game['channels'])

    else:
        visiting = _first(row.get('Visiting Team', []))
        home = _first(row.get('Home Team', row.get('Teams', [])))
        game['time'] = _first(row.get('Time', []))
        game['sport'] = section_name
        game['event'] = visiting
        game['detail'] = home
        game['channels'] = row.get('Channel', [])
        game['drtv'] = row.get('DRTV', [])
        game['is_streaming'] = _is_streaming_channel(game['channels'])

    if game.get('is_streaming') and not game.get('streaming_service'):
        for ch in game.get('channels', []):
            svc = STREAMING_SERVICES.get(ch)
            if svc:
                game['streaming_service'] = ch
                game['streaming_display'] = svc['display']
                game['streaming_color'] = svc['color']
                game['streaming_text_color'] = svc['text_color']
                game['streaming_app'] = svc['app']
                break

    return game


def parse_guide_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    blue_div = soup.find('div', class_='blue-divider')
    date_text = ''
    if blue_div:
        h4 = blue_div.find('h4')
        if h4:
            date_text = h4.get_text(strip=True)
            date_text = re.sub(r'\s*by\s+(Sport|Time|QScore).*$', '', date_text, flags=re.IGNORECASE)

    print_section = soup.find('div', id='section-to-print')
    if not print_section:
        raise ValueError('Could not find #section-to-print in guide HTML')

    favorites = []
    streaming = []
    sections = []

    for h3 in print_section.find_all('h3'):
        section_name = h3.get_text(strip=True)
        table_div = h3.find_next_sibling('div', class_='table-responsive')
        if not table_div:
            continue
        table = table_div.find('table')
        if not table:
            continue

        thead = table.find('thead')
        headers = []
        if thead:
            for th in thead.find_all('th'):
                if 'hidden-print' in th.get('class', []):
                    continue
                headers.append(th.get_text(strip=True))

        tbody = table.find('tbody')
        if not tbody:
            continue

        games = []
        for tr in tbody.find_all('tr'):
            tds = tr.find_all('td')
            if not tds:
                continue
            is_favorite = 'favorite' in tr.get('class', [])

            data_tds_raw = []
            for td in tr.find_all('td'):
                if td.find('a', class_='listing-note-btn'):
                    continue
                data_tds_raw.append(str(td))

            row = {}
            for i, header in enumerate(headers):
                if i < len(data_tds_raw):
                    cell_soup = BeautifulSoup(data_tds_raw[i], 'html.parser')
                    td_tag = cell_soup.find('td')
                    if td_tag:
                        row[header] = extract_cell_text(td_tag)
                    else:
                        row[header] = []

            game = normalize_game(section_name, headers, row, is_favorite)
            if game:
                games.append(game)

        if section_name == 'Favorites':
            favorites = games
        elif section_name == 'Streaming':
            streaming = games
        else:
            sections.append({
                'sport': section_name,
                'icon': SPORT_ICONS.get(section_name, '\U0001f4fa'),
                'game_count': len(games),
                'games': games,
            })

    for section in sections:
        for game in section['games']:
            if game.get('is_favorite'):
                continue
            text = f"{game.get('event', '')} {game.get('detail', '')}".lower()
            for team in FAVORITE_TEAMS:
                if team.lower() in text:
                    game['is_favorite'] = True
                    break

    return {
        'date': date_text,
        'updated_at': datetime.now().isoformat(),
        'favorites': favorites,
        'streaming': streaming,
        'sections': sections,
    }


def scrape_fanzo_guide():
    try:
        logger.info('Starting FANZO guide scrape...')
        html = fetch_guide_html()
        data = parse_guide_html(html)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SPORTS_GUIDE_JSON, 'w') as f:
            json.dump(data, f, indent=2)
        game_count = sum(s['game_count'] for s in data['sections'])
        logger.info(f'FANZO guide scraped: {data["date"]}, {len(data["sections"])} sections, {game_count} games')
        return True
    except PermissionError as e:
        logger.error(f'FANZO auth failed: {e}')
        return False
    except Exception as e:
        logger.error(f'FANZO scrape failed: {e}', exc_info=True)
        return False


def load_sports_data():
    if not os.path.exists(SPORTS_GUIDE_JSON):
        return None
    try:
        with open(SPORTS_GUIDE_JSON, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f'Error loading sports guide data: {e}')
        return None
PYEOF

# ── sports.py (Flask routes) ───────────────────────────────
echo "Creating sports.py..."
cat > /opt/rednun/sports_guide/sports.py << 'PYEOF'
"""Flask routes for Red Nun Sports Guide"""
from datetime import datetime
from flask import Blueprint, render_template, jsonify
from .fanzo_scraper import load_sports_data, scrape_fanzo_guide

sports_bp = Blueprint('sports', __name__, template_folder='templates')


@sports_bp.route('/sports/public')
def sports_guide_public():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_guide.html', data=data, stale=stale, show_nav=False)


@sports_bp.route('/sports')
def sports_guide():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_guide.html', data=data, stale=stale, show_nav=True)


@sports_bp.route('/sports/refresh', methods=['POST'])
def sports_guide_refresh():
    success = scrape_fanzo_guide()
    if success:
        return jsonify({'status': 'ok', 'message': 'Guide refreshed'})
    return jsonify({'status': 'error', 'message': 'Scrape failed. Check FANZO session cookie.'}), 500


def _is_stale(data):
    if not data or 'updated_at' not in data:
        return True
    try:
        updated = datetime.fromisoformat(data['updated_at'])
        return (datetime.now() - updated).total_seconds() > 86400
    except (ValueError, TypeError):
        return True
PYEOF

# ── Jinja2 Template ────────────────────────────────────────
echo "Creating template..."
cat > /opt/rednun/sports_guide/templates/sports_guide.html << 'HTMLEOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Red Nun Sports Guide</title>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0C0C0E;
            --bg-card: #151518;
            --red-accent: #9B1B1B;
            --gold: #D4A843;
            --gold-dim: rgba(212, 168, 67, 0.15);
            --green: #22C55E;
            --text-primary: #E8E8E8;
            --text-secondary: #999;
            --text-muted: #666;
            --border-color: #2A2A2E;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'IBM Plex Sans', -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            -webkit-font-smoothing: antialiased;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1a0505 0%, var(--red-accent) 50%, #1a0505 100%);
            padding: 16px 20px;
            position: sticky;
            top: 0;
            z-index: 100;
            border-bottom: 2px solid var(--gold);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .header-left { display: flex; align-items: center; gap: 14px; }
        .logo-badge {
            background: var(--bg-primary);
            border: 2px solid var(--gold);
            border-radius: 6px;
            padding: 4px 10px;
            font-family: 'Bebas Neue', sans-serif;
            font-size: 20px;
            letter-spacing: 2px;
            color: var(--gold);
        }
        .header-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 22px;
            letter-spacing: 3px;
            color: white;
        }
        .header-subtitle { font-size: 11px; color: rgba(255,255,255,0.6); letter-spacing: 1px; }
        .header-right { text-align: right; }
        .header-date { font-family: 'Bebas Neue', sans-serif; font-size: 18px; letter-spacing: 1px; color: white; }
        .header-day { font-size: 11px; color: rgba(255,255,255,0.6); text-transform: uppercase; letter-spacing: 1px; }
        .stale-banner {
            background: #7C2D12;
            color: #FED7AA;
            padding: 10px 20px;
            text-align: center;
            font-size: 13px;
            font-weight: 600;
        }
        .no-data { text-align: center; padding: 80px 20px; color: var(--text-secondary); }
        .no-data h2 { font-family: 'Bebas Neue', sans-serif; font-size: 28px; letter-spacing: 2px; margin-bottom: 12px; color: var(--text-primary); }
        .content { max-width: 1100px; margin: 0 auto; padding: 20px 16px 60px; }
        .section {
            background: var(--bg-card);
            border-radius: 8px;
            margin-bottom: 16px;
            overflow: hidden;
            border: 1px solid var(--border-color);
        }
        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 16px;
            border-left: 4px solid var(--red-accent);
        }
        .section-header.favorites { border-left-color: var(--gold); background: var(--gold-dim); }
        .section-header.streaming { border-left-color: #3B82F6; }
        .section-icon { font-size: 20px; margin-right: 10px; }
        .section-title { font-family: 'Bebas Neue', sans-serif; font-size: 20px; letter-spacing: 2px; flex-grow: 1; }
        .section-badge {
            background: rgba(255,255,255,0.1);
            color: var(--text-secondary);
            font-size: 11px;
            font-weight: 600;
            padding: 3px 10px;
            border-radius: 12px;
        }
        .game-table { width: 100%; border-collapse: collapse; }
        .game-table th {
            text-align: left;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            padding: 8px 12px;
            border-bottom: 1px solid var(--border-color);
        }
        .game-table th.ch-col { text-align: center; width: 110px; }
        .game-table th.time-col { width: 80px; }
        .game-row {
            border-bottom: 1px solid rgba(255,255,255,0.03);
            transition: background 0.15s;
        }
        .game-row:hover { background: rgba(255,255,255,0.03); }
        .game-row:last-child { border-bottom: none; }
        .game-row.fav-row { border-left: 3px solid var(--gold); }
        .game-row td { padding: 10px 12px; font-size: 13px; vertical-align: middle; }
        .game-row td.time-cell { font-weight: 600; color: var(--text-primary); white-space: nowrap; width: 80px; }
        .game-row td.event-cell { font-weight: 600; color: var(--text-primary); }
        .game-row td.detail-cell { color: var(--text-secondary); }
        .game-row td.sport-cell { color: var(--text-secondary); font-size: 12px; }
        .channel-cell { text-align: center; width: 110px; }
        .channel-info { display: flex; flex-direction: column; align-items: center; gap: 2px; }
        .channel-name { font-size: 11px; color: var(--text-secondary); font-weight: 500; }
        .channel-num { font-family: 'Bebas Neue', sans-serif; font-size: 20px; color: var(--gold); line-height: 1; }
        .channel-multi { margin-top: 4px; padding-top: 4px; border-top: 1px solid var(--border-color); }
        .streaming-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .streaming-app { display: block; font-size: 10px; color: var(--text-muted); margin-top: 2px; text-align: center; }
        .guide-footer {
            max-width: 1100px;
            margin: 0 auto;
            padding: 20px 16px;
            text-align: center;
            border-top: 1px solid var(--border-color);
        }
        .footer-title { font-family: 'Bebas Neue', sans-serif; font-size: 14px; letter-spacing: 2px; color: var(--text-muted); margin-bottom: 6px; }
        .footer-updated { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; justify-content: center; gap: 6px; }
        .pulse-dot { width: 6px; height: 6px; background: var(--green); border-radius: 50%; display: inline-block; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        .footer-note { font-size: 11px; color: var(--text-muted); margin-top: 8px; }
        .refresh-btn {
            background: transparent;
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
        }
        .refresh-btn:hover { border-color: var(--gold); color: var(--gold); }
        @media (max-width: 768px) {
            .header { padding: 12px 14px; }
            .logo-badge { font-size: 16px; padding: 3px 8px; }
            .header-title { font-size: 18px; }
            .content { padding: 12px 8px 60px; }
            .game-row td { padding: 8px 8px; font-size: 12px; }
        }
        @media print {
            body { background: white; color: black; }
            .header { position: relative; background: #333; }
            .channel-num { color: #333; }
            .refresh-btn { display: none; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div class="logo-badge">RED NUN</div>
            <div>
                <div class="header-title">SPORTS GUIDE</div>
                <div class="header-subtitle">Dennis Port &amp; Chatham</div>
            </div>
        </div>
        <div class="header-right">
            {% if data %}<div class="header-date">{{ data.date }}</div>{% endif %}
            <div class="header-day">DirecTV &bull; All Times Eastern</div>
        </div>
    </div>
    {% if stale %}
    <div class="stale-banner">&#9888; Guide may be outdated &mdash; last updated {{ data.updated_at[:10] if data else 'never' }}</div>
    {% endif %}
    {% if not data %}
    <div class="no-data">
        <h2>Guide Unavailable</h2>
        <p>The sports guide hasn't been loaded yet. It updates automatically at 5:00 AM daily.</p>
        <br>
        <p><a href="https://guide.thedailyrail.com/guide/display/239743" style="color: #D4A843;">View on FANZO directly &rarr;</a></p>
    </div>
    {% else %}
    <div class="content">
        {% if data.favorites %}
        <div class="section">
            <div class="section-header favorites">
                <span class="section-icon">&#11088;</span>
                <span class="section-title">FAVORITES</span>
                <span class="section-badge">{{ data.favorites|length }} games</span>
            </div>
            <table class="game-table">
                <thead><tr>
                    <th class="time-col">Time</th><th>Sport</th><th>Event</th><th>Detail</th><th class="ch-col">Channel</th>
                </tr></thead>
                <tbody>
                {% for game in data.favorites %}
                <tr class="game-row fav-row">
                    <td class="time-cell">{{ game.time }}</td>
                    <td class="sport-cell">{{ game.sport }}</td>
                    <td class="event-cell">{{ game.event }}</td>
                    <td class="detail-cell">{{ game.detail }}</td>
                    <td class="channel-cell">
                        {% if game.is_streaming %}
                        <span class="streaming-badge" style="background:{{ game.streaming_color|default('#666') }};color:{{ game.streaming_text_color|default('#fff') }}">{{ game.streaming_display|default(game.streaming_service) }}</span>
                        <span class="streaming-app">{{ game.streaming_app|default('') }}</span>
                        {% else %}
                        {% for i in range(game.channels|length) %}
                        <div class="channel-info{% if i > 0 %} channel-multi{% endif %}">
                            <span class="channel-name">{{ game.channels[i] }}</span>
                            {% if i < game.drtv|length %}<span class="channel-num">{{ game.drtv[i] }}</span>{% endif %}
                        </div>
                        {% endfor %}
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
        {% if data.streaming %}
        <div class="section">
            <div class="section-header streaming">
                <span class="section-icon">&#128225;</span>
                <span class="section-title">STREAMING</span>
                <span class="section-badge">{{ data.streaming|length }} events</span>
            </div>
            <table class="game-table">
                <thead><tr>
                    <th class="time-col">Time</th><th>Sport</th><th>Event</th><th>Detail</th><th class="ch-col">Service</th>
                </tr></thead>
                <tbody>
                {% for game in data.streaming %}
                <tr class="game-row{% if game.is_favorite %} fav-row{% endif %}">
                    <td class="time-cell">{{ game.time }}</td>
                    <td class="sport-cell">{{ game.sport }}</td>
                    <td class="event-cell">{{ game.event }}</td>
                    <td class="detail-cell">{{ game.detail }}</td>
                    <td class="channel-cell">
                        <span class="streaming-badge" style="background:{{ game.streaming_color|default('#666') }};color:{{ game.streaming_text_color|default('#fff') }}">{{ game.streaming_display|default(game.streaming_service) }}</span>
                        <span class="streaming-app">{{ game.streaming_app|default('') }}</span>
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
        {% for section in data.sections %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">{{ section.icon }}</span>
                <span class="section-title">{{ section.sport|upper }}</span>
                <span class="section-badge">{{ section.game_count }} game{{ 's' if section.game_count != 1 }}</span>
            </div>
            <table class="game-table">
                <thead><tr>
                    <th class="time-col">Time</th><th>Event</th><th>Detail</th><th class="ch-col">Channel</th>
                </tr></thead>
                <tbody>
                {% for game in section.games %}
                <tr class="game-row{% if game.is_favorite %} fav-row{% endif %}">
                    <td class="time-cell">{{ game.time }}</td>
                    <td class="event-cell">{{ game.event }}</td>
                    <td class="detail-cell">{{ game.detail }}</td>
                    <td class="channel-cell">
                        {% if game.is_streaming and game.streaming_display is defined %}
                        <span class="streaming-badge" style="background:{{ game.streaming_color }};color:{{ game.streaming_text_color }}">{{ game.streaming_display }}</span>
                        <span class="streaming-app">{{ game.streaming_app }}</span>
                        {% else %}
                        {% for i in range(game.channels|length) %}
                        <div class="channel-info{% if i > 0 %} channel-multi{% endif %}">
                            <span class="channel-name">{{ game.channels[i] }}</span>
                            {% if i < game.drtv|length %}<span class="channel-num">{{ game.drtv[i] }}</span>{% endif %}
                        </div>
                        {% endfor %}
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
        {% endfor %}
    </div>
    <div class="guide-footer">
        <div class="footer-title">DirecTV Channel Guide &mdash; Dennis Port &amp; Chatham</div>
        <div class="footer-updated">
            <span class="pulse-dot"></span>
            Auto-Updated &bull; {{ data.updated_at[:16]|replace('T', ' ') }} ET
        </div>
        <div class="footer-note">Streaming: ESPN+ &bull; Peacock &bull; Apple TV+ &bull; Amazon Prime</div>
        {% if show_nav %}
        <div style="margin-top: 12px;">
            <button class="refresh-btn" onclick="refreshGuide()">&#8635; Refresh Now</button>
        </div>
        {% endif %}
    </div>
    {% endif %}
    {% if show_nav %}
    <script>
    function refreshGuide() {
        var btn = document.querySelector('.refresh-btn');
        btn.textContent = 'Refreshing...';
        btn.disabled = true;
        fetch('/sports/refresh', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status === 'ok') { window.location.reload(); }
                else { alert('Refresh failed: ' + data.message); btn.textContent = 'Refresh Now'; btn.disabled = false; }
            })
            .catch(function() { alert('Refresh failed.'); btn.textContent = 'Refresh Now'; btn.disabled = false; });
    }
    </script>
    {% endif %}
</body>
</html>
HTMLEOF

# ── Patch server.py to add the Blueprint and scheduler job ──
echo "Patching server.py..."

# Check if already patched
if grep -q "sports_bp" /opt/rednun/server.py; then
    echo "server.py already has sports_bp — skipping patch"
else
    # Backup first
    cp /opt/rednun/server.py /opt/rednun/server.py.bak_sports

    # Add import after the storage_routes import
    sed -i '/from storage_routes import storage_bp/a from sports_guide import sports_bp, scrape_fanzo_guide' /opt/rednun/server.py

    # Add blueprint registration after storage_bp
    sed -i '/app.register_blueprint(storage_bp)/a app.register_blueprint(sports_bp)' /opt/rednun/server.py

    # Add scheduler job — find the line with scheduler.start() and add before it
    sed -i "/scheduler.start()/i\\    scheduler.add_job(func=scrape_fanzo_guide, trigger='cron', hour=5, minute=0, timezone='US/Eastern', id='fanzo_scrape', replace_existing=True)" /opt/rednun/server.py

    echo "server.py patched successfully"
fi

# ── Install beautifulsoup4 ──────────────────────────────────
echo "Installing beautifulsoup4..."
/opt/rednun/venv/bin/pip install beautifulsoup4 -q

# ── Restart gunicorn ─────────────────────────────────────────
echo "Restarting server..."
pkill -f "gunicorn.*server:app" || true
sleep 2
cd /opt/rednun
/opt/rednun/venv/bin/gunicorn --bind 127.0.0.1:8080 --workers 2 --timeout 120 server:app --daemon
sleep 2

# ── Test it ──────────────────────────────────────────────────
echo ""
echo "Testing routes..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/sports/public)
echo "/sports/public returned: $STATUS"

echo ""
echo "=== DEPLOY COMPLETE ==="
echo "Public URL: https://dashboard.rednun.com/sports/public"
echo "Dashboard URL: https://dashboard.rednun.com/sports"
echo ""
echo "To load today's data, run:"
echo "  curl -X POST https://dashboard.rednun.com/sports/refresh"
echo ""
