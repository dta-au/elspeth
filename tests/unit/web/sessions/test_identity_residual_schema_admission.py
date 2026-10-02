"""A current epoch stamp cannot hide missing identity and admission contracts."""

from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, MetaData

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import metadata
from elspeth.web.sessions.schema import SessionSchemaError, _stamp_schema_sentinels, initialize_session_schema, probe_current_schema
from tests.fixtures.schema_mutations import copy_metadata_for_mutation


def _create_stamped_database(path: Path, declarations: MetaData) -> str:
    url = f"sqlite:///{path}"
    engine = create_session_engine(url)
    try:
        declarations.create_all(engine)
        _stamp_schema_sentinels(engine)
    finally:
        engine.dispose()
    return url


def _assert_refused(url: str, *, detail: str) -> None:
    engine = create_session_engine(url)
    try:
        with pytest.raises(SessionSchemaError, match=detail):
            initialize_session_schema(engine)
    finally:
        engine.dispose()


def test_complete_residual_schema_is_admitted(tmp_path: Path) -> None:
    url = _create_stamped_database(tmp_path / "complete.db", copy_metadata_for_mutation(metadata))
    engine = create_session_engine(url)
    try:
        initialize_session_schema(engine)
        assert probe_current_schema(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("table_name", ["sessions", "user_secrets", "user_preferences"])
def test_current_epoch_missing_identity_owner_fk_is_refused(tmp_path: Path, table_name: str) -> None:
    copied = copy_metadata_for_mutation(metadata)
    table = copied.tables[table_name]
    constraint = next(constraint for constraint in table.foreign_key_constraints if list(constraint.column_keys) == ["user_id"])
    table.constraints.remove(constraint)
    _assert_refused(_create_stamped_database(tmp_path / "missing-owner-fk.db", copied), detail=f"{table_name} foreign-key mismatch")


@pytest.mark.parametrize(
    ("table_name", "check_name"),
    [
        ("approvals", "ck_approvals_revocation_provenance"),
        ("run_start_permits", "ck_run_start_permits_admission_hash"),
        ("run_start_permits", "ck_run_start_permits_state_fields"),
        ("run_start_permits", "ck_run_start_permits_recovery_refusal"),
    ],
)
def test_current_epoch_missing_residual_check_is_refused(tmp_path: Path, table_name: str, check_name: str) -> None:
    copied = copy_metadata_for_mutation(metadata)
    table = copied.tables[table_name]
    constraints = [
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint) and constraint.name == check_name
    ]
    assert constraints
    for constraint in constraints:
        table.constraints.remove(constraint)
    _assert_refused(_create_stamped_database(tmp_path / "missing-check.db", copied), detail=f"{table_name} CHECK constraint mismatch")


@pytest.mark.parametrize(
    ("table_name", "column_name", "check_name"),
    [
        ("approvals", "revocation_event_id", "ck_approvals_revocation_provenance"),
        ("run_start_permits", "admission_decision", "ck_run_start_permits_state_fields"),
        ("run_start_permits", "execution_refusal", "ck_run_start_permits_recovery_refusal"),
    ],
)
def test_current_epoch_missing_residual_column_is_refused(tmp_path: Path, table_name: str, column_name: str, check_name: str) -> None:
    copied = copy_metadata_for_mutation(metadata)
    table = copied.tables[table_name]
    table._columns.remove(table.c[column_name])
    constraints = [
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint) and constraint.name == check_name
    ]
    assert constraints
    for constraint in constraints:
        table.constraints.remove(constraint)
    _assert_refused(_create_stamped_database(tmp_path / "missing-column.db", copied), detail=f"{table_name} column mismatch")
