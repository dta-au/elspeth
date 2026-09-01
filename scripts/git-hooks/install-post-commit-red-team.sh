#!/usr/bin/env bash
# Install the red-team post-commit trigger as a managed block in
# .git/hooks/post-commit, following the same managed-block convention as the
# Loomweave hook. Idempotent: re-running replaces the existing block.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="$(git rev-parse --git-path hooks)/post-commit"
BEGIN="# BEGIN ELSPETH RED-TEAM MANAGED BLOCK"
END="# END ELSPETH RED-TEAM MANAGED BLOCK"

if [[ ! -f "$HOOK" ]]; then
    printf '#!/bin/sh\n' >"$HOOK"
fi

# Drop any existing managed block, then append the current one before any
# trailing `exit 0` line would swallow it — so strip a bare trailing exit 0
# and re-add it after our block.
TMP="$(mktemp)"
awk -v begin="$BEGIN" -v end="$END" '
    $0 == begin {skip = 1; next}
    $0 == end {skip = 0; next}
    !skip {print}
' "$HOOK" >"$TMP"

# Remove a single trailing "exit 0" (with optional surrounding blank lines).
python3 - "$TMP" <<'PY'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    lines = handle.read().splitlines()
while lines and not lines[-1].strip():
    lines.pop()
had_exit = bool(lines) and lines[-1].strip() == "exit 0"
if had_exit:
    lines.pop()
with open(path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
PY

{
    cat "$TMP"
    echo "$BEGIN"
    echo "# Managed by scripts/git-hooks/install-post-commit-red-team.sh."
    echo "# Fail-soft: the red-team trigger must never block git."
    echo "\"${REPO_ROOT}/scripts/git-hooks/post-commit-red-team.sh\" || true"
    echo "$END"
    echo "exit 0"
} >"$HOOK"
rm -f "$TMP"
chmod +x "$HOOK"
echo "Installed red-team managed block into $HOOK"
