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

```bash
elspeth run --settings examples/row_union_ab_experiment/settings.yaml --execute
```

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
