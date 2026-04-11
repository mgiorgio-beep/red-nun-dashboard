#!/usr/bin/env python3
"""Fetch thermostat data and cache to JSON file. Run via cron every 5 min."""
import json
import sys
import somecomfort

DEVICE_LABELS = {
    6583982: "Restaurant",
    6581545: "Bathrooms",
    711786: "Restaurant",
}

LOCATIONS = {
    6635802: "dennis",
    3272967: "chatham",
}

try:
    client = somecomfort.SomeComfort('mike@rednun.com', 'Mollypj2029')
except Exception as e:
    print(f"Login failed: {e}", file=sys.stderr)
    sys.exit(1)

result = {}
for loc_id, loc_name in LOCATIONS.items():
    loc = client.locations_by_id.get(loc_id)
    if not loc:
        continue
    devices = []
    for dev_id, dev in loc.devices_by_id.items():
        humidity = dev.current_humidity
        if humidity and humidity >= 128:
            humidity = None
        devices.append({
            "id": dev_id,
            "label": DEVICE_LABELS.get(dev_id, dev.name),
            "current_temp": dev.current_temperature,
            "heat_setpoint": dev.setpoint_heat,
            "cool_setpoint": dev.setpoint_cool,
            "mode": dev.system_mode,
            "equipment_status": dev.equipment_output_status,
            "fan_running": dev.fan_running,
            "is_alive": dev.is_alive,
            "humidity": humidity,
        })
    result[loc_name] = devices

with open('/opt/rednun/thermostat_cache.json', 'w') as f:
    json.dump({"data": result, "ts": __import__('time').time()}, f)

print(f"OK: {sum(len(v) for v in result.values())} devices")
