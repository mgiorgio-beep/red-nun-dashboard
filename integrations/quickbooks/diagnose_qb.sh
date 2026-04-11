#!/bin/bash
# Diagnose QB auth setup on Beelink
set +e

echo "=== .qb_env contents (masked) ==="
if [ -f ~/.qb_env ]; then
    while IFS= read -r line; do
        if [[ "$line" =~ ^export[[:space:]]+QB_CLIENT_ID= ]]; then
            val="${line#*=}"
            val="${val#\'}"; val="${val%\'}"
            len=${#val}
            echo "QB_CLIENT_ID: len=$len  first6='${val:0:6}'  last4='${val: -4}'"
        elif [[ "$line" =~ ^export[[:space:]]+QB_CLIENT_SECRET= ]]; then
            val="${line#*=}"
            val="${val#\'}"; val="${val%\'}"
            len=${#val}
            echo "QB_CLIENT_SECRET: len=$len  first4='${val:0:4}'  last4='${val: -4}'"
        elif [[ "$line" =~ ^export[[:space:]]+QB_REALM_ID= ]]; then
            echo "$line"
        fi
    done < ~/.qb_env
else
    echo "NO ~/.qb_env FOUND"
fi

echo
echo "=== line endings (looking for \\r) ==="
if od -c ~/.qb_env 2>/dev/null | grep -q '\\r'; then
    echo "!!! CRLF LINE ENDINGS DETECTED - this will corrupt the secret !!!"
    od -c ~/.qb_env | head -5
else
    echo "OK - LF only"
fi

echo
echo "=== sourced values (actually-exported env) ==="
. ~/.qb_env
echo "QB_CLIENT_ID length: ${#QB_CLIENT_ID}"
echo "QB_CLIENT_SECRET length: ${#QB_CLIENT_SECRET}"
echo "QB_REALM_ID: $QB_REALM_ID"
# check for stray chars
printf '%s' "$QB_CLIENT_ID" | od -c | tail -3
echo

echo "=== tokens.json summary ==="
python3 - <<'PY'
import json, time, os
try:
    d = json.load(open(os.path.expanduser("~/.qb_tokens.json")))
    print("keys:", list(d.keys()))
    print("realm_id:", d.get("realm_id"))
    age_h = round((time.time() - d.get("obtained_at", 0)) / 3600, 1)
    print(f"obtained_at age: {age_h} hours ago")
    rt = d.get("refresh_token", "")
    print(f"refresh_token: len={len(rt)}  prefix={rt[:12]}...")
    at = d.get("access_token", "")
    print(f"access_token: len={len(at)}  prefix={at[:12]}...")
except Exception as e:
    print("ERROR:", e)
PY

echo
echo "=== attempting live token refresh ==="
python3 - <<'PY'
import json, os, time, base64, urllib.request, urllib.parse, urllib.error
cid = os.environ.get("QB_CLIENT_ID","")
sec = os.environ.get("QB_CLIENT_SECRET","")
print(f"Using client_id len={len(cid)}, secret len={len(sec)}")
tokens = json.load(open(os.path.expanduser("~/.qb_tokens.json")))
rt = tokens["refresh_token"]
creds = base64.b64encode(f"{cid}:{sec}".encode()).decode()
data = urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":rt}).encode()
req = urllib.request.Request("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", data=data, method="POST")
req.add_header("Authorization", f"Basic {creds}")
req.add_header("Content-Type", "application/x-www-form-urlencoded")
req.add_header("Accept", "application/json")
try:
    with urllib.request.urlopen(req) as r:
        print("SUCCESS:", json.loads(r.read()).get("access_token","")[:20]+"...")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"HTTP {e.code}: {body}")
PY
