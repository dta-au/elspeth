#!/usr/bin/env bash
# Pair chain over base..tip. The SHA list is DERIVED from the branch, because a
# hard-coded one went stale at the rebase and would have silently measured the
# wrong commits.
set -u
X=.claude/worktrees/u45-bisect-20261025
L=$X/.claude/lanes/u45-fix
BASE=d7ad18596
BRANCH=codex/0.8.10-u45-fix
export PYTHONPATH=$X/src:$X/elspeth-lints/src:$L
cd "$X" || exit 1
CANDIDATES=(
  tests/unit/web/sessions/test_continuation_composition_initialization.py
  tests/unit/contracts/test_prompt_guard_provenance_thaw.py
  tests/unit/web/catalog/test_schema_cache_isolation.py
)
mapfile -t SHAS < <(git log --format=%h --reverse "${BASE}~1..${BRANCH}")
echo "chain over ${#SHAS[@]} points: ${SHAS[*]}"
[ "${SHAS[0]}" = "$BASE" ] || { echo "FAIL: first point ${SHAS[0]} is not the base $BASE"; exit 2; }
for sha in "${SHAS[@]}"; do
  git checkout -q "$sha" 2>/dev/null
  paths=(); for p in "${CANDIDATES[@]}"; do [ -f "$p" ] && paths+=("$p"); done
  echo "===== $sha  (${#paths[@]} files collected) ====="
  U45_PAIRS_OUT="$L/p3-$sha.txt" timeout 2400 .venv/bin/python -m pytest "${paths[@]}" -n 0 -q -p u45pairs > "$L/p3-$sha.log" 2>&1
  echo "  exit=$? :: $(tail -1 "$L/p3-$sha.log")"
  echo "  ids collected: $(grep -oE '^[0-9]+ (passed|failed)|[0-9]+ passed' "$L/p3-$sha.log" | tail -1)"
  echo "  pairs: $(wc -l < "$L/p3-$sha.txt" 2>/dev/null || echo 0)"
  sed 's/^/    /' "$L/p3-$sha.txt" 2>/dev/null | cut -c1-185
done
echo
echo "===== transitions: every ADDED pair needs an explanation ====="
prev=""
for sha in "${SHAS[@]}"; do
  [ -f "$L/p3-$sha.txt" ] || : > "$L/p3-$sha.txt"
  if [ -n "$prev" ]; then
    echo "--- $prev -> $sha ---"
    echo "  removed:"; comm -23 "$L/p3-$prev.txt" "$L/p3-$sha.txt" | sed 's/^/    /' | cut -c1-185
    echo "  ADDED:";   comm -13 "$L/p3-$prev.txt" "$L/p3-$sha.txt" | sed 's/^/    /' | cut -c1-185
  fi
  prev=$sha
done
echo "===== ids introduced at the tip must pass there ====="
tail -3 "$L/p3-${SHAS[-1]}.log"
