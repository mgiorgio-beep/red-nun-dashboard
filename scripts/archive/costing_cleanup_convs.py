"""Curated unit conversions + gallon-pack fixes for the 2026-07 costing cleanup.
Idempotent: deletes prior source='cleanup_2026_07' rows before re-inserting."""
from integrations.toast.data_store import get_connection

SRC = 'cleanup_2026_07'

# Gallon jugs whose vendor_items had NULL pack_contains -> set gallons so the
# volume path resolves fl_oz recipes. (vendor_item_id, gallons)
GAL_FIX = {972: 1, 1234: 4, 1254: 2, 1446: 2, 208: 4}

# product_id -> list of (to_qty, to_unit) conversions, all as "1 cs = to_qty to_unit".
# COUNT (each/slice) and volumetric/weight liquids measured per case at purchase_price.
CONV_CASE = {
    # --- sliced/count components ---
    1528: [(160, 'slice')],          # tomato sliced, 10lb ~1oz/slice
    1526: [(320, 'slice')],          # red onion slab, 10lb ~0.5oz/slice
    476:  [(120, 'slice')],          # american white 120ct
    521:  [(120, 'slice')],          # american white 120ct (glnvw)
    975:  [(160, 'slice')],          # cheddar sliced, 10lb ~1oz
    563:  [(192, 'slice')],          # swiss .75oz, 9lb
    535:  [(144, 'each')],           # pepper jack 1oz slice, 9lb
    570:  [(152, 'slice')],          # white bread, 8 loaves x19 slices
    1611: [(24, 'each')],            # hmbgr bun 6/4
    1222: [(48, 'each')],            # brioche bun 4/12
    540:  [(60, 'each')],            # hoagie roll 10/6
    572:  [(48, 'each')],            # english muffin 6/8
    515:  [(48, 'each')],            # english muffin thomas 6/8
    107:  [(200, 'each')],           # potato skin 200ct
    1223: [(80, 'each')],            # franks 8-1, 10lb
    293:  [(12, 'each')],            # soft pretzel 12/10oz
    742:  [(144, 'each')],           # bao bun 6/24
    1607: [(120, 'each')],           # potsticker 120/1oz
    905:  [(20, 'each')],            # choc bomb cake 20/4.6oz
    732:  [(24, 'each')],            # butter toffee cake 24/4.76oz
    750:  [(28, 'each')],            # raspberry donut cheesecake 2 x 14 slices
    568:  [(80, 'each')],            # pizza crust 80/4oz
    794:  [(180, 'oz')],             # stuffed clam 36 x 5oz
    546:  [(150, 'each')],           # oyster cracker 150 packets
    241:  [(400, 'each')],           # maraschino cherry (approx count)
    500:  [(50, 'each')],            # fry tray 1/50
    1190: [(1000, 'each')],          # ketchup foil packet 1000/9gr
    # --- liquids measured by weight-oz in recipe (density ~1) ---
    557:  [(512, 'oz')],             # mayo 4 gal
    197:  [(512, 'oz')],             # coleslaw dressing 4 gal
    95:   [(512, 'oz')],             # bbq sauce 4 gal
    850:  [(128, 'oz')],             # coleslaw dressing 1 gal
    475:  [(256, 'oz')],             # pickle chips 2 gal
    192:  [(256, 'oz')],             # pickle chips 2 gal (metrodeli)
    90:   [(512, 'oz')],             # banana pepper 4 gal
    239:  [(384, 'oz')],             # butter-alt oil 3 gal
    208:  [(512, 'oz')],             # olive/canola blend 4 gal
    1234: [(512, 'oz')],             # tartar sauce 4 gal
    212:  [(654, 'oz')],             # black beans 6/#10 (109oz/can)
    468:  [(654, 'oz')],             # refried beans 6/#10
    756:  [(654, 'oz')],             # jalapeno 6/#10
    # --- liquids measured by fl_oz in recipe ---
    524:  [(320, 'fl_oz')],          # beer cheese sauce 20lb
    491:  [(320, 'fl_oz')],          # beer cheese sauce 20lb
    564:  [(320, 'fl_oz')],          # sour cream 20lb
    748:  [(256, 'fl_oz')],          # tomato bisque 16lb
    970:  [(136, 'fl_oz')],          # salsa 136oz
    1601: [(56, 'fl_oz')],           # honey 5lb (density 1.42)
    1144: [(120, 'fl_oz')],          # sriracha 6/20oz
    1487: [(81, 'fl_oz')],           # balsamic glaze 6/13.5oz
    1191: [(66, 'fl_oz'), (66, 'oz')],   # sesame oil 61oz weight ~66floz
    1274: [(110, 'fl_oz')],          # pan oil 6/17oz
    519:  [(120, 'fl_oz')],          # dijon 12/10oz
    471:  [(120, 'fl_oz')],          # dijon 12/10oz
    508:  [(172, 'fl_oz')],          # grapefruit juice 24/7.2oz
    # --- lettuce by weight-oz (head ~24oz) ---
    198:  [(576, 'oz')],             # iceberg 24 heads
    1450: [(576, 'oz')],             # romaine 24 heads
    105:  [(16, 'oz')],              # pepper jack 1oz, 16 per pack
}

# Spice / small-measure products: recipe uses pinch/tsp/tbsp against a weight
# pack. Add (from_unit, oz_equivalent) so branch A resolves. Amounts are the
# cooking-standard dry weight; cost impact is fractions of a cent.
SPICE_OZ = {
    1142: [('pinch', 0.013), ('tsp', 0.21)],        # kosher salt
    1104: [('pinch', 0.006), ('tsp', 0.08)],        # black pepper grinder
    556:  [('pinch', 0.004), ('tsp', 0.05)],        # parsley flakes
    465:  [('tsp', 0.1), ('pinch', 0.01)],          # montreal steak rub
    185:  [('tsp', 0.1)],                            # montreal rub (mccormick)
    1060: [('tsp', 0.09), ('pinch', 0.006), ('tbsp', 0.27)],  # cajun
    873:  [('tsp', 0.1)],                            # bbq granulated
    1495: [('pinch', 0.006)],                        # lemon pepper
    1441: [('pinch', 0.005)],                        # ground ginger
    1443: [('tsp', 0.07), ('pinch', 0.005)],         # ground cumin
    1439: [('tsp', 0.07), ('pinch', 0.005)],         # ground coriander
    531:  [('pinch', 0.005)],                        # fresh mint
    102:  [('tsp', 0.18)],                            # minced garlic
    745:  [('tsp', 0.14)],                            # extra fine sugar
    894:  [('tbsp', 0.5)],                            # tahini paste
    1447: [('tbsp', 0.28)],                           # corn starch
    480:  [('tbsp', 0.5)],                            # clarified butter
    520:  [('tbsp', 0.5)],                            # salted butter
    1381: [('tbsp', 0.55)],                           # yellow mustard
}


def main():
    conn = get_connection()
    conn.execute("DELETE FROM product_unit_conversions WHERE source=?", (SRC,))

    # Gallon pack fixes
    for vid, gal in GAL_FIX.items():
        conn.execute("UPDATE vendor_items SET pack_contains=?, contains_unit='gal' WHERE id=?", (gal, vid))

    # Neutralize reusable bamboo steamer basket (#237) — equipment, not food.
    conn.execute("UPDATE vendor_items SET contains_unit=NULL WHERE id=(SELECT active_vendor_item_id FROM products WHERE id=237)")

    def used_from_units(pid):
        return {r[0] for r in conn.execute(
            "SELECT from_unit FROM product_unit_conversions WHERE product_id=?", (pid,)).fetchall()}

    n = 0
    skipped = []
    # Case-level conversions: from_unit label is cosmetic for branch B, so pick a
    # collision-free 'cs' label (UNIQUE is on product_id+from_unit).
    for pid, convs in CONV_CASE.items():
        used = used_from_units(pid)
        for to_qty, to_unit in convs:
            label, i = 'cs', 1
            while label in used:
                i += 1
                label = f'cs{i}'
            used.add(label)
            conn.execute("""INSERT INTO product_unit_conversions
                (product_id, from_qty, from_unit, to_qty, to_unit, source)
                VALUES (?,1,?,?,?,?)""", (pid, label, to_qty, to_unit, SRC))
            n += 1
    # Spice conversions must keep their real from_unit so branch A fires. Skip if
    # a conversion for that unit already exists (don't clobber manual entries).
    for pid, convs in SPICE_OZ.items():
        used = used_from_units(pid)
        for from_unit, oz in convs:
            if from_unit in used:
                skipped.append((pid, from_unit))
                continue
            conn.execute("""INSERT INTO product_unit_conversions
                (product_id, from_qty, from_unit, to_qty, to_unit, source)
                VALUES (?,1,?,?,'oz',?)""", (pid, from_unit, oz, SRC))
            n += 1
    conn.commit()
    print(f"Inserted {n} conversions; fixed {len(GAL_FIX)} gallon packs; neutralized basket #237")
    if skipped:
        print("Skipped (existing from_unit):", skipped)


if __name__ == '__main__':
    main()
