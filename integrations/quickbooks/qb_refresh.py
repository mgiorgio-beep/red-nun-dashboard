#!/usr/bin/env python3
"""Force-refresh the QBO access token using the saved refresh_token.
Prints the outcome so we can see exactly what went wrong if it fails."""
import sys, json, time, urllib.request, urllib.error, urllib.parse, base64, os
from pathlib import Path

TOKEN_FILE = Path.home() / ".qb_tokens.json"
TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

client_id     = os.environ.get("QB_CLIENT_ID")
client_secret = os.environ.get("QB_CLIENT_SECRET")
if not client_id or not client_secret:
    sys.exit("Missing QB_CLIENT_ID / QB_CLIENT_SECRET in env")

if not TOKEN_FILE.exists():
    sys.exit(f"No token file at {TOKEN_FILE}")

tokens = json.loads(TOKEN_FILE.read_text())
rt = tokens.get("refresh_token")
if not rt:
    sys.exit("No refresh_token in saved tokens")

print(f"Loaded tokens: obtained_at={tokens.get('obtained_at')}")
print(f"Refresh token (first 12 chars): {rt[:12]}...")
print(f"Realm id: {tokens.get('realm_id')}")

basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
data = urllib.parse.urlencode({
    "grant_type":    "refresh_token",
    "refresh_token": rt,
}).encode()

req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
req.add_header("Authorization", f"Basic {basic}")
req.add_header("Content-Type", "application/x-www-form-urlencoded")
req.add_header("Accept", "application/json")

try:
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        new = json.loads(body)
    print("OK — refresh succeeded")
except urllib.error.HTTPError as e:
    err_body = e.read().decode(errors="replace")
    print(f"FAIL — HTTP {e.code}")
    print(f"Body: {err_body}")
    sys.exit(1)

new["obtained_at"] = time.time()
if "refresh_token" not in new:
    new["refresh_token"] = rt
new["realm_id"] = tokens.get("realm_id")
TOKEN_FILE.write_text(json.dumps(new, indent=2))
print(f"New access_token (first 20): {new['access_token'][:20]}...")
print(f"Expires in: {new.get('expires_in')}s")
print(f"Saved -> {TOKEN_FILE}")
