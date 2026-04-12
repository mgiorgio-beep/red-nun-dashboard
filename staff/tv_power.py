"""
Red Nun — Fire TV Power Control via ADB over WiFi
Blueprint prefix: /staff/api/tv-power
Supports multiple Fire TVs via fire_tvs config array.
"""

import subprocess
import json
import os
import time
import logging
import threading
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

tv_power_bp = Blueprint('tv_power', __name__)

CONFIG_PATH = '/opt/red-nun-dashboard/data/tv_power.json'
DEFAULT_SPECIALS_URL = 'http://10.1.10.83:8080/staff/specials/tv'


def _load_config():
    defaults = {
        "fire_tvs": [],
        "adb_port": 5555,
        "schedule_on": "11:00",
        "schedule_off": "21:00",
        "schedule_enabled": False,
        "specials_url": DEFAULT_SPECIALS_URL
    }
    try:
        with open(CONFIG_PATH, 'r') as f:
            saved = json.load(f)
            defaults.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Backward compat: migrate single fire_tv_ip → fire_tvs array
    if 'fire_tv_ip' in defaults:
        old_ip = defaults.pop('fire_tv_ip')
        if old_ip and not defaults.get('fire_tvs'):
            defaults['fire_tvs'] = [{"name": "TV 1", "ip": old_ip}]
        _save_config(defaults)

    if not defaults.get('specials_url'):
        defaults['specials_url'] = DEFAULT_SPECIALS_URL

    return defaults


def _save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config.pop('fire_tv_ip', None)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def _adb(ip, port, args, timeout=8):
    """Run an ADB command. Returns (success: bool, output: str)."""
    target = "{}:{}".format(ip, port)
    try:
        result = subprocess.run(
            ['/usr/bin/adb', '-s', target] + args,
            capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "ADB command timed out"
    except FileNotFoundError:
        return False, "ADB not installed"


def _ensure_connected(ip, port):
    """Connect ADB to the Fire TV if not already connected."""
    result = subprocess.run(['/usr/bin/adb', 'devices'], capture_output=True, text=True)
    target = "{}:{}".format(ip, port)
    if target not in result.stdout:
        subprocess.run(
            ['/usr/bin/adb', 'connect', target],
            capture_output=True, text=True, timeout=8
        )


def _screen_is_on(ip, port):
    """Returns True if the Fire TV screen is currently on."""
    ok, output = _adb(ip, port, ['shell', 'dumpsys', 'power'])
    if not ok:
        return None
    if 'state=ON' in output or 'mScreenOn=true' in output:
        return True
    return False


def _launch_specials(ip, port, specials_url=None, cold_boot=False):
    """Launch/reload specials in Downloader. Avoids force-stop to prevent home screen flash."""
    if not specials_url:
        specials_url = DEFAULT_SPECIALS_URL
    # Add cache buster, preserve existing query params
    sep = '&' if '?' in specials_url else '?'
    url = '{}{}t={}'.format(specials_url, sep, int(time.time()))
    # On cold boot, force-stop needed since app isn't running yet
    if cold_boot:
        _adb(ip, port, ['shell', 'am', 'force-stop', 'com.esaba.downloader'])
        time.sleep(2)
    _adb(ip, port, [
        'shell', 'am', 'start',
        '-n', 'com.esaba.downloader/.ui.main.MainActivity',
        '-a', 'android.intent.action.VIEW',
        '-d', url
    ])
    # Cold boot needs longer wait for app to fully render
    wait = 12 if cold_boot else 6
    time.sleep(wait)
    # Send MENU twice to enter fullscreen, retry once if cold boot
    for attempt in range(1):
        _adb(ip, port, ['shell', 'input', 'keyevent', 'KEYCODE_MENU'])
        time.sleep(1)
        _adb(ip, port, ['shell', 'input', 'keyevent', 'KEYCODE_MENU'])
        time.sleep(1)
        _adb(ip, port, ['shell', 'input', 'touchscreen', 'swipe', '960', '540', '1919', '1079', '50'])
        if cold_boot and attempt == 0:
            time.sleep(5)


def _get_target_tvs(config, target_ip=None):
    """Return list of TV dicts to operate on. If target_ip given, filter to that one."""
    tvs = config.get('fire_tvs', [])
    if target_ip:
        tvs = [tv for tv in tvs if tv.get('ip') == target_ip]
    return tvs


def _in_schedule(schedule_on, schedule_off):
    """Return True if current time is within the on/off schedule (Eastern time)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('America/New_York'))
    try:
        on_h, on_m = map(int, schedule_on.split(':'))
        off_h, off_m = map(int, schedule_off.split(':'))
    except (ValueError, AttributeError):
        return None
    on_mins = on_h * 60 + on_m
    off_mins = off_h * 60 + off_m
    now_mins = now.hour * 60 + now.minute
    return on_mins <= now_mins < off_mins


def _get_foreground_app(ip, port):
    """Return the package name of the foreground app, or None."""
    ok, output = _adb(ip, port, ['shell', 'dumpsys', 'window', 'windows'], timeout=10)
    if not ok:
        return None
    for line in output.splitlines():
        if 'mCurrentFocus' in line or 'mFocusedApp' in line:
            for part in line.split():
                if '/' in part and '.' in part:
                    return part.split('/')[0].rstrip('}')
    return None


_WATCHDOG_LOCK = '/tmp/rednun_watchdog.lock'


def _specials_watchdog():
    """Background thread: every 2 min, manage Fire TV power and specials display."""
    logger.info("Specials watchdog started")
    time.sleep(30)
    while True:
        # File lock so only one gunicorn worker runs the watchdog at a time
        try:
            import fcntl
            lock_fd = open(_WATCHDOG_LOCK, 'w')
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            # Another worker holds the lock — skip this cycle
            time.sleep(120)
            continue
        try:
            config = _load_config()
            tvs = config.get('fire_tvs', [])
            port = config.get('adb_port', 5555)
            schedule_on = config.get('schedule_on', '11:00')
            schedule_off = config.get('schedule_off', '21:00')
            schedule_enabled = config.get('schedule_enabled', True)
            specials_url = config.get('specials_url', DEFAULT_SPECIALS_URL)

            if not tvs:
                time.sleep(120)
                continue

            in_sched = _in_schedule(schedule_on, schedule_off) if schedule_enabled else None

            for tv in tvs:
                ip = tv.get('ip', '')
                name = tv.get('name', ip)
                if not ip:
                    continue
                tv_url = tv.get('specials_url', specials_url)
                try:
                    _ensure_connected(ip, port)
                    screen_on = _screen_is_on(ip, port)

                    if in_sched is True and screen_on is False:
                        logger.info("Watchdog [%s]: schedule says ON, waking TV", name)
                        _adb(ip, port, ['shell', 'input', 'keyevent', '224'])
                        time.sleep(5)
                        _launch_specials(ip, port, tv_url, cold_boot=True)
                        continue

                    if in_sched is False and screen_on is True and schedule_enabled:
                        logger.info("Watchdog [%s]: schedule says OFF, sleeping TV", name)
                        _adb(ip, port, ['shell', 'input', 'keyevent', '223'])
                        continue

                    if screen_on:
                        fg = _get_foreground_app(ip, port)
                        if fg and 'com.esaba.downloader' not in fg:
                            logger.info("Watchdog [%s]: foreground is '%s', launching specials", name, fg)
                            _launch_specials(ip, port, tv_url, cold_boot=True)

                except Exception as e:
                    logger.error("Watchdog [%s] error: %s", name, e)

        except Exception as e:
            logger.error("Watchdog error: %s", e)
        finally:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

        time.sleep(120)


#_watchdog_thread = threading.Thread(target=_specials_watchdog, daemon=True)
#_watchdog_thread.start()


# --- Routes ---

@tv_power_bp.route('/staff/api/tv-power/config', methods=['GET'])
def get_config():
    return jsonify(_load_config())


@tv_power_bp.route('/staff/api/tv-power/config', methods=['POST'])
def save_config():
    config = _load_config()
    data = request.get_json(force=True)
    for key in ('fire_tvs', 'adb_port', 'schedule_on', 'schedule_off',
                'schedule_enabled', 'specials_url'):
        if key in data:
            config[key] = data[key]
    _save_config(config)
    return jsonify({'ok': True, 'config': config})


@tv_power_bp.route('/staff/api/tv-power/status', methods=['GET'])
def tv_status():
    config = _load_config()
    port = config.get('adb_port', 5555)
    target_ip = request.args.get('ip')
    tvs = _get_target_tvs(config, target_ip)

    if not tvs:
        return jsonify({'ok': False, 'error': 'No Fire TVs configured', 'results': []})

    results = []
    for tv in tvs:
        ip = tv.get('ip', '')
        name = tv.get('name', ip)
        _ensure_connected(ip, port)
        on = _screen_is_on(ip, port)
        results.append({
            'name': name,
            'ip': ip,
            'screen': 'on' if on else ('off' if on is False else 'unknown')
        })

    return jsonify({'ok': True, 'results': results})


@tv_power_bp.route('/staff/api/tv-power/on', methods=['POST'])
def tv_on():
    config = _load_config()
    port = config.get('adb_port', 5555)
    specials_url = config.get('specials_url', DEFAULT_SPECIALS_URL)
    target_ip = request.args.get('ip')
    tvs = _get_target_tvs(config, target_ip)

    if not tvs:
        return jsonify({'ok': False, 'error': 'No Fire TVs configured'})

    results = []
    results_lock = threading.Lock()

    def wake_tv(tv):
        ip = tv.get('ip', '')
        name = tv.get('name', ip)
        tv_url = tv.get('specials_url', specials_url)
        _ensure_connected(ip, port)

        ok, output = _adb(ip, port, ['shell', 'input', 'keyevent', '224'])
        if not ok:
            with results_lock:
                results.append({'name': name, 'ip': ip, 'ok': False, 'error': output})
            return

        try:
            time.sleep(5)
            _launch_specials(ip, port, tv_url, cold_boot=True)
        except Exception as e:
            logger.error("Failed to launch specials after wake on %s: %s", name, e)

        with results_lock:
            results.append({'name': name, 'ip': ip, 'ok': True, 'action': 'on', 'output': output})

    threads = []
    for tv in tvs:
        t = threading.Thread(target=wake_tv, args=(tv,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=60)

    return jsonify({'ok': True, 'results': results})


@tv_power_bp.route('/staff/api/tv-power/off', methods=['POST'])
def tv_off():
    config = _load_config()
    port = config.get('adb_port', 5555)
    target_ip = request.args.get('ip')
    tvs = _get_target_tvs(config, target_ip)

    if not tvs:
        return jsonify({'ok': False, 'error': 'No Fire TVs configured'})

    results = []
    for tv in tvs:
        ip = tv.get('ip', '')
        name = tv.get('name', ip)
        _ensure_connected(ip, port)

        ok, output = _adb(ip, port, ['shell', 'input', 'keyevent', '223'])
        if not ok:
            results.append({'name': name, 'ip': ip, 'ok': False, 'error': output})
            continue

        results.append({'name': name, 'ip': ip, 'ok': True, 'action': 'off', 'output': output})

    return jsonify({'ok': True, 'results': results})


@tv_power_bp.route('/staff/api/tv-power/reload', methods=['POST'])
def tv_reload():
    config = _load_config()
    port = config.get('adb_port', 5555)
    specials_url = config.get('specials_url', DEFAULT_SPECIALS_URL)
    target_ip = request.args.get('ip')
    tvs = _get_target_tvs(config, target_ip)

    if not tvs:
        return jsonify({'ok': False, 'error': 'No Fire TVs configured'})

    # Use server-triggered reload — TVs poll and reload themselves (no home screen flash)
    try:
        import requests as _req
        _req.post('http://127.0.0.1:8080/staff/api/board/reload', timeout=3)
    except Exception:
        pass

    return jsonify({'ok': True, 'results': [{'name': 'All TVs', 'ok': True, 'message': 'Reload signal sent — TVs will refresh within 30s'}]})


@tv_power_bp.route('/staff/api/tv-power/pair', methods=['POST'])
def pair_tv():
    config = _load_config()
    port = config.get('adb_port', 5555)
    target_ip = request.args.get('ip')
    tvs = _get_target_tvs(config, target_ip)

    if not tvs:
        return jsonify({'ok': False, 'error': 'No Fire TVs configured'})

    results = []
    for tv in tvs:
        ip = tv.get('ip', '')
        name = tv.get('name', ip)
        try:
            result = subprocess.run(
                ['/usr/bin/adb', 'connect', '{}:{}'.format(ip, port)],
                capture_output=True, text=True, timeout=12
            )
            output = (result.stdout + result.stderr).strip()
            connected = 'connected' in output.lower()
            results.append({
                'name': name, 'ip': ip, 'ok': connected, 'output': output,
                'message': 'Connected! Check TV for authorization popup.' if not connected else output
            })
        except subprocess.TimeoutExpired:
            results.append({
                'name': name, 'ip': ip, 'ok': False,
                'error': 'Connection timed out — is the Fire TV on and ADB enabled?'
            })

    return jsonify({'ok': True, 'results': results})
