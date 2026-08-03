# R3 RCA remediation tracker

Last refreshed: 2026-08-03T16:57:00+10:00 (Australia/Canberra)
Filigree snapshot: 2026-08-03T16:56:03+10:00
Release baseline: `release/0.7.2@0928c6ac6`
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

## Current roll-up — 44 objective items plus 4 discovered/delivery records

| State | Objective 44 | Additional 4 | Meaning |
|---|---:|---:|---|
| Closed | 8 | 0 | Tracker says done; closure evidence is still sampled during the completion audit |
| Verifying | 11 | 1 | Locally fixed; live or requirement-specific acceptance remains |
| Fixing | 3 | 1 | Owned bug implementation is still in flight |
| In progress | 0 | 1 | A delivery task spanning existing objective defects is in flight |
| Open | 4 | 1 | Confirmed task/epic work not yet started here |
| Triage | 15 | 0 | Root cause and reproducibility must be checked against current HEAD before fixing |
| Proposed | 3 | 0 | Regression-gate features require approval/acceptance design before implementation |

The additional records are newly discovered gate-custody bug
`elspeth-1d97fc4b80`, run-diagnostics UI bug `elspeth-18b39eb829`, and
non-overlapping compose delivery task `elspeth-73c7a4df36`, plus deferred
evidence-supportability task `elspeth-01c627b420`. They are tracked without
changing the supplied objective's 44-item arithmetic.

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
| `/root/impl_guided_node_custody` | Preserve unchanged node behavior across guided replanning | `.claude/worktrees/guided-node-custody`; `codex/fix-guided-node-custody` | `guided/planning.py`, `composer/service.py`, `pipeline_planner.py`, direct tests | None | RED captured; implementation active |
| `/root/impl_gate_row_error_policy` | Per-row gate `on_error` handling | `.claude/worktrees/gate-row-error-policy`; `codex/fix-gate-row-error-policy` | gate config/executor/traversal/source-iteration seams and direct tests | None | RED captured at missing `GateSettings.on_error`; active |
| `/root/impl_csv_audit_characteristics` | Honest CSV audit characteristics | `.claude/worktrees/csv-audit-characteristics`; `codex/fix-csv-audit-characteristics` | CSV source, characteristic UI wording, exact scenario-corpus oracle, direct tests | None | Complete; behavior `d78fbbed6`, corpus oracle `10179f2c1` |
| `/root/impl_s3_source_profiles` | Operator-profiled Web S3 source | `.claude/worktrees/s3-source-profiles`; `codex/fix-s3-source-profiles` | Web config/profile/policy/S3 lowering/source seams and direct tests | None | Active; two bounded review subagents |
| `/root/impl_run_diagnostics_ui` | Surface routed-failure provenance | `.claude/worktrees/run-diagnostics-ui`; `codex/fix-run-diagnostics-ui` | `RunsHistoryDrawer` and direct frontend diagnostics presentation/tests | None | Complete; integrated as `9130e1209` + `0928c6ac6` |
| `/root/impl_compose_authoring_aids` | Compose context and truthful field-mapping aids | `.claude/worktrees/compose-authoring-aids`; `codex/fix-compose-authoring-aids` | `prompts.py`, `planner_authoring_aids.py`, `field_mapper.py`, direct tests | None | Active |

No subagent may mutate AWS or the shared release checkout during this wave.
Each implementation agent is confined to the explicit worktree/file custody
listed above.

Parent workstream state:

| Parent record | Live state | Immediate coordination action |
|---|---|---|
| `elspeth-7ffd77deca` — advisor gate | `verifying` | Run the merged D1-D6 acceptance matrix on one deployed candidate |
| `elspeth-7da4e52344` — compose loop | `fixing` | Integrate the no-collision aids lane, then sequence the shared `service.py` and tool-boundary lanes |
| `elspeth-e7ff15ac0b` — gate routing | `open` | Reconcile five closed, four verifying, and three residual/discovered children |
| `elspeth-e54343d43b` — AWS installer | `open` | Hold implementation until demo-aware product/system work is accepted; only deconflict existing owners in the meantime |

## Advisor gate — `elspeth-7ffd77deca`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-955438d517` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live Composer run proves complete structural projection, failure routes, and no false sign-off |
| `elspeth-fcef029996` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live second-pass re-review converges with prior findings/actions visible |
| `elspeth-ca751fa4e1` | P1 | `verifying` | `codex-release-0.7.2-integration` | Human surface contains fixed safe wording, never raw advisor text or sentinels |
| `elspeth-f5a9021d2d` | P2 | `verifying` | `codex-release-0.7.2-integration` | Green runtime preflight remains green while advisor completion is withheld |
| `elspeth-4b3ac84038` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live surfaces agree on the chosen completion-only policy: execution remains admitted; Save/review completion is refused |
| `elspeth-1033d97b6c` | P3 | `verifying` | `codex-release-0.7.2-integration` | Live Textract uses deployment-owned region and proves the bucket region before Textract |
| `elspeth-bc6d1c5d8d` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample merged telemetry and terminology evidence during final audit |

## Compose loop — `elspeth-7da4e52344`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-981130d70a` | P1 | `triage` | unassigned | Reproduce missing required-control auto-wiring on current freeform path; compare guided scaffolding |
| `elspeth-cd98ea9d82` | P1 | `triage` | unassigned | Trace request ID from envelope creation through structured logs and CloudWatch |
| `elspeth-f159d2394b` | P2 | `triage` | unassigned | Measure deadline/turn-budget relationship and identify the operator-owned configuration seam |
| `elspeth-7bd0141bbe` | P2 | `triage` | unassigned | Reproduce registry discoverability and false `raw_html` assistance against current catalog/prompt state |
| `elspeth-ecd8594b63` | P3 | `triage` | unassigned | Audit all taught `select_only` examples for downstream field obligations |
| `elspeth-ebba0b2171` | P3 | `triage` | unassigned | Trace per-turn boundaries and define parity with planner telemetry without leaking content |
| `elspeth-73c7a4df36` | P1 | `in_progress` | `codex-compose-authoring-aids` | Delivery task: compose context parity plus truthful Textract/`select_only` teaching without shared-file collisions |

## Gate routing — `elspeth-e7ff15ac0b`

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-fa63549a59` | P1 | `closed` | unassigned | Sample closure against retained operator prose and stable-subject edit binding |
| `elspeth-2ac590c79f` | P1 | `closed` | unassigned | Sample literal/`option_path` carry-forward through durable deferred intent |
| `elspeth-82d8bea477` | P1 | `closed` | unassigned | Sample threshold vocabulary and topology-stage disposition |
| `elspeth-fd32c3e6fd` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live observed CSV numeric gate is rejected before run creation |
| `elspeth-b326add5be` | P1 | `fixing` | `codex-gate-row-error-policy` | RED proves `GateSettings.on_error` is absent; add fail-fast-by-default and auditable per-row diversion |
| `elspeth-dc07d517cf` | P2 | `closed` | unassigned | Sample clarification intent visibility/claimability at later stages |
| `elspeth-6795b3ae3a` | P2 | `open` | unassigned | Inventory every provider-facing projection and compare obligations with rendered evidence |
| `elspeth-d0d52e2fde` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample the real HTTP/lifecycle regression added with the proof fix |
| `elspeth-c4734bc69a` | P3 | `verifying` | `codex-r3-rca-coordinator` | Behavior `d78fbbed6` plus corpus `10179f2c1`; live catalog acceptance and combined full-suite gate remain |
| `elspeth-aaa9e3f597` | P2 | `verifying` | `codex-release-0.7.2-integration` | Live guided path retains the decision heading and treats gates as topology, not plugins |
| `elspeth-1d97fc4b80` | P1 | `fixing` | `codex-guided-node-custody` | Preserve server-custodied unchanged node behavior/options during unrelated guided corrections |
| `elspeth-18b39eb829` | P2 | `verifying` | `codex-r3-rca-coordinator` | Integrated as `0928c6ac6`; live run panel must expose the already-recorded Textract node/code/hint without direct DB access |

## Related core/demo-aware product and system work

These are part of the objective but are parentless in live Filigree; they are
not gate-routing children.

| ID | Priority | Live state | Filigree assignee | Next proof/action |
|---|---:|---|---|---|
| `elspeth-926ac02d3e` | P1 | `fixing` | `codex-s3-source-profiles` | Add an operator-profiled Web S3 source with fixed bucket/prefix/region and safe relative-key lowering |
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
