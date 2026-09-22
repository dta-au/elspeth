# release/0.8.1 web tier review: 48-hour window ending 2026-09-23

## Scope

- **Pinned:** branch `review/web-0.8.1-20260923` at `74c0ce0db` (worktree `.claude/worktrees/review-web-0.8.1-20260923`). The window is `7c986dc97..74c0ce0db` on `release/0.8.1`: the 48 hours ending 2026-09-23 04:53 +1000, 133 commits (`git rev-list --count`). Every file:line below is at `74c0ce0db`. Other agents kept committing to `release/0.8.1` during the review, so line numbers may have moved on the live branch.
- **Covered:**
  - Web backend: 27 bundles, 69 files. This is production code under `src/elspeth/web`, excluding the frontend.
  - Web frontend: 12 bundles, 126 files under `src/elspeth/web/frontend/src`. Frontend e2e specs were excluded.
  - 9 seams, which pull in non-web contracts and deploy config where they meet the web tier.
- **Not reviewed:** tests, docs and the other ~70 non-web code and config files in the window, except where a seam pulled them in.
- **Method:**
  - A diff-focused review of each bundle.
  - Adversarial verification. Every finding went to one refuter who traced the code. Findings filed critical or high also went to a second refuter, who checked impact and intent independently. Some findings filed at medium carry both lenses as well.
  - Status: *confirmed* means no refuter refuted it, *disputed* means the refuters split, *unverified* means the verifier failed.
- **Completeness:** No review agent was left without a result after retry, and no bundle file went unexamined.
- **Verification counts (per source finding):** 109 confirmed, 1 disputed, 19 refuted. No finding is unverified.
- **Per-agent reports:** full detail, including hunks judged fine, is in `.claude/lanes/web-review-20260923/*.md`. The source-to-root-cause map used for every count in this document is `.claude/lanes/web-review-20260923/synthesis-map.tsv`.

Severity is the verifiers' *adjusted* severity. Where the reviewer's original severity differed, the entry says so ("raised as high"). "Pre-existing" marks a finding on lines the window did not touch (`diff_anchored=false`). Ten of the eleven were filed at medium, as the brief requires for undiffed lines. The exception is seam-09-deploy-config#3, which was filed low. It is kept only because it merges into R72 with a diff-anchored sibling, be-02#2.

## 1. Summary

### Counts

**Source findings** (110 not refuted = 109 confirmed + 1 disputed), by line and adjusted severity:

| Line | High | Medium | Low | Total |
|---|---|---|---|---|
| Backend (`be-*`) | 1 | 14 | 34 | 49 |
| Frontend (`fe-*`) | 0 | 2 | 27 | 29 |
| Seams (`seam-*`) | 0 | 12 | 20 | 32 |
| **Total** | **1** | **28** | **81** | **110** |

Eleven source findings are pre-existing: seam-03-pricing#2, seam-03-pricing#3, seam-05-people-auth#3, seam-07-interpretation#2, seam-09-deploy-config#3, be-16#1, be-18#2, be-21#1, be-23#2, be-26#2 and be-27#1.

**Deduplicated root causes:** 79 in total. 1 is high, 19 are medium and 59 are low. Six of the lows are in the retiring guided lane (issues G1–G6). Eighteen root causes merge two or more source findings. The largest is the missing epoch bump, which six reviewers reported independently.

### Top issues

1. **R01 (high).** Profile-only `azure_ai_search` nodes always fail Stage 1 with `guarantees: [(none)]`. On the web every such node is profile-only, so every web RAG pipeline that feeds a required-fields consumer is rejected. This includes the plugin's own taught pairing with an `llm` node. It also empties the source data-contract demand.
2. **R02 (medium, one source disputed).** 41aeaeac0 made `completion_gates` v2 (`schema_version` and `cause`) mandatory in the Tier-1 parser without bumping the session epoch past 65. An epoch-65 store written by the old writer passes startup and then returns a 500 on every send, recompose, validate and execute for any session with a compose turn.
3. **R03 and R04 (medium).** 41aeaeac0's "save the review outcome even when the graph is unchanged" writes a new head row with no graph change. That orphans proposals staged in the same turn (a 409 on accept). The SPA also treats the bump as bookkeeping, so it keeps showing the previous advisor verdict.
4. **R05 (medium).** 89444b793 reordered the progress, ticket and approval writers to lock the session before the identity. Provider-attempt admission still locks the identity first and then takes a foreign-key `KEY SHARE` on the session, so PostgreSQL can deadlock (`40P01`).
5. **R06 (medium, raised as high).** 07faf477e's new Approve gate makes the prompt review for a multi-query `llm` node with no node-level `prompt_template` permanently unapprovable in the UI. The pending review then blocks execution.
6. **R07, R08 and R09 (medium).** ce3602aed opened local-account administration to every local-auth admin. As a result:
   - deleting the dev-admin account lets the next open registrant of that username inherit the credential grant;
   - admin account creation and password reset write no `auth_events` row;
   - credential deletion is audited with `actor='operator'` and no admin identity.
7. **R10 (medium).** Logging out in one tab no longer logs out the other tabs. The 401 ownership check ignores 401s on requests that carried no credential.
8. **R11 (medium).** Under the new Compose + nginx bundle, uvicorn ignores `X-Forwarded-For` from the Docker bridge. Every client then shares one per-IP auth rate-limit bucket (20/min), and audit rows record the bridge IP.
9. **R12 (medium).** The operator's `execution_rate_limit` governs the run, but Landscape and the approval config hash record the engine-default `rate_limit`.
10. **R15 and R16 (medium).** Advisor copy is now false:
    - an absent-preflight graph rejection promises a re-review "on your next message", which the END gate now skips;
    - every durable blocker detail says ELSPETH withheld the composer's summary, which it now publishes.

## 2. Issues

Each issue has its own file under [`issues/`](issues/), one per root cause after merging duplicate findings. Each file gives the merged finding, then every source finding it absorbed together with the verifiers' verdicts. `findings.json` holds all 129 source findings, including refuted ones, with their verdicts and the issue each one maps to. Where a verifier corrected part of a reviewer's claim, the issue states the corrected version. Guided mode is being retired (freeform is the only mode): the G-numbered issues come from diff hunks only, and none of them is a recommendation to invest in the guided lane. The per-agent reports named under Scope sit in the gitignored lane directory, so they exist only on the review host.

| Id | Severity | Status | Area | Line | Issue |
|---|---|---|---|---|---|
| R01 | high | confirmed | — | backend, seams | [Profile-only `azure_ai_search` nodes always fail Stage 1 with false `guarantees: [(none)]`](issues/R01-profile-only-azure-ai-search-nodes-always-fail-stage-1-with.md) |
| R02 | medium | disputed | — | backend, seams | [The `completion_gates` v2 grammar (`schema_version` 2 and a required `cause`) shipped without a session epoch bump](issues/R02-the-completion-gates-v2-grammar-schema-version-2-and-a-requi.md) |
| R03 | medium | confirmed | — | backend, seams | [A review-only save moves the head and orphans proposals staged in the same turn](issues/R03-a-review-only-save-moves-the-head-and-orphans-proposals-stag.md) |
| R04 | medium | confirmed | — | seams | [A decision-only state save is invisible to the frontend readiness refresh](issues/R04-a-decision-only-state-save-is-invisible-to-the-frontend-read.md) |
| R05 | medium | confirmed | — | seams | [The session-before-identity reorder can deadlock with provider-attempt admission on PostgreSQL](issues/R05-the-session-before-identity-reorder-can-deadlock-with-provid.md) |
| R06 | medium | confirmed | — | seams | [The prompt review for a multi-query `llm` node with no node-level `prompt_template` cannot be approved](issues/R06-the-prompt-review-for-a-multi-query-llm-node-with-no-node-le.md) |
| R07 | medium | confirmed | — | backend, seams | [Deleting the dev-admin account lets the next registrant of that username inherit credential administration](issues/R07-deleting-the-dev-admin-account-lets-the-next-registrant-of-t.md) |
| R08 | medium | confirmed | — | backend, seams | [Admin account creation and password reset write no audit row, now that every local-auth admin can use them](issues/R08-admin-account-creation-and-password-reset-write-no-audit-row.md) |
| R09 | medium | confirmed | — | backend | [The audit row for web credential deletion names no actor](issues/R09-the-audit-row-for-web-credential-deletion-names-no-actor.md) |
| R10 | medium | confirmed | — | frontend | [Logging out in one tab no longer logs out the other tabs](issues/R10-logging-out-in-one-tab-no-longer-logs-out-the-other-tabs.md) |
| R11 | medium | confirmed | — | seams | [nginx `X-Forwarded-For` is ignored under Compose, so every client shares the bridge IP](issues/R11-nginx-x-forwarded-for-is-ignored-under-compose-so-every-clie.md) |
| R12 | medium | confirmed | — | backend | [The operator's `execution_rate_limit` governs the run, but Landscape records the engine-default `rate_limit`](issues/R12-the-operator-s-execution-rate-limit-governs-the-run-but-land.md) |
| R13 | medium | confirmed · pre-existing | — | backend | [A user-cancelled run whose output finalization fails is never recovered (pre-existing)](issues/R13-a-user-cancelled-run-whose-output-finalization-fails-is-neve.md) |
| R14 | medium | confirmed · pre-existing | — | backend | [A recovery persist after a mid-turn tool write erases the durable advisor block fact (pre-existing)](issues/R14-a-recovery-persist-after-a-mid-turn-tool-write-erases-the-du.md) |
| R15 | medium | confirmed | — | backend, seams | [The flagged absent-preflight notice promises a re-review "on your next message", which the END gate now skips](issues/R15-the-flagged-absent-preflight-notice-promises-a-re-review-on.md) |
| R16 | medium | confirmed | — | backend, seams | [The durable blocker detail says ELSPETH withheld the composer's summary, which END-gate blocks now publish (open item M-6)](issues/R16-the-durable-blocker-detail-says-elspeth-withheld-the-compose.md) |
| R17 | medium | confirmed | — | backend | [Deferred-blob classification misses markers in multi-query `queries.<name>.template`](issues/R17-deferred-blob-classification-misses-markers-in-multi-query-q.md) |
| R18 | medium | confirmed | — | backend | [A deferred `llm` prompt marker leaves the node with no guarantees, so downstream required fields fail Stage 1](issues/R18-a-deferred-llm-prompt-marker-leaves-the-node-with-no-guarant.md) |
| R19 | medium | confirmed | — | frontend, seams | [Unfinished-deletion recovery disappears after a reload, and the new comment claims it survives one](issues/R19-unfinished-deletion-recovery-disappears-after-a-reload-and-t.md) |
| R20 | medium | confirmed | — | frontend | [E2E specs click or assert a Fullscreen control that is not rendered on an empty pipeline](issues/R20-e2e-specs-click-or-assert-a-fullscreen-control-that-is-not-r.md) |
| R21 | low | confirmed | Composer, advisor and planner | seams | [The model-facing disclosure says the review "did not clear" on outage blocks where no review was obtained](issues/R21-the-model-facing-disclosure-says-the-review-did-not-clear-on.md) |
| R22 | low | confirmed | Composer, advisor and planner | backend | [Withheld-reply rows in the compose loop are written in separate transactions ahead of the turn's atomic audit cohort](issues/R22-withheld-reply-rows-in-the-compose-loop-are-written-in-separ.md) |
| R23 | low | confirmed | Composer, advisor and planner | backend | [CATEGORY/STEPS parsing does not tolerate markdown emphasis](issues/R23-category-steps-parsing-does-not-tolerate-markdown-emphasis.md) |
| R24 | low | confirmed | Composer, advisor and planner | backend | [Unreachable `None` arm on `_advisor_blocked_result(assistant_message)`](issues/R24-unreachable-none-arm-on-advisor-blocked-result-assistant-mes.md) |
| R25 | low | confirmed | Composer, advisor and planner | backend | [A NUL character in unadmitted planner prose makes the atomic planner audit cohort unwritable on PostgreSQL](issues/R25-a-nul-character-in-unadmitted-planner-prose-makes-the-atomic.md) |
| R26 | low | confirmed | Composer, advisor and planner | backend | [The staged `planner_prose_unadmitted` row is not bound to the call that produced it](issues/R26-the-staged-planner-prose-unadmitted-row-is-not-bound-to-the.md) |
| R27 | low | confirmed | Composer, advisor and planner | backend | [The coalesce `on_error` rejection offers `quorum`, which the composer cannot author, and omits the `best_effort` timeout](issues/R27-the-coalesce-on-error-rejection-offers-quorum-which-the-comp.md) |
| R28 | low | confirmed | Composer, advisor and planner | backend | [The model-catalog budget was cut from 32 KiB to 8 KiB to fit a test ratchet, and the shipped catalog now carries only bedrock](issues/R28-the-model-catalog-budget-was-cut-from-32-kib-to-8-kib-to-fit.md) |
| R29 | low | confirmed | Composer, advisor and planner | backend | [The catalog docstrings still describe closed discovery and all-or-nothing deferral](issues/R29-the-catalog-docstrings-still-describe-closed-discovery-and-a.md) |
| R30 | low | confirmed | Composer, advisor and planner | backend | [On the planner surface, the `llm_user_prompt_missing` explanation promises query names the planner never receives](issues/R30-on-the-planner-surface-the-llm-user-prompt-missing-explanati.md) |
| R31 | low | confirmed | Composer, advisor and planner | backend | [The prompt-role fixes name `patch_node_options`, which the planner surface does not have](issues/R31-the-prompt-role-fixes-name-patch-node-options-which-the-plan.md) |
| R32 | low | confirmed | Composer, advisor and planner | backend | [The `prompt_template_parts_required` guard is bypassed by echoing the unchanged parts, and the raw edit is silently dropped](issues/R32-the-prompt-template-parts-required-guard-is-bypassed-by-echo.md) |
| R33 | low | confirmed | Composer, advisor and planner | backend | [The corrupt-digest refusal applies only to `patch_node_options`, and `upsert_node` silently repairs the same corrupt digest](issues/R33-the-corrupt-digest-refusal-applies-only-to-patch-node-option.md) |
| R34 | low | confirmed | Composer, advisor and planner | backend | [The new `prompt_template_parts_required` code is not registered in the validation-guidance catalogue](issues/R34-the-new-prompt-template-parts-required-code-is-not-registere.md) |
| R35 | low | confirmed · pre-existing | Interpretation events | backend, seams | [Server-route interpretation surfaces are still recorded, and hashed, with `actor='composer-llm'` (mostly pre-existing)](issues/R35-server-route-interpretation-surfaces-are-still-recorded-and.md) |
| R36 | low | confirmed | Interpretation events | backend | [The `/validate` repair surfacer stamps `composer_llm` provenance on surfaces owed by a state revert](issues/R36-the-validate-repair-surfacer-stamps-composer-llm-provenance.md) |
| R37 | low | confirmed | Interpretation events | frontend, seams | [The inline-receipt preservation code and the `addPendingEvent` guard protect paths that have no production caller](issues/R37-the-inline-receipt-preservation-code-and-the-addpendingevent.md) |
| R38 | low | confirmed | Interpretation events | frontend | [The newest-started refresh fence discards a successful older snapshot when the newer refresh fails](issues/R38-the-newest-started-refresh-fence-discards-a-successful-older.md) |
| R39 | low | confirmed | Pricing, planner failures and tutorial run | backend, seams | [`cost_unavailable` still offers Retry, and every retry is another provider call that cannot be priced](issues/R39-cost-unavailable-still-offers-retry-and-every-retry-is-anoth.md) |
| R40 | low | confirmed · pre-existing | Pricing, planner failures and tutorial run | backend, seams | [The recompose planner-failure progress event lacks the `COST_UNAVAILABLE` branch that send_message has](issues/R40-the-recompose-planner-failure-progress-event-lacks-the-cost.md) |
| R41 | low | confirmed · pre-existing | Pricing, planner failures and tutorial run | seams | [Malformed or missing provider usage is reported as `COST_UNAVAILABLE`, which now reads as a pricing-configuration fault (pre-existing)](issues/R41-malformed-or-missing-provider-usage-is-reported-as-cost-unav.md) |
| R42 | low | confirmed · pre-existing | Pricing, planner failures and tutorial run | seams | [Boolean `max_tokens` rejection misses the operator profile and LLM source boundaries (pre-existing)](issues/R42-boolean-max-tokens-rejection-misses-the-operator-profile-and.md) |
| R43 | low | confirmed | Pricing, planner failures and tutorial run | seams | [The `tutorial_live_run_failed` 409 points learners at run details the tutorial error screen does not offer](issues/R43-the-tutorial-live-run-failed-409-points-learners-at-run-deta.md) |
| R44 | low | confirmed | Plugin policy (`azure_ai_search`) | seams | [The contract comment says the private set is shared with the plugin, but the plugin does not use it](issues/R44-the-contract-comment-says-the-private-set-is-shared-with-the.md) |
| R45 | low | confirmed | Plugin policy (`azure_ai_search`) | seams | [Profiled assistance replaces the plugin's operational hints instead of extending them](issues/R45-profiled-assistance-replaces-the-plugin-s-operational-hints.md) |
| R46 | low | confirmed | Plugin policy (`azure_ai_search`) | backend | [The Azure Search rejection text still uses the LLM provider/model/pacing repair wording](issues/R46-the-azure-search-rejection-text-still-uses-the-llm-provider.md) |
| R47 | low | confirmed | Plugin policy (`azure_ai_search`) | backend | [The Azure `example_use` hard-codes an index that the only profile's closed pin may reject](issues/R47-the-azure-example-use-hard-codes-an-index-that-the-only-prof.md) |
| R48 | low | confirmed | Execution and run controls | seams | [The poll-path live-run attach seeds progress as "pending", but the Run gates check only for "running"](issues/R48-the-poll-path-live-run-attach-seeds-progress-as-pending-but.md) |
| R49 | low | confirmed | Execution and run controls | seams | [The `composerBusy` gate was added only to ExecuteButton, so other Run surfaces become silent dead controls](issues/R49-the-composerbusy-gate-was-added-only-to-executebutton-so-oth.md) |
| R50 | low | confirmed | Execution and run controls | backend | [The saga settlement in `finally` raises `SessionDerivedCustodyError` when the failed status never landed, masking the original exception](issues/R50-the-saga-settlement-in-finally-raises-sessionderivedcustodye.md) |
| R51 | low | confirmed | Execution and run controls | backend | [A bad `execution_rate_limit.persistence_path` is accepted at boot and fails every web run](issues/R51-a-bad-execution-rate-limit-persistence-path-is-accepted-at-b.md) |
| R52 | low | confirmed | Execution and run controls | backend | [Removing a check name makes share links minted before the upgrade return 500 instead of 401](issues/R52-removing-a-check-name-makes-share-links-minted-before-the-up.md) |
| R53 | low | confirmed | Execution and run controls | backend | [The `completion_gates_meta_from_facts` docstring describes old behaviour](issues/R53-the-completion-gates-meta-from-facts-docstring-describes-old.md) |
| R54 | low | confirmed | Execution and run controls | frontend, seams | [The readiness decoder does not check the new required blocker field `note`](issues/R54-the-readiness-decoder-does-not-check-the-new-required-blocke.md) |
| R55 | low | confirmed · pre-existing | People and access | seams | ["Creating the account does not grant access" is false under the default open registration (pre-existing)](issues/R55-creating-the-account-does-not-grant-access-is-false-under-th.md) |
| R56 | low | confirmed | People and access | frontend | [The hidden delete sub-view stays open and dirty after a partial deletion](issues/R56-the-hidden-delete-sub-view-stays-open-and-dirty-after-a-part.md) |
| R57 | low | confirmed | People and access | frontend | [Moving Sign-in above the tabs places the Roles tab's content under the Sign-in heading](issues/R57-moving-sign-in-above-the-tabs-places-the-roles-tab-s-content.md) |
| R58 | low | confirmed | People and access | frontend, seams | [`active_human_admin_count` is still produced but no longer consumed, and related prose is stale](issues/R58-active-human-admin-count-is-still-produced-but-no-longer-con.md) |
| R59 | low | confirmed | People and access | backend | [The `active_human_admin_ids` docstring says "advisory for display", but the method is now the authorization gate for local-account admin](issues/R59-the-active-human-admin-ids-docstring-says-advisory-for-displ.md) |
| R60 | low | confirmed | People and access | frontend | [Inserted blocks orphaned the doc comments of `personDisambiguator` and `deleteAdminUser`](issues/R60-inserted-blocks-orphaned-the-doc-comments-of-persondisambigu.md) |
| R61 | low | confirmed | Frontend workspace, inspector and stores | frontend | [`flex-wrap` on blocker rows moves the Ask button onto its own line, and the comment says layout is unchanged](issues/R61-flex-wrap-on-blocker-rows-moves-the-ask-button-onto-its-own.md) |
| R62 | low | confirmed | Frontend workspace, inspector and stores | frontend | [The Approvals tab has no keyboard-focusable scroll region](issues/R62-the-approvals-tab-has-no-keyboard-focusable-scroll-region.md) |
| R63 | low | confirmed | Frontend workspace, inspector and stores | frontend | [Comments still describe the removed Workflow-tab approvals table](issues/R63-comments-still-describe-the-removed-workflow-tab-approvals-t.md) |
| R64 | low | confirmed | Frontend workspace, inspector and stores | frontend | [Multi-query prompt rows are keyed by a query name that may not be unique](issues/R64-multi-query-prompt-rows-are-keyed-by-a-query-name-that-may-n.md) |
| R65 | low | confirmed | Frontend workspace, inspector and stores | frontend | [The unsaved-draft recovery branch cannot be reached against the current backend](issues/R65-the-unsaved-draft-recovery-branch-cannot-be-reached-against.md) |
| R66 | low | confirmed | Frontend workspace, inspector and stores | frontend | [The Graph-to-Workflow and Focus graph-to-Fullscreen renames were not carried into the training docs](issues/R66-the-graph-to-workflow-and-focus-graph-to-fullscreen-renames.md) |
| R67 | low | confirmed | Frontend workspace, inspector and stores | frontend | [The LLM model label appears on llm transform cards but not on llm source cards](issues/R67-the-llm-model-label-appears-on-llm-transform-cards-but-not-o.md) |
| R68 | low | confirmed | Frontend workspace, inspector and stores | frontend | [A failed accept hydration leaves `compositionStateLoaded` false permanently](issues/R68-a-failed-accept-hydration-leaves-compositionstateloaded-fals.md) |
| R69 | low | confirmed | Frontend workspace, inspector and stores | frontend | [The 409 detail is dropped when the conflict-refresh snapshot is superseded](issues/R69-the-409-detail-is-dropped-when-the-conflict-refresh-snapshot.md) |
| R70 | low | confirmed | Frontend workspace, inspector and stores | frontend | [A blob delete failure with an empty `statusText` shows no error](issues/R70-a-blob-delete-failure-with-an-empty-statustext-shows-no-erro.md) |
| R71 | low | confirmed | Deploy configuration and docs | seams | [The systemd env example says the shipped nginx uses 240 s, but it uses 360 s](issues/R71-the-systemd-env-example-says-the-shipped-nginx-uses-240-s-bu.md) |
| R72 | low | confirmed · pre-existing | Deploy configuration and docs | backend, seams | [The environment-variable reference is stale for the Docker timeout, the transport ceiling and `EXECUTION_RATE_LIMIT`](issues/R72-the-environment-variable-reference-is-stale-for-the-docker-t.md) |
| R73 | low | confirmed | Deploy configuration and docs | seams | [The firewall advice for port 8451 is usually ineffective for a Docker-published port](issues/R73-the-firewall-advice-for-port-8451-is-usually-ineffective-for.md) |
| G1 | low | confirmed | Guided lane (retiring) | frontend | [A guided respond during a proposal action leaves the busy flag stuck](issues/G1-a-guided-respond-during-a-proposal-action-leaves-the-busy-fl.md) |
| G2 | low | confirmed | Guided lane (retiring) | backend | [The inspection confirmation must equal the observed headers, which strands CSVs with blank headers](issues/G2-the-inspection-confirmation-must-equal-the-observed-headers.md) |
| G3 | low | confirmed | Guided lane (retiring) | frontend | [The `isTutorial` prop is dead, and three comments describe the removed column editor](issues/G3-the-istutorial-prop-is-dead-and-three-comments-describe-the.md) |
| G4 | low | confirmed | Guided lane (retiring) | frontend | [The JSON textarea no longer opens pretty-printed](issues/G4-the-json-textarea-no-longer-opens-pretty-printed.md) |
| G5 | low | confirmed | Guided lane (retiring) | frontend | [The `fieldHasError` comment is stale, and its `json-object`/`json-array` string arms are unreachable](issues/G5-the-fieldhaserror-comment-is-stale-and-its-json-object-json.md) |
| G6 | low | confirmed | Guided lane (retiring) | backend | [A docstring claims `ModifyResponseException` handling matches `_explain_run_diagnostics`](issues/G6-a-docstring-claims-modifyresponseexception-handling-matches.md) |

## 3. Disputed findings

### seam-08-execution#1: `completion_gates` v2 shipped without an epoch bump (merged into R02)

**Case that it is a defect.** The trace refuter could not refute it and rated it medium.
- 41aeaeac0 made the persisted grammar strictly incompatible and left the epoch at 65.
- On the first parent of `release/0.8.1`, commits 5bcd983c9, 7a0c3172d, df9c76ec7 and 44e13a82e run at epoch 65 with the old writer.
- The ruling "no old-format compatibility" rules out compatibility code, not the epoch bump. The project pairs strict-grammar breaks with an epoch bump so that startup refuses the store: 539121b17 did exactly that for the `note` key, and `test_epoch_61_is_rejected_before_reading_old_advisor_gate_grammar` pins the pattern.
- The four other independent sources (be-23#1, be-04#1, be-26#1, seam-01-wire#2) and be-17#1 all reached the same conclusion through their own verifiers.

**Case that it is not.** The impact refuter refuted it and rated it low.
1. The implementation plan for 41aeaeac0 records the operator's explicit decision: reject old envelopes, including empty mappings, with no migration, no compatibility branch and no data reset. An epoch bump would force the reset the plan forbids.
2. The remote-tracking reflog shows `origin/release/0.8.1` moving straight from `fe01ad94b` (epoch 64) to `9fce3b2c6`, which already contains the merge 23185f55a. So no pushed tip ever paired epoch 65 with the old writer.
3. The only exposed stores are local, unpublished ones, and the plan's rollback section covers those with an approved development-store reset. What remains is a documentation nit: the epoch-65 runbook paragraph names only the note key.

**What this synthesis measured.**
- `git reflog show origin/release/0.8.1` shows `fe01ad94b` at 2026-09-22 09:35 followed directly by `9fce3b2c6` at 2026-09-23 05:46. 5bcd983c9 *is* an ancestor of `origin/release/0.8.1`, but it was never a pushed tip.
- On the first parent, the old writer ran at epoch 65 from 22:55 (5bcd983c9) to 00:55 (23185f55a), about two hours. Counting from 539121b17 on its feature branch (14:38), it is about ten hours.
- Both refuters are therefore right in their own frames: no *published* tip is exposed, and *local* stores stamped in that window are.
- The be-23#1 impact refuter reports that the operator's local `elspeth-web` restarted at 09-22 23:28:43 AEST on first-parent head 5bcd983c9 and wrote `data/sessions.db` four seconds later. That is one concrete exposed store. It was not re-checked here, because the main checkout was off-limits.

**Assessment.** The plan's ban on "resetting existing data" is about data migration, not the startup epoch fence. An epoch bump makes the forced recreate explicit at startup rather than a 500 per request. The remaining decision is the operator's: either bump to 66, or record that early-65 local stores must be recreated and add that to the CHANGELOG and runbook.

### Related cross-source disagreement (not a disputed status)

be-19#3 (confirmed, R47) and seam-04-plugin-policy#4 (refuted, §4) report the same hard-coded `index: approved-documents` in `profiles.py:1617`.
- One trace refuter confirmed it as a real, low-severity teaching defect that costs one repair turn when a single closed-pin profile is present.
- The other held that the design promise is kept, because the `index` enum and the per-alias description already teach the admitted index.

## 4. Refuted findings

These findings were refuted. Do not re-raise them without new evidence.

| Id | Title | File:line | Why refuted |
|---|---|---|---|
| seam-03-pricing#1 | An Azure node with no `pricing_model` is priced from the bare deployment name at OpenAI rates | `plugins/infrastructure/clients/llm.py:582` | The path is real, but nothing shows the OpenAI rate card is wrong for that deployment. The fallback is documented, deliberate behaviour of the same commit. |
| be-11#2 | `upsert_node` silently discards a changed `prompt_template` on a structured node | `web/composer/tools/transforms.py:829` | The refuter agreed the mechanism is real: the reconciler overwrites `prompt_template` with the parts render. The "enforced on patch only" framing is wrong, because the patch-side guard is just as narrow (see R32, the confirmed echo bypass). The rest of the reason was truncated in the verifier output. |
| seam-03-pricing#4 | The docs say `pricing_model` is the effective billing identity even when it did not price the call | `docs/reference/environment-variables.md:209` | The code facts are right: `pricing_model` is always recorded (`llm_response_parsing.py:669`) and is ignored when a provider cost is present (`llm_pricing.py:78-79,96-101`). The finding reads "effective billing identity" as "the identity that priced this call", which is not how the project uses the term, so the doc is not false. |
| seam-04-plugin-policy#2 | The private-binding guard is a denylist, so a new `AzureAISearchConfig` option would be authorable | `web/plugin_policy/profiles.py:1223` | No failure at the pin. Every current field is either private or ordinary query configuration. The scenario needs a hypothetical future option. |
| seam-04-plugin-policy#4 | The profiled `example_use` hard-codes `index: approved-documents` | `web/plugin_policy/profiles.py:1617` | The design teaches the admitted index through the enum and the per-alias descriptions. See §3: be-19#3 on the same line was confirmed as low. |
| seam-08-execution#5 | The "canonical 23 checks" docstring does not match the registry count | `web/execution/service.py:1457` | "Canonical 23" is a defined term, `CORE_VALIDATION_CHECK_NAMES`, fixed by an import-time guard. The reviewer's 26 also counted checks after the cut-off. |
| seam-09-deploy-config#4 | The probe rejects a valid terminal event with no `event_sequence` | `scripts/probe_websocket_ingress.py:34` | The orphan-cancel branch it relies on runs only when `recovery_coordinator is None`, and production always passes one. |
| be-09#5 | `request_not_met` FLAGs are classified `graph_rejected`, so they persist across messages | `web/composer/service.py:8625` | This is the recorded design. The plan maps `flagged_final_pass` and `flagged_no_repair` to GRAPH_REJECTED whatever the category, and only `flagged_unrepairable` maps to MESSAGE_REJECTED. |
| be-24#1 | The guided accept provenance refusal fires after settlement is durable | `web/sessions/routes/composer/guided.py:5215` | Correct about placement, but a proposal row with NULL provenance cannot reach that settlement, because the authority load rejects it first. |
| be-22#2 | Pending-interpretation DTOs accept a `composer_skill_hash` that the DB CHECK rejects | `web/sessions/protocol.py:1558` | Every producer passes a sha256 hexdigest, a value read back from a CHECK-guarded column, or None. There is no Tier-3 path. |
| fe-07#1 | Clearing a JSON field is treated as invalid JSON (guided) | `frontend/.../guided/SchemaFormTurn.tsx:464` | The headline claim is false: an optional JSON knob can still be cleared through the window's code path. |
| be-08#3 | A new single-variable A/B rule ships beside an "A/B" exemplar whose arms differ in everything | `web/composer/planner_authoring_aids.py:451` | The "A/B" label is only in a July Python docstring that never reaches the LLM. The exemplar's LLM-facing metadata is "Per-branch LLM assessment". |
| be-08#4 | The literal-prompt rules disagree (ask before adapting, adapt and disclose, keep unchanged) | `web/composer/planner_authoring_aids.py:677` | Read together, the new rule is the precedence statement the finding asks for. |
| be-17#3 | The handoff branch ignores fact currency and says "has not cleared" for an unreviewed graph | `web/execution/completion_gates.py:355` | The branch repeats no advisor verdict for either a current or a stale graph. It returns one fixed, verdict-free sentence, so the invariant holds. |
| be-21#2 | `SESSION_SCHEMA_EPOCH` is 65 but the history block ends at 64 | `web/sessions/models.py:356` | The missing `# 65:` entry is an incomplete comment, not a false one. The startup error names the fix. (The separate v2-without-bump defect is R02.) |
| be-21#3 | The `cost_unavailable` copy blames pricing configuration for malformed provider cost metadata | `web/sessions/routes/guided_operations.py:93` | The refuter agreed the path is reachable: `usage.cost = -1`, or missing `prompt_tokens`, leads to `COST_UNAVAILABLE` and the pricing copy. It refuted three of the finding's parts. Only the first survives in the truncated output: "the code does not contradict" its stated distinction. The same mechanism was confirmed from the planner side as R41 (seam-03-pricing#2, low), so treat R41 as the record, not this row. |
| fe-11#4 | A rejected logout cleanup barrier is never cleared | `frontend/src/stores/authStore.ts:123` | No way to make the cleanup reject exists at the pin. The reviewer called it hardening. |
| be-14#1 | A web-only `set_pipeline` wrapper instruction sits in the declaration the MCP server also serves | `web/composer/tools/sessions.py:2334` | The plumbing is right, but the instruction is not false for MCP clients. |
| be-14#2 | The envelope teaching was fixed in one declaration only, and sibling hints still say "exact set_pipeline arguments" | `web/composer/tools/sessions.py:2334` | The anchored line is the corrected text. The cited stale lines were untouched and the finding is low severity, which is out of scope for undiffed lines. Three of the four sites teach nothing wrong. |

## 5. Coverage and limits

- **Scope.** This covers web backend production code, the web frontend under `src/elspeth/web/frontend/src` (excluding e2e specs) and nine seams. Tests, docs and about 70 non-web code and config files in the window were read only where a seam or finding pulled them in (for example R20, R66 and R72). A clean result says nothing about them.
- **No suites were run.** Reviewers and verifiers were read-only: no pytest, no testcontainer runs, no npm, no build and no linters. Where a finding says "reproduced", it was run with a read-only scratch probe against the worktree, with `elspeth.__file__` checked to be inside it (R01, R17, R18, R28, R32, R33, R34 and seam-03-pricing#3).
- **Reasoned, not measured.** The following were argued from code and documentation, not measured:
  - PostgreSQL deadlock victim selection and frequency (R05);
  - psycopg driver behaviour on NUL (R25);
  - Docker's firewall bypass (R73);
  - uvicorn's peer address inside the container (R11);
  - CSS line-breaking (R61);
  - browser scroll-focus behaviour (R62).
- **Not re-verified by this synthesis.**
  - The live local deployment behind R02 (service start 09-22 23:28:43 AEST) comes from one refuter's observation of the main checkout and was not re-checked here.
  - Line numbers are at `74c0ce0db`. `release/0.8.1` has moved since: `origin` is at `9fce3b2c6`, pushed 09-23 05:46.
- **Guided lane.** Only the diff hunks were reviewed (issues G1–G6).
- **Refuted reasons (§4).** These are summarised from verifier output that the synthesis received cut at about 400 characters. The full, untruncated verifier reasons for every source finding, refuted ones included, are in `findings.json` (`verdicts[].reason`). Treat the §4 reasons as abstracts.
- **Loomweave.** Its index was not trusted for the pinned commit. Callers and definitions came from grep and git over the worktree.
- **Severity.** Every severity is the verifiers' adjusted value. Merged root causes take the highest severity among their sources, and the disagreements are noted in each entry.
- **Counts.** All counts in §1 were computed from `.claude/lanes/web-review-20260923/synthesis-map.tsv`: 110 rows, 110 unique ids and 79 clusters.
