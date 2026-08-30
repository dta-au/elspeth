# Deferred Platform Completion Pause Checkpoint

## Latest pause boundary -- 2026-07-30T06:29:44+10:00

This section supersedes the older capture below wherever the two differ.  The
work remained on the same worktree, branch, and uncommitted HEAD; no release
branch file was edited.

### Exact branch and release state

- Worktree: `/home/john/elspeth/.worktrees/deferred-platform-completion`
- Branch: `codex/deferred-platform-completion`
- HEAD: `fcd5df055d540b1d3c933aa3cddaa38e4a15ccab`
- Local `release/0.7.2`: `f2bdf0f0951edacb5a36b9fe3c2095647550dedd`
- Fetched and live remote `release/0.7.2`:
  `f40058424b045e978532e4e59c64e8593968410f`
- The remote tip was checked again immediately before capture and was stable.
- The latest release increment from the prior assessed tip `2af5ce0` contains
  substantive row-union/DAG work: 76 changed files, 2,812 insertions, and 149
  deletions.  It overlaps the current dirty worktree in one newly changed file,
  `tests/unit/web/execution/test_service.py`, and does not overlap any
  production file in the accepted proposal-rejection slice.
- Across the entire interval from this branch's frozen HEAD to the current
  remote release tip, 50 dirty-worktree paths overlap.  Therefore Task 5 may
  resume from this checkpoint, but release reconciliation is a hard boundary
  after Task 5 and before any Task 6 production work.  Re-fetch the release tip
  at that boundary; do not rely on `f4005842` remaining current.
- Warpline enumerated the release change identities but reported edge
  enrichment absent, so it is advisory only and is not evidence that the DAG
  change is safe.

### Exact dirty-worktree state

- 114 tracked files modified, 32 untracked files (including this checkpoint),
  and 0 staged files.
- Fingerprints exclude this self-referential checkpoint file:
  - tracked binary patch:
    `94b9280d23231ad4fd91b0fba54a7bb5fc2efb109403a5e467fb37a50c16b644`
  - sorted porcelain path/status inventory:
    `2b7c9822cff40ddc15ce76ae1f01ae6535e18f18c9eede32ce21342b44286cb4`
  - sorted untracked-content inventory:
    `f2f5849093dd28f30a0a5931c57b0c0e7b15f80f6f0041b42fb0228d0f716f10`
- No Task 5 implementation or review agent remains active, and no test, Ruff,
  mypy, or Wardline process remains active against this worktree.
- Nothing was staged, committed, pushed, merged, or rebased.  Task 6 production
  has not started.
- Filigree umbrella `elspeth-b5d7aa5655` remains held by
  `codex-deferred-platform`; its lease was refreshed through
  `2026-07-30T08:20:04.340450+00:00`.  B4-B/DAG work remains unclaimed and the
  operator-owned P0 remains read-only.

### Newly accepted Task 5 authority cohorts

Since the older capture below, the following bounded cohorts completed strict
implementation, owner evidence, spec review, and quality review:

- audit-access authority;
- user-secret authority;
- skill-markdown-history authority;
- account-level user-preference authority, including PostgreSQL wall-clock
  correction after advisory-lock acquisition;
- ordinary Composer proposal creation authority;
- session Composer preferences authority and post-commit response boundary;
- ordinary Composer proposal rejection authority.

The just-finished proposal-rejection slice is the natural pause boundary.  It
requires an exact live `PROPOSAL` context at DML, writes the rejection event and
pending-only CAS proposal update in one transaction, binds `audit_event_id`,
and acquires the route lease before authoritative read.  Validation-failure
auto-rejection uses a short exact lease.  PostgreSQL predecessor/winner,
post-commit cleanup, stale/wrong/released/expired authority, scanner, and
integration evidence are green.  Equal database timestamps now preserve each
proposal's `created -> accepted/rejected` causal order without globally
inventing chronology.  Final spec and quality reviews both returned PASS.

Final rejection-slice hashes:

- `src/elspeth/web/sessions/service.py`:
  `159eb0714fd995088b2010bb872264cf494b708e0fadbfc3f2cea2f69fedbf2c`
- `src/elspeth/web/sessions/protocol.py`:
  `7b3de46fec1d33f4be5a3b8053213ae5c842332968c456d6bdfd4b599e7d96cf`
- `src/elspeth/web/sessions/routes/composer/proposals.py`:
  `63fd21daa1eb1adf17e122790d9870926ee7a610d9ffec641e07a07836eb3d01`
- `tests/unit/web/sessions/test_composer_proposal_authority.py`:
  `1e6af54d95d818c0849a92b20db6e0f7a2b8febc7717f39cd63b6c4b587673e4`
- `tests/unit/web/sessions/test_composer_proposals.py`:
  `67d0a42376f72d22e2cbd7ff022cd9ed6f4caf8d99b67b5609fb1d46d486924b`
- `tests/unit/web/sessions/test_composer_proposal_reject_route_postcommit.py`:
  `23e366fc9ebb8e82dfa5f31695c98581a61e271a472e15d96e05b8a08d743a53`
- `tests/integration/web/composer/test_freeform_proposal_prevalidation.py`:
  `ed483f2887cf9cb065d2d114218b046fdb77b4859edfd963b277167311b2d221`
- `tests/testcontainer/web/test_session_mutation_fencing_postgres.py`:
  `eba750017552ac558a569af8d51b8669e80aebea9f0ef4c0960008c4a2f020e1`
- `tests/unit/architecture/test_session_db_mutation_authority.py`:
  `a65907d88dc005f5a2203adbff6f1c281a271835533c59d6347fe126f95fc558`

Decisive current evidence: equal-time terminal tests 2 passed; rejection
authority 5 passed; route cleanup 3 passed; exact integration 1 passed; real
PostgreSQL predecessor/winner 1 passed; Ruff, production mypy, and diff check
clean.  The broad proposal file is 19 passed / 31 failed because later direct
callers have not yet been migrated to mandatory contexts; those failures remain
Task 5 completion blockers and are not waived by this slice acceptance.
Wardline remains green but inert with zero recognized trust boundaries and must
not be credited as substantive proof.

### Exact resume boundary

1. Verify worktree, branch, HEAD, zero staged files, and the three fingerprints
   above.  Re-check the live remote release tip, but do not edit the release
   checkout.
2. Resume Task 5 with generic ordinary proposal acceptance.  Replace the
   current separate `save_composition_state` and
   `mark_composition_proposal_committed` transactions with one exact
   `PROPOSAL`-fenced atomic state-plus-terminal-proposal authority.  Cover both
   state-changing and blob-store-only acceptance, cancellation, cleanup, stale
   predecessor, and real PostgreSQL winner behavior.
3. Continue the remaining Task 5 pipeline settlement/rejection, guided
   proposal/session dispatch, and residual Session/Interpretation/Runs/
   manifest/connection authority cohorts.  Preserve strict RED/GREEN and
   spec-then-quality review per cohort.
4. Clear the known direct-caller failures, run the mandatory Task 5 unit,
   PostgreSQL, full Sessions, scanner, static, and trust-boundary gates, then
   freeze and commit the Task 5 checkpoint locally.
5. Re-fetch `release/0.7.2` and reconcile this branch onto that exact tip.  Run
   the overlap-focused regression set.  Only then refresh Task 6 assumptions
   and begin Task 6 production.
6. Continue Tasks 6-13, independent-process PostgreSQL B4-B, and corpus/evidence
   reacceptance.  Stop before Task 14.  Do not push or merge the release branch.

Captured: 2026-07-29T15:23:33+10:00

Last refreshed at the final pause boundary: 2026-07-29T16:12:20+10:00

This is the resume authority for the intentional multi-hour pause on the
deferred-platform line of effort.  It records the exact dirty-worktree state;
it is not an acceptance record and must not be used to waive any remaining
gate.

## Custody and branch boundary

- Worktree: `/home/john/elspeth/.worktrees/deferred-platform-completion`
- Branch: `codex/deferred-platform-completion`
- HEAD: `fcd5df055d540b1d3c933aa3cddaa38e4a15ccab`
- Frozen release replay source: `41f99644192f6a4767dead2b554f7a14d023394d`
- Worktree at capture: 95 tracked files modified, 21 untracked files (this
  checkpoint plus 20 implementation/test files), 0 staged files.
- Exact dirty-state fingerprints, excluding this self-referential checkpoint:
  - tracked binary patch:
    `fb0868e3fc749a728fa28704aa7dbd2291cefce66e4499209d61e6efe290d3c8`
  - sorted porcelain path/status inventory:
    `070fb01276a18868fe5425bd2a97d0e88b2f4b5c58756524633757f8a9abcf9f`
  - sorted untracked-content inventory:
    `c43eb1fb91c2522b74e5a4b596087def611bce65ba131026fbed700179c2f622`
- The tracked fingerprint is `git diff --binary -- . | sha256sum`.  The
  porcelain fingerprint is the sorted, newline-normalized
  `git status --porcelain=v1 -z` output with this checkpoint removed.  The
  untracked fingerprint is the SHA-256 of the sorted per-file SHA-256 list,
  again excluding this checkpoint.
- All implementation and diagnostic agents were stopped at safe boundaries.
  No pytest process remains active against this worktree.
- No commit, stage, push, merge, release-branch reconciliation, or Task 6
  production work occurred during the pause transition.
- The release branch is out of scope.  Its only activity is release-readiness
  and CI cleanup and it must not be monitored or reconciled before this branch
  is ready to merge.
- Continue through Task 13 and the independent-process PG B4-B gate, then stop
  before Task 14.
- Keep the four current `codex-deferred-platform` claims open:
  `elspeth-b5d7aa5655` (building), `elspeth-3d1d1fcb6c` (fixing),
  `elspeth-f321e3ff21` (fixing), and `elspeth-245b21351b` (fixing).  Their
  eight-hour leases were refreshed during this work period.  Do not touch the
  operator-owned P0.

All pytest commands must use:

```bash
env -u VIRTUAL_ENV uv run --frozen python -m pytest ...
```

There is one intentional design xfail.  Any other xfail, unexplained skip, or
collection shrink remains a blocker.

## Stable evidence before the current open gates

- Frozen-release replay baseline: 593 passed.
- Full Sessions unit surface before the final architecture assertion was
  enabled: 1613 passed, 12 skipped, 1 deselected.
- All 12 PostgreSQL-dependent skipped cases were subsequently exercised in a
  disposable PostgreSQL 16 container: 12 passed, 125 deselected; the container
  was stopped and removed.
- `tests/testcontainer/web/test_session_operation_fence_postgres.py`: 40/40
  passed after correcting stale test ordering.
- Task 5 PostgreSQL gate: 15/15 passed at the last accepted functional state.
- Blob surface before the latest scanner/composer cleanup: 535 passed, 1
  PostgreSQL-custody skip.  The skip is covered by the dedicated PG gates.
- Progress durability was dual-approved at manifest
  `c6625edca14127cfc246bacf2d740f40bc22dcf8bd85b4208550c55afada4daa`.
  Later narrow Sessions service error translations changed that manifest, but
  the functional progress gates were rerun afterward: coordination 21/21,
  durability 110/110, UoW manifest 6/6, PG progress 3/3, Task 5 PG 15/15.
- Blob DELETE exact gate was dual-approved at
  `fc0ca5f183ffa6347dfd48a76ef640573ce17fc2e5fd4f9275ff3ac3dacdb318`;
  route semantics are missing/wrong-session/idempotent -> 204 and active,
  pending, or fork-protected -> 409.
- Wardline exited successfully but recognized zero declared boundaries.  It is
  inert evidence and must not be cited as a Task 5 proof.

## Task 5: execution lease-loss/effect fencing checkpoint

The implementation now carries the exact `SessionOperationLease` into setup,
the worker, orchestrator lifecycle, and sink-effect adapter boundaries.  Its
guard performs loss-latch -> authoritative exact-context CAS -> loss-latch.
`SessionOperationFenceLost` is ordered ahead of graceful-cancellation and broad
failure compensation; local `finally` cleanup remains live.

Current hashes:

- `tests/unit/web/execution/test_session_operation_lease.py`:
  `8a1c0e2539b170b51a7a3e017aaac7727516c8f8aabbfbc12a46f5ae6f601c90`
- `src/elspeth/web/execution/service.py`:
  `8136b91236363a2cd229b2143f7e1d6747356a6df51d0cd90b75298f26b18614`
- `src/elspeth/web/coordination/lifecycle.py`:
  `2579de726f38be0c9025fd27550f52f63ee90d8d4b087455024ed64a36d4883f`
- `src/elspeth/engine/executors/sink_effects.py`:
  `5b59d6714afcde2ada5b5778e904710bcd294f9949ebdb86204e259820161c22`

Decisive focused matrix: 7 passed in 8.96s.  The real queued-worker takeover
test proves that after A expires and B advances the epoch, resumed A performs
no status update, run-event DML, canonical output write, or broadcast, while
local shutdown/broadcaster cleanup and lease closure still happen exactly once.

The whole execution lease file is currently 53 passed / 6 failed.  Triage
confirmed that all six are stale test contracts, not a production fencing
failure: five parametrizations demand the exact lease and then contradict
themselves by also demanding its context as a separate argument; the AST gate
accepts context derivation from the exact lease but still insists on a separate
context parameter.  Lifecycle plus sink focused tests remain 53/53 green.

The broader execution/orchestrator/sink sweep was interrupted near 88 percent
after 1210 passed / 142 failed.  It is not acceptance evidence.  A later
read-only file-by-file audit completed normally and produced this exact
non-acceptance baseline:

- execution lease: 53 passed / 6 failed;
- routes: 40 passed / 20 failed;
- execution service: 66 passed / 117 failed;
- inline-blob validation: 3 passed / 2 failed;
- validation: 173 passed;
- WebSocket: 9 passed / 3 failed;
- combined total: 344 passed / 148 failed.

The classified families are:

- route fixtures expose MagicMock lease timing/authority values to real lease
  acquisition (20 failed / 40 passed in the route file);
- 115 stale direct execution-service calls across 113 test functions: 59
  `execute`, 43 `_run_pipeline`, 5 `_finalize_output_blobs`, 3
  `_on_pipeline_done`, 2 `_broadcast_progress_event`, 2 `validate`, and 1
  `validate_state`;
- validation callers omit the now-mandatory exact BLOB_READ context;
- the baseline WebSocket replay path exposed one real production boundary
  defect: frozen recursive `mappingproxy` event data was passed directly into
  Pydantic.  The bounded TDD slice below applies `deep_thaw(record.data)` at
  the schema boundary while retaining the immutable record contract;
- one cancel-pending test is obsolete because production intentionally fails
  closed without local EXECUTE authority, and one shutdown test needs a live
  exact EXECUTE lease;
- no deadlock is proven.  Every focused file completed.  The concrete stall
  risks are unfinished workers, a mocked event loop that cannot settle
  lease-completion futures, and route mocks that accept a transferred lease
  without taking lifecycle ownership.

No production or test edit was made during the triage itself.  A subsequent
bounded TDD slice fixed only the WebSocket boundary:

- `src/elspeth/web/execution/routes.py` is now
  `e2eed3bf434c485f1c711c43c1406c61963ec1b4f2148c0bb0d7bfff7fb2b4b7`;
- `tests/unit/web/execution/test_websocket.py` is now
  `1d039f25a8f22895f5c4ee873eb8e6049c1d5eafa9c5341c3e1af6712b35e137`;
- the terminal replay regression was strengthened with nested completed-event
  accounting, observed RED when Pydantic received recursive `mappingproxy`
  values, then passed after `_run_event_from_record` used
  `deep_thaw(record.data)`;
- the three previously failing replay/reconnect nodes now pass together, and
  `git diff --check` is clean;
- the whole WebSocket file, Ruff check/format, coordinator rerun, self-review,
  and independent spec/quality reviews remain outstanding.  This slice is not
  accepted evidence yet.

Resume the execution cohort by finishing those WebSocket checks and reviews,
then correct the six contradictory lease tests and build one reusable real
SQLite lease/session fixture.  It must acquire the exact EXECUTE or BLOB_READ
lease, expose only `lease.context`, await the owned service/worker drain before
`lease.close()`, and make drain-plus-close cancellation-safe.  Synchronous
worker calls should run through `asyncio.to_thread`; successful route fakes
must explicitly own and drain transferred leases.  Run each affected file
separately before another broad sweep.  Do not make lease or context optional.

## Task 5: Sessions mutation architecture/blob checkpoint

Current scanner hash:

- `tests/unit/architecture/test_session_db_mutation_authority.py`:
  `73a5e66675d2368b78c0124d873b736a3672ebd5abdfba504fc117b1551b3ab0`
- `tests/unit/web/blobs/test_service.py`:
  `f89a219900f528ab71bbef2079043c4c3c7a7954501e1b3574a5fd088d22d4db`

The contradictory older scanner fixtures have been corrected while preserving
all-acquisition, escape, fail-closed, multiplicity, and read-provenance
coverage.  Synthetic/adversarial coverage was 53 passed with the two live
production assertions deselected before the final classification edit.  The
final scanner agent added exact tri-state `sessions` / `non_sessions` /
`unknown` semantic provenance using table modules, engine factories, branded
types, annotations, assignments, and statement provenance.  Unknown raw SQL
and engine provenance were intended to remain fail-closed; no path-wide
exclusion or production edit was added.  Its observed final run was 55 passed /
2 live-gate failures in 13.31s, with Ruff check/format and whitespace checks
clean.  The coordinator independently reran the exact scanner file and observed
55 passed / 2 intended live-gate failures in 11.44s with the same live counts.
That run proves stability, not acceptance: independent exact-hash review and a
separate false-exclusion audit both rejected the current scanner specimen.

The exact-hash spec review found these concrete fail-open classes:

- a conditional/wrapper statement with one proven Landscape branch and one
  unknown branch is dropped instead of remaining in the Sessions domain;
- rebinding or deleting an imported member does not invalidate qualified
  provenance, including `schema.runs_table` and `sqlite3.connect`;
- attribute-domain inference crosses unrelated nested classes and methods
  because it walks an entire class and compares unparsed receiver text rather
  than using scope-aware reaching definitions;
- connection escapes through augmented assignment, `with ... as` attribute or
  subscript targets, and stored callable acquisitions are missed or blessed by
  a contained authority;
- bare mutating or unknown PRAGMAs such as `PRAGMA optimize` are treated as
  reads instead of using a closed read-only allowlist;
- the canonical Sessions factory exception accepts the prefix
  `create_session_engine_external` instead of only the exact factory and its
  lexical descendants.

The separate false-exclusion audit additionally proved that generic
`sqlalchemy.create_engine` and aliased factory calls are incorrectly branded
non-Sessions even when their URL is the Sessions database, which can hide raw
Sessions SQL after the emitted connection identity is reviewed.  It also
reproduced receiver-scope pollution across nested classes and same-spelled
method parameters.  Mixed or unknown statement evidence must not collapse to
"no evidence" and restore a previously inferred origin.

Do not obtain a code-quality approval for hash
`73a5e66675d2368b78c0124d873b736a3672ebd5abdfba504fc117b1551b3ab0`.
Resume with strict RED specimens for every bypass above, correct the scanner
without padding manifests, rerun the full file and owner evidence, then obtain
fresh exact-hash spec and code-quality reviews.

The final live scan found 340 sites and 337 live unexpected/unreviewed entries,
including 124 raw-connection violations, 74 unresolved execute sites, 96
protected-table writers without a named authority, and 3 wrong-authority
writers.  The 96 writers partition as:

- Sessions service: 77
- Composer progress: 10
- Composer blob tools: 2
- Coordination repository: 2
- Preferences: 2
- Composer service, secrets, and shareable reviews: 1 each

Three fork writes carry the wrong narrow authority facet:

- `_ForkCreationTransaction.insert_child_state` and
  `_ForkCreationTransaction.append_child_messages` use
  `SessionOperationAuthority` but require `SessionMutationAuthority`;
- `_ForkCreationTransaction.bind_guided_fork` uses
  `SessionOperationAuthority` but requires
  `GuidedSessionMutationAuthority`.

The independent read-only authority audit decomposed those 96 sites into 15
already protected by exact mandatory authority in an enclosing transaction,
12 behind existing narrow facets whose exact methods are absent from the
scanner registry, 63 genuine production-authority blockers, and 6 global-table
writers requiring dedicated named authorities.  The three fork writes remain
genuine wrong-authority blockers.  The smallest correction is one private
atomic fork transaction composed from a child-session facet retaining the
fork child context and a parent-guided facet retaining the exact guided fence;
do not pass a raw connection or broaden `SessionOperationAuthority`.

There are also 12 stale reviewed-writer identities, six stale read-connection
identities, and eleven stale non-Sessions identities.  Eleven unresolved
Sessions/shared-wrapper sites intentionally remain fail-closed; 63 apparently
external or non-database unresolved executions and 36 newly proven
non-Sessions connections still require individual exact manifest review.  Do
not bulk-copy live output or refresh manifests around production failures.

Other current evidence:

- Blob service: 198 passed, 1 known PG-custody skip.  The two stale tests no
  longer monkeypatch the deleted generic transaction wrapper and now exercise
  the exact fenced UoW; the wrapper was not restored.
- Composer blob-inline tools: 24/24 passed.
- Scanner/blob Ruff check, Ruff format check, and targeted diff check are clean.
- The live composer create path now requires the exact COMPOSE authority pair;
  the legacy blob tail is intended to remain exact-fork-only,
  idempotent/write-guarded.

## Task 6 prep gates (production not started)

Task 6 remains blocked on Task 5 completion and explicit gate release.

Architecture gate:

- File: `tests/unit/architecture/test_web_landscape_mutation_fencing.py`
- Frozen hash:
  `ce9a0b21aa27724329b3c69b87716ed003033938b77113a9d253864cb50a3fc2`
- Self tests: 26 passed, 5 deselected in 12.14s.
- Full owned file: 26 passed and exactly 5 intended production REDs in 54.76s.
- Bounded production probe: intended 81-violation RED in 40.75s, no scanner
  exception.
- Ruff format/check, `py_compile`, and whitespace checks are clean.
- Exact-hash hostile spec and quality reviews are still pending.
- Frozen inventories:
  - DML: 126 / `a18761e893e6837d20cd2437fbcc371fac1d1c461438ae2b432e9ba27d862ee7`
  - callers: 241 / `c7fe84414c114833d87406c1b76122f4664901c878cc9a87bc8c9788beb77c7b`
  - coordination: 15 / `e2c82952e49763ee53e8772dcaedd9afbeecd1448c7c93a99659e25037e8d511`
  - internal: 98 / `f015f32f3a7e4ababa7bdc3af16309a402c4ca404955d189079033202fa65f3b`
  - subordinate: 70 / `0b49ee25a1763bd528a195560b2348a14ac5f937cdfe0360f3305711bc2b54c2`

Clock gate:

- File: `tests/unit/core/landscape/test_database_clock_authority.py`
- Current interrupted hash:
  `f29a6561f7b16a761fee2b1f0e8f27cdf3bf9dd26d299a8234bccd713e83d49b`
- This specimen is explicitly unverified and unapproved.
- The last fully exercised earlier hash
  `a9aeaf6df832622e504500dcfb42213f3203531d6f9fad01d710fb9e782dd756`
  collected 245 tests (236 self + 9 production), with self green and exactly
  four intended production REDs, but hostile review rejected it.
- Required fixes/reproofs include late verifier rebinding; conditional, dead,
  repeated, unreachable, and nested transaction/yield control flow; shared
  mapping and authority-column aliases; dynamic SQLAlchemy execute;
  namespace/HOF mutation variants; and standalone raw UPSERT applicability.

Both Task 6 gate files are untracked and uncommitted.  Resume the clock gate
from RED/self-test work; do not begin Task 6 production merely because the
architecture gate has a coherent hash.

## Exact resume order

1. Confirm this worktree, branch, HEAD, zero staged files, and the hashes in
   this note.  Do not inspect or wait on the release branch.
2. Keep scanner hash
   `73a5e66675d2368b78c0124d873b736a3672ebd5abdfba504fc117b1551b3ab0`
   rejected.  Add strict RED specimens for all recorded fail-open cases, fix
   the domain/escape/provenance logic without manifest padding, rerun the exact
   file as owner, then obtain fresh exact-hash spec and code-quality approval.
3. Route the three fork writes through their exact typed facets; add only the
   27 exact scanner registrations already proven by existing enclosing
   authority/facets; then migrate the 63 genuine protected writers and six
   global writers by authority cohort.  Individually classify the remaining
   raw-connection/unresolved identities.  Do not refresh manifests until the
   production paths are genuinely typed.
4. Rerun the whole WebSocket file plus Ruff check/format and independently
   review the two-file deep-thaw slice.  Then correct the six stale
   execution-lease expectations, migrate route/service/validation tests to
   real exact authority and lease fixtures, and isolate any late-suite stall
   by running each affected file separately.
5. Rerun the full execution lease, route, service, validation, WebSocket,
   sink, orchestrator, and lifecycle suites; fix regressions, then obtain
   independent exact-hash spec and quality approval.
6. Run the mandatory Task 5 unit and PostgreSQL commands from the main plan,
   then the full `tests/unit/web/sessions` surface.  Re-exercise the 12
   PostgreSQL-backed skip cases if touched production surfaces warrant it.
7. Freeze and dual-review the final Task 5 architecture manifest.  Only after
   all Task 5 gates are green may Task 6 production begin.
8. Reopen Task 6 prep by finishing the rejected clock-gate specimen and
   exact-hash reviewing both gates.  Keep the five architecture production
   REDs and exact clock production REDs as the Task 6 TDD boundary.
9. Continue Tasks 6-13, then run independent-process PG B4-B and corpus/evidence
   reacceptance.  Stop before Task 14.

Mandatory Task 5 gate:

```bash
env -u VIRTUAL_ENV uv run --frozen python -m pytest -q -n 0 \
  tests/unit/web/sessions/test_operation_fence_wiring.py \
  tests/unit/architecture/test_session_db_mutation_authority.py \
  tests/unit/web/sessions/test_static_direct_writers.py \
  tests/unit/web/blobs/test_service.py \
  tests/unit/web/composer/test_blob_inline_tools.py \
  tests/unit/contracts/test_web_blob_fencing.py
env -u VIRTUAL_ENV CI=1 uv run --frozen python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/web/test_session_mutation_fencing_postgres.py
env -u VIRTUAL_ENV uv run --frozen python -m pytest -q -n 0 \
  tests/unit/web/sessions
```
