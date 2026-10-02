#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src${PYTHONPATH:+:$PYTHONPATH}"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
EXAMPLE=examples/fixed_web_search
RUNS="$EXAMPLE/runs_detail"
OUTPUT="$EXAMPLE/output_detail"
SETTINGS="$EXAMPLE/settings_fixture_detail.yaml"
PORT=8215

run_elspeth() {
    "$PYTHON_BIN" -c 'from elspeth.cli import app; app()' "$@"
}

if curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    echo "Refusing to use an existing server on port $PORT" >&2
    exit 2
fi
for artifact in "$RUNS" "$OUTPUT"; do
    if [ -e "$artifact" ]; then
        echo "Refusing to overwrite existing $artifact" >&2
        exit 2
    fi
done
mkdir -p "$RUNS" "$OUTPUT"

server_pid=""
stop_server() {
    if [ -n "$server_pid" ]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
        server_pid=""
    fi
}
trap stop_server EXIT

start_server() {
    "$PYTHON_BIN" "$EXAMPLE/serve_fixture.py" --port "$PORT" \
        --access-log "$RUNS/access.log" --wire-log "$RUNS/wire.log" \
        >> "$RUNS/server.log" 2>&1 &
    server_pid=$!
    for _ in $(seq 1 30); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
            echo "Fixture server exited; see $RUNS/server.log" >&2
            exit 1
        fi
        if curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
            return
        fi
        sleep 0.2
    done
    echo "Fixture server did not become ready; see $RUNS/server.log" >&2
    exit 1
}

start_server
if ! run_elspeth run --settings "$SETTINGS" --execute > "$RUNS/pipeline.out" 2> "$RUNS/pipeline.err"; then
    cat "$RUNS/pipeline.err" >&2
    exit 1
fi

"$PYTHON_BIN" - "$RUNS" "$OUTPUT" "$SETTINGS" <<'PY'
import json
import pathlib
import sqlite3
import sys

runs, output, settings_path = map(pathlib.Path, sys.argv[1:])
def rows(name: str) -> list[dict]:
    path = output / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]

one = rows("one_candidate")
ambiguous = rows("ambiguous_candidates")
none = rows("no_results")
assert not (output / "failures.jsonl").exists()
assert len(one) == 1 and len(ambiguous) == 2 and len(none) == 1
assert (one[0]["source_id"], one[0]["candidate_count"], one[0]["registry_id"]) == (1, 1, "RC-001")
assert {row["source_id"] for row in ambiguous} == {2}
assert {row["candidate_count"] for row in ambiguous} == {2}
assert {row["registry_id"] for row in ambiguous} == {"NCT-101", "NCT-102"}
assert {row["candidate_index"] for row in ambiguous} == {0, 1}
assert all(row["detail_url"].startswith("http://127.0.0.1:8215/entity/") for row in one + ambiguous)
assert all(row["detail_fingerprint"] and row["search_fingerprint"] for row in one + ambiguous)
assert all(row["candidate_name"] == row["legal_name"] for row in one + ambiguous)
assert all(row["detail_url"].endswith("/" + row["registry_id"]) for row in one + ambiguous)
assert {row["registry_id"]: row["record_status"] for row in one + ambiguous} == {
    "RC-001": "active", "NCT-101": "active", "NCT-102": "inactive"
}
assert none[0]["id"] == 3 and none[0]["candidate_count"] == 0 and none[0]["candidates"] == []
expected_access = {
    "search:Riverdale Council",
    "search:North Coast Trading",
    "search:Unknown Entity",
    "detail:RC-001",
    "detail:NCT-101",
    "detail:NCT-102",
}
access = (runs / "access.log").read_text().splitlines()
assert len(access) == 6 and set(access) == expected_access, access

conn = sqlite3.connect(runs / "audit.db")
run = conn.execute("SELECT run_id, status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
assert run[1] == "completed", run
source_run_id = run[0]

# Every terminal row retains its source row ID through both expansions. The
# ambiguous pair must share one source ancestor while carrying distinct tokens.
roots = {
    row_id: token_id
    for row_id, token_id in conn.execute(
        "SELECT t.row_id, t.token_id FROM tokens t "
        "JOIN node_states n ON n.token_id=t.token_id AND n.run_id=t.run_id "
        "WHERE t.run_id=? AND n.node_id LIKE 'source_%'", (source_run_id,)
    )
}
assert len(roots) == 3, roots
parents = dict(conn.execute("SELECT token_id, parent_token_id FROM token_parents WHERE run_id=?", (source_run_id,)))
outcomes = list(conn.execute(
    "SELECT o.sink_name, t.row_id, t.token_id FROM token_outcomes o "
    "JOIN tokens t ON t.token_id=o.token_id AND t.run_id=o.run_id "
    "WHERE o.run_id=? AND o.outcome='success'", (source_run_id,)
))
assert sorted(sink for sink, _, _ in outcomes) == [
    "ambiguous_candidates", "ambiguous_candidates", "no_results", "one_candidate"
]
assert len({row_id for _, row_id, _ in outcomes}) == 3
ambiguous_lineage = {row_id for sink, row_id, _ in outcomes if sink == "ambiguous_candidates"}
assert len(ambiguous_lineage) == 1, ambiguous_lineage
for _, row_id, token_id in outcomes:
    visited = set()
    while token_id in parents:
        assert token_id not in visited, "cyclic token lineage"
        visited.add(token_id)
        token_id = parents[token_id]
    assert token_id == roots[row_id], (row_id, token_id)
    assert all(token_id != root for other_row_id, root in roots.items() if other_row_id != row_id)

settings = settings_path.read_text()
for mode in ("replay", "verify"):
    (runs / f"settings_{mode}.yaml").write_text(
        f'run_mode: {mode}\nreplay_from: "{source_run_id}"\n' + settings
    )
print("live: 3 search requests, 3 detail requests, 1 unique, 2 ambiguous, 1 no-result")
PY

stop_server
if ! run_elspeth run --settings "$RUNS/settings_replay.yaml" --execute > "$RUNS/replay.out" 2> "$RUNS/replay.err"; then
    cat "$RUNS/replay.err" >&2
    exit 1
fi
"$PYTHON_BIN" - "$RUNS" <<'PY'
import pathlib
import sys

runs = pathlib.Path(sys.argv[1])
assert len((runs / "access.log").read_text().splitlines()) == 6, "replay accessed the network"
PY

start_server
if ! run_elspeth run --settings "$RUNS/settings_verify.yaml" --execute > "$RUNS/verify.out" 2> "$RUNS/verify.err"; then
    cat "$RUNS/verify.err" >&2
    exit 1
fi

"$PYTHON_BIN" - "$RUNS" <<'PY'
import pathlib
import sqlite3
import sys

runs = pathlib.Path(sys.argv[1])
access = (runs / "access.log").read_text().splitlines()
assert len(access) == 12, access
assert sorted(access[:6]) == sorted(access[6:]), access
conn = sqlite3.connect(runs / "audit.db")
statuses = [row[0] for row in conn.execute("SELECT status FROM runs ORDER BY started_at")]
assert statuses == ["completed"] * 3, statuses
verdicts = conn.execute("SELECT COUNT(*), SUM(is_match) FROM call_verifications").fetchone()
assert verdicts == (6, 6), verdicts
replayed = conn.execute("SELECT COUNT(*) FROM calls WHERE source_call_id IS NOT NULL").fetchone()[0]
assert replayed == 6, replayed
print(f"replay: 6 calls without network; verify: {verdicts[1]} matching calls")
PY
