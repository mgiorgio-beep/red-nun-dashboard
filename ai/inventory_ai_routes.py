"""
AI Inventory API Routes — Red Nun Dashboard
============================================
Blueprint for the AI-powered inventory counting system.
Mounts under /api/ai-inventory/* and serves the dashboard page at /ai-inventory.

Upload endpoint starts processing in a background thread and returns a task_id.
The client polls /api/ai-inventory/status/<task_id> until status == 'ready'.
"""

import os
import logging
import secrets
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, send_from_directory, session as flask_session
from auth_routes import login_required
from data_store import get_connection
from inventory_ai_reconcile import confirm_session

logger = logging.getLogger(__name__)

_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_ai.log")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _fh = logging.FileHandler(_log_file)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_fh)
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

# ── Blueprint ─────────────────────────────────────────────────────────────────
ai_inventory_bp = Blueprint("ai_inventory", __name__, url_prefix="/api/ai-inventory")

INTAKE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_intake")
os.makedirs(INTAKE_DIR, exist_ok=True)

# ── In-memory job store (survives only while process is running) ──────────────
# {task_id: {status, session_id, item_count, flagged_count, error, started_at}}
_jobs: dict = {}
_jobs_lock = threading.Lock()

ALLOWED_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/network-info — local network routing
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/network-info", methods=["GET"])
def network_info():
    """Return local network upload URL for bypassing Cloudflare 100MB limit."""
    return jsonify({
        "local_ip": "10.1.10.83",
        "local_port": 8080,
        "local_upload_base": "http://10.1.10.83:8080",
    })


# ─────────────────────────────────────────────────────────────────────────────
# HTML page
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/page")
def serve_page():
    """Served by server.py at /ai-inventory — this route is a fallback."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static"),
        "ai_inventory.html",
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/sessions  — create a new session (login required)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions", methods=["POST"])
@login_required
def create_session():
    """
    Create a new AI inventory session.
    Returns session_id + upload_token so the iOS Shortcut can later upload.

    Body (JSON):
        { "location": "chatham", "started_by": "mike" }
    """
    body     = request.get_json(silent=True) or {}
    location = body.get("location", "chatham")
    if isinstance(location, dict): location = next(iter(location.values()), "chatham")
    location = str(location).strip().lower()
    # Normalise location slug to display name
    loc_map  = {"chatham": "Chatham", "dennis port": "Dennis Port", "dennis": "Dennis Port"}
    location_display = loc_map.get(location, location.title())
    started_by = body.get("started_by", "manager").strip()

    upload_token = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO ai_inventory_sessions
                (location, status, source_type, upload_token, started_by, started_at, session_date)
            VALUES (?, 'started', 'glasses', ?, ?, ?, date('now'))
            """,
            (location_display, upload_token, started_by, now)
        )
        session_id = cur.lastrowid
        conn.commit()
        logger.info("create_session: session_id=%d location=%s started_by=%s",
                    session_id, location_display, started_by)
        return jsonify({
            "session_id":    session_id,
            "upload_token":  upload_token,
            "location":      location_display,
            "status":        "started",
            "started_at":    now,
        })
    except Exception as exc:
        conn.rollback()
        logger.error("create_session: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/active-session  — no auth (used by iOS Shortcut)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/active-session", methods=["GET"])
def get_active_session():
    """
    Returns the most recent non-confirmed session for this location.
    Used by iOS Shortcut to know which session to upload to.
    No auth required — Shortcut runs outside browser session.
    Only exposes session ID, status, token — no inventory data.
    """
    location = request.args.get("location", "chatham").strip().lower()
    loc_map  = {"chatham": "Chatham", "dennis port": "Dennis Port", "dennis": "Dennis Port"}
    location_display = loc_map.get(location, location.title())

    conn = get_connection()
    try:
        # Auto-abandon sessions older than 2 hours
        conn.execute("""
            UPDATE ai_inventory_sessions
            SET status = 'abandoned'
            WHERE status IN ('started', 'recording_uploaded', 'recording')
            AND started_at < datetime('now', '-2 hours')
        """)
        conn.commit()

        row = conn.execute("""
            SELECT id, location, status, started_at, upload_token, upload_count
            FROM ai_inventory_sessions
            WHERE status IN ('started', 'recording', 'recording_uploaded', 'processing')
            AND location = ?
            ORDER BY started_at DESC LIMIT 1
        """, (location_display,)).fetchone()

        if not row:
            return jsonify({"error": "No active session"}), 404

        return jsonify({
            "session_id":   row["id"],
            "location":     row["location"],
            "status":       row["status"],
            "started_at":   row["started_at"],
            "upload_token": row["upload_token"],
            "upload_count": row["upload_count"] or 0,
        })
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/sessions/<id>/upload  — token auth (iOS Shortcut)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions/<int:session_id>/upload", methods=["POST"])
def session_upload(session_id: int):
    """
    Accept a video/audio file for a specific session.
    Auth: ?token=<upload_token> query param (no cookie auth needed for iOS Shortcut).
    Also accepts browser upload if the user is logged in (falls back to cookie auth).

    Form fields:
        video : the recording file

    Returns:
        { "status": "ok", "task_id": "...", "session_id": N }
    """
    token = request.args.get("token", "").strip()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ai_inventory_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "Session not found"}), 404

    # Auth: accept valid upload_token OR active browser session
    authed_by_cookie = "user_id" in flask_session
    authed_by_token  = bool(token) and token == row["upload_token"]
    if not authed_by_cookie and not authed_by_token:
        return jsonify({"error": "Invalid token"}), 403

    if row["status"] not in ("started", "recording", "recording_uploaded"):
        return jsonify({"error": f"Session is not accepting uploads (status={row['status']})"}), 409

    f = request.files.get("video") or request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided (field name: 'video')"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type '{ext}'"}), 400

    # Determine next sequence number
    current_count = row["upload_count"] or 0
    seq = current_count + 1

    # Save to intake folder with sequence number
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    loc_slug = (row["location"] or "unknown").lower().replace(" ", "_")
    filename = f"session{session_id}_seq{seq}_{loc_slug}_{ts}{ext}"
    file_path = os.path.join(INTAKE_DIR, filename)
    f.save(file_path)
    logger.info("session_upload: saved %s (%d bytes) session_id=%d seq=%d",
                filename, os.path.getsize(file_path), session_id, seq)

    # Mark session as recording, increment upload_count — do NOT trigger processing
    conn2 = get_connection()
    try:
        conn2.execute(
            "UPDATE ai_inventory_sessions SET status='recording', upload_count=? WHERE id=?",
            (seq, session_id)
        )
        conn2.commit()
    finally:
        conn2.close()

    return jsonify({
        "status": "ok",
        "session_id": session_id,
        "upload_count": seq,
        "filename": filename,
    })


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/sessions/<id>/process — "Done Recording" trigger
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions/<int:session_id>/process", methods=["POST"])
def session_process(session_id: int):
    """
    Concatenate all uploaded files for a session and start AI processing.
    Auth: ?token=<upload_token> OR browser cookie.

    The endpoint:
    1. Finds all session files in INTAKE_DIR (pattern: session{id}_seq*)
    2. If >1 file, ffmpeg concat → single combined file
    3. Starts background _worker thread (same as old upload flow)
    4. Sets status to 'processing'

    Returns: { "status": "ok", "task_id": "...", "session_id": N, "file_count": N }
    """
    import glob as glob_mod
    import subprocess

    token = request.args.get("token", "").strip()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ai_inventory_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "Session not found"}), 404

    # Auth: accept valid upload_token OR active browser session
    authed_by_cookie = "user_id" in flask_session
    authed_by_token  = bool(token) and token == row["upload_token"]
    if not authed_by_cookie and not authed_by_token:
        return jsonify({"error": "Invalid token"}), 403

    if row["status"] not in ("recording", "recording_uploaded", "draft"):
        return jsonify({"error": f"Session not in recording state (status={row['status']})"}), 409

    # If reprocessing a draft, clear old items first
    if row["status"] == "draft":
        conn_clear = get_connection()
        try:
            conn_clear.execute("DELETE FROM ai_inventory_items WHERE session_id = ?", (session_id,))
            conn_clear.commit()
            logger.info("session_process: cleared %d old items for reprocessing session %d",
                        conn_clear.total_changes, session_id)
        finally:
            conn_clear.close()

    upload_count = row["upload_count"] or 0
    if upload_count < 1:
        return jsonify({"error": "No files uploaded yet"}), 400

    # Find all uploaded files for this session, sorted by name (seq order)
    pattern = os.path.join(INTAKE_DIR, f"session{session_id}_seq*")
    files = sorted(glob_mod.glob(pattern))
    if not files:
        return jsonify({"error": "No upload files found on disk"}), 404

    logger.info("session_process: session_id=%d found %d files: %s",
                session_id, len(files), [os.path.basename(f) for f in files])

    # Determine final file path — concat if multiple, else use the single file
    if len(files) == 1:
        combined_path = files[0]
    else:
        # Build ffmpeg concat demuxer input file
        concat_list = os.path.join(INTAKE_DIR, f"session{session_id}_concat.txt")
        with open(concat_list, "w") as cl:
            for fp in files:
                cl.write(f"file '{fp}'\n")

        ext = os.path.splitext(files[0])[1]
        combined_path = os.path.join(INTAKE_DIR, f"session{session_id}_combined{ext}")

        try:
            result = subprocess.run(
                ["/usr/bin/ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_list, "-c", "copy", combined_path],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                logger.error("session_process: ffmpeg concat failed: %s", result.stderr)
                return jsonify({"error": "ffmpeg concat failed: " + result.stderr[:200]}), 500
            logger.info("session_process: concatenated %d files → %s (%d bytes)",
                        len(files), os.path.basename(combined_path),
                        os.path.getsize(combined_path))
        except subprocess.TimeoutExpired:
            return jsonify({"error": "ffmpeg concat timed out"}), 500
        finally:
            # Clean up concat list file
            if os.path.exists(concat_list):
                os.remove(concat_list)

    # Register job and start background worker
    task_id = str(uuid.uuid4())
    location = row["location"]
    with _jobs_lock:
        _jobs[task_id] = {
            "status":       "processing",
            "session_id":   session_id,
            "item_count":   0,
            "flagged_count": 0,
            "error":        None,
            "started_at":   datetime.now(timezone.utc).isoformat(),
            "location":     location,
            "file_count":   len(files),
        }

    def _process_worker(task_id: str, file_path: str, location: str, session_id: int):
        # Update DB status to processing
        conn3 = get_connection()
        try:
            conn3.execute(
                "UPDATE ai_inventory_sessions SET status='processing' WHERE id=?",
                (session_id,)
            )
            conn3.commit()
        finally:
            conn3.close()

        try:
            from inventory_ai_reconcile import process_inventory
            out_session_id = process_inventory(file_path, location)
            conn4 = get_connection()
            try:
                r = conn4.execute(
                    "SELECT item_count, flagged_count FROM ai_inventory_sessions WHERE id = ?",
                    (out_session_id,)
                ).fetchone()
                ic = r["item_count"]    if r else 0
                fc = r["flagged_count"] if r else 0

                # Bridge: merge into the ORIGINAL session so the frontend sees the transition
                if out_session_id != session_id:
                    conn4.execute(
                        """UPDATE ai_inventory_sessions
                           SET status='draft', item_count=?, flagged_count=?
                           WHERE id=?""",
                        (ic, fc, session_id)
                    )
                    conn4.execute(
                        "UPDATE ai_inventory_items SET session_id=? WHERE session_id=?",
                        (session_id, out_session_id)
                    )
                    draft = conn4.execute(
                        "SELECT raw_transcript, audio_file_path, source_type, auto_confirmed_count FROM ai_inventory_sessions WHERE id=?",
                        (out_session_id,)
                    ).fetchone()
                    if draft:
                        conn4.execute(
                            """UPDATE ai_inventory_sessions
                               SET raw_transcript=?, audio_file_path=?, source_type=?, auto_confirmed_count=?
                               WHERE id=?""",
                            (draft["raw_transcript"], draft["audio_file_path"],
                             draft["source_type"], draft["auto_confirmed_count"], session_id)
                        )
                    conn4.execute("DELETE FROM ai_inventory_sessions WHERE id=?", (out_session_id,))
                    conn4.commit()
                    logger.info("session_process: merged draft session %d into original session %d",
                                out_session_id, session_id)
                    out_session_id = session_id
            finally:
                conn4.close()

            with _jobs_lock:
                _jobs[task_id].update({
                    "status":        "ready",
                    "session_id":    out_session_id,
                    "item_count":    ic,
                    "flagged_count": fc,
                })
            logger.info("session_process job %s complete: session_id=%d items=%d flagged=%d",
                        task_id, out_session_id, ic, fc)

        except Exception as exc:
            logger.error("session_process job %s failed: %s", task_id, exc, exc_info=True)
            conn5 = get_connection()
            try:
                conn5.execute(
                    "UPDATE ai_inventory_sessions SET status='error' WHERE id=?",
                    (session_id,)
                )
                conn5.commit()
            finally:
                conn5.close()
            with _jobs_lock:
                _jobs[task_id].update({"status": "error", "error": str(exc)})

    threading.Thread(
        target=_process_worker, args=(task_id, combined_path, location, session_id), daemon=True
    ).start()

    return jsonify({
        "status": "ok",
        "task_id": task_id,
        "session_id": session_id,
        "file_count": len(files),
    })


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/sessions/<id>/status  — poll session status (login required)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions/<int:session_id>/status", methods=["GET"])
@login_required
def session_status(session_id: int):
    """Poll session status. Returns DB status + task info if processing."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, location, status, item_count, flagged_count, started_at, upload_count FROM ai_inventory_sessions WHERE id=?",
            (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "Session not found"}), 404

    resp = dict(row)
    # Attach in-memory job info if available
    with _jobs_lock:
        for tid, job in _jobs.items():
            if job.get("session_id") == session_id:
                resp["task_id"] = tid
                resp["job_status"] = job["status"]
                resp["job_error"] = job.get("error")
                break
    return jsonify(resp)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/sessions/<id>/cancel  — abandon session (login required)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions/<int:session_id>/cancel", methods=["POST"])
def cancel_session(session_id: int):
    """Mark a started session as abandoned. Accepts login session or token auth."""
    # Allow token auth (from local upload page) or login session
    token = request.args.get("token")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status, token FROM ai_inventory_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Session not found"}), 404
        # Verify auth: either logged in or valid token
        if token:
            if token != row["token"]:
                return jsonify({"error": "Invalid token"}), 403
        else:
            from auth_routes import login_required as _lr
            # If no token, require login — check if user is authenticated
            from flask_login import current_user
            if not current_user.is_authenticated:
                return jsonify({"error": "Unauthorized"}), 401
        if row["status"] == "confirmed":
            return jsonify({"error": "Cannot cancel a confirmed session"}), 409
        conn.execute(
            "UPDATE ai_inventory_sessions SET status='abandoned' WHERE id=?",
            (session_id,)
        )
        conn.commit()
        logger.info("cancel_session: session_id=%d abandoned", session_id)
        return jsonify({"success": True})
    except Exception as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/sessions/recent  — last 5 sessions (login required)
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/sessions/recent", methods=["GET"])
@login_required
def recent_sessions():
    """Return last 5 sessions for a location (any status except abandoned)."""
    location = request.args.get("location", "").strip()
    conn = get_connection()
    try:
        where = "WHERE status != 'abandoned'"
        params: list = []
        if location:
            where += " AND location = ?"
            params.append(location)
        rows = conn.execute(
            f"""
            SELECT id, location, status, item_count, flagged_count,
                   session_date, started_at, confirmed_at, confirmed_by
            FROM ai_inventory_sessions
            {where}
            ORDER BY COALESCE(started_at, created_at) DESC LIMIT 5
            """,
            params
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/upload
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    """
    Accept a video or audio file, save it to inventory_intake/,
    and start process_inventory() in a background thread.

    Form fields:
        file     : the recording
        location : 'Chatham' or 'Dennis Port' (default 'Chatham')

    Returns:
        {task_id, status: 'processing'}
    """
    location = request.form.get("location", "Chatham").strip()

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type '{ext}'"}), 400

    # Save to intake folder
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    loc_slug = location.lower().replace(" ", "_")
    filename = f"{loc_slug}_{ts}{ext}"
    file_path = os.path.join(INTAKE_DIR, filename)
    f.save(file_path)
    logger.info("upload: saved %s (%d bytes) for location=%s", filename, os.path.getsize(file_path), location)

    # Register job
    task_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[task_id] = {
            "status":       "processing",
            "session_id":   None,
            "item_count":   0,
            "flagged_count": 0,
            "error":        None,
            "started_at":   datetime.now(timezone.utc).isoformat(),
            "location":     location,
            "filename":     filename,
        }

    def _worker(task_id: str, file_path: str, location: str):
        try:
            from inventory_ai_reconcile import process_inventory
            session_id = process_inventory(file_path, location)
            # Read back counts from DB
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT item_count, flagged_count FROM ai_inventory_sessions WHERE id = ?",
                    (session_id,)
                ).fetchone()
                item_count    = row["item_count"]    if row else 0
                flagged_count = row["flagged_count"] if row else 0
            finally:
                conn.close()

            with _jobs_lock:
                _jobs[task_id].update({
                    "status":        "ready",
                    "session_id":    session_id,
                    "item_count":    item_count,
                    "flagged_count": flagged_count,
                })
            logger.info("job %s complete: session_id=%d items=%d flagged=%d",
                        task_id, session_id, item_count, flagged_count)

        except Exception as exc:
            logger.error("job %s failed: %s", task_id, exc, exc_info=True)
            with _jobs_lock:
                _jobs[task_id].update({"status": "error", "error": str(exc)})

    threading.Thread(target=_worker, args=(task_id, file_path, location), daemon=True).start()
    return jsonify({"task_id": task_id, "status": "processing"})


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/status/<task_id>
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/status/<task_id>")
@login_required
def job_status(task_id: str):
    """Poll for background processing result."""
    with _jobs_lock:
        job = dict(_jobs.get(task_id, {}))
    if not job:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(job)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/drafts
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/drafts")
@login_required
def list_drafts():
    """List AI sessions with status='draft', newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, location, session_date, status, source_type,
                   item_count, auto_confirmed_count, flagged_count,
                   created_at, confirmed_at, confirmed_by
            FROM ai_inventory_sessions
            WHERE status = 'draft'
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/drafts/<session_id>
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/drafts/<int:session_id>")
@login_required
def get_draft(session_id: int):
    """
    Return a single draft session with all its items.
    Items include audio_quantity, vision_quantity, reconciled_quantity, flag, etc.
    """
    conn = get_connection()
    try:
        sess = conn.execute(
            """
            SELECT id, location, session_date, status, source_type,
                   audio_file_path, video_file_path, raw_transcript,
                   item_count, auto_confirmed_count, flagged_count,
                   count_session_id, created_at
            FROM ai_inventory_sessions WHERE id = ?
            """,
            (session_id,)
        ).fetchone()
        if not sess:
            return jsonify({"error": "Session not found"}), 404

        items = conn.execute(
            """
            SELECT i.id, i.product_id, i.product_name,
                   i.storage_location_id,
                   sl.name AS storage_location_name,
                   i.quantity, i.unit, i.is_partial,
                   i.audio_quantity, i.audio_confidence,
                   i.vision_quantity, i.vision_confidence,
                   i.reconciled_quantity, i.reconciled_confidence,
                   i.flag, i.flag_notes,
                   i.confirmed_quantity, i.created_at
            FROM ai_inventory_items i
            LEFT JOIN storage_locations sl ON sl.id = i.storage_location_id
            WHERE i.session_id = ?
            ORDER BY sl.name, i.product_name
            """,
            (session_id,)
        ).fetchall()

        return jsonify({
            "session": dict(sess),
            "items":   [dict(r) for r in items],
        })
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai-inventory/drafts/<session_id>/confirm
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/drafts/<int:session_id>/confirm", methods=["POST"])
@login_required
def confirm_draft(session_id: int):
    """
    Confirm a draft session.

    Body (JSON):
        {
          "confirmed_by": "manager name",
          "items": [
            {"item_id": 1, "confirmed_quantity": 3.5},
            ...
          ]
        }

    Calls confirm_session() which:
    - Updates ai_inventory_items.confirmed_quantity
    - Writes ai_inventory_history
    - Bridges into count_items (manual system)
    - Sets session status = 'confirmed'
    """
    body = request.get_json(silent=True) or {}
    raw_items    = body.get("items", [])
    confirmed_by = body.get("confirmed_by", "manager")

    if not raw_items:
        return jsonify({"error": "No items provided"}), 400

    # Fetch product info for each item to pass to confirm_session
    conn = get_connection()
    try:
        confirmed_items = []
        for ci in raw_items:
            item_id = ci.get("item_id")
            qty     = ci.get("confirmed_quantity")
            if item_id is None or qty is None:
                continue
            row = conn.execute(
                "SELECT product_id, product_name FROM ai_inventory_items WHERE id = ? AND session_id = ?",
                (item_id, session_id)
            ).fetchone()
            if row:
                confirmed_items.append({
                    "ai_item_id":     item_id,
                    "confirmed_qty":  float(qty),
                    "product_id":     row["product_id"],
                    "product_name":   row["product_name"],
                })
    finally:
        conn.close()

    if not confirmed_items:
        return jsonify({"error": "No valid items to confirm"}), 400

    ok = confirm_session(session_id, confirmed_items, confirmed_by=confirmed_by)
    if ok:
        return jsonify({"success": True, "session_id": session_id,
                        "confirmed_count": len(confirmed_items)})
    else:
        return jsonify({"error": "confirm_session failed — check inventory_ai.log"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# PUT /api/ai-inventory/drafts/<session_id>/items/<item_id>
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/drafts/<int:session_id>/items/<int:item_id>", methods=["PUT"])
@login_required
def update_item(session_id: int, item_id: int):
    """
    Edit a single item's reconciled_quantity and/or flag before confirmation.

    Body (JSON): { "reconciled_quantity": 2.5, "flag": "none", "flag_notes": "" }
    """
    body = request.get_json(silent=True) or {}
    qty  = body.get("reconciled_quantity")

    if qty is None:
        return jsonify({"error": "reconciled_quantity required"}), 400

    conn = get_connection()
    try:
        flag       = body.get("flag", "none")
        flag_notes = body.get("flag_notes", "")
        new_name   = body.get("product_name")
        if new_name:
            conn.execute(
                """
                UPDATE ai_inventory_items
                SET reconciled_quantity = ?, product_name = ?, flag = ?, flag_notes = ?
                WHERE id = ? AND session_id = ?
                """,
                (float(qty), new_name, flag, flag_notes, item_id, session_id)
            )
        else:
            conn.execute(
                """
                UPDATE ai_inventory_items
                SET reconciled_quantity = ?, flag = ?, flag_notes = ?
                WHERE id = ? AND session_id = ?
                """,
                (float(qty), flag, flag_notes, item_id, session_id)
            )
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        conn.rollback()
        logger.error("update_item: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@ai_inventory_bp.route("/drafts/<int:session_id>/items/<int:item_id>", methods=["DELETE"])
@login_required
def delete_item(session_id: int, item_id: int):
    """Remove an item from a draft session (false positive, etc.)."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM ai_inventory_items WHERE id = ? AND session_id = ?",
            (item_id, session_id)
        )
        conn.commit()
        logger.info("delete_item: removed item %d from session %d", item_id, session_id)
        return jsonify({"success": True})
    except Exception as exc:
        conn.rollback()
        logger.error("delete_item: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/ai-inventory/drafts/<session_id>
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/drafts/<int:session_id>", methods=["DELETE"])
@login_required
def delete_draft(session_id: int):
    """
    Delete a draft AI inventory session and its items.
    Refuses to delete already-confirmed sessions (409 Conflict).
    """
    conn = get_connection()
    try:
        sess = conn.execute(
            "SELECT id, status FROM ai_inventory_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        if sess["status"] == "confirmed":
            return jsonify({"error": "Cannot delete a confirmed session"}), 409

        # CASCADE on ai_inventory_items handles items automatically
        conn.execute("DELETE FROM ai_inventory_history WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM ai_inventory_sessions WHERE id = ?", (session_id,))
        conn.commit()
        logger.info("delete_draft: deleted session_id=%d", session_id)
        return jsonify({"success": True, "session_id": session_id})
    except Exception as exc:
        conn.rollback()
        logger.error("delete_draft: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai-inventory/history
# ─────────────────────────────────────────────────────────────────────────────

@ai_inventory_bp.route("/history")
@login_required
def get_history():
    """List confirmed AI inventory sessions, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, location, session_date, status, source_type,
                   item_count, auto_confirmed_count, flagged_count,
                   created_at, confirmed_at, confirmed_by
            FROM ai_inventory_sessions
            WHERE status = 'confirmed'
            ORDER BY confirmed_at DESC
            LIMIT 100
            """
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()
