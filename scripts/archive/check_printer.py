"""
Check Printer — PDF generation for vendor bill pay checks.
Generates pure white PDFs for printing on DocuGard Top-Check Voucher stock.
Layout: check on top, two stubs below.
MICR line rendered as transparent PNG composed from glyph strip.
Signature rendered as transparent PNG.

Expected files:
  /opt/rednun/check_assets/micr_chars_strip.png  — E-13B glyph strip
  /opt/rednun/check_assets/signature.png          — transparent signature PNG

Page: 612 x 792pt (US Letter)
Check section: y = 528–792pt
Middle stub:   y = 264–528pt
Bottom stub:   y = 0–264pt
"""

import io
import os
import logging
from datetime import datetime
from num2words import num2words

logger = logging.getLogger(__name__)

ASSETS_DIR = "/opt/rednun/check_assets"
MICR_FONT_PATH = os.path.join(ASSETS_DIR, "micr-e13b.ttf")
SIGNATURE_PATH = os.path.join(ASSETS_DIR, "signature.png")

# Page dimensions
PAGE_W = 612  # pt
PAGE_H = 792  # pt

# MICR font settings
# In the E-13B TTF font, special chars map to:
#   A = Transit symbol (⑆)
#   B = Amount symbol (⑇)  — not used on personal/business checks
#   C = On-Us symbol (⑈)
#   D = Dash symbol (⑉)   — not used on personal/business checks
MICR_FONT_SIZE = 12     # pt — standard MICR is ~12pt
MICR_Y = 555            # pt from page bottom — MICR clear band position

# MICR font registered flag
_micr_font_registered = False


def _amount_to_words(amount):
    """Convert dollar amount to written check format: 'One thousand five hundred twenty-three and 47/100'."""
    dollars = int(amount)
    cents = round((amount - dollars) * 100)
    if dollars == 0:
        words = "Zero"
    else:
        words = num2words(dollars, to='cardinal').replace(',', '')
        # Capitalize first letter
        words = words[0].upper() + words[1:]
    return f"{words} and {cents:02d}/100"


def _format_amount(amount):
    """Format as $ X,XXX.XX."""
    return f"$ {amount:,.2f}"


def _register_micr_font():
    """Register the MICR E-13B TTF font with ReportLab (once)."""
    global _micr_font_registered
    if _micr_font_registered:
        return True
    if not os.path.exists(MICR_FONT_PATH):
        logger.warning(f"MICR font not found at {MICR_FONT_PATH} — skipping MICR line")
        return False
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont('MICR', MICR_FONT_PATH))
        _micr_font_registered = True
        return True
    except Exception as e:
        logger.warning(f"Could not register MICR font: {e}")
        return False


def _build_micr_string(check_number, routing, account):
    """Build the MICR line string using E-13B font character mappings.
    Font mapping: A=Transit(⑆), C=On-Us(⑈)
    Format: C[6-digit check#]C  A[9-digit routing]A  [9-digit account]C
    """
    check_str = str(check_number).zfill(6)
    routing_str = str(routing).zfill(9)
    account_str = str(account).zfill(9)
    return f"C{check_str}C  A{routing_str}A  {account_str}C"


def _make_signature_transparent(sig_path):
    """Load signature PNG and make white pixels transparent."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return sig_path  # return as-is

    if not os.path.exists(sig_path):
        return None

    img = Image.open(sig_path).convert("RGBA")
    arr = np.array(img)
    # Make white-ish pixels transparent (threshold > 200 on R, G, B)
    white_mask = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 200) & (arr[:, :, 2] > 200)
    arr[white_mask, 3] = 0
    result = Image.fromarray(arr)
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_check_pdf(payment, invoices, config, vendor_info=None,
                       check_number=None, output_path=None):
    """
    Generate a single check PDF on pure white background.
    Designed for DocuGard Top-Check Voucher stock.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    if not output_path:
        output_path = f"/tmp/check_{check_number or 'draft'}.pdf"

    c = canvas.Canvas(output_path, pagesize=letter)
    ox = config.get("offset_x", 0) or 0
    oy = config.get("offset_y", 0) or 0

    check_num = check_number or payment.get("check_number", "")
    amount = payment.get("amount", 0)
    vendor_name = payment.get("vendor_name", "")
    payment_date = payment.get("payment_date", datetime.now().strftime("%Y-%m-%d"))
    memo = payment.get("memo", "")

    # Format date nicely
    try:
        dt = datetime.strptime(payment_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %d, %Y")
    except ValueError:
        date_str = payment_date

    # Build memo from invoice numbers if not provided
    if not memo and invoices:
        inv_nums = [inv.get("invoice_number", "") for inv in invoices if inv.get("invoice_number")]
        if inv_nums:
            memo = "Inv #" + ", #".join(inv_nums)

    # Vendor address for bottom envelope window
    payee_addr1 = ""
    payee_addr2 = ""
    if vendor_info:
        recipient = vendor_info.get("payment_recipient") or vendor_name
        addr1 = vendor_info.get("remit_address_1", "") or ""
        addr2 = vendor_info.get("remit_address_2", "") or ""
        city = vendor_info.get("remit_city", "") or ""
        state = vendor_info.get("remit_state", "") or ""
        zipcode = vendor_info.get("remit_zip", "") or ""
        payee_addr1 = addr1
        csz = ", ".join(filter(None, [city, state]))
        if zipcode:
            csz = f"{csz} {zipcode}" if csz else zipcode
        payee_addr2 = csz
        if addr2:
            payee_addr1 = addr1
            payee_addr2 = addr2 + ("  " + csz if csz else "")
    else:
        recipient = vendor_name

    account_name = config.get("account_name", "") or ""
    account_addr1 = config.get("account_address_1", "") or ""
    account_csz = config.get("account_city_state_zip", "") or ""
    bank_name = config.get("bank_name", "") or ""
    bank_addr = config.get("bank_address", "") or ""

    # ─── CHECK SECTION (top third: y = 528–792) ───

    # Top envelope window — company return address
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 745 + oy, account_name)
    c.setFont("Helvetica", 9)
    if account_addr1:
        c.drawString(50 + ox, 732 + oy, account_addr1)
    if account_csz:
        c.drawString(50 + ox, 719 + oy, account_csz)

    # Right-side header
    c.setFont("Helvetica-Bold", 10)
    c.drawString(310 + ox, 763 + oy, bank_name)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(586 + ox, 763 + oy, str(check_num))
    c.setFont("Helvetica", 9)
    if bank_addr:
        # Split bank address if it has a comma
        parts = bank_addr.split(",", 1)
        c.drawString(310 + ox, 750 + oy, parts[0].strip())
        if len(parts) > 1:
            c.drawString(310 + ox, 738 + oy, parts[1].strip())

    # Date
    c.setFont("Helvetica", 10)
    c.drawString(480 + ox, 712 + oy, date_str)

    # Pay to
    c.setFont("Helvetica-Bold", 10)
    c.drawString(84 + ox, 700 + oy, recipient)

    # Dollar amount
    c.drawString(500 + ox, 691 + oy, _format_amount(amount))

    # Written amount
    written = _amount_to_words(amount)
    c.setFont("Helvetica", 9)
    c.drawString(20 + ox, 680 + oy, written)
    c.drawRightString(586 + ox, 680 + oy, "DOLLARS")

    # Bottom envelope window — vendor/payee address
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 635 + oy, recipient)
    c.setFont("Helvetica", 9)
    if payee_addr1:
        c.drawString(50 + ox, 622 + oy, payee_addr1)
    if payee_addr2:
        c.drawString(50 + ox, 609 + oy, payee_addr2)

    # Memo
    if memo:
        c.setFont("Helvetica", 9)
        c.drawString(50 + ox, 590 + oy, memo)

    # Signature line
    c.setLineWidth(1)
    c.line(340 + ox, 575 + oy, 586 + ox, 575 + oy)
    c.setFont("Helvetica", 7)
    c.drawCentredString(463 + ox, 566 + oy, "AUTHORIZED SIGNATURE")

    # Signature image
    sig_path = config.get("signature_path") or SIGNATURE_PATH
    sig_buf = _make_signature_transparent(sig_path)
    if sig_buf:
        try:
            sig_img = ImageReader(sig_buf)
            c.drawImage(sig_img, 466 + ox, 577 + oy, width=120, height=39,
                       preserveAspectRatio=True, mask='auto')
        except Exception as e:
            logger.warning(f"Could not render signature: {e}")

    # MICR line (rendered with E-13B TTF font)
    routing = config.get("routing_number", "") or ""
    account_num = config.get("account_number", "") or ""
    if routing and account_num and check_num and _register_micr_font():
        micr_text = _build_micr_string(check_num, routing, account_num)
        c.setFont("MICR", MICR_FONT_SIZE)
        c.drawCentredString(PAGE_W / 2 + ox, MICR_Y + oy, micr_text)

    # ─── MIDDLE STUB (y = 264–528) — Invoice Detail ───
    stub_top = 520
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 + ox, stub_top + oy, account_name)
    c.drawRightString(586 + ox, stub_top + oy, f"Check # {check_num}")

    c.setFont("Helvetica", 8)
    c.drawString(20 + ox, stub_top - 14 + oy, f"Payment Date: {date_str}")
    c.drawString(250 + ox, stub_top - 14 + oy, f"Vendor: {vendor_name}")

    # Invoice detail table header
    table_y = stub_top - 36
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 + ox, table_y + oy, "Invoice #")
    c.drawString(150 + ox, table_y + oy, "Invoice Date")
    c.drawString(280 + ox, table_y + oy, "Invoice Total")
    c.drawRightString(500 + ox, table_y + oy, "Amount Applied")

    c.setLineWidth(0.5)
    c.line(20 + ox, table_y - 3 + oy, 520 + ox, table_y - 3 + oy)

    # Invoice rows
    c.setFont("Helvetica", 8)
    row_y = table_y - 16
    for inv in invoices:
        if row_y < 280:
            break
        inv_num = inv.get("invoice_number", "—")
        inv_date = inv.get("invoice_date", "—")
        inv_total = inv.get("total", 0) or 0
        applied = inv.get("amount_applied", 0) or 0
        c.drawString(20 + ox, row_y + oy, str(inv_num))
        c.drawString(150 + ox, row_y + oy, str(inv_date))
        c.drawString(280 + ox, row_y + oy, f"${inv_total:,.2f}")
        c.drawRightString(500 + ox, row_y + oy, f"${applied:,.2f}")
        row_y -= 13

    # Total line
    row_y -= 5
    c.setLineWidth(0.5)
    c.line(380 + ox, row_y + 10 + oy, 520 + ox, row_y + 10 + oy)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(380 + ox, row_y + oy, "Total:")
    c.drawRightString(500 + ox, row_y + oy, _format_amount(amount))

    # ─── BOTTOM STUB (y = 0–264) — Summary ───
    stub2_top = 256
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 + ox, stub2_top + oy, account_name)
    c.drawRightString(586 + ox, stub2_top + oy, f"Check # {check_num}")

    c.setFont("Helvetica", 8)
    c.drawString(20 + ox, stub2_top - 14 + oy, f"Payment Date: {date_str}")

    # Vendor name + address (left)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 + ox, stub2_top - 34 + oy, recipient)
    c.setFont("Helvetica", 8)
    if payee_addr1:
        c.drawString(20 + ox, stub2_top - 46 + oy, payee_addr1)
    if payee_addr2:
        c.drawString(20 + ox, stub2_top - 58 + oy, payee_addr2)

    # Summary boxes (center/right)
    box_y = stub2_top - 34
    c.setFont("Helvetica", 7)
    c.drawString(300 + ox, box_y + 10 + oy, "Invoices")
    c.drawString(400 + ox, box_y + 10 + oy, "Total Amount")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(300 + ox, box_y + oy, str(len(invoices)))
    c.drawString(400 + ox, box_y + oy, _format_amount(amount))

    if memo:
        c.setFont("Helvetica", 7)
        c.drawString(20 + ox, stub2_top - 78 + oy, f"Memo: {memo}")

    c.save()
    logger.info(f"Check PDF generated: {output_path} (check #{check_num}, ${amount:,.2f} to {vendor_name})")
    return output_path


def generate_batch_checks_pdf(payments_data, config, output_path):
    """Generate multi-page PDF with one check per page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(output_path, pagesize=letter)

    for i, pd in enumerate(payments_data):
        if i > 0:
            c.showPage()

        # Generate each check on its own page using the single-check logic
        # We reuse the same canvas
        _draw_check_on_canvas(c, pd["payment"], pd["invoices"], config,
                              pd.get("vendor_info"), pd["check_number"])

    c.save()
    logger.info(f"Batch checks PDF generated: {output_path} ({len(payments_data)} checks)")
    return output_path


def _draw_check_on_canvas(c, payment, invoices, config, vendor_info, check_number):
    """Draw a single check on the current canvas page (shared logic)."""
    from reportlab.lib.utils import ImageReader

    ox = config.get("offset_x", 0) or 0
    oy = config.get("offset_y", 0) or 0

    amount = payment.get("amount", 0)
    vendor_name = payment.get("vendor_name", "")
    payment_date = payment.get("payment_date", datetime.now().strftime("%Y-%m-%d"))
    memo = payment.get("memo", "")

    try:
        dt = datetime.strptime(payment_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %d, %Y")
    except ValueError:
        date_str = payment_date

    if not memo and invoices:
        inv_nums = [inv.get("invoice_number", "") for inv in invoices if inv.get("invoice_number")]
        if inv_nums:
            memo = "Inv #" + ", #".join(inv_nums)

    # Resolve payee info
    if vendor_info:
        recipient = vendor_info.get("payment_recipient") or vendor_name
        addr1 = vendor_info.get("remit_address_1", "") or ""
        addr2 = vendor_info.get("remit_address_2", "") or ""
        city = vendor_info.get("remit_city", "") or ""
        state = vendor_info.get("remit_state", "") or ""
        zipcode = vendor_info.get("remit_zip", "") or ""
        payee_addr1 = addr1
        csz = ", ".join(filter(None, [city, state]))
        if zipcode:
            csz = f"{csz} {zipcode}" if csz else zipcode
        payee_addr2 = csz
        if addr2:
            payee_addr1 = addr1
            payee_addr2 = addr2 + ("  " + csz if csz else "")
    else:
        recipient = vendor_name
        payee_addr1 = ""
        payee_addr2 = ""

    account_name = config.get("account_name", "") or ""
    account_addr1 = config.get("account_address_1", "") or ""
    account_csz = config.get("account_city_state_zip", "") or ""
    bank_name = config.get("bank_name", "") or ""
    bank_addr = config.get("bank_address", "") or ""

    # ─── CHECK SECTION ───
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 745 + oy, account_name)
    c.setFont("Helvetica", 9)
    if account_addr1:
        c.drawString(50 + ox, 732 + oy, account_addr1)
    if account_csz:
        c.drawString(50 + ox, 719 + oy, account_csz)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(310 + ox, 763 + oy, bank_name)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(586 + ox, 763 + oy, str(check_number))
    c.setFont("Helvetica", 9)
    if bank_addr:
        parts = bank_addr.split(",", 1)
        c.drawString(310 + ox, 750 + oy, parts[0].strip())
        if len(parts) > 1:
            c.drawString(310 + ox, 738 + oy, parts[1].strip())

    c.setFont("Helvetica", 10)
    c.drawString(480 + ox, 712 + oy, date_str)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(84 + ox, 700 + oy, recipient)
    c.drawString(500 + ox, 691 + oy, _format_amount(amount))

    written = _amount_to_words(amount)
    c.setFont("Helvetica", 9)
    c.drawString(20 + ox, 680 + oy, written)
    c.drawRightString(586 + ox, 680 + oy, "DOLLARS")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 635 + oy, recipient)
    c.setFont("Helvetica", 9)
    if payee_addr1:
        c.drawString(50 + ox, 622 + oy, payee_addr1)
    if payee_addr2:
        c.drawString(50 + ox, 609 + oy, payee_addr2)

    if memo:
        c.setFont("Helvetica", 9)
        c.drawString(50 + ox, 590 + oy, memo)

    c.setLineWidth(1)
    c.line(340 + ox, 575 + oy, 586 + ox, 575 + oy)
    c.setFont("Helvetica", 7)
    c.drawCentredString(463 + ox, 566 + oy, "AUTHORIZED SIGNATURE")

    # Signature
    sig_path = config.get("signature_path") or SIGNATURE_PATH
    sig_buf = _make_signature_transparent(sig_path)
    if sig_buf:
        try:
            sig_img = ImageReader(sig_buf)
            c.drawImage(sig_img, 466 + ox, 577 + oy, width=120, height=39,
                       preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    # MICR line (rendered with E-13B TTF font)
    routing = config.get("routing_number", "") or ""
    account_num = config.get("account_number", "") or ""
    if routing and account_num and check_number and _register_micr_font():
        micr_text = _build_micr_string(check_number, routing, account_num)
        c.setFont("MICR", MICR_FONT_SIZE)
        c.drawCentredString(PAGE_W / 2 + ox, MICR_Y + oy, micr_text)

    # ─── MIDDLE STUB ───
    stub_top = 520
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 + ox, stub_top + oy, account_name)
    c.drawRightString(586 + ox, stub_top + oy, f"Check # {check_number}")
    c.setFont("Helvetica", 8)
    c.drawString(20 + ox, stub_top - 14 + oy, f"Payment Date: {date_str}")
    c.drawString(250 + ox, stub_top - 14 + oy, f"Vendor: {vendor_name}")

    table_y = stub_top - 36
    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 + ox, table_y + oy, "Invoice #")
    c.drawString(150 + ox, table_y + oy, "Invoice Date")
    c.drawString(280 + ox, table_y + oy, "Invoice Total")
    c.drawRightString(500 + ox, table_y + oy, "Amount Applied")
    c.setLineWidth(0.5)
    c.line(20 + ox, table_y - 3 + oy, 520 + ox, table_y - 3 + oy)

    c.setFont("Helvetica", 8)
    row_y = table_y - 16
    for inv in invoices:
        if row_y < 280:
            break
        c.drawString(20 + ox, row_y + oy, str(inv.get("invoice_number", "—")))
        c.drawString(150 + ox, row_y + oy, str(inv.get("invoice_date", "—")))
        c.drawString(280 + ox, row_y + oy, f"${(inv.get('total', 0) or 0):,.2f}")
        c.drawRightString(500 + ox, row_y + oy, f"${(inv.get('amount_applied', 0) or 0):,.2f}")
        row_y -= 13

    row_y -= 5
    c.setLineWidth(0.5)
    c.line(380 + ox, row_y + 10 + oy, 520 + ox, row_y + 10 + oy)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(380 + ox, row_y + oy, "Total:")
    c.drawRightString(500 + ox, row_y + oy, _format_amount(amount))

    # ─── BOTTOM STUB ───
    stub2_top = 256
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 + ox, stub2_top + oy, account_name)
    c.drawRightString(586 + ox, stub2_top + oy, f"Check # {check_number}")
    c.setFont("Helvetica", 8)
    c.drawString(20 + ox, stub2_top - 14 + oy, f"Payment Date: {date_str}")

    c.setFont("Helvetica-Bold", 8)
    c.drawString(20 + ox, stub2_top - 34 + oy, recipient)
    c.setFont("Helvetica", 8)
    if payee_addr1:
        c.drawString(20 + ox, stub2_top - 46 + oy, payee_addr1)
    if payee_addr2:
        c.drawString(20 + ox, stub2_top - 58 + oy, payee_addr2)

    c.setFont("Helvetica", 7)
    c.drawString(300 + ox, stub2_top - 24 + oy, "Invoices")
    c.drawString(400 + ox, stub2_top - 24 + oy, "Total Amount")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(300 + ox, stub2_top - 34 + oy, str(len(invoices)))
    c.drawString(400 + ox, stub2_top - 34 + oy, _format_amount(amount))

    if memo:
        c.setFont("Helvetica", 7)
        c.drawString(20 + ox, stub2_top - 78 + oy, f"Memo: {memo}")


def generate_payroll_check_pdf(payroll, config, check_number=None, output_path=None):
    """
    Generate a payroll check PDF on pure white background.
    Check section (top) is same layout as vendor checks.
    Stubs show earnings/deductions instead of invoices.
    """
    import json
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    if not output_path:
        output_path = f"/tmp/payroll_check_{check_number or 'draft'}.pdf"

    c = canvas.Canvas(output_path, pagesize=letter)
    ox = config.get("offset_x", 0) or 0
    oy = config.get("offset_y", 0) or 0

    check_num = check_number or payroll.get("check_number", "")
    net_pay = payroll.get("net_pay", 0)
    gross_pay = payroll.get("gross_pay", 0)
    employee_name = payroll.get("employee_name", "")
    pay_start = payroll.get("pay_period_start", "")
    pay_end = payroll.get("pay_period_end", "")
    memo = payroll.get("memo", "") or f"Payroll {pay_start} - {pay_end}"

    # Parse deductions
    ded_raw = payroll.get("deductions", "{}")
    if isinstance(ded_raw, str):
        try:
            deductions = json.loads(ded_raw)
        except (json.JSONDecodeError, TypeError):
            deductions = {}
    else:
        deductions = ded_raw or {}

    # Format date
    payment_date = payroll.get("printed_at") or datetime.now().strftime("%Y-%m-%d")
    try:
        if "T" in payment_date:
            payment_date = payment_date.split("T")[0]
        dt = datetime.strptime(payment_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %d, %Y")
    except ValueError:
        date_str = payment_date

    # Employee address
    addr1 = payroll.get("employee_address_1", "") or ""
    addr2 = payroll.get("employee_address_2", "") or ""
    city = payroll.get("employee_city", "") or ""
    state = payroll.get("employee_state", "") or ""
    zipcode = payroll.get("employee_zip", "") or ""
    csz = ", ".join(filter(None, [city, state]))
    if zipcode:
        csz = f"{csz} {zipcode}" if csz else zipcode
    payee_addr1 = addr1
    payee_addr2 = addr2 + ("  " + csz if csz and addr2 else csz if csz else "")
    if not addr2:
        payee_addr2 = csz

    account_name = config.get("account_name", "") or ""
    account_addr1 = config.get("account_address_1", "") or ""
    account_csz = config.get("account_city_state_zip", "") or ""
    bank_name = config.get("bank_name", "") or ""
    bank_addr = config.get("bank_address", "") or ""

    # ─── CHECK SECTION (top third: y = 528–792) ───
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 745 + oy, account_name)
    c.setFont("Helvetica", 9)
    if account_addr1:
        c.drawString(50 + ox, 732 + oy, account_addr1)
    if account_csz:
        c.drawString(50 + ox, 719 + oy, account_csz)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(310 + ox, 763 + oy, bank_name)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(586 + ox, 763 + oy, str(check_num))
    c.setFont("Helvetica", 9)
    if bank_addr:
        parts = bank_addr.split(",", 1)
        c.drawString(310 + ox, 750 + oy, parts[0].strip())
        if len(parts) > 1:
            c.drawString(310 + ox, 738 + oy, parts[1].strip())

    c.setFont("Helvetica", 10)
    c.drawString(480 + ox, 712 + oy, date_str)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(84 + ox, 700 + oy, employee_name)
    c.drawString(500 + ox, 691 + oy, _format_amount(net_pay))

    written = _amount_to_words(net_pay)
    c.setFont("Helvetica", 9)
    c.drawString(20 + ox, 680 + oy, written)
    c.drawRightString(586 + ox, 680 + oy, "DOLLARS")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50 + ox, 635 + oy, employee_name)
    c.setFont("Helvetica", 9)
    if payee_addr1:
        c.drawString(50 + ox, 622 + oy, payee_addr1)
    if payee_addr2:
        c.drawString(50 + ox, 609 + oy, payee_addr2)

    if memo:
        c.setFont("Helvetica", 9)
        c.drawString(50 + ox, 590 + oy, memo)

    c.setLineWidth(1)
    c.line(340 + ox, 575 + oy, 586 + ox, 575 + oy)
    c.setFont("Helvetica", 7)
    c.drawCentredString(463 + ox, 566 + oy, "AUTHORIZED SIGNATURE")

    # Signature
    sig_path = config.get("signature_path") or SIGNATURE_PATH
    sig_buf = _make_signature_transparent(sig_path)
    if sig_buf:
        try:
            sig_img = ImageReader(sig_buf)
            c.drawImage(sig_img, 466 + ox, 577 + oy, width=120, height=39,
                       preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    # MICR line
    routing = config.get("routing_number", "") or ""
    account_num = config.get("account_number", "") or ""
    if routing and account_num and check_num and _register_micr_font():
        micr_text = _build_micr_string(check_num, routing, account_num)
        c.setFont("MICR", MICR_FONT_SIZE)
        c.drawCentredString(PAGE_W / 2 + ox, MICR_Y + oy, micr_text)

    # Parse YTD data
    ytd_raw = payroll.get("ytd", {})
    if isinstance(ytd_raw, str):
        try:
            ytd = json.loads(ytd_raw)
        except:
            ytd = {}
    else:
        ytd = ytd_raw or {}

    total_hours = payroll.get("total_hours", 0) or 0

    # ─── Helper: draw payroll stub ───
    def _draw_payroll_stub(stub_top):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(20 + ox, stub_top + oy, account_name)
        c.drawRightString(586 + ox, stub_top + oy, f"Check # {check_num}")
        c.setFont("Helvetica", 8)
        c.drawString(20 + ox, stub_top - 14 + oy, f"Employee: {employee_name}")
        c.drawString(250 + ox, stub_top - 14 + oy, f"Pay Period: {pay_start} — {pay_end}")
        if total_hours:
            c.drawString(470 + ox, stub_top - 14 + oy, f"Hours: {total_hours:.1f}")

        # Table header
        has_ytd = bool(ytd)
        col1_x = 20      # Description
        col2_x = 200      # Current
        col3_x = 300      # YTD
        col4_x = 370      # Description (deductions)
        col5_x = 500      # Current (ded)
        col6_x = 560      # YTD (ded)
        row_y = stub_top - 32

        # ── Left side: Earnings ──
        c.setFont("Helvetica-Bold", 7)
        c.drawString(col1_x + ox, row_y + oy, "EARNINGS")
        c.drawRightString(col2_x + 50 + ox, row_y + oy, "Current")
        if has_ytd:
            c.drawRightString(col3_x + 50 + ox, row_y + oy, "YTD")
        c.setLineWidth(0.5)
        c.line(col1_x + ox, row_y - 3 + oy, col3_x + 55 + ox, row_y - 3 + oy)

        c.setFont("Helvetica", 7)
        row_y -= 14
        c.drawString(col1_x + ox, row_y + oy, "Gross Pay")
        c.drawRightString(col2_x + 50 + ox, row_y + oy, f"${gross_pay:,.2f}")
        if has_ytd and ytd.get("gross_pay"):
            c.drawRightString(col3_x + 50 + ox, row_y + oy, f"${ytd['gross_pay']:,.2f}")

        # ── Right side: Deductions ──
        ded_y = stub_top - 32
        c.setFont("Helvetica-Bold", 7)
        c.drawString(col4_x + ox, ded_y + oy, "DEDUCTIONS")
        c.drawRightString(col5_x + ox, ded_y + oy, "Current")
        if has_ytd:
            c.drawRightString(col6_x + ox, ded_y + oy, "YTD")
        c.line(col4_x + ox, ded_y - 3 + oy, col6_x + 5 + ox, ded_y - 3 + oy)

        c.setFont("Helvetica", 7)
        ded_y -= 14
        total_ded = 0

        ded_labels = [
            ("federal_tax", "Federal Tax"),
            ("state_tax", "State Tax (MA)"),
            ("fica_ss", "Social Security"),
            ("fica_medicare", "Medicare"),
        ]
        for key, label in ded_labels:
            val = deductions.get(key, 0) or 0
            if val > 0:
                c.drawString(col4_x + ox, ded_y + oy, label)
                c.drawRightString(col5_x + ox, ded_y + oy, f"${val:,.2f}")
                if has_ytd and ytd.get(key):
                    c.drawRightString(col6_x + ox, ded_y + oy, f"${ytd[key]:,.2f}")
                total_ded += val
                ded_y -= 11

        for item in deductions.get("other", []):
            lbl = item.get("label", "Other")
            amt = item.get("amount", 0) or 0
            if amt > 0:
                c.drawString(col4_x + ox, ded_y + oy, lbl)
                c.drawRightString(col5_x + ox, ded_y + oy, f"${amt:,.2f}")
                total_ded += amt
                ded_y -= 11

        # Totals line
        ded_y -= 2
        c.line(col4_x + ox, ded_y + 8 + oy, col6_x + 5 + ox, ded_y + 8 + oy)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(col4_x + ox, ded_y + oy, "Total Deductions")
        c.drawRightString(col5_x + ox, ded_y + oy, f"${total_ded:,.2f}")

        # Net pay line
        net_y = min(row_y, ded_y) - 14
        c.setFont("Helvetica-Bold", 9)
        c.drawString(col1_x + ox, net_y + oy, "NET PAY")
        c.drawRightString(col2_x + 50 + ox, net_y + oy, f"${net_pay:,.2f}")
        if has_ytd and ytd.get("net_pay"):
            c.drawRightString(col3_x + 50 + ox, net_y + oy, f"${ytd['net_pay']:,.2f}")

    # ─── MIDDLE STUB (y = 264–528) — Employer copy ───
    _draw_payroll_stub(520)

    # ─── BOTTOM STUB (y = 0–264) — Employee copy ───
    _draw_payroll_stub(256)

    c.save()
    logger.info(f"Payroll check PDF generated: {output_path} (check #{check_num}, ${net_pay:,.2f} to {employee_name})")
    return output_path


def generate_batch_payroll_checks_pdf(payroll_list, config, output_path):
    """Generate multi-page PDF with one payroll check per page.
    Uses PyPDF2 to merge individually generated check PDFs.
    """
    import tempfile
    from pypdf import PdfWriter, PdfReader

    writer = PdfWriter()
    temp_files = []

    for pr in payroll_list:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        temp_files.append(tmp.name)
        generate_payroll_check_pdf(
            payroll=pr["payroll"],
            config=config,
            check_number=pr["check_number"],
            output_path=tmp.name,
        )
        reader = PdfReader(tmp.name)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    # Clean up temp files
    for tf in temp_files:
        try:
            os.remove(tf)
        except OSError:
            pass

    logger.info(f"Batch payroll PDF generated: {output_path} ({len(payroll_list)} checks)")
    return output_path


def generate_calibration_page(config, output_path):
    """
    Generate a calibration page with labeled grid and field position markers.
    Print on plain paper, hold against check stock to verify alignment.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import Color

    c = canvas.Canvas(output_path, pagesize=letter)
    ox = config.get("offset_x", 0) or 0
    oy = config.get("offset_y", 0) or 0

    gray = Color(0.7, 0.7, 0.7)
    red = Color(1, 0, 0, 0.5)

    # Draw grid every 50pt
    c.setStrokeColor(gray)
    c.setLineWidth(0.25)
    for x in range(0, 620, 50):
        c.line(x, 0, x, 792)
        c.setFont("Helvetica", 5)
        c.setFillColor(gray)
        c.drawString(x + 2, 785, str(x))
    for y in range(0, 800, 50):
        c.line(0, y, 612, y)
        c.setFont("Helvetica", 5)
        c.setFillColor(gray)
        c.drawString(2, y + 2, str(y))

    # Section dividers
    c.setStrokeColor(red)
    c.setLineWidth(1)
    c.setDashArray([4, 4])
    c.line(0, 528, 612, 528)  # check / middle stub
    c.line(0, 264, 612, 264)  # middle stub / bottom stub

    # Label sections
    c.setFillColor(red)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(306, 660 + oy, "CHECK")
    c.drawCentredString(306, 396 + oy, "MIDDLE STUB")
    c.drawCentredString(306, 132 + oy, "BOTTOM STUB")

    # Mark key field positions with offset applied
    c.setDashArray([])
    markers = [
        (50, 745, "Company Name"),
        (310, 763, "Bank Name"),
        (586, 763, "Check #"),
        (480, 712, "Date"),
        (84, 700, "Pay To"),
        (500, 691, "Amount"),
        (20, 680, "Written Amount"),
        (50, 635, "Payee Name"),
        (50, 622, "Payee Addr 1"),
        (50, 609, "Payee Addr 2"),
        (50, 590, "Memo"),
        (340, 575, "Signature Line Start"),
        (0, 553, "MICR Line"),
    ]

    c.setFont("Helvetica", 6)
    for x, y, label in markers:
        c.setFillColor(red)
        c.circle(x + ox, y + oy, 2, fill=1)
        c.drawString(x + ox + 4, y + oy + 1, label)

    # Offset info
    c.setFillColor(Color(0, 0, 0))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20, 20, f"Offset X: {ox}pt  |  Offset Y: {oy}pt")
    c.drawString(20, 8, "Print on plain paper. Hold against check stock. Adjust offsets in Check Setup if needed.")

    c.save()
    logger.info(f"Calibration page generated: {output_path}")
    return output_path
