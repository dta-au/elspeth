# Resolve a customer, then consume its region

One `reference_join` node performs two ordered internal lookups. The ticket
contains `customer_id`; the first lookup produces `service_region`. The second
lookup uses that newly produced field to select a regional support policy.
There is no region, tier, queue or SLA column in the original ticket.

```text
ticket -> resolve_support_policy -> output
                  |
                  +-> quarantine

Inside that one node:
customer_id -> customers.csv -> service_region -> region_policies.csv -> policy
```

Run from the repository root with a fresh example audit/output directory:

```bash
elspeth validate --settings examples/reference_join_chain/settings.yaml
elspeth run --settings examples/reference_join_chain/settings.yaml --execute
```

The run deliberately ends **PARTIAL, exit 1**: three enriched tickets and two
original tickets in quarantine. Validation itself exits 0.

| Ticket | First lookup | Second lookup | Result |
|---|---|---|---|
| T101 / C100 | APAC | silver / sydney-support / 24 | Output |
| T102 / C200 | EMEA | gold / dublin-support / 8 | Output |
| T103 / C300 | AMER | platinum / denver-support / 4 | Output |
| T104 / C999 | No customer | Not attempted | Original ticket quarantined |
| T105 / C400 | LATAM | No regional policy | Original ticket quarantined |

The SLA values are strings because the reference is CSV. Returned fields are
declared by the plugin's `output` maps; the settings loader binds each reference
file relative to this settings directory. The references form part of the
configuration and audit identity, rather than being separate runtime sources.

`lookups` list order is the dependency order. Reversing the two entries is
refused at configuration admission because `service_region` is not yet
available. Only `customer_id` is an external lookup key; do not add
`service_region` to the source or the node's upstream requirements.

Both lookups explicitly use `on_miss: fail`. The node publishes its joined
fields only when both succeed. For T105 the successful first lookup does not
leak `service_region` into the error sink: quarantine contains exactly the
original `ticket_id`, `customer_id` and `request` fields. Source validation
failures use the same quarantine sink. Both sinks explicitly choose `discard`
for a write failure, which remains a failed outcome in the audit.

The unused GLOBAL policy is a deliberate fallback illustration: changing the
first lookup to `on_miss: default` with
`default_values: {service_region: GLOBAL}` makes an unknown customer consume
the GLOBAL policy in the second lookup. It does not skip that lookup or waive
its own miss policy. The shipped config keeps the stricter fail behavior.

Outputs are `output/support_policy.csv` and `output/quarantine.csv`. Inspect
lookup attribution without expecting an error column in those original rows:

```bash
sqlite3 examples/reference_join_chain/runs/audit.db \
  "SELECT error_details_json FROM transform_errors;"
```

The two `reference_miss` records name lookup index 0/table `customers` and
lookup index 1/table `policies`. Successful node states retain the two ordered
lookup results in `node_states.success_reason_json`. Error reasons and lookup
provenance belong to Landscape, not added row fields.
