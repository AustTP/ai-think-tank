#!/bin/bash
# Push main without typing the GitHub token. The HTTPS remote has no stored
# credentials (osxkeychain has none for it), so a plain `git push` fails with
# "could not read Username". This pulls GITHUB_TOKEN out of the local .env
# (line-grep, NOT `. .env` -- SEED_ROSTER contains pipes that bash would
# misparse as a pipeline) and pushes with the token in the URL:
#
#   git -c credential.helper= push "https://x-access-token:${GITHUB_TOKEN}@github.com/AustTP/ai-village.git" main
#
# Usage: ./scripts/push.sh [branch]   (defaults to main)
set -euo pipefail
cd "$(dirname "$0")/.."
GITHUB_TOKEN="$(grep -E '^GITHUB_TOKEN=' .env | head -1 | cut -d= -f2-)"
if [ -z "$GITHUB_TOKEN" ]; then
    echo "GITHUB_TOKEN is empty in .env -- nothing to push with." >&2
    exit 1
fi
BRANCH="${1:-main}"
git -c credential.helper= push "https://x-access-token:${GITHUB_TOKEN}@github.com/AustTP/ai-village.git" "$BRANCH"