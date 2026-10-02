# Composer Async Operations Implementation Plan (re-based 2026-09-28)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task lives in its own file under [`2026-09-20-composer-async-operations/`](2026-09-20-composer-async-operations/); execute them in order N00 → N19.

**Goal:** Make `POST /{session_id}/messages` and `POST /{session_id}/recompose` acknowledge an accepted turn with a short `202`, run the turn in an app-owned worker that no longer depends on the POST socket, and deliver the exact former `MessageWithStateResponse`, or the exact former public error, through short authenticated polls. A middlebox that cuts idle requests at 60–125 s can then no longer destroy a healthy turn. That includes the first-run tutorial, whose Build now uses `/messages`.

**Architecture:**

- **Admission.** Admission commits a queued `composer_async_operations` row, keyed by the client-minted `operation_id`. The same id is the `message_ingress_receipts` key, so there is one id per send.
- **Start.** A worker on every instance claims the row. In **one** locked transaction it then:
  1. advances the session's COMPOSE `SessionOperationLease` fence;
  2. checks ownership, the bound base and (recompose) the transcript, refusing a moved base before any side effect;
  3. marks the job `running`.

  It then adopts the minted lease.
- **Turn.** The rebuilt route body runs under that lease. A positive fence predicate refuses every non-audit write once the job is cancel-marked or terminal.
- **Terminal.** One composite transaction publishes the assistant row, the audit cohort, the pending-proposal read and the terminal row.
- **Recovery.** A reaper settles expired queued rows and dead owners without replaying a provider turn.
- **SPA.** It keeps the id and body in `sessionStorage`, polls to terminal, and feeds the result into the existing reducers.

**Tech Stack:** Python 3.12, FastAPI 0.136, SQLAlchemy Core on SQLite and PostgreSQL, pydantic v2, asyncio; React + Zustand + Vitest + Playwright.

**Spec:** [`docs/specs/2026-09-16-composer-async-operations-design.md`](../specs/2026-09-16-composer-async-operations-design.md) (second amendment, 2026-09-28).

**Interface contract:** [`contract.md`](2026-09-20-composer-async-operations/contract.md). It fixes every cross-task name, signature, column and wire shape, the owner rulings, and decisions E1–E25. It has no override tables.

**Decision record:** [`panel-2026-09-28/`](2026-09-20-composer-async-operations/panel-2026-09-28/). It holds the storage panel (`RECOMMENDATION.md`, B′ 9/9) and the owner rulings 1–7 (`RULINGS.md`).

**Evidence:** [`findings-2026-09-28/`](2026-09-20-composer-async-operations/findings-2026-09-28/). This is a re-survey pinned at `1effedab2`, with a completeness critique (44/44 anchors verified). The 09-25 plan, findings and review are in the [historical snapshot of `history/2026-09-25/`](https://github.com/dta-au/elspeth/tree/5cf32329378de8042a80c449010f9f0039ac9a05/docs/plans/2026-09-20-composer-async-operations/history/2026-09-25/). They are kept for the review items they resolved, which the task files carry forward by id; none of their line anchors is current.

## Owner rulings this plan implements

- **Scope.** Freeform is the only composer (guided was removed in `7001600fe`). The cutover set is the two routes above. The tutorial rides it with no tutorial branch.
- **R0.** 202 + poll ships now, and a later streaming UI reads the same row.
- **R2.** The terminal is a composite transaction.
- **R3.** The running state is bound to the COMPOSE lease via `adopt`.
- **1 and 6.** One id per send, `operation_id` everywhere. Ingress is the immutable acceptance record written by the worker. The ingress 409s and the SPA transcript-matching recovery are deleted.
- **2.** A positive fence predicate.
- **3.** No events table, but forensic columns on the terminal row.
- **4.** A delete guard, and retention with the session.
- **5 and 7.** A moved base is refused before any side effect, and an absent `state_id` means "no state".
- **B′.** A separate table, with a request normaliser shared with receipts and pinned by a golden vector. If the vector moves, ship C.

## Global Constraints

These are copied from the spec and the rulings. Every task's requirements include them.

- The cutover set is exactly `compose_message` (`POST /{session_id}/messages`) and `compose_recompose` (`POST /{session_id}/recompose`). `kind` is a closed two-value CHECK.
- "The LLM remains the author of pipeline structure; no provider bypass is introduced." There is no tutorial-only dispatch, polling or fallback (composer invariants 1–2).
- An accepted POST returns `202` with `{"operation_id","kind","status","poll_after_ms"}` only after authentication, ownership, strict body validation, soft capacity admission and an atomic queue insert. It never waits for a compose lock, a provider or a worker claim.
- A retry reuses the exact id and body. The server hashes the normalised body (excluding `operation_id`, including `state_id` and, for recompose, `expected_user_message_id`) with session and kind. A mismatch is 409, and a new id is a new user action. At most one nonterminal job exists per session; this is a schema index, enforced as 409 `composer_operation_active`.
- The poll checks ownership on every call, returns `Cache-Control: no-store`, and never returns an exception string or a secret-bearing detail. A missing job is 404.
- The job row, not progress or `inflight_requests`, is the settlement authority.
- The start transaction guarantees there is no side effect before `running`. A `running` row is never taken over and never returns to queued; there is no provider replay. A retry of the same id never creates another user row.
- One absolute deadline is set at admission from `composer_timeout_seconds`. Queue time consumes it, and each stage gets only the remaining time.
- The worker has no `Request` and never calls `request.receive()`. The LLM-call audit cohort is persisted `audit_only` before any cancel terminal.
- A post-202 error keeps today's public HTTP meaning inside the terminal envelope. Unknown exceptions use the generic envelope only after audit and server diagnostics are retained.
- Budgets change only in their relationship. The compose budget is decoupled from the transport ceiling for the async routes. The ceiling still bounds the synchronous consumers: diagnostics, proposal Accept and the tutorial run wait.
- No PostgreSQL advisory lock is added. The in-process `asyncio.Lock` is a queueing aid, not the cross-instance authority.
- AGENTS.md applies throughout:
  - a worktree with both source roots on `PYTHONPATH`;
  - commits by pathspec, and no `git stash`;
  - no `# noqa` or `type: ignore`, and no `getattr`/`hasattr` on owned types;
  - the key-free trust-tier lint compared as a finding set and never re-signed by an agent.

## Review Focus

These are the inputs the spec implies but the task tests are most likely to under-exercise. Each has an owning task that pins it.

1. **Client clock skew.** A browser whose clock is minutes off must neither cancel early nor wait forever. The deadline is a `performance.now()` deadline from `deadline_remaining_ms`, which is computed on the DB clock. Owners: N03, N13, N15.
2. **The session disappears under live custody** (archived elsewhere, or an epoch reset). The poll's `session_missing` 404 stops polling, clears custody and takes today's not-found path, without resubmitting. Owner: N15.
3. **Browser storage refuses the descriptor.** The turn runs from memory custody. After a reload the server's `composer_operation_active` 409 reattaches the user; with the transcript-matching fallback gone, it is the only reattach path. Owners: N14, N15.
4. **Two tabs press Send with different ids.** The D8 index admits exactly one; the loser attaches and never shows a failed row. Owners: N05 (race), N15.
5. **An archived session's running job after its owner dies.** It is settled through `settle_lost_inactive_session` and never leaks capacity. Owners: N05, N12.
6. **The head moves while a send is queued** (fork, revert or Accept in another tab). The result is a terminal 409 `stale_compose_state`, with no user row and no provider call. Owners: N06, N15.
7. **A resend after a stale refusal must not replay the refused id.** Retry mints a new id with the reloaded head; only a network-ambiguous retry reuses the id and body. Owner: N15.
8. **A send typed while `selectSession` is still loading** carries `state_id: null` and would be refused. Send is held until `compositionStateLoaded`. Owner: N15.

## Tasks

| # | File | Deliverable | Depends on |
|---|---|---|---|
| N00 | [N00](2026-09-20-composer-async-operations/N00.md) | Worktree; measurements M0–M12; gate-2 baseline test; **Appendix A**: every exit of both routes and its owning task (spec §5) | docs commit |
| N01 | [N01](2026-09-20-composer-async-operations/N01.md) | Six `composer_async_*` settings; `composer_sync_timeout_seconds` for the three synchronous consumers; behaviour-neutral | N00 |
| N02 | [N02](2026-09-20-composer-async-operations/N02.md) | Shared request normaliser + response hash; receipts delegate; golden vectors committed first; **go/no-go → C** | N00 |
| N03 | [N03](2026-09-20-composer-async-operations/N03.md) | Owned types, strict + transitional legacy DTOs, wire DTOs, request-JSON bound | N02 |
| N04 | [N04](2026-09-20-composer-async-operations/N04.md) | `composer_async_operations` table, bundles, transition + delete guards, D8 index, single-authority policy, PG reflection | N03 |
| N05 | [N05](2026-09-20-composer-async-operations/N05.md) | `ComposerAsyncOperationAuthority` + connection-taking helpers; SQLite + PG races | N04 |
| N06 | [N06](2026-09-20-composer-async-operations/N06.md) | Composite start + precondition gate (ownership, foreign base, moved base, recompose transcript) | N05 |
| N07 | [N07](2026-09-20-composer-async-operations/N07.md) | Post-terminal write inventory → positive predicate (Family S + R) → composite terminal | N06 |
| N08 | [N08](2026-09-20-composer-async-operations/N08.md) | Request lifecycle handle + durable carrier + app-keyed lock registry; both routes behaviour-unchanged | N00 |
| N09 | [N09](2026-09-20-composer-async-operations/N09.md) | Budget threading (compose, planner, settlement) + D10 | N01 |
| N10 | [N10](2026-09-20-composer-async-operations/N10.md) | Error projection with real-handler parity | N03 |
| N11a | [N11a](2026-09-20-composer-async-operations/N11a.md) | `ComposerAppServices`, D6 settlement signatures, the ingress/job binding | N07–N10 |
| N11b | [N11b](2026-09-20-composer-async-operations/N11b.md) | `run_composer_turn` + observation + retargeted structural pins | N11a |
| N12 | [N12](2026-09-20-composer-async-operations/N12.md) | Worker, reaper, lifespan, cancellation and lease-loss watcher (spec gate 4) | N11b |
| N13 | [N13](2026-09-20-composer-async-operations/N13.md) | Poll and cancel routes; test helpers; shared composer fakes | N12 |
| N14 | [N14](2026-09-20-composer-async-operations/N14.md) | Cutover: 202, ingress DDL + rename, deletions, caller migration (incl. `state_id`), ACA P1 redefinition, spec gate 2 | N13 |
| N15 | [N15](2026-09-20-composer-async-operations/N15.md) | SPA cutover (spec gate 5), tutorial gating, e2e and transition-ledger boundary | N14 |
| N16 | [N16](2026-09-20-composer-async-operations/N16.md) | Budget decoupling and deployment mirrors | N14 |
| N17 | [N17](2026-09-20-composer-async-operations/N17.md) | PostgreSQL crash windows, cross-instance and the new PG proofs (spec gate 3) | N14 |
| N18 | [N18](2026-09-20-composer-async-operations/N18.md) | Session epoch 72 (re-read first) + doc/website/CHANGELOG sweep | all |
| N19 | [N19](2026-09-20-composer-async-operations/N19.md) | Full gates (all stages, spec gate 6), lints set diff, local short-idle-proxy browser acceptance | N18 |

Spec verification gates map to tasks as follows:

| Gate | Owning tasks |
|---|---|
| 1 | N02–N07 |
| 2 | N14, plus N19 proxy acceptance |
| 3 | N17 |
| 4 | N12, plus the N08 lifecycle twins |
| 5 | N15 |
| 6 | N19 |

## Execution notes

- **Branch and worktree.** Work on `feat/composer-async-ops` in `.claude/worktrees/composer-async-ops`, cut from the live `release/0.8.1` tip that N00 records. Every command block starts with the task's P0 preamble: an unset `ELSPETH_WORKTREE` silently measures the main checkout.
- **Docs commit first.** This index, `contract.md`, the panel records, the re-survey findings and the second spec amendment land as one docs commit before N00. The plan folder is untracked until then.
- **No interim merge from N03 to N15.** N03 introduces the transitional `Legacy*Request` DTOs and N14 deletes them. A branch merged or paused in between would ship that dual path, against the no-old-pathways rule.
- **Epoch.** N18 is the last code commit. It re-reads `SESSION_SCHEMA_EPOCH` and `_COORDINATION_HARD_CUT_EPOCH` immediately before bumping (72 at plan time) and takes the next free number.
- **Deployment obligation.** The epoch bump requires the operator to move the served session store aside and restore each local account with `elspeth composer users bootstrap-admin local <user> --note ...` (`docs/runbooks/staging-session-db-recreation.md`). Every deploy or drain settles in-flight freeform turns as 503 `composer_operation_worker_lost` (E17), which the CHANGELOG and runbook say.
- **Gates.** The trust-tier CI red is the operator's signing boundary. Tasks compare the key-free finding set to N00's baseline and stage additions for the operator; no agent re-signs. Run `scripts/full-suite-gate.sh` with `--stages ruff,mypy,contracts,lints,pytest,testcontainer`.
- **Stop points.** The plan ends at a locally verified branch. Merging to `release/0.8.1`, pushing and live-cloud acceptance are separate decisions for John.
