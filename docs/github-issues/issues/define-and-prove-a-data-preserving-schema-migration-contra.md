---
title: Define and prove a data-preserving schema migration contract before the 1.0 schema lock
labels: [area/audit, area/engine, type/task, priority/P1]
---

ELSPETH's two databases — the Landscape, which holds the audit trail of every run, and the web
session store — refuse to open at any schema version other than the current one, and the
documented remedy is to delete and recreate them. That is deliberate pre-1.0 policy. The
documentation also commits to data-preserving upgrades from 1.0 onward, and nothing has been
designed, built or tested to deliver them. This issue is that work, and it needs to conclude
before the 1.0 schema baseline is declared.

## Where to start

Three files establish the current position:

- `src/elspeth/core/landscape/schema.py` — the Landscape schema epoch and its history comment
  at `:239-246`, with the constant at `:449` (currently 43).
- `src/elspeth/web/sessions/schema.py:327-332` — the session database's version check.
- `src/elspeth/core/schema_identity.py` — the shared cross-dialect identity table both stores
  stamp on creation.

## What happens today

Opening a session database written by a different release:

```
Session DB schema version <n> does not match SESSION_SCHEMA_EPOCH=<m>. Pre-release ELSPETH
does not migrate session databases. Delete the session DB file and restart.
```

The Landscape equivalent, at `src/elspeth/core/landscape/database.py:1506-1510`, raises
`SchemaCompatibilityError` with "Landscape database schema is outdated." Both are correct
behaviour for pre-release software, and both are documented policy rather than oversight.

What is missing is what replaces them. `docs/reference/configuration.md:2246` states that
"data-preserving, version-to-version schema migrations become a first-class compatibility
obligation at 1.0. They are intentionally not a pre-1.0 promise."
`docs/specs/2026-07-08-aws-ecs-runtime-readiness-design.md:725` states that "post-1.0 schema
migrations need a separate design. Until then, pre-release Aurora schema incompatibility
remains an operator drop/recreate workflow." That separate design does not exist.

## Impact

ELSPETH's product claim is a reviewable, reproducible audit trail. Once 1.0 is out, an upgrade
that requires deleting the Landscape destroys the run history that is the point of the system,
and one that requires deleting the session database destroys composer sessions and the identity
records held alongside them. The contract also determines what the frozen baseline has to look
like, so settling it after the freeze is the expensive order.

Three schemas version independently and would have to move together: the Landscape (epoch 43),
the sessions database (epoch 65), and the guided session payload, which carries its own
`GUIDED_SESSION_SCHEMA_VERSION` in
`src/elspeth/web/composer/guided/state_machine.py:51`. Which persisted payload shapes are
covered by the promise at all is itself part of the scope — guided is being retired as a
user-facing mode while its machinery stays, so that needs a decision rather than an assumption.

## Scope

The first deliverable is an accepted architecture decision, not code. It has to settle at
least:

- supported source and target versions per store, and whether skipped-version upgrades are
  supported;
- the migration registry and tooling, and which database role owns schema change as against
  runtime data access;
- offline upgrade versus rolling or mixed-version deployment; forward and backward
  compatibility; and when a rollback is refused rather than attempted;
- preservation of audit semantics, of cross-store links between runs, blobs and identities, and
  of the interpretability of opaque payloads written by an older release;
- transactional atomicity when a migration fails, idempotent restart after interruption, and
  the backup and restore procedure around both;
- parity between SQLite and PostgreSQL, which differ enough here to be a first-class concern
  rather than a detail.

## Acceptance

- The architecture decision is accepted before the 1.0 schema baseline is declared.
- A complete migration inventory exists, starting from the final pre-1.0 version of each store.
- Automated tests cover fresh creation, an N-1 upgrade, a supported skipped-version upgrade, an
  interrupted migration retried to completion, refusal of a schema newer than the code, and
  backup and restore — on both dialects. The repository already runs a PostgreSQL-backed
  selection under `tests/testcontainer/` separately from the default suite, which is where the
  dialect half belongs.
- Deployment compatibility records and operator runbooks state exactly when a rollback is
  permitted.
- A 1.x upgrade covered by the compatibility promise completes with no delete-and-recreate step
  and no loss of historical audit or session data.

**Size.** The largest item in this group, and a poor first issue. It needs an accepted
architecture decision before any code can be written, it spans two databases and a versioned
payload schema, its test matrix crosses two SQL dialects, and it gates a release milestone —
so it should start well ahead of that milestone rather than alongside it. Expect the design to
be most of the work.
