# Argument model preparation (read-only)

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> This is supporting historical preparation. Its optional no-model treatment and pending choices are superseded by the MODEL charter and ruling amendment. Universal admission is required; finite integral JSON numbers are allowed.

Evidence: supplied model-matrix.json, live get_tool_definitions(), handler source at campaign HEAD 1278c5c215fc8f749d53fd16d57413e416b93a00. This is a proposal, not ratified implementation. No production/tests edited.

## Reconciled categories

20 absent rows comprise 10 genuine no-input tools and 10 tools with shipped inputs. The 2 unresolved rows are different: set_pipeline already validates the complete owned model; request_advisor_hint has handwritten public boundary validation outside dispatch. Neither should be fenced merely to satisfy the census.

## Ten argument-bearing absent rows

Use module-local owned Pydantic models with extra="forbid"; validate the original argument object at the named handler boundary and consume validated.<field>. Keep domain validation, custody locking, redaction, and tool failure envelopes. Do not reconstruct a partial dict before model_validate, or validate a model and then continue consuming the original dict.

| Tool | Proposed model and exact fields | Integration point / reuse |
|---|---|---|
| get_blob_metadata | _BlobIdArgumentsModel: blob_id: str | blobs.py _handle_get_blob_metadata:411 before first argument subscript. Retain _blob_id_uuid_validation_error and session-scoped lookup. Shared identity model can also serve delete_blob. |
| get_blob_content | Existing GetBlobContentArgumentsModel: blob_id: str | blobs.py _execute_get_blob_content:1589, before current blob_id read. Import and validate existing redaction.GetBlobContentArgumentsModel; do not invent a duplicate redaction model. UUID/domain/custody checks stay. |
| delete_blob | _BlobIdArgumentsModel: blob_id: str | blobs.py _execute_delete_blob:1432, before argument read and before mutation/locking. Reuse metadata identity model; preserve UUID and reference/custody restrictions. |
| wire_blob_inline_ref | _WireBlobInlineRefArgumentsModel: field_path: str; blob_id: str; encoding: ContentEncoding = 'utf-8' | blobs.py _execute_wire_blob_inline_ref:621 before current argument reads. Reuse existing ContentEncoding (latin-1, utf-16, utf-8, utf-8-sig), UUID validator, and BlobInlineRef for owned domain validation. BlobInlineRef includes server-owned hash and is NOT a public argument model. sha256_override disposition below is mandatory. |
| clear_source | _ClearSourceArgumentsModel: source_name: str = 'source', min_length=1 | sources.py _execute_clear_source:2098 reached by _handle_clear_source:2125. Replace handwritten extra-key/type checks with model validation; keep missing-source failure. Existing SetSource/PatchSource models require other fields, so cannot be reused unchanged. |
| get_pipeline_state | _GetPipelineStateArgumentsModel: optional component, absent means full state | sessions.py _execute_get_pipeline_state:2162. Consume validated.component; retain exact special alias/node/output resolution order. Explicit null issue below must be settled; do not silently broaden schema. |
| get_plugin_schema | _GetPluginSchemaArgumentsModel: plugin_type: PluginKind; name: str | generation.py _handle_get_plugin_schema:249. Validate before _validate_plugin_name/catalog lookup. Reuse PluginKind source/transform/sink type; no existing suitable argument model found. |
| explain_validation_error | _ExplainValidationErrorArgumentsModel: error_text: str | generation.py _execute_explain_validation_error:1814. Validate before regex/text processing; preserve exact/fuzzy explanation behavior. |
| get_plugin_assistance | _GetPluginAssistanceArgumentsModel: plugin_type: PluginKind; plugin_name: str; issue_code: str | None = None | generation.py _execute_get_plugin_assistance:1923. Schema explicitly permits null issue_code. Consume three validated fields and retain catalog policy checks; replace redundant handwritten family narrowing with typed family. |
| list_models | _ListModelsArgumentsModel: optional provider; limit: strict int = 50 | generation.py _execute_list_models:2126. Consume validated.provider/limit. Preserve empty-string provider meaning "without prefix". Invalid types/null/negative limit decisions below must be explicit before choosing validators. |

Existing InspectSourceArgumentsModel in sources.py has the same one-field shape as the blob identity tools, but importing sources into blobs merely to reuse a trivial model risks unnecessary module coupling. Existing GetBlobContentArgumentsModel should stay the authority for get_blob_content because it is already the redaction-bearing model. A shared public blob identity model could later replace both deliberately; not necessary for this repair.

## Ten legitimate no-input rows

All ten live definitions have properties={}, required=[], additionalProperties=false. Their absence of reads is legitimate, not SHIPPED-not-READ. If universal model coverage is ratified, add ONE shared EmptyToolArgumentsModel(BaseModel) with extra="forbid" in tools/_common.py and call its model_validate on original input at each boundary below. There are no fields to read; successful validation is the entire input contract. Do not add ten bespoke empty subclasses, fictitious fields, or fences. Rejecting unexpected keys makes the implementation honor the already-shipped schema.

| Tool | Exact boundary |
|---|---|
| list_blobs | blobs.py _handle_list_blobs:354 |
| list_composer_blobs | blobs.py _handle_list_composer_blobs:379; replace del arguments with validation |
| list_sources | sources.py _handle_list_sources:157 |
| get_expression_grammar | generation.py _handle_get_expression_grammar:299 |
| get_audit_info | generation.py _execute_get_audit_info:2057 |
| preview_pipeline | generation.py _execute_preview_pipeline:3848 |
| diff_pipeline | generation.py _execute_diff_pipeline:3988 |
| list_transforms | transforms.py _handle_list_transforms:135 |
| list_sinks | transforms.py _handle_list_sinks:168 |
| list_secret_refs | secrets.py _handle_list_secret_refs:116 |

An alternative explicit no-input census category is honest if universal MODEL coverage is not desired, but it must prove the live schema is empty and unknown arguments are rejected by the dispatch boundary. A blanket "no model" skip is not that proof.

## Two unresolved rows

### set_pipeline: reuse existing model, fix measurement

sessions.py build_set_pipeline_candidate:807 already calls SetPipelineArgumentsModel.model_validate(args) and subsequently consumes its source/sources/nodes/edges/outputs/metadata. Redaction already uses the same class. Do not create another model or change the handler merely to appease the AST reader. Census must follow the existing candidate-builder input provenance. Existing SetPipelineArgumentsModel validators and source-authority handling stay authoritative.

### request_advisor_hint: own public model, preserve internal checkpoint boundary

service.py _validate_advisor_arguments:7238 currently validates the public shape by hand. Its actual caller is tool_batch.py:1696, before advisor budget use/provider calls. This path needs an owned RequestAdvisorHintArgumentsModel if MODEL coverage is required:

- trigger: Literal['proactive_security_safety', 'proactive_red_listed_plugin']; reuse the authoritative public trigger vocabulary rather than admitting backend checkpoint triggers.
- problem_summary: strict string, max 2000.
- recent_errors: strict list of strict strings, max 5 items, each max 2000 chars.
- attempted_actions: strict list of strict strings, max 8 items, each max 2000 chars.
- schema_excerpt: optional strict string, max 8000; omission versus explicit null verdict below.
- extra='forbid'.

Minimal integration: model_validate the original public arguments inside the existing service validation method (or a typed admission helper called there), then pass the owned result to public advisor prompt preparation. Keep the existing dynamic formatted-prompt cost check; it depends on settings and actual formatting and is not replaced by per-field length limits. Tool batch must still produce ARG_ERROR and no provider call/budget consumption on invalid input. If changing the method return shape, update tool_batch's caller explicitly and its tests.

READ requirement: consume all five typed fields while constructing the public advisor message/request. A bare model_dump then unchanged raw dict reads is only MODEL coverage, not the intended typed handler contract. The downstream shared _call_advisor_with_audit and _build_advisor_user_message also accept backend-authored checkpoint payloads, including user_message and distinct triggers. Keep those internal fields outside the public model. A small typed public-to-internal request constructor with explicit field reads is a justified handoff; do not add internal fields to the public model and fence them. Retain neutralization, secret scrubbing, size accounting, and exact formatted output.

## Ratification points and discovered drift

1. wire_blob_inline_ref currently reads an UNADVERTISED sha256_override at blobs.py:665. redaction.py:3446 describes it as a guardrail; test_blob_inline_tools.py:610 exercises mismatching override. It is not among the live three public properties. Preferred narrow disposition: retire this unreachable public extra and its special-case test, keep authoritative hash pinning, and prove unknown-key rejection. If an internal caller actually needs an assertion, move it to a separate owned internal API after proving its callers; do not publish it by accident. Adding a fourth public knob/fence requires a separate explicit ruling.
2. list_models currently silently defaults invalid limit types and limit<1 to 50; non-string provider falls back to provider summary. Schema says provider string and limit integer but gives no positive minimum. Strict model validation changes malformed-input handling for booleans/strings; making limit positive additionally narrows currently schema-valid zero/negative values. Preferred truthful contract is strict types and positive limit with schema minimum=1, explicitly ratified. Alternatively preserve integer<=0 normalization in a documented model validator and teach it; never silently choose.
3. get_pipeline_state.component, list_models.provider, and advisor.schema_excerpt are optional non-null strings in shipped schemas, while current handlers tolerate explicit null. A naive str|None=None model silently maintains a schema/model discrepancy. Choose reject explicit null while permitting omission, or advertise null deliberately; this is shape parity, not just field-name parity. Model defaults and model_fields_set / field validators can distinguish omission from explicit null without invented sentinel fields.
4. clear_source currently requires a nonempty string but its schema lacks minLength. Adding minLength=1 aligns published schema with existing behavior; record as a producer repair, not new semantics.
5. Model validation error envelopes should retain safe classifications and avoid echoing secret-bearing raw argument values. Use current ToolArgumentError / failure conventions; do not expose raw Pydantic errors wholesale.

## READ seam and focused evidence

For real inputs, trace each model field to direct typed access in handler/called owned helper, not merely to model declaration, serialization, or logging. Probe alternate optional branches: source default vs named root; full-state aliases and exact round-trip read; discovery vs issue-specific assistance; provider summary vs provider filter; default/explicit limit; default/explicit encoding; optional advisor excerpt.

No-input positive control: {} admitted; a newly injected property makes MODEL parity fail and unknown-key execution fails. Input positive control: inject a shipped field with no owned field/read and observe appropriate MODEL/READ failure. Domain probes retain blob UUID and custody restrictions. No provider calls, DB mutations, production edits, or tests were run in this preparation.

## Bounded historical persisted evidence supplied by parent

observed-argument-keys.json contains redacted persisted upsert_node and set_pipeline rows dated 2026-09-07. upsert_node records description/id/input/node_type/on_error/on_success/options/plugin; set_pipeline records all six top-level keys. Redaction projects options/metadata to strings and can fill defaults, including null sources. These rows corroborate that those persisted surfaces exist, but cannot establish which arguments the provider explicitly supplied or whether raw payloads satisfied current schemas. No set_output row was found in the decoded legacy OpenAI-shaped subset; other recorded envelope grammars remain undecoded. This is neither evidence that set_output is unused nor a basis for fencing any knob. The inspected missing-model rows therefore have no direct provider-presence claim from these samples.
