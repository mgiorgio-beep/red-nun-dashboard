"""
AI Inventory Database Tables
Creates the three AI-specific tables that extend the existing manual count system.
These tables sit alongside count_sessions / count_items — they do NOT replace them.
"""

import logging
import os
from data_store import get_connection

# ── Logging setup ────────────────────────────────────────────────────────────
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
# ─────────────────────────────────────────────────────────────────────────────


def init_ai_inventory_tables():
    """
    Create the AI inventory tables in the shared toast_data.db.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    conn = get_connection()
    conn.executescript("""
        -- AI-driven inventory sessions.
        -- Each session represents one glasses/audio recording run.
        -- count_session_id links back to the manual count_sessions table
        -- so AI-confirmed quantities can be pushed into the existing count flow.
        CREATE TABLE IF NOT EXISTS ai_inventory_sessions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            location                TEXT    NOT NULL,
            session_date            TEXT,
            status                  TEXT    DEFAULT 'draft',
            source_type             TEXT    DEFAULT 'glasses',
            audio_file_path         TEXT,
            video_file_path         TEXT,
            raw_transcript          TEXT,
            item_count              INTEGER DEFAULT 0,
            auto_confirmed_count    INTEGER DEFAULT 0,
            flagged_count           INTEGER DEFAULT 0,
            count_session_id        INTEGER,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed_at            TIMESTAMP,
            confirmed_by            TEXT,
            upload_token            TEXT,
            started_by              TEXT,
            started_at              TIMESTAMP,
            FOREIGN KEY (count_session_id) REFERENCES count_sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_ai_sessions_location
            ON ai_inventory_sessions(location, session_date);
        CREATE INDEX IF NOT EXISTS idx_ai_sessions_status
            ON ai_inventory_sessions(status);

        -- Individual items captured in an AI session.
        -- audio_quantity / vision_quantity store the raw per-engine readings.
        -- reconciled_quantity is the resolved value used after comparison.
        -- confirmed_quantity is what the manager ultimately approves.
        CREATE TABLE IF NOT EXISTS ai_inventory_items (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              INTEGER NOT NULL,
            product_id              INTEGER,
            product_name            TEXT    NOT NULL,
            storage_location_id     INTEGER,
            quantity                REAL,
            unit                    TEXT,
            is_partial              INTEGER DEFAULT 0,
            audio_quantity          REAL,
            audio_confidence        REAL,
            vision_quantity         REAL,
            vision_confidence       REAL,
            reconciled_quantity     REAL,
            reconciled_confidence   REAL,
            flag                    TEXT    DEFAULT 'none',
            flag_notes              TEXT,
            confirmed_quantity      REAL,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id)          REFERENCES ai_inventory_sessions(id)
                                              ON DELETE CASCADE,
            FOREIGN KEY (product_id)          REFERENCES product_inventory_settings(id),
            FOREIGN KEY (storage_location_id) REFERENCES storage_locations(id)
        );

        CREATE INDEX IF NOT EXISTS idx_ai_items_session
            ON ai_inventory_items(session_id);
        CREATE INDEX IF NOT EXISTS idx_ai_items_product
            ON ai_inventory_items(product_id);
        CREATE INDEX IF NOT EXISTS idx_ai_items_flag
            ON ai_inventory_items(flag);

        -- Historical record of AI-confirmed counts per product.
        -- Used for variance analysis and trend tracking.
        CREATE TABLE IF NOT EXISTS ai_inventory_history (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id              INTEGER,
            session_id              INTEGER,
            quantity                REAL,
            unit                    TEXT,
            counted_date            TEXT,
            variance_vs_previous    REAL,
            variance_vs_theoretical REAL,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id)  REFERENCES product_inventory_settings(id),
            FOREIGN KEY (session_id)  REFERENCES ai_inventory_sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_ai_history_product
            ON ai_inventory_history(product_id, counted_date);
        CREATE INDEX IF NOT EXISTS idx_ai_history_session
            ON ai_inventory_history(session_id);
    """)
    conn.commit()
    conn.close()
    logger.info("AI inventory tables initialized (ai_inventory_sessions, ai_inventory_items, ai_inventory_history)")
