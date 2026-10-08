"""
Spoken stock moves: "bringing 2 cases of Tito's from Chatham to Dennis" and
"waste, half a case of haddock, it went bad".

One parser behind both Siri Shortcuts (/api/transfers/voice, /api/waste/voice)
and the web fallback pages. Deterministic: word rules plus the count page's
item matcher (reports/item_match). No API calls.

  parse(text, kind)              -> houses, quantity, unit, size words, reason, item phrase
  (which product: reports/item_recognition.recognize)
  to_base(conn, product, q, u)   -> quantity in the product's count unit (bottles for
                                    liquor bought by the case), or None if it can't
                                    be converted. A weight is never guessed.
  effective_cost(conn, pid, house) -> what one count unit cost this house on its most
                                    recent confirmed invoice, bonus units included.
"""

import re

from reports.count_units import bottles_per_case, read_purchase_lines, unit_cost

LOCATIONS = ('chatham', 'dennis')
OTHER = {'chatham': 'dennis', 'dennis': 'chatham'}
ENTITY = {'chatham': 'Red Buoy Inc.', 'dennis': 'Red Nun Public House Inc.'}

# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

_HOUSE_WORDS = [
    (r'dennis\s*port|dennisport|denis\s*port|den\s*port|dennis|denis|dennys|dennies|tennis', 'dennis'),
    (r'chatham|chatam|chattam|chatum|chathem|chatem|chat\s+ham|red\s+buoy|the\s+buoy', 'chatham'),
]
_HOUSE_RE = re.compile(
    r"\b(?P<prep>back\s+to|over\s+to|down\s+to|up\s+to|from|to|at|for|out\s+of)?\s*(?P<h>"
    + '|'.join(f'(?:{w})' for w, _ in _HOUSE_WORDS) + r")(?:'?s)?\b", re.I)

_NUM_WORDS = {
    'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
    'thirty': 30, 'forty': 40, 'fifty': 50, 'dozen': 12, 'to': 2, 'too': 2, 'for': 4,
}
_FRACTIONS = {'half': 0.5, 'a half': 0.5, 'third': 1 / 3, 'a third': 1 / 3, 'quarter': 0.25,
              'a quarter': 0.25, 'two thirds': 2 / 3, 'three quarters': 0.75}
_VAGUE = re.compile(r'\b(a\s+couple(\s+of)?|couple|a\s+few|few|some|several|a\s+bunch(\s+of)?|bunch|a\s+lot(\s+of)?|lots\s+of)\b')

# canonical unit -> spoken forms (longest phrases are tried first)
UNITS = {
    'case': ['cases', 'case', 'cs', 'box of', 'carton', 'cartons'],
    'bottle': ['bottles', 'bottle', 'btls', 'btl', 'fifths', 'fifth', 'handles', 'handle'],
    'each': ['each', 'ea', 'pieces', 'piece', 'units', 'unit', 'ct'],
    'half barrel': ['half barrels', 'half barrel', 'half kegs', 'half keg', 'halfs', 'full kegs', 'full keg'],
    'quarter barrel': ['quarter barrels', 'quarter barrel', 'quarter kegs', 'quarter keg', 'pony kegs', 'pony keg'],
    'sixtel': ['sixtels', 'sixtel', 'sixth barrels', 'sixth barrel', 'sixth kegs', 'sixth keg', 'sixers'],
    'keg': ['kegs', 'keg'],
    'pack': ['six packs', 'six pack', 'sixpacks', 'sixpack', '6 packs', '6 pack', '6-packs', '6-pack',
             'four packs', 'four pack', '4 packs', '4 pack', '4-packs', '4-pack', 'twelve packs', 'twelve pack',
             '12 packs', '12 pack', '12-pack', 'packs', 'pack', 'pk'],
    'lb': ['pounds', 'pound', 'lbs', 'lb'],
    'oz': ['ounces', 'ounce', 'oz'],
    'gallon': ['gallons', 'gallon', 'gal'],
    'quart': ['quarts', 'quart', 'qt'],
    'can': ['cans', 'can'],
    'bag': ['bags', 'bag'],
    'box': ['boxes', 'box'],
    'sleeve': ['sleeves', 'sleeve'],
    'jar': ['jars', 'jar'],
    'jug': ['jugs', 'jug', 'containers', 'container'],
    'tub': ['tubs', 'tub', 'buckets', 'bucket', 'pails', 'pail'],
    'tray': ['trays', 'tray'],
    'sixth pan': ['sixth pans', 'sixth pan', '6th pan', '6th pans'],
    'third pan': ['third pans', 'third pan', '3rd pan', '3rd pans'],
    'ninth pan': ['ninth pans', 'ninth pan', '9th pan'],
    'pan': ['hotel pans', 'hotel pan', 'full pans', 'full pan', 'pans', 'pan'],
    'batch': ['batches', 'batch'],
    'portion': ['portions', 'portion', 'servings', 'serving', 'orders', 'order'],
}
_UNIT_FORMS = sorted(((f, c) for c, fs in UNITS.items() for f in fs), key=lambda x: -len(x[0]))
_UNIT_ALT = '|'.join(re.escape(f) for f, _ in _UNIT_FORMS)
_UNIT_OF = {f: c for f, c in _UNIT_FORMS}
KEG_UNITS = {'keg', 'half barrel', 'quarter barrel', 'sixtel'}

# Size words narrow which product: "1 liter", "a handle", "750".
_SIZE_RE = re.compile(
    r"\b(?:(?P<n>\d+(?:\.\d+)?)\s*(?P<u>ml|milliliters?|l|lt|ltr|liters?|litres?|oz|ounces?)\b(?!\s+(?:of\s+)?(?:" + _UNIT_ALT + r")\b)"
    r"|(?P<handle>handle|handles)\b|(?P<fifth>fifth|fifths)\b|(?P<bare>1\.75|750|375|1\.5)\b(?!\s*(?:" + _UNIT_ALT + r")\b))", re.I)

# Words that say what's happening but name nothing.
_FILLER = {
    'bringing', 'bring', 'brought', 'taking', 'take', 'took', 'sending', 'send', 'sent', 'moving', 'move',
    'moved', 'borrowing', 'borrowed', 'borrow', 'giving', 'gave', 'give', 'grabbing', 'grabbed', 'grab',
    'running', 'ran', 'dropping off', 'transfer', 'transferring', 'transferred', 'i', 'im', 'were', 'we',
    'are', 'am', 'is', 'just', 'got', 'get', 'over', 'back', 'here', 'there', 'please', 'okay', 'ok',
    'waste', 'wasted', 'wasting', 'tossed', 'tossing', 'toss', 'threw', 'throw', 'throwing', 'out', 'away',
    'trash', 'trashed', 'garbage', 'binned', 'dumped', 'dump', 'about', 'around', 'roughly', 'like',
    'maybe', 'approximately', 'worth', 'had', 'have', 'has', 'to', 'it', 'they', 'them', 'that', 'this',
    'these', 'those', 'our', 'my', 'from', 'with', 'for', 'and', 'but', 'so', 'house', 'the', 'of', 'a', 'an',
    'some', 'today', 'tonight', 'yesterday', 'went', 'go', 'gone', 'all', 'lost', 'whole', 'entire',
}

# ---------------------------------------------------------------------------
# Waste reasons
# ---------------------------------------------------------------------------
REASONS = {
    'spoiled': 'Spoiled', 'dropped': 'Dropped', 'kitchen_error': 'Kitchen error', 'overprep': 'Over-prep',
    'bar': 'Bar', 'staff_meal': 'Staff meal', 'other': 'Other',
}
_REASON_RULES = [   # first hit wins; staff meal and bar before the generic words
    ('staff_meal', r'staff\s+meals?|family\s+meals?|shift\s+meals?|employee\s+meals?'),
    ('bar', r'broken\s+bottles?|broke\s+(?:a\s+)?bottles?|bottles?\s+broke|bad\s+kegs?|kegs?\s+(?:was\s+|went\s+)?(?:bad|skunked|flat)|foam(?:y|ing)?|line\s+clean(?:ing)?|cleaning\s+(?:the\s+)?lines?|skunked|flat\s+beer|over\s*pour(?:ed)?'),
    ('kitchen_error', r'burn(?:t|ed)|over\s*cooked|under\s*cooked|wrong\s+orders?|wrong\s+tickets?|re-?makes?|re-?made|sent\s+back|kicked\s+back|messed\s+up|screwed\s+up|cooked\s+wrong|mistakes?'),
    ('spoiled', r'went\s+bad|gone\s+bad|go\s+bad|spoil(?:ed|t)?|expired|out\s+of\s+date|past\s+(?:the\s+)?date|mo(?:u)?ldy|mold|rotten|rotted|rot|turned|sour(?:ed)?|slimy|freezer\s+burn(?:ed|t)?|smell(?:s|ed)?\s+(?:bad|off)|off\b|bad'),
    ('dropped', r'dropped|drop|spilled|spilt|spill|fell|knocked\s+over|tipped\s+over|on\s+the\s+floor'),
    ('overprep', r'(?:made|prepped|cooked)\s+too\s+much|too\s+much|over\s*prep(?:ped)?|end\s+of\s+(?:the\s+)?night|leftovers?|left\s+over|tossing\s+the\s+batch|extra'),
]
_REASON_RES = [(code, re.compile(r'\b(?:' + rx + r')\b', re.I)) for code, rx in _REASON_RULES]
_REASON_LEAD = re.compile(r'\b(because|cause|cuz|since|due\s+to|as)\b.*$', re.I)
_COMP_RE = re.compile(r'\bcomp(?:s|ed|ing|ped)?\b|\bcomped\b|\bcomplimentary\b|\bvoid(?:ed)?\s+(?:the|a|an)?\s*(?:check|table|ticket)', re.I)


def _clean(text):
    s = (text or '').lower().replace('’', "'").replace('‘', "'")
    s = re.sub(r"(\d)\s*-\s*(pack)", r"\1 \2", s)
    s = re.sub(r"[^a-z0-9'./\- ]+", ' ', s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", ' ', s)          # keep decimals, drop sentence periods
    return ' '.join(s.split())


def _num(tok):
    if re.fullmatch(r'\d+(\.\d+)?', tok):
        return float(tok)
    if re.fullmatch(r'\d+/\d+', tok):
        a, b = tok.split('/')
        return float(a) / float(b) if float(b) else None
    return _NUM_WORDS.get(tok)


def _parse_qty(s):
    """Find the quantity and unit. Returns (qty, unit, vague, span) with span the
    (start, end) of the matched words so the item phrase can drop them."""
    frac = r'(?:a\s+half|half|a\s+third|third|a\s+quarter|quarter|two\s+thirds|three\s+quarters)'
    numw = r'(?:\d+(?:\.\d+)?(?:/\d+)?|' + '|'.join(sorted((k for k in _NUM_WORDS if k not in ('to', 'too', 'for')), key=len, reverse=True)) + r')'
    unit = r'(?P<unit>' + _UNIT_ALT + r')'
    pats = [
        # "2 and a half cases"
        (re.compile(r'\b(?P<n>' + numw + r')\s+and\s+a\s+half\s+' + unit + r'\b'), 'n_and_half'),
        # "a case and a half"
        (re.compile(r'\b(?P<n>a|an|one)\s+' + unit + r'\s+and\s+a\s+half\b'), 'n_half'),
        # "half a case", "a third of the pan", "half of a case"
        (re.compile(r'\b(?P<frac>' + frac + r')\s+(?:of\s+)?(?:a|an|the|one)?\s*' + unit + r'\b'), 'frac'),
        # "2 and a half cases", "two cases", "a case", "1/2 case", "to cases" (dictation)
        (re.compile(r'\b(?P<n>' + numw + r'|to|too|for)(?P<and>\s+and\s+a\s+half)?\s+' + unit + r'\b'), 'n'),
        # bare unit: "case of titos"
        (re.compile(r'(?:^|\s)' + unit + r'\s+of\b'), 'unit_only'),
        # number with no unit: "2 titos", "two fries"
        (re.compile(r'\b(?P<n>' + numw + r')\b(?!\s*(?:ml|l|lt|ltr|liter|oz|%))'), 'n_only'),
        (re.compile(r'\b(?P<frac>' + frac + r')\b'), 'frac_only'),
    ]
    vague = _VAGUE.search(s)
    for rx, kind in pats:
        if vague and kind in ('unit_only', 'n_only', 'frac_only'):
            break                      # "a couple cases" names no number: ask, don't assume
        m = rx.search(s)
        if not m:
            continue
        g = m.groupdict()
        u = _UNIT_OF.get((g.get('unit') or '').strip())
        if kind == 'frac':
            return _FRACTIONS[re.sub(r'\s+', ' ', g['frac'])], u, False, m.span()
        if kind == 'n_and_half':
            n = _num(g['n'])
            if n is not None:
                return n + 0.5, u, False, m.span()
            continue
        if kind in ('n', 'n_half'):
            n = _num(g['n'])
            if n is None:
                continue
            if kind == 'n_half' or g.get('and'):
                n += 0.5
            return n, u, False, m.span()
        if kind == 'unit_only':
            return 1.0, u, False, m.span()
        if kind == 'n_only':
            if g['n'] in ('a', 'an'):          # "a" alone names nothing ("a fries")
                continue
            n = _num(g['n'])
            if n is None:
                continue
            return n, None, False, m.span()
        if kind == 'frac_only':
            return _FRACTIONS[re.sub(r'\s+', ' ', g['frac'])], None, False, m.span()
    v = vague
    if v:
        um = re.search(r'\b' + unit + r'\b', s)
        span = (v.start(), max(v.end(), um.end())) if um and um.start() - v.end() <= 2 else v.span()
        return None, (_UNIT_OF.get(um.group('unit')) if um else None), True, span
    um = re.search(r'\b' + unit + r'\b', s)
    if um:
        return None, _UNIT_OF.get(um.group('unit')), False, um.span()
    return None, None, False, None


def _size(s):
    """'1l' / '1.75l' / '750ml' style token, or None; plus the span."""
    m = _SIZE_RE.search(s)
    if not m:
        return None, None
    if m.group('handle'):
        return '1.75l', m.span()
    if m.group('fifth'):
        return '750ml', m.span()
    if m.group('bare'):
        b = m.group('bare')
        return ('750ml' if b == '750' else '375ml' if b == '375' else b + 'l'), m.span()
    n, u = float(m.group('n')), m.group('u')
    if u.startswith('m'):
        return f'{int(n)}ml', m.span()
    if u.startswith('o'):
        return f'{n:g}oz', m.span()
    return f'{n:g}l', m.span()


def parse(text, kind='transfer'):
    """Pull houses / quantity / unit / size / reason out of one dictation.
    Returns a dict; 'item' is what's left to match against products."""
    raw = text or ''
    s = _clean(raw)
    s = re.sub(r'\bhalf\s+(?:and|&|n)\s+half\b', 'halfnhalf', s)
    from reports.item_recognition import MISHEARD
    for wrong in sorted((w for w in MISHEARD if ' ' in w), key=len, reverse=True):
        s = re.sub(rf'\b{re.escape(wrong)}\b', MISHEARD[wrong], s)   # "had a cake" before "a" reads as 1     # a product (Sun Cruiser Half & Half), not a quantity
    out = {'raw': raw, 'kind': kind, 'from': None, 'to': None, 'named': [], 'qty': None, 'unit': None,
           'vague_qty': False, 'size': None, 'reason': None, 'reason_words': None, 'comp': False, 'item': ''}
    if kind == 'waste' and _COMP_RE.search(s):
        out['comp'] = True

    # houses
    for m in list(_HOUSE_RE.finditer(s)):
        h = next(code for w, code in _HOUSE_WORDS if re.fullmatch(w, m.group('h'), re.I))
        prep = re.sub(r'\s+', ' ', (m.group('prep') or '').lower())
        if h == 'dennis' and m.group('h').lower() == 'tennis' and prep not in ('to', 'from', 'back to', 'over to', 'at', 'out of'):
            continue                     # "tennis" only counts as Dennis after to/from
        out['named'].append((prep, h))
    for prep, h in out['named']:
        if prep in ('from', 'out of'):
            out['from'] = out['from'] or h
        elif prep in ('to', 'back to', 'over to', 'down to', 'up to', 'for'):
            out['to'] = out['to'] or h
    if kind == 'transfer':
        bare = [h for p, h in out['named'] if p not in ('from', 'out of', 'to', 'back to', 'over to', 'down to', 'up to', 'for')]
        # "chatham to dennis" dictated as "chatham dennis" or "from chatham dennis"
        if not out['from'] and not out['to'] and len(bare) >= 2 and bare[0] != bare[1]:
            out['from'], out['to'] = bare[0], bare[1]
        if out['to'] and not out['from']:
            out['from'] = OTHER[out['to']]
        if out['from'] and not out['to']:
            out['to'] = OTHER[out['from']]
        if out['from'] == out['to']:
            out['from'] = out['to'] = None
    s = _HOUSE_RE.sub(' ', s)

    # waste reason: the "because ..." tail is reason, not item
    if kind == 'waste':
        tail = _REASON_LEAD.search(s)
        for code, rx in _REASON_RES:      # first rule that hits names the reason ...
            m = rx.search(s)
            if m and not out['reason']:
                out['reason'], out['reason_words'] = code, m.group(0)
            if m:                          # ... and every reason word leaves the item
                s = rx.sub(' ', s)
        if tail:
            out['reason_words'] = out['reason_words'] or tail.group(0)
            s = _REASON_LEAD.sub(' ', s)
        s = re.sub(r'\b(it|they|that|which)\s+(was|were|is|are|went|got)\b.*$', ' ', s)

    size, span = _size(s)
    if size:
        out['size'] = size
        s = s[:span[0]] + ' ' + s[span[1]:]

    qty, unit, vague, span = _parse_qty(s)
    out.update(qty=qty, unit=unit, vague_qty=vague)
    if span:
        s = s[:span[0]] + ' ' + s[span[1]:]
    if kind == 'waste' and qty is None and not vague and re.search(r'\bbroke(n)?\s+(a\s+)?bottle\b', _clean(raw)):
        out.update(qty=1.0, unit='bottle')           # "broken bottle of goslings" = one bottle
    if unit in ('bottle',) and re.search(r'\bhandles?\b', _clean(raw)) and not size:
        out['size'] = '1.75l'
    words = [w for w in re.sub(r"[^a-z0-9' ]+", ' ', s).split() if w not in _FILLER]
    out['item'] = ' '.join(words).strip().replace('halfnhalf', 'half & half')
    return out


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
_UNIT_CANON = {'cs': 'case', 'case': 'case', 'cases': 'case', 'combo': 'case', 'bx': 'box',
               'ea': 'each', 'each': 'each', 'ct': 'each', 'pc': 'each', 'piece': 'each', 'unit': 'each',
               'bottle': 'bottle', 'bottles': 'bottle', 'btl': 'bottle', 'can': 'can', 'cans': 'can',
               'lb': 'lb', 'lbs': 'lb', 'pound': 'lb', '#': 'lb', 'oz': 'oz', 'gal': 'gallon', 'gallon': 'gallon',
               'qt': 'quart', 'quart': 'quart', 'bag': 'bag', 'box': 'box', 'keg': 'keg', 'pk': 'pack', 'pack': 'pack',
               '1/2 bbl': 'half barrel', 'half barrel': 'half barrel', '1/6 bbl': 'sixtel', 'sixtel': 'sixtel',
               '1/4 bbl': 'quarter barrel', 'portion': 'portion', 'each/portion': 'portion'}
_COUNT_FAMILY = {'each', 'bottle', 'can'}


def canon_unit(u):
    u = (u or '').strip().lower().rstrip('.')
    return _UNIT_CANON.get(u) or _UNIT_OF.get(u) or u


def base_unit(p):
    """The unit the product is counted in (counts and transfers agree)."""
    return canon_unit(p['inventory_unit'] or p['unit']) or 'each'


def ordering_unit(p):
    return canon_unit(p['unit'] or p['inventory_unit']) or 'each'


def _same(a, b, booze):
    if a == b:
        return True
    if a in KEG_UNITS and b in KEG_UNITS:
        return True
    return booze and a in _COUNT_FAMILY and b in _COUNT_FAMILY


def to_base(conn, p, qty, unit):
    """(qty_base, base_unit, how) or None. Never guesses a weight."""
    if qty is None:
        return None
    b = base_unit(p)
    u = canon_unit(unit) if unit else ordering_unit(p)
    booze = (p['category'] or '').upper() in ('BEER', 'LIQUOR', 'WINE')
    if _same(u, b, booze):
        return round(qty, 4), b, 'same unit'
    vi = conn.execute("SELECT pack_size, pack_contains, contains_unit, vendor_description FROM vendor_items WHERE id = ?",
                      (p['active_vendor_item_id'],)).fetchone() if p['active_vendor_item_id'] else None
    if u == 'case' and (b in _COUNT_FAMILY or b == 'pack'):
        n, where = bottles_per_case(p, vi, conn)
        if n:
            return round(qty * n, 4), b, f'{n} per case ({where})'
    if b == 'case' and u in _COUNT_FAMILY | {'pack'}:
        n, where = bottles_per_case(p, vi, conn)
        if n:
            return round(qty / n, 4), b, f'1/{n} case ({where})'
    for r in conn.execute("SELECT from_qty, from_unit, to_qty, to_unit FROM product_unit_conversions WHERE product_id = ?",
                          (p['id'],)).fetchall():
        fu, tu = canon_unit(r['from_unit']), canon_unit(r['to_unit'])
        if not r['from_qty'] or not r['to_qty']:
            continue
        if _same(fu, u, booze) and _same(tu, b, booze):
            return round(qty * r['to_qty'] / r['from_qty'], 4), b, f"{r['from_qty']:g} {fu} = {r['to_qty']:g} {tu}"
        if _same(fu, b, booze) and _same(tu, u, booze):
            return round(qty * r['from_qty'] / r['to_qty'], 4), b, f"{r['from_qty']:g} {fu} = {r['to_qty']:g} {tu}"
    return None


def units_for(conn, p):
    """Units this product can be entered in (for the Fix page and 'which unit?')."""
    out = [ordering_unit(p)]
    if base_unit(p) not in out:
        out.append(base_unit(p))
    for u in ('case', 'bottle', 'each', 'lb', 'bag', 'box', 'keg'):
        if u not in out and to_base(conn, p, 1, u):
            out.append(u)
    return out


def plural(q, unit):
    if unit is None:
        return ''
    if q is not None and abs(q - 1) < 1e-9 or q is not None and q < 1:
        return unit
    irregular = {'each': 'each', 'box': 'boxes', 'batch': 'batches', 'lb': 'lb', 'oz': 'oz', 'half barrel': 'half barrels'}
    return irregular.get(unit, unit + 's')


def fmt_qty(q):
    if q is None:
        return '?'
    for v, s in ((0.5, '1/2'), (0.25, '1/4'), (0.75, '3/4'), (1 / 3, '1/3'), (2 / 3, '2/3')):
        if abs(q - v) < 0.01:
            return s
    whole = int(q)
    if abs(q - whole - 0.5) < 0.01:
        return f'{whole} 1/2'
    return f'{q:g}' if abs(q - round(q)) > 1e-9 else str(int(round(q)))


# ---------------------------------------------------------------------------
# Item matching
# ---------------------------------------------------------------------------
_SIZE_IN_NAME = [
    ('1.75l', re.compile(r'1\.75\s*(l|lt|ltr|liter)?\b|\bhandle\b|\b175\b', re.I)),
    ('1l', re.compile(r'(?<![\d.])1\s*(l|lt|ltr|liter|litre)\b|\b1lt\b|/1lt|\b1ltr\b', re.I)),
    ('750ml', re.compile(r'750\s*(ml|m)?\b', re.I)),
    ('375ml', re.compile(r'375\s*(ml|m)?\b', re.I)),
    ('1.5l', re.compile(r'1\.5\s*(l|lt|ltr|liter)\b', re.I)),
]
_OZ_IN_NAME = re.compile(r'(?<![\d/.])(\d{1,2}(?:\.\d)?)\s*(?:oz|z|ounce)\b', re.I)


def product_size(conn, pid):
    r = conn.execute("""SELECT p.name, p.display_name, p.pack_size, vi.pack_size AS vps, vi.vendor_description, vi.pack_contains, vi.contains_unit
                        FROM products p LEFT JOIN vendor_items vi ON vi.id = p.active_vendor_item_id WHERE p.id = ?""", (pid,)).fetchone()
    if not r:
        return None
    text = ' '.join(str(x or '') for x in (r['name'], r['display_name'], r['vps'], r['vendor_description'], r['pack_size']))
    for size, rx in _SIZE_IN_NAME:
        if rx.search(text):
            return size
    m = _OZ_IN_NAME.search(' '.join(str(x or '') for x in (r['display_name'], r['name'])))
    if m:
        return f'{float(m.group(1)):g}oz'
    if r['pack_contains'] and (r['contains_unit'] or '').upper() in ('ML',):
        return f"{int(r['pack_contains'])}ml"
    return None


def _product(conn, pid):
    return conn.execute("""SELECT id, name, display_name, category, unit, inventory_unit, pack_size, location,
                                  current_price, active_vendor_item_id, source_recipe_id, active
                           FROM products WHERE id = ?""", (pid,)).fetchone()


def option_label(conn, p):
    """A choice line that tells two sizes apart: name, size, how it's bought."""
    name = p['display_name'] or p['name']
    bits = []
    size = product_size(conn, p['id'])
    if size and size.lower() not in name.lower().replace(' ', ''):
        bits.append(size.upper().replace('ML', 'ml'))
    ou = ordering_unit(p)
    if ou == 'case':
        vi = conn.execute("SELECT pack_size, pack_contains, contains_unit, vendor_description FROM vendor_items WHERE id = ?",
                          (p['active_vendor_item_id'],)).fetchone() if p['active_vendor_item_id'] else None
        n, _ = bottles_per_case(p, vi, conn)
        bits.append(f'case of {n}' if n else 'by the case')
    elif ou:
        bits.append(f'by the {ou}')
    return f"{name} ({', '.join(bits)})" if bits else name


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def _invoice_lines(conn, p, location):
    """This house's confirmed invoice lines for the product, newest invoice first."""
    vis = conn.execute("SELECT vendor_item_code, vendor_description FROM vendor_items WHERE product_id = ?", (p['id'],)).fetchall()
    codes = sorted({v['vendor_item_code'] for v in vis if v['vendor_item_code']})
    descs = sorted({v['vendor_description'] for v in vis if v['vendor_description']})
    if not codes and not descs:
        return []
    conds, params = [], []
    if codes:
        conds.append(f"sii.vendor_item_code IN ({','.join('?' * len(codes))})")
        params += codes
    if descs:
        conds.append(f"sii.product_name IN ({','.join('?' * len(descs))})")
        params += descs
    return conn.execute(f"""
        SELECT si.id AS invoice_id, si.invoice_date, si.invoice_number, si.vendor_name,
               sii.product_name, sii.quantity, sii.unit, sii.unit_price, sii.total_price
        FROM scanned_invoice_items sii JOIN scanned_invoices si ON si.id = sii.invoice_id
        WHERE si.status = 'confirmed' AND si.location = ? AND ({' OR '.join(conds)})
        ORDER BY si.invoice_date DESC, si.id DESC
    """, (location, *params)).fetchall()


def effective_cost(conn, product_id, location):
    """{'cost' per count unit, 'unit', 'source', 'detail'}.

    The most recent confirmed invoice at `location` that bought the product:
    dollars paid / units received, bonus units included. A deal invoice (20 cases
    at list + 2 cases at $1) is (20 x price + 2 x $1) / 22 cases, so the bonus
    lowers the cost; source = 'deal'. $1 lines labeled as bottles/each are
    backorders and deposits (read_purchase_lines), never bonus stock.
    No invoice -> the catalog price (count_units.unit_cost), source 'catalog'.
    """
    p = _product(conn, product_id)
    if not p:
        return {'cost': None, 'unit': None, 'source': 'none', 'detail': 'unknown product'}
    b = base_unit(p)
    lines = _invoice_lines(conn, p, location)
    if lines:
        inv = lines[0]['invoice_id']
        these = [dict(l) for l in lines if l['invoice_id'] == inv]
        paid = [l for l in these if (l['unit_price'] or 0) > 1.01 and (l['quantity'] or 0) > 0]
        read = read_purchase_lines(conn, p['id'], paid) if paid else []
        dollars = sum(l['total_price'] or (l['quantity'] * l['unit_price']) for l in read if l['count_qty'])
        units = sum(l['count_qty'] for l in read if l['count_qty'])
        bonus_units, bonus_dollars = 0.0, 0.0
        if units > 0:
            for l in these:
                if 0 <= (l['unit_price'] or 0) <= 1.01 and (l['quantity'] or 0) > 0 and canon_unit(l['unit']) == 'case':
                    conv = to_base(conn, p, l['quantity'], 'case')
                    if conv:
                        bonus_units += conv[0]
                        bonus_dollars += l['total_price'] or 0
        if units > 0 and dollars > 0:
            cost = (dollars + bonus_dollars) / (units + bonus_units)
            src = 'deal' if bonus_units else 'last_invoice'
            detail = (f"{lines[0]['vendor_name']} {lines[0]['invoice_date']} #{lines[0]['invoice_number'] or inv}: "
                      f"${dollars + bonus_dollars:,.2f} / {units + bonus_units:g} {plural(2, b)}"
                      + (f" ({bonus_units:g} bonus)" if bonus_units else ''))
            return {'cost': round(cost, 4), 'unit': b, 'source': src, 'detail': detail, 'invoice_id': inv}
    uc = unit_cost(conn, p['id'], p['inventory_unit'] or p['unit'])
    if uc['cost'] is not None:
        return {'cost': uc['cost'], 'unit': b, 'source': 'catalog', 'detail': uc['basis']}
    if p['source_recipe_id']:
        rc = recipe_unit_cost(conn, p['source_recipe_id'])
        if rc:
            return {'cost': rc['cost'], 'unit': rc['unit'], 'source': 'recipe', 'detail': rc['detail']}
    return {'cost': None, 'unit': b, 'source': 'none', 'detail': uc['basis']}


def recipe_unit_cost(conn, recipe_id):
    """Cost of one yield unit of a prepped recipe, from its last costing. Read only."""
    r = conn.execute("SELECT name, yield_qty, yield_unit, total_cost FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not r or not r['total_cost'] or not r['yield_qty']:
        return None
    return {'cost': round(r['total_cost'] / r['yield_qty'], 4), 'unit': canon_unit(r['yield_unit']) or 'each',
            'detail': f"recipe {r['name']}: ${r['total_cost']:,.2f} / {r['yield_qty']:g} {r['yield_unit']}"}
