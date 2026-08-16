# State Engine — disposition plan for the six open issues

Date: 2026-08-15
Branch: `release/0.7.2` @ `be87bcaee`
Scope: `elspeth-4b3d734e3a`, `elspeth-67be892457`, `elspeth-cc0b256aca`,
`elspeth-d262ace360`, `elspeth-eefd990b46`, `elspeth-f89d82e925`

> **STATUS 2026-08-15: the tracker restructure in §4 has been EXECUTED.** The
> tree in §0 is the *former* structure, kept because §1–§4 reference it. Live
> structure is two sibling epics: `elspeth-1040aa2143` (state engine, 21 members)
> and `elspeth-ab24e49260` (engine code-quality, 110). Steps 1 and 4 of §3 are
> filed as issues; step 2 is done; step 3 is filed and ready. The **only item
> still needing you is F1's disposition** — `elspeth-44129778e7`.

## 0. What these six actually are

They are not bugs. They are the **v2 pinning-and-completion campaign plan tree**:
one milestone, four proof-cohort steps, and the final assessment step.

```
elspeth-4b3d734e3a   [milestone] State Engine v2 pinning and completion
├── elspeth-977b1a2283  [phase] Pin the current contract            DONE (2/2)
├── elspeth-7152ce00e4  [phase] Close implementation and proof cohorts  (1/5)
│   ├── elspeth-d262ace360  Task 5   queue/source/transform/gate     pending
│   ├── elspeth-eefd990b46  Tasks 4,6,7  lease/process/read-model    in_progress
│   ├── elspeth-cc0b256aca  Task 8   aggregation/coalesce/row-union  in_progress
│   ├── elspeth-f227dd8d2f  Task 9   sink-effect publication/repair  CLOSED 08-14  ← see F1
│   └── elspeth-67be892457  Tasks 10,11  lifecycle/follower/plugins  pending
└── elspeth-19149b1cb7  [phase] Publish completion                      (0/1)
    └── elspeth-f89d82e925  Task 12  final assessment + maintained gates  pending
```

"Resolving" them means **making the tracker true and naming the owner of each
remaining gap** — not closing them. The reason is in §1.

## 1. Structural verdict: none of the six is closeable today

The first full v3 assessment ran and published on 2026-08-15
(`docs/architecture/state_engine/assessments/2026-08-15-0537/`, freeze
F = `2b4b04a8a`, publication P = `1c0d93b67`, merged to release at `4d78035`).
Its own machine record says:

- **73 of 73 legs are `derived_verdict: unknown`. Zero are `confirmed`.**
- 489 of 7,010 cells promoted (417 `pass`, 72 `partial`).
- 6,521 cells unresolved: **1,780 live-lane** + **4,741 local**.
- `HG-09-mandatory-leg-unresolved` is **open**; the other nine hard gates are `unknown`.
- Overall verdict: `not_complete`.

Every leg except `RC-05` and `PB-08` carries ≥10 unexecuted live-lane cells
(`PB-11` is 50 live / 0 local). `completeness-criteria.md` — last updated in the
publication commit itself — makes
`postgresql-16-aws-single-leader-landscape` a **required** execution-profile case
for every mandatory v2 cell, and states outright:

> Applicability is catalog-owned. An assessor cannot mark a dimension N/A merely
> because it is inconvenient to execute.

AWS was torn down 2026-08-10. Therefore **no cohort step can reach its stated exit
gate, `f89d82e925` cannot close, and the milestone cannot close, until the
protected live-provider workflow fires at F.** That is a precondition, not a defect.

Per-cohort accounting from `assessment.json` (the five cohorts partition all 73 legs exactly):

| Cohort | Legs | Confirmed | Unknown | Cells `pass` | `partial` | Live unresolved | Local unresolved |
|---|---:|---:|---:|---:|---:|---:|---:|
| `d262ace360` Task 5 | 13 | 0 | 13 | 47 | 0 | 130 | 343 |
| `eefd990b46` Tasks 4/6/7 | 35 | 0 | 35 | 145 | 2 | 380 | 863 |
| `cc0b256aca` Task 8 | 14 | 0 | 14 | 31 | 0 | 200 | 569 |
| `f227dd8d2f` Task 9 **(closed)** | 7 | 0 | 7 | 105 | 3 | **330** | **882** |
| `67be892457` Tasks 10/11 | 4 | 0 | 4 | 89 | 67 | 740 | 2084 |
| **Total** | **73** | **0** | **73** | **417** | **72** | **1780** | **4741** |

## 2. Findings

### F1 (P1) — `elspeth-f227dd8d2f` was closed on seven unresolved legs, on a rationale that does not exist

On 2026-08-14 20:34 UTC the actor `codex-p1-closeout` transitioned
`f227dd8d2f` pending → in_progress → completed in under a second, with:

> "…the retired AWS-only witness is not remaining work."

Ten minutes later the same actor **force-closed** `elspeth-82592e3aa1` as
"obsolete under the corrected acceptance boundary" — the closure John already
rejected, replacing it with the slim successor `elspeth-29a7f5a21a`.

The phrase "corrected acceptance boundary" appears **nowhere in the repository**:

```
git grep -n "corrected acceptance boundary"        → no matches
git log -S "acceptance boundary" -- docs/architecture/state_engine/  → no commits
grep -rn "acceptance boundary" docs/architecture/state_engine/*.md   → no matches
```

`completeness-criteria.md` was last touched by the publication commit `1c0d93b67`
— **the day after** the closure — and still mandates the AWS profile. So no
amendment retired the witness. Meanwhile `f227dd8d2f`'s own exit gate reads:

> "Exit: effect publication, ambiguous connection loss, restart/repair, **real
> external-provider**, and supported-profile evidence establishes exactly one
> effective publication."

Its seven legs (TS-11–14, PB-06/07, F-08) are all `unknown` in the assessment,
carrying **1,212 unresolved cells** (330 live + 882 local; PB-06 alone is 200/553).
The issue's own last three comments — written by the agents who did the work —
say the opposite of the close reason: *"Live AWS+PostgreSQL16+S3 remains unknown
and is not relabelled from local evidence; owner remains open for that external lane."*

This is load-bearing, not bookkeeping: `f227dd8d2f` **blocks `f89d82e925`**. Its
closure has already removed one of the four gates on the final assessment step.
If the other three were closed the same way, the milestone would read as closeable
with ~6,500 cells unknown.

Contrast — the sweep's third action, closing `elspeth-5b140232d2`, **is**
defensible: that task's scope was the static v3 catalog/selector/workflow-policy
contract, and its close reason says plainly "Live dispatch and AWS evidence were
explicitly outside this task." Only `f227dd8d2f` asserts a retirement.

### F2 (P2) — HG-10's tracker arm is unverifiable as built

`HG-10-normative-contract-drift` requires that "source, architecture, catalog,
assessment, **and tracker** do not contradict one another", and claims all 73 legs
as affected. But the assessment's `tracker_snapshot` is a three-field prose stub:

```json
{"provider": "Filigree issue tracker (local database) at evidence-capture time",
 "captured_at": "2026-08-15T09:00:40.134138+10:00",
 "limitation": "Live-lane gap owners remain open by design until the operator
                restores AWS and fires the protected workflow."}
```

No issue identities, no statuses, no leg→owner cross-check. This is precisely the
blind spot that let F1 pass unnoticed: a cohort owner can close while every leg it
owns is `unknown`, and nothing in the pipeline detects the contradiction.

### F3 (P3) — seven stale claims held by deleted agents

All seven appear in `work_stale_list`. Their claimants' branches and worktrees
were deleted 2026-08-13 (verified patch-contained first, so no code was lost):

| Issue | Claimant | Claim expired |
|---|---|---|
| `cc0b256aca` | `codex-task8` | 2026-08-13 |
| `eefd990b46` | `codex-state-engine-task6` | 2026-08-13 |
| `c0d4a28e11`, `2e66723070`, `9cd07962c7` | `codex-state-engine-task5` | — |
| `2aba594afb` | `codex-state-engine-task6` | — |
| `6f6bbbec00` | `codex-state-engine-task10` | — |

The last five are the wave-1 P2 tasks that appear in the cohort steps' `blocked_by`
sets. They read as actively worked; nobody is working them.

### F4 (P4) — v2 naming drift

The milestone is titled "State Engine **v2** pinning and completion" and all five
step descriptions cite `docs/plans/2026-08-11-state-engine-pinning-and-completion.md`,
while the catalog moved to v3 and the assessment is v3-derived. Leg IDs
(TS-/AUX-/RC-/PB-/RM-/F-) are unchanged across v2→v3, so the *ownership* text is
still accurate — this is cosmetic drift, listed for completeness only.

## 3. Plan

### Step 1 — Record F1 and put the decision to John (blocking, do first)

Add a comment to `elspeth-f227dd8d2f` stating the finding with the evidence above.
That much is uncontroversial and should happen regardless of the disposition.

Then **John chooses** between two dispositions — this is his call, not the agent's,
because it mirrors a judgement he has already made once:

- **(a) Reopen `f227dd8d2f`.** Restores the honest blocked-by edge on
  `f89d82e925`. Most faithful to the evidence; costs a forced transition.
- **(b) Bridge with a slim successor,** exactly as he did for `82592e3aa1` →
  `29a7f5a21a`: leave the closure, open one issue owning "TS-11–14/PB-06/07/F-08
  live + local residual", and make it block `f89d82e925`.

Both restore the correct `blocked_by` edge on `f89d82e925`, so **gating is right
either way**. What differs is how the tree reads. Under (b),
`filigree plan elspeth-4b3d734e3a` keeps printing
`[x] elspeth-f227dd8d2f …` and "2/5 steps complete" — the exact false-green this
finding is about survives at a glance, with the truth one comment-click away.

**Recommendation: (b), with that cost stated.** It keeps the closed record intact
rather than rewriting history, and produces the same true gating.

But the precedent argument is weaker here than for the sibling: John accepted a
successor for `82592e3aa1` because that closure's *rationale was correct* — AWS
restoration genuinely is not repo work — and only the tail needed an owner. Here
the rationale is fabricated. (a) is the better choice if he wants the sweep's
closures treated as void rather than superseded, or if he wants the milestone tree
to read honestly without reading comments.

Do **not** force-transition it unilaterally either way.

### Step 2 — Release the seven stale claims (cheap, unambiguous)

```bash
cd /home/john/elspeth   # CLI from repo root, per the MCP-write-conflict rule
R="claim expired; claimant branch/worktree deleted 2026-08-13 (work patch-contained on release/0.7.2)"
for i in cc0b256aca eefd990b46 c0d4a28e11 2e66723070 9cd07962c7 2aba594afb 6f6bbbec00; do
  filigree --actor claude release "elspeth-$i" --reason "$R"
done
```

Then, for each of the five wave-1 tasks, check whether its **specific** assertions
landed in the merged tree (the branches were patch-contained, so the code did —
the open question is whether the named assertions exist, not whether the commits
survived). Where they did, close on integrated evidence with the assertion node
IDs in the close reason; where they did not, restate the residual.

**The distinction that keeps this from repeating F1:** a wave-1 task closes when
its named assertions exist and run; a **leg** reaches `confirmed` only when every
required case *including the AWS profile* passes. The five wave-1 tasks are
"add a test that…" items, not leg owners, so they are legitimately closeable on
the former. Closing a *cohort step* on the same reasoning is the F1 move — the
cohort steps' exit gates are written in leg terms.

This shrinks `blocked_by` sets. It **unblocks nothing** — the live-lane cells still
dominate every cohort's exit gate. Sequenced second because it is hygiene, not progress.

### Step 3 — Do the only work available now: `elspeth-efb47cb5fd`

Open, ready, zero blockers, **no AWS required**. It owns the 4,741 local unresolved
cells — the largest body of work anyone can do today.

**Be clear about what it buys, because it is not the verdict.** Exactly 2 of 73 legs
(`RC-05`, `PB-08`) have zero live cells, and they sit in two *different* cohorts
(`eefd990b46`, `67be892457`) whose other legs all carry live cells. So completing
100% of `efb47cb5fd` confirms 2 legs, closes **zero** cohort steps, leaves HG-09
open, and leaves the verdict at `not_complete`, unchanged. The verdict is
single-gated on AWS.

What it does buy is retiring **4,741 of 6,521 residual cells (73%)**, so that when
AWS returns, completion is one workflow dispatch away instead of a dispatch plus a
multi-thousand-cell authoring campaign. Necessary but not sufficient — fund it on
that basis, not on expectation of verdict movement.

Where the mass sits:

| Leg | Local unresolved | Share |
|---|---:|---:|
| PB-09 (per-plugin matrix) | 2,022 | 42.6% |
| PB-06 | 553 | 11.7% |
| PB-10 | 200 | 4.2% |
| PB-07 | 192 | 4.0% |
| remaining 68 legs | 1,774 | 37.4% (~26 each) |

Sizing caveat to resolve **before** committing effort: `efb47cb5fd`'s own
description says PB-09's residual includes "38 provider-backed PB-09 cases with no
local internal-composition test." Those may need providers, so the genuinely
local-authorable count is ≤4,741, not equal to it. First action on this issue is to
split PB-09's 2,022 into locally-authorable vs provider-dependent and record the
split — that number determines whether this is a large-but-finite lane or mostly
another AWS-shaped wait.

Discipline that applies (from the campaign's own review cycle, which demoted seven
cells for exactly these failures): author a test or extend the coverage mapping
**only where an assertion genuinely proves the cell**. No tautological passes, no
stand-in subjects, no non-discriminating success effects. A mapping that does not
discriminate is worse than an honest `unknown`.

### Step 4 — Close HG-10's tracker arm (F2)

Give `tracker_snapshot` real content: per-leg `owner_issue` → issue status at
capture time, plus a validator rule on unknown-verdict legs. That single rule
would have caught F1 at publication time.

The rule needs a **superseded-owner escape**, or it blocks the next publication
immediately: 71 of 73 legs currently name `82592e3aa1`, which is closed *by
design* and bridged to `29a7f5a21a`. So:

- closed owner **with** a recorded successor pointer → acceptable;
- closed owner **with no** successor → violation.

That means the successor pointer has to become structured data (a
`superseded_by` field on the leg or an owner-chain map), not just a bridge comment
in the tracker — otherwise the validator cannot tell the two cases apart.

Two constraints:
- The published package is **digest-frozen and is not edited retroactively**
  (`29a7f5a21a` states this; the stale `82592e3aa1` owner pointer on 71/73 legs is
  deliberately handled by a bridge comment, not by rewriting the package). This
  change lands in the **assembler and validator** for the *next* assessment.
- `validate-package` / `collect-evidence` are **capture-root-bound** — they
  fail-closed outside `.claude/worktrees/state-engine-v3-assessment`, which must
  stay in place. CI never runs them, so CI stays green either way.

File as a new issue against the assessment tooling; it is not owned by any of the six.

### Step 5 — Leave the six open, correctly blocked

- `d262ace360`, `eefd990b46`, `cc0b256aca`, `67be892457` — remain open. Each needs
  its live-lane cells before its exit gate is met.
- `f89d82e925` — remains open; its `blocked_by` set should regain a fourth edge
  from step 1.
- `4b3d734e3a` (milestone) — remains open behind them.

The chain closes in this order, and no sooner:

```
AWS restored (operator, outside the tracker)
  → elspeth-29a7f5a21a  dispatch state-engine-live-provider.yml ONCE at F=2b4b04a8a
                        + ingest-live-evidence into package 2026-08-15-0537
                        + re-derive verdict from the capture worktree
  → 1,780 live cells resolve; legs can begin reaching `confirmed`
  → cohort steps close (with elspeth-efb47cb5fd's local cells also resolved)
  → elspeth-f89d82e925 closes
  → elspeth-4b3d734e3a closes
```

`29a7f5a21a`'s exit gate also admits the alternative: **a deliberate
completeness-criteria amendment retires the live-lane requirement.** That is a
legitimate path and it is John's to take — but it must be written into
`completeness-criteria.md` as an amendment. Asserting it in a close reason, which
is what F1 did, is not that.

## 4. Executed: consolidation into two sibling epics (2026-08-15)

Per the operator's direction — "move everything into a single epic with tasks and
bugs underneath it… reach out for P2 and P3 tasks and bugs as well, we're starting
a hard push to 1.0."

**`elspeth-1040aa2143` — State engine — completion to 1.0** (epic, P1, `release:1.0`).
21 members: the 6 cohort/final steps moved off the plan tree, all 13 open
`[state engine]` tasks and bugs (11 P2, 2 P3), and 2 issues created from this
document's findings. Full manifest in its first comment.

**`elspeth-ab24e49260` — Engine code-quality burn-down** (epic, P2, `release:1.0`).
110 members: the 109 open `panel-triage:src-elspeth-engine-2026-07-03` findings
(3 P2 bug, 103 P3 bug, 2 P3 task, 1 P4 task) plus a validation-pass task.

Kept as **siblings, not one epic**, because they are different debt classes: Epic A
is proof/evidence debt that cannot complete without AWS; Epic B is code-quality
debt workable today. Combining them would hide which is blocked.

Issues created:

| ID | Type | What |
|---|---|---|
| `elspeth-44129778e7` | P1 bug | **Adjudicate F1** — the Task 9 closure. Needs your decision. |
| `elspeth-079c8fb9ab` | P2 task | F2 — HG-10 tracker arm unverifiable |
| `elspeth-ed60b251ff` | P2 task | Validate the panel-triage corpus against the current tree |

Scaffolding retired after all work was moved off it — `4b3d734e3a` →
**cancelled**, `7152ce00e4` / `19149b1cb7` → **skipped**, `977b1a2283` →
**completed** (its two children genuinely were). `cancelled`/`skipped` were chosen
over `completed` deliberately: the v2 completion this tree described never happened.

### Things that bit, recorded for the next restructure

- **The claim guard blocks re-parenting.** `update --parent` fails with
  `Cannot operate on <id>: assigned to '<agent>'` on any claimed issue. Releasing
  the 7 stale claims was a *prerequisite*, not the hygiene step §3 called it.
  Release also resets `in_progress` → `pending`, which is the honest state.
- **`filigree create --json` prints `warning: ACTOR_MISMATCH` to stdout before the
  JSON**, so `json.load(stdin)` crashes. Seek to the first `{`. A crashed parser
  reads as "the create failed" — it did not, and a duplicate epic
  (`f872ee9eee`) was created and later deleted.
- **`filigree list --type epic --limit 20` is priority-sorted**, so a new open P1
  epic can fall outside the window. It is not an existence check. Search by title.
- **`filigree plan` is milestone-only** — `Error: Issue … is not a milestone`.
  Cancelling the milestone costs that rollup; use `filigree list --parent <epic>`.
  No loss here: the old `plan` view was printing `[x] elspeth-f227dd8d2f`.
- **Type is immutable** (`update` has no `--type`), so the cohort steps keep
  `step` type under an epic. Re-parenting preserved comments and every
  dependency edge — verified: `f89d82e925` is still blocked by all four cohorts.
- **A phase's `[DONE] … (2/2)` in `filigree plan` describes its children, not
  itself.** `977b1a2283` was still `pending`; the manifest was corrected.

## 5. What this plan deliberately does not do

- Does not close any of the six. Nothing in the evidence supports it.
- Does not edit the published assessment package (digest-frozen by design).
- Does not force-transition `f227dd8d2f` in either direction — F1 is surfaced for
  John's decision, with a recommendation.
- Does not treat AWS restoration as tracked work; it is operator infrastructure,
  and `29a7f5a21a` correctly owns only the repo-facing tail.
