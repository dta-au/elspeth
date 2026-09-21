# Composer feedback repairs — 2026-09-22

This repair covers all eight defects identified in the targeted systems review
following approval-conflict fix `e26a1dd59`. The release target is `release/0.8.1`.

| Defect | Repair and regression |
| --- | --- |
| Delayed snapshots resurrect retired approvals or erase newer cards | Per-session request ownership orders both interpretation snapshot endpoints. Terminal IDs include superseded and abandoned without counting them as approvals. Inline events survive older reads. Tests cover reverse response ordering, inline additions, independent sessions, and reset. |
| Approval response overwrites another session's composition | Both guided and freeform callbacks capture session and activation generation. Tests cover A-to-B-to-A navigation and a working current-activation control. |
| Busy response hides a still-pending proposal as stale | Reconcile with authoritative proposal status and preserve conflict detail. A pending proposal remains actionable; a failed refresh cannot manufacture retirement. |
| Successful proposal decision reported as failed after a GET failure | Publish the terminal receipt before hydration. Report refresh failure separately, and clear the obsolete composition after acceptance. Terminal receipts survive stale and overlapping list reads. Recovery tests assert no second decision POST. |
| Execute incorrectly claims an active run | Only the actual `run_already_active` discriminator gets active-run copy; other errors retain their detail. |
| File deletion misidentifies its blocker | Preserve the server's retention or contention detail instead of assuming an active run. |
| YAML export mislabels contention as validation failure | Neutral error heading preserves the actual detail; Retry YAML export issues a fresh GET. Genuine validation blockers remain visible. |
| Delayed 401 logs out a replacement login | Capture credential and generation at fetch dispatch. Unauthorized handling can invalidate only its originating login. Auth lifecycle results are generation-fenced, including identical token text; replacement login waits for previous-principal cache cleanup. |

The interpretation store also rejects mutation publication after reset, and
logout clears its projections and request ownership. Concurrent logout calls
share the cleanup barrier. No provider bypass, prompt rewrite, schema change,
or backend mutation behavior change was introduced. The sole Python edit
corrects a route docstring that described the old all-409-means-stale behavior.

## Validation

- Full frontend suite: **277 files, 5,004 tests passed**, process exit 0.
- Full frontend typecheck, ESLint, and production build: exit 0 each.
- Candidate files were hashed before and after aggregate validation; unchanged.
- Focused Python route and attribute-contract tests: **8 passed**, exit 0.
- Ruff check and format check of the Python docstring edit: exit 0.
- Trust-tier ratchet against the release baseline: no added findings. This
  does not clear the repository's existing package-signing gate.
- Regression tests failed before repair. Additional negative controls removed
  activation-generation fencing and terminal-receipt preservation separately;
  the corresponding tests failed, then passed after restoration.
- Independent review found and verified fixes for two draft gaps: older list
  reads overwriting decision receipts, and deferred logout cleanup retaining
  previous-user caches.

The initial full frontend run caught the YAML retry control using a raw button;
it was changed to the shared Button primitive. Two registry subprocess tests
were denied by the sandbox. The final full run executed with subprocess access
and passed all tests. No test was skipped to obtain the result.

Aggregate frontend validation ran on baseline `fa26f34b8`. Before integration,
the candidate incorporated `d9869c3f8`; its changes are backend pricing and
associated documentation/tests. The frontend tree is identical across those
baselines, so the frontend proof remains applicable.

## Local browser reproduction

Loaded the candidate's built assets only in an isolated browser against the
local installation. On existing test session
`49bd5b14-6088-479b-b045-f2711f940344`, held a bounded COMPOSE lease using the
real session authority. Opening YAML returned the real contention error and
displayed Retry YAML export. Captured the actual rendered tab and button in
`output/playwright/composer-yaml-retry-detail.png` (local artifact).

After releasing the lease, clicking Retry issued another request and displayed
the pipeline's existing runtime-preflight validation blocker. Thus the live
check proves retry and truthful error replacement, not successful export of
this still-unapproved pipeline. The component regression separately covers a
successful export response after retry. Both controlled leases were released
and the isolated browser was closed; shared deployment assets were unchanged.

These are bounded frontend and local-browser proofs, not a full Python or
PostgreSQL suite, production deployment, or confirmation of the original
production incident's exact cause.
