"""Sessions-backed COMPOSE admission, separate from provider-attempt quota."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.chargeable_admission import ChargeableOperation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.protocol import ComposerAdmissionRefused

if TYPE_CHECKING:
    from elspeth.web.sessions.protocol import SessionServiceProtocol


class ComposerChargeableAdmission:
    """Apply the durable session admission decision to each chargeable request."""

    def __init__(self, sessions_service: SessionServiceProtocol | None) -> None:
        self._sessions_service = sessions_service

    async def require(self, session_operation_context: SessionOperationContext | None) -> None:
        """Refuse a request without the exact COMPOSE authority or capacity."""
        if self._sessions_service is None or session_operation_context is None:
            raise ComposerAdmissionRefused("Composer admission requires session authority.")
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise ValueError("Composer admission requires COMPOSE session authority")
        decision = await self._sessions_service.assess_chargeable_operation(
            session_operation_context=session_operation_context,
            operation=ChargeableOperation.COMPOSER,
        )
        if not decision.allowed:
            if decision.refusal_reason is None:
                raise AuditIntegrityError("Refused Composer admission has no refusal reason")
            raise ComposerAdmissionRefused(f"Composer admission refused: {decision.refusal_reason.value}.")
