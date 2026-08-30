## Wave-1 end-of-wave hub work
- recent-code-hints.md: land B11's two entries (type() is vs isinstance else-branch narrowing; @overload on Literal action shifts body[N] ast paths).
- re-stage reconciliation: rationale keys are file:RULE:Symbol:ast=<path> (no fp); B11 sink_effects.py body[N] +3 after _returned_attempt.
- stale allowlist entries created: B11 state_guard.py:101 R1, :104 R5, :389 R5 (already-signed sites removed).
- source_snapshot_sha256 shifts: B04 url_validation move.
- masquerade_baseline.yaml reseeded by B13 — later lanes may conflict.
- Promoted: elspeth-6a9eb088c6 (P1 bug, sink.py Protocol dispatch) — own lane after wave 1; elspeth-a881fce8bc (P3 provider model split).
- Observations open: elspeth-obs-a85afe0dde (P3, lease-owner rule duplicated) — sweep at wave end.
- obs elspeth-obs-cfa45ccb84 (P2, tier_model try/except join drops source_param) — promote at wave end; B20 already added recent-code-hints entries.
- B01: known-weak rationale sink_effects._require_bounded_positive_int (assert isinstance int) — if judge BLOCKs, escalate to require_int TypeIs tree-wide, do NOT re-word. obs elspeth-obs-3cf090c480 (P3 lint post-init R5 exemption gap) — decide after sweep. obs elspeth-obs-d5c598e219 routed to B12 (export.py:164, audit_export_effects.py:311).
- B16 obs: elspeth-obs-c12a90675a (4 signed entries bind dead symbols — stale_delete), elspeth-obs-49ae0fbb2b (P? docstring), elspeth-obs-2dbf1496b7 (S3 prepare_effect spool leak) — sweep.
- WAVE-END CHECK: scenario-corpus manifest pins plugin source_file_hash; every plugin-touching lane (B13,B14,B16,B17,B18,B19,B20,B21,B15) must have re-pinned or the corpus tests fail — full suite will show; B17 re-pins json_sink.
- WAVE-END: regenerate tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json (drift xfails GREEN, py3.13-only) — obs elspeth-obs-2d409e128b; B19 obs elspeth-obs-2ccc7259c5 (json missing text_field KeyError vs csv routing).
- BRIEF FIX for wave 2+: boundary decorators suppress ONLY R1/R5 (trust_boundary.py:73). R2-R4/R6-R9 = real code or rationale. Rationale key corpus is MIXED (ast= vs fp=) — reconcile at re-stage. B22 obs: elspeth-obs-ad0b51bb91 (P3 cross-thread counter), elspeth-obs-c6998fc57d (P3 signed rationales cite line numbers).
- WAVE-END: a red in composer authoring tests that poison litellm import (generation.py:1816, planner_authoring_aids.py:1868) = fixture modelling OLD empty-tuple behaviour, not a B15 regression.
- WAVE-END REVIEW (John's ask): scan the FULL wave-1 diff (ff917243a..wave-1 tip) for control-location errors like B06's execution_repository.py:1104 — both directions: (a) a swallowed/contained signal that LOOKS like a lost integrity gate but the real gate lives elsewhere (rationalise correctly, don't "fix" into a regression), and (b) a removal/narrowing that deleted the REAL gate because a lane believed the control lived elsewhere (regression). Also rationales that name the wrong control. Two independent lenses: fable general-purpose adversarial "where does the control actually live" audit + yzmir-systems-thinking:pattern-recognizer for second-order effects. Feed findings back as fix lanes before re-stage.
- WAVE-END: PH3 stale source_file_hash in sources/dataverse.py, sinks/dataverse.py, azure/document_intelligence.py (B04's import rewrite, not re-pinned) — re-pin before suite (test_plugin_hashes_json_mode_succeeds_on_current_codebase). B18 obs elspeth-obs-bb84aea40e (textract safe_config.get sibling).
- Re-stage authoring rule (obs elspeth-obs-c6998fc57d): rationales cite SYMBOLS/excerpts, never line numbers — put in the annotate brief. Hub-owned at re-stage: elspeth-obs-c12a90675a (4 dead signed entries → stale_delete), elspeth-obs-0dbb251f06 (allow_hits[159] steps.py). Hub-owned wave end: elspeth-obs-2d409e128b baseline regen (py3.13.1 OK).
- B17 chroma metadata scalars now exact type() — IntEnum/StrEnum/numpy subclasses DIVERT; audit candidate.
- JOHN: PAUSE at end of wave 1 — do NOT create wave-2 issues/worktrees/lanes until he says go. Close-out order: hygiene merge → full suite (background, worktree) → audit findings triaged (fix lanes if regressions) → close 23 bucket issues → report.
- Lens B (8726): repeat two-lens audit before wave-2 sign-off, esp. B26-28 and B38/B54. S1 closed-union doc → hygiene hints. audit_export_effects.py:305 isinstance(db) dropped w/o replacement — cosmetic (AttributeError 2 lines later).

## WAVE 2 (authorised by John 2026-08-29)
- Wave-1 suite @53f6c10d8: 43366 passed / 1 failed (sink-factory test double was a callable instance; B14 removed isinstance(type) guard) → fixed 2ca7d2227 (fixture). All Wave-1 issues CLOSED @2ca7d2227. wave1-suite worktree removed.
- Round 1 dispatched @2ca7d2227: B38(fable) B44(fable) B24 B25 B43 B47 B48 B49 (opus). Worktrees .claude/worktrees/tier-B*, issues in wave2-issues.txt, brief LANE_BRIEF_W2.md, worklists worklists2/.
- Round 2 queue (largest first): B34 B35 B42 B33 B52 B53 B37 B36(fable); Round 3: B41 B46 B31(fable) B51 B39 B32 B50 B30(fable); Round 4: B29 B40 B45 B55(fable).
- Merge order: --no-ff per lane; recent-code-hints conflicts additive; masquerade baseline conflicts drop both + seed_baseline --check.
- Wave-end: two-lens control-location audit (esp. B38/B43/B44/B47), full suite in a worktree (-n 16), close issues, then Wave 3 needs John's go? (John authorised wave 2 only.)
- RE-STAGE: tool_batch.py has 3 R4 + 1 R5 fp= entries in web.yaml rotated stale (B43 never touched the file) + R6 per-file rule matched 5/3 — drift_repair lane, not lane residue.
- RE-STAGE: composer/service.py 10 stale signed entries in web.yaml (_serialize_response_via_walker, _backend_surface_args_for_site x6, _run_advisor_checkpoint x2, _cleanup_recipe_fast_path_blob [symbol gone → stale_delete]) — B33 did not cause. ToolResult is subclassed in tests/ → type() is unsafe at _ToolOutcomeResponse discriminators (B33 hint).
- RE-STAGE RULE for stale-signed sites (fp rotated; B43 vs B52): dead steps.py entry disables nothing (B33+B52 measured; per-entry continue). For each site under a `Stale tier-model allowlist entry`: (a) if a Wave-2 sidecar carries an `ast=` rationale for it → sidecar wins, the stale fp entry becomes stale_delete (B52's diagnostics.py:R6:_summarize_error_payload_for_llm fp=adee98bd09ce79dd, routes.py:R1:create_execution_router:get_run_output_content fp=41717a08b3857ff5); (b) if no sidecar rationale and the code at the site is unchanged → drift_repair/resign, keep the signed ruling (B43's tool_batch.py ×4 + R6 per-file ratchet); (c) if the finding is gone → stale_delete. Never two records per site. allow_hits[154] (steps.py) → stale_delete.
- JOHN RULING 2026-08-29: honest fix ALWAYS beats minimising churn; signing effort LOWEST priority. Re-stage rule amended: drift_repair only for signed sites that are binding-equivalent and where no honest fix removed the finding; lanes may edit signed sites. tool_batch.py 9 stale-signed sites → lane B43b (worktree tier-B43b).
- RE-STAGE STEP (B37 obs elspeth-obs-ca3fd469e9): before any drift_repair/resign, READ each stale signed entry's rationale prose against the live code — chat_solver._parse_step_2_sink_tool_arguments ×4 are factually wrong AND their sites are gone → stale_delete. Per-file R5 ratchet chat_solver 31/11 over cap (obs elspeth-obs-872262ee44) → operator cap decision or lint try-join fix (elspeth-8d46db34ff).
- B53: CORRECTION — 4abc48063 WAS in the merge (tip had 7 keys); tier/B53b@cdfc60aa9 merged to restore the 2 → 9 keys. Counting allowlist entries: use grep '^- key:' — bare grep counts prose.
- WAVE-2 AUDIT PROBE (B51): deep_freeze renders NESTED mappings as mappingproxy → `type(x) is dict` below a frozen options bag is ALWAYS False. Audit every `type(x) is dict`/`is list` introduced in Wave 1+2 (git diff ff917243a..tip -- src | grep 'type(.*) is \(not \)\?dict') on values that can be composition state (NodeSpec/SourceSpec options, deep_freeze'd payloads): does the else-branch silently skip a guard? Known-safe per lanes: B36 redaction (producers emit to_dict/deep_thaw/JSON dicts, pinned), B38 planning (hash-sealed JSON). Verify, don't trust.
- RE-STAGE: execution/service.py 7 stale signed fp entries (672f676…, 49e0a3c…, a05f176…, d1c4b87…, 34ecae2…, 03a8ebd…, 8b3cf7b…) → stale_delete (3 sites removed, 4 re-justified in B51 sidecar).
- RE-STAGE: tools/generation.py 5 dead signed entries (4× R6:compute_proof_diagnostics → symbol extracted to _compute_proof_diagnostics_for_source; 1 fp re-rolled) → stale_delete; _row_fields_referenced_by_condition has 9 isinstance sites vs 8 signed → bundle needs a 9th (obs elspeth-obs-07866fb4e4). WAVE-END: wardline PY-WL-102 active 17 (was 6 on 08-28) — attribute before gate.
- WAVE-END HYGIENE (B32 handoff): planner_authoring_aids.py:847 `.get(plugin_id, ())` on usable_profile_aliases — same honest fix as PolicyCatalogView._usable_profile_aliases (name build_plugin_snapshot as control); B42 rationalised it instead.
- WAVE-END HYGIENE (B32 obs elspeth-obs-cf02fe5c79): knob_schema.py accept-gates (_attach_tier:363, _attach_required_when:385, _is_composer_hidden:443/:460) are silent-skip on a frozen json_schema_extra; _is_composer_hidden should fail closed (audit anchor). Pre-existing, unreachable today; fix in hygiene lane with a frozen-input pin. Audit heuristic: POLARITY first (accept-gate = hazard), reachability second.
- WAVE-END HYGIENE (audit-frozen-narrowing #8784, 0 DEFECT/0 LATENT): pin provenance for two silent-skip accept-gates safe only by an upstream deep_thaw — web/execution/service.py:_discover_blob_rows_sources (frozen config → [] → blob_rows admission block skipped; protected by deep_thaw at ~1970) and web/composer/redaction.py:normalize_set_pipeline_redacted_arguments (frozen → unchanged; callers thaw). Add a frozen-input test at each proving the producer thaws OR make the gate fail closed. planning.py:_canonical_state_from_private_pipeline on frozen raw → bare TypeError (unreachable, sole caller thaws) — consider AuditIntegrityError.
- RE-STAGE FACT (B45): a signed entry binds by scope_fingerprint, NOT fp=/ast_path — fp/ast drift after sibling module-level insertions does not unbind it; only scope_fingerprint mismatch reports Stale. Reconcile sidecars to bundle by file:RULE:symbol and let the bundle's binding decide. B41 sidecar false ToolResult clause fixed by hub (obs elspeth-obs-055a2798bc).
- OPERATOR CALL (B43b): tool_batch.py per-file R6 rule matched 5/3 — the rule's `reason` sanctions 3 shapes; 2 genuine boundaries were added by later features (advisor TimeoutError arm; required-controls speculative preview). Options: update reason + max_hits to 5 (cap counts SANCTIONED sites, argued not a budget), or commission the ~170-line proposal/custody extraction (obs elspeth-obs-8b5c2d89e5, P2: two set_pipeline finalization blocks share 36 identical lines incl. an 18-field ToolContext). 3 stale sealed entries (fp 4527c354…, 1f22b58d…, ef16f6fc…) → stale_delete, superseded by B43b sidecar. LINT FACT: narrowing a broad except converts R4→R6 at the same line (only a non-default-returning handler clears it) — hints entry landed.
- P1 REGRESSION (B43b, self-caught): tool_batch.run_tool_batch prevalidation-rejection arm `type(candidate_data) is dict` — ToolResult.__post_init__ freeze_fields(data) → mappingproxy → guard always False → PREVALIDATION_REJECTED payload shape changed ({"candidate_data": proxy} instead of spread keys). 8838 tests passed over it (coverage hole). Fix + frozen-input test on tier/B43c (in flight). Merged at ce399cd1b. LESSON: the CONTAINER freezes the field after construction — check the dataclass, not the producers.
- WAVE-END MANDATORY [DONE 2026-08-28 — lens A comment 8798 ran a range-wide sweep over 2ca7d2227..d68447450: B43b tool_batch is the ONLY hit; every other added exact-type site reads pydantic/json.loads/to_dict/deep_thaw output or is frozen-aware]: re-run the frozen-narrowing audit over 92a846c66..tip (merges after the first audit: B41 6cada217b, B50 2ce7446f9, B32 d1f88d7d9, B46 d549399b5, B39b/c, B55 1354c95ee, B29 e3f2e1896, B45 7228f2b39, B43b ce399cd1b, B40 pending) with the polarity-first heuristic and the "check the dataclass that holds the field" rule.
- OPERATOR (B40 obs elspeth-obs-e2ff0cc748): _common.py per-file rules — R1 rule now 'Unused' (delete), R5 rule ratchet 13→7; both reason prose stale. LINT FACT: boundary-suppressed sites DO stop counting toward max_hits (narrows B37 note).
- W2 AUDIT lens B (#8797): F1 = known B43b (fix in flight); F2 low (str-subclass tolerance divergence _node_str_option vs _backend_surface_args_for_site, no production path); F3 → bug filed (run_status_projection.py:207). OPEN from lens B: composer-invariant 'server authors pipeline structure' half UNSWEPT — lens A must cover or it stays an explicit gap in the wave sign-off.

## WAVE 3 PREP (2026-08-29, hub; DISPATCH AWAITS JOHN'S GO)
- Scope per plan doc (authoritative over the handover's compressed line): Wave 3 = B54 (web/plugin_policy)
  + B56–B61 (web/sessions). B26–28 (_aws_ecs_acceptance, 455 raw lines incl. suppressed) stay Wave 4,
  pending the module-level @trust_boundary decision John reserved.
- Live counts at base (corpus @2b6e87bc4, findings only, R_TB_SUPPRESSED excluded): B54=121, B56=161,
  B57=39, B58=11 (+engine.py), B59=32, B60=53 (+routes/messages.py, routes/composer/{proposals,compose}.py),
  B61=58 — 475 total. Worklists: scratchpad `worklists3/B*.md` (per-file findings + signed entries binding
  each file). Brief: scratchpad `LANE_BRIEF_W3.md` (amends `LANE_BRIEF_W2.md`, which applies verbatim).
- Signed coverage in scope: 44 entries across web/sessions (guided.py 16, _helpers.py 14, state.py 4,
  schema.py 4, service.py 3, compose.py 2, messages.py 2, _auto_title/engine/proposals 1 each); ZERO in
  plugin_policy — B54 works its whole corpus with no subtraction. Honest-fix-wins ruling applies.
- Rounds: R1 = B56 (fable, SOLO — nothing else touches web/sessions while it runs) + B54 (opus) +
  hygiene lane (opus: the wave-end hygiene list above — provenance pins for _discover_blob_rows_sources
  + normalize_set_pipeline_redacted_arguments, knob_schema accept-gates fail-closed w/ frozen-input pin,
  planner_authoring_aids `.get(plugin_id, ())` honest fix, _canonical_state_from_private_pipeline bare
  TypeError → consider AuditIntegrityError). R2 after B56 merges = B57 B59 B60 B61 B58 (opus). ≤`-n 2`
  per lane; every brief mandates an explicit `-n` (elspeth-xdist-auto defaults to `-n auto`).
- Wave-end gates: two-lens control-location audit (wave-1 lens B mandated a repeat "esp. B38/B54"),
  range-wide frozen-narrowing sweep, full suite in a worktree (`-n 16`, background), close issues, PAUSE
  before Wave 4. Filigree bucket issues are NOT created yet — create under epic elspeth-3ab6107b1f at
  dispatch.
- Still owed before the re-stage (NOT a Wave-3 gate): elspeth-23ee8e3440 (stage_status --lanes /
  --continue-on-block) and the operator's check-judge-quality re-run for the f0e38838d rotation.

## WAVE 3 CLOSED (2026-08-29, hub; tip e94fe3250)
- All 7 buckets + hygiene lane merged --no-ff over 25cce9cdb..771e36a89; wording lane merged e94fe3250.
  Corpus (findings-only, allowlist-disabled, hub-measured at every merge): 2873 → 2535 = **338 removed**
  (B56 127, hyg 1, B54 113, B58 11, B59 14, B60 23, B57 22, B61 27); ~119 rationalised across sidecars
  B{54,56,57,59,60,61}.rationales.json. Full suite GREEN @771e36a89: 43485/66/1x, 0 failed (18:16, -n 16).
- Two-lens audit (comments 8814/8815 lens B, 8816 lens A; wording fixes merged via tier/w3wording):
  ZERO fail-open regressions, ZERO deleted real gates, composer invariants clean, all 10 B54 widenings
  honestly pinned (fingerprints match the rule's own resolver). Every surviving finding was PROSE
  (false mechanism, true conclusion) and is now rewritten to measured ground — see the w3wording merge.
- Real defects found+fixed inside the wave: B59's tool-outcome projection read the audit envelope at the
  wrong level (applied mutations rendered as lookups; eval parity copy carried the identical bug — both
  fixed, producer-minted tests); B60's fork blob-custody absent-`options` fail-open; hyg3's reachable
  redaction nested-frozen normalisation miss; B54's catalog_items unguarded index; eval classifier
  lockstep drift (lens-B F1, 060d95649).
- RE-STAGE additions from Wave 3 (all measured, per-lane comments have the entries):
  * stale_delete: B56 ×3 (already stale at base), B58 ×1 (engine.py fp=d823ee01639278b7, cleared by fix),
    B59 ×6, B60 ×9 (incl. `_reattach_guided_blob_refs` ×4 — zero live findings, pure dead weight),
    B61 ×5, B57 ×16. Zero binding rulings were disturbed anywhere in the wave.
  * OPERATOR (lens-A F3): the Wave-2 B43-era `_validate_schema_form_payload` @trust_boundary invariant
    carries half-false "arrive deep-frozen" prose and MAY be signed — never hand-edit; at re-stage, read
    the prose against live code and route per the sidecar-WINS/drift_repair rules.
  * Commit 4f11a28a3 (B61)'s message contains an impossible deep_freeze claim; no sidecar entry carries
    it. Correction text is in epic comment 8817 — treat the sidecar + this ledger as authoritative over
    that commit message.
  * BEFORE the next stage_scan: land elspeth-0bd4fb6042 (P2 — tier_model rule's _R5_NAMED_BOUNDARY_CONTEXTS
    dead entries + staleness reporting; deleting them surfaces ~3 currently-exempt findings honestly).
- Follow-up tickets filed this wave-end: elspeth-0bd4fb6042 (P2 lints map), elspeth-3b6708ef3d (P3
  blobs_inline far-away-thaw), elspeth-b5d005a913 (P3 service.py accept-gates → reject).
- Wave 4 (B26–28, web/_aws_ecs_acceptance, ~434 findings) remains PAUSED on John's module-level
  @trust_boundary decision (plan doc note); the wave-3 evidence for that call: boundary-per-payload-type
  removed 113/121 in plugin_policy (B54) and the acceptance harness is Tier-3-by-construction throughout.

## WAVE 4 PREP (authorised by John 2026-08-29; base 99d43f87d)
- John's rulings this session: (1) Wave 4 GO with per-payload-type `@trust_boundary` declarations in B54's
  shape (NOT a blanket module declaration); (2) flow.py archive DROPPED — directory deleted, obs
  elspeth-obs-bb4ce637ce dismissed with the ruling; (3) land elspeth-0bd4fb6042 (exemption-map cleanup) and
  elspeth-23ee8e3440 (stage_status flags) as fix lanes alongside the wave.
- Base corpus @99d43f87d (allowlist-disabled, findings-only): 2535 whole-tree; web/_aws_ecs_acceptance = 411
  (B26=198, B27=192, B28=21; s3.py 0). Saved: scratchpad `w4_prefix_corpus_raw.txt`. ZERO signed allowlist
  entries and ZERO per-file caps bind the package — lanes work the whole list, no subtraction.
- The exemption map (0bd4fb6042) has NO entries for the harness, so the fix lanes and the wave lanes touch
  disjoint files; the ~+3 findings the map cleanup surfaces (routes/_helpers.py, lens-B F4) are hub-owned
  after merge, counted OUTSIDE the Wave-4 removal number.
- Dispatched @99d43f87d, worktrees `.claude/worktrees/tier-*`, `-n 2` each: tier-lintmap (fable,
  elspeth-0bd4fb6042), tier-stagecmd (opus, elspeth-23ee8e3440), tier-B26 (opus, elspeth-1213f153ae),
  tier-B27 (opus, elspeth-23dd89ac63), tier-B28 (fable, elspeth-be73a74f17). Briefs: scratchpad
  `LANE_BRIEF_FIX_COMMON.md`, `LANE_BRIEF_LINTMAP.md`, `LANE_BRIEF_STAGECMD.md`, `LANE_BRIEF_W4.md`
  (+W2/W3 verbatim); worklists `worklists4/B2*.md`.
- Merge plan: --no-ff per lane after hub's own scan + full diff review; lintmap merge → hub re-measures the
  whole-tree base and records the surfaced findings; wave-end: full suite in a worktree, two-lens audit,
  close issues anchored at the tip, then PAUSE (re-stage is the operator seam: ONE stage_scan after the
  last fix merge; f0e38838d judge-quality re-run still owed by the operator).
- CORRECTION (caught by tier-B27's reconciliation, hub-measured): the hub's findings-count regex
  `^[a-z_/]+\.py:[0-9]+:` rejects digit-bearing filenames, dropping s3.py (23), plugins/sources/aws_s3_source.py
  (18), plugins/sinks/aws_s3_sink.py (8), web/composer/telemetry_phase8.py (10) = 59 findings from every
  Wave-3 and Wave-4 absolute count. Correct regex: `^[a-z0-9_/]+\.py:[0-9]+:`. Corrected absolutes: Wave-3
  base 2932 (not 2873) → Wave-3 close 2594 (not 2535); the 338 removed and the 1,304 W1–W3 total are
  UNCHANGED (the dropped files did not move). Wave-4 base @99d43f87d = 2594 whole-tree, harness = 434
  (B26=198, B27=215 incl. s3.py, B28=21). s3.py belongs to B27 (worklist and brief corrected in place).
- tier-lintmap MERGED (b5a5ecc51, elspeth-0bd4fb6042): map re-keyed to qualified symbol paths (84 → 78 live
  entries; 12 dead removed; 2 bare-name collisions made explicit: state.py from_dict ×6, redaction.py
  provider ×2 — narrowing needs per-site evidence), single resolver backs the test pin + whole-tree ERROR.
  Hub scan: 2594 → 2594, comm -3 EMPTY (corrected regex). CORRECTION to the prep note above: deleting a DEAD
  entry moves the corpus by ZERO — the "+3 surfacing" never existed (routes/_helpers.py:964/970/971 were
  already in the base). Lane committed with SKIP=elspeth-lints-trust-tier after an A/B showing the hook's
  rc=1 is the elspeth-13f0cc04fb fail-closed state, byte-identical on base and branch.
  (lintmap follow-up: `task` type has no `verifying` status — stays in_progress until wave-end close; the
  merged commit body of b5a5ecc51 quotes the old-regex 2535/1235 figures — superseded by this ledger, not
  rewritten. Lane re-derived under the corrected regex: 2594→2594 and real-allowlist 1270→1270, both empty.)
- tier-B28 MERGED (0d0214b9d, lane HEAD a80e2aa79, elspeth-be73a74f17): 21 → 2 (19 removed, 2 rationalised:
  EINTR flock retry R6, close-in-finally R4). Hub scan 2594 → 2575, outside-bucket identity EMPTY. Real fixes:
  lock open-or-create handler returns the published descriptor; receipt_store parses (schema) BEFORE binding;
  textract `_client_error_code` = non_raising boundary over ClientError.response with None failing closed in
  `_probe_invocable`. Hub-reviewed flags: lexists/unlink(missing_ok) accepted (exclusive create is the
  os.link FileExistsError path; dangling-symlink pin added). Lane's own absolute counts (2592/2573) carry a
  2-line offset vs hub's; deltas identical (19).
- tier-stagecmd MERGED (lane HEAD c0cf09f39, elspeth-23ee8e3440): every staging tool now emits
  `sign_bundle_plan` (per-lane actions + judge calls from `sign_bundle_transaction._JUDGE_GATED_KINDS`,
  lane-scoped `--lanes` commands cheapest-first, `--continue-on-block` above 10 judge calls, un-rationaled
  justify advisory). Whole-bundle `sign_bundle_command` kept; handoff doc + both skill twins updated.
  Corpus unchanged (no rules/ edits). Issue is a bug type → `verifying` accepted; close at wave end.
- Retroactive check on the regex blind spot (raised by tier-stagecmd): the four digit-named files carry an
  IDENTICAL 59-finding set at the Wave-3 base (postB43c), Wave-3 close (w3_final) and Wave-4 base, and
  `git log 25cce9cdb..99d43f87d` on them is empty — Wave-3's outside-bucket identity checks were blind to
  those files but nothing moved in them; every Wave-3 identity claim stands.
- tier-B27 MERGED (lane HEAD d92ead51b, elspeth-23dd89ac63): 215 → 25 (190 removed, 25 rationalised), 19
  boundaries, 11 new pinning tests. Hub scan 2575 → 2385, outside-bucket identity EMPTY. Real fix: approvals.py
  imported Ed25519PublicKey lazily inside `except Exception: return False`, so a missing/broken `cryptography`
  read as a bad signature on the gate guarding terraform apply — hoisted to module scope. Hub at merge
  (941b9718f): corrected the payload_root rationale (false "pydantic Path field with validate_default" claim on a
  METHOD — capture sub-lane caught it, lane shipped it uncorrected) and reseeded the masquerade baseline (−1:
  contracts.py::check_error_with_cause now amnestied by its boundary; 37 entries). Lane self-reports worth
  keeping: `-c core.hooksPath=.git/hooks` inside a worktree runs ZERO hooks (silent --no-verify); `type(x) is
  Path` is a live bug (pydantic yields PosixPath); gate_ledger got no boundary because 4/5 callers are write-path
  self-checks (read-back prose would be false). Filed by lane: elspeth-obs-539724f2db (wrong check id on
  malformed captured_at). Issue title/description corrected to 215.
- tier-B26 MERGED (e0caff32d, lane HEAD b425286a7, elspeth-1213f153ae): 198 → 23 (175 removed, 23 rationalised).
  Hub scan 2575 → 2400 vs ae34b48b3, outside-bucket identity EMPTY. Hub-reviewed in full: threaded receipt-node
  budget replaces the nonlocal counter exactly; `_sentinel_observed` extracted as a staticmethod boundary;
  `_operator_receipt` exact-type on the OperatorTelemetryEvidence/OutageEvidence closed union (git grep: no
  subclasses). Measured method corrections now in recent-code-hints: helper return values DO keep the trail
  (W3-4 was wrong); the trail-killer is a name assigned inside a `try:` and read after it; "extract a
  `_decode_or_none` helper" trades the loss for a fresh R6. Fragility noted by lane: `_validate_scenario_inventory`
  test_ref points at a shared parametrized test — any added param case rotates its fingerprint. Hints conflict
  resolved additively. Masquerade baseline up to date (37); trust_boundary gates rc=0; 1108 harness/arch/lints
  tests green on the merged tree. NOTE: `docs/architecture/adr/024-...md` is modified in the shared checkout by
  ANOTHER session (not hub, not any lane) — left untouched; `tier-lintprecision` worktree likewise not ours.
- B26 positive proof (post-merge re-measure at fb95c4c89): its five files emit exactly 175 R_TB_SUPPRESSED lines
  = the 175 findings removed — every removal is a live suppression, none a voided decorator (zero
  R_TB_NONLITERAL/MALFORMED/UNKNOWN_KWARG/STACKED tree-wide). Lane's post-merge branch commit 7852f55db (tip
  re-merge + hints reword, --no-verify with hand-run gates) was superseded by the hub's e0caff32d and deleted.

## WAVE 4 AUDIT (range 99d43f87d..e0caff32d; evidence scratchpad/auditW4A, auditW4B)
- Corpus at e0caff32d: 2594 → 2210 = 384 removed (harness 434 → 50; outside-harness identity EMPTY vs base).
  W1–W4 total 3,898 → 2,210 = 1,688. John 2026-08-29: goal was ~2k — "I'll call it a win".
- Lens A (code, fable, 5 sub-lanes): ALL-STOP NO. 17 exact-type sites, 120 removed guard lines, 18 membership
  conversions, 8 restructures probed old-vs-new, 13 AST shapes for the exemption map, 31 rendered commands —
  0 weakened controls. P1: capture.py:388 test_ref pins 1/3 invariant clauses (control measured real). P2:
  three orphan_sweep observation_boundary "never raises" invariants false (caught by outer R4 → fail-closed).
  P3: 18/78 map entries exempt zero sites (standing grants the liveness check accepts); `$USER` unquoted by
  design; receipt expires_at wrong-type reports control_manifest_schema.
- Lens B (prose/pins, fable): ALL-STOP YES by the brief's letter — 8 non_raising/observation boundaries whose
  "never raises" is false on constructible input (orphan_sweep ×4, bedrock RecursionError under the 64 KiB cap,
  evidence.py `_decoded_log_message` lone-surrogate/RecursionError REACHABLE from an operator evidence file,
  textract unconstructible-shape, scenario_inventory set() before type test → acceptance_internal). P1: paged
  token/destination TypeError holes; check_error_with_cause `str(exc)` outside suppress; FIVE B26 rationales with
  false mechanism ("strict=True removal would make the target rootable" — measured identical; "botocore returns
  datetime subclasses" — botocore returns exact datetime, naive when tz-less). P2: receipt invariants omit the
  control_manifest_schema code; two s3 R6 pin claims name tests that never reach the handler; bedrock R5
  sentence inverted; `_orphan_call` rationale omits the `else:` arm that retires the finding; B27 hint overstates
  R_TB_NONLITERAL (voids ONE decorator, reported, not per-file); 5/6 state.py from_dict map entries inert.
  Confirmed by probe: helper-return keeps trail, try-body loss, `_decode_or_none` R6, zip/enumerate unrootable.
- HUB ADJUDICATION: no fail-open, no deleted gate, no composer breach; every raise is caught into a named
  fail-closed error (only evidence.py #6 is an operator-visible crash). Downgraded from ALL-STOP to a P1 BATCH
  owned by the hub BEFORE the re-stage; John informed. Fix lane tier-w4fix (fable, worktree, brief
  LANE_BRIEF_W4FIX.md) dispatched from e0caff32d with 18 work items (code A.1–9, prose B.10–15, tickets C.16–18).
- Sequencing after w4fix merges: merge fix/codex-salvage (pre-re-stage merge, own delta vs 2210, 1 sidecar key
  on audit_export_snapshots.py to re-derive), ONE full suite on the final tip, close Wave-4 issues at that tip,
  close the 5 codex PRs, PAUSE for the operator re-stage.

## PRE-RE-STAGE MERGE: fix/codex-salvage (John authorised 2026-08-29; MERGED e42bfe0c0)
- Six commits (base fe4d87e6b, tip f36f9b81a): PRs #117 #123 #127 #128 #129 re-ported/cherry-picked. Zero merge
  conflicts; 132 touched-file tests pass; masquerade baseline up to date; full suite at e0caff32d was green
  (43709 passed) and this merge touches cli.py, contracts/errors.py, landscape/factory.py,
  landscape/execution/audit_export_snapshots.py + tests + a runbook line — the final full suite runs on the tip
  after tier-w4fix merges. Corpus delta vs Wave-4 close: 2210 → 2210 (ZERO). One sidecar key re-derived from
  the post-merge tree (audit_export_snapshots.py R6 register_verified_candidate: body[31] → body[32], new
  module-level import). Five GitHub PRs closed as landed (johnm-dta). Not a wave; reported separately by John's
  instruction.
- tier-w4fix MERGED (2319dec1d, lane HEAD 1601f2d79): all 18 audit work items disposed. Code: 8 boundaries made
  genuinely non-raising (type-test before hash/compare; RecursionError joins malformed-input tuples; surrogatepass
  size bound; str(exc) inside the suppress; _receipt_timestamp raises receipt_store_schema); the _orphan_call
  else:-arm move retired 1 finding honestly (corpus 2210 → 2209, hub-verified, sole diff line). Prose: 5 false
  strict=True/botocore-subclass mechanisms rewritten to measured ground (botocore returns exact naive-capable
  datetime; strict removal changes nothing in the walk); s3 R6 pin claims replaced with a test that actually
  reaches the handler; hints R_TB_NONLITERAL corrected (per-decorator, diagnostic emitted). Pins: capture 15-case
  operator-env test, evidence reverify-digest case; all changed test_refs re-fingerprinted; trust_boundary gates
  rc 0, 1135 harness tests green post-merge. Tickets: elspeth-f124a558a0 (map effect check), elspeth-09450c9ba9
  (map root check), elspeth-4620c87aa0 (dead naive clause). obs elspeth-obs-539724f2db left open (manifest_schema
  captured_at, same class, different file). Final full suite running at 2319dec1d.

## WAVE 4 CLOSED (2026-08-29, hub; suite tip 2319dec1d)
- Final full suite at 2319dec1d: 43753 passed / 66 skipped / 1 xfailed / 0 failed (21m06s, -n 16, detached
  worktree). All five Wave-4 issues CLOSED anchored feature/unified-lineage@2319dec1d (B26 elspeth-1213f153ae,
  B27 elspeth-23dd89ac63, B28 elspeth-be73a74f17, lintmap elspeth-0bd4fb6042, stagecmd elspeth-23ee8e3440 with
  fix_verification). All Wave-4 + fix + audit + suite worktrees reclaimed; remaining worktrees are other
  sessions'/waves' (barrier-nav-width unmerged work, tier-lintprecision another session, lensA-wording +
  wave1-audit historical).
- FINAL NUMBERS: Wave 4 removed 384 (2594 → 2210) + w4fix 1 (→ 2209); codex-salvage delta 0. W1–W4 + fixes:
  3,898 → 2,209 = 1,689 removed. John's target was ~2k: "I'll call it a win."
- STILL OWED AT THE RE-STAGE (operator seam, NOT started): ONE stage_scan after the last fix merge; the
  f0e38838d check-judge-quality re-run with a real LLM before any sign-bundle; the 40 Wave-3 stale signed
  entries → stale_delete per the Wave-3 section; lens-A W3 F3 possibly-signed prose is operator-routed. New
  sign_bundle_plan (stagecmd) now prices the run per lane. Open follow-ups for assignment: elspeth-f124a558a0
  (exemption-map effect check — 18/78 entries exempt zero sites), elspeth-09450c9ba9 (map root check),
  elspeth-4620c87aa0 (dead naive clause), elspeth-3b6708ef3d, elspeth-b5d005a913, elspeth-d152024c84,
  elspeth-69149540e0, elspeth-2cab8e43b1; obs elspeth-obs-539724f2db open (manifest_schema captured_at code).

## PRE-RE-STAGE MERGE: fix/barrier-nav-width (John authorised 2026-08-29; MERGED 570194aae)
- Six commits (base 845afa326, tip 2e0b10a8b) closing elspeth-b6a0a85a15 (P0 gate-in-collector-scope: one
  closer registry node_id -> (CloserKind, name) in DAGNavigator, terminal-arm dispatch coalesce/collector with
  row_union as a named fail-closed invariant), elspeth-258bd49d81 (settings.max_expand_group_width default
  100k: traversal multi-row arm refuses ahead of the mint through transform.on_error with explicit
  expand_width_exceeded via the single _branch_loss_reason derivation; TokenManager.expand_token same ceiling
  as fail-closed backstop before any DB work), and elspeth-9db785ace7's close-out nit (unbound-fork advice no
  longer recommends a row_union closer). All three tickets were already CLOSED with fix_verification naming
  these commits (adversarial lane confirmations 8102/8103, nothing refuted).
- Hub verification on the MERGED tree: one conflict (recent-code-hints.md, both sides' dated entries kept
  additively, newest-first); full source diff personally reviewed (fence threaded settings -> factory ->
  processor -> TokenManager; parity-yaml a5086db8ea98e150 not_authorable claim verified against the
  yaml_importer decline in the same diff); corpus 2,209 findings, identity diff vs w4fix baseline EMPTY
  (postnav.findings.ids = w4fix_after_hub.txt.ids); 1,544 targeted tests green on the merged tree incl.
  test_dag_navigator (the resolve_jump_target_sink overlap with main's 305673f52 nominal-dispatch rewrite),
  both new integration suites, runtime-rejection parity gate, masquerade gate, named-boundary-map pin,
  dag_scenario_corpus contract.
- Staged-bundle impact: ZERO key drift. Only keyed files touched are core/config.py (B03 module-import key,
  keyed by module name) and web/composer/yaml_importer.py (B48 body[35]/body[38] keys — the branch adds a dict
  ENTRY, not a top-level statement; both indices re-verified to resolve to the named functions post-merge).
  Cosmetic prose drift only: B06 cites contracts/errors.py:961 (now 965), B07 cites engine/processor.py:5298
  (now 5302) — correct symbols named in both, no mechanism falsehood, left as-is.
- Full suite at 570194aae GREEN: 43,773 passed / 66 skipped / 1 xfailed / 0 failed (18m19s, -n 24, detached
  worktree, dual-root PYTHONPATH). Suite + lane worktrees reclaimed; fix/barrier-nav-width deleted (-d,
  fully merged). The operator
  re-stage baseline is now AFTER this merge: the ONE stage_scan covers W1–W4 + w4fix + codex-salvage +
  barrier-nav-width.
- LINT-PRECISION MERGE 216b3152a (elspeth-8d46db34ff, tier/lintprecision, --no-ff onto 9918fedb0): seven
  tier_model false-positive classes fixed in the RULE, not the tree — try-join and if-join skip branches that
  cannot fall through; post-init R5 exemption follows `for x in self.<f>` and module-private `_f(self.<f>)`
  returns; R4 shares R6's explicit-outcome predicate; constructed error entries recorded into a validator
  accumulator are explicit; for/with/comprehension targets derive by the assignment rule. Closures still do
  NOT inherit derived names (deliberate, df3463583). Measured vs clean export of fc05a280c, allowlist-disabled:
  2207 → 2088, 119 removed, 0 added; three adversarial audit lanes confirmed 119/119. Full suite on the
  rebased tree 43,807 / 0 failed. RE-STAGE: 6 signed entries now report Stale (finding gone) → stale_delete
  (contracts/events.py _render_public_phase_error_message; contracts/value_source.py
  CatalogValueSource.__post_init__; core/operations.py _render_exception; engine/executors/state_guard.py ×2;
  web/execution/runtime_preflight.py _capture). Per-file caps: chat_solver 31→13 against max_hits 11 (still
  over — operator decision); tool_batch 5/3 and telemetry/serialization 8/4 unchanged. pipeline_planner
  `_provider_fields` invariant prose corrected (its "launders the suppression root" explanation was the
  try-join drop). Rolling hint: docs/agents/recent-code-hints.md 2026-08-29 entry.

## 2026-08-30 — PER-FILE BLANKET MIGRATION, WAVE 1 (plan docs/plans/2026-08-30-per-file-blanket-migration.md, epic elspeth-e561df3c4e)

- Why: the four whole-of-repo waves never saw 974 findings because worklists were built from REPORTED
  findings and a `per_file_rules` blanket suppresses reporting. John's rulings (08-30): every exception is
  REGULARLY re-approved by the judge (90-day expiry + resign lane is the design); a permanent suppression of
  any shape is the defect; closed-set R5 dispatch goes to the judge individually; `_R5_NAMED_BOUNDARY_CONTEXTS`
  is the same defect class, left open as a possible Wave 4.
- Task 1 @e52e2c4cc: 3 zero-coverage blankets deleted (identity.py R5, database_sink.py R5, _common.py R1);
  max_per_file_rules 99 → 82; corpus 2051/2051 same regex.
- Wave 1: 11 lanes (C01–C06, E01–E03, L01, L02) in .claude/worktrees/tier-<b>, each --no-ff merged onto
  tier/blanket-burndown (e41a8f755..072bc23b4), every source diff reviewed by the hub. Dispositions of the
  602 blanket-standing findings: 43 F, 54 D, 505 J (+4 re-approvals of signed entries rotated by lane
  edits: gate.py R4, explain_screen.py R1×2, plugin_context.py R6). Notable F: scheduler_drain converges on
  its own `_is_result_tuple` TypeIs; token_traversal drops an unreachable tripwire; config.py env reads →
  membership form, `_fingerprint_config_for_audit` → exact `type()` on model_dump data; canonical.py NaT
  tautology; database.py test-double-shaped guard removed; runtime_val_manifest getattr_static → vars()
  (masquerade baseline entry deleted, count DOWN one). D: 9 new @trust_boundary on real YAML parse entry
  points (config.py ×2, schema.py ×7, runtime.py ×2), token_usage widened, 3 @observation_boundary.
- Blankets deleted @39168d8b4: every block in core/contracts/engine/tui/testing (45) → 37 remain.
  blanket_census.py: raw 2094 → 1997 (−97 = 43F + 54D, exact), standing 974 → 372 (−602, exact),
  unused 0. sidecar_join.py (ast-path join, fp re-derived on the merged tree): 506/506 bound, 0 unbound.
  Real-allowlist uncovered net of R_TB_SUPPRESSED: 797 → 1306 (= 505 + 4 rotations + 797). Ceilings:
  max_allow_hits 652 → 1611 (601 − 188 stale_delete + 693 carried + 505), max_total_entries 745 → 1648,
  max_per_file_rules 82 → 37, max_permanent_per_file_rules 99 → 37.
- Gates on the merged tree: check-per-file-blanket-ratchet --baseline-ref 791ecca15 rc=0;
  trust_boundary.tests/scope/tier rc=0 (all 54 D fingerprints verified here); full suite: see next entry.
- Measurement traps found this wave (memory + broadcast): the findings regex ALSO matches R_TB_SUPPRESSED
  observation lines (1,254), so a D lane's count would not drop — exclude them, and the census is the only
  wave-arithmetic authority; the session scratchpad root is shared by all lanes (generic filenames were
  overwritten cross-lane); the persistent shell CWD is shared by every agent in a session, so a sibling's
  `cd` moves a bare relative command into another worktree (E02 caught it via a foreign commit subject; no
  damage, verified by blob SHA). The (file, rule, symbol) triple is non-unique in EVERY bucket — retired.
- Follow-ups filed under the epic: elspeth-c4916d0115 (transform.py:754 runtime_checkable Protocol
  dispatch), elspeth-a90f42ff7a (check-contracts union blind spot), elspeth-1aa8d8910d (begin_run guard
  adversarial test); standalone P1 elspeth-4f3cd4155b (blobs.py `_state_options_reference_blob` fail-open,
  found by the rationale lane for sign-2026-08-30 — its two justify actions carry DEFECT text so the judge
  blocks them).
- Operator seam: sign-2026-08-30 (c601c957b) was never fired and is stale (another session moved
  feature/unified-lineage to 7d0fdf534, docs-only). Decision: ONE combined re-stage after this merge —
  its 1,063 actions (141 previously un-annotated rationales now authored) + Wave 1's ~505 new_judgment +
  4 drift_repair. One fire instead of two.
