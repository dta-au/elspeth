# Row Union Example — Fork-Based A/B Experiment

Demonstrates the `row_union` barrier: a correlated, same-row, N-to-N UNION ALL join
over fork branches. It is the construct that makes fork-based A/B experiments
expressible, because it preserves row cardinality instead of collapsing it.

## What This Shows

Every support ticket is forked into two experiment arms. Each arm tags its own
`prompt_variant` and computes a `score`, so the two tokens differ in content while
sharing a `row_id`. The `row_union` waits for both arms of a ticket, then releases
**both tokens as one indivisible group** — 8 tickets in, 16 scored rows out.

```
source ─(routed)─> [variant_fork] ─┬─ control_branch ───> tag_control ──────┐
                                    └─ treatment_branch ─> tag_treatment ────┤
                                                                             ├─ [variant_union]
                                          ─(experiment_in)─> [prompt_experiment] ─> output
```

The released stream is in **long format** — one row per observation, with a
discriminator column naming the variant:

| id | prompt_variant | score |
|----|----------------|-------|
| 1  | control        | 60.0  |
| 1  | treatment      | 75.0  |
| 2  | control        | 72.0  |
| 2  | treatment      | 90.0  |

That is exactly the shape `batch_experiment_compare` consumes.

In production the two branches would be LLM calls with different prompts. They are
deterministic expressions here so the example runs offline with no external services.

## Running

Run one settings file at a time:

```bash
elspeth run --settings examples/row_union_ab_experiment/settings.yaml --execute
```

## Variants

Five configurations, each exercising a different part of the barrier's contract.

### 1. `settings.yaml` — pooled comparison (start here)

Dict-form branches with a per-variant transform chain, feeding
`batch_experiment_compare`. 8 tickets → 16 long-format rows → one comparison row.

### 2. `settings_paired_preference.yaml` — within-ticket comparison

The same topology feeding `batch_paired_preference`, which joins on
`pair_field: id` and compares the two variants *within* each ticket.

```bash
elspeth run --settings examples/row_union_ab_experiment/settings_paired_preference.yaml --execute
```

This is the sharpest test of what `row_union` guarantees: a ticket whose two
variants were split across batch boundaries would silently contribute nothing to
the tally. Uses `paired_input.csv`, whose explicit per-ticket `treatment_delta`
makes the outcome mixed — 6 wins, 2 losses, `win_rate 0.75` — because a uniform
sweep could not distinguish working pairing from broken pairing.

### 3. `settings_screened.yaml` — screen before the fork

A production-shaped topology where a `quality_screen` gate, wired ahead of
`variant_fork`, routes tickets below a quality floor straight to a
`screened_out` sink — before either experiment arm ever exists.

```bash
elspeth run --settings examples/row_union_ab_experiment/settings_screened.yaml --execute
```

Screened tickets never fork at all, so `row_union` never has a partial group
to worry about for them. The run ends **SUCCESS** (exit 0):

```
✓ Run COMPLETED: 8 rows processed | ✓4 succeeded | ✗0 failed | ⚠0 quarantined | →3 routed (screened_out:3)
```

3 of 8 tickets are screened out, so the comparison is computed over the 5
surviving tickets — `baseline_count`/`variant_count` are both 5 and
`batch_size` is 10 (5 tickets × 2 variants), the correct answer, not a
silently biased one. Screening ahead of the fork is the only shape a
screening predicate known before the fork may legally take inside a bound
group (spec §7 rule 4 rejects a gate that could route to a sink from
*inside* the region — see `settings_screened_at_settlement.yaml` below for
the case where the predicate is only knowable after the fork).

### 4. `settings_screened_at_settlement.yaml` — screen after the fork, mid-branch

The companion to #3: here the screening predicate is `score`, a field
`tag_control` computes — it does not exist before the fork, so the screen has
to live *inside* the control branch, between `tag_control` and the barrier.
It routes failing tickets to the virtual `discard` sentinel rather than a
sink (a real in-region sink route is rejected flat by spec §7 rule 4;
`discard` draws no graph edge at all, so it is invisible to that walk).

```bash
elspeth run --settings examples/row_union_ab_experiment/settings_screened_at_settlement.yaml --execute
```

This is the fail-closed cost the pre-ruling-23 shape of this example used to
demonstrate: a discarded control token orphans its treatment sibling (already
billed into `variant_union`'s roster), so `row_union` — require-all — fails
that ticket's group closed rather than releasing half of it. The run ends
`PARTIAL` (verified):

```
⚠ Run PARTIAL: 8 rows processed | ✓4 succeeded | ✗3 failed | 0.4s total
```

This designed `PARTIAL` result returns process exit 1.

3 of 8 tickets fail this way (same three as #3's quality floor: 45, 53, 58);
the comparison is still computed over the 5 surviving tickets
(`baseline_count`/`variant_count` both 5, `batch_size` 10). Screened rows are
never written to a sink here — the control token is discarded before
producing one — but the loss is recorded, not merely inferable: the
WS2-era per-branch ledger `coalesce_branch_losses` carries the actual row
ids, branch names and `gate_discarded` reasons for all 3 losses (the
unified `group_losses` table supersedes this ledger once WS3's
settle-member seam lands; until then, this is the honest recovery path):

```bash
sqlite3 examples/row_union_ab_experiment/runs/settlement.db \
  "SELECT row_id, branch_name, reason FROM coalesce_branch_losses
   WHERE coalesce_name = 'variant_union';"
```

### 5. `settings_identity_branches.yaml` — list-form branches

The ergonomic list form, `branches: [replica_a, replica_b]`, where each branch
runs straight from the fork gate to the barrier with no transform chain.

```bash
elspeth run --settings examples/row_union_ab_experiment/settings_identity_branches.yaml --execute
```

This isolates the cardinality contract: no merging, no field synthesis, no
per-branch processing — 8 source rows become 16 released tokens, all written.
Contrast with [`fork_coalesce`](../fork_coalesce/), where the same fork shape
yields 8 rows because coalesce collapses each pair into one.

Use the list form when both branches carry identical payloads (redundancy
fan-out, or duplicating a row for two sinks); use the dict form when each branch
needs its own transform chain.

## Output

One comparison row in `output/experiment_comparison.json`:

```json
{
  "baseline_variant": "control", "variant": "treatment",
  "baseline_mean": 63.75, "variant_mean": 79.6875,
  "mean_delta": 15.9375, "relative_lift": 0.25,
  "standard_error": 6.749, "z_score": 2.361,
  "confidence_95_low": 2.709, "confidence_95_high": 29.166,
  "baseline_count": 8, "variant_count": 8, "batch_size": 16
}
```

`batch_size: 16` is the point of the example — all 16 branch tokens reached the
statistics plugin as one stream, paired by ticket.

## Why `row_union` And Not Coalesce Or A Queue?

The three fan-in primitives differ along two axes — whether they correlate on
`row_id`, and whether they change row cardinality:

| Primitive | Correlated? | Cardinality | Use when |
|-----------|-------------|-------------|----------|
| `coalesce` | Yes, per `row_id` | N → 1 (merges fields) | You want one wide row combining both branches |
| `queue` | No | pass-through | You are interleaving independent streams |
| `row_union` | Yes, per `row_id` | N → N (no merge) | You want one row per branch, kept together |

**Coalesce would collapse the experiment.** Its `union` merge produces a single wide
row per ticket (`control_score` and `treatment_score` as separate columns). The batch
statistics plugins cannot consume that — they need one row per observation plus a
discriminator column. Reshaping wide-to-long inside the pipeline is not possible, so
before `row_union` this scenario had no in-pipeline expression at all.

**A queue would not keep the pair together.** Queues are uncorrelated: they interleave
whatever arrives, with no guarantee a ticket's two variants stay adjacent or even land
in the same batch. `row_union` correlates on `row_id` and releases the pair as one
unit, so a batch boundary can never fall between a ticket's control and treatment rows.

## Key Concepts

**Group indivisibility.** A released group is atomic. Aggregations downstream of a
`row_union` may therefore use only the implicit end-of-source trigger (`trigger: {}`);
a `count`, `timeout`, or `condition` trigger could fire between a ticket's two variants
and split the group, so the graph builder rejects it at build time with a diagnostic.

**Declared branch order is release order.** `control_branch` is declared first, so
control precedes treatment for every ticket — and that ordering is durable, not an
artifact of which branch happened to finish first.

**Require-all, fail closed.** v1 waits for every declared branch. If a branch is lost
(error-routed, filtered, quarantined, retry-exhausted), the whole group fails closed
with an audit record naming the loss — never a partial release, and never a silent
half-group statistic.

**Payloads pass through untouched.** The barrier merges nothing. Each token keeps its
own `token_id`, `row_id`, `branch_name`, and payload; the variant discriminator is
authored upstream by the branch transforms, not synthesised by the barrier.

## Inspecting The Audit Trail

Every branch token is adjudicated at the barrier — 16 completed node states for
8 released groups:

```bash
sqlite3 examples/row_union_ab_experiment/runs/audit.db \
  "SELECT node_id, status, COUNT(*) FROM node_states
   WHERE node_id LIKE 'row_union%' GROUP BY 1, 2;"
```

The durable scheduler journal records which barrier each token was held at:

```bash
sqlite3 examples/row_union_ab_experiment/runs/audit.db \
  "SELECT row_union_name, barrier_key, COUNT(*) FROM token_work_items
   WHERE row_union_name IS NOT NULL GROUP BY 1, 2;"
```

## See Also

- [`fork_coalesce`](../fork_coalesce/) — the N-to-1 field-merging sibling, with merge
  policies and collision strategies
- [`multi_source_queue`](../multi_source_queue/) — uncorrelated fan-in
- [`statistical_batch_plugins`](../statistical_batch_plugins/) — the batch statistics
  plugins consuming long-format data, including `batch_paired_preference`, which this
  topology also feeds by adding a `pair_field`
