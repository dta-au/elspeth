"""Original PRIMARY entrypoint for the shared atomic SDK terminal writer."""

from sqlalchemy import Connection

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation
from elspeth.web.sessions.continuation import PreparedContinuationProvider
from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission
from elspeth.web.sessions.continuation_sdk_finalization import finalize_v4_sdk_terminal_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import VerifiedSdkTerminal
from elspeth.web.sessions.service import SessionServiceImpl


def finalize_v4_primary_sdk_terminal_on_connection(
    conn: Connection,
    *,
    session_service: SessionServiceImpl,
    admission: V4PrimaryProviderAdmission,
    prepared: PreparedContinuationProvider,
    observation: V4SdkFailureObservation,
    custody: ProviderCallCustody,
    call: ComposerLLMCall | None,
) -> VerifiedSdkTerminal:
    if (
        type(session_service) is not SessionServiceImpl
        or type(prepared) is not PreparedContinuationProvider
        or type(admission) is not V4PrimaryProviderAdmission
        or custody.continuation_primary_v4 is not prepared
        or custody.continuation_primary_v4_admission is not admission
        or call is not custody.call
    ):
        raise AuditIntegrityError("PRIMARY SDK terminal changed its original nominal owner")
    return finalize_v4_sdk_terminal_on_connection(conn, custody=custody, observation=observation)
