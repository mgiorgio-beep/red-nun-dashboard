"""
Voice Recipe Builder Routes — Red Nun Analytics

Endpoints for voice-based recipe creation:
  POST /api/voice-recipe/process — upload audio, transcribe + parse + match
  POST /api/voice-recipe/save — save parsed recipe to DB

Pipeline: Audio → Whisper (base) → Haiku parse → fuzzy match → review → save
"""

import os
import json
import logging
import tempfile
import requests
from flask import Blueprint, request, jsonify
from dotenv import load_dotenv

from data_store import get_connection
from vendor_item_matcher import match_vendor_item_to_product
from recipe_costing import cost_recipe

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

voice_recipe_bp = Blueprint("voice_recipe", __name__)

# Load Whisper model once at module level
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        model_path = os.path.expanduser("~/.cache/whisper/base.pt")
        if not os.path.exists(model_path):
            logger.warning("Whisper base model not cached — downloading ~140MB on first use")
        _whisper_model = whisper.load_model("base")
        logger.info("Whisper base model loaded")
    return _whisper_model


@voice_recipe_bp.route("/api/voice-recipe/process", methods=["POST"])
def process_voice_recipe():
    """
    Upload audio, transcribe with Whisper, parse with Haiku, match ingredients.
    Accepts audio/webm or audio/mp4 file upload.
    """
    try:
        file = request.files.get("audio")
        if not file:
            return jsonify({"error": "No audio file provided"}), 400

        # Detect format from Content-Type or filename
        content_type = file.content_type or ""
        filename = file.filename or ""
        if "mp4" in content_type or filename.endswith(".mp4"):
            ext = ".mp4"
        elif "m4a" in content_type or filename.endswith(".m4a"):
            ext = ".m4a"
        else:
            ext = ".webm"

        # Save to temp file with correct extension
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        try:
            file.save(tmp)
            tmp.close()

            # Transcribe with Whisper
            logger.info(f"Transcribing audio: {tmp.name} ({ext})")
            model = get_whisper_model()
            result = model.transcribe(tmp.name)
            transcript = result.get("text", "").strip()

            if not transcript:
                return jsonify({"error": "Could not transcribe audio. Please try again with clearer audio."}), 400

            logger.info(f"Transcript: {transcript[:100]}...")

        finally:
            # Clean up temp file
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

        # Parse with Claude Haiku
        parsed = _parse_recipe_with_haiku(transcript)
        if not parsed:
            return jsonify({
                "error": "Could not parse recipe from transcript",
                "transcript": transcript,
            }), 400

        # Match ingredients to existing products
        conn = get_connection()
        matches = []
        try:
            for ing in parsed.get("ingredients", []):
                item_dict = {"product_name": ing["name"]}
                match = match_vendor_item_to_product(item_dict, conn)
                matches.append({
                    "ingredient_name": ing["name"],
                    "product_id": match["product_id"],
                    "product_name": match["product_name"],
                    "score": match["score"],
                    "match_type": match["match_type"],
                })
        finally:
            conn.close()

        return jsonify({
            "transcript": transcript,
            "parsed": parsed,
            "matches": matches,
        })

    except Exception as e:
        logger.error(f"Voice recipe process error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def _parse_recipe_with_haiku(transcript):
    """Send transcript to Claude Haiku for structured parsing."""
    if not ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY set, cannot parse recipe")
        return None

    prompt = f"""Parse this spoken recipe into JSON:
{transcript}

Return this exact structure (valid JSON, double quotes only):
{{
  "name": "recipe name",
  "category": "FOOD or APPETIZER or ENTREE or DESSERT etc",
  "servings": 1,
  "ingredients": [
    {{
      "name": "ingredient name, cleaned up",
      "quantity": 1.0,
      "unit": "oz | lb | cup | tsp | tbsp | each | gallon | quart"
    }}
  ],
  "notes": "any other info mentioned"
}}

Rules:
- Ingredient names should match common kitchen product names
- Convert all fractions to decimals (1/2 = 0.5)
- If a detail is unclear, make a reasonable guess
- servings defaults to 1 if not mentioned
- Return ONLY the JSON object, no markdown, no explanation"""

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
                "max_tokens": 1000,
                "system": "You are a recipe parser for a restaurant. Extract structured recipe data from spoken text. Return ONLY valid JSON, no other text.",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(f"Haiku parse error {resp.status_code}: {resp.text[:300]}")
            return None

        result = resp.json()
        text = result.get("content", [{}])[0].get("text", "").strip()

        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

        parsed = json.loads(text)
        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error from Haiku: {e}\nText: {text[:200]}")
        return None
    except Exception as e:
        logger.error(f"Haiku parse error: {e}")
        return None


@voice_recipe_bp.route("/api/voice-recipe/save", methods=["POST"])
def save_voice_recipe():
    """
    Save a parsed and reviewed recipe to the database.
    Body: {name, category, servings, menu_price, ingredients: [{product_id, quantity, unit}]}
    """
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Recipe name is required"}), 400

        category = data.get("category", "FOOD")
        servings = float(data.get("servings", 1))
        menu_price = float(data.get("menu_price", 0))
        ingredients = data.get("ingredients", [])

        conn = get_connection()
        try:
            # Create recipe
            conn.execute("""
                INSERT INTO recipes (name, category, serving_size, menu_price, active)
                VALUES (?, ?, ?, ?, 1)
            """, (name, category, servings, menu_price))
            recipe_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Create recipe_ingredients
            for ing in ingredients:
                product_id = ing.get("product_id")
                quantity = float(ing.get("quantity", 0))
                unit = ing.get("unit", "")

                if not product_id:
                    continue

                conn.execute("""
                    INSERT INTO recipe_ingredients (recipe_id, product_id, quantity, unit, yield_pct)
                    VALUES (?, ?, ?, ?, 100)
                """, (recipe_id, product_id, quantity, unit))

            conn.commit()

            # Cost the recipe immediately
            cost_result = cost_recipe(recipe_id, conn)

            return jsonify({
                "recipe_id": recipe_id,
                "message": f"Recipe '{name}' saved!",
                "cost": cost_result,
            })
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Save voice recipe error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
