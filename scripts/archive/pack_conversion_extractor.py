"""
pack_conversion_extractor.py
Extracts per-unit weight/volume conversions from invoice pack descriptions.
Used by invoice OCR pipeline to auto-populate product_unit_conversions.

Confidence levels (most to least trusted):
  'invoice_high'        — conversion stated directly on invoice line
  'invoice_calculated'  — derived from count ÷ weight on invoice
  'industry_standard'   — from INDUSTRY_STANDARDS fallback table (estimate only)
"""

import re

# ---------------------------------------------------------------------------
# Bulk product patterns — these products are sold by weight, no conversion
# needed. Recipe should use oz or lb directly.
# ---------------------------------------------------------------------------
BULK_PATTERNS = [
    r'^\d+\s*LB$',                  # "20 LB"
    r'^\d+\s*OZ$',                  # "5 OZ"
    r'\b(BULK|LOOSE|BY\s+THE\s+(LB|POUND|OZ|OUNCE))\b',
]

# ---------------------------------------------------------------------------
# Regex patterns for extracting conversions from invoice pack descriptions
# Examples of invoice pack strings: "4/3 LB", "20/8 OZ", "6/24/1 OZ",
# "12 CT / 6 OZ", "2/5 LB", "144 EA / 0.5 OZ"
# ---------------------------------------------------------------------------
PATTERNS = [
    # "12 CT / 1.5 OZ" or "12 / 1.5 OZ" — count / weight
    {
        'name': 'count_slash_weight',
        'regex': r'(\d+\.?\d*)\s*(?:CT|COUNT|EA|PC|PCS|EACH)?\s*/\s*(\d+\.?\d*)\s*(OZ|LB|G|ML|FL\s*OZ)',
        'confidence': 'invoice_calculated',
        'extract': lambda m: {
            'from_qty': 1,
            'from_unit': 'each',
            'to_qty': round(float(m.group(2)), 4),
            'to_unit': m.group(3).lower().replace(' ', ''),
            'note': f'Calculated from {m.group(1)} ct × {m.group(2)} {m.group(3)}'
        }
    },
    # "4/3 LB" — case/count / weight per unit (e.g. 4 bags of 3 lb)
    {
        'name': 'case_count_weight',
        'regex': r'^(\d+)/(\d+\.?\d*)\s*(LB|OZ|G|KG)$',
        'confidence': 'invoice_calculated',
        'extract': lambda m: {
            'from_qty': 1,
            'from_unit': 'each',
            'to_qty': round(float(m.group(2)) * {'LB': 16, 'OZ': 1, 'G': 0.035274, 'KG': 35.274}[m.group(3)], 4),
            'to_unit': 'oz',
            'note': f'Calculated from {m.group(1)} units × {m.group(2)} {m.group(3)}'
        }
    },
    # "1 EACH = 8 OZ" — direct statement
    {
        'name': 'direct_each_weight',
        'regex': r'1\s*(?:EACH|EA|PC)\s*=\s*(\d+\.?\d*)\s*(OZ|LB|G|ML|FL\s*OZ)',
        'confidence': 'invoice_high',
        'extract': lambda m: {
            'from_qty': 1,
            'from_unit': 'each',
            'to_qty': round(float(m.group(1)), 4),
            'to_unit': m.group(2).lower().replace(' ', ''),
            'note': f'Stated directly on invoice: 1 each = {m.group(1)} {m.group(2)}'
        }
    },
]

# ---------------------------------------------------------------------------
# Industry standard weights
# Source: USDA food database + common operator references
# ESTIMATES only — always flagged as 'industry_standard' in UI
# Keys are partial product name matches (lowercase)
# ---------------------------------------------------------------------------
INDUSTRY_STANDARDS = [

    # PROTEINS — PORTIONED
    {'match': ['burger patty', 'beef patty', 'hamburger patty'],
     'from_unit': 'each', 'to_qty': 4.0, 'to_unit': 'oz',
     'note': 'Industry std: 4 oz burger (verify — may be 6 oz or 8 oz)'},

    {'match': ['chicken breast', 'chx breast', 'brst bnls'],
     'from_unit': 'each', 'to_qty': 6.0, 'to_unit': 'oz',
     'note': 'Industry std: 6 oz boneless breast portion'},

    {'match': ['chicken wing', 'chx wing', 'wing sect', 'wingette'],
     'from_unit': 'each', 'to_qty': 2.0, 'to_unit': 'oz',
     'note': 'Industry std: ~2 oz per wing section'},

    {'match': ['chicken tender', 'chx tender', 'chicken strip', 'chx strip'],
     'from_unit': 'each', 'to_qty': 1.5, 'to_unit': 'oz',
     'note': 'Industry std: ~1.5 oz per tender/strip'},

    {'match': ['shrimp'],
     'from_unit': 'each', 'to_qty': 0.67, 'to_unit': 'oz',
     'note': 'Industry std: ~0.67 oz (21/25 count avg) — verify count size'},

    # APPETIZERS / FROZEN PORTIONS
    {'match': ['mozz', 'mozzarella stick', 'mozz stick'],
     'from_unit': 'each', 'to_qty': 1.1, 'to_unit': 'oz',
     'note': 'Industry std: ~1.1 oz per breaded mozz stick'},

    {'match': ['onion ring'],
     'from_unit': 'each', 'to_qty': 0.5, 'to_unit': 'oz',
     'note': 'Industry std: ~0.5 oz per onion ring'},

    {'match': ['egg roll'],
     'from_unit': 'each', 'to_qty': 2.0, 'to_unit': 'oz',
     'note': 'Industry std: ~2 oz per egg roll'},

    {'match': ['spring roll'],
     'from_unit': 'each', 'to_qty': 1.0, 'to_unit': 'oz',
     'note': 'Industry std: ~1 oz per spring roll'},

    # PRODUCE
    {'match': ['lettuce iceberg', 'iceberg lettuce', 'head lettuce'],
     'from_unit': 'each', 'to_qty': 26.0, 'to_unit': 'oz',
     'note': 'Industry std: ~26 oz per iceberg head (USDA avg)'},

    {'match': ['romaine', 'romaine heart'],
     'from_unit': 'each', 'to_qty': 12.0, 'to_unit': 'oz',
     'note': 'Industry std: ~12 oz per romaine heart'},

    {'match': ['tomato'],
     'from_unit': 'each', 'to_qty': 5.0, 'to_unit': 'oz',
     'note': 'Industry std: ~5 oz per medium tomato (USDA)'},

    {'match': ['lemon'],
     'from_unit': 'each', 'to_qty': 3.5, 'to_unit': 'oz',
     'note': 'Industry std: ~3.5 oz per lemon (USDA)'},

    {'match': ['lime'],
     'from_unit': 'each', 'to_qty': 2.5, 'to_unit': 'oz',
     'note': 'Industry std: ~2.5 oz per lime (USDA)'},

    {'match': ['onion', 'yellow onion', 'white onion'],
     'from_unit': 'each', 'to_qty': 7.0, 'to_unit': 'oz',
     'note': 'Industry std: ~7 oz per medium onion (USDA)'},

    {'match': ['avocado'],
     'from_unit': 'each', 'to_qty': 5.0, 'to_unit': 'oz',
     'note': 'Industry std: ~5 oz per Hass avocado (USDA)'},

    # BREAD / BAKED
    {'match': ['dinner roll', 'roll'],
     'from_unit': 'each', 'to_qty': 1.5, 'to_unit': 'oz',
     'note': 'Industry std: ~1.5 oz per dinner roll'},

    {'match': ['hamburger bun', 'burger bun', 'bun'],
     'from_unit': 'each', 'to_qty': 2.0, 'to_unit': 'oz',
     'note': 'Industry std: ~2 oz per burger bun'},

    {'match': ['hot dog bun'],
     'from_unit': 'each', 'to_qty': 1.5, 'to_unit': 'oz',
     'note': 'Industry std: ~1.5 oz per hot dog bun'},

    # DAIRY / EGGS
    {'match': ['egg'],
     'from_unit': 'each', 'to_qty': 1.75, 'to_unit': 'oz',
     'note': 'Industry std: ~1.75 oz per large egg (USDA)'},

    {'match': ['butter pat', 'butter portion'],
     'from_unit': 'each', 'to_qty': 0.5, 'to_unit': 'oz',
     'note': 'Industry std: ~0.5 oz per butter pat'},
]


def lookup_industry_standard(product_name):
    """
    Look up industry standard weight by product name matching.
    Returns suggestion dict or None.
    Always marked confidence='industry_standard'.
    """
    name_lower = (product_name or '').lower()
    for standard in INDUSTRY_STANDARDS:
        for keyword in standard['match']:
            if keyword in name_lower:
                return {
                    'result': {
                        'from_qty': 1,
                        'from_unit': standard['from_unit'],
                        'to_qty':   standard['to_qty'],
                        'to_unit':  standard['to_unit'],
                        'note':     standard['note']
                    },
                    'confidence': 'industry_standard',
                    'note':       standard['note'],
                    'skip':       False
                }
    return None


def extract_conversion(pack_description, product_name=''):
    """
    Extract per-unit weight/volume conversion from an invoice pack description.

    Returns dict:
      result       - {from_qty, from_unit, to_qty, to_unit, note} or None
      confidence   - 'invoice_high' | 'invoice_calculated' | 'industry_standard' | None
      note         - human-readable explanation
      skip         - True if product is bulk (use oz/lb directly in recipes)
      pattern      - pattern name that matched (if any)
    """
    if not pack_description and not product_name:
        return {'result': None, 'confidence': None, 'note': 'No data', 'skip': False}

    text = (pack_description or '').upper().strip()

    # Check bulk patterns first — these products don't need each→oz conversions
    for bulk_pat in BULK_PATTERNS:
        if re.search(bulk_pat, text):
            return {'result': None, 'confidence': None,
                    'note': 'Bulk weight product — use oz/lb in recipes directly',
                    'skip': True}

    # Try invoice pattern extraction
    for pattern in PATTERNS:
        m = re.search(pattern['regex'], text)
        if m:
            try:
                result = pattern['extract'](m)
                return {
                    'result':     result,
                    'confidence': pattern['confidence'],
                    'note':       result.get('note', ''),
                    'skip':       False,
                    'pattern':    pattern['name']
                }
            except Exception:
                continue

    # Fall back to industry standards
    if product_name:
        std = lookup_industry_standard(product_name)
        if std:
            return std

    return {'result': None, 'confidence': None,
            'note': 'No pattern matched', 'skip': False}
