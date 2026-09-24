"""FMT Holdings (x1239) transfers — standing rule, Mike 2026-09-24.

Chatham (5975) OUT to 1239 is rent; IN from 1239 is FMT covering a Red Buoy
shortfall (FMT Loan). An outflow of exactly 8,000.00 is Mike repaying the
FMT loan: held for his confirmation, never auto-coded as rent. Any other
1239 pairing (e.g. Dennis 2757) goes to review. Pure function; always runs.
"""
import pytest

from routes.register_routes import classify_transfer


@pytest.mark.parametrize("desc,amt,last4,name,held", [
    ("Transfer from x5975 to x1239", -1200.00, "5975", "Building Rent", False),
    ("Transfer from x1239 to x5975", 4000.00, "5975", "FMT Loan", False),
    ("Transfer from x5975 to x1239", -8000.00, "5975", None, True),
    ("Transfer from x2757 to x1239", -500.00, "2757", None, False),
    ("Transfer from x1239 to x2757", 500.00, "2757", None, False),
])
def test_fmt_transfers(desc, amt, last4, name, held):
    got, reason = classify_transfer(desc, amt, last4)
    assert got == name
    if name is None:
        assert reason, "an unruled 1239 transfer must carry a review reason"
        assert ("held for his confirmation" in reason) is held


def test_intercompany_and_realty_rules_unchanged():
    assert classify_transfer("Transfer from x2757 to x5087", -3000.0, "2757")[0] == "Building Rent"
    assert classify_transfer("Transfer from x2757 to x5975", -2000.0, "2757")[0] is None
