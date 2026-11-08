# Composer session-cache implementation checkpoint

Continue the implementation plan in `composer-session-cache-implementation-plan.md`.
The user authorized implementation of all six stages, fixing verified Composer
bugs, delegation, and Astra review after every stage. The threat model is hostile
external input and accidental internal mistakes. No new cryptographic cache or
capability framework is needed.

## Branch and progress

Repository: `https://github.com/dta-au/elspeth.git`.
Task branch: `codex/composer-session-cache`. This is an implementation checkpoint,
not a completed migration or a release. Check whether the branch has been pushed
before assuming a new container can fetch it. The original remote base is
`3b28d537be98234da8135c418bf185a998c18b68`.

Completed commits before this checkpoint:

- `bff9269da`: two discovery-hit diagnostic/event-loop fixes and the reviewed plan.
- `caa0fbe6d`: controlled baseline fixtures and benchmarks; Astra approved Stage 1.
- `fb5e799ba`: session ownership and analysis gateway; Astra approved Stage 2.

Stage 3 is implemented in this checkpoint. Astra's final closure approved the
code and mixed-consumer evidence for checkpointing and Stage 4, subject to the
required final frozen-tree gates. Stages 4–6 remain to be implemented and reviewed.

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

1. Stage 4: prepare immutable base catalog summaries once per actual catalog/manager
   revision; stop repeated schema generation; isolate mutable responses. Add public
   policy/profile/catalog projections and explicit declaration cache lifetimes.
   Preserve `cacheable=True` as operation scope unless explicitly promoted. Keep
   `list_models` operation-only. Unify planner and outer discovery with canonical
   successful admitted payloads, fresh per-use envelopes/admission/audit/disclosure,
   and operation-local logical-repeat classification independent of physical hits.
2. Admit every tool's arguments before lookup using its owning parser. RFC8785
   collapses integer and integral-float JSON representations; actual `list_models`
   permits integral floats, while stricter future declarations must reject invalid
   arguments before any hit. Missing declared parsers/revisions bypass retention.
3. Inspect `planner_authoring_aids.py`'s bounded module `_AIDS_MEMO`: it currently
   keys only by snapshot, lacking catalog/profile revision. Migrate its public
   projection or justify a static application lifetime. Keep static skill/prompt
   compilation separate from per-session results.
4. Stage 5: current-state GET must borrow only after fresh ownership/head/hydration
   checks. Move schema-disclosure guidance from the old service tracker to the
   owner. Inject service-level retirement around the entire archive attempt,
   including cold/overlapping attempts, refusal, soft archive, consumption,
   postcommit failure/cancellation and library-fork cleanup. Wire orderly app
   begin/finish shutdown around real worker draining.
5. Stage 6: count/latency comparisons for disabled, cold and warm modes; bounded
   metrics without identities; configuration/architecture docs; Astra review and
   the final frozen-tree default and serial PostgreSQL suites and other gates.

Do not cache provider/advisor verdicts, authorization, blob access, publication,
transcripts, budgets or runtime-preflight verdicts across operations. Preserve
every provider-authoring transition and the ordinary tutorial backend.

## Environment and final-gate setup

Read AGENTS.md and CONTRIBUTING.md again in a fresh container. Use a dedicated
worktree and symlink its `.venv` to the main checkout as documented. Always export
both worktree source roots in `PYTHONPATH`; the editable venv otherwise imports the
main checkout. Use project-local `work/` for all logs, caches and basetemp.

The old container had frozen/all-extras Python dependencies, root/frontend locked
npm installs, SQLCipher, local Docker/PostgreSQL, Caddy and a SQLite CLI prepared.
Re-establish these from official sources in a new container; do not assume its
untracked binaries, node_modules or venv have transferred.

The user saved a new unrestricted-network configuration for **new containers**.
The current attached instance still reports restricted revision 1 and the official
Playwright CDN returns HTTP 403. No proxy bypass or fake browser-cache symlink was
used. Recheck the newly attached instance's observed policy, then install the
pinned browser. System Chromium also exists in the old container, but the current
HTTPS harness does not select it through an environment variable.

Before final merge, use the canonical detached full-suite gate for Ruff, mypy,
contracts, keyless lints, default pytest and serial PostgreSQL testcontainers on a
frozen tree. Reserve host capacity and run only one broad suite at a time. Explicitly
select the performance cases. The Stage-1 frozen keyless baseline had 5423 findings
and exit 1; compare finding sets under matching own-CWD/source-root governance
scope, not to zero. Never acquire an operator HMAC key or edit signed allowlists.
No persisted epoch, database migration, package version or release change is part
of this work. No merge or deployment has been performed.
