"""
One-time draft of kitchen words for the transfer/waste Shortcuts (brief 4A L2, L4).

For every product on either house's weekly count list plus the top 200 by 90-day
spend at each house, Claude drafts:
  - a short CARD NAME ("Fries, battered 3/8")
  - the words staff say for it ("fries", "fry", "french fries", "tidos")
Everything lands as status 'proposed'. Nothing goes live until Mike approves it on
/transfer/admin (Words tab). An alias proposed for several products at one house
is a group ("fries" -> battered + straight cut); L3/L4 narrow it at runtime.

Run by hand, once (Mike-approved spend, logged in voice_ai_calls purpose='seed'):
  venv/bin/python3 scripts/seed_voice_aliases.py --dry-run     # list what would be sent
  venv/bin/python3 scripts/seed_voice_aliases.py               # draft + store proposals
Re-running only drafts products that have no card name yet.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from integrations.toast.data_store import get_connection
from integrations.claude_models import CLAUDE_MODEL
from reports.key_items import rank_key_items
from reports import item_recognition as R

BATCH = 40
PRICE = {'claude-sonnet-4-6': (3.0, 15.0), 'claude-sonnet-5-5': (2.0, 10.0), 'claude-opus-5-5': (4.0, 20.0)}

SYSTEM = """You help a two-location seafood pub (Cape Cod) set up voice logging. Cooks, chefs, bartenders and
managers will say things to Siri like "two cases of fries to Dennis" or "waste, half a case of haddock".
Siri mishears brand names. For each product (from invoices, so names are ugly) return:
  card_name: a SHORT plain name a cook would recognize, 2-5 words, distinguishing details only
             (cut, size, flavor, brand when it matters). e.g. "Fries, battered 3/8", "Tito's Vodka 1L",
             "Bud Light keg", "Burger patties", "High Noon Peach".
  aliases:   3-8 lowercase phrases people would actually SAY for it, most common first. Include the plain
             generic word even if other products share it ("fries", "wings", "oil"), short brand-only forms
             ("titos", "jamo"), kitchen slang ("frialator oil", "fingers"), container words people attach
             ("keg of bud light"), and 1-2 likely Siri mishearings of brand names ("tidos", "titus").
             Do NOT include a distinguishing word the product lacks (sweet potato fries never get plain
             "fries"; Bud Light never gets plain "bud"). No quantities, no house names.
Answer ONLY with JSON: {"items":[{"id":<id>,"card_name":"...","aliases":["..."]}]} covering every id."""


def pick_products(conn):
    ids = []
    for loc in ('chatham', 'dennis'):
        ids += [r[0] for r in conn.execute("SELECT product_id FROM count_templates WHERE location = ?", (loc,))]
        ids += [x['product_id'] for x in rank_key_items(conn, loc, days=90)[:200]]
    seen, out = set(), []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def describe(conn, pid):
    p = conn.execute("""SELECT p.id, p.name, p.display_name, p.category, p.unit, vi.vendor_description, vi.pack_size, vi.vendor_name
                        FROM products p LEFT JOIN vendor_items vi ON vi.id = p.active_vendor_item_id WHERE p.id = ?""",
                     (pid,)).fetchone()
    bits = [p['display_name'] or '', p['name'], p['vendor_description'] or '', p['pack_size'] or '', p['vendor_name'] or '']
    seen, txt = set(), []
    for b in bits:
        if b and b.lower() not in seen:
            seen.add(b.lower())
            txt.append(b)
    return f"{p['id']} | {p['category']} | bought by {p['unit']} | " + ' | '.join(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    conn = get_connection()
    R.ensure_tables(conn)
    conn.commit()
    pids = pick_products(conn)
    have = {r[0] for r in conn.execute("SELECT product_id FROM product_card_names")}
    todo = [p for p in pids if p not in have]
    if args.limit:
        todo = todo[:args.limit]
    ctx = {loc: R.house_context(conn, loc) for loc in ('chatham', 'dennis')}
    print(f'{len(pids)} products picked, {len(todo)} without a card name; model {CLAUDE_MODEL}')
    if args.dry_run:
        for pid in todo[:20]:
            print('  ' + describe(conn, pid))
        return
    import anthropic
    client = anthropic.Anthropic(timeout=180.0, max_retries=2)
    pin, pout = PRICE.get(CLAUDE_MODEL, (5.0, 25.0))
    total = 0.0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        user = 'Products (id | category | how bought | names):\n' + '\n'.join(describe(conn, p) for p in chunk)
        t0 = time.time()
        resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=8000, system=SYSTEM,
                                      messages=[{'role': 'user', 'content': user}])
        text = ''.join(b.text for b in resp.content if b.type == 'text')
        cost = resp.usage.input_tokens * pin / 1e6 + resp.usage.output_tokens * pout / 1e6
        total += cost
        m = re.search(r'\{.*\}', text, re.S)
        items = json.loads(m.group(0)).get('items', []) if m else []
        ok = 0
        for it in items:
            pid = it.get('id')
            if pid not in chunk:
                continue          # never trust an id we didn't send
            name = (it.get('card_name') or '').strip()[:60]
            if name:
                conn.execute("""INSERT OR IGNORE INTO product_card_names (product_id, card_name, status, source)
                                VALUES (?, ?, 'proposed', 'seed_claude')""", (pid, name))
            for loc in ('chatham', 'dennis'):
                if pid not in ctx[loc]['ids']:
                    continue      # aliases only where the house actually has the product
                for a in (it.get('aliases') or [])[:8]:
                    key = R.alias_key(a)
                    if key:
                        conn.execute("""INSERT OR IGNORE INTO product_aliases
                                        (location, alias_text, product_id, source, status, created_by)
                                        VALUES (?, ?, ?, 'seed_claude', 'proposed', 'seed_voice_aliases')""",
                                     (loc, key, pid))
            ok += 1
        conn.execute("""INSERT INTO voice_ai_calls (purpose, model, input_tokens, output_tokens, cost_usd, latency_ms, result)
                        VALUES ('seed', ?, ?, ?, ?, ?, ?)""",
                     (CLAUDE_MODEL, resp.usage.input_tokens, resp.usage.output_tokens, round(cost, 4),
                      int((time.time() - t0) * 1000), f'{ok}/{len(chunk)} products'))
        conn.commit()
        print(f'  batch {i // BATCH + 1}: {ok}/{len(chunk)} products, ${cost:.3f}')
    print(f'done, ${total:.2f}')


if __name__ == '__main__':
    main()
