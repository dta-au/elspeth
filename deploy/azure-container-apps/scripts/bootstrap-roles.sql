-- Cold install only. Connect as the Flexible Server administrator using
-- PGHOST/PGPORT/PGUSER/PGPASSWORD/PGSSLMODE/PGSSLROOTCERT from the operator.
-- Passwords are read from the environment, never passed on argv or echoed.
\getenv schema_owner_password ELSPETH_SCHEMA_OWNER_PASSWORD
\getenv runtime_password ELSPETH_RUNTIME_PASSWORD
CREATE ROLE elspeth_schema_owner LOGIN PASSWORD :'schema_owner_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE elspeth_runtime LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT elspeth_schema_owner TO CURRENT_USER WITH ADMIN OPTION;
ALTER DATABASE elspeth_sessions OWNER TO elspeth_schema_owner;
ALTER DATABASE elspeth_landscape OWNER TO elspeth_schema_owner;
REVOKE ALL ON DATABASE elspeth_sessions FROM PUBLIC;
REVOKE ALL ON DATABASE elspeth_landscape FROM PUBLIC;
GRANT CONNECT ON DATABASE elspeth_sessions, elspeth_landscape TO elspeth_runtime;
\connect elspeth_sessions
SET ROLE elspeth_schema_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime;
\connect elspeth_landscape
SET ROLE elspeth_schema_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime;
