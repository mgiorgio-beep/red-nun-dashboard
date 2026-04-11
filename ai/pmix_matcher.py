import logging
from rapidfuzz import fuzz, process
from data_store import get_connection

logger = logging.getLogger(__name__)


def suggest_pmix_mappings(min_score=85):
    """
    For each unmapped menu item, find the best matching recipe by name.
    Auto-saves matches >= min_score. Returns suggestions for lower scores.
    Returns: {'auto_mapped': int, 'suggestions': list}
    """
    conn = get_connection()
    c = conn.cursor()

    # Get all distinct menu item names that have no mapping yet
    c.execute("""
        SELECT DISTINCT oi.item_name
        FROM order_items oi
        LEFT JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
        WHERE pm.id IS NULL
          AND oi.item_name IS NOT NULL
          AND oi.item_name != ''
          AND oi.voided = 0
    """)
    unmapped = [row[0] for row in c.fetchall()]

    # Get all active recipes
    c.execute("SELECT id, name FROM recipes WHERE active = 1 OR active IS NULL")
    recipes = c.fetchall()
    recipe_names = [r[1] for r in recipes]
    recipe_map = {r[1]: r[0] for r in recipes}

    auto_mapped = 0
    suggestions = []

    for menu_name in unmapped:
        if not recipe_names:
            break
        result = process.extractOne(menu_name, recipe_names, scorer=fuzz.WRatio)
        if result is None:
            continue
        match_name, score, _ = result
        recipe_id = recipe_map[match_name]

        if score >= min_score:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO pmix_mapping (menu_item_name, recipe_id, multiplier)
                    VALUES (?, ?, 1.0)
                """, (menu_name, recipe_id))
                if c.rowcount > 0:
                    auto_mapped += 1
                    logger.info(f"Auto-mapped: '{menu_name}' -> recipe #{recipe_id} '{match_name}' ({score:.0f}%)")
            except Exception as e:
                logger.error(f"Error auto-mapping '{menu_name}': {e}")
        elif score >= 60:
            suggestions.append({
                'menu_item_name': menu_name,
                'suggested_recipe_id': recipe_id,
                'suggested_recipe_name': match_name,
                'score': round(score, 1)
            })

    conn.commit()
    conn.close()
    return {'auto_mapped': auto_mapped, 'suggestions': suggestions}
