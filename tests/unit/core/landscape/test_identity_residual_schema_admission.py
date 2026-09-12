"""Current epoch stamps cannot conceal missing residual audit contracts."""

from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, MetaData, create_engine, inspect

from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH, metadata, schema_identity_table
from elspeth.core.schema_identity import insert_schema_identity
from tests.fixtures.schema_mutations import copy_metadata_for_mutation


def _create_stamped_database(path: Path, declarations: MetaData) -> str:
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    try:
        declarations.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version = {SQLITE_SCHEMA_EPOCH}")
            insert_schema_identity(connection, schema_identity_table, store_kind="landscape", schema_epoch=SQLITE_SCHEMA_EPOCH)
    finally:
        engine.dispose()
    return url


def test_complete_residual_schema_is_admitted(tmp_path: Path) -> None:
    url = _create_stamped_database(tmp_path / "complete.db", copy_metadata_for_mutation(metadata))
    with LandscapeDB.from_url(url, create_tables=False) as database:
        columns = {column["name"] for column in inspect(database.engine).get_columns("calls")}
        assert {"prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"} <= columns


@pytest.mark.parametrize(
    ("table_name", "column_name", "check_name"),
    [
        ("calls", "prompt_tokens", "calls_prompt_tokens_nonnegative"),
        ("calls", "completion_tokens", "calls_completion_tokens_nonnegative"),
        ("calls", "cached_prompt_tokens", "calls_cached_prompt_tokens_nonnegative"),
        ("calls", "reasoning_tokens", "calls_reasoning_tokens_nonnegative"),
        ("run_web_plugin_policy", "admission_decision_json", "ck_run_web_plugin_policy_admission_pair"),
        ("run_web_plugin_policy", "admission_decision_hash", "ck_run_web_plugin_policy_admission_pair"),
    ],
)
def test_current_epoch_missing_residual_column_is_refused(tmp_path: Path, table_name: str, column_name: str, check_name: str) -> None:
    copied = copy_metadata_for_mutation(metadata)
    table = copied.tables[table_name]
    table._columns.remove(table.c[column_name])
    constraint = next(constraint for constraint in table.constraints if constraint.name == check_name)
    table.constraints.remove(constraint)
    url = _create_stamped_database(tmp_path / "missing-column.db", copied)

    with pytest.raises(SchemaCompatibilityError, match=rf"{table_name}\.{column_name}"):
        LandscapeDB.from_url(url, create_tables=False)


@pytest.mark.parametrize(
    ("table_name", "check_name"),
    [
        ("calls", "calls_prompt_tokens_nonnegative"),
        ("calls", "calls_completion_tokens_nonnegative"),
        ("calls", "calls_cached_prompt_tokens_nonnegative"),
        ("calls", "calls_reasoning_tokens_nonnegative"),
        ("run_web_plugin_policy", "ck_run_web_plugin_policy_admission_pair"),
    ],
)
def test_current_epoch_missing_residual_check_is_refused(tmp_path: Path, table_name: str, check_name: str) -> None:
    copied = copy_metadata_for_mutation(metadata)
    table = copied.tables[table_name]
    constraint = next(
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint) and constraint.name == check_name
    )
    table.constraints.remove(constraint)
    url = _create_stamped_database(tmp_path / "missing-check.db", copied)

    with pytest.raises(SchemaCompatibilityError, match=check_name):
        LandscapeDB.from_url(url, create_tables=False)
