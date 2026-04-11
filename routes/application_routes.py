"""
application_routes.py
Red Nun Bar & Grill — Job Application Form
Route: /apply  (GET = form, POST = submit)
Generates a PDF, uploads to Google Drive, emails based on location.
"""
import os, io, pickle, base64, mimetypes
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import base64 as b64lib

from flask import Blueprint, render_template, request, jsonify
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable
from reportlab.lib.styles import ParagraphStyle

application_bp = Blueprint("application", __name__)

GOOGLE_TOKEN     = os.path.join(os.path.dirname(__file__), "google_token.pickle")
GMAIL_TOKEN      = os.path.join(os.path.dirname(__file__), "gmail_token.pickle")

LOCATION_CONFIG = {
    "Chatham": {
        "emails":    ["matt@rednun.com"],
        "folder_id": "1ZIbKK9xp8hKsHgVpmQ5gefS1tK1O4Qer",
    },
    "Dennis Port": {
        "emails":    ["alexis@rednun.com"],
        "folder_id": "119iaRcv98V4tycrvs2SBx12CvFRqOua1",
    },

}
DEFAULT_EMAILS   = ["matt@rednun.com", "alexis@rednun.com"]
DEFAULT_FOLDER   = "1ZIbKK9xp8hKsHgVpmQ5gefS1tK1O4Qer"
SHEET_FOLDER     = "1qzgYOEHub5CXlo7_S-CKvL8cWGU4r8fN"

SHEET_HEADERS = [
    "Submitted", "Name", "DOB", "Email", "Phone", "Address",
    "Location(s)", "Position(s)", "Start Date", "End Date",
    "Reference 1", "Reference 2", "Reference 3",
    "Employer 1", "Employer 2", "Employer 3",
    "Resume Attached", "Drive Link",
    "H2B", "H2B Status", "H2B Notes"
]
SHEET_NAME = "Applications 2026"

# ── Colors ────────────────────────────────────────────────────────────
RED      = colors.HexColor("#8B2020")
RED_DARK = colors.HexColor("#5C1414")
RED_LITE = colors.HexColor("#FAEAEA")
GRAY_BG  = colors.HexColor("#F7F5F2")
BORDER   = colors.HexColor("#DDD8D2")
INK      = colors.HexColor("#1C1C1C")
MUTED    = colors.HexColor("#6B6560")

def s(name, **kw):
    return ParagraphStyle(name, **kw)


# ── Drive ─────────────────────────────────────────────────────────────
def get_drive_service():
    with open(GOOGLE_TOKEN, "rb") as f:
        creds = pickle.load(f)
    return build("drive", "v3", credentials=creds)

def upload_to_drive(pdf_bytes, filename, folder_id=None):
    service = get_drive_service()
    meta = {"name": filename}
    if folder_id:
        meta["parents"] = [folder_id]
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    return f.get("webViewLink", "")


# ── Sheets helper ────────────────────────────────────────────────────
def get_sheets_service():
    with open(GOOGLE_TOKEN, "rb") as f:
        creds = pickle.load(f)
    return build("sheets", "v4", credentials=creds)

def find_or_create_sheet():
    drive = get_drive_service()
    q = f"name='{SHEET_NAME}' and '{SHEET_FOLDER}' in parents and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    results = drive.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    # Create new spreadsheet in folder
    sheets = get_sheets_service()
    body = {"properties": {"title": SHEET_NAME}, "sheets": [{"properties": {"title": "Sheet1"}}]}
    ss = sheets.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    sid = ss["spreadsheetId"]
    # Move to folder
    drive.files().update(fileId=sid, addParents=SHEET_FOLDER, removeParents="root", fields="id").execute()
    # Write headers
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range="Sheet1!A1",
        valueInputOption="RAW", body={"values": [SHEET_HEADERS]}
    ).execute()
    return sid

def append_to_sheet(data, has_resume, drive_link):
    sid = find_or_create_sheet()
    sheets = get_sheets_service()
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        data.get("name", ""),
        data.get("dob", ""),
        data.get("email", ""),
        data.get("phone", ""),
        data.get("address", ""),
        ", ".join(data.get("locations", [])),
        ", ".join(data.get("positions", [])),
        data.get("start_date", ""),
        data.get("end_date", ""),
        (data.get("references", []) + ["","",""])[0],
        (data.get("references", []) + ["","",""])[1],
        (data.get("references", []) + ["","",""])[2],
        (data.get("employers", []) + ["","",""])[0],
        (data.get("employers", []) + ["","",""])[1],
        (data.get("employers", []) + ["","",""])[2],
        "Yes" if has_resume else "No",
        drive_link,
        data.get("h2b", "No"),
        data.get("h2b_status", ""),
        data.get("h2b_notes", ""),
    ]
    sheets.spreadsheets().values().append(
        spreadsheetId=sid, range="Sheet1!A:U",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()

    # Highlight if Michael Giorgio is listed as a reference
    refs_text = " ".join((data.get("references", []) + ["","",""])[:3]).lower()
    if "michael giorgio" in refs_text or "mike giorgio" in refs_text:
        meta = sheets.spreadsheets().get(spreadsheetId=sid).execute()
        sheet_gid = meta["sheets"][0]["properties"]["sheetId"]
        # Find last row
        vals = sheets.spreadsheets().values().get(spreadsheetId=sid, range="Sheet1!A:A").execute()
        last_row = len(vals.get("values", []))
        sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"repeatCell": {"range": {"sheetId": sheet_gid, "startRowIndex": last_row - 1, "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": 21}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": 0.93, "blue": 0.8}}}, "fields": "userEnteredFormat.backgroundColor"}}]}).execute()


# ── Gmail send ────────────────────────────────────────────────────────
def get_gmail_service():
    token_path = GMAIL_TOKEN if os.path.exists(GMAIL_TOKEN) else GOOGLE_TOKEN
    with open(token_path, "rb") as f:
        creds = pickle.load(f)
    return build("gmail", "v1", credentials=creds)


def send_alert_email(to_list, name, loc_str, pos_str, payload, drive_link):
    import base64 as b64lib
    from email.mime.text import MIMEText
    service = get_gmail_service()
    body = (
        f"New job application received.\n\n"
        f"Name:       {name}\n"
        f"Location:   {loc_str}\n"
        f"Position:   {pos_str}\n"
        f"Start Date: {payload.get('start_date','')}\n"
        f"End Date:   {payload.get('end_date','')}\n"
        f"Email:      {payload.get('email','')}\n"
        f"Phone:      {payload.get('phone','')}\n\n"
        f"View application: {drive_link}"
    )
    for to_addr in to_list:
        msg = MIMEText(body)
        msg["to"] = to_addr
        msg["subject"] = f"New Application — {name} ({loc_str})"
        raw = b64lib.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

def send_email(to_list, subject, body_text, pdf_bytes, pdf_filename):
    service = get_gmail_service()
    for to_addr in to_list:
        msg = MIMEMultipart()
        msg["to"]      = to_addr
        msg["subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{pdf_filename}"')
        msg.attach(part)

        raw = b64lib.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── PDF builder ───────────────────────────────────────────────────────
def build_pdf(data, resume_bytes=None, resume_ext=".pdf"):
    buf = io.BytesIO()
    W   = letter[0] - 96
    doc = SimpleDocTemplate(buf, pagesize=letter,
          leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)

    title_s  = s("T",  fontName="Helvetica-Bold", fontSize=20, textColor=RED_DARK, alignment=1, spaceAfter=4)
    sub_s    = s("S",  fontName="Helvetica",       fontSize=9,  textColor=MUTED,    alignment=1, spaceAfter=16)
    head_s   = s("H",  fontName="Helvetica-Bold",  fontSize=9,  textColor=RED,      spaceBefore=12, spaceAfter=4)
    body_s   = s("B",  fontName="Helvetica",       fontSize=9,  textColor=INK,      spaceAfter=3)
    label_s  = s("L",  fontName="Helvetica",       fontSize=8,  textColor=MUTED)
    small_s  = s("Sm", fontName="Helvetica",       fontSize=8,  textColor=INK,      spaceAfter=2)

    def field_row(label, value):
        return Table(
            [[Paragraph(label, label_s), Paragraph(str(value or "(not provided)"), body_s)]],
            colWidths=[120, W-120],
            style=TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")])
        )

    def divider():
        return Table([[""]], colWidths=[W],
            style=TableStyle([("LINEABOVE",(0,0),(-1,0),0.5,BORDER)]))

    story = []
    story.append(Paragraph("RED NUN BAR & GRILL", title_s))
    story.append(Paragraph("JOB APPLICATION", sub_s))
    story.append(divider())
    story.append(Spacer(1, 10))

    # Basic info
    story.append(Paragraph("APPLICANT INFORMATION", head_s))
    story.append(field_row("Full Name:",       data.get("name","")))
    story.append(field_row("Date of Birth:",   data.get("dob","")))
    story.append(field_row("Email:",           data.get("email","")))
    story.append(field_row("Phone:",           data.get("phone","")))
    story.append(field_row("Address:",         data.get("address","")))
    story.append(field_row("Location(s):",     ", ".join(data.get("locations",[])) or "(not specified)"))
    story.append(field_row("Position(s):",     ", ".join(data.get("positions",[])) or "(not specified)"))
    story.append(field_row("Start Date:",      data.get("start_date","")))
    story.append(field_row("End Date:",        data.get("end_date","")))
    story.append(Spacer(1, 6))

    # H2B
    story.append(Paragraph("VISA STATUS", head_s))
    story.append(field_row("H2B Visa Worker:", data.get("h2b","No")))
    if data.get("h2b_status"):
        story.append(field_row("Current Status:", data.get("h2b_status","")))
    if data.get("h2b_notes"):
        story.append(field_row("Notes:", data.get("h2b_notes","")))
    story.append(Spacer(1, 6))

    # References
    story.append(Paragraph("REFERENCES", head_s))
    for i, ref in enumerate(data.get("references", []), 1):
        if ref.strip():
            story.append(Paragraph(f"Reference {i}: {ref}", small_s))
    story.append(Spacer(1, 6))

    # Employment history
    story.append(Paragraph("EMPLOYMENT HISTORY", head_s))
    for i, emp in enumerate(data.get("employers", []), 1):
        if emp.strip():
            story.append(Paragraph(f"Employer {i}: {emp}", small_s))
    story.append(Spacer(1, 6))

    # Resume note
    if resume_bytes and resume_ext.lower() == ".pdf":
        story.append(divider())
        story.append(Spacer(1, 8))
        story.append(Paragraph("RESUME", head_s))
        story.append(Paragraph("See following page(s).", body_s))
    elif resume_bytes:
        story.append(divider())
        story.append(Spacer(1, 8))
        story.append(Paragraph("RESUME", head_s))
        story.append(Paragraph("Resume attached separately in email (non-PDF format).", body_s))

    doc.build(story)
    app_pdf = buf.getvalue()

    # Merge resume PDF pages after the application
    if resume_bytes and resume_ext.lower() == ".pdf":
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(app_pdf)).pages:
            writer.add_page(page)
        try:
            for page in PdfReader(io.BytesIO(resume_bytes)).pages:
                writer.add_page(page)
        except Exception:
            pass  # skip if resume PDF is corrupted
        merged = io.BytesIO()
        writer.write(merged)
        return merged.getvalue()

    return app_pdf


# ── Routes ────────────────────────────────────────────────────────────
@application_bp.route("/hiring", methods=["GET"])
def application_form():
    return render_template("application.html")


@application_bp.route("/hiring/submit", methods=["POST"])
def application_submit():
    try:
        payload   = request.get_json()
        name      = (payload.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "Name is required."}), 400

        # Resume (base64 encoded if provided)
        resume_b64  = payload.get("resume_data", "")
        resume_ext  = payload.get("resume_ext", ".pdf")
        resume_bytes = None
        if resume_b64 and "," in resume_b64:
            resume_bytes = b64lib.b64decode(resume_b64.split(",",1)[1])

        pdf_bytes = build_pdf(payload, resume_bytes, resume_ext)
        parts = name.split(None, 1)
        if len(parts) == 2:
            safe_name = parts[1].replace(" ", "_") + "_" + parts[0].replace(" ", "_")
        else:
            safe_name = parts[0].replace(" ", "_")
        filename  = f"{safe_name}.pdf"

        # Determine locations
        locations  = payload.get("locations", [])
        recipients = set()
        folders_used = set()

        for loc in locations:
            cfg = LOCATION_CONFIG.get(loc)
            if cfg:
                for em in cfg["emails"]:
                    recipients.add(em)
                folders_used.add(cfg["folder_id"])

        if not recipients:
            recipients   = set(DEFAULT_EMAILS)
            folders_used = {DEFAULT_FOLDER}

        # Upload a copy to each relevant Drive folder
        link = ""
        for fid in folders_used:
            link = upload_to_drive(pdf_bytes, filename, folder_id=fid)

        # Append to central spreadsheet
        try:
            append_to_sheet(payload, bool(resume_bytes), link)
        except Exception as sheet_err:
            print(f"Sheet append failed: {sheet_err}")
        loc_str = ", ".join(locations) if locations else "Not specified"
        pos_str = ", ".join(payload.get("positions", [])) or "Not specified"

        body = (
            f"New job application received.\n\n"
            f"Name:       {name}\n"
            f"Location:   {loc_str}\n"
            f"Position:   {pos_str}\n"
            f"Start Date: {payload.get('start_date','')}\n"
            f"Email:      {payload.get('email','')}\n"
            f"Phone:      {payload.get('phone','')}\n\n"
            f"Full application PDF attached.\n"
            f"Drive link: {link}"
        )

        # Send lightweight email alert with Drive link
        try:
            send_alert_email(list(recipients), name, loc_str, pos_str, payload, link)
        except Exception as mail_err:
            print(f"Email send failed: {mail_err}")
        return jsonify({"ok": True, "link": link})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


def send_email_with_attachments(to_list, subject, body_text,
                                 pdf_bytes, pdf_filename,
                                 resume_bytes, resume_filename):
    service = get_gmail_service()
    for to_addr in to_list:
        msg = MIMEMultipart()
        msg["to"]      = to_addr
        msg["subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        for data, fname in [(pdf_bytes, pdf_filename), (resume_bytes, resume_filename)]:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            msg.attach(part)

        raw = b64lib.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
