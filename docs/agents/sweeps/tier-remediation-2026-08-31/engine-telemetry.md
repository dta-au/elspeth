# Tier remediation — lane engine-telemetry (ticket elspeth-3f3278f3c5)

Dispositions for the 19 sign-2026-08-30-w1 blocked findings assigned to this
lane. All adjudicated against the live tree (the bundle's judge rationales
arrived scrambled across keys — several describe sites not in this lane's
files — so every disposition below was re-derived from code). Line numbers
cite the tree as of the last commit listed below.

Counts: **9 fixed, 9 restage, 1 abandoned.**

Commits (all on `lane/tier-rem-engine-telemetry`):

| sha | scope |
| --- | ----- |
| 1b02b7b1f | nominal BatchTransformRuntime dispatch (transform.py, contracts, mixin, tests) |
| 5e3a1125b | sink.py canonical render + bulk-begin Protocol removal |
| 3b12c46b9 | sink_effects.py publication_kind required read |
| f5350fd66 | audit_export_effects.py propagation-only containment |
| 937b4bf70 | follower.py typed depart containment + tests |
| 961ac5b5d | preflight.py required_input_kind recheck deleted |
| 5146e5977 | resume.py recorded degradation + tests |
| 54b7cda2e | processor.py retry classification + PluginRetryableError contract + tests |
| 6dd3cc8ba | telemetry/manager.py transport-loss accounting + tests |

Scoped suite (`tests/unit/engine tests/unit/telemetry tests/unit/tui`, -n 4):
**3480 passed, 1 skipped (pre-existing ddtrace skip), exit 0.**

---

## 1. engine/executors/sink.py:R4:SinkExecutor:_record_boundary_failure_operation:fp=5982ddf7ab994f17

**fixed** — 5e3a1125b. The inline `try: scrub_text_for_audit(str(violation))
/ except BaseException` duplicated `core.operations._render_exception` (the
adjacent comment already called it a mirror of track_operation's renderer).
The site now calls the canonical chokepoint, which performs the identical
scrub with the identical honest type-name degradation; the broad except no
longer exists at this site.

## 2. engine/executors/sink.py:R5:SinkExecutor:_open_primary_states:fp=8b90998d7414d8bc

**fixed** — 5e3a1125b. `_BulkBeginNodeStateRepository` was a
`@runtime_checkable` Protocol probed with `isinstance` against the
first-party `ExecutionRepository` — the structural dispatch ADR-032 forbids,
and dead besides: `ExecutionRepository.begin_node_states_many`
(core/landscape/execution_repository.py:285) is part of the concrete
contract. Protocol deleted; the bulk path is gated only on the
resume-provenance condition (fresh tokens bulk, resumed tokens take the
singular API that carries attempt/checkpoint provenance). The signed
allowlist entry (config/cicd/enforce_tier_model/engine.yaml) is now stale —
drop it; nothing fires at this site.

## 3. engine/executors/sink_effects.py:R1:SinkEffectCoordinator:_predecessor_descriptor:fp=495900afa46e5bf8

**fixed** — 3b12c46b9. For a finalized predecessor with
`publication_performed=False`, the plan is a NO_PUBLICATION plan and
`SinkEffectPlan.__post_init__` (contracts/sink_effects.py:1307-1313) plus
`_finalize_no_publication` (sink_effects.py:1438-1440) guarantee
`publication_kind` is present — so `.get(...) != "reaffirmed"` asserted an
absent required fact as "not reaffirmed" and silently walked past corruption.
Now a required read whose KeyError reifies as
`LandscapeRecordError("finalized no-publication sink effect predecessor is
missing publication evidence")`, mirroring _finalize_no_publication's form.

## 4. engine/executors/sink_effects.py:R4:_SinkEffectLeaseHeartbeat:_run:fp=9fba59cd789e7a82

**restage.** Proposed rationale:

> sink_effects.py:168-171: the heartbeat thread's `except BaseException as
> exc` stores the failure (`self._error = exc`), latches `_failed_event`, and
> exits the loop immediately — record-and-relocate across a thread boundary,
> not discard. The stored exception is re-raised on the coordinating thread by
> `check_and_raise` (sink_effects.py:141-146), which `refresh_and_check`
> (sink_effects.py:148-157) invokes before AND after every synchronous lease
> heartbeat; the coordinator calls `refresh_and_check` after every observable
> adapter call (sink_effects.py:563, 599, 624, 627, 739, 805, 843, 1123, 1133),
> so a heartbeat failure surfaces at the next authority proof and cannot admit
> further durable writes. Breadth is required at a thread boundary: an
> uncaught exception in the daemon thread would die silently, leaving the
> lease unrefreshed while the main thread continues adapter I/O — the exact
> silent loss R4 exists to prevent. The thread does no further work after
> storing (immediate `return`).

## 5. engine/executors/sink_effects.py:R5:SinkEffectCoordinator:_latest_exact_member_result:fp=755208f37daeea8f

**restage.** Proposed rationale:

> sink_effects.py:875-877: nominal union dispatch over the owned result union
> returned by `decode_sink_effect_returned_result`
> (core/landscape/execution/sink_effect_attempt_results.py:88-91, returns
> `SinkEffectReturnedResult`). Both `isinstance` targets are concrete
> ELSPETH-owned dataclasses (`SinkEffectCommitResult`,
> `SinkEffectReconcileResult`); the reconcile arm additionally narrows on the
> owned enum `SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR`. Nothing
> is masked: attempts that match neither arm are skipped by the loop's
> declared search semantics, and exhaustion raises
> `LandscapeRecordError("finalized member set is missing an exact returned
> attempt")` (sink_effects.py:879) — fail-closed. This is the permitted
> nominal-isinstance-for-union-dispatch form under ADR-032.

## 6. engine/executors/state_guard.py:R6:stamped_node_state_id:fp=2339d2e8e97d0d1c

**restage** (existing entry is stale: the site moved to
state_guard.py:98-110 and the read was restructured from `vars(exc).get(...)`
to an explicit `in`-check with direct subscript). Proposed rationale:

> state_guard.py:100-103: `vars(exc)` raises TypeError only for exception
> types with no instance `__dict__` (`__slots__`); the narrow
> `except TypeError: return None` returns the honest "unstamped" signal that
> the stamping contract (state_guard.py:87-95) documents: an unstampable
> exception is deliberately left unstamped so the consumer's
> state_id-required invariant fails loudly instead of mis-attributing an
> audit record (`record_transform_error_with_routing`, transform.py, fails
> closed on a missing state id — the control the accepted R7 entry for
> `stamp_node_state_id` already cites). The remaining reads are direct
> membership + subscript; a non-str stamp value returns None because the
> attribute is read off a foreign exception object (plugin-raised exceptions
> can carry arbitrary attrs), and an unstamped result fails loudly downstream
> rather than fabricating attribution. No Landscape write failure is caught
> here.

## 7. engine/executors/transform.py:R5:TransformExecutor:execute_transform:fp=51685afaede58730

**fixed** — 1b02b7b1f — with a restageable residual.
`BatchTransformRuntimeProtocol` was a `@runtime_checkable` Protocol used as
the batch-path dispatch control (the forbidden structural form). It is now
the nominal concrete class `BatchTransformRuntime`
(contracts/batch_runtime.py), inherited by `BatchTransformMixin`
(plugins/infrastructure/batching/mixin.py:42); a structural impostor no
longer passes (pinned by
`test_batch_transform_mixin_is_nominal_runtime_subclass`). The `isinstance`
at transform.py:757 still fires R5 syntactically; proposed rationale for the
residual:

> transform.py:755-758: nominal opt-in union dispatch (ADR-032's permitted
> form) against the concrete owned class `BatchTransformRuntime`
> (contracts/batch_runtime.py) — a transform participates in the
> row-pipelined batch runtime by inheriting it via `BatchTransformMixin`,
> never by structural shape;
> tests/unit/engine/test_executors.py::TestTransformExecutorBatchPath::test_batch_transform_mixin_is_nominal_runtime_subclass
> pins that an impostor implementing every member without inheriting is
> refused. The else-arm is the ordinary single-row path; nothing is coerced
> or masked.

## 8. engine/orchestrator/audit_export_effects.py:R4:_contain_cleanup_failure:fp=db4e685915f5912d

**fixed** — f5350fd66 — with a restageable residual. The genuine violation
was the call topology, not the helper: the `finally` arm ran
`_contain_cleanup_failure(spool.close, ...)` on the SUCCESS path, so a close
failure could be swallowed behind a successful return. Containment now runs
only inside `except BaseException` branches (audit_export_effects.py:365,
429-434) while a primary exception is propagating, and the success path
closes the spool directly so its failure surfaces; the helper's docstring
now states the propagation-only contract. The helper's `except Exception` at
audit_export_effects.py:81 still fires R4; proposed rationale for the
residual:

> audit_export_effects.py:78-83: `_contain_cleanup_failure` is called only
> from `except BaseException` branches (:365, :429-434, verified by grep)
> while the primary export/cancellation exception is propagating; its
> docstring forbids success-path use and the success path closes the spool
> uncontained (:437-441). The broad catch records the secondary cleanup
> failure (`logger.exception`) and preserves the primary exception as the
> outcome — the same secondary-failure-during-propagation form already
> accepted for `SinkExecutor._best_effort_cleanup` (both call sites
> immediately followed by bare `raise`). KeyboardInterrupt/SystemExit raised
> by the cleanup itself still propagate (Exception, not BaseException).

## 9. engine/orchestrator/cleanup.py:R4:_safe_cleanup_error_text:fp=f678cf61a66db302

**restage** (code unchanged; entry blocked as drift repair). Proposed
rationale:

> cleanup.py:66-70: the broad except wraps exactly one operation —
> `str(error)` on a plugin-supplied exception whose `__str__` ELSPETH does
> not control — and substitutes the honest marker
> `f"<unrepresentable {type(error).__name__}>"`, recording absence rather
> than fabricating content. The caller `record_cleanup_error`
> (cleanup.py:137-150) logs and folds the scrubbed text, digest, and raw
> length into `cleanup_errors`, which `cleanup_plugins` raises as an
> aggregated RuntimeError after teardown when no exception is already
> propagating. Tier-1 integrity is preserved upstream: `run_hook` re-raises
> `contract_errors.TIER_1_ERRORS` (cleanup.py:162-163) before its
> plugin-boundary catch. Suppression is valid only while the catch remains
> limited to rendering unrepresentable plugin exception text.

## 10. engine/orchestrator/cleanup.py:R4:cleanup_plugins:run_hook:fp=c96620c8a90878cb

**restage** (code unchanged; entry blocked as drift repair). Proposed
rationale:

> cleanup.py:160-165: best-effort plugin lifecycle teardown in the
> policy-prescribed shape. The dedicated
> `except contract_errors.TIER_1_ERRORS: raise` clause (:162-163) precedes
> the broad catch, so framework/audit corruption crashes; only then does
> `except Exception as exc:` (:164) record the per-plugin hook failure via
> `record_cleanup_error` (WARNING log + `cleanup_errors` aggregation), so
> every plugin still gets its cleanup attempt. The collected failures are
> surfaced as the function's declared failure result: `cleanup_plugins`
> raises the aggregated RuntimeError after all hooks finish unless a
> `pending_exc` is already propagating, in which case the failures are
> logged and the in-flight exception is preserved as primary — recorded and
> surfaced, never discarded.

## 11. engine/orchestrator/follower.py:R4:FollowerProcessor:_best_effort_depart:fp=14a94c6dae76c96b

**fixed** — 937b4bf70. The `except Exception` + DEBUG log silently discarded
every failure class. The catch is now typed to exactly the residual transient
classes the original entry itself identified —
`(OperationalError, IntegrityError)` from the cross-connection depart race —
recorded at WARNING; plugin bugs and Tier-1 audit errors propagate (pinned by
`TestBestEffortDepartContainment`: containment for both transient classes,
propagation for RuntimeError and AuditIntegrityError). The old R4 entry is
stale — drop it. The narrowed site now fires R6 at follower.py:479; proposed
rationale for the new key:

> follower.py:471-485: typed containment of exactly the transient DB classes
> (`sqlalchemy.exc.OperationalError`, `IntegrityError`) that can escape
> `depart_worker` (core/landscape/run_coordination_repository.py:842-867),
> whose single-transaction CAS + event insert cannot split-brain and whose
> benign already-departed race returns normally. The failure is recorded at
> WARNING with traceback; the depart is loss-tolerant by design —
> coordination events are project-designated best-effort and a worker whose
> depart did not persist is reclaimed by run lease expiry. Every other
> exception, including TIER_1_ERRORS members, propagates untouched (pinned
> by tests/unit/engine/orchestrator/test_follower_processor.py::TestBestEffortDepartContainment).

## 12. engine/orchestrator/preflight.py:R5:_validate_instance_extension_capabilities:fp=e2e7afcc2e462dbc

**restage.** The blocked rationale reportedly misclassified plugins as
Tier-3; the check itself is the prescribed offensive form. Proposed
rationale (no trust-tier misclaim):

> preflight.py:84-101: offensive nominal-capability admission checks against
> the concrete ELSPETH-owned opt-in classes `MemberSinkEffectCapability` and
> `RestagingSinkEffectCapability` (contracts/sink_effects.py:1421, 1445 —
> plain nominal classes, not Protocols). Plugins are system-owned; the
> isinstance does not re-validate their data, it detects the one defect this
> gate exists to refuse: a sink that nominally subclasses a capability marker
> while leaving the base's NotImplementedError methods unoverridden. Every
> arm RAISES `SinkEffectCapabilityError` — fail-closed refusal at the
> admission gate, no fallback, no coercion, nothing masked. This is nominal
> union dispatch plus loud crash, the ADR-032-prescribed handling for owned
> code whose declaration is defective.

## 13. engine/orchestrator/preflight.py:R5:sink_effect_modes_from_runtime_bindings:fp=21c4bfecca4eab61

**restage.** Proposed rationale:

> preflight.py:374-378: the admission-once identity check that
> `SinkEffectContract`'s own contract prescribes
> (contracts/sink_effects.py:1412-1418: "Admission uses this identity once,
> then trusts the declared contract directly"). This function is the
> authoritative post-secret-expansion admission gate (called from cli.py,
> web/execution/preflight.py, engine/orchestrator/export.py before sink
> execution); the raw-config gate in runtime_factory deliberately `continue`s
> past sinks whose secret-bearing options cannot be expanded yet
> (plugins/infrastructure/runtime_factory.py:398-405), so this is the first
> guaranteed contract-participation check on the instantiated sink. It
> raises `SinkEffectCapabilityError` (fail-closed operator-facing refusal of
> a sink type that does not participate in the recoverable-effect protocol)
> and admits nothing on mismatch. After this single identity use the
> function trusts declarations directly (`cast` + direct attribute reads),
> exactly as the contract prescribes.

## 14. engine/orchestrator/preflight.py:R5:validate_sink_effect_type_capability:fp=e5ed047089792a73

**abandoned** — 961ac5b5d. `required_input_kind` is first-party
fixed-contract data: the sole caller constructs it from `SinkEffectInputKind`
members (plugins/infrastructure/runtime_factory.py:376, 384 → :415-419).
Re-validating a declared parameter's own annotation is the defensive recheck
ADR-032 prohibits for owned data (the judge rejected the suppression on this
ground). The two-line isinstance-and-raise was deleted outright; no test
pinned it (grep: no test references "exact SinkEffectInputKind" for this
site).

## 15. engine/orchestrator/resume.py:R6:_derive_resume_failure_counter_baseline:fp=5bee3d44d5b0a4eb

**fixed + restage** — 5146e5977 (the old entry is stale; re-stage with the
updated rationale). The narrow OperationalError arm returned the documented
None sentinel with nothing recording the degradation; it now logs at WARNING
before returning (logging is the permitted channel here — the audit read
itself failed). Proposed rationale:

> resume.py:488-505: catches only `sqlalchemy.exc.OperationalError` —
> transient DB contention while reading the audit-cumulative baseline for
> FAILED-ceremony enrichment. The failure is recorded (WARNING with
> traceback, resume.py:498-503) and surfaced as the function's declared
> failure result: `ExecutionCounters | None`, where None is the documented
> "partial-only" sentinel the caller branches on
> (`_resume_failure_result_from_baseline`, resume.py:459-463). Corruption
> and invariant signals (`AuditIntegrityError`,
> `OrchestrationInvariantError`) are not caught and propagate — pinned by
> tests/unit/engine/orchestrator/test_resume_failure.py::TestResumeFailureCounterBaselineDegradation
> (all three arms).

## 16. engine/processor.py:R5:RowProcessor:_execute_transform_with_retry:is_retryable:fp=116b46da62812e6a

**fixed** — 54b7cda2e — with a restageable residual. Bare `OSError` in the
retryable tuple reclassified plugin bug-classes (`FileNotFoundError`,
`PermissionError`) as transient transport failures — the real bug-class
behind the rejection. Both the `is_retryable` classifier (processor.py:2388)
and the lockstep no-retry conversion arm (processor.py:2332) now name only
`ConnectionError | TimeoutError | CapacityError`; every other unclassified
exception crashes as Plugin Ownership requires.
`PluginRetryableError`'s docstring (contracts/errors.py:1498-1512) now
documents the deliberate engine-classified carve-out so contract and code
agree. Pins: existing assertions keep ConnectionError/TimeoutError/
CapacityError retryable; new assertions pin FileNotFoundError and
PermissionError as NOT retryable. Proposed rationale for the residual R5 at
processor.py:2388:

> processor.py:2378-2388: retry-eligibility classification over exception
> TYPES — semantically an except-clause tuple expressed as the predicate
> `RetryManager.execute_with_retry` requires. The final arm names only the
> engine-classified transport carve-out that `PluginRetryableError`'s
> contract documents (contracts/errors.py:1504-1512): `ConnectionError` and
> `TimeoutError`, the runtime's canonical transient network signals that
> surface from beneath provider SDKs with no plugin seam to classify them
> (ratified by tests/unit/engine/test_processor.py, is_retryable pins), plus
> the contract-owned `CapacityError` whose `retryable` attribute is always
> True (contracts/errors.py:2186-2204). Bare OSError is deliberately
> excluded and pinned non-retryable. Nothing is coerced; a non-retryable
> exception propagates or is converted to the declared row-scoped error
> result by the caller's typed arms.

## 17. telemetry/manager.py:R4:TelemetryManager:_export_loop:fp=6647cee4585b4dfb

**fixed + restage** — 6dd3cc8ba. The signed entry described the pre-refactor
single `except Exception`; the loop now has three typed arms, so the entry is
stale (drop/re-stage). The genuine defect in the current code — the
`TELEMETRY_TRANSPORT_ERRORS` arm logging and silently discarding the event —
is fixed: the loss now lands in `events_dropped` under `_dropped_lock` (the
module's declared loss accounting, surfaced by `health_metrics`), pinned by
`TestExportLoopTransportFailureAccounting`. Proposed rationales for the
current sites:

- R4 catch-all, manager.py:250-254:
  > The broad catch is the thread-boundary relocation arm: an unanticipated
  > programming error in the non-daemon export thread cannot propagate to the
  > main thread on its own, so it is stored (`self._stored_exception = e`)
  > and the loop breaks — processing stops on integrity compromise, and
  > `flush()` re-raises the stored exception on the main thread. Narrowing
  > would let an unexpected error class vanish silently in the background
  > thread; this arm exists to prevent exactly that.
- R6 `except TelemetryExporterError`, manager.py:234-237:
  > Typed park-and-re-raise: the fail-on-total signal raised by
  > `_dispatch_to_exporters` is stored for `flush()` to re-raise on the main
  > thread; the loop continues consuming so backpressure cannot deadlock
  > producers. Recorded (ERROR log + stored exception), surfaced at the next
  > synchronization point — not discarded.
- R6 `except TELEMETRY_TRANSPORT_ERRORS`, manager.py:238-249:
  > Transport failure escaping per-exporter isolation: the event's loss is
  > counted into `events_dropped` under `_dropped_lock` and logged at ERROR
  > (recorded loss in the declared health_metrics surface, pinned by
  > tests/unit/telemetry/test_manager.py::TestExportLoopTransportFailureAccounting),
  > and the loop keeps consuming — telemetry is the loss-tolerant channel and
  > the audit record has already been written by the time an event reaches
  > this thread (audit primacy).

## 18. tui/widgets/lineage_tree.py:R5:LineageTree:__init__:fp=133227fb4863b2a7

**restage.** Proposed rationale:

> lineage_tree.py:67: nominal union dispatch over the constructor's declared
> owned union `LineageData | TuiLineageView`. `LineageData` is a TypedDict
> (tui/types.py:78) and cannot be isinstance-checked, so dispatching on the
> concrete owned class `TuiLineageView` (tui/lineage_view.py:58) is the only
> runtime discrimination the union admits; the else-arm is the other union
> member by construction. Both arms are contractually valid inputs; nothing
> foreign is parsed, nothing malformed is coerced or defaulted, and the
> TypedDict arm's later field reads are direct subscripts that crash on
> malformed Tier-1 data per the widget's documented contract
> (lineage_tree.py:55-58: "Missing or malformed fields will raise KeyError,
> not silently degrade").

## 19. tui/widgets/node_detail.py:R5:NodeDetailPanel:render_content:fp=c39d6a802a47f16f

**restage.** Proposed rationale:

> node_detail.py:285-288: a fail-closed nominal assertion on Tier-1 audit
> data, not a defensive fallback. The optional `artifact` value read from the
> node-state payload must be a dict per the schema contract; on violation the
> code RAISES `TypeError(... "audit integrity violation" ...)` naming the
> state's audit context — corruption crashes, exactly the Tier-1 rule. There
> is no else-arm, no default, no coercion; the validated dict then flows to
> `_validate_artifact` (node_detail.py:91), whose required-key reads crash on
> absence. R5 fires on the syntactic isinstance, but the site is the
> crash-on-corruption form the tier model prescribes.

---

## Shared-baseline changes needed

No masquerade-baseline or dynamic-attribute pinned-set updates are required:
no `getattr`/`hasattr` sites were added or removed, and the new test double
(`_StructuralImpostor` in tests/unit/engine/test_executors.py) exists only to
be asserted NOT to pass nominal dispatch. Wire-shape templates and output
bytes untouched.

Allowlist consequences for the hub (I did not touch any yaml):

1. **Drop (finding retired, site no longer fires):**
   - `engine/executors/sink.py:R5:SinkExecutor:_open_primary_states:fp=8b90998d7414d8bc`
   - `engine/orchestrator/follower.py:R4:FollowerProcessor:_best_effort_depart:fp=14a94c6dae76c96b`
     (superseded by the new R6 key below)
   - `telemetry/manager.py:R4:TelemetryManager:_export_loop:fp=6647cee4585b4dfb`
     (superseded by the three per-arm keys in finding 17)
2. **Re-stage stale entries whose sites my commits relocated (signed in the
   w1 bundle, honest churn from fixes in the same functions — prior accepted
   rationales remain accurate):**
   - `engine/executors/transform.py:R5:TransformExecutor:execute_transform:fp=1bc9803417b72a7b`
     (now the `isinstance(e, TimeoutError)` arm at transform.py:846; line
     shift + scope-fingerprint churn from commit 1b02b7b1f)
   - `engine/processor.py:R5:...:is_retryable:fp=18d893faf7871108` and
     `fp=352a9bcb7a23e240` (the InterruptedError/PluginRetryableError arms,
     now processor.py:2378/2380; churn from commit 54b7cda2e)
   - `telemetry/manager.py:R1:TelemetryManager:_export_loop:fp=38e937e9439cada6`
     (`queue.Queue.get`, not `dict.get` — prior false-positive rationale
     still exact; now manager.py:229; churn from commit 6dd3cc8ba)
   - `engine/executors/state_guard.py:R6:stamped_node_state_id:fp=2339d2e8e97d0d1c`
     (stale before this lane started; fresh rationale in finding 6)
   - `engine/orchestrator/resume.py:R6:_derive_resume_failure_counter_baseline:fp=5bee3d44d5b0a4eb`
     (updated rationale in finding 15)
3. **New keys to stage (rationale text above):** findings 4, 5, 7 (residual,
   transform.py:757), 8 (residual, audit_export_effects.py:81), 9, 10, 11
   (new R6, follower.py:479), 12, 13, 16 (residual, processor.py:2388), 17
   (three per-arm keys), 18, 19.

Out-of-scope residue noted for the hub (pre-existing raw findings in lane
files, NOT in this lane's worklist, untouched): telemetry/manager.py
queue.Full/queue.Empty R6 family (:485, :491, :531, :563, :755, :765) and
`except TELEMETRY_TRANSPORT_ERRORS` at :796; engine/executors/state_guard.py
R5 at :404 (NodeStateGuard.complete); the twin instance-level
`isinstance(sink, SinkEffectContract)` at preflight.py:176 inside
decorator-covered `validate_sink_effect_capability`.
