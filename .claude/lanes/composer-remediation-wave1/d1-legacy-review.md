# D1 legacy acceptance/UI review

Reviewed frozen worktree `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1` against base `5e192342c3fb025fd9a91244bebe437158e9de8e`, using the brief, implementation report, review diff, production dependencies, tests, and recorded logs. No source edits, test reruns, or agents were used. The four source/test SHA-256 values still match `d1-legacy-freeze.txt`.

## Verdicts

- **Specification compliance: APPROVE after Fix1.** The sole regression-coverage finding is ADDRESSED by the two real route controls and the failing early-equality mutation described below.
- **Code quality: APPROVE.** No production correctness defect found in the reviewed patch and no new breakage found in the scoped test amendment. The narrow exception handling, existing authority use, and frontend change are appropriate. Broader verification/integration and the explicitly open canonical/staging work remain parent-owned.

## Finding

### P2 — The tests do not pin equality after required-control/review finalization — ADDRESSED in Fix1

The following records the initial finding; the scoped re-review below closes it.

Location: `tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py:61` (`test_content_identity_controls_legacy_accept`); protected production boundary: `src/elspeth/web/sessions/routes/composer/proposals.py:579` through the equality decision at line 612.

The four cases seed `_set_pipeline_arguments`, which is a CSV source, no nodes, and a JSON sink. The three no-op cases leave that already-finalized graph unchanged. The successful control changes `metadata.name` in the dispatcher itself. None creates a result whose content equals the current state immediately after dispatch but differs after the existing required-control/review finalizer. Consequently these controls do not distinguish the implemented ordering from an incorrect early equality rejection that skips finalization.

The focused existing acceptance suites supply secret-policy, validation, transient-preflight, stale-state, and blob-effect controls, but no legacy accept case at this equality/finalization boundary. The standalone `test_required_control_autowire.py` exercises the helper and even demonstrates an acceptable candidate acquiring controls, but does not exercise the new acceptance decision or its ordering.

Add a real legacy HTTP/store regression using supported policy-selected controls and existing valid authority fixtures: a version-incrementing empty/same-value edit must be content-identical at dispatch, gain a real content delta during finalization, then commit with the finalized controls/review evidence, one accepted event, a new state, and actual preflight. Retain the ordinary no-op sibling. Record a negative control showing that placing equality before finalization fails this regression. Do not substitute a mocked finalizer or fake preflight success. This is a test requirement gap, not a claim that the present production order is wrong.

## Reviewed behavior and evidence

- The replay catch is exactly `ToolArgumentError`. `args[0]` is the protocol's canonical, safe-by-construction diagnostic; the chained cause is not serialized. The real invalid-filename case asserts terminal rejection and absence of raw content/filename leakage. Generic dispatch `ValueError` and named rejection faults remain exact escaping exceptions.
- Semantic equality uses the existing content hash, excluding version, after the existing finalization block and before `_state_data_from_composer_state`. The real matrix verifies same-version success, empty metadata patch, identical-value version advance, and an actual changed-name counterpart; it measures state history, preflight calls, actors, terminal event cardinality, retry dispatch count, and pending reload.
- All blob-store-only tools are exempt, using the existing full classification. Receipt-first update/delete replay remains ahead of dispatch/equality. Existing tests prove actual file effects, no graph delta, tampered-receipt refusal, recovery after an applied effect, changed-head replay, and no redispatch. Secret wiring mutates the graph; the separately dispatched async interpretation tool is outside this legacy `execute_tool` path.
- The historical manually staged `create_blob` test correctly preserves its pre-existing durable blob effect followed by 409/pending and no graph advance. It must not be called a successful no-op terminalization or supported receipt replay. This patch adds no receipt support.
- The rejection helper catches only the terminal-conflict sentinel; competing committed/rejected winners retain their event actor and audit binding, and response text describes the already-terminal outcome. Missing-loaded-row `KeyError`, `AuditIntegrityError`, and unexpected writer `ValueError` are tested by identity at the real writer seam and are not swallowed.
- The cancellation tests cover dispatch, rejection before/after durability, and lease close, with one terminal event and retry safety. Named rejection failures survive cancellation plus cleanup failure by identity, with cleanup notes. Existing lease-close synthetic-cancellation behavior is expressly retained; SQLite/gated seam tests are not PostgreSQL contention evidence. The back-to-back cancel calls in the phase test do not independently prove every possible multi-phase repeated-cancellation interleaving.
- Frontend tests cover both new terminal types, existing validation details, successful refresh/reload, refresh failure, switching sessions, same-session generation renewal, and transient failure. Existing conflict behavior remains in the unchanged branch. The added pre-refresh `isCurrent()` guard and existing post-refresh/generation guards prevent publication into a later activation.
- Canonical settlement/staging and immediate auto-commit are unchanged and explicitly OPEN follow-up tasks.

## Verification status

The implementation report records explicit exits. The saved logs corroborate final results of 28 new backend cases, 45 focused existing cases, and 190 session-store cases; scoped lint/format, mypy, contracts, ESLint, and TypeScript checks are also recorded successful. Import provenance resolves both Python roots into this worktree.

Baseline-red logs show the intended four backend failures (three content-identical cases plus the real typed argument error) with three passing controls, and eight frontend failures with two passing controls. These provide meaningful red/green evidence for the implemented behavior, but do not close the finalization-order gap above.

At initial review the focused architecture/attribute/mock gate was incomplete. Its subsequently completed log and updated implementer report now record 306 passed, 1 xfailed, exit 0. The existing authority-inventory burn-down xfail is not a clean authority-inventory result. No full suite, PostgreSQL proof, or all-rule corpus clearance is claimed.

## Fix1 scoped re-review

**Finding status: ADDRESSED. No new findings.** Reviewed `d1-legacy-fix1.diff`, the appended implementer report, the new real route test, the in-memory mutation runner, and the final green/red logs. No tests were rerun. Current hashes match the regenerated four-file freeze; production and frontend hashes are unchanged from the initial review.

`test_legacy_accept_compares_content_after_required_control_finalization` covers both an empty patch and the same metadata value. It builds the underlying graph through actual `set_pipeline` execution and inline-blob custody, then saves that producer-derived graph through the existing fenced seed helper. The required-control policy and profiles come from the existing compiled fixture; a nominal principal-specific snapshot retains the actual compiled `control_modes`. This models a persisted graph predating required controls without inventing invalid owned records or altering shared fixtures.

Both cases assert that real `set_metadata` dispatch succeeds, remains valid, increments the version, and retains the exact current content hash before finalization. The HTTP route must then return 200 and persist a different state with the selected prompt-shield/content-safety profiles and canonical pending pipeline-decision disclosures. Persisted content must equal the actual preflight input and differ from the original graph; history/event assertions require two state rows and exactly one accepted event. The dispatch/preflight observers delegate to production implementations, and the finalizer is not mocked. The ordinary no-op siblings remain intact.

The negative-control runner moves exactly the complete equality/refusal block ahead of finalization in the already-registered endpoint's in-memory code, with an assertion that precisely one block is moved. It changes no production files and runs in a separate process. `d1-fix1-mutant.log` shows both new variants reaching the successful, valid, equal-content dispatch assertions and then failing specifically on expected HTTP 200 versus mutant 422 `proposal_no_change`. This directly detects the regression identified in the initial review.

The implementer records exit 0 for the amended route module plus required-control tests; `d1-fix1-green.log` confirms 93 passed (30 route cases plus 63 helper cases). The two-case mutant records exit 1 and 2 failures with 28 deselected. Changed-test Ruff and format checks are recorded successful. The amendment resolves the sole review blocker without changing production behavior, broadening the task, or closing the unsupported historical `create_blob` receipt boundary.
