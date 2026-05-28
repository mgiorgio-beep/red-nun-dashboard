#!/usr/bin/env python3
"""
One-off: figure out why the poller query returns zero hits.

Runs the live poller query and a few diagnostic variations so we can see
whether the messages exist but are marked read, or simply aren't being
matched by the query.

Read-only.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/debug_poller_query.py
"""
import sys
import pickle

sys.path.insert(0, "/opt/red-nun-dashboard")

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from integrations.invoices.watchers.email_receipt_poller import RECEIPT_GMAIL_QUERY

GMAIL_TOKEN_PATH = "/opt/red-nun-dashboard/integrations/google/gmail_token.pickle"


def svc():
    with open(GMAIL_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def count(service, q):
    try:
        resp = service.users().messages().list(userId="me", q=q, maxResults=100).execute()
        msgs = resp.get("messages", []) or []
        return len(msgs)
    except Exception as e:
        return f"ERROR: {e}"


def list_first_n(service, q, n=5):
    try:
        resp = service.users().messages().list(userId="me", q=q, maxResults=n).execute()
    except Exception as e:
        return [f"ERROR: {e}"]
    out = []
    for meta in resp.get("messages", []) or []:
        try:
            msg = service.users().messages().get(
                userId="me", id=meta["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            labels = msg.get("labelIds", [])
            unread = "UNREAD" in labels
            out.append(f"  [{'UNREAD' if unread else 'READ  '}] {headers.get('Subject','')[:70]}")
        except Exception as e:
            out.append(f"  ERROR: {e}")
    return out


def main():
    s = svc()
    profile = s.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile.get('emailAddress')}")
    print()

    diagnostics = [
        ("Live poller query (is:unread + senders + forwards)", RECEIPT_GMAIL_QUERY),
        ("Same query but WITHOUT is:unread",
         RECEIPT_GMAIL_QUERY.replace("is:unread ", "")),
        ("All forwards from mgiorgio@ in last 1d (unread only)",
         "is:unread from:mgiorgio@rednun.com subject:Fwd newer_than:1d"),
        ("All forwards from mgiorgio@ in last 1d (regardless of read)",
         "from:mgiorgio@rednun.com subject:Fwd newer_than:1d"),
        ("All unread newer_than:1d",
         "is:unread newer_than:1d"),
    ]

    for label, q in diagnostics:
        n = count(s, q)
        print(f"{label}")
        print(f"  query: {q[:140]}{'...' if len(q) > 140 else ''}")
        print(f"  count: {n}")
        if isinstance(n, int) and n > 0:
            for line in list_first_n(s, q, 5):
                print(line)
        print()


if __name__ == "__main__":
    main()
