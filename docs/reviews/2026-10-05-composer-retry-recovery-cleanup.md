# Composer retry and recovery cleanup

This local candidate starts at freshly fetched main commit
`edc844699a350a90a624089e12a5a1e75b7b2dde`. The preceding
[timeout investigation](2026-10-05-composer-timeout-investigation.md) describes
that immutable baseline, including the cancellation and persistence lifecycle.
This note describes the candidate changes separately. No infrastructure,
timeout configuration, provider retry policy, deployment, or execution ownership
model changes are included.

## Resulting behavior

An unstructured HTTP 504/524 or ambiguous browser transport failure starts
read-only recovery. The browser reads messages, current draft state, proposals,
and composer progress. An unavailable read, missing request count, active lease,
or complete-progress snapshot without its saved reply remains unconfirmed.
Automatic recovery issues GETs only; its message action is labelled **Refresh**.
Request identity and the originally requested state are retained.

After quiescence is established, a saved final assistant response is displayed.
A turn without a final reply may offer an explicit **Retry** to request a new
response using saved work. This is fresh composition, not continuation of the
interrupted provider request. Repeated clicks are serialized locally, and the
API repeats its eligibility checks under the existing compose lock and fenced
session operation lease.

The recompose route binds to the latest exact user ID. It allows an interrupted
assistant tool-call prefix, preserving the originating user's position before
that prefix in history and reading the current saved state. It blocks a saved
no-tool assistant response, including an empty or failure-worded response, and
a bound pending or committed pipeline proposal. Those refusals trigger reads
instead of another provider invocation. Rejected proposals do not themselves
prevent explicit new work. Stored tool calls are not mechanically replayed.

Message retrieval now reads all offset pages, with authenticated sequential
500-row GETs. It deduplicates IDs, preserves server sequence order, rejects a
non-progressing full page, and rejects the whole operation when a page fails.
Opening a session inspects the latest saved turn and its settlement status.
Recovery respects existing activation, owner, proposal-decision, and read-ticket
fences so older results cannot overwrite a newer session or decision.

## Boundaries

- Composer POSTs remain buffered and inline. A disconnect can still cancel the
  provider loop; saved tool/state/audit prefixes and protected final settlement
  retain their existing behavior. This cleanup does not make composer work
  detached or guarantee an eventual final answer.
- An explicit retry may incur another provider call and the provider may choose
  another non-idempotent effect. There is no exactly-once tool guarantee or
  durable retry-attempt receipt. A new POST is never automatic recovery.
- The proposal wire representation does not expose its originating user ID.
  The API performs the exact association check. On reload the browser can
  initially offer Retry for a proposal-backed turn, receive the authoritative
  refusal before provider work, and then show Refresh and the saved proposal.
- Reload loses browser-only failure metadata. Recoverable progress reasons are
  honored; existing backend policy/admission checks remain authoritative. This
  is not a new durable failure-disposition API.
- Offset pages and the four read surfaces are not an atomic snapshot. Progress
  checks and backend saved-outcome guards handle incomplete observations.
- Actual deployed commit, middleboxes, and effective timeout values remain
  deployment facts to verify. Source behavior alone does not establish them.

## Validation

Tests use local fake providers and disposable storage; neither paid provider
calls nor real production requests are involved. The following focused and
static checks completed with actual process exit 0:

- Twelve new backend retry regressions. Baseline controls reproduced four
  failures fixed by this candidate and one passing rejected-proposal control.
- 188 frontend store/ownership tests, including 25 new store regressions and
  stopping during retry admission. Isolated baseline controls reproduced nine
  failures while three controls passed.
- Thirteen history-pagination tests and 47 shared composer/message UI tests.
- Ten PostgreSQL cross-process composer lifecycle tests against a disposable
  local container. The provisioned database environment override was unset.
- Python mypy across `src/` and `elspeth-lints/src/`: 1047 source files.
- Ruff check/format on changed Python files; frontend TypeScript and complete
  frontend ESLint; contract and soft-mapping census checks; diff whitespace.

The final full frontend application suite passed: **4058 tests in 248 files**,
exit 0. Its final production and test source were frozen throughout the run.
The broader backend selection completed with 3873 passed, three skipped, one
expected failure, and three failures (exit 1). Its workers imported the new
regression file before two fixture corrections: providing the mandatory
metadata description and the proposal acceptance draft hash. Those three
failures are confined to the old fixtures; the final file already passed all
12 cases independently. This run is not claimed as a frozen green suite.
The final frozen selection of the corrected file, all session route tests, and
composer attribute contracts passed **337 cases**, exit 0. No production code
was changed to resolve those fixture failures.

The three skipped masquerade cases require Python 3.13 PEP 696 support; the
environment uses Python 3.12. The mutation-authority inventory test reports an
expected failure for inventory drift, so its audit is not cleared.
Other affected whole-tree test gates ran in the broader selection, including
masquerade, direct mock discipline, and landscape mutation fencing. The fixture
corrections add required domain data and assertions, without changing dynamic
attribute probes or mock constructors.

Independent final review found no remaining must-fix source defect. It identified
and verified corrections for two races: a failed GET stopping periodic recovery,
and a rejected older message read overwriting a newer transcript through the
error handler. Regression tests cover both. Automatic entrypoints were traced
to ensure they can only read, never send or recompose.

The standard `npm test` dependency precheck fails because the installed braces
package does not throw the expected depth-limit exception. The same command
fails on untouched main. Application Vitest is therefore also run directly;
dependency files and shared installed packages are unchanged.

The keyless whole-source lint exits 1 on both baseline and candidate. Comparing
AST scope, rule, complete message/fingerprint, and occurrence count after only
normalizing checkout paths and shifted locations found **zero added or removed
diagnostics or warnings**: each has 4555 diagnostic occurrences and 118 warnings.
Comparison controls verified that changed finding text/fingerprints are detected.
Existing expired/stale allowlist debt, including two `recompose` entries, remains.
No signatures or suppressions were changed; keyless comparison is not operator
signature clearance. Full Python and full testcontainer selections were not run;
the bounded route/frontend cleanup is validated by the affected suites and
relevant whole-tree gates rather than a pre-merge claim.

## Evidence and handoff

The local branch is `fix/composer-retry-recovery-20261005` in
`/workspace/elspeth-retry-remediation`; HEAD remains the base commit and changes
are uncommitted. The complete patch is
`/tmp/elspeth-composer-retry-remediation.patch`, including the new tests and
reports. It passes `git apply --check` against the clean original checkout.
Original checkout tracked/index state remains clean. No push, PR, merge,
deployment, infrastructure change, or production request was performed.

| Evidence | Local file |
|---|---|
| Final full frontend application suite | `/tmp/retry-frontend-vitest-final.log` |
| Final frozen backend routes/regressions/attributes | `/tmp/retry-backend-frozen-final.log` |
| Broader backend run and superseded fixture failures | `/tmp/retry-backend-broad.log` |
| PostgreSQL cross-process composer lifecycle | `/tmp/retry-postgres-composer.log` |
| Independent final review | `/tmp/retry-final-independent-review.md` |
| Frontend implementation, regressions and baseline controls | `/tmp/frontend-retry-report.md` |
| Backend implementation and baseline controls | `/tmp/backend-retry-report.md` |
| History pagination acceptance | `/tmp/history-retry-report.md` |
| Keyless lint comparison and controls | `/tmp/retry-lints-comparison.md` |
| Python typing / contracts | `/tmp/retry-mypy.log`, `/tmp/retry-contracts.log` |
| Final frontend typing / ESLint | `/tmp/retry-frontend-typecheck-final.log`, `/tmp/retry-frontend-lint-final.log` |
| Standard dependency precheck on untouched main | `/tmp/retry-frontend-dependencies-baseline.log` |
