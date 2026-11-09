# D2 and adjacent02.17 design review

Read-only source review, 2026-11-09. Measured HEAD:
`5e192342c3fb025fd9a91244bebe437158e9de8e` (`git rev-parse HEAD`). Read root
`AGENTS.md` and `.claude/lanes/composer-remediation-wave1/d2-mcp-path-map.md`.
No production/test edits, executed tests, providers, or child agents. This note
recommends implementation; transport observations below are attributed to the
existing lane evidence, not reproduced in this review. Destination checked with
`git check-ignore -v docs/d2-design-review.md`: no output, exit 1.

## Recommendation

Implement D2 as a small nominal MCP configured-root authority threaded through
existing authoring and validation, and adjacent02.17 as standalone stdio process
lifecycle ownership around `run_server`. They are distinct defects but both are
needed for a useful fresh-process file-authoring/preview acceptance test. Do not
instantiate a Web app, invent an HTTP MCP server, replace bounded workers, or
install a callback that only records/logs recovery failure.

The existing lane's fresh SDK stdio child successfully initialized and mutated
state but returned `Tool error: AuditIntegrityError` for preview; a separate
observation-only in-process wrapper identified `Shared executor recovery callback
absent`. Source agrees: `server.py:1053` uses `run_sync_in_worker`,
`async_workers.py:499` requires registration, while `run_server:1076` merely
builds and runs the transport. `tests/conftest.py:679` supplies an autouse owner,
so an ordinary pytest-backed preview is insufficient regression evidence.

## D2: narrow API and ownership

1. Introduce one frozen nominal value, for example `McpConfiguredRootAuthority`,
   holding the canonical configured data root. A natural home is alongside
   `web/paths.py` (or one small sibling module), not an inheritance/strategy
   framework. The root is issued from operator configuration in the stdio
   construction boundary, never from tool JSON or scratch/session identity.
   Resolve the configured root once and use that same root for admission,
   preflight settings, YAML resolution and cache identity.
2. Add an explicit optional keyword through `create_server`, `_dispatch_tool`,
   `execute_tool` and `ToolContext`; then pass it to the existing three path
   guards. An explicit required keyword on `create_server` is a reasonable
   compatibility decision, but not necessary to the fix. If omitted, the MCP
   dispatcher must still refuse all file path writers (source, output and
   provider), including bulk replacement. Existing general-purpose Web/direct
   dispatcher defaults need not change tree-wide merely to repair MCP.
3. Reject wrong nominal types and contradictory Web custody context as internal
   contract errors. `session_id=None` must keep its existing Web meaning. MCP
   root authority is separate from blob provenance, operation authority, session
   engine and user identity. A genuine Web caller must never acquire root-wide
   access by omitting its session id or supplying a magic id.
4. In the MCP branch resolve source, output and provider local path keys with
   `resolve_data_path` against the owned root; accept only canonical descendants.
   In the ordinary Web branch retain `allowed_source_directories`,
   `allowed_sink_directories` and `resolve_sink_data_path` unchanged. Existing
   sink spelling canonicalization remains: bare `report.csv` becomes
   `outputs/report.csv`; the MCP resolver must not subsequently insert a fake
   session component. Preserve source/sink/provider typed refusal codes and
   full-state no-change results.
5. Validate Tier-3 path values before filesystem construction (exact nonempty
   string and no NUL). Preserve explicit nullable nested provider path semantics.
   Do not conflate missing authority with missing directory or leaf file;
   containment does not require existence. Root symlinks should follow the same
   canonical-root interpretation as existing `Path.resolve`; an in-root symlink
   that resolves outside refuses. Runtime revalidation still matters if a
   symlink changes after authoring. This is resolved-path confinement, not a
   claim of race-free OS sandboxing.

The path-writer inventory and detailed mutation sites are already in the lane
map. Reuse those eight writers, including both singular/plural `set_pipeline`
sources and merged patches. Apply bulk guards before any inline blob persistence
and before committing the candidate. `load_session` may preserve old state, but
cannot turn legacy escaped paths into successful preview or raw public exports.

### Preflight propagation that must not be missed

Thread the same optional nominal authority through `validate_pipeline`,
`_ValidationRunImpl` / `ValidationPipeline.run`, `_validate_pipeline_impl`,
`validate_path_policy`, `materialize_validation_yaml`, and
`resolve_runtime_yaml_paths`. Keep strict-only preview, existing real plugin/graph
validation and side-effect-free preflight behavior.

There is a second resolution path: `_compiled_identity_settings`
(`validation.py:411–438`) independently materializes authored identity YAML and
calls `resolve_runtime_yaml_paths`. It also needs the authority, including its
caller. Otherwise path validation may succeed and execution YAML use the MCP
root while compiled identity uses fake-session/Web resolution. Test equality of
the resolved output/provider path in actual and identity configurations.

`_build_stdio_server:1033–1050`'s synthetic-session rationale is obsolete under
this design, including the statement that public YAML emits authored paths
verbatim. Remove the fake filesystem authorization; a private nonce may remain
only if needed for coordinator scoping. Cache identity must distinguish canonical
root and policy mode in addition to the existing settings/plugin snapshot.
Public YAML projection must continue stripping private source/output/provider
paths without rewriting private live state.

`resolve_runtime_yaml_paths:185–215` deliberately limits provider rewriting to
generated `transforms`, with a documented future trigger for collector plugins.
Do not silently broaden that unrelated exclusion based only on vocabulary;
preserve the existing plugin-bearing-node admission guards.

## adjacent02.17: own the actual process lifecycle

The smallest sound production boundary is `run_server` (or a small private
context manager used only by it), before building/serving the stdio server.
Keep `create_server` and `_build_stdio_server` construction free of process-global
ownership mutations: embedded construction may share Web's executor, and the
global `configure_required_executor_recovery` API is not a safe multi-owner
registry. Do not overwrite a live owner or reset private executor globals to
make standalone construction pass. An embedded host remains responsible for
its own lifecycle, as its supplied callback already implies.

Use existing owners: a shared `threading.Event` for draining, a separate
generation-unavailable event, `ProcessRecovery`, and the existing real
`ProcessWatchdogControl` factory. Register
`process_recovery.required_generation_expired` with
`configure_required_executor_recovery` before the first possible shared
submission; assert watchdog readiness and start its monitor. No application
finalizer capability is needed merely for read-only preflight; the executor
configuration already permits `application_finalizer_owner=None`.

The recovery callback is substantive: `RequiredExecutorGenerationCustodian`
quarantines a broken submission, retains actual admission/witness custody,
observes the drain deadline and calls recovery at `required_executor.py:317`.
`ProcessRecovery` arms independent supervision and requests process termination.
Retain those semantics and the existing worker admission/timeout behavior.

Shutdown must execute on normal EOF, transport failure, partial startup and
cancellation. Stop accepting new tools, mark draining and arm the existing
shutdown deadline, then settle owned preflight tasks and join actual executor
work through `shutdown_async_workers`. Keep original exceptions/cancellations
and cleanup failures (aggregate when distinct); catching cancellation must not
skip the remaining joins. Do not use a finite timeout as evidence a Python
worker has stopped. A hung worker must remain owned until independent process
recovery acts. Only after actual joins and successful resource cleanup may the
owner issue `ProcessCompletionWitness`, complete the watchdog, and join the
monitor. A lost/quarantined generation or failed cleanup must not get a clean
completion witness. Preserve normal caller cancellation as an outcome even when
cleanup successfully settles it.

One real missing seam: `RuntimePreflightCoordinator` retains shielded tasks after
caller timeout/cancellation (`runtime_preflight.py:100–108`) and currently has no
close/join API. `create_server` hides its internally created coordinator. For
standalone ownership, explicitly create/pass the coordinator to the builder and
retain it for teardown. Add one bounded-in-scope seal-and-join method (or an
equivalent private owned preflight-task collection) so teardown can account for
its actual tasks before disarming. Do not cancel unrelated tasks via
`asyncio.all_tasks` or reach into `_inflight` from production transport code.
Sealing should prevent new work; joining should retain caller cancellation and
observe actual outcomes. This is a small lifecycle seam, not a new executor
framework. Shared embedded coordinators must be drained only by their host.

The JSONL recorder opens and fsyncs during append (`audit.py:166` onward); its
documented unsaved-session in-memory buffering is not a reason to add a new audit
durability subsystem here.

## Source-grounded design constraints versus routine choices

* **Compatibility constraint requiring an explicit decision:** the existing
  production `ProcessWatchdog` is Linux-only (`process_watchdog.py:1`, `pidfd_open`
  at 107 and `pidfd_send_signal`). MCP's existing parent-death workaround
  explicitly skips macOS/Windows/BSD (`server.py:1130`); the server itself does not
  currently declare those platforms unsupported. Unconditionally adopting this
  watchdog would introduce a platform restriction. On this Linux environment it
  is directly reusable. Either explicitly document a Linux-only supported stdio
  lifecycle boundary, or supply an equally substantive supported-platform owner;
  a no-op fallback is invalid. Do not build an entire portable supervision
  framework speculatively. This is a real compatibility question, not evidence
  that the D2 authority design is blocked.
* **Ownership constraint:** a standalone run cannot safely overwrite an existing
  process-global executor owner. Keep standalone lifespan separate from embedded
  `create_server`; tests must model the distinction rather than reuse a pytest
  global owner as proof of initialization.
* **Required implementation seam:** retained coordinator tasks need explicit
  shutdown ownership, and both runtime YAML resolution paths need the same path
  authority. Omitting these is incomplete implementation, not optional polish.
* **Routine choices:** exact class/module/keyword name, a private context manager
  versus one small owner class, and using the existing 10-second drain default
  versus adding a documented CLI setting. None requires inventing Web sessions,
  a transport, a new executor, or another user decision about configured-root
  authorization.

## Acceptance and regression matrix

| Surface | Positive control | Negative/control evidence |
|---|---|---|
| Actual public stdio child, no pytest fixtures | Initialize; author real in-root CSV + valid sink; preview passes path check and reaches real plugin/graph checks; normal EOF exits cleanly | Existing absent-registration error cannot occur; child completes with no lingering worker/watchdog; no provider network call |
| All eight live path writers | In-root relative/absolute values, merged encoding-only source patch, nonexistent contained output | Outside absolute, traversal, symlink escape; typed exact component refusal; entire private state/hash/version and saved bytes unchanged |
| Bulk replacement | Singular and plural file sources; non-file null source control | Earlier valid component plus later bad source/output/provider refuses atomically; no prepared blob writes |
| Malformed/missing authority | Explicit root, nullable supported provider value | Embedded missing root refuses every file writer; int/list/null/empty/NUL handled intentionally; wrong owned type/mixed Web custody raises owned error |
| Preview and export | Same root resolves admission, executable YAML and identity YAML; export redacted, private state intact | Inject/load legacy escaped path and independently prove `path_allowlist` refusal, without weakening admission |
| Web tenancy | Real session A source and output/provider paths | Deployment root/shared subtree, session B, missing session, traversal/symlink escape remain refused |
| Actual worker lifecycle | Healthy worker executes; EOF joins it; caller timeout retains actual admission and deduplicated work | Cancel during worker/cleanup; partial construction failure; transport error; actual submit/quarantine failure and drain expiry; failures retained, no early completion witness |
| Recovery ownership | Production Linux child uses actual watchdog; unit fixtures may record exact owner events | No-op callback cannot satisfy tests; no global reset/owner overwrite; hung-worker child is externally bounded and verifies real escalation rather than hanging pytest |

Use subprocess acceptance for lifecycle so `explicit_required_executor_recovery`
cannot mask missing initialization. Unit-inject the existing nominal test
watchdog for deterministic fault ordering, and retain at least one real public
stdio happy-path child plus a real recovery child where applicable. In tests,
the owner-factory seam should be explicit and unavailable as tool input. Run
the path/preflight/server focused suites and affected whole-tree AST/lint/type
gates; changes to shared coordinator/executor semantics can require the full
Python suite under repository policy. This review executed no tests and claims
no passing gate.
