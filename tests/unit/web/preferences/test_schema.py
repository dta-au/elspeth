"""Schema-level tests: the user_preferences table exists with the expected columns."""

from elspeth.web.sessions.models import metadata


def test_user_preferences_table_registered() -> None:
    """The user_preferences table is registered on the shared metadata."""
    assert "user_preferences" in metadata.tables


def test_user_preferences_table_columns() -> None:
    """The user_preferences table has the expected columns."""
    table = metadata.tables["user_preferences"]
    column_names = {c.name for c in table.columns}
    assert column_names == {
        "user_id",
        "freeform_intro_dismissed_at",
        "tutorial_completed_at",
        "tutorial_stage",
        "tutorial_session_id",
        "tutorial_run_id",
        "tutorial_source_data_hash",
        "show_advanced",
        "updated_at",
    }


def test_freeform_intro_dismissed_at_is_nullable_timestamp() -> None:
    table = metadata.tables["user_preferences"]
    assert table.c.freeform_intro_dismissed_at.nullable


def test_user_preferences_user_id_is_primary_key() -> None:
    """user_id is the primary key (one row per user)."""
    table = metadata.tables["user_preferences"]
    pk_columns = {c.name for c in table.primary_key.columns}
    assert pk_columns == {"user_id"}


def test_user_preferences_table_has_no_composer_mode_column() -> None:
    table = metadata.tables["user_preferences"]
    assert "default_composer_mode" not in table.c


def test_tutorial_completed_at_is_nullable_timestamp() -> None:
    """NULL means first-run tutorial is not complete; timestamp means done."""
    table = metadata.tables["user_preferences"]
    column = table.c.tutorial_completed_at
    assert column.nullable


def test_tutorial_stage_check_constraint_closes_the_enum() -> None:
    """The tutorial resume stage is a closed enum at the DB level too
    (elspeth-918f4434b3). NULL = no in-progress tutorial; 'welcome' is
    deliberately NOT stored (nothing has started)."""
    from sqlalchemy import CheckConstraint

    table = metadata.tables["user_preferences"]
    check_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_user_preferences_tutorial_stage" in check_names


def test_tutorial_resume_columns_are_nullable() -> None:
    """All four resume columns are NULL when no tutorial is in progress."""
    table = metadata.tables["user_preferences"]
    for name in ("tutorial_stage", "tutorial_session_id", "tutorial_run_id", "tutorial_source_data_hash"):
        assert table.c[name].nullable, name


def test_show_advanced_is_not_null_with_false_server_default() -> None:
    """False is the standard-view default even at the DB level, so
    pre-existing rows (created before this column existed) read as False
    rather than NULL on the next server_default-backed recreation."""
    table = metadata.tables["user_preferences"]
    column = table.c.show_advanced
    assert not column.nullable
    assert column.server_default is not None
