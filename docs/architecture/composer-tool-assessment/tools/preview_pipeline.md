# `preview_pipeline` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Perform authoring validation, caller-wired runtime preflight and bounded semantic inspection of blob-backed sources. This is a diagnostic tool; it does not execute pipeline rows. It is state-sensitive, noncacheable discovery. Planner and MCP callers must supply the preflight context they can actually support.

## 2. Contracts and authority

No semantic arguments. The envelope carries authoring validation and optional runtime_preflight; data contains preview_is_valid, preview_errors, edge contracts, proof diagnostics and graph overview. Strict and tolerant structural preview are deliberately distinct: tolerant findings never establish readiness. Blob proof requires exact session/store/operation authority. Missing runtime preflight makes preview_is_valid false; a successful tool call can report an invalid preview ([src/elspeth/web/composer/tools/generation.py:4475](../../../../src/elspeth/web/composer/tools/generation.py#L4475)).

## 3. Source trace to the user

`_execute_preview_pipeline` admits source-proof claims, validates against policy catalog, invokes supplied strict/tolerant preflight, then calls `compute_proof_diagnostics` ([src/elspeth/web/composer/tools/generation.py:4504](../../../../src/elspeth/web/composer/tools/generation.py#L4504), [src/elspeth/web/composer/tools/generation.py:4534](../../../../src/elspeth/web/composer/tools/generation.py#L4534)). Proof loops over blob-backed sources, resolves row and bytes under session custody, memoizes repeated blob IDs, checks CSV/text and downstream field/type constraints, and returns explicit not-applicable/evaluated/unavailable variants ([src/elspeth/web/composer/tools/generation.py:4233](../../../../src/elspeth/web/composer/tools/generation.py#L4233)). It then forms the conjunction and selected preview payload ([src/elspeth/web/composer/tools/generation.py:4553](../../../../src/elspeth/web/composer/tools/generation.py#L4553)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

A source-proof-unavailable result can terminate the issued tool batch after its executed prefix, so later tools in that batch must not be assumed to have run ([src/elspeth/web/composer/tool_batch.py:3388](../../../../src/elspeth/web/composer/tool_batch.py#L3388)).

## 4. Branches, failures and recovery

Branches include malformed source proof identity; no blob source (proof not applicable); missing authenticated context; unavailable/non-ready blob; integrity failure; source cap exceeded; shared-blob memoization; CSV/text diagnostics; proof-work budget exhausted; absent strict runtime stage; tolerant structural advisory; valid/invalid conjunction. File hash or stored authority anomalies escalate; ordinary source/configuration failures become diagnostics. Recovery should restore authenticated custody or repair the actual source/configuration, never fabricate an empty source. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=False`: every invocation re-evaluates diagnostics; caller-owned runtime-preflight caching is a separate mechanism and must bind its state. Repetition does not author a graph mutation but performs reads, validation and audit again. The shared blob reader may perform storage-maintenance recovery before reading, so graph-read-only does not promise universally write-free execution. Blob metadata/bytes are observed under the exact operation custody for each resolution; the tool does not claim one timeless snapshot across arbitrary later calls. There is no handler retry loop. Recoverable changed/unavailable evidence requires a fresh admitted call; integrity failures must not be retried as invented graph repair. Provider retry accounting remains outside this synchronous diagnostic handler.

## 5. Defects, restrictions and hypotheses

No preview correctness defect was demonstrated in this lane. **Source-grounded performance concern, owned by blob lane:** the inspection prefix is bounded, but Composer resolves full bytes with `_locked_read_ready_blob`, then slices to 8 KiB ([src/elspeth/web/composer/tools/generation.py:4299](../../../../src/elspeth/web/composer/tools/generation.py#L4299), [src/elspeth/web/composer/tools/blobs.py:1958](../../../../src/elspeth/web/composer/tools/blobs.py#L1958)). This establishes full-file materialization, not measured latency or memory exhaustion. Integrity verification may intentionally require reading all bytes. Do not call the whole I/O bounded by the inspection prefix. **Restriction:** proof applies to supported sampled semantics and does not prove all data rows or live provider behavior.

## 6. Ownership, remediation and dependencies

Execution/preflight owner owns runtime admission; source-proof owner owns sampled evidence and uncertainty; blob custody owner owns verified reads; orchestrator owns whether missing proof ends the batch. A structural remediation should provide one explicit diagnostic result carrying proof applicability, limits and readiness, while preserving the conjunct and authority fencing. Any streaming verified-read redesign must preserve row/bytes version consistency and full integrity checks.

**Implementation risk:** High for custody/read/validation consolidation; preserve bounded inspection memory, exact content integrity, stage applicability and fail-closed missing-preflight behavior.

## 7. Evidence, regression checks and limits

Read preview empty/valid semantics ([tests/unit/web/composer/tools/test_tools_preview.py:189](../../../../tests/unit/web/composer/tools/test_tools_preview.py#L189)), contract guard `test_valid_preview_call_does_not_claim_valid_pipeline` ([tests/unit/web/composer/test_generation_response_contracts.py:174](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L174)), proof cap implementation and missing-context branch. Generation response contract tests passed in the 370-test selection; the full preview test file passed in the second focused selection: 195 tests, exit 0. Confidence: high in staged verdict semantics; moderate in exhaustive diagnostic reach. Regression oracles: absent stage cannot pass, unavailable saved source halts suffix, repeated blob source reads once per proof, corrupt hash escalates, cap exhaustion cannot certify partial proof.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.generation._execute_preview_pipeline`](../../../../src/elspeth/web/composer/tools/generation.py#L4475).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```
