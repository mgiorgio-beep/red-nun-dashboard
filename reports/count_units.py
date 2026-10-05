"""
What one counted unit of a product costs.

Counts are in the count unit (products.inventory_unit, falling back to unit):
liquor and wine bought by the case are counted in bottles (2026-10-05). Prices
follow invoices in the purchase unit (products.current_price = the latest
invoice line). So a bottle of a case-bought product costs case price / bottles
per case, and bottles per case has to be found, never assumed:

  1. the active vendor item's pack_contains, when it counts bottles/each
     (Tito's: 12 bottle). pack_contains in ML/L/OZ is the bottle SIZE, not a count.
  2. an "N/CS" or "N/<size>" pack written in the vendor item's or product's
     pack size, description or name ("12/CS", "6/1LT", "24/CS").

No answer -> None, and callers show "price needed" instead of a guess.
Used by the transfer statement now; the weekly cost page needs the same thing.
"""

import re

_COUNTISH = {'bottle', 'bottles', 'btl', 'each', 'ea', 'ct', 'can', 'cans', 'unit', 'units'}
_CASEISH = {'case', 'cases', 'cs', 'combo'}
_PACK = re.compile(r'\b(\d{1,2})\s*/\s*(?:cs|case|c|\d+(?:\.\d+)?\s*(?:ml|m|l|lt|ltr|litr|oz|z)?)\b', re.I)


def _norm(u):
    return (u or '').strip().lower()


def bottles_per_case(p, vi):
    """(n, where) or (None, None). p and vi are product and vendor-item rows/dicts."""
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
    n, where = bottles_per_case(p, vi)
    if not n:
        return {'cost': None, 'count_unit': count_unit, 'basis': 'bottles per case unknown'}
    return {'cost': round(price / n, 4), 'count_unit': count_unit, 'basis': f'case ${price:,.2f} / {n} ({where})'}
