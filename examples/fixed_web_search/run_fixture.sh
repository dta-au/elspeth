#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
ELSPETH_BIN="${ELSPETH_BIN:-.venv/bin/elspeth}"
EXAMPLE=examples/fixed_web_search
mkdir -p "$EXAMPLE/output" "$EXAMPLE/runs"

for artifact in "$EXAMPLE/runs/audit.db" "$EXAMPLE/runs/access.log" "$EXAMPLE/runs/server.log" "$EXAMPLE/runs/pipeline.out" "$EXAMPLE/runs/pipeline.err" "$EXAMPLE/output/results.jsonl" "$EXAMPLE/output/failures.jsonl"; do
    if [ -e "$artifact" ]; then
        echo "Refusing to overwrite existing $artifact" >&2
        exit 2
    fi
done

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
    "$PYTHON_BIN" "$EXAMPLE/serve_fixture.py" --access-log "$EXAMPLE/runs/access.log" \
        >> "$EXAMPLE/runs/server.log" 2>&1 &
    server_pid=$!
    for _ in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:8213/health > /dev/null 2>&1; then
            return
        fi
        if ! kill -0 "$server_pid" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    echo "Fixture server did not start; see $EXAMPLE/runs/server.log" >&2
    exit 1
}

start_server

set +e
"$ELSPETH_BIN" run --settings "$EXAMPLE/settings_fixture.yaml" --execute \
    > "$EXAMPLE/runs/pipeline.out" 2> "$EXAMPLE/runs/pipeline.err"
pipeline_exit=$?
set -e
printf 'pipeline_exit=%s\n' "$pipeline_exit"
if [ "$pipeline_exit" -ne 0 ]; then
    cat "$EXAMPLE/runs/pipeline.err" >&2
    exit "$pipeline_exit"
fi

"$PYTHON_BIN" - "$EXAMPLE" <<'PY'
import json
import pathlib
import sys

example = pathlib.Path(sys.argv[1])
rows = [json.loads(line) for line in (example / "output/results.jsonl").read_text().splitlines()]
requests = (example / "runs/access.log").read_text().splitlines()
assert len(rows) == 3, len(rows)
assert len(requests) == 3, requests
assert {row["search_text"] for row in rows} == set(requests)
assert all(row["fetch_status"] == 200 for row in rows)
assert sorted(len(row["candidates"]) for row in rows) == [0, 1, 2]
assert all(set(candidate) == {"name"} for row in rows for candidate in row["candidates"])
assert not (example / "output/failures.jsonl").exists()
print(f"verified_rows={len(rows)} verified_requests={len(requests)}")
PY

"$PYTHON_BIN" - "$EXAMPLE" <<'PY'
import pathlib
import sqlite3
import sys

example = pathlib.Path(sys.argv[1])
source_run_id = sqlite3.connect(example / "runs/audit.db").execute(
    "SELECT run_id FROM runs WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1"
).fetchone()[0]
settings = (example / "settings_fixture.yaml").read_text()
for mode in ("replay", "verify"):
    (example / f"runs/settings_{mode}.yaml").write_text(
        f'run_mode: {mode}\nreplay_from: "{source_run_id}"\n' + settings
    )
PY

stop_server
set +e
"$ELSPETH_BIN" run --settings "$EXAMPLE/runs/settings_replay.yaml" --execute \
    > "$EXAMPLE/runs/replay.out" 2> "$EXAMPLE/runs/replay.err"
replay_exit=$?
set -e
printf 'replay_exit=%s\n' "$replay_exit"
if [ "$replay_exit" -ne 0 ]; then
    cat "$EXAMPLE/runs/replay.err" >&2
    exit "$replay_exit"
fi

start_server
set +e
"$ELSPETH_BIN" run --settings "$EXAMPLE/runs/settings_verify.yaml" --execute \
    > "$EXAMPLE/runs/verify.out" 2> "$EXAMPLE/runs/verify.err"
verify_exit=$?
set -e
printf 'verify_exit=%s\n' "$verify_exit"
if [ "$verify_exit" -ne 0 ]; then
    cat "$EXAMPLE/runs/verify.err" >&2
    exit "$verify_exit"
fi

"$PYTHON_BIN" - "$EXAMPLE" <<'PY'
import pathlib
import sqlite3
import sys

example = pathlib.Path(sys.argv[1])
requests = (example / "runs/access.log").read_text().splitlines()
conn = sqlite3.connect(example / "runs/audit.db")
verdicts = conn.execute("SELECT COUNT(*), SUM(is_match) FROM call_verifications").fetchone()
assert len(requests) == 6, requests
assert verdicts == (3, 3), verdicts
print(f"verified_replay_without_network=3 verified_live_calls={verdicts[1]}")
PY
