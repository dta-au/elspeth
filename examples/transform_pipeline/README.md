# Type Coercion and Derived Values

Normalize CSV fields into runtime types, then compute fields that depend on
those normalized values.

## Run

From the repository root:

```bash
elspeth run --settings examples/transform_pipeline/settings.yaml --execute
```

The example reads five product rows and writes five enriched rows to
`examples/transform_pipeline/output/enriched.csv`. A successful run ends
`COMPLETED`; the quarantine sink receives no rows and its file may therefore
be absent.

## Pipeline

1. `type_coerce` converts `price` to `float`, `quantity` to `int`, and
   `in_stock` to `bool`.
2. `value_transform` calculates `total`, `discounted_price`, and
   `discounted_total`. The last calculation deliberately reads the
   `discounted_price` field created by the preceding operation.
3. Rows that fail either transform route to
   `examples/transform_pipeline/output/quarantine.csv`.

The Landscape audit trail is written to
`examples/transform_pipeline/runs/audit.db`.
