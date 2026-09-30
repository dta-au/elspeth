# Systems review — composer async operations plan (2026-09-20)

Reviewer lens: second-order effects (shared thread-pool contention, poll load,
R05 lock-order interaction, SQLite single-process assumptions, deploy/rollback,
rolling-deploy version skew, shutdown/drain ordering with `membership.stop()`).

Tree read at `release/0.8.1`, current HEAD per gitStatus (working tree, not the
plan's worktree — the plan is unimplemented, so all citations below are
against the plan's own task files/contract and the CURRENT production code the
plan will graft onto).

## Finding 1 (Warning/Major, plan-admitted and unresolved) — guided's client-side abort ceiling goes stale once `composer_timeout_seconds` is decoupled, on any platform whose transport ceiling can't be raised to match

**Evidence:**
- `src/elspeth/web/frontend/src/App.tsx:402-403` — the SPA's ONE source for
  the client compose abort ceiling is `status.composer_timeout_seconds` from
  `GET /api/system/status`, fed into `applyServerComposerTimeout`
  (`src/elspeth/web/frontend/src/config/composer.ts:60`), which arms
  `runComposeWithTimeout` used by `useComposer.ts` — and per the contract
  (`contract.md:415-417`), **guided keeps `runComposeWithTimeout`**; only
  freeform switches to the new poll-body `deadline_at` tracking.
- `docs/plans/2026-09-20-composer-async-operations/T01.md:1101` — **the plan's
  own text admits this**, verbatim: *"The SPA still derives every client abort
  ceiling from `composer_timeout_seconds` (`App.tsx:402-403`). Until Task 15
  that is equal; after it, guided client calls would wait the full async
  budget while the server cuts at the sync cap. The server still answers
  first, so this is stale data rather than a correctness failure; Task 14 or
  15 should make the guided client read `composer_sync_timeout_seconds`."*
- I grepped both named owners for the fix and found neither implements it:
  `grep -n "App\.tsx\|abort ceiling\|composer_timeout_seconds\b" T14.md` → no
  hits; `T15.md`'s many `composer_sync_timeout_seconds` hits are all backend
  config/deploy-mirror work (settings, `/api/system/status`, Terraform/Bicep
  descriptions) — none touch `App.tsx` or `applyServerComposerTimeout`.
- Whether this actually bites depends on whether the deployed transport
  ceiling is raised to keep pace with the new, larger `composer_timeout_seconds`.
  T15 does exactly that for the ECS default (`T15.md:709`: ceiling raised to
  900 "so the synchronous cap at package defaults equals the 840s compose
  budget") — so at ECS package defaults the two stay equal and the staleness
  is latent. But Azure Container Apps has a **fixed, unraisable** 240s
  ingress ceiling (`T15.md:956`: "The Container Apps ingress request timeout
  is a fixed 240 seconds"), so on ACA `composer_sync_timeout_seconds = min(840,
  240-30) = 210s` while the guided client's local abort timer is armed at the
  full 840s.

**Ripple:** This is exactly the class of route the plan's own Global
Constraints forbid touching: *"the three guided routes keep their synchronous
contract... a change here that alters them is a regression"*
(`2026-09-20-composer-async-operations.md:33`, Scope ruling). The routes
themselves are unchanged, but their *client-perceived* failure behavior is not:
on ACA (and any ECS deployment where the operator doesn't separately raise the
ceiling to match a raised `composer_timeout_seconds`), a guided turn that the
server-side ALB/ingress cuts at the real ~210s ceiling now races a client
timer that thinks it has ~840s left. Per the plan's own assessment this
degrades to "stale data" (the network cut still happens, so it's not an
infinite hang) rather than a silent freeze — but it means the client's clean,
purpose-built compose-timeout UX (`runComposeWithTimeout`'s own handling) is
now *dead code* for that failure on ACA, replaced by whatever generic
network-error handling fires on an aborted mid-flight fetch, which is a
regression in guided's user-visible failure quality even though guided's
server contract is untouched. This is a real, admitted, and — per my grep of
T14/T15 — currently unassigned gap between two tasks each of which could
plausibly have picked it up.

**Confidence:** High — the plan's own T01.md:1101 states the defect exists and
names the fix; I independently confirmed neither candidate task implements it.
Moderate on user-facing severity (network-level cut still protects against an
infinite hang; the loss is UX quality, not availability).

**Suggested fix:** Retarget `App.tsx`'s system-status consumption (and
`applyServerComposerTimeout`) to `composer_sync_timeout_seconds` for the
guided/`runComposeWithTimeout` path, and either add this as an explicit T14 or
T15 step or open a tracked follow-up before merge — right now it's a "should"
in a review note inside T01 with no owning checklist item in either candidate
task.

## Finding 2 (Warning/Major) — every graceful drain (including an ordinary rolling deploy) immediately cancels in-flight composer turns as `worker_lost`, with no bounded grace window, and this isn't named as an operational tradeoff anywhere in the deploy tasks

**Evidence:**
- `docs/plans/2026-09-20-composer-async-operations/T11.md:931-942` —
  `_ComposerJob.request_shutdown()`: when `phase == "running"` it calls
  `self.deliver(_COMPOSER_OPERATION_SHUTDOWN)` immediately (`T11.md:923-929`:
  `task.cancel(marker)`) — no bounded wait, no check of remaining turn budget
  or how close the provider call is to finishing.
- `T11.md:1085-1117` — `ComposerAsyncWorker.stop()` calls `request_shutdown()`
  on every owned job, then `await asyncio.wait(job_tasks)`, which waits only
  for the already-cancelled tasks to unwind and persist their terminal write —
  not for the turn to finish naturally.
- `T11.md:1917-1923` places `composer_async_worker.stop()` right after
  `web_instance_membership.begin_drain()` in `app.py`'s lifespan — i.e. on
  **every** SIGTERM/rolling-deploy drain, not only a crash.
- I traced the exact terminal code the shutdown marker produces
  (`T11.md:1488-1502`, `_terminal_failure`): the comment states explicitly
  *"Lease loss, shutdown, or a cancellation from outside the worker: the
  server stopped this turn (D14 worker_lost)."* — so the user sees the D14
  `worker_lost` 503 body ("The server stopped while composing this request.
  Reload to see what was saved, then resubmit."), not `request_cancelled`.
  This is an honest message and an existing, already-designed-for failure
  code (also used for real crashes) — so this is not a new error *shape*,
  only a new, routine *trigger* for it.
- `grep -n "in-flight\|drain.*turn\|stopTimeout\|graceful"` across T15/T17/T18
  and the spec found nothing describing this as an expected consequence of a
  deploy; `T15.md:330`'s "in-flight run...destroyed" comment is about the
  *pre-existing* proxy-cut bug this plan fixes, not this new self-inflicted
  cancellation on drain.

**Ripple:** The central premise of the plan (`docs/specs/.../design.md:19-25`)
is that a turn survives disruption outside the application's control. It does
not survive disruption fully inside the operator's control: an ordinary
deploy. Because decoupling `composer_timeout_seconds` from the transport
ceiling is the point of the change (turns can now legitimately run for
minutes), the window during which a turn is "in flight and vulnerable to a
routine deploy" grows in direct proportion to the feature's own value — the
longer a turn is *allowed* to run, the more likely a redeploy lands on top of
one. `worker_lost` is a reasonable, already-designed failure code for this,
but nothing in T15/T17/T18 tells the operator to expect it on every deploy,
size stop-timeout accordingly, or consider deploying only when composer
traffic is quiet — and the code gives `stop()` no bounded grace window
(e.g. `asyncio.wait(job_tasks, timeout=drain_grace_seconds)` before escalating
to cancellation) to let a near-complete turn finish inside the platform's own
stop-timeout budget.

There is also a reinforcing-loop variant worth flagging: the running job's
`SessionOperationLease` renews itself through its own background loop
(`src/elspeth/web/coordination/lifecycle.py`, `_renew_forever`, calls
`run_sync_in_worker` at both retry branches), and the async worker's watcher
polls `lease.wait_until_lost()` to decide when to cancel with the
`lease_lost` marker — which *also* maps to `worker_lost` per the same
`_terminal_failure` table. If the shared `run_sync_in_worker` pool (Finding 3)
is saturated, SOL renewal calls can themselves start timing out, causing
leases to appear lost, causing turns to be cancelled as `worker_lost` — whose
own terminal write *also* needs the same saturated pool. This loop is
pre-existing machinery (SOL renewal already goes through this pool for every
synchronous compose today), but this plan is the first thing to add a
perpetual, once-per-second background consumer (the worker's scan+reap loop,
Finding 3) to that same pool, raising the baseline load under which this
spiral could start.

**Confidence:** High on the mechanism (direct code citations, including the
comment that names D14 `worker_lost` as the outcome — corrects an earlier
draft of this finding that mislabeled it `request_cancelled`). Moderate on
real-world severity: `worker_lost` with a "reload and resubmit" message is
still materially better UX than today's silent middlebox hang, so this is a
documentation/robustness gap rather than a functional break.

**Suggested fix / question for the owner:** either explicitly document in
T15/T17's deploy notes that active composer turns are cancelled on every
deploy (and consider timing deploys around usage, or accepting the tradeoff on
the record), or give `stop()` a bounded grace window under the platform's
configured stop timeout before delivering the cancel marker.

## Finding 3 (Warning/Minor-to-Major, config trap + new perpetual load source) — `composer_async_worker_concurrency` is validated up to 256, but every authority call (admit/claim/renew/poll/reap) funnels through the existing process-wide 16-worker/16-queue `run_sync_in_worker` pool shared with unrelated features, and the worker adds the pool's first perpetual, always-on periodic consumer

**Evidence:**
- `src/elspeth/web/async_workers.py:14-19` — `MAX_WORKERS = 16`, `MAX_QUEUED =
  16`, `ADMISSION_CAPACITY = 32`, one pool, process-wide, shared today by
  `secrets/routes.py`, `blobs/service.py`, `composer/pipeline_custody.py`,
  `composer/pipeline_commit.py`, `composer/tool_batch.py`, `composer/service.py`,
  `tutorial_service.py`, `sessions/routes/runs.py`,
  `sessions/routes/workflow/audit_view.py`, `sessions/routes/workflow/library.py`.
- `contract.md:192` — the new `ComposerAsyncOperationAuthority` is "Called
  through `run_sync_in_worker`" for every method. `T11.md:1141,1147,1194,
  1253,1268,1288,...` — the worker's scan loop (`claim_next`), reaper
  (`list_expired_queued` + `list_expired_running`, every
  `composer_async_scan_interval_seconds`, default 1.0s per `contract.md:78`),
  and claim renewal all go through it. `T12.md:601,1122,1155` — the poll and
  cancel routes do too, at `composer_async_poll_after_ms` cadence (default
  1000ms, `contract.md:79`) per actively-polling tab.
- `contract.md:76` — `composer_async_worker_concurrency: int = Field(default=4,
  ge=1, le=256)`. Nothing in T01/T11/T15 ties this range to the shared pool's
  fixed 32-slot capacity, gives the composer authority a reserved/dedicated
  pool, or documents the coupling.
- `T11.md:2045` acknowledges the sharing explicitly ("every call here uses the
  shared 16+16 pool") and makes the worker's own loops retry
  `AsyncWorkerAdmissionTimeoutError` rather than crash — this protects the
  worker's own liveness but does nothing for the other, unrelated callers
  listed above.
- No load/soak/poll-fanout test exists (checked T16.md, T18.md — no hits for
  "concurren[t]", "load test", "soak", or similar beyond a couple of
  concurrency-2 unit-level crash-window tests).

**Ripple:** Before this plan, nothing hammered the shared pool on a fixed
cadence forever; every caller was a one-off tied to an actual user action. This
plan adds the pool's first perpetual, once-per-second (by default) background
consumer that runs for the lifetime of every process whether or not there is
any composer traffic, plus a new per-second-per-active-tab poll load on top.
Under healthy DB latency this is very likely fine (32 slots at millisecond
round trips is a high sustained throughput) — but during any DB
slowness/contention episode, the composer background loops become a steadily
retrying consumer competing for the same scarce slots as unrelated routes
(e.g. an operator trying to read the audit view *during* an incident), at
exactly the moment those slots are scarcest. Separately, `composer_async_
worker_concurrency`'s own declared range (up to 256) invites an operator to
expect real concurrency the shared substrate cannot deliver — worth capping
the validated range or reserving capacity to match the promise, the way
`_AuthAuditWorkers` already reserves capacity away from ordinary work for
must-fire audit writes in the same module.

**Confidence:** High on the mechanism. Low-Moderate on real-world severity —
this is a plausible, currently-unmeasured degraded-mode risk, not a proven
one, and the worker's own retry-on-saturation design (T11.md:2045) means the
worker itself is resilient to short saturation; the exposure is to *other*
callers of the same pool during that saturation window.

**Suggested fix:** give the composer authority its own bounded pool (mirroring
`_AuthAuditWorkers`), or cap `composer_async_worker_concurrency`'s validated
range and document the coupling to `MAX_WORKERS`/`MAX_QUEUED` in the field's
description.

## Checked and confirmed NOT a defect (recorded so these aren't re-litigated)

- **R05 lock-order interaction with `SessionOperationLease.acquire()`.** The
  plan's Execution Notes (`2026-09-20-composer-async-operations.md:86`) flag
  that R05 (`docs/plans/2026-09-23-web-review-remediation/persistence.md:33-42`)
  "fixes a lock order this plan's admission and claim transactions must
  respect (session lock before identity/ledger)." I read
  `SessionOperationAuthority.acquire()` (`src/elspeth/web/coordination/
  repository.py:4756-4843`, which T05 refactors into
  `_advance_exclusive_fence_on_connection`) and confirmed it only touches
  `sessions_table` and `session_operation_fences_table` — never identity or
  ledger rows. R05's actual target (`persistence.md:35`: "Admission still
  reads session without `FOR UPDATE`... then locks identity") is
  `chargeable_admission_authority.py`/`quota_authority.py`, which this plan
  does not touch. The composer async admission's own per-user rate limiter is
  `RepositoryRateLimitAuthority` (`coordination/rate_limit_authority.py:36-44`),
  a *separate* bucket-table authority whose docstring states "No connection or
  transaction escapes this authority... avoiding lock-order cycles with
  admission" — confirmed by `contract.md:18`'s adopted-deviation row (it
  "commits on its own connection," outside the new authority's locked
  transaction). One additional, previously-unstated point worth recording for
  the next reader: `admit`'s `actor_user_id → identities` FK (RESTRICT) takes
  its key-share lock *inside* `locked_session_transaction`, i.e. after the
  session lock — consistent with R05's chosen session-before-identity order,
  which R05's own fix text (`persistence.md:33`) says to audit "including
  foreign-key key-share acquisition." I did not trace every quota/ledger call
  reachable from inside `run_composer_turn` itself (the moved provider-call
  body) for identity/ledger lock ordering — see Information Gaps.

- **Scan loop pulling new work while draining.** I checked whether the worker
  keeps claiming jobs after `begin_drain()`/`instance_draining` is set (it
  consumes the DB queue, not HTTP, so readiness failing alone wouldn't stop
  it). `T11.md:1185-1188`, `_scan_once`: `if self._stopping or
  self._instance_draining.is_set(): return 0` — the scan loop is explicitly
  gated on the same shared `instance_draining` event `begin_drain()` sets
  (`app.state.instance_draining = web_instance_membership.draining`,
  `src/elspeth/web/app.py:1876`), and `begin_drain()` is awaited *before*
  `composer_async_worker.stop()` in the lifespan (`T11.md:1911-1923`). A
  draining instance stops claiming new work before its existing jobs are
  cancelled — no defect here.

- **Rolling deploy / mixed old-and-new-code instances sharing one live
  PostgreSQL, and rollback.** I read T18's generated `DEPLOY` note
  (`T18.md:1670-1695`) and T17 (`T17.md:1-132`). The epoch bump (68) is
  documented as a **hard, single-stop-window cutover** — "Stop the web
  service... Move the session database artifact set aside... Start the
  service" — not a rolling deploy at all; there is no window in which old
  (synchronous) and new (202/poll) code share one live store, because the
  documented procedure requires the service to be fully stopped before the
  store is moved and recreated. This matches the plan's own stated precedent
  (`T17.md:21`, "the same shape as `f5a770d3f`," a prior epoch-66→67 bump) —
  i.e. this is pre-existing ELSPETH operational discipline for any session
  schema epoch bump, not a new risk introduced by this plan. I did not verify
  that `docs/runbooks/staging-session-db-recreation.md` itself spells out the
  equivalent hard-cutover procedure for the ECS/ACA *production* targets
  specifically (only the dev-box systemd note was read in full) — flagged
  under Information Gaps rather than asserted as a gap, since the runbook is
  pre-existing infrastructure this plan only updates the epoch number in.

## Confidence Assessment

| Finding | Confidence | Basis |
|---|---|---|
| Guided client abort ceiling goes stale after decoupling (Finding 1) | High | Plan's own text (`T01.md:1101`) admits the defect; independently confirmed neither T14 nor T15 implements the named fix |
| Severity of Finding 1 is "degraded UX" not "hang" | Moderate | Depends on whether the deployed transport ceiling actually stays below the raised `composer_timeout_seconds` (true on ACA's fixed ceiling; false at ECS package defaults per T15's matched ceiling raise) |
| Shutdown cancels in-flight turns immediately as `worker_lost`, no grace window (Finding 2) | High | Direct code citations, `T11.md:923-942,1085-1117,1488-1502` |
| This is undocumented as an operational deploy consequence | High | Grep across T15/T17/T18 and the spec found nothing |
| Net severity of Finding 2 vs. today's behavior | Moderate | `worker_lost` + "reload and resubmit" is still better than today's silent hang; the gap is operational documentation and lack of a bounded grace window, not a functional break |
| Shared pool now carries a perpetual per-second consumer (Finding 3) | High | Direct code citation, `async_workers.py:14-19` + task files' own `run_sync_in_worker` call sites |
| Pool contention materially degrades unrelated routes under normal load | Low | Healthy-latency math suggests this is fine most of the time; only bites under DB degradation or near-ceiling `worker_concurrency`, neither measured by the plan's test suite |
| R05/`acquire()` lock-order entanglement is narrower than the plan's execution note implies | High | Read the actual `acquire()` body and the rate-limit authority's docstring; both confirmed to not touch identity/ledger tables |
| Rolling-deploy/mixed-version risk is moot (hard cutover, not rolling) | High | Read T18's generated deploy note and T17 in full; matches stated epoch-67 precedent |
| Full quota/ledger call graph inside `run_composer_turn` re: R05 | Insufficient Data | Did not trace every call inside the moved `send_message`/`recompose` bodies for identity/ledger locking during a provider attempt |

## Risk Assessment

**Implementation Risk:** Medium — none of the three findings blocks
correctness of the happy path or the crash-window proofs (spec gate 3); all
three are steady-state/degraded-mode/cross-platform ripple effects that show
up under load, during routine deploys, or on a specific deployment target
(ACA), which is exactly what a pre-merge systems review is positioned to catch
before they surface as a live incident or a silent regression.
**Reversibility:** Moderate — Finding 1 is a small, contained frontend change
(retarget one field read); Finding 2 is a targeted change to `stop()`'s wait
logic plus a docs decision; Finding 3 is a pool-sizing/reservation change
isolated to `async_workers.py` and the new authority's call sites. None
requires a schema/epoch change to fix.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Guided timeout UX silently degrades on ACA (client timer never fires; platform-level abort surfaces instead) | Medium | Likely on ACA at any config where `composer_timeout_seconds` exceeds `composer_sync_timeout_seconds` | Retarget `App.tsx`'s guided abort-ceiling read to `composer_sync_timeout_seconds`; add as an explicit T14/T15 step |
| Deploy cancels a near-complete long composer turn, user sees an unexplained-until-they-reload failure right after a release | Medium | Likely (every deploy with active composer users) | Bounded grace window in `stop()`, and/or document the tradeoff in the deploy runbook |
| Composer background loops compete with unrelated `run_sync_in_worker` callers during a DB slow patch | Low-Medium | Possible, correlates with incidents (worst timing) | Reserve capacity for the composer authority the way `_AuthAuditWorkers` already does |
| Operator sets `composer_async_worker_concurrency` near 256 expecting real concurrency | Low | Possible (the field's own validated range invites it) | Cap the validated range to what the shared pool can serve, or give it a dedicated pool |

## Information Gaps

1. [ ] **Full call graph of `run_composer_turn`'s provider-attempt path for
   identity/ledger locking** — would let me confirm or rule out any *new*
   R05-relevant lock ordering beyond the two authorities I already checked
   (the turn body is stated to be moved-only/unchanged, which if true means no
   new risk, but I did not trace it line by line inside `composer/service.py`).
2. [ ] **ECS/ACA `stopTimeout`/revision-drain configuration values actually in
   use** — would let me state precisely how much grace would be available for
   Finding 2's suggested bounded-wait fix, and whether it's enough to matter
   for realistic turn lengths given the new, larger `composer_timeout_seconds`
   defaults (840s at ECS package defaults per `T15.md:720`).
3. [ ] **Whether `docs/runbooks/staging-session-db-recreation.md` documents an
   ECS/ACA-specific hard-cutover procedure equivalent to T18's dev-box
   systemd note** — I read only the generated dev-box note in full; the
   runbook itself (pre-existing, only its epoch number is touched by this
   plan) was not read end to end.
4. [ ] **A load/soak test exercising N concurrent polling sessions against the
   shared pool** — none exists in T16/T18; without it, Finding 3's real-world
   severity stays a plausible-but-unmeasured risk rather than a proven one.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] Confirm with the plan owner whether Finding 2's behavior (immediate
  cancel on drain) is an intentional, accepted tradeoff rather than an
  oversight — if intentional, this downgrades from "gap" to "must be
  documented in T15/T17," which is the concrete, actionable ask either way.
- [ ] Confirm which task (T14 or T15, or a new follow-up) will own Finding 1's
  fix before merge, since the plan's own T01 note leaves it unassigned between
  the two.
- [ ] Re-grep `run_sync_in_worker` call sites and the frontend `App.tsx`/
  `composer.ts` files after T11/T13/T14/T15 actually land, in case
  implementation diverges from what the task files currently specify.

### Assumptions Made
- The plan's task files (T00–T18, contract.md) accurately describe what will
  be implemented; I reviewed the plan and the current production code it will
  graft onto, not a built/tested version of the feature (it doesn't exist yet
  on this tree).
- "Rolling deploy" means the standard ECS/ACA pattern of draining old tasks
  while new tasks come up; I relied on T18's dev-box deploy note and the
  epoch-67 precedent cited in T17 rather than re-deriving ECS/ACA stop-timeout
  values from Terraform/Bicep in this pass (Information Gap 2/3).

### Limitations
- This analysis does not cover symbol/test-existence correctness (a different
  reviewer's lens per the dispatch brief) or security.
- Quantitative throughput claims (Finding 3) are order-of-magnitude reasoning
  from the pool's own documented constants and cadence settings, not a
  measured benchmark.
