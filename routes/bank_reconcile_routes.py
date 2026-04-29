"""
Bank Reconciliation routes — upload a PDF statement, parse it, dedupe against
the existing register, and import the missing rows as manual_bank_entries.

Blueprint: bank_reconcile_bp at /api/bank-reconcile/*

Endpoints:
    POST /api/bank-reconcile/upload
        multipart: file=<pdf>, account_id=<int>
        Returns: { upload_id, parsed: {…parser output…},
                   matches: [ {parsed_index, register_match: {...} | null,
                               match_kind: "exact"|"likely"|"none"}, … ] }

    POST /api/bank-reconcile/import
        json: { upload_id, indexes: [int,…], also_clear_matches: bool }
        Inserts the chosen parsed rows as manual_bank_entries. If
        also_clear_matches=true, marks the matched register rows as cleared.
        Returns: { inserted: N, cleared: M }

    GET  /api/bank-reconcile/uploads?account_id=<int>
        Lists past uploads for an account.

    GET  /api/bank-reconcile/uploads/<id>
        Returns the saved parsed result + match list for re-review.

The parser lives in integrations.bank_statements.processor.

Storage:
    bank_statement_uploads table — one row per PDF uploaded. Stores the raw
    parsed JSON so a user can re-open the review screen without re-uploading.

The actual transactions are written to the existing manual_bank_entries
table (used by the register), so they automatically show up in the register.
We tag them with `created_by = 'statement-import'` and `memo = "[stmt #<id>] …"`
so they can be traced back.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request, session

from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required, admin_required

logger = logging.getLogger(__name__)

bank_reconcile_bp = Blueprint("bank_reconcile_bp", __name__)

# Where uploaded statement PDFs are kept on disk.
STATEMENT_DIR = Path(os.getenv("BANK_STATEMENT_DIR", "data/bank_statements"))
STATEMENT_DIR.mkdir(parents=True, exist_ok=True)


# ─── TABLE INIT ──────────────────────────────────────────────────────────────

def init_bank_reconcile_tables():
    """Create the upload-history table. Idempotent."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bank_statement_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_account_id INTEGER NOT NULL,
            filename TEXT,
            file_path TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            period_start TEXT,
            period_end TEXT,
            beginning_balance REAL,
            ending_balance REAL,
            total_debits REAL,
            total_credits REAL,
            transaction_count INTEGER DEFAULT 0,
            imported_count INTEGER DEFAULT 0,
            parsed_json TEXT,                 -- full parser output
            warnings_json TEXT,               -- list of strings
            FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_bsu_account ON bank_statement_uploads(bank_account_id);
        CREATE INDEX IF NOT EXISTS idx_bsu_period ON bank_statement_uploads(period_start, period_end);
    """)

    # Tag manual_bank_entries with the statement upload that created them, so
    # we can avoid re-importing on a second upload of the same period.
    try:
        conn.execute("ALTER TABLE manual_bank_entries ADD COLUMN statement_upload_id INTEGER")
    except Exception:
        pass  # already exists

    conn.commit()
    conn.close()


# ─── UPLOAD + PARSE ──────────────────────────────────────────────────────────

@bank_reconcile_bp.route("/api/bank-reconcile/upload", methods=["POST"])
@login_required
def upload_statement():
    """Accept a PDF, parse it, dedupe against the register, persist the parse
    result, and return the full review payload."""
    from integrations.bank_statements.processor import parse_bank_statement_pdf

    account_id = request.form.get("account_id") or request.args.get("account_id")
    if not account_id:
        return jsonify({"error": "account_id is required"}), 400
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return jsonify({"error": "account_id must be an integer"}), 400

    file = request.files.get("file") or request.files.get("statement")
    if not file:
        return jsonify({"error": "No file uploaded (form field 'file')"}), 400

    pdf_bytes = file.read()
    if not pdf_bytes:
        return jsonify({"error": "Uploaded file is empty"}), 400

    # Validate the account exists
    conn = get_connection()
    acct = conn.execute(
        "SELECT id, name, account_last4 FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if not acct:
        conn.close()
        return jsonify({"error": "Account not found"}), 404

    # Save the file on disk
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = (file.filename or "statement.pdf").replace("/", "_").replace("\\", "_")
    file_path = STATEMENT_DIR / f"acct{account_id}_{ts}_{safe_name}"
    try:
        file_path.write_bytes(pdf_bytes)
    except Exception as e:
        logger.exception("Failed to save statement PDF")
        conn.close()
        return jsonify({"error": f"Could not write file: {e}"}), 500

    # Parse
    try:
        parsed = parse_bank_statement_pdf(pdf_bytes)
    except Exception as e:
        logger.exception("Statement parse failed")
        conn.close()
        return jsonify({
            "error": f"Parse failed: {e}",
            "file_path": str(file_path),
        }), 500

    # Verify the uploaded statement is actually for the selected account.
    # The parser pulls account_last4 from the PDF header (looks for the
    # known last4s 5975 / 2757). If it found one and it doesn't match the
    # bank_account record's last4, reject the upload — this prevents a Dennis
    # statement from being imported as Chatham (or vice-versa).
    parsed_last4 = (parsed.get("account_last4") or "").strip()
    expected_last4 = (acct["account_last4"] or "").strip()
    if parsed_last4 and expected_last4 and parsed_last4 != expected_last4:
        # Wrong account picked. Delete the saved file and bail.
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        conn.close()
        return jsonify({
            "error": (
                f"Account mismatch: you selected {acct['name']} "
                f"(•••{expected_last4}), but this statement is for an account "
                f"ending in •••{parsed_last4}. Pick the matching account and try again."
            ),
            "expected_last4": expected_last4,
            "found_last4": parsed_last4,
        }), 400

    # If the parser couldn't find a last4 at all, surface a soft warning so
    # the user knows we couldn't auto-verify.
    if expected_last4 and not parsed_last4:
        parsed.setdefault("warnings", []).append(
            f"Could not detect account number on the PDF — proceeding under "
            f"the assumption it's {acct['name']} (•••{expected_last4})."
        )

    # Dedupe against the register
    register_rows = _load_register_rows_for_period(conn, account_id, parsed)
    matches = _match_transactions(parsed.get("transactions", []), register_rows)

    # Persist the upload record
    uploaded_by = session.get("username") or session.get("email") or "unknown"
    cur = conn.execute(
        """INSERT INTO bank_statement_uploads
           (bank_account_id, filename, file_path, uploaded_by,
            period_start, period_end, beginning_balance, ending_balance,
            total_debits, total_credits, transaction_count,
            parsed_json, warnings_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            account_id,
            safe_name,
            str(file_path),
            uploaded_by,
            parsed.get("period_start"),
            parsed.get("period_end"),
            parsed.get("beginning_balance"),
            parsed.get("ending_balance"),
            parsed.get("total_debits") or 0,
            parsed.get("total_credits") or 0,
            len(parsed.get("transactions", [])),
            json.dumps(parsed),
            json.dumps(parsed.get("warnings", [])),
        ),
    )
    upload_id = cur.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "upload_id": upload_id,
        "account": dict(acct),
        "parsed": parsed,
        "matches": matches,
    })


# ─── IMPORT SELECTED ROWS ────────────────────────────────────────────────────

@bank_reconcile_bp.route("/api/bank-reconcile/import", methods=["POST"])
@login_required
def import_selected():
    """Insert the selected parsed rows into manual_bank_entries.

    Body: {
        "upload_id": int,
        "indexes":   [int, …]          // 0-based indexes into parsed.transactions
        "also_clear_matches": bool     // optional — if true, matched register rows
                                       // get cleared = 1 even if not imported
    }
    """
    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id")
    indexes = data.get("indexes") or []
    also_clear = bool(data.get("also_clear_matches"))

    if not isinstance(upload_id, int) or not isinstance(indexes, list):
        return jsonify({"error": "upload_id (int) and indexes (list) required"}), 400

    conn = get_connection()
    upload = conn.execute(
        "SELECT * FROM bank_statement_uploads WHERE id = ?", (upload_id,)
    ).fetchone()
    if not upload:
        conn.close()
        return jsonify({"error": "Upload not found"}), 404

    parsed = json.loads(upload["parsed_json"]) if upload["parsed_json"] else {}
    transactions = parsed.get("transactions", [])
    account_id = upload["bank_account_id"]

    # Re-run match so we know which rows are dupes (in case register changed
    # between upload and import).
    register_rows = _load_register_rows_for_period(conn, account_id, parsed)
    matches = _match_transactions(transactions, register_rows)
    match_by_index = {m["parsed_index"]: m for m in matches}

    created_by = session.get("username") or session.get("email") or "statement-import"

    inserted = 0
    cleared_total = 0
    for idx in indexes:
        if not isinstance(idx, int) or idx < 0 or idx >= len(transactions):
            continue
        tx = transactions[idx]

        # Signed amount: positive = inflow, negative = outflow
        debit = float(tx.get("debit") or 0)
        credit = float(tx.get("credit") or 0)
        signed = credit - debit
        if signed == 0:
            continue

        entry_type = _entry_type_from_tx(tx)
        memo_parts = []
        if tx.get("memo"):
            memo_parts.append(tx["memo"])
        memo_parts.append(f"[stmt #{upload_id}]")
        memo = " ".join(memo_parts).strip()

        # Apply GL rule (if any) so freshly imported rows pre-fill the
        # right account. Falls back to NULL if no rule matches yet.
        from routes.register_routes import _find_gl_account_for_description
        gl_id = _find_gl_account_for_description(
            conn, (tx.get("description") or "") + " " + (tx.get("memo") or "")
        )

        cur = conn.execute(
            """INSERT INTO manual_bank_entries
               (bank_account_id, entry_date, entry_type, payee, memo,
                ref_number, amount, cleared, cleared_date, created_by,
                statement_upload_id, gl_account_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                account_id,
                tx.get("date"),
                entry_type,
                tx.get("description") or "",
                memo,
                tx.get("ref") or None,
                round(signed, 2),
                tx.get("date"),
                created_by,
                upload_id,
                gl_id,
            ),
        )
        if cur.rowcount:
            inserted += 1

    # Optionally clear the register rows that matched parsed transactions.
    if also_clear:
        for m in matches:
            reg = m.get("register_match")
            if not reg:
                continue
            if m.get("match_kind") == "none":
                continue
            cleared_total += _mark_cleared(conn, reg["source"], reg["id"], reg.get("date"))

    conn.execute(
        "UPDATE bank_statement_uploads SET imported_count = imported_count + ? WHERE id = ?",
        (inserted, upload_id),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok",
        "inserted": inserted,
        "cleared": cleared_total,
    })


# ─── HISTORY ─────────────────────────────────────────────────────────────────

@bank_reconcile_bp.route("/api/bank-reconcile/uploads", methods=["GET"])
@login_required
def list_uploads():
    account_id = request.args.get("account_id")
    conn = get_connection()
    if account_id:
        rows = conn.execute(
            """SELECT id, bank_account_id, filename, uploaded_by, uploaded_at,
                      period_start, period_end, beginning_balance, ending_balance,
                      total_debits, total_credits, transaction_count, imported_count
               FROM bank_statement_uploads
               WHERE bank_account_id = ?
               ORDER BY uploaded_at DESC""",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, bank_account_id, filename, uploaded_by, uploaded_at,
                      period_start, period_end, beginning_balance, ending_balance,
                      total_debits, total_credits, transaction_count, imported_count
               FROM bank_statement_uploads
               ORDER BY uploaded_at DESC LIMIT 200"""
        ).fetchall()
    conn.close()
    return jsonify({"uploads": [dict(r) for r in rows]})


@bank_reconcile_bp.route("/api/bank-reconcile/uploads/<int:upload_id>", methods=["GET"])
@login_required
def get_upload(upload_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM bank_statement_uploads WHERE id = ?", (upload_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Upload not found"}), 404

    parsed = json.loads(row["parsed_json"]) if row["parsed_json"] else {}
    register_rows = _load_register_rows_for_period(conn, row["bank_account_id"], parsed)
    matches = _match_transactions(parsed.get("transactions", []), register_rows)

    out = dict(row)
    out["parsed"] = parsed
    out["matches"] = matches
    out.pop("parsed_json", None)
    conn.close()
    return jsonify(out)


@bank_reconcile_bp.route("/api/bank-reconcile/uploads/<int:upload_id>/raw-text", methods=["GET"])
@login_required
def get_upload_raw_text(upload_id):
    """Diagnostic: return the raw text pdfplumber extracted from this upload's
    PDF, so we can tune the parser regex against the real statement format."""
    from integrations.bank_statements.processor import _extract_text

    conn = get_connection()
    row = conn.execute(
        "SELECT file_path FROM bank_statement_uploads WHERE id = ?", (upload_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Upload not found"}), 404
    if not row["file_path"] or not os.path.exists(row["file_path"]):
        return jsonify({"error": f"PDF file missing on disk: {row['file_path']}"}), 404

    try:
        with open(row["file_path"], "rb") as f:
            full, pages = _extract_text(f.read())
    except Exception as e:
        return jsonify({"error": f"Extract failed: {e}"}), 500

    return jsonify({
        "upload_id": upload_id,
        "page_count": len(pages),
        "char_count": len(full),
        "full_text": full,
        "page_lengths": [len(p) for p in pages],
    })


@bank_reconcile_bp.route("/api/bank-reconcile/uploads/<int:upload_id>", methods=["DELETE"])
@admin_required
def delete_upload(upload_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT file_path FROM bank_statement_uploads WHERE id = ?", (upload_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Upload not found"}), 404

    # NOTE: this only deletes the upload metadata. manual_bank_entries created
    # via this upload remain — to clean those up too, also DELETE
    # manual_bank_entries WHERE statement_upload_id = ?.
    conn.execute("DELETE FROM bank_statement_uploads WHERE id = ?", (upload_id,))
    conn.commit()
    conn.close()

    try:
        if row["file_path"] and os.path.exists(row["file_path"]):
            os.remove(row["file_path"])
    except Exception as e:
        logger.warning(f"Could not remove statement file {row['file_path']}: {e}")

    return jsonify({"status": "ok"})


# ─── MATCHING LOGIC ──────────────────────────────────────────────────────────

def _load_register_rows_for_period(conn, account_id: int, parsed: dict) -> list[dict]:
    """Pull every register row (bill pay, payroll, deposit, manual) within a
    window around the statement period. Used for dedupe matching."""
    start = parsed.get("period_start")
    end = parsed.get("period_end")

    today = date.today()
    if not start:
        start = (today - timedelta(days=120)).strftime("%Y-%m-%d")
    if not end:
        end = today.strftime("%Y-%m-%d")

    # Widen window by 7 days on each side — checks often clear before/after
    # the statement boundary.
    try:
        s_dt = datetime.strptime(start, "%Y-%m-%d").date()
        e_dt = datetime.strptime(end, "%Y-%m-%d").date()
        start = (s_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (e_dt + timedelta(days=7)).strftime("%Y-%m-%d")
    except Exception:
        pass

    rows: list[dict] = []

    # Bill pay (vendor_payments) — also include rows with NULL bank_account_id
    # for the Chatham account (catch-all per register_routes convention).
    acct = conn.execute(
        "SELECT account_last4 FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    is_default = bool(acct and acct["account_last4"] == "5975")

    bp_clause = "(bank_account_id = ?" + (" OR bank_account_id IS NULL" if is_default else "") + ")"
    for r in conn.execute(
        f"""SELECT id, vendor, payment_date AS date, payment_total AS amount,
                  check_number, payment_method, payment_ref, memo, status
            FROM vendor_payments
            WHERE payment_date >= ? AND payment_date <= ?
              AND (status IS NULL OR status != 'void')
              AND {bp_clause}""",
        (start, end, account_id),
    ).fetchall():
        rows.append({
            "source": "bill_pay",
            "id": r["id"],
            "date": r["date"],
            "amount": float(r["amount"] or 0),
            "direction": "out",
            "ref": str(r["check_number"]) if r["check_number"] else (r["payment_ref"] or ""),
            "label": f"{r['vendor']} ({r['payment_method'] or 'check'})",
        })

    # Payroll — pay_date is on payroll_runs (parent), joined via payroll_run_id.
    try:
        for r in conn.execute(
            """SELECT pc.id, pc.employee_name, pc.check_number,
                      COALESCE(pr.pay_date, pc.pay_period_end) AS date,
                      pc.net_pay AS amount
               FROM payroll_checks pc
               LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
               WHERE COALESCE(pr.pay_date, pc.pay_period_end) >= ?
                 AND COALESCE(pr.pay_date, pc.pay_period_end) <= ?
                 AND (pc.voided IS NULL OR pc.voided = 0)
                 AND pc.bank_account_id = ?""",
            (start, end, account_id),
        ).fetchall():
            rows.append({
                "source": "payroll",
                "id": r["id"],
                "date": r["date"],
                "amount": float(r["amount"] or 0),
                "direction": "out",
                "ref": str(r["check_number"]) if r["check_number"] else "",
                "label": f"Payroll: {r['employee_name']}",
            })
    except Exception as e:
        logger.warning(f"payroll match query failed: {e}")

    # Deposits
    for r in conn.execute(
        """SELECT id, deposit_date AS date, amount, description
           FROM bank_deposits
           WHERE bank_account_id = ? AND deposit_date >= ? AND deposit_date <= ?""",
        (account_id, start, end),
    ).fetchall():
        rows.append({
            "source": "deposit",
            "id": r["id"],
            "date": r["date"],
            "amount": float(r["amount"] or 0),
            "direction": "in",
            "ref": "",
            "label": r["description"] or "Deposit",
        })

    # Manual entries (already in register)
    for r in conn.execute(
        """SELECT id, entry_date AS date, amount, payee, memo, ref_number,
                  COALESCE(statement_upload_id, 0) AS statement_upload_id
           FROM manual_bank_entries
           WHERE bank_account_id = ? AND entry_date >= ? AND entry_date <= ?""",
        (account_id, start, end),
    ).fetchall():
        amt = float(r["amount"] or 0)
        rows.append({
            "source": "manual",
            "id": r["id"],
            "date": r["date"],
            "amount": abs(amt),
            "direction": "in" if amt >= 0 else "out",
            "ref": r["ref_number"] or "",
            "label": r["payee"] or "Manual",
            "statement_upload_id": r["statement_upload_id"],
        })

    return rows


def _match_transactions(parsed_txs: list[dict], register_rows: list[dict]) -> list[dict]:
    """For each parsed statement row, decide whether the register already
    contains it.

    Strategy:
      - exact:  same direction + same amount + ref equality (e.g. check #) +
                date within 7 days → exact
      - likely: same direction + same amount + date within 4 days → likely
      - none:   no candidate

    Returns a list parallel to parsed_txs:
        [{ parsed_index: int, register_match: {...}|None, match_kind: str }, …]
    """
    results: list[dict] = []
    used_register_ids: set[tuple[str, int]] = set()  # don't re-use a register row

    def parse_d(s: str | None):
        try:
            return datetime.strptime(s or "", "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    for i, tx in enumerate(parsed_txs):
        debit = float(tx.get("debit") or 0)
        credit = float(tx.get("credit") or 0)
        amt = round(max(debit, credit), 2)
        direction = "out" if debit > 0 else "in"
        tx_ref = (tx.get("ref") or "").lstrip("0")
        tx_date = parse_d(tx.get("date"))

        best = None
        best_kind = "none"
        best_score = -1

        for reg in register_rows:
            key = (reg["source"], reg["id"])
            if key in used_register_ids:
                continue
            if reg["direction"] != direction:
                continue
            if abs(reg["amount"] - amt) > 0.005:
                continue

            reg_date = parse_d(reg.get("date"))
            day_diff = abs((tx_date - reg_date).days) if (tx_date and reg_date) else 99

            reg_ref = (reg.get("ref") or "").lstrip("0")
            ref_match = bool(tx_ref) and tx_ref == reg_ref

            kind = "none"
            score = -1
            if ref_match and day_diff <= 14:
                kind, score = "exact", 100 - day_diff
            elif day_diff <= 4:
                kind, score = "likely", 50 - day_diff
            elif day_diff <= 7:
                kind, score = "likely", 30 - day_diff

            if score > best_score:
                best, best_kind, best_score = reg, kind, score

        if best and best_kind != "none":
            used_register_ids.add((best["source"], best["id"]))
            results.append({
                "parsed_index": i,
                "register_match": best,
                "match_kind": best_kind,
            })
        else:
            results.append({
                "parsed_index": i,
                "register_match": None,
                "match_kind": "none",
            })

    return results


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _entry_type_from_tx(tx: dict) -> str:
    """Map parser tx_type into manual_bank_entries.entry_type values."""
    t = tx.get("tx_type") or ""
    debit = float(tx.get("debit") or 0)
    credit = float(tx.get("credit") or 0)
    if t == "fee":
        return "fee"
    if t in ("deposit", "ach_credit") or credit > 0:
        # Deposits go in as 'other' so they don't show up under the
        # 'Transfer' label in the register pill — entry_type is just a hint.
        return "other"
    if t == "check":
        return "other"
    if t in ("ach_debit", "other") and debit > 0:
        return "other"
    return "other"


def _mark_cleared(conn, source: str, row_id: int, when: str | None) -> int:
    table_by_source = {
        "bill_pay": "vendor_payments",
        "payroll": "payroll_checks",
        "deposit": "bank_deposits",
        "manual": "manual_bank_entries",
    }
    table = table_by_source.get(source)
    if not table:
        return 0
    when = when or datetime.now().strftime("%Y-%m-%d")
    cur = conn.execute(
        f"UPDATE {table} SET cleared = 1, cleared_date = COALESCE(cleared_date, ?) WHERE id = ?",
        (when, row_id),
    )
    return cur.rowcount or 0
