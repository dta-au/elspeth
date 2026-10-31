"""Independent epoch-74 recovery shape oracle, measured from SQLite catalogs."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, MetaData, String, Table, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH, composition_graph_parks_table, composition_states_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.helpers.composition_state import stored_default_pipeline_metadata

_PROVENANCE_CHECK = (
    "provenance IN ('tool_call', 'convergence_persist', 'plugin_crash_persist', 'preflight_persist', "
    "'tutorial_normalization', 'post_compose', 'session_seed', 'session_fork', 'interpretation_resolve', 'graph_recovery')"
)


@pytest.fixture
def schema_engine() -> Iterator[Engine]:
    engine = create_session_engine("sqlite:///:memory:")
    try:
        yield engine
    finally:
        engine.dispose()


def _assert_recovery_catalog_contract(engine: Engine) -> None:
    inspector = inspect(engine)
    columns = inspector.get_columns("composition_graph_parks")
    assert {column["name"]: (str(column["type"]), column["nullable"]) for column in columns} == {
        "id": ("VARCHAR", False),
        "session_id": ("VARCHAR", False),
        "state_id": ("VARCHAR", False),
        "operation_id": ("VARCHAR", False),
        "created_at": ("DATETIME", False),
    }, "park columns/types/nullability drifted"
    assert inspector.get_pk_constraint("composition_graph_parks")["constrained_columns"] == ["id"], "park primary key drifted"
    foreign_keys = inspector.get_foreign_keys("composition_graph_parks")
    assert {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            foreign_key["options"].get("ondelete"),
        )
        for foreign_key in foreign_keys
    } == {
        (("session_id",), "sessions", ("id",), "CASCADE"),
        (("state_id", "session_id"), "composition_states", ("id", "session_id"), None),
    }, "park foreign-key authority drifted"
    assert {
        (index["name"], tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes("composition_graph_parks")
    } == {("ix_composition_graph_parks_session_id", ("session_id",), False)}, "park indexes drifted"
    assert inspector.get_unique_constraints("composition_graph_parks") == [], "park uniqueness drifted"
    state_columns = {column["name"]: column for column in inspector.get_columns("composition_states")}
    assert "creator_operation_id" in state_columns, "checkpoint operation ownership missing"
    creator = state_columns["creator_operation_id"]
    assert (str(creator["type"]), creator["nullable"]) == ("VARCHAR", True), "checkpoint operation ownership shape drifted"
    assert {
        (constraint["name"], tuple(constraint["column_names"])) for constraint in inspector.get_unique_constraints("composition_states")
    } >= {
        ("uq_composition_state_id_session", ("id", "session_id")),
    }, "same-session state relation lost its unique target"
    checks = {constraint["name"]: constraint["sqltext"] for constraint in inspector.get_check_constraints("composition_states")}
    assert checks["ck_composition_states_provenance"] == _PROVENANCE_CHECK, "recovery provenance vocabulary drifted"


def test_epoch_74_recovery_schema_matches_independent_catalog_contract(schema_engine: Engine) -> None:
    assert SESSION_SCHEMA_EPOCH == 79
    initialize_session_schema(schema_engine)
    _assert_recovery_catalog_contract(schema_engine)


def test_park_same_session_and_recovery_provenance_are_enforced(schema_engine: Engine) -> None:
    """Known-positive recovery writes and negative cross-session/enum writes."""
    isolated = MetaData()
    sessions = Table("sessions", isolated, Column("id", String, primary_key=True))
    states = composition_states_table.to_metadata(isolated)
    parks = composition_graph_parks_table.to_metadata(isolated)
    isolated.create_all(schema_engine)
    now = datetime.now(UTC)
    with schema_engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        connection.execute(sessions.insert(), [{"id": "session-a"}, {"id": "session-b"}])
        connection.execute(
            states.insert().values(
                metadata_=stored_default_pipeline_metadata(),
                id="state-a",
                session_id="session-a",
                version=1,
                is_valid=False,
                created_at=now,
                provenance="graph_recovery",
                creator_operation_id="recovery-operation",
            )
        )
        connection.execute(
            parks.insert().values(
                id="park-a", session_id="session-a", state_id="state-a", operation_id="recovery-operation", created_at=now
            )
        )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), schema_engine.begin() as connection:
        connection.execute(
            parks.insert().values(
                id="foreign-park", session_id="session-b", state_id="state-a", operation_id="foreign-operation", created_at=now
            )
        )
    with pytest.raises(IntegrityError, match="CHECK constraint"), schema_engine.begin() as connection:
        connection.execute(
            states.insert().values(
                metadata_=stored_default_pipeline_metadata(),
                id="invalid-writer",
                session_id="session-a",
                version=2,
                is_valid=False,
                created_at=now,
                provenance="invented_recovery_writer",
            )
        )


@pytest.mark.parametrize(
    "mutation,diagnostic",
    [
        ("remove_same_session_fk", "foreign-key authority"),
        ("remove_session_fk", "foreign-key authority"),
        ("nullable_park_operation", "columns/types/nullability"),
        ("remove_session_index", "indexes drifted"),
        ("require_creator_operation", "operation ownership shape"),
        ("remove_recovery_provenance", "provenance vocabulary"),
        ("allow_extra_provenance", "provenance vocabulary"),
    ],
)
def test_catalog_oracle_rejects_mutated_database_shape(schema_engine: Engine, mutation: str, diagnostic: str) -> None:
    """Mutate isolated DDL, never production metadata or just a captured dict."""
    isolated = MetaData()
    Table("sessions", isolated, Column("id", String, primary_key=True))
    states = composition_states_table.to_metadata(isolated)
    parks = composition_graph_parks_table.to_metadata(isolated)
    if mutation == "remove_same_session_fk":
        constraint = next(
            item for item in parks.constraints if isinstance(item, ForeignKeyConstraint) and item.name == "fk_graph_park_state_session"
        )
        parks.constraints.remove(constraint)
    elif mutation == "remove_session_fk":
        constraint = next(
            item for item in parks.constraints if isinstance(item, ForeignKeyConstraint) and tuple(item.column_keys) == ("session_id",)
        )
        parks.constraints.remove(constraint)
    elif mutation == "nullable_park_operation":
        parks.c.operation_id.nullable = True
    elif mutation == "remove_session_index":
        parks.indexes.clear()
    elif mutation == "require_creator_operation":
        states.c.creator_operation_id.nullable = False
    else:
        check = next(
            item for item in states.constraints if isinstance(item, CheckConstraint) and item.name == "ck_composition_states_provenance"
        )
        states.constraints.remove(check)
        replacement = (
            _PROVENANCE_CHECK.replace(", 'graph_recovery'", "")
            if mutation == "remove_recovery_provenance"
            else _PROVENANCE_CHECK.replace(")", ", 'invented_recovery_writer')")
        )
        states.append_constraint(CheckConstraint(replacement, name="ck_composition_states_provenance"))
    isolated.create_all(schema_engine)
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_recovery_catalog_contract(schema_engine)
