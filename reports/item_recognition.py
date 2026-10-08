"""
What product did they mean? (Transfers + waste, spoken in the walk-in.)

Layers, in order (brief 4A, Mike 2026-10-08):
  L1 normalize    filler, plurals, apostrophes, Siri mishearings (MISHEARD)
  L2 aliases      kitchen words Mike approved ("fries", "frialator oil", "jamo").
                  One alias can name several products (a group); L3/L4 narrow it.
  L3 veto         a product with a distinguishing word the speaker didn't say
                  (SWEET potato fries, bud LIGHT) is dropped when a sibling
                  without that word is in the running. A few words (sweet, decaf,
                  diet, zero) must always be said.
  L4 context      bought here lately, on this house's weekly count, moved before,
                  what this person picked last time. One clear leader -> card;
                  otherwise "Which one?" with up to 4, most likely first.
  L5 Claude       only when L1-L4 find nothing usable. Picks from this house's own
                  list or says unknown; anything else is thrown away. Capped per
                  month, has an off switch, every call logged (voice_ai_calls).
  L6 learning     a pick or a Fix-page change saves a learned alias for
                  (house, phrase, person). Used automatically after the same pick
                  twice; until then it only ranks first. A quick void + re-log as
                  something else demotes it.
The confirm card is the last check: nothing here writes a stock move.
"""

import json
import math
import os
import re
import time

from reports.item_match import Matcher, norm, _sing, _tok_match

LOCATIONS = ('chatham', 'dennis')

# L1: Siri / dictation mishearings -> what was meant. Grows as misses show up
# in the per-entry emails (add the miss to tests/voice_phrases.json too).
MISHEARD = {   # only words Siri gets WRONG; kitchen words for a product are aliases, not here
    'tidos': 'titos', 'titus': 'titos', 'teetos': 'titos', 'tido': 'titos', 'cheetos': 'titos',
    'frys': 'fries', 'freis': 'fries', 'friez': 'fries', 'jamison': 'jameson', 'jamisons': 'jameson',
    'had a cake': 'haddock', 'haddix': 'haddock', 'had dock': 'haddock', 'mick ultra': 'mich ultra',
    'bud lite': 'bud light', 'miller light': 'miller lite', 'guiness': 'guinness', 'genius': 'guinness',
    'hi noon': 'high noon', 'frialator': 'fryolator', 'fry a later': 'fryolator', 'fryer later': 'fryolator',
    'chowda': 'chowder', 'gosslings': 'goslings', 'gostlings': 'goslings', 'tomatoe': 'tomato',
    'tomatoes': 'tomato', 'potatoes': 'potato', 'mozzerella': 'mozzarella', 'mozarella': 'mozzarella',
}
# Kitchen words that mean a word product names use. Applied word by word.
SYNONYMS = {
    'fryer': 'fry', 'fryolator': 'fry', 'fryalator': 'fry', 'frier': 'fry',
    'fingers': 'tenders', 'finger': 'tender', 'lite': 'light', 'codfish': 'cod',
    'buns': 'bun', 'rolls': 'roll',
}

# L3: words that set a product apart from its siblings.
DISTINGUISHERS = {
    'sweet', 'curly', 'waffle', 'crinkle', 'wedge', 'wedges', 'tot', 'tots', 'battered', 'straight',
    'shoestring', 'light', 'lite', 'diet', 'decaf', 'zero', 'ultra', 'spiced', 'flavored',
    'citron', 'vanilla', 'mango', 'peach', 'pineapple', 'lemon', 'lime', 'cherry', 'black',
    'raspberry', 'strawberry', 'watermelon', 'grapefruit', 'cranberry', 'coconut', 'cinnamon', 'honey',
    'fire', 'apple', 'blueberry', 'orange', 'turkey', 'veggie', 'vegan', 'gluten', 'impossible',
    'spicy', 'buffalo', 'sugar', 'rose', 'blonde', 'amber', 'ipa',
    'stout', 'porter', 'oktoberfest', 'pumpkin', 'reposado', 'anejo', 'blanco', 'cab', 'cabernet',
    'chardonnay', 'chard', 'merlot', 'sauvignon', 'grigio', 'noir', 'malbec', 'riesling', 'transfusion',
    'premier', 'slider', 'popcorn', 'bits',
}
ALWAYS_SAID = {'sweet', 'decaf', 'diet', 'zero', 'sugar', 'gluten', 'vegan', 'impossible'}

# Alias statuses that count. The test set can add 'proposed' to measure the
# draft before Mike reviews it (tests/test_voice_recognition.py --proposed).
LIVE_ALIAS = ('approved',)
CLEAR_GAP = 15.0             # points the leader needs over the runner-up for no "Which one?"
WEAK_COVER = 75              # matcher cover % under which we call the match weak (L5 may help)
_CACHE = {}                  # per-process, short-lived: catalogs are rebuilt every few minutes
_CACHE_TTL = 300


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            alias_text TEXT NOT NULL,          -- normalized (alias_key)
            raw_text TEXT,                     -- as drafted / said
            product_id INTEGER NOT NULL REFERENCES products(id),
            source TEXT NOT NULL DEFAULT 'learned',   -- seed_claude | seed_rule | learned | admin
            status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | struck (picks live in voice_picks)
            picks INTEGER NOT NULL DEFAULT 0,
            person TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_by TEXT,
            reviewed_at TEXT,
            last_used_at TEXT,
            hits INTEGER NOT NULL DEFAULT 0,
            UNIQUE(location, alias_text, product_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_product_aliases_lookup ON product_aliases(location, alias_text)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            alias_text TEXT NOT NULL,          -- alias_key of what was said
            product_id INTEGER NOT NULL REFERENCES products(id),
            person TEXT NOT NULL DEFAULT '',
            picks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'learned',   -- learned | demoted
            first_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(location, alias_text, product_id, person)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_card_names (
            product_id INTEGER PRIMARY KEY REFERENCES products(id),
            card_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | struck
            source TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_ai_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,             -- fallback | seed
            location TEXT,
            phrase TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            latency_ms INTEGER,
            result TEXT,
            error TEXT,
            person TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS move_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)


SETTING_DEFAULTS = {
    'ai_fallback': 'on',          # on | off
    'ai_monthly_cap_usd': '10',
    'email_mode': 'per_entry',    # per_entry | daily_digest | off
    'waste_flag_usd': '100',
}


def get_setting(conn, key):
    ensure_tables(conn)
    r = conn.execute("SELECT value FROM move_settings WHERE key = ?", (key,)).fetchone()
    return r['value'] if r and r['value'] is not None else SETTING_DEFAULTS.get(key)


def set_setting(conn, key, value, who):
    ensure_tables(conn)
    conn.execute("""INSERT INTO move_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                                                   updated_at = CURRENT_TIMESTAMP""", (key, str(value), who))


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------

def normalize(phrase):
    """Spoken words -> matcher words. 'the frialator oil' -> 'fry oil',
    "tidos" -> 'titos', 'patties' -> 'patty'."""
    s = (phrase or '').lower().replace('’', "'").replace("'", '')
    s = re.sub(r'[^a-z0-9& ]+', ' ', s)
    s = ' ' + ' '.join(s.split()) + ' '
    for wrong in sorted(MISHEARD, key=len, reverse=True):
        s = re.sub(rf'(?<= ){re.escape(wrong)}(?= )', MISHEARD[wrong], s)
    s = ' '.join(SYNONYMS.get(w, w) for w in s.split())
    return norm(s)


def _singular(w):
    if len(w) > 4 and w.endswith('oes'):
        return w[:-2]          # tomatoes -> tomato
    return _sing(w)


def alias_key(phrase):
    """Lookup key: normalized words, singular, in the order said."""
    return ' '.join(_singular(w) for w in normalize(phrase).split())


# ---------------------------------------------------------------------------
# House catalog + context (L4 inputs)
# ---------------------------------------------------------------------------

def house_context(conn, location):
    """Per house: which products belong, and the context points for each.
    Product ids are shared across the houses (175 are bought at both), and
    products.location is unreliable, so 'belongs' = this house buys, counts,
    or shelves it (or it's a prepped item made here)."""
    key = ('ctx', location)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    from reports.key_items import rank_key_items
    ensure_tables(conn)
    spend90 = {x['product_id']: max(0.0, x['dollars']) for x in rank_key_items(conn, location, days=90)}
    spend365 = {x['product_id']: max(0.0, x['dollars']) for x in rank_key_items(conn, location, days=365)}
    template = {r[0] for r in conn.execute("SELECT product_id FROM count_templates WHERE location = ?", (location,))}
    shelved = {r[0] for r in conn.execute("""
        SELECT psl.product_id FROM product_storage_locations psl
        JOIN storage_locations sl ON sl.id = psl.storage_location_id WHERE sl.location = ?""", (location,))}
    counted = {r[0] for r in conn.execute("""
        SELECT DISTINCT ici.product_id FROM inventory_count_items ici
        JOIN inventory_counts ic ON ic.id = ici.count_id
        WHERE ic.location = ? AND ic.count_date >= date('now', '-180 day') AND COALESCE(ici.counted_quantity, 0) > 0""",
                                                    (location,))}
    prepped = {r[0] for r in conn.execute("""
        SELECT p.id FROM products p JOIN recipes r ON r.id = p.source_recipe_id
        WHERE p.active = 1 AND COALESCE(r.location, 'both') IN (?, 'both')""", (location,))}
    aliased = {r[0] for r in conn.execute("""
        SELECT product_id FROM product_aliases WHERE location = ? AND status IN (%s)"""
        % ','.join('?' * len(LIVE_ALIAS)), (location, *LIVE_ALIAS))} | \
        {r[0] for r in conn.execute("SELECT product_id FROM voice_picks WHERE location = ? AND status = 'learned'", (location,))}
    moved = {}
    for t in ('inventory_transfers', 'waste_log'):
        try:
            col = 'from_product_id' if t == 'inventory_transfers' else 'product_id'
            loc = 'from_location' if t == 'inventory_transfers' else 'location'
            for pid, n in conn.execute(f"SELECT {col}, COUNT(*) FROM {t} WHERE {loc} = ? AND status = 'logged' GROUP BY 1",
                                       (location,)):
                if pid:
                    moved[pid] = moved.get(pid, 0) + n
        except Exception:
            pass
    active = {r[0] for r in conn.execute("SELECT id FROM products WHERE active = 1")}
    ids = (set(spend365) | template | shelved | counted | prepped | aliased) & active
    pts = {}
    for pid in ids:
        pts[pid] = (min(24.0, 6.0 * math.log10(1 + spend90.get(pid, 0)))
                    + (6.0 if pid in template else 0) + (3.0 if pid in shelved or pid in counted else 0)
                    + min(6.0, 2.0 * moved.get(pid, 0)))
    # live = really in use here: bought this year, or on a shelf / in a recent count
    live = {pid for pid in ids if spend365.get(pid, 0) > 0 or pid in shelved or pid in counted or pid in prepped}
    ctx = {'ids': ids, 'points': pts, 'spend90': spend90, 'template': template, 'live': live}
    _CACHE[key] = (time.time(), ctx)
    return ctx


def card_names(conn):
    ensure_tables(conn)
    return {r['product_id']: r['card_name'] for r in
            conn.execute("SELECT product_id, card_name FROM product_card_names WHERE status IN (%s)"
                         % ','.join('?' * len(LIVE_ALIAS)), LIVE_ALIAS)}


_NOISE_RES = [
    re.compile(r'\([^)]*\)'),                                             # (Kettle Cuisine), (case of 6)
    re.compile(r"\b\d+(\.\d+)?\s*(°|'|’|proof|pf)(?=\W|$)", re.I),        # 80°, 80'
    re.compile(r'\bK-\d+(\.\d+)?\s*(gal|liter|l)?\b|\bB-\d+\b', re.I),       # K-15.5 GAL, B-24
    re.compile(r'\b\d+\s*/\s*\d+(\.\d+)?\s*(lt|l|ml|oz|z|lb)\b', re.I),       # 12/1LT, 20/8 OZ (not 3/8" cut)
    re.compile(r'\b\d+\s*/\s*(cs|c|case)\b', re.I),                          # 6/C, 24/CS
    re.compile(r'\b\d+\s*(pk|pack|ct|count)\b', re.I),                       # 24pk, 95ct
    re.compile(r'\b(hb|sb|cs|case|loose|new pkg|single)\b', re.I),
]
_SIZE_RE = re.compile(r'(?<![-\d.])\b\d+(\.\d+)?\s*(ml|l|lt|ltr|liter|oz|z|lb|gal)\b', re.I)   # not ranges (8-10oz)


def _clean(name):
    s = name or ''
    for rx in _NOISE_RES:
        s = rx.sub(' ', s)
    s = _SIZE_RE.sub(' ', s)
    s = re.sub(r'\s*,\s*$', '', re.sub(r'\s+', ' ', s)).strip(' ,-/')
    s = re.sub(r'\s+,', ',', s)
    if s and s == s.upper():
        s = s.title()
    return s or (name or '')


def card_name(conn, p, house=None):
    """Short name for the confirm card, emails and lists.
    An approved card name (Words review) wins. Otherwise the product name minus
    proof marks, pack codes and sizes; the size comes back only when this house
    stocks two sizes of the same thing (so it can still tell them apart)."""
    ensure_tables(conn)
    r = conn.execute("SELECT card_name, status FROM product_card_names WHERE product_id = ? AND status <> 'struck'",
                     (p['id'],)).fetchone()
    if r and r['status'] == 'approved':
        return r['card_name']
    # the unreviewed draft is a far better label than an invoice string (display only;
    # matching never trusts a draft)
    base = _clean(r['card_name'] if r else (p['display_name'] or p['name']))
    if house:
        ctx = house_context(conn, house)
        twins = [q for q in ctx['live'] if q != p['id']]
        if twins:
            ph = ','.join('?' * len(twins))
            for o in conn.execute(f"SELECT name, display_name FROM products WHERE id IN ({ph})", twins):
                if _clean(o['display_name'] or o['name']).lower() == base.lower():
                    from reports.house_moves import product_size
                    size = product_size(conn, p['id'])
                    if size:
                        return f"{base} {size.upper() if size.endswith('l') and not size.endswith('ml') else size}"
                    break
    return base


def _alias_words(conn, location):
    """{product_id: 'words its approved aliases add'} so the matcher can find them."""
    out = {}
    for r in conn.execute("""SELECT product_id, alias_text FROM product_aliases
                             WHERE location = ? AND status IN (%s)""" % ','.join('?' * len(LIVE_ALIAS)), (location, *LIVE_ALIAS)):
        out[r['product_id']] = (out.get(r['product_id'], '') + ' ' + r['alias_text']).strip()
    return out


def _matcher(conn, location):
    key = ('m', location)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    ctx = house_context(conn, location)
    names = card_names(conn)
    words = _alias_words(conn, location)
    rows = {r['id']: r for r in conn.execute("SELECT id, name, display_name FROM products WHERE active = 1")}
    # the same word rules on both sides: a name saying LITE answers to "light"
    extra = {pid: ' '.join((names.get(pid, ''), words.get(pid, ''),
                            normalize(f"{rows[pid]['display_name'] or ''} {rows[pid]['name']}") if pid in rows else '')).strip()
             for pid in ctx['ids']}
    m = Matcher(conn, location, 'all', only_ids=ctx['ids'], extra_bonus=None, extra_text=extra)
    m.tokmap = {p['id']: set(t) for p, t in zip(m.products, m.toks)}
    # the words of the clean name only (card name, else display name): a match
    # there beats one buried in an invoice string ("FRY" in OIL CANOLA CLR FRY TFF)
    m.cleantok = {p['id']: set(norm(names.get(p['id']) or p['display_name'] or p['name']).split()) | set(words.get(p['id'], '').split())
                  for p in m.products}
    # what the product IS (its names), for the L3 veto; alias words don't count
    m.nametok = {p['id']: set(normalize((names.get(p['id']) or '') + ' ' + (p['display_name'] or '') + ' ' + p['name']).split())
                 for p in m.products}
    m.unit = {p['id']: (p['unit'] or '') for p in m.products}
    _CACHE[key] = (time.time(), m)
    return m


def clear_cache():
    _CACHE.clear()


# ---------------------------------------------------------------------------
# L3
# ---------------------------------------------------------------------------

def _said(q, token):
    # a short form in the name counts when the long one was said ("Cab" / "cabernet")
    return any(_tok_match(t, token) or (len(token) >= 3 and t.startswith(token)) for t in q)


def veto(cands, said_words, tokmap):
    """L3. ALWAYS_SAID words nobody said drop the product outright. Otherwise each
    candidate counts its distinguishing words that nobody said and some sibling
    lacks ("sweet" in sweet potato fries next to plain fries); only the candidates
    with the fewest such words stay. c['unsaid'] > 0 means even the survivor has
    a word the speaker didn't say, so it may be offered but never auto-picked."""
    q = said_words
    out = []
    for c in cands:
        toks = tokmap.get(c['product_id'], set())
        if any(t in ALWAYS_SAID and not _said(q, t) for t in toks):
            continue
        out.append(c)
    for c in out:
        toks = tokmap.get(c['product_id'], set())
        c['unsaid'] = sum(1 for t in toks if t in DISTINGUISHERS and not _said(q, t)
                          and any(t not in tokmap.get(o['product_id'], set()) for o in out if o is not c))
    least = min((c['unsaid'] for c in out), default=0)
    return [c for c in out if c['unsaid'] == least]


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def _row(conn, pid):
    return conn.execute("""SELECT id, name, display_name, category, unit, inventory_unit, pack_size, location,
                                  current_price, active_vendor_item_id, source_recipe_id, active
                           FROM products WHERE id = ?""", (pid,)).fetchone()


_KEGGY = re.compile(r'\bkeg\b|\bbbl\b|\bk-\d|\d+(\.\d+)?\s*gal\b|\b(1/2|1/6|1/4)\s*(bbl|keg)|\b50\s*l(iter)?\b|\bk-\d', re.I)


def _is_keg(conn, pid):
    r = conn.execute("SELECT name, display_name, unit FROM products WHERE id = ?", (pid,)).fetchone()
    return bool(r) and ((r['unit'] or '').lower() in ('keg', 'kegs', '1/2 bbl', '1/6 bbl')
                        or bool(_KEGGY.search(f"{r['name']} {r['display_name'] or ''}")))


def unit_filter(conn, cands, unit):
    """'a keg of bud light' is the keg, '2 cases of bud light' is not."""
    from reports.house_moves import KEG_UNITS
    if not unit or len(cands) < 2:
        return cands
    if unit in KEG_UNITS:
        k = [c for c in cands if _is_keg(conn, c['product_id'])]
    elif unit in ('case', 'pack', 'bottle', 'can'):
        k = [c for c in cands if not _is_keg(conn, c['product_id'])]
    else:
        return cands
    return k or cands


DOMINANCE = 3.0      # bought this many times more (90 days) than the runner-up = the one they mean
DOMINANCE_MIN = 100.0


def recognize(conn, location, phrase, size=None, person=None, allow_ai=True, unit=None):
    """{'product': row, 'via': ...} | {'options': [rows], 'via': ...} | {'via': 'none'}.
    'trace' explains the decision (shown on the Fix page / email for misses)."""
    from reports.house_moves import product_size
    ensure_tables(conn)
    key = alias_key(phrase)
    if not key:
        return {'via': 'none', 'trace': 'nothing to match'}
    ctx = house_context(conn, location)
    trace = []

    # L2: approved aliases
    rows = conn.execute("""SELECT a.product_id FROM product_aliases a JOIN products p ON p.id = a.product_id
                           WHERE a.location = ? AND a.alias_text = ? AND p.active = 1 AND a.status IN (%s)"""
                        % ','.join('?' * len(LIVE_ALIAS)), (location, key, *LIVE_ALIAS)).fetchall()
    approved = rows
    # L6: what people picked for these words here (picked the same way twice = automatic)
    picks = conn.execute("""SELECT v.product_id, SUM(v.picks) AS n, MAX(v.person = ?) AS mine
                            FROM voice_picks v JOIN products p ON p.id = v.product_id
                            WHERE v.location = ? AND v.alias_text = ? AND v.status = 'learned' AND p.active = 1
                            GROUP BY v.product_id""", (person or '', location, key)).fetchall()
    sure = [r for r in picks if r['n'] >= 2]
    learned_bonus = {r['product_id']: 12.0 if r['mine'] else 10.0 for r in picks}

    def sized(pids):
        if not size:
            return pids
        s = [p for p in pids if product_size(conn, p) == size]
        return s or pids

    m = _matcher(conn, location)
    if len(sure) == 1 and (not size or product_size(conn, sure[0]['product_id']) in (None, size)):
        return {'product': _row(conn, sure[0]['product_id']), 'via': 'learned',
                'trace': f'picked {sure[0]["n"]}x for "{key}"'}
    group = list(dict.fromkeys(r['product_id'] for r in approved)) if approved else []
    # Word matches and alias hits compete together: an alias can point at a dead
    # duplicate row while the house buys another row for the same thing.
    cands = [c for c in m.candidates(normalize(phrase), 10) if c['score'] >= 50]
    trace.append(f'matcher: {len(cands)} candidate(s)')
    if group:
        # An approved alias is Mike's word for what this phrase means here: if any of
        # its products is live at this house (bought / counted / shelved), only they
        # compete. If all are dead rows, the word matches compete with them.
        live = [pid for pid in group if pid in ctx['live']]
        if live and size and not any(product_size(conn, pid) == size for pid in live):
            live = []          # they said a size none of the alias's products has
        trace.append(f'alias "{key}" -> {len(group)} product(s)' + ('' if live else ', none live/sized'))
        have = {c['product_id']: c for c in cands}
        groupc = []
        for pid in group:
            if pid not in ctx['ids']:
                continue
            c = have.get(pid) or {'product_id': pid, 'score': 100, 'rank': 103.0, 'bought': ctx['spend90'].get(pid, 0)}
            c['score'], c['alias'] = 100, True
            groupc.append(c)
        if live:
            top_alias = max(ctx['spend90'].get(pid, 0) for pid in live)
            # the alias draft may miss the row this house really buys: a full word match
            # bought DOMINANCE x more than every alias product joins the running
            extra = [c for c in cands if not c.get('alias') and c['score'] >= 100
                     and ctx['spend90'].get(c['product_id'], 0) >= max(DOMINANCE_MIN, DOMINANCE * top_alias)]
            cands = [c for c in groupc if c['product_id'] in live] + extra
            if extra:
                trace.append(f'+{len(extra)} bought more')
        else:
            cands = groupc + [c for c in cands if not c.get('alias')]

    # size words narrow; a size that fits nothing means we can't be sure
    size_miss = False
    if size and cands:
        s = [c for c in cands if product_size(conn, c['product_id']) == size]
        if s:
            cands = s
            trace.append(f'size {size}')
        else:
            size_miss = True
            trace.append(f'no {size} found')

    if unit and cands:
        before = len(cands)
        cands = unit_filter(conn, cands, unit)
        if len(cands) < before:
            trace.append(f'unit {unit}')

    # Products that hold every word said beat ones that hold some of them.
    if cands:
        best = max(c['score'] for c in cands)
        if best >= 100:
            cands = [c for c in cands if c['score'] >= 100]
    # Dead rows (never bought, shelved or counted here) only compete when nothing
    # live fits as well.
    live_best = max((c['score'] for c in cands if c['product_id'] in ctx['live']), default=None)
    if live_best is not None:
        before = len(cands)
        cands = [c for c in cands if c['product_id'] in ctx['live'] or c['score'] > live_best]
        if len(cands) < before:
            trace.append(f'{before - len(cands)} dead row(s) out')

    # L3
    said = normalize(phrase).split()
    if cands:
        before = len(cands)
        cands = veto(cands, said, m.nametok)
        if len(cands) < before:
            trace.append(f'veto dropped {before - len(cands)}')

    # L4
    for c in cands:
        clean = m.cleantok.get(c['product_id'], set())
        name_hit = 10.0 if said and all(any(_tok_match(t, u) for u in clean) for t in said) else 0.0
        c['total'] = (c['rank'] + name_hit + ctx['points'].get(c['product_id'], 0)
                      + learned_bonus.get(c['product_id'], 0) + (15.0 if c.get('alias') else 0.0))
    cands.sort(key=lambda c: -c['total'])
    weak = not cands or cands[0]['score'] < WEAK_COVER
    if cands and not weak:
        lead = cands[0]['total'] - cands[1]['total'] if len(cands) > 1 else 99.0
        if len(cands) > 1 and lead < CLEAR_GAP:
            # several fit, but this house really buys only one of them
            best_cover = max(c['score'] for c in cands)
            top = max((c for c in cands if c['score'] == best_cover), key=lambda c: ctx['spend90'].get(c['product_id'], 0))
            s0 = ctx['spend90'].get(top['product_id'], 0)
            s1 = max((ctx['spend90'].get(c['product_id'], 0) for c in cands
                      if c is not top and c['score'] >= best_cover), default=0)
            if s0 >= DOMINANCE_MIN and s0 >= DOMINANCE * s1:
                cands.remove(top)
                cands.insert(0, top)
                lead = CLEAR_GAP
                trace.append(f'bought ${s0:,.0f} vs ${s1:,.0f}')
        blocked = size_miss or (cands[0].get('unsaid') and not cands[0].get('alias'))
        if lead >= CLEAR_GAP and not blocked:
            return {'product': _row(conn, cands[0]['product_id']), 'via': 'alias_group' if group else 'match',
                    'trace': '; '.join(trace + [f'lead {lead:.0f}'])}
        return {'options': [_row(conn, c['product_id']) for c in cands[:4]], 'via': 'ambiguous',
                'trace': '; '.join(trace + [f'lead {lead:.0f} < {CLEAR_GAP:.0f}'])}

    # L5
    if allow_ai:
        ai = ai_fallback(conn, location, phrase, person, hint_ids=[c['product_id'] for c in cands[:10]])
        if ai.get('ids'):
            rows_ = [_row(conn, pid) for pid in ai['ids']]
            if len(rows_) == 1:
                return {'product': rows_[0], 'via': 'ai', 'trace': '; '.join(trace + ['claude picked'])}
            return {'options': rows_[:4], 'via': 'ai', 'trace': '; '.join(trace + ['claude narrowed'])}
        trace.append(f"claude: {ai.get('why')}")
    if cands:
        return {'options': [_row(conn, c['product_id']) for c in cands[:4]], 'via': 'weak', 'trace': '; '.join(trace)}
    return {'via': 'none', 'trace': '; '.join(trace)}


# ---------------------------------------------------------------------------
# L6: learning
# ---------------------------------------------------------------------------

def learn(conn, location, phrase, product_id, person):
    """A person picked / fixed to this product for this phrase. Caller commits."""
    ensure_tables(conn)
    key = alias_key(phrase)
    if not key:
        return
    conn.execute("""
        INSERT INTO voice_picks (location, alias_text, product_id, person, picks) VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(location, alias_text, product_id, person) DO UPDATE SET
            picks = CASE WHEN status = 'demoted' THEN 1 ELSE picks + 1 END, status = 'learned',
            last_at = CURRENT_TIMESTAMP
    """, (location, key, product_id, person or ''))
    clear_cache()


def demote(conn, location, phrase, product_id):
    """Voided within 15 min and re-logged as something else: that pick was wrong."""
    ensure_tables(conn)
    conn.execute("""UPDATE voice_picks SET status = 'demoted', picks = 0
                    WHERE location = ? AND alias_text = ? AND product_id = ?""",
                 (location, alias_key(phrase), product_id))
    clear_cache()


def touch(conn, location, phrase, product_id):
    ensure_tables(conn)
    conn.execute("""UPDATE product_aliases SET hits = hits + 1, last_used_at = CURRENT_TIMESTAMP
                    WHERE location = ? AND alias_text = ? AND product_id = ?""",
                 (location, alias_key(phrase), product_id))


# ---------------------------------------------------------------------------
# L5: Claude, constrained
# ---------------------------------------------------------------------------
# $ per 1M tokens, input/output. Settable without code via CLAUDE_PRICE_<MODEL> = "in,out".
_PRICES = {'claude-haiku-5-5': (0.10, 0.50), 'claude-haiku-4-5': (1.0, 5.0), 'claude-sonnet-4-6': (3.0, 15.0),
           'claude-sonnet-5-5': (2.0, 10.0), 'claude-opus-5-5': (4.0, 20.0)}


def fast_model():
    from integrations.claude_models import CLAUDE_FAST_MODEL
    return CLAUDE_FAST_MODEL


def _price(model):
    env = os.getenv('CLAUDE_PRICE_' + re.sub(r'[^A-Z0-9]', '_', model.upper()))
    if env:
        a, b = env.split(',')
        return float(a), float(b)
    return _PRICES.get(model, (5.0, 25.0))   # unknown model: price it high so the cap bites early


def month_spend(conn):
    ensure_tables(conn)
    r = conn.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM voice_ai_calls
                        WHERE purpose = 'fallback' AND created_at >= strftime('%Y-%m-01', 'now')""").fetchone()
    return float(r[0] or 0)


def _log_call(conn, **kw):
    conn.execute("""INSERT INTO voice_ai_calls (purpose, location, phrase, model, input_tokens, output_tokens, cost_usd,
                                                latency_ms, result, error, person)
                    VALUES (:purpose, :location, :phrase, :model, :input_tokens, :output_tokens, :cost_usd,
                            :latency_ms, :result, :error, :person)""",
                 {k: kw.get(k) for k in ('purpose', 'location', 'phrase', 'model', 'input_tokens', 'output_tokens',
                                         'cost_usd', 'latency_ms', 'result', 'error', 'person')})
    conn.commit()


def ai_fallback(conn, location, phrase, person=None, hint_ids=()):
    """One person-triggered call. Returns {'ids': [...]} (validated against the
    house list) or {'ids': [], 'why': ...}. Never raises."""
    if get_setting(conn, 'ai_fallback') != 'on':
        return {'ids': [], 'why': 'fallback off'}
    cap = float(get_setting(conn, 'ai_monthly_cap_usd') or 0)
    spent = month_spend(conn)
    if spent >= cap:
        _log_call(conn, purpose='fallback', location=location, phrase=phrase, model=None, cost_usd=0,
                  result='capped', error=f'monthly cap ${cap:.2f} reached (${spent:.2f})', person=person)
        return {'ids': [], 'why': 'monthly cap reached'}
    if not os.getenv('ANTHROPIC_API_KEY'):
        return {'ids': [], 'why': 'no API key'}
    ctx = house_context(conn, location)
    names = card_names(conn)
    words = _alias_words(conn, location)
    ids = sorted(ctx['ids'], key=lambda pid: -ctx['points'].get(pid, 0))[:300]
    for h in hint_ids:
        if h not in ids:
            ids.append(h)
    allowed = set(ids)
    lines = []
    for pid in ids:
        p = _row(conn, pid)
        nm = names.get(pid) or p['display_name'] or p['name']
        extra = f" | also: {words[pid]}" if words.get(pid) else ''
        lines.append(f"{pid} | {nm} | {p['name']}{extra}")
    system = ("You match what a restaurant cook or manager said (dictated by Siri, so words may be misheard) "
              "to products in their stockroom. Answer ONLY with JSON: {\"ids\": [<product ids>]} using ids from "
              "the list. One id if you are confident; up to 4 if it could be several; [] if none fit. "
              "Never invent an id. A product with a distinguishing word (sweet potato, light, decaf, a size) "
              "only fits if the speaker said it.")
    user = f"They said: \"{phrase}\"\n\nProducts (id | short name | invoice name):\n" + '\n'.join(lines)
    model = fast_model()
    t0 = time.time()
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=8.0, max_retries=1)
        resp = client.messages.create(model=model, max_tokens=200, system=system,
                                      messages=[{'role': 'user', 'content': user}],
                                      extra_body={'output_config': {'effort': 'low'}})
        text = ''.join(b.text for b in resp.content if b.type == 'text')
        pin, pout = _price(model)
        cost = resp.usage.input_tokens * pin / 1e6 + resp.usage.output_tokens * pout / 1e6
        m = re.search(r'\{.*\}', text, re.S)
        got = json.loads(m.group(0)).get('ids', []) if m else []
        valid = [int(x) for x in got if str(x).isdigit() and int(x) in allowed][:4]
        _log_call(conn, purpose='fallback', location=location, phrase=phrase, model=model,
                  input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens, cost_usd=round(cost, 6),
                  latency_ms=int((time.time() - t0) * 1000),
                  result=json.dumps({'raw': got, 'kept': valid}), person=person)
        return {'ids': valid, 'why': 'claude' if valid else 'claude: unknown'}
    except Exception as e:
        _log_call(conn, purpose='fallback', location=location, phrase=phrase, model=model, cost_usd=0,
                  latency_ms=int((time.time() - t0) * 1000), result='error', error=str(e)[:500], person=person)
        return {'ids': [], 'why': f'claude error: {type(e).__name__}'}
