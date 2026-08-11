# ELSPETH test-suite forensic audit

Date: 2026-08-11

Audited revision: `4e8042d266568d277b6546f2d1bbe7fe79891556` on `release/0.7.2`

Audit tracker: `elspeth-febddcdfe8`

## Verdict

ELSPETH has a large, generally serious test suite, but its current green signals are not a reliable proof of all the contracts they claim. The most important defects are not a shortage of assertions. They are false-green gates, tests disconnected from the production seam named in their prose, skipped or non-collected cohorts, order-dependent global state, and property tests that prove facts about their generators or Python instead of ELSPETH.

At the audited revision:

- The documented plain local Python gate was red: `1 failed, 39638 passed, 42 skipped, 1 xfailed` in 19m42s. The failure was a three-second outer safety timeout under the repository's automatic 12-worker xdist mode.
- The same failing node passed in isolation under both `-n0` and `-n12` (`-n0`: 20.49s). CI disables xdist, so the advertised local and CI execution models are not equivalent even though the isolated fault challenge did not reproduce a product race.
- The full serial CI-model baseline was also red: `7 failed, 39632 passed, 42 skipped, 271 deselected, 1 xfailed` in 1h25m49s. All seven failures lost `structlog.testing.capture_logs()` entries even though the expected events appeared on stdout and in pytest's captured stdlib log.
- The two serial-failure files passed together in isolation (`40 passed`). Ordered challenges also passed with the complete preceding sessions prefix (`664 passed`), every pre-sessions unit-web file (`10084 passed`), telemetry/logging (`477 passed`), and logging plus the Composer/execution prefix (`6226 passed`). This proves a non-local suite-state defect without identifying one deterministic polluter.
- The full xdist run made real OTLP export attempts to `127.0.0.1:4317` and emitted a `PytestUnhandledThreadExceptionWarning` from PyrateLimiter cleanup.
- Vitest was green: 2,946/2,946 cases in 176 files. The configured application/OIDC typecheck was green.
- The separately declared E2E TypeScript project was red with TS7016 and is omitted from the configured typecheck.
- Playwright collects 44 cases in 17 files, but only 36 are runnable; eight critical Composer journeys are skipped.
- The weekly mutation workflow and local strict helper cannot produce trustworthy mutation evidence with the installed mutmut 2.5.1 cache/result contract.

This is not a verdict that the suite is bad. Many superficially simple assertions were retained because they pin real ELSPETH wire values, schema shapes, state transitions, custody rules, or exact inventories. The audit rejected hundreds of scanner candidates that were legitimate no-raise contracts, delegated assertions, distinct public carriers, or meaningful exact-set gates.

## Classification standard

| Class | Audit meaning |
|---|---|
| Buggy | The test or harness itself races, leaks state, performs unintended I/O, or reports the wrong outcome. |
| Wrong | The test name/prose claims one branch or contract but its setup/oracle exercises another. |
| Silly | Failure would imply a change to Python, IEEE-754, set theory, reflexive equality, or another world invariant rather than ELSPETH. |
| Pointless | The collected node executes no test logic or cannot distinguish any relevant implementation. |
| Moot | The test targets removed or obsolete behavior, or is permanently non-collected/skipped without a replacement witness. |
| Redundant | Another node exercises the same call, input, and oracle; deleting the weaker copy loses no contract coverage. |
| Flaky/unreachable | The test depends on timing, order, random state, unavailable infrastructure, markers, or collection rules that keep it out of authoritative evidence. |
| Missing | A material production contract has no direct or adequately layered witness. |

Simple does not mean silly. For example, `status.value == "interrupted"`, an exact row shape, and a stable error code are valid contracts. `RunStatus.INTERRUPTED == RunStatus.INTERRUPTED` and `len(tuple_value) >= 0` are silly because the product implementation is irrelevant to their truth.

## Authoritative inventory and reachability

| Surface | Files/cases | Default or CI reach |
|---|---:|---|
| Python tests | 1,580 `test_*.py`; 1,785 Python files under `tests` | 39,681 selected test items of 39,952 collected by default; the full-session summary also counted one collection-time skip |
| Unit web | 398 files; 13,017 cases | Default Python lane |
| Integration | 197 files; 2,722 cases | 2,682 default; all 2,722 on protected integration pushes |
| Python E2E | 25 files; 156 cases | Default Python lane, subject to runtime skips |
| Testcontainer directory | 25 files; 146 cases | Dedicated Docker lane |
| All `testcontainer`-marked cases | 178 | 32 are outside the dedicated Docker lane |
| Property | 75 modules; 1,154 collected | 1,153 default-selected |
| Invariants | 10 modules; 114 cases | Default-selected, with runtime skips |
| Performance/stress | 81 cases, including 30 stress | No tracked automated lane |
| Vitest | 176 files; 2,946 cases | Required CI lane; no skipped cases in collection |
| Playwright | 17 files; 44 collected | Required CI lane; 36 active, 8 skipped |
| Staging Playwright | 9 spec files | Manual scripts expose only three single-file paths |

## Priority findings

### Gates and high-integrity contracts that can falsely certify the tree

#### G1. [P2] Local and CI Python execution models disagree

- Evidence: [`pyproject.toml`](../../pyproject.toml) enables the local xdist plugin; [`pytest_xdist_auto.py`](../../src/elspeth/testing/pytest_xdist_auto.py) disables it when `CI` or coverage is present.
- Observed survivor: [`test_late_older_guided_plan_progress_cannot_overwrite_the_newer_operation`](../../tests/integration/web/composer/guided/test_guided_full.py) timed out after three seconds in the 12-worker full run, then passed in isolation under `-n0` and `-n12`. The test already uses deterministic events; its outer wall-clock safety deadline is too tight under full-suite load.
- Risk: the documented local gate is red while serial CI can remain green; CI never challenges the default local concurrency model.
- Action: add a bounded xdist lane, keep the serial coverage lane, retain the event synchronization, and increase or derive the outer safety deadline from a suite-level timeout policy.

#### G2. [P2] Process-global operator telemetry makes tests order-dependent and can perform real I/O

- Evidence: [`create_app`](../../src/elspeth/web/app.py) bootstraps operator telemetry; [`bootstrap_operator_telemetry`](../../src/elspeth/web/operator_telemetry.py) reuses the first process-global runtime. A direct challenge showed later settings reuse whichever runtime was created first: a prior Prometheus runtime can suppress AWS exporters, while AWS-first construction can create a real loopback exporter.
- Observed behavior: the default run emitted repeated OTLP connection attempts. [`test_app.py`](../../tests/unit/web/test_app.py) includes AWS settings without a consistently owned singleton/factory seam, but the audit did not isolate the full-run attempts to one specific node.
- Risk: tests are non-hermetic and test order can select the wrong telemetry mode or allow unintended network activity.
- Action: inject no-op factories in app tests and restore/shutdown the singleton per owner; retain one explicit runtime-reuse test.

#### G3. [P2] Scheduled mutation evidence loses fatal status and strict-score meaning

- Evidence: [`.github/workflows/mutation-testing.yaml`](../../.github/workflows/mutation-testing.yaml) swallows all mutmut exit codes, checks `.mutmut-cache/mutmut.db`, and uploads `.mutmut-cache/` as a directory. Installed mutmut 2.5.1 uses one SQLite file named `.mutmut-cache`.
- Additional defect: [`scripts/run_mutation_testing.py`](../../scripts/run_mutation_testing.py) calls `rmtree()` on that file, parses a result format mutmut does not emit, discards nonzero statuses, and lets `--strict` pass when score parsing returns `None`.
- Risk: mutmut can execute and print human-readable results, but fatal runner failure, bad cache accounting, missing artifacts, manual-input states, or an absent score can still be reported as a successful weekly quality signal.
- Action: preserve exit codes, distinguish fatal from advisory survivor states, query the actual SQLite/status API, fail strict mode on absent results, upload the file, and use the frozen dependency install.
- Existing exact owner for the local strict-helper defect: `elspeth-85beab4f38`.

#### G4. [P3] Trust-tier fingerprint equality is a misleading diagnostic xfail

- Evidence: [`test_baseline_capture_is_self_consistent`](../../tests/unit/elspeth_lints/test_allowlist_loader_unification.py) says the baseline must match byte-for-byte but calls `pytest.xfail()` on drift. The current full run's single xfail was this node.
- Risk: this diagnostic does not enforce the behavior its prose claims. The independent trust-tier gate remains deliberately fail-closed, so this is not a bypass of operator signing.
- Action: split diagnostic capture from a gating equality test, or fail normally. This does not authorize agents to sign or regenerate the operator baseline.

#### G5. [P1] Masquerade exact-root coverage derives its oracle from production

- Evidence: [`test_live_scan_visits_files_and_sites_in_every_covered_root`](../../tests/unit/elspeth_lints/test_masquerade_gate.py) iterates production `SCAN_SUBDIRS`. Removing `scripts` removes both the product root and the test parameter; the synthetic probe exists only under `src/elspeth`.
- Risk: an entire trust-gate root can disappear while all anti-inert checks remain green.
- Action: assert an independent literal four-root set and plant a firing synthetic probe in every required root.

#### G6. [P1] Session direct-writer governance misses ordinary alias forms

- Evidence: [`test_static_direct_writers.py`](../../tests/unit/web/sessions/test_static_direct_writers.py) misses `insert as sql_insert`, `sa.insert(...)`, and aliased table names. Its supposedly line-anchored dynamic allowlist approves arbitrary sites when counts match.
- Risk: unreviewed direct writes can bypass sanctioned custody writers while the static gate passes.
- Action: resolve import/table aliases and bind allowlist entries to path, enclosing symbol, operation, and stable identity/line.

#### G7. [P2] Browser retries can hide a regression and discard its evidence

- Evidence: [`playwright.config.ts`](../../src/elspeth/web/frontend/playwright.config.ts) uses two retries in CI without `failOnFlakyTests`; [`ci.yaml`](../../.github/workflows/ci.yaml) uploads artifacts only on final failure.
- Survivor: a test that fails only when `testInfo.retry == 0` makes the lane green and skips failure-only artifact upload.
- Action: set `failOnFlakyTests: isCI`; retain retries for diagnosis; upload reports/traces with `always()`.

#### G8. [P2] Gating browser journeys execute Vite, not the built SPA

- Evidence: the Playwright base URL is the Vite origin even though the harness builds `dist` and starts the FastAPI production-assembly server. Only a document-header check touches the built document; the release-smoke journey is staging-only.
- Survivor: break built chunk paths, `index.html`, or same-origin routing after `npm run build`; ordinary gating journeys can stay green.
- Action: make at least one representative authenticated journey execute the production-served SPA and API assembly.

#### G9. [P1] The staging browser harness can mishandle live credentials

- Evidence: [`playwright.staging.config.ts`](../../src/elspeth/web/frontend/playwright.staging.config.ts) accepts unvalidated `STAGING_BASE_URL`; [`staging-global-setup.ts`](../../src/elspeth/web/frontend/tests/e2e/setup/staging-global-setup.ts) posts credentials and writes bearer storage state without restrictive modes. Current `.auth/` was 775 and `staging-user.json` was 664.
- Risk: plaintext/arbitrary-origin credential egress, symlink/file-mode exposure, and an unclosed staging test denominator.
- Action: require an exact HTTPS origin before network I/O; create a private 0700 directory and 0600 file without symlink following; give the staging config an explicit `testMatch`.

#### G10. [P1] Thirty-two Docker-backed integration tests miss their Docker lane

- Evidence: 178 cases carry `testcontainer`, but the dedicated lane runs only the 146 cases under `tests/testcontainer`. The protected integration lane's explicit `-m integration` selects the other 32 in an environment documented as lacking Docker.
- Risk: PostgreSQL concurrency proofs self-skip or fail outside their only capable lane while the Docker gate never sees them.
- Action: select all repository `testcontainer` markers in the Docker lane and explicitly exclude them from the non-Docker integration lane.

#### G11. [P1] Three shipped example configurations are invalid but structurally certified

- Evidence: [`test_shipped_examples.py`](../../tests/e2e/examples/test_shipped_examples.py) exempts 16 configs from `load_settings()` and checks YAML shape only. Safe placeholder validation found missing `on_success`/`on_error` in:
  - `examples/azure_blob_sentiment/settings.yaml`
  - `examples/azure_blob_sentiment/settings_pooled.yaml`
  - `examples/azure_openai_sentiment/settings_pooled.yaml`
- Risk: shipped examples pass their tests but fail production `ElspethSettings` validation.
- Action: validate every maintained config with harmless placeholder values and execute deterministic examples according to `examples/AGENTS.md` and `examples/README.md`.

#### G12. [P1] The optional secret-backed integration lane has no live denominator

- Evidence: CI supplies `OPENROUTER_API_KEY`, but no integration/E2E test uses that ambient key for a real call; actual live checks require other opt-ins. The lane explicitly accepts self-skips.
- Risk: the lane can be green with zero external calls.
- Action: define a closed live-test inventory, configure its actual credentials/opt-ins, and fail inert when zero live witnesses execute.

#### G13. [P1] Composer LLM tool arguments can escape as `RecursionError`

- Evidence: [`_coerce_stringified_json_object`](../../src/elspeth/web/composer/redaction.py) catches JSON/value errors but not recursion exhaustion. Depths around 9,999 raised `RecursionError`; current tests cover only shallow malformed/list/scalar cases.
- Risk: untrusted LLM arguments can become an HTTP 500 rather than a typed, audited argument error.
- Action: bound depth/size before or during decoding and add a public dispatch/redaction witness that asserts fail-closed disposition and no raw-input persistence.
- The broad JSON-bounds owner `elspeth-b944d2324a` was closed after earlier ingress paths were fixed, but this live survivor shows that its declared scope is incomplete.

#### G14. [P2] Persisted Composer dispatch JSON can escape as `RecursionError`

- Evidence: [`_validate_exact_canonical_json`](../../src/elspeth/web/composer/pipeline_commit.py) passes persisted JSON directly to `json.loads`; the surrounding path explicitly disclaims broad exception containment. A valid object nested roughly 1,500 levels deep raised raw `RecursionError` in a direct challenge.
- Risk: hostile or corrupt persisted dispatch data can escape the typed integrity boundary and terminate request processing.
- Action: apply the same bounded JSON policy at persisted canonical dispatch validation and assert a typed fail-closed result around the chosen depth/node/byte limits.
- Tracker disposition: amend or reopen `elspeth-b944d2324a`; this is a separate persisted-input seam from G13 but falls inside that issue's stated recursive JSON scope.

#### G15. [P2] Frontend external-boundary decoders have material unwitnessed holes

- Audit readiness: [`auditReadiness.ts`](../../src/elspeth/web/frontend/src/api/auditReadiness.ts) drops `plugin_policy_readiness` and accepts incomplete/duplicate readiness rows. The dropped field has exact owner `elspeth-8cfd44bd67`; incomplete/duplicate rows are an additional P2 contract gap.
- Shared reviews: [`shareableReviews.ts`](../../src/elspeth/web/frontend/src/api/shareableReviews.ts) validates top-level arrays but not owned nested source/node/edge/output shapes; `{nodes: [null]}` passes its current decoder.
- WebSocket: [`websocket.ts`](../../src/elspeth/web/frontend/src/api/websocket.ts) parses and casts untrusted frames without envelope validation; its tests never deliver a message. Exact owner: `elspeth-0df5201431`.
- Action: add owned runtime decoders, exact negative fixtures, and malformed-frame tests at the external boundary. Keep the distinct tracker owners rather than combining all decoder work into one ticket.

#### G16. [P2] Fork settlement-failure coverage ignores copied-blob compensation

- Evidence: [`test_fork_state_rewrite_failure_archives_session`](../../tests/unit/web/sessions/test_fork.py) stays green when `cleanup_blobs_for_fork` is replaced with a successful no-op.
- Risk: a failed fork can leave copied blobs orphaned while the test checks only HTTP 500 and session visibility.
- Action: assert the exact copied-blob set is removed and unrelated blobs remain.

#### G17. [P2] Full-serial logging assertions depend on non-local suite state

- Evidence: the full `CI=true` serial baseline failed two nodes in [`test_guided_start.py`](../../tests/unit/web/sessions/test_guided_start.py) and all five nodes in [`test_unwind_persist_forensics.py`](../../tests/unit/web/sessions/test_unwind_persist_forensics.py). In every failure, `capture_logs()` was empty while the exact expected event appeared on stdout and under pytest's captured logger `_pytest.monkeypatch`.
- Isolation: both files passed together (`40 passed`). No bounded reproduction triggered the failures: the complete sessions prefix plus sentinels passed 664 cases; every earlier `tests/unit/web` file plus sentinels passed 10,084; telemetry/logging passed 477; core logging plus the Composer/execution prefix passed 6,226.
- Risk: the CI execution model can fail only after a broad suite history, while targeted reruns falsely clear it. Conversely, the failing assertions misdiagnose an event as absent when it was actually emitted.
- Action: stop relying on process-global `capture_logs()` for these threaded/request-path contracts. Inject or bind a test-owned event sink, restore all structlog/stdlib configuration and background owners, add deterministic order sentinels, and require both full serial and isolated-file outcomes to agree.

## P2: important wrong, unreachable, or missing evidence

### Selection and harness gaps

- All 81 performance/stress cases are absent from tracked automation. This includes correctness/concurrency assertions, not only host-sensitive budgets.
- The 1,000-example Hypothesis `nightly` profile is never selected; 775 of 849 Hypothesis tests hard-code `max_examples`, so most would ignore it anyway.
- PostgreSQL session/blob race variants skip because no CI job sets `ELSPETH_TEST_POSTGRES_URL`.
- Permission-boundary tests run as root in Python CI and self-skip.
- Nine Azurite E2E cases skip in a clean CI checkout because the Python job never installs the root package dependency.
- The 37 convergence-scenario cases depend on an intentionally untracked corpus and can skip in clean CI.
- Staging/OIDC Playwright scripts are manual only; default E2E bypasses the login UI entirely.
- E2E TypeScript, broad frontend lint, and CSS lint are outside the required gates. `npx tsc -p tsconfig.e2e.json --noEmit` currently exits 2.

### Wrong or disconnected Python tests

| Evidence | Defect | Smallest repair |
|---|---|---|
| [`test_retention_monotonicity.py:168`](../../tests/property/core/test_retention_monotonicity.py) and `:258` | Both monotonicity properties put every generated run on the same side of both cutoffs; the first also uses wall time. | Pass explicit `as_of`, generate a run between cutoffs, and require the exact strict set difference. |
| [`test_azure_safety_properties.py:341`](../../tests/property/plugins/transforms/azure/test_azure_safety_properties.py) | “Fail-closed” properties prove filtered-map lookup, set subtraction, `isinstance`, truthiness, boolean OR, and a copied loop; none calls production parsing. | Generate external JSON shapes and drive `_analyze_content`/`_analyze_prompt` through mocked HTTP responses. |
| [`test_processor.py:9927`](../../tests/unit/engine/test_processor.py) | Ready-emission parity manually reproduces the mapping and never calls the repository writer. | Call `complete_barrier` with a maximal emission and assert the persisted/claimed full row. |
| [`test_validation_path_agreement.py:653`](../../tests/unit/plugins/test_validation_path_agreement.py) | Completeness collapses a plugin's validators to one plugin-name bit; one case exempts all present/future validators. | Inventory validator identities/rejection cases and require two-path agreement per condition. |
| [`test_database_sqlcipher.py:330`](../../tests/unit/core/landscape/test_database_sqlcipher.py) | Three MCP passphrase tests locally reproduce an obsolete SQLite/`ELSPETH_AUDIT_KEY` branch. | Exercise current `main()` parsing, named-env lookup, blank refusal, and `run_server` forwarding. |
| [`test_service.py:755`](../../tests/unit/web/composer/test_service.py) plus planner tests | Helper and planner are tested separately; dropping `conversation_context` at the service join survives. | Add one service-path referential-history regression. |
| [`test_tools.py:2420`](../../tests/unit/web/composer/test_tools.py) and `:11152` | Tests claim transform-condition validation but assert only mutation success. | Assert `transform_unexpected_condition` in returned validation. |
| [`test_chat_solver.py:242`](../../tests/unit/web/composer/guided/test_chat_solver.py) | Closed unions are checked by length, not exact membership. | Compare each alias to the literal permitted type set. |
| [`test_skill.py:13`](../../tests/unit/web/composer/guided/test_skill.py) | Substring `"invent"` accepts the opposite anti-fabrication policy. | Assert the negative semantic sentence or a structured policy token. |
| [`test_interpretation_events_service.py:1781`](../../tests/unit/web/sessions/test_interpretation_events_service.py) | Generic `ValueError` satisfies a contract whose route maps only `InterpretationNodeMissingError` to 422. | Assert the exact exception and route response. |
| [`test_request_id.py:133`](../../tests/unit/web/middleware/test_request_id.py) | Empty-ID replacement can diverge between request state and response header. | Assert header, body/state, and log share the generated ID. |
| [`test_local_provider.py:116`](../../tests/unit/web/auth/test_local_provider.py) | Two create-user tests stay green when creation immediately returns. | Read back and assert durable identity/email/verification state. |
| [`test_outputs_loader.py:147`](../../tests/unit/web/execution/test_outputs_loader.py) | No positive node reaches `load_run_outputs_for_settings`; read-only/passphrase/session/payload forwarding can disappear. | Use a real temporary SQLite settings wrapper and spy on exact factory/delegate arguments. |
| [`test_interpretation_opt_out_routes.py:237`](../../tests/unit/web/sessions/test_interpretation_opt_out_routes.py) | Telemetry regressions cause the tests themselves to skip by source-text feature detection. | Remove conditional skipping and fail when the shipped counter path is absent. |

### Performance and stress tests that do not measure their named contract

- [`memory_tracker`](../../tests/performance/conftest.py) uses lifetime high-water RSS. Prior allocations can make retained leaks report zero; the “linear” node checks only a 500 MB absolute cap and no slope.
- “P99” benchmarks use `Q3 + 3*IQR` or `mean + 3*stddev`, not an empirical 99th percentile.
- Stress injection uses unseeded randomness; free-port discovery closes the socket before the server binds; teardown does not fail on a live thread.
- Eleven of twelve scalability cases record timings without asserting them. Their correctness assertions should be moved into an ordinary load-smoke lane; calibrated NFR budgets should run separately in fresh processes.

### Missing product-contract witnesses

The following are confirmed gaps. Items with existing owners should receive audit evidence rather than duplicate tickets.

| Contract | Evidence gap | Existing owner |
|---|---|---|
| Async-worker admission remains bounded after caller timeout | Tests prove exception draining, not backpressure while synchronous workers continue. | `elspeth-5269b43bca` |
| Resume cleans leadership/seat after post-CAS payload failure | Cleanup `try` begins after reconstruction; no corrupt/missing payload witness at that seam. | `elspeth-245b21351b` |
| Current Terraform package and selected image agree before apply | Current tests compare package to HEAD or defer image admission to apply time. | `elspeth-b3ef2a6fb0`, `elspeth-9f7d336e1c` |
| Maintained examples actually execute | Only three directories have substantive pytest execution; most receive config/docs/launcher-shape checks. | `elspeth-16dfb937ba` |
| DAG scenario evidence is complete by dimension | 49 cases/165 cells still include partial/unknown/fail cells; concurrency, scale, and round-trip have no passing cells. | `elspeth-ef29ef6ba4` |
| Source-schema non-string corruption fails on a strict backend | The only node is permanently skipped under SQLite; no PostgreSQL counterpart was found. | none found |
| Composer progress, edit/fork, pending proposals, cancellation, and secrets-store state transitions work through real seams | Parent/component/API layers mock each other or assert presence/copy only. | bundle as frontend interaction coverage |
| Mermaid output is sanitized and render rejection exposes source fallback | Current tests inspect an empty container synchronously; one loops over an empty NodeList. | no exact owner; accessibility issue `elspeth-aad3eaf40c` is adjacent |
| YAML copy/download emits exact bytes | Current tests assert feedback/filename, not clipboard/blob content. | seeded YAML E2E work `elspeth-7cf763da7c` is adjacent |
| Catalog fingerprint rejects missing/blank values | Production is correct; no negative test protects the guard. | none found |
| Shared graph button has an observable action | Presence test does not click; production mounts no graph modal. | none found |

## P3/P4: pointless, moot, silly, and redundant cleanup

These should be handled as bounded cleanup, not release blockers, unless they mask one of the P1/P2 gaps above.

### Empty, non-collected, or skipped nodes

- [`test_execute_flush_empty_buffer_raises_runtime_error`](../../tests/unit/engine/test_executors.py) contains only `pass`; production raises `OrchestrationInvariantError`, not the named `RuntimeError`.
- [`test_arguments_hash_matches_domain_v2`](../../tests/unit/web/sessions/test_interpretation_events_service.py) contains only a docstring/comments; the adjacent numbered node covers the real contract.
- `_LegacyExportLandscapeJSON` in [`test_export.py`](../../tests/unit/engine/orchestrator/test_export.py) contains 19 `test_*` methods but is not collected; two call a removed symbol.
- [`test_non_string_schema_raises`](../../tests/unit/core/landscape/test_run_lifecycle_repository.py) is unconditionally skipped.
- The invariant autouse fixture [`_verify_plugin_manager_clean`](../../tests/invariants/conftest.py) only yields and performs no promised teardown verification.

### Medium-confidence cleanup flake

- [`RateLimiter.close`](../../src/elspeth/core/rate_limit/limiter.py) waits only 50 ms before removing its thread-ID suppression and restoring the process-global exception hook, even when the PyrateLimiter leaker thread remains alive. This is a plausible explanation for the full run's `PytestUnhandledThreadExceptionWarning`, but 77 targeted rate-limiter tests passed both serially and under xdist without reproducing it. Treat this as P3 diagnostic evidence for existing owner `elspeth-b860136b9b`, not as a newly proven product failure.

### Silly or tautological sub-assertions

- `len(result.failed_refs) >= 0` in [`test_retention_monotonicity.py`](../../tests/property/core/test_retention_monotonicity.py).
- `RunStatus.INTERRUPTED == RunStatus.INTERRUPTED` and the equivalent completion enum comparison in [`test_graceful_shutdown.py`](../../tests/unit/engine/orchestrator/test_graceful_shutdown.py). Retain the adjacent `.value == "interrupted"` contracts.
- IEEE-754 setup assertions such as `nan != nan` and `nan is nan` in NaN rejection properties. Retain the ELSPETH `canonical_json` rejection assertions.
- [`TestValueSourceUnion::test_union_accepts_both_variants`](../../tests/unit/contracts/test_value_source.py) constructs each concrete class and asserts its concrete type; a local union annotation is not runtime-enforced and can drop a member without failing.
- Valid-range retry properties that generate only valid lower bounds and reassert those generator bounds.
- The “immutable readable” retry-config property reads fields but never attempts mutation.

### False structural oracles

- Advisor delegation test proves only that callers do not directly call a lower-level method; removing the shared gate calls survives.
- Shared compose-lock test counts settlement calls and survives removing every lock.
- Dynamic-attribute test searches source for `"getattr("`; aliasing `getattr` survives, while comments/strings can fail it. The semantic whole-tree gate is authoritative.
- “Every tool dispatches” samples seven calls; removing an entire declaration plane survives.
- Registry overlap test reloads the facade while the owning `_registry` remains cached.
- Request-ID maximum “exact pin” accepts any value from 32 to 128.
- Unknown-tool continuation uses `expected_text in message or message`, so any non-empty prose passes.
- Rate-limiter “serialized” test has no suspension inside the critical section; replacing the lock with a no-op survives.
- Frontend union “exhaustiveness” tests assign a hand-written array and assert its length; widening the production union does not require updating the array.
- CSS source regexes can pass despite later contradictory cascade rules and fail on semantically equivalent CSS. Existing owner: `elspeth-27dc483116`.

### Exact redundancy and dead support

- Static normalized-AST analysis found 127 duplicate-body groups covering 284 Python tests. Eighteen groups were adjudicated as exact same-call/input/oracle redundancy; the rest require domain-level review before deletion.
- Confirmed duplicate examples include two port-ingestion tests in `test_app.py`, two endpoint-default tests in `test_config.py`, repeated expression-parser rows, clock/coalesce/processor/executor nodes, and moved-but-not-removed processor/navigation characterizations.
- Guided Playwright has one Ctrl+Shift+G open-only case subsumed by open-and-Escape-close.
- MultiSelectWithCustomTurn repeats the same fixture and oracle twice.
- Imported `Test*` classes create 104 redundant integration nodes: 102 Step-2 cases plus two cross-step copies.
- Static reference analysis found 17 fixtures and 14 helper/class definitions with no tracked in-tree consumer. These are candidates, not automatic deletions; external/manual imports must be checked first.

## Rejected candidates

The audit explicitly retained the following despite superficial scanner signals:

- No-assert calls that pin a real “must not raise” or idempotent no-op contract.
- Repeated-call determinism checks when nondeterminism is possible and an independent exact-value oracle exists.
- Simple schema, enum-value, exact-inventory, row-shape, and error-code assertions that pin owned ELSPETH contracts.
- Distinct tests for `RunStatusResponse` and `RunResultsResponse`, which are separate public carriers despite identical bodies.
- Attribute, masquerade, wire-shape, IAM action-set, and public `__all__` tests where an independent exact expected surface is present.
- Real CSV source/sink E2E assembly, gateway socket tests with mocks only at the external agency boundary, and PostgreSQL testcontainers.
- Expression `len`/`abs` tests that exercise ELSPETH parsing/allowlisting/evaluation rather than Python builtins alone.
- Token fork isolation, which killed a `deepcopy`-to-identity mutation; only the separate expand-token claim survived that mutation.
- Azure threshold boundary tests that call the live `_check_thresholds`; only the disconnected “fail-closed property” cohort is rejected.

## Tracker disposition

All newly created work is a child of audit epic `elspeth-febddcdfe8`. Existing exact owners received current evidence rather than duplicate issues.

| Audit slice | Tracker disposition |
|---|---|
| G1 local/CI execution parity | `elspeth-8096406717` |
| G2 telemetry test hermeticity | `elspeth-85bee04b10` |
| G3 mutation workflow | `elspeth-1563ede8c3`; exact local strict-helper owner `elspeth-85beab4f38` |
| G4 diagnostic fingerprint prose | Evidence added to closed intentional-reminder owner `elspeth-b886ef9edb` |
| G5 masquerade roots | `elspeth-fb173ab571` |
| G6 direct-writer aliases/sites | `elspeth-225e8df59e` |
| G7-G8 browser gate and E2E TypeScript | `elspeth-c9ba1ba888` |
| G9 staging credentials | `elspeth-a688acff82` |
| G10 Docker marker routing | `elspeth-876388226c`; residual evidence added to closed directory-lane owner `elspeth-89e267a4f8` |
| G11 invalid examples | `elspeth-ecc17febe3`; execution remains under `elspeth-16dfb937ba` |
| G12 live denominator | `elspeth-11c4f5592b` |
| G13-G14 recursive JSON | Reopened `elspeth-b944d2324a` with both current survivors |
| G15 frontend decoders | `elspeth-e027510adf`; exact owners `elspeth-8cfd44bd67` and `elspeth-0df5201431` |
| G16 fork compensation | `elspeth-090148dc8a` |
| G17 serial logging capture isolation | `elspeth-364103ba1b` |
| Vacuous properties | `elspeth-51fc1f9b8c` |
| Disconnected Python contracts | `elspeth-15ac854f37` |
| Performance/stress evidence | `elspeth-efe3330432` |
| Dormant environment denominators | `elspeth-baa39ba125` |
| Pointless/moot/silly/redundant cleanup | `elspeth-7165df66b2` |
| Rate-limiter cleanup warning | Medium-confidence evidence added to `elspeth-b860136b9b` |
| Other confirmed missing contracts | Evidence added to `elspeth-5269b43bca`, `elspeth-245b21351b`, `elspeth-b3ef2a6fb0`, `elspeth-9f7d336e1c`, `elspeth-ef29ef6ba4`, `elspeth-27dc483116`, `elspeth-aad3eaf40c`, and `elspeth-7cf763da7c` |

## Executable remediation sequence

1. **Restore trustworthy gates by domain.** Fix G1 and G17 before claiming local/CI Python parity; G3 before treating mutation score as evidence; G7-G8 before treating browser green as built-SPA correctness; and G10/G12 before claiming Docker/live integration coverage. Preserve first-failure evidence and add fail-on-inert denominators where a lane can run zero witnesses.
2. **Close current production escapes.** Bound nested Composer JSON; harden staging credential handling; repair frontend external-boundary decoders; validate the three invalid examples.
3. **Repair high-integrity false oracles.** Direct-writer aliases/identity, fork compensation, retention boundaries, Azure response parsing, ready-emission persistence, exact exception/error contracts.
4. **Activate missing environments.** All testcontainer markers in Docker, PostgreSQL race lane, non-root permission lane, Azurite installation, scheduled property/performance campaigns.
5. **Add integration witnesses at real joins.** Composer service history, output settings wrapper, user persistence, frontend parent/child interaction seams, exact emitted YAML bytes.
6. **Clean the noise.** Delete empty/comment-only nodes, non-collected legacy cohorts, reflexive/world-invariant assertions, exact duplicates, and verified dead support. Rename honest smoke tests rather than letting them claim exhaustiveness.
7. **Measure improvement by fault challenge.** A remediation is complete only when the cited survivor mutation makes the new/changed test fail for the intended reason.

### Repeatable audit commands

```bash
# Inventory and authoritative selection
CI=true .venv/bin/python -m pytest --collect-only -q -n0 tests/
.venv/bin/python -m pytest --collect-only -q -n0 -o addopts='' tests/
.venv/bin/python -m pytest --collect-only -q -n0 -m testcontainer tests/

# Both Python execution models
.venv/bin/python -m pytest tests/
CI=true .venv/bin/python -m pytest -n0 tests/

# Frontend (subshells preserve the repository-root cwd for later commands)
npm --prefix src/elspeth/web/frontend test -- --run
npm --prefix src/elspeth/web/frontend run typecheck
(cd src/elspeth/web/frontend && npx tsc -p tsconfig.e2e.json --noEmit)
(cd src/elspeth/web/frontend && npx playwright test --list)

# Dormant cohorts
HYPOTHESIS_PROFILE=nightly .venv/bin/python -m pytest -n0 -m 'not performance and not stress and not testcontainer' tests/property
.venv/bin/python -m pytest -n0 -m 'performance or stress' tests/performance

# Existing semantic static guard
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules all --root src/elspeth
```

Do not treat shape-only trust-tier verification as authoritative signing, and do not regenerate or hand-edit operator signatures during test remediation.

## Verification record and limitations

Completed in this audit:

- Current collection and marker census for Python, Vitest, and Playwright.
- Full local/default Python run under automatic xdist.
- Full serial `CI=true` Python baseline, the isolated xdist-failure reproduction, and bounded order challenges for the seven serial-only logging failures.
- Full Vitest run and configured frontend typecheck.
- E2E TypeScript typecheck reproduction.
- Targeted current tests and safe fault challenges across core, engine, plugins, web, property, frontend, CI, and integration surfaces.
- Current mutmut CLI and installed-source cache contract inspection.
- Current branch/worktree checks before and after audit actions.

Not completed by this audit:

- A full Playwright execution, avoided during audit because the harness writes a fixed shared auth-state file; collection and static harness review were completed.
- Testcontainer/PostgreSQL, performance/stress, live external-provider, live AWS, staging, OIDC, multiprocess, or full maintained-example execution.
- Any operator-held HMAC signing or signed-baseline regeneration.
