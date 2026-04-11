#!/usr/bin/env python3
"""
test_ai_inventory.py — Integration tests for the AI Inventory system.

Tests 1-4: Unit + DB tests using SYNTHETIC data (no real audio/video needed).
Test 5:    API endpoint tests via Flask test client.

Cleans up all test data it creates.

Usage:
    cd /opt/rednun && source venv/bin/activate && python3 test_ai_inventory.py
"""

import os
import sys
import json

# Run from the project root so relative DB path and .env resolve correctly
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED   = "\033[91m"
RESET = "\033[0m"

# ── Test counters ──────────────────────────────────────────────────────────────
PASS_COUNT = 0
FAIL_COUNT = 0
_created_session_ids = []   # all AI sessions created — cleaned up at end


def p(label: str, ok: bool, msg: str = "") -> bool:
    global PASS_COUNT, FAIL_COUNT
    symbol = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"    [{symbol}] {label}" + (f"\n           {RED}{msg}{RESET}" if (not ok and msg) else ""))
    if ok:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
    return ok


def section(title: str):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print('='*65)


# ── DB helpers ─────────────────────────────────────────────────────────────────

from data_store import get_connection
from inventory_ai_db import init_ai_inventory_tables

init_ai_inventory_tables()   # idempotent — safe to call every run


def cleanup_sessions(session_ids):
    """Delete test sessions (and their items/history via CASCADE)."""
    if not session_ids:
        return
    conn = get_connection()
    for sid in session_ids:
        conn.execute("DELETE FROM ai_inventory_history WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM ai_inventory_items WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM ai_inventory_sessions WHERE id = ?", (sid,))
    conn.commit()
    conn.close()


# ── Shared-format item factories ───────────────────────────────────────────────

def audio_item(name, qty, unit="case", pid=None, loc="Walk-in Cooler",
               conf=0.9, partial=False, notes="", flag="none", flag_notes=""):
    return {
        "product_name": name, "product_id": pid, "quantity": qty,
        "unit": unit, "is_partial": partial, "notes": notes,
        "confidence": conf, "source": "audio",
        "storage_location": loc, "storage_location_id": None,
        "flag": flag, "flag_notes": flag_notes,
    }


def vision_item(name, qty, unit="case", pid=None, loc="Walk-in Cooler",
                conf=0.85, partial=False):
    return {
        "product_name": name, "product_id": pid, "quantity": qty,
        "unit": unit, "is_partial": partial, "notes": "",
        "confidence": conf, "source": "vision",
        "storage_location": loc, "storage_location_id": None,
        "flag": "none", "flag_notes": "",
    }


# ── Import pipeline functions ──────────────────────────────────────────────────

from inventory_ai_reconcile import (
    reconcile_streams,
    cross_reference_history,
    create_draft_session,
    confirm_session,
)


# =============================================================================
# TEST 1 — Full happy path
# =============================================================================

def test1_happy_path():
    section("TEST 1 — Full Happy Path (5 audio + 4 vision → DB → confirm → bridge)")

    audio = [
        audio_item("Bud Light",      3.0, "case",   pid=10, loc="Bar"),
        audio_item("Chicken Breast", 2.0, "case",   pid=42, loc="Walk-in Cooler"),
        audio_item("Tito's Vodka",   0.75,"bottle", pid=87, loc="Bar", partial=True),
        audio_item("French Fries",   4.0, "case",   pid=55, loc="Freezer"),
        audio_item("Romaine Lettuce",1.5, "case",   pid=61, loc="Walk-in Cooler", partial=True),
    ]
    vision = [
        vision_item("Bud Light",      3.1, "case",   pid=10, loc="Bar"),
        vision_item("Chicken Breast", 2.0, "case",   pid=42, loc="Walk-in Cooler"),
        vision_item("French Fries",   4.0, "case",   pid=55, loc="Freezer"),
        vision_item("Coors Light",    5.0, "case",   pid=11, loc="Bar"),  # vision-only
    ]

    # ── reconcile_streams ─────────────────────────────────────────────────────
    try:
        reconciled = reconcile_streams(audio, vision)
    except Exception as exc:
        p("reconcile_streams runs without crash", False, str(exc))
        return None

    p("reconcile_streams runs without crash", True)
    p("result is a list", isinstance(reconciled, list))
    p("correct item count (5+1 vision-only = 6)", len(reconciled) == 6,
      f"got {len(reconciled)}")

    # Check Bud Light (both agree within 10%: 3.0 vs 3.1 → avg 3.05)
    bud = next((i for i in reconciled if i["product_name"] == "Bud Light"), None)
    if bud:
        p("Bud Light: flag='none' (agree)", bud["flag"] == "none", f"flag={bud['flag']}")
        p("Bud Light: reconciled_quantity = avg(3.0,3.1)",
          abs(bud["reconciled_quantity"] - 3.05) < 0.01,
          f"got {bud['reconciled_quantity']}")
        p("Bud Light: reconciled_confidence = 0.95", bud["reconciled_confidence"] == 0.95)
    else:
        p("Bud Light in results", False, "not found")

    # Check Coors Light (vision-only)
    coors = next((i for i in reconciled if i["product_name"] == "Coors Light"), None)
    if coors:
        p("Coors Light: flag='review' (vision-only)", coors["flag"] == "review",
          f"flag={coors['flag']}")
        p("Coors Light: reconciled_confidence = 0.60",
          coors["reconciled_confidence"] == 0.60,
          f"got {coors['reconciled_confidence']}")
    else:
        p("Coors Light in results", False, "not found")

    # Audio-only items have confidence=0.70
    romaine = next((i for i in reconciled if "Romaine" in i["product_name"]), None)
    if romaine:
        p("Romaine: audio-only → confidence=0.70",
          romaine["reconciled_confidence"] == 0.70,
          f"got {romaine['reconciled_confidence']}")

    # ── cross_reference_history (empty DB — no errors expected) ───────────────
    try:
        enriched = cross_reference_history(reconciled, "Chatham")
    except Exception as exc:
        p("cross_reference_history runs on empty history", False, str(exc))
        return None

    p("cross_reference_history returns same count", len(enriched) == len(reconciled),
      f"got {len(enriched)}")
    p("all items have variance keys",
      all("recent_purchase_qty" in i and "last_count_qty" in i for i in enriched))

    # ── create_draft_session ──────────────────────────────────────────────────
    try:
        sid = create_draft_session(
            location="Chatham",
            audio_path="/tmp/test_audio.wav",
            video_path="/tmp/test_video.mp4",
            transcript="test transcript",
            items=enriched,
        )
        _created_session_ids.append(sid)
    except Exception as exc:
        p("create_draft_session creates session", False, str(exc))
        return None

    p("create_draft_session returns int session_id", isinstance(sid, int) and sid > 0,
      f"got {sid!r}")

    conn = get_connection()
    sess_row = conn.execute(
        "SELECT status, item_count, location, source_type FROM ai_inventory_sessions WHERE id = ?",
        (sid,)
    ).fetchone()
    items_rows = conn.execute(
        "SELECT COUNT(*) as c FROM ai_inventory_items WHERE session_id = ?",
        (sid,)
    ).fetchone()
    conn.close()

    p("session status = 'draft'",   sess_row["status"] == "draft",       f"got {sess_row['status']}")
    p("session location = Chatham", sess_row["location"] == "Chatham",   f"got {sess_row['location']}")
    p("session item_count = 6",     sess_row["item_count"] == 6,          f"got {sess_row['item_count']}")
    p("ai_inventory_items has 6 rows", items_rows["c"] == 6,             f"got {items_rows['c']}")
    p("source_type = 'glasses' (both paths)", sess_row["source_type"] == "glasses",
      f"got {sess_row['source_type']}")

    # ── confirm_session ───────────────────────────────────────────────────────
    conn = get_connection()
    item_rows = conn.execute(
        "SELECT id, product_id, product_name FROM ai_inventory_items WHERE session_id = ?",
        (sid,)
    ).fetchall()
    conn.close()

    confirmed_items = [
        {
            "ai_item_id":    r["id"],
            "confirmed_qty": 3.0,
            "product_id":    r["product_id"],
            "product_name":  r["product_name"],
        }
        for r in item_rows
    ]

    ok = confirm_session(sid, confirmed_items, confirmed_by="test_runner")
    p("confirm_session returns True", ok is True, f"got {ok!r}")

    conn = get_connection()
    sess_after = conn.execute(
        "SELECT status, confirmed_by, confirmed_at FROM ai_inventory_sessions WHERE id = ?",
        (sid,)
    ).fetchone()
    history_count = conn.execute(
        "SELECT COUNT(*) as c FROM ai_inventory_history WHERE session_id = ?",
        (sid,)
    ).fetchone()
    conn.close()

    p("session status = 'confirmed'",       sess_after["status"] == "confirmed",
      f"got {sess_after['status']}")
    p("confirmed_by = 'test_runner'",        sess_after["confirmed_by"] == "test_runner",
      f"got {sess_after['confirmed_by']}")
    p("confirmed_at is set",                sess_after["confirmed_at"] is not None)

    # History: only items with product_id get written (Bud/Chicken/Tito's/FF/Romaine/Coors = 6 if all have pids)
    pids_with_value = sum(1 for r in item_rows if r["product_id"] is not None)
    p(f"ai_inventory_history has {pids_with_value} rows (one per item with product_id)",
      history_count["c"] == pids_with_value,
      f"got {history_count['c']}")

    # Bridge: check count_sessions linked + count_items written
    conn = get_connection()
    sess_link = conn.execute(
        "SELECT count_session_id FROM ai_inventory_sessions WHERE id = ?",
        (sid,)
    ).fetchone()
    conn.close()

    csid = sess_link["count_session_id"]
    p("count_session_id is linked (found active count session)",
      csid is not None,
      f"got {csid!r} — OK if no active count session exists (not a test failure)")

    return sid   # return confirmed session_id for use in other tests


# =============================================================================
# TEST 2 — Audio-only path
# =============================================================================

def test2_audio_only():
    section("TEST 2 — Audio-Only Path (no vision)")

    audio = [
        audio_item("Heineken",       6.0, "case",   pid=20, loc="Bar"),
        audio_item("Salmon Fillet",  3.0, "case",   pid=30, loc="Walk-in Cooler"),
        audio_item("Olive Oil",      2.0, "case",   pid=40, loc="Dry Storage"),
    ]

    try:
        reconciled = reconcile_streams(audio, [])
    except Exception as exc:
        p("reconcile_streams with empty vision", False, str(exc))
        return

    p("reconcile_streams with empty vision: no crash", True)
    p("all items returned", len(reconciled) == 3, f"got {len(reconciled)}")

    for item in reconciled:
        p(f"{item['product_name']}: confidence = 0.70 (audio-only)",
          item["reconciled_confidence"] == 0.70,
          f"got {item['reconciled_confidence']}")
        p(f"{item['product_name']}: flag = 'none'",
          item["flag"] == "none",
          f"got {item['flag']}")

    # Persist session
    try:
        enriched = cross_reference_history(reconciled, "Dennis Port")
        sid = create_draft_session(
            location="Dennis Port",
            audio_path="/tmp/test_audio_only.wav",
            video_path=None,
            transcript="Heineken six cases, salmon three cases, olive oil two",
            items=enriched,
        )
        _created_session_ids.append(sid)
    except Exception as exc:
        p("create_draft_session for audio-only", False, str(exc))
        return

    conn = get_connection()
    row = conn.execute(
        "SELECT source_type, item_count FROM ai_inventory_sessions WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()

    p("source_type = 'audio_only'", row["source_type"] == "audio_only",
      f"got {row['source_type']}")
    p("item_count = 3", row["item_count"] == 3, f"got {row['item_count']}")


# =============================================================================
# TEST 3 — Conflict handling
# =============================================================================

def test3_conflicts():
    section("TEST 3 — Conflict Handling")

    # Conflict: audio says chicken = 3, vision says 7 (> 10% diff)
    audio = [audio_item("Chicken Breast", 3.0, "case", pid=42, loc="Walk-in Cooler")]
    vision = [vision_item("Chicken Breast", 7.0, "case", pid=42, loc="Walk-in Cooler")]

    reconciled = reconcile_streams(audio, vision)

    chicken = reconciled[0] if reconciled else None
    if not chicken:
        p("conflict item returned", False, "no items")
        return

    p("flag = 'conflict'",                   chicken["flag"] == "conflict",
      f"got {chicken['flag']}")
    p("audio value trusted (qty = 3.0)",     chicken["reconciled_quantity"] == 3.0,
      f"got {chicken['reconciled_quantity']}")
    p("confidence = 0.50",                   chicken["reconciled_confidence"] == 0.50,
      f"got {chicken['reconciled_confidence']}")
    p("flag_notes contains both values",
      "3.0" in (chicken.get("flag_notes") or "") and "7.0" in (chicken.get("flag_notes") or ""),
      f"got {chicken.get('flag_notes')!r}")

    # Exactly at threshold (10%): 3.0 vs 3.3 → diff=0.1/3.3=0.0303 < 0.10 → agree
    audio2  = [audio_item("Jameson", 3.0, "bottle", pid=92)]
    vision2 = [vision_item("Jameson", 3.3, "bottle", pid=92)]
    r2 = reconcile_streams(audio2, vision2)
    j = r2[0]
    p("Jameson 3.0 vs 3.3: within threshold → flag='none'", j["flag"] == "none",
      f"got {j['flag']}")
    p("Jameson 3.0 vs 3.3: avg quantity ≈ 3.15",
      abs(j["reconciled_quantity"] - 3.15) < 0.01,
      f"got {j['reconciled_quantity']}")

    # Persist a draft with conflict item (for flag count verification)
    enriched = cross_reference_history(reconciled, "Chatham")
    try:
        sid = create_draft_session(
            location="Chatham",
            audio_path=None,
            video_path=None,
            transcript=None,
            items=enriched,
        )
        _created_session_ids.append(sid)
    except Exception as exc:
        p("create_draft_session with conflict item", False, str(exc))
        return

    conn = get_connection()
    row = conn.execute(
        "SELECT flagged_count, auto_confirmed_count FROM ai_inventory_sessions WHERE id = ?",
        (sid,)
    ).fetchone()
    conn.close()

    p("flagged_count = 1 (the conflict)",     row["flagged_count"] == 1,
      f"got {row['flagged_count']}")
    p("auto_confirmed_count = 0",             row["auto_confirmed_count"] == 0,
      f"got {row['auto_confirmed_count']}")


# =============================================================================
# TEST 4 — Edge Cases
# =============================================================================

def test4_edge_cases():
    section("TEST 4 — Edge Cases")

    # ── Both empty ────────────────────────────────────────────────────────────
    try:
        r = reconcile_streams([], [])
        p("both empty: no crash", True)
        p("both empty: returns empty list", r == [], f"got {r!r}")
    except Exception as exc:
        p("both empty: no crash", False, str(exc))

    try:
        sid = create_draft_session("Chatham", None, None, None, [])
        _created_session_ids.append(sid)
        p("create_draft_session with 0 items: no crash", True)
        conn = get_connection()
        row = conn.execute(
            "SELECT item_count FROM ai_inventory_sessions WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        p("0-item session: item_count = 0", row["item_count"] == 0, f"got {row['item_count']}")
    except Exception as exc:
        p("create_draft_session with 0 items: no crash", False, str(exc))

    # ── Duplicate product names ───────────────────────────────────────────────
    audio_dupes = [
        audio_item("Bud Light", 3.0, pid=10),
        audio_item("Bud Light", 2.0, pid=10),   # duplicate — last one wins in dict
    ]
    try:
        r = reconcile_streams(audio_dupes, [])
        p("duplicate product names: no crash", True)
        # reconcile_streams uses a dict keyed on normalised name → deduplicated to 1
        p("duplicate names deduped to 1 item", len(r) == 1, f"got {len(r)}")
    except Exception as exc:
        p("duplicate product names: no crash", False, str(exc))

    # ── Very long notes field ─────────────────────────────────────────────────
    long_notes = "X" * 2000
    audio_long = [audio_item("Long Notes Item", 1.0, notes=long_notes)]
    try:
        r = reconcile_streams(audio_long, [])
        sid = create_draft_session("Chatham", None, None, None, r)
        _created_session_ids.append(sid)
        p("very long notes field: no crash", True)
    except Exception as exc:
        p("very long notes field: no crash", False, str(exc))

    # ── Unicode in product names ──────────────────────────────────────────────
    audio_uni = [
        audio_item("Château Margaux",  1.0, "bottle", pid=None),
        audio_item("Jalapeño Poppers", 2.0, "case",   pid=None),
        audio_item("日本酒",            3.0, "bottle", pid=None),
    ]
    try:
        r = reconcile_streams(audio_uni, [])
        sid = create_draft_session("Chatham", None, None, None, r)
        _created_session_ids.append(sid)
        p("unicode product names: no crash", True)
        p("unicode: all 3 items saved", len(r) == 3, f"got {len(r)}")
    except Exception as exc:
        p("unicode product names: no crash", False, str(exc))

    # ── Empty audio results (0 items) ─────────────────────────────────────────
    try:
        r = cross_reference_history([], "Chatham")
        p("cross_reference_history with empty list: no crash", True)
        p("empty list → empty list", r == [], f"got {r!r}")
    except Exception as exc:
        p("cross_reference_history with empty list: no crash", False, str(exc))

    # ── confirm_session on already-confirmed session ───────────────────────────
    # Create a fresh session then confirm it twice
    try:
        sid = create_draft_session("Chatham", None, None, None,
            [audio_item("Test Item", 1.0, pid=None)])
        _created_session_ids.append(sid)
        conn = get_connection()
        row = conn.execute(
            "SELECT id, product_id, product_name FROM ai_inventory_items WHERE session_id = ?",
            (sid,)
        ).fetchone()
        conn.close()
        ci = [{"ai_item_id": row["id"], "confirmed_qty": 1.0,
               "product_id": None, "product_name": row["product_name"]}]
        # First confirm
        ok1 = confirm_session(sid, ci, confirmed_by="test")
        # Second confirm — should return False (already confirmed)
        ok2 = confirm_session(sid, ci, confirmed_by="test")
        p("confirm_session: first confirm returns True",  ok1 is True,  f"got {ok1!r}")
        p("confirm_session: second confirm returns False (already confirmed)",
          ok2 is False, f"got {ok2!r}")
    except Exception as exc:
        p("double-confirm guard", False, str(exc))


# =============================================================================
# TEST 5 — API endpoint tests via Flask test client
# =============================================================================

def test5_api_endpoints():
    section("TEST 5 — API Endpoint Tests (Flask test_client)")

    # Build a minimal test app — avoids all the cron/scheduler side-effects of server.py
    from flask import Flask
    from inventory_ai_routes import ai_inventory_bp

    test_app = Flask(__name__, static_folder="static")
    test_app.config["TESTING"] = True
    test_app.config["SECRET_KEY"] = "test-secret-for-session"
    test_app.register_blueprint(ai_inventory_bp)

    client = test_app.test_client()

    # Helper: inject authenticated session
    def auth_session():
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "test_runner"

    auth_session()

    # ── Pre-create a draft session + a confirmed session for testing ──────────
    audio = [audio_item("Test Beer", 4.0, "case", pid=10)]
    r     = reconcile_streams(audio, [])
    draft_sid = create_draft_session("Chatham", None, None, None, r)
    _created_session_ids.append(draft_sid)

    audio2 = [audio_item("Test Wine", 2.0, "bottle", pid=20)]
    r2     = reconcile_streams(audio2, [])
    confirmed_sid = create_draft_session("Dennis Port", None, None, None, r2)
    _created_session_ids.append(confirmed_sid)
    # Confirm it directly via DB so it's in confirmed state
    conn = get_connection()
    conn.execute(
        "UPDATE ai_inventory_sessions SET status='confirmed', confirmed_by='seed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?",
        (confirmed_sid,)
    )
    conn.commit()
    conn.close()

    # ── GET /api/ai-inventory/drafts → 200, returns list ─────────────────────
    auth_session()
    rv = client.get("/api/ai-inventory/drafts")
    p("GET /drafts → 200", rv.status_code == 200, f"status={rv.status_code}")
    body = rv.get_json()
    p("GET /drafts → returns list", isinstance(body, list), f"got {type(body)}")

    # ── GET /api/ai-inventory/drafts/<id> (valid) → 200 ──────────────────────
    auth_session()
    rv = client.get(f"/api/ai-inventory/drafts/{draft_sid}")
    p(f"GET /drafts/{draft_sid} → 200", rv.status_code == 200, f"status={rv.status_code}")
    body = rv.get_json()
    p("GET /drafts/<id> → has 'session' and 'items' keys",
      isinstance(body, dict) and "session" in body and "items" in body,
      f"keys={list(body.keys()) if isinstance(body, dict) else body}")

    # ── GET /api/ai-inventory/drafts/999 → 404 ───────────────────────────────
    auth_session()
    rv = client.get("/api/ai-inventory/drafts/999999")
    p("GET /drafts/999999 → 404", rv.status_code == 404, f"status={rv.status_code}")

    # ── POST /api/ai-inventory/drafts/<id>/confirm (valid) → 200 ─────────────
    auth_session()
    conn = get_connection()
    items_rows = conn.execute(
        "SELECT id FROM ai_inventory_items WHERE session_id = ?", (draft_sid,)
    ).fetchall()
    conn.close()
    payload = {
        "confirmed_by": "test_runner",
        "items": [{"item_id": r["id"], "confirmed_quantity": 4.0} for r in items_rows],
    }
    rv = client.post(
        f"/api/ai-inventory/drafts/{draft_sid}/confirm",
        json=payload,
    )
    p(f"POST /drafts/{draft_sid}/confirm → 200", rv.status_code == 200,
      f"status={rv.status_code} body={rv.get_json()}")

    # ── POST confirm on already-confirmed → error ─────────────────────────────
    auth_session()
    # draft_sid is now confirmed — try again
    rv = client.post(
        f"/api/ai-inventory/drafts/{draft_sid}/confirm",
        json=payload,
    )
    p("POST confirm on already-confirmed → 500 (confirm_session returns False)",
      rv.status_code == 500,
      f"status={rv.status_code}")

    # ── PUT /api/ai-inventory/drafts/<id>/items/<item_id> ────────────────────
    # Use confirmed_sid which still has items; just test the UPDATE path
    conn = get_connection()
    item_row = conn.execute(
        "SELECT id FROM ai_inventory_items WHERE session_id = ?", (confirmed_sid,)
    ).fetchone()
    conn.close()
    if item_row:
        auth_session()
        rv = client.put(
            f"/api/ai-inventory/drafts/{confirmed_sid}/items/{item_row['id']}",
            json={"reconciled_quantity": 7.5, "flag": "none", "flag_notes": ""},
        )
        p("PUT /drafts/<id>/items/<item_id> → 200", rv.status_code == 200,
          f"status={rv.status_code}")

    # ── DELETE /api/ai-inventory/drafts/<id> on draft → 200 ──────────────────
    # Create a fresh draft to delete
    r3    = reconcile_streams([audio_item("Delete Me", 1.0)], [])
    del_sid = create_draft_session("Chatham", None, None, None, r3)
    # Don't add to _created_session_ids — we expect DELETE to remove it

    auth_session()
    rv = client.delete(f"/api/ai-inventory/drafts/{del_sid}")
    p(f"DELETE /drafts/{del_sid} (draft) → 200", rv.status_code == 200,
      f"status={rv.status_code}")

    # Verify it's actually gone
    conn = get_connection()
    gone = conn.execute(
        "SELECT id FROM ai_inventory_sessions WHERE id = ?", (del_sid,)
    ).fetchone()
    conn.close()
    p("DELETE: session is actually removed from DB", gone is None,
      f"still found id={gone}")

    # ── DELETE on confirmed session → 409 ─────────────────────────────────────
    auth_session()
    rv = client.delete(f"/api/ai-inventory/drafts/{confirmed_sid}")
    p(f"DELETE /drafts/{confirmed_sid} (confirmed) → 409", rv.status_code == 409,
      f"status={rv.status_code}")

    # ── GET /api/ai-inventory/history → 200 ──────────────────────────────────
    auth_session()
    rv = client.get("/api/ai-inventory/history")
    p("GET /history → 200", rv.status_code == 200, f"status={rv.status_code}")
    body = rv.get_json()
    p("GET /history → returns list", isinstance(body, list), f"got {type(body)}")


# =============================================================================
# PRODUCTION READINESS CHECKLIST
# =============================================================================

def check_production_readiness():
    section("PRODUCTION READINESS CHECKLIST")

    # 1. AI tables exist with correct schema
    conn = get_connection()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_inventory%'"
    ).fetchall()}
    conn.close()
    p("ai_inventory_sessions table exists", "ai_inventory_sessions" in tables)
    p("ai_inventory_items table exists",    "ai_inventory_items"    in tables)
    p("ai_inventory_history table exists",  "ai_inventory_history"  in tables)

    # 2. Blueprint registered in server.py
    import re
    with open("/opt/rednun/server.py") as f:
        srv = f.read()
    p("ai_inventory_bp imported in server.py",   "from inventory_ai_routes import ai_inventory_bp" in srv)
    p("ai_inventory_bp registered in server.py", "app.register_blueprint(ai_inventory_bp)" in srv)

    # 3. inventory_intake/ folder exists
    intake = "/opt/rednun/inventory_intake"
    p("inventory_intake/ folder exists", os.path.isdir(intake))

    # 4. No orphaned /tmp/rednun_frames_* directories
    orphans = [d for d in os.listdir("/tmp") if d.startswith("rednun_frames_")]
    p(f"No orphaned /tmp/rednun_frames_* dirs",
      len(orphans) == 0,
      f"found: {orphans}")

    # 5. ai_inventory.html exists in static/
    p("static/ai_inventory.html exists",
      os.path.isfile("/opt/rednun/static/ai_inventory.html"))

    # 6. sidebar.js has Smart Count entry
    with open("/opt/rednun/static/sidebar.js") as f:
        sidebar = f.read()
    p("sidebar.js has 'Smart Count' entry", "Smart Count" in sidebar)
    p("sidebar.js has /ai-inventory path",  "/ai-inventory" in sidebar)

    # 7. Max content length in server.py (upload protection)
    has_max_content = "MAX_CONTENT_LENGTH" in srv
    p("server.py sets MAX_CONTENT_LENGTH for uploads",
      has_max_content,
      "not found — consider adding app.config['MAX_CONTENT_LENGTH'] = 500*1024*1024")

    # 8. All inventory_ai_*.py files import logging and set up log handler
    ai_files = [
        "inventory_ai_db.py", "inventory_ai_audio.py",
        "inventory_ai_vision.py", "inventory_ai_reconcile.py",
        "inventory_ai_routes.py",
    ]
    for fname in ai_files:
        path = f"/opt/rednun/{fname}"
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read()
            has_logger = "logger = logging.getLogger" in content
            p(f"{fname}: has logger setup", has_logger)
        else:
            p(f"{fname}: file exists", False, "not found")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    print("\n" + "="*65)
    print("  AI INVENTORY INTEGRATION TEST SUITE")
    print("  Red Nun Dashboard — Session 6")
    print("="*65)

    try:
        confirmed_sid = test1_happy_path()
        test2_audio_only()
        test3_conflicts()
        test4_edge_cases()
        test5_api_endpoints()
        check_production_readiness()
    finally:
        # Always clean up — even if tests crash mid-run
        if _created_session_ids:
            print(f"\n  Cleaning up {len(_created_session_ids)} test sessions...")
            cleanup_sessions(_created_session_ids)
            print("  Cleanup done.")

    print(f"\n{'='*65}")
    total = PASS_COUNT + FAIL_COUNT
    print(f"  RESULTS: {PASS_COUNT}/{total} PASSED  |  {FAIL_COUNT} FAILED")
    if FAIL_COUNT == 0:
        print(f"  {GREEN}ALL TESTS PASSED{RESET}")
    else:
        print(f"  {RED}{FAIL_COUNT} TEST(S) FAILED — review output above{RESET}")
    print('='*65 + "\n")

    sys.exit(0 if FAIL_COUNT == 0 else 1)
