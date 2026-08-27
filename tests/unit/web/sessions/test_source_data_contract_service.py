"""Service round trip for the source_data_contract acknowledgement flow.

Pins the writer boundary (server-computed draft, planner field lists
structurally impossible) and the resolve arm (stamps EXACTLY the backtraced
demand set into ``schema.guaranteed_fields``, upserts the resolved
requirement with the field-set artifact hash) — elspeth-da68332faf work
item 2.

Fixture pattern mirrors ``test_interpretation_events_service.py``: in-memory
SQLite engine + ``SessionServiceImpl``; states are seeded with a real
uploaded-style csv source (path on disk, no ``source_authoring``) and an llm
consumer whose ``required_input_fields`` is the demand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationKind,
)
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    build_source_data_contract_draft,
    source_data_contract_artifact_hash,
)
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateRecord,
    InterpretationResolveError,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def engine():
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def service(engine) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


def _uploaded_state_dict(csv_path: str, *, required: list[str]) -> dict[str, Any]:
    state = CompositionState(
        source=None,
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="source",
                options={"path": csv_path},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="rate",
                node_type="transform",
                plugin="llm",
                input="source",
                on_success="rated",
                on_error="discard",
                options={
                    "prompt_template": "Rate {{ row.colour }}",
                    "model": "gpt-test",
                    "schema": {"mode": "observed"},
                    "required_input_fields": required,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Data contract test", description=""),
        version=1,
    )
    return state.to_dict()


async def _seed_state(
    service: SessionServiceImpl,
    *,
    session_id: UUID,
    csv_path: str,
    required: list[str],
) -> CompositionStateRecord:
    with service._engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id="alice",
                auth_provider_type="local",
                title="Data contract test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    state_dict = _uploaded_state_dict(csv_path, required=required)
    return await service.save_composition_state(
        session_id,
        CompositionStateData(
            sources=state_dict["sources"],
            nodes=state_dict["nodes"],
            metadata_={"name": "Data contract test", "description": ""},
            is_valid=False,
        ),
        provenance="tool_call",
    )


def _server_draft(csv_header: tuple[str, ...] | None, demand: list[str]) -> str:
    return build_source_data_contract_draft(demand, csv_header)


async def _create_contract_event(
    service: SessionServiceImpl,
    *,
    session_id: UUID,
    state: CompositionStateRecord,
    llm_draft: str,
):
    return await service.create_pending_interpretation_event(
        session_id=session_id,
        composition_state_id=state.id,
        affected_node_id="source",
        tool_call_id="call_data_contract",
        user_term=SOURCE_DATA_CONTRACT_USER_TERM,
        kind=InterpretationKind.SOURCE_DATA_CONTRACT,
        llm_draft=llm_draft,
        model_identifier="anthropic/test-model",
        model_version="1",
        provider="anthropic",
        composer_skill_hash="0" * 64,
    )


@pytest.mark.asyncio
async def test_round_trip_stamps_exactly_the_demand_set(service, tmp_path: Path) -> None:
    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour,extra\nred,1\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=["colour"])

    draft = _server_draft(("colour", "extra"), ["colour"])
    event = await _create_contract_event(service, session_id=sid, state=state, llm_draft=draft)
    assert event.choice is InterpretationChoice.PENDING
    assert event.kind is InterpretationKind.SOURCE_DATA_CONTRACT

    resolved_event, new_state = await service.resolve_interpretation_event(
        session_id=sid,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="alice",
    )
    assert resolved_event.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED

    patched = state_from_record(new_state)
    source_options = patched.sources["source"].options
    # Stamp is EXACTLY the demand set — never the full sample header.
    assert list(source_options["schema"]["guaranteed_fields"]) == ["colour"]
    assert source_options["schema"]["mode"] == "observed"
    rows = source_options[INTERPRETATION_REQUIREMENTS_KEY]
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["kind"] == InterpretationKind.SOURCE_DATA_CONTRACT.value
    assert row["status"] == "resolved"
    assert row["event_id"] == str(event.id)
    assert row["accepted_artifact_hash"] == source_data_contract_artifact_hash(["colour"])
    # accepted_value round-trips the acknowledged field set for the re-open check.
    from elspeth.web.composer.source_demand import parse_source_data_contract_accepted_fields

    assert parse_source_data_contract_accepted_fields(row["accepted_value"]) == ("colour",)


@pytest.mark.asyncio
async def test_writer_boundary_rejects_planner_supplied_field_list(service, tmp_path: Path) -> None:
    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour,extra\nred,1\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=["colour"])

    forged = _server_draft(("colour", "extra"), ["colour", "extra"])  # planner tries the full header
    with pytest.raises(InterpretationResolveError):
        await _create_contract_event(service, session_id=sid, state=state, llm_draft=forged)


@pytest.mark.asyncio
async def test_writer_boundary_rejects_composer_authored_source(service, tmp_path: Path) -> None:
    csv_path = tmp_path / "generated.csv"
    csv_path.write_text("colour\nred\n", encoding="utf-8")
    sid = uuid4()
    state_dict = _uploaded_state_dict(str(csv_path), required=["colour"])
    source_options = dict(state_dict["sources"]["source"]["options"])
    source_options["source_authoring"] = {
        "modality": "llm_generated",
        "content_hash": "0" * 64,
        "review_event_id": None,
        "resolved_kind": None,
    }
    state_dict["sources"]["source"]["options"] = source_options
    with service._engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(sid),
                user_id="alice",
                auth_provider_type="local",
                title="Data contract test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    state = await service.save_composition_state(
        sid,
        CompositionStateData(
            sources=state_dict["sources"],
            nodes=state_dict["nodes"],
            metadata_={"name": "Data contract test", "description": ""},
            is_valid=False,
        ),
        provenance="tool_call",
    )
    draft = _server_draft(("colour",), ["colour"])
    with pytest.raises(InterpretationResolveError):
        await _create_contract_event(service, session_id=sid, state=state, llm_draft=draft)


@pytest.mark.asyncio
async def test_writer_boundary_rejects_no_demand(service, tmp_path: Path) -> None:
    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour\nred\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=[])
    draft = _server_draft(("colour",), [])
    with pytest.raises(InterpretationResolveError):
        await _create_contract_event(service, session_id=sid, state=state, llm_draft=draft)


@pytest.mark.asyncio
async def test_demand_drift_between_surface_and_resolve_is_refused(service, tmp_path: Path) -> None:
    """A card acknowledges the demand THE USER SAW. If the graph's demand
    changes between surfacing and resolve, the live identity no longer
    matches the surfacing identity and the resolve is refused — the card
    must be re-requested against the new demand."""
    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour,size\nred,10\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=["colour"])
    draft = _server_draft(("colour", "size"), ["colour"])
    event = await _create_contract_event(service, session_id=sid, state=state, llm_draft=draft)

    # The demand grows after the card was surfaced.
    new_state_dict = _uploaded_state_dict(str(csv_path), required=["colour", "size"])
    await service.save_composition_state(
        sid,
        CompositionStateData(
            sources=new_state_dict["sources"],
            nodes=new_state_dict["nodes"],
            metadata_={"name": "Data contract test", "description": ""},
            is_valid=False,
        ),
        provenance="tool_call",
    )

    with pytest.raises(InterpretationResolveError):
        await service.resolve_interpretation_event(
            session_id=sid,
            event_id=event.id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="alice",
        )


@pytest.mark.asyncio
async def test_settlement_surfacer_mints_the_card_for_a_blocked_uploaded_source(service, tmp_path: Path) -> None:
    """The kind-general settlement surfacer — the SAME pass the freeform
    settlement and the guided wire-confirm settlement run
    (surface_pending_interpretation_reviews_for_state) — mints the
    data-contract event with the server-computed draft, so a guided session
    reaching the blocked shape gets its card without a planner tool call."""
    from elspeth.web.composer.service import surface_pending_interpretation_reviews_for_state
    from elspeth.web.interpretation_state import BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX

    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour,extra\nred,1\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=["colour"])

    await surface_pending_interpretation_reviews_for_state(
        state_from_record(state),
        sessions_service=service,
        session_id=str(sid),
        current_state_id=str(state.id),
        model_identifier="anthropic/test-model",
        model_version="1",
        provider="anthropic",
        composer_skill_hash="0" * 64,
    )

    events = await service.list_interpretation_events(sid, status="all")
    contract_events = [event for event in events if event.kind is InterpretationKind.SOURCE_DATA_CONTRACT]
    assert len(contract_events) == 1
    event = contract_events[0]
    assert event.choice is InterpretationChoice.PENDING
    assert event.affected_node_id == "source"
    assert (event.tool_call_id or "").startswith(BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX)
    assert event.llm_draft == _server_draft(("colour", "extra"), ["colour"])


@pytest.mark.asyncio
async def test_settlement_surfacer_skips_card_ineligible_sources(service, tmp_path: Path) -> None:
    """Composer-authored bound content never gets a data-contract card from
    the surfacer (the invented_source flow owns it), and neither does a
    source the pipeline demands nothing from."""
    from elspeth.web.composer.service import _backend_surface_args_for_site
    from elspeth.web.interpretation_state import InterpretationReviewSite

    csv_path = tmp_path / "generated.csv"
    csv_path.write_text("colour\nred\n", encoding="utf-8")
    state_dict = _uploaded_state_dict(str(csv_path), required=["colour"])
    source_options = dict(state_dict["sources"]["source"]["options"])
    source_options["source_authoring"] = {
        "modality": "llm_generated",
        "content_hash": "0" * 64,
        "review_event_id": None,
        "resolved_kind": None,
    }
    state_dict["sources"]["source"]["options"] = source_options
    from elspeth.web.composer.state import CompositionState

    authored_state = CompositionState.from_dict(state_dict)
    site = InterpretationReviewSite(
        component_id="source",
        component_type="source",
        user_term=SOURCE_DATA_CONTRACT_USER_TERM,
        kind=InterpretationKind.SOURCE_DATA_CONTRACT,
    )
    assert _backend_surface_args_for_site(authored_state, site) is None

    no_demand_state = CompositionState.from_dict(_uploaded_state_dict(str(csv_path), required=[]))
    assert _backend_surface_args_for_site(no_demand_state, site) is None


@pytest.mark.asyncio
async def test_amended_resolution_is_refused(service, tmp_path: Path) -> None:
    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("colour\nred\n", encoding="utf-8")
    sid = uuid4()
    state = await _seed_state(service, session_id=sid, csv_path=str(csv_path), required=["colour"])
    draft = _server_draft(("colour",), ["colour"])
    event = await _create_contract_event(service, session_id=sid, state=state, llm_draft=draft)
    with pytest.raises(InterpretationResolveError):
        await service.resolve_interpretation_event(
            session_id=sid,
            event_id=event.id,
            choice=InterpretationChoice.AMENDED,
            amended_value="colour, extra",
            actor="alice",
        )
