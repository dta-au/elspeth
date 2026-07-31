from __future__ import annotations

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.tools import is_approval_required_blob_store_only_mutation_tool
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import StaleComposeStateError

from .._helpers import (
    _DATA_ERROR_KEY,
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
    _initial_composition_state_with_guided_session,
    _proposal_event_response,
    _state_data_from_composer_state,
    _state_from_record,
    _verify_session_ownership,
    asyncio,
    cast,
    deep_thaw,
    execute_tool,
    get_current_user,
    run_sync_in_worker,
    slog,
)
from .pipeline_settlement import (
    _await_with_deferred_cancellation,
    _proposal_user_message_content,
    settle_pipeline_proposal_under_compose_lock,
)

router = APIRouter()


_PROPOSAL_COMPOSER_CONTEXT_FIELDS: tuple[str, ...] = (
    "composer_model_identifier",
    "composer_model_version",
    "composer_provider",
    "composer_skill_hash",
    "tool_arguments_hash",
)


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
    if isinstance(cleanup_error, Exception):
        slog.error(
            event,
            session_id=str(session_id),
            exc_class=type(cleanup_error).__name__,
        )
        return cleanup_cancelled
    raise cleanup_error


@trust_boundary(
    tier=3,
    source="persisted LLM tool-call arguments of a stored CompositionProposalRecord (Tier-3 on read-back)",
    source_param="arguments",
    suppresses=("R5",),
    invariant="returns None on any absent/wrong-typed branch of arguments.source.inline_blob.content; never raises on arguments",
    non_raising=True,
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
    missing = [name for name in _PROPOSAL_COMPOSER_CONTEXT_FIELDS if getattr(proposal, name) is None]
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


@router.get(
    "/{session_id}/proposals",
    response_model=list[CompositionProposalResponse],
)
async def list_composition_proposals(
    session_id: UUID,
    request: Request,
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
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
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
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
    body: AcceptProposalRequest | None = None,
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
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
                reviewed_facts=None,
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
            if body is None or body.draft_hash is None:
                request_error = HTTPException(
                    status_code=422,
                    detail="Canonical pipeline proposal acceptance requires draft_hash.",
                )
                await _close_proposal_lease_before_commit(lease, primary=request_error)
                raise request_error
            cleanup_error, cleanup_cancelled = await _drain_proposal_lease_close(lease)
            if cleanup_error is not None:
                raise cleanup_error
            if cleanup_cancelled:
                raise asyncio.CancelledError
            route_settlement = await settle_pipeline_proposal_under_compose_lock(
                request=request,
                user=user,
                authority=pipeline_authority,
                draft_hash=body.draft_hash,
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
                    detail="The session state changed after this proposal was created. Ask ELSPETH to rebase the proposal.",
                )
            current_state = (
                _state_from_record(current_record) if current_record is not None else _initial_composition_state_with_guided_session()
            )
            arguments = cast(dict[str, Any], deep_thaw(proposal.arguments_json))
            user_message_content = await _proposal_user_message_content(service, proposal)
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
                    (accepted_state, _validation), was_cancelled = await _await_with_deferred_cancellation(
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
                        )
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
                    session_operation_authority=service.session_operation_authority,
                    session_operation_context=lease.context,
                    secret_service=request.app.state.scoped_secret_resolver,
                    user_id=str(user.user_id),
                    user_message_id=str(proposal.user_message_id) if proposal.user_message_id is not None else None,
                    user_message_content=user_message_content,
                    composer_model_identifier=proposal.composer_model_identifier,
                    composer_model_version=proposal.composer_model_version,
                    composer_provider=proposal.composer_provider,
                    composer_skill_hash=proposal.composer_skill_hash,
                    tool_arguments_hash=proposal.tool_arguments_hash,
                    _accepting_proposal_id=proposal.id,
                )
            )
            cancellation_deferred = cancellation_deferred or was_cancelled

            accepted_state = None
            if result.updated_state.version == current_state.version:
                if not result.success:
                    error_summary = result.data[_DATA_ERROR_KEY] or "Composer proposal failed validation."
                    validation_errors_payload = (
                        [{"component": entry.component, "message": entry.message} for entry in result.validation.errors]
                        if result.validation is not None
                        else []
                    )
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
                    except ValueError:
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
                    (accepted_state, _validation), was_cancelled = await _await_with_deferred_cancellation(
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
                        )
                    )
                    cancellation_deferred = cancellation_deferred or was_cancelled
            else:
                (accepted_state, _validation), was_cancelled = await _await_with_deferred_cancellation(
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
                    )
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
    user: UserIdentity = Depends(get_current_user),  # noqa: B008
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
            authority = await service.get_authoritative_composition_proposal(
                session_id=session.id,
                proposal_id=proposal_id,
                reviewed_facts=None,
            )
        except KeyError as primary:
            await _close_proposal_lease_before_commit(lease, primary=primary)
            raise HTTPException(status_code=404, detail="Proposal not found") from None
        except BaseException as primary:
            await _close_proposal_lease_before_commit(lease, primary=primary)
            raise
        if authority.pipeline is None:
            try:
                proposal = await service.reject_composition_proposal(
                    session_id=session.id,
                    proposal_id=proposal_id,
                    actor=f"user:{user.user_id}",
                    session_operation_context=lease.context,
                )
            except ValueError as primary:
                await _close_proposal_lease_before_commit(lease, primary=primary)
                raise HTTPException(status_code=409, detail=str(primary)) from primary
            except BaseException as primary:
                await _close_proposal_lease_before_commit(lease, primary=primary)
                raise
            _ = body
            response = _composition_proposal_response(proposal)
            cleanup_cancelled = await _close_proposal_lease_after_commit(
                lease,
                session_id=session.id,
            )
            if cleanup_cancelled:
                raise asyncio.CancelledError
            return response

        await lease.close()
        if authority.pipeline.proposal.surface.value in {"guided_staged", "tutorial_profile"}:
            raise HTTPException(
                status_code=409,
                detail="This pipeline proposal must be rejected through its guided workflow.",
            )
        try:
            proposal = await service.reject_pipeline_composition_proposal(
                session_id=session.id,
                proposal_id=proposal_id,
                draft_hash=authority.pipeline.proposal.draft_hash,
                reviewed_facts=None,
                reason="operator_rejected",
                dispatch=None,
                actor=f"user:{user.user_id}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _ = body
        return _composition_proposal_response(proposal)
