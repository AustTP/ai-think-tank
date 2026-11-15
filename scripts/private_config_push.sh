#!/bin/bash
# Sync the gitignored-but-valuable files from this tank tree into the
# private companion repo (config overlay). Paths: private_config_manifest.txt
# (relative to the tank root). Dirs mirror exactly (--delete); missing
# optional files are skipped. Run from anywhere; think-tank-private checkout
# location defaults to ~/ai-village-private.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
TANK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRIVATE_DIR="${THINK_TANK_PRIVATE_DIR:-$HOME/ai-village-private}"
MANIFEST="$TANK_ROOT/scripts/private_config_manifest.txt"
[ -d "$PRIVATE_DIR/.git" ] || { echo "private repo checkout not found at $PRIVATE_DIR (set THINK_TANK_PRIVATE_DIR)"; exit 1; }
FILTERED="$(mktemp)"
trap 'rm -f "$FILTERED"' EXIT
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if [ ! -e "$TANK_ROOT/$p" ]; then
    echo "skip (absent): $p"
  elif [ -d "$TANK_ROOT/$p" ]; then
    mkdir -p "$PRIVATE_DIR/$p"
    rsync -a --delete "$TANK_ROOT/$p/" "$PRIVATE_DIR/$p/"
  else
    mkdir -p "$PRIVATE_DIR/$(dirname "$p")"
    cp "$TANK_ROOT/$p" "$PRIVATE_DIR/$p"
  fi
done < "$MANIFEST"
cd "$PRIVATE_DIR"
git add -A
if git diff --cached --quiet; then echo "no changes to sync"; exit 0; fi
git commit -m "config sync $(date '+%Y-%m-%d %H:%M')"
git push origin main
echo "synced -> $PRIVATE_DIR"
