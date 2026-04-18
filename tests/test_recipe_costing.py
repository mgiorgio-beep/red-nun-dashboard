"""
Table-driven unit tests for cost_ingredient() in recipe_costing.py.

Covers all resolution paths:
  - Early exits: no product_id, product not found, no price, no unit
  - price_per_unit shortcut
  - PATH 1: direct unit match
  - PATH 2a: weight-to-weight conversion
  - PATH 2b: volume-to-volume conversion
  - PATH 3A: recipe-unit conversion (weight, volume, count)
  - PATH 3B: purchase-unit conversion (volume, weight, count)
  - PATH 4: no resolution fallback
  - Liquor fl_oz correction
  - vendor_item pack_contains override
"""
import sqlite3
import pytest

from integrations.recipes.recipe_costing import cost_ingredient


@pytest.fixture
def conn():
    """In-memory SQLite with the minimal schema cost_ingredient() needs."""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE vendor_items (
            id INTEGER PRIMARY KEY,
            product_id INTEGER,
            purchase_price REAL,
            price_per_unit REAL,
            pack_size TEXT,
            pack_unit TEXT,
            pack_contains REAL,
            contains_unit TEXT,
            description TEXT
        );

        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            unit TEXT,          -- purchase_unit
            pack_size REAL,
            pack_unit TEXT,
            inventory_unit TEXT,
            active_vendor_item_id INTEGER,
            yield_pct REAL
        );

        CREATE TABLE product_unit_conversions (
            id INTEGER PRIMARY KEY,
            product_id INTEGER,
            from_qty REAL,
            from_unit TEXT,
            to_qty REAL,
            to_unit TEXT
        );
    """)
    return db


def _insert_product(conn, pid, name='Test Product', purchase_unit='lb',
                     pack_size=1, pack_unit='lb', inventory_unit=None,
                     price=10.0, price_per_unit=0, vi_pack_contains=None,
                     vi_contains_unit=None, vi_id=None):
    """Helper: insert a product + its vendor item, return product_id."""
    vid = vi_id or (pid * 100)
    conn.execute(
        "INSERT INTO vendor_items VALUES (?,?,?,?,?,?,?,?,?)",
        (vid, pid, price, price_per_unit, None, None,
         vi_pack_contains, vi_contains_unit, name))
    conn.execute(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?,?)",
        (pid, name, purchase_unit, pack_size, pack_unit,
         inventory_unit, vid, None))
    conn.commit()
    return pid


def _insert_conversion(conn, product_id, from_qty, from_unit, to_qty, to_unit):
    conn.execute(
        "INSERT INTO product_unit_conversions (product_id, from_qty, from_unit, to_qty, to_unit) VALUES (?,?,?,?,?)",
        (product_id, from_qty, from_unit, to_qty, to_unit))
    conn.commit()


# ── Early exits ──────────────────────────────────────────────────────────────

class TestEarlyExits:

    def test_no_product_id(self, conn):
        ri = {'product_id': None, 'quantity': 1, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_price'
        assert result['cost'] == 0.0

    def test_product_not_found(self, conn):
        ri = {'product_id': 9999, 'quantity': 1, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_price'
        assert result['cost'] == 0.0

    def test_zero_price(self, conn):
        _insert_product(conn, 1, price=0)
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_price'
        assert result['cost'] == 0.0

    def test_negative_price(self, conn):
        _insert_product(conn, 1, price=-5.0)
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_price'
        assert result['cost'] == 0.0

    def test_blank_recipe_unit(self, conn):
        _insert_product(conn, 1, price=25.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': ''}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_unit'
        assert result['cost'] == 0.0
        assert result['unit_price'] == 25.0

    def test_null_recipe_unit(self, conn):
        _insert_product(conn, 1, price=25.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': None}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_unit'

    def test_zero_quantity(self, conn):
        _insert_product(conn, 1, purchase_unit='oz', pack_unit='oz', price=10.0)
        ri = {'product_id': 1, 'quantity': 0, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['cost'] == 0.0


# ── price_per_unit shortcut ──────────────────────────────────────────────────

class TestPricePerUnitShortcut:

    def test_ppu_matches_pack_unit(self, conn):
        """price_per_unit used when pack_unit == recipe_unit."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='oz',
                        pack_size=16, price=40.0, price_per_unit=2.50)
        ri = {'product_id': 1, 'quantity': 4, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        assert result['unit_price'] == 2.50
        assert result['cost'] == 10.0

    def test_ppu_ignored_when_units_differ(self, conn):
        """price_per_unit NOT used when pack_unit != recipe_unit."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='oz',
                        pack_size=16, price=40.0, price_per_unit=2.50)
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'lb'}
        result = cost_ingredient(ri, conn)
        # Should fall through to PATH 2a weight conversion, not use ppu
        assert result['source'] == 'standard_conversion'


# ── PATH 1: direct unit match ────────────────────────────────────────────────

class TestPath1DirectMatch:

    def test_recipe_matches_purchase_unit(self, conn):
        _insert_product(conn, 1, purchase_unit='lb', pack_unit='lb',
                        pack_size=1, price=8.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'lb'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        assert result['cost'] == 16.0
        assert result['unit_price'] == 8.0

    def test_recipe_matches_pack_unit_with_size(self, conn):
        """10lb case at $30 → $3/lb."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='lb',
                        pack_size=10, price=30.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'lb'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        assert result['unit_price'] == 3.0
        assert result['cost'] == 6.0


# ── PATH 2a: weight conversion ───────────────────────────────────────────────

class TestPath2aWeight:

    def test_lb_to_oz(self, conn):
        """Product sold per lb ($8/lb), recipe needs oz → $0.50/oz."""
        _insert_product(conn, 1, purchase_unit='lb', pack_unit='lb',
                        pack_size=1, price=8.0)
        ri = {'product_id': 1, 'quantity': 4, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        assert result['unit_price'] == 0.5
        assert result['cost'] == 2.0

    def test_oz_to_lb(self, conn):
        """Product sold per oz at $0.50, recipe needs 2 lb → $16."""
        _insert_product(conn, 1, purchase_unit='oz', pack_unit='oz',
                        pack_size=1, price=0.50)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'lb'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        assert result['unit_price'] == 8.0
        assert result['cost'] == 16.0

    def test_case_of_lbs_to_oz(self, conn):
        """10lb case at $30, recipe uses oz → $30/(10*16) = $0.1875/oz."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='lb',
                        pack_size=10, price=30.0)
        ri = {'product_id': 1, 'quantity': 8, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        expected_per_oz = 30.0 / (10 * 16)
        assert abs(result['unit_price'] - expected_per_oz) < 0.0001
        assert abs(result['cost'] - 8 * expected_per_oz) < 0.001


# ── PATH 2b: volume conversion ───────────────────────────────────────────────

class TestPath2bVolume:

    def test_gallon_to_cup(self, conn):
        """$5/gallon, recipe needs 2 cups → $5/128*8*2."""
        _insert_product(conn, 1, purchase_unit='gallon', pack_unit='gallon',
                        pack_size=1, price=5.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'cup'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        expected_per_cup = 5.0 / 128 * 8
        assert abs(result['unit_price'] - expected_per_cup) < 0.0001

    def test_fl_oz_to_cup(self, conn):
        """$0.10/fl oz, recipe needs 1 cup (8 fl oz) → $0.80."""
        _insert_product(conn, 1, purchase_unit='fl oz', pack_unit='fl oz',
                        pack_size=1, price=0.10)
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'cup'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        assert abs(result['cost'] - 0.80) < 0.001


# ── Liquor fl_oz correction ──────────────────────────────────────────────────

class TestLiquorFlOzCorrection:

    def test_oz_rewritten_to_fl_oz_for_volume_product(self, conn):
        """Product with pack_unit 'fl oz' but vi contains_unit 'oz'
        should be treated as fl oz, not weight oz."""
        _insert_product(conn, 1, purchase_unit='bottle', pack_unit='fl oz',
                        pack_size=1, price=27.0,
                        vi_pack_contains=25.36, vi_contains_unit='oz')
        ri = {'product_id': 1, 'quantity': 1.5, 'unit': 'fl oz'}
        result = cost_ingredient(ri, conn)
        # Should resolve: pack_unit rewritten to 'fl oz', pack_size=25.36
        # per_fl_oz = 27 / 25.36 ≈ 1.0646
        assert result['source'] == 'vendor_item'
        expected = 27.0 / 25.36 * 1.5
        assert abs(result['cost'] - expected) < 0.01


# ── pack_contains override ───────────────────────────────────────────────────

class TestPackContainsOverride:

    def test_vi_pack_contains_overrides_product(self, conn):
        """Vendor item has pack_contains=160 oz (20x8oz case).
        Recipe asks for 8 oz → should cost $price/160."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='oz',
                        pack_size=20, price=40.0,
                        vi_pack_contains=160, vi_contains_unit='oz')
        ri = {'product_id': 1, 'quantity': 8, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        # pack_size overridden to 160, pack_unit stays 'oz'
        # PATH 1 match: 8 * (40/160) = 2.0
        assert result['source'] == 'vendor_item'
        assert result['cost'] == 2.0


# ── PATH 3A: recipe-unit conversions ─────────────────────────────────────────

class TestPath3ARecipeUnit:

    def test_shot_to_fl_oz(self, conn):
        """Conversion: 1 shot = 1.5 fl oz. Product sold by fl oz."""
        _insert_product(conn, 1, purchase_unit='fl oz', pack_unit='fl oz',
                        pack_size=25.36, price=27.0)
        _insert_conversion(conn, 1, from_qty=1, from_unit='shot',
                           to_qty=1.5, to_unit='fl oz')
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'shot'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        # cost_per_floz = 27 / (25.36 * 1) = 1.0646
        # cost_per_shot = 1.0646 * 1.5 = 1.5969
        # cost = 2 * 1.5969 = 3.1938
        expected_per_floz = 27.0 / 25.36
        expected_per_shot = expected_per_floz * 1.5
        assert abs(result['cost'] - 2 * expected_per_shot) < 0.01

    def test_each_to_oz_weight(self, conn):
        """Conversion: 1 each = 4 oz (weight). Product sold by lb."""
        _insert_product(conn, 1, purchase_unit='lb', pack_unit='lb',
                        pack_size=5, price=25.0)
        _insert_conversion(conn, 1, from_qty=1, from_unit='each',
                           to_qty=4, to_unit='oz')
        ri = {'product_id': 1, 'quantity': 3, 'unit': 'each'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        # cost_per_oz = 25 / (5 * 16) = 0.3125
        # cost_per_each = 0.3125 * 4 = 1.25
        # cost = 3 * 1.25 = 3.75
        assert abs(result['cost'] - 3.75) < 0.01


# ── PATH 3B: purchase-unit conversions ───────────────────────────────────────

class TestPath3BPurchaseUnit:

    def test_case_to_slice(self, conn):
        """Conversion: 1 case = 288 slice. $25/case → ~$0.0868/slice."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='case',
                        pack_size=1, price=25.0)
        _insert_conversion(conn, 1, from_qty=1, from_unit='case',
                           to_qty=288, to_unit='slice')
        ri = {'product_id': 1, 'quantity': 10, 'unit': 'slice'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        expected_per_slice = 25.0 / 288
        assert abs(result['cost'] - 10 * expected_per_slice) < 0.01

    def test_case_to_volume_recipe_cup(self, conn):
        """Conversion: 1 case = 6 gallon. Recipe uses cups.
        $30/case → $30 / (6*128) per fl oz → * 8 per cup."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='case',
                        pack_size=1, price=30.0)
        _insert_conversion(conn, 1, from_qty=1, from_unit='case',
                           to_qty=6, to_unit='gallon')
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'cup'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        cost_per_floz = 30.0 / (6 * 128)
        cost_per_cup = cost_per_floz * 8
        assert abs(result['cost'] - 2 * cost_per_cup) < 0.001

    def test_case_to_weight_recipe_oz(self, conn):
        """Conversion: 1 case = 40 lb. Recipe uses oz.
        $80/case → $80 / (40*16) per oz."""
        _insert_product(conn, 1, purchase_unit='case', pack_unit='case',
                        pack_size=1, price=80.0)
        _insert_conversion(conn, 1, from_qty=1, from_unit='case',
                           to_qty=40, to_unit='lb')
        ri = {'product_id': 1, 'quantity': 8, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        cost_per_oz = 80.0 / (40 * 16)
        assert abs(result['cost'] - 8 * cost_per_oz) < 0.001


# ── PATH 4: no resolution ───────────────────────────────────────────────────

class TestPath4NoResolution:

    def test_incompatible_units(self, conn):
        """Weight product, volume recipe unit, no conversion → no_conversion."""
        _insert_product(conn, 1, purchase_unit='lb', pack_unit='lb',
                        pack_size=1, price=10.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'cup'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_conversion'
        assert result['cost'] == 0.0
        assert result['unit_price'] == 10.0

    def test_unknown_unit(self, conn):
        """Completely unknown recipe unit → no_conversion."""
        _insert_product(conn, 1, purchase_unit='lb', pack_unit='lb',
                        pack_size=1, price=10.0)
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'bunch'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'no_conversion'
        assert result['cost'] == 0.0


# ── Unit normalization edge cases ────────────────────────────────────────────

class TestUnitNormalization:

    def test_fl_oz_underscore(self, conn):
        """fl_oz in recipe unit should normalize to 'fl oz' and match."""
        _insert_product(conn, 1, purchase_unit='fl oz', pack_unit='fl oz',
                        pack_size=1, price=0.50)
        ri = {'product_id': 1, 'quantity': 4, 'unit': 'fl_oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        assert result['cost'] == 2.0

    def test_uppercase_units(self, conn):
        """Units should be case-insensitive."""
        _insert_product(conn, 1, purchase_unit='LB', pack_unit='LB',
                        pack_size=1, price=8.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'LB'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'vendor_item'
        assert result['cost'] == 16.0


# ── Real-world regression: Burger B&B ────────────────────────────────────────

class TestRealWorldRegression:

    def test_burger_bun_from_case(self, conn):
        """Burger buns: 1 case = 48 each at $15.50.
        Recipe needs 1 each → ~$0.3229."""
        _insert_product(conn, 1, name='Burger Buns', purchase_unit='case',
                        pack_unit='case', pack_size=1, price=15.50)
        _insert_conversion(conn, 1, from_qty=1, from_unit='case',
                           to_qty=48, to_unit='each')
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'each'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        assert abs(result['cost'] - 15.50 / 48) < 0.001

    def test_cheese_lb_to_oz(self, conn):
        """American cheese: 5lb at $18. Recipe needs 2 oz → $0.45."""
        _insert_product(conn, 1, name='American Cheese', purchase_unit='lb',
                        pack_unit='lb', pack_size=5, price=18.0)
        ri = {'product_id': 1, 'quantity': 2, 'unit': 'oz'}
        result = cost_ingredient(ri, conn)
        assert result['source'] == 'standard_conversion'
        expected = 18.0 / (5 * 16) * 2
        assert abs(result['cost'] - expected) < 0.001

    def test_liquor_bottle_shot(self, conn):
        """Vodka: 1L bottle ($27) with 25.36 fl oz via pack_contains.
        Conversion: 1 shot = 1.5 fl oz. Recipe: 1 shot."""
        _insert_product(conn, 1, name='Vodka', purchase_unit='bottle',
                        pack_unit='fl oz', pack_size=1, price=27.0,
                        vi_pack_contains=25.36, vi_contains_unit='oz')
        _insert_conversion(conn, 1, from_qty=1, from_unit='shot',
                           to_qty=1.5, to_unit='fl oz')
        ri = {'product_id': 1, 'quantity': 1, 'unit': 'shot'}
        result = cost_ingredient(ri, conn)
        # oz rewritten to fl oz (product has fl oz pack_unit)
        # pack_size=25.36, pack_unit='fl oz'
        # PATH 3A: 1 shot = 1.5 fl oz, cost_per_floz = 27/25.36
        cost_per_floz = 27.0 / 25.36
        expected = cost_per_floz * 1.5
        assert abs(result['cost'] - expected) < 0.01
