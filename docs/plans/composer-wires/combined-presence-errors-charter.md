# Implementation charter: authored presence and structured validation errors

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> The epoch values 53 and 54 below are historical coordinates, not current targets. Re-read both session epoch pins at implementation and advance them together from that actual baseline.

Read-only preparation verified at HEAD 1278c5c215fc8f749d53fd16d57413e416b93a00. No production changes, database changes, or provider calls were made. This replaces the original presence handoff's instruction to leave all authority comparison sites unchanged.

## Authorized code versus operational cutover

Prepare, test, review and integrate reversible code for both contracts together. Current SESSION_SCHEMA_EPOCH is 53 in src/elspeth/web/sessions/models.py:312; src/elspeth/web/sessions/schema.py:35 pins the coordination hard cut to 53. Recheck integrated HEAD, then increment both together (54 at this inspected HEAD), documenting both persisted semantic changes. Retain the existing startup rejection of old SQLite and PostgreSQL stores. Test using newly created temporary stores and deliberately stale temporary stores.

The sparse display change also belongs in this cutover: historical pending proposals contain default-inclusive argument projections, while post-change dispatch envelopes are sparse. A real audit serialization/binding probe confirmed their hashes differ for identical raw arguments. Do not add legacy readers, backfills, dual projections, or silent fallback. Old-epoch rejection is the supported boundary. Implementation and guard tests do not require resetting any actual session database. Deployment/reset, loss of session history, and any operational recreation remain operator decisions. Do not delete, migrate, rewrite, reset, or connect to production stores; do not change Landscape/auth epochs.

## Presence contract and authority seam

Retain frontend-presence-handoff.md's sparse validated redaction design: model_dump(exclude_unset=True) only for argument projection, before Sensitive substitution; shared helper defaults remain inclusive for responses. Keep explicit nonsensitive null and omit absent fields recursively. Preserve nested options/blob redaction and corrected sparse metadata summaries. Canonical proposal publication compares and persists the exact newly produced display, without inline_blob normalization.

The existing inline_blob:null equivalence is only a semantic dispatch-binding convention. Use one helper to compute the redacted semantic hash from normalize_set_pipeline_redacted_arguments(value), without widening its transformation. Use that helper in PipelineDispatchAuditBinding.from_persisted_envelope after canonical/hash/projection validation, and at every service comparison against row.arguments_redacted_json. Current exact comparison sites are src/elspeth/web/sessions/service.py:7725,8026,12676,12837,12952. This fixes both first guided dispatch recording and later recovery.

DO NOT normalize private invocation authority: PipelineDispatchAuditBinding.from_invocation, pipeline_commit.py:571 and service.py:12835 bind exact private arguments to row.tool_arguments_hash. DO NOT normalize publication equality, row/event display equality, _pipeline_audit_payload_hash, private arguments, or proposal draft hashes. Null omission must remain detectable in the displayed/audit projection even though the already established dispatch equivalence recognizes both as no inline blob.

Presence production paths:
- src/elspeth/web/composer/redaction.py: _redact_via_schema, redact_tool_call_arguments, existing normalizer and narrowly named semantic-redacted hash helper.
- src/elspeth/web/composer/pipeline_commit.py: from_persisted_envelope semantic hash only.
- src/elspeth/web/sessions/service.py: new proposal publication and the five dispatch comparison sites above.
- src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx: replacement scope from projection dispatch/registry; unconditional pipeline/component replacement notice; empty state says "No difference in the supplied arguments this view can compare." Preserve the separate option-values-not-compared ledger.
- scripts/cicd/bootstrap_proposal_diff_fixture.py and src/elspeth/web/frontend/src/test/fixtures/redacted-tool-arguments.json: actual producer regeneration, preserving order.
- scripts/cicd/bootstrap_redaction_snapshot.py and tests/unit/web/composer/redaction_policy_snapshot.json: inspect/regenerate legitimate summarizer-byte drift; Sensitive path inventory stays unchanged.

## Structured validation-error contract

One required-key record, no extra fields:
{"message": str, "error_code": str | null, "component": str | null}

Use a frozen nominal session record, explicit serializer, strict Tier-1 persisted decoder, strict HTTP model, and matching TypeScript type/decoder. The collection is nullable; preserve existing null/empty semantics at each producer rather than opportunistically normalizing them. Reject string rows, missing keys, unknown keys, wrong scalars, and malformed containers in current-epoch storage. An external JSON decoder must validate before constructing owned types; owned constructors/consumers use nominal types. No dict-or-string compatibility union.

The three fields form a closed object shape. Current ValidationEntry.error_code is str | None (state.py:1509); do not claim an exhaustive closed code enum exists. Existing producer-owned codes can be carried directly. If implementation introduces a closed code enum, derive its cases from the actual code producer contract and sweep consumers, rather than improvising one from messages.

Project message/error_code and only surface-permitted component directly from owned ValidationEntry. Do not add contract dumps, rejected input, plugin IDs, row schemas, or arbitrary context. Known interpretation-review pending producers construct the code and permitted component directly, replacing colon-packed storage. Message-only producers construct records with null code/component; never parse prose to recover identity.

Preserve SessionPendingInterpretationValidationResult's digest-bound tuple[str] contract (protocol.py:1337-1347). Its persistence adapter constructs message-only records. Guided disclosure emits only the existing closed invalid status, now {message:"guided_composition_invalid", error_code:"guided_composition_invalid", component:null}; valid remains null. Never expose raw guided validation text or component through this change.

Frontend humanization consumes structured error_code/component for applicable error kinds, retaining message as readable text/details. Do not manufacture producer/consumer identities from contract prose. The safe three-field contract cannot represent two edge endpoints: use accurate generic/single-component text where that is all the record supports. Warnings/suggestions already have separate records and must not be forced into an error-only collection. Update their shared-humanizer callers explicitly if the function signature changes.

Structured error production/transport paths, verified references:
- src/elspeth/web/sessions/protocol.py: CompositionStateData:1065 and CompositionStateRecord:1155; add/own the error record and preserve pending result:1337.
- src/elspeth/web/sessions/service.py: row decode:9977; persistence:6366; patched-validation producer:8745; clone/fork/copy/equality paths:10442,12450,12612,13512,13558,13706-13717; blob-custody free-text checks:1913-1943 must inspect serialized message/component fields without losing custody detection.
- src/elspeth/web/sessions/_persist_payload.py: ensure nominal records are explicitly serialized through the actual payload adapter, not deep_thaw assumptions.
- src/elspeth/web/composer/service.py: persisted validation producer:2570-2584, including pending-site code/component.
- src/elspeth/web/sessions/pending_interpretation.py: persistence adapter:2117-2120; preserve message-only nominal authority producer:1691-1706.
- src/elspeth/web/sessions/guided_replay.py: closed status:133-151, normalization:278-280, strict equality/HTTP projection:410-423.
- src/elspeth/web/sessions/schemas.py: CompositionStateResponse.validation_errors:333 and new strict nested response record.
- src/elspeth/web/sessions/routes/_helpers.py: response:808; runtime preflight error constructor:1218-1247; ordinary persisted preflight projector feeding:2474-2487. Existing diagnostic strings can become honest message-only records; the known runtime_preflight_failed code belongs directly on its producer record, with no prose parsing.
- src/elspeth/web/sessions/routes/sessions.py: fork custody check/copy:629-645.
- src/elspeth/web/sessions/routes/composer/guided.py: _guided_persisted_validity and save/response sites; guided_chat_atomic.py:2306-2336; guided_plan.py:459; compose.py:648. Typed forwarding callers must remain coherent.
- src/elspeth/web/frontend/src/types/index.ts: CompositionState.validation_errors:255; src/elspeth/web/frontend/src/api/guidedDecoder.ts:2224-2226 strict nested record parsing.
- src/elspeth/web/frontend/src/lib/validationHumaniser.ts and callers: stores/subscriptions.ts, components/chat/guided/PipelineValidationSummary.tsx, components/sidebar/SideRailValidationBanner.tsx, components/execution/ValidationResult.tsx. Sweep component/store fixtures and ordinary HTTP state consumption as well as guided decoding.
- evals/lib/composer_rgr_score.py:685-721: retire assumptions that actual HTTP state carries only strings; use actual CompositionStateResponse fixtures with structured records. Do not retain twin compatibility reads simply to keep tests green.

Do not blindly replace every validation_errors occurrence: execution discard accounting uses a number, other routes use distinct detailed validation/Pydantic records, and Landscape validation tables are separate domains. This charter targets the session composition-state error contract and directly related consumers.

## Dependencies and test matrix

1. Implement owned error record/serializer/decoder and both epoch pins together. Unit tests: tests/unit/web/sessions/test_protocol.py, test_persist_payload.py, test_schemas.py, test_models.py, test_service.py. Verify malformed current-epoch payloads fail and old-epoch startup is refused without mutation; tests/testcontainer/web/test_schema_probe_postgres.py covers PostgreSQL sentinel behavior.
2. Sweep error producers, persistence, HTTP, guided replay and frontend atomically. Tests/unit/web/composer/test_mid_turn_state_payload.py; tests/unit/web/sessions/test_persist_compose_turn.py, test_guided_atomic_settlement.py, test_fork_custody_settlement.py, test_interpretation_trust_boundaries.py; tests/integration/web/composer/guided/test_persisted_validity.py and test_progressive_disclosure.py. Add real response-to-frontend fixture parity and evals/lib producer-backed fixtures via tests/unit/evals/lib/test_composer_rgr_score.py. Check null/empty, coded/uncoded, guided disclosure, pending digest authority unchanged, malformed records and forbidden contextual fields.
3. Sparse redaction with exact publication plus narrow semantic binding. Tests/unit/web/composer/test_redact_set_source.py, test_redact_tool_call_arguments.py, test_redact_tool_call_response.py, test_row_union_authority_hashing.py, test_pipeline_commit_operation_authority.py; tests/unit/web/sessions/test_composer_proposals.py; tests/integration/web/composer/test_pipeline_proposal_lifecycle.py. Required new case: explicit inline_blob:null survives row/event/display restoration AND first durable guided dispatch record, acceptance/rejection and retry/recovery succeed; omitted counterpart succeeds. Tampered other field/order still fails; private raw authority unchanged. Old default-inclusive pending projection is intentionally excluded by epoch guard, not silently accepted.
4. Frontend ProposalDiff.test.tsx: omitted non-null current fields do not fabricate supplied-null rows; explicit null does compare; empty and nonempty replacements always disclose reset behavior; option caveat remains independent. Run generated-producer fixture parity test_proposal_diff_redaction_fixture.py, guidedDecoder.test.ts, validationHumaniser.test.ts and affected component/store tests, frontend typecheck/lint.
5. Run whole-tree gates after integration with frozen-tree evidence: ruff, mypy, contracts, key-free lints corpus comparison, pytest default full suite AND serial testcontainer suite because sessions persistence is touched. Use scripts/full-suite-gate.sh with both source roots bound, required provenance and per-stage exits. Do not restage global signatures or claim operator signing.

All code, strict decoders, epoch guards and test evidence can be prepared now. Operational store recreation remains outside this charter; no request for permission is needed before reversible implementation.
