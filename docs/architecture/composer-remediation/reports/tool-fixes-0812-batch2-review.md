# Independent review: 0.8.12 tool fixes, Codex batch (U2, U4, U6, U7, U8, U9)

Reviewed at tools worktree HEAD 981ac946d; composition checked in merged tree 2caf10780.
"Ran" = executed; "read" = source reading only. Logs under
`composer-0812-tools/.claude/lanes/tool-fixes-review/`.

## MUST-FIX

### M1. U9 (6e8ca6956) turns a whole-tree gate red: protocol.py module-bindings recipe not refreshed
- `tests/helpers/required_work_reserve_recipes/reviewed-writer-recipes.json` key
  `src/elspeth/web/composer/protocol.py:module-bindings` pins the stable AST dump of every
  non-def/class top-level statement in `protocol.py`. U9 rewrote four `PIPELINE_STAGED_*`
  string constants (top-level bindings) and did not touch the JSON.
- Verified by running (instrument controlled): recomputed
  `stable_ast_dump(module-bindings)` for `protocol.py` at `60bbcb409` -> equals the JSON
  (`True`); at HEAD (2caf10780) -> `False`. The JSON still contains the old wording
  ("staged it for your review", "interpretation review cards").
- Consumers: `tests/unit/architecture/test_web_landscape_mutation_fencing.py:5900-5912`
  (`_reserve_recipe_failures`, via `_reserve_ReserveProof`, used by
  `test_required_work_reserve_admission_on_actual_sources` and siblings) and `:10704`
  (retained/private transfer namespace check, `composer_turn.py` family). Expected: those
  fail with "transitive non-DML recipe or imported bindings changed" / "namespace changed
  src/elspeth/web/composer/protocol.py".
  Targeted test was started (`reserve.log`); see the addendum at the foot of this file if it
  finished. The binding mismatch above is the measured cause regardless.
- Fix: regenerate that one JSON entry from the current `protocol.py` (reviewed-recipe
  refresh, not a hand edit of other keys), or move the strings out of module bindings. This
  is exactly the CONTRIBUTING "whole-tree gate" class a scoped run misses. Other touched
  files (tool_batch.py, blobs.py, transforms.py, generation.py, proposal_blob_refs.py,
  error_codes.py, _generation_responses.py) are absent from both recipe JSONs (read).

## SHOULD-FIX

### S1. U2: new ValueError paths on six more tools are uncaught at proposal creation (read)
`_BLOB_REFERENCE_TOOLS` now includes `upsert_node/set_source/set_output/patch_*`, so
`validate_proposal_blob_references` (mutation_capabilities.py:129) runs for them and
`proposal_blob_reference_ids` raises plain `ValueError` for e.g. `options={"x": {"blob_ref": 5}}`
or `blob_ref: ""` (proposal_blob_refs.py `_collect_option_blob_references`). tool_batch.py:2405
catches only `ProposalResourceUnavailableError`. Scenario: explicit-approve mode, model emits
`upsert_node` with a non-string `blob_ref` inside free-form options; the preview `execute_tool`
accepts free-form options, proposal creation raises ValueError out of the batch. Same hazard
already existed for `set_pipeline`; U2 widens it. Also `pending_proposal_reference_id`
converts such a malformed pending row into `AuditIntegrityError` on every later
`delete_blob/update_blob` in that session. Suggest: either validate blob_ref shape at tool
admission or map ValueError at proposal creation to a refusal. Not run.

### S2. U6: commit message over-claims
"preserve the credential guard across page boundaries" - the diff adds only the byte gate
(blobs.py). The full-content guard before slicing already existed (comment at the
`_credential_safe_blob_discovery_result` call). No behaviour defect; fix the message/report
so the audit trail is honest (project is an AI-authorship evaluation).

### S3. U7: pending approval-mode edits are invisible to diff_pipeline, and nothing teaches it
In explicit-approve the diff correctly reports 0 changes (test asserts it), but the
declaration/skill only say "request-start state". A model that proposed an edit and then diffs
sees `total_changes: 0`. Skill text pipeline_composer.md:603-606 covers only the
`diff_baseline_unavailable` case. One sentence in the declaration or skill would prevent a
retry loop. (read)

## NOTES

- Per-commit mutation controls (reasoned, not mutated): U2 tests assert delete refusal /
  reference ids for the six tools; without the `_OPTIONS/_PATCH` sets in `_BLOB_REFERENCE_TOOLS`
  they return `()` / allow delete -> fail. U4 tests need `severity="warning"` and the three new
  fields admitted; without the model widening `admit` raises FrameworkBugError. U6 test needs
  code `blob_content_size_limit` (also absent from the registry -> redaction test fails).
  U7 tests need `baseline=` wiring: without it payload `success` is False
  (`diff_baseline_unavailable`). U8 edge-free tests fail at the old "exactly one direct visual
  edge" refusal. U9 tests assert new wording.
- Ran in merged tree 2caf10780, all exit 0: `test_freeform_diff_baseline.py` (4 passed, incl.
  explicit-approval and next-request reset); `test_validation_guidance_catalogue.py` +
  `test_error_code_redaction.py` + the U6 size-limit test (636 passed);
  `test_splice_transform_tool.py` + `test_pending_pipeline_review_guidance.py` +
  `test_planner_stage_runtime_preflight.py` (76 passed). These do not cover M1.
- No `# noqa`, `type: ignore`, broad except, `getattr/hasattr`, or new production
  `Mapping[str, Any]` in any of the six diffs (grep with a positive control: it matched the
  three `dict[str, Any]` helper signatures in the U7 test file). Test-only `dict[str, Any]` in
  `test_freeform_diff_baseline.py:_call/_response/_diff_payloads`.

## Per-commit

### U2 f45dd925d - keep with named fix (S1)
Fixes the stated defect: pending option mutations now retain blobs; present-but-None
options/patch = no references; non-dict non-null refused; nested ownership/status checks
unchanged (shared collector, `validate_proposal_blob_references`). Argument field names match
(`options` for upsert_node/set_source/set_output, `patch` for patch_*). Sibling effect: creation
of these proposals now also requires referenced blobs be same-session and ready
(`ProposalResourceUnavailableError` -> existing `proposal_resource_unavailable` refusal path),
which is the desired consistency with set_pipeline. No frontend/skill dependence.

### U4 f76e646be - keep
Response model widened (`severity` adds "warning"; `selected_record_count`,
`sample_extra_record_count`, `all_rows_inspected` with `ge=0`, nullable, still rejected when
None by `_evidence_presence`); JSON evidence locators gain `"source": "blob"` so the
blob-attribution validator passes (attribution adds `source_name`, generation.py:4246).
Round-trip test (`readmit`) and an end-to-end preview test were added. Warnings do not flip
`preview_is_valid` (test asserts). Frontend does not read `evidence_locator`/proof severity
(grep, no hits); skills do not mention these fields. Note the test's warning case expects
`all_rows_inspected is True` with one extra of two rows - consistent with the code.

### U6 38045378b - keep (fix message per S2)
New code `blob_content_size_limit` registered in `_EMITTED_VALIDATION_ERROR_CODES`; gate is
`len(data) > max_string_bytes` (262144), identical to the credential traversal bound, so
at-limit passes and over-limit gets a distinct, non-credential refusal (test: ascii-over,
utf8-over, at-limit). Frontend/skills do not reference the code or message.
**Xfail inventory edit** (`tests/unit/architecture/test_session_db_mutation_authority.py`):
exactly three `_REVIEWED_READ_CONNECTIONS` `line=` values in `tools/blobs.py` changed:
`_sync_get_blob` 502->503, `_sync_list_blobs` 538->539, `_sync_list_ready_blob_inline_descriptors`
563->564 (the new import adds one line above). Fingerprints, ordinals, authority, classification
unchanged. Verified at HEAD: `def` lines are 501/537/562 and the `with engine.connect()` sites
503/539/564 match. Ran
`test_all_production_sessions_writers_are_reviewed_typed_authorities` at tools HEAD: it still
XFAILS (106 unexpected, 30 connections outside authority, 74 unresolved, 2 stale read
connections - cli.py:5043 and shareable_reviews/service.py:655; zero mention of
tools/blobs.py). So with the edit: status xfail, reason set contains no blobs.py entries.
Without the edit (derived, not run): the import shifts the three sites by one line, so
`_identity_key` (includes `site.line`, test comment at :6266) would no longer match; those three
would surface as additional "Unexpected" and "Stale reviewed read connections" entries. Status
stays xfail either way (it xfails on pre-existing drift); only the reason set grows by those
entries. The edit is a pure line-number refresh and does not change the xfail's status or
the pre-existing reasons. Acceptable.

### U7 59006f509 - keep (S3 optional)
Fixes the stated defect: both `execute_tool` sites in tool_batch.py (preview at ~2250, dispatch
at ~3238 in the merged tree) now pass `ctx.turn_evidence.effects.initial_state`. That value is
set once in `TurnEvidence.__init__` (`turn_effects.py:360-366`) from the state at request entry
(service.py:1205, planning_application.py:1089), never reassigned (grep: only read), so it is
fixed across provider rounds, retries inside one request, and same-batch ordering. Approval
mode: held edits leave `state` unchanged, so diff = 0 (by design, matches spec C); the
approval-containment code in the merged tree does not substitute state, and the 4-case test
(same-batch/later-round x auto/explicit) passes in the merged tree. Next request resets
(test). Production loops all thread `turn_evidence` (service, planning, completion);
`None` still yields `diff_baseline_unavailable`, with reworded message/repair text. The
skill (pipeline_composer.md:603) still says "do not retry... never tell the user earlier
versions do not exist" - consistent with the new wording. Diff stays uncacheable (unchanged).

### U8 196123d38 - keep
Fixes the stated defect (empty whole edge tuple). Composer invariant: it does NOT make the
server author structure. Model-supplied: `predecessor_id`, `successor_id`, the inserted node
(id, plugin, options) and the pre-existing logical wiring (`predecessor.on_success ==
successor.input`). Server computed (pre-existing behaviour of this tool, unchanged in kind):
the inserted node's `input`/`on_success` and a unique `derived_connection` name, rewiring
`successor.input`. U8 *removes* server derivation in the edge-free case (no edge ids invented;
`edges` stays `()`; response omits `replaced_edge_id/new_edge_id`). All scalar, type, branch,
error-route, multi-consumer and sink checks still run; nonempty edge tuples keep the exact
old checks (mixed/partial-edge graphs are still refused). Replay branch: `if not state.edges:
return None` comes after the logical-connection, consumer, sink and node-adjacency checks, so
idempotency still validates the logical splice. The declaration text was updated for the
conditional response fields. Residual: the connection-name derivation is a pre-existing
server-side naming act (validation-adjacent plumbing, not pipeline topology the planner chose);
worth a one-line ADR/skill note if the maintainer wants the invariant stated explicitly.

### U9 6e8ca6956 - keep with named fix (M1)
Copy-only; green-only auto-commit and trust re-read untouched. Wording is accurate per
branch: REVIEW (explicit or revoked auto-commit; also used at composer_turn.py:1155),
FINDINGS, NOT_RUN, PENDING_INTERPRETATION; no "validated" in non-green constants
(existing test still passes); auto-commit users are not told "auto-commit paused"
(asserted). The mounted-route test proves accept -> candidate-bound interpretation card
(76 tests above passed). Frontend does not match these strings (grep). The only defect is
M1: the protocol.py module-bindings recipe pin was not regenerated.

## Addendum (gate test)
See below if appended.

M1 confirmed by running: `test_required_work_reserve_admission_on_actual_sources` in the merged
tree (2caf10780) FAILS (exit 1, 123 s):
`AssertionError: ['writer target/field recipe changed src/elspeth/web/composer/protocol.py:module-bindings']`
(`tests/unit/architecture/test_web_landscape_mutation_fencing.py:25422`; log `reserve.log`).
