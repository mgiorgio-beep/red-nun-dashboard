"""
L7: does the walk-in voice parser pick the right product? (brief 4A)

tests/voice_phrases.json holds how staff actually talk, each with the product(s)
that are right at that house and any that must never come back (sweet potato
fries for "fries"). Targets before staff touch the Shortcut:
  - >= 95% right product on the confirm card with no "Which one?" step
  - 100% right product somewhere in the "Which one?" list
  - 0 'never' products returned
A phrase marked "ambiguous" is one where the house really stocks several (Dennis
buys battered AND straight-cut fries): asking "Which one?" with the right one in
the list counts as right; a wrong card does not.
Reads the live DB; Claude fallback OFF (it is the last resort, not the score).

  venv/bin/python3 tests/test_voice_recognition.py              # approved words only
  venv/bin/python3 tests/test_voice_recognition.py --proposed   # also the unreviewed draft
  ... --holdout   held-out phrases (voice_phrases_holdout.json): never tune against these;
                  when a miss there gets fixed, move it to voice_phrases.json and write new holdouts
Re-run on every matcher change; add every real miss to the JSON.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection
from reports import item_recognition as R
from reports.house_moves import parse

HERE = os.path.dirname(os.path.abspath(__file__))


def run(proposed=False, verbose=True, fname='voice_phrases.json'):
    if proposed:
        R.LIVE_ALIAS = ('approved', 'proposed')
    R.clear_cache()
    conn = get_connection()
    cases = json.load(open(os.path.join(HERE, fname)))
    auto = listed = never_hits = 0
    misses = []
    for c in cases:
        p = parse(c['text'], c['kind'])
        r = R.recognize(conn, c['house'], p['item'], size=p['size'], allow_ai=False, unit=p['unit'])
        got = [r['product']['id']] if 'product' in r else [o['id'] for o in r.get('options', [])]
        ok_auto = bool('product' in r and r['product']['id'] in c['expect']) or bool(
                  c.get('ambiguous') and 'product' not in r and any(g in c['expect'] for g in got))
        ok_list = any(g in c['expect'] for g in got)
        bad = [g for g in got if g in c.get('never', [])]
        auto += ok_auto
        listed += ok_list
        never_hits += bool(bad)
        if not ok_auto or bad:
            misses.append((c, p['item'], 'card' if 'product' in r else 'choose' if got else 'none', got, r.get('trace'), bad))
    n = len(cases)
    res = {'cases': n, 'auto_pct': round(100 * auto / n, 1), 'list_pct': round(100 * listed / n, 1),
           'never_hits': never_hits}
    if verbose:
        print(f"{n} phrases | right on the card, no choice: {auto} ({res['auto_pct']}%) | "
              f"right in the list: {listed} ({res['list_pct']}%) | never-products returned: {never_hits}")
        for c, item, how, got, trace, bad in misses:
            names = []
            for g in got:
                row = conn.execute("SELECT COALESCE(display_name, name) FROM products WHERE id = ?", (g,)).fetchone()
                names.append(f"{g}:{row[0][:26]}")
            flag = ' NEVER!' if bad else ''
            print(f"  MISS{flag} [{c['house']}] \"{c['text']}\" item='{item}' want {c['expect']} -> {how} {' / '.join(names)}  ({trace})")
    return res


def test_voice_recognition_targets():
    res = run(verbose=False)
    assert res['never_hits'] == 0
    assert res['list_pct'] == 100.0
    assert res['auto_pct'] >= 95.0


if __name__ == '__main__':
    # --holdout: phrases written once and never tuned against (the honest number)
    out = run(proposed='--proposed' in sys.argv,
              fname='voice_phrases_holdout.json' if '--holdout' in sys.argv else 'voice_phrases.json')
    sys.exit(0 if out['never_hits'] == 0 and out['list_pct'] == 100 and out['auto_pct'] >= 95 else 1)
