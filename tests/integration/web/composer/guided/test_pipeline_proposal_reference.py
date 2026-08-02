"""Atomic durable-reference contracts for guided pipeline proposals."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.guided.emitters import build_step_4_wire_turn
from elspeth.web.composer.guided.planning import (
    build_guided_proposal_projection,
    guided_candidate_state,
    guided_private_reviewed_facts,
)
from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.stage_subjects import OptionValueConstraint, StableSubject
from elspeth.web.composer.guided.state_machine import (
    DeferredStageIntent,
    GuidedProposalRef,
    GuidedSession,
    TurnRecord,
)
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, PresentBase, composition_content_hash
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import validate_authored_composition_state
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.guided_payloads import prepare_guided_json_payload
from elspeth.web.sessions.models import (
    composition_proposals_table,
    composition_states_table,
    guided_operations_table,
    proposal_events_table,
)
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    GuidedOperationClaimed,
    GuidedOperationSettlementConflictError,
    GuidedPipelineProposalStageCommand,
    GuidedReplayTurn,
    GuidedResponseDescriptor,
)
from elspeth.web.sessions.routes._helpers import _initial_composition_state_with_guided_session, _state_from_record
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

SOURCE_ID = "00000000-0000-4000-8000-000000000201"
OUTPUT_ID = "00000000-0000-4000-8000-000000000202"
DEFERRED_ID = "00000000-0000-4000-8000-000000000203"
MESSAGE_ID = "00000000-0000-4000-8000-000000000204"
CATALOG_IDS = {
    "source": frozenset({"csv"}),
    "transform": frozenset(),
    "sink": frozenset({"json"}),
}


@pytest.fixture
def service() -> SessionServiceImpl:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.guided.proposal"))


def _guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        step=GuidedStep.STEP_3_TRANSFORMS,
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                observed_columns=("name",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="rows",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(SOURCE_ID,),
        output_order=(OUTPUT_ID,),
    )


def _state_data(guided: GuidedSession) -> CompositionStateData:
    state = replace(_initial_composition_state_with_guided_session(), guided_session=guided)
    data = state.to_dict()
    return CompositionStateData(
        sources=data["sources"],
        nodes=data["nodes"],
        edges=data["edges"],
        outputs=data["outputs"],
        metadata_=data["metadata"],
        is_valid=False,
        validation_errors=("guided_composition_invalid",),
        composer_meta={"guided_session": guided.to_dict()},
    )


def _pipeline() -> dict[str, object]:
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {"schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    }


def _deferred_guided(*, covered_value: object = "observed", message_content_hash: str | None = None) -> GuidedSession:
    return replace(
        _guided(),
        deferred_intents=(
            DeferredStageIntent.create(
                intent_id=DEFERRED_ID,
                receiving_stage="output",
                target_stage="topology",
                catalog_kind="source",
                catalog_name="csv",
                redacted_summary="Apply the deferred source option constraint.",
                originating_message_id=MESSAGE_ID,
                message_content_hash=message_content_hash or stable_hash("deferred source instruction"),
                constraints=(
                    OptionValueConstraint(
                        kind="option_value",
                        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
                        option_path=("schema", "mode"),
                        operator="equals",
                        value=covered_value,  # type: ignore[arg-type]
                    ),
                ),
            ),
        ),
    )


async def _command(
    service: SessionServiceImpl,
    payload_store: FilesystemPayloadStore,
    *,
    drift: bool = False,
    initial_guided: GuidedSession | None = None,
    covered_deferred_intent_ids: tuple[str, ...] = (),
) -> tuple[GuidedPipelineProposalStageCommand, UUID]:
    session = await service.create_session("alice", "Guided proposal", "local")
    guided = initial_guided or _guided()
    if guided.deferred_intents:
        deferred_intents = []
        for deferred in guided.deferred_intents:
            message = await service.add_message(
                session.id,
                "user",
                "deferred source instruction",
                writer_principal="route_user_message",
            )
            deferred_intents.append(replace(deferred, originating_message_id=str(message.id)))
        guided = replace(guided, deferred_intents=tuple(deferred_intents))
    predecessor = await service.save_composition_state(session.id, _state_data(guided), provenance="session_seed")
    predecessor_state = _state_from_record(predecessor)
    checkpoint_id = uuid4()
    proposal_id = uuid4()
    plan = PipelinePlanResult(
        proposal=PipelineProposal.create(
            pipeline=_pipeline(),
            base=PresentBase(
                state_id=checkpoint_id,
                composition_content_hash=composition_content_hash(predecessor_state),
            ),
            reviewed_facts=guided_private_reviewed_facts(guided),
            surface=PlannerSurface.GUIDED_STAGED,
            repair_count=0,
            skill_hash=stable_hash("guided skill"),
            covered_deferred_intent_ids=covered_deferred_intent_ids,
            supersedes_draft_hash=None,
        ),
        tool_call_id="guided-planner-terminal",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
    )
    projection = build_guided_proposal_projection(
        proposal_id=proposal_id,
        proposal=plan.proposal,
        guided=guided,
        catalog_plugin_ids=CATALOG_IDS,
    )
    prepared = prepare_guided_json_payload(payload_store, purpose="turn", payload=projection)
    active = GuidedProposalRef(
        proposal_id=proposal_id,
        draft_hash=plan.proposal.draft_hash,
        base=plan.proposal.base,
        reviewed_anchor_hash=plan.proposal.reviewed_anchor_hash,
        covered_deferred_intent_ids=covered_deferred_intent_ids,
        creation_event_schema="pipeline_proposal_created.v1",
    )
    checkpoint_guided = replace(
        guided,
        active_proposal=active,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_3_TRANSFORMS,
                turn_type=TurnType.PROPOSE_PIPELINE,
                payload_hash=prepared.payload_id,
                response_hash=None,
                emitter="server",
            ),
        ),
    )
    operation_id = str(uuid4())
    outcome = await service.reserve_guided_operation(
        session_id=session.id,
        operation_id=operation_id,
        kind="guided_respond",
        request_hash=stable_hash({"operation_id": operation_id}),
        actor="test",
        lease_seconds=300,
    )
    assert isinstance(outcome, GuidedOperationClaimed)
    if drift:
        await service.save_composition_state(session.id, _state_data(guided), provenance="convergence_persist")
    redacted = redact_tool_call_arguments("set_pipeline", _pipeline(), telemetry=NoopRedactionTelemetry())
    return (
        GuidedPipelineProposalStageCommand(
            fence=outcome.fence,
            expected_current_state_id=predecessor.id,
            expected_current_state_version=predecessor.version,
            expected_current_content_hash=composition_content_hash(predecessor_state),
            checkpoint_state_id=checkpoint_id,
            proposal_id=proposal_id,
            state=_state_data(checkpoint_guided),
            plan=plan,
            summary="pipeline_proposal.summary.v1",
            rationale="pipeline_proposal.rationale.v1",
            affects=("pipeline",),
            arguments_redacted_json=redacted,
            catalog_plugin_ids=CATALOG_IDS,
            proposal_projection=projection,
            actor="composer_route",
            user_message_id=None,
            user_message_content_hash=None,
            originating_message=None,
            supersedes_proposal_id=None,
            response=GuidedResponseDescriptor(
                kind="guided_respond",
                next_turn=GuidedReplayTurn(
                    turn_type=TurnType.PROPOSE_PIPELINE,
                    step_index=2,
                    payload_id=prepared.payload_id,
                ),
                assistant_turn_seq=None,
            ),
            payloads=(prepared,),
        ),
        session.id,
    )


async def _successor_command(
    service: SessionServiceImpl,
    payload_store: FilesystemPayloadStore,
    *,
    initial_guided: GuidedSession | None = None,
) -> tuple[GuidedPipelineProposalStageCommand, GuidedPipelineProposalStageCommand, UUID]:
    predecessor_command, session_id = await _command(service, payload_store, initial_guided=initial_guided)
    await service.stage_guided_pipeline_proposal(predecessor_command, payload_store=payload_store)
    current = await service.get_current_state(session_id)
    assert current is not None
    current_state = _state_from_record(current)
    current_guided = current_state.guided_session
    assert current_guided is not None and current_guided.active_proposal is not None

    response = prepare_guided_json_payload(
        payload_store,
        purpose="turn_response",
        payload={
            "action": "revise",
            "proposal_id": str(predecessor_command.proposal_id),
            "draft_hash": predecessor_command.plan.proposal.draft_hash,
            "edit_target": {"kind": "source", "stable_id": SOURCE_ID},
        },
    )
    answered = replace(
        current_guided.history[-1],
        response_hash=response.payload_id,
        summary="Guided pipeline proposal revision requested.",
    )
    checkpoint_id = uuid4()
    proposal_id = uuid4()
    plan = PipelinePlanResult(
        proposal=PipelineProposal.create(
            pipeline=_pipeline(),
            base=PresentBase(
                state_id=checkpoint_id,
                composition_content_hash=composition_content_hash(current_state),
            ),
            reviewed_facts=guided_private_reviewed_facts(current_guided),
            surface=PlannerSurface.GUIDED_STAGED,
            repair_count=0,
            skill_hash=stable_hash("guided skill"),
            covered_deferred_intent_ids=(),
            supersedes_draft_hash=predecessor_command.plan.proposal.draft_hash,
        ),
        tool_call_id="guided-planner-successor-terminal",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
    )
    projection = build_guided_proposal_projection(
        proposal_id=proposal_id,
        proposal=plan.proposal,
        guided=current_guided,
        catalog_plugin_ids={
            "source": frozenset({"csv"}),
            "transform": frozenset(),
            "sink": frozenset({"json"}),
        },
    )
    prepared = prepare_guided_json_payload(payload_store, purpose="turn", payload=projection)
    successor_guided = replace(
        current_guided,
        history=(
            *current_guided.history[:-1],
            answered,
            TurnRecord(
                step=GuidedStep.STEP_3_TRANSFORMS,
                turn_type=TurnType.PROPOSE_PIPELINE,
                payload_hash=prepared.payload_id,
                response_hash=None,
                emitter="server",
            ),
        ),
        active_proposal=GuidedProposalRef(
            proposal_id=proposal_id,
            draft_hash=plan.proposal.draft_hash,
            base=plan.proposal.base,
            reviewed_anchor_hash=plan.proposal.reviewed_anchor_hash,
            covered_deferred_intent_ids=(),
            creation_event_schema="pipeline_proposal_created.v1",
            supersedes_proposal_id=predecessor_command.proposal_id,
            supersedes_draft_hash=predecessor_command.plan.proposal.draft_hash,
        ),
    )
    operation_id = str(uuid4())
    outcome = await service.reserve_guided_operation(
        session_id=session_id,
        operation_id=operation_id,
        kind="guided_respond",
        request_hash=stable_hash({"operation_id": operation_id}),
        actor="test",
        lease_seconds=300,
    )
    assert isinstance(outcome, GuidedOperationClaimed)
    return (
        predecessor_command,
        GuidedPipelineProposalStageCommand(
            fence=outcome.fence,
            expected_current_state_id=current.id,
            expected_current_state_version=current.version,
            expected_current_content_hash=composition_content_hash(current_state),
            checkpoint_state_id=checkpoint_id,
            proposal_id=proposal_id,
            state=_state_data(successor_guided),
            plan=plan,
            summary="pipeline_proposal.summary.v1",
            rationale="pipeline_proposal.rationale.v1",
            affects=("pipeline",),
            arguments_redacted_json=redact_tool_call_arguments(
                "set_pipeline",
                _pipeline(),
                telemetry=NoopRedactionTelemetry(),
            ),
            catalog_plugin_ids=CATALOG_IDS,
            proposal_projection=projection,
            actor="composer_route",
            user_message_id=None,
            user_message_content_hash=None,
            originating_message=None,
            supersedes_proposal_id=predecessor_command.proposal_id,
            response=GuidedResponseDescriptor(
                kind="guided_respond",
                next_turn=GuidedReplayTurn(
                    turn_type=TurnType.PROPOSE_PIPELINE,
                    step_index=2,
                    payload_id=prepared.payload_id,
                ),
                assistant_turn_seq=None,
            ),
            payloads=(response, prepared),
        ),
        session_id,
    )


def test_stage_writes_checkpoint_reference_private_row_event_and_operation_atomically(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    command, session_id = asyncio.run(_command(service, payload_store))

    settlement = asyncio.run(service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    assert settlement.result_state.id == command.checkpoint_state_id
    assert settlement.proposal.id == command.proposal_id
    assert deep_thaw(settlement.proposal.arguments_json) == _pipeline()
    assert settlement.proposal.pipeline_metadata is not None
    assert settlement.proposal.pipeline_metadata.base == {
        "kind": "present",
        "state_id": str(command.checkpoint_state_id),
        "composition_content_hash": command.expected_current_content_hash,
    }
    current = asyncio.run(service.get_current_state(session_id))
    assert current is not None and current.id == command.checkpoint_state_id
    replay = asyncio.run(
        service.get_guided_operation(
            session_id=session_id,
            operation_id=command.fence.operation_id,
            kind="guided_respond",
            request_hash=stable_hash({"operation_id": command.fence.operation_id}),
        )
    )
    assert replay is not None and replay.result.proposal_id == command.proposal_id  # type: ignore[union-attr]
    events = asyncio.run(service.list_proposal_events(session_id))
    assert [event.event_type for event in events] == ["proposal.created"]
    assert all("options" not in repr(event.payload) for event in events)


def test_predecessor_drift_rolls_back_every_stage_row(service: SessionServiceImpl, tmp_path: Path) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    command, session_id = asyncio.run(_command(service, payload_store, drift=True))

    with pytest.raises(GuidedOperationSettlementConflictError):
        asyncio.run(service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    with service._engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_proposals_table)
                .where(composition_proposals_table.c.id == str(command.proposal_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_states_table)
                .where(composition_states_table.c.id == str(command.checkpoint_state_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(proposal_events_table.c.proposal_id == str(command.proposal_id))
            ).scalar_one()
            == 0
        )
    assert (asyncio.run(service.get_current_state(session_id))).id != command.checkpoint_state_id  # type: ignore[union-attr]


def _assert_stage_cohort_absent(
    service: SessionServiceImpl,
    command: GuidedPipelineProposalStageCommand,
    session_id: UUID,
) -> None:
    current = asyncio.run(service.get_current_state(session_id))
    assert current is not None and current.id == command.expected_current_state_id
    with service._engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_proposals_table)
                .where(composition_proposals_table.c.id == str(command.proposal_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_states_table)
                .where(composition_states_table.c.id == str(command.checkpoint_state_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(proposal_events_table.c.proposal_id == str(command.proposal_id))
            ).scalar_one()
            == 0
        )
        operation = conn.execute(
            select(
                guided_operations_table.c.status,
                guided_operations_table.c.result_kind,
                guided_operations_table.c.result_state_id,
                guided_operations_table.c.proposal_id,
                guided_operations_table.c.settled_at,
            )
            .where(guided_operations_table.c.session_id == str(session_id))
            .where(guided_operations_table.c.operation_id == command.fence.operation_id)
        ).one()
    assert operation.status == "in_progress"
    assert operation.result_kind is None
    assert operation.result_state_id is None
    assert operation.proposal_id is None
    assert operation.settled_at is None


def test_stage_command_requires_exact_catalog_snapshot() -> None:
    parameter = inspect.signature(GuidedPipelineProposalStageCommand).parameters.get("catalog_plugin_ids")

    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_stage_rejects_public_projection_that_differs_from_immutable_proposal(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "payload-proposal-mismatch")
    command, session_id = asyncio.run(_command(service, payload_store))
    original_payload = next(
        payload
        for payload in command.payloads
        if payload.payload_id == command.response.next_turn.payload_id  # type: ignore[union-attr]
    )
    altered_projection = deep_thaw(original_payload.payload)
    altered_projection["graph"]["sources"][0]["plugin"]["id"] = "json"
    altered_payload = prepare_guided_json_payload(payload_store, purpose="turn", payload=altered_projection)
    checkpoint_guided = GuidedSession.from_dict(deep_thaw(command.state.composer_meta)["guided_session"])
    checkpoint_guided = replace(
        checkpoint_guided,
        history=(*checkpoint_guided.history[:-1], replace(checkpoint_guided.history[-1], payload_hash=altered_payload.payload_id)),
    )
    assert command.response.next_turn is not None
    tampered = replace(
        command,
        state=_state_data(checkpoint_guided),
        response=replace(command.response, next_turn=replace(command.response.next_turn, payload_id=altered_payload.payload_id)),
        payloads=tuple(altered_payload if payload.payload_id == original_payload.payload_id else payload for payload in command.payloads),
    )

    with pytest.raises(AuditIntegrityError, match="projection"):
        asyncio.run(service.stage_guided_pipeline_proposal(tampered, payload_store=payload_store))

    _assert_stage_cohort_absent(service, tampered, session_id)


def test_stage_recomputes_covered_deferred_constraints_before_sql(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "covered-deferred-mismatch")
    guided = _deferred_guided(covered_value="not-observed")
    command, session_id = asyncio.run(
        _command(
            service,
            payload_store,
            initial_guided=guided,
            covered_deferred_intent_ids=(DEFERRED_ID,),
        )
    )

    with pytest.raises(AuditIntegrityError, match="covered deferred constraint"):
        asyncio.run(service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    _assert_stage_cohort_absent(service, command, session_id)


def test_stage_revalidates_deferred_originating_message_hash_inside_settlement(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "deferred-message-toctou")
    command, session_id = asyncio.run(
        _command(
            service,
            payload_store,
            initial_guided=_deferred_guided(message_content_hash=stable_hash("changed after planner verification")),
            covered_deferred_intent_ids=(DEFERRED_ID,),
        )
    )

    with pytest.raises(AuditIntegrityError, match="deferred intent message content hash mismatch"):
        asyncio.run(service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    _assert_stage_cohort_absent(service, command, session_id)


def test_stage_repeats_mechanical_coverage_under_settlement_lock_before_write(
    service: SessionServiceImpl,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.composer.guided import planning

    payload_store = FilesystemPayloadStore(tmp_path / "deferred-mechanical-toctou")
    command, session_id = asyncio.run(
        _command(
            service,
            payload_store,
            initial_guided=_deferred_guided(),
            covered_deferred_intent_ids=(DEFERRED_ID,),
        )
    )
    original = planning.verified_remaining_deferred_intents
    calls = 0

    def fail_second_verification(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AuditIntegrityError("simulated pre-stage mechanical authority drift")
        return original(**kwargs)

    monkeypatch.setattr(planning, "verified_remaining_deferred_intents", fail_second_verification)
    with pytest.raises(AuditIntegrityError, match="mechanical authority drift"):
        asyncio.run(service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    assert calls == 2
    _assert_stage_cohort_absent(service, command, session_id)


def test_stage_rejects_removal_of_uncovered_deferred_intent_atomically(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "uncovered-deferred-removal")
    _predecessor, command, session_id = asyncio.run(_successor_command(service, payload_store, initial_guided=_deferred_guided()))
    checkpoint_guided = GuidedSession.from_dict(deep_thaw(command.state.composer_meta)["guided_session"])
    tampered = replace(command, state=_state_data(replace(checkpoint_guided, deferred_intents=())))

    with pytest.raises(AuditIntegrityError, match="deferred"):
        asyncio.run(service.stage_guided_pipeline_proposal(tampered, payload_store=payload_store))

    _assert_stage_cohort_absent(service, tampered, session_id)


def test_revision_old_status_drift_rolls_back_successor_cohort(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    payload_store = FilesystemPayloadStore(tmp_path / "revision-status-drift")
    predecessor, successor, session_id = asyncio.run(_successor_command(service, payload_store))
    asyncio.run(
        service.reject_pipeline_composition_proposal(
            session_id=session_id,
            proposal_id=predecessor.proposal_id,
            draft_hash=predecessor.plan.proposal.draft_hash,
            reviewed_facts=guided_private_reviewed_facts(_guided()),
            reason="operator_rejected",
            dispatch=None,
            actor="concurrent-operator",
        )
    )

    with pytest.raises(AuditIntegrityError, match="no longer pending"):
        asyncio.run(service.stage_guided_pipeline_proposal(successor, payload_store=payload_store))

    with service._engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_proposals_table)
                .where(composition_proposals_table.c.id == str(successor.proposal_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_states_table)
                .where(composition_states_table.c.id == str(successor.checkpoint_state_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(proposal_events_table.c.proposal_id == str(successor.proposal_id))
            ).scalar_one()
            == 0
        )
    predecessor_events = [
        event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == predecessor.proposal_id
    ]
    assert [event.event_type for event in predecessor_events] == ["proposal.created", "proposal.rejected"]
    assert predecessor_events[-1].payload["reason_code"] == "operator_rejected"


async def _stage_and_reject(
    service: SessionServiceImpl,
    payload_store: FilesystemPayloadStore,
    *,
    reason: str,
) -> tuple[GuidedPipelineProposalStageCommand, UUID]:
    """Fabricate the rejected-row-behind-active-reference orphan.

    ``reject_pipeline_composition_proposal`` terminalizes the durable row
    without clearing the guided checkpoint reference — a state no fenced
    lifecycle produces. Used by ``tests/unit/web/sessions/test_routes.py``
    to pin that GET /guided fails closed on it without mutating state
    (elspeth-4dc78b3897).
    """
    command, session_id = await _command(service, payload_store)
    await service.stage_guided_pipeline_proposal(command, payload_store=payload_store)
    await service.reject_pipeline_composition_proposal(
        session_id=session_id,
        proposal_id=command.proposal_id,
        draft_hash=command.plan.proposal.draft_hash,
        reviewed_facts=guided_private_reviewed_facts(_guided()),
        reason=reason,  # type: ignore[arg-type]
        dispatch=None,
        actor="test",
    )
    return command, session_id


# ---------------------------------------------------------------------------
# CONFIRM_WIRING settlement parity (inv-f6 F6): staging asserts; settlement
# verifies — and the independent re-derivation must lower operator-profile
# options through the session principal's snapshot exactly like the route.
# ---------------------------------------------------------------------------

_PROFILE_CATALOG_IDS = {
    "source": frozenset({"csv"}),
    "transform": frozenset({"llm"}),
    "sink": frozenset({"json"}),
}


@pytest.fixture
def profile_service(tmp_path: Path) -> SimpleNamespace:
    """A profile-aware session service mirroring production create_app wiring."""
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        llm_profiles={
            "task-role": {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            }
        },
        default_llm_profile="task-role",
    )
    catalog = create_catalog_service()
    runtime_policy = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_policy)
    profile_registry = OperatorProfileRegistry(policy=policy, settings=runtime_policy)

    class _EmptyInventory:
        def has_server_ref(self, name: str) -> bool:
            return False

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return False

        def server_generation(self, name: str) -> str | None:
            return None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    def snapshot_for(user_id: str):
        return build_plugin_snapshot(
            policy=policy,
            catalog=catalog,
            profiles=profile_registry,
            principal_scope=f"local:{user_id}",
            secret_inventory=_EmptyInventory(),
            generation_key=b"guided-proposal-settlement-key",
        )

    return SimpleNamespace(
        service=SessionServiceImpl(
            engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.guided.proposal.profile"),
            plugin_snapshot_factory=snapshot_for,
            operator_profile_registry=profile_registry,
            catalog=catalog,
        ),
        snapshot_for=snapshot_for,
        profile_registry=profile_registry,
        catalog=catalog,
    )


def _profile_bound_pipeline() -> dict[str, object]:
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "summarize_rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "summarize_rows",
                "node_type": "transform",
                "plugin": "llm",
                "input": "summarize_rows",
                "on_success": "rows",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "profile": "task-role",
                    "prompt_template": "Summarise this row in one short sentence.",
                    "response_field": "summary",
                },
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {"schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    }


async def _confirm_wiring_command(
    harness: SimpleNamespace,
    payload_store: FilesystemPayloadStore,
) -> tuple[GuidedPipelineProposalStageCommand, UUID]:
    """Stage command whose durable turn is a route-built CONFIRM_WIRING review."""
    service = harness.service
    session = await service.create_session("alice", "Guided wire proposal", "local")
    base_guided = _guided()
    predecessor = await service.save_composition_state(session.id, _state_data(base_guided), provenance="session_seed")
    predecessor_state = _state_from_record(predecessor)
    checkpoint_id = uuid4()
    proposal_id = uuid4()
    plan = PipelinePlanResult(
        proposal=PipelineProposal.create(
            pipeline=_profile_bound_pipeline(),
            base=PresentBase(
                state_id=checkpoint_id,
                composition_content_hash=composition_content_hash(predecessor_state),
            ),
            reviewed_facts=guided_private_reviewed_facts(base_guided),
            surface=PlannerSurface.GUIDED_STAGED,
            repair_count=0,
            skill_hash=stable_hash("guided skill"),
            covered_deferred_intent_ids=(),
            supersedes_draft_hash=None,
        ),
        tool_call_id="guided-planner-wire-terminal",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
    )
    projection = build_guided_proposal_projection(
        proposal_id=proposal_id,
        proposal=plan.proposal,
        guided=base_guided,
        catalog_plugin_ids=_PROFILE_CATALOG_IDS,
    )
    # Route-built wire review: the authored candidate validated once through
    # the principal's policy boundary, with the lowered executable view
    # feeding the projection (guided.py review-advance/correction parity).
    candidate = guided_candidate_state(plan.proposal)
    policy_result = validate_authored_composition_state(
        candidate,
        snapshot=harness.snapshot_for("alice"),
        profile_registry=harness.profile_registry,
        catalog=harness.catalog,
    )
    assert not policy_result.validation.errors, policy_result.validation.errors
    assert policy_result.executable_state is not candidate
    wire_turn = build_step_4_wire_turn(
        candidate,
        proposal_projection=projection,
        guided=base_guided,
        catalog=None,
        validation_state=policy_result.executable_state,
        validation_summary=policy_result.validation,
    )
    prepared_wire = prepare_guided_json_payload(payload_store, purpose="turn", payload=wire_turn["payload"])
    active = GuidedProposalRef(
        proposal_id=proposal_id,
        draft_hash=plan.proposal.draft_hash,
        base=plan.proposal.base,
        reviewed_anchor_hash=plan.proposal.reviewed_anchor_hash,
        covered_deferred_intent_ids=(),
        creation_event_schema="pipeline_proposal_created.v1",
    )
    checkpoint_guided = replace(
        base_guided,
        step=GuidedStep.STEP_4_WIRE,
        active_proposal=active,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_4_WIRE,
                turn_type=TurnType.CONFIRM_WIRING,
                payload_hash=prepared_wire.payload_id,
                response_hash=None,
                emitter="server",
            ),
        ),
    )
    operation_id = str(uuid4())
    outcome = await service.reserve_guided_operation(
        session_id=session.id,
        operation_id=operation_id,
        kind="guided_respond",
        request_hash=stable_hash({"operation_id": operation_id}),
        actor="test",
        lease_seconds=300,
    )
    assert isinstance(outcome, GuidedOperationClaimed)
    return (
        GuidedPipelineProposalStageCommand(
            fence=outcome.fence,
            expected_current_state_id=predecessor.id,
            expected_current_state_version=predecessor.version,
            expected_current_content_hash=composition_content_hash(predecessor_state),
            checkpoint_state_id=checkpoint_id,
            proposal_id=proposal_id,
            state=_state_data(checkpoint_guided),
            plan=plan,
            summary="pipeline_proposal.summary.v1",
            rationale="pipeline_proposal.rationale.v1",
            affects=("pipeline",),
            arguments_redacted_json=redact_tool_call_arguments(
                "set_pipeline",
                _profile_bound_pipeline(),
                telemetry=NoopRedactionTelemetry(),
            ),
            catalog_plugin_ids=_PROFILE_CATALOG_IDS,
            proposal_projection=projection,
            actor="composer_route",
            user_message_id=None,
            user_message_content_hash=None,
            originating_message=None,
            supersedes_proposal_id=None,
            response=GuidedResponseDescriptor(
                kind="guided_respond",
                next_turn=GuidedReplayTurn(
                    turn_type=TurnType.CONFIRM_WIRING,
                    step_index=3,
                    payload_id=prepared_wire.payload_id,
                ),
                assistant_turn_seq=None,
            ),
            payloads=(prepared_wire,),
        ),
        session.id,
    )


def test_stage_confirm_wiring_re_verifies_through_profile_lowering(
    profile_service: SimpleNamespace,
    tmp_path: Path,
) -> None:
    """Settlement's 7-key comparison must hold against the route-built payload.

    inv-f6 F6 parity regression: the settlement rebuilt the wire review from
    the AUTHORED candidate — the un-lowered profile options crashed the
    row-cardinality probe, and even a tolerant probe would have diffed
    authored-vs-lowered projections into a false AuditIntegrityError.
    """
    payload_store = FilesystemPayloadStore(tmp_path / "payloads-profile-wire")
    command, session_id = asyncio.run(_confirm_wiring_command(profile_service, payload_store))

    settlement = asyncio.run(profile_service.service.stage_guided_pipeline_proposal(command, payload_store=payload_store))

    assert settlement.result_state.id == command.checkpoint_state_id
    assert settlement.proposal.id == command.proposal_id
    current = asyncio.run(profile_service.service.get_current_state(session_id))
    assert current is not None and current.id == command.checkpoint_state_id
    guided = _state_from_record(current).guided_session
    assert guided is not None and guided.active_proposal is not None
    assert guided.active_proposal.proposal_id == command.proposal_id
