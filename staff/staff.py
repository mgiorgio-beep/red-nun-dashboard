"""
RN Staff — Staff Control PWA
Flask blueprint serving the staff shell at /staff
"""

import json
import os
import time
import threading
import fcntl
import logging
import socket
import struct
import base64
import re
import requests as http_requests
import websocket
import soco
import xml.etree.ElementTree as ET
from soco.music_services import MusicService
from flask import Blueprint, render_template, jsonify, request

logger = logging.getLogger(__name__)

staff_bp = Blueprint('staff', __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/staff/static')

DATA_DIR = '/opt/red-nun-dashboard/data'

ROKU_APP_IDS = {
    'ESPN+': '34376', 'ESPN App': '34376',
    'Peacock': '593099', 'Peacock App': '593099',
    'Apple TV+': '551012', 'Apple TV App': '551012',
    'Prime Video': '13', 'Prime App': '13',
    'YouTube TV': '195316',
}

DEFAULT_TVS = [
    {"id": "bar-left", "name": "Bar Left", "dtv_ip": "", "roku_ip": "", "channel": ""},
    {"id": "bar-middle", "name": "Bar Middle", "dtv_ip": "", "roku_ip": "", "channel": ""},
    {"id": "bar-right", "name": "Bar Right", "dtv_ip": "", "roku_ip": "", "channel": ""},
    {"id": "dr-left", "name": "DR Left", "dtv_ip": "", "roku_ip": "", "channel": ""},
    {"id": "dr-middle", "name": "DR Middle", "dtv_ip": "", "roku_ip": "", "channel": ""},
    {"id": "dr-right", "name": "DR Right", "dtv_ip": "", "roku_ip": "", "channel": ""},
]


def _load_sports_data():
    """Load sports guide data (same source as /guide)."""
    path = os.path.join(DATA_DIR, 'sports_guide.json')
    if not os.path.exists(path):
        # Scraper saves to scraping/data/
        path = os.path.join(os.path.dirname(DATA_DIR), 'scraping', 'data', 'sports_guide.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


_tvs_lock = threading.Lock()
_TVS_FILE = os.path.join(DATA_DIR, 'tvs.json')


def _load_tvs():
    """Load TV config from tvs.json or return defaults."""
    if os.path.exists(_TVS_FILE):
        with open(_TVS_FILE) as f:
            return json.load(f)
    return DEFAULT_TVS


def _save_tvs(tvs):
    """Save TV config to tvs.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_TVS_FILE, 'w') as f:
        json.dump(tvs, f, indent=2)


def _update_tv_channel(match_field, match_value, channel):
    """Atomically update a TV's channel by matching on a field. Safe across workers."""
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = _TVS_FILE + '.lock'
    with open(lock_path, 'w') as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            tvs = _load_tvs()
            for tv in tvs:
                if tv.get(match_field) == match_value:
                    tv['channel'] = channel
            _save_tvs(tvs)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


@staff_bp.route('/staff')
def staff_home():
    data = _load_sports_data()
    stale = False
    if data and 'updated_at' in data:
        from datetime import datetime
        try:
            updated = datetime.fromisoformat(data['updated_at'])
            stale = (datetime.now() - updated).total_seconds() > 86400
        except Exception:
            stale = True
    from flask import make_response
    resp = make_response(render_template('staff.html', data=data, stale=stale))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


# ── Specials Board ──

SPECIALS_PATH = os.path.join(DATA_DIR, 'specials.json')


def _load_specials():
    try:
        with open(SPECIALS_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'title': "Today's Specials", 'items': [], 'footer': ''}


def _save_specials(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SPECIALS_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _board_rv():
    """Return specials file mtime as reload version (survives restarts)."""
    try:
        return int(os.path.getmtime(os.path.join(DATA_DIR, 'specials.json')))
    except OSError:
        return 0

@staff_bp.route('/staff/api/board', methods=['GET'])
def get_board():
    data = _load_specials()
    data['_rv'] = _board_rv()
    return jsonify(data)

@staff_bp.route('/staff/api/board/reload', methods=['POST'])
def trigger_board_reload():
    """Touch specials file to bump mtime — TVs will full-page reload on next poll."""
    path = os.path.join(DATA_DIR, 'specials.json')
    try:
        os.utime(path, None)
    except OSError:
        pass
    return jsonify({'ok': True, 'rv': _board_rv()})


@staff_bp.route('/staff/api/board', methods=['POST'])
def update_board():
    data = request.json
    if not data:
        return jsonify({'error': 'No data'}), 400
    _save_specials(data)
    return jsonify({'ok': True})


@staff_bp.route('/staff/api/board/sync-toast', methods=['POST'])
def sync_toast_specials():
    """Pull specials from Toast POS and return structured board data."""
    try:
        from integrations.toast.toast_client import ToastAPIClient
        client = ToastAPIClient()
        location = (request.json or {}).get('location', 'chatham')
        menus = client.get_menus(location)

        soup = {}
        appetizer = {}
        entrees = []

        for menu in menus.get('menus', []):
            for group in menu.get('menuGroups', []):
                gname = group.get('name', '').lower()

                # Soup of the Day from Soups group
                if 'soup' in gname:
                    for item in group.get('menuItems', []):
                        if 'soup' in item.get('name', '').lower():
                            desc = (item.get('description') or '').strip()
                            if desc:
                                soup = {
                                    'name': desc,
                                    'desc': '',
                                    'price': '${:.0f}'.format(item['price']) if item.get('price') else ''
                                }

                # Specials group — app + entrees
                if gname == 'specials':
                    for item in group.get('menuItems', []):
                        iname = item.get('name', '')
                        desc = (item.get('description') or '').strip()
                        price = '${:.0f}'.format(item['price']) if item.get('price') else ''

                        if 'special app' in iname.lower():
                            appetizer = {
                                'name': desc or iname,
                                'desc': '',
                                'price': price
                            }
                        elif 'soup of the day' in iname.lower():
                            # Soup description = actual soup name
                            if desc:
                                soup = {
                                    'name': desc,
                                    'desc': '',
                                    'price': ''
                                }
                        else:
                            # Strip "Special " prefix from name
                            display_name = iname
                            if display_name.lower().startswith('special '):
                                display_name = display_name[8:]
                            entrees.append({
                                'name': display_name,
                                'desc': desc,
                                'price': price,
                                'color': 'white'
                            })

        return jsonify({
            'ok': True,
            'soup': soup,
            'appetizer': appetizer,
            'items': entrees
        })
    except Exception as e:
        logger.error("Toast sync failed: %s", e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@staff_bp.route('/staff/specials')
def specials_display():
    """Full-screen chalkboard display for portrait TV."""
    data = _load_specials()
    from flask import make_response
    resp = make_response(render_template('specials_tv.html', data=json.dumps(data)))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@staff_bp.route('/staff/specials/tv')
def specials_tv():
    """Alias for Fire TV Downloader app."""
    data = _load_specials()
    from flask import make_response
    resp = make_response(render_template('specials_tv.html', data=json.dumps(data)))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@staff_bp.route('/staff/specials/edit')
def specials_edit():
    """Manager editor — update specials from phone."""
    data = _load_specials()
    return render_template('specials_edit.html', data=json.dumps(data))


# ── TV Config ──

@staff_bp.route('/staff/api/tvs')
def get_tvs():
    """Returns list of TVs with name, IPs, current channel."""
    return jsonify(_load_tvs())


@staff_bp.route('/staff/api/tvs', methods=['POST'])
def save_tvs():
    """Save TV configuration (names, IPs)."""
    data = request.json
    if not isinstance(data, list):
        return jsonify({"status": "error", "message": "Expected array"}), 400
    _save_tvs(data)
    return jsonify({"status": "ok"})


# ── DirecTV Control ──

@staff_bp.route('/staff/api/dtv/tune', methods=['POST'])
def dtv_tune():
    """Tune a DirecTV receiver to a channel via SHEF protocol (port 8080)."""
    data = request.json
    dtv_ip = data.get('dtv_ip', '').strip()
    channel = data.get('channel', '').strip()
    if not dtv_ip or not channel:
        return jsonify({"status": "error", "message": "Missing dtv_ip or channel"}), 400
    try:
        url = 'http://{}:8080/tv/tune?major={}'.format(dtv_ip, channel)
        r = http_requests.get(url, timeout=3)
        logger.info("DTV tune %s → ch %s: %d", dtv_ip, channel, r.status_code)
        _update_tv_channel('dtv_ip', dtv_ip, channel)
        return jsonify({"status": "ok"})
    except http_requests.exceptions.ConnectionError:
        logger.warning("DTV unreachable: %s", dtv_ip)
        return jsonify({"status": "error", "message": "Receiver unreachable at " + dtv_ip}), 502
    except http_requests.exceptions.Timeout:
        logger.warning("DTV timeout: %s", dtv_ip)
        return jsonify({"status": "error", "message": "Receiver timed out"}), 504
    except Exception as e:
        logger.error("DTV error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@staff_bp.route('/staff/api/dtv/key', methods=['POST'])
def dtv_key():
    """Send a remote key press to a DirecTV receiver via SHEF."""
    data = request.json
    dtv_ip = data.get('dtv_ip', '').strip()
    key = data.get('key', '').strip()
    if not dtv_ip or not key:
        return jsonify({"status": "error", "message": "Missing dtv_ip or key"}), 400
    try:
        url = 'http://{}:8080/remote/processKey?key={}'.format(dtv_ip, key)
        r = http_requests.get(url, timeout=3)
        return jsonify({"status": "ok"})
    except http_requests.exceptions.ConnectionError:
        return jsonify({"status": "error", "message": "Receiver unreachable"}), 502
    except http_requests.exceptions.Timeout:
        return jsonify({"status": "error", "message": "Receiver timed out"}), 504
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Roku Control ──

@staff_bp.route('/staff/api/roku/command', methods=['POST'])
def roku_command():
    """Send a command to a Roku TV via ECP (port 8060)."""
    data = request.json
    roku_ip = data.get('roku_ip', '').strip()
    command = data.get('command', '').strip()
    if not roku_ip or not command:
        return jsonify({"status": "error", "message": "Missing roku_ip or command"}), 400
    try:
        base = 'http://{}:8060'.format(roku_ip)
        if command == 'power_on':
            url = base + '/keypress/PowerOn'
        elif command == 'power_off':
            url = base + '/keypress/PowerOff'
        elif command == 'hdmi1':
            url = base + '/launch/tvinput.hdmi1'
        elif command == 'hdmi2':
            url = base + '/launch/tvinput.hdmi2'
        elif command == 'hdmi3':
            url = base + '/launch/tvinput.hdmi3'
        elif command == 'launch_app':
            app_id = data.get('app_id', '').strip()
            if not app_id:
                return jsonify({"status": "error", "message": "Missing app_id"}), 400
            url = base + '/launch/' + app_id
            # Deep link params (e.g. YouTube TV contentId + mediaType)
            params = []
            if data.get('content_id'):
                params.append('contentId=' + data['content_id'])
            if data.get('media_type'):
                params.append('mediaType=' + data['media_type'])
            if params:
                url += '?' + '&'.join(params)
        elif command == 'home':
            url = base + '/keypress/Home'
        else:
            return jsonify({"status": "error", "message": "Unknown command: " + command}), 400

        r = http_requests.post(url, timeout=3)
        logger.info("Roku %s %s: %d", roku_ip, command, r.status_code)
        # Clear channel on power off
        if command == 'power_off':
            _update_tv_channel('roku_ip', roku_ip, '')
        return jsonify({"status": "ok"})
    except http_requests.exceptions.ConnectionError:
        logger.warning("Roku unreachable: %s", roku_ip)
        return jsonify({"status": "error", "message": "Roku unreachable at " + roku_ip}), 502
    except http_requests.exceptions.Timeout:
        logger.warning("Roku timeout: %s", roku_ip)
        return jsonify({"status": "error", "message": "Roku timed out"}), 504
    except Exception as e:
        logger.error("Roku error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@staff_bp.route('/staff/api/roku/apps')
def roku_apps():
    """Returns Roku app ID mapping for streaming services."""
    return jsonify(ROKU_APP_IDS)


# ── YouTube TV Channel Mapping ──

YTTV_CHANNELS_FILE = os.path.join(DATA_DIR, 'yttv_channels.json')


def _yttv_load():
    """Return cached YTTV channels, or empty dict."""
    if os.path.exists(YTTV_CHANNELS_FILE):
        with open(YTTV_CHANNELS_FILE) as f:
            return json.load(f)
    return {'channels': {}, 'updated': 0}


def _yttv_save(channels):
    """Save YTTV channel mapping."""
    os.makedirs(DATA_DIR, exist_ok=True)
    data = {'channels': channels, 'updated': int(time.time())}
    with open(YTTV_CHANNELS_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    return data


@staff_bp.route('/staff/api/yttv/channels')
def api_yttv_channels():
    """Return YTTV channel → videoId mapping."""
    return jsonify(_yttv_load())


@staff_bp.route('/staff/api/yttv/channels', methods=['POST'])
def api_yttv_save_channel():
    """Add or update a single YTTV channel mapping.
    Body: {name: "ESPN", video_id: "UBsv3sM_DH4"} or {name: "ESPN", url: "https://tv.youtube.com/watch/UBsv3sM_DH4?..."}
    """
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Channel name required'}), 400

    video_id = data.get('video_id', '').strip()
    if not video_id and data.get('url'):
        # Extract video ID from YouTube TV URL
        m = re.search(r'/watch/([a-zA-Z0-9_-]{11})', data['url'])
        if m:
            video_id = m.group(1)
    if not video_id:
        return jsonify({'ok': False, 'error': 'Video ID or URL required'}), 400

    current = _yttv_load()
    current['channels'][name] = video_id
    result = _yttv_save(current['channels'])
    return jsonify({'ok': True, 'channels': result['channels']})


@staff_bp.route('/staff/api/yttv/channels', methods=['DELETE'])
def api_yttv_delete_channel():
    """Remove a YTTV channel mapping. Body: {name: "ESPN"}"""
    data = request.json or {}
    name = data.get('name', '').strip()
    current = _yttv_load()
    current['channels'].pop(name, None)
    result = _yttv_save(current['channels'])
    return jsonify({'ok': True, 'channels': result['channels']})


# ── Samsung TV Control ──

SAMSUNG_APP_NAME = base64.b64encode(b'RedNunStaff').decode()
SAMSUNG_MAC_CACHE = {'10.1.10.209': 'B8:BC:5B:56:F6:1E'}  # ip -> mac


def _samsung_send_key(ip, key):
    """Send a remote key to a Samsung TV via WebSocket. Returns (ok, msg)."""
    url = 'ws://{}:8001/api/v2/channels/samsung.remote.control?name={}'.format(ip, SAMSUNG_APP_NAME)
    try:
        ws = websocket.create_connection(url, timeout=4)
        # Read the initial response (connection confirmation)
        ws.recv()
        payload = json.dumps({
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey"
            }
        })
        ws.send(payload)
        time.sleep(0.3)
        ws.close()
        return True, "ok"
    except Exception as e:
        logger.error("Samsung key %s to %s failed: %s", key, ip, e)
        return False, str(e)


def _samsung_wol(ip):
    """Wake a Samsung TV via Wake-on-LAN using cached or discovered MAC."""
    mac = SAMSUNG_MAC_CACHE.get(ip)
    if not mac:
        # Try to get MAC from the TV's API (when it was last on)
        try:
            r = http_requests.get('http://{}:8001/api/v2/'.format(ip), timeout=2)
            data = r.json()
            mac = data.get('device', {}).get('wifiMac', '')
            if mac:
                SAMSUNG_MAC_CACHE[ip] = mac
        except Exception:
            pass
    if not mac:
        # Fallback: check ARP table
        try:
            result = os.popen("arp -n {} 2>/dev/null".format(ip)).read()
            for part in result.split():
                if ':' in part and len(part) == 17:
                    mac = part
                    SAMSUNG_MAC_CACHE[ip] = mac
                    break
        except Exception:
            pass
    if not mac:
        return False, "No MAC address found for " + ip
    # Send WOL magic packet
    mac_bytes = bytes.fromhex(mac.replace(':', '').replace('-', ''))
    magic = b'\xff' * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(magic, ('255.255.255.255', 9))
    sock.close()
    logger.info("Samsung WOL sent to %s (%s)", ip, mac)
    return True, "WOL sent"


@staff_bp.route('/staff/api/samsung/command', methods=['POST'])
def samsung_command():
    """Send a command to a Samsung TV."""
    data = request.json
    samsung_ip = data.get('samsung_ip', '').strip()
    command = data.get('command', '').strip()
    if not samsung_ip or not command:
        return jsonify({"status": "error", "message": "Missing samsung_ip or command"}), 400
    try:
        if command == 'power_on':
            ok, msg = _samsung_wol(samsung_ip)
            return jsonify({"status": "ok" if ok else "error", "message": msg})
        elif command == 'power_off':
            ok, msg = _samsung_send_key(samsung_ip, 'KEY_POWER')
            if ok:
                _update_tv_channel('samsung_ip', samsung_ip, '')
            return jsonify({"status": "ok" if ok else "error", "message": msg})
        elif command == 'hdmi1':
            ok, msg = _samsung_send_key(samsung_ip, 'KEY_HDMI1')
            return jsonify({"status": "ok" if ok else "error", "message": msg})
        elif command == 'hdmi2':
            ok, msg = _samsung_send_key(samsung_ip, 'KEY_HDMI2')
            return jsonify({"status": "ok" if ok else "error", "message": msg})
        elif command == 'source':
            ok, msg = _samsung_send_key(samsung_ip, 'KEY_SOURCE')
            return jsonify({"status": "ok" if ok else "error", "message": msg})
        else:
            return jsonify({"status": "error", "message": "Unknown command: " + command}), 400
    except Exception as e:
        logger.error("Samsung error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Auto-Tune Scheduler ──

AUTOTUNE_PATH = os.path.join(DATA_DIR, 'autotunes.json')


def _load_autotunes():
    try:
        with open(AUTOTUNE_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_autotunes(tunes):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AUTOTUNE_PATH, 'w') as f:
        json.dump(tunes, f, indent=2)


@staff_bp.route('/staff/api/autotune', methods=['GET'])
def list_autotunes():
    return jsonify(_load_autotunes())


@staff_bp.route('/staff/api/autotune', methods=['POST'])
def add_autotune():
    data = request.json
    if not data:
        return jsonify({'error': 'No data'}), 400
    for key in ('tv_id', 'game_id', 'channel', 'start_ts'):
        if key not in data:
            return jsonify({'error': 'Missing ' + key}), 400
    tunes = _load_autotunes()
    tunes = [t for t in tunes if not (t['tv_id'] == data['tv_id'] and t['game_id'] == data['game_id'])]
    tunes.append({
        'tv_id': data['tv_id'],
        'game_id': data['game_id'],
        'channel': data['channel'],
        'start_ts': data['start_ts'],
        'game_label': data.get('game_label', ''),
        'tv_name': data.get('tv_name', ''),
        'is_stream': data.get('is_stream', False),
        'stream_name': data.get('stream_name', ''),
        'fired': False,
        'created_at': int(time.time())
    })
    _save_autotunes(tunes)
    return jsonify({'ok': True, 'count': len(tunes)})


@staff_bp.route('/staff/api/autotune', methods=['DELETE'])
def remove_autotune():
    data = request.json or {}
    tv_id = data.get('tv_id')
    game_id = data.get('game_id')
    if not tv_id or not game_id:
        return jsonify({'error': 'Missing tv_id or game_id'}), 400
    tunes = _load_autotunes()
    before = len(tunes)
    tunes = [t for t in tunes if not (t['tv_id'] == tv_id and t['game_id'] == game_id)]
    _save_autotunes(tunes)
    return jsonify({'ok': True, 'removed': before - len(tunes)})


def _execute_autotune(tune):
    """Execute a single auto-tune: wake TV, tune channel or launch app."""
    tvs = _load_tvs()
    tv = next((t for t in tvs if t['id'] == tune['tv_id']), None)
    if not tv:
        logger.error("Autotune: unknown tv_id %s", tune['tv_id'])
        return False
    # Wake TV first (Roku or Samsung)
    if tv.get('roku_ip'):
        try:
            http_requests.post('http://{}:8060/keypress/PowerOn'.format(tv['roku_ip']), timeout=3)
            time.sleep(2)
        except Exception:
            pass
    elif tv.get('samsung_ip'):
        try:
            _samsung_wol(tv['samsung_ip'])
            time.sleep(4)
        except Exception:
            pass
    if tune.get('is_stream') and tune.get('stream_name'):
        if not tv.get('roku_ip'):
            return False
        app_id = ROKU_APP_IDS.get(tune['stream_name'], tune['stream_name'])
        try:
            resp = http_requests.post('http://{}:8060/launch/{}'.format(tv['roku_ip'], app_id), timeout=5)
            logger.info("Autotune: launched %s on %s -> %d", tune['stream_name'], tv['name'], resp.status_code)
            return resp.status_code == 200
        except Exception as e:
            logger.error("Autotune stream launch failed: %s", e)
            return False
    else:
        if not tv.get('dtv_ip'):
            return False
        ch = tune['channel']
        try:
            resp = http_requests.get('http://{}:8080/tv/tune?major={}'.format(tv['dtv_ip'], ch), timeout=5)
            logger.info("Autotune: tuned %s to ch %s -> %d", tv['name'], ch, resp.status_code)
            if tv.get('roku_ip'):
                try:
                    http_requests.post('http://{}:8060/launch/tvinput.hdmi1'.format(tv['roku_ip']), timeout=3)
                except Exception:
                    pass
            elif tv.get('samsung_ip'):
                try:
                    time.sleep(2)
                    _samsung_send_key(tv['samsung_ip'], 'KEY_HDMI1')
                except Exception:
                    pass
            for t in tvs:
                if t['id'] == tune['tv_id']:
                    t['channel'] = ch
            _save_tvs(tvs)
            return resp.status_code == 200
        except Exception as e:
            logger.error("Autotune DTV tune failed: %s", e)
            return False


def _autotune_loop():
    logger.info("Staff autotune scheduler started")
    time.sleep(15)
    while True:
        try:
            tunes = _load_autotunes()
            now = int(time.time())
            changed = False
            for tune in tunes:
                if tune.get('fired'):
                    continue
                ts = tune.get('start_ts')
                if not ts:
                    continue
                if now >= ts and now < ts + 120:
                    logger.info("Autotune firing: %s -> ch %s for %s",
                                tune.get('tv_name'), tune.get('channel'), tune.get('game_label'))
                    _execute_autotune(tune)
                    tune['fired'] = True
                    changed = True
            tunes = [t for t in tunes if t.get('start_ts', 0) > now - 14400]
            if changed:
                _save_autotunes(tunes)
        except Exception as e:
            logger.error("Autotune loop error: %s", e)
        time.sleep(30)


_at_thread = threading.Thread(target=_autotune_loop, daemon=True)
_at_thread.start()


# ── Lights ──

@staff_bp.route('/staff/api/lights', methods=['GET'])
def get_lights():
    lights_file = os.path.join(DATA_DIR, 'lights.json')
    default = {
        "zones": [
            {"id": "bar", "name": "Bar", "level": 80, "on": True},
            {"id": "dining", "name": "Dining Room", "level": 60, "on": True}
        ],
        "activeScene": None
    }
    if os.path.exists(lights_file):
        with open(lights_file) as f:
            return jsonify(json.load(f))
    return jsonify(default)


@staff_bp.route('/staff/api/lights', methods=['POST'])
def set_lights():
    data = request.json
    lights_file = os.path.join(DATA_DIR, 'lights.json')
    os.makedirs(os.path.dirname(lights_file), exist_ok=True)
    # TODO: implement Lutron Caseta telnet proxy to Smart Bridge on Beelink
    with open(lights_file, 'w') as f:
        json.dump(data, f)
    return jsonify({"status": "ok"})


# ── Sonos ──

SONOS_IP = '10.1.10.242'

SONOS_SID_MAP = {
    '9': 'Spotify', '12': 'Spotify', '160': 'Spotify',
    '236': 'Pandora', '203': 'Pandora',
    '204': 'Apple Music', '52': 'Apple Music', '2311': 'Apple Music',
    '254': 'Amazon Music',
    '284': 'YouTube Music',
}


def _get_sonos():
    """Return SoCo device for the Dining Room speaker."""
    return soco.SoCo(SONOS_IP)


def _sonos_source(speaker):
    """Return (service_name, station/playlist_name) for current playback."""
    try:
        mi = speaker.avTransport.GetMediaInfo([('InstanceID', 0)])
        uri = mi.get('CurrentURI', '')
        meta = mi.get('CurrentURIMetaData', '')
        # Detect service from sid param
        sid_match = re.search(r'sid=(\d+)', uri)
        service = SONOS_SID_MAP.get(sid_match.group(1), '') if sid_match else ''
        if not service and 'x-rincon-stream' in uri:
            service = 'Line-In'
        # Extract station/playlist name from metadata
        station = ''
        if meta:
            try:
                root = ET.fromstring(meta)
                t = root.find('.//{http://purl.org/dc/elements/1.1/}title')
                if t is not None:
                    station = t.text or ''
            except ET.ParseError:
                pass
        return service, station
    except Exception:
        return '', ''


@staff_bp.route('/staff/api/sonos/status')
def sonos_status():
    try:
        speaker = _get_sonos()
        info = speaker.get_current_track_info()
        state = speaker.get_current_transport_info()['current_transport_state']
        service, station = _sonos_source(speaker)
        return jsonify({
            'playing': state == 'PLAYING',
            'track': info.get('title', ''),
            'artist': info.get('artist', ''),
            'album': info.get('album', ''),
            'art_url': info.get('album_art', ''),
            'volume': speaker.volume,
            'state': state,
            'service': service,
            'station': station
        })
    except Exception as e:
        logger.error("Sonos status error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/play', methods=['POST'])
def sonos_play():
    try:
        _get_sonos().play()
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos play error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/pause', methods=['POST'])
def sonos_pause():
    try:
        _get_sonos().pause()
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos pause error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/next', methods=['POST'])
def sonos_next():
    try:
        _get_sonos().next()
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos next error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/prev', methods=['POST'])
def sonos_prev():
    try:
        _get_sonos().previous()
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos prev error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/volume', methods=['POST'])
def sonos_volume():
    try:
        data = request.json or {}
        vol = int(data.get('volume', 0))
        vol = max(0, min(100, vol))
        _get_sonos().volume = vol
        return jsonify({'ok': True, 'volume': vol})
    except Exception as e:
        logger.error("Sonos volume error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/mute', methods=['POST'])
def sonos_mute():
    try:
        speaker = _get_sonos()
        speaker.mute = not speaker.mute
        return jsonify({'ok': True, 'muted': speaker.mute})
    except Exception as e:
        logger.error("Sonos mute error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/zones')
def sonos_zones():
    try:
        import soco as _soco
        zones = []
        for speaker in _soco.discover(timeout=5) or []:
            zones.append({
                'name': speaker.player_name,
                'ip': speaker.ip_address,
                'volume': speaker.volume,
                'is_coordinator': speaker.is_coordinator,
                'group': speaker.group.label if speaker.group else '',
            })
        zones.sort(key=lambda z: z['name'])
        return jsonify({'zones': zones})
    except Exception as e:
        logger.error("Sonos zones error: %s", e)
        return jsonify({'error': str(e)}), 502


@staff_bp.route('/staff/api/sonos/zone', methods=['POST'])
def sonos_set_zone():
    try:
        data = request.json or {}
        ip = data.get('ip', '')
        if not ip:
            return jsonify({'error': 'Missing ip'}), 400
        global SONOS_IP
        SONOS_IP = ip
        speaker = _get_sonos()
        return jsonify({'ok': True, 'name': speaker.player_name, 'ip': ip})
    except Exception as e:
        logger.error("Sonos set zone error: %s", e)
        return jsonify({'error': str(e)}), 502


@staff_bp.route('/staff/api/sonos/favorites')
def sonos_favorites():
    try:
        speaker = _get_sonos()
        favs = speaker.music_library.get_sonos_favorites(complete_result=True)
        result = []
        for f in favs:
            uri = f.resources[0].uri if f.resources else ''
            if not uri:
                continue
            # Detect service from uri sid
            sid_match = re.search(r'sid=(\d+)', uri)
            service = SONOS_SID_MAP.get(sid_match.group(1), '') if sid_match else ''
            if not service and 'x-rincon-stream' in uri:
                service = 'Line-In'
            result.append({
                'title': f.title,
                'uri': uri,
                'meta': f.resource_meta_data if hasattr(f, 'resource_meta_data') else '',
                'service': service
            })
        # Apply custom sort order if saved
        order_path = os.path.join(DATA_DIR, 'sonos_fav_order.json')
        try:
            with open(order_path, 'r') as _f:
                order = json.load(_f)
            order_map = {t: i for i, t in enumerate(order)}
            result.sort(key=lambda x: order_map.get(x['title'], 999))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return jsonify({'favorites': result})
    except Exception as e:
        logger.error("Sonos favorites error: %s", e)
        return jsonify({'error': 'Speaker unreachable', 'detail': str(e)}), 502


@staff_bp.route('/staff/api/sonos/favorites/order', methods=['POST'])
def sonos_favorites_order():
    data = request.json or {}
    order = data.get('order', [])
    order_path = os.path.join(DATA_DIR, 'sonos_fav_order.json')
    with open(order_path, 'w') as f:
        json.dump(order, f)
    return jsonify({'ok': True})


@staff_bp.route('/staff/api/sonos/favorite', methods=['POST'])
def sonos_play_favorite():
    """Play a Sonos favorite by URI. Handles both radio streams and container playlists."""
    try:
        data = request.json or {}
        uri = data.get('uri', '')
        meta = data.get('meta', '')
        if not uri:
            return jsonify({'error': 'Missing uri'}), 400
        speaker = _get_sonos()
        if 'cpcontainer' in uri:
            # Container (playlist): clear queue, add, and play
            speaker.clear_queue()
            speaker.add_uri_to_queue(uri, meta)
            speaker.play_from_queue(0)
        else:
            # Radio/stream: play directly
            speaker.play_uri(uri, meta)
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos play favorite error: %s", e)
        return jsonify({'error': 'Failed to play', 'detail': str(e)}), 502


# ── Sonos Music Service Linking & Browsing ──

SONOS_SERVICES = ['Spotify', 'Pandora']
_sonos_linking = {}  # service_name -> MusicService instance (during linking)
_SONOS_LINK_FILE = '/tmp/rednun_sonos_linking.json'  # shared across gunicorn workers
_sonos_svc_cache = {}  # service_name -> (MusicService, timestamp)
_SONOS_SVC_TTL = 300  # cache linked service objects for 5 min


def _is_service_linked(name):
    """Quick check if a service has tokens in the store (no SMAPI call)."""
    try:
        svc = MusicService(name)
        store = svc.token_store
        # Check if there are non-empty tokens for this service
        collection = getattr(store, '_token_store', {}).get(
            getattr(store, '_token_collection', 'default'), {})
        for key, val in collection.items():
            if key.startswith(str(svc.service_id) + '#'):
                # val is [token, key] — linked if either is non-empty
                if isinstance(val, (list, tuple)) and any(v for v in val):
                    return True
        return False
    except Exception:
        return False


def _get_music_service(name):
    """Get a MusicService instance, returning None if not linked."""
    cached = _sonos_svc_cache.get(name)
    if cached and time.time() - cached[1] < _SONOS_SVC_TTL:
        return cached[0]
    try:
        svc = MusicService(name)
        # Use raw SOAP call to test — SoCo's get_metadata parser is buggy
        svc.soap_client.call(
            "getMetadata",
            [("id", "root"), ("index", 0), ("count", 1), ("recursive", 0)],
        )
        _sonos_svc_cache[name] = (svc, time.time())
        return svc
    except Exception:
        _sonos_svc_cache.pop(name, None)
        return None


@staff_bp.route('/staff/api/sonos/services')
def sonos_services():
    """List music services with their linked status."""
    result = []
    for name in SONOS_SERVICES:
        linked = _is_service_linked(name)
        result.append({'name': name, 'linked': linked})
    return jsonify({'services': result})


@staff_bp.route('/staff/api/sonos/link/start', methods=['POST'])
def sonos_link_start():
    """Start the OAuth linking flow for a music service. Returns auth URL."""
    data = request.json or {}
    name = data.get('service', '')
    if name not in SONOS_SERVICES:
        return jsonify({'error': 'Unknown service'}), 400
    try:
        svc = MusicService(name)
        url = svc.begin_authentication()
        _sonos_linking[name] = svc
        # Persist linking state so any gunicorn worker can complete it
        try:
            link_data = {}
            if os.path.exists(_SONOS_LINK_FILE):
                with open(_SONOS_LINK_FILE, 'r') as f:
                    link_data = json.load(f)
            link_data[name] = {
                'link_code': svc.link_code,
                'auth_token': getattr(svc, 'auth_token', ''),
            }
            with open(_SONOS_LINK_FILE, 'w') as f:
                json.dump(link_data, f)
        except Exception:
            pass
        return jsonify({'ok': True, 'url': url, 'link_code': svc.link_code})
    except Exception as e:
        logger.error("Sonos link start error for %s: %s", name, e)
        return jsonify({'error': str(e)}), 500


@staff_bp.route('/staff/api/sonos/link/complete', methods=['POST'])
def sonos_link_complete():
    """Complete the OAuth linking after user has authorized."""
    data = request.json or {}
    name = data.get('service', '')
    svc = _sonos_linking.get(name)
    # If not in this worker's memory, restore from shared file
    if not svc:
        try:
            with open(_SONOS_LINK_FILE, 'r') as f:
                link_data = json.load(f)
            saved = link_data.get(name)
            if saved:
                svc = MusicService(name)
                svc.link_code = saved['link_code']
                svc.auth_token = saved.get('auth_token', '')
        except Exception:
            pass
    if not svc:
        return jsonify({'error': 'No pending link for ' + name}), 400
    try:
        svc.complete_authentication()
        _sonos_linking.pop(name, None)
        # Clean up shared file
        try:
            with open(_SONOS_LINK_FILE, 'r') as f:
                link_data = json.load(f)
            link_data.pop(name, None)
            with open(_SONOS_LINK_FILE, 'w') as f:
                json.dump(link_data, f)
        except Exception:
            pass
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos link complete error for %s: %s", name, e)
        return jsonify({'error': str(e)}), 500


def _sonos_browse_raw(svc, item_id, index=0, count=50):
    """Browse a music service using raw SOAP (bypasses SoCo parser bugs)."""
    response = svc.soap_client.call(
        "getMetadata",
        [("id", item_id), ("index", index), ("count", count), ("recursive", 0)],
    )
    result_key = 'getMetadataResult'
    data = response.get(result_key, {})
    items_raw = data.get('mediaCollection', data.get('mediaMetadata', []))
    # Normalize: single item comes as dict, multiple as list
    if isinstance(items_raw, dict):
        items_raw = [items_raw]
    return items_raw, int(data.get('total', 0))


@staff_bp.route('/staff/api/sonos/browse')
def sonos_browse():
    """Browse a music service. ?service=Spotify&id=root"""
    name = request.args.get('service', '')
    item_id = request.args.get('id', 'root')
    if name not in SONOS_SERVICES:
        return jsonify({'error': 'Unknown service'}), 400
    svc = _get_music_service(name)
    if not svc:
        return jsonify({'error': 'Service not linked', 'not_linked': True}), 403
    try:
        items_raw, total = _sonos_browse_raw(svc, item_id)
        result = []
        for raw in items_raw:
            itype = raw.get('itemType', '')
            is_container = itype in ('container', 'collection', 'albumList', 'playlist')
            can_play = raw.get('canPlay', 'false') == 'true'
            result.append({
                'id': raw.get('id', ''),
                'title': raw.get('title', ''),
                'art': raw.get('albumArtURI', ''),
                'container': is_container,
                'can_play': can_play,
                'item_type': itype,
            })
        return jsonify({'items': result, 'total': total})
    except Exception as e:
        logger.error("Sonos browse error for %s/%s: %s", name, item_id, e)
        return jsonify({'error': str(e)}), 500


@staff_bp.route('/staff/api/sonos/browse/play', methods=['POST'])
def sonos_browse_play():
    """Play an item from a music service browse result."""
    data = request.json or {}
    name = data.get('service', '')
    item_id = data.get('id', '')
    title = data.get('title', '')
    if not name or not item_id:
        return jsonify({'error': 'Missing service or id'}), 400
    try:
        speaker = _get_sonos()
        svc = _get_music_service(name)
        if not svc:
            return jsonify({'error': 'Service not linked'}), 403
        sid = svc.service_id
        desc_text = svc.desc
        from urllib.parse import quote
        encoded_id = quote(item_id, safe='')
        # Get item info for metadata title if not provided
        if not title:
            try:
                raw_resp = svc.soap_client.call(
                    "getMediaMetadata", [("id", item_id)])
                raw_item = raw_resp.get('getMediaMetadataResult', {})
                title = raw_item.get('title', item_id)
            except Exception:
                title = item_id
        # Build URI: x-sonosapi-radio for radio/program items
        uri = 'x-sonosapi-radio:{}?sid={}&flags=8296&sn=0'.format(encoded_id, sid)
        # Build DIDL metadata matching what Sonos expects
        meta = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="000c0068{eid}" parentID="-1" restricted="true">'
            '<dc:title>{title}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
            '{desc}</desc>'
            '</item></DIDL-Lite>'
        ).format(
            eid=encoded_id,
            title=title.replace('&', '&amp;').replace('<', '&lt;'),
            desc=desc_text
        )
        speaker.play_uri(uri, meta)
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("Sonos browse play error: %s", e)
        return jsonify({'error': str(e)}), 502
