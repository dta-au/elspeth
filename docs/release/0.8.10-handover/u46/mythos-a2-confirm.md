**Verdict: not landable as designed.** Three gaps still break the acyclic-equality pin or drop a row. All are small textual fixes to §8.5. Lines cite `docs/release/0.8.10-fencing-gate-diagnosis.md` (§) and the gate at 036a708f5 (gate:).

## 1. Previous findings

| Finding | Status | Where |
|---|---|---|
| §8.6 proved R_fix ≥ R_dfs, not rows | RESOLVED | §8.6 L636–654 [R1] |
| Acyclic exactness false, no collapse | RESOLVED | §8.2 L530–535 [R2] |
| Rule 3 scoped too wide | RESOLVED | §8.2 L526–528, §8.5 L610–622 [R3][R4] |
| Call override gave two origins | PARTIAL | §8.5 L599–606 [R5], see §3 below |
| ClassDef producer edge | RESOLVED | §8.3 L553–555 [R6] |
| Formal defaults | PARTIAL | edge fixed §8.3 L548–550; value rule missing, see §3 |
| Cache, budget, evidence | RESOLVED | §8.4 L577–586 [R7], §8.8 L707–739 |

Retraction: my earlier "roots use read_target=True" was wrong and §8.4 L578 copied it. `retained_expression` defaults False (gate:12215); only the AugAssign target passes True (gate:12476). Key the cache by the value the consumer actually passes.

## 2. Elementwise rows

Sound for every consumer, by one argument rather than per consumer. Every root is evaluated with an empty path (gate:12213), so each operand's DFS result is one fixed tuple. By I1 that tuple is in the product, so "deny if any combination denies" reproduces the DFS verdict. No consumer yields fewer denials, including the negative tests.

Two conditions the implementation must honour:

- **Exemptions are per combination, never per element.** The equality and non-membership exemptions at gate:12512 (metadata_query), 12603–12611 (preserving_conversion), 12620–12626 (identity_consumer) and 12634–12668 (disclosure, control_options) must not be read as "exempt if any element qualifies". Probe: a callee set {metadata-query, type-metadata} must deny.
- **No per-argument decomposition.** `enum_arguments[0]` at gate:12614 couples arguments, so the product cannot be reduced to per-argument any/all.

Blow-up: disclosure_consumer has up to 7 keyword factors, each up to 2|O| at an A-false root. §8.6 states no bound, and (b) counts rule evaluations inside the evaluator, not consumer combinations. Add a fail-closed cap counted against (b): over the cap, emit the row.

Precision defect: §8.6 L642–644 unions both branches at every A-false root. By I2, R(root)=False means R_dfs is False on every path, so the retained branch is spurious there. It injects enum-compound, which is a retained_supplier_kind, so the [R3] probe `isinstance(x, E)` with cyclic x fires the suppression inside yet still gets a "stored in unclosed carrier" row at the root. Gate the retained branch on R(root).

## 3. Exactness on G-acyclic roots

Not yet. Counterexamples:

- **[R5] None case.** The DFS override at gate:12138–12141 fires when `callee == scoped_expression(func)` including None == None, guarded only by the call's own static origin being non-None. §8.5 L601–603 says "that static origin is not None", which reads as the callee's. Under that reading f(None) = None, and a Call with a registered static origin (gate:8215 registers one on a Call) loses it. Acyclic: inequality. Cyclic with a retained_supplier_kind origin: a dropped row.
- **Arg producer has no transfer rule.** §8.3 fixes the edge, but §8.5 never states the value. The DFS at gate:12030–12046 is an override: static origin unless None or "unknown", then the paired default's origin. As a union, `def f(x=A)` gives |O| = 2, the A-true collapse turns it into None, and the DFS gives the static origin. Needs a per-element f(d) like the Call rule.
- **Call static supplier-scalar early return** at gate:12164–12165 is not in rule 3. On acyclic input the design's `R(func) or … or any(R(arg))` returns True where the DFS returns False.
- **Store-ctx leaf.** gate:12004–12005 returns a leaf for a Store Name when read_target is False. §8.3's Name edge is unconditional, so walrus and comprehension targets gain producer edges, manufacture cycles, and misclassify roots in the §8.8 split. Precision only, but it corrupts the corpus measurement.

Also, I3 states O only. §8.6 L645–646 selects one branch at an A-true root, which needs R(k) = R_dfs(k) stated and proved.

## 4. New in the revision

- §8.7 L680–681 "no suppression can fire inside an SCC" is false: the callee of a member Call can be a final A-true singleton outside the SCC. Monotonicity still holds because the predicate is constant over final inputs. Fix the argument, not the design.
- `retained_callable_capture(node)` at gate:12483 is a boolean root outside §8.6. State that it is served by the capture node's R.
- A never-finalising SCC re-trips the budget on every root that reaches it. Bounded at roots × budget, but say so.

## 5. Verdict

Not landable as designed. Blocking amendments to §8.5: the [R5] None clause, a per-element arg rule, and the Call static supplier-scalar suppression. Textual amendments: Store-ctx leaf in the edge function, R-gated branch union, capped and budget-counted product, the capture root, I3 R-exactness, the read_target key, and the §8.7 monotonicity argument. Add probes for each blocking item: an acyclic `def f(x=A)` and a Call with a registered static origin, both requiring DFS equality. Then implement and verify per §8.8.

A copy is in `~/.claude/plans/defensive-security-review-of-virtual-island.md`. No repository files were changed.
