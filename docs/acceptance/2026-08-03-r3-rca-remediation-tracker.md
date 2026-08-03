# R3 RCA remediation tracker

Last refreshed: 2026-08-03T19:24:00+10:00 (Australia/Canberra)
Filigree snapshot: 2026-08-03T19:23:00+10:00
Release baseline: `release/0.7.2@278593c09`
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

## Current roll-up — 44 objective items plus 10 discovered/delivery records

| State | Objective 44 | Additional 10 | Meaning |
|---|---:|---:|---|
| Closed | 8 | 5 | Tracker says done; closure evidence is still sampled during the completion audit |
| Verifying | 13 | 2 | Locally fixed; live or requirement-specific acceptance remains |
| Fixing | 5 | 0 | Owned bug implementation is still in flight |
| In progress | 0 | 1 | A delivery task spanning existing objective defects is in flight |
| Open | 6 | 2 | Confirmed task/epic work not yet started here |
| Triage | 9 | 0 | Root cause and reproducibility must be checked against current HEAD before fixing |
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
progress `elspeth-4e6f2a59e4`.

Closed-record assignees below are retained Filigree audit history, not active
claim custody.

## Active coordination wave

| Agent | Scope | Worktree/branch | File custody | AWS authority | Status |
|---|---|---|---|---|---|
| `/root` | Integration, tracker custody, worktree partitioning, and AWS operations | `.claude/worktrees/r3-rca-remediation-tracker`; `codex/r3-rca-remediation-tracker` | This tracker until implementation partitions are assigned | Sole mutation custodian | Active |
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
| `/root/impl_compose_required_controls` | Finalize deterministic required controls on both freeform publication paths | `.claude/worktrees/compose-required-controls`; `codex/fix-compose-required-controls` | `tool_batch.py`, private proposal authority/commit/audit/blob-retention seams, direct tests | None | Named one-source and two-source blob proposals now pass exact route/acceptance tests without public or audit disclosure; lower-level tamper/nonpublication coverage is finishing before review |
| `/root/audit_provider_projection_obligations` | Re-audit provider-facing projection and hidden-option custody on integrated release | Shared read-only `ca375291c` baseline | None | None | Complete; four P1 residuals reproduced and partitioned into three tracked implementation children |
| `/root/impl_guided_chat_revision_custody` | Prevent blind source/sink replacement through guided chat | `.claude/worktrees/guided-chat-revision-custody`; `codex/fix-guided-chat-revision-custody` | Released after integration; `guided_chat_atomic.py` handed to terminal-progress lane | None | Complete as `7afa62b6d` + `c4cade0f0`; independent review clean and 10 exact release regressions passed |
| `/root/rca_compose_request_correlation` | Emit bounded correlation events for structured HTTP errors and Pydantic 422s | `.claude/worktrees/compose-request-correlation`; `codex/fix-compose-request-correlation` | Released after integration | None | Complete through `39c6b14a7`; 32 exact release-handler regressions passed; live CloudWatch lookup remains |
| `/root/impl_frontend_readiness_axes` | Apply backend readiness axes to every frontend action | `.claude/worktrees/frontend-readiness-axes`; `codex/fix-frontend-readiness-axes` | Released after integration | None | Complete; integrated through `9016ce6a6`, exact release rerun 148 passed, task closed |
| `/root/impl_guided_plan_terminal_progress` | Settle terminal progress for every guided-plan outcome | `.claude/worktrees/guided-plan-terminal-progress`; `codex/fix-guided-plan-terminal-progress` | Released after integration | None | Complete through `859c2a642`; independent adversarial review clean, 87 progress/guided-plan plus 10 chat-custody release tests passed, task closed |
| `/root/impl_guided_gate_proof_validation` | Make source-proof diagnostics authoritative during guided confirmation | `.claude/worktrees/guided-gate-proof-validation`; `codex/fix-guided-gate-proof-validation` | Guided confirmation plus reviewed-source custody/redaction seams and direct tests | None | Candidate `b4fb70270` held: mixed carriers can leak private paths and rejected blob custody can incorrectly abstain to a green proof; real positive/negative ExecutionService regressions are RED/in progress |
| `/root/impl_advisor_surface_deadline` | Close successful re-review leakage and bind checkpoints to the compose deadline | `.claude/worktrees/advisor-surface-deadline`; `codex/fix-advisor-surface-deadline` | Advisor service/turn-audit/no-tool surfaces and direct tests; explicitly excludes `tool_batch.py` until released | None | Real FLAG-to-repair-to-CLEAN transcript leakage and four-call deadline overrun are RED; production implementation is active |

No subagent may mutate AWS or the shared release checkout during this wave.
Each implementation agent is confined to the explicit worktree/file custody
listed above.

Parent workstream state:

| Parent record | Live state | Immediate coordination action |
|---|---|---|
| `elspeth-7ffd77deca` — advisor gate | `verifying` | Run the merged D1-D6 acceptance matrix on one deployed candidate |
| `elspeth-7da4e52344` — compose loop | `fixing` | Integrate the no-collision aids lane, then sequence the shared `service.py` and tool-boundary lanes |
| `elspeth-e7ff15ac0b` — gate routing | `open` | Reconcile five closed, five verifying, and two residual children |
| `elspeth-e54343d43b` — AWS installer | `open` | Hold implementation until demo-aware product/system work is accepted; only deconflict existing owners in the meantime |

## Advisor gate — `elspeth-7ffd77deca`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-955438d517` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live Composer run proves complete structural projection, failure routes, and no false sign-off |
| `elspeth-fcef029996` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live second-pass re-review converges with prior findings/actions visible |
| `elspeth-ca751fa4e1` | P1 | `fixing` | `codex-advisor-surface-deadline` | Close successful FLAG-to-repair-to-CLEAN leakage from final and persisted assistant prose, not only the terminal blocked branch |
| `elspeth-f5a9021d2d` | P2 | `verifying` | `codex-release-0.7.2-integration` | Green runtime preflight remains green while advisor completion is withheld |
| `elspeth-4b3ac84038` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live surfaces agree on the chosen completion-only policy: execution remains admitted; Save/review completion is refused |
| `elspeth-1033d97b6c` | P3 | `verifying` | `codex-release-0.7.2-integration` | Live Textract uses deployment-owned region and proves the bucket region before Textract |
| `elspeth-bc6d1c5d8d` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample merged telemetry and terminology evidence during final audit |

## Compose loop — `elspeth-7da4e52344`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-981130d70a` | P1 | `fixing` | `codex-compose-required-controls` | Complete strict private proposal authority for valid named/multiple blob-backed sources, redacted review output, tamper rejection, and exact acceptance custody |
| `elspeth-cd98ea9d82` | P1 | `verifying` | `codex-rca-compose-request-correlation` | Integrated through `39c6b14a7`; verify body/header/log equality in deployed CloudWatch events |
| `elspeth-f159d2394b` | P2 | `confirmed` | unassigned | Core seconds-per-logical-turn premise disproved; defer Terraform/operator timeout tuning as setup ergonomics |
| `elspeth-7bd0141bbe` | P2 | `fixing` | `codex-rca-compose-registry-assistance` | Integrated aids fixed prompt delivery and Textract false aid; queue write-boundary registry validation after active tool ownership releases |
| `elspeth-ecd8594b63` | P3 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `2906409e1` + `e1d31f104`; prove a live freeform fixed-sink build preserves every required field |
| `elspeth-ebba0b2171` | P3 | `confirmed` | unassigned | Durable provider-call audit exists; logger parity is policy debt, while terminal guided progress is split to `4e6f2a59e4` |
| `elspeth-73c7a4df36` | P1 | `closed` | `codex-compose-authoring-aids` | Delivery completed at `release/0.7.2@e1d31f104`; assignee retained as audit history |
| `elspeth-57232f6f3c` | P1 | `in_progress` | `codex-advisor-surface-deadline` | Bind EARLY/END retries to one shrinking deadline now; defer only the small `tool_batch.py` counter/failed-turn follow-up until required-controls releases custody |
| `elspeth-4e6f2a59e4` | P2 | `closed` | `codex-guided-plan-terminal-progress` | Integrated through `859c2a642`; all guided-plan outcomes terminalize under exact generation and authoritative outcome primacy |

## Gate routing — `elspeth-e7ff15ac0b`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-fa63549a59` | P1 | `closed` | unassigned | Sample closure against retained operator prose and stable-subject edit binding |
| `elspeth-2ac590c79f` | P1 | `closed` | unassigned | Sample literal/`option_path` carry-forward through durable deferred intent |
| `elspeth-82d8bea477` | P1 | `closed` | unassigned | Sample threshold vocabulary and topology-stage disposition |
| `elspeth-fd32c3e6fd` | P1 | `fixing` | `codex-gate-proof-guided-validation` | Candidate held: guided persistence must use the real proof, and a claimed blob sentinel whose session/path/status custody fails must be hard-invalid rather than proof abstention; unify all-carrier redaction validation |
| `elspeth-b326add5be` | P1 | `verifying` | `codex-gate-row-error-policy` | Integrated through `e3804416f`; live mixed good/bad CSV run must route one row per policy without aborting the run |
| `elspeth-dc07d517cf` | P2 | `closed` | unassigned | Sample clarification intent visibility/claimability at later stages |
| `elspeth-6795b3ae3a` | P2 | `open` | unassigned | Inventory every provider-facing projection and compare obligations with rendered evidence |
| `elspeth-dca1e81c58` | P1 | `open` | unassigned | Preserve selected-node hidden state and reuse schema-form rewind for wire-stage source/output edits after S3 releases `guided.py` custody |
| `elspeth-3526685369` | P1 | `closed` | `codex-guided-chat-revision-custody` | Integrated as `7afa62b6d` + `c4cade0f0`; form-directed source/sink custody and retained intent verified locally |
| `elspeth-eacfec09a6` | P1 | `closed` | unassigned | Current-head D1 audit proved unified evidence/injection candidates, withheld-value semantics, complete bounded triples, and failure routes; 12 focused regressions passed at `131a5f584` |
| `elspeth-4c699cb5d0` | P1 | `closed` | `codex-frontend-readiness-axes` | Integrated through `9016ce6a6`; exact release 148-test action/fanout slice passed; parent D5 remains live-verifying |
| `elspeth-d0d52e2fde` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample the real HTTP/lifecycle regression added with the proof fix |
| `elspeth-c4734bc69a` | P3 | `verifying` | `codex-r3-rca-coordinator` | Behavior `d78fbbed6` plus corpus `10179f2c1`; live catalog acceptance and combined full-suite gate remain |
| `elspeth-aaa9e3f597` | P2 | `verifying` | `codex-release-0.7.2-integration` | Live guided path retains the decision heading and treats gates as topology, not plugins |
| `elspeth-1d97fc4b80` | P1 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `b424c08c4`; live correction must preserve unrelated gate routes and LLM prompt/profile options byte-for-byte |
| `elspeth-18b39eb829` | P2 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `0928c6ac6`; live run panel must expose the already-recorded Textract node/code/hint without direct DB access |

## Related core/demo-aware product and system work

These are part of the objective but are parentless in live Filigree; they are
not gate-routing children.

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-926ac02d3e` | P1 | `verifying` | `codex-s3-source-profiles` | Integrated through `131a5f584`; run one live operator-profiled S3 read and confirm redacted audit evidence plus endpoint denial |
| `elspeth-6801b71f71` | P2 | `closed` | `codex-r3-rca-coordinator` | Read-only live DB proof found complete structured failure and DIVERT provenance; UI gap split to `elspeth-18b39eb829` |

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
