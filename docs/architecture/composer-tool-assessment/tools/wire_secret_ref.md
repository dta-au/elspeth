# `wire_secret_ref` — authorize and author a credential marker

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance/design only. **Overall confidence: High for source and focused authorization/review tests.**

## 1. Purpose and usage

Select this tool after discovering an available reference and a plugin-owned credential option that the deployment explicitly authorizes. It writes a marker, never the secret value. It cannot grant policy permission or approve a run. The destination component must exist; the reference must resolve for the current authenticated user. Requires the scoped resolver, user ID, and server-authored wiring policy. It is web-only and absent from Composer MCP.

## 2. Contract

Required `name: string`, `target: source|node|output`, and `option_key: string`; optional `target_id` is required for node/output and for source when more than one source exists. With one source, omitted `target_id` selects that source. Explicit null is invalid: omit this optional field instead. Extra fields reject. `option_key` names one top-level key; dotted input is not a nested path traversal feature.

The tool verifies reference availability, target existence, exact server policy tuple `(secret, component_type, plugin, option_key)`, and plugin-approved secret placement. Node targets are restricted to transform/aggregation/collector plugin nodes; these use `component_type=transform` for policy. Output targets use `sink`. Source/output AWS endpoint policy is rechecked. It sets `options[option_key]={"secret_ref": name}`, reconciles authoritative reviews, checks canonical interpretation invariants, and returns affected component, validation/delta, and applied-component echo after a composition version increment. It does not resolve the secret or execute a plugin.

This is uncached `SECRET_MUTATION` with graph/validation/YAML effects. Explicit-approval mode proposes the graph edit; any eventual credential-use acknowledgement is a separate out-of-band run gate. Repeating an identical wire may advance version again. Missing context, unavailable ref, missing/unsupported target, destination denial, disallowed placement, endpoint or review failure returns a typed refusal with prior state preserved. Denied exact policy cannot be fixed by retries. Shape errors raise safe argument errors; owned faults propagate.

## 3. Implementation trace

The [argument model](../../../../src/elspeth/web/composer/tools/secrets.py#L75) and [handler/declaration](../../../../src/elspeth/web/composer/tools/secrets.py#L323) own authoring semantics. [`secret_wiring_authorization_error`](../../../../src/elspeth/web/secrets/wiring_policy.py) enforces exact destination policy; [placement helpers](../../../../src/elspeth/web/composer/tools/_common.py#L3280) use plugin/ref policy. [`_reconciled_secret_wire_result`](../../../../src/elspeth/web/composer/tools/secrets.py#L287) reopens/refuses stale review evidence and enforces canonical requirements.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111), [canonical model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), provider wire projection, and [public dispatcher](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) connect schema/context admission. [Tool-batch approval and worker execution](../../../../src/elspeth/web/composer/tool_batch.py#L1729), [audit](../../../../src/elspeth/web/composer/audit.py#L812), and [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L75) publish the authored edit. [Execution secret validation](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) rechecks markers introduced by any route; [run admission](../../../../src/elspeth/web/execution/service.py#L2549) requires state-bound out-of-band acknowledgement before execution. Tool arguments cannot supply that authority.

## 4. Alternative paths

Single source can be selected implicitly; plural sources require exact identity. A structural gate/coalesce-like node cannot receive plugin credentials. Available reference plus a near-match policy still denies before placement/endpoint processing. Authorized placement mutates only the intended options and reconciles review state; invalidated review anchors reopen or the edit refuses. Inline markers introduced through patch/YAML/whole-pipeline paths are revalidated before execution, so this tool is not the sole enforcement seam. Operator-profile-lowered credentials have a separately server-authored trust path and are not model wiring grants.

## 5. Usability assessment

No authorization bypass was demonstrated. Tests explicitly show available credentials do not authorize near-match destinations. **Intended restrictions:** deny-by-default exact policy and separate run acknowledgement remain essential, even when a planner cannot complete the user's request without operator changes. Workaround: surface the exact blocked capability and request the relevant operator action; never fabricate credentials, alter destination spellings to evade policy, or interpret a successful wire as execution approval.

**Source-established guidance limitation:** `target_id` schema prose mentions node/output but omits the multi-source requirement, which is enforced only in handler semantics. The larger structural issue is that destination capability discovery, placement, candidate mutation, review reconciliation, preflight, and run acknowledgement are separate contracts the orchestrator must join correctly.

## 6. Structural remediation

Use a shared typed destination descriptor and value-free authorization/capability view across schema discovery, list/validate references, wiring, whole-state admission, and execution disclosure. Plugin contracts should own supported credential fields; web policy owns destination permission; review authority owns invalidation; execution owns approval and actual resolution. Keep all alternate marker-entry paths under the same enforcement boundary and make each outcome actionable.

Acceptance: source/node/output/aggregation/collector targets obey one destination vocabulary; multi-source omission rejects with explicit guidance; exact authorized tuple succeeds, every near-match denies; plugin placement and owned-field constraints cannot be bypassed through patch/YAML/blob markers; review evidence refreshes consistently; a successful wire cannot mint a run acknowledgement; real values never enter tool/audit payloads. Compatibility risk is Medium for destination descriptor/interface changes, Low for correcting schema guidance. Preserve existing no-value, exact allowlist, and out-of-band approval contracts.

## 7. Evidence, gaps, and caveats

Read [authorized wiring assertions](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L386), [near-match rejection controls](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L413), [authorization-before-endpoint tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L440), [review drift refusal](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L491), and [collector policy tests](../../../../tests/unit/web/composer/test_secret_wiring_collector.py#L68). Included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. Execution gates were source-traced, not exercised against a live deployment; no real secrets, cloud service, PostgreSQL, or browser acceptance was used. Complete alternate-entry parity remains an acceptance requirement, not an inferred blanket success.

## Runtime argument schema and measured exposure

Synchronous kind: `secret_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.secrets._execute_wire_secret_ref`](../../../../src/elspeth/web/composer/tools/secrets.py#L323).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "Secret reference name."
    },
    "target": {
      "type": "string",
      "enum": [
        "source",
        "node",
        "output"
      ],
      "description": "Which component to wire the secret into."
    },
    "target_id": {
      "type": "string",
      "description": "Node ID or output name (required for node/output targets)."
    },
    "option_key": {
      "type": "string",
      "description": "Config option key to set (e.g. 'api_key')."
    }
  },
  "required": [
    "name",
    "target",
    "option_key"
  ],
  "additionalProperties": false
}
```
