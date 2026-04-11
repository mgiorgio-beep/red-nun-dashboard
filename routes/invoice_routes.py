"""
Invoice Scanner API Routes — Red Nun Analytics
Adds endpoints for uploading, processing, reviewing, and confirming invoices.
Mounts under /api/invoices/* in the main Flask app.
"""

import os
import io
import json
import base64
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, send_from_directory
from PIL import Image

from integrations.invoices.processor import (
    init_invoice_tables,
    extract_invoice_data,
    verify_math_errors,
    fix_subtotal_includes_tax,
    validate_invoice_extraction,
    save_invoice,
    confirm_invoice,
    delete_invoice,
    get_invoice,
    get_invoices,
    get_price_changes,
    get_spending_summary,
    get_price_alerts_for_invoice,
    mark_invoice_paid,
    get_outstanding_invoices,
    get_payment_summary,
    parse_iif_invoice,
    parse_csv_invoice,
    parse_pfg_csv_invoice,
    parse_vtinfo_csv_invoice,
    generate_csv_thumbnail,
)
from integrations.toast.data_store import get_connection
from integrations.vendors.vendor_item_matcher import process_invoice_items
from reports.invoice_anomaly import analyze_invoice_for_anomalies
from integrations.recipes.recipe_costing import cost_all_recipes
from integrations.toast.data_store import get_connection as _get_conn
import threading

def _run_cost_all():
    conn = _get_conn()
    try:
        cost_all_recipes(conn)
    finally:
        conn.close()

logger = logging.getLogger(__name__)


def auto_rotate_image(image_data, mime_type):
    """Apply EXIF orientation for phone photos. Does NOT force landscape→portrait
    since many invoices (US Foods dot-matrix) are natively landscape.
    The rotation retry logic handles misoriented images during OCR."""
    if mime_type == "application/pdf":
        return image_data
    try:
        img = Image.open(io.BytesIO(image_data))
        from PIL import ImageOps
        original_size = img.size
        img = ImageOps.exif_transpose(img)
        if img.size != original_size:
            buf = io.BytesIO()
            fmt = "PNG" if mime_type == "image/png" else "JPEG"
            img.save(buf, format=fmt, quality=92)
            logger.info(f"EXIF-rotated image from {original_size[0]}x{original_size[1]} to {img.size[0]}x{img.size[1]}")
            return buf.getvalue()
    except Exception as e:
        logger.warning(f"Auto-rotate failed: {e}")
    return image_data


def _auto_orient_page(img, page_num=0):
    """Auto-detect and fix rotated pages from ScanSnap scanning.
    Checks top/bottom strips for 180° flip and left/right strips for 90° rotation.
    Documents have header text at top (high variance) and blank margin at bottom."""
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

        # Check 90° rotation (ScanSnap auto-rotate turns landscape pages to portrait)
        # High left + low right = original top is on left → rotate 90° CW to fix
        if left_var > right_var * 3 and left_var > 500:
            logger.info(f'Page {page_num}: 90° rotation detected (left_var={left_var:.0f}, right_var={right_var:.0f}) — rotating 90° CW')
            return img.rotate(-90, expand=True)
        # High right + low left = original top is on right → rotate 90° CCW to fix
        if right_var > left_var * 3 and right_var > 500:
            logger.info(f'Page {page_num}: 90° rotation detected (right_var={right_var:.0f}, left_var={left_var:.0f}) — rotating 90° CCW')
            return img.rotate(90, expand=True)
        # Check 180° flip (duplex scanning)
        if bot_var > top_var * 2 and bot_var > 500:
            logger.info(f'Page {page_num}: upside-down detected (top_var={top_var:.0f}, bot_var={bot_var:.0f}) — rotating 180°')
            return img.rotate(180)
    except Exception as e:
        logger.warning(f'Page {page_num} orientation check failed: {e}')
    return img


invoice_bp = Blueprint("invoices", __name__)

# Directory for storing invoice images
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "invoice_images")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@invoice_bp.route("/api/invoices/scan", methods=["POST"])
def api_scan_invoice():
    """
    Upload and scan an invoice image.

    Accepts:
        - multipart/form-data with 'image' file field
        - OR JSON with 'image' as base64 string

    Query params:
        - location: 'dennis' or 'chatham' (required)
    """
    location = request.args.get("location") or request.form.get("location", "dennis")

    try:
        image_b64 = None
        mime_type = "image/jpeg"
        image_path = None

        if request.content_type and "multipart" in request.content_type:
            # File upload
            file = request.files.get("file") or request.files.get("image")
            if not file:
                return jsonify({"error": "No image file provided"}), 400

            # Read file and determine mime type BEFORE auto-rotate
            image_data = file.read()
            fname = file.filename or ""
            if fname.lower().endswith(".png"):
                mime_type = "image/png"
            elif fname.lower().endswith(".webp"):
                mime_type = "image/webp"
            elif fname.lower().endswith(".heic"):
                mime_type = "image/heic"
            elif fname.lower().endswith(".pdf"):
                mime_type = "application/pdf"

            image_data = auto_rotate_image(image_data, mime_type)
            image_b64 = base64.b64encode(image_data).decode("utf-8")

            # Save image
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = fname.rsplit(".", 1)[-1] if "." in fname else "jpg"
            image_path = os.path.join(UPLOAD_DIR, f"{location}_{ts}.{ext}")
            with open(image_path, "wb") as f:
                f.write(image_data)

        else:
            # JSON with base64
            data = request.get_json(silent=True) or {}
            image_b64 = data.get("image")
            mime_type = data.get("mime_type", "image/jpeg")

            if not image_b64:
                return jsonify({"error": "No image data provided"}), 400

            # Save image
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            image_path = os.path.join(UPLOAD_DIR, f"{location}_{ts}.jpg")
            with open(image_path, "wb") as f:
                f.write(base64.b64decode(image_b64))

        # If PDF with multiple pages, split into pages for Claude Vision
        if mime_type == 'application/pdf':
            try:
                from pdf2image import convert_from_bytes
                import io
                pdf_bytes = base64.b64decode(image_b64)
                pages = convert_from_bytes(pdf_bytes, dpi=300, poppler_path='/usr/bin')
                if len(pages) > 1:
                    logger.info(f'Multi-page PDF detected: {len(pages)} pages — sending as native PDF document')
                    # Send entire PDF as native document — Claude reads all pages natively
                    # This is much more reliable than splitting into separate JPEG images
                pdf_extra = []
            except Exception as e:
                logger.warning(f'PDF page split failed: {e} — sending as-is')
                pdf_extra = []
        else:
            pdf_extra = []

        # Auto-orient main image if it's a direct JPEG/PNG upload (not from PDF split)
        if mime_type != 'application/pdf' and not pdf_extra:
            try:
                main_img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
                main_img = _auto_orient_page(main_img, 1)
                buf = io.BytesIO()
                fmt = 'PNG' if mime_type == 'image/png' else 'JPEG'
                main_img.save(buf, format=fmt, quality=85)
                image_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            except Exception as e:
                logger.warning(f'Main image orient check failed: {e}')

        # Collect any extra pages
        extra_pages = list(pdf_extra)
        i = 0
        while True:
            ef = request.files.get(f'extra_file_{i}')
            if not ef:
                break
            ef_data = ef.read()
            ef_fname = ef.filename or ''
            ef_mime = 'image/jpeg'
            if ef_fname.lower().endswith('.pdf'): ef_mime = 'application/pdf'
            elif ef_fname.lower().endswith('.png'): ef_mime = 'image/png'
            elif ef_fname.lower().endswith('.heic'): ef_mime = 'image/heic'
            # Auto-orient extra pages too
            try:
                ef_img = Image.open(io.BytesIO(ef_data))
                ef_img = _auto_orient_page(ef_img, i + 2)
                ef_buf = io.BytesIO()
                ef_fmt = 'PNG' if ef_mime == 'image/png' else 'JPEG'
                ef_img.save(ef_buf, format=ef_fmt, quality=85)
                ef_data = ef_buf.getvalue()
            except Exception as e:
                logger.warning(f'Extra page {i} orient check failed: {e}')
            extra_pages.append({'data': base64.b64encode(ef_data).decode('utf-8'), 'mime': ef_mime})
            i += 1
        # Extract data using Claude Vision
        logger.info(f"Processing invoice for {location}, extra pages: {len(extra_pages)}...")
        extracted = extract_invoice_data(image_b64, mime_type, extra_pages=extra_pages)

        # Score extraction quality — higher is better
        def _extraction_score(ex):
            score = 0
            vendor = (ex.get("vendor_name") or "").strip()
            inv_num = ex.get("invoice_number")
            inv_date = ex.get("invoice_date")
            subtotal = float(ex.get("subtotal") or ex.get("invoice_subtotal") or 0)
            total = float(ex.get("total") or ex.get("invoice_total") or 0)
            items = ex.get("line_items", [])
            # Key fields present
            if vendor and len(vendor) >= 3: score += 10
            if inv_num: score += 10
            if inv_date: score += 10
            if subtotal > 0 or total > 0: score += 10
            # Line items quality
            score += min(len(items), 30) * 2  # up to 60 points for items (capped at 30)
            # Items sum vs invoice total — closer is better
            items_sum = sum(float(it.get("total_price") or 0) for it in items)
            inv_total = total or subtotal
            if inv_total > 0 and items_sum > 0:
                ratio = items_sum / inv_total
                if 0.8 <= ratio <= 1.2:
                    score += 20  # items match total well
                elif 0.5 <= ratio <= 1.5:
                    score += 10
            elif items_sum > 0:
                score += 5  # at least got some prices
            return score

        def _extraction_needs_rotation(ex):
            """Check if extraction is poor enough to warrant trying rotations."""
            items = ex.get("line_items", [])
            vendor = (ex.get("vendor_name") or "").strip()
            subtotal = float(ex.get("subtotal") or ex.get("invoice_subtotal") or 0)
            total = float(ex.get("total") or ex.get("invoice_total") or 0)
            inv_total = total or subtotal
            items_sum = sum(float(it.get("total_price") or 0) for it in items)
            # Case 1: Missing most key fields (original check)
            missing = 0
            if not vendor or len(vendor) < 3: missing += 1
            if not ex.get("invoice_number"): missing += 1
            if not ex.get("invoice_date"): missing += 1
            if inv_total == 0 and len(items) > 0: missing += 1
            if missing >= 3:
                return True
            # Case 2: Very few items for the dollar amount (e.g. $700 but only 4 items from a food distributor)
            if len(items) <= 5 and inv_total > 300:
                return True
            # Case 3: Items sum is way off from invoice total
            if inv_total > 0 and items_sum > 0:
                ratio = items_sum / inv_total
                if ratio < 0.5 or ratio > 2.0:
                    return True
            # Case 4: No items at all
            if len(items) == 0:
                return True
            # Case 5: Items have $0 prices (couldn't read prices = likely rotated)
            zero_price_items = sum(1 for it in items if float(it.get("total_price") or 0) == 0)
            if len(items) >= 2 and zero_price_items >= len(items) * 0.5:
                return True
            return False

        if _extraction_needs_rotation(extracted) and mime_type != "application/pdf":
            orig_score = _extraction_score(extracted)
            logger.info(f"Extraction may be poor (score={orig_score}) — trying rotated orientations...")
            best_score = orig_score
            best_extracted = extracted
            best_b64 = image_b64
            try:
                img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
                fmt = "PNG" if mime_type == "image/png" else "JPEG"
                # Try 90° CW (rotate -90), 90° CCW (rotate 90), and 180°
                for angle, label in [(-90, "90° CW"), (90, "90° CCW"), (180, "180°")]:
                    img_rotated = img.rotate(angle, expand=True)
                    buf = io.BytesIO()
                    img_rotated.save(buf, format=fmt, quality=92)
                    rotated_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                    extracted_rotated = extract_invoice_data(rotated_b64, mime_type, extra_pages=extra_pages)
                    rot_score = _extraction_score(extracted_rotated)
                    logger.info(f"  {label} rotation score={rot_score} (items={len(extracted_rotated.get('line_items', []))})")
                    if rot_score > best_score:
                        best_score = rot_score
                        best_extracted = extracted_rotated
                        best_b64 = rotated_b64
                    # If we found a good result, stop early
                    if best_score >= 80:
                        break
            except Exception as re:
                logger.warning(f"Rotation retry failed: {re}")
            if best_b64 != image_b64:
                logger.info(f"Using rotated result (score={best_score} vs original={orig_score})")
                extracted = best_extracted
                image_b64 = best_b64
                # Save the rotated image over the original
                if image_path and os.path.exists(image_path):
                    with open(image_path, "wb") as f:
                        f.write(base64.b64decode(best_b64))
            else:
                logger.info(f"Original orientation was best (score={orig_score})")

        # Check if image is unreadable/not an invoice
        confidence = extracted.get("confidence_score", 0)
        if confidence == 0:
            # Delete the saved image file
            if image_path and os.path.exists(image_path):
                os.remove(image_path)

            return jsonify({
                "status": "unreadable",
                "message": "Image is unreadable or does not appear to be an invoice. Please retake the photo or upload a clearer image.",
                "confidence_score": 0
            }), 400

        # Per-line math validation: flag items where qty × unit_price ≠ total_price
        vendor_lower = (extracted.get("vendor_name") or "").lower()
        is_beer_vendor = any(v in vendor_lower for v in ["colonial", "l. knife", "craft collective"])
        def _flag_math_errors(items):
            count = 0
            for item in items:
                qty = float(item.get("quantity", 0) or 0)
                up = float(item.get("unit_price", 0) or 0)
                tp = float(item.get("total_price", 0) or 0)
                expected = round(qty * up, 2)
                diff = abs(expected - tp)
                if diff > 0.02 and qty > 0 and up > 0:
                    # Beer distributors include deposits in total (kegs, bottles)
                    if is_beer_vendor and tp > expected:
                        item["math_error"] = False
                    else:
                        item["math_error"] = True
                        count += 1
                else:
                    item["math_error"] = False
            return count

        items = extracted.get("line_items", [])

        # Filter out subtotal/total rows that OCR mistakenly included as line items
        invoice_total_val = float(extracted.get("invoice_total") or extracted.get("total") or 0)
        invoice_subtotal_val = float(extracted.get("invoice_subtotal") or extracted.get("subtotal") or 0)
        def _is_summary_row(item):
            tp = float(item.get("total_price", 0) or 0)
            name = (item.get("product_name") or "").lower()
            summary_keywords = ["subtotal", "sub total", "sub-total", "merchandise total", "balance due", "amount due", "invoice total", "grand total", "total dry", "total ref", "total frozen", "total chilled", "storage location", "storage location recap", "items shipped", "product total", "order total", "category total", "location recap", "recap"]
            if tp > 0 and (tp == invoice_total_val or tp == invoice_subtotal_val):
                # Only filter if name looks like a summary OR there are other items
                # (single-item invoices often have item total == invoice total)
                if any(kw in name for kw in summary_keywords) or len(items) > 3:
                    return True
            if any(kw in name for kw in summary_keywords):
                return True
            # Filter US Foods category subtotal rows (qty=0 or no product number)
            product_num = str(item.get("product_number") or item.get("sku") or "").strip()
            qty = float(item.get("quantity", 0) or 0)
            if qty == 0 and not product_num and tp > 0:
                return True
            return False
        filtered = [it for it in items if not _is_summary_row(it)]
        if len(filtered) < len(items):
            logger.info(f"Removed {len(items) - len(filtered)} summary row(s) from line items")
            extracted["line_items"] = filtered
            items = filtered

        # Filter out prompt pay discount line items (Martignetti, SG)
        def _is_prompt_pay(item):
            name = (item.get("product_name") or "").lower()
            return "prompt pay" in name or "prompt-pay" in name or "ppd discount" in name
        ppd_filtered = [it for it in items if not _is_prompt_pay(it)]
        if len(ppd_filtered) < len(items):
            logger.info(f"Removed {len(items) - len(ppd_filtered)} prompt pay discount row(s)")
            extracted["line_items"] = ppd_filtered
            items = ppd_filtered

        line_item_errors = _flag_math_errors(items)

        # Invoice-level total cross-check: sum of line items vs printed subtotal (pre-tax)
        items_sum = round(sum(float(it.get("total_price", 0) or 0) for it in items), 2)
        # Prefer invoice_subtotal (pre-tax) over invoice_total (which may include tax)
        invoice_subtotal = float(extracted.get("invoice_subtotal") or extracted.get("subtotal") or 0)
        invoice_total = float(extracted.get("invoice_total") or extracted.get("total") or 0)
        # Use subtotal for comparison if available, otherwise fall back to total minus tax
        invoice_tax = float(extracted.get("tax") or 0)
        if invoice_subtotal > 0:
            compare_total = invoice_subtotal
        elif invoice_total > 0 and invoice_tax > 0:
            compare_total = round(invoice_total - invoice_tax, 2)
        else:
            compare_total = invoice_total
        total_gap = round(abs(items_sum - compare_total), 2) if compare_total > 0 else 0
        needs_verify = line_item_errors > 0 or total_gap > 1.00

        if needs_verify:
            logger.info(f"OCR issues: {line_item_errors} math errors, total gap ${total_gap} (items sum ${items_sum} vs invoice ${invoice_total}). Running verification pass...")
            try:
                extracted = verify_math_errors(image_b64, mime_type, extracted, extra_pages=extra_pages)
                line_item_errors = _flag_math_errors(extracted.get("line_items", []))
            except Exception as ve:
                logger.warning(f"Verification pass failed: {ve}")

        extracted["line_item_errors"] = line_item_errors
        # Store total cross-check for UI display
        items_sum = round(sum(float(it.get("total_price", 0) or 0) for it in extracted.get("line_items", [])), 2)
        extracted["_items_sum"] = items_sum
        extracted["_total_gap"] = round(abs(items_sum - invoice_total), 2) if invoice_total > 0 else 0

        # Fix subtotal=total when tax>0 (Cintas, service invoices)
        extracted = fix_subtotal_includes_tax(extracted)

        # Validate extraction for auto-confirm eligibility
        import json
        extracted["_mime_type"] = mime_type
        validation_result = validate_invoice_extraction(extracted)
        # If verification pass ran, never auto-confirm — force manual review
        if needs_verify and validation_result.get("auto_confirm") and mime_type != "application/pdf":
            validation_result["auto_confirm"] = False
            validation_result["issues"].append("OCR required correction pass — please review")
        logger.info(f"Validation result: auto_confirm={validation_result['auto_confirm']}, issues={validation_result['issues']}")

        # Auto-detect location from ship-to address
        ship_to = (extracted.get("ship_to_address") or "").lower()
        vendor_name = (extracted.get("vendor_name") or "").lower()
        all_text = ship_to + " " + vendor_name + " " + (extracted.get("notes") or "").lower()
        if "chatham" in all_text:
            if location != "chatham":
                logger.info(f"Auto-detected Chatham location from invoice text (was '{location}')")
            location = "chatham"
        elif "dennis" in all_text:
            if location != "dennis":
                logger.info(f"Auto-detected Dennis location from invoice text (was '{location}')")
            location = "dennis"

        # Save to database with validation data
        result = save_invoice(
            location, extracted,
            image_path=image_path,
            raw_json=json.dumps(extracted),
            validation_data=validation_result,
        )

        # Check for duplicate
        if isinstance(result, dict) and result.get("duplicate"):
            return jsonify({
                "status": "duplicate",
                "message": f"Duplicate invoice detected: {result['existing_invoice']['vendor_name']} #{result['existing_invoice']['invoice_number']} already exists.",
                "existing_invoice": result["existing_invoice"],
                "existing_id": result["existing_id"]
            }), 409

        invoice_id = result

        # Auto-confirm if validation passed
        if validation_result.get("auto_confirm"):
            try:
                confirm_invoice(invoice_id)
                logger.info(f"Invoice #{invoice_id} auto-confirmed: {validation_result['item_count_extracted']} items, total ${validation_result['total_extracted']:.2f}")
                
                # Build success message
                msg_parts = []
                if validation_result.get("item_count_match") is True:
                    msg_parts.append(f"{validation_result['item_count_extracted']}/{validation_result['item_count_invoice']} items")
                elif validation_result.get("item_count_extracted"):
                    msg_parts.append(f"{validation_result['item_count_extracted']} items")
                
                if validation_result.get("total_match") is True:
                    msg_parts.append(f"total matched within ${validation_result['total_difference']:.2f}")
                
                success_msg = f"Invoice auto-confirmed ✅ ({', '.join(msg_parts)})" if msg_parts else "Invoice auto-confirmed ✅"
                
                # Run vendor item matching + anomaly detection
                match_counts = {"auto_matched": 0, "suggestions": 0, "new_products": 0}
                anomaly_alert = None
                try:
                    conn = get_connection()
                    match_counts = process_invoice_items(invoice_id, conn)
                    anomaly_alert = analyze_invoice_for_anomalies(invoice_id, conn)
                    conn.close()
                except Exception as me:
                    logger.error(f"Post-auto-confirm processing error: {me}", exc_info=True)

                # Recalculate recipe costs in background
                threading.Thread(target=_run_cost_all, daemon=True).start()

                invoice = get_invoice(invoice_id)
                invoice["validation"] = validation_result
                return jsonify({
                    "status": "auto_confirmed",
                    "invoice_id": invoice_id,
                    "data": invoice,
                    "message": success_msg,
                    "validation": validation_result,
                    "anomaly_alert": anomaly_alert,
                    "auto_matched": match_counts.get("auto_matched", 0),
                    "suggestions": match_counts.get("suggestions", 0),
                    "new_products": match_counts.get("new_products", 0),
                })
            except Exception as e:
                logger.error(f"Auto-confirm failed for invoice #{invoice_id}: {e}")
                # Fall through to manual review

        # Needs manual review
        invoice = get_invoice(invoice_id)
        # Include detected_location from OCR so the UI can auto-select it
        if extracted.get("detected_location"):
            invoice["detected_location"] = extracted["detected_location"]
        
        invoice["validation"] = validation_result
        
        # Build warning message if validation failed
        message = "Invoice scanned successfully. Please review and confirm."
        if validation_result.get("issues"):
            message = "⚠️ " + " • ".join(validation_result["issues"]) + " — Please review and confirm."
        
        return jsonify({
            "status": "needs_review",
            "invoice_id": invoice_id,
            "data": invoice,
            "message": message,
            "validation": validation_result
        })

    except Exception as e:
        logger.error(f"Invoice scan error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/import-iif", methods=["POST"])
def api_import_iif():
    """
    Import a US Foods IIF (QuickBooks Interchange Format) file.
    Structured tab-separated data — no OCR needed, auto-confirmed at 100% confidence.

    Accepts JSON: { "iif_data": "<raw IIF text>", "location": "dennis"|"chatham" }
    """
    try:
        data = request.get_json(silent=True) or {}
        iif_text = data.get("iif_data")
        location = data.get("location", "dennis")

        if not iif_text:
            return jsonify({"error": "No iif_data provided"}), 400

        # Parse IIF into standard invoice dict
        extracted = parse_iif_invoice(iif_text, location=location)

        # Save to database
        import json
        result = save_invoice(
            location, extracted,
            image_path=None,
            raw_json=json.dumps(extracted),
        )

        # Check for duplicate
        if isinstance(result, dict) and result.get("duplicate"):
            return jsonify({
                "status": "duplicate",
                "message": f"Duplicate IIF invoice: {result['existing_invoice']['vendor_name']} #{result['existing_invoice']['invoice_number']}",
                "existing_id": result["existing_id"],
            }), 409

        invoice_id = result

        # Auto-confirm immediately — IIF data is perfect
        confirmed = False
        try:
            confirm_invoice(invoice_id)
            confirmed = True
            logger.info(f"IIF invoice #{invoice_id} auto-confirmed: {extracted['vendor_name']} "
                        f"#{extracted.get('invoice_number')} — {len(extracted['line_items'])} items, "
                        f"${extracted.get('total', 0):.2f}")

            # Run vendor item matching + anomaly detection
            try:
                conn = get_connection()
                process_invoice_items(invoice_id, conn)
                analyze_invoice_for_anomalies(invoice_id, conn)
                conn.close()
            except Exception as me:
                logger.error(f"Post-IIF-confirm processing error: {me}", exc_info=True)

            # Recalculate recipe costs in background
            threading.Thread(target=_run_cost_all, daemon=True).start()
        except Exception as e:
            logger.error(f"IIF auto-confirm failed for #{invoice_id}: {e}")

        item_count = len(extracted.get("line_items", []))
        total_val = extracted.get("total", 0)
        return jsonify({
            "status": "auto_confirmed" if confirmed else "needs_review",
            "invoice_id": invoice_id,
            "vendor_name": extracted.get("vendor_name"),
            "invoice_number": extracted.get("invoice_number"),
            "invoice_date": extracted.get("invoice_date"),
            "total": total_val,
            "item_count": item_count,
            "source": "iif",
            "message": f"IIF invoice imported and {'auto-confirmed' if confirmed else 'saved (confirm failed)'}: {item_count} items, ${total_val:.2f}",
        })

    except ValueError as e:
        logger.error(f"IIF parse error: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"IIF import error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/existing", methods=["GET"])
def api_existing_invoices():
    """Return all invoice numbers already in scanned_invoices for a given vendor.
    Used by scrapers to avoid downloading already-imported invoices.

    Query params:
        vendor: Vendor name (required). Matches case-insensitively.
    Returns: {"invoice_numbers": ["606658", "607123", ...]}
    """
    vendor = request.args.get("vendor", "").strip()
    if not vendor:
        return jsonify({"error": "vendor parameter required"}), 400

    conn = get_connection()
    rows = conn.execute(
        "SELECT invoice_number FROM scanned_invoices WHERE LOWER(vendor_name) LIKE ? AND invoice_number IS NOT NULL",
        (f"%{vendor.lower()}%",)
    ).fetchall()
    conn.close()

    return jsonify({"invoice_numbers": [r["invoice_number"] for r in rows]})


@invoice_bp.route("/api/invoices/import-csv", methods=["POST"])
def api_import_csv():
    """Import a vendor CSV file (US Foods, PFG, or VTInfo).
    Structured CSV data — no OCR needed, auto-confirmed at 100% confidence.

    Accepts multipart/form-data with 'file' field, or JSON with 'csv_data' text.
    Query params:
        location: 'dennis' or 'chatham' (optional, auto-detected from CSV if omitted)
        vendor: 'pfg' | 'vtinfo_lknife' | 'vtinfo_colonial' (default: US Foods)
        filename: original filename (needed for VTInfo to extract invoice metadata)
    """
    try:
        location = request.args.get("location")
        vendor_hint = request.args.get("vendor", "").lower()
        orig_filename = request.args.get("filename", "")
        csv_text = None

        if request.content_type and "multipart" in request.content_type:
            file = request.files.get("file")
            if not file:
                return jsonify({"error": "No CSV file provided"}), 400
            if not orig_filename and file.filename:
                orig_filename = file.filename
            csv_text = file.read().decode('utf-8', errors='replace')
        else:
            data = request.get_json(silent=True) or {}
            csv_text = data.get("csv_data")
            location = data.get("location") or location
            vendor_hint = data.get("vendor", vendor_hint).lower()
            orig_filename = data.get("filename", orig_filename)

        if not csv_text:
            return jsonify({"error": "No CSV data provided"}), 400

        # VTInfo CSV (L. Knife / Colonial): single invoice, metadata from filename
        if vendor_hint.startswith("vtinfo"):
            extracted = parse_vtinfo_csv_invoice(csv_text, filename=orig_filename, location=location)
            if not extracted or not extracted.get('line_items'):
                return jsonify({"error": "No items found in VTInfo CSV"}), 400

            loc = location or extracted.get('_detected_location') or 'dennis'
            import json as _json
            result = save_invoice(
                loc, extracted,
                image_path=None,
                raw_json=_json.dumps(extracted),
            )

            if isinstance(result, dict) and result.get("duplicate"):
                return jsonify({
                    "status": "duplicate",
                    "message": f"Duplicate: {extracted.get('vendor_name')} #{extracted.get('invoice_number')}",
                    "existing_id": result["existing_id"],
                }), 409

            invoice_id = result
            confirmed = False
            try:
                confirm_invoice(invoice_id)
                confirmed = True
                try:
                    conn = get_connection()
                    process_invoice_items(invoice_id, conn)
                    analyze_invoice_for_anomalies(invoice_id, conn)
                    conn.close()
                except Exception as me:
                    logger.error(f"Post-VTInfo-confirm processing error: {me}", exc_info=True)
                threading.Thread(target=_run_cost_all, daemon=True).start()
            except Exception as e:
                logger.error(f"VTInfo CSV auto-confirm failed for #{invoice_id}: {e}")

            # Generate CSV invoice image
            generate_csv_thumbnail(
                invoice_id, extracted.get('vendor_name'), extracted.get('invoice_number'),
                extracted.get('invoice_date'), extracted.get('total', 0),
                len(extracted.get('line_items', [])), source_label='CSV',
                line_items=extracted.get('line_items', []),
                ship_to_address=extracted.get('ship_to_address'),
            )

            return jsonify({
                "status": "auto_confirmed" if confirmed else "needs_review",
                "invoice_id": invoice_id,
                "invoice_number": extracted.get("invoice_number"),
                "vendor": extracted.get("vendor_name"),
                "total": extracted.get("total", 0),
                "item_count": len(extracted.get("line_items", [])),
                "location": loc,
                "message": f"VTInfo CSV: {extracted.get('vendor_name')} #{extracted.get('invoice_number')} imported",
            })

        # PFG CSV: returns a LIST of invoice dicts (one CSV can contain multiple invoices)
        if vendor_hint == "pfg":
            invoice_list = parse_pfg_csv_invoice(csv_text, location=location)
            if not invoice_list:
                return jsonify({"error": "No invoices found in PFG CSV"}), 400

            results = []
            for extracted in invoice_list:
                loc = location or extracted.get('_detected_location') or 'dennis'
                import json as _json
                result = save_invoice(
                    loc, extracted,
                    image_path=None,
                    raw_json=_json.dumps(extracted),
                )
                if isinstance(result, dict) and result.get("duplicate"):
                    results.append({
                        "status": "duplicate",
                        "invoice_number": extracted.get("invoice_number"),
                        "message": f"Duplicate: {extracted.get('vendor_name')} #{extracted.get('invoice_number')}",
                    })
                    continue

                invoice_id = result
                confirmed = False
                try:
                    confirm_invoice(invoice_id)
                    confirmed = True
                    try:
                        conn = get_connection()
                        process_invoice_items(invoice_id, conn)
                        analyze_invoice_for_anomalies(invoice_id, conn)
                        conn.close()
                    except Exception as me:
                        logger.error(f"Post-PFG-confirm processing error: {me}", exc_info=True)
                    threading.Thread(target=_run_cost_all, daemon=True).start()
                except Exception as e:
                    logger.error(f"PFG CSV auto-confirm failed for #{invoice_id}: {e}")

                # Generate CSV invoice image
                generate_csv_thumbnail(
                    invoice_id, extracted.get('vendor_name'), extracted.get('invoice_number'),
                    extracted.get('invoice_date'), extracted.get('total', 0),
                    len(extracted.get('line_items', [])), source_label='CSV',
                    line_items=extracted.get('line_items', []),
                    ship_to_address=extracted.get('ship_to_address'),
                )

                results.append({
                    "status": "auto_confirmed" if confirmed else "needs_review",
                    "invoice_id": invoice_id,
                    "invoice_number": extracted.get("invoice_number"),
                    "total": extracted.get("total", 0),
                    "item_count": len(extracted.get("line_items", [])),
                    "location": loc,
                    "is_credit": extracted.get("is_credit", False),
                })

            imported = [r for r in results if r.get("status") == "auto_confirmed"]
            dupes = [r for r in results if r.get("status") == "duplicate"]
            logger.info(f"PFG CSV import: {len(imported)} imported, {len(dupes)} duplicates")
            return jsonify({
                "status": "ok",
                "invoices": results,
                "count": len(imported),
                "duplicates": len(dupes),
                "message": f"PFG CSV: {len(imported)} imported, {len(dupes)} duplicates",
            })

        # Default: US Foods CSV (single invoice per CSV)
        extracted = parse_csv_invoice(csv_text, location=location)

        # Use detected location from parser
        loc = location or extracted.get('_detected_location') or 'dennis'
        # Re-derive from ship_to_address if location was in the CSV
        if not location:
            ship_to = extracted.get('ship_to_address', '')
            if 'chatham' in ship_to.lower() or '02633' in ship_to:
                loc = 'chatham'
            elif 'dennis' in ship_to.lower() or '02639' in ship_to:
                loc = 'dennis'

        # Save to database
        import json as _json
        result = save_invoice(
            loc, extracted,
            image_path=None,
            raw_json=_json.dumps(extracted),
        )

        # Check for duplicate
        if isinstance(result, dict) and result.get("duplicate"):
            return jsonify({
                "status": "duplicate",
                "message": f"Duplicate CSV invoice: {result['existing_invoice']['vendor_name']} #{result['existing_invoice']['invoice_number']}",
                "existing_id": result["existing_id"],
            }), 409

        invoice_id = result

        # Auto-confirm immediately — CSV data is perfect structured data
        confirmed = False
        try:
            confirm_invoice(invoice_id)
            confirmed = True
            logger.info(f"CSV invoice #{invoice_id} auto-confirmed: {extracted['vendor_name']} "
                        f"#{extracted.get('invoice_number')} — {len(extracted['line_items'])} items, "
                        f"${extracted.get('total', 0):.2f}")

            # Run vendor item matching + anomaly detection
            try:
                conn = get_connection()
                process_invoice_items(invoice_id, conn)
                analyze_invoice_for_anomalies(invoice_id, conn)
                conn.close()
            except Exception as me:
                logger.error(f"Post-CSV-confirm processing error: {me}", exc_info=True)

            # Recalculate recipe costs in background
            threading.Thread(target=_run_cost_all, daemon=True).start()
        except Exception as e:
            logger.error(f"CSV auto-confirm failed for #{invoice_id}: {e}")

        # Generate CSV invoice image
        generate_csv_thumbnail(
            invoice_id, extracted.get('vendor_name'), extracted.get('invoice_number'),
            extracted.get('invoice_date'), extracted.get('total', 0),
            len(extracted.get('line_items', [])), source_label='CSV',
            line_items=extracted.get('line_items', []),
            ship_to_address=extracted.get('ship_to_address'),
        )

        item_count = len(extracted.get("line_items", []))
        total_val = extracted.get("total", 0)
        return jsonify({
            "status": "auto_confirmed" if confirmed else "needs_review",
            "invoice_id": invoice_id,
            "vendor_name": extracted.get("vendor_name"),
            "invoice_number": extracted.get("invoice_number"),
            "invoice_date": extracted.get("invoice_date"),
            "total": total_val,
            "item_count": item_count,
            "location": loc,
            "source": "csv",
            "is_credit": extracted.get("is_credit", False),
            "message": f"CSV {'credit' if extracted.get('is_credit') else 'invoice'} imported and "
                       f"{'auto-confirmed' if confirmed else 'saved (confirm failed)'}: "
                       f"{item_count} items, ${total_val:.2f}",
        })

    except ValueError as e:
        logger.error(f"CSV parse error: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"CSV import error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/<int:invoice_id>/confirm", methods=["POST"])
def api_confirm_invoice(invoice_id):
    """
    Confirm an invoice after review. Send updated data if corrections were made.
    Runs vendor item matching and anomaly detection after confirm.
    """
    try:
        data = request.get_json(silent=True)
        # Session 29: server-side reconciliation validation
        if data and data.get("line_items"):
            items = data["line_items"]
            line_sum = round(sum(float(it.get("total_price", 0) or 0) for it in items), 2)
            stated_total = float(data.get("total", 0) or 0)
            tax = float(data.get("tax", 0) or 0)
            computed = round(line_sum + tax, 2)
            if stated_total and abs(computed - stated_total) > 0.02:
                return jsonify({
                    "error": f"Line items + tax (${computed:.2f}) do not match invoice total (${stated_total:.2f})",
                    "discrepancy": round(stated_total - computed, 2)
                }), 422
        confirm_invoice(invoice_id, updated_data=data)
        invoice = get_invoice(invoice_id)

        # Run vendor item matching + anomaly detection
        match_counts = {"auto_matched": 0, "suggestions": 0, "new_products": 0}
        anomaly_alert = None
        try:
            conn = get_connection()
            match_counts = process_invoice_items(invoice_id, conn)
            anomaly_alert = analyze_invoice_for_anomalies(invoice_id, conn)
            conn.close()
        except Exception as e:
            logger.error(f"Post-confirm processing error: {e}", exc_info=True)

        # Recalculate recipe costs in background (prices may have changed)
        threading.Thread(target=_run_cost_all, daemon=True).start()

        return jsonify({
            "status": "ok",
            "invoice": invoice,
            "message": "Invoice confirmed and saved.",
            "anomaly_alert": anomaly_alert,
            "auto_matched": match_counts.get("auto_matched", 0),
            "suggestions": match_counts.get("suggestions", 0),
            "new_products": match_counts.get("new_products", 0),
        })
    except Exception as e:
        logger.error(f"Confirm error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/<int:invoice_id>", methods=["GET"])
def api_get_invoice(invoice_id):
    """Get a single invoice with line items."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return jsonify({"error": "Invoice not found"}), 404
    return jsonify(invoice)


@invoice_bp.route("/api/invoices/<int:invoice_id>", methods=["DELETE"])
def api_delete_invoice(invoice_id):
    """Delete a pending invoice."""
    try:
        delete_invoice(invoice_id)
        return jsonify({"status": "ok", "message": "Invoice deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/create-manual", methods=["POST"])
def api_create_manual_invoice():
    """Create a manual invoice (no OCR). Saved as confirmed immediately."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        vendor_name = (data.get("vendor_name") or "").strip()
        invoice_date = data.get("invoice_date") or datetime.now().strftime("%Y-%m-%d")
        invoice_number = data.get("invoice_number") or ""
        location = data.get("location") or "dennis"
        invoice_type = data.get("invoice_type") or "one_time"
        recurring_frequency = data.get("recurring_frequency")
        recurring_day = data.get("recurring_day")
        detail_level = data.get("detail_level") or "category"
        tax = float(data.get("tax") or 0)
        fuel_freight = float(data.get("fuel_freight") or 0)
        credit = float(data.get("credit") or 0)
        other_charge = float(data.get("other_charge") or 0)
        other_desc = data.get("other_charge_description") or ""
        total = float(data.get("total") or 0)

        if not vendor_name:
            return jsonify({"error": "Vendor name is required"}), 400

        # Build line items from categories or line_items
        items = []
        if detail_level == "category":
            for cat in (data.get("categories") or []):
                amt = float(cat.get("amount") or 0)
                if amt:
                    items.append({
                        "product_name": cat.get("category", "FOOD"),
                        "description": "Category-level entry",
                        "quantity": 1, "unit": "category",
                        "unit_price": amt, "total_price": amt,
                        "category_type": cat.get("category", "FOOD"),
                    })
        else:
            for li in (data.get("line_items") or []):
                items.append({
                    "product_name": li.get("product_name") or "",
                    "description": li.get("description") or "",
                    "quantity": float(li.get("quantity") or 1),
                    "unit": li.get("unit") or "each",
                    "unit_price": float(li.get("unit_price") or 0),
                    "total_price": float(li.get("total_price") or 0),
                    "category_type": li.get("category") or "FOOD",
                })

        # Add fuel/freight, credit, other as NON_COGS items
        if fuel_freight:
            items.append({"product_name": "Fuel / Freight", "description": "",
                          "quantity": 1, "unit": "charge", "unit_price": fuel_freight,
                          "total_price": fuel_freight, "category_type": "NON_COGS"})
        if credit:
            items.append({"product_name": "Credit / Return", "description": "",
                          "quantity": 1, "unit": "credit", "unit_price": -abs(credit),
                          "total_price": -abs(credit), "category_type": "NON_COGS"})
        if other_charge:
            items.append({"product_name": other_desc or "Other Charge", "description": "",
                          "quantity": 1, "unit": "charge", "unit_price": other_charge,
                          "total_price": other_charge, "category_type": "NON_COGS"})

        # Determine category from vendor
        from integrations.invoices.processor import categorize_vendor
        category = categorize_vendor(vendor_name)

        # Compute subtotal from items
        subtotal = round(sum(it["total_price"] for it in items), 2)

        conn = get_connection()
        cur = conn.cursor()
        now = datetime.now().isoformat()

        cur.execute("""
            INSERT INTO scanned_invoices
            (location, vendor_name, invoice_number, invoice_date, subtotal, tax, total,
             category, status, source, auto_confirmed, confirmed_at, created_at,
             invoice_type, recurring_frequency, recurring_day,
             needs_reconciliation, discrepancy)
            VALUES (?,?,?,?,?,?,?,?,'confirmed','manual',1,?,?,?,?,?,0,0.0)
        """, (location, vendor_name, invoice_number, invoice_date, subtotal, tax, total,
              category, now, now, invoice_type, recurring_frequency, recurring_day))
        invoice_id = cur.lastrowid

        for it in items:
            cur.execute("""
                INSERT INTO scanned_invoice_items
                (invoice_id, product_name, description, quantity, unit, unit_price, total_price, category_type)
                VALUES (?,?,?,?,?,?,?,?)
            """, (invoice_id, it["product_name"], it["description"],
                  it["quantity"], it["unit"], it["unit_price"], it["total_price"],
                  it["category_type"]))

        conn.commit()

        # Post-confirm processing for line-item invoices
        if detail_level == "line_item" and items:
            try:
                process_invoice_items(invoice_id, conn)
            except Exception as e:
                logger.warning(f"Post-confirm processing error on manual invoice: {e}")

        # Recalculate recipe costs in background
        threading.Thread(target=_run_cost_all, daemon=True).start()

        conn.close()

        return jsonify({"status": "ok", "invoice_id": invoice_id,
                        "message": f"Invoice created for {vendor_name}"})
    except Exception as e:
        logger.error(f"Create manual invoice error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices", methods=["GET"])
def api_list_invoices():
    """List invoices with optional filters."""
    location = request.args.get("location")
    status = request.args.get("status")
    start = request.args.get("start")
    end = request.args.get("end")
    limit = int(request.args.get("limit", 50))

    invoices = get_invoices(location, status, start, end, limit)
    return jsonify(invoices)


@invoice_bp.route("/api/invoices/price-changes", methods=["GET"])
def api_price_changes():
    """Get recent product price changes."""
    days = int(request.args.get("days", 30))
    changes = get_price_changes(days)
    return jsonify(changes)


@invoice_bp.route("/api/invoices/spending", methods=["GET"])
def api_invoice_spending():
    """Get spending summary from confirmed scanned invoices."""
    location = request.args.get("location")
    start = request.args.get("start")
    end = request.args.get("end")
    summary = get_spending_summary(location, start, end)
    return jsonify(summary)


@invoice_bp.route("/api/invoices/<int:invoice_id>/image")
def serve_invoice_image(invoice_id):
    """Serve the raw invoice image/PDF file."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return jsonify({"error": "Invoice not found"}), 404
    image_path = invoice.get("image_path")
    if not image_path or not os.path.exists(image_path):
        return jsonify({"error": "No image available"}), 404
    directory = os.path.dirname(os.path.abspath(image_path))
    filename = os.path.basename(image_path)
    return send_from_directory(directory, filename)


@invoice_bp.route("/api/invoices/<int:invoice_id>/pay", methods=["POST"])
def api_pay_invoice(invoice_id):
    """Mark a confirmed invoice as paid."""
    data = request.get_json(silent=True) or {}
    paid_date = data.get("paid_date")
    payment_method = data.get("payment_method", "check")
    payment_reference = data.get("payment_reference")
    if not paid_date:
        return jsonify({"error": "paid_date is required"}), 400
    try:
        mark_invoice_paid(invoice_id, paid_date, payment_method, payment_reference)
        invoice = get_invoice(invoice_id)
        return jsonify({"status": "ok", "invoice": invoice})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Pay invoice error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/outstanding", methods=["GET"])
def api_outstanding_invoices():
    """Return unpaid confirmed invoices grouped by vendor."""
    try:
        location = request.args.get("location")
        return jsonify(get_outstanding_invoices(location=location))
    except Exception as e:
        logger.error(f"Outstanding invoices error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/payment-summary", methods=["GET"])
def api_payment_summary():
    """Return payment summary stats."""
    try:
        location = request.args.get("location")
        return jsonify(get_payment_summary(location=location))
    except Exception as e:
        logger.error(f"Payment summary error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/invoices/<int:invoice_id>/price-alerts")
def api_price_alerts(invoice_id):
    """Return price change alerts for each line item vs historical prices."""
    try:
        alerts = get_price_alerts_for_invoice(invoice_id)
        return jsonify(alerts)
    except Exception as e:
        logger.error(f"Price alerts error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# Serve the invoice scanner page
@invoice_bp.route("/invoices")
def invoice_page():
    """Serve the invoice scanner mobile web app."""
    return send_from_directory("static", "invoices.html")

@invoice_bp.route("/api/invoices/<int:invoice_id>/thumbnail")
def serve_invoice_thumbnail(invoice_id):
    """Serve the thumbnail JPG for an invoice."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return jsonify({"error": "Invoice not found"}), 404
    image_path = invoice.get("image_path")
    if not image_path:
        return jsonify({"error": "No image available"}), 404
    # Derive thumbnail path from image filename
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    thumb_path = f"/opt/rednun/invoice_thumbnails/{base_name}.jpg"
    if not os.path.exists(thumb_path):
        # Try to generate it on the fly
        from integrations.invoices.processor import generate_thumbnail
        thumb_path = generate_thumbnail(image_path)
    if not thumb_path or not os.path.exists(thumb_path):
        return jsonify({"error": "No thumbnail available"}), 404
    return send_from_directory("/opt/rednun/invoice_thumbnails", os.path.basename(thumb_path))


# ─── Vendor Session Status ────────────────────────────────────────────────────


@invoice_bp.route("/api/vendor-sessions/update", methods=["POST"])
def api_update_vendor_session():
    """Update vendor session status. Called by scrapers after each run."""
    data = request.get_json(force=True)
    vendor_name = data.get("vendor_name", "").strip()
    if not vendor_name:
        return jsonify({"error": "vendor_name required"}), 400

    status = data.get("status", "unknown")
    failure_reason = data.get("failure_reason")
    invoices_scraped = data.get("invoices_scraped_last_run", 0)
    now = datetime.now().isoformat()

    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM vendor_session_status WHERE vendor_name = ?", (vendor_name,)
    ).fetchone()

    if existing:
        if status == "healthy":
            if invoices_scraped and invoices_scraped > 0:
                conn.execute("""
                    UPDATE vendor_session_status
                    SET status = ?, last_successful_scrape = ?, failure_reason = NULL,
                        invoices_scraped_last_run = ?, last_invoice_date = ?, updated_at = ?
                    WHERE vendor_name = ?
                """, (status, now, invoices_scraped, now, now, vendor_name))
            else:
                conn.execute("""
                    UPDATE vendor_session_status
                    SET status = ?, last_successful_scrape = ?, failure_reason = NULL,
                        invoices_scraped_last_run = ?, updated_at = ?
                    WHERE vendor_name = ?
                """, (status, now, invoices_scraped, now, vendor_name))
        else:
            if invoices_scraped and invoices_scraped > 0:
                conn.execute("""
                    UPDATE vendor_session_status
                    SET status = ?, last_failure = ?, failure_reason = ?,
                        invoices_scraped_last_run = ?, last_invoice_date = ?, updated_at = ?
                    WHERE vendor_name = ?
                """, (status, now, failure_reason, invoices_scraped, now, now, vendor_name))
            else:
                conn.execute("""
                    UPDATE vendor_session_status
                    SET status = ?, last_failure = ?, failure_reason = ?,
                        invoices_scraped_last_run = ?, updated_at = ?
                    WHERE vendor_name = ?
                """, (status, now, failure_reason, invoices_scraped, now, vendor_name))
    else:
        if status == "healthy":
            if invoices_scraped and invoices_scraped > 0:
                conn.execute("""
                    INSERT INTO vendor_session_status (vendor_name, status, last_successful_scrape, invoices_scraped_last_run, last_invoice_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (vendor_name, status, now, invoices_scraped, now, now))
            else:
                conn.execute("""
                    INSERT INTO vendor_session_status (vendor_name, status, last_successful_scrape, invoices_scraped_last_run, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (vendor_name, status, now, invoices_scraped, now))
        else:
            conn.execute("""
                INSERT INTO vendor_session_status (vendor_name, status, last_failure, failure_reason, invoices_scraped_last_run, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (vendor_name, status, now, failure_reason, invoices_scraped, now))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "vendor": vendor_name, "session_status": status})


@invoice_bp.route("/api/vendor-sessions", methods=["GET"])
def api_get_vendor_sessions():
    """Get all vendor session statuses."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM vendor_session_status ORDER BY vendor_name").fetchall()
    conn.close()
    return jsonify({"sessions": [dict(r) for r in rows]})


# ── Vendor Scraper Runner ───────────────────────────────────────────
# Allows triggering scrapers from the dashboard UI.

import subprocess

# Map of scraper key -> (display_name, directory, script, session_name)
_SCRAPER_REGISTRY = {
    "usfoods": ("US Foods", os.path.expanduser("~/usfoods-scraper"), "usfoods_invoice_scraper.py", "US Foods"),
    "pfg": ("PFG", os.path.expanduser("~/vendor-scrapers/pfg"), "scraper.py", "Performance Foodservice"),
    "vtinfo": ("VTInfo", os.path.expanduser("~/vendor-scrapers/vtinfo"), "scraper.py", "L. Knife & Son, Inc."),
    "sg_chatham": ("SG Chatham", os.path.expanduser("~/vendor-scrapers/southern-glazers"), "scraper_chatham.py", "Southern Glazer's Beverage Company (chatham)"),
    "sg_dennis": ("SG Dennis", os.path.expanduser("~/vendor-scrapers/southern-glazers"), "scraper_dennis.py", "Southern Glazer's Beverage Company (dennis)"),
    "martignetti": ("Martignetti", os.path.expanduser("~/vendor-scrapers/martignetti"), "scraper.py", "Martignetti Companies"),
    "craft_collective": ("Craft Collective", os.path.expanduser("~/vendor-scrapers/craft-collective"), "scraper.py", "Craft Collective Inc"),
}

_PYTHON = "/opt/rednun/venv/bin/python3"
_IMPORT_SCRIPT = os.path.expanduser("~/vendor-scrapers/common/import_downloads.py")
_SCRAPER_LOG_DIR = os.path.expanduser("~/vendor-scrapers/logs")
_SCRAPER_STATE_FILE = os.path.expanduser("~/vendor-scrapers/logs/scraper_state.json")

_running_lock = threading.Lock()


def _read_scraper_state():
    """Read shared scraper state from disk (works across gunicorn workers)."""
    try:
        if os.path.exists(_SCRAPER_STATE_FILE):
            with open(_SCRAPER_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"running": {}, "results": {}}


def _write_scraper_state(state):
    """Write shared scraper state to disk."""
    os.makedirs(os.path.dirname(_SCRAPER_STATE_FILE), exist_ok=True)
    tmp = _SCRAPER_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _SCRAPER_STATE_FILE)


def _set_running(key, display_name):
    """Mark a scraper as running in shared state."""
    with _running_lock:
        state = _read_scraper_state()
        state["running"][key] = {"started": datetime.now().isoformat(), "display_name": display_name}
        _write_scraper_state(state)


def _clear_running(key):
    """Remove a scraper from running state."""
    with _running_lock:
        state = _read_scraper_state()
        state["running"].pop(key, None)
        _write_scraper_state(state)


def _set_result(key, exit_code, tail):
    """Store scraper result in shared state."""
    with _running_lock:
        state = _read_scraper_state()
        state["results"][key] = {
            "exit_code": exit_code,
            "tail": tail,
            "finished": datetime.now().isoformat(),
        }
        _write_scraper_state(state)


def _run_scraper_bg(key, display_name, scraper_dir, script, session_name):
    """Run a single scraper in background thread, update status when done."""
    os.makedirs(_SCRAPER_LOG_DIR, exist_ok=True)
    log_path = os.path.join(_SCRAPER_LOG_DIR, f"{key}.log")
    try:
        result = subprocess.run(
            [_PYTHON, script],
            cwd=scraper_dir,
            capture_output=True, text=True, timeout=600
        )
        # Write full output to log file
        with open(log_path, "w") as f:
            f.write(result.stdout or "")
            if result.stderr:
                f.write("\n--- STDERR ---\n")
                f.write(result.stderr)

        all_output = (result.stdout or "") + (result.stderr or "")
        tail = "\n".join(all_output.strip().splitlines()[-20:])
        _set_result(key, result.returncode, tail)

        if result.returncode == 0:
            logger.info(f"Scraper {key} completed successfully")
        else:
            logger.warning(f"Scraper {key} exited {result.returncode}: {tail[-200:]}")
            import requests as _req
            _req.post("http://127.0.0.1:8080/api/vendor-sessions/update",
                       json={"vendor_name": session_name, "status": "expired",
                              "failure_reason": f"manual_run_exit_{result.returncode}"}, timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning(f"Scraper {key} timed out after 600s")
        _set_result(key, -1, "Timed out after 10 minutes")
        import requests as _req
        _req.post("http://127.0.0.1:8080/api/vendor-sessions/update",
                   json={"vendor_name": session_name, "status": "expired",
                          "failure_reason": "timeout_manual_run"}, timeout=5)
    except Exception as e:
        logger.error(f"Scraper {key} error: {e}")
        _set_result(key, -1, str(e))
    finally:
        _clear_running(key)


def _run_all_scrapers_bg():
    """Run all scrapers sequentially in a background thread, then import."""
    order = ["usfoods", "pfg", "vtinfo", "sg_chatham", "sg_dennis", "martignetti", "craft_collective"]
    for key in order:
        if key not in _SCRAPER_REGISTRY:
            continue
        display_name, scraper_dir, script, session_name = _SCRAPER_REGISTRY[key]
        if not os.path.isdir(scraper_dir):
            continue
        _set_running(key, display_name)
        _run_scraper_bg(key, display_name, scraper_dir, script, session_name)

    # Run import_downloads.py after all scrapers
    try:
        _set_running("import", "Import Downloads")
        subprocess.run(
            [_PYTHON, _IMPORT_SCRIPT],
            cwd=os.path.expanduser("~/vendor-scrapers"),
            capture_output=True, text=True, timeout=300
        )
    except Exception as e:
        logger.error(f"Import error: {e}")
    finally:
        _clear_running("import")
        _clear_running("all")


@invoice_bp.route("/api/vendor-scrapers/run", methods=["POST"])
def api_run_vendor_scraper():
    """Trigger a vendor scraper in the background."""
    data = request.get_json(force=True)
    vendor_key = data.get("vendor", "").strip()

    if not vendor_key:
        return jsonify({"error": "vendor key required"}), 400

    state = _read_scraper_state()
    running = state.get("running", {})

    # Check if already running
    if vendor_key in running:
        return jsonify({"error": f"{vendor_key} is already running"}), 409
    if vendor_key == "all" and any(k != "import" for k in running):
        return jsonify({"error": "scrapers already running"}), 409

    if vendor_key == "all":
        _set_running("all", "Run All")
        t = threading.Thread(target=_run_all_scrapers_bg, daemon=True)
        t.start()
        return jsonify({"status": "started", "vendor": "all"})

    if vendor_key not in _SCRAPER_REGISTRY:
        return jsonify({"error": f"unknown vendor: {vendor_key}"}), 400

    display_name, scraper_dir, script, session_name = _SCRAPER_REGISTRY[vendor_key]
    if not os.path.isdir(scraper_dir):
        return jsonify({"error": f"scraper directory not found: {scraper_dir}"}), 404

    _set_running(vendor_key, display_name)

    t = threading.Thread(target=_run_scraper_bg, args=(vendor_key, display_name, scraper_dir, script, session_name), daemon=True)
    t.start()
    return jsonify({"status": "started", "vendor": vendor_key})


@invoice_bp.route("/api/vendor-scrapers/running", methods=["GET"])
def api_get_running_scrapers():
    """Return which scrapers are currently running, plus recent results."""
    state = _read_scraper_state()
    return jsonify({"running": state.get("running", {}), "results": state.get("results", {})})


@invoice_bp.route("/api/vendor-scrapers/log/<key>", methods=["GET"])
def api_get_scraper_log(key):
    """Return the full log output from the last run of a scraper."""
    log_path = os.path.join(_SCRAPER_LOG_DIR, f"{key}.log")
    if not os.path.exists(log_path):
        return jsonify({"error": "no log found", "log": ""}), 404
    try:
        with open(log_path) as f:
            content = f.read()
        return jsonify({"key": key, "log": content, "size": len(content)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
