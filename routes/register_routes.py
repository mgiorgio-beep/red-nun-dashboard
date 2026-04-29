"""
Bank Register routes — QBO-style register per operating bank account.

Blueprint: register_bp at /api/register/*

Unifies four transaction sources into one register view:
  * vendor_payments  (bill pay checks & ACH, mirrored from ap_payments)
  * payroll_checks   (payroll runs)
  * bank_deposits    (local cache of Toast / cash deposits pulled from QBO)
  * manual_bank_entries (transfers, fees, adjustments — entered by hand)

V1 scope: register view + manual entry + on-demand QBO deposit sync.
V2 will add statement upload + reconcile workflow (cleared checkbox is
already populated so the data shape is ready).

All schema additions are purely additive. Existing tables are extended via
ALTER TABLE ADD COLUMN (nullable) only, matching the pattern in
init_payment_tables() — no destructive changes.
"""

import csv
import json
import logging
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required, admin_required

logger = logging.getLogger(__name__)

register_bp = Blueprint("register_bp", __name__)


# ─── TABLE INIT ──────────────────────────────────────────────────────────────

def init_register_tables():
    """Create bank register tables and extend existing ones.

    Safe to run repeatedly. All migrations are additive.
    """
    conn = get_connection()
    conn.executescript("""
        -- Operating bank accounts (Chatham CCF, Dennis CCF, add more as needed)
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,            -- e.g. "Cape Cod Five (5975) — Chatham"
            short_name TEXT,               -- e.g. "Chatham Operating"
            qbo_account_id TEXT,           -- QBO Account.Id for deposit sync
            qbo_account_name TEXT,         -- QBO display name for JE/deposit push
            location TEXT,                 -- chatham | dennis | null (if unassigned)
            account_last4 TEXT,
            opening_balance REAL DEFAULT 0,
            opening_date TEXT,             -- YYYY-MM-DD. Balance above assumed prior to this.
            active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_bank_accounts_loc ON bank_accounts(location);

        -- Manual register entries (transfers, bank fees, interest, adjustments)
        CREATE TABLE IF NOT EXISTS manual_bank_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_account_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,      -- YYYY-MM-DD
            entry_type TEXT NOT NULL,      -- transfer | fee | interest | adjustment | other
            payee TEXT,
            memo TEXT,
            ref_number TEXT,
            amount REAL NOT NULL,          -- signed: positive = deposit into bank, negative = payment out
            cleared INTEGER DEFAULT 0,
            cleared_date TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_mbe_account ON manual_bank_entries(bank_account_id);
        CREATE INDEX IF NOT EXISTS idx_mbe_date ON manual_bank_entries(entry_date);

        -- Local cache of deposits pulled from QBO (Toast CC settlement + cash deposits)
        CREATE TABLE IF NOT EXISTS bank_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_account_id INTEGER NOT NULL,
            deposit_date TEXT NOT NULL,    -- YYYY-MM-DD (bank date)
            amount REAL NOT NULL,          -- always positive; register shows as inflow
            description TEXT,
            memo TEXT,
            source TEXT,                   -- 'toast' | 'cash' | 'qbo_other'
            qbo_txn_id TEXT UNIQUE,        -- TransactionList.id from QBO (dedup key)
            qbo_txn_type TEXT,             -- Deposit | Transfer | JournalEntry etc.
            cleared INTEGER DEFAULT 1,     -- deposits pulled from QBO assumed cleared
            cleared_date TEXT,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_bd_account ON bank_deposits(bank_account_id);
        CREATE INDEX IF NOT EXISTS idx_bd_date ON bank_deposits(deposit_date);
        CREATE INDEX IF NOT EXISTS idx_bd_qbo ON bank_deposits(qbo_txn_id);
    """)

    # ── Extend existing tables with bank_account_id (additive, nullable) ──
    migrations = [
        "ALTER TABLE vendor_payments ADD COLUMN bank_account_id INTEGER",
        "ALTER TABLE vendor_payments ADD COLUMN cleared INTEGER DEFAULT 0",
        "ALTER TABLE vendor_payments ADD COLUMN cleared_date TEXT",
        "ALTER TABLE payroll_checks ADD COLUMN bank_account_id INTEGER",
        "ALTER TABLE payroll_checks ADD COLUMN cleared INTEGER DEFAULT 0",
        "ALTER TABLE payroll_checks ADD COLUMN cleared_date TEXT",
        # GL coding columns (added later — guard with try/pass for idempotency)
        "ALTER TABLE vendor_payments ADD COLUMN gl_account_id INTEGER",
        "ALTER TABLE payroll_checks ADD COLUMN gl_account_id INTEGER",
        "ALTER TABLE bank_deposits ADD COLUMN gl_account_id INTEGER",
        "ALTER TABLE manual_bank_entries ADD COLUMN gl_account_id INTEGER",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists

    # ── GL chart of accounts ──
    # Each location (Chatham, Dennis) has its own QBO company file and its
    # own chart of accounts. We carry a location column so the dropdown on
    # the register can show the right list.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gl_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qbo_id TEXT,
            acct_num TEXT,
            name TEXT NOT NULL,
            account_type TEXT,
            account_subtype TEXT,
            location TEXT,                  -- chatham | dennis (NULL = legacy/unscoped)
            active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_gl_active ON gl_accounts(active, location, account_type, name);
        CREATE INDEX IF NOT EXISTS idx_gl_qbo_id ON gl_accounts(qbo_id, location);

        -- Auto-populate rules: when a description matches the pattern (case-insensitive
        -- substring), pre-assign the named GL account. Scoped per location so a
        -- DAVO rule for Chatham doesn't auto-code Dennis rows to a Chatham account.
        CREATE TABLE IF NOT EXISTS gl_account_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT,                  -- chatham | dennis | NULL (any)
            pattern TEXT NOT NULL,
            gl_account_id INTEGER NOT NULL,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(location, pattern),
            FOREIGN KEY (gl_account_id) REFERENCES gl_accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_gl_rules_pattern ON gl_account_rules(location, pattern);
    """)
    # Idempotent ALTER for older installs that already had the table without
    # location (the unique constraint above is only enforced on new tables).
    try:
        conn.execute("ALTER TABLE gl_accounts ADD COLUMN location TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE gl_account_rules ADD COLUMN location TEXT")
    except Exception:
        pass

    # ── Seed the two Cape Cod Five operating accounts if missing ──
    # Mirrors the hardcoded values in payroll_routes.py:29-30.
    seed_accounts = [
        {
            "name": "Cape Cod Five (5975) — Chatham",
            "short_name": "Chatham Operating",
            "qbo_account_id": "63",
            "qbo_account_name": "Cape Cod Five (5975)",
            "location": "chatham",
            "account_last4": "5975",
            "sort_order": 10,
        },
        {
            "name": "Cape Cod Five (2757) — Dennis",
            "short_name": "Dennis Operating",
            "qbo_account_id": None,  # unknown; populate via /api/register/accounts/<id>
            "qbo_account_name": "Cape Cod Five (2757)",
            "location": "dennis",
            "account_last4": "2757",
            "sort_order": 20,
        },
    ]
    for acct in seed_accounts:
        existing = conn.execute(
            "SELECT id FROM bank_accounts WHERE account_last4 = ?",
            (acct["account_last4"],),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """INSERT INTO bank_accounts
               (name, short_name, qbo_account_id, qbo_account_name,
                location, account_last4, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (acct["name"], acct["short_name"], acct["qbo_account_id"],
             acct["qbo_account_name"], acct["location"], acct["account_last4"],
             acct["sort_order"]),
        )

    # ── Backfill payroll_checks.bank_account_id from location column ──
    try:
        chatham_id = conn.execute(
            "SELECT id FROM bank_accounts WHERE account_last4 = '5975'"
        ).fetchone()
        dennis_id = conn.execute(
            "SELECT id FROM bank_accounts WHERE account_last4 = '2757'"
        ).fetchone()
        if chatham_id and dennis_id:
            conn.execute(
                "UPDATE payroll_checks SET bank_account_id = ? "
                "WHERE bank_account_id IS NULL AND location = 'chatham'",
                (chatham_id["id"],),
            )
            conn.execute(
                "UPDATE payroll_checks SET bank_account_id = ? "
                "WHERE bank_account_id IS NULL AND location = 'dennis'",
                (dennis_id["id"],),
            )
    except Exception as e:
        logger.warning(f"payroll_checks bank_account backfill skipped: {e}")

    conn.commit()
    conn.close()

    # ── Seed gl_accounts from the COA CSV if empty ──
    try:
        seed_gl_accounts_if_empty()
    except Exception as e:
        logger.warning(f"GL account seed skipped: {e}")


# ─── GL ACCOUNT SEED ─────────────────────────────────────────────────────────

# Where the seed CSVs live. We look for one CSV per location, plus an
# unscoped fallback. Search order per location:
#   data/<loc>_coa.csv   →  /opt/red-nun-dashboard/data/<loc>_coa.csv
#   /home/rednun/<loc>_coa.csv  (legacy location)
def _coa_seed_paths_for(location: str) -> list[Path]:
    paths = [
        Path(f"data/{location}_coa.csv"),
        Path(f"/opt/red-nun-dashboard/data/{location}_coa.csv"),
        Path(f"/home/rednun/{location}_coa.csv"),
        Path.home() / f"{location}_coa.csv",
    ]
    # Only include real files (skip blank/missing entries safely).
    return [p for p in paths if p and str(p) and p.is_file()]


def _coa_seed_paths_unscoped() -> list[Path]:
    """Legacy: a single coa.csv used before per-location was a thing. Loads
    as 'dennis' since that's where the existing dennis_coa.csv came from."""
    paths = []
    env_path = os.environ.get("COA_CSV_PATH")
    if env_path:
        paths.append(Path(env_path))
    paths.append(Path("data/coa.csv"))
    paths.append(Path("/opt/red-nun-dashboard/data/coa.csv"))
    # Reject anything that's not a regular file (e.g. an empty string that
    # resolves to the current directory).
    return [p for p in paths if p and str(p) and p.is_file()]


def seed_gl_accounts_if_empty() -> int:
    """If gl_accounts has no rows for a given location, populate it from the
    matching CSV. Returns total accounts inserted across both locations.

    CSV format (matches integrations/quickbooks/qb_list_accounts.py output
    and the existing dennis_coa.csv):
        AcctNum,ID,Name,AccountType,AccountSubType,Active

    Files looked for, per location:
      - data/chatham_coa.csv   → location=chatham
      - data/dennis_coa.csv    → location=dennis
      - /home/rednun/<loc>_coa.csv (legacy fallback)
    """
    inserted_total = 0

    for loc in ("dennis", "chatham"):  # seed dennis first so chatham can fall back to it
        conn = get_connection()
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM gl_accounts WHERE location = ?", (loc,)
        ).fetchone()["c"]
        if n > 0:
            conn.close()
            continue
        # Find the first real file in this location's search path.
        loc_paths = _coa_seed_paths_for(loc)
        path = loc_paths[0] if loc_paths else None
        # Fallback chain when location-specific CSV is missing:
        #   1. unscoped data/coa.csv (committed)
        #   2. dennis_coa.csv (Beelink legacy — same content used for both
        #      locations until a separate chatham_coa.csv is provided).
        if not path:
            unscoped = _coa_seed_paths_unscoped()
            path = unscoped[0] if unscoped else None
        if not path:
            dennis_paths = _coa_seed_paths_for("dennis")
            path = dennis_paths[0] if dennis_paths else None
        if not path:
            conn.close()
            logger.info(
                "No COA CSV found for %s. Drop a CSV at data/%s_coa.csv "
                "or data/coa.csv to seed.", loc, loc,
            )
            continue
        inserted_total += _seed_one_csv(conn, path, loc)
        conn.close()

    return inserted_total


def _seed_one_csv(conn, path: Path, location: str) -> int:
    """Read a CSV and insert its rows scoped to `location`."""
    inserted = 0
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("Name") or "").strip()
                if not name:
                    continue
                qbo_id = (row.get("ID") or "").strip() or None
                acct_num = (row.get("AcctNum") or "").strip() or None
                acct_type = (row.get("AccountType") or "").strip() or None
                acct_sub = (row.get("AccountSubType") or "").strip() or None
                active_yes = (row.get("Active") or "Yes").strip().lower()
                active = 1 if active_yes in ("yes", "true", "1", "y") else 0
                try:
                    conn.execute(
                        """INSERT INTO gl_accounts
                           (qbo_id, acct_num, name, account_type, account_subtype, active, location)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (qbo_id, acct_num, name, acct_type, acct_sub, active, location),
                    )
                    inserted += 1
                except Exception as e:
                    logger.warning(f"Skip GL row {name!r} ({location}): {e}")
        conn.commit()
        logger.info(f"Seeded {inserted} GL accounts for {location} from {path}")
    except Exception as e:
        logger.warning(f"Failed to seed GL accounts from {path}: {e}")
    return inserted


# ─── GL ACCOUNT API ──────────────────────────────────────────────────────────

# Generic banking-noise words that should NOT become rule patterns. The
# remainder of a description tokens defines the rule (so "DEP JAN 04 TOAST"
# → rule "TOAST"; "Deposit" → rule "DEPOSIT"; "DAVO TECHNOLOGIE 412.54" →
# rule "DAVO TECHNOLOGIE").
_RULE_SKIP_WORDS = {
    "DEP", "ACH", "CCD", "PPD", "DBT", "CRD", "POS", "WEB", "INST", "XFER",
    "SALE", "PAYMENT", "BILLPAY", "MEBILLPAY", "EFT", "TRANSFER", "TXN",
    "CHECK", "CHK", "REC", "DDA", "PMT", "AGNT", "AR", "VENDOR", "PAY",
    "INVOICES", "SVCS", "HORIZON", "BE", "AP", "INV", "PCR", "TAX",
    # Date words
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
}


def _extract_rule_pattern(description: str) -> str:
    """Pick a stable, distinctive substring from a transaction description to
    use as an auto-populate rule key.

    Strategy: take alphabetic tokens of length >= 3, drop generic banking
    prefix words and dates, keep the first 2 remaining significant tokens.
    Falls back to the original (uppercased, trimmed) description if no
    significant tokens remain.
    """
    if not description:
        return ""
    words = re.findall(r"[A-Za-z]{3,}", description.upper())
    significant = [w for w in words if w not in _RULE_SKIP_WORDS]
    if significant:
        return " ".join(significant[:2])
    if words:
        return " ".join(words[:2])
    return description.strip()[:40].upper()


def _find_gl_account_for_description(conn, description: str, location: str | None = None) -> int | None:
    """Find a gl_account_id whose rule pattern appears (case-insensitive
    substring) in `description`. Longest pattern wins on ties.

    If `location` is supplied, only rules scoped to that location (or with
    NULL location, meaning "any") are considered.
    """
    if not description:
        return None
    upper = description.upper()
    if location:
        rows = conn.execute(
            "SELECT pattern, gl_account_id FROM gl_account_rules "
            "WHERE location = ? OR location IS NULL "
            "ORDER BY LENGTH(pattern) DESC",
            (location,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pattern, gl_account_id FROM gl_account_rules "
            "ORDER BY LENGTH(pattern) DESC"
        ).fetchall()
    for r in rows:
        if r["pattern"] and r["pattern"].upper() in upper:
            return r["gl_account_id"]
    return None


@register_bp.route("/api/gl-accounts", methods=["GET"])
@login_required
def list_gl_accounts():
    """List GL accounts for the dropdown.

    Query params:
        active: 1|0|all  (default 1)
        type:   filter by account_type (e.g. "Expense"). Multiple via ?type=X&type=Y.

    Response shape:
      {"accounts": [{ id, qbo_id, name, account_type, account_subtype, active }, ...]}
    """
    active = request.args.get("active", "1")
    types = request.args.getlist("type")
    location = request.args.get("location")  # chatham | dennis | None (all)

    where = []
    params: list = []
    if active != "all":
        where.append("active = ?")
        params.append(1 if active == "1" else 0)
    if types:
        placeholders = ",".join("?" for _ in types)
        where.append(f"account_type IN ({placeholders})")
        params.extend(types)
    if location:
        # Show this location's accounts AND any unscoped (legacy NULL) ones.
        where.append("(location = ? OR location IS NULL)")
        params.append(location)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection()
    rows = conn.execute(
        f"""SELECT id, qbo_id, acct_num, name, account_type, account_subtype, location, active
            FROM gl_accounts {where_sql}
            ORDER BY account_type, name"""
        , params
    ).fetchall()
    conn.close()
    return jsonify({"accounts": [dict(r) for r in rows]})


@register_bp.route("/api/gl-accounts", methods=["POST"])
@login_required
def create_gl_account():
    """Add a new GL account manually (when one isn't already in the seeded COA).

    Body: {
        "name":           "Roof Repairs",        // required
        "account_type":   "Expense",             // optional but recommended
        "account_subtype": "RepairMaintenance",  // optional
        "location":       "chatham" | "dennis" | null,  // null = visible to both
        "qbo_id":         "300"                  // optional
    }
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    account_type = (data.get("account_type") or "").strip() or None
    account_subtype = (data.get("account_subtype") or "").strip() or None
    location = (data.get("location") or "").strip().lower() or None
    if location and location not in ("chatham", "dennis"):
        return jsonify({"error": "location must be 'chatham', 'dennis', or null"}), 400
    qbo_id = (data.get("qbo_id") or "").strip() or None

    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO gl_accounts
           (qbo_id, name, account_type, account_subtype, location, active)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (qbo_id, name, account_type, account_subtype, location),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"status": "ok", "id": new_id}), 201


@register_bp.route("/api/gl-accounts/<int:gl_id>", methods=["PUT"])
@login_required
def update_gl_account(gl_id):
    """Edit a GL account (rename, change type, deactivate, etc.)."""
    data = request.get_json(silent=True) or {}
    allowed = {"name", "account_type", "account_subtype", "location", "active", "qbo_id", "acct_num"}
    sets = {k: data[k] for k in data if k in allowed}
    if not sets:
        return jsonify({"error": "No updatable fields"}), 400
    conn = get_connection()
    cur = conn.execute(
        f"UPDATE gl_accounts SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
        list(sets.values()) + [gl_id],
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "GL account not found"}), 404
    return jsonify({"status": "ok"})


@register_bp.route("/api/gl-accounts/refresh-from-csv", methods=["POST"])
@admin_required
def refresh_gl_from_csv():
    """Force a re-seed from the COA CSV (useful after editing the file or
    after the table is wiped). Skipped when the table already has rows
    unless ?force=1 is passed (which TRUNCATEs first)."""
    force = request.args.get("force", "0") == "1"
    conn = get_connection()
    if force:
        conn.execute("DELETE FROM gl_accounts")
        conn.commit()
    conn.close()
    inserted = seed_gl_accounts_if_empty()
    return jsonify({"status": "ok", "inserted": inserted})


@register_bp.route("/api/register/row/gl-account", methods=["PUT"])
@login_required
def set_row_gl_account():
    """Assign a GL account to a single register row. Auto-creates a rule
    keyed on the row's description and back-fills any other unassigned rows
    matching the same rule.

    Body: {
        "source": "bill_pay" | "payroll" | "deposit" | "manual",
        "id":     int,
        "gl_account_id": int | null,        // null clears the assignment
        "create_rule": true                 // default true; pass false to skip rule + back-fill
    }
    """
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    row_id = data.get("id")
    gl_id = data.get("gl_account_id")
    create_rule = data.get("create_rule", True)

    table = _TABLE_BY_SOURCE.get(source)
    if not table or not isinstance(row_id, int):
        return jsonify({"error": "Bad source or id"}), 400
    if gl_id is not None and not isinstance(gl_id, int):
        return jsonify({"error": "gl_account_id must be an integer or null"}), 400

    conn = get_connection()

    # Validate the GL account
    if gl_id is not None:
        gl = conn.execute("SELECT id, name FROM gl_accounts WHERE id = ?", (gl_id,)).fetchone()
        if not gl:
            conn.close()
            return jsonify({"error": "GL account not found"}), 404

    # Update the row
    cur = conn.execute(f"UPDATE {table} SET gl_account_id = ? WHERE id = ?", (gl_id, row_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "Row not found"}), 404

    backfilled = 0
    rule_pattern: str | None = None

    if gl_id is not None and create_rule:
        # Pull the description from the row to extract a rule key, and figure
        # out which location this row belongs to so the rule is scoped right.
        desc_col, loc_col = {
            "bill_pay": ("vendor", "location"),
            "payroll":  ("employee_name", "location"),
            "deposit":  ("description", None),
            "manual":   ("payee", None),
        }[source]

        row = conn.execute(
            f"SELECT {desc_col} AS desc, bank_account_id FROM {table} WHERE id = ?",
            (row_id,),
        ).fetchone()
        desc = (row["desc"] if row else None) or ""

        # For manual entries (statement-imported), the memo often contains the
        # original transaction description with stronger signal than payee.
        if source == "manual":
            row2 = conn.execute(
                "SELECT payee, memo, bank_account_id FROM manual_bank_entries WHERE id = ?",
                (row_id,),
            ).fetchone()
            desc = (row2["payee"] or "") + " " + (row2["memo"] or "")

        # Resolve the location via bank_account_id (every register-source row
        # carries one). Falls back to None if unset (NULL = unassigned bill_pay).
        loc = None
        if row and row["bank_account_id"]:
            ba = conn.execute(
                "SELECT location FROM bank_accounts WHERE id = ?", (row["bank_account_id"],)
            ).fetchone()
            if ba:
                loc = ba["location"]

        rule_pattern = _extract_rule_pattern(desc)
        if rule_pattern:
            from flask import session
            who = session.get("username") or session.get("email") or "unknown"
            try:
                conn.execute(
                    """INSERT INTO gl_account_rules (location, pattern, gl_account_id, created_by)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(location, pattern) DO UPDATE SET
                         gl_account_id = excluded.gl_account_id,
                         created_by = excluded.created_by,
                         created_at = CURRENT_TIMESTAMP""",
                    (loc, rule_pattern, gl_id, who),
                )
            except Exception as e:
                logger.warning(f"Could not write GL rule {rule_pattern!r} for {loc}: {e}")

            # Back-fill all unassigned rows whose description matches this pattern,
            # scoped to the same location (so a Chatham rule doesn't auto-code Dennis).
            backfilled = _backfill_unassigned_for_pattern(conn, rule_pattern, gl_id, loc)

    conn.commit()
    conn.close()
    return jsonify({
        "status": "ok",
        "rule_pattern": rule_pattern,
        "backfilled": backfilled,
    })


def _backfill_unassigned_for_pattern(conn, pattern: str, gl_id: int, location: str | None) -> int:
    """For every register-source table, set gl_account_id=<gl_id> on rows
    that (a) match `pattern` case-insensitively, (b) currently have NULL
    gl_account_id, and (c) belong to a bank_account in `location` (if given).

    Scoping by location keeps a Chatham rule from accidentally coding Dennis
    rows to a Chatham GL account.
    """
    if not pattern:
        return 0
    upper_like = f"%{pattern.upper()}%"

    # Resolve the set of bank_account_ids that belong to this location.
    if location:
        loc_acct_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM bank_accounts WHERE location = ?", (location,)
            ).fetchall()
        ]
    else:
        loc_acct_ids = []

    if location and not loc_acct_ids:
        # No bank accounts for this location — nothing to back-fill.
        return 0

    loc_clause = ""
    loc_params: tuple = ()
    if location and loc_acct_ids:
        ph = ",".join("?" for _ in loc_acct_ids)
        loc_clause = f" AND bank_account_id IN ({ph})"
        loc_params = tuple(loc_acct_ids)

    queries = [
        ("vendor_payments",
         f"UPDATE vendor_payments SET gl_account_id = ? WHERE gl_account_id IS NULL "
         f"AND UPPER(vendor) LIKE ?{loc_clause}",
         (gl_id, upper_like) + loc_params),
        ("payroll_checks",
         f"UPDATE payroll_checks SET gl_account_id = ? WHERE gl_account_id IS NULL "
         f"AND UPPER(employee_name) LIKE ?{loc_clause}",
         (gl_id, upper_like) + loc_params),
        ("bank_deposits",
         f"UPDATE bank_deposits SET gl_account_id = ? WHERE gl_account_id IS NULL "
         f"AND (UPPER(COALESCE(description,'')) LIKE ? OR UPPER(COALESCE(memo,'')) LIKE ?)"
         f"{loc_clause}",
         (gl_id, upper_like, upper_like) + loc_params),
        ("manual_bank_entries",
         f"UPDATE manual_bank_entries SET gl_account_id = ? WHERE gl_account_id IS NULL "
         f"AND (UPPER(COALESCE(payee,'')) LIKE ? OR UPPER(COALESCE(memo,'')) LIKE ?)"
         f"{loc_clause}",
         (gl_id, upper_like, upper_like) + loc_params),
    ]
    total = 0
    for table, q, params in queries:
        try:
            cur = conn.execute(q, params)
            total += cur.rowcount or 0
        except Exception as e:
            logger.warning(f"Backfill on {table} failed: {e}")
    return total


# ─── ACCOUNT CRUD ────────────────────────────────────────────────────────────

@register_bp.route("/api/register/accounts", methods=["GET"])
@login_required
def list_accounts():
    """List all active bank accounts."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, name, short_name, qbo_account_id, qbo_account_name,
                  location, account_last4, opening_balance, opening_date,
                  active, sort_order
           FROM bank_accounts
           WHERE active = 1
           ORDER BY sort_order, name"""
    ).fetchall()
    conn.close()
    return jsonify({"accounts": [dict(r) for r in rows]})


@register_bp.route("/api/register/accounts/<int:account_id>", methods=["PUT"])
@admin_required
def update_account(account_id):
    """Update an account's QBO id, opening balance, etc."""
    data = request.get_json() or {}
    allowed = {"name", "short_name", "qbo_account_id", "qbo_account_name",
               "location", "opening_balance", "opening_date", "active", "sort_order"}
    sets = {k: data[k] for k in data if k in allowed}
    if not sets:
        return jsonify({"error": "No updatable fields supplied"}), 400

    conn = get_connection()
    cur = conn.execute(
        f"UPDATE bank_accounts SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
        list(sets.values()) + [account_id],
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Account not found"}), 404
    return jsonify({"status": "ok"})


# ─── UNIFIED REGISTER QUERY ──────────────────────────────────────────────────

def _normalize_row(source, r, bank_account_id):
    """Normalize a row from any of the four sources into the register shape.

    Register shape:
      date       YYYY-MM-DD
      type       bill_pay | payroll | deposit | manual
      ref        display ref (check #, ACH ref, etc.)
      payee      vendor / employee / description
      memo
      outflow    float (positive = money leaving bank)
      inflow     float (positive = money into bank)
      status     pending | printed | cleared | void | unassigned
      cleared    0 | 1
      source_id  primary key of the source row (for drill-in)
      unassigned bool — True if bill pay row has NULL bank_account_id
    """
    out = {
        "source": source,
        "bank_account_id": bank_account_id,
        "unassigned": False,
    }
    # GL account fields (present on all four sources after the migration)
    try:
        gl_id = r["gl_account_id"]
    except (IndexError, KeyError):
        gl_id = None
    out["gl_account_id"] = gl_id
    if source == "bill_pay":
        # vendor_payments
        ref = r["payment_ref"] or (f"CHK-{r['check_number']}" if r["check_number"] else "")
        # If it's ACH (no check number), prefer reference_number via ap_payments if present.
        # vendor_payments doesn't carry reference_number directly — the ref falls back to
        # payment_ref. For ACH entries, surface method.
        method = (r["payment_method"] or "").lower()
        if method in ("ach", "eft", "wire") and not r["check_number"]:
            # Try to reach through to ap_payments.reference_number
            ref = r["payment_ref"]  # already something like CHK-AP123 or vendor-supplied
        out.update({
            "date": r["payment_date"],
            "type": "bill_pay",
            "type_label": "Check" if r["check_number"] else (method.upper() or "Bill Pay"),
            "ref": ref,
            "payee": r["vendor"],
            "memo": r["memo"],
            "outflow": float(r["payment_total"] or 0),
            "inflow": 0.0,
            "status": "void" if r["status"] == "void" else r["status"],
            "cleared": int(r["cleared"] or 0) if "cleared" in r.keys() else 0,
            "source_id": r["id"],
            "ap_payment_id": r["ap_payment_id"],
            "unassigned": bank_account_id is None,
        })
    elif source == "payroll":
        # payroll_checks
        out.update({
            "date": r["pay_date"] or r["pay_period_end"],
            "type": "payroll",
            "type_label": "Payroll",
            "ref": f"PR-{r['check_number']}" if r["check_number"] else f"PR-{r['id']}",
            "payee": r["employee_name"],
            "memo": f"Payroll {r['pay_period_start']}–{r['pay_period_end']}",
            "outflow": float(r["net_pay"] or 0),
            "inflow": 0.0,
            "status": "void" if r["voided"] else (r["status"] or "pending"),
            "cleared": int(r["cleared"] or 0),
            "source_id": r["id"],
            "payroll_run_id": r["payroll_run_id"],
        })
    elif source == "deposit":
        # bank_deposits
        out.update({
            "date": r["deposit_date"],
            "type": "deposit",
            "type_label": "Deposit" if r["source"] != "toast" else "Toast Settlement",
            "ref": r["qbo_txn_id"] or "",
            "payee": r["description"] or "Deposit",
            "memo": r["memo"],
            "outflow": 0.0,
            "inflow": float(r["amount"] or 0),
            "status": "cleared",
            "cleared": int(r["cleared"] or 0),
            "source_id": r["id"],
        })
    elif source == "manual":
        # manual_bank_entries
        amt = float(r["amount"] or 0)
        out.update({
            "date": r["entry_date"],
            "type": "manual",
            "type_label": (r["entry_type"] or "Manual").title(),
            "ref": r["ref_number"] or "",
            "payee": r["payee"] or "",
            "memo": r["memo"],
            "outflow": -amt if amt < 0 else 0.0,
            "inflow": amt if amt > 0 else 0.0,
            "status": "cleared" if r["cleared"] else "pending",
            "cleared": int(r["cleared"] or 0),
            "source_id": r["id"],
        })
    return out


@register_bp.route("/api/register/<int:account_id>", methods=["GET"])
@login_required
def get_register(account_id):
    """Return the unified register for a bank account.

    Query params:
      start      YYYY-MM-DD (default: 90 days ago)
      end        YYYY-MM-DD (default: today)
      cleared    all | cleared | uncleared (default: all)
      include_unassigned  1 | 0 (default: 1 — show bill pay with NULL account on Chatham)
    """
    conn = get_connection()
    account = conn.execute(
        "SELECT * FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if not account:
        conn.close()
        return jsonify({"error": "Account not found"}), 404

    # Date range
    today = date.today()
    end = request.args.get("end") or today.strftime("%Y-%m-%d")
    start = request.args.get("start") or (today - timedelta(days=90)).strftime("%Y-%m-%d")
    cleared_filter = request.args.get("cleared", "all")
    include_unassigned = request.args.get("include_unassigned", "1") == "1"

    # For vendor_payments, payment_date is stored as YYYY-MM-DD (per billpay INSERT);
    # for payroll_checks, pay_date may be YYYY-MM-DD too. For bank_deposits it's YYYY-MM-DD.
    rows = []

    # ── bill pay ──
    # vendor_payments has bank_account_id (new column). Pull rows matching this
    # account, plus NULL ones if this is the "default" account (Chatham) and
    # include_unassigned is on.
    is_default_account = account["account_last4"] == "5975"  # Chatham = catch-all for unassigned
    bp_query = (
        "SELECT id, vendor, location, payment_date, payment_ref, payment_method, "
        "payment_total, check_number, memo, status, ap_payment_id, "
        "bank_account_id, cleared, cleared_date, gl_account_id "
        "FROM vendor_payments WHERE payment_date >= ? AND payment_date <= ? AND "
        "(status IS NULL OR status != 'void') AND ("
        "bank_account_id = ?"
        + (" OR bank_account_id IS NULL" if (is_default_account and include_unassigned) else "")
        + ")"
    )
    for r in conn.execute(bp_query, (start, end, account_id)).fetchall():
        rows.append(_normalize_row("bill_pay", r, r["bank_account_id"] or (account_id if is_default_account else None)))

    # ── payroll ──
    pr_query = (
        "SELECT id, employee_name, check_number, net_pay, gross_pay, location, "
        "payroll_run_id, pay_period_start, pay_period_end, payment_method, status, "
        "voided, bank_account_id, cleared, cleared_date, gl_account_id, "
        "COALESCE(pay_date, pay_period_end) AS pay_date "
        "FROM payroll_checks WHERE "
        "COALESCE(pay_date, pay_period_end) >= ? AND "
        "COALESCE(pay_date, pay_period_end) <= ? AND "
        "(voided IS NULL OR voided = 0) AND "
        "bank_account_id = ?"
    )
    try:
        for r in conn.execute(pr_query, (start, end, account_id)).fetchall():
            rows.append(_normalize_row("payroll", r, account_id))
    except Exception as e:
        logger.warning(f"payroll query failed (payroll_checks may not exist yet): {e}")

    # ── deposits ──
    dep_query = (
        "SELECT id, bank_account_id, deposit_date, amount, description, memo, "
        "source, qbo_txn_id, qbo_txn_type, cleared, cleared_date, gl_account_id "
        "FROM bank_deposits WHERE bank_account_id = ? AND "
        "deposit_date >= ? AND deposit_date <= ?"
    )
    for r in conn.execute(dep_query, (account_id, start, end)).fetchall():
        rows.append(_normalize_row("deposit", r, account_id))

    # ── manual ──
    man_query = (
        "SELECT id, bank_account_id, entry_date, entry_type, payee, memo, "
        "ref_number, amount, cleared, cleared_date, gl_account_id "
        "FROM manual_bank_entries WHERE bank_account_id = ? AND "
        "entry_date >= ? AND entry_date <= ?"
    )
    for r in conn.execute(man_query, (account_id, start, end)).fetchall():
        rows.append(_normalize_row("manual", r, account_id))

    # Resolve GL account names in one shot (avoid N+1 lookups)
    needed = {r.get("gl_account_id") for r in rows if r.get("gl_account_id")}
    gl_name_by_id: dict[int, str] = {}
    if needed:
        ph = ",".join("?" for _ in needed)
        for g in conn.execute(
            f"SELECT id, name FROM gl_accounts WHERE id IN ({ph})", list(needed)
        ).fetchall():
            gl_name_by_id[g["id"]] = g["name"]
    for r in rows:
        gid = r.get("gl_account_id")
        r["gl_account_name"] = gl_name_by_id.get(gid) if gid else None

    conn.close()

    # Filter by cleared state
    if cleared_filter == "cleared":
        rows = [r for r in rows if r["cleared"]]
    elif cleared_filter == "uncleared":
        rows = [r for r in rows if not r["cleared"]]

    # Sort ascending by date to compute running balance, then reverse for display
    rows.sort(key=lambda r: (r["date"] or "", r["source"], r["source_id"]))

    # Running balance: starts from opening_balance at opening_date (or 0 if unset).
    # Anything dated before opening_date is ignored for balance purposes.
    opening_bal = float(account["opening_balance"] or 0)
    running = opening_bal
    for r in rows:
        running += r["inflow"] - r["outflow"]
        r["balance"] = round(running, 2)

    # Return newest first for display
    rows.reverse()

    # Summary totals (signed)
    total_in = sum(r["inflow"] for r in rows)
    total_out = sum(r["outflow"] for r in rows)
    unassigned_count = sum(1 for r in rows if r.get("unassigned"))
    uncleared_count = sum(1 for r in rows if not r["cleared"])

    return jsonify({
        "account": dict(account),
        "start": start,
        "end": end,
        "rows": rows,
        "summary": {
            "total_inflow": round(total_in, 2),
            "total_outflow": round(total_out, 2),
            "net": round(total_in - total_out, 2),
            "opening_balance": round(opening_bal, 2),
            "ending_balance": round(running, 2),
            "row_count": len(rows),
            "unassigned_count": unassigned_count,
            "uncleared_count": uncleared_count,
        },
    })


# ─── MANUAL ENTRY CRUD ───────────────────────────────────────────────────────

@register_bp.route("/api/register/<int:account_id>/manual", methods=["POST"])
@login_required
def create_manual_entry(account_id):
    data = request.get_json() or {}
    required = ("entry_date", "entry_type", "amount")
    missing = [k for k in required if data.get(k) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    try:
        amount = float(data["amount"])
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number (signed)"}), 400

    conn = get_connection()
    acct = conn.execute("SELECT id FROM bank_accounts WHERE id = ?", (account_id,)).fetchone()
    if not acct:
        conn.close()
        return jsonify({"error": "Account not found"}), 404

    from flask import session
    created_by = session.get("username") or session.get("email") or "unknown"

    cur = conn.execute(
        """INSERT INTO manual_bank_entries
           (bank_account_id, entry_date, entry_type, payee, memo, ref_number,
            amount, cleared, cleared_date, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            account_id,
            data["entry_date"],
            data["entry_type"],
            data.get("payee"),
            data.get("memo"),
            data.get("ref_number"),
            amount,
            1 if data.get("cleared") else 0,
            data.get("cleared_date"),
            created_by,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"status": "ok", "id": new_id}), 201


@register_bp.route("/api/register/manual/<int:entry_id>", methods=["DELETE"])
@admin_required
def delete_manual_entry(entry_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM manual_bank_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Entry not found"}), 404
    return jsonify({"status": "ok"})


# ─── CLEARED / UNCLEARED TOGGLE ──────────────────────────────────────────────

_TABLE_BY_SOURCE = {
    "bill_pay": "vendor_payments",
    "payroll":  "payroll_checks",
    "deposit":  "bank_deposits",
    "manual":   "manual_bank_entries",
}


@register_bp.route("/api/register/row/cleared", methods=["PUT"])
@login_required
def set_cleared():
    """Toggle the cleared flag on a single register row.

    Body: {"source": "bill_pay|payroll|deposit|manual", "id": 123, "cleared": true}
    """
    data = request.get_json() or {}
    source = data.get("source")
    row_id = data.get("id")
    cleared = 1 if data.get("cleared") else 0

    if source not in _TABLE_BY_SOURCE or not isinstance(row_id, int):
        return jsonify({"error": "Bad source or id"}), 400

    table = _TABLE_BY_SOURCE[source]
    cleared_date = datetime.now().strftime("%Y-%m-%d") if cleared else None
    conn = get_connection()
    cur = conn.execute(
        f"UPDATE {table} SET cleared = ?, cleared_date = ? WHERE id = ?",
        (cleared, cleared_date, row_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Row not found"}), 404
    return jsonify({"status": "ok"})


# ─── REASSIGN BILL PAY ROW TO AN ACCOUNT ─────────────────────────────────────

@register_bp.route("/api/register/reassign", methods=["PUT"])
@login_required
def reassign_row():
    """Assign (or change) the bank account for a bill pay or payroll row.

    Body: {"source": "bill_pay|payroll", "id": 123, "bank_account_id": 2}
    """
    data = request.get_json() or {}
    source = data.get("source")
    row_id = data.get("id")
    new_acct = data.get("bank_account_id")
    if source not in ("bill_pay", "payroll") or not isinstance(row_id, int):
        return jsonify({"error": "Bad source or id"}), 400

    table = _TABLE_BY_SOURCE[source]
    conn = get_connection()
    if new_acct is not None:
        acct = conn.execute("SELECT id FROM bank_accounts WHERE id = ?", (new_acct,)).fetchone()
        if not acct:
            conn.close()
            return jsonify({"error": "Target account not found"}), 404
    cur = conn.execute(
        f"UPDATE {table} SET bank_account_id = ? WHERE id = ?",
        (new_acct, row_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Row not found"}), 404
    return jsonify({"status": "ok"})


# ─── QBO DEPOSIT SYNC (user-triggered) ───────────────────────────────────────

@register_bp.route("/api/register/<int:account_id>/sync-deposits", methods=["POST"])
@login_required
def sync_deposits(account_id):
    """Pull deposits from QBO for this bank account and cache them locally.

    Uses the same GeneralLedger report mechanics as
    integrations/quickbooks/qb_query_register.py. Only triggered by explicit
    user action (button click) per the "no silent API spend" rule in CLAUDE.md.

    Body: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}  (optional, defaults to last 90d)
    """
    data = request.get_json() or {}
    today = date.today()
    start = data.get("start") or (today - timedelta(days=90)).strftime("%Y-%m-%d")
    end = data.get("end") or today.strftime("%Y-%m-%d")

    conn = get_connection()
    account = conn.execute(
        "SELECT * FROM bank_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if not account:
        conn.close()
        return jsonify({"error": "Account not found"}), 404
    if not account["qbo_account_id"]:
        conn.close()
        return jsonify({
            "error": "Account has no qbo_account_id set. Update the account first.",
        }), 400

    # Lazy-import QBO helpers so a broken QBO token doesn't break the whole app startup.
    try:
        import sys
        from pathlib import Path
        qb_dir = Path(__file__).resolve().parent.parent / "integrations" / "quickbooks"
        sys.path.insert(0, str(qb_dir))
        from qb_push import get_credentials, get_realm_id, get_valid_token  # noqa: E402
        from qb_query_register import fetch_transaction_list  # noqa: E402
    except Exception as e:
        conn.close()
        logger.exception("QBO helper import failed")
        return jsonify({"error": f"QBO helpers unavailable: {e}"}), 500

    try:
        client_id, client_secret, _ = get_credentials()
        realm_id = get_realm_id(client_id, client_secret)
        token = get_valid_token(client_id, client_secret)
        qbo_rows = fetch_transaction_list(
            realm_id, token, account["qbo_account_id"], start, end
        )
    except Exception as e:
        conn.close()
        logger.exception("QBO fetch failed")
        return jsonify({"error": f"QBO fetch failed: {e}"}), 502

    def fnum(v):
        try:
            return float(str(v).replace(",", "")) if v not in ("", None) else 0.0
        except ValueError:
            return 0.0

    inserted = 0
    updated = 0
    for r in qbo_rows:
        # Skip summary / header rows
        d = (r.get("Date") or "").strip()
        if not d or d.startswith(("Beginning", "Total", "Ending")):
            continue

        amt = fnum(r.get("Amount"))
        # Only want deposits (money INTO bank): positive amount on a bank account.
        if amt <= 0:
            continue

        txn_type = (r.get("Transaction Type") or "").strip()
        name = (r.get("Name") or "").strip()
        memo = (r.get("Memo/Description") or "").strip()
        num = (r.get("Num") or "").strip()

        # QBO report rows don't have a stable unique id; Num + Date + Amount + Type
        # is the best cheap dedup key.
        txn_id = r.get("Date_id") or f"{txn_type}|{num}|{d}|{amt:.2f}|{name}"[:200]

        # Classify
        if "toast" in (name + " " + memo).lower():
            src = "toast"
        elif "cash" in (name + " " + memo).lower():
            src = "cash"
        else:
            src = "qbo_other"

        existing = conn.execute(
            "SELECT id FROM bank_deposits WHERE qbo_txn_id = ?", (txn_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE bank_deposits SET
                   deposit_date = ?, amount = ?, description = ?, memo = ?,
                   source = ?, qbo_txn_type = ?, synced_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (d, amt, name or "Deposit", memo, src, txn_type, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO bank_deposits
                   (bank_account_id, deposit_date, amount, description, memo,
                    source, qbo_txn_id, qbo_txn_type, cleared, cleared_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (account_id, d, amt, name or "Deposit", memo, src, txn_id, txn_type, d),
            )
            inserted += 1

    conn.commit()
    conn.close()
    return jsonify({
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "start": start,
        "end": end,
    })


# ─── PAGE ROUTE ──────────────────────────────────────────────────────────────

# The HTML page is served by web/server.py (for consistency with other pages
# that use send_from_directory). This blueprint only owns the /api/register/*
# namespace.
