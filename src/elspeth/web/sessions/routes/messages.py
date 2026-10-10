from __future__ import annotations

from typing import Annotated

from elspeth.web.sessions.schemas import ComposerOperationAcceptedResponse

from ._helpers import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    UUID,
    APIRouter,
    ChatMessageResponse,
    Depends,
    HTTPException,
    Query,
    Request,
    UserIdentity,
    WebRateLimiter,
    _composer_conversation_messages,
    _composer_conversation_or_llm_audit_messages,
    _composer_conversation_or_tool_messages,
    _composer_conversation_tool_or_llm_audit_messages,
    _message_response,
    _rejections_by_tool_call_id,
    _tool_call_outcomes_by_call_id,
    _verify_session_ownership,
    get_rate_limiter,
    require_pipeline_user,
)
from .composer.operations import admit, parse_message_body


def _requests_audit_grade_messages_view(
    *,
    include_tool_rows: bool,
    include_llm_audit: bool,
    include_raw_content: bool,
    include_rejection_reasons: bool,
) -> bool:
    return include_tool_rows or include_llm_audit or include_raw_content or include_rejection_reasons


def register_message_routes(router: APIRouter) -> None:
    @router.post("/{session_id}/messages", response_model=ComposerOperationAcceptedResponse, status_code=202)
    async def send_message(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        rate_limiter: Annotated[WebRateLimiter, Depends(get_rate_limiter)],
    ) -> ComposerOperationAcceptedResponse:
        body = await parse_message_body(request, session_id, user)
        return await admit(request, session_id, body, user, rate_limiter, "compose_message")

    @router.get(
        "/{session_id}/messages",
        response_model=list[ChatMessageResponse],
    )
    async def get_messages(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        include_llm_audit: bool = Query(
            False,
            description="Include value-free LLM-call rows and their paired semantic planner-attempt rows.",
        ),
        include_raw_content: bool = Query(False),
        include_tool_rows: bool = Query(False),
        include_rejection_reasons: bool = Query(
            False,
            description=(
                "Audit-grade: attach, to tool rows, the unredacted reason a refused composer tool call "
                "returned to the planner (composition_rejection_events). Requires include_tool_rows=true."
            ),
        ),
    ) -> list[ChatMessageResponse]:
        """Get conversation history for a session.

        ``include_raw_content`` opts in to the assistant message's
        pre-synthesis prose (the model's actual final text when the
        empty-state synthesizer replaced the visible content). Default
        omits it — the SPA conversation channel does not need audit data.
        Eval/diagnosis tooling enables it to verify whether the model
        converged on useful output that the synthesizer hid.
        """
        session = await _verify_session_ownership(session_id, user, request)
        service = request.app.state.session_service
        if include_rejection_reasons and not include_tool_rows:
            # Rejections attach to tool rows; without them the opt-in would
            # return nothing and look like "no rejections". Refuse before the
            # access-log write so no audit-grade read is recorded or served.
            raise HTTPException(
                status_code=422,
                detail="include_rejection_reasons requires include_tool_rows=true",
            )
        if _requests_audit_grade_messages_view(
            include_tool_rows=include_tool_rows,
            include_llm_audit=include_llm_audit,
            include_raw_content=include_raw_content,
            include_rejection_reasons=include_rejection_reasons,
        ):
            audit_query_args = {key: value for key, value in request.query_params.items() if key in AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST}
            # The authority re-proves (session, principal, provider) against the
            # live row before writing, so the provider is the session's own —
            # ownership above already proved it equals the deployment's.
            await service.record_audit_grade_view_async(
                session_id=str(session.id),
                requesting_principal=user.user_id,
                auth_provider_type=session.auth_provider_type,
                request_path=request.url.path,
                query_args=audit_query_args,
                ip_address=request.client.host if request.client else None,
            )
        # Fetch before slicing so hidden audit rows cannot skew normal-chat
        # pagination. The service remains the durable audit store; this route
        # is the user-facing conversation channel. The eval harness can opt in
        # to paired LLM-call/planner-attempt sidecars. They contain bounded
        # model/usage/cost metadata and closed decision classifications, but
        # not raw prompts, provider reasoning artifacts, tool arguments,
        # candidate values, or tool results.
        messages = await service.get_messages(session.id, limit=None)
        if include_tool_rows and include_llm_audit:
            conversation_messages = _composer_conversation_tool_or_llm_audit_messages(messages)
        elif include_tool_rows:
            conversation_messages = _composer_conversation_or_tool_messages(messages)
        elif include_llm_audit:
            conversation_messages = _composer_conversation_or_llm_audit_messages(messages)
        else:
            conversation_messages = _composer_conversation_messages(messages)
        paged_messages = conversation_messages[offset : offset + limit]
        # Per-call outcome stamping (elspeth-f5e6723133): project the Tier-1
        # role="tool" rows onto the assistant envelopes so the SPA can label
        # executed mutations as applied (with the resulting state version)
        # instead of describing every call as a lookup. Derived server-side
        # from durable rows — never from tool names. The version map is a
        # lean id/version projection, fetched only when tool rows exist.
        has_tool_rows = any(m.role == "tool" and m.tool_call_id is not None for m in messages)
        tool_outcomes = (
            _tool_call_outcomes_by_call_id(
                messages,
                state_versions_by_id=await service.get_state_version_numbers(session.id),
            )
            if has_tool_rows
            else None
        )
        rejections = (
            _rejections_by_tool_call_id(await service.list_composition_rejection_events(session.id)) if include_rejection_reasons else None
        )
        return [
            _message_response(
                m,
                include_raw_content=include_raw_content,
                tool_outcomes=tool_outcomes,
                rejections=rejections,
            )
            for m in paged_messages
        ]
