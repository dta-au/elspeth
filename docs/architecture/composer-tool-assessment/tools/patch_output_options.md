# `patch_output_options` — shallow sink option edit

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance/design only. **Overall confidence: High for source and focused tests.**

## 1. Purpose and usage

Use this tool for an option-only edit to an existing sink when its name, plugin, write-failure route, and description should remain. Read the sink's effective configuration first. It is preferable to full replacement for a small top-level option change, provided the caller understands shallow merge semantics. Available through web Composer and common Composer MCP. The output must exist and its plugin must be admitted by the request's policy/catalog snapshot.

## 2. Contract

Required `sink_name: string` and `patch: object`; extra fields are forbidden. Patching is **shallow**: absent keys remain, non-null values replace complete top-level values, null deletes a top-level key. A nested object is replaced wholesale, not recursively merged. Null cannot be used to store a literal null. The operation changes only `OutputSpec.options`; name/plugin/failure route/description are retained.

Effective merged options are rechecked against plugin policy, catalog schema, AWS endpoint policy, credential placement, session-local destination rules, plugin config model, and explicit web file collision policy. Success increments composition version and returns affected output, validation/delta, applied-component echo, and plugin post-call hints when supplied. It may still leave graph-level errors. Missing output, policy/path/credential/config/collision problems reject with prior state preserved and appropriate error codes; option failures may include inline plugin schemas and static repair guidance. Schema/model type errors use `ToolArgumentError`; unexpected schema/config bugs propagate.

This is an uncached graph/validation/YAML `MUTATION`. Explicit-approval mode proposes the patch before application. An identical patch can still increment version; it is not version-idempotent. It does not write sink artifacts or resolve real secrets. Concurrency and persistence belong to the enclosing session operation. Outer public entry points enable closed schema checks; direct internal handlers have their own Pydantic boundary but are not substitutes for web authority.

## 3. Implementation trace

The [handler and declaration](../../../../src/elspeth/web/composer/tools/outputs.py#L242) use the redaction-bearing [`PatchOutputOptionsArgumentsModel`](../../../../src/elspeth/web/composer/redaction.py#L2259), [`_apply_merge_patch`](../../../../src/elspeth/web/composer/tools/_common.py#L1753), and [plugin config admission](../../../../src/elspeth/web/composer/tools/_common.py#L2514). Successful `_handle_patch_output_options` looks up the actual updated output and attaches hints from the catalog; absence after success is an assertion-level invariant failure.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111), [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), and provider wire projection expose the schema. [Dispatch finalization](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) normalizes policy validation and attaches prior validation/repair content. [Tool-batch approval and worker dispatch](../../../../src/elspeth/web/composer/tool_batch.py#L1729), [audit](../../../../src/elspeth/web/composer/audit.py#L812), and [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L75) own publication and visible outcome. [MCP dispatch](../../../../src/elspeth/composer_mcp/server.py#L363) shares the handler with its operator-controlled context and collision admission.

## 4. Alternative paths

Patching an existing authorized output preserves non-option fields. An absent target produces `output_not_found`; unknown/unavailable state-held plugin refuses before schema augmentation. Deleting a required option or replacing an incomplete nested config is rejected by the plugin model where decidable. Deferred secret/blob markers receive authored-state admission, then execution resolves actual values separately. A valid patch can introduce/retain graph-level failures which validation reports; a patch success never certifies successful publication to the external sink.

## 5. Usability assessment

No confirmed defect was demonstrated in this lane. **Intended restriction:** shallow replacement and null-as-delete are explicitly advertised. The model must not assume JSON Merge Patch recursion merely from the word “patch.” Workaround: copy the complete intended nested object or use full output replacement deliberately, then inspect the applied echo and preview result.

**Source-established ownership limitation:** output patch, full set, secret wiring, inline-blob wiring, and whole-pipeline edits converge only through several helper/finalizer layers. Their individual tests are meaningful, but adding a plugin option can require consistent updates across authored/deferred/runtime policy seams. The proposed remedy is shared candidate admission, not more independent field-name guards.

## 6. Structural remediation

Create a common output candidate operation that applies the chosen edit semantics, asks plugin-owned config/deferred-value contracts for validation, then applies web policy/review/publication once. Keep shallow patch behaviour explicit and stable; do not silently switch to recursive merge. Model results should distinguish accepted edit from ready-to-execute and report effective configuration without requiring a second full state read.

Acceptance: absent/unset/null/nested replacement cases are independently asserted; the same effective candidate has the same rejection through patch/set/wire/YAML paths; malformed args and unavailable plugins reject without prior-state damage; secret values never enter authoring output; hint/schema faults remain owned failures; repeated/concurrent requests produce truthful version/effect evidence. Compatibility risk is Medium for mutation-envelope changes and High for changing patch semantics, which this assessment does not recommend.

## 7. Evidence, gaps, and caveats

Read [output replacement/patch preservation tests](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L322), [credential repair assertions](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L283), policy/path/schema-fault cases, and the actual merge implementation. Included in the seven-file selection: exit 0, **448 passed, 43 warnings, 66.95s**. No real sink write, live provider, browser, PostgreSQL, or complete plugin matrix ran. These findings support structural consolidation while preserving the currently tested rejection contracts.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.outputs._handle_patch_output_options`](../../../../src/elspeth/web/composer/tools/outputs.py#L335).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "sink_name": {
      "type": "string",
      "description": "Name of the output (sink) to patch."
    },
    "patch": {
      "type": "object",
      "description": "Merge-patch to apply to output options. The interpretation_requirements option root is not settable on outputs. Stage an authorable review on its source or node instead."
    }
  },
  "required": [
    "sink_name",
    "patch"
  ],
  "additionalProperties": false
}
```
