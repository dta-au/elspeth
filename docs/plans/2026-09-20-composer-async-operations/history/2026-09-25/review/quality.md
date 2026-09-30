# Quality Review — Composer Async Operations Plan (QA lens)

Reviewer: quality (SME protocol). Scope: docs/plans/2026-09-20-composer-async-operations.md +
docs/plans/2026-09-20-composer-async-operations/{T00..T18,contract.md,findings/}, against
docs/specs/2026-09-16-composer-async-operations-design.md and the plan's own "Review Focus" list.
Repo read at release/0.8.1, tree state as given (uncommitted docs). All line numbers/greps below were
run against the actual files in this checkout; each finding cites what I read, not what I inferred.

## Test Strategy Assessment

**Test approach:** TDD-shaped (task files consistently write/red the test before landing production code;
T16 additionally does mutation testing on its own fail-closed control). Overall this is an unusually
rigorous plan for this project's normal bar. T16 in particular (spec gate 3) is exemplary: real `SIGKILL`
across spawned OS processes, real PostgreSQL, a provider-call ledger shared across processes so "no
replay" is a line count rather than a mock assertion, explicit instrument self-checks (grep positive/negative
controls for `getattr`/`hasattr` and `MagicMock`), and a genuine mutation test (Step 5 flips
`_expired_owner_allows_takeover`'s predicate to `return True`, proves the fail-closed control goes red,
then proves the revert is byte-identical and green again). That gives me high confidence gate 1–4 and 6
are solidly built where they are drafted. My findings below are about the gaps: places where a gate-3/4/5
requirement or a named Review Focus item has no test that would actually fail if the property broke —
either because no test was written, or because the test asserts something weaker than the stated
requirement, or (in two cases) because the underlying production fix itself was never drafted.

| Gate / Review Focus item | Test exists? | Would it fail if the property broke? |
|---|---|---|
| Gate 1 (schema/transaction, T03–T06) | Yes | Yes — not reviewed in depth here (in scope for the Reality/Architecture lenses); T04's race tests use real `ThreadPoolExecutor`/PG concurrency, not mocks |
| Gate 2 (202 + poll, T13+T18) | Yes | Yes, per T13's read |
| Gate 3 (crash windows, T16) | Yes | Yes — see praise above; only gap is R1 (below), explicitly disclaimed by T16 itself |
| Gate 4 (cancellation/audit, T11) | Yes | Yes for the state-machine outcomes; **no** for the log-event contents (see Observability) |
| Gate 5 (frontend, T14) | Partial | **No** for Review Focus item 1 (clock skew); weak for item 2 (vanished session) |
| Gate 6 (integration, T18) | Yes | Not deep-reviewed; inherits the T04/T11/T14 gaps below |
| Review Focus 1 — clock skew | **No test anywhere** | N/A — not written |
| Review Focus 2 — vanished session | Weak/indirect | Unclear — see finding |
| Review Focus 3 — storage refusal | Yes (proxy) | Plausible |
| Review Focus 4 — two-tab race | Yes but deterministic, not a true race | Would catch the sequential case only |
| Review Focus 5 — archived session zombie row | **No test possible** — production fix not drafted | N/A — nothing to test |

**Gaps:** listed as findings below, most severe first.

## Findings

### Finding 1 (BLOCKER): Review Focus item 5 has no test because the fix it requires was never drafted

The plan's own index (`docs/plans/2026-09-20-composer-async-operations.md:55`) states:

> 5. **An archived session's running job after its owner dies.** Expectation: the reaper settles it through
> `settle_lost_inactive_session`. It never stays `running` and never counts against
> `composer_async_max_queued_operations` indefinitely. Owner: T04 + T11.

`contract.md:21` ("Adopted deviations" table, which the file says overrides every later section) presents
this as already adopted and owned:

> archived-session `running` row | not specified | `settle_lost_inactive_session(*, session_id, operation_id,
> failure, cancelled_failure)` (T04), an unfenced settle guarded inside the locked transaction by
> `sessions.archived_at IS NOT NULL`; the reaper's `OWNER_INACTIVE` arm calls it (T11)

I grepped `docs/plans/2026-09-20-composer-async-operations/T04.md` for `archiv` and for
`settle_lost_inactive_session`: **zero matches**. The method the contract assigns to T04 is not in T04's
drafted deliverable at all.

T11's own review notes admit this directly (`T11.md:2044`):

> **Zombie running row in an archived session.** The reaper skips `FenceLossReason.OWNER_INACTIVE` forever,
> so the row stays `running` and counts in `count_nonterminal` (D3 capacity). It now logs
> `composer_operation.reap_skipped_inactive_session` on every sweep so the leak is visible. **The fix is
> unresolved and recorded as a residual in RECONCILE.md.**

I read `.claude/lanes/async-ops-plan-2026-09-25/tasks/RECONCILE.md:163-170` (R1): it confirms this is a
known, explicitly *unfixed* residual — "Not fixed: it needs a new authority write path (design, not
reconciliation)" — with `settle_lost_inactive_session` recorded only as a **proposal**, not something any
task file actually implements. `contract.md`'s Adopted-deviations table nonetheless states the proposal as
if it were the landed design, which will mislead an executor into believing this review-focus item has
coverage when it does not.

**Why this is a QA-lens blocker, not just a docs nit:** there is no way to write a test that proves "the
archived session's running job settles and stops charging capacity," because the production write path to
settle it doesn't exist in the drafted plan. T16's own crash-window suite explicitly disclaims this scenario
(`T16.md:1459`, "Not covered here (owned elsewhere): archive during a job (D7, Task 13 and Task 4 tests)")
and points at tests that, per the above, were never drafted. The only artifact produced for this scenario is
a warning log line (`composer_operation.reap_skipped_inactive_session`) — and per Finding 4 below, *that
log line itself has no test proving it fires*. So the one Review Focus item the plan calls out as most
likely to leak `composer_async_max_queued_operations` capacity indefinitely in production ships with
neither a fix nor a tested alarm.

**Owning task:** T04 (add `settle_lost_inactive_session` per contract) + T11 (wire the `OWNER_INACTIVE` reap
arm to call it, and add the reap-until-settled test T16 already has the harness for — `T16.md:874-883`
`_reap_until_settled` could be reused directly by a T16-style archived-session test once the T04/T11 fix
lands). Until then, this is a design hole, not a test hole — flag for the architecture lens too.

### Finding 2 (BLOCKER): Review Focus item 1 (client clock skew) is not merely untested — the drafted design contradicts its stated requirement

The plan's index (`docs/plans/2026-09-20-composer-async-operations.md:51`) states the required behavior in
unambiguous terms:

> 1. **Client clock skew against `deadline_at`.** A browser whose clock is minutes off must neither cancel
> a healthy turn early nor wait forever. Expectation: the client deadline is measured from a
> **server-relative remaining duration**, never by comparing the server's absolute `deadline_at` to
> `Date.now()`. Owner: T12 (poll body) + T14.

T14's actual drafted implementation does exactly the forbidden comparison. The CONTRACT DEVIATION block at
`T14.md:13-20` states the mechanism plainly:

> The deadline is armed inside the store's poll loop on every non-terminal poll body
> (`Date.now() >= Date.parse(deadline_at) + COMPOSE_CLIENT_GRACE_MS` → `cancelComposerOperation` once, keep
> polling).

and the drafted production code at `T14.md:3825-3830` implements it verbatim:

```ts
if (
  attachment.cancelReason === null &&
  Date.now() >= Date.parse(answer.deadline_at) + COMPOSE_CLIENT_GRACE_MS
) {
  requestComposerOperationCancel(attachment, "deadline");
}
```

I grepped every task file (`*.md` in the plan directory) for the word "skew": **zero occurrences anywhere
in the 19 task files**. There is no test that mocks a skewed `Date.now()` (e.g., client clock 10 minutes
ahead of the server) and asserts the healthy turn is *not* cancelled early, nor a test that mocks the client
clock behind and asserts the client does not "wait forever." The only client-deadline tests I found
(`T14.md:2772`, `4432-4437`) exercise the happy-path elapsed-time arithmetic (`vi.advanceTimersByTimeAsync`)
and a mutation control on the `>=`/`+60_000` predicate — both of which pass or fail identically whether or
not the client's wall clock is skewed from the server's, because they never desynchronize `Date.now()` from
the server-issued timestamp in the first place.

Concretely, with a client clock 10 minutes fast, `Date.now() >= Date.parse(deadline_at) + GRACE` is true
immediately on the first poll after `deadline_at` is set, even though the turn has just started — this is
exactly the "cancel a healthy turn early" failure mode the review-focus item was written to prevent, and
the drafted design reproduces it rather than avoiding it.

**Owning task:** T14 (client deadline arithmetic must use a server-relative remaining-duration measurement,
e.g. captured at first-poll time via `performance.now()` deltas or an explicit `remaining_seconds` field
from T12's poll body) + T12 (poll body may need to add that field, since `deadline_at` alone cannot carry a
skew-tolerant remaining duration for a client whose clock is untrustworthy). Needs a new test in T14's
`sessionStore.composerOperations.test.ts` that mocks `Date.now()` skewed in both directions and asserts (a)
no cancel fires before the true remaining time elapses, and (b) a cancel does eventually fire and does not
"wait forever" when the server never settles.

### Finding 3 (MAJOR): Review Focus item 2 (vanished session) — the two 404 reasons the backend deliberately distinguishes are collapsed by the frontend, and no test exercises the specific scenario

Contract `§HTTP` (`contract.md:363-366`) specifies the poll route returns **two different 404 shapes**
depending on cause: `404 "Session not found"` shape when ownership fails (session archived, foreign, or
unknown) vs. `{"detail":"Operation not found"}` when the session is owned but the job row is missing. This
distinction exists specifically so the client can tell "your session is gone" apart from "this one poll
target vanished."

T14's client functions discard that distinction. `fetchComposerOperation` and `cancelComposerOperation`
(`T14.md:1590-1628`) both do:

```ts
if (response.status === 404) {
  return null;
}
```

with no inspection of the body, so both 404 reasons become an undifferentiated `null`. The store's poll
loop (`T14.md:3842-3846`) then always maps a `null` result to `{ kind: "missing" }`, and
`settleComposerOperation` (`T14.md:4136-4150`) always renders a generic
`COMPOSER_OPERATION_MISSING_MESSAGE` ("operation missing") local error plus a best-effort resync
(`resyncAfterSettledComposeTurn`) — never the "today's session not found" rendering path the review-focus
item calls for verbatim: *"the poll's 404 stops polling, clears the descriptor, and renders today's
'session not found' path."*

I found no test in T14 with "archived" in its name or scenario (grep of `it("..."` for
archiv/vanish/session-not-found in T14.md returns nothing beyond the generic client-level 404→null unit
tests at `T14.md:437,460,474`, which only prove the client function maps status 404 to `null` — they don't
exercise the store's downstream rendering, and they don't distinguish which of the two 404 causes
triggered it). It's plausible the pre-existing `resyncAfterSettledComposeTurn` refetch incidentally
surfaces a genuine "session not found" UI once its own GET calls 404 — but that is an unverified,
accidental path, not a tested one, and the *first* thing the user sees is the generic "operation missing,
reload" message regardless of whether the cause was "the operation vanished" or "your session was archived
in another tab."

**Owning task:** T14. Needs a test that specifically simulates the archived-session case: mock
`fetchComposerOperation`/the resync's message-fetch to answer with the "Session not found" shape, and
assert the poll loop takes the session-not-found path (stops polling, clears
`composerOperationCustody`, no resubmission) rather than (or in addition to) the generic
"operation missing" message.

### Finding 4 (MAJOR): Zero tests assert that the worker's failure-mode structured logs actually fire

T11's production code emits nine distinct structured `slog` events for worker/reaper failure modes
(`T11.md:955-1693`): `composer_operation.job_failed`, `.watcher_failed`, `.watcher_poll_retrying`,
`.watcher_poll_degraded`, `.watcher_poll_failed`, `.claim_renewal_failed`, `.start_fence_lost`,
`.adopt_fence_lost`, `.terminal_write_fenced`, `.lease_close_failed_after_settle`, `.lease_close_failed`,
`.reaped_running`, `.reap_skipped_inactive_session`. This is the operator's primary diagnostic signal for
exactly the crash windows this plan is built around.

I grepped `T04.md`, `T06.md`, `T11.md`, and `T16.md` for `caplog|capture_logs|log_output`: **no matches in
any of them**. By contrast, T09 (error projection) does this correctly —
`test_each_generic_projection_mints_a_fresh_diagnostic_id_and_logs_it_without_the_message`
(`T09.md:692-704`) uses `structlog.testing.capture_logs()` and asserts the exact event name, `exc_class`,
`failure_code`, `request_id`, and that secrets never leak into the log — a good pattern that T11's worker
tests do not follow for any of its 13 events, including T16's real-process crash tests, which exercise
these exact code paths (a worker dying mid-claim, mid-run, under partition) without ever checking that the
corresponding log line was emitted with the right fields.

This matters most for `composer_operation.reap_skipped_inactive_session` (Finding 1): it is the *only*
mitigation shipped for a permanent capacity leak, and per this finding, no test proves it fires at all,
let alone on every sweep as claimed (`T11.md:2044`, "now logs ... on every sweep so the leak is visible").
An untested "visibility" mitigation for an admitted unfixed leak is not actually visibility.

**Owning task:** T11 (add `capture_logs()` assertions to its worker/reaper unit tests for at least
`job_failed`, `watcher_failed`, `terminal_write_fenced`, and `reap_skipped_inactive_session`, following
T09's pattern) and, once Finding 1 is fixed, a test proving `reap_skipped_inactive_session` fires on every
sweep it claims to.

### Finding 5 (MINOR): Review Focus item 4 (two tabs, different ids) is proven only for the deterministic ordering, not a genuine race

Review focus item 4 (`docs/plans/2026-09-20-composer-async-operations.md:54`): *"Two tabs press Send at
once with different ids. Expectation: exactly one job is admitted; the other tab gets 409
`composer_operation_active`..."* — Owner: T13 (server race) + T14.

T13's `test_send_message_serializes_concurrent_requests_per_session` (`T13.md:1546-1592`) is the drafted
proof, but it is sequenced with an explicit `composer.first_call_started` event: the second POST is only
issued *after* the first job is already confirmed `running`, not truly concurrently. This proves the
"second send while the first is active" business rule, but not that the "active nonterminal row" admission
check is race-free under real simultaneous contention.

T04's genuinely concurrent tests (`test_concurrent_same_id_admits_from_two_authorities_reserve_exactly_one_row`,
`test_postgres_concurrent_same_id_admits_reserve_exactly_one_row`, both using real
`ThreadPoolExecutor`/multi-connection contention) only cover the *same*-id replay race (D2), not two
*different* ids racing for the one-`running`-per-session slot that Review Focus item 4 is actually about
(`test_postgres_active_and_capacity_refusals` at `T04.md:1185-1194` is sequential: admit, then admit again
and expect refusal).

This is lower severity because admission runs inside `locked_session_transaction` (contract §Authority),
so the session-level lock should serialize two concurrent different-id admits by construction — but no
drafted test exercises that specific claim with real concurrency (two threads/processes racing `admit()`
with different ids on the same session and asserting exactly one lands `queued`/`running` and the other
raises `ComposerOperationActiveError`).

**Owning task:** T04 (add a `test_concurrent_different_id_admits_races_for_the_active_slot`-style test
alongside its existing same-id concurrent tests, reusing the same `ThreadPoolExecutor` harness).

### Finding 6 (MINOR, positive note — not a defect, included for calibration)

T16 (gate 3) deserves explicit credit against this lens's own rubric ("is there a test that would actually
FAIL if the property broke"): it uses real `SIGKILL` on spawned OS processes against real PostgreSQL, a
provider-call ledger written and fsynced to disk so "no replay" is verified as a literal line count rather
than an assertion on a mock's call history, and — critically — a genuine mutation test in Step 5
(`T16.md:1290-1329`) that flips `_expired_owner_allows_takeover`'s guard to `return True`, proves the
fail-closed control test goes red for the right assertion, then proves the file is byte-identical after
revert. This is exactly the instrument-validation discipline AGENTS.md's "Claims Must Be Measured" section
asks for, and it is the strongest evidence in this plan that the gate-3 tests are not vacuous. I found no
similar mutation-testing step for gate 4 (T11) or the frontend gate 5 (T14) tests, beyond the one deadline-
predicate mutation control at `T14.md:4432-4437` (which only proves the `>=`/`+60_000` mutation is caught,
not the skew defect in Finding 2).

## Security Scan

No `eval`/`exec`, no raw SQL string interpolation, no `shell=True`, no `dangerouslySetInnerHTML`, no
hardcoded secrets encountered in the sections read. T09's secret-scrubbing test
(`T09.md:692-704`, asserting a live API-key-shaped string never appears in the public body or the log
record) is a good, specific control for exactly the failure mode ("generic 500 leaks provider secret") this
kind of error-projection code is prone to.

## Production Readiness

| Element | Status | Notes |
|---|---|---|
| Error handling | Present | D13's closed failure-code set + D14's exact bodies are well specified and tested (T09) |
| Logging | Present in production code, **untested** | See Finding 4 |
| Configuration | Configurable | New `composer_async_*` settings, bounded (`ge`/`le`) in contract §Settings |
| Graceful degradation | Partial | Transient poll errors back off and never declare failure (good); the archived-session leak (Finding 1) degrades capacity silently over time |
| Rollback plan | Not assessed here | Outside QA lens; T17 (epoch bump) and T18 (deploy note) own operational rollback, not reviewed in depth |

## Summary

- **Test gaps:** 5 (Findings 1–5)
- **Observability gaps:** 1 substantial (Finding 4, spanning 13 named log events across T11)
- **Edge cases missing:** clock-skewed client (Finding 2), archived-session-in-another-tab (Finding 3),
  genuine two-different-id admission race (Finding 5)
- **Security issues:** 0 found in the sections read

## Blocking Issues

1. **Finding 1** — Review Focus item 5 (archived-session zombie row) has no implementation to test against;
   `contract.md`'s Adopted-deviations table misrepresents an explicitly unfixed RECONCILE.md residual (R1)
   as landed. This is a design hole surfaced through a QA-lens test-coverage gap; architecture/reality
   lenses should also flag it.
2. **Finding 2** — Review Focus item 1 (client clock skew): the drafted T14 design implements the exact
   anti-pattern the plan's own review-focus item says to avoid (`Date.now()` vs. absolute `deadline_at`),
   and no test in any of the 19 task files even contains the word "skew."

## Warnings

3. **Finding 3** — vanished-session 404 handling collapses two backend-distinguished causes into one
   generic client message; no test proves the archived-session path renders "session not found."
4. **Finding 4** — 13 worker/reaper failure-mode structured log events have zero test coverage
   (`caplog`/`capture_logs` absent from T04, T06, T11, T16), unlike T09's rigorous pattern for the same
   concern.
5. **Finding 5** — the two-different-ids admission race (Review Focus item 4) is proven only for
   deterministic ordering, not genuine concurrency; the concurrent-admit tests that do exist (T04) cover
   only the same-id replay case.

## Confidence Assessment

**Overall Confidence:** High for Findings 1, 2, 4, 5 (each grounded in a direct grep/read against the
actual task-file text, with exact line citations, and in Finding 1's case cross-checked against a second
source file, RECONCILE.md, that independently confirms the gap). Moderate for Finding 3 (the collapse of
the two 404 shapes is directly verified in code; whether the downstream resync accidentally produces
correct UX regardless is genuinely unverifiable without running the actual frontend, which is out of scope
for a plan review).

| Finding | Confidence | Basis |
|---|---|---|
| 1 — archived-session fix not drafted | High | Zero occurrences of `settle_lost_inactive_session`/`archiv` in T04.md (grep); T11.md:2044 self-admits "unresolved... residual"; RECONCILE.md:163-170 confirms "Not fixed" |
| 2 — clock-skew design contradicts its own requirement | High | Direct quote-match: plan index's stated expectation vs. T14.md:13-20 CONTRACT DEVIATION and T14.md:3825-3830 code; zero "skew" hits across all 19 task files (grep) |
| 3 — vanished-session 404s collapsed | Moderate | Verified in code (T14.md:1590-1628, 3842-3846, 4136-4150) that both 404 causes map to one message; not verified whether a downstream resync incidentally recovers correct UX |
| 4 — worker log events untested | High | Grep of `caplog\|capture_logs\|log_output` across T04/T06/T11/T16 returns nothing; T09's contrasting pattern is directly quoted for comparison |
| 5 — two-tab race proven only sequentially | Moderate | T13's test is read in full and is provably sequenced (waits on an event before the second POST); inferred (not directly disproven) that the session lock makes this safe in practice |
| T16 gate-3 quality (Finding 6) | High | Full file read; mutation-testing step read line-by-line |

## Risk Assessment

**Implementation Risk:** High — two of the five findings (1 and 2) are blocking because the underlying
production behavior does not meet the plan's own stated requirement, not merely a missing test.
**Reversibility:** Moderate — both blocking findings are additive fixes (a new settle path; a new client
deadline calculation) rather than requiring a redesign of already-landed work, but Finding 1 needs a new
authority write path per RECONCILE.md's own assessment ("needs a new authority write path (design, not
reconciliation)").

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| `composer_async_max_queued_operations` capacity silently exhausted by archived-session zombie rows in production | Critical | Certain over time in any deployment with session archival + occasional worker death | Land T04's `settle_lost_inactive_session` and T11's `OWNER_INACTIVE` call to it before merge; do not ship on the log-only mitigation |
| A user on a skewed clock has a healthy long-running turn cancelled seconds after it starts | High | Likely for any user behind a captive portal, VM, or with a wrong system clock (exactly the population this whole plan's middlebox-timeout problem already targets) | Redesign T14's client deadline to a server-relative remaining-duration measurement before merge |
| An archived-session-in-another-tab reload shows a confusing "operation missing, reload" message instead of the existing "session not found" UX | Medium | Possible (a real, if narrow, UX regression for a documented review-focus scenario) | Add the distinguishing test in T14; decide whether to preserve the 404-shape distinction through `fetchComposerOperation` |
| An operator cannot diagnose a production worker-death incident because the log line they'd grep for was never proven to fire | Medium | Possible — only surfaces when someone actually needs the log during an incident | Add `capture_logs()` assertions per Finding 4 |
| Two tabs sending different ids at the true same instant produce two `running` rows or an unhandled exception | Low | Unlikely given the locking primitive, but unproven | Add the genuine-concurrency test per Finding 5 |

## Information Gaps

1. [ ] **Whether T12's poll body could add a `remaining_seconds`/skew-tolerant field** — I did not check T12
   in full for whether such a field already exists or is trivial to add; this would directly inform the
   smallest fix for Finding 2.
2. [ ] **Whether the pre-existing (pre-plan) `resyncAfterSettledComposeTurn` and its downstream message/state
   refetch already render a correct "session not found" UI when their own GET 404s** — if so, Finding 3 may
   be lower severity (a UX ordering issue, message-then-correct-render) rather than a missing path
   entirely. I did not have budget to read the pre-existing (non-plan) `sessionStore.ts` resync code this
   task reuses.
3. [ ] **T18's actual gate-6 acceptance test content** — I read T18's role from the index and contract's
   task table but did not do a line-by-line read of T18.md itself; it's possible some of these gaps are
   caught at the integration layer even if the per-task tests don't isolate them. Given T16's explicit
   disclaimer (item 8, "archive during a job... owned elsewhere" pointing at tests that don't exist) and
   T11's explicit "unresolved" note, I judge this unlikely for Finding 1, but flag it as unverified for
   Findings 3 and 5.
4. [ ] **Live confirmation that these findings reproduce at runtime** — this is a plan review; nothing in
   this repository has been executed. All findings are text/design-level (an unimplemented method, a
   design that matches its own documented anti-pattern, an absent test file), which is the appropriate
   level for a plan review, but the executor should still confirm at Task 4/11/14 execution time.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] Confirm with the plan owner whether Finding 1 (archived-session settle) was deliberately deferred as
  an accepted residual (RECONCILE.md frames it that way) rather than an oversight — if deliberately
  deferred, the finding should be re-labeled from "blocking" to "known accepted risk, tracked," but the
  Adopted-deviations table in contract.md should then be corrected so it doesn't misstate the proposal as
  landed.
- [ ] Confirm whether "T12 (poll body)" in Review Focus item 1's ownership line means T12 was expected to
  add a skew-tolerant field to the wire contract that T14 simply never consumed — I did not do a full T12
  read to rule this in or out (see Information Gap 1).

### Assumptions Made
- The task files as currently drafted in `docs/plans/2026-09-20-composer-async-operations/` are the ones
  that will be executed verbatim (i.e., a plan-assembly pass has not already silently patched these gaps
  in a version I didn't see). RECONCILE.md is dated as part of the same 2026-09-25 planning pass and is
  consistent with what I read in the task files, so I treat it as authoritative context, not a stale draft.
- "Review Focus" items in the plan index are binding requirements ("The fix pass adds each line's test to
  the owning task," `docs/plans/2026-09-20-composer-async-operations.md:49`), not aspirational notes.

### Limitations
- This analysis does NOT cover symbol existence or whether the referenced production names
  (`_expired_owner_allows_takeover`, `locked_session_transaction`, etc.) actually exist at the cited
  locations in the current tree — that is the Reality reviewer's lens.
- This analysis does NOT cover architectural soundness of the composite-transaction design, the SOL/claim
  fencing model, or whether T04/T05/T06's transaction boundaries are correct — that is the Architecture
  reviewer's lens. My read of those tasks was purely to locate/confirm the presence or absence of tests and
  log assertions, not to assess their design.
- I did not execute any code, run any test, or verify against a live database; all findings are grounded in
  the plan's own text as written, cross-checked internally (contract vs. task file vs. RECONCILE.md) and
  against the spec's stated requirements.
