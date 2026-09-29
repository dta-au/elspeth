#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src${PYTHONPATH:+:$PYTHONPATH}"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
run_elspeth() {
    "$PYTHON_BIN" -c 'from elspeth.cli import app; app()' "$@"
}
EXAMPLE=examples/fixed_web_search
variant="${1:-directory}"
case "$variant" in
    directory) suffix="" ;;
    registry) suffix="_registry" ;;
    form) suffix="_form" ;;
    *) echo "Unknown fixture variant: $variant" >&2; exit 2 ;;
esac
RUNS="$EXAMPLE/runs$suffix"
OUTPUT="$EXAMPLE/output$suffix"
SETTINGS="$EXAMPLE/settings_fixture$suffix.yaml"
mkdir -p "$OUTPUT" "$RUNS"

for artifact in "$RUNS/audit.db" "$RUNS/access.log" "$RUNS/server.log" "$RUNS/pipeline.out" "$RUNS/pipeline.err" "$OUTPUT/results.jsonl" "$OUTPUT/failures.jsonl"; do
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
    "$PYTHON_BIN" "$EXAMPLE/serve_fixture.py" --access-log "$RUNS/access.log" \
        >> "$RUNS/server.log" 2>&1 &
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
    echo "Fixture server did not start; see $RUNS/server.log" >&2
    exit 1
}

start_server

set +e
run_elspeth run --settings "$SETTINGS" --execute \
    > "$RUNS/pipeline.out" 2> "$RUNS/pipeline.err"
pipeline_exit=$?
set -e
printf 'pipeline_exit=%s\n' "$pipeline_exit"
if [ "$pipeline_exit" -ne 0 ]; then
    cat "$RUNS/pipeline.err" >&2
    exit "$pipeline_exit"
fi

"$PYTHON_BIN" - "$RUNS" "$OUTPUT" "$variant" <<'PY'
import json
import pathlib
import sys

runs = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
variant = sys.argv[3]
rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
requests = (runs / "access.log").read_text().splitlines()
assert len(rows) == 3, len(rows)
assert len(requests) == 3, requests
assert {row["search_text"] for row in rows} == set(requests)
assert all(row["fetch_status"] == 200 for row in rows)
assert sorted(len(row["candidates"]) for row in rows) == [0, 1, 2]
candidate_fields = {"name"} if variant in {"directory", "form"} else {"name", "detail_path", "registration"}
assert all(set(candidate) == candidate_fields for row in rows for candidate in row["candidates"])
assert not (output / "failures.jsonl").exists()
print(f"verified_rows={len(rows)} verified_requests={len(requests)}")
PY

"$PYTHON_BIN" - "$RUNS" "$SETTINGS" <<'PY'
import pathlib
import sqlite3
import sys

runs = pathlib.Path(sys.argv[1])
settings_path = pathlib.Path(sys.argv[2])
source_run_id = sqlite3.connect(runs / "audit.db").execute(
    "SELECT run_id FROM runs WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1"
).fetchone()[0]
settings = settings_path.read_text()
for mode in ("replay", "verify"):
    (runs / f"settings_{mode}.yaml").write_text(
        f'run_mode: {mode}\nreplay_from: "{source_run_id}"\n' + settings
    )
PY

stop_server
set +e
run_elspeth run --settings "$RUNS/settings_replay.yaml" --execute \
    > "$RUNS/replay.out" 2> "$RUNS/replay.err"
replay_exit=$?
set -e
printf 'replay_exit=%s\n' "$replay_exit"
if [ "$replay_exit" -ne 0 ]; then
    cat "$RUNS/replay.err" >&2
    exit "$replay_exit"
fi

start_server
set +e
run_elspeth run --settings "$RUNS/settings_verify.yaml" --execute \
    > "$RUNS/verify.out" 2> "$RUNS/verify.err"
verify_exit=$?
set -e
printf 'verify_exit=%s\n' "$verify_exit"
if [ "$verify_exit" -ne 0 ]; then
    cat "$RUNS/verify.err" >&2
    exit "$verify_exit"
fi

"$PYTHON_BIN" - "$RUNS" "$variant" <<'PY'
import hashlib
import json
import pathlib
import sqlite3
import sys
import urllib.parse

runs = pathlib.Path(sys.argv[1])
variant = sys.argv[2]
requests = (runs / "access.log").read_text().splitlines()
conn = sqlite3.connect(runs / "audit.db")
verdicts = conn.execute("SELECT COUNT(*), SUM(is_match) FROM call_verifications").fetchone()
assert len(requests) == 6, requests
assert verdicts == (3, 3), verdicts
if variant == "form":
    refs = [row[0] for row in conn.execute("SELECT request_ref FROM calls WHERE call_type = 'http'")]
    assert len(refs) == 9, refs
    for ref in refs:
        request = json.loads((runs / "payloads" / ref[:2] / ref).read_text())
        form = request["form"]
        assert [pair[0] for pair in form] == ["q", "scope", "scope"], form
        assert [pair[1] for pair in form[1:]] == ["public", "active"], form
        assert request["body_encoding"] == "urlencoded-v1"
        encoded = urllib.parse.urlencode([tuple(pair) for pair in form]).encode("ascii")
        assert request["body_sha256"] == hashlib.sha256(encoded).hexdigest()
print(f"verified_replay_without_network=3 verified_live_calls={verdicts[1]}")
PY
