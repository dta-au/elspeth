"""Milestones agree with diff baselines without changing receipt responses."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import create_model

from elspeth.web.sessions.operation_receipts import operation_receipt_response_hash
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.routes._helpers import _state_response
from elspeth.web.sessions.schemas import CompositionStateResponse
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _make_app, _save_test_composition_state


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize(
    "meta", [None, {}, {"validation_lane": "strict"}, {"validation_lane": "authoring_only"}, {"validation_lane": "unknown"}]
)
async def test_milestone_agrees_with_baseline_and_versions(tmp_path: Path, valid, meta) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Milestones", "local")
    row = await _save_test_composition_state(
        service, session.id, CompositionStateData(is_valid=valid, composer_meta=meta), provenance="session_seed"
    )
    expected = valid and (meta is None or "validation_lane" not in meta or meta["validation_lane"] == "strict")
    baseline = await service.get_last_validated_state(session.id, through_version=1, exclude_version=2)
    assert (baseline is not None) is expected
    response = SyncASGITestClient(app).get(f"/api/sessions/{session.id}/state/versions")
    assert response.status_code == 200
    wire = response.json()[0]
    assert "is_milestone" in wire
    assert wire["is_milestone"] is expected
    assert importlib.util.find_spec("elspeth.web.sessions.validation_milestone") is not None
    from elspeth.web.sessions.validation_milestone import is_validation_milestone

    assert is_validation_milestone(row) is expected


@pytest.mark.asyncio
async def test_baseline_pages_past_non_milestones(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "History", "local")
    first = await _save_test_composition_state(service, session.id, CompositionStateData(is_valid=True), provenance="session_seed")
    for _ in range(101):
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True, composer_meta={"validation_lane": "authoring_only"}),
            provenance="session_seed",
        )
    baseline = await service.get_last_validated_state(session.id, through_version=102, exclude_version=103)
    assert baseline is not None and baseline.id == first.id
    response = SyncASGITestClient(app).get(f"/api/sessions/{session.id}/state/versions?limit=200")
    assert response.status_code == 200
    assert "is_milestone" in response.json()[0]
    assert [row["version"] for row in response.json() if row["is_milestone"]] == [baseline.version]


@pytest.mark.asyncio
async def test_newest_window_preserves_ascending_api_contract(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Long history", "local")
    for version in range(1, 56):
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True, composer_meta={"validation_lane": "strict" if version == 54 else "authoring_only"}),
            provenance="session_seed",
        )
    client = SyncASGITestClient(app)
    default = client.get(f"/api/sessions/{session.id}/state/versions")
    assert default.status_code == 200
    assert [row["version"] for row in default.json()] == list(range(1, 51))
    head = client.get(f"/api/sessions/{session.id}/state/head")
    assert head.status_code == 200
    offset = max(0, head.json()["version"] - 50)
    newest = client.get(f"/api/sessions/{session.id}/state/versions?limit=50&offset={offset}")
    assert newest.status_code == 200
    assert [row["version"] for row in newest.json()] == list(range(6, 56))
    assert "is_milestone" in newest.json()[-2]
    assert newest.json()[-2]["is_milestone"] is True


def fixed_receipt_record() -> CompositionStateRecord:
    return CompositionStateRecord(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        session_id=UUID("00000000-0000-4000-8000-000000000002"),
        version=3,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_=None,
        is_valid=True,
        validation_errors=None,
        created_at=datetime(2026, 11, 14, tzinfo=UTC),
        derived_from_state_id=None,
        composer_meta={"validation_lane": "strict"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [None, {"validation_lane": "strict"}, {"validation_lane": "authoring_only"}])
async def test_revert_carries_saved_verdict_without_revalidation(tmp_path: Path, meta) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Revert carry", "local")
    # An empty graph would fail fresh validation; revert copies its stored verdict.
    prior = await _save_test_composition_state(
        service, session.id, CompositionStateData(is_valid=True, composer_meta=meta), provenance="session_seed"
    )
    await _save_test_composition_state(service, session.id, CompositionStateData(is_valid=False), provenance="session_seed")
    response = SyncASGITestClient(app).post(
        f"/api/sessions/{session.id}/state/revert",
        json={"operation_id": "00000000-0000-4000-8000-000000000003", "state_id": str(prior.id)},
    )
    assert response.status_code == 200, response.text
    copied = await service.get_current_state(session.id)
    assert copied is not None
    assert copied.is_valid is True
    assert copied.composer_meta == prior.composer_meta
    assert (copied.sources, copied.nodes, copied.edges, copied.outputs, copied.metadata_) == (
        prior.sources,
        prior.nodes,
        prior.edges,
        prior.outputs,
        prior.metadata_,
    )


def test_revert_receipt_hash_stays_in_original_response_domain() -> None:
    response = _state_response(fixed_receipt_record())
    digest = operation_receipt_response_hash(response)
    assert digest == "c57873f3d21902ede6f7076a0c84ef63fe76ea9835203d2927fc21251bd0429b"
    widened = create_model("WidenedReceiptControl", __base__=CompositionStateResponse, is_milestone=(bool, True))
    assert operation_receipt_response_hash(widened(**response.model_dump())) != digest
    assert "is_milestone" not in response.model_dump()
