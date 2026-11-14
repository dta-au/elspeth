# Review 2 of tool-fixes 0.8.12 (U2, U4, U6, U7, U8, U9)

Reviewer: independent read-only. Worktree composer-0812-tools, uncommitted diff over HEAD 60bbcb409.
Method: read `git diff` plus callers; ran 393 tests with `-n 0` (test_source_proof_readiness,
test_generation_response_contracts, test_freeform_diff_baseline, test_splice_transform_tool,
test_proposal_blob_refs, test_pending_pipeline_review_guidance, test_error_code_redaction): exit 0,
393 passed. Import provenance verified (elspeth.__file__ resolves into the worktree).
Patch check: applied u2,u4,u6,u7,u8,u9 patches in order to a clean `git archive HEAD`: all apply,
and every live changed path is byte-identical to the reconstruction EXCEPT
tests/integration/web/test_shareable_reviews_routes.py (it is carried by validation-fixture-commit.patch,
a separate fixture unit, not U2-U9). U4 and U7 each apply alone to HEAD: their generation.py hunks are disjoint.

## MUST-FIX
None found.

## SHOULD-FIX
1. U7, approval mode: baseline = request-start state, and approval mode never changes state in-request, so
   `diff_pipeline` returns total_changes 0 for a turn that staged a proposal (test pins this:
   `total_changes == 0` for explicit_approval). A model may read "0 changes" as "nothing staged".
   Not a defect of the unit (the declaration says "request-start state"), but consider one sentence in the
   tool description or skill. Cost: prompt ceiling has only 7 bytes of headroom (135,723 vs 135,730), so any
   added text must be paid for elsewhere. Read, not run.
2. U2: absent `options`/`patch` also returns () (not only None). Safe because required-argument validation runs
   upstream, but it is the same "absent = no refs" loosening; confirm the tool argument models make these
   fields required for the six tools. Read only.

## NOTES (verified)
- U2: None -> (); non-dict non-None still raises ValueError (`type(options) is not dict`); tests include 6 null
  and 12 wrong-type controls. Nothing else loosened; set_pipeline / blob tools branches unchanged.
- U4: the changed contract is the producer-side Pydantic discovery response model `_generation_responses.py`:
  ProofDiagnostic.severity gains "warning" (producer already emitted it at generation.py:4066) and
  ProofEvidence gains three optional fields (selected_record_count>=0, sample_extra_record_count>=0,
  all_rows_inspected). Strict superset: previously admitted payloads stay admitted; absent fields omitted from
  wire. grep of src/elspeth/web/frontend/src finds no decoder of proof_diagnostics/evidence_locator;
  redaction.py has no key manifest for them; no persisted schema touched. Added `"source": "blob"` in the three
  JSON branches (required by `_evidence_presence`). Tests (393 above) pass.
  Skill text only names "blocking" severity (pipeline_composer.md:1341): not contradicted.
- U6: `blob_content_size_limit` added to `_EMITTED_VALIDATION_ERROR_CODES` (the redaction allowlist) and to the
  registered-code redaction parametrization. Read-pin change in test_session_db_mutation_authority.py is three
  line numbers +1 (502/538/563 -> 503/539/564) for blobs.py symbols shifted by the one new import line; I
  confirmed `_sync_get_blob` `engine.connect()` is now at blobs.py:503. Honest re-pin, not loosening.
  Limit test covers ascii-over, utf8-over and at-limit.
- U7: both `execute_tool` sites in tool_batch.py pass `ctx.turn_evidence.effects.initial_state` (per-request
  TurnEvidence, built at service.py:1205/2419 and planning_application.py:1069 from the loaded state, so after
  any previously accepted proposal; no cross-session carrier). None when no evidence -> original refusal.
  Test covers same-batch/later-round x auto/approval, and reset on next request. The prompt-limit change only
  shortened the declaration prefix; ceiling and +1 control unchanged; test_tool_declarations pin updated to
  the same string. The first-turn "empty baseline" is not directly tested (initial states are version>=1 empties;
  I could not exercise it ad hoc; low risk since diff_states takes any CompositionState).
- U8: Composer invariant satisfied. The splice acts only on the model-named predecessor/successor and the
  logical on_success connection (all pre-existing logical checks retained); with `edges == ()` it neither
  creates nor infers any edge (`edge_data` empty, response omits edge ids). Connection-name derivation is
  pre-existing committed behaviour. Replay branch returns None on empty edges after the immediate-order check,
  so identical replay is deterministic (already_applied). Mixed graphs (some edges, wrong ones) are still
  refused. Declaration text updated accordingly; skill text mentions splice only by name.
- U9: all four messages are truthful in both trust modes: they are only emitted when a proposal is staged
  (auto-commit downgraded to review on non-green; composer_turn.py:1155 reuses PIPELINE_STAGED_REVIEW_MESSAGE
  on revoked authority, where "not applied" is true). No frontend/test consumer of the old strings remains.
- Hygiene: no new noqa/type-ignore/broad except/getattr/skip/xfail in the src diff; no new Mapping[str, Any]
  parameters (the only signatures touched are unchanged). Test changes strengthen, none weaken; the
  shareable-reviews change replaces a raw chat_messages insert with the fenced session-service helper (repairs a
  direct-writer violation already on HEAD) and binds provenance to the returned message id.

## Verdicts
- U2: land as-is
- U4: land as-is
- U6: land as-is
- U7: land as-is (optionally SHOULD-FIX 1)
- U8: land as-is
- U9: land as-is
- validation-fixture patch (shareable reviews test): land as-is; note it is a separate unit, outside U2-U9.
