"""Original PRIMARY cold interface, backed by the shared SDK proof core."""

from uuid import UUID

from sqlalchemy import Connection

from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import SdkTerminalEvidence as PrimarySdkTerminalEvidence
from elspeth.web.sessions.continuation_sdk_terminal_cold import (
    read_v4_sdk_terminal_on_connection,
    read_v4_sdk_terminal_verified_on_connection,
)
from elspeth.web.sessions.continuation_sdk_terminal_result import VerifiedSdkTerminal


def read_v4_primary_sdk_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> PrimarySdkTerminalEvidence | None:
    return read_v4_sdk_terminal_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key, family=SdkTerminalFamily.PRIMARY
    )


def read_v4_primary_sdk_terminal_verified_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> VerifiedSdkTerminal | None:
    return read_v4_sdk_terminal_verified_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key, family=SdkTerminalFamily.PRIMARY
    )
