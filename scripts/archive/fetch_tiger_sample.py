#!/usr/bin/env python3
"""
One-off: Pull the recent Tiger Exchange receipt Mike forwarded to dashboard@rednun.com.

Casts a wide net since we don't know Tiger's actual sender domain yet:
  - Anything from Mike's own addresses in the last 3 days (the forward)
  - Anything with "tiger" anywhere, last 14 days
  - Most recent 5 messages overall, last 1 day (fallback)

Output: /opt/red-nun-dashboard/scripts/archive/tiger_sample.json

Read-only.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/fetch_tiger_sample.py
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
OUT_PATH = '/opt/red-nun-dashboard/scripts/archive/tiger_sample.json'

QUERIES = {
    'forwards_from_mike': [
        # Mike said he just forwarded one — these are the likely senders
        'from:mgiorgio@rednun.com newer_than:3d',
        'from:mike@rednun.com newer_than:3d',
    ],
    'tiger_keyword_recent': [
        '"tiger" newer_than:14d',
        'subject:tiger newer_than:30d',
    ],
    'most_recent_overall': [
        # Fallback in case the forward stripped the keyword
        'newer_than:1d',
    ],
}

PER_QUERY = 5


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
    text_body = ''
    html_body = ''
    attachments = []

    def walk(part):
        nonlocal text_body, html_body
        mime = part.get('mimeType', '')
        filename = part.get('filename') or ''
        body = part.get('body', {}) or {}
        data = body.get('data')
        if filename:
            attachments.append({
                'filename': filename,
                'mimeType': mime,
                'size': body.get('size', 0),
            })
        if data and not filename:
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
    return text_body, html_body, attachments


def fetch(service, query, limit):
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
        text_body, html_body, attachments = decode_body(payload)

        out.append({
            'id': msg['id'],
            'snippet': msg.get('snippet', ''),
            'from': header(headers, 'From'),
            'to': header(headers, 'To'),
            'delivered_to': header(headers, 'Delivered-To'),
            'subject': header(headers, 'Subject'),
            'date': header(headers, 'Date'),
            'labels': msg.get('labelIds', []),
            'attachments': attachments,
            'text_body': text_body[:10000],
            'html_body_len': len(html_body),
            'html_body_first_4k': html_body[:4000],
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
                'messages': fetch(service, q, PER_QUERY),
            })

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\nWrote samples to: {OUT_PATH}")
    print("Paste the contents back to me.")


if __name__ == '__main__':
    main()
