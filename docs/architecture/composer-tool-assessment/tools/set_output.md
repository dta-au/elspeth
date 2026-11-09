# `set_output` — add or replace a configured sink

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance/design only. **Overall confidence: High for inspected source and focused tests.**

## 1. Purpose and usage

Use this tool when a sink instance must be created or wholly replaced. Discover allowed sink plugins and inspect their configuration schema first. The sink's name is also the upstream connection name it consumes; it must match an upstream `on_success`/route, not necessarily a node ID. The graph may be incomplete while editing. A new sink requires an explicit write-failure choice. It is available through web Composer and the common Composer MCP surface, with different storage/authority contexts.

## 2. Contract

Required arguments: `sink_name: string`, `plugin: string`, `options: object`. Optional `on_write_failure` preserves the existing same-name sink's choice only when omitted; a new sink needs a nonempty value such as `discard` or an eligible sink diversion. Explicit null/empty is invalid despite null being admitted by the outer schema. Optional `description` defaults to null. This is replacement, not merge: omitted old options are removed and omitted description is cleared.

The plugin's own config model controls option shape. The web additionally enforces plugin policy/availability, secret reference placement, session-local path rules, AWS S3 endpoint policy, and explicit collision policy for file sinks in runnable web contexts. Success adds/replaces the named output and increments composition version; it returns affected identity, validation/delta, and an applied-component echo. Valid authoring can still leave graph validation false. No sink bytes or remote writes occur in this handler.

It is uncached `MUTATION` with graph/validation/YAML effects. Under `explicit_approve`, the web prepares a proposal before applying; `auto_commit` applies admitted edits. Repeating identical arguments advances version again, so it is not version-idempotent. Schema failures are safe argument errors. Failure-route, path, policy, credentials, config, collision, or whole-state invariant failures preserve the old graph and carry typed validation codes; relevant option failures receive plugin schema repair context. Unexpected owned config/schema exceptions propagate as framework/plugin failures rather than being blamed on model input.

## 3. Implementation trace

The [declaration and handler](../../../../src/elspeth/web/composer/tools/outputs.py#L48) feed the [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [canonical model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). [`_execute_set_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L137) resolves write-failure semantics, policy, catalog schema, credential placement, path policy, [`_prevalidate_sink`](../../../../src/elspeth/web/composer/tools/_common.py#L3631), collision policy, `OutputSpec`, and the post-mutation invariant. [Plugin option admission](../../../../src/elspeth/web/composer/tools/_common.py#L2514) selects the plugin config model and handles deferred secret/blob values without exposing real secrets.

[`CompositionState.with_output`](../../../../src/elspeth/web/composer/state.py#L8278) replaces by name and increments version. [Dispatch finalization](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) normalizes policy validation and attaches repair schema/guidance. [Web batch approval/execution](../../../../src/elspeth/web/composer/tool_batch.py#L1729), [audit](../../../../src/elspeth/web/composer/audit.py#L812), and [frontend proposal/outcome cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L75) manage publication. [MCP dispatch](../../../../src/elspeth/composer_mcp/server.py#L363) uses the shared tool but a trained-operator policy view and no web data root; it also applies its file-sink collision control.

## 4. Alternative paths

New sink with omitted failure choice refuses; replacing an existing sink can preserve that choice. Unknown, unavailable, prohibited, or profile-restricted plugin fails before schema disclosure beyond allowed policy context. Literal credential repair returns structured reference guidance. Bad plugin options carry the admitted plugin's schema; a broken schema provider raises rather than fabricating an unavailable plugin. Web output paths are session-authorized. MCP can author operator-controlled destinations according to its separate context. Successful replacement does not prove route completeness, secret resolution, runtime preflight, or external write acceptance.

## 5. Usability assessment

No confirmed defect was demonstrated for this tool in the scoped review. **Intended editor semantics:** `success=true` can accompany invalid graph validation; the output was stored as an authored edit, not certified runnable. The name/connection dual role, replacement semantics, and contextual collision/failure-route requirements create a substantial reasoning burden, but current guidance explicitly covers the main distinctions. Workaround: read the applied echo and validation, preserve full options when replacing, and use preview before claiming readiness.

**Source-established structural limitation:** admission logic is repeated across set/patch/wire/whole-pipeline paths, while deferred values are validated differently before and after resolution. The focused rejection parity tests are valuable existing controls; their success does not prove every future plugin/provider option has complete parity.

## 6. Structural remediation

Consolidate candidate output admission around plugin-owned authoring/deferred-value contracts and a web-owned policy/review finalizer, reused by creation, patch, secret/blob wiring, YAML, and whole-pipeline authoring. Separate outcomes for edit accepted, validation failed, review pending, and runtime-ready. Keep plugin execution separate from authoring and keep route validation authoritative.

Acceptance: the same effective output candidate yields the same policy/config/credential/collision decision through every authoring route; missing explicit failure routes reject; allowed deferred refs remain authored without secret material; invalid provider responses/framework faults do not masquerade as argument errors; valid incremental edit returns exact effective options and a truthful readiness state. Compatibility risk is Medium for changing output result semantics and High if persisted route/config meaning changes. Preserve existing failure routes, session path restrictions, and refuse contracts.

## 7. Evidence, gaps, and caveats

Read [success/replacement/version assertions](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L322), [credential repair tests](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L283), path/policy/unknown-plugin/schema-fault test bodies, and the config admission implementation. Included in a seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. No real sink write, cloud service, browser, PostgreSQL, or live provider ran. The recommendation concerns consistent ownership and outcome clarity, not a demonstrated sink-execution failure.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.outputs._handle_set_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L64).

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
      "description": "Sink name: BOTH the sink's identifier (used by patch_output_options/remove_output) AND the connection-name the sink consumes — it MUST equal some upstream's on_success value; it need not match any node id."
    },
    "plugin": {
      "type": "string",
      "description": "Sink plugin name (e.g. 'csv', 'json')."
    },
    "options": {
      "type": "object",
      "description": "Plugin-specific config. For csv/document/json/text file sinks in runnable web pipelines, include path, schema, and explicit collision_policy. Sink schema.fields and custom header keys use the carried row names, normally normalized source headers or mapping targets. The interpretation_requirements option root is not settable on outputs. Stage an authorable review on its source or node instead."
    },
    "on_write_failure": {
      "type": [
        "string",
        "null"
      ],
      "description": "Explicit non-empty write-failure choice required for new sinks: 'discard' records the drop; an eligible sink name diverts failed rows. Omit only to preserve this existing sink's choice. Null/empty is invalid."
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
    "sink_name",
    "plugin",
    "options"
  ],
  "additionalProperties": false
}
```
