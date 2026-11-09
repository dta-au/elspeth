# `patch_node_options` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Edit only an existing node's plugin options with a shallow top-level patch, while keeping node identity/kind/routing. The tool revalidates the exact reconciled candidate before publishing it.

Select for option-only changes. Use upsert_node for input/on_success/on_error/routes/fork changes, and preserve structured prompt interpretation references when editing model prompts.

## 2. Contract

Required node_id:string and patch:object; no other fields. Missing keys remain; null deletes; nested objects replace completely. Routing keys in patch refuse node_routing_patch_invalid even if a permissive plugin would accept them. Plugin identity and routes are retained. LLM-owned runtime hashes cannot be written; changing a compiled prompt without its existing prompt_template_parts refuses. Success returns affected node/applied echo, validation, hints and a new version; an empty patch still advances version. Mutation is not cached or automatically retried and has no request idempotency key. No plugin execution or provider call is performed.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:1984](../../../../src/elspeth/web/composer/tools/transforms.py#L1984) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1728](../../../../src/elspeth/web/composer/tools/transforms.py#L1728) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1690](../../../../src/elspeth/web/composer/tools/transforms.py#L1690) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1874](../../../../src/elspeth/web/composer/tools/transforms.py#L1874) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1903](../../../../src/elspeth/web/composer/tools/transforms.py#L1903) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/redaction.py:2223](../../../../src/elspeth/web/composer/redaction.py#L2223) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_common.py:1753](../../../../src/elspeth/web/composer/tools/_common.py#L1753) — shared mutation/config/result machinery.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Missing node is node_not_found. After patch-delta ownership checks, merged options stage default reviews. Existing plugin is checked against live policy before schema use; batch placement and required fields plus inline prompt restrictions run. Queue intrinsic contract and post-mutation invariants reject early. Reconciliation verifies existing approved prompt artifact hashes, regenerates structured prompt content and invalidates stale approval; then the final reconciled options undergo plugin prevalidation, retry-budget/profile and path checks. Review contract failure refuses the whole edit. Thus schema-valid input is insufficient for admission, and accepted mutation is separate from execution readiness.

## 5. Usability assessment

**SG-03 — demonstrated no-change inconsistency; high confidence.** Empty node patch preserves complete content but increments version4→5 and reports progress, while the source patch control retains version4. **SG-06 — shared candidate boundary; high confidence.** Patch correctly validates after reconciliation, unlike other verbs with differing sequencing; this should be one reusable admission contract rather than a remembered sequence. Workaround: patch only changed top-level fields, provide full intended nested mappings, and treat new review requirements as blocking until resolved.

## 6. Structural remediation

Extract node candidate admission and explicit no-change effect reporting shared with upsert, splice and bulk authoring. Preserve the post-reconciliation validation order so compiled prompts and review obligations are the actual validated objects. Regression: empty/same-value patch preserves version/progress; legal changed option succeeds; routing keys, forged runtime hashes and corrupt approved prompt evidence refuse unchanged; changing prompt context reopens review; profile/batch/path decisions agree across routes. Compatibility risk high around review hashes/structured prompts and medium for no-op revisions. Do not replace shallow patch semantics or heal corrupt evidence silently.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:3220](../../../../tests/unit/web/composer/test_tools_mutations.py#L3220) — option change, literal credential and routing-key rejection.
- [tests/unit/web/composer/test_tools_mutations.py:4005](../../../../tests/unit/web/composer/test_tools_mutations.py#L4005) — profile private-option refusal after positive setup.
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — exact reconciled candidate and cross-route guards.
- [tests/unit/web/composer/test_inline_blob_prompt_surface_tool_parity.py:1](../../../../tests/unit/web/composer/test_inline_blob_prompt_surface_tool_parity.py#L1) — cross-route prompt surface tests inspected, not run here.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. Controlled no-change probes compared full serialized state excluding version: metadata and node empty patches had identical content, versions4→5 and progress=True; source empty patch had identical content, versions4→4 and progress=False.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_patch_node_options`](../../../../src/elspeth/web/composer/tools/transforms.py#L1952).

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
    "node_id": {
      "type": "string",
      "description": "ID of the node to patch."
    },
    "patch": {
      "type": "object",
      "description": "Merge-patch to apply to plugin options only. Node-level routing fields such as on_success, on_error, input, routes, and fork_to are siblings of options; edit them with upsert_edge or upsert_node. For a gate, edit on_error only with upsert_node. If prompt_template_parts exists, edit that structure while preserving interpretation_ref entries; do not change only the compiled prompt_template. A patched schema declares arriving types for consumed fields and output types for created fields. To convert an arriving value, change the source contract (patch_source_options) or insert a type_coerce upstream; a declaration does not convert. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    }
  },
  "required": [
    "node_id",
    "patch"
  ],
  "additionalProperties": false
}
```
