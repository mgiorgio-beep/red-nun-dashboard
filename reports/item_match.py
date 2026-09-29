"""
Match spoken / typed item names to products (the count page's "Say items").

"titos jameson goslings rum" -> Titos Vodka 80°, Jameson 80°, Gosling Black Seal Rum.
Free: word matching only, no API calls. Among near-identical names (the catalog has
four Tito's), the one this house actually bought ranks first.
"""
import math
import re

from rapidfuzz import fuzz

from reports.key_items import rank_key_items, BOOZE_CATS

# Words that carry no identity when spoken or in a vendor name.
_NOISE = {'the', 'a', 'an', 'of', 'and', 'cs', 'case', 'pk', 'pack', 'ct', 'oz', 'ml', 'l', 'lb', 'gal',
          'hb', 'sb', 'bbl', 'nr', 'loose', 'bulk', 'btl', 'bottle', 'bottles', 'can', 'cans'}
# Spoken separators between items.
_SPLIT = re.compile(r"[,;\n.]+|\b(?:and then|next|then)\b", re.I)
MIN_SCORE = 75       # % of a segment's words a name must cover to count as an item
SEG_COST = 15        # price of splitting into one more item
MAX_SEG_WORDS = 5


def norm(s):
    s = (s or '').lower().replace("'", '').replace('’', '')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return ' '.join(w for w in s.split() if w not in _NOISE and not re.fullmatch(r'\d+[a-z]*', w))


def _catalog(conn, location, count_type):
    rows = conn.execute("""
        SELECT id, name, display_name, category, unit, inventory_unit, current_price, location
        FROM products WHERE active = 1
    """).fetchall()
    out = []
    for r in rows:
        booze = (r['category'] or '').upper() in BOOZE_CATS
        if (count_type == 'booze' and not booze) or (count_type == 'food' and booze):
            continue
        out.append(dict(r))
    spend = {x['product_id']: x['dollars'] for x in rank_key_items(conn, location, days=365)}
    for p in out:
        p['label'] = p['display_name'] or p['name']
        p['bought'] = max(0, round(spend.get(p['id'], 0)))  # credits can net negative
        # Up to +8 for things this house buys a lot of; breaks ties between duplicates.
        p['bonus'] = min(8.0, 2.0 * math.log10(1 + p['bought'])) + (1.0 if p['location'] == location else 0.0)
    return out


def _sing(w):
    if len(w) > 4 and w.endswith('ies'):
        return w[:-3] + 'y'
    if len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        return w[:-1]
    return w


def _undouble(w):
    return re.sub(r'(.)\1+', r'\1', w)


def _tok_match(t, u):
    """How well spoken word t names product-name word u: 1 for the same word, a
    prefix ("mich" -> "michelob") or a plural ("patties" -> "patty"); 0.9 for a
    sound-alike spelling ("kettle" -> "ketel", "jamison" -> "jameson"); else 0.
    The 0.9 keeps a real word ahead of a near-spelling ("fries" is not "fresh")."""
    t, u = _sing(t), _sing(u)
    if t == u or (len(t) >= 3 and u.startswith(t)) or (len(u) >= 4 and t.startswith(u)):
        return 1.0
    if len(t) >= 5 and fuzz.ratio(_undouble(t), _undouble(u)) >= 80:
        return 0.9
    return 0.0


class Matcher:
    def __init__(self, conn, location, count_type='all'):
        self.products = _catalog(conn, location, count_type)
        self.toks = [sorted(set((norm(p['label']) + ' ' + norm(p['name'])).split())) for p in self.products]
        self.by_prefix = {}
        for i, ts in enumerate(self.toks):
            for u in ts:
                self.by_prefix.setdefault(u[:2], set()).add(i)
        self._cache = {}

    def candidates(self, phrase, limit=5):
        """Score = % of the spoken words the product name covers (100 = all of them),
        then how much of the name was said, then how much this house buys it."""
        q = norm(phrase).split()
        if not q:
            return []
        key = ' '.join(q)
        if key not in self._cache:
            pool = set()
            for t in q:
                pool |= self.by_prefix.get(t[:2], set())
            scored = []
            for i in pool:
                ts = self.toks[i]
                hit = sum(max(_tok_match(t, u) for u in ts) for t in q)
                if not hit:
                    continue
                cover = 100.0 * hit / len(q)
                said = sum(1 for u in ts if any(_tok_match(t, u) for t in q)) / len(ts)
                p = self.products[i]
                scored.append((cover + 3 * said + p['bonus'], cover, p))
            scored.sort(key=lambda x: -x[0])
            self._cache[key] = [{'product_id': p['id'], 'name': p['label'], 'category': p['category'],
                                 'unit': p['unit'], 'inventory_unit': p['inventory_unit'],
                                 'score': round(cover), 'bought': p['bought']}
                                for _, cover, p in scored[:10]]
        return self._cache[key][:limit]

    def _best(self, words):
        c = self.candidates(' '.join(words), 1)
        return c[0]['score'] if c else 0

    def segment(self, chunk):
        """Split a run-on chunk ("titos jameson goslings rum") into items: the
        fewest items whose names cover every word. Each extra item costs a bit,
        so "goslings rum" stays one item rather than "goslings" + "rum"."""
        words = norm(chunk).split()
        if not words:
            return []
        n = len(words)
        best = [(0.0, None)] + [(-1e9, None)] * n
        for i in range(1, n + 1):
            for j in range(max(0, i - MAX_SEG_WORDS), i):
                s = self._best(words[j:i])
                val = (s - MIN_SCORE) * (i - j) - SEG_COST if s >= MIN_SCORE else -50.0 * (i - j)
                if best[j][0] + val > best[i][0]:
                    best[i] = (best[j][0] + val, j)
        segs, i = [], n
        while i > 0:
            j = best[i][1]
            segs.append(' '.join(words[j:i]))
            i = j
        return segs[::-1]


def match_text(conn, location, text, count_type='all'):
    """[{phrase, candidates:[...]}] in the order spoken."""
    m = Matcher(conn, location, count_type)
    out = []
    for chunk in _SPLIT.split(text or ''):
        chunk = (chunk or '').strip()
        if not norm(chunk):
            continue
        for seg in m.segment(chunk):
            out.append({'phrase': seg, 'candidates': m.candidates(seg)})
    return out
