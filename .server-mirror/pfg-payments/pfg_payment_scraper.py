#!/usr/bin/env python3
"""
PFG Payment Scraper (Billfire Valet)
====================================
Pays PFG invoices via billfirevalet.com using a token link from email.
No username/password — auth is via a rotating token URL sent weekly by Billfire.

The scraper can get the token URL from:
1. payment_request.json (token_url field)
2. A stored config file (data/billfire_token.json)
3. Gmail API search (future enhancement)

Flow:
    Open token URL → Dashboard → Pay → Select invoices → Agree to terms → Confirm and Pay

Convention: Print CONFIRMATION_REF=<value> on success (exit 0).
            Exit 1 on failure.

Deployment:
    ~/vendor-scrapers/pfg-payments/pfg_payment_scraper.py
    Called by payment_routes.py via _run_payment_scraper_bg()
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
REQUEST_FILE = SCRIPT_DIR / "payment_request.json"
TOKEN_FILE = SCRIPT_DIR / "data" / "billfire_token.json"
SCREENSHOT_DIR = SCRIPT_DIR / "screenshots"

BILLFIRE_BASE = "https://billfirevalet.com"

# PFG location IDs on Billfire
LOCATIONS = {
    "chatham": "323ce708-90a4-11ed-9b79-032903be12a6",
    "dennis": "323da8c8-90a4-11ed-9b79-9b27c7899acf",
}

# How location names appear in Billfire UI (for matching)
# Order matters — more specific patterns first to avoid matching wrong location
# (e.g. "dennis" would match Knockout Pizza's address "DENNIS PORT, MA")
LOCATION_PATTERNS = {
    "chatham": ["09736", "red nun bar"],
    "dennis": ["09848", "red nun dennis"],
}

# Funding account last-4 expected on the Billfire payment screen per location.
# Chatham draws from Cape Cod Five 5975, Dennis from Cape Cod Five 2757.
# If the Billfire page shows the wrong account, we abort before clicking Pay.
EXPECTED_ACCOUNT_LAST4 = {
    "chatham": "5975",
    "dennis": "2757",
}

# Browser settings — no persistent profile needed (token auth, no cookies)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
SLOW_MO = int(os.getenv("SLOW_MO", "500"))

# Dashboard API
DASHBOARD_API = os.getenv("DASHBOARD_API", "http://127.0.0.1:8080")


# ─── HELPERS ─────────────────────────────────────────────────────────────────


def log(msg):
    print(f"[PFG-PAY] {datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


async def screenshot(page, name):
    """Save a timestamped screenshot for debugging."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"{name}_{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log(f"  Screenshot: {path}")
    except Exception as e:
        log(f"  [WARN] Screenshot failed: {e}")


async def wait_for_page(page, timeout=15000):
    """Wait for page to settle after navigation."""
    await page.wait_for_timeout(3000)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    await page.wait_for_timeout(2000)


async def dump_buttons(page, label=""):
    """Debug: print all visible buttons/links on the page."""
    buttons = await page.evaluate(r"""
        () => {
            const results = [];
            const els = document.querySelectorAll('button, a, [role="button"], input[type="checkbox"], input[type="submit"]');
            for (const el of els) {
                const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                if (!text || el.offsetParent === null) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 5) continue;
                if (text.length > 80) continue;
                const tag = el.tagName.toLowerCase();
                const type = el.getAttribute('type') || '';
                const checked = el.checked ? ' [checked]' : '';
                results.push(`<${tag} type="${type}"${checked}> "${text}" @(${Math.round(rect.x)},${Math.round(rect.y)})`);
            }
            return results;
        }
    """)
    if label:
        log(f"  [{label}] Elements:")
        for b in buttons:
            log(f"    {b}")
    return buttons


GMAIL_TOKEN_PATH = "/opt/red-nun-dashboard/integrations/google/gmail_token.pickle"

_BILLFIRE_URL_RE = re.compile(
    r"https://billfirevalet\.com/token/[A-Fa-f0-9\-]+"
    r"(?:/vendors/[A-Fa-f0-9\-]+/locations/[A-Fa-f0-9\-]+(?:/[a-zA-Z0-9]+)?)?"
)


def _extract_gmail_body(payload: dict) -> str:
    """Walk a Gmail message payload tree and concatenate text body parts."""
    import base64

    def walk(part):
        out = []
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and ("text/" in mime or mime == ""):
            try:
                padded = data + "=" * (-len(data) % 4)
                out.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace"))
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            out.extend(walk(sub))
        return out

    return "\n".join(walk(payload))


def fetch_token_url_from_gmail(location: str = "", max_age_days: int = 14) -> str:
    """
    Query dashboard@rednun.com Gmail for the most recent Billfire token URL.

    If `location` is provided (e.g. "chatham", "dennis") and its UUID is in
    LOCATIONS, prefer a URL whose path contains that UUID. Otherwise return
    the most recent Billfire URL found.

    Returns empty string on any failure — the caller falls back to the stored
    token file, which is better than nothing even if stale.
    """
    try:
        import pickle
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
    except ImportError as e:
        log(f"  [gmail] libraries unavailable: {e}")
        return ""

    if not Path(GMAIL_TOKEN_PATH).exists():
        log(f"  [gmail] token pickle not found at {GMAIL_TOKEN_PATH}")
        return ""

    try:
        with open(GMAIL_TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GMAIL_TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        if not creds.valid:
            log("  [gmail] credentials invalid")
            return ""
        svc = build("gmail", "v1", credentials=creds)
    except Exception as e:
        log(f"  [gmail] auth failed: {e}")
        return ""

    query = f"(billfire OR billfirevalet) newer_than:{max_age_days}d"
    try:
        res = svc.users().messages().list(userId="me", q=query, maxResults=20).execute()
    except Exception as e:
        log(f"  [gmail] list failed: {e}")
        return ""

    msgs = res.get("messages", [])
    if not msgs:
        log(f"  [gmail] no messages matched '{query}'")
        return ""

    target_loc_id = LOCATIONS.get(location) if location else None
    matched_for_location = ""
    most_recent = ""

    for m in msgs:
        try:
            full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        except Exception:
            continue
        body_text = _extract_gmail_body(full.get("payload", {}))
        found = _BILLFIRE_URL_RE.findall(body_text)
        if not found:
            continue
        for url in found:
            if not most_recent:
                most_recent = url
            if target_loc_id and target_loc_id in url:
                matched_for_location = url
                break
        if matched_for_location:
            break

    if matched_for_location:
        log(f"  [gmail] found token URL for {location}")
        return matched_for_location
    if most_recent:
        log(f"  [gmail] found token URL (location not matched — using most recent)")
        return most_recent

    log("  [gmail] no Billfire URL found in recent messages")
    return ""


def get_token_url(request_data: dict, location: str = "") -> str:
    """
    Get the Billfire token URL from available sources.
    Priority: payment_request.json > Gmail (dashboard@rednun.com) > stored token file

    Gmail beats the stored token because Billfire tokens are short-lived —
    the most recent email always has the freshest URL.
    """
    token_url = request_data.get("token_url", "")
    if token_url and "billfirevalet.com/token/" in token_url:
        log(f"  Token URL from payment request")
        return token_url

    gmail_url = fetch_token_url_from_gmail(location=location)
    if gmail_url:
        log(f"  Token URL from Gmail (dashboard@rednun.com inbox)")
        return gmail_url

    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            token_url = data.get("token_url", "")
            if token_url and "billfirevalet.com/token/" in token_url:
                log(f"  Token URL from stored config (saved {data.get('updated', '?')}) — may be stale")
                return token_url
        except Exception:
            pass

    return ""


def save_token_url(token_url: str):
    """Persist the token URL for future use."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token_url": token_url,
            "updated": datetime.now().isoformat(),
        }, f, indent=2)
    log(f"  Saved token URL to {TOKEN_FILE}")


# ─── BILLFIRE NAVIGATION ────────────────────────────────────────────────────


def parse_token_url(token_url: str) -> dict:
    """
    Parse a Billfire token URL into components.
    URL format: https://billfirevalet.com/token/{TOKEN}/vendors/{VENDOR}/locations/{LOCATION}/statement
    """
    m = re.match(
        r'https://billfirevalet\.com/token/([^/]+)/vendors/([^/]+)/locations/([^/]+)',
        token_url,
    )
    if not m:
        return {}
    return {
        "token": m.group(1),
        "vendor_id": m.group(2),
        "location_id": m.group(3),
    }


def build_url(token_url: str, path: str, location_id: str = None) -> str:
    """
    Build a Billfire URL from a token URL and a path suffix.
    path can be: "" (dashboard), "click2pay", "statement"
    Optionally override the location_id.
    """
    parts = parse_token_url(token_url)
    if not parts:
        return token_url
    loc = location_id or parts["location_id"]
    base = f"{BILLFIRE_BASE}/token/{parts['token']}/vendors/{parts['vendor_id']}/locations/{loc}"
    if path:
        return f"{base}/{path}"
    return base


async def navigate_to_dashboard(page, token_url: str) -> bool:
    """Navigate directly to the dashboard URL (no /statement suffix)."""
    dashboard_url = build_url(token_url, "")
    log(f"  Opening dashboard: {dashboard_url[:80]}...")
    await page.goto(dashboard_url, wait_until="networkidle", timeout=60000)
    # React SPA — wait for JS render
    await page.wait_for_timeout(5000)
    await screenshot(page, "01_dashboard")

    page_text = await page.evaluate("() => document.body.innerText.substring(0, 1000)")
    log(f"  Page text: {page_text[:300]}")

    if "dashboard" in page_text.lower() or "pay" in page_text.lower():
        log(f"  Dashboard loaded OK. URL: {page.url}")
        return True

    log("  [ERROR] Dashboard page did not load expected content")
    await screenshot(page, "dashboard_failed")
    return False


async def click_change_location(page) -> bool:
    """Click the 'Change Location' div on the dashboard."""
    log("  Clicking 'Change Location'...")
    clicked = await page.evaluate(r"""
        () => {
            const els = document.querySelectorAll('div, span');
            for (const el of els) {
                const text = (el.innerText || '').trim().toLowerCase();
                if (text === 'change location' && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if clicked:
        await page.wait_for_timeout(3000)
        await screenshot(page, "change_location_popup")
    return clicked


async def click_location_entry(page, location: str) -> bool:
    """
    On the 'Choose a Location' popup, click a location entry.
    Location entries are DIV.sc-ctKHVw elements with text like
    'RED NUN DENNIS PORT - 09848'.
    """
    patterns = LOCATION_PATTERNS.get(location, [location])
    log(f"  Looking for location matching: {patterns}")

    clicked = await page.evaluate(r"""
        (patterns) => {
            // Location name entries use class sc-ctKHVw (or similar styled-component)
            // They're leaf DIVs with text like "RED NUN DENNIS PORT - 09848"
            const els = document.querySelectorAll('div');
            for (const el of els) {
                const text = (el.innerText || '').trim().toLowerCase();
                if (!text || text.length > 80 || el.children.length > 0) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                // Check if this matches any of our patterns
                for (const pat of patterns) {
                    if (text.includes(pat.toLowerCase())) {
                        el.click();
                        return { clicked: true, text: el.innerText.trim() };
                    }
                }
            }
            return { clicked: false };
        }
    """, patterns)

    if clicked and clicked.get("clicked"):
        log(f"  Clicked: \"{clicked.get('text')}\"")
        await wait_for_page(page)
        return True

    log(f"  [WARN] No location entry found matching {patterns}")
    return False


async def switch_location(page, token_url: str, location: str) -> str:
    """
    Switch to a different location. If we know the location ID, navigate by URL.
    Otherwise discover it via the Change Location UI.
    Returns the location ID if successful, empty string on failure.
    """
    log(f"  Switching to {location}...")

    loc_id = LOCATIONS.get(location)

    if loc_id:
        # Navigate directly by URL
        dashboard_url = build_url(token_url, "", location_id=loc_id)
        log(f"  Navigating to {location} dashboard by URL...")
        await page.goto(dashboard_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        await screenshot(page, f"switched_to_{location}")
        log(f"  URL: {page.url}")
        return loc_id

    # Don't have location ID — use the Change Location popup to discover it
    clicked = await click_change_location(page)
    if not clicked:
        log(f"  [WARN] Could not open Change Location popup")
        return ""

    # Click the target location entry
    if not await click_location_entry(page, location):
        log(f"  [WARN] Could not find {location} in location list")
        return ""

    # Extract location ID from the new URL
    m = re.search(r'/locations/([a-f0-9-]+)', page.url)
    if m:
        loc_id = m.group(1)
        LOCATIONS[location] = loc_id
        log(f"  Discovered {location} location ID: {loc_id}")
        await screenshot(page, f"switched_to_{location}")
        return loc_id

    log(f"  [WARN] Could not extract location ID from URL: {page.url}")
    return ""


async def navigate_to_pay(page, token_url: str, location_id: str = None, location: str = None) -> bool:
    """Navigate to the click2pay page. Handles both full and base token URLs."""
    parts = parse_token_url(token_url)

    if parts:
        # Full token URL with vendor/location — build click2pay URL directly
        pay_url = build_url(token_url, "click2pay", location_id=location_id)
        log(f"  Opening pay page: {pay_url[:80]}...")
        await page.goto(pay_url, wait_until="networkidle", timeout=60000)
    else:
        # Base token URL — navigate to it, handle Choose a Location page
        log(f"  Opening base token URL (will handle location selection)...")
        await page.goto(token_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        page_text = await page.evaluate("() => document.body.innerText.substring(0, 1000)")

        if "choose a location" in page_text.lower():
            log("  Detected 'Choose a Location' page — selecting location...")
            target_location = location or "chatham"
            if not await click_location_entry(page, target_location):
                log(f"  [WARN] Could not select {target_location} from location list")
                await screenshot(page, "location_select_failed")
                return False
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Now we should be on the dashboard — extract the full URL with location
            new_url = page.url
            log(f"  After location select, URL: {new_url}")
            m_loc = re.search(r"/locations/([a-f0-9-]+)", new_url)
            if m_loc:
                location_id = m_loc.group(1)
                log(f"  Extracted location ID: {location_id}")
                # Now build the click2pay URL from what we have
                m_full = re.match(r"(https://billfirevalet\.com/token/[^/]+/vendors/[^/]+/locations/[^/]+)", new_url)
                if m_full:
                    pay_url = m_full.group(1) + "/click2pay"
                    log(f"  Navigating to click2pay: {pay_url[:80]}...")
                    await page.goto(pay_url, wait_until="networkidle", timeout=60000)
                else:
                    log("  [WARN] Could not build click2pay URL from redirected URL")
            else:
                # Try clicking Pay or Click2Pay from the dashboard
                log("  No location ID in URL — looking for Pay link on page...")

    await page.wait_for_timeout(5000)
    await screenshot(page, "03_pay_page")

    page_text = await page.evaluate("() => document.body.innerText.substring(0, 1000)")
    log(f"  Pay page text: {page_text[:300]}")

    # Verify we're on the payment page — should have invoices or "Confirm and Pay"
    if "confirm" in page_text.lower() or "invoice" in page_text.lower() or "amount due" in page_text.lower():
        log(f"  Pay page loaded OK. URL: {page.url}")
        return True

    # Check if "pay" is in text but not "choose a location" (avoid false positive on the location page)
    if "pay" in page_text.lower() and "choose a location" not in page_text.lower():
        log(f"  Pay page loaded OK. URL: {page.url}")
        return True

    log("  [ERROR] Pay page did not load expected content")
    await screenshot(page, "pay_failed")
    return False


async def select_invoices(page, invoice_numbers: list) -> dict:
    """
    Select invoices by clicking their checkboxes on the payment page.
    Returns dict with selected/not_found lists.
    """
    results = {"selected": [], "not_found": [], "amounts": {}}

    for inv_num in invoice_numbers:
        # Find the row containing this invoice number and click its checkbox
        # Billfire structure: INPUT.sc-kiwPtn (checkbox) near BUTTON.sc-iKMXQg (invoice number)
        selected = await page.evaluate(r"""
            (invoiceNum) => {
                // Strategy 1: Find BUTTON with exact invoice number text, then find nearby checkbox
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim();
                    if (text !== invoiceNum) continue;
                    // Walk up to find the row container with a checkbox
                    let row = btn;
                    for (let i = 0; i < 10; i++) {
                        row = row.parentElement;
                        if (!row) break;
                        const checkbox = row.querySelector('input[type="checkbox"]');
                        if (checkbox) {
                            const rowText = row.innerText || '';
                            const amounts = rowText.match(/\$[\d,]+\.\d{2}/g) || [];
                            if (!checkbox.checked) {
                                checkbox.click();
                            }
                            return {
                                found: true,
                                method: 'button-then-checkbox',
                                amount: amounts[0] || '',
                                checked: true,
                            };
                        }
                    }
                }

                // Strategy 2: Find any element with exact invoice number, walk up to checkbox
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const text = (el.innerText || '').trim();
                    if (text !== invoiceNum && text !== '#' + invoiceNum) continue;
                    if (text.length > 20) continue;
                    let row = el;
                    for (let i = 0; i < 10; i++) {
                        row = row.parentElement;
                        if (!row) break;
                        const checkbox = row.querySelector('input[type="checkbox"]');
                        if (checkbox && (row.innerText || '').includes(invoiceNum)) {
                            const amounts = (row.innerText || '').match(/\$[\d,]+\.\d{2}/g) || [];
                            if (!checkbox.checked) {
                                checkbox.click();
                            }
                            return {
                                found: true,
                                method: 'text-then-checkbox',
                                amount: amounts[0] || '',
                                checked: true,
                            };
                        }
                    }
                }

                return { found: false };
            }
        """, str(inv_num))

        if selected and selected.get("found"):
            results["selected"].append(inv_num)
            results["amounts"][inv_num] = selected.get("amount", "")
            log(f"  [OK] Selected #{inv_num} ({selected.get('amount', '?')}) via {selected.get('method')}")
        else:
            results["not_found"].append(inv_num)
            log(f"  [WARN] Invoice #{inv_num} not found")

        await page.wait_for_timeout(500)

    return results


async def select_first_invoice(page) -> dict:
    """Select the first available invoice checkbox for dry-run testing."""
    result = await page.evaluate(r"""
        () => {
            // Strategy: Find buttons with numeric-only text (invoice numbers),
            // then walk up to find the checkbox — same approach as select_invoices.
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = (btn.innerText || '').trim();
                if (!/^\d{4,}$/.test(text)) continue;  // Must be 4+ digit number
                // Walk up to find a container with a checkbox
                let row = btn;
                for (let i = 0; i < 10; i++) {
                    row = row.parentElement;
                    if (!row) break;
                    const checkbox = row.querySelector('input[type="checkbox"]');
                    if (checkbox) {
                        if (!checkbox.checked) {
                            checkbox.click();
                        }
                        return [text];
                    }
                }
            }
            return [];
        }
    """)

    if result and len(result) > 0:
        return {"selected": result, "not_found": [], "amounts": {}}
    return {"selected": [], "not_found": ["first"], "amounts": {}}


async def select_bank_account(page, location: str) -> dict:
    """
    On the Billfire payment screen, open the 'Payment Method' dropdown and
    pick the option matching EXPECTED_ACCOUNT_LAST4 for this location.

    No-op (returns ok=True, changed=False) if the correct account is already
    selected. Returns ok=False if the dropdown couldn't be opened or the
    expected option wasn't found — the caller should still run
    verify_bank_account afterwards as the final guard.
    """
    expected = EXPECTED_ACCOUNT_LAST4.get((location or "").lower())
    if not expected:
        return {"ok": False, "changed": False,
                "message": f"No expected bank account configured for location={location!r}"}

    result = await page.evaluate(r"""
        async (expected) => {
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));

            // 1. Find the 'Payment Method' row, then the dropdown toggle inside it.
            //    The toggle displays "Checking ####" (and a chevron). We look for
            //    an element whose text matches "Checking \d{3,}" within the row.
            function findPaymentMethodRow() {
                const all = document.querySelectorAll('div, section, li');
                for (const el of all) {
                    const t = (el.innerText || '').trim();
                    if (!t) continue;
                    if (!/payment\s*method/i.test(t)) continue;
                    // The row itself should also contain "Checking ####"
                    if (/checking\s*\d{3,}/i.test(t) && t.length < 200) {
                        return el;
                    }
                }
                return null;
            }

            const row = findPaymentMethodRow();
            if (!row) return { ok: false, stage: 'find-row', message: 'Payment Method row not found' };

            // Current selection (before any change)
            const currentMatch = (row.innerText || '').match(/checking\s*(\d{3,})/i);
            const currentLast4 = currentMatch ? currentMatch[1] : '';

            if (currentLast4 === expected) {
                return { ok: true, changed: false, currentLast4, message: `Already on ****${expected}` };
            }

            // Find the clickable toggle inside the row. Prefer a leaf element
            // whose text matches "Checking \d{3,}".
            let toggle = null;
            const candidates = row.querySelectorAll('*');
            for (const el of candidates) {
                const t = (el.innerText || '').trim();
                if (!t || t.length > 60) continue;
                if (/^checking\s*\d{3,}$/i.test(t) || /checking\s*\d{3,}/i.test(t) && el.children.length <= 3) {
                    const r = el.getBoundingClientRect();
                    if (r.width >= 20 && r.height >= 10 && el.offsetParent !== null) {
                        toggle = el;
                    }
                }
            }
            // Fallback: click the row itself
            toggle = toggle || row;

            toggle.click();
            await sleep(800);

            // 2. Dropdown should now be open. Look for an option containing
            //    the expected last-4. Options are typically LI/DIV leaves.
            function findOption(last4) {
                const els = document.querySelectorAll('li, div, button, [role="option"], [role="menuitem"]');
                for (const el of els) {
                    const t = (el.innerText || '').trim();
                    if (!t || t.length > 80) continue;
                    if (!/checking/i.test(t)) continue;
                    if (!new RegExp(`\\b${last4}\\b`).test(t)) continue;
                    // Must be a leaf-ish, visible element (not the whole row)
                    if (el.children.length > 4) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10 || el.offsetParent === null) continue;
                    return el;
                }
                return null;
            }

            // Give the dropdown another moment to render, then try
            let opt = findOption(expected);
            if (!opt) {
                await sleep(600);
                opt = findOption(expected);
            }
            if (!opt) {
                return { ok: false, stage: 'find-option', currentLast4,
                         message: `Option ****${expected} not found in dropdown` };
            }

            opt.click();
            await sleep(800);

            // 3. Confirm the row now shows the new selection
            const afterRow = findPaymentMethodRow();
            const afterMatch = afterRow ? (afterRow.innerText || '').match(/checking\s*(\d{3,})/i) : null;
            const afterLast4 = afterMatch ? afterMatch[1] : '';
            if (afterLast4 === expected) {
                return { ok: true, changed: true, previousLast4: currentLast4, currentLast4: afterLast4,
                         message: `Switched ****${currentLast4} → ****${expected}` };
            }
            return { ok: false, stage: 'post-click', currentLast4: afterLast4,
                     message: `Clicked ****${expected} but row still shows ****${afterLast4 || '?'}` };
        }
    """, expected)

    msg = result.get("message", "")
    if result.get("ok"):
        return {"ok": True, "changed": bool(result.get("changed")), "message": msg}
    return {"ok": False, "changed": False, "message": msg}


async def verify_bank_account(page, location: str) -> dict:
    """
    Before clicking Confirm and Pay, verify the funding account currently
    selected in Billfire's Payment Method row matches EXPECTED_ACCOUNT_LAST4
    for this location.

    We scope this to the Payment Method row (not the whole page) because
    Billfire renders the dropdown options — including both accounts — in the
    DOM even when the dropdown is closed, so whole-page matching is ambiguous.

    Returns {"ok": bool, "message": str}. Caller must abort when ok=False.
    """
    expected = EXPECTED_ACCOUNT_LAST4.get((location or "").lower())
    if not expected:
        return {"ok": False, "message": f"No expected bank account configured for location={location!r}"}

    result = await page.evaluate(r"""
        () => {
            // Find the Payment Method row — same heuristic as select_bank_account.
            const all = document.querySelectorAll('div, section, li');
            let row = null;
            for (const el of all) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                if (!/payment\s*method/i.test(t)) continue;
                if (/checking\s*\d{3,}/i.test(t) && t.length < 200) {
                    row = el;
                    break;
                }
            }
            if (!row) return { found: false };

            // The first "Checking \d+" in the row text is the currently
            // selected value; subsequent matches are dropdown options.
            const txt = row.innerText || '';
            const m = txt.match(/checking\s*(\d{3,})/i);
            return {
                found: true,
                selectedLast4: m ? m[1] : '',
                rowText: txt.substring(0, 200),
            };
        }
    """)

    if not result.get("found"):
        return {"ok": False,
                "message": f"Could not find Payment Method row on Billfire for {location}"}

    selected = result.get("selectedLast4", "")
    if not selected:
        return {"ok": False,
                "message": f"Payment Method row did not contain a recognizable account number for {location}"}

    if selected != expected:
        return {"ok": False,
                "message": f"Wrong bank account on Billfire: selected ****{selected} "
                           f"but expected ****{expected} for {location}"}

    return {"ok": True, "message": f"Bank account verified: ****{expected} for {location}"}


async def agree_and_confirm(page, dry_run=False, location: str = "") -> dict:
    """
    Check 'Agree to terms and conditions' checkbox, then click 'Confirm and Pay'.
    Returns dict with status and confirmation info.
    """
    # Step 0: Ensure the funding bank account matches this location.
    # First try to pick the correct one via the Payment Method dropdown,
    # then verify it by reading the page text. Verify is the final guard
    # — if it fails, we abort before agreeing to terms.
    if location:
        await screenshot(page, "03b_pre_bank_check")
        log(f"  Selecting bank account for {location}...")
        sel = await select_bank_account(page, location)
        log(f"  {sel['message']}")
        if sel.get("changed"):
            await screenshot(page, "03c_bank_switched")

        log(f"  Verifying bank account for {location}...")
        verify = await verify_bank_account(page, location)
        log(f"  {verify['message']}")
        if not verify["ok"]:
            await screenshot(page, "bank_account_mismatch")
            return {"status": "error", "message": verify["message"]}

    # Step 1: Check the terms and conditions checkbox
    log("  Agreeing to terms and conditions...")
    agreed = await page.evaluate(r"""
        () => {
            // Find a checkbox near "terms" or "agree" text
            const checkboxes = document.querySelectorAll('input[type="checkbox"]');
            for (const cb of checkboxes) {
                let parent = cb;
                for (let i = 0; i < 5; i++) {
                    parent = parent.parentElement;
                    if (!parent) break;
                    const text = (parent.innerText || '').toLowerCase();
                    if (text.includes('terms') || text.includes('agree') || text.includes('condition')) {
                        if (!cb.checked) {
                            cb.click();
                        }
                        return { found: true, text: text.substring(0, 100) };
                    }
                }
            }

            // Also try label-based approach
            const labels = document.querySelectorAll('label');
            for (const label of labels) {
                const text = (label.innerText || '').toLowerCase();
                if (text.includes('terms') || text.includes('agree')) {
                    const cb = label.querySelector('input[type="checkbox"]') ||
                               document.getElementById(label.getAttribute('for'));
                    if (cb && !cb.checked) {
                        cb.click();
                        return { found: true, text: text.substring(0, 100) };
                    }
                    // Click the label itself
                    label.click();
                    return { found: true, text: text.substring(0, 100), method: 'label-click' };
                }
            }

            return { found: false };
        }
    """)

    if not agreed or not agreed.get("found"):
        log("  [WARN] Could not find terms checkbox")
        await screenshot(page, "no_terms_checkbox")
        await dump_buttons(page, "no-terms")
    else:
        log(f"  Agreed: \"{agreed.get('text', '')[:80]}\"")

    await page.wait_for_timeout(1000)
    await screenshot(page, "04_terms_agreed")

    # Step 2: Click "Confirm and Pay" button
    log("  Clicking 'Confirm and Pay'...")
    confirm_btn = page.locator('button:has-text("Confirm"), button:has-text("confirm"), '
                                'a:has-text("Confirm"), button:has-text("Submit"), '
                                'button:has-text("Pay Now")')

    if await confirm_btn.count() > 0:
        # Check if button is enabled
        first_btn = confirm_btn.first
        is_disabled = await first_btn.evaluate("el => el.disabled")
        if is_disabled:
            log("  [WARN] 'Confirm and Pay' button is disabled — terms may not be checked")
            await screenshot(page, "confirm_disabled")
            await dump_buttons(page, "confirm-disabled")
            return {"status": "error", "message": "Confirm button is disabled"}

        btn_text = await first_btn.evaluate("el => (el.innerText || '').trim()")
        log(f"  'Confirm and Pay' button is ENABLED: \"{btn_text}\"")

        if dry_run:
            log("  [DRY RUN] Stopping before clicking Confirm and Pay")
            await screenshot(page, "dry_run_confirm_ready")
            return {"status": "dry_run", "message": "Confirm button is enabled and ready"}

        await first_btn.click()
        log("  Clicked Confirm and Pay")
    else:
        log("  [WARN] No 'Confirm and Pay' button found")
        await screenshot(page, "no_confirm_button")
        await dump_buttons(page, "no-confirm")
        return {"status": "error", "message": "Could not find Confirm and Pay button"}

    # Wait for payment to process
    await page.wait_for_timeout(8000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await screenshot(page, "05_after_confirm")

    # Extract confirmation from result page
    page_text = await page.evaluate("() => document.body.innerText.substring(0, 2000)")
    log(f"  Result page text: {page_text[:300]}")

    # Look for confirmation number
    conf_match = re.search(
        r'(?:confirmation|reference|transaction|payment)\s*(?:#|number|ref|id)?\s*[:=]?\s*([A-Z0-9-]{6,})',
        page_text, re.IGNORECASE
    )
    if conf_match:
        return {"status": "ok", "confirmation_ref": conf_match.group(1), "page_text": page_text}

    # Check for success indicators
    success_patterns = [
        r'payment.*(?:success|complet|submitt|process|confirm|accept)',
        r'(?:success|complet|submitt|process|confirm|accept).*payment',
        r'thank you',
        r'payment has been',
    ]
    for pat in success_patterns:
        if re.search(pat, page_text, re.IGNORECASE):
            ts_ref = f"PFG-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            return {"status": "ok", "confirmation_ref": ts_ref, "page_text": page_text}

    return {
        "status": "unknown",
        "message": "Could not determine payment result",
        "page_text": page_text,
    }


# ─── MAIN ────────────────────────────────────────────────────────────────────


async def test_location(page, token_url: str, location: str, invoice_numbers: list) -> bool:
    """
    Dry-run test for a single location: navigate → select invoices → get to Confirm button.
    Returns True if Confirm button was reached and enabled.
    """
    log(f"\n{'─' * 60}")
    log(f"TESTING: {location.upper()}")
    log(f"{'─' * 60}")

    loc_id = LOCATIONS.get(location)

    # If we don't have the location ID, discover it from dashboard
    if not loc_id:
        nav_ok = await navigate_to_dashboard(page, token_url)
        if not nav_ok:
            log(f"  [FAIL] Could not navigate to dashboard for {location}")
            return False

        loc_id = await switch_location(page, token_url, location)
        if not loc_id:
            log(f"  [FAIL] Could not switch to {location}")
            return False

    # Navigate directly to click2pay for this location
    pay_ok = await navigate_to_pay(page, token_url, location_id=loc_id, location=location)
    if not pay_ok:
        log(f"  [FAIL] Could not load pay page for {location}")
        return False

    # Dump available invoices
    await dump_buttons(page, f"{location}-pay-page")

    # Select invoices (or select all if no specific ones given)
    if invoice_numbers:
        log(f"  Selecting {len(invoice_numbers)} invoices...")
        selection = await select_invoices(page, invoice_numbers)
    else:
        # Select the first available invoice for dry-run testing
        log("  No specific invoices — selecting first available...")
        selection = await select_first_invoice(page)

    if not selection["selected"]:
        # Check if location has no open invoices (legitimate — not a failure)
        page_text = await page.evaluate("() => document.body.innerText.substring(0, 1000)")
        if "no" in page_text.lower() and "invoice" in page_text.lower():
            log(f"  [PASS] {location.upper()}: Page loaded OK but no open invoices (nothing to pay)")
            return True
        log(f"  [FAIL] Could not select any invoices for {location}")
        return False

    log(f"  Selected: {selection['selected']}")

    # Try to agree and reach Confirm button (dry run — won't click)
    log("  Testing agree + confirm flow...")
    result = await agree_and_confirm(page, dry_run=True, location=location)

    if result["status"] == "dry_run":
        log(f"  [PASS] {location.upper()}: Confirm button is enabled and ready!")
        return True
    else:
        log(f"  [FAIL] {location.upper()}: {result.get('message', 'unknown error')}")
        return False


async def main():
    dry_run = "--dry-run" in sys.argv or "--test" in sys.argv

    log(f"{'=' * 60}")
    log(f"PFG Payment Scraper (Billfire) — {datetime.now().isoformat()}")
    if dry_run:
        log(f"MODE: DRY RUN (will NOT submit payment)")
    log(f"{'=' * 60}")

    # 1. Read payment request
    if not REQUEST_FILE.exists():
        log(f"ERROR: No payment_request.json at {REQUEST_FILE}")
        sys.exit(1)

    with open(REQUEST_FILE) as f:
        request_data = json.load(f)

    vendor = request_data.get("vendor_name", "Unknown")
    total = request_data.get("total", 0)
    invoices = request_data.get("invoices", [])
    vp_id = request_data.get("vendor_payment_id")
    location = request_data.get("location", "chatham")

    log(f"Vendor: {vendor}")
    log(f"Total: ${total:.2f}")
    log(f"Location: {location}")
    log(f"Vendor Payment ID: {vp_id}")
    log(f"Invoices: {len(invoices)}")
    for inv in invoices:
        log(f"  - #{inv.get('invoice_number', '?')}  ${inv.get('amount', 0):.2f}  due {inv.get('due_date', '?')}")

    invoice_numbers = [str(inv.get("invoice_number", "")) for inv in invoices if inv.get("invoice_number")]
    if not invoice_numbers:
        log("ERROR: No invoice numbers in payment request")
        sys.exit(1)

    # 2. Get token URL
    token_url = get_token_url(request_data, location=location)
    if not token_url:
        log("ERROR: No Billfire token URL available")
        log("  Provide token_url in payment_request.json or save it to data/billfire_token.json")
        sys.exit(1)

    log(f"Token URL: {token_url[:80]}...")
    save_token_url(token_url)

    # 3. Launch browser
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
        )
        page = await context.new_page()

        try:
            if dry_run:
                # ── DRY RUN: Test both locations ──
                # Use provided invoices for Chatham, auto-select first for Dennis
                # (each location has different invoices)

                # Test Chatham first (we have the location ID from the token URL)
                chatham_ok = await test_location(page, token_url, "chatham", invoice_numbers)

                # Test Dennis (will discover location ID, select first available invoice)
                dennis_ok = await test_location(page, token_url, "dennis", [])

                log(f"\n{'=' * 60}")
                log(f"DRY RUN RESULTS")
                log(f"{'=' * 60}")
                log(f"  Chatham: {'PASS' if chatham_ok else 'FAIL'}")
                log(f"  Dennis:  {'PASS' if dennis_ok else 'FAIL'}")
                log(f"{'=' * 60}")

                await browser.close()
                sys.exit(0 if (chatham_ok or dennis_ok) else 1)

            # ── LIVE PAYMENT ──
            # 4. Determine location ID
            loc_id = LOCATIONS.get(location)
            if not loc_id:
                # Need to discover the location ID
                nav_ok = await navigate_to_dashboard(page, token_url)
                if not nav_ok:
                    log("ERROR: Could not navigate to Billfire dashboard")
                    await browser.close()
                    sys.exit(1)
                loc_id = await switch_location(page, token_url, location)
                if not loc_id:
                    log(f"ERROR: Could not find location ID for {location}")
                    await browser.close()
                    sys.exit(1)

            # 5. Navigate directly to click2pay page
            pay_ok = await navigate_to_pay(page, token_url, location_id=loc_id, location=location)
            if not pay_ok:
                log("ERROR: Could not load pay page")
                await browser.close()
                sys.exit(1)

            # 6. Select invoices
            log(f"\nSelecting {len(invoice_numbers)} invoices...")
            selection = await select_invoices(page, invoice_numbers)

            if not selection["selected"]:
                # Invoice not found — try the other location
                other_locations = {k: v for k, v in LOCATIONS.items() if v and v != loc_id}
                for other_name, other_id in other_locations.items():
                    log(f"\n  Invoice not found at {location} — trying {other_name}...")
                    pay_ok2 = await navigate_to_pay(page, token_url, location_id=other_id)
                    if pay_ok2:
                        selection = await select_invoices(page, invoice_numbers)
                        if selection["selected"]:
                            location = other_name
                            loc_id = other_id
                            log(f"  Found invoice(s) at {other_name}!")
                            break

            if not selection["selected"]:
                log("ERROR: Could not select any invoices at any location")
                await screenshot(page, "selection_failed")
                await dump_buttons(page, "selection-fail")
                await browser.close()
                sys.exit(1)

            if selection["not_found"]:
                log(f"  [WARN] {len(selection['not_found'])} invoices not found: {selection['not_found']}")

            log(f"  Selected {len(selection['selected'])} of {len(invoice_numbers)} invoices")
            await screenshot(page, "invoices_selected")

            # 8. Agree to terms and confirm payment
            log("\nSubmitting payment...")
            result = await agree_and_confirm(page, location=location)

            await screenshot(page, "06_final_result")

            # 9. Report result
            log(f"\n{'=' * 60}")
            log(f"PAYMENT RESULT")
            log(f"{'=' * 60}")
            log(f"  Status: {result['status']}")
            log(f"  Invoices: {len(selection['selected'])}")

            if result["status"] == "ok":
                conf_ref = result["confirmation_ref"]
                log(f"  Confirmation: {conf_ref}")
                print(f"\nCONFIRMATION_REF={conf_ref}")
                await browser.close()
                sys.exit(0)
            elif result["status"] == "unknown":
                log(f"  Message: {result.get('message', 'Unknown result')}")
                log(f"  Page text: {result.get('page_text', '')[:300]}")
                log("\nPayment may have succeeded but could not confirm.")
                log("Check the Billfire portal manually.")
                await browser.close()
                sys.exit(1)
            else:
                log(f"  Error: {result.get('message', 'Payment failed')}")
                await browser.close()
                sys.exit(1)

        except PlaywrightTimeoutError as e:
            log(f"\nTIMEOUT: {e}")
            await screenshot(page, "timeout_error")
            await browser.close()
            sys.exit(1)
        except Exception as e:
            log(f"\nERROR: {e}")
            await screenshot(page, "error")
            await browser.close()
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
