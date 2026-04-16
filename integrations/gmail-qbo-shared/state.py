"""
Shared state: tracks which Gmail message IDs have been processed
by either the invoice scraper or the payment scraper.
"""
import json
import os
from datetime import datetime
from threading import Lock

STATE_DIR = os.path.expanduser("/opt/red-nun-dashboard/integrations/gmail-qbo-shared")
STATE_FILE = os.path.join(STATE_DIR, "processed_ids.json")

_lock = Lock()


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def load_state():
    """Return the full processed-IDs dict. Empty dict if file doesn't exist."""
    _ensure_dir()
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    """Atomic write via temp file + rename."""
    _ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def is_processed(message_id, state=None):
    """Check if a message_id has been processed."""
    if state is None:
        state = load_state()
    return message_id in state


def mark_processed(message_id, scraper, outcome, extra=None):
    """Record that a message has been processed. Thread-safe."""
    with _lock:
        state = load_state()
        state[message_id] = {
            "processed_at": datetime.now().isoformat(),
            "scraper": scraper,
            "outcome": outcome,
            **(extra or {}),
        }
        save_state(state)
