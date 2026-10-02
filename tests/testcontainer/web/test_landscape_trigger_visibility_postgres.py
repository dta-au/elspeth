"""Physical trigger admission remains visible to a SELECT-only runtime role."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool
from tests.testcontainer.web.test_schema_probe_postgres import postgres_engine, postgres_url

from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError
from elspeth.web.schema_probe import init_landscape_schema

__all__ = ["postgres_engine", "postgres_url"]
pytestmark = pytest.mark.testcontainer


@pytest.mark.parametrize(
    "mutation",
    ["healthy", "missing", "disabled", "replica_only", "wrong_table", "wrong_function", "wrong_event", "wrong_schema", "conditional"],
)
def test_select_only_role_observes_and_validates_trigger_contract(postgres_engine: Engine, mutation: str) -> None:
    init_landscape_schema(postgres_engine)
    role = f"trigger_reader_{uuid.uuid4().hex}"
    trigger = "trg_audit_export_snapshot_immutable"
    table = "audit_export_snapshots"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{role}"')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
        if mutation == "disabled":
            connection.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        elif mutation == "replica_only":
            connection.exec_driver_sql(f"ALTER TABLE {table} ENABLE REPLICA TRIGGER {trigger}")
        elif mutation != "healthy":
            connection.exec_driver_sql(f"DROP TRIGGER {trigger} ON {table}")
            if mutation == "wrong_schema":
                connection.exec_driver_sql("CREATE SCHEMA trigger_shadow")
                connection.exec_driver_sql(
                    "CREATE FUNCTION trigger_shadow.fn_audit_export_snapshot_immutable() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
                )
            if mutation != "missing":
                target = "audit_export_snapshot_chunks" if mutation == "wrong_table" else table
                function = "fn_audit_export_chunk_immutable" if mutation == "wrong_function" else "fn_audit_export_snapshot_immutable"
                if mutation == "wrong_schema":
                    function = "trigger_shadow." + function
                event = "INSERT" if mutation == "wrong_event" else "UPDATE"
                condition = "WHEN (false) " if mutation == "conditional" else ""
                connection.exec_driver_sql(
                    f"CREATE TRIGGER {trigger} BEFORE {event} ON {target} FOR EACH ROW {condition}EXECUTE FUNCTION {function}()"
                )

    reader = create_engine(postgres_engine.url, connect_args={"options": f"-c role={role}"}, poolclass=NullPool)
    try:
        with reader.connect() as connection:
            assert connection.exec_driver_sql("SELECT current_user").scalar_one() == role
            assert connection.exec_driver_sql("SELECT has_table_privilege(current_user, 'audit_export_snapshots', 'SELECT')").scalar_one()
            assert not connection.exec_driver_sql(
                "SELECT has_table_privilege(current_user, 'audit_export_snapshots', 'INSERT,UPDATE,DELETE,TRIGGER')"
            ).scalar_one()
            assert connection.exec_driver_sql("SELECT count(*) FROM information_schema.triggers").scalar_one() == 0
            assert connection.exec_driver_sql("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal").scalar_one() > 0
        # Keep the role set on every connection opened by the real admission path.
        url = reader.url.update_query_dict({"options": f"-c role={role}"}).render_as_string(hide_password=False)
        if mutation == "healthy":
            with LandscapeDB.from_url(url, create_tables=False):
                pass
        else:
            with pytest.raises(SchemaCompatibilityError, match=f"Missing triggers: {trigger}"):
                LandscapeDB.from_url(url, create_tables=False)
    finally:
        reader.dispose()
        with postgres_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
            connection.exec_driver_sql(f'DROP ROLE "{role}"')
