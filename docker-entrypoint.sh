#!/bin/sh
# Run as PUID/PGID (recommended: the owner of the media folder), otherwise as root.
set -e
if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}" ]; then
  PGID="${PGID:-$PUID}"
  DB="${DB_PATH:-/data/mediascan.db}"
  mkdir -p "$(dirname "$DB")"
  # hand the database and work folders of an earlier (root) install over to PUID/PGID
  chown "$PUID:$PGID" "$(dirname "$DB")" "$DB" "$DB-wal" "$DB-shm" 2>/dev/null || true
  [ -d "${MEDIA_ROOT:-/media}/_MediaScan" ] && chown -R "$PUID:$PGID" "${MEDIA_ROOT:-/media}/_MediaScan" 2>/dev/null || true
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs "$@"
fi
exec "$@"
