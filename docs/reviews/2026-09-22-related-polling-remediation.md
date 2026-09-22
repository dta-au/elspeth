# Polling audit remediation — findings 1–5

Remediation of the five findings in the 2026-09-22 related-polling
investigation (`docs/reviews/2026-09-22-related-polling-audit.md`). Branch
`fix/polling-remediation`, worktree `.claude/worktrees/polling-remediation`,
based on `release/0.8.1` at `0bd0c5e40`. Not merged.

Each finding was reproduced as a failing test against the production code
first, then fixed. Where a test could pass for the wrong reason it was
mutated against the unfixed code and confirmed red.

## What landed

| # | Commit | Defect | Repair |
|---|--------|--------|--------|
| 5 | `3e66806bc` | `finish_request` delegated straight to `run_sync_in_worker`, whose queued submissions are cancelled with their caller, so a cancelled request could discard its exact-token lease deletion | Shield and join the cleanup, mirroring `start_request`'s cancelled-admission handling |
| 4 | `7c2acc588` | The authority's re-checked `PermissionError` was untranslated at both polling routes, and `app.py`'s `OSError` handler rethrows an errno-less one, so revocation mid-poll surfaced as HTTP 500 | Typed `ComposerProgressIdentityInactive` / `ComposerProgressSessionUnavailable`, translated at the two routes to 401 / non-disclosing 404 |
| 1 | `5f7831412` | Close codes 1011 and 1000 neither reconnect nor notify; the only REST fallback unmounts with the Run tab, so an off-tab stream loss left a run looking live | `onStreamEnded` on the transport plus a store-owned recovery poll that outlives every component |
| 1 | `f2b43cfdc` | The durable poller closed 1011 with one reason for a set mixing `OperationalError` with `ProgrammingError`/`IntegrityError`, so no client could tell a momentary outage from a permanent defect and the "never reconnect on 1011" rule retired live streams over database blips | `RunStreamCloseCode` as the sole authority for all 22 close sites in both handlers, plus `BACKEND_UNAVAILABLE` (4503) for the retryable half, which the client reconnects on |
| 2, 3 | `7e52d8afc` | `loadComposerProgress` validated no poller generation after its await; both intervals launch overlapping reads that all pass ownership and land in arbitrary order | Two-mode ownership fence (as `loadInflightMessages` already had) plus a monotone read ticket on both pollers |
| 4 (follow-on) | `d2362c0c8` | The typed denials changed `type(exc).__name__`, which the cross-process PostgreSQL proof pins as a string; the default `pytest tests/` selection deselects the testcontainer marker, so only the gate's testcontainer stage caught it | Pin the precise subclass rather than restore the base name — the base could not distinguish an identity refusal from a session one |

Findings 2 and 3 are one commit: they are two defects in the same read-admission
mechanism, and the ownership fence and the ordering fence are adjacent lines of
the same functions.

Finding 1 took two commits because the first fixed only the half that needed no
wire change. See the correction below.

## Evidence

Backend, in the worktree with both source roots on the path:

- `tests/unit/web/composer/test_progress.py` — 32 passed (finding 5's test is
  red on the previous code: the task was already cancelled with the cleanup
  still queued).
- `tests/unit/web/sessions/test_routes.py::TestComposerProgressRoutes` and
  `::TestComposerInFlightEndpoint` — 11 passed. Negative control: with the
  translation removed from `state.py`, both translation tests fail.
- `tests/unit/architecture/test_session_db_mutation_authority.py` — 265 passed,
  1 failed before re-pinning, all passing after. The typed raises change every
  affected method's scope fingerprint and the two class definitions shift each
  pinned line by 17; all 22 entries were re-derived from the live scan
  (`scan_production_writers`), not hand-edited.

Frontend (`src/elspeth/web/frontend`):

- `npm run typecheck` — exit 0.
- `npm run lint` — exit 0.
- `npm test` — 278 files, 5045 tests, exit 0.

### Close-code change (`f2b43cfdc`)

- `test_websocket.py` + `test_run_stream_close_codes.py` — 31 passed.
- Mutation control on the arm ordering, which is the one silent failure mode
  here: with the broad arm moved above `TRANSIENT_BACKEND_FAILURES`, the
  transient case still closes and still raises but degrades to 1011, and
  `test_durable_stream_transient_backend_failure_closes_4503_and_propagates`
  goes red on exactly that. Reverted and re-confirmed green.
- The frontend parity regexes were controlled before use against the exact text
  they would parse, and mutated four ways — dropping a `case` arm, changing a
  value, dropping a member, and renaming the exported constant. Each is caught.
  The rename case is why the smoke test exists: without it a renamed constant
  makes every parity assertion compare two empty sets and pass.

### Tier-model corpus

`elspeth-lints check --rules all --root src/elspeth`, before and after the
close-code change, compared line by line rather than to zero:

- **2274 findings before, 2274 after.** The ten differing lines are the same
  ten findings at shifted line numbers (the new import and comments move them);
  no rule is newly triggered and none newly suppressed.
- Stale tier-model entries: **194 before, 194 after**; **6 before, 6 after** for
  `web/execution/routes.py`. Those six are the `websocket_run_progress`
  entries, already stale on the base tree.
- The classifier is an `except` tuple rather than an `isinstance` predicate
  specifically so it adds no R5 finding; `websocket_close.py` contributes none.

The full-suite gate at `3d404f14d` (before the close-code change) recorded
`frozen=yes`: ruff/mypy/contracts exit 0, lints 2274, pytest 2 failed / 55621
passed — the two failures being the pre-existing
`test_freeform_planner_failure_translation` reds on `release/0.8.1` — and
testcontainer 1 failed / 558 passed, which is the stale assertion repaired in
`d2362c0c8`. A re-run covers the close-code change.

## Correction

An earlier revision of this document, and the message of commit `5f7831412`,
said of finding 1:

> The server closes both with 1011, so the client cannot tell them apart.

**That was wrong, and it is worth recording why rather than quietly deleting
it.** The six 1011 close sites in `routes.py` carry five distinct reason
strings, and the reason does reach the browser on `CloseEvent.reason`. So the
server was not, in general, sending an undifferentiated signal.

What is true is narrower and is what actually justified the new close code. The
distinction the audit asked for — transient versus permanent — lives entirely
inside one arm, `except (SQLAlchemyError, ConnectionError, OSError)` in the
durable poller. That arm sends a single reason, "Run progress unavailable", for
both halves of the mixed set, so no client can split it however carefully it
reads the text. And the reason string is unfit to dispatch on regardless: it is
unversioned, capped at 123 bytes, and droppable by an intermediary.

Two further claims made while scoping the change were also measured and found
wrong, and are corrected here for the same reason:

- The change was described as touching 18 close sites. It is **22**, counted by
  `ast` rather than by eye.
- The tier-model cost was quoted as "up to 6 `websocket_run_progress` allowlist
  entries will go stale and need re-signing." They were **already stale before
  this branch existed**: all 12 `routes.py` entries carry an `ast_path` whose
  leading module index is off by +4, and the gate's own corpus reports exactly
  those 6 as stale on the base tree. The change adds none. See the evidence
  below.

## Deliberately not done

- **Finding 4 translates only the two polling routes.** The same typed denials
  can be raised from `bind_request`, `publish`, `replay` and `clear` on the
  compose path, where the heartbeat's existing `PermissionError` arm already
  handles them. Those sites are observed, not changed — they are outside the
  reported defect.
- No live browser or Azure acceptance was performed. Finding 5 **is** now
  confirmed on real PostgreSQL in this worktree, as a side effect of repairing
  the test in `d2362c0c8`: the stale assertion was the first statement after
  revocation, so the half of
  `test_peer_rechecks_revocation_and_teardown_still_removes_exact_token` that
  proves teardown deletes the exact request token had never executed. It runs
  now and passes.
