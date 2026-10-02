---
title: Recovery fork-group reconstruction is quadratic in group count
labels: [area/engine, type/bug]
---

Resuming a run with many fork groups stalls before any work restarts. The check that decides whether a run is resumable rescans its whole member list once per group, so its cost grows with the square of the group count.

## Where to start

One function, `check_group_satisfiability_resumable`, in `src/elspeth/core/checkpoint/recovery.py`. Its existing tests live in `tests/unit/core/checkpoint/test_group_satisfiability_gate.py`.

Background in a sentence: a fork group is a row that fanned out into several branches, all of which must reach the node that closes the fork before a run can be resumed safely. This function is what verifies that for every group in a run.

## Why

The function materialises `fork_member_pairs` — one `(group_id, member_key)` tuple per branch — from a single database query. It then iterates the unique group ids and rescans the complete pair list once per group to build that group's member set (`recovery.py:444-445`). With one pair set per group this is O(N²) Python work after the database has already returned everything needed.

Its docstring records that it is the single shared implementation behind both the advisory `RecoveryManager.can_resume` surface and the enforcing guard in `ResumeCoordinator.resume()`, so one resume can pay the cost more than once.

## Impact

Nothing is computed incorrectly; large recoveries simply stall in the eligibility check before restarting any work. Measured at commit `3dc67fb1d`: 8,000 two-arm groups took about 1.55 to 1.61 seconds, against about 0.0023 seconds for the grouping path that preceded it — roughly 689 times slower.

## Fix

Build the mapping in one pass: accumulate each `member_key` into a dict keyed by `group_id` while walking the query result, then read from that dict in the per-group loop.

Two correctness checks inside that loop must survive unchanged. First, the whole-roster closure check — every member of a group that binds a closer must itself be bound, or the function raises `AuditIntegrityError`. Second, the per-member check that each one is settled, still live, or named in the run's recorded group losses. Neither depends on the rescan; both depend on the member set being complete.

You would know this holds when the existing tests in `test_group_satisfiability_gate.py` pass unchanged, and a new scale-sensitive test or benchmark — several thousand groups, asserting a bound well under the 1.5 seconds measured above — fails against the current implementation. Write that test first and watch it go red, because a timing test that passes either way is worse than no test at all.

## Size

Small and self-contained: one function, no interface change, no schema or audit-format implications. A reasonable first ticket in this subsystem. The care needed is in preserving the two checks above, not in the restructuring itself.
