---
title: SQLite identity store saves a non-UTC grant expiry at the wrong instant
labels: [area/web, type/bug]
---

On the SQLite sessions store, a role grant whose expiry carries a non-UTC offset is saved as its local wall-clock digits and read back as if it were UTC. The grant then expires late by the size of the offset — at +10:00, ten hours late.

## Where this lives

The web service stores identities and their role grants in a "sessions" database, which can be SQLite or PostgreSQL. Three files are involved, all under `src/elspeth/web/`:

- `coordination/membership_authority.py` — holds the shared `_ensure_utc` helper
- `coordination/identity_authority.py` — `RepositoryIdentityAuthority.grant_role`, which writes the grant
- `auth/identity_admin_routes.py` — the admin HTTP route that accepts the request

The table definition is `identity_roles_table` in `src/elspeth/web/sessions/models.py`.

## What happens

An administrator grants a role with an expiry two days ahead, expressed with a non-UTC offset. The API accepts it, the store records it, and the grant keeps authorising requests past the stated instant.

Measured on a clean checkout of `46219b2b7` against an in-memory SQLite store, granting the `reviewer` role:

| Offset | Given | Raw SQLite | Read back as | Error |
| --- | --- | --- | --- | --- |
| UTC (control) | `2026-09-16T21:52:27+00:00` | `2026-09-16 21:52:27.000000` | `2026-09-16T21:52:27+00:00` | 0:00:00 |
| +10:00 | `2026-09-17T07:52:27+10:00` (same instant) | `2026-09-17 07:52:27.000000` | `2026-09-17T07:52:27+00:00` | 10:00:00 |

Both rows describe the same moment in time. Only the second is stored wrongly.

## Why

`_ensure_utc` in `src/elspeth/web/coordination/membership_authority.py:50-51` tags naive values only:

```python
return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
```

A naive datetime (no timezone attached) gets stamped as UTC. An aware one is returned unchanged — the helper never calls `astimezone(UTC)` to convert it. For its read-side callers that is correct, because values coming back from SQLite are naive. On the write side it is not.

The chain:

1. The admin route accepts any aware offset — `GrantRoleRequest.expires_at: AwareDatetime` at `src/elspeth/web/auth/identity_admin_routes.py:144`. A `+10:00` value is as valid as a `+00:00` one.
2. `RepositoryIdentityAuthority.grant_role` passes it through `_ensure_utc`, which is a no-op for an aware input.
3. The column is `DateTime(timezone=True)`. On SQLite, SQLAlchemy drops the tzinfo and writes the wall-clock text — `07:52:27`, with the `+10:00` discarded.
4. On read, the now-naive value is tagged UTC by the same helper, so the liveness check at `src/elspeth/web/coordination/identity_authority.py:964` — `expires_at is None or _ensure_utc(expires_at) > now` — compares the wrong instant.

## Impact

SQLite deployments only. PostgreSQL stores the instant correctly through `timestamptz`, so the same grant behaves as intended there. The effect is an authorisation grant outliving its expiry by exactly the administrator's UTC offset, with no error raised at any point.

Not measured, and worth checking rather than assuming: the same write-side pattern may affect other aware datetimes written to SQLite — for example relationship `effective_from` and `effective_until` via `assert_relationship`, and any other `DateTime(timezone=True)` column written from a caller-supplied aware value.

## Fix

**Size: small — roughly a one-line change plus a test, with one decision to make first.**

The decision: `_ensure_utc` is shared between read-side and write-side callers, and it is correct for the read side as written. Converting in place would change behaviour for every caller. Adding a separate write-side helper that calls `astimezone(UTC)` for aware values keeps the two paths distinct. Either is defensible; pick one deliberately and apply it to every write-side call, not just `grant_role`.

Correct behaviour: whatever offset an administrator submits, the instant stored is the same instant, and a grant expires exactly when it was set to expire.

You would know it holds by a regression test that grants a role with a non-UTC offset on SQLite and asserts both the stored value and the expiry boundary against the UTC control in the table above. Make it fail first against current code — the two rows must produce identical stored text once the fix is in, and visibly different text before it.
