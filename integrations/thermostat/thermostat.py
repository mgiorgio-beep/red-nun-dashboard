"""Honeywell TCC thermostat - reads from cached JSON file."""
import os
import json
import time
import logging
import subprocess

logger = logging.getLogger(__name__)

CACHE_FILE = '/opt/rednun/thermostat_cache.json'
CACHE_TTL = 600  # 10 min - cron runs every 5

def get_thermostats():
    """Read thermostat data from cache file."""
    try:
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        age = time.time() - cache.get('ts', 0)
        if age < CACHE_TTL:
            return cache.get('data', {})
        # Stale but return it anyway with a flag
        data = cache.get('data', {})
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Thermostat cache read error: {e}")
        return {}

def set_setpoint(location, device_id, heat_sp=None, cool_sp=None):
    """Change thermostat setpoint via somecomfort subprocess."""
    try:
        cmd = f'''python3 -c "
import somecomfort
c = somecomfort.SomeComfort('mike@rednun.com', 'Mollypj2029')
locs = {{6635802: 'dennis', 3272967: 'chatham'}}
for lid, loc in c.locations_by_id.items():
    if locs.get(lid) == '{location}':
        dev = loc.devices_by_id.get({device_id})
        if dev:
            {'dev.setpoint_heat = ' + str(heat_sp) if heat_sp else ''}
            {'dev.setpoint_cool = ' + str(cool_sp) if cool_sp else ''}
            print('OK')
"'''
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if 'OK' in result.stdout:
            # Refresh cache
            subprocess.Popen(['/opt/rednun/venv/bin/python3', '/opt/rednun/thermostat_fetch.py'])
            return {"success": True}
        else:
            return {"error": result.stderr or "Unknown error"}
    except Exception as e:
        return {"error": str(e)}
