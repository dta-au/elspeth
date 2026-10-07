# Current HTTPS Composer readiness — 2026-10-07

**Current disposition: WIP SOURCE / NO-GO for merge.** This document is proposed for an isolated publish branch based on `origin/main` `43280d2db0d2f9c8b1671609bedb911b17748ed5`; no candidate commit existed at preparation. Authenticated live progress followed by the completed answer is the settled scope. Actual Astra has bounded SOURCE GO for the reviewed 0.8.3 release successor and browser runner 4, without replacing historical Daybreak decisions. The first focused release run ended at 286 passed and one Azure skill version failure; a successor and rerun are owed. Canonical static gate 2 passed frozen, and focused policy runs passed 10 and seven tests, each with their recorded source epoch. The lost 727-case foreground attempt remains UNKNOWN. Final default pytest, serial PostgreSQL, TLS7 under default-suite load, browser runner 4 native/local acceptance, operator signature verification and protected PR checks remain owed. Aurora success is user-reported only; external Playwright remains pending without a verified exact SHA or digest. No merge or deployment clearance is claimed.

See the [current source entry](../2026-09-20-composer-async-operations.md), [WIP checkpoint](in-progress-checkpoint-2026-10-07.md), and [102-row obligation ledger](current-obligation-proof-ledger-2026-10-06.md). The [four supplemental collections](supplemental-obligation-collections-2026-10-07.json) retain 188 historical entries, not current-source validation. Two unavailable original reports remain UNKNOWN. The link inventory records 57 nonportable `file:line` references, ten missing evidence/capture targets and twelve deliberately excluded draft/raw/bulk evidence targets; it does not prove all references work or full custody. Historical first-phase and prior-baseline text below remains intact and retains its original date and scope.

After the separately reviewed ACA skill receipt-path correction, the affected ACA runbook/skill contract selection completed 39 passed in 4.57 seconds, exit0, with zero failures, errors or skips and exact source pre/post checks. The source epoch contains362 paths. The earlier286-pass/one-failure release run remains preserved. This does not establish a fresh287-case pass or close the remaining full-suite, TLS, PostgreSQL, browser, image, signature or protected-PR obligations.

## Retained first-phase and prior-baseline evidence

**Current status:** Actual Daybreak Blue final plan review is GO, CLI exit 0, with B1–B5 closed. See [current reconciliation](reconciliation-2026-10-06.md) and the [complete verdict](daybreak-plan-final-verdict-2026-10-06.md). The following is retained prior-baseline evidence; its old pending statements are historical. Application implementation and original-report recovery remain incomplete.

# HTTPS Composer streaming: first-phase readiness — 2026-10-06

**Verdict: NO-GO for implementation.** The executor can inspect the repository and create isolated work, but the implementation contract is not reconciled, source-artifact recovery is incomplete, and genuine Daybreak plan review has not run. This is first-phase readiness, not a substitute for the final plan or its independent review.

## Custody and boundaries

The [complete parent packet](recovery-2026-10-06.md) is preserved verbatim. The pending task's full [19-part expanded export](pending-task-export-2026-10-06.md) has now arrived and been preserved; its [23 proposed documents](draft-export-2026-10-06/README.md) are reconstructed separately, with all 21 task pages, nine previously missing pages, five supplied errata and all 13 contract hunks verified. Receipt does **not** establish complete custody of the original two review documents: those remain unrecovered after parent reported NOT_FOUND. Preserve source conversations/artifacts until parent verifies replacement custody. No fine-grained finding is dismissed as redundant. Reconcile recovered draft errata and exact retry integration before final plan review.

Implementation requires: recovery gaps addressed or explicitly resolved with John; a concrete revised index, contract and full task bundle; product scope settled; genuine Daybreak review with blockers resolved. Sol implements after those gates. Astra reviews application/design/correctness; Daybreak reviews authentication, authorization, transport and trust boundaries as appropriate. Hold the branch for John's local testing. No merge, deployment, production/paid-provider tests, force push, infrastructure/security configuration change or new credentials/access grants.

## Executor and branch evidence

Commands used the explicit worktree CWD. `git fetch origin main` succeeded and `git rev-parse --verify origin/main^{commit}` returned:

```text
edc844699a350a90a624089e12a5a1e75b7b2dde
```

Branch: `work/composer-https-streaming-20261006`.
Worktree: `.claude/worktrees/composer-https-streaming-20261006` under the main checkout.
Its HEAD is the fetched SHA. Main stays on `main`; its status remained:

```text
?? BUGREP.md
```

No implementation, dependency installation, tests, commit, push, PR publication or merge occurred in this phase. Only recovery/readiness documentation is added. The shared `.venv` was linked without modifying its packages. Read AGENTS.md, CONTRIBUTING.md whole-tree gate guidance, maintainer toolchain and relevant `.agents/skills` (orchestrator, lane-manager, logging-telemetry-policy, design).

Measured executable versions:

```text
codex-cli 0.160.0
gh version 2.100.0 (2026-09-03)
node v24.13.0
npm 11.6.2
Python 3.13.15
pytest 9.0.3
frontend_node_modules_exit=1
```

Node/npm satisfy the checked-in package's declared `>=24 <25` and `>=11 <12` ranges. Frontend dependencies are not installed in this worktree. With both source roots on PYTHONPATH and bytecode disabled, imported module paths relative to the measured worktree were:

```text
elspeth=src/elspeth/__init__.py
elspeth_lints=elspeth-lints/src/elspeth_lints/__init__.py
metadata controls: sessions=present; __readiness_known_absent__=absent
composer_async_operations_registered=False
SESSION_SCHEMA_EPOCH=71
```

The import/metadata probe is not a runtime or test-suite verdict. It used the owned live SQLAlchemy metadata with present/absent controls. `_COORDINATION_HARD_CUT_EPOCH = 71` was also read at `src/elspeth/web/sessions/schema.py:45`. Re-read both constants before any schema change.

Host `uptime` reported load `7.24, 7.56, 7.47`. The sandbox exposes only its own process namespace, so the process listing does not establish host test ownership or absence of an active suite. PR276's archive owner retains capacity priority. No broad suite is started. Before later tests, coordinate capacity; one broad suite at a time, focused pytest initially serial (`-n 0`), Playwright sequential per worktree.

## Plan inspection and missing tasks

The [index](../2026-09-20-composer-async-operations.md) goal and contract R0 specify 202 plus polling, with streaming deferred. The execution notes still use `release/0.8.1` and an obsolete branch/worktree. They are historical instructions and cannot be followed for this task. The approved base is current `origin/main`; no release version or deployment task is inferred.

A filesystem existence probe controlled with present `N00.md` and absent `__readiness_known_absent__.md` returned:

```text
N00: present    N01: present    N02: MISSING    N03: MISSING
N04: MISSING    N05: present    N06: present    N07: present
N08: MISSING    N09: MISSING    N10: MISSING    N11a: present
N11b: present   N12: present    N13: present    N14: present
N15: present    N16: present    N17: MISSING    N18: MISSING
N19: MISSING
```

Retain the useful durable-operation design: saved jobs/outcomes, admission validation/idempotency, COMPOSE leases/fencing, explicit cancellation, audit-before-cancel, atomic terminal publication and reaper/recovery. The missing documents are now recovered in the separate draft archive. Reconcile them and the surviving detailed tasks before replacing the active bundle; do not execute the unreconciled drafts.

## Finding-by-finding disposition

All 12 packet findings remain mandatory plan inputs. “Rechecked” below means the cited source/metadata was inspected locally; none is declared fixed or independently tested.

| Packet finding | Local evidence / recovery status | Required disposition before implementation |
|---|---|---|
| 1. Async planned, synchronous runtime | Rechecked route declarations at `sessions/routes/messages.py:132` and `sessions/routes/composer/compose.py:92`; both return `MessageWithStateResponse`. Live metadata does not register `composer_async_operations`. Frontend `api/client.ts:952–988` awaits JSON. | Reconcile N02–N07 and N11–N15 as durable foundation. Existing artifact-download streaming and run WebSocket are distinct surfaces. |
| 2. Disconnect cancellation | Rechecked watcher mounts at `messages.py:590`, `composer/compose.py:397`; `_helpers.py:2362` documents and implements route-task cancellation. | N08/N11/N12 extract app-owned lifecycle. Delivery loss detaches observation; explicit Stop cancels durably. Preserve attached LLM-call audit custody and cancellation/completion race semantics. |
| 3. Progress identity/custody | Rechecked `composer/compose.py:172–179`: request ID is prior conversational user-message ID. `composer/progress.py` stores session-scoped latest snapshots. | Give each recompose its own operation/lease-bound identity. Distinguish idempotency, message, operation and progress identities; pin repeated-recompose isolation and reconnect custody. |
| 4. Live stream authorization | Rechecked Bearer injection at frontend `api/client.ts:88–97`; routes use `require_pipeline_user` and ownership at entry. No new stream implementation exists here. | Define live ownership/tenant/operation checks, revocation and expiry behavior. Fetch-consumed SSE is proposed, not finalized. No silent WebSocket substitution. Review through real Daybreak. |
| 5. Progress versus generated answer text | Rechecked forbidden progress content in `composer/progress.py:1–16`; provider gateway `:482–489` awaits/adopts complete completion; gateway contract documents rejection of `stream`. | Parent owns pending product clarification. Do not assume progress-only scope. Generated text needs separate compatibility/admission/security treatment if required. |
| 6. HTTPS delivery proof | Rechecked `deploy/compose/nginx.conf:35`: `proxy_buffering off`. No browser/proxy measurement performed. Operator-local Caddy configuration is not recovered. | Add actual local HTTPS acceptance with observed chunk before terminal settlement; heartbeat, buffering, fragmentation and truncation coverage. Configuration inspection is insufficient. |
| 7. Effective backpressure bounds | Inherited concern; prior detailed limits not exported. No claim of a verified send-bound implementation. | Define and measure actual ASGI send bounds plus connection/subscription/frame/queue limits and cleanup. Producer timer alone is insufficient. Recover or re-establish explicit limits, then Daybreak review. |
| 8. Database-dependent progress durability | Rechecked app wiring: `DatabaseComposerProgressRegistry` at `app.py:1913`, in-memory `ComposerProgressRegistry` at `:1936`. | Define PostgreSQL cross-instance intermediate observation separately from SQLite restart recovery of durable terminal; do not promise event replay absent proof. |
| 9. Existing history/recompose protections | Rechecked `composer/compose.py:144–196`: full `limit=None` history, conversational filtering, last-user check, expected-user ID check. | Preserve these through async preconditions/start. Require parity tests. Do not duplicate unpublished retry patch without exact artifact. |
| 10. Admission before persistence | Rechecked message ingress credential guard `messages.py:158` and compartment ingress `:167`. | Add both protections before queued request JSON is persisted, including any derived recompose request material. Strict DTO checks alone are insufficient. |
| 11. Principal-scoped browser custody | Proposed requirement inherited; not claimed implemented. | N14/N15 must define principal/provider binding, bounds/retention, logout cleanup, same-principal token refresh and principal-replacement rejection; test existing auth reset integration. |
| 12. Epoch reconciliation | Live metadata reports 71; schema hard-cut constant is 71. | Restore N18 around live-main reconciliation and re-read-before-change, without assuming stale release or deployment authority. |

For later delivery design, preserve the packet's refinement: bounded progress stream carries terminal **notification**, and authenticated durable GET returns the potentially large final result. Reconnect or terminal replay must never dispatch another provider turn merely because transport failed. Short GET polling remains recovery/fallback. Provider authors proposals; no server graph synthesis; tutorial follows ordinary backend and provider calls are measured per transition.

## Acceptance and verification to carry forward

No item below has run or passed in this phase. The final revised plan must assign ownership and concrete assertions for each:

- Local HTTPS/proxy chunk before terminal settlement; heartbeats and buffering; UTF-8/decoder fragmentation, split frames, truncated/aborted stream handling.
- Disconnect, reload, reconnect, terminal replay; duplicate admission with stable idempotency; distinct repeated recomposes; cancellation/completion races; no provider replay from delivery/retry/recovery.
- Ownership, tenant/session/operation isolation, token expiry, live revocation; bounded subscriptions, connections, frames, queues and actual slow-consumer send/backpressure.
- Timeout cleanup, safe errors and redacted observability; logging/telemetry policy and audit primacy; exclude raw tool arguments/results, secrets and reasoning from progress.
- SQLite/PostgreSQL contention, crash windows, fencing and atomic terminal publication; explicit differences in intermediate durability.
- Frontend decoder/store/auth/persisted custody; full-history and recompose guards; credential/compartment protection before persistence.
- Provider-authored structure and ordinary tutorial path, with provider-call assertions per transition.
- Whole-tree gates applicable to changed inputs, normal CI, keyless trust-tier finding-set comparison and operator-only signing. Persistence/locking work requires PostgreSQL testcontainer selection, serially; default pytest does not cover it.

Starting paths confirmed by `git ls-files` (inventory only, no collection or execution):

```text
tests/unit/web/sessions/test_routes.py
tests/unit/web/sessions/test_freeform_route_custody.py
tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py
tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py
tests/unit/web/sessions/routes/test_composer_request_telemetry.py
tests/unit/web/composer/test_progress.py
tests/testcontainer/web/test_cross_process_composer_postgres.py
src/elspeth/web/frontend/src/api/client.recovery.test.ts
src/elspeth/web/frontend/src/stores/sessionStore.test.ts
src/elspeth/web/frontend/src/stores/authStore.test.ts
```

These paths do not recover the pending task's complete proposed baseline inventory. Inspect and extend after export; never treat this starting list as full coverage.

## Dependencies and reviewer readiness

Read-only `gh pr view --repo dta-au/elspeth --json ...` returned:

```text
PR275 state=OPEN isDraft=true base=main
head=240f71d4956f6fa1953a9d35fe41072e11a9b2a8
branch=chore/release-0.8.2-metadata
PR276 state=OPEN isDraft=true base=main
head=d58482d652ab4a339fa1dd192b04ed7d04c0d027
branch=fix/efs-archive-20261006
```

PR276 changed-file API lists `src/elspeth/web/app.py`, `sessions/service.py`, `sessions/protocol.py`, `sessions/locking.py` and `coordination/repository.py`, among its archive-specific changes. These intersect async lifecycle/persistence work. PR275 owns release/dependency/provenance repair. Leave both owners and their main-bound merge authority intact; consume landed main after refresh. Do not duplicate fixes or absorb either PR. The unpublished retry candidate has now been received and hash-verified through Library; its uncommitted patch identity and assessed semantic overlap are recorded in [retry integration](retry-integration-2026-10-06.md). It remains unapplied. That update also records refreshed PR276 head `bba870e35d90d301f1485c6130389cd30b40767f`; the preceding PR output is the earlier first-phase observation. Other integration needs exact artifact/overlap review on the held branch.

John clarified through parent on 2026-10-06 that “Daybreak” means the actual **Daybreak Blue model**, whose discovered CLI slug is `gpt-daybreak-blue-latest`. A security product/workflow or another model cannot satisfy this review requirement. The existing plan-review and payload-approval gates remain unchanged. Installed Codex CLI exposes `exec --model ... --sandbox read-only`; local model cache contains that exact slug. Cache presence is not successful execution or entitlement. The earlier advisory Codex Security access result (`status=unknown; programs=none`) concerns a separate product and has no bearing on the required model-review gate. No Daybreak invocation or private-code review transmission has occurred. The archive payload denial is a separate unresolved boundary and is neither retried nor bypassed here. Once the concrete plan exists, identify exact payload, destination and authorized reviewer route. If actual Daybreak access/transmission is denied, pause that step and report the exact blocker; never label Astra or another model Daybreak.

The worktree requires explicit filesystem escalation for writes under this executor's permission profile. Fetch, isolated worktree creation and durable documentation writes were authorized successfully by automatic review. No rejected action is worked around.

## Next authorized phase

Expanded export and exact retry artifact are now received; reconcile each item with this packet without losing its disposition, and resolve the original two-report recovery gap. Refresh main/dependency heads before plan finalization. Resolve product scope through parent. Then revise the existing plan/contract/tasks against actual code and establish explicit resource/auth/transport contracts. Obtain genuine Daybreak plan review and resolve blockers. Implementation remains on hold until those conditions are met, and any later implementation remains held for John's local testing.

## Current reconciliation update

The [current-source reconciliation](reconciliation-2026-10-06.md) now records resolved draft errata, restored active tasks, exact candidate-aware recompose/custody semantics, local dependency preparation and completed bounded baseline results. Earlier pending statements above are historical observations. Original two-review coverage, product scope, remaining runtime evidence, review-payload approval and actual Daybreak Blue review remain open; candidate/application code remains unapplied and the no-merge/local-testing hold remains.

## Independent N00 measurement update

See [current measurement evidence](n00-measurements-2026-10-06.md) and [registered handlers/direct exits](appendix-a-2026-10-06.md). These supersede earlier pending-baseline statuses only within their measured scope. Browser body/aggregate proposals now use 512 KiB / 1 MiB after current accepted fixtures exceeded 256 KiB. Product choice, original two-report custody, review-payload authorization and actual Daybreak Blue plan review remain open; implementation is NO-GO. Original raw findings, complete historical task analyses and exact unapplied retry evidence remain retained.
