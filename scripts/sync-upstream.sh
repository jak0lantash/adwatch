#!/bin/sh
# Merge the latest adwatch core into this fork, then run the tests.
# Forks only add files under fork/, so core updates merge without conflicts unless a fork
# changed core files. Usage: scripts/sync-upstream.sh   (then review and push)
set -eu
UPSTREAM=${ADWATCH_UPSTREAM:-https://github.com/jak0lantash/adwatch}
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM"
git fetch upstream main
echo "New core commits:"
git log --oneline HEAD..upstream/main || true
git merge --no-edit upstream/main
if command -v pytest >/dev/null 2>&1; then python3 -m pytest -q; else echo "(pytest not installed: tests skipped)"; fi
echo "Merged. Review, then: git push"
