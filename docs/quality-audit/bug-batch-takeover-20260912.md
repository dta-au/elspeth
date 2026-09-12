# Bug-batch takeover and diagnostic completeness

Target: `release/0.8.1`. This report is in progress, not a release-readiness certificate.

## Integration state

- Original batch: `7379e1572b426ad0d9727186e2a6f37fcdaf59f9`.
- Reviewed heartbeat/CLI and regression repairs: `e064ecd52ad04608bc4a3402af7c826b78b7f40f`.
- Release synchronization into the task branch: `0966eda0f17c78e97aff0c9ec2265baf0f8dae67`.
- Release integration and structural remediation: pending.

The original four unstaged heartbeat/error files were preserved and reviewed.
The historical workflow's three-file report omitted the contract test file.
Its final review results were recovered separately from its stale implementation
summary. Historical test results were not accepted as current verification.

## Exact original-batch review scope

Complete-file review was divided between the coordinator and engine/web
reviewers. The committed batch changed these 26 paths; this is a Git inventory,
not a source-text estimate.

| Path | Disposition |
| --- | --- |
| `src/elspeth/contracts/errors.py` | Required emitted index is serialized; pending heartbeat observation now reaches consumers. |
| `src/elspeth/engine/_error_hash.py` | Exact empty-string test preserves hashing of falsey, nonempty string subclasses. |
| `src/elspeth/engine/executors/__init__.py` | Removed internal facade export; callers import its defining module. |
| `src/elspeth/engine/executors/declaration_dispatch.py` | Rejects invalid plugin names explicitly, without fabricating identity. |
| `src/elspeth/engine/executors/schema_config_mode.py` | Output position reaches structured audit evidence. |
| `src/elspeth/engine/orchestrator/aggregation.py` | Exception documentation matches implementation. |
| `src/elspeth/engine/orchestrator/ceremony.py` | Uses the existing canonical completion-status mapping. |
| `src/elspeth/engine/orchestrator/cleanup.py` | Actual run identity reaches cleanup logs. |
| `src/elspeth/engine/orchestrator/graph_registration.py` | Removed unused forwarding; graph metadata still owns transform identities. |
| `src/elspeth/engine/orchestrator/heartbeat.py` | Restored observation production and completed exception/CLI consumption. |
| `src/elspeth/engine/orchestrator/preflight.py` | Removed unused validator input; getter has no side effects. |
| `src/elspeth/engine/orchestrator/resume.py` | Removed unused forwarding; GraphArtifacts retains identities. |
| `src/elspeth/engine/orchestrator/source_iteration.py` | Removed idle-helper arguments; lifecycle attribution remains. |
| `src/elspeth/engine/orchestrator/validation.py` | Removed unused parameters; error-sink validation remains. |
| `src/elspeth/web/auth/routes.py` | Rejects multiple-at-sign email input. |
| `src/elspeth/web/blobs/service.py` | Enforces filename byte cap with oversized ASCII and Unicode suffixes. |
| `tests/unit/core/landscape/repository_integration/test_schema_config_mode_serialization_roundtrip.py` | Added nonzero output-position proof through Landscape and explain. |
| `tests/unit/engine/orchestrator/test_cleanup_failure_ceremony.py` | Checks emitted run identity. |
| `tests/unit/engine/orchestrator/test_resume_failure.py` | Fake context carries the real required run identity. |
| `tests/unit/engine/orchestrator/test_validation.py` | Removed obsolete inputs; rejection assertions retained. |
| `tests/unit/engine/test_declaration_dispatch.py` | Invalid truthy name gets the informative framework exception. |
| `tests/unit/engine/test_error_hash.py` | Falsey nonempty string distinguishes truthiness from emptiness. |
| `tests/unit/engine/test_executors.py` | Correct imports; replaced an empty test and added cross-collaborator ordering assertions. |
| `tests/unit/engine/test_schema_config_mode_contract.py` | Index-zero checks retained; supplemented by nonzero consumer proof. |
| `tests/unit/web/auth/test_routes.py` | Positive/negative email controls. |
| `tests/unit/web/blobs/test_service.py` | Byte-cap controls for ASCII and Unicode. |

Additional repair paths are `src/elspeth/cli.py`,
`tests/unit/contracts/test_errors.py`,
`tests/unit/engine/orchestrator/test_run_heartbeat_thread.py`, and
`tests/integration/cli/test_coordination_loss_diagnostics.py`.

## Repair findings and evidence

The heartbeat exception previously contradicted a foreign-seat snapshot whose
worker membership was still active. Reason-present messages now say that the
worker lost coordination; the no-reason message remains byte-identical. The
first observation is assigned before Event publication and is not overwritten.
Real run/resume/join handlers now carry the observation to console output and
JSON; JSON adds a reason only when one was supplied.

The existing emitted-index tests passed with the actual index replaced by zero.
Four added cases exercise both dispatcher paths, place the invalid output after
one or two valid outputs, and check the persisted index through explain. All
four fail under the constant-zero mutation.

Executor tests now observe registration, acceptance, and waiting in one ordered
sequence. The empty-buffer test opens real batch membership and asserts the
specific refusal before execution. One duplicate test with an inaccurate
configuration-validation claim was removed; it did invoke the executor, so it
was not literally a test of only its own mock assignment.

| Verification | Recorded result |
| --- | --- |
| Original pending heartbeat/contracts baseline | Exit 0; 108 passed. |
| Drop heartbeat reason wiring | Exit 1; five failed, 103 passed. |
| Replace heartbeat reason with a constant | Exit 1; five failed, 103 passed. |
| Remove one-shot publication guard | Exit 1; two failed, 106 passed. |
| Expanded CLI consumer constant-reason mutation | Twelve failing CLI cases; six no-observation controls pass. |
| Combined repaired focused tests | Exit 0; 373 passed. |
| Release-synced focused tests, including release docs and dispatcher | Exit 0; 402 passed. |
| Repaired mypy run | Exit 0; no issues in 946 source files. |
| Baseline default whole-tree suite | Exit 1; 21 failed, 50,657 passed, 87 skipped, two xfailed. |
| Baseline PostgreSQL stage | Deliberately interrupted; exit 143, not verified. |
| Baseline freeze check | Frozen=yes; overall RESULT=FAIL. |

These are separate runs, not additive counts. Five failures under each original
heartbeat reason mutation were all heartbeat tests, not contract tests as the
historical narrative claimed. Removing the empty-buffer guard produced the
wrong exception rather than an assertion failure; that experiment is not
strong assertion-level mutation evidence.

The four baseline release-document failures disappear with release
synchronization. The other 17 are DAG oracle failures. A representative case
fails on both original candidate and release: expected node metadata says
`engine:0.8.0`, while the runtime emits `engine:0.8.1`. The narrow repair updates
eight node-version records across seven cases and their case-registry digest.
It leaves behavioral expectations and frozen semantic snapshots unchanged.
Verification of the final candidate remains required.

That correction touches only
`docs/architecture/dag/scenario-corpus/v1/manifest.yaml` and
`tests/unit/architecture/test_dag_scenario_corpus_contract.py`. Reversing the
eight version values in the parsed candidate recovers the original complete
case-registry digest exactly. The isolated correction passes the 17 original
failed DAG nodes plus registry checks (23 tests, exit 0). No frozen-oracle
writer was used.

## Structural remediation remaining

| Site | Current assessment and required work |
| --- | --- |
| `WriteLockHeldError.workers` | Production CLI consumption is missing. Revisit prior privacy decision; registered workers are candidates, not proven lock holders. Provide an appropriate operator surface with host/PID context. |
| `SessionCheckoutMismatchError.active_session_id` | Trace and repair actual MCP refusal output, including no-active-checkout versus another session. |
| Dataverse counters | Establish partial/failed/exhausted lifecycle semantics and a real typed telemetry consumer; a stale SourceCompleted TODO is not an event contract. |
| `NonResumableRunError` | Historical byte-identical JSON claim is false: free-text reasons differ. A stable machine-readable cause is still missing. |
| General exception-field guard | Implement controlled discovery plus executable consumer/exclusion contracts; printing every field or finding lexical reads is insufficient. |

Related leads include Dataverse's uncounted locked-contract rejection, RAG's
raw-dictionary telemetry emission, and provider preflight status loss. Their
production consumers must be reproduced before treating synthetic serializer
examples as confirmed end-to-end defects. Older decisions may be revisited on
current evidence; privacy and explicit safety constraints still apply.

## Gate limitations and custody

Key-free diagnosis of the reviewed heartbeat/dispatcher signed entries is
identical between release and the original candidate. Existing identity,
missing-finding, and AST-path failures are inherited. This is shape/source
binding diagnosis, not HMAC verification. No signatures were changed or staged.

The explicit plugin-name type rejection adds an R5 lint finding. It preserves
the old implicit string rejection while making invalid owned audit identity
informative. Keep it ready for narrow adjudication; do not label owned data as
Tier 3 or distort code to evade the lint. Final drift comparison is pending.

The attempted code-map refresh failed at its entity-count cap and published no
usable index. Current Git and direct source inspection were used instead.
Temporary logs and detailed reviewer reports remain in the ignored takeover
lane; this report records durable conclusions, not a signed plan package.
