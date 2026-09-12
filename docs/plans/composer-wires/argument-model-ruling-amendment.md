# Argument model amendment: authorized contract tightening

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

This amendment supersedes the pending choices and alternative no-model category in `argument-model-handoff.md`. It records the parent's supplied standing user ruling: replace open shapes that are not externally imposed and make tooling contracts precise. It authorizes implementation work described here, not provider calls, deployment, or operational database deletion. No production files were edited in preparing this amendment.

## Ready decisions and source evidence

| Decision | External-constraint check | Implementation |
|---|---|---|
| Owned admission for every shipped tool, including the ten empty-input definitions | Tool schemas are authored in ELSPETH declarations and `_dispatch.py`; `_closed_root_schema` locally supplies root closure. They are published to providers, not supplied by providers as a mandatory schema. No evidence requires permissive direct-handler admission. | Validate complete original input at each true handler boundary. Use shared `EmptyToolArgumentsModel` for empty-input tools. Reject extra root keys. Derive coverage from the live registry; do not hard-code 42 or ten as a permanent expected count. |
| Retire `wire_blob_inline_ref.sha256_override` | Repository search found only the handler read, one test, and redaction prose. It is absent from the three declared properties. No production caller supplying that literal key was found. Authoritative blob hash comes from `blob['content_hash']`, not the caller. | Remove the read/mismatch arm and stale redaction prose; replace the special override test with extra-key rejection. Preserve ready-state and non-null hash checks and `BlobInlineRef.sha256=pinned_hash` plus marker `sha256=pinned_hash`. |
| Reject explicit null on optional non-null advertised fields | `get_pipeline_state.component`, `list_models.provider`, and advisor `schema_excerpt` are locally advertised strings; omission carries the default behavior. Current null tolerance is hand-written/internal permissiveness, not a provider requirement. | Omission succeeds; supplied null fails. Keep explicit nullable fields, such as `get_plugin_assistance.issue_code`, nullable. Follow the schema for every other field instead of globally banning null. |
| `list_models.limit` is a positive JSON Schema integer, default 50 | `generation.py` locally slices/filter results and currently silently rewrites invalid/nonpositive values to 50. The declaration advertises integer and default 50 in prose. JSON Schema defines integral numbers such as 1.0 as integers; a Python-only distinction would disagree with the wire schema. | Accept finite integral numbers with exact normalization to int; reject bool, numeric strings, nonintegral numbers, null, zero, and negative numbers. Advertise `minimum: 1` and `default: 50`; preserve omitted default and positive values. No new maximum is authorized or justified by this finding. |
| `clear_source.source_name` has minimum length 1 and default `source` | `_execute_clear_source` already rejects empty strings at its local boundary. Its declaration currently lacks `minLength`. This is existing internal behavior, not an external naming requirement. | Model strict string with min_length=1 and default `source`; advertise `minLength: 1` and default. Preserve existing named-source resolution and missing-source result. |

These named changes are ready for implementation under the ruling. Evidence is repository-local and current source inspection, not a claim that all historical clients were surveyed. Invalid-input behavior intentionally tightens where the previous handler was more permissive than the published contract. Do not interpret an external caller's ability to send malformed input as an externally imposed requirement to accept it.

## Precise type treatment

Use the currently advertised JSON types and owned domain vocabularies. Apply strict scalar admission so `true` is not an integer and strings are not coerced into numbers. Extra-key rejection concerns complete owned object contracts; preserve declared plugin-option maps and other intentional data-bearing JSON objects where their keys are owned by plugin configuration or external data contracts. An arbitrary mapping field must have its boundary explained, not be closed blindly or left open merely for convenience.

Numeric correction, 2026-09-10: the earlier all-floats refusal for list_models was an internal planning mistake, not an explicit user preference. [JSON Schema's numeric reference](https://json-schema.org/understanding-json-schema/reference/numeric) treats 1 and 1.0 as the same integer value. A local Draft202012Validator probe confirms both pass type=integer/minimum=1, while 1.5, true, "1", null, zero and negative values fail. Apply that integer value semantics consistently to all advertised integer fields; retain ordinary JSON number semantics for number fields. No provider call was involved.

Do not blanket-enable strict Python tuple admission and thereby reject JSON arrays that existing models intentionally normalize to tuples. The input is decoded JSON: preserve published JSON representations while rejecting cross-type coercion. Verify number semantics explicitly (JSON number allows integer values); keep existing honest domain validators and authority checks. Do not turn malformed blob IDs into a different error channel merely by changing their declared string field to UUID.

For optional-but-non-null fields, distinguish omission from a supplied null in the model itself. An ordinary `str | None = None` without a supplied-null validator is insufficient. Use a narrow field validator that rejects supplied None while leaving the omitted internal default, and ensure the generated/exported schema still describes non-null supplied values. Avoid constructing partial dictionaries before validation. Add no synthetic wire sentinel key and no unsupported mutation of raw inputs.

The public advisor model has five fields and excludes backend-only `user_message` and checkpoint trigger variants. Validate before budget/provider work; keep public ARG_ERROR envelopes, safe errors, neutralization, exact formatting, and settings-based total prompt-size checks. A typed public-to-internal constructor may pass the five explicit field values to the existing shared implementation. This is a justified owned adapter, not a reason to publish internal checkpoint fields or retain raw public mappings indefinitely.

## Editing scope

Start with the exact integration points in `argument-model-handoff.md`, refreshing line numbers first:

- `tools/_common.py`: one shared empty argument model and existing safe validation helper.
- `tools/blobs.py`, `sources.py`, `sessions.py`, `generation.py`, `transforms.py`, `outputs.py`, `secrets.py`: original-input validation and typed reads as needed, including already model-bearing tools whose contracts are currently permissive.
- `tools/_dispatch.py`: advisor public schema and advertised limit/default constraints where owned there.
- `redaction.py`: reuse existing public model identities where suitable, remove retired override prose, preserve sensitive-key/redaction policy. Universal argument admission does not itself authorize weakening redaction.
- `service.py` and `tool_batch.py`: typed public advisor admission/handoff while preserving internal checkpoint path.
- Focused existing handler/schema/error-boundary tests plus census/gate tests. Replace stale behavior assertions intentionally; do not delete authority/integrity coverage alongside the override test.

MODEL coverage must follow true callers including set_pipeline's existing candidate preparation and advisor interception. Do not move production code just to fit the old census, validate a copied partial dict, or claim complete validation from a redaction manifest. Preserve unresolved census findings until those paths are measured correctly.

## Practical READ after typed models

Use a direct typed READ census with explicit caller and instance provenance, not the generic semantic-use analyzer contemplated by the earlier charter. Its precise claim is “these named fields are extracted from the admitted input on the registered handler/helper source path.” It is not proof of causal effect on every execution.

1. Resolve the actual handler and reachable explicitly supported owned helpers. Bind model locals only from original-input admission expressions, not annotations/class names. Follow direct argument forwarding and identity-bound casts; unresolved aliases/copies remain findings.
2. Collect direct declared model-field accesses and remaining literal raw reads. Exclude unused nested functions, foreign objects, and independent instances of the same model. Model validation and whole-model dumping never imply that every field is read. A nested field projection records its top-level input field only.
3. Report extraction locations and unresolved provenance separately. Do not label extraction as proven operational consumption. Do not hide reads outside SHIPPED, including any remaining private helper field.
4. Use meaningful behavior regressions to verify consumption: changing a field changes the intended resulting state, selected data, or request passed to a mocked boundary; optional branches and omitted defaults receive explicit probes. A dead-read/logging-only positive must not be offered as that evidence. This is cheaper and more truthful than attempting to infer arbitrary operational effects statically.
5. Prefer a handful of deliberate public-to-internal typed adapters and direct field reads where they improve the production contract. Do not add repetitive shadow reads solely to satisfy the census. If one opaque bulk conversion remains justified, report that limitation and pair it with focused behavior evidence; do not give it blanket per-field credit.

Examples: upsert_node id/type/plugin/options assertions on returned state; list_models positive limit and provider filtering with a deterministic model catalog; clear_source omitted/named source results; inline blob marker uses the stored hash and requested encoding; advisor stub captures all five public fields after neutralization. No live provider call is needed.

## Required acceptance probes

- Every current tool has an admitted owned argument model; every empty-input tool admits `{}` and rejects an injected key. An injected shipped property changes derived MODEL/READ parity and cannot disappear into an empty-tool exception.
- Strict scalar wrong-type probes, supplied-null versus omission probes, and intentional nullable fields tested in both directions.
- list_models defaults to 50, accepts 1 and larger JSON integers including finite integral float representations, refuses zero/negative/bool/string/nonintegral/null, and publishes minimum/default consistently.
- clear_source omitted default, valid name, empty string, supplied null, wrong type, and extra key.
- wire_blob_inline_ref rejects sha256_override as unknown while using authoritative metadata hash; missing hash still escalates and storage/hash integrity protections remain intact.
- Advisor invalid inputs consume no budget and make no provider call; safe ARG_ERROR classification remains. Internal checkpoint shape still accepts its own owned fields through its separate path.
- READ origin negatives: foreign model/state instance, unused nested function, root copy/alias ambiguity, and whole-model dump. Behavior tests verify the actual effect of representative fields rather than testing duplicated read statements.

No additional user ratification is needed for the named internal-contract repairs. New acceptance changes outside these rules, or evidence of an actual external protocol constraint, should be surfaced concretely rather than silently expanding this amendment.
