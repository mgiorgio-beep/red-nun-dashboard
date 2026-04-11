#!/usr/bin/env python3
import os, urllib.request, json
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

ZONE_ID = os.getenv('CF_ZONE_ID')
RECORD_NAME = 'dashboard.rednun.com'
API_TOKEN = os.getenv('CF_API_TOKEN')

if not API_TOKEN or not ZONE_ID:
    raise SystemExit('CF_API_TOKEN and CF_ZONE_ID must be set in .env')

def cf(method, path, data=None):
    req = urllib.request.Request(
        f'https://api.cloudflare.com/client/v4{path}',
        data=json.dumps(data).encode() if data else None,
        method=method
    )
    req.add_header('Authorization', f'Bearer {API_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    return json.loads(urllib.request.urlopen(req).read())

ip = urllib.request.urlopen('https://api.ipify.org').read().decode()
records = cf('GET', f'/zones/{ZONE_ID}/dns_records?name={RECORD_NAME}')
record_id = records['result'][0]['id']
result = cf('PUT', f'/zones/{ZONE_ID}/dns_records/{record_id}', {
    'type': 'A', 'name': RECORD_NAME, 'content': ip, 'ttl': 1, 'proxied': True
})
print('Updated to:', ip if result['success'] else 'Failed: ' + str(result))
