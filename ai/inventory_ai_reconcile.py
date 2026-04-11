"""
AI Inventory Reconciliation Engine
===================================
Merges audio and vision streams, cross-references purchase history,
creates draft sessions in the DB, handles manager confirmation, and
bridges confirmed AI counts back into the manual count system.

Functions
---------
reconcile_streams(audio_items, vision_items)   -> list
cross_reference_history(items, location)       -> list
create_draft_session(location, audio_path,
                     video_path, transcript,
                     items)                    -> int  (session_id)
confirm_session(session_id, confirmed_items)   -> bool
process_inventory(file_path, location)         -> int  (session_id)
"""

import logging
import os
import re
from datetime import datetime, timezone

from data_store import get_connection

# ── Logging setup ─────────────────────────────────────────────────────────────
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
# ─────────────────────────────────────────────────────────────────────────────

# Agreement threshold: quantities within this fraction are considered matching.
# e.g. 0.10 means audio=3.0 and vision=3.2 agree (within 10%).
_AGREE_THRESHOLD = 0.10

# Confidence values assigned during reconciliation
_CONF_AGREE  = 0.95   # both streams match
_CONF_AUDIO  = 0.70   # audio only (video had no match)
_CONF_VISION = 0.60   # vision only (audio had no match) — reviewed before confirm
_CONF_CONFLICT = 0.50 # both present but disagree — audio trusted, flagged for review

# Location name → storage_locations.location column value
_LOCATION_TO_DB = {
    "chatham":     "chatham",
    "dennis":      "dennis",
    "dennis port": "dennis",
    "dennisport":  "dennis",
}


def _normalise(name: str) -> str:
    """Lowercase + collapse whitespace — used to group items across streams."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _strip_suffixes(name: str) -> str:
    """Remove common product suffixes/prefixes for fuzzy matching across streams."""
    s = name.lower()
    # Remove possessives
    s = re.sub(r"[''']s?\b", "", s)
    # Remove brand prefixes (vision often adds the brand, audio often doesn't)
    for prefix in ["ken", "ken steak house", "hellmann", "heinz", "frank",
                    "saratoga", "hewitt farms", "sweet chili"]:
        s = re.sub(rf"^{re.escape(prefix)}\s+", "", s)
    # Remove category suffixes
    for suffix in [" dressing", " sauce", " dressing/sauce", " dressing sauce",
                   " condiment", " spread", " mix", " blend", " seasoning",
                   # Liquor suffixes
                   " rum", " vodka", " gin", " tequila", " whiskey",
                   " bourbon", " brandy", " liqueur", " beer", " ale", " lager",
                   " spiced", " original", " coconut", " white", " dark", " gold",
                   " silver", " black seal", " jamaican", " especial"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    # Remove leading adjectives
    for prefix_word in ["magic blend ", "real "]:
        if s.startswith(prefix_word):
            s = s[len(prefix_word):]
    return re.sub(r"\s+", " ", s.strip())


def _common_words(a: str, b: str) -> int:
    """Count shared words between two strings."""
    a_words = set(a.split())
    b_words = set(b.split())
    return len(a_words & b_words)


def _fuzzy_match_vision_to_audio(audio_by_name: dict, vision_by_name: dict) -> dict:
    """
    Build a mapping: vision_norm_name -> audio_norm_name for fuzzy matches.
    Only matches unmatched vision items to unmatched audio items.

    Uses three strategies:
    1. Substring containment after stripping suffixes/prefixes
    2. Shared-word matching (≥1 meaningful word in common after stripping)
    3. Known Whisper misspelling patterns
    """
    # Common Whisper misspellings → canonical form
    _WHISPER_FIXES = {
        "coltod": "coleslaw",
        "colton": "coleslaw",
        "coltlaw": "coleslaw",
        "gongju": "gochujang",
        "saracha": "sriracha",
        "siracha": "sriracha",
    }

    audio_names = set(audio_by_name.keys())
    vision_names = set(vision_by_name.keys())
    exact_matches = audio_names & vision_names
    unmatched_vision = vision_names - exact_matches
    unmatched_audio = audio_names - exact_matches

    mapping = {}  # vision_name -> audio_name

    for vname in unmatched_vision:
        v_stripped = _strip_suffixes(vname)
        best_match = None
        best_score = 0

        for aname in unmatched_audio:
            if aname in mapping.values():
                continue  # already claimed
            a_stripped = _strip_suffixes(aname)

            # Also try fixing Whisper misspellings in audio name
            a_fixed = a_stripped
            for typo, fix in _WHISPER_FIXES.items():
                a_fixed = a_fixed.replace(typo, fix)

            score = 0

            # Strategy 1: substring containment
            if a_stripped in v_stripped or v_stripped in a_stripped:
                score = min(len(a_stripped), len(v_stripped))
            elif a_fixed in v_stripped or v_stripped in a_fixed:
                score = min(len(a_fixed), len(v_stripped))

            # Strategy 2: shared words (at least 1 meaningful word ≥4 chars)
            if score == 0:
                shared = _common_words(v_stripped, a_stripped)
                if shared == 0:
                    shared = _common_words(v_stripped, a_fixed)
                # Only count if a shared word is ≥4 chars (skip "the", "and", etc.)
                v_words = set(v_stripped.split())
                a_words = set(a_stripped.split()) | set(a_fixed.split())
                meaningful_shared = [w for w in v_words & a_words if len(w) >= 4]
                if meaningful_shared:
                    score = sum(len(w) for w in meaningful_shared)

            if score > best_score and score >= 3:
                best_score = score
                best_match = aname

        if best_match:
            mapping[vname] = best_match

    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# 1. reconcile_streams
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_streams(audio_items: list, vision_items: list) -> list:
    """
    Merge audio and vision item lists into a single reconciled list.

    Rules
    -----
    Both agree (within 10%):
        quantity  = average of the two
        confidence = 0.95, flag = 'none'

    Both present but conflict (>10% difference):
        quantity  = audio value (audio is primary source)
        confidence = 0.50, flag = 'conflict'
        flag_notes = "audio={audio_qty} vision={vision_qty}"

    Audio only:
        quantity  = audio value
        confidence = 0.70, flag = 'none'

    Vision only:
        quantity  = vision value
        confidence = 0.60, flag = 'review'  (needs manager check — no verbal confirm)

    Parameters
    ----------
    audio_items  : list of shared-format dicts (source='audio')
    vision_items : list of shared-format dicts (source='vision')

    Returns
    -------
    list of dicts with all shared-format fields plus:
        audio_quantity, audio_confidence, vision_quantity, vision_confidence,
        reconciled_quantity, reconciled_confidence
    """
    # Build lookup by normalised product_name
    audio_by_name  = {_normalise(item["product_name"]): item for item in audio_items}
    vision_by_name = {_normalise(item["product_name"]): item for item in vision_items}

    # Fuzzy-match vision items to audio items that didn't match exactly
    fuzzy_map = _fuzzy_match_vision_to_audio(audio_by_name, vision_by_name)
    if fuzzy_map:
        logger.info("reconcile_streams: fuzzy matched %d vision→audio pairs: %s",
                     len(fuzzy_map),
                     ", ".join(f'"{v}"→"{a}"' for v, a in fuzzy_map.items()))
    # Merge fuzzy-matched vision items into their audio counterparts
    for vname, aname in fuzzy_map.items():
        # Move the vision data under the audio name so they reconcile together
        vision_by_name[aname] = vision_by_name.pop(vname)

    all_names = set(audio_by_name) | set(vision_by_name)
    reconciled = []

    for norm_name in sorted(all_names):
        a = audio_by_name.get(norm_name)
        v = vision_by_name.get(norm_name)

        # Start from whichever source has data (prefer audio as base)
        base = a if a is not None else v
        item = {
            "product_name":           base["product_name"],
            "product_id":             base.get("product_id"),
            "unit":                   base.get("unit", ""),
            "is_partial":             base.get("is_partial", False),
            "notes":                  base.get("notes", ""),
            "storage_location":       base.get("storage_location", ""),
            "storage_location_id":    base.get("storage_location_id"),
            # Raw per-engine values
            "audio_quantity":         a["quantity"] if a else None,
            "audio_confidence":       a.get("confidence") if a else None,
            "vision_quantity":        v["quantity"] if v else None,
            "vision_confidence":      v.get("confidence") if v else None,
        }

        if a and v:
            aq, vq = float(a["quantity"]), float(v["quantity"])
            # Compute relative difference; avoid division by zero
            denom = max(aq, vq, 0.001)
            rel_diff = abs(aq - vq) / denom

            if rel_diff <= _AGREE_THRESHOLD:
                # Streams agree — average the quantities
                item["reconciled_quantity"]   = round((aq + vq) / 2, 3)
                item["reconciled_confidence"] = _CONF_AGREE
                item["flag"]                  = "none"
                item["flag_notes"]            = ""
                # Merge notes from both if different
                notes_parts = [n for n in [a.get("notes",""), v.get("notes","")] if n]
                item["notes"] = "; ".join(dict.fromkeys(notes_parts))
            else:
                # Conflict — trust audio, flag for review
                item["reconciled_quantity"]   = aq
                item["reconciled_confidence"] = _CONF_CONFLICT
                item["flag"]                  = "conflict"
                item["flag_notes"]            = f"audio={aq} vision={vq}"

        elif a:
            # Audio only
            item["reconciled_quantity"]   = float(a["quantity"])
            item["reconciled_confidence"] = _CONF_AUDIO
            item["flag"]                  = "none"
            item["flag_notes"]            = ""

        else:
            # Vision only — flag for review
            item["reconciled_quantity"]   = float(v["quantity"])
            item["reconciled_confidence"] = _CONF_VISION
            item["flag"]                  = "review"
            item["flag_notes"]            = "vision only — no audio confirmation"

        reconciled.append(item)

    logger.info(
        "reconcile_streams: %d audio + %d vision → %d reconciled items "
        "(%d conflicts, %d review, %d none)",
        len(audio_items), len(vision_items), len(reconciled),
        sum(1 for i in reconciled if i["flag"] == "conflict"),
        sum(1 for i in reconciled if i["flag"] == "review"),
        sum(1 for i in reconciled if i["flag"] == "none"),
    )
    return reconciled


# ─────────────────────────────────────────────────────────────────────────────
# 2. cross_reference_history
# ─────────────────────────────────────────────────────────────────────────────

def cross_reference_history(items: list, location: str) -> list:
    """
    Enrich each reconciled item with purchase history and prior count data.

    For each item that has a matched product_id:
      - Look up total units purchased in the last 30 days (me_invoice_items)
      - Look up the most recent AI-confirmed quantity (ai_inventory_history)
      - If variance vs. theoretical or vs. previous count exceeds 20%, escalate flag

    Items without a product_id (new products) are returned unchanged.

    Parameters
    ----------
    items    : output of reconcile_streams()
    location : 'Chatham' or 'Dennis Port'  (full display name)

    Returns
    -------
    Same list with additional keys added to each item:
        recent_purchase_qty   (float | None)  — units received last 30 days
        last_count_qty        (float | None)  — last AI-confirmed count
        last_count_date       (str   | None)
        variance_vs_previous  (float | None)  — pct change vs last count
        variance_vs_purchases (float | None)  — pct of purchased qty on hand
    """
    db_location = _LOCATION_TO_DB.get(location.lower(), location.lower())

    conn = get_connection()
    try:
        enriched = []
        for item in items:
            pid = item.get("product_id")

            item["recent_purchase_qty"]   = None
            item["last_count_qty"]        = None
            item["last_count_date"]       = None
            item["variance_vs_previous"]  = None
            item["variance_vs_purchases"] = None

            if pid is None:
                enriched.append(item)
                continue

            # ── Purchase history (last 30 days) ───────────────────────────
            # Join me_invoice_items → me_invoices on order_id+location.
            # me_invoice_items has no product_id FK to product_inventory_settings,
            # so match by product_name LIKE (using the DB product_name as pattern).
            try:
                # Get the canonical product name from the catalog
                row = conn.execute(
                    "SELECT product_name FROM product_inventory_settings WHERE id = ?",
                    (pid,)
                ).fetchone()
                if row:
                    pname = row["product_name"]
                    purchases = conn.execute(
                        """
                        SELECT COALESCE(SUM(ii.quantity), 0) as total_qty
                        FROM me_invoice_items ii
                        JOIN me_invoices i
                          ON ii.order_id = i.order_id AND ii.location = i.location
                        WHERE i.invoice_date >= date('now', '-30 days')
                          AND i.location = ?
                          AND LOWER(ii.product_name) LIKE LOWER(?)
                        """,
                        (db_location, f"%{pname[:20]}%")
                    ).fetchone()
                    if purchases:
                        item["recent_purchase_qty"] = float(purchases["total_qty"])
            except Exception as exc:
                logger.warning("purchase lookup failed for product_id=%s: %s", pid, exc)

            # ── Prior AI count ─────────────────────────────────────────────
            try:
                prior = conn.execute(
                    """
                    SELECT quantity, counted_date
                    FROM ai_inventory_history
                    WHERE product_id = ?
                    ORDER BY counted_date DESC
                    LIMIT 1
                    """,
                    (pid,)
                ).fetchone()
                if prior:
                    item["last_count_qty"]  = float(prior["quantity"])
                    item["last_count_date"] = prior["counted_date"]
            except Exception as exc:
                logger.warning("history lookup failed for product_id=%s: %s", pid, exc)

            # ── Variance calculations ──────────────────────────────────────
            current_qty = item.get("reconciled_quantity")
            if current_qty is not None:
                # vs previous count
                if item["last_count_qty"] is not None and item["last_count_qty"] > 0:
                    item["variance_vs_previous"] = round(
                        (current_qty - item["last_count_qty"]) / item["last_count_qty"], 4
                    )

                # vs purchases (what fraction of purchased qty is still on hand)
                if item["recent_purchase_qty"] and item["recent_purchase_qty"] > 0:
                    item["variance_vs_purchases"] = round(
                        current_qty / item["recent_purchase_qty"], 4
                    )

            # ── Escalate flag on high variance ────────────────────────────
            vp = item["variance_vs_previous"]
            if vp is not None and abs(vp) > 0.20 and item["flag"] == "none":
                item["flag"] = "review"
                pct = round(vp * 100, 1)
                item["flag_notes"] = (
                    f"variance vs prior count: {pct:+.1f}%"
                )

            enriched.append(item)

        logger.info(
            "cross_reference_history: enriched %d items for location=%s "
            "(%d flagged after history check)",
            len(enriched), location,
            sum(1 for i in enriched if i["flag"] in ("conflict", "review")),
        )
        return enriched

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. create_draft_session
# ─────────────────────────────────────────────────────────────────────────────

def create_draft_session(
    location: str,
    audio_path: str | None,
    video_path: str | None,
    transcript: str | None,
    items: list,
) -> int:
    """
    Persist a new AI inventory session (status='draft') with all its items.

    Parameters
    ----------
    location   : 'Chatham' or 'Dennis Port'
    audio_path : path to the source audio file, or None
    video_path : path to the source video file, or None
    transcript : raw Whisper transcript text, or None
    items      : enriched reconciled items (from cross_reference_history)

    Returns
    -------
    session_id : int (ai_inventory_sessions.id)
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    source_type = "glasses"          # default — has both streams
    if audio_path and not video_path:
        source_type = "audio_only"
    elif video_path and not audio_path:
        source_type = "video_only"

    flagged_count = sum(1 for i in items if i.get("flag") in ("conflict", "review"))
    auto_count    = sum(1 for i in items if i.get("flag") == "none")

    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO ai_inventory_sessions
                (location, session_date, status, source_type,
                 audio_file_path, video_file_path, raw_transcript,
                 item_count, auto_confirmed_count, flagged_count)
            VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location, today, source_type,
                audio_path, video_path, transcript,
                len(items), auto_count, flagged_count,
            ),
        )
        session_id = cur.lastrowid
        conn.commit()

        # Insert all items
        for item in items:
            conn.execute(
                """
                INSERT INTO ai_inventory_items
                    (session_id, product_id, product_name, storage_location_id,
                     quantity, unit, is_partial,
                     audio_quantity, audio_confidence,
                     vision_quantity, vision_confidence,
                     reconciled_quantity, reconciled_confidence,
                     flag, flag_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    item.get("product_id"),
                    item.get("product_name", ""),
                    item.get("storage_location_id"),
                    item.get("reconciled_quantity"),
                    item.get("unit", ""),
                    1 if item.get("is_partial") else 0,
                    item.get("audio_quantity"),
                    item.get("audio_confidence"),
                    item.get("vision_quantity"),
                    item.get("vision_confidence"),
                    item.get("reconciled_quantity"),
                    item.get("reconciled_confidence"),
                    item.get("flag", "none"),
                    item.get("flag_notes", ""),
                ),
            )
        conn.commit()

        logger.info(
            "create_draft_session: session_id=%d location=%s items=%d flagged=%d auto=%d",
            session_id, location, len(items), flagged_count, auto_count,
        )
        return session_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. confirm_session
# ─────────────────────────────────────────────────────────────────────────────

def confirm_session(session_id: int, confirmed_items: list, confirmed_by: str = "manager") -> bool:
    """
    Mark an AI inventory session as confirmed and bridge quantities into the
    manual count system (count_items table).

    Parameters
    ----------
    session_id      : ai_inventory_sessions.id
    confirmed_items : list of dicts, each with:
                        ai_item_id      (int)   — ai_inventory_items.id
                        confirmed_qty   (float) — manager-approved quantity
                        product_id      (int|None) — product_inventory_settings.id
                        product_name    (str)
    confirmed_by    : display name for who confirmed (default 'manager')

    Returns
    -------
    True on success, False on any error.
    """
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today  = now_ts[:10]

    conn = get_connection()
    try:
        # Fetch session to get location and find linked count_session
        sess = conn.execute(
            "SELECT location, count_session_id, status FROM ai_inventory_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not sess:
            logger.error("confirm_session: session_id=%d not found", session_id)
            return False
        if sess["status"] == "confirmed":
            logger.warning("confirm_session: session_id=%d already confirmed", session_id)
            return False

        location        = sess["location"]
        count_session_id = sess["count_session_id"]

        # Resolve location → count_sessions.location (lowercase short name)
        db_location = _LOCATION_TO_DB.get(location.lower(), location.lower())

        # If no linked count_session_id, try to find the active one
        if not count_session_id:
            active = conn.execute(
                "SELECT id FROM count_sessions WHERE location = ? AND status = 'in_progress' LIMIT 1",
                (db_location,)
            ).fetchone()
            if active:
                count_session_id = active["id"]
                conn.execute(
                    "UPDATE ai_inventory_sessions SET count_session_id = ? WHERE id = ?",
                    (count_session_id, session_id)
                )
            else:
                # Auto-create a count session so Smart Count bridges into Inventory
                cursor = conn.execute(
                    "INSERT INTO count_sessions (location, status, started_at) VALUES (?, 'in_progress', datetime('now'))",
                    (db_location,)
                )
                count_session_id = cursor.lastrowid
                conn.execute(
                    "UPDATE ai_inventory_sessions SET count_session_id = ? WHERE id = ?",
                    (count_session_id, session_id)
                )

        for ci in confirmed_items:
            ai_item_id    = ci["ai_item_id"]
            confirmed_qty = float(ci["confirmed_qty"])
            pid           = ci.get("product_id")
            pname         = ci.get("product_name", "")

            # Auto-create product if not in catalog
            if not pid and pname:
                ai_item = conn.execute(
                    "SELECT unit, storage_location_id FROM ai_inventory_items WHERE id = ?",
                    (ai_item_id,)
                ).fetchone()
                unit = ai_item["unit"] if ai_item else "each"
                cur = conn.execute(
                    "INSERT INTO product_inventory_settings (product_name, inventory_unit, category) VALUES (?, ?, 'AI Inventory')",
                    (pname, unit)
                )
                pid = cur.lastrowid
                conn.execute(
                    "UPDATE ai_inventory_items SET product_id = ? WHERE id = ?",
                    (pid, ai_item_id)
                )
                logger.info("confirm_session: auto-created product '%s' (id=%d)", pname, pid)

            # Update ai_inventory_items with confirmed quantity
            conn.execute(
                """
                UPDATE ai_inventory_items
                SET confirmed_quantity = ?, flag = 'none', flag_notes = ''
                WHERE id = ?
                """,
                (confirmed_qty, ai_item_id)
            )

            # Write to ai_inventory_history for variance tracking
            if pid:
                conn.execute(
                    """
                    INSERT INTO ai_inventory_history
                        (product_id, session_id, quantity, unit, counted_date)
                    SELECT ?, ?, ?, unit, ?
                    FROM ai_inventory_items WHERE id = ?
                    """,
                    (pid, session_id, confirmed_qty, today, ai_item_id)
                )

            # Bridge into manual count system if a count_session is linked
            if count_session_id and pid:
                # Find the matching product in the manual products table
                # (count_items.product_id → products.id, NOT product_inventory_settings)
                manual_product = conn.execute(
                    """
                    SELECT id FROM products
                    WHERE LOWER(name) LIKE LOWER(?)
                    LIMIT 1
                    """,
                    (f"%{pname[:20]}%",)
                ).fetchone()

                if not manual_product and pname:
                    # Auto-create in products table so it shows in Inventories
                    ai_item_row = conn.execute(
                        "SELECT unit, storage_location_id FROM ai_inventory_items WHERE id = ?",
                        (ai_item_id,)
                    ).fetchone()
                    unit = ai_item_row["unit"] if ai_item_row else "each"
                    cur2 = conn.execute(
                        """INSERT INTO products (name, category, inventory_unit, location, active)
                           VALUES (?, 'AI Inventory', ?, ?, 1)""",
                        (pname, unit, db_location)
                    )
                    manual_product = {"id": cur2.lastrowid}
                    logger.info("confirm_session: auto-created product in products table: '%s' (id=%d)", pname, manual_product["id"])

                if manual_product:
                    manual_pid = manual_product["id"]
                    # Get storage location — fall back to Walk-in Cooler for this location
                    storage_id = conn.execute(
                        "SELECT storage_location_id FROM ai_inventory_items WHERE id = ?",
                        (ai_item_id,)
                    ).fetchone()
                    sloc_id = storage_id["storage_location_id"] if storage_id else None
                    if not sloc_id:
                        # Default to Walk-in Cooler for the session's location
                        default_sl = conn.execute(
                            "SELECT id FROM storage_locations WHERE name = 'Walk-in Cooler' AND location = ? LIMIT 1",
                            (db_location,)
                        ).fetchone()
                        sloc_id = default_sl["id"] if default_sl else 1

                    rows_updated = conn.execute(
                        """
                        UPDATE count_items
                        SET count_qty = ?,
                            counted_at = CURRENT_TIMESTAMP,
                            counted_by = 'ai_inventory'
                        WHERE session_id = ? AND product_id = ?
                        """,
                        (confirmed_qty, count_session_id, manual_pid)
                    ).rowcount

                    if rows_updated == 0:
                        # Product not on count sheet yet — insert it
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO count_items
                                (session_id, product_id, storage_location_id,
                                 count_qty, counted_at, counted_by)
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 'ai_inventory')
                            """,
                            (count_session_id, manual_pid, sloc_id, confirmed_qty)
                        )

        # Mark AI session as confirmed
        conn.execute(
            """
            UPDATE ai_inventory_sessions
            SET status = 'confirmed',
                confirmed_at = ?,
                confirmed_by = ?
            WHERE id = ?
            """,
            (now_ts, confirmed_by, session_id)
        )

        # Close the linked count_session
        if count_session_id:
            conn.execute(
                "UPDATE count_sessions SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
                (count_session_id,)
            )

        conn.commit()

        logger.info(
            "confirm_session: session_id=%d confirmed %d items by '%s'",
            session_id, len(confirmed_items), confirmed_by
        )
        return True

    except Exception as exc:
        conn.rollback()
        logger.error("confirm_session: failed — %s", exc, exc_info=True)
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. process_inventory — master pipeline
# ─────────────────────────────────────────────────────────────────────────────

def process_inventory(file_path: str, location: str = "Chatham") -> int:
    """
    Full AI inventory pipeline from a single file to a draft DB session.

    Detects file type and routes through the appropriate engine(s):
      - VIDEO (.mp4 .mov .avi .mkv .webm):
            → vision engine  (extract_frames + analyze_video)
            → audio engine   (extract_audio_from_video + transcribe + parse + match)
            → reconcile_streams
      - AUDIO ONLY (.wav .mp3 .m4a .aac .ogg .flac):
            → audio engine only
            → reconcile_streams(audio_items, [])

    After reconciliation, cross_reference_history adds purchase variance,
    then create_draft_session persists everything to the DB.

    Parameters
    ----------
    file_path : absolute path to the recording
    location  : 'Chatham' or 'Dennis Port'

    Returns
    -------
    session_id : int  (ai_inventory_sessions.id, status='draft')

    Raises
    ------
    FileNotFoundError if file_path does not exist
    ValueError if file type is not recognised
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"process_inventory: file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}

    if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
        raise ValueError(f"process_inventory: unsupported file type '{ext}'")

    audio_items   = []
    vision_items  = []
    transcript    = None
    audio_path    = None
    video_path    = None

    # ── Vision pass (video only) ───────────────────────────────────────────
    if ext in VIDEO_EXTS:
        video_path = file_path
        logger.info("process_inventory: starting vision engine on %s", file_path)
        try:
            from inventory_ai_vision import process_video
            vision_items = process_video(file_path)
            logger.info("process_inventory: vision engine returned %d items", len(vision_items))
        except Exception as exc:
            logger.error("process_inventory: vision engine failed — %s", exc, exc_info=True)
            vision_items = []

    # ── Audio pass ────────────────────────────────────────────────────────
    logger.info("process_inventory: starting audio engine on %s", file_path)
    try:
        from inventory_ai_audio import (
            extract_audio_from_video,
            transcribe_audio,
            parse_inventory_transcript,
            match_products,
        )
        audio_path = extract_audio_from_video(file_path)
        transcript = transcribe_audio(audio_path)
        parsed     = parse_inventory_transcript(transcript)
        audio_items = match_products(parsed, location=location)
        logger.info("process_inventory: audio engine returned %d items", len(audio_items))
    except Exception as exc:
        logger.error("process_inventory: audio engine failed — %s", exc, exc_info=True)
        audio_items = []

    # ── Reconcile ─────────────────────────────────────────────────────────
    reconciled = reconcile_streams(audio_items, vision_items)

    # ── Product-match any items still missing product_id ──────────────────
    unmatched = [i for i in reconciled if not i.get("product_id")]
    if unmatched:
        try:
            matched = match_products(unmatched, location=location)
            # Merge product_id back into reconciled items
            matched_by_name = {_normalise(m["product_name"]): m for m in matched}
            for item in reconciled:
                if not item.get("product_id"):
                    m = matched_by_name.get(_normalise(item["product_name"]))
                    if m and m.get("product_id"):
                        item["product_id"] = m["product_id"]
                        item["storage_location_id"] = m.get("storage_location_id")
            logger.info("process_inventory: product-matched %d/%d previously unmatched items",
                        sum(1 for m in matched if m.get("product_id")), len(unmatched))
        except Exception as exc:
            logger.warning("process_inventory: product matching pass failed — %s", exc)

    # ── History cross-reference ───────────────────────────────────────────
    enriched = cross_reference_history(reconciled, location)

    # ── Persist draft session ─────────────────────────────────────────────
    session_id = create_draft_session(
        location=location,
        audio_path=audio_path,
        video_path=video_path,
        transcript=transcript,
        items=enriched,
    )

    logger.info(
        "process_inventory: done — session_id=%d location=%s items=%d",
        session_id, location, len(enriched),
    )
    return session_id


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("reconcile_streams() self-test")
    print("=" * 60)

    # Sample audio items (7 products — mix of full and partial)
    sample_audio = [
        {"product_name": "Bud Light",      "product_id": 10, "quantity": 3.0,  "unit": "case",   "is_partial": False, "confidence": 0.90, "source": "audio", "notes": "",             "storage_location": "Bar"},
        {"product_name": "Chicken Breast", "product_id": 42, "quantity": 2.0,  "unit": "case",   "is_partial": False, "confidence": 0.97, "source": "audio", "notes": "",             "storage_location": "Walk-in Cooler"},
        {"product_name": "Tito's Vodka",   "product_id": 87, "quantity": 0.75, "unit": "bottle", "is_partial": True,  "confidence": 0.90, "source": "audio", "notes": "looks light",  "storage_location": "Bar"},
        {"product_name": "French Fries",   "product_id": 55, "quantity": 4.0,  "unit": "case",   "is_partial": False, "confidence": 1.00, "source": "audio", "notes": "",             "storage_location": "Freezer"},
        {"product_name": "Romaine Lettuce","product_id": 61, "quantity": 1.5,  "unit": "case",   "is_partial": True,  "confidence": 0.90, "source": "audio", "notes": "",             "storage_location": "Walk-in Cooler"},
        {"product_name": "Jameson",        "product_id": 92, "quantity": 2.0,  "unit": "bottle", "is_partial": False, "confidence": 0.90, "source": "audio", "notes": "",             "storage_location": "Bar"},
        {"product_name": "Mushrooms",      "product_id": 73, "quantity": 1.0,  "unit": "bag",    "is_partial": False, "confidence": 0.90, "source": "audio", "notes": "",             "storage_location": "Walk-in Cooler"},
    ]

    # Sample vision items — some agree, one conflicts, one vision-only
    sample_vision = [
        {"product_name": "Bud Light",      "product_id": 10, "quantity": 3.1,  "unit": "case",   "is_partial": False, "confidence": 0.85, "source": "vision", "notes": "",            "storage_location": "Bar"},
        {"product_name": "Chicken Breast", "product_id": 42, "quantity": 2.0,  "unit": "case",   "is_partial": False, "confidence": 0.90, "source": "vision", "notes": "",            "storage_location": "Walk-in Cooler"},
        # Conflict: Tito's — audio says 0.75, vision says 1.5
        {"product_name": "Tito's Vodka",   "product_id": 87, "quantity": 1.5,  "unit": "bottle", "is_partial": False, "confidence": 0.70, "source": "vision", "notes": "",            "storage_location": "Bar"},
        {"product_name": "French Fries",   "product_id": 55, "quantity": 4.0,  "unit": "case",   "is_partial": False, "confidence": 0.80, "source": "vision", "notes": "",            "storage_location": "Freezer"},
        # Vision-only: Coors Light (not captured by audio)
        {"product_name": "Coors Light",    "product_id": 11, "quantity": 5.0,  "unit": "case",   "is_partial": False, "confidence": 0.75, "source": "vision", "notes": "",            "storage_location": "Bar"},
    ]

    result = reconcile_streams(sample_audio, sample_vision)

    print(f"\n{'Product':<22} {'Flag':<10} {'AudQty':>7} {'VisQty':>7} {'RecQty':>7} {'Conf':>6}  Notes")
    print("-" * 80)
    for r in sorted(result, key=lambda x: x["product_name"]):
        aq = f"{r['audio_quantity']:.2f}"  if r["audio_quantity"]  is not None else "  —  "
        vq = f"{r['vision_quantity']:.2f}" if r["vision_quantity"] is not None else "  —  "
        rq = f"{r['reconciled_quantity']:.2f}"
        cf = f"{r['reconciled_confidence']:.2f}"
        fl = r["flag"]
        nm = r["product_name"]
        nt = r.get("flag_notes") or r.get("notes") or ""
        print(f"{nm:<22} {fl:<10} {aq:>7} {vq:>7} {rq:>7} {cf:>6}  {nt}")

    # Tally
    agree    = sum(1 for r in result if r["flag"] == "none"     and r["audio_quantity"] and r["vision_quantity"])
    conflict = sum(1 for r in result if r["flag"] == "conflict")
    review   = sum(1 for r in result if r["flag"] == "review")
    audio_only = sum(1 for r in result if r["vision_quantity"] is None)
    vis_only   = sum(1 for r in result if r["audio_quantity"]  is None)

    print(f"\nTotal items : {len(result)}")
    print(f"  Agreed    : {agree}  (both streams, within 10%)")
    print(f"  Conflicts : {conflict}  (both present, audio trusted)")
    print(f"  Review    : {review}  (vision-only or high variance flag)")
    print(f"  Audio-only: {audio_only}")
    print(f"  Vis-only  : {vis_only}")
    print("\nSelf-test complete.")
