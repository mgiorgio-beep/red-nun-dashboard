#!/usr/bin/env python3
"""
Validate the receipt classifier against the JSON samples we pulled from
dashboard@rednun.com (autopay_receipt_samples.json + tiger_sample.json).

Does NOT write to the DB and does NOT call Gmail. Pure offline test of
the parsing logic. Used to confirm we're extracting vendor/amount/invoice_no
correctly for every signature before going live.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/test_receipt_classifier.py
"""
import os
import sys
import json
import base64

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.invoices.receipt_classifier import (
    classify_message,
    RECEIPT_SIGNATURES,
)

SAMPLE_FILES = [
    "/opt/red-nun-dashboard/scripts/archive/autopay_receipt_samples.json",
    "/opt/red-nun-dashboard/scripts/archive/tiger_sample.json",
    "/opt/red-nun-dashboard/scripts/archive/forwarded_vendor_samples.json",
]


def reconstruct_message(stored: dict) -> dict:
    """
    Rebuild a Gmail-API-shaped message dict from the slimmed-down JSON we
    stored in the sample files. The classifier only reads .id, .payload.headers,
    and walks .payload.parts for text/plain body — so we fake exactly that.
    """
    headers = []
    for hdr_name in ("from", "to", "delivered_to", "subject", "date"):
        v = stored.get(hdr_name)
        if v:
            # Gmail uses Title-Case header names; classifier matches case-insensitively
            display = {"from": "From", "to": "To", "delivered_to": "Delivered-To",
                       "subject": "Subject", "date": "Date"}[hdr_name]
            headers.append({"name": display, "value": v})

    # Re-encode the truncated bodies so the classifier's base64 decoder works.
    text_body = (stored.get("text_body")
                 or stored.get("text_body_first_5k")
                 or stored.get("text_body_first_2k")
                 or "")
    html_body = (stored.get("html_body_first_5k")
                 or stored.get("html_body_first_4k")
                 or stored.get("html_body_first_2k")
                 or "")

    parts = []
    if text_body:
        parts.append({
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text_body.encode("utf-8")).decode("ascii")},
            "filename": "",
        })
    if html_body:
        parts.append({
            "mimeType": "text/html",
            "body": {"data": base64.urlsafe_b64encode(html_body.encode("utf-8")).decode("ascii")},
            "filename": "",
        })

    return {
        "id": stored.get("id", ""),
        "snippet": stored.get("snippet", ""),
        "payload": {
            "headers": headers,
            "parts": parts,
        },
    }


def iter_sample_messages():
    seen_ids = set()
    for path in SAMPLE_FILES:
        if not os.path.exists(path):
            print(f"  (skipping missing file: {path})")
            continue
        with open(path) as f:
            data = json.load(f)
        # Two file shapes: {"samples": {source: [{"messages": [...]}, ...]}}
        # OR {"messages": [...]} for the simpler forwarded_vendor_samples.json
        if "samples" in data:
            for source, batches in data["samples"].items():
                for batch in batches:
                    for msg in batch.get("messages", []):
                        mid = msg.get("id")
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        yield source, msg
        elif "messages" in data:
            for msg in data["messages"]:
                mid = msg.get("id")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                yield "forwarded", msg


def main():
    print("=" * 70)
    print("Receipt classifier validation against saved samples")
    print("=" * 70)
    print(f"Configured signatures: {[s['key'] for s in RECEIPT_SIGNATURES]}")
    print()

    matched = []
    skipped = []

    for source, stored in iter_sample_messages():
        msg = reconstruct_message(stored)
        result = classify_message(msg)

        subj = stored.get("subject", "")[:70]
        if result is None:
            skipped.append((source, subj, stored.get("from", "")))
            continue

        matched.append((source, result))
        print(f"[{result.signature_key}] {subj}")
        print(f"    vendor: {result.vendor_canonical}")
        print(f"    location: {result.location}")
        print(f"    total: {result.total_amount}")
        if result.line_items:
            print(f"    line items ({len(result.line_items)}):")
            for li in result.line_items:
                print(f"      - #{li.invoice_number}  ${li.amount}")
        else:
            print(f"    line items: (none — matcher will fuzzy-match on total)")
        print(f"    date: {result.payment_date}")
        print(f"    tier: {result.tier}  payment_method: {result.payment_method}")
        if result.parse_notes:
            print(f"    notes: {result.parse_notes}")
        print()

    print("-" * 70)
    print(f"Classified as receipts: {len(matched)}")
    print(f"Skipped (not a receipt): {len(skipped)}")
    print()

    if skipped:
        print("Skipped messages (sanity check — these should all be non-receipts):")
        for source, subj, frm in skipped:
            print(f"  [{source}] {subj[:60]}  ← {frm[:50]}")
        print()

    # Sanity-check coverage: each signature should match at least one sample
    seen_keys = {r.signature_key for _, r in matched}
    for sig in RECEIPT_SIGNATURES:
        if sig["key"] not in seen_keys:
            print(f"WARN: signature {sig['key']!r} did not match any sample message")

    # Flag any classified receipt with missing fields. For multi-invoice
    # signatures we expect both a total and at least one line item.
    incomplete = []
    for _, r in matched:
        if r.total_amount is None:
            incomplete.append((r, "no total_amount"))
            continue
        if r.signature_key in ("lknife_vtinfo", "colonial_vtinfo", "usfoods_ach_remit") and not r.line_items:
            incomplete.append((r, "expected line items for this signature"))
    if incomplete:
        print()
        print(f"WARN: {len(incomplete)} classified receipts have issues:")
        for r, why in incomplete:
            print(f"  - {r.signature_key} ({r.raw_subject[:60]}): {why}")


if __name__ == "__main__":
    main()
