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

All lookups read the original input row. Outputs must be distinct and cannot
serve as another lookup's input. Chaining requires separate nodes. A lookup
may apply its configured default, preserve the existing null policy, or return
a row error on a genuine miss. The node's `on_error` routes that original row
to quarantine. No partially enriched row reaches the success path. Audit
metadata identifies the lookup and table without copying row values.

Malformed configuration, files, tables, expressions, and infrastructure errors
are distinct from a missing match. Existing duplicate-key and field-collision
checks remain. Tables are materialized before execution and bound into run
identity, including replay and verify. Parsed tables are shared within one
construction; caches do not outlive their configuration snapshot. Existing
cell/byte safeguards apply to aggregate retained indexes without a fixed
lookup-count limit.

## Implementation and acceptance

1. Write failing plugin tests, then implement typed table/lookup configuration,
   independent evaluation, atomic results, output contracts, scoped sharing,
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
