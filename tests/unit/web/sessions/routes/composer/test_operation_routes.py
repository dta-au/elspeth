"""The durable Composer operation routes: admission, observation, discovery and Stop.

Design: docs/architecture/design-notes/0.8.10-continuation-protocol-design.md
§3.3, as narrowed by the single-lifecycle wire contract (0.8.10): no version
header is negotiated and no lifecycle or protocol version is on the wire.
Each test names its mutation control in its docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from tests.helpers.composer_continuation_rows import PRIVATE_MARKERS, insert_v2_operation
from tests.helpers.composer_operations import build_composer_operation_app, message_body

from elspeth.web.sessions.models import composer_async_operations_table, composer_operation_segments_table, rate_limit_events_table
from elspeth.web.sessions.schemas import ComposerOperationSegment, ComposerOperationSnapshot, ComposerOperationStatusPayload

# The retired negotiation header. The server neither reads nor sends it.
RETIRED_VERSION_HEADER = "Composer-Operation-Version"


def _client(setup, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {setup.token}", **headers},
    )


def _row_counts(app) -> tuple[int, int]:
    with app.state.session_engine.connect() as conn:
        jobs = conn.execute(select(func.count()).select_from(composer_async_operations_table)).scalar_one()
        quota = conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one()
    return jobs, quota


def _operation_bundle(app, operation_id: str):
    with app.state.session_engine.connect() as conn:
        parent = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == operation_id)
        ).one()
        segments = tuple(
            conn.execute(
                select(composer_operation_segments_table)
                .where(composer_operation_segments_table.c.operation_id == operation_id)
                .order_by(composer_operation_segments_table.c.sequence)
            )
        )
    return parent, segments


# --- the retired version header ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", "2", "3", "v2"])
@pytest.mark.parametrize("route", ["messages", "get", "cancel", "active"])
async def test_a_retired_version_request_header_changes_nothing(tmp_path, route, value) -> None:
    """Wire contract §4: the header is ignored like any unknown header.
    Control: re-add a 406/409 refusal keyed on the header -> red."""
    setup = await build_composer_operation_app(tmp_path)
    base = f"/api/sessions/{setup.session_id}"
    operation_id = str(uuid4())
    async with _client(setup, **{RETIRED_VERSION_HEADER: value}) as client:
        if route == "messages":
            response = await client.post(f"{base}/messages", json=message_body("Ignored header", operation_id=operation_id))
            expected = 202
        elif route == "get":
            response = await client.get(f"{base}/operations/{operation_id}")
            expected = 404
        elif route == "cancel":
            response = await client.post(f"{base}/operations/{operation_id}/cancel")
            expected = 404
        else:
            response = await client.get(f"{base}/operations/active")
            expected = 204
    assert response.status_code == expected, response.text
    assert RETIRED_VERSION_HEADER.lower() not in {name.lower() for name in response.headers}


@pytest.mark.asyncio
async def test_system_status_carries_no_admission_version(tmp_path) -> None:
    """Wire contract §5. Control: re-add composer_operation_admission_version -> red."""
    setup = await build_composer_operation_app(tmp_path)
    async with _client(setup) as client:
        response = await client.get("/api/system/status")
    assert response.status_code == 200
    assert "composer_operation_admission_version" not in response.json()


# --- observation -----------------------------------------------------------


def _seed(setup, state, **kwargs):
    return insert_v2_operation(
        setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="transport-user", state=state, **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "status", "phase", "segment_state"),
    [("queued", "queued", "queued", "queued"), ("running", "running", "executing", "running"), ("failed", "failed", "settled", None)],
)
async def test_get_returns_the_operation_snapshot(tmp_path, state, status, phase, segment_state) -> None:
    """Control: drop a field from the snapshot projection -> strict validation goes red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, state, status_revision=5)
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
    assert not {"protocol_version", "lifecycle_version"} & set(response.json())
    assert (snapshot.operation_id, snapshot.status, snapshot.phase) == (seeded.operation_id, status, phase)
    assert snapshot.status_revision == 5
    if segment_state is None:
        assert snapshot.segment is None and snapshot.error is not None and snapshot.error.failure_code == "request_cancelled"
        assert snapshot.cancel_requested is True
    else:
        assert snapshot.segment is not None and snapshot.segment.state == segment_state
        assert (snapshot.segment.deadline_at is not None) == (segment_state == "running")
        assert snapshot.result is None and snapshot.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running", "failed"])
async def test_snapshot_carries_no_private_custody_value(tmp_path, state) -> None:
    """B-10 (GET). Control: add checkpoint_id or the fence id to the snapshot -> red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, state)
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}")
    assert response.status_code == 200
    for marker in (*PRIVATE_MARKERS.values(), seeded.checkpoint_id):
        assert marker not in response.text
    assert not {"checkpoint_id", "journal_cursor", "claim_token", "session_operation_id"} & set(response.json())


def _status_fields(**overrides):
    now = datetime.now(UTC)
    fields = {
        "operation_id": str(uuid4()),
        "kind": "compose_message",
        "status": "running",
        "phase": "executing",
        "status_revision": 1,
        "created_at": now,
        "observed_at": now,
        "poll_after_ms": 1000,
        "cancel_requested": False,
        "segment": ComposerOperationSegment(sequence=1, generation=1, state="running", deadline_at=now),
    }
    fields.update(overrides)
    return fields


_TERMINAL = {"phase": "settled", "segment": None}


def test_status_valid_counterparts_construct() -> None:
    ComposerOperationStatusPayload(**_status_fields())
    ComposerOperationStatusPayload(**_status_fields(status="failed", cancel_requested=True, **_TERMINAL))
    ComposerOperationStatusPayload(**_status_fields(status="completed", **_TERMINAL))


@pytest.mark.parametrize(
    "overrides",
    [
        {"segment": None},
        {"status": "completed", "cancel_requested": True, **_TERMINAL},
        {"phase": "settled"},
        {"status": "failed", "phase": "settled"},
        {"unexpected": 1},
    ],
    ids=["live-without-segment", "completed-cancelled", "live-settled-phase", "terminal-with-segment", "extra-field"],
)
def test_status_rejects_each_single_violation(overrides) -> None:
    """B-9: each case violates exactly one rule; the counterparts above are valid.
    Control: drop that rule or extra='forbid' -> the matching case goes red."""
    with pytest.raises(ValidationError):
        ComposerOperationStatusPayload(**_status_fields(**overrides))


def test_snapshot_result_bundle_rejects_each_single_violation() -> None:
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    error = request_cancelled_error(request_id=None)
    failed = _status_fields(status="failed", cancel_requested=True, **_TERMINAL)
    ComposerOperationSnapshot(**failed, error=error)
    with pytest.raises(ValidationError):
        ComposerOperationSnapshot(**failed)
    with pytest.raises(ValidationError):
        ComposerOperationSnapshot(**_status_fields(), error=error)
    with pytest.raises(ValidationError):
        ComposerOperationSnapshot(**failed, error=error, unexpected=1)


@pytest.mark.parametrize(("state", "deadline"), [("running", None), ("queued", datetime.now(UTC))])
def test_only_a_running_segment_has_a_deadline(state, deadline) -> None:
    with pytest.raises(ValidationError):
        ComposerOperationSegment(sequence=1, generation=1, state=state, deadline_at=deadline)


# --- step 4: discovery of the live operation -----------------------------


@pytest.mark.asyncio
async def test_active_operation_is_204_when_the_session_is_idle(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/active")
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_active_operation_names_a_freshly_admitted_operation(tmp_path) -> None:
    """D-8. Control: return 204 unconditionally -> red."""
    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(uuid4())
    async with _client(setup) as client:
        admitted = await client.post(
            f"/api/sessions/{setup.session_id}/messages", json=message_body("Discover me", operation_id=operation_id)
        )
        assert admitted.status_code == 202
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/active")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"operation_id": operation_id, "kind": "compose_message", "status": "queued"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running"])
async def test_active_operation_names_a_live_operation_in_any_phase(tmp_path, state) -> None:
    setup = await build_composer_operation_app(tmp_path)
    seeded = insert_v2_operation(setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="transport-user", state=state)
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/active")
    assert response.status_code == 200, response.text
    assert response.json() == {"operation_id": seeded.operation_id, "kind": "compose_message", "status": state}


@pytest.mark.asyncio
async def test_active_operation_ignores_a_terminal_operation(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    insert_v2_operation(setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="transport-user", state="failed")
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/active")
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_active_operation_never_names_another_actors_row(tmp_path) -> None:
    """B-14 row check. Session ownership already refuses foreign sessions, so the fixture plants a
    live row whose actor is not the session owner. Control: drop the actor filter -> red."""
    from tests.fixtures.identities import ensure_test_identity

    setup = await build_composer_operation_app(tmp_path)
    with setup.app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="someone-else")
    insert_v2_operation(setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="someone-else", state="queued")
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/active")
    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_active_operation_on_a_foreign_session_is_non_disclosing_404(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{uuid4()}/operations/active")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


# --- admission acknowledgement -----------------------------------------------


@pytest.mark.asyncio
async def test_admission_returns_the_operation_snapshot(tmp_path) -> None:
    """Control: return a bare acknowledgement instead of the snapshot -> red."""
    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(uuid4())
    async with _client(setup) as client:
        response = await client.post(
            f"/api/sessions/{setup.session_id}/messages", json=message_body("Continue me", operation_id=operation_id)
        )
    assert response.status_code == 202, response.text
    snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
    assert (snapshot.operation_id, snapshot.status, snapshot.phase, snapshot.status_revision) == (operation_id, "queued", "queued", 0)
    assert snapshot.segment is not None and (snapshot.segment.sequence, snapshot.segment.state) == (1, "queued")
    parent, segments = _operation_bundle(setup.app, operation_id)
    assert parent.current_segment_sequence == 1 and len(segments) == 1


@pytest.mark.asyncio
async def test_admission_replay_returns_the_same_row(tmp_path) -> None:
    """An identical replay creates nothing new. Control: re-admit instead of replaying -> red (row count)."""
    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(uuid4())
    body = message_body("Continue me once", operation_id=operation_id)
    async with _client(setup) as client:
        first = await client.post(f"/api/sessions/{setup.session_id}/messages", json=body)
        before = _row_counts(setup.app)
        second = await client.post(f"/api/sessions/{setup.session_id}/messages", json=body)
    assert first.status_code == second.status_code == 202
    assert second.json()["operation_id"] == operation_id
    assert _row_counts(setup.app)[0] == before[0]


# --- review R-1: the projection reads a consistent parent/segment pair ---


@pytest.mark.asyncio
async def test_projection_rereads_when_a_segment_start_commits_between_reads(tmp_path, monkeypatch) -> None:
    """A segment start changes queued -> running without changing (sequence, generation), and bumps the
    parent revision in the same transaction. Control: drop the second parent read's identity check -> red
    (a revision-4 parent paired with the stale queued segment)."""
    from datetime import timedelta

    from sqlalchemy import update

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions import composer_operation_projection as projection
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    setup = await build_composer_operation_app(tmp_path)
    engine = setup.app.state.session_engine
    seeded = insert_v2_operation(engine, session_id=setup.session_id, actor_user_id="transport-user", state="queued", status_revision=3)
    real = projection._read_segment_row
    calls = []

    def start_segment_after_reading(*args, **kwargs):
        row = real(*args, **kwargs)
        if not calls:
            with engine.begin() as conn:
                now = database_now(conn)
                conn.execute(
                    update(segments)
                    .where(segments.c.operation_id == seeded.operation_id)
                    .values(state="running", started_at=now, cooperative_at=now, deadline_at=now + timedelta(minutes=5))
                )
                conn.execute(
                    update(composer_async_operations_table)
                    .where(composer_async_operations_table.c.operation_id == seeded.operation_id)
                    .values(status="running", phase="executing", started_at=now, status_revision=4)
                )
        calls.append(1)
        return row

    monkeypatch.setattr(projection, "_read_segment_row", start_segment_after_reading)
    inputs = projection.read_projection_inputs(
        setup.app.state.composer_async_operation_authority, engine, session_id=setup.session_id, operation_id=seeded.operation_id
    )
    assert len(calls) == 2
    assert inputs.record is not None and inputs.record.status_revision == 4
    assert inputs.segment is not None and inputs.segment.state == "running" and inputs.segment.deadline_at is not None


# --- review R-3, R-7: refusal ordering and echoes ------------------------


@pytest.mark.asyncio
async def test_archived_session_stream_is_non_disclosing_404(tmp_path) -> None:
    """R-3. Control: read the operation before session authorization -> red (operation metadata disclosed)."""
    from sqlalchemy import update

    from elspeth.web.sessions.models import sessions_table

    setup = await build_composer_operation_app(tmp_path)
    seeded = insert_v2_operation(
        setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="transport-user", state="running"
    )
    with setup.app.state.session_engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(setup.session_id)).values(archived_at=datetime.now(UTC)))
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/stream")
    assert response.status_code == 404, response.text
    assert seeded.operation_id not in response.text


@pytest.mark.asyncio
async def test_operation_404_is_the_fixed_non_disclosing_body(tmp_path) -> None:
    """Wire contract §7. Control: name the operation in the 404 -> red."""
    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(uuid4())
    async with _client(setup) as client:
        response = await client.get(f"/api/sessions/{setup.session_id}/operations/{operation_id}")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    detail = response.json()
    detail.pop("request_id", None)
    assert detail == {"detail": "Operation not found"}


# --- Stop ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running"])
async def test_stop_marks_once_and_returns_the_live_snapshot(tmp_path, monkeypatch, state) -> None:
    """Control: retain the interim refusal, omit revision, or settle directly -> red.

    This drives the real HTTP and SQL authorities without starting the worker;
    its wake and local-cancel calls are scheduling hints, not settlement proof.
    """
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, state)
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))
    async with _client(setup) as client:
        path = f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel"
        first = await client.post(path)
        first_bundle = _operation_bundle(setup.app, seeded.operation_id)
        second = await client.post(path)
        assert _operation_bundle(setup.app, seeded.operation_id) == first_bundle
    for response in (first, second):
        assert response.status_code == 202, response.text
        assert response.headers["cache-control"] == "no-store"
        snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
        assert snapshot.status == state
        assert snapshot.cancel_requested is True
        assert snapshot.status_revision == seeded.status_revision + 1
        assert snapshot.segment is not None and snapshot.segment.state == state
        assert snapshot.result is None and snapshot.error is None
        for marker in (*PRIVATE_MARKERS.values(), seeded.checkpoint_id):
            assert marker not in response.text
    assert hints == [
        {"session_id": setup.session_id, "operation_id": seeded.operation_id},
        "wake",
        {"session_id": setup.session_id, "operation_id": seeded.operation_id},
        "wake",
    ]
    with setup.app.state.session_engine.connect() as conn:
        row = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert row.cancel_requested_at is not None
    assert row.result_json is None
    assert row.status_revision == seeded.status_revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "segment_state"), [("continuation_queued", "queued"), ("retry_waiting", "queued"), ("reconciling", "interrupted")]
)
async def test_stop_wakes_a_backed_off_segment_when_the_parent_status_is_running(tmp_path, monkeypatch, phase, segment_state) -> None:
    """ST-1. Control: notify only parent status queued -> no wake for this running parent."""
    from datetime import timedelta

    from sqlalchemy import update

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, "running")
    with setup.app.state.session_engine.begin() as conn:
        delayed_until = database_now(conn) + timedelta(hours=1)
        conn.execute(
            update(composer_async_operations_table)
            .where(composer_async_operations_table.c.operation_id == seeded.operation_id)
            .values(phase=phase)
        )
        conn.execute(
            update(segments)
            .where(segments.c.operation_id == seeded.operation_id)
            .values(
                state=segment_state,
                claim_token=None,
                claim_owner_instance_id=None,
                claim_expires_at=None,
                next_eligible_at=delayed_until,
                started_at=None if segment_state == "queued" else segments.c.started_at,
                cooperative_at=None if segment_state == "queued" else segments.c.cooperative_at,
                deadline_at=None if segment_state == "queued" else segments.c.deadline_at,
            )
        )
    hints = []
    monkeypatch.setattr(setup.app.state.composer_async_worker, "notify_work", lambda: hints.append("wake"))
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "running"
    assert response.json()["phase"] == phase
    assert response.json()["segment"]["state"] == segment_state
    assert hints == ["wake"]
    with setup.app.state.session_engine.connect() as conn:
        marker = conn.execute(
            select(composer_async_operations_table.c.cancel_requested_at).where(
                composer_async_operations_table.c.operation_id == seeded.operation_id
            )
        ).scalar_one()
        eligible_at = conn.execute(select(segments.c.next_eligible_at).where(segments.c.operation_id == seeded.operation_id)).scalar_one()
    assert eligible_at == marker
    assert eligible_at.replace(tzinfo=None) < delayed_until.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_stop_of_a_terminal_failure_is_read_only(tmp_path, monkeypatch) -> None:
    """ST-6. Control: mark or wake a terminal operation -> red."""
    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, "failed")
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))
    with setup.app.state.session_engine.connect() as conn:
        before = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 200, response.text
    snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
    assert snapshot.status == "failed" and snapshot.error is not None
    assert snapshot.status_revision == seeded.status_revision
    assert hints == []
    with setup.app.state.session_engine.connect() as conn:
        after = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert after == before


@pytest.mark.asyncio
async def test_stop_preserves_completed_result_without_marking_or_waking(tmp_path, monkeypatch) -> None:
    """ST-6/ST-7. Control: mark a completion -> red.

    The persisted completed fixture includes real ingress and assistant rows;
    it proves terminal HTTP behavior, not live V2 worker completion.
    """
    from sqlalchemy import insert, update

    from elspeth.web.coordination.composer_operation_authority import _terminal_values
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.sessions.schemas import ChatMessageResponse, MessageWithStateResponse

    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, "running")
    with setup.app.state.session_engine.begin() as conn:
        row = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
        now = database_now(conn)
        sequence = conn.execute(
            select(func.coalesce(func.max(chat_messages_table.c.sequence_no), 0)).where(
                chat_messages_table.c.session_id == str(setup.session_id)
            )
        ).scalar_one()
        assistant_id = str(uuid4())
        conn.execute(
            insert(chat_messages_table),
            [
                {
                    "id": row.reserved_user_message_id,
                    "session_id": str(setup.session_id),
                    "role": "user",
                    "content": "Seeded continuation request",
                    "sequence_no": sequence + 1,
                    "writer_principal": "route_user_message",
                    "created_at": now,
                },
                {
                    "id": assistant_id,
                    "session_id": str(setup.session_id),
                    "role": "assistant",
                    "content": "Finished before Stop.",
                    "sequence_no": sequence + 2,
                    "writer_principal": "compose_loop",
                    "created_at": now,
                },
            ],
        )
        result = MessageWithStateResponse(
            message=ChatMessageResponse(
                id=assistant_id,
                session_id=str(setup.session_id),
                role="assistant",
                content="Finished before Stop.",
                segments=[],
                created_at=now,
                sequence_no=sequence + 2,
            ),
            proposals=[],
        )
        conn.execute(
            update(composer_async_operations_table)
            .where(composer_async_operations_table.c.operation_id == seeded.operation_id)
            .values(
                **_terminal_values(result, now=now, settled_by="owner_terminal"),
                phase="settled",
                ingress_phase="committed",
                user_message_id=row.reserved_user_message_id,
                status_revision=seeded.status_revision + 1,
            )
        )
        conn.execute(
            update(composer_operation_segments_table)
            .where(composer_operation_segments_table.c.operation_id == seeded.operation_id)
            .values(state="settled", claim_token=None, claim_owner_instance_id=None, claim_expires_at=None)
        )
    before = _operation_bundle(setup.app, seeded.operation_id)
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
    assert snapshot.status_revision == seeded.status_revision + 1
    assert snapshot.status == "completed" and snapshot.cancel_requested is False
    assert snapshot.result == result and snapshot.error is None
    assert _operation_bundle(setup.app, seeded.operation_id) == before
    assert before[0].cancel_requested_at is None
    assert hints == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "failed"])
@pytest.mark.parametrize("change", ["owner", "provider", "archive"])
async def test_stop_rechecks_owner_provider_and_archive_inside_its_transaction(tmp_path, monkeypatch, state, change) -> None:
    """Control: authorize only before the locked transaction or after terminal return -> red."""
    from sqlalchemy import update
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.web.sessions.models import sessions_table

    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, state)
    authority = setup.app.state.composer_async_operation_authority
    request_owner_cancel = authority.request_owner_cancel
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))

    def change_ownership_before_cancel(**kwargs):
        with setup.app.state.session_engine.begin() as conn:
            ensure_test_identity(conn, identity_id="replacement-owner")
            values = (
                {"user_id": "replacement-owner"}
                if change == "owner"
                else {"auth_provider_type": "entra"}
                if change == "provider"
                else {"archived_at": datetime.now(UTC)}
            )
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(setup.session_id)).values(**values))
        return request_owner_cancel(**kwargs)

    monkeypatch.setattr(authority, "request_owner_cancel", change_ownership_before_cancel)
    with setup.app.state.session_engine.connect() as conn:
        before = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 404, response.text
    assert response.headers["cache-control"] == "no-store"
    assert seeded.operation_id not in response.text
    assert hints == []
    with setup.app.state.session_engine.connect() as conn:
        after = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "failed"])
async def test_owner_cancel_refuses_a_foreign_operation_actor_inside_its_transaction(tmp_path, state) -> None:
    """Control: omit the exact row actor check in the authenticated authority -> red.

    The operation actor is immutable, so this is an initially foreign row,
    rather than an impossible actor update between the route and its lock.
    """
    from tests.fixtures.identities import ensure_test_identity

    setup = await build_composer_operation_app(tmp_path)
    with setup.app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="someone-else")
    seeded = insert_v2_operation(setup.app.state.session_engine, session_id=setup.session_id, actor_user_id="someone-else", state=state)
    with setup.app.state.session_engine.connect() as conn:
        before = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert (
        setup.app.state.composer_async_operation_authority.request_owner_cancel(
            session_id=setup.session_id,
            operation_id=seeded.operation_id,
            actor_user_id="transport-user",
            auth_provider_type="local",
        )
        is None
    )
    with setup.app.state.session_engine.connect() as conn:
        after = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert after == before


@pytest.mark.asyncio
async def test_stop_rechecks_session_after_commit_before_projecting_its_ack(tmp_path, monkeypatch) -> None:
    """Control: skip the postcommit ownership check before projecting -> red."""
    from sqlalchemy import update

    from elspeth.web.sessions.models import sessions_table

    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, "queued")
    authority = setup.app.state.composer_async_operation_authority
    request_owner_cancel = authority.request_owner_cancel
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))

    def archive_after_cancel(**kwargs):
        result = request_owner_cancel(**kwargs)
        with setup.app.state.session_engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(setup.session_id)).values(archived_at=datetime.now(UTC)))
        return result

    monkeypatch.setattr(authority, "request_owner_cancel", archive_after_cancel)
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 404, response.text
    assert response.headers["cache-control"] == "no-store"
    assert seeded.operation_id not in response.text
    assert hints == []
    with setup.app.state.session_engine.connect() as conn:
        row = conn.execute(
            select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == seeded.operation_id)
        ).one()
    assert row.cancel_requested_at is not None
    assert row.status_revision == seeded.status_revision + 1


@pytest.mark.asyncio
async def test_stop_projects_a_settlement_committed_after_its_marker(tmp_path, monkeypatch) -> None:
    """Control: return the mutation's stale live row and wake a settled request -> red."""
    from sqlalchemy import update

    from elspeth.web.coordination.composer_operation_authority import _terminal_values
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    setup = await build_composer_operation_app(tmp_path)
    seeded = _seed(setup, "queued")
    authority = setup.app.state.composer_async_operation_authority
    request_owner_cancel = authority.request_owner_cancel
    hints = []
    worker = setup.app.state.composer_async_worker
    monkeypatch.setattr(worker, "notify_work", lambda: hints.append("wake"))
    monkeypatch.setattr(worker, "signal_local_cancel", lambda **key: hints.append(key))

    def settle_after_cancel(**kwargs):
        result = request_owner_cancel(**kwargs)
        with setup.app.state.session_engine.begin() as conn:
            values = _terminal_values(request_cancelled_error(request_id=None), now=database_now(conn), settled_by="owner_terminal")
            conn.execute(
                update(composer_async_operations_table)
                .where(composer_async_operations_table.c.operation_id == seeded.operation_id)
                .values(**values, phase="settled", status_revision=seeded.status_revision + 2)
            )
        return result

    monkeypatch.setattr(authority, "request_owner_cancel", settle_after_cancel)
    async with _client(setup) as client:
        response = await client.post(f"/api/sessions/{setup.session_id}/operations/{seeded.operation_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["error"]["failure_code"] == "request_cancelled"
    assert hints == []
