#!/usr/bin/env bash
# Daily SQLite backup. Runs from /etc/cron.d/workout-app-backup as user
# `workout`. Keeps 30 days of compressed snapshots in /opt/workout-app/backups.
#
# SQLite's online backup avoids torn reads even if the app is mid-write.

set -euo pipefail

APP_DIR="/opt/workout-app"
DB="$APP_DIR/data/gym.db"
DEST="$APP_DIR/backups"
TS="$(date -u +%Y%m%d_%H%M%S)"
RETAIN_DAYS=30

mkdir -p "$DEST"

if [[ ! -f "$DB" ]]; then
    echo "[backup $TS] no db at $DB — skipping" >&2
    exit 0
fi

OUT="$DEST/gym-$TS.db"
sqlite3 "$DB" ".backup '$OUT'"
gzip --force "$OUT"

# Prune old snapshots
find "$DEST" -name 'gym-*.db.gz' -mtime "+$RETAIN_DAYS" -delete

echo "[backup $TS] wrote $OUT.gz"
