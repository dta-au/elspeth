"""Same-operation projection of exact dispatch candidates from durable history."""

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import JSON, ColumnElement, Connection, Integer, case, exists, func, select, type_coerce
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DataError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import chat_messages_table


def read_dispatch_envelopes(conn: Connection, *, session_id: str, tool_call_ids: Sequence[str]) -> tuple[tuple[str, object], ...]:
    """Project every matching envelope, including malformed/duplicate evidence.

    Only identity is filtered here. Kind, tool, status and hash validation stay
    with the authority parser; filtering them in SQL would hide corruption.
    """
    if not tool_call_ids:
        return ()
    if conn.dialect.name not in {"sqlite", "postgresql"}:
        raise AuditIntegrityError("Dispatch evidence reads require a qualified SQLite or PostgreSQL dialect")
    # This is fast-query eligibility, not a new authority/admission restriction.
    # Opaque persisted IDs need not be representable as SQL text parameters.
    if any(not _is_sql_text_identity(call_id) for call_id in tool_call_ids):
        return _read_legacy_dispatch_envelopes(conn, session_id=session_id, tool_call_ids=tool_call_ids)
    if conn.dialect.name == "postgresql":
        projection_failure: DataError | None = None
        try:
            # SELECT-only nested scope: never change the caller's transaction
            # mode, commit it, or reuse a failed PostgreSQL transaction. Consume
            # all driver results before releasing the savepoint.
            with conn.begin_nested():
                try:
                    candidates = _read_projected_candidates(conn, session_id=session_id, tool_call_ids=tool_call_ids)
                except DataError as error:
                    projection_failure = error
                    raise
        except DataError as error:
            if error is not projection_failure or not _is_jsonb_domain_error(error, driver=conn.dialect.driver):
                raise
            return _read_legacy_dispatch_envelopes(conn, session_id=session_id, tool_call_ids=tool_call_ids)
    else:
        candidates = _read_projected_candidates(conn, session_id=session_id, tool_call_ids=tool_call_ids)
    return _matching_envelopes(candidates, tool_call_ids)


def _is_sql_text_identity(value: str) -> bool:
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _is_jsonb_domain_error(error: DataError, *, driver: str) -> bool:
    # Both repository-qualified PG drivers expose structured SQLSTATE. Never
    # interpret error prose or widen this to arbitrary database/driver errors.
    original = error.orig
    if driver == "psycopg":
        from psycopg import DataError as PsycopgDataError

        return isinstance(original, PsycopgDataError) and original.sqlstate in {"22P05", "22003", "22P02"}
    if driver == "psycopg2":
        from psycopg2 import DataError as Psycopg2DataError

        return isinstance(original, Psycopg2DataError) and original.pgcode in {"22P05", "22003", "22P02"}
    return False


def _read_legacy_dispatch_envelopes(conn: Connection, *, session_id: str, tool_call_ids: Sequence[str]) -> tuple[tuple[str, object], ...]:
    rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.session_id == session_id))

    def slots() -> Iterable[object]:
        for calls in rows.scalars():
            if type(calls) is list:
                yield from calls

    return _matching_envelopes(slots(), tool_call_ids)


def _read_projected_candidates(conn: Connection, *, session_id: str, tool_call_ids: Sequence[str]) -> list[object]:
    envelope: ColumnElement[Any]
    invocation: ColumnElement[Any]
    array: ColumnElement[Any]
    call_id: ColumnElement[Any]
    matches_identity: ColumnElement[bool]
    calls = chat_messages_table.c.tool_calls
    if conn.dialect.name == "sqlite":
        array = case((func.json_type(calls) == "array", calls), else_="[]")
        slots = func.json_each(array).table_valued("value", "type", joins_implicitly=True)
        # CASE guards matter: json_each returns an unquoted SQL string for a
        # scalar JSON string, which json_extract cannot safely parse.
        object_slot = case((slots.c.type == "object", slots.c.value), else_="{}")
        invocations = func.json_each(object_slot).table_valued("key", "value", "type", joins_implicitly=True)
        invocation_object = case((invocations.c.type == "object", invocations.c.value), else_="{}")
        identities = func.json_each(invocation_object).table_valued("key", "value", "type", joins_implicitly=True)
        # SQLite json_extract sees the FIRST duplicate object key, whereas
        # our JSON decoder (and PostgreSQL JSONB) sees the LAST. An EXISTS
        # over all key occurrences is deliberately overinclusive; the decoded
        # identity below applies the original semantics. EXISTS also ensures
        # repeated keys do not invent repeated physical dispatch envelopes.
        matches_identity = exists(
            select(1)
            .select_from(invocations)
            .select_from(identities)
            .where(
                invocations.c.key == "invocation",
                identities.c.key == "tool_call_id",
                identities.c.type == "text",
                identities.c.value.in_(tool_call_ids),
            )
            .correlate(slots)
        )
        envelope = type_coerce(slots.c.value, JSON)
    elif conn.dialect.name == "postgresql":
        document = sql_cast(calls, JSONB)
        array = case((func.jsonb_typeof(document) == "array", document), else_=func.jsonb_build_array())
        slots = func.jsonb_array_elements(array).table_valued("value", with_ordinality="ordinality", joins_implicitly=True)
        # JSONB is only a candidate selector. Return the same physical slot
        # from original JSON: normalization can change number types or expand
        # an exponent past the Python integer decoder limit in ignored fields.
        envelope = type_coerce(func.pg_catalog.json_array_element(calls, sql_cast(slots.c.ordinality - 1, Integer)), JSON)
        invocation = type_coerce(slots.c.value, JSONB)["invocation"]
        call_id = case(
            (func.jsonb_typeof(invocation["tool_call_id"]) == "string", invocation["tool_call_id"].as_string()),
            else_=None,
        )
        matches_identity = call_id.in_(tool_call_ids)
    else:
        raise AuditIntegrityError("Dispatch evidence reads require a qualified SQLite or PostgreSQL dialect")
    rows = conn.execute(
        select(envelope)
        .select_from(chat_messages_table)
        .select_from(slots)
        .where(chat_messages_table.c.session_id == session_id, matches_identity)
    )
    return list(rows.scalars())


def _matching_envelopes(candidates: Iterable[object], tool_call_ids: Sequence[str]) -> tuple[tuple[str, object], ...]:
    selected: list[tuple[str, object]] = []
    wanted = frozenset(tool_call_ids)
    for candidate in candidates:
        # The SQL selector is allowed false positives for duplicate JSON keys,
        # never false negatives. Match the ordinary JSON-decoded identity;
        # kind/status/tool/result integrity is still checked by the authority.
        if type(candidate) is not dict:
            continue
        decoded_invocation = candidate["invocation"] if "invocation" in candidate else None
        if type(decoded_invocation) is not dict:
            continue
        decoded_id = decoded_invocation["tool_call_id"] if "tool_call_id" in decoded_invocation else None
        if type(decoded_id) is str and decoded_id in wanted:
            selected.append((decoded_id, candidate))
    return tuple(selected)


class _PipelineDispatchEvidenceRead:
    """Lazy batch confined to one proposal-list read and its connection.

    This holds untrusted durable candidates, never a successful authority
    verdict. Each proposal still validates all its matching evidence. It must
    not escape the listing operation or be reused by a mutation/retry.
    """

    def __init__(self, conn: Connection, *, session_id: str, tool_call_ids: Sequence[str]) -> None:
        transaction = conn.get_transaction()
        if transaction is None:
            raise AuditIntegrityError("Dispatch evidence batch requires an active proposal-list transaction")
        self._transaction = transaction
        self._conn = conn
        self._session_id = session_id
        self._tool_call_ids = frozenset(tool_call_ids)
        self._envelopes: dict[str, list[object]] | None = None

    def envelopes_for(self, conn: Connection, *, session_id: str, tool_call_id: str) -> tuple[object, ...]:
        if (
            conn is not self._conn
            or conn.get_transaction() is not self._transaction
            or session_id != self._session_id
            or tool_call_id not in self._tool_call_ids
        ):
            raise AuditIntegrityError("Dispatch evidence batch escaped its proposal-list read")
        if self._envelopes is None:
            grouped: dict[str, list[object]] = {call_id: [] for call_id in self._tool_call_ids}
            for call_id, envelope in read_dispatch_envelopes(conn, session_id=session_id, tool_call_ids=tuple(self._tool_call_ids)):
                grouped[call_id].append(envelope)
            self._envelopes = grouped
        return tuple(self._envelopes[tool_call_id])
