#!/bin/bash
# Inverse of private_config_push.sh: copy the config overlay from the private
# repo checkout back into this tank tree. Used on a fresh VM / restored tree.
# Non-deleting by design: it overlays, it never removes tank-tree files.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
TANK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRIVATE_DIR="${THINK_TANK_PRIVATE_DIR:-$HOME/ai-village-private}"
MANIFEST="$TANK_ROOT/scripts/private_config_manifest.txt"
[ -d "$PRIVATE_DIR/.git" ] || { echo "private repo checkout not found at $PRIVATE_DIR (set THINK_TANK_PRIVATE_DIR)"; exit 1; }
while IFS= read -r p; do
  [ -n "$p" ] || continue
  [ -e "$PRIVATE_DIR/$p" ] || { echo "skip (absent in private): $p"; continue; }
  mkdir -p "$TANK_ROOT/$(dirname "$p")"
  if [ -d "$PRIVATE_DIR/$p" ]; then
    rsync -a "$PRIVATE_DIR/$p/" "$TANK_ROOT/$p/"
  else
    cp "$PRIVATE_DIR/$p" "$TANK_ROOT/$p"
  fi
done < "$MANIFEST"
chmod 600 "$TANK_ROOT/.env" 2>/dev/null || true
find "$TANK_ROOT/.secret_keys" -type f -exec chmod 600 {} + 2>/dev/null || true
echo "overlay restored into $TANK_ROOT"
