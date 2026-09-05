"""PostgreSQL advisory-lock classid registry.

PostgreSQL exposes two flavours of advisory locks: the
single-argument form (one int8 namespace per connecting database) and the
two-argument form (two int4 namespaces, also database-scoped but
partitioned by the first argument -- the *classid*). ELSPETH uses
the two-argument form exclusively so that each subsystem holding
advisory locks gets its own classid namespace, avoiding cross-
subsystem collision (and cross-application collision with any
other software using single-argument advisory locks on the same
cluster).

This module is the SINGLE registry of classid values. Adding a
new advisory lock anywhere in ELSPETH MUST add a new constant
here with a distinct value. Reusing a classid across subsystems
re-introduces the collision risk that splitting the namespace
was meant to eliminate.

ABI commitment
--------------
Every constant defined here is **on-the-wire ABI**. Two ELSPETH
instances on the same Postgres cluster -- including instances
running different ELSPETH versions during a rolling deploy --
MUST agree on the value of every classid in this module, or
they will not serialise against each other on the same logical
resource. A version mismatch on a classid value produces a silent
correctness violation: both instances think they hold the lock,
both execute the protected code path concurrently, and the
correctness guarantee the lock was protecting is lost.

Changing any constant here therefore requires:

1. An ADR documenting the rationale and the migration plan.
2. A coordinated deploy that drains all writers using the old
   value before any writer using the new value comes online.
3. A schema/runbook update so operators understand the
   constraint.

The 32-bit signed integer space is enormous (~4.3 billion
values); pick distinct values, never reuse a retired value
within the same major release.
"""

from __future__ import annotations

# 0x454C5350 = 1,162,629,968 -- ASCII "ELSP" big-endian. First
# classid assigned in this registry; chosen so a Postgres operator
# inspecting pg_locks sees a recognisable value rather than a random
# magic number. Used by SessionServiceImpl._acquire_session_advisory_lock
# (src/elspeth/web/sessions/service.py) and the shared Sessions locking
# helpers for the session-scoped write lock that serialises same-session
# writers within a single Postgres cluster.
ELSPETH_SESSIONS_LOCK_CLASSID: int = 0x454C5350

# 0x53434845 = ASCII "SCHE". Session and Landscape initialization use the
# same classid and target so distinct schemas in one connecting database are
# deliberately over-serialized during bootstrap.
ELSPETH_SCHEMA_INIT_LOCK_CLASSID: int = 0x53434845

# 0x524F5554 = ASCII "ROUT". Used by routing-event persistence to
# serialize ownership of a routing_group_id before any state row is locked
# or an absent group is inspected. The second int4 key is
# hashtext(routing_group_id); collisions only over-serialize unrelated
# groups and cannot weaken the ownership invariant.
ELSPETH_ROUTING_GROUP_LOCK_CLASSID: int = 0x524F5554

# 0x41455850 = ASCII "AEXP". Used by audit-export snapshot registration
# (_acquire_signer_lineage_authority in
# src/elspeth/engine/orchestrator/audit_export_effects.py) to serialize
# the signer-policy recheck with the registry CAS insert for one export
# lineage. The second int4 key is hashtext() of the lineage string;
# collisions only over-serialize unrelated lineages and cannot weaken
# the registry's insert-once guarantee.
ELSPETH_AUDIT_EXPORT_LOCK_CLASSID: int = 0x41455850

# 0x424C4F42 = ASCII "BLOB". Used by the blob custody lock
# (_blob_custody_session_lock in src/elspeth/web/blobs/service.py): a
# SESSION-level lock held on one dedicated connection across a blob's
# reservation, file write and finalize, so concurrent writers of one
# session's blobs serialise. It deliberately does NOT share
# ELSPETH_SESSIONS_LOCK_CLASSID: session-operation fence operations
# (acquire, renew, release) take transaction-scoped locks on the same
# session key, and one shared classid made every fence operation on a
# session wait behind that session's filesystem persistence. The second
# int4 key is hashtext(session_id); collisions only over-serialize
# unrelated sessions' blob writes.
ELSPETH_BLOB_CUSTODY_LOCK_CLASSID: int = 0x424C4F42
