# `set_source` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Add or replace one named source specification. The model chooses the plugin, options and runtime connection; the tool admits that explicit choice and maintains source review authority.

Use for a configured external or authorised path-based source. For session blob custody use the dedicated singular/plural binding tools. New sources require an explicit failure destination; replacing the same named source can preserve its existing choice.

## 2. Contract

Required plugin:string, on_success:string and options:object. source_name defaults to source; names must be lowercase runtime-valid identifiers, bounded to the runtime name limit and not reserved labels. Optional on_validation_failure distinguishes omitted from null/empty: omission preserves an existing choice, while new/null/empty without a valid explicit choice refuses. description is optional informational prose. Options replace the previous options, rather than merge. Success returns the applied source echo, affected source component, new version, validation and optional plugin hints/server_owned_metadata_note. Structured rejection leaves the exact saved state/version and can attach plugin schemas and repair guidance. No plugin execution, remote source fetch or blob write occurs. No automatic retry or exactly-once key exists; an identical replacement still advances version.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:229](../../../../src/elspeth/web/composer/tools/sources.py#L229) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:993](../../../../src/elspeth/web/composer/tools/sources.py#L993) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1172](../../../../src/elspeth/web/composer/tools/sources.py#L1172) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/_common.py:3485](../../../../src/elspeth/web/composer/tools/_common.py#L3485) — shared mutation/config/result machinery.
- [src/elspeth/web/interpretation_state.py:1](../../../../src/elspeth/web/interpretation_state.py#L1) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/state.py:8221](../../../../src/elspeth/web/composer/state.py#L8221) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Argument model failure becomes ToolArgumentError. Admission normalises exact echoes of stored server metadata, rejects forged authority and manual blob_ref/blobs/source_authoring roots, checks endpoint and plugin policy, credential placement, path confinement and plugin config. It then resolves the failure route, constructs a SourceSpec and reconciles reviews. Review-reconciliation failure refuses the whole mutation. A source may be accepted into an incomplete graph: success means the edit applied, not that execution is ready. Operator profiles lower safe public options separately from private provider binding. Public dispatch requires the data directory for path options; direct handler contexts can deliberately lack that public guard.

## 5. Usability assessment

**SG-03 — source-established lifecycle inconsistency; high confidence.** Repeating an identical replacement advances version, while an empty patch_source_options call can retain the same version. This matters because Composer uses version advance as a progress signal. It is not data loss or a broken promised exactly-once contract. **SG-06 — structural ownership pressure; high confidence.** This mutation repeats policy, profile, credential, path, review and config admission also present in bulk authoring and patch/bind routes. Existing refusal tests defend several seams, but adding a policy requires coordinated edits. Workaround: inspect the returned applied component and validation; use the patch tool for an option-only edit and binding tools for blobs.

## 6. Structural remediation

Introduce one source candidate-admission operation used by replacement, patch, blob binding and bulk authoring, with explicit author-owned options, binding evidence, policy snapshot and review reconciliation inputs. Plugins retain config/schema ownership; the web adapter owns path/credential/profile policy; the orchestrator owns selection and retry. Define successful_no_change separately from applied and from execution readiness, then use actual accepted content change for progress. Regression: source replacement, equivalent patch and bulk authoring must accept the same legal config and reject forged review evidence, escaping paths, literal credentials and malformed options without version change; an exact replay must follow the declared no-change policy. Compatibility risk medium: source naming, failure-route preservation and stored authority metadata must remain readable.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:575](../../../../tests/unit/web/composer/test_tools_mutations.py#L575) — TestSetSource basic/named-source replacement and explicit destination tests.
- [tests/unit/web/composer/test_source_rejection_contracts.py:125](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L125) — refusal preservation on handler and dispatch routes.
- [tests/unit/web/composer/test_source_rejection_contracts.py:662](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L662) — test_source_successes_preserve_literal_options_failure_choice_and_update_semantics.
- [tests/unit/web/composer/test_tools_mutations.py:899](../../../../tests/unit/web/composer/test_tools_mutations.py#L899) — literal-credential rejection.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._handle_set_source`](../../../../src/elspeth/web/composer/tools/sources.py#L207).

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
      "description": "Stable source root name. Defaults to 'source' for legacy single-source pipelines."
    },
    "plugin": {
      "type": "string",
      "description": "Source plugin name."
    },
    "on_success": {
      "type": "string",
      "description": "Connection-name string this source PUBLISHES. Some downstream consumer (transform 'input' or output 'sink_name') MUST equal this value — the runtime matches strings, not graph topology."
    },
    "options": {
      "type": "object",
      "description": "Plugin-specific config. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    },
    "on_validation_failure": {
      "type": [
        "string",
        "null"
      ],
      "description": "Explicit non-empty failure choice required for new sources. 'discard' records the drop; Any other value, including 'quarantine', must match a configured output/sink name. a named route must satisfy sink and required-control policy. Omit only to preserve the same existing source's choice. Null/empty is invalid; no server default is supplied."
    },
    "description": {
      "type": [
        "string",
        "null"
      ],
      "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
    }
  },
  "required": [
    "plugin",
    "on_success",
    "options"
  ],
  "additionalProperties": false
}
```
