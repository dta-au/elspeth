"""Database capability admission for caller-authored PostgreSQL audit SQL.

This is deliberately separate from Landscape's ordinary read connections:
runtime writers may read maintained queries with their writer credentials.
The SQL tool instead requires a dedicated audit-reader login in a database
without accessible custom SQL capabilities. PostgreSQL's initdb ACL records,
not mutable current PUBLIC grants, identify privileged built-in routines.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from elspeth.core.landscape.schema import metadata


class MCPQueryPrivilegeError(PermissionError):
    """The configured PostgreSQL principal exceeds the SQL tool's authority."""


# Bootstrap objects have OIDs below FirstNormalObjectId (16384). This also
# excludes extension routines installed into pg_catalog. initprivs preserves
# initdb's restricted ACLs even if an administrator later grants PUBLIC EXECUTE.
_POSTGRES_CAPABILITY_CHECK = text("""
WITH principal AS (
    SELECT oid, rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
           rolreplication, rolbypassrls
    FROM pg_catalog.pg_roles
    WHERE rolname IN (SESSION_USER, CURRENT_USER)
), allowed_tables AS (
    SELECT c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND c.relname = ANY(CAST(:audit_tables AS text[]))
), refusals AS (
    SELECT 'use one restricted LOGIN credential, without SET ROLE' AS reason
    WHERE SESSION_USER <> CURRENT_USER
       OR (SELECT count(*) FROM principal) <> 1
    UNION ALL
    SELECT 'the login has administrative role attributes'
    FROM principal
    WHERE NOT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb
       OR rolreplication OR rolbypassrls
    UNION ALL
    SELECT 'the login has role memberships, including switchable roles'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.member IN (SELECT oid FROM principal)
    )
    UNION ALL
    SELECT 'the login owns the database or can create database objects'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_database d
        WHERE d.datname = pg_catalog.current_database()
          AND (d.datdba IN (SELECT oid FROM principal)
               OR pg_catalog.has_database_privilege(d.oid, 'CREATE'))
    )
    UNION ALL
    SELECT 'the login owns a schema or can create schema objects'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace n
        WHERE n.nspowner IN (SELECT oid FROM principal)
           OR pg_catalog.has_schema_privilege(n.oid, 'CREATE')
    )
    UNION ALL
    SELECT 'the login owns relations or has relation write privileges'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        WHERE c.relowner IN (SELECT oid FROM principal)
           OR (c.relkind IN ('r', 'p', 'v', 'm', 'f')
               AND c.oid <> 'pg_catalog.pg_settings'::pg_catalog.regclass AND (
               pg_catalog.has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(c.oid, 'INSERT,UPDATE,REFERENCES')))
           OR (c.relkind = 'S' AND pg_catalog.has_sequence_privilege(c.oid, 'USAGE,UPDATE'))
    )
    UNION ALL
    SELECT 'the login can read non-audit tables, views or foreign tables'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE (n.nspname NOT IN ('pg_catalog', 'information_schema') OR c.oid >= 16384)
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
          AND pg_catalog.has_any_column_privilege(c.oid, 'SELECT')
          AND c.oid NOT IN (SELECT oid FROM allowed_tables)
    )
    UNION ALL
    SELECT 'the login can read privileged catalog relations or columns'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute column_info ON column_info.attrelid = c.oid
        LEFT JOIN pg_catalog.pg_init_privs table_acl
          ON table_acl.classoid = 'pg_catalog.pg_class'::pg_catalog.regclass
         AND table_acl.objoid = c.oid AND table_acl.objsubid = 0 AND table_acl.privtype = 'i'
        LEFT JOIN pg_catalog.pg_init_privs column_acl
          ON column_acl.classoid = 'pg_catalog.pg_class'::pg_catalog.regclass
         AND column_acl.objoid = c.oid AND column_acl.objsubid = column_info.attnum AND column_acl.privtype = 'i'
        WHERE n.nspname = 'pg_catalog'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND column_info.attnum > 0 AND NOT column_info.attisdropped
          AND pg_catalog.has_column_privilege(c.oid, column_info.attnum, 'SELECT')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.aclexplode(table_acl.initprivs) a
              WHERE a.grantee = 0 AND a.privilege_type = 'SELECT'
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.aclexplode(column_acl.initprivs) a
              WHERE a.grantee = 0 AND a.privilege_type = 'SELECT'
          )
    )
    UNION ALL
    SELECT 'the login can execute privileged built-in or custom routines'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN pg_catalog.pg_init_privs i
          ON i.classoid = 'pg_catalog.pg_proc'::pg_catalog.regclass
         AND i.objoid = p.oid AND i.objsubid = 0 AND i.privtype = 'i'
        WHERE pg_catalog.has_schema_privilege(n.oid, 'USAGE')
          AND pg_catalog.has_function_privilege(p.oid, 'EXECUTE')
          AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
          AND (
              n.nspname NOT IN ('pg_catalog', 'information_schema') OR p.oid >= 16384 OR p.prosecdef
              OR NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.aclexplode(COALESCE(i.initprivs, pg_catalog.acldefault('f', p.proowner))) a
                  WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
              )
          )
    )
    UNION ALL
    SELECT 'the database exposes custom operators or casts'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_operator o
        WHERE o.oid >= 16384 AND pg_catalog.has_schema_privilege(o.oprnamespace, 'USAGE')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_cast c
        JOIN pg_catalog.pg_type t ON t.oid = c.casttarget
        WHERE c.oid >= 16384 AND pg_catalog.has_schema_privilege(t.typnamespace, 'USAGE')
    )
    UNION ALL
    SELECT 'the database exposes custom types beyond audit table row types'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_type t
        LEFT JOIN pg_catalog.pg_type element ON element.oid = t.typelem
        WHERE t.oid >= 16384
          AND pg_catalog.has_schema_privilege(t.typnamespace, 'USAGE')
          AND pg_catalog.has_type_privilege(t.oid, 'USAGE')
          AND NOT (t.typtype = 'c' AND t.typrelid IN (SELECT oid FROM allowed_tables))
          AND NOT (t.typelem <> 0 AND element.typtype = 'c'
                   AND element.typrelid IN (SELECT oid FROM allowed_tables))
    )
    UNION ALL
    SELECT 'the login has large-object capabilities'
    WHERE EXISTS (
        SELECT 1 FROM pg_catalog.pg_largeobject_metadata l
        WHERE l.lomowner IN (SELECT oid FROM principal) OR EXISTS (
            SELECT 1 FROM pg_catalog.aclexplode(COALESCE(l.lomacl, pg_catalog.acldefault('L', l.lomowner))) a
            WHERE a.grantee IN (0, (SELECT oid FROM principal LIMIT 1))
              AND a.privilege_type IN ('SELECT', 'UPDATE')
        )
    )
)
SELECT reason FROM refusals LIMIT 1
""")


def admit_postgres_query_connection(conn: Connection) -> None:
    """Refuse excessive authority on the connection that will run caller SQL.

    Re-run before each query: neither pooled connections nor changed grants
    may reuse an earlier verdict. Catalog/probe failures propagate unchanged.
    """
    reason = conn.execute(_POSTGRES_CAPABILITY_CHECK, {"audit_tables": list(metadata.tables)}).scalar_one_or_none()
    if reason is not None:
        raise MCPQueryPrivilegeError(
            f"MCP SQL refused: {reason}. Configure a dedicated PostgreSQL audit-reader LOGIN "
            "with SELECT only on Landscape tables, no role memberships or custom SQL capabilities; "
            "see docs/guides/landscape-mcp-analysis.md."
        )
