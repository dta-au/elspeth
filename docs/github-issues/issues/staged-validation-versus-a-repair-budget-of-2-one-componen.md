---
title: Staged candidate validation costs two repair turns against a default budget of two
labels: [area/composer, type/bug]
---

The Composer validates a proposed pipeline in two stages and reports only one stage at a
time, so a proposal carrying one defect in each stage consumes the entire default repair
budget by construction. No change to the planner's brief or teaching text alters the
arithmetic.

## Background

The Composer is ELSPETH's authenticated web authoring surface: an LLM planner proposes a whole
pipeline, the server validates it, and a rejected proposal costs a *repair turn* — a further
provider call spent letting the model fix it. Repair turns are capped per session.

## What happens

A session that hits one component-level defect and one whole-graph defect spends two repair
turns before its proposal can be accepted. Observed on a tutorial session and reproduced
independently by two sessions, on different commits and with different graphs:

```
#1 discovery  discovery_executed  codes=[]
#2 candidate  candidate_rejected  codes=['validation_error']      <- stage 1
#3 repair     candidate_rejected  codes=['locked_input_extras']   <- stage 2, first reachable here
#4 repair     accepted            codes=[]
```

The session did finish and produce correct output, but used 4 provider calls and 2 repair
turns against a ceiling of at most 2 calls and 0 repair turns.

## Why

**Stage 1** runs every per-component gate in `src/elspeth/web/composer/tools/sessions.py` and
then short-circuits at `sessions.py:1691-1699`, because spec construction and the whole-state
checks below it need a complete component set — a partially validated proposal would either
construct specs from options that never passed their gate, or report whole-state defects that
are artefacts of the missing components.

**Stage 2** is spec construction and the whole-state and graph checks. It is unreachable
while any stage-1 rejection stands.

**The budget** is `composer_planner_repair_budget` in `src/elspeth/web/config.py`:
`Field(default=2, strict=True, ge=0)`.

One stage-1 defect plus one stage-2 defect therefore costs two repair turns deterministically
against a budget of exactly two. Zero margin. The short-circuit is correct, which is what
makes this structural rather than a defect in the gate — and stage-2 feedback demonstrably
works once visible, since the stage-2 rejection above was repaired in a single turn the first
time the model could see it.

The stage-1 rejection also carried no repair information. `validation_error` is the fallback
a rejection resolves to when its constructor passes no `error_code` — five sites in
`src/elspeth/web/composer/pipeline_planner.py` read `entry.error_code or "validation_error"`.
The rejection the model receives (a `ValidationEntry`, defined in
`src/elspeth/web/composer/state.py:1508`) carries component, severity, error code and error
class, and nothing else. The model can call `explain_validation_error` to look a code up, but
for this one that tool answers, in `src/elspeth/web/composer/tools/generation.py`, "The text
does not match any known validation message or closed error_code." Told where the fault was
and not what it was, the model changed the only thing an address licenses you to change.

Measured at the time of the report: an AST sweep of `src/elspeth/web/composer/` found 27
direct `ValidationEntry(...)` constructions, 10 with no explicit `error_code` — a floor on
direct constructions only, since factory helpers and other rejection paths are not counted.

## Where to start

Everything here is in the Composer package, `src/elspeth/web/composer/`: the staging behaviour
in its tool layer (`tools/sessions.py`), the rejection objects in `state.py`, and the budget as
a single field in `src/elspeth/web/config.py`. The efficiency ceilings are expressed as
`green_criteria.max_repair_turns` in the convergence scenarios under `tests/unit/evals/`,
checked against `composer_meta.repair_turns_used` — that harness is where a fix is measured.

**Size.** The staging half is not startable as written: which of options 1–3 is intended has
not been decided, and option 3 changes a default an operator sees. Option 4 is independent of
that decision, needs no design work, and is the better first pickup — a bounded pass over the
rejection-producing sites in one package, plus the matching entries in the code catalogue that
`explain_validation_error` consults.

## Impact

An earlier, already-fixed defect covered a different axis of the same problem: the first
defective component returned before later ones were checked, so N defective components cost N
turns. This axis survives that fix and multiplies with it — even with every component defect
reported in one turn, one defect per stage still costs two turns, which alone exhausts the
default budget. Otherwise-correct sessions fail an efficiency ceiling for a reason unrelated
to the model's competence.

## Fix

**Needs a decision first.** Options 1–3 address the arithmetic and are alternatives; none has
been adjudicated, and the choice belongs to the maintainer.

1. **Report the stage,** so the model knows a clean stage 1 is not a clean proposal. Cheapest;
   does not change the arithmetic but removes the surprise.
2. **Do not charge a repair turn for a stage transition,** or make the budget stage-aware — a
   turn that advances from stage 1 to stage 2 is progress, not a failed repair.
3. **Raise the default budget.** Weakest: it hides the arithmetic, and the efficiency ceiling
   is what made this visible.
4. **Give every stage-1 rejection a closed `error_code`.** Independent of the above, and worth
   doing either way.

Correct behaviour for 1–3: a proposal carrying exactly one defect per stage is accepted within
the default budget, and the model is never charged a turn for information the server withheld.
You would know it holds when a convergence scenario built from the trace above — one component
defect, one whole-graph defect — reaches `accepted` inside `max_repair_turns`, while a scenario
with two defects in the *same* stage is unaffected.

Correct behaviour for 4: every rejection the planner can emit carries a code
`explain_validation_error` can answer for. Assert it rather than sampling it — walk the
rejection-producing sites and check that no path can yield the `validation_error` fallback, and
that every code a rejection can carry resolves to a real explanation.

Reporting more of what validation already found is not weakened validation. Nothing here
proposes admitting a defective proposal, or constructing specs from options that never passed
their gate — the short-circuit stays.
