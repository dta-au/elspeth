# R3 RCA remediation tracker

Last refreshed: 2026-08-03
Release baseline: `release/0.7.2@e326dab0c4703bfd02f048cba13f323e4b165c60`
Coordination owner: `codex-r3-rca-coordinator`

This is the human-readable companion to Filigree for the R3 remediation
program. Live Filigree state and the current release branch are authoritative;
this file is refreshed after each audit, implementation, integration, and AWS
acceptance checkpoint.

The supplied objective says “41 issues across 4 RCA epics, plus 5
pre-existing”, but its explicit tables contain **44 unique non-parent issue
IDs**, plus four parent epics. This tracker follows the explicit IDs so nothing
is silently dropped.

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
integrations, validation, routing, audit evidence, and deployment needed to
exercise those behaviors are core/demo-aware work even though they involve
AWS. IAM/Terraform/runbook/shell mechanics that only improve installation are
the deferred lane.

## Current roll-up

| State | Count | Meaning |
|---|---:|---|
| Closed | 7 | Tracker says done; closure evidence is still sampled during the completion audit |
| Verifying | 9 | Locally fixed; live or requirement-specific acceptance remains |
| Fixing | 1 | Owned implementation is still in flight |
| Open | 5 | Confirmed task/epic work not yet started here |
| Triage | 19 | Root cause and reproducibility must be checked against current HEAD before fixing |
| Proposed | 3 | Regression-gate features require approval/acceptance design before implementation |

Parent state:

| Epic | Live state | Immediate coordination action |
|---|---|---|
| `elspeth-7ffd77deca` — advisor gate | `triage` | Verify six merged children live, then disposition the parent from child evidence |
| `elspeth-7da4e52344` — compose loop | `triage` | Complete current-HEAD RCA and partition six residuals |
| `elspeth-e7ff15ac0b` — gate routing | `open` | Reconcile five closed, two verifying, and five residual children |
| `elspeth-e54343d43b` — AWS installer | `open` | Hold implementation until demo-aware product/system work is accepted; only deconflict existing owners in the meantime |

## Advisor gate — `elspeth-7ffd77deca`

| ID | Priority | Live state | Owner | Next proof/action |
|---|---:|---|---|---|
| `elspeth-955438d517` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live Composer run proves complete structural projection, failure routes, and no false sign-off |
| `elspeth-fcef029996` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live second-pass re-review converges with prior findings/actions visible |
| `elspeth-ca751fa4e1` | P1 | `verifying` | `codex-release-0.7.2-integration` | Human surface contains fixed safe wording, never raw advisor text or sentinels |
| `elspeth-f5a9021d2d` | P2 | `verifying` | `codex-release-0.7.2-integration` | Green runtime preflight remains green while advisor completion is withheld |
| `elspeth-4b3ac84038` | P1 | `verifying` | `codex-release-0.7.2-integration` | Every run entry point and backend execute/review path rejects a durable advisor block |
| `elspeth-1033d97b6c` | P3 | `verifying` | `codex-release-0.7.2-integration` | Live Textract uses deployment-owned region and proves the bucket region before Textract |
| `elspeth-bc6d1c5d8d` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample merged telemetry and terminology evidence during final audit |

## Compose loop — `elspeth-7da4e52344`

| ID | Priority | Live state | Owner | Next proof/action |
|---|---:|---|---|---|
| `elspeth-981130d70a` | P1 | `triage` | unassigned | Reproduce missing required-control auto-wiring on current freeform path; compare guided scaffolding |
| `elspeth-cd98ea9d82` | P1 | `triage` | unassigned | Trace request ID from envelope creation through structured logs and CloudWatch |
| `elspeth-f159d2394b` | P2 | `triage` | unassigned | Measure deadline/turn-budget relationship and identify the operator-owned configuration seam |
| `elspeth-7bd0141bbe` | P2 | `triage` | unassigned | Reproduce registry discoverability and false `raw_html` assistance against current catalog/prompt state |
| `elspeth-ecd8594b63` | P3 | `triage` | unassigned | Audit all taught `select_only` examples for downstream field obligations |
| `elspeth-ebba0b2171` | P3 | `triage` | unassigned | Trace per-turn boundaries and define parity with planner telemetry without leaking content |

## Gate routing — `elspeth-e7ff15ac0b`

| ID | Priority | Live state | Owner | Next proof/action |
|---|---:|---|---|---|
| `elspeth-fa63549a59` | P1 | `closed` | unassigned | Sample closure against retained operator prose and stable-subject edit binding |
| `elspeth-2ac590c79f` | P1 | `closed` | unassigned | Sample literal/`option_path` carry-forward through durable deferred intent |
| `elspeth-82d8bea477` | P1 | `closed` | unassigned | Sample threshold vocabulary and topology-stage disposition |
| `elspeth-fd32c3e6fd` | P1 | `verifying` | `codex-release-0.7.2-integration` | Live observed CSV numeric gate is rejected before run creation |
| `elspeth-b326add5be` | P1 | `triage` | unassigned | Reproduce per-row gate failure semantics and establish the intended `on_error` contract |
| `elspeth-dc07d517cf` | P2 | `closed` | unassigned | Sample clarification intent visibility/claimability at later stages |
| `elspeth-6795b3ae3a` | P2 | `open` | unassigned | Inventory every provider-facing projection and compare obligations with rendered evidence |
| `elspeth-d0d52e2fde` | P2 | `closed` | `codex-release-0.7.2-integration` | Sample the real HTTP/lifecycle regression added with the proof fix |
| `elspeth-c4734bc69a` | P3 | `triage` | unassigned | Reconcile `AuditCharacteristic.COERCE` with observed-mode runtime behavior |
| `elspeth-926ac02d3e` | P1 | `triage` | unassigned | R3-F5 is already filed; reproduce refusal, find the operator control seam, and correct guidance |
| `elspeth-aaa9e3f597` | P2 | `verifying` | `codex-release-0.7.2-integration` | Live guided path retains the decision heading and treats gates as topology, not plugins |
| `elspeth-6801b71f71` | P2 | `open` | unassigned | Query Landscape evidence for a routed/quarantined row once the acceptance database is reachable |

## AWS installer — `elspeth-e54343d43b`

**Deferred lane:** this section covers installer-specific IAM, Terraform,
runbook, teardown, and shell mechanics. Do not defer Textract, S3 plugin,
runtime AWS integration, validation, or audit behavior merely because it uses
AWS; those are core/demo-aware product work.

| ID | Priority | Live state | Owner | Next proof/action |
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

| ID | Priority | Live state | Owner | Next proof/action |
|---|---:|---|---|---|
| `elspeth-bcc6bdac99` | P1 | `triage` | unassigned | Reproduce the llm/guardrail correction and verify it performs a real re-plan |
| `elspeth-5904b1683a` | P1 | `verifying` | `codex-aws-cold-install-coordinator` | Do not close from advisor evidence; exercise the canonical planner-repair prompt live |
| `elspeth-a229c247a1` | P2 | `triage` | unassigned | Reproduce Container Insights orphan lifecycle with the integrated installer policy |
| `elspeth-9f7d336e1c` | P1 | `fixing` | `operator` | Preserve operator custody; verify a published candidate image boots after the signing/release gate |
| `elspeth-5c0c09db31` | P1 | `closed` | `codex-r2-f17` | Objective text is stale: sample the closed R2-F17 evidence instead of reopening by assumption |

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
