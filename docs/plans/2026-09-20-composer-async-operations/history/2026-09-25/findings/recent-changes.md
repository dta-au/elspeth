# Async ops plan — seam: reconcile the spec with recent changes

Measured 2026-09-25 ~03:45 AEST on the main checkout, `release/0.8.1` tip `d479eb2b4`
(`git log -1` -> `d479eb2b4 chore: retire local tracker and code index for shared GitHub workflow`).
`src/` and `tests/` are clean. The spec `docs/specs/2026-09-16-composer-async-operations-design.md` is
modified in the working tree (mtime 03:38:48; the freeform-only scope ruling). The old plan
`docs/plans/2026-09-20-composer-async-operations.md` is **unmodified** against HEAD (`git diff --stat` printed
nothing). The 03:38 amendment touched the spec only.

Read-only. The only write is this file.

---

## 0. Direction conflict. Read this first.

In 37 minutes on 2026-09-25, three artefacts described three approaches to "a turn must survive its HTTP request":

| Time (AEST) | Artefact | What it says |
|---|---|---|
| 03:01:43 | `.claude/lanes/session-ed3c015b/disconnect-progress-report.md:5` | "Defect 3: parked by explicit user direction ... **User is replacing the web interface with streaming** and explicitly parked this work. The earlier plan is superseded by that direction. Do not claim refresh survival fixed." |
| 03:09:04 | `.claude/lanes/session-ed3c015b/plan.md:5` | "Refresh survival is explicitly parked for the **upcoming streaming interface rewrite**." |
| 03:19 (file mtime) | `docs/plans/2026-09-25-composer-live-run-defects-fix-prompt.md`, Defect 3 | A third design: keep the turn alive and let the reloaded page collect it through the existing `/messages` and `/composer-progress`. |
| 03:38:48 | the spec amendment (this plan's input) | Freeform-only 202 + durable poll through `composer_async_operations`. |

The lane notes are a subagent's paraphrase of the user. They are not the user's words to this plan. I can't tell
from the tree which approach supersedes which. The spec amendment is the most recent, and the relayed user request
for this run ("guided is going to be removed long term") matches its scope ruling. Even so, the plan author should
confirm with John that 202+poll is still wanted alongside, or instead of, a "streaming interface rewrite". This is
open question 1.

The old plan's §-level phase list has another pitfall. The live-run branch **did not** implement defect 3. Its lane
wrote a plan (`disconnect-progress-plan.md`) and then parked it. So no competing lifecycle code exists on any branch.
Measured below (§9).

---

## 1. Session schema epoch

- **Memory claim of 67 is correct.** `src/elspeth/web/sessions/models.py:366` is `SESSION_SCHEMA_EPOCH = 67`, and
  `src/elspeth/web/sessions/schema.py:40` is `_COORDINATION_HARD_CUT_EPOCH = 67`. Exact equality is enforced at
  `schema.py:535-536` (`if SESSION_SCHEMA_EPOCH != _COORDINATION_HARD_CUT_EPOCH: _schema_error("coordination schema
  epoch mismatch", ...)`). A `models.py`-only bump fails every session-DB open. The `f5a770d3f` body measured this.
- Set by `f5a770d3f` (2026-09-24, "chore(sessions): session schema epoch 67 for ordered coalesce and source authority
  hashes"). Its `--stat` is the sweep template. It touched 10 files: `CHANGELOG.md`,
  `docs/runbooks/staging-session-db-recreation.md`, `models.py`, `schema.py`,
  `tests/integration/web/composer/guided/test_schema9_epoch.py`, `tests/unit/contracts/test_web_blob_fencing.py`,
  `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`,
  `tests/unit/web/sessions/test_interpretation_events_table.py`,
  `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py`, `tests/unit/web/sessions/test_schema.py`.
  The live `== 67` pins are: `test_schema9_epoch.py:62`, `test_proposal_blob_effect_receipts_schema.py:26`,
  `test_schema.py:368`, `test_web_blob_fencing.py:3204`, `test_interpretation_events_table.py:248`, and
  `test_blob_inline_resolutions_schema.py:70,72` (`PRAGMA user_version == 67`).
- The epoch bump also carries a deploy obligation, from the `f5a770d3f` body: move `sessions.db` aside, start on a fresh
  store (served config `deploy/elspeth-web.env`), and run `elspeth composer users bootstrap-admin local <user> --note
  ...` for every local account.
- The async table needs **epoch 68**. I searched for anyone else claiming 68:
  - `grep -rn "epoch 68|EPOCH = 68"` over `docs/plans docs/specs` matched only unrelated K8s/guided-2.1 docs.
  - The S2 plan says "No DDL and no epoch bump" (`docs/plans/2026-09-25-composer-strict-contracts-s2-plan.md:84`, `:661`).
  - The live-run branch `fix/session-ed3c015b-convergence` has no `models.py`/`schema.py` diff. The §9 instrument
    printed only `composer/service.py` for it.
  - The live-run fix prompt says an epoch bump is "pre-approved" if needed
    (`2026-09-25-composer-live-run-defects-fix-prompt.md:47`). Its landed commits don't use one.
  - Re-check at implementation time. The r1-r2 plan's rule applies: if another lane lands a bump first, redo at the
    next free number (`docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md:809`).
- The r1-r2 plan (§E1, line 323) records six epoch doc/website/receipt pins that "already fail on base". Treat them as
  base-red, not as something this change introduces.

---

## 2. Commits since 2026-09-19 on the turn path, by seam

Instrument: `git log --since=2026-09-19 --no-merges -- src/elspeth/web/sessions src/elspeth/web/composer
src/elspeth/web/frontend/src/stores src/elspeth/web/frontend/src/api` gave 124 lines including merges. I narrowed per file.

- `messages.py` since 09-19: `41aeaeac0`, `1e1a4832d`, `101824e7d`, `44cf55a65`.
- `routes/composer/compose.py`: `8719d8f37`, `41aeaeac0`, `1e1a4832d`, `44cf55a65`.
- `routes/_helpers.py`: `41aeaeac0`, `101824e7d`, `889913b1a`, `beb2660a0`, `44cf55a65`.

### 2.1 Advisor END-gate fact handed to the turn (constrains the worker)

- **`1e1a4832d`** (09-22, "hand the durable advisor gate fact to the END gate"). Both routes parse the prior state
  row's completion-gate envelope **outside every try**:
  - `messages.py:216` `prior_completion_gates_facts = parse_completion_gates(state_record.composer_meta) if ...`
  - `compose.py:135` (same).

  The fact is passed as `completion_gates=` into `composer.compose` (`messages.py:376`; recompose at the matching
  call after `compose.py:203`). `ComposerService.compose` gained `completion_gates: CompletionGateFacts | None = None`
  (`composer/protocol.py`, protocol at ~`:1487-1492`).

  *Constraint.* This is a Tier-1 parse of `state_record`, read **under the COMPOSE lease**. In the async design it
  becomes a worker check after lease acquisition (spec §3). A corrupt envelope is meant to propagate as an integrity
  fault ("a corrupt envelope is Tier 1 and must propagate, not be mistaken for a compose failure"). It must not
  collapse into a generic 4xx terminal envelope. The inventory has to give it an owned terminal (the generic
  `operation_failed` with a diagnostic id plus server audit), and it must happen before `running` so no side effect
  precedes it.
- **`41aeaeac0`** (09-23, "retry transient advisor failures and persist review decisions"):
  - `ComposerResult.advisor_gate_decision: AdvisorGateDecision | None = None` (`composer/protocol.py:276`). Its
    `__post_init__` raises `ValueError("An advisor decision cannot accompany an unsettled pipeline commit intent")`.
  - The save branch widened: `elif result.state.version != state.version or completion_gate_decision_changes(
    prior_completion_gates_facts, result.advisor_gate_decision, result.state):` at `messages.py:845` and
    `compose.py:592`. A turn can therefore write a new state row (and return non-null `state`) **with an unchanged
    graph**, when only the advisor decision changed.
  - `_state_data_from_composer_state(..., advisor_gate_decision=...)` (`_helpers.py:2664`) now resolves gate facts
    via `resolve_completion_gate_facts`.
  - The advisor retry lengthens turns (more provider calls inside `compose()`). That strengthens the case for async and
    changes nothing structurally.

  *Constraint.* The worker must carry `prior_completion_gates_facts` from its own post-lease read into both the
  compose call and the save predicate. The stored result DTO's `state.composer_meta` carries the
  `completion_gates` key.
- **Advisor-block reply publication**: `f776a8a8d`, `862546b40` (END gate skips unchanged graph), `f85d2b471`,
  `de2e86b56`, `19f882b8d`, `4e0d5d0d9`, `d8a6e2902`, `1809379f6`, `4afd73169`, `45707f7ca`, `e69498f6c`. All of
  them live in `composer/service.py`, `no_tool_policy.py`, `control_messages.py`, `withheld_replies.py`,
  `advisor_*.py`, and `execution/completion_gates.py`. **None changed the route response shape.** An advisor block is
  still a 200 `MessageWithStateResponse`: the published reply is in `message`, and the blocked gate fact is in
  `state.composer_meta.completion_gates` plus validation. The terminal-envelope adapter doesn't need a new result
  variant for advisor blocks (see §7).

### 2.2 Recompose history (keep it when moving the body)

- **`8719d8f37`** (09-23, "preserve control history on recompose"): `compose.py:187-190` became
  `chat_messages = _composer_chat_history([record for record in records if record.id != conversation_records[-1].id])`.
  The full transcript is kept so control-audit rows replay in order. The web-review composer sheet calls this "the only
  relevant compose-route drift; keep it when editing" (`docs/plans/2026-09-23-web-review-remediation/composer.md:31`).

### 2.3 Error vocabulary the terminal envelope must carry verbatim

- **`101824e7d`** (09-22, "distinguish unavailable pricing from invalid responses"):
  - `_FREEFORM_PLANNER_FAILURE_HTTP` gained `"cost_unavailable": (503, "The composer could not determine the model
    cost. ...")` (`_helpers.py:2894`).
  - `_freeform_planner_failure_code` maps `COST_UNAVAILABLE -> "cost_unavailable"` (`_helpers.py:2983`).
  - A progress reason was added: `COST_UNAVAILABLE -> service_setup_failed`.
  - `messages.py:~649-661` gives the failed-progress evidence/likely-next copy that branches on `exc.code ==
    "COST_UNAVAILABLE"`.

  A post-202 terminal must preserve `http_status: 503` plus `failure_code: "cost_unavailable"`. The web-review
  contracts sheet (R42, `contracts.md:92`) plans a frontend non-retryable classification for `cost_unavailable`. That
  classification will read it from the terminal envelope after cutover.
- **`44cf55a65`** (09-20, identity finalization): threaded `chat_ingress` / `chat_ingress_inputs` into every error
  handler (`_handle_convergence_error`, `_handle_plugin_crash`, `_handle_runtime_preflight_failure`) and into
  `_post_compose_updates` in both routes. Anchors: `messages.py:143` computes
  `chat_ingress = compartment_ingress_record(body.content, own_compartment_id=settings.compartment_id)` before the lock.
  `compose.py:~149-150` computes it from `last_user_content` **after** the transcript read.

  *Constraint.* For send it can be built at admission from the DTO. For recompose it depends on the transcript, so it
  is a worker-side value.
- `ComposerAdmissionRefused` → `403 {"error_type":"composer_admission_refused","failure_code":"admission_refused",
  "detail": str(exc)}` (`messages.py:683-697`). It is raised inside `compose()` (`composer/service.py:4051`, `:4063`).
  The spec §2 keeps "401/403/404" as pre-202 **auth/ownership** failures. This 403 is a **post-202** policy/quota
  refusal and belongs in the terminal envelope. Say this explicitly so nobody moves it pre-202.

### 2.4 Polling remediation (frontend reuse candidates; the run-stream part is not composer)

- **`7e52d8afc`** (09-22, "admit composer poll responses by owner and by arrival order"), `sessionStore.ts`:
  - `composerProgressReadTicket` / `composerProgressAppliedTicket` / `inflightMessagesReadTicket` /
    `inflightMessagesAppliedTicket` (`sessionStore.ts:~548-561`).
  - `loadComposerProgress(sessionId, { ownerGeneration })` two-mode ownership fence (`sessionStore.ts:~2611-2660`).
  - All four explicit post-stop reads pass `ownerGeneration` (e.g. `sessionStore.ts:2408-2416` in `sendMessage`'s
    `finally`).

  **Directly reusable** for the operation poller: capture a generation before each fetch, apply only while the claim
  is live, and order replies by monotone ticket.
- **`33abfd48f`** (09-23, "answer mid-poll revocation opaquely and keep recovery reads serial"). It adds a
  no-overlapping-reads guard with a 30 s stale bound for the run recovery poll (`executionStore.ts:~860-884`,
  `RUN_RECOVERY_READ_STALE_MS`). The commit body notes "**authFetch sets no timeout**". That is relevant to spec §5
  ("`AbortController` is used only to bound individual short HTTP calls"): today nothing bounds a poll GET.
  `authFetch` is at `src/elspeth/web/frontend/src/api/authSession.ts:14-23` and has no retry or timeout.
- **`7c2acc588`** + **`33abfd48f`**: `ComposerProgressIdentityInactive` / `ComposerProgressSessionUnavailable` (both
  `PermissionError`) are in `coordination/composer_progress_authority.py:~113-127`. The progress poll route translates
  them to `401 "Invalid token"` / `404 "Session not found"` (`routes/composer/state.py:546-549`). **The new `GET
  .../operations/{id}` and cancel routes should mirror that translation exactly**: opaque 401 text, non-disclosing
  404, never a 500.
- **`f2b43cfdc`, `5f7831412`, `e8ec61475`, `60baa12ad`**: the run-stream close codes (`execution/websocket_close.py:49`
  `INTERNAL_ERROR = 1011`, `:52` `BACKEND_UNAVAILABLE = 4503`) and the executionStore REST recovery poll
  (`executionStore.ts:207` `RUN_RECOVERY_POLL_INTERVAL_MS = 3000`). They are **run** progress, not composer. They are
  a pattern precedent only, not code to reuse. The memory's "polling remediation" is this set plus `7e52d8afc`.

### 2.5 Other turn-path-adjacent commits (no route-shape impact)

- `7d981d6cf` (09-22, "preserve decision state across delayed responses"): changed `mergeCompositionProposals`
  (`sessionStore.ts:~1078-1092`). The new rule: `if (previous && previous.status !== "pending" && proposal.status ===
  "pending") continue;`. It exists to stop a delayed response resurrecting a decided proposal. See the design risk in
  §10 on applying a stored terminal result late.
- `07faf477e` (chat cards bound to authoritative review state) and `889913b1a` (`surface_origin` on interpretation
  events): `schemas.py` was touched only by `889913b1a`, and only in interpretation-event DTOs.
- S0/S1 strict tool contracts (`94e8234ef` ... `1c4cd049e`, `d7f75b93f`, `20aa1a89b`, `04b46a6d7`): these live in
  `composer/tools/*`, `tool_batch.py`, `service.py`, `boot_probe.py`, `strict_transport.py`, and `app.py`. **None
  touches the two route handlers or `MessageWithStateResponse`.** The one relevant `app.py` fact: the composer boot
  probe runs in `_service_lifespan` at `app.py:743-~870`, before `yield` at `app.py:925`. The new worker should start
  after the probe and after recovery/orphan-sweeper wiring.
- Branch-order fixes `37a24ca39`, `6d2ec43c4`, `eeb40718c` changed authority hash preimages (the reason for epoch 67).
  No route impact.

---

## 3. The turn-end write sequence today (spec §4 "same transaction" is not how it works now)

Both routes run the same ordered awaits after `compose()` returns, all under the COMPOSE lease context:

| Step | `messages.py` | `compose.py` | Transaction |
|---|---|---|---|
| auto-commit settlement (if `pipeline_commit_intent`) | `:808` `settle_auto_commit_intent(request=request, ...)` | `:555` | own |
| versioned/decision save predicate | `:845` | `:592` | — |
| guided-transition commit (state + assistant) | `:922` `service.commit_transition_response` | `:665` | own (atomic state+msg) |
| plain state save | `:933` `service.save_composition_state` | `:676` | own |
| unchanged-version guided transition | `:963` | `:706` | own |
| assistant row (if not already written by a branch above) | `:978` `service.add_message(... writer_principal="compose_loop")` | `:721` | own |
| tool+LLM audit cohort | `:994` `_persist_turn_audit_cohort(...)` | `:734` | own, single-transaction cohort (elspeth-90231248dc) |
| read pending proposals | `:1027` `_pending_proposal_responses(service, session.id)` | `:760` | **read after commits** |
| build DTO, `terminal_status = "completed"` | `:1028-1033` | `:761-766` | — |

That is 3–4 separate commits followed by a read. Spec §4 says: "Persist the public final response and transport
terminal in the same session transaction as the final assistant/result publication." The implication for the plan:

- It needs a **new service method** (inside the session mutation authority) that, in one transaction, writes the
  last publication (the audit cohort, or assistant row plus cohort), reads the pending proposals, validates and hashes
  the DTO, and performs the fenced terminal CAS on the job row. `_persist_turn_audit_cohort`'s own single-transaction
  contract must survive inside it.
- Alternatively, the design must accept a terminal write *after* the cohort commit and treat "cohort committed, job
  not terminal" as a crash window. The reaper then settles `worker_lost`, and the rows are visible on reload. The
  spec's crash table already allows "existing fenced writes and audit remain visible on reload".
- The proposals list is read **after** the commits today. If it is read inside the terminal transaction, it will
  match the stored DTO.

Mid-turn writes are fenced only by the COMPOSE lease today. The compose loop itself commits assistant/tool rows
mid-turn (`turn_audit.persist_compose_turn_async`; `composer_turn_end_assistant_row` docstring,
`_helpers.py:1494-1545`). Spec §1 requires **both** fences for every worker write. That means threading the job fence
through `SessionOperationContext` (`contracts/session_operation.py:48`) or checking it at the lease. `lifecycle.py` and
`session_operation.py` have **no commits since 09-19**, so the lifecycle primitives are stable.

`composer_turn_end_assistant_row` (`_helpers.py:1494`) raises `AuditIntegrityError` at `:1547` and `:1560`. Those
escape both routes' `except` arms (send catches only `GuidedCustodyIntegrityError` at `messages.py:1035`; recompose
has **no** `GuidedCustodyIntegrityError` arm; its arms are `InvariantError` `compose.py:768` and `CancelledError`
`:787`) and reach the app-level handler. The exit inventory must classify them as post-202 terminal exits.

---

## 4. Request-object coupling the worker must replace

Spec §3: "The worker receives no `Request` and never calls `request.receive()`." A per-route grep undercounts because
two reads live in helpers. The complete coupling is:

- `request.app.state.*`: 14 textual sites per route (`grep -on` over lines 1–1180 of `messages.py` and all of
  `compose.py`, 14 each). The worker needs an app-state handle.
- `_request_plugin_policy_context(request, user)` (`_helpers.py:373-381`) builds `plugin_snapshot` via
  `request.app.state.plugin_snapshot_factory(user)`. Called at `messages.py:217`, `compose.py:110`.
- `_get_session_compose_lock_registry(request)` (`_helpers.py:352-364`) is lazily attached to `app.state`. Called at
  `messages.py:144`, `compose.py:112`.
- `_get_composer_progress_registry(request)` (`_helpers.py:367-369`).
- **Hidden:** `_composer_progress_sink(registry, request, ...)` (`_helpers.py:382-403`) reads
  `request.state.composer_request_lease`. That attribute is set **only** by `_track_compose_inflight`
  (`_helpers.py:2492`, `request.state.composer_request_lease = lease`). The worker cannot publish progress without
  its own `ComposerRequestLease` (see §5).
- **Hidden:** `_track_compose_inflight` reads `request.url.path` to pick the metrics surface label
  (`surface = "guided" if "/guided/" in request.url.path else "freeform"`, `_helpers.py:2485`). The extracted
  lifecycle needs the surface label as an explicit argument.
- `settle_auto_commit_intent(request=request, ...)` (`routes/composer/pipeline_settlement.py:447-458`) uses only
  `request.app.state` (sites `:196-421`: session_service, composer_service, settings, plugin_snapshot_factory,
  catalog_service, operator_profile_registry, session_engine, scoped_secret_resolver). Its last change was
  `44cf55a65` (09-20).
- `_cancel_on_client_disconnect(request)` at `messages.py:357`, `compose.py:202`. It is **not mounted** around the
  detached turn (spec §3).
- `_verify_session_ownership(session_id, user, request)` at `messages.py:140`, `compose.py:107`.

---

## 5. Three leases, not two

Spec §1 names the `SessionOperationLease` and the transport job fence. The tree has a third one that the frontend
depends on:

1. `SessionOperationLease` COMPOSE (`coordination/lifecycle.py:272`), acquired at `messages.py:147`, `compose.py:115`.
2. **`ComposerRequestLease`**, a row in `composer_inflight_requests` created by
   `SessionComposerProgressAuthority.begin_request` (`composer_progress_authority.py:146-167`). It is renewed by
   `_track_compose_inflight`'s heartbeat every `_COMPOSER_HEARTBEAT_SECONDS = 15.0` (`_helpers.py:2338`) against
   `_COMPOSER_REQUEST_LEASE_SECONDS = 60` (`_helpers.py:2340`). `heartbeat_request` (`:267-287`) **re-checks identity
   active + session ownership** and raises `ComposerProgressIdentityInactive` / `SessionUnavailable`. The heartbeat
   maps those to `_COMPOSER_HEARTBEAT_LEASE_LOST` and cancels the owner. So a user deactivated mid-turn cancels the
   turn today. The worker inherits that behaviour if it reuses this lease.
3. The new job claim/running fence.

The **only** settlement signal the SPA has today is `inflight_requests`, the count of live rows in (2):
`get_latest` at `composer_progress_authority.py:416-436`, consumed by `waitForCancelledComposeToSettle`
(`sessionStore.ts:~805-826`). `resyncAfterAbortedComposeTurn` and `resyncAfterAmbiguousComposeFailure`
(`sessionStore.ts:939`, added `e3bd9562a` on 09-15) both wait on it.

Spec §2 says the operation row, not `inflight_requests`, is the settlement authority. The plan must choose one of two
paths, and I don't choose here:

- (a) the worker holds a `ComposerRequestLease` for the whole job, so `inflight_requests` stays truthful and progress
  can publish; or
- (b) the abort/ambiguous resync paths are rewired to the operation row, and (2) becomes progress-only.

Either way, the `uncancel()` + 503 arm of `_track_compose_inflight` (`_helpers.py:2594-2612`) and the disconnect
watcher's `uncancel()` bookkeeping (`_helpers.py:2233`, `:2294`, `:2322`) are what spec §3 says to preserve in an
operation-owned path. The lifecycle region (`_helpers.py:2184-2810`) was last changed by `3bf4ec7f6` on 09-15. The only
09-19+ hit in `git log -L2184,2810` is `41aeaeac0`, whose hunk is in `_state_data_from_composer_state`, not the
lifecycle. The lifecycle is stable.

---

## 6. Wire / DTO facts

- **`/recompose` has no request body today.** The signature is `recompose(session_id, request, user, rate_limiter,
  _inflight_tally)` (`compose.py:92-104`). The client sends a bodiless POST (`client.ts:917-929`). Spec §2's
  "freeform send and recompose DTOs gain a required `operation_id`" means **creating** a `RecomposeRequest`. A
  bodiless POST then becomes a 422.
- `SendMessageRequest(_RequestModel)` (`schemas.py:145-158`: `content: str` 1..65536, `state_id: UUID | None`).
  `_RequestModel` is `ConfigDict(extra="forbid")` only (`schemas.py:68-71`, "Tier 3 request base: allow coercion").
  `_GuidedOperationRequest` is `ConfigDict(strict=True, extra="forbid")` with `operation_id: str` (36 chars)
  canonical-UUID validation (`schemas.py:74-90`). `guided_operation_request_hash` **refuses** a non-strict DTO
  (`sessions/guided_operations.py:26-27`). The spec says the freeform codec is "the model to follow, not a function
  to share", so the strictness decision is still open. Under `strict=True`, `state_id: UUID` would reject a JSON
  string in FastAPI's python-mode validation. That is presumably why the guided base types `operation_id` as `str`.
- `MessageWithStateResponse` is unchanged since 09-19 (`schemas.py:228-237`: `message: ChatMessageResponse`,
  `state: CompositionStateResponse | None = None`, `proposals: list[CompositionProposalResponse]`).
  `ChatMessageResponse` (`:193-225`) and `CompositionStateResponse` (`:359-380`, including `composer_meta`) are also
  unchanged. `git log --since=2026-09-19 -L285,420:schemas.py` printed nothing.
- **Frontend error parsing is `Response`-bound.** `parseResponse` (`client.ts:243-490`) builds `ApiError` inline from
  a `Response`: status, JSON body, and the `X-ELSPETH-Plugin-Snapshot` header at `~:483`. No `body -> ApiError` helper
  exists, and the terminal envelope `{http_status, error_type, body}` needs one so the existing
  `sendMessage`/`retryMessage` error reducers (`sessionStore.ts:~2306-2330`) see the same `ApiError`. The only
  producer of that header is `catalog/routes.py:49`. `grep` found no compose route setting headers or `Retry-After`
  (`rate_limit.py:135,167` sets `Retry-After` on the **pre-202** rate-limit 429, `messages.py:138` /
  `compose.py:106`). The terminal envelope doesn't need to carry headers.
- `guidedOperationRetry.ts` exists (last change `0de8c3029`, 07-31). It is the existing client-minted-UUID plus
  persisted-descriptor pattern: `acquireGuidedRetry` at `:242`, `crypto.randomUUID()` at `:276`,
  **`sessionStorage`** at `:53-55`, schema `guided-operation-retries.v2`, 24 h / 16-descriptor / 8 KiB bounds. Its
  kinds already include the non-guided `state_revert` and `session_fork` (`:1-9`). It is a reuse candidate even
  though guided is out of scope. Note that `sessionStorage` survives reload of the same tab but not a new tab, which
  matters for "reload resumes polling".
- The frontend has one send/recompose funnel. `api.sendMessage` is called only at `sessionStore.ts:2215` and
  `api.recompose` only at `:2853`. Positive control: both matched. Every UI entry point goes through the store
  (`ChatPanel.tsx:2070,2105,2145,2276,2297`, `SideRailValidationBanner.tsx:55`). `composer.compose` is called only at
  `messages.py:358` and `compose.py:203`. The tutorial calls neither route (`grep /messages|/recompose` over
  `frontend/src` excluding tests showed only `client.ts:752,769,906,921` plus comments). `tutorial_service.py` has
  no `.compose(` call.

---

## 7. What did not change (the good news)

- The response shape (§6). An advisor block is still a 200 `MessageWithStateResponse`, so the terminal success union
  is exactly one DTO for both kinds.
- The lifecycle helpers `_cancel_on_client_disconnect` / `_track_compose_inflight` (§5).
- `useComposer.ts`, `config/composer.ts`, and `components/chat/ComposingIndicator.tsx` have **no commits since 09-19**.
  Instrument: per-file `git log --since=2026-09-19 --no-merges`. Positive control: the same command lists 14 commits
  for `ChatPanel.tsx`. Stop is still `activeControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON)`
  (`useComposer.ts:66`). The client deadline is `DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + COMPOSE_CLIENT_GRACE_MS`
  (`config/composer.ts:28-29`).
- The timeout coupling: `WebSettings._validate_composer_timeout_transport_headroom` (`config.py:1187-~1210`). Its last
  change was `f10895f7c` (08-17) per `git log -L1187,1232`. `composer_timeout_seconds` is at `config.py:325`,
  `composer_transport_idle_ceiling_seconds` at `:335`. The only other consumer is `tutorial_service.py:413`
  (`run_timeout_seconds = ceiling - headroom`). Spec §3's anchors hold. `config.py` did gain `composer_strict_tools`
  (`:309`, `50a2b67cd`), which is unrelated.
- `app.py` lifespan: `_service_lifespan` (`app.py:598`). The periodic orphan sweeper (`app.py:904-922`) is the
  template for the worker/reaper: `asyncio.create_task(...)`, then `add_done_callback` requesting process recovery on
  fatal failure, then cancel+await in the `finally`, then drain via `web_instance_membership.begin_drain()`.
- **Instance-wide provider-turn limit** (spec line 50–52 asks for this). `grep -rn Semaphore src/elspeth/web` found
  nothing. Positive control: the same grep over `src/elspeth` matches `plugins/infrastructure/pooling/executor.py:124`.
  The only instance-wide bound both paths share is the `run_sync_in_worker` thread pool
  (`web/async_workers.py:87-100`, `AsyncWorkerAdmissionTimeoutError`, documented in `e8ec61475`). It bounds sync DB
  work, not provider turns. Per-user admission is `WebRateLimiter.check` (`middleware/rate_limit.py:103-167`) plus the
  in-compose chargeable admission (`composer/service.py:4051,4063`). No existing limit counts concurrent provider
  turns, so guided and freeform share none.

---

## 8. Whole-tree gates the new table trips

- `tests/unit/architecture/test_digest_column_shape_checks.py`. Added by `3c0d5b549` (09-21, "shape CHECKs on every
  digest column"). It keeps an explicit inventory of every digest column, evaluated against the live SQLite CHECKs.
  New `request_hash` and result-SHA-256 columns need `_lower_sha256_constraints(...)` (`models.py:406-418`) or the
  inline `_lower_sha256_check` form that `guided_operations` uses (`models.py:1129-1133`), **plus** an inventory entry.
- `tests/unit/architecture/test_session_db_mutation_authority.py`: fail-closed writer inventory keyed by AST
  fingerprint plus ordinal. It needs a `TablePolicy("composer_async_operations", "session", "<Authority>")` row (cf.
  `:79` for `composer_inflight_requests`) and the writer routed through that named authority. `7c2acc588` had to
  re-derive this gate's pins after touching the progress authority, so expect the same here. The r1-r2 plan (§E1)
  records two line-pinned ids in this file as base-red at `c4c52c110`. Compare to base, not zero.
- PostgreSQL CHECK reflection: `tests/testcontainer/web/test_schema_probe_postgres.py`. **Lesson from `889913b1a`**
  (09-21): a CHECK spelt `(origin = 'x') = (hash IS NOT NULL)` reflects with `::text` casts that the startup shape
  comparator can't normalise, and every fresh PG store then reads as stale ("45 of 83 schema-probe ids red while
  SQLite was green"). Spell per-status NULL-bundle CHECKs as AND/OR arms. AGENTS.md also records the older
  one-element `IN` → `=` reflection trap (elspeth-d0e62aea41). This applies to a closed two-value `kind` CHECK:
  two values avoids the one-element case, but a one-value status arm would hit it.
- The `schema.py` identity/epoch machinery and every epoch pin (§1).

---

## 9. In-flight work: file-level conflict risk

Instrument: for every worktree branch ahead of `release/0.8.1`, `git diff --name-only $(merge-base) $branch --` over
the files this plan will touch (`messages.py`, `compose.py`, `_helpers.py`, `schemas.py`, `schema.py`, `models.py`,
`service.py`, `protocol.py`, `app.py`, `config.py`, `sessionStore.ts`, `client.ts`, `useComposer.ts`,
`config/composer.ts`, `coordination/`, `composer/progress.py`, `composer/service.py`). Positive controls: it printed
`composer/service.py` for `fix/session-ed3c015b-convergence` and `coordination/identity_authority.py` for
`fix/credential-generation-fence`. It printed nothing for the other 18 branches (k056-*, sweep-*, replay-*,
review-*, gateway-bounds, 5887, p6b).

| Work | State | Files it shares with this plan | Risk |
|---|---|---|---|
| `fix/session-ed3c015b-convergence` @ `4f6d47149` (7 ahead; release merged in 03:27; `plan.md:5` says merge to local release after acceptance) | about to land | `composer/service.py`; frontend `ChatPanel.tsx` (adds `handleRepairGraph` → `sendMessage(repairGraphPrompt(...))`, a **new send call site**), `ComposingIndicator.tsx` (timer keyed on `composerProgress.request_id` + `updated_at`), `DecisionPanel.tsx`, `lib/suggestionPrompts.ts` | Medium. The old plan §6 edits `ChatPanel.tsx`, so expect a rebase. The new call site goes through the store funnel, so it is covered automatically. The `ComposingIndicator` change assumes progress stays advisory and keyed by `request_id` (the user message id for send, `messages.py:253`). The worker must keep publishing progress with that `request_id`. It did **not** touch the route lifecycle (defect 3 parked). |
| S2 strict contracts (`docs/plans/2026-09-25-composer-strict-contracts-s2-plan.md`) | planned; start gated on John's stability call (`:30-40`) | `composer/*` tool layer; a comment in `sessions/models.py:1683-1685` (`composition_rejection_events`); `app.py:2358-2368` status block (read-only reference) | Low. "No DDL, no epoch bump" (`:84`, `:661`). A textual `models.py` merge is possible but in a different region. |
| Live-run defects fix prompt (same file set as the branch above) | defects 0/0b/1/2/4/5 committed on the branch; **defect 3 parked** | would have hit `messages.py`, `_helpers.py`, `useComposer.ts` | Resolved by parking. See §0. |
| Web-review remediation (`docs/plans/2026-09-23-web-review-remediation.md` + 5 sheets) | **not started**: no `.claude/lanes/web-review-remediation-20260923/`; no R-numbered commits after `73fc6fc81` | Lane B owns `messages.py`, `compose.py`, `sessions/service.py`, `_helpers.py` (`composer.md:60`, `:70`); J/K/L own `sessionStore.ts`, `api/client.ts` (`frontend.md:25,27,38-49`); G owns the shared progress mapping in `compose.py:422-447` (`contracts.md:98`); C (R05) owns the lock order across "admission, ticket, heartbeat, progress" (`persistence.md:24,33-41`) | **High, both directions.** The plan claims **exclusive** file ownership. Whoever lands first forces the other to rebase. R05 especially: the new durable admission path adds another lock-order participant. The plan also still says "current epoch 65" (`:21`), which is stale (now 67). |
| Old async plan `docs/plans/2026-09-20-composer-async-operations.md` | unmodified vs HEAD | — | Stale against the amended spec. See §11. |

---

## 10. Design risks (for the plan author)

1. **Turn end is multi-transaction** (§3). The spec's single terminal transaction needs a new composite service
   method, or an explicit "cohort committed, job not terminal" crash window. Proposals are read after the commits
   today.
2. **The third lease and the SPA's settlement signal** (§5). `inflight_requests` is the frontend's only quiescence
   signal today. Also, `heartbeat_request` cancels the turn when the identity is deactivated or the session archived.
3. **The progress sink is coupled to the request-scoped lease** (§4). `_composer_progress_sink` reads
   `request.state.composer_request_lease`, and `_track_compose_inflight` reads `request.url.path`.
4. **DTOs.** Recompose has no body. Freeform DTOs are Tier-3 coercing, not strict. `parseResponse` has no body-only
   error builder (§6).
5. **Tier-1 pre-compose parses in the worker.** `parse_completion_gates` (`messages.py:216`, `compose.py:135`),
   `AuditIntegrityError` from `composer_turn_end_assistant_row`, and recompose's missing
   `GuidedCustodyIntegrityError` arm all need owned terminal classification, not a 4xx envelope (§2.1, §3).
6. **Late application of a stored result.** The success reducers (`sessionStore.ts:~2229-2295` send,
   `~2863-2905` recompose) take `state ?? s.compositionState` with no monotonic-version guard. They compute
   `lastComposeChangedPipeline` against the client's current version. After a reload the client already holds the
   newer state from `GET state`, so a replayed terminal result could (a) mis-badge "Response ready" and (b) if another
   tab or action advanced the session after the job settled, roll `compositionState` back. `mergeCompositionProposals`'
   non-pending guard (`7d981d6cf`) protects proposals only.
7. **Whole-tree gates** (§8). The digest inventory, mutation-authority policy and PG reflection spelling.
8. **Merge order** against the live-run branch and the web-review lanes (§9).
9. **Direction conflict** (§0): streaming rewrite vs 202+poll.
10. `isComposing` is in-memory (store gate `sessionStore.ts:2184`, `:2824`), and `useComposer`'s pre-checks read it (`useComposer.ts:51`, `:59`).
    Reload reattach needs a persisted "active operation" gate, or a second send is admitted after reload.

---

## 11. Old plan sections the 03:38 amendment made stale

`docs/plans/2026-09-20-composer-async-operations.md`:

- Header, "Goal"/"Scope" (lines 4-11): "five Composer authoring routes", "One hard cutover for ... `/guided/plan`,
  `/guided/respond`, and `/guided/chat`".
- §0.2 (lines 37-45): the exit inventory over `guided_plan.py`, `guided.py`, `guided_chat_atomic.py`.
- §0.3 (lines 46-49): the five-route frontend side-effect inventory.
- §1.1 (line 67): "closed five-kind". The amended spec §1 has a closed **two**-value `kind`.
- §2.1 (lines 114-116): "The three guided routes use `guided_operation_request_hash` exactly".
- §2.4 (lines 129-132): "For guided turns, bind the internal guided operation to the same UUID/hash".
- §3 (line 143): "the five route modules".
- §5 (lines 209-239): the whole five-route cutover, including `guided_plan.py`, `guided.py`,
  `guided_chat_atomic.py`, `test_guided_plan_terminal_publication.py`, `test_guided_chat_integrity.py`, and "Remove
  `_track_compose_inflight` from these five route signatures". Under the amended spec the dependency **stays** on the
  three guided routes.
- §6 (lines 241-274): `types/guided.ts`, `guidedOperationRetry.ts` (as a guided file), `client.guided.test.ts`,
  `sessionStore.guided.test.ts`, the "five-way success union", "guided state".
- Final acceptance (lines 299-303): "each of the five buffered routes".
- **Missing from the old plan, now required by the amended spec**: the guided **budget cap** (spec lines 211-217).
  Once the compose budget may exceed the transport ceiling, guided turns are bounded by `min(composer_timeout_seconds,
  ceiling - headroom)`, and every previously valid config keeps its guided bound. The old plan §3.5 only decouples.
  It also misses the spec §3 requirement that the three guided routes keep identical heartbeat behaviour, "proved by
  their existing heartbeat tests, not rewritten ones".

Test files the old plan cites. All exist (`ls`): `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py`,
`tests/unit/web/sessions/routes/test_composer_request_telemetry.py`,
`tests/unit/web/sessions/test_recompose_admission_refused.py`, `frontend/src/api/client.recovery.test.ts`,
`frontend/src/api/client.guided.test.ts`, `frontend/src/stores/guidedOperationRetry.ts`.

---

## 12. Instruments and controls used

- Bare `raise` in the routes: `grep -n '^\s*raise$'` → `messages.py:1050`, `:1154`; `compose.py:826`. The
  `raise HTTPException` grep (positive: `messages.py:406`) misses these, and it also misses helper-raised
  `AuditIntegrityError` (§3). This is why the exit inventory needs an AST or control-flow instrument, as the old plan
  §0.2 already says.
- `Semaphore`: negative under `web/`; positive `pooling/executor.py:124` (§7).
- Per-file `git log --since=2026-09-19`: positive `ChatPanel.tsx` (14 commits); negative `useComposer.ts`,
  `config/composer.ts`, `ComposingIndicator.tsx` (§7).
- Branch-overlap scan: positive `fix/session-ed3c015b-convergence` and `fix/credential-generation-fence` (§9).
- Epoch: constant read directly from `models.py:366`. `f5a770d3f --stat` for the sweep set.
