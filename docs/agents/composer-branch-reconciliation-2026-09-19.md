# Composer branch reconciliation — 2026-09-19

Base: `release/0.8.1@8f4384936aac64e34e8c617a635f092c261ba559`.
Integration branch: `integration/composer-unlanded-20260919`.

This is a source and custody inventory for the retained Composer branches and
detached review trees. It records what the integration branch carries and why
the other snapshots were left in place. No donor worktree was edited or removed.

| Donor | Disposition at the base commit |
| --- | --- |
| `feature/composer-decision-panel@c728f3492` | Carried. The decision panel, its readiness projection, shared Apply prompt and tests were absent from release. The commit applied cleanly on the integration branch. Its phase-1 scope and known gaps remain as filed on `elspeth-cb0d4b8dba`. |
| `composer-phase5-durable-presence@f2ae66fc9` and `composer-wires-phase5-sparse@dfcc51682` | Authored-presence behavior and `test_sparse_argument_presence.py` are already on release through the Phase 5 integration (`1cee88f30`). The later `dfcc51682` version of `pipeline_commit.py` is byte-identical to release. The dirty durable-presence test files are older and omit current audit-hash, identity and candidate-acceptance assertions. The dirty sparse wire-parity test predates the shared admitted-wire census. |
| `composer-phase5-producers@32dacee78` | Structured error producer tests and guided replay are on release through Phase 5. The dirty `pipeline_commit.py` would remove the release's validated canonical-hash branch; the dirty census is an older count. |
| `composer-phase5-frontend@0163c1c0d` | Structured error decoder, humanizer and presentation files are on release through Phase 5. Differing client and ProposalDiff files gained later null-response, FastAPI error, projector ownership and dispatch guards on release. |
| `composer-wire-scorecard@f8476245a` | The scorecard scripts and scorecard tests are on release through the later campaign integration (`40c4c61e7`). Differing census and gate files are newer on release. |
| `fix/composer-phase6-response-residue@fb228fead` | The destination-note and private-label fixes are on release through Phase 6 (`40c4c61e7`). The release's private-label test adds a real SQLite persist/reload witness beyond the donor's capturing fake. |
| `composer-phase5-evaluation@0d1e85b0e` | Branch tip is a release ancestor. Its distinct dirty convergence test expects five provider calls and removes the release's sixth tools-disabled explanation and identity setup; current release behavior and stronger test stay authoritative. |

Detached review worktrees were checked separately. All 146 changed blobs in
`composer-hardening-gate` and both changed blobs in
`composer-dag-vanguard-review` are byte-identical to a blob in release history.
The diagnostics review's distinct planner test expects a generic provider
failure for a noncanonical schema where release now raises `FrameworkBugError`.
The API baseline test predates the release's empty-session and malformed-body
checks. The hash-durable review contains older server-side uploaded-source
derivation that release replaced with provider-authored behavior; it is not
restored. All six distinct checks from the Phase 5 frontend review file
`phase5-review-private.test.ts` were adapted into
`src/elspeth/web/frontend/src/api/compositionStateBoundary.test.ts`: ordinary
metadata isolation, exact unusual error strings, required fields on a later
error, and fail-closed later-version decoding.

On 2026-09-19, all 14 donor worktrees were removed after a fresh status,
process-use, ignored-file and blob-reachability check. Of 205 changed donor
files, 190 matched release or integration history. Exact copies of the other
15 files and a hash manifest are retained locally under
`.claude/lanes/composer-donor-cleanup-20260919/`; the three non-cache ignored
evaluation scenarios were byte-identical in the main checkout. Donor branch
refs remain available. The integration worktree was retained.

The reconciled donor commits change only the decision-panel frontend files,
the adapted decoder boundary test, this reconciliation note, and the separate
[landing plan](../plans/2026-09-19-composer-landing.md).

## Verification on the integration branch

- Baseline backend envelope gate before applying the donor commit: 95 passed.
- Focused frontend panel and decoder checks: 354 passed.
- Full frontend Vitest after allowing its existing Node subprocess tests to
  spawn: 4,767 passed across 251 files, exit 0. The sandboxed first attempt
  had two `spawnSync ... EPERM` failures; both passed in the permitted rerun.
- Frontend TypeScript typecheck, ESLint, Stylelint and production build: exit 0.

These donor checks do not establish live-browser acceptance. Validator
suggestion rows remain transient after reload and the advisor's discarded
suggestion is not on this wire. The independent landing review subsequently
measured that `Open checks` already switches narrow Compose to Pipeline and
focuses Checks; the donor's navigation warning was stale. The landing plan
records the separate review repairs and validation sequence. The phase-2
migration of review and proposal controls remains `elspeth-b0ef01ea25`.
