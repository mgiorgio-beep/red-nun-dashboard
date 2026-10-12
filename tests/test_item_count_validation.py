"""Item-count check in validate_invoice_extraction (2026-10-12).

A Colonial invoice listing 3 beers was extracted as 6 lines (each container deposit
on its own line) and held as "6 of 3 items extracted — 3 item(s) may be missing",
although the lines summed to the invoice total. Deposit/fuel/freight lines don't
count as products, and extra lines that add up to the total are not a problem.
Pure function — no database.
"""
from integrations.invoices.processor import validate_invoice_extraction


def _line(name, price):
    return {"product_name": name, "total_price": price}


def _check(**data):
    data.setdefault("invoice_number", "1")
    data.setdefault("vendor_name", "Test Vendor")
    return validate_invoice_extraction(data)


def test_deposit_lines_do_not_count_as_items():
    r = _check(total_line_items=3, invoice_total=343.80, line_items=[
        _line("Lite 24-12 NR Loose", 140), _line("Lite 24-12 NR Loose - Container Deposit", 6),
        _line("Coors Light 24-12NR Loose", 140), _line("Coors Light 24-12NR Loose - Container Deposit", 6),
        _line("Pabst 4-6PK 16OZ Can", 49.40), _line("Pabst 4-6PK 16OZ Can - Container Deposit", 2.40),
    ])
    assert r["item_count_match"] is True
    assert not any("may be missing" in i for i in r["issues"])


def test_missing_items_still_held():
    r = _check(total_line_items=10, invoice_total=100, line_items=[_line("a", 50), _line("b", 50)])
    assert r["item_count_match"] is False
    assert r["auto_confirm"] is False
    assert any("8 item(s) may be missing" in i for i in r["issues"])


def test_extra_lines_that_do_not_add_up_are_held():
    r = _check(total_line_items=2, invoice_total=100,
               line_items=[_line("a", 50), _line("b", 50), _line("a", 50), _line("b", 50), _line("c", 1)])
    assert r["item_count_match"] is False
    assert any("check for duplicated lines" in i for i in r["issues"])


def test_extra_lines_that_add_up_pass():
    r = _check(total_line_items=2, invoice_total=100, line_items=[_line("a", 20)] * 5)
    assert r["item_count_match"] is True
