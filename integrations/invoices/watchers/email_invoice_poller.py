#!/usr/bin/env python3
"""
Email Invoice Poller — Gmail API (modular pipeline)

Watches Gmail for unread emails with invoice attachments and saves them
to /opt/rednun/invoice_images/. Does NOT run OCR — that is handled
separately by local_invoice_watcher.py (runs every 5 minutes via cron).

Requires gmail_token.pickle with gmail.readonly + gmail.modify scopes.
Run gmail_auth.py once to create the token.

Manifest: /opt/rednun/.email_poller_manifest.json  (deduplication by Gmail message ID)

Location detection (highest priority first):
  1. To/Delivered-To contains +dennis or +chatham
  2. Subject contains "dennis" or "chatham"
  3. Default: dennis

Run via cron every 5 minutes:
  */5 * * * * cd /opt/rednun && source venv/bin/activate && python email_invoice_poller.py >> /var/log/rednun_email_poller.log 2>&1
"""
import os
import sys
import json
import base64
import logging
import pickle
from datetime import datetime
from email.header import decode_header as email_decode_header

sys.path.insert(0, '/opt/rednun')
os.chdir('/opt/rednun')

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

GMAIL_TOKEN_PATH = '/opt/rednun/gmail_token.pickle'
INTAKE_DIR       = '/opt/rednun/invoice_images'
MANIFEST_PATH    = '/opt/rednun/.email_poller_manifest.json'

GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

SUPPORTED_MIME = {
    'application/pdf': 'pdf',
    'image/jpeg': 'jpg',
    'image/jpg':  'jpg',
    'application/zip': 'zip',
    'application/x-zip-compressed': 'zip',
    'image/png':  'png',
    'image/webp': 'webp',
    'image/heic': 'heic',
    'application/octet-stream': None,  # resolved by filename
    'text/plain': None,                # IIF files sometimes arrive as text/plain
}


# ── Manifest ──────────────────────────────────────────────────────────────────

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read manifest ({e}), starting fresh")
    return {"processed": {}}


def save_manifest(manifest):
    tmp = MANIFEST_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, MANIFEST_PATH)


# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    if not os.path.exists(GMAIL_TOKEN_PATH):
        logger.error(
            "Gmail token not found. Run gmail_auth.py once to authorize:\n"
            "  cd /opt/rednun && source venv/bin/activate && python gmail_auth.py --url\n"
            "  python gmail_auth.py --code <code>"
        )
        return None

    with open(GMAIL_TOKEN_PATH, 'rb') as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        logger.info("Refreshing Gmail token...")
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, 'wb') as f:
            pickle.dump(creds, f)

    if not creds.valid:
        logger.error("Gmail credentials invalid. Re-run gmail_auth.py.")
        return None

    return build('gmail', 'v1', credentials=creds)


# ── Header helpers ────────────────────────────────────────────────────────────

def decode_mime_header(value):
    if not value:
        return ''
    parts = email_decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(str(part))
    return ' '.join(result)


def detect_location(headers_list):
    """Detect invoice location (dennis/chatham) from email headers."""
    headers = {h['name'].lower(): h['value'] for h in headers_list}

    for hdr in ['delivered-to', 'to', 'x-original-to']:
        addr = headers.get(hdr, '').lower()
        if '+dennis' in addr:
            return 'dennis'
        if '+chatham' in addr:
            return 'chatham'

    subject = decode_mime_header(headers.get('subject', '')).lower()
    if 'chatham' in subject:
        return 'chatham'
    if 'dennis' in subject:
        return 'dennis'

    return 'dennis'


# ── Attachment extraction ─────────────────────────────────────────────────────

def _infer_ext_from_filename(filename):
    """Return (mime_type, ext) inferred from filename extension, or (None, None)."""
    fn = (filename or '').lower().strip()
    if fn.endswith('.pdf'):
        return 'application/pdf', 'pdf'
    if fn.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg', 'jpg'
    if fn.endswith('.png'):
        return 'image/png', 'png'
    if fn.endswith('.webp'):
        return 'image/webp', 'webp'
    if fn.endswith('.heic'):
        return 'image/heic', 'heic'
    # ScanSnap garbles filenames — check for .zip anywhere (e.g. "foo .zip")
    if '.zip' in fn:
        return 'application/zip', 'zip'
    if fn.endswith('.iif'):
        return 'text/plain', 'iif'
    return None, None


def collect_attachments(parts, service, msg_id):
    """Recursively collect invoice attachment data from Gmail message parts."""
    attachments = []
    for part in parts:
        mime_type = part.get('mimeType', '')
        filename  = part.get('filename', '')
        body      = part.get('body', {})

        # Recurse into multipart
        if mime_type.startswith('multipart/') and 'parts' in part:
            attachments.extend(collect_attachments(part['parts'], service, msg_id))
            continue

        # Determine extension — try filename first, then MIME
        inferred_mime, ext = _infer_ext_from_filename(filename)
        if ext:
            mime_type = inferred_mime
        else:
            ext = SUPPORTED_MIME.get(mime_type)
        if not ext:
            continue

        # Fetch attachment bytes
        if body.get('attachmentId'):
            try:
                att = service.users().messages().attachments().get(
                    userId='me', messageId=msg_id, id=body['attachmentId']
                ).execute()
                data = base64.urlsafe_b64decode(att['data'])
            except Exception as e:
                logger.warning(f"  Could not fetch attachment '{filename}': {e}")
                continue
        elif body.get('data'):
            data = base64.urlsafe_b64decode(body['data'])
        else:
            continue

        if len(data) < 1000 and ext != 'iif':
            continue  # skip tiny icons / signatures (IIF text files can be small)

        # Unzip if needed — ScanSnap sends JPEGs inside a zip
        if ext == 'zip' or mime_type in ('application/zip', 'application/x-zip-compressed') or filename.lower().endswith('.zip'):
            try:
                import zipfile, io as _io
                with zipfile.ZipFile(_io.BytesIO(data)) as zf:
                    for zname in sorted(zf.namelist()):
                        if zname.lower().endswith(('.jpg', '.jpeg')):
                            zdata = zf.read(zname)
                            attachments.append({
                                'data': zdata,
                                'mime_type': 'image/jpeg',
                                'filename': zname,
                                'ext': 'jpg',
                            })
                            logger.info(f'  Unzipped: {zname} ({len(zdata)} bytes)')
                continue
            except Exception as e:
                logger.warning(f'  Failed to unzip {filename}: {e}')
                continue

        attachments.append({
            'data':      data,
            'mime_type': mime_type,
            'filename':  filename or f'invoice.{ext}',
            'ext':       ext,
        })

    return attachments


# ── Per-message processing ────────────────────────────────────────────────────

def save_attachments(service, msg_id, location):
    """
    Fetch full message, extract attachments, save to intake folder.
    When multiple images come from the same email (e.g. ZIP with 2 JPEGs),
    send them as a single multi-page scan (page 1 + extra_file_0, extra_file_1...).
    Returns count of files saved.
    """
    msg     = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    payload = msg.get('payload', {})
    parts   = payload.get('parts', [payload])

    attachments = collect_attachments(parts, service, msg_id)

    if not attachments:
        logger.info(f"  No invoice attachments found in message {msg_id}")
        return 0

    os.makedirs(INTAKE_DIR, exist_ok=True)
    ts_base = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Separate IIF files from image/PDF attachments
    iif_attachments = [a for a in attachments if a['ext'] == 'iif']
    image_attachments = [a for a in attachments if a['ext'] != 'iif']

    saved_count = 0

    # Process IIF files — structured data, no OCR needed
    for att in iif_attachments:
        try:
            import requests
            # Detect location from US Foods customer number in filename
            iif_location = location
            fname = att.get('filename', '')
            if '90541301' in fname:
                iif_location = 'chatham'
            elif '91097345' in fname:
                iif_location = 'dennis'
            logger.info(f"  Sending IIF file to import-iif endpoint ({len(att['data'])} bytes)")
            resp = requests.post(
                'http://127.0.0.1:8080/api/invoices/import-iif',
                json={'iif_data': att['data'].decode('utf-8', errors='replace'), 'location': iif_location},
                timeout=30,
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                vendor = result.get('vendor_name', '?')
                items = result.get('item_count', 0)
                logger.info(f'  IIF imported: {vendor} ({items} items) — auto-confirmed')
                saved_count += 1
            elif resp.status_code == 409:
                logger.info(f'  Duplicate IIF invoice — skipped')
                saved_count += 1
            else:
                logger.warning(f'  IIF import failed: {resp.status_code} {resp.text[:200]}')
        except Exception as e:
            logger.error(f'  IIF import error: {e}')

    if not image_attachments:
        return saved_count

    # Save image/PDF files to disk
    saved_files = []
    for idx, att in enumerate(image_attachments):
        filename = f"{location}_{ts_base}_{idx}.{att['ext']}"
        filepath = os.path.join(INTAKE_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(att['data'])
        logger.info(f"  Saved: {filename} ({len(att['data'])} bytes)")
        saved_files.append({'path': filepath, 'name': filename, 'mime': att['mime_type']})

    # Send to scan API — multiple images from same email go as one multi-page scan
    try:
        import requests
        files_dict = {}
        # First file is the main invoice image
        files_dict['file'] = (saved_files[0]['name'],
                              open(saved_files[0]['path'], 'rb'),
                              saved_files[0]['mime'])
        # Additional files as extra pages
        extra_handles = []
        for i, sf in enumerate(saved_files[1:]):
            fh = open(sf['path'], 'rb')
            extra_handles.append(fh)
            files_dict[f'extra_file_{i}'] = (sf['name'], fh, sf['mime'])

        if len(saved_files) > 1:
            logger.info(f"  Sending {len(saved_files)} pages as multi-page scan")

        try:
            resp = requests.post(
                'http://127.0.0.1:8080/api/invoices/scan',
                files=files_dict,
                data={'location': location},
                timeout=180
            )

            if resp.status_code in (200, 201):
                result = resp.json()
                status = result.get('status', 'unknown')
                vendor = (result.get('data') or {}).get('vendor_name', '?')
                items = len((result.get('data') or {}).get('items', []))
                logger.info(f'  OCR complete: {vendor} [{status}] ({items} items)')
            elif resp.status_code == 409:
                logger.info(f'  Duplicate invoice — skipped')
            else:
                logger.warning(f'  OCR failed: {resp.status_code} {resp.text[:200]}')
        finally:
            # Always close file handles
            files_dict['file'][1].close()
            for fh in extra_handles:
                fh.close()
    except Exception as e:
        logger.error(f'  OCR error: {e}')

    return saved_count + len(saved_files)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info(f"=== Email invoice poller run @ {datetime.now().isoformat()} ===")

    service = get_gmail_service()
    if not service:
        return

    manifest  = load_manifest()
    processed = manifest.get('processed', {})

    # Search for unread messages with attachments
    try:
        results = service.users().messages().list(
            userId='me', q='has:attachment is:unread', maxResults=50
        ).execute()
    except Exception as e:
        logger.error(f"Gmail API list error: {e}")
        return

    messages = results.get('messages', [])
    if not messages:
        logger.info("No unread emails with attachments.")
        return

    new_msgs = [m for m in messages if m['id'] not in processed]
    logger.info(f"Found {len(messages)} unread message(s) — {len(new_msgs)} new")

    if not new_msgs:
        logger.info("All messages already processed.")
        return

    saved_total = 0

    for msg_meta in new_msgs:
        msg_id = msg_meta['id']
        try:
            # Fetch headers only first (cheap)
            hdr_msg = service.users().messages().get(
                userId='me', id=msg_id, format='metadata',
                metadataHeaders=['To', 'Delivered-To', 'From', 'Subject', 'X-Original-To']
            ).execute()
            headers_list = hdr_msg.get('payload', {}).get('headers', [])
            headers_dict = {h['name']: h['value'] for h in headers_list}

            location = detect_location(headers_list)
            sender   = headers_dict.get('From', 'unknown')
            subject  = decode_mime_header(headers_dict.get('Subject', ''))

            logger.info(f"Processing [{location}] from {sender}: {subject[:60]}")

            count = save_attachments(service, msg_id, location)
            saved_total += count

            # Mark as read
            service.users().messages().modify(
                userId='me', id=msg_id,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()

            # Record in manifest immediately (survive crash on next msg)
            processed[msg_id] = datetime.now().isoformat()
            save_manifest({'processed': processed})

            logger.info(f"  -> {count} file(s) saved, marked as read")

        except Exception as e:
            logger.error(f"Error processing message {msg_id}: {e}")

    logger.info(f"=== Done: {saved_total} file(s) saved from {len(new_msgs)} email(s) ===")
    if saved_total > 0:
        logger.info("local_invoice_watcher.py will OCR new files within 5 minutes.")


if __name__ == '__main__':
    main()
