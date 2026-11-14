# Composer remediation pickup notes

Work is deliberately paused at the user's request. The user then requested a durable remote checkpoint. Resume remediation only when asked; do not interpret this document as an instruction to launch background work during the pause.

The checkpoint branch is `fix/composer-tool-remediation` on `origin` (`dta-au/elspeth`). The implementation checkpoint is **`dd3b3524ccd2a1910db98f77eb35f9d7ce69a249`**. The documentation commit containing these notes follows that checkpoint. This branch is a work-in-progress backup, **not a declaration that the whole audit is complete or ready to merge**. No merge to `main`, PR or release is part of the pause operation.

## Start here

1. Read this file, the repository `AGENTS.md`, and `CONTRIBUTING.md` whole-tree gate guidance. Verify the current branch, HEAD and working tree before making changes. Do not reset or clean existing worktrees.
2. Consult [the checklist snapshot](CHECKLIST.md) and the [live checklist Page](https://chatgpt.com/space/page_22de36ba40a08191b1aca9ae0b943d60). The snapshot records Page sequence 46; later Page edits take precedence. Keep the checklist current, and do not close shared fixes until their affected routes and combined gates are verified.
3. Read the pending verification section below. The next integration work is lint-delta adjudication, broad Astra review, and combined ordinary/proof/PostgreSQL verification. No new broad review or full behavior suite was launched after the user requested the halt.
4. For the next implementation wave, the reviewed blob pagination plan and the bounded recovery-audit classification fix are prepared but **not implemented**. Their entry points are below.

## Durable material and local-only evidence

- `../composer-tool-assessment/` contains the imported comprehensive audit documents from `audit/composer-orchestration-tools-20261109`.
- `reports/` preserves selected final implementation reports, review dispositions, designs, next-task briefs and the combined verification runbook. These are historical records: their original commit IDs, local paths and wording describe the point at which they were written. This resume file supplies the latest integration status.
- The tracked source and tests are the authoritative implementation. Review reports distinguish passing evidence from failed, interrupted, baseline or superseded attempts; retain those qualifications.
- Raw logs, temporary mutants, input manifests, runtime downloads and agent coordination state remain local under `.claude/lanes/composer-remediation-wave1/`. They are intentionally not committed. A fresh clone has the source, tests, audit, checklist and selected reports, **not every raw local evidence artifact**. Do not claim a raw log was independently rechecked if that environment is gone; regenerate the relevant evidence when needed.
- Do not commit the ignored lane directory or install/build caches. These notes and selected reports are durable deliverables under `docs/`.

## Accepted product and workflow decisions

- D1: return known refusals and no-ops directly to the model; create approval cards only for changes that pass validation-only admission. Existing no-op/refused approvals settle terminally rather than remaining pending.
- D2: retain file-backed MCP source authoring. Resolve against the configured data directory and reject out-of-root paths at admission, including the explicit-null legacy bypass case.
- Standalone MCP recovery must preserve existing platform support through portable supervision. Do not replace it with a Linux-only public contract.
- Pin behavior with meaningful tests, including discriminating mutation/negative controls where they establish the intended boundary. Preserve raw arguments and custody hashes where required.
- Use **Sol 6.1 at high** for implementation and **Astra at medium or high** for review. The user explicitly authorized appropriate subagents and skills. Supply bounded briefs and require file-backed reports. Old agent names are not evidence that those agents remain available after an environment transition.
- Do not obtain operator signing keys, hand-edit signed metadata, suppress lint findings to manufacture green results, forge Actions identity, or silently weaken whole-tree gates.

## Integrated implementation

The task branch started this wave at `5e192342c3fb025fd9a91244bebe437158e9de8e`. The final integration diff to `dd3b3524c` covers 119 files, 13,452 additions and 1,163 deletions. Those figures were measured with Git before the documentation checkpoint. The task branch was fast-forwarded to this implementation checkpoint for the requested remote backup.

| Area | Integrated revision | Report and review |
| --- | --- | --- |
| D1 approval lifecycle, validation admission, terminal settlement and related custody fixes | through `14fef5e5` | `d1-implementation-plan.md`, `d1-final-review.md`, `d1-final-verification.md` |
| D1 whole-tree fixture, binding and gateway repairs | `e192aa135` | `d1-gate-repair-report.md`, `d1-gate-repair-fix1-review.md` |
| Landscape metadata creation concurrency and dialect cleanup | `f9c23c59c` | `d1-landscape-metadata-report.md`, `d1-landscape-metadata-review.md` |
| Cache-hit audit envelope matches admitted model envelope | `205c56fe3` | `cache-audit-report.md`, `cache-audit-review.md` |
| Lifecycle failure, cancellation and retained-task foundations | `b4064d270` | `d2-lifecycle-1-report.md`, `d2-lifecycle-1-fix2-review.md` |
| Configured-root file admission | `1bc91710f` | `d2-path-admission-report.md`, `d2-path-admission-fix1-review.md` |
| Private portable process supervisor | `8c35a2e2a` | `d2-lifecycle-2-report.md`, `d2-lifecycle-2-fix1-review.md` |
| Three queue scope fields enter NodeSpec admission | `ec079378e` | `queue-admission-report.md`, `queue-admission-review.md` |
| Blob UUID identity across lookup, binding, retention, facts and proposal custody | `05bcab93955b2070ffdc90e72eddd7368502d2c1` | `blob-identity-report.md`, `blob-identity-review.md` |
| Public standalone MCP lifecycle and stdio ownership repair | `dc67bdd2f94bcedd6f59d29957791c5bccd58c06` | `d2-lifecycle-3-report.md`, `d2-lifecycle-3-fix1-review.md` |
| Park UUID identity and stronger refusal oracle | `dd3b3524ccd2a1910db98f77eb35f9d7ce69a249` | `park-identity-report.md`, `park-identity-fix1-review.md` |

All report filenames in this table are relative to `reports/`. The three latest patches were individually reviewed, rebased with unchanged patch bytes and source checks, and fast-forwarded into the integration tree. There are no outstanding per-task blocking review findings in those three slices; the broad combined review remains pending.

### Blob identity details worth retaining

External blob UUID selectors canonicalize without trimming whitespace. Successful ID echoes use owned canonical identity. Singular/plural bindings share duplicate detection; planner facts use actual argument models and canonical facts. Conservative proposal projection remains separate from strict embedded-marker/sentinel grammar. Original provider/stored arguments and receipt hashes remain unchanged, including retry/tamper controls.

Evidence includes 950 affected passes, 97 targeted passes, 171 inspection passes, 31 real PostgreSQL cases and eight discriminating mutants. The 1,089-test gate actually recorded **1,083 passes, three skips, one existing xfail and two failures**. Both failures reproduced on the base; five reviewed locator-only repairs subsequently passed the two gates and four negative controls. Do not rewrite the original cohort as an all-green run. Three new R6 parse-refusal diagnostics were accepted in the component review.

### Portable lifecycle details worth retaining

The installed CLI runs a dedicated stdlib supervisor in the original interpreter using the same portable path on all platforms. One private runner retains process custody and inherits stdio/cwd/environment. Bootstrap authenticates and consumes the nonce before runtime/provider imports. Actual process recovery and the coordinator exist before first worker submission; embedded hosts retain their own lifecycle authority.

Recovery generations retain outstanding observer/drain tasks across replacement and settle only after real cleanup. Linux PDEATHSIG setup lives in the dedicated supervisor after signal handlers and before child creation, with parent-registration race checks; embedded hosts do not receive process-wide mutation. Portable deadlines and retained process custody remain independent.

Astra identified that closing Python wrappers did not release FD 0/1 (`closefd=False`). The scoped repair explicitly closes the supervisor's inherited descriptors after child inheritance; stderr remains available. A real production-path test establishes EOF/lost input-reader while the supervisor is alive, while child ping/pong and stderr remain functional. The final bounded cohort passed 85 tests with four unchanged long cases deselected; a helper-only cleanup was followed by one focused pass and static checks. Earlier component evidence includes 600 focused passes and actual SDK preview/file admission/export checks. Real deadline, parent-death, control-channel-loss and quarantine cleanup were exercised on Linux. **Native Windows/macOS/BSD qualification is not available and is not claimed.**

### Park identity details worth retaining

Production changes are four added lines in `GraphRecoveryIntent.__post_init__`: existing raw type/length/nonblank checks run first; restore intents canonicalize parseable UUIDs after `strip()`. Non-UUID text/messages/raw tool arguments remain unchanged. Session authority, eligibility, fences, D15 refusal behavior, pending disposition and owned-corruption checks remain intact. Blob whitespace policy is different and unchanged.

Evidence: 42 successful route/dialect/spelling cases, 28 real PostgreSQL cases, and a normalization-removal mutant with 48 intended alias failures plus eight canonical passes. The affected gate passed 604 tests with one existing xfail. The final test-only review repair pins complete public assistant/notice equality for foreign versus unknown park aliases, across seven spellings and two head states, forbidding both raw and canonical IDs. It passed 28 tests. A raw-alias leak mutant passes the old four selected assertions but fails all four strengthened assertions. Astra marked the P2 addressed and passed spec/quality review. Planner refusal settlement is not covered by this new refusal matrix; all-route coverage applies to successful restore roundtrips.

## Verification at the stopping point

The canonical static gate ran against clean, frozen `dd3b3524c` in `.claude/worktrees/composer-remediation-landscape-metadata`. Its terminal directory is:

```text
.claude/lanes/composer-remediation-wave1/combined-wave-gates/
20261109T210349Z-composer-remediation-landscape-metadata-332098/
```

Actual terminal receipts:

```text
stage=ruff exit=0 seconds=3 fatal=1
stage=mypy exit=0 seconds=33 fatal=1
stage=contracts exit=0 seconds=36 fatal=1
stage=lints exit=1 seconds=252 fatal=0 findings=5454
after=dd3b3524ccd2a1910db98f77eb35f9d7ce69a249 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
driver_exit=0 reason=completed phase=complete stage=none stage_exit=UNKNOWN freeze_monitor=healthy
```

Each stage's freeze watcher exited zero. `.done` and `driver.exit` confirm completion. The wrapper's PASS means its fatal stages passed and its input remained frozen; **it does not mean the lint corpus is cleared**. The 5,454-finding corpus still requires semantic delta review. The later documentation-only checkpoint does not change production/test inputs; this is evidence at the recorded implementation SHA, not a new all-stage run at the documentation commit.

The supplemental pre-run freeze captured 31,837 inputs including physical frontend dependencies, with canonical manifest SHA `62992254b59a4ed2f5abad97194ee24b21cb3db8cee918c80f7a33a69e84a7ed`. The local helper is `d2-checks/freeze_tree.py`; it includes tracked/untracked inputs, modes and symlinks without following symlinks. External real-Chroma cache plugin SHA: `7f07b832cfaad2f929c25c4f4b3ab551db899ca863d4f4bb36bf64b764a91813`.

### Pending combined work

1. Compare the final lint corpus against the qualified frozen `b4064d270` baseline, and obtain Astra adjudication of actual changes. The baseline `combined-foundations-lints.json` has 5,433 findings and paired before/after/exit evidence. Its four additions over the earlier 5,429 corpus were reviewed in `combined-foundations-lint-review.md`. Preserve path/rule/full-message multiplicity; line-offset changes alone must not hide substantive changes. The intermediate `05bc` text capture has **no paired source freeze** and is corroborative only. The preliminary private-identity delta (5,436 to 5,446, 11 additions/one removal) is not final adjudication. If using text instead of JSON, disclose missing severity/suggestion/fingerprint information. Do not blanket-repin or suppress.
2. Broad Astra review of the complete branch diff from `5e192342c` through the implementation checkpoint, using component reports and honest gate limitations. Prepared local inputs are `combined-wave-review.patch` and `combined-wave-paths.json`; no broad reviewer was dispatched before the pause.
3. Combined ordinary Python tests plus the three official expensive proof partitions, followed by the separate PostgreSQL testcontainer selection. These have **not run on dd3b3524c**. Use the runbook and capacity/provenance controls below.
4. Diagnose new failures against an appropriate base; use discriminating controls for repairs. One consolidated review fix wave and scoped re-review are preferable to repeatedly launching broad reviewers. Re-run only checks justified by changed inputs and unresolved concerns.
5. Update checklist status with exact results. Do not equate individual slice review with completion of a shared audit item or the whole audit.

### Historical failures are not final results

At the older `14fef5e5` freeze, the broad backend run recorded 71,776 passes, 161 failures, 139 errors, 227 skips and one xfail; PostgreSQL recorded 1,068 passes, one failure and one skip. An initial 12-worker attempt was OOM/interrupted. Environment failures, fixture/binding repairs and the Landscape metadata defect were subsequently diagnosed and addressed, but those old runs must not be represented as green runs on the final candidate. The reports retain the detailed separation.

The old frontend run recorded 4,447 Vitest and 26 dependency tests at `14fef5e5`. Frontend production is unchanged **since that revision**, not since `5e192342c` (D1 changed sessionStore). Reuse historical frontend results only after an explicit unchanged-input comparison. New pagination frontend work requires fresh relevant checks.

## Execution runbook for resumption

See [combined-wave-gate-runbook.md](reports/combined-wave-gate-runbook.md). Its initial statement that the cache plugin is ordinary-only is superseded by its step 3: **load the identical plugin, pythonpath and environment for ordinary and all three proof collections**. All partitions collect the entire test tree before selecting cases.

- Last measured host: five CPUs, 17 GiB RAM, Python 3.12.14, Docker 29.9.2. Re-measure after reconnecting. Parent owns capacity: one broad suite at a time, at most four workers on that host, and serial/exclusive PostgreSQL. Do not repeat the 12-worker OOM experiment.
- Use explicit worktree paths. Export **both** `<tree>/src` and `<tree>/elspeth-lints/src`, and pass matching pytest `-o pythonpath`; verify both imported module paths. A linked `.venv` otherwise imports the main editable checkout silently.
- Local real dependencies: `runtime-deps/sqlite3/bin`, `runtime-deps/caddy/caddy`, `runtime-deps/terraform-plugins/main`, `playwright-browsers`, and verified real Chroma ONNX model/cache. Configure PATH, `TF_PLUGIN_CACHE_DIR` and `PLAYWRIGHT_BROWSERS_PATH`. Terraform binary was absent; 23 existing skips were reported honestly. Re-provision actual dependencies on a fresh host rather than faking them or changing shared-venv packages without analysis.
- The explicit `-p composer_test_cache` helper is local under `runtime-deps/pytest_plugins/`; it configures the real verified model cache after pricing initialization. It is not included in the remote checkpoint. Reconstruct/requalify that setup if the local lane is gone. Do not set global `PYTEST_PLUGINS`, which leaks into nested tests. A previous frozen smoke tested actual RAG multiprocessing and xdist collection; that is setup evidence, not a final broad test run.
- Use `scripts/full-suite-gate.sh --execute --detach` for ordinary static/behavior/PostgreSQL stages, explicit `--root` and project-local log directory. Check actual terminal exits and frozen receipts, not launcher exit zero. Do not edit inputs under an active gate.
- Ordinary uses `-p scripts.ci_test_shards --ci-partition=ordinary --ci-shard-count=1 --ci-shard-index=0 --ci-shard-report=<unique>` and identical explicit cache-plugin/pythonpath setup to the proofs.
- The three official proofs in `tests/unit/architecture/test_web_landscape_mutation_fencing.py` are `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists`, `test_required_work_reserve_admission_on_actual_sources`, and `test_required_work_reserve_rejects_protected_instance_formal_recovery`. Confirm exact current node IDs before execution.
- Use `scripts/ci_proof_progress.py` with exact node ID, Python 3.12 timeout 7,200 seconds, heartbeat 30 and grace 10. Supervise the canonical gate without detaching the inner command; only the outer supervisor may be detached. Keep process-group cleanup and freeze monitoring intact. Do not interrupt a long proof merely because it has been running a while.
- PostgreSQL is the separate `tests/ -m testcontainer -n 0` selection; default tests do not cover it.
- Verify same tested commit/interpreter/full collection digest across ordinary and three proofs, exact singleton proof IDs and actual proof-call success, no overlap, complete union and terminal exits. A controlled local checker for these records remains to be implemented. Qualify it with missing/overlap/head/wrong-proof/nonzero-exit controls. Native Actions aggregation rejects local identity; do not forge it.

## Next implementation work, not started

### Blob pagination

Read `reports/blob-pagination-plan.md`, `blob-pagination-design-review.md`, `pagination-service-brief.md`, and `pagination-service-preflight.md`. The five-task design is reviewed. Task 1 service/codec/contracts/app wiring has a completed read-only preflight at `dc67`; there is no implementation worktree or test result for it yet. Later tasks cover tool/REST/context wiring, planner tracking, UI, and PostgreSQL mutation proofs. Use an isolated worktree based on the appropriate reviewed checkpoint; preserve the frozen verification tree.

Important accepted boundaries:

- Same SQL authority, order `created_at DESC, id ASC`, filter before `limit+1`, exact UTC six-microsecond timestamps. Live pagination is not a snapshot and gives no current-absence guarantee, even after Refresh. No long locks or index migration in this slice.
- Strict bounded non-JWT cursor: canonical base64url JSON plus hex HMAC, fixed 30-minute root lifetime, session/projection/limit/order/nonce/start/expiry binding. App HKDF info is `b"elspeth-blob-inventory-cursor-v1"`; never use an operator signing key.
- Existing tool names adopt a v2 explicit page envelope; `list_blobs` bare-list to object is an intentional compatibility boundary. Legacy REST bare list stays; add the page route. Do not expose blob authoring over MCP.
- Composer metadata excludes descriptions/content and refuses the whole page on credential exposure. Existing ordinary REST metadata/output policy remains unchanged.
- Use the same configured BlobService through contexts; missing codec is a framework fault. The private position inspector uses the same codec, without DB/filesystem access or an expiry recheck after a successful query. Private audited discovery metadata must not change wire serialization.
- Planner coverage is rooted, contiguous, live and current-generation. Terminal pages alone, failed/invalid/unavailable pages or nonce-only variation cannot close a gap. Fresh roots invalidate old coverage; the aggregate 256 cap includes manifest history and reserves before publication.
- UI needs a closed runtime decoder/session echo, Load more/Refresh, truthful loaded/incomplete counts, duplicate-page faults, stale success/error fences including A-to-B-to-A, and mutation invalidation of continuation.
- The existing `_row_to_record` normalizes stored UUID strings. The new page path must **reject noncanonical owned raw ID/session strings before cursor creation, including lookahead rows**, rather than repair them. L0 contracts must not import the L3 resource helper. Explicitly convert aware timestamps to UTC.

### Recovery audit classification, tracker 11.19

Read `reports/recovery-audit-triage.md` and `recovery-audit-brief.md`. Confirmed bounded private audit bug; no implementation or tests yet. Planner `known_tools` includes dialect wire tools and the terminal name but omits the already-declared `PLANNER_RECOVERY_TOOL_NAMES`. The expected minimal repair unions that existing vocabulary, preserving genuine-unknown and known-but-unsent classification without widening execution authority.

Keep capture-stage semantics: some strict-control and sole-call refusals happen after generic admission receipts. Preserve raw aliases/messages, privacy filtering, physical-call/operation binding and historical records. Planned coverage includes main/hatch and both dialects, malformed/length/duplicate pre-admission controls, mixed/strict-model post-admission controls, persisted restore categorization, known-but-unsent/genuine unknown/privacy checks, and a removing-the-union mutant. This can be a separate bounded worktree alongside pagination service work when capacity allows.

Other audit items remain open in the checklist. The guidance-parity design/brief and next-remediation-candidates report are preserved for later scheduling; prepared designs do not imply implementation completion.

## Local workspace map and stopping state

Main checkout: `/workspace/elspeth`, branch `fix/composer-tool-remediation`. Integration tree: `.claude/worktrees/composer-remediation-landscape-metadata`, detached at `dd3b3524c`. Other preserved trees include `composer-remediation-resource-identity` at `05bc`, `composer-remediation-mcp-lifecycle` at `dc67`, and `composer-remediation-park-identity` at `dd3`. Do not delete them as part of resuming: they retain local evidence and earlier review inputs.

The active static gate completed normally before the pause. A `/proc` cwd inventory after completion showed only the inspection shell/Python process under this repository, with no remediation test process running. Prior agents had completed; reconnects had already invalidated older agent registries. No automatic follow-on implementation, reviewer, broad test or timer was started. Verify remote HEAD after the push and consult Git history for the documentation checkpoint's exact SHA rather than treating the implementation SHA as the branch tip.
