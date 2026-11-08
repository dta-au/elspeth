# Composer session cache implementation plan

Replace Composer's fragmented memoization with one bounded, application-owned cache per active authenticated session. Share reusable analysis and discovery data across the outer compose loop, nested planner, completion preparation, and current-state reads. Keep operation state and live execution checks at their existing lifetimes.

This plan targets the source at `3b28d537be98234da8135c418bf185a998c18b68` in `src/elspeth/web`, dated 8 November 2026. Unless prefixed with `src/`, `tests/`, or `scripts/`, code paths below are relative to `src/elspeth/web`. It follows the stated threat model of hostile external input and accidental internal mistakes: use typed interfaces, explicit ownership, immutable retained values, and regression tests. The change needs no new cache service, database schema, cryptographic cache protection, or approval mechanism.

# Outcome and scope

For a resident session and an unchanged, cache-eligible graph/dependency snapshot, repeated Composer consumers should compute each analysis once and reuse its immutable result. A new message may reuse session data after fresh ownership and policy checks. Graph edits, policy/profile changes, model-catalog changes, or different principals must select the appropriate different entry.

The first release covers Stage 1 validation summaries, explicitly classified discovery payloads, public catalog projections, and the lifecycle of schema-disclosure guidance. Retain the existing runtime-preflight coordinator and operation-local completed-preflight dictionaries. Provider responses, advisor verdicts, authorization, blob access, publication decisions, transcripts, budgets, and live plugin instances remain outside session-long result retention. Prompt Shields connection reuse and provider-call sequencing are separate follow-up work if request traces show they dominate remaining latency.

No public HTTP response shape, YAML configuration, persisted session epoch, Landscape epoch, or supported upgrade boundary should change. Public Python helpers must retain an uncached compatibility path where existing callers have no session context. A process restart simply makes caches cold. A session routed to another replica may recompute; database authority and acceptance behavior remain unchanged.

# Current behavior and evidence

| Path | Current behavior | Implementation consequence |
| --- | --- | --- |
| `app.py:2299` | Composer and its preflight coordinator already have application lifetime | Introduce a session-cache registry alongside them; do not reconstruct Composer per session |
| `composer/service.py:2497` | Discovery data starts in a new dictionary for every compose call | Acquire one session owner before choosing the planner or ordinary loop |
| `composer/discovery_cache.py:93` | Every restored discovery result calls `state.validate()` | Supply the current cached validation to a fresh result envelope |
| `composer/state.py:8443` | Every validation creates and closes its own plugin probe cache | Retain the completed immutable summary, preserving probe cleanup |
| `composer/pipeline_planner.py:4895` | Planner discovery dispatch bypasses the outer loop cache | Route both dispatch paths through the same discovery facade |
| `sessions/routes/composer/state.py:639` | State reload independently recomputes Stage 1 advice | Reuse analysis after reading the live owned graph head |
| `catalog/service.py:220` | Raw schemas are cached, but listings rebuild summaries and regenerate schemas | Prepare reusable base summaries once per catalog instance |
| `catalog/policy_view.py:29` | Each fresh policy view builds another listing cache | Reuse base metadata and memoize the public projection by current snapshot |
| `composer/schema_disclosure.py:13` | A service-wide session dictionary has no cleanup | Give disclosure guidance the session owner's bounded lifecycle |

Offline measurements establish redundant local work, not deployed end-to-end latency. Ten discovery-result restores of an unchanged valid 80-node graph invoked validation ten times. A valid 80-node type-coerce chain took about 94 ms per validation in one local run, constructing 80 transform probes each time. Reusing catalog summaries reduced a controlled policy-snapshot experiment from hundreds of milliseconds to approximately 5–7 ms, with equivalent snapshots and isolated returned objects. Re-run the same fixtures on the implementation machine before setting timing expectations.

# Ownership and interfaces

Use three small application components, with ordinary dependency injection:

1. `ComposerSessionCacheRegistry` in a proposed `composer/session_cache.py` owns resident entries keyed by authenticated principal scope and session UUID. The application instance already supplies deployment isolation; a registry is never a module-global singleton.
2. `ComposerSessionCache` owns typed validation and discovery namespaces, schema-disclosure guidance, and in-flight computations. An operation borrows the existing owner and releases it in `finally`. Nested planning and worker submissions receive that same owner explicitly.
3. `ComposerAnalysis` in a proposed `composer/analysis.py` provides cached analysis methods and an uncached implementation path. Async callers offload misses through the existing bounded worker pool. Synchronous tool handlers use the same facade from worker threads.

Retain application-owned immutable base catalog metadata outside the session object. Session projections reference it; sessions do not each duplicate the unrestricted schema inventory. Convert mutable Pydantic/list payloads to owned immutable cache records, or return copies at the API boundary. Do not return a shared mutable `PluginSummary` merely inside a fresh list.

Add the analysis dependency to `ToolContext`, `ToolBatchContext`, planner inputs, planning application, completion preparation, and applicable route dependencies. Pass it to nested helpers explicitly. Keep `CompositionState.validate()` as the uncached reference operation. Do not use a mutable `current_session` service field or assume context variables propagate through `run_sync_in_worker`; that helper submits explicit callables to a custom executor.

Calls without an authenticated persisted session receive a private operation-local context. They never join a shared literal `session:unsaved` partition. Existing direct helper and local MCP callers continue to work through the uncached or private context path.

# Cache identities and eligibility

Introduce typed key/value records and a dependency snapshot assembled from already available application and request inputs. Use the existing `composition_content_hash`; it preserves relevant authored ordering and works across reconstructed equal states. Version numbers alone are insufficient for competing candidates.

| Namespace | Key inputs | Retained value |
| --- | --- | --- |
| Raw Stage 1 analysis | Exact authored content hash, catalog/validator dependency generation, relevant mutable model-catalog revision | Complete immutable `ValidationSummary`, including errors, warnings, suggestions, and contracts |
| Profile-aware summary | Authored content hash, fresh principal-bound policy snapshot hash, application profile generation, analysis mode, relevant dynamic revisions | Immutable summary/public findings only |
| Public catalog projection | Catalog generation, current policy snapshot hash, profile generation, plugin kind/name | Immutable public summaries, schemas, and assistance |
| Session discovery | Declared tool identity, admitted canonical arguments, policy/catalog/profile dependencies, tool-specific revision | Successful immutable admitted payload, without state or invocation envelope |

Application-owned static dependencies may use an instance generation that changes when those dependencies are replaced. Prefer existing revision identifiers; add only the non-secret generations actually needed. Do not use `runtime_preflight_settings_hash` as a general key: it currently hashes only `data_dir`. Do not hash secret values or retain decrypted credentials to build identities.

Before enabling cross-turn Stage 1 reuse, inspect the live registry's validation/probe paths and prove their dependency boundary. The validator calls plugin constructors; its purity docstring alone does not establish that constructors ignore environment, files, credentials, or refreshable model metadata. For each ambient dependency, use an existing immutable/materialized input or an explicit revision. If a needed revision is unavailable, bypass retention for that computation and record a bounded bypass reason. Keep this classification small and data-driven; do not build a general dependency-tracking framework.

Profile lowering stays request-owned and uses the fresh snapshot. Do not retain `ProfileAwareValidationResult.executable_state` or another carrier containing private lowered configuration. Its summary may be reused only when its complete dependency identity is established. Fresh lowering and policy admission must still execute where required.

Cache invalid drafts as ordinary successful computations returning invalid summaries. Unexpected exceptions, cleanup failures, cancellations, and timeouts are not retained result entries. A result becomes reusable only after the probe context has closed successfully.

# Discovery lifetime and per-use behavior

The live registry currently marks nine tools cacheable within a compose call. Make the longer lifetime explicit in `ToolDeclaration` rather than silently broadening the meaning of `cacheable=True`. Use a small cache-scope enum or equivalent typed field with `none`, `operation`, and `session` scopes; default to no caching. Provide a separate revision resolver only where a tool needs one.

| Tools | Planned lifetime | Dependency rule |
| --- | --- | --- |
| `list_sources`, `list_transforms`, `list_sinks`, `get_plugin_schema`, `get_plugin_assistance` | Session | Current public policy/profile/catalog namespace |
| `get_expression_grammar`, `get_audit_info` | Session | Application/tool implementation generation |
| `explain_validation_error` | Session after handler dependency inspection | Canonical arguments and any state-dependent input actually consulted; otherwise keep operation scope |
| `list_models` | Operation initially | Promote only after the full selected provider/model-catalog revision is available in its key |
| Blob, attachment, source-inspection, secret, session-state, and mutation tools | Existing live behavior | No broad lifetime promotion |

Retain only the canonical successful data. On every use, rebuild a `ToolResult` with the actual current state and analysis summary, perform the existing response-contract admission and policy normalization, record a distinct invocation audit, and update disclosure only when data is actually delivered. Never cache a `ToolResult`, recorder, tool-call ID, or validation envelope from an earlier state.

The carried `last_validation` is a bare summary, not a cache key or a reliable fresh-state proof: failed mutations can place rejection-only diagnostics in it. Do not replace a real current-state analysis with that value merely to avoid recomputation. Retrieve a summary by exact content/dependency identity and keep mutation-rejection feedback separate. Preserve planner-specific per-use response-byte limits, truncation, provenance, and information-evidence bookkeeping; never retain a previously truncated provider message as the canonical payload.

Separate physical reuse from the existing logical meaning of a repeated tool call within an operation. A first discovery in a new operation can reuse session data, but must keep the cold path's budget, progress, and disclosure behavior. Maintain an operation-local seen-key set so the outer loop's `all_cache_hits` and related convergence logic do not accidentally treat every warm-session lookup as a free repeated call. Planner discovery currently charges each discovery turn at `composer/pipeline_planner.py:4852`; preserve that behavior even on a physical hit instead of importing the outer loop's free-repeat rule. The existing audit `cache_hit` fact can describe physical reuse; budget accounting must use the caller's existing logical classification. Test both dispatch paths explicitly.

Schema-disclosure guidance remains separate from cache occupancy. A retained payload is not evidence the current planner or advisor saw it. Preserve actual transcript evidence. On eviction, a conservative empty convergence hint is acceptable until disclosure is re-established; do not fabricate or delete durable disclosure history. Generation changes must not claim a newly projected schema has already been disclosed.

# Concurrency and lifecycle

Use a thread-safe per-key future to coalesce identical misses across synchronous workers and asynchronous callers. Protect lookup, insertion, and accounting with short metadata locks. Perform computation outside registry locks, and do not synchronously wait for a future on the event loop. Reuse the existing bounded executor and this explicit scheduling protocol:

1. A synchronous caller already running in the pool claims a missing computation and performs it inline. It never submits the producer back into the same pool.
2. An async caller first joins any already-running computation without consuming another worker. On a miss, a strongly referenced cache-owned async task submits a worker through `run_sync_in_worker`. Each request awaits that task through cancellation isolation.
3. Claim the actual computation key inside the executing worker, immediately before computing inline. Do not publish a joinable pending result whose producer is merely queued: synchronous joiners could occupy every thread needed to start it. Jobs queued before the first claim recheck the key when they begin and join or reuse the result. This can admit redundant small job wrappers during a race, but the expensive computation runs once.
4. Pin the session owner for each submitted job through actual worker completion. A requesting coroutine's `finally` releases its request borrow, not the worker pin. An admission rejection or cancellation before execution releases its own job reservation even though the body never ran. Once running, a worker must complete the pending future or remove its entry on every terminal path, including `BaseException`; retain no traceback as resident data.
5. Observe the independently owned task's terminal outcome. Original-request cancellation cannot cancel another caller's queued or running producer. If shutdown cancels a queued submission, remove its reservation and resolve affected awaiters so none remain stuck. Running work retains ownership until it finishes under the existing executor's drain behavior.

Test the protocol with a small real worker capacity and deterministic barriers: queued miss, running miss, cancelled first requester with a surviving waiter, all synchronous workers reaching one key, admission rejection, and shutdown during queued/running work. Assert completion and released accounting as well as computation counts. Keep this a small task/future protocol, not a new executor or lifecycle framework.

Retire matching local owners at the start of `SessionService.archive_session`, using a narrow injected registry hook so all callers are covered, including `sessions/routes/workflow/library.py:503`. Begin a session-level retirement reservation even when no owner is currently resident; keep it until the actual archive attempt and its protected work finish, releasing it in `finally`. While reserved, every principal's new borrows for that session use the uncached path. This prevents a GET from creating a fresh resident owner between the old owner's drain and the archive commit. Use an ordinary reference count/token for overlapping attempts, not a new authority system.

A failed archive attempt may make a live session cold; that is acceptable for an ephemeral optimization. Do not infer database outcome from HTTP success: soft archive and consumption are distinct paths, and quarantine cleanup errors or cancellation can occur after commit. This conservative service-level attempt invalidation avoids adding a commit-observer mechanism.

Never replace an owner while existing borrowers or workers can still use it. Mark it retired, keep it registered/accounted until they drain, and make new borrows during retirement take the uncached path. Late results can only finish against the retired owner and cannot populate its replacement. Fresh ownership checks remain authoritative on every replica; this local callback does not promise immediate removal of other replicas' resident data. Shutdown stops new borrows, joins cache-owned tasks in the existing worker shutdown order, and releases resident data after underlying workers drain.

Start with configurable internal limits and exercise them with measured fixture sizes. Proposed starting defaults are 128 resident sessions, 15 minutes idle lifetime, 16 analysis entries and 128 discovery entries per session, 2 MiB retained weight per session, 64 MiB retained weight per process, and a 1 MiB maximum individual value. These are tuning defaults, not measured capacity claims. Count all retained namespaces, keys, disclosure bookkeeping, and pending-computation reservations. Serialized payload size plus explicit overhead is a cache accounting weight, not a claim about exact process RSS.

Evict least-recently-used idle entries first. Never evict an in-use owner merely to satisfy a new borrow. Oversized values bypass retention; if registry capacity is exhausted by active owners, new sessions use the uncached path rather than creating another registered owner for an already resident session. Cache pressure may reduce hit rates but must not fail a valid composition. Bound expensive pending work through existing worker admission and coalescing, and test that memory accounting is released after failure and cancellation.

# Implementation stages

## Stage 1 Establish the baseline and dependency boundary

Port the investigation instruments into a focused, maintainable benchmark fixture under the repository's existing performance-test structure. Include a shipped small pipeline, a valid 80-node non-LLM graph, invalid drafts, and a profile-configured graph. Record actual validator/probe/schema-generation counts alongside elapsed time. Inspect live registered cacheable declarations and plugin probe dependencies. Define the initially eligible computation set and bypass reasons.

Acceptance: the instrument catches deliberately duplicated calls, equal reconstructed state produces equal results, same-version different-content candidates remain distinct, and benchmark output identifies graph and dependency fixtures. No remote provider calls are required for this stage.

## Stage 2 Build the session owner and analysis gateway

Add `session_cache.py`, `analysis.py`, focused unit tests, and application wiring. Implement typed namespaces, thread-safe single-flight, fake-clock expiry, size accounting, borrowing, retirement, shutdown, and the uncached path. Keep runtime preflight implementation intact. Add minimal configuration fields only for limits operators need to tune, following existing `WebSettings` validation conventions.

Acceptance: one resident owner is reused for a principal/session, independent principals and sessions cannot share results, identical concurrent requests compute once, a failed computation can retry, and saturation/expiry/oversized values preserve correct uncached behavior.

## Stage 3 Remove repeated graph analysis across Composer paths

Migrate discovery envelope restoration first, then the general `_discovery_result` and mutation/candidate helper seams in `tools/_common.py`. Thread the gateway through `service.py`, `tool_batch.py`, `pipeline_planner.py`, `planning_application.py`, `composition_completion.py`, and advisor preparation where they ask identical structural questions. Adapt `PolicyCatalogView.validate_composition_state` and `plugin_policy/validation.py` to separate fresh policy/lowering from reusable summaries. An independently testable first hardening patch may move current cache-hit reconstruction and normalization off the event loop while preserving its result semantics; it must not substitute a bare prior rejection summary for fresh validation.

Inventory direct Stage 1 call sites and classify each as migrated, deliberately uncached, or requiring a different admitted input. Keep validations before/after canonicalization or custody rewriting as distinct calls; they can hit only when their admitted inputs are equal. Avoid a blind source-wide replacement of `.validate()`.

Acceptance: a mixed same-session fixture spanning outer-loop discovery, planner discovery, and completion computes one eligible summary per exact input/dependency key. Every graph mutation or relevant dependency change produces the expected new computation and full diagnostics. The cold cache-hit reconstruction path does no graph CPU work on the event loop.

## Stage 4 Reuse catalog preparation and unify discovery

Build immutable base summaries once in `CatalogServiceImpl`, reusing its existing raw schema preparation. Make policy views consume those summaries without generating JSON schemas again. Add policy-scoped public projections to the session cache. Introduce explicit declaration scopes, then switch freeform and planner discovery to one lookup/compute/admit facade. Carry operation-local discovery data and seen-key bookkeeping under the same borrowed session owner/context.

Acceptance: warmed snapshot preparation no longer re-emits unchanged raw schemas; caller mutation cannot corrupt another response; current policy/profile changes alter visibility immediately; `list_models` stays fresh at its documented lifetime; warm and cold discovery have equivalent outward results, budget behavior, and per-use audit/disclosure effects.

## Stage 5 Integrate state reads and cleanup

After live ownership and graph-head reads, let `get_current_state` ask the gateway for advice keyed by the decoded graph and current policy inputs. Preserve hydration errors and existing head/refusal handling. Move schema-disclosure guidance under the session lifecycle, integrate the service-level archive-attempt retirement callback, and add orderly cache shutdown. Verify tutorial, recovery, forked sessions, and local MCP paths receive the correct same owner or their documented uncached/private path.

Acceptance: a reload of unchanged content reuses eligible analysis; a changed head or reconstructed different graph cannot reuse stale advice; forks have separate owners. Precommit archive failure/refusal, soft archive, no-data-directory consumption, postcommit cleanup failure/cancellation, and library-fork cleanup all exercise retirement correctly. A barrier test authenticates a read after the previous owner drained but before archive completion, including an attempt with no original resident owner; no replacement owner can be created during the attempt. Shutdown and archive release entries without orphaned work or resurrected owners. The tutorial uses the same cache and backend paths as ordinary sessions.

## Stage 6 Verify performance and prepare release

Compare the same fixtures with retention disabled, cold retention, and a warmed session. Record compute counts, local analysis time, cache hit/miss/join/bypass/eviction counts, and retained weight. Report provider, guard, and advisor time separately when a deployment trace is available. Define success primarily by removed duplicate computations and unchanged outcomes; use relative timing measurements rather than a flaky hard millisecond threshold in ordinary CI.

Add bounded operational metrics by namespace and result category, with no user/session/content identifiers. Update architecture/contributor documentation and configuration references for any introduced operator settings. Keep package version and changelog placement tied to the confirmed release branch during implementation; this plan does not assign a new release number.

Acceptance: the count-based targets below pass, all required regression gates complete on a frozen tree, memory remains bounded in the tested scenarios, and warm-cache output/admission decisions match the uncached reference.

# Verification matrix

| Area | Required cases and success criterion |
| --- | --- |
| Reuse | Ten identical discovery reads do not trigger ten validations; eligible mixed consumers compute once per unique analysis key |
| Identity | Same-version different-content candidates miss; equal persisted reconstructions hit; relevant ordering and metadata changes remain bound |
| Dependency changes | Fresh principal/policy/profile/model-catalog inputs select new projections; untracked ambient dependencies bypass retention |
| Live external checks | Blob deletion/replacement/expiry, secret revocation, grant changes, and a new operation fence still change the required runtime result with unchanged graph content |
| Result ownership | Returned list/dict mutation cannot poison retained values; no live probes or lowered private state survive in retained entries |
| Concurrency | Identical callers join one computation; different keys progress independently; waiter cancellation, worker failure, archive, and expiry release accounting correctly |
| Semantics | Cached discovery retains distinct invocation audit, per-use planner projection, and actual disclosure; first-use/repeated-use outer-loop budgets and every-turn planner budgets match cold execution; rejected-mutation feedback cannot replace current-state advice |
| Lifecycle | Fake-clock TTL, LRU pressure, all-active saturation, large entries, many policy revisions, fork, archive failure/success, and shutdown stay bounded and correct |
| Performance | Warm validation/probe counts collapse to one per eligible key, and warmed catalog listings do not regenerate unchanged schemas |

Use deterministic barriers and fake clocks for concurrency/lifecycle tests. Negative controls deliberately change content without version, rotate a dependency while preserving content, and disable memo reuse to prove count assertions detect duplication. Exercise real production dispatch paths with mocked provider transport; a helper-only test cannot establish planner/route integration.

During each stage, run focused tests with `-n 0` and store full logs under the task's repository-local `work/` directory. Existing anchor suites include `tests/unit/web/composer/test_service.py`, `test_tools_discovery.py`, `test_discovery_response_contracts.py`, `test_profile_policy_validation_adapter.py`, the `tests/unit/web/catalog/` and `plugin_policy/` suites, `tests/unit/web/execution/test_runtime_preflight_coordinator.py`, and session-route tests. Select the applicable cases rather than running every suite after every small edit.

Keep stable compute-count, equivalence, lifecycle, and integration assertions in normally selected unit/integration tests. Put elapsed-time comparison fixtures in the proposed `tests/performance/benchmarks/test_composer_session_cache.py` and select them explicitly: the normal pytest selection and canonical gate exclude the `performance` marker. Record full output, process exit, selected-case count, fixture identity, and bypass/cold/warm mode. For example, with `repo_root` set to the absolute implementation-worktree path:

```bash
cd "$repo_root" && .venv/bin/python -m pytest \
  tests/performance/benchmarks/test_composer_session_cache.py \
  -m performance -n 0 > "$repo_root/work/composer-cache-performance.log" 2>&1
```

Capture and read that command's exit code before interpreting its log. A zero exit with no selected benchmark cases is not performance evidence.

Before merging the completed shared-runtime change, run the canonical gate once against the final frozen tree, after checking shared-host test capacity:

```bash
cd "$repo_root" && scripts/full-suite-gate.sh --execute --detach \
  --root "$repo_root" \
  --log-dir "$repo_root/work/composer-session-cache-gate" \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Read recorded stage exits and frozen-tree status. The required PostgreSQL testcontainer selection needs Docker and runs serially; the default pytest selection does not cover it. Respect repository whole-tree attribute, masquerade, contract-census, and relevant boundary gates. Compare the known keyless trust-tier finding corpus before/after changes; do not claim operator signature clearance from a local green check. A missing prerequisite or known standing failure must be reported precisely rather than omitted from the result.

# Delivery and rollback

Implement in a dedicated worktree, keeping the registry/gateway, analysis integration, discovery/catalog integration, and lifecycle/verification changes as reviewable commits. Keep the dependent migration linear; only independent test/fixture work is suitable for parallel editing. Complete all stages before declaring the session-cache migration finished.

Fix verified Composer defects encountered during the work in bounded patches with a reproduction and regression test. Prioritize cache-hit event-loop stalls, stale or misattributed diagnostics, cancellation hangs, and lifecycle leaks when demonstrated. Keep each defect's behavior change explicit; do not fold unrelated refactors or unverified flaky-test guesses into the migration. If a defect reaches beyond the current slice, record its trigger and affected path and size the repair before expanding that slice.

Provide a single settings-controlled bypass for retained results so an operator can diagnose cache-related regressions without changing persisted data. The bypass must leave live runtime checks, existing bounded worker admission, and audit behavior intact; test both enabled and bypassed paths. A code rollback or process restart discards ephemeral entries and needs no data migration. Deploy through the project's normal release process after validation, using a same-session workload to compare duplicate counts and local latency with the baseline.

Definition of done: every targeted Composer path uses the same resident session owner when eligible; lifecycle and pressure fallbacks are explicit; the verification matrix and required final gates pass; remaining uncacheable work is named with its dependency reason; and measured results separate saved local computation from unchanged provider/network latency.

# Astra review

Astra reviewed the plan against the current source and re-reviewed the revisions on 8 November 2026. Final verdict: ready for implementation, with no blocking plan findings remaining. Implementation must still establish dependency eligibility and pass the stated tests.

The review produced three substantive changes: an explicit single-flight protocol compatible with worker admission and cancellation; service-level retirement held for the entire archive attempt, including initially cold sessions; and separate explicit selection of performance benchmarks. It also clarified planner-specific budget/projection behavior and rejected reuse of a bare prior mutation-validation summary as fresh graph advice.
