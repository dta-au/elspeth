# Runbook: adversarial fix/review loop

A repeatable process for taking a bug (or a cluster of them) from ticket to
committed fix, using parallel subagents with adversarial review.

Derived from the 2026-08-05 session that produced seven commits across five fix
units. Its results are the reason the standing rules below are phrased as
absolutes: **three of five tickets rested on false premises**, **two of three
fixes that landed mid-session carried regressions**, and **the most valuable
output of three units was work that got reverted**.

Scale the fleet to the work. A one-file bug needs one fixer and one reviewer,
not a panel. The rules apply at every size; the agent count does not.

---

## The paste-ready prompt

Replace `<TICKET>` and delete the tiers you do not need.

> Work `<TICKET>` end to end: investigate, fix, review adversarially, and land it
> on the release branch. Do not trust the ticket's premise — reproduce the defect
> yourself before writing any fix, and if the premise is wrong, say so and correct
> the ticket rather than fixing what it describes.
>
> **Phase 1 — reproduce and challenge.** Reproduce the symptom the ticket
> describes, at the layer it describes it. State what you verified and what you
> are taking on trust. If the ticket names a mechanism ("X never happens",
> "these N sites need fixing"), search for the *capability* by mechanism rather
> than checking the names given — a list verified against itself finds nothing.
>
> **Phase 2 — fix.** Brief the invariant, not the site: state what must be true,
> and let the investigation find where that lives. Write the failing test first
> and show it failing. The test must assert the **symptom the ticket described**,
> not the mechanism you changed.
>
> **Phase 3 — adversarial review.** Hand the diff to a reviewer whose job is to
> break it, with an explicit instruction that "I found nothing" is only valid
> alongside a list of what was tried. Iterate until the reviewer has nothing
> left. Reverting the fix is a valid outcome.
>
> **Phase 4 — reconcile.** Record `HEAD`, run the full suite, re-check `HEAD`
> (if it moved the run is void), then commit file-scoped per unit. Read the
> suite's tail, not its exit code.
>
> Report what you corrected along the way, not just what shipped.

For multi-unit work, add:

> Split the work into single-concern units with **disjoint file ownership**.
> Give each fixer an explicit owned list and an explicit off-limits list naming
> the files other agents hold, and require it to *stop and report* rather than
> edit outside its set. Assign **files, not directories**.

For a large or contested change, add:

> After all units are green, convene a panel with distinct remits — design
> coherence, the language/mechanism, the test suite, and the pattern across the
> defects. Give each a specific thing to try to break. Do not let them
> duplicate each other.

---

## Standing rules

These are the ones that caught real defects. Each is phrased as the failure it
prevents.

### On premises

**Absence of one mechanism is not absence of the capability.** `grep -c
track_operation sink.py` returning 0 is true and useless — sinks open
`sink_write` operations through a different path entirely. Search by mechanism,
never by a list you were handed.

**A comment describing a mechanism is a claim, not documentation.** Two comments
in this codebase asserted `track_operation` calls that do not exist. One guard's
error message asserted something provably false. Verify before citing.

**Shape-matching manufactures defects out of correct code.** Four aggregation
sites *looked* identical to four real ones. Recording outcomes there would have
tripped a restore sweep scoped to exactly the pair being written — silent data
loss on resume, with its own purpose-built tests green.

### On tests

**A test written by the author of a fix inherits the author's model of the bug.**
Seven times in one session a test asserted the mechanism the author built rather
than the symptom the ticket described. Ask of every test: *does it assert the
thing the ticket complained about, or the thing I changed?* If the ticket says
"the UI shows nothing", the test must call the UI's own loader.

**Mutation-test the assertions.** Neutralise the fix and confirm the *right*
tests fail for the *right* reason. Several agents did this unprompted and two
found their own new test was vacuous.

**A green purpose-built test is not evidence the invariant is right** — only that
the code does what you told it to.

**A landed fix is not evidence.** Verify committed code with a repro, not with
its test suite.

### On invariants and checks

**When a mechanism is a difference, check the difference.** `demoted = created −
consumed`. Four separate checks sampled one operand and produced confident,
reproducible, wrong answers — including one written to *guard* the mechanism,
and one written to *verify* that guard.

**Take a check's scope from a source independent of the thing under test.** An
invariant that derives its subject list from the property it is checking is
blind exactly where they diverge, and *reads* as whole-registry.

**A gate must be validated in the direction that loses data.** A classification
whose under-declare case is caught and whose over-declare case is not only
proves it is self-consistent, never that it is right.

### On process

**Assert a patch's anchor matched before trusting it landed.** A silent no-op
against stale content is indistinguishable from success.

**Re-verify after any hook mutates the tree.** A formatter that reflows a file
after a content hash is computed leaves the hash describing content that no
longer exists — and a scoped suite will not catch it.

**Read the tail, not the exit code.** A background run reported `exit code 0`
with `2 failed` in its output.

**Assign files, not directories.** Two agents in one directory is luck, not
design.

**Record corrections on the ticket, not just outcomes.** A `fix_verification`
saying "all gates green" teaches nothing. One saying "the fix site, the premise
and the instance count were all wrong in the brief" makes the next attempt
cheaper.

---

## What to expect

- Most findings come from reviewers told to attack, not from fixers' gates.
- Expect to be wrong about *where* the fix goes. Brief the invariant.
- Expect at least one unit's best outcome to be a revert.
- Budget for the review rounds, not the fix. The fix is usually small.
