"""Landscape initialization owns and restores SQLAlchemy's transient DDL rules."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import create_mock_engine

from elspeth.core.landscape import database as database_module
from elspeth.core.landscape.database import LandscapeDB, LandscapeSchemaShape, probe_schema_shape
from elspeth.core.landscape.schema import metadata, sink_effect_members_table


@pytest.fixture
def original_rules():
    """Include a meaningful pre-existing rule, then isolate intentional DDL leaks."""
    untouched = [(constraint, constraint._create_rule) for table in metadata.tables.values() for constraint in table.constraints]
    constraint = next(iter(sink_effect_members_table.foreign_key_constraints))

    def allow_constraint(_compiler):
        return True

    constraint._create_rule = allow_constraint
    original = [(constraint, constraint._create_rule) for table in metadata.tables.values() for constraint in table.constraints]
    try:
        yield original
    finally:
        for constraint, rule in untouched:
            constraint._create_rule = rule


@pytest.mark.parametrize("ddl_fails", [False, True])
def test_landscape_creation_restores_exact_rules_after_ddl(monkeypatch, original_rules, ddl_fails):
    """Both successful and failed DDL must preserve non-None prior rules."""
    real_create_all = metadata.create_all
    engines = []
    primary_error = RuntimeError("DDL failed after PostgreSQL deferred constraints")

    def create_all(bind, *, checkfirst=True):
        engines.append(bind)
        # Keep actual SQLite creation intact; use real SQLAlchemy PostgreSQL
        # generation to induce the same deferred-FK mutation without a server.
        real_create_all(bind=bind, checkfirst=checkfirst)
        real_create_all(create_mock_engine("postgresql://", lambda *_args, **_kwargs: None))
        assert any(constraint._create_rule is not rule for constraint, rule in original_rules)
        if ddl_fails:
            raise primary_error

    monkeypatch.setattr(metadata, "create_all", create_all)
    try:
        if ddl_fails:
            with pytest.raises(RuntimeError) as caught:
                LandscapeDB.in_memory()
            assert caught.value is primary_error
        else:
            with LandscapeDB.in_memory() as db:
                db._validate_schema()
                assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES
        assert all(constraint._create_rule is rule for constraint, rule in original_rules)
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.parametrize("first_fails", [False, True])
def test_landscape_initializers_cannot_capture_each_others_transient_rules(monkeypatch, original_rules, first_fails):
    """A second initializer must wait until its predecessor restores metadata."""
    real_create_all = metadata.create_all
    first_mutated, release_first, contender_observed, contender_blocked, second_ddl = (Event() for _ in range(5))
    engines = []
    primary_error = RuntimeError("first initializer failed")

    class ObservedLock:
        """A real lock with a deterministic notification when acquisition blocks."""

        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                contender_blocked.set()
                contender_observed.set()
                assert self.lock.acquire(timeout=10)
            return self

        def __exit__(self, *_args):
            self.lock.release()

    def create_all(bind, *, checkfirst=True):
        engines.append(bind)
        if first_mutated.is_set():
            second_ddl.set()
            contender_observed.set()
            real_create_all(bind=bind, checkfirst=checkfirst)
            return
        real_create_all(bind=bind, checkfirst=checkfirst)
        real_create_all(create_mock_engine("postgresql://", lambda *_args, **_kwargs: None))
        assert any(constraint._create_rule is not rule for constraint, rule in original_rules)
        first_mutated.set()
        assert release_first.wait(10)
        if first_fails:
            raise primary_error

    def initialize():
        with LandscapeDB.in_memory() as db:
            db._validate_schema()
            assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES

    # On the unfixed implementation this lock is ignored: the second DDL
    # interval enters while the first is paused, which is the failing oracle.
    monkeypatch.setattr(database_module, "_LANDSCAPE_METADATA_CREATE_LOCK", ObservedLock(), raising=False)
    monkeypatch.setattr(metadata, "create_all", create_all)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(initialize)
            try:
                assert first_mutated.wait(10)
                second = workers.submit(initialize)
                try:
                    assert contender_observed.wait(10)
                    assert contender_blocked.is_set()
                    assert not second_ddl.is_set()
                finally:
                    release_first.set()
                if first_fails:
                    with pytest.raises(RuntimeError) as caught:
                        first.result(timeout=10)
                    assert caught.value is primary_error
                else:
                    first.result(timeout=10)
                second.result(timeout=10)
                assert second_ddl.is_set()
            finally:
                release_first.set()
        assert all(constraint._create_rule is rule for constraint, rule in original_rules)
    finally:
        for engine in engines:
            engine.dispose()
