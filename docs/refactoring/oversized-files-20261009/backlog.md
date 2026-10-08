# Remaining oversized-file backlog

This is the explicit remaining backlog after the three local slices; it is not a completion claim for the oversized-file goal. The current measurement scope is committed `fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2` plus the frozen DAG overlay. The original inventory and earlier slice observations below remain historical evidence. Completed executor collection, parity, focused checks and independent review are recorded in [executor-slice.md](executor-slice.md) and [executor-review.md](executor-review.md). DAG size evidence is recorded here, with completed validation and independent review in [dag-slice.md](dag-slice.md) and [dag-review.md](dag-review.md).

The first processor extraction leaves `tests/unit/engine/test_processor.py` at **9,731 lines**, so that target remains unfinished. The extracted transform/telemetry module is 1,871 lines at the first-slice commit. All 25 original authored targets above 5,000 remained above 5,000 at that historical commit; the executor slice reduced the residual to 24, and the measured DAG continuation now leaves **23 original authored targets above 5,000**. The original executor module is 3,317 lines and the original DAG module is 2,955; all their extracted modules are below 5,000. **Eight original authored targets remain above 10,000**, all reserved to streaming literal or semantic ownership. The published streaming snapshot adds a 26th prospective authored target: `src/elspeth/web/execution/service.py`, at 5,315 lines. It is 4,853 lines on the main-based refactoring branch, and its streaming size must be retained in the backlog rather than disappearing from scope.

## Evidence and current-status boundaries

- Main baseline: `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`, from `docs/refactoring/oversized-files-20261009/inventory.csv.gz`; decompressed parsed records exactly matched a fresh main Git-blob inventory.
- Current committed base: `fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2`; freshly enumerated with the controlled Git-blob counting script. Current byte reads of the other 24 original authored targets exactly matched this commit; the DAG target uses the frozen overlay observation.
- Current DAG frozen-source manifest: `dag-slice/frozen-source-files.json`, SHA-256 `9f1c139d6865a0e5978a913a5a976954fcc44721862fc70523aa4855cbffb95c`. All four line counts and source hashes matched the implementing lane's manifest. A second byte read of every current target matched; this is observed stability, not an atomic locked snapshot. No pre-readiness count was used as final evidence.
- First-slice historical commit: `794531426a48ed6666bb68a0b690126fe58690d0`. At the earlier executor observation, the other 24 baseline authored targets exactly matched that commit. This is historical byte-parity evidence, not an assertion about every future overlay.
- Historical executor frozen-source manifest: `executor-slice/frozen-source-files.json`, SHA-256 `6c9f618a229e0d2f549aefec38eb392421e7d8d95093024d2dddfd994900db55`. All six source hashes and line counts matched that lane's manifest. Its second byte reads matched; those observations were not an atomic locked snapshot. The current committed inventory independently records the completed executor sizes.
- Published PR281 snapshot: `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`; published sizes reconstructed from the reviewed baseline plus published snapshot deltas.
- Canonical streaming ownership at owner checkpoint 72: `streaming-owned-paths-20261008-538-post-caddy.txt`, SHA-256 `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`; measured 538 nonempty paths, all unique. Compared with the historical 536-file manifest, the exact additions are `CONTRIBUTING.md` and `tests/unit/helpers/test_composer_https.py`, with no removals.
- Historical streaming tracked-file manifest: `streaming-full-changelog-snapshot-20261008-1/tracked-file-manifest.json`, against the same main base, SHA-256 `d7e8e437f730534880586c41e8687057f8bc80a4996e8bdf18605ecb4184dee1`; 536 files and unique paths. It remains snapshot evidence; it is superseded by the canonical 538-path list for current literal ownership.
- A `No` canonical literal membership does **not** release semantically active Composer, Sessions, execution, coordination, or related gate surfaces. Broad streaming ownership still applies; scope needs integration or an explicit bounded release on a remeasured stable tree.
- Physical-line counting retains the controlled inventory rule: regular strict-UTF-8 non-NUL blobs, LF plus one nonterminated final line. No pytest, source execution, gates, commit, or source/test edit was performed by this backlog lane.

Historical original/first-slice count output:

```text
snapshot=main commit=9e481bc85a32ee15f808c5e2b16cab7634e57b8e entries=6133 text=6109 excluded=24 gt5000=36 gt10000=13
snapshot=first-slice commit=794531426a48ed6666bb68a0b690126fe58690d0 entries=6144 text=6119 excluded=25 gt5000=36 gt10000=12
changed_rows=12 oversized_overlap_rows=1
baseline_gzip_parsed_equality_with_fresh_main=True
manifest_files=536 manifest_unique_paths=536
original_authored_gt5000=25 first_slice_authored_gt5000=25
original_authored_gt10000=9 first_slice_authored_gt10000=8
exact_manifest_overlap_among_original_authored_gt5000=10
```

Current committed and ownership measurement:

```text
snapshot=second-slice commit=fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2 entries=6151 text=6126 excluded=25 gt5000=35 gt10000=12
changed_rows=21 oversized_overlap_rows=2
canonical_owned_paths=538 unique_paths=538
canonical_additions=['CONTRIBUTING.md', 'tests/unit/helpers/test_composer_https.py']
canonical_removals=[]
committed_authored_gt5000=24 committed_authored_gt10000=8
observed_after_DAG_authored_gt5000=23 observed_after_DAG_authored_gt10000=8
huge_literal_owned=6 huge_semantic_only_owned=2
```

The eight authored targets still above 10,000 are all reserved: six are literal members of the canonical list (`test_tools.py`, the Landscape fencing and Sessions mutation-authority gates, session routes, execution service tests, and Composer/runtime agreement); two remain semantically owned despite unchanged literal paths (`src/elspeth/web/composer/state.py` and `tests/unit/web/composer/test_state.py`). Their table rows retain full repository-relative identities. The ordinary processor test remains open at 9,731, so it is outside this eight-target >10,000 subset but still above the 5,000 target.

## One status table for every authored target

The baseline and published columns permit later integration to retain growing targets. The current column records the immutable second-slice commit for byte-identical targets and the measured frozen DAG overlay for the DAG row. “Reserved” indicates coordination status, not missing design. Canonical 538-path literal membership is distinct from semantic ownership. Counts are physical lines; the table retains every original authored target, including completed executor work and the measured DAG size target, plus the published execution-service target.

| Target | Main lines | Current lines | Published lines | Canonical 538 literal | Current status / next coherent slice |
|---|---:|---:|---:|---|---|
| `tests/unit/web/composer/test_tools.py` | 21212 | 21212 | 21212 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Mutation tools; canonical pipeline and credential refusals; blob/secret policy; discovery; preview/proof, keeping coherent refusal cases. |
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | 19572 | 19572 | 22439 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Typed policy inventory; AST/DML support; authority/transaction/escape proof families; retain live pins and adversarial tests. |
| `tests/unit/architecture/test_session_db_mutation_authority.py` | 19389 | 19389 | 19910 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Policy/manifest; lexical writer collector; provenance/wrapper/caller proof support; live versus adversarial assertions. |
| `tests/unit/web/sessions/test_routes.py` | 15361 | 15361 | 15891 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. CRUD/IDOR; acceptance/proposals; history/recompose; YAML/export; progress/cancellation; persistence/provenance. |
| `tests/unit/web/composer/test_state.py` | 14062 | 14062 | 14062 | No | Reserved to streaming semantic owner / coordinated post-integration split. Spec/serialization; Stage-1 structure; templates; schema contracts; queue/union/coalesce; field spelling/extras. |
| `tests/unit/web/execution/test_service.py` | 12067 | 12067 | 12367 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Admission/proof/fanout; execution infrastructure; cancellation/terminal recovery; paths/blobs/errors; accounting. |
| `tests/integration/pipeline/test_composer_runtime_agreement.py` | 10106 | 10106 | 10176 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Structural routing; schema/guarantee/union; templates/field spelling; execution/secrets/blobs. Preserve independent expected results. |
| `src/elspeth/web/composer/state.py` | 10054 | 10054 | 10054 | No | Reserved to streaming semantic owner / coordinated post-integration split. Leaf spec/value types → validation probes/topology/templates/schema; preserve immutable-state identity and ordered diagnostics. |
| `tests/unit/engine/test_processor.py` | 11561 | 9731 | 11561 | No | Remaining >5k after first extraction; engine/core follow-up. Move shared setup authority to fixtures, then constructor/navigation, row/source processing, scheduler/drain, group loss, aggregation/terminal routing. |
| `tests/unit/web/composer/test_service.py` | 9584 | 9584 | 9584 | No | Reserved to streaming semantic owner / coordinated post-integration split. Tool loop/cache; timeout/convergence/cancellation; provider audit/errors; persistence; preflight/final gates/repair. |
| `tests/unit/web/composer/test_pipeline_planner.py` | 9383 | 9383 | 9383 | No | Reserved to streaming semantic owner / coordinated post-integration split. Canonicalization/identity; planner responsiveness; tool/advisor evidence; schema withholding; structural facts. |
| `tests/unit/core/landscape/test_database_clock_authority.py` | 8243 | 8243 | 8243 | No | Authority-gate support; coordinate with active writer/fencing gate owners before extraction. Clock-source and fence analyzer support; separate live pins and adversarial source/first-fence suites. |
| `tests/unit/engine/test_executors.py` | 7432 | 3317 | 7432 | No | Completed local decomposition with focused validation and independent review. Transforms/security gate remain here; gate, aggregation and node-state groups moved to their actual modules; shared setup in `tests/fixtures/executors.py`. |
| `src/elspeth/web/sessions/service.py` | 7247 | 7247 | 8953 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Transcript/history; proposals; interpretation; run admission/accounting; receipt/fork, with explicit transaction authorities. |
| `tests/unit/web/execution/test_validation.py` | 6913 | 6913 | 6913 | No | Reserved to streaming semantic owner / coordinated post-integration split. Profile/network/path policy; credential refusals; real runtime materialization; semantic/edge contracts; diagnostics. |
| `tests/unit/architecture/test_dag_scenario_corpus_contract.py` | 6729 | 6729 | 6729 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Registry/manifest/schema; semantic projection; recovery evidence; independent case/harness parity. |
| `tests/unit/web/composer/test_advisor_checkpoint.py` | 6310 | 6310 | 6310 | No | Reserved to streaming semantic owner / coordinated post-integration split. Checkpoint/evidence summaries; termination/repair; disclosure/history replay; telemetry. Preserve per-transition provider calls. |
| `tests/unit/core/test_dag.py` | 6292 | 2955 | 6292 | No | Completed local decomposition with focused validation and independent review. Validation remains here; construction and routing moved to explicit modules with `tests/fixtures/dag.py` as setup authority. |
| `tests/unit/web/blobs/test_service.py` | 6246 | 6246 | 6250 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. CRUD/quota; custody/reconciliation; fork rollback; run-output finalization; read/verified streaming. |
| `elspeth-lints/src/elspeth_lints/core/cli.py` | 6110 | 6110 | 6110 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Parser/dispatch; lint reporting; allowlist/reaudit operations; signing-bundle/rekey diagnostics, preserving exact CLI output. |
| `tests/unit/core/test_config.py` | 5909 | 5909 | 5909 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Settings/loaders; gates/coalesce; secrets/redaction; env/templates and boundary refusals. |
| `src/elspeth/web/coordination/repository.py` | 5807 | 5807 | 5921 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Session/composition/run facets; blob custody; fork transactions; PostgreSQL authority specialization, preserving one transaction owner. |
| `src/elspeth/engine/processor.py` | 5523 | 5523 | 5523 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Aggregate completion; transform retry/error execution; group-loss settlement through explicit owned collaborators after test decomposition. |
| `tests/fixtures/dag_scenario_corpus/harness.py` | 5408 | 5408 | 5408 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Build/render; stable/semantic projections; export/sink evidence; recovery runners, preserving frozen oracle bytes. |
| `src/elspeth/cli.py` | 5278 | 5278 | 5278 | No | Proposed engine/core or CLI/lints follow-up; no independent release inferred. Bootstrap; run/validate/explain; resume/join/abandon; export verification; deployment/web command groups. |
| `src/elspeth/web/execution/service.py` | 4853 | 4853 | 5315 | Yes | Reserved to streaming semantic owner / coordinated post-integration split. Admission/materialization; worker lifecycle; cancellation/recovery; finalization/diagnostics through explicit leases and accounting. |

## Completed executor evidence and next sequencing

The executor continuation keeps transforms and the dedicated `TestReRaiseGuardPattern` security gate in the original file, with gate execution, aggregation, node-state guards, and shared setup in explicit modules. The implementing lane reported frozen source; these are the actual resulting names and measured byte counts. Every source hash and line count matched its manifest, and the second reads matched the first:

```text
tests/fixtures/executors.py: lines=227 bytes=7900 sha256=50547b0a7c4a91d692af3ad6c937833f14a2b3659307d8a0245a4d347447e11d frozen_manifest_match=True
tests/unit/engine/test_aggregation_executor.py: lines=1793 bytes=85343 sha256=b780c41e8acf4a0dd1132256c15ca6bedd13626609210a719809270841ef106a frozen_manifest_match=True
tests/unit/engine/test_node_state_guard.py: lines=832 bytes=36322 sha256=5b37a4f54ecf5b3cbee8245a78d89858b1305ceb7f23b3bedf4e40f17e7c91b1 frozen_manifest_match=True
tests/unit/engine/test_executors.py: lines=3317 bytes=153117 sha256=5f878e414c50e11b6b6dbbff7b2591e237b051d30ff1f4c5c25f037eee588642 frozen_manifest_match=True
tests/unit/engine/test_gate_executor.py: lines=1384 bytes=56369 sha256=227a0d85558cebf3957252a5a3e4bc295a6ae6b5e47c5672de4a5a737e500a69 frozen_manifest_match=True
tests/unit/engine/test_executor_authority_refusal.py: lines=73 bytes=3124 sha256=902d5191d1af2dff374e576af0e471ac13a81c66ee28b18a19faf4f61717a78c frozen_manifest_match=True
baseline_authored_targets=25 observed_residual_gt5000=24
other_24_baseline_authored_targets_byte_equal_to_first_slice_commit=True
observed_target_second_byte_reads_match=True; observations_not_atomic=True
```

The executor size target is met in this validated local slice; its linked report supplies collection, assertion-preservation and security-gate evidence. Publication and integration remain outside the authorization. Keep the security gate explicit and retain its template/process-boundary latch controls. A future reduction of any original file does not discharge its target if a resulting coherent unit still exceeds 5,000.

Then continue processor test decomposition until the remaining 9,731-line module and every resulting authored unit are at most 5,000. Move shared setup as one authority with all consumers, retain corpus-pinned classes where coherent, and handle real evidence locators together if they move. Do not turn the original into a wrapper, import discoverable test classes back, hide assertions in a generic loop, or change production behavior to repair a relocation.

The authorized DAG continuation retains validation in `tests/unit/core/test_dag.py` and moves construction to `test_dag_construction.py`, routing to `test_dag_routing.py`, and shared setup to `tests/fixtures/dag.py`. These final source names and counts were measured only after the readiness signal; every hash matched the lane's frozen-source manifest:

```text
tests/fixtures/dag.py: lines=233 bytes=9587 sha256=e7981f29ee261630f80ab5bec41241aeaf3f2ae4f5390854885eec4e53e273b2 frozen_manifest_match=True
tests/unit/core/test_dag.py: lines=2955 bytes=118026 sha256=dcfa2815bb593a0825dc173b69b2cc29534c79122b1752eedc43cf419ad0a834 frozen_manifest_match=True
tests/unit/core/test_dag_construction.py: lines=1611 bytes=64469 sha256=82b552bb5e5a330ba596f8103348479f5b191e523f258db9e207a4204ea3f08f frozen_manifest_match=True
tests/unit/core/test_dag_routing.py: lines=1536 bytes=61362 sha256=e491e6d38743893c07799c0c7d95eb448c9a910b25014b42bcf945c726fef981 frozen_manifest_match=True
baseline_authored_targets=25 observed_residual_gt5000=23
observed_residual_gt10000=8 literal_owned=6 semantic_only_owned=2
other_24_baseline_authored_targets_byte_equal_to_fb6c7185f=True
observed_target_second_byte_reads_match=True; observations_not_atomic=True
```

Core/config and CLI seams follow after dependency ownership is checked. Gate support follows Sessions collector → clock proof support → Landscape support, under one coordinated owner with full live-record and refusal-control parity. Composer and execution/Sessions production movement follows streaming integration and characterization. Keep the LLM/provider authoring path, immutable composition semantics, transaction/cancellation scopes, and PostgreSQL authority proofs intact.

The target remains all authored modules at most 5,000 lines and none above 10,000. Locks, generated report data, curated proof/policy artifacts, input data, and frozen baselines retain producer/consumer-specific treatment; no mechanical artifact splitting is proposed. Broad suite and PostgreSQL execution belongs to the integrator’s capacity slot when a later implementation changes the relevant runtime or persistence behavior.

## Outstanding completion evidence

- Executor final names, source hashes and per-module counts are measured above; local validation and review are complete in the linked report. Publication and integration are not performed.
- DAG final per-module counts, source hashes and original-target residual accounting are measured above; local validation and independent review are complete in the linked DAG reports. Publication and integration are not performed.
- Processor first-slice residual 9,731 lines remains scheduled work, despite crossing below 10,000.
- Streaming integration/release and remeasurement remain prerequisites for reserved semantic surfaces, including unchanged exact-manifest paths.
- This backlog does not claim whole-project decomposition completion, CI acceptance, or signed allowlist clearance.
