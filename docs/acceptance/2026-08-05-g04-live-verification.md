# g04 live verification — `json_explode` is wireable again

Date: 2026-08-05. Scope: targeted live verification of `elspeth-7a2c9a24c3`
("json_explode is unwireable in the Composer"), not a full battery round.

Deployed for this run: `a-fa1b99c60192978b10f7-web:15`, release
`release/0.7.2@f65af9258`, image digest
`sha256:a2b483f8a4b6d74c276bbe4d8badb24783e481b439ff18cc4165615024841a2f`.
Previous deployment `web:14` ran release `6bcd69037`, which predates the fix.

## Verdict

**`elspeth-7a2c9a24c3` is verified fixed live.** Recommend close; the close
carries `close_commit release/0.7.2@f65af9258`.

| | Pre-fix | Post-fix (`web:15`) |
|---|---|---|
| Compose outcome | **5 failures / 5 attempts, across two builds** | 2 × HTTP 200 / 2 attempts |
| — `web:13`, effort `high` | 2 × 422 `convergence_wall_clock_timeout` (compose loop) | — |
| — `web:14`, effort `medium` | 1 × 422 @ 271s (compose loop) | 199s, 143s |
| — `web:14`, planner via `g04p` | 2 × 500 `planner_repair_exhausted` @ 127s, 113s | |
| `json_explode` in graph | never | both runs |
| Semantic contract | high-severity rejection | `passed: true`, advisory |

Pre-fix counts are from `2026-08-05-compose-cost-measurement.md` addenda 1 and
2. The failures span two task-definition revisions and two reasoning-effort
settings, so neither the effort knob nor the authoring surface was the lever —
consistent with a gate that rejects the topology outright.

The decisive artifact is the validate response, which is the WARN path the fix
introduced, observed live:

```json
{"name": "semantic_contracts", "passed": true,
 "detail": "1 semantic contract(s) checked; 1 unproven (advisory): json_explode.array_field.list",
 "affected_nodes": ["explode_items"]}
```

The requirement is **disclosed and non-blocking** — exactly the designed
behaviour. `line_explode` keeps `FAIL`.

## Run 2 — the full pass

Session `86aea6e9-aeef-4f91-88d6-9255a542ec47`, run
`c3e36a91-fa65-4b4e-81eb-41bc260123e0`, `completed`.

```
source_source_c6e78df4a5d1 -> transform_explode_items_92a0d7b4199d
                           -> transform_flatten_item_9f59154d6c26
                           -> sink_flat_line_items_8787d9b93ef9

rows_processed 3 | emitted 9 | terminal 9 | succeeded 6 | structural 3 | failed 0
closure closed  | missing_terminal_outcomes 0
```

**Terminal tokens (9) > source rows (3)** — the property
`round3-graph-corpus.md` names as the point of g04. Sink content is six JSONL
rows, one per line item, each carrying `order_id` and `customer_name`:

```json
{"customer_name": "Alice Nguyen", "order_id": "ORD-001", "product": "Wireless Keyboard", "quantity": 1}
{"customer_name": "Alice Nguyen", "order_id": "ORD-001", "product": "USB-C Hub", "quantity": 2}
{"customer_name": "Bob Martínez", "order_id": "ORD-002", "product": "Noise-Cancelling Headphones", "quantity": 1}
{"customer_name": "Bob Martínez", "order_id": "ORD-002", "product": "Laptop Stand", "quantity": 1}
{"customer_name": "Clara O'Brien", "order_id": "ORD-003", "product": "Mechanical Pencil Set", "quantity": 3}
{"customer_name": "Clara O'Brien", "order_id": "ORD-003", "product": "Desk Lamp", "quantity": 1}
```

## Run 1 — converged, then failed downstream

Session `2b1013e0-10ba-44df-916c-7ba084d25fd8`, run
`ed3b37c9-4fbf-45af-8cfd-d97ffd685390`, `failed`. Compose and the semantic
contract behaved identically to run 2; `json_explode` fanned out correctly
(`fields_added: ["item"]`, `fields_removed: ["line_items"]`, expand group
created). The run died at the **next** node. Three findings below come from it.

The composed topology was correct in both runs. Run 1:

```yaml
explode_items:     {plugin: json_explode,    array_field: line_items, output_field: item, schema: {mode: observed}}
hoist_item_fields: {plugin: value_transform, required_input_fields: [item],
                    operations: [{target: product,  expression: "row['item']['product']"},
                                 {target: quantity, expression: "row['item']['quantity']"}],
                    schema: {mode: flexible,
                             fields: [order_id, customer_name, product, quantity],
                             guaranteed_fields: [order_id, customer_name, product, quantity]}}
project_columns:   {plugin: field_mapper, select_only: true}
```

## The round-3 g04 pass was void

Round 3 recorded g04 as `completed` 6/0/0, **INTEGRITY PASS** (run
`57cc5039-0567-4edf-a4df-dee0c912c15e`). Its artifacts show a **two-node**
graph:

```
source_source_84261bdf4594 -> sink_flat_rows_84c264e028df      (no transform at all)
rows_processed 6 | emitted 6 | terminal 6
```

The composer could not wire `json_explode`, so it satisfied the intent by
pointing the `json` source at a nested record path. Source rows == terminal
tokens, against a corpus criterion that reads *"terminal tokens > source rows,
which is the property `json_explode` provides"*. The grading checked terminal
state and never the stated property, so **6 == 6 was graded a pass for a
fan-out test**.

This is the same hole the round-3 report self-criticised for routing
(report line 468: *"A named-routing regression sending every row to `standard`
would still show 4/1/1 and still be marked PASS"*). Round 4 should assert the
per-graph property, not just terminal state. It also means g04 has **never**
been a green regression datum for `json_explode` — the round-2 g04 label was a
different graph (gate + named routes) entirely.

## Findings from run 1

### F1 — `value_transform` conflates its input and output contract (new)

`value_transform.py:103` states one `schema` block defines "input/output
expectations", and `:181` builds `input_schema` and `output_schema` from it.
The plugin then *separately* derives the output by **adding** the operation
targets to `guaranteed_fields` (`_build_value_transform_output_schema_config`,
`:199-223`) — an explicit admission that `schema` is the **input** contract.

Listing an operation's target in `schema.fields` is therefore silently
self-contradictory: it makes `product` required **on input** when the operation
exists to **create** it. Nothing rejects the config. Compose returned
`is_valid: true`; the runtime rejected every row.

Deterministic local repro (no AWS needed):

```python
cfg = {"schema": {"mode": "flexible",
                  "fields": [{"name": "product", "field_type": "str"},
                             {"name": "quantity", "field_type": "int"}]},
       "required_input_fields": ["item"],
       "operations": [{"target": "product",  "expression": "row['item']['product']"},
                      {"target": "quantity", "expression": "row['item']['quantity']"}]}
t = ValueTransform(cfg)
# input_schema required fields: ['product', 'quantity']  <- the fields it creates
# t.input_schema.model_validate({"item": {...}}, strict=True)  ->  2 validation errors
```

Same defect class as `elspeth-7a2c9a24c3`: an unsatisfiable declaration
accepted at authoring time and discovered only at runtime. Fail-closed fix:
reject a config where a `schema.fields` entry is also an `operations[].target`,
or exclude operation targets from the derived input schema. Either turns a
runtime crash into a compose-time error the repair loop can act on.

Note the plugin's own `example_use` and `probe_config` both use
`schema: {mode: observed}` with no `fields` — run 2 authored it that way and
passed. So the failure is intermittent at the authoring layer but rests on a
real, deterministic hole in the config contract.

### F2 — `failure_detail` attributes a transform failure to the source (new)

Run 1's diagnostics carry an internal contradiction. Token-level states name
the failing node correctly:

```
transform_hoist_item_fields_b449c03d0a63  status=failed  PluginContractViolation
```

but `failure_detail` names the source:

```json
{"node_id": "source_source_11938742c5d0", "operation_type": "source_load",
 "error_message": "Transform 'value_transform' input validation failed: ..."}
```

A reader taking `failure_detail` at face value concludes the source failed.
The mechanism looks like recording the in-flight operation (the streaming
`source_load` the exception propagated through) rather than the node that
raised. Round 3's report opens its lessons with *"Reproduce before believing a
diagnosis — including your own"* for a run that was misread as a transform
failure; this field actively invites that error.

### F3 — `elspeth-82d4c5146c` reproduces on the current release

Third independent reproduction, third distinct root cause, now on
`f65af9258` / `web:15`:

```
emitted 3 | terminal 1 | succeeded 0 | failed 0 | structural 1 | pending 2
closure open | missing_terminal_outcomes 2
```

A token whose state is `failed` is counted as neither succeeded nor failed, and
a finished run reports `closure: open`. This confirms the ticket's own claim
that it is a property of the failure path rather than of any one defect, and
that it is not fixed.

## Deployment record (AWS ledger)

| Mutation | Value |
|---|---|
| ECR push | `elspeth-web:dev-f65af9258064-20260805T081204Z`, index `sha256:a2b483f8…` |
| Scanned | amd64 child `sha256:1001db22…`, `COMPLETE`, zero findings |
| Registered | `a-fa1b99c60192978b10f7-web:15` (image + 3 telemetry values only; normalised diff clean) |
| Pre-deploy doctor | run on `web:15` itself, exit 0, 35/35 ok, `session_schema: current` |
| Deployed | zero-overlap; single PRIMARY `COMPLETED`, `failedTasks: 0`, task HEALTHY |
| Chain verified | TD pin == running digest == `a2b483f8…`; index amd64 child == scanned digest |
| Endpoints | `/api/health` 200, `/api/ready` 200 |

No store recreation: epoch 45 on both sides, doctor reported
`session_schema: current`.

## Incidental: the composer wall clock is 270, not 240

`ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS = 270` on the live task definition,
while `locals.tf:391` pins 240. The deployed value is authoritative and is
consistent with round 3's composes dying at 271s. This closes leg 5 of
`elspeth-7da4e52344`. The terraform default and the deployed value have
drifted — a cold install would silently reduce the budget by 30s.

## Not done here

This was a targeted verification, not battery round 4. The full battery, the
cost measurement, and the advisor-model decision in
`2026-08-05-battery-round-4-brief.md` all remain outstanding, and `web:15` is
the build to run them against.
