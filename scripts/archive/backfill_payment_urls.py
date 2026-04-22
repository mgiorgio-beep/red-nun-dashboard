"""
One-off backfill: extract payment URLs from existing PDF invoices.

Runs `extract_payment_url_from_pdf()` over every scanned_invoices row whose
image_path points at a .pdf on disk and whose payment_url is currently NULL.
Manual user-entered URLs are preserved (NULL-only update).

Usage (on server):
    cd /opt/red-nun-dashboard
    sudo -u rednun venv/bin/python scripts/archive/backfill_payment_urls.py
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.toast.data_store import get_connection
from integrations.invoices.processor import extract_payment_url_from_pdf


def main():
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, vendor_name, image_path
        FROM scanned_invoices
        WHERE payment_url IS NULL
          AND image_path IS NOT NULL
          AND LOWER(image_path) LIKE '%.pdf'
    """).fetchall()

    print(f"Scanning {len(rows)} candidate PDF invoices...")
    filled = 0
    missing = 0
    skipped = 0

    for r in rows:
        inv_id = r["id"]
        vendor = r["vendor_name"]
        path = r["image_path"]
        if not os.path.exists(path):
            missing += 1
            continue
        url = extract_payment_url_from_pdf(path)
        if not url:
            skipped += 1
            continue
        conn.execute(
            "UPDATE scanned_invoices SET payment_url = ? WHERE id = ? AND payment_url IS NULL",
            (url, inv_id),
        )
        filled += 1
        print(f"  [{inv_id}] {vendor}: {url}")

    conn.commit()
    conn.close()

    print(f"\nDone. filled={filled}  no_link={skipped}  file_missing={missing}")


if __name__ == "__main__":
    main()
