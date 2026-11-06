"""Bind diagnostic graph identity to the actual current session operation."""

from uuid import UUID

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.protocol import SessionOperationAuthority


def composition_scope_for_operation(
    state: CompositionState,
    *,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None,
    session_operation_authority: SessionOperationAuthority,
) -> ComposerContentScope:
    """Check the live fence before minting an identity, never inspection permission."""
    operation = session_operation_context
    if (
        type(state) is not CompositionState
        or type(session_id) is not str
        or type(operation) is not SessionOperationContext
        or operation.operation_kind is not SessionOperationKind.COMPOSE
        or operation.fence.session_id != session_id
    ):
        raise AuditIntegrityError("Diagnostic graph requires its actual current compose session")
    try:
        identity = UUID(session_id)
    except ValueError:
        raise AuditIntegrityError("Diagnostic graph requires a canonical session identity") from None
    if str(identity) != session_id:
        raise AuditIntegrityError("Diagnostic graph requires a canonical session identity")
    session_operation_authority.compare_and_swap(operation)
    return ComposerContentScope(identity, composition_content_hash(state))
