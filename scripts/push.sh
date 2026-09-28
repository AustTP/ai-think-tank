#!/bin/bash
# Push main without typing the GitHub token. The HTTPS remote has no stored
# credentials (osxkeychain has none for it), so a plain `git push` fails with
# "could not read Username". This sources GITHUB_TOKEN from the local .env and
# pushes with the token in the URL, exactly like the documented one-liner:
#
#   git -c credential.helper= push "https://x-access-token:${GITHUB_TOKEN}@github.com/AustTP/ai-village.git" main
#
# Usage: ./scripts/push.sh [branch]   (defaults to main)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "GITHUB_TOKEN is empty in .env -- nothing to push with." >&2
    exit 1
fi
BRANCH="${1:-main}"
git -c credential.helper= push "https://x-access-token:${GITHUB_TOKEN}@github.com/AustTP/ai-village.git" "$BRANCH"