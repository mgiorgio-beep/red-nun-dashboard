"""
Morning Report Routes
Blueprint mounted at /staff/api/reports/morning
"""

import logging
from datetime import date, datetime
from flask import Blueprint, request, jsonify

from reports.morning_report import build_html, send_email

logger = logging.getLogger(__name__)

morning_report_bp = Blueprint("morning_report", __name__)


@morning_report_bp.route("/staff/api/reports/morning")
def morning_report():
    """Generate and optionally send the morning sales report.

    Query params:
        date     - YYYY-MM-DD (default: today)
        preview  - if "1", return HTML without sending email
    """
    date_str = request.args.get("date")
    preview = request.args.get("preview") == "1"

    if date_str:
        try:
            report_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date format, use YYYY-MM-DD"}), 400
    else:
        report_date = date.today()

    html = build_html(report_date)

    if not preview:
        try:
            send_email(html, report_date)
            logger.info(f"Morning report sent for {report_date}")
        except Exception as e:
            logger.error(f"Failed to send morning report: {e}")
            return jsonify({"error": str(e)}), 500

    return html, 200, {"Content-Type": "text/html"}
