"""Temp Stick WiFi sensor integration — walk-in freezer/cooler monitoring.

Polls the Temp Stick cloud API (https://tempstickapi.com/docs/) every 10 min
via the in-app scheduler, logs readings to SQLite, serves current + history
to the dashboard, and emails an alert if a freezer warms past its threshold
or the sensor stops checking in.

API key: pasted into the dashboard card (stored in app_settings table),
or TEMPSTICK_API_KEY in .env as a fallback.
"""
import os
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

from integrations.toast.data_store import get_connection

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

logger = logging.getLogger(__name__)

API_BASE = "https://tempstickapi.com/api/v1"
REQUEST_TIMEOUT = 20

# Alert defaults (freezer should sit around -10 to 0 F)
DEFAULT_THRESHOLD_F = 10.0     # alert when above this
RECOVERY_MARGIN_F = 2.0        # recovered when back below threshold - margin
CONSECUTIVE_WARM_TO_ALERT = 2  # ~30 min at a 15-min send interval
SPIKE_MARGIN_F = 15.0          # single reading this far above threshold alerts immediately
OFFLINE_ALERT_HOURS = 3        # no checkin for this long -> offline alert
REALERT_HOURS = 4              # re-send warm alert at most every 4 h

_tables_ready = False


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------

def _ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS tempstick_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id   TEXT NOT NULL,
            sensor_name TEXT,
            ts_utc      TEXT NOT NULL,
            temp_c      REAL,
            temp_f      REAL,
            humidity    REAL,
            battery_pct REAL,
            UNIQUE(sensor_id, ts_utc)
        );
        CREATE INDEX IF NOT EXISTS idx_tempstick_sensor_ts
            ON tempstick_readings(sensor_id, ts_utc);
    """)
    conn.commit()
    conn.close()
    _tables_ready = True


def get_setting(key, default=None):
    _ensure_tables()
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    _ensure_tables()
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_api_key():
    return get_setting("tempstick_api_key") or os.getenv("TEMPSTICK_API_KEY")


def _get_alert_state():
    try:
        return json.loads(get_setting("tempstick_alert_state") or "{}")
    except Exception:
        return {}


def _save_alert_state(state):
    set_setting("tempstick_alert_state", json.dumps(state))


# ------------------------------------------------------------------
# Temp Stick API
# ------------------------------------------------------------------

def _api_headers(api_key):
    return {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "RedNunDashboard/1.0",
        "X-Client-Name": "red-nun-dashboard",
        "X-Client-Version": "1.0",
    }


def _fetch_sensors(api_key):
    """Return a list of sensor dicts from GET /sensors/all."""
    resp = requests.get(f"{API_BASE}/sensors/all",
                        headers=_api_headers(api_key), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        for key in ("items", "sensors"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def _parse_ts(raw):
    """Parse a Temp Stick timestamp (UTC) into an aware datetime, or None."""
    if not raw:
        return None
    s = str(raw).strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def c_to_f(c):
    return None if c is None else round(float(c) * 9 / 5 + 32, 1)


# ------------------------------------------------------------------
# Poller (runs every 10 min via the in-app scheduler)
# ------------------------------------------------------------------

def poll_tempstick():
    """Fetch latest readings for all sensors, store them, evaluate alerts."""
    api_key = get_api_key()
    if not api_key:
        return  # not configured yet — nothing to do

    try:
        sensors = _fetch_sensors(api_key)
    except Exception as e:
        logger.error(f"Temp Stick poll failed: {e}")
        return

    _ensure_tables()
    conn = get_connection()
    stored = 0
    for s in sensors:
        sensor_id = str(s.get("sensor_id") or "")
        if not sensor_id:
            continue
        ts = _parse_ts(s.get("last_checkin"))
        if not ts:
            continue
        temp_c = s.get("last_temp")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tempstick_readings "
                "(sensor_id, sensor_name, ts_utc, temp_c, temp_f, humidity, battery_pct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sensor_id,
                    s.get("sensor_name"),
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    temp_c,
                    c_to_f(temp_c),
                    s.get("last_humidity"),
                    s.get("battery_pct"),
                ),
            )
            stored += 1
        except Exception as e:
            logger.error(f"Temp Stick store error ({sensor_id}): {e}")
    conn.commit()
    conn.close()

    try:
        _evaluate_alerts(sensors)
    except Exception as e:
        logger.error(f"Temp Stick alert evaluation failed: {e}")

    logger.info(f"Temp Stick poll OK — {len(sensors)} sensor(s)")


# ------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------

def _evaluate_alerts(sensors):
    threshold = float(get_setting("tempstick_alert_threshold_f", DEFAULT_THRESHOLD_F))
    state = _get_alert_state()
    now = datetime.now(timezone.utc)
    changed = False

    conn = get_connection()
    for s in sensors:
        sensor_id = str(s.get("sensor_id") or "")
        if not sensor_id:
            continue
        name = s.get("sensor_name") or f"Sensor {sensor_id}"
        st = state.setdefault(sensor_id, {})
        temp_f = c_to_f(s.get("last_temp"))
        last_checkin = _parse_ts(s.get("last_checkin"))

        # --- Offline check ---
        offline_hours = (now - last_checkin).total_seconds() / 3600 if last_checkin else None
        if offline_hours is not None and offline_hours > OFFLINE_ALERT_HOURS:
            if not st.get("offline_alerted"):
                _send_alert_email(
                    f"[Red Nun] {name} sensor OFFLINE",
                    f"<p><b>{name}</b> hasn't reported in "
                    f"{offline_hours:.1f} hours (last checkin "
                    f"{last_checkin.strftime('%m/%d %I:%M %p')} UTC).</p>"
                    f"<p>Check WiFi / batteries — the freezer is NOT being monitored right now.</p>",
                )
                st["offline_alerted"] = True
                changed = True
            continue  # stale temp — skip warm/recovery logic
        elif st.get("offline_alerted"):
            st["offline_alerted"] = False
            changed = True

        if temp_f is None:
            continue

        # --- Warm check: last N stored readings all above threshold, or one big spike ---
        rows = conn.execute(
            "SELECT temp_f FROM tempstick_readings WHERE sensor_id = ? "
            "ORDER BY ts_utc DESC LIMIT ?",
            (sensor_id, CONSECUTIVE_WARM_TO_ALERT),
        ).fetchall()
        recent = [r["temp_f"] for r in rows if r["temp_f"] is not None]
        sustained_warm = (
            len(recent) >= CONSECUTIVE_WARM_TO_ALERT
            and all(t > threshold for t in recent)
        )
        spike = temp_f > threshold + SPIKE_MARGIN_F
        warm = sustained_warm or spike

        if warm:
            last_alert = _parse_ts(st.get("last_warm_alert"))
            hours_since = (now - last_alert).total_seconds() / 3600 if last_alert else None
            if not st.get("warm_active") or hours_since is None or hours_since >= REALERT_HOURS:
                _send_alert_email(
                    f"[Red Nun] WARNING: {name} at {round(temp_f)}°F",
                    f"<p><b>{name}</b> is reading <b>{round(temp_f)}&deg;F</b> "
                    f"(alert threshold {threshold:.0f}&deg;F).</p>"
                    f"<p>Recent readings: {', '.join(f'{t:.0f}&deg;' for t in recent)}</p>"
                    f"<p>Check the compressor and make sure the door is shut.</p>",
                )
                st["warm_active"] = True
                st["last_warm_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
                changed = True
        elif st.get("warm_active") and temp_f <= threshold - RECOVERY_MARGIN_F:
            _send_alert_email(
                f"[Red Nun] OK: {name} back to {round(temp_f)}°F",
                f"<p><b>{name}</b> has recovered — now reading "
                f"<b>{round(temp_f)}&deg;F</b>.</p>",
            )
            st["warm_active"] = False
            changed = True
    conn.close()

    if changed:
        _save_alert_state(state)


def _send_alert_email(subject, html_body):
    """Same SMTP setup as the morning report."""
    from_addr = os.getenv("REPORT_FROM_EMAIL", "dashboard@rednun.com")
    to_addr = os.getenv("REPORT_TO_EMAIL", "mgiorgio@rednun.com")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info(f"Temp Stick alert sent: {subject}")
    except Exception as e:
        logger.error(f"Temp Stick alert email failed: {e}")


# ------------------------------------------------------------------
# Dashboard API helpers
# ------------------------------------------------------------------

def get_freezer_data(hours=24):
    """Latest reading + history per sensor for the dashboard card."""
    _ensure_tables()
    api_key = get_api_key()
    threshold = float(get_setting("tempstick_alert_threshold_f", DEFAULT_THRESHOLD_F))
    if not api_key:
        return {"configured": False, "sensors": [], "threshold_f": threshold}

    conn = get_connection()
    sensor_rows = conn.execute(
        "SELECT sensor_id, MAX(ts_utc) AS ts_utc FROM tempstick_readings GROUP BY sensor_id"
    ).fetchall()

    now = datetime.now(timezone.utc)
    sensors = []
    for sr in sensor_rows:
        latest = conn.execute(
            "SELECT * FROM tempstick_readings WHERE sensor_id = ? AND ts_utc = ?",
            (sr["sensor_id"], sr["ts_utc"]),
        ).fetchone()
        if not latest:
            continue
        history = conn.execute(
            "SELECT ts_utc, temp_f, humidity FROM tempstick_readings "
            "WHERE sensor_id = ? AND ts_utc >= datetime('now', ?) "
            "ORDER BY ts_utc",
            (sr["sensor_id"], f"-{int(hours)} hours"),
        ).fetchall()
        ts = _parse_ts(latest["ts_utc"])
        minutes_ago = round((now - ts).total_seconds() / 60) if ts else None
        sensors.append({
            "sensor_id": latest["sensor_id"],
            "name": latest["sensor_name"] or f"Sensor {latest['sensor_id']}",
            "temp_f": latest["temp_f"],
            "humidity": latest["humidity"],
            "battery_pct": latest["battery_pct"],
            "last_checkin_utc": latest["ts_utc"],
            "minutes_ago": minutes_ago,
            "stale": minutes_ago is not None and minutes_ago > OFFLINE_ALERT_HOURS * 60,
            "warm": latest["temp_f"] is not None and latest["temp_f"] > threshold,
            "history": [
                {"t": h["ts_utc"], "f": h["temp_f"]} for h in history
                if h["temp_f"] is not None
            ],
        })
    conn.close()
    return {"configured": True, "sensors": sensors, "threshold_f": threshold}


def get_tempstick_settings():
    key = get_api_key()
    return {
        "configured": bool(key),
        "api_key_masked": (key[:4] + "…" + key[-4:]) if key and len(key) > 8 else None,
        "threshold_f": float(get_setting("tempstick_alert_threshold_f", DEFAULT_THRESHOLD_F)),
    }


def save_tempstick_settings(body):
    api_key = (body.get("api_key") or "").strip()
    if api_key:
        # validate before saving
        try:
            sensors = _fetch_sensors(api_key)
        except Exception as e:
            return {"ok": False, "error": f"API key rejected by Temp Stick: {e}"}
        set_setting("tempstick_api_key", api_key)
        logger.info(f"Temp Stick API key saved ({len(sensors)} sensor(s) visible)")
    if body.get("threshold_f") is not None:
        try:
            set_setting("tempstick_alert_threshold_f", float(body["threshold_f"]))
        except (TypeError, ValueError):
            return {"ok": False, "error": "threshold_f must be a number"}
    return {"ok": True, **get_tempstick_settings()}
