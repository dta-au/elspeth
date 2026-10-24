# U-45 — accountable officer gate standard

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - W6: the tip is `c26167c4d` (9 commits; c9 COMMITTED, nothing uncommitted). The lead paragraph's "1b59c10a1 plus an uncommitted c9" and the "c9, uncommitted" line are stale.


Lane session: `u45-fix` (local_9c376c61-4ec2-4818-85b2-936ba2843342).
Unit base: `ea96096e1` (U-38 land tip) — coordinator ruling, deliberate exception
to AGENT-BRIEF's `release/0.8.10-assembly` default.
Officer: John's orientation session, appointed 2026-10-25 (confirmed by the
maintainer directly, deconflicted with the coordinator "0.8.10 - Main").

Standard = `docs/release/0.8.10-first-compose-diagnosis.md` §6, §8–§8.3, §10.4,
§10.5, §12, §13. Not this file; this file only says what I check and why.
Deliverable at the end is ONE line appended to the lane's report. No review
package, no sidecar, no approval chain (AGENTS.md § delivery posture).

## Order of operations

Lane finishes -> lane sends me tip SHA + report path -> I gate -> I append the
line -> tip goes to the coordinator. The lane's original brief said it reports
to the coordinator; that was corrected on 2026-10-25.

## Part 1 / Part 2 split (coordinator ruling, 2026-10-25)

U-45 lands in two parts. Both ship in 0.8.10 — this is sequencing, not
deferral. Part 1 is already complete and green, and it is what U-44b and
design-protocol need from `continuation_prompt_producers.py:98`.

**Part 1 = c1–c6** (`44a97bcde`, `c43922dd2`, `8598d9066`, `6e12d739b`,
`358bcca8b`, `33737d863`): the three §10 defects, the provenance seam, the
enumeration collapse, the diagnosis notes, F3/G6 and the fourth defect.
Astra + red-team on those six, then my gate, then it lands right after U-38.

- Checks that gate part 1: **9** (§10 root causes), **11** (diagnosis notes),
  **12** (third defect), **10** (composer invariants — c2 is on the dispatch
  path), **5** (no allowance or production policy touched), **2** (mutation
  evidence: M1–M6 captured), and the fourth-defect entry with its
  failing-first repro on pre-F3 code.
- **B6 also applies to part 1**, not just part 2: c6 is a performance change
  and c2 touches the dispatch path, so hops/windows/admission/clock counts must
  be shown unchanged across c1–c6, independently of the F1 work.
- The landing rule applies NOW, since part 1 lands first: failures only
  decrease across c1–c6, ids introduced at a commit are green there, compared
  as `(id, signature)` pairs.
- **Part 1 needs its OWN keyless trust-tier set-diff.** c2, c3 and c6 all
  touch `src/`, so AGENT-BRIEF's set-diff requirement binds on part 1 and not
  only on part 2. The lane's in-flight tip scan IS part 1's scan — attribute it
  there. Base 3889 lines, exit 1, confirmed non-empty.
- **Part 1 needs the PostgreSQL ids that cover the fixtures it changed.** The
  lane measured that `test_continuation_composition_initialization_postgres.py`
  IMPORTS the unit's fixtures (re-exports at lines 6–45), so c1's quota seed
  and c4's blob fixture propagate straight into the PG ids. AGENTS.md is
  explicit that a green default run says nothing about PostgreSQL. Landing
  part 1 with those ids unrun leaves exactly the gap that doctrine names.
  **RESOLVED 2026-10-25: the coordinator moved both into part 1, and part 1
  does not land without them.** So the part-1 blocking set is: Astra +
  red-team on c1–c6, the keyless set-diff over c1–c6, the two PG ids, B6
  unchanged across c1–c6, and the `(id, signature)` pair chain.

**Part 2** = F1/F2/F4 with B1–B6, the four per-stage carrier commits, the
G2/G4/G5/G7 refusal tests with mutation output, the two frontend A9 ids, the
remaining PG ids, and its own keyless set-diff. Its own Astra + red-team, lands
as a follow-on.

- Checks that gate part 2: **1** (B6 at tip), **3** (budgets red at base),
  **4** (non-vacuity controls), **6** (G5 — measured, closed, and the design
  basis for the carrier), **7** (A9, incl. the two owed frontend ids),
  **8** (F5 absent or proved), **13** (memo mechanism), **14** (carrier safety
  argument + `setdefault`), **15** (four-stage scope, per-stage evidence).

## What a diff can pass on while still being wrong

These are the discriminating checks. The generic "read the diff against §8"
does not catch any of them on its own.

1. **B6 unchanged, measured at tip.** 8 worker hops; 11 complete windows; 313
   admission-table reads; 72 clock reads (SQLite). §8.3 states B6 is the
   converse guard: a fix that goes green by DROPPING a required re-read lowers
   one of these. A drop means G2, G3 or G8 work was removed. Highest-value
   single number in the unit. Not "unchanged" by assertion — by measurement.
2. **Mutation evidence, not green tests.** §8.3 requires, for each new refusal
   test under G2/G4/G5/G6/G7, that the guarded check be disabled and the test
   confirmed red (worked example: skip the payload SHA-256 re-hash under a memo
   hit for G4). Passing tests do not satisfy this. I want the red output.
3. **Failing-first means the budgets are red at the base**, not "a test failed
   once": B1 107 and 68; B2 verify 18.7 MB over 11 distinct inputs; B4 60;
   B5 41. **CONFIRMED 2026-10-25: the lane measured the base budgets on
   `ea96096e1` equal to the §8.3 figures**, so these numbers are now measured
   on this unit's base, not carried over from `897dabeb8`. Any later
   divergence is a question, not a pass.
4. **Non-vacuity controls present** (§8.3): every alias binding holds the
   wrapper after installation (13/17/26/14 bindings for the four functions);
   every counter above zero in the stage; the parametrized positive control
   raises each counter by exactly one (measured: decode 213->214, statements
   1096->1097). Defining-module-only patching sees just 49 of 213 decodes and
   4 of 429 verifies (§12 point 6d), so alias coverage is load-bearing.
5. **No allowance widened, and no production policy value touched by this
   unit.** (Coordinator wording, 2026-10-25. RE-BASED — the 1.0 s fixture does
   not exist on this unit's base; see § Re-based items.) U-38 owns
   `src/elspeth/web/continuation_settings.py` and the derived test policy. A
   tip that reaches green by widening an allowance is a reject. If the lane
   believes 5.0 s is wrong, that is a proposal with evidence through me to the
   coordinator, never an edit. The §9 timed result is INFORMATION in the
   report, not the gate; the coordinator schedules one idle run near hand-over.
6. **G5 deep immutability resolved explicitly.** §8.1 marks it **I**: the
   nested members of the decoded `ContinuationFrameStack` must be checked at
   implementation time. Lane must state what it found and which branch it took
   (return shared / read-only consumers only / skip memoizing). Silence = gap.
   **CLOSED 2026-10-25: 13 frozen models, instrument controlled** — so the
   "return memoized value shared" branch is available. Confirm at the gate
   that the tip actually relies on that finding and nothing else.
7. **A9 recorded even though it is zero.** §8.2: the stage makes 0 provider
   calls before and after (§4.7), and the per-transition count still has to be
   in the review record. Trivially true, trivially skipped.
8. **F5 absent, or its preconditions proved.** F5 (proven-redundant SQL
   de-duplication, 267 data-table repeats) is optional and needs BOTH a proof
   that every writer of those tables holds the session advisory lock on PG and
   an explicit amendment of the `sessions/continuation_first_runtime.py:42`
   design rule. Absent is fine. Present without both is a reject.
9. **The two §10 masked defects fixed at root cause**, per §10.5: blob history
   written through the production persist path or with a real `VisibleToolEvent`
   envelope (NOT by relaxing the gate — the refusal for unbound history is the
   guard's contract, and §10.5(5) wants that negative case kept);
   `creation_modality` from the created blob's stored `verbatim`, not the
   literal `user_upload`; the double seed resolved so BOTH params end green,
   KEEPING the paid branch's policy state (§10.5(3)'s "in the `[True]` branch"
   is mis-premised — see § Re-based items); and the
   replacement history must genuinely exercise the blob re-read, so it must not
   set `selected_result_retained=True` (that short-circuits before any reader
   call). Both blob corrections are needed; either alone leaves it red.
10. **Composer invariants.** No server-side authoring of pipeline structure, no
    tutorial-only path. §8.2 says none of F1–F4 touches the planner or tutorial
    path; per-transition provider counts are the evidence.
11. **The diagnosis correction is part of this unit's landing** (coordinator
    ruling, 2026-10-25). The lane adds a dated "Correction (U-45 lane,
    2026-10-25)" note BESIDE §6 and §10.5(3) of
    `docs/release/0.8.10-first-compose-diagnosis.md`, plus a one-line banner at
    the top saying the document was measured at `897dabeb8`, that later units
    have moved the tree, and to verify a section before relying on it. One
    commit. CONTENT — narrowed after the lane RETRACTED its causal claim, and
    the earlier wording must not survive:
    - §10.5(3)'s diagnosis and fix are **correct**. Only the phrase "in the
      `[True]` branch" is loose, for a block that is textually unconditional.
      The note says that, narrowly. It must NOT say the seed makes both params
      red — the collision is paid-only, because only `ready_case(paid=True)`
      routes through `paid_title` → `title_case`, which pre-seeds alice
      (`test_continuation_title_provider.py:66-68`, verified), and
      `grant_test_pipeline_user` is idempotent while `seed_token_policies` is
      a raw insert against a unique `identity_id`.
    - §6's claim **held** at `897dabeb8`. The note says what changed on
      `ea96096e1`, measured, not inferred (u45-firstcompose's refinement).
    - §8's 1.0 s exit criterion is obsolete — note it beside §8.
    - §10.5(1)'s "attach a real `VisibleToolEvent` envelope" is NOT achievable
      on this tree (lane measurement, 2026-10-25):
      `validate_visible_audit_row` admits a visible companion only on an
      `audit` row with exactly two envelopes, so the public `add_message`
      fails closed against an envelope on a `tool` row. Only
      `persist_compose_turn_async` writes that shape, which is what the lane
      used. Note it beside §10.5(1).
    The existing text STAYS AS WRITTEN: a tip that rewrites §6, §8 or §10.5(3)
    instead of annotating them is a reject. u45-firstcompose (the author, now
    on U-46) gives a one-line confirmation before hand-over. Tracked file under
    `docs/`, so it lands in the unit's diff and in the commit list.
12. **The third defect — prompt-guard provenance lost through `deep_thaw`**
    (coordinator ruling 2026-10-25: IN U-45's scope, the lane fixes it).
    Chain, verified by me from the object store at `ea96096e1`:
    `prompts.py:650` builds `PROMPT_GUARD_CONTENT_PARTS_KEY` as a tuple;
    `continuation_primary.py:164` does `owned_messages, owned_tools =
    deep_thaw(messages), deep_thaw(tools)` and passes them straight to
    `prepare_continuation_primary`; `contracts/freeze.py` turns EVERY tuple
    into a list (`isinstance(value, tuple) → [deep_thaw(item) ...]`, docstring:
    "converts ALL tuples to lists ... This is intentional — JSON has no tuple
    type"); so `validate_prompt_guard_content_parts`
    (`contracts/prompt_guard.py:119-120`, `type(parts) is not tuple`) correctly
    raises "Prompt guard content partition is not an owned producer identity".
    **The gate is right and the input is wrong.** Requirements:
    - **Enumerate every `deep_thaw` caller that can carry guard provenance,
      BEFORE the fix.** Co-location of `deep_thaw` and the guard key in one
      file is a SCREEN, not a finding: the enumeration must trace actual
      dataflow into the validator. My screen at `ea96096e1` — files naming the
      guard key that also contain `deep_thaw`: `pipeline_planner.py` (10
      hits), `tool_batch.py` (5), `continuation_message_family_builders.py`
      (2), `prompts.py` (1). And `detach_prompt_guard_evidence` is reached
      from four gateway entry points, `provider_gateway.py:489, 690, 891,
      1125` — `:690` is the one in the traceback, so the live question is
      which of the other three receive thawed input.
    - **Fix at the provenance/wire split, not at one call site.**
    - **`deep_thaw`'s semantics stay as they are, and the validator is NOT
      loosened.** Accepting a list there is defensive code on Tier-1 owned
      data (ADR-032; the maintainer's fix-at-the-right-location doctrine) and
      would blind the gate to a genuinely foreign partition. Relaxing either
      is a reject, not a pass.
    - **Astra reviews this part specifically.**
    - **REUSE IS A REQUIREMENT, and a hand-written second guard-key list is a
      REJECT** (coordinator, 2026-10-25). The fix reuses
      `detach_prompt_guard_evidence` and the existing key set at
      `continuation_prompt_producers.py:98`
      (`{PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY,
      PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY, PROMPT_GUARD_THINKING_PARTS_KEY}`).
      Two independent lists of guard keys drift the first time a fifth key is
      added, and the list that misses it fails open.
    - **Any shared-file change beyond `continuation_primary.py` goes to the
      coordinator first.** `contracts/freeze.py`: the coordinator owns that
      decision and the default is NO CHANGE — `deep_thaw`'s semantics stay. If
      the enumeration shows the provenance/wire split belongs in `freeze.py`,
      the lane brings the proposal with its evidence BEFORE editing, and the
      coordinator puts Astra on it as a shared-contract change. A new helper
      beside `deep_thaw` is NOT a change to `deep_thaw`, but it routes through
      the coordinator too. `continuation_primary.py` is U-44b (orphan) on the
      carve map.
13. **SUPERSEDED BY CHECK 16 — read 16 for the live mechanism.** Option (b)
    was replaced by option (d) on 2026-10-25 after the lane measured that
    explicit plumbing touches 21+ call sites in 12+ files across several lanes.
    Kept because its four non-vacuity tests and its process-global reject
    carry over to (d) unchanged, and because the reasoning for rejecting (c)
    is what makes (d) acceptable. Do not gate against (b)'s mechanism.
    *Historical:* **F1's memo mechanism: explicit stage-owned plumbing, option
    (b)** (coordinator ruling, 2026-10-25). NOT option (c), `copy_context` in
    `async_workers.py`: that would change every `ContextVar`'s propagation as
    a side effect, `prompt_guard_user_scope` included — a blast radius far
    wider than the unit, and exactly the shape of change that buys a local win
    at a global risk. The lane maps the hop chain and the signature list to the
    coordinator BEFORE editing.
    - **A process-global memo is a REJECT.** This is the load-bearing
      condition: a memo that outlives one retention stage, or is shared beyond
      it, is a cross-stage and potentially cross-session data path, not an
      optimization. The four required non-vacuity tests are what prove it is
      not one, so none of them is optional:
      1. the B1 counters **drop inside the hops** — the memo is really hit
         where the work happens, not bypassed;
      2. **two concurrent stages share nothing** — no cross-stage leak;
      3. **no entries survive the stage** — no lifetime leak;
      4. **B6 unchanged** — nothing required was dropped to buy the win.
    - Tests 2 and 3 are security properties, not performance checks. A missing
      or weakened one is a reject, not a gap to note in passing.

14. **F1's carrier: the safety argument, the `setdefault` form, and the lock
    escape hatch** (coordinator ruling, 2026-10-25 — all three REQUIRED).
    - **The signature list states that the safety argument is the
      sequential-awaits one**: the hops are awaited in sequence, and
      `concurrent.futures.Future` locks in both `set_result` and `result()`, so
      each await establishes happens-before between one hop's writes and the
      next hop's reads. It must NOT cite GIL atomicity as the reason a lock is
      unnecessary. The GIL keeps each individual dict read and write
      indivisible, so no torn state is observable; it does NOT cover the memo's
      check-then-set compound. One support, not two.
    - **The memo uses `setdefault`**, no longer optional:
      ```
      hit = memo.get(key, _MISSING)
      if hit is _MISSING:
          hit = memo.setdefault(key, compute(key))
      ```
      Rationale is no-tech-debt: a memo correct only while nobody adds a
      fan-out is a latent defect, and G5's shared-identity branch needs exactly
      one winning object per key.
    - **TRAP — the `get` guard is load-bearing, not decoration.** Python
      evaluates arguments eagerly, so a bare
      `memo.setdefault(key, compute(key))` calls `compute` on EVERY access.
      That memo is a no-op for cost while still looking like a memo, and it
      would leave B1/B2/B5 exactly at their base figures. What catches it is
      non-vacuity test 1 (the B1 counters drop inside the hops) — a good
      example of the test set covering an implementation trap, not just a
      design one. Check the guard is present and the counters actually moved.
    - **The escape hatch stands:** if any hop turns out to run while the
      carrier is being written, it needs a lock AND a test. `setdefault`
      removes the identity hazard; it does not remove the measurement.

15. **SCOPE EXPANSION: the clock-owned carrier covers all four retention
    stages, inside U-45** (coordinator ruling (B), 2026-10-25). Justified by
    measurement: stages 1–3 re-decode one snapshot 31, 36 and 51 times — the
    same defect at smaller scale — and first-Compose's figures grow with
    history, so deferring would be tech debt. Structure: first-Compose first
    with B1–B6, then ONE COMMIT PER remaining stage, each carrying before/after
    decode max-per-key, B6 unchanged for that stage, and the non-vacuity tests
    for that stage. The signature list for all four chains goes to the
    coordinator BEFORE any edit.
    - **What I measured about "clock-owned", which the ruling's wording does
      not capture.** There are FOUR clock constructions, one per entry point,
      each a per-invocation local: `continuation_composition_initialization.py:358`,
      `continuation_initialization.py:206`, `continuation_title_application.py:84`,
      `continuation_title_provider.py:66` (the last on `policy.actions.title`,
      the rest on `session_sql`). So clock-owned is NOT one carrier spanning
      four stages — it is four independently scoped carriers. Good: that keeps
      it clear of the process-global reject by construction.
    - **But a clock spans MORE than its retention stage.** Stage names are
      `physical` ×5, `retention` ×4, `guard` ×1, `quota` ×1, and they share
      clocks: `continuation_composition_initialization.py` makes 2 `run` calls
      on one clock (`physical` at :364, `retention` at :381);
      `continuation_initialization.py` makes 3. So entries created during
      `physical` are visible during `retention`, which contradicts §8's literal
      "for one retention stage".
    - **RESOLVED by the coordinator on that measurement: the carrier is scoped
      to the retention STAGE RUN, not the clock.** `clock.run("retention", …)`
      opens the carrier, threads it, and closes it on exit; each of the four
      entry points has its own clock, and each retention run its own carrier.
      Nothing lives across the physical stage. So §8 stays literally true, and
      **G2 is protected by structure rather than by restraint** — which is the
      stronger position and the reason this measurement was worth making.
    - The two non-vacuity tests therefore KEEP their stronger original
      wording: *no entries survive the retention stage*, and *two concurrent
      retention stages from DIFFERENT entry points share nothing*.
    - **Key-set test adopted** (my suggestion, coordinator-required): the
      carrier may hold only results of pure parse helpers, and a test asserts
      its key set contains nothing admission-derived or SQL-derived. The
      signature list must name every function the carrier memoizes. Structure
      now prevents the breach, so this test is defence in depth rather than the
      sole guard — keep it anyway; it is what catches a later widening.

16. **MECHANISM CHANGE: option (d), an ambient ContextVar carrier in a FRESH
    Context** (coordinator approval, 2026-10-25). Explicit plumbing (b) was
    measured to touch 21+ call sites in 12+ files across several lanes. Note
    (d) is NOT the (c) rejected earlier: (c) was `copy_context()`, carrying
    every ContextVar; (d) is a fresh Context carrying only the memo variable,
    which answers the original objection directly. Required conditions, all
    gate checks: fresh Context holding only the memo var, with a regression
    test that `prompt_guard_user_scope` and other caller ContextVars do NOT
    cross; `clock.run("retention")` sets the variable and resets it BY TOKEN in
    `finally`, tested after the stage and after an exception; fail-safe when no
    carrier is active, both paths tested; the untruncated instrument proves
    every thread entry goes through the propagating hop; the ambient-carrier
    reason documented at the definition; Astra on the `async_workers.py`
    change and red-team on isolation.
    - **MEASURED, and it endorses (d): pool threads already get a fresh
      context today.** `async_workers.py` contains no `contextvars` /
      `copy_context` usage at all (control: the same grep finds `contextvars`
      in 12 other `src/` files, so its silence there is real). Since
      `_submit_shared` hands work to a `ThreadPoolExecutor` and nothing
      propagates context, every ContextVar already reads its default inside a
      pool thread. So (d) is **current behaviour plus the memo** — it strips
      nothing that reaches the worker today.
    - **That also closes the gap condition 1 does not cover.** Condition 1
      tests that caller vars do not cross IN. It does not test the converse —
      whether anything inside the hop NEEDS a caller var — and a fresh Context
      would silently break such a dependency while the isolation test passed.
      The measurement answers it: nothing in the hop can depend on a caller
      ContextVar, because none reaches it today. Eleven ContextVars exist in
      `src/`, including `_SESSION_WRITE_LOCK_HELD`
      (`web/sessions/service.py:471`) and `_USER_PROMPT`
      (`composer/prompt_guard.py:48`); none of them crosses into the pool.
    - **NEW TRAP specific to (d), and it is the eager-`setdefault` failure in
      another costume.** A ContextVar set on the caller/event-loop side is NOT
      visible inside the pool thread. So if `clock.run("retention")` sets the
      variable on the caller side rather than inside the Context the worker
      actually runs, the carrier is simply absent where the work happens: the
      fail-safe path engages, every call recomputes, and the memo is a no-op
      that reviews clean. What catches it is again non-vacuity test 1, the B1
      counters dropping INSIDE the hops. Require that the test demonstrate a
      hit in the worker, not merely that the variable was set.
    - **Watch `_SESSION_WRITE_LOCK_HELD`'s set/guard pair stays co-located.**
      `service.py:1099` sets it and `:1080` guards on it
      (`if (id(conn), session_id) not in ….get()`), with `:1112` resetting by
      token. That pair only works inside one context. (d) must not change where
      the hop runs relative to that guard; if the propagating hop moves the
      boundary, the guard reads an empty set and either refuses spuriously or
      stops detecting a held lock.
    - **Required after the (d) change** (coordinator, 2026-10-25): name
      `_SESSION_WRITE_LOCK_HELD` and `service.py:1080/1099/1112` explicitly in
      the Astra brief for `async_workers.py`, AND run the PostgreSQL
      session-write-lock contention ids, because that guard's failure mode only
      appears under contention and a serial test cannot see it. The id is
      `tests/testcontainer/web/test_session_mutation_fencing_postgres.py::test_postgres_composer_preferences_concurrent_updates_serialise_under_the_session_write_lock`
      (line 585); the adjacent dual-fence takeover and target-rename deadlock
      proofs in the same file are the nearest related contention cases. That
      file is in the `testcontainer` selection, so it needs a slot.
    - **Put the ContextVar measurement in the report**, not just the gate: pool
      threads already run with every ContextVar at its default, established
      with a controlled grep, which is what makes (d) "today's behaviour plus
      the memo" rather than a propagation change.

## Re-based items (officer verification, 2026-10-25)

§8's exit criterion and §10.5(3) were written at `897dabeb8`. U-38 — this
unit's base — moved the ground under both. The lane raised it; I verified it
from the object store at the commit rather than from its report, controlling
the grep in both directions, because a search that finds nothing returns
exactly what a correct search returns when there is nothing to find.

- `git grep -n retention ea96096e1 -- tests/helpers/` → ONE hit, an unrelated
  message string at `web_reserve_provider_mint_contract.py:241`. The
  `retention=1.0` literal is gone.
- Control, negative→positive: the same grep at `897dabeb8` DOES hit
  `tests/helpers/continuation_policy.py:12` (provider, `retention=5.0`) and
  `:13` (local, `retention=1.0`). Control, positive: 8 `retention` hits in
  `src/elspeth/web/continuation_settings.py` at `ea96096e1`. The silence at the
  base is a real absence, not a broken instrument.
- `minimal_continuation_policy()` now calls `continuation_policy_from_settings`
  (docstring: "design §4.5, U-45 §13.5"). `session_sql: _local(5.0)`, and
  `_local()` sets `retention=5.0`; the settings class docstring reads
  "`session_sql.retention` is 5.0 s per U-45 §13.4". U-38 implemented §13.5.
- So "the existing 1.0 s first-Compose test passes without padding" CANNOT be
  measured on this base, and the TimeoutError ids are green at `ea96096e1`
  before any U-45 work (lane measurement, logs in its worktree lane dir:
  composition-init 3 failed / 23 passed, zero TimeoutErrors, the 3 reproducing
  at `-n 0`; first_runtime 9 passed).
- `grant_test_pipeline_user` + `seed_token_policies` sit directly in the `try:`
  body of `test_constructed_first_compose_enters_original_guarded_primary_once`
  under `@pytest.mark.parametrize("paid", (False, True))`, with NO `paid`
  conditional. §10.5(3)'s "`[True]` branch" therefore does not exist, and §6's
  "the `[False]` variant never runs `paid_title` and passes" no longer holds —
  both params are red on base.

Consequence, ruled by the coordinator and consistent with §13.5: the
deterministic gate is B1–B6 measured, the G2/G4/G5/G6/G7 refusals with real
mutation-red output, and the §10 root causes. F1–F4 still have to be done —
the budgets prove that work, they do not replace it. The §8.3 base budget
figures in item 3 above were measured at `897dabeb8` and are indicative only;
they are to be re-measured on `ea96096e1`.

Forward risk to carry into the report: §13.4 calls 5.0 s "still a ceiling on
growth", reaching about 5 s at roughly 200–300 prior pairs (**I**, inferred).
With 5.0 now frozen in production, F1–F4 plus the budgets are what stand
between this and a re-timeout on long sessions. Coordinator agreed 2026-10-25:
this is exactly why the budgets and F1–F4 are not softened now that the timed
test no longer bites. Hold that line at the gate.

§9 idle result — disposition ruled by the coordinator 2026-10-25: it belongs to
U-38's production policy, NOT to U-45. At p95 ≤ 2.5 s on PG at 50 prior pairs
it is recorded as the evidence for the 5.0 s that U-38 froze before the run,
which closes that gap. Above 2.5 s, design-runtime re-sizes on its merits from
that number before the 0.8.10 merge — a RELEASE blocker, not a U-45 blocker.
U-45 lands on its deterministic gate either way, so I do not hold the tip
waiting for this number.

## Landing rule: failures may only decrease (general form)

Coordinator ruling, 2026-10-25, written here in its general form because it
recurs on any unit that fixes several defects in a shared file.

**The rule is "no landed commit may make a green test red" — not "every test is
green at every commit".** That is bisect's definition of a regression. What it
targets is a commit that adds a failing-first test ahead of its own fix, or
that carries a planted mutation: either makes that commit look like it
introduced the bug.

Sequential fixes in a shared file are therefore fine as separate commits,
**provided failures only decrease**. Reds inherited from the base are not
regressions: they are red before the unit, stay red until their own fix lands,
and no commit turns a green id red. U-45 qualifies — fewer ids fail at each
step — so its four commits land as four, in one contiguous cherry-pick
sequence with green checked at the end of the sequence.

**The measurement** (coordinator's spec, cheaper than a full per-commit suite):
at each of commits 1–3, in detached worktrees at `-n 0`, run the three base-red
ids, the whole `test_continuation_composition_initialization.py`, and at commit
2 also the new `test_prompt_guard_provenance_thaw.py`. The check is that each
commit's failing set is a **subset** of the previous commit's. The chain is
base → c1 → c2 → c3 → c4, and base and c4 are already measured (3 failed → 0
failed, 27 passed).

**Three refinements the subset check needs, or it passes a real regression.
Refinements 2 and 3 were each found by a measurement, not by review:**

1. **Compare id SETS, never counts.** Commit 4 adds a test (§10.5(5)'s kept
   negative case: 26 collected at base, 27 at tip), so collection counts differ
   between commits and a count comparison is meaningless.
2. **A subset check over ids is blind to a CAUSE CHANGE under a constant id
   set** (lane observation, 2026-10-25, measured). At c1 the failing set stayed
   equal to base at 3 ids, because `[True]`'s quota error was fixed there and
   it then failed on the prompt-guard defect instead — the same id, a different
   cause. A subset check reads that as "no progress". Worse, it would equally
   pass a commit that fixed one failure mode and introduced a different one in
   the same id, which is a genuine regression wearing a clean subset check.
   **MECHANISM (coordinator, sharpened, AGENT-BRIEF clause (c)): compare
   `(id, failure signature)` PAIRS, not bare ids**, where the signature is the
   exception type plus the first line of its message. A changed cause then
   appears as a removed pair AND an added pair, and every added pair must be
   explained in the report. This is strictly better than requiring narrative
   discipline — it makes the detection mechanical instead of trusting the
   reporter to notice. The c1 case (quota → prompt-guard under the same id)
   gets an explicit line under it.
   **Caveat to carry: signature stability.** The first line of a pytest
   `AssertionError` can embed volatile data — a `tmp_path`, a uuid, a digest,
   a timestamp, a count — which produces spurious added/removed pairs that look
   like cause changes. Where a signature embeds volatile data, normalise it and
   say in the report how. A spurious added pair costs an explanation; a
   normalisation that hides a real cause change costs the whole check, so
   normalise narrowly and show the rule used.
3. **A subset check is blind to newly added ids, which is exactly the case the
   rule exists to catch.** `test_prompt_guard_provenance_thaw.py` does not
   exist at commit 1; commit 2 adds it *together with* the provenance fix. So
   that file must be **green at commit 2** — a new test file that is red at the
   commit introducing it IS a new red, however the inherited set behaves, and
   it is precisely "a failing-first test ahead of its fix". Commit 4 also edits
   that file (14 lines), so it must still be green there.

## Report completeness (coordinator's hand-over spec)

- tip SHA + unit branch + commit list with source SHAs
- failing-first ids: red before, green after, with the ids both times
- base comparison: any red set-diffed against the same run on `ea96096e1`
- keyless trust-tier corpus set-diff base vs tip (added/removed findings, never
  counts, never against zero; exit 1 expected both times)
- PostgreSQL / testcontainer result, or the ids owed and why
- A9 per-transition provider-call counts
- Astra findings AND red-team findings, with disposition of each
- the base-SHA exception reason on record (coordinator's U-38 reasoning)
- every gate command with exit code and log path; nothing reported from a pipe
- the two maintainer-approved xfails untouched; report any status flip
- the banner plus the §6 / §8 / §10.5(3) correction notes added BESIDE the
  original text (not a rewrite), plus u45-firstcompose's one-line confirmation
- the third defect: the `deep_thaw` guard-provenance enumeration, the fix at
  the provenance/wire split, and Astra's review of that part specifically

## Escalation

To the coordinator: sequencing (U-38 landing, rebases, capacity slots), files
owned by other lanes (design-runtime, design-protocol, carve-partition,
u45-firstcompose), genuine design decisions. The §9 idle-timing protocol is
"for the coordinator to schedule"; it is no longer the exit criterion, and the
coordinator has committed to one idle run near hand-over by pausing the other
lanes' runs. To the maintainer if the coordinator is unavailable.

## Part 1 evidence as measured (2026-10-25)

- **Pair chain complete, c1–c7**: exactly ONE added pair across seven
  transitions — base→c1 removes `[True]`/`sqlalchemy.exc.IntegrityError` and
  adds `[True]`/`AuditIntegrityError` — which is the pair the lane predicted in
  writing before measuring. Every other transition is removals only. This is
  the case the bare-id check could not see: the id set at c1 stayed at 3, so
  the old instrument read "3 → 3, nothing achieved" while the pair chain shows
  one failure mode closing and another surfacing under the same id. Clause (a)
  of the landing rule, measured against a real unit before it was written into
  AGENTS.md.
- **Newly-added ids green at their introducing commit**: thaw file 6 passed at
  c2 and 7 at c3 after the reorder; isolation file green at c6; c7's own M7/M8
  regressions green at c7 (40 passed, 0 pairs).
- **B6 across c1–c7, ACCEPTED WITH A RECORDED LIMITATION.** Measured
  whole-test-scoped on `[nonpaid-rootless]` at `-n 0`: SQL statements
  3494→3494, transactions 124→124, worker hops 36→36, lock acquisitions
  156→156, timed stages 6→6. The lane flagged — correctly, and unprompted —
  that §8.3's B6 figures (8 hops, 11 windows, 313 admission reads, 72 clock
  reads) are RETENTION-STAGE-scoped while these are whole-test-scoped, so this
  is not a reproduction of 8/11/313/72.
  **I corrected its "strictly stronger" to "consistent with nothing being
  dropped, but cannot exclude a compensating pair":** an unchanged aggregate is
  consistent with retention SQL falling by *n* while something else rises by
  *n*. Accepted for part 1 anyway on two grounds that belong in the report
  instead of an adjective — FIVE independent counters are unchanged
  simultaneously, which makes a compensating-pair hypothesis unserious; and
  there is no mechanism, since c1/c4 are test fixtures, c5 is docs, c2/c3 touch
  no SQL, and c6/c7 are in-memory schema copies. **Retention-scoped B6 is a
  PART 2 deliverable that retroactively confirms part 1**, where the question
  flips to whether a memo LOWERED one of them and the scope must match the
  diagnosis's own.
- **`_StrictResponse` is 7/7, not 6/7** (lane AST-check, superseding my grep):
  all seven bases under `src/elspeth/web/` are
  `ConfigDict(strict=True, extra="forbid")` with no `frozen`, including
  `execution/schemas.py`. Control: 255 files under `web/` contain
  `frozen=True`. My `grep -A12` reported "<no model_config in window>" for that
  file because a long class docstring pushes `model_config` past the window —
  **a blank window is indistinguishable from an absence.** Fourth instrument
  error of the night, same family; reusable form: a windowed or line-based
  search against wrapped or docstring-laden source yields negatives that are
  not evidence until a positive control passes.
- **The landing rule is now tracked**, resolving the durability concern:
  `a2cbca5b0` "docs(agents): landed commits must never turn a green test red",
  at `AGENTS.md:241-253`, ancestor of the target's HEAD. It carries all three
  clauses plus the normalisation rule and the cherry-pick-conflict re-check.
  **BUT it is NOT an ancestor of `codex/0.8.10-u45-fix`** (it landed after
  `ea96096e1`), so the lane's own worktree `AGENTS.md` has zero matches. Any
  lane whose branch predates `a2cbca5b0` is held to a policy its tree does not
  contain — raised with the coordinator as a fleet-wide point.
  **RESOLVED 2026-10-25:** AGENT-BRIEF now states the target's AGENTS.md is
  authoritative over worktree copies, read with
  `git show release/0.8.10-assembly:AGENTS.md`, and every lane has been told.
  I verified the command against both trees before relaying it — the target's
  copy matches `git bisect` once, the lane's branch zero times — because a
  command that silently returns the stale file would be worse than none.

## The instrument lesson, and the uncomfortable part of it

Five instrument failures in this unit — four mine, and the lane's `full_key`
metric. The lane's strengthened form, which is better than mine: **a windowed,
line-based or key-based instrument can return a result structurally
indistinguishable from the correct answer, so a negative is not evidence until
a positive control passes.** Second clause: **a control can establish one
property while saying nothing about the property you actually need, so name
which property the control proves.**

**The uncomfortable part: this rule was already written down, and we re-derived
it four times anyway.** `AGENTS.md` § Claims Must Be Measured already says
"Control an ad-hoc instrument before you trust a number it produced: run it
against a known-positive case it must match and a known-negative case it must
not. A `grep`, `find -size`, glob or regex that silently matches nothing
returns exactly what a correct one returns when there is nothing to find, so a
clean answer is not evidence the instrument works." That text was in my context
from the first message of this session. The maintainer confirmed via the
coordinator that no new tracked wording is needed, and he is right.

So the failure was **not** a documentation gap. It was that a known rule did not
fire at the moment of use. The trigger is specific and worth stating as one:
**the moment a search, grep, window or key comparison returns a negative or an
"unchanged", that is when the rule applies** — not when writing up the result,
by which time the negative already reads as a finding. Four of my five misses
were caught by a control I ran *after* forming the conclusion; the fifth
(AGENTS.md line-wrapping) I caught only because the commit existed and
contradicted my grep.

**The three families, as they ended up:**

1. **A negative trusted before the instrument was shown capable of a
   positive.** Already in AGENTS.md § Claims Must Be Measured. Four of mine.
2. **A control that validated one property, mistaken for validating another** —
   the lane's `full_key` metric: a stability control *passed*, proving the
   metric deterministic, while saying nothing about whether it was
   *comparable* across runs (it embedded per-run UUIDs, so no key was).
3. **A verification whose success condition cannot distinguish the good
   outcome from the bad.** Tree-hash equality after the U-38 rebase: the hash
   must change whether the lane's work survived or whether it had re-applied a
   superseded U-38, so neither value discriminates. Distinct from 1 and 2
   because the instrument is *sound* — it is the claim that is unfalsifiable.
   The lane's phrasing: a check that returns the same answer either way is not
   a weak check, it is not a check.

Families 2 and 3 are not covered by the existing section, which handles
known-positive/known-negative for EXISTENCE and mutate-it-goes-red for gates.
Recorded here and in the lane report only; NOT escalated, per the maintainer's
ruling that no further tracked wording is needed.

Prior note, superseded by the list above: the lane's
determinism-vs-comparability distinction. The section covers known-positive /
known-negative for EXISTENCE and mutate-it-goes-red for gates, but not a metric
that is present and stable yet incomparable across runs because it embeds a
per-run identifier. Recorded here and in the lane report; NOT raised with the
maintainer, per his ruling that no further tracked wording is needed.

## Test-capacity envelope (AGENT-BRIEF clarified 2026-10-25)

Raised because the lane disclosed two concurrent `-n 0` runs against the old
"one pytest process at a time" wording rather than deciding it silently. The
brief now says: **one multi-worker pytest process per lane**; single-worker
`-n 0` runs may run alongside it or each other, up to about **4 workers' worth
per lane in total**, with at least **8 GB RAM free**. Wide runs (a whole
directory, `-n` above 4, the full suite, the testcontainer selection) still
need a slot from the coordinator. This governs how part 2's per-stage evidence
is gathered, which is a lot of measuring.

## Watch items

- Preserve ref before any rebase that drops commits:
  `git for-each-ref 'refs/heads/preserve/0.8.10-u45*'` must be non-empty.
  Nothing exists yet; check when the U-38 rebase is imminent.
- Flaky areas (e2e/recovery, integration/pipeline, unit/engine/orchestrator):
  re-run a red id at `-n 0` before attributing it to the change.
- The Landscape-fencing escape test cannot complete on the target (U-46 owns
  it); do not block hand-over on it.
- Prototype `codex/0.8.10-u45-fix-prototype` (5932af0e9) is WIP, NOT REVIEWED,
  based on pure v152 rather than the target: reference only.
- The lane's stated prediction, to be measured once the quota seed is fixed:
  `[True]` dies at the seed BEFORE the primary invocation, so it should then
  fail with the same prompt-guard error. If that holds, the third defect is
  not optional — the test cannot reach green without it. Hold the lane to
  reporting this either way; a prediction stated before the test is the
  honest instrument, and it decides whether check 12 is load-bearing.
- PREDICTION CONFIRMED 2026-10-25, so **check 12 is load-bearing**: measured
  base 3 → c1 3 → c2 1, i.e. `[False]` and `[True]` both go green at the
  provenance seam. `[True]`'s quota error is fixed at c1 and it then fails on
  the prompt-guard defect, exactly as the lane predicted BEFORE testing. The
  test cannot reach green without the third defect fixed; it was never
  optional. A prediction stated in advance and then measured is the strongest
  evidence in this unit — require it in the report.
- c2's NEW RED: I cleared it as option 1; the **coordinator OVERRODE that on
  2026-10-25 and the reorder is required**. Commit structure is the
  coordinator's and I had flagged that it might override. Its two reasons beat
  mine: (i) disclosure in a commit message does not change what bisect sees —
  someone bisecting an unrelated regression across c2 meets a red that is not
  theirs and must read commit bodies to find out; (ii) making the first
  exception to a rule written the same night is how a rule dies. My cost
  argument was also overweighted: the fix is to move ONE test (the tree-wide
  drift guard) into c3 while c2 keeps the seam's own six tests — a mechanical
  rebase — and re-measurement is about a minute per commit with instruments
  the lane already has. ACTIONS, in order: pin
  `preserve/0.8.10-u45-pre-reorder` at the current tip FIRST; confirm the tree
  is clean (no uncommitted F1 work) before rebasing; move the drift-guard test
  into c3; then re-run the subset chain over EVERY commit through the tip,
  using `(id, signature)` pairs. Historical record of the superseded
  clearance:
  `tests/unit/contracts/test_prompt_guard_provenance_thaw.py` is `exit=1`,
  1 failed / 6 passed at c2 — the tree-wide drift guard
  `test_only_the_canonical_definition_enumerates_the_guard_keys`, red until
  c3's conversions land. Found only because of refinement 2 above; a subset
  check over pre-existing ids could never see it. Accepted because: it does not
  breach the coordinator's rule as written (the test was not green before c2,
  it did not exist); it is disclosed in c2's own commit message; the red is
  bounded and explicable; and the coordinator authorised committing it in that
  state. Rejected option 2 (split the file across c2/c3) on cost — the tip is
  now six commits, so a rewrite of c2/c3 restamps every later SHA and
  **invalidates the bisect measurement just completed**, which the lane had not
  counted. Commit structure remains the coordinator's call; it may override.
  The check still did its job: surfaced for judgement, not passed invisibly.
- A FOURTH defect, outside §10 and §8, fixed in passing and to be named as one
  in the report: `get_schema` handed out the cached record itself, with two
  mutable dicts on a process-lifetime cache — a live aliasing defect
  independent of F3's reuse. Do not let it be folded into "G6 copy isolation".
- MESSAGE CROSSING 2026-10-25: the lane acted on my option-1 clearance and
  recorded its rationale in the report BEFORE the coordinator's override
  reached it. Re-sent the override as operative, with an instruction to correct
  the report's commit-structure section and keep the superseded call as a
  record. CHECK AT THE GATE that the report reflects the override and not
  option 1 — a crossed instruction is exactly the kind of thing that survives
  into a hand-over because both parties think it was settled.
- REORDER DONE AND VERIFIED 2026-10-25. New tip `33737d863`; preserve ref
  `preserve/0.8.10-u45-pre-reorder` at `0674a3b60`. Content-neutrality proven:
  both refs have tree `aabf548c963f13ba700bee81e4d6c0f604d33316`, the diff
  between them is empty, and the control discriminates (tip-vs-base non-empty).
  c2 green at c2 (6 passed), c3 green at c3 (7 passed) — tree identity proves
  the endpoint did not move, the per-commit greens prove each intermediate
  state is sound; both are needed. `contronts` typo fixed in `8598d9066`, so
  the coordinator should NOT fix it again at cherry-pick. New SHAs: c1
  `44a97bcde` (unchanged), c2 `c43922dd2`, c3 `8598d9066`, c4 `6e12d739b`,
  c5 `358bcca8b`, c6 `33737d863`.
- THERE ARE FOUR RETENTION STAGES, not one: `clock.run("retention", ...)` at
  `composer/continuation_composition_initialization.py:381`,
  `composer/continuation_initialization.py:282`,
  `composer/continuation_title_application.py:132`,
  `composer/continuation_title_provider.py:168`. So check 13's "scoped to one
  retention stage" means one INVOCATION, and the signature list must say which
  of the four it covers and what happens at the other three. It also sharpens
  non-vacuity test (ii): across two DIFFERENT retention stages it proves the
  carrier is not keyed on anything stage-type-wide; across two instances of
  one stage it proves only instance isolation. Require the stronger form.
- CARRIER CONCURRENCY, established by reading not grepping: the stage body is
  sequential (`await _registered(...)` then `return await _consume(...)`). But
  this codebase uses no `asyncio.gather`/`TaskGroup` at all — `async_workers.py`
  dispatches into a **process-wide `ThreadPoolExecutor`** (`_SHARED_EXECUTOR`,
  "Threads in the process-wide worker pool") via `_submit_shared` /
  `run_required_sql_finish_once`. So the carrier can cross thread boundaries
  with no simultaneity. A plain dict is likely correct under CPython's GIL;
  require that reasoning STATED in the signature list, not left implicit. Note
  the adjacency to check 13's process-global-memo reject: a process-wide pool
  is nearby, so confirm the memo is not keyed on anything pool- or
  thread-level.
- THE CARRIER ARGUMENT, and the half of it that is the wrong property. The
  coordinator's expected wording: hops awaited sequentially so each executor
  future's completion orders writes before the next hop's reads, and the GIL
  makes the dict operations atomic. **First half sound** —
  `concurrent.futures.Future` locks in both `set_result` and `result()`, so the
  await establishes happens-before. **Second half does not cover the case**: a
  memo is CHECK-THEN-SET (read, conditional write, with `compute()` between),
  which is separate bytecodes with a thread switch possible, so GIL atomicity
  of each half does not protect the compound. The argument therefore has ONE
  support, not two: everything rests on the sequential-awaits half, and the GIL
  clause only keeps individual reads/writes indivisible so no torn state is
  observable. Reject any signature list that cites GIL atomicity as licence for
  "no lock needed" under concurrency — it is precisely backwards.
- Suggested (not required) and raised with both: `memo.get` for the hit, then
  `memo.setdefault(key, compute(key))` on the miss. `setdefault` IS atomic, so
  every caller gets the single winning object even under fan-out. This matters
  for G5's chosen "return the memoized object shared" branch — a plain
  check-then-set under concurrency can hand two callers equal-but-distinct
  frozen stacks, and any identity-dependent consumer diverges silently. The
  argument for it is durability: check-then-set is correct only while the hops
  stay sequential, and a fan-out added to one of those four callees later
  breaks it silently, whereas `setdefault` survives that.
- Report must say which of the four retention stages the carrier covers, and if
  fewer than four, why the others do not need it **with their measured
  budgets, not by assumption** (coordinator, 2026-10-25).
- OFFICER ERROR 2026-10-25 (third, and the costliest): **I applied enumeration
  discipline to check 12 and not to F3, and a regression reached the tip.**
  Check 12 says, of `deep_thaw`: "enumerate every caller that can carry guard
  provenance BEFORE the fix — co-location is a SCREEN, not a finding". F3
  converts a REGENERATED object into a CACHED one, which creates exactly the
  same obligation: enumerate every surface the cached object is reachable
  through, and test each. I instead accepted "G6 copy isolation tested" plus
  M6, and I endorsed the lane's "the reuse would have EXTENDED the blast radius
  to listings" framing without requiring the test that covered the extended
  surface. Astra then found `_field_summary` passing `field_schema.default`
  straight into `ConfigFieldSummary.default`: measured on
  `blob_fetch.allowed_content_types`, reaching a later listing and a later
  `get_schema` went False/False at base to True/True at the pre-c7 tip. Fixed
  in c7 `ed2297a82`.
  **GENERAL RULE for this gate and any future one: when a change turns a
  regenerated value into a shared or cached one, the test obligation is the
  ENUMERATED set of paths by which the value is reachable — not a sample, and
  not the one path the author was thinking about.** A claim about blast radius
  is a claim about a set; accepting it without the enumeration is accepting an
  untested set description.
- CROSS-CUTTING, not U-45's to fix: **Tier-1 `_StrictResponse` models are
  strict and closed but MUTABLE.** Measured at `ed2297a82`: six of the seven
  `_StrictResponse` bases in `src/elspeth/web/` (`auth/admin_routes.py`,
  `auth/routes.py`, `blobs/schemas.py`, `catalog/schemas.py`,
  `secrets/schemas.py`, `sessions/schemas.py`) are
  `ConfigDict(strict=True, extra="forbid")` with no `frozen`; the seventh
  (`execution/schemas.py`) had no `model_config` in the grep window, so it is
  unverified. Control: `frozen=True` IS used elsewhere under
  `src/elspeth/web/`, so the absence is a pattern rather than a codebase-wide
  oversight. Consequence: the inference "strict + `extra=forbid`, therefore
  safe to share" is wrong wherever one of these instances is cached or reused,
  because attribute REASSIGNMENT still works — which is precisely the vector
  Astra mis-stated and the lane corrected. Raised with the coordinator as a
  follow-up candidate beyond this unit.
- OFFICER ERROR 2026-10-25 (second of the night, same family): I searched
  `web/sessions/continuation_composition_initialization.py` for the retention
  stage when it lives in `web/composer/` — two files share that basename and
  the citation did not disambiguate — using a `gather|TaskGroup|create_task`
  pattern that returns 0 even against `async_workers.py`. A concurrency pattern
  that cannot find concurrency in the concurrency module was never a valid
  instrument. Lesson: when a basename is ambiguous, resolve the path before
  searching, and run the control BEFORE trusting a negative, not after.
- The six commits (PRE-REORDER, superseded, kept as record): c1 `44a97bcde`
  quota seed; c2 `36737f223` provenance thaw
  seam; c3 `fd1801473` enumeration collapse; c4 `43bfba18c` blob fixture;
  c5 `d8d4f0c75` diagnosis correction notes (docs only — the deliverable added
  mid-unit by check 11); c6 `0674a3b60` F3 catalog reuse + G6. All six SHAs
  are restamped by the c2/c3 reorder, so every measurement keyed to them must
  be retaken afterwards.
- c3 measures `{blob}`, 1 failing id — a subset of c2's `{blob}`, but an EQUAL
  set, so refinement 2 requires its one-line explanation. It is easy here
  (c3 is a pure refactor touching nothing the blob fixture needs) and it must
  still be stated, because the whole point of the rule is that "equal" is
  never self-explaining.
- Subset chain must cover EVERY commit through the tip, not just c1–c3 as
  originally specified; the tip has grown to six commits (`0674a3b60` = F3).
  c3 still owes its number: must be ⊆ {blob}, AND
  `test_prompt_guard_provenance_thaw.py` must be GREEN at c3, since c3 carries
  the conversions that satisfy the drift guard. Still red at c3 = the guard is
  not satisfied by the conversions and the fix is incomplete.
- Lane tip `43bfba18c`, 4 commits, as of 2026-10-25: all three §10 defects
  green (whole-file composition-init `exit=1, 3 failed / 23 passed` on base →
  `exit=0, 27 passed` on tip, the 27th being §10.5(5)'s kept negative case),
  M1–M5 mutation reds captured as real output. F1–F4 and B1–B6 still ahead.
- RED INTERMEDIATE COMMITS, derived (NOT measured) 2026-10-25, with the
  coordinator for a ruling: three of the four commits hold a red test, by
  construction rather than by mutation control. The unit fixes three defects in
  sequence sharing one test file; the blob test was red at base and its fix
  lands only in `43bfba18c` (142 lines in
  `test_continuation_composition_initialization.py`), while `44a97bcde` is the
  quota seed alone and `fd1801473` touches no test file. So the blob test is red
  at commits 1–3, and the prompt-guard error is live at commit 1 since its fix
  is in commit 2. Evidence grade: derived from `git show --stat` plus the lane's
  base measurement, NOT a per-commit suite run. To measure it: detached worktree
  at each of commits 1–3, the three base-red ids at `-n 0` (~20 s each). This
  collides with the coordinator's "squash any WIP commit holding a red test":
  three sequential fixes in one shared test file cannot each be green alone, so
  it is inherent to the unit, not a lane discipline failure.
- BEFORE any squash or rebase that drops commits: `preserve/0.8.10-u45-*`
  pinned at the then-current tip. The lane proposed squashing its four
  separable commits to fix a `contronts`→`contracts` typo in a commit message
  (`git commit --amend` is hook-blocked). I held it: the coordinator lands with
  `cherry-pick -x` and wants a commit list with source SHAs, so four coherent
  commits beat one, and a cosmetic typo is a poor reason to lose separability.
  One-vs-four is the coordinator's call, not the lane's or mine.
- CROSS-LANE, not U-45's — U-38 reds the masquerade gate on landing. Its own
  new file `tests/unit/web/composer/test_continuation_policy_settings.py` is
  absent at `ba4b25ee2`, added by `ea96096e1`, carries unadjudicated `getattr`
  sites and is NOT in `config/cicd/masquerade_baseline.yaml` (control: that
  baseline holds 121 `tests/` paths, so its silence is a real absence). The
  gate reports FOUR findings; raw grep finds FIVE occurrences — 76, 91, 103,
  127, 128 — with two nested calls on 91. So a reseed must run
  `elspeth_lints.rules.masquerade.seed_baseline` over the live tree, NEVER the
  reported line numbers, or 128 stays unadjudicated and the gate stays red
  after an apparent fix. Raised with the coordinator; U-38's own commit.
- WITHDRAWN 2026-10-25, do not re-raise: the lane's "`BlobRecord.created_by`
  is annotated `BlobCreator` but populated with a plain `str`" is not a defect.
  `contracts/blobs.py:79` is `BlobCreator = Literal["user", "assistant",
  "pipeline"]`, so `created_by="assistant"` is correct usage, and `:87` derives
  `BLOB_CREATORS` from `get_args(BlobCreator)` precisely so static and runtime
  cannot drift.
- 12 reds in `test_continuation_prompt_producers.py` and
  `test_continuation_message_families.py` are U-44b's, not U-45's —
  byte-identical FAILED sets on base and tip. One is `TypeError: 'mappingproxy'
  object does not support item assignment`, the same freeze/thaw family as
  U-45's third defect, so U-44b may hold a sibling of it. Passed to the
  coordinator for U-44b's owner.
- OFFICER ERROR, recorded 2026-10-25: I verified that the seed block is
  textually unconditional (true) and let that fuse with the lane's causal
  conclusion "so both params are red" (false) into one compound claim in this
  note, and repeated it to the coordinator, which ruled on it. The structural
  half was measured; the causal half was inherited. AGENTS.md § Claims Must Be
  Measured covers exactly this: state no conclusion by inference. Verifying a
  premise is not verifying the conclusion drawn from it — check which half of
  a compound claim the measurement actually reaches.

## Part 1 hand-over dispositions (officer, 2026-10-25 06:14 AEDT)

Written at lane tip `1b59c10a1` on `d7ad18596`, after red-team landed. The
coordinator asked for F3's disposition in this file before hand-over.

### F3 (red-team, low) — FIXED as c8, coordinator's option (i)

The c3 drift guard inspected only container literals, so two `or`-chain
enumerations in `continuation_message_family_builders.py` (3 keys at :161,
4 at :232) were invisible while the gate reported zero.

Verified independently, not taken from the lane's report. I extracted the
`_duplicate_enumerations` function from each era's test file and ran both
against each era's production file, so the known-positive is real production
code at the exact lines the coordinator named rather than a synthetic snippet:

| detector | vs prod @c7 (or-chains live) | vs prod @c8 (derived) |
|---|---|---|
| c7, literal-only | `[]` — the evaded zero | `[163]` |
| c8, literal+BoolOp | `[161, 232]` | `[]` |

The off-diagonal cell was not planned and corroborates the lane's account of
the second gap it found while writing the fix: `[163]` is the exclusion
literal, which the pre-exemption rule flags and the exemption now clears.

c8's own control gained four shapes (membership chain; derived form; two-key
exclusion set; the same literal standing alone, still caught). That answers
the family-1 criticism the lane raised against itself: its original positive
control used a `frozenset({...})` literal, the one shape it was already
looking for.

### F4 (officer, low, OPEN) — the tree-wide exemption is file-scoped

Raised by me at this gate, from asking what c8 does NOT establish: that its
two shapes are the only ways to enumerate these keys by hand. That is a claim
about a set, so I sieved for it with a deliberately shape-blind instrument
(any statement naming >= 2 distinct guard keys, which also catches `==`-chains,
dict literals and repeated `.get()` calls — over-approximating on purpose so
triage is mine and not the detector's).

Eight statements across 18 files. Six are legitimate: the canonical definition
at `prompt_guard.py:32`, the exempted derived site at
`continuation_message_family_builders.py:162`, and three message-CONSTRUCTION
sites (`advisor_checkpoint.py:510`, `pipeline_planner.py:3489`,
`prompts.py:690`) where separate messages each receive their own provenance
key — adding a fifth key would not touch them.

The remaining three are `prompt_guard.py:172/176/180`. c8's detector SEES them:
run tree-wide it flags `[33, 172, 176, 180]`, all in that one file. They are
invisible to the gate because the test exempts by path, not by line:

    if lines and path != _CANONICAL_DEFINITION:

Measured, with a planted known-positive. `PLANTED_KEYS = frozenset({TRUST,
CONTENT_PARTS})` appended to a copy of that file:

    detector found      : [33, 311, 172, 176, 180]   <- 311 is the plant
    path == canonical?  : True
    REPORTED as offender: False

Control: the identical planted file at any other path reports `True`. So the
exemption admits arbitrary NEW enumerations in the one file where the keys
live, not merely the definition that requires one.

Why I read it as live rather than cosmetic. Those three clauses are three
copies of one rule — `X in message and (value is None or TRUST in message)
-> raise` — over the three partition keys, each with its own accumulator and
its own audit message. A fifth partition key would be stripped from the wire
by the derived comprehension at :183 and then have NO conflict check, with the
gate green. That is the guard docstring's own sentence: the one that is missed
fails open. And `test_only_the_canonical_definition_enumerates_the_guard_keys`
asserts by name something stronger than it checks.

Recommended disposition, as c9 before hand-over: narrow the exemption from the
file to the canonical assignment, and derive the three clauses from
`PROMPT_GUARD_PROVENANCE_KEYS - {PROMPT_GUARD_TRUST_KEY}` with a per-key
message map so the three audit strings survive byte-for-byte. If the lane
judges they must stay separate, the exemption still narrows to line 33 and the
three get an explicit allowance naming why. Severity stays low: there are four
keys today and all three partitions are covered, so nothing is currently
fail-open. Sequencing (part 1 c9 vs part 2) is the coordinator's call; my
recommendation is part 1, because the guard is a part-1 artifact and this is
the second instance of its claim exceeding its enforcement.

### Census wording — pin verified; provenance is testimony, not measurement

Red-team's report calls it "the hand-edited `soft-mapping-census.yaml`". The
lane disputes the wording and says c2 (`303286be6`) re-pinned it with
`scripts/check_contracts.py --write-census`. The distinction matters because a
hand-edited pin is a forbidden practice, so the record should not carry the
phrase unchallenged.

What I measured, at the tip, with the real instrument in its default read-only
mode (`write_census` is reached only under the pin flag, and `git status`
confirmed the pin untouched by the run; `elspeth.__file__` resolved inside the
worktree):

    exit=0
    Soft-mapping census matches config/cicd/soft-mapping-census.yaml:
    2740 soft occurrences across 435 files, 60 boundary-parsed

The committed file also carries the tool's own `GENERATED by ... --write-census`
banner, and the c2 diff is +1/+1 on `prompt_guard.py` with all three totals
moved consistently — the shape of a re-pin, which a hand-edit would have to
reproduce across four places.

Stated precisely, because this is exactly the compound-claim trap: the pin
MATCHES the tool's recomputation, which is the substantive question and is
measured. WHO typed it is not recoverable from the artifact, and I am not
offering an independent confirmation of the lane's account of its own keystrokes
— only that the artifact is what the tool would have produced.

### Rebase verification (officer, independent of the lane's report)

- All eight patch-ids identical and in the same order across the rebase.
- Net diffs share one sha256 (`a282e970...`), 1087 lines each; `cmp` exit 0,
  and a one-byte mutation makes `cmp` exit 1.
- `d7ad18596` is an ancestor of the tip; `ea96096e1` is not.
- Touched-file overlap against the base-to-target set: 0 of 27.
- No `.claude/`, user-home, `.db` or `.log` path among the thirteen files.

Net-diff identity and the patch-id chain are NOT the same check: endpoints
constrain the sum, per-commit patch-ids constrain each term, so the pair rules
out a squash or reorder that preserved the aggregate.

The 18-vs-27 discrepancy between my earlier figure and the lane's is settled by
measurement: `ea96096e1..60953a830` is 18 (variant against variant),
`ea96096e1..d7ad18596` is 27 (the whole base-to-target range). The lane's is
the right denominator, so the no-conflict guarantee is the broader one — not
merely that U-38's divergence cannot touch this work, but that nothing landed
between the two bases can.

### Officer errors five and six, both instrument failures, both caught by controls

FIVE — a known-positive I assumed instead of measuring. I controlled the
overlap-zero claim by planting `continuation_settings.py` into my file list,
expecting it to appear in the U-38 divergence set. It is not in that set, so
the control returned 0 when it had to return 1. Identical in cause to the
lane's `frozenset` control failure, in the same hour: a control assembled from
an assumption inherits the assumption. Taking the known-positive from
`head -1` of the measured target set costs nothing and cannot be wrong.
Rebuilt: clean 0 / planted 1 / self 13.

SIX — a re-derivation that disagreed with the pin for two reasons, one of them
plausible. My own census harness reported `870 per-file disagreements` (its
live keys were absolute paths, the pin's relative — every file "differed", and
the control returned 870 with AND without a planted mutation, so it never
discriminated) and `soft 2734` against a pinned `2740`. The second number is
the dangerous one: a six-site drift is believable and would have read as a
finding. Cause was `build_census(src_dir)` called without the `alias_index`
the real checker builds, so six alias-resolved sites went uncounted.

The lesson that is new, and belongs with the three families: an instrument
wrong by a BELIEVABLE margin is more dangerous than one wrong by an absurd
one. `870` announced itself. `2734` would not have. When a re-derivation
disagrees with a pin by a small number, suspect the re-derivation first — and
prefer running the project's own instrument read-only over rebuilding it.

### Part 1 blocking set as it stands

1. F4 disposition — c9, or a justified narrowing. OPEN, with the lane.
2. Keyless set-diff re-run against `d7ad18596` (the earlier 0/0 was
   base-relative). Was in flight when the lane's session hit its limit.
3. Pair chain over the restamped SHAs `b00d48f28 … 1b59c10a1`. Same.

Items 2 and 3 must be re-run at the tip if c9 lands, which is why F4 went to
the lane immediately rather than after this file was written.

Lane session hit its usage limit at ~05:45 AEDT; the 06:00 reset has now passed,
so it should be resumable. F4 is in its inbox. Nothing further should start
until it resumes or the coordinator re-sequences.

## COLD START (paused 2026-10-25)

Written by the accountable officer for U-45 at the maintainer's pause, per
`.claude/lanes/release-0.8.10/PAUSE-PROTOCOL.md` § 3. This is the GATE's view:
what has been independently verified, what has not, and what a resumer must
re-measure rather than trust. The lane's own notes are in `u45-fix-report.md`.

### Unit, branch, base

- Unit: U-45, first-Compose retention `TimeoutError`. Part 1 = the fix.
  Part 2 = the per-stage memo. I am the officer, not the implementer.
- Lane branch `codex/0.8.10-u45-fix` @ **`1b59c10a1`**, plus an uncommitted c9
  that becomes a `wip(...)` commit at the pause.
- Worktree: `.claude/worktrees/u45-fix-20261025`.
- Base: **`d7ad18596`** (U-38 land tip on `release/0.8.10-assembly`). The
  superseded U-38 variant `ea96096e1` is NOT in the history and must not return.
- Pre-rebase preserve ref: `preserve/0.8.10-u45-pre-u38-rebase` @ `f824d69bd`,
  eight commits on `ea96096e1`.
- Nothing is pushed. Nothing is landed on the target.

### Verified by me, with the command to re-measure it

My harnesses lived in a session scratchpad that does not survive this pause, so
the reproductions are written out here rather than referenced.

**1. The rebase preserved the work, commit by commit.**

    git rev-list --reverse d7ad18596..1b59c10a1 \
      | while read s; do git show "$s" | git patch-id --stable | cut -c1-16; done
    # must equal the same list over ea96096e1..f824d69bd
    git diff ea96096e1 f824d69bd > /tmp/pre.diff
    git diff d7ad18596 1b59c10a1 > /tmp/post.diff
    cmp /tmp/pre.diff /tmp/post.diff          # exit 0; sha256 a282e970..., 1087 lines
    git merge-base --is-ancestor d7ad18596 1b59c10a1   # exit 0
    git merge-base --is-ancestor ea96096e1 1b59c10a1   # exit 1

Net-diff identity and the patch-id chain are different checks: endpoints
constrain the sum, per-commit ids constrain each term. Run both.

**2. F3 (red-team) is fixed.** Extract `_duplicate_enumerations` and
`_guard_key_names` from each era's test file with `ast`, exec them into a
namespace holding `{"ast": ast, "Iterable": Iterable}`, and run each against
each era's `continuation_message_family_builders.py`:

    c7 detector vs prod@c7 -> []          <- the evaded zero
    c7 detector vs prod@c8 -> [163]       <- the exclusion literal
    c8 detector vs prod@c7 -> [161, 232]  <- known-positive on REAL code
    c8 detector vs prod@c8 -> []

NOTE for a resumer: the test file has since grown `_canonical_assignments` and
`_canonical_definition_literals`, so an extraction that picks only the two
original helpers now raises `NameError`. Extract every module-level
`FunctionDef`, not a fixed list.

**3. F4 (mine) is the reason c9 exists.** The c8 tree-wide test exempted by
PATH, so the detector's four hits in `contracts/prompt_guard.py`
(`[33, 172, 176, 180]`) were all discarded. Reproduce by appending
`PLANTED_KEYS = frozenset({PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY})`
to a COPY of that file and applying the old rule `path != _CANONICAL_DEFINITION`:
the plant is detected and not reported, while the identical file at any other
path is reported. That control is the acceptance test for c9.

**4. The census pin is correct.** From the worktree, read-only:

    PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src \
      .venv/bin/python <wt>/scripts/check_contracts.py
    # exit 0; "census matches ... 2740 soft occurrences across 435 files"

`write_census` is reached only under `--write-census`; `git status` confirmed
the pin untouched by the run.

**5. Commit hygiene.** Thirteen touched files, no `.claude/`, user-home, `.db`
or `.log` path among them. Touched-file overlap against the base-to-target set
is 0 of 27.

### In progress

c9, uncommitted in the lane worktree at the pause: `M
src/elspeth/contracts/prompt_guard.py`, `M
tests/unit/contracts/test_prompt_guard_provenance_thaw.py`.

It meets three of the coordinator's four requirements. The exemption is
node-scoped (`_canonical_assignments` + `_canonical_definition_literals`), the
three partition clauses are derived from a new `PROMPT_GUARD_PARTITION_KEYS`
with completeness tests, and the misleading test name became
`test_no_module_enumerates_the_guard_keys_outside_the_canonical_assignment`.

The lane deviated from the specified per-key MESSAGE MAP, and was right to: a
dict literal naming the keys would have reintroduced the same enumeration in a
shape the detector does not inspect, since it checks `Set`/`Tuple`/`List` and
`ast.Dict` exposes `.keys`, not `.elts`. Measured: the dict form returns `[]`
from the c8 detector while the identical keys as a set literal are flagged.

### Rulings RECEIVED from the coordinator (2026-10-25, at the pause)

Both items that stood open when I first wrote this section have been ruled.
Nobody acts on either until 0.8.10 resumes.

**1. Byte-identity: the derived message is ACCEPTED** in place of the
byte-for-byte requirement. The reasoning is the measurement below: keeping the
three strings byte-for-byte needs a per-key map, a dict literal of the keys is
invisible to the detector (`ast.Dict` exposes `.keys`, not `.elts`), so it would
restore the very enumeration F4 closed in a shape the gate cannot see. The old
three were never one template either, and this is pre-release under the
no-tech-debt policy. Two conditions attach:

  (1) a measured search for each retired string across `src/`, `tests/`,
      `docs/` and any export or redaction fixture, controlled with a string
      known to be present; any consumer is updated in c9 or reported.
      **DONE — see below.**
  (2) the pair chain's transition into c9 must EXPLAIN every signature that
      changes because of the new message, naming this cause for each removed
      and each added pair. **NOT DONE — it belongs to the pair chain and is a
      landing condition, not a code change.**

**2. `PROMPT_GUARD_PARTITION_KEYS` supersedes the specified dict message map:
CONFIRMED.** Measured: the dict form returns `[]` from the detector while the
identical keys as a set literal are flagged.

**Condition (a), measured by me at `c26167c4d`.** Zero consumers. Each of the
three retired strings returns 0 files across the whole worktree:

    "Prompt guard content partition conflicts"      -> 0 files
    "Prompt guard tool argument partition conflicts" -> 0 files
    "Provider thinking partitions conflict"          -> 0 files

A looser sweep for the old shapes (`content partition conflicts|tool argument
partition conflicts|thinking partitions conflict`) also returns nothing, and no
fixture, golden, snapshot, expected, export or redaction file mentions
prompt-guard at all.

Controlled both ways, because three zeros are exactly the result a broken
search returns:

    CONTROL A, a string known present ("Prompt guard partition ")
      -> HIT src/elspeth/contracts/prompt_guard.py
      -> HIT src/elspeth/web/composer/prompt_guard.py
    CONTROL B, a string known absent -> no hit
    SCOPE PROOF, files mentioning "Prompt guard" per tree
      -> src 5, tests 2, docs 2, config 0, examples 0

So nothing needs updating in c9 for ruling condition 1, and there is no consumer to
report.

LIMITS of that search, stated because the claim is a negative: it covers the
repository at this tip only. It does NOT cover audit rows already persisted in
any Landscape store, which still carry the old strings as data — changing the
message does not rewrite history, so an operator grepping historical audit rows
for the old text will still match it. It also does not cover other branches or
the dev box.

### Next step, in order, under the capacity caps

Capacity is binding: ONE pytest per lane, at most `-n 3`, every run wrapped in
`timeout` (<=2400 s), directory-wide selections need a coordinator slot
(`AGENT-BRIEF.md:63`).

1. **c10 — F5. Ruled into part 1 by the coordinator (2026-10-25), do this
   first.** Extend `_duplicate_enumerations` to inspect `ast.Dict` KEYS. I
   prototyped it before the pause so the size is measured, not estimated:
   insert one branch beside the Set/Tuple/List and BoolOp branches, collecting
   `[k for k in node.keys if k is not None]` and applying the same
   `id(node) not in exempt` and `>= 2` rules. Measured with that branch in
   place: 0 sites flagged across `src/` (so NO new exemption is needed and c9's
   zero is preserved), a dict-keyed plant goes red, and four shapes that must
   not regress stay clean — a list of dicts with one guard key each (the real
   message-construction shape), a single-key dict, a dict
   comprehension, and a dict with `**` unpacking (where `node.keys` holds
   `None`; `_guard_key_names` already filters it, so the explicit filter is
   defensive rather than required). `ast.DictComp` needs NO coverage: a
   comprehension can only enumerate by hand via a literal iterable, which the
   existing Set/Tuple/List branch already catches. Add the dict-keyed plant to
   `test_the_duplicate_enumeration_detector_discriminates` beside the five
   shapes already there.
   Then c9 is COMPLETE and committed as `c26167c4d` — not WIP, and both rulings
   that gated it have been given. F4's acceptance criteria are verified at that
   tip: the tree-wide detector reports 0 across `src/` with NO file exemption,
   and a planted enumeration in the canonical file is reported (`[318]`) while
   the genuine definition is not (`[]`). Nothing is owed on c9 itself, so the
   only code in this step is c10.
2. Keyless set-diff against **`d7ad18596`** — the earlier 0/0 was base-relative
   and does not carry over:

       ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
         elspeth-lints check --rules all --root src/elspeth

   Compare finding SETS on base and tip, added and removed. Never against zero.
3. Pair chain over **c1 through c10** (coordinator, at the F5 ruling), which
   means `p2` captures at c8 (`1b59c10a1`), c9 (`c26167c4d`) and c10 — none of
   which exists yet; the chain on disk is complete c1–c7 only. Sequential, one
   capped run per transition — not a fan-out. Compare sets of
   `(test id, failure signature)` pairs, not counts.
4. Affected-file sweep, **LAST** — the lane's ordering and it is right: c10
   edits a file the sweep covers, so running it earlier measures a tree that is
   about to change. Under the capacity caps, and it must include
   `test_continuation_message_family_builders.py`, the module c3 and c8 edited
   and the one with no run behind it.
   The transition INTO c9 (`1b59c10a1 -> c26167c4d`) carries coordinator
   ruling condition 2: every signature that changes because the three partition
   messages became one derived message must be named with that cause, each
   removed and each added pair. A changed signature under an existing id is
   exactly what the landing rule requires be explained rather than counted.

### Hazards — things that look true but are not

- **Do not "fix" the census.** My own re-derivation produced `soft 2734`
  against the pinned `2740` and `870 per-file disagreements`. Both were my
  instrument: absolute-vs-relative dict keys, and `build_census(src_dir)`
  called without the `alias_index` the real checker builds. The pin is right.
  An instrument wrong by a believable margin is the dangerous kind.
- **18 vs 27 touched files are both correct.** 18 is `ea96096e1..60953a830`
  (variant against variant), 27 is `ea96096e1..d7ad18596` (base to target).
  The overlap claim belongs against 27.
- **A tree-hash comparison after a rebase does not discriminate** — the hash
  must change whether the work survived or a superseded patch was re-applied.
  Use patch-ids.
- **A green `pytest tests/` says nothing about PostgreSQL.** The default
  selection deselects `testcontainer`. This work touches session persistence.
- **KNOWN HANG:** `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`
  and the web-landscape fencing file. Two runs stalled 57 and 94 min. `--ignore`
  both. The brief names u45-firstcompose as the one exception, for U-46
  evidence — confirm that applies before relying on it.
- **Flaky under parallelism:** `e2e/recovery`, `integration/pipeline`,
  `unit/engine/orchestrator`. Re-run a red id at `-n 0` before attributing it.
- **Three officer instrument failures are recorded above**, and two share one
  cause: a control built from an ASSUMED known-positive inherits the
  assumption. Take the known-positive from the measured target set.

### Part 2 scope, unchanged by the pause

Mechanism (d): an ambient ContextVar carrier plus a per-stage memo keyed on the
complete exact input, at the four `clock.run("retention", ...)` sites. `get` +
`setdefault` is mandatory — a bare `setdefault(key, compute(key))` evaluates
the computation on every call, which defeats the memo. The carrier's safety
argument rests on sequential awaits, NOT on GIL atomicity: a memo is
check-then-set, which the GIL does not make atomic.

**Out of scope, and must not be pulled in:** the 6.6 s continuation evidence
read. design-runtime measured it as `restore_turn_evidence`'s
`CONTINUATION_READ_SQL`, which has no production caller yet, costing 5 link
checks x 2 graph decodes INSIDE a single read. A memo collapses N identical
reads to one and cannot make the first cheaper, so (d) does not touch it. The
coordinator approved a producer-side fix as its own unit after U-35b — a frozen
`AdmittedTurnEvidence` parsed once, no cache — tracked as U-38c under
design-runtime.

### Addendum after the lane's closeout (officer, 2026-10-25 06:45)

**F5, NEW, latent, NOT fixed — the detector never inspects `ast.Dict` keys.**
A mapping literal keyed by two guard keys is a third evasion shape beside
red-team's container literal and my or-chain. I measured this independently
before the ruling (a dict message map returns `[]` from the detector while the
identical keys as a set literal are flagged), but recorded it only as the
REASON the specified dict map was the wrong instrument. The lane is right that
it is a standing gap in its own right, so it is logged here as a finding.
Production has no such literal today, so it is latent, not live. Fix is about
two lines in `_duplicate_enumerations` plus the control that already exists in
`test_the_duplicate_enumeration_detector_discriminates`. Deferred by the pause.

**Coordinator ruling condition 2: impact measured nil on the captured chain, and
PREDICTED nil at c9 — confirm, do not assume.** Pair signatures live in
`.claude/worktrees/u45-bisect-20261025/.claude/lanes/u45-fix/p2-*.txt`, 11
files, 15 signature lines total. Measured by parsing every exception-message
string literal out of `contracts/prompt_guard.py` on both sides of c9 and
cross-referencing each against those signatures:

    messages before c9: 24      after c9: 22
    REMOVED by c9 (3)  -> none appears in any captured signature
    ADDED by c9   (1)  -> "Prompt guard partition {…} conflicts with whole-message provenance"
    UNCHANGED and PRESENT in a signature:
      "Prompt guard content partition is not an owned producer identity"

That last line is what makes the negative meaningful rather than a bare zero:
an `AuditIntegrityError` from this same file DOES reach the captured
signatures, so the instrument is demonstrably capable of showing a message of
this class, and the three retired ones still do not appear. Controls both ways
passed (a known-present 60-char probe matches, a known-absent string does not).

LIMIT, and it is the whole reason this is a prediction: **there is no `p2`
capture for c8 (`1b59c10a1`) or c9 (`c26167c4d`)**. The chain is complete c1
through c7 only. Nothing has ever been run for pairs at the two commits whose
transition ruling condition 2 is about. So the falsifiable prediction for the
resumer is: capture pairs at c8 and c9, and the added and removed sets for
those two transitions should contain NO pair whose signature carries any of the
three retired messages or the new derived one. If one appears, ruling condition 2
applies to it and must be explained with that cause. If one appears that is
NOT explained by the message change, stop. That is a real regression, and the
rule it breaks is **landing clause (a)** (`AGENTS.md:246-247`: any pair new
under an existing id must be explained) — NOT ruling condition 2, which only
obliges someone to explain the deltas the message change causes.

**The lane closed the second half of F4, which I had not asked for.** My
finding was that a fifth partition key would be stripped from the wire with no
conflict check. Deriving the loop closes that. But a fifth key would then be
admission-checked AND stripped, yet accumulated, validated and carried
nowhere — a different hole. `test_every_partition_key_has_a_partition_field_to_be_carried_on`
compares the partition-key count against the `*_parts` field count on
`DetachedPromptGuardEvidence`, both sides derived. M10 (planted fifth key) goes
red on that gate ALONE while both refusal gates pass, which separates the two
halves rather than conflating them; M11 (loop narrowed to two hard-coded keys)
goes red on both behavioural gates and the detector.

**Not verified at c9, stated plainly.** The 41-file focused sweep never
completed — the lane stopped it, correctly: it was out of compliance at `-n 12`
and was measuring a tree still being edited, so it could not have been
evidence. Stopping it took available memory from 9 GB to 24 GB. What IS
verified at c9: the full pre-commit chain (mypy, Check Contracts, elspeth-lints
contract invariants), a failing-first capture (`1 failed, 7 passed` naming
`{'contracts/prompt_guard.py': [172, 176, 180]}` -> 12 passed, with line 33
absent from the red, so one run proved both halves), and my own two acceptance
checks at the tip. There is NO broad test run at c9. That is a landing
condition, not a defect.

**`web/composer/prompt_guard.py:226-230` is clear for a different reason** and
needs no follow-up: the detector measures `[]` there, and `_request` takes the
three partitions as typed keyword parameters, so a fifth kind cannot reach it
without a signature edit. Do not "fix" it by analogy with c9.

### Naming collision — I created it; corrected 2026-10-25 07:40

Not an inherent ambiguity. **The coordinator numbered its conditions (1) and
(2). I relabelled them (a) and (b) in this file**, which collided with the
`AGENTS.md` landing rule's lettered clauses, and the relabelling then
propagated into the lane report. Corrected at nine sites here. The convention,
which cannot collide even if abbreviated:

| name | source | satisfied by |
|---|---|---|
| **ruling condition 1** | coordinator, numbered | the controlled search for the three retired strings — DONE, zero consumers, two independent searches |
| **ruling condition 2** | coordinator, numbered | the c8/c9/c10 captures, with each changed signature named to the new-message cause |
| **landing clause (a)** | `AGENTS.md:246-247` | failing pairs a subset of the previous commit's, and any pair new under an EXISTING id explained — `chain3.sh`'s transition tables |
| **landing clause (b)** | `AGENTS.md:248-250` | every id a commit INTRODUCES passes at that commit — the five new c9 ids by name, plus the rename as a disappearing id rather than a red |

The error that hid inside the collision, caught by the lane: "a regression
wearing condition (b)'s clothes" describes an unexplained ADDED PAIR, which is
landing clause **(a)**. Clause (b) is only about ids a commit introduces. Both
documents said (b). Verified at `AGENTS.md:246-250`, with the checkout,
`d7ad18596` and `c26167c4d` all hashing identically over those lines, so this
was not the stale-worktree hazard.

The lesson is narrower than "disambiguate your labels": **adopting a label
without checking what it points at defeats the disambiguation.** My note would
have resolved the collision and preserved the mistake inside it.

### Addendum 2 after the lane's second closeout (officer, 2026-10-25 07:00)

**F5 is RULED into part 1 as c10**, not deferred. Spec and measured sizing are
in step 1 of the next-step list above. Condition (b) stays recorded as written,
a prediction that can fail, now covering c8, c9 and c10.

**The lane's structural argument for ruling condition 2, and its limit.** The lane
offers a better reason than my empirical zero: a pair signature exists only for
a FAILING test, so a message raised only into a `pytest.raises` never enters the
chain at all. That is sound and it explains WHY the prediction is expected to
hold. It must not harden into "the captures are unnecessary". The structural
argument is conditional on no test ever hitting that raise UNEXPECTEDLY — and a
test failing with one of these messages where none did before is precisely the
regression ruling condition 2 exists to surface. A check whose success value cannot
distinguish the good case from the bad is not a check; keep the captures.

**A real coverage gap the lane found in its own notes.** Its "150 passed" row
covered exactly `test_prompt_guard_admission.py`,
`test_prompt_guard_content_parts.py` and `test_prompt_guard_producer_parts.py`.
It did NOT cover `test_continuation_message_family_builders.py` — the module c3
and c8 actually edited — which was inside the sweep that got stopped. c9 does
not touch that module so it is expected to pass, but expected is not shown.
Covered by the affected-sweep re-run.

**A distinction the lane drew that corrects my framing too.** Its pair-chain
and no-pins measurements addressed the consequence I raised (the landing rule's
signature comparison). They did NOT address what the coordinator's ruling
protected, which is the byte-identity of operator-facing audit output. Those are
two different concerns that happened to touch the same three strings, and the
measurements speak only to mine. Worth keeping separate on resume: an operator
grepping historical audit rows for the old text is unaffected by any pair-chain
result.

**Landing conditions owed, final list at the pause.** All three are now blocked
on cold start rather than on any decision: (1) keyless set-diff against
`d7ad18596`; (2) the pair chain c1–c10 with the three missing captures; (3) the
affected-file sweep re-run after c9 and c10, under the capacity caps. Plus c10
itself, which is code.

### Addendum 3 — c10's blast radius, enumerated (officer, 2026-10-25 07:20)

Both this gate and the lane report characterised the safety of adding an
`ast.Dict` arm by naming three message-construction files. The two lists were
DIFFERENT — mine `advisor_checkpoint.py`, `pipeline_planner.py`, `prompts.py`;
the lane's `continuation_message_builders.py`, `composer/service.py`,
`tool_batch.py` — and neither was wrong. Both were partial samples, picked by
two different criteria. My own rule applies: a claim about blast radius is a
claim about a SET, so here is the set.

Dict literals carrying at least one guard key, measured at `c26167c4d`:

    web/composer/tool_batch.py                            15 literal(s)
    web/composer/pipeline_planner.py                      12
    web/composer/prompts.py                                5
    web/composer/advisor_checkpoint.py                     2
    web/composer/continuation_message_builders.py          3
    web/composer/session_tool.py                           3
    web/composer/continuation_message_family_builders.py   1
    web/composer/provider_thinking.py                      1
    web/composer/service.py                                1
    web/sessions/_auto_title.py                            1
    --------------------------------------------------------
    44 literals across 10 files; MAX GUARD KEYS IN ANY ONE LITERAL = 1

    dict literals anywhere with >= 2 guard keys: 0
    CONTROL: the same scan on a planted 2-key dict -> [1]

**Independently confirmed.** The lane wrote its own enumerator
(`.claude/lanes/u45-fix/dictscan.py`) and reproduced the whole set: same 44, same
10 files, same per-file counts including the 15 in `tool_batch.py` and the 12 in
`pipeline_planner.py`, same max of 1, same zero, same planted-dict positive.
Two independently written enumerators agreeing on the entire set is the
strongest form this claim takes.

**Why the earlier three-file lists were partial, which is the reusable part.**
The lane's pattern was `grep -rn "{PROMPT_GUARD_[A-Z_]*KEY:"` — anchored on the
brace, so it could only match a literal whose FIRST key is a guard key, and
every `{"role": ..., GUARD_KEY: ...}` site was invisible to it. The
instrument's shape chose the sample, and a safety conclusion was then written on
that sample. This is the one failure mode tonight that fails on a POSITIVE: it
returned hits, so no suspicious zero appeared and the
negative-or-unchanged trigger never fired. Note also that two brace-anchored
greps agreeing on three files each would have READ as corroboration and been
worth nothing — the two lists differing is what exposed it.

So c10's arm is safe, and the reason is now 44 enumerated literals rather than
six file names. Four of those files (`continuation_message_family_builders.py`,
`provider_thinking.py`, `session_tool.py`, `_auto_title.py`) appear in neither
of the earlier lists. The pattern is uniform and deliberate: one message, one
provenance key, which is why a fifth key extends the derived set rather than
requiring an edit at 44 sites.

### Two hashes of the same five lines (officer, 2026-10-25 08:00)

The gate and the lane report both cite a digest for `AGENTS.md:246-250` and the
digests DISAGREE: `3a55897a32bfedad` here, `3c85d98474420bc9` there. Not a
contradiction, and recorded so nobody reads it as one. Mine is md5, the lane's
is sha256 of the identical five lines:

    md5    -> 3a55897a32bfedad
    sha256 -> 3c85d98474420bc9

Neither of us said which function we used, so each published a label without
its referent — the exact failure we had just finished correcting, in our own
evidence for the correction.

The content question is settled by something better than either digest:

    diff <(git show d7ad18596:AGENTS.md | sed -n '246,250p') <(sed -n '246,250p' AGENTS.md)  -> IDENTICAL
    diff <(git show c26167c4d:AGENTS.md | sed -n '246,250p') <(sed -n '246,250p' AGENTS.md)  -> IDENTICAL

A `diff` needs no shared convention between the two parties, so it cannot
disagree for a reason that is not about the content. Prefer it over a digest
whenever two parties must compare the same thing.

### Whole-file diff adopted for the landing-rule citation (officer, 08:20)

The lane proposed replacing my five-line range check with a whole-file diff of
`AGENTS.md` across the three refs. Adopted — but not for the reason given, and
the distinction matters for anyone reusing either form.

Its stated reason was that hashing `246,250p` across refs ASSUMES those line
numbers hold the same clauses in every ref. Measured, that assumption is
verified rather than latent: inserting one line above the clauses so they move
246->247 makes the extracted text differ, and the range diff reports DIFFERS
with the shift visible. A line shift cannot pass it.

The whole-file form is still better, for a different reason: it bounds the
ENTIRE file, so it also covers the landing rule's surrounding context — the
run-varying-data normalisation guidance and the squash/reorder rule — which the
citation leans on and which a five-line window would not see change. It also
needs no range bookkeeping, so it cannot go stale when the file is edited.

    diff <(git show d7ad18596:AGENTS.md) AGENTS.md  -> IDENTICAL
    diff <(git show c26167c4d:AGENTS.md) AGENTS.md  -> IDENTICAL
    CONTROL: append one byte -> DIFFERS

Both digests are named with their functions in both documents (`md5`
`3a55897a32bfedad`, `sha256` `3c85d98474420bc9`), recorded as the near-miss
rather than as the evidence.
