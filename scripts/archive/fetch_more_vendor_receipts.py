#!/usr/bin/env python3
"""
One-off: pull payment-receipt samples for L. Knife, Colonial, US Foods, PFG.

Mike said all four email payment confirmations to dashboard@rednun.com. We
need to see the actual sender/subject/body format before we can add classifier
signatures for them.

Searches widely — sender domains, brand keywords, "payment receipt" / "thank
you for your payment" patterns, last 120 days.

Output: /opt/red-nun-dashboard/scripts/archive/more_vendor_receipts.json
Read-only.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/fetch_more_vendor_receipts.py
"""
import os
import sys
import json
import pickle
from datetime import datetime

sys.path.insert(0, "/opt/red-nun-dashboard")

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

GMAIL_TOKEN_PATH = "/opt/red-nun-dashboard/integrations/google/gmail_token.pickle"
OUT_PATH = "/opt/red-nun-dashboard/scripts/archive/more_vendor_receipts.json"

# Cast a wide net per vendor. Domains based on CLAUDE.md notes:
#   US Foods    → order.usfoods.com
#   PFG         → customerfirstsolutions.com
#   Colonial    → apps.vtinfo.com (VTInfo)
#   L. Knife    → connect.vtinfo.com (Connect platform)
QUERIES = {
    "lknife": [
        '"l knife" newer_than:120d',
        '"l. knife" newer_than:120d',
        'from:vtinfo.com newer_than:120d (payment OR receipt OR paid OR "thank you")',
        'from:vipworldwide.com newer_than:120d (payment OR receipt OR paid)',
    ],
    "colonial": [
        '"colonial" (payment OR receipt OR paid OR "thank you") newer_than:120d',
        'from:colonial.com newer_than:120d',
        'from:colonialbev.com newer_than:120d',
    ],
    "usfoods": [
        '"us foods" newer_than:120d (payment OR receipt OR paid OR "thank you")',
        'from:usfoods.com newer_than:120d (payment OR receipt OR paid)',
        'from:order.usfoods.com newer_than:120d',
    ],
    "pfg": [
        '"performance food" newer_than:120d (payment OR receipt)',
        '"pfg" newer_than:120d (payment OR receipt OR paid)',
        'from:pfgc.com newer_than:120d',
        'from:customerfirstsolutions.com newer_than:120d (payment OR receipt OR paid)',
    ],
    "any_payment_keyword_recent": [
        # Catch-all in case sender/keyword is unexpected
        'subject:"payment receipt" newer_than:60d -from:quickbooks@notification.intuit.com '
        '-from:no-reply@servicecore.com -from:tigerexchange.us',
        'subject:"payment confirmation" newer_than:60d -from:quickbooks@notification.intuit.com',
    ],
}

PER_QUERY = 5


def load_creds():
    with open(GMAIL_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return creds


def header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def list_attachments(payload):
    found = []

    def walk(part):
        filename = part.get("filename") or ""
        mime = part.get("mimeType", "")
        size = (part.get("body") or {}).get("size", 0)
        if filename:
            found.append({"filename": filename, "mimeType": mime, "size": size})
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return found


def decode_text_body(payload):
    import base64

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime == "text/plain" and not (part.get("filename") or ""):
            try:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                return ""
        for sub in part.get("parts", []) or []:
            r = walk(sub)
            if r:
                return r
        return ""

    return walk(payload) or ""


def fetch(service, query, limit):
    out = []
    seen = set()
    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=limit
        ).execute()
    except Exception as e:
        return [{"error": str(e), "query": query}]

    for meta in resp.get("messages", []) or []:
        if meta["id"] in seen:
            continue
        seen.add(meta["id"])
        try:
            msg = service.users().messages().get(
                userId="me", id=meta["id"], format="full"
            ).execute()
        except Exception as e:
            out.append({"error": str(e), "id": meta["id"]})
            continue

        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        out.append({
            "id": msg["id"],
            "snippet": msg.get("snippet", "")[:240],
            "from": header(headers, "From"),
            "to": header(headers, "To"),
            "delivered_to": header(headers, "Delivered-To"),
            "subject": header(headers, "Subject"),
            "date": header(headers, "Date"),
            "labels": msg.get("labelIds", []),
            "attachments": list_attachments(payload),
            "text_body_first_2k": decode_text_body(payload)[:2000],
        })
    return out


def main():
    creds = load_creds()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile.get('emailAddress')}")

    results = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mailbox": profile.get("emailAddress"),
        "samples": {},
    }

    for source, queries in QUERIES.items():
        results["samples"][source] = []
        for q in queries:
            print(f"  [{source}] {q}")
            results["samples"][source].append({
                "query": q,
                "messages": fetch(service, q, PER_QUERY),
            })

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    print()
    print("=" * 60)
    for source, batches in results["samples"].items():
        total = sum(len(b["messages"]) for b in batches)
        with_attach = sum(
            1 for b in batches for m in b["messages"] if m.get("attachments")
        )
        print(f"{source}: {total} messages, {with_attach} with attachments")

    print()
    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
