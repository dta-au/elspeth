# ACA pivot and remaining multi-replica work

**Goal:** Make the ACA delivery and the unfinished parts of the earlier
multi-replica programme independently resumable.

**Baseline:** Desktop review on 2026-09-10, `release/0.8.1` at `7090aae27`.
The checkout also contains unrelated auth/identity changes; those are not
delivery evidence for this plan. No tests or cloud operations were run for
the original desktop reconciliation. Tracker states below were retrieved on
this date; subsequent implementation evidence is distinguished below.

**Implementation update (2026-09-10):** A is implemented; B–E are implemented
with completed local runtime verification recorded under
[Final verification](#final-verification). F adopts explicit deferral of receipt v3 and the listed
provider work. No live ACA acceptance receipt or cloud test is claimed.

**Architecture:** Keep the delivered ACA environment/workload bundle and
PostgreSQL-backed membership/session fencing. Retain Single revision mode
with sticky sessions until routing without affinity is separately qualified.
Durable run handoff, cross-replica streaming and shared quotas are runtime
extensions distinct from the unchanged legacy receipt contract.

**Prerequisites for implementation:** Re-read current `AGENTS.md` and
`CONTRIBUTING.md`, inspect the current tracker and source, and use an isolated
worktree under `.claude/worktrees/`. Do not execute the July plan's obsolete
epoch changes, worktree setup, dependency installation or broad staging commands.

## Scope and precedence

This document governs resumption of the ACA/multi-replica portions of the
[July implementation plan](2026-07-26-finish-deferred-deployment-platforms.md),
[September resume brief](2026-09-03-multi-replica-resume-brief.md),
[release readiness plan](2026-09-04-release-0.8.0-readiness-plan.md), and
[Phase 6b design](2026-09-04-release-0.8.0-phase6b-azure-container-apps-plan.md).
Their historical measurements remain useful history, not current task lists.
This does not re-plan the unrelated release, Composer or signing programmes.

- The current task is ACA-focused. Kubernetes/AKS and general platform-profile
  delivery are outside this task; the pivot does not delete their backlog.
- On 2026-09-10 the operator replaced the live-run closure condition of
  `elspeth-5ec3befc1a` with desktop analysis: "desktop analysis - don't wait
  for proof". That task is **closed**. A first cloud run, second clean run,
  live receipt and receipt-conditioned documentation promotion are no longer
  prerequisites to its closure. Do not reopen it merely because they are absent.
- This ruling does not manufacture cloud evidence. Keep the executable
  acceptance procedure and its receipt validators honest if it is later run.
  Public support wording needs its own reconciliation (task A below).
- Phase 6b `elspeth-f23854fed7`, membership `elspeth-66a19780b1`, review
  `elspeth-c1ea76ff05`, and follow-up recording `elspeth-6f8c1714d5` are
  terminal. Closing the recording task did not implement its deferred features.
- The broader runtime feature `elspeth-b5d7aa5655` remains `approved`.
  The old roll-up `elspeth-a5b07ac072` and ACA prerequisite
  `elspeth-3bbf9a9731` were superseded; do not restore their old dependency cycle.

## Original delivered slice — historical baseline

This table records the source-backed baseline before B–E implementation, not
the current runtime or a fresh passing-test/live-cloud verdict. In particular,
the recovery-only and owner-affine limitations below are superseded by the
dated task statuses; the legacy receipt's measurement limits remain.

| Area | Original implementation and boundary |
| --- | --- |
| Infrastructure | `deploy/azure-container-apps/main.bicep`, `environment.bicep`, `workload.bicep`: environment plus workload, not the July workload-only module. Separate external PostgreSQL databases; shared NFS for files, never SQLite on that share. |
| Installation | `scripts/bootstrap-acceptance.sh`, `resolve-workload-parameters.sh`, `run-job.sh` under that bundle: concrete versioned-secret parameters, Jobs deployed before execution, runtime app admitted after provisioning/doctors. Preserve the existing-registry and non-root runtime contracts. |
| Membership | `src/elspeth/web/coordination/membership_authority.py` and `membership_lifecycle.py`: register, heartbeat, drain and stop. The old claim that membership has no writer is obsolete. |
| Recovery | `src/elspeth/web/coordination/run_recovery_authority.py` and the shared replica probes: dead-owner recovery/session-fence reacquisition is distinct from automatically resuming its engine execution. |
| Acceptance | `deploy/azure-container-apps/scripts/acceptance.sh`, `src/elspeth/web/azure_container_apps_acceptance.py`, `_azure_container_apps_acceptance/`: executable driver/facade and provider-bound receipts. Preserve both labelled-revision probing and the final Single-revision two-replica P1/P4a phase. |
| P1 / P2 | `src/elspeth/web/_acceptance_common/replica_probes.py`: concurrent guided/run-start contention is scored through session-operation fences. P2 requires one run and one conflict; it does not prove a durable run-start permit saga. |
| P3 | Expired dead-owner lease recovery; a graceful stop is a different mechanism and cannot count as proof of dead-owner takeover. It is not a transparent run-resume claim. |
| P4a / P4b | P4a proves shared database state and blob visibility. P4b records owner-affine live progress/tickets and structurally cannot pass. Sticky routing is a mitigation, not durable state. |
| Lease clock | The [Landscape clock plan](2026-09-08-landscape-lease-clock.md) and ADR-047 describe the separate engine authority contract. New web ownership must preserve Landscape fencing and fresh post-lock time decisions. |

## Original July work mapping — historical baseline

The deferred/owner-affine labels in this original mapping describe the starting
point. The dated implementation statuses below govern the current disposition.

| Original work | Disposition for resumption |
| --- | --- |
| Tasks 1–5: baseline, contracts, schema, ownership, lifecycle | Substantial substrate exists. Reconcile any missing behavior against current authorities; do not recreate modules or pin epoch 37/51. Membership completion alone does not complete the original run-ownership design. |
| Task 6: execution, resume, cancellation | Deferred saga/handoff scope: task B below. Preserve existing recovery and inspect current cancellation before changing it. |
| Task 7: tickets and run progress | Still owner-affine: task C. Database table presence is not evidence of a wired production adapter. |
| Task 8: Composer progress and quotas | Split into tasks D and E. The Phase 6b follow-up list names progress but does not adequately carry the shared-rate-limit obligation. |
| Tasks 9–10: blob safety and two-process corpus | Preserve delivered custody/fencing checks. Extend the corpus with each new capability; do not infer that P1–P4 cover every July scenario. |
| Tasks 11–12: Kubernetes/kind | Outside this ACA task. Existing `elspeth-2ff97dbc70` is open under `elspeth-f30e71ab70`; its notes include AKS and require a rewrite from the July one-replica bar. |
| Tasks 13–14: ACA bundle and acceptance | Replaced by Phase 6b and desktop acceptance; no new bundle or mandatory cloud run is required to close that delivered slice. |
| Task 15: machine-readable platform profiles | Deferred separately. `src/elspeth/web/deployment_profiles.py` is a startup-policy registry, not delivery of the July generated deployment-profile schema. |
| Tasks 16–17: public docs and closeout | Reconcile wording and actual evidence through A/F below. Do not revive the superseded 0.7.2 aggregator or reuse old suite counts as current evidence. |

## A. Reconcile public documentation with desktop acceptance

**Status (2026-09-10): implemented; integrated verification complete.**
See [Final verification](#final-verification).
Public documentation separates desktop acceptance, bounded runtime capability
and available evidence. Documentation contracts and current epoch checks
passed against the integrated implementation.

**Files:** `docs/reference/deployment-platforms.md`,
`deploy/azure-container-apps/README.md`,
`src/elspeth/web/_azure_container_apps_acceptance/README.md`,
`docs/runbooks/azure-container-apps-deployment.md`,
`docs/runbooks/azure-container-apps-cold-install.md`,
`docs/runbooks/azure-container-apps-existing-service-redeploy.md`.
**Checks:** `tests/unit/docs/test_deployment_platform_docs.py`,
`tests/unit/web/test_azure_container_apps_runbook_contract.py`.

1. Replace the obsolete claim that `5ec3befc1a` is pending with its desktop
   acceptance disposition. Separate implementation status, supported operating
   configuration and available evidence in the wording.
2. Reconcile the reference page's blanket one-replica/stop-before-start text
   with ACA's delivered Single/sticky configuration. Keep other targets'
   restrictions intact. State the current loss/reconnect limitations explicitly.
3. Retain cloud commands as an executable operator procedure, not an outstanding
   tracker closure condition. Never add an invented receipt to satisfy a docs gate.
4. Update any documentation-contract assertions which encode the superseded
   closure rule, preserving assertions that reject false live-evidence claims.

**Done when:** the named pages agree about desktop acceptance and runtime
limitations, and their contract tests pass. No cloud deployment is required.
The subsequent instruction to execute this plan authorizes this follow-up.

## B. Durable run admission, handoff and cancellation

**Status (2026-09-10): implemented; integrated verification complete.** The
integrated implementation provides immutable execution envelopes, atomic run/permit
admission, retained input bytes and version-pinned secrets, authenticated peer
cancellation, and fresh web/Landscape ownership for automatic dispatch,
PREPARED restart and eligible checkpoint resume. Landscape advances to epoch
39; the integrated Sessions schema is epoch 54. Local PostgreSQL process-crash
proofs cover admission/permit/linkage death, PREPARED and checkpoint recovery,
stale-owner refusal, terminal/output crash retry and CLI takeover races. These
mechanisms are covered by the completed default and serial PostgreSQL suites
recorded under [Final verification](#final-verification). They do not establish
live ACA acceptance.

Supported transitions preserve one active Landscape scheduler leader per run
and the original run UUID. PREPARED replay requires proof of no effects;
EXECUTING is marked before plugin initialization. Resume selects the latest
checkpoint after fresh leadership acquisition and continuously checks web
custody. FAILED/INTERRUPTED reconciliation holds status-preserving Landscape
authority and the row lock through web/output finalization. Unsafe effects,
incomplete sources and identity/compatibility failures remain
`recovery_required`. Cancellation before a baseline uses the admitted envelope
without resolving changed secrets/runtime/policy or invoking plugins. Retained
input objects are content-addressed and fsynced with no automatic pruning.

Resume identity qualification (closure review, `elspeth-f321e3ff21` comment
10110): central plugin version, source and determinism checks cover CLI and
web. The full engine/runtime source and distribution fingerprint applies to
the web handoff envelope. Direct CLI resume across unchanged-version engine
or interpreter drift remains open; B does not close universal CLI resume
identity compatibility.

The [public handoff contract](../reference/deployment-platforms.md#durable-run-handoff)
and [ADR-041 amendment](../architecture/adr/041-state-engine-supported-profiles.md#amendment-2026-09-10-bounded-aca-runtime-acceptance)
record this bounded runtime acceptance. They do not promote the frozen v3
state-engine catalog or change legacy receipt schemas. The steps below remain
the implementation and integration acceptance checklist.

**Inspect/modify:** `src/elspeth/web/execution/service.py`, `routes.py`,
`protocol.py` in the same directory; `src/elspeth/web/coordination/repository.py`,
`run_recovery_authority.py`; `src/elspeth/web/sessions/models.py`, `protocol.py`;
`src/elspeth/engine/orchestrator/resume.py`.
**Extend tests:** `tests/unit/web/execution/test_service.py`,
`tests/testcontainer/web/test_global_run_recovery_postgres.py`.
**Added tests:** `tests/testcontainer/web/test_cross_process_run_control_postgres.py`,
`tests/testcontainer/web/test_cross_process_run_reconciliation_postgres.py`.

1. Inventory the current production admission, ownership, cancellation and
   recovery writers through the authority registry and their real callers.
   Resolve which July requirements already have coverage before adding a saga.
2. Specify the cross-database failure states: admitted but not dispatched,
   Landscape started but Sessions not linked, owner lost during execution,
   engine terminal but Sessions unfinished. Define retry/reconciliation for each;
   neither database may silently authorize writes in the other.
3. Add independent-process PostgreSQL regressions for crashes at each seam,
   stale-owner refusal, one winning admission, peer cancellation, and terminal
   reconciliation. Assert unchanged state/audit records on refused writes.
4. Implement only the missing transitions through owned typed authorities.
   Automatic resume must acquire valid Landscape authority and revalidate web
   ownership; failure must not refresh an old token or replay external effects.
5. Preserve the `elspeth-f321e3ff21` identity qualification above before claiming
   safe resume across image changes; direct CLI unchanged-version engine or
   interpreter drift remains open. The separate cleanup bug
   `elspeth-245b21351b` is closed; reuse its
   regressions rather than treat it as unimplemented.

**Done when:** every documented crash state has a deterministic recovery/refusal
outcome, non-owner cancellation is durable, and stale owners cannot project or
finalize. Automatic handoff is implemented for the stated transitions;
integrated verification covers those transitions and preserves the explicit
refusal cases above.

## C. Durable tickets and reconnectable run progress

**Status (2026-09-10): implemented; integrated verification complete.**
See [Final verification](#final-verification).
External PostgreSQL stores ticket digests for atomic single-use consumption
and ordered run events for authorized peer replay. Local process/socket tests
exercise expiry, races, reconnect and owner loss. The local adapter remains
process-local. Legacy v2 P4b remains conservative `cannot_pass`; its unchanged
owner-affine mechanism does not measure these durable runtime capabilities.

**Files:** `src/elspeth/web/execution/websocket_ticket.py`, `progress.py`,
`routes.py`, `service.py`; Sessions models/protocol and the appropriate authority.
**Tests:** `tests/unit/web/execution/test_websocket.py`, `test_progress.py`;
create `tests/testcontainer/web/test_cross_process_progress_postgres.py`.

1. Specify ticket ownership against the current identity model, not the July
   `user_id`/username snapshot. Define reconnect cursor and authorization behavior.
2. Add tests where A issues and B consumes a ticket, two consumers race,
   expiry/wrong identity/wrong run refuse, and only a token digest is persisted.
3. Wire atomic consumption through a database authority for external PostgreSQL.
   Retain the single-process adapter only on the explicitly local path.
4. Resume streams from durable ordered events; test owner death, duplicate
   delivery/reconnect, terminal state and authorization loss with separate processes.

**Done when:** tickets are single-use across replicas and reconnect recovers
authorized progress without the old process. Change P4b only with corresponding
new mechanism/schema tests; task D is also needed before removing affinity globally.

## D. Durable Composer progress and inflight accounting

**Status (2026-09-10): implemented; integrated verification complete.**
See [Final verification](#final-verification).
Renewable PostgreSQL request leases, bounded redacted progress snapshots and
per-request inflight records support peer reads and current cluster activity.
Local process/HTTP integration evidence exercises the production path.
Interrupted provider requests are not automatically resumed; guided, freeform
and tutorial transitions retain the same provider-backed Composer behavior.

**Files:** `src/elspeth/web/composer/progress.py`, `service.py`;
`src/elspeth/web/sessions/routes/composer/compose.py`, `guided_plan.py`;
Sessions models/protocol and owning authorities.
**Tests:** `tests/unit/web/composer/test_progress.py`;
create `tests/testcontainer/web/test_cross_process_composer_postgres.py`.

1. Define bounded progress snapshots and request identities/expiry. Preserve
   redaction and distinguish no local work from no work anywhere in the cluster.
2. Test A publishing/B reading, overlapping requests, one request settling while
   another remains active, owner death, expiry and cross-user refusal.
3. Persist via an authority with database-clock expiry and bounded cleanup.
   A request completion removes only its own inflight record.
4. Exercise reload/abort/reconnect against two processes. Preserve the same
   provider-backed Composer path for guided, freeform and tutorial transitions.

**Done when:** a peer cannot falsely report idle or settled because its local
registry is empty, and snapshots expose no raw tool content or secrets.

## E. Cluster-wide rate limits — omitted from the short follow-up list

**Status (2026-09-10): implemented; integrated verification complete.**
See [Final verification](#final-verification).
External PostgreSQL provides shared auth-IP, cheap-write and Composer budgets
through privacy-preserving keys, fresh post-lock clock decisions and bounded
cleanup. Local independent-process tests exercise shared admission and refusal
without local fallback. The explicitly local adapter remains process-local.

**Files:** `src/elspeth/web/middleware/rate_limit.py`, `src/elspeth/web/app.py`,
Sessions models/protocol and owning authorities.
**Tests:** `tests/unit/web/middleware/test_rate_limit.py`;
create `tests/testcontainer/web/test_cross_process_rate_limit_postgres.py`.

The original baseline used per-process buckets for Composer, cheap-write and
auth-IP instances; affinity did not provide one cluster-wide quota. That
external-PostgreSQL limitation is replaced by the shared adapter above.

1. Define scope/subject keys and privacy-preserving digests for all three
   limiter uses; carry separate budgets for cheap writes and LLM/execution work.
2. Test simultaneous requests through two independent processes: combined
   admissions respect the budget, expiry restores capacity, and scopes remain
   independent. Include database refusal and verify there is no local fallback.
3. Add an atomic database-clocked prune/count/admit authority and bounded
   cleanup. Reuse the existing PostgreSQL substrate; no new service is required
   merely because the current module's docstring suggests Redis.
4. Keep raw IPs, credential values and reversible subject identifiers out of
   persistence and diagnostics; test retry timing at the window boundary.

**Done when:** adding replicas no longer multiplies a configured cluster quota,
and auth, write and Composer routes select the shared adapter consistently.

## F. Receipt evolution and deferred provider work

**Status (2026-09-10): deferral adopted; existing receipt contracts verified.**
See [Final verification](#final-verification). No new provider or receipt v3 is implemented. Existing receipt
envelopes, closed mechanism vocabulary and validators remain unchanged; old
and current diagnostic reasons remain admissible without enabling a P4b pass.

The v3 trigger in `_azure_container_apps_acceptance/README.md` includes
**0.8.1 planning**, and this checkout is already on that release branch.
The instruction to execute this plan adopts its explicit disposition:
**defer v3 implementation** because this work changes neither the receipt
envelope nor the provider set. This does not erase the trigger. Revisit it
before a third provider or envelope/compatibility-field
change; implement the shared contract once, with both provider regression suites.

For that work inspect `src/elspeth/web/_acceptance_common/`,
`_azure_container_apps_acceptance/receipt_contracts.py`, and
`_aws_ecs_acceptance/receipt_contracts.py`. Preserve old evidence admission or
provide an explicit version transition; do not rename identifiers casually.

Keep Scenario B/C on ACA, `azure-otlp`, Entra PostgreSQL auth, Document
Intelligence acceptance, Kubernetes/AKS and generated platform profiles out of
this ACA completion scope. Their earlier inclusion/exclusion is not proof of
implementation. A provider-profile project must distinguish the existing startup
registry from a public profile schema. The ADR-041 bounded runtime amendment
records this scope; it does not promote the frozen state-engine catalog.

## Order and verification

The original execution order separated A from the runtime changes and required
a reviewed cross-database transition design for B. C, D and E coordinated
shared Sessions/auth, schema and authority-gate changes. Routing without
affinity still requires separate qualification before relaxing the ACA
configuration. None of B–F is silently made a new closure blocker for the
already desktop-accepted ACA task.

For each runtime task first observe the targeted behavioral regression fail,
then implement and record its completed passing result. Use the current schema
epoch and compatibility policy; never copy the July hard-cut numbers. Review
the complete touched files and whole-tree authority/fencing gates.

Focused documentation verification, from the selected checkout:

```bash
cd "$(git rev-parse --show-toplevel)" && \
  export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
aca_docs_log=$(mktemp /tmp/aca-docs-check.XXXXXX)
.venv/bin/python -m pytest -n 0 \
  tests/unit/docs/test_deployment_platform_docs.py \
  tests/unit/web/test_azure_container_apps_runbook_contract.py \
  > "$aca_docs_log" 2>&1
aca_docs_exit=$?
printf 'exit=%s log=%s\n' "$aca_docs_exit" "$aca_docs_log"
```

For runtime integration use the canonical full gate from that worktree:

```bash
cd "$(git rev-parse --show-toplevel)" && \
  scripts/full-suite-gate.sh --execute --detach \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Read the completed `summary.txt` and frozen-tree result. PostgreSQL tests
require Docker and serial execution; default pytest is not PostgreSQL evidence.
Compare the key-free lint corpus against the base and report the signing state
separately. Bicep changes also require the compiled-ARM bundle tests and pinned
CI compile job. The completed results below distinguish runtime verification
from the separately reported trust-tier signing state. Historical measurements
do not substitute for the completed integrated results.

## Final verification

The corrected working tree based on `a086371f1`, with the B merge staged
(frozen fingerprint `fb5231de4f42f6ea`), completed the default suite (**50,598 passed, 83 skipped, 2 xfailed**, exit 0)
and serial PostgreSQL suite (**433 passed, 1 skipped**, exit 0), with `frozen=YES`
in **/tmp/aca-corrected-final-runtime-gates/20260909T233507Z-aca-replica-residuals-2427810/summary.txt**. The separate final static run recorded
Ruff, mypy and contracts exit 0; the key-free trust-tier lint stage exited 1 with
1,923 findings (**/tmp/aca-corrected-final-static-gates/20260909T233154Z-aca-replica-residuals-2404264/summary.txt**). This is not an all-gates-green
or operator-signature claim. The earlier attempt with 74 default-suite and
6 PostgreSQL failures is corrected history, not the current result; the final
results above follow those corrections. The subsequent documentation-only
closeout does not change the tested Python sources. Verification is local and covers the
bounded A–E implementation and preserved F receipt contracts. It does not
produce a live ACA receipt, qualify routing without affinity, promote a frozen
state-engine catalog, or close the direct CLI runtime-fingerprint residual
`elspeth-f321e3ff21`.
