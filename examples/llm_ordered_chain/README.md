# Extract a receipt, then apply a policy inside one LLM node

One `llm` node makes two ordered provider queries for each valid receipt. Query
`extract` returns a typed amount and expense type. Query `apply` consumes those
validated fields through explicit bindings:

```yaml
query_input_mode: ordered
pool_size: 1
required_input_fields: [receipt_text]
queries:
  - name: extract
    input_fields: {receipt: receipt_text}
    # template and typed output_fields are in settings.yaml
  - name: apply
    input_fields: {amount: extract_amount_usd, expense_type: extract_expense_type}
```

`input.csv` contains only `expense_id` and `receipt_text`. The second query's
prompt sees the generated amount/type, not the receipt text. This is an
internal query edge in one plugin node, rather than two LLM nodes connected by
a pipeline edge. The complete settings declare both query templates and typed
structured-output contracts; the fragment above only highlights the binding.

## Run the offline demonstration

From the repository root, with the normal ELSPETH environment and `errorworks`
installed, run:

```bash
./examples/llm_ordered_chain/run.sh
```

The launcher starts its own single-worker ChaosLLM on loopback port 8199,
generates a process-scoped secret-fingerprint key if needed, runs the normal
ELSPETH provider path, verifies outputs and audited requests, and stops its
owned server. It refuses occupied ports and existing audit/output files; use a
fresh example copy for another run. No real provider credential or external
network service is used.

The local provider fixture reads each actual rendered request. Its replies
vary between taxi, meal and equipment receipts, and its policy reply reads the
amount/type supplied in the second request. It is a deterministic integration
fixture, not a claim of model extraction accuracy or a server-authored
replacement for the pipeline. Every query makes an actual HTTP provider call.

| Expense | Validated extraction | Policy result |
|---|---|---|
| E101: taxi | 48.0 / transport | approve |
| E102: client lunch | 82.0 / meals | manual_review |
| E103: printer repair | 126.5 / equipment | manual_review |
| E104: faded receipt | Invalid string instead of a number | Original row quarantined; `apply` never called |

The pipeline deliberately ends **PARTIAL, exit 1**. The self-verifying launcher
ends **exit 0** when all expected conditions hold. Outputs are
`output/assessed_expenses.jsonl` (three rows) and `output/quarantine.jsonl` (the
one original row). Alongside the columns above, successful rows retain the
original input, each query's raw `*_answer`, returned model and reported usage.

## Why the order and declarations matter

Use list-form named queries and `pool_size: 1`. The default `upstream` mode
does not allow this generated-field dependency. Reversing the queries is a
forward reference and is refused before a provider call; `pool_size: 2` is
also refused. Do not put `extract_amount_usd` or `extract_expense_type` in
`required_input_fields` or the arriving schema: neither exists at node entry.
The typed `number` output normalizes a valid JSON number to a Python float
before the dependent prompt renders. An invalid extraction fails first.

`on_error: quarantine` applies to the whole node. Intermediate fields are not
published on failure, including if the first query succeeded and the second
failed. Earlier calls remain audited and can still be charged with a real
provider. Recovery is at the node boundary and can repeat earlier queries;
there is no durable mid-node checkpoint or exactly-once provider guarantee.
`row.source_row` and image inputs would still mean the original row. Source
validation failures also route to quarantine; sink write failures explicitly
choose `discard` and remain failed outcomes in Landscape.

## Inspect the actual dependency

`verify.py` joins semantic `llm` calls to their node state and original row,
then loads request/response payloads from `runs/payloads`. For each successful
expense it checks that the second request contains the first response's
validated amount/type and excludes the original receipt text. It also checks
one LLM graph node, seven row calls (2+2+2+1), one separate runtime-preflight
call, and the original-only error row. This detects a skipped provider query,
a second prompt rendered without the first result, and a consumer called after
invalid typed output.

```bash
.venv/bin/python examples/llm_ordered_chain/verify.py
sqlite3 examples/llm_ordered_chain/runs/audit.db \
  "SELECT error_details_json FROM transform_errors;"
```

The first call for E104 is retained even though its output was rejected;
provider-call success is not proof of accepted or published row data. Error
details live in Landscape rather than being added to the original error row.
See [Ordered LLM queries](../../docs/guides/ordered-llm-queries.md) for the full
binding, type-validation and recovery contract.
