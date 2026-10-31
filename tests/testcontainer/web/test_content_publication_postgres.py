"""Run the content publication storage behaviors against isolated PostgreSQL."""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_content_publication import (
    test_edit_and_identical_revert_receive_fresh_version_bindings_without_reusing_clearance as test_edit_and_identical_revert_receive_fresh_version_bindings_without_reusing_clearance,
)
from tests.unit.web.sessions.test_content_publication import (
    test_failed_insert_rolls_back_graph_and_content_evidence as test_failed_insert_rolls_back_graph_and_content_evidence,
)
from tests.unit.web.sessions.test_content_publication import (
    test_malformed_inherited_metadata_refuses_before_any_graph_write as test_malformed_inherited_metadata_refuses_before_any_graph_write,
)
from tests.unit.web.sessions.test_content_publication import (
    test_read_refuses_graph_tampering_instead_of_returning_stale_applicability as test_read_refuses_graph_tampering_instead_of_returning_stale_applicability,
)
from tests.unit.web.sessions.test_content_publication import (
    test_real_checkpoint_publishes_version_and_uninspected_applicability_together as test_real_checkpoint_publishes_version_and_uninspected_applicability_together,
)
from tests.unit.web.sessions.test_content_publication import (
    test_repository_readers_reject_stale_publication_before_derived_mutation as test_repository_readers_reject_stale_publication_before_derived_mutation,
)
from tests.unit.web.sessions.test_content_publication import (
    test_storage_number_type_binding_survives_database_roundtrip as test_storage_number_type_binding_survives_database_roundtrip,
)

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def content_postgres_target():
    with postgres_test_target(mem_limit="256m", nano_cpus=500_000_000) as target:
        yield target


@pytest.fixture
def store(content_postgres_target, tmp_path):
    # The shared behavioral tests assert whole-database row counts. Each gets
    # its own database so sibling tests cannot contribute to those assertions.
    database = f"content_publication_{uuid4().hex}"
    admin = create_session_engine(content_postgres_target).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(content_postgres_target).set(database=database)
    engine = create_session_engine(url.render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as connection:
            ensure_test_identity(connection, identity_id="alice")
        service = FencedSessionServiceHarness(
            engine,
            data_dir=tmp_path,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.pg.content_publication"),
        )
        yield service, engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()
