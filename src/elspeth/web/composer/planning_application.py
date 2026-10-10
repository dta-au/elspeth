"""Planning application for empty-state Composer requests."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, cast
from uuid import UUID

import structlog
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext
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
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.no_tool_policy import (
    is_pending_interpretation_handoff,
    is_referential_pipeline_mutation_intent,
    state_is_structurally_empty,
)
from elspeth.web.composer.pipeline_planner import (
    PipelinePlanResult,
    PlannerBudgetPolicy,
    PlannerConversationContext,
    PlannerCustodyConfig,
    PlannerDeclined,
    PlannerModelConfig,
    PlannerOriginatingMessage,
    PlannerPriorUserRequest,
    PlannerRequestLifecycle,
    plan_pipeline,
)
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PresentBase,
    composition_content_hash,
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
    ComposerSettings,
    PipelineCommitIntent,
)
from elspeth.web.composer.provider_config import LLM_API_MAX_ATTEMPTS, LLM_API_RETRY_BASE_DELAY_SECONDS
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.required_controls import wire_required_controls
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import RuntimePreflight
from elspeth.web.composer.withheld_replies import WithheldReply, withheld_reply_envelope
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_custody import (
    original_outcome_group,
    project_creation_handoff,
    read_rejection_authority,
    reject_pipeline_with_required_custody,
)
from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, RedactedPipelineArguments, SessionServiceProtocol

slog = structlog.get_logger()
_FREEFORM_PLANNER_PRIOR_USER_REQUEST_MAX_ITEMS: Final[int] = 8


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
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Planner candidate finalizer that auto-wires deployment-REQUIRED controls.

    R2-F10 (elspeth-f99655f540): the planner runs the
    ``wire_required_controls`` pass on its terminal candidate so uncovered
    graphs are repaired server-side (with acknowledgeable disclosure) instead
    of shipping into the execution-time required-control block. The pass is
    idempotent, so re-finalizing a covered candidate is a no-op.
    """

    def finalize(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        return wire_required_controls(candidate, plugin_snapshot, policy_catalog)

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
        required_work: RequiredWorkBinding | None = None,
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
        audit_sql = audit_projection = None
        if required_work is not None:
            required_work.validate_context(session_operation_context)
            audit_sql, audit_projection = required_work.reserve_pair(
                RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL, RequiredWorkSource.REQUIRED_UNWIND_AUDIT_PROJECTION
            )
        try:
            await sessions.add_messages_atomic(
                session_id,
                tuple(drafts),
                composition_state_id=current_state_id,
                writer_principal="compose_loop",
                session_operation_context=session_operation_context,
                required_work=audit_sql,
            )
        except SQLAlchemyError as exc:
            if audit_projection is not None and audit_sql is not None and audit_sql.complete:
                audit_projection.complete_without_submission()
            raise AuditIntegrityError("pipeline planner audit persistence failed before proposal creation") from exc
        except BaseException:
            if audit_projection is not None and audit_sql is not None and audit_sql.complete:
                audit_projection.complete_without_submission()
            raise
        if audit_projection is not None:
            audit_projection.begin_projection()
            audit_projection.complete_owned()

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
        required_work: RequiredWorkBinding | None = None,
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
            required_work=required_work,
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

        if required_work is None:
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
                            reason="request_cancelled",
                            dispatch=None,
                            actor="system:auto_reject_request_cancelled",
                            session_operation_context=session_operation_context,
                        ),
                        deferred=deferred,
                    )
                raise deferred
        else:
            required_work.validate_context(session_operation_context)
            redacted_pipeline_arguments = RedactedPipelineArguments(summary.arguments_redacted_json)
            creation_sql, creation_projection = required_work.reserve_pair(
                RequiredWorkSource.PROPOSAL_CREATION_SQL,
                RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
            )
            required_work.coordinator.validate_creation_work(
                creation_ticket=creation_sql,
                projection_ticket=creation_projection,
                transition_ordinal=required_work.transition_ordinal,
                semantic_ordinal=required_work.semantic_ordinal,
            )
            if user_id is None:
                missing_principal = AuditIntegrityError("Required planner staging omitted its authenticated principal")
                creation_sql.complete_without_submission(missing_principal)
                creation_projection.complete_without_submission(missing_principal)
                raise missing_principal
            handoff = await sessions.create_pipeline_composition_proposal_finish_once(
                session_id=session_id,
                plan=plan,
                summary=summary.summary,
                rationale=summary.rationale,
                affects=summary.affects,
                arguments_redacted_json=redacted_pipeline_arguments,
                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                composer_model_identifier=plan.model_identifier,
                composer_model_version=plan.model_version,
                composer_provider=plan.provider,
                user_message_id=user_message_id,
                session_operation_context=session_operation_context,
                required_work=creation_sql,
                running=required_work.running,
            )
            required_row, creation_failures, creation_cancellations = project_creation_handoff(
                required_work,
                creation_sql,
                creation_projection,
                handoff,
            )
            if required_row is None:
                raise original_outcome_group(
                    "Actual creation failure and original cancellation", *creation_failures, *creation_cancellations
                )
            row = required_row
            originals: list[BaseException] = [*creation_failures, *creation_cancellations]
            if creation_cancellations and auto_commit_authorized:
                try:
                    authority, read_cancellations = await read_rejection_authority(
                        sessions,
                        binding=required_work,
                        session_id=session_id,
                        proposal_id=row.id,
                    )
                    originals.extend(read_cancellations)
                    child, producer = required_work.coordinator.begin_proposal_child(
                        str(row.id),
                        row.tool_call_id,
                        transition_ordinal=required_work.transition_ordinal,
                        semantic_ordinal=required_work.semantic_ordinal,
                    )
                    child_binding = RequiredWorkBinding(
                        child,
                        required_work.transition_ordinal,
                        required_work.semantic_ordinal,
                        required_work.role,
                        required_work.running,
                    )
                    try:
                        await reject_pipeline_with_required_custody(
                            sessions,
                            expected=PipelineRejectionExpected(
                                authority,
                                "request_cancelled",
                                None,
                                "system:auto_reject_request_cancelled",
                                user_id,
                                session_operation_context,
                                required_work.running,
                            ),
                            binding=child_binding,
                        )
                    except BaseException as rejection_error:
                        originals.append(rejection_error)
                        retained = original_outcome_group("Planner creation/rejection original outcomes", *originals)
                        try:
                            child.assert_completed()
                        except BaseException as incomplete:
                            raise original_outcome_group("Planner original and unresolved rejection child", retained, incomplete) from None
                        required_work.coordinator.complete_proposal_child(child, producer, retained)
                    else:
                        required_work.coordinator.complete_proposal_child(child, producer)
                except BaseException as cleanup_error:
                    originals.append(cleanup_error)
            if originals:
                raise original_outcome_group("Planner actual creation and rejection original outcomes", *originals) from None
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
        budget_seconds: float | None = None,
        required_work: RequiredWorkBinding | None = None,
        provider_owner: ProviderInvocationOwner | None = None,
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
                # Round-trippable planner projection (elspeth-c67fbbbd83).
                provider_current_state=project_server_owned_option_metadata(state.to_dict()),
                schemas_loaded=self._schema_disclosure.schemas_loaded_for_session(session_id),
                mark_schema_loaded=functools.partial(self._schema_disclosure.mark_plugin_schema_loaded, session_id),
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
                    timeout_seconds=self._timeout_seconds if budget_seconds is None else budget_seconds,
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
                provider_service=self._sessions_service if provider_owner is not None else None,
                provider_owner=provider_owner,
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
                required_work=required_work,
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
                    required_work=required_work,
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
            required_work=required_work,
        )
