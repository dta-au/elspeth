"""Private plural authority and current-record refusals on isolated PostgreSQL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import structlog
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as _operation_store
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.composer import test_reviewed_plural_authority as unit
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from xdist import is_xdist_worker

from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer
operation_store = _operation_store


@pytest.fixture(scope="module")
def operation_postgres_target(request: pytest.FixtureRequest) -> Iterator[str]:
    if is_xdist_worker(request) or os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError("Reviewed plural PostgreSQL controls must run serially with -n 0.")
    with postgres_test_target(mem_limit="256m", nano_cpus=500_000_000) as target:
        yield target


@pytest.fixture
def authority_harness(operation_store, tmp_path: Path) -> unit._AuthorityHarness:
    # operation_store creates and owns one fresh UUID-named database per case.
    # Its fixture owns engine disposal and database cleanup; do not reset it here.
    engine, repository, _authority, service, session_id = operation_store
    assert engine.dialect.name == "postgresql"
    # The shared blob helper acquires a real CREATE fence as this owner.
    # The session harness uses the original operation_store owner for COMPOSE.
    _register_instance(engine, instance_id="test-fenced-owner", lease_delta=timedelta(minutes=10))
    sessions = FencedSessionServiceHarness(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg.reviewed_plural_authority"),
        session_operation_authority=repository,
        owner_instance_id=service.session_operation_owner_instance_id,
    )
    return unit._AuthorityHarness(engine, sessions, BlobServiceImpl(engine, tmp_path), create_catalog_service(), session_id, tmp_path)


def test_unchanged_persisted_plural_entries_authorize_private_reuse_postgres(authority_harness: unit._AuthorityHarness) -> None:
    unit.test_unchanged_persisted_plural_entries_authorize_private_reuse(authority_harness)


def test_one_attachment_may_be_reused_across_reviewed_roots_postgres(authority_harness: unit._AuthorityHarness) -> None:
    unit.test_one_attachment_may_be_reused_across_reviewed_roots(authority_harness)


@pytest.mark.parametrize(
    "perturbation",
    (
        "payload_ref",
        "filename",
        "mime_type",
        "size_bytes",
        "foreign_session_same_user",
        "foreign_session_other_user",
        "not_ready_pending",
        "not_ready_error",
        "llm_generated",
        "disambiguated",
        "llm_generated_then_amended",
        "missing",
    ),
)
def test_reviewed_plural_entry_revalidation_postgres(authority_harness: unit._AuthorityHarness, perturbation: str) -> None:
    unit.test_reviewed_plural_entry_revalidation(authority_harness, perturbation)
