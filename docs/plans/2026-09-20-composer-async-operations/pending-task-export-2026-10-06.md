# Pending-task expanded plan export — 2026-10-06

Complete 19-part numbered export received from source task `01a10f36-0226-7788-b930-84ca87b9e0f9`, relayed by parent. These are supplied drafts/observations, not adopted contract or implementation approval. All 19 parts are present and checked; preserve each received part verbatim. The [reconstruction](draft-export-2026-10-06/README.md) retains raw and decoded payloads, 23 proposed documents and supplied errata. The original two review documents remain a separate recovery gap.

---

**Transfer 01/19 — custody, authorization and source**

Destination: Nyx thread `01a10f38-def3-7203-9dfe-a1c58b6520e3`. Source task is this conversation; parent source thread `01a10aef-253f-7248-9619-7bb7271430ed`.

No persisted changes exist: no checkout/worktree/branch/commit/PR/source edits/filesystem artifacts. Drafts were only conversation/tool-memory content; tool stores did not survive the turn boundary. This numbered retransmission and preceding final are the available custody. No tests, runtime inventories, local import checks, CLI checks, HTTPS measurements, implementation agents or reviews ran. No review payload was transmitted.

Environment `ccarenv_b64_Y2NhcmVudl8wMzAwMmI1MmVlMzA4MTkxODFmNWEwZDA2Yzk4ZWJhYg` remained starting without terminal failure. No execution/filesystem tool appeared. Wait cell18 was not explicitly terminated/cancelled. Parent authorized stopping further waiting after transfer; nothing was deleted or cancelled.

**Daybreak means Daybreak Blue `gpt-daybreak-blue-latest`. Astra is not a substitute. Transfer authorizes no implementation or review transmission.** Existing archive transmission approval is separate and pending.

Remote main baseline: `edc844699a350a90a624089e12a5a1e75b7b2dde`, repeatedly verified via GitHub connector, latest dependency refresh `2026-10-06 02:59:18 UTC`. No local HEAD exists.

Dependencies at last observation:

- PR275 open draft, head `83d527a469ea29af5b01564be95160eba5644cb6`, `chore/release-0.8.2-metadata`: independent version/deps/provenance owner and main-merge authority.
- PR276 open draft, head `d58482d652ab4a339fa1dd192b04ed7d04c0d027`, `fix/efs-archive-20261006`: independent EFS archive/recovery owner and main-merge authority. Overlap: `src/elspeth/web/app.py`, `src/elspeth/web/coordination/repository.py`, `src/elspeth/web/sessions/service.py`, `src/elspeth/web/sessions/protocol.py`.
- Retry child `01a10e53-99a2-7242-bdfa-2282432c20dd`: parent must provide exact candidate SHA; preserve GET-only reconciliation/full-history/recompose guards. Do not identify a commit from thread ID.
- Consume PR275/276 through freshly updated main after independent landing, or explicitly coordinate temporary integration on held review branch. Do not redirect/duplicate owner work.
- Other open #272/#271/#243/#242/#240 are outside identified integration set.

Required order: usable workspace/local provenance → reconcile existing readiness bundle/product choice/overlaps → resolved payload approval → **REAL Daybreak Blue plan review and blocker resolution** → Sol implementation workers → Astra correctness/design plus Daybreak Blue transport/security review → tests → exact-HEAD held branch for John’s LOCAL testing.

No merge/deploy, protected bypass, new access/credential grants, infrastructure changes, paid-provider or production tests.

---

**Transfer 02/19 — substantive source findings and proposed treatment**

Paths are relative to pinned `dta-au/elspeth` main. Approximate lines are navigation only. These are connector source observations/design implications, not tested fixes or validated vulnerabilities.

- `src/elspeth/web/sessions/routes/messages.py`, send around136/disconnect guard590: synchronous final `MessageWithStateResponse`; credential material refused before writes; compartment ingress, rate limit, ownership and COMPOSE lease already exist. Preserve refusal boundaries before queued body persistence;202 only after durable admission.
- `sessions/routes/composer/compose.py`, recompose around96/disconnect397: **already** full history `get_messages(limit=None)`, `_composer_conversation_messages` audit filtering, empty/non-user/expected-user-message guards. Retain at atomic start with bound-state check before side effects. Do not truncate history or add recompose user row.
- `sessions/routes/_helpers.py`, `_cancel_on_client_disconnect` around2362: socket cancellation currently owns turn lifetime. Detached worker must replace it at full cutover; reader disconnect ends subscription, explicit cancel POST is Stop.
- `_track_compose_inflight`/CRL: preserve renew/revocation/telemetry/durable-completion semantics. Inflight is advisory, never settlement authority.
- `_verify_session_ownership` around2936: missing/archived/wrong user/**wrong auth provider** all opaque404. Preserve scope for poll/stream/cancel.
- `auth/middleware.py`: Bearer provider authentication and live human-role authority. Convenience `request.state.auth_claims` are **unverified**; never authorize streams from them.
- `sessions/routes/composer/state.py` progress GET around530 and `coordination/composer_progress_authority.py`: committed identity/ownership authority, PostgreSQL REPEATABLE READ snapshot/request token/generation. Reuse, do not bypass with local stream memory.
- `composer/progress.py`, progress models: latest snapshot per session; `request_id` is user-message ID. Repeated recomposes of the SAME message therefore alias unless exact operation/worker-lease bound. Snapshot JSON bound16384 chars. Do not relabel session-latest progress.
- `app.py` around1913: PG database-backed progress; SQLite in-memory progress plus single-process membership. Durable terminal replay both; restarted SQLite may lose advisory progress. No claim of shared SQLite progress.
- `composer/provider_gateway.py` `_litellm_acompletion` awaits complete LiteLLM completion. Gateway `core/contract.py` ChatRequest extra-forbid rejects `stream`. Answer-text-as-generated requires product decision and provider/audit/usage/redaction/retention review.
- `async_workers.py`:16 running+16 queued admissions, one-second admission wait. Cancelled queued work may be removed; running thread cannot be interrupted and retains slot until completion. Test SQL late commit after timeout; bound driver/read waits and pool contention; never falsely free slots.
- Consequently, a later `composer_operation_active` refusal does **not** prove an earlier ambiguous in-flight admission cannot commit. Retain earlier exact ID/body reconciliation separately until resolved.
- Frontend `src/elspeth/web/frontend/src/api/client.ts`: send carries `client_request_id`; recompose only expected-user-message ID today. `stores/sessionStore.ts`, `sessionOperationRetry.ts`, `hooks/useComposer.ts` share send/retry/progress/Stop. One observer and reducers for stream+poll; submitted exact-body custody distinct from observation-only attachment. Never re-key losing body under another accepted ID.
- Frontend `authStore.ts` reset around92–122 and auth-generation guards: body custody principal/provider-scoped; purge logout/principal replacement; same-principal refresh may resume.
- `sessions/models.py` SESSION_SCHEMA_EPOCH71 and `sessions/schema.py` hard-cut epoch71. Re-read before coherent necessary bump; stale72 plan instructions unsafe.
- `deploy/compose/nginx.conf`: TLS443, buffering off, read/send360; logs exclude query args. Source headers/config are insufficient proof of real chunks.
- Caddy development config/service is operator-local/gitignored; runbook refers `/run/elspeth/uvicorn.sock`. Inspect local prerequisites only, no deployed infra change.
- Existing bundle old release0.8.1 and202+poll/stream-deferred; N00 skeleton; missing N02/03/04/08/09/10/17/18/19. Surviving templates historical. Update existing bundle and preserve histories/panel/findings.

---

**Transfer 03/19 — blockers, errata, acceptance and instructions**

NO-GO: usable local workspace/provenance; unanswered user choice generated answer text vs safe progress+final; actual Daybreak Blue CLI/tool availability; applicable payload approval; reconciled internally consistent plan; exact retry SHA/overlap coordination; REAL Daybreak Blue plan review/blocker resolution. Daybreak tool absent from exposed tools; CLI **unknown**, not proven absent, because environment unavailable. No auto-review rejection occurred.

Draft errata to resolve **before review**:

1. Retained contract frontend snippet still “one body-bearing descriptor per session”; detailed new HTTPS clause/N15 require submitted-vs-observation sum type **and separate unresolved earlier-submission custody**. Harmonize.
2. Retained contract N08 dependency N00 conflicts updated page N00,N03.
3. Index “Measured baseline” is source observations only; relabel “Source-verified baseline” pending N00 runtime measurement.
4. Historical line numbers, route/handler inventories, DML/audit classifications, byte parity, p50/p99 sizes and golden vectors unmeasured on live local tree.
5. Bounds and interfaces proposed/unreviewed; generated-answer behavior unapproved.

Acceptance: SQLite+serial PostgreSQL fault/race/claim/start/terminal/cancel/cascade/fence proofs; crash after dispatch without requeue/takeover; atomic audit/publication/terminal and immutable hash-validated replay; reaper zero provider calls; SQL late commit and earlier ambiguous custody race; live identity/role/token/provider/owner/archive changes; no frame after failed auth check; short snapshots/no DB connection across socket awaits; **ASGI send-boundary** timeout, bounded pool/tasks/memory and subscriber cleanup; UTF8/CRLF/multiline/multiframe/truncated/oversized/invalid decoder controls; duplicate terminal/auth generation/logout/refresh/reload/two-tab/session-switch/Stop races; local TLS early chunk before delayed fake completion; deliberately buffering/short-idle proxy polling same job without provider replay. Fake providers/isolated DBs only.

Verified existing tests, **none run**:
```text
tests/unit/web/sessions/test_routes.py
tests/unit/web/sessions/test_operation_fence_wiring.py
tests/unit/web/composer/test_progress.py
tests/unit/web/composer/test_wire_fidelity_matrix.py
tests/unit/architecture/test_session_db_mutation_authority.py
src/elspeth/web/frontend/src/stores/sessionStore.test.ts
src/elspeth/web/frontend/src/stores/sessionOperationRetry.test.ts
src/elspeth/web/frontend/src/api/client.auth-races.test.ts
src/elspeth/web/frontend/src/hooks/useComposer.test.ts
```

Read AGENTS.md and all CONTRIBUTING.md; skills elspeth-design, orchestrator+runbook, lane-manager, logging-telemetry-policy, config-contracts-guide. Keep dedicated worktree; actual import provenance from src and elspeth-lints/src; `uv sync --frozen --all-extras`, root/frontend npm ci, Node24/npm11; no stash/shared checkout switching; stage owned paths; branch-safety before commit/rebase/push; no no-verify; frozen-tree affected whole-tree checks; serial PG testcontainers; key-free lint set diff; positive/negative live measurement controls; sequential Playwright; provider-authored proposals/ordinary tutorial path.

Retained N00 M0–M12: SHA, epoch, overlaps, key-free lint set, writer manifest, focused baseline, request/result quantiles, all POST callers incl omitted state with head, hot auto-title post-terminal writers, gateway/UPSTREAM_TIMEOUT, unmodified receipt vectors, PG cascade control, auto-title timing. Registered route/handler exit Appendix A also required.

Payload boundaries: A=04–07 (concatenate code contents); B=08–13 (concatenate diff contents); C=14–18 (pages separated by filename markers). Final receipt instructions=19.

---

**Transfer 04/19 — payload A, index part1/4**

Target `docs/plans/2026-09-20-composer-async-operations.md`. Concatenate04–07 code contents. Unpersisted/unreviewed; errata in03.

```markdown
# Composer async operations and HTTPS delivery readiness plan
Source baseline: dta-au/elspeth main edc844699a350a90a624089e12a5a1e75b7b2dde, verified 2026-10-06.
Status: readiness proposal; implementation is NO-GO until the gates below close. No merge or deployment is authorized.

## Scope and delivery gate
Update this existing bundle; preserve the durable job design and integrate HTTPS delivery with it.
The cutover remains POST /api/sessions/{session_id}/messages and /recompose, across every ordinary authoring surface including the tutorial. The provider remains the author of pipeline structure. No server graph synthesis, tutorial-only path, duplicate provider dispatch on reconnect, unrelated dependency/version/provenance/archive work, infrastructure change, credential grant, paid-provider test, or production test is included.

Before implementation:
- Verify the selected workspace is usable; fetch main and record exact local/remote SHAs and source provenance.
- Resolve the user decision: generated answer text versus provider-safe progress followed by the final answer.
- Reconcile every affected plan task and contract clause with source; restore the missing task documents.
- Obtain REAL Daybreak review of this readiness proposal and resolve its blockers. Approval to transmit a review payload must be resolved before transmission. Astra does not substitute for Daybreak.
- Parent supplies the exact unpublished retry candidate before integration into the held review branch. PR 275 and PR 276 keep independent main-merge authority and owners: consume updated main after they land, or use an explicitly coordinated temporary integration. Do not redirect or duplicate their work.
After those gates: Sol implementation workers, Astra correctness/design review and Daybreak transport/security review. Hold the verified branch for John's LOCAL testing. No merge, deploy, protected-check bypass, or new access.

## Measured baseline and source anchors
- messages.py send_message mounts MessageWithStateResponse, rejects credential-bearing ingress before writes, and guards compose with _cancel_on_client_disconnect. Preserve the existing compartment/credential admission boundaries before any async request_json is persisted.
- routes/composer/compose.py recompose has the same socket-owned cancellation. It already reads full history with limit=None, filters conversational rows, and rejects empty history, a non-user final conversational row, or an unexpected user-message ID; move those guards into the atomic start without weakening them.
- frontend api/client.ts sends client_request_id and recompose sends expected_user_message_id without a bound state.
- frontend sessionStore.ts owns send/retry, request cancellation, progress polls and accepted-send reconciliation. Preserve GET-only reconciliation, full-history and recompose guards from the independently reviewed unpublished candidate once coordinated.
- routes/composer/state.py get_composer_progress uses require_pipeline_user and ownership checks, then registry.get_latest.
- auth/middleware.py get_current_user authenticates a Bearer header; require_pipeline_user checks a live user role.
- composer_progress_authority.py rechecks committed identity and ownership for a progress snapshot. The existing snapshot is session-scoped and already has request_token/generation fields; user-message request_id alone cannot distinguish repeated recomposes. app.py uses database-backed progress on PostgreSQL and in-memory progress on SQLite; durable terminal recovery applies to both, while restarted SQLite may lose intermediate advisory progress.
- composer/provider_gateway.py _litellm_acompletion returns a complete LiteLLM response to admission; gateway core/contract.py rejects unsupported stream fields. Generated answer deltas require an explicit provider-path decision.
- async_workers.py has 16 running plus 16 queued shared slots, a one-second wait to acquire admission capacity, and cancellation that can remove queued work but cannot interrupt a running thread. Preserve worker-lifetime admission accounting; prove late admission commit and subscription/worker contention with real faults.
- sessions/models.py SESSION_SCHEMA_EPOCH and sessions/schema.py _COORDINATION_HARD_CUT_EPOCH are both 71. Re-read before any implementation bump.
- deploy/compose/nginx.conf terminates TLS and disables proxy buffering. The development Caddy config is operator-local/gitignored. Source settings do not prove live chunk delivery.
- main tree lacks N02, N03, N04, N08, N09, N10, N17, N18, N19. N00 is a skeleton. Detailed surviving task examples use historical source anchors and require reconciliation.
- PR 275 is an open draft for 0.8.2 metadata/dependencies/provenance; PR 276 is an open draft for EFS archive/recovery. Neither is part of this patch.

```

---

**Transfer 05/19 — payload A, index part2/4**

```markdown
## Durable operations retained
Admission commits a bounded queued job after authentication, ownership, strict body validation, idempotency comparison and capacity checks. One client operation_id identifies one action and immutable body/base. Same id/body reattaches; mismatch conflicts. Database constraints admit at most one nonterminal job per session.
Start atomically binds the job to the COMPOSE SessionOperationLease, checks owner/base/recompose transcript before side effects, and enters running. Running work never returns to queued or gets provider replay. Queue time consumes the absolute database-clock deadline.
A positive fence predicate blocks non-audit writes after cancellation or terminal settlement. Retain required audit-only writes and join owned children before settlement.
Publish final assistant data, audit cohort, proposal/state projection and terminal result atomically. The durable job result and strict result hash are the settlement authority. Progress, connection liveness and inflight counts cannot settle a turn.
Explicit cancel markers, lease loss, bounded drain and the reaper retain their different meanings. A disconnect abandons only a subscription after async cutover; only the explicit cancel endpoint expresses Stop. Reaping terminalizes lost work without another provider call.

## Common HTTPS delivery contract
POST accepts quickly with 202 after the durable insert. The operation-scoped authenticated GET remains the authoritative poll and final-result replay endpoint. Scope any body-bearing browser custody to the authenticated principal/provider, preserve the existing 256 KiB/24-hour proposal bounds, and purge it on logout/principal replacement; token refresh for the same principal may resume it. Add GET /api/sessions/{sid}/operations/{operation_id}/stream to deliver status and provider-safe progress incrementally from the same job and progress sources.
Use fetch with Authorization headers and the existing auth-generation guards; do not put credentials in URLs or introduce access grants. Proposed framing is UTF-8 Server-Sent Events consumed through fetch, not browser EventSource. Decode complete frames across arbitrary byte fragmentation and coalescing.
Bind each progress publication to the operation_id or exact worker lease, with provenance retained in the snapshot. A session's latest progress must never be relabelled as the requested operation. Queued jobs get status from their own job row, without borrowing a prior turn's progress. Existing user-message request_id may remain a separate correlation field.
Authenticate and check role, deployment/provider scope, live identity, session ownership/archive state and requested job before headers. Recheck live authorization at bounded intervals and before each emitted data frame; token expiry closes the subscription. Auth revocation and transport failure leave durable work settlement to the worker's existing revocation/lease rules. No result or progress is sent after a failed authorization check. Use redacted control frames or clean closure after headers; never exception strings.
Poll and stream read short committed snapshots; hold no database transaction, row lock or connection across socket writes or heartbeat waits. Cross-instance reconnect reads committed job/progress data, not another process's memory. No events table is required for snapshot-only progress.
Send bounded heartbeats below the supported proxy idle ceiling, bound subscription lifetime, read cadence, frame size, write wait, per-user/per-operation/per-instance subscriptions and reconnect backoff. Coalesce obsolete progress; never let a slow reader block the worker, retain unbounded queues or provider data, or leak permits/tasks on disconnect. Defaults and limits must be measured against the ordinary worker pool and reviewed by Daybreak.
Terminal notification leads to the same authenticated durable final GET, avoiding unbounded graph/result frames. This preserves the former MessageWithStateResponse and public error body. Duplicate, late or reordered frames cannot apply terminal reducers twice. Reconnect may skip intermediate advisory snapshots; terminal replay is durable.
Set no-store and disable applicable proxy buffering/compression transformations for the stream. Verify emitted bytes through a real local TLS proxy with a delayed fake composer; headers and an ASGI-only test do not establish incremental HTTPS delivery.
On EOF, idle timeout, malformed stream data, blocked/buffering proxy or unsupported streaming, close that reader and poll the same operation. After ambiguous submission, poll first; resubmit only the same id and immutable body if the operation is absent. A composer_operation_active refusal attaches an observation-only descriptor for the existing action: never relabel the losing unsent body with that ID, and never resubmit an observation-only descriptor. Retain separate reconciliation custody for any earlier still-ambiguous submitted action; an active-operation refusal from a later attempt alone does not prove that earlier attempt cannot commit. Never resubmit a missing/archived session or mint a new action due to transport failure.
Transport/watchdog expiry cannot silently erase operation custody or claim cancellation. Stop posts explicit cancel and continues observing until a durable terminal. A committed terminal always wins a late cancel.


```

---

**Transfer 06/19 — payload A, index part3/4**

````markdown
## Control and delivery flow

```mermaid
flowchart TD
  Client[Authenticated client] --&gt; Admit[Validate and admit immutable action]
  Admit --&gt; Queue[Durable queued job]
  Queue --&gt; Start[Atomic preconditions and COMPOSE lease start]
  Start --&gt; Turn[Provider-authored composer turn]
  Turn --&gt; Commit[Atomic audit, publication and terminal]
  Commit --&gt; Final[Authenticated durable result GET]
  Queue --&gt; Observe[Operation-scoped status and safe-progress stream]
  Turn --&gt; Observe
  Observe --&gt; Client
  Observe --&gt;|disconnect or transport failure| Poll[Poll the same operation]
  Poll --&gt; Final
  Client --&gt; Stop[Explicit cancel POST]
  Stop --&gt; Marker[Durable cancel marker]
  Marker --&gt; Audit[Worker joins required audit]
  Audit --&gt; Commit
```

The subscription owns its reader resources. The COMPOSE lease and durable job own the provider turn. Terminal notification is emitted only after commit and leads to the authoritative result GET.

## Generated answer text decision
Do not infer authorization from the pending question. If required, add a separately reviewed extension for provider-supported final-answer deltas, usage/audit finalization, cancellation, content/secret redaction, retry/attempt discrimination, ordering and reconnect retention. Do not stream reasoning, tool arguments, credentials or intermediate planner proposals as answer text. Gateway contract changes and any retention/storage design must be explicit. Snapshot-only progress does not satisfy generated-answer text; record that distinction in readiness.

## Task reconciliation

The [interface contract](2026-09-20-composer-async-operations/contract.md) and task documents form this bundle. All task interfaces are proposed until the readiness gate closes; historical code templates must be rebuilt against live source.

| Task | Plan file |
|---|---|
| N00 | [N00](2026-09-20-composer-async-operations/N00.md) |
| N01 | [N01](2026-09-20-composer-async-operations/N01.md) |
| N02 | [N02](2026-09-20-composer-async-operations/N02.md) |
| N03 | [N03](2026-09-20-composer-async-operations/N03.md) |
| N04 | [N04](2026-09-20-composer-async-operations/N04.md) |
| N05 | [N05](2026-09-20-composer-async-operations/N05.md) |
| N06 | [N06](2026-09-20-composer-async-operations/N06.md) |
| N07 | [N07](2026-09-20-composer-async-operations/N07.md) |
| N08 | [N08](2026-09-20-composer-async-operations/N08.md) |
| N09 | [N09](2026-09-20-composer-async-operations/N09.md) |
| N10 | [N10](2026-09-20-composer-async-operations/N10.md) |
| N11a | [N11a](2026-09-20-composer-async-operations/N11a.md) |
| N11b | [N11b](2026-09-20-composer-async-operations/N11b.md) |
| N12 | [N12](2026-09-20-composer-async-operations/N12.md) |
| N13 | [N13](2026-09-20-composer-async-operations/N13.md) |
| N14 | [N14](2026-09-20-composer-async-operations/N14.md) |
| N15 | [N15](2026-09-20-composer-async-operations/N15.md) |
| N16 | [N16](2026-09-20-composer-async-operations/N16.md) |
| N17 | [N17](2026-09-20-composer-async-operations/N17.md) |
| N18 | [N18](2026-09-20-composer-async-operations/N18.md) |
| N19 | [N19](2026-09-20-composer-async-operations/N19.md) |

Work on a dedicated held review branch from freshly fetched main. Stage only this task's paths; preserve shared-checkout and both-source-root test discipline. No interim or final merge is authorized.
````

---

**Transfer 07/19 — payload A, index part4/4**

```markdown
N00: source/provenance, ownership overlap, real source inventories and focused baseline; source symbols replace historical line-number scripts.
N01: worker/poll settings and measured bounded stream resource settings; preserve synchronous consumers.
N02: receipt codec golden vectors before extraction; separate codec if parity moves.
N03: strict job/request/status/error/stream DTOs, operation-bound progress, decoder/frame bounds.
N04: job schema/transition/delete guards and indexes; any progress-binding schema change joins one re-read epoch bump.
N05: typed authority, snapshot authorization, bounded claims/scans, idempotency and active-job races.
N06: atomic start, base/transcript rejection before user/provider effects, exact lease adoption.
N07: positive non-audit fence and atomic terminal; audit/cancel and post-terminal negative controls.
N08: app-owned request lifecycle, renewal/revocation and exact progress binding; no Request in worker.
N09: one remaining deadline across queue/compose/planner/settlement; separate transport subscription budgets.
N10: real handler parity and redacted poll/stream errors.
N11a/N11b: per-job services and rebuilt common turn; preserve all route ladder, proposal and owned-child semantics.
N12: worker/reaper/drain, cancellation identity and persisted audit; subscribers cannot own worker lifetime.
N13: authenticated poll/cancel/stream routes and ordinary local helpers; poll and stream share durable source.
N14: complete backend/caller cutover to 202; remove obsolete socket-cancel/recovery paths, retain diagnostic/synchronous consumers correctly.
N15: fetch stream decoder/store/custody fallback and resume; Stop/late-frame/auth/session-switch/multi-tab tests; ordinary tutorial transport; transition-ledger terminal boundary.
N16: document timeout relationships and check shipped proxy expectations; no deployed configuration or infrastructure mutation.
N17: PostgreSQL/SQLite crash/race/fault/revocation/cascade/terminal replay and cross-instance stream proofs.
N18: re-read schema sentinels and apply necessary schema change once; docs/API/generated types only. Do not mix PR 275 release metadata.
N19: required checks and local real-HTTPS/browser acceptance, independent reviews, exact HEAD and branch hold.

## Go/no-go evidence
GO requires approved scope, complete source-grounded task/contract bundle, real Daybreak plan review with blockers closed and functioning normal local prerequisites.
Completion requires focused Python and frontend tests plus affected whole-tree gates; PostgreSQL testcontainer proofs for persistence/lock changes; decoder negative controls for split UTF-8, CRLF/multiline frames, multiple frames/chunk, oversized/invalid/truncated frames and stale operation/auth generations.
Fault proofs cover same-id retries without duplicate user/provider work; cancel versus terminal; worker crash after dispatch without takeover; audit/terminal transaction failures; post-terminal writes; ownership/role/token expiry during a stream; peer leases; reload/reconnect/fallback; slow/disconnected readers releasing all resources.
Local TLS acceptance must observe an early bounded data/heartbeat chunk before final settlement, under fragmentation and short proxy idle limits, plus a deliberately buffering proxy that causes safe polling fallback without provider replay. Use fake providers and isolated local databases, including PostgreSQL. No production or paid-provider tests.
Report exact branch HEAD, base SHA, exit codes and scope of checks, review results and local test steps. A green branch remains held for John's LOCAL testing; no merge/deploy.
```

---

**Transfer 08/19 — payload B, contract patch part1/6**

Concatenate **08–13 diff contents**. Apply to `docs/plans/2026-09-20-composer-async-operations/contract.md` from main `edc844699a350a90a624089e12a5a1e75b7b2dde`, original blob `76bda4c8f7c13394fe9462d9d9070e73f6d90628`. Retains complete original durable design. Prior in-memory reconstruction matched69,149-char draft; this is transfer verification, not code validation.13 hunks total. Parts11–12 split inside one hunk; concatenate without extra separator lines. Draft errata03 applies.

```diff
--- a/docs/plans/2026-09-20-composer-async-operations/contract.md
+++ b/docs/plans/2026-09-20-composer-async-operations/contract.md
@@ -1,7 +1,8 @@
+# Readiness interface contract — composer async operations and HTTPS delivery (2026-10-06)
+
+This is a readiness proposal against current main, not a claim that these interfaces exist. Implementation is NO-GO until the user delivery decision, real Daybreak plan review and its blockers, and parent overlap coordination are resolved. No review payload is transmitted while approval remains unresolved. No merge or deployment is authorized.
-# Interface contract — composer async operations (re-based 2026-09-28)

+It retains the durable jobs, admission, idempotency, COMPOSE leases, cancellation, atomic terminal and reaper design from:
-This contract is what every task file builds against. It fixes the names, signatures, columns, wire shapes and module
-homes. It folds in:

 - the 09-25 decisions;
 - the 09-25 multi-lens review;
@@ -11,11 +12,11 @@
 There are no override tables. An executor who finds a clause impossible against the tree records a `CONTRACT DEVIATION:`
 block in their task file, with the measured reason and the smallest change. They do not rename silently.

+- **Tree:** `main`, verified at `edc844699a350a90a624089e12a5a1e75b7b2dde` on 2026-10-06. Local checkout/provenance remains pending selected-workspace startup. Re-fetch before implementation.
+- **Spec:** `docs/specs/2026-09-16-composer-async-operations-design.md` is the historical durable-operation design input. Its streaming deferral and release-base instructions do not authorize execution against current main.
-- **Tree:** `release/0.8.1`, pinned at `1effedab2` (surveyed; HEAD `6cb338f2f` differs only in `gateway/`).
-- **Spec:** `docs/specs/2026-09-16-composer-async-operations-design.md` (second amendment 2026-09-28).
 - **Evidence:** `findings-2026-09-28/` (`routes.md`, `persistence.md`, `composer.md`, `frontend.md`, `platform.md`,
   `plan-impact.md`, `critique.md`). The 09-25 plan, findings and review are kept in `history/2026-09-25/` for
+  provenance only; none of their anchors is current. Historical line numbers in retained interface examples are orientation only: resolve the named live symbols and current registrations in N00 before implementation.
-  provenance only; none of their anchors is current.

 ## Glossary (binding)

@@ -33,7 +34,7 @@
 | # | Ruling |
 |---|---|
 | Scope | Freeform is the only composer. The cutover set is `POST /{sid}/messages` (`compose_message`) and `POST /{sid}/recompose` (`compose_recompose`). The first-run tutorial's Build uses `/messages`, so the cutover covers it with no tutorial branch (composer invariant 2). |
+| R0 | Current task: 202 acceptance, authenticated operation-scoped incremental HTTPS status/provider-safe progress and polling fallback use the same durable job and progress sources. Generated answer text is a pending user decision, with a separate provider-path compatibility gate. |
-| R0 | Ship 202 + poll now. A later streaming UI consumes the same durable job row. |
 | R2 | The terminal is committed in one composite transaction with the final publication. |
 | R3 | The job's running state is bound to the COMPOSE SOL. One composite transaction mints the SOL context and marks the job `running`, and the worker `adopt`s it. Job liveness == SOL liveness. |
 | 1 | One id per send. The `operation_id` IS the ingress key. Ingress stays the immutable acceptance record, written by the worker in the user-row transaction. The ingress→job composite FK makes them agree. The ingress 409 arms and the SPA transcript-matching recovery are deleted. |
```

---

**Transfer 09/19 — payload B, contract patch part2/6**

```diff
@@ -64,11 +65,11 @@
 | E13 | **SOL close after the terminal.** A close or renewal failure, including an `ExceptionGroup` from `__aexit__`, never relabels a committed terminal. It is logged as `composer_operation.lease_close_failed_after_settle`. Before the terminal, the worker unwraps a body-plus-close group: any lease-loss member (renewal error, `SessionOperationFenceLost`, the settlement-child cancel from `_freeform_child_result`) → `worker_lost`; otherwise it projects the body exception. | Letting the group escape as a bare 500. |
 | E14 | **Budgets.** The job deadline is set at admission from `composer_timeout_seconds`. At `running` the worker records `ComposerBudgetAnchor`, and `compose(budget_seconds=…)` gets the time remaining immediately before the call. The planner gets the time remaining at the branch. Auto-commit settlement (`pipeline_settlement` `commit_timeout_seconds`) gets the remaining job budget. The synchronous Accept route gets `composer_sync_timeout_seconds`. D10: the convergence body's `timeout_seconds` reports the budget the turn received. | A fresh full budget per stage. |
 | E15 | **Sync cap.** `WebSettings.composer_sync_timeout_seconds = min(composer_timeout_seconds, ceiling − headroom)` has these consumers: `explain_run_diagnostics`, proposal Accept settlement, and the tutorial run wait (which already uses `ceiling − headroom`). `ComposerSettings` gains the property. **No** new status key: no client reads one after cutover. | Publishing an unread key. |
+| E16 | **SPA timers.** Delete obsolete socket-turn timer plumbing at complete cutover. Read database-clock remaining duration into a monotonic observation timer that only tightens. Stream watchdog/subscription expiry closes that reader and falls back to the same operation GET; it neither cancels the worker nor clears custody. Only a durable terminal settles the turn. `/api/system/status` retains its informational composer timeout. | Treating a client timer or lost stream as durable settlement. |
-| E16 | **SPA timers.** `runComposeWithTimeout`, `applyServerComposerTimeout` and the `composeTimeoutReady` latch lose their last caller at cutover and are deleted. The freeform deadline is the poll loop's monotonic `performance.now()` deadline from `deadline_remaining_ms + COMPOSE_CLIENT_GRACE_MS`, which only tightens across bodies. `/api/system/status` keeps `composer_timeout_seconds` (an existing public field, now informational). | Keeping dead timer plumbing. |
 | E17 | **Drain.** `stop()` stops claiming, releases any claim that returns after the stop, cancels owned jobs with the `shutdown` marker, and joins each for up to `composer_async_drain_seconds` (default 10.0). A job still unsettled is left for a peer's reaper (`worker_lost`). Every deploy/drain therefore settles in-flight turns as 503 `composer_operation_worker_lost`; this is a documented behaviour (CHANGELOG + runbook). | An unbounded join. |
 | E18 | **Run and diagnostics while a job exists.** No server change. A queued job holds no SOL, so Run/diagnostics proceed, and a start that then conflicts requeues (claim discovery skips live-fenced sessions). A running job makes them answer today's 409 `Session operation is already active`. The tutorial's Build/Run gating is the SPA reading the operation state (N15). | A new Run admission rule. |
 | E19 | **FK actions.** job→`sessions` CASCADE; job→`identities` (actor) RESTRICT; job→`chat_messages(user_message_id, session_id)` **NO ACTION** (checked at statement end, so cascade order cannot fail a D7 delete on PG); job→`composition_states(base_state_id, session_id)` NO ACTION; ingress→job `(session_id, operation_id, user_message_id)` → job `(session_id, operation_id, user_message_id)` **CASCADE**. N00 measures a PG cascade with a RESTRICT sibling; N17 proves the final set. | RESTRICT on the job's own references. |
+| E20 | **Progress.** User-message `request_id` remains separate correlation. Each publication/read also carries exact operation or worker-lease custody, retained by the progress authority. Repeated recomposes of the same user message cannot share progress custody. Never relabel session-latest progress as an operation. Progress remains advisory and cannot decide the terminal. | Message-id-only operation correlation; memory-only cross-instance replay. |
-| E20 | **Progress.** The progress `request_id` is the persisted user-message id (send: the row the worker inserts; recompose: `expected_user_message_id`), never the operation id. Progress failures are advisory: they are logged and never decide the terminal. | — |
 | E21 | **Failure codes** (a closed CHECK + Literal): `http_error`, `operation_failed`, `worker_lost`, `request_cancelled`, `deadline_expired`. **`settled_by`** (a closed CHECK + Literal): `owner_terminal`, `settle_unstarted`, `request_cancel`, `settle_lost`, `settle_own_lapsed`, `settle_lost_inactive_session`. | — |
 | E22 | **Fixed bodies** (all get the request id per E4): deadline 504 `{"error_type":"composer_operation_deadline_expired","detail":"The composer request waited too long to start. Please resubmit.","timeout_seconds":&lt;configured&gt;}`; worker lost 503 `{"error_type":"composer_operation_worker_lost","detail":"The server stopped while composing this request. Reload to see what was saved, then resubmit."}`; cancel 499 `{"error_type":"request_cancelled","detail":"The composer request was stopped."}`. An **unmarked** `CancelledError` out of a detached turn is `worker_lost`. Only the cancel endpoint's marker is a user Stop. | — |
 | E23 | **Test harness.** Tests drive the worker inline (`await worker.run_until_idle()`). Route tests use one `httpx.AsyncClient(ASGITransport)` per test via `tests/helpers/composer_operations.py`; legacy sync sites use `settle_sync` (one `anyio.run`). The worker resolves `app.state.composer_service` **per job** (tests swap it after build). The event-held composer fakes move to `tests/fixtures/composer_fakes.py`. | Running the real lifespan in route tests. |
```

---

**Transfer 10/19 — payload B, contract patch part3/6**

```diff
@@ -191,7 +192,7 @@
     error: ComposerOperationError | None = None
 # ChatMessageResponse.client_request_id → operation_id: str | None = None (N14; ruling 6)
 ```
+Absent and explicit-null `state_id` must hash identically; N02 proves that on the unmodified codec and proposed strict DTOs. Both mean "no state" (ruling 7).
-Absent and explicit-null `state_id` hash identically (measured). Both mean "no state" (ruling 7).

 ## Table — `composer_async_operations` in `sessions/models.py` (N04)

@@ -231,7 +232,7 @@
 The fence triple is **load-bearing on terminal rows**. If a terminal settle nulled it, the positive predicate (E10)
 could not find the row and would fail open.

+**Triggers.** Both dialects and every live required-trigger inventory. N00 measures the current registry; add these two named guards and update the real inventories without copying historical totals.
-**Triggers.** Both dialects, all 5 inventory places; `_REQUIRED_AUDIT_TRIGGERS` goes from 11 to 13.

 - `trg_composer_async_operations_transition_guard` (UPDATE) forbids:
   - running→queued;
@@ -329,7 +330,7 @@
 ## Composite start (R3 + E5) — `coordination/repository.py` (N06)

 ```python
+# _SessionOperationAuthorityRepository + the SessionOperationAuthority Protocol (+ every Protocol fake identified by the current live inventory):
-# _SessionOperationAuthorityRepository + the SessionOperationAuthority Protocol (+ every Protocol fake, measured):
 def start_composer_async_operation(self, claim: ComposerOperationClaim, *, owner_instance_id: str,
                                    lease_seconds: int, auth_provider_type: str) -&gt; SessionOperationContext
     # ONE _locked_transaction:
@@ -357,7 +358,7 @@

 The worker adopts the returned context:
 `await SessionOperationLease.adopt(service.session_operation_authority, context, lease_seconds=…)`.
+N00 measures existing production `adopt` callers; preserve their behavior while adding the worker.
-This is `adopt`'s first production caller.

 ## Composite terminal (R2) — `sessions/service.py` (N07)

@@ -555,7 +556,7 @@
 - a dead loop escalates via `process_recovery.request_shutdown()`;
 - the worker is published as `app.state.composer_async_worker`.

+## HTTP (N13 poll/cancel/HTTPS stream; N14 complete cutover)
-## HTTP (N13 poll/cancel; N14 cutover)

 - **`GET /api/sessions/{session_id}/operations/{operation_id}`** → 200 `ComposerOperationStatusResponse`.
   - `Cache-Control: no-store`.
```

---

**Transfer 11/19 — payload B, contract patch part4/6**

This starts the58-line addition hunk;12 continues it directly.

````diff
@@ -581,6 +582,64 @@
   - deletes the ingress 409 arms, `lookup_message_ingress` and the legacy DTOs;
   - renames `ChatMessageResponse.client_request_id` → `operation_id`.

+
+## Admission boundaries retained at the async cutover (N00, N06, N10, N14)
+
+Before the durable queue insert, preserve the current send credential-material refusal and compartment ingress policy alongside strict DTO/ownership/idempotency checks. Strict shape validation alone is not proof that request_json is safe to persist. Keep existing ingress refusal envelopes and rate-limit behavior within the reviewed admission ordering.
+
+Current recompose already uses get_messages(limit=None), filters audit-only rows through _composer_conversation_messages, and checks empty/non-user/mismatched final conversational messages. Retain those source-derived guards in the start composite; do not add another user row or truncate history. Source reads are navigation evidence; N00 still needs the live registered-handler/error-ladder inventory and ordinary baseline tests before implementation.
+
+## HTTPS observer contract (N03, N08, N13, N15, N17, N19)
+
+### Wire and source
+- Add `GET /api/sessions/{sid}/operations/{operation_id}/stream` beside the existing proposed authoritative operation GET and cancel POST.
+- Use the existing bearer authentication/role/ownership policy through `fetch` with Authorization headers. Do not put tokens in URLs, add credentials, new grants or browser EventSource-specific ticket infrastructure.
+- Proposed response is UTF-8 `text/event-stream`, `Cache-Control: no-store` and appropriate anti-buffering headers. Frames carry a closed event discriminator, schema version, session/operation identity and a bounded owned payload.
+- Events: `status` (queued/running/cancel marker and remaining duration), `progress` (existing provider-safe progress fields with exact job/lease binding), `heartbeat` (no request/provider content) and `terminal` (durable terminal notification). Generated answer deltas are excluded pending the user's decision.
+- Terminal notification triggers the same authenticated durable GET; that GET returns the exact former final response/public error after strict hash/DTO validation. Do not duplicate an unbounded composition/result graph into a bounded progress frame.
+- No events table is required for advisory snapshot delivery. Reconnect may skip progress snapshots; final result replay is durable. Do not claim a lossless token/event log.
+- User-message `request_id`, transport correlation `request_id` and action `operation_id` are distinct. Bind progress in the authority by the exact operation/lease, not by the message ID alone. Queued status comes only from the requested job.
+- Read role/identity, session ownership/archive state, job and matching progress through short committed snapshots. PostgreSQL cross-instance subscriptions read durable job/progress sources. SQLite keeps the shipped single-process progress registry: restart may lose advisory progress, but job status and terminal replay remain durable. Do not introduce or imply SQLite multi-instance progress support. A terminal must be committed before the notification; no send can decide settlement.
+
+### Authorization and redaction
+- Before response headers, authenticate bearer and live role, validate deployment/provider scope, ownership and the requested job, using opaque ordinary refusals.
+- Recheck live authorization at a bounded cadence and before each data frame; never emit after failed authorization. Token expiry closes the reader. Use verified owned token claims or provider reauthentication, never the unverified convenience claims from request state as an authorization proof.
+- Define and prove the worker's live-role/identity/ownership revocation behavior with Daybreak. Preserve CRL renewal and the worker's cancel/lease semantics; the subscriber is not a new authorization grant or the job owner.
+- After headers, only bounded redacted control information or clean closure is allowed. The client recovers through an authorized GET. No provider prompts, reasoning, tool arguments, credential/secret values, raw exceptions or diagnostic internals in frames/logs.
+- Disconnect, authentication expiry or read/write failure ends a subscription. Explicit cancel POST alone marks user Stop; late cancel cannot relabel a committed terminal.
+
````

---

**Transfer 12/19 — payload B, contract patch part5/6**

Continue11 with no inserted blank separator.

````diff
+### Bounded resources — proposed starting values, pending ordinary local measurement and Daybreak review
+- Heartbeat every 10 seconds, snapshot read cadence no faster than 1 second, subscription lifetime at most 120 seconds and an ASGI send wait at most 5 seconds.
+- Maximum encoded frame 64 KiB; oversized snapshots are not emitted. Final result stays on the durable GET.
+- Per-process caps: two readers per operation, four per principal, 32 total. State that these are process caps, not a promised global cluster limit.
+- Coalesce stale progress instead of queuing all updates. Use no worker-coupled subscriber queue and no unbounded retained frame/body buffer.
+- Release permits and join subscription tasks on every exit, including repeated cancellation and ASGI send failure.
+- Hold no database transaction, row lock or pooled connection across socket writes, heartbeat waits or client backpressure.
+- Enforce send deadlines at the ASGI send boundary; a timer only inside the producer generator does not bound a blocked transport write.
+- Bound subscription admission/read wait and reconnect backoff. Prove the worker/ordinary sync pool remains usable under the subscriber cap. Reduce limits if measured contention demands it.
+- Subscription renew/reconnect does not extend the admission deadline, worker lease, quota or provider retry budget.
+- Preserve async_workers.py's worker-lifetime accounting: 16 threads plus 16 queued admissions, with a one-second wait to acquire capacity. The helper cancels queued work when its caller is cancelled, while a running thread remains admitted until completion. Do not assume an async timeout stopped SQL or freed that capacity. Prove bounded driver/read behavior, late commit, admitted-queue cancellation and shared-pool contention without modifying global infrastructure.
+
+### Frontend custody and fallback
+- One operation observer and terminal reducer serve stream and poll. Preserve the immutable operation ID/body and auth/session generations. Body-bearing sessionStorage custody is scoped to authenticated principal/provider, bounded to the proposed 256 KiB/24-hour lifetime, and purged on logout/principal replacement through the existing authStore reset seam. Same-principal token refresh may resume; cross-principal restore is refused.
+- Incremental decoder accepts split UTF-8 codepoints, split line endings, complete SSE frames and multiple frames per chunk; reject invalid/oversized/truncated frames at the external boundary.
+- Validate requested session/operation and closed event shape before applying data. Duplicate/late frames and a previous auth/session generation cannot apply a terminal or overwrite current state.
+- Stream EOF/watchdog/error/proxy buffering falls back to GET for the same job. Missing operation after ambiguous submission permits only the same id/body retry; session missing/archive stops recovery without resubmission.
+- A client deadline is an observation budget, not cancellation proof. Stop calls explicit cancel and continues observing to durable terminal.
+- Custody is a strict sum type: submitted custody holds the exact action ID/body and may reconcile/resubmit that same action after an ambiguous admission; observation-only custody holds a server-reported active action ID/kind and has no replay body. A 409 active-job attachment cannot re-key the losing unsent body to the accepted operation. Preserve that unsent content as a draft, reload authoritative rows and never resubmit observation-only custody. Any earlier still-ambiguous submitted action retains its own reconciliation custody until resolved; a later active-job refusal does not establish that an earlier in-flight admission cannot commit. Test that race explicitly.
+- Reload, two tabs and session switching preserve custody without duplicate provider work. Ordinary tutorial UI uses the same observer and admission path.
+
+### Real HTTPS and fault acceptance
+- Use ordinary local fake providers and isolated SQLite/PostgreSQL databases, never paid-provider/production tests.
+- A real TLS client/browser through the intended local proxy must receive an early frame/heartbeat before delayed fake work terminalizes. ASGI tests and `proxy_buffering off` source text alone are insufficient.
+- Exercise a deliberately buffering proxy and a short idle cutoff; polling recovers the same job with one provider dispatch.
+- Prove slow-reader/send failure, subscription churn, bounded memory/tasks/pool use, token/role/ownership revocation and zero data after denial.
+- Do not change deployed Caddy/nginx/cloud infrastructure in this task. Installed development Caddy configuration is operator-local; record any unavailable local TLS prerequisite as a completion blocker.
+
+### Generated answer text readiness gate
+The shipped gateway `ChatRequest` rejects `stream`; `_litellm_acompletion` currently returns a complete response to owned admission. If the user requires answer text as generated, explicitly revise this contract before implementation and Daybreak review. Specify supported provider paths, final-answer versus intermediate/tool/reasoning content, attempt discrimination, usage/audit terminalization, secret redaction and bounded reconnect custody. Do not promise token replay from snapshot-only progress or invent a server-authored answer/graph fallback.
+
+
 ## Test helpers — `tests/helpers/composer_operations.py` (N13)

 `submit_and_settle(client, app, *, path, body, max_rounds=50) -&gt; SettledComposerOperation`,
````

---

**Transfer 13/19 — payload B, contract patch part6/6**

````diff
@@ -591,6 +650,8 @@

 ## Frontend — `src/elspeth/web/frontend/src` (N15)

+The operation observer tries the authenticated fetch stream and uses polling after EOF, fragmentation/protocol error, idle timeout, unsupported transport or buffering. Both feed one operation-bound reducer/custody path. Existing poll-only examples below are historical starting shapes; implement the complete observer defined in the HTTPS contract, not a separate stream store.
+
 - **`api/client.ts`:**
   - `apiErrorFromBody(status, body)` (no 401 logout), used by `parseResponse`;
   - `submitComposerOperation`, `fetchComposerOperation` and `cancelComposerOperation`, each bounded by
@@ -608,7 +669,7 @@
     the operation is missing → poll loop (`poll_after_ms`; backoff 1→2→4→8 s on transient errors, which never fail
     the turn) → the existing reducers;
   - reducers get the `result`, or `apiErrorFromBody(error.http_status, error.body)`;
+  - stream readers and fallback pollers share exact session/operation/auth-generation custody and stop settlement only on a durable terminal; `isComposing` holds from custody acquire until terminal;
-  - pollers stop only on terminal; `isComposing` holds from custody acquire until terminal;
   - `resumeComposerOperation(sessionId)` runs on `selectSession`/boot before render;
   - 409 `composer_operation_active` → attach to that id;
   - a 404 `session_missing` → today's session-not-found path, clears custody, never resubmits;
@@ -655,13 +716,13 @@
 | N11a | `ComposerAppServices` + D6 settlement signatures + the ingress/job binding in `add_message_with_transcript` | T10 part | N07, N08, N09, N10 |
 | N11b | `run_composer_turn` + observation + retargeted structural pins | T10 part | N11a |
 | N12 | Worker + reaper + lifespan + watcher (gate 4) | T11 | N11b |
+| N13 | Poll/cancel/operation-scoped HTTPS stream routes, snapshot authorization/resource limits + test helpers + shared fakes | T12 | N12 |
-| N13 | Poll/cancel routes + test helpers + shared fakes | T12 | N12 |
 | N14 | Cutover: 202, ingress DDL + rename, deletions (409 arms, lookup, legacy DTOs, `_track_compose_inflight`), caller migration incl. `state_id`, ACA P1 redefinition + probes + `acceptance.sh`, eval battery, gate 2 | T13 + T03 part | N13 |
+| N15 | Fetch stream decoder/observer, polling fallback, custody/resume/Stop + e2e + transition-ledger terminal boundary | T14 | N14 |
-| N15 | SPA cutover (above) + e2e + transition-ledger boundary | T14 | N14 |
 | N16 | Budget decoupling + deployment mirrors | T15 | N14 |
+| N17 | SQLite/PG crash and race proofs, cross-instance reconnect, revocation, backpressure/resource faults and retained cascade/fence/terminal proofs | T16 | N14, N15 |
+| N18 | Re-read current epoch pair before the necessary coherent schema cut; current contract/API/local test docs only; leave PR 275 release metadata separate | T17 | all |
+| N19 | Required gates, Astra correctness/design + real Daybreak transport/security review, real local TLS chunk/proxy/browser acceptance and exact-HEAD LOCAL test branch hold | T18 | N18 |
-| N17 | PG crash windows + cross-instance + the new PG proofs (cascade, starvation, stale base, post-terminal write, `settled_by`, delete guard) | T16 | N14 |
-| N18 | Epoch 72 (re-read first) + doc/website/CHANGELOG sweep | T17 | all |
-| N19 | Full gates (all stages) + lints set diff + local short-idle-proxy browser acceptance | T18 | N18 |

 **N00 measurements:**

````

---

**Transfer 14/19 — payload C, task pages N00–N03**

Targets `docs/plans/2026-09-20-composer-async-operations/&lt;task&gt;.md`. Filename comments delimit complete pages; do not include delimiter in file. Unpersisted/unreviewed. N02,N03 are missing on baseline main.

```markdown
&lt;!-- FILE: N00.md --&gt;
# N00: Current source, overlap and baseline readiness

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: none.

## Deliverables

- Wait for a usable selected workspace; fetch main, record exact local/remote SHAs and provenance without printing credentials.
- Read applicable repository instructions and relevant skills; create a dedicated review worktree without switching a shared checkout.
- Parent supplies the exact retry candidate; PR 275/276 keep independent owners and main authority. Refresh source after they land or coordinate temporary integration.
- Measure registered route/handler exits, production writer policy, job/progress source paths, DB epochs and caller inventory through their live authorities.
- Run ordinary affected baseline checks and receipt golden-vector capture; use declared frozen dependencies if prerequisites are absent.
- Inspect actual Daybreak CLI/tool availability without transmitting a payload while approval is unresolved; resolve its readiness blockers before implementation.

## Required proofs

- Main and worktree provenance, actual imports from both source roots, exact active PR/candidate SHAs and a measured overlap list.
- Positive/negative controls for each measuring instrument; no zero-findings or parity claim from an unvalidated source search.
- User delivery decision and real Daybreak plan review are recorded; NO-GO persists until those gates close.

&lt;!-- FILE: N01.md --&gt;
# N01: Worker and subscription budgets

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N00.

## Deliverables

- Add retained async queue/concurrency/claim/scan/poll/drain policy with valid bounded settings and real consumers.
- Preserve synchronous transport-safe cap consumers while threading the later job budget.
- Measure the proposed 10-second heartbeat, 1-second snapshot cadence, 120-second subscription, 5-second ASGI send wait and 64-KiB frame limit.
- Review per-process subscription caps (2/operation, 4/principal, 32 total), pool contention and the smallest supported proxy idle ceiling.

## Required proofs

- Settings-to-runtime mapping and invalid-boundary tests; new settings are consumed rather than orphaned.
- Heartbeat is below supported idle limits; subscription deadlines and memory/read/send bounds are independent of the provider/job deadline.
- Ordinary fake-provider load controls show no worker/DB pool starvation at the chosen caps.

&lt;!-- FILE: N02.md --&gt;
# N02: Receipt codec parity and operation hashes

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N00.

## Deliverables

- Compute golden vectors from the unmodified operation_receipts request/response hash functions, including omitted/null base and strict DTO cases.
- Extract a shared normalizer only if all receipt bytes stay identical; otherwise retain receipts and add a separate composer codec.
- Bind hash to session, kind and immutable request/base; exclude only the action operation_id.

## Required proofs

- Same id/body replay; changed content/base/kind conflict.
- Golden receipt vectors remain byte-identical; deliberately mutated serializer goes red.

&lt;!-- FILE: N03.md --&gt;
# N03: Strict operation and HTTPS stream contracts

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N02.

## Deliverables

- Define closed owned job/claim/running/error/status bundles and canonical UUID parsing.
- Define versioned status/progress/terminal-notification stream frames bound to session and operation, with strict bounded payloads.
- Keep user-message request_id separate from the operation/lease progress binding; repeated recomposes must not alias.
- Leave generated answer deltas contingent on the user's decision and provider-path review.

## Required proofs

- Strict decode rejects extras, invalid status bundles, inconsistent job IDs, bad terminal shapes and oversized frames.
- Request JSON cap measured with worst-case escaping; UTF-8 bytes versus character limits tested explicitly.
```

---

**Transfer 15/19 — payload C, task pages N04–N07**

N04 is missing on baseline main.

```markdown
&lt;!-- FILE: N04.md --&gt;
# N04: Durable job schema and operation progress binding

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N03.

## Deliverables

- Implement the retained job schema, immutable terminal/transition/delete guards, partial active-job index and composite FKs in both dialects.
- Persist exact operation/lease binding for progress through the existing progress authority; no snapshot-only event log.
- Retain terminal fence fields required by the positive predicate. Re-read epoch sentinels before the one coherent bump.

## Required proofs

- SQLite and PostgreSQL reflection, invalid bundle/transition rejection, terminal immutability and cascade order.
- One nonterminal job per session and matching ingress/job/message binding; malicious cross-operation progress rejected.

&lt;!-- FILE: N05.md --&gt;
# N05: Typed job authority and idempotent admission

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N04.

## Deliverables

- One named typed authority owns all job DML; composites call connection-taking helpers and writer manifests stay fail-closed.
- Use the database clock, bounded candidate scans/counts and a queue-only claim CAS; preserve soft-cap semantics explicitly.
- Admission checks credential/compartment policy, auth/ownership, strict body and same-id binding before durable insertion; replays avoid new provider work.
- Operation reads authorize live role/identity/provider/session scope and produce strict terminal/hash-validated DTOs; progress selection is exact-bound.

## Required proofs

- SQLite and PostgreSQL races for same id/body, changed body/base/kind, distinct ids and capacity.
- Foreign/archived/missing sessions stay opaque; actor/provider binding cannot be widened through a job ID.
- Expired claims and scans stay bounded; positive CAS controls distinguish queue-only mutations from running/terminal rows.
- Cancelled queued versus still-running SQL admission, delayed commit after client timeout and same-id replay; preserve shared-pool admission until actual worker completion.

&lt;!-- FILE: N06.md --&gt;
# N06: Atomic COMPOSE start and preconditions

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N05.

## Deliverables

- Extract the existing fence advance without weakening ordinary acquire; atomically advance COMPOSE, validate queued claim/base/owner/transcript, and mark running.
- Adopt the exact minted lease and bind the job's lifetime to it.
- Retain existing full-history conversational filtering and recompose empty/non-user/mismatch guards; reject stale/foreign bases before user/provider effects.
- Preserve ordinary operation receipts and fork/revert fencing behavior.

## Required proofs

- Crash/rollback between each start seam leaves no unowned running state or premature user/provider side effect.
- Cross-instance stale claimant cannot adopt another lease or restart a running turn.
- Positive baseline fork/revert tests plus deliberate stale-base/foreign-owner/transcript negative controls.

&lt;!-- FILE: N07.md --&gt;
# N07: Positive mutation fence and atomic terminal

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N06.

## Deliverables

- Measure current non-audit versus audit-only writers before adding the positive job-running/no-cancel predicate.
- Preserve exact fence lookup on completed/failed rows; do not change shared renewal/release/archive predicates.
- Publish assistant data, required audit, pending proposal/state projection and terminal response hash in one composite transaction.
- Join auto-title/owned children before settlement; cancellation persists required audit without allowing further authored mutations.

## Required proofs

- Non-audit post-terminal/cancel-marked writes fail; authorized independent fork/revert writers still work.
- Cancel versus terminal CAS winners, transaction faults, repeated cancellation and post-commit close failures preserve one durable terminal.
- Result tampering fails strict integrity checks; no error string or unreviewed secret appears in the public result.
```

---

**Transfer 16/19 — payload C, task pages N08,N09,N10,N11a**

N08,N09,N10 are missing on baseline main.

```markdown
&lt;!-- FILE: N08.md --&gt;
# N08: App-owned lifecycle, renewal and progress custody

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N00, N03.

## Deliverables

- Extract the existing request lifecycle into an app-owned worker lifecycle retaining renewal, revocation, telemetry and audit semantics.
- Carry durable completion explicitly; worker has no HTTP Request, request.receive or subscriber AbortSignal.
- Bind the progress sink to exact job plus request lease; do not let a stale sink overwrite a new operation.
- Document live-role/identity/ownership revocation behavior for Daybreak; subscription close is never user Stop.

## Required proofs

- Lease admission/cleanup cancellation, heartbeat loss and stale-sink tests with ordinary production paths.
- Two recomposes of the same user message get distinct custody; subscriber disconnect leaves the accepted job alive.

&lt;!-- FILE: N09.md --&gt;
# N09: One job deadline and independent subscription budgets

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N01.

## Deliverables

- Thread one remaining admission deadline through compose, planner and terminal settlement; queued time consumes it.
- Preserve synchronous caps for diagnostics and proposal settlement.
- Give HTTPS subscriptions independent idle/write/lifetime budgets; reconnect cannot renew a job deadline or grant provider retries.

## Required proofs

- Fake clocks do not replace asyncio timer clocks; budget accounting includes queue and stage time.
- Client clock skew, deadline-versus-terminal and transport timeout controls; losing subscription never cancels by inference.

&lt;!-- FILE: N10.md --&gt;
# N10: Public error parity and redacted stream closure

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N03.

## Deliverables

- Project durable operation errors using real current route/app handler registrations and preserve public JSON meaning.
- Keep correlation IDs from submission and generic diagnostic identifiers without exception/secret payloads.
- Before headers use ordinary HTTP refusals; after headers emit only bounded redacted control information or close and reconcile by GET.

## Required proofs

- Compare every live typed route/handler exit, including settlement and cancellation/lease groups.
- Secret/credential/exception markers absent from progress, terminal notifications, error control frames and logs.

&lt;!-- FILE: N11a.md --&gt;
# N11a: Per-job services and ingress binding

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N07, N08, N09, N10.

## Deliverables

- Resolve app services per job; thread explicit services, exact COMPOSE context and remaining budgets into settlement.
- Bind the user row, immutable ingress receipt and job user-message identity in their existing owned transaction.
- Keep existing plugin, secret, proposal and interpretation authorities; no server-authored graph or tutorial path.
- Retain per-kind telemetry/audit attribution and ordinary non-composer callers.

## Required proofs

- Worker test seams can replace the composer service per job without losing source provenance.
- Ingress/job/message mismatch or non-fresh outcome fails closed; same action replay has one user row.
- Affected writer-manifest and plugin/secret/proposal parity tests remain green.
```

---

**Transfer 17/19 — payload C, task pages N11b–N14**

```markdown
&lt;!-- FILE: N11b.md --&gt;
# N11b: One detached composer turn

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N11a.

## Deliverables

- Rebuild current send/recompose route behavior into one app-owned turn using existing composer/provider paths.
- Await compose inline to retain cancellation call-audit custody; preserve every current typed error arm and public projection.
- Keep proposal settlement, advisor completion gates, owned children and audit persistence before terminal.
- No Request, request.receive, subscriber cancellation or tutorial-only dispatch enters the turn.

## Required proofs

- Current ordinary send/recompose/provider error, planner proposal, advisor and cancellation cases through production paths.
- Per-transition provider-call scrutiny for ordinary/rootless/tutorial entry; no synthesis fallback.
- Faults and cancellation at each publication seam retain audit and cannot publish two terminals.

&lt;!-- FILE: N12.md --&gt;
# N12: Worker, reaper and drain

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N11b.

## Deliverables

- Claim bounded queued work, renew claims while waiting, atomically start/adopt, and watch explicit cancel, lease loss, shutdown and absolute deadline.
- Persist required audit on each non-success exit; use bounded transient settlement retries while authority remains valid.
- Reaper terminalizes expired queued/lost running jobs without replaying provider work or stealing a live COMPOSE lease.
- Integrate normal lifespan recovery/drain while coordinating PR 276's shared files and ownership.

## Required proofs

- Independent-process crash/lease tests; running work never requeues and reaping makes zero new provider calls.
- Unclaimed, claimed, waiting, running and committed-terminal cancel cases; terminal winner survives late Stop/drain.
- Dead-loop escalation, repeated shutdown cancellation and all task/permit cleanup paths.

&lt;!-- FILE: N13.md --&gt;
# N13: Authenticated operation poll, cancel and HTTPS stream

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N12.

## Deliverables

- Mount operation-scoped poll/cancel/stream routes using one durable status/result source and exact progress custody.
- Preserve require_pipeline_user, provider scope and opaque ownership refusals; recheck live auth throughout a bounded subscription.
- Use bounded SSE/fetch-compatible frames, heartbeat and ASGI send deadline, short DB snapshots and per-process reader admission.
- A terminal frame is a notification for authoritative GET; subscription disconnect cannot cancel work.
- Create ordinary fake-provider helpers and one-loop sync adapters without live-provider/production calls.

## Required proofs

- IDOR/missing/archive, role/identity/token expiry, redaction, result integrity and cross-operation progress refusal.
- Before-header HTTP refusal and safe post-header closure; no bytes after a failed authorization check.
- Slow/abandoned readers, repeated cancellation, task/permit/pool cleanup and deliberate buffering fallback.

&lt;!-- FILE: N14.md --&gt;
# N14: Complete backend and caller cutover

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N13.

## Deliverables

- Both ordinary authoring POSTs return 202 only after durable validated admission; migrate all callers to operation_id/base/transcript binding.
- Remove backend socket-turn cancellation once the detached worker owns the turn. N15 removes frontend ingress/transcript recovery after the unified observer is ready; ship neither intermediate state.
- Retain credential/compartment boundaries and current public terminal meanings.
- Retarget structural/ownership/writer and transition-ledger pins; no intermediate merge or deployment.
- Coordinate the exact retry candidate so its GET-only reconciliation/full-history/recompose protections are preserved or superseded by equivalent durable checks.

## Required proofs

- Every measured caller consumes 202 and authoritative operation terminal; both route kinds and tutorial use the same path.
- Ambiguous POST/reload/resubmit tests prove one provider/user action and immutable id/body custody.
- No legacy body/DTO/socket-cancel pathway ships on the held final branch.
```

---

**Transfer 18/19 — payload C, task pages N15–N19**

N17,N18,N19 are missing on baseline main.

```markdown
&lt;!-- FILE: N15.md --&gt;
# N15: Unified frontend observer, decoder and custody

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N14.

## Deliverables

- Use authFetch with bearer headers for the operation stream; one store/custody path handles stream and poll fallback.
- Strict external decoder bounds frames and handles arbitrary UTF-8/SSE fragmentation/coalescing.
- Body-bearing custody is principal/provider scoped, bounded, purged on logout/principal replacement and resumed for same-principal refresh/reload.
- Apply final response/errors through existing authoritative reducers once; reload state after non-success terminals.
- Explicit Stop posts cancel and observes terminal; session switch, EOF, proxy buffering and client deadlines cannot mint another user action.
- Use ordinary tutorial transport and retarget Playwright provider-call transition boundary to durable terminal.
- Separate submitted exact-body replay custody from observation-only attachment to a server-reported active action; never re-key an unsent body or resubmit an observation-only descriptor.

## Required proofs

- Decoder positive/negative controls for fragmented codepoints, CRLF/multiline/multiple frames, bad/extra/oversized/truncated frames.
- Store tests for duplicates/late frames, auth-generation change, logout, same-principal refresh, session switch, two tabs, reload and terminal-versus-cancel.
- Ambiguous submission, operation missing versus session missing, stale-base new action versus same-id network retry, polling fallback and recovery.
- Sequential mocked Playwright ordinary/tutorial paths plus frontend lint/typecheck/build.
- Two tabs with different IDs/bodies: the loser observes the accepted action, retains its own unsent draft, and never creates/replays a provider action under the other ID.
- Late commit of an earlier ambiguous submission while a later attempt observes another active action: retain the submitted ID/body until authoritative resolution.

&lt;!-- FILE: N16.md --&gt;
# N16: Timeout and HTTPS deployment expectations

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N14.

## Deliverables

- Decouple accepted async job budget from a socket idle ceiling while preserving synchronous consumers' safe cap.
- Document heartbeat/subscription behavior against the smallest supported proxy timeout and shipped nginx buffering policy.
- Record that installed development Caddy configuration is operator-local and must be inspected in authorized local testing.
- Do not edit/deploy infrastructure, change cloud/provider grants or mix independent release/provenance repairs.

## Required proofs

- Timeout relationship and settings mirror checks applicable to changed source.
- Local real-TLS acceptance observes early chunks; deliberately buffering/short-idle conditions recover by poll.
- No deployed-service or production-provider action appears in verification.

&lt;!-- FILE: N17.md --&gt;
# N17: SQLite/PostgreSQL crash, race and reconnect proofs

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N14, N15.

## Deliverables

- Prove claim/start/cancel/terminal races and crash windows in both dialects without provider takeover of running work.
- Use independent processes/instances to prove fencing, admission races, stale writers, role/identity/ownership changes and replay.
- Prove subscriber lifecycle and bounded read/send behavior under database outage, slow readers and proxy buffering.
- Keep EFS archive fixes outside this lane; coordinate common repository/service/lifespan edits with parent.

## Required proofs

- Concurrent same-id admission yields one action/user/provider dispatch; distinct-id admission obeys partial index.
- Fault injection before/after audit and terminal writes; cancel winner and terminal winner both proven; reaper never replays provider.
- No DB lock/connection held across network waits; dead/unauthorized/slow subscribers release all permits and tasks.
- Reconnect and restart replay durable terminal through authorized GET; progress binding distinguishes same-message recomposes.
- Saturated/hung shared worker pool and bounded DB-driver failures: no unbounded submission growth or falsely freed running-thread slots.

&lt;!-- FILE: N18.md --&gt;
# N18: One schema cut and current documentation

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N17.

## Deliverables

- Re-read SESSION_SCHEMA_EPOCH and _COORDINATION_HARD_CUT_EPOCH immediately before committing required schema changes.
- Update API/contract/generated types and ordinary local test documentation for final behavior.
- Use main as the integration base. Do not rewrite release metadata, dependencies, gateway provenance or archive code from PR 275/276.
- Document eventual operator storage implications without performing DB recreation, infrastructure edits or deployment.

## Required proofs

- Epoch equality and both dialects' schema checks.
- Plan links resolve, obsolete deferral/release-base instructions removed, and public error/wire documentation agrees with implementation.

&lt;!-- FILE: N19.md --&gt;
# N19: Readiness gates, real local HTTPS and branch hold

Status: readiness proposal; not implemented or reviewed. The bundle's NO-GO and no-merge/no-deploy gates apply.

Dependencies: N18.

## Deliverables

- Obtain Daybreak plan review before implementation, then Astra correctness/design and Daybreak transport/security reviews of the final candidate; transmit only after resolved approval.
- Run affected checks and required whole-tree gates, recording exact exit codes and source provenance.
- Exercise fake-provider delayed work through a real local TLS proxy and browser; prove early chunks, heartbeats and polling fallback.
- Record exact branch HEAD/base SHA and local test steps; hold for John's LOCAL testing, no merge/deploy.

## Required proofs

- Real HTTPS client observes progress/heartbeat before terminal; deliberate buffering/short idle cutoff safely falls back without new provider work.
- Fragmented UTF-8/SSE, truncated/oversized/bad frames, stale auth generations, duplicate terminal, session switch, reload and explicit Stop tests.
- SQLite and serial PostgreSQL testcontainer checks for schema/persistence/lock changes; scoped frontend tests, typecheck/build and sequential Playwright.
- No paid-provider/production tests, new grants, protected-check bypass or unrelated changes.
```

---

**Transfer 19/19 — manifest and required receipt**

Exactly **19 numbered transfer messages** have been emitted here. No new task or external application artifact was created. Direct cross-thread task messaging is unavailable in this tool set.

Relay each numbered message individually to Nyx `01a10f38-def3-7203-9dfe-a1c58b6520e3`:

| Chunks | Contents |
|---|---|
|01–03|Custody, authorization, source/dependencies, findings, blockers, errata, tests and instructions|
|04–07|Complete payload A: readiness-index draft|
|08–13|Complete payload B:13-hunk contract patch, preserving original durable design|
|14|N00,N01,N02,N03|
|15|N04,N05,N06,N07|
|16|N08,N09,N10,N11a|
|17|N11b,N12,N13,N14|
|18|N15,N16,N17,N18,N19|
|19|This manifest/receipt requirement|

Reconstruct **23 proposed files**: index, contract,21 task pages. Nine task files absent from baseline: N02/03/04/08/09/10/17/18/19. Keep all original history/panel/findings. Preserve this raw packet and errata independently of subsequent reconciled edits.

For A/B, concatenate raw fenced payloads in order; discard message headers and outer fences only. For B preserve diff context prefixes, including blank context lines containing a single space. Chunk10 contains original triple-backtick context lines—extract from its **first outer opening fence through last outer closing fence**, not a Markdown parser’s first closing match. Chunks11–12 continue one hunk without extra separator. Apply B only to the verified original blob `76bda4c8f7c13394fe9462d9d9070e73f6d90628` at pinned main. Prior source-turn in-memory patch reconstruction was exact; replacement must independently check application.

**Required Nyx confirmation before parent retires this task:**

- Received all19 chunks, explicitly list any missing/truncated chunk.
- Persisted raw transfer, A, reconstructed B, all21 C pages, and the five draft errata; give real paths and custody/branch SHA if committed.
- Verified23-file manifest, all nine missing pages recovered, all13 contract hunks applied against correct original, task links resolve.
- Acknowledge everything remains untested/unreviewed; identify actual local source SHA separately.
- Acknowledge **Daybreak Blue `gpt-daybreak-blue-latest`**, unresolved product choice/approval/review gates, and no implementation/review transmission authorized by transfer.
- Report exact remaining blockers.

Parent should retain this source conversation until that confirmation. Receipt/persistence is **not yet confirmed**. Nothing has been deleted, cancelled, merged, deployed, reviewed or implemented during transfer.
