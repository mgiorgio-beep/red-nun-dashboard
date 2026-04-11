#!/usr/bin/env python3
"""
Local Invoice Watcher — Monitor /opt/rednun/invoice_images/ for new files
and trigger OCR via invoice_processor.py.

Replaces drive_invoice_watcher.py for the Beelink local deployment.
Tracks processed files in a manifest so it never re-processes the same file.

Run via cron every 5 minutes:
  */5 * * * * cd /opt/rednun && source venv/bin/activate && python local_invoice_watcher.py >> /opt/rednun/invoice_watcher.log 2>&1
"""
import os
import sys
import json
import base64
import logging
import time
from datetime import datetime

sys.path.insert(0, '/opt/rednun')
os.chdir('/opt/rednun')

from invoice_processor import extract_invoice_data, save_invoice, validate_invoice_extraction, confirm_invoice

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

INTAKE_DIR = '/opt/rednun/invoice_images'
MANIFEST_PATH = '/opt/rednun/.invoice_watcher_manifest.json'
DELAY_BETWEEN = 3  # seconds between API calls to avoid rate limits

SUPPORTED_TYPES = {
    '.pdf':  'application/pdf',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png':  'image/png',
    '.webp': 'image/webp',
    '.heic': 'image/heic',
}


def load_manifest():
    """Load the processed-files manifest. Returns dict with 'processed' key."""
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read manifest ({e}), starting fresh")
    return {"processed": {}}


def save_manifest(manifest):
    """Write the manifest atomically via a temp file."""
    tmp = MANIFEST_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, MANIFEST_PATH)


def get_location_from_filename(filename):
    """Infer location from filename prefix: dennis_* or chatham_*.
    Returns 'chatham', 'dennis', or None if no recognizable prefix."""
    lower = filename.lower()
    if lower.startswith('chatham'):
        return 'chatham'
    elif lower.startswith('dennis'):
        return 'dennis'
    else:
        return None


def process_file(filepath, filename_location):
    """
    OCR and save one invoice file.
    Returns (invoice_id, vendor_name, total, item_count) on success.
    Raises ValueError for unreadable files or duplicates.

    Location priority:
      1. OCR ship_to_address detection (most reliable)
      2. Filename prefix passed in as filename_location
      3. 'unknown' — saved as-is so the dashboard flags it for manual assignment
    """
    ext = os.path.splitext(filepath)[1].lower()
    mime = SUPPORTED_TYPES[ext]

    with open(filepath, 'rb') as f:
        raw = f.read()

    image_b64 = base64.b64encode(raw).decode('utf-8')

    # OCR via Claude Vision (same pattern as batch_ocr.py)
    data = extract_invoice_data(image_b64, mime_type=mime)

    if data.get('confidence_score', 0) == 0:
        raise ValueError("OCR confidence=0 — not a readable invoice")

    # Prefer OCR-detected location, fall back to filename, then 'unknown'
    location = data.get('detected_location') or filename_location or 'unknown'
    if data.get('detected_location'):
        logger.info(f"  -> Location auto-detected from ship_to: {location}")
    elif filename_location:
        logger.info(f"  -> Location from filename: {location}")
    else:
        logger.warning(f"  -> Location unknown — invoice will need manual location assignment")

    # Validate extraction for auto-confirm
    validation_result = validate_invoice_extraction(data)

    raw_json = json.dumps(data)
    result = save_invoice(location, data, image_path=filepath, raw_json=raw_json, validation_data=validation_result)

    # Duplicate detection — save_invoice returns a dict if duplicate found
    if isinstance(result, dict) and result.get('duplicate'):
        existing = result.get('existing_invoice', {})
        raise ValueError(
            f"Duplicate: already saved as invoice #{result.get('existing_id')} "
            f"({existing.get('vendor_name', 'unknown vendor')})"
        )

    invoice_id = result
    vendor = data.get('vendor_name', 'Unknown')
    total = data.get('total', 0) or 0
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
    logger.info(f"=== Local invoice watcher run @ {datetime.now().isoformat()} ===")
    logger.info(f"Scanning: {INTAKE_DIR}")

    manifest = load_manifest()
    processed = manifest.get('processed', {})

    # Enumerate all supported files in the intake folder
    try:
        all_files = sorted(os.listdir(INTAKE_DIR))
    except FileNotFoundError:
        logger.error(f"Intake directory not found: {INTAKE_DIR}")
        return

    new_files = []
    skipped = 0
    for filename in all_files:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_TYPES:
            continue
        if filename in processed:
            skipped += 1
        else:
            new_files.append(filename)

    logger.info(f"Files: {len(new_files)} new, {skipped} already processed")

    if not new_files:
        logger.info("Nothing to process.")
        return

    successes = 0
    errors = 0

    for i, filename in enumerate(new_files):
        filepath = os.path.join(INTAKE_DIR, filename)
        filename_location = get_location_from_filename(filename)
        logger.info(f"[{i+1}/{len(new_files)}] {filename} (filename_loc={filename_location or 'unknown'})")

        try:
            invoice_id, vendor, total, items = process_file(filepath, filename_location)
            logger.info(f"  -> OK: invoice #{invoice_id} — {vendor} ${total:.2f} ({items} items)")
            # Mark as processed immediately so a crash on the next file doesn't re-queue this one
            processed[filename] = datetime.now().isoformat()
            save_manifest({'processed': processed})
            successes += 1
        except Exception as e:
            logger.error(f"  -> ERROR: {e}")
            errors += 1

        # Brief delay between API calls (skip after last file)
        if i < len(new_files) - 1:
            time.sleep(DELAY_BETWEEN)

    logger.info(
        f"=== Done: {successes} processed, {errors} error(s), {skipped} skipped ==="
    )


if __name__ == '__main__':
    main()
