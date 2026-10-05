"""
What one counted unit of a product costs.

Counts are in the count unit (products.inventory_unit, falling back to unit):
liquor and wine bought by the case are counted in bottles (2026-10-05). Prices
follow invoices in the purchase unit (products.current_price = the latest
invoice line). So a bottle of a case-bought product costs case price / bottles
per case, and bottles per case has to be found, never assumed:

  0. a "1 case = N bottle/can/each" row in product_unit_conversions (the fixer's
     format, which recipe costing also reads) — explicit beats inferred.
  1. the active vendor item's pack_contains, when it counts bottles/each
     (Tito's: 12 bottle). pack_contains in ML/L/OZ is the bottle SIZE, not a count.
  2. an "N/CS" or "N/<size>" pack written in the vendor item's or product's
     pack size, description or name ("12/CS", "6/1LT", "24/CS").

No answer -> None, and callers show "price needed" instead of a guess.
Used by the transfer statement now; the weekly cost page needs the same thing.
"""

import re

LIQUOR_BOTTLE_FLOOR = 4.00
_COUNTISH = {'bottle', 'bottles', 'btl', 'each', 'ea', 'ct', 'can', 'cans', 'unit', 'units'}
_CASEISH = {'case', 'cases', 'cs', 'combo'}
_PACK = re.compile(r'\b(\d{1,2})\s*/\s*(?:cs|case|c|\d+(?:\.\d+)?\s*(?:ml|m|l|lt|ltr|litr|oz|z)?)\b', re.I)


def _norm(u):
    return (u or '').strip().lower()


def bottles_per_case(p, vi, conn=None):
    """(n, where) or (None, None). p and vi are product and vendor-item rows/dicts."""
    if conn is not None:
        r = conn.execute("""
            SELECT from_qty, to_qty FROM product_unit_conversions
            WHERE product_id = ? AND LOWER(from_unit) IN ('case', 'cases', 'cs')
              AND LOWER(to_unit) IN ('bottle', 'bottles', 'btl', 'can', 'cans', 'each', 'ea', 'ct')
            ORDER BY id DESC LIMIT 1""", (p['id'],)).fetchone()
        if r and r['from_qty'] and r['to_qty'] and r['to_qty'] / r['from_qty'] > 1:
            return int(round(r['to_qty'] / r['from_qty'])), 'conversion'
    if vi and vi['pack_contains'] and vi['pack_contains'] > 1 and _norm(vi['contains_unit']) in _COUNTISH:
        return int(vi['pack_contains']), 'invoice pack'
    for src in ((vi['pack_size'] if vi else None), p['pack_size'],
                (vi['vendor_description'] if vi else None), p['name'], p['display_name']):
        m = _PACK.search(str(src or ''))
        if m and int(m.group(1)) > 1:
            return int(m.group(1)), 'pack in name'
    return None, None


def unit_cost(conn, product_id, count_unit=None):
    """{'cost', 'count_unit', 'basis'} for one `count_unit` (default: how the product is counted
    now). Pass the unit a count or transfer RECORDED, so a later change to how a product
    is counted can't reprice old rows. cost None when unknown."""
    p = conn.execute("""
        SELECT id, name, display_name, unit, inventory_unit, pack_size, current_price, active_vendor_item_id
        FROM products WHERE id = ?""", (product_id,)).fetchone()
    if not p:
        return {'cost': None, 'count_unit': None, 'basis': 'unknown product'}
    vi = conn.execute("""
        SELECT purchase_price, pack_size, pack_contains, contains_unit, vendor_description
        FROM vendor_items WHERE id = ?""", (p['active_vendor_item_id'],)).fetchone() if p['active_vendor_item_id'] else None
    price = (vi['purchase_price'] if vi and vi['purchase_price'] else None) or p['current_price']
    count_unit = count_unit or p['inventory_unit'] or p['unit']
    if not price or price <= 0:
        return {'cost': None, 'count_unit': count_unit, 'basis': 'no invoice price'}
    bought, counted = _norm(p['unit']), _norm(count_unit)
    if counted == bought or bought not in _CASEISH or counted in _CASEISH:
        return {'cost': round(price, 4), 'count_unit': count_unit, 'basis': 'invoice price'}
    n, where = bottles_per_case(p, vi, conn)
    if not n:
        return {'cost': None, 'count_unit': count_unit, 'basis': 'bottles per case unknown'}
    return {'cost': round(price / n, 4), 'count_unit': count_unit, 'basis': f'case ${price:,.2f} / {n} ({where})'}


def read_purchase_lines(conn, product_id, lines):
    """Invoice lines -> quantity in the product's count unit, without trusting the
    case/bottle label blindly. `lines`: dicts with quantity, unit, unit_price.
    Returns the lines with count_qty (None = skipped or unknown) and why.

    - $1 lines are backorders/deposits, not stock ("4 bottles backorder @ $1.00").
    - With bottles per case (n) known, the PRICE decides: a line priced like a
      case is cases, one priced like a bottle is bottles, whatever the label says
      ("1 bottle @ $47" for a $47 case of High Noon). Prices of one product split
      into two groups n apart; one group alone means every line is the same kind,
      and that group's majority label wins (an odd label out is the mislabel).
    - A liquor "case" that works out under LIQUOR_BOTTLE_FLOOR a bottle is a bottle
      (Southern Glazer's bills Bacardi 1L "case" @ $21.70). The floor sits under
      well vodka ($6), so a real cheap case is never misread.
    Known blind spot: every line the same wrong label at a price above the floor
    (Grey Goose "6/CS case" @ $40.94 is really one bottle) can't be caught from
    its own lines.
    """
    p = conn.execute("SELECT id, name, display_name, category, unit, inventory_unit, pack_size, active_vendor_item_id FROM products WHERE id = ?",
                     (product_id,)).fetchone()
    vi = conn.execute("SELECT pack_size, pack_contains, contains_unit, vendor_description FROM vendor_items WHERE id = ?",
                      (p['active_vendor_item_id'],)).fetchone() if p and p['active_vendor_item_id'] else None
    count_unit = _norm((p['inventory_unit'] or p['unit']) if p else '')
    n, _ = bottles_per_case(p, vi, conn) if p else (None, None)
    # OCR sometimes misses the unit price: total / quantity, never the bare total
    lines = [dict(l, unit_price=(l.get('unit_price') or
                                 (round(l['total_price'] / l['quantity'], 4) if l.get('total_price') and (l.get('quantity') or 0) > 0 else 0)))
             for l in lines]
    real = [l for l in lines if (l.get('unit_price') or 0) > 1.01 and (l.get('quantity') or 0) != 0]
    out = []
    hi = max((l['unit_price'] for l in real), default=0)
    split = n and n > 1 and real and min(l['unit_price'] for l in real) < hi / (n ** 0.5)
    group_kind = {}
    if n and n > 1 and real and not split:
        labels = [('case' if _norm(l.get('unit')) in _CASEISH else 'each') for l in real]
        group_kind['all'] = max(set(labels), key=labels.count)
    for l in lines:
        q, price, lab = l.get('quantity') or 0, l.get('unit_price') or 0, _norm(l.get('unit'))
        if price <= 1.01 or q == 0:
            out.append(dict(l, count_qty=None, why='skipped: $1 or zero line (backorder/deposit)'))
            continue
        if not n or n <= 1:
            same = lab == count_unit or (lab in _CASEISH) == (count_unit in _CASEISH)
            if not same and lab in _CASEISH and any(
                    _norm(o.get('unit')) not in _CASEISH and abs(o['unit_price'] - price) <= 0.1 * price for o in real):
                # the same price also appears labeled as a bottle: a case can't cost
                # what one bottle does, so this "case" is a bottle (Tanqueray $32.60)
                out.append(dict(l, count_qty=q, why=f'label says {lab}, same price as its bottle lines'))
                continue
            out.append(dict(l, count_qty=q if same else None, why='as labeled' if same else 'unit differs, bottles per case unknown'))
            continue
        kind = ('case' if price >= hi / (n ** 0.5) else 'each') if split else group_kind['all']
        if kind == 'case' and (p['category'] or '').upper() == 'LIQUOR' and count_unit not in _CASEISH \
                and price / n < LIQUOR_BOTTLE_FLOOR:
            kind = 'each'   # "case" of 1L Bacardi @ $21.70 = $1.81 a bottle: it's one bottle
        labeled = 'case' if lab in _CASEISH else 'each'
        if count_unit in _CASEISH:
            cq = q if kind == 'case' else q / n
        else:
            cq = q * n if kind == 'case' else q
        out.append(dict(l, count_qty=cq, why=('as labeled' if kind == labeled else
                                               f"label says {lab}, price says {'case' if kind == 'case' else count_unit}")))
    return out
