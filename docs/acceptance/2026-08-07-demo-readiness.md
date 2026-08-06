# Demo readiness — weather as at 2026-08-07, and the pin list

Date: 2026-08-07. Author: `claude-r6-battery`.

## Read this first: two runs, two provenances, do not blend them

This weather report merges **two sources that measured different things**, and
the disagreements between them are the most useful signal in it.

| | Source A — round 6 (mine) | Source B — swarm full corpus |
|---|---|---|
| shape | targeted: g08 ×3, g04 ×2, g03, g11 | full corpus, 12 runs / 11 graphs |
| pin | `69c6ad4b5` | **unknown** |
| render | ALB 900 / composer 840 (the shipped default) | **unknown, but almost certainly smaller** |
| evidence | `ops-local/acceptance/r6-battery{,-state}/` | run ids only |

Source B's g08 includes a **compose-422**, i.e. a wall death. At an 840s wall a
g08 compose would have to run 4× longer than any observed sample (mine were
107–206s) to hit it. A 422 is entirely expected at 240–270s. So Source B is
near-certainly running a **pre-`e0d78882e` envelope**, and probably a pin
without the row_union fix (`c408ea870`) either.

That matters for how you read every disagreement below: where the two differ on
an envelope-sensitive or row_union-sensitive graph, Source A is the newer
measurement. Where they differ on anything else, it is **stochastic variation**
— and for a demo, stochastic is the thing to fear.

## Weather

| | Graph | Source B (full corpus) | Source A (round 6, 900/840) | Read |
|---|---|---|---|---|
| ☀️ | g01 linear multi-transform | `completed` | not sampled | Stable across r5 + B |
| ☀️ | g02 gate + routing + poisoned row | `completed_with_failures` 4/1/1 — designed | not sampled | Stable across r5 + B |
| ⚠️ | g03 fork / coalesce | `completed` | **validate-FAILED** — incompatible coalesce merge | **STOCHASTIC — see below** |
| ☀️ | g04 json + explode | `completed` 6/0/0 | `completed` ×2, 164s / 100s | Fixed and fast; agree |
| ⛈ | g05 text → text | **`failed`** | not sampled | **Regression, untriaged** |
| ☀️ | g06 sink variety | `completed` | not sampled | Stable |
| ☀️ | g07 Textract profile-first | `completed_with_failures` 1/1 — designed | not sampled | Stable |
| ⚠️ | g08 row_union A/B | 1 failed, 1 cwf, 1 completed, 1 compose-422 | **3/3 `completed`** — all three via REPAIR | A supersedes B, but green is reached by self-correction — see below |
| ☀️ | g09 four LLM nodes | `completed` 15/18 | not sampled | See row-count note |
| ☀️ | g10 LLM → fixed mapper | `completed` | not sampled | Stable |
| ⛈ | g11 llm source | **`failed`** | **`failed`** — sink write, 0-byte artifact | **Reproducible. Mechanism known** |

Two footnotes that are not weather but matter:

- **g09 `completed` 15/18** — the run is green but three rows did not make it.
  A demo that shows a row count will show 15 of 18. Worth knowing which before
  you stand in front of anyone; it may be designed, but nothing on record says so.
- **g08 in Source A is 3/3 at the new render** (round 5: 1 completed, 2
  validate-failed) — but every one of the three reached green by **repairing
  itself**, not by authoring correctly first time: s1 corrected once, s2 and s3
  four times each, including a `set_pipeline[failed]` and a
  `patch_node_options[rejected]`. That is the same behaviour class flagged as
  ⚠️ for g03; the difference is only that g08 recovered and g03 did not, and at
  n=3 "always recovers" is indistinguishable from "recovered three times".
  It is amber, not sunny. **If the demo shows the compose transcript, an
  audience watches four corrections scroll past.**

## The three that decide the demo

### 1. g11 — reproducible failure, mechanism known ⛈

Both runs fail. Mine has the mechanism: the llm **source** works (reads its row
in 4.5s), content safety passes, and then the **text sink fails** — 0-byte
artifact, `publication_performed: false`. The root cause of the write failure is
**not yet known**; one CloudWatch query on run
`badfc85f-5657-416e-9624-7cfe5caedcf5` should name it.

Worse for a demo than the failure itself: **nothing says why.** Token state says
`failed` with `error_message: None`; the operations array says the *same node*
`completed`; `failure_detail` is `null`; `discard_summary.stages[0].node_id` is
`null`. If this breaks on stage you cannot narrate it.

Note this graph could not run at all before today — `source:llm` was
unauthorised (now fixed, `905613641`). It is a **new** capability with **one**
sample. Treat it as the least-proven thing in the corpus.

### 2. g03 — stochastic, and that is the dangerous kind ⚠️

Source B says `completed`. Mine says `/validate` failed because the composer
declared `price` as `int` on one branch and `str` on the other and fed both
into a coalesce union merge — with **one** `set_pipeline` call, no
`preview_pipeline`, no self-check.

Same graph, same intent, opposite outcomes. A graph that works four times and
authors an invalid merge the fifth is exactly what fails in front of an
audience. Round 5 could not see this because g03 never survived its wall; now
it composes in 191s and the *authoring quality* is what is exposed.

### 3. g05 — a regression nobody has triaged ⛈

Round 5: ☀️ `completed` 6/6. Source B: **`failed`**. Filed as
`elspeth-d1602e4b90`, undiagnosed. I did not sample it, so I cannot say whether
it is stochastic, hard, or simply an envelope death on Source B's smaller wall.
**This is the biggest unknown in the corpus** and the cheapest thing to
resolve — one drive.

## Pin list before the demo

Ordered by what actually threatens a demo. All four must-fix items are filed
and carry the **`demo-blocker`** label, so `filigree list-issues --label
demo-blocker` is the live version of this table.

### Must fix

| # | Ticket | Why it blocks |
|---|---|---|
| 1 | `elspeth-d1602e4b90` | **g05 regression.** Was green 6/6 in round 5, `failed` in the swarm run, unsampled at the round-6 pin. Diagnose first — one drive answers it, and it may be an envelope death rather than a graph defect |
| 2 | `elspeth-9595abb7b0` | **g11 sink failure is undiagnosable.** Two surfaces contradict each other; if anything fails on stage you cannot explain it. Fix the diagnosability even if the root cause turns out trivial |
| 3 | `elspeth-afdf55a17c` (P1) | **g11 sink write fails** — 0-byte artifact, run failed. The only reproducible red in the corpus, on the newest capability, with one sample. Reproduced independently by the swarm run on a different envelope, so it is not envelope-dependent |
| 4 | `elspeth-85f3cc3022` | **g03 authors invalid pipelines sometimes.** Stochastic failure in a demo graph. The narrow fix is cheap: make the composer `preview_pipeline` before declaring done — it already does this on g11 and g08 |

### Fix if the demo touches it

| # | Ticket | Trigger |
|---|---|---|
| 5 | `elspeth-b73666ac82` (P1) | Only if you show **YAML export**. The Composer's own export cannot be re-loaded by the YAML surface, which is precisely the "two surfaces, one runtime" story. Do not demo the export until this is decided |
| 6 | `elspeth-de3638b6ac` | Only if you show the **transcript**. Corrections render as "ELSPETH is applying a pipeline correction" with no detail, and tool-result rows are not returned |
| 7 | `elspeth-f5e6723133`, `bf9c296ee5`, `2784531888` | Composer authority + completion labels. These govern what the UI *claims* happened. Two are `verifying`, one is `confirmed` and still blocked |

### Close these — they are done, and closing them is the story

All verified in round 6 with negative controls. Leaving them at `verifying`
understates where the product is.

| Ticket | Evidence |
|---|---|
| `elspeth-41bcaa882e` | L1 prevention + repair-channel probe, both controlled; g08 3/3 live. Also closes `elspeth-902fc354b2` |
| `elspeth-155947ca47` | 7/7 agree, including on an invalid state — the condition it governs |
| `elspeth-b19dfe41fb` | Live `/validate` rejects round-5's exact shape; round-5 code accepted it |
| `elspeth-09c91778f5` | g03 composes in 191s vs ~500s; ALB 900 verified on the ALB |
| `elspeth-9d59c33480` | Loop gone 6/6 — **but read `elspeth-obs-8ad9b34eea` first**: the byte-match trap survives and round 6 did not exercise the fix's own mechanism. Close the caveat or close the ticket knowingly |

### Do not let these block the demo

`elspeth-9615d6c75a` (3 test failures at the pin) is in-flight and owned; the
product moved in the safe direction. The four `deferred` P0s are governance/CI
items, not runtime.

## What I would do with one more hour of stack time

The Sydney stack's **cleanup deadline is 2026-08-09**, so this window is
closing. In priority order:

1. **Drive g05 once.** It is the only untriaged red and the cheapest answer.
2. **One CloudWatch query** for the g11 sink cause.
3. **Drive g03 ×3.** Establish whether the invalid coalesce is a coin flip or
   an edge case; that single number decides whether g03 belongs in a demo.
4. **Drive g09 once** and check the 15/18.

That is four cheap actions that would turn every remaining ⚠️/⛈ into either a
known-good or a named defect.
