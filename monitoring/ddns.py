#!/usr/bin/env python3
import os, urllib.request, json
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ZONE_ID = os.getenv('CF_ZONE_ID')
API_TOKEN = os.getenv('CF_API_TOKEN')

if not API_TOKEN or not ZONE_ID:
    raise SystemExit('CF_API_TOKEN and CF_ZONE_ID must be set in .env')

RECORDS = [
    ('dashboard.rednun.com',  True),
    ('wheelhouse.rednun.com', True),
    ('skywatch.rednun.com',   True),
    ('northfla.rednun.com',   True),
    ('ssh.rednun.com',        False),
]

# Never let this script touch the apex or www: `rednun.com` points at the
# Register.com web host and `www` is a CNAME to Toast online ordering.
# Mutating either has taken down real revenue before ($1k, Apr 12 2026).
FORBIDDEN = {'rednun.com', 'www.rednun.com'}

def cf(method, path, data=None):
    req = urllib.request.Request(
        f'https://api.cloudflare.com/client/v4{path}',
        data=json.dumps(data).encode() if data else None,
        method=method,
    )
    req.add_header('Authorization', f'Bearer {API_TOKEN}')
    req.add_header('Content-Type', 'application/json')
    return json.loads(urllib.request.urlopen(req).read())

ip = urllib.request.urlopen('https://api.ipify.org').read().decode().strip()

for name, proxied in RECORDS:
    if name in FORBIDDEN:
        raise SystemExit(f'Refusing to touch protected record: {name}')
    records = cf('GET', f'/zones/{ZONE_ID}/dns_records?type=A&name={name}')
    result = records.get('result') or []
    if not result:
        print(f'{name}: no A record found — skipping')
        continue
    rec = result[0]
    if rec['content'] == ip and rec.get('proxied') == proxied:
        print(f'{name}: ok ({ip}, proxied={proxied})')
        continue
    resp = cf('PUT', f'/zones/{ZONE_ID}/dns_records/{rec["id"]}', {
        'type': 'A', 'name': name, 'content': ip, 'ttl': 1, 'proxied': proxied,
    })
    if resp.get('success'):
        print(f'{name}: updated {rec["content"]} -> {ip} (proxied={proxied})')
    else:
        print(f'{name}: FAILED — {resp}')
