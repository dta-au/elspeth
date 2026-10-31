# Daybreak implementation review — request rejection

Source reviewed: immutable commit `67ad14c22b3987a16f0e9a71155102ee451da342`, tree `f3d8c44a1cfa0bf8284d40fa01f96d05e2aec4c9`, parent `2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31`. All source line references below are from that commit. This was read-only source review; no tests, provider calls, deployment, or historical request replay ran.

## Findings for debugger

### 1. Recognized request reason is lost from early and planner-decline audit (P2, diagnostic integrity)

`src/elspeth/web/composer/advisor_checkpoint.py:1439-1458,1502-1526` retains a nominal SDK bad request as `ProviderRequestFailure` with a closed `SafeRequestRejection`. But `completed()` passes only verdict, findings text/hash, attempts and counters to `AdvisorCheckpointPassRecord` (`advisor_checkpoint.py:1285-1322`), whose serialized payload has no rejection reason, source, or safe diagnostic reference (`advisor_audit.py:326-344,480-541`). The per-call audit records the bad-request class, not the closed reason (`advisor_checkpoint.py:608-613,643-667`). EARLY returns after that pass without a gate fact (`advisor_checkpoint.py:1544-1583`). Planner-decline final review removes `advisor_gate_decision` before terminal persistence (`planning_application.py:989-1009`); its pass and publication audit vocabularies likewise lack the descriptor (`advisor_audit.py:326-354,580-595`).

The ordinary END graph gate does persist and strictly reload the safe descriptor (`advisor_checkpoint.py:967-999`; `src/elspeth/web/execution/completion_gates.py:227-255,342-378`). Debugger: decide whether every recognized `request_rejected` checkpoint pass must retain the bounded safe reason and reference. If so, extend its audit contract with strict reader and verdict consistency checks. Cover EARLY and planner-decline with two distinct admitted codes, unknown-code refusal, and provider-text non-leakage. Keep the original exception process-local. Source review establishes the omission; it does not show that a historical incident used either path.

### 2. Live `/validate` response skips check-level rejection admission (P3, frontend contract)

`src/elspeth/web/frontend/src/api/client.ts:1359-1371` casts the 200 body to `ValidationResult` and validates only `readiness`. It does not validate `checks[*].request_rejection`, newly typed as `SafeRequestRejection` (`types/index.ts:548-555`). The audit reload path does validate each check and its optional descriptor (`api/auditReadiness.ts:88-105,125-135`); readiness blockers have their own descriptor validator (`api/validationReadiness.ts:25-43`). Therefore a synthetic live 200 response with valid readiness but `checks: [{name:"advisor_signoff",passed:false,detail:"rejected",affected_nodes:[],outcome_code:null,request_rejection:{message:"raw provider text"}}]` is accepted as typed data, whereas the same malformed audit check is refused. The backend `ValidationCheck` schema constrains its own output (`src/elspeth/web/execution/schemas.py:173-199`); this finding does not claim it currently emits malformed data or that the descriptor is rendered. Debugger: use the shared check validator on the live boundary and test an admitted descriptor plus a malformed one.

## Other reviewed areas

The bounded review did not identify a concrete regression in source option no-op admission or shareable-review authentication and capability handling. The core rejection classifier and ordinary END gate have positive and negative unit fixtures in the commit; those fixtures were read, not executed here. This report is not an integration, PostgreSQL, signed allowlist, or live acceptance result.
