# Fork/Coalesce Example

Demonstrates the fork/join DAG pattern where a single row is duplicated into parallel tokens, processed independently, and merged back together.

## What This Shows

After pre-processing (truncating long descriptions), every row is forked into two parallel tokens. Both tokens traverse to a coalesce point that waits for both to arrive, then merges them using the `nested` strategy — the output contains each branch's data under a separate key.

```
source ─(raw)─> truncate ─(preprocessed)─> [fork_gate] ─┬─ path_a ─┐
                                                          └─ path_b ─┤
                                                                     ├─ [merge_results]
                                                           (merged) ─> [route_output] ─> output
```

## Running

**Basic fork/coalesce (no per-branch transforms):**
```bash
elspeth run --settings examples/fork_coalesce/settings.yaml --execute
```

**Per-branch transforms variant (ARCH-15):**
```bash
elspeth run --settings examples/fork_coalesce/settings_per_branch.yaml --execute
```
> **Note:** Per-branch transforms are an ARCH-15 feature and validate through the same CLI path as the basic variant.

## Variants

This example has two configuration files demonstrating different fork/coalesce patterns:

### 1. `settings.yaml` — Basic Fork/Coalesce (Direct Wiring)

Uses the **list format** for branches: `branches: [path_a, path_b]`

Both fork paths carry identical data to the coalesce — no transforms run on the branches. This demonstrates the basic merge barrier pattern.

**DAG:**
```
source ─> truncate ─> [fork] ─┬─ path_a ─┐
                               └─ path_b ─┤
                                          ├─ [coalesce] ─> output
```

**Use case:** Redundancy (send to 3 LLMs, take quorum) or fan-out pattern setup.

### 2. `settings_per_branch.yaml` — Per-Branch Transforms (ARCH-15)

Uses the **dict format** for branches: `branches: {path_a: truncated_a, path_b: mapped_b}`

Each fork path runs **different transforms** before coalescing. This is the key ARCH-15 innovation — per-branch processing chains.

**DAG:**
```
source ─> [fork] ─┬─ path_a ─> truncate ─────> truncated_a ─┐
                  └─ path_b ─> field_mapper ─> mapped_b ────┤
                                                             ├─ [coalesce] ─> output
```

**Transforms:**
- **path_a**: Truncates `description` field to 20 chars
- **path_b**: Renames fields (`product` → `product_name`, `id` → `item_id`, `price` → `cost`) and drops `category` and `description`

**Output structure** (nested merge):
```json
{
  "path_a": {
    "id": 1,
    "product": "Widget Pro",
    "price": 2500,
    "category": "electronics",
    "description": "High-performance ..."
  },
  "path_b": {
    "product_name": "Widget Pro",
    "item_id": 1,
    "cost": 2500
  }
}
```

**Use case:** Multi-API enrichment (sentiment API on path_a, entity extraction on path_b), then merge enriched results.

## Output

Results appear in `output/merged_results.json` (basic variant) or `output/per_branch_results.json` (per-branch variant) as JSONL.

See the **Variants** section above for example output structures.

## Why Fork/Coalesce?

In this minimal example both paths carry the same data. The real power emerges in production pipelines where each fork path hits a **different external service**:

| Scenario | Fork Paths | Merge Policy |
|----------|-----------|-------------|
| **LLM redundancy** | 3 different models | `quorum` (2-of-3 agree) |
| **Multi-API enrichment** | Sentiment API + Entity API | `require_all` + `union` |
| **Fastest wins** | Fast model + Accurate model | `first` |
| **Best effort** | Primary + Fallback | `best_effort` with timeout |

## Merge Policies

| Policy | Behaviour |
|--------|-----------|
| `require_all` | Wait for all branches — fail if any missing |
| `quorum` | Wait for N-of-M branches (`quorum_count: 2`) |
| `best_effort` | Merge when timeout expires or all branches arrive |
| `first` | Merge immediately when first branch arrives |

## Merge Strategies

| Strategy | Result Shape |
|----------|-------------|
| `union` | `{field_a: val_a, field_b: val_b, ...}` — flat merge; collision handling is configurable via `union_collision_policy` (see "Union Collision Policy Variants" below) |
| `nested` | `{path_a: {...}, path_b: {...}}` — branch-keyed (used in this example) |
| `select` | `{...}` from one chosen branch (`select_branch: path_a`) |

## Key Concepts

- **Fork gate**: `routes: {true: fork}` with `fork_to: [path_a, path_b]` duplicates each row
- **Token lineage**: Each forked token gets its own `token_id` with parent recorded for audit
- **Pre-fork transforms**: Shared processing (like truncation) happens before the fork
- **Post-coalesce routing**: The coalesce name (`merge_results`) becomes a connection for downstream gates
- **Terminal state**: Parent tokens are marked `FORKED`; merged tokens are `COALESCED`

## Union Collision Policy Variants

Three additional configurations demonstrate `union_collision_policy` — a `CoalesceSettings` field controlling how field-name collisions are resolved when `merge: union` is used. Each variant forks into two paths that run `truncate` on `description` with different cutoffs (20 vs 50 chars), then union-merges the results so every field name collides.

| Variant | Policy | Selected branch on collision | Exit |
|---------|--------|------------------------------|------|
| `settings_union_last_wins.yaml` | `last_wins` (default) | last branch in declaration order (`path_b`) | 0 |
| `settings_union_first_wins.yaml` | `first_wins` | first branch in declaration order (`path_a`) | 0 |
| `settings_union_fail.yaml` | `fail` | none — raises `CoalesceCollisionError` | non-zero (**deliberate**) |

**Orthogonality note:** `union_collision_policy` is independent of the arrival `policy` (`require_all`, `quorum`, `best_effort`, `first`). Arrival policy decides **when** to merge; collision policy decides **how** to reconcile overlapping field names once merging begins. All three variants use `policy: require_all`.

### `settings_union_last_wins.yaml` — last_wins (default)

On collision the last branch in `branches` declaration order wins. Every row in the merged output carries `path_b`'s 50-char truncated `description`.

```bash
elspeth run --settings examples/fork_coalesce/settings_union_last_wins.yaml --execute
```

Expected output excerpt (`output/union_last_wins.json`):
```json
{"id": 1, "product": "Widget Pro", "price": 2500, "category": "electronics", "description": "High-performance widget with advanced sensor ar..."}
```

### `settings_union_first_wins.yaml` — first_wins

On collision the first branch in declaration order wins. The merged output carries `path_a`'s 20-char truncated `description`.

```bash
elspeth run --settings examples/fork_coalesce/settings_union_first_wins.yaml --execute
```

Expected output excerpt (`output/union_first_wins.json`):
```json
{"id": 1, "product": "Widget Pro", "price": 2500, "category": "electronics", "description": "High-performance ..."}
```

### `settings_union_fail.yaml` — fail (deliberate failure)

**This pipeline is designed to fail.** Any overlap in field names raises `CoalesceCollisionError`. The orchestrator catches the first collision, marks that coalesce group `FAILED`, persists the collision record, and aborts the run. Because both branches emit the same column set, the first source row is sufficient to demonstrate the policy.

```bash
elspeth run --settings examples/fork_coalesce/settings_union_fail.yaml --execute || echo "expected non-zero exit"
```

Use this variant when you want the pipeline to reject overlap rather than silently pick a winner — for example, when two enrichment APIs are supposed to return disjoint fields and any overlap indicates a misconfiguration.

Note that `fail` does not distinguish "same value" from "different value" collisions — any duplicate field name is treated as an error. If your branches intentionally carry shared fields, use `last_wins` or `first_wins`, or rename those fields before the merge.

### Inspecting the audit trail

All three variants record the full collision provenance on the coalesce `node_states` row via `context_after_json`. The key fields are:

- `union_field_origins` — map of `field_name → selected_branch_name` for `first_wins`/`last_wins`; under `fail`, it records the final resolution candidate even though no merged row is emitted
- `union_field_collisions` — map of each collided field to its contributing branches in merge order

The audit record intentionally omits collision values, value hashes, and Python
types. Branch/field/winner provenance remains available without making low-entropy
values recoverable by enumerating their hashes.

Open the audit database with the Landscape MCP server:
```bash
elspeth-mcp --database sqlite:///examples/fork_coalesce/runs/union_last_wins.db
```

Or inspect directly via Python:
```python
import json, sqlite3
db = sqlite3.connect("examples/fork_coalesce/runs/union_last_wins.db")
for (ctx_json,) in db.execute(
    "SELECT context_after_json FROM node_states WHERE context_after_json LIKE '%union_field_origins%'"
):
    ctx = json.loads(ctx_json)
    print("origins:", ctx["union_field_origins"])
    print("collisions:", ctx["union_field_collisions"])
    break
```

For the `fail` variant, look for `status = 'failed'` on the `coalesce_merge_results_*` `node_id` — the same collision record is preserved even though the pipeline aborted.

## See Also

- [`deep_routing`](../deep_routing/) — Complex gate cascades (fan-out to sinks, no merge)
- [`explicit_routing`](../explicit_routing/) — Basic wiring model
