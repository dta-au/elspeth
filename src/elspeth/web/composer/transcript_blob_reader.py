"""Service-owned current transcript reader; never serializable evidence authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.tools.blobs import _locked_read_ready_blob

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.turn_effects import TurnEvidence


@dataclass(frozen=True, slots=True)
class CurrentTranscriptBlobReader:
    service: ComposerServiceImpl = field(repr=False)
    engine: Engine = field(repr=False)
    data_dir: str = field(repr=False)
    session_id: str
    context: SessionOperationContext = field(repr=False)

    def __post_init__(self) -> None:
        from elspeth.web.composer.service import ComposerServiceImpl

        if (
            type(self.service) is not ComposerServiceImpl
            or not isinstance(self.engine, Engine)
            or type(self.data_dir) is not str
            or self.service._session_engine is not self.engine
            or self.service._data_dir != self.data_dir
            or type(self.context) is not SessionOperationContext
            or self.context.fence.session_id != self.session_id
        ):
            raise AuditIntegrityError("transcript reader lost its actual service/store/current context")

    @classmethod
    def from_service(
        cls,
        service: ComposerServiceImpl,
        *,
        session_id: str,
        context: SessionOperationContext,
    ) -> CurrentTranscriptBlobReader:
        from elspeth.web.composer.service import ComposerServiceImpl

        if type(service) is not ComposerServiceImpl or service._session_engine is None:
            raise AuditIntegrityError("current transcript reader requires its actual session-backed service")
        return cls(service, service._session_engine, service._data_dir, session_id, context)

    def require_for_evidence(self, evidence: TurnEvidence) -> None:
        from elspeth.web.composer.turn_effects import TurnEvidence

        self.__post_init__()
        if (
            type(evidence) is not TurnEvidence
            or evidence.session_id != self.session_id
            or evidence.operation_id != self.context.fence.operation_id
            or evidence._continuation_current_epoch != self.context.fence.operation_epoch
        ):
            raise AuditIntegrityError("transcript reader belongs to another evidence physical owner")

    def __call__(self, blob_id: str) -> tuple[Mapping[str, Any], bytes] | None:
        # Same actual primitive, arguments, unavailable projection and exception
        # behavior as the original service closure. Authority lookup remains lazy.
        self.__post_init__()
        record, raw = _locked_read_ready_blob(
            self.engine,
            self.session_id,
            blob_id,
            data_dir=self.data_dir,
            session_operation_context=self.context,
            session_operation_authority=self.service._require_sessions_service().session_operation_authority,
        )
        return (record, raw) if record is not None and raw is not None else None
