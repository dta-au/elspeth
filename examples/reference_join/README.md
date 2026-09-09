# reference_join — enrich a row from a keyed reference table

A row arrives carrying a business key and nothing else about it:

```csv
order_id,product,quantity
A-1001,hats,3
```

The description, price, category and tax treatment of `hats` live somewhere
else. `reference_join` matches the row's key against a reference table and
lifts named values onto the row.

## Run it

```bash
elspeth run --settings examples/reference_join/settings.yaml --execute
elspeth run --settings examples/reference_join/settings_nested.yaml --execute
elspeth run --settings examples/reference_join/settings_missing_product.yaml --execute   # exits 1 BY DESIGN
```

| Config | Table | Rows | Ends |
|---|---|---|---|
| `settings.yaml` | `products.csv` (flat) | 5 | SUCCESS, exit 0 |
| `settings_nested.yaml` | `products.json` (nested, one sparse entry) | 5 | SUCCESS, exit 0 |
| `settings_missing_product.yaml` | `products.csv` (flat) | 3 | **PARTIAL, exit 1 by design** |

## The three things worth understanding

### 1. The table is configuration, not a second source

`reference_file` is read by the settings loader, not by the transform. The
table's bytes land inside the node's options, which means they land inside the
node identity and the run's topology hash.

That is the whole design. Edit `products.csv` between a run and its resume and
the resume is **refused**, because the enrichment answers would differ from the
ones already recorded. A transform that opened a file at row time would have
neither that protection nor the one sources get (resume never re-reads a
source; rows replay from the content-addressed payload store).

The consequence you will notice first: `reference_file` resolves relative to
**this directory**, not the repository root, and is confined to it. That is why
`products.csv` sits beside `settings.yaml` while `orders.csv` is addressed as
`examples/reference_join/orders.csv` in the source block. Sources and reference
tables genuinely do not resolve paths the same way.

### 2. Two names, two sides of the join

```yaml
key_field: product            # the column on the ARRIVING ROW
reference_key_name: sku       # the key column INSIDE the reference table
```

Values are addressed by expression over the matched entry, bound as `ref`:

```yaml
output:
  product_description: "ref['description']"          # flat CSV
  tax_rate: "ref['tax']['rate']"                     # nested JSON
```

This is the engine's existing expression grammar. There is deliberately no
second, shorter spelling for the flat case — one grammar, so a reader never has
to work out which of two forms they are looking at.

### 3. A miss fails the row by default

`on_miss` covers two situations with one policy: the row's key is absent from
the table, or an output path does not resolve inside the entry that *did*
match. It applies per output field.

* `fail` (default) — the row becomes an error, routed by `on_error`.
* `null` — that one field is null; siblings that resolved are kept.
* `default` — that one field takes its `default_values` entry.

`settings_missing_product.yaml` shows why `fail` is the default. Order A-2002
asks for `scarves`, which the table does not list, and the run ends PARTIAL with
that row in `output/unknown_products.csv`. The alternative — emitting the order
with blank enrichment columns — puts a row in the output that no downstream
reader can tell apart from a correctly enriched one.

`settings_nested.yaml` shows the granularity. `products.json` gives `socks` no
`tax` member, so `tax_rate` takes its default while that row keeps the
description and price the table *could* supply.

## What this is not

Not a way to read a data file at runtime, and not a second source. The table is
fixed when the run starts.

If you want the reference data to become **rows** — one token per record,
traversing the DAG and landing in the audit trail — that is `blob_fetch` plus
`blob_csv_expand`, and it is the right shape for ingesting a dataset. It is the
wrong shape for a lookup table, and you would then need a join to get the values
back onto the main line, which is this transform.

## Files

| File | Role |
|---|---|
| `orders.csv` | Main line: 5 orders, every product listed |
| `orders_with_unknown.csv` | Main line: 3 orders, one product absent from the table |
| `products.csv` | Flat reference table |
| `products.json` | Nested reference table; `socks` deliberately lacks `tax` |
| `output/` | Written by the runs |
| `runs/` | Landscape audit databases, one per config |
