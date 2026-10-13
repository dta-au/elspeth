\set ON_ERROR_STOP on
\getenv bootstrap_password POSTGRES_PASSWORD
\getenv session_schema_password POSTGRES_SESSION_SCHEMA_PASSWORD
\getenv landscape_schema_password POSTGRES_LANDSCAPE_SCHEMA_PASSWORD
\getenv session_runtime_password POSTGRES_SESSION_RUNTIME_PASSWORD
\getenv landscape_runtime_password POSTGRES_LANDSCAPE_RUNTIME_PASSWORD

-- URL-unreserved, distinct passwords are required before changing any state.
SELECT count(DISTINCT password) = 5 AND bool_and(password ~ '^[0-9a-f]{48}$') AS passwords_valid
FROM (VALUES (:'bootstrap_password'), (:'session_schema_password'),
             (:'landscape_schema_password'), (:'session_runtime_password'),
             (:'landscape_runtime_password')) AS passwords(password)
\gset
\if :passwords_valid
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_passwords_invalid: supply five distinct 48-character lowercase hexadecimal passwords'; END $$;
\endif

-- This bundle owns a dedicated cluster. Refuse unrelated databases rather
-- than changing their PUBLIC permissions or leaving an uninspected escape.
SELECT NOT EXISTS (
    SELECT FROM pg_database
    WHERE datname NOT IN ('postgres', 'template0', 'template1', 'elspeth_sessions', 'elspeth_landscape')
) AS cluster_safe
\gset
\if :cluster_safe
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_cluster_not_dedicated: use a dedicated PostgreSQL volume containing only the Compose databases'; END $$;
\endif

-- A reused role or membership must not preserve a SET ROLE escape.
SELECT NOT EXISTS (
    SELECT FROM pg_roles
    WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema',
                      'elspeth_session_runtime', 'elspeth_landscape_runtime')
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
) AND NOT EXISTS (
    SELECT FROM pg_auth_members
    WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN
        ('elspeth_session_schema', 'elspeth_landscape_schema',
         'elspeth_session_runtime', 'elspeth_landscape_runtime'))
       OR roleid IN (SELECT oid FROM pg_roles WHERE rolname IN
        ('elspeth_session_schema', 'elspeth_landscape_schema',
         'elspeth_session_runtime', 'elspeth_landscape_runtime'))
) AND NOT EXISTS (
    SELECT FROM pg_parameter_acl, LATERAL aclexplode(paracl) AS permission
    WHERE permission.grantee = 0 OR permission.grantee IN (SELECT oid FROM pg_roles WHERE rolname IN
        ('elspeth_session_schema', 'elspeth_landscape_schema',
         'elspeth_session_runtime', 'elspeth_landscape_runtime'))
) AND NOT EXISTS (
    -- Direct grants in postgres/template/other databases can confer server-
    -- file access even without a powerful role membership. Application roles
    -- must have no ownership or ACL dependencies outside their two stores.
    SELECT FROM pg_shdepend
    WHERE refclassid = 'pg_authid'::regclass
      AND refobjid IN (SELECT oid FROM pg_roles WHERE rolname IN
          ('elspeth_session_schema', 'elspeth_landscape_schema',
           'elspeth_session_runtime', 'elspeth_landscape_runtime'))
      AND deptype IN ('a', 'o')
      AND dbid NOT IN (SELECT oid FROM pg_database WHERE datname IN ('elspeth_sessions', 'elspeth_landscape'))
      AND NOT (dbid = 0 AND classid = 'pg_database'::regclass AND objid IN
          (SELECT oid FROM pg_database WHERE datname IN ('elspeth_sessions', 'elspeth_landscape')))
) AND NOT EXISTS (
    -- PUBLIC must not acquire execution of a system function that initdb
    -- restricted. pg_init_privs is PostgreSQL's own initial ACL authority.
    SELECT FROM pg_proc AS function
    JOIN pg_init_privs AS initial ON initial.objoid = function.oid
      AND initial.classoid = 'pg_proc'::regclass AND initial.objsubid = 0
      AND initial.privtype = 'i'
    WHERE EXISTS (SELECT FROM aclexplode(COALESCE(function.proacl, acldefault('f', function.proowner))) AS permission
                  WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS permission
                      WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
) AND NOT EXISTS (
    -- PUBLIC catalog table/column permissions cannot exceed initdb's ACLs.
    SELECT FROM pg_class AS relation
    CROSS JOIN LATERAL aclexplode(COALESCE(relation.relacl, acldefault('r', relation.relowner))) AS permission
    LEFT JOIN pg_init_privs AS initial ON initial.objoid = relation.oid
      AND initial.classoid = 'pg_class'::regclass AND initial.objsubid = 0 AND initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS original
                      WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type)
) AND NOT EXISTS (
    SELECT FROM pg_attribute AS column_acl
    JOIN pg_class AS relation ON relation.oid = column_acl.attrelid
    CROSS JOIN LATERAL aclexplode(column_acl.attacl) AS permission
    LEFT JOIN pg_init_privs AS column_initial ON column_initial.objoid = relation.oid
      AND column_initial.classoid = 'pg_class'::regclass
      AND column_initial.objsubid = column_acl.attnum AND column_initial.privtype = 'i'
    LEFT JOIN pg_init_privs AS relation_initial ON relation_initial.objoid = relation.oid
      AND relation_initial.classoid = 'pg_class'::regclass AND relation_initial.objsubid = 0 AND relation_initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (
          SELECT FROM aclexplode(COALESCE(column_initial.initprivs, '{}'::aclitem[])
                                || COALESCE(relation_initial.initprivs, '{}'::aclitem[])) AS original
          WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type
      )
) AS roles_safe
\gset
\if :roles_safe
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_roles_unsafe: stop deployment and recreate the pre-release PostgreSQL volume with operator approval'; END $$;
\endif

-- Legacy volumes gave the bootstrap superuser ownership. Do not migrate or
-- reassign shared administrative objects in a deployment initialization step.
SELECT NOT EXISTS (
    SELECT FROM pg_database AS database
    JOIN pg_roles AS owner ON owner.oid = database.datdba
    WHERE (database.datname = 'elspeth_sessions' AND owner.rolname <> 'elspeth_session_schema')
       OR (database.datname = 'elspeth_landscape' AND owner.rolname <> 'elspeth_landscape_schema')
) AS databases_safe
\gset
\if :databases_safe
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_legacy_owner: stop deployment and recreate the pre-release PostgreSQL volume with operator approval'; END $$;
\endif

-- Close admission before terminating backends so prior credentials cannot
-- reconnect while grants are resealed. Early preflight failures above do not
-- change role admission; later failures leave application roles NOLOGIN.
SELECT format('ALTER ROLE %I NOLOGIN', rolname)
FROM pg_roles
WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema',
                  'elspeth_session_runtime', 'elspeth_landscape_runtime')
\gexec

-- CONNECT revocation and password changes do not invalidate an established
-- backend. Bootstrap is a stopped-service maintenance step, so close every
-- prior application backend before applying the new credential boundary.
SELECT COALESCE(bool_and(pg_terminate_backend(pid, 5000)), true) AS old_connections_closed
FROM pg_stat_activity
WHERE usename IN ('elspeth_session_schema', 'elspeth_landscape_schema',
                  'elspeth_session_runtime', 'elspeth_landscape_runtime')
\gset
\if :old_connections_closed
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_connections_not_closed: stop web and CLI processes, then rerun bootstrap'; END $$;
\endif

SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT', name)
FROM (VALUES ('elspeth_session_schema'), ('elspeth_landscape_schema'),
             ('elspeth_session_runtime'), ('elspeth_landscape_runtime')) AS roles(name)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = name)
\gexec
SELECT format('ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', name, password)
FROM (VALUES ('elspeth_session_schema', :'session_schema_password'),
             ('elspeth_landscape_schema', :'landscape_schema_password'),
             ('elspeth_session_runtime', :'session_runtime_password'),
             ('elspeth_landscape_runtime', :'landscape_runtime_password')) AS roles(name, password)
\gexec

SELECT 'CREATE DATABASE elspeth_sessions OWNER elspeth_session_schema'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'elspeth_sessions')
\gexec
SELECT 'CREATE DATABASE elspeth_landscape OWNER elspeth_landscape_schema'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'elspeth_landscape')
\gexec

REVOKE ALL ON DATABASE elspeth_sessions FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime, elspeth_landscape_schema;
REVOKE ALL ON DATABASE elspeth_landscape FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime, elspeth_session_schema;
-- Runtime credentials must not reach uninspected administrative/template DBs.
-- The bootstrap administrator owns these databases and retains owner access.
REVOKE ALL ON DATABASE postgres, template0, template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE elspeth_sessions TO elspeth_session_runtime;
GRANT CONNECT ON DATABASE elspeth_landscape TO elspeth_landscape_runtime;

\connect elspeth_sessions
SELECT NOT EXISTS (
    SELECT FROM pg_class WHERE relnamespace = 'public'::regnamespace
      AND relowner <> 'elspeth_session_schema'::regrole
) AND NOT EXISTS (
    SELECT FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proowner <> 'elspeth_session_schema'::regrole
) AND (SELECT nspowner IN ('pg_database_owner'::regrole, 'elspeth_session_schema'::regrole)
       FROM pg_namespace WHERE nspname = 'public') AND NOT EXISTS (
    SELECT FROM pg_proc, LATERAL aclexplode(proacl) AS permission
    WHERE pronamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_namespace, LATERAL aclexplode(nspacl) AS permission
    WHERE nspname <> 'public'
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_class, LATERAL aclexplode(relacl) AS permission
    WHERE relnamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_attribute JOIN pg_class ON pg_class.oid = attrelid,
                LATERAL aclexplode(attacl) AS permission
    WHERE relnamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- Ownership permits ALTER/DROP even after CREATE is revoked. PostgreSQL's
    -- dependency catalog covers types, collations and other owned objects as
    -- well as relations, routines and namespaces.
    SELECT FROM pg_shdepend
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND deptype = 'o'
      AND refobjid IN ('elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- PUBLIC must not acquire execution of a system function that initdb
    -- restricted. pg_init_privs is PostgreSQL's own initial ACL authority.
    SELECT FROM pg_proc AS function
    JOIN pg_init_privs AS initial ON initial.objoid = function.oid
      AND initial.classoid = 'pg_proc'::regclass AND initial.objsubid = 0
      AND initial.privtype = 'i'
    WHERE EXISTS (SELECT FROM aclexplode(COALESCE(function.proacl, acldefault('f', function.proowner))) AS permission
                  WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS permission
                      WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
) AND NOT EXISTS (
    -- Per-schema REVOKE cannot undo a global default privilege grant.
    SELECT FROM pg_default_acl, LATERAL aclexplode(defaclacl) AS permission
    WHERE defaclnamespace = 0
      AND defaclrole IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole)
      AND permission.grantee IN (0, 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- No operator extension schemas are part of this dedicated application
    -- store. PUBLIC wrappers there could bypass the scoped function grants.
    SELECT FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'public', 'information_schema', 'pg_toast')
      AND nspname !~ '^pg_(toast_)?temp_[0-9]+$'
) AND NOT EXISTS (
    -- PostgreSQL reserves OIDs below 16384 for builtin objects. Custom
    -- routines belong only in public, even when placed in a builtin schema.
    SELECT FROM pg_proc
    WHERE oid >= 16384 AND pronamespace <> 'public'::regnamespace
) AND NOT EXISTS (
    -- PUBLIC catalog table/column permissions cannot exceed initdb's ACLs.
    SELECT FROM pg_class AS relation
    CROSS JOIN LATERAL aclexplode(COALESCE(relation.relacl, acldefault('r', relation.relowner))) AS permission
    LEFT JOIN pg_init_privs AS initial ON initial.objoid = relation.oid
      AND initial.classoid = 'pg_class'::regclass AND initial.objsubid = 0 AND initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS original
                      WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type)
) AND NOT EXISTS (
    SELECT FROM pg_attribute AS column_acl
    JOIN pg_class AS relation ON relation.oid = column_acl.attrelid
    CROSS JOIN LATERAL aclexplode(column_acl.attacl) AS permission
    LEFT JOIN pg_init_privs AS column_initial ON column_initial.objoid = relation.oid
      AND column_initial.classoid = 'pg_class'::regclass
      AND column_initial.objsubid = column_acl.attnum AND column_initial.privtype = 'i'
    LEFT JOIN pg_init_privs AS relation_initial ON relation_initial.objoid = relation.oid
      AND relation_initial.classoid = 'pg_class'::regclass AND relation_initial.objsubid = 0 AND relation_initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (
          SELECT FROM aclexplode(COALESCE(column_initial.initprivs, '{}'::aclitem[])
                                || COALESCE(relation_initial.initprivs, '{}'::aclitem[])) AS original
          WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type
      )
) AS objects_safe
\gset
\if :objects_safe
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_objects_unsafe: stop deployment and recreate the pre-release PostgreSQL volume with operator approval'; END $$;
\endif
REVOKE ALL ON SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime, elspeth_landscape_schema;
GRANT USAGE ON SCHEMA public TO elspeth_session_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO elspeth_session_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO elspeth_session_runtime;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO elspeth_session_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_session_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO elspeth_session_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_session_runtime;

\connect elspeth_landscape
SELECT NOT EXISTS (
    SELECT FROM pg_class WHERE relnamespace = 'public'::regnamespace
      AND relowner <> 'elspeth_landscape_schema'::regrole
) AND NOT EXISTS (
    SELECT FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proowner <> 'elspeth_landscape_schema'::regrole
) AND (SELECT nspowner IN ('pg_database_owner'::regrole, 'elspeth_landscape_schema'::regrole)
       FROM pg_namespace WHERE nspname = 'public') AND NOT EXISTS (
    SELECT FROM pg_proc, LATERAL aclexplode(proacl) AS permission
    WHERE pronamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_namespace, LATERAL aclexplode(nspacl) AS permission
    WHERE nspname <> 'public'
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_class, LATERAL aclexplode(relacl) AS permission
    WHERE relnamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    SELECT FROM pg_attribute JOIN pg_class ON pg_class.oid = attrelid,
                LATERAL aclexplode(attacl) AS permission
    WHERE relnamespace <> 'public'::regnamespace
      AND permission.grantee IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole,
                                 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- Use ownership dependencies rather than an incomplete list of catalogs.
    SELECT FROM pg_shdepend
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND refclassid = 'pg_authid'::regclass AND deptype = 'o'
      AND refobjid IN ('elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- PUBLIC must not acquire execution of a system function that initdb
    -- restricted. pg_init_privs is PostgreSQL's own initial ACL authority.
    SELECT FROM pg_proc AS function
    JOIN pg_init_privs AS initial ON initial.objoid = function.oid
      AND initial.classoid = 'pg_proc'::regclass AND initial.objsubid = 0
      AND initial.privtype = 'i'
    WHERE EXISTS (SELECT FROM aclexplode(COALESCE(function.proacl, acldefault('f', function.proowner))) AS permission
                  WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS permission
                      WHERE permission.grantee = 0 AND permission.privilege_type = 'EXECUTE')
) AND NOT EXISTS (
    -- Per-schema REVOKE cannot undo a global default privilege grant.
    SELECT FROM pg_default_acl, LATERAL aclexplode(defaclacl) AS permission
    WHERE defaclnamespace = 0
      AND defaclrole IN ('elspeth_session_schema'::regrole, 'elspeth_landscape_schema'::regrole)
      AND permission.grantee IN (0, 'elspeth_session_runtime'::regrole, 'elspeth_landscape_runtime'::regrole)
) AND NOT EXISTS (
    -- No operator extension schemas are part of this dedicated application
    -- store. PUBLIC wrappers there could bypass the scoped function grants.
    SELECT FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'public', 'information_schema', 'pg_toast')
      AND nspname !~ '^pg_(toast_)?temp_[0-9]+$'
) AND NOT EXISTS (
    -- PostgreSQL reserves OIDs below 16384 for builtin objects. Custom
    -- routines belong only in public, even when placed in a builtin schema.
    SELECT FROM pg_proc
    WHERE oid >= 16384 AND pronamespace <> 'public'::regnamespace
) AND NOT EXISTS (
    -- PUBLIC catalog table/column permissions cannot exceed initdb's ACLs.
    SELECT FROM pg_class AS relation
    CROSS JOIN LATERAL aclexplode(COALESCE(relation.relacl, acldefault('r', relation.relowner))) AS permission
    LEFT JOIN pg_init_privs AS initial ON initial.objoid = relation.oid
      AND initial.classoid = 'pg_class'::regclass AND initial.objsubid = 0 AND initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (SELECT FROM aclexplode(initial.initprivs) AS original
                      WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type)
) AND NOT EXISTS (
    SELECT FROM pg_attribute AS column_acl
    JOIN pg_class AS relation ON relation.oid = column_acl.attrelid
    CROSS JOIN LATERAL aclexplode(column_acl.attacl) AS permission
    LEFT JOIN pg_init_privs AS column_initial ON column_initial.objoid = relation.oid
      AND column_initial.classoid = 'pg_class'::regclass
      AND column_initial.objsubid = column_acl.attnum AND column_initial.privtype = 'i'
    LEFT JOIN pg_init_privs AS relation_initial ON relation_initial.objoid = relation.oid
      AND relation_initial.classoid = 'pg_class'::regclass AND relation_initial.objsubid = 0 AND relation_initial.privtype = 'i'
    WHERE relation.relnamespace = 'pg_catalog'::regnamespace AND permission.grantee = 0
      AND NOT EXISTS (
          SELECT FROM aclexplode(COALESCE(column_initial.initprivs, '{}'::aclitem[])
                                || COALESCE(relation_initial.initprivs, '{}'::aclitem[])) AS original
          WHERE original.grantee = 0 AND original.privilege_type = permission.privilege_type
      )
) AS objects_safe
\gset
\if :objects_safe
\else
  DO $$ BEGIN RAISE EXCEPTION 'compose_database_objects_unsafe: stop deployment and recreate the pre-release PostgreSQL volume with operator approval'; END $$;
\endif
REVOKE ALL ON SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime, elspeth_session_schema;
GRANT USAGE ON SCHEMA public TO elspeth_landscape_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO elspeth_landscape_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO elspeth_landscape_runtime;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM elspeth_session_runtime, elspeth_landscape_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_landscape_schema IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_landscape_runtime;

-- Reopen application admission only after both stores are fully sealed.
SELECT format('ALTER ROLE %I LOGIN', name)
FROM (VALUES ('elspeth_session_schema'), ('elspeth_landscape_schema'),
             ('elspeth_session_runtime'), ('elspeth_landscape_runtime')) AS roles(name)
\gexec
