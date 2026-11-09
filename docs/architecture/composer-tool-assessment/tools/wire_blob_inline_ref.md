# `wire_blob_inline_ref` — pin artifact content into plugin options

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source and focused tests; Moderate for broad plugin compatibility beyond exercised cases.**

## 1. Purpose and usage

Use this tool when an existing authorized artifact should supply text to a plugin configuration option at runtime without copying bytes into the tool arguments. It authors an `inline_content` marker pinned to authoritative blob metadata. Use source-binding tools instead when the artifact is the pipeline's source input. The destination component must already exist. Session engine/ID and a current ready blob are required; execution later needs the ordinary blob/content authority. This tool is absent from Composer MCP.

## 2. Contract

Required `field_path: string` and `blob_id: UUID string`; optional `encoding` defaults to `utf-8`, with allowed `utf-8`, `utf-8-sig`, `latin-1`, and `utf-16`. Canonical paths are `source.options.<field>`, `source:<name>.options.<field>`, `node:<id>.options.<field>`, or `output:<name>.options.<field>`. Nested object paths are supported; a scalar collision is refused rather than coerced into an object. Structural nodes without a runtime plugin cannot be targets.

Success increments composition version, returns `data.field_path`, affected component IDs, validation, and the applied-component projection. The marker contains canonical `blob_ref`, `mode="inline_content"`, server-pinned `sha256`, and a nondefault encoding if needed. A caller-provided hash is not admitted. This is a graph-affecting `BLOB_MUTATION`, not blob-store-only: explicit approval applies in that session mode, and no cache is used. Repeating the same wiring can increment version again; there is no semantic no-op/idempotency contract.

Runtime-owned fields, source binding metadata, and interpretation-review metadata cannot be overwritten through paths. LLM-generated artifacts cannot bypass prompt/model reviews by occupying an LLM prompt surface. Missing/not-ready blobs, missing components, illegal paths, policy errors, and reconciliation errors refuse the edit while preserving prior state. A ready blob with null hash is an integrity failure. Wiring metadata does not certify that bytes decode or that the resulting plugin configuration is runnable; validation/preflight remains authoritative.

## 3. Implementation trace

The [handler/declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L1004) admits `WireBlobInlineRefArgumentsModel`, canonicalizes UUID, queries same-session metadata, builds the owned `BlobInlineRef`, and applies [`_apply_inline_blob_marker`](../../../../src/elspeth/web/composer/tools/blobs.py#L735). [Prompt surface admission](../../../../src/elspeth/web/composer/tools/blobs.py#L890), endpoint policy, canonical interpretation checks, and authoritative review reconciliation run before returning the candidate.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) derives graph effects from its declaration; [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) and provider wire projection expose the argument contract. [Dispatch finalization](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) revalidates against the request's policy snapshot and adds delta/guidance. [Tool-batch approval/worker execution](../../../../src/elspeth/web/composer/tool_batch.py#L1729) controls proposal versus apply; [audit](../../../../src/elspeth/web/composer/audit.py#L812) and [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) present accepted effects. Runtime resolution belongs to [inline-blob execution validation](../../../../src/elspeth/web/execution/_validation_authoring.py), not this writer.

## 4. Alternative paths

An uploaded or otherwise eligible artifact can wire a normal option. A missing node, scalar intermediate path, unsupported encoding, non-ready artifact, or forbidden owned field is rejected. A valid metadata marker can still fail later because bytes changed, decoding fails, or the plugin rejects the resolved value. Edits to reviewed options reopen or refuse incompatible review evidence; they do not silently retain stale approvals. Source aliases and plural source names resolve by their explicit identities rather than choosing an arbitrary source.

## 5. Usability assessment

**Source-established limitation:** this tool's generic path interface accepts prospective references while plugin-specific value validation is deferred. A successful wire is an accepted authoring operation, not proof of runtime readiness. The direct handler is careful about ownership/review rules, but those rules are distributed across path parsing, modality checks, canonical interpretation checks, dispatch validation, and runtime dereference. This makes the orchestrator depend on several later diagnostics to understand one apparent edit. Workaround: inspect the returned applied component and validation, then request preview; do not claim completion from the wire result alone.

The prompt-surface and resolver-owned-field refusals are **intended restrictions**, backed by tests. No bypass or stale-review defect was demonstrated in this lane.

## 6. Structural remediation

Unify plugin option authorability and deferred-value capabilities in a plugin-owned contract consumed by schema discovery, path-based wiring, option patching, preview, and runtime. The web layer should own session authorization and review transitions; the plugin should own whether a given option accepts deferred content and how the resolved value is validated. A shared mutation finalizer should reconcile review evidence and emit explicit authoring-versus-readiness outcomes across all edit routes.

Acceptance: each declared deferred field wires successfully and resolves consistently through preview/runtime; unsupported fields and scalar collisions reject; forbidden review/runtime roots remain unwriteable; generated prompt blobs cannot evade review; hash mismatch, wrong-session blob, changed bytes, or bad encoding fails without execution. Compatibility risk is Medium because persisted field paths and marker shapes are public authored data. Preserve current pinning and review/refuse boundaries; no data migration is authorised by this assessment.

## 7. Evidence, gaps, and caveats

Read [authoritative hash test](../../../../tests/unit/web/composer/test_blob_inline_tools.py#L488), [named source/output path tests](../../../../tests/unit/web/composer/test_blob_inline_tools.py#L515), and rejection bodies for pending blobs, invalid paths, ownership roots, and canonical review checks. Included in the seven-file run: exit 0, **448 passed, 43 warnings, 66.95s**. Runtime validation source was traced but no full plugin matrix, live provider, browser, or PostgreSQL experiment ran. Proposed plugin ownership consolidation remains design, not a claim that current markers are generally unsafe.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_wire_blob_inline_ref`](../../../../src/elspeth/web/composer/tools/blobs.py#L1004).

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
    "field_path": {
      "type": "string",
      "description": "Canonical path: source.options.<field>, source:<name>.options.<field>, node:<node_id>.options.<field> for a transform, aggregation, or collector, or output:<name>.options.<field>. The interpretation_requirements option root is not settable on any source, node, or output path. On source paths, these server/resolver-owned roots are also not settable: blob_ref, blobs, source_authoring. Bind sources with set_source_from_blob or set_source_from_blobs instead. When field_path targets an LLM node, its runtime-owned top-level option is also not settable: approved_prompt_artifact_hash. For author-owned LLM option edits use patch_node_options, or upsert_node for a full node edit; blob wiring cannot author those values."
    },
    "blob_id": {
      "type": "string",
      "format": "uuid",
      "description": "Ready blob ID to wire as inline content."
    },
    "encoding": {
      "type": "string",
      "enum": [
        "latin-1",
        "utf-16",
        "utf-8",
        "utf-8-sig"
      ],
      "default": "utf-8",
      "description": "Text decoder used at runtime. Defaults to utf-8."
    }
  },
  "required": [
    "field_path",
    "blob_id"
  ],
  "additionalProperties": false
}
```
