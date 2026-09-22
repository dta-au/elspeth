---
title: SQLite identity store saves a non-UTC grant expiry at the wrong instant
labels: [area/web, type/bug]
---

On the SQLite sessions store, a role grant whose `expires_at` carries a non-UTC offset is persisted as its local wall-clock digits and read back as if it were UTC. The grant then expires late by the size of the offset. At +10:00 an authorisation grant stays live ten hours past the expiry the administrator set.

## What happens

An administrator grants a role with an expiry two days ahead, expressed in a non-UTC offset. The API accepts it, the store records it, and the grant continues to authorise requests well past the stated instant. Measured on a clean checkout of `46219b2b7` against an in-memory SQLite store, granting `reviewer`:

| Offset | Given | Raw SQLite | Read back as | Error |
| --- | --- | --- | --- | --- |
| UTC (control) | `2026-09-16T21:52:27+00:00` | `2026-09-16 21:52:27.000000` | `2026-09-16T21:52:27+00:00` | 0:00:00 |
| +10:00 | `2026-09-17T07:52:27+10:00` (same instant) | `2026-09-17 07:52:27.000000` | `2026-09-17T07:52:27+00:00` | 10:00:00 |

## Why

`_ensure_utc` in `src/elspeth/web/coordination/membership_authority.py:50-51` tags naive values only:

```python
return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
```

It returns aware values unchanged and never calls `astimezone(UTC)`. The chain is:

- The admin route accepts any aware offset — `GrantRoleRequest.expires_at: AwareDatetime` in `src/elspeth/web/auth/identity_admin_routes.py:144`.
- `RepositoryIdentityAuthority.grant_role` (`src/elspeth/web/coordination/identity_authority.py`) passes the value through `_ensure_utc`, which is a no-op for an aware input.
- The `identity_roles.expires_at` column is `DateTime(timezone=True)` (`src/elspeth/web/sessions/models.py`, `identity_roles_table`). On SQLite, SQLAlchemy drops the tzinfo and stores the wall-clock text.
- On read the now-naive value is tagged UTC by the same helper, so the liveness check at `src/elspeth/web/coordination/identity_authority.py:964` (`expires_at is None or _ensure_utc(expires_at) > now`) compares the wrong instant.

## Impact

SQLite deployments only. PostgreSQL stores the instant correctly via `timestamptz`, so the same grant behaves as intended there. The effect is an authorisation grant outliving its expiry by exactly the administrator's UTC offset, with no error raised at any point.

Not measured: the same write-side pattern may affect other aware datetimes written to SQLite, for example relationship `effective_from` and `effective_until` via `assert_relationship`, and any other `DateTime(timezone=True)` column written from a caller-supplied aware value. Those were not reproduced and should be checked rather than assumed.

## Fix

Normalise to UTC on write — `astimezone(UTC)` for aware values — before persisting, so the stored text is always the UTC instant the reader assumes it to be.

Done looks like a regression test that grants with a non-UTC offset on SQLite and asserts both the stored instant and the expiry boundary against the UTC control above, plus a decision on whether the write-side normalisation belongs in `_ensure_utc` itself or in a separate helper, since the current one is correct for its read-side callers.
