# D1 shared blob admission implementation review

Spec-compliance verdict: **APPROVE** for the frozen D1 blob-admission tranche.

Code-quality verdict: **APPROVE**. No actionable introduced defect or blocking coverage gap found in the reviewed patch. This is scoped implementation approval, not whole-branch release clearance.

## Reviewed candidate and method

Reviewed the complete 13-path staged diff against base/HEAD `ff755b12b5c01bcdc3e060a8f2d93b0219518de6` in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. The reviewed artifact is `d1-blob-admission-review.diff`, SHA-256 `6177a62c3ddf70ea749d145d10ba574907fc9340c0a8c1e143b218ca78ec2651`.

Read repository AGENTS/CONTRIBUTING guidance, the full brief including its corrected debt-consistency requirement, the extraction map, design review, implementation report, independent authority-inventory analysis and final-adapter recheck, mutation-control script, source-derived manifest-refresh script, relevant production callers/driver phases, all three new test modules, integration/acceptance changes, and completed test evidence. No code edits, test reruns, recovery, commits, or agents were performed. This review file is the sole write.

Read-only verification during review produced:

```text
HEAD: ff755b12b5c01bcdc3e060a8f2d93b0219518de6
paths: 13
staged=expected: True
worktree=index: True
reviewdiff=index: True
freeze manifests equal: True
parent PG manifests equal: True
sha256sum -c d1-blob-final-frozen-after.sha256: all 13 paths OK
```

## Spec assessment

The adapter at `src/elspeth/web/composer/tools/blobs.py:1734` is an independent prerequisite path, not destructive dispatch plus rollback. It brackets safe content inspection with fenced database phases while holding BLOB_CUSTODY, creates no intent/reservation/receipt, and never calls recovery. `None` is an internal stageable result. Only a real refusal is routed through `_unapplied_admission` at `tool_batch.py:2094`; the BLOB_STORE exclusion prevents graph-hash no-op classification, and the proposal cap remains before publication at `tool_batch.py:2218`. Same-content updates remain stageable external effects, and create_blob remains immediate.

The corrected debt requirement is implemented at `coordination/repository.py:2124`. It observes deletion before replacement, recognizes atomic all-NULL evidence separately, validates both ledgers before returning an observation, rejects simultaneous incompatible ledgers, and compares complete phase-specific replacement metadata using `contracts/blobs.py:879`. Qualified deletion retains its complete live snapshot/purge-absence checks; atomic deletion retains its no-live-metadata rule. These checks bind actual custody without requiring the successor fence to equal the historical producer lease.

Debt filesystem validation uses the original exact path authorities and supported content alternatives: `blobs/replacement.py:149` and `blobs/service.py:2938`. Old backup evidence can support interrupted uncommitted replacement, purge artifacts can already be gone, and qualified absent-file debt cannot acquire unrecorded bytes. The adapter performs a fresh fenced observation and compares the entire owned observation before returning recovery-needed. Corrupt metadata, incompatible ledgers, incorrect invocation-qualified paths and corrupt content remain faults rather than ordinary recovery feedback.

Fresh replacement admission shares metadata/immutable-field checks at `repository.py:2161`, retention/quota at `repository.py:2191`, and retains the real commit recheck. Fresh deletion shares snapshot, metadata and observed file-evidence checks at `repository.py:3326`; `contracts/blobs.py:791` retains the exact bool/size/hash domain for admission and durable preparation. Canonical replacement content requires correct existing bytes (`replacement.py:137`). Deletion's safe observer (`service.py:2913`) preserves `(False, None, None)` and nullable metadata hashes. Proposal-only readiness does not remove direct pending/error deletion.

Current-draft references and relational pending proposal/run/fork retention remain complementary. COMPOSE admission does not invent an accepting-proposal exclusion; real PROPOSAL preparation and acceptance retain their exact existing exclusion and commit checks. Quota admission uses the existing real authority/recorder, includes output-reservation excess, checks session projection for all deltas, preserves positive-growth identity/container admission and the owner-first nonpositive-growth bypass, and adds no delete growth gate. Accounting-unavailable faults remain distinct from ordinary quota refusal.

## Test and oracle assessment

The no-effect oracle (`tests/unit/web/blobs/test_mutation_admission.py:86` and `:110`) snapshots full resource rows, provenance-bearing blob records, both ledgers, reservations, inline resolutions, receipts, complete fence values and path types/content. Its DML allowance is limited to the existing SQLite fence self-assignment prefix, not arbitrary fence UPDATEs. Destructive handlers, recovery, prepare/commit/retire/abort, reservation/publication and rename/unlink paths have tripwires. Controls at `:701` prove destructive dispatch, resource SQL and changed-fence SQL trip the oracle; real handlers and acceptance prove actual effects change the snapshots.

The interrupted-debt cases use real stopped producers for all replacement and qualified deletion phases plus atomic fork cleanup (`:262`, `:325`). They test valid successor recovery separately, metadata corruption/missing rows, exact foreign/wrong-invocation paths without foreign reads, bytes corruption, simultaneous ledgers (`:731`), post-observation debt/fence changes (`:775`), interrupted rename/backup alternatives (`:817`), and absent-file debt (`:889`). Authority, file readiness, final metadata/directory/fence checks, direct pending/error deletion, and relational retention have actual production-path controls. The separate quota module covers independently binding bounds, real recorder metadata, each reservation coverage case, other sessions/identities and the nonpositive-growth nuances.

The real loop tests prove once-only refusal feedback/outcome, no refused proposal, original baseline for the repair, and refusal at the proposal cap. Real stage/accept tests cover unchanged-content updates, unchanged composition state, and one durable receipt on replay. Existing receipt/acknowledgement-loss and driver recovery suites supplement these changes; preview is not substituted for acceptance authority.

Inspected the actual seven PostgreSQL cases in `tests/testcontainer/web/test_blob_mutation_admission_postgres.py`, not merely their names or collection:

* `:71` (two cases): a real fenced prepare holds the database transaction; an independent admission connection is observed blocked via `pg_stat_activity`, then sees the committed replacement/deletion debt without allocating a second ledger. The same test first proves successful admission leaves no durable effect.
* `:160` (two cases): paused filesystem reads allow real SESSIONS renewal to finish; final metadata change or expiry causes admission to fault after resumption.
* `:205`: another identity's real quota evaluation holds the shared container policy and commits an output reservation while admission is observably blocked. Admission then records the truthful measured container refusal and creates no reservation/intent.
* `:286`: a real delete writer is observably blocked on BLOB_CUSTODY during paused admission, then completes after admission returns successfully.
* `:336`: a fence-row waiter observes committed expiry and cannot reach content reads.

These use bounded Events/timeouts and independent engines where connection contention is required. The completed parent log records all seven passing; both terminal exit files are zero, and the parent before/after source manifests match. No PostgreSQL execution was repeated by this reviewer.

## Recorded validation and inventory limits

Read the completed logs and explicit terminal exit files. The corrected candidate records 271 behavioral passes, 295 response/code-envelope passes, 145 response-consumer passes, and 298 authority-module passes with one existing generic xfail. Scoped Ruff/format, seven-production-file mypy and contracts checks exit zero. All nine isolated omission/recovery mutants exit 1 at the intended assertion. The earlier 421-pass/1-skip compatibility run predates the adapter's None correction and covers unchanged storage/authority production; it is not represented as a final 13-file frozen-tree run.

The authority manifest change is confined to 101 already-reviewed `WriterIdentity.line` fields. The independent canonical-producer comparison and applied refresh script preserve the complete non-line identities, duplicate multiplicities, fingerprints, ordinals, authority/escape bindings, policies, scanner semantics and normalized test AST. The final adapter recheck adds no writer identity drift. This is source-location refresh, not admission of an unreviewed writer or signature manipulation.

The generic `test_all_production_sessions_writers_are_reviewed_typed_authorities` remains an expected failure, not a green authority inventory. Its recorded debt remains 106 unexpected/unreviewed identities, 30 containment gaps, 74 unresolved executions, two writers without named authority and two stale reviewed read bindings; the overlapping categories must not be added together. There are zero stale reviewed writers, wrong-authority writers or invalid reviewed read policies. The pre-existing escaping fork-pair helper remains unreviewed.

The parent still owns combined full-backend regression, the final complete serial PostgreSQL selection, broad static checks and all-rules corpus comparison after the remaining staging-race/canonical work. This approval does not waive or claim those gates, nor operator signed trust-tier clearance.
