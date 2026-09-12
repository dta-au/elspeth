# Universal owned MODEL admission and strict schema alignment

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Preparation only while `error_twin_retirement` remains the active implementer. Do not begin repository edits until the parent releases ownership of the affected files. This charter authorizes no commit, provider egress, signing, deployment, or operational database deletion.

Authority: the parent's supplied standing user ruling authorizes tightening internally owned contracts and rejecting shapes the published tool schema does not accept. This replaces the pending choices in [argument-model-handoff.md](argument-model-handoff.md). Apply [argument-model-ruling-amendment.md](argument-model-ruling-amendment.md) and the measured cases in [existing-model-strictness.md](existing-model-strictness.md).

## Outcome and boundaries

Every currently shipped tool validates the complete original public input into an owned model on its actual authoring path, including empty-input tools and the advisor interception. Advertised root fields, requiredness, supplied nullability, owned nested records, and JSON scalar admission agree with those models. Known invalid values fail safely before operational effects. The MODEL census measures those actual admissions rather than substituting the redaction manifest.

This task does not build the READ analyzer or extend ADMITTED parity. Implement direct typed reads needed to make the new input models real; leave derived READ gates and broader redaction admission accounting to their separate tasks. No generic schema comparison framework or generic control-flow analyzer is required.

Also defer the applicability rule for `expected_output_count` with passthrough to [expected-output-count-alignment.md](count-applicability-implementation-charter.md). This task fixes demonstrated JSON-type coercion of that integer; it does not impose the separate cross-field runtime/import/export semantic rule. Read [expected-output-count-audit-ordering.md](expected-output-count-audit-ordering.md) before editing shared redaction/admission models or changing when validation happens. That report concerns structurally valid but semantically incompatible arguments. Structurally malformed calls in this task retain the existing safe ARG_ERROR summary; do not widen disclosure to preserve invalid raw field values.

## Exact ownership

One implementer should own these coupled admission surfaces after the current lane releases them:

| Files | Responsibility |
|---|---|
| `src/elspeth/web/composer/tools/_common.py` | Shared empty argument model, narrow common JSON scalar admission types where justified, existing safe ToolArgumentError translation. |
| `src/elspeth/web/composer/tools/blobs.py` | Missing blob argument admissions; reuse existing models where authoritative; typed field reads; remove hidden sha256_override read while preserving stored-hash authority. |
| `src/elspeth/web/composer/tools/sources.py` | Missing/empty admissions; clear_source nonempty name model and matching schema constraint; reuse redaction-bearing source models safely. |
| `src/elspeth/web/composer/tools/transforms.py` | Existing model strictness, owned trigger/metadata shape, empty admissions, complete original-input validation; no expected_output_count applicability rule. |
| `src/elspeth/web/composer/tools/outputs.py`, `secrets.py` | Existing model/schema supplied-null and strictness alignment, empty admissions, original-input validation. Preserve owned vocabularies and secret handling. |
| `src/elspeth/web/composer/tools/generation.py` | Missing discovery model admissions, positive strict list_models.limit, typed plugin family and open string filters, empty admissions. Preserve result behavior and repair advice. |
| `src/elspeth/web/composer/tools/sessions.py` | get_pipeline_state admission; existing set_pipeline/model provenance integration; session-aware review admission. Preserve candidate, custody, and state/version semantics. |
| `src/elspeth/web/composer/tools/_dispatch.py` | Advertised schema corrections and public advisor definition; maintain actual registry and error envelopes. |
| `src/elspeth/web/composer/redaction.py` | Existing shared model strictness/owned nested closure, deliberate retirement of hidden nested object-string coercion, sensitive metadata retained, stale override prose removed. Do not conflate rejection with losing attempted-call evidence. |
| `src/elspeth/web/composer/service.py`, `tool_batch.py` | True public advisor admission and typed handoff, safe ARG_ERROR/no-budget/no-provider behavior, internal checkpoint separation, audit ordering informed by the forthcoming report. |
| `scripts/cicd/composer_wire_census.py` | Resolve actual MODEL input ownership for candidate and advisor paths; preserve catalog closure and unsupported-provenance findings. Never count a manifest model as handler admission. |
| `src/elspeth/web/composer/tools/schema_contract.py`, `tests/unit/web/composer/test_tool_model_wire_parity.py` | Campaign Task 1.3: extend the existing schema-contract authority with symmetric MODEL key parity, and add/complete the live registry-derived MODEL gate. No new fence unless the parent approves a concretely justified narrow exception. |
| `tests/unit/scripts/test_composer_wire_census.py`, `tests/unit/web/composer/test_tool_argument_wire_parity.py` | Extend existing census probes; inspect the existing argument-wire test's distinct scope and preserve it rather than mistaking it for the MODEL pin. |

Existing production behavioral tests to extend are listed below. New small model-focused tests are appropriate where no existing fixture covers the admission boundary. Do not claim readiness by counting regex class declarations or maintaining a manual list of all tool names.

## Implementation sequence

1. Freeze/re-read the integrated starting tree and recent error-twin changes. Read CONTRIBUTING's whole-tree gates and the forthcoming audit-ordering report. Measure the live tool universe and actual handler/model relationships with local-only imports. Inspect complete touched files before editing.
2. Add one `EmptyToolArgumentsModel(extra='forbid')`, and validate original input for every schema-empty handler. A model with zero fields is real admission, not a no-model skip. Avoid ten bespoke classes and fake field reads.
3. Add missing argument-bearing models using the field contracts in the earlier handoff. Existing models are equally in scope: fix measured boolean/numeric-string coercion, supplied-null disagreement, and fixed nested record openness. Validate once at the true boundary where practical and consume the result rather than re-reading raw mappings.
4. Align published schemas for positive limit/default 50, clear_source minLength 1/default source, and closed owned nested records. Preserve declared nullable cases and plugin/data maps. Resolve nested object-string normalization as below.
5. Integrate typed public advisor admission without widening its field set to backend checkpoint data. Keep audit/custody ordering and safe failure behavior.
6. Resolve MODEL census's remaining candidate/advisor provenance with explicit actual callers or narrow typed adapters. Do not rewrite correct production candidate preparation just to satisfy an AST spelling rule. Unsupported source must remain visible until proved.
7. Run focused normal and same-selection mutation checks, then the parent integrates and runs required whole-tree checks. Report any remaining census/schema gap explicitly; no broad fence, skipped no-model row, or blanket signed-allowlist action.

Task 1.3 MODEL pin: derive every tool row and SHIPPED property set from the live registry; derive MODEL fields from complete ORIGINAL argument admission on its actual caller path. Require a model for empty-input tools too. Refuse missing models and unresolved/invalid provenance before comparing field names in both directions. The manifest's redaction model remains a separate observation and can never substitute for admission. The key-parity assertion belongs in the existing `schema_contract.py` authority, exercised by `test_tool_model_wire_parity.py`; it proves top-level name equality only. Type, nullability, requiredness and numeric behavior need the separate behavioral/schema probes described here and must not be presented as consequences of key equality.

## Contract details that must survive

- `list_models.limit`: JSON Schema integer, minimum 1, omitted default 50; accept finite integral numbers normalized exactly to int, reject bool, numeric strings, nonintegral numbers, null, zero and negative values. This corrects the earlier Python-only float rejection, which conflicts with the published JSON Schema integer semantics (see the ruling amendment's official reference and local probe). Publish minimum/default. No new upper bound. `provider` remains an open prefix string, including existing empty-string behavior; omission differs from explicit null.
- `clear_source.source_name`: strict nonempty string, omitted default source; reject supplied null/empty/wrong type/extra keys. Preserve named-source errors.
- Existing `expected_output_count`: reject bool and numeric strings at upsert and nested set_pipeline admission. A finite integral JSON number such as 3.0 passes the published JSON Schema integer rule, consistently with list_models.limit. Reject nonintegral/nonfinite numbers; preserve existing range semantics.
- Existing nested trigger count/timeout: reject demonstrated wrong JSON types; keep JSON integer values valid for number fields, deliberate nullable cases intact, and existing domain/range semantics unless separately authorized. Incremental trigger is an owned record, unlike arbitrary plugin options.
- Optional non-null strings: permit omission and reject explicitly supplied null in the admission model; generated/exported schemas must also express that distinction. Do not use bare `str | None = None` as the complete fix. Preserve explicitly nullable cases such as upsert_node.plugin, upsert_edge.label, and set_pipeline.metadata.
- JSON arrays are legitimate public input even if owned representations normalize them to tuples. Do not blanket-enable strict Python container admission and break arrays. Strict scalar types and fixed-record closure must not prohibit extensible plugin configuration/data keys.
- Blob IDs remain advertised strings with existing UUID/domain error handling. Do not accidentally relocate malformed-ID errors or weaken session/custody checks.
- Remove sha256_override from handler parsing and stale prose. The stored blob content_hash, ready-state check, non-null hash integrity failure, BlobInlineRef.sha256 and emitted marker.sha256 remain authoritative.

## Retire hidden nested object-string coercion deliberately

`_LlmJsonObject` currently uses `_coerce_stringified_json_object` to repair nested strings where the public schema advertises an object. Historical source/tests document malformed output from a provider, not a provider/SDK requirement that those nested fields be strings. Preserve that historical reason in a concise change/test note, but strict public admission now rejects the malformed nested value with actionable safe feedback.

Remove the relevant BeforeValidator conversion rather than assuming strict=True defeats it; conversion runs before strict field validation. Retire obsolete helper/metadata only after checking actual users, then update its dedicated acceptance tests to rejection tests. Keep valid object arguments, sensitive markers, legitimate top-level JSON-encoded tool arguments, and bounded decoding for input paths that still decode JSON. Do not delete resource-boundary tests just because one conversion is removed; relocate retained decoder guarantees to their real remaining authority if needed.

## Advisor and audit semantics

Public RequestAdvisorHintArgumentsModel contains only trigger, problem_summary, recent_errors, attempted_actions, optional non-null schema_excerpt. Use existing public trigger vocabulary and documented string/list caps, reject extras, and keep the settings-dependent total formatted-prompt check. Internal checkpoint triggers and user_message remain in a separate owned internal input path.

Pass typed values explicitly into the public-to-internal request constructor. A raw model_dump followed by unchanged public dictionary reads is not the intended admission contract. A small explicit projection is preferable to duplicating prompt formatting; preserve neutralization, redaction, exact formatted output, and shared backend checkpoint behavior.

Translate validation failures through current safe ToolArgumentError/ARG_ERROR conventions. Never expose raw Pydantic input/error payloads containing secrets. Invalid calls must be observable in their existing safe ARG_ERROR audit representation, leave state/version unchanged, and consume neither advisor budget nor provider calls. Do not claim that representation preserves supplied values: it deliberately retains only bounded error classification and field-count evidence. Do not globally relax the model or redaction policy, or add raw argument disclosure. The separate semantic applicability task must preserve redacted structurally valid mode/count values through an ordinary failed ToolResult, as the audit-ordering report explains.

## Meaningful verification and same-selection mutations

Use local catalog/provider stubs, existing state fixtures, and whole-model valid baselines before changing one field. Keep the same selected tests for baseline and each mutation; a mutant is killed only when the relevant test fails for the intended behavior. Restore only the implementer's own mutation, never user/sibling work. Mutations are a few deliberate temporary implementation changes, not a new mutation-testing framework.

| Intended guarantee | Normal test | Same-selection mutation that must fail |
|---|---|---|
| Empty handler validates | `{}` accepted; injected extra rejected on real handler path | Remove that handler's empty-model validation call. |
| Existing numeric admission is strict | Valid whole node/pipeline baseline; true/"3" rejected; accepted numeric/null controls retained | Remove the new scalar strict validator/type at one tested field. |
| Omission differs from null | Omitted optional non-null value accepted, explicit null rejected, schema agrees | Remove supplied-null rejection for the tested field. |
| Extra owned keys rejected | Valid trigger/metadata baseline plus injected unknown property | Change the tested owned model to ignore extras. |
| Nested object string rejected | Literal object accepted; string encoding rejected through actual boundary | Restore the old BeforeValidator conversion. |
| Original-input admission measured | Registered actual caller validates full input; copied partial input cannot satisfy census | Replace original-input validation with selected-key reconstruction. |
| MODEL pin is derived and non-vacuous | Every registered tool, including empty-input tools, has a proven admission model with matching top-level fields | Remove the admission model, add one shipped property without a model field, or invalidate original-input provenance. Each mutant must fail explicitly, not skip or fall back to the manifest. |
| Stored hash remains authoritative | Extra override key rejected; emitted marker hash equals stored content hash | Replace the marker/hash assignment with caller-selected data or stop empty/extra validation in the tested path. |
| Advisor rejects before effects | Invalid field yields safe ARG_ERROR, unchanged budget/state, zero mocked provider calls, attempted-call evidence retained | Move validation after mocked provider/budget consumption or bypass it. |

Do not overbuild mutations: representative discriminating cases plus the full derived catalog gate suffice. Preserve domain fixtures and authoritative-hash tests while retiring the obsolete mismatch-override expectation.

Focused tests to extend: `test_tool_dispatch_boundary.py`, `test_tool_schema_contract.py`, `test_redaction_trust_boundaries.py`, `test_coerce_stringified_json_object_args.py`, `test_blob_inline_tools.py`, `test_tools.py`, `test_set_pipeline_candidate.py`, `test_redact_tool_call_arguments.py`, `test_redact_set_source.py`, and existing advisor/tool-batch tests selected by actual call sites. Also rerun current MODEL census/parity tests after shared integration changes. Exact fixture/test names should come from source at implementation time, not this historical file list.

Execute with explicit worktree source roots and interpreter provenance, `LITELLM_LOCAL_MODEL_COST_MAP=True`, private logs, and recorded exit codes. No provider calls. Required whole-tree checks, trust-tier delta accounting, and PostgreSQL checks when persistence/locking paths actually change remain the parent campaign's integration obligations; no signing or global allowlist cleanup.

## Handoff criteria

Return exact modified paths, live derived MODEL universe/results, focused test and same-selection mutation outcomes, public schema behavior changes, and any remaining unsupported provenance or audit-ordering dependency. Do not claim whole-tree green from focused results. READ extraction/behavior proof, ADMITTED parity, and expected_output_count applicability remain explicitly separate follow-ons.
