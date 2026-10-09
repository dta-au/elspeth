# Remaining oversized-file backlog on the published streaming base

This backlog uses actual rebased commit `df7deda0e31df7e5a50a85b9e16363893f41eb44` on published PR281 `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`, plus the validated configuration test split measured after its lane reported frozen source. The bounded split is complete and independently approved with no introduced findings; [config-slice.md](config-slice.md) and [config-review.md](config-review.md) record its evidence. The historical main-based inventory remains useful baseline evidence, but its sizes are not the current tree. This is not a whole-project completion claim.

## Current scope and measured evidence

- Current commit: `df7deda0e31df7e5a50a85b9e16363893f41eb44`, measured by the controlled Git-blob inventory script with self-test process exit 0.
- Config frozen-source manifest: `config-slice/frozen-source-files.json`, SHA-256 `dc4b1825814a393f3382b240ba3992c2ecc025e507d6571e070f2a9bc2ba4b99`. All three source hashes and line counts matched. All 26 target rows were remeasured; the other 25 union targets exactly matched committed rebase blobs. Second byte reads matched first reads; the observation is not an atomic locked snapshot.
- Exact parent base: published PR281 `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`; merge-base equality and positive ancestor control verified.
- Historical main: `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`, used only for the preserved baseline column and original 25-target universe.
- Current canonical streaming ownership: checkpoint 72 `streaming-owned-paths-20261008-538-post-caddy.txt`, SHA-256 `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`, 538 unique paths. It adds `CONTRIBUTING.md` and `tests/unit/helpers/test_composer_https.py` to the historical 536 list with no removals.
- The exact published base excludes repair/integrated `703cdd1674d01d422baa4d54f210105edccf498c` and its uncommitted repair overlay, main PR282 merge `85806a05e9b03d71e9dc793bbf45e0bd8150ddb7`, and main PR283 merge `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. All three exclusion ancestry checks exited 1. Do not imply that these fixes were integrated by rebasing onto the published branch.
- `tests/unit/helpers/test_composer_https.py` is the only canonical owned path absent from the published base. It remains reserved. Literal “No” never releases semantically active Composer, Sessions, execution, coordination, or related gate surfaces.
- Current inherited sizes now include execution production service 5,315, Landscape fencing 22,439, Sessions writer authority 19,910, routes 15,891, execution tests 12,367, Composer/runtime agreement 10,176, Sessions service 8,953, coordination repository 5,921 and blob tests 6,250.
- Physical lines use regular strict-UTF-8 non-NUL blobs, LF bytes plus one nonempty unterminated final segment. No production imports, test runs, repository gates, source edits or commits were performed by this backlog lane.

Raw current committed output:

```text
snapshot=published-pr281 commit=9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc entries=6393 text=6369 excluded=24 gt5000=38 gt10000=13
snapshot=rebased commit=df7deda0e31df7e5a50a85b9e16363893f41eb44 entries=6416 text=6391 excluded=25 gt5000=36 gt10000=12
changed_rows=27 oversized_overlap_rows=3
current_authored_gt5000=24 current_authored_gt10000=8
current_original_25_targets_gt5000=23
current_extra_execution_source_lines=5315
```

The committed **24 authored >5,000** count above is historical to the exact rebase commit: 23 original targets plus the execution service. After the frozen config split, the freshly measured 26-target universe leaves **23 authored targets above 5,000: 22 original targets plus the execution service at 5,315**. All three config modules are below 5,000. Eight authored >10,000 remain reserved: six literal paths (tool tests, fencing, Sessions writer authority, routes, execution tests, Composer/runtime agreement) and two semantic-only paths (`src/elspeth/web/composer/state.py` and `tests/unit/web/composer/test_state.py`). Processor tests remain open at 9,731 despite falling below 10,000.

## Every original target plus the published execution service

The 26-row table retains all three now size-resolved original targets. Historical main and published columns remain exact immutable snapshots. Current is the frozen config overlay for the config row and byte-identical rebased committed content for the other 25 rows. Ownership is the canonical 538-path literal list, distinct from broader semantic reservation.

| Target | Historical main lines | Published PR281 lines | Current lines | Canonical literal | Status / next coherent slice |
|---|---:|---:|---:|---|---|
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | 19572 | 22439 | 22439 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Typed policy inventory; AST/DML support; authority/transaction/escape proof families; retain live pins and adversarial tests. |
| `tests/unit/web/composer/test_tools.py` | 21212 | 21212 | 21212 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Mutation tools; canonical pipeline and credential refusals; blob/secret policy; discovery; preview/proof, keeping coherent refusal cases. |
| `tests/unit/architecture/test_session_db_mutation_authority.py` | 19389 | 19910 | 19910 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Policy/manifest; lexical writer collector; provenance/wrapper/caller proof support; live versus adversarial assertions. |
| `tests/unit/web/sessions/test_routes.py` | 15361 | 15891 | 15891 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. CRUD/IDOR; acceptance/proposals; history/recompose; YAML/export; progress/cancellation; persistence/provenance. |
| `tests/unit/web/composer/test_state.py` | 14062 | 14062 | 14062 | No | Reserved to streaming semantic owner / coordinated post-integration split. Spec/serialization; Stage-1 structure; templates; schema contracts; queue/union/coalesce; field spelling/extras. |
| `tests/unit/web/execution/test_service.py` | 12067 | 12367 | 12367 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Admission/proof/fanout; execution infrastructure; cancellation/terminal recovery; paths/blobs/errors; accounting. |
| `tests/integration/pipeline/test_composer_runtime_agreement.py` | 10106 | 10176 | 10176 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Structural routing; schema/guarantee/union; templates/field spelling; execution/secrets/blobs. Preserve independent expected results. |
| `src/elspeth/web/composer/state.py` | 10054 | 10054 | 10054 | No | Reserved to streaming semantic owner / coordinated post-integration split. Leaf spec/value types → validation probes/topology/templates/schema; preserve immutable-state identity and ordered diagnostics. |
| `tests/unit/engine/test_processor.py` | 11561 | 11561 | 9731 | No | Remaining >5k after first extraction; engine/core follow-up. Move shared setup authority to fixtures, then constructor/navigation, row/source processing, scheduler/drain, group loss, aggregation/terminal routing. |
| `tests/unit/web/composer/test_service.py` | 9584 | 9584 | 9584 | No | Reserved to streaming semantic owner / coordinated post-integration split. Tool loop/cache; timeout/convergence/cancellation; provider audit/errors; persistence; preflight/final gates/repair. |
| `tests/unit/web/composer/test_pipeline_planner.py` | 9383 | 9383 | 9383 | No | Reserved to streaming semantic owner / coordinated post-integration split. Canonicalization/identity; planner responsiveness; tool/advisor evidence; schema withholding; structural facts. |
| `src/elspeth/web/sessions/service.py` | 7247 | 8953 | 8953 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Transcript/history; proposals; interpretation; run admission/accounting; receipt/fork, with explicit transaction authorities. |
| `tests/unit/core/landscape/test_database_clock_authority.py` | 8243 | 8243 | 8243 | No | Authority-gate support; coordinate with active writer/fencing gate owners before extraction. Clock-source and fence analyzer support; separate live pins and adversarial source/first-fence suites. |
| `tests/unit/web/execution/test_validation.py` | 6913 | 6913 | 6913 | No | Reserved to streaming semantic owner / coordinated post-integration split. Profile/network/path policy; credential refusals; real runtime materialization; semantic/edge contracts; diagnostics. |
| `tests/unit/architecture/test_dag_scenario_corpus_contract.py` | 6729 | 6729 | 6729 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Registry/manifest/schema; semantic projection; recovery evidence; independent case/harness parity. |
| `tests/unit/web/composer/test_advisor_checkpoint.py` | 6310 | 6310 | 6310 | No | Reserved to streaming semantic owner / coordinated post-integration split. Checkpoint/evidence summaries; termination/repair; disclosure/history replay; telemetry. Preserve per-transition provider calls. |
| `tests/unit/web/blobs/test_service.py` | 6246 | 6250 | 6250 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. CRUD/quota; custody/reconciliation; fork rollback; run-output finalization; read/verified streaming. |
| `elspeth-lints/src/elspeth_lints/core/cli.py` | 6110 | 6110 | 6110 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Parser/dispatch; lint reporting; allowlist/reaudit operations; signing-bundle/rekey diagnostics, preserving exact CLI output. |
| `src/elspeth/web/coordination/repository.py` | 5807 | 5921 | 5921 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Session/composition/run facets; blob custody; fork transactions; PostgreSQL authority specialization, preserving one transaction owner. |
| `tests/unit/core/test_config.py` | 5909 | 5909 | 2156 | No | Completed bounded test decomposition with focused validation and independent approval; no introduced findings. Security/boundary tests remain here; settings and loading groups live in explicit modules. All three modules are below 5,000. |
| `src/elspeth/engine/processor.py` | 5523 | 5523 | 5523 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Aggregate completion; transform retry/error execution; group-loss settlement through explicit owned collaborators after test decomposition. |
| `tests/fixtures/dag_scenario_corpus/harness.py` | 5408 | 5408 | 5408 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Build/render; stable/semantic projections; export/sink evidence; recovery runners, preserving frozen oracle bytes. |
| `src/elspeth/web/execution/service.py` | 4853 | 5315 | 5315 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Admission/materialization; worker lifecycle; cancellation/recovery; finalization/diagnostics through explicit leases and accounting. |
| `src/elspeth/cli.py` | 5278 | 5278 | 5278 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Bootstrap; run/validate/explain; resume/join/abandon; export verification; deployment/web command groups. |
| `tests/unit/engine/test_executors.py` | 7432 | 7432 | 3317 | No | Completed local decomposition with focused validation and independent review. Transforms/security gate remain here; gate, aggregation and node-state groups moved to their actual modules; shared setup in `tests/fixtures/executors.py`. |
| `tests/unit/core/test_dag.py` | 6292 | 6292 | 2955 | No | Completed local decomposition with focused validation and independent review. Validation remains here; construction and routing moved to explicit modules with `tests/fixtures/dag.py` as setup authority. |

## Local slices and measured config continuation

Existing processor, executor and DAG reports record their original collection, assertion-preservation, focused checks and review. Their main-based counts are historical; current committed sizes were remeasured after rebase. Their resulting modules remain substantive tests rather than import-forwarding shells. [streaming-rebase.md](streaming-rebase.md) and [streaming-rebase-review.md](streaming-rebase-review.md) record the completed `focused.exit: 0` with `627 passed`, plus `gates.exit: 1` with `2 failed, 24 passed`. Both failing gates reproduced with identical complete diagnostics on the untouched published base. The separate completed config evidence follows; this count lane ran none of those checks.

- Processor test residual: `test_processor.py` 9,731; `test_processor_transform_execution.py` 1,871. The residual is explicitly unfinished. Its shared setup and corpus-pinned classes still need a coherent follow-up.
- Executor size-resolved modules: original 3,317; gate 1,384; aggregation 1,793; node-state guard 832; fixture 227; authority-refusal consumer 73. Keep the dedicated security gate and process-boundary controls explicit.
- DAG size-resolved modules: validation 2,955; construction 1,611; routing 1,536; fixture 233. Keep one fixture authority and preserve the real producer/runtime assertions.
- Config split: committed input 5,909; measured security/boundary original 2,156, settings 2,151, loading 1,613. Counts and hashes below were measured only after lane readiness. Focused execution, collection/source parity and boundary checks are complete; [config-slice.md](config-slice.md) and [config-review.md](config-review.md) record independent approval with no introduced findings.

Frozen-source measurements and residual accounting:

```text
tests/unit/core/test_config.py: lines=2156 bytes=90656 sha256=f8ae794fbb9ac636e16d714d499527e9f3e84ba94fb8d748131dc28a1ba1b0ea frozen_manifest_match=True
tests/unit/core/test_config_settings.py: lines=2151 bytes=85551 sha256=23e556da6fbdb4309d10d2299e16ff1c5831904328c6220276cbe77a04770b0b frozen_manifest_match=True
tests/unit/core/test_config_loading.py: lines=1613 bytes=52660 sha256=35c0314fad3b6da0dfa955b2c7222a5f3eb36165df4147215eba8afe17730b43 frozen_manifest_match=True
config_frozen_source_manifest_sha256=dc4b1825814a393f3382b240ba3992c2ecc025e507d6571e070f2a9bc2ba4b99
original_25_target_residual_gt5000=22
published_extra_gt5000=1 current_extra_lines=5315
observed_26_target_residual_gt5000=23
observed_residual_gt10000=8 literal_owned=6 semantic_only_owned=2
other_25_current_union_targets_byte_equal_to_rebased_df7deda=True
observed_target_second_byte_reads_match=True; observations_not_atomic=True
```

These measurements cover every original target, the published execution-service extra and all resulting config modules. The linked implementation and independent review provide the bounded collection/definition and execution evidence; clean integration gates are not claimed. A future split is not size-complete if any resulting coherent unit still exceeds 5,000. Report total authored residual and original-25 residual separately because execution service is now oversized in the selected base.

Completed config validation, reported from the integrator's raw logs and independently verified:

```text
baseline.exit: 0; 340 passed in 4.51s
baseline-collection.exit: 0; 340 tests collected in 0.20s
after-collection.exit: 0; 340 tests collected in 0.19s
after.exit: 0; 340 passed in 4.43s
339 recursive definitions: complete source and location-free AST equality=True
340 collected IDs: complete multiset equal after approved module-prefix mapping=True
baseline-boundary.exit: 0
after-boundary.exit: 0; complete output byte-equal to baseline=True
gates.exit: 1; 2 failed, 18 passed in 103.43s
before.json == after.json: true, including HEAD
canonical Python inputs: 3983
input-dictionary SHA-256: f43c45c6f13f21899d85b00351fa64ddab9b0bd50b5b8dc1ca8827b4b7c7bfb8
complete 11 masquerade finding tuples equal clean published base=True
complete three-mock diagnostic equal clean published base=True
independent review: approved for bounded split; no introduced findings
```

Both affected integration gates remain red on the exact inherited published-base findings. Their full diagnostics were compared, not only totals, and no gate/policy suppression was added. Boundary checks are keyless and do not establish operator signature clearance. This completed isolated test split does not certify a full repository or PostgreSQL suite.

## Next coherent work and blockers

Continue the 9,731-line processor test decomposition under its existing setup/corpus locator constraints. Ordinary core/config tests and CLI work can follow checked dependency ownership. Authority support moves in Sessions collector → clock proof → Landscape order under one coordinated owner; proof inputs, live records, pins and refusal behavior must remain intact. Composer and execution/Sessions production moves wait for streaming integration or a bounded explicit release on a remeasured tree. Preserve the provider authoring path, immutable composition semantics and transaction/cancellation authorities.

The goal remains every authored module at most 5,000 and none above 10,000. Curated policy/proof records, lockfiles, baseline fixtures, generated reports and data keep producer/consumer-specific maintenance; they are not mechanical code-splitting targets. Broad suite capacity and PostgreSQL proofs remain the integrator’s responsibility for changes that reach those domains.

Remaining blockers and work: inherited unspecced-mock and masquerade baseline integration-gate failures; streaming reserved surfaces and integration of later base omissions if their owner requires them; the 23 oversized authored targets listed above, including processor tests at 9,731. Config local validation and independent review are complete. The rebase and config reviews approve their bounded scopes but do not clear those two inherited gates or certify a full suite/PostgreSQL run. This document does not claim whole-project decomposition, remote publication, CI acceptance or operator signature clearance.
