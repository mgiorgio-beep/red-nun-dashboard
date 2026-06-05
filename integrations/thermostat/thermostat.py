"""Honeywell TCC thermostat - reads from cached JSON file."""
import os
import json
import time
import logging
import subprocess

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

logger = logging.getLogger(__name__)

CACHE_FILE = '/opt/red-nun-dashboard/thermostat_cache.json'
CACHE_TTL = 600  # 10 min - cron runs every 5

LOCATIONS = {6635802: 'dennis', 3272967: 'chatham'}
FETCH_SCRIPT = '/opt/red-nun-dashboard/integrations/thermostat/thermostat_fetch.py'
VENV_PYTHON = '/opt/red-nun-dashboard/venv/bin/python3'

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
    """Change thermostat setpoint via somecomfort (in-process, no shell)."""
    user = os.getenv('HONEYWELL_USER')
    password = os.getenv('HONEYWELL_PASS')
    if not user or not password:
        return {"error": "HONEYWELL_USER / HONEYWELL_PASS not set in .env"}
    try:
        import somecomfort
        client = somecomfort.SomeComfort(user, password)
        applied = False
        for lid, loc in client.locations_by_id.items():
            if LOCATIONS.get(lid) != location:
                continue
            dev = loc.devices_by_id.get(int(device_id))
            if not dev:
                continue
            if heat_sp is not None:
                dev.setpoint_heat = heat_sp
            if cool_sp is not None:
                dev.setpoint_cool = cool_sp
            applied = True
        if not applied:
            return {"error": "Device or location not found"}
        # Refresh cache in the background
        subprocess.Popen([VENV_PYTHON, FETCH_SCRIPT])
        return {"success": True}
    except Exception as e:
        logger.error(f"set_setpoint failed: {e}")
        return {"error": str(e)}
