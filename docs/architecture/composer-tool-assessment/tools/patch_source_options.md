# `patch_source_options` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Change only author-owned options on one existing named source while retaining its plugin, routing and description. This is the narrow edit operation for source configuration.

Select for option-only edits; use set_source or blob binding to change plugin/binding, and set_source to alter source routing. Requires a matching saved source and request policy snapshot.

## 2. Contract

Required patch:object; source_name defaults source. It is a **shallow** top-level merge: absent keys retain values, null deletes, and a nested object replaces that entire value. Routing fields are not arguments. Existing plugin, failure destination and description are preserved. Exact server metadata echoes are tolerated but forged ownership is refused. Blob-backed path/blob_ref and plural blobs/source_authoring roots cannot be patched. Result includes source applied echo, validation, hints and optional echo note. After reconciliation the entire authority payload is compared with version neutralised using standard JSON: exact no-change returns the original state/version and empty affected_nodes. No execution, provider call, blob write, auto retry or approval bypass occurs.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:2284](../../../../src/elspeth/web/composer/tools/sources.py#L2284) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:2028](../../../../src/elspeth/web/composer/tools/sources.py#L2028) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:2140](../../../../src/elspeth/web/composer/tools/sources.py#L2140) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:2247](../../../../src/elspeth/web/composer/tools/sources.py#L2247) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/_common.py:1753](../../../../src/elspeth/web/composer/tools/_common.py#L1753) — shared mutation/config/result machinery.
- [src/elspeth/web/composer/redaction.py:2197](../../../../src/elspeth/web/composer/redaction.py#L2197) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Missing source returns source_not_found. Removed/unavailable plugin is a policy refusal before schema augmentation. Patch delta is checked for forged resolver review evidence; merged options are checked for canonical metadata, endpoint policy, credentials, confined paths and actual plugin config. Review reconciliation can reject atomically. Changed source input/schema can reopen authority reviews; unchanged metadata echoes must not downgrade approved evidence. Setting a mandatory option to null ordinarily deletes it and then fails plugin admission; null does not mean plugin-specific nullable value. A valid patch can leave unrelated graph errors. Direct/MCP contexts differ on permitted file paths.

## 5. Usability assessment

**SG-03 — useful existing reference implementation; high confidence.** This tool explicitly distinguishes no semantic change from a version advance, unlike empty node/metadata patches. It is not a defect here; it demonstrates that the progress contract can be made consistent. **SG-06 — shared admission pressure.** The handler repeats source candidate checks and review reconciliation alongside replacement/binding/bulk authoring. Workaround: supply the complete intended nested mapping, and inspect returned applied component rather than assuming recursive JSON Merge Patch semantics.

## 6. Structural remediation

Extract source candidate admission and a shared semantic-change comparison, preserving shallow patch semantics as a public tool contract. Do not silently switch to recursive merge: that changes nested delete/replace behaviour. Regression: unchanged patch/echo returns identical state/version; bool,int,float changes remain distinguishable; nested mapping replacement is tested; forbidden binding/authority edits refuse without touching storage; missing plugin remains a policy refusal; changed evidence reopens exactly the affected reviews. Compatibility risk medium for comparison/canonicalisation and high if patch semantics were changed. Orchestrator progress should consume explicit applied/no_change evidence.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:2805](../../../../tests/unit/web/composer/test_tools_mutations.py#L2805) — shallow merge patch unit cases.
- [tests/unit/web/composer/test_tools_mutations.py:2861](../../../../tests/unit/web/composer/test_tools_mutations.py#L2861) — source update/add/delete and binding locks.
- [tests/unit/web/composer/test_tools_mutations.py:3079](../../../../tests/unit/web/composer/test_tools_mutations.py#L3079) — guarantee stamp ownership.
- [tests/unit/web/composer/test_source_rejection_contracts.py:638](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L638) — singular binding custody retained.
- [tests/unit/web/composer/test_authoring_reconciliation.py:1254](../../../../tests/unit/web/composer/test_authoring_reconciliation.py#L1254) — unchanged source authority version preservation.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. Controlled no-change probes compared full serialized state excluding version: metadata and node empty patches had identical content, versions4→5 and progress=True; source empty patch had identical content, versions4→4 and progress=False.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._handle_patch_source_options`](../../../../src/elspeth/web/composer/tools/sources.py#L2262).

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
    "source_name": {
      "type": "string",
      "description": "Source root name to patch. Defaults to 'source'."
    },
    "patch": {
      "type": "object",
      "description": "Merge-patch to apply to source options. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    }
  },
  "required": [
    "patch"
  ],
  "additionalProperties": false
}
```
