"""Canonical durable-image assertions for state-engine tests."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from tests.helpers.state_engine import (
    EXCLUDED_STATE_ENGINE_TABLES,
    STATE_ENGINE_TABLES,
    canonicalize_state_value,
    capture_state_engine_image,
)

from elspeth.contracts import NodeType, PipelineRow
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import metadata

_OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
_CATALOG_SHA256 = "0" * 64


class _ExampleEnum(StrEnum):
    READY = "ready"


@dataclass(frozen=True)
class _SeededRun:
    db: LandscapeDB
    factory: RecorderFactory
    database_url: str
    run_id: str
    work_item_id: str
    worker_id: str
    now: datetime


@pytest.fixture
def seeded_run(tmp_path: Path) -> Generator[_SeededRun, None, None]:
    database_url = f"sqlite:///{tmp_path / 'state-engine-image.db'}"
    db = LandscapeDB(database_url)
    factory = RecorderFactory(db)
    worker_id = "worker:image:leader"
    run = factory.run_lifecycle.begin_run(
        config={"test": "state-engine-image"},
        canonical_version="v1",
        run_id="run-image",
        openrouter_catalog_sha256=_CATALOG_SHA256,
        openrouter_catalog_source="bundled",
        leader_worker_id=worker_id,
    )
    source = factory.data_flow.register_node(
        run_id=run.run_id,
        plugin_name="test_source",
        node_type=NodeType.SOURCE,
        plugin_version="1",
        config={},
        node_id="source",
        sequence=0,
        schema_config=_OBSERVED_SCHEMA,
    )
    transform = factory.data_flow.register_node(
        run_id=run.run_id,
        plugin_name="test_transform",
        node_type=NodeType.TRANSFORM,
        plugin_version="1",
        config={},
        node_id="transform",
        sequence=1,
        schema_config=_OBSERVED_SCHEMA,
    )
    factory.run_lifecycle.record_run_source(
        run_id=run.run_id,
        source_node_id=source.node_id,
        source_name="primary",
        plugin_name="test_source",
        config_hash="source-config",
        lifecycle_state="loaded",
    )
    row = factory.data_flow.create_row(
        run_id=run.run_id,
        source_node_id=source.node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"id": 1},
    )
    token = factory.data_flow.create_token(row.row_id)
    now = datetime.now(UTC)
    item = factory.scheduler.enqueue_ready(
        run_id=run.run_id,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=transform.node_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=factory.scheduler.serialize_row_payload(
            PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
        ),
        available_at=now,
    )
    claimed = factory.scheduler.claim_ready(
        run_id=run.run_id,
        lease_owner=worker_id,
        lease_seconds=30,
        now=now,
    )
    assert claimed is not None and claimed.work_item_id == item.work_item_id

    seeded = _SeededRun(
        db=db,
        factory=factory,
        database_url=database_url,
        run_id=run.run_id,
        work_item_id=item.work_item_id,
        worker_id=worker_id,
        now=now,
    )
    try:
        yield seeded
    finally:
        db.close()


def test_state_engine_table_inventory_covers_every_run_owned_table() -> None:
    assert set(STATE_ENGINE_TABLES) == set(metadata.tables) - set(EXCLUDED_STATE_ENGINE_TABLES)
    assert len(STATE_ENGINE_TABLES) == 44


def test_canonicalize_state_value_normalizes_temporal_enum_and_binary_values() -> None:
    value = {
        "binary": memoryview(b"durable bytes"),
        "enum": _ExampleEnum.READY,
        "timestamp": datetime(2026, 8, 12, 12, 30, 15, 123456, tzinfo=UTC),
    }

    assert canonicalize_state_value(value) == {
        "binary": "sha256:849e9d3592edcb72635d1e74af2b7ded2c07f6b79f4b27de7e4bc2e507169213",
        "enum": "ready",
        "timestamp": "2026-08-12T12:30:15.123456Z",
    }


def test_capture_is_exact_for_factory_database_and_explicit_url(seeded_run: _SeededRun) -> None:
    from_factory = capture_state_engine_image(seeded_run.factory, run_id=seeded_run.run_id)
    from_database = capture_state_engine_image(seeded_run.db, run_id=seeded_run.run_id)
    from_url = capture_state_engine_image(seeded_run.database_url, run_id=seeded_run.run_id)

    assert from_factory == from_database == from_url
    assert set(from_factory.tables) == set(STATE_ENGINE_TABLES)
    assert from_factory.tables["runs"][0]["run_id"] == seeded_run.run_id


def test_durable_image_reports_only_allowlisted_delta(seeded_run: _SeededRun) -> None:
    before = capture_state_engine_image(seeded_run.factory, run_id=seeded_run.run_id)
    seeded_run.factory.scheduler.heartbeat_lease(
        run_id=seeded_run.run_id,
        work_item_id=seeded_run.work_item_id,
        lease_owner=seeded_run.worker_id,
        lease_seconds=60,
        now=seeded_run.now,
        membership_fenced=True,
    )
    after = capture_state_engine_image(seeded_run.factory, run_id=seeded_run.run_id)

    delta = before.diff(after)
    assert delta.changed_tables == {"token_work_items"}
    assert delta.changed_columns == {"token_work_items": {"lease_expires_at"}}
    delta.assert_only({"token_work_items": {"lease_expires_at"}})
    with pytest.raises(AssertionError, match="unexpected durable image delta"):
        delta.assert_only({"token_work_items": {"updated_at"}})


def test_capture_excludes_rows_owned_by_another_run(seeded_run: _SeededRun) -> None:
    seeded_run.factory.run_lifecycle.begin_run(
        config={"test": "foreign"},
        canonical_version="v1",
        run_id="run-foreign",
        openrouter_catalog_sha256=_CATALOG_SHA256,
        openrouter_catalog_source="bundled",
        leader_worker_id="worker:foreign:leader",
    )

    image = capture_state_engine_image(seeded_run.factory, run_id=seeded_run.run_id)

    assert [row["run_id"] for row in image.tables["runs"]] == [seeded_run.run_id]
