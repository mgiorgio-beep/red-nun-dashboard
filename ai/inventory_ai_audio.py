"""
AI Inventory Audio Engine — Red Nun Analytics
Transcribes inventory count recordings using Whisper, then parses
the transcript with Claude to extract structured inventory items.
Matches parsed items to the product_inventory_settings catalog via rapidfuzz.

Pipeline:
  file (mp4/m4a/wav/…) → extract_audio_from_video()
                        → transcribe_audio()          [Whisper base]
                        → parse_inventory_transcript() [Claude Haiku]
                        → match_products()             [rapidfuzz WRatio]
                        → list of items in shared output format
"""

import os
import json
import re
import logging
import subprocess
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from data_store import get_connection

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_ai.log")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _fh = logging.FileHandler(_log_file)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_fh)
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

# ── Whisper model (lazy-loaded once per process) ───────────────────────────────
_whisper_model = None

# ── Location keyword map (spoken → storage_locations.name) ───────────────────
_LOCATION_ALIASES = {
    "walk-in cooler":   "Walk-in Cooler",
    "walk-in":          "Walk-in Cooler",
    "walk in cooler":   "Walk-in Cooler",
    "walk in":          "Walk-in Cooler",
    "cooler":           "Walk-in Cooler",
    "the cooler":       "Walk-in Cooler",
    "dry storage":      "Dry Storage",
    "dry goods":        "Dry Storage",
    "dry":              "Dry Storage",
    "bar":              "Bar",
    "the bar":          "Bar",
    "freezer":          "Freezer",
    "the freezer":      "Freezer",
    "frozen":           "Freezer",
    "front line":       "Front Line",
    "the line":         "Front Line",
    "front":            "Front Line",
    "line":             "Front Line",
    "shed":             "shed",
    "the shed":         "shed",
}


# =============================================================================
# STEP 1 — EXTRACT AUDIO
# =============================================================================

def extract_audio_from_video(file_path):
    """
    Extract mono 16 kHz PCM WAV audio from a video (or passthrough audio files).

    Args:
        file_path: Path to .mp4, .mov, .m4a, .wav, .mp3, .aac, .ogg, or .flac

    Returns:
        Path to .wav file ready for Whisper (same path if already a suitable audio file)
    """
    ext = os.path.splitext(file_path)[1].lower()

    # Audio-only formats — Whisper can take them directly
    if ext in ('.wav', '.mp3', '.m4a', '.aac', '.ogg', '.flac', '.webm'):
        logger.info(f"Audio file detected ({ext}), passing through: {os.path.basename(file_path)}")
        return file_path

    # Video — extract audio track with ffmpeg
    out_path = os.path.splitext(file_path)[0] + "_audio.wav"
    cmd = [
        "/usr/bin/ffmpeg", "-y",
        "-i", file_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        out_path
    ]

    logger.info(f"Extracting audio from {os.path.basename(file_path)}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out after 5 minutes extracting audio")
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found — install with: apt-get install ffmpeg")
    except Exception as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}")

    if result.returncode != 0:
        logger.error(f"ffmpeg stderr: {result.stderr[:400]}")
        raise RuntimeError(f"ffmpeg returned exit code {result.returncode}")

    logger.info(f"Audio extracted → {os.path.basename(out_path)}")
    return out_path


# =============================================================================
# STEP 2 — TRANSCRIBE
# =============================================================================

def _get_whisper_model():
    """Load Whisper small model (once). Raises MemoryError if RAM is insufficient."""
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            logger.info("Loading Whisper small model (~461 MB download on first use)...")
            _whisper_model = whisper.load_model("small")
            logger.info("Whisper small model loaded successfully")
        except MemoryError as exc:
            logger.error(
                "Insufficient RAM to load Whisper base model. "
                "Migrate to the Beelink (more RAM) or pre-transcribe on another machine."
            )
            raise exc
        except Exception as exc:
            logger.error(f"Unexpected error loading Whisper: {exc}")
            raise
    return _whisper_model


def transcribe_audio(audio_file_path):
    """
    Transcribe an audio file using local Whisper base model.

    Args:
        audio_file_path: Path to audio file

    Returns:
        Raw transcript text string (empty string on failure)
    """
    t0 = time.time()
    logger.info(f"Transcribing: {os.path.basename(audio_file_path)}")

    model = _get_whisper_model()

    try:
        result = model.transcribe(
            audio_file_path,
            language="en",
            fp16=False,         # Required on CPU — no FP16 support
            verbose=False,
        )
    except Exception as exc:
        logger.error(f"Whisper transcription failed: {exc}")
        return ""

    text = result.get("text", "").strip()
    elapsed = round(time.time() - t0, 1)
    logger.info(f"Transcription done: {len(text)} chars, {elapsed}s")
    return text


# =============================================================================
# STEP 3 — PARSE TRANSCRIPT WITH CLAUDE
# =============================================================================

def parse_inventory_transcript(transcript_text):
    """
    Send raw transcript text to Claude Haiku to extract structured inventory items.

    Args:
        transcript_text: Raw string from Whisper (or any source)

    Returns:
        List of item dicts in shared output format (source: "audio"), product_id=None.
        product_id and storage_location_id are filled in by match_products().
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in .env file")

    if not transcript_text or not transcript_text.strip():
        logger.warning("Empty transcript — nothing to parse")
        return []

    prompt = f"""You are parsing a restaurant inventory count recording transcript.
The speaker walked through storage areas (Walk-in Cooler, Bar, Freezer, Dry Storage,
Front Line, shed) counting items out loud.

TRANSCRIPT:
{transcript_text}

Extract every inventory item mentioned. Return ONLY a valid JSON array — no other text.

CRITICAL — Liquor bottle counting pattern:
The speaker counts FULL bottles and PARTIAL bottles separately for the same product.
Example: "One full bottle of Myers Rum, one bottle at half"
  → This means: 1 full bottle (1.0) PLUS 1 partial bottle (0.5) = 1.5 total bottles.
  → Output ONE item: product_name="Myers Rum", quantity=1.5, is_partial=true, unit="bottle"
  → In notes, record the breakdown: "1 full + 1 at 50%"

More examples:
  "one full bottle, one bottle at three quarter" → quantity=1.75 (1.0 + 0.75), is_partial=true
  "one full bottle, one bottle at 90%" → quantity=1.9 (1.0 + 0.9), is_partial=true
  "no full bottle, one bottle three quarters" → quantity=0.75, is_partial=true
  "two full bottles, one partial at a quarter" → quantity=2.25 (2.0 + 0.25), is_partial=true
  "three full bottles" → quantity=3.0, is_partial=false

ALWAYS combine full + partial into a SINGLE item per product with the total quantity.

Number conversion rules:
- Words: one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10
- "a couple"=2, "a few"=3, "a dozen"=12, "half a dozen"=6
- Fractions: "half"=0.5, "a quarter"=0.25, "three quarters"=0.75, "three quarter"=0.75
- Percentages: "90%"=0.9, "75%"=0.75, "50%"=0.5, "25%"=0.25, "10%"=0.10
- Combined: "three and a half"=3.5, "two and a quarter"=2.25, "one and a half"=1.5
- "one partial" or "partial" at end → add 0.5 to quantity, set is_partial: true
- "about" or "roughly" → set confidence to 0.65

Storage location tracking:
- When speaker says "moving to the bar", "in the freezer", "walk-in", etc. →
  apply that location to ALL subsequent items until another location is mentioned
- Known locations: Walk-in Cooler, Dry Storage, Bar, Freezer, Front Line, shed

Notes / flags:
- Capture observations: "looks light", "looks old", "expiring soon", "low",
  "reorder", "flag this", "needs to be ordered" → put in notes field

Unit defaults (when speaker does not specify):
- Liquor / wine → "bottle"
- Beer (cans/bottles) → "case"
- Beer (kegs) → "keg"
- Produce, meat, dry goods → "case"
- Unknown → "each"

For each item produce exactly this JSON object:
{{
  "product_name": "name as spoken, normalized to title case",
  "product_id": null,
  "quantity": 1.5,
  "unit": "case",
  "is_partial": false,
  "notes": "",
  "confidence": 0.9,
  "source": "audio",
  "storage_location": "Walk-in Cooler"
}}

storage_location must be one of: Walk-in Cooler, Dry Storage, Bar, Freezer, Front Line, shed
— or an empty string if unknown.

Return ONLY the JSON array, nothing else."""

    # Retry up to 3 times on transient errors (529 overloaded, 500, etc.)
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 8192,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                },
                timeout=60,
            )
        except requests.exceptions.Timeout:
            logger.error("parse_inventory_transcript: API call timed out after 60s (attempt %d)", attempt + 1)
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return []
        except requests.exceptions.RequestException as exc:
            logger.error(f"parse_inventory_transcript: network error — {exc}")
            return []

        if resp.status_code == 200:
            break
        if resp.status_code in (529, 500, 502, 503) and attempt < 2:
            logger.warning("parse_inventory_transcript: API returned %d, retrying in %ds (attempt %d)",
                           resp.status_code, 3 * (attempt + 1), attempt + 1)
            time.sleep(3 * (attempt + 1))
            continue
        logger.error(f"Claude API error {resp.status_code}: {resp.text[:300]}")
        return []

    result = resp.json()
    stop_reason = result.get("stop_reason", "unknown")
    raw_text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            raw_text += block["text"]

    logger.info(f"parse_inventory_transcript: stop_reason={stop_reason}, response_len={len(raw_text)}")

    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON array embedded in the response
        array_match = re.search(r'\[[\s\S]*\]', text)
        if array_match:
            try:
                items = json.loads(array_match.group())
            except json.JSONDecodeError as exc:
                logger.error(f"Could not parse Claude response: {exc}\nRaw: {text[:400]}")
                return []
        else:
            # If truncated (max_tokens), try to salvage partial JSON
            if stop_reason == "max_tokens" or not text.endswith("]"):
                logger.warning(f"Response appears truncated (stop_reason={stop_reason}, len={len(text)}), attempting partial parse")
                # Find the last complete JSON object by looking for last "},"
                last_complete = text.rfind("},")
                if last_complete > 0:
                    salvage = text[:last_complete + 1] + "]"
                    try:
                        items = json.loads(salvage)
                        logger.info(f"Salvaged {len(items)} items from truncated response")
                    except json.JSONDecodeError:
                        logger.error(f"Could not salvage truncated response:\n{text[:400]}")
                        return []
                else:
                    logger.error(f"Could not salvage truncated response (no complete objects):\n{text[:400]}")
                    return []
            else:
                logger.error(f"No JSON array found in Claude response:\n{text[:400]}")
                return []

    if not isinstance(items, list):
        logger.error(f"Claude returned {type(items)} instead of list")
        return []

    # Ensure required fields are present and typed correctly
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item.setdefault("product_id", None)
        item.setdefault("is_partial", False)
        item.setdefault("notes", "")
        item.setdefault("confidence", 0.8)
        item.setdefault("source", "audio")
        item.setdefault("storage_location", "")
        # Normalize quantity to float
        try:
            item["quantity"] = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            item["quantity"] = 0.0
        cleaned.append(item)

    logger.info(f"parse_inventory_transcript: extracted {len(cleaned)} items")
    return cleaned


# =============================================================================
# STEP 4 — MATCH PRODUCTS
# =============================================================================

def _load_product_catalog():
    """Load all countable products from product_inventory_settings."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, product_name, category, ordering_unit, inventory_unit, is_canonical
        FROM product_inventory_settings
        WHERE skip_inventory = 0
        ORDER BY is_canonical DESC, product_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_storage_locations(location_filter=None):
    """Load storage_locations rows, optionally filtered by short location name."""
    conn = get_connection()
    if location_filter:
        loc = location_filter.lower()
        loc = 'dennis' if 'dennis' in loc else ('chatham' if 'chatham' in loc else loc)
        rows = conn.execute(
            "SELECT id, name FROM storage_locations WHERE location = ? ORDER BY name",
            (loc,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, location FROM storage_locations ORDER BY name"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _match_storage_location(spoken_location, storage_locations):
    """
    Resolve a spoken location phrase to a storage_locations row.

    Returns:
        (storage_location_id, canonical_name)  — both None if no match
    """
    if not spoken_location:
        return None, None

    spoken_lower = spoken_location.lower().strip()

    # Fast path: alias lookup
    canonical_name = _LOCATION_ALIASES.get(spoken_lower)
    if canonical_name:
        for sl in storage_locations:
            if sl['name'].lower() == canonical_name.lower():
                return sl['id'], sl['name']

    # Fuzzy fallback
    from rapidfuzz import process, fuzz
    names = [sl['name'] for sl in storage_locations]
    result = process.extractOne(spoken_lower, names, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= 70:
        matched_name = result[0]
        for sl in storage_locations:
            if sl['name'] == matched_name:
                return sl['id'], sl['name']

    return None, spoken_location   # Preserve original spoken name if unresolved


def match_products(parsed_items, location=None):
    """
    Match parsed item names against the product_inventory_settings catalog.

    Scoring (rapidfuzz WRatio):
      >= 80  → auto-matched:  product_id set, flag stays 'none'
      60-79  → low-confidence: product_id set, flag = 'review'
      < 60   → no match:      product_id = None, flag = 'new_product'

    Also resolves storage_location strings to storage_location_id.

    Args:
        parsed_items: Output from parse_inventory_transcript()
        location:     'dennis' or 'chatham' (or 'Dennis Port' / 'Chatham') to
                      filter storage_locations; None = load all

    Returns:
        Updated item list with product_id, matched_name, match_score,
        storage_location_id, flag, flag_notes populated.
    """
    from rapidfuzz import process, fuzz

    if not parsed_items:
        return []

    catalog = _load_product_catalog()
    storage_locations = _load_storage_locations(location)
    product_names = [p['product_name'] for p in catalog]

    # Build name→product dict for O(1) lookup after match
    name_to_product = {p['product_name']: p for p in catalog}

    results = []
    matched_count = 0

    for raw_item in parsed_items:
        item = dict(raw_item)
        spoken_name = item.get('product_name', '').strip()

        if not spoken_name:
            item.setdefault('product_id', None)
            item.setdefault('flag', 'none')
            results.append(item)
            continue

        # ── Product matching ──────────────────────────────────────────────────
        # Use token_sort_ratio — WRatio is too loose (partial substring gives false 86%)
        best = process.extractOne(
            spoken_name,
            product_names,
            scorer=fuzz.token_sort_ratio,
        )

        if best:
            score = best[1]
            matched_product = name_to_product.get(best[0])

            if matched_product:
                item['matched_name'] = matched_product['product_name']
                item['match_score'] = round(score, 1)

                if score >= 70:
                    item['product_id'] = matched_product['id']
                    if item.get('flag') in (None, '', 'none'):
                        item['flag'] = 'none'
                    parse_conf = float(item.get('confidence', 0.8))
                    item['confidence'] = round(min(parse_conf, score / 100.0), 2)
                    matched_count += 1

                elif score >= 55:
                    item['product_id'] = matched_product['id']
                    item['flag'] = 'review'
                    existing_notes = item.get('flag_notes', '') or ''
                    item['flag_notes'] = (
                        f"Low match {score:.0f}% — confirm product is {matched_product['product_name']!r}"
                        + (f"; {existing_notes}" if existing_notes else "")
                    )
                    parse_conf = float(item.get('confidence', 0.7))
                    item['confidence'] = round(min(parse_conf, score / 100.0), 2)
                    matched_count += 1

                else:
                    item['product_id'] = None
                    item['flag'] = 'new_product'
                    existing_notes = item.get('flag_notes', '') or ''
                    item['flag_notes'] = (
                        f"No match (best: {best[0]!r} at {score:.0f}%)"
                        + (f"; {existing_notes}" if existing_notes else "")
                    )
                    item['confidence'] = round(min(float(item.get('confidence', 0.5)), 0.4), 2)
            else:
                item['product_id'] = None
                item['flag'] = 'new_product'
                item['match_score'] = 0
        else:
            item['product_id'] = None
            item['flag'] = 'new_product'
            item['match_score'] = 0

        # ── Storage location resolution ───────────────────────────────────────
        spoken_loc = item.get('storage_location', '') or ''
        loc_id, loc_name = _match_storage_location(spoken_loc, storage_locations)
        item['storage_location_id'] = loc_id
        item['storage_location'] = loc_name   # Overwrite with canonical name

        results.append(item)

    logger.info(
        f"match_products: {matched_count}/{len(results)} matched "
        f"(>= 80%), {len(results) - matched_count} flagged/unmatched"
    )
    return results


# =============================================================================
# MASTER FUNCTION
# =============================================================================

def process_audio(file_path, location=None):
    """
    Master pipeline: extract audio → transcribe → parse → match products.

    Args:
        file_path: Path to video or audio file in inventory_intake/
        location:  'dennis' / 'Dennis Port' or 'chatham' / 'Chatham'
                   Used to filter storage locations. None = load all.

    Returns:
        List of matched item dicts in shared output format.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    logger.info(f"process_audio START — {os.path.basename(file_path)}")
    t0 = time.time()

    # 1. Extract audio
    audio_path = extract_audio_from_video(file_path)

    # 2. Transcribe
    transcript = transcribe_audio(audio_path)
    if not transcript:
        logger.warning("Empty transcript — aborting pipeline")
        return []

    # 3. Parse with Claude
    parsed = parse_inventory_transcript(transcript)

    # 4. Match products
    matched = match_products(parsed, location=location)

    elapsed = round(time.time() - t0, 1)
    logger.info(
        f"process_audio DONE — {len(matched)} items in {elapsed}s "
        f"from {os.path.basename(file_path)}"
    )
    return matched


# =============================================================================
# SELF-TEST  (skips Whisper — tests NLP parsing + product matching only)
# =============================================================================

if __name__ == "__main__":
    TEST_CASES = [
        {
            "label": "Basic food + beer count",
            "transcript": "Six cases bud light. Three and a half cases chicken breast. Two bags romaine.",
        },
        {
            "label": "Bar zone transition with partials",
            "transcript": "Moving to the bar now. Tito's, about three quarters. Jameson, half.",
        },
        {
            "label": "Flags and notes",
            "transcript": "Mushrooms one case looks light. French fries eight cases.",
        },
    ]

    print("\n" + "=" * 65)
    print("  AUDIO ENGINE SELF-TEST — NLP PARSING + PRODUCT MATCHING")
    print("=" * 65)

    total_items = 0
    total_matched = 0

    for tc in TEST_CASES:
        print(f"\n{'─'*65}")
        print(f"  {tc['label']}")
        print(f"  Transcript: {tc['transcript']!r}")
        print()

        items = parse_inventory_transcript(tc['transcript'])
        items = match_products(items)

        for item in items:
            pid       = item.get('product_id')
            score     = item.get('match_score', 0)
            matched   = item.get('matched_name', '—')
            flag      = item.get('flag', 'none')
            qty       = item.get('quantity', '?')
            unit      = item.get('unit', '?')
            partial   = item.get('is_partial', False)
            notes     = item.get('notes', '')
            loc       = item.get('storage_location') or '—'
            conf      = item.get('confidence', 0)

            status = "✅" if pid and flag == 'none' else ("⚠️ " if flag == 'review' else "❌")
            print(f"  {status} {item['product_name']!r}")
            print(f"       qty={qty} {unit}  partial={partial}  loc={loc!r}")
            if notes:
                print(f"       notes={notes!r}")
            print(f"       → DB: {matched!r}  id={pid}  score={score:.0f}%  flag={flag}")
            print(f"       confidence={conf}")
            print()
            total_items += 1
            if pid:
                total_matched += 1

    print("=" * 65)
    print(f"  SUMMARY: {total_matched}/{total_items} items matched to DB products")
    print("=" * 65 + "\n")
