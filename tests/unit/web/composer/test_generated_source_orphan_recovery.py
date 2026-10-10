"""Recover historical generated-source orphans through real blob and review seams."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, OutputSpec
from elspeth.web.composer.tools import _execute_create_blob, _execute_set_source_from_blob, _handle_request_interpretation_review
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    InterpretationReviewPending,
    interpretation_sites,
    materialize_state_for_execution,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer.test_promote_set_source_from_blob import ToolContext, _empty_state, _mock_catalog
from tests.unit.web.composer.test_request_interpretation_review_tool import (
    _insert_test_session_with_released_create_fence,
    _now,
    _provenance_kwargs,
    _save_composition_state_with_compose_authority,
)
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


@pytest.fixture
def recovery_service() -> SessionServiceImpl:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


async def _persist(service: SessionServiceImpl, session_id: UUID, state: CompositionState):
    wire = state.to_dict()
    return await _save_composition_state_with_compose_authority(
        service,
        session_id,
        CompositionStateData(
            sources=wire["sources"],
            nodes=wire["nodes"],
            edges=wire["edges"],
            outputs=wire["outputs"],
            metadata_=wire["metadata"],
            is_valid=False,
        ),
    )


def _assert_pending_sources(state: CompositionState, expected_components: frozenset[str]) -> None:
    blocked = materialize_state_for_execution(state)
    assert isinstance(blocked, InterpretationReviewPending)
    assert {site.component_id for site in blocked.sites} == expected_components
    assert all(site.kind is InterpretationKind.INVENTED_SOURCE for site in blocked.sites)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_names", [("source",), ("orders", "refunds")], ids=("one-orphan", "two-orphans"))
async def test_historical_orphan_rebind_requests_new_term_and_resolves_strictly(
    recovery_service: SessionServiceImpl,
    tmp_path: Path,
    source_names: tuple[str, ...],
) -> None:
    service = recovery_service
    session_id = uuid4()
    _insert_test_session_with_released_create_fence(service, session_id, title="Generated source orphan recovery")
    with fenced_operation_context(service._engine, str(session_id)) as operation:
        user_message = await service.add_message(
            session_id,
            role="user",
            content="Please create URL-list source artifacts from my project briefs.",
            writer_principal="route_user_message",
            session_operation_context=operation,
        )
    assert user_message.session_id == session_id
    assert user_message.sequence_no == 1
    user_message_id = str(user_message.id)
    catalog = _mock_catalog()
    context = ToolContext(
        catalog=catalog,
        data_dir=str(tmp_path),
        session_engine=service._engine,
        session_id=str(session_id),
        user_message_id=user_message_id,
        user_message_content="Please create URL-list source artifacts from my project briefs.",
        composer_model_identifier="openai/gpt-5-mini",
        composer_model_version="gpt-5-mini-2026-05-01",
        composer_provider="openai",
        composer_skill_hash="a" * 64,
        tool_arguments_hash="b" * 64,
    )
    state = _empty_state()
    blob_ids: dict[str, str] = {}
    with fenced_operation_context(service._engine, str(session_id)) as operation:
        bound_context = replace(
            context,
            session_operation_context=operation,
            session_operation_authority=SQLiteLocalSessionOperationAuthority(service._engine),
        )
        for source_name in source_names:
            content = f"url\nhttps://example.gov.au/{source_name}/café\n"
            created = _execute_create_blob(
                {"filename": f"{source_name}.csv", "mime_type": "text/csv", "content": content},
                state,
                bound_context,
            )
            assert created.success, created.validation.errors
            blob_ids[source_name] = created.data["blob_id"]
            bound = _execute_set_source_from_blob(
                {
                    "blob_id": blob_ids[source_name],
                    "source_name": source_name,
                    "on_success": f"{source_name}_rows",
                    "options": {"schema": {"mode": "observed"}},
                },
                state,
                bound_context,
            )
            assert bound.success, bound.validation.errors
            state = bound.updated_state

    outputs = tuple(
        OutputSpec(
            name=f"{source_name}_rows",
            plugin="json",
            options={
                "path": str(tmp_path / f"{source_name}.jsonl"),
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            on_write_failure="discard",
        )
        for source_name in source_names
    )
    historical_sources = {
        name: replace(source, options={key: value for key, value in source.options.items() if key != INTERPRETATION_REQUIREMENTS_KEY})
        for name, source in state.sources.items()
    }
    state = replace(state, sources=historical_sources, outputs=outputs)
    record = await _persist(service, session_id, state)
    state = state_from_record(record)
    expected_components = frozenset("source" if name == "source" else f"source:{name}" for name in source_names)
    assert {site.user_term for site in interpretation_sites(state)} == {"llm_generated_source"}
    _assert_pending_sources(state, expected_components)

    for source_name in source_names:
        with fenced_operation_context(service._engine, str(session_id)) as operation:
            bound_context = replace(
                context,
                session_operation_context=operation,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(service._engine),
            )
            rebound = _execute_set_source_from_blob(
                {
                    "blob_id": blob_ids[source_name],
                    "source_name": source_name,
                    "on_success": f"{source_name}_rows",
                    "options": {"schema": {"mode": "observed"}},
                },
                state,
                bound_context,
            )
        assert rebound.success, rebound.validation.errors
        record = await _persist(service, session_id, rebound.updated_state)
        state = state_from_record(record)
        current = next(
            site
            for site in interpretation_sites(state)
            if site.component_id == ("source" if source_name == "source" else f"source:{source_name}")
        )
        assert current.user_term == "inline_source_url_list"
        assert state.sources[source_name].options[INTERPRETATION_REQUIREMENTS_KEY][0]["draft"].startswith("url\n")
        # Rebinding one orphan must never approve it or the untouched peer.
        _assert_pending_sources(state, expected_components)

    first_site = interpretation_sites(state)[0]
    with pytest.raises(ToolArgumentError, match="pending invented_source"):
        await _handle_request_interpretation_review(
            {"affected_node_id": first_site.component_id, "kind": "invented_source", "user_term": "llm_generated_source"},
            state,
            session_id=session_id,
            composition_state_id=record.id,
            tool_call_id="stale-fallback-term",
            now=_now(),
            per_term_cap=3,
            per_session_day_cap=10,
            create_pending_interpretation_event=service.create_pending_interpretation_event,
            list_interpretation_events=service.list_interpretation_events,
            **_provenance_kwargs(),
        )

    for review_index, source_name in enumerate(source_names):
        component_id = "source" if source_name == "source" else f"source:{source_name}"
        site = next(site for site in interpretation_sites(state) if site.component_id == component_id)
        assert site.kind is InterpretationKind.INVENTED_SOURCE
        requested = await _handle_request_interpretation_review(
            {"affected_node_id": site.component_id, "kind": "invented_source", "user_term": site.user_term},
            state,
            session_id=session_id,
            composition_state_id=record.id,
            tool_call_id=f"review-{source_name}",
            now=_now(),
            per_term_cap=3,
            per_session_day_cap=10,
            create_pending_interpretation_event=service.create_pending_interpretation_event,
            list_interpretation_events=service.list_interpretation_events,
            **_provenance_kwargs(),
        )
        assert requested.success
        pending = await service.list_interpretation_events(session_id, status="pending")
        matching = [event for event in pending if event.affected_node_id == site.component_id and event.user_term == site.user_term]
        assert len(matching) == 1
        _, record = await service.resolve_interpretation_event(
            session_id=session_id,
            event_id=matching[0].id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:alice",
        )
        state = state_from_record(record)
        remaining = expected_components - frozenset(
            "source" if name == "source" else f"source:{name}" for name in source_names[: review_index + 1]
        )
        if remaining:
            # Accepting the first of two reviews leaves the peer fail-closed.
            _assert_pending_sources(state, remaining)

    assert interpretation_sites(state) == ()
    assert state.validate().is_valid
    assert materialize_state_for_execution(state) == state
