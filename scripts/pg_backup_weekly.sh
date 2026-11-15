#!/bin/bash
# Weekly compressed Aiven backup. pg_dump (custom format) -> think-tank-db-backups/,
# then upload as a GitHub Release asset in the private companion repo
# (2 GB per asset; git objects would bust GitHub's 100 MB file limit and bloat
# history). Each week's dump REPLACES the previous one: only the newest
# release and dump are kept (KEEP=1).
# Restore:
#   gh release download db-backup-YYYYMMDD -R AustTP/think-tank-private -p '*.dump' -D /tmp
#   pg_restore --no-owner --no-privileges -j 4 -d "$THINK_TANK_DB_URL" /tmp/pg-YYYYMMDD.dump
# Schedule: systemd timer think-tank-db-backup on the VM (weekly, Sunday
# 03:00 America/New_York). The VM runs pg_dump 18 via Docker (the distro
# client is too old for Aiven's PostgreSQL 18 server):
#   PG_DUMP_BIN="$HOME/bin/pg_dump18" bash scripts/pg_backup_weekly.sh
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"
TANK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRIVATE_REPO="${THINK_TANK_PRIVATE_REPO:-AustTP/think-tank-private}"
KEEP=1
DSN="$(grep -E '^THINK_TANK_DB_URL=' "$TANK_ROOT/.env" | head -1 | cut -d= -f2-)"
[ -n "$DSN" ] || { echo "no THINK_TANK_DB_URL in $TANK_ROOT/.env"; exit 1; }
STAMP="$(date +%Y%m%d)"
BACKUPS="$TANK_ROOT/think-tank-db-backups"
mkdir -p "$BACKUPS"
OUT="$BACKUPS/pg-$STAMP.dump"
echo "[$(date '+%F %T')] dumping $DSN -> $OUT (using $PG_DUMP_BIN)"
"$PG_DUMP_BIN" --format=custom --no-owner --no-privileges --no-comments -d "$DSN" > "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUT" > "$OUT.sha256"
else
  shasum -a 256 "$OUT" > "$OUT.sha256"
fi
SIZE="$(du -h "$OUT" | cut -f1)"
echo "[$(date '+%F %T')] dump complete ($SIZE), uploading release"
gh release create "db-backup-$STAMP" "$OUT" "$OUT.sha256" -R "$PRIVATE_REPO" \
  --title "pg backup $STAMP" --notes "custom-format pg_dump ($SIZE); sha256 sidecar included"
set +e
OLD_TAGS="$(gh api "repos/$PRIVATE_REPO/releases" --paginate \
  -q '.[] | select(.tag_name | startswith("db-backup-")) | .tag_name' | tail -n +$((KEEP + 1)))"
for tag in $OLD_TAGS; do
  echo "pruning old release $tag"
  gh release delete "$tag" -R "$PRIVATE_REPO" --cleanup-tags --yes
done
ls -t "$BACKUPS"/pg-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
  rm -f "$old" "$old.sha256"
done
echo "[$(date '+%F %T')] backup finished: $OUT"
