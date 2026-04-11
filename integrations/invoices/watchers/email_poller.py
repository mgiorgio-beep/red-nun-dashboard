"""
Email Invoice Poller — Red Nun Analytics
Checks invoices@rednun.com via IMAP for invoice attachments.
Processes them through Claude Vision and saves to the database.

Location detection:
  - invoices+dennis@rednun.com → Dennis Port
  - invoices+chatham@rednun.com → Chatham
  - Subject contains "dennis" → Dennis Port
  - Subject contains "chatham" → Chatham
  - Default: Dennis Port

Run via cron every 5 minutes:
  */5 * * * * cd /opt/rednun && /opt/rednun/venv/bin/python email_poller.py >> /var/log/rednun_email.log 2>&1
"""

import os
import sys
import imaplib
import email
from email.header import decode_header
import base64
import logging
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

EMAIL_ADDRESS = os.getenv("INVOICE_EMAIL", "")
EMAIL_PASSWORD = os.getenv("INVOICE_EMAIL_APP_PASSWORD", "")
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

# Supported attachment types
SUPPORTED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",  # fallback for unnamed attachments
}


def decode_subject(msg):
    """Decode email subject from MIME encoding."""
    subject = msg.get("Subject", "")
    decoded = decode_header(subject)
    parts = []
    for part, enc in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return " ".join(parts)


def detect_location(msg):
    """
    Detect location from email address or subject.
    - invoices+dennis@rednun.com → dennis
    - invoices+chatham@rednun.com → chatham
    - Subject contains dennis/chatham
    - Default: dennis
    """
    # Check To/Delivered-To for +tag
    for header in ["Delivered-To", "To", "X-Original-To"]:
        to_addr = (msg.get(header) or "").lower()
        if "+dennis" in to_addr:
            return "dennis"
        if "+chatham" in to_addr:
            return "chatham"

    # Check subject
    subject = decode_subject(msg).lower()
    if "chatham" in subject:
        return "chatham"
    if "dennis" in subject:
        return "dennis"

    # Check from — some vendors only deliver to one location
    # Default to dennis
    return "dennis"


def get_attachments(msg):
    """Extract image/PDF attachments from an email message."""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp = str(part.get("Content-Disposition") or "")
            filename = part.get_filename() or ""

            # Check if it's an attachment we care about
            is_attachment = "attachment" in content_disp or "inline" in content_disp
            is_image = content_type.startswith("image/")
            is_pdf = content_type == "application/pdf"

            # Also catch images sent inline (like phone photos)
            if is_image or is_pdf or (is_attachment and content_type in SUPPORTED_TYPES):
                data = part.get_payload(decode=True)
                if data and len(data) > 1000:  # skip tiny images (signatures etc)
                    ext = SUPPORTED_TYPES.get(content_type, "jpg")
                    if filename.lower().endswith(".pdf"):
                        ext = "pdf"
                        content_type = "application/pdf"
                    attachments.append({
                        "data": data,
                        "mime_type": content_type,
                        "filename": filename or f"invoice.{ext}",
                        "ext": ext,
                    })
    else:
        # Single part message — check if it's an image
        content_type = msg.get_content_type()
        if content_type in SUPPORTED_TYPES:
            data = msg.get_payload(decode=True)
            if data and len(data) > 1000:
                ext = SUPPORTED_TYPES.get(content_type, "jpg")
                attachments.append({
                    "data": data,
                    "mime_type": content_type,
                    "filename": f"invoice.{ext}",
                    "ext": ext,
                })

    return attachments


def _auto_orient_page(img, page_num=0):
    """Auto-detect and fix rotated pages from ScanSnap scanning.
    Checks top/bottom for 180° flip, left/right for 90° rotation."""
    try:
        gray = img.convert('L')
        w, h = gray.size

        def _strip_var(crop_box):
            pixels = list(gray.crop(crop_box).getdata())
            mean = sum(pixels) / len(pixels)
            return sum((p - mean) ** 2 for p in pixels) / len(pixels)

        strip_h = int(h * 0.05)
        strip_w = int(w * 0.05)
        top_var = _strip_var((0, 0, w, strip_h))
        bot_var = _strip_var((0, h - strip_h, w, h))
        left_var = _strip_var((0, 0, strip_w, h))
        right_var = _strip_var((w - strip_w, 0, w, h))

        if left_var > right_var * 3 and left_var > 500:
            logger.info(f'  Page {page_num}: 90° rotation detected — rotating 90° CW')
            return img.rotate(-90, expand=True)
        if right_var > left_var * 3 and right_var > 500:
            logger.info(f'  Page {page_num}: 90° rotation detected — rotating 90° CCW')
            return img.rotate(90, expand=True)
        if bot_var > top_var * 2 and bot_var > 500:
            logger.info(f'  Page {page_num}: upside-down detected — rotating 180°')
            return img.rotate(180)
    except Exception as e:
        logger.warning(f'  Page {page_num} orientation check failed: {e}')
    return img


def process_email(msg):
    """Process a single email: extract attachments and run OCR."""
    from invoice_processor import (
        extract_invoice_data, save_invoice, init_invoice_tables,
    )

    subject = decode_subject(msg)
    sender = msg.get("From", "unknown")
    location = detect_location(msg)
    attachments = get_attachments(msg)

    if not attachments:
        logger.info(f"No invoice attachments in email from {sender}: {subject}")
        return 0

    logger.info(f"Processing {len(attachments)} attachment(s) from {sender} "
                f"[{subject}] → location: {location}")

    processed = 0
    for att in attachments:
        try:
            # Save image to disk
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            image_dir = os.path.join(os.path.dirname(__file__), "invoice_images")
            os.makedirs(image_dir, exist_ok=True)
            image_path = os.path.join(image_dir, f"{location}_{ts}_{processed}.{att['ext']}")

            with open(image_path, "wb") as f:
                f.write(att["data"])

            # Convert to base64 for Claude
            image_b64 = base64.b64encode(att["data"]).decode("utf-8")
            ocr_mime = att["mime_type"]
            extra_pages = []

            # Split multi-page PDFs into individual page images for Claude Vision
            if att["mime_type"] == "application/pdf":
                try:
                    from pdf2image import convert_from_bytes
                    from PIL import Image as _PILImage
                    import io as _io
                    pages = convert_from_bytes(att["data"], dpi=300, poppler_path='/usr/bin')
                    if len(pages) > 1:
                        logger.info(f"  Multi-page PDF: {len(pages)} pages — splitting for OCR")
                        # Auto-orient each page (ScanSnap duplex flips some pages 180°)
                        oriented = []
                        for pi, pg in enumerate(pages):
                            pg = _auto_orient_page(pg, pi + 1)
                            oriented.append(pg)
                        # Page 1 becomes main image
                        buf = _io.BytesIO()
                        oriented[0].save(buf, format='JPEG', quality=85)
                        image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                        ocr_mime = 'image/jpeg'
                        # Remaining pages become extra_pages
                        for pg in oriented[1:]:
                            buf2 = _io.BytesIO()
                            pg.save(buf2, format='JPEG', quality=85)
                            extra_pages.append({
                                'data': base64.b64encode(buf2.getvalue()).decode('utf-8'),
                                'mime': 'image/jpeg',
                            })
                        logger.info(f"  Split into {1 + len(extra_pages)} page images")
                except Exception as e:
                    logger.warning(f"  PDF page split failed: {e} — sending as-is")

            # Extract data via Claude Vision
            logger.info(f"  Running OCR on {att['filename']} ({ocr_mime})...")
            extracted = extract_invoice_data(image_b64, ocr_mime, extra_pages=extra_pages)

            # Save to database
            import json
            invoice_id = save_invoice(
                location, extracted,
                image_path=image_path,
                raw_json=json.dumps(extracted),
            )

            logger.info(f"  ✓ Saved invoice #{invoice_id}: "
                        f"{extracted.get('vendor_name', '?')} "
                        f"${extracted.get('total', 0)}")
            processed += 1

        except Exception as e:
            logger.error(f"  ✗ Failed to process {att['filename']}: {e}")

    return processed


def poll_inbox():
    """Connect to Gmail IMAP, check for new emails, process them."""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        logger.error("INVOICE_EMAIL or INVOICE_EMAIL_APP_PASSWORD not set in .env")
        return

    logger.info(f"Connecting to {IMAP_SERVER} as {EMAIL_ADDRESS}...")

    try:
        # Connect
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        status, _ = mail.select("Invoices")
        if status != "OK":
            mail.select("INBOX")

        # Search for unread emails sent to +invoice addresses
        # Check the Invoices label for unread emails
        status, messages = mail.search(None, 'UNSEEN')
        if status != "OK":
            logger.error(f"IMAP search failed: {status}")
            return

        email_ids = messages[0].split()
        if not email_ids:
            logger.info("No new emails")
            mail.logout()
            return

        logger.info(f"Found {len(email_ids)} new email(s)")

        total_processed = 0
        for eid in email_ids:
            try:
                # Fetch email
                status, data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(data[0][1])
                count = process_email(msg)
                total_processed += count

                # Mark as read (it's already marked by IMAP fetch)
                # Move to processed label
                try:
                    mail.store(eid, "+FLAGS", "\\Seen")
                    # Create and move to "Processed" label if it exists
                    try:
                        mail.copy(eid, "Processed")
                        mail.store(eid, "+FLAGS", "\\Deleted")
                    except:
                        pass  # Label might not exist, that's OK
                except:
                    pass

            except Exception as e:
                logger.error(f"Error processing email {eid}: {e}")

        # Expunge deleted messages
        try:
            mail.expunge()
        except:
            pass

        mail.logout()
        logger.info(f"Done. Processed {total_processed} invoice(s) from "
                     f"{len(email_ids)} email(s)")

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP error: {e}")
    except Exception as e:
        logger.error(f"Email polling error: {e}")


if __name__ == "__main__":
    # Initialize tables if needed
    from invoice_processor import init_invoice_tables
    init_invoice_tables()

    poll_inbox()
