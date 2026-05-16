"""
Print-agent routes — endpoints consumed by the Windows print agent that
sits on the home desktop and forwards check PDFs to the Brother HL-L6210DW.

Blueprint: print_agent_bp at /api/print-agent/*
Auth:      X-API-Key header, value from PRINT_AGENT_API_KEY in .env.

Flow:
    1. POST /api/print-agent/checkout         → claims the next pending job
    2. GET  /api/print-agent/jobs/<id>/pdf    → downloads the PDF bytes
    3. POST /api/print-agent/jobs/<id>/ack    → reports success or failure
"""

import os
import logging
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request, send_file, abort

from integrations.toast.data_store import get_connection

logger = logging.getLogger(__name__)

print_agent_bp = Blueprint("print_agent_bp", __name__)


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────

def _expected_key():
    """Read PRINT_AGENT_API_KEY from environment at request time so .env
    changes don't require a server restart in dev."""
    return (os.getenv("PRINT_AGENT_API_KEY") or "").strip()


def api_key_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = _expected_key()
        if not expected:
            logger.error("PRINT_AGENT_API_KEY is unset — refusing print-agent requests")
            return jsonify({"error": "server misconfigured"}), 503
        provided = (request.headers.get("X-API-Key") or "").strip()
        if not secrets.compare_digest(provided, expected):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────────────────────
# Health / smoke test
# ──────────────────────────────────────────────────────────────────────────────

@print_agent_bp.route("/api/print-agent/health")
@api_key_required
def health():
    """Cheap endpoint the agent hits on startup to verify connectivity + auth."""
    conn = get_connection()
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM print_jobs WHERE status = 'pending'"
    ).fetchone()["c"]
    conn.close()
    return jsonify({
        "ok": True,
        "server_time": datetime.now().isoformat(),
        "pending_jobs": pending,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Checkout — claim the next pending job
# ──────────────────────────────────────────────────────────────────────────────

@print_agent_bp.route("/api/print-agent/checkout", methods=["POST"])
@api_key_required
def checkout():
    """Atomically claim the oldest pending print_job. Returns 204 if queue
    empty. Optionally accepts JSON body {"agent_id": "hostname"} for audit."""
    agent_id = ""
    try:
        body = request.get_json(silent=True) or {}
        agent_id = (body.get("agent_id") or "").strip()[:120]
    except Exception:
        pass

    conn = get_connection()
    try:
        # Use an explicit transaction to claim atomically.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, kind, payment_id, check_number, location, pdf_path,
                   attempts
            FROM print_jobs
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return ("", 204)

        conn.execute(
            """
            UPDATE print_jobs
            SET status = 'claimed',
                claimed_by = ?,
                claimed_at = datetime('now'),
                attempts = COALESCE(attempts, 0) + 1,
                updated_at = datetime('now')
            WHERE id = ? AND status = 'pending'
            """,
            (agent_id or "unknown", row["id"]),
        )
        conn.commit()
        return jsonify({
            "id": row["id"],
            "kind": row["kind"],
            "payment_id": row["payment_id"],
            "check_number": row["check_number"],
            "location": row["location"],
            "attempts": (row["attempts"] or 0) + 1,
            "pdf_url": f"/api/print-agent/jobs/{row['id']}/pdf",
        })
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Download PDF
# ──────────────────────────────────────────────────────────────────────────────

@print_agent_bp.route("/api/print-agent/jobs/<int:job_id>/pdf")
@api_key_required
def get_pdf(job_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT id, pdf_path, status FROM print_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    if row["status"] not in ("claimed", "pending", "error"):
        # Already-printed jobs are still downloadable so the agent can
        # re-print if asked, but no auto download for cancelled jobs.
        return jsonify({"error": f"job in state '{row['status']}'"}), 409
    pdf_path = row["pdf_path"]
    if not pdf_path or not os.path.exists(pdf_path):
        return jsonify({"error": "pdf missing on server"}), 410
    return send_file(
        pdf_path, mimetype="application/pdf",
        as_attachment=False,
        download_name=f"check_{row['id']}.pdf",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Ack — agent reports success or failure
# ──────────────────────────────────────────────────────────────────────────────

@print_agent_bp.route("/api/print-agent/jobs/<int:job_id>/ack", methods=["POST"])
@api_key_required
def ack(job_id):
    """Body: {"status": "printed" | "error", "error": "<message>"}.

    On 'error', we return the job to 'pending' so it retries on the next
    checkout — unless it has already failed too many times, in which case
    we leave it 'error' for human attention.
    """
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    err = (body.get("error") or "").strip()[:2000]

    if status not in ("printed", "error"):
        return jsonify({"error": "status must be 'printed' or 'error'"}), 400

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, attempts FROM print_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404

        if status == "printed":
            conn.execute(
                """
                UPDATE print_jobs
                SET status = 'printed',
                    printed_at = datetime('now'),
                    last_error = NULL,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (job_id,),
            )
            conn.commit()
            logger.info(f"print_job #{job_id} ack: printed")
            return jsonify({"ok": True})

        # error path
        new_status = "error" if (row["attempts"] or 0) >= 5 else "pending"
        conn.execute(
            """
            UPDATE print_jobs
            SET status = ?,
                last_error = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_status, err, job_id),
        )
        conn.commit()
        logger.warning(
            f"print_job #{job_id} ack: error (attempts={row['attempts']}, "
            f"new_status={new_status}): {err}"
        )
        return jsonify({"ok": True, "new_status": new_status})
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Optional: list recent jobs (admin-facing — same API key)
# Useful for debugging from a browser w/ a key in a header tool.
# ──────────────────────────────────────────────────────────────────────────────

@print_agent_bp.route("/api/print-agent/jobs")
@api_key_required
def list_jobs():
    limit = min(int(request.args.get("limit", 50) or 50), 500)
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, kind, payment_id, check_number, location, status,
               attempts, last_error, claimed_by, claimed_at, printed_at,
               created_at
        FROM print_jobs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify({"jobs": [dict(r) for r in rows]})
