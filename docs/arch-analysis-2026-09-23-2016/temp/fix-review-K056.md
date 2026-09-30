# Fix review: K056 (run_mode replay/verify wired): adversarial red-team

Reviewed at HEAD `ffd704d1a` (release/0.8.1). Branch `fix/k056-replay-20260924` was merged at `87a3e2573`, with 13 more replay commits before the integration merge `bc0f251c4`.
Reviewer posture: I assumed the fix was broken and tried to prove it. Scratch files are under `/tmp/claude-1000/rt-k056/`. I made no changes to the main checkout.

## Verdict: PARTIAL

The original K056 defect is closed. Replay/verify no longer silently runs live. Both modes are consumed and persisted (`runs.run_mode`, `runs.replay_from_run_id`, `calls.source_call_id`, `call_verifications`). A missing, incomplete or drifted source run is refused, and an unreviewed plugin is refused. I found no canonical-config path that falls back to a live call. However, three defects remain in the delivered feature:

1. **LLM replay always refuses for the OpenRouter/gateway provider path** (High, confirmed). The first semantic LLM call raises `AuditIntegrityError`, because frozen evidence is compared with `!=` against a thawed dict.
2. **Verify cannot report a match against any real LLM or HTTP server** (Medium, confirmed). The comparison is exact and includes `raw_response.id`, `raw_response.created` and the HTTP `Date` header. In my run all 22 calls were recorded as mismatches even though the responses were semantically identical.
3. **Case/env-spelling bypass of raw non-live admission reaches the Key Vault client in a replay run** (Medium, confirmed). This is egress that the design says is refused, and it happens before any parsed-settings gate runs.

## E2E result (Q1)

The runs used `/home/john/elspeth/.venv/bin/python -m elspeth.cli` with `PYTHONPATH=/home/john/elspeth/src` (`elspeth.__file__` = main checkout src). Landscape, payloads and outputs all went under `/tmp/claude-1000/rt-k056/`. Egress was measured with an in-process `sys.addaudithook` on `socket.connect`/`socket.getaddrinfo`/`sendto` (`/tmp/claude-1000/rt-k056/egress_harness.py`). **Positive control:** the same hook logged 22 events for the LLM verify run. It covers the parent process only, not the template-worker subprocess.

**threshold_gate (fully offline):**
- The live run with the default `max_workers` (4) completed. Replay and verify of that run are both refused: first "requires concurrency.max_workers=1", then with `max_workers: 1` "Replay execution settings differ from the source run" (see F4).
- A live run with `max_workers: 1` completed, run `1ce8c7f1…`.
- Replay exit 0 and verify exit 0. Neither wrote any sink file and the `output/` directory is never created (checked with `rm -rf output`). The audit hook logged 0 socket events. `runs.run_mode` and `replay_from_run_id` are populated.
- Changed input then verify: exit 4, run `failed`, "Verify source 'primary': row 1 differs from audited run". Replay with the changed input: exit 0. This is correct, because replay reconstructs rows from the audit, not the file.
- The source run's rows (134 rows across all run-scoped tables, including calls and node_states) hash identically before and after a further replay and verify.

**chaosllm_sentiment (localhost ChaosLLM on :8299, 0% faults):**
- Live exit 0: 10 rows, 11 `http` + 11 `llm` calls.
- **Replay exit 4**: "Replayed semantic LLM response differs from its source call" (F1). A TCP counter listening on :8299 accepted 0 connections, and the audit hook logged 0 events. So the replay failed closed and made no egress.
- Replay with an in-process scratch monkeypatch that thaws the evidence (THAW_FIX=1, scratch only): exit 0, 10 rows. All 22 replayed calls carry `source_call_id` lineage, there were 0 socket events, and no output directory was created. This shows that F1 is the only thing blocking this path.
- **Verify exit 4**: "verify call verification incomplete: 0 unconsumed source calls, 22 failed decisions". 22 `call_verifications` rows were recorded, all with `is_match=0` (F2). Verdicts are therefore durably recorded.
- The source run's rows (166) hash identically after the extra verify and replay attempts.

Reviewer artefact: the two `running` replay rows in `llm/runs/audit.db` (83eefb13, 0f862164) came from my own debug harness raising `SystemExit` inside a monkeypatch. They are not a product finding.

## Findings

### F1: High, confirmed: OpenRouter/gateway LLM replay always fails (frozen-vs-thawed compare)
- `src/elspeth/plugins/transforms/llm/provider.py:179` (introduced in 3f78ae862): `evidence.response_data != actual_response`.
- `src/elspeth/contracts/call_mode.py:26`: `ReplayCallEvidence.__post_init__` deep-freezes `response_data`, which turns lists into tuples and dicts into mappingproxy. Every OpenAI-shaped response has `raw_response.choices` as a list, so the frozen and live values are never equal.
- Minimal repro: `ReplayCallEvidence(..., response_data={'raw_response':{'choices':[{}]}}).response_data == {'raw_response':{'choices':[{}]}}` gives `False`. A flat payload gives `True`.
- Scratch unit test `/tmp/claude-1000/rt-k056/scratch_tests/test_k056_frozen_compare.py`: the flat payload passes and the OpenAI-shaped payload FAILS with the production `AuditIntegrityError`.
- Affected callers: `providers/openrouter.py:544,585` and `providers/gateway.py:761,802`. Gateway goes through the same `LLMAuditParent.record_call`, so it is probable but I did not execute it. The Azure/SDK path (`clients/llm.py:425+`) reconstructs responses instead of comparing them and is not affected.
- The tests pass for the wrong reason. `tests/unit/plugins/llm/test_provider_openrouter.py::test_replay_semantic_llm_record_binds_source_call` and `::test_replay_semantic_llm_row_record_keeps_claim_authority` mock `replay_call` with a `SimpleNamespace` and a flat, thawed `{"content": "answer"}`, so they never see the freeze. The e2e LLM test (`tests/integration/pipeline/test_run_mode_end_to_end.py::test_cli_llm_replay_skips_sdk_and_verify_persists_mismatch`) uses `provider: azure`, which is the other path. All 79 openrouter tests pass at HEAD.

### F2: Medium, confirmed: verify can never produce a match against a real LLM/HTTP provider
- The exact comparison (the `call_mode_session.py` verify path, `stable_hash(recorded) != stable_hash(live)`) includes non-semantic, per-response fields. In the chaosllm verify, the only differences between source and current responses were `raw_response.id`, `raw_response.created`, and at HTTP level `body.id`/`body.created`, `transport.body_b64` and the `Date` header. Result: 22/22 `is_match=0`, exit 4.
- The design note (`docs/architecture/design-notes/replay-verify-runtime-contract.md`) says "Comparisons are exact by default … no general ignored-path setting". I am reporting what this produces, not judging the design: for OpenAI-shaped providers, and for any HTTP origin that sends `Date` (RFC 9110 origin servers with a clock), verify always fails.
- The e2e tests pass because the mocks omit these fields. The respx response in `test_cli_http_replay_has_no_network_and_verify_persists_mismatch` has only `content-type`, and the Azure mock in the LLM e2e test has no `id` or `created`.

### F3: Medium, confirmed: raw admission keyed on literal-case `run_mode` lets a replay run reach the Key Vault client
- `src/elspeth/cli.py:523,530` (`_admit_raw_cli_nonlive_run`) and `cli.py:788-794` (`_load_settings_with_secrets`) read `raw_config.get("run_mode")` and `os.environ.get("ELSPETH_RUN_MODE")`. The Dynaconf loader (`src/elspeth/config_loading.py:261`) lowercases keys and accepts `ELSPETH_<any case>`.
- YAML `RUN_MODE: replay` / `REPLAY_FROM: <id>`, or env `ELSPETH_run_mode=replay` + `ELSPETH_replay_from=<id>`, is classified as LIVE by the raw gate and loads as `run_mode=replay`. Probe `/tmp/claude-1000/rt-k056/tg/probe_load.py`: "raw admission mode: live / loaded run_mode: replay". The full run then completes as a replay (run 9d998324…).
- With `secrets: source: keyvault`, the canonical spelling is refused ("Replay/verify cannot fetch Key Vault secrets", exit 1). The upper-case YAML and the lower-case env variants both reach `KeyVaultSecretLoader._get_client` (DefaultAzureCredential + SecretClient), trapped in a harness (`tg/kv_harness.py`, exit 9). Ordering is what makes this an egress hole. `load_secrets_from_config` runs at `cli.py:798` inside `_load_settings_with_secrets`, before the parsed-settings admission `_admit_cli_nonlive_run` at `cli.py:996`. The case-literal raw gate is therefore the ONLY gate that can sit before Key Vault I/O, and a check in the parsed gate would already be too late. The parsed `admit_nonlive_settings` (`run_modes.py:55-70`) also has no Key Vault refusal, so the run then proceeds as a replay.
- The same bypass also skips the scoped plugin manager (global discovery runs) and the `ELSPETH_CONCURRENCY`/`ELSPETH_SOURCES`… env-prefix bans.
- Repro: see the JSON block.

### F4: Low, confirmed: default-config live runs can never be replayed or verified
- `ConcurrencySettings.max_workers` defaults to 4 (`core/config.py:1867`), and non-live modes require 1 (`run_modes.py:59`, `cli.py:568`). `source_compatibility.py:43-50` requires every non-invocation setting to be equal. A source run made with default settings is therefore permanently un-replayable.
- The refusal is a generic "Replay execution settings differ from the source run" with a traceback (exit 4) and does not name the field.
- `docs/reference/configuration.md:105` says "Concurrency must be one worker" but does not say that the *source* run must also have been run that way.

### F5: Low, confirmed: a verify mismatch uses the "framework bug" exit code
- A verify mismatch, which is the feature's expected negative verdict, exits 4 with a `FATAL — AuditIntegrityError` traceback. `cli.py:1192` labels exit 4 as "audit integrity / framework bug". A drift verdict cannot be told apart from Landscape corruption by exit code.
- The mismatch reason IS durably recorded: the failed `source_load` operation row of verify run e91d93eaf… carries "Verify source 'primary': row 1 differs…". There are no `call_verifications` rows, which is expected for a call-free source. The only issue is the exit-code conflation.

## Q2: Egress safety, other attacks that did NOT break it
- **Scope limit:** the no-egress evidence covers the offline threshold_gate pipeline, the openrouter HTTP-transport LLM path (live chaosllm, with the scratch thaw patch for replay), and static inspection of the 9 `AuditedHTTPClient` sites and the sink constructors. SDK-based providers (azure/bedrock LLM, AWS Textract/Guardrails, Chroma HttpClient, Azure Search managed identity) were NOT exercised end to end because no credentials were available. For those I relied on the existing tests, so this all-clear does not cover boto3/azure-sdk construction.
- Plugin admission (`plugins/infrastructure/run_mode_capabilities.py`) is a nominal class inventory. `type(x) is` and `registered_class(name) is expected` are ADR-032 compliant. All registered built-ins are in the inventory (measured with the plugin manager: 0 non-admitted names), and an unknown name is refused before the registry is imported: `acme_enricher` → "Replay/verify does not support transforms plugin 'acme_enricher'", exit 1. **A plugin without an adapter is refused, not run live.**
- All 9 `AuditedHTTPClient(` construction sites in admitted plugins pass `call_mode_session`. `PluginContext.__init__` (`contracts/plugin_context.py:231`) raises if a non-live context lacks a session.
- Sink clients (chroma_sink, dataverse) are built in `on_start`, which non-live modes do not call. No DNS in constructors (grep of `getaddrinfo` in plugins: 0 non-comment hits).
- Follower `join` refuses non-live (`cli.py:4249`). A config with `max_workers != 1` is refused.
- Mutation M1 (worktree, http.py `request_ssrf_safe` replay branch changed to `if False`) was killed by `test_cli_http_replay_has_no_network_and_verify_persists_mismatch` ("replay opened HTTP client"). Unmutated baseline: pass.

## Q3: Audit / Landscape
- Epoch went 43 → 44 in 1049a7bf0. 4bbe380f5 added `operations.occurrence_index` under the same unreleased epoch 44, and both commits are inside one merge. HEAD is now at 45 from the unrelated 69c57f943. The docs were aligned in 179bdbbc6.
- `CallRepository.record_verification_decision` (`core/landscape/execution/calls.py:830`) takes `coordination_token: CoordinationToken`. It runs inside `fenced_leader_transaction`, checks `token.run_id == current_run_id`, checks that the run is VERIFY and names that source, that the current call belongs to the current run, and that the source call belongs to the source run with the same type. Operation-call writes also call `_verify_source_call`.
- Replay "consume once" is in-memory (`call_mode_session.py`, `self._consumed`). Measured: the source-run rows are unchanged after replay and verify (hash comparison above). **A verify run cannot write into the source run's audit trail through these paths.**
- Observation, not a finding: `call_verifications` has no database-level constraint tying `current_call_id`'s run to `current_run_id`. This is enforced in code only.

## Q4: Trust tiers
- No `runtime_checkable` was added by the feature. The only diff hit is a pre-existing import line in `contracts/call_data.py`. The new `isinstance` checks are against owned concrete classes (`HTTPCallRequest`, `HTTPResponseTransport`, config classes).
- The Tier-1 read-back I sampled (`call_mode_session.py` `_preflight_source_calls`, `replay_call`, `_require_archived_dns_pin`, `_mi_non_auth_request`; `source_compatibility.py`; `clients/llm.py` replay reconstruction) uses exact type checks and raises `AuditIntegrityError`/`ValueError` on anomalies. I found no default-filling or coercion in the sampled paths. One nit: `json.loads(call.error_json)` raises a bare `JSONDecodeError`, not `AuditIntegrityError`. It still crashes, so it does not fail open.

## Q5: Size and scope (measured)
- `git diff --shortstat e69498f6c 87a3e2573^2` (the K056 branch, 58 commits): **127 files, +12,852 / −378**. Of that, src is 70 files +6,520/−312, tests 53 files +6,162/−54, docs 3 files +148/−4.
- 13 post-merge replay/landscape commits (d8ec3f43b…5cfb3ff9c): **+518 / −241**.
- Total ≈ **13.4k lines added, 71 commits**. The largest single file is `engine/orchestrator/call_mode_session.py` (+650).
- Fact: the analysis framed K056 as a wire-or-remove decision. The design note records "The operator selected **wire** for both modes". I did not verify where that selection was made.

## Q6: Test mutation evidence
- Throwaway worktree `/home/john/elspeth/.claude/worktrees/rt-k056` at ffd704d1a. `elspeth.__file__` resolved to the worktree src. The worktree was removed afterwards (`git worktree remove --force`, exit 0).
- Baseline: `test_cli_http_replay_has_no_network_and_verify_persists_mismatch` passed.
- M1 (bypass the HTTP replay intercept): the test FAILS with "replay opened HTTP client".
- M2 (raw CLI admission always returns LIVE): 21 failures in `tests/unit/cli/test_run_mode_admission.py`.
- The admission tests are strong against removing code. They do not cover input-spelling variants (F3), and the LLM-replay tests do not use the real evidence type (F1).

```json
{"findings": [
  {"title": "OpenRouter/gateway LLM replay always fails: frozen ReplayCallEvidence compared with != against thawed live response",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["src/elspeth/plugins/transforms/llm/provider.py", "src/elspeth/contracts/call_mode.py", "tests/unit/plugins/llm/test_provider_openrouter.py"],
   "repro": "PYTHONPATH=/home/john/elspeth/src python -c \"from elspeth.contracts.call_mode import ReplayCallEvidence; from elspeth.contracts.enums import CallStatus; d={'raw_response':{'choices':[{}]}}; print(ReplayCallEvidence(source_call_id='c',status=CallStatus.SUCCESS,response_data=d,error_data=None,latency_ms=None).response_data==d)\"  -> False. E2E: live-run examples/chaosllm_sentiment (openrouter provider, localhost chaosllm, max_workers 1) into a temp landscape, then run_mode: replay + replay_from -> exit 4 'Replayed semantic LLM response differs from its source call' (0 network connections). Scratch unit test /tmp/claude-1000/rt-k056/scratch_tests/test_k056_frozen_compare.py fails at provider.py:180.",
   "detail": "provider.py:179 (3f78ae862) compares evidence.response_data (deep_freeze -> tuples/mappingproxy, call_mode.py:26) with response_data.to_dict() (lists). Every OpenAI-shaped response carries raw_response.choices as a list, so replay through LLMAuditParent.record_call (openrouter.py:544/585, gateway.py:761/802) always raises. It fails closed (no egress), but whole-run LLM replay is unusable for these providers. The unit tests mock replay_call with SimpleNamespace and a flat thawed payload, and the e2e LLM test uses the azure SDK path, so every test passes."},
  {"title": "Verify never matches a real LLM/HTTP provider: exact comparison includes response id/created and HTTP Date header",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/engine/orchestrator/call_mode_session.py", "tests/integration/pipeline/test_run_mode_end_to_end.py"],
   "repro": "After the chaosllm live run above, restart chaosllm (same sequential responses) and run run_mode: verify -> exit 4 'verify call verification incomplete: ... 22 failed decisions'; call_verifications has 22 rows is_match=0; diffing source vs current payloads shows only raw_response.id, raw_response.created, body_b64 and headers.date differ.",
   "detail": "Verdicts are durably recorded, but the exact comparison treats per-response nondeterministic fields as semantic, so verify reports a mismatch on every call to an OpenAI-shaped provider and to any origin that sends Date. The e2e tests pass only because their mocks (respx with only content-type, azure mock without id/created) omit these fields. The design note mandates exact comparison. This records what that produces."},
  {"title": "Raw non-live admission keyed on literal-case run_mode: RUN_MODE: replay or ELSPETH_run_mode env reaches Key Vault client in a replay run",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/cli.py", "src/elspeth/engine/orchestrator/run_modes.py", "src/elspeth/config_loading.py"],
   "repro": "cd /tmp/claude-1000/rt-k056/tg; YAML with 'RUN_MODE: replay', 'REPLAY_FROM: <completed run id>', 'secrets: {source: keyvault, vault_url: https://rtk056-probe.vault.azure.net, mapping: {RTK056_DUMMY: rtk056-dummy}}' plus the threshold_gate pipeline with max_workers 1; run via kv_harness.py (traps KeyVaultSecretLoader._get_client) -> KEYVAULT_CLIENT_REQUESTED, exit 9. Same with lower-case run_mode -> 'Replay/verify cannot fetch Key Vault secrets', exit 1. Env variant: ELSPETH_run_mode=replay ELSPETH_replay_from=<id> -> also reaches the client. probe_load.py shows raw gate 'live' vs loaded 'replay'.",
   "detail": "cli.py:523/530 and 788-794 read raw_config.get('run_mode') and os.environ['ELSPETH_RUN_MODE'] literally, while the Dynaconf loader lowercases keys and accepts any-case ELSPETH_ env suffixes. The raw gate classifies the run as LIVE and skips the Key Vault refusal, the env-prefix bans and the scoped plugin manager. Key Vault I/O (cli.py:798) precedes the parsed admission (cli.py:996), so the raw gate is the only gate that can sit before it. The parsed admit_nonlive_settings also has no Key Vault check, and a run whose persisted run_mode is replay performs credential acquisition and Key Vault I/O. The design note says remote Key Vault resolution is refused in both non-live modes."},
  {"title": "Default-config live runs (max_workers default 4) can never be replayed or verified; refusal does not name the differing field",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/engine/orchestrator/source_compatibility.py", "src/elspeth/engine/orchestrator/run_modes.py", "docs/reference/configuration.md"],
   "repro": "Live-run examples/threshold_gate/settings.yaml (no concurrency block) into a temp landscape; replay it with max_workers omitted -> 'requires concurrency.max_workers=1'; with max_workers: 1 -> exit 4 AuditIntegrityError 'Replay execution settings differ from the source run'.",
   "detail": "Non-live modes require max_workers=1 and exact settings equality apart from run_mode/replay_from, so any source run made with the default (4) is permanently unreplayable. The docs do not tell operators that the source run must itself use max_workers: 1."},
  {"title": "Verify mismatch verdict exits 4 ('audit integrity / framework bug') with FATAL traceback",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/cli.py", "src/elspeth/engine/orchestrator/source_replay.py"],
   "repro": "threshold_gate: live (max_workers 1), edit input.csv row Bob 1500->900, run_mode: verify -> exit 4, 'FATAL — AuditIntegrityError: Verify source 'primary': row 1 differs from audited run' with traceback; run status failed.",
   "detail": "The expected negative verdict of verify shares exit code 4 with genuine audit-integrity failures and framework bugs, so automation cannot tell drift from corruption. The reason text is durably recorded on the failed source_load operation."}
]}
```
