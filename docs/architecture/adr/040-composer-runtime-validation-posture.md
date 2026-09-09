# ADR-040: The Composer/Runtime Validation Posture — Soundness, Placement, and Per-Rule Authority

**Date:** 2026-08-07
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** composer-authoring, validation, runtime-parity, divergence-registry

## Context

ELSPETH validates a pipeline on more than one surface, and until this ADR the
relationship between those surfaces was recorded nowhere: it was inferred from
code comments and a test docstring, which is how two reviewers in one session
read the posture in opposite directions — one arguing Stage 1 must never
self-check what the runtime checks, the other that Stage 1 must mirror
everything. Both positions were defensible from the artifacts that existed.
Neither was the actual posture.

The 2026-08-07 review range then closed a family of divergences in both
directions — an over-rejection in Stage 1 (`coalesce_missing_policy`,
elspeth-deb2f5ed93), an over-rejection **in the runtime**, and a Stage-1
abstention that hid a runtime rejection (union-coalesce guarantees,
elspeth-ae83a6b60c) — which made the implicit rules load-bearing enough to
write down.

## Decision

### 1. There are three validation surfaces, not two

1. **Stage 1 — composition validation** (`CompositionState.validate`): field
   presence, closed vocabularies, graph shape, schema contracts, at authoring
   time.
2. **Stage 2 — runtime preflight** (`validate_pipeline`): the exact runtime
   YAML is generated and checked with the runtime's own machinery (settings
   load, plugin instantiation, graph build, path allowlists) without executing.
3. **Executor-level per-row enforcement**: contracts checked while rows flow —
   diversions, guard rails, audit admission.

Any framing that names only "the two stages" misses the third surface, and
claims of equivalence or coverage must say which surface they are about.

### 2. The soundness invariant

**Stage 1 must never ACCEPT what the runtime REJECTS.** Abstaining is
legitimate. Over-rejection is permitted but costs authoring friction and is a
defect to fix when found. **Global equivalence is explicitly not the target** —
Stage 1 is a fast, explainable authoring aid, not a re-implementation of the
engine.

The two failure directions are not symmetric: validate-green/runtime-red
(soundness violation) strands the author with no error to repair against;
validate-red/runtime-green (over-rejection) costs a needless repair turn.
Both are defects; the first is worse.

### 3. The placement rule

Derived from (2): **Stage 1 models the runtime's own treatment of the input;
it never invents a treatment.**

- The runtime defaults it → Stage 1 defaults it (at the construction boundary,
  so every path inherits the default — `NodeSpec.__post_init__` for coalesce
  `merge` and `policy`).
- The runtime rejects absence → Stage 1 may require it (`branches`,
  `condition`, `routes` — no runtime default exists, so no default may be
  fabricated anywhere, including the YAML generator).
- The runtime has a comparison rule → Stage 1 mirrors it **by calling the
  runtime's own function** (`merge_guaranteed_fields` for union-coalesce
  guarantees), never by re-implementing it.

The 2026-08-07 sweep of all ten Stage-1 required-field rules found nine
conforming; `coalesce_missing_policy` was the one violation and is closed
(Shape 20).

### 4. Authority is per-rule, not uniform

A blanket "the runtime remains authoritative" is false as a global claim: the
divergence registry's own Shape 17 records the composer as the stronger
surface, and the 2026-08-07 range fixed an over-rejection **in the runtime**.
Each divergence names its own authority when it is filed and when it is
closed. The registry entry is the record; neither surface holds standing
authority over the other.

### 5. Mirrors reuse the runtime's values and gate on the normalised value

A mirrored rule must read the runtime's own constant or call its own function
(`CoalesceSettings.model_fields["policy"].default`, never a copied literal),
and every consumer that keys on the mirrored value must observe the
**normalised** value — `merge=None` is the worked example: gating
`merge == "union"` on the raw value silently excluded every defaulted
coalesce until normalisation moved to the construction boundary.

### 6. Closing a divergence obliges amending its registry entry

The divergence registry (the module docstring of
`tests/integration/pipeline/test_composer_runtime_agreement.py`) records what
is **actually** closed, never a bare "closed": name the construction paths the
fix covers and the routes that remain open (e.g. "closed for
NodeSpec-constructed state; the injected-`state_dict` route remains open"),
and name the test class that pins the closure. A closure that cannot say what
it leaves open is not yet understood.

## Consequences

- Reviewers arguing "Stage 1 shouldn't check this" or "Stage 1 must check
  this" now argue from the placement rule, not from taste: the question is
  only ever *what does the runtime do with this input*, and the answer
  determines Stage 1's obligation mechanically.
- New Stage-1 rules that require a field must first establish the runtime
  rejects its absence; new defaults must first establish the runtime's
  default and read it from the runtime's model.
- Abstention remains legitimate, but an abstention that renders as
  `is_valid: true` on a surface that publishes verdicts is a soundness
  violation on that surface (elspeth-2ed41f0a4a) — abstaining and asserting
  validity are different acts.
- The registry grows an amend-on-close obligation; a fix PR that closes a
  shape without amending its entry is incomplete.

## Related

- ADR-009 (shared aggregation rule), ADR-032 (validate by trust domain)
- Shape 17, 19, 20 in the divergence registry
- elspeth-deb2f5ed93 (the placement-rule violation), elspeth-bceffeba19 /
  elspeth-5581fcb76f (lowering refusals become red verdicts; no fabricated
  defaults), elspeth-ae83a6b60c (mirror by calling the runtime's function),
  elspeth-96bbb2699f (open: quorum/best_effort conditional requirements not
  yet mirrored)
