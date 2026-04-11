"""
AI Inventory Vision Engine — Red Nun Analytics
Extracts video frames using ffmpeg, then analyzes each with Claude Vision
to identify visible inventory items, quantities, and labels.
Also handles kitchen scale readings via the bottle_weights table.

Pipeline:
  video → extract_frames()   [ffmpeg, max 40 frames]
        → analyze_frame()    [Claude Sonnet Vision, per frame]
        → analyze_video()    [deduplicates across frames]
        → list of items in shared output format (source: "vision")

Bonus:
  scale image → read_scale_display() → bottle_weights lookup → fraction remaining
"""

import os
import json
import re
import base64
import logging
import shutil
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

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_FRAMES       = 40          # Hard cap — prevents runaway API costs
DEFAULT_INTERVAL = 5           # Seconds between frames by default
FRAME_QUALITY    = 3           # ffmpeg -q:v (2=best, 5=acceptable, lower=larger file)
VISION_MODEL     = "claude-sonnet-4-20250514"   # same model as invoice_processor.py

MIME_TYPES = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".pdf":  "application/pdf",
}


# =============================================================================
# STEP 1 — EXTRACT FRAMES
# =============================================================================

def _get_video_duration(video_path):
    """Return video duration in seconds (float). Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration_str = result.stdout.strip()
        return float(duration_str) if duration_str else None
    except Exception as exc:
        logger.warning(f"ffprobe duration check failed: {exc}")
        return None


def extract_frames(video_file_path, interval_seconds=DEFAULT_INTERVAL):
    """
    Extract JPEG frames from a video file using ffmpeg.

    Hard cap: maximum MAX_FRAMES (40) frames. If the video would produce more
    than 40 frames at the requested interval, the interval is widened to
    stay within the cap.

    Args:
        video_file_path:  Path to .mp4, .mov, .avi, .mkv, etc.
        interval_seconds: Desired seconds between frames (default 5).

    Returns:
        List of absolute paths to extracted .jpg frame files.
        Empty list if extraction fails.
    """
    if not os.path.exists(video_file_path):
        raise FileNotFoundError(f"Video file not found: {video_file_path}")

    # Check if this is actually an image already (sanity-check pass-through)
    ext = os.path.splitext(video_file_path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        logger.info(f"Image file passed to extract_frames — returning as single frame")
        return [video_file_path]

    # Determine video duration and choose interval
    duration = _get_video_duration(video_file_path)
    if duration:
        natural_frame_count = duration / interval_seconds
        if natural_frame_count > MAX_FRAMES:
            interval_seconds = duration / MAX_FRAMES
            logger.info(
                f"Video duration {duration:.0f}s would yield {natural_frame_count:.0f} frames "
                f"at {DEFAULT_INTERVAL}s interval — widening to {interval_seconds:.1f}s "
                f"to stay within {MAX_FRAMES}-frame cap"
            )
        logger.info(
            f"Video: {os.path.basename(video_file_path)} | "
            f"duration={duration:.0f}s | interval={interval_seconds:.1f}s | "
            f"expected_frames≤{int(duration/interval_seconds)+1}"
        )
    else:
        logger.warning("Could not determine video duration — using requested interval")

    # Create temp directory for frames
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = f"/tmp/rednun_frames_{timestamp}"
    os.makedirs(out_dir, exist_ok=True)
    frame_pattern = os.path.join(out_dir, "frame_%04d.jpg")

    # Build fps filter — "1/N" means one frame every N seconds
    fps_filter = f"fps=1/{interval_seconds:.3f}"

    cmd = [
        "/usr/bin/ffmpeg", "-y",
        "-i", video_file_path,
        "-vf", fps_filter,
        "-q:v", str(FRAME_QUALITY),
        frame_pattern,
    ]

    logger.info(f"Extracting frames to {out_dir} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out extracting frames")
        return []

    if result.returncode != 0:
        logger.error(f"ffmpeg error (rc={result.returncode}): {result.stderr[-400:]}")
        return []

    # Collect extracted frames — enforce hard cap
    frames = sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.endswith(".jpg")
    )

    if len(frames) > MAX_FRAMES:
        logger.warning(f"Frame count {len(frames)} exceeds cap {MAX_FRAMES} — truncating")
        # Evenly sample MAX_FRAMES from the full set
        step = len(frames) / MAX_FRAMES
        frames = [frames[int(i * step)] for i in range(MAX_FRAMES)]

    logger.info(f"Extracted {len(frames)} frames from {os.path.basename(video_file_path)}")
    return frames


# =============================================================================
# STEP 2 — ANALYZE A SINGLE FRAME
# =============================================================================

_FRAME_PROMPT = """You are analyzing a restaurant bar/storage area photo for inventory counting.
Identify SPECIFIC products you can see — read labels and brand names.

CRITICAL RULES:
- ONLY list items where you can read or confidently identify the brand/product name
- Use the EXACT brand name from the label (e.g. "Tito's Vodka", "Goslings Black Seal Rum")
- Do NOT create vague categories like "Assorted Liquor Bottles", "Various Spirits",
  "Bar supplies", "Liquor bottles assorted" — these are useless for inventory
- If you cannot read the label, SKIP the item entirely — do not guess
- Count individual bottles separately, not groups
- For partially full bottles, set is_partial: true and estimate the fill level as quantity
  (e.g. a bottle that is 75% full → estimated_quantity: 0.75, is_partial: true)
- A full sealed bottle → estimated_quantity: 1.0, is_partial: false

Return ONLY a valid JSON array — no other text.

For each visible item:
{
  "product_name": "Exact Brand Name from label",
  "estimated_quantity": 1.0,
  "unit": "bottle",
  "is_partial": false,
  "sealed_or_opened": "sealed",
  "visible_brand_or_label": "Exact text read from label",
  "notes": "",
  "confidence": 0.85,
  "source": "vision",
  "storage_location": ""
}

If a digital kitchen scale display is visible, also include ONE item:
{
  "product_name": "Scale Reading",
  "scale_reading": 14.2,
  "scale_unit": "oz",
  "estimated_quantity": 1,
  "unit": "bottle",
  "is_partial": true,
  "sealed_or_opened": "opened",
  "visible_brand_or_label": "",
  "notes": "scale display visible",
  "confidence": 0.95,
  "source": "vision",
  "storage_location": ""
}

Confidence scoring:
- 0.9+ = label clearly readable
- 0.7-0.85 = brand identifiable but label partially obscured
- Below 0.7 = SKIP the item, do not include it

Return empty array [] if no identifiable products are visible.
Return ONLY the JSON array."""


def _encode_file(file_path):
    """Read and base64-encode a file. Returns (b64_string, mime_type)."""
    ext = os.path.splitext(file_path)[1].lower()
    mime = MIME_TYPES.get(ext, "image/jpeg")
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return b64, mime


BATCH_SIZE = 5   # Frames per API call — reduces 40 calls to ~8


def _parse_vision_response(raw_text, label=""):
    """Parse Claude Vision JSON response text into a list of item dicts."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        array_match = re.search(r'\[[\s\S]*\]', text)
        if array_match:
            try:
                items = json.loads(array_match.group())
            except json.JSONDecodeError as exc:
                logger.error(f"Could not parse vision response for {label}: {exc}\nRaw: {text[:300]}")
                return []
        else:
            logger.debug(f"No JSON array in vision response for {label}: {text[:200]}")
            return []

    if not isinstance(items, list):
        logger.warning(f"Vision response was {type(items)} not list for {label}")
        return []

    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qty_raw = item.get("estimated_quantity") or item.get("quantity") or 0
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 0.0

        cleaned.append({
            "product_name":       item.get("product_name", "Unknown"),
            "product_id":         None,
            "quantity":           qty,
            "unit":               item.get("unit", "each"),
            "is_partial":         bool(item.get("is_partial", False)),
            "notes":              item.get("notes", ""),
            "confidence":         float(item.get("confidence", 0.7)),
            "source":             "vision",
            "storage_location":   item.get("storage_location", ""),
            "sealed_or_opened":   item.get("sealed_or_opened", ""),
            "visible_brand":      item.get("visible_brand_or_label", ""),
            "scale_reading":      item.get("scale_reading"),
            "scale_unit":         item.get("scale_unit"),
        })
    return cleaned


def analyze_frame(frame_path):
    """
    Send one image frame to Claude Vision and extract visible inventory items.
    (Legacy single-frame call — kept for compatibility. Batched path preferred.)
    """
    items = analyze_frame_batch([frame_path])
    return items


def analyze_frame_batch(frame_paths):
    """
    Send multiple image frames in a single Claude Vision API call.
    Returns a flat list of all items found across the batch.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in .env file")

    if not frame_paths:
        return []

    labels = [os.path.basename(p) for p in frame_paths]
    batch_label = f"{labels[0]}..{labels[-1]}" if len(labels) > 1 else labels[0]

    # Build content blocks: one image per frame + one text prompt
    content_blocks = []
    for frame_path in frame_paths:
        if not os.path.exists(frame_path):
            logger.error(f"Frame file not found: {frame_path}")
            continue
        try:
            b64_data, mime_type = _encode_file(frame_path)
        except Exception as exc:
            logger.error(f"Could not read frame {os.path.basename(frame_path)}: {exc}")
            continue
        content_type = "document" if mime_type == "application/pdf" else "image"
        content_blocks.append({
            "type": content_type,
            "source": {"type": "base64", "media_type": mime_type, "data": b64_data},
        })

    if not content_blocks:
        return []

    n_frames = len(content_blocks)
    prompt_text = _FRAME_PROMPT
    if n_frames > 1:
        prompt_text = (
            f"You are looking at {n_frames} frames from a restaurant inventory video.\n"
            f"Analyze ALL frames together. Identify SPECIFIC products visible across any of the frames.\n"
            f"If the same product appears in multiple frames, list it ONCE with the best count.\n\n"
            + _FRAME_PROMPT
        )

    content_blocks.append({"type": "text", "text": prompt_text})

    # Scale max_tokens with batch size
    max_tokens = min(4096, 1024 + n_frames * 512)

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
                    "model": VISION_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": content_blocks}],
                },
                timeout=90,
            )
        except requests.exceptions.Timeout:
            logger.error(f"analyze_frame_batch: API timed out for {batch_label} (attempt {attempt+1})")
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return []
        except requests.exceptions.RequestException as exc:
            logger.error(f"analyze_frame_batch: network error for {batch_label}: {exc}")
            return []

        if resp.status_code == 200:
            break
        if resp.status_code in (529, 500, 502, 503) and attempt < 2:
            logger.warning(f"analyze_frame_batch: API returned {resp.status_code}, retrying in {3*(attempt+1)}s")
            time.sleep(3 * (attempt + 1))
            continue
        logger.error(f"Claude API error {resp.status_code} for batch {batch_label}: {resp.text[:300]}")
        return []

    result = resp.json()
    raw_text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            raw_text += block["text"]

    cleaned = _parse_vision_response(raw_text, batch_label)

    logger.info(f"analyze_frame_batch: {len(cleaned)} items from {n_frames} frames ({batch_label})")
    return cleaned


# =============================================================================
# STEP 3 — ANALYZE ALL FRAMES + DEDUPLICATE
# =============================================================================

def _normalize_name(name):
    """Lowercase, strip, collapse whitespace — for deduplication keying."""
    return re.sub(r'\s+', ' ', (name or "").lower().strip())


def _dedup_key(name):
    """
    Aggressive normalization for vision dedup — collapses near-duplicates like
    "Ken's Blue Cheese" / "Ken's Blue Cheese Dressing" / "Blue Cheese Dressing"
    into the same key.
    """
    s = (name or "").lower().strip()
    # Remove possessives
    s = re.sub(r"[''']s?\b", "", s)
    # Remove common brand prefixes
    for prefix in ["ken", "hellmann", "heinz", "frank", "saratoga", "hewitt farms"]:
        s = re.sub(rf"^{prefix}\s+", "", s)
    # "Ken's Steak House" is the company name — strip it as a compound prefix
    s = re.sub(r"^steak house\s+", "", s)
    # Remove trailing category words
    for suffix in ["dressing", "sauce", "dressing/sauce", "dressing sauce",
                    "condiment", "spread", "mix", "blend", "seasoning"]:
        s = re.sub(rf"\s+{re.escape(suffix)}$", "", s)
    # Remove leading category words like "magic blend"
    for prefix_word in ["magic blend", "real"]:
        s = re.sub(rf"^{re.escape(prefix_word)}\s+", "", s)
    return re.sub(r'\s+', ' ', s.strip())


def analyze_video(video_file_path, interval_seconds=DEFAULT_INTERVAL):
    """
    Extract frames from a video and analyze each with Claude Vision.
    Deduplicates: same product seen in multiple frames → one output item.

    Deduplication rules:
      - Key = normalized product_name
      - quantity:    take the MAXIMUM seen across frames
      - confidence:  take the MAXIMUM seen across frames
      - notes:       merge unique notes (comma-separated)
      - is_partial:  True if ANY frame sees it as partial
      - visible_brand: use the first non-empty value

    Args:
        video_file_path:  Path to video file.
        interval_seconds: Seconds between frames extracted.

    Returns:
        Deduplicated list of item dicts in shared output format.
        Frames are extracted to /tmp/ and kept until process_video() cleans them.
    """
    frames = extract_frames(video_file_path, interval_seconds)
    if not frames:
        logger.warning("No frames extracted — aborting analyze_video")
        return [], []   # (items, frame_paths) — caller needs frame_paths to clean up

    # Batch frames into groups of BATCH_SIZE for fewer API calls
    batches = []
    for i in range(0, len(frames), BATCH_SIZE):
        batches.append(frames[i:i + BATCH_SIZE])

    logger.info(f"Analyzing {len(frames)} frames in {len(batches)} batches (batch_size={BATCH_SIZE})...")
    all_items = []
    for bi, batch in enumerate(batches):
        batch_names = [os.path.basename(f) for f in batch]
        logger.info(f"  Batch {bi+1}/{len(batches)}: {batch_names}")
        batch_items = analyze_frame_batch(batch)
        all_items.extend(batch_items)
        # Small delay between API calls to avoid rate-limit bursts
        if bi < len(batches) - 1:
            time.sleep(0.5)

    total_raw = len(all_items)

    # Deduplication: group by aggressive dedup key
    groups = {}
    for item in all_items:
        key = _dedup_key(item["product_name"])
        if key == "scale reading" or not key:
            # Scale readings are NOT deduplicated — each is a unique measurement
            key = f"scale_reading_{len(groups)}"
        if key not in groups:
            groups[key] = []
        groups[key].append(item)

    deduped = []
    for key, group in groups.items():
        # Merge the group into one canonical item
        best = max(group, key=lambda x: x["confidence"])  # start from highest-conf item
        merged = dict(best)

        merged["quantity"]   = max(g["quantity"]   for g in group)
        merged["confidence"] = max(g["confidence"] for g in group)
        merged["is_partial"] = any(g.get("is_partial") for g in group)

        # Merge unique non-empty notes
        all_notes = [g["notes"] for g in group if g.get("notes")]
        unique_notes = list(dict.fromkeys(all_notes))   # dedupe while preserving order
        merged["notes"] = "; ".join(unique_notes)

        # Use first non-empty visible_brand
        for g in group:
            if g.get("visible_brand"):
                merged["visible_brand"] = g["visible_brand"]
                break

        deduped.append(merged)

    merged_count = total_raw - len(deduped)
    logger.info(
        f"analyze_video: {total_raw} raw items → {len(deduped)} unique "
        f"({merged_count} duplicates merged) from {os.path.basename(video_file_path)}"
    )
    return deduped, frames


# =============================================================================
# SCALE READING
# =============================================================================

_SCALE_PROMPT = """Look at this image and read the kitchen scale display.
Return ONLY valid JSON — no other text.

If a scale display is visible:
{"scale_visible": true, "weight": 14.2, "unit": "oz"}

If no scale is visible or readable:
{"scale_visible": false}

Read the number exactly as displayed. Do not estimate."""


def read_scale_display(frame_path, bottle_brand=None):
    """
    Read a kitchen scale display from an image and calculate liquor fill level.

    If bottle_brand is provided, looks up the bottle in the bottle_weights table
    and computes the fraction remaining:
        fraction = (gross_weight - tare_weight_oz) / liquid_weight_oz

    Args:
        frame_path:    Path to image showing a scale with a bottle on it.
        bottle_brand:  Brand name to look up in bottle_weights (optional).

    Returns:
        Dict with scale reading and optional fraction calculation, or None if
        no scale is visible.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in .env file")

    try:
        b64_data, mime_type = _encode_file(frame_path)
    except Exception as exc:
        logger.error(f"Could not read scale image: {exc}")
        return None

    content_type = "document" if mime_type == "application/pdf" else "image"

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": VISION_MODEL,
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": content_type,
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": b64_data,
                                },
                            },
                            {"type": "text", "text": _SCALE_PROMPT},
                        ],
                    }
                ],
            },
            timeout=30,
        )
    except requests.exceptions.Timeout:
        logger.error("read_scale_display: API call timed out")
        return None
    except requests.exceptions.RequestException as exc:
        logger.error(f"read_scale_display: network error — {exc}")
        return None

    if resp.status_code != 200:
        logger.error(f"Scale API error {resp.status_code}: {resp.text[:200]}")
        return None

    raw_text = ""
    for block in resp.json().get("content", []):
        if block.get("type") == "text":
            raw_text += block["text"]

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        obj_match = re.search(r'\{[\s\S]*?\}', text)
        if obj_match:
            try:
                data = json.loads(obj_match.group())
            except json.JSONDecodeError:
                logger.warning(f"Could not parse scale response: {text[:200]}")
                return None
        else:
            return None

    if not data.get("scale_visible", False):
        logger.info("No scale visible in frame")
        return None

    weight_raw = data.get("weight")
    scale_unit = data.get("unit", "oz").lower()

    if weight_raw is None:
        return None

    try:
        gross_weight_oz = float(weight_raw)
    except (TypeError, ValueError):
        return None

    # Convert grams to oz if needed
    if scale_unit == "g":
        gross_weight_oz = gross_weight_oz / 28.3495
        scale_unit = "oz"

    logger.info(f"Scale reading: {gross_weight_oz:.1f} oz")

    result = {
        "product_name":     bottle_brand or "Unknown Bottle",
        "product_id":       None,
        "quantity":         None,          # filled in below if bottle found
        "unit":             "bottle",
        "is_partial":       True,
        "notes":            f"scale: {gross_weight_oz:.1f} oz gross",
        "confidence":       0.9,
        "source":           "vision",
        "storage_location": "",
        "scale_reading_oz": gross_weight_oz,
    }

    # Look up bottle_weights to calculate fill fraction
    if bottle_brand:
        conn = get_connection()
        bw = conn.execute(
            """
            SELECT brand_name, tare_weight_oz, liquid_weight_oz, full_weight_oz,
                   bottle_size_label
            FROM bottle_weights
            WHERE LOWER(brand_name) LIKE LOWER(?)
            ORDER BY verified DESC
            LIMIT 1
            """,
            (f"%{bottle_brand}%",),
        ).fetchone()
        conn.close()

        if bw:
            tare   = bw["tare_weight_oz"]
            liquid = bw["liquid_weight_oz"]
            full   = bw["full_weight_oz"]

            if liquid and liquid > 0:
                net_liquid_oz = gross_weight_oz - tare
                fraction = round(max(0.0, min(1.0, net_liquid_oz / liquid)), 2)
                result["quantity"]    = fraction
                result["is_partial"]  = fraction < 1.0
                result["product_name"]= bw["brand_name"]
                result["notes"] = (
                    f"scale: {gross_weight_oz:.1f} oz gross | "
                    f"tare: {tare:.1f} oz | "
                    f"net: {net_liquid_oz:.1f} oz / {liquid:.1f} oz = {fraction*100:.0f}% full"
                )
                logger.info(
                    f"Scale calc for {bw['brand_name']}: "
                    f"{gross_weight_oz:.1f} oz gross → {fraction*100:.0f}% full"
                )
            else:
                logger.warning(f"bottle_weights entry for {bottle_brand!r} has no liquid_weight_oz")
        else:
            logger.info(f"No bottle_weights entry found for {bottle_brand!r}")

    return result


# =============================================================================
# MASTER FUNCTION
# =============================================================================

def process_video(video_file_path, interval_seconds=DEFAULT_INTERVAL):
    """
    Master pipeline: extract frames → analyze each → deduplicate → clean up.

    Temp frame files in /tmp/rednun_frames_<timestamp>/ are removed after
    processing regardless of success or failure.

    Args:
        video_file_path:  Path to video (or image) file.
        interval_seconds: Seconds between extracted frames.

    Returns:
        Deduplicated list of item dicts in shared output format (source: "vision").
    """
    if not os.path.exists(video_file_path):
        raise FileNotFoundError(f"File not found: {video_file_path}")

    logger.info(f"process_video START — {os.path.basename(video_file_path)}")
    t0 = time.time()

    frames = []
    try:
        items, frames = analyze_video(video_file_path, interval_seconds)
    finally:
        # Clean up temp directory (frames live under /tmp/rednun_frames_*/
        if frames:
            frame_dir = os.path.dirname(frames[0])
            if frame_dir.startswith("/tmp/rednun_frames_") and os.path.isdir(frame_dir):
                try:
                    shutil.rmtree(frame_dir)
                    logger.info(f"Cleaned up temp frames: {frame_dir}")
                except Exception as exc:
                    logger.warning(f"Could not remove temp dir {frame_dir}: {exc}")

    elapsed = round(time.time() - t0, 1)
    logger.info(
        f"process_video DONE — {len(items)} items in {elapsed}s "
        f"from {os.path.basename(video_file_path)}"
    )
    return items
