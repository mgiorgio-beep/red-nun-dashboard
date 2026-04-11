"""
Product Mapping Routes — Vendor Item ↔ Canonical Product linking.
Vendor items come from scanned_invoice_items.
Canonicals live in _archived_product_costing.
vendor_item_links is the persistent mapping table.
"""
import logging
from flask import Blueprint, jsonify, request, send_from_directory
from data_store import get_connection
from auth_routes import login_required

logger = logging.getLogger(__name__)

mapping_bp = Blueprint('mapping', __name__)

# Categories that get mapped (food + beverage)
MAPPABLE_CATS = ('FOOD', 'BEER', 'WINE', 'LIQUOR', 'NA_BEVERAGES')
SKIP_CATS = ('NON_COGS', 'TOGO_SUPPLIES', 'DR_SUPPLIES', 'KITCHEN_SUPPLIES')


@mapping_bp.route('/product-mapping')
@login_required
def mapping_page():
    return send_from_directory("static", "product_mapping.html")


# ── Stats ──────────────────────────────────────────────────────
@mapping_bp.route('/api/product-mapping/stats')
@login_required
def pm_stats():
    conn = get_connection()
    placeholders = ','.join('?' * len(MAPPABLE_CATS))

    total = conn.execute(
        f"SELECT COUNT(DISTINCT product_name) FROM scanned_invoice_items WHERE category_type IN ({placeholders})",
        MAPPABLE_CATS
    ).fetchone()[0]

    auto_linked = conn.execute("SELECT COUNT(*) FROM vendor_item_links WHERE auto_linked = 1").fetchone()[0]

    suggested = conn.execute(
        "SELECT COUNT(*) FROM vendor_item_links WHERE auto_linked = 0 AND confidence >= 60 AND confidence < 85"
    ).fetchone()[0]

    linked_manual = conn.execute(
        "SELECT COUNT(*) FROM vendor_item_links WHERE auto_linked = 0 AND (confidence IS NULL OR confidence >= 85)"
    ).fetchone()[0]

    all_linked_names = conn.execute("SELECT COUNT(*) FROM vendor_item_links").fetchone()[0]

    # Unlinked = food/bev vendor items not in vendor_item_links at all
    unlinked = conn.execute(f"""
        SELECT COUNT(DISTINCT sii.product_name) FROM scanned_invoice_items sii
        WHERE sii.category_type IN ({placeholders})
        AND NOT EXISTS (
            SELECT 1 FROM vendor_item_links vil
            WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(sii.product_name))
        )
    """, MAPPABLE_CATS).fetchone()[0]

    skip_ph = ','.join('?' * len(SKIP_CATS))
    skipped = conn.execute(
        f"SELECT COUNT(DISTINCT product_name) FROM scanned_invoice_items WHERE category_type IN ({skip_ph})",
        SKIP_CATS
    ).fetchone()[0]

    conn.close()
    return jsonify({
        'total_vendor_items': total,
        'auto_linked': auto_linked,
        'suggested': suggested,
        'unlinked': unlinked,
        'skipped': skipped,
        'manual_linked': linked_manual,
    })


# ── Suggested (60-84% confidence, needs confirm) ──────────────
@mapping_bp.route('/api/product-mapping/suggested')
@login_required
def pm_suggested():
    conn = get_connection()
    rows = conn.execute("""
        SELECT vil.vendor_item_name, vil.canonical_product_name, vil.confidence,
               sii.unit_price, si.vendor_name
        FROM vendor_item_links vil
        LEFT JOIN scanned_invoice_items sii
            ON LOWER(TRIM(sii.product_name)) = LOWER(TRIM(vil.vendor_item_name))
        LEFT JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE vil.auto_linked = 0 AND vil.confidence >= 60 AND vil.confidence < 85
        GROUP BY vil.vendor_item_name
        ORDER BY vil.confidence DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Unlinked (no match at all, food/bev only) ─────────────────
@mapping_bp.route('/api/product-mapping/unlinked')
@login_required
def pm_unlinked():
    conn = get_connection()
    placeholders = ','.join('?' * len(MAPPABLE_CATS))
    rows = conn.execute(f"""
        SELECT sii.product_name as vendor_item_name, sii.category_type as category,
               si.vendor_name, sii.unit_price as price
        FROM scanned_invoice_items sii
        LEFT JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE sii.category_type IN ({placeholders})
        AND NOT EXISTS (
            SELECT 1 FROM vendor_item_links vil
            WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(sii.product_name))
        )
        GROUP BY sii.product_name
        ORDER BY sii.product_name
    """, MAPPABLE_CATS).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Linked (all confirmed mappings) ───────────────────────────
@mapping_bp.route('/api/product-mapping/linked')
@login_required
def pm_linked():
    conn = get_connection()
    search = request.args.get('search', '').strip()
    query = """
        SELECT vil.vendor_item_name, vil.canonical_product_name, vil.confidence,
               vil.auto_linked, vil.created_at,
               sii.unit_price, si.vendor_name, si.invoice_date
        FROM vendor_item_links vil
        LEFT JOIN scanned_invoice_items sii
            ON LOWER(TRIM(sii.product_name)) = LOWER(TRIM(vil.vendor_item_name))
        LEFT JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE (vil.auto_linked = 1 OR vil.confidence IS NULL OR vil.confidence >= 85)
    """
    params = []
    if search:
        query += " AND (vil.vendor_item_name LIKE ? OR vil.canonical_product_name LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    query += " GROUP BY vil.vendor_item_name ORDER BY vil.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Unlink a linked item ─────────────────────────────────────
@mapping_bp.route('/api/product-mapping/unlink', methods=['POST'])
@login_required
def pm_unlink():
    data = request.json
    vi_name = (data.get('vendor_item_name') or '').strip()
    if not vi_name:
        return jsonify({'error': 'vendor_item_name required'}), 400
    conn = get_connection()
    conn.execute("DELETE FROM vendor_item_links WHERE LOWER(TRIM(vendor_item_name)) = LOWER(TRIM(?))", (vi_name,))
    conn.execute("""
        UPDATE scanned_invoice_items SET canonical_product_name = NULL, auto_linked = 0
        WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
    """, (vi_name,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ── Confirm a suggestion ──────────────────────────────────────
@mapping_bp.route('/api/product-mapping/confirm', methods=['POST'])
@login_required
def pm_confirm():
    data = request.json
    vi_name = (data.get('vendor_item_name') or '').strip()
    canon = (data.get('canonical_product_name') or '').strip()
    if not vi_name or not canon:
        return jsonify({'error': 'vendor_item_name and canonical_product_name required'}), 400

    conn = get_connection()

    # Upsert into vendor_item_links
    conn.execute("""
        INSERT INTO vendor_item_links (vendor_item_name, canonical_product_name, confidence, auto_linked)
        VALUES (?, ?, NULL, 0)
        ON CONFLICT(vendor_item_name) DO UPDATE SET
            canonical_product_name = excluded.canonical_product_name,
            confidence = NULL,
            auto_linked = CASE WHEN vendor_item_links.auto_linked = 1 THEN 1 ELSE 0 END
    """, (vi_name, canon))

    # Update scanned_invoice_items
    conn.execute("""
        UPDATE scanned_invoice_items
        SET canonical_product_name = ?, auto_linked = 0
        WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
    """, (canon, vi_name))

    # Price resolution: update _archived_product_costing with latest price
    price_row = conn.execute("""
        SELECT sii.unit_price, si.vendor_name, si.invoice_date
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE LOWER(TRIM(sii.product_name)) = LOWER(TRIM(?))
        ORDER BY si.invoice_date DESC LIMIT 1
    """, (vi_name,)).fetchone()

    if price_row and price_row['unit_price'] and price_row['unit_price'] > 0:
        conn.execute("""
            UPDATE _archived_product_costing SET
                case_price = ?,
                vendor_name = COALESCE(?, vendor_name),
                cost_per_recipe_unit = CASE
                    WHEN units_per_case IS NOT NULL AND units_per_case > 0
                    THEN ROUND(? / units_per_case, 4)
                    ELSE cost_per_recipe_unit
                END,
                updated_at = datetime('now')
            WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
        """, (price_row['unit_price'], price_row['vendor_name'],
              price_row['unit_price'], canon))

    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ── Reject a suggestion → moves to unlinked ───────────────────
@mapping_bp.route('/api/product-mapping/reject-suggestion', methods=['POST'])
@login_required
def pm_reject():
    data = request.json
    vi_name = (data.get('vendor_item_name') or '').strip()
    if not vi_name:
        return jsonify({'error': 'vendor_item_name required'}), 400
    conn = get_connection()
    conn.execute("DELETE FROM vendor_item_links WHERE LOWER(TRIM(vendor_item_name)) = LOWER(TRIM(?))", (vi_name,))
    conn.execute("""
        UPDATE scanned_invoice_items SET canonical_product_name = NULL, auto_linked = 0
        WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
    """, (vi_name,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ── Create new canonical + link ────────────────────────────────
@mapping_bp.route('/api/product-mapping/create-canonical', methods=['POST'])
@login_required
def pm_create_canonical():
    data = request.json
    name = (data.get('product_name') or '').strip()
    category = data.get('category', 'Food')
    case_price = data.get('case_price')
    vi_name = (data.get('vendor_item_name') or '').strip()
    if not name:
        return jsonify({'error': 'product_name required'}), 400

    conn = get_connection()

    # Create in _archived_product_costing
    conn.execute("""
        INSERT OR IGNORE INTO _archived_product_costing (product_name, category, case_price, is_canonical, updated_at)
        VALUES (?, ?, ?, 1, datetime('now'))
    """, (name, category, case_price))

    # If a vendor item triggered this, auto-link it
    if vi_name:
        conn.execute("""
            INSERT INTO vendor_item_links (vendor_item_name, canonical_product_name, confidence, auto_linked)
            VALUES (?, ?, NULL, 0)
            ON CONFLICT(vendor_item_name) DO UPDATE SET
                canonical_product_name = excluded.canonical_product_name,
                confidence = NULL
        """, (vi_name, name))
        conn.execute("""
            UPDATE scanned_invoice_items SET canonical_product_name = ?, auto_linked = 0
            WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
        """, (name, vi_name))

    conn.commit()

    row = conn.execute("SELECT * FROM _archived_product_costing WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))", (name,)).fetchone()
    conn.close()
    return jsonify({'success': True, 'canonical': dict(row) if row else {'product_name': name}})


# ── Search canonicals ──────────────────────────────────────────
@mapping_bp.route('/api/product-mapping/search-canonicals')
@login_required
def pm_search_canonicals():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    conn = get_connection()
    query = "SELECT product_name, category, case_price, vendor_name FROM _archived_product_costing WHERE 1=1"
    params = []
    for word in q.split():
        query += " AND product_name LIKE ?"
        params.append(f'%{word}%')
    query += " ORDER BY product_name LIMIT 20"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Auto-match trigger ─────────────────────────────────────────
@mapping_bp.route('/api/product-mapping/auto-match', methods=['POST'])
@login_required
def pm_auto_match_trigger():
    count = auto_match_vendor_items()
    return jsonify({'success': True, 'matched': count})


# ═══════════════════════════════════════════════════════════════
# Auto-fuzzy-match engine (5-rule algorithm)
# ═══════════════════════════════════════════════════════════════
import re as _re

_STOPWORDS = {'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'and',
              'or', 'not', 'with', 'w', 'ct', 'oz', 'lb', 'gal', 'pk', 'bag',
              'box', 'btl', 'can', 'jug', 'jar', 'tub', 'ref', 'fzn', 'frz',
              'frs', 'shlf', 'plst', 'fresh', 'frozen'}


def _tokenize(s):
    return _re.findall(r'[a-z]{2,}', s.lower())


def _significant_tokens(s):
    return [t for t in _tokenize(s) if len(t) >= 4 and t not in _STOPWORDS]


def _smart_match(vendor_name, canonical_name):
    """5-rule match: token_set_ratio + length ratio + key token overlap."""
    vn_lower = vendor_name.lower().strip()
    cn_lower = canonical_name.lower().strip()
    score = fuzz.token_set_ratio(vn_lower, cn_lower)
    # Rule 2: length ratio
    if len(cn_lower) / max(len(vn_lower), 1) < 0.4:
        return (score, False)
    # Rule 3: key token overlap
    canon_sig = _significant_tokens(canonical_name)
    vendor_tokens = set(_tokenize(vendor_name))
    if canon_sig and not any(t in vendor_tokens for t in canon_sig):
        return (score, False)
    return (score, True)


# Rule 4: only match FOOD items to FOOD canonicals
_FOOD_CATS = ('FOOD', 'NA_BEVERAGES')


def auto_match_vendor_items(invoice_id=None):
    """
    Match unlinked vendor items to _archived_product_costing canonicals using rapidfuzz.
    Uses 5-rule algorithm: token_set_ratio, length ratio, key token overlap,
    category gate (FOOD only), raised thresholds (90% auto, 75% suggest).
    """
    try:
        from rapidfuzz import fuzz as _fuzz
    except ImportError:
        logger.warning("rapidfuzz not installed — skipping auto-match")
        return 0

    conn = get_connection()
    food_ph = ','.join('?' * len(_FOOD_CATS))

    # Get unlinked vendor items — FOOD/NA_BEVERAGES only (Rule 4)
    if invoice_id:
        unlinked = conn.execute(f"""
            SELECT DISTINCT sii.product_name, sii.category_type
            FROM scanned_invoice_items sii
            WHERE sii.invoice_id = ?
            AND sii.category_type IN ({food_ph})
            AND NOT EXISTS (
                SELECT 1 FROM vendor_item_links vil
                WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(sii.product_name))
            )
        """, (invoice_id, *_FOOD_CATS)).fetchall()
    else:
        unlinked = conn.execute(f"""
            SELECT DISTINCT sii.product_name, sii.category_type
            FROM scanned_invoice_items sii
            WHERE sii.category_type IN ({food_ph})
            AND NOT EXISTS (
                SELECT 1 FROM vendor_item_links vil
                WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(sii.product_name))
            )
        """, _FOOD_CATS).fetchall()

    # Get all canonicals
    canonicals = conn.execute("SELECT product_name FROM _archived_product_costing").fetchall()
    canon_names = [r['product_name'] for r in canonicals]

    if not canon_names or not unlinked:
        conn.close()
        return 0

    matched = 0
    for row in unlinked:
        vi_name = row['product_name']
        if not vi_name:
            continue

        best_score = 0
        best_canon = None

        for cn in canon_names:
            score, passes = _smart_match(vi_name, cn)
            if passes and score > best_score:
                best_score = score
                best_canon = cn

        # Rule 5: 90% auto, 75-89% suggest
        if best_score >= 75 and best_canon:
            auto = 1 if best_score >= 90 else 0
            try:
                conn.execute("""
                    INSERT INTO vendor_item_links (vendor_item_name, canonical_product_name, confidence, auto_linked)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(vendor_item_name) DO UPDATE SET
                        canonical_product_name = excluded.canonical_product_name,
                        confidence = excluded.confidence,
                        auto_linked = excluded.auto_linked
                """, (vi_name, best_canon, best_score, auto))

                if auto:
                    conn.execute("""
                        UPDATE scanned_invoice_items
                        SET canonical_product_name = ?, auto_linked = 1
                        WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
                    """, (best_canon, vi_name))
                    matched += 1
            except Exception as e:
                logger.warning(f"Auto-match insert failed for '{vi_name}': {e}")

    conn.commit()
    conn.close()
    logger.info(f"Auto-match: {matched} items auto-linked, {len(unlinked)} processed")
    return matched


def apply_existing_links(invoice_id, conn):
    """
    For a just-confirmed invoice, apply any existing vendor_item_links
    to its line items automatically.
    """
    conn.execute("""
        UPDATE scanned_invoice_items
        SET canonical_product_name = (
            SELECT vil.canonical_product_name
            FROM vendor_item_links vil
            WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(scanned_invoice_items.product_name))
            LIMIT 1
        ),
        auto_linked = 1
        WHERE invoice_id = ?
        AND canonical_product_name IS NULL
        AND EXISTS (
            SELECT 1 FROM vendor_item_links vil
            WHERE LOWER(TRIM(vil.vendor_item_name)) = LOWER(TRIM(scanned_invoice_items.product_name))
        )
    """, (invoice_id,))
