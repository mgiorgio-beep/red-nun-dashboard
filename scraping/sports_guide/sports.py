"""Flask routes for Red Nun Sports Guide"""
from datetime import datetime
from flask import Blueprint, render_template, jsonify, Response, request
import json
from .fanzo_scraper import load_sports_data, scrape_fanzo_guide

sports_bp = Blueprint('sports', __name__, template_folder='templates', static_folder='static', static_url_path='/sports/static')


@sports_bp.route('/sports/public')
def sports_guide_public():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_guide.html', data=data, stale=stale, show_nav=False)


@sports_bp.route('/sports')
def sports_guide():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_guide.html', data=data, stale=stale, show_nav=True)


@sports_bp.route('/sports/refresh', methods=['POST'])
def sports_guide_refresh():
    success = scrape_fanzo_guide()
    if success:
        return jsonify({'status': 'ok', 'message': 'Guide refreshed'})
    return jsonify({'status': 'error', 'message': 'Scrape failed. Check FANZO session cookie.'}), 500


@sports_bp.route('/sports/embed')
def sports_guide_embed():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_embed.html', data=data, stale=stale)


@sports_bp.route('/sports/api/data')
def sports_api_data():
    data = load_sports_data()
    callback = "(function(){window.__rednunSportsData=" + json.dumps(data) + ";document.dispatchEvent(new Event('rednunSportsLoaded'));})();"
    return Response(callback, mimetype='application/javascript')


def _is_stale(data):
    if not data or 'updated_at' not in data:
        return True
    try:
        updated = datetime.fromisoformat(data['updated_at'])
        return (datetime.now() - updated).total_seconds() > 86400
    except (ValueError, TypeError):
        return True


@sports_bp.route('/guide')
def sports_guide_short():
    data = load_sports_data()
    stale = _is_stale(data)
    return render_template('sports_guide.html', data=data, stale=stale, show_nav=False)


@sports_bp.route('/sports/api/odds')
def api_odds():
    """Serve cached odds data."""
    import json, os
    odds_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'odds.json')
    if os.path.exists(odds_file):
        with open(odds_file, 'r') as f:
            from flask import jsonify; return jsonify(json.load(f))
    return {'odds': {}}


@sports_bp.route('/sports/api/section-order', methods=['GET', 'POST'])
def section_order():
    """Get or set section display order."""
    import json, os
    from flask import request
    order_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'section_order.json')

    if request.method == 'POST':
        data = request.get_json()
        if data and 'order' in data:
            os.makedirs(os.path.dirname(order_file), exist_ok=True)
            with open(order_file, 'w') as f:
                json.dump({'order': data['order']}, f)
            return {'status': 'ok'}
        return {'error': 'missing order'}, 400

    if os.path.exists(order_file):
        with open(order_file, 'r') as f:
            from flask import jsonify; return jsonify(json.load(f))
    return {'order': []}
