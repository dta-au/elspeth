# R3 RCA remediation tracker

Last refreshed: 2026-08-04T16:12:00+10:00 (Australia/Canberra)
Filigree snapshot: 2026-08-04T16:10:00+10:00
Release source baseline: `release/0.7.2@937a8010b`
Coordination owner: `codex-r3-rca-coordinator`

This is the human-readable companion to Filigree for the R3 remediation
program. Live Filigree state and the current release branch are authoritative;
this file is refreshed after each audit, implementation, integration, and AWS
acceptance checkpoint.

The supplied objective headline says “41 issues across 4 RCA epics, plus 5
pre-existing” (46 items), but its explicit list contains **39 RCA-group IDs
plus five pre-existing IDs: 44 unique non-parent records**. Four parent
workstream records are tracked separately. This tracker follows the explicit
IDs so nothing is silently dropped.

## Priority policy

Work is ordered by demo impact, not merely by tracker priority number:

1. advisor-gate correctness and live sign-off behavior;
2. compose-loop scaffolding, guidance, correlation, and telemetry;
3. gate-routing fidelity, validation, failure behavior, and audit evidence;
4. directly implicated product/system defects that affect the demonstrated
   workflow; and
5. work whose primary effect is only to make installation, teardown, policy
   application, or operator shell/runbook setup easier **after** the
   demo-aware product path is accepted.

The classification test is: **does this change core functionality, or does it
only make setup easier?** Textract behavior, the S3 source/plugin, runtime AWS
integrations in an already usable deployment, validation, routing, and audit
evidence are core/demo-aware work even though they involve AWS. Installing,
updating, or tearing down the environment through IAM, Terraform, runbooks, or
shell mechanics remains deferred even when it blocks a fresh install.

## Current roll-up — 44 objective items plus 26 discovered/delivery records

| State | Objective 44 | Additional 26 | Meaning |
|---|---:|---:|---|
| Closed | 11 | 8 | Tracker says done; closure evidence is still sampled during the completion audit |
| Verifying | 15 | 12 | Locally fixed; live or requirement-specific acceptance remains |
| Fixing | 2 | 3 | Owned bug implementation is still in flight |
| In progress | 0 | 0 | A delivery task spanning existing objective defects is in flight |
| Open | 4 | 2 | Confirmed task/epic work not yet started here |
| Triage | 9 | 1 | Root cause and reproducibility must be checked against current HEAD before fixing |
| Proposed | 3 | 0 | Regression-gate features require approval/acceptance design before implementation |

The additional records are newly discovered gate-custody bug
`elspeth-1d97fc4b80`, run-diagnostics UI bug `elspeth-18b39eb829`,
non-overlapping compose delivery task `elspeth-73c7a4df36`, deferred
evidence-supportability task `elspeth-01c627b420`, and three implementation
children of the systemic provider-projection audit: `elspeth-dca1e81c58`,
`elspeth-3526685369`, and `elspeth-eacfec09a6`. They are tracked without
changing the supplied objective's 44-item arithmetic. Three later core splits
are also tracked: frontend readiness parity `elspeth-4c699cb5d0`, shared
advisor deadline custody `elspeth-57232f6f3c`, and guided-plan terminal
progress `elspeth-4e6f2a59e4`. The completed exhaustive provider-projection
audit added nine precisely scoped implementation records; they are listed in
the dedicated follow-up table below. Current-HEAD advisor re-audit added one
contract-honesty split, `elspeth-532dfcb0c7`, without reopening the fixed D1
false-FLAG loop.
The guided advisory RCA added one P1 authority-split record,
`elspeth-32a2242510`, after reproducing typed authored gate literals in a
provider system-role message.
The advisor contract-honesty RCA also found one independent runtime-validation
gap, `elspeth-0c73de77d5`, because an unknown JSON sink encoding reaches the
first write before codec rejection.
The exact-option projection audit added two coupled core records:
`elspeth-826765af90` for safely grounding option obligations before provider
projection, and `elspeth-d293c5d139` for rejecting contradictory deferred
constraints as one allegedly valid intent.
A full-example dogfood run added `elspeth-454892147c`: retry exhaustion reaches
the terminal `UNROUTED` arm without consulting the transform's configured
`on_error` quarantine sink, contrary to the shipped example contracts.
Provider-telemetry review added `elspeth-4d0d239886`: the provider-capable
`guided/respond` route is not mounted on the shared Composer request telemetry
dependency, so provider-call metrics exist without the matching request
duration, call-count aggregate, status, or inflight lifecycle.

Closed-record assignees below are retained Filigree audit history, not active
claim custody.

## Active coordination wave

| Agent | Scope | Worktree/branch | File custody | AWS authority | Status |
|---|---|---|---|---|---|
| `/root` | Integration, tracker custody, combined release verification, worktree partitioning, and AWS operations | `.claude/worktrees/r3-rca-remediation-tracker` (`codex/r3-rca-remediation-tracker`); `.claude/worktrees/verify-guided-schema-batch`; release checkout | Release integration; tracker mutation custody transferred 2026-08-04 to `claude-grounded-custodian` (Claude Fable session, main checkout) | Sole mutation custodian | Active; advisor, JSON-codec, and retry-routing fixes are durable through `937a8010b`; exact release `d8c8ee7b8` passed 37,305 tests and Loomweave is fresh there |
| `/root/review_guided_proposal_feedback` | Focused custody, retry, closed-shape, unchanged-target, security, and accessibility review | Read-only `.claude/worktrees/guided-proposal-feedback` | None | None | Complete; approved exact candidate `a5b5c9cee` with no P1/P2 findings |
| `/root/rca_multiquery_standard_json` | Root-cause the standard-JSON provider-contract omission and audit-trace seam | Shared read-only release baseline | None | None | Complete; confirmed standard mode enforced names, types, and enums absent from both provider and Langfuse messages |
| `/root/fix_multiquery_standard_json` | TDD implementation of deterministic standard-JSON contract projection | `.claude/worktrees/fix-multiquery-standard-json`; `codex/fix-multiquery-standard-json` | Released after integration | None | Complete exact candidate `f84a2fc37`; integrated as release `4f95c29a1` after two independent approvals |
| `/root/review_multiquery_standard_json` | Independent provider-message, structured-parity, trace-parity, and fail-closed review | Read-only `.claude/worktrees/fix-multiquery-standard-json` | None | None | Complete; first review blocked empty-system trace drift, then approved amended `f84a2fc37` with no P1/P2 findings |
| `/root/rca_guided_prose_revision_preservation` | Reproduce and bound whole-proposal prose-amend preservation/custody | Shared read-only release baseline | None | None | Complete; confirmed active proposal is absent from planning and private predecessor semantics have no server custody |
| `/root/fix_guided_prose_revision_preservation` | Add closed amend/replace authority, preservation, repair, exact message custody, and retry | `.claude/worktrees/guided-prose-revision`; `codex/fix-guided-prose-revision` | Released after reviewed integration | None | Complete through release `97bad7128` + `21f55834d` + fixture correction `6a59bcf77`; live acceptance remains |
| `/root/rca_advisor_residual_obligations` | Recheck END evidence non-injectivity after the advisor projection fix | Shared read-only release baseline | None | None | Complete; original impossible false-FLAG obligation is fixed, leaving a separate product-contract question about calling evidence-scoped CLEAN whole-pipeline sign-off |
| `/root/rca_freeform_schema_progress` | Reproduce cross-request schema-evidence custody and design bounded rehydration | Shared read-only release baseline | None | None | Complete; exact request-two capture proved progress says satisfied while the full schema is absent and specified current-policy rehydration |
| `/root/fix_freeform_schema_progress` | Rehydrate bounded whole current-policy schema contracts and base progress on current request evidence | `.claude/worktrees/freeform-schema-progress`; `codex/fix-freeform-schema-progress` | Released after reviewed integration | None | Complete as release `bb5a81213`; all 48 trained-operator contracts projected without omission and live two-request acceptance remains |
| `/root/rca_planner_escape_hatch_final_candidate` | Reproduce the final-candidate omission in the one-shot repair escape hatch | Shared read-only release baseline | None | None | Complete; distinct candidates proved the final candidate is absent while only its rejection code survives |
| `/root/fix_planner_escape_hatch_final_candidate` | Retain the raw provider-authored terminal candidate plus one protocol-closed rejection result for the hatch | `.claude/worktrees/planner-escape-hatch-final-candidate`; `codex/fix-planner-escape-hatch-final-candidate` | Released after reviewed integration | None | Complete as release `8ba6f97a8`; terminal candidate custody, association scrubbing, and error primacy approved |
| `/root/rca_guided_advisory_graph_context` | Reconcile Step-3/4 Explain obligations with actual graph evidence and provider role authority | Shared read-only release baseline | None | None | Complete; materially different valid gates had identical contexts and typed authored gate literals were promoted into system role |
| `/root/fix_guided_advisory_graph_authority` | Bind advisory graph context to the frozen proposal/wire payload and split system-safe structure from authored user-role literals | `.claude/worktrees/guided-advisory-graph-authority`; `codex/fix-guided-advisory-graph-authority` | Released after reviewed integration | None | Complete as release `07f966c8a`; exact graph custody and provider-role separation independently approved |
| `/root/rca_run_diagnostics_safe_classification` | Reproduce equal-length failure-class collapse and isolate the safe evidence boundary | Shared read-only release baseline | None | None | Complete; advisory Explain alone loses cause classification while Landscape/static UI remain correct |
| `/root/fix_run_diagnostics_safe_classification` | Preserve a closed server-owned failure classification without raw runtime/provider text | `.claude/worktrees/run-diagnostics-safe-classification`; `codex/fix-run-diagnostics-safe-classification` | Released after reviewed integration | None | Complete as release `1213ab5b4`; 37,208-test full gate and 69 release-local diagnostics canaries passed |
| `/root/rca_advisor_signoff_contract_honesty` | Reconcile bounded END evidence with advertised whole-pipeline certification | Shared read-only release baseline | None | None | Complete; evidence is intentionally non-injective and durable semantics are a completion/share veto, not affirmative certification |
| `/root/fix_advisor_evidence_scope_wording` | Make skill, progress, terminal, readiness, and repair wording consistently evidence-scoped | `.claude/worktrees/advisor-evidence-scope-wording`; `codex/fix-advisor-evidence-scope-wording` | Released after reviewed integration | None | Complete as release `ee341c6f4`; independently approved and advanced to verifying after the clean combined and release-local gates |
| `/root/fix_json_sink_encoding_validation` | Reject unavailable and non-text JSON codecs before publication or the first write | `.claude/worktrees/json-sink-encoding-validation`; `codex/fix-json-sink-encoding-validation` | Released after reviewed integration | None | Complete through release `9d517df5e`; codec validation and the refreshed authoritative DAG corpus are advanced to verifying |
| `/root/grounded_option_finish` | Ground exact safe option obligations, keep private values out of provider context, and reject contradictory constraints | `.claude/worktrees/grounded-option-constraints`; `codex/fix-grounded-option-constraints` | Deferred-intent authority/admission/planning seams and direct tests | None | Owner `claude-grounded-custodian` since 2026-08-04; prior owner Codex is frozen on a usage limit until 2026-08-08. Terminal candidate `ef73bfa70` (parent `5a96b3848`) is abandoned non-integrable per a unanimous 7-lens SME panel; preservation commit `a5d7fc0e7` on the branch holds the orphaned exact-type/tz-aware custody verification for lane C. **Lane A is integrated**: fresh ADR-first candidate `b803879cb` merged `--no-ff` as release `50d57e07d` after an independent review returned CLEAR with no in-scope findings; `elspeth-d293c5d139` advanced to verifying. Lanes B (`elspeth-826765af90`, stated-option grounding/projection with SESSION_SCHEMA_EPOCH 43→44) and C (`elspeth-e75dc03d3e`, custody/prepublication hardening) are next; the `grounded-option-constraints` worktree is retained as their reference |
| Wave-2 advisor pair lane | Split the advisor injection scan from the render predicate, scan metadata/condition/routes, and stop publishing "configured and ready" on unvalidated repair turns | `.claude/worktrees/advisor-prescan-ready-fixes`; `claude/fix-advisor-prescan-ready` (removed after integration) | composer/service.py, composer/no_tool_policy.py + direct tests | None | Complete; two-commit chain (`4611b7491` = `elspeth-cd9af8e61d`, `c3ef6553a` = `elspeth-88592f5be7`) merged `--no-ff` as release `27a402979` after an independent review returned CLEAR; both bugs advanced to verifying. The prescan fix disproves the closure evidence of `elspeth-eacfec09a6` — reopened 2026-08-04 at operator sign-off (comment 2295) |
| Wave-2 proof-bypass lane | Fail closed on exit-to-freeform source-proof admission instead of degrading to the freeform no-proof baseline | `.claude/worktrees/exit-freeform-proof-fix`; `claude/fix-exit-freeform-proof` (removed after integration) | execution/service.py, composer/yaml_generator.py + direct tests | None | Complete; candidate `192c32ace` merged `--no-ff` as release `bf55d3bd1` after an independent review returned CLEAR; `elspeth-3b45cdb41e` advanced to verifying and parent epic `elspeth-c1b8b26d32` (three fail-direction inversions) carries the wave summary comment — its workflow holds it in_progress pending live acceptance |
| `/root/fix_retry_exhaustion_routing` | Route retry exhaustion through the configured transform `on_error` contract with complete audit custody | `.claude/worktrees/retry-exhaustion-routing`; `codex/fix-retry-exhaustion-routing` | Released after reviewed integration | None | Complete as release `937a8010b`; exact release retry/processor/traversal/property/integration canary passed 325 tests plus mypy, Ruff, and non-inert Wardline |
| `/root/fix_composer_provider_telemetry` | Project durable Composer provider-call audit facts into bounded operator metrics only after commit | `.claude/worktrees/composer-provider-telemetry`; `codex/fix-composer-provider-telemetry` | New provider projector, session post-commit seam, request/operator telemetry, and direct tests; grounded Composer files excluded | None | Complete as release merge `e8026fc7f`; amended candidate `401569773` cleared by parallel implementation review with no in-scope findings and integrated `--no-ff`; live CloudWatch acceptance remains |
| `/root/fix_guided_respond_request_telemetry` | Mount provider-capable guided/respond on the shared request telemetry lifecycle | `.claude/worktrees/guided-respond-request-telemetry`; `codex/fix-guided-respond-request-telemetry`; parent `401569773` | `guided.py` route-signature dependency hunk only and direct request-telemetry tests | None | Complete as release merge `67e2a1661`; candidate `e8cfe0d5e` cleared by parallel implementation review with no in-scope findings and integrated `--no-ff` after its parent lane; live CloudWatch acceptance remains; planner-intent construction hunks remain grounded-lane custody |
| `/root/review_advisor_evidence_scope` | Adversarial review of bounded evidence and actual checkpoint wire instructions | Shared read-only advisor worktree | None | None | Complete; found and drove repair of the shared stuck-composer system-contract conflict, then approved current bytes with no findings |
| `/root/audit_advisor_demo` | D1-D6 and advisor/F14 completion evidence | Shared read-only baseline | None | None | Completed; no residual local code defect found |
| `/root/audit_compose_loop` | Six compose-loop RCAs and implementation partitioning | Shared read-only baseline | None | None | Completed; all six remain actionable |
| `/root/audit_gate_routing` | Closed/verifying routing audit plus residual routing RCAs | Shared read-only baseline | None | None | Completed; two residual and one new P1 confirmed |
| `/root/audit_s3_textract_core` | Core S3 source/plugin, Textract, and Landscape audit behavior | Shared read-only baseline | None | None | Completed; S3 P1 confirmed; audit data complete |
| `/root/audit_implicated_legacy` | R2-F16, F14, and closed R2-F17 evidence | Shared read-only baseline | None | None | Completed; 17 focused tests passed |
| `/root/design_demo_acceptance` | Cross-issue live demo acceptance matrix | Shared read-only baseline | None | None | Completed; 12-step deployment matrix produced |
| `/root/review_text_tracker` | Tracker accuracy and completeness review | Shared read-only baseline | None | None | Completed; corrections incorporated |
| `/root/impl_guided_node_custody` | Preserve unchanged node behavior across guided replanning | `.claude/worktrees/guided-node-custody`; `codex/fix-guided-node-custody` | Released after integration | None | Complete; reviewed commit integrated as `b424c08c4` |
| `/root/impl_gate_row_error_policy` | Per-row gate `on_error` handling | `.claude/worktrees/gate-row-error-policy`; `codex/fix-gate-row-error-policy` | Released after integration | None | Complete through `e3804416f`; 72 backend plus 56 frontend release-checkout regressions passed; live verification remains |
| `/root/impl_csv_audit_characteristics` | Honest CSV audit characteristics | `.claude/worktrees/csv-audit-characteristics`; `codex/fix-csv-audit-characteristics` | CSV source, characteristic UI wording, exact scenario-corpus oracle, direct tests | None | Complete; behavior `d78fbbed6`, corpus oracle `10179f2c1` |
| `/root/impl_s3_source_profiles` | Operator-profiled Web S3 source | `.claude/worktrees/s3-source-profiles`; `codex/fix-s3-source-profiles` | Released after integration | None | Complete through `131a5f584`; independent security review clean and 629 release-checkout tests passed; live AWS S3 acceptance remains |
| `/root/impl_run_diagnostics_ui` | Surface routed-failure provenance | `.claude/worktrees/run-diagnostics-ui`; `codex/fix-run-diagnostics-ui` | `RunsHistoryDrawer` and direct frontend diagnostics presentation/tests | None | Complete; integrated as `9130e1209` + `0928c6ac6` |
| `/root/impl_compose_authoring_aids` | Compose context and truthful field-mapping aids | `.claude/worktrees/compose-authoring-aids`; `codex/fix-compose-authoring-aids` | Released after integration | None | Complete; integrated as `2906409e1` + `e1d31f104` |
| `/root/impl_compose_required_controls` | Finalize deterministic required controls on both freeform publication paths | `.claude/worktrees/compose-required-controls`; `codex/fix-compose-required-controls` | Released after integration | None | Complete through `32cec3d8a`; independent lifecycle/security review clean and 241 release-checkout proposal/authority/lifecycle tests passed; live Composer acceptance remains |
| Five read-only projection auditors | Reconcile every production provider call against the evidence its prompt asks the model to use | Shared read-only `23c1494a2` baseline | None | None | Complete; Loomweave plus direct inventory covered `src/elspeth`, `elspeth-lints/src`, and `gateway/src`; audit task `6795b3ae3a` closed with comment `2240` and thirteen linked residuals |
| `/root/impl_guided_chat_revision_custody` | Prevent blind source/sink replacement through guided chat | `.claude/worktrees/guided-chat-revision-custody`; `codex/fix-guided-chat-revision-custody` | Released after integration; `guided_chat_atomic.py` handed to terminal-progress lane | None | Complete as `7afa62b6d` + `c4cade0f0`; independent review clean and 10 exact release regressions passed |
| `/root/rca_compose_request_correlation` | Emit bounded correlation events for structured HTTP errors and Pydantic 422s | `.claude/worktrees/compose-request-correlation`; `codex/fix-compose-request-correlation` | Released after integration | None | Complete through `39c6b14a7`; 32 exact release-handler regressions passed; live CloudWatch lookup remains |
| `/root/impl_frontend_readiness_axes` | Apply backend readiness axes to every frontend action | `.claude/worktrees/frontend-readiness-axes`; `codex/fix-frontend-readiness-axes` | Released after integration | None | Complete; integrated through `9016ce6a6`, exact release rerun 148 passed, task closed |
| `/root/impl_guided_plan_terminal_progress` | Settle terminal progress for every guided-plan outcome | `.claude/worktrees/guided-plan-terminal-progress`; `codex/fix-guided-plan-terminal-progress` | Released after integration | None | Complete through `859c2a642`; independent adversarial review clean, 87 progress/guided-plan plus 10 chat-custody release tests passed, task closed |
| `/root/impl_guided_gate_proof_validation` | Make source-proof diagnostics authoritative during guided confirmation | `.claude/worktrees/guided-gate-proof-validation`; `codex/fix-guided-gate-proof-validation` | Released after integration | None | Complete through `39c7fc635`; final lifecycle/custody review clean and 512 release-checkout execution/redaction/guided tests passed; live guided acceptance remains |
| `/root/impl_advisor_surface_deadline` | Close successful re-review leakage, bind checkpoints to the compose deadline, and preserve truthful tool-timeout counters | `.claude/worktrees/advisor-surface-deadline` (`codex/fix-advisor-surface-deadline`); `.claude/worktrees/advisor-tool-timeout-counters` (`codex/fix-advisor-tool-timeout-counters`) | Released after reviewed integration | None | Complete through `a913ccb63`; independent follow-up review clean, 79 release-local advisor/persistence tests passed, task closed |
| `/root/impl_guided_selected_node_custody` | Preserve selected-node hidden state and reuse form rewind for wire edits | `.claude/worktrees/guided-selected-node-custody`; `codex/fix-guided-selected-node-custody` | Released after reviewed integration | None | Complete as `e2bc8c5e3` + `45476f021`; 32 backend and 220 frontend reviewer tests plus 19 release-local backend canaries passed; task closed |

No subagent may mutate AWS or the shared release checkout during this wave.
Each implementation agent is confined to the explicit worktree/file custody
listed above.

Parent workstream state:

| Parent record | Live state | Immediate coordination action |
|---|---|---|
| `elspeth-7ffd77deca` — advisor gate | `verifying` | Deploy `a8c6f091d` and prove a correct Textract pipeline reaches CLEAN without invented repair while visible mismatches still FLAG |
| `elspeth-7da4e52344` — compose loop | `fixing` | Advisor timeout and registry lanes are integrated; run combined release gates and deployed acceptance |
| `elspeth-e7ff15ac0b` — gate routing | `open` | Reconcile five closed, five verifying, and two residual children |
| `elspeth-e54343d43b` — AWS installer | `open` | Hold implementation until demo-aware product/system work is accepted; only deconflict existing owners in the meantime |

## Advisor gate — `elspeth-7ffd77deca`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-955438d517` | P1 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `a8c6f091d`; live Composer must prove bounded/withheld facts neither false-FLAG nor receive false certification |
| `elspeth-fcef029996` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live second-pass re-review converges with prior findings/actions visible |
| `elspeth-ca751fa4e1` | P1 | `verifying` | `codex-advisor-surface-deadline` | Integrated through `4f70a3619`; live FLAG-to-repair-to-CLEAN must expose only final clean user prose while retaining internal audit evidence |
| `elspeth-f5a9021d2d` | P2 | `verifying` | `codex-release-0.7.2-integration` | Green runtime preflight remains green while advisor completion is withheld |
| `elspeth-4b3ac84038` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live surfaces agree on the chosen completion-only policy: execution remains admitted; Save/review completion is refused |
| `elspeth-1033d97b6c` | P3 | `verifying` | `codex-release-0.7.2-integration` | Live Textract uses deployment-owned region and proves the bucket region before Textract |
| `elspeth-bc6d1c5d8d` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample merged telemetry and terminology evidence during final audit |
| `elspeth-cecfeca77b` | P2 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `a8c6f091d`; EARLY now asks only internal topology/field-contract questions and uses the checkpoint verdict system contract |

## Compose loop — `elspeth-7da4e52344`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-981130d70a` | P1 | `closed` | `codex-compose-required-controls` | Closed 2026-08-04 at operator sign-off (close_commit `release/0.7.2@a7f1709d4`, comment 2291): live freeform compose on deployed `67e2a1661` auto-wired `prompt_shield_auto_1` + `content_safety_auto_1` with pending `required_control_auto_wired` reviews, Run disabled until acknowledged, consent enumerated external effects, run `ba32b535` completed 3/3 rows; Landscape-verified |
| `elspeth-cd98ea9d82` | P1 | `verifying` | `codex-rca-compose-request-correlation` | Integrated through `39c6b14a7`; verify body/header/log equality in deployed CloudWatch events |
| `elspeth-f159d2394b` | P2 | `confirmed` | unassigned | Core seconds-per-logical-turn premise disproved; defer Terraform/operator timeout tuning as setup ergonomics |
| `elspeth-7bd0141bbe` | P2 | `verifying` | `codex-r3-rca-coordinator` | Reviewed `86453aa5c` + `3ec1036d6` reject invented terms before publication and retain the public registry through Anthropic/Bedrock adapters; verify live repair convergence |
| `elspeth-ecd8594b63` | P3 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `2906409e1` + `e1d31f104`; prove a live freeform fixed-sink build preserves every required field |
| `elspeth-ebba0b2171` | P3 | `closed` | `claude-grounded-custodian` | Closed 2026-08-04 at operator sign-off (close_commit `release/0.7.2@a7f1709d4`, comment 2294): CloudWatch namespace ELSPETH/Operator carries all four metrics with surface ∈ {guided,freeform} and closed status enums on deployed `67e2a1661`; failed guided request projected per the post-commit contract (status=failed, 8 provider calls, 77.7s); privacy sweep of dimensions and raw EMF clean |
| `elspeth-4d0d239886` | P3 | `closed` | `claude-grounded-custodian` | Closed 2026-08-04 at operator sign-off (close_commit `release/0.7.2@a7f1709d4`, comment 2293): single `Depends(_track_compose_inflight)` mount covers all three provider-planner call sites in `post_guided_respond` (review-verified); guided-surface metrics confirmed live in CloudWatch including the failed-request projection |
| `elspeth-73c7a4df36` | P1 | `closed` | `codex-compose-authoring-aids` | Delivery completed at `release/0.7.2@e1d31f104`; assignee retained as audit history |
| `elspeth-57232f6f3c` | P1 | `closed` | `codex-r3-rca-coordinator` | Reviewed chain integrated through `a913ccb63`; complete no-session audit recovery and empty post-P4 replay evidence verified with 79 release-local tests |
| `elspeth-4e6f2a59e4` | P2 | `closed` | `codex-guided-plan-terminal-progress` | Integrated through `859c2a642`; all guided-plan outcomes terminalize under exact generation and authoritative outcome primacy |

## Gate routing — `elspeth-e7ff15ac0b`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-fa63549a59` | P1 | `closed` | unassigned | Sample closure against retained operator prose and stable-subject edit binding |
| `elspeth-2ac590c79f` | P1 | `closed` | unassigned | Sample literal/`option_path` carry-forward through durable deferred intent |
| `elspeth-82d8bea477` | P1 | `closed` | unassigned | Sample threshold vocabulary and topology-stage disposition |
| `elspeth-fd32c3e6fd` | P1 | `verifying` | `codex-gate-proof-guided-validation` | Integrated through `39c7fc635`; live guided numeric-gate build must persist check 25 red and refuse execution before row processing |
| `elspeth-b326add5be` | P1 | `verifying` | `codex-gate-row-error-policy` | Integrated through `e3804416f`; live mixed good/bad CSV run must route one row per policy without aborting the run |
| `elspeth-dc07d517cf` | P2 | `closed` | unassigned | Sample clarification intent visibility/claimability at later stages |
| `elspeth-6795b3ae3a` | P2 | `closed` | `codex-r3-rca-coordinator` | Exhaustive Loomweave-plus-direct provider-call inventory recorded in Filigree comment `2240` at `23c1494a2` |
| `elspeth-dca1e81c58` | P1 | `closed` | `codex-r3-rca-coordinator` | Reviewed chain integrated as `e2bc8c5e3` + `45476f021`; selected node identity/type/plugin/hidden options fail closed and source/output wire corrections use the atomic form rewind |
| `elspeth-3526685369` | P1 | `closed` | `codex-guided-chat-revision-custody` | Integrated as `7afa62b6d` + `c4cade0f0`; form-directed source/sink custody and retained intent verified locally |
| `elspeth-eacfec09a6` | P1 | `open` | unassigned | Reopened 2026-08-04 (operator-authorized, comment 2295): the `elspeth-cd9af8e61d` reproduction disproved the D1 closure evidence — the advisor injection prescan force-FLAGged ordinary structural column lists and never scanned metadata/conditions/routes, so the closing verification could not have exercised what it claimed; re-verify against the prescan fix merged as release `27a402979` |
| `elspeth-4c699cb5d0` | P1 | `closed` | `codex-frontend-readiness-axes` | Integrated through `9016ce6a6`; exact release 148-test action/fanout slice passed; parent D5 remains live-verifying |
| `elspeth-d0d52e2fde` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample the real HTTP/lifecycle regression added with the proof fix |
| `elspeth-c4734bc69a` | P3 | `verifying` | `codex-r3-rca-coordinator` | Behavior `d78fbbed6` plus corpus `10179f2c1`; combined full-suite gate passed at `6844b684a`, while live catalog acceptance remains |
| `elspeth-aaa9e3f597` | P2 | `verifying` | `codex-release-0.7.2-integration` | Live guided path retains the decision heading and treats gates as topology, not plugins |
| `elspeth-1d97fc4b80` | P1 | `verifying` | `codex-r3-rca-coordinator` | Integrated through `97bad7128` + `21f55834d` + `6a59bcf77`; live amend/replace must preserve the active candidate, reviewed source/output authority, and exact retry mode |
| `elspeth-18b39eb829` | P2 | `closed` | `codex-r3-rca-coordinator` | Closed 2026-08-04 at operator sign-off (close_commit `release/0.7.2@a7f1709d4`, comment 2292): equivalent failed run `7e91065a` on deployed `67e2a1661` showed the failed node, `InvalidS3ObjectException`, Reason `submit_failed`, Cause `s3_object_unreadable`, the full access-scope hint, per-token path, and the quarantine artifact with sha256 + download — all without DB access; screenshot in the maintainer docs archive |

## Related core/demo-aware product and system work

These are part of the objective but are parentless in live Filigree; they are
not gate-routing children.

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-926ac02d3e` | P1 | `verifying` | `codex-s3-source-profiles` | Integrated through `131a5f584`; run one live operator-profiled S3 read and confirm redacted audit evidence plus endpoint denial |
| `elspeth-6801b71f71` | P2 | `closed` | `codex-r3-rca-coordinator` | Read-only live DB proof found complete structured failure and DIVERT provenance; UI gap split to `elspeth-18b39eb829` |
| `elspeth-0c73de77d5` | P2 | `verifying` | `codex-r3-rca-coordinator` | Integrated through `9d517df5e`; unknown and non-text codecs reject before publication and the refreshed production DAG corpus is exact; live/non-default codec acceptance remains |
| `elspeth-454892147c` | P1 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `937a8010b`; exhausted retries use named/discard `on_error` with final-attempt transform-error/DIVERT custody; live example acceptance remains |

## Provider-projection audit follow-up

These nine records were created by the exhaustive `6795b3ae3a` audit. They
are ordered with demo-blocking core behavior first; judge/installer-oriented
supportability remains last.

| ID | Priority | Live state | Surface | Next proof/action |
|---|---:|---|---|---|
| `elspeth-71b22759cc` | P1 | `verifying` | Freeform planner | Integrated as `368a55ef7`; live two-turn acceptance must preserve the first request's topology and threshold through a referential build |
| `elspeth-43208ece4c` | P1 | `verifying` | Guided proposal revision | Reviewed candidate `a5b5c9cee` passed 37,054 Python and 2,874 frontend full-suite tests, then integrated as release `8797fce2d`; live node/edge correction acceptance remains |
| `elspeth-1345480bd7` | P1 | `verifying` | Multi-query standard JSON | Amended candidate `f84a2fc37` passed two independent reviews, provider/trace parity, and fail-closed regressions; integrated as release `4f95c29a1`, with 37,126 release tests passed; live provider acceptance remains |
| `elspeth-8ef90e59cc` | P2 | `verifying` | Freeform schema progress | Integrated as `bb5a81213`; live request two must receive current-policy whole schema evidence or honestly report the gap unsatisfied |
| `elspeth-68a2ff10aa` | P2 | `verifying` | Planner repair escape hatch | Integrated as `8ba6f97a8`; live exhaustion must expose the final attempted candidate to the hatch without private finalizer associations |
| `elspeth-73c1af1562` | P2 | `verifying` | Guided advisory chat | Integrated as `07f966c8a`; live Step-3/4 explanations must distinguish materially different same-count graphs from exact frozen authority |
| `elspeth-32a2242510` | P1 | `verifying` | Guided advisory role authority | Integrated as `07f966c8a`; live adversarial capture must keep authored literals in delimited user content and absent from system authority |
| `elspeth-b14aa70771` | P2 | `verifying` | Run diagnostics | Integrated as `1213ab5b4`; live Explain must distinguish safe auth/throttle/Textract classifications while provider text remains redacted and audit hashes bind the exact messages |
| `elspeth-cecfeca77b` | P2 | `verifying` | EARLY advisor | Integrated at `a8c6f091d`; deployed prompt capture must confirm the internal-coherence-only rubric |
| `elspeth-532dfcb0c7` | P2 | `verifying` | Advisor sign-off contract honesty | Integrated as `ee341c6f4`; every completion/readiness/UI surface now describes the bounded CLEAN/FLAG result as evidence-scoped, with live acceptance remaining |
| `elspeth-826765af90` | P2 | `fixing` | Guided option constraints | Terminal candidate `ef73bfa70` abandoned non-integrable; claim reclaimed by `claude-grounded-custodian` 2026-08-04. Lane B of the grounded scope reset carries StatedOptionValueConstraint + prose grounding + projection un-redaction + SESSION_SCHEMA_EPOCH 43→44 (sessions.db wipe at landing); residual projection gaps stay on this ticket |
| `elspeth-d293c5d139` | P2 | `verifying` | Guided constraint admission | Lane A closed contradiction checker (ADR-033) integrated as release merge `50d57e07d` (candidate `b803879cb`); 41 post-merge canaries and the 37,407-test full gate at `9ab853d01` passed; EDIT-path `DeferredRequestUnchanged` named-refusal form recorded as an ADR-033 erratum; live guided contradiction-rejection acceptance remains |
| `elspeth-0502deb48c` | P1 | `triage` | Judge staging | Deferred: site-specific rationale is a release/supportability surface, after demo core |

## AWS installer — `elspeth-e54343d43b`

**Deferred lane:** this section covers installer-specific IAM, Terraform,
runbook, teardown, and shell mechanics. Do not defer Textract, S3 plugin,
runtime AWS integration, validation, or audit behavior merely because it uses
AWS; those are core/demo-aware product work.

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-b119ad49ff` | P1 | `open` | unassigned | Reconcile and commit the existing uncommitted policy fixes in their owning worktree |
| `elspeth-acc2ce713b` | P1 | `open` | unassigned | Complete one least-privilege Scenario A apply after policy integration |
| `elspeth-b6a6c8b027` | P1 | `triage` | unassigned | Reproduce/confirm `elasticfilesystem:PutLifecycleConfiguration` denial and policy seam |
| `elspeth-e8e590b723` | P2 | `triage` | unassigned | Reproduce/confirm the credential-containment `ecs:StopTask` path |
| `elspeth-daac6a06cb` | P2 | `open` | unassigned | Verify the Container Insights cleanup statement in the rendered and live policy |
| `elspeth-39ce9e5351` | P2 | `triage` | unassigned | Remove root-profile pinning from the operator surface and prove least-privilege selection |
| `elspeth-73b06f178f` | P2 | `triage` | unassigned | Make the state-bucket census guard fail on a deliberately non-empty census |
| `elspeth-1580875c7a` | P2 | `triage` | unassigned | Prove teardown reinitializes and asserts the intended backend profile |
| `elspeth-36864530f8` | P2 | `triage` | unassigned | Compare rendered template with the live default policy version before apply |
| `elspeth-122fc19e6f` | P2 | `proposed` | unassigned | Approve/design a plan-JSON principal oracle before implementation |
| `elspeth-c877039a4b` | P2 | `proposed` | unassigned | Approve/design an IAM action-vocabulary gate against AWS authority data |
| `elspeth-36c92f6418` | P3 | `proposed` | unassigned | Approve/design module-action coverage and condition-shape checks |
| `elspeth-2a9d2b3073` | P3 | `triage` | unassigned | Confirm and remove the three inert phantom S3 action strings |
| `elspeth-083f836731` | P3 | `triage` | unassigned | Exercise update/re-apply paths and capture each missing grant separately |

## Directly implicated pre-existing work

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-bcc6bdac99` | P1 | `verifying` | `codex-r3-rca-coordinator` | Local fixes/tests verified; require the exact shield → llm → safety → mapper → gate → two sinks correction to produce a new wire review live |
| `elspeth-5904b1683a` | P1 | `verifying` | `codex-aws-cold-install-coordinator` | Do not close from advisor evidence; exercise the canonical planner-repair prompt live |
| `elspeth-a229c247a1` | P2 | `triage` | unassigned | Reproduce Container Insights orphan lifecycle with the integrated installer policy |
| `elspeth-9f7d336e1c` | P1 | `fixing` | `operator` | Preserve operator custody; verify a published candidate image boots after the signing/release gate |
| `elspeth-5c0c09db31` | P1 | `closed` | `codex-r2-f17` | Objective text is stale: sample the closed R2-F17 evidence instead of reopening by assumption |

## Deferred supportability after demo-aware core

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-01c627b420` | P2 | `open` | unassigned | Make all 16 `docs-archive/acceptance/` artifacts clone-visible or migrate the 89 affected issue references after core integration stabilizes |

## AWS operation ledger

| Time (AEST) | Actor | Mode | Scope/result | Mutation |
|---|---|---|---|---|
| 2026-08-03 16:13–16:16 | `/root` | Read-only inventory | Confirmed account `559849758286`; steady ECS service on task definition `a-fa1b99c60192978b10f7-web:7`; deployed candidate SHA `4baf1109`; target healthy | None |
| 2026-08-03 16:16 | `/root` | Diagnostic HTTPS probe | `/api/health` and `/api/ready` returned 200/ready only with certificate verification disabled; ordinary TLS verification rejected the self-signed certificate | None; diagnostic result is not acceptance evidence |
| 2026-08-03 16:19 | `/root` | Read-only CloudWatch Logs Insights | Old deployed baseline had 411 web log records in the bounded window: 26 contained `composer.`, zero contained `request_id`, and zero contained `advisor` | None; query IDs retained in the AWS audit service |
| 2026-08-03 16:34–16:38 | `/root` | ECS Exec plus read-only PostgreSQL transaction | Queried archived session `6e7f5281-7b75-4757-ac7e-498e6e6bbc88` / run `fd445b25-6d3a-47b5-a30a-8aa44fd28c7d`; Landscape contains `InvalidS3ObjectException`, actionable access hint, failed node, quarantine destination, and DIVERT routing reason | No AWS resource or row mutation; transaction explicitly read-only; transient Exec session only |

No subagent has AWS delegation. `/root` retains sole custody of any future AWS
mutation and must record the exact resource and reason here before handoff.

## Checkpoint log

Append entries; do not rewrite history.

| Time (AEST) | Checkpoint | Evidence/result |
|---|---|---|
| 2026-08-03 16:09 | Scope reconciled | Explicit objective contains 44 non-parent IDs plus four parent workstreams; live Filigree state sampled |
| 2026-08-03 16:10 | Durable tracking created | Filigree coordination task `elspeth-f944ec61f7` claimed by `codex-r3-rca-coordinator` |
| 2026-08-03 16:11 | Text tracker committed | `4881358c2` on `codex/r3-rca-remediation-tracker` |
| 2026-08-03 16:12 | Demo-aware audit wave dispatched | Seven read-only agents assigned with no code, tracker, index, or AWS mutation authority |
| 2026-08-03 16:14 | Structural index refreshed | Loomweave run `d63a80e8-8392-4aab-9b50-5eff9d63c298` completed; index is fresh at exact release SHA `e326dab0c` |
| 2026-08-03 16:17 | Tracker independently reviewed | All 44 item cells matched live Filigree; structural/count/custody corrections incorporated |
| 2026-08-03 16:23 | Implicated legacy audit complete | 17 focused tests passed; R2-F17 stays closed, F14 stays verifying, and R2-F16 advanced from stale triage to verifying with exact live topology still required |
| 2026-08-03 16:25 | Advisor current-HEAD audit complete | 330 advisor/composer, 4 readiness, 1 shareable-review, 125 frontend, 454 Textract/profile/config, and 72 AWS contract tests passed; parent advanced to verifying |
| 2026-08-03 16:27 | Gate current-HEAD audit complete | Closed fixes held; `b326add5be` and `c4734bc69a` confirmed; new P1 `1d97fc4b80` filed for unchanged-node custody and all three implementation lanes assigned |
| 2026-08-03 16:29 | Compose current-HEAD audit complete | 181 characterization tests passed while all six children remained actionable; non-overlapping aids lane isolated before shared `service.py` work |
| 2026-08-03 16:31 | S3/Textract audit complete | 64 focused tests passed; categorical Web S3 refusal confirmed and operator-profiled implementation assigned; installer wiring remains deferred |
| 2026-08-03 16:38 | Landscape audit question settled | Direct read-only DB proof closed `elspeth-6801b71f71`; UI discoverability bug `elspeth-18b39eb829` filed and assigned independently |
| 2026-08-03 16:40 | Compose implementation started | Parent advanced to fixing; delivery task `elspeth-73c7a4df36` assigned with exclusive prompt/aids/field-mapper custody |
| 2026-08-03 16:42 | CSV audit-honesty fix integrated | Reviewed `1d85fbb758`, cherry-picked as `d78fbbed6`; 100 backend and 114 frontend focused tests plus repository gates passed; item advanced to verifying |
| 2026-08-03 16:44 | CSV corpus obligation discovered | Exact maintained `linear:happy-path` production case failed only at durable-projection equality because the manifest pins the old honest plugin hash; item returned to fixing and ownership expanded narrowly to the corpus oracle |
| 2026-08-03 16:44 | Ignored evidence risk tracked | Filed deferred supportability task `elspeth-01c627b420`; read-only audit found 89 acceptance-related issues citing paths absent from fresh clones |
| 2026-08-03 16:51 | CSV corpus obligation closed | Reviewed and integrated oracle commit `9197657ae` as `10179f2c1`; corpus contract 435 and full production path 158 passed; coordinator reran the formerly failing case plus CSV metadata (7 passed) |
| 2026-08-03 16:52 | Guided custody scope completed | Step-3 proposal revisions now bind against the authoritative proposal candidate as well as Step-4 wire corrections; independent review and repository gates remain |
| 2026-08-03 16:56 | Run-diagnostics UI integrated | Reviewed implementation `940c8b1c2` and boundary hardening `63a691d5c`, integrated as `9130e1209` + `0928c6ac6`; coordinator reran 26 drawer tests on the release branch |
| 2026-08-03 17:06 | Compose authoring aids integrated | Reviewed both handoffs and rejected the first stale plugin identity; integrated behavior as `2906409e1` and honest FieldMapper source identity as `e1d31f104`; 176 integrated tests passed; delivery task closed and `ecd8594b63` advanced to verifying |
| 2026-08-03 17:08 | Guided node custody integrated | Independent review approved `9b8882cfe`; integrated as `b424c08c4`; five new regressions passed on release and `1d97fc4b80` advanced to verifying |
| 2026-08-03 17:11 | Required-controls lane assigned | `981130d70a` confirmed fixing at exact release baseline `b424c08c4`; dedicated worktree/file custody excludes the active S3 tools surface and requires localized handoff before the combined full-suite gate |
| 2026-08-03 17:18 | Structural index refreshed after core integrations | Loomweave run `fd361568-996f-4d8d-a52d-9d1bd01edb53` completed; index is fresh at exact release SHA `ca375291c` with 70,776 entities and 141,110 edges |
| 2026-08-03 17:24 | Gate row-error localized handoff held | Candidate `48a6f33fb` passed 1,889 affected tests and repository gates, but independent review found nine P1/P2 gaps; Filigree comment `2163` records Changes requested and the issue remains fixing |
| 2026-08-03 17:26 | Required-controls RED established | Both explicit approval and auto-commit publish valid Textract-to-LLM proposals without the required prompt-shield/content-safety nodes; production correction is in progress |
| 2026-08-03 17:33 | S3 source localized handoff held | Candidate `190292009` passed 1,895 targeted tests and repository gates, but independent review found four Important gaps; Filigree comments `2164`/`2165` record the verdict and stale-turn nuance; the issue remains fixing |
| 2026-08-03 17:35 | Provider-projection audit partitioned | Original D1 and typed gate carry-forward are fixed, but four P1 residuals were reproduced; created child tasks `dca1e81c58`, `3526685369`, and `eacfec09a6`, started the non-overlapping guided-chat lane, and held `guided.py`/`service.py` lanes for current owners |
| 2026-08-03 17:53 | Compose RCA seams reclassified | The fixed turn-count premise and durable provider-call audit were separated from real core gaps; created advisor-deadline task `57232f6f3c` and guided terminal-progress task `4e6f2a59e4`; installer/log-format tuning remains deferred |
| 2026-08-03 17:59 | S3 second handoff held on security | `feb90a75e` passes focused policy/custody/key tests but re-exposes private bucket/prefix data in provider-call audit, validation evidence, and quarantine rows; third surgical follow-up assigned |
| 2026-08-03 18:01 | Frontend readiness handoff held | `30bbaeb8d` plus `ff9305b89` align ordinary action surfaces, but fresh review found pending fanout confirmation bypasses a live readiness recheck; Filigree comment `2184` supersedes provisional approval |
| 2026-08-03 18:04 | Request-correlation localized handoff | Candidate `b14ae689d` passed 249 affected tests and repository gates without a full suite; independent review now checks that a pre-populated envelope cannot split body, header, and log identities |
| 2026-08-03 18:06 | Gate row-error second localized handoff | `48a6f33fb` + `5bcc7fe60` passed the reported focused backend/frontend/docs slices, mypy, lints, Wardline, and hooks; full suite remains intentionally deferred while a fresh independent review checks all nine prior blockers |
| 2026-08-03 18:08 | Request-correlation handoff rejected | Independent review reproduced split body/header/log IDs and telemetry exceptions replacing the primary 422; Filigree comment `2188` records Changes requested and a surgical follow-up is active |
| 2026-08-03 18:09 | S3 third security handoff | `263e3027c` adds nominal server-owned profiled audit authority and reported 633 affected tests plus repository gates; comprehensive runtime-evidence re-review active before integration |
| 2026-08-03 18:10 | Frontend fanout follow-up | `cf959e11f` adds a live fail-closed execution readiness check at the pending fanout continuation; 131 focused action tests plus type/lint/Wardline evidence passed and re-review is active |
| 2026-08-03 18:14 | Frontend readiness integrated | Independent re-review approved with no findings; integrated as `127267811` + `3b173a4e0` + `9016ce6a6`; coordinator reran the exact four-suite slice on release (148 passed) and closed `4c699cb5d0` while parent D5 remains verifying |
| 2026-08-03 18:15 | Structural index refreshed | Loomweave run `0d84720a-d7ca-40d5-8d75-75065aea887f` completed fresh at exact `9016ce6a6` with 70,784 entities and 141,110 edges |
| 2026-08-03 18:16 | Frontend temporal worklist sampled | Warpline enumerated the exact 13 changed frontend files, but its edge snapshot is 429 commits stale/partial; direct review and 148 release tests remain authoritative, and one batch recapture is deferred until the core integration wave stabilizes |
| 2026-08-03 18:19 | Gate second handoff held again | Fresh review confirmed most prior gaps closed but reproduced telemetry replacing terminalization and empty Composer `on_error` collapsing to omission; third follow-up assigned, Filigree comment `2192` |
| 2026-08-03 18:21 | Guided progress and request correlation held | `9672163d8` failed cleanup/cross-surface/post-settlement/replay lifecycle review; `50717d0c6` fixed original correlation blockers but swallowed Tier-1 logging errors; comments `2193`/`2194` record both holds |
| 2026-08-03 18:23 | S3 and guided-chat handoffs held | S3 `263e3027c` needs typed audit-safe state plus authenticated binding and a shared option contract; guided chat `12bde9290` needs one non-contradictory form-directed stale-pair response; comments `2195`/`2196` record both |
| 2026-08-03 18:28 | Request correlation integrated | Third review approved Tier-1 primacy and authoritative identity; integrated as `8101f1f08` + `1537c7885` + `39c6b14a7`; coordinator reran 32 exact release-handler regressions and advanced the bug to verifying |
| 2026-08-03 18:34 | Guided chat revision custody integrated | Independent re-review approved `12bde9290` + `d7c60411c`; integrated as `7afa62b6d` + `c4cade0f0`; 10 exact release regressions passed, task closed, and `guided_chat_atomic.py` released to terminal progress |
| 2026-08-03 18:36 | Gate row-error policy integrated | Reviewed four-commit chain integrated as `a80ea0259` + `947c242ac` + `65acd35fb` + `e3804416f`; coordinator reran 72 backend and 56 frontend release-checkout tests and advanced the bug to verifying |
| 2026-08-03 18:38 | Required-controls candidate held | External review of `2f68a281f` reproduced consent after-the-fact control insertion for incremental proposals and corrupted `affected_nodes`; candidate remains isolated and a surgical follow-up is active |
| 2026-08-03 18:42 | Structural index refreshed | Loomweave run `0021908e-959a-4444-861e-05b034d71f99` completed fresh at exact release `0a27bdf2e` with 70,872 entities and 138,369 edges |
| 2026-08-03 18:44 | Advisor D4 current-head audit | The older `4baf`/`e4a8` RCA snapshot was stale: release already selects a green preflight independently of advisor reason; four exact regressions passed and no code change was opened |
| 2026-08-03 18:48 | Advisor D1 current-head audit | All five prior projection-review blockers are fixed; generic field/collision values, shared injection scanning, withheld-value rubric, atomic field-count bounds, and exact sink/triple fixtures passed 12 focused regressions |
| 2026-08-03 18:49 | Guided terminal-progress handoff held | Independent real-route review found no-winner fence loss raises before terminal publication and leaves `calling_model` active; comment `2207` records the remaining P1 |
| 2026-08-03 18:50 | S3 fourth handoff held | Coordinator proved the excluded endpoint test passes on release, and review found missing audit-safe carrier bypasses profile binding; comment `2208` records both candidate regressions |
| 2026-08-03 18:51 | Part 5 item 1 narrowed again | Constraint carry-forward, exact one-sink routing, and check-25 execution admission are fixed, but guided confirmation still persists green after bare 24-check validation; `fd32c3e6fd` returned to fixing and a narrow lane was assigned |
| 2026-08-03 19:09 | Guided terminal progress integrated | Four reviewed commits integrated through `859c2a642`; coordinator reran 87 guided-plan/progress plus 10 guided-chat custody tests and closed `4e6f2a59e4` |
| 2026-08-03 19:12 | Operator-profiled S3 integrated | Five reviewed commits integrated through `131a5f584`; exact binding, endpoint, carrier, redaction, and raw-CLI review approved and 629 release-checkout tests passed; issue advanced to verifying for live AWS acceptance |
| 2026-08-03 19:15 | Advisor residuals re-audited | D2 convergence and D5 execution/completion policy hold, while real-loop reproduction confirmed successful FLAG-to-repair-to-CLEAN prose leakage and four unbounded END-checkpoint calls; D3 returned to fixing and deadline task started |
| 2026-08-03 19:19 | Guided proof localized handoff held | Candidate `b4fb70270` passed reported focused gates but review reproduced mixed-carrier private-path disclosure and a wrong-session/path blob sentinel collapsing to green proof abstention; stronger real ExecutionService regressions assigned |
| 2026-08-03 19:23 | Required-controls blob proposal seam GREEN | Exact named one-source and two-source explicit proposals now retain canonical private state and controls while public/audit projections omit blob IDs and paths; lower-level tamper/nonpublication coverage remains before review |
| 2026-08-03 19:28 | Structural index refreshed | Loomweave run `a86ff356-bc5d-4b0e-8f54-5ce99f5204a1` completed fresh at exact release `d3fb24c89` with 71,034 entities and 137,904 edges |
| 2026-08-03 19:38 | Graceful pause checkpoint | All reviewed completed fixes remain durable on release; required-controls and advisor lanes stopped safely as staged/dirty uncommitted work; guided chain stayed isolated after final review found exited-to-freeform history still acting as current blob authority; durable handover brief added |
| 2026-08-03 20:00 | Required controls integrated | Four-commit reviewed chain integrated through `32cec3d8a`; coordinator reran 241 release proposal/owned-authority/lifecycle tests and advanced `981130d70a` to verifying |
| 2026-08-03 20:07 | Guided proof custody integrated | Three-commit reviewed chain integrated through `39c7fc635`; coordinator reran 512 release execution/redaction/export/replay/guided tests and advanced `fd32c3e6fd` to verifying |
| 2026-08-03 20:09 | Next core lanes sequenced | Advisor `733475375` held on post-attempt deadline classification primacy; selected-node/wire rewind `dca1e81c58` started after guided custody released; concurrent deploy-owned commits `6b4611f27` and `b4370bd16` were preserved without root AWS mutation |
| 2026-08-03 20:34 | Advisor surface and deadline integrated | Independently approved three-commit chain integrated through `4f70a3619`; 340 release-checkout advisor/service tests passed; D3 advanced to verifying while truthful `tool_batch.py` timeout counters remain active under `57232f6f3c` |
| 2026-08-03 20:49 | Selected-node custody integrated | Reviewed `fad962371` + `6db5ca9b8` integrated as `e2bc8c5e3` + `45476f021`; 19 exact release canaries passed after reviewer evidence of 32 backend and 220 frontend; task `dca1e81c58` closed |
| 2026-08-03 20:50 | Compose registry boundary active | Reclaimed `7bd0141bbe` to the coordinator and opened a clean `4f70a3619`-based worktree; RED proves unknown `pipeline_decision` terms currently publish on all four node writers and the tool schema accepts them; localized fail-closed repair is under test |
| 2026-08-03 21:13 | Advisor timeout recovery integrated | Independently approved three-commit chain integrated through `a913ccb63`; 79 release-local advisor/persistence tests passed and `57232f6f3c` closed with exact recovery/non-replay evidence |
| 2026-08-03 21:15 | Compose registry boundary integrated | Reviewed `e6b44424a` + provider follow-up `f82b5fee6` integrated as `86453aa5c` + `3ec1036d6`; 376 release-local tests passed, supported provider transforms retain exactly the three public terms, and `7bd0141bbe` advanced to verifying |
| 2026-08-03 21:59 | Full-suite regression candidate approved | Two independent reviews approved `07f86af34` after one stale semantic-failure expectation was corrected; 499 boundary/authority tests, 626 service/guided tests, source-slice mypy, lints, and Wardline passed |
| 2026-08-03 22:12 | Combined CI-equivalent gate passed and integrated | Exact candidate `07f86af34` completed `pytest tests/` with 36,982 passed, 66 skipped, and one expected trust-tier xfail in 12m15s; cherry-picked byte-identically as release `6844b684a`, then 375 release-local canaries passed |
| 2026-08-03 22:16 | Structural index refreshed on durable release | Loomweave run `2881b4c0-e280-409d-9e52-93ff39b55b78` completed fresh at exact `23c1494a2` with 71,179 entities and 136,739 edges |
| 2026-08-03 22:37 | Exhaustive provider-projection audit closed | Five independent read-only audits reconciled Loomweave with every production provider-call family; Filigree comment `2240` records thirteen residual links, nine new implementation issues, known-good boundaries, and no agent AWS mutation |
| 2026-08-03 22:48 | Advisor localized candidate approved | Initial RED proved five evidence-scope failures; independent review then found the higher-priority shared “stuck composer / one hint” system contract on deterministic checkpoints. Trigger-specific wire instructions, visible-evidence scoping, nine-field omission evidence, manual-hint parity, and EARLY/END coverage passed 142 advisor plus 252 adjacent tests; reviewer approved with no findings |
| 2026-08-03 23:04 | Advisor full gate passed and integrated | Exact candidate `ffe899fd4` completed `pytest tests/` with 36,986 passed, 66 skipped, and one expected trust-tier xfail in 12m18s; cherry-picked byte-identically as release `a8c6f091d`, exact release advisor rerun 142 passed, and D1/EARLY/parent advanced to verifying |
| 2026-08-04 00:07 | Empty-state freeform history integrated | Exact candidate `c2d52e552` completed `pytest tests/` with 37,039 passed, 66 skipped, and one expected trust-tier xfail in 12m04s; cherry-picked as release `368a55ef7`, 32 release-local history regressions passed, and `71b22759cc` advanced to verifying for live two-turn acceptance |
| 2026-08-04 00:34 | Guided proposal-feedback localized candidate | Node/edge revision now collects and atomically binds exact correction feedback, source/output keeps the reviewed form rewind, retry identity includes feedback, and unchanged selected targets cannot supersede the predecessor; 1,384 Python plus 309 frontend tests, static/hooks, Check Contracts, and Wardline passed; full suite held for focused review |
| 2026-08-04 01:01 | Guided feedback review blockers repaired | Initial review found unchanged-target rejection was post-loop/terminal and TS still represented target-only node/edge actions. Single replacement commit `5cbd870a1` moves the registered objection into post-validation candidate acceptance before custody, preserves normal repair/repeat/hatch behavior, closes the TS unions, and adds positive node custody; 1,758 Python, 226 service/shared-planner, and 309 frontend tests passed before follow-up review |
| 2026-08-04 01:21 | Guided proposal feedback integrated | Follow-up review approved exact `a5b5c9cee` with no P1/P2 findings; 37,054 Python and 2,874 frontend full-suite tests passed, the candidate was cherry-picked as release `8797fce2d`, 405 Python plus 292 frontend release regressions and TypeScript passed, and `43208ece4c` advanced to verifying |
| 2026-08-04 01:22 | Multi-query standard JSON candidate under review | Read-only RCA proved standard mode hid its enforced field/type/enum contract and Langfuse reconstructed the same incomplete message. Narrow candidate `9f6306257` passed 126 owned-module and 238 adjacent tests plus static gates and Wardline; `1345480bd7` advanced to fixing and independent review started |
| 2026-08-04 01:31 | Multi-query review blocker repaired | First review correctly found that an empty system prompt was omitted from provider messages but retained in Langfuse reconstruction. Amended `f84a2fc37` normalized tracer custody to exact provider bytes; two independent reviews approved success plus all three error branches, pooled/sequential execution, canonical escaping, and unchanged structured/no-output behavior |
| 2026-08-04 01:52 | Multi-query release gate passed | A coordinator workdir error cherry-picked the reviewed candidate into release before the intended disposable combined gate. The single two-file commit remained isolated as `4f95c29a1`; the CI-equivalent release suite then passed 37,126 with 27 skips and one expected xfail, and `1345480bd7` advanced to verifying |
| 2026-08-04 01:56 | Structural index refreshed | Loomweave incremental run `7254b3ce-4155-4fb6-a390-ff10a60518c0` completed fresh at exact release `b8ce2e46f` with 71,239 entities and 136,472 edges |
| 2026-08-04 02:04 | Advisor residual separated from D1 | Current-HEAD re-audit proved the impossible false-FLAG obligation remains fixed while evidence-scoped CLEAN is still advertised as whole-pipeline sign-off; new P2 `532dfcb0c7` records that contract-honesty choice without reopening D1 |
| 2026-08-04 02:18 | Guided custody and schema evidence in TDD | Guided amend/replace now plans from the active proposal, binds exact instruction lineage and retry mode, and is closing route-side hidden-state custody regressions before commit; schema-progress RCA completed and `8ef90e59cc` moved to fixing with isolated current-policy evidence work. No AWS or release mutation occurred |
| 2026-08-04 02:34 | Next provider-evidence lanes opened | Escape-hatch RCA proved the final rejected candidate is absent and opened isolated `68a2ff10aa` implementation. Guided advisory RCA proved same-count gates collapse to identical contexts and split P1 `32a2242510` because typed authored literals gain system-role authority; combined graph/authority repair now has disjoint custody. No AWS mutation occurred |
| 2026-08-04 03:10 | Four localized candidates approved | Guided amend/replace `db4c8aaa3`, schema replay `9e84426a0`, escape hatch `ee5dfcc761`, and guided advisory graph/role authority `5c87828ef0` passed independent P1/P2 review plus their localized/static/security gates; none had yet mutated release |
| 2026-08-04 03:19 | Full gate caught stale revision fixture | First combined run reached 37,122 passed but correctly rejected two shared deterministic-planner no-op successors; production remained fail closed. Fixture correction `227c93ee2` made the planner emit a real retained-authority transform, and the full two-file guided integration neighborhood passed 288 tests |
| 2026-08-04 03:35 | Combined CI-equivalent gate passed | Exact combined head `25b77901f` completed `pytest tests/` with 37,169 passed, 66 skipped, and one expected trust-tier xfail in 13m19s after 908 Python, 333 frontend, TypeScript, Ruff, ELSPETH lints, and Wardline canaries |
| 2026-08-04 03:37 | Five demo-critical fixes integrated | Six reviewed commits were cherry-picked durably through release `07f966c8a`; exact release rerun passed 908 Python plus 333 frontend and TypeScript. `1d97fc4b80`, `8ef90e59cc`, `68a2ff10aa`, `73c1af1562`, and `32a2242510` advanced to verifying for live acceptance |
| 2026-08-04 03:38 | Run-diagnostics implementation lane active | RCA proved only advisory Explain collapses equal-length auth/throttle evidence; Filigree comment `2254` records the closed-classification boundary. Independent review approved the two-file candidate after 39 focused and 60 direct/route tests; no AWS mutation occurred |
| 2026-08-04 03:40 | Structural index refreshed | Loomweave run `dcb0c3ec-4fc1-48e8-af55-6a5a2b16cfd9` completed fresh at release `07f966c8a` with 71,412 entities and 136,665 edges |
| 2026-08-04 03:53 | Run-diagnostics full gate passed | Exact combined candidate `3150164c3` completed `pytest tests/` with 37,208 passed, 66 skipped, and one expected trust-tier xfail in 12m53s after 69 exact diagnostics canaries |
| 2026-08-04 03:54 | Run-diagnostics classification integrated | Reviewed candidate `b36333cce1` integrated as release `1213ab5b4`; exact release diagnostics rerun passed 69 tests and `b14aa70771` advanced to verifying for live Explain/audit-hash acceptance |
| 2026-08-04 03:54 | Advisor contract-honesty lane opened | Two valid delimiter/encoding graph pairs produced identical advisor messages despite distinct graph fingerprints. `532dfcb0c7` advanced to fixing for wording-only evidence scoping; separate P2 `0c73de77d5` records JSON codec validation deferred to first write |
| 2026-08-04 03:55 | Structural index refreshed after diagnostics | Loomweave run `f96057b0-f71a-4ff0-9486-ff549914c09e` completed fresh at exact release `1213ab5b4` with 71,427 entities and 136,684 edges |
| 2026-08-04 07:18 | Advisor and JSON combined full gate passed | Exact combined head `9aacd4768` completed `pytest tests/` with 37,233 passed, 66 skipped, and one expected trust-tier xfail in 13m32s; a prior isolated scheduler-contention start-starvation flake passed three immediate reproductions before the clean rerun |
| 2026-08-04 07:24 | Advisor and JSON fixes integrated | Reviewed commits were cherry-picked through release `9d517df5e`; exact release canaries passed 1,189 backend tests, six frontend regressions, and TypeScript. `532dfcb0c7` and `0c73de77d5` advanced to verifying |
| 2026-08-04 07:27 | Retry-exhaustion routing integrated | Independently approved `44c0b1a38` was cherry-picked as release `937a8010b`; 325 exact release tests plus mypy, Ruff, and non-inert Wardline passed, and `454892147c` advanced to verifying |
| 2026-08-04 07:52 | Retry release full gate passed | Exact release `d8c8ee7b8` completed `pytest tests/` with 37,305 passed, 27 skipped, and one expected trust-tier xfail in 23m53s; Loomweave run `eb271254-4297-4b53-ab9b-f28e94838f55` is fresh at the same commit |
| 2026-08-04 07:52 | Remaining demo-core work reconciled | Read-only current-state audit found no unclaimed local-code leaf: grounded constraints and bounded provider telemetry are the only active local implementations; thirty other core leaves require bundled live acceptance, and installer/setup/signing lanes remain deferred or externally owned |
| 2026-08-04 08:02 | Guided/respond telemetry split tracked | Exact current-source probe found three provider-planner call sites without `_track_compose_inflight`; SEI-bound P3 `4d0d239886` was filed, made a blocker of `ebba0b2171`, and assigned in a disjoint route-only worktree |
| 2026-08-04 08:07 | Final localized candidates held by review | Grounded candidate `89a43e95e` exposed false accepted lifecycle/custody, cross-intent/alias contradictions, retraction ambiguity, durable schema drift, and tool/decoder mismatch; telemetry candidate `98c0ff961` exposed a commit-without-projection cancellation race. Both remain fixing and isolated; no full suite or release integration started |
| 2026-08-04 08:21 | Telemetry amendments independently approved | Provider projection `401569773` closes commit/rollback/cancellation settlement and guided/respond `e8cfe0d5e` mounts the shared lifecycle dependency. The exact parented pair passed 359 combined localized tests plus static/security gates and was handed off before any full-suite or release integration; grounded amendments remain active |
| 2026-08-04 09:00 | Grounded amendment remains held on custody ordering | Interim `5511eb5f5` is not an integration candidate. Review proved final authority must follow raw validation/coverage → pure deterministic custody preparation → custody-safe validation/coverage/acceptance → terminal settlement → blob publication; publication-before-revalidation can orphan a ready blob, and the safe validator must bind only the exact prepared session/arguments/blob/metadata. Combined telemetry interaction review found no P1–P3 defect, but both telemetry commits remain held for parallel implementation review and no full suite has started |
| 2026-08-04 09:41 | Grounded moving-tree review remains unresolved | HEAD remains interim `5511eb5f5` with 18 modified files and one untracked test; no frozen candidate. Provisional review still reports unrelated catalog identity acceptance, cross-kind validation/retention and durable edit-replay mismatches, ambiguous scalar grounding, incompatible same-plugin subject collapse, signed-zero and route/future-sink cardinality mismatches, and tool-schema/decoder gaps. Shared finalizer verification now covers direct callers and 11 focused custody tests passed, while duplicate off-loop verification and a Ruff import-order failure remain. Re-review all findings on one frozen exact commit; no full suite or release integration has started |
| 2026-08-04 13:20 | Grounded candidate `ef73bfa70` withdrawn terminal | A unanimous 7-lens SME panel (security, static-analysis rule design, solution scope, defect triage, test suite, systems leverage, cross-lane recurrence) judged the terminal candidate non-integrable: an executed probe showed 13 same-plugin subjects under at_most 13 (trivially satisfiable; release accepts) rejected `constraint_proof_budget_exceeded` at the `_MAX_ALIAS_PROFILE_SUBJECTS+1` cliff with the proof budget consumed by accumulated session history, and rejections bypass the R2-F15 retention net; all prior candidates (`17ab6e88e`, interim `5511eb5f5`, `42a0b158a`) are likewise abandoned |
| 2026-08-04 13:22 | Grounded custody transferred | Filigree claims `elspeth-d293c5d139` and `elspeth-826765af90` reclaimed atomically to `claude-grounded-custodian` (statuses unchanged at `fixing`; Codex frozen on usage limit until 2026-08-08); tracker mutation custody transferred to the same custodian on the main checkout; preservation commit `a5d7fc0e7` on the branch holds the orphaned exact-type/tz-aware `created_at` custody verification for lane C |
| 2026-08-04 13:24 | Grounded scope reset is plan of record | ADR-first (admission-contract ADR in draft); lane A closed contradiction checker (`elspeth-d293c5d139`), lane B stated-option grounding/projection with SESSION_SCHEMA_EPOCH 43→44 (`elspeth-826765af90`), lane C custody/prepublication hardening (`elspeth-e75dc03d3e`); salvage/declination issues filed: `elspeth-f18f7ff84f` (BlobError laundering), `elspeth-33233bc372` (custody-drain CancelledError), `elspeth-c07ab8cadb` (int/float grounding parity), `elspeth-7faac0267b` (planner_intent raw-message leak), `elspeth-3ad24f10eb` (multi-subject satisfiability declination) |
| 2026-08-04 13:44 | Telemetry pair integrated (first integration wave of the new custody) | Reviewed pair merged `--no-ff` as `e8026fc7f` (provider projection `401569773`, `elspeth-ebba0b2171`) then `67e2a1661` (guided/respond lifecycle mount `e8cfe0d5e`, `elspeth-4d0d239886`), both conflict-free; post-merge canary on the merged tip passed 211/211 across the six affected test modules with clean Ruff on all merged sources; both tickets reclaimed to `claude-grounded-custodian` and advanced to `verifying` with live CloudWatch acceptance remaining; out-of-scope follow-up filed as `elspeth-e9724daf79` (run-diagnostics durable provider calls not projected; request-status dimension docs note) |
| 2026-08-04 16:02 | Wave-2 integration: three reviewed-CLEAR fixes merged and reconciled | Three conflict-free `--no-ff` merges: `50d57e07d` (ADR-033 closed contradiction checker `b803879cb`, `elspeth-d293c5d139`), `27a402979` (advisor prescan split `4611b7491` + repair-readiness honesty `c3ef6553a`, `elspeth-cd9af8e61d`/`elspeth-88592f5be7`), `bf55d3bd1` (fail-closed exit-to-freeform source-proof admission `192c32ace`, `elspeth-3b45cdb41e`). Post-merge canaries: 41 (closed-conjunction 35 + intent-management 6), advisor 163 + 224, proof 68 + 234; Ruff clean on all eight merged sources. First full-suite run (37,405 passed) surfaced exactly two failures, both pre-existing baseline hygiene not attributable to the merged candidates: the `449c93397` ADR-033 archive-path citation and the `201931108` redaction reason-text edit without snapshot regeneration; both fixed at `9ab853d01` (citation rephrased, snapshot regenerated byte-idempotent). Clean CI-equivalent rerun at the exact integrated tip `9ab853d01`: 37,407 passed, 27 skipped, 1 expected trust-tier xfail in 12m52s. All four bugs advanced to verifying with fix_verification recorded; epic `elspeth-c1b8b26d32` commented (workflow holds it in_progress); reopen of `elspeth-eacfec09a6` recommended at operator sign-off on the prescan evidence. Review residue filed under `wave2-review-residue`: `elspeth-8e44675d36` (P3 colon-delimiter injection-phrase loss), `elspeth-e01f75b034` (P3 blob-read TOCTOU custody), `elspeth-c3727c7732` (P4 unscanned control-flow renders), `elspeth-1947b6da30` (P4 non-sentinel divergence degradation), `elspeth-c6ababad46` (P4 soundness-oracle atom extension). ADR-033 EDIT-path erratum committed as `2bf86c1a3`. Lane worktrees/branches for the three integrated candidates removed; `grounded-option-constraints` retained for lanes B/C |
| 2026-08-04 16:12 | Operator sign-off closes, eacfec09a6 reopen, endpoint and RDS budget of record | Operator (John) authorized and `claude-grounded-custodian` executed four closes on 2026-08-04 live acceptance against deployed release `67e2a1661`: `elspeth-981130d70a` (compose-loop required-control auto-wiring, comment 2291), `elspeth-18b39eb829` (run-diagnostics drawer, comment 2292), `elspeth-ebba0b2171` (Composer provider-telemetry projection, comment 2294), and `elspeth-4d0d239886` (guided/respond request-telemetry mount, comment 2293), each with close_commit `release/0.7.2@a7f1709d4`. Reopened `elspeth-eacfec09a6` (comment 2295): the `elspeth-cd9af8e61d` reproduction disproved its D1 closure evidence (prescan force-FLAGged structural column lists and never scanned metadata/conditions/routes; fix merged as release `27a402979`); left unclaimed. Durable acceptance endpoint of record is `https://elspeth.aws.foundryside.dev` — recorded so later readers stop expecting an apex record. Operator ratified the RDS connection budget at approved-budget=50 / safety-margin=50, superseding the agent-chosen 100/50; verify-connection-budget re-ran in-task at ~16:0x AEST (06:0x UTC) with the ratified numbers — PASS, receipt schema `elspeth.rds-connection-budget.v3`, high_water 6.0 vs max_connections 194, ok:true, same 10-point 05:38–05:47Z window, scenario A. The ratified 50/50 parameters are now the operational ceiling of record (no user-load data yet; conservative by ~8x over observed high-water) |

## Reconciliation findings

- R3-F5 is **already filed** as `elspeth-926ac02d3e`; no duplicate should be
  created.
- The `numeric_route` finding is **already filed** as
  `elspeth-aaa9e3f597` and is currently verifying.
- R2-F17 `elspeth-5c0c09db31` is **closed**, not open/in flight. Its closure
  must be audited against the current release before deciding whether it needs
  reopening.
- The six advisor implementation bugs are already merged at the release
  baseline and await live acceptance. The advisor telemetry task is closed.
- The Landscape audit itself is complete for the routed Textract failure. The
  quarantine row is correctly data-only; only Web diagnostics discoverability
  remains defective.
- The source RCA documents remain under ignored `docs-archive/`; a fresh clone
  cannot reproduce their evidence until the acceptance subtree is tracked or
  the references are migrated.

## Update protocol

Each refresh must record:

1. exact release/worktree commit;
2. Filigree state and assignee changes;
3. the reproduction or acceptance evidence gathered;
4. files/worktrees owned by each agent;
5. AWS resources or policies mutated by whom;
6. tests and repository gates run on the exact integrated commit; and
7. residual blockers or operator-owned actions.

Do not mark an item complete from a narrow test alone. Closed and verifying
items are sampled against their stated acceptance evidence, and live AWS items
remain open until the deployed environment proves the behavior.
