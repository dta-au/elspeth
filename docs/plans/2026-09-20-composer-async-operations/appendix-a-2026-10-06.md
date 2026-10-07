# Appendix A: current registered composer exits — 2026-10-06

Source HEAD: `edc844699a350a90a624089e12a5a1e75b7b2dde`. Actual production `create_app` handler registration and session APIRoute objects were inspected with no lifespan/provider startup. Random local test app signing material is not operator HMAC material and is not retained. Direct function AST exits exclude nested functions; synthetic known return/raise, nested-function negative and injected-raise controls passed. Full expressions and conditions remain in `registered-handler-and-route-exits.json`.

**Scope:** this is a complete direct-exit and registration baseline for the two current routes, not a transitive call-graph/error-contract proof. The future job table, DTOs, stream and worker do not exist. Proposed ruling exits below remain review targets.

## `/api/sessions/{session_id}/recompose`

Function: `recompose` in `src/elspeth/web/sessions/routes/composer/compose.py`. Actual dependencies: `elspeth.web.auth.middleware.require_pipeline_user`, `elspeth.web.middleware.rate_limit.get_rate_limiter`, `elspeth.web.sessions.routes._helpers._track_compose_inflight`.

| Source line | Direct exit / condition | Owning task |
|---|---|---|
| 152 | `raise HTTPException(status_code=400, detail='No messages to recompose from')` — `if not conversation_records` | N06/N10/N11b: retry admission/refusal |
| 154 | `raise HTTPException(status_code=409, detail='Cannot recompose: the last message is not a user message. Recompose is only valid when the most recent message is the user turn whose composition failed.')` — `if conversation_records[-1].role != 'user'` | N10/N11b: public error matrix + authoritative failure terminal |
| 161 | `raise HTTPException(status_code=409, detail={'error_type': 'recompose_user_message_mismatch', 'detail': 'The latest user message changed. Refresh the session before retrying composition.'})` — `if conversation_records[-1].id != body.expected_user_message_id` | N06/N10/N11b: retry admission/refusal |
| 425 | `raise post_provider_error from watcher_cancel` — `except asyncio.CancelledError; if post_provider_error is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 426 | `raise` — `except asyncio.CancelledError` | N10/N11b: public error matrix + authoritative failure terminal |
| 452 | `raise HTTPException(status_code=422, detail=response_body) from exc` — `except ComposerConvergenceError` | N10/N11b: public error matrix + authoritative failure terminal |
| 454 | `raise await _handle_composer_provider_failure(exc, route='recompose', service=service, session_id=session.id, composition_state_id=pre_send_state_id, progress_sink=progress_sink, session_operation_context=compose_operation_lease.context, expose_provider_error=settings.composer_expose_provider_errors) from exc` — `except OpenAIError` | N10/N11b: public error matrix + authoritative failure terminal |
| 490 | `raise HTTPException(status_code=502, detail=_litellm_error_detail('llm_unavailable', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc` — `except _BadRequestLLMError` | N10/N11b: public error matrix + authoritative failure terminal |
| 529 | `raise HTTPException(status_code=500, detail=response_body) from crash.original_exc` — `except ComposerPluginCrashError` | N10/N11b: public error matrix + authoritative failure terminal |
| 569 | `raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc` — `except ComposerRuntimePreflightError` | N10/N11b: public error matrix + authoritative failure terminal |
| 595 | `raise HTTPException(status_code=status_code, detail=planner_response_body) from exc` — `except PipelinePlannerError` | N10/N11b: public error matrix + authoritative failure terminal |
| 597 | `raise await _handle_composer_chargeable_refusal(exc, service=service, session_id=session.id, composition_state_id=pre_send_state_id, progress_sink=progress_sink, session_operation_context=compose_operation_lease.context) from exc` — `except ChargeableAdmissionRefused` | N10/N11b: public error matrix + authoritative failure terminal |
| 622 | `raise HTTPException(status_code=422, detail=exc.to_payload()) from exc` — `except ComposerAdmissionRefused; if isinstance(exc, CredentialMaterialRefused)` | N10/N11b: public error matrix + authoritative failure terminal |
| 623 | `raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc` — `except ComposerAdmissionRefused` | N10/N11b: public error matrix + authoritative failure terminal |
| 648 | `raise HTTPException(status_code=502, detail={'error_type': 'composer_error', 'detail': str(exc)}) from exc` — `except ComposerServiceError` | N10/N11b: public error matrix + authoritative failure terminal |
| 673 | `raise post_provider_error` — `if post_provider_error is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 675 | `raise InvariantError('Provider returned without a freeform continuation receipt')` — `if continuation_receipt is None` | N10/N11b: public error matrix + authoritative failure terminal |
| 677 | `raise deferred_cancellation` — `if deferred_cancellation is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 678 | `return continuation_receipt.response` — `unconditional in this function block` | N10/N11b: public error matrix + authoritative failure terminal |
| 691 | `raise HTTPException(status_code=500, detail={'error_type': 'server_invariant_violated', 'detail': 'Server invariant violated. See application audit log for diagnostic detail.'}) from exc` — `except InvariantError` | N10/N11b: public error matrix + authoritative failure terminal |
| 702 | `raise HTTPException(status_code=499, detail='Client disconnected after the compose turn completed.') from exc` — `except asyncio.CancelledError; if request.state.composer_durable_completed; if _is_client_disconnect_cancel(exc)` | N10/N11b: public error matrix + authoritative failure terminal |
| 706 | `raise` — `except asyncio.CancelledError; if request.state.composer_durable_completed` | N10/N11b: public error matrix + authoritative failure terminal |
| 747 | `raise HTTPException(status_code=499, detail='Client disconnected while the compose turn was running.') from exc` — `except asyncio.CancelledError; if _is_client_disconnect_cancel(exc)` | N10/N11b: public error matrix + authoritative failure terminal |
| 751 | `raise` — `except asyncio.CancelledError` | N10/N11b: public error matrix + authoritative failure terminal |

## `/api/sessions/{session_id}/messages`

Function: `register_message_routes.<locals>.send_message` in `src/elspeth/web/sessions/routes/messages.py`. Actual dependencies: `elspeth.web.auth.middleware.require_pipeline_user`, `elspeth.web.middleware.rate_limit.get_rate_limiter`, `elspeth.web.sessions.routes._helpers._track_compose_inflight`.

| Source line | Direct exit / condition | Owning task |
|---|---|---|
| 160 | `raise HTTPException(status_code=422, detail=exc.to_payload()) from exc` — `except CredentialMaterialRefused` | N10/N11b: public error matrix + authoritative failure terminal |
| 187 | `raise _ingress_receipt_conflict(existing_ingress)` — `if isinstance(existing_ingress, (MessageIngressAccepted, MessageIngressConflict))` | N06/N10/N11b: ingress custody/integrity |
| 205 | `raise HTTPException(status_code=404, detail='State not found') from None` — `if body.state_id is not None; except ValueError` | N10/N11b: public error matrix + authoritative failure terminal |
| 207 | `raise HTTPException(status_code=404, detail='State not found')` — `if body.state_id is not None; if client_state.session_id != session.id` | N10/N11b: public error matrix + authoritative failure terminal |
| 260 | `raise _ingress_receipt_conflict(ingress_result)` — `if isinstance(ingress_result, (MessageIngressAccepted, MessageIngressConflict))` | N06/N10/N11b: ingress custody/integrity |
| 262 | `raise AuditIntegrityError('Message ingress returned an unrecognized outcome')` — `if not isinstance(ingress_result, MessageIngressFresh)` | N06/N10/N11b: ingress custody/integrity |
| 265 | `raise ingress_cancellation` — `if ingress_cancellation is not None` | N06/N10/N11b: ingress custody/integrity |
| 311 | `raise AuditIntegrityError(f'Tier 1 audit anomaly: send_message transcript snapshot for session {session.id} does not end at inserted user message {user_msg.id}. Refusing to compose against interleaved session history.')` — `if not conversation_records or conversation_records[-1].id != user_msg.id` | N10/N11b: public error matrix + authoritative failure terminal |
| 628 | `raise post_provider_error from watcher_cancel` — `except asyncio.CancelledError; if post_provider_error is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 629 | `raise` — `except asyncio.CancelledError` | N10/N11b: public error matrix + authoritative failure terminal |
| 656 | `raise HTTPException(status_code=422, detail=response_body) from exc` — `except ComposerConvergenceError` | N10/N11b: public error matrix + authoritative failure terminal |
| 658 | `raise await _handle_composer_provider_failure(exc, route='messages', service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=compose_operation_lease.context, expose_provider_error=settings.composer_expose_provider_errors) from exc` — `except OpenAIError` | N10/N11b: public error matrix + authoritative failure terminal |
| 694 | `raise HTTPException(status_code=502, detail=_litellm_error_detail('llm_unavailable', exc, expose_provider_error=settings.composer_expose_provider_errors)) from exc` — `except _BadRequestLLMError` | N10/N11b: public error matrix + authoritative failure terminal |
| 756 | `raise HTTPException(status_code=500, detail=response_body) from crash.original_exc` — `except ComposerPluginCrashError` | N10/N11b: public error matrix + authoritative failure terminal |
| 814 | `raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc` — `except ComposerRuntimePreflightError` | N10/N11b: public error matrix + authoritative failure terminal |
| 852 | `raise HTTPException(status_code=status_code, detail=planner_response_body) from exc` — `except PipelinePlannerError` | N10/N11b: public error matrix + authoritative failure terminal |
| 854 | `raise await _handle_composer_chargeable_refusal(exc, service=service, session_id=session.id, composition_state_id=compose_base_state_id, progress_sink=progress_sink, session_operation_context=compose_operation_lease.context) from exc` — `except ChargeableAdmissionRefused` | N10/N11b: public error matrix + authoritative failure terminal |
| 874 | `raise HTTPException(status_code=422, detail=exc.to_payload()) from exc` — `except ComposerAdmissionRefused; if isinstance(exc, CredentialMaterialRefused)` | N10/N11b: public error matrix + authoritative failure terminal |
| 875 | `raise HTTPException(status_code=403, detail={'error_type': 'composer_admission_refused', 'failure_code': 'admission_refused', 'detail': str(exc)}) from exc` — `except ComposerAdmissionRefused` | N10/N11b: public error matrix + authoritative failure terminal |
| 900 | `raise HTTPException(status_code=502, detail={'error_type': 'composer_error', 'detail': str(exc)}) from exc` — `except ComposerServiceError` | N10/N11b: public error matrix + authoritative failure terminal |
| 928 | `raise post_provider_error` — `if post_provider_error is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 930 | `raise InvariantError('Provider returned without a freeform continuation receipt')` — `if continuation_receipt is None` | N10/N11b: public error matrix + authoritative failure terminal |
| 932 | `raise deferred_cancellation` — `if deferred_cancellation is not None` | N07/N09/N10/N11b: cancellation/settlement priority |
| 933 | `return continuation_receipt.response` — `unconditional in this function block` | N10/N11b: public error matrix + authoritative failure terminal |
| 945 | `raise HTTPException(status_code=500, detail={'error_type': 'server_invariant_violated', 'detail': 'Server invariant violated. See application audit log for diagnostic detail.'}) from exc` — `except InvariantError` | N10/N11b: public error matrix + authoritative failure terminal |
| 956 | `raise HTTPException(status_code=499, detail='Client disconnected after the compose turn completed.') from exc` — `except asyncio.CancelledError; if request.state.composer_durable_completed; if _is_client_disconnect_cancel(exc)` | N10/N11b: public error matrix + authoritative failure terminal |
| 960 | `raise` — `except asyncio.CancelledError; if request.state.composer_durable_completed` | N10/N11b: public error matrix + authoritative failure terminal |
| 1021 | `raise HTTPException(status_code=499, detail='Client disconnected while the compose turn was running.') from exc` — `except asyncio.CancelledError; if _is_client_disconnect_cancel(exc)` | N10/N11b: public error matrix + authoritative failure terminal |
| 1025 | `raise` — `except asyncio.CancelledError` | N10/N11b: public error matrix + authoritative failure terminal |

## Actual exception registry

Native registry has 17 entries. The WebSocket handler is outside these HTTP routes; installed-dependency exits were not source-scanned. Run/audit-specific global handlers are retained in the registration corpus, not presumed composer-reachable. All HTTP error projections belong to N10; durable admitted failure persistence/replay belongs to N11b. OperationalError/OSError and audit/fence failures also require N07/N09 cancellation/accounting and N17 dialect tests.

### `starlette.exceptions.HTTPException`

Handler: `elspeth.web.app._create_app.<locals>.handle_http_exception`; source `src/elspeth/web/app.py`.

- Line 2268: `return await fastapi_http_exception_handler(request, exc)`. Conditions: `if envelope is None`.
- Line 2283: `return await fastapi_http_exception_handler(request, correlated)`. Conditions: `unconditional in this function block`.

### `fastapi.exceptions.RequestValidationError`

Handler: `elspeth.web.app._create_app.<locals>.handle_validation_error`; source `src/elspeth/web/app.py`.

- Line 2241: `return JSONResponse(status_code=422, content={'detail': safe_errors, 'request_id': request_id})`. Conditions: `unconditional in this function block`.

### `fastapi.exceptions.WebSocketRequestValidationError`

Handler: `fastapi.exception_handlers.websocket_request_validation_exception_handler`; source `<installed-dependency>`.

No source-owned direct exits captured (installed dependency / HTTP-scope exclusion).

### `elspeth.web.coordination.contracts.SessionOperationFenceLost`

Handler: `elspeth.web.session_operation_handlers.register_session_operation_exception_handlers.<locals>._session_operation_fence_lost_handler`; source `src/elspeth/web/session_operation_handlers.py`.

- Line 26: `return JSONResponse(status_code=404, content={'detail': 'Session not found'})`. Conditions: `unconditional in this function block`.

### `elspeth.web.coordination.repository.SessionOperationConflictError`

Handler: `elspeth.web.session_operation_handlers.register_session_operation_exception_handlers.<locals>._session_operation_conflict_handler`; source `src/elspeth/web/session_operation_handlers.py`.

- Line 30: `return JSONResponse(status_code=409, content={'detail': 'Session operation is already active'})`. Conditions: `unconditional in this function block`.

### `elspeth.web.credential_guard.CredentialMaterialRefused`

Handler: `elspeth.web.app._create_app.<locals>._credential_material_refused_handler`; source `src/elspeth/web/app.py`.

- Line 1369: `return JSONResponse(status_code=422, content={'detail': exc.to_payload(), 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.contracts.errors.AuditIntegrityError`

Handler: `elspeth.web.app._create_app.<locals>._audit_integrity_error_handler`; source `src/elspeth/web/app.py`.

- Line 1398: `return JSONResponse(status_code=500, content={'error_type': 'audit_integrity_error', 'detail': "ELSPETH stopped before replying because it could not verify this session's audit trail.", 'diagnostic': 'no_failed_turn_metadata', 'reason': 'originated outside compose-loop annotation scope', 'request_id': request_id})`. Conditions: `if failed_turn is None`.
- Line 1408: `return JSONResponse(status_code=500, content={'error_type': 'audit_integrity_error', 'detail': "ELSPETH stopped before replying because it could not verify this session's audit trail.", 'failed_turn': {'assistant_message_id': failed_turn.assistant_message_id, 'tool_calls_attempted': failed_turn.tool_calls_attempted, 'tool_responses_persisted': failed_turn.tool_responses_persisted or 0, 'transcript_url': None}, 'request_id': request_id})`. Conditions: `unconditional in this function block`.

### `elspeth.web.preferences.service.CorruptPreferencesError`

Handler: `elspeth.web.app._create_app.<locals>._corrupt_preferences_error_handler`; source `src/elspeth/web/app.py`.

- Line 1452: `return JSONResponse(status_code=500, content={'error_type': 'corrupt_preferences', 'detail': 'Saved preferences are corrupt; the composer is using defaults.', 'field_name': exc.field_name, 'user_id': exc.user_id, 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.web.sessions.audit_story_service.AuditStoryIntegrityError`

Handler: `elspeth.web.app._create_app.<locals>._audit_story_integrity_error_handler`; source `src/elspeth/web/app.py`.

- Line 1479: `return JSONResponse(status_code=500, content={'error_type': 'audit_story_integrity_error', 'detail': str(exc), 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.web.sessions.audit_story_service.AuditStoryNotRecordedError`

Handler: `elspeth.web.app._create_app.<locals>._audit_story_not_recorded_error_handler`; source `src/elspeth/web/app.py`.

- Line 1503: `return JSONResponse(status_code=404, content={'error_type': 'audit_story_not_recorded', 'detail': 'No audit story was recorded for this run.', 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.web.sessions.protocol.StaleComposeStateError`

Handler: `elspeth.web.app._create_app.<locals>._stale_compose_state_error_handler`; source `src/elspeth/web/app.py`.

- Line 1520: `return JSONResponse(status_code=409, content={'error_type': 'stale_compose_state', 'detail': 'The session changed while the compose turn was running.', 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.web.sessions.protocol.AuditAccessLogWriteError`

Handler: `elspeth.web.app._create_app.<locals>._audit_access_log_write_error_handler`; source `src/elspeth/web/app.py`.

- Line 1537: `return JSONResponse(status_code=500, content={'error_type': 'audit_access_log_write_failed', 'detail': 'Audit-grade transcript access could not be recorded; no audit-grade data returned.', 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.web.sessions.protocol.RunAlreadyActiveError`

Handler: `elspeth.web.app._create_app.<locals>.handle_run_already_active`; source `src/elspeth/web/app.py`.

- Line 2012: `return JSONResponse(status_code=409, content={'detail': str(exc), 'error_type': 'run_already_active', 'request_id': _correlation_id(request)})`. Conditions: `unconditional in this function block`.

### `elspeth.contracts.secrets.FingerprintKeyMissingError`

Handler: `elspeth.web.app._create_app.<locals>.handle_fingerprint_missing`; source `src/elspeth/web/app.py`.

- Line 2118: `return JSONResponse(status_code=503, content={'detail': 'Secret resolver is not configured: ELSPETH_FINGERPRINT_KEY is unset. Set the environment variable on the server and retry.', 'error_type': 'fingerprint_key_missing', 'request_id': request_id})`. Conditions: `unconditional in this function block`.

### `elspeth.contracts.secrets.SecretDecryptionError`

Handler: `elspeth.web.app._create_app.<locals>.handle_secret_decryption_failed`; source `src/elspeth/web/app.py`.

- Line 2143: `return JSONResponse(status_code=409, content={'detail': 'Stored secret cannot be decrypted — likely a web secret_key rotation. Re-save the secret to resolve.', 'error_type': 'secret_decryption_failed', 'request_id': request_id})`. Conditions: `unconditional in this function block`.

### `sqlalchemy.exc.OperationalError`

Handler: `elspeth.web.app._create_app.<locals>.handle_database_unavailable`; source `src/elspeth/web/app.py`.

- Line 2173: `return JSONResponse(status_code=503, content={'detail': 'Database is currently unavailable. Please retry in a moment.', 'error_type': 'database_unavailable', 'request_id': request_id})`. Conditions: `unconditional in this function block`.

### `builtins.OSError`

Handler: `elspeth.web.app._create_app.<locals>.handle_storage_unavailable`; source `src/elspeth/web/app.py`.

- Line 2195: `raise exc`. Conditions: `if exc.errno not in _RETRYABLE_STORAGE_ERRNOS`.
- Line 2205: `return JSONResponse(status_code=503, content={'detail': 'Storage backend is currently unavailable. Please retry in a moment.', 'error_type': 'storage_unavailable', 'request_id': request_id})`. Conditions: `unconditional in this function block`.

## Dependency/helper and proposed terminal dispositions

| Boundary | Current/proposed disposition and owner |
|---|---|
| Authentication / pipeline-user, limiter, inflight registration | Actual registered dependencies above; pre-admission auth/rate/storage refusal remains HTTP. N03/N10 must define safe envelope and prevent admissions on rejected authority. Stream reauthorization is N16. |
| Session lookup/ownership, state lookup, ingress transaction, transcript custody | Pre-admission session/state/ingress refusals versus post-admission audit failure must be classified by the actual moved boundary in N03/N06/N10/N11b; current direct exits are preserved above. |
| Provider, convergence, planner, plugin and preflight helpers | Existing helper failures can publish progress/audit before returning/raising. N08 extracts behavior; N10 freezes safe error shape, N07 preserves audit and provider accounting, N09/N11b publishes only after joins. SDK timeout 504 and convergence timeout 422 remain distinct; see measured M9. |
| Audit/settlement failure during cancellation | Authoritative integrity/storage failure must not be hidden by cancellation/weather. N07/N09/N11b join actual custody and select failure priority; N17 tests both dialects and thread completion. |
| Proposed job/admission outcomes | Unknown/active/delayed admissions, exact ID/body conflicts, saved response/proposal retry guards and base mismatch are N03/N06/N10/N11b contract additions, not measured existing handlers. |
| Proposed cancellation/fence terminal | Positive predicate, terminal CAS, clear body/claim, progress publication and exact replay are N07/N09/N11a/N11b/N12/N13 obligations. Current direct CancelledError/499 behavior is baseline to replace, not durable operation cancellation semantics. |
| Proposed stream/public GET failures | Invalid scope/job/auth, revoked/expired token, failed send/read, buffer/EOF and resource refusal are N16/N18/N19. Same-job GET remains authoritative; no provider replay or guessed terminal. |

Every prior numbered safety requirement remains review input via each active task page and the byte-preserved historical bundle. This appendix does not close unrecovered original-report findings or assert future normalization parity.
