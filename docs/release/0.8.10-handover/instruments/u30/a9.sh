#!/usr/bin/env bash
# A9 instruments for U-30 (master plan :54). Usage: a9.sh <label>. Writes one log and exit code per instrument.
set -u
W=.claude/worktrees/u30-20261025
L=$W/.claude/lanes/u30/a9-$1
mkdir -p "$L"
cd "$W" || exit 2
PY="env PYTHONPATH=$W/src:$W/elspeth-lints/src .venv/bin/python"
git rev-parse HEAD > "$L/head.txt"; git status --short | grep -v '^??' > "$L/dirty.txt"
timeout 2400 $PY -m pytest tests/unit/web/sessions/test_composer_real_provider_transitions.py -n 0 -p no:cacheprovider -v > "$L/census.log" 2>&1; echo "census=$?" > "$L/exit.txt"
timeout 2400 $PY -m pytest tests/integration/web/test_tutorial_routes.py -n 3 -p no:cacheprovider -q > "$L/tutorial_routes.log" 2>&1; echo "tutorial_routes=$?" >> "$L/exit.txt"
timeout 2400 $PY -m pytest "tests/unit/web/sessions/test_routes.py::test_accept_refuses_server_provider_proposal_provenance" -n 0 -p no:cacheprovider -q > "$L/server_rejection.log" 2>&1; echo "server_rejection=$?" >> "$L/exit.txt"
FE=$W/src/elspeth/web/frontend
created_link=0
if [ ! -e "$FE/node_modules" ]; then ln -s src/elspeth/web/frontend/node_modules "$FE/node_modules"; created_link=1; fi
(cd "$FE" && npx vitest run tests/e2e/harness/transition-ledger.test.ts > "$L/transition_ledger.log" 2>&1); echo "transition_ledger=$?" >> "$L/exit.txt"
[ "$created_link" = 1 ] && rm "$FE/node_modules"
echo "tutorial_spec=NOT_RUN (webServer uses uv run; unsafe in a linked-.venv worktree)" >> "$L/exit.txt"
git grep -c 'provider=.server' -- src | awk -F: '{s+=$2} END {print "provider_server_src_count=" s+0}' >> "$L/exit.txt"
grep -oE '::[A-Za-z0-9_]+(\[[a-z_]+\])? (PASSED|FAILED)' "$L/census.log" > "$L/census-ids.txt"
touch "$L/.done"
