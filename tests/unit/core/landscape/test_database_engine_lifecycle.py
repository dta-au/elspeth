"""A database factory owns its engine until it returns a usable database."""

from pathlib import Path

import pytest
from sqlalchemy import event

from elspeth.core.landscape import database as database_module
from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError


@pytest.mark.parametrize("disposal_fails", [False, True])
def test_failed_validation_disposes_created_engine_and_preserves_primary_error(tmp_path: Path, monkeypatch, disposal_fails: bool) -> None:
    engines = []
    disposals = []
    real_create_engine = database_module.create_engine

    def create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        engines.append(engine)
        event.listen(engine, "engine_disposed", lambda disposed: disposals.append(disposed))
        if disposal_fails:
            original_dispose = engine.dispose

            def fail_dispose():
                original_dispose()
                raise RuntimeError("secondary cleanup failure")

            monkeypatch.setattr(engine, "dispose", fail_dispose)
        return engine

    primary = SchemaCompatibilityError("incompatible schema")

    def fail_validation(self):
        raise primary

    monkeypatch.setattr(database_module, "create_engine", create_engine)
    monkeypatch.setattr(LandscapeDB, "_validate_schema", fail_validation)
    with pytest.raises(SchemaCompatibilityError) as caught:
        LandscapeDB.from_url(f"sqlite:///{tmp_path / 'invalid.db'}")
    assert caught.value is primary
    assert len(engines) == 1
    assert disposals == engines


def test_successful_factory_transfers_live_engine_to_caller(tmp_path: Path, monkeypatch) -> None:
    disposals = []
    real_create_engine = database_module.create_engine

    def create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        event.listen(engine, "engine_disposed", lambda disposed: disposals.append(disposed))
        return engine

    monkeypatch.setattr(database_module, "create_engine", create_engine)
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'valid.db'}")
    engine = db.engine
    try:
        assert disposals == []
        with db.read_only_connection() as conn:
            assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        db.close()
    assert disposals == [engine]
