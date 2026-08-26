# reference_join → fork → two LLM calls → coalesce

One lookup, two consumers.

```
tickets.csv ──> [enrich: reference_join] ──> [fork_gate] ─┬─ path_triage ──> [llm: triage] ─┐
                                                          └─ path_reply  ──> [llm: reply ] ─┤
                                                                                            ├─ [merge_branches]
                                                                                 (merged) ──> [route_output] ──> handled_tickets.json
```

A support ticket arrives with a product key and a customer message. The
product's description, category, support tier and warranty come from
`products.csv`. Both LLM branches need that context *and* the customer's
original words.

## Run it

```bash
./examples/reference_join_fork_llm/run.sh
```

Ends SUCCESS, exit 0: 5 tickets in, 5 merged rows out. The script starts two
local ChaosLLM servers (ports 8201 and 8202), runs the pipeline, and stops them
on exit. No credentials, no network.

## Enrich BEFORE you fan out

This is the reason the example exists. The join sits between the source and the
fork, so it runs **once per ticket**, and both branches inherit the same
enriched row.

Forking first and joining inside each branch would look equivalent and is not:

* the same lookup runs twice per ticket;
* two copies of the reference table sit in the config, and the two nodes can
  drift apart in a later edit;
* the branches can then disagree about what the product *is*, which is exactly
  the kind of split-brain a coalesce cannot detect — it merges whatever arrives.

Enrichment that both branches need belongs upstream of the fork.

## Both branches see the enriched row AND the original

Look at one merged output row: under both `path_triage` and `path_reply` you
will find `ticket_id`, `product` and `message` — the source's own fields — next
to `product_description`, `product_category`, `support_tier` and
`warranty_months`, which the join added. Neither branch had to re-read anything
to get them.

The two `llm` nodes declare those joined fields in `required_input_fields`, so
the DAG checks them against `reference_join`'s declared output guarantees at
**config time**. Misspell one and the run refuses to start with the field
named, rather than failing at template render on the first row. That check is
the payoff for the join declaring its output fields statically instead of
discovering them from data.

## `require_all`, and why

```yaml
policy: require_all
merge: nested
```

A ticket is written only when both the triage note and the draft reply exist. A
half-answered ticket is worse than an unanswered one, because downstream it
looks answered. `nested` keeps each branch's view under its own key rather than
flattening them into a collision.

## Each branch has its own endpoint

`triage` calls port 8201, `draft_reply` calls port 8202, with different `model`
values. That is what fanning out to two models looks like: in production these
would be two providers or two models, and here they are two ChaosLLM servers so
the example runs offline.

## About the model output

ChaosLLM replays canned responses from `triage_responses.jsonl` and
`reply_responses.jsonl` in **request order**, so the text you see in a row is
not derived from that row — it is the next line in the file. The responses are
deliberately written to be row-agnostic so nothing in the output contradicts
the ticket it landed on.

The row-specific part is the **prompt**, which is built from the enriched row.
Prompt bodies are stored by reference in the payload store rather than inline in
`audit.db` (the `calls` table carries `request_ref`, a content hash), so read
them through the Landscape tooling rather than a raw SQL SELECT.

What you can check directly is that both branches carried the joined fields:

```bash
python3 -c "
import json
row = json.loads(open('examples/reference_join_fork_llm/output/handled_tickets.json').readline())
for branch in ('path_triage', 'path_reply'):
    print(branch, '->', sorted(row[branch]))
"
```

Both lists contain the source's `ticket_id`, `product` and `message` alongside
the joined `product_description`, `product_category`, `support_tier` and
`warranty_months`.

Fault injection is set to zero in both `chaos_*.yaml`. This example teaches a
DAG shape; a stochastic fault would make its exit code a coin flip.
`chaosllm_sentiment` and `chaosllm_endurance` are where fault handling is
exercised.

## Files

| File | Role |
|---|---|
| `tickets.csv` | Main line: 5 support tickets |
| `products.csv` | Reference table, resolved relative to THIS directory |
| `settings.yaml` | The pipeline |
| `run.sh` | Starts both ChaosLLM servers, runs, tears down |
| `chaos_triage.yaml`, `chaos_reply.yaml` | Zero-fault ChaosLLM configs |
| `triage_responses.jsonl`, `reply_responses.jsonl` | Canned model output |
| `output/`, `runs/` | Written by the run |
