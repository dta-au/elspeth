# ADR-033: Deferred-Intent Admission Decides Closed Contradiction, Not Satisfiability

**Date:** 2026-08-04
**Status:** Accepted
**Deciders:** John Morrissey, Claude Fable 5
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
Unless stated otherwise, file and line references in this ADR are to
branch `codex/fix-grounded-option-constraints` at commit `ef73bfa70`,
which carries the code this contract governs.

Under ADR-032 the parse and the decision are different work in
different trust domains. Parsing untrusted operator prose into
ELSPETH-owned typed constraints is external-boundary work: value
assertions, construction of an owned type, original discarded. Once
parsed, the constraints are owned values; deciding whether a
conjunction of owned values is jointly satisfiable is not boundary
validation at all — it is a semantic decision procedure, and it needs
its own contract. This ADR is that contract.

### What was asked for, and what was built

Tracker `elspeth-d293c5d139` asked for a small closed conjunction
checker: reject an equals/equals mismatch, an equals plus a matching
not_equals, and incompatible count bounds. The delivered branch instead
built a bounded exact-partition satisfiability solver: witness
unification across ambiguous plugin subjects, alias-slot counting
against cardinality caps, and a proof budget
(`_MAX_ALIAS_PROFILE_SUBJECTS = 12`, `_MAX_ALIAS_PARTITION_STEPS =
50_000`; `deferred_intents.py:1606-1611`).

A seven-lens SME panel (security, static-analysis rule design, solution
scope, defect triage, test suite, systems leverage, cross-lane
recurrence) unanimously rejected the solver. The decisive evidence:

- **Probe-verified false rejection of trivially satisfiable input.**
  Thirteen same-plugin subjects under `at_most 13` — satisfiable by
  inspection — were rejected with `constraint_proof_budget_exceeded` at
  a hard cliff of `_MAX_ALIAS_PROFILE_SUBJECTS + 1`
  (`deferred_intents.py:1610`, raise at `:1623-1624`).
- **Monotone session degradation.** The budget is spent on the whole
  prospective conjunction — every constraint already retained in the
  session plus the new one (`:2227-2232`) — so accumulated history
  consumes it. A session that retains enough valid instructions
  eventually rejects every further instruction, and nothing the
  operator does in-session recovers it.
- **Undiagnosable, retention-bypassing rejection.** All contradiction
  and budget rejections collapse to one generic message
  (`src/elspeth/web/sessions/routes/composer/guided_chat_intent_management.py:226-229`)
  and return `DeferredRequestUnchanged` (`:427-428`) instead of routing
  through `apply_deferred_clarification` — bypassing the R2-F15
  retention net ("instructions are never silently dropped",
  `elspeth-a96b2f1b0a`).
- **Unbounded review surface.** Four candidate implementations
  (`17ab6e88e`, `5511eb5f5`, `42a0b158a`, `ef73bfa70`) were withdrawn
  over four hours; each repair exposed a further soundness defect in
  the partition or alias arithmetic.

### Admission is not the safety control

Downstream safety is independent of admission.
`verified_remaining_deferred_intents`
(`src/elspeth/web/composer/guided/planning.py:2058`) re-verifies
mechanical coverage of **all** retained intents against the concrete
proposal, and wire confirmation refuses with HTTP 409 while any remain
(`src/elspeth/web/sessions/routes/composer/guided.py:3043-3051`), with
explicit operator edit/cancel recourse. An unsatisfiable set that
admission misses therefore wedges fail-closed at wire confirmation; it
cannot reach execution. Admission is a diagnostics and efficiency gate
— catch the contradiction while the operator is still talking about it
— not a security control. The cost of under-rejection is a late,
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
   asserted both true and false (`:1793-1794`); a subject required by
   another retained constraint while asserted absent (`:1795-1796`);
   two component kinds for one subject (`:1803-1804`); two plugin
   identities for one subject (`:1805-1806`); option equals/equals
   mismatch and equals plus a matching not_equals per
   `(subject, option_path)` (`:1858-1872`); two distinct stated gate
   predicate signatures on one subject (`:1799-1800`); two distinct
   routing target pairs for one predicate (`:1801-1802`); a
   failure-route equals target conflicting with a second equals or a
   matching not_equals per `(subject, failure_kind)` (`:1849-1856`); an
   edge asserted both present and absent for one `(from, type, to)`
   triple (`:1797-1798`).
2. **Count bounds.** Empty intersection within one count key —
   `at_least` above `at_most`, `equals` outside the bounds, two
   distinct `equals` (`:1957-1969`); a plugin identity asserted absent
   while a count requires at least one (`:1976-1985`); an upper bound
   of zero on a component kind or plugin identity that a required
   subject inhabits (`:1987-2004`).
3. **Cross-group count subsumption, closed identity-free form.** Where
   one count group is contained in another — a plugin-scoped group
   within its component-kind group — the required minima of the
   contained groups sum against the containing cap. A global node
   `at_most 1` alongside `transform:normalize at_least 2` is a
   contradiction. On `ef73bfa70` this arithmetic is entangled with
   witness counting (`:2013-2038`, `:2108-2124`); the retained rule is
   the arithmetic core with no witness or alias resolution.
4. **Exact-subject predicate-implied gate counting.** Distinct stated
   predicate signatures on distinct **exact** subjects each imply one
   synthesised gate node, counted against node-kind caps (collection at
   `:1810-1847`, consumed at `:2119-2124`). Declined for ambiguous
   plugin subjects: a gate implied by a subject that has not resolved
   to an exact identity is not counted.
5. **Presence false versus implicit requirement.**
   `SubjectPresenceConstraint(present=False)` conflicts with any other
   constraint that implicitly requires that subject: option constraints
   (`:1738`); edge routes, which require both endpoints regardless of
   the edge's own present flag (`:1784-1785`); stated predicates
   (`:1745`); failure routes (`:1789-1791`); and, at plugin-identity
   level, a globally absent identity intersecting a required identity
   (`:1807-1808`). This rule fixes the reach of the requirement
   relation that rule 1's required-but-absent check consumes.
6. **Single-subject option-path prefix/descendant collapse.** A scalar
   `equals` at path P conflicts with any constraint at a strict
   descendant of P on the same subject. On `ef73bfa70` this is
   reachable only inside the deleted partition path (`:1884-1891`); it
   is lifted to a standalone single-subject rule.
7. **Finite-domain exhaustion.** A `not_equals` set that exhausts a
   validated finite schema domain per `(subject, option_path)`
   (`:2158-2183`, `_option_schema_conjunction_is_consistent`).

### Subject keying: keep the single-match plugin-name aliasing

`_constraint_subject_key` (`:1589-1597`) resolves a plugin-name subject
to a stable identity exactly when exactly one reviewed or
pending-intent component of that kind bears the plugin name. This
aliasing is retained. It was adversarially verified sound because it is
the **same resolution rule coverage applies**: at planning time,
`_DeferredCoverageContext.resolve` (`:2341-2361`) returns ambiguous for
two or more matches and `constraint_holds` then fails, so a set fused
at admission under a unique match is genuinely unsatisfiable at
coverage. The codebase already declines this aliasing for transforms
(`:1573-1580`), keeping future-transform identities existential; the
aliasing applies to sources and sinks only.

Residual risk, recorded and requiring a fixture: two option constraints
on `PluginSubject(source, "csv")` stated **before** a second csv-named
source is reviewed may fuse under the then-unique match and falsely
reject. The source/sink asymmetry needs a test either way — either the
fusion is correct because coverage will also fuse, or the asymmetry is
a defect; the fixture decides which.

### Rejection path (normative)

- A contradiction rejection MUST route through
  `apply_deferred_clarification` so the instruction is retained as
  clarification debt (R2-F15, `elspeth-a96b2f1b0a`). The branch
  currently returns `DeferredRequestUnchanged`
  (`guided_chat_intent_management.py:427-428`); that is a regression
  this contract requires corrected.
- A contradiction rejection MUST render a distinct, actionable message
  naming the conflicting retained intent. The model is the planning
  blocker message (`guided/planning.py:786-795`), which names the exact
  intent and the edit/cancel recourse; the anti-model is the collapsed
  catch-all (`guided_chat_intent_management.py:226-229`).

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

- `_ConstraintProofBudgetExceeded` and both budget constants
  (`:1606-1611`).
- `_minimum_exact_compatible_partitions` (`:1614-1653`).
- The gate-witness bipartite matching (`:1810-1847`), replaced by
  rule 4's exact-subject form.
- `minimum_compatible_plugin_witnesses` and its `group_is_compatible`
  closure (`:1874-1955`).
- The global cardinality and alias-slot arithmetic (`:2006-2126`),
  replaced by rules 3 and 4.
- The `constraint_proof_budget_exceeded` rejection reason (`:217-218`,
  `:235`, `:2239-2240`).

The governing principle, recorded so the deletion is not re-litigated
piecemeal: a fail-closed tier must reject what it cannot prove safe;
this tier rejected what it could not prove unsafe, inverting the gate's
own default (release admits everything). No future extension of this
checker may convert "unproven" into "rejected".

### Documented behaviour changes

- Four tests asserting cardinality-forced-merge rejections are deleted
  **with this declination recorded**
  (`tests/unit/web/composer/guided/test_grounded_option_constraints.py:2318`,
  `:2588`, `:2622`, `:2657`). The constraint sets they rejected become
  admitted and are caught at wire confirmation.
- Two admit tests that depended on optimistic alias merging keep
  admitting (`:2290`, `:2368`); the contract introduces no
  over-rejection.
- The predicate-gate invert case (`:2151`) is retained through rule 4's
  exact-subject form.

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

### Abandon the branch and reimplement from release

**Rejected.** It discards verified salvage: a pre-existing provider
over-disclosure fix (raw deferred prose no longer reaches the provider;
`src/elspeth/web/sessions/routes/composer/guided.py:3720-3732`,
`:4766-4776`) and the grounding work that makes safe un-redaction of
stated option values possible.

## Related decisions

- ADR-032: Boundary Validation Splits by Trust Domain — fixes where
  this ADR sits. Parsing untrusted prose into owned typed constraints
  is external-boundary work under ADR-032; deciding the joint
  feasibility of already-parsed owned constraints is internal
  semantics, governed here.

## References

- `elspeth-d293c5d139` — the originating ticket: "a small closed
  conjunction checker". The seven-lens panel verdict and scope reset
  are recorded on this ticket (2026-08-04).
- `elspeth-a96b2f1b0a` — R2-F15: instructions are never silently
  dropped; the retention requirement the rejection path must satisfy.
- `elspeth-826765af90` — lane B: `StatedOptionValueConstraint`, prose
  grounding, projection, schema epoch 43→44.
- The seven-lens panel synthesis is preserved in the maintainer-local
  docs archive (untracked; it does not survive a fresh clone). The
  durable record is the ticket comment trail on `elspeth-d293c5d139`.
- `src/elspeth/web/composer/guided/deferred_intents.py` @ `ef73bfa70`
  (branch `codex/fix-grounded-option-constraints`) — the governed gate.
- `src/elspeth/web/composer/guided/planning.py:2058`,
  `src/elspeth/web/sessions/routes/composer/guided.py:3043-3051` — the
  independent fail-closed net at wire confirmation.

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
