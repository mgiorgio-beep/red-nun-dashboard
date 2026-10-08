"""
Stock moves by voice: inter-house transfers and waste (brief 2026-10-08).

One state machine behind both Siri Shortcuts and the web pages:

  start(kind, text, client_id, actor)  -> asks what's missing ("Which one?") or shows the card
  answer(pending_id, choice, actor)    -> applies a pick, then the same
  confirm(pending_id, actor)           -> writes the row (once; a retry is a no-op)
  fix(pending_id, fields, actor)       -> the Fix page's edits

Nothing is written to inventory_transfers / waste_log before Confirm. Until then
the dictation lives in move_pending and expires after 15 minutes.

actor = {'person', 'token_id', 'role' ('owner' sees $), 'home' (house or None), 'via' ('siri'|'web')}
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from reports import house_moves as H
from reports import item_recognition as R

ET = ZoneInfo('America/New_York')
PENDING_MINUTES = 15
KINDS = ('transfer', 'waste')
BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://dashboard.rednun.com')
HOUSE = {'chatham': 'Chatham', 'dennis': 'Dennis'}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def ensure_tables(conn):
    R.ensure_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,            -- idempotency: one row per client_id
            from_location TEXT NOT NULL CHECK (from_location IN ('chatham', 'dennis')),
            to_location TEXT NOT NULL CHECK (to_location IN ('chatham', 'dennis')),
            from_product_id INTEGER NOT NULL REFERENCES products(id),
            to_product_id INTEGER REFERENCES products(id),     -- NULL = needs a product link
            qty_entered REAL NOT NULL,
            unit_entered TEXT,
            qty_base REAL,
            base_unit TEXT,
            unit_cost_base REAL,
            total_cost REAL,
            cost_source TEXT,                          -- last_invoice | deal | catalog | recipe | manual | none
            cost_detail TEXT,
            category_type TEXT,
            raw_text TEXT,
            entered_by TEXT,
            entered_via TEXT,                          -- siri | web
            token_id INTEGER,
            transferred_at TEXT NOT NULL,
            business_date TEXT NOT NULL,               -- YYYYMMDD, 4AM ET boundary
            status TEXT NOT NULL DEFAULT 'logged',     -- logged | voided
            voided_by TEXT,
            voided_at TEXT,
            void_reason TEXT,
            notes TEXT,
            needs_link INTEGER NOT NULL DEFAULT 0,
            is_settlement INTEGER NOT NULL DEFAULT 0,  -- a return in kind (brief 8H4)
            settlement_line_id INTEGER,
            fixes TEXT,                                -- JSON: what changed on the Fix page
            match_trace TEXT,
            CHECK (from_location <> to_location)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_transfers_date ON inventory_transfers(business_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS waste_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            location TEXT NOT NULL CHECK (location IN ('chatham', 'dennis')),
            product_id INTEGER REFERENCES products(id),
            recipe_id INTEGER REFERENCES recipes(id),  -- prepped item (product.source_recipe_id)
            qty_entered REAL NOT NULL,
            unit_entered TEXT,
            qty_base REAL,
            base_unit TEXT,
            unit_cost_base REAL,
            total_cost REAL,
            cost_source TEXT,
            cost_detail TEXT,
            category_type TEXT,
            reason_code TEXT,                          -- spoiled | dropped | kitchen_error | overprep | bar | staff_meal | other | NULL
            reason_words TEXT,
            raw_text TEXT,
            entered_by TEXT,
            entered_via TEXT,
            token_id INTEGER,
            logged_at TEXT NOT NULL,
            business_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'logged',
            voided_by TEXT,
            voided_at TEXT,
            void_reason TEXT,
            notes TEXT,
            flagged INTEGER NOT NULL DEFAULT 0,        -- over the review threshold -> Mike's list
            reviewed_by TEXT,
            reviewed_at TEXT,
            fixes TEXT,
            match_trace TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_waste_date ON waste_log(location, business_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chatham_product_id INTEGER NOT NULL REFERENCES products(id),
            dennis_product_id INTEGER NOT NULL REFERENCES products(id),
            status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | confirmed | rejected
            source TEXT,
            confirmed_by TEXT,
            confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chatham_product_id, dennis_product_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfer_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,           -- sha256; the raw token is shown once
            person_name TEXT NOT NULL,
            home_location TEXT CHECK (home_location IN ('chatham', 'dennis') OR home_location IS NULL),
            role TEXT NOT NULL DEFAULT 'staff' CHECK (role IN ('owner', 'staff')),
            active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS move_pending (
            pending_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            client_id TEXT NOT NULL,
            token_id INTEGER,
            person TEXT,
            role TEXT,
            home TEXT,
            via TEXT,
            state TEXT NOT NULL,                       -- JSON (parsed + picked)
            ask TEXT,                                  -- JSON: what we're asking + its options
            status TEXT NOT NULL DEFAULT 'open',       -- open | confirmed | expired
            result_id INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(kind, client_id)
        )
    """)


def now_et():
    return datetime.now(ET)


def business_date(dt=None):
    """YYYYMMDD; before 4 AM ET belongs to the previous day (same rule as Toast)."""
    dt = (dt or now_et()).astimezone(ET)
    return (dt - timedelta(hours=4)).strftime('%Y%m%d')


# ---------------------------------------------------------------------------
# Tokens (Shortcuts can't hold a login)
# ---------------------------------------------------------------------------

def _hash(raw):
    return hashlib.sha256((raw or '').encode()).hexdigest()


def create_token(conn, person, home, role, who):
    ensure_tables(conn)
    raw = 'rn_' + secrets.token_urlsafe(24)
    conn.execute("""INSERT INTO transfer_tokens (token_hash, person_name, home_location, role, created_by)
                    VALUES (?, ?, ?, ?, ?)""", (_hash(raw), person.strip(), home or None, role, who))
    return raw


def check_token(conn, raw):
    """The active token row, or None. Touches last_used_at."""
    if not raw:
        return None
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM transfer_tokens WHERE token_hash = ? AND active = 1", (_hash(raw),)).fetchone()
    if r:
        conn.execute("UPDATE transfer_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (r['id'],))
        conn.commit()
    return r


def actor_from_token(t, via='siri'):
    return {'person': t['person_name'], 'token_id': t['id'], 'role': t['role'], 'home': t['home_location'], 'via': via}


def actor_from_session(sess, via='web'):
    loc = (sess.get('location') or '').lower()
    return {'person': sess.get('full_name') or sess.get('username') or 'web', 'token_id': None,
            'role': 'owner' if sess.get('role') == 'admin' else 'staff',
            'home': loc if loc in H.LOCATIONS else None, 'via': via}


# ---------------------------------------------------------------------------
# Fix-page links: signed, tied to one pending row, dead after confirm / 15 min
# ---------------------------------------------------------------------------

def _secret():
    return (os.getenv('SECRET_KEY') or 'rednun').encode()


def sign(pending_id, token_id, expires_at):
    msg = f'{pending_id}|{token_id or ""}|{expires_at}'.encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:32]


def fix_url(p):
    kind = p['kind']
    return f"{BASE_URL}/{kind}/fix?p={p['pending_id']}&s={sign(p['pending_id'], p['token_id'], p['expires_at'])}"


def load_signed(conn, pending_id, sig):
    """The pending row if the link is genuine, still open and not expired; else (None, why)."""
    ensure_tables(conn)
    p = conn.execute("SELECT * FROM move_pending WHERE pending_id = ?", (pending_id or '',)).fetchone()
    if not p or not sig or not hmac.compare_digest(sign(p['pending_id'], p['token_id'], p['expires_at']), sig):
        return None, 'This link is not valid.'
    if p['status'] == 'confirmed':
        return None, 'Already logged. Void it on the transfer page if it was wrong.'
    if p['status'] != 'open' or p['expires_at'] < now_et().isoformat():
        return None, 'This link timed out (15 min). Say it again.'
    return p, None


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

def _err(say, **kw):
    return dict(status='error', say=say, **kw)


def _load(conn, pending_id):
    p = conn.execute("SELECT * FROM move_pending WHERE pending_id = ?", (pending_id or '',)).fetchone()
    return p


def _save(conn, p, state, ask):
    conn.execute("UPDATE move_pending SET state = ?, ask = ? WHERE pending_id = ?",
                 (json.dumps(state), json.dumps(ask) if ask else None, p['pending_id']))
    conn.commit()
    return _load(conn, p['pending_id'])


def _product(conn, pid):
    return conn.execute("""SELECT id, name, display_name, category, unit, inventory_unit, pack_size, location,
                                  current_price, active_vendor_item_id, source_recipe_id, active
                           FROM products WHERE id = ?""", (pid,)).fetchone()


def _sender(st):
    return st['from'] if st['kind'] == 'transfer' else st['location']


def _logged(conn, kind, client_id):
    t = 'inventory_transfers' if kind == 'transfer' else 'waste_log'
    return conn.execute(f"SELECT id FROM {t} WHERE client_id = ?", (client_id,)).fetchone()


def start(conn, kind, text, client_id, actor):
    """A new dictation (or a retry of one: same client_id gives the same answer)."""
    ensure_tables(conn)
    client_id = (client_id or '').strip() or ('srv-' + secrets.token_hex(8))
    done = _logged(conn, kind, client_id)
    if done:
        return {'status': 'logged', 'say': 'Already logged.', 'id': done['id'], f'{kind}_id': done['id']}
    old = conn.execute("SELECT * FROM move_pending WHERE kind = ? AND client_id = ?", (kind, client_id)).fetchone()
    if old and old['status'] == 'open' and old['expires_at'] > now_et().isoformat():
        return respond(conn, old, actor)
    text = (text or '').strip()
    if not text:
        return _err("I didn't hear anything. Try again.")
    p = H.parse(text, kind)
    if kind == 'waste' and p['comp']:
        return _err('Comps go in Toast.')
    st = {'kind': kind, 'raw': text, 'item': p['item'], 'size': p['size'], 'qty': p['qty'],
          'unit': p['unit'], 'unit_said': bool(p['unit']), 'vague': p['vague_qty'],
          'reason': p['reason'], 'reason_words': p['reason_words'], 'product_id': None, 'via': None,
          'trace': None, 'changes': [], 'from': None, 'to': None, 'location': None, 'notes': None}
    named = [h for _, h in p['named']]
    if kind == 'transfer':
        st['from'], st['to'] = p['from'], p['to']
        if not st['from'] and len(set(named)) == 1 and actor.get('home') and named[0] != actor['home']:
            # "two cases titos dennis" from a Chatham phone: Chatham -> Dennis
            st['from'], st['to'] = actor['home'], named[0]
        if p['named'] and any(pr in ('back to',) for pr, _ in p['named']):
            st['notes'] = 'said "back to"'
    else:
        st['location'] = named[0] if named else actor.get('home')
    now = now_et()
    pid = secrets.token_urlsafe(12)
    conn.execute("""INSERT INTO move_pending (pending_id, kind, client_id, token_id, person, role, home, via, state,
                                              created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (pid, kind, client_id, actor.get('token_id'), actor.get('person'), actor.get('role'),
                  actor.get('home'), actor.get('via'), json.dumps(st), now.isoformat(),
                  (now + timedelta(minutes=PENDING_MINUTES)).isoformat()))
    conn.commit()
    return respond(conn, _load(conn, pid), actor)


def _resolve(conn, p, st, actor):
    """Fill in what can be worked out without asking. Returns the ask, or None when complete."""
    kind = st['kind']
    # 1. which house(s)
    if kind == 'transfer' and not (st['from'] and st['to']):
        return {'what': 'direction', 'say': 'Which way?',
                'options': [{'id': 'cd', 'label': 'Chatham → Dennis', 'value': ['chatham', 'dennis']},
                            {'id': 'dc', 'label': 'Dennis → Chatham', 'value': ['dennis', 'chatham']}]}
    if kind == 'waste' and not st['location']:
        return {'what': 'house', 'say': 'Chatham or Dennis?',
                'options': [{'id': 'c', 'label': 'Chatham', 'value': 'chatham'},
                            {'id': 'd', 'label': 'Dennis', 'value': 'dennis'}]}
    house = _sender(st)
    # 2. which product
    if not st['product_id']:
        if not st['item']:
            return {'what': 'none', 'say': f"I didn't catch the item. Try again: "
                                           f"{'2 cases of fries to Dennis' if kind == 'transfer' else 'half a case of haddock, went bad'}."}
        r = R.recognize(conn, house, st['item'], size=st['size'], person=actor.get('person'), unit=st['unit'])
        st['trace'] = r.get('trace')
        if 'product' in r:
            st['product_id'], st['via'] = r['product']['id'], r['via']
        elif r.get('options'):
            return {'what': 'product', 'say': 'Which one?',
                    'options': [{'id': chr(97 + i), 'label': H.option_label(conn, o), 'value': o['id']}
                                for i, o in enumerate(r['options'][:4])]}
        else:
            return {'what': 'none', 'say': f"I couldn't find \"{st['item']}\" at {HOUSE[house]}. "
                                           f"Try again with the name on the box, or use the Fix page."}
    prod = _product(conn, st['product_id'])
    # 3. how many
    if st['qty'] is None:
        u = st['unit'] or H.ordering_unit(prod)
        qs = [0.5, 1, 2, 3] if kind == 'waste' else [1, 2, 3, 4]
        return {'what': 'qty', 'say': 'How many?' if kind == 'transfer' else 'How much?',
                'options': [{'id': str(i + 1), 'label': f'{H.fmt_qty(q)} {H.plural(q, u)}', 'value': [q, u]}
                            for i, q in enumerate(qs)]}
    # 4. in what unit (the product's ordering unit when nobody said one; the card spells it out)
    if not st['unit']:
        st['unit'] = H.ordering_unit(prod)
    if not H.to_base(conn, prod, st['qty'], st['unit']):
        units = H.units_for(conn, prod)
        return {'what': 'unit', 'say': f"{H.fmt_qty(st['qty'])} what?",
                'options': [{'id': str(i + 1), 'label': f"{H.fmt_qty(st['qty'])} {H.plural(st['qty'], u)}", 'value': u}
                            for i, u in enumerate(units[:4])]}
    return None


def money(x):
    return '' if x is None else f'${x:,.2f}'


def preview(conn, st):
    """What the row would hold: quantities, cost, link."""
    prod = _product(conn, st['product_id'])
    house = _sender(st)
    qb = H.to_base(conn, prod, st['qty'], st['unit'])
    ec = H.effective_cost(conn, prod['id'], house)
    cost = st.get('cost_override')
    src = 'manual' if cost is not None else ec['source']
    unit_cost = cost if cost is not None else ec['cost']
    total = round(qb[0] * unit_cost, 2) if (qb and unit_cost is not None) else None
    out = {'product': prod, 'card_name': R.card_name(conn, prod), 'qty_base': qb[0] if qb else None,
           'base_unit': qb[1] if qb else None, 'conv': qb[2] if qb else None,
           'unit_cost_base': unit_cost, 'total_cost': total, 'cost_source': src, 'cost_detail': ec.get('detail'),
           'category_type': (prod['category'] or '').upper()}
    if st['kind'] == 'transfer':
        out.update(link_for(conn, prod['id'], st['from'], st['to']))
    return out


def link_for(conn, from_pid, src, dst):
    """The receiving house's product for a sending house's product.
    Product ids are shared by both houses for most items (one row, bought by
    both), so the same id is right when the receiver stocks it. Otherwise a
    confirmed product_links pair; otherwise NULL and the row is flagged."""
    ensure_tables(conn)
    col_src, col_dst = f'{src}_product_id', f'{dst}_product_id'
    r = conn.execute(f"SELECT {col_dst} FROM product_links WHERE {col_src} = ? AND status = 'confirmed'",
                     (from_pid,)).fetchone()
    if r:
        return {'to_product_id': r[0], 'needs_link': 0}
    if from_pid in R.house_context(conn, dst)['live']:
        return {'to_product_id': from_pid, 'needs_link': 0}
    return {'to_product_id': None, 'needs_link': 1}


def card(conn, st, owner):
    """The text on the Shortcut's confirm card. $ only for the owner."""
    pv = preview(conn, st)
    qty_line = f"{H.fmt_qty(st['qty'])} {H.plural(st['qty'], st['unit']).upper()}"
    if st['kind'] == 'transfer':
        lines = ['TRANSFER', pv['card_name'], qty_line, f"{HOUSE[st['from']]} → {HOUSE[st['to']]}"]
    else:
        lines = [f"WASTE — {HOUSE[st['location']]}", pv['card_name'], qty_line,
                 f"Reason: {R_LABEL(st['reason'])}"]
    if owner:
        lines.append(money(pv['total_cost']) + (f" ({pv['cost_source']})" if pv['total_cost'] is not None else 'no price yet'))
    return '\n'.join(lines), pv


def R_LABEL(code):
    return H.REASONS.get(code, 'none given') if code else 'none given'


def respond(conn, p, actor):
    """The answer for a pending row: the next question, or the confirm card."""
    st = json.loads(p['state'])
    owner = (actor or {}).get('role') == 'owner' or p['role'] == 'owner'
    ask = _resolve(conn, p, st, actor or {})
    p = _save(conn, p, st, ask)
    if ask and ask['what'] == 'none':
        return _err(ask['say'], pending_id=p['pending_id'], fix_url=fix_url(p))
    if ask:
        return {'status': 'choose', 'say': ask['say'], 'pending_id': p['pending_id'],
                'options': [{'id': o['id'], 'label': o['label']} for o in ask['options']],
                'fix_url': fix_url(p)}
    text, pv = card(conn, st, owner)
    unit_note = '' if st['unit_said'] else f" ({H.plural(st['qty'], st['unit'])})"
    if st['kind'] == 'transfer':
        say = f"{H.fmt_qty(st['qty'])} {H.plural(st['qty'], st['unit'])} {pv['card_name']} to {HOUSE[st['to']]}?"
    else:
        say = f"{H.fmt_qty(st['qty'])} {H.plural(st['qty'], st['unit'])} {pv['card_name']}, {R_LABEL(st['reason'])}?"
    if owner and pv['total_cost'] is not None:
        say += f" {money(pv['total_cost'])}."
    return {'status': 'confirm', 'card': text, 'say': say, 'pending_id': p['pending_id'], 'fix_url': fix_url(p),
            'unit_assumed': not st['unit_said'], 'unit_note': unit_note.strip()}


def answer(conn, pending_id, choice, actor):
    ensure_tables(conn)
    p = _load(conn, pending_id)
    if not p:
        return _err("I lost that one. Say it again.")
    if p['status'] == 'confirmed':
        return {'status': 'logged', 'say': 'Already logged.', 'id': p['result_id'], f"{p['kind']}_id": p['result_id']}
    if p['expires_at'] < now_et().isoformat():
        return _err('That one timed out. Say it again.')
    st, ask = json.loads(p['state']), json.loads(p['ask'] or 'null')
    if not ask:
        return respond(conn, p, actor)
    opt = next((o for o in ask['options'] if o['id'] == str(choice) or o['label'] == str(choice)), None)
    if not opt:
        return {'status': 'choose', 'say': ask['say'], 'pending_id': p['pending_id'],
                'options': [{'id': o['id'], 'label': o['label']} for o in ask['options']], 'fix_url': fix_url(p)}
    w = ask['what']
    if w == 'direction':
        st['from'], st['to'] = opt['value']
    elif w == 'house':
        st['location'] = opt['value']
    elif w == 'product':
        st['product_id'], st['via'] = opt['value'], 'picked'
        R.learn(conn, _sender(st), st['item'], opt['value'], p['person'])   # L6: next time it ranks first
    elif w == 'qty':
        st['qty'], st['unit'] = opt['value']
        st['unit_said'] = True
    elif w == 'unit':
        st['unit'], st['unit_said'] = opt['value'], True
    conn.execute("UPDATE move_pending SET state = ?, ask = NULL WHERE pending_id = ?", (json.dumps(st), pending_id))
    conn.commit()
    return respond(conn, _load(conn, pending_id), actor)


def fix(conn, p, fields, actor):
    """Fix-page edits. Records what changed so the email can show it."""
    st = json.loads(p['state'])
    ch = st.setdefault('changes', [])
    if 'product_id' in fields and fields['product_id'] and int(fields['product_id']) != st.get('product_id'):
        old = _product(conn, st['product_id']) if st.get('product_id') else None
        new = _product(conn, int(fields['product_id']))
        if not new or not new['active']:
            return _err('Unknown product.')
        ch.append(f"item: said \"{st['item']}\"" + (f" ({R.card_name(conn, old)})" if old else '')
                  + f", changed to {R.card_name(conn, new)}")
        st['product_id'], st['via'] = new['id'], 'fixed'
        R.learn(conn, _sender(st), st['item'], new['id'], p['person'])
    if 'qty' in fields and fields['qty'] not in (None, ''):
        q = float(fields['qty'])
        if q <= 0:
            return _err('Quantity must be more than zero.')
        if q != st.get('qty'):
            ch.append(f"qty: {H.fmt_qty(st.get('qty'))} -> {H.fmt_qty(q)}")
            st['qty'] = q
    if fields.get('unit') and fields['unit'] != st.get('unit'):
        ch.append(f"unit: {st.get('unit')} -> {fields['unit']}")
        st['unit'], st['unit_said'] = fields['unit'], True
    if st['kind'] == 'transfer' and fields.get('from') in H.LOCATIONS and fields['from'] != st.get('from'):
        ch.append(f"direction: {HOUSE.get(st.get('from'), '?')} -> {HOUSE.get(st.get('to'), '?')} swapped")
        st['from'], st['to'] = fields['from'], H.OTHER[fields['from']]
        st['product_id'] = st['product_id']   # the item stays; the cost re-reads at the new sender
    if st['kind'] == 'waste' and fields.get('location') in H.LOCATIONS and fields['location'] != st.get('location'):
        ch.append(f"house: {st.get('location')} -> {fields['location']}")
        st['location'] = fields['location']
    if st['kind'] == 'waste' and 'reason' in fields and fields['reason'] != st.get('reason'):
        if fields['reason'] and fields['reason'] not in H.REASONS:
            return _err('Unknown reason.')
        ch.append(f"reason: {R_LABEL(st.get('reason'))} -> {R_LABEL(fields['reason'])}")
        st['reason'] = fields['reason'] or None
    if 'notes' in fields:
        st['notes'] = (fields['notes'] or '').strip()[:300] or None
    if 'cost' in fields and actor.get('role') == 'owner':
        st['cost_override'] = float(fields['cost']) if fields['cost'] not in (None, '') else None
    conn.execute("UPDATE move_pending SET state = ?, ask = NULL WHERE pending_id = ?", (json.dumps(st), p['pending_id']))
    conn.commit()
    return respond(conn, _load(conn, p['pending_id']), actor)


def confirm(conn, pending_id, actor):
    """Write the row. All-or-nothing; a second confirm returns the first result."""
    ensure_tables(conn)
    p = _load(conn, pending_id)
    if not p:
        return _err("I lost that one. Say it again.")
    if p['status'] == 'confirmed':
        return {'status': 'logged', 'say': 'Done.', 'id': p['result_id'], f"{p['kind']}_id": p['result_id']}
    if p['expires_at'] < now_et().isoformat():
        return _err('That one timed out. Say it again.')
    st = json.loads(p['state'])
    if _resolve(conn, p, st, actor or {}):
        return respond(conn, p, actor)          # something still missing: ask it
    pv = preview(conn, st)
    now = now_et()
    conn.commit()                               # close any implicit read transaction first
    try:
        conn.execute('BEGIN IMMEDIATE')
        again = conn.execute("SELECT status, result_id FROM move_pending WHERE pending_id = ?", (pending_id,)).fetchone()
        if again['status'] == 'confirmed':     # the other worker got there first
            conn.rollback()
            return {'status': 'logged', 'say': 'Done.', 'id': again['result_id'], f"{p['kind']}_id": again['result_id']}
        common = dict(client_id=p['client_id'], qty_entered=st['qty'], unit_entered=st['unit'],
                      qty_base=pv['qty_base'], base_unit=pv['base_unit'], unit_cost_base=pv['unit_cost_base'],
                      total_cost=pv['total_cost'], cost_source=pv['cost_source'], cost_detail=pv['cost_detail'],
                      category_type=pv['category_type'], raw_text=st['raw'], entered_by=p['person'],
                      entered_via=p['via'], token_id=p['token_id'], business_date=business_date(now),
                      notes=st.get('notes'), fixes=json.dumps(st['changes']) if st.get('changes') else None,
                      match_trace=f"{st.get('via')}: {st.get('trace')}")
        if p['kind'] == 'transfer':
            ret = settlement_return_for(conn, st, pv)
            row = dict(common, from_location=st['from'], to_location=st['to'], from_product_id=st['product_id'],
                       to_product_id=pv['to_product_id'], needs_link=pv['needs_link'], transferred_at=now.isoformat(),
                       is_settlement=1 if ret else 0, settlement_line_id=ret['id'] if ret else None)
            if ret:     # a return in kind is valued at the original transfer cost so the item nets to $0
                row.update(unit_cost_base=ret['unit_cost_base'], cost_source='settlement',
                           total_cost=round((pv['qty_base'] or 0) * ret['unit_cost_base'], 2) if ret['unit_cost_base'] is not None else None)
            table = 'inventory_transfers'
        else:
            flag_at = float(R.get_setting(conn, 'waste_flag_usd') or 100)
            prod = pv['product']
            row = dict(common, location=st['location'], product_id=st['product_id'], recipe_id=prod['source_recipe_id'],
                       reason_code=st.get('reason'), reason_words=st.get('reason_words'), logged_at=now.isoformat(),
                       flagged=1 if (pv['total_cost'] or 0) > flag_at else 0)
            table = 'waste_log'
        cols = ','.join(row)
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        new_id = cur.lastrowid
        if p['kind'] == 'transfer' and row.get('settlement_line_id'):
            close_settlement_line(conn, row['settlement_line_id'], new_id, pv['qty_base'])
        conn.execute("UPDATE move_pending SET status = 'confirmed', result_id = ? WHERE pending_id = ?", (new_id, pending_id))
        _demote_on_relog(conn, p, st)
        R.touch(conn, _sender(st), st['item'], st['product_id'])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    say = 'Done.'
    if p['kind'] == 'transfer' and row.get('settlement_line_id'):
        say = f"Logged. That clears what {HOUSE[st['from']]} owed {HOUSE[st['to']]}."
    from reports import move_notify
    move_notify.queue(p['kind'], new_id, 'logged')
    return {'status': 'logged', 'say': say, 'id': new_id, f"{p['kind']}_id": new_id}


def _demote_on_relog(conn, p, st):
    """L6: the same person voided an entry for this phrase in the last 15 min and
    re-logged it as another product -> the old alias was wrong."""
    t, col = ('inventory_transfers', 'from_product_id') if st['kind'] == 'transfer' else ('waste_log', 'product_id')
    cutoff = (now_et() - timedelta(minutes=PENDING_MINUTES)).isoformat()
    for r in conn.execute(f"""SELECT {col} AS pid, raw_text FROM {t} WHERE status = 'voided' AND entered_by = ?
                              AND voided_at >= ? AND {col} <> ?""", (p['person'], cutoff, st['product_id'])).fetchall():
        old = H.parse(r['raw_text'] or '', st['kind'])
        if R.alias_key(old['item']) == R.alias_key(st['item']):
            R.demote(conn, _sender(st), st['item'], r['pid'])


def void(conn, kind, row_id, who, reason=None):
    ensure_tables(conn)
    t = 'inventory_transfers' if kind == 'transfer' else 'waste_log'
    n = conn.execute(f"""UPDATE {t} SET status = 'voided', voided_by = ?, voided_at = ?, void_reason = ?
                         WHERE id = ? AND status = 'logged'""", (who, now_et().isoformat(), reason, row_id)).rowcount
    conn.commit()
    if n:
        from reports import move_notify
        move_notify.queue(kind, row_id, 'voided')
    return n


# Returns in kind (brief 8H4) are wired in reports/intercompany.py; these two hooks
# keep the voice path from importing the whole settlement module when unused.
def settlement_return_for(conn, st, pv):
    try:
        from reports.intercompany import open_return_for
    except ImportError:
        return None
    return open_return_for(conn, st['from'], st['to'], st['product_id'], pv.get('to_product_id'))


def close_settlement_line(conn, line_id, transfer_id, qty_base):
    from reports.intercompany import close_return
    close_return(conn, line_id, transfer_id, qty_base)


def log_direct(conn, kind, client_id, fields, actor):
    """The web pickers (product, quantity and unit already chosen): same checks and
    same row as the voice path, confirmed in one step. Idempotent on client_id."""
    ensure_tables(conn)
    done = _logged(conn, kind, client_id)
    if done:
        return {'status': 'logged', 'say': 'Already logged.', 'id': done['id']}
    st = {'kind': kind, 'raw': fields.get('raw_text') or '(web picker)', 'item': fields.get('item') or '',
          'size': None, 'qty': float(fields['qty']), 'unit': fields.get('unit'), 'unit_said': bool(fields.get('unit')),
          'vague': False, 'reason': fields.get('reason'), 'reason_words': None, 'product_id': int(fields['product_id']),
          'via': 'picker', 'trace': None, 'changes': [], 'from': fields.get('from'), 'to': fields.get('to'),
          'location': fields.get('location'), 'notes': fields.get('notes')}
    if kind == 'transfer' and st['from'] in H.LOCATIONS and not st['to']:
        st['to'] = H.OTHER[st['from']]
    old = conn.execute("SELECT pending_id FROM move_pending WHERE kind = ? AND client_id = ?", (kind, client_id)).fetchone()
    if old:
        pid = old['pending_id']
        conn.execute("UPDATE move_pending SET state = ?, status = 'open', expires_at = ? WHERE pending_id = ?",
                     (json.dumps(st), (now_et() + timedelta(minutes=PENDING_MINUTES)).isoformat(), pid))
    else:
        pid = secrets.token_urlsafe(12)
        now = now_et()
        conn.execute("""INSERT INTO move_pending (pending_id, kind, client_id, token_id, person, role, home, via, state,
                                                  created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (pid, kind, client_id, actor.get('token_id'), actor.get('person'), actor.get('role'), actor.get('home'),
                      actor.get('via'), json.dumps(st), now.isoformat(), (now + timedelta(minutes=PENDING_MINUTES)).isoformat()))
    conn.commit()
    return confirm(conn, pid, actor)


def cancel(conn, pending_id):
    conn.execute("UPDATE move_pending SET status = 'expired' WHERE pending_id = ? AND status = 'open'", (pending_id,))
    conn.commit()
    return {'status': 'cancelled', 'say': 'Nothing logged.'}


def fix_view(conn, p, actor):
    """Everything the Fix page needs for one pending row."""
    st = json.loads(p['state'])
    owner = actor.get('role') == 'owner'
    house = _sender(st) if (st.get('from') or st.get('location')) else None
    out = {'kind': p['kind'], 'pending_id': p['pending_id'], 'said': st['raw'], 'item_said': st['item'],
           'from': st.get('from'), 'to': st.get('to'), 'location': st.get('location'), 'qty': st.get('qty'),
           'unit': st.get('unit'), 'reason': st.get('reason'), 'notes': st.get('notes'), 'owner': owner,
           'reasons': [{'code': k, 'label': v} for k, v in H.REASONS.items()], 'expires_at': p['expires_at'],
           'product': None, 'matches': [], 'units': []}
    if house and st.get('item'):
        r = R.recognize(conn, house, st['item'], size=st.get('size'), person=p['person'], allow_ai=False, unit=st.get('unit'))
        rows = [r['product']] if 'product' in r else list(r.get('options') or [])
        if len(rows) < 6:
            m = R._matcher(conn, house)
            said = R.normalize(st['item']).split()
            for c in m.candidates(R.normalize(st['item']), 12):
                toks = m.nametok.get(c['product_id'], set())
                if c['score'] < 100 or any(t in R.ALWAYS_SAID and not R._said(said, t) for t in toks):
                    continue        # every word said, and never sweet potato fries for "fries"
                if all(x['id'] != c['product_id'] for x in rows):
                    rows.append(_product(conn, c['product_id']))
                if len(rows) >= 6:
                    break
        out['matches'] = [{'id': x['id'], 'label': H.option_label(conn, x)} for x in rows[:6]]
    if st.get('product_id'):
        prod = _product(conn, st['product_id'])
        out['product'] = {'id': prod['id'], 'label': H.option_label(conn, prod), 'name': R.card_name(conn, prod)}
        out['units'] = H.units_for(conn, prod)
        if st.get('unit') and st['unit'] not in out['units']:
            out['units'].insert(0, st['unit'])
        if owner and st.get('qty') is not None and (st.get('from') or st.get('location')):
            pv = preview(conn, st)
            out['cost'] = {'total': pv['total_cost'], 'unit_cost': pv['unit_cost_base'], 'base_unit': pv['base_unit'],
                           'source': pv['cost_source'], 'detail': pv['cost_detail']}
    return out


def search_products(conn, house, q, limit=12):
    """Fix-page / web search: this house's products first, then everything else."""
    q = (q or '').strip()
    if len(q) < 2:
        return []
    ctx = R.house_context(conn, house) if house else {'ids': set(), 'live': set()}
    m = R._matcher(conn, house) if house else None
    seen, out = set(), []
    if m:
        for c in m.candidates(R.normalize(q), limit):
            seen.add(c['product_id'])
            out.append(_product(conn, c['product_id']))
    if len(out) < limit:
        words = q.split()
        where = ' AND '.join(["(name LIKE ? OR COALESCE(display_name, '') LIKE ?)"] * len(words))
        for r in conn.execute(f"SELECT id FROM products WHERE active = 1 AND {where} LIMIT 40",
                              [x for w in words for x in (f'%{w}%', f'%{w}%')]):
            if r['id'] not in seen:
                seen.add(r['id'])
                out.append(_product(conn, r['id']))
    out.sort(key=lambda x: (x['id'] not in ctx['live'], x['id'] not in ctx['ids']))
    return [{'id': x['id'], 'label': H.option_label(conn, x), 'here': x['id'] in ctx['live']} for x in out[:limit]]
