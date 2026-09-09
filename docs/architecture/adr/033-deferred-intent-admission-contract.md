# ADR-033: Deferred-Intent Admission Decides Closed Contradiction, Not Satisfiability

**Date:** 2026-08-04
**Status:** Accepted
**Deciders:** ELSPETH maintainer
**Tags:** composer, guided-mode, deferred-intents, admission-gate,
          planning, fail-closed, testing-doctrine

## Context

During guided Composer sessions an operator states constraints in prose
("the CSV source must use a semicolon delimiter", "at most one gate").
The assistant parses each statement into typed deferred constraints,
retains them in durable session state, and a later planner-authored
pipeline must satisfy them. `validate_deferred_intent_action()`
(`src/elspeth/web/composer/guided/deferred_intents.py`) is the admission
gate: it decides whether a new instruction may join the retained set.
The maintainer's scope reset is preserved in reachable commit `449c93397`.
The governed checker and its exhaustive soundness suite landed in reachable
commit `b803879cb`, in
`src/elspeth/web/composer/guided/deferred_intents.py` and
`tests/unit/web/composer/guided/test_deferred_intent_closed_conjunction.py`.

Under ADR-032 the parse and the decision are different work in
different trust domains. Parsing untrusted operator prose into
ELSPETH-owned typed constraints is external-boundary work: value
assertions, construction of an owned type, original discarded. Once
parsed, the constraints are owned values; deciding whether a
conjunction of owned values is jointly satisfiable is not boundary
validation at all — it is a semantic decision procedure, and it needs
its own contract. This ADR is that contract.

### What was asked for, and what withdrawn iterations explored

Tracker `elspeth-d293c5d139` asked for a small closed conjunction
checker: reject an equals/equals mismatch, an equals plus a matching
not_equals, and incompatible count bounds. Withdrawn implementation
iterations instead explored a bounded exact-partition satisfiability solver:
witness unification across ambiguous plugin subjects, alias-slot counting
against cardinality caps, and fixed subject and partition-step proof budgets.

A non-authoritative seven-lens SME review (security, static-analysis rule
design, solution scope, defect triage, test suite, systems leverage, and
cross-lane recurrence) recommended rejecting the solver; the maintainer
adopted that recommendation. The decisive evidence:

- **Probe-verified false rejection of trivially satisfiable input.** A probe
  against a withdrawn iteration showed that thirteen same-plugin subjects
  under `at_most 13` — satisfiable by inspection — were rejected with
  `constraint_proof_budget_exceeded` at a hard subject-budget cliff.
- **Monotone session degradation.** The budget is spent on the whole
  prospective conjunction — every constraint already retained in the
  session plus the new one — so accumulated history consumes it. A session
  that retains enough valid instructions eventually rejects every further
  instruction, and nothing the operator does in-session recovers it.
- **Undiagnosable, retention-bypassing rejection.** In the withdrawn
  iterations, contradiction and budget rejections collapsed to one generic
  message and returned `DeferredRequestUnchanged` instead of routing through
  `apply_deferred_clarification` — bypassing the R2-F15 retention net
  ("instructions are never silently dropped", `elspeth-a96b2f1b0a`).
- **Unbounded review surface.** Four candidate iterations were withdrawn over
  four hours; each repair exposed a further soundness defect in the partition
  or alias arithmetic.

### Admission is not the safety control

Downstream safety is independent of admission.
`verified_remaining_deferred_intents`
in `src/elspeth/web/composer/guided/planning.py` re-verifies mechanical
coverage of **all** retained intents against the concrete proposal, and wire
confirmation in `src/elspeth/web/sessions/routes/composer/guided.py` refuses
with HTTP 409 while any remain, with explicit operator edit/cancel recourse.
An unsatisfiable set that admission misses therefore wedges fail-closed at
wire confirmation; it cannot reach execution. Admission is a diagnostics and
efficiency gate — catch the contradiction while the operator is still talking
about it — not a security control. The cost of under-rejection is a late,
signposted block with recourse. The cost of over-rejection is refusing
a valid operator instruction, which no later stage can repair.

The solver also inverted the gate's own default. Release behaviour
admits every conjunction; a fail-closed tier layered on top of that
default must reject only what it can prove **unsafe**. On budget
exhaustion the solver rejected what it could not prove **safe** —
treating "unproven" as "contradictory" — which is the fail-closed
posture of a gate whose default is rejection, applied to a gate whose
default is admission.

## Decision

**Deferred-intent admission decides contradiction within (i) a single
exact subject identity and (ii) closed count-bound arithmetic. It
explicitly declines existential subject resolution: no witness
partitioning, no proof budget, no counting of ambiguous plugin-subject
hints against cardinality caps, no general multi-subject
satisfiability. Constraint sets that are unsatisfiable outside the
closed rules remain admitted — release behaviour — and are caught
fail-closed at wire confirmation.**

### The closed rule set (normative)

1. **Functional-dependency conflicts on one subject key.** Presence
   asserted both true and false; a subject required by another retained
   constraint while asserted absent; two component kinds for one subject;
   two plugin identities for one subject; option equals/equals
   mismatch and equals plus a matching not_equals per
   `(subject, option_path)`; two distinct stated gate predicate signatures on
   one subject; two distinct routing target pairs for one predicate; a
   failure-route equals target conflicting with a second equals or a
   matching not_equals per `(subject, failure_kind)`; an edge asserted both
   present and absent for one `(from, type, to)` triple.
2. **Count bounds.** Empty intersection within one count key —
   `at_least` above `at_most`, `equals` outside the bounds, two
   distinct `equals`; a plugin identity asserted absent while a count requires
   at least one; an upper bound of zero on a component kind or plugin identity
   that a required subject inhabits.
3. **Cross-group count subsumption, closed identity-free form.** Where
   one count group is contained in another — a plugin-scoped group
   within its component-kind group — the required minima of the
   contained groups sum against the containing cap. A global node
   `at_most 1` alongside `transform:normalize at_least 2` is a
   contradiction. One withdrawn iteration entangled this arithmetic with
   witness counting; the retained rule is the arithmetic core with no witness
   or alias resolution.
4. **Exact-subject predicate-implied gate counting.** Distinct stated
   predicate signatures on distinct **exact** subjects each imply one
   synthesised gate node, counted against node-kind caps. Declined for
   ambiguous plugin subjects: a gate implied by a subject that has not
   resolved to an exact identity is not counted.
5. **Presence false versus implicit requirement.**
   `SubjectPresenceConstraint(present=False)` conflicts with any other
   constraint that implicitly requires that subject: option constraints; edge
   routes, which require both endpoints regardless of the edge's own present
   flag; stated predicates; failure routes; and, at plugin-identity level, a
   globally absent identity intersecting a required identity. This rule fixes
   the reach of the requirement relation that rule 1's required-but-absent
   check consumes.
6. **Single-subject option-path prefix/descendant collapse.** A scalar
   `equals` at path P conflicts with any constraint at a strict
   descendant of P on the same subject. A withdrawn iteration made this
   reachable only inside the partition path; the governed implementation lifts
   it to a standalone single-subject rule.
7. **Finite-domain exhaustion.** A `not_equals` set that exhausts a
   validated finite schema domain per `(subject, option_path)`, implemented by
   `_option_schema_conjunction_is_consistent`.

### Subject keying: keep the single-match plugin-name aliasing

`_constraint_subject_key` in `deferred_intents.py` resolves a plugin-name
subject to a stable identity exactly when exactly one reviewed or
pending-intent component of that kind bears the plugin name. This
aliasing is retained. It was adversarially verified sound because the **same
resolution rule applies during coverage**: `_DeferredCoverageContext.resolve`
in `deferred_intents.py` returns ambiguous for two or more matches and
`constraint_holds` then fails, so a set fused
at admission under a unique match is genuinely unsatisfiable at
coverage. The codebase already declines this aliasing for transforms, keeping
future-transform identities existential; the aliasing applies to sources and
sinks only.

Residual risk, recorded and requiring a fixture: two option constraints
on `PluginSubject(source, "csv")` stated **before** a second csv-named
source is reviewed may fuse under the then-unique match and falsely
reject. The source/sink asymmetry needs a test either way — either the
fusion is correct because coverage will also fuse, or the asymmetry is
a defect; the fixture decides which.

### Rejection path (normative)

- A contradiction rejection MUST route through
  `apply_deferred_clarification` so the instruction is retained as
  clarification debt (R2-F15, `elspeth-a96b2f1b0a`). At adoption, a withdrawn
  iteration returned `DeferredRequestUnchanged`; that was the regression this
  contract required corrected.
- A contradiction rejection MUST render a distinct, actionable message
  naming the conflicting retained intent. The model is the planning
  blocker message in `guided/planning.py`, which names the exact intent and the
  edit/cancel recourse; the anti-model is a collapsed catch-all.

### Verification (normative)

The checker carries an exhaustive small-model soundness test against
`validate_deferred_intent_action` through its public API only:
enumerate small constraint models, brute-force their satisfiability,
assert the checker **never rejects a satisfiable model**, and assert
every rejection names a closed rule. The checker is sound and
deliberately incomplete; the test proves the first property and
documents the second. The prior oracle test asserted solver internals
and is deleted with them.

## Consequences

### Deletions

- `_ConstraintProofBudgetExceeded` and both budget constants.
- `_minimum_exact_compatible_partitions`.
- The gate-witness bipartite matching, replaced by rule 4's exact-subject form.
- `minimum_compatible_plugin_witnesses` and its `group_is_compatible`
  closure.
- The global cardinality and alias-slot arithmetic, replaced by rules 3 and 4.
- The `constraint_proof_budget_exceeded` rejection reason.

The governing principle, recorded so the deletion is not re-litigated
piecemeal: a fail-closed tier must reject what it cannot prove safe;
this tier rejected what it could not prove unsafe, inverting the gate's
own default (release admits everything). No future extension of this
checker may convert "unproven" into "rejected".

### Documented behaviour changes

- Four tests asserting cardinality-forced-merge rejections are deleted
  **with this declination recorded**. The constraint sets they rejected become
  admitted and are caught at wire confirmation.
- Two admit tests that depended on optimistic alias merging keep
  admitting; the contract introduces no over-rejection.
- The predicate-gate invert case is retained through rule 4's exact-subject
  form.

### What improves, what is given up

- Admission cost is bounded by simple grouping over the conjunction; a
  session no longer degrades as it accumulates valid instructions.
- Every rejection is diagnosable: it names a closed rule and the
  conflicting retained intent, and the instruction is retained as
  clarification debt rather than dropped.
- The checker is exhaustively verifiable by small-model enumeration —
  a property no bounded satisfiability solver can offer.
- Given up, deliberately: some genuinely unsatisfiable sets are
  admitted and surface only at wire confirmation, as an HTTP 409 with
  edit/cancel recourse, later than a complete solver would sometimes
  have caught them.

### Delivery

Implemented in three lanes. **A:** this checker (closes
`elspeth-d293c5d139`; no schema change). **B:**
`StatedOptionValueConstraint`, prose grounding, projection, and
`SESSION_SCHEMA_EPOCH` 43→44 with `sessions.db` recreation at landing
(`elspeth-826765af90`). **C:** custody and prepublication hardening
(own review). This ADR governs lane A's semantics and binds lane B's
checker extension to the same closed rules.

## Alternatives considered

### Complete the solver: global subject-to-witness assignment

**Rejected.** Deciding joint satisfiability with existential subjects,
cardinality caps, and alias unification is an NP-hard decision
procedure sitting inside an interactive admission gate. The review
surface is unbounded — four candidates were withdrawn in four hours,
each repair exposing the next soundness defect — and any resource bound
reintroduces a budget cliff, which converts "unproven" into "rejected"
and recreates the defect this ADR exists to remove.

### Exact-identity-only subject keying (drop the single-match aliasing)

**Rejected.** It silently drops cross-alias domain-exhaustion
detection — rule 7 stops seeing that constraints stated against the
plugin name and against the exact identity concern one component — and
it diverges admission keying from coverage keying, so admission would
pass sets that coverage then fuses and fails. Keying drift between the
two ends of one contract is precisely the class of defect the retained
rule closes.

### Discard all withdrawn work and reimplement from release

**Rejected.** It discards verified salvage: a pre-existing provider
over-disclosure fix (raw deferred prose no longer reaches the provider;
`src/elspeth/web/sessions/routes/composer/guided.py`) and the grounding work
that makes safe un-redaction of stated option values possible.

## Related decisions

- ADR-032: Boundary Validation Splits by Trust Domain — fixes where
  this ADR sits. Parsing untrusted prose into owned typed constraints
  is external-boundary work under ADR-032; deciding the joint
  feasibility of already-parsed owned constraints is internal
  semantics, governed here.

## References

- `elspeth-d293c5d139` — the originating ticket: "a small closed
  conjunction checker". The advisory review findings and maintainer scope
  reset are recorded on this ticket (2026-08-04).
- `elspeth-a96b2f1b0a` — R2-F15: instructions are never silently
  dropped; the retention requirement the rejection path must satisfy.
- `elspeth-826765af90` — lane B: `StatedOptionValueConstraint`, prose
  grounding, projection, schema epoch 43→44.
- Commit `449c93397` and the ticket comment trail on
  `elspeth-d293c5d139` preserve the scope reset.
- Commit `b803879cb` landed the governed gate and regression suite in
  `src/elspeth/web/composer/guided/deferred_intents.py` and
  `tests/unit/web/composer/guided/test_deferred_intent_closed_conjunction.py`.
- `src/elspeth/web/composer/guided/planning.py` and
  `src/elspeth/web/sessions/routes/composer/guided.py` implement the independent
  fail-closed net at wire confirmation.

## Erratum (2026-08-04)

The rejection-path clause above requires contradiction rejections to
route through `apply_deferred_clarification` and records the
`DeferredRequestUnchanged` return as a regression to correct. As
adopted (lane A, release merge `50d57e07d`), the EDIT path stands
corrected against that clause: on the EDIT path a contradiction
returns `DeferredRequestUnchanged` with a distinct named refusal, and
the original deferred intent is retained intact. The retention-net
routing clause's anti-model was the silent drop of the new
instruction (R2-F15); an EDIT-path refusal that leaves the targeted
intent exactly as it was and names the conflict drops nothing. This
is the adopted form.
