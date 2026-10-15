# Container token policy and compatibility boundary

This change provisions `quota_container_tokens_per_day` in the Sessions policy
store during application startup, before serving requests. Container token
policies have optional storage ceilings; identity allowances still require both
token and storage limits. `quota_container_storage_bytes` remains validated-only.
No daily token accounting, overshoot, reset, or replica precedence semantics
change.

Startup serializes the entire policy population, including the empty case:
SQLite uses the session engine's `BEGIN IMMEDIATE`; PostgreSQL takes a
`SHARE ROW EXCLUSIVE` table lock. It then applies these rules:

| Active container policy | Configured token cap | Result |
| --- | --- | --- |
| Absent | Present | Create one config-owned token policy with storage absent |
| Absent | Absent | Leave absent |
| Config-owned, matching | Present | Retain the policy ID and every stored value |
| Config-owned, different | Present | Refuse startup without replacing or revoking |
| Any single policy | Absent | Retain it |
| Explicit non-config policy | Present | Retain its existing precedence and values |
| More than one | Either | Refuse as ambiguous; do not select a winner |

A non-null `container_slot`, constrained to the constant `1`, is unique among
active container rows. This enforces the singleton across actors using an
ordinary index that both database dialects can reflect. Identity rows retain
their existing separate active-policy uniqueness rule. A SQL check requires
storage for identity rows. Revoked historical rows remain intact.

New policies emit a `quota_set` event with actor/source `config`, scope
`container`, dimension `tokens`, and no identity or request provenance. The
Landscape recorder starts before the Sessions write lock. An audit callback
failure rolls back the Sessions insert. Startup retains and joins the exact
provisioning task through repeated cancellation, with independent process
recovery armed, before closing either database owner. As with existing identity
quota writes,
the two databases do not share an atomic commit: a later Sessions commit failure
can leave the configuration audit event without a committed policy.

## Sessions compatibility

The owner patch advances Sessions epoch 75 to a provisional 76. The .7
integration allocates final epoch **77**, retaining the .6 epoch-76 committed
graph-review authority and adding this quota shape. Neither the .6 epoch-76
contract nor the owner's distinct quota-only provisional-76 contract is the
combined identity. An old binary
must refuse the new representation, and this binary preserves/refuses old stores
using the existing schema admission checks. Startup does not migrate a store.

The repository currently has no supported Sessions migration entry point:
`src/elspeth/web/sessions/schema.py` requires exact shape and epoch;
`create_session_engine()` admits existing SQLite stores through a read-only
probe before opening a writable connection. Compatibility policy permits a
separately reviewed conversion, but does not authorize automatic conversion,
store replacement, reset, or live cutover.

## Non-destructive conversion proposal

The proposed future operation copies a verified, quiescent supported source to
a distinct empty destination. It never modifies, restamps, replaces, or deletes
the source. No user-store conversion is performed by this patch, and a wipe is
not required by the quota representation itself.

1. Verify the complete source epoch and schema, backend identity, foreign keys,
   and policy invariants before writing any destination. Refuse unknown shapes,
   multiple active container policies, missing identity storage, or a nonempty
   destination. Do not repair policies or choose an actor's row as the winner.
2. Create the final integrated schema in an isolated destination. Preserve all
   logical rows, IDs, token and storage values, actors, timestamps, revocations,
   usage (including unknown values), pending attempts, and relationships. The
   only quota representation additions are `container_slot = 1` and permission
   for future container storage values to be absent. Existing numeric storage
   values remain numeric, including config-owned rows.
3. Validate foreign keys, complete row equality for retained columns, index and
   constraint semantics, and the full integrated schema before accepting the
   destination. Publish its new schema identity only after successful validation.
   A failure must leave the source unchanged and the destination unavailable.
4. Require a separate operator decision for production cutover and for any
   future retirement of the source. Conversion does not alter policy precedence,
   revoke a policy when configuration is absent, or activate container storage
   configuration.

The isolated feasibility probe covers SQLite only, using embedded immutable
epoch-75 model definitions and synthetic rows. It compares every table's row
snapshot, although most tables are empty. It verifies source file/sidecar bytes,
policy numeric/provenance preservation, unknown usage and pending attempts, and
negative controls for occupied/same-source destinations, ambiguous policies and
schema drift, including missing audit triggers. Mutated target CHECK/index
controls must refuse. An injected pre-stamp failure verifies copied-row rollback,
destination refusal and unchanged source bytes. Before stamping it validates
metadata shape, foreign keys, required triggers and row equality; complete
current-schema admission follows the stamp and commit.

This is not a concurrent WAL snapshot, crash-recovery or post-stamp failure proof.
Its evidence remains **main epoch 75 to quota-only provisional epoch 76**.
The target-delta guard compares physical DDL and does not detect the .6
graph-review semantic boundary, whose DDL was unchanged. Repointing that probe
at integrated epoch 77 cannot establish preservation of .6 graph-review,
cohort, receipt, recovery or execution-input authority. The final integrated
delta needs separate review and evidence; no conversion was run for it.
PostgreSQL needs its own preserving conversion
implementation and validation before a supported operator workflow can be
offered. The probe is a test of this proposal, not an installed migration command
or evidence of a production conversion.
