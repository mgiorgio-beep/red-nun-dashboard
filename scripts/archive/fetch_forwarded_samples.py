#!/usr/bin/env python3
"""
Pull the most recent forwarded vendor-receipt samples Mike sent to dashboard@.

Looks for messages from mgiorgio@/mike@/invoice@ in the last 24h. The
forwarded message contains the original "From: ..." / "Subject: ..." lines
in the body, plus any PDF attachments.

Output: /opt/red-nun-dashboard/scripts/archive/forwarded_vendor_samples.json
Read-only.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/fetch_forwarded_samples.py
"""
import os
import sys
import json
import base64
import pickle
from datetime import datetime

sys.path.insert(0, "/opt/red-nun-dashboard")

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

GMAIL_TOKEN_PATH = "/opt/red-nun-dashboard/integrations/google/gmail_token.pickle"
OUT_PATH = "/opt/red-nun-dashboard/scripts/archive/forwarded_vendor_samples.json"

QUERY = (
    "newer_than:1d ("
    "from:mgiorgio@rednun.com OR "
    "from:mike@rednun.com OR "
    "from:invoice@rednun.com"
    ")"
)
LIMIT = 25


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
    out = []

    def walk(part):
        filename = part.get("filename") or ""
        mime = part.get("mimeType", "")
        size = (part.get("body") or {}).get("size", 0)
        if filename:
            out.append({"filename": filename, "mimeType": mime, "size": size})
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return out


def decode_bodies(payload):
    text_body = ""
    html_body = ""

    def walk(part):
        nonlocal text_body, html_body
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        filename = part.get("filename") or ""
        if data and not filename:
            try:
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/plain" and not text_body:
                text_body = decoded
            elif mime == "text/html" and not html_body:
                html_body = decoded
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return text_body, html_body


def main():
    creds = load_creds()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile.get('emailAddress')}")
    print(f"Query: {QUERY}")

    try:
        resp = service.users().messages().list(
            userId="me", q=QUERY, maxResults=LIMIT
        ).execute()
    except Exception as e:
        print(f"Gmail list failed: {e}")
        sys.exit(1)

    metas = resp.get("messages", []) or []
    print(f"Found {len(metas)} candidate messages")

    messages = []
    for meta in metas:
        try:
            msg = service.users().messages().get(
                userId="me", id=meta["id"], format="full"
            ).execute()
        except Exception as e:
            messages.append({"error": str(e), "id": meta["id"]})
            continue

        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        text_body, html_body = decode_bodies(payload)
        messages.append({
            "id": msg["id"],
            "snippet": msg.get("snippet", "")[:300],
            "from": header(headers, "From"),
            "to": header(headers, "To"),
            "delivered_to": header(headers, "Delivered-To"),
            "subject": header(headers, "Subject"),
            "date": header(headers, "Date"),
            "labels": msg.get("labelIds", []),
            "attachments": list_attachments(payload),
            "text_body_first_5k": text_body[:5000],
            "html_body_first_5k": html_body[:5000],
        })

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mailbox": profile.get("emailAddress"),
        "query": QUERY,
        "messages": messages,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print()
    print("=" * 60)
    for m in messages:
        if "error" in m:
            print(f"  error: {m.get('error')}")
            continue
        subj = (m.get("subject") or "")[:80]
        att = len(m.get("attachments") or [])
        print(f"  [{att} att] {subj}")

    print()
    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
