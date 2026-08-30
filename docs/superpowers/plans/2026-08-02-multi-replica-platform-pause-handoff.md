# Deferred platform / DAG reacceptance pause handover

**Captured:** 2026-08-02T23:09:02+10:00

**Coordinator:** `codex/deferred-platform-completion`

**Pause boundary:** committed run-diagnostics lock-order regression; release replay
repairs are still being prepared elsewhere; interpretation-resolution work is
parked as unverified WIP.

## Purpose and scope

Resume the deferred-platform integration objective from the durable state in
this document. The bounded objective remains:

1. integrate correct `release/0.7.2` changes into the platform branch;
2. finish deferred-platform Tasks 6 through 13;
3. implement the genuine distributed B4-B proof;
4. regenerate and reaccept the DAG corpus on the combined tree; and
5. stop before Task 14.

Do not push, merge the platform branch into `release/0.7.2`, perform operator
signing, or begin provider packaging. The DAG parent remains open. The one
intentional xfail accepted by the developer is exempt from the original
zero-xfail wording; any other xfail, skip, warning delta, or collection shrink
still requires investigation.

The quarantine semantic remains: a quarantined row is a consumed-row boundary
and therefore triggers aggregation, coalescing, and row-union timer sweeps. It
does not become aggregation/coalescing membership or downstream row input.

## Exact workspace snapshot

| Role | Worktree | Branch / base | Captured state |
|---|---|---|---|
| Coordinator | `/home/john/elspeth/.worktrees/deferred-platform-completion` | `codex/deferred-platform-completion` | clean at `929e5ef1fce9b2bc6ee238de67bdae1aeb3e159a` |
| Release | `/home/john/elspeth` | `release/0.7.2` | tracked tip and origin both `0d7a8fec71f40a7d3b06d97b41b1affa37f92a4e`; six unstaged repair files are in flight |
| Interpretation WIP | `/home/john/elspeth/.claude/worktrees/deferred-interpretation-resolution` | `codex/deferred-interpretation-resolution`, base `64b7d144e18fc338677cb69f70d99e5d44dafef8` | sixteen dirty paths; no WIP commit; not accepted |

The coordinator/release merge base at capture is
`4f1e76ddedf2b3fc18adeaaf92e8173617d8bc78`. Therefore the coordinator is not
yet aligned with release commits `512c84444`, `d15ff8efd`, or `0d7a8fec7`.

The coordinator's relevant first-parent history is:

```text
929e5ef1f test: pin run-diagnostics timestamp lock ordering
a4a6e0d2e merge: advance multi-replica platform through release 4f1e76dde
39267be3d merge: advance multi-replica platform through release 41680eb60
4f1e76dde feat: add codex-cli transport and model configuration to wardline judge
41680eb60 fix: close three proposal-terminalization defects in pipeline settlement
```

Full merge commit IDs:

- `a4a6e0d2e1afa1ca27d4518f2fb9eabbf3e93d6c`
- `39267be3d716cdfeb4986a11b450069c3d543f61`

Nothing from this coordinator branch has been pushed.

## Release-side state and integration decisions

The captured release first-parent sequence is:

```text
0d7a8fec7 test: prove the ordinary guided retry replays without double-surfacing
d15ff8efd fix: stamp the run-diagnostics audit row under the session lock
512c84444 fix: surface interpretation reviews on the guided RESPOND replay arm
4f1e76dde feat: add codex-cli transport and model config to wardline judge
41680eb60 fix: close three proposal-terminalization defects
```

### Accepted

- Release through `4f1e76dde` is integrated into the coordinator.
- `d15ff8efd` was independently approved. Its production lock/timestamp
  semantics were already present on the platform branch through earlier
  platform work, so replaying its production hunk would be a no-op. Its useful
  regression was strengthened and committed as `929e5ef1f`.

### Rejected or insufficient as captured

- `512c84444` was trial-merged and independently audited, then the merge was
  cleanly aborted. It is not in the coordinator. Its replay approach has three
  confirmed production defects listed below.
- `0d7a8fec7` is test-only. It proves that one pending event remains, but does
  not prove exact event identity, all-status history, provenance, response
  equality, no authority acquisition, or no durable mutation. It does not fix
  any of the three production defects.

### Three positively confirmed release defects at `0d7a8fec7`

1. Guided replay discards the exact `GuidedCompositionStateResult.proposal_id`
   and reverse-lookups through `committed_state_id`, which is legitimately
   non-unique for blob-only proposals. A later legitimate proposal can make
   replay ambiguous or misattribute provenance. Use `result.proposal_id` and
   the existing authoritative proposal reader; delete
   `get_proposal_by_committed_state`.
2. Replay unconditionally resurfaces historical state. After resolution or
   state advance, the placeholder has been consumed and replay raises
   `InterpretationPlaceholderConsumedError` instead of returning the immutable
   recorded response. Fulfilment must use exact all-status/per-site evidence
   and preserve partial-crash repair without replaying resolved work.
3. `_replay_completed` invokes the mutating replay callback before checking the
   stored response hash. A corrupt replay can mutate interpretation state and
   only then fail. Verify immutable response integrity before any recovery
   mutation or fresh authority acquisition.

These are recorded on release task `elspeth-64c319bf4d` in comments 2137, 2138,
2139, and baseline note 2141.

### Release repairs observed in flight at pause

The release worktree remained at tracked/origin tip `0d7a8fec7` but had an
unstaged, `git diff --check`-clean repair diff in these six files:

```text
src/elspeth/web/composer/service.py
src/elspeth/web/sessions/protocol.py
src/elspeth/web/sessions/routes/composer/guided.py
src/elspeth/web/sessions/routes/guided_operations.py
src/elspeth/web/sessions/service.py
tests/integration/web/composer/guided/test_respond.py
```

Captured size: 526 insertions and 75 deletions. Captured binary-diff SHA-256:
`e4946c2ff45c48421d5c00a68250a9e26419c4a39909f5fc7f9722139ff52093`.

This is only an observation of release-side WIP. It was not reviewed, tested,
committed, or accepted by the coordinator. On resume, use the resulting release
commit(s), not this working-tree hash, and prove in code/tests that all three
defects are actually fixed.

## Completed coordinator regression at `929e5ef1f`

`tests/unit/web/sessions/test_service.py` now pins that the run-diagnostics
timestamp is acquired under the session lock and is shared by the audit row,
session timestamp, and returned record.

Evidence obtained before commit:

- The worktree environment was refreshed with
  `env -u VIRTUAL_ENV uv sync --frozen --all-extras`.
- `sys.prefix`, Python, pytest, Ruff, and mypy were proven to resolve lexically
  from the coordinator worktree's `.venv`.
- `importlib.metadata.version("rfc8785")` was `0.1.4`.
- Mutation proof: moving `datetime.now(UTC)` before the lock made the new test
  fail with expected `12:00:30` versus actual `12:00:00`; restoring the
  production placement made it pass.
- Exact regression: 1 passed.
- `TestRunDiagnosticsAuditMessage`: 8 passed.
- Ruff check and format check passed.
- Pre-commit passed with only the deliberate `SKIP=check-contracts`; no
  signatures were edited or requested.

No full suite was claimed for this single-test coordinator commit.

## Parked interpretation-resolution worktree

The interpretation slice remains based on `64b7d144e` and is deliberately
uncommitted. Its tracked diff contains 1,827 insertions and 602 deletions across
fourteen tracked files. It also has two untracked files, making sixteen dirty
paths in total:

```text
src/elspeth/web/coordination/repository.py
src/elspeth/web/sessions/pending_interpretation.py
src/elspeth/web/sessions/protocol.py
src/elspeth/web/sessions/routes/interpretation.py
src/elspeth/web/sessions/service.py
tests/integration/web/composer/test_guided_interpretation_run_backstop.py
tests/integration/web/composer/test_interpretation_runtime_handoff.py
tests/unit/architecture/test_session_db_mutation_authority.py
tests/unit/web/composer/test_compose_loop_persistence.py
tests/unit/web/composer/test_request_interpretation_review_tool.py
tests/unit/web/sessions/test_interpretation_events_routes.py
tests/unit/web/sessions/test_interpretation_events_service.py
tests/unit/web/sessions/test_operation_fence_wiring.py
tests/unit/web/sessions/test_static_direct_writers.py
src/elspeth/web/sessions/interpretation_validation.py                 (untracked)
tests/unit/web/sessions/test_interpretation_validation_inputs.py      (untracked)
```

Captured tracked binary-diff SHA-256:
`baa76f8bbc31059e18d0aee8cf551151ecbffbd4a5417481e10cef1927676ea4`.
Captured untracked-file SHA-256 values:

- `interpretation_validation.py`:
  `6a8ef93a9a356694e4c11e914df0497e28052ddec799f55c2ce2541f1de7cb81`
- `test_interpretation_validation_inputs.py`:
  `8e06c695f0d133066d7dd7ff228a246e8ea1d7b5b4eb90eda7e207e61292dfb2`

`git diff --check` is clean. No test result after the latest DTO/cleanup edits
is accepted or claimed.

### Why the earlier quality approval is obsolete

A fresh quality review found four blocking issues in the earlier object-graph
scanner design:

- P1: module/class/weakref/iterator/deque/function carriers can hide live
  database/runtime authority from the scanner.
- P1: wrong-type inputs are traversed before exact nominal rejection and can
  execute attacker-controlled behavior.
- P2: the 500,000 unique-object counter is not a work bound; eager or infinite
  iterables can bypass or hang it.
- P2: one-time scanning has a mutable TOCTOU gap because the same live catalog
  and registry objects are retained afterward.

Do not restore or extend the general Python object-graph scanner.

### Chosen correction and current WIP

The accepted design direction is a closed, frozen, per-principal DTO built
before `_run_sync`. It must contain an immutable
`PluginAvailabilitySnapshot`, detached plugin authority/public schema/aliases,
and detached lowering recipes. It must not retain a live
`CatalogServiceImpl`, `OperatorProfileRegistry`, Pydantic object, callable,
iterator, module, database handle, or other extensible object graph.

The interrupted implementer began this correction:

- added `interpretation_validation.py` with frozen detached inputs, catalog and
  profile adapters, and a detached validation entry point;
- added focused parity/mutation/hidden-carrier tests in
  `test_interpretation_validation_inputs.py`;
- moved more interpretation resolution work behind repository authority; and
- changed the route so an ordinary exception during post-commit lease cleanup
  is structured-logged after a durable response has been assembled, preserving
  the successful 200 outcome.

Treat all of that as partial, unverified implementation. On resume, inspect the
entire diff before editing, finish RED/GREEN proof for DTO closure and cleanup
primacy, and obtain a new independent code-quality approval. The prior approval
predates the blocking review and is not acceptance.

Cleanup semantics to preserve:

- before durable commit, the original error remains primary and cleanup may
  only attach context;
- after the durable event/state receipt exists, fully drain lease close;
- ordinary cleanup failure is structured-logged and cannot replace the 200
  resolution outcome; and
- cancellation/process-control `BaseException` after the drain still
  propagates.

## Task 5 architecture checkpoint

The last scanner report taken on the interpretation WIP tree was:

- 458 sites scanned;
- 342 unexpected aggregate sites;
- zero stale reviewed-writer rows;
- 93 connection violations;
- 185 unresolved execute sites;
- 29 genuinely unclassified protected-table writers;
- zero wrong-authority writers; and
- six stale escaped-read identities.

The 342 figure is a work-in-progress aggregate, not 342 adjudicated defects.
Rerun the authoritative test after integrating the interpretation slice; do not
copy these counts forward as final evidence.

Recommended next inventory/domain cohort:

1. add RED proofs for exact LocalAuth sqlite wrapper provenance;
2. prove `LandscapeConnectionProvider`, `Tier1Engine`, `begin_write`, and
   `write_connection` propagation;
3. distinguish wrapper `SELECT` from DML poisoning;
4. refresh the exact read manifest; and
5. classify each external domain explicitly, without path-wide padding.

Recommended first production cohort after that:

- consolidate message/title/state mutation authority across
  `service.py`, `protocol.py`, `routes/sessions.py`, and
  `composer/tutorial_service.py`;
- remove optional-context bypasses from `update_session_title` and
  `add_message`; and
- route those writes through typed units of work with focused regressions.

The release experiment's raw proposal-by-state getter and unconditional replay
lease were rejected and are absent from the coordinator; do not classify or
admit them into the Task 5 inventory.

## Filigree checkpoint

Coordinator claims were heartbeated for 48 hours at approximately
2026-08-02 12:48 UTC:

- `elspeth-4d6c0dd0f5` — deferred-platform review remediation;
- `elspeth-f321e3ff21` — compatibility implementation identity; and
- `elspeth-245b21351b` — resume cleanup after leadership acquisition.

Other relevant status:

- platform feature `elspeth-b5d7aa5655` remains approved/unassigned;
- B4-B `elspeth-9a52eb80f9` is not claimed and must not be claimed until Tasks
  6 through 13 make it startable;
- DAG parent `elspeth-ef29ef6ba4` remains open;
- release task `elspeth-64c319bf4d` carries the confirmed replay defects; and
- the operator signing task remains untouched.

No issue was closed at this pause.

## Tool and process state

- All coordinator subagents are completed or interrupted; none is running.
- The platform-worktree `loomweave analyze . --json` refresh reached Python
  plugin processing but stopped emitting progress and was interrupted with
  Ctrl-C at committed `929e5ef1f` (exit 130). No Loomweave analysis process
  remains. The index must therefore be treated as stale/incomplete and rerun at
  committed HEAD before archaeology or final acceptance.
- Warpline evidence remains advisory and was not used to narrow any full gate.
- The release checkout had unrelated release-side work in progress and was not
  modified by the coordinator during shutdown.

## Exact resume sequence

1. Read the original objective and all three controlling documents completely:
   the Wave B handoff, the preplatform sprint, and the live deferred-platform
   plan. Reread the live platform plan after any integration that changes it.
2. Confirm the coordinator is clean at the handover commit descended from
   `929e5ef1f`. Confirm the interpretation WIP status and hashes above before
   touching it; investigate any drift rather than overwriting it.
3. Fetch `origin/release/0.7.2` and capture an immutable release source anchor.
   The shared release checkout was dirty at pause, so do not update over WIP.
   Wait for a clean committed release state, then inspect commits and code.
4. Verify that the three replay defects are fixed by the latest release code
   and narrow regressions. Required proof includes exact proposal identity,
   blob-only shared-state behavior, all-status immutable replay after
   resolution/state advance, partial surfacing repair, no-authority/no-mutation
   fast replay, provenance mismatch rejection, and response-hash rejection
   before mutation.
5. Integrate acceptable release commits semantically. Preserve platform
   operation-context authority. Do not add unconditional COMPOSE leases to
   satisfied terminal replay, do not restore proposal-by-state lookup, and do
   not let replay recovery run before response-integrity verification.
6. In each worktree used for tests, run
   `env -u VIRTUAL_ENV uv sync --frozen --all-extras`, prove the local `.venv`
   prefix and Python/pytest/Ruff/mypy paths, and prove `rfc8785 == 0.1.4`.
7. Resume the interpretation worktree. Finish the closed DTO and cleanup-error
   primacy with mutation-proven RED/GREEN tests. Run focused and broad authority
   suites, Ruff, format, mypy, and `git diff --check`. Obtain a fresh
   independent quality review and fix all valid findings.
8. Integrate the approved interpretation slice into the coordinator
   semantically. The previously anticipated overlap is chiefly the static
   writer inventory; re-audit the full merged diff rather than accepting a
   mechanical side.
9. Rerun the authoritative Task 5 gates and record fresh counts. Complete the
   inventory/domain cohort, then the message/title/state authority cohort.
10. Continue the live plan through Tasks 6 through 13 in order. Only then claim
    and implement B4-B on independent registered worker processes sharing
    PostgreSQL.
11. Regenerate and reaccept every DAG recovery/runtime/audit evidence item on
    the final combined tree, run all required reviews and gates, refresh
    Loomweave at committed HEAD, and stop before Task 14.

## Pause guarantees

- Coordinator branch clean after this handover commit.
- Release-side dirty files not touched.
- Interpretation WIP preserved exactly and explicitly not accepted.
- No active coordinator subagents or Loomweave process.
- Nothing pushed.
- Nothing merged into `release/0.7.2`.
- Task 14 not started.
- B4-B not claimed.
- Operator signing state and credentials untouched.
