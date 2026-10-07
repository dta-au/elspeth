from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
from typing import Annotated
from uuid import uuid4

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.compartments import compartment_ingress_record
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.required_controls import (
    merge_required_control_affected_components,
    wire_required_controls_state,
)
from elspeth.web.composer.tools import is_approval_required_blob_store_only_mutation_tool
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_custody import (
    close_required_proposal_lease,
    original_outcome_group,
    raise_required_proposal_failure,
    read_composition_rejection_authority,
    reject_pipeline_with_required_custody,
)
from elspeth.web.sessions.protocol import ProposalStateConflictError, StaleComposeStateError

from .._helpers import (
    UUID,
    AcceptProposalRequest,
    Any,
    APIRouter,
    CompositionProposalRecord,
    CompositionProposalResponse,
    Depends,
    HTTPException,
    Mapping,
    ProposalEventResponse,
    ProposalLifecycleStatus,
    Query,
    RejectProposalRequest,
    Request,
    SessionServiceProtocol,
    UserIdentity,
    _composition_proposal_response,
    _get_session_compose_lock_registry,
    _initial_composition_state,
    _log_last_resort_diagnostic,
    _proposal_event_response,
    _state_data_from_composer_state,
    _state_from_record,
    _verify_session_ownership,
    asyncio,
    cast,
    deep_thaw,
    execute_tool,
    merge_composer_meta_updates,
    require_pipeline_user,
    run_sync_in_worker,
    slog,
)
from .pipeline_settlement import (
    _await_with_deferred_cancellation,
    _proposal_chat_ingress_inputs,
    _proposal_user_message_content,
    settle_pipeline_proposal_under_compose_lock,
)

router = APIRouter()


async def _drain_proposal_lease_close(
    lease: SessionOperationLease,
) -> tuple[BaseException | None, bool]:
    """Drain lease closure and report both its outcome and caller cancellation."""
    close_task = asyncio.ensure_future(lease.close())
    caller_task = asyncio.current_task()
    cancellation_deferred = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as cancellation:
            if caller_task is None or caller_task.cancelling() == 0:
                return cancellation, cancellation_deferred
            cancellation_deferred = True
        except BaseException:
            break
    try:
        close_task.result()
    except BaseException as cleanup_error:
        return cleanup_error, cancellation_deferred
    return None, cancellation_deferred


async def _close_proposal_lease_before_commit(
    lease: SessionOperationLease,
    *,
    primary: BaseException,
) -> None:
    """Close a precommit lease without replacing the primary failure."""
    cleanup_error, cleanup_cancelled = await _drain_proposal_lease_close(lease)
    if cleanup_error is not None and cleanup_error is not primary:
        _log_last_resort_diagnostic(
            slog.error,
            "composer_proposal_precommit_cleanup_failed",
            session_id=lease.context.fence.session_id,
            operation_id=lease.context.fence.operation_id,
            exc_class=type(cleanup_error).__name__,
        )
        primary.add_note(f"Composer proposal lease cleanup also failed with {type(cleanup_error).__name__}.")
    if cleanup_cancelled and not isinstance(primary, asyncio.CancelledError):
        primary.add_note("Composer proposal lease cleanup also observed request cancellation.")


async def _close_proposal_lease_after_commit(
    lease: SessionOperationLease,
    *,
    session_id: UUID,
    event: str = "composer_proposal_reject_postcommit_cleanup_failed",
) -> bool:
    """Drain cleanup after a durable transition without masking success."""
    cleanup_error, cleanup_cancelled = await _drain_proposal_lease_close(lease)
    if cleanup_error is None:
        return cleanup_cancelled
    if isinstance(cleanup_error, contract_errors.TIER_1_ERRORS):
        raise cleanup_error
    if isinstance(cleanup_error, Exception):
        _log_last_resort_diagnostic(
            slog.error,
            event,
            session_id=str(session_id),
            exc_class=type(cleanup_error).__name__,
        )
        return cleanup_cancelled
    raise cleanup_error


@observation_boundary(
    tier=3,
    source="persisted LLM tool-call arguments of a stored CompositionProposalRecord (Tier-3 on read-back)",
    source_param="arguments",
    suppresses=("R5",),
    invariant="returns None on any absent/wrong-typed branch of arguments.source.inline_blob.content; never raises on arguments",
)
def _inline_blob_content_for_proposal(
    proposal: CompositionProposalRecord,
    arguments: Mapping[str, Any],
) -> str | None:
    """Return inline blob content that accept replay would persist, if any."""
    if proposal.tool_name != "set_pipeline":
        return None
    source = arguments["source"] if "source" in arguments else None
    if not isinstance(source, Mapping):
        return None
    inline_blob = source["inline_blob"] if "inline_blob" in source else None
    if not isinstance(inline_blob, Mapping):
        return None
    content = inline_blob["content"] if "content" in inline_blob else None
    return content if isinstance(content, str) else None


def _missing_proposal_composer_context(
    proposal: CompositionProposalRecord,
    *,
    user_message_content: str | None,
) -> tuple[str, ...]:
    context_fields = (
        ("composer_model_identifier", proposal.composer_model_identifier),
        ("composer_model_version", proposal.composer_model_version),
        ("composer_provider", proposal.composer_provider),
        ("composer_skill_hash", proposal.composer_skill_hash),
        ("tool_arguments_hash", proposal.tool_arguments_hash),
    )
    missing = [name for name, value in context_fields if value is None]
    if proposal.composer_provider == "server":
        # A server-authored graph is never valid composer provenance.
        missing.append("composer_provider (provider='server' is never valid provenance)")
    if user_message_content is None:
        missing.insert(0, "user_message_content")
    return tuple(missing)


def _ensure_inline_blob_proposal_context(
    proposal: CompositionProposalRecord,
    arguments: Mapping[str, Any],
    *,
    user_message_content: str | None,
) -> None:
    inline_blob_content = _inline_blob_content_for_proposal(proposal, arguments)
    if inline_blob_content is None:
        return
    if user_message_content is not None and inline_blob_content and inline_blob_content in user_message_content:
        return
    missing = _missing_proposal_composer_context(proposal, user_message_content=user_message_content)
    if not missing:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "Accepted proposal is missing composer provenance required for inline-blob source writes "
            f"({', '.join(missing)}). Ask ELSPETH to regenerate the proposal."
        ),
    )


def _accept_runtime_preflight_failure(proposal: CompositionProposalRecord) -> HTTPException:
    """Name a settle-time runtime-preflight failure without committing anything.

    ``_state_data_from_composer_state(preflight_exception_policy="raise")``
    has already recorded the exception telemetry. This is a server failure,
    not a proposal lifecycle conflict. The same exception class is a
    structured 500 on the compose and message routes. The wrapped exception's
    text stays server-side.
    """
    return HTTPException(
        status_code=500,
        detail={
            "detail": (
                "Runtime preflight could not complete, so the proposal was not applied and was left pending. Try accepting it again."
            ),
            "error_type": "runtime_preflight_failed",
            "tool_name": proposal.tool_name,
        },
    )


async def _await_accept_state_data[T](
    awaitable: Awaitable[T],
    *,
    proposal: CompositionProposalRecord,
) -> tuple[T, bool]:
    """Await accept-time state preparation, naming a runtime-preflight failure.

    The raised ``HTTPException`` reaches the route's precommit handler, which
    closes the lease before re-raising, exactly as the other pre-commit
    refusals do.
    """
    try:
        return await _await_with_deferred_cancellation(awaitable)
    except ComposerRuntimePreflightError as preflight_error:
        raise _accept_runtime_preflight_failure(proposal) from preflight_error


@router.get(
    "/{session_id}/proposals",
    response_model=list[CompositionProposalResponse],
)
async def list_composition_proposals(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    status: ProposalLifecycleStatus | None = Query(None),  # noqa: B008
) -> list[CompositionProposalResponse]:
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    proposals = await service.list_composition_proposals(session.id, status=status)
    return [_composition_proposal_response(proposal) for proposal in proposals]


@router.get(
    "/{session_id}/proposal-events",
    response_model=list[ProposalEventResponse],
)
async def list_proposal_events(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> list[ProposalEventResponse]:
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    events = await service.list_proposal_events(session.id)
    return [_proposal_event_response(event) for event in events]


@router.post(
    "/{session_id}/proposals/{proposal_id}/accept",
    response_model=CompositionProposalResponse,
)
async def accept_composition_proposal(
    session_id: UUID,
    proposal_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    body: AcceptProposalRequest | None = None,
) -> CompositionProposalResponse:
    session = await _verify_session_ownership(session_id, user, request)
    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
    async with compose_lock:
        service: SessionServiceProtocol = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session.id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        try:
            proposal_authority = await service.get_authoritative_composition_proposal(
                session_id=session.id,
                proposal_id=proposal_id,
            )
        except KeyError:
            primary = HTTPException(status_code=404, detail="Proposal not found")
            await _close_proposal_lease_before_commit(lease, primary=primary)
            raise primary from None
        except BaseException as primary:
            await _close_proposal_lease_before_commit(lease, primary=primary)
            raise

        proposal = proposal_authority.row
        pipeline_authority = proposal_authority.pipeline
        if pipeline_authority is not None:
            required_work = RequiredWorkCoordinator(
                RequiredWorkAuthority(
                    RequiredAuthorityKind.MANUAL_PROPOSAL,
                    lease.context,
                    proposal_id=str(proposal.id),
                    invocation_id=str(uuid4()),
                    tool_call_id=proposal.tool_call_id,
                )
            )
            lease.bind_required_work(required_work)
            if body is None or body.draft_hash is None:
                request_error = HTTPException(
                    status_code=422,
                    detail="Canonical pipeline proposal acceptance requires draft_hash.",
                )
                cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
                if cleanup:
                    raise original_outcome_group("Manual acceptance refusal and exact close originals", request_error, *cleanup)
                raise request_error
            try:
                route_settlement = await settle_pipeline_proposal_under_compose_lock(
                    services=composer_app_services(request.app),
                    user_id=user.user_id,
                    commit_timeout_seconds=request.app.state.settings.composer_sync_timeout_seconds,
                    authority=pipeline_authority,
                    draft_hash=body.draft_hash,
                    session_operation_context=lease.context,
                    required_work=required_work,
                    required_binding=RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN),
                )
            except ComposerRuntimePreflightError as preflight_error:
                preflight_failure = _accept_runtime_preflight_failure(proposal)
                cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
                if cleanup:
                    raise original_outcome_group(
                        "Manual acceptance preflight and exact close originals", preflight_error, preflight_failure, *cleanup
                    ) from preflight_error
                raise preflight_failure from preflight_error
            except BaseException as primary:
                cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
                if cleanup:
                    raise_required_proposal_failure(
                        required_work,
                        original_outcome_group("Manual acceptance original and required lease close", primary, *cleanup),
                        lease=lease,
                    )
                raise_required_proposal_failure(required_work, primary, lease=lease)
            cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
            if cleanup:
                raise_required_proposal_failure(
                    required_work,
                    original_outcome_group("Manual accepted publication and exact lease close originals", *cleanup),
                    lease=lease,
                )
            return _composition_proposal_response(route_settlement.settlement.proposal)

        durable_transition = False
        cancellation_deferred = False
        try:
            blob_effect_applied = False
            if is_approval_required_blob_store_only_mutation_tool(proposal.tool_name):
                blob_effect_applied = await service.has_applied_blob_proposal_effect(
                    session_id=session.id,
                    proposal_id=proposal.id,
                    session_operation_context=lease.context,
                )
            if proposal.status == "committed" and blob_effect_applied:
                durable_transition = True
                response = _composition_proposal_response(proposal)
                cleanup_cancelled = await _close_proposal_lease_after_commit(
                    lease,
                    session_id=session.id,
                    event="composer_proposal_accept_postcommit_cleanup_failed",
                )
                if cleanup_cancelled:
                    raise asyncio.CancelledError
                return response
            if proposal.status != "pending":
                raise HTTPException(
                    status_code=409,
                    detail="Only pending proposals can be accepted.",
                )

            current_record = await service.get_current_state(session.id)
            if (
                proposal.base_state_id is not None
                and (current_record is None or current_record.id != proposal.base_state_id)
                and not (blob_effect_applied and current_record is not None)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_type": "proposal_base_state_changed",
                        "detail": "The session state changed after this proposal was created. Ask ELSPETH to rebase the proposal.",
                    },
                )
            current_state = _state_from_record(current_record) if current_record is not None else _initial_composition_state()
            arguments = cast(dict[str, Any], deep_thaw(proposal.arguments_json))
            user_message_content = await _proposal_user_message_content(service, proposal)
            chat_ingress_inputs = await _proposal_chat_ingress_inputs(
                service, proposal, own_compartment_id=request.app.state.settings.compartment_id
            )
            proposal_composer_meta = merge_composer_meta_updates(
                current_record.composer_meta if current_record is not None else None,
                {
                    "ingress": compartment_ingress_record(
                        user_message_content, own_compartment_id=request.app.state.settings.compartment_id
                    ),
                    "chat_ingress_inputs": chat_ingress_inputs,
                }
                if user_message_content is not None
                else {},
            )
            _ensure_inline_blob_proposal_context(
                proposal,
                arguments,
                user_message_content=user_message_content,
            )
            plugin_snapshot = request.app.state.plugin_snapshot_factory(user)
            policy_catalog = PolicyCatalogView(
                request.app.state.catalog_service,
                plugin_snapshot,
                request.app.state.operator_profile_registry,
            )
            if blob_effect_applied:
                accepted_state = None
                if current_record is None:
                    (accepted_state, _validation), was_cancelled = await _await_accept_state_data(
                        _state_data_from_composer_state(
                            current_state,
                            settings=request.app.state.settings,
                            secret_service=request.app.state.scoped_secret_resolver,
                            user_id=str(user.user_id),
                            session_id=session.id,
                            plugin_snapshot=plugin_snapshot,
                            profile_registry=request.app.state.operator_profile_registry,
                            catalog=request.app.state.catalog_service,
                            runtime_preflight=None,
                            preflight_exception_policy="raise",
                            initial_version=current_state.version,
                            telemetry_source="compose",
                            composer_meta=proposal_composer_meta,
                        ),
                        proposal=proposal,
                    )
                    cancellation_deferred = cancellation_deferred or was_cancelled
                committed, was_cancelled = await _await_with_deferred_cancellation(
                    service.accept_composition_proposal(
                        session_id=session.id,
                        proposal_id=proposal.id,
                        expected_current_state_id=current_record.id if current_record is not None else None,
                        state=accepted_state,
                        actor=f"user:{user.user_id}",
                        session_operation_context=lease.context,
                    )
                )
                cancellation_deferred = cancellation_deferred or was_cancelled
                durable_transition = True
                response = _composition_proposal_response(committed)
                cleanup_cancelled = await _close_proposal_lease_after_commit(
                    lease,
                    session_id=session.id,
                    event="composer_proposal_accept_postcommit_cleanup_failed",
                )
                if cancellation_deferred or cleanup_cancelled:
                    raise asyncio.CancelledError
                return response
            result, was_cancelled = await _await_with_deferred_cancellation(
                run_sync_in_worker(
                    execute_tool,
                    proposal.tool_name,
                    arguments,
                    current_state,
                    policy_catalog,
                    plugin_snapshot=plugin_snapshot,
                    data_dir=str(request.app.state.settings.data_dir),
                    session_engine=request.app.state.session_engine,
                    session_id=str(session.id),
                    session_operation_context=lease.context,
                    session_operation_authority=service.session_operation_authority,
                    secret_service=request.app.state.scoped_secret_resolver,
                    user_id=str(user.user_id),
                    user_message_id=str(proposal.user_message_id) if proposal.user_message_id is not None else None,
                    user_message_content=user_message_content,
                    composer_model_identifier=proposal.composer_model_identifier,
                    composer_model_version=proposal.composer_model_version,
                    composer_provider=proposal.composer_provider,
                    composer_skill_hash=proposal.composer_skill_hash,
                    tool_arguments_hash=proposal.tool_arguments_hash,
                    executing_proposal_id=str(proposal.id),
                    validate_arguments=True,
                    require_data_dir_for_paths=True,
                )
            )
            cancellation_deferred = cancellation_deferred or was_cancelled
            if result.success and result.updated_state.version > current_state.version and result.validation.is_valid:
                finalized_state, was_cancelled = await _await_with_deferred_cancellation(
                    run_sync_in_worker(
                        wire_required_controls_state,
                        result.updated_state,
                        plugin_snapshot,
                        policy_catalog,
                    )
                )
                cancellation_deferred = cancellation_deferred or was_cancelled
                if finalized_state is not result.updated_state:
                    finalized_validation, was_cancelled = await _await_with_deferred_cancellation(
                        run_sync_in_worker(
                            policy_catalog.validate_composition_state,
                            finalized_state,
                        )
                    )
                    cancellation_deferred = cancellation_deferred or was_cancelled
                    if not finalized_validation.validation.is_valid:
                        raise AuditIntegrityError("Required-control proposal finalization produced an invalid composition")
                    result = replace(
                        result,
                        updated_state=finalized_validation.authored_state,
                        validation=finalized_validation.validation,
                        affected_nodes=merge_required_control_affected_components(
                            result.affected_nodes,
                            result.updated_state,
                            finalized_validation.authored_state,
                        ),
                    )

            accepted_state = None
            if result.updated_state.version == current_state.version:
                # The tool ran but did not advance composition state. The route
                # used to return a single uninformative 409 here regardless of
                # whether the tool succeeded with no-op or failed semantically.
                # Distinguish the cases so the operator sees the actual reason
                # — a generic "did not change composition state" leaves a user
                # with no path forward (session f613306b-… 2026-05-14: the LLM
                # emitted a set_pipeline with no `options` blocks on any node;
                # the validator rejected it; the 409 said only that state
                # didn't change, marking the proposal as "stale" in the
                # frontend without revealing the validation errors that were
                # the actual blocker).
                if not result.success:
                    # Helper rejections lead with their own detail. Standing-state
                    # errors on a general failure do not identify this rejection.
                    error_summary = "Composer proposal failed validation."
                    if result.validation.errors and result.validation.errors[0].component == "rejected_mutation":
                        error_summary = result.validation.errors[0].message or error_summary
                    validation_errors_payload = (
                        [{"component": entry.component, "message": entry.message} for entry in result.validation.errors]
                        if result.validation is not None
                        else []
                    )
                    # Auto-reject the proposal. It is structurally unacceptable
                    # in its current form — the LLM emitted invalid arguments
                    # that the runtime validator rejects, and the proposal
                    # cannot become acceptable without the composer producing
                    # a fresh, corrected proposal. Leaving it pending causes
                    # the "refresh asks me to reapprove" friction reported by
                    # the operator on 2026-05-14: in-memory frontend stale
                    # marking is lost on page reload, so the broken proposal
                    # re-surfaces in the pending banner. Marking as rejected
                    # server-side keeps the audit trail honest (the user
                    # clicked Accept; the server recorded why it could not
                    # apply; the proposal is terminal). Audit attribution
                    # records the system as the rejecting actor so the trail
                    # distinguishes operator-driven rejection from this
                    # automatic-on-validation-failure path.
                    # Control-flow sentinel, not a swallowed error. The
                    # route-level session compose lock serializes normal
                    # Accept/reject HTTP handlers, but non-route callers can
                    # still transition the proposal between our load above and
                    # this defensive auto-reject write. ``reject_composition_proposal``
                    # raises ``ProposalStateConflictError`` for that terminal-state case.
                    # When that fires, the desired end state — the proposal is no
                    # longer pending — is already satisfied, and the winning
                    # transition recorded its own lifecycle event.
                    # Fall through to the 422 below, which surfaces the real
                    # validation failure to the operator. We suppress ONLY
                    # ``ProposalStateConflictError`` (the benign status-race signal); we deliberately
                    # do NOT suppress ``KeyError`` (proposal row missing): the row
                    # was loaded successfully above and proposals are never
                    # hard-deleted, so a missing row is corruption of our own data
                    # and must crash rather than be swallowed.
                    try:
                        _rejected, was_cancelled = await _await_with_deferred_cancellation(
                            service.reject_composition_proposal(
                                session_id=session.id,
                                proposal_id=proposal.id,
                                actor=f"system:auto_reject_validation_failed:user:{user.user_id}",
                                session_operation_context=lease.context,
                            )
                        )
                        cancellation_deferred = cancellation_deferred or was_cancelled
                        durable_transition = True
                    except ProposalStateConflictError:
                        pass
                    if cancellation_deferred:
                        raise asyncio.CancelledError
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "detail": (
                                f"The composer's proposed change could not be applied: {error_summary} "
                                "The proposal has been automatically rejected. Ask the composer to revise and resubmit."
                            ),
                            "error_type": "proposal_validation_failed",
                            "tool_name": proposal.tool_name,
                            "validation_errors": validation_errors_payload,
                        },
                    )
                if not is_approval_required_blob_store_only_mutation_tool(proposal.tool_name):
                    raise HTTPException(
                        status_code=409,
                        detail="Accepted proposal did not change composition state.",
                    )
                if current_record is None:
                    (accepted_state, _validation), was_cancelled = await _await_accept_state_data(
                        _state_data_from_composer_state(
                            current_state,
                            settings=request.app.state.settings,
                            secret_service=request.app.state.scoped_secret_resolver,
                            user_id=str(user.user_id),
                            session_id=session.id,
                            plugin_snapshot=plugin_snapshot,
                            profile_registry=request.app.state.operator_profile_registry,
                            catalog=request.app.state.catalog_service,
                            runtime_preflight=result.runtime_preflight,
                            preflight_exception_policy="raise",
                            initial_version=current_state.version,
                            telemetry_source="compose",
                            composer_meta=proposal_composer_meta,
                        ),
                        proposal=proposal,
                    )
                    cancellation_deferred = cancellation_deferred or was_cancelled
            else:
                (accepted_state, _validation), was_cancelled = await _await_accept_state_data(
                    _state_data_from_composer_state(
                        result.updated_state,
                        settings=request.app.state.settings,
                        secret_service=request.app.state.scoped_secret_resolver,
                        user_id=str(user.user_id),
                        session_id=session.id,
                        plugin_snapshot=plugin_snapshot,
                        profile_registry=request.app.state.operator_profile_registry,
                        catalog=request.app.state.catalog_service,
                        runtime_preflight=result.runtime_preflight,
                        preflight_exception_policy="raise",
                        initial_version=current_state.version,
                        telemetry_source="compose",
                        composer_meta=proposal_composer_meta,
                    ),
                    proposal=proposal,
                )
                cancellation_deferred = cancellation_deferred or was_cancelled

            try:
                committed, was_cancelled = await _await_with_deferred_cancellation(
                    service.accept_composition_proposal(
                        session_id=session.id,
                        proposal_id=proposal.id,
                        expected_current_state_id=current_record.id if current_record is not None else None,
                        state=accepted_state,
                        actor=f"user:{user.user_id}",
                        session_operation_context=lease.context,
                    )
                )
                cancellation_deferred = cancellation_deferred or was_cancelled
            except KeyError:
                raise HTTPException(status_code=404, detail="Proposal not found") from None
            except (StaleComposeStateError, ValueError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

            durable_transition = True
            response = _composition_proposal_response(committed)
        except BaseException as primary:
            if durable_transition:
                cleanup_cancelled = await _close_proposal_lease_after_commit(
                    lease,
                    session_id=session.id,
                    event="composer_proposal_accept_postcommit_cleanup_failed",
                )
                if cleanup_cancelled and not isinstance(primary, asyncio.CancelledError):
                    raise asyncio.CancelledError from primary
            else:
                await _close_proposal_lease_before_commit(lease, primary=primary)
            raise

        cleanup_cancelled = await _close_proposal_lease_after_commit(
            lease,
            session_id=session.id,
            event="composer_proposal_accept_postcommit_cleanup_failed",
        )
        if cancellation_deferred or cleanup_cancelled:
            raise asyncio.CancelledError
        return response


@router.post(
    "/{session_id}/proposals/{proposal_id}/reject",
    response_model=CompositionProposalResponse,
)
async def reject_composition_proposal(
    session_id: UUID,
    proposal_id: UUID,
    body: RejectProposalRequest,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionProposalResponse:
    session = await _verify_session_ownership(session_id, user, request)
    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
    async with compose_lock:
        service: SessionServiceProtocol = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session.id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        required_work = RequiredWorkCoordinator(
            RequiredWorkAuthority(
                RequiredAuthorityKind.MANUAL_PROPOSAL,
                lease.context,
                proposal_id=str(proposal_id),
                invocation_id=str(uuid4()),
            )
        )
        lease.bind_required_work(required_work)
        binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN)
        preparation_failure: BaseException | None = None
        try:
            authority, preparation_cancellations = await read_composition_rejection_authority(
                service,
                binding=binding,
                session_id=session.id,
                proposal_id=proposal_id,
            )
        except BaseException as preparation_error:
            cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
            if cleanup:
                preparation_failure = original_outcome_group(
                    "Manual rejection preparation and required close originals", preparation_error, *cleanup
                )
                if isinstance(preparation_failure, BaseExceptionGroup):
                    raise preparation_failure from preparation_error
            else:
                if isinstance(preparation_error, KeyError):
                    raise HTTPException(status_code=404, detail="Proposal not found") from preparation_error
                raise
        if preparation_failure is not None:
            raise preparation_failure
        if authority.pipeline is None:
            try:
                proposal, was_cancelled = await _await_with_deferred_cancellation(
                    service.reject_composition_proposal(
                        session_id=session.id,
                        proposal_id=proposal_id,
                        actor=f"user:{user.user_id}",
                        session_operation_context=lease.context,
                    )
                )
            except ValueError as legacy_error:
                await _close_proposal_lease_before_commit(lease, primary=legacy_error)
                raise HTTPException(status_code=409, detail=str(legacy_error)) from legacy_error
            except BaseException as legacy_error:
                await _close_proposal_lease_before_commit(lease, primary=legacy_error)
                raise
            _ = body
            response = _composition_proposal_response(proposal)
            cleanup_cancelled = await _close_proposal_lease_after_commit(
                lease,
                session_id=session.id,
            )
            if preparation_cancellations:
                raise original_outcome_group("Manual nonpipeline preparation original cancellations", *preparation_cancellations)
            if was_cancelled or cleanup_cancelled:
                raise asyncio.CancelledError
            return response

        primary: BaseException | None = None
        pipeline_proposal: CompositionProposalRecord | None = None
        try:
            child, producer = required_work.begin_proposal_child(
                str(authority.row.id),
                authority.row.tool_call_id,
                transition_ordinal=0,
                semantic_ordinal=0,
            )
            child_binding = RequiredWorkBinding(child, 0, 0, RequiredWorkRole.TURN)
            try:
                pipeline_proposal = await reject_pipeline_with_required_custody(
                    service,
                    expected=PipelineRejectionExpected(
                        authority.pipeline,
                        "operator_rejected",
                        None,
                        f"user:{user.user_id}",
                        user.user_id,
                        lease.context,
                        None,
                    ),
                    binding=child_binding,
                )
                if preparation_cancellations:
                    raise original_outcome_group("Manual rejection preparation original cancellations", *preparation_cancellations)
            except BaseException as original:
                retained = original_outcome_group(
                    "Manual rejection failure and preparation original cancellations",
                    original,
                    *preparation_cancellations,
                )
                primary = retained
                try:
                    child.assert_completed()
                except BaseException as incomplete:
                    primary = original_outcome_group("Manual original and unresolved rejection child", retained, incomplete)
                else:
                    required_work.complete_proposal_child(child, producer, retained)
            else:
                required_work.complete_proposal_child(child, producer)
        except BaseException as scope_failure:
            primary = original_outcome_group("Manual rejection scope and retained originals", scope_failure, *preparation_cancellations)
        cleanup = await close_required_proposal_lease(lease, coordinator=required_work)
        if primary is not None:
            if cleanup:
                raise_required_proposal_failure(
                    required_work,
                    original_outcome_group("Manual rejection original and actual lease close", primary, *cleanup),
                    lease=lease,
                )
            if isinstance(primary, ValueError):
                raise HTTPException(status_code=409, detail=str(primary)) from primary
            raise_required_proposal_failure(required_work, primary, lease=lease)
        if cleanup:
            raise_required_proposal_failure(
                required_work, original_outcome_group("Manual rejection actual close originals", *cleanup), lease=lease
            )
        if pipeline_proposal is None:
            raise AuditIntegrityError("Manual rejection lost its validated actual row")
        _ = body
        return _composition_proposal_response(pipeline_proposal)
