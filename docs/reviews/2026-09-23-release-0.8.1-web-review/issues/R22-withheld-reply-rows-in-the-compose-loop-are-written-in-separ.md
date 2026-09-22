# R22. Withheld-reply rows in the compose loop are written in separate transactions ahead of the turn's atomic audit cohort

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-09#3 |

## Finding

- **Location:** `src/elspeth/web/composer/service.py:5483-5492`. The same pattern is at `:3435`, `:5991`, `:6315` and `:7274`.
- **Wrong:** `_persist_turn_audit` calls a plain `add_message` for the withheld reply before `persist_turn_audit`. The planner path, by contrast, settles withheld replies inside `add_messages_atomic` (`:4816-4820`, `:4867-4874`). **Scenario:**
  - If the extra write raises a raw SQLAlchemy error, the error leaves the mandatory-audit `finally`. That skips the `failed_turn` stamping and replaces an in-flight plugin crash, advisor exception or deadline envelope.
  - If the cohort later rolls back (for example `unwind_audit_failed`), the withheld row remains as an orphan.
- **Fix:** Stage the rows with `BufferingRecorder.record_withheld_reply` and settle them inside the cohort. At minimum, move the write after `persist_turn_audit`.
- **Sources:** be-09#3.
- **Verifier notes:** Reaching this needs an advisor-repair tool turn with prose plus a DB failure. The orphan row holds true model prose and is excluded from replay.


## Source findings and verification

### be-09#3: Compose-loop withheld-reply rows are separate transactions, written before the turn's atomic audit cohort

- **Reported at:** `src/elspeth/web/composer/service.py:5486`; reviewer severity medium; category audit-integrity; diff-anchored True.
- **Summary:** _persist_turn_audit (called from the mandatory-audit finally block) now issues a separate add_message for the withheld reply before persist_compose_turn_async. The same pattern is at 3435, 5991, 6315 and 7274. The planner path deliberately settles withheld replies inside add_messages_atomic (4867-4874, "never as a separate write").
- **Failure scenario:** If a plugin crash or stale-state CAS makes persist_compose_turn_async roll back (unwind_audit_failed), the withheld row stays committed as an orphan for a turn with no assistant or tool rows. If the extra write itself fails, the error comes out of the finally block before the mandatory tool audit runs. That skips turn_audit's AuditIntegrityError/failed_turn stamping and replaces any in-flight advisor or plugin exception. The deadline handlers (5991/6315) write after the deadline has passed and can swap the timeout envelope for a different error class.
- **Evidence:** service.py:5484-5494 is ahead of persist_turn_audit. turn_audit.py:109-111 states the invariant "a mid-step raise must leave the DB in its pre-step shape". The planner comment at service.py:4816-4820 says withheld replies settle inside the cohort.
- **Suggested fix:** Stage compose-loop withheld replies with BufferingRecorder.record_withheld_reply and settle them inside the turn/terminal cohort. Otherwise, document the sidecar as non-transactional and amend the P4 invariant.
- **Verifier (trace):** upheld, confidence medium, severity low. The structural claim holds at 74c0ce0db, and I could trace both failure paths. The consequences are narrower than the finding says, so I rate it low rather than medium.

**Confirmed.**
- `_persist_turn_audit` (service.py:5483-5492) calls `_persist_withheld_reply` before `persist_turn_audit`.
- `_persist_withheld_reply` (service.py:3381-3388) is a plain `sessions.add_message` in its own transaction (sessions/service.py:9369+). It does no error translation, so it raises raw SQLAlchemy errors.
- The call runs inside the mandatory-audit `finally` in the compose driver (service.py:7373-7393).

**Path 1: an exception replaces the in-flight one.** Setup: `advisor_repair_context_introduced` is true, the tool-call turn carries non-blank prose, and the `add_message` raises (for example an `OperationalError` such as SQLite "database is locked" or disk full). That error propagates out of the `finally` before `persist_turn_audit` runs. As a result:
- the `failed_turn` stamping at turn_audit.py:286-291 never runs;
- when a plugin crash is pending, the service's deliberate unwind-primacy handling never runs. At sessions/service.py:6901-6910 and 6955-6966 that handling returns `unwind_audit_failed` so the captured plugin crash stays the primary error. Here a raw DB error replaces it instead;
- any in-flight advisor-checkpoint exception is also replaced.

**Path 2: orphan row.** When `crash_pending` is set and the second write rolls back (stale-state CAS or `OperationalError`, sessions/service.py:6901-6966), the withheld row stays committed with no assistant row or tool rows.

The deadline sites (5991, 6315) and the terminal site (3435) have the same shape. The planner path shows the atomic alternative: it stages the reply on the recorder with `record_withheld_reply` and settles it inside `add_messages_atomic` (service.py:4816-4820, 4867-4874, audit.py:264).

**Why low, not medium.**
- **Narrow reach.** A path only fires when every one of these holds: an advisor-repair tool turn with non-blank prose (blank content returns early at 3376), plus a DB write failure or a stale-state CAS during a crash unwind.
- **Path 1 is mostly misclassification.** A DB failure would largely have failed the cohort write too, so the main change is which error the route sees and the lost `failed_turn` metadata. Unaudited state is unlikely: the composition-state advance lives in the same `persist_compose_turn_async` cohort, so it is also lost rather than persisted without proof.
- **Path 2 is a truthful row.** The orphan is model prose that was really produced. By design it stays out of the chat view and out of provider context (service.py:3368-3374), so it cannot distort replay.
- **The invariant is scoped to its own step.** The quoted turn_audit.py:109-111 invariant covers `persist_turn_audit`'s own step, which runs after the withheld write.
- **Precedent for separate writes.** The docstring cites the advisor disclosure row (P4-D6 A2b) as a precedent for separate fenced writes in the compose loop.

**No ruling covers it.** Neither recent-code-hints.md nor docs/reviews/2026-09-22-composer-reply-withholding-review.md records that the compose-loop sidecar may be non-atomic or may run before the mandatory audit. So this is not an adjudicated decision, and the suggested fix (stage on `BufferingRecorder` and settle in the cohort, or at minimum move the write after or around `persist_turn_audit`) is valid.
