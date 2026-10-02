"""Explicit leader transaction helper for scheduler writes."""

from __future__ import annotations

from contextlib import AbstractContextManager

from sqlalchemy.engine import Connection

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction


def require_coordination_token(
    coordination_token: CoordinationToken,
    *,
    verb: str,
) -> CoordinationToken:
    """Reject missing authority before a strict scheduler write can transact."""
    if not isinstance(coordination_token, CoordinationToken):
        raise TypeError(f"{verb} requires coordination_token; None cannot select an unfenced write")
    return coordination_token


def fenced_write(
    engine: Tier1Engine,
    *,
    coordination_token: CoordinationToken,
    verb: str,
) -> AbstractContextManager[Connection]:
    """Return a leader-fenced write transaction; missing authority refuses.

    The non-optional annotation prevents new Optional-authority call sites,
    while the runtime check protects Python callers that bypass static typing.
    Both contracts reject ``None`` before ``BEGIN IMMEDIATE`` is opened.

    Takes no clock (ADR-047): the fence statement writes its own deadline from
    ``CURRENT_TIMESTAMP`` inside the transaction it opens, so a caller clock
    could only have been recorded, never obeyed.
    """
    require_coordination_token(coordination_token, verb=verb)
    return fenced_leader_transaction(
        engine,
        token=coordination_token,
        window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        verb=verb,
    )
