# Composer session-cache implementation checkpoint

**Latest durable status:** [quota-conscious handover](composer-cache-handover.md).
The handover preserves the untested bootstrap patch and supersedes older pending-status paragraphs below.

Continue the implementation plan in `composer-session-cache-implementation-plan.md`.
The user authorized implementation of all six stages, fixing verified Composer
bugs, delegation, and Astra review after every stage. The threat model is hostile
external input and accidental internal mistakes. No new cryptographic cache or
capability framework is needed.

## Remote checkpoint status — 8 November 2026

The user authorized a dedicated feature-branch push of the current fixes.
This checkpoint includes the session cache, discovery/lifecycle integration,
follow-up performance changes, ADI endpoint defaults, workflow display repairs,
regression tests and profiling scripts. It is not merge or deployment approval.
Raw diagnostics, databases, provider captures, credentials and local `work/`
artifacts are excluded; referenced local receipts remain in the preserved workspace.

Acceptance is incomplete. The latest caller selection finished with 850 passes
and three deadline-path failures. All three then passed unchanged in the isolated
serial selection (9.45 seconds; frozen receipt), so no timeout increase or
assertion weakening has been applied. This does not prove their original cause.
The next integrated gate was stopped after a new plugin source-hash finding;
its pytest and PostgreSQL results are incomplete. The ADI hash was repaired and
all eight hash/registry/provenance controls passed. The earlier completed broad
failed gate remains the authoritative broad receipt, with focused reruns below.
Two global authority proofs, final integrated backend/PostgreSQL acceptance and
an exact final-source matched profile remain outstanding. Astra has approved
bounded changes, not the entire final release. Original transfer-export untracked
contents remain unverified. No merge or deployment has occurred.

## Latest reconciliation — 8 November 2026

The complete frozen canonical gate at `work/composer-session-cache-gate/20261108T142140Z-composer-cache-final-319122`
failed: default pytest 71,191 passed, 53 failed, 267 skipped and 1 xfailed;
PostgreSQL 1,179 passed, 3 failed and 1 skipped. These remain valid failed
receipts; later focused repairs do not rewrite them. Ruff/mypy/contracts passed.
The keyless lint scan reported 5,458 findings and requires delta review, not
an agent-held signing key.

Subsequent focused repairs cover the catalog serialization/ownership cases,
source inventories, precise protected-reader transfer, review-cohort rollback,
ADI endpoint defaults, lease scanner and startup cancellation fixtures.
`work/post-gate-focused-3` passed all 39 cases. The later probe qualification
also passed the previously crashed consumed failure-carrier test. Two of the
three original PostgreSQL failures passed unchanged in a serial rerun; the
lease-acknowledgement failure reproduced and has a reviewed test repair that subsequently passed all six module cases. Full default/serial PostgreSQL acceptance remains unfinished.

The matched main comparison is complete: six functional qualifications and
three 20-turn pairs (120 measured turns), all with exact workload parity and
unchanged source. Median aggregate CPU fell from 76.374 to 58.796 seconds;
paired reductions were 14.87%, 29.23% and 23.02%. SQL differences were small,
and no memory improvement is established. This measures the recorded snapshot
before the subsequent policy-only component-probe optimisation. See
`composer-session-cache-profile.md` for all 20 turns, full conditions and limits.

The catalog notice overlay is repaired and browser-qualified. The user clarified
ADI is not deployed; a new documented endpoint-only default was implemented
from source analysis, preserving explicit precedence and credentials. The first
staging report is now available and audited; the second complex Bug Rep remains
unavailable. This checkpoint is being prepared for the newly authorized push; no merge or deployment has occurred. Original
transfer-export untracked contents remain unverified.

Latest focused qualification is now complete: `work/post-gate-probe-3`
passed 69 cases (31 policy-only component probes, 37 ADI cases and the previous
worker-crash case), and `work/post-gate-pg-ack-1` passed all six PostgreSQL
module cases, including commit/expiry/unrelated-error controls. Astra closed
that bounded technical review. `work/post-gate-recovery-1` then passed all
12 previously failing recovery cases in 1,197.59 seconds, exit 0 and unchanged
source, at the original deadlines. Original-failure reconciliation now has
51 passing reruns and two pending global authority proofs. A fresh integrated
broad gate is required; these scoped passes are not a retrospective canonical
PASS and do not establish an exclusive cause for the earlier timing failures.

Historical test working data was archived and compared byte-for-byte before
removing its expanded copy to make room. See `work/test-data-archives/receipt.json`
and the original-permission metadata; only inactive Unix socket entries were
not archived. Source, logs and raw profiling results remain intact.

## Branch and progress

Repository: `https://github.com/dta-au/elspeth.git`.
Original task branch: `codex/composer-session-cache`. Repairs are being prepared
on `codex/composer-cache-final` in a separate worktree preserving the original
frozen snapshot. Both descend from local Stage 5 commit
`1a97c2c04e69c5dd2e5971f2b7e5fe718567ad84`. This is an implementation checkpoint,
not a release. The pushed checkpoint `3adcea5cce15bd3f2d81c2864c73026359542047`
was fetched and verified in the new container. Subsequent commits are included in this authorized feature checkpoint. The original remote base is
`3b28d537be98234da8135c418bf185a998c18b68`.

Completed commits before this checkpoint:

- `bff9269da`: two discovery-hit diagnostic/event-loop fixes and the reviewed plan.
- `caa0fbe6d`: controlled baseline fixtures and benchmarks; Astra approved Stage 1.
- `fb5e799ba`: session ownership and analysis gateway; Astra approved Stage 2.

Stage 3 is implemented in this checkpoint. Astra's final closure approved the
code and mixed-consumer evidence for checkpointing and Stage 4, subject to the
required final frozen-tree gates. Stage 4 is locally committed as `695230a63` and
Stage 5 as `1a97c2c04`; Astra approved both. Stage 6 adds metrics, documentation,
and deterministic count/performance comparisons; final gate evidence must be
checked before declaring implementation complete.

## Implemented behavior

The application creates one `ComposerSessionCacheRegistry` and passes that exact
instance to Composer. Each authenticated persisted-session operation borrows a
principal/session owner, binds one `ComposerAnalysis` to its `PolicyCatalogView`,
passes it through nested consumers, and releases the borrow in `finally`.
Unsaved/anonymous compatibility paths remain uncached.

The registry has bounded validation/discovery/projection namespaces, TTL/LRU and
byte/count pressure fallback, running-work coalescing, archive-attempt reservation
primitives, disclosure bookkeeping, and shutdown primitives. Its defaults include
128 owners, 900 seconds idle expiry, 16 validation entries, 128 discovery entries,
2 MiB per owner, 64 MiB process retained weight, and 1 MiB maximum entry weight.
These are accounting weights, not an RSS guarantee.

`ComposerAnalysis.run_in_worker` and `PolicyCatalogView.run_in_worker` pin a captured
owner before submission and release it through the actual concurrent future's
completion callback. Request cancellation remains prompt. Queued cancellation,
admission failure, repeated cancellation, retirement, and the executor's
RUNNING-before-callable-entry gap are covered. A retired owner cannot be replaced
until its borrows and actual workers drain. The bridge reuses the existing bounded
pool; it introduces no executor or generic capability system.

Raw keys bind authored content and actual manager/class registrations. Unknown or
replacement classes bypass retention. Exact audited builtins are resolved from
already-loaded canonical modules, without forcing optional SDK imports. Power
Automate source/sink, database sink, and LLM transforms selecting Langfuse tracing
bypass because their constructor dependencies are not fully versioned.

Policy authorization and profile lowering remain fresh. Genuinely lowered state
is never hashed or retained; its immutable summary uses the authored key, current
snapshot and non-secret monotonic profile generation. Ordinary unchanged lowering
shares raw analysis. Current source-demand hypotheses each use their own exact
content key, while samples and interpretation-event reads remain fresh.

Tool helpers and both dispatchers use keyed current-state advice. Rejected
mutation feedback is kept separate from cached summaries. Snapshot-only validation
tag reuse was removed because replacing a result's graph could preserve its tag.
Withheld replacement diagnostics remain rejection-only. Worker placement includes
prompt preparation, discovery restoration, planner/commit, interpretation review,
completion and source-demand operations.

`ELSPETH_WEB__COMPOSER_SESSION_CACHE_ENABLED=false` bypasses retained results while
preserving live checks and bounded disclosure guidance.

## Verification recorded so far

Stage 1: 12 controlled unit cases and five explicitly selected performance cases.
Stage 2: 40 core, five stream-consumer, three app/config cases; the boundary gate
completed with 249 passed and three Python-3.13-only skips on Python 3.12.

Stage 3 focused completed checks include 50 worker/cache cases; 266 policy cases
and 34 final adapter/catalog cases; 113 source-demand/surfacing cases; 107 completion
regressions plus eight final focused cases; nine new tools cases and 397 scoped
tool regressions. Mypy, Ruff and the contract census passed their recorded scoped
checks. The five attribute-contract cases passed. No census/whitelist change was
needed: 2613 soft occurrences, 416 files, 60 boundary-parsed occurrences.

Two normally selected mixed-consumer tests use the real shipped graph, catalog,
outer compose dispatch, planner provider parser/discovery dispatch, and completion
raw lookup under the same actual session owner with default limits. Enabled
retention computes the target graph once; disabled retention increases the count
at every transition with equal full diagnostics and expected output. Counter
positive/negative controls and actual two-plus-two provider calls are asserted.

The root planner/cache/commit regression log records a completed 411-passed summary
in 665.04 seconds. Its executor completion handle was lost across user steering
before the exit code was retrieved, so this is not claimed as measured exit-zero
gate evidence. The required final frozen-tree suite must still run.

## Remaining work

The first canonical default suite terminated with exit 1: **70,481 passed,
478 failed, 184 errors, 226 skipped and 1 xfailed**. Its freeze monitor was
healthy. Ruff, mypy and contracts passed; keyless lints exited 1 with 5,452
findings. The superseded serial PostgreSQL run was intentionally interrupted
(exit 143) to release capacity for the corrected candidate; it is not passed.
The driver terminated with exit 1 and `frozen=yes`. A complete PostgreSQL run
on the corrected candidate remains required.
Failure triage distinguishes environment failures (read-only home caches,
overlong socket paths, nested Git discovery), obsolete fixtures, reviewed
source/AST guard drift, and a real catalog serialization regression. These
are not all being classified as flaky.

The catalog repair preserves unsupported leaves from unrevisioned producers
so existing evidence admission omits them, returns isolated deep copies,
and prevents subclasses from inheriting cache eligibility for ambient state.
Owned registered-plugin schemas reject non-JSON leaves before serialization.
The repair and new controls are applied in the repair worktree; 115 focused
schema/catalog/cache baseline/telemetry cases passed (exit 0), and catalog
type checking passed. Original omission assertions remain intact.

Reviewed guard repairs have 25 focused passing controls, including new
app-owned registry identity negatives. Their full gate remains outstanding.
Additional fixture corrections are in progress. No signed allowlist metadata
was changed. See [follow-up investigations](composer-follow-up-investigations.md)
for each separately reported bug.

`scripts/profile_composer.py` measures actual HTTP admission, durable worker
settlement and terminal status with controlled provider transport. The planned
matrix measures 20 successive turns, small/80-node graphs, discovery/mutation,
CPU and separate memory processes, with SQLite and PostgreSQL. The first quiet
20-turn small/discovery SQLite CPU run completed successfully. The following
mutation run stopped at the harness's durable-review qualification query before
recording a measured row; that failure is under investigation and is not yet
classified as an application defect. Replication and memory measurements remain
outstanding. Each run preserves raw NDJSON plus source/profiler fingerprints,
commit, database version, package versions and resource limits. A separately
measured main baseline is not assumed to have matching conditions.

The user subsequently supplied OpenRouter models for a bounded live pilot:
`deepseek/deepseek-v4.1-flash` and `z-ai/glm-5.3`, with 10 composition rounds,
24 discovery rounds, a 240-second turn and rate limit 20/minute. Free provider
metadata checks found both models. The user clarified that the account is
manually replenished, has concurrent experiments, and has no fixed $5 task cap.
A subsequent free credits query returned an available balance. The first
dispatched live pilot made three returned calls, reporting approximately $0.0083
upstream cost, 9.16 seconds of SDK wait and 13.80 seconds of turn wall time. It
did not complete: the conservative $3 full-model-limit reservation guard refused
an additional Advisor call before dispatch. This is interrupted measurement,
not a successful-turn timing or an application deadline failure. A single
follow-up pilot has a $10 total reservation ceiling and eight-call activity
limit, retaining the existing three reservations without refund/reset. Every
dispatch still requires current account credits plus headroom. Account-wide
consumption cannot be attributed to this experiment. Credentials are excluded from this document,
profiling reports and Git.

Stages 4 and 5 are implemented, with frozen Stage 4 acceptance (629 cases plus
2 planner budget cases) and Stage 5 acceptance (26 focused, 1291 regression, and
102 MCP/tutorial/attribute cases), all measured exit zero. See
[the architecture](composer-session-cache.md) for the resulting behavior.

Stage 6 requires Astra review, explicit disabled/cold/warm performance selection,
and the final canonical default and serial PostgreSQL gates on a frozen tree.
Keep exact exit codes and any lint-baseline differences in the final handoff.
Do not treat completed migration or focused tests as final-gate success.

Do not cache provider/advisor verdicts, authorization, blob access, publication,
transcripts, budgets or runtime-preflight verdicts across operations. Preserve
every provider-authoring transition and the ordinary tutorial backend.

## Environment and final-gate setup

Read AGENTS.md and CONTRIBUTING.md again in a fresh container. Use a dedicated
worktree and symlink its `.venv` to the main checkout as documented. Always export
both worktree source roots in `PYTHONPATH`; the editable venv otherwise imports the
main checkout. Use project-local ignored paths for logs and caches; keep
temporary socket roots short and isolate miniature Git repositories. Avoid
inheriting a shared `--basetemp` into nested pytest processes.

The old container had frozen/all-extras Python dependencies, root/frontend locked
npm installs, SQLCipher, local Docker/PostgreSQL, Caddy and a SQLite CLI prepared.
Re-establish these from official sources in a new container; do not assume its
untracked binaries, node_modules or venv have transferred.

The new container reports enforced unrestricted HTTP. Official pinned Playwright
Chromium/headless downloads and actual browser launch succeeded; the real HTTPS
progress test passed. Frozen Python/all-extras dependencies, exact Node 24.13.0 /
npm 11.6.2, locked npm installs, SQLCipher, official SQLite CLI, Caddy, and Docker
PostgreSQL were verified. These are local setup artifacts, not Git content.

The original workspace's additional transfer archive was not accessible here.
Only pushed Git content is verified migrated; completeness of any additional
untracked export remains unverified. The original session was preserved.

Before final merge, use the canonical detached full-suite gate for Ruff, mypy,
contracts, keyless lints, default pytest and serial PostgreSQL testcontainers on a
frozen tree. Reserve host capacity and run only one broad suite at a time. Explicitly
select the performance cases. The Stage-1 frozen keyless baseline had 5423 findings
and exit 1; compare finding sets under matching own-CWD/source-root governance
scope, not to zero. Never acquire an operator HMAC key or edit signed allowlists.
No persisted epoch, database migration, package version or release change is part
of this work. No merge or deployment has been performed.
