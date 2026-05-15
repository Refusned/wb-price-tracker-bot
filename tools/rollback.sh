#!/usr/bin/env bash
# Rollback the bot DB to the most recent backup.
#
# Usage:
#   ./tools/rollback.sh                   # restores newest backup in data/
#   ./tools/rollback.sh data/app.db.bak-build-20260515T143012Z   # specific
#
# Behavior:
#   1. Stop bot (docker-compose stop)
#   2. Move current data/app.db → data/app.db.pre-rollback-<ts> (in case rollback is wrong too)
#   3. Copy specified-or-newest backup → data/app.db
#   4. Start bot (docker-compose up -d)
#   5. Show last 30 log lines
#
# Safety: this script does NOT delete anything. The "current" DB is renamed,
# not removed — you can swap back if rollback was wrong.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DB="data/app.db"

# Choose backup
if [[ $# -ge 1 ]]; then
    BACKUP="$1"
    if [[ ! -f "$BACKUP" ]]; then
        echo "ERROR: backup not found: $BACKUP" >&2
        exit 1
    fi
else
    BACKUP=$(ls -1t data/app.db.bak-* 2>/dev/null | head -1 || true)
    if [[ -z "$BACKUP" ]]; then
        echo "ERROR: no data/app.db.bak-* found. Either backup-first or pass path explicitly." >&2
        exit 1
    fi
fi

TS=$(date +%Y%m%dT%H%M%SZ)
PRE_ROLLBACK="data/app.db.pre-rollback-$TS"

echo ">>> Rollback plan:"
echo "    Current DB:   $DB  (will be moved to $PRE_ROLLBACK)"
echo "    Restore from: $BACKUP"
echo ""
read -r -p "Proceed? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

# Stop bot (BEFORE moving WAL sidecars).
# Codex review fix #1: WAL mode keeps committed frames in .db-wal until
# checkpoint. If we restore .db without handling .db-wal/.db-shm sidecars,
# SQLite either rejects opens (sidecar mismatch) or replays stale WAL into
# the restored DB → silent corruption.
if command -v systemctl >/dev/null 2>&1 && systemctl list-units --type=service | grep -q wb-bot.service; then
    echo ">>> Stopping wb-bot.service..."
    systemctl stop wb-bot.service || true
elif command -v docker-compose >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
    echo ">>> Stopping bot via docker-compose..."
    docker-compose stop || true
else
    echo ">>> WARNING: no bot service manager detected. Make sure the bot is stopped before continuing."
    read -r -p "Bot is stopped? [y/N] " stopped
    [[ "$stopped" != "y" && "$stopped" != "Y" ]] && { echo "Aborted."; exit 1; }
fi
sleep 1  # let writers flush

# Move current DB + WAL sidecars TOGETHER so the trio stays consistent.
if [[ -f "$DB" ]]; then
    echo ">>> Moving current DB trio aside (.db, .db-wal, .db-shm) → ${PRE_ROLLBACK}*"
    mv "$DB" "$PRE_ROLLBACK"
    [[ -f "$DB-wal" ]] && mv "$DB-wal" "$PRE_ROLLBACK-wal"
    [[ -f "$DB-shm" ]] && mv "$DB-shm" "$PRE_ROLLBACK-shm"
fi

# Restore. The backup was created via VACUUM INTO (checkpointed) or by
# stopping the bot first, so it has no WAL sidecars and is consistent.
# Any stale -wal/-shm next to it would be corrupting, so remove first.
echo ">>> Removing any stale WAL sidecars for the new $DB..."
rm -f "$DB-wal" "$DB-shm"
echo ">>> Restoring: $BACKUP → $DB"
cp "$BACKUP" "$DB"

# Start
if command -v docker-compose >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
    echo ">>> Starting bot..."
    docker-compose up -d
    sleep 3
    echo ">>> Recent logs:"
    docker-compose logs --tail=30 || true
else
    echo ">>> Bot must be started manually (no docker-compose detected)."
fi

echo ""
echo "Rollback complete. Pre-rollback DB preserved at: $PRE_ROLLBACK"
echo "If rollback was wrong, restore via: mv $PRE_ROLLBACK $DB"
