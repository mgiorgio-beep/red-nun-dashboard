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

    POST /api/bank-reconcile/import-all
        json: { account_id, dry_run: bool }
        Imports every parsed-but-unimported statement for the account in
        period order: continuity check against the previous statement
        (beginning == previous ending to the cent, dates contiguous), then
        match + auto-clear, import the unmatched lines, check OCR, invariant
        audit, tie-out. One transaction per period; stops at the first period
        that breaks continuity or fails. This is what job 062 (2026-09-22)
        did by hand outside Flask; it belongs here.

    POST /api/bank-reconcile/dedupe
        json: { account_id, start_date, end_date | all_periods: true, … }
        Merges statement-imported manual rows into the uncleared book row
        (vendor payment / manual payroll check) they duplicate; the book row
        takes the STATEMENT date as its cleared_date. all_periods runs every
        open statement period for the account, one transaction each.

    TIE-OUT IS BY CLEARED DATE. A period's bank side is the statement's
    beginning balance plus every register row the bank cleared inside the
    period (cleared_date), whatever the book date. Outstanding at period end
    is every row dated on or before period end that the bank had not cleared
    by then. See _reconciliation_state.

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
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
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

def resolve_import_gl(conn, tx: dict, signed: float,
                      acct_location: str | None, acct_last4: str | None,
                      quiet: bool = False):
    """Decide the GL account for one freshly imported statement row.

    Extracted from the import loop so the regression test can drive THIS code
    rather than a copy of it — the bug being guarded against was a write path
    that skipped a check the other paths had.

    Order:
      1. The deterministic transfer classifier. Inter-account transfers are the
         one case a substring rule reliably gets wrong (2757->5087 is rent,
         2757->5975 is the intercompany loan, four digits apart). A transfer it
         refuses to guess at stays UNCODED — it must not fall through to the
         rules, which would happily guess.
      2. The Venmo classifier — single-purpose channel, split by amount, which
         a text rule cannot do.
      3. The learned rules, SCOPED TO THIS ENTITY.
      4. Nothing.

    Whatever comes out is validated exactly as a human coding would be.
    `quiet` skips the per-row log lines (the Bank Transactions page asks for
    hundreds of rows at a time; the import path still logs).
    """
    log = logger if not quiet else logging.getLogger("routes.bank_reconcile_routes.quiet")
    if quiet:
        log.disabled = True
    from routes.register_routes import (
        _find_gl_account_for_description, classify_transfer, classify_venmo,
        classify_tip_settlement, resolve_gl_for_location,
    )
    desc = (tx.get("description") or "") + " " + (tx.get("memo") or "")
    gl_id = None

    name, reason = classify_transfer(desc, signed, acct_last4 or "")

    # FMT rent reversal (Mike, 2026-09-24): an inflow from 1239 is coded as a
    # reversal of Building Rent only when it pairs with rent actually sent —
    # a same-amount outflow to 1239 on or up to FMT_REVERSAL_WINDOW_DAYS before
    # this date. Anything else stays uncoded for Mike to look at.
    if name == "Building Rent" and (signed or 0) > 0 and "1239" in desc:
        from routes.register_routes import FMT_REVERSAL_WINDOW_DAYS
        paired = tx.get("date") and conn.execute(
            """SELECT 1 FROM manual_bank_entries m JOIN bank_accounts b ON b.id = m.bank_account_id
               WHERE b.account_last4 = ? AND ABS(m.amount + ?) < 0.005
                 AND UPPER(COALESCE(m.payee,'') || ' ' || COALESCE(m.memo,'')) LIKE '%TO X1239%'
                 AND m.entry_date BETWEEN date(?, ?) AND ?""",
            (acct_last4 or "", float(signed), tx["date"], f"-{FMT_REVERSAL_WINDOW_DAYS} day", tx["date"]),
        ).fetchone()
        if not paired:
            name, reason = None, (f"FMT inflow {signed:,.2f} on {tx.get('date')} pairs with no rent sent to "
                                  f"1239 in the prior {FMT_REVERSAL_WINDOW_DAYS} days — Mike to review")

    # Tip settlement channels (7shifts tip service, Kickfin). Runs before the
    # rules because a bare "7SHIFTS" rule cannot tell a tip reload from a
    # payroll draft from the SaaS bill — that is what put tip reloads on
    # Payroll Expenses. Returns a reason with no name when a row needs a human,
    # notably the first Kickfin row, which is the float retainer.
    if not name and not reason:
        name, tip_reason = classify_tip_settlement(
            conn, desc, signed, acct_location, tx.get("date"))
        if name:
            log.info("Tip channel classified: %s — %s", desc.strip()[:60], tip_reason)
        elif tip_reason:
            reason = tip_reason
            log.warning("Tip channel left for review: %s — %s",
                           desc.strip()[:70], tip_reason)

    if not name and not reason:
        name, venmo_reason = classify_venmo(desc, signed)
        if name:
            log.info("Venmo classified: %s — %s", desc.strip()[:60], venmo_reason)

    if name:
        # Prefer this entity's own account; a NULL-location (shared) one is the
        # fallback. Never the other entity's copy of the same name.
        hit = conn.execute(
            "SELECT id FROM gl_accounts WHERE name = ? AND (location = ? OR location IS NULL) "
            "AND active = 1 ORDER BY location IS NULL LIMIT 1",
            (name, acct_location),
        ).fetchone()
        if hit:
            gl_id = hit["id"]
        else:
            log.warning("No active %r account for %s — leaving row uncoded",
                           name, acct_location)
    elif reason:
        log.info("Transfer left for review: %s — %s", desc.strip()[:70], reason)

    if gl_id is None and not reason:
        # SCOPE THE LOOKUP TO THIS ENTITY. Omitting the location here is what
        # coded 114 Dennis rows to Chatham accounts: the unscoped query
        # considers rules from BOTH charts and returns whichever pattern is
        # longest, so a Chatham "LINENS" rule won a Dennis row. The rules were
        # correctly scoped all along; the caller discarded the scoping.
        gl_id = _find_gl_account_for_description(conn, desc, acct_location)

    # Final gate — the same validator a human coding passes through. An
    # automatic coder must never write what the PUT endpoint would refuse.
    return resolve_gl_for_location(conn, gl_id, acct_location,
                                   context="statement import")


def init_bank_reconcile_tables():
    """Create the upload-history table. Idempotent."""
    conn = get_connection()
    try:
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

        -- Trail for dedupe_register(), which is the only irreversible
        -- operation in the reconciliation path: it stamps the book row cleared
        -- and then DELETEs the statement row. Without this there is no record
        -- of what was removed, what it merged into, or why — and a wrong match
        -- is undetectable afterwards. deleted_entry_json holds the full
        -- pre-delete row so a merge can be undone by hand.
        CREATE TABLE IF NOT EXISTS register_merge_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merged_at TEXT DEFAULT CURRENT_TIMESTAMP,
            merged_by TEXT,
            bank_account_id INTEGER,
            -- surviving book row
            target_source TEXT,              -- vendor_payment | payroll_check
            target_id INTEGER,
            target_label TEXT,
            target_cleared_date TEXT,
            -- deleted statement row
            deleted_entry_id INTEGER,
            deleted_entry_date TEXT,
            deleted_entry_amount REAL,
            deleted_entry_json TEXT,         -- full row, for manual restore
            -- why they were matched
            match_amount REAL,
            match_date_diff_days INTEGER,
            match_tolerance_days INTEGER,
            match_rule TEXT,
            reversed_at TEXT                 -- set if a human undoes the merge
        );
        CREATE INDEX IF NOT EXISTS idx_rma_target
            ON register_merge_audit(target_source, target_id);
        CREATE INDEX IF NOT EXISTS idx_rma_deleted
            ON register_merge_audit(deleted_entry_id);

        -- ── Bank reconciliation sign-off ─────────────────────────────────
        -- One row per (account, statement period). The pass condition is NOT
        -- "delta == 0" — a period holding a legitimate outstanding check can
        -- never satisfy that. It is "the delta is fully itemized and someone
        -- accepted it", which is meaningless without a record of who accepted.
        CREATE TABLE IF NOT EXISTS bank_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_account_id INTEGER NOT NULL,
            statement_upload_id INTEGER,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',   -- open | reconciled
            -- the three anchors, frozen at close
            beginning_balance REAL,
            ending_balance REAL,
            bank_balance REAL,                     -- computed from cleared rows
            book_balance REAL,                     -- computed from all rows
            outstanding_net REAL,                  -- book - bank, itemized below
            delta REAL,                            -- bank_balance - ending_balance
            closed_by TEXT,
            closed_at TEXT,
            notes TEXT,
            FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_rec_period
            ON bank_reconciliations(bank_account_id, period_start, period_end);

        -- Snapshot of what was outstanding at sign-off. Deliberately a COPY,
        -- not a view: it is the record that these specific items, at these
        -- amounts, were known and accepted when the period closed, and it must
        -- survive later edits to the underlying row.
        CREATE TABLE IF NOT EXISTS bank_reconciliation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_id INTEGER NOT NULL,
            source TEXT NOT NULL,       -- manual | bill_pay | payroll | deposit
            source_id INTEGER NOT NULL,
            entry_date TEXT,
            payee TEXT,
            memo TEXT,
            amount REAL,                -- signed: negative = outflow
            age_days INTEGER,           -- period_end - entry_date at close
            -- Set when the same item was already outstanding in the previous
            -- period's snapshot. A long carried_from chain is the stale-
            -- outstanding signal: a check nobody ever cashed, i.e. a void
            -- candidate worth surfacing.
            carried_from_item_id INTEGER,
            carry_count INTEGER DEFAULT 0,
            FOREIGN KEY (reconciliation_id) REFERENCES bank_reconciliations(id),
            FOREIGN KEY (carried_from_item_id) REFERENCES bank_reconciliation_items(id)
        );
        CREATE INDEX IF NOT EXISTS idx_bri_rec
            ON bank_reconciliation_items(reconciliation_id);
        CREATE INDEX IF NOT EXISTS idx_bri_source
            ON bank_reconciliation_items(source, source_id);
        """)

        # One statement per (account, period). The table was append-only, so a
        # re-upload created a second row for the same period and re-imported every
        # line. Created separately from the script above because it can fail if
        # duplicates already exist — in that case leave the table alone and say so
        # rather than half-applying a constraint. NOTE: because this tolerates
        # failure, the index cannot be relied on to exist — upload_statement
        # enforces one-row-per-period itself with an explicit select-then-update.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_bsu_account_period "
                "ON bank_statement_uploads(bank_account_id, period_start, period_end)"
            )
        except Exception as e:
            logger.warning(
                "Could not create uq_bsu_account_period — duplicate (account, period) "
                "rows already exist and must be resolved by hand: %s", e)

        # Tag manual_bank_entries with the statement upload that created them, so
        # we can avoid re-importing on a second upload of the same period.
        try:
            conn.execute("ALTER TABLE manual_bank_entries ADD COLUMN statement_upload_id INTEGER")
        except Exception:
            pass  # already exists

        # Backfill reconciliation_id on cleared rows inside every already-closed
        # period. Runs once. Idempotent: guarded on reconciliation_id IS NULL so
        # a re-run doesn't touch rows that already carry a stamp. Without this,
        # the four periods that were signed off before the R column existed
        # would render as C forever.
        for rec in conn.execute(
            "SELECT id, bank_account_id, period_start, period_end "
            "FROM bank_reconciliations WHERE status = 'reconciled'"
        ).fetchall():
            _backfill_reconciled_stamps(
                conn, rec["bank_account_id"],
                rec["period_start"], rec["period_end"], rec["id"],
            )

        conn.commit()
    finally:
        conn.close()


def _backfill_reconciled_stamps(conn, account_id, period_start, period_end, rec_id):
    """One-time stamp of reconciliation_id on rows cleared inside a
    pre-existing reconciliation. Same filter shape as _stamp_reconciled_rows,
    plus an `AND reconciliation_id IS NULL` guard so it is safe to re-run on
    init.
    """
    _stamp_reconciled_rows(conn, account_id, period_start, period_end, rec_id,
                           only_unstamped=True)


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
    try:
        acct = conn.execute(
            "SELECT id, name, account_last4 FROM bank_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not acct:
            return jsonify({"error": "Account not found"}), 404

        # Save the file on disk
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = (file.filename or "statement.pdf").replace("/", "_").replace("\\", "_")
        file_path = STATEMENT_DIR / f"acct{account_id}_{ts}_{safe_name}"
        try:
            file_path.write_bytes(pdf_bytes)
        except Exception as e:
            logger.exception("Failed to save statement PDF")
            return jsonify({"error": f"Could not write file: {e}"}), 500

        # Parse
        try:
            parsed = parse_bank_statement_pdf(pdf_bytes)
        except Exception as e:
            logger.exception("Statement parse failed")
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

        # Persist the upload record. One row per (account, period): re-uploading
        # a period that is already stored REPLACES its parse. That is a required
        # workflow, not a convenience — parse_bank_statement_pdf runs only here
        # and its output is frozen in parsed_json, so a parser fix can reach an
        # already-uploaded period no other way. Done as an explicit
        # select-then-update rather than ON CONFLICT because uq_bsu_account_period
        # is created in a try that tolerates failure (see init) and cannot be
        # relied on to exist.
        uploaded_by = session.get("username") or session.get("email") or "unknown"
        existing = conn.execute(
            """SELECT id, imported_count, file_path FROM bank_statement_uploads
               WHERE bank_account_id = ? AND period_start = ? AND period_end = ?
               ORDER BY id DESC LIMIT 1""",
            (account_id, parsed.get("period_start"), parsed.get("period_end")),
        ).fetchone()

        if existing and (existing["imported_count"] or 0) > 0:
            # NOT replaceable: /api/bank-reconcile/import addresses rows by
            # POSITIONAL index into parsed_json, so swapping the parse under an
            # already-imported period silently invalidates what those imports
            # point at.
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({
                "error": (
                    f"{existing['imported_count']} rows were already imported from "
                    f"this period (upload #{existing['id']}), so its stored parse "
                    f"cannot be replaced — the imported rows reference positions in "
                    f"it. Review that upload first; delete it if the import must be "
                    f"redone."
                ),
                "upload_id": existing["id"],
            }), 409

        superseded_pdf = existing["file_path"] if existing else None
        row_values = (
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
        )
        if existing:
            conn.execute(
                """UPDATE bank_statement_uploads
                   SET filename = ?, file_path = ?, uploaded_by = ?,
                       uploaded_at = CURRENT_TIMESTAMP,
                       period_start = ?, period_end = ?,
                       beginning_balance = ?, ending_balance = ?,
                       total_debits = ?, total_credits = ?, transaction_count = ?,
                       parsed_json = ?, warnings_json = ?
                   WHERE id = ?""",
                row_values + (existing["id"],),
            )
            upload_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO bank_statement_uploads
                   (filename, file_path, uploaded_by,
                    period_start, period_end, beginning_balance, ending_balance,
                    total_debits, total_credits, transaction_count,
                    parsed_json, warnings_json, bank_account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row_values + (account_id,),
            )
            upload_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # The replaced row's old PDF is superseded — remove it now that the new
    # row is committed (same courtesy delete_upload does).
    if superseded_pdf and superseded_pdf != str(file_path):
        try:
            if os.path.exists(superseded_pdf):
                os.remove(superseded_pdf)
        except Exception as e:
            logger.warning(f"Could not remove superseded statement file {superseded_pdf}: {e}")

    return jsonify({
        "upload_id": upload_id,
        "replaced": bool(existing),
        "account": dict(acct),
        "parsed": parsed,
        "matches": matches,
    })


# ─── IMPORT SELECTED ROWS ────────────────────────────────────────────────────

def _import_upload_rows(conn, upload, indexes=None, also_clear=False,
                        created_by="statement-import"):
    """Import parsed statement lines from one upload into manual_bank_entries.

    THE ONE IMPORT LOOP. /import (the review screen) and /import-all both
    run this; job 062 re-implemented it in a shell script because the
    endpoint needed a login session, and that copy is what stamped ~410
    cleared_dates with the book date instead of the statement date (job 064
    repaired them). Nothing writes statement rows except this function.

    indexes     0-based positions into parsed.transactions to insert. None
                means "every line the matcher did not pair with an existing
                register row" — the import-all rule.
    also_clear  stamp the register rows the matcher paired as cleared, on the
                STATEMENT line's date (the day the bank cleared them).

    Does not commit. Returns counts; the caller owns the transaction.
    """
    upload_id = upload["id"]
    parsed = json.loads(upload["parsed_json"]) if upload["parsed_json"] else {}
    transactions = parsed.get("transactions", [])
    account_id = upload["bank_account_id"]

    # Re-run match so we know which rows are dupes (in case register changed
    # between upload and import).
    register_rows = _load_register_rows_for_period(conn, account_id, parsed)
    matches = _match_transactions(transactions, register_rows)
    matched = {m["parsed_index"]: m for m in matches
               if m.get("register_match") and m.get("match_kind") != "none"}
    kinds = {"exact": 0, "likely": 0, "none": 0}
    for m in matches:
        kinds[m.get("match_kind") or "none"] = kinds.get(m.get("match_kind") or "none", 0) + 1

    if indexes is None:
        indexes = [i for i in range(len(transactions)) if i not in matched]

    # Needed by the transfer classifier: which account is "this" one, and which
    # entity's chart of accounts to resolve names against.
    _acct = conn.execute(
        "SELECT account_last4, location FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    acct_last4 = (_acct["account_last4"] if _acct else "") or ""
    acct_location = _acct["location"] if _acct else None

    inserted = uncoded = skipped_zero = 0
    for idx in indexes:
        if not isinstance(idx, int) or idx < 0 or idx >= len(transactions):
            continue
        tx = transactions[idx]

        # Signed amount: positive = inflow, negative = outflow
        debit = float(tx.get("debit") or 0)
        credit = float(tx.get("credit") or 0)
        signed = credit - debit
        if signed == 0:
            skipped_zero += 1
            continue

        entry_type = _entry_type_from_tx(tx)
        memo_parts = []
        if tx.get("memo"):
            memo_parts.append(tx["memo"])
        memo_parts.append(f"[stmt #{upload_id}]")
        memo = " ".join(memo_parts).strip()

        # Pre-fill the GL account so freshly imported rows aren't all blank.
        gl_id = resolve_import_gl(conn, tx, signed, acct_location, acct_last4)
        if not gl_id:
            uncoded += 1

        # Any coding applied here is machine-derived, so it is suggested, never
        # confirmed — nothing may learn a rule from it (see GL_PROVENANCE).
        # A statement row is cleared on its own date by definition.
        cur = conn.execute(
            """INSERT INTO manual_bank_entries
               (bank_account_id, entry_date, entry_type, payee, memo,
                ref_number, amount, cleared, cleared_date, created_by,
                statement_upload_id, gl_account_id, gl_source, gl_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
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
                ("rule" if gl_id else None),
                ("suggested" if gl_id else None),
            ),
        )
        if cur.rowcount:
            inserted += 1

    cleared_total = 0
    if also_clear:
        for i, m in matched.items():
            reg = m["register_match"]
            # Stamp the day the BANK cleared it (the statement line), not the
            # day the check was cut — the tie-out counts by clearing date.
            line = transactions[i] if 0 <= i < len(transactions) else {}
            cleared_total += _mark_cleared(conn, reg["source"], reg["id"],
                                           line.get("date") or reg.get("date"))

    conn.execute(
        "UPDATE bank_statement_uploads SET imported_count = imported_count + ? WHERE id = ?",
        (inserted, upload_id),
    )
    return {
        "upload_id": upload_id,
        "lines": len(transactions),
        "inserted": inserted,
        "cleared": cleared_total,
        "uncoded": uncoded,
        "skipped_zero": skipped_zero,
        "match_kinds": kinds,
    }


def _ocr_checks(conn, upload_id):
    """Check images are ALWAYS extracted and OCR'd after an import.

    Extraction used to be a separate job, so April imported 15 check rows
    reading "Check 9698" with no payee and no image. OCR is local (tesseract),
    so this is not metered API spend. It never blocks the import: the rows
    are real transactions either way."""
    try:
        from integrations.bank_statements.check_ocr import enrich_upload
        checks = enrich_upload(conn, upload_id)
        logger.info("Check OCR for upload %s: %s", upload_id,
                    checks.get("banner") or checks.get("error"))
        return checks
    except Exception as e:
        logger.exception("Check extraction failed for upload %s", upload_id)
        return {"ok": False, "error": str(e)}


def _post_import_audit(conn, acct_location, upload_id):
    """Import is the moment rows get coded automatically, so it is the moment
    to check the codings. The guard that would have caught the 114
    cross-entity codings already existed — as a pytest assertion nobody ran
    between the April import and someone spotting the wrong accounts on
    screen. It runs here now.

    The audit NEVER blocks or rolls back the import: the rows are real bank
    transactions and belong in the register either way. It reports."""
    try:
        from routes.register_routes import audit_register_invariants
        audit = audit_register_invariants(conn, location=acct_location)
        if not audit["ok"]:
            logger.error(
                "POST-IMPORT AUDIT FAILED after upload %s (%s): %s",
                upload_id, acct_location,
                "; ".join(f"{c['name']}={c['count']}"
                          for c in audit["checks"] if not c["ok"]),
            )
        else:
            logger.info("Post-import audit clean for upload %s (%s)",
                        upload_id, acct_location)
        return audit
    except Exception as e:
        logger.exception("Post-import audit could not run")
        return {"ok": None, "error": str(e), "checks": []}


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
    try:
        upload = conn.execute(
            "SELECT * FROM bank_statement_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if not upload:
            return jsonify({"error": "Upload not found"}), 404
        _acct = conn.execute(
            "SELECT location FROM bank_accounts WHERE id = ?", (upload["bank_account_id"],)
        ).fetchone()
        acct_location = _acct["location"] if _acct else None

        created_by = session.get("username") or session.get("email") or "statement-import"
        try:
            res = _import_upload_rows(conn, upload, indexes, also_clear, created_by)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        checks = _ocr_checks(conn, upload_id)
        audit = _post_import_audit(conn, acct_location, upload_id)

        return jsonify({
            "status": "ok",
            "inserted": res["inserted"],
            "cleared": res["cleared"],
            "uncoded": res["uncoded"],
            "audit": audit,
            "checks": checks,
        })
    finally:
        conn.close()


@bank_reconcile_bp.route("/api/bank-reconcile/import-all", methods=["POST"])
@login_required
def import_all_pending():
    """Import every parsed-but-unimported statement for one account, in
    period order, with the continuity check job 062 applied by hand.

    Body: { "account_id": int, "dry_run": bool }

    Per period, in order of period_start:
      1. continuity — beginning_balance must equal the previous statement's
         ending_balance to the cent and the periods must be contiguous
         (previous end + 1 day == start). A break means a statement is
         missing or mis-parsed; importing past it would stamp the wrong
         period's rows, so the run STOPS there and says so.
      2. already imported (imported_count > 0) — skipped, never re-imported.
      3. match against the register, insert the unmatched lines, clear the
         matched book rows on the statement line's date — one transaction,
         rolled back whole on any error (which also stops the run).
      4. check OCR, invariant audit, tie-out — reported per period.

    Returns { status: ok|stopped, periods: [ {period, action, …} ] }.
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    dry_run = bool(data.get("dry_run"))
    if not isinstance(account_id, int):
        return jsonify({"error": "account_id (int) is required"}), 400

    conn = get_connection()
    try:
        acct = conn.execute(
            "SELECT * FROM bank_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not acct:
            return jsonify({"error": f"bank_account {account_id} not found"}), 404
        created_by = session.get("username") or session.get("email") or "statement-import"

        uploads = conn.execute(
            "SELECT * FROM bank_statement_uploads WHERE bank_account_id = ? "
            "AND period_start IS NOT NULL ORDER BY period_start", (account_id,)
        ).fetchall()

        results = []
        stopped_at = None
        prev = None
        for up in uploads:
            tag = f"{up['period_start']}..{up['period_end']}"
            entry = {"upload_id": up["id"], "period": tag,
                     "lines": up["transaction_count"]}

            if prev is not None:
                reasons = []
                if _c(up["beginning_balance"]) != _c(prev["ending_balance"]):
                    reasons.append(
                        f"beginning {up['beginning_balance']} != previous ending "
                        f"{prev['ending_balance']} ({prev['period_end']})")
                try:
                    if (date.fromisoformat(prev["period_end"]) + timedelta(days=1)
                            != date.fromisoformat(up["period_start"])):
                        reasons.append(
                            f"gap or overlap between {prev['period_end']} and "
                            f"{up['period_start']}")
                except (TypeError, ValueError):
                    reasons.append("unparseable period dates")
                if reasons:
                    entry.update({"action": "stopped",
                                  "reason": "continuity: " + "; ".join(reasons)})
                    results.append(entry)
                    stopped_at = tag
                    break
            prev = up

            if (up["imported_count"] or 0) > 0:
                entry.update({"action": "already_imported",
                              "imported_count": up["imported_count"]})
                results.append(entry)
                continue

            if dry_run:
                entry.update({"action": "would_import"})
                results.append(entry)
                continue

            try:
                res = _import_upload_rows(conn, up, None, True, created_by)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.exception("import-all failed on upload %s", up["id"])
                entry.update({"action": "failed", "error": str(e)})
                results.append(entry)
                stopped_at = tag
                break

            checks = _ocr_checks(conn, up["id"])
            audit = _post_import_audit(conn, acct["location"], up["id"])
            try:
                # Re-read: imported_count changed.
                up2 = conn.execute("SELECT * FROM bank_statement_uploads WHERE id = ?",
                                   (up["id"],)).fetchone()
                state = _reconciliation_state(conn, up2)
                tie = {"ties": state["ties"], "delta": state["delta"],
                       "outstanding_count": len(state["outstanding_items"])}
            except Exception as e:
                tie = {"ties": None, "error": str(e)}
            entry.update({
                "action": "imported",
                "inserted": res["inserted"],
                "cleared": res["cleared"],
                "uncoded": res["uncoded"],
                "match_kinds": res["match_kinds"],
                "checks": (checks or {}).get("banner") or (checks or {}).get("error"),
                "audit_ok": (audit or {}).get("ok"),
                "audit_failures": [c["name"] for c in (audit or {}).get("checks", [])
                                   if not c.get("ok")],
                **tie,
            })
            results.append(entry)

        return jsonify({
            "status": "stopped" if stopped_at else "ok",
            "account_id": account_id,
            "dry_run": dry_run,
            "stopped_at": stopped_at,
            "periods": results,
            "imported_periods": sum(1 for r in results if r["action"] == "imported"),
        })
    finally:
        conn.close()


@bank_reconcile_bp.route("/api/bank-reconcile/checks/<int:upload_id>", methods=["POST"])
@login_required
def rerun_check_ocr(upload_id: int):
    """Re-run check extraction and OCR over one upload.

    Idempotent, so this is the safe way to pick up an OCR improvement over a
    statement already imported. `force=true` re-reads images whose payee is
    already stored; without it, stored payees are kept and only missing ones
    are attempted.
    """
    force = bool((request.get_json(silent=True) or {}).get("force"))
    conn = get_connection()
    try:
        from integrations.bank_statements.check_ocr import enrich_upload
        result = enrich_upload(conn, upload_id, force=force)
        code = 200 if result.get("ok") else 400
        return jsonify(result), code
    except Exception as e:
        logger.exception("Check OCR re-run failed for upload %s", upload_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


# ─── HISTORY ─────────────────────────────────────────────────────────────────

@bank_reconcile_bp.route("/api/bank-reconcile/uploads", methods=["GET"])
@login_required
def list_uploads():
    account_id = request.args.get("account_id")
    conn = get_connection()
    try:
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
    finally:
        conn.close()
    return jsonify({"uploads": [dict(r) for r in rows]})


@bank_reconcile_bp.route("/api/bank-reconcile/uploads/<int:upload_id>", methods=["GET"])
@login_required
def get_upload(upload_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bank_statement_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Upload not found"}), 404

        parsed = json.loads(row["parsed_json"]) if row["parsed_json"] else {}
        register_rows = _load_register_rows_for_period(conn, row["bank_account_id"], parsed)
        matches = _match_transactions(parsed.get("transactions", []), register_rows)
    finally:
        conn.close()

    out = dict(row)
    out["parsed"] = parsed
    out["matches"] = matches
    out.pop("parsed_json", None)
    return jsonify(out)


@bank_reconcile_bp.route("/api/bank-reconcile/uploads/<int:upload_id>/raw-text", methods=["GET"])
@login_required
def get_upload_raw_text(upload_id):
    """Diagnostic: return the raw text pdfplumber extracted from this upload's
    PDF, so we can tune the parser regex against the real statement format."""
    from integrations.bank_statements.processor import _extract_text

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT file_path FROM bank_statement_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
    finally:
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
    try:
        row = conn.execute(
            "SELECT file_path FROM bank_statement_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Upload not found"}), 404

        # NOTE: this only deletes the upload metadata. manual_bank_entries created
        # via this upload remain — to clean those up too, also DELETE
        # manual_bank_entries WHERE statement_upload_id = ?.
        conn.execute("DELETE FROM bank_statement_uploads WHERE id = ?", (upload_id,))
        conn.commit()
    finally:
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
    period_start, period_end = start, end   # the statement's own window

    def _cleared_elsewhere(cleared, cleared_date):
        """A book row the bank ALREADY cleared on a date outside this
        statement's period belongs to another statement. It must not be
        paired with a line here — that is how a February PFG payment came to
        carry an August cleared_date (job 063). Rows cleared inside this
        period stay matchable so a re-preview of an imported period still
        shows its lines as matched."""
        if not cleared or not cleared_date:
            return False
        return not (period_start <= cleared_date <= period_end)

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
                  check_number, payment_method, payment_ref, memo, status,
                  cleared, cleared_date
            FROM vendor_payments
            WHERE payment_date >= ? AND payment_date <= ?
              AND (status IS NULL OR status NOT IN ('void', 'failed'))
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
            "cleared": int(r["cleared"] or 0),
            "cleared_date": r["cleared_date"],
            "cleared_elsewhere": _cleared_elsewhere(r["cleared"], r["cleared_date"]),
        })

    # Payroll — pay_date is on payroll_runs (parent), joined via payroll_run_id.
    # Direct Deposit checks excluded: they're rolled into the lump-sum 7shifts
    # ACH (PCR 7shifts on the bank statement) and never appear individually,
    # so the matcher would never find a match for them. Manual paper checks DO
    # appear individually on the statement and are matched here.
    try:
        for r in conn.execute(
            """SELECT pc.id, pc.employee_name, pc.check_number,
                      COALESCE(pr.pay_date, pc.pay_period_end) AS date,
                      pc.net_pay AS amount, pc.cleared, pc.cleared_date
               FROM payroll_checks pc
               LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
               WHERE COALESCE(pr.pay_date, pc.pay_period_end) >= ?
                 AND COALESCE(pr.pay_date, pc.pay_period_end) <= ?
                 AND (pc.voided IS NULL OR pc.voided = 0)
                 AND (pc.payment_method IS NULL OR pc.payment_method != 'Direct Deposit')
                 AND COALESCE(pc.net_pay, 0) <> 0
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
                "cleared": int(r["cleared"] or 0),
                "cleared_date": r["cleared_date"],
                "cleared_elsewhere": _cleared_elsewhere(r["cleared"], r["cleared_date"]),
            })
    except Exception as e:
        logger.warning(f"payroll match query failed: {e}")

    # Deposits
    for r in conn.execute(
        """SELECT id, deposit_date AS date, amount, description, cleared, cleared_date
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
            "cleared": int(r["cleared"] or 0),
            "cleared_date": r["cleared_date"],
            "cleared_elsewhere": _cleared_elsewhere(r["cleared"], r["cleared_date"]),
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


# Noise tokens in a Cape Cod Five description that carry no payee information.
# "SALE FORE & AFT INC." -> {FORE, AFT} once these are stripped.
_DESC_NOISE = {
    "SALE", "CHECK", "CHECKS", "DBT", "CRD", "CARD", "DEBIT", "CREDIT",
    "MEBILLPAY", "BILLPAY", "BILL", "PAY", "PAYMENT", "AR", "ACH", "EFT",
    "WIRE", "XFER", "TRANSFER", "WITHDRAWAL", "DEPOSIT", "PURCHASE", "POS",
    "RECURRING", "ONLINE", "MOBILE", "PMT", "INV", "REF", "ID", "CO", "INC",
    "LLC", "LTD", "CORP", "CORPORATION", "COMPANY", "THE", "OF", "AND",
    "GROUP", "SYSTEMS", "SERVICE", "SERVICES", "PAYROLL", "VENDOR", "TI",
}


_RE_NON_ALPHA = re.compile(r"[^A-Za-z]+")


@lru_cache(maxsize=8192)
def _payee_tokens_cached(text: str) -> frozenset:
    words = _RE_NON_ALPHA.split(text.upper())
    return frozenset(
        w[:6] for w in words           # stem, so PERFORMANCEBOS ~ PERFORMANCE
        if len(w) >= 3 and w not in _DESC_NOISE
    )


def _payee_tokens(text: str) -> frozenset:
    """Informative uppercase word-stems from a payee or statement description.

    Memoised: _match_transactions compares every statement line against every
    register row, so a 266-line statement against 182 register rows is ~48k
    calls per period and the same handful of distinct strings recur constantly.
    Tokenising uncached made the regression suite exceed 300s.
    """
    if not text:
        return frozenset()
    return _payee_tokens_cached(str(text))


# Vendors the bank names differently from the way we store them. Without these
# the token comparison sees zero overlap and would VETO a correct pairing --
# e.g. we store "PFG" while Cape Cod Five prints "AR PAYMENT PERFORMANCEBOS"
# (PFG = Performance Food Group). A false veto is worse than a missed veto: the
# statement line then imports as a new manual entry and double-counts a bill we
# already have. Stems are truncated to 6 chars to match _payee_tokens.
_PAYEE_ALIASES = [
    {"PFG", "PERFOR"},                    # Performance Foodservice
    {"USFOOD", "FOODS", "FOODSE"},        # US Foods / US Foodservice
    {"SOUTHE", "GLAZER", "SGWS"},         # Southern Glazer's
    {"MARTIG", "ARTIGN"},                 # Martignetti (OCR drops the M)
    {"KNIFE", "LKNIFE", "VTINFO"},        # L. Knife & Son
    {"CRAFT", "TERMSY"},                  # Craft Collective via TermSync
    {"COLONI", "VTINFO"},                 # Colonial Wholesale
    {"SEVENS", "SHIFTS", "7SHIFT"},       # 7shifts payroll ACH
    {"TOAST", "TOASTT"},                  # Toast settlement
    {"DAVO", "DAVOSA"},                   # DAVO sales tax
]


def _alias_linked(a: set[str], b: set[str]) -> bool:
    """True if any alias group contains a stem from each side."""
    for grp in _PAYEE_ALIASES:
        if (a & grp) and (b & grp):
            return True
    return False


def _payee_agreement(tx_desc: str, reg_label: str) -> str:
    """'match' | 'conflict' | 'unknown' for a statement description vs a
    register row's label.

    The matcher used to ignore payee text entirely — direction + amount ±0.005
    + date proximity only, ties broken by iteration order. On real data that
    produced provably wrong pairings: statement line
    'SALE FORE & AFT INC.' $260.00 was matched to a $260.00 'The Caron Group'
    bill because the amounts were equal and the dates were close. Chatham
    June alone had 14 matches choosing among 5–7 identical-amount candidates.
    Now that a completed reconciliation stamps rows R, a wrong match is sticky.

    'conflict' is the valuable verdict: both sides name something identifiable
    and they share nothing, so this pairing is refused outright rather than
    merely scored lower.
    """
    a = _payee_tokens(tx_desc)
    b = _payee_tokens(reg_label)
    if not a or not b:
        return "unknown"
    if a & b:
        return "match"
    # Catch the substring case ("GLANOLA" inside "GLANOLANORTHAM") that
    # tokenising on word boundaries misses.
    ja, jb = "".join(sorted(a)), "".join(sorted(b))
    if any(t in jb for t in a) or any(t in ja for t in b):
        return "match"
    if _alias_linked(a, b):
        return "match"
    # Both sides name something identifiable and share nothing, directly or by
    # alias. Veto.
    #
    # Chose to veto rather than merely down-rank, because the two failure modes
    # are not symmetric. A missed veto marks the WRONG bill cleared and drops
    # the real statement line from the import, so a bill the vendor never
    # cashed reads as paid — that corrupts AP and is what the accountant sees.
    # A false veto imports the line as a new manual entry, which inflates
    # outstanding, shows up immediately, and can be merged back with
    # register_merge_audit recording it. Unmatched is the safe direction.
    #
    # If a legitimate vendor starts getting vetoed, add it to _PAYEE_ALIASES
    # rather than loosening this.
    return "conflict"


def _match_transactions(parsed_txs: list[dict], register_rows: list[dict]) -> list[dict]:
    """For each parsed statement row, decide whether the register already
    contains it.

    Strategy (direction + amount to the penny are always required):
      - a register row the bank already cleared OUTSIDE this statement's
        period is never a candidate (cleared_elsewhere) — see
        _load_register_rows_for_period
      - exact:  ref equality (check #) and date within 14 days
      - exact:  payee agreement and date within 7 days
      - likely: payee agreement and date within 14 days
      - likely: no payee signal either way, date within 4 or 7 days
      - none:   no candidate, or payee text CONFLICTS

    A payee conflict vetoes the pairing outright — see _payee_agreement.

    Returns a list parallel to parsed_txs:
        [{ parsed_index: int, register_match: {...}|None, match_kind: str,
           payee_check: str }, …]
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
        best_payee = None

        for reg in register_rows:
            key = (reg["source"], reg["id"])
            if key in used_register_ids:
                continue
            if reg.get("cleared_elsewhere"):
                continue
            if reg["direction"] != direction:
                continue
            if abs(reg["amount"] - amt) > 0.005:
                continue

            reg_date = parse_d(reg.get("date"))
            day_diff = abs((tx_date - reg_date).days) if (tx_date and reg_date) else 99

            reg_ref = (reg.get("ref") or "").lstrip("0")
            ref_match = bool(tx_ref) and tx_ref == reg_ref

            payee = _payee_agreement(tx.get("description") or "",
                                     reg.get("label") or "")
            # A named-payee disagreement vetoes the pairing. Equal amounts and
            # nearby dates are not evidence that 'SALE FORE & AFT INC.' is
            # 'The Caron Group'. Ref equality still wins, since a check number
            # is harder evidence than a description string.
            if payee == "conflict" and not ref_match:
                continue

            kind = "none"
            score = -1
            if ref_match and day_diff <= 14:
                kind, score = "exact", 200 - day_diff
            elif payee == "match" and day_diff <= 7:
                kind, score = "exact", 150 - day_diff
            elif payee == "match" and day_diff <= 14:
                kind, score = "likely", 100 - day_diff
            elif payee == "unknown" and day_diff <= 4:
                kind, score = "likely", 50 - day_diff
            elif payee == "unknown" and day_diff <= 7:
                kind, score = "likely", 30 - day_diff

            if score > best_score:
                best, best_kind, best_score = reg, kind, score
                best_payee = payee

        if best and best_kind != "none":
            used_register_ids.add((best["source"], best["id"]))
            results.append({
                "parsed_index": i,
                "register_match": best,
                "match_kind": best_kind,
                "payee_check": best_payee,
            })
        else:
            results.append({
                "parsed_index": i,
                "register_match": None,
                "match_kind": "none",
                "payee_check": None,
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


def _mark_cleared(conn, source: str, row_id: int, when: str | None,
                  force: bool = False) -> int:
    """Stamp one register row cleared on `when` — the date the BANK cleared
    it, i.e. the statement line's date. Callers must not pass the book date.

    FIRST CLEARING WINS. A row that already carries a cleared_date keeps it
    (COALESCE): that date came from the statement line that first cleared
    the row, possibly in a period since signed off, and a later match must
    not drag the row into another period. The matcher already refuses to
    pair a row cleared outside the current period (cleared_elsewhere), so in
    practice the only rows that reach here with a date are re-runs on the
    same period, where the existing date is the right one.

    `force=True` overwrites — for a deliberate repair (job 064's kind), never
    for an import.
    """
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
    if force:
        cur = conn.execute(
            f"UPDATE {table} SET cleared = 1, cleared_date = ? WHERE id = ?",
            (when, row_id),
        )
    else:
        cur = conn.execute(
            f"UPDATE {table} SET cleared = 1, cleared_date = COALESCE(cleared_date, ?) WHERE id = ?",
            (when, row_id),
        )
    return cur.rowcount or 0


# ─── DEDUPE TOOL ─────────────────────────────────────────────────────────────
#
# Retroactive cleanup for the case where a bank statement was imported and
# created manual_bank_entries rows that duplicate existing dashboard-side
# vendor_payments / payroll_checks rows (because the import-time matcher
# missed them, or the user imported all parsed rows instead of unmatched-only).
#
# For each manual_bank_entries outflow in the date range, we look for a
# matching vendor_payment or Manual payroll_check by amount + date proximity.
# The "winner" is the dashboard row (it has vendor info, GL coding, link to
# the invoice); the manual_bank_entry duplicate gets deleted on commit.
#
# Inflow duplicates (deposits) are not handled here because the typical
# dashboard side (bank_deposits) is populated from QBO sync — if we later
# add deposit dedup the same pattern applies.

DEDUPE_MATCH_RULE = ("exact_amount;date_within_tolerance;closest_date_wins;"
                     "payroll_preferred_on_tie;ambiguous_skipped;"
                     "book_row_uncleared_only;cleared_date=statement_date")

# Payroll mode (Mike, 2026-09-24). Employees cash paper checks weeks late, so
# the symmetric few-day window misses them; widening it naively lets
# closest-date-wins pick between two same-amount paychecks. Payroll mode:
# the check clears on or after its pay date (PAYROLL_EARLY_DAYS of slack for a
# run dated after its checks were handed out) and within PAYROLL_LATE_DAYS;
# where the check image's OCR names a payee it must name the employee; and
# more than one candidate is ambiguous, never a tie-break. check_number is the
# dashboard's own sequence, not the bank's (TestCheckNumberIsNotAKey), so it
# is reported, never matched on.
PAYROLL_LATE_DAYS = 60
PAYROLL_EARLY_DAYS = 3
PAYROLL_MATCH_RULE = ("payroll_mode;exact_amount;cleared_on_or_after_pay_date_within_60d;"
                      "ocr_payee_names_employee_when_present;unique_candidate_only;"
                      "book_row_uncleared_only;cleared_date=statement_date")
_RE_CHK_PAYEE = re.compile(r"CHK:\s*([^|\[]+)")


def _ocr_names_employee(memo, employee_name):
    """'match' / 'mismatch' / None (no readable OCR payee on the line).

    Unreadable OCR ('teen and .. 6 6 686') is no name at all: the learner's
    guard decides readability. A readable name must carry every token of the
    employee's name (fuzzy per token, so 'NASCIMENT0' reads as NASCIMENTO)."""
    from difflib import SequenceMatcher
    from routes.register_routes import _readable_check_payee  # lazy: avoids a cycle
    m = _RE_CHK_PAYEE.search(memo or "")
    if not m or not _readable_check_payee(m.group(1).strip()):
        return None
    ocr = re.findall(r"[A-Z]{2,}", m.group(1).upper())
    if not ocr:
        return None
    want = re.findall(r"[A-Z]{2,}", (employee_name or "").upper())
    if not want:
        return None

    def seen(tok):
        return any(SequenceMatcher(None, tok, o).ratio() >= 0.8 for o in ocr)
    return "match" if all(seen(t) for t in want) else "mismatch"


def _dedupe_period(conn, bank, start, end, tol, match_bp, match_pr, commit, who,
                   payroll_mode=False):
    """The dedupe rule for one account and one date window. See
    dedupe_register for the contract; this does the work and returns
    {candidates, summary, merged_count, deleted_count}. Does not commit.

    payroll_mode: payroll checks only, under PAYROLL_MATCH_RULE (tol is
    ignored for them)."""
    if payroll_mode:
        match_bp, match_pr = False, True
    account_id = bank["id"]
    is_default = bank["account_last4"] == "5975"

    # 1. Statement-imported outflows in range. Only statement rows are
    #    candidates: a hand-entered manual row is not a duplicate of anything.
    me_rows = conn.execute(
        """SELECT id, entry_date, entry_type, payee, memo, amount, ref_number,
                  cleared, statement_upload_id
           FROM manual_bank_entries
           WHERE bank_account_id = ?
             AND entry_date >= ? AND entry_date <= ?
             AND amount < 0
             AND statement_upload_id IS NOT NULL
           ORDER BY entry_date, amount""",
        (account_id, start, end),
    ).fetchall()

    # 2. Book-side candidates in a wider window (range ± tolerance). Only rows
    #    the bank has NOT cleared: a row already carrying a cleared_date was
    #    matched to some other statement line, and merging a second line into
    #    it would delete a real transaction.
    from datetime import datetime as _dt, timedelta as _td

    def _shift(iso, days):
        return (_dt.strptime(iso, "%Y-%m-%d") + _td(days=days)).strftime("%Y-%m-%d")

    wide_start, wide_end = _shift(start, -tol), _shift(end, tol)

    bp_rows = []
    if match_bp:
        bp_clause = "(bank_account_id = ?" + (" OR bank_account_id IS NULL" if is_default else "") + ")"
        bp_rows = conn.execute(
            f"""SELECT id, payment_date, vendor, payment_total, payment_method,
                       payment_ref, check_number, status, bank_account_id,
                       cleared, ap_payment_id
               FROM vendor_payments
               WHERE payment_date >= ? AND payment_date <= ?
                 AND (status IS NULL OR status NOT IN ('void', 'failed'))
                 AND COALESCE(cleared, 0) = 0
                 AND reconciliation_id IS NULL
                 AND {bp_clause}""",
            (wide_start, wide_end, account_id),
        ).fetchall()

    pr_rows = []
    if payroll_mode:
        wide_start, wide_end = _shift(start, -PAYROLL_LATE_DAYS), _shift(end, PAYROLL_EARLY_DAYS)
    if match_pr:
        pr_rows = conn.execute(
            """SELECT pc.id, pc.employee_name, pc.check_number, pc.net_pay,
                      pc.payment_method, pc.bank_account_id, pc.cleared,
                      COALESCE(pr.pay_date, pc.pay_period_end) AS pay_date
               FROM payroll_checks pc
               LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
               WHERE COALESCE(pr.pay_date, pc.pay_period_end) >= ?
                 AND COALESCE(pr.pay_date, pc.pay_period_end) <= ?
                 AND (pc.voided IS NULL OR pc.voided = 0)
                 AND pc.payment_method = 'Manual'
                 AND COALESCE(pc.net_pay, 0) <> 0
                 AND COALESCE(pc.cleared, 0) = 0
                 AND pc.reconciliation_id IS NULL
                 AND pc.bank_account_id = ?""",
            (wide_start, wide_end, account_id),
        ).fetchall()

    from collections import defaultdict
    bp_by_amount = defaultdict(list)
    for r in bp_rows:
        bp_by_amount[round(float(r["payment_total"] or 0), 2)].append(dict(r))
    pr_by_amount = defaultdict(list)
    for r in pr_rows:
        pr_by_amount[round(float(r["net_pay"] or 0), 2)].append(dict(r))

    used_bp_ids, used_pr_ids = set(), set()

    def _date_diff(a_iso, b_iso):
        return abs((_dt.strptime(a_iso, "%Y-%m-%d") - _dt.strptime(b_iso, "%Y-%m-%d")).days)

    candidates = []
    for me in me_rows:
        me = dict(me)
        target_amt = round(abs(float(me["amount"] or 0)), 2)
        me_date = me["entry_date"]

        cands = []
        for cand in bp_by_amount.get(target_amt, []):
            if cand["id"] in used_bp_ids:
                continue
            dd = _date_diff(me_date, cand["payment_date"])
            if dd > tol:
                continue
            cands.append({
                "source": "vendor_payment", "id": cand["id"],
                "date": cand["payment_date"],
                "amount": float(cand["payment_total"] or 0),
                "label": cand["vendor"] or "(no vendor)",
                "date_diff_days": dd,
                "current_bank_account_id": cand["bank_account_id"],
                "currently_cleared": bool(cand["cleared"]),
            })
        name_mismatches = []
        for cand in pr_by_amount.get(target_amt, []):
            if cand["id"] in used_pr_ids and not payroll_mode:
                continue
            dd = _date_diff(me_date, cand["pay_date"])
            name_status = None
            if payroll_mode:
                late = (_dt.strptime(me_date, "%Y-%m-%d") - _dt.strptime(cand["pay_date"], "%Y-%m-%d")).days
                if not (-PAYROLL_EARLY_DAYS <= late <= PAYROLL_LATE_DAYS):
                    continue
                name_status = _ocr_names_employee(me["memo"], cand["employee_name"])
                if name_status == "mismatch":
                    name_mismatches.append(cand["employee_name"])
                    continue
            elif dd > tol:
                continue
            cands.append({
                "name_check": name_status,
                "check_number_agrees": (str(cand["check_number"] or "") != ""
                                        and str(cand["check_number"]) == str(me["ref_number"] or "")),
                "source": "payroll_check", "id": cand["id"],
                "date": cand["pay_date"],
                "amount": float(cand["net_pay"] or 0),
                "label": f"Payroll: {cand['employee_name']}",
                "date_diff_days": dd,
                "current_bank_account_id": cand["bank_account_id"],
                "currently_cleared": bool(cand["cleared"]),
            })

        # Closest date wins; payroll_check preferred on a tie (more specific).
        # Two candidates from the same source at the same distance are
        # ambiguous and left alone for a human (the three Cozzini $23.90
        # drafts of 2026-05-29 were exactly this).
        chosen, skip_reason, ambiguous_count = None, None, 0
        if not cands:
            skip_reason = "name_mismatch" if name_mismatches else "no_match"
        elif payroll_mode:
            # No tie-break: one candidate or a human decides.
            if len(cands) > 1:
                skip_reason, ambiguous_count = "ambiguous", len(cands)
            else:
                chosen = cands[0]
        else:
            cands.sort(key=lambda c: (c["date_diff_days"],
                                      0 if c["source"] == "payroll_check" else 1))
            best = cands[0]
            same = [c for c in cands if c["date_diff_days"] == best["date_diff_days"]
                    and c["source"] == best["source"]]
            if len(same) > 1:
                skip_reason, ambiguous_count = "ambiguous", len(same)
            else:
                chosen = best
        if chosen:
            (used_bp_ids if chosen["source"] == "vendor_payment" else used_pr_ids).add(chosen["id"])

        candidates.append({
            "manual_entry_id": me["id"],
            "manual_entry_date": me["entry_date"],
            "manual_entry_amount": float(me["amount"] or 0),
            "manual_entry_payee": me["payee"],
            "manual_entry_memo": me["memo"],
            "manual_entry_ref": me["ref_number"],
            "match": chosen,
            "skip_reason": skip_reason,
            "ambiguous_count": ambiguous_count,
            "options": cands if skip_reason == "ambiguous" else None,
        })

    if payroll_mode:
        # A paycheck that is the only candidate for two statement lines is
        # not a match for either.
        from collections import Counter
        claims = Counter(c["match"]["id"] for c in candidates if c["match"])
        for c in candidates:
            if c["match"] and claims[c["match"]["id"]] > 1:
                c["skip_reason"], c["ambiguous_count"] = "ambiguous", claims[c["match"]["id"]]
                c["contested"] = c["match"]
                c["match"] = None

    merged_count = deleted_count = 0
    if commit:
        for c in candidates:
            m = c["match"]
            if not m:
                continue
            entry_date = c["manual_entry_date"]
            # The book row takes the STATEMENT date: that is the day the bank
            # cleared it. The candidate query guarantees it carried no date.
            if m["source"] == "vendor_payment":
                conn.execute(
                    """UPDATE vendor_payments
                       SET bank_account_id = COALESCE(bank_account_id, ?),
                           cleared = 1, cleared_date = ?
                       WHERE id = ?""",
                    (account_id, entry_date, m["id"]),
                )
            else:
                conn.execute(
                    "UPDATE payroll_checks SET cleared = 1, cleared_date = ? WHERE id = ?",
                    (entry_date, m["id"]),
                )
            # Record the merge BEFORE deleting anything. Capture the full row
            # so this is reversible; a wrong match is otherwise invisible once
            # the statement line is gone.
            full = conn.execute(
                "SELECT * FROM manual_bank_entries WHERE id = ?", (c["manual_entry_id"],)
            ).fetchone()
            conn.execute(
                """INSERT INTO register_merge_audit
                   (merged_by, bank_account_id, target_source, target_id,
                    target_label, target_cleared_date, deleted_entry_id,
                    deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                    match_amount, match_date_diff_days, match_tolerance_days,
                    match_rule)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (who, account_id, m["source"], m["id"], m.get("label"), entry_date,
                 c["manual_entry_id"], c["manual_entry_date"], c["manual_entry_amount"],
                 json.dumps(dict(full)) if full else None,
                 m.get("amount"), m.get("date_diff_days"),
                 PAYROLL_LATE_DAYS if payroll_mode else tol,
                 PAYROLL_MATCH_RULE if payroll_mode else DEDUPE_MATCH_RULE),
            )
            conn.execute("DELETE FROM manual_bank_entries WHERE id = ?",
                         (c["manual_entry_id"],))
            merged_count += 1
            deleted_count += 1

    return {
        "start": start, "end": end,
        "candidates": candidates,
        "summary": {
            "manual_entries_scanned": len(me_rows),
            "matched": sum(1 for c in candidates if c["match"]),
            "ambiguous": sum(1 for c in candidates if c["skip_reason"] == "ambiguous"),
            "name_mismatch": sum(1 for c in candidates if c["skip_reason"] == "name_mismatch"),
            "unmatched": sum(1 for c in candidates if c["skip_reason"] == "no_match"),
            "would_merge_amount": round(
                sum(abs(c["manual_entry_amount"]) for c in candidates if c["match"]), 2),
        },
        "merged_count": merged_count,
        "deleted_count": deleted_count,
    }


@bank_reconcile_bp.route("/api/bank-reconcile/dedupe", methods=["POST"])
@admin_required
def dedupe_register():
    """Find and (optionally) merge duplicates between statement-imported
    manual_bank_entries and the uncleared vendor_payments / manual
    payroll_checks they duplicate.

    The rule (job 066's, now the endpoint's): a statement outflow pairs with
    an UNCLEARED book row of exactly the same amount within
    date_tolerance_days; closest date wins, payroll preferred on a tie,
    ambiguous (two equal candidates from the same source) is skipped. On
    commit the book row is stamped cleared ON THE STATEMENT DATE and the
    statement row is deleted, with the full row saved in register_merge_audit.

    Body (JSON):
        account_id:            int, required
        start_date, end_date:  "YYYY-MM-DD" — one window, or
        all_periods:           true — every statement period held for the
                               account, one transaction per period; periods
                               already signed off are skipped unless
                               include_reconciled is true
        date_tolerance_days:   int, default 5 (job 066 used 7)
        match_vendor_payments: bool, default true
        match_payroll_manual:  bool, default true
        commit:                bool, default false (preview only)

    Response: the single-window shape (candidates, summary, applied,
    merged_count, deleted_manual_entries) plus `periods`, one entry per
    window run, each with its own counts and any error.
    """
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not isinstance(account_id, int):
        return jsonify({"error": "account_id (int) is required"}), 400
    all_periods = bool(data.get("all_periods"))
    include_reconciled = bool(data.get("include_reconciled"))
    import re as _re
    start = (data.get("start_date") or "").strip()
    end = (data.get("end_date") or "").strip()
    if not all_periods and (not _re.match(r"^\d{4}-\d{2}-\d{2}$", start)
                            or not _re.match(r"^\d{4}-\d{2}-\d{2}$", end)):
        return jsonify({"error": "start_date and end_date must be YYYY-MM-DD "
                                 "(or pass all_periods: true)"}), 400
    try:
        tol = int(data.get("date_tolerance_days", 5))
    except (TypeError, ValueError):
        tol = 5
    tol = max(0, min(60, tol))
    match_bp = bool(data.get("match_vendor_payments", True))
    match_pr = bool(data.get("match_payroll_manual", True))
    commit = bool(data.get("commit", False))
    who = session.get("username") or session.get("email") or "unknown"

    conn = get_connection()
    try:
        bank = conn.execute(
            "SELECT id, name, account_last4 FROM bank_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not bank:
            return jsonify({"error": f"bank_account {account_id} not found"}), 404

        windows = []
        skipped_periods = []
        if all_periods:
            closed = {(r["period_start"], r["period_end"]) for r in conn.execute(
                "SELECT period_start, period_end FROM bank_reconciliations "
                "WHERE bank_account_id = ? AND status = 'reconciled'", (account_id,))}
            for u in conn.execute(
                "SELECT period_start, period_end FROM bank_statement_uploads "
                "WHERE bank_account_id = ? AND period_start IS NOT NULL "
                "ORDER BY period_start", (account_id,)):
                w = (u["period_start"], u["period_end"])
                if w in closed and not include_reconciled:
                    skipped_periods.append({"period": f"{w[0]}..{w[1]}",
                                            "reason": "signed off"})
                    continue
                windows.append(w)
            if not windows:
                return jsonify({"error": "no open statement periods for this account",
                                "skipped": skipped_periods}), 400
        else:
            windows.append((start, end))

        periods = []
        all_cands = []
        totals = {"manual_entries_scanned": 0, "matched": 0, "ambiguous": 0,
                  "unmatched": 0, "would_merge_amount": 0.0}
        merged = deleted = 0
        for (ws, we) in windows:
            try:
                r = _dedupe_period(conn, bank, ws, we, tol, match_bp, match_pr, commit, who)
                if commit:
                    conn.commit()
            except Exception as e:
                conn.rollback()
                logger.exception("dedupe failed for %s..%s", ws, we)
                periods.append({"period": f"{ws}..{we}", "error": str(e)})
                break
            periods.append({"period": f"{ws}..{we}", **r["summary"],
                            "merged": r["merged_count"]})
            all_cands.extend(r["candidates"])
            for k in totals:
                totals[k] += r["summary"][k]
            merged += r["merged_count"]
            deleted += r["deleted_count"]
        totals["would_merge_amount"] = round(totals["would_merge_amount"], 2)

        return jsonify({
            "account_id": account_id,
            "account_last4": bank["account_last4"],
            "start": windows[0][0],
            "end": windows[-1][1],
            "all_periods": all_periods,
            "date_tolerance_days": tol,
            "match_rule": DEDUPE_MATCH_RULE,
            "candidates": all_cands,
            "summary": totals,
            "periods": periods,
            "skipped_periods": skipped_periods,
            "applied": commit,
            "merged_count": merged,
            "deleted_manual_entries": deleted,
        })
    finally:
        conn.close()


# ─── RECONCILIATION SIGN-OFF ─────────────────────────────────────────────────
#
# The pass condition is "the delta is fully itemized and accepted", not
# "delta == 0". A period holding a legitimate outstanding check can never
# satisfy the latter, so closing a period means: the cleared rows tie to the
# statement exactly, and everything left over is captured as a named
# reconciling item that somebody accepted.



def _c(x):
    """Money -> integer cents. The premise is 'ties to the penny'."""
    return int(round(float(x or 0) * 100))


def _bp_account_clause(conn, acct_id):
    acct = conn.execute("SELECT account_last4 FROM bank_accounts WHERE id = ?", (acct_id,)).fetchone()
    is_default = bool(acct and acct["account_last4"] == "5975")
    return "(bank_account_id = ?" + (" OR bank_account_id IS NULL" if is_default else "") + ")"


def _cleared_flow_by_date(conn, acct_id, start, end):
    """(inflow_cents, outflow_cents) of every register row the bank cleared
    inside [start, end] — by cleared_date, whatever its book date. The sums
    are register_flow()'s, the register's own, so the two cannot drift."""
    from routes.register_routes import register_flow
    acct = conn.execute("SELECT * FROM bank_accounts WHERE id = ?", (acct_id,)).fetchone()
    if acct is None:
        raise ValueError(f"bank account {acct_id} not found")
    inflow, outflow = register_flow(conn, acct, start, end, by="cleared_date")
    return _c(inflow), _c(outflow)


def _reconciliation_state(conn, upload):
    """Compute the closing figures + the outstanding items for one statement
    period, WITHOUT writing anything. Shared by preview, close and import-all.

    THE TIE-OUT IS BY CLEARED DATE. A bank reconciliation counts a row on the
    day the BANK cleared it. A check cut 7/28 that clears 8/03 is outstanding
    at 7/31 and cleared in August — regardless of the date on the check.
    Counting cleared=1 rows by their book date (the behaviour until job 065)
    produced the alternating negative/positive deltas of 2026-09-22:
    -13,807 May / +4,878 June etc. Statement-imported rows carry
    cleared_date == entry_date, so they are unaffected; Bill Pay and payroll
    rows count in the period whose statement actually shows them.

        bank_balance   = statement beginning + rows cleared in the period
        outstanding    = every row dated on or before period end that the
                         bank had NOT cleared by period end — cumulative, so
                         a check nobody cashes is carried period to period
                         and bank_reconciliation_items.carry_count can grow
        book_balance   = bank_balance + outstanding (the standard proof)
        delta          = bank_balance - statement ending; ties when zero

    Both balances sit on the SAME baseline, the statement's beginning
    balance — the one number here we did not compute ourselves. The previous
    mixed baselines (bank from the statement, book from the register roll-
    forward) are what produced the +26,251.28 / +11,466.49 "unexplained
    gaps" on Dennis Jan/Feb — baseline artifacts, not money.

    `identity_holds` is an independent check: the book balance rebuilt from
    BOOK dates (beginning + prior outstanding + this period's rows) must
    equal bank + outstanding once rows cleared in a different period than
    they were booked in (`off_period_cleared`) are accounted for. If it is
    ever False, the four sums are not describing one set of rows.
    """
    from routes.register_routes import (build_register_view, register_flow,
                                        row_cleared_by, _pre_period_net)
    acct_id = upload["bank_account_id"]
    start, end = upload["period_start"], upload["period_end"]
    account = conn.execute("SELECT * FROM bank_accounts WHERE id = ?", (acct_id,)).fetchone()
    if account is None:
        raise ValueError(f"bank account {acct_id} not found")

    # Every row from the anchor through period end, so outstanding can be
    # cumulative. Reuse the register's own builder so these rows can never
    # drift from what the UI renders.
    floor = account["opening_date"] or "1970-01-01"
    if floor > start:
        floor = start
    wide = build_register_view(conn, acct_id, floor, end)
    if wide is None:
        raise ValueError(f"bank account {acct_id} not found")
    all_rows = wide["rows"]
    rows = [r for r in all_rows if (r["date"] or "") >= start]

    begin_c = _c(upload["beginning_balance"])
    end_c = _c(upload["ending_balance"])
    clr_in, clr_out = _cleared_flow_by_date(conn, acct_id, start, end)
    bank_c = begin_c + clr_in - clr_out
    delta_c = bank_c - end_c

    def signed(r):
        return _c(r["inflow"]) - _c(r["outflow"])

    outstanding = [r for r in all_rows if not row_cleared_by(r, end)]
    outstanding_c = sum(signed(r) for r in outstanding)
    book_c = bank_c + outstanding_c

    # Independent rebuild of the book balance from BOOK dates.
    day_before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    prior_open_c = sum(signed(r) for r in all_rows
                       if (r["date"] or "") < start and not row_cleared_by(r, day_before))
    all_in = sum(_c(r["inflow"]) for r in rows)
    all_out = sum(_c(r["outflow"]) for r in rows)
    book_by_date_c = begin_c + prior_open_c + all_in - all_out
    # Rows booked in this period but cleared BEFORE it (a Bill Pay entered
    # after the bank drafted it), and rows cleared IN this period but booked
    # after it. Both are legitimate; both make book-by-date differ from
    # bank + outstanding, so they are itemized rather than hidden.
    early = [r for r in rows if r["cleared"]
             and (r.get("cleared_date") or r["date"] or "") < start]
    late_view = build_register_view(conn, acct_id,
                                    (date.fromisoformat(end) + timedelta(days=1)).isoformat(),
                                    (date.fromisoformat(end) + timedelta(days=120)).isoformat())
    late = [r for r in (late_view["rows"] if late_view else []) if r["cleared"]
            and start <= (r.get("cleared_date") or r["date"] or "") <= end]
    early_c = sum(signed(r) for r in early)
    late_c = sum(signed(r) for r in late)
    identity_holds = (book_by_date_c - bank_c) == (outstanding_c + early_c - late_c)

    # Independent cross-check: the register's own roll-forward opening should
    # equal the statement's beginning balance. If it does not, the books'
    # history disagrees with the bank's for this period, and that is a
    # finding in its own right — reported, never silently absorbed.
    register_opening = float(account["opening_balance"] or 0) + _pre_period_net(conn, account, start)
    opening_drift_c = _c(register_opening) - begin_c

    end_date = date.fromisoformat(end)

    def item(r):
        try:
            age = (end_date - date.fromisoformat(r["date"])).days if r["date"] else None
        except ValueError:
            age = None
        return {
            "source": r["source"],
            "source_id": r["source_id"],
            "entry_date": r["date"],
            "payee": r.get("payee"),
            "memo": r.get("memo"),
            "amount": round(r["inflow"] - r["outflow"], 2),
            "age_days": age,
            "cleared_date": r.get("cleared_date") if r.get("cleared") else None,
            "carried": (r["date"] or "") < start,
        }

    items = [item(r) for r in sorted(outstanding, key=lambda r: (r["date"] or "", r["source"], r["source_id"]))]

    return {
        "bank_account_id": acct_id,
        "statement_upload_id": upload["id"],
        "period_start": start,
        "period_end": end,
        "beginning_balance": round(begin_c / 100, 2),
        "ending_balance": round(end_c / 100, 2),
        "bank_balance": round(bank_c / 100, 2),
        "book_balance": round(book_c / 100, 2),
        "outstanding_net": round(outstanding_c / 100, 2),
        "outstanding_prior_count": sum(1 for i in items if i["carried"]),
        "delta": round(delta_c / 100, 2),
        "ties": delta_c == 0,
        "outstanding_items": items,
        "identity_holds": identity_holds,
        "book_balance_by_date": round(book_by_date_c / 100, 2),
        "off_period_cleared": {
            "cleared_before_period": [item(r) for r in early],
            "cleared_in_period_booked_after": [item(r) for r in late],
            "net": round((early_c - late_c) / 100, 2),
        },
        "opening_drift": round(opening_drift_c / 100, 2),
        "register_opening": round(register_opening, 2),
        # A period whose book side carries ONLY imported statement rows cannot
        # fail its own tie-out: the statement is being checked against itself.
        # Report the composition so a hollow tie is visible as one.
        "row_sources": {
            k: sum(1 for r in rows if r["source"] == k)
            for k in ("manual", "bill_pay", "payroll", "deposit")
        },
    }


@bank_reconcile_bp.route("/api/bank-reconcile/reconciliation/preview", methods=["GET"])
@login_required
def preview_reconciliation():
    """Dry run: what would closing this period record? Writes nothing.

    Query: ?upload_id=N   (or ?account_id=&start=&end=)
    """
    conn = get_connection()
    try:
        upload = _resolve_upload(conn)
        if upload is None:
            return jsonify({"error": "Statement period not found"}), 404
        state = _reconciliation_state(conn, upload)
        existing = conn.execute(
            "SELECT id, status, closed_by, closed_at FROM bank_reconciliations "
            "WHERE bank_account_id = ? AND period_start = ? AND period_end = ?",
            (upload["bank_account_id"], upload["period_start"], upload["period_end"]),
        ).fetchone()
        state["existing"] = dict(existing) if existing else None
        state["outstanding_count"] = len(state["outstanding_items"])
        return jsonify(state)
    finally:
        conn.close()


@bank_reconcile_bp.route("/api/bank-reconcile/reconciliation/close", methods=["POST"])
@admin_required
def close_reconciliation():
    """Sign off a statement period.

    Body: { upload_id } or { account_id, start, end }, plus optional { notes }.

    REFUSES unless the cleared rows tie to the statement exactly. A nonzero
    delta means a transaction is missing, an amount is wrong, or something is
    cleared that should not be — none of which a signature should paper over.
    Outstanding items are NOT a reason to refuse; they are the point.
    """
    data = request.get_json(silent=True) or {}
    conn = get_connection()
    try:
        upload = _resolve_upload(conn, data)
        if upload is None:
            return jsonify({"error": "Statement period not found"}), 404

        state = _reconciliation_state(conn, upload)
        if not state["ties"]:
            return jsonify({
                "error": f"Refusing to close: cleared rows are off by "
                         f"${state['delta']:,.2f}. The statement must tie exactly "
                         f"before it can be signed off — outstanding items are "
                         f"fine, an unexplained delta is not.",
                "delta": state["delta"],
            }), 409

        who = session.get("username") or session.get("email") or "unknown"
        prior = conn.execute(
            "SELECT id FROM bank_reconciliations WHERE bank_account_id = ? "
            "AND period_end < ? AND status = 'reconciled' "
            "ORDER BY period_end DESC LIMIT 1",
            (upload["bank_account_id"], upload["period_start"]),
        ).fetchone()

        # Re-close support: capture the OLD rec id for this exact period (if
        # any) so we can unstamp the rows it previously locked. Without this,
        # a re-close would orphan reconciliation_id references pointing at a
        # deleted bank_reconciliations row.
        old_rec = conn.execute(
            "SELECT id FROM bank_reconciliations WHERE bank_account_id = ? "
            "AND period_start = ? AND period_end = ?",
            (upload["bank_account_id"], upload["period_start"], upload["period_end"]),
        ).fetchone()
        old_rec_id = old_rec["id"] if old_rec else None

        conn.execute("DELETE FROM bank_reconciliation_items WHERE reconciliation_id IN "
                     "(SELECT id FROM bank_reconciliations WHERE bank_account_id = ? "
                     " AND period_start = ? AND period_end = ?)",
                     (upload["bank_account_id"], upload["period_start"], upload["period_end"]))
        conn.execute("DELETE FROM bank_reconciliations WHERE bank_account_id = ? "
                     "AND period_start = ? AND period_end = ?",
                     (upload["bank_account_id"], upload["period_start"], upload["period_end"]))
        if old_rec_id:
            for _tbl in ("vendor_payments", "payroll_checks",
                         "bank_deposits", "manual_bank_entries"):
                conn.execute(
                    f"UPDATE {_tbl} SET reconciliation_id = NULL "
                    f"WHERE reconciliation_id = ?", (old_rec_id,)
                )

        cur = conn.execute(
            """INSERT INTO bank_reconciliations
               (bank_account_id, statement_upload_id, period_start, period_end,
                status, beginning_balance, ending_balance, bank_balance,
                book_balance, outstanding_net, delta, closed_by, closed_at, notes)
               VALUES (?,?,?,?,'reconciled',?,?,?,?,?,?,?,datetime('now'),?)""",
            (upload["bank_account_id"], upload["id"], upload["period_start"],
             upload["period_end"], state["beginning_balance"], state["ending_balance"],
             state["bank_balance"], state["book_balance"], state["outstanding_net"],
             state["delta"], who, data.get("notes")),
        )
        rec_id = cur.lastrowid

        # Stamp every CLEARED row in this period with the new reconciliation_id.
        # From now on those render as R (locked) and PUT /api/register/row/cleared
        # refuses to touch them unless the discrepancy flow passes force=true.
        # The scope matches build_register_view()'s date/filter shape so the
        # tie-out we just checked is exactly the set of rows we lock.
        stamped = _stamp_reconciled_rows(
            conn, upload["bank_account_id"],
            upload["period_start"], upload["period_end"], rec_id,
        )

        carried = 0
        for it in state["outstanding_items"]:
            prev = None
            if prior:
                prev = conn.execute(
                    "SELECT id, carry_count FROM bank_reconciliation_items "
                    "WHERE reconciliation_id = ? AND source = ? AND source_id = ?",
                    (prior["id"], it["source"], it["source_id"]),
                ).fetchone()
            conn.execute(
                """INSERT INTO bank_reconciliation_items
                   (reconciliation_id, source, source_id, entry_date, payee, memo,
                    amount, age_days, carried_from_item_id, carry_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rec_id, it["source"], it["source_id"], it["entry_date"],
                 it["payee"], it["memo"], it["amount"], it["age_days"],
                 (prev["id"] if prev else None),
                 ((prev["carry_count"] or 0) + 1 if prev else 0)),
            )
            if prev:
                carried += 1
        conn.commit()

        return jsonify({
            "status": "ok",
            "reconciliation_id": rec_id,
            "closed_by": who,
            "delta": state["delta"],
            "outstanding_count": len(state["outstanding_items"]),
            "carried_forward": carried,
            "stamped_reconciled": stamped,
        })
    finally:
        conn.close()


def _stamp_reconciled_rows(conn, account_id, period_start, period_end, rec_id,
                           only_unstamped=False):
    """Set reconciliation_id = rec_id on every register row the bank CLEARED
    inside this period, across the four source tables. Returns the number of
    rows stamped.

    Filter shape mirrors register_flow(by="cleared_date") — the bank side of
    the tie-out that was just verified — so the rows locked are exactly the
    rows that tied:
      - vendor_payments: cleared=1, cleared_date in period, not void/failed,
        matching acct (plus NULL bank_account_id if this is the Chatham default)
      - payroll_checks:  cleared=1, cleared_date in period, not voided, not
        Direct Deposit, net_pay <> 0, matching acct
      - bank_deposits:   cleared=1, cleared_date in period, matching acct
      - manual_bank_entries: cleared=1, cleared_date in period, matching acct
    A cleared row with no cleared_date falls back to its book date, as
    everywhere else. Outstanding rows are not locked: they are not yet the
    bank's.

    only_unstamped=True adds `reconciliation_id IS NULL` (init backfill).
    """
    acct = conn.execute(
        "SELECT account_last4 FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    is_default_account = acct and acct["account_last4"] == "5975"
    guard = "reconciliation_id IS NULL AND " if only_unstamped else ""

    total = 0

    bp_where = (
        f"{guard}cleared = 1 "
        "AND COALESCE(cleared_date, payment_date) >= ? "
        "AND COALESCE(cleared_date, payment_date) <= ? "
        "AND (status IS NULL OR status NOT IN ('void', 'failed')) AND ("
        "bank_account_id = ?"
        + (" OR bank_account_id IS NULL" if is_default_account else "")
        + ")"
    )
    cur = conn.execute(
        f"UPDATE vendor_payments SET reconciliation_id = ? WHERE {bp_where}",
        (rec_id, period_start, period_end, account_id),
    )
    total += cur.rowcount or 0

    try:
        pr_guard = "pc.reconciliation_id IS NULL AND " if only_unstamped else ""
        cur = conn.execute(
            "UPDATE payroll_checks SET reconciliation_id = ? "
            "WHERE id IN ("
            "  SELECT pc.id FROM payroll_checks pc "
            "  LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id "
            f"  WHERE {pr_guard}pc.cleared = 1 "
            "  AND COALESCE(pc.cleared_date, pr.pay_date, pc.pay_period_end) >= ? "
            "  AND COALESCE(pc.cleared_date, pr.pay_date, pc.pay_period_end) <= ? "
            "  AND (pc.voided IS NULL OR pc.voided = 0) "
            "  AND (pc.payment_method IS NULL OR pc.payment_method != 'Direct Deposit') "
            "  AND COALESCE(pc.net_pay, 0) <> 0 "
            "  AND pc.bank_account_id = ?"
            ")",
            (rec_id, period_start, period_end, account_id),
        )
        total += cur.rowcount or 0
    except Exception as e:
        logger.warning(f"stamp payroll failed for account {account_id}: {e}")

    cur = conn.execute(
        "UPDATE bank_deposits SET reconciliation_id = ? "
        f"WHERE {guard}cleared = 1 AND bank_account_id = ? "
        "AND COALESCE(cleared_date, deposit_date) >= ? "
        "AND COALESCE(cleared_date, deposit_date) <= ?",
        (rec_id, account_id, period_start, period_end),
    )
    total += cur.rowcount or 0

    cur = conn.execute(
        "UPDATE manual_bank_entries SET reconciliation_id = ? "
        f"WHERE {guard}cleared = 1 AND bank_account_id = ? "
        "AND COALESCE(cleared_date, entry_date) >= ? "
        "AND COALESCE(cleared_date, entry_date) <= ?",
        (rec_id, account_id, period_start, period_end),
    )
    total += cur.rowcount or 0

    return total


@bank_reconcile_bp.route("/api/bank-reconcile/reconciliations", methods=["GET"])
@login_required
def list_reconciliations():
    """Closed periods, newest first, with their outstanding items.

    `stale_outstanding` flags items that have been carried forward three or
    more times — a check nobody has cashed in three statement periods is a
    void candidate, not a timing difference.
    """
    conn = get_connection()
    try:
        acct = request.args.get("account_id", type=int)
        q = ("SELECT * FROM bank_reconciliations "
             + ("WHERE bank_account_id = ? " if acct else "")
             + "ORDER BY bank_account_id, period_start DESC")
        recs = [dict(r) for r in conn.execute(q, (acct,) if acct else ())]
        stale = []
        for r in recs:
            r["items"] = [dict(i) for i in conn.execute(
                "SELECT * FROM bank_reconciliation_items WHERE reconciliation_id = ? "
                "ORDER BY entry_date", (r["id"],))]
            for i in r["items"]:
                if (i["carry_count"] or 0) >= 3:
                    stale.append({**i, "period_end": r["period_end"]})
        return jsonify({"reconciliations": recs, "stale_outstanding": stale})
    finally:
        conn.close()


def _resolve_upload(conn, data=None):
    """Find the statement upload from either an upload_id or an explicit
    (account_id, start, end) triple."""
    src = data if data is not None else request.args
    up_id = src.get("upload_id")
    if up_id:
        return conn.execute(
            "SELECT * FROM bank_statement_uploads WHERE id = ?", (int(up_id),)
        ).fetchone()
    acct = src.get("account_id")
    start, end = src.get("start"), src.get("end")
    if not (acct and start and end):
        return None
    return conn.execute(
        "SELECT * FROM bank_statement_uploads WHERE bank_account_id = ? "
        "AND period_start = ? AND period_end = ?", (int(acct), start, end)
    ).fetchone()
