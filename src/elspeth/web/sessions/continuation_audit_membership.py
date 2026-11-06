"""Exact declared identities for continuation audit candidate discovery.

These predicates establish correlation only. Readers retain their scope,
cardinality, payload bounds and complete closed-row validation. In particular,
malformed rows claiming an identity must still reach those refusal checks.
"""

from sqlalchemy import ColumnElement, Connection, and_, case, column, exists, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.selectable import TableValuedAlias

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.diagnostic_evidence import diagnostic_evidence_identity
from elspeth.web.sessions.models import chat_messages_table


def _audit_slots(conn: Connection) -> TableValuedAlias:
    """Expand arrays and preserve corrupt object roots as single candidates."""
    calls = chat_messages_table.c.tool_calls
    if conn.dialect.name == "sqlite":
        valid = case((func.json_valid(calls) == 1, calls), else_="null")
        array = case(
            (func.json_type(valid) == "array", valid),
            (func.json_type(valid) == "object", func.json_array(func.json(valid))),
            else_="[]",
        )
        return func.json_each(array).table_valued("value", "type")
    if conn.dialect.name == "postgresql":
        document = sql_cast(calls, JSONB)
        array = case(
            (func.jsonb_typeof(document) == "array", document),
            (func.jsonb_typeof(document) == "object", func.jsonb_build_array(document)),
            else_=func.jsonb_build_array(),
        )
        return func.jsonb_array_elements(array).table_valued(column("value", JSONB))
    raise AuditIntegrityError("Continuation audit membership requires a qualified SQLite or PostgreSQL dialect")


def private_audit_membership_predicate(conn: Connection, *, attempt_id: str) -> ColumnElement[bool]:
    """Find any private envelope identity, including disagreeing outer copies."""
    evidence_ids = (
        diagnostic_evidence_identity("provider-request", attempt_id),
        diagnostic_evidence_identity("provider-failure", attempt_id),
    )
    slots = _audit_slots(conn)
    if conn.dialect.name == "sqlite":
        document = case((slots.c.type == "object", slots.c.value), else_="{}")
        member = or_(
            func.json_extract(document, "$.tool_call_id") == attempt_id,
            func.json_extract(document, "$.evidence_id").in_(evidence_ids),
            func.json_extract(document, "$.evidence.tool_call_id") == attempt_id,
            func.json_extract(document, "$.evidence.evidence_id").in_(evidence_ids),
        )
    else:
        slot = slots.c.value
        outer_call = slot["tool_call_id"]
        outer_evidence = slot["evidence_id"]
        inner_call = slot["evidence"]["tool_call_id"]
        inner_evidence = slot["evidence"]["evidence_id"]
        member = or_(
            and_(func.jsonb_typeof(outer_call) == "string", outer_call.as_string() == attempt_id),
            and_(func.jsonb_typeof(outer_evidence) == "string", outer_evidence.as_string().in_(evidence_ids)),
            and_(func.jsonb_typeof(inner_call) == "string", inner_call.as_string() == attempt_id),
            and_(func.jsonb_typeof(inner_evidence) == "string", inner_evidence.as_string().in_(evidence_ids)),
        )
    return exists(select(1).select_from(slots).where(member).correlate(chat_messages_table))


def public_audit_membership_predicate(conn: Connection, *, attempt_id: str) -> ColumnElement[bool]:
    """Find a public call or its response-gap companion without multiplying rows."""
    slots = _audit_slots(conn)
    if conn.dialect.name == "sqlite":
        document = case((slots.c.type == "object", slots.c.value), else_="{}")
        member = or_(
            func.json_extract(document, "$.call.call_id") == attempt_id,
            func.json_extract(document, "$.provider_call_id") == attempt_id,
        )
    else:
        slot = slots.c.value
        call = slot["call"]["call_id"]
        companion = slot["provider_call_id"]
        member = or_(
            and_(func.jsonb_typeof(call) == "string", call.as_string() == attempt_id),
            and_(func.jsonb_typeof(companion) == "string", companion.as_string() == attempt_id),
        )
    return exists(select(1).select_from(slots).where(member).correlate(chat_messages_table))
