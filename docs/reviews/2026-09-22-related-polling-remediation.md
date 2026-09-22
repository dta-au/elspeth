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
| 2, 3 | `7e52d8afc` | `loadComposerProgress` validated no poller generation after its await; both intervals launch overlapping reads that all pass ownership and land in arbitrary order | Two-mode ownership fence (as `loadInflightMessages` already had) plus a monotone read ticket on both pollers |

Findings 2 and 3 are one commit: they are two defects in the same read-admission
mechanism, and the ownership fence and the ordering fence are adjacent lines of
the same functions.

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
- `npm test` — 278 files, 5040 tests, exit 0.

The full-suite gate result is recorded separately; this change touches shared
web runtime (a registry method, two routes, an exception hierarchy), so the
bounded checks above are not sufficient on their own.

## Deliberately not done

- **Finding 1 is narrower than the audit's correction direction.** It asked to
  distinguish a transient database error from an integrity failure before
  deciding whether to reconnect. The server closes both with 1011, so the
  client cannot tell them apart; doing it properly needs a distinct close code,
  which is a wire-shape change. What landed keeps "never reconnect on 1011" and
  adds REST recovery instead. **Whether to add that close code is an open
  question for the operator.**
- **Finding 4 translates only the two polling routes.** The same typed denials
  can be raised from `bind_request`, `publish`, `replay` and `clear` on the
  compose path, where the heartbeat's existing `PermissionError` arm already
  handles them. Those sites are observed, not changed — they are outside the
  reported defect.
- No live browser or Azure acceptance was performed, and no testcontainer
  PostgreSQL reproduction of findings 4 and 5 was re-run in this worktree; the
  audit's own probes established those on real PostgreSQL.
