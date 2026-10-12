# Multiple reference lookups in one node

John approved this design on 2026-10-12. The feature starts from main at
`fdaa670394fd311fc5df61befb1a7b1941c01182`, outside frozen release 0.8.4.
John's current delivery direction combines this feature with the emergency
diagnostic repairs on `codex/emergency-diagnostics-20261012`, followed by
combined qualification and normal protected landing into `main`.

## Behavior

Extend `reference_join` with a named `tables` mapping and a nonempty `lookups`
list. Each table provides configuration-bound reference content and format;
each lookup selects a table, input key, reference key, output expression map,
and miss policy. Existing single-table configurations remain accepted.

John subsequently approved ordered chaining in the same node. Each lookup
reads either the original input row or an earlier lookup's output. Outputs
must be distinct and cannot overwrite arriving fields. Self and forward
references are rejected. Only external keys are declared as required inputs,
alongside explicit author requirements; internally produced keys are not
required on arrival. A lookup may apply its configured default, preserve the existing null policy, or return
a row error on a genuine miss. The node's `on_error` routes that original row
to quarantine. No partially enriched row reaches the success path. Audit
metadata identifies the lookup and table without copying row values.

Lookups execute in configuration order after schema/collision/input admission.
The first lookup failure stops processing. A generated null remains an invalid
downstream key, even if that consumer has a null/default miss policy. A default
string is looked up normally; a missing string match applies the consumer's
own miss policy. Existing scalar normalization and invalid-type rejection stay
unchanged. The worked chained example first resolves a shared product key and
uses it in both product and fulfilment CSV tables. Earlier independent-lookup
acceptance does not establish this chaining contract.

Malformed configuration, files, tables, expressions, and infrastructure errors
are distinct from a missing match. Existing duplicate-key and field-collision
checks remain. Tables are materialized before execution and bound into run
identity, including replay and verify. Parsed tables are shared within one
construction; caches do not outlive their configuration snapshot. Existing
cell/byte safeguards apply to aggregate retained indexes without a fixed
lookup-count limit.

## Implementation and acceptance

1. Write failing plugin tests, then implement typed table/lookup configuration,
   ordered evaluation and chaining, atomic results, output contracts, scoped sharing,
   aggregate limits, and attribution.
2. Write failing loader tests, then materialize nested table files with existing
   containment and replay/verify rules. Keep unrelated plugin payloads opaque.
3. Protect nested emitted defaults before environment expansion and at direct
   plugin validation using declarations on the typed nested model.
4. Verify CLI execution/quarantine and audit records, Composer blob-backed
   tables and lowering, old configuration parity, catalog exposure, and docs.
5. Run independent correctness and security reviews. Repair findings, refresh
   generated source-hash/census/catalog evidence, and run focused and relevant
   whole-tree checks. Run the canonical full-repository gate on a frozen tree
   when shared test capacity is available; preserve every failure and exit code.
6. Include the reviewed feature commit in the emergency repair branch and
   publish the combined PR into `main`. Use normal protected merge only
   after current-head checks qualify.

No deployment, protection changes, paid calls, or unrelated release/security
work is included. The main checkout and sibling worktrees remain untouched.
