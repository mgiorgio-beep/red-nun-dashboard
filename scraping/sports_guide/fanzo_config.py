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
    'Max':        {'display': 'Max', 'app': 'Max App', 'color': '#002BE7', 'text_color': '#FFFFFF'},
    'Paramount+': {'display': 'Paramount+', 'app': 'Paramount+ App', 'color': '#0064FF', 'text_color': '#FFFFFF'},
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
