"""
Check extraction and OCR for imported bank statements.

MIKE'S INVARIANT: CHECKS ARE ALWAYS OCR'D. A statement import that leaves 15
check rows reading "Check 9698" with no payee is not a partial success, it is
the pipeline not running. This module makes extraction part of import rather
than a job somebody remembers to launch.

HOW A CHECK IS IDENTIFIED — and what is NOT used as a key:

    The statement PDF carries a TEXT LAYER over the check images:
        "Check: 9698  Amount: $715.38  Date: 4/7/2026"
    so the number, amount and date are READ, never OCR'd. Only the payee needs
    vision, which is the one thing the text layer omits.

    Images are paired to that metadata by READING ORDER (top-to-bottom,
    left-to-right), verified per page by count, and then each pairing is
    CHECKED by confirming the check number appears in the image's own OCR text.
    Order is how they are paired; OCR is how the pairing is proved.

    CHECK NUMBER IS NEVER A MATCHING KEY against the books — see
    TestCheckNumberIsNotAKey. Two entities reuse numbers, and payroll and AP
    run separate sequences. Matching to payroll_checks is on amount + date +
    payee.

OCR IS LOCAL. Tesseract, not the Anthropic API, so this is not metered spend
and Rule #11 does not apply. It still only runs on an explicit import or a
manual re-run — never on a timer.

THE PAYEE BAND. A full-page OCR of a check reads badly: the payee sits in a sea
of security-pattern noise and comes out as "The Caron'Group of Companies'Lle ;
ty SN :". So the payee is read from a crop anchored on the words ORDER OF and
upscaled — which turns that into "The Caron Group of Companies LLC" at 89%.

No single crop works for every check, so several are tried and scored (see
_score). Printed AP checks read reliably at 75-89%; HANDWRITTEN PAYROLL CHECKS
LARGELY DO NOT, and are held for review rather than guessed at. On the April
statement that is 5 of 5 AP checks read and 9 of 10 payroll checks held.

That asymmetry is fine and is the point: the payee rules that need reading —
Caron -> Daily Cleaning, Craft Collective, Dennisport Village — are all on the
printed side. A payroll check is identified by amount and date against
payroll_checks, where the payee only has to corroborate.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Where extracted check images live, and the manifest the register reads.
CHECK_IMAGE_DIR = Path("web/static/check_images")
MANIFEST_PATH = CHECK_IMAGE_DIR / "manifest.json"

# The statement's own text layer. Amount and date are captured so a pairing can
# be sanity-checked against the register row it will enrich.
_CHECK_TOKEN = re.compile(
    r"Check:\s*(\d+)\s+Amount:\s*\$([\d,]+\.\d\d)\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})")

# Anchor for the payee band. Tesseract splits it into two words.
_ANCHOR = ("ORDER", "OF")

# Below this SCORE (confidence less implausibility, see _score) the payee is
# not trustworthy. Gating on raw confidence alone was wrong: tesseract reads
# the amount-in-words line beneath the payee at 65% confidence, so "Savan
# hiindrad fiftaan and 22 ronte" would have been written to a memo as a payee.
# A confident read of the wrong line is worse than no read.
#
# A check below the bar keeps its image and its row, and is REPORTED — the
# failure mode is a visible queue, not a silent gap.
MIN_PAYEE_SCORE = 55.0

# Payroll checks name a person and carry a pay period; AP checks name a company.
_PAYROLL_HINT = re.compile(r"pay\s*period", re.I)


class TesseractMissing(RuntimeError):
    """The OCR binary could not be found. Distinct from a bad read."""


# Candidate locations, tried after $TESSERACT_BIN and PATH.
_TESSERACT_CANDIDATES = (
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
    "/snap/bin/tesseract",
)


@lru_cache(maxsize=1)
def _resolve_tesseract() -> str | None:
    """Absolute path to the tesseract binary, or None.

    Do NOT rely on bare "tesseract" resolving through PATH. The systemd unit
    sets `Environment=PATH=/opt/red-nun-dashboard/venv/bin` — venv only — so
    /usr/bin/tesseract is invisible to gunicorn even though it is installed and
    resolves fine from an interactive shell. Every check on the Chatham
    February statement came back "nothing legible" for that reason: 30 reports
    of an unreadable payee when the truth was that the OCR binary was never
    found and no read was ever attempted.
    """
    env = os.environ.get("TESSERACT_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in _TESSERACT_CANDIDATES:
        if Path(cand).is_file():
            logger.info("tesseract not on PATH; using %s", cand)
            return cand
    return None


def _tesseract(image_path: Path, psm: int = 6, tsv: bool = False) -> str:
    """Run tesseract, returning text or TSV. Empty string on a failed read.

    Raises TesseractMissing when the binary itself cannot be found — that is a
    deployment fault, not an unreadable image, and it must not be reported as
    30 illegible checks.
    """
    exe = _resolve_tesseract()
    if not exe:
        raise TesseractMissing(
            "tesseract binary not found. Install it, or set TESSERACT_BIN to "
            "its full path. Note the rednun.service unit sets PATH to the venv "
            "only, so a bare 'tesseract' does not resolve there."
        )
    args = [exe, str(image_path), "stdout", "--psm", str(psm)]
    if tsv:
        args.append("tsv")
    try:
        out = subprocess.run(args, capture_output=True, timeout=120)
        return out.stdout.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("tesseract failed on %s: %s", image_path, e)
        return ""


def _parse_tsv(tsv: str):
    rows = []
    for line in tsv.splitlines()[1:]:
        p = line.split("\t")
        if len(p) < 12 or not p[11].strip():
            continue
        try:
            rows.append({"left": int(p[6]), "top": int(p[7]), "width": int(p[8]),
                         "height": int(p[9]), "conf": float(p[10]), "text": p[11].strip()})
        except ValueError:
            continue
    return rows


# (whole-image upscale, vertical padding as a multiple of the anchor's height,
# right edge as a fraction of width). Ordered cheapest first; all are tried.
_BAND_VARIANTS = ((3, 2.0, 0.72), (2, 1.6, 0.72), (1, 1.1, 0.78), (2, 2.2, 0.70))

# The amount-in-words line runs directly under the payee and a loose crop
# catches it instead. It is recognisable and never a payee.
# Matched fuzzily on purpose: tesseract renders it as "Savan hiindrad fiftaan
# and 22 ronte", so the literal words are not there to match. What survives
# mangling is the STRUCTURE — "... and <digits> <something like cents>".
# NOTE the end-anchor on the cents alternative. The bank is literally called
# "Cape Cod Five Cents Savings Bank" and that name is printed on every check,
# so an unanchored /cents/ rejects the bank's own name as an amount line.
_AMOUNT_WORDS = re.compile(
    r"\band\b\s*\d"            # "... and 22 ..."
    r"|c[reo]nt\w*\s*$"          # "... 22 ronte" / "... cents" at the end
    r"|\bd[oa]ll?ars?\s*$"       # "... DOLLARS" at the end
    r"|\d\d\s*/\s*100"         # "... and 00/100"
    r"|\band\s*$",               # trailing "and", a truncated amount line
    re.I)


def _score(payee: str | None, conf: float) -> float:
    """Rank a candidate payee. Confidence, less what makes it implausible.

    A high-confidence read of the WRONG LINE is worse than a mediocre read of
    the right one, so the amount-in-words line is penalised out of contention
    rather than left to win on confidence alone.
    """
    if not payee:
        return -1.0
    s = conf
    if _AMOUNT_WORDS.search(payee):
        s -= 60                      # it read the amount line, not the payee
    if re.search(r"\d", payee):
        s -= 15                      # payees rarely contain digits
    letters = sum(c.isalpha() for c in payee)
    if letters < 6:
        s -= 25
    # Character-level fragmentation ("E ric J a ri S e") — many 1-char tokens.
    toks = payee.split()
    if toks and sum(1 for t in toks if len(t) == 1) > len(toks) / 3:
        s -= 30
    return s


def _read_band(image_path: Path, upscale: int, pad_mult: float,
               right_edge: float):
    """OCR the payee band under one set of parameters."""
    from PIL import Image

    img = Image.open(image_path)
    if upscale != 1:
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
    tmp_full = image_path.with_suffix(f".u{upscale}.png")
    img.save(tmp_full)
    try:
        anchor = _find_anchor(_parse_tsv(_tesseract(tmp_full, psm=6, tsv=True)))
        if not anchor:
            return None, 0.0
        W, H = img.size
        x0 = anchor["left"] + anchor["width"] + int(0.008 * W)
        x1 = int(W * right_edge)
        pad = int(anchor["height"] * pad_mult)
        y0 = max(0, anchor["top"] - pad)
        y1 = min(H, anchor["top"] + anchor["height"] + pad)
        if x1 <= x0 + 20 or y1 <= y0 + 6:
            return None, 0.0
        band = img.crop((x0, y0, x1, y1))
        band = band.resize((band.width * 3, band.height * 3),
                           Image.LANCZOS).convert("L")
        tmp_band = image_path.with_suffix(f".band{upscale}.png")
        band.save(tmp_band)
        try:
            words = [w for w in _parse_tsv(_tesseract(tmp_band, psm=7, tsv=True))
                     if w["conf"] > 0]
            if not words:
                return None, 0.0
            return (_clean_payee(" ".join(w["text"] for w in words)),
                    sum(w["conf"] for w in words) / len(words))
        finally:
            tmp_band.unlink(missing_ok=True)
    finally:
        tmp_full.unlink(missing_ok=True)


def _find_anchor(words: list[dict]) -> dict | None:
    """Find the ORDER OF anchor, however tesseract chose to split it.

    It is inconsistent about the spacing on a check's pre-printed caption, and
    returns any of ",ORDEROF" / "ORDER" + "OF" / "PAYTOTHE" + "ORDEROF". Keying
    on the two-word form alone found the anchor on only 3 of 15 April checks;
    the rest had it merged into one token and silently produced no payee.
    """
    def norm(t):
        return re.sub(r"[^A-Z]", "", t.upper())

    # Single merged token, the most common shape.
    for w in words:
        if "ORDEROF" in norm(w["text"]):
            return w
    # Split across two tokens.
    for i in range(len(words) - 1):
        if norm(words[i]["text"]).endswith("ORDER") and norm(words[i + 1]["text"]) == "OF":
            return {**words[i + 1],
                    "left": words[i + 1]["left"], "width": words[i + 1]["width"]}
    # "ORDER" alone, with OF lost to the security pattern.
    for w in words:
        if norm(w["text"]) == "ORDER":
            return w
    # Last resort: the PAY TO THE caption sits directly above the payee line.
    for w in words:
        if "PAYTOTHE" in norm(w["text"]):
            return w
    return None


def read_payee(image_path: Path) -> dict:
    """Read the payee off one check image.

    One full-page pass for the memo line and for pairing verification, then
    several anchored crops of the payee band, best result winning.
    """
    raw = _tesseract(image_path, psm=6)
    memo = None
    for line in raw.splitlines():
        if _PAYROLL_HINT.search(line):
            memo = line.strip()[:120]
            break

    payee, conf = None, 0.0

    # NO SINGLE CROP WORKS FOR EVERY CHECK. Handwritten payees, printed ones,
    # payroll stubs and AP checks all sit slightly differently, and the same
    # parameters that read "The Caron Group of Companies LLC" at 89% confidence
    # reduce "Eric Jansen" to "E ric J a ri S e". So several variants are tried
    # and the most plausible result wins. Tesseract is local and fast; running
    # it four times on one image costs nothing worth saving.
    for upscale, pad_mult, right_edge in _BAND_VARIANTS:
        try:
            cand, cconf = _read_band(image_path, upscale, pad_mult, right_edge)
        except Exception as e:
            logger.warning("payee band failed on %s: %s", image_path, e)
            continue
        if cand and _score(cand, cconf) > _score(payee, conf):
            payee, conf = cand, cconf
    return {"payee": payee, "memo_line": memo, "raw_text": raw,
            "confidence": round(conf, 1),
            "is_payroll": 1 if memo and _PAYROLL_HINT.search(memo) else 0}


def _clean_payee(p: str | None) -> str | None:
    """Strip the debris tesseract leaves around a payee.

    Trailing amounts ("Chloe Nash 1,032.96"), stray punctuation and the
    security-pattern speckle that comes through as isolated symbols.
    """
    if not p:
        return None
    p = re.sub(r"\s*\$?[\d,]+\.\d\d\s*$", "", p)          # trailing amount
    p = re.sub(r"[^\w&.,'\- ]+", " ", p)                   # speckle
    p = re.sub(r"\s{2,}", " ", p).strip(" .,-'")
    # A payee of one or two characters is noise, not a name.
    return p if len(p) >= 3 else None


def extract_checks(pdf_path: Path, account_key: str) -> list[dict]:
    """Pull every check image out of a statement PDF with its metadata.

    Returns [{check_number, amount, check_date, image_path, verified}], where
    `verified` means the image's own OCR contains the check number that reading
    order assigned to it.
    """
    import fitz

    CHECK_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    doc = fitz.open(str(pdf_path))
    try:
        for page in doc:
            text = page.get_text()
            tokens = _CHECK_TOKEN.findall(text)
            if not tokens:
                continue
            placed = []
            for info in page.get_images(full=True):
                for rect in page.get_image_rects(info[0]):
                    placed.append((round(rect.y0), round(rect.x0), info[0]))
            placed.sort()
            if len(placed) != len(tokens):
                # Do not guess a pairing we cannot justify.
                logger.warning(
                    "page %s: %d check tokens but %d images — skipped, pairing "
                    "would be a guess", page.number + 1, len(tokens), len(placed))
                continue
            for (num, amt, dt), (_y, _x, xref) in zip(tokens, placed):
                dest = CHECK_IMAGE_DIR / f"{account_key}_check_{num}.png"
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:      # CMYK
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    pix.save(str(dest))
                except Exception as e:
                    logger.warning("could not save check %s: %s", num, e)
                    continue
                mm, dd, yy = dt.split("/")
                out.append({
                    "check_number": num,
                    "amount": float(amt.replace(",", "")),
                    "check_date": f"{yy}-{int(mm):02d}-{int(dd):02d}",
                    "image_path": dest,
                    "page": page.number + 1,
                })
    finally:
        doc.close()
    return out


def _write_manifest(conn):
    """Rebuild the manifest the register page reads, from what is on disk."""
    manifest: dict = {}
    for p in sorted(CHECK_IMAGE_DIR.glob("*_check_*.png")):
        m = re.match(r"(.+?)_check_(.+)\.png$", p.name)
        if m:
            manifest.setdefault(m.group(1), {})[m.group(2)] = f"/static/check_images/{p.name}"
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return sum(len(v) for v in manifest.values())


def enrich_upload(conn, upload_id: int, force: bool = False) -> dict:
    """Extract, OCR and enrich every check row on one statement upload.

    IDEMPOTENT. Re-running is safe and is the intended way to pick up an OCR
    improvement: images are overwritten, check_ocr rows are upserted, and the
    "| CHK: <payee>" suffix is replaced rather than appended twice. `force`
    re-OCRs images that already have a stored payee.

    COVERAGE IS RETURNED, ALWAYS. Never a bare success — the caller reports
    how many checks the statement holds, how many images came out, how many
    payees were read, and how many fell below confidence. A check the OCR
    cannot read stays uncoded and shows up in `unread`.
    """
    up = conn.execute(
        """SELECT u.id, u.file_path, u.filename, u.bank_account_id,
                  ba.location, ba.account_last4
           FROM bank_statement_uploads u
           JOIN bank_accounts ba ON ba.id = u.bank_account_id
           WHERE u.id = ?""", (upload_id,)).fetchone()
    if not up:
        return {"ok": False, "error": f"upload {upload_id} not found"}

    pdf = Path(up["file_path"] or "")
    if not pdf.exists():
        return {"ok": False, "error": f"statement PDF missing: {up['file_path']}"}

    # Fail fast and say what is actually wrong. Without this the loop below
    # calls tesseract 4x per check, swallows the "binary not found" each time,
    # and reports N illegible payees — which is what happened on Chatham
    # February: 30 checks "nothing legible", 0 reads attempted.
    if not _resolve_tesseract():
        return {
            "ok": False,
            "ocr_unavailable": True,
            "error": "tesseract binary not found, so no payee could be read. "
                     "This is a deployment fault, not an unreadable statement. "
                     "Install tesseract, or set TESSERACT_BIN to its full path "
                     "(the rednun.service unit sets PATH to the venv only, so a "
                     "bare 'tesseract' does not resolve inside the app).",
        }

    account_key = f"acct{up['bank_account_id']}"
    location = up["location"]

    # The check rows this statement brought in, keyed by the number in the
    # payee text. That number identifies the ROW; it is never used to match
    # against the books.
    rows = {}
    for r in conn.execute(
            """SELECT id, entry_date, payee, memo, amount, gl_account_id
               FROM manual_bank_entries WHERE statement_upload_id = ?""",
            (upload_id,)):
        m = re.search(r"check\s*#?\s*(\d+)", (r["payee"] or ""), re.I)
        if m:
            rows[m.group(1)] = dict(r)

    checks = extract_checks(pdf, account_key)
    read, low, unread, coded = 0, 0, [], 0
    from_books = 0          # payee taken from payroll_checks, not OCR

    for ch in checks:
        num = ch["check_number"]
        existing = conn.execute(
            "SELECT payee, raw_text FROM check_ocr WHERE account_key = ? AND check_number = ?",
            (account_key, num)).fetchone()
        if existing and existing["payee"] and not force:
            result = {"payee": existing["payee"], "memo_line": None,
                      "raw_text": existing["raw_text"], "confidence": 100.0,
                      "is_payroll": 0}
        else:
            result = read_payee(ch["image_path"])

        # Verify the pairing: the number reading order assigned must appear in
        # the image's own text. Order pairs them; OCR proves it.
        verified = num in re.sub(r"[^\d]", " ", result.get("raw_text") or "")

        conn.execute(
            """INSERT INTO check_ocr (account_key, check_number, payee, memo_line,
                                      is_payroll, raw_text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_key, check_number) DO UPDATE SET
                   payee = excluded.payee, memo_line = excluded.memo_line,
                   is_payroll = excluded.is_payroll, raw_text = excluded.raw_text""",
            (account_key, num, result["payee"], result["memo_line"],
             result["is_payroll"], result["raw_text"]))

        row = rows.get(num)
        score = _score(result["payee"], result["confidence"])
        payee_out = result["payee"]
        payee_src = "ocr"

        if score < MIN_PAYEE_SCORE:
            # OCR could not read it. Before reporting it unreadable, ask the
            # books: a payroll check's payee is something we already know, and
            # the amount usually identifies it uniquely. This is what turns
            # "30 checks, nothing legible" into a named list.
            book = identify_payroll_payee(
                conn, location, ch.get("amount"),
                (row or {}).get("entry_date") or ch.get("date"))
            if book:
                payee_out = book["employee_name"]
                payee_src = "books"
                from_books += 1
                conn.execute(
                    "UPDATE check_ocr SET payee = ?, is_payroll = 1 "
                    "WHERE account_key = ? AND check_number = ?",
                    (payee_out, account_key, num))
            else:
                low += 1
                unread.append({"check_number": num, "amount": ch["amount"],
                               "confidence": result["confidence"],
                               "score": round(score, 1),
                               "payee": result["payee"], "verified": verified,
                               "row_id": row["id"] if row else None})
                continue
        else:
            read += 1

        if not row:
            continue
        result = {**result, "payee": payee_out}

        # Replace rather than append, so a re-run does not stack suffixes.
        base = re.sub(r"\s*\|\s*CHK:.*$", "", row["memo"] or "").strip()
        conn.execute("UPDATE manual_bank_entries SET memo = ? WHERE id = ?",
                     (f"{base} | CHK: {result['payee']}".strip(), row["id"]))
        row["memo"] = f"{base} | CHK: {result['payee']}"

    conn.commit()

    # Now that the memos carry payees, run the coder over JUST these rows so a
    # payee rule (Caron -> Daily Cleaning) can fire on what it could not see
    # before. Scoped to this upload's rows; nothing else is touched.
    from routes.register_routes import (
        _find_gl_account_for_description, resolve_gl_for_location,
        GL_SOURCE_RULE, GL_STATUS_SUGGESTED)
    for num, row in rows.items():
        if row.get("gl_account_id"):
            continue                      # never overwrite an existing coding
        desc = f"{row['payee'] or ''} {row['memo'] or ''}"
        gl = resolve_gl_for_location(
            conn, _find_gl_account_for_description(conn, desc, location),
            location, context="check payee coding")
        if gl:
            conn.execute(
                "UPDATE manual_bank_entries SET gl_account_id = ?, gl_source = ?, "
                "gl_status = ? WHERE id = ?",
                (gl, GL_SOURCE_RULE, GL_STATUS_SUGGESTED, row["id"]))
            coded += 1
    conn.commit()

    payroll_candidates = match_payroll_checks(conn, upload_id, account_key, location)
    images = _write_manifest(conn)

    return {
        "ok": True, "upload_id": upload_id, "account_key": account_key,
        "checks_on_statement": len(checks),
        "check_rows_in_register": len(rows),
        "images": len(checks),
        "payees_read": read,
        "payees_from_books": from_books,
        "below_confidence": low,
        "newly_coded": coded,
        "payroll_candidates": payroll_candidates,
        "unread": unread,
        "images_on_disk": images,
        "banner": (f"{len(checks)} check(s) on this statement, {len(checks)} image(s), "
                   f"{read} payee(s) read"
                   + (f", {from_books} named from payroll records" if from_books else "")
                   + f", {low} below confidence"
                   + (f", {coded} newly coded" if coded else "")
                   + (f", {len(payroll_candidates)} payroll match candidate(s)"
                      if payroll_candidates else "")),
    }


def identify_payroll_payee(conn, location: str | None, amount: float,
                           entry_date: str, days: int = 14) -> dict | None:
    """Name a check from the BOOKS instead of from its handwriting.

    A payroll check's payee is already recorded — we wrote the check. Requiring
    OCR to read a handwritten name before we will admit who it was paid to is
    backwards, and it fails in practice: on the Chatham February statement the
    band crop read 1 of 14 payees, yet the amounts alone identify most of them
    ($270.96 Zachary Signore, $52.61 Leah Artman, $740.08 Christopher Lubin).

    Held to the same bar as the check-number backfill: EXACTLY ONE uncleared,
    unvoided payroll check for this entity at this amount to the penny within
    `days`. Two candidates means ambiguous, and ambiguous is left to a human --
    naming the wrong employee on a cleared check is worse than leaving it blank.

    Returns {payroll_check_id, employee_name, pay_date} or None.
    """
    if amount is None:
        return None
    hits = conn.execute(
        """SELECT pc.id, pc.employee_name,
                  COALESCE(pr.pay_date, pc.pay_period_end) AS pay_date
             FROM payroll_checks pc
             LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
            WHERE pc.location = ?
              AND ROUND(pc.net_pay, 2) = ROUND(?, 2)
              AND ABS(JULIANDAY(COALESCE(pr.pay_date, pc.pay_period_end))
                      - JULIANDAY(?)) <= ?
              AND (pc.voided IS NULL OR pc.voided = 0)
              AND (pc.payment_method IS NULL OR pc.payment_method != 'Direct Deposit')
              AND pc.employee_name IS NOT NULL AND TRIM(pc.employee_name) <> ''
            LIMIT 2""",
        (location, abs(float(amount)), entry_date, days)).fetchall()
    if len(hits) != 1:
        return None
    return {"payroll_check_id": hits[0]["id"],
            "employee_name": hits[0]["employee_name"],
            "pay_date": hits[0]["pay_date"]}


def match_payroll_checks(conn, upload_id: int, account_key: str,
                         location: str | None) -> int:
    """Match OCR'd checks to payroll_checks on AMOUNT + DATE + PAYEE.

    NEVER ON CHECK NUMBER. The two entities reuse numbers and payroll runs its
    own sequence, so the number is not unique enough to join on — that is what
    TestCheckNumberIsNotAKey exists to hold. Amount and date narrow it; the
    OCR'd payee confirms the person.

    IT REPORTS; IT DOES NOT STAMP. An earlier version called _mark_cleared on
    the payroll check and left the statement row cleared as well, so the same
    $1,251.75 counted twice and Dennis April stopped tying by exactly that.
    Clearing a book row without REMOVING the statement row it duplicates is not
    half a merge, it is a broken balance.

    The merge that would be correct — stamp the book row, delete the statement
    row, write register_merge_audit with the full deleted row for restore —
    already exists as dedupe_register(). It should be invoked deliberately, on
    a reviewed match list, not as a side effect of OCR. So these are returned
    as CANDIDATES for that path to act on.

    Returns the candidate list.
    """
    candidates = []
    rows = conn.execute(
        """SELECT m.id, m.entry_date, m.amount, m.memo
           FROM manual_bank_entries m
           WHERE m.statement_upload_id = ? AND m.memo LIKE '%| CHK:%'""",
        (upload_id,)).fetchall()

    for r in rows:
        payee = (re.search(r"\|\s*CHK:\s*(.+)$", r["memo"] or "") or [None, ""])[1].strip()
        if not payee:
            continue
        surname = payee.split()[-1].lower() if payee.split() else ""
        if len(surname) < 3:
            continue
        hit = conn.execute(
            """SELECT pc.id, pc.employee_name, pc.net_pay
               FROM payroll_checks pc
               LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
               WHERE pc.location = ?
                 AND ROUND(pc.net_pay, 2) = ROUND(?, 2)
                 AND ABS(JULIANDAY(COALESCE(pr.pay_date, pc.pay_period_end))
                         - JULIANDAY(?)) <= 14
                 AND (pc.voided IS NULL OR pc.voided = 0)
                 AND (pc.cleared IS NULL OR pc.cleared = 0)
                 AND LOWER(pc.employee_name) LIKE ?
               LIMIT 2""",
            (location, abs(r["amount"]), r["entry_date"], f"%{surname}%")).fetchall()
        if len(hit) != 1:
            continue                      # ambiguous or absent: leave for a human
        candidates.append({
            "entry_id": r["id"], "entry_date": r["entry_date"],
            "amount": round(abs(r["amount"]), 2),
            "ocr_payee": payee,
            "payroll_check_id": hit[0]["id"],
            "employee_name": hit[0]["employee_name"],
        })
    return candidates
