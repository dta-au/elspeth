"""Qualified PostgreSQL 16 environment for the restricted audit SELECT policy.

The distribution and database maintenance administrator are trusted. Admission
does not promise protection against a hostile superuser replacing system code
between these checks and execution. Unsupported catalog customization refuses
the API; it is never repaired by changing grants or database objects here.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

from elspeth.core.landscape.schema import _POSTGRES_AUDIT_EXPORT_TRIGGER_DDL, metadata
from elspeth.mcp.sql_policy import AGGREGATES, SCALAR_FUNCTIONS, SCALAR_TYPES, SQLPolicyError, admit_stored_expression

# These are trusted transaction controls, never derived from caller SQL. Naming
# pg_temp explicitly last prevents PostgreSQL's implicit temp-first resolution.
_SETTINGS = (
    "SET LOCAL search_path = pg_catalog, public, pg_temp",
    "SET LOCAL standard_conforming_strings = on",
    "SET LOCAL statement_timeout = '10000ms'",
    "SET LOCAL lock_timeout = '1000ms'",
    "SET LOCAL row_security = on",
)

# The finite expression policy plus closed scalar inputs bounds name resolution.
# All candidate implementations/support procedures must be original builtin
# internal/C routines, non-VOLATILE and without session configuration or elevated
# execution. Bootstrap provenance alone is not permission to call a routine:
# sql_policy independently admits only its reviewed function/operator families.
_CATALOG_CTES = """
WITH principal AS (
    SELECT oid, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
    FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER
), audit AS (
    SELECT c.* FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[]))
), scalar_types AS (
    SELECT t.* FROM pg_catalog.pg_type t
    JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'pg_catalog' AND t.typname = ANY(CAST(:types AS text[]))
), operators AS (
    SELECT o.* FROM pg_catalog.pg_operator o
    WHERE ((o.oprleft = 0 OR o.oprleft IN (SELECT oid FROM scalar_types))
       AND (o.oprright = 0 OR o.oprright IN (SELECT oid FROM scalar_types)))
       OR o.oid IN (2779, 2780)
), families AS (
    SELECT f.oid FROM pg_catalog.pg_opfamily f
    JOIN pg_catalog.pg_am am ON am.oid = f.opfmethod
    WHERE am.amname IN ('btree', 'hash') AND f.oid < 16384
), routines AS (
    SELECT p.oid FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'pg_catalog' AND p.proname = ANY(CAST(:functions AS text[]))
      AND p.proargtypes::oid[] <@ ARRAY(
          SELECT oid FROM scalar_types
          UNION ALL SELECT oid FROM pg_catalog.pg_type WHERE typname IN ('any', 'anyelement') AND oid < 16384
      )
), callback_ids AS (
    SELECT oid FROM routines
    UNION SELECT oprcode FROM operators
    UNION SELECT oprrest FROM operators
    UNION SELECT oprjoin FROM operators
    UNION SELECT castfunc FROM pg_catalog.pg_cast
          WHERE castsource IN (SELECT oid FROM scalar_types) AND casttarget IN (SELECT oid FROM scalar_types)
    UNION SELECT typinput FROM scalar_types
    UNION SELECT typoutput FROM scalar_types
    UNION SELECT typreceive FROM scalar_types
    UNION SELECT typsend FROM scalar_types
    UNION SELECT typmodin FROM scalar_types
    UNION SELECT typmodout FROM scalar_types
    UNION SELECT typanalyze FROM scalar_types
    UNION SELECT typsubscript FROM scalar_types
    UNION SELECT a.aggtransfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggfinalfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggcombinefn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggserialfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggdeserialfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggmtransfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggminvtransfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT a.aggmfinalfn FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid IN (SELECT oid FROM routines)
    UNION SELECT amproc FROM pg_catalog.pg_amproc WHERE amprocfamily IN (SELECT oid FROM families)
), callbacks AS (
    SELECT p.* FROM pg_catalog.pg_proc p WHERE p.oid IN (SELECT oid FROM callback_ids WHERE oid <> 0)
    UNION
    SELECT p.* FROM pg_catalog.pg_proc p
    WHERE p.oid IN (SELECT prosupport FROM pg_catalog.pg_proc WHERE oid IN (SELECT oid FROM callback_ids))
)
"""

_PROFILE = text(
    _CATALOG_CTES
    + """, failures AS (
    SELECT 'unsupported_server_version' AS reason
    WHERE CAST(pg_catalog.current_setting('server_version_num') AS integer) / 10000 <> 16
    UNION ALL SELECT 'transaction_is_not_read_only'
    WHERE pg_catalog.current_setting('transaction_read_only') <> 'on'
    UNION ALL SELECT 'unrestricted_principal'
    WHERE SESSION_USER <> CURRENT_USER OR (SELECT count(*) FROM principal) <> 1
       OR EXISTS (SELECT FROM principal WHERE NOT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb
                  OR rolreplication OR rolbypassrls)
       OR EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE member IN (SELECT oid FROM principal))
    UNION ALL SELECT 'reader_owns_database_objects'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid IN (SELECT oid FROM principal) AND deptype = 'o'
          AND dbid IN (0, (SELECT oid FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()))
    )
    UNION ALL SELECT 'reader_can_create_objects'
    WHERE pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE')
       OR EXISTS (SELECT FROM pg_catalog.pg_namespace WHERE pg_catalog.has_schema_privilege(oid, 'CREATE'))
    UNION ALL SELECT 'reader_has_write_authority'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_class c
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND c.oid <> 'pg_catalog.pg_settings'::pg_catalog.regclass
          AND (pg_catalog.has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(c.oid, 'INSERT,UPDATE,REFERENCES'))
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class
        WHERE CASE WHEN relkind = 'S' THEN pg_catalog.has_sequence_privilege(oid, 'USAGE,UPDATE') ELSE false END
    )
    UNION ALL SELECT 'noncanonical_read_scope'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND c.oid >= 16384
          AND c.oid NOT IN (SELECT oid FROM audit)
          AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
          AND pg_catalog.has_any_column_privilege(c.oid, 'SELECT')
    )
    UNION ALL SELECT 'noncanonical_relation_profile'
    WHERE (SELECT count(*) FROM audit) <> cardinality(CAST(:tables AS text[]))
       OR EXISTS (
          SELECT FROM audit a LEFT JOIN pg_catalog.pg_am am ON am.oid = a.relam
          WHERE a.relkind <> 'r' OR a.relpersistence <> 'p' OR a.relrowsecurity OR a.relforcerowsecurity
             OR a.relhasrules OR a.relispartition OR am.amname <> 'heap' OR am.oid >= 16384
       )
       OR EXISTS (SELECT FROM pg_catalog.pg_inherits WHERE inhrelid IN (SELECT oid FROM audit)
                  OR inhparent IN (SELECT oid FROM audit))
       OR EXISTS (SELECT FROM pg_catalog.pg_policy WHERE polrelid IN (SELECT oid FROM audit))
    UNION ALL SELECT 'temporary_resolution_objects'
    WHERE EXISTS (SELECT FROM pg_catalog.pg_class WHERE relnamespace = pg_catalog.pg_my_temp_schema())
       OR EXISTS (SELECT FROM pg_catalog.pg_type WHERE typnamespace = pg_catalog.pg_my_temp_schema())
    UNION ALL SELECT 'unsupported_column_type_or_collation'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_collation c ON c.oid = a.attcollation
        WHERE a.attrelid IN (SELECT oid FROM audit) AND a.attnum > 0 AND NOT a.attisdropped
          AND (a.atttypid NOT IN (SELECT oid FROM scalar_types)
               OR (a.attcollation <> 0 AND (c.oid >= 16384 OR c.collnamespace <> 'pg_catalog'::pg_catalog.regnamespace)))
    )
    UNION ALL SELECT 'noncanonical_column_identity'
    WHERE EXISTS (
        SELECT FROM audit c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        WHERE a.attnum > 0 AND NOT a.attisdropped
          AND NOT (c.relname || '.' || a.attname = ANY(CAST(:columns AS text[])))
    ) OR EXISTS (
        SELECT FROM unnest(CAST(:columns AS text[])) expected(identity)
        WHERE NOT EXISTS (
            SELECT FROM audit c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
            WHERE a.attnum > 0 AND NOT a.attisdropped
              AND c.relname || '.' || a.attname = expected.identity
        )
    )
    UNION ALL SELECT 'custom_execution_objects'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_operator WHERE oid >= 16384
    )
      OR EXISTS (SELECT FROM pg_catalog.pg_cast WHERE oid >= 16384)
      OR EXISTS (SELECT FROM pg_catalog.pg_am WHERE oid >= 16384)
      OR EXISTS (SELECT FROM pg_catalog.pg_opclass WHERE oid >= 16384)
      OR EXISTS (SELECT FROM pg_catalog.pg_opfamily WHERE oid >= 16384)
      OR EXISTS (SELECT FROM pg_catalog.pg_amproc WHERE oid >= 16384)
      OR EXISTS (SELECT FROM pg_catalog.pg_amop WHERE oid >= 16384)
    UNION ALL SELECT 'custom_scalar_types'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_type t
        WHERE t.oid >= 16384
          AND NOT (t.typtype = 'c' AND t.typrelid IN (SELECT oid FROM audit))
          AND NOT EXISTS (SELECT FROM pg_catalog.pg_type element WHERE element.oid = t.typelem
                          AND element.typtype = 'c' AND element.typrelid IN (SELECT oid FROM audit))
    )
    UNION ALL SELECT 'unsupported_builtin_callback'
    WHERE EXISTS (
        SELECT FROM callbacks p JOIN pg_catalog.pg_language l ON l.oid = p.prolang
        WHERE p.oid >= 16384 OR p.pronamespace <> 'pg_catalog'::pg_catalog.regnamespace
          OR p.provolatile = 'v' OR p.prosecdef OR p.proconfig IS NOT NULL
          OR l.lanname NOT IN ('internal', 'c', 'sql')
    )
    UNION ALL SELECT 'unsupported_index_profile'
    WHERE EXISTS (
        SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
        JOIN pg_catalog.pg_am am ON am.oid = c.relam
        WHERE i.indrelid IN (SELECT oid FROM audit)
          AND (am.amname <> 'btree' OR am.oid >= 16384 OR i.indexprs IS NOT NULL
               OR EXISTS (SELECT FROM unnest(i.indclass::oid[]) oc(oid)
                          WHERE oc.oid NOT IN (SELECT oid FROM pg_catalog.pg_opclass WHERE oid < 16384))
               OR EXISTS (SELECT FROM unnest(i.indcollation::oid[]) co(oid) WHERE co.oid >= 16384))
    )
)
SELECT reason FROM failures ORDER BY reason LIMIT 1
"""
)

# SQL-language callbacks and every custom routine require exact review below.
# This is a single finite extension to the ordinary internal/C callback rule.
_ROUTINES = text(
    _CATALOG_CTES
    + """
SELECT p.oid, n.nspname, p.proname, l.lanname, p.provolatile, p.prosecdef,
       p.proconfig IS NOT NULL, pg_catalog.oidvectortypes(p.proargtypes),
       pg_catalog.format_type(p.prorettype, NULL), p.prosrc,
       p.prosqlbody IS NOT NULL, p.prokind, p.provariadic <> 0,
       ARRAY(SELECT t.tgname || ':' || c.relname || ':' || t.tgtype::text
             FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
             WHERE t.tgfoid = p.oid AND NOT t.tgisinternal AND t.tgenabled = 'O'
               AND t.tgnargs = 0 AND t.tgqual IS NULL AND t.tgconstraint = 0
               AND c.oid IN (SELECT oid FROM audit) ORDER BY t.tgname),
       (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgfoid = p.oid),
       CASE WHEN p.prokind = 'f' AND p.prosqlbody IS NOT NULL
            THEN pg_catalog.pg_get_functiondef(p.oid) ELSE NULL END
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE p.oid >= 16384 OR (p.oid IN (SELECT oid FROM callbacks) AND l.lanname = 'sql')
"""
)


@dataclass(slots=True)
class _Routine:
    oid: int
    schema: str
    name: str
    language: str
    volatility: str
    security_definer: bool
    configured: bool
    arguments: str
    result: str
    source: str
    parsed_body: bool
    kind: str
    variadic: bool
    attachments: list[str]
    attachment_count: int
    parsed_definition: str | None = None


# PG16 pg_proc.dat: exact stable scalar-to-text wrappers. Their only operations
# are checked scalar type output and pg_catalog text concatenation, already in
# the callback closure. Caller arrays, composite values and custom types refuse.
_SQL_WRAPPERS = {
    2003: ("textanycat", "text, anynonarray", "select $1 operator(pg_catalog.||) $2::pg_catalog.text"),
    2004: ("anytextcat", "anynonarray, text", "select $1::pg_catalog.text operator(pg_catalog.||) $2"),
}
# These seven PG16 parsed bodies invoke only already-checked scalar operators
# or numeric/int4 rounding. Whole definitions are compared below, including
# the strict/parallel/configuration properties emitted by pg_get_functiondef.
_PARSED_SQL_WRAPPERS = {
    1708: ("round", "numeric", "numeric", "i", "round($1, 0)"),
    1296: ("timedate_pl", "time without time zone, date", "timestamp without time zone", "i", "($2 + $1)"),
    1848: ("interval_pl_time", "interval, time without time zone", "time without time zone", "i", "($2 + $1)"),
    2546: ("interval_pl_date", "interval, date", "timestamp without time zone", "i", "($2 + $1)"),
    2548: ("interval_pl_timestamp", "interval, timestamp without time zone", "timestamp without time zone", "i", "($2 + $1)"),
    2549: ("interval_pl_timestamptz", "interval, timestamp with time zone", "timestamp with time zone", "s", "($2 + $1)"),
    2550: ("integer_pl_date", "integer, date", "date", "i", "($2 + $1)"),
}
_TRIGGER_ATTACHMENTS = {
    "fn_audit_export_chunk_insert_validate": ("trg_audit_export_chunk_insert_validate:audit_export_snapshot_chunks:7",),
    "fn_audit_export_snapshot_insert_seal": ("trg_audit_export_snapshot_insert_seal:audit_export_snapshots:7",),
    "fn_audit_export_snapshot_immutable": (
        "trg_audit_export_snapshot_immutable:audit_export_snapshots:19",
        "trg_audit_export_snapshot_immutable_delete:audit_export_snapshots:11",
    ),
    "fn_audit_export_chunk_immutable": (
        "trg_audit_export_chunk_immutable:audit_export_snapshot_chunks:19",
        "trg_audit_export_chunk_immutable_delete:audit_export_snapshot_chunks:11",
    ),
}


def _trigger_body(name: str) -> str:
    prefix = f"CREATE FUNCTION {name}() RETURNS trigger AS $$"
    suffix = "$$ LANGUAGE plpgsql"
    bodies = [
        ddl.strip()[len(prefix) : -len(suffix)]
        for ddl in _POSTGRES_AUDIT_EXPORT_TRIGGER_DDL
        if ddl.strip().startswith(prefix) and ddl.strip().endswith(suffix)
    ]
    if len(bodies) != 1:
        raise SQLPolicyError("Canonical PostgreSQL writer trigger definition is unavailable")
    return bodies[0]


def _admit_routine(routine: _Routine) -> None:
    if routine.security_definer or routine.configured or routine.kind != "f" or routine.variadic:
        raise SQLPolicyError("Unsupported PostgreSQL routine configuration")
    if routine.oid < 16384:
        if routine.parsed_body and routine.oid in _PARSED_SQL_WRAPPERS:
            name, arguments, result, volatility, body = _PARSED_SQL_WRAPPERS[routine.oid]
            volatility_sql = "IMMUTABLE" if volatility == "i" else "STABLE"
            expected = (
                f"CREATE OR REPLACE FUNCTION pg_catalog.{name}({arguments})\n"
                f" RETURNS {result}\n LANGUAGE sql\n {volatility_sql} PARALLEL SAFE STRICT COST 1\nRETURN {body}\n"
            )
            if (
                routine.schema == "pg_catalog"
                and routine.language == "sql"
                and (routine.name, routine.arguments, routine.result, routine.volatility) == (name, arguments, result, volatility)
                and routine.source == ""
                and routine.parsed_definition == expected
                and not routine.attachments
                and routine.attachment_count == 0
            ):
                return
            raise SQLPolicyError(f"Unsupported PostgreSQL parsed builtin callback: {routine.name}")
        if (
            routine.schema == "pg_catalog"
            and routine.language == "sql"
            and routine.volatility == "s"
            and routine.result == "text"
            and routine.oid in _SQL_WRAPPERS
            and (routine.name, routine.arguments, routine.source) == _SQL_WRAPPERS[routine.oid]
            and not routine.attachments
            and routine.attachment_count == 0
            and not routine.parsed_body
            and routine.parsed_definition is None
        ):
            return
        raise SQLPolicyError(f"Unsupported PostgreSQL builtin SQL callback: {routine.name}")
    if (
        routine.schema == "public"
        and routine.name in _TRIGGER_ATTACHMENTS
        and routine.arguments == ""
        and routine.result == "trigger"
        and routine.language == "plpgsql"
        and routine.volatility == "v"
        and tuple(routine.attachments) == _TRIGGER_ATTACHMENTS[routine.name]
        and routine.attachment_count == len(routine.attachments)
        and routine.source == _trigger_body(routine.name)
        and not routine.parsed_body
        and routine.parsed_definition is None
    ):
        return
    raise SQLPolicyError(f"Unsupported PostgreSQL custom routine: {routine.schema}.{routine.name}")


_STORED_EXPRESSIONS = text("""
WITH audit AS (
    SELECT c.oid, c.relname FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[]))
)
SELECT pg_catalog.pg_get_expr(c.conbin, c.conrelid) AS expression, a.relname
FROM pg_catalog.pg_constraint c JOIN audit a ON a.oid = c.conrelid WHERE c.contype = 'c'
UNION ALL
SELECT pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS expression, a.relname
FROM pg_catalog.pg_index i JOIN audit a ON a.oid = i.indrelid WHERE i.indpred IS NOT NULL
""")


def admit_postgres_profile(conn: Connection, tables: frozenset[str]) -> None:
    """Check the exact executing connection; never cache the admission verdict."""
    if conn.dialect.name != "postgresql":
        raise SQLPolicyError("PostgreSQL profile requires a PostgreSQL connection")
    if not tables or not tables <= metadata.tables.keys():
        raise SQLPolicyError("PostgreSQL profile requires canonical Landscape tables")
    for setting in _SETTINGS:
        conn.exec_driver_sql(setting)
    parameters = {
        "tables": sorted(tables),
        "types": sorted(SCALAR_TYPES),
        "functions": sorted(SCALAR_FUNCTIONS | AGGREGATES | {"btrim"}),
        "columns": sorted(f"{table}.{column.name}" for table in tables for column in metadata.tables[table].columns),
    }
    reason = conn.execute(_PROFILE, parameters).scalar_one_or_none()
    if reason is not None:
        raise SQLPolicyError(f"PostgreSQL query profile refused: {reason}")
    for row in conn.execute(_ROUTINES, parameters):
        _admit_routine(_Routine(*row))
    for expression, table in conn.execute(_STORED_EXPRESSIONS, {"tables": sorted(tables)}):
        if table not in tables:
            raise SQLPolicyError("Stored PostgreSQL expression has no admitted relation")
        admit_stored_expression(expression, frozenset(column.name for column in metadata.tables[table].columns))
