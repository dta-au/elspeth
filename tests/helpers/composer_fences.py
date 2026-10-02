"""Explicit live session custody for direct Composer tool producers."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.protocol import SessionOperationAuthority
from tests.helpers.session_fences import fenced_operation_context


@contextmanager
def fenced_tool_context(
    *,
    catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    session_engine: Engine,
    session_id: str,
    **fields: Any,
) -> Iterator[ToolContext]:
    """Hold a real COMPOSE operation for explicitly scoped direct tool calls.

    Callers choose this producer boundary; ToolContext and dispatch never
    acquire missing authority implicitly. The existing fence helper acquires
    and releases through the production authority, and the tool uses the same
    database-backed authority implementation to reprove that exact context.
    """
    authority: SessionOperationAuthority
    if session_engine.dialect.name == "sqlite":
        authority = SQLiteLocalSessionOperationAuthority(session_engine)
    else:
        authority = PostgresSessionOperationRepository(session_engine)
    with fenced_operation_context(session_engine, session_id) as operation:
        yield ToolContext(
            catalog=catalog,
            plugin_snapshot=plugin_snapshot,
            session_engine=session_engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=authority,
            **fields,
        )
