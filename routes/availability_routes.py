"""
availability_routes.py
Red Nun — Summer 2026 Staff Availability Form
Route: /availability  (GET = form, POST = submit)
Generates a PDF and uploads it to Google Drive.
"""

import os
import io
import pickle
import base64
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

availability_bp = Blueprint("availability", __name__)

# ── Config ────────────────────────────────────────────────────────────
GOOGLE_TOKEN   = os.path.join(os.path.dirname(__file__), "google_token.pickle")
DRIVE_FOLDER   = "1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA"  # Chatham   # paste folder ID in .env
DAYS           = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
SHIFTS         = ["Lunch","Dinner"]


# ── Drive helper ──────────────────────────────────────────────────────
def get_drive_service():
    with open(GOOGLE_TOKEN, "rb") as f:
        creds = pickle.load(f)
    return build("drive", "v3", credentials=creds)


def upload_to_drive(pdf_bytes: bytes, filename: str) -> str:
    service  = get_drive_service()
    meta     = {"name": filename}
    if DRIVE_FOLDER:
        meta["parents"] = [DRIVE_FOLDER]
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    return f.get("webViewLink", "")


# ── PDF builder ───────────────────────────────────────────────────────
RED      = colors.HexColor("#8B2020")
RED_DARK = colors.HexColor("#5C1414")
RED_LITE = colors.HexColor("#FAEAEA")
GRAY_BG  = colors.HexColor("#F7F5F2")
BORDER   = colors.HexColor("#DDD8D2")
INK      = colors.HexColor("#1C1C1C")
MUTED    = colors.HexColor("#6B6560")

def build_pdf(data: dict, sig_png_bytes: bytes | None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48
    )
    W = letter[0] - 96
    styles = getSampleStyleSheet()

    def style(name, **kw):
        s = ParagraphStyle(name, **kw)
        return s

    title_s  = style("T",  fontName="Helvetica-Bold",   fontSize=20, textColor=RED_DARK, alignment=TA_CENTER, spaceAfter=4)
    sub_s    = style("S",  fontName="Helvetica",         fontSize=9,  textColor=MUTED,    alignment=TA_CENTER, spaceAfter=16)
    head_s   = style("H",  fontName="Helvetica-Bold",    fontSize=9,  textColor=RED,      spaceBefore=10, spaceAfter=4)
    body_s   = style("B",  fontName="Helvetica",         fontSize=9,  textColor=INK,      spaceAfter=4)
    label_s  = style("L",  fontName="Helvetica",         fontSize=8,  textColor=MUTED)
    policy_s = style("P",  fontName="Helvetica",         fontSize=8,  textColor=INK,      spaceAfter=3, leftIndent=10)

    story = []

    # Header
    story.append(Paragraph("THE RED NUN", title_s))
    story.append(Paragraph("SUMMER 2026 — STAFF AVAILABILITY FORM", sub_s))

    # Divider table
    story.append(Table([[""]], colWidths=[W], style=TableStyle([
        ("LINEABOVE", (0,0), (-1,0), 0.5, BORDER),
    ])))
    story.append(Spacer(1, 10))

    # Basic info
    def field_row(label, value):
        return Table(
            [[Paragraph(label, label_s), Paragraph(value or "(not specified)", body_s)]],
            colWidths=[100, W-100],
            style=TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")])
        )

    story.append(field_row("Full Name:", data.get("name", "")))
    story.append(field_row("Position(s):", data.get("positions", "")))
    story.append(field_row("Start Date:", data.get("start_date", "")))
    story.append(field_row("Last Day:", data.get("end_date", "")))
    story.append(Spacer(1, 8))

    # Availability grid
    story.append(Paragraph("WEEKLY AVAILABILITY", head_s))

    col_w = W / 8
    grid_data = [[""] + [d[:3].upper() for d in DAYS]]
    for shift in SHIFTS:
        row = [Paragraph(shift, style("sh", fontName="Helvetica-Bold", fontSize=8, textColor=MUTED))]
        for day in DAYS:
            key = f"{shift}|{day}"
            is_x = key in data.get("unavailable", [])
            cell = Paragraph("X", style("x", fontName="Helvetica-Bold", fontSize=11,
                             textColor=RED, alignment=TA_CENTER)) if is_x else ""
            row.append(cell)
        grid_data.append(row)

    grid_style = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0),  GRAY_BG),
        ("BACKGROUND",  (0,1), (0,-1),  GRAY_BG),
        ("GRID",        (0,0), (-1,-1), 0.5, BORDER),
        ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0),  7),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("ROWHEIGHT",   (0,0), (-1,-1), 22),
        ("TEXTCOLOR",   (0,0), (-1,0),  MUTED),
    ])
    for ri, shift in enumerate(SHIFTS, 1):
        for ci, day in enumerate(DAYS, 1):
            if f"{shift}|{day}" in data.get("unavailable", []):
                grid_style.add("BACKGROUND", (ci, ri), (ci, ri), RED_LITE)

    story.append(Table(grid_data, colWidths=[col_w]*8, style=grid_style))
    story.append(Spacer(1, 10))

    story.append(field_row("Shifts/week:", data.get("shifts_per_week", "")))
    story.append(Spacer(1, 4))
    story.append(Paragraph("TIME OFF / DATES UNABLE TO WORK", head_s))
    story.append(Paragraph(data.get("time_off") or "(none listed)", body_s))
    story.append(Spacer(1, 8))

    # Divider
    story.append(Table([[""]], colWidths=[W], style=TableStyle([
        ("LINEABOVE", (0,0), (-1,0), 0.5, BORDER),
    ])))
    story.append(Spacer(1, 8))

    # Policies
    story.append(Paragraph("POLICIES & ACKNOWLEDGMENTS", head_s))
    policies = [
        "All staff must be available to work during the July 4th holiday weekend (July 3–July 5) regardless of your above availability.",
        "Shifts are your responsibility. Posting a shift \"up for grabs\" does not mean it has been covered — you remain accountable until it is properly filled and approved.",
        "Proper uniform is required, including closed-toed shoes, a Red Nun shirt, and appropriate attire.",
    ]
    for p in policies:
        story.append(Paragraph("• " + p, policy_s))
    story.append(Spacer(1, 10))

    # Divider
    story.append(Table([[""]], colWidths=[W], style=TableStyle([
        ("LINEABOVE", (0,0), (-1,0), 0.5, BORDER),
    ])))
    story.append(Spacer(1, 10))

    # Signature
    story.append(Paragraph("EMPLOYEE SIGNATURE", head_s))

    sig_cell = ""
    if sig_png_bytes:
        sig_img = Image(io.BytesIO(sig_png_bytes), width=180, height=52)
        sig_cell = sig_img

    sig_table = Table(
        [[sig_cell, Paragraph("Date: " + (data.get("sig_date") or "(not specified)"), body_s)]],
        colWidths=[220, W-220],
        style=TableStyle([
            ("VALIGN",   (0,0), (-1,-1), "BOTTOM"),
            ("LINEBELOW",(0,0), (0,0),   0.5, BORDER),
        ])
    )
    story.append(sig_table)

    doc.build(story)
    return buf.getvalue()


AVAILABILITY_FOLDER = "1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA"  # Chatham folder
AVAILABILITY_EMAIL  = "matt@rednun.com"

def get_gmail_service():
    gmail_token = os.path.join(os.path.dirname(__file__), "gmail_token.pickle")
    token_path  = gmail_token if os.path.exists(gmail_token) else GOOGLE_TOKEN
    with open(token_path, "rb") as f:
        creds = pickle.load(f)
    return build("gmail", "v1", credentials=creds)

def send_availability_notification(name, data, drive_link):
    import base64 as b64lib
    from email.mime.text import MIMEText
    from collections import defaultdict
    shifts = data.get("shifts_per_week", "not specified")
    start  = data.get("start_date", "")
    end    = data.get("end_date", "")
    unavail = data.get("unavailable", [])
    by_shift = defaultdict(list)
    for key in unavail:
        shift, day = key.split("|")
        by_shift[shift].append(day)
    avail_lines = "\n".join(f"  {shift}: NOT available {', '.join(days)}" if days else f"  {shift}: All days available" for shift, days in by_shift.items()) or "  All days/shifts available"
    body = f"New availability form submitted.\n\nName:        {name}\nStart Date:  {start}\nLast Day:    {end}\nShifts/week: {shifts}\nTime off:    {data.get('time_off') or 'None listed'}\n\nAvailability:\n{avail_lines}\n\nPDF saved to Drive: {drive_link}"
    service = get_gmail_service()
    msg = MIMEText(body)
    msg["to"] = AVAILABILITY_EMAIL
    msg["subject"] = f"Summer 2026 Availability \u2014 {name}"
    raw = b64lib.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

# ── Routes ────────────────────────────────────────────────────────────
@availability_bp.route("/availability", methods=["GET"])
def availability_form():
    return render_template("availability.html")


@availability_bp.route("/availability/submit", methods=["POST"])
def availability_submit():
    try:
        payload = request.get_json()
        name    = (payload.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "Name is required."}), 400

        # Decode signature PNG from base64 data URL
        sig_data = payload.get("signature", "")
        sig_bytes = None
        if sig_data and "," in sig_data:
            sig_bytes = base64.b64decode(sig_data.split(",", 1)[1])

        pdf_bytes = build_pdf(payload, sig_bytes)

        safe_name = name.replace(" ", "_")
        date_str  = datetime.now().strftime("%Y%m%d")
        filename  = f"Availability_{safe_name}_{date_str}.pdf"

        link = upload_to_drive(pdf_bytes, filename)

        return jsonify({"ok": True, "link": link})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
