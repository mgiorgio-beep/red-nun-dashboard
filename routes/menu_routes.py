"""
Menu sync routes — manual Toast menu sync only.
No automatic/background API calls.
"""

import logging
from flask import Blueprint, jsonify, request
from auth_routes import login_required
from toast_client import ToastAPIClient
from data_store import store_menus

logger = logging.getLogger(__name__)

menu_bp = Blueprint('menu', __name__)


@menu_bp.route('/api/menu/sync-toast', methods=['POST'])
@login_required
def sync_toast_menu():
    """Pull current menu from Toast API and store in menu_items table.
    Manual trigger only — called from Settings UI button."""
    location = request.json.get('location', 'dennis') if request.is_json else 'dennis'

    try:
        client = ToastAPIClient()
        menus_data = client.get_menus(location)

        if not menus_data:
            return jsonify({"success": False, "error": "No menu data returned from Toast"}), 404

        count = store_menus(location, menus_data)

        logger.info(f"Menu sync complete: {count} items for {location}")
        return jsonify({
            "success": True,
            "items_synced": count,
            "location": location,
        })

    except Exception as e:
        logger.error(f"Menu sync failed for {location}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
