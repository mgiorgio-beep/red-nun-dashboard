#!/usr/bin/env bash
# 04_telemetry_drop_v2.sh
# Drop-in replacement for /opt/red-nun-dashboard/scripts/telemetry_drop.sh.
# Fixes:
#   - journalctl filter (-u 'gunicorn*' matched zero units; we need
#     --identifier=gunicorn to match the syslog identifier)
#   - now also captures the rednun.service journal directly as a fallback
#
# Install on the Beelink:
#   sudo cp 04_telemetry_drop_v2.sh /opt/red-nun-dashboard/scripts/telemetry_drop.sh
#   sudo chmod +x /opt/red-nun-dashboard/scripts/telemetry_drop.sh
#   /opt/red-nun-dashboard/scripts/telemetry_drop.sh   # test once
#   cat ~/cowork/red-nun-dashboard/_telemetry/last_run.txt
#   wc -l ~/cowork/red-nun-dashboard/_telemetry/journal_payments.log
#
# Existing cron entry (*/5 * * * *) will pick up the new script automatically.

set -uo pipefail
OUT=/home/rednun/cowork/red-nun-dashboard/_telemetry
mkdir -p "$OUT" "$OUT/scraper_screenshots"
TS=$(date -Iseconds)

# ─── 1. Rolling 24h of relevant journal lines ──────────────────────────────────
# Both filters: rednun.service (the unit) and identifier=gunicorn (process name).
{
  sudo -n journalctl --since "24 hours ago" -u rednun.service --no-pager 2>/dev/null
  sudo -n journalctl --since "24 hours ago" --identifier=gunicorn --no-pager 2>/dev/null
} | grep -Ei 'payment|portal|scraper|chatham|dennis|invoice|error|warning|fail' \
  | sort -u \
  > "$OUT/journal_payments.log" || true

# ─── 2. vendor_payments snapshot ──────────────────────────────────────────────
DB="${DB_PATH:-/var/lib/rednun/toast_data.db}"
if [ -f "$DB" ] && sqlite3 "$DB" "SELECT 1 FROM vendor_payments LIMIT 1;" >/dev/null 2>&1; then
  sqlite3 "$DB" -header -csv \
    "SELECT id, vendor, location, payment_date, payment_total, status,
            payment_ref, substr(memo, 1, 200) AS memo_trunc,
            substr(COALESCE(error_detail,''), 1, 400) AS error_detail_trunc,
            created_at, updated_at
     FROM vendor_payments
     ORDER BY id DESC
     LIMIT 200;" \
    > "$OUT/vendor_payments_recent.csv"
  echo "$DB" > "$OUT/db_used.txt"
fi

# ─── 3. Recent scraper artifacts (last 24h) ───────────────────────────────────
# Copies any new screenshots into the Drive folder so they're viewable from chat.
find "$HOME/vendor-scrapers"/*/screenshots -type f -mtime -1 2>/dev/null \
  | while read -r f; do
      base=$(basename "$f")
      # Avoid re-copying if it's already there
      [ -f "$OUT/scraper_screenshots/$base" ] || cp -p "$f" "$OUT/scraper_screenshots/" 2>/dev/null || true
    done

# Prune screenshots older than 7 days from the Drive copy
find "$OUT/scraper_screenshots" -type f -mtime +7 -delete 2>/dev/null || true

# ─── 4. Per-payment logs (last 24h) ───────────────────────────────────────────
mkdir -p "$OUT/payment_logs"
find "$HOME/vendor-scrapers/logs" -maxdepth 1 -type f -name 'payment_*_vp*.log' -mtime -1 2>/dev/null \
  | while read -r f; do
      base=$(basename "$f")
      cp -p "$f" "$OUT/payment_logs/" 2>/dev/null || true
    done
find "$OUT/payment_logs" -type f -mtime +7 -delete 2>/dev/null || true

# ─── 5. Health summary ─────────────────────────────────────────────────────────
{
  echo "last_run: $TS"
  echo "db: $DB ($(stat -c %s "$DB" 2>/dev/null) bytes)"
  echo "vendor_payments_count: $(sqlite3 "$DB" 'SELECT COUNT(*) FROM vendor_payments;' 2>/dev/null)"
  echo "failed_last_24h: $(sqlite3 "$DB" "SELECT COUNT(*) FROM vendor_payments WHERE status='failed' AND created_at > datetime('now', '-24 hours');" 2>/dev/null)"
  echo "journal_lines_captured: $(wc -l < "$OUT/journal_payments.log" 2>/dev/null)"
} > "$OUT/health.txt"

echo "$TS" > "$OUT/last_run.txt"
