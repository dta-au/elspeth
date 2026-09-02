"""Fork blob custody pinned on the REAL path, not the helper.

elspeth-f478b01787 / elspeth-d178282593. The sibling file
``test_guided_operation_fork_service.py`` pins each fork-rewrite mechanism by
calling ``_rewrite_fork_state_blob_custody`` directly. These tests drive the
production chain the route runs -- ``fork_session`` -> ``copy_blobs_for_fork``
-> ``_rewrite_fork_state_blob_custody`` -> ``settle_guided_fork_operation``
-> ``get_current_state`` -- or the HTTP route itself, so that the rewriter's
output is judged by the settlement verifier and the outbound projection, the
two authorities that rejected or disclosed the parent's custody in the
incidents. Real blobs come from ``BlobServiceImpl.create_blob``; nothing here
hand-builds the settlement payload the verifier sees except where the test's
purpose is to hand the verifier a payload the rewriter would never produce.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.blobs.protocol import BlobForkWriteFence
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, composition_states_table, sessions_table
from elspeth.web.sessions.protocol import CompositionStateData, GuidedForkSettlementCommand
from elspeth.web.sessions.routes.sessions import _rewrite_fork_state_blob_custody
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl, _value_references_parent_blob
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_fork import _complete_guided_start_authority, _make_fork_app
from tests.unit.web.sessions.test_guided_operation_fork_service import _claim_fork, _service_for

_PRE_STAGING_TEXT = "retains parent blob custody the fork rewriter does not model"
_SETTLEMENT_TEXT = "Guided fork settlement state retains parent blob custody"


@pytest.fixture()
def engine():
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return engine


def _stale_report(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": 1, "entries": entries, "normalization_events": []}


def _entry(path: str, value: str) -> dict[str, object]:
    return {"path": path, "value": value, "category": "source", "provenance": "picked"}


async def _parent_blob_refs(blob_service: BlobServiceImpl, parent_id: UUID) -> frozenset[str]:
    """Exactly what the route builds: every parent blob row id and path, ANY status."""
    return frozenset(
        ref
        for parent_blob in await blob_service.list_blobs(parent_id, limit=None)
        for ref in (str(parent_blob.id), parent_blob.storage_path)
    )


async def _stage_copy_rewrite(
    service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
    *,
    parent_id: UUID,
    fork_message_id: UUID,
    data_dir: Path,
):
    """Run the route's pre-settlement chain with the route's own argument shapes."""
    fence = await _claim_fork(service, parent_id, operation_id=str(uuid4()))
    staged = await service.fork_session(fence, fork_message_id=fork_message_id, new_message_content="edited")
    assert staged.state is not None

    async def checkpoint() -> None:
        return None

    source_blobs = {entry.source_blob_id: await blob_service.get_blob(entry.source_blob_id) for entry in staged.blob_plan}
    blob_map = await blob_service.copy_blobs_for_fork(
        parent_id,
        staged.session.id,
        staged.blob_plan,
        BlobForkWriteFence(
            source_session_id=parent_id,
            target_session_id=staged.session.id,
            operation_id=fence.operation_id,
            lease_token=fence.lease_token,
            attempt=fence.attempt,
        ),
        checkpoint=checkpoint,
    )
    source_blob_path_map = {source_blobs[source_id].storage_path: copied for source_id, copied in blob_map.items()}
    rewritten = _rewrite_fork_state_blob_custody(
        staged.state,
        blob_map,
        source_blob_path_map,
        parent_blob_refs=await _parent_blob_refs(blob_service, parent_id),
        data_dir=data_dir,
        parent_session_id=parent_id,
        child_session_id=staged.session.id,
    )
    return fence, staged, blob_map, rewritten


def _settlement_command(fence, staged, rewritten: CompositionStateData | None) -> GuidedForkSettlementCommand:
    return GuidedForkSettlementCommand(
        fence=fence,
        child_session_id=staged.session.id,
        expected_current_state_id=staged.state.id,
        edited_message_id=staged.messages[-1].id,
        rewritten_state_id=uuid4() if rewritten is not None else None,
        rewritten_state=rewritten,
        response_hash="b" * 64,
        actor="composer_route",
    )


# --- T1: the incident shape, end to end through settlement -------------------


@pytest.mark.asyncio
async def test_incident_shaped_fork_settles_and_child_names_only_its_own_blob(engine, tmp_path: Path) -> None:
    """elspeth-f478b01787 on the production chain. The parent's state carries a
    stale ``implicit_decisions`` report naming its blob three ways (bare id,
    ``blob:`` sentinel, raw storage path). Before the fix the staged child kept
    that report verbatim and ``settle_guided_fork_operation`` rejected it with
    the incident's exact error AFTER staging had committed. Now the child must
    SETTLE through the same verifier, its persisted state must reference no
    parent identity by the verifier's own predicate, and its re-derived report
    must name the child's blob without ever carrying the child's raw path.
    """
    service = _service_for(engine)
    blob_service = BlobServiceImpl(engine, tmp_path / "blobs")
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "cases.csv", b"a,b\n1,2\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "data": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "P", "description": None},
            composer_meta={
                "validation_lane": "strict",
                "implicit_decisions": _stale_report(
                    [
                        _entry("source.blob_ref", str(blob.id)),
                        _entry("source.path", f"blob:{blob.id}"),
                        _entry("source.file", blob.storage_path),
                    ]
                ),
            },
        ),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )

    fence, staged, blob_map, rewritten = await _stage_copy_rewrite(
        service,
        blob_service,
        parent_id=parent.id,
        fork_message_id=message.id,
        data_dir=tmp_path,
    )
    assert len(blob_map) == 1
    child_blob = blob_map[blob.id]
    assert rewritten is not None

    settled = await service.settle_guided_fork_operation(_settlement_command(fence, staged, rewritten))
    assert settled.archived_at is None

    child_state = await service.get_current_state(staged.session.id)
    assert child_state is not None
    forbidden = frozenset({str(blob.id), blob.storage_path})
    # Judged by the settlement verifier's own predicate over the PERSISTED child
    # state, both custody-bearing columns.
    assert not _value_references_parent_blob(deep_thaw(child_state.composer_meta), forbidden)
    assert not _value_references_parent_blob(deep_thaw(child_state.sources), forbidden)
    report = deep_thaw(child_state.composer_meta)["implicit_decisions"]
    values = [entry["value"] for entry in report["entries"]]
    assert str(child_blob.id) in values, values
    # A blob_ref-bearing source records its path as the sentinel, never the
    # filesystem value, so the child's raw path must not have entered the report.
    assert child_blob.storage_path not in values, values
    # Other subsystems' keys ride through the re-derivation untouched.
    assert deep_thaw(child_state.composer_meta)["validation_lane"] == "strict"


# --- T3: the settlement verifier sees mapping KEYS -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key_shape", ["id", "sentinel", "storage_path"])
async def test_settlement_rejects_rewritten_state_keyed_by_parent_blob(engine, tmp_path: Path, key_shape: str) -> None:
    """A parent blob id, ``blob:`` sentinel or raw storage path used as a dict
    KEY inside ``composer_meta`` names the parent exactly as a value does
    (red-team B2 on ee1ae108b: it crossed both guards and was served on a 200).
    The rewriter would now refuse this shape itself, so the payload is handed
    straight to ``settle_guided_fork_operation`` -- the settlement authority
    must reject it on its own, through the real plan and cohort checks.
    """
    service = _service_for(engine)
    blob_service = BlobServiceImpl(engine, tmp_path / "blobs")
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "cases.csv", b"a,b\n1,2\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "data": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "P", "description": None},
        ),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    fence, staged, _blob_map, rewritten = await _stage_copy_rewrite(
        service,
        blob_service,
        parent_id=parent.id,
        fork_message_id=message.id,
        data_dir=tmp_path,
    )
    assert rewritten is not None
    key = {
        "id": str(blob.id),
        "sentinel": f"blob:{blob.id}",
        "storage_path": blob.storage_path,
    }[key_shape]
    # Sources are the rewriter's own (child-bound) output; only the KEY names
    # the parent, so nothing but key inspection can reject this payload.
    keyed_state = CompositionStateData(
        sources=rewritten.sources,
        nodes=rewritten.nodes,
        edges=rewritten.edges,
        outputs=rewritten.outputs,
        metadata_=rewritten.metadata_,
        composer_meta={"notes_by_blob": {key: "note"}},
    )

    with pytest.raises(AuditIntegrityError, match=_SETTLEMENT_TEXT):
        await service.settle_guided_fork_operation(_settlement_command(fence, staged, keyed_state))

    assert (await service.get_session(staged.session.id)).archived_at is not None


# --- T2: guided-native child served on GET /state ------------------------------


def _wire_state_projection(app) -> None:
    """The catalog wiring ``GET /state`` needs (as the sibling route tests do)."""
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

    catalog = create_catalog_service()
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    app.state.plugin_snapshot_factory = lambda _user: PluginAvailabilitySnapshot.for_trained_operator(catalog)


@pytest.mark.asyncio
async def test_guided_native_fork_child_serves_no_raw_storage_path(tmp_path: Path) -> None:
    """elspeth-d178282593 at the 200 boundary. A guided commit strips ``blob_ref``
    from the executable source, so the raw storage path is the live source's
    ONLY carrier and the stale parent report holds it too. Before the fix the
    forked child's ``implicit_decisions`` still named the PARENT's raw path and
    the projection -- keyed on the CHILD's live carrier -- could not mask it,
    so ``GET /state`` disclosed a private path of another session on a 200.
    Driven through the real route: POST /fork, then GET /state and
    GET /state/versions on the child; neither body may carry the parent's raw
    path nor the child's.
    """
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
    from elspeth.web.composer.guided.resolved import SourceResolved
    from elspeth.web.composer.guided.state_machine import GuidedSession, TurnRecord

    app, service, blob_service = _make_fork_app(tmp_path)
    _wire_state_projection(app)
    parent = await service.create_session("alice", "Parent", "local")
    root = await service.add_message(parent.id, "user", "root", writer_principal="route_user_message")
    parent_blob = await blob_service.create_blob(parent.id, "orders.csv", b"id,name\n1,Ada\n", "text/csv")
    stable_id = str(uuid4())
    # Reviewed snapshot RETAINS blob_ref; the committed source does not.
    snapshot_options = {"path": parent_blob.storage_path, "blob_ref": str(parent_blob.id), "schema": {"mode": "observed"}}
    live_options = {"path": parent_blob.storage_path, "schema": {"mode": "observed"}}
    guided = GuidedSession(
        step=GuidedStep.STEP_2_SINK,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_2_SINK,
                turn_type=TurnType.INSPECT_AND_CONFIRM,
                payload_hash="a" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
        source_order=(stable_id,),
        reviewed_sources={
            stable_id: SourceResolved(
                name="orders",
                plugin="csv",
                options=snapshot_options,
                observed_columns=("id", "name"),
                sample_rows=({"id": 1, "name": "Ada"},),
                on_validation_failure="discard",
            )
        },
        root_intent_message_id=str(root.id),
    )
    state_data = CompositionStateData(
        sources={"orders": {"plugin": "csv", "on_success": "out", "options": dict(live_options), "on_validation_failure": "discard"}},
        nodes=[],
        edges=[],
        outputs=[],
        metadata_={"name": "Guided", "description": ""},
        is_valid=True,
        composer_meta={
            "guided_session": guided.to_dict(),
            "implicit_decisions": _stale_report([_entry("source.path", parent_blob.storage_path)]),
        },
    )
    state = await service.save_composition_state(parent.id, state_data, provenance="session_seed")
    await _complete_guided_start_authority(
        service,
        session_id=parent.id,
        root_message=root,
        state=state,
        state_data=state_data,
    )
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    client = SyncASGITestClient(app)

    response = client.post(
        f"/api/sessions/{parent.id}/fork",
        json={"operation_id": str(uuid4()), "from_message_id": str(fork_message.id), "new_message_content": "edited"},
    )
    assert response.status_code == 201, response.text
    child_id = UUID(response.json()["session_id"])
    child_blobs = await blob_service.list_blobs(child_id, limit=None)
    assert len(child_blobs) == 1
    child_raw_path = child_blobs[0].storage_path
    assert child_raw_path != parent_blob.storage_path

    for path in (f"/api/sessions/{child_id}/state", f"/api/sessions/{child_id}/state/versions"):
        projected = client.get(path)
        assert projected.status_code == 200, (path, projected.text)
        assert parent_blob.storage_path not in projected.text, path
        assert child_raw_path not in projected.text, path
        assert str(parent_blob.id) not in projected.text, path
    body = client.get(f"/api/sessions/{child_id}/state").json()
    # The report SURVIVED re-derivation (not suppressed) and names the child.
    report_values = [entry["value"] for entry in body["composer_meta"]["implicit_decisions"]["entries"]]
    assert report_values, "implicit_decisions must be re-derived, not dropped"
    assert str(child_blobs[0].id) in " ".join(str(value) for value in report_values)


# --- T4: unrewritable parent custody is refused BEFORE any child row exists -----


@pytest.mark.parametrize(
    "carrier",
    ["unknown_meta_value", "top_level_meta_key", "validation_errors", "validation_errors_embedded", "metadata", "metadata_embedded"],
)
@pytest.mark.asyncio
async def test_unrewritable_parent_custody_refuses_the_fork_before_any_child_row_exists(tmp_path: Path, carrier: str) -> None:
    """Round-two systems finding B. The route commits the staged child inside
    ``fork_session`` BEFORE the rewriter runs, so the rewrite-boundary backstop
    could only make the "unknown key retains parent custody" failure observable
    -- the orphaned, archived child it was invented to prevent was created
    regardless (ee1ae108b's "no orphan child" claim was false). Custody that no
    rewriter can rebase -- a ``composer_meta`` key outside the two the rewriter
    models, whether the reference sits in its VALUE or is the top-level KEY
    itself (round-two red-team F1), free text in ``validation_errors``, or the
    ``metadata`` column (name and description, no rewriter either) -- is now
    detected inside the staging transaction before ``sessions`` is written.
    The non-ready blob keeps this outside the fork plan, so only the all-rows
    forbidden set can see it. Driven through the real route: 500, the key named
    in the last-resort record, settlement never entered, and NO child session
    row for this parent.
    """
    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    ready_blob = await blob_service.create_blob(parent.id, "orders.csv", b"id\n1\n", "text/csv")
    pending_blob = await blob_service.create_blob(parent.id, "late.csv", b"id\n2\n", "text/csv")
    with service._engine.begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == str(pending_blob.id)).values(status="pending"))
    assert (await blob_service.get_blob(pending_blob.id)).status == "pending"
    composer_meta = {
        "unknown_meta_value": {"some_future_subsystem": {"remembered_blob": str(pending_blob.id)}},
        "top_level_meta_key": {str(pending_blob.id): {"note": "x"}},
    }.get(carrier)
    # The free-text carriers are matched by SUBSTRING (round-two red-team sign-off
    # S1): a real validator message or description embeds the path in a sentence,
    # a shape whole-string equality can never see.
    validation_errors = {
        "validation_errors": [pending_blob.storage_path],
        "validation_errors_embedded": [f"source file not found: {pending_blob.storage_path}"],
    }.get(carrier)
    description = {
        "metadata": pending_blob.storage_path,
        "metadata_embedded": f"built from {pending_blob.storage_path} on Tuesday",
    }.get(carrier)
    expected_name = {
        "unknown_meta_value": "'some_future_subsystem'",
        "top_level_meta_key": f"'{pending_blob.id}'",
        "validation_errors": "validation_errors",
        "validation_errors_embedded": "validation_errors",
        "metadata": "metadata",
        "metadata_embedded": "metadata",
    }[carrier]
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(ready_blob.id), "path": ready_blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "Parent pipeline", "description": description},
            is_valid=validation_errors is None,
            validation_errors=validation_errors,
            composer_meta=composer_meta,
        ),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    settle_calls: list[GuidedForkSettlementCommand] = []
    original_settle = service.settle_guided_fork_operation

    async def _spy_settle(command: GuidedForkSettlementCommand):
        settle_calls.append(command)
        return await original_settle(command)

    client = SyncASGITestClient(app, raise_server_exceptions=False)
    with patch.object(service, "settle_guided_fork_operation", new=_spy_settle), capture_logs() as cap_logs:
        response = client.post(
            f"/api/sessions/{parent.id}/fork",
            json={"operation_id": str(uuid4()), "from_message_id": str(message.id), "new_message_content": "edited"},
        )

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    assert settle_calls == [], "settlement must not be entered when custody detection refuses the fork"
    records = [entry for entry in cap_logs if entry.get("event") == "session.fork_rewrite_integrity_error"]
    assert len(records) == 1, [entry.get("event") for entry in cap_logs]
    message_text = records[0]["message"]
    assert expected_name in message_text
    assert _PRE_STAGING_TEXT in message_text
    assert _SETTLEMENT_TEXT not in message_text
    assert records[0]["child_session_id"] is None
    with service._engine.begin() as conn:
        child_rows = conn.execute(select(sessions_table.c.id).where(sessions_table.c.forked_from_session_id == str(parent.id))).all()
    assert child_rows == [], "the refused fork must not leave an archived child session behind"


# --- T5: validation_errors is inside the settlement verifier's payload ----------


@pytest.mark.parametrize("entry_shape", ["exact", "embedded"])
@pytest.mark.asyncio
async def test_settlement_rejects_a_rewritten_state_whose_validation_errors_retain_a_parent_path(
    engine, tmp_path: Path, entry_shape: str
) -> None:
    """Round-two systems finding C. ``validation_errors`` is persisted, copied
    verbatim into the child and served on GET /state, yet the settlement payload
    was built from six columns that did not include it -- the only served state
    column outside every custody walker. The rewriter now refuses this shape
    itself, so the payload is handed straight to settlement: the verifier must
    reject it through its own plan and cohort checks.
    """
    service = _service_for(engine)
    blob_service = BlobServiceImpl(engine, tmp_path / "blobs")
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "cases.csv", b"a,b\n1,2\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "data": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "P", "description": None},
        ),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id, "user", "fork here", composition_state_id=state.id, writer_principal="route_user_message"
    )
    fence, staged, _blob_map, rewritten = await _stage_copy_rewrite(
        service, blob_service, parent_id=parent.id, fork_message_id=message.id, data_dir=tmp_path
    )
    assert rewritten is not None
    tainted = CompositionStateData(
        sources=rewritten.sources,
        nodes=rewritten.nodes,
        edges=rewritten.edges,
        outputs=rewritten.outputs,
        metadata_=rewritten.metadata_,
        is_valid=False,
        validation_errors=[blob.storage_path if entry_shape == "exact" else f"source file not found: {blob.storage_path}"],
        composer_meta=rewritten.composer_meta,
    )

    with pytest.raises(AuditIntegrityError, match=_SETTLEMENT_TEXT):
        await service.settle_guided_fork_operation(_settlement_command(fence, staged, tainted))


@pytest.mark.parametrize("entry_shape", ["exact", "embedded"])
@pytest.mark.asyncio
async def test_settlement_rejects_a_staged_state_whose_validation_errors_retain_a_parent_path(
    engine, tmp_path: Path, entry_shape: str
) -> None:
    """The second settlement arm: no rewritten state, so the verifier reads the
    staged row itself. The parent binds no blob (empty plan) but owns a pending
    one, the child stages clean, and the staged row is then corrupted in place
    to carry that blob's raw path in ``validation_errors`` -- settlement must
    still refuse, because the verifier is the whole-payload authority.
    """
    service = _service_for(engine)
    blob_service = BlobServiceImpl(engine, tmp_path / "blobs")
    parent = await service.create_session("alice", "Parent", "local")
    pending_blob = await blob_service.create_blob(parent.id, "late.csv", b"a\n1\n", "text/csv")
    with engine.begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == str(pending_blob.id)).values(status="pending"))
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(sources={}, metadata_={"name": "P", "description": None}),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id, "user", "fork here", composition_state_id=state.id, writer_principal="route_user_message"
    )
    fence = await _claim_fork(service, parent.id, operation_id=str(uuid4()))
    staged = await service.fork_session(fence, fork_message_id=message.id, new_message_content="edited")
    assert staged.state is not None and staged.blob_plan == ()
    with engine.begin() as conn:
        conn.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(staged.state.id))
            .values(
                is_valid=False,
                validation_errors=[
                    pending_blob.storage_path if entry_shape == "exact" else f"source file not found: {pending_blob.storage_path}"
                ],
            )
        )

    with pytest.raises(AuditIntegrityError, match=_SETTLEMENT_TEXT):
        await service.settle_guided_fork_operation(_settlement_command(fence, staged, None))


# --- T6: the fork plan row never reaches GET /messages ------------------------


@pytest.mark.asyncio
async def test_forked_child_messages_never_serve_the_parent_blob_plan_row(tmp_path: Path) -> None:
    """Round-two systems finding D. The settlement verifier reads the frozen fork
    plan from a ``role="audit"`` row in the CHILD's transcript, and that row
    names the parent's blob ids by design (``_fork_blob_plan_content``). So "no
    parent identity in the child" is true of ``composition_states`` only, and
    the transcript projection's unconditional audit-row exclusion is the sole
    thing between that row and the wire. Nothing pinned it. Every opt-in view
    of GET /messages on a forked child must omit it.
    """
    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "orders.csv", b"id\n1\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "Parent pipeline", "description": None},
        ),
        provenance="session_seed",
    )
    fork_message = await service.add_message(
        parent.id, "user", "fork", composition_state_id=state.id, writer_principal="route_user_message"
    )
    client = SyncASGITestClient(app)
    response = client.post(
        f"/api/sessions/{parent.id}/fork",
        json={"operation_id": str(uuid4()), "from_message_id": str(fork_message.id), "new_message_content": "edited"},
    )
    assert response.status_code == 201, response.text
    child_id = UUID(response.json()["session_id"])
    stored = await service.get_messages(child_id, limit=None)
    assert any(item.role == "audit" and str(blob.id) in item.content for item in stored), "fixture: the plan row must exist in the child"

    for query in (
        "",
        "?include_tool_rows=true",
        "?include_llm_audit=true",
        "?include_raw_content=true",
        "?include_tool_rows=true&include_llm_audit=true",
    ):
        served = client.get(f"/api/sessions/{child_id}/messages{query}")
        assert served.status_code == 200, (query, served.text)
        assert str(blob.id) not in served.text, query
        assert blob.storage_path not in served.text, query
        assert all(item["role"] != "audit" for item in served.json()), query
