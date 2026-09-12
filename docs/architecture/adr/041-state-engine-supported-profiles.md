# ADR-041: State-Engine Supported Profiles

**Date:** 2026-08-11
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** state-engine, landscape, sqlite, postgresql, aws, deployment,
          single-leader, amends-adr-030

## Context

The v1 state-engine proof catalog advertised only SQLite WAL, while ELSPETH's
maintained AWS integration provisions PostgreSQL for the Landscape database.
ADR-030 recorded PostgreSQL runtime as unsupported in its historical 0.6.0
one-host worker-pack decision. Those statements cannot remain simultaneous:
the proof catalog must cover every maintained state-engine backend rather than
letting deployment documentation make a broader production claim.

The database backend and the scheduler deployment topology are separate axes.
Using PostgreSQL for Landscape does not by itself authorize multiple leaders,
multiple web replicas, or distributed scheduling. Evidence for one topology
cannot be promoted to another by analogy.

## Decision

The current v3 proof catalog requires two state-engine profiles:

| State store | Supported deployment scope | Authority |
| --- | --- | --- |
| `sqlite-wal` | Single-process leader; one-host leader with claim-only followers; web-hosted leader with same-host CLI followers | ADR-030 |
| `postgresql-16` | Maintained AWS single-leader Landscape deployment | This ADR |

SQLite WAL one-host leader/follower support remains governed by ADR-030. Its
one-host filesystem, WAL sidecar, clock, payload-store, fencing, and worker
lifecycle requirements remain in force.

PostgreSQL 16 single-leader is the database profile for the maintained AWS
Landscape deployment. PostgreSQL 16 is a required first-class state-engine
backend for that deployment, not an optional, provisional, or future port. The
AWS application topology still has at most one active Landscape scheduler
leader per run.

PostgreSQL multi-replica scheduling remains unsupported when it means
concurrent scheduler leaders or multi-host claim-only followers for one run.
The bounded ACA web-custody handoff amendment below permits sequential
ownership transfer while preserving one active scheduler leader per run.

The PostgreSQL state-engine contract includes DB-server time, row locking,
isolation, schema migration, and connection-loss behavior. The proof catalog
must keep their evidence attributable to the PostgreSQL profile:

- **DB-server time:** lease, heartbeat, expiry, and liveness decisions use a
  database-authoritative time posture rather than assuming one host clock.
- **Row locking:** competing state transitions use an explicit lock order and
  the row locks required to serialize their read-then-write decisions,
  including the ADR-038 outcome-versus-abandonment race.
- **Isolation:** transaction isolation and statement visibility must preserve
  the same single-winner, fencing, rollback, and audit-atomicity invariants as
  the SQLite profile.
- **Schema migration:** schema bootstrap and migration must carry every
  state-engine table, constraint, index, and enum/value obligation to the
  supported PostgreSQL 16 schema before runtime admission.
- **Connection-loss behavior:** loss before, during, or after commit must fail
  closed or reconcile from durable evidence; an ambiguous database response
  must never be treated as proof that a state transition did or did not win.

A catalog-wide ACA state-engine verdict requires a new catalog/profile
revision and topology-specific executable evidence. It cannot inherit
single-leader evidence. The bounded runtime acceptance amendment below does
not grant that verdict or change the frozen catalog's proof subjects.

## Amendment 2026-09-10: bounded ACA runtime acceptance

This amendment explicitly replaces the earlier catalog-revision prerequisite
for all multi-replica enablement with bounded runtime acceptance for the ACA
transitions specified here. The current v3 catalog and retained assessments
remain unchanged; an ACA catalog-wide completeness claim still requires a new
catalog identity and its own evidence. Desktop acceptance of the deployment
slice is not proof of the state-engine contract.

Automatic handoff is implemented for durable run/permit admission before
dispatch, permit-bound PREPARED initialization with no prior effects, and
eligible executing runs with a durable checkpoint. The successor preserves
the run UUID and immutable execution envelope, obtains fresh web EXECUTE
authority and Landscape leadership, and selects the latest checkpoint after
the leadership compare-and-swap. Continuous web-ownership checks fence later
execution. A live Landscape seat defers takeover; a web membership lease,
ticket or cancellation record alone never grants Landscape authority.

The envelope retains admitted policy evidence, version-pinned secrets and
content-addressed input bytes. Runtime, schema, protocol, source and
distribution compatibility must hold before recovered execution. Unsafe or
ambiguous effects, incomplete source ingestion and failed identity or
compatibility checks remain explicit `recovery_required` cases. The header
moves to EXECUTING before plugin initialization or effects; pure-initialization
replay is restricted to PREPARED with proof of no effects.

Authenticated peer cancellation is durable. Terminal reconciliation projects
status, counters, one terminal event and outputs after process death.
FAILED/INTERRUPTED reconciliation takes fresh status-preserving Landscape
authority and holds the row lock through web/output finalization to exclude
CLI takeover. Cancellation without a baseline checks the admitted envelope's
digest and identity, uses its policy evidence, and makes no plugin calls even
when current secrets, runtime or policy have changed. Retained input objects
currently have no automatic pruning policy.

The integrated implementation has completed default-suite and serial
PostgreSQL verification, including local process-crash evidence; see
[final verification](../../plans/2026-09-10-aca-pivot-and-replica-residuals.md#final-verification).
The executable surfaces are
`tests/testcontainer/web/test_cross_process_run_control_postgres.py`,
`tests/testcontainer/web/test_cross_process_run_reconciliation_postgres.py`
and the engine handoff regressions described in the
[ACA resumption plan](../../plans/2026-09-10-aca-pivot-and-replica-residuals.md#b-durable-run-admission-handoff-and-cancellation).
These results do not claim a live ACA run, unrestricted effect replay,
automatic restart of an interrupted Composer provider request, or routing
qualification without affinity. Preserve
[ADR-047's database clock and fencing contract](047-landscape-database-clock-authority.md).

## Amendment to ADR-030

ADR-030 remains the historical authority for the 0.6.0 SQLite WAL one-host
worker pack. This ADR amends only ADR-030's PostgreSQL runtime refusal and
future-port wording: PostgreSQL 16 single-leader is now supported for the
maintained AWS Landscape deployment. ADR-030's refusals of multi-host SQLite,
multi-replica scheduling, follower auto-promotion, and multiple web workers
remain unchanged.

## Consequences

- The state-engine proof catalog must name both `sqlite-wal` and
  `postgresql-16` as required profiles and must keep their deployment scopes
  mechanically distinct.
- A complete or production-supported state-engine verdict requires evidence
  for both required stores. Missing PostgreSQL evidence is unresolved evidence,
  not permission to narrow the maintained AWS claim.
- SQLite evidence cannot promote a PostgreSQL case, and PostgreSQL
  single-leader evidence cannot promote a multi-replica case.
- Maintained AWS documentation, schema admission, and release verification use
  the PostgreSQL 16 single-leader vocabulary from this ADR.

## Related Decisions

- **Amends:** ADR-030 (Multi-Worker Deployment Shape — One-Host WAL Pack), only
  for the PostgreSQL 16 single-leader profile described above.
- ADR-026 (Durable Token Scheduler) — scheduler rows, CAS discipline, and lease
  semantics apply to both required state stores.
- ADR-029 (Scheduler Journal Is the Single Source of Barrier-Buffer Truth) —
  barrier durability remains store-independent; locking evidence is
  profile-specific.
- ADR-038 (Non-Terminal ABANDONED Path) — its PostgreSQL row-lock ordering is a
  required PostgreSQL profile obligation.

## References

- `docs/architecture/state_engine/proof-catalog/v3/catalog.json`
- `deploy/aws-ecs/terraform/README.md`
- `docs/architecture/adr/030-multi-worker-deployment-shape.md`
