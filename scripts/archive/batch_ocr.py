#!/usr/bin/env python3
"""
Batch OCR — Process all unscanned invoice images in /opt/rednun/invoice_images/
Uses the existing invoice_processor.py functions.
Run from /opt/rednun with venv activated:
    source /opt/rednun/venv/bin/activate
    python batch_ocr.py
"""
import os
import sys
import time
import base64
import json
import logging
import sqlite3
from datetime import datetime

# Add project to path
sys.path.insert(0, '/opt/rednun')
os.chdir('/opt/rednun')

from invoice_processor import extract_invoice_data, save_invoice, get_connection, validate_invoice_extraction, confirm_invoice

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/rednun/batch_ocr.log')
    ]
)
logger = logging.getLogger(__name__)

IMAGE_DIR = '/opt/rednun/invoice_images'
DELAY_BETWEEN = 3  # seconds between API calls to avoid rate limits

MIME_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.pdf': 'application/pdf',
}


def get_already_scanned():
    """Get set of image paths already in the database."""
    conn = get_connection()
    rows = conn.execute("SELECT image_path FROM scanned_invoices WHERE image_path IS NOT NULL").fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_location_from_filename(filename):
    """Extract location from filename like 'dennis_20260212_...' or 'chatham_20260212_...'
    Returns 'chatham', 'dennis', or None if the filename has no recognizable prefix."""
    lower = filename.lower()
    if lower.startswith('chatham'):
        return 'chatham'
    elif lower.startswith('dennis'):
        return 'dennis'
    else:
        return None


def process_single(filepath, filename_location):
    """OCR and save a single invoice image. Returns (invoice_id, vendor) or raises.

    Location priority:
      1. OCR ship_to_address detection (most reliable)
      2. Filename prefix (dennis_* / chatham_*)
      3. 'unknown' — saved as-is so the dashboard flags it for manual assignment
    """
    ext = os.path.splitext(filepath)[1].lower()
    mime = MIME_TYPES.get(ext)
    if not mime:
        raise ValueError(f"Unsupported file type: {ext}")

    with open(filepath, 'rb') as f:
        raw = f.read()

    image_b64 = base64.b64encode(raw).decode('utf-8')

    # OCR via Claude
    data = extract_invoice_data(image_b64, mime_type=mime)

    # Prefer OCR-detected location, fall back to filename, then 'unknown'
    location = data.get('detected_location') or filename_location or 'unknown'
    if data.get('detected_location'):
        logger.info(f"  -> Location auto-detected from ship_to: {location}")
    elif filename_location:
        logger.info(f"  -> Location from filename: {location}")
    else:
        logger.warning(f"  -> Location unknown — neither OCR nor filename determined location")

    # Validate extraction for auto-confirm
    validation_result = validate_invoice_extraction(data)

    # Save to DB with validation data
    raw_json = json.dumps(data)
    invoice_id = save_invoice(location, data, image_path=filepath, raw_json=raw_json, validation_data=validation_result)

    vendor = data.get('vendor_name', 'Unknown')
    total = data.get('total', 0)
    items = len(data.get('line_items', []))

    # Auto-confirm if validation passed
    if validation_result.get('auto_confirm'):
        try:
            confirm_invoice(invoice_id)
            logger.info(f"  -> Auto-confirmed: {items} items, total ")
        except Exception as e:
            logger.warning(f"  -> Auto-confirm failed: {e}")
    else:
        if validation_result.get('issues'):
            logger.info(f"  -> Needs review: {', '.join(validation_result['issues'])}")
        else:
            logger.info(f"  -> Needs review (validation inconclusive)")

    return invoice_id, vendor, total, items


def main():
    logger.info("=" * 60)
    logger.info("BATCH OCR START")
    logger.info("=" * 60)

    # Get all image files
    all_files = sorted(os.listdir(IMAGE_DIR))
    all_paths = []
    for f in all_files:
        ext = os.path.splitext(f)[1].lower()
        if ext in MIME_TYPES:
            all_paths.append(os.path.join(IMAGE_DIR, f))

    # Filter out already scanned
    already = get_already_scanned()
    to_process = [p for p in all_paths if p not in already]

    logger.info(f"Total images: {len(all_paths)}")
    logger.info(f"Already scanned: {len(already)}")
    logger.info(f"To process: {len(to_process)}")

    if not to_process:
        logger.info("Nothing to process!")
        return

    successes = 0
    failures = 0
    results = []

    for i, filepath in enumerate(to_process):
        filename = os.path.basename(filepath)
        filename_location = get_location_from_filename(filename)
        logger.info(f"[{i+1}/{len(to_process)}] Processing: {filename} (filename_loc={filename_location or 'unknown'})")

        try:
            invoice_id, vendor, total, items = process_single(filepath, filename_location)
            logger.info(f"  -> OK: #{invoice_id} {vendor} ${total:.2f} ({items} items)")
            results.append({
                'file': filename,
                'status': 'ok',
                'invoice_id': invoice_id,
                'vendor': vendor,
                'total': total,
                'items': items,
            })
            successes += 1
        except Exception as e:
            logger.error(f"  -> FAILED: {e}")
            results.append({
                'file': filename,
                'status': 'error',
                'error': str(e),
            })
            failures += 1

        # Rate limit delay (skip after last one)
        if i < len(to_process) - 1:
            time.sleep(DELAY_BETWEEN)

    # Summary
    logger.info("=" * 60)
    logger.info(f"BATCH OCR COMPLETE: {successes} ok, {failures} failed out of {len(to_process)}")
    logger.info("=" * 60)

    # Save results summary
    summary = {
        'timestamp': datetime.now().isoformat(),
        'total': len(to_process),
        'successes': successes,
        'failures': failures,
        'results': results,
    }
    with open('/opt/rednun/batch_ocr_results.json', 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info("Results saved to /opt/rednun/batch_ocr_results.json")

    if failures:
        logger.info("Failed files:")
        for r in results:
            if r['status'] == 'error':
                logger.info(f"  {r['file']}: {r['error']}")


if __name__ == '__main__':
    main()
