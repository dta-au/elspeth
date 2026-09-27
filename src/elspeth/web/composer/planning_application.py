"""Planning application for Guided and empty-state freeform Composer requests."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID

import structlog
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.blobs import BlobGuidedOperationWriteFence
from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import (
    BufferingRecorder,
    interleave_planner_audit_records,
    llm_call_audit_envelope,
    llm_call_audit_summary,
    planner_attempt_audit_envelope,
    planner_attempt_audit_summary,
)
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.discovery_cache import RuntimePreflightCache as _RuntimePreflightCache
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.no_tool_policy import (
    is_pending_interpretation_handoff,
    is_referential_pipeline_mutation_intent,
    state_is_structurally_empty,
)
from elspeth.web.composer.pipeline_planner import (
    DELTA_PLANNER_TERMINAL_INSTRUCTION,
    GuidedPlannerDecline,
    PipelineCandidatePolicyRejection,
    PipelinePlannerError,
    PipelinePlanResult,
    PlannerBudgetPolicy,
    PlannerConversationContext,
    PlannerCustodyConfig,
    PlannerDeclined,
    PlannerModelConfig,
    PlannerOriginatingMessage,
    PlannerPriorUserRequest,
    PlannerRequestLifecycle,
    PlannerTerminalContract,
    PlannerTerminalMaterialization,
    plan_pipeline,
)
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    PresentBase,
    composition_content_hash,
    owned_composition_state_authority,
)
from elspeth.web.composer.prompts import project_server_owned_option_metadata
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_AUTHORED_KEY,
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE,
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE,
    PIPELINE_STAGED_REVIEW_MESSAGE,
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE,
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE,
    ComposerHistoryMessage,
    ComposerResult,
    ComposerRuntimePreflightError,
    ComposerServiceError,
    ComposerSettings,
    PipelineCommitIntent,
)
from elspeth.web.composer.provider_config import LLM_API_MAX_ATTEMPTS, LLM_API_RETRY_BASE_DELAY_SECONDS
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.required_controls import wire_required_controls
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import RuntimePreflight
from elspeth.web.composer.withheld_replies import WithheldReply, withheld_reply_envelope
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy
from elspeth.web.sessions.protocol import SessionServiceProtocol

if TYPE_CHECKING:
    from elspeth.web.composer.guided.planning import GuidedCorrectionTarget, GuidedRevisionAuthority
    from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, GuidedOperationFence

slog = structlog.get_logger()
_FREEFORM_PLANNER_PRIOR_USER_REQUEST_MAX_ITEMS: Final[int] = 8


def _log_guided_planner_failure(
    exc: PipelinePlannerError,
    *,
    session_id: str,
    operation_id: str,
    surface: str,
) -> None:
    """Emit the typed planner disposition when a guided planner call fails.

    The freeform surface records ``planner_code`` and the last candidate
    rejection's ``rejection_codes`` in a durable ``planner_failure_disposition``
    audit row (``routes/_helpers._handle_planner_failure``). The guided route's
    terminal-failure ``slog`` carries only ``exc_class`` + frames (route-side,
    signed), so a guided planner 5xx hid the closed ``PipelinePlannerError.code``
    and the ``detail_codes`` that name the wall the repair loop hit — leaving a
    churned failure (e.g. REPAIR_EXHAUSTED after the escape hatch) opaque. Emit
    them here, the one in-fence site that holds the typed exception (it awaits
    ``plan_pipeline``), so a guided failure is as diagnosable as a freeform one.
    Structured and session/operation scoped; the caller re-raises so the
    terminal-failure path is unchanged. This is a diagnostic log, not a durable
    audit row — the guided cohort/terminalization is not reachable from in-fence.
    """
    slog.error(
        "composer.guided_planner_failure",
        session_id=session_id,
        operation_id=operation_id,
        surface=surface,
        planner_code=exc.code,
        rejection_codes=sorted(set(exc.detail_codes)),
        # The typed message is module-authored (closed codes; the candidate
        # construction path names the offending key) — bounded, never raw
        # provider/row content.
        error_detail=str(exc)[:300],
    )


async def _await_pipeline_staging_write_with_deferred_cancellation[T](
    awaitable: Awaitable[T],
    *,
    deferred: asyncio.CancelledError | None = None,
) -> tuple[T, asyncio.CancelledError | None]:
    """Finish one proposal lifecycle write after request cancellation.

    Session writes run in synchronous workers which cannot be stopped after
    submission. Shield the child from the outset, remember the first external
    cancellation, and retain any later child failure as its diagnostic cause.
    """
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task), deferred
        except asyncio.CancelledError as exc:
            # A child that cancelled itself is not an external request cancel
            # and must preserve its normal cancellation semantics.
            if task.cancelled():
                raise
            if deferred is None:
                deferred = exc
            if task.done():
                try:
                    return task.result(), deferred
                except asyncio.CancelledError:
                    raise
                except Exception as child_exc:
                    raise deferred from child_exc
        except Exception as child_exc:
            if deferred is None:
                raise
            raise deferred from child_exc


def _required_controls_candidate_finalizer(
    *,
    policy_catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    inner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Planner candidate finalizer that auto-wires deployment-REQUIRED controls.

    R2-F10 (elspeth-f99655f540): every planner surface runs the
    ``wire_required_controls`` pass on its terminal candidate so uncovered
    graphs are repaired server-side (with acknowledgeable disclosure) instead
    of shipping into the execution-time required-control block. ``inner``
    composes a surface-specific finalizer (the guided reviewed-component
    binder) BEFORE the pass, so wiring always sees the bound candidate. The
    pass is idempotent, so re-finalizing a covered candidate is a no-op.
    """

    def finalize(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        staged = inner(candidate) if inner is not None else candidate
        return wire_required_controls(staged, plugin_snapshot, policy_catalog)

    return finalize


@dataclass(frozen=True, slots=True)
class _PlannerPreviewPreflightCallbacks:
    """Precomputed ``preview_pipeline`` Stage-2 callbacks for one planner request.

    ``runtime`` is the strict verdict; ``structural`` is the interpretation-
    tolerant verdict, wired only when the strict verdict is handoff-shaped
    (elspeth-229e9e8195). Either may be ``None`` — an absent callback leaves
    the preview on its honest un-run / block-absent arm.
    """

    runtime: RuntimePreflight | None = None
    structural: RuntimePreflight | None = None


def _freeform_planner_conversation_context(
    message: str,
    messages: list[ComposerHistoryMessage],
) -> PlannerConversationContext | None:
    """Project bounded, authoritative earlier user requests for the planner.

    Empty-state planning receives the current message separately as its
    custody-bearing ``PlannerOriginatingMessage``. This projection carries
    only preceding user-authored requests needed to resolve a referential turn;
    assistant prose is model synthesis, not intent authority, and is excluded.
    The first request plus the seven most recent requests survive long
    histories, with an explicit omission count. The planner's existing exact
    request-byte budget remains the final provider-call bound.
    """
    if not is_referential_pipeline_mutation_intent(message):
        return None

    prior_requests: list[PlannerPriorUserRequest] = []
    for history_index, history_message in enumerate(messages):
        if type(history_message) is not dict:
            raise InvariantError("composer chat history entries must be exact dictionaries")
        # The marker is NotRequired[Literal[True]] (protocol.py): ABSENT is the
        # legitimate assistant-entry state, but any PRESENT value that is not
        # exactly True — including None — is a broken first-party contract and
        # must crash, so membership and value are checked separately rather
        # than letting a `.get()` default fold present-but-invalid into absent.
        if COMPOSER_HISTORY_USER_AUTHORED_KEY not in history_message:
            continue
        authorship = history_message[COMPOSER_HISTORY_USER_AUTHORED_KEY]
        if authorship is not True or "role" not in history_message or history_message["role"] != "user":
            raise InvariantError("composer user-authorship marker is malformed")
        content = history_message["content"] if "content" in history_message else None
        if type(content) is not str or not content.strip():
            raise InvariantError("composer user chat history content must be a non-empty exact string")
        prior_requests.append(PlannerPriorUserRequest(history_index=history_index, content=content))

    if not prior_requests:
        return None
    if len(prior_requests) <= _FREEFORM_PLANNER_PRIOR_USER_REQUEST_MAX_ITEMS:
        retained = tuple(prior_requests)
        omitted = 0
    else:
        tail_count = _FREEFORM_PLANNER_PRIOR_USER_REQUEST_MAX_ITEMS - 1
        retained = (prior_requests[0], *prior_requests[-tail_count:])
        omitted = len(prior_requests) - len(retained)
    return PlannerConversationContext(
        prior_user_requests=retained,
        additional_prior_user_requests_omitted=omitted,
    )


class PlanningApplication:
    """Own planner adaptation, custody, audit persistence and staging."""

    def __init__(
        self,
        *,
        sessions_service: SessionServiceProtocol | None,
        policy_context: PluginPolicyContextFactory,
        chargeable_admission: ComposerChargeableAdmission,
        preflight: ComposerPreflight,
        schema_disclosure: SchemaDisclosureTracker,
        settings: ComposerSettings,
        availability: ComposerAvailability,
        planner_dialect: ToolContractDialect,
        hatch_dialect: ToolContractDialect,
        advisor_provider: str,
        composer_skill_text: str,
        session_engine: Engine | None,
        secret_service: WebSecretResolver | None,
        secret_wiring_policy: SecretWiringPolicy,
        endpoint_base_url: str | None,
        endpoint_api_key: str | None,
        advisor_endpoint_base_url: str | None,
        advisor_endpoint_api_key: str | None,
    ) -> None:
        self._sessions_service_optional = sessions_service
        self._policy_context = policy_context
        self._chargeable_admission = chargeable_admission
        self._preflight = preflight
        self._schema_disclosure = schema_disclosure
        self._settings = settings
        self._availability = availability
        self._planner_dialect = planner_dialect
        self._hatch_dialect = hatch_dialect
        self._advisor_provider = advisor_provider
        self._composer_skill_text = composer_skill_text
        self._session_engine = session_engine
        self._secret_service = secret_service
        self._secret_wiring_policy = secret_wiring_policy
        self._endpoint_base_url = endpoint_base_url
        self._endpoint_api_key = endpoint_api_key
        self._advisor_endpoint_base_url = advisor_endpoint_base_url
        self._advisor_endpoint_api_key = advisor_endpoint_api_key
        self._model = settings.composer_model
        self._max_composition_turns = settings.composer_max_composition_turns
        self._max_discovery_turns = settings.composer_max_discovery_turns
        self._max_tool_calls_per_turn = settings.composer_max_tool_calls_per_turn
        self._timeout_seconds = settings.composer_timeout_seconds
        self._data_dir = str(settings.data_dir)

    @property
    def _sessions_service(self) -> SessionServiceProtocol:
        if self._sessions_service_optional is None:
            raise RuntimeError("sessions_service not wired")
        return self._sessions_service_optional

    def _planner_request_lifecycle(self, progress: ComposerProgressSink | None) -> PlannerRequestLifecycle:
        """Adapt the already-established route request envelope to the planner.

        HTTP callers have completed rate limiting and entered their in-flight
        and disconnect scopes before invoking ``compose``. The planner receives
        an explicit lifecycle object so it cannot grow an independent route
        policy; its adapters only delimit work already covered by that outer
        envelope.
        """

        async def before_start() -> None:
            return None

        @asynccontextmanager
        async def request_scope() -> AsyncIterator[None]:
            yield

        async def on_settled(_outcome: str) -> None:
            return None

        return PlannerRequestLifecycle(
            before_start=before_start,
            request_scope=request_scope,
            on_settled=on_settled,
            progress=progress,
        )

    @staticmethod
    def _require_guided_planner_operation_context(
        session_operation_context: SessionOperationContext,
        *,
        session_id: str,
    ) -> None:
        """A guided planner call runs under the route's live COMPOSE authority."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise TypeError("guided planner calls require a COMPOSE session operation context")
        if session_operation_context.fence.session_id != session_id:
            raise TypeError("guided planner session_operation_context is bound to a different session")

    async def plan_guided_full_pipeline(
        self,
        *,
        intent: str,
        current_state: CompositionState,
        originating_message: PlannerOriginatingMessage,
        base: PresentBase,
        policy_catalog: PolicyCatalogView,
        plugin_snapshot: PluginAvailabilitySnapshot,
        recorder: BufferingRecorder,
        operation_fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
        progress: ComposerProgressSink | None = None,
    ) -> tuple[PipelinePlanResult, Mapping[str, frozenset[str]]] | GuidedPlannerDecline:
        """Plan one ordinary guided-full proposal through the canonical core."""

        from elspeth.web.sessions.protocol import GuidedOperationFence

        if type(recorder) is not BufferingRecorder:
            raise TypeError("recorder must be an exact BufferingRecorder")
        self._require_guided_planner_operation_context(
            session_operation_context,
            session_id=originating_message.session_id,
        )
        await self._chargeable_admission.require(session_operation_context)
        with composer_quota_scope(self._sessions_service, session_operation_context):
            if type(operation_fence) is not GuidedOperationFence:
                raise TypeError("operation_fence must be an exact GuidedOperationFence")
            if str(operation_fence.session_id) != originating_message.session_id:
                raise AuditIntegrityError("guided-full planner operation fence targets a different session")
            if policy_catalog.snapshot is not plugin_snapshot:
                raise ValueError("plugin_snapshot_catalog_mismatch")
            if not self._availability.available:
                raise ComposerServiceError(self._availability.reason or "Composer is unavailable.")

            preview_preflight_callbacks = await self._planner_preview_preflight(
                current_state,
                user_id=originating_message.user_id,
                session_id=originating_message.session_id,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
                llm_calls=recorder.llm_calls,
            )
            # Await inside a try so a typed planner failure is logged with its
            # code+rejection_codes before re-raising to the (signed) guided route
            # (see _log_guided_planner_failure); the coroutine runs nothing until
            # awaited, so every PipelinePlannerError surfaces inside the guard.
            guided_full_planner_call = plan_pipeline(
                intent=intent,
                current_state=current_state,
                # Round-trippable planner projection (elspeth-c67fbbbd83): the
                # provider both reads this as current_state and serves it back
                # through its own get_pipeline_state palette tool, so server-owned
                # option metadata must not reach it un-projected.
                provider_current_state=project_server_owned_option_metadata(current_state.to_dict()),
                # No reviewed guided source or output exists on the guided-FULL
                # surface (reviewed_facts is empty by construction), so there is no
                # declared output contract a gap could be computed against.
                unproducible_output_fields=(),
                reviewed_facts={},
                reviewed_planner_context={},
                schemas_loaded=self._schema_disclosure.schemas_loaded_for_session(originating_message.session_id),
                mark_schema_loaded=functools.partial(self._schema_disclosure.mark_plugin_schema_loaded, originating_message.session_id),
                eligible_deferred_intent_ids=(),
                claim_evaluator=None,
                supersedes_draft_hash=None,
                surface=PlannerSurface.GUIDED_FULL,
                profile="ordinary",
                policy_catalog=policy_catalog,
                plugin_snapshot=plugin_snapshot,
                originating_message=originating_message,
                base=base,
                model_config=PlannerModelConfig(
                    completion=provider_gateway._litellm_acompletion,
                    model_identifier=self._model,
                    provider=self._availability.provider or "unknown",
                    temperature=self._settings.composer_temperature,
                    seed=self._settings.composer_seed,
                    timeout_seconds=self._timeout_seconds,
                    max_composition_turns=self._max_composition_turns,
                    max_discovery_turns=self._max_discovery_turns,
                    max_tool_calls_per_turn=self._max_tool_calls_per_turn,
                    max_api_attempts=LLM_API_MAX_ATTEMPTS,
                    api_retry_base_seconds=LLM_API_RETRY_BASE_DELAY_SECONDS,
                    discovery_reasoning_effort=self._settings.composer_discovery_reasoning_effort,
                    candidate_reasoning_effort=self._settings.composer_candidate_reasoning_effort,
                    tool_contract_dialect=self._planner_dialect,
                    escape_hatch_tool_contract_dialect=self._hatch_dialect,
                    pricing_model=self._settings.composer_pricing_model,
                    escape_hatch_model=self._settings.composer_advisor_model,
                    escape_hatch_provider=self._advisor_provider,
                    escape_hatch_pricing_model=self._settings.composer_advisor_pricing_model,
                    api_base=self._endpoint_base_url,
                    api_key=self._endpoint_api_key,
                    escape_hatch_api_base=self._advisor_endpoint_base_url,
                    escape_hatch_api_key=self._advisor_endpoint_api_key,
                ),
                rendered_skill=self._composer_skill_text,
                repair_budget=self._settings.composer_planner_repair_budget,
                budget_policy=PlannerBudgetPolicy(
                    max_total_provider_calls=self._settings.composer_planner_max_provider_calls,
                    max_request_bytes=self._settings.composer_planner_max_request_bytes,
                    max_completion_tokens=self._settings.composer_planner_max_completion_tokens,
                    max_cumulative_provider_cost=self._settings.composer_planner_max_cumulative_provider_cost,
                ),
                custody_config=PlannerCustodyConfig(
                    data_dir=self._data_dir,
                    session_engine=self._session_engine,
                    session_operation_context=session_operation_context,
                    session_operation_authority=self._sessions_service.session_operation_authority,
                    max_storage_per_session=self._settings.max_blob_storage_per_session_bytes,
                    secret_service=self._secret_service,
                    secret_wiring_policy=self._secret_wiring_policy,
                    runtime_preflight=preview_preflight_callbacks.runtime,
                    structural_preflight=preview_preflight_callbacks.structural,
                    write_fence=BlobGuidedOperationWriteFence(
                        session_id=operation_fence.session_id,
                        operation_id=operation_fence.operation_id,
                        lease_token=operation_fence.lease_token,
                        attempt=operation_fence.attempt,
                    ),
                    # Guided-full inserts its originating chat message only inside
                    # the atomic staging settlement; finalizing inline custody
                    # mid-plan violates the blob lineage FK (elspeth-1e3ad83d89).
                    defer_finalize=True,
                ),
                lifecycle=self._planner_request_lifecycle(progress),
                recorder=recorder,
                candidate_finalizer=_required_controls_candidate_finalizer(
                    policy_catalog=policy_catalog,
                    plugin_snapshot=plugin_snapshot,
                ),
            )
            try:
                plan = await guided_full_planner_call
            except PlannerDeclined as declined:
                # Honest decline: a successful conversational outcome, not a
                # planner failure. Either origin lands here — an ordinary
                # manifest-satisfied turn whose text reply led with the taught
                # DECLINE: marker, or the escape-hatch advisor turn, which
                # accepts any text.
                # Return it (rather than letting it fall into the broad
                # PipelinePlannerError handler below) so the caller can persist
                # an ordinary assistant message and complete the guided
                # operation instead of routing it into
                # GuidedOperationFailureCode — mirrors the freeform surface's
                # handling in ComposerServiceImpl.compose.
                return GuidedPlannerDecline(decline_text=declined.decline_text)
            except PipelinePlannerError as exc:
                _log_guided_planner_failure(
                    exc,
                    session_id=originating_message.session_id,
                    operation_id=str(operation_fence.operation_id),
                    surface=PlannerSurface.GUIDED_FULL.value,
                )
                raise
            return plan, {
                "source": frozenset(item.name for item in policy_catalog.list_sources()),
                "transform": frozenset(item.name for item in policy_catalog.list_transforms()),
                "sink": frozenset(item.name for item in policy_catalog.list_sinks()),
            }

    async def plan_guided_pipeline(
        self,
        *,
        intent: str,
        current_state: CompositionState,
        guided: Any,
        originating_message: PlannerOriginatingMessage,
        base: PresentBase,
        user_id: str | None,
        supersedes_draft_hash: str | None,
        recorder: BufferingRecorder,
        operation_fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
        progress: ComposerProgressSink | None = None,
        correction_target: GuidedCorrectionTarget | None = None,
        revision_authority: GuidedRevisionAuthority | None = None,
        root_goal: str | None = None,
    ) -> tuple[PipelinePlanResult, Mapping[str, frozenset[str]]] | GuidedPlannerDecline:
        """Run one shared planner call for the current guided checkpoint."""

        self._require_guided_planner_operation_context(
            session_operation_context,
            session_id=originating_message.session_id,
        )

        await self._chargeable_admission.require(session_operation_context)
        with composer_quota_scope(self._sessions_service, session_operation_context):
            from elspeth.web.composer.guided.deferred_intents import evaluate_deferred_intent_coverage
            from elspeth.web.composer.guided.planning import (
                GuidedCorrectionTarget,
                GuidedRevisionAuthority,
                bind_guided_prose_revision_candidate,
                build_guided_proposal_projection,
                guided_authorized_pipeline_schema,
                guided_private_reviewed_facts,
                guided_redacted_current_state_context,
                guided_redacted_planner_context,
                guided_revision_execution_hash,
                guided_unproducible_output_field_names,
                guided_unproducible_output_fields,
                materialize_guided_authorized_candidate,
                require_guided_proposal_correction_target_changed,
            )
            from elspeth.web.composer.guided.profile import TUTORIAL_PROFILE
            from elspeth.web.composer.guided.prompts import load_step_planner_skill
            from elspeth.web.composer.guided.stage_subjects import StatedGateRoutingConstraint, StatedPredicateConstraint
            from elspeth.web.composer.guided.state_machine import GuidedSession

            if type(guided) is not GuidedSession:
                raise TypeError("guided must be an exact GuidedSession")
            if type(recorder) is not BufferingRecorder:
                raise TypeError("recorder must be an exact BufferingRecorder")
            from elspeth.web.sessions.protocol import GuidedOperationFence

            if type(operation_fence) is not GuidedOperationFence:
                raise TypeError("operation_fence must be an exact GuidedOperationFence")
            if correction_target is not None and type(correction_target) is not GuidedCorrectionTarget:
                raise TypeError("correction_target must be an exact GuidedCorrectionTarget or None")
            if revision_authority is not None and type(revision_authority) is not GuidedRevisionAuthority:
                raise TypeError("revision_authority must be an exact GuidedRevisionAuthority or None")
            if correction_target is not None and revision_authority is not None:
                raise ValueError("guided selected correction and prose revision authority are mutually exclusive")
            if root_goal is not None and (type(root_goal) is not str or not root_goal):
                raise TypeError("root_goal must be a non-empty exact str or None")
            if root_goal is not None and correction_target is None and revision_authority is None:
                # The fresh-candidate run at the step-2 finish IS the goal being
                # requested, so there it belongs in ``intent``. The named fact
                # exists only where a LATER instruction supersedes it.
                raise ValueError("root_goal names the standing goal behind a correction or revision, not a fresh-candidate request")
            if str(operation_fence.session_id) != originating_message.session_id:
                raise AuditIntegrityError("guided planner operation fence targets a different session")
            if guided.active_proposal is not None:
                raise AuditIntegrityError("guided planning requires no active proposal")
            if guided.pending_source_intents or guided.pending_output_intents:
                raise AuditIntegrityError("guided planning requires completed reviewed source/output facts")
            if not guided.reviewed_sources or not guided.reviewed_outputs:
                raise AuditIntegrityError("guided planning requires at least one reviewed source and output")
            if not self._availability.available:
                raise ComposerServiceError(self._availability.reason or "Composer is unavailable.")

            plugin_snapshot, policy_catalog = self._policy_context.build(user_id)
            reviewed_facts = guided_private_reviewed_facts(guided)
            reviewed_context = guided_redacted_planner_context(guided)
            if correction_target is not None:
                reviewed_context = {
                    **reviewed_context,
                    "correction_target": correction_target.planner_context(),
                }
            if revision_authority is not None:
                if revision_authority.predecessor != current_state:
                    raise AuditIntegrityError("guided prose revision predecessor differs from planner current state")
                reviewed_context = {
                    **reviewed_context,
                    "revision_authority": revision_authority.planner_context(),
                }
            if root_goal is not None:
                # The session's standing goal, named and ordered rather than
                # concatenated into the request. Prepending it to ``intent`` made a
                # revision that narrows, changes, or withdraws part of the goal
                # argue against the goal inside the field that means "what is being
                # asked for now" — the default amend policy pushes the same way, so
                # the likely landing was a pipeline that kept the superseded part.
                # It also fed the deterministic request guards that parse ``intent``
                # as the current message: a threshold stated only in the goal
                # resurrected as a stated_threshold on a revision that had just
                # withdrawn it, and one stated in the revision went dark behind a
                # revocation phrase in the goal.
                #
                # Same custody class as the intent itself: the author's own words,
                # verbatim, already read by the planner on the run that produced the
                # proposal being revised.
                reviewed_context = {
                    **reviewed_context,
                    "root_goal": root_goal,
                    "root_goal_usage": (
                        "The outcome the author stated when this session started. It stays the pipeline's purpose, "
                        "but the current instruction is the request: where the instruction narrows, changes, or "
                        "withdraws part of the goal, follow the instruction."
                    ),
                }

            def evaluate_claims(candidate: CompositionState, claimed_intent_ids: tuple[str, ...]) -> tuple[str, ...]:
                required_intent_ids = tuple(
                    intent.intent_id
                    for intent in guided.deferred_intents
                    if any(
                        type(constraint) in {StatedPredicateConstraint, StatedGateRoutingConstraint} for constraint in intent.constraints
                    )
                )
                return evaluate_deferred_intent_coverage(
                    candidate=candidate,
                    reviewed_guided=guided,
                    claimed_intent_ids=claimed_intent_ids,
                    required_intent_ids=required_intent_ids,
                )

            planner_surface = PlannerSurface.TUTORIAL_PROFILE if guided.profile == TUTORIAL_PROFILE else PlannerSurface.GUIDED_STAGED
            planner_profile = "tutorial" if planner_surface is PlannerSurface.TUTORIAL_PROFILE else "ordinary"
            catalog_ids: Mapping[str, frozenset[str]] = {
                "source": frozenset(item.name for item in policy_catalog.list_sources()),
                "transform": frozenset(item.name for item in policy_catalog.list_transforms()),
                "sink": frozenset(item.name for item in policy_catalog.list_sinks()),
            }
            preview_preflight_callbacks = await self._planner_preview_preflight(
                current_state,
                user_id=user_id,
                session_id=originating_message.session_id,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
                llm_calls=recorder.llm_calls,
            )
            custody_config = PlannerCustodyConfig(
                data_dir=self._data_dir,
                session_engine=self._session_engine,
                session_operation_context=session_operation_context,
                session_operation_authority=self._sessions_service.session_operation_authority,
                max_storage_per_session=self._settings.max_blob_storage_per_session_bytes,
                secret_service=self._secret_service,
                secret_wiring_policy=self._secret_wiring_policy,
                runtime_preflight=preview_preflight_callbacks.runtime,
                structural_preflight=preview_preflight_callbacks.structural,
                write_fence=BlobGuidedOperationWriteFence(
                    session_id=operation_fence.session_id,
                    operation_id=operation_fence.operation_id,
                    lease_token=operation_fence.lease_token,
                    attempt=operation_fence.attempt,
                ),
            )

            # A zero-transform pipeline emits exactly what the reviewed source
            # carries, so a declared sink field no source can supply makes it
            # unbuildable. Validation cannot be the guard (R2-F4): the sink
            # contract check fires only when the producer participates in
            # propagation (an observed-schema source abstains under ADR-007), and
            # even then as an opaque sink_contract_violation the planner cannot
            # repair away. The gap is therefore named to the planner up front, and
            # the planner loop refuses any zero-transform candidate carrying it
            # (passthrough_cannot_produce_declared_fields). That is not a general
            # satisfiability gate — with a transform present a field may
            # legitimately be produced, and the loop's guard says nothing.
            output_field_gaps = guided_unproducible_output_fields(guided)
            unproducible_output_fields = guided_unproducible_output_field_names(guided)
            if output_field_gaps:
                # Name the gap to the provider planner rather than letting it
                # rediscover the wall by rejection. Zero new egress: the source
                # observed/declared field names and the output's required_fields
                # are already members of guided_redacted_planner_context.
                reviewed_context = {
                    **reviewed_context,
                    "unproducible_output_fields": [dict(gap) for gap in output_field_gaps],
                    # States only what is KNOWN. An earlier draft asserted the
                    # pipeline "will fail at runtime" — ELSPETH cannot know that
                    # (a source with no observed columns and an observed-mode
                    # schema has an unknown, not an empty, inventory), and the
                    # over-claim pushes the planner toward fabricating transforms
                    # to satisfy a prediction rather than closing a named gap.
                    "unproducible_output_fields_usage": (
                        "No reviewed source declares or observes these fields; a pass-through has nothing to "
                        "produce them from. Propose the transform(s) that do. The final candidate must also "
                        "preserve or produce every other reviewed output required field; adding any transform "
                        "or renaming these fields into place is not, by itself, proof of a satisfiable output contract."
                    ),
                }

            # Build the coroutine, then await inside a try so a typed planner failure
            # is logged with its code+rejection_codes before it re-raises to the
            # (signed) guided route. An ``async def`` runs nothing until awaited, so
            # every PipelinePlannerError surfaces at ``await``, inside the guard.
            pending_revision_rejection: Literal["guided_amend_contract_violation"] | None = None

            terminal_contract: PlannerTerminalContract | None = None
            if revision_authority is None:

                def materialize_guided_delta(delta: Mapping[str, Any]) -> PlannerTerminalMaterialization:
                    canonical = materialize_guided_authorized_candidate(
                        delta,
                        correction_target,
                        guided,
                        current_state,
                    )
                    config_owned_refs = {
                        *(
                            "source"
                            if guided.reviewed_sources[stable_id].name == "source"
                            else f"source:{guided.reviewed_sources[stable_id].name}"
                            for stable_id in guided.source_order
                        ),
                        *(f"output:{guided.reviewed_outputs[stable_id].name}" for stable_id in guided.output_order),
                    }
                    if correction_target is not None:
                        # Existing predecessor nodes were materialized from
                        # private server authority (even when one routing scalar
                        # was changed by the admitted delta). Mask their config
                        # facts exactly as the former finalizer-owned binder did.
                        config_owned_refs.update(f"node:{node.id}" for node in current_state.nodes)
                    return PlannerTerminalMaterialization(
                        pipeline=dict(canonical),
                        config_owned_refs=frozenset(config_owned_refs),
                    )

                terminal_contract = PlannerTerminalContract(
                    schema=guided_authorized_pipeline_schema(
                        guided,
                        correction_target=correction_target,
                    ),
                    materialize=materialize_guided_delta,
                    instruction=DELTA_PLANNER_TERMINAL_INSTRUCTION,
                )

            def bind_guided_candidate(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
                nonlocal pending_revision_rejection
                pending_revision_rejection = None
                if revision_authority is not None:
                    binding = bind_guided_prose_revision_candidate(
                        candidate,
                        guided,
                        authority=revision_authority,
                    )
                    pending_revision_rejection = binding.rejection_code
                    return binding.pipeline
                # Initial/correction deltas have already passed through
                # materialize_guided_authorized_candidate at the selected terminal
                # seam.  Rebinding here would misclassify the canonical result as
                # provider-authored authority and duplicate correction custody.
                return candidate

            candidate_acceptance: Callable[[CompositionState], None] | None = None
            if correction_target is not None or revision_authority is not None:

                def require_guided_revision_delta(candidate_state: CompositionState) -> None:
                    if pending_revision_rejection is not None:
                        raise PipelineCandidatePolicyRejection(pending_revision_rejection)
                    if revision_authority is not None and guided_revision_execution_hash(candidate_state) == guided_revision_execution_hash(
                        revision_authority.predecessor
                    ):
                        raise PipelineCandidatePolicyRejection("guided_revision_unchanged")
                    if correction_target is not None:
                        candidate_proposal = PipelineProposal.create(
                            pipeline=owned_composition_state_authority(candidate_state),
                            base=base,
                            reviewed_facts=reviewed_facts,
                            surface=planner_surface,
                            repair_count=0,
                            skill_hash=stable_hash("composer.guided-correction-candidate-check.v1"),
                            covered_deferred_intent_ids=(),
                            supersedes_draft_hash=supersedes_draft_hash,
                        )
                        candidate_projection = build_guided_proposal_projection(
                            proposal_id=base.state_id,
                            proposal=candidate_proposal,
                            guided=guided,
                            catalog_plugin_ids=catalog_ids,
                        )
                        try:
                            require_guided_proposal_correction_target_changed(
                                candidate_projection,
                                correction_target,
                                candidate_state,
                            )
                        except AuditIntegrityError as exc:
                            if str(exc) != "guided correction planner did not change the selected component":
                                raise
                            raise PipelineCandidatePolicyRejection("guided_correction_unchanged") from exc

                candidate_acceptance = require_guided_revision_delta

            guided_planner_call = plan_pipeline(
                intent=intent,
                current_state=current_state,
                provider_current_state=guided_redacted_current_state_context(current_state),
                reviewed_facts=reviewed_facts,
                reviewed_planner_context=reviewed_context,
                unproducible_output_fields=unproducible_output_fields,
                schemas_loaded=self._schema_disclosure.schemas_loaded_for_session(originating_message.session_id),
                mark_schema_loaded=functools.partial(self._schema_disclosure.mark_plugin_schema_loaded, originating_message.session_id),
                eligible_deferred_intent_ids=tuple(item.intent_id for item in guided.deferred_intents),
                claim_evaluator=evaluate_claims,
                supersedes_draft_hash=supersedes_draft_hash,
                surface=planner_surface,
                profile=planner_profile,
                policy_catalog=policy_catalog,
                plugin_snapshot=plugin_snapshot,
                originating_message=originating_message,
                base=base,
                model_config=PlannerModelConfig(
                    completion=provider_gateway._litellm_acompletion,
                    model_identifier=self._model,
                    provider=self._availability.provider or "unknown",
                    temperature=self._settings.composer_temperature,
                    seed=self._settings.composer_seed,
                    timeout_seconds=self._timeout_seconds,
                    max_composition_turns=self._max_composition_turns,
                    max_discovery_turns=self._max_discovery_turns,
                    max_tool_calls_per_turn=self._max_tool_calls_per_turn,
                    max_api_attempts=LLM_API_MAX_ATTEMPTS,
                    api_retry_base_seconds=LLM_API_RETRY_BASE_DELAY_SECONDS,
                    discovery_reasoning_effort=self._settings.composer_discovery_reasoning_effort,
                    candidate_reasoning_effort=self._settings.composer_candidate_reasoning_effort,
                    tool_contract_dialect=self._planner_dialect,
                    escape_hatch_tool_contract_dialect=self._hatch_dialect,
                    pricing_model=self._settings.composer_pricing_model,
                    escape_hatch_model=self._settings.composer_advisor_model,
                    escape_hatch_provider=self._advisor_provider,
                    escape_hatch_pricing_model=self._settings.composer_advisor_pricing_model,
                    api_base=self._endpoint_base_url,
                    api_key=self._endpoint_api_key,
                    escape_hatch_api_base=self._advisor_endpoint_base_url,
                    escape_hatch_api_key=self._advisor_endpoint_api_key,
                ),
                rendered_skill=load_step_planner_skill(guided.step),
                repair_budget=self._settings.composer_planner_repair_budget,
                budget_policy=PlannerBudgetPolicy(
                    max_total_provider_calls=self._settings.composer_planner_max_provider_calls,
                    max_request_bytes=self._settings.composer_planner_max_request_bytes,
                    max_completion_tokens=self._settings.composer_planner_max_completion_tokens,
                    max_cumulative_provider_cost=self._settings.composer_planner_max_cumulative_provider_cost,
                ),
                custody_config=custody_config,
                lifecycle=self._planner_request_lifecycle(progress),
                recorder=recorder,
                candidate_finalizer=_required_controls_candidate_finalizer(
                    policy_catalog=policy_catalog,
                    plugin_snapshot=plugin_snapshot,
                    inner=bind_guided_candidate,
                ),
                candidate_acceptance=candidate_acceptance,
                terminal_contract=terminal_contract,
            )
            try:
                plan = await guided_planner_call
            except PlannerDeclined as declined:
                # Same decline handling as plan_guided_full_pipeline above —
                # marker decline on an ordinary turn or the escape-hatch advisor
                # turn alike: a decline is a conversational outcome, not a planner
                # failure, so it must not fall into the broad
                # PipelinePlannerError handler below and must never route
                # through GuidedOperationFailureCode.
                return GuidedPlannerDecline(decline_text=declined.decline_text)
            except PipelinePlannerError as exc:
                _log_guided_planner_failure(
                    exc,
                    session_id=originating_message.session_id,
                    operation_id=str(operation_fence.operation_id),
                    surface=planner_surface.value,
                )
                raise
            return plan, catalog_ids

    async def _persist_pipeline_planner_audit(
        self,
        *,
        session_id: UUID,
        current_state_id: UUID | None,
        llm_calls: tuple[ComposerLLMCall, ...],
        planner_attempts: tuple[ComposerPlannerAttempt, ...],
        invocations: tuple[ComposerToolInvocation, ...],
        # REQUIRED (no default): the prose replies this planning request
        # refused to publish. They settle inside this cohort, never as a
        # separate write, so a caller that forgets them must fail loudly rather
        # than silently drop the model's words.
        withheld_replies: tuple[WithheldReply, ...],
        session_operation_context: SessionOperationContext | None,
    ) -> None:
        """Make planner LLM/discovery evidence durable before proposal authority.

        The whole evidence set — every physical LLM call, every value-free
        semantic response disposition, and every discovery invocation of one
        planning request — is one cohort and settles in a single
        ``add_messages_atomic`` transaction (elspeth-90231248dc). Provider
        failures can create physical ordinal gaps; every response row is
        immediately followed by its logical attempt row. A mid-write failure
        or cancellation therefore leaves the evidence either fully durable or
        cleanly absent, never a partial cohort that reads as the complete
        planning record.
        """

        from elspeth.web.sessions._persist_payload import AuditMessageDraft

        sessions = self._sessions_service
        drafts: list[AuditMessageDraft] = []
        for record in interleave_planner_audit_records(llm_calls, planner_attempts):
            if type(record) is ComposerLLMCall:
                drafts.append(
                    AuditMessageDraft(
                        role="audit",
                        content=llm_call_audit_summary(record),
                        tool_calls=(llm_call_audit_envelope(record),),
                    )
                )
            else:
                attempt = cast(ComposerPlannerAttempt, record)
                drafts.append(
                    AuditMessageDraft(
                        role="audit",
                        content=planner_attempt_audit_summary(attempt),
                        tool_calls=(planner_attempt_audit_envelope(attempt),),
                    )
                )
        for invocation in invocations:
            content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
            drafts.append(
                AuditMessageDraft(
                    role="audit",
                    content=content,
                    tool_calls=(envelope,),
                )
            )
        for withheld in withheld_replies:
            drafts.append(
                AuditMessageDraft(
                    role="audit",
                    content=withheld.content,
                    tool_calls=(withheld_reply_envelope(withheld.origin, withheld.content),),
                )
            )
        # Fenced session write (P4-D6 family A2b): the planner evidence carries
        # the compose operation the staging turn runs under.
        if session_operation_context is None:
            raise TypeError("pipeline planner audit requires the turn's session_operation_context")
        try:
            await sessions.add_messages_atomic(
                session_id,
                tuple(drafts),
                composition_state_id=current_state_id,
                writer_principal="compose_loop",
                session_operation_context=session_operation_context,
            )
        except SQLAlchemyError as exc:
            raise AuditIntegrityError("pipeline planner audit persistence failed before proposal creation") from exc

    async def _planner_preview_preflight(
        self,
        current_state: CompositionState,
        *,
        user_id: str | None,
        session_id: str,
        plugin_snapshot: PluginAvailabilitySnapshot | None,
        session_operation_context: SessionOperationContext | None = None,
        llm_calls: tuple[ComposerLLMCall, ...] = (),
    ) -> _PlannerPreviewPreflightCallbacks:
        """Stage-2 callbacks for ``preview_pipeline`` inside a planner request.

        Precompute-then-close-over, the same shape ``tool_batch`` uses for the
        compose loop: ``execute_tool`` is synchronous, so the async preflight
        is paid once here and the callback just hands back the result. That is
        sound for a planner because planner tools are DISCOVERY-ONLY —
        ``execute_discovery_tool_with_context`` refuses a mutation registry,
        and the planner raises ``AuditIntegrityError`` if any discovery call
        returns a changed ``updated_state`` — so the one state this callback
        can ever be asked about is the one preflighted here.

        Returns empty callbacks (leaving ``preview_pipeline`` on its
        fail-closed ``runtime_preflight_not_run`` branch) in the two cases
        where a verdict would be noise rather than signal:

        * a structurally empty pipeline — there is nothing to dry-run, and the
          empty-topology planner passes one by construction;
        * the preflight itself failed — a planner request must not die because
          Stage 2 broke, and the un-run tripwire already reports it honestly.

        When the strict verdict is handoff-shaped (invalid with the
        ``interpretation_review_pending`` blocker), the interpretation-
        tolerant preflight is additionally precomputed as the ``structural``
        callback so the preview surfaces the structural findings the strict
        ledger skipped (elspeth-229e9e8195). A tolerant-pass failure degrades
        to no structural callback under the same must-not-die rule — the
        block is then absent, which claims nothing.

        ``ComposerRuntimePreflightError`` is the only catch because the
        coordinator funnels every ``Exception`` (timeouts included) into that
        one envelope; ``asyncio.CancelledError`` is a ``BaseException`` and
        propagates, so a cancelled planner request still aborts.
        """
        if state_is_structurally_empty(current_state):
            return _PlannerPreviewPreflightCallbacks()
        # One request-local cache for both passes: the strict and tolerant
        # entries key separately (``interpretation_tolerant`` is in the key),
        # and the process-wide coordinator dedupes each against any
        # concurrent same-key run elsewhere.
        cache: _RuntimePreflightCache = {}
        try:
            preflight_result = await self._preflight.cached_runtime_preflight(
                current_state,
                user_id=user_id,
                session_id=session_id,
                cache=cache,
                initial_version=current_state.version,
                session_scope=f"session:{session_id}",
                llm_calls=llm_calls,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
            )
        except ComposerRuntimePreflightError:
            return _PlannerPreviewPreflightCallbacks()

        def _callback(_state: CompositionState, _result: ValidationResult = preflight_result) -> ValidationResult:
            return _result

        if not is_pending_interpretation_handoff(preflight_result):
            return _PlannerPreviewPreflightCallbacks(runtime=_callback)

        try:
            tolerant_result = await self._preflight.cached_runtime_preflight(
                current_state,
                user_id=user_id,
                session_id=session_id,
                cache=cache,
                initial_version=current_state.version,
                session_scope=f"session:{session_id}",
                llm_calls=llm_calls,
                plugin_snapshot=plugin_snapshot,
                session_operation_context=session_operation_context,
                interpretation_tolerant=True,
            )
        except ComposerRuntimePreflightError:
            return _PlannerPreviewPreflightCallbacks(runtime=_callback)

        def _structural_callback(_state: CompositionState, _result: ValidationResult = tolerant_result) -> ValidationResult:
            return _result

        return _PlannerPreviewPreflightCallbacks(runtime=_callback, structural=_structural_callback)

    async def _stage_pipeline_plan(
        self,
        *,
        plan: PipelinePlanResult,
        state: CompositionState,
        session_id: UUID,
        current_state_id: UUID | None,
        user_message_id: UUID,
        user_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
        preferences: ComposerSessionPreferencesRecord,
        recorder: BufferingRecorder,
        planner_llm_calls: tuple[ComposerLLMCall, ...],
        planner_attempts: tuple[ComposerPlannerAttempt, ...],
        planner_invocations: tuple[ComposerToolInvocation, ...],
        # REQUIRED (no default): see ``_persist_pipeline_planner_audit``.
        planner_withheld_replies: tuple[WithheldReply, ...],
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
    ) -> ComposerResult:
        """Persist planner evidence, then create one reviewable proposal row.

        ``preferences`` is the snapshot taken before planning started. Because
        the planner spends unbounded wall-clock time in provider calls, trust
        authority is re-read here — after every provider call has completed —
        and auto-commit is granted only when the snapshot and the current
        preference both say ``auto_commit``. A downgrade to
        ``explicit_approve`` mid-plan therefore always lands the proposal on
        the review path (elspeth-01d4c6e683).

        Auto-commit ALSO requires a green runtime-equivalent preflight over the
        proposed candidate state (elspeth-2ed41f0a4a). Trust authority answers
        "may this commit without review"; it does not answer "is this
        runnable", and the planner used to publish "I prepared and validated
        the requested pipeline" having measured only the Stage-1 authoring
        pass. A non-green verdict now downgrades to the review arm and says so.
        """
        if session_operation_context is None:
            raise AuditIntegrityError("Pipeline planning requires exact session operation authority")

        await self._persist_pipeline_planner_audit(
            session_id=session_id,
            current_state_id=current_state_id,
            llm_calls=planner_llm_calls,
            planner_attempts=planner_attempts,
            invocations=planner_invocations,
            withheld_replies=planner_withheld_replies,
            session_operation_context=session_operation_context,
        )
        arguments = cast(dict[str, Any], deep_thaw(plan.proposal.pipeline))
        redacted_arguments = redact_tool_call_arguments(
            "set_pipeline",
            arguments,
            telemetry=NoopRedactionTelemetry(),
        )
        summary = build_tool_proposal_summary(
            tool_name="set_pipeline",
            arguments=arguments,
            redacted_arguments=redacted_arguments,
        )
        sessions = self._sessions_service
        current_preferences = await sessions.get_composer_preferences(session_id)
        auto_commit_authorized = preferences.trust_mode == "auto_commit" and current_preferences.trust_mode == "auto_commit"

        # Stage 2 over the state this proposal WOULD produce (elspeth-2ed41f0a4a).
        #
        # ``None`` here is the fail-closed "not run" verdict, and it covers
        # three genuinely different holes on purpose — the plan carried no
        # candidate state, the preflight raised, or it timed out. What they
        # share is the only thing the announce needs to know: no evidence of
        # runnability exists, so the claim must not be made.
        #
        # This REPORTS rather than raises. A planner that produced an
        # otherwise stageable proposal must not lose it to a preflight
        # infrastructure failure or to a false red — the proposal still stages
        # and a human still reads it. The one thing withheld is the authority
        # to commit unreviewed.
        # ``ComposerRuntimePreflightError`` is the ONLY failure to catch here:
        # ``RuntimePreflightCoordinator`` converts every ``Exception`` — the
        # worker's own via ``_capture``, this caller's timeout via ``run`` —
        # into a ``RuntimePreflightFailure`` that ``_cached_runtime_preflight``
        # re-raises in that one envelope. Catching ``TimeoutError`` alongside
        # it would be dead code that implies a second live path. ``asyncio.CancelledError`` is a ``BaseException``, escapes
        # ``_capture``, and is deliberately NOT caught: a cancelled request must
        # abort, not stage a proposal on a verdict nobody waited for.
        runtime_result: ValidationResult | None = None
        if plan.candidate_state is not None:
            try:
                runtime_result = await self._preflight.cached_runtime_preflight(
                    plan.candidate_state,
                    user_id=user_id,
                    session_id=str(session_id),
                    cache={},
                    initial_version=state.version,
                    session_scope=f"session:{session_id}",
                    llm_calls=recorder.llm_calls,
                    plugin_snapshot=plugin_snapshot,
                    session_operation_context=session_operation_context,
                )
            except ComposerRuntimePreflightError:
                runtime_result = None

        # Green is the ONLY verdict that may claim validation or commit
        # unreviewed. Deliberately `is_valid`, not `readiness.completion_ready`:
        # the pending-interpretation handoff is completion-ready yet carries an
        # unresolved review card, and auto-committing it would make a state
        # canonical that no human — and no validator — ever cleared.
        preflight_green = runtime_result is not None and runtime_result.is_valid

        row, deferred = await _await_pipeline_staging_write_with_deferred_cancellation(
            sessions.create_pipeline_composition_proposal(
                session_id=session_id,
                plan=plan,
                summary=summary.summary,
                rationale=summary.rationale,
                affects=summary.affects,
                arguments_redacted_json=summary.arguments_redacted_json,
                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                composer_model_identifier=plan.model_identifier,
                composer_model_version=plan.model_version,
                composer_provider=plan.provider,
                user_message_id=user_message_id,
                session_operation_context=session_operation_context,
            )
        )
        if deferred is not None:
            if auto_commit_authorized:
                await _await_pipeline_staging_write_with_deferred_cancellation(
                    sessions.reject_pipeline_composition_proposal(
                        session_id=session_id,
                        proposal_id=row.id,
                        draft_hash=plan.proposal.draft_hash,
                        reviewed_facts={},
                        reason="request_cancelled",
                        dispatch=None,
                        actor="system:auto_reject_request_cancelled",
                        session_operation_context=session_operation_context,
                    ),
                    deferred=deferred,
                )
            raise deferred
        # Auto-commit needs BOTH authorities: the operator's trust mode (may
        # this commit without review) and a green Stage 2 (is there anything
        # worth committing). The cancellation branch above stays on the trust
        # authority alone — it asks whether a cancelled request should leave a
        # proposal behind, which is a custody question, not a readiness one.
        intent = (
            PipelineCommitIntent(proposal_id=row.id, draft_hash=plan.proposal.draft_hash)
            if auto_commit_authorized and preflight_green
            else None
        )
        # ``raw_assistant_content`` differs by arm because the two arms are
        # different acts. The green announce is the ordinary staging copy — no
        # synthesis happened, so it stays ``None``. The two non-green arms are
        # backend synthesis of a verdict-bearing notice over prose that does
        # not exist (this surface's model emitted a tool call, never prose), so
        # they carry the empty-string REPLACEMENT shape the field-pairing
        # invariant requires and that ``routes._composer_history_content``
        # reads structurally. Setting "" on the green arm instead would falsely
        # imply synthesis on a verbatim response.
        raw_assistant_content: str | None = None
        if preflight_green:
            message = PIPELINE_STAGED_AUTO_COMMIT_MESSAGE if intent is not None else PIPELINE_STAGED_REVIEW_MESSAGE
        elif runtime_result is None:
            message = PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE
            raw_assistant_content = ""
        elif is_pending_interpretation_handoff(runtime_result):
            # Split from the findings arm on the SHAPE, not on ``is_valid``:
            # both are ``is_valid=False``, but only one is a validator
            # objection. Reporting a pending review card as "issues that must
            # be fixed" sends the operator hunting for a defect that is not
            # there — the same over-claim in mirror image.
            message = PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE
            raw_assistant_content = ""
        else:
            message = PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE
            raw_assistant_content = ""
        return ComposerResult(
            message=message,
            state=state,
            runtime_preflight=runtime_result,
            raw_assistant_content=raw_assistant_content,
            pipeline_commit_intent=intent,
        )

    async def _plan_and_stage_empty_pipeline(
        self,
        *,
        message: str,
        messages: list[ComposerHistoryMessage],
        state: CompositionState,
        session_id: str,
        current_state_id: str | None,
        user_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
        progress: ComposerProgressSink | None,
        user_message_id: str,
        recorder: BufferingRecorder,
        plugin_snapshot: PluginAvailabilitySnapshot,
        policy_catalog: PolicyCatalogView,
    ) -> ComposerResult:
        """Build one canonical full-pipeline proposal for an empty topology."""

        session_uuid = UUID(session_id)
        message_uuid = UUID(user_message_id)
        state_uuid = UUID(current_state_id) if current_state_id is not None else None
        base = (
            PresentBase(state_id=state_uuid, composition_content_hash=composition_content_hash(state))
            if state_uuid is not None
            else AbsentBase()
        )
        preferences = await self._sessions_service.get_composer_preferences(session_uuid)
        rendered_skill = self._composer_skill_text
        origin = PlannerOriginatingMessage(
            session_id=session_id,
            message_id=user_message_id,
            content=message,
            user_id=user_id,
        )
        # Resolves to empty callbacks today — this surface plans an EMPTY
        # topology by construction, and the helper's structurally-empty guard
        # is the single source of that rule. Routed through it anyway so the
        # callbacks appear by themselves if this dispatch ever accepts a
        # non-empty state, rather than silently staying un-run.
        preview_preflight_callbacks = await self._planner_preview_preflight(
            state,
            user_id=user_id,
            session_id=session_id,
            plugin_snapshot=plugin_snapshot,
            session_operation_context=session_operation_context,
            llm_calls=recorder.llm_calls,
        )
        custody_config = PlannerCustodyConfig(
            data_dir=self._data_dir,
            session_engine=self._session_engine,
            session_operation_context=session_operation_context,
            session_operation_authority=self._sessions_service.session_operation_authority,
            max_storage_per_session=self._settings.max_blob_storage_per_session_bytes,
            secret_service=self._secret_service,
            secret_wiring_policy=self._secret_wiring_policy,
            runtime_preflight=preview_preflight_callbacks.runtime,
            structural_preflight=preview_preflight_callbacks.structural,
        )
        planner_llm_start = len(recorder.llm_calls)
        planner_attempt_start = len(recorder.planner_attempts)
        planner_invocation_start = len(recorder.invocations)
        planner_withheld_start = len(recorder.withheld_replies)
        try:
            plan = await plan_pipeline(
                intent=message,
                conversation_context=_freeform_planner_conversation_context(message, messages),
                current_state=state,
                # Round-trippable planner projection (elspeth-c67fbbbd83); see
                # the guided-full call site above.
                provider_current_state=project_server_owned_option_metadata(state.to_dict()),
                reviewed_facts={},
                reviewed_planner_context={},
                # Freeform has no reviewed guided output, so no operator
                # has declared a sink field contract a gap could exist
                # against.
                unproducible_output_fields=(),
                schemas_loaded=self._schema_disclosure.schemas_loaded_for_session(session_id),
                mark_schema_loaded=functools.partial(self._schema_disclosure.mark_plugin_schema_loaded, session_id),
                eligible_deferred_intent_ids=(),
                claim_evaluator=None,
                supersedes_draft_hash=None,
                surface=PlannerSurface.FREEFORM,
                profile="ordinary",
                policy_catalog=policy_catalog,
                plugin_snapshot=plugin_snapshot,
                originating_message=origin,
                base=base,
                model_config=PlannerModelConfig(
                    completion=provider_gateway._litellm_acompletion,
                    model_identifier=self._model,
                    provider=self._availability.provider or "unknown",
                    temperature=self._settings.composer_temperature,
                    seed=self._settings.composer_seed,
                    timeout_seconds=self._timeout_seconds,
                    max_composition_turns=self._max_composition_turns,
                    max_discovery_turns=self._max_discovery_turns,
                    max_tool_calls_per_turn=self._max_tool_calls_per_turn,
                    max_api_attempts=LLM_API_MAX_ATTEMPTS,
                    api_retry_base_seconds=LLM_API_RETRY_BASE_DELAY_SECONDS,
                    discovery_reasoning_effort=self._settings.composer_discovery_reasoning_effort,
                    candidate_reasoning_effort=self._settings.composer_candidate_reasoning_effort,
                    tool_contract_dialect=self._planner_dialect,
                    escape_hatch_tool_contract_dialect=self._hatch_dialect,
                    pricing_model=self._settings.composer_pricing_model,
                    escape_hatch_model=self._settings.composer_advisor_model,
                    escape_hatch_provider=self._advisor_provider,
                    escape_hatch_pricing_model=self._settings.composer_advisor_pricing_model,
                    api_base=self._endpoint_base_url,
                    api_key=self._endpoint_api_key,
                    escape_hatch_api_base=self._advisor_endpoint_base_url,
                    escape_hatch_api_key=self._advisor_endpoint_api_key,
                ),
                rendered_skill=rendered_skill,
                repair_budget=self._settings.composer_planner_repair_budget,
                budget_policy=PlannerBudgetPolicy(
                    max_total_provider_calls=self._settings.composer_planner_max_provider_calls,
                    max_request_bytes=self._settings.composer_planner_max_request_bytes,
                    max_completion_tokens=self._settings.composer_planner_max_completion_tokens,
                    max_cumulative_provider_cost=self._settings.composer_planner_max_cumulative_provider_cost,
                ),
                custody_config=custody_config,
                lifecycle=self._planner_request_lifecycle(progress),
                recorder=recorder,
                candidate_finalizer=_required_controls_candidate_finalizer(
                    policy_catalog=policy_catalog,
                    plugin_snapshot=plugin_snapshot,
                ),
            )
        except PlannerDeclined as declined:
            # Honest decline — from an ordinary manifest-satisfied turn
            # led by the taught DECLINE: marker, or from the escape-hatch
            # advisor turn, which accepts any text: a successful
            # conversational outcome, not a provider failure. Mirror the
            # success path's audit persistence, then surface the model's own
            # words as the assistant message.
            await self._persist_pipeline_planner_audit(
                session_id=session_uuid,
                current_state_id=state_uuid,
                llm_calls=recorder.llm_calls[planner_llm_start:],
                planner_attempts=recorder.planner_attempts[planner_attempt_start:],
                invocations=recorder.invocations[planner_invocation_start:],
                withheld_replies=recorder.withheld_replies[planner_withheld_start:],
                session_operation_context=session_operation_context,
            )
            decline_message = declined.decline_text.strip() or (
                "I could not find a way to build this pipeline with the available components."
            )
            return ComposerResult(message=decline_message, state=state)
        except BaseException as exc:
            exc_dict = exc.__dict__
            attached_calls = exc_dict["llm_calls"] if "llm_calls" in exc_dict else ()
            if type(attached_calls) is not tuple or any(type(call) is not ComposerLLMCall for call in attached_calls):
                raise AuditIntegrityError("pipeline planner exception carried malformed LLM audit evidence") from exc
            if attached_calls != recorder.llm_calls[planner_llm_start:]:
                raise AuditIntegrityError("pipeline planner exception carried unrelated LLM audit evidence") from exc
            attached_attempts = exc_dict["planner_attempts"] if "planner_attempts" in exc_dict else ()
            if type(attached_attempts) is not tuple or any(type(attempt) is not ComposerPlannerAttempt for attempt in attached_attempts):
                raise AuditIntegrityError("pipeline planner exception carried malformed semantic attempt evidence") from exc
            if attached_attempts != recorder.planner_attempts[planner_attempt_start:]:
                raise AuditIntegrityError("pipeline planner exception carried unrelated semantic attempt evidence") from exc
            _persisted, deferred = await _await_pipeline_staging_write_with_deferred_cancellation(
                self._persist_pipeline_planner_audit(
                    session_id=session_uuid,
                    current_state_id=state_uuid,
                    llm_calls=attached_calls,
                    planner_attempts=attached_attempts,
                    invocations=recorder.invocations[planner_invocation_start:],
                    withheld_replies=recorder.withheld_replies[planner_withheld_start:],
                    session_operation_context=session_operation_context,
                ),
                deferred=exc if type(exc) is asyncio.CancelledError else None,
            )
            exc_dict["llm_calls_durable"] = True
            if deferred is not None:
                if deferred is exc:
                    raise
                raise deferred from exc
            raise
        return await self._stage_pipeline_plan(
            plan=plan,
            state=state,
            session_id=session_uuid,
            session_operation_context=session_operation_context,
            current_state_id=state_uuid,
            user_message_id=message_uuid,
            user_id=user_id,
            preferences=preferences,
            recorder=recorder,
            planner_llm_calls=recorder.llm_calls[planner_llm_start:],
            planner_attempts=recorder.planner_attempts[planner_attempt_start:],
            planner_invocations=recorder.invocations[planner_invocation_start:],
            planner_withheld_replies=recorder.withheld_replies[planner_withheld_start:],
            plugin_snapshot=plugin_snapshot,
        )
