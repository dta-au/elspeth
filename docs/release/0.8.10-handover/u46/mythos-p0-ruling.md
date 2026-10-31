The gate's hot path is not quite where the brief places it. `_mutation_callable_escapes` (gate `:19883`) constructs `_reserve_ReserveProof` at `:19886`, whose `__init__` at `:17634` runs `_reserve_selected_origin_effect_failures`. The exploding evaluator is `retained_enum_state` at `:11970`, the supplier-enum retention proof. That matters for the questions: the non-terminating query is a *reserve* proof, and the escape walk is hostage to it.

**Where the gate stands today.** The escape test carries `landscape_long_run`, which the default marker expression at `pyproject.toml:486` does not deselect, and addopts set no pytest timeout. So the gate is neither red nor green on the target. A local run hangs. CI hangs until the job timeout and dies with no attributable row. That is fail-absent, and option (b) is what restores a verdict at all.

## 1. Soundness

**Current recursion is sound on cycles.** The cut at `:12019–12020` returns `(None, False)` only for the re-entered sub-path. The node's outer frame is still on the stack and ORs every child (`:12112`, `:12171–12174`, `:12186`), so any retained leaf reachable by a simple path is found. The cut pushes a Name's `origins` set toward `None` (`:12111–12112`), and a `None` origin *disables* the only annihilators in the function, the supplier-scalar returns at `:12132–12133`, `:12150–12165`. Cuts therefore bias toward "retained". The invariant "origin in `retained_enum_kinds` implies retained" holds by construction at `:12028`, `:12056`, `:12134`, `:12170`, `:12185`, so losing an origin never drops a True. Over-approximating, not unsound. The memo itself is exact: an entry is consulted only on equal `seen ∩ footprint` (`:12003`), and `seen` is read only through `retained_path_contains` (`:11946`), so equal intersections mean identical executions. The blow-up is distinct contexts, not wrong hits.

**Option (a) is sound only if decomposed.** Compute `retained` as the least fixpoint of OR over the binding graph. Set `origin = None` for any node whose producers include an SCC-mate. Apply supplier-scalar suppression only to origins proven acyclically. Under those three rules the fixpoint is at least as strict as the DFS, because every DFS cut is an intra-SCC edge and the fixpoint treats all intra-SCC edges as cuts. Precision loss: a node the DFS proves scalar on every simple path becomes retained when a cycle-mate is. Soundness loss: iterating the uniqueness join at `:12112` or the suppression non-monotonically, where a transient unique "supplier-scalar" freezes a False that a later iteration would have flipped. Start from `(None, False)`, never from a specific origin.

**The reviewed contract blocks (a) as written.** `test_web_reserve_reviewed_contracts.py:6850–7200` pins `two == one == none` to the DFS (`:7172`, `:7161–7163`). A fixpoint cannot satisfy exact equality in general. Replacing that contract is a recorded decision: pin fixpoint ⊇ DFS on the synthetic probes and exact row-set equality on the corpus.

## 2. Option (b) as a permanent net

Acceptable, and it should stay after (a). "Fail closed" must mean all of the following, or it drifts open:

- **Deterministic count**, in body evaluations (the `traces` frame count already recorded at `:7188`), never CPU or wall time. Machine load must not change a verdict.
- **Per-root reset** at `retained_empty_root_state` (`:12212`), the empty-path boundary.
- **Conservative value plus a named row.** The root's result becomes `(None, True)`, and a failure row "retention budget exceeded at path:line" flows through the T2 root-before-cascade path. Not an exception that kills `measure()`, not a log line. Note that the code's natural give-up value `(None, False)` is the fail-open value.
- **Never published** into `entries`; a budget-truncated witness would poison later exact hits.
- **No env or CLI override.** Raising the constant is a reviewed code change. `--report-only` already masks every row equally and is explicit.
- **Mutation control:** budget 1 turns a real file red with the named row. Pin the constant and the row format.
- **Contract interaction:** evaluation counts differ by memo policy, so a borderline probe could be red under "none" and green under "two". Run the reviewed-contract tests with the budget disabled and pin the budget in its own test.
- **Set the constant from data:** 550 roots completed at 42 evaluations or fewer, one at 5.4M (EVIDENCE.md). Record the distribution beside the chosen value.

## 3. Option (c)

A production refactor owned by U-44b (EVIDENCE.md line 63), not a gate fix. It also moves pinned fingerprints and may change results, since a formal parameter is analysed from its annotation (`:12031–12046`). The lane's own evidence shows the driver is distinct `(seen ∩ footprint)` contexts across a wide dataflow graph, not simple cycles (`scaling.jsonl` grows linearly; simple cycles do not reproduce it). So (c) alone leaves the gate exponential for the next long function.

## 4. Plan and evidence

Order: (b), then (a) under the amended contract, with (c) left to U-44b.

Evidence before landing:

- **Corpus set-diff.** `fencing_inventory.py --json` live rows on main, the target tip and the overlay, compared as sets (added and removed), never as counts.
- **Known positives.** The §5.4 plant must yield its three rows. Add a second plant inside `_execute_continuation_call_live` near `tool_batch.py:3758`, so the exploding path is itself a known positive.
- **Non-vacuity for (a).** Apply suppression inside an SCC on a synthetic probe and confirm a row drops and the test goes red.
- **Difference policy.** An added row is a precision loss, adjudicated individually. A dropped row is a soundness defect and blocks the landing.
- **Budget control.** Budget 1 on a real file goes red; the pinned constant on the corpus goes green with the plants still red.

A copy of this recommendation is in the plan file at `~/.claude/plans/defensive-security-review-of-shimmering-lemur.md`. No code or repository files were changed.
