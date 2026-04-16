"""
Gmail client shared by QBO invoice + payment scrapers.

Uses the existing gmail_token.pickle / google_token.pickle from
the dashboard's routes/ directory so we don't re-auth.
"""
import os
import pickle
import base64
from googleapiclient.discovery import build

# Token paths — same ones application_routes.py uses
DASHBOARD_ROUTES = "/opt/red-nun-dashboard/integrations/google"
GMAIL_TOKEN = os.path.join(DASHBOARD_ROUTES, "gmail_token.pickle")
GOOGLE_TOKEN = os.path.join(DASHBOARD_ROUTES, "google_token.pickle")

QBO_SENDER = "quickbooks@notification.intuit.com"


def get_service():
    """Build an authenticated Gmail API service."""
    token_path = GMAIL_TOKEN if os.path.exists(GMAIL_TOKEN) else GOOGLE_TOKEN
    if not os.path.exists(token_path):
        raise RuntimeError(f"No Gmail token found at {GMAIL_TOKEN} or {GOOGLE_TOKEN}")
    with open(token_path, "rb") as f:
        creds = pickle.load(f)
    return build("gmail", "v1", credentials=creds)


def search_messages(service, query, max_results=None):
    """Search Gmail and return list of {id, threadId} dicts.

    Handles pagination automatically. Set max_results=None for all results.
    """
    results = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        msgs = resp.get("messages", [])
        results.extend(msgs)
        if max_results and len(results) >= max_results:
            return results[:max_results]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def get_message(service, message_id, fmt="full"):
    """Fetch a full message by ID."""
    return service.users().messages().get(
        userId="me", id=message_id, format=fmt
    ).execute()


def get_headers(message):
    """Extract headers as a dict from a Gmail message object."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]
    return headers


def get_body_text(message):
    """Extract plain-text body from a Gmail message, walking MIME parts."""
    def _walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
        if mime == "text/html" and data and not _walk.has_plain:
            html = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
            return html
        for sub in part.get("parts", []):
            result = _walk(sub)
            if result:
                return result
        return None
    _walk.has_plain = False

    payload = message.get("payload", {})
    body = _walk(payload)
    return body or ""


def get_pdf_attachments(service, message_id, message=None):
    """Return list of (filename, bytes) for all PDF attachments on a message."""
    if message is None:
        message = get_message(service, message_id)

    attachments = []

    def _walk(part):
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})

        is_pdf = (
            mime == "application/pdf"
            or filename.lower().endswith(".pdf")
        )

        if is_pdf and body.get("attachmentId"):
            att = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=body["attachmentId"]
            ).execute()
            data = att.get("data", "")
            pdf_bytes = base64.urlsafe_b64decode(data.encode())
            attachments.append((filename or "attachment.pdf", pdf_bytes))

        for sub in part.get("parts", []):
            _walk(sub)

    _walk(message.get("payload", {}))
    return attachments


def format_date_query(dt):
    """Format a datetime or date as Gmail's YYYY/MM/DD query format."""
    return dt.strftime("%Y/%m/%d")
