"""
Vendor name handling shared across both QBO email scrapers.
"""
import re

NATIVE_SCRAPER_VENDORS = [
    "us foods", "usfoods",
    "performance food", "pfg",
    "l. knife", "l knife", "knife & son",
    "colonial wholesale", "colonial beverage",
    "southern glazer", "glazer's",
    "martignetti",
    "craft collective",
    "cintas",
]


def is_native_scraper_vendor(vendor_or_subject):
    """Return True if this vendor is handled by a dedicated scraper."""
    if not vendor_or_subject:
        return False
    lower = vendor_or_subject.lower()
    return any(v in lower for v in NATIVE_SCRAPER_VENDORS)


def normalize_vendor(name):
    """Normalize a vendor name for fuzzy comparison."""
    if not name:
        return ""
    n = name.lower().strip()

    suffixes = [
        ", inc.", ", inc", " inc.", " inc",
        ", llc", " llc",
        ", corp", " corp", " corporation",
        ", ltd", " ltd",
        " company", " co.", " co",
        " homegrown",
    ]
    changed = True
    while changed:
        changed = False
        for s in suffixes:
            if n.endswith(s):
                n = n[: -len(s)].strip()
                changed = True

    n = n.replace(" and ", " & ")
    n = re.sub(r"[.,\-]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = n.replace("foodservice", "food service")
    return n


def extract_vendor_from_subject(subject):
    """Extract vendor name from QBO email subject."""
    if not subject:
        return None
    m = re.search(r"\bfrom\s+(.+?)(?:\s*$|\s*-\s|\s*\[|\s*\()", subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None
