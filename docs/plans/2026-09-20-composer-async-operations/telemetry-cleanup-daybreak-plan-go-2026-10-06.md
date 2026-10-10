# Actual Daybreak telemetry plan GO — round 3

Actual model: `gpt-daybreak-blue-latest`, provider `openai`, native read-only CLI. Session `01a11203-e950-7323-9c04-2ef9535d98bb`; completed exit `0`; **renewed narrow PLAN GO**.

All 30 immutable technical input hashes were verified after completion. B1–B9 are resolved at design level. This verdict clears the required narrow design gate under the existing implementation workflow; it grants no source, executed-proof, frozen-tree, merge or deployment clearance. The exact reviewed v5 proposal is preserved alongside this complete verdict. All mandatory future controls below remain obligations. Complete raw review, native log and captured SDK/source files remain in the task evidence workspace. Source/SDK line references identify that immutable snapshot, not later mutable source. Only snapshot link prefixes and the versioned proposal filename are normalized. Prior NO-GO verdicts and all proposals remain preserved.

# Telemetry cleanup v5 review: NARROW PLAN GO

**Verdict: PLAN GO for implementation planning only.**

The repaired v5 proposal resolves B9 and preserves the substantive resolutions of B1–B8. I found no remaining viable-design blocker within the immutable snapshot.

This is not executed proof, implementation authorization, final source clearance, deployment clearance, or evidence that the current telemetry implementation satisfies the proposal. Telemetry implementation remains held pending the mandatory proofs below.

The plan-review skill normally uses four subagents and writes a report. The review constraints prohibited nested models and writes, so I applied the reality, architecture, quality, and systems lenses directly.

## Blocker dispositions

| Finding | Disposition | Basis |
|---|---|---|
| B1 — unreachable cleanup owner | **Resolved** | V5 uses a preallocated lexical `telemetry_owner`, reachable directly from `create_app()`’s synchronous `BaseException` handler. It does not expand the three-kind `ApplicationFinalizerKind` set. The required order is explicit: retain primary → publish failed recovery → arm `FAILED_STARTUP` → telemetry cleanup → independent auth/session cleanup. See [the v5 owner design](<telemetry-cleanup-design-v5-2026-10-06.md:5>) and [current constructor catch](<../../../src/elspeth/web/app.py:1342>). |
| B2 — Prometheus registry contradiction | **Resolved at design level; parity proof mandatory** | The installed reader hardcodes module `REGISTRY` in both construction and shutdown. V5 therefore explicitly owns the minimal initializer, retains the exact registry and collector, binds the collector to the inherited final `collect`, and unregisters only from the retained registry. This is a viable solution for both the production default registry and dedicated test registries. See [installed reader](<sdk/installed-prometheus-reader-source.txt:1>) and [v5 registry design](<telemetry-cleanup-design-v5-2026-10-06.md:51>). |
| B3 — provider weak-set/callback ownership | **Resolved subject to mandatory identity proofs** | V5 requires exact referent enumeration using `is`, not equality-based weak-set membership; exact superclass lock/set objects; no subclass class-attribute shadowing; pre-absence and `_collect is None`; exact retained consumer callback identity; and no reader escape before initialization. Under those invariants, post-membership with the exact owned callback—or the known insertion-before-callback prefix—proves this constructor’s acquisition. Unexpected hashing, equality, callback, lock, set, tuple, or concurrency evidence remains `UNRESOLVED`. |
| B4 — `BaseException` skips later readers | **Resolved** | The owner sweeps every reader receipt after provider return or throw, continues after every `BaseException`, and invokes only resources not already reached. SDK aggregate exceptions remain associated diagnostics; originals are not reconstructed from messages. |
| B5 — exporter ABI and partial ownership | **Resolved with the declared unsupported boundary** | The wrapper accepts the periodic reader’s `timeout=` ABI and calls the installed raw exporter with `timeout_millis=`. Exact raw exceptions are retained before logging and re-raised. The inaccessible object inside a throwing raw-exporter constructor correctly remains `UNRESOLVED`, with `FAILED_STARTUP` armed and no invented cleanup, timeout, or resource-free inference. |
| B6 — partial global installation | **Resolved** | Getter verification occurs after setter return and throw. The stage machine distinguishes exact, foreign, no-op, attempted-not-installed, and getter-unknown outcomes. ACTIVE publication requires exact identity. Exact install followed by failure becomes a permanent failed/CLOSED record; foreign providers are never closed. |
| B7 — process lifetime and test reset | **Resolved at plan level** | V5 separates permanent production ownership from nominal, token-bound replaceable test domains. Same-owner bootstrap is idempotent; foreign owners allocate nothing; production CLOSED/UNRESOLVED state cannot reopen. Test reset requires exact provider/getter/token identity and a successful cleanup witness and cannot clear a real global provider. The supplied caller inventory’s syntax limits are acknowledged rather than treated as unrestricted Python proof. |
| B8 — periodic-thread custody | **Resolved** | V5 names and validates the actual installed fields, preserves the exact thread and event from the preallocated object, and requires synchronous physical joining under the already armed external watchdog. Missing or malformed fields never mean “no thread”; an indeterminate start remains `UNRESOLVED`. SDK bounded shutdown alone cannot establish physical completion. PID checks precede inherited locks, and post-bootstrap fork is explicitly unsupported. |
| B9 — SDK provider `atexit` authority | **Resolved** | V5 selects `shutdown_on_exit=False` as a required nominal ABI at bootstrap, production/test factory, and actual superclass call. The inspected SDK accepts this argument, initializes `_atexit_handler` to `None`, and registers only on the true branch. V5 retains the flag receipt before the superclass call and requires `_atexit_handler is None` after both return and throw. Missing, malformed, or non-`None` evidence remains `UNRESOLVED`; no arbitrary handler is unregistered. See [installed provider ABI](<sdk/installed-metric-reader-provider-source.txt:391>) and [v5 B9 contract](<telemetry-cleanup-design-v5-2026-10-06.md:83>). |

One factual refinement: the current production helper in this snapshot already passes `shutdown_on_exit=False` at [operator_telemetry.py](<../../../src/elspeth/web/operator_telemetry.py:141>). That alone did not resolve B9 because the existing factory seam is structural and does not retain forwarding or post-construction handler evidence. V5 resolves that gap with the mandatory nominal ABI and receipts.

## Four-lens assessment

### Reality

The installed SDK facts support the design:

- `opentelemetry-sdk` is locked at 1.40.0; the Prometheus exporter is 0.61b0.
- `MetricReader.collect` and `_set_collect_callback` are final.
- Provider registration inserts into the class weak set before installing the callback.
- Provider shutdown catches only `Exception`, validating the need for the owner’s `BaseException` sweep.
- Periodic shutdown performs a bounded join and then invokes the exporter using `timeout=`.
- Provider `_atexit_handler` starts as `None` and registration is conditional on `shutdown_on_exit`.
- The metadata range remains broader than the inspected implementation, so the proposed exact installed-version-and-shape guard is mandatory. No dependency edit is needed.

V5 preserves the actual reader instances through provider registration and does not introduce a proxy that would change metric, consumer, hash, callback, or collector identity.

### Architecture

The one-owner, one-serving-process boundary is viable for the maintained startup paths:

- CLI uses a Uvicorn factory string and no worker/preload parameter.
- Development reload is optional and defaults false; the factory executes in the serving process.
- Acceptance serving constructs the app in the process passed to Uvicorn.
- Compose, Azure Container Apps, and ECS select the same CLI path; maintained container configuration uses one web process.
- The ECS helper completes before `exec` of the web command.

The alias-aware inventory reports 184 `create_app` sites, 20 bootstrap sites, three external factory-constructor sites plus production construction, and nine reset sites. Its declared boundary—module imports, module aliases, simple assignment aliases, and factory strings—is acceptable for plan approval because v5 explicitly requires the canonical inventory to be rerun and unresolved shadowing/dynamic cases reviewed before source clearance.

The design correctly keeps telemetry cleanup outside the existing three-kind shared finalizer authority and does not add a pool, default owner/callback, bootstrap shortcut, endpoint, or product-scope change.

### Quality

The proposal now has a complete proof strategy, but none of it has been executed. Source clearance requires controls demonstrating:

- exact nominal production and test provider ABI;
- explicit false forwarding through every layer;
- zero SDK `atexit` registrations;
- exact `None` handler evidence;
- exact registry/collector membership and unrelated-collector preservation;
- SDK behavior parity for metrics, callbacks, aggregation, temporality, collection and `force_flush`;
- every provider-constructor prefix and callback fault;
- exact original exception identities and deterministic provenance;
- once-only shutdown across provider, readers, collector, wrapper, and exporter;
- actual periodic-thread termination;
- production/test ownership and reset isolation;
- maintained deployment and caller inventory closure.

### Systems

The lifecycle rules are coherent:

- `FAILED_STARTUP` is armed before any blocking telemetry or later finalizer.
- Successful failed-startup cleanup never emits `COMPLETE`.
- Normal `COMPLETE` is permitted only after every semantic and physical obligation succeeds.
- A live periodic thread, unregister failure, exporter/provider failure, unknown handler, malformed private shape, unresolved global installation, or inaccessible partial resource prevents completion.
- Concurrent cleanup observing `RUNNING` cannot receive a completion witness or start a second cleanup.
- PID mismatch is rejected before inherited locks and cannot reuse the parent owner, touch parent recovery state, create a replacement worker, or emit `COMPLETE`.

## Mandatory future proof controls

Positive controls must establish:

- exact lexical-owner reachability at every bootstrap throw boundary;
- same-owner identity idempotence and zero-allocation foreign-owner refusal;
- explicit `False` receipt at bootstrap, nominal factory, and superclass;
- `_atexit_handler is None` and zero observed SDK registrations;
- exact superclass weak-set/lock resolution with no subclass shadowing;
- referent-is membership, exact reader tuple, and exact callback `__self__`/`__func__`;
- exact retained Prometheus registry/collector registration and removal;
- later-reader cleanup after `SystemExit`, `KeyboardInterrupt`, or cancellation;
- actual thread death after final join;
- setter/getter exact identity on both return and throw;
- normal and failed-owner shutdown parity;
- exact exception-object preservation and acquisition-order provenance.

Negative controls must establish:

- missing/wrong/non-`None` handler evidence refuses ACTIVE and completion;
- dropped, ignored, omitted, or true shutdown flag refuses admission;
- missing or malformed private fields never prove absence of acquisition;
- foreign weak-set membership/callbacks are untouched and block success;
- wrong Prometheus registry or unregister failure prevents completion;
- inaccessible throwing exporter construction remains `UNRESOLVED`;
- setter no-op, foreign install, install-then-throw, and getter failure cannot publish ACTIVE;
- production ACTIVE/CLOSED/FAILED/UNRESOLVED records cannot be reset;
- PID mismatch cannot borrow the parent witness;
- failed startup never emits `COMPLETE`;
- `RUNNING` cleanup cannot return a witness.

Mutation controls must fail if implementation:

- removes any explicit `shutdown_on_exit=False` hop;
- allows a nominal factory to ignore the flag;
- accepts an absent handler field as equivalent to `None`;
- shadows the SDK weak set or lock;
- replaces referent identity with equality membership;
- accepts a structurally similar callback;
- restores module-global Prometheus shutdown;
- removes getter verification;
- treats the SDK timed join as completion or omits `is_alive()`;
- reconstructs failures from SDK aggregate text;
- swallows exporter, provider, registry, handler, or cleanup failures;
- invokes transferred ownership twice;
- lets one `BaseException` terminate the sweep;
- clears permanent production ownership;
- emits `COMPLETE` during failed startup;
- accepts a maintained preload/post-factory-fork command.

## Final disposition

**Renewed narrow PLAN GO. No open design blocker remains.**

B9 is resolved by exclusive application-owned provider shutdown with mandatory `shutdown_on_exit=False`, exact forwarding receipts, and present-`None` handler evidence. B1–B8 remain resolved under their stated identity, compatibility, lifecycle, and test obligations.

The inaccessible throwing-exporter case, malformed/unknown partial SDK custody, and post-bootstrap application fork remain deliberately `UNRESOLVED` outcomes, not successful paths and not blockers to implementing the bounded design.

No source was changed, no tests or provider/runtime calls were made, and no final source clearance is granted.
