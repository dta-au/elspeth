# Replay and Verify Run Modes

Records one live run and then reruns it in two other ways. **Replay** answers
the recorded external calls from the audit trail and makes no network
requests. **Verify** makes the calls again and compares every response with
the recording.

```
source ─(urls)─> fetch_page (web_scrape) ─┬─(output)─> pages.jsonl
                                          └─(on_error)─> fetch_failures.jsonl
```

The pipeline fetches three pages from a small fixture server on
`127.0.0.1:8204` (`serve_pages.py`). Each fetch is an audited HTTP call. No
credentials, API keys or internet access are needed.

## Run it

```bash
./examples/replay_verify/run.sh
```

The launcher runs every step below and checks each one. It exits 0 only when
all of them behave as described. Expected result: `COMPLETED`, exit 0. The
refusals and the failed verify are expected, and the script checks each one
for its reason, not only for a non-zero exit code.

## The three modes

| `run_mode` | External calls | Configured sinks | What it proves |
|------------|----------------|------------------|----------------|
| `live` (default) | Made and recorded | Written | The normal run |
| `replay` | Served from the recording, **no network** | Not written; rows compared with the recording | The pipeline reproduces the recorded outputs from the recorded inputs |
| `verify` | Made again, then compared with the recording | Not written; rows compared with the recording | The world still returns what it returned when the run was recorded |

Replay and verify each write a new run to the same Landscape database. Its
`run_mode` column is `replay` or `verify`, and its `replay_from_run_id` column
names the recorded run. Neither mode publishes to a sink. At each sink
boundary, the canonical rows are compared with the recording (node, role,
ingest sequence, disposition and payload hash). Serialized file bytes are not
compared.

## Walkthrough (what `run.sh` does)

Commands run from the repository root. The transcript lines below come from a
real run. Run IDs and call IDs differ on every run.

### Step 1: live

```bash
python examples/replay_verify/serve_pages.py --port 8204 \
    --access-log examples/replay_verify/runs/access.log &
elspeth run --settings examples/replay_verify/settings.yaml --execute --format json
```

```
--- Step 1: LIVE run (fixture server up on port 8204) ---
  exit code: 0   run_id: a9a15111d5cb4c2bbe9c3936e7e5a7fb
  requests served: 3   rows written: 3   sha256: fe9361be6e075ac85b7b19cb4f14b4167cc404502b4aec3a3e60c6794b4d2a9e
  call_type  status   call_id
  http       success  ea8938e6e3a4
  http       success  f7e73252bd5f
  http       success  c403c008fdf8
```

Note the `run_id` from the final `execution_result` event.

### Step 2: replay, with the server stopped

Stop the fixture server. Then write a replay settings file: the same pipeline
with two extra top-level keys. `run.sh` generates it as
`runs/settings_replay.yaml`:

```yaml
run_mode: replay
replay_from: "a9a15111d5cb4c2bbe9c3936e7e5a7fb"   # the live run_id, quoted
# ... every other line exactly as in settings.yaml ...
```

`run_mode` and `replay_from` must be literal YAML. The CLI refuses
`ELSPETH_REPLAY_FROM` and the other admission fields when they come from the
environment. Quote the run ID so that YAML cannot read an all-digit ID as a
number.

```bash
elspeth run --settings examples/replay_verify/runs/settings_replay.yaml --execute --format json
```

```
--- Step 2: REPLAY run (fixture server STOPPED) ---
  port 8204 refuses connections
  exit code: 0   run_id: 0e899a8f114241439fb037bbf5db8fe9
  Each replayed call names the recorded call it was served from:
  call_type  replay_call   source_call
  http       534b57fc3f88  ea8938e6e3a4
  http       083642c7b960  f7e73252bd5f
  http       643bad538839  c403c008fdf8
  The sink boundary was compared, not published (virtual effect, same payload hash):
  run_mode  published  evidence  payload_hash
  live      1          returned  8540f7ca147b2141
  replay    0          virtual   8540f7ca147b2141
```

Nothing is listening on port 8204 during this step, and the server's access
log does not grow. Had replay tried the network, the connection would have
been refused and the run could not have reproduced the recording. Each
replayed call is recorded with `calls.source_call_id` pointing at the recorded
call it was served from. The sink effect is `virtual`: it was compared, not
published. Its payload hash equals the live run's. `output/pages.jsonl` is
still the live run's file, byte for byte.

### Step 3: verify, with the server running again

```yaml
run_mode: verify
replay_from: "a9a15111d5cb4c2bbe9c3936e7e5a7fb"
```

```bash
elspeth run --settings examples/replay_verify/runs/settings_verify.yaml --execute --format json
```

```
--- Step 3: VERIFY run (fixture server restarted, same pages) ---
  exit code: 0   run_id: 9cdf70f4eeff45ff83a561e6c64a6a9d
  Verdicts (call_verifications):
  verify_call   source_call   is_match  differences_json
  5263dcdd164a  ea8938e6e3a4  1         {}
  3f6bbeb8fb74  f7e73252bd5f  1         {}
  94a09ab026b1  c403c008fdf8  1         {}
```

Verify makes the three requests again. Each call gets one verdict row in the
`call_verifications` table.

### Where the verdicts land

No `elspeth explain` view or MCP tool shows verdicts yet. Query the table
directly, or use the read-only `query` tool of the `elspeth-mcp` server:

```sql
-- The replay/verify runs and the run they came from
SELECT run_id, run_mode, replay_from_run_id, status FROM runs ORDER BY started_at;

-- Verify verdicts: one row per external call
SELECT current_call_id, source_call_id, is_match, differences_json
FROM call_verifications WHERE current_run_id = '<verify run_id>';

-- Replay lineage: which recorded call answered each replayed call
SELECT c.call_id, c.source_call_id
FROM calls c JOIN node_states n ON n.state_id = c.state_id
WHERE n.run_id = '<replay run_id>';
```

`is_match` is `1` when the live response is identical to the recording. It is
`0` when the two differ, and `differences_json` then carries the source and
current hashes.

## What is refused

`run.sh` exercises three negative cases:

| Case | Result | Real message |
|------|--------|--------------|
| `replay_from` names a run that does not exist | exit 1, before any run is recorded | `Configuration error: Replay/verify source run 'no-such-run' does not exist` |
| Any execution setting differs from the recorded run (here `http.timeout: 10` changed to `20`) | exit 4, before any plugin starts | `AuditIntegrityError: Replay execution settings differ from the source run` |
| Verify after a page changed (`two-year` changed to `three-year` in a copy of `warranty.html`) | exit 4, run recorded as `failed`, a `call_verifications` row with `is_match = 0` | `OrchestrationInvariantError: replay sink output differs from the source run` |

The code also refuses the following. `run.sh` does not exercise these:

- `concurrency.max_workers` other than 1 (`Replay/verify requires concurrency.max_workers=1`).
- `depends_on`, `collection_probes`, `commencement_gates`, Landscape export,
  telemetry exporters and Key Vault secrets.
- A source run that did not complete, or a changed graph, plugin version or
  plugin source file.
- Plugins outside the reviewed built-in inventory. Every shipped built-in
  plugin is in that inventory. A third-party plugin is refused even if it
  reuses a built-in name.
- Payloads or call evidence from the recorded run that are missing or have
  been tampered with.
- `elspeth resume` of a replay or verify run.

`elspeth validate` checks only the shape of the settings. It accepts a
`replay_from` that names a missing run. Source-run admission happens at
`elspeth run`.

## Why the fixture server strips the `Date` header

Verify compares each response **exactly**, headers included. `serve_pages.py`
sends only `Content-Type` and `Content-Length`, so an unchanged page returns
an identical response every time. **This example verifies cleanly only because
the fixture is byte-deterministic by construction.** An ordinary web server,
ChaosLLM or a real LLM provider changes something on every response (`Date`,
a response `id`, a `created` timestamp). Verify records every call against
them as a mismatch. See limitation 2 below.

## Known limitations (0.8.1)

These describe how the code behaves today. They are not workarounds for this
example to paper over.

1. **Replay through the OpenRouter LLM provider always refuses.** The run
   fails with `AuditIntegrityError: Replayed semantic LLM response differs from
   its source call`, even against a clean recording. The recorded evidence is
   deep-frozen (tuples and mappingproxies), and the freshly rebuilt response is
   plain lists and dicts. `provider.py` compares the two directly, so any
   response containing a list (`choices` always does) never compares equal.
   The gateway provider probably has the same problem. The Azure provider path
   (SDK-based) is the one covered by the end-to-end test. This is why this
   example uses `web_scrape` rather than an LLM.
2. **Verify never matches a real LLM or HTTP server.** The comparison is exact
   and includes per-response fields such as the OpenAI-shaped `id` and
   `created`, and the HTTP `Date` header. Against ChaosLLM, OpenRouter or any
   origin that sends `Date`, every call is recorded as `is_match = 0`, and
   verify exits 4.
3. **The live run must already use `concurrency.max_workers: 1`.** Replay and
   verify require one worker, and every setting other than `run_mode` and
   `replay_from` must equal the recorded run's. A live run made with the
   default worker count can therefore never be replayed. `settings.yaml` sets
   `max_workers: 1` for this reason.
4. **A verify mismatch is a fatal error, not a report.** It exits 4 and prints
   a traceback (`OrchestrationInvariantError` or `AuditIntegrityError`). With
   `--format json`, the `run_completed` event reports `"exit_code": 2` while
   the process exits 4. The verdict rows are still written before the failure.
   The fatal message for a verify run says "replay sink output differs".

## Files

| Path | Purpose |
|------|---------|
| `settings.yaml` | The live pipeline (`run_mode: live`) |
| `serve_pages.py` | Deterministic local page server, with an access log for counting requests |
| `pages/*.html` | The three pages it serves |
| `input.csv` | Three URLs on `127.0.0.1:8204` |
| `run.sh` | The self-checking walkthrough |
| `output/pages.jsonl` | Written by the live run only |
| `runs/` | Landscape DB, payload store, generated replay/verify settings, per-step logs and the access log (all gitignored) |
