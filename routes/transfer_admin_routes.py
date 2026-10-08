"""
/transfer/admin — Mike only (brief section 6, 4A L2/L4/L5/L6, 8I).

  People      add person + home house + role, create/revoke token (shown once), last used
  Words       review the drafted card names + kitchen words, by product; approve / strike in bulk
  Links       transfers waiting on a product link + suggested Chatham/Dennis pairs
  Learned     what people picked ("fries" -> straight cut, 2x); delete a bad one
  Waste       entries over the review threshold
  Settings    Claude fallback on/off + monthly cap, email per entry / daily digest / off, waste flag $
  Problems    failed emails, fallback calls this month
"""
import json
import re
from difflib import SequenceMatcher

from flask import Blueprint, current_app, jsonify, request, session

from integrations.toast.data_store import get_connection
from reports import item_recognition as R
from reports import moves as M
from routes.auth_routes import admin_required

transfer_admin_bp = Blueprint('transfer_admin', __name__)
LOCATIONS = ('chatham', 'dennis')


def _who():
    return session.get('full_name') or session.get('username') or 'admin'


@transfer_admin_bp.route('/transfer/admin')
@admin_required
def admin_page():
    return current_app.send_static_file('transfer_admin.html')


# ---------------------------------------------------------------------------
# People + tokens
# ---------------------------------------------------------------------------

@transfer_admin_bp.route('/api/transfer-admin/people', methods=['GET'])
@admin_required
def people():
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        rows = conn.execute("""SELECT t.id, t.person_name, t.home_location, t.role, t.active, t.created_at, t.created_by,
                                      t.last_used_at, t.revoked_at,
                                      (SELECT COUNT(*) FROM inventory_transfers x WHERE x.token_id = t.id) AS transfers,
                                      (SELECT COUNT(*) FROM waste_log w WHERE w.token_id = t.id) AS waste
                               FROM transfer_tokens t ORDER BY t.active DESC, t.person_name""").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/people', methods=['POST'])
@admin_required
def add_person():
    d = request.json or {}
    name = (d.get('person') or '').strip()
    home = (d.get('home') or '').strip().lower() or None
    role = d.get('role') if d.get('role') in ('owner', 'staff') else 'staff'
    if not name or (home and home not in LOCATIONS):
        return jsonify({'error': 'name required; home is chatham, dennis or blank'}), 400
    conn = get_connection()
    try:
        raw = M.create_token(conn, name, home, role, _who())
        conn.commit()
        return jsonify({'token': raw, 'person': name, 'home': home, 'role': role})
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/people/<int:tid>/revoke', methods=['POST'])
@admin_required
def revoke(tid):
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        conn.execute("UPDATE transfer_tokens SET active = 0, revoked_by = ?, revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (_who(), tid))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Words: card names + aliases, reviewed by product
# ---------------------------------------------------------------------------

@transfer_admin_bp.route('/api/transfer-admin/words', methods=['GET'])
@admin_required
def words():
    status = request.args.get('status', 'proposed')         # proposed | approved | struck | all
    q = (request.args.get('q') or '').strip().lower()
    page = max(0, int(request.args.get('page', 0)))
    per = 25
    conn = get_connection()
    try:
        R.ensure_tables(conn)
        ctx = {l: R.house_context(conn, l) for l in LOCATIONS}
        cond = "1=1" if status == 'all' else "a.status = ?"
        params = [] if status == 'all' else [status]
        pids = [r[0] for r in conn.execute(f"""
            SELECT a.product_id FROM product_aliases a JOIN products p ON p.id = a.product_id
            WHERE {cond} GROUP BY a.product_id
            ORDER BY MAX(COALESCE(p.display_name, p.name) LIKE ?) DESC, p.category, COALESCE(p.display_name, p.name)
        """, params + [f'%{q}%' if q else '%']).fetchall()]
        if q:
            keep = set(r[0] for r in conn.execute(
                "SELECT DISTINCT product_id FROM product_aliases WHERE alias_text LIKE ?", (f'%{q}%',)))
            keep |= set(r[0] for r in conn.execute(
                "SELECT id FROM products WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(display_name,'')) LIKE ?", (f'%{q}%', f'%{q}%')))
            pids = [p for p in pids if p in keep]
        total = len(pids)
        out = []
        groups = {}
        for loc in LOCATIONS:
            for r in conn.execute("""SELECT alias_text, COUNT(*) n FROM product_aliases WHERE location = ? AND status <> 'struck'
                                     GROUP BY alias_text HAVING n > 1""", (loc,)):
                groups[(loc, r['alias_text'])] = r['n']
        for pid in pids[page * per:(page + 1) * per]:
            p = conn.execute("SELECT id, name, display_name, category, unit FROM products WHERE id = ?", (pid,)).fetchone()
            cn = conn.execute("SELECT card_name, status FROM product_card_names WHERE product_id = ?", (pid,)).fetchone()
            al = conn.execute(f"""SELECT id, location, alias_text, status, source FROM product_aliases a
                                  WHERE product_id = ? AND ({cond}) ORDER BY location, alias_text""", [pid] + params).fetchall()
            out.append({'product_id': pid, 'name': p['name'], 'display_name': p['display_name'], 'category': p['category'],
                        'unit': p['unit'], 'card_name': cn['card_name'] if cn else None,
                        'card_status': cn['status'] if cn else None,
                        'spend90': {l: round(ctx[l]['spend90'].get(pid, 0)) for l in LOCATIONS},
                        'aliases': [dict(a, group=groups.get((a['location'], a['alias_text']), 1)) for a in al]})
        counts = {r['status']: r['n'] for r in conn.execute("SELECT status, COUNT(*) n FROM product_aliases GROUP BY status")}
        cards = {r['status']: r['n'] for r in conn.execute("SELECT status, COUNT(*) n FROM product_card_names GROUP BY status")}
        return jsonify({'products': out, 'total': total, 'page': page, 'per': per, 'alias_counts': counts, 'card_counts': cards})
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/words', methods=['POST'])
@admin_required
def save_words():
    """{approve: [alias ids], strike: [alias ids], cards: {pid: {"name": str, "status": "approved"|"struck"}},
        add: [{location, alias, product_id}]}"""
    d = request.json or {}
    who = _who()
    conn = get_connection()
    try:
        R.ensure_tables(conn)
        for ids, st in ((d.get('approve') or [], 'approved'), (d.get('strike') or [], 'struck')):
            conn.executemany("UPDATE product_aliases SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
                             [(st, who, int(i)) for i in ids])
        for pid, c in (d.get('cards') or {}).items():
            name = (c.get('name') or '').strip()[:60]
            st = c.get('status') if c.get('status') in ('approved', 'struck', 'proposed') else 'approved'
            if name:
                conn.execute("""INSERT INTO product_card_names (product_id, card_name, status, source, reviewed_by, reviewed_at)
                                VALUES (?, ?, ?, 'admin', ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(product_id) DO UPDATE SET card_name = excluded.card_name, status = excluded.status,
                                    reviewed_by = excluded.reviewed_by, reviewed_at = CURRENT_TIMESTAMP""", (int(pid), name, st, who))
        for a in d.get('add') or []:
            key = R.alias_key(a.get('alias'))
            if key and a.get('location') in LOCATIONS and a.get('product_id'):
                conn.execute("""INSERT INTO product_aliases (location, alias_text, raw_text, product_id, source, status, created_by,
                                                             reviewed_by, reviewed_at)
                                VALUES (?, ?, ?, ?, 'admin', 'approved', ?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(location, alias_text, product_id) DO UPDATE SET status = 'approved',
                                    reviewed_by = excluded.reviewed_by, reviewed_at = CURRENT_TIMESTAMP""",
                             (a['location'], key, a['alias'], int(a['product_id']), who, who))
        conn.commit()
        R.clear_cache()
        return jsonify({'success': True})
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/score', methods=['POST'])
@admin_required
def score():
    """Re-run the L7 test set against what's approved now (the go-live gate)."""
    import sys
    import os
    sys.path.insert(0, os.path.join(current_app.root_path, '..', 'tests'))
    from test_voice_recognition import run
    main = run(verbose=False)
    hold = run(verbose=False, fname='voice_phrases_holdout.json')
    R.LIVE_ALIAS = ('approved',)
    R.clear_cache()
    gate = main['auto_pct'] >= 95 and main['list_pct'] == 100 and main['never_hits'] == 0
    return jsonify({'main': main, 'holdout': hold, 'gate_passed': gate})


# ---------------------------------------------------------------------------
# Learned picks
# ---------------------------------------------------------------------------

@transfer_admin_bp.route('/api/transfer-admin/learned', methods=['GET'])
@admin_required
def learned():
    conn = get_connection()
    try:
        R.ensure_tables(conn)
        rows = conn.execute("""SELECT v.*, COALESCE(cn.card_name, p.display_name, p.name) AS item_name
                               FROM voice_picks v JOIN products p ON p.id = v.product_id
                               LEFT JOIN product_card_names cn ON cn.product_id = v.product_id AND cn.status = 'approved'
                               ORDER BY v.last_at DESC LIMIT 300""").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/learned/<int:vid>', methods=['DELETE'])
@admin_required
def delete_learned(vid):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM voice_picks WHERE id = ?", (vid,))
        conn.commit()
        R.clear_cache()
        return jsonify({'success': True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Product links
# ---------------------------------------------------------------------------

def _norm_name(s):
    s = (s or '').lower()
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = re.sub(r'[^a-z ]+', ' ', s)
    return ' '.join(w for w in s.split() if len(w) > 2)


def suggest_pairs(conn, limit=60):
    """Rows one house buys that the other house buys under a DIFFERENT row:
    same vendor, similar name. Suggestions only; nothing is linked until Mike confirms."""
    ctx = {l: R.house_context(conn, l) for l in LOCATIONS}
    done = {(r[0], r[1]) for r in conn.execute("SELECT chatham_product_id, dennis_product_id FROM product_links")}
    def info(pid):
        return conn.execute("""SELECT p.id, COALESCE(p.display_name, p.name) n, p.category, vi.vendor_name
                               FROM products p LEFT JOIN vendor_items vi ON vi.id = p.active_vendor_item_id WHERE p.id = ?""",
                            (pid,)).fetchone()
    c_only = [info(p) for p in ctx['chatham']['live'] - ctx['dennis']['live'] if ctx['chatham']['spend90'].get(p, 0) > 0]
    d_only = [info(p) for p in ctx['dennis']['live'] - ctx['chatham']['live'] if ctx['dennis']['spend90'].get(p, 0) > 0]
    out = []
    for c in c_only:
        cn = _norm_name(c['n'])
        best = None
        for d in d_only:
            if (c['category'] or '') != (d['category'] or ''):
                continue
            sc = SequenceMatcher(None, cn, _norm_name(d['n'])).ratio() + (0.1 if c['vendor_name'] and c['vendor_name'] == d['vendor_name'] else 0)
            if sc >= 0.72 and (not best or sc > best[0]):
                best = (sc, d)
        if best and (c['id'], best[1]['id']) not in done:
            out.append({'chatham_product_id': c['id'], 'chatham_name': c['n'], 'dennis_product_id': best[1]['id'],
                        'dennis_name': best[1]['n'], 'score': round(best[0], 2)})
    out.sort(key=lambda x: -x['score'])
    return out[:limit]


@transfer_admin_bp.route('/api/transfer-admin/links', methods=['GET'])
@admin_required
def links():
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        need = conn.execute("""SELECT t.id, t.from_location, t.to_location, t.from_product_id, t.qty_entered, t.unit_entered,
                                      t.business_date, t.entered_by, COALESCE(p.display_name, p.name) AS item_name
                               FROM inventory_transfers t JOIN products p ON p.id = t.from_product_id
                               WHERE t.needs_link = 1 AND t.status = 'logged' ORDER BY t.id DESC""").fetchall()
        confirmed = conn.execute("""SELECT l.*, COALESCE(a.display_name, a.name) AS chatham_name, COALESCE(b.display_name, b.name) AS dennis_name
                                    FROM product_links l JOIN products a ON a.id = l.chatham_product_id
                                    JOIN products b ON b.id = l.dennis_product_id ORDER BY l.id DESC""").fetchall()
        return jsonify({'needs_link': [dict(r) for r in need], 'suggested': suggest_pairs(conn),
                        'links': [dict(r) for r in confirmed]})
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/links', methods=['POST'])
@admin_required
def save_link():
    """{chatham_product_id, dennis_product_id, status: confirmed|rejected}. Confirming
    fills to_product_id on every transfer waiting on this pair."""
    d = request.json or {}
    c, dn = int(d.get('chatham_product_id') or 0), int(d.get('dennis_product_id') or 0)
    st = d.get('status') if d.get('status') in ('confirmed', 'rejected') else 'confirmed'
    if not c or not dn:
        return jsonify({'error': 'both products required'}), 400
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        conn.execute("""INSERT INTO product_links (chatham_product_id, dennis_product_id, status, source, confirmed_by, confirmed_at)
                        VALUES (?, ?, ?, 'admin', ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(chatham_product_id, dennis_product_id) DO UPDATE SET status = excluded.status,
                            confirmed_by = excluded.confirmed_by, confirmed_at = CURRENT_TIMESTAMP""", (c, dn, st, _who()))
        n = 0
        if st == 'confirmed':
            n += conn.execute("""UPDATE inventory_transfers SET to_product_id = ?, needs_link = 0
                                 WHERE needs_link = 1 AND from_location = 'chatham' AND from_product_id = ?""", (dn, c)).rowcount
            n += conn.execute("""UPDATE inventory_transfers SET to_product_id = ?, needs_link = 0
                                 WHERE needs_link = 1 AND from_location = 'dennis' AND from_product_id = ?""", (c, dn)).rowcount
        conn.commit()
        return jsonify({'success': True, 'transfers_updated': n})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Waste review, settings, problems
# ---------------------------------------------------------------------------

@transfer_admin_bp.route('/api/transfer-admin/waste-review', methods=['GET'])
@admin_required
def waste_review():
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        rows = conn.execute("""SELECT w.*, COALESCE(p.display_name, p.name) AS item_name FROM waste_log w
                               JOIN products p ON p.id = w.product_id
                               WHERE w.flagged = 1 AND w.reviewed_at IS NULL AND w.status = 'logged' ORDER BY w.id DESC""").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/waste-review/<int:wid>', methods=['POST'])
@admin_required
def waste_reviewed(wid):
    conn = get_connection()
    try:
        conn.execute("UPDATE waste_log SET reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?", (_who(), wid))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


SETTINGS = {'ai_fallback': ('on', 'off'), 'email_mode': ('per_entry', 'daily_digest', 'off')}


@transfer_admin_bp.route('/api/transfer-admin/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    conn = get_connection()
    try:
        R.ensure_tables(conn)
        if request.method == 'POST':
            d = request.json or {}
            for k, v in d.items():
                if k in SETTINGS and v in SETTINGS[k]:
                    R.set_setting(conn, k, v, _who())
                elif k in ('ai_monthly_cap_usd', 'waste_flag_usd'):
                    R.set_setting(conn, k, f'{max(0.0, float(v)):g}', _who())
            conn.commit()
        out = {k: R.get_setting(conn, k) for k in R.SETTING_DEFAULTS}
        out['ai_spend_month'] = round(R.month_spend(conn), 4)
        from integrations.claude_models import CLAUDE_FAST_MODEL
        out['ai_model'] = CLAUDE_FAST_MODEL
        return jsonify(out)
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/problems', methods=['GET'])
@admin_required
def problems():
    from reports import move_notify
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        calls = conn.execute("""SELECT id, purpose, location, phrase, model, cost_usd, latency_ms, result, error, person, created_at
                                FROM voice_ai_calls ORDER BY id DESC LIMIT 50""").fetchall()
        return jsonify({'email_failures': move_notify.failures(conn), 'ai_calls': [dict(r) for r in calls],
                        'needs_link': conn.execute("SELECT COUNT(*) FROM inventory_transfers WHERE needs_link = 1 AND status = 'logged'").fetchone()[0],
                        'waste_to_review': conn.execute("SELECT COUNT(*) FROM waste_log WHERE flagged = 1 AND reviewed_at IS NULL AND status = 'logged'").fetchone()[0]})
    finally:
        conn.close()


@transfer_admin_bp.route('/api/transfer-admin/test-email', methods=['POST'])
@admin_required
def test_email():
    from reports import move_notify
    try:
        move_notify.smtp_send('Test: transfer/waste emails are working',
                              '<p>This is the test email from /transfer/admin. Entries will look like the next ones you get.</p>')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
