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
    multipart) suffix="_multipart" ;;
    *) echo "Unknown fixture variant: $variant" >&2; exit 2 ;;
esac
RUNS="$EXAMPLE/runs$suffix"
OUTPUT="$EXAMPLE/output$suffix"
SETTINGS="$EXAMPLE/settings_fixture$suffix.yaml"
mkdir -p "$OUTPUT" "$RUNS"

for artifact in "$RUNS/audit.db" "$RUNS/access.log" "$RUNS/wire.log" "$RUNS/server.log" "$RUNS/pipeline.out" "$RUNS/pipeline.err" "$OUTPUT/results.jsonl" "$OUTPUT/failures.jsonl"; do
    if [ -e "$artifact" ]; then
        echo "Refusing to overwrite existing $artifact" >&2
        exit 2
    fi
done

if [ "$variant" = multipart ]; then
    "$PYTHON_BIN" - "$RUNS" <<'PY'
import json
import pathlib
import sys

from elspeth.core.payload_store import FilesystemPayloadStore

runs = pathlib.Path(sys.argv[1])
ref = FilesystemPayloadStore(runs / "payloads").store(b"public-search-fixture")
rows = ["Australian Taxation Office", "Commonwealth Bank", "Café Not Found"]
with (runs / "input.jsonl").open("w", encoding="utf-8") as output:
    for index, query in enumerate(rows, start=1):
        output.write(json.dumps({
            "id": index,
            "fixture_id": str(index),
            "search_text": query,
            "search_parts": [
                {"name": "q", "value": query},
                {"name": "attachment", "blob_ref": ref, "filename": "query.txt", "content_type": "text/plain"},
                {"name": "scope", "value": "public"},
            ],
        }, ensure_ascii=False) + "\n")
    output.write(json.dumps({"id": 4, "fixture_id": "4", "search_text": "Malformed Row", "search_parts": "invalid"}) + "\n")
PY
fi

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
    "$PYTHON_BIN" "$EXAMPLE/serve_fixture.py" --access-log "$RUNS/access.log" --wire-log "$RUNS/wire.log" \
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
if [ "$pipeline_exit" -ne 0 ] && { [ "$variant" != multipart ] || [ "$pipeline_exit" -ne 1 ]; }; then
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
candidate_fields = {"name"} if variant in {"directory", "form", "multipart"} else {"name", "detail_path", "registration"}
assert all(set(candidate) == candidate_fields for row in rows for candidate in row["candidates"])
if variant == "multipart":
    failures = [json.loads(line) for line in (output / "failures.jsonl").read_text().splitlines()]
    assert len(failures) == 1, failures
    assert failures[0]["id"] == 4, failures
else:
    assert not (output / "failures.jsonl").exists()
print(f"verified_rows={len(rows)} verified_requests={len(requests)}")
PY

"$PYTHON_BIN" - "$RUNS" "$SETTINGS" <<'PY'
import pathlib
import sqlite3
import sys

runs = pathlib.Path(sys.argv[1])
settings_path = pathlib.Path(sys.argv[2])
expected_status = "completed_with_failures" if runs.name.endswith("multipart") else "completed"
source_run_id = sqlite3.connect(runs / "audit.db").execute(
    "SELECT run_id FROM runs WHERE status = ? ORDER BY started_at DESC LIMIT 1", (expected_status,)
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
if [ "$replay_exit" -ne 0 ] && { [ "$variant" != multipart ] || [ "$replay_exit" -ne 1 ]; }; then
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
if [ "$verify_exit" -ne 0 ] && { [ "$variant" != multipart ] || [ "$verify_exit" -ne 1 ]; }; then
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
from collections import Counter

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
if variant == "multipart":
    statuses = [row[0] for row in conn.execute("SELECT status FROM runs")]
    assert statuses == ["completed_with_failures"] * 3, statuses
    failed_states = conn.execute("SELECT COUNT(*) FROM node_states WHERE status = 'failed'").fetchone()[0]
    assert failed_states == 3, failed_states
    refs = [row[0] for row in conn.execute("SELECT request_ref FROM calls WHERE call_type = 'http'")]
    assert len(refs) == 9, refs
    audited_pairs = []
    for ref in refs:
        request = json.loads((runs / "payloads" / ref[:2] / ref).read_text())
        assert request["body_encoding"] == "multipart-v1", request
        assert [part["name"] for part in request["multipart"]] == ["q", "attachment", "scope"]
        assert request["body_size"] <= 8192
        audited_pairs.append((str(request["params"]["fixture_id"]), request["body_sha256"]))
    wire_entries = [json.loads(line) for line in (runs / "wire.log").read_text().splitlines()]
    assert len(wire_entries) == 6, wire_entries
    wire_pairs = [(entry["fixture_id"], entry["sha256"]) for entry in wire_entries]
    assert set(audited_pairs) == set(wire_pairs)
    assert set(Counter(audited_pairs).values()) == {3}
    assert set(Counter(wire_pairs).values()) == {2}
    audited_by_id = dict(audited_pairs)
    wire_by_id = dict(wire_pairs)
    assert audited_by_id == wire_by_id
    first_id, second_id = sorted(wire_by_id)[:2]
    swapped = dict(wire_by_id)
    swapped[first_id], swapped[second_id] = swapped[second_id], swapped[first_id]
    assert swapped != audited_by_id
print(f"verified_replay_without_network=3 verified_live_calls={verdicts[1]}")
PY
