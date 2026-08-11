"""TS-00/TS-01 queue replay, refusal, and atomicity contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from scripts.state_engine_profile_reporter import RuntimeProfileReporter
from sqlalchemy import insert

from elspeth.contracts import NodeType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    nodes_table,
    rows_table,
    run_workers_table,
    runs_table,
    tokens_table,
)
from tests.helpers.state_engine import capture_state_engine_image


@dataclass(frozen=True)
class _QueueHarness:
    db: LandscapeDB
    repo: TokenSchedulerRepository
    payload: str
    now: datetime


def _make_repository() -> _QueueHarness:
    db = LandscapeDB("sqlite:///:memory:")
    now = datetime.now(UTC)

    with db.engine.begin() as conn:
        for run_id in ("run-a", "run-b"):
            conn.execute(
                insert(runs_table).values(
                    run_id=run_id,
                    started_at=now,
                    config_hash="config",
                    settings_json="{}",
                    canonical_version="v1",
                    status="running",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
            conn.execute(
                insert(nodes_table).values(
                    run_id=run_id,
                    node_id="source-0",
                    plugin_name="csv",
                    node_type=NodeType.SOURCE.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="config",
                    config_json="{}",
                    registered_at=now,
                )
            )

        for run_id, node_id, row_id, token_id in (
            ("run-a", "normalize-a", "row-a", "token-a"),
            ("run-b", "normalize-b", "row-b", "token-b"),
        ):
            conn.execute(
                insert(nodes_table).values(
                    run_id=run_id,
                    node_id=node_id,
                    plugin_name="identity",
                    node_type=NodeType.TRANSFORM.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="config",
                    config_json="{}",
                    registered_at=now,
                )
            )
            conn.execute(
                insert(rows_table).values(
                    row_id=row_id,
                    run_id=run_id,
                    source_node_id="source-0",
                    row_index=0,
                    source_row_index=0,
                    ingest_sequence=0,
                    source_data_hash=f"hash-{row_id}",
                    created_at=now,
                )
            )
            conn.execute(
                insert(tokens_table).values(
                    token_id=token_id,
                    row_id=row_id,
                    run_id=run_id,
                    created_at=now,
                )
            )
        conn.execute(
            insert(run_workers_table).values(
                worker_id="worker-a",
                run_id="run-a",
                role="follower",
                status="active",
                registered_at=now,
                heartbeat_expires_at=now + timedelta(hours=1),
            )
        )

    payload = TokenSchedulerRepository.serialize_row_payload(
        PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    )
    return _QueueHarness(db=db, repo=TokenSchedulerRepository(db.engine), payload=payload, now=now)


@pytest.fixture
def queue_harness() -> Generator[_QueueHarness, None, None]:
    harness = _make_repository()
    try:
        yield harness
    finally:
        harness.db.close()


def _enqueue_ready(
    repo: TokenSchedulerRepository,
    *,
    payload: str,
    available_at: datetime,
    token_id: str = "token-a",
    row_id: str = "row-a",
    node_id: str = "normalize-a",
    ingest_sequence: int = 0,
    step_index: int = 1,
) -> TokenWorkItem:
    return repo.enqueue_ready(
        run_id="run-a",
        token_id=token_id,
        row_id=row_id,
        node_id=node_id,
        step_index=step_index,
        ingest_sequence=ingest_sequence,
        row_payload_json=payload,
        available_at=available_at,
    )


def _enqueue_ready_claimed(
    repo: TokenSchedulerRepository,
    *,
    payload: str,
    available_at: datetime,
    now: datetime,
) -> TokenWorkItem:
    return repo.enqueue_ready_claimed(
        run_id="run-a",
        token_id="token-a",
        row_id="row-a",
        node_id="normalize-a",
        step_index=1,
        ingest_sequence=0,
        row_payload_json=payload,
        available_at=available_at,
        lease_owner="worker-a",
        lease_seconds=30,
        now=now,
    )


def _inject_event_failure(
    repo: TokenSchedulerRepository,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failed_event: SchedulerEventType,
) -> None:
    original_record = repo.events.record

    def fail_selected_event(conn: Any, *, event_type: SchedulerEventType, **kwargs: Any) -> None:
        if event_type is failed_event:
            raise LandscapeRecordError(f"forced {failed_event.value} event failure")
        original_record(conn, event_type=event_type, **kwargs)

    monkeypatch.setattr(repo.events, "record", fail_selected_event)


def test_exact_ready_replay_preserves_complete_state_engine_image(
    queue_harness: _QueueHarness,
    request: pytest.FixtureRequest,
) -> None:
    first = _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now)
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")

    replayed = _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now)

    assert replayed == first
    assert capture_state_engine_image(queue_harness.db, run_id="run-a") == before

    if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
        reporter = cast(RuntimeProfileReporter, request.getfixturevalue("state_engine_profile"))
        with queue_harness.db.engine.connect() as conn:
            raw_connection = conn.connection.driver_connection
            assert isinstance(raw_connection, sqlite3.Connection)
            reporter.observe_sqlite(raw_connection, deployment="single-process-leader")


def test_incompatible_ready_replay_preserves_complete_state_engine_image(queue_harness: _QueueHarness) -> None:
    _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now)
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")

    with pytest.raises(LandscapeRecordError, match="incompatible existing work item"):
        _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now, step_index=2)

    assert capture_state_engine_image(queue_harness.db, run_id="run-a") == before


@pytest.mark.parametrize(
    ("token_id", "row_id", "node_id", "ingest_sequence", "message"),
    (
        pytest.param("token-b", "row-b", "normalize-a", 0, r"token_id='token-b'.*run_id='run-a'", id="token-cross-run"),
        pytest.param("token-a", "row-a", "normalize-b", 0, r"node_id='normalize-b'.*run_id='run-a'", id="node-cross-run"),
        pytest.param("token-a", "row-a", "normalize-a", 7, r"ingest_sequence=0.*not scheduled ingest_sequence=7", id="ingest"),
    ),
)
def test_reference_validation_refusal_preserves_complete_state_engine_image(
    token_id: str,
    row_id: str,
    node_id: str,
    ingest_sequence: int,
    message: str,
    queue_harness: _QueueHarness,
) -> None:
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")

    with pytest.raises(AuditIntegrityError, match=message):
        _enqueue_ready(
            queue_harness.repo,
            payload=queue_harness.payload,
            available_at=queue_harness.now,
            token_id=token_id,
            row_id=row_id,
            node_id=node_id,
            ingest_sequence=ingest_sequence,
        )

    assert capture_state_engine_image(queue_harness.db, run_id="run-a") == before


def test_existing_ready_replay_emits_claim_only_and_preserves_every_other_field(queue_harness: _QueueHarness) -> None:
    _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now)
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")

    claimed = _enqueue_ready_claimed(
        queue_harness.repo,
        payload=queue_harness.payload,
        available_at=queue_harness.now,
        now=queue_harness.now + timedelta(seconds=1),
    )
    after = capture_state_engine_image(queue_harness.db, run_id="run-a")

    assert claimed.status is TokenWorkStatus.LEASED
    added_events = tuple(event for event in after.tables["scheduler_events"] if event not in before.tables["scheduler_events"])
    assert len(added_events) == 1
    delta = before.diff(after)
    assert delta.changed_tables == {"token_work_items", "scheduler_events"}
    delta.assert_only(
        {
            "token_work_items": {"status", "lease_owner", "lease_expires_at", "updated_at"},
            "scheduler_events": set(added_events[0]),
        }
    )
    work_item = after.tables["token_work_items"][0]
    assert work_item["status"] == TokenWorkStatus.LEASED.value
    assert work_item["lease_owner"] == "worker-a"
    assert work_item["lease_expires_at"] == (queue_harness.now + timedelta(seconds=31)).isoformat().replace("+00:00", "Z")
    assert work_item["updated_at"] == (queue_harness.now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    assert all(event in after.tables["scheduler_events"] for event in before.tables["scheduler_events"])
    assert added_events[0]["event_type"] == SchedulerEventType.CLAIM_READY.value
    event_types = tuple(cast(str, event["event_type"]) for event in after.tables["scheduler_events"])
    assert sorted(event_types) == sorted((SchedulerEventType.ENQUEUE.value, SchedulerEventType.CLAIM_READY.value))


@pytest.mark.parametrize("failed_event", (SchedulerEventType.ENQUEUE, SchedulerEventType.CLAIM_READY))
def test_new_enqueue_and_claim_event_failure_rolls_back_complete_state_engine_image(
    monkeypatch: pytest.MonkeyPatch,
    failed_event: SchedulerEventType,
    queue_harness: _QueueHarness,
) -> None:
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")
    _inject_event_failure(queue_harness.repo, monkeypatch, failed_event=failed_event)

    with pytest.raises(LandscapeRecordError, match=f"forced {failed_event.value} event failure"):
        _enqueue_ready_claimed(
            queue_harness.repo,
            payload=queue_harness.payload,
            available_at=queue_harness.now,
            now=queue_harness.now + timedelta(seconds=1),
        )

    assert capture_state_engine_image(queue_harness.db, run_id="run-a") == before


def test_existing_ready_claim_event_failure_rolls_back_complete_state_engine_image(
    monkeypatch: pytest.MonkeyPatch,
    queue_harness: _QueueHarness,
) -> None:
    _enqueue_ready(queue_harness.repo, payload=queue_harness.payload, available_at=queue_harness.now)
    before = capture_state_engine_image(queue_harness.db, run_id="run-a")
    _inject_event_failure(queue_harness.repo, monkeypatch, failed_event=SchedulerEventType.CLAIM_READY)

    with pytest.raises(LandscapeRecordError, match="forced claim_ready event failure"):
        _enqueue_ready_claimed(
            queue_harness.repo,
            payload=queue_harness.payload,
            available_at=queue_harness.now,
            now=queue_harness.now + timedelta(seconds=1),
        )

    assert capture_state_engine_image(queue_harness.db, run_id="run-a") == before
