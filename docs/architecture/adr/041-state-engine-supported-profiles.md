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

ELSPETH supports two required state-engine profiles:

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
AWS application topology still has exactly one Landscape scheduler leader.

PostgreSQL multi-replica scheduling remains unsupported and is separately
owned by the multi-replica-safe web runtime program (`elspeth-b5d7aa5655`).
Its current remediation prerequisite is `elspeth-4d6c0dd0f5`. This ADR neither
enables nor supplies evidence for multiple scheduler leaders, multi-host
follower scheduling, or multiple web replicas sharing run custody.

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

Any future multi-replica enablement requires a new catalog/profile revision,
a new or amended architecture decision, and topology-specific executable
evidence. It cannot inherit single-leader evidence.

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

- `docs/architecture/state_engine/proof-catalog/v2/catalog.json`
- `deploy/aws-ecs/terraform/README.md`
- `docs/architecture/adr/030-multi-worker-deployment-shape.md`
