# Architecture review — composer async operations plan (2026-09-25)

Reviewer lens: senior-architect / SME protocol. Scope: `docs/plans/2026-09-20-composer-async-operations.md`,
`.../contract.md`, T00–T18, `.../findings/*`. Tree: `release/0.8.1`. Repo read-only except this file.

## Method

Read the plan index, the contract (including "Adopted deviations"), the spec, and the task files T00–T13 in
full or in targeted excerpt (T00–T04 in full, T05/T06 headers + interfaces, T10's `run_composer_turn` body in
full, T11's reaper `_reap_running`). Cross-checked every `CONTRACT DEVIATION`/`RECONCILED` marker in T04, T10,
T11 against the contract's own "Adopted deviations" table and against the plan's "Review Focus" list. Verified
file existence for every document the task files cite (`RECONCILE.md`) with `find`/`git check-ignore`.

## Findings

### 1. [MAJOR] Contract-mandated fix for the archived-session capacity leak is not implemented; the only record of that gap lives outside the plan tree

The contract's own "Adopted deviations" table (`contract.md:21`) states, as an *override that wins over every
later section*:

> `archived-session running row | not specified | settle_lost_inactive_session(*, session_id, operation_id,
> failure, cancelled_failure) (T04), an unfenced settle guarded inside the locked transaction by
> sessions.archived_at IS NOT NULL; the reaper's OWNER_INACTIVE arm calls it (T11) | the reaper's COMPOSE
> acquire raises OWNER_INACTIVE for an archived session, so without it the row stays running and charges
> capacity forever; the archive already proves the owner's fence is dead`

The plan's own front matter restates this as a required behaviour, not an optional nicety
(`2026-09-20-composer-async-operations.md` § Review Focus, item 5):

> "An archived session's running job after its owner dies. Expectation: the reaper settles it through
> `settle_lost_inactive_session`. It never stays `running` and never counts against
> `composer_async_max_queued_operations` indefinitely. Owner: T04 + T11."

Neither task implements it:

- `T04.md` — I enumerated every method Task 4 defines on `ComposerAsyncOperationAuthority` (`admit`, `get`,
  `claim_next`, `renew_claim`, `release_claim`, `request_cancel`, `settle_unstarted`, `list_expired_queued`,
  `list_expired_running`, `settle_lost`, `count_nonterminal`). `settle_lost_inactive_session` is absent.
- `T11.md:1650-1670` (`_reap_running`) — on `SessionOperationFenceLost` with `exc.reason is
  FenceLossReason.OWNER_INACTIVE`, the code does not settle the row at all. It logs
  `composer_operation.reap_skipped_inactive_session` with `counts_against_capacity=True` and returns
  `_ReapOutcome.SESSION_INACTIVE`. The comment at that line reads: "Unresolved W18 hole (Task 0 Review note
  11): the row stays `running` and counts against D3 capacity. Say so on every sweep rather than leak capacity
  silently." `T11.md:2044` restates it: "The reaper skips `FenceLossReason.OWNER_INACTIVE` forever... The fix
  is unresolved and recorded as a residual in RECONCILE.md."

That is a genuine, permanent resource leak: once an owner instance dies (or a session is archived) while a
freeform turn is `running`, the job row never reaches a terminal state. It continues to count against
`composer_async_max_queued_operations` (D3) forever, and — because a poll against an archived session's
operation goes through the same session-ownership check as every other route (D7: archive makes the worker's
start composite raise `SessionOperationFenceLost` → terminal 404 "Session not found") — the *client* can never
observe or clear it either. The only way out is a hard delete of the session (`ON DELETE CASCADE`, D7), which
is not something a leaked capacity slot triggers on its own. Under sustained load (or an operator archiving
sessions routinely, which is a normal admin action) this can exhaust `composer_async_max_queued_operations`
and start returning 429 to every new user, with no operational signal beyond a log line nobody is necessarily
alerting on.

The only place this is tracked as a *known, accepted* residual is `RECONCILE.md`:

```
R1. W18 capacity leak for a `running` row in an archived session (T00 Review note 11). ...
    Not fixed: it needs a new authority write path (design, not reconciliation). Proposal:
    Task 4 adds `settle_lost_inactive_session(...)` ...
    Mitigation applied: T11 now logs `composer_operation.reap_skipped_inactive_session` on every sweep.
```

But that file is **not part of the plan tree the task told me to review**. It lives at
`.claude/lanes/async-ops-plan-2026-09-25/tasks/RECONCILE.md` — a lane-scratch path that
`git check-ignore -v` confirms is gitignored (`.gitignore:67 .claude/lanes/`), i.e. it is not committed, not
under `docs/plans/2026-09-20-composer-async-operations/`, and not among the `findings/` evidence files the plan
index names as its evidence base. AGENTS.md's own worktree-cleanup rules mark an untracked, non-ignored-content
directory like this as a candidate for `--discard-ignored` cleanup once the lane is done. If this document is
lost (or simply never read by whoever executes the plan against `docs/plans/...` only), the record of "this is
a known-accepted gap, not an oversight" disappears and the contradiction between the contract's binding table
and T04/T11's actual content stands as an unexplained defect.

**Impact on my review:** taken at face value, the shipped task files (T04 + T11) violate the contract's own
"Adopted deviations" override and leave a stated Review Focus acceptance criterion unmet, with a resource leak
as the consequence and a queue-capacity DoS as the escalation path. Taken together with `RECONCILE.md`, it is a
consciously deferred fix with a stated design proposal — but that acceptance was never folded back into the
committed plan artifacts, so anyone auditing `docs/plans/2026-09-20-composer-async-operations/` alone (as this
review was scoped to) sees a live contradiction and a dangling reference to a document that is not there.

**Recommendation:** either (a) implement `settle_lost_inactive_session` in T04 and wire T11's `OWNER_INACTIVE`
arm to call it, closing the gap before this plan ships, or (b) if the leak is being knowingly deferred, move
`RECONCILE.md`'s R1 (and ideally the whole reconciliation record) into `docs/plans/2026-09-20-composer-async-operations/findings/` or fold it into `contract.md` itself, and add an explicit line to the plan's own Review Focus
table and to T11's deliverable description stating the leak is accepted and by what mitigation (a metric/alert
on `composer_operation.reap_skipped_inactive_session`, and/or an operator runbook step to hard-delete or
otherwise reap archived sessions with a stuck running job). Silence in the shipped plan is not an acceptable
resting state for a table-policy authority that explicitly promises capacity bounding (D3).

### 2. [MAJOR] `run_composer_turn` (T10) is an ~800-line function that interleaves two route bodies rather than staying one coherent function

Per the contract (`contract.md:332-337`) and T10 (`T10.md:1741-2554` in the task file), `run_composer_turn` is
"the moved bodies of `send_message` / `recompose` from the lock onward, one function with a per-kind
preamble... typed ladder unchanged (still raises `HTTPException` bodies)". I read the function body in full.
It spans roughly 800 lines (T10.md:1741 to the next top-level `def` at 2554, which is a test) and is not
cleanly "one function with a per-kind preamble": the per-kind branching recurs throughout the body, not just at
the top —

- `T10.md:1799-1824`: a genuine per-kind preamble (`if type(request) is SendMessageRequest: ... else: ...`
  for the user-message insert vs. the recompose transcript precondition).
- `T10.md:1854-1863`: a *second* per-kind branch, nested one level inside the shared `try:` block that starts
  the provider call, re-checking `type(request) is SendMessageRequest` for the Tier-1 transcript-snapshot
  guard.
- `T10.md:1869`: a *third* per-kind branch (`type(request) is SendMessageRequest and len(records) == 1 ...`)
  for the auto-title task, also inside the shared `try:`.
- The shared exception ladder that follows (`ComposerConvergenceError`, `LiteLLMAuthError`, `LiteLLMAPIError`,
  `_BadRequestLLMError`, `ComposerPluginCrashError`, and — per the contract — more arms after that) is common to
  both kinds but is large and repetitive (the three LiteLLM/`_BadRequestLLMError` arms each independently
  build a progress event, conditionally persist `llm_calls`, and raise a near-identical `HTTPException`).

This is not a new complexity this plan invents from nothing — it is the literal, byte-preserving move of two
already-large route handlers (`messages.py`/`compose.py`) into one file, and the plan is explicit that
behaviour preservation, not refactoring, is the goal for this task. That is a defensible choice for a
risk-averse cutover. But from an architecture standpoint the result is a single ~800-line function with three
separate points of per-kind branching and a long duplicated exception ladder, which:

- makes `run_composer_turn` hard to reason about as "one coherent function" (the review lens this task asked
  about) — a reader has to track which branch they are in across 800 lines to know whether a given line applies
  to `compose_message`, `compose_recompose`, or both;
- concentrates all future freeform-turn maintenance (a new provider error class, a new per-kind precondition, a
  third operation kind) into one function that already mixes three concerns (kind dispatch, provider-error
  translation, and terminal-write orchestration);
- is not covered by any complexity or size gate in this plan's own gate tables (the plan is otherwise
  meticulous about pinning finding-set diffs, xfail counts, and structural AST checks for every other module it
  touches, but records no equivalent check — even an informational one — for this function's size or branch
  count).

None of the plan's evidence files (`findings/completeness-critique.md`, `findings/lifecycle-anatomy.md`,
`findings/recent-changes.md`) discuss this trade-off; I grepped all three for "run_composer_turn", "one
function", "god function", "coherent", "monolith" and found no hits, so this does not appear to have been
raised and consciously accepted elsewhere in the review chain.

**Recommendation:** before or shortly after landing T10, extract the exception-to-(progress-event,
HTTPException) translation arms (`ComposerConvergenceError`, `LiteLLMAuthError`, `LiteLLMAPIError`,
`_BadRequestLLMError`, `ComposerPluginCrashError`, and whatever remains in the un-excerpted tail) into small
named helpers that `run_composer_turn` calls, the way `_handle_convergence_error` and `_handle_plugin_crash`
already are (T10.md:1912, 2035) — those two prove the pattern is already in use for the largest arms, it just
was not applied to the LiteLLM arms. That alone would cut the function's visible length substantially without
changing behaviour, and would let a future reader see the per-kind preamble/exception-ladder/terminal-write
structure without wading through duplicated bodies.

### 3. [MINOR] `LegacySendMessageRequest` is real transitional debt, but is scoped and removed within the same plan

T10 (Step 1b) strictifies `SendMessageRequest` and, because the still-synchronous `POST /messages` route (and
~107 test call sites) are not migrated until T13, introduces `LegacySendMessageRequest` as a verbatim copy of
today's coercing body, bound only by the still-synchronous route (`T10.md:174-239`). T13 (Step 8b) deletes it
in the same task that deletes the synchronous route (`T13.md:1134-1151`), and greps `src tests` for the name to
confirm zero remain.

This is exactly the kind of "transitional dual-path" shape AGENTS.md's no-tech-debt doctrine warns about in
general, but it is intra-branch scaffolding with a committed removal step in the same plan, not a shipped
dual-acceptance surface — closer to how a multi-commit refactor temporarily keeps an old name alive between
"rename" and "delete the alias" commits. It is not a defect on its own. The residual risk is purely
sequencing: if the branch were merged or paused after T10 lands but before T13 runs (the plan's own "Stop
points" section notes merging to `release/0.8.1` is a separate decision made after the plan ends, and nothing
enforces that T13 must run before any such pause), `LegacySendMessageRequest` would ship as permanent dead
weight next to the real `SendMessageRequest`, and the two-DTO fork these tasks were built to avoid would exist
in the tree.

**Recommendation:** no code change needed; note the sequencing dependency (T13 must land before any interim
merge/release of this branch) so it is not silently forgotten if the plan's execution is interrupted between
T10 and T13.

### 4. [OK — verified sound] Three-writer authority split for `composer_async_operations`

The row's lifecycle is written by three named authorities under one `TablePolicy`
(`TablePolicy("composer_async_operations", "session", "ComposerAsyncOperationAuthority", (("SessionOperationAuthority", {"update"}), ("SessionComposerOperationTerminalAuthority", {"update"})))`,
contract.md:185-186, echoed in T03.md's Interfaces § Produces):

1. `ComposerAsyncOperationAuthority` (new, `coordination/composer_operation_authority.py`, T04) — admission,
   claim/renew/release, cancel-intent, unstarted/lost settlement.
2. `SessionOperationAuthority` / `_SessionOperationAuthorityRepository.start_composer_async_operation`
   (`coordination/repository.py`, T05) — the composite claim→`running` transition, sharing
   `_advance_exclusive_fence_on_connection` with the pre-existing `acquire()` path.
3. `SessionServiceImpl.complete_composer_async_operation` / `fail_composer_async_operation`
   (`sessions/service.py`, T06) — the terminal CAS, composed with the assistant-row insert, audit cohort, and
   pending-proposal read in one transaction.

This is not a novel pattern for this codebase: the `TablePolicy` shape already supports multiple named
authorities per table (the contract's own two-tuple form matches existing multi-authority tables), and the
split maps directly onto three genuinely different transactional contexts the row must join: transport-queue
mechanics (own module, no session-lease dependency), session-lease-fence advance (must share code with the
existing `acquire()` used by every other session-operation kind), and message/audit persistence (must be
atomic with `chat_messages`/audit writes that only `SessionServiceImpl` can reach). Splitting it into one
authority would have meant either duplicating the fence-advance logic outside `repository.py` or duplicating
message/audit-cohort insert logic outside `service.py` — both worse.

The design also pushes the invariant that ties the three writers together into the database itself
(`ck_composer_async_operations_status_bundle`, T03.md, is a single CHECK enumerating every legal shape for
`queued`/`running`/`completed`/`failed`, tested against every writer's actual insert/update shape in
`test_composer_async_operations_schema.py`), rather than relying on each authority's Python code being correct
in isolation. That is a real strength: a bug in any one of the three writers that produces an illegal
combination of columns is caught by the CHECK, not merely by a Python-level review. I looked for, and did not
find, a corresponding cross-authority integration test that exercises the *full* admit → claim → start →
complete/fail lifecycle end-to-end through all three authorities in one test (the schema test only proves each
individual shape is legal or illegal via direct `insert`/`update`, not that the three authorities in sequence
actually produce those shapes) — T05's and T06's own "Consumes" sections show they build real rows through
Task 4's authority for their tests, and T18 is named as the task that runs the full integration/crash-window
gates, so this coverage most likely exists at T16/T18, just not confirmed by the files I read in this pass. Not
a defect; noting it as an unconfirmed a gap check for whoever runs T18's gate.

### 5. [OK — verified sound] Import-direction and circular-import risk was actively designed around, not accidentally introduced

Two places show the plan authors catching and resolving real circular-import risk rather than leaving it
implicit:

- T02's `ComposerOperationError` home (contract's own "Adopted deviations" row, `contract.md:24`; restated as a
  `CONTRACT DEVIATION` in `T02.md:20-32`): `schemas.py` must import three `Literal` types from
  `composer_operations.py` at pydantic-model-definition time (runtime), so `composer_operations.py` can only
  import `schemas.py` under `TYPE_CHECKING`. The class therefore lives in `composer_operations.py` (subclassing
  `BaseModel` directly with `_StrictResponse`'s config restated, rather than subclassing `_StrictResponse`
  itself) and is re-bound, not re-defined, in `schemas.py`. This is checked by an identity test
  (`schemas.ComposerOperationError is ComposerOperationError`).
- T11's `CONTRACT DEVIATION 2` (`T11.md:3`): the contract originally placed `_ComposerOperationCancel` (the
  cancellation-reason marker) in the worker module. T11 measured that the worker imports `run_composer_turn`,
  and the turn's `except asyncio.CancelledError` arm needs to read the marker — so the marker cannot live in a
  module the turn itself would need to import from the worker (a cycle). It was moved to
  `sessions/composer_turn.py`, which both the turn and the worker can import without a cycle.

Both are evidence the import graph (`composer_turn` ← `composer_async_worker`, `schemas` ← `composer_operations`
at runtime / `composer_operations` ← `schemas` only under `TYPE_CHECKING`) was actually walked and fixed, not
assumed correct. I found no remaining case, in the tasks I read, where a lower layer (`coordination/`) imports
a higher layer (`sessions/routes/`) at runtime — `coordination/repository.py` importing
`elspeth.web.sessions.composer_operations` (T05) is consistent with an existing precedent already in that file
(the edit is inserted immediately next to an existing `from elspeth.web.sessions.converters import …`), i.e.
`sessions`-owned value types (not services) are already an accepted dependency of `coordination/`, so this is
not new coupling.

### 6. [Informational] `admit`'s rate-limit charge moved outside the atomic admission transaction (D-4a)

`ComposerAsyncOperationAuthority.admit` dropped the contract's `rate_limit: Callable[[], None]` parameter
because both real limiters (`ComposerRateLimiter.check`, `SharedRateLimiter.check`) are coroutines that commit
on their own connection, and calling either from inside the locked admission transaction on the bounded
`run_sync_in_worker` pool would block a pool thread while holding `BEGIN IMMEDIATE` plus the session lock (and,
for the shared limiter, nest a second pool submission). The fix is a new coroutine `admit_composer_operation`
that does get → charge-only-if-absent → `admit`. The plan honestly states the residual cost: "two CONCURRENT
first submits of one id can both be charged, and the loser is answered as a replay." This is a narrow,
self-documented race (it requires two genuinely concurrent requests bearing the *same* client-minted operation
id, which in the design is only ever reused by a single tab's own retry, not a cross-tab scenario — cross-tab
same-session collisions use different ids and are handled by D8's `composer_operation_active` 409). Sound
trade-off, correctly scoped and disclosed; not a finding.

## Confidence Assessment

**Overall Confidence:** Moderate-High.

| Finding | Confidence | Basis |
|---|---|---|
| #1 (archived-session leak, missing `settle_lost_inactive_session`) | High | Grepped T04's full method list (no such method) and read T11's actual `_reap_running` body verbatim; cross-checked against contract.md:21 and the plan's own Review Focus §5; confirmed `RECONCILE.md`'s location and gitignore status directly. |
| #2 (`run_composer_turn` size/coherence) | High | Read the function body directly from T10.md line 1741 through line ~2040 (of ~800 total lines to the next top-level def); the per-kind branches and duplicated exception arms are quoted verbatim above. |
| #3 (`LegacySendMessageRequest` transitional debt) | High | Directly traced its introduction (T10) and deletion (T13) with grep across all task files; no ambiguity in the text. |
| #4 (three-writer authority split) | Moderate | Verified the three authorities and their method signatures from contract.md + T03–T06 Interfaces sections; did NOT execute or directly read a full end-to-end integration test, so the "unconfirmed gap check" sub-claim is inference from task dependency structure, not direct evidence. |
| #5 (import direction) | High | Both cited deviations are explicit, measured `CONTRACT DEVIATION` blocks with stated reasons; I did not independently re-derive the import graph beyond what the task files assert, but their reasoning is internally consistent and specific (file:line citations). |
| #6 (rate-limit race) | High | Directly quoted from the `CONTRACT DEVIATION (D-4a)` block, which states the residual cost itself. |

## Risk Assessment

**Implementation Risk:** Medium-High (driven almost entirely by Finding #1).
**Reversibility:** Moderate — the leak (#1) is a runtime data/capacity issue that needs an operator remediation
step or a follow-up authority method to fix once discovered in production; it is not a one-way schema door
(no migration needed to add `settle_lost_inactive_session` later), but every day it ships unfixed leaks more
capacity that can only be recovered by finding and hard-deleting (or otherwise cascading) the affected sessions.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Archived-session running rows never settle, permanently consuming `composer_async_max_queued_operations` capacity, escalating to cluster-wide 429s under sustained archival activity | High | Likely, over time, in any deployment where sessions are archived while a turn may be in flight (an ordinary admin/user action) | Implement `settle_lost_inactive_session` (T04) and wire T11's `OWNER_INACTIVE` arm before this plan is considered complete, or explicitly accept the gap in the shipped plan documents (not only in an ignored lane file) with an operator runbook and an alert on `composer_operation.reap_skipped_inactive_session` |
| `run_composer_turn`'s size and per-kind branching increase the cost and risk of every future change to either freeform route | Medium | Certain to matter the next time either route needs a change | Extract the LiteLLM/plugin-crash exception arms into named helpers, mirroring the already-present `_handle_convergence_error` / `_handle_plugin_crash` pattern |
| `LegacySendMessageRequest` ships permanently if the branch is merged/paused between T10 and T13 | Low | Low, given the plan's own sequential task order, but not structurally prevented | Treat T13 as a hard prerequisite for any interim merge or release cut from this branch |
| The reconciliation record (`RECONCILE.md`) that resolves 11+ contract deviations and documents Finding #1 as a known-accepted gap is not part of the committed/reviewable plan tree | Medium | Certain once the lane directory is cleaned up (it is a normal `worktree-cleanup.sh` candidate) | Copy or merge its content into `docs/plans/2026-09-20-composer-async-operations/findings/` or `contract.md` before the lane is retired |

## Information Gaps

1. [ ] **T05/T06 full bodies and T16/T18 integration-gate content** — I read their Interfaces sections but not
   their full step-by-step bodies or the T16/T18 crash-window and cross-instance tests; a full-text read would
   let me confirm whether Finding #4's "unconfirmed gap check" (a true end-to-end three-authority lifecycle
   test) actually exists, and would let me check T16's PostgreSQL crash-window tests for anything that
   exercises the archived-session path from Finding #1.
2. [ ] **T07, T08, T09, T12, T14, T15, T17 full bodies** — not read in this pass (token-budgeted); T07 in
   particular (`_composer_request_lifecycle` extraction) is central to the "moved bodies stay coherent"
   question and deserves a dedicated pass; T14 (frontend cutover) was out of this lens's primary focus but
   touches the same authority/ownership boundary from the client side.
3. [ ] **Whether `RECONCILE.md`'s other 10 `CONTRACT DEVIATION`/`RECONCILED` resolutions (beyond the one I
   traced for Finding #1) are all actually reflected in the current task files** — I spot-checked several via
   grep (matches for T04/T10/T11's own inline deviation blocks) but did not cross-verify every row of
   `RECONCILE.md` § 2 against its owning task file line-by-line.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] Re-run the T04/T11 method-name search against the *executed* worktree (not just the task-file prose) once
  the plan is actually run, to confirm `settle_lost_inactive_session` is still absent (or has been added) at
  execution time — task-file prose and executed code can diverge if an executor makes an on-the-fly fix.
- [ ] Confirm with the plan owner whether the archived-session leak (Finding #1) is being knowingly accepted for
  this release or must be closed before merge; my finding is that the *shipped plan documents* do not make that
  decision visible, not that the decision was never made.

### Assumptions Made
- I treated `contract.md`'s "Adopted deviations" table as authoritative over the plan's later prose, per its own
  stated precedence rule, and treated the plan's "Review Focus" table as a binding acceptance-criteria list
  (the plan's own words: "The fix pass adds each line's test to the owning task"), not merely illustrative.
- I treated `.claude/lanes/async-ops-plan-2026-09-25/tasks/RECONCILE.md` as genuine session/lane state rather
  than a plan artifact, per AGENTS.md's definition of what belongs under `.claude/lanes/` vs. `docs/`; this is
  why its content, while informative, does not resolve Finding #1 as a documented-and-shipped decision.

### Limitations
- This review does not cover test coverage adequacy, symbol/API existence verification, security posture, or
  systemic/ripple effects outside the architecture lens — those are other reviewers' scope per the task brief.
- Given the plan's size (19 task files, several exceeding 1000 lines), this pass prioritized breadth across the
  lens's specific questions (module boundaries, the three-writer authority, function coherence, tech debt,
  composer invariants) over exhaustive line-by-line coverage of every task file; Information Gaps above name
  what was not read.
