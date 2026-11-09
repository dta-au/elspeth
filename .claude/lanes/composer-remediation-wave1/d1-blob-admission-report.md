# D1 shared non-writing blob admission

Status: implemented, frozen and staged for parent review. Corrected behavioral, response, scoped static and focused PostgreSQL checks pass. The full Sessions authority module exits 0 with its existing generic expected-failure reported below. No commit.

Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`.
Base/HEAD: `ff755b12b5c01bcdc3e060a8f2d93b0219518de6`.
Scope: seven production paths and six test paths, enumerated in `d1-blob-files.txt`. The preceding T1 admission/cap implementation is reused, not duplicated. Deliberate untracked frontend `node_modules` is preserved and excluded.

## Implemented contract

`admit_blob_mutation` is a separate update/delete prerequisite entry point. It never invokes either destructive handler. Its explicit internal result is `ToolResult | None`: `None` means stageable admission; an actual refusal carries the original feedback result through T1's `_UnappliedAdmission` and canonical audited dispatch. Successful store admission never uses composition-hash equality, including same-text updates. Proposal caps still apply after admission and before publication; `create_blob` remains immediate.

The typed blob facet now observes metadata plus both ledgers under the actual live mutation fence. It classifies the atomic all-NULL schema separately, retains deletion-before-replacement ordering, rejects simultaneous incompatible ledgers, and compares replacement metadata to the complete phase-appropriate durable snapshot. Historical producer lease identity does not have to equal the successor's current fence.

Valid interrupted debt is refused as recovery-needed only after non-writing path/artifact consistency checks and a fresh exact ledger/metadata/fence observation. Corrupt metadata, ledgers, invocation paths or bytes still fault in their original integrity categories. No recovery, adoption, reconciliation, staging or retirement is used to discover these prerequisites. Tests include real interrupted intent/swap/purge producers, deletion intent/staged/purge producers, atomic fork cleanup, old-backup alternatives, already-removed purge artifacts, and absent-file debt.

Fresh admission shares replacement metadata/immutable-field, retention and quota checks with real preparation, and shares the real commit retention/quota recheck. Deletion shares the exact snapshot/file-evidence predicates and the durable plan's exact evidence-domain validator. Pinned directory fds and no-follow regular-file reads validate canonical bytes; directory identity and live metadata/fence are checked after I/O. No Sessions transaction spans filesystem reads. Replacement requires correct existing bytes; deletion retains `(False, None, None)` absence evidence. Proposal readiness remains adapter-only: direct pending/error deletion, nullable hashes and absent files still work.

Current-draft references and relational pending-proposal/run/fork retention remain complementary. COMPOSE preview never invents a proposal exclusion. Real PROPOSAL preparation and acceptance retain their exact accepting-proposal binding and repeated checks.

Quota uses the existing authority and its real recorder: session projection includes output-reservation excess; identity/container admission uses positive net growth and live policies. Nonpositive growth still checks the session cap, but retains the owner-first policy-measurement bypass. Delete acquires no new growth gate. Revoked/unavailable accounting remains a distinct operational fault. Tests read real `AuthAuditRecorder` events and verify measured scope/cap/ceiling/usage, including identity precedence and cross-session/cross-identity usage.

## No-resource-effect evidence and controls

The oracle snapshots full blob metadata/provenance, both cleanup ledgers, output reservations, inline resolutions, applied-effect receipts, fence values, and canonical/artifact/neighbor/foreign path types and bytes. It forbids prepare/reserve/mark/commit/retire/abort APIs, recovery entry points, destructive dispatch, atomic publication, rename and unlink. SQL DML is forbidden except SQLite's exact existing fence self-assignment CAS. That exemption is not a blanket UPDATE permission; resource/fence mutation controls fail and full fence values remain unchanged.

Real actual handlers and real proposal acceptance prove the oracle sees intended durable effects. Same-text updates stage and accept with provenance/effect-receipt semantics. Existing actual receipt replay and acknowledgement-loss tests still pass. The real compose-loop cases prove known missing/bound/nonready/session-quota refusals publish no proposal, return feedback once, and leave the next repair on the original baseline; an at-cap blob refusal still returns feedback.

`d1-blob-oracle-mutants.py` changes imported objects only in isolated processes. All nine final mutants exit 1 at the relevant assertion: skipped retention; skipped identity or container admission; omitted reservation excess independently at session/identity/container bounds; broadened operation kind; swallowed replacement metadata corruption; and recovery during preview. `final-mutant-*.log/.exit` retain the exact failures. The runner emits an upstream AnyIO assert-rewrite warning; no warning suppression was added.

## Red/green and gate reconciliation

`loop-red3.log/.exit`: exit 1, six genuine current-base loop failures before production implementation. Bound calls create an extra proposal; missing/nonready calls raise at proposal publication instead of returning direct feedback. Earlier red fixture attempts are retained but are not claimed as valid evidence where their setup was wrong. `unit-red`/`debt-red` also retain missing-API reds over actual producer fixtures.

`file-evidence-red`: three failures proved a check-only deletion facet initially omitted the plan constructor's exact bool/hash evidence domain. `file-evidence-green`: three pass after extracting that shared non-writing validator.

The initial `final-response-gates` run was **red: 18 failed, 582 passed, 3 skipped**, not a baseline failure or a green gate. The internal success `_discovery_result` was incorrectly represented as a shippable tool payload. The approved `None` stageability contract removes that synthetic payload. No envelope scanner, teaching fence, response schema or forwarder registry was widened. Full error-code redaction census/forwarder/stale-site tests and the envelope gate subsequently pass on the corrected production tree.

The initial targeted authority gate was **7 failed, 1 passed, 1 xfailed**. Independent producer comparison proved that all affected writer multisets were identical except their line field. `d1-blob-authority-inventory-analysis.md`, its full JSON, and `d1-blob-authority-final-adapter-recheck.json` retain the comparison, mutation controls and frozen source hashes. The canonical `scan_production_writers` re-derived exactly 101 already-reviewed locations (79 repository, 22 service). `d1-blob-refresh-authority-lines.py` generated the applied patch and asserted the entire test AST is unchanged after normalizing only `WriterIdentity.line`. Fingerprints, ordinals, authority bindings, escapes, policy and scanner semantics are unchanged. The existing unreviewed fork-pair escape remains unreviewed. No signature/key operation occurred.

## Completed commands and terminal results

Logs and terminal `.exit` files are under `d1-blob-logs/`. Every pytest command uses explicit worktree cwd, the worktree `.venv/bin/python`, `-n0`, both worktree package roots in `PYTHONPATH`, and `-o pythonpath='<worktree>/src <worktree>/elspeth-lints/src'`. `provenance.log` proves both imports and HEAD. The producer additionally puts the worktree root on `PYTHONPATH` to import the test-owned scanner.

| Artifact stem | Terminal evidence |
| --- | --- |
| `final-behavior` | Before the internal None correction: exit 0, 271 passed |
| `final-corrected-behavior` | Corrected final candidate: exit 0, 271 passed |
| `final-driver-compatibility` | Exit 0: 421 passed, 1 PostgreSQL-URL-dependent skip; storage/authority production is unchanged since this run |
| `corrected-response-precheck` | Exit 0: 295 passed; all 84 error-code redaction cases plus all 211 envelope-gate cases |
| `final-corrected-response-consumers` | Exit 0: 145 passed; attribute contracts, ToolResult envelopes, blob response contracts, dispatch arms, proposal caps and progress |
| `final-corrected-ruff`, `final-corrected-format` | Exit 0: all 13 changed paths |
| `final-corrected-mypy` | Exit 0: all seven changed production paths |
| `final-corrected-contracts` | Exit 0: census unchanged, 2615 soft occurrences / 60 boundary-parsed; no whitelist/census change |
| `final-corrected-authority-module` | Exit 0: complete module, 298 passed / 1 existing generic xfail in 350.13s; all real injected mutation/escape controls executed |
| `authority-lines-producer-final` | Exit 0: 101 producer-derived line-only refreshes, non-line identity/AST unchanged |
| `pg-collection` | Exit 0: seven PostgreSQL cases collected; collection is not execution evidence |
| `d1-blob-parent-pg` (main lane) | Parent execution, exit 0: seven passed in 16.10s; before/after source comparison exit 0 |

The driver selection is `tests/unit/contracts/test_blobs.py`; blob service/fencing/replacement/deletion/atomic/fork-ack/quota-site suites; Sessions-derived mutation and storage-quota-authority suites; and Composer blob-inline tools. It covers actual existing preparation, commit, retirement and successor recovery. Its single skip explicitly requires `ELSPETH_TEST_POSTGRES_URL`. The initial broader response run also passed masquerade and mock-discipline checks; its three skips require Python 3.13 PEP 696 support. No all-rules lint corpus or signed trust-tier clearance is claimed.

The generic authority expected-failure is `test_all_production_sessions_writers_are_reviewed_typed_authorities`. Its measured final debt is 106 unexpected/unreviewed identities, 0 stale reviewed writers, 30 connection-containment gaps, 74 unresolved executions, 2 writers without named authority, 0 wrong-authority writers, 2 stale reviewed read bindings, and 0 invalid reviewed read policies. These overlapping categories are not a clean whole-tree inventory. The exact log preserves the outstanding sites, including the pre-existing escaping fork-pair helper. This task neither admits nor suppresses that debt.

## PostgreSQL and remaining release gates

The new module is `tests/testcontainer/web/test_blob_mutation_admission_postgres.py`. Parent ran the seven cases serially with explicit worktree provenance and the equivalent of this prelude:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest \
  tests/testcontainer/web/test_blob_mutation_admission_postgres.py \
  -m testcontainer -n0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src"
```

Seven cases use independent engines/connections, bounded Events and actual `pg_stat_activity` lock observations: admission waiter versus replacement/deletion intent; successful admission before a real writer; custody waiter behind paused admission; SESSIONS renew completing during paused file I/O; final metadata/fence expiry rechecks; a fence waiter observing committed expiry before content reads; and container-policy waiting against another identity's committed output reservation. The quota recorder is real. `d1-blob-parent-pg.log/.exit` proves all seven passed; `.before/.after/.frozen-exit` proves unchanged source throughout. I read and verified the terminal log and both zero exit files after the parent's report.

Parent still owns the combined full backend, final serial PostgreSQL selection, full static/corpus comparison and independent implementation review. This report does not claim whole-tree release clearance.

## Frozen staged handoff

`d1-blob-final-frozen-before.sha256` and `d1-blob-final-frozen-after.sha256` match for all 13 changed source/test/manifest files (`d1-blob-final-frozen.exit` is 0). This freeze covers the final corrected behavioral/authority/consumer/static runs. Parent's focused PostgreSQL before/after comparison separately exits 0. The earlier 295-case corrected response precheck ran on the same final production sources while the unrelated authority-test line literals were refreshed; its scanned Composer inputs did not change. The earlier compatibility run used the unchanged storage/authority production, before the internal adapter's return-contract correction. Neither earlier run is represented as a 13-file frozen-tree run.

Exactly the 13 paths in `d1-blob-files.txt` are staged, including all three complete new test files. `git diff --check` and `git diff --cached --check` pass. `d1-blob-admission-review.diff` is the complete staged binary-capable Git diff, with SHA-256 `6177a62c3ddf70ea749d145d10ba574907fc9340c0a8c1e143b218ca78ec2651`. HEAD remains the exact base. The deliberate untracked `src/elspeth/web/frontend/node_modules` is the only remaining untracked path and is excluded. No commit, merge, full backend run, complete testcontainer selection or all-rules corpus run was performed by this implementation task.

`final-artifact-verification.log/.exit` is exit 0: staged paths equal the exact intended set, every staged blob equals its frozen worktree file, all three new files are complete, review-diff bytes equal Git's complete staged producer output, and the frozen manifests match.
