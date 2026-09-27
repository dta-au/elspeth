"""Session-aware Composer tool dispatch and audit ownership."""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit, finish_arg_error, finish_success
from elspeth.web.composer.discovery_cache import serialize_tool_result as _serialize_tool_result
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_error_payloads import arg_error_payload as _arg_error_payload
from elspeth.web.composer.tools import (
    _SESSION_AWARE_TOOL_HANDLERS,
    RATE_CAP_CODE_TO_TELEMETRY_CAP_TYPE,
    ToolResult,
    normalize_tool_result_validation,
)
from elspeth.web.composer.tools._dispatch import require_schema_valid_arguments
from elspeth.web.composer.tools._registry import resolve_tool_effects
from elspeth.web.composer.tools.declarations import EffectDomain
from elspeth.web.execution.schemas import ValidationResult

if TYPE_CHECKING:
    from elspeth.web.sessions.protocol import SessionServiceProtocol
    from elspeth.web.sessions.telemetry import _SessionsTelemetry


@trust_boundary(
    tier=3,
    source="LLM composer tool-call payload (request_interpretation_review arguments)",
    source_param="arguments",
    suppresses=("R5",),
    invariant="raises AuditIntegrityError on a non-string or non-member kind; never coerces or writes a fabricated audit-row discriminator",
    test_ref="tests/unit/web/composer/test_request_interpretation_review_kind_boundary.py::test_non_str_kind_raises_audit_integrity_error",
    test_fingerprint="69e4bec4d82790adb9f3dfd104b1a504755721cd7aff2f56982e7cc7b86f0621",
)
def _request_interpretation_review_kind_from_arguments(arguments: Mapping[str, Any]) -> InterpretationKind:
    # `arguments` is the LLM tool-call payload (Tier 3); `kind` becomes the
    # interpretation-kind discriminator on an audit row, so a non-member or
    # non-string value must NOT be written. The InterpretationKind constructor
    # is itself the boundary check: a missing/non-string/unhashable value raises
    # ValueError (and TypeError on exotic inputs) from the enum lookup, which we
    # convert to a typed AuditIntegrityError rather than coercing or writing a
    # bad row. The uncaught AuditIntegrityError is the intended crash (we refuse
    # to record an audit row under a fabricated kind).
    raw_kind = arguments["kind"] if "kind" in arguments else None
    if not isinstance(raw_kind, str):
        raise AuditIntegrityError(f"request_interpretation_review rate-cap row has invalid kind {raw_kind!r}")
    try:
        return InterpretationKind(raw_kind)
    except ValueError as exc:
        raise AuditIntegrityError(f"request_interpretation_review rate-cap row has invalid kind {raw_kind!r}") from exc


@dataclass(frozen=True, slots=True)
class _SessionAwareDispatchOutcome:
    """Return value of ``_dispatch_session_aware_tool``.

    Carries the post-dispatch signals the compose loop needs to update
    its loop-local accounting:

    - ``result``: the SUCCESS ``ToolResult`` when the handler returned
      cleanly; ``None`` when the dispatch ended in an ARG_ERROR path
      (rate cap or generic) — the audit record was already written and
      the LLM-facing tool message already appended to ``llm_messages``.
    - ``is_discovery``: whether the loop should charge this turn to the
      discovery or composition budget. Session-aware tools that mutate
      composition state report ``False`` so they count as composition
      turns regardless of the success/failure shape.
    - ``error_class`` / ``error_category`` / ``error_message`` /
      ``post_version``: the P4 audit outcome metadata required to preserve
      the assistant tool-call row.
    """

    result: ToolResult | None
    is_discovery: bool
    error_class: str | None = None
    error_category: ToolArgumentErrorCategory | None = None
    error_message: str | None = None
    post_version: int = 0


class SessionToolOwner:
    """Own the exact session-aware tool dispatch and its audit side effects."""

    def __init__(
        self,
        *,
        sessions_service: SessionServiceProtocol | None,
        telemetry: _SessionsTelemetry,
        per_term_cap: int,
        per_session_day_cap: int,
        model_identifier: str,
        provider: str | None,
        composer_skill_hash: str,
    ) -> None:
        self._sessions_service = sessions_service
        self._telemetry = telemetry
        self._per_term_cap = per_term_cap
        self._per_session_day_cap = per_session_day_cap
        self._model = model_identifier
        self._provider = provider
        self._composer_skill_hash = composer_skill_hash

    def _require_sessions_service(self) -> SessionServiceProtocol:
        if self._sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        return self._sessions_service

    async def _dispatch_session_aware_tool(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        state: CompositionState,
        audit: DispatchAudit,
        recorder: BufferingRecorder,
        session_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
        current_state_id: str | None,
        composer_model_version: str,
        llm_messages: list[dict[str, Any]],
        anti_anchor: AntiAnchorTracker,
        policy_catalog: PolicyCatalogView,
        review_preflight: ValidationResult | None = None,
    ) -> _SessionAwareDispatchOutcome:
        """Dispatch a session-aware async composer tool.

        Mirrors the structural envelope discipline of ``dispatch_with_audit``
        used for the sync ``execute_tool`` path:

        * SUCCESS → ``finish_success`` and a serialized ToolResult appended
          to ``llm_messages``.
        * ARG_ERROR (generic) → ``finish_arg_error`` and the standard
          ``_arg_error_payload`` echo.
        * ARG_ERROR with ``code in RATE_CAP_CODE_TO_TELEMETRY_CAP_TYPE``
          (F-6 / F-15) → emit ``interpretation_rate_cap_exceeded`` operational
          telemetry, await ``record_auto_interpreted_no_surfaces_event`` to
          write the AUTO_INTERPRETED_NO_SURFACES audit row, THEN
          ``finish_arg_error`` and echo the standard ARG_ERROR payload so
          the LLM is nudged into the fallback path from the composer
          skill (bake the interpretation directly into the prompt
          template).
        * Plugin crash → propagate; outer compose loop wraps with
          ``ComposerPluginCrashError`` exactly as for the sync path.

        Pre-conditions:

        * ``session_id`` is not None — ``compose()`` admits a turn only
          with COMPOSE session authority bound to its ``session_id`` (the
          tool list itself is not filtered). ``RuntimeError`` is raised on a
          missing session id (interpreter-level invariant, not Tier-3).
        * ``current_state_id`` is not None for tools that need a
          composition_state foreign key (currently every session-aware
          tool). If the LLM calls the tool before a successful state-staging
          tool has created that row, this returns ARG_ERROR so the model can
          retry after staging the state instead of crashing the request.

        Per-tool dispatch is performed by reading the handler from
        ``_SESSION_AWARE_TOOL_HANDLERS`` and awaiting it with the
        keyword-arguments dict built by ``_build_session_aware_kwargs``.
        Adding a new session-aware tool extends that dict; this dispatch
        method itself does not need to change shape.
        """
        if session_id is None:
            # Compose-loop invariant. ``composer_loop_tool_definitions`` filters
            # nothing: every compose turn advertises the session-aware
            # tools. What guarantees a session here is ``compose()``'s
            # admission, which refuses a turn without COMPOSE session
            # authority and requires that authority's fence to name this
            # ``session_id``. Reaching this branch with no ``session_id``
            # is therefore a plumbing bug, not a Tier-3 LLM error, so crash
            # with a diagnostic message.
            raise RuntimeError(
                f"Session-aware tool {tool_name!r} dispatched without a session_id. "
                "compose() admits a turn only with COMPOSE session authority bound to its "
                "session_id, so the compose loop lost that binding."
            )
        if current_state_id is None:
            # Fresh chat sessions legitimately start without a
            # composition_states row. A session-aware tool can only write
            # its audit row after a successful state-staging tool
            # (set_pipeline/upsert_node/etc.) has advanced and persisted
            # the state. Treat an earlier call as LLM-correctable
            # sequencing, not a server crash: the request reached this
            # branch through a valid authenticated compose session, but
            # the LLM called the review tool before its FK target exists.
            exc = ToolArgumentError(
                argument="composition_state_id",
                expected=(
                    "a persisted composition state; call set_pipeline or another "
                    "state-staging tool successfully, wait for its tool result, "
                    "then call request_interpretation_review"
                ),
                actual_type="missing current_state_id",
            )
            error_message = str(exc.args[0] if exc.args else "ToolArgumentError")
            arg_error_payload = _arg_error_payload(exc, tool_name)
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class=type(exc).__name__,
                    error_category=exc.category,
                    error_message=error_message,
                    error_payload=arg_error_payload,
                )
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(arg_error_payload),
                }
            )
            return _SessionAwareDispatchOutcome(
                result=None,
                is_discovery=False,
                error_class=type(exc).__name__,
                error_category=exc.category,
                error_message=error_message,
                post_version=state.version,
            )

        if resolve_tool_effects(tool_name, arguments).domains != (EffectDomain.INTERPRETATION,):
            raise AssertionError("Session-aware dispatch requires owned interpretation effects.")
        handler = _SESSION_AWARE_TOOL_HANDLERS[tool_name]
        kwargs = self._build_session_aware_kwargs(
            tool_name=tool_name,
            arguments=arguments,
            state=state,
            session_id=session_id,
            current_state_id=current_state_id,
            tool_call_id=tool_call_id,
            composer_model_version=composer_model_version,
            session_operation_context=session_operation_context,
        )

        try:
            # Hold the arguments to the tool's closed-root flat schema S before
            # the handler's pydantic model, like every execute_tool dispatch.
            require_schema_valid_arguments(tool_name, arguments)
            if review_preflight is not None:
                result = ToolResult(
                    success=False,
                    updated_state=state,
                    validation=policy_catalog.validate_composition_state(state).validation,
                    affected_nodes=(),
                    runtime_preflight=review_preflight,
                    data={
                        "_kind": "interpretation_review_blocked",
                        "message": "Repair the runtime validation errors before requesting interpretation review cards.",
                    },
                )
            else:
                result = await handler(**kwargs)
        except ToolArgumentError as exc:
            # Two sub-paths: rate-cap (write F-6 row + emit F-15 telemetry
            # BEFORE raising the LLM-facing ARG_ERROR) vs. generic
            # ARG_ERROR (no extra side effects, standard echo).
            cap_type = (
                RATE_CAP_CODE_TO_TELEMETRY_CAP_TYPE[exc.code]
                if exc.code is not None and exc.code in RATE_CAP_CODE_TO_TELEMETRY_CAP_TYPE
                else None
            )
            if cap_type is not None:
                # F-15 telemetry FIRST (the spec is explicit: emit BEFORE
                # the ARG_ERROR returns). Operational-only — no
                # ``user_term`` attribute, PII risk.
                self._telemetry.interpretation_rate_cap_exceeded_total.add(
                    1,
                    attributes={
                        "cap_type": cap_type,
                    },
                )
                # F-6 writer SECOND. Best-effort with respect to the
                # interpretation_events row for the rejected call — the
                # handler already declined to insert a row, so this
                # AUTO_INTERPRETED_NO_SURFACES row is the only record of
                # the cap event. Exceptions here are NOT swallowed: a DB
                # failure at this site is a Tier-1 audit anomaly.
                sessions_service = self._require_sessions_service()
                if type(session_operation_context) is not SessionOperationContext:
                    raise AuditIntegrityError("Rate-cap interpretation persistence requires exact COMPOSE authority") from None
                await sessions_service.record_auto_interpreted_no_surfaces_event(
                    session_id=UUID(session_id),
                    session_operation_context=session_operation_context,
                    # ``audit.actor`` is the loop-local ``composer-web:user-…``
                    # actor string assembled at the top of ``_compose_loop``;
                    # it is the truthful caller identity for this dispatch
                    # and matches the audit envelope's ``actor`` field.
                    actor=audit.actor,
                    kind=_request_interpretation_review_kind_from_arguments(arguments),
                    model_identifier=self._model,
                    model_version=composer_model_version,
                    provider=self._provider or "unknown",
                    composer_skill_hash=self._composer_skill_hash,
                )

            # Audit envelope: ARG_ERROR. Truthful — the handler returned
            # a ToolArgumentError; the rate-cap subtype is recorded
            # elsewhere (F-6 row + F-15 telemetry).
            error_message = str(exc.args[0] if exc.args else "ToolArgumentError")
            arg_error_payload = _arg_error_payload(exc, tool_name)
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class=type(exc).__name__,
                    error_category=exc.category,
                    error_message=error_message,
                    error_payload=arg_error_payload,
                )
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(arg_error_payload),
                }
            )
            # Session-aware tools currently all carry composition-state
            # mutation intent (interpretation review stages a future
            # /resolve patch). Count toward composition turns regardless
            # of the SUCCESS/ARG_ERROR outcome, matching the sync
            # ARG_ERROR handling for mutation tools.
            return _SessionAwareDispatchOutcome(
                result=None,
                is_discovery=False,
                error_class=type(exc).__name__,
                error_category=exc.category,
                error_message=error_message,
                post_version=state.version,
            )

        result = normalize_tool_result_validation(result, policy_catalog)

        # SUCCESS path. The handler returned a clean ToolResult; record
        # ``finish_success`` and serialise the result for the LLM. The
        # ``result_payload`` matches the sync path's ToolResult.to_dict()
        # so the audit table's ``result_canonical`` column is shape-
        # consistent across dispatch paths.
        recorder.record(
            finish_success(
                audit,
                result_payload=result.to_dict(),
                version_after=result.updated_state.version,
            )
        )
        # Don't claim mutation success when the handler intentionally
        # returns state.version unchanged (interpretation_review_pending
        # stages a future /resolve patch; the version advances at
        # resolve-time, not at staging-time). Treat as a structural
        # success for anti-anchor tracking but not as a version-advance
        # mutation.
        if result.updated_state.version > state.version:
            anti_anchor.record_success()
        llm_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _serialize_tool_result(result),
            }
        )
        return _SessionAwareDispatchOutcome(
            result=result,
            is_discovery=False,
            error_class=None,
            error_message=None,
            post_version=result.updated_state.version,
        )

    def _build_session_aware_kwargs(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        state: CompositionState,
        session_id: str,
        current_state_id: str,
        tool_call_id: str,
        composer_model_version: str,
        session_operation_context: SessionOperationContext | None,
    ) -> dict[str, Any]:
        """Build the kwarg dict for a session-aware tool handler.

        Each session-aware tool's handler signature is closed-form
        (the session-aware tool contract documents the required shape); the kwargs differ per
        tool because the injected service methods and snapshot fields
        vary. Adding a new session-aware tool adds a branch here.
        """
        if tool_name == "request_interpretation_review":
            sessions_service = self._require_sessions_service()
            if type(session_operation_context) is not SessionOperationContext:
                raise AuditIntegrityError("request_interpretation_review requires the compose loop's exact session operation authority")
            create_pending = functools.partial(
                sessions_service.create_pending_interpretation_event,
                session_operation_context=session_operation_context,
            )
            return {
                "arguments": arguments,
                "state": state,
                "session_id": UUID(session_id),
                "composition_state_id": UUID(current_state_id),
                "tool_call_id": tool_call_id,
                "now": datetime.now(UTC),
                "per_term_cap": self._per_term_cap,
                "per_session_day_cap": self._per_session_day_cap,
                "model_identifier": self._model,
                # ``model_version`` is the actual provider-returned model
                # string when available; the response boundary has already
                # admitted and bounded it before this dispatch. LiteLLM
                # populates this for Anthropic/OpenAI with the dated
                # variant (e.g. ``claude-opus-4-7-20260101``). When the
                # provider does not return one we fall back to the
                # requested identifier — keeps the column NOT NULL
                # without fabricating a value.
                "model_version": composer_model_version,
                "provider": self._provider or "unknown",
                "composer_skill_hash": self._composer_skill_hash,
                "create_pending_interpretation_event": create_pending,
                "list_interpretation_events": sessions_service.list_interpretation_events,
            }
        # Defensive: a session-aware tool registered without a kwarg
        # branch here would silently fail at dispatch. Crash loudly so
        # the registration is wired completely before the LLM can
        # invoke it.
        raise RuntimeError(
            f"_build_session_aware_kwargs has no branch for {tool_name!r}; "
            f"every entry in _SESSION_AWARE_TOOL_HANDLERS must add a kwarg-build "
            f"branch here."
        )
