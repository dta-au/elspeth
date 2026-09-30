# Composer strict tool contracts: S2 tranche roadmap

- **Date:** 2026-09-25
- **Status:** general planning only. **S2 is on hold:** John has not made the stability call. A major engine refactor
  is landing and the codebase stays unsettled until it is tested (John, 2026-09-25). Nothing here starts before
  that call.
- **Detail level:** tranches, goals, entry and exit conditions, gates and the decision each tranche hands to John.
  It deliberately names no line numbers and no specific code changes. The step-level detail stays in the S2 plan and
  gets re-derived against the tree once the call is made, because the tree will have moved by then.
- **Parents:** `docs/plans/2026-09-25-composer-strict-contracts-s2-plan.md` (the steps, principles and 2026-09-25
  rulings) and `docs/plans/2026-09-23-composer-strict-tool-contracts.md` (the design).

## Reading this roadmap after the 2026-09-30 recovery

This roadmap is retained for `release/0.8.1` from `docs/s2-tranche-roadmap` at
`108faf5244`. Recovering the document does not lift John's S2 hold. The dated
rulings below remain the planning authority; a new stability call is still
required before starting S2.

The tranche sequence is a design proposal, not a current implementation
inventory. Its tool counts, branch/deployment status, placeholder sites and
cost cap describe the 2026-09-25 investigation. Re-measure tool membership and
wire partitions through the live registry and wire projector, and verify each
prerequisite against the candidate and deployed route before scheduling a
tranche. The Composer application owners have since moved, Guided Composer has
been removed, and onboarding uses the normal freeform Composer. Re-derive
code ownership and test locations from those surviving paths.

Preserve these owner decisions when refreshing the detailed S2 plan: the
stability call gates all S2 work; the healthy live window additionally gates
wire-changing tranches T4 onward; the per-tool scripted probe uses automatic
selection with the full production roster and named provider endpoints,
followed by a final workflow battery; T1 instruments and A8 land separately.
Use the existing tracked evaluation harnesses before adding probe machinery.
No local codec test establishes live-provider acceptance.

## 1. What changed at the 2026-09-25 planning checkpoint

The live-run defect work (branch `fix/session-ed3c015b-convergence`, not yet merged) changed four inputs to S2:

1. **The strict partition stays 32/10.** The proposed fix for the zero-property regression (stamp those tools
   `strict:false`) failed live controls. The fix that landed on the branch frames the ten parameterless tools with a
   closed wire marker that decodes to the empty semantic object. Every "22 strict" figure is dead. The ten
   marker-framed tools are a schema shape of their own, so they need their own live canary.
2. **M1a is a known defect, not only an instrument.** The stored rejection record did not hold the error detail the
   planner actually received, and that misled the 2026-09-25 diagnosis. M1a fixes exactly that.
3. **A8 now has three sites, not two.** The marker rejection writes the same constant redaction placeholder as the
   envelope and over-bound JSON rejections, so different bad calls look identical to the retry-hint logic at all
   three.
4. **A planner repeat guard now exists.** The branch added a bounded retry hint and stop for repeated identical
   argument rejections in the pipeline planner. A8's compose-loop fix should stay consistent with it.

It also exposed an inconsistency in the S2 plan. §1.1 lets Tier M and Tier A start on the stability call alone,
while §3.1 says nothing starts until the live window is healthy too. **Ruled by John, 2026-09-25:** the live window
gates the steps that change wire bytes (Tier P and Tier F). It does not gate Tier M or A8, which change no wire
bytes and so cannot cause an S1-style provider regression.

## 2. The tranches

Each tranche is a group of small, separately revertible commits, with focused and whole-tree gates on each commit
and one full-suite gate per tranche (ruling R-K). Each ends at a point where John can stop without leaving
half-built machinery (ruling R-F: no machinery ahead of its first producer).

### T0. Readiness (operational, plus the canary probe tool; before any S2 product code)

- **Goal:** establish that S1 as fixed is healthy live, and fix the plan text.
- **Contents:** the live-defect branch merged and deployed; S1's owed dev deployment acceptance; a live canary on
  every strict schema shape S1 already sends, including the ten marker-framed tools, on each `provider_served`
  endpoint the deployed route reaches; the §1.1 and §3.1 wording reconciled as ruled above.
- **How the canary runs (John, 2026-09-25):**
  1. **The gate: a scripted per-tool probe.** One real call per strict tool, sent with the full production tool list
     and automatic tool selection (forced selection was itself shown to cause failures, so it would give false
     reds). Each call is pinned to a named provider endpoint, and every endpoint the deployed route reaches is
     covered. Pass means the returned arguments decode through the production codec.
  2. **Then one final battery round** of real multi-turn workflows through the running service, as the realism
     check.
- **The probe must be a tracked tool.** The live-defect lane's probes are one-off scripts in untracked lane state.
  T0 promotes them into `evals/` (with its own offline controls: a known-good call must pass and a known-bad shape
  must fail), because every flip from T4 onward reuses the same probe before and after.
- **Exit:** a healthy live window on all 32 strict tools, or a named failing shape that goes back to defect work
  first.
- **Hands John:** the stability call, with that evidence beside it.

### T1. Instruments and the live-defect fix (Tier M code: M1a, M1b, M2, M3, M5; plus A8; M4 in parallel)

- **Goal:** make every later flip measurable, and remove the two false signals that exist today.
- **Outcomes:**
  - the stored rejection record equals what the planner saw (M1a);
  - every violation is kept as closed, value-free facts, even past the planner-facing cap (M1b);
  - every tool call is attributable to the endpoint that served it (M2);
  - repeated JSON keys are counted, never rejected, which sizes ruling R-B (M3);
  - the turn-cost and per-turn-rate instruments follow the frozen definition (M5, ruling R-J);
  - the retry-hint logic can tell different rejected calls apart at all three placeholder sites (A8);
  - the battery can reach the tools the corpus never exercises, and names the dialect when it refuses to compare
    (M4, evals only).
- **Model-visible:** A8 only (which hint fires). Everything else must leave the tool-list bytes and the planner's
  messages unchanged, and a byte pin proves it.
- **Gates:** fixture controls per step; persistence changes run the PostgreSQL testcontainer suite as well; a
  redaction review of every new stored field; confirm no session epoch bump is needed (JSON content of existing
  columns only; if that turns out false, it is an honest bump, not a workaround).
- **Landing:** two merges (John, 2026-09-25): the instruments first, then A8 alone.
- **Exit:** tranche merged and deployed.
- **Hands John:** nothing to rule; T2 needs a deploy slot.

### T2. Baseline round (M6; operational)

- **Goal:** the reference every later step is judged against.
- **Contents:** on the deployed S0 + S1 + T1 build: the canary battery, one full round, and the new M4 scenarios.
  First confirm the deployed loop really runs the strict dialect (a round on the non-strict route measures nothing).
- **Exit:** per-tool baselines recorded, including rejection split by endpoint, turn cost, drift-hint count and the
  duplicate-key count.
- **Hands John:** ruling R-B, now with data (how common duplicate keys are), and the first proxy for R-A's cost
  (how often non-enforcing endpoints ignore a strict schema).

### T3. Wire-neutral surface cleanups (Tier A text steps and the census gate)

- **Goal:** make every teaching and diagnostic surface true on both wire forms before any flip would contradict it.
- **Contents:** the guidance catalogue, validation-diagnostic repair calls, state-validation hints, the skill and
  capability-core lines, and the W-side teaching census gate. One surface per commit.
- **Gates:** deterministic per step, then one paired round for the whole tranche against the T2 baseline. That round
  becomes the control arm for the first flip.
- **Hands John:** confirmation that the text changes did not regress anything, before the first flip.

### T4. First flip, as pilot (P1 if R-B says reject, P3, then F1 `patch_node_options`)

- **Goal:** the first option-bearing tool goes strict, carrying the one-off machinery only it needs.
- **Entry:** T3's paired round clean; R-B ruled; the pilot choice confirmed (deferred ruling).
- **Gates:** live canary before and after on every endpoint; zero-tolerance tripwires; the regression bound; the
  legacy-form rejection rate by endpoint; the per-flip benefit read.
- **Hands John:** the first real reading of R-A's cost, and whether the benefit read justifies continuing.

### T5. Close the `patch_*` family (F2, F3)

- **Goal:** the two small follow-on flips that reuse the pilot's machinery.
- **Hands John:** the flip order after F3 (a deferred ruling), decided on T4 and T5 data.

### T6. First pair maps (P4 if R-L says encode, then F4 `upsert_node`)

- **Goal:** the R2 pair codec lands inside the first flip that uses it, never ahead of it.
- **Hands John:** ruling R-D, which was deferred to F4.

### T7. The big one (P2, then F5 `set_pipeline`)

- **Goal:** the largest flip, with its exemplars already rendered per consumer.
- **Note:** the only XL step. If it still looks too large when reached, the price of splitting it is relaxing R-F
  for its machinery, which is John's call (S2 plan §7.2).

### T8. The untrafficked tools (F6 to F10)

- **Goal:** the remaining flips, each accepted on seeded scenarios, since organic traffic will not reach them
  (ruling R-G).
- **After T8:** S3, the planner terminal, follows F5 as the S2 plan §6 describes.

## 3. How each tranche is run

The confirmed cycle (2026-09-25): readers against the tree as it is then; a step-level plan; critics; a Codex review
of the plan; sequential TDD commits; an adversarial review of the whole diff; the lead's full-suite gate with every red
attributed against the base; a `--no-ff` local merge. The step-level plan for a tranche is written only when that
tranche is about to start, against the tree of the day.

## 4. Stop conditions

The S2 plan's §7.3 applies unchanged. In particular: any Tier M, A or P step that changes tool-list bytes; a step that
would need both argument forms accepted for one tool on one route; carrier text in any stored audit row; a live canary
failure on a changed shape; any fix that needs the server to author or repair a value (composer invariant 1) or a
tutorial-only path (invariant 2).

## 5. Recorded owner rulings

1. **Ruled 2026-09-25: yes.** The live window gates the wire-changing tranches (T4 onward), not T1. The S2 plan's
   §3.1 sentence is reconciled to this when the plan is next edited.
2. **Ruled 2026-09-25: a scripted per-tool probe is the gate, followed by one final battery round.** The probe is the
   only option that covers every strict schema shape on every named provider endpoint, and it is cheap enough to
   repeat before and after every flip. The battery round then confirms that real multi-turn workflows still work.
   A manual pass is optional and is not evidence. Budget: the OpenRouter key is capped at $20 of credit, so a canary
   run cannot overspend; exhausting the cap is not a concern (John).
3. **Ruled 2026-09-25: two merges.** T1 lands as the instruments first, then A8 on its own, so the one
   model-visible change is separately revertible and its effect can be read on its own.
