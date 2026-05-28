#!/usr/bin/env python3
"""
One-off: Pull sample auto-pay receipt emails from dashboard@rednun.com.

Goal: gather examples so we can design the receipt classifier + invoice matcher
that will sit in front of email_invoice_poller.py.

Output: /opt/red-nun-dashboard/scripts/archive/autopay_receipt_samples.json

Read-only — does NOT mark messages as read, does NOT modify labels.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/sample_autopay_receipts.py
"""
import os
import sys
import json
import base64
import pickle
from datetime import datetime

sys.path.insert(0, '/opt/red-nun-dashboard')

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

GMAIL_TOKEN_PATH = '/opt/red-nun-dashboard/integrations/google/gmail_token.pickle'
OUT_PATH = '/opt/red-nun-dashboard/scripts/archive/autopay_receipt_samples.json'

# Each query pulls up to PER_QUERY most-recent matches.
# We cast a wide net on sender/keywords so we capture whatever format actually arrives.
PER_QUERY = 5

QUERIES = {
    'tiger_exchange': [
        'from:tigerexchange.com',
        'from:tiger-exchange.com',
        '"tiger exchange" (receipt OR payment OR paid OR thank)',
    ],
    'suburban_supply': [
        'from:suburbansupply.com',
        'from:suburban-supply.com',
        '"suburban supply" (receipt OR payment OR paid OR thank)',
    ],
    'quickbooks_payments': [
        'from:intuit.com (receipt OR payment OR paid)',
        'from:quickbooks.intuit.com',
        'subject:"payment receipt" from:intuit.com',
        'subject:"you paid" from:intuit.com',
    ],
}


def load_creds():
    with open(GMAIL_TOKEN_PATH, 'rb') as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, 'wb') as f:
            pickle.dump(creds, f)
    return creds


def header(headers, name):
    for h in headers:
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def decode_body(payload):
    """Return (text_body, html_body) as best-effort decoded strings."""
    text_body = ''
    html_body = ''

    def walk(part):
        nonlocal text_body, html_body
        mime = part.get('mimeType', '')
        body = part.get('body', {}) or {}
        data = body.get('data')
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace')
            except Exception:
                decoded = ''
            if mime == 'text/plain' and not text_body:
                text_body = decoded
            elif mime == 'text/html' and not html_body:
                html_body = decoded
        for sub in part.get('parts', []) or []:
            walk(sub)

    walk(payload)
    return text_body, html_body


def fetch_sample(service, query, limit):
    """Return list of message detail dicts for a Gmail query."""
    out = []
    try:
        resp = service.users().messages().list(
            userId='me', q=query, maxResults=limit
        ).execute()
    except Exception as e:
        return [{'error': str(e), 'query': query}]

    for msg_meta in resp.get('messages', []) or []:
        try:
            msg = service.users().messages().get(
                userId='me', id=msg_meta['id'], format='full'
            ).execute()
        except Exception as e:
            out.append({'error': str(e), 'id': msg_meta['id']})
            continue

        payload = msg.get('payload', {}) or {}
        headers = payload.get('headers', []) or []
        text_body, html_body = decode_body(payload)

        out.append({
            'id': msg['id'],
            'thread_id': msg.get('threadId'),
            'internal_date': msg.get('internalDate'),
            'snippet': msg.get('snippet', ''),
            'from': header(headers, 'From'),
            'to': header(headers, 'To'),
            'delivered_to': header(headers, 'Delivered-To'),
            'subject': header(headers, 'Subject'),
            'date': header(headers, 'Date'),
            'labels': msg.get('labelIds', []),
            'has_attachments': any(
                (p.get('filename') or '').strip() for p in payload.get('parts', []) or []
            ),
            'text_body': text_body[:8000],   # truncate so the JSON stays readable
            'html_body_len': len(html_body),
            'html_body_first_2k': html_body[:2000],
        })
    return out


def main():
    creds = load_creds()
    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

    profile = service.users().getProfile(userId='me').execute()
    print(f"Authenticated as: {profile.get('emailAddress')}")

    results = {
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'mailbox': profile.get('emailAddress'),
        'samples': {},
    }

    for source, queries in QUERIES.items():
        results['samples'][source] = []
        for q in queries:
            print(f"  [{source}] {q}")
            results['samples'][source].append({
                'query': q,
                'messages': fetch_sample(service, q, PER_QUERY),
            })

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\nWrote samples to: {OUT_PATH}")
    print("Send that file back — I'll design the classifier from real data.")


if __name__ == '__main__':
    main()
