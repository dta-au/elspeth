# Bug: `session_fork` reports `integrity_error` yet leaves a user-visible, partially-populated session

- **Reported**: 2026-08-19
- **Environment**: DTA-Dev AWS ECS, `elspeth-web`, task definition 25
  (`c668d25f9934c06b8bfd527a45f04db4d34d5d13`)
- **Parent session**: `7aa0ab13-8f2e-457c-97a7-da0427c193f2` (`user_id=johnm`)
- **Severity**: High — the operation's reported outcome contradicts what it
  committed, the user is shown sessions that are not what they appear to be, and
  the natural response (retry) multiplies the damage.

## Summary

Two `session_fork` operations were recorded as `status=failed` with
`failure_code=integrity_error`. **Both nevertheless created a session**, and both
of those sessions are listed to the user alongside real ones. Each carries most
of the parent's transcript but almost none of its state.

The user, having been told the fork failed, retried — producing a second orphan
from the same originating message.

## Evidence

Both operations, from `guided_operations`:

| operation_id | created_at (UTC) | settled_at (UTC) | status | failure_code | originating_message_id |
|---|---|---|---|---|---|
| `023d81f7-…c16ac9` | 03:28:00.307667 | 03:28:00.622755 | failed | `integrity_error` | `f31e8b38-…29054d` |
| `6fcbe899-…d73fc95` | 03:28:09.279142 | 03:28:09.564245 | failed | `integrity_error` | `f31e8b38-…29054d` |

Both sessions exist, created ~30 ms **before** their operation settled as failed:

| | parent `7aa0ab13` | fork `2c19a467` | fork `d1cae706` |
|---|---|---|---|
| created_at (UTC) | 2026-08-18 01:24:23 | 2026-08-19 03:28:00.337 | 2026-08-19 03:28:09.308 |
| `forked_from_session_id` | NULL | `7aa0ab13…` | `7aa0ab13…` |
| `forked_from_message_id` | NULL | `f31e8b38…` | `f31e8b38…` |
| chat_messages | 86 | **72** | **72** |
| composition_states | 19 | **1** | **1** |
| latest state version | 19 | **1** | **1** |
| runs | 3 | **0** | **0** |
| blobs | 1 | **0** | **0** |
| interpretation_events | 4 | **0** | **0** |

Both appear in the user's session list:

```
d1cae706-7c3b-42b1-a769-5d41e3293f69  Session — 18 Aug 2026 (3) (fork)   2026-08-19 03:28:09
2c19a467-0c96-4612-9e50-4340e358c034  Session — 18 Aug 2026 (3) (fork)   2026-08-19 03:28:00
```

## The core defect — CONFIRMED

Independently of what a fork is *supposed* to copy, the observed combination is
incoherent: the operation reported failure, and the session was committed and
surfaced anyway. One of the two must be wrong.

- If the fork succeeded, reporting `integrity_error` is a false negative that
  drove the user to retry.
- If it failed, the session must not survive, and must not be listed.

The duplicate is a direct consequence: identical `originating_message_id` on both
attempts means a retry of the same user action produced a second session rather
than resolving or resuming the first.

## Partial population — NEEDS CONFIRMATION AGAINST INTENDED SEMANTICS

The forks carry 72 of the parent's 86 messages but one composition state against
the parent's nineteen, no runs, no blobs, and no interpretation events. The
parent itself is intact and undamaged.

Whether a fork is meant to deep-copy composition history, blobs and runs is a
design question this report cannot settle. Two observations that do not depend on
that answer:

- A fork whose transcript discusses a pipeline at v19 while its state is v1 is
  misleading to open, regardless of intent.
- The transcript references an uploaded CSV under the **parent's** blob path.
  With `blobs = 0` on the fork, whether that source resolves depends on whether
  blob references are session-scoped. If they are, the fork cannot run.

The message count (72 of 86) is the strongest hint that the copy was interrupted
rather than deliberately scoped, but the parent gained messages after the fork
point, so this is suggestive, not conclusive.

## Inferred cause

The fork is not atomic. The session row and part of the message copy commit, a
later step violates a constraint, the failure is recorded — and the earlier
commit is not rolled back. The ~30 ms gap between session creation and the
operation settling as failed is consistent with this.

Not verified against the implementation; the code path was not read for this
report.

## Reproduction

1. Open a session with substantial history (19 composition versions, several runs).
2. Fork it from a message.
3. Observe the operation report `integrity_error`.
4. Observe a fork session appear in the session list regardless.
5. Retry the fork — a second orphan appears.

## Suggested fixes

1. **Make the fork atomic.** One transaction covering the session row and every
   copied artifact, or an explicit compensating delete on failure.
2. **Never list a session whose creating operation did not succeed.** Even with
   (1), a guard on the read path keeps a partial commit invisible.
3. **Make retry idempotent** on `(parent_session_id, originating_message_id)` so
   a second attempt resumes or replaces rather than duplicating.
4. **Surface the constraint that was violated.** `integrity_error` alone gives an
   operator nothing to act on and gives the model nothing to repair.
5. **Clean up the two existing orphans** (`2c19a467…`, `d1cae706…`) once the
   intended fork semantics are settled.
