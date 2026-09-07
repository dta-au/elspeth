-- Fresh disposable acceptance only; fail on pre-existing roles.
\ir bootstrap-roles.sql
\connect postgres
\getenv runtime_a_password ELSPETH_RUNTIME_A_PASSWORD
\getenv runtime_b_password ELSPETH_RUNTIME_B_PASSWORD
CREATE ROLE elspeth_runtime_a LOGIN PASSWORD :'runtime_a_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE elspeth_runtime_b LOGIN PASSWORD :'runtime_b_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT CONNECT ON DATABASE elspeth_sessions, elspeth_landscape TO elspeth_runtime_a, elspeth_runtime_b;
\connect elspeth_sessions
SET ROLE elspeth_schema_owner;
GRANT USAGE ON SCHEMA public TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime_a, elspeth_runtime_b;
\connect elspeth_landscape
SET ROLE elspeth_schema_owner;
GRANT USAGE ON SCHEMA public TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO elspeth_runtime_a, elspeth_runtime_b;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO elspeth_runtime_a, elspeth_runtime_b;
