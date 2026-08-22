#!/usr/bin/env bash
# 05_backup_toast_db.sh
# Nightly offsite backup of /var/lib/rednun/toast_data.db.
# - Uses sqlite3 .backup (safe with the DB open and being written to)
# - Compresses with zstd (1.3 GB -> ~150-200 MB typically for this workload)
# - Drops the artifact into the Drive folder (rclone-synced to G:\My Drive\Red NUn Dashboard)
# - Keeps a rolling 7 daily + 4 weekly retention
#
# Install on the Beelink:
#   sudo cp 05_backup_toast_db.sh /opt/red-nun-dashboard/scripts/backup_toast_db.sh
#   sudo chmod +x /opt/red-nun-dashboard/scripts/backup_toast_db.sh
#   # Test
#   /opt/red-nun-dashboard/scripts/backup_toast_db.sh
#   # Add to cron (3:15am daily — well outside business hours)
#   ( crontab -l 2>/dev/null; echo "15 3 * * * /opt/red-nun-dashboard/scripts/backup_toast_db.sh >> /home/rednun/cowork/red-nun-dashboard/_backups/backup.log 2>&1" ) | crontab -

set -uo pipefail

SRC=/var/lib/rednun/toast_data.db
DEST_DIR=/home/rednun/cowork/red-nun-dashboard/_backups
DAILY_DIR="$DEST_DIR/daily"
WEEKLY_DIR="$DEST_DIR/weekly"
mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

STAMP=$(date +%Y%m%d_%H%M%S)
DOW=$(date +%u)   # 1=Mon … 7=Sun. We mirror Sunday backup as the weekly anchor.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

LOG_PREFIX="[$(date -Iseconds)] backup_toast_db:"

echo "$LOG_PREFIX starting backup of $SRC"

# 1. Hot snapshot via sqlite3 .backup — safe with writers active
if ! sqlite3 "$SRC" ".backup '$TMP/toast_data.db'"; then
  echo "$LOG_PREFIX ERROR: sqlite3 .backup failed" >&2
  exit 1
fi

# 2. Integrity check on the snapshot
if ! sqlite3 "$TMP/toast_data.db" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "$LOG_PREFIX ERROR: integrity check failed on snapshot" >&2
  exit 2
fi

# 3. Compress (zstd if available, gzip fallback)
if command -v zstd >/dev/null 2>&1; then
  zstd -19 -T2 --quiet "$TMP/toast_data.db" -o "$TMP/toast_data.db.zst"
  ARTIFACT="toast_data_${STAMP}.db.zst"
  cp "$TMP/toast_data.db.zst" "$DAILY_DIR/$ARTIFACT"
else
  gzip -9 < "$TMP/toast_data.db" > "$TMP/toast_data.db.gz"
  ARTIFACT="toast_data_${STAMP}.db.gz"
  cp "$TMP/toast_data.db.gz" "$DAILY_DIR/$ARTIFACT"
fi

SIZE=$(stat -c %s "$DAILY_DIR/$ARTIFACT" 2>/dev/null)
echo "$LOG_PREFIX wrote $DAILY_DIR/$ARTIFACT ($SIZE bytes)"

# 4. Sunday → also copy to weekly directory as the long-retention anchor
if [ "$DOW" = "7" ]; then
  cp "$DAILY_DIR/$ARTIFACT" "$WEEKLY_DIR/$ARTIFACT"
  echo "$LOG_PREFIX (Sunday) also wrote weekly anchor"
fi

# 5. Retention: keep 7 daily, 4 weekly
ls -1t "$DAILY_DIR"/toast_data_*.db.* 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$WEEKLY_DIR"/toast_data_*.db.* 2>/dev/null | tail -n +5 | xargs -r rm -f

# 6. Emit a simple status file the telemetry script (or a healthcheck) can read
{
  echo "last_backup: $(date -Iseconds)"
  echo "artifact: $DAILY_DIR/$ARTIFACT"
  echo "size_bytes: $SIZE"
  echo "src_size_bytes: $(stat -c %s "$SRC" 2>/dev/null)"
  echo "daily_count: $(ls -1 "$DAILY_DIR"/toast_data_*.db.* 2>/dev/null | wc -l)"
  echo "weekly_count: $(ls -1 "$WEEKLY_DIR"/toast_data_*.db.* 2>/dev/null | wc -l)"
} > "$DEST_DIR/last_backup.txt"

echo "$LOG_PREFIX done"

# ─── NOTES ────────────────────────────────────────────────────────────────────
# - Drive sync (rclone) carries these into G:\My Drive\Red NUn Dashboard\_backups\
#   on Mike's PC, giving offsite protection if the Beelink dies.
# - Compressed at level 19 (zstd) — tunable. Daily 1.3 GB → ~150 MB; 7 daily +
#   4 weekly ≈ 1.5 GB on the Drive, well within Drive limits.
# - To restore: zstd -d toast_data_YYYYMMDD_HHMMSS.db.zst -o restored.db,
#   then PRAGMA integrity_check; on it before swapping in.
