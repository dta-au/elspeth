# Tier remediation — lane web-periphery (65 findings)

Ticket elspeth-faf1ae7a4e, epic elspeth-e561df3c4e. Branch
`lane/tier-rem-web-periphery`. Scope: `src/elspeth/web/` outside
`composer/` and `sessions/`. Every finding key from the lane worklist is
listed; dispositions per DOCTRINE.md (fixed / restage / abandoned). All
adjudications were re-derived from the tree — several
`judge_rationale_best_effort` texts in the worklist were visibly mis-paired
with other sites and were used only where their content demonstrably
matched the code.

Commits (in order):
- `678085c98` acceptance probes record-and-report rework
- `39fa7b32b` auth optional-claim boundary forms
- `f8f690156` app.py decorator / schema-failure propagation / build identity
- `b2d2c44dc` blobs fork-copy drain outcomes
- `e74de4bd4` readiness rework
- `f5b1c36d5` structural boundaries + visible Tier-3 read forms
- `9330932a1` readiness single-flight registry membership form

Corpus effect (keyless shape-only lint, this lane's files, R_TB_SUPPRESSED
excluded): 59 raw findings before → 24 after; the 24 survivors are the 21
restages below plus 3 sites uncovered by fingerprint churn (see
"Shared-baseline changes needed"). `trust_boundary.tests` gate: exit 0.

Fixed: 40 · Restage: 21 · Abandoned: 4

---

## web/_aws_ecs_acceptance/ (22)

Shared mechanism (commit 678085c98): `contracts.py` gains
`CheckFailureRecord` (static check id + exception class name only),
`check_failure_error()`, and `close_failure()` — which records a close
fault and, when an exception is already in flight, attaches it to that
exception as a PEP 678 note so teardown faults survive unwinding. All
tokens/class names respect the module's redaction discipline (no message
text, no values).

1. `web/_aws_ecs_acceptance/bedrock.py:R4:run_bedrock_guardrails_live:fp=b7fa27fc50d3c1cd` — **fixed** (678085c98).
   The `except AcceptanceCheckError` / `except Exception` handlers no longer
   assign-and-continue: both record the FAILED node state / token outcome /
   run status and end in `raise` (the generic arm raises
   `check_error_with_cause("guardrails_live_check", exc)` carrying the cause
   class, `from None` per redaction discipline).
2. `web/_aws_ecs_acceptance/bedrock.py:R4:run_bedrock_guardrails_live:fp=e6d59c0490701433` — **fixed** (678085c98). Same rework, second handler.
3. `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:fp=2facb0d4e0c96d87` — **fixed** (678085c98).
4. `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:fp=9c864c1e81c1ed59` — **fixed** (678085c98).
5. `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:restore:fp=1b90a1babb61660f` — **fixed** (678085c98).
6. `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:restore:fp=499fc99b3cdf51d9` — **fixed** (678085c98).
7. `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:restore:fp=51f6c0826d025075` — **fixed** (678085c98).
   3–7: every `contextlib.suppress` in `_suppress_process_output` is gone.
   Flush/rebind/close steps run through `_fd_step_failure` (typed
   OSError/ValueError → static `step:ExcClass` token); collected problems
   raise `AcceptanceCheckError("bedrock_output_boundary", cause_fields=…)`
   on the success path or are attached to the in-flight exception as a
   note. The judge's core complaint — fd 2 silently left on /dev/null —
   can no longer happen silently on any path.
8. `web/_aws_ecs_acceptance/capture.py:R5:provision_storage:fp=3f30bb90037471c1` — **abandoned** (678085c98).
9. `web/_aws_ecs_acceptance/capture.py:R5:provision_storage:fp=874ff7fbd140a1b9` — **abandoned** (678085c98).
   8–9: the `isinstance(data_dir, Path) or isinstance(payload_root, Path)`
   re-check was deleted outright. Both values are Tier-1/2 owned:
   `WebSettings.data_dir` is a declared `Path` and
   `get_payload_store_path()` returns `Path` on every branch (web/config.py);
   a contract breach must crash, not be laundered into a
   `storage_settings` verdict (ADR-032).
10. `web/_aws_ecs_acceptance/contracts.py:R7:check_error_with_cause:fp=2e45978d3c1d8cde` — **fixed** (678085c98).
    The `contextlib.suppress(Exception)` around third-party introspection
    (`str(exc)`, `errors()`) became a typed try/except whose failure path
    returns the envelope with the static `<cause-introspection-failed>`
    token in `cause_fields` — degradation is now operator-visible instead
    of silently dropped enrichment. Boundary `invariant` text updated to
    match. Pinning test updated
    (`test_check_error_with_cause_survives_an_exception_whose_str_raises`).
11. `web/_aws_ecs_acceptance/operator_telemetry.py:R4:AWSOperatorMetricEmitter:emit_web_metric:fp=a29d2279ab4a0337` — **fixed** (678085c98).
    `except Exception: return False` narrowed to
    `_METRIC_DELIVERY_ERRORS = (MetricsTimeoutError, *TELEMETRY_TRANSPORT_ERRORS)`
    around `force_flush` only; the meter/counter path is outside the try, so
    a first-party defect crashes instead of masquerading as collector
    degradation (the judge's outage-misclassification complaint). Each
    delivery failure is recorded as a `CheckFailureRecord` in
    `self._failures` (exposed via `delivery_failures`).
12. `web/_aws_ecs_acceptance/operator_telemetry.py:R4:verify_operator_telemetry_live:fp=8b264672c7acd27e` — **fixed** (678085c98).
    The finally-loop close handling now uses `close_failure`: on the
    unwind path the fault is attached to the propagating exception as a
    note (previously dropped whenever `sys.exc_info()` was non-None); on
    the normal path it still raises
    `OperatorTelemetryAcceptanceError("operator telemetry resource close failed")`.
13. `web/_aws_ecs_acceptance/orphan_sweep.py:R4:orphan_sweep:close_client:fp=18d8e862a31f45c0` — **fixed** (678085c98).
    `close_client` records `CheckFailureRecord`s into `failures` via
    `close_failure` (ExitStack unwind faults get the PEP 678 note); the
    terminal raise carries the first recorded cause class.
14. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=096100a2ea3f03c7` — **fixed** (678085c98).
15. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=0f09e2c26a55fa8b` — **fixed** (678085c98).
16. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=5c511445473f8e99` — **fixed** (678085c98).
17. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=7b522d86414263d9` — **fixed** (678085c98).
18. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=95e840c6ca6830ac` — **fixed** (678085c98).
19. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=e684046151112b9e` — **fixed** (678085c98).
20. `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=f4c905e0d101e85b` — **fixed** (678085c98).
    14–20: every broad handler in `verify_s3` (body steps, resource
    closes, cleanup client/delete/head/close) records a
    `CheckFailureRecord` with the caught exception's class; the flag-based
    priority ordering (cleanup > resource_close > body failure) is
    unchanged, and the three terminal raises go through
    `check_failure_error`, so the declared `AcceptanceCheckError` now
    carries `cause_class` instead of discarding the exception identity.
21. `web/_aws_ecs_acceptance/secure_documents.py:R6:_flock_retry_interrupted:fp=2d1dd10677ccec80` — **abandoned** (678085c98).
    Helper deleted; call sites use `_fcntl.flock` directly. CPython retries
    flock on EINTR internally (PEP 475) — verified empirically on this
    interpreter (5 interrupts delivered during a blocked flock, no
    InterruptedError). The retry loop was unreachable dead defensiveness,
    and worse: a signal handler deliberately raising InterruptedError would
    have been swallowed into an infinite retry. Replacement test
    `test_receipt_manifest_lock_surfaces_interrupted_flock_as_check_failure`
    pins the new mechanism (typed conversion at the lock boundary).
22. `web/_aws_ecs_acceptance/textract.py:R4:verify_textract:fp=c303b601bc382e2b` — **fixed** (678085c98).
    The finally/flag close became explicit both-path handling via
    `close_failure`: unwind path notes the propagating exception (the
    judge-identified drop), success path raises
    `AcceptanceCheckError("textract_resource_close", cause_class=…)`.

## web/app.py (5)

23. `web/app.py:R1:_BodySizeLimitMiddleware:dispatch:fp=4dba165a68d0c9dc` — **fixed** (f8f690156).
    Structural form: `@trust_boundary(tier=3, source_param="request",
    suppresses=("R1",), non_raising=True)` on `dispatch` (precedent:
    web/auth/routes.py Origin-header boundary). Lint confirms
    `R_TB_SUPPRESSED` at the `.get("content-length")` site. The
    drift-staled per-line entry should be dropped, not repaired.
24. `web/app.py:R6:_frontend_build_identity:fp=5e7d9852dfe80a66` — **fixed** (f8f690156).
    `except OSError: return None` collapsed genuine dev/test absence and
    real IO/permission failures into the same disarm. Now: explicit
    `is_file()` probe → None (absence disarms the beacon); a read failure
    on an existing index.html propagates and fails create_app.
25. `web/app.py:R6:_periodic_orphan_cleanup:fp=f2706c402e810c7b` — **restage** (after partial fix in f8f690156).
    The genuine-violation component is fixed: `SchemaCompatibilityError`
    was removed from the catch tuple (an incompatible Landscape schema is
    operator-actionable, not transient; it now terminates the task and
    surfaces at lifespan shutdown — pinned by the rewritten
    `test_schema_compatibility_failure_terminates_task_without_leaking_detail`,
    alongside the existing programmer-bug guardrail test). The remaining
    `except (SQLAlchemyError, OSError)` at web/app.py:345 is the honest
    per-line form. Proposed rationale:
    > R6, Tier-3 infra-fault tolerance in a supervised retry loop.
    > `_periodic_orphan_cleanup` (web/app.py:296) absorbs only
    > `(SQLAlchemyError, OSError)` around the orphan-cancel/Landscape
    > reconcile pass, records
    > `slog.error("periodic_orphan_cleanup_failed", exc_class=…)`
    > (exc_info deliberately omitted — SQLAlchemy chains carry DB URLs),
    > and retries the identical pass next interval — the loop IS the
    > declared recovery. First-party bug classes (AttributeError,
    > TypeError, AssertionError) and SchemaCompatibilityError are NOT
    > caught: they terminate the task, and `_service_lifespan` re-raises
    > the stored exception at shutdown (web/app.py:768-770 drains with
    > suppress narrowed to CancelledError only). Pinned by
    > TestPeriodicOrphanCleanup::test_programmer_bug_terminates_task_and_does_not_log
    > and ::test_schema_compatibility_failure_terminates_task_without_leaking_detail.
    > Invalidated if the catch widens or the retry loop stops re-running
    > the reconcile arm.
26. `web/app.py:R6:_service_lifespan:fp=16f660e310f0a064` — **restage** (after partial fix in f8f690156).
    (This fp and #27 are the two `_service_lifespan` R6 keys; per-site
    attribution between them is by position, the pair covers exactly the
    two surviving sites.) Startup orphan sweep, now
    `except (SQLAlchemyError, OSError)` at web/app.py:518 —
    SchemaCompatibilityError removed as in #25 (startup now fails on an
    incompatible schema instead of booting with unreconcilable audit
    state). Proposed rationale:
    > R6, Tier-3 infra-fault tolerance at startup. The catch absorbs only
    > `(SQLAlchemyError, OSError)` from the startup orphan sweep +
    > Landscape reconcile, records
    > `slog.error("lifespan_orphan_cleanup_failed", exc_class=…)`, and the
    > IDENTICAL pass re-runs with the reconcile arm every
    > `orphan_run_check_interval_seconds` in `_periodic_orphan_cleanup`
    > (started unconditionally at web/app.py:751 with the same
    > landscape_url) — a transient DB outage at boot degrades boundedly
    > instead of blocking service start. SchemaCompatibilityError and
    > first-party bug classes propagate and fail startup. The
    > custody-critical reconcile above it
    > (`reconcile_inline_custody_publications`, web/app.py:503) is
    > deliberately OUTSIDE the catch. Invalidated if the periodic task
    > stops re-running reconciliation or the tuple widens.
27. `web/app.py:R6:_service_lifespan:fp=6e2dcc1443ee8406` — **restage**.
    Composer boot probe `except TimeoutError` at web/app.py:708. Code
    unchanged — it is already the honest form. Proposed rationale:
    > R6, Tier-3 provider-probe timeout with a fully recorded declared
    > outcome. The bounded `asyncio.wait_for(probe_composer_config…,
    > _COMPOSER_BOOT_PROBE_TIMEOUT_SECONDS)` timeout sets
    > `probe_status="transient_failure"` and logs
    > `composer_boot_probe_transient_failure`; the `finally`
    > (web/app.py:726-730) stamps probe_status into the telemetry
    > attributes and emits `_COMPOSER_BOOT_CONFIG_COUNTER` +
    > `_COMPOSER_BOOT_CONFIG_PROBE_LATENCY`, so the timeout IS the probe's
    > recorded result, mirroring the provider-error `if not ok:` arm.
    > Config rejection (ComposerBootConfigError), cancellation, and every
    > other exception re-raise (probe_status rejected/cancelled/
    > local_error). Boot continues degraded by design: composer LLM calls
    > are exercised at first use. Invalidated if the timeout arm stops
    > recording to telemetry or begins swallowing other classes.

## web/auth/ (3)

28. `web/auth/entra.py:R1:EntraAuthProvider:authenticate:fp=564181fecfacb986` — **fixed** (39fa7b32b).
29. `web/auth/entra.py:R1:EntraAuthProvider:get_user_info:fp=9ef9844bc003576e` — **fixed** (39fa7b32b).
    28–29: `payload.get("preferred_username") or sub` →
    `optional_profile_claim(payload, "preferred_username") or sub` — the
    audited judge-free boundary helper (membership-then-subscript,
    absent/null/non-str/blank → None). Strictly better: a whitespace-only
    or non-string claim can no longer become the username. Stale allowlist
    entries → drop.
30. `web/auth/oidc.py:R1:JWKSTokenValidator:decode_token:fp=7f739b2cc0f07da8` — **fixed** (39fa7b32b).
    `header.get("kid")` → membership-then-subscript with the RFC 7515
    optionality comment; absent/unknown kid still fails closed via
    `_UnknownSigningKeyError`. Stale entry → drop.

## web/blobs/service.py (3)

31. `web/blobs/service.py:R6:_restore_staged_blob_deletion:fp=c7f88db745463cff` — **restage**.
    Code is right: the rollback-failure handler's whole job is to record
    the secondary fault on the PRIMARY exception (PEP 678 note at
    web/blobs/service.py:618-624, naming tombstone divergence and manual
    reconciliation) without masking it — raising here would replace the
    primary at every call site. The prior rationale lost because it
    omitted a caller and over-claimed `except BaseException`; the accurate
    control set is: all five callers pass the in-flight exception and
    re-raise it —
    `_stage_blob_deletion` web/blobs/service.py:602 (`except BaseException as primary_exc` + `raise`),
    web/blobs/service.py:2511, :2588, :3266 (service delete/fork paths),
    and `_execute_delete_blob` web/composer/tools/blobs.py:1885
    (`except Exception as primary_exc` + `raise` — narrower, but the
    restore call still precedes an unconditional re-raise). Proposed
    rationale should name exactly those five sites and the note text
    contract, and be invalidated if any caller stops re-raising
    primary_exc after restore.
32. `web/blobs/service.py:R7:_await_fork_copy_io_with_checkpoints:fp=b2d5736d395250f5` — **fixed** (b2d2c44dc).
33. `web/blobs/service.py:R7:_await_fork_copy_io_with_checkpoints:fp=cc7d384003aacbe7` — **fixed** (b2d2c44dc).
    32–33: all three `with suppress(BaseException): await operation_task`
    drains replaced by `_drained_worker_outcome` — an owned
    `_DrainOutcome` record; expected CancelledError retires quietly, any
    other worker-terminal exception's class is attached to the propagating
    primary as a PEP 678 note. Every handler still ends in `raise`
    (primary preserved on all paths).

## web/execution/ (15)

34. `web/execution/_validation_diagnostics.py:R1:_reframe_settings_missing_parts:fp=3b0d04bc6bf5a2cc` — **fixed** (f5b1c36d5).
35. `web/execution/_validation_diagnostics.py:R1:_reframe_settings_missing_parts:fp=e35f6ec9fc354cbc` — **fixed** (f5b1c36d5).
    34–35: `error.get("type")` / `error.get("loc") or ()` on pydantic
    ErrorDetails → membership-then-subscript visible form. The census pin
    (`test_validation_trust_tier.py::_ADJUDICATION_CANDIDATES`) was
    updated: the two R1 candidates are retired, the R5 shape probes remain
    declared.
36. `web/execution/completion_gates.py:R5:parse_completion_gates:fp=bc62c54d01a06de4` — **restage**.
37. `web/execution/completion_gates.py:R5:parse_completion_gates:fp=c73fbc7bae488a7b` — **restage**.
    36–37: the two `isinstance(…, Mapping)` checks at
    web/execution/completion_gates.py:254 and :264 are the ADR-032 parse
    point for the JSON-round-tripped `completion_gates` envelope read back
    off our own `composition_states.composer_meta` (callers:
    web/execution/service.py:1289, web/audit_readiness/service.py:555,
    web/sessions/routes/_helpers.py:875,
    web/shareable_reviews/service.py:386). Every malformed PRESENT value
    raises `ValueError("Tier 1: …")`; membership probes distinguish the
    two legal absences (pre-envelope rows; gate not withheld); the result
    is the owned `CompletionGateFacts`. The `@trust_boundary` decorator is
    NOT the right form here: the value is first-party persisted state, not
    external data rooted at a Tier-3 parameter (consistent with the
    signing judge's ruling on the guided_audit invocation-envelope case
    that a function receiving first-party persisted records does not take
    the decorator), and
    the function's own docstring carries the standing adjudication note.
    Proposed rationale: the above, plus the census declaration
    `_COMPLETION_GATE_ADJUDICATION_CANDIDATES` in
    tests/unit/web/execution/test_validation_trust_tier.py pinning exactly
    these two sites. Invalidated if the function stops raising on
    malformed present values or gains a non-composer_meta caller.
38. `web/execution/diagnostics.py:R6:_node_state_error_correlating_exception:fp=7a97b06c8a703e6c` — **restage**.
    `except (TypeError, ValueError): return None` around
    `_decode_json(error_json)` at web/execution/diagnostics.py:332.
    Proposed rationale:
    > R6, declared negative result of a correlation probe. The function's
    > contract (docstring) is "returns None for anything it cannot
    > positively correlate — an absent, malformed, or unrecognised
    > envelope"; None degrades attribution to scope level, the prior and
    > safe behaviour, while a false positive would name an unrelated node
    > in operator diagnostics. The typed catch covers only JSON decode of
    > the `operations.error_json` Text column; the Tier-3 envelope parse
    > lives in `_node_state_error_envelope`, and no audit datum is
    > fabricated — the diagnostics projection is read-only over already
    > persisted state. Invalidated if the return value ever feeds
    > anything other than best-effort attribution ranking.
39. `web/execution/routes.py:R5:_artifact_error_type:fp=5bbaf45aecf7e0f7` — **restage** (drift repair; code unchanged, rationale refreshed with symbols).
40. `web/execution/routes.py:R5:_artifact_error_type:fp=8be996d77169865c` — **restage**.
    39–40 proposed rationale (both sites in the helper at
    web/execution/routes.py:190-194):
    > R5 classifier over FastAPI `HTTPException.detail`, whose framework
    > contract permits plain-str payloads; a Mapping carrying a str
    > `error_type` is the only recognized envelope, everything else
    > classifies None. The controls are
    > `_verified_artifact_file_snapshot_from_candidates`
    > (web/execution/routes.py:434-466) and
    > `_verified_artifact_preview_head_from_candidates`
    > (web/execution/routes.py:469-501): only
    > "artifact_purged_or_moved" / "artifact_content_drift" continue the
    > candidate loop, and an unrecognized or malformed envelope reaches
    > the trailing bare `raise`, re-raising the original exception.
    > Not decorator-eligible: `exc` is a first-party exception object
    > classifying locally authored envelopes
    > (web/execution/routes.py:150-188), not Tier-3 external data.
    > Invalidated if either caller stops re-raising on unrecognized
    > error_type.
41. `web/execution/routes.py:R6:_resolved_allowed_artifact_paths:fp=f8383cd29fa5d544` — **restage** (drift repair).
    Proposed rationale:
    > R6, fail-closed filesystem-boundary skip. The `except OSError:
    > continue` at web/execution/routes.py:219-222 covers only
    > `fs_path.resolve()` on artifact path candidates; an unresolvable
    > candidate is simply NOT admitted to `resolved_paths`, and when no
    > candidate resolves the function raises the 403
    > `output_path_outside_allowlist` envelope
    > (web/execution/routes.py:229-236). Skipping grants nothing —
    > failure direction is deny. Invalidated if the empty-result raise is
    > removed.
42. `web/execution/routes.py:R6:create_execution_router:websocket_run_progress:fp=5de17b68dcaf1724` — **restage**.
43. `web/execution/routes.py:R6:create_execution_router:websocket_run_progress:fp=88c0e2e19f9b61e8` — **restage**.
44. `web/execution/routes.py:R6:create_execution_router:websocket_run_progress:fp=ebb0bb3cf474aee9` — **restage**.
    42–44: the three transport-boundary handlers at the tail of
    `websocket_run_progress`:
    (a) `except WebSocketDisconnect: pass` (web/execution/routes.py:1591)
    — the peer ended the push stream; that is a normal terminal condition
    of a websocket, there is no one left to report to, and `finally:
    broadcaster.unsubscribe(run_id, queue)` still runs;
    (b) `except (ConnectionError, OSError)` (:1593) — transport failure
    recorded via `slog.error("websocket_handler_error", …)` followed by a
    best-effort 1011 close frame;
    (c) `except (WebSocketDisconnect, ConnectionError, OSError)` on that
    close attempt (:1601) — recorded via
    `slog.error("websocket_close_failed", …)`; re-raising would crash the
    handler on a socket that is already gone AFTER the primary error was
    logged. All three are typed Tier-3 socket-lifecycle catches; run
    status/audit state is never derived from them (authoritative status is
    re-checked from `_load_run_status_snapshot_with_accounting`).
    Invalidated if any of the three starts swallowing non-transport
    classes or the finally unsubscribe is removed.
45. `web/execution/schemas.py:R1:RunEvent:_resolve_data_from_event_type:fp=1e7fd408339f3f26` — **fixed** (f5b1c36d5).
    Membership-then-subscript for the two optional pre-validation keys;
    unrecognized shapes fall through to pydantic's validators unchanged.
    Stale entry → drop.
46. `web/execution/service.py:R6:ExecutionServiceImpl:_authoritative_proof_blob_resolver:fp=bcbdaf16191678dc` — **restage**.
    `except ValueError: continue` at web/execution/service.py:933-935 is
    the Tier-3 parse of an AUTHORED `source.options["blob_ref"]` (composer
    LLM / user-authored options): a non-canonical-UUID value means "this
    source does not carry a live blob binding", so it contributes no
    expected paths. It cannot silently absolve a claim: retained sentinel
    review claims are censused separately from
    `guided.reviewed_sources` and an unresolvable claim surfaces as the
    blocking `UnresolvedClaimedProofBlob` diagnostic (admission-direction
    comment at web/execution/service.py:911-916, elspeth-3b45cdb41e).
    Invalidated if the skipped value stops being authored data or the
    sentinel-claim census stops covering exited history.
47. `web/execution/service.py:R6:ExecutionServiceImpl:_authoritative_state_preflight_sync:_blob_get_metadata:fp=ac75faefbf697433` — **restage**.
    `except BlobNotFoundError: return None` at
    web/execution/service.py:1039-1041 is a protocol adapter converting the
    blob service's typed absence signal into the `BlobRecord | None`
    contract `validate_pipeline` consumes; directly below, a cross-session
    record is ALSO mapped to None deliberately so an attacker cannot
    distinguish "absent" from "not yours". The typed catch cannot hide
    integrity failures: a non-`BlobRecord` return raises TypeError
    (web/execution/service.py:1042-1043) and every other exception
    propagates. Invalidated if callers stop treating None as
    absent-or-foreign, or if the exact-type check is removed.
48. `web/execution/service.py:R6:ExecutionServiceImpl:_persist_and_broadcast_run_event:fp=bd5eb6f0c7b82244` — **restage** (drift repair).
    `except (OSError, SQLAlchemyError)` at web/execution/service.py:3185
    around the `append_run_event` write only: on infra fault it records
    `slog.error("run_event_persist_failed", …, exc_class=…)` and degrades
    to broadcast-without-sequence. run_events is the secondary
    websocket-replay/inspection stream; authoritative lifecycle state
    persists on the separate must-succeed `update_run_status` path
    (web/execution/service.py:1743, :1921, :1958, :2341, :2588, :2733 —
    cancel/complete/running transitions, none of which route through this
    helper). Tier-1 breaches are NOT in the tuple:
    `append_run_event` raises `AuditIntegrityError` for an invalid
    event_type and `ValueError("Run … not found")`
    (web/sessions/service.py:9538-9556), both of which propagate here.
    Invalidated if run_events becomes the record-of-record for terminal
    events or the tuple widens.

## web/interpretation_state.py (2)

49. `web/interpretation_state.py:R5:_validated_mapping_pair:fp=340d89ea1bebd458` — **fixed** (f5b1c36d5).
50. `web/interpretation_state.py:R5:_validated_mapping_pair:fp=7680a3b648067e29` — **fixed** (f5b1c36d5).
    49–50: the structural form the signing judges directed. New
    `_validated_mapping_field` carries
    `@trust_boundary(tier=3, source_param="field", suppresses=("R5",))`
    with a raising invariant (ValueError, never coerce/stringify), pinned
    by the new
    `tests/unit/web/test_interpretation_state.py::test_validated_mapping_field_rejects_non_string_mapping_sides`
    (fingerprint recorded in the decorator; `trust_boundary.tests` gate
    exit 0). `_validated_mapping_pair` composes it per side. Per-line
    entries → drop.

## web/operator_telemetry.py (3)

51. `web/operator_telemetry.py:R6:OperatorTelemetryRuntime:shutdown:fp=5e2327f4f9f06d9d` — **restage**.
    `except TELEMETRY_TRANSPORT_ERRORS` at web/operator_telemetry.py:310
    around the SDK's bounded synchronous shutdown. Proposed rationale:
    > R6, telemetry-only teardown tolerance (logging-primacy exemption).
    > The tuple is closed to transport/IO classes
    > (src/elspeth/telemetry/errors.py:17-21: ConnectionError,
    > TimeoutError, OSError); a collector outage at shutdown records
    > `_log.warning("operator_otlp_shutdown_unavailable")` and the
    > `finally: self._finish_shutdown()` completes the once-only state
    > machine so a wedged exporter cannot be re-shutdown. Raising would
    > convert a dead OTLP collector into a lifespan shutdown failure —
    > telemetry is subordinate to audit and app teardown. First-party
    > errors propagate. The exporter-level health tracker additionally
    > records transport failures (`_HealthTrackingMetricExporter.shutdown`,
    > web/operator_telemetry.py:265-269). Invalidated if the tuple widens
    > beyond transport classes.
52. `web/operator_telemetry.py:R6:reset_operator_telemetry_for_tests:fp=0d4f46ad3dcd2a91` — **restage**.
53. `web/operator_telemetry.py:R6:reset_operator_telemetry_for_tests:fp=80966bb8c3158ba4` — **restage**.
    52–53: `except TimeoutError` (web/operator_telemetry.py:575) and
    `except TELEMETRY_TRANSPORT_ERRORS` (:577) in the TEST-ONLY reset
    helper (callers: tests/unit/web/test_operator_telemetry.py,
    tests/unit/web/test_operator_pipeline_metrics.py; no production call
    site). The OTel provider shutdown runs under the same bounded
    `_EXPORT_TIMEOUT_MILLIS` deadline and may raise its timeout; each arm
    records a distinct warning
    (`operator_telemetry_test_reset_timeout` /
    `operator_telemetry_test_reset_unavailable`) and the
    `finally` completes `_finish_shutdown()` + clears `_runtime`, which is
    the helper's whole contract — detaching test state deterministically.
    Raising would fail unrelated test teardown on collector flakiness.
    Invalidated if the helper gains a production caller.

## web/plugin_policy/validation.py (1)

54. `web/plugin_policy/validation.py:R5:_components:fp=a77c31088c87d6de` — **abandoned** (f5b1c36d5).
    `deep_thaw(source.options) if isinstance(source.options, Mapping) else {}`
    deleted in favor of the direct call. `SourceSpec.options` is a typed,
    frozen-at-construction `Mapping[str, Any]`
    (web/composer/state.py:558); substituting `{}` for a corrupted shape
    silently erased authored options and disabled every downstream policy
    check — the forbidden silent-recovery pattern. Corruption now crashes
    in `deep_thaw`.

## web/readiness.py (6)

55. `web/readiness.py:R1:ReadinessProbeRunner:run:wrapped_done:fp=6c6746e429001565` — **fixed** (e74de4bd4).
    Registry read in `wrapped_done` → membership-then-subscript with the
    legal-absence rationale in code (stale callback racing a newer
    registration's cleanup); identity-checked delete unchanged.
56. `web/readiness.py:R1:readiness_report:fp=19e24721a4266257` — **fixed** (e74de4bd4).
    The fabricated `ReadinessCheck(name, False, "check result missing")`
    default is gone: `by_name[name]` indexes directly, and a missing name
    (first-party contract breach — every `runner.run` returns a complete
    named tuple on all paths) surfaces through the enclosing
    `except BaseException` arm as the fail-closed
    "readiness evaluation failed (KeyError)" report.
57. `web/readiness.py:R4:ReadinessCache:_harvest_locked:fp=64dea864c0a70688` — **fixed** (e74de4bd4).
    Returns an explicit `_HarvestOutcome`; the waiterless-failure path —
    the judge's genuine gap — is now recorded
    (`readiness_compute_failed_without_waiter` with exc_class) at the
    harvest site in `get_or_compute`. Narrowed: CancelledError retires
    quietly, Exception → failure record, stored process-control
    BaseExceptions propagate.
58. `web/readiness.py:R4:_probe_directory:fp=f8595d6468a898e6` — **fixed** (e74de4bd4).
    Handlers append the declared failure `ReadinessCheck` records into an
    accumulator (cleanup precedence preserved); narrowed BaseException →
    Exception/OSError so process-control exceptions propagate to the
    worker future and convert at run()'s retrieval; unlink cleanup uses
    `Path.unlink(missing_ok=True)`.
59. `web/readiness.py:R7:_drain_source:fp=eafcf9380ec5d84e` — **fixed** (e74de4bd4).
60. `web/readiness.py:R7:_drain_wrapped:fp=3b020c12d444fde2` — **fixed** (e74de4bd4).
    59–60: `suppress(BaseException)` deleted. With the added
    `cancelled() or not done()` guards, `Future.exception()` on a done
    non-cancelled future returns the stored exception rather than raising
    — retrieval is total, and conversion stays at run()'s retrieval site
    (`_exception_failures`).

## web/schema_probe.py (3)

61. `web/schema_probe.py:R1:postgres_logical_target_key:fp=9500974e06324a75` — **fixed** (f5b1c36d5).
62. `web/schema_probe.py:R5:_sqlstate:fp=cc5e4d561080c051` — **fixed** (f5b1c36d5).
63. `web/schema_probe.py:R5:postgres_logical_target_key:fp=791cb671e708acde` — **fixed** (f5b1c36d5).
    61+63: `@trust_boundary(tier=3, source_param="url",
    suppresses=("R1","R5"))` raising boundary on
    `postgres_logical_target_key` (invariant names
    DatabaseTargetConflictError; test_ref
    `tests/unit/web/test_schema_probe.py::test_unprovable_target_is_rejected_with_static_message`
    with recorded fingerprint). 62: non-raising
    `@trust_boundary(source_param="exc", suppresses=("R5",),
    non_raising=True)` on `_sqlstate` (None sentinel over driver-specific
    DBAPI attributes). Lint shows R_TB_SUPPRESSED at all three sites;
    `trust_boundary.tests` gate exit 0. Per-line entries → drop.

## web/secrets/service.py (2)

64. `web/secrets/service.py:R6:WebSecretService:resolve_scoped:fp=28790277ba1d8809` — **restage**.
65. `web/secrets/service.py:R6:WebSecretService:resolve_scoped:fp=8b0a5b821d8db53a` — **restage**.
    64–65: `except FingerprintKeyMissingError` (web/secrets/service.py:252)
    and `except SecretDecryptionError` (:255). Proposed rationale (shared,
    one entry per class):
    > R6, declared miss contract of the scoped resolver. `resolve_scoped`
    > returns None for EVERY unresolvable condition so
    > `resolve_secret_refs` (core/secrets.py:94-121) buckets scoped and
    > unscoped misses via `_walk` (core/secrets.py:358-376) and raises one
    > aggregate `SecretResolutionError` listing all missing refs — the
    > run fails closed; nothing proceeds on a fabricated value. The two
    > conditions stay operator-distinguishable through their own
    > rate-limited breadcrumbs (`_log_fingerprint_missing_rate_limited`,
    > `_log_secret_decryption_rate_limited`,
    > web/secrets/service.py:91-123) while remaining indistinguishable to
    > the caller by design (no fallback between stores; an undecryptable
    > user row must not reveal or shadow a same-named server secret).
    > Pinned per exception class by
    > tests/unit/web/secrets/test_service.py::TestResolveScoped —
    > ::test_scope_mismatch_never_falls_through_to_the_other_store,
    > ::test_undecryptable_user_row_returns_none_and_emits_decryption_breadcrumb,
    > ::test_absent_secret_returns_none_with_no_breadcrumb. Invalidated
    > if resolve_secret_refs stops raising on misses or a store fallback
    > is introduced.

---

## Shared-baseline changes needed

No masquerade or dynamic-attribute pinned-set updates are required: the
new code introduces no `getattr`/`setattr` dynamic-attribute sites and no
masquerade sites (the two pre-existing sentinel `getattr`s in
`_sqlstate` and `check_error_with_cause` are unchanged code, only
re-fingerprinted).

Allowlist consequences for the hub (I did not touch any yaml):

1. **Drop (retired by fixes)** — the stale entries for:
   `web/auth/entra.py` R1 ×2, `web/auth/oidc.py` R1,
   `web/app.py:R1:_BodySizeLimitMiddleware:dispatch`,
   `web/execution/schemas.py:R1:RunEvent:_resolve_data_from_event_type`,
   plus every per-line entry covering sites now suppressed structurally
   (`web/schema_probe.py` R1/R5, `web/interpretation_state.py`
   `_validated_mapping_pair` R5 ×2) or deleted outright
   (`web/_aws_ecs_acceptance/capture.py` R5 ×2,
   `web/plugin_policy/validation.py` R5,
   `web/_aws_ecs_acceptance/secure_documents.py` R6).
2. **Mechanical re-fingerprint (drift from this lane's edits; code
   semantics unchanged)** — signed entries now reported stale:
   `web/_aws_ecs_acceptance/bedrock.py:R6:run_bedrock_guardrails_live:fp=fb708d910ac4fa68`,
   `web/_aws_ecs_acceptance/bedrock.py:R7:_suppress_process_output:fp=5050b43fd5756158`
   and `fp=ace862310a79fa5e` (pre-yield flush entries — sites now REMOVED,
   so these two should be dropped rather than repaired),
   `web/_aws_ecs_acceptance/contracts.py:R2:check_error_with_cause:fp=760c581512e81e47`,
   `web/_aws_ecs_acceptance/orphan_sweep.py:R5:orphan_sweep:fp=53dc3c2095454b3e`,
   `web/_aws_ecs_acceptance/s3.py:R4:verify_s3:fp=04ac1ea79c1b5c94` /
   `fp=0e00ecabca06cc69` (dropped-or-refit: the R4 body handlers were
   reworked — compare against the new tree),
   `web/_aws_ecs_acceptance/s3.py:R6:verify_s3` ×3 (fp=1cf4…, 2591…,
   4615… — the `_S3EffectFailure` / `AcceptanceCheckError` /
   `S3ConditionalWriteRejectedError` typed arms; the first two now append
   records and may be retired entirely, the third —
   `except S3ConditionalWriteRejectedError: collision_rejected = True` at
   s3.py:352 — is unchanged semantics: the rejection IS the probe's
   expected positive outcome; re-stage it),
   `web/app.py:R7:_service_lifespan:fp=28977df3cc6a900b` (the
   CancelledError-narrowed orphan-task shutdown drain, now at
   web/app.py:768 — unchanged),
   `web/blobs/service.py:R7:_await_fork_copy_io_with_checkpoints:fp=283032e6c21d204d`
   (site removed by the drain rework — drop),
   `web/execution/_validation_diagnostics.py:R5:_reframe_settings_missing_parts:fp=ae7beb5113d37fb4`
   (line shift only),
   `web/readiness.py:R1:ReadinessProbeRunner:run:fp=1dfdf4af89cb50e3`
   (site converted to membership form in 9330932a1 — drop),
   `web/readiness.py:R4:_probe_directory` ×2 + `R6:_probe_directory`
   (handlers reworked — compare against new tree; likely drop),
   `web/schema_probe.py:R2:_sqlstate` ×2 (sentinel getattrs unchanged,
   fingerprint shift from the added decorator — re-stage).
3. **New raw findings this lane deliberately leaves for re-staging** (the
   21 restages above, at their post-edit line numbers as reported by the
   shape-only lint run of 2026-08-31).
4. `web/execution/service.py` R5 at :1341 and :1421 were raw in the
   baseline and are NOT in this lane's worklist; untouched.
5. One out-of-scope one-liner rode along in f8f690156:
   `src/elspeth/web/composer/guided/emitters.py` — the wrapper-injected
   `on_write_failure` knob was missing the required `tier` key
   (pre-existing `typeddict-item` mypy error that the changed-files hook
   surfaces on any web commit). Flagging for the composer lane owner.
