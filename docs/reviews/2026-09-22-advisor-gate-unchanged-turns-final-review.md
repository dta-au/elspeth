# Final review — `fix/advisor-gate-unchanged-turns` (plan 1 of the advisor-block fix)

> **Disposition (executor, after the review).** Reviewed range `0bd0c5e40..b77d64fcf`.
> Fix pass landed as `90d3c04f0` (I2/I3: notice copy chosen per *family* — a
> persisted block says "after your next pipeline change", the unchanged-turn-only
> ABSENT/UNVERIFIED family keeps "on your next message"; I4: route seam tests
> over both envelope writers), `a43c90a41` (M4 hints line) and `6ae6d2ad7`
> (durable `unavailable`/`malformed` suggestions no longer offer a retry the
> END gate would skip). I1 is **not fixed** — a block that first arises on an
> unmutated turn persists no fact because no state row is written, so the skip
> cannot cover it; it is recorded as a known limit in the plan's Non-goals and
> awaits the maintainer's ruling. M1/M2/M3/M5/M6/M7 deferred.
> Full-suite gate on `6ae6d2ad7` (`frozen=yes`): ruff/mypy/contracts 0;
> lints `findings=2274` (unchanged across the branch, position-stripped diff vs
> base empty); pytest 2 failed / 55642 passed — both reds are the pre-existing
> `test_freeform_planner_failure_translation` failures reproduced on the
> untouched base.

# Code review — `fix/advisor-gate-unchanged-turns` (0bd0c5e40..b77d64fcf)

Reviewer: senior code reviewer (read-only). Reviewed the four commits, the plan
(`docs/plans/2026-09-22-composer-advisor-gate-skips-unchanged-turns.md`), the
diagnosis (`docs/reviews/2026-09-22-composer-advisor-block-diagnosis.md`) and the
executor's ledger. No suite was run (a full-suite gate is live on this tree);
every claim below is a code reading with the file:line that produced it.

Verdict counts: **0 Critical, 4 Important, 7 Minor.**

> **Post-review note (tree state).** `git status --short` was empty when this
> review began and is not now: `tests/unit/web/composer/test_no_tool_policy_segments.py`
> and `tests/unit/web/sessions/test_routes.py` carry uncommitted edits. HEAD is
> still `b77d64fcf`, so everything below stands as a review of the four commits.
> Two operational consequences the executor must act on:
> (1) the edits appear to address I2/I3 (a per-shape notice split into
> `_NOTICES_FOR_A_PERSISTED_BLOCK` / `_NOTICES_FOR_AN_UNCHANGED_TURN_BLOCK`) and
> I4 (a route-level pair asserting the composer receives the seeded fact, and
> `None` when there is none) — I have **not** reviewed them, and the fact that
> they exist is not evidence that they are complete or correct;
> (2) **the tree is no longer frozen.** Any full-suite gate spanning this change
> is `frozen=NO` and is not evidence (AGENTS.md § canonical scripts). Re-run it
> on the committed tree.

---

## Strengths

- **The predicate is the right shape.** `advisor_block_covers_unchanged_graph`
  (`src/elspeth/web/execution/completion_gates.py:317-336`) is pure, takes no
  message text, and every "unknown" answers `False`. `completion_gates=None`
  defaults on all six signatures mean an omitting caller fails toward reviewing.
  That is the correct failure direction and it is asserted, not asserted-about:
  `tests/unit/web/execution/test_completion_gates.py:546-572` pins both negative
  controls (version moved; `for_graph` mismatch) alongside the positive.

- **Composer invariant 1 holds, and I checked the non-obvious half.** The skip
  reads `state.version` and the persisted fact only. It also cannot be used to
  bypass R2-F13 withholding: for `advisor_repair_context_introduced` to be True
  on a skipped turn, either the END gate injected earlier in the same turn
  (impossible — the first gate call on an unchanged turn skips, so no injection
  ever happens) or the EARLY checkpoint injected, and
  `_maybe_run_early_checkpoint` (`service.py:8997-9000`) fires only on the
  empty→non-empty transition, which necessarily moved the version. So a skipped
  turn never publishes prose written after hidden findings entered context.

- **Invariant 2 holds.** No tutorial or guided branch anywhere in the diff.

- **Tier-1 propagation is real, not claimed.** I verified there is no enclosing
  `try:` at or above the parse in either route: the nearest `try` before
  `messages.py:216` is at `:183` and is closed at `:189`, and no `try` exists at
  indentation ≤16 in `send_message` before that line. `compose.py:135` sits
  directly after the `state_record` load inside the `async with` only. A corrupt
  envelope 500s, as Review Focus 5 requires.

- **Threading is complete, and completeness is measurable.** `src` has exactly
  two `.compose(` call sites (`messages.py:358`, `compose.py:202`); both pass the
  keyword. Every test double with an explicit signature was updated — I
  enumerated them with `grep -rn "async def compose(" tests/ src/`: the five
  changed (`test_routes.py:461,504,11405`, `tests/helpers/composer_lease.py:72`,
  `tests/testcontainer/web/test_composer_splice_concurrency.py:158`) are the
  complete set; the remainder take `*args, **kwargs`. Catching the testcontainer
  double — which the default pytest selection never runs — was good discipline.

- **Both reviewed ledger rulings are correct, and I confirm rather than refute
  them.**
  - *The unchanged-turn arm never reads `result.runtime_preflight`.* Confirmed.
    `messages.py:845` gates the only reader; on the else path `state_response`
    stays `None` (`:803`) and the response body (`:1025-1029`) carries only the
    assistant message. `compose.py:591` has the same shape. The P2 merge is
    invariant-preserving, not load-bearing for the HTTP response.
  - *The P5 finalize sites do not need the fold.* Confirmed, and for a stronger
    reason than the ledger gives: the B-4D-3 last-chance finalize
    (`service.py:6001`) is lexically inside `if turn_has_mutation:`
    (`service.py:5886`), so `state.version > initial_version` always holds there
    and the predicate is False by construction — the fold would be dead code,
    not merely unread.

- **The copy test carries a positive control** and I re-ran the instrument: the
  phrase survives only in the test's own assertion
  (`test_no_tool_policy_segments.py:869`), and the control phrase "Composer
  completion is withheld" still matches (`no_tool_policy.py:142`). The
  instrument works.

- **Comments cite the ruling, the live session and the ticket** at each new
  site, and the gate comment explicitly contrasts itself with the proof gate
  whose version guard was *removed* — exactly the "why is this one different"
  a future reader needs.

- **"Can a fact for the current fingerprint exist while the graph is not
  actually blocked?" — No, by construction.** (A brief question worth answering
  in the affirmative.) The writer emits `advisor_signoff` only when the turn's
  preflight carries an `ADVISOR_SIGNOFF_BLOCKED_CODE` blocker
  (`completion_gates.py:191`), `status` is always `_GATE_STATUS_BLOCKED` and the
  parser rejects any other value (`:267`), and that same fact is what
  `/validate` and Run enforce via `merge_completion_gates`
  (`execution/service.py:1544`). So fact ⇔ blocked, and the skip can never let a
  block be *cleared* — it suppresses a new verdict, saves no row, and leaves the
  durable withholding exactly where it was. The only sense in which the fact
  could be stale is that a fresh LLM review might now clear it; that is I3's
  outage case, not a soundness hole in the predicate.

---

## Issues

### Critical

None.

### Important

#### I1. The skip cannot fire for a block that first arises on a turn that changed nothing

`src/elspeth/web/sessions/routes/messages.py:845` /
`src/elspeth/web/sessions/routes/composer/compose.py:591` /
`src/elspeth/web/sessions/routes/_helpers.py:2772`

A `completion_gates` envelope is written only by
`_state_data_from_composer_state`, which the route calls only inside
`elif result.state.version != state.version:`. A terminal advisor block on a
turn that mutated nothing therefore **persists no fact at all** — the row is
never written.

Consequence: the fix covers a block whose turn also edited the pipeline, and
only that. The reported incident qualifies (request `fc112a2f` mutated, so
`6ebd025d`'s question would now be answered — the defect as filed is fixed). But
the same trap stays wide open one step earlier in the sequence:

> User asks a question. The model calls `preview_pipeline` (a discovery tool —
> no version bump). `_reuse_or_recompute_runtime_preflight` (`service.py:3692`)
> returns the reused green preflight. The END gate runs, the advisor reads the
> user's message (`_build_checkpoint_arguments`), FLAGS, repair-continues,
> FLAGS again, terminal-blocks and deletes the reply. Version unchanged → no
> row → no fact. The next question re-enters the identical loop, forever.

This is not a hypothetical corner: the plan's own Review Focus 3 ("a question
turn where the model ran `preview_pipeline`") already treats that turn shape as
realistic. RF3 presupposes the fact exists and asks what the skip must then do;
I1 is what happens when that same turn is the *first* one blocked, before any
fact exists. The operator ruling is "a turn that changes no pipeline state does not
need the advisor gate"; what shipped is "…provided some earlier turn both
blocked *and* mutated". The plan's Non-goals cite `messages.py:838` only in the
*clearing* direction ("a re-review that can clear a block without a pipeline
change"); the symmetric consequence for *recording* the block is undisclosed.

Why it matters: a user in this state sees their answer deleted on every message
and has no way out that the UI names. It is the exact user harm the ticket was
opened for.

How to fix (bigger than this branch — surface to John rather than patch here):
either (a) persist a gate-fact-only state row when the END gate terminal-blocks
with an unchanged version, or (b) key the predicate off the last
`AdvisorTerminalPublication` audit row plus the fingerprint instead of the state
row. Minimum for this branch: say so plainly in the hand-back and in the plan's
Non-goals, so the limitation is a recorded decision rather than a surprise.

#### I2. The new copy is misleading for exactly the notice shapes that can only be emitted on an unmutated turn

`src/elspeth/web/composer/no_tool_policy.py:159-163, 184, 260-263`

`_reuse_or_recompute_runtime_preflight` (`service.py:3680-3718`) returns `None`
only when `state.version <= initial_version` and nothing else forced a
recompute. So the ABSENT-preflight notice family —
`_ADVISOR_SIGNOFF_UNVERIFIED_*` (`:159`),
`_ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_*` (`:260`), and the ABSENT uses of
`_ADVISOR_SIGNOFF_UNRENDERED_NEXT_STEP` (`:184`, via `:202-225`) — is emitted
**only on turns that changed nothing**. Per I1 those blocks persist no fact, so
the skip never applies to them and the advisory review *does* run again on the
user's next message.

These three strings now tell that user "validation and the advisory review run
again after your next pipeline change". Strictly the clause is *true* — a
pipeline change does trigger a review — but it names a strict subset of when the
review actually runs, and the harm is in what it leaves out: it withholds the
real remedy (send another message; you will be re-reviewed) and prescribes a
pipeline edit to someone whose pipeline may be fine. The comment at
`no_tool_policy.py:168-176` records that this precise defect class — "an advisor
outage on a validated build told the user the review did not clear and to
'Review the pipeline' — a pipeline with nothing wrong" — was already closed
once. This partially reopens it.

Fix: make the clause conditional on the shape. The GREEN/RED shapes keep "after
your next pipeline change"; the UNVERIFIED/ABSENT shapes need wording that is
honest when no fact was recorded. If a per-shape split is too much for this
branch, the neutral form is "…runs again when the pipeline changes" without the
"your next" promise, plus an explicit note in the hand-back. Splitting
`_ADVISOR_SIGNOFF_UNRENDERED_NEXT_STEP` into per-shape constants changes how
notices *compose*, not the template structure the wire-shape gate pins — but
re-run `test_the_round_trip_parametrization_covers_every_wrapped_template`
(named in the plan's Task 4 Step 4) before believing that.

#### I3. Two edited notices are now self-contradictory

`src/elspeth/web/composer/no_tool_policy.py:244-263` and `:184`

- `_ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_NOTICE` now reads (header
  + edited clause): *"…a chat message is not something the composer can change.
  Composer completion is withheld. Pipeline readiness was not re-verified this
  turn. Reword your message and resend; validation and the advisory review run
  again after your next pipeline change."* Its own GREEN twin (`:251`) says "No
  pipeline change is needed". One sentence tells the user to reword and resend
  and the next tells them only a pipeline change re-triggers the review.
- `_ADVISOR_SIGNOFF_UNRENDERED_NEXT_STEP` (`:184`) now reads *"Retry the
  request, or check the advisor model configuration; validation and the advisory
  review run again after your next pipeline change."* Whenever the outage block
  rode a **mutating** turn — the case where the fact does persist — the edit
  makes the first clause provably useless: retrying on the now-unchanged graph
  hits the skip and can never obtain a fresh verdict, so the block is clearable
  only by editing the pipeline. (GREEN is not always durable: it also arises on
  an unmutated turn via a reused `last_runtime_preflight` from
  `preview_pipeline`, or the blob-ref recompute at `service.py:3694`. In *those*
  sub-cases no fact persists and the contradiction is I2's instead — the user is
  told to change the pipeline when retrying would in fact work. Either way the
  sentence misdirects.)

The plan lists the "Retry the request" clause under Non-goals and says to leave
it. That was defensible when the clause was merely stale; it is not defensible
now that *this commit edited that same string* and turned it into a
contradiction. Scope discipline says the reported defect is the deliverable, and
Task 4's deliverable is "stop promising a re-review that will not happen" — this
string still does, in the same file, in a line the commit touched.

Fix: for `:184`, drop "Retry the request, or" (or replace with "Check the
advisor model configuration; the advisory review runs again when the pipeline
changes"). For `:260-263`, drop the re-review clause entirely — the unrepairable
family's whole point is that the remedy is the message, not the pipeline.

#### I4. Nothing crosses the persistence → parse → thread → skip seam

`tests/unit/web/composer/test_advisor_checkpoint.py:5747-5760` (AST pin only)

The executor flagged this and asked for a grading. **Important**, for two
reasons rather than one.

*Coverage.* The AST test pins keyword *presence*; the behaviour tests inject a
hand-built `CompletionGateFacts`. Nothing asserts that the object the route
hands down is derived from the row the writer wrote. Concretely, the skip's
positive case rests on one equality surviving a DB round-trip through
`state.to_dict()`:

```
completion_gate_fingerprint(_state_from_record(row))
  == row.composer_meta["completion_gates"]["advisor_signoff"]["for_graph"]
```

If that equality fails — because the writer stopped emitting the fact, because
`merge_composer_meta_updates` dropped the key on some save path, because the
route read a different row, or because a serialization change perturbed
`to_dict()` — the skip is **silently inert** and every test in this branch stays
green, returning the user to the reported bug with no signal. What tempers the
risk is that `execution/service.py:1544` already depends on the same equality
for `/validate`'s blocker wording, so a break would not be invisible everywhere.
But nothing proves it for *this* path except the test pair below.

*Blast radius of the new call.* `parse_completion_gates` is now on the hot path
of **both** chat routes, before the user's message is even persisted
(`messages.py:216` precedes step 2 at `:229`). It is Tier 1: it raises on
anything that is not `dict`/`MappingProxyType`
(`completion_gates.py:251`). That is the correct posture, but it moves an
envelope-drift failure from `/validate` and Run onto every `send_message` — and
it discards the user's typed message with it. The shape is already exercised by
`execution/routes.py:1003` and `_helpers.py:997`, so I am not claiming a live
break; I am saying the branch adds a 500-on-every-message dependency with no
route-level test.

Fix: one integration test in `tests/integration/web/composer` (or
`tests/unit/web/sessions`) that seeds a `composition_states` row whose
`composer_meta.completion_gates.advisor_signoff.for_graph` equals
`completion_gate_fingerprint(state)`, sends a message that mutates nothing, and
asserts `_run_advisor_checkpoint` was not called. A second case with a
`for_graph` for a different graph asserts it was. That single pair closes both
halves.

### Minor

#### M1. `slog.info` for a gate decision is the wrong channel under the logging policy

`src/elspeth/web/composer/service.py:6744-6749`

`.agents/skills/logging-telemetry-policy/SKILL.md` permits `logger`/`structlog`
for transitory DEBUG diagnostics, audit-system failures and telemetry-system
failures only, and its Must-audit table names "gate routed" as probative. The
ledger's justification is precedent (`composer_authoring_surface_selected`), not
the policy — and precedent is not the rule.

The content of the line is clean (closed vocabulary, no user text, no findings —
the security check in the brief passes). The gap is that a deliberate bypass of a
mandatory review now has **no durable record**: the other terminal outcomes
persist an `AdvisorTerminalPublication` row (`service.py:6947`) or an `audit`
chat row (`:6910`). Note the side effect on measurement: the diagnosis's "Not
measured" item — *"How often an unchanged-graph turn is flagged in production.
`AdvisorTerminalPublication` rows joined to a zero version delta would count
it"* — stops being countable the moment this ships, and is replaced by an
unqueryable log line.

Fix: either downgrade to `slog.debug`, or add the one-line audit row that the
surrounding code already has a writer for, or get an explicit telemetry-only
ruling from John and cite it in the comment.

#### M2. `completion_gate_fingerprint`'s docstring justification is factually wrong

`src/elspeth/web/execution/completion_gates.py:158-174`

It says `metadata` is excluded because "a rename ... does not invalidate an
advisor verdict". `_summarize_pipeline_for_advisor` (`service.py:10493-10496`)
renders `state.metadata.name` as "Pipeline:" and `state.metadata.description` as
"Intent (stated):" — the advisor reads both, and "does the topology match the
stated intent" is exactly the kind of finding the reported session produced.

Effect is narrow (a metadata edit bumps the version, so the same-turn review
always runs; the hole needs a *recovery* save to carry the fact forward across a
metadata-only change via `completion_gates_meta_from_facts`). But the predicate
now uses this fingerprint to decide whether a review runs *at all*, not just how
a blocker is worded, so the stale justification is a live trap for the next
change.

Fix: correct the docstring to say what is true — the fingerprint covers the
structural evidence, and `metadata` is excluded knowingly even though the
advisor reads it, because a version bump is the guard for that case.

#### M3. The merge can flip a legal `ComposerResult` into an illegal one; nothing pins why it currently cannot

`src/elspeth/web/composer/service.py:6378-6384`

`replace(result, runtime_preflight=merge_completion_gates(...))` re-runs
`ComposerResult.__post_init__`. `merge_completion_gates` clears
`completion_ready`, which is one of the three conditions in the pending-handoff
escape clause at `protocol.py:365-372`. So the merge can move `preflight_failed`
from False to True, and a result with `raw_assistant_content is None` then
raises `ValueError` — a 500 on a question turn.

I traced it and it is **safe today**, by exactly one thread:
`no_tool_finalize.py:387-393` does return a handoff-shaped result with
`raw_assistant_content=None`, but `_surface_and_finalize_no_tools` intercepts
that shape at `service.py:6592` and routes it through
`_append_interpretation_review_handoff_message`, which always sets the field
(`service.py:1257`). The orphan producer sets it too (`service.py:6511`). No
test pins that coupling.

Fix: one regression test — drive `_try_terminate_no_tools` with a
`finalize_result` in the handoff shape (`is_valid=False`,
`authoring_valid=True`, `completion_ready=True`, `execution_ready=False`) and a
blocked fact, and assert it does not raise. Cheap, and it fails loudly the day a
new handoff producer forgets `raw_assistant_content`.

#### M4. Task 5 is unrecorded — no dated hint line, and no ledger entry for the gates

`docs/agents/recent-code-hints.md`

Step 4: the file has no 2026-09-22 entry and the diff does not touch it. The
plan specifies the exact text. AGENTS.md points every agent at this file as the
dated incident log, and the branch adds a convention a future `compose()` caller
must know ("a caller that holds a state row should pass
`parse_completion_gates(record.composer_meta)`"). Add it before hand-back.

Steps 1–2 are also absent from the ledger, which ends at Task 4: no recorded
`ruff check` / `ruff format --check` / `mypy src/elspeth/web` result, and no
before/after trust-tier lint-corpus diff. (On the one lint question I could
settle by reading rather than running: the 187-character
`no_tool_policy.py:184` is fine — `line-length = 140` but `E501` is in
`pyproject.toml`'s ruff `ignore` list, and the formatter cannot split a lone
string literal.) The hand-back must carry all four artefacts —
lint, types, lint-corpus diff, and the gate's `summary.txt` — not just the
suite result.

#### M5. The finalize-tail comment overstates when the fold runs

`src/elspeth/web/composer/service.py:6374-6377`

The comment reads "The END gate stood aside for a graph the advisor already
blocked". The fold is guarded by the predicate, not by *why* the gate fell
through: it also fires when the gate returned early at `:6731` (structurally
empty, or `advisor_checkpoint_passes_used >= max_passes`) while a matching
durable fact happens to exist. The behaviour is right in those cases too — a
durable block should still withhold completion — but the comment describes one
branch as if it were the only one. One clause fixes it.

#### M6. What the finalize stub hides (stated for the hand-back, not a defect)

`tests/unit/web/composer/test_advisor_checkpoint.py:2369-2371`

`drive_try_terminate` replaces `_surface_and_finalize_no_tools` with a recorder,
so the three new tests never exercise the orphan surface+gate pair, the real
`_reuse_or_recompute_runtime_preflight`, the handoff appender, or the
grounding check. The consequential one: on a genuinely unchanged turn with no
preview, the real preflight is `None` (`service.py:3718`), so the merge is a
**no-op in the common case**. `test_skipped_turn_result_keeps_completion_withheld`
covers only the `preview_pipeline` sub-case — which is what the plan asked for,
so this is not a deviation — but the test's name reads as a general guarantee
that the code does not provide. Combined with the confirmed fact that no route
reads `result.runtime_preflight` on an unchanged turn, Review Focus 3's
real-world guarantee is delivered by `execution/service.py:1544` (`/validate`
and Run merge the durable fact themselves), not by this fold. Say that in the
hand-back so the fold is understood as a latent-invariant pin rather than as the
thing keeping chat honest.

#### M7 (nit). The merge passes `state`, not `result.state`

`src/elspeth/web/composer/service.py:6383`. Identical today (the predicate
already compared against `state`, and finalize returns the same object).
`result.state` would make the two arguments to `merge_completion_gates` come
from one source. Take it or leave it.

---

## Declined to judge

One line each; the executor rules on all of them.

- **Whether a re-review should be able to clear a block without a pipeline
  change.** Explicit plan Non-goal; the branch neither fixes nor worsens the
  durable half.
- **Showing advisor findings to the user (ruling 2, "reviewer's note").** Plan
  Non-goal; a third plan not yet written.
- **Publishing the composer's reply on a blocked turn (ruling 3).** Separate
  plan, `docs/plans/2026-09-22-composer-reply-published-on-advisor-block.md`.
- **`_replace_advisor_repair_public_result` after a CLEAN re-review (case 5).**
  Plan Non-goal, ruled KEEP.
- **The report's regression test 1.** Plan Non-goal; waits on OPEN Q1.
- **`ADVISOR_SIGNOFF_PENDING_DETAIL` ("Re-run the composer to obtain a current
  review") at `completion_gates.py:50-53`.** Same false-promise class as Task 4,
  but outside the composer package the new test scans and not among the plan's
  eight strings. Flagging its existence only.
- **`service.py:11110` ("Reword your chat message … then resend").** Plan
  Non-goal, listed as flagged-not-changed. Note it inherits I3's contradiction.
- **Whether the advisor should read `user_message` at all.** Design question
  behind both I1 and the incident (diagnosis OPEN Q5); out of this plan's scope.
- **The two pre-existing `test_freeform_planner_failure_translation` reds.**
  Reproduced by the executor on the untouched base; not this branch's.
- **The `test_chat_schema8_atomic` cancellation flake.** Passed serially per the
  ledger; AGENTS.md § parallelism flakes covers it.
- **Full-suite gate, ruff/mypy, and trust-tier lint corpus results.** Task 5 was
  in flight and I was instructed not to run suites; the gate's `summary.txt`,
  the lint/type results and the before/after lint corpus are owed evidence, not
  review findings. Tracked as M4 so they are not lost.
- **CHANGELOG entry and ticket elspeth-032ec69c41 closure.** Not in the plan;
  raising as a process reminder, not a finding.
- **Frontend and docs copy.** I re-ran the phrase grep across `src docs website
  tests` and the only hit is the test's own assertion, so there is nothing to
  judge.

---

## Recommendations

Ordered by what I would do before merging.

1. **Fix I3** (two contradictory notices). Smallest change, clearest user harm,
   and it is in a file the commit already owns.
2. **Fix I2** (per-shape copy) or, if a per-shape split is out of scope, use a
   shape-neutral wording and record the decision.
3. **Add the I4 route-level test pair.** It is the only thing that will tell
   anyone when the fix stops working.
4. **Close out Task 5 (M4)**: the dated hint line, plus the ruff/mypy and
   lint-corpus evidence in the hand-back. Completed plan steps, not extra scope.
5. **Surface I1 to John explicitly** — in the hand-back and as a line in the
   plan's Non-goals. Do not fix it here: persisting a fact on a non-mutating turn
   is a new save path and deserves its own ruling.
6. Rule on M1 (`slog.info`) with the policy text in hand, not the precedent.
7. Take M2, M3, M5 as cheap hygiene while the file is open.

---

## Assessment

**Ready to merge? With fixes.**

The core mechanism is correct, narrowly scoped, and honest about its failure
direction: the predicate is pure, the defaults review rather than skip, both
composer invariants hold (including the non-obvious R2-F13 path), and the
reported incident is genuinely fixed. What holds it back is user-facing copy the
branch itself made contradictory (I3) or misdirecting for a reachable shape
(I2), and the absence of any test across the persistence seam the whole fix
depends on (I4). I1 is a real limitation rather than a defect in the code as
written, but it must be recorded before this is called "the ruling implemented".
