# Example report: `examples/replay_verify` (run_mode live / replay / verify)

- Branch: `examples/replay-verify-20260924` (worktree `.claude/worktrees/examples-replay-verify`, based on `release/0.8.1` @ `ffd704d1a`)
- Commits: `20e0250a8` "examples: add replay_verify walkthrough for run_mode live/replay/verify", then `84b4ab1da`, a one-line README fix that dates the plugin-inventory claim ("At 0.8.1, ..."). HEAD is `84b4ab1da`. Nothing was merged, rebased or pushed, and the worktree is still in place.
- Provenance of the limitations: F1 and F5 were **reproduced here**. F2 is **consistent with the reviewer's report**: I confirmed that ChaosLLM bodies carry a random `id` and a `created` timestamp, but I did not run verify against ChaosLLM. F4 was **not tested here**; it is documented from the reviewer's report and the code (`admit_nonlive_settings` combined with the exact-settings check in `source_compatibility.py:50`).
- Result: **all three steps work end to end**, and so do three negative arms. The run used the worktree's `elspeth`: `elspeth.__file__` = `<wt>/src/elspeth/__init__.py`, and branch-safety-check reported "provenance PASS".

## 1. What was built, and why this shape

| File | Purpose |
|------|---------|
| `examples/replay_verify/settings.yaml` | Live pipeline: `csv` source, then `web_scrape` (3 HTTP GETs to `127.0.0.1:8204`), then `json` sink. It sets `run_mode: live` explicitly, `concurrency.max_workers: 1`, and puts the Landscape and payload store under `examples/replay_verify/runs/`. |
| `examples/replay_verify/serve_pages.py` | Stdlib deterministic page server. It uses `send_response_only`, so there are no `Date`/`Server` headers. It appends every request to `--access-log`, and `--pages-dir` lets the drift arm serve a changed copy. |
| `examples/replay_verify/pages/*.html`, `input.csv` | Three static pages and their URLs. |
| `examples/replay_verify/run.sh` | The self-checking walkthrough. It generates the replay and verify settings into `runs/`, because `replay_from` must be literal YAML. |
| `examples/replay_verify/README.md` | The three-step walkthrough with real transcript, where the verdicts land, what is refused, why the server strips `Date`, and "Known limitations (0.8.1)". |
| `examples/replay_verify/{output,runs}/.gitkeep` | Ignored output dirs, following the existing convention. |
| `examples/README.md`, `examples/AGENTS.md` | Index entries: a quick-start row, a new "Replay and Verify" section, an "If You Want to See..." row, and an AGENTS table. |
| `tests/unit/core/dag/canonical_hash_corpus.json` | **One new entry** for `examples/replay_verify/settings.yaml`. No existing pin moved: the failing diff before the edit showed "Left contains 1 more item" and nothing else. |

**Why `web_scrape` instead of an LLM.** I first built the example with the `llm` transform (OpenRouter provider) against ChaosLLM in template mode. The live run worked, but replay refused on every attempt (F1, reproduced independently below). Verify could never match either way (F2: ChaosLLM puts a random `id` and a `created` timestamp in every body). The `web_scrape` path is the one the e2e test `test_cli_http_replay_has_no_network_and_verify_persists_mismatch` covers, and it works today. It still makes a real recorded external call (an HTTP GET over a socket), and it needs no credentials. The fixture server is deterministic *by construction*, and the README says so in the body text: verify matches only because the fixture sends no `Date` header.

No `ELSPETH_FINGERPRINT_KEY` is needed. I measured this: a live run with the variable unset (and no `.env`) exited 0. So `run.sh` does not source `chaosllm_env.sh`.

## 2. Real transcript: `./examples/replay_verify/run.sh` (final run, committed tree)

Command: `cd <wt> && export PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src && unset ELSPETH_FINGERPRINT_KEY && ./examples/replay_verify/run.sh > log 2>&1; echo exit=$?` → **exit=0**

```
=== Replay / verify walkthrough ===

--- Step 1: LIVE run (fixture server up on port 8204) ---
  exit code: 0   run_id: 7df6a0e3cb8c4894b7de433af12b9185
  requests served: 3   rows written: 3   sha256: fe9361be6e075ac85b7b19cb4f14b4167cc404502b4aec3a3e60c6794b4d2a9e
  call_type  status   call_id
  http       success  07a8a90bcc42
  http       success  da73476ca0e4
  http       success  8cbcc115b52a

--- Step 2: REPLAY run (fixture server STOPPED) ---
  port 8204 refuses connections
  exit code: 0   run_id: 71a6c831fa4f44249063845cdad136f7
  Each replayed call names the recorded call it was served from:
  call_type  replay_call   source_call
  http       ae904cb3b9d4  07a8a90bcc42
  http       2c31cbc02e33  da73476ca0e4
  http       129b71d799f8  8cbcc115b52a
  The sink boundary was compared, not published (virtual effect, same payload hash):
  run_mode  published  evidence  payload_hash
  live      1          returned  8540f7ca147b2141
  replay    0          virtual   8540f7ca147b2141

--- Step 3: VERIFY run (fixture server restarted, same pages) ---
  exit code: 0   run_id: 9c63a6379b3b48d1a0cf6165a0a56e7f
  Verdicts (call_verifications):
  verify_call   source_call   is_match  differences_json
  8d67fd1f7e42  07a8a90bcc42  1         {}
  5c7d2a8f2809  da73476ca0e4  1         {}
  5e440feb1e4e  8cbcc115b52a  1         {}

--- Refusal a: replay_from names a run that does not exist ---
  exit code: 1   run_id: <none>
  Configuration error: Replay/verify source run 'no-such-run' does not exist

--- Refusal b: an execution setting differs from the recorded run ---
  exit code: 4   run_id: <none>
  AuditIntegrityError: Replay execution settings differ from the source run

--- Mismatch c: a page changed since the recording; verify must fail ---
  exit code: 4   run_id: 8c5486cbb3d74bb698aff2b302639de0
  OrchestrationInvariantError: replay sink output differs from the source run
  verify_call   is_match  differences
  b509685a90e9  1         {}
  6bbc61ce2445  1         {}
  846b23d71223  0         {"response_hash":{"current":"355afdb31f0fd0ad53c980a9978ff1e

--- Runs recorded in examples/replay_verify/runs/audit.db ---
  run_id                            run_mode  replay_from_run_id                status
  7df6a0e3cb8c4894b7de433af12b9185  live      None                              completed
  71a6c831fa4f44249063845cdad136f7  replay    7df6a0e3cb8c4894b7de433af12b9185  completed
  9c63a6379b3b48d1a0cf6165a0a56e7f  verify    7df6a0e3cb8c4894b7de433af12b9185  completed
  8c5486cbb3d74bb698aff2b302639de0  verify    7df6a0e3cb8c4894b7de433af12b9185  failed

VERIFIED: live -> replay (0 requests, same sink payload) -> verify (3/3 matches);
          missing source and drifted settings refused; a changed page failed verify.
Output (written by the live run only): examples/replay_verify/output/pages.jsonl
```

What the script asserts, not only prints:
- Live: exit 0; the access log has exactly 3 requests (the readiness probe is excluded); 3 rows written.
- Replay: exit 0; `curl` to 8204 fails before the run (the server is really down); the access log gains 0 lines; the `pages.jsonl` sha256 is unchanged; 3 row calls carry `calls.source_call_id`.
- Verify: exit 0; the access log gains exactly 3 lines; the sha256 is unchanged; 3 of 3 `call_verifications` rows have `is_match = 1`.
- Refusal a: non-zero exit, and stderr contains `source run 'no-such-run' does not exist`.
- Refusal b: non-zero exit, and the JSON fatal event equals `AuditIntegrityError: Replay execution settings differ from the source run`.
- Mismatch c: non-zero exit; a run id was recorded and its status is `failed`; at least one `call_verifications` row has `is_match = 0` with a source call; the sha256 is unchanged; the fatal reason contains "differs from the source run".

Instrument controls: the access-log counter reads 3 in the live step and 0 in replay, so it discriminates. The verify-drift arm shows that the `is_match` query finds a 0 when one exists.

## 3. Gates

| Gate | Command | Result |
|------|---------|--------|
| ruff check | `.venv/bin/ruff check examples/replay_verify/serve_pages.py` | exit 0, "All checks passed!" |
| ruff format | `.venv/bin/ruff format --check examples/replay_verify/serve_pages.py` | exit 0, "1 file already formatted" |
| bash syntax | `bash -n examples/replay_verify/run.sh` | exit 0 (shellcheck is not installed) |
| validate | `elspeth validate --settings examples/replay_verify/settings.yaml` (worktree PYTHONPATH) | exit 0, "Pipeline configuration valid! ... Graph: 4 nodes, 3 edges" |
| examples tests | `python -m pytest tests/unit/core/dag/test_canonical_hash_corpus.py tests/unit/docs/test_examples_readme_index.py tests/e2e/examples/test_shipped_examples.py -n 0` | exit 0, **40 passed**. Before the corpus entry was added: exit 1, with the corpus test failing only on the new key. |
| index test negative control | renamed the README link to `replay_verify_X/`, then ran `test_examples_readme_index.py` | exit 1 (red), file restored, final `git diff` shows only the intended +8 lines |
| related whole-repo tests | `tests/unit/docs/test_agent_docs_privacy.py tests/unit/deployment/test_deploy_ignore_policy.py tests/unit/elspeth_lints/test_pre_commit_triggers.py -n 0` | exit 0, 43 passed |
| branch safety | `scripts/branch-safety-check.sh --intent commit --base release/0.8.1` | FAIL=0 WARN=0 (home-paths, secrets, provenance all PASS) |
| pre-commit hooks | on `git commit -- <pathspecs>` | all Passed or Skipped |

No whole-tree AST gate scans `examples/`. Every `iter_gate_files` or `iter_gate_sources` root is under `src/` or `tests/`; I checked by tallying the arguments passed to those two functions. I did not run the full suite: the change is examples, docs and one corpus pin, which the focused tests cover.

## 4. Feature bugs and observations

### F1 confirmed independently: OpenRouter-provider replay always refuses
I reproduced this before the coordinator's note arrived. Setup: the `llm` transform with `provider: openrouter`, `base_url: http://127.0.0.1:8204/v1`, and ChaosLLM in template mode with zero faults (config below). The live run exited 0 with 4 rows. Replay exited 4 with `AuditIntegrityError: Replayed semantic LLM response differs from its source call`, raised at `runtime_preflight` → `openrouter.py:457 execute_query` → `provider.py:180 record_call`. The ChaosLLM access log showed **no** POSTs during replay, so the transport replay itself worked. A scratch harness that wraps `LLMAuditParent.record_call` printed:

```
TYPES <class 'mappingproxy'> <class 'tuple'> <class 'list'>
THAWED_EQUAL True
```

The recorded `evidence.response_data` is deep-frozen (`raw_response.choices` is a tuple). The live `response_data.to_dict()` has a list. `provider.py:179` compares them with `!=`, so every list-bearing response fails even though the thawed values are equal. ChaosLLM config for re-checking once F1 is fixed (`server.port: 8204`, `workers: 1`, every `*_pct: 0.0`, `burst.enabled: false`):

```yaml
response:
  mode: template
  template:
    body: |
      {%- set text = messages[-1]['content'] | string -%}
      {%- if 'late' in text or 'charged twice' in text -%}
      {"category": "complaint", "priority": "high"}
      {%- elif 'Thank you' in text -%}
      {"category": "praise", "priority": "low"}
      {%- else -%}
      {"category": "question", "priority": "medium"}
      {%- endif -%}
```

Even after F1 is fixed, verify against ChaosLLM will still fail on every call because of F2: I confirmed with `curl` that each ChaosLLM body carries `"id":"fake-<uuid4>"` and `"created":<epoch>`.

### Further findings (minor, new)
1. **The verify-mode fatal message says "replay".** A verify run against a changed page fails with `OrchestrationInvariantError: replay sink output differs from the source run` (`engine/executors/replay_sink_effect.py:128`, raised from `leader_drain.py:366`). The message names the wrong mode. Also, the sink-boundary comparison fires before the call-verification completeness check, so the user sees the sink message even though a call-level `is_match = 0` verdict was recorded.
2. **Exit code disagreement.** With `--format json`, the `run_completed` event reports `"exit_code": 2` for the failed verify run (and for the F1 replay), while the process exits 4. A JSON consumer and a shell see different codes.
3. **`elspeth validate` accepts a nonexistent `replay_from`.** `validate --settings` on a replay file naming `no-such-run` exits 0 ("Pipeline configuration valid!"). Source-run admission happens only at `run`. The README documents this.
4. **YAML typing trap for `replay_from`.** An unquoted all-digit ID (tried `replay_from: 00000000000000000000000000000000`) is parsed by YAML as an int and refused with `Configuration error: Replay/verify requires a literal replay_from run ID`. Hex run IDs are normally safe, but a hex ID shaped like a float (for example digits, then `e`, then digits) would also mis-parse. `run.sh` quotes the ID and the README says to.
5. **No read surface for verdicts.** Neither `elspeth explain` nor any `elspeth-mcp` tool reads `call_verifications` (a grep of `src/elspeth/mcp`, `src/elspeth/tui` and `cli.py` found no reference). Users must use SQL or the MCP generic `query` tool. The README documents this.
6. **Observation, not investigated.** Replay and verify runs each record one `operations.operation_type = 'sink_write'` operation with a `filesystem` call, even though no file is written: the output sha256 is unchanged, and the sink effect is `virtual` with `publication_performed = 0`. The label may mislead audit readers. Query: `select o.run_id, r.run_mode, o.operation_type, c.call_type from calls c join operations o on c.operation_id=o.operation_id join runs r on r.run_id=o.run_id`.

The README's "Known limitations (0.8.1)" section lists F1, F2, F4 and F5 with their current behaviour, and folds findings 1 and 2 into F5. No feature code was changed on the branch.

## 5. Notes for whoever runs it
- `run.sh` calls `.venv/bin/elspeth`. In a worktree, export `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src` first, or it imports the main checkout.
- Port 8204 is new. No other example uses it (existing ones use 8199–8202).
- `examples/reset.sh` already clears `runs/*.db*`. `run.sh` also clears the payloads, the generated YAML, the logs and the access log on every start.
