# Composer remediation planning: R02, R03, R14–R16, R21–R26, R53

> **Historical diagnostic input, retained 2026-09-30.** Read the
> [parent plan's current execution context](../2026-09-23-web-review-remediation.md#current-execution-context)
> before using this sheet. Its source sites, missing-work statements, epoch-66
> proposal and service ownership describe the 2026-09-23 checkpoint, not the
> current release. At recovery base `a2f0281ff`, Sessions/hard-cut are epoch 71
> and the advisor parses structured output through `advisor_output.py` and
> `advisor_checkpoint.py`; R23's prose/markdown parser design is superseded.
> Preserve its boundary rejection and note-projection requirements through the
> current structured contract. Re-derive remaining acceptance tests and owners;
> do not recreate an old parser or infer closure from these dated notes.

Read-only investigation on 2026-09-23. Source checkpoint: `1e9cafa9d47df5654926509ef13a2a3e9e4452c8`; review checkpoint: `74c0ce0db`. No production changes, tests, commits, service actions or database reads performed. Paths below are repository-relative. Tests named below are proposed, not passed. These are 12 assigned issue IDs, not a new finding inventory.

## Drift evidence and disposition at the 2026-09-23 checkpoint

Commands and relevant raw output:

```
git rev-parse HEAD
1e9cafa9d47df5654926509ef13a2a3e9e4452c8

git show --stat --oneline 1809379f6
1809379f6 fix(composer): stop the advisor blocker claiming the published reply was withheld
 CONTRIBUTING.md
 docs/agents/recent-code-hints.md
 src/elspeth/web/composer/no_tool_policy.py
 src/elspeth/web/composer/service.py
 tests/unit/web/composer/test_advisor_checkpoint.py
 tests/unit/web/composer/test_advisor_terminal_publication.py
 tests/unit/web/composer/test_no_tool_policy_segments.py
 7 files changed, 156 insertions(+), 392 deletions(-)

rg -n 'SESSION_SCHEMA_EPOCH =|_COORDINATION_HARD_CUT_EPOCH =' src/elspeth/web/sessions
src/elspeth/web/sessions/models.py:358:SESSION_SCHEMA_EPOCH = 65
src/elspeth/web/sessions/schema.py:40:_COORDINATION_HARD_CUT_EPOCH = 65
```

`git diff 74c0ce0db..HEAD -- src/elspeth/web/composer/service.py src/elspeth/web/composer/no_tool_policy.py` shows the R16 withheld-copy fix: `_advisor_signoff_blocked_wording` now defaults to `_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE` and the absent helper uses `_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE`. **R16 is partly fixed, not closed:** current `service.py:11354–11367` still feeds the generic notice into `flagged_unrepairable`; the default notice still requires a pipeline change, contrary to message-rejected retry semantics. Current CONTRIBUTING lines 300–308 now expressly require published notices on END blocks, resolving the old M-6 approval caveat for that already-landed part. Do not reintroduce withheld branches or ask for an already-resolved operator ruling.

`git diff --name-only 74c0ce0db..HEAD -- src/elspeth/web/sessions/service.py src/elspeth/web/sessions/routes/_helpers.py src/elspeth/web/composer/pipeline_planner.py src/elspeth/web/composer/withheld_replies.py src/elspeth/web/execution/completion_gates.py` printed no paths. The inspected defect sites in these files remain. Source inspection also found R15's next-message notice, R21's unconditional did-not-clear disclosure, R22's pre-cohort write, R23's raw regexes and R24's optional arm still present. The only relevant compose-route drift is `8719d8f37`, which preserves control history during recompose; keep it when editing.

## Ownership and sequencing

Avoid parallel writers in the 11k-line `composer/service.py`. Use these ownership batches, with independently running test-design/review agents allowed throughout:

1. **EPOCH owner:** R02 plus R53. Own sessions `models.py`, `schema.py`, completion-gates serializer docstring, epoch pin tests, current release docs. Land early before any deployment. Coordinate any other epoch-affecting fix through this owner; choose final epoch once, and repeat the sweep on the final integrated tree.
2. **STATE owner:** R03 and R14. Own `sessions/service.py`, route helpers, message/compose routes, the `composer/service.py` mid-turn payload section only. Obtain a temporary exclusive file-edit window before this owner edits `composer/service.py`. Coordinate R05 lock ordering with the persistence lane before finalizing any transaction change. Coordinate frontend pipeline-change semantics with frontend owner; do not have both edit sessionStore concurrently.
3. **ADVISOR owner:** R15, residual R16, R21, R23, R24. Own `composer/service.py` advisor/parser sections, `no_tool_policy.py`, corresponding advisor tests. Start after STATE's service.py commit is integrated. R23 must check for the separate untracked structured-output proposal `docs/plans/2026-09-23-advisor-structured-output-prompt.md`; if implementation is already underway, agree one parser owner and do not build a soon-obsolete regex path independently.
4. **AUDIT owner:** R22, R25, R26. Own `composer/audit.py`, `turn_audit.py`, `withheld_replies.py`, `pipeline_planner.py`, and service.py audit sections. Start after ADVISOR's service.py edits or share a single sequential worktree. R25 and R26 can be prepared independently in their own files, but final API plumbing must land with R22.

Each batch gets one independent reviewer who did not implement it. STATE and AUDIT additionally get a persistence/failure-injection reviewer. All reviewers read final full touched files, the original issue source verdicts, and test logs; they must attempt counterexamples, not just confirm intended branches. Review R03 and R14 together because a new metadata-only save mechanism can otherwise fix proposals while erasing gate facts.

## R02 — exact-epoch admission for completion-gates v2

**Current:** epoch 65, hard cut 65, serializer emits schema version 2 (`execution/completion_gates.py:198–208`), parser rejects missing version/cause (`:273–276`, `:309–316`). Mechanism remains; published exposure is disputed and does not need resolving to repair startup admission for locally created stores.

**Fix:** session epoch 66 and hard cut 66, with comments explaining the v2 envelope/cause incompatibility. Maintain strict parsing; no dual-read, silent normalization, or automatic store reset. Leave historical epoch-65 note introduction prose as historical chronology, add epoch-66 v2/cause explanation, and move only *current* deploy/cutoff/expected-value prose. Landscape remains 43 unless a separate change independently requires otherwise. This is a code/docs decision, not permission to recreate a live DB or deploy.

**Exact sweep:** `src/elspeth/web/sessions/{models,schema}.py`; README.md; CHANGELOG.md current 0.8.1 section; `docs/guides/sharing-pipelines.md`; `docs/runbooks/{staging-session-db-recreation,azure-container-apps-cold-install,azure-container-apps-deployment,aws-ecs-deployment}.md`; `website/get-started.html`. Check structured candidate examples as well as prose: AWS runbook has `session_epoch: 65` JSON and `session_epoch_35_to_65...` structural-change strings, Azure deployment has candidate JSON, staging has `PRAGMA user_version` expected value. Historical review files must retain pinned 65 references.

Literal test sites measured: `tests/unit/contracts/test_web_blob_fencing.py:3202`; `tests/integration/web/composer/guided/test_schema9_epoch.py:60`; `tests/unit/web/sessions/{test_interpretation_events_table.py:245,test_proposal_blob_effect_receipts_schema.py:24,test_blob_inline_resolutions_schema.py:68,test_schema.py:364}`. Dynamic docs tests: `tests/unit/docs/{test_release_version_surfaces,test_readme_release_surface,test_staging_session_recreation_policy}.py`, `tests/unit/website/test_release_site_contract.py`. The previous bump's exact changed paths are recoverable with `git show --format= --name-only 539121b17`; use that as a positive control, not as a complete current inventory.

**Regression:** temporary SQLite DB stamped 65 with old empty and blocked-no-cause envelopes must fail *startup* with expected schema incompatibility before any head deserialize; epoch 66 initialized DB accepts round-tripped empty v2 and all cause variants; wrong schema at current epoch still fails. Add serial PostgreSQL schema-admission coverage in `tests/testcontainer/web/test_schema_probe_postgres.py` or the existing identity-owner schema fixture as appropriate. No live DB reset. Test current release documentation agrees with constants, while 0.8.0 historical epoch remains 53.

**Focused command:** `PYTHONPATH="$tree/src:$tree/elspeth-lints/src" "$venv/bin/python" -m pytest -n 0 tests/unit/web/sessions/test_schema.py tests/unit/web/sessions/test_blob_inline_resolutions_schema.py tests/unit/web/sessions/test_interpretation_events_table.py tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py tests/unit/contracts/test_web_blob_fencing.py tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_readme_release_surface.py tests/unit/docs/test_staging_session_recreation_policy.py tests/unit/website/test_release_site_contract.py tests/integration/web/composer/guided/test_schema9_epoch.py`. A final full schema/persistence gate and serial testcontainer selection remain required.

## R03 — metadata-only head changes must preserve proposal validity

**Source:** `sessions/routes/messages.py`, `sessions/routes/composer/compose.py`, `sessions/service.py`, `composer/tool_batch.py`, `composer/pipeline_commit.py`, `sessions/routes/composer/proposals.py`; frontend `src/elspeth/web/frontend/src/stores/sessionStore.ts` and `components/chat/actionableProposals.ts`. Current `_insert_composition_state` still allocates a new UUID/version and unconditionally supersedes approvals (`sessions/service.py:6562–6597`).

**Preferred design:** preserve immutable state history but represent a review-only successor explicitly and, in the same fenced session transaction, transfer eligible pending proposal bases and preserve/rebind still-valid workflow approvals *only after checking the entire proposal/approval binding contract*, not just a convenient graph hash. Both freeform proposal kinds, set_pipeline PresentBase, and blob-effect/custody receipts must remain valid together. Do not skip durable advisor decisions just because a proposal exists. Do not blindly mutate every pending base. Implementation's first checkpoint must map those binding fields and decide whether atomic rebind or a separate gate-fact persistence design is smaller and correct; the suggested atomic rebind is not permission to weaken authorization. A real graph/content/authority change must still invalidate proposals and approvals.

**Regression:** explicit_approve turn stages a proposal at S0, unchanged END block saves S1, accept succeeds and applies exactly proposed content. Repeat transient block→clean and clean→block; varying retryable advisor detail must not obsolete same-content proposals. Negative controls: changed graph; stale base from an earlier actual edit; another session's proposal; revoked identity; blob receipt mismatch; genuine approval scope change all retain rejection/supersession. Check `GET state`, reload, auto-commit, send and recompose. UI says no pipeline update for gate-only version changes, but says updated for real content changes.

**Tests:** extend `tests/unit/web/sessions/test_advisor_recovery_routes.py`, `tests/unit/web/sessions/routes/composer/test_proposal_stale_base.py`, `tests/integration/web/composer/test_pipeline_proposal_lifecycle.py`, `tests/unit/web/sessions/test_composer_proposal_authority.py`, `tests/unit/web/sessions/test_blob_proposal_accept.py`; frontend `sessionStore.test.ts` and existing actionable-proposal tests. Add a serial PostgreSQL concurrent-head/revoke test in `tests/testcontainer/web/test_session_derived_mutations_postgres.py`. Focused pytest names above with `-n 0`; frontend runner and exact script should come from package.json at implementation time. Required shared session-authority AST gate: `tests/unit/architecture/test_session_db_mutation_authority.py`.

## R14 — durable gate facts survive mid-turn writes and aborts

**Source:** `composer/service.py:2728–2741` omits gate facts from authoring-only StatePayload; `_helpers.py:995–998` reads the moving head. Merely changing recovery to use the turn-start record does not cover generic 502 paths that leave a mid-turn row as the head.

**Fix:** carry parsed, typed turn-start gate facts through every authoring-only mid-turn payload and fenced persistence path. Preserve original `for_graph`; the read-side mismatch must yield pending-review wording, never claim the new graph was reviewed. Only an explicit final advisor decision may replace/clear these facts. Do not mask corrupt prior envelopes. Include persistence after each tool batch, failed unwind, convergence abort, preflight failure and generic provider exception; leave no ungated mid-turn head.

**Tests:** extend `tests/unit/web/sessions/test_advisor_recovery_routes.py`, `tests/unit/web/composer/test_audit_cohort_settlement.py`, `tests/unit/web/sessions/test_turn_audit_cohort.py`. Add blocked S0→mutating R1→each abort family→read head; assert exact carried fact, changed fingerprint pending status, and completion_ready false in validation, audit readiness and shareable-review consumers. Negative control: final clean advisor clears exactly its reviewed graph; absent gate on genuinely new graph does not fabricate rejection. Run these files `-n 0`, session mutation AST gate, then final integrated full and PostgreSQL gates.

## R15 + R16 residual + R21 — one cause-consistent wording matrix

**Fix ownership:** update `composer/no_tool_policy.py`, `composer/service.py`, and if needed fixed control-envelope vocabulary in `composer/control_messages.py`. R15 graph-rejected ABSENT wording must say next pipeline change and retain readiness-not-reverified clause; outdated comment at no_tool_policy.py:157–167 must be rewritten. Retryable unavailable/malformed causes keep next message. Residual R16 MESSAGE_REJECTED detail and suggestion both require reword/resend, with no unsupported no-pipeline-change claim on RED or ABSENT. Use published notices on every END block and keep true case-5 withholding intact. R21 selects fixed backend control disclosure by obtained verdict versus unavailable/malformed; neither may imply an outage rejected graph content. Do not embed raw advisor findings into the fixed provider-visible control row.

**Regression matrix:** GRAPH_REJECTED, MESSAGE_REJECTED, UNAVAILABLE, MALFORMED × GREEN, RED, ABSENT, pending interpretation handoff; check chat text, trusted-segment ownership, readiness blocker detail/suggestion, persisted gate, reload projection and next-turn provider-control replay. Subsequent unchanged graph rejection skips provider as existing design requires; transient/message rejection re-reviews. Published reply remains visible with no claim it was withheld. Case 5 still discloses genuine withholding. R21 outage two-pass case followed by recompose must replay failure-to-obtain wording (preserve 8719d8f37's full-history fix).

**Tests/commands:** `pytest -n 0 tests/unit/web/composer/test_advisor_checkpoint.py tests/unit/web/composer/test_advisor_terminal_publication.py tests/unit/web/composer/test_no_tool_policy_segments.py tests/unit/web/sessions/test_advisor_recovery_routes.py`. Wire-template AST inventory/roundtrip coverage lives in `test_no_tool_policy_segments.py` and is mandatory whenever notices change. R16 main fix should be recorded as inherited `1809379f6`, with residual fix evidence separate.

## R22 — atomic withheld-prose audit settlement

**Source:** service.py `_persist_turn_audit` at 5483–5491 writes `_persist_withheld_reply` before cohort settlement; sibling writes at 3435,5991,6315,7274. The planner settlement already creates staged messages inside `add_messages_atomic` at 4867 onward.

**Fix:** stage withheld replies in the owned recorder/cohort DTO, then write them within the same fenced transaction as their mandatory turn audit/call/attempt rows. Extend `composer/audit.py`, `composer/turn_audit.py`, `composer/withheld_replies.py`, and service audit plumbing. Review every cited early-write path; moving only the first write later does not meet atomicity. Maintain exception semantics: a failure to persist mandatory audit becomes the existing typed audit failure with its original exception linked and failed-turn accounting retained; no orphan audit row or replayed prose. Do not add broad defensive catches that hide programming errors.

**Tests:** `tests/unit/web/composer/test_audit_cohort_settlement.py`, `tests/unit/web/sessions/test_turn_audit_cohort.py`, `tests/unit/web/composer/test_advisor_audit.py`, `tests/unit/web/composer/test_advisor_terminal_publication.py`. Inject failure before row write, after withheld staging, at cohort message insert, at final transaction commit, and during plugin unwind. Assert all-or-none persisted cohort, no orphan withheld row, appropriate failed turn, preserved cause/typed failure and exactly-once success path. Add PostgreSQL cohort rollback integration proof, proposed new `tests/testcontainer/web/test_composer_audit_cohort_postgres.py`. Run focused files `-n 0` and complete testcontainer gate at integration checkpoint.

## R23 — markdown-tolerant machine lines

**Source:** service.py `_ADVISOR_CATEGORY_LINE_RE`/`_ADVISOR_STEPS_LINE_RE` at 11028–11029, verdict parser and reviewer-note projection. Normalize recognized machine-label emphasis consistently with verdict parsing, prefer terminal machine metadata, and strip exactly recognized metadata from the reviewer note. Preserve source narrative formatting; avoid removing every incidental prose `Steps:` line. The terminal metadata grammar should be explicit, not a loose search that can silently overwrite an earlier correct category.

**Tests:** extend `tests/unit/web/composer/test_advisor_checkpoint.py` and `test_advisor_clean_verdict_table.py`: plain, bold-label (`**CATEGORY:**`), bold-whole-line, duplicate terminal records, prose `Steps:`, missing/unknown category, unknown step IDs, mixed case, no terminal metadata; assert category, known step IDs, and note text together. Add a negative control proving prose survives and unrecognized IDs are filtered. Check pending structured-output work before implementation; if it replaces parsing, the same behavioral cases become boundary tests for that implementation instead of a second parser.

## R24 — remove test-only optional production branch

**Source:** service.py `_advisor_blocked_result(assistant_message)` and caller at 6915. Restore nonoptional admitted assistant-message type, remove conditional None fallback and inaccurate docstring, and update tests to real admitted messages. Preserve empty *content* behavior if a real admitted message permits it; None object and empty content are different contracts. Also update related recent-code-hints only if it remains current guidance, keeping historical evidence intact.

**Tests:** `tests/unit/web/composer/test_advisor_checkpoint.py`, `test_advisor_terminal_publication.py`, normal mypy gate. No new mock attribute shims or suppressions. Independent reviewer confirms every production call through code-map callers before simplifying, since current planning only inspected the cited single call.

## R25 — PostgreSQL-safe unadmitted prose

**Source:** `composer/pipeline_planner.py:1618–1633` checks type/bounded text but not NUL; staged content becomes a PostgreSQL text message.

**Fix:** reject NUL for the optional raw-prose capture at the external-response boundary and retain existing mandatory hashes/call/attempt metadata. The narrow reviewed policy is returning empty capture, matching oversized-capture behavior; do not silently remove a NUL and hash rewritten text as if original. An owned WithheldReply should assert its producer supplied admissible content, but merely adding an assertion there would turn hostile provider text into a crash. If preserving the raw payload is required, choose explicit safe encoding with envelope metadata rather than an implicit replacement. Check adjacent producer boundary paths without expanding into a new unrelated feature.

**Tests:** `tests/unit/web/composer/test_pipeline_planner.py`, `test_audit_cohort_settlement.py`; NUL, ordinary Unicode, oversize, nonstring, blank. Planner can nudge and retain call/attempt audit after a NUL-containing PROSE_REPLY; valid subsequent text retains normal capture. Add real PostgreSQL proof to proposed `tests/testcontainer/web/test_composer_audit_cohort_postgres.py`, and run the actual configured driver. The source review explicitly did not execute psycopg2 behavior; do not report raw ValueError behavior as reproduced until measured.

## R26 — bind withheld planner prose to its producing call

**Source:** `pipeline_planner.py:4133`, `composer/audit.py:264–281`, `withheld_replies.py`, service.py `_persist_planner_audit` message builder.

**Fix:** capture the producing planner call ordinal when recording the withheld reply; thread it through the owned dataclass, recorder and persisted envelope, require it for planner origin and refuse inconsistent provenance. Prefer an explicit variant or validated optional field whose requiredness follows origin; do not invent a fallback ordinal from message position. Consider immutable call identifier as additional linkage if ordinal is scoped to a planning request; repeated planner invocations in one session must remain unambiguous. Pin the scope in the envelope. Review whether envelope grammar changes require a schema version change/epoch coordination with R02; do not automatically bump for an additive audit-only field without inspecting actual strict consumers.

**Regression:** multiple calls with skipped blank/oversized/NUL captures, retries, second planner invocation in same session, and successful terminal call; each stored prose maps to its actual call regardless of row append order. Invalid ordinals and invalid origin/provenance pair fail at the owned boundary. Existing audit-kind exclusion still prevents chat rendering/provider replay. Tests: `tests/unit/web/composer/test_pipeline_planner.py`, `tests/unit/web/composer/test_audit_cohort_settlement.py`, `tests/unit/contracts/test_composer_planner_audit.py`; proposed focused `tests/unit/web/composer/test_withheld_replies.py` for cross-field contract only, plus AUDIT PostgreSQL cohort proof.

## R53 — serializer docstring

Update `execution/completion_gates.py:185–196`: serializer writes both explicit resolved turn-end facts and verbatim preserved recovery facts; fact resolution/adjudication is separate. Keep correct fingerprint/pending explanation. No new unit test for a docstring; reviewer compares `resolve_completion_gate_facts` and `_helpers.py` turn-end caller. Bundle with R02 to avoid a second editor of completion_gates.py.

## Validation and handoff constraints

Every future shell command starts `cd "$tree" &&`; use a real local environment or explicit approved interpreter with BOTH source roots on PYTHONPATH and verify module provenance. The `$tree`/`$venv` placeholders above must be concrete before execution. Always redirect test output to unique lane log, capture exit status after process termination, and report that status. Worker ceiling is `-n 0` for all focused jobs and PostgreSQL; coordinate a single shared-host full-suite owner before broad validation.

Use CONTRIBUTING whole-tree gates appropriate to touched source. Changes to session write paths require `tests/unit/architecture/test_session_db_mutation_authority.py`; no_tool_policy changes require segment/AST roundtrips. Final merged production tree changes include shared session persistence and schemas, so the coordinator must run one frozen whole-tree gate including default pytest and the complete serial `pytest tests/ -m testcontainer -n 0`. Scoped passing tests are not final release proof. Compare trust-tier finding corpus to base without global signing or suppressions. Each issue closes only with a head SHA, production-seam regression, independent review disposition, and final integration evidence; R16 must explicitly distinguish inherited versus newly-fixed pieces.
