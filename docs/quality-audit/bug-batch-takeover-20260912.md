# Bug-batch takeover and diagnostic completeness

Target: `release/0.8.1`. This report is in progress, not a release-readiness certificate.

## Integration state

- Original batch: `7379e1572b426ad0d9727186e2a6f37fcdaf59f9`.
- Reviewed heartbeat/CLI and regression repairs: `e064ecd52ad04608bc4a3402af7c826b78b7f40f`.
- Release synchronization into the task branch: `0966eda0f17c78e97aff0c9ec2265baf0f8dae67`.
- DAG version-oracle correction: `e19a1a23cce65bca5a59104fb94f81d8b48cfa7f`.
- Acceptance version and plugin hash repairs: `5e911e6f8c903d24ab4ec5c80fbd88b33d7616c3`.
- Initial release integration: `d1b473c8f846f9ed7dc1da1d6c2dd6641abfed8a`, fast-forwarded into both the original bugfix branch and `release/0.8.1`.
- Structural remediation: in progress in an isolated task worktree; not yet integrated.

The initial integration preserves the exact fully gated tree
`6c34800f09d0e6be6c9aef8c78ee8df19d49fc78`. The six-stage gate records
default-suite exit 0 (50,512 passed, 87 skipped, two expected failures),
PostgreSQL exit 0 (394 passed, one skipped), `frozen=yes`, and `RESULT=PASS`.
Ruff, mypy, and contract checks pass. Trust-tier lint remains in its documented
key-free fail-closed state, with the controlled delta described below.
After integration, all changed test files were run on the release checkout
with both import roots verified: exit 0, 1,516 passed, one PostgreSQL-URL-dependent
skip. The full PostgreSQL gate was run separately. Unrelated untracked files
were preserved; no push, signing, or worktree cleanup was performed.

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
| Release-synced DAG suite | Exit 0; 661 passed, two existing retirement-comparator skips. |
| Whole-tree default suite at `e19a1a23c` | Exit 1; 10 failed, 50,499 passed, 87 skipped, two xfailed. |
| PostgreSQL suite at `e19a1a23c` | Exit 0; 394 passed, one skipped; frozen=yes, overall RESULT=FAIL. |
| Whole-tree default suite at `5e911e6f8` | Exit 1; three failed, 50,509 passed, 87 skipped, two xfailed. |
| PostgreSQL suite at `5e911e6f8` | Exit 0; 394 passed, one skipped; frozen=yes, overall RESULT=FAIL. |
| Release plugin-hash failure reproduction | Exit 1; exact failing test reproduces both stale declarations. |
| Repaired plugin-contract module | Exit 0; 29 passed. |
| Combined gate-repair focused selection | Exit 0; 334 passed. |
| Isolated acceptance and release-version selection | Exit 0; 313 passed after restoration. |
| Remove acceptance candidate-version checks | Exit 1; four rejection assertions fail. |

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
The later final gate and integration supersede this intermediate failure.

That correction touches only
`docs/architecture/dag/scenario-corpus/v1/manifest.yaml` and
`tests/unit/architecture/test_dag_scenario_corpus_contract.py`. Reversing the
eight version values in the parsed candidate recovers the original complete
case-registry digest exactly. The isolated correction passes the 17 original
failed DAG nodes plus registry checks (23 tests, exit 0). No frozen-oracle
writer was used.

The next whole-tree run exposed release-inherited gate drift: one test rejects
stale JSONSource and ChromaSink source hashes after release comment edits;
nine AWS/Azure acceptance tests use compatibility fixtures still bound to
`0.8.0`. Those repairs were committed in a separate task worktree. The source changes are
exactly two hash literals computed by the existing canonical hash function,
not signature changes. Positive fixtures use the current package authority;
explicit stale-version rejection controls preserve independent negative
coverage. Production acceptance validators are unchanged.

The exact additional gate-repair paths are:

- `src/elspeth/plugins/sources/json_source.py`
- `src/elspeth/plugins/sinks/chroma_sink.py`
- `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py`
- `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`
- `tests/unit/web/azure_container_apps_acceptance/test_facade_contract.py`
- `tests/unit/web/azure_container_apps_acceptance/test_receipt_contracts.py`
- `docs/runbooks/azure-container-apps-deployment.md`

The full gate on that repair commit exposed three DAG failures: exact plugin
provenance, the JSON explode parent-child production path, and the B3 exact
contract check. The first explicitly reports the old JSONSource hash in the
manifest against the corrected declaration. The dependent oracle update was
missed in the hash repair. A real scenario projection comparison establishes
that all three failures arise from that single provenance value: replacing
only the actual source hash recovers the entire expected projection. The
follow-through updates that one manifest value and the complete case-registry
digest. Reversing only that hash recovers the previous digest exactly. No
resumed projection digest depends on this case; the complete isolated corpus,
production-path, and frozen-oracle selection passes (exit 0; 661 passed, two
existing skips). No frozen semantic oracle is rewritten.
The coordinator's actual-candidate corpus and production-path run also exits
0 (611 passed), with both import roots verified inside the worktree.

Those two failed gate runs kept their trees frozen; neither is an overall
passing result. The subsequent gate on `d1b473c8f` passed and that exact commit
was integrated, as recorded above.

## Structural remediation under verification

| Site | Disposition in the structural candidate; not yet integrated |
| --- | --- |
| `WriteLockHeldError.workers` | Local CLI renders registration candidates with worker/role/status/PID/host, including unknown values. Generic exception text stays redacted. Registration is not proof of lock ownership, and no SIGKILL instruction is inferred. |
| `SessionCheckoutMismatchError.active_session_id` | MCP emits the requested and active identities, with explicit null for no checkout, and persists the same safe refusal in its audit sidecar without changing session state. |
| Dataverse counters | Typed observation statistics distinguish not-started, partial, exhausted and failed loads; counts advance after audit recording, including locked-contract rejection. They are not run-success or cross-resume totals. |
| `NonResumableRunError` | Historical byte-identical JSON claim is false: free-text reasons differ. Required nominal causes now survive operational refusal producers and CLI rendering without parsing text or re-reading mutable state to reconstruct the decision. |
| General exception-field guard | Bounded live constructor discovery reconciles an exact field partition with executable message, structured, operator, control and privacy cases. Imported base fields and arbitrary Python dataflow are outside its stated scope. |

The initial real MCP tests reproduced identical class-only refusals for three
checkout states. Candidate SDK and durable JSONL tests now distinguish all
three, preserve unchanged session bytes/versions, and retain unexpected faults
as plugin crashes. Dropped keys, constant identities and incorrect audit-status
mutations fail their respective consumer assertions.

The wider telemetry trace reproduced raw-dictionary failures from RAG and
Chroma through the actual telemetry manager/exporter. Both now emit typed
statistics, and the callback contract is narrowed across the affected plugin
seams. Explicit lifecycle filtering and the OTLP attribute allowlist are wired
and tested; generic serialization alone would not have preserved those fields.
No row values, queries, collection names or endpoint URLs enter the new events.
Unequal RAG observations (two queries, three chunks, one score sample) protect
against swapped counters. Dataverse tests cover real normal completion,
source failure and downstream early termination through the orchestrator.

The census also removes two genuine unused fields: constant
`CapacityError.retryable` and redundant `RuntimePreflightFailedError.status_code`.
Actual pool decisions and timeout result diagnostics protect the retained
retryability/status contracts. Additional consumer exercises cover shutdown
summary counters and equal-total destination maps, coalesce failure metadata
persisted in Landscape, and failed-turn diagnostics from the audit producer
through the registered web exception handler. These are named seams, not a
claim that each test executes a complete pipeline or HTTP stack.

Provider preflight HTTP status is retained in audited child calls and safe
operation text, but omitted from typed MCP operation-call details. Further
contract review classifies that projection as an additive API/schema feature,
not a demonstrated breach of the existing call-detail contract. Opaque call
payloads cannot safely be treated as nominal HTTP error records by matching
keys or discriminator strings. Do not introduce a reader heuristic to retain
an unused exception field: remove the redundant wrapper status field while
preserving the original cause and audited evidence. A dedicated typed HTTP
status channel would require a separately scoped schema change.

The raw commencement-gate context snapshot is
an evidenced privacy exclusion, not a proven production consumer: an actual
CLI negative-disclosure test passes and its disclosure control fails. The
exception now carries a local explanatory note and maintained consumer coverage.
Older decisions may be revisited on current evidence; privacy and explicit
safety constraints still apply.

Operational cause coverage does not demote durable-integrity backstops:
the immutable-success acquisition guard still raises `AuditIntegrityError`.
The candidate does not promise every possible admission failure has a cause
code. Checkpoint compatibility follow-through removes the dead
`IncompatibleCheckpointError` carrier and a catch around an advisory reader
that never raised it. The actual workset-read refusal now uses the existing
typed operational exception. Real CLI tests change persisted format after the
advisory checks, then restore it before rendering: the observed refusal survives
both changes without reclassification.

The guard's current live inventory is 48 local exception classes, 92 directly
owned fields and 100 executable cases. The discovered roster is not a hardcoded
count: new or removed classes/fields must reconcile with the case partition.
The helper rejects unsupported generated constructors, dynamic writes and
receiver escapes rather than silently treating them as covered. It recognizes
the existing nominal audit wrapper and inherited rendering explicitly.
Message and serializer oracles also run dropped/constant-output controls;
privacy oracles run deliberate disclosure controls. The scope is these three
modules and their reviewed consumer obligations, not whole-program taint
analysis or proof that every caller invokes every available serializer.

### Cross-tree follow-through

The canonical soft-mapping checker first failed on one genuine retirement:
RAG's raw completion payload is now a typed event. Regeneration changes only
that file's `dict[str, Any]` count (six to five) and the corresponding totals
(2,704 to 2,703); no soft form was traded for another.

The coordination fencing guard also correctly rejected the new refusal
constructors. A controlled AST comparison shows exactly three added cause
arguments across its two acquisition recipes; removing only those arguments
recovers the old executable AST exactly. The recipes and explicit enum
dependency bindings are updated without changing locking or writer authority.
The subsequent complete fencing module run passed 567 tests and failed only
the caller-identity pin. Its full scanner comparison attributes that drift to
six Dataverse validation-error calls moving from `load` into `_load_rows`:
275 total callers remain, and reversing only those six owner names recovers
the previous complete digest. The corrected pin test passes independently.

The real plugin registry verifies every registered class's declared hash
against the canonical source hash. Seven edited concrete plugin hashes change;
the four additional classes inheriting callback-only base edits retain their
correct class-local hashes. None of the seven old hashes occurs in the parsed
DAG manifest; known-present JSONSource and absent-token controls validate that
lookup. No DAG behavioral expectations or frozen semantic oracles were moved
for the structural changes.

The eleven original batch tracker closures now include the freshly verified
release integration and full-gate evidence, while retaining their historical
branch-only verification. The heartbeat issue is closed on the integrated
commit. Structural work remains uncommitted until its final review and gates.

## Gate limitations and custody

Key-free diagnosis of the reviewed heartbeat/dispatcher signed entries is
identical between release and the original candidate. Existing identity,
missing-finding, and AST-path failures are inherited. This is shape/source
binding diagnosis, not HMAC verification. No signatures were changed or staged.

The explicit plugin-name type rejection adds an R5 lint finding. It preserves
the old implicit string rejection while making invalid owned audit identity
informative. Keep it ready for narrow adjudication; do not label owned data as
Tier 3 or distort code to evade the lint. At `e19a1a23c`, a controlled normalized
multiset comparison found only that additional finding versus release
(1,849 versus 1,848); selected signed-entry diagnoses remained identical.
At `5e911e6f8`, the controlled normalized multiset comparison is 1,847 versus
release's 1,848: the same added R5 and removal of the two repaired plugin-hash
findings. Identical-input and synthetic-change controls pass. The nine selected
signed-entry diagnoses have identical keys, statuses, details, and notes versus
release; this remains key-free diagnosis, not signature authentication.

The structural candidate's final key-free corpus contains 1,855 findings
against 1,847 on the integrated initial release. Controlled normalized
multiset comparison finds eight additions and no removals: four R6 findings
in the changed MCP/Chroma exception handling, the nominal Bedrock event check's
R5 finding, two stale MCP allowlist bindings, and one unused Chroma per-file
binding. Three R6 sites are existing handled-error paths whose bindings moved;
the new checkout refusal is explicitly returned to the client and persisted
in audit. Tests verify both handling and disclosure, rather than distorting
correct code to hide the findings. The Bedrock check enforces an owned type,
not Tier-3 parsing. These changes require narrow operator adjudication/signing
at package completion; no allowlist or signature metadata was edited, no
global bundle was staged, and shape-only results are not HMAC verification.

The attempted code-map refresh failed at its entity-count cap and published no
usable index. Current Git and direct source inspection were used instead.
Temporary logs and detailed reviewer reports remain in the ignored takeover
lane; this report records durable conclusions, not a signed plan package.

## Exact structural candidate paths

This Git-derived inventory includes implementation, regression fixtures,
cross-tree pin follow-through and this report. Source and related tests were
reviewed across the coordinator and independent CLI/telemetry readers; the
new guard has separate discovery and executed-obligation controls.

```text
config/cicd/soft-mapping-census.yaml
docs/quality-audit/bug-batch-takeover-20260912.md
src/elspeth/cli.py
src/elspeth/composer_mcp/server.py
src/elspeth/composer_mcp/session.py
src/elspeth/contracts/__init__.py
src/elspeth/contracts/checkpoint.py
src/elspeth/contracts/contexts.py
src/elspeth/contracts/coordination.py
src/elspeth/contracts/errors.py
src/elspeth/contracts/events.py
src/elspeth/contracts/plugin_context.py
src/elspeth/core/checkpoint/__init__.py
src/elspeth/core/checkpoint/compatibility.py
src/elspeth/core/checkpoint/manager.py
src/elspeth/core/checkpoint/recovery.py
src/elspeth/core/landscape/run_coordination_repository.py
src/elspeth/engine/orchestrator/abandon.py
src/elspeth/engine/orchestrator/export.py
src/elspeth/engine/orchestrator/resume.py
src/elspeth/engine/processor.py
src/elspeth/plugins/infrastructure/telemetry.py
src/elspeth/plugins/sinks/chroma_sink.py
src/elspeth/plugins/sources/dataverse.py
src/elspeth/plugins/transforms/aws/_guardrail_transform.py
src/elspeth/plugins/transforms/aws/textract_document_analysis.py
src/elspeth/plugins/transforms/aws/textract_inline_analysis.py
src/elspeth/plugins/transforms/azure/base.py
src/elspeth/plugins/transforms/azure/document_intelligence.py
src/elspeth/plugins/transforms/llm/transform.py
src/elspeth/plugins/transforms/rag/transform.py
src/elspeth/telemetry/__init__.py
src/elspeth/telemetry/filtering.py
src/elspeth/telemetry/serialization.py
src/elspeth/web/_aws_ecs_acceptance/bedrock.py
tests/e2e/recovery/test_crash_and_resume.py
tests/fixtures/abandon_refusal_diagnostics.py
tests/fixtures/exception_audit_consumer.py
tests/fixtures/exception_coalesce_consumer.py
tests/fixtures/exception_diagnostic_cases.py
tests/fixtures/exception_diagnostic_consumers.py
tests/helpers/exception_diagnostics.py
tests/integration/core/dag/test_dag_scenario_production_path.py
tests/integration/pipeline/test_audit_export_effect_recovery.py
tests/integration/pipeline/test_eof_resume_proof.py
tests/integration/plugins/llm/test_source_pipeline.py
tests/integration/plugins/test_dataverse_statistics.py
tests/integration/test_exception_diagnostic_consumers.py
tests/invariants/test_exception_diagnostic_contracts.py
tests/unit/architecture/test_web_landscape_mutation_fencing.py
tests/unit/cli/test_abandon_command.py
tests/unit/cli/test_abandon_refusal_causes.py
tests/unit/cli/test_cli.py
tests/unit/cli/test_commencement_privacy.py
tests/unit/cli/test_refusal_causes.py
tests/unit/cli/test_write_lock_diagnostics.py
tests/unit/composer_mcp/test_checkout_diagnostics.py
tests/unit/contracts/test_checkpoint.py
tests/unit/contracts/test_errors.py
tests/unit/core/checkpoint/test_compatibility.py
tests/unit/core/checkpoint/test_group_satisfiability_gate.py
tests/unit/core/checkpoint/test_recovery.py
tests/unit/core/landscape/test_run_coordination_repository.py
tests/unit/engine/orchestrator/test_abandon_leaderless_run.py
tests/unit/engine/orchestrator/test_resume_entry_guard.py
tests/unit/engine/orchestrator/test_resume_failure.py
tests/unit/engine/orchestrator/test_run_status.py
tests/unit/helpers/test_exception_diagnostics.py
tests/unit/plugins/llm/test_capacity_errors.py
tests/unit/plugins/llm/test_transform.py
tests/unit/plugins/sinks/test_chroma_sink.py
tests/unit/plugins/sources/test_dataverse_source.py
tests/unit/plugins/transforms/rag/test_transform.py
tests/unit/telemetry/test_plugin_statistics.py
tests/unit/telemetry/test_property_based.py
tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py
```
