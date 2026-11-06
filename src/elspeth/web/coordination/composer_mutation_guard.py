"""Connection-local automatic business authority under the session write lock."""

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.composer_mutation import (
    ComposerAutoMutationRefused,
    ComposerMutationCeiling,
    ComposerMutationRevocation,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.sessions.models import sessions_table


def check_auto_mutation_on_connection(
    conn: Connection,
    *,
    context: SessionOperationContext,
    ceiling: ComposerMutationCeiling,
) -> ComposerMutationRevocation | None:
    """Read current mode inside the caller's existing locked transaction."""
    if type(ceiling) is not ComposerMutationCeiling:
        raise TypeError("Auto mutation requires an owned ceiling")
    ceiling.validate_context(context)
    current = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == ceiling.session_id)).scalar_one()
    if current not in ("auto_commit", "explicit_approve"):
        raise AuditIntegrityError("Session trust mode is corrupt")
    return None if ceiling.permits_auto(current) else ComposerMutationRevocation(ceiling, current)


def require_auto_mutation_on_connection(
    conn: Connection,
    *,
    context: SessionOperationContext,
    ceiling: ComposerMutationCeiling,
) -> None:
    refused = check_auto_mutation_on_connection(conn, context=context, ceiling=ceiling)
    if refused is not None:
        raise ComposerAutoMutationRefused(refused)
