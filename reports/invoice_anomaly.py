"""
Invoice Anomaly Detector — Red Nun Analytics

Runs once per invoice on confirm. Sends invoice summary to Claude Haiku
for plain-English anomaly alerts (price spikes, unusual quantities, new fees).

Cost: ~$0.01 per invoice. Skips automatically when < 2 prior invoices
from the same vendor (no meaningful history to compare against).
"""

import os
import json
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def analyze_invoice_for_anomalies(invoice_id, conn):
    """
    Analyze a confirmed invoice for anomalies using Claude Haiku.

    Skips if < 2 prior confirmed invoices from the same vendor.
    Stores result in scanned_invoices.anomaly_alert.

    Returns: alert string or None
    """
    # Get invoice header
    invoice = conn.execute(
        "SELECT id, vendor_name, invoice_number, total FROM scanned_invoices WHERE id = ?",
        (invoice_id,)
    ).fetchone()

    if not invoice:
        logger.warning(f"Invoice {invoice_id} not found for anomaly check")
        return None

    vendor_name = invoice["vendor_name"]
    if not vendor_name:
        return None

    # Count prior confirmed invoices from same vendor
    prior_count = conn.execute("""
        SELECT COUNT(*) FROM scanned_invoices
        WHERE vendor_name = ? AND status = 'confirmed' AND id != ?
    """, (vendor_name, invoice_id)).fetchone()[0]

    if prior_count < 2:
        logger.info(
            f"Skipping anomaly check for invoice {invoice_id}: "
            f"only {prior_count} prior invoices from {vendor_name}"
        )
        return None

    # Get current invoice line items
    items = conn.execute("""
        SELECT product_name, quantity, unit_price, total_price, unit
        FROM scanned_invoice_items
        WHERE invoice_id = ?
    """, (invoice_id,)).fetchall()

    if not items:
        return None

    # Get last 5 confirmed invoices from same vendor for comparison
    prior_invoices = conn.execute("""
        SELECT si.id, si.invoice_date, si.total,
               GROUP_CONCAT(sii.product_name || ' x' || sii.quantity || ' @$' || sii.unit_price, '; ') as items_summary
        FROM scanned_invoices si
        LEFT JOIN scanned_invoice_items sii ON sii.invoice_id = si.id
        WHERE si.vendor_name = ? AND si.status = 'confirmed' AND si.id != ?
        GROUP BY si.id
        ORDER BY si.confirmed_at DESC
        LIMIT 5
    """, (vendor_name, invoice_id)).fetchall()

    # Build compact item list for current invoice
    item_lines = []
    for item in items:
        name = item["product_name"] or "Unknown"
        qty = item["quantity"] or 0
        price = item["unit_price"] or 0
        item_lines.append(f"- {name}: qty {qty}, ${price:.2f}/ea")

    items_text = "\n".join(item_lines)

    # Build history summary
    history_lines = []
    for inv in prior_invoices:
        date = inv["invoice_date"] or "unknown"
        total = inv["total"] or 0
        summary = inv["items_summary"] or "no items"
        # Truncate long summaries
        if len(summary) > 300:
            summary = summary[:300] + "..."
        history_lines.append(f"- {date}: total ${total:.2f} | {summary}")

    history_text = "\n".join(history_lines) if history_lines else "No history available"

    # Call Claude Haiku
    prompt = f"""New invoice from {vendor_name}, total ${invoice['total'] or 0:.2f}.
Line items:
{items_text}

Recent history from this vendor:
{history_text}

Flag ONLY:
1. Items where quantity is unusual vs history (>50% different)
2. Price increases >8% vs last seen price for same item
3. Line items never seen before on this vendor
4. Any fees or surcharges not on previous invoices

If nothing is anomalous, respond: NO_ANOMALIES
Otherwise respond in 2-3 plain English sentences max."""

    try:
        if not ANTHROPIC_API_KEY:
            logger.warning("No ANTHROPIC_API_KEY set, skipping anomaly check")
            return None

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system": "You are an invoice auditor for a restaurant. Be concise. Only flag real anomalies -- don't flag normal variation.",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(f"Haiku API error {resp.status_code}: {resp.text[:300]}")
            return None

        result = resp.json()
        text = result.get("content", [{}])[0].get("text", "").strip()

        if not text or "NO_ANOMALIES" in text.upper():
            alert = None
        else:
            alert = text

        # Store result in DB
        conn.execute(
            "UPDATE scanned_invoices SET anomaly_alert = ? WHERE id = ?",
            (alert, invoice_id)
        )
        conn.commit()

        if alert:
            logger.info(f"Anomaly detected for invoice {invoice_id}: {alert[:100]}")
        else:
            logger.info(f"No anomalies for invoice {invoice_id}")

        return alert

    except requests.Timeout:
        logger.warning(f"Haiku timeout for invoice {invoice_id}")
        return None
    except Exception as e:
        logger.error(f"Anomaly check error for invoice {invoice_id}: {e}")
        return None
