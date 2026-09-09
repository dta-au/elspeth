SELECT 'CREATE DATABASE elspeth_sessions OWNER elspeth'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'elspeth_sessions'
)
\gexec

SELECT 'CREATE DATABASE elspeth_landscape OWNER elspeth'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'elspeth_landscape'
)
\gexec
