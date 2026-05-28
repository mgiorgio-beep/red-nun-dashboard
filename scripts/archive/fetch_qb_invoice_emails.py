#!/usr/bin/env python3
"""
One-off: find QuickBooks-mediated invoice emails in dashboard@rednun.com.

Question we're answering: Mike's QB-paid vendors (Fore and Aft, Glanola, etc.)
have payment-confirmation emails but no entries in scanned_invoices. Are their
original invoice emails arriving here but not being scanned? Or never arriving?

Searches for:
  - Fore and Aft anywhere
  - Glanola anywhere
  - QB-routed "Invoice ### from..." subjects (any vendor)

Output: /opt/red-nun-dashboard/scripts/archive/qb_invoice_emails.json
Read-only.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/fetch_qb_invoice_emails.py
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
OUT_PATH = "/opt/red-nun-dashboard/scripts/archive/qb_invoice_emails.json"

QUERIES = {
    "fore_and_aft_anything": [
        '"fore and aft" newer_than:90d',
    ],
    "glanola_anything": [
        '"glanola" newer_than:90d',
    ],
    "qb_invoice_emails_recent": [
        # QB sends invoices as "Invoice #### from VendorName"
        'from:quickbooks@notification.intuit.com subject:"Invoice " newer_than:60d',
    ],
}

PER_QUERY = 10


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


def fetch(service, query, limit):
    out = []
    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=limit
        ).execute()
    except Exception as e:
        return [{"error": str(e), "query": query}]

    for meta in resp.get("messages", []) or []:
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
            "snippet": msg.get("snippet", "")[:200],
            "from": header(headers, "From"),
            "to": header(headers, "To"),
            "delivered_to": header(headers, "Delivered-To"),
            "subject": header(headers, "Subject"),
            "date": header(headers, "Date"),
            "labels": msg.get("labelIds", []),
            "attachments": list_attachments(payload),
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

    # Quick summary for the terminal
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
