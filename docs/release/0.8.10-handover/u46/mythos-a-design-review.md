The evaluator is not in this worktree (bf7d75ba5 is docs-only). `gate:` below cites `036a708f5:tests/unit/architecture/test_web_landscape_mutation_fencing.py`; `§` cites the design in `docs/release/0.8.10-fencing-gate-diagnosis.md`.

**Verdict.** The set-of-origins fixpoint is the right shape, and the engineer is right that my literal rule 2 was unsound. It is not landable as written: §8.6 proves the wrong theorem, acyclic exactness is false, and rule 3 is scoped too wide.

## 1. Soundness and the edge function

**§8.6 proves R_fix ≥ R_dfs, not "never fewer denials".** Rows are not emitted from R. They come from ~25 consumers of `retained_expression` (gate:12422–12799), which read the origin tuple. `retained_expression` (gate:12215–12233) returns `direct` whenever `direct[0] ∈ retained_supplier_kinds` (gate:9251), and that set holds ~15 kinds outside `retained_enum_kinds` (gate:11907): type-metadata, retained-supplier-namespace, framework-model, and so on. For those kinds R is False in the DFS and the denial rides on `direct` alone. On a G-cyclic root the fixpoint's `direct = unique(O) = None`, so two confirmed consumers lose rows: the namespace check at gate:12446–12456 denies on `("retained-supplier-namespace", q)` and does nothing on None, and the callable check at gate:12529–12545 denies on `target[0] ∈ retained_supplier_kinds` and does nothing on None. That is fewer denials than the DFS, on exactly the cyclic roots the design targets.

Fix: carry O to the root and emit rows elementwise (a consumer denies if any element denies), or produce a dominance table proving None and enum-compound deny wherever each specific origin denies. The second needs auditing every consumer; the first is the honest generalisation.

**Edge function.** Superset in the main, with two defects:

- **Nested ClassDef producer.** The DFS evaluates the class node through the generic branch (gate:12066–12071 calls `retained_enum_state(producer)`, falling to `iter_child_nodes` at gate:12186). §8.3 says "capture node". A capture walk sees only outer Load names, so `class C: X = SomeEnum.A` loses the Attribute retention. The edge must be the generic children (bases, keywords, decorators, body).
- **Formal defaults.** The DFS reads only the paired default (`if formal is producer`, gate:12040–12045). Reading every default is a superset but manufactures spurious cycles, which under rule 3 become precision loss.

Short-circuit (`retained or any(args)`, gate:12166–12176), the isinstance early return, `str()`'s read of `args[0]` (gate:12160), and Lambda capture-plus-body (gate:12116–12121) are all covered. Attribute module traversal (gate:12125–12131) reads only static tables.

## 2. The flagged deviation

**Rule 2 was unsound.** A DFS cut nulls one edge; my rule nulled every SCC member's origin. A member whose origin arrives through a value-independent edge (static Call origin, gate:12138–12144) keeps a specific `o ∉ K` in the DFS, and a downstream map sends it into K (gate:9303, `object → enum-value`). Conceded.

**Sets are sound and monotone inside an SCC.** Union, elementwise maps, and OR are monotone; with A False no suppression branch exists.

**But acyclic exactness is false as written.** The DFS collapses a multi-producer Name to None when origins disagree (gate:12111–12112). §8.5 unions without collapse, so an acyclic `x = A` / `x = B` gives |O| = 2 and the next map can retain where the DFS did not. §8.2's "on acyclic inputs every O is a singleton" is the broken claim.

**Precise invariant.** For every node k and every DFS path π reaching k:

- I1: `o_dfs(k, π) ∈ O(k)`, with a cut contributing None.
- I2: `R_dfs(k, π) ⇒ R(k)`.
- I3: `A(k) ⇒ O(k) = {o_dfs(k)}` after collapse, path-independent, equal to the DFS.
- I4: a suppression fires only if, for each input k′ it reads, `A(k′)` and `|O(k′)| = 1` and the sole element is the exact premise the DFS tests. `{None}` is a singleton and must not count.
- Collapse rule: at an A-true Name, `O := {unique(⋃)}`; at an A-false node, never collapse.

Rule 3 then cannot fire on a set containing None: inside a cyclic SCC None is in O from the start value and union never removes it; downstream, every map sends None to None (gate:9278, 10093), so A-false nodes keep it. None can also enter O at A-true nodes from an unresolved static source (gate:12016), exactly as in the DFS, which is why I4 tests the element, not just the cardinality.

**Rule 3 is scoped too wide.** The isinstance suppression reads only the callee (gate:12146–12153); arguments are ignored. Node-transitive A disables it whenever an argument is cyclic, so `isinstance(x, E)` in any loop over an enum-carrying variable becomes a new row. Direction is ⊇ so soundness holds, but the adjudication list will bloat. Require A and singleton on the inputs the suppression reads: callee for isinstance/issubclass, base for the Attribute scalar (gate:12132), callee plus `args[0]` for `str()`.

**Call override** (gate:12138–12144): enum-value from the callee, then overridden by the static origin if the callee equals `scoped_expression(func)`. Implemented as two unions it yields |O| = 2 on acyclic input. It must be a per-element function f(c).

## 3. Lattice and termination

Finite. Every map carries `qualified` unchanged or draws it from a static table (`supplier_enum_classes`, `bindings`, `selected`, `modules`); the only constructed strings are `"annotation dictionary " + attr` over three values (gate:9294) and `("member", q + "." + attr)` gated by `protected_receiver_method` (gate:9312). So |U| ≤ |kinds| × |qualified names in source and tables|. Per node at most |U| + 2 changes; per SCC at most |C| · (|U| + 2) · max in-degree evaluations. Monotone transfer functions give a unique least fixpoint regardless of worklist order.

## 4. Exactness and the per-unit cache

No path-dependence remains once collapse is confined to A-true nodes: G is static, Tarjan finalises an SCC only after its successors are final, so finality is closed under successors and a later root cannot add an edge into a finalised closure. Three obligations: key by `read_target` (roots use True, children False); keep the `eligible` guard on non-default inputs (gate:11989–11996); and when (b)'s budget trips mid-worklist, publish nothing from any non-final SCC. The DFS's "never published" rule carries over unchanged.

## 5. Evidence beyond §8.8

- **The 478,077 / 624 split is wrong.** Finished is not acyclic: G ⊇ the DFS's explored graph, so many finished roots are G-cyclic. Split the corpus by G-cyclicity from the edge function; equality on acyclic, ⊇ with adjudication on cyclic. Report the counts.
- Consumer dominance table, or elementwise rows, for the §1 seam, with a cyclic probe whose DFS origin is a non-K supplier kind (retained-supplier-namespace).
- Acyclic two-disagreeing-producers probe that must equal the DFS; a cyclic variant that must retain.
- Per-edge-class knockouts (Call keyword, Lambda body, ClassDef generic child, paired default, Match subject, with-item), each turning a named probe red.
- Start-value control: seeding a member with a static origin instead of `{None}` turns a probe red.
- Reversed-worklist determinism: identical row sets.
- Budget trip mid-SCC publishes nothing; a following root re-derives.

A copy is in `~/.claude/plans/defensive-security-review-of-serene-brook.md`. No repository files were changed.
