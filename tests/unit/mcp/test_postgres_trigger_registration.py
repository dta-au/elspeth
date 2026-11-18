"""Every Landscape PostgreSQL trigger function is admitted by the MCP audit profile.

The profile refuses any public plpgsql function it does not know, so an
unregistered trigger function makes the audit-analysis server refuse every
Landscape that carries it. PostgreSQL proofs run only under ``testcontainer``;
this guard reads the DDL the Landscape metadata actually emits for the
PostgreSQL dialect, without a database, so the default selection catches a new function.
"""

from __future__ import annotations

import re

from sqlalchemy import DDL

from elspeth.core.landscape.schema import metadata
from elspeth.mcp.postgres_profile import _TRIGGER_ATTACHMENTS, _trigger_body

_FUNCTION = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\s*\(", re.IGNORECASE)
_TRIGGER = re.compile(
    r"CREATE\s+TRIGGER\s+(\w+)\s+(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\w+)\s+"
    r"FOR\s+EACH\s+(ROW|STATEMENT)\s+EXECUTE\s+FUNCTION\s+(\w+)\s*\(\)",
    re.IGNORECASE,
)
# pg_trigger.tgtype bits: ROW 1, BEFORE 2, INSERT 4, DELETE 8, UPDATE 16.
_EVENT_BITS = {"INSERT": 4, "DELETE": 8, "UPDATE": 16}


def _emitted_postgres_ddl() -> list[str]:
    """The DDL the Landscape tables attach to ``after_create``.

    Read from the listeners rather than by running ``create_all`` against a
    mock engine: that run marks cyclic foreign keys for ALTER on the shared
    metadata and strips them from every later CREATE TABLE in the process.
    Only PostgreSQL DDL declares a FUNCTION or ``EXECUTE FUNCTION``, so the
    SQLite listeners never match the patterns below.
    """
    return [
        listener.statement for table in metadata.tables.values() for listener in table.dispatch.after_create if isinstance(listener, DDL)
    ]


def _emitted_functions() -> set[str]:
    return {name for ddl in _emitted_postgres_ddl() for name in _FUNCTION.findall(ddl)}


def _emitted_attachments() -> dict[str, set[str]]:
    attachments: dict[str, set[str]] = {}
    for ddl in _emitted_postgres_ddl():
        for trigger, timing, event, table, level, function in _TRIGGER.findall(" ".join(ddl.split())):
            tgtype = (1 if level.upper() == "ROW" else 0) | (2 if timing.upper() == "BEFORE" else 0) | _EVENT_BITS[event.upper()]
            attachments.setdefault(function, set()).add(f"{trigger}:{table}:{tgtype}")
    return attachments


def test_the_instrument_sees_the_emitted_postgres_trigger_ddl() -> None:
    # Control: the audit-export functions are known to be emitted; a scan that
    # matched nothing would pass the coverage test below vacuously.
    functions = _emitted_functions()
    assert "fn_audit_export_chunk_immutable" in functions
    assert len(functions) >= 5


def test_every_emitted_trigger_function_is_admitted_and_every_admission_is_emitted() -> None:
    assert _emitted_functions() == set(_TRIGGER_ATTACHMENTS)


def test_admitted_attachments_match_the_emitted_triggers() -> None:
    emitted = _emitted_attachments()
    for function, attachments in _TRIGGER_ATTACHMENTS.items():
        assert set(attachments) == emitted[function], function


def test_every_admitted_function_has_one_canonical_body() -> None:
    for function in _TRIGGER_ATTACHMENTS:
        assert _trigger_body(function).strip().startswith("BEGIN"), function
