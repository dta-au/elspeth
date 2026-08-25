"""Atomic retention of guided instructions given at the wrong stage."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.expression import FromClause

from elspeth.contracts.blobs import BlobQuotaExceededError
from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary
from elspeth.web.composer.guided import chat_solver
from elspeth.web.composer.guided.chat_solver import Step1SourceChatResolution
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentAction,
    DeferredIntentCancelAction,
    DeferredIntentEditAction,
    DeferredIntentManagementAction,
)
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.intent_management import deferred_intent_management_option
from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SinkResolved
from elspeth.web.composer.guided.stage_subjects import (
    ComponentCountConstraint,
    EdgeRouteConstraint,
    OptionValueConstraint,
    PluginSubject,
    StatedGateRoutingConstraint,
    SubjectPresenceConstraint,
)
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_proposal import PipelineProposal
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.sessions._guided_step_chat import (
    GuidedStepDeferredIntentResult,
    GuidedStepDeferredManagementResult,
    Step1SourceResolvedResult,
    Step2SinkResolvedResult,
    StepChatResult,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    guided_operation_events_table,
    guided_operations_table,
    proposal_events_table,
)
from elspeth.web.sessions.protocol import GUIDED_FAILURE_AUDIT_LINEAGE_KEY, GuidedOriginatingUserMessageDraft
from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.routes.composer import guided_chat_atomic
from elspeth.web.sessions.routes.composer.guided_chat_atomic import GuidedChatProviderOutcome
from tests.integration.web.composer.guided.test_respond import (
    TestStep2IntraStep,
    _post_current_response,
    _respond,
    _review_wiring,
)
from tests.integration.web.composer.guided.test_respond import (
    _outputs_path as _respond_outputs_path,
)
from tests.integration.web.composer.guided.test_respond_schema8_atomic import _respond_operation_count
from tests.integration.web.composer.guided.test_step_chat import TestStepChatCrossStep, _create_session
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def _dml_target_table(context: ExecutionContext) -> FromClause | None:
    """Return the table a compiled DML statement writes to, else ``None``.

    ``before_cursor_execute`` also fires for SELECT, DDL, and raw-text
    execution, none of which carry a DML target; those cases are the honest
    ``None``, not a missing attribute.
    """
    compiled = context.compiled
    statement = compiled.statement if compiled is not None else None
    if isinstance(statement, Insert | Update | Delete):
        return statement.table
    return None


def _session_message_rows(connection, session_id: str) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            select(chat_messages_table).where(chat_messages_table.c.session_id == session_id).order_by(chat_messages_table.c.sequence_no)
        )
        .mappings()
        .all()
    ]


def _assert_only_failure_chat_turn_audit_added(
    before: list[dict[str, object]],
    after: list[dict[str, object]],
    *,
    operation_id: str,
) -> None:
    before_by_id = {row["id"]: row for row in before}
    after_by_id = {row["id"]: row for row in after}
    assert {row_id: after_by_id[row_id] for row_id in before_by_id} == before_by_id
    added = [row for row_id, row in after_by_id.items() if row_id not in before_by_id]
    assert len(added) == 1, added
    (audit_row,) = added
    assert audit_row["role"] == "audit"
    assert audit_row["raw_content"] is None
    assert audit_row["writer_principal"] == "compose_loop"
    content = json.loads(str(audit_row["content"]))
    assert content["_kind"] == "chat_turn_audit"
    assert content[GUIDED_FAILURE_AUDIT_LINEAGE_KEY]["operation_id"] == operation_id
    tool_calls = audit_row["tool_calls"]
    assert isinstance(tool_calls, list)
    assert len(tool_calls) == 1
    assert tool_calls[0]["_kind"] == "chat_turn_audit"
    assert tool_calls[0][GUIDED_FAILURE_AUDIT_LINEAGE_KEY] == content[GUIDED_FAILURE_AUDIT_LINEAGE_KEY]


def _action(
    *,
    target_stage: str = "topology",
    catalog_kind: str = "transform",
    catalog_name: str = "passthrough",
    count: int = 1,
) -> DeferredIntentAction:
    component_kind = {"source": "source", "transform": "node", "sink": "output"}[catalog_kind]
    return DeferredIntentAction(
        target_stage=target_stage,  # type: ignore[arg-type]
        catalog_kind=catalog_kind,  # type: ignore[arg-type]
        catalog_name=catalog_name,
        redacted_summary=f"Include the named {catalog_kind} during {target_stage} authoring.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind=component_kind,  # type: ignore[arg-type]
                plugin_kind=catalog_kind,  # type: ignore[arg-type]
                plugin_name=catalog_name,
                operator="at_least",
                count=count,
            ),
        ),
    )


def _provider(action: DeferredIntentAction) -> Callable[..., Awaitable[GuidedChatProviderOutcome]]:
    async def run(**_kwargs: object) -> GuidedChatProviderOutcome:
        return GuidedStepDeferredIntentResult(
            chat=StepChatResult(
                assistant_message="provider provisional text must not become authority",
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            actions=(action,),
        )

    return run


def _management_provider(action: DeferredIntentManagementAction) -> Callable[..., Awaitable[GuidedChatProviderOutcome]]:
    async def run(**_kwargs: object) -> GuidedChatProviderOutcome:
        return GuidedStepDeferredManagementResult(
            chat=StepChatResult(
                assistant_message="provider provisional management text must not become authority",
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            action=action,
        )

    return run


class _Catalog:
    def __init__(
        self,
        plugins: tuple[tuple[PluginKind, str], ...],
        schemas: dict[tuple[PluginKind, str], dict[str, object]] | None = None,
        schema_overrides: dict[tuple[PluginKind, str], PluginSchemaInfo] | None = None,
    ) -> None:
        self._plugins = plugins
        self._schemas = schemas or {}
        self._schema_overrides = schema_overrides or {}

    def _list(self, kind: PluginKind) -> list[PluginSummary]:
        return [
            PluginSummary(name=name, description=name, plugin_type=kind, config_fields=[])
            for plugin_kind, name in self._plugins
            if plugin_kind == kind
        ]

    def list_sources(self) -> list[PluginSummary]:
        return self._list("source")

    def list_transforms(self) -> list[PluginSummary]:
        return self._list("transform")

    def list_sinks(self) -> list[PluginSummary]:
        return self._list("sink")

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        overridden = self._schema_overrides.get((plugin_type, name))
        if overridden is not None:
            return overridden
        json_schema = self._schemas.get((plugin_type, name))
        if json_schema is None:
            raise AssertionError("wrong-stage integration must inspect schemas only for option-value constraints")
        return PluginSchemaInfo(
            name=name,
            plugin_type=plugin_type,
            description=name,
            json_schema=json_schema,
            knob_schema={"fields": []},
        )

    def post_call_hints(
        self,
        *,
        plugin_type: PluginKind,
        plugin_name: str,
        tool_name: str,
        config_snapshot: dict[str, object],
    ) -> tuple[str, ...]:
        raise AssertionError("wrong-stage integration must not dispatch plugins")


def _policy_context(
    installed: tuple[tuple[PluginKind, str], ...],
    *,
    available: frozenset[PluginId],
    schemas: dict[tuple[PluginKind, str], dict[str, object]] | None = None,
    schema_overrides: dict[tuple[PluginKind, str], PluginSchemaInfo] | None = None,
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:alice",
        available=available,
        unavailable=tuple(
            PluginAvailability(plugin_id=PluginId(kind, name), reason=PluginUnavailableReason.NOT_AUTHORIZED)
            for kind, name in installed
            if PluginId(kind, name) not in available
        ),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(_Catalog(installed, schemas, schema_overrides), snapshot, profiles=None), snapshot  # type: ignore[arg-type]


def _post(
    client: TestClient,
    session_id: str,
    *,
    operation_id: str,
    turn_token: str,
    message: str,
) -> object:
    return client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json={"operation_id": operation_id, "turn_token": turn_token, "message": message},
    )


def _text_outside_chat_history(response_json: object) -> str:
    """Serialize a guided chat/respond response with the transcript excised.

    The rendered transcript legitimately carries the author's verbatim words
    (R2-F15 transcript custody) — both at ``guided_session.chat_history`` and
    in the persisted checkpoint mirror at
    ``composition_state.composer_meta.guided_session.chat_history``. Every
    OTHER response surface — next_turn payloads, composition state proper,
    assistant messages, terminal state — must still never echo the private
    prose. This keeps the blanket needle guard on those surfaces.
    """
    scrubbed = json.loads(json.dumps(response_json))
    if isinstance(scrubbed, dict):
        guided_session = scrubbed.get("guided_session")
        if isinstance(guided_session, dict):
            guided_session.pop("chat_history", None)
        composition_state = scrubbed.get("composition_state")
        if isinstance(composition_state, dict):
            composer_meta = composition_state.get("composer_meta")
            if isinstance(composer_meta, dict):
                meta_guided = composer_meta.get("guided_session")
                if isinstance(meta_guided, dict):
                    meta_guided.pop("chat_history", None)
    return json.dumps(scrubbed)


def _pair_sink_provider(sink: SinkResolved, action: DeferredIntentAction) -> Callable[..., Awaitable[GuidedChatProviderOutcome]]:
    async def run(**_kwargs: object) -> GuidedChatProviderOutcome:
        return Step2SinkResolvedResult(
            chat=StepChatResult(
                assistant_message="Output set to JSON.",
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            sink=sink,
            deferred_actions=(action,),
        )

    return run


def _pair_source_provider(
    resolution: Step1SourceChatResolution,
    action: DeferredIntentAction,
) -> Callable[..., Awaitable[GuidedChatProviderOutcome]]:
    async def run(**_kwargs: object) -> GuidedChatProviderOutcome:
        return Step1SourceResolvedResult(
            chat=StepChatResult(
                assistant_message=resolution.assistant_message,
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            resolution=resolution,
            deferred_actions=(action,),
        )

    return run


def _guided(client: TestClient, session_id: str):
    record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided = state_from_record(record).guided_session
    assert guided is not None
    return guided


def _non_root_user_rows(client: TestClient, session_id: str) -> list[tuple[str, str]]:
    """Return user rows created after the authoritative guided-start intent."""
    guided = _guided(client, session_id)
    root_message_id = guided.root_intent_message_id
    assert root_message_id is not None
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    return [(str(message.id), message.content) for message in messages if message.role == "user" and str(message.id) != root_message_id]


def _topology_presence_action() -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Preserve the configured source during topology authoring.",
        constraints=(
            SubjectPresenceConstraint(
                kind="subject_presence",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="11111111-1111-4111-8111-111111111111",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                present=True,
            ),
        ),
    )


def _stated_gate_routing_action() -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage="topology",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Preserve the explicitly stated gate routes.",
        constraints=(
            StatedGateRoutingConstraint(
                kind="stated_gate_routing",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="11111111-1111-4111-8111-111111111111",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                column="amount",
                operator="greater_than",
                value=500,
                true_target="high_value",
                false_target="standard",
            ),
        ),
    )


def _wire_review_action(*, present: bool) -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage="wire_review",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary="Preserve the structural source-to-output route.",
        constraints=(
            EdgeRouteConstraint(
                kind="edge_route",
                from_subject=PluginSubject(
                    kind="plugin",
                    subject_id="11111111-1111-4111-8111-111111111111",
                    plugin_kind="source",
                    plugin_name="csv",
                ),
                edge_type="on_success",
                to_subject=PluginSubject(
                    kind="plugin",
                    subject_id="22222222-2222-4222-8222-222222222222",
                    plugin_kind="sink",
                    plugin_name="json",
                ),
                present=present,
            ),
        ),
    )


def _stage_schema8_topology_intent_proposal(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, object, dict]:
    session_id = _create_session(client)
    initial = client.get(f"/api/sessions/{session_id}/guided").json()
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_topology_presence_action()))
    retained_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=initial["next_turn"]["turn_token"],
        message="Later retain the topology constraint.",
    )
    assert retained_response.status_code == 200, retained_response.json()
    (retained,) = _guided(client, session_id).deferred_intents
    staged = TestStep2IntraStep()._stage_proposal(client, session_id, filename="schema8-rewind.jsonl")
    assert staged["guided_session"]["step"] == "step_3_transforms"
    return session_id, retained, staged


def test_initial_topology_planner_receives_verified_deferred_user_instruction(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-F17: the target-stage planner must receive the retained prose.

    The route already loads the originating user row and verifies its session,
    role, and content hash.  The exact verified content — not a canned fallback
    — is the planner intent that lets the existing stated-threshold guard reject
    a constant fan-out gate.
    """
    client = composer_test_client
    created = client.post("/api/sessions", json={"title": "r2-f17-no-root-intent"})
    assert created.status_code == 201, created.json()
    session_id = created.json()["id"]
    started = client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={"profile": "tutorial", "operation_id": str(uuid4())},
    )
    assert started.status_code == 200, started.json()
    initial = started.json()
    instruction = "Later add a gate that routes csv rows with amount > 500 to high_value, and every other row to standard."
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_stated_gate_routing_action()))
    retained = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=initial["next_turn"]["turn_token"],
        message=instruction,
    )
    assert retained.status_code == 200, retained.json()

    captured: dict[str, object] = {}
    planner = client.app.state.composer_service
    real_plan = planner.plan_guided_pipeline

    async def capture_plan(**kwargs: object) -> object:
        captured.update(kwargs)
        return await real_plan(**kwargs)

    monkeypatch.setattr(planner, "plan_guided_pipeline", capture_plan)
    TestStep2IntraStep()._drive_to_step_2_single_select(client, session_id)
    _respond(client, session_id, chosen=["json"])
    _respond(
        client,
        session_id,
        edited_values={
            "plugin": "json",
            "options": {
                "path": _respond_outputs_path(client, session_id, "r2-f17.jsonl"),
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
        },
    )
    _respond(client, session_id, chosen=["text"], custom_inputs=[])
    # The shared test planner intentionally returns a no-gate fixture.  The
    # newly mandatory constraint correctly rejects that fixture after the call;
    # this test is about what prose reached the call boundary.
    rejected = _post_current_response(
        client,
        session_id,
        component_action={"action": "finish", "component_kind": "output"},
    )
    assert rejected.status_code == 500
    assert captured["intent"] == instruction


def test_proposal_revision_keeps_verified_deferred_instruction_in_planner_intent(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later revision cannot silently displace an uncovered obligation."""
    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    proposal = staged["next_turn"]["payload"]
    captured: dict[str, object] = {}
    planner = client.app.state.composer_service
    real_plan = planner.plan_guided_pipeline

    async def capture_plan(**kwargs: object) -> object:
        captured.update(kwargs)
        return await real_plan(**kwargs)

    monkeypatch.setattr(planner, "plan_guided_pipeline", capture_plan)
    revision = "Also add a deduplication transform before the outputs."
    revised = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": staged["next_turn"]["turn_token"],
            "proposal_id": proposal["proposal_id"],
            "draft_hash": proposal["draft_hash"],
            "edited_values": {"revision_instruction": revision},
        },
    )

    assert revised.status_code == 200, revised.json()
    revised_payload = revised.json()["next_turn"]["payload"]
    assert revised_payload["component_counts"]["nodes"] == 1
    assert [node["plugin"]["id"] for node in revised_payload["nodes"]] == ["passthrough"]
    assert captured["intent"] == f"Later retain the topology constraint.\n\n{revision}"


def test_wire_confirmation_refuses_to_complete_with_an_uncovered_deferred_intent(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ignored retained instruction cannot survive as hidden terminal debt."""
    client = composer_test_client
    session_id, retained, _staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    reviewed = _review_wiring(client, session_id)
    turn = reviewed["next_turn"]

    from elspeth.web.composer.guided import planning as guided_planning

    monkeypatch.setattr(
        guided_planning,
        "verified_remaining_deferred_intents",
        lambda **_kwargs: (retained,),
    )
    from elspeth.web.composer import pipeline_commit

    async def unexpected_prepare(**_kwargs: object) -> None:
        raise AssertionError("unresolved retained intent reached commit preparation")

    service = client.app.state.session_service

    async def unexpected_dispatch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unresolved retained intent reached pipeline dispatch")

    monkeypatch.setattr(pipeline_commit, "prepare_pipeline_proposal_commit", unexpected_prepare)
    monkeypatch.setattr(service, "record_guided_pipeline_dispatch", unexpected_dispatch)
    operations_before = _respond_operation_count(client, session_id)

    rejected = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": turn["turn_token"],
            "proposal_id": turn["payload"]["proposal_id"],
            "draft_hash": turn["payload"]["draft_hash"],
            "chosen": ["confirm_wiring"],
        },
    )

    assert rejected.status_code == 409, rejected.json()
    assert rejected.json()["detail"] == "Guided wiring still has unresolved retained instructions."
    assert _respond_operation_count(client, session_id) == operations_before
    current = client.get(f"/api/sessions/{session_id}/guided").json()
    assert current["terminal"] is None
    assert current["next_turn"]["type"] == "confirm_wiring"
    assert _guided(client, session_id).deferred_intents == (retained,)


def test_unique_future_catalog_intent_is_private_atomic_retryable_and_restart_durable(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    operation_id = str(uuid4())
    private_message = "Later use passthrough with customer-secret-needle in its private context."
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action()))
    with client.app.state.session_engine.connect() as connection:
        state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()

    first = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=turn["turn_token"],
        message=private_message,
    )

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message"] == "I saved that instruction for the topology stage."
    assert private_message not in _text_outside_chat_history(first_json)
    assert "provider provisional text" not in first.text
    guided = _guided(client, session_id)
    assert len(guided.deferred_intents) == 1
    (intent,) = guided.deferred_intents
    assert intent.receiving_stage == "source"
    assert intent.target_stage == "topology"
    assert intent.catalog_kind == "transform"
    assert intent.catalog_name == "passthrough"
    assert intent.message_content_hash == stable_hash(private_message)
    assert guided.active_proposal is None
    assert private_message not in repr(intent.to_dict())
    # Transcript custody (R2-F15): the author's own transcript shows their own
    # words; privacy is enforced at the provider/audit boundary, not by
    # blanking the user's turn.
    assert guided.chat_history[-2].content == private_message
    assert all("[Future-stage instruction submitted privately.]" not in turn.content for turn in guided.chat_history)

    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert _non_root_user_rows(client, session_id) == [(intent.originating_message_id, private_message)]
    assert all(private_message not in message.content for message in messages if message.role != "user")
    assert all(private_message not in repr(message.tool_calls) for message in messages if message.role != "user")
    audit_envelopes = [envelope for message in messages if message.role == "audit" for envelope in (message.tool_calls or ())]
    assert [envelope["_kind"] for envelope in audit_envelopes] == ["audit", "chat_turn_audit"]
    assert audit_envelopes[0]["invocation"]["tool_name"] == "guided_turn_emitted"
    assert set(audit_envelopes[1]) == {"_kind", "turn"}
    assert private_message not in repr(audit_envelopes)

    retry = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=turn["turn_token"],
        message=private_message,
    )
    assert retry.status_code == 200, retry.json()
    assert retry.json() == first_json
    with client.app.state.session_engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
            ).scalar_one()
            == state_count_before + 1
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(chat_messages_table)
                .where(
                    chat_messages_table.c.session_id == session_id,
                    chat_messages_table.c.role == "user",
                    chat_messages_table.c.id != _guided(client, session_id).root_intent_message_id,
                )
            ).scalar_one()
            == 1
        )
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one()
        )
    assert operation["originating_message_id"] == intent.originating_message_id

    restart = client.app.state.restart_test_client
    restarted = restart()
    refreshed = restarted.get(f"/api/sessions/{session_id}/guided")
    assert refreshed.status_code == 200, refreshed.json()
    restarted_guided = _guided(restarted, session_id)
    assert restarted_guided.deferred_intents == guided.deferred_intents


def test_cancel_requires_explicit_user_authority_then_removes_only_the_named_intent_and_replays_exactly(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action()))
    retained_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message="Later add passthrough.",
    )
    assert retained_response.status_code == 200, retained_response.json()
    assert retained_response.json()["assistant_message"] == "I saved that instruction for the topology stage."
    (retained,) = _guided(client, session_id).deferred_intents

    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _management_provider(
            DeferredIntentCancelAction(
                intent_id=retained.intent_id,
                selection_token=deferred_intent_management_option(retained).selection_token,
            )
        ),
    )
    unauthorized = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=retained_response.json()["next_turn"]["turn_token"],
        message="Keep this saved instruction; explain it.",
    )
    assert unauthorized.status_code == 200, unauthorized.json()
    assert unauthorized.json()["assistant_message_kind"] == "synthetic_failure"
    assert _guided(client, session_id).deferred_intents == (retained,)

    operation_id = str(uuid4())
    private_cancel_request = f"Cancel exact intent {retained.intent_id}."
    first = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=unauthorized.json()["next_turn"]["turn_token"],
        message=private_cancel_request,
    )

    assert first.status_code == 200, first.json()
    assert first.json()["assistant_message"] == "I cancelled that saved topology instruction."
    assert _guided(client, session_id).deferred_intents == ()
    assert private_cancel_request not in _text_outside_chat_history(first.json())
    assert _guided(client, session_id).chat_history[-2].content == private_cancel_request
    retry = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=unauthorized.json()["next_turn"]["turn_token"],
        message=private_cancel_request,
    )
    assert retry.status_code == 200
    assert retry.json() == first.json()

    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    cancellation_events = [
        envelope
        for message in messages
        if message.role == "audit"
        for envelope in (message.tool_calls or ())
        if envelope.get("invocation", {}).get("tool_name") == "guided_intent_cancelled"
    ]
    assert len(cancellation_events) == 1
    assert private_cancel_request not in repr(cancellation_events)


def test_destructive_selection_binding_rejects_mixups_and_requires_uuid_when_selection_is_plural(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(count=1)))
    first_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message="Save the count-one transform constraint.",
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(count=2)))
    second_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=first_response.json()["next_turn"]["turn_token"],
        message="Save the count-two transform constraint.",
    )
    first, second = _guided(client, session_id).deferred_intents

    mixed = DeferredIntentCancelAction(
        intent_id=first.intent_id,
        selection_token=deferred_intent_management_option(second).selection_token,
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _management_provider(mixed))
    mismatch_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=second_response.json()["next_turn"]["turn_token"],
        message="Cancel the count-one transform constraint.",
    )
    assert mismatch_response.status_code == 200, mismatch_response.json()
    assert mismatch_response.json()["assistant_message_kind"] == "synthetic_failure"
    assert mismatch_response.json()["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert _guided(client, session_id).deferred_intents == (first, second)

    correct = DeferredIntentCancelAction(
        intent_id=first.intent_id,
        selection_token=deferred_intent_management_option(first).selection_token,
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _management_provider(correct))
    selected_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=mismatch_response.json()["next_turn"]["turn_token"],
        message=f"Cancel exact intent {first.intent_id}.",
    )
    assert selected_response.status_code == 200, selected_response.json()
    assert _guided(client, session_id).deferred_intents == (second,)

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(count=2)))
    duplicate_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=selected_response.json()["next_turn"]["turn_token"],
        message="Save another count-two transform constraint.",
    )
    second, duplicate = _guided(client, session_id).deferred_intents
    duplicate_cancel = DeferredIntentCancelAction(
        intent_id=second.intent_id,
        selection_token=deferred_intent_management_option(second).selection_token,
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _management_provider(duplicate_cancel))
    ambiguous_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=duplicate_response.json()["next_turn"]["turn_token"],
        message="Cancel one count-two transform constraint.",
    )
    assert ambiguous_response.status_code == 200, ambiguous_response.json()
    assert ambiguous_response.json()["assistant_message_kind"] == "synthetic_failure"
    assert ambiguous_response.json()["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert _guided(client, session_id).deferred_intents == (second, duplicate)

    explicit_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=ambiguous_response.json()["next_turn"]["turn_token"],
        message=f"Cancel exact intent {second.intent_id}.",
    )
    assert explicit_response.status_code == 200, explicit_response.json()
    assert _guided(client, session_id).deferred_intents == (duplicate,)


def test_schema8_topology_management_rewinds_through_output_review_and_atomically_supersedes_pending_proposal(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id, retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]

    operation_id = str(uuid4())
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _management_provider(
            DeferredIntentCancelAction(
                intent_id=retained.intent_id,
                selection_token=deferred_intent_management_option(retained).selection_token,
            )
        ),
    )
    cancelled = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=staged["next_turn"]["turn_token"],
        message=f"Cancel exact intent {retained.intent_id}.",
    )

    assert cancelled.status_code == 200, cancelled.json()
    body = cancelled.json()
    assert body["guided_session"]["step"] == "step_2_sink"
    assert body["next_turn"]["type"] == "review_components"
    assert body["next_turn"]["payload"]["component_kind"] == "output"
    assert _guided(client, session_id).deferred_intents == ()
    with client.app.state.session_engine.connect() as connection:
        proposal = (
            connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).mappings().one()
        )
        events = (
            connection.execute(select(proposal_events_table.c.event_type).where(proposal_events_table.c.proposal_id == proposal_id))
            .scalars()
            .all()
        )
    assert proposal["status"] == "rejected"
    assert events == ["proposal.created", "proposal.rejected"]

    replay = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=staged["next_turn"]["turn_token"],
        message=f"Cancel exact intent {retained.intent_id}.",
    )
    assert replay.status_code == 200
    assert replay.json() == body


def test_management_non_string_provider_content_is_bounded_without_private_egress(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    private_canary = "PRIVATE-NESTED-MANAGEMENT-ROUTE-CANARY"
    malformed_content = {"summary": ["ordinary", {"private": private_canary}]}

    async def malformed_reply(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=malformed_content, tool_calls=None))],
        )

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", guided_chat_atomic.run_guided_chat_provider_attempt)
    monkeypatch.setattr(chat_solver, "_litellm_acompletion", malformed_reply)
    operation_id = str(uuid4())
    with capture_logs() as logs:
        response = _post(
            client,
            session_id,
            operation_id=operation_id,
            turn_token=staged["next_turn"]["turn_token"],
            message="Explain the saved topology instruction.",
        )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    # "model_defect", not "unavailable", since the R2-F15 pair-salvage fix:
    # the non-string content now classifies as GuidedToolArgumentShapeError —
    # the provider ANSWERED and the reply violated the contract, which is
    # exactly the mislabel the model_defect mapping exists to correct
    # (inv-f1 D4). The turn is still synthetic and the egress bounds below
    # are unchanged.
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "model_defect"
    assert private_canary not in response.text
    assert private_canary not in repr(logs)

    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert private_canary not in repr(messages)
    with client.app.state.session_engine.connect() as connection:
        persisted_rows = {
            "messages": connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)).all(),
            "states": connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == session_id)).all(),
            "proposals": connection.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.session_id == session_id)
            ).all(),
            "operations": connection.execute(
                select(guided_operations_table).where(guided_operations_table.c.session_id == session_id)
            ).all(),
            "operation_events": connection.execute(
                select(guided_operation_events_table).where(guided_operation_events_table.c.session_id == session_id)
            ).all(),
            "proposal_events": connection.execute(
                select(proposal_events_table).where(proposal_events_table.c.session_id == session_id)
            ).all(),
        }
    assert private_canary not in repr(persisted_rows)


@pytest.mark.parametrize("covered", [False, True], ids=["uncovered", "covered"])
@pytest.mark.parametrize("management_kind", ["cancel", "edit"])
def test_active_proposal_future_wire_management_always_invalidates_and_rewinds_without_planner(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    covered: bool,
    management_kind: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    initial = client.get(f"/api/sessions/{session_id}/guided").json()
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_wire_review_action(present=True)))
    retained_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=initial["next_turn"]["turn_token"],
        message="Later preserve the source-to-output route.",
    )
    assert retained_response.status_code == 200, retained_response.json()
    (retained,) = _guided(client, session_id).deferred_intents

    planner = client.app.state.composer_service
    real_plan = planner.plan_guided_pipeline

    async def plan_with_exact_coverage(*, guided, **kwargs):
        plan, catalog_plugin_ids = await real_plan(guided=guided, **kwargs)
        proposal = plan.proposal
        rebound = PipelineProposal.create(
            pipeline=proposal.pipeline,
            base=proposal.base,
            reviewed_facts=guided_private_reviewed_facts(guided),
            surface=proposal.surface,
            repair_count=proposal.repair_count,
            skill_hash=proposal.skill_hash,
            covered_deferred_intent_ids=(retained.intent_id,) if covered else (),
            supersedes_draft_hash=proposal.supersedes_draft_hash,
        )
        return replace(plan, proposal=rebound), catalog_plugin_ids

    monkeypatch.setattr(planner, "plan_guided_pipeline", plan_with_exact_coverage)
    staged = TestStep2IntraStep()._stage_proposal(client, session_id, filename=f"wire-{covered}-{management_kind}.jsonl")
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]
    assert staged["guided_session"]["step"] == "step_3_transforms"

    async def planner_must_not_run(**_kwargs: object) -> object:
        raise AssertionError("management rewind must not invoke the planner")

    monkeypatch.setattr(planner, "plan_guided_pipeline", planner_must_not_run)
    if management_kind == "cancel":
        management_action: DeferredIntentManagementAction = DeferredIntentCancelAction(
            intent_id=retained.intent_id,
            selection_token=deferred_intent_management_option(retained).selection_token,
        )
        management_message = f"Cancel exact intent {retained.intent_id}."
    else:
        management_action = DeferredIntentEditAction(
            intent_id=retained.intent_id,
            selection_token=deferred_intent_management_option(retained).selection_token,
            replacement=_wire_review_action(present=False),
        )
        management_message = f"Edit exact intent {retained.intent_id}: remove the saved wire requirement."
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _management_provider(management_action))
    operation_id = str(uuid4())
    response = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=staged["next_turn"]["turn_token"],
        message=management_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["guided_session"]["step"] == "step_2_sink"
    assert body["next_turn"]["payload"]["component_kind"] == "output"
    remaining = _guided(client, session_id).deferred_intents
    if management_kind == "cancel":
        assert remaining == ()
    else:
        assert [intent.intent_id for intent in remaining] == [retained.intent_id]
        assert remaining[0].constraints[0].to_dict()["present"] is False
    with client.app.state.session_engine.connect() as connection:
        proposal = (
            connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).mappings().one()
        )
    assert proposal["status"] == "rejected"
    replay = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=staged["next_turn"]["turn_token"],
        message=management_message,
    )
    assert replay.status_code == 200
    assert replay.json() == body


def test_step3_chat_before_a_prose_revision_yields_one_ordered_transcript(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-F6 interleaving: /guided/chat and /guided/respond share one counter.

    ``/guided/chat`` is admitted at step 3 exactly when a deferred intent is
    pending, so a chat turn and a prose revision can interleave at the same
    step. Every other R2-F6 test starts from an empty transcript, where a
    wrong-seq-base regression is invisible (0 is 0 either way). This one pins a
    non-zero base: the step-1 deferral writes seq 0/1, the step-3 chat writes
    2/3, and the revision must continue at 4/5 in one strictly increasing,
    correctly attributed sequence.
    """
    from elspeth.web.composer.guided.protocol import GUIDED_PROSE_REVISION_ACKNOWLEDGEMENT

    client = composer_test_client
    session_id, retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    proposal = staged["next_turn"]["payload"]
    base = _guided(client, session_id)
    assert base.chat_turn_seq == 2, "the step-1 deferral must already have written a user/assistant pair"

    # A binding mismatch (right token, wrong intent id) is deliberately NOT
    # applied, so the session stays at step 3 with its proposal intact and the
    # turn is recorded as a synthetic failure — the interleaved shape this test
    # needs without rewinding the wizard.
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _management_provider(
            DeferredIntentCancelAction(
                intent_id=str(uuid4()),
                selection_token=deferred_intent_management_option(retained).selection_token,
            )
        ),
    )
    chat_message = "Cancel the topology constraint I saved earlier."
    chatted = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=staged["next_turn"]["turn_token"],
        message=chat_message,
    )
    assert chatted.status_code == 200, chatted.json()
    assert chatted.json()["assistant_message_kind"] == "synthetic_failure"
    after_chat = _guided(client, session_id)
    assert after_chat.step.value == "step_3_transforms"
    assert after_chat.chat_turn_seq == 4
    assert after_chat.active_proposal is not None

    instruction = "Add a deduplication transform before the output."
    revised = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": chatted.json()["next_turn"]["turn_token"],
            "proposal_id": proposal["proposal_id"],
            "draft_hash": proposal["draft_hash"],
            "edited_values": {"revision_instruction": instruction},
        },
    )

    assert revised.status_code == 200, revised.json()
    revised_payload = revised.json()["next_turn"]["payload"]
    assert revised_payload["component_counts"]["nodes"] == 1
    assert [node["plugin"]["id"] for node in revised_payload["nodes"]] == ["passthrough"]
    guided = _guided(client, session_id)
    assert [(turn.role.value, turn.seq, turn.step.value) for turn in guided.chat_history] == [
        ("user", 0, "step_1_source"),
        ("assistant", 1, "step_1_source"),
        ("user", 2, "step_3_transforms"),
        ("assistant", 3, "step_3_transforms"),
        ("user", 4, "step_3_transforms"),
        ("assistant", 5, "step_3_transforms"),
    ]
    assert guided.chat_history[2].content == chat_message
    assert guided.chat_history[4].content == instruction
    assert guided.chat_history[5].content == GUIDED_PROSE_REVISION_ACKNOWLEDGEMENT
    assert guided.chat_history[5].assistant_message_kind == "assistant"
    assert guided.chat_turn_seq == 6


def test_management_provider_api_error_completes_unavailable_turn_without_mutating_intent_or_active_proposal(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.exceptions import APIError as LiteLLMAPIError

    real_provider_runner = guided_route._run_guided_chat_provider_attempt
    client = composer_test_client
    session_id, retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]
    before = _guided(client, session_id)
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", real_provider_runner)

    async def provider_failure(**_kwargs: object) -> object:
        raise LiteLLMAPIError(
            status_code=500,
            message="private upstream failure detail",
            llm_provider="test",
            model="test/model",
        )

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", provider_failure)
    operation_id = str(uuid4())
    response = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=staged["next_turn"]["turn_token"],
        message="Cancel the pending topology instruction.",
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == "I'm unavailable right now; you can still use the wizard controls."
    after = _guided(client, session_id)
    assert after.deferred_intents == (retained,)
    assert after.active_proposal == before.active_proposal
    assert after.step == before.step
    with client.app.state.session_engine.connect() as connection:
        proposal = (
            connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).mappings().one()
        )
        events = (
            connection.execute(select(proposal_events_table.c.event_type).where(proposal_events_table.c.proposal_id == proposal_id))
            .scalars()
            .all()
        )
        operation = (
            connection.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id))
            .mappings()
            .one()
        )
    assert proposal["status"] == "pending"
    assert events == ["proposal.created"]
    assert operation["status"] == "completed"
    assert operation["result_state_id"] is not None


def test_schema8_passed_output_edit_preserves_stable_id_and_rewinds_reviewed_pending_proposal(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    initial = client.get(f"/api/sessions/{session_id}/guided").json()
    output_action = DeferredIntentAction(
        target_stage="output",
        catalog_kind="sink",
        catalog_name="json",
        redacted_summary="Retain one JSON output requirement.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="output",
                plugin_kind="sink",
                plugin_name="json",
                operator="at_least",
                count=1,
            ),
        ),
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(output_action))
    retained_response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=initial["next_turn"]["turn_token"],
        message="Keep this output instruction pending through proposal review.",
    )
    assert retained_response.status_code == 200, retained_response.json()
    (retained,) = _guided(client, session_id).deferred_intents

    planner = client.app.state.composer_service
    real_plan = planner.plan_guided_pipeline

    async def plan_without_claiming_intent(*, guided, **kwargs):
        plan, catalog_plugin_ids = await real_plan(guided=guided, **kwargs)
        proposal = plan.proposal
        unclaimed = PipelineProposal.create(
            pipeline=proposal.pipeline,
            base=proposal.base,
            reviewed_facts=guided_private_reviewed_facts(guided),
            surface=proposal.surface,
            repair_count=proposal.repair_count,
            skill_hash=proposal.skill_hash,
            covered_deferred_intent_ids=(),
            supersedes_draft_hash=proposal.supersedes_draft_hash,
        )
        return replace(plan, proposal=unclaimed), catalog_plugin_ids

    monkeypatch.setattr(planner, "plan_guided_pipeline", plan_without_claiming_intent)
    staged = TestStep2IntraStep()._stage_proposal(client, session_id, filename="schema8-passed-output.jsonl")
    proposal = staged["next_turn"]["payload"]
    reviewed = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": staged["next_turn"]["turn_token"],
            "proposal_id": proposal["proposal_id"],
            "draft_hash": proposal["draft_hash"],
            "chosen": ["review_wiring"],
        },
    )
    assert reviewed.status_code == 200, reviewed.json()
    assert reviewed.json()["guided_session"]["step"] == "step_4_wire"
    assert [intent.intent_id for intent in _guided(client, session_id).deferred_intents] == [retained.intent_id]

    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _management_provider(
            DeferredIntentEditAction(
                intent_id=retained.intent_id,
                selection_token=deferred_intent_management_option(retained).selection_token,
                replacement=DeferredIntentAction(
                    target_stage="output",
                    catalog_kind="sink",
                    catalog_name="json",
                    redacted_summary="Retain the revised JSON output requirement.",
                    constraints=(
                        ComponentCountConstraint(
                            kind="component_count",
                            component_kind="output",
                            plugin_kind="sink",
                            plugin_name="json",
                            operator="at_least",
                            count=2,
                        ),
                    ),
                ),
            )
        ),
    )
    cancelled = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=reviewed.json()["next_turn"]["turn_token"],
        message=f"Edit exact intent {retained.intent_id}: require two JSON outputs.",
    )

    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["guided_session"]["step"] == "step_2_sink"
    assert cancelled.json()["next_turn"]["type"] == "review_components"
    assert cancelled.json()["next_turn"]["payload"]["component_kind"] == "output"
    (revised,) = _guided(client, session_id).deferred_intents
    assert revised.intent_id == retained.intent_id
    assert revised.constraints[0].to_dict()["count"] == 2
    assert revised.originating_message_id != retained.originating_message_id
    with client.app.state.session_engine.connect() as connection:
        proposal_row = (
            connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal["proposal_id"]))
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                select(proposal_events_table.c.event_type).where(proposal_events_table.c.proposal_id == proposal["proposal_id"])
            )
            .scalars()
            .all()
        )
    assert proposal_row["status"] == "rejected"
    assert events == ["proposal.created", "proposal.rejected"]


@pytest.mark.parametrize("fault_point", ("proposal_event", "proposal_update"))
def test_schema8_management_proposal_invalidation_fault_rolls_back_intent_proposal_checkpoint_and_custody(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    client = composer_test_client
    session_id, retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]
    operation_id = str(uuid4())
    engine = client.app.state.session_engine
    with engine.connect() as connection:
        state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()
        messages_before = _session_message_rows(connection, session_id)

    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _management_provider(
            DeferredIntentCancelAction(
                intent_id=retained.intent_id,
                selection_token=deferred_intent_management_option(retained).selection_token,
            )
        ),
    )
    reached = False

    def inject_fault(_conn, _cursor, statement, _parameters, context, _executemany):
        nonlocal reached
        target_table = _dml_target_table(context)
        normalized = " ".join(statement.lower().split())
        matched = (fault_point == "proposal_event" and target_table is proposal_events_table) or (
            fault_point == "proposal_update" and normalized.startswith("update composition_proposals")
        )
        if matched and not reached:
            reached = True
            raise RuntimeError(f"injected {fault_point}")

    event.listen(engine, "before_cursor_execute", inject_fault)
    try:
        response = _post(
            client,
            session_id,
            operation_id=operation_id,
            turn_token=staged["next_turn"]["turn_token"],
            message=f"Cancel exact intent {retained.intent_id}.",
        )
    finally:
        event.remove(engine, "before_cursor_execute", inject_fault)

    assert response.status_code == 500, response.json()
    assert reached
    assert [intent.intent_id for intent in _guided(client, session_id).deferred_intents] == [retained.intent_id]
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
            ).scalar_one()
            == state_count_before
        )
        messages_after = _session_message_rows(connection, session_id)
        proposal = (
            connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).mappings().one()
        )
        proposal_event_types = (
            connection.execute(select(proposal_events_table.c.event_type).where(proposal_events_table.c.proposal_id == proposal_id))
            .scalars()
            .all()
        )
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one()
        )
    assert proposal["status"] == "pending"
    assert proposal_event_types == ["proposal.created"]
    _assert_only_failure_chat_turn_audit_added(messages_before, messages_after, operation_id=operation_id)
    assert operation["status"] == "failed"
    assert operation["originating_message_id"] is None
    assert operation["result_state_id"] is None


def test_pair_of_sink_resolution_and_future_intent_applies_both_atomically(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message mixing sink values and a future-stage instruction loses neither.

    R2-F15's observed failure: the pair was rejected wholesale (both halves
    discarded, `Path: Not set`). The pair must now stage the sink prefill AND
    create the deferred intent in one atomic settlement.
    """
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_2_sink"
    out_path = _respond_outputs_path(client, session_id, "pair_out.jsonl")
    private_message = "Save results as jsonl, and later add the passthrough transform with secret-pair-needle."
    provider_calls = 0

    async def pair_completion(**_kwargs: object) -> SimpleNamespace:
        nonlocal provider_calls
        provider_calls += 1
        tool_calls = [
            SimpleNamespace(
                id="c_sink",
                function=SimpleNamespace(
                    name="resolve_sink",
                    arguments=json.dumps(
                        {
                            "resolution": "sink",
                            "output": {
                                "name": "main",
                                "plugin": "json",
                                "options": {
                                    "path": out_path,
                                    "schema": {"mode": "observed"},
                                    "mode": "write",
                                    "collision_policy": "auto_increment",
                                },
                                "required_fields": [],
                                "schema_mode": "observed",
                                "on_write_failure": "discard",
                            },
                            "assistant_message": "Output set to JSON.",
                        }
                    ),
                ),
            ),
            SimpleNamespace(
                id="c_retain",
                function=SimpleNamespace(
                    name="retain_deferred_intent",
                    arguments=json.dumps(
                        {
                            "target_stage": "topology",
                            "catalog_kind": "transform",
                            "catalog_name": "passthrough",
                            "redacted_summary": "Include the named transform during topology authoring.",
                            "constraints": [
                                {
                                    "kind": "component_count",
                                    "component_kind": "node",
                                    "plugin_kind": "transform",
                                    "plugin_name": "passthrough",
                                    "operator": "at_least",
                                    "count": 1,
                                }
                            ],
                        }
                    ),
                ),
            ),
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))])

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", pair_completion)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert provider_calls == 1
    assert body["assistant_message_kind"] == "assistant"
    assert body["assistant_message"] == "Output set to JSON. I saved that instruction for the topology stage."
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.receiving_stage == "output"
    assert intent.target_stage == "topology"
    assert intent.catalog_kind == "transform"
    assert intent.catalog_name == "passthrough"
    assert intent.message_content_hash == stable_hash(private_message)
    # The sink half applied too: the plugin selection was answered and the
    # projected sink schema form carries the resolved prefill.
    next_turn = body["next_turn"]
    assert next_turn is not None
    assert next_turn["type"] == "schema_form"
    assert next_turn["payload"]["prefilled"]["path"] == out_path
    # The raw mixed message stays out of every non-user surface.
    assert private_message not in _text_outside_chat_history(body)
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert [content for _message_id, content in _non_root_user_rows(client, session_id)] == [private_message]
    assert all(private_message not in message.content for message in messages if message.role != "user")
    assert all(private_message not in repr(message.tool_calls) for message in messages if message.role != "user")


def test_group_of_sink_resolution_and_two_future_intents_applies_all_atomically(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message naming a sink AND two future stages loses none of its halves.

    elspeth-3a21f09f09 (the WS6 collector calibration walk): the planner
    correctly emits one retain per future-stage instruction, and the whole
    group must settle atomically — the sink prefill applies and BOTH deferred
    intents append, in call order, bound to the one originating message.
    """
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_2_sink"
    out_path = _respond_outputs_path(client, session_id, "group_out.jsonl")
    private_message = "Save results as jsonl, later add the passthrough transform, and later map fields with secret-group-needle."

    def _retain_call(call_id: str, plugin_name: str, summary: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name="retain_deferred_intent",
                arguments=json.dumps(
                    {
                        "target_stage": "topology",
                        "catalog_kind": "transform",
                        "catalog_name": plugin_name,
                        "redacted_summary": summary,
                        "constraints": [
                            {
                                "kind": "component_count",
                                "component_kind": "node",
                                "plugin_kind": "transform",
                                "plugin_name": plugin_name,
                                "operator": "at_least",
                                "count": 1,
                            }
                        ],
                    }
                ),
            ),
        )

    async def group_completion(**_kwargs: object) -> SimpleNamespace:
        tool_calls = [
            SimpleNamespace(
                id="c_sink",
                function=SimpleNamespace(
                    name="resolve_sink",
                    arguments=json.dumps(
                        {
                            "resolution": "sink",
                            "output": {
                                "name": "main",
                                "plugin": "json",
                                "options": {
                                    "path": out_path,
                                    "schema": {"mode": "observed"},
                                    "mode": "write",
                                    "collision_policy": "auto_increment",
                                },
                                "required_fields": [],
                                "schema_mode": "observed",
                                "on_write_failure": "discard",
                            },
                            "assistant_message": "Output set to JSON.",
                        }
                    ),
                ),
            ),
            _retain_call("c_retain_1", "passthrough", "Include the named transform during topology authoring."),
            _retain_call("c_retain_2", "field_mapper", "Include the named mapping transform during topology authoring."),
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))])

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", group_completion)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "Output set to JSON. I saved that instruction for the topology stage. I saved that instruction for the topology stage."
    )
    guided = _guided(client, session_id)
    first, second = guided.deferred_intents
    assert (first.catalog_name, second.catalog_name) == ("passthrough", "field_mapper")
    for intent in (first, second):
        assert intent.receiving_stage == "output"
        assert intent.target_stage == "topology"
        assert intent.message_content_hash == stable_hash(private_message)
    assert first.originating_message_id == second.originating_message_id
    # The sink half applied too.
    next_turn = body["next_turn"]
    assert next_turn is not None
    assert next_turn["type"] == "schema_form"
    assert next_turn["payload"]["prefilled"]["path"] == out_path
    # The raw mixed message stays out of every non-user surface.
    assert private_message not in _text_outside_chat_history(body)
    assert [content for _message_id, content in _non_root_user_rows(client, session_id)] == [private_message]


def _last_chat_turn_audit(client: TestClient, session_id: str) -> dict:
    """Return the newest chat_turn_audit envelope's recorded turn."""
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    envelopes = [
        envelope
        for message in messages
        if message.role == "audit"
        for envelope in (message.tool_calls or ())
        if envelope.get("_kind") == "chat_turn_audit"
    ]
    assert envelopes, "no chat_turn_audit envelope recorded"
    return dict(envelopes[-1]["turn"])


def test_model_authored_transform_identity_for_structural_gate_retains_unverified_clarification(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    user_message = (
        "This is an orders CSV. Later on I want a gate that routes rows with amount greater than 500 to a high_value JSON sink, "
        "and everything else to a standard JSON sink. Every row must land in exactly one of them."
    )
    catalog, snapshot = _policy_context(
        (("transform", "passthrough"),),
        available=frozenset({PluginId("transform", "passthrough")}),
    )
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(catalog_name="numeric_route")))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message=user_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assistant_message = body["assistant_message"]
    assert "not installed" not in assistant_message.lower()
    assert "gate" in assistant_message.lower()
    assert "structure was not verified" in assistant_message.lower()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    audit_turn = _last_chat_turn_audit(client, session_id)
    assert audit_turn["status"] == ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE.value
    assert audit_turn["error_class"] == "DeferredIntentModelCatalogIdentity"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None
    assert intent.message_content_hash == stable_hash(user_message)
    ((originating_message_id, persisted_message),) = _non_root_user_rows(client, session_id)
    assert persisted_message == user_message
    assert intent.originating_message_id == originating_message_id


@pytest.mark.parametrize(
    ("installed_requested", "expected_reason"),
    [
        pytest.param(False, "is not installed", id="not-installed"),
        pytest.param(True, "is not enabled by the current policy", id="policy-denied"),
    ],
)
def test_user_named_unavailable_transform_reports_bounded_policy_visible_alternatives(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    installed_requested: bool,
    expected_reason: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    user_message = "Later use the numeric_route transform during topology authoring."
    visible_names = ("alpha", "beta", "delta", "epsilon", "gamma", "zeta")
    installed = (
        tuple(("transform", name) for name in visible_names)
        + ((("transform", "numeric_route"),) if installed_requested else ())
        + (("transform", "secret_transform"),)
    )
    available = frozenset(PluginId("transform", name) for name in visible_names)
    catalog, snapshot = _policy_context(installed, available=available)
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(catalog_name="numeric_route")))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message=user_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert expected_reason in body["assistant_message"]
    assert "Policy-visible transform alternatives: alpha, beta, delta, epsilon, gamma." in body["assistant_message"]
    assert "secret_transform" not in body["assistant_message"]
    assert "zeta" not in body["assistant_message"]
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    audit_turn = _last_chat_turn_audit(client, session_id)
    assert audit_turn["status"] == ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE.value
    assert audit_turn["error_class"] == "DeferredIntentUnsupported"
    assert _guided(client, session_id).deferred_intents == ()


_PAIR_RETAIN_ARGUMENTS: dict[str, object] = {
    "target_stage": "topology",
    "catalog_kind": "transform",
    "catalog_name": "passthrough",
    "redacted_summary": "Include the named transform during topology authoring.",
    "constraints": [
        {
            "kind": "component_count",
            "component_kind": "node",
            "plugin_kind": "transform",
            "plugin_name": "passthrough",
            "operator": "at_least",
            "count": 1,
        }
    ],
}

_PAIR_CONFIG_INVALID_SINK_ARGUMENTS: dict[str, object] = {
    "resolution": "sink",
    "output": {
        "name": "results",
        "plugin": "json",
        # flexible-without-fields fails the json sink's config model.
        "options": {"path": "out.jsonl", "schema": {"mode": "flexible"}},
        "required_fields": [],
        "schema_mode": "observed",
        "on_write_failure": "discard",
    },
    "assistant_message": "Saved the results as a JSON Lines file.",
}


def _pair_round(sink_arguments: object, round_index: int) -> SimpleNamespace:
    tool_calls = [
        SimpleNamespace(id=f"c_sink_{round_index}", function=SimpleNamespace(name="resolve_sink", arguments=sink_arguments)),
        SimpleNamespace(
            id=f"c_retain_{round_index}",
            function=SimpleNamespace(name="retain_deferred_intent", arguments=json.dumps(_PAIR_RETAIN_ARGUMENTS)),
        ),
    ]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))])


@pytest.mark.parametrize(
    ("scenario", "expected_error_class"),
    [
        ("cap_exhaustion", "PairedResolutionConfigRejected"),
        ("shape_invalid_sink", "PairedResolutionShapeRejected"),
        ("prose_decline", "PairedResolutionNotResent"),
        ("hallucinated_tool", "PairedResolutionNotResent"),
    ],
)
def test_retain_alone_exits_keep_the_not_applied_signal(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_error_class: str,
) -> None:
    """Every retain-alone exit must keep the F1 honesty contract, mirrored.

    Round-2 review finding: when a pair's sink half never becomes acceptable
    (cap exhaustion, shape-invalid sink, prose decline, or the hallucinated-
    tool gate after a rejected pair round), the retain applies alone — but the
    turn previously rendered ONLY the bare disposition with kind=assistant and
    audit SUCCESS, hiding that the requested output was never configured.
    """
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_2_sink"
    private_message = "Save results as jsonl with the private retain-alone-needle, and later add passthrough."
    rounds = 0

    async def responder(**_kwargs: object) -> SimpleNamespace:
        nonlocal rounds
        rounds += 1
        if scenario == "cap_exhaustion":
            return _pair_round(json.dumps(_PAIR_CONFIG_INVALID_SINK_ARGUMENTS), rounds)
        if scenario == "shape_invalid_sink":
            return _pair_round("{}", rounds)
        if rounds == 1:
            return _pair_round(json.dumps(_PAIR_CONFIG_INVALID_SINK_ARGUMENTS), rounds)
        if scenario == "prose_decline":
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="The path must live under outputs.", tool_calls=None))]
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(id="c_hx", function=SimpleNamespace(name="set_pipeline", arguments="{}"))],
                    )
                )
            ]
        )

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", responder)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "I couldn't apply the output configuration from that message, so your "
        "pipeline output is unchanged. Describe the output again and I'll rebuild it. "
        "I saved that instruction for the topology stage."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.target_stage == "topology"
    assert intent.catalog_name == "passthrough"
    assert intent.message_content_hash == stable_hash(private_message)
    # The output stage really is untouched.
    assert guided.reviewed_outputs == {}
    assert body["composition_state"]["outputs"] == []
    # The recorded chat-turn audit carries the scoped failure, not SUCCESS.
    audit_turn = _last_chat_turn_audit(client, session_id)
    assert audit_turn["status"] == ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE.value
    assert audit_turn["error_class"] == expected_error_class
    assert private_message not in _text_outside_chat_history(body)


def _inline_source_resolution() -> Step1SourceChatResolution:
    return Step1SourceChatResolution(
        assistant_message="Created the JSON rows as the source.",
        plugin="json",
        filename="rows.json",
        mime_type="application/json",
        content='[{"line": "alpha"}]',
        options={"schema": {"mode": "observed", "guaranteed_fields": ["line"]}},
        observed_columns=("line",),
        sample_rows=({"line": "alpha"},),
        on_validation_failure="discard",
    )


def test_pair_with_config_invalid_sink_prefill_still_retains_and_says_so(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink half fails the route's prefill re-check; the retain half must stay visible.

    Previously the failure copy displaced the whole composed message: the
    intent settled durably while the turn claimed "I didn't change your
    pipeline" (review finding 1).
    """
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_2_sink"
    private_message = "Save results as jsonl with the private pair-needle, and later add passthrough."
    invalid_sink = SinkResolved(
        outputs=(
            SinkOutputResolved(
                name="main",
                plugin="json",
                # flexible-without-fields fails the json sink's config model.
                options={"path": "out.jsonl", "schema": {"mode": "flexible"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        )
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _pair_sink_provider(invalid_sink, _action()))

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "I couldn't apply that output configuration because it fails the "
        "selected plugin's validation, so I didn't change your pipeline. "
        "Describe the output again and I'll rebuild it. "
        "I saved that instruction for the topology stage."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.target_stage == "topology"
    assert intent.message_content_hash == stable_hash(private_message)
    assert private_message not in _text_outside_chat_history(body)


def test_pair_with_rejected_transition_still_retains_and_says_so(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink transition is rejected after retention; the message must say both."""
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_2_sink"
    private_message = "Save results as jsonl with the private pair-needle, and later add passthrough."
    valid_sink = SinkResolved(
        outputs=(
            SinkOutputResolved(
                name="main",
                plugin="json",
                options={
                    "path": _respond_outputs_path(client, session_id, "pair_transition.jsonl"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        )
    )
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _pair_sink_provider(valid_sink, _action()))

    def _forced_rejection(*_args: object, **_kwargs: object) -> object:
        raise InvariantError("forced pair transition failure")

    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", _forced_rejection)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "I couldn't apply that configuration, so I didn't change your pipeline. "
        "Review the wizard fields and try again, or keep going with the wizard controls. "
        "I saved that instruction for the topology stage."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.target_stage == "topology"
    assert private_message not in _text_outside_chat_history(body)


def test_pair_with_storage_failed_inline_source_still_retains_and_says_so(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline source cannot be stored after retention; the message says both."""
    client = composer_test_client
    session_id = _create_session(client)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == "step_1_source"
    private_message = "Use these JSON rows with the private pair-needle, and later add passthrough."
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _pair_source_provider(_inline_source_resolution(), _action()),
    )

    async def _quota_exhausted(**_kwargs: object) -> object:
        raise BlobQuotaExceededError(session_id, current_bytes=1, limit_bytes=1)

    monkeypatch.setattr(guided_chat_atomic, "_step_1_inline_source_inspection_facts", _quota_exhausted)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "I could not store the generated source content because this "
        "session's storage quota is full. Remove an uploaded file or "
        "provide a smaller source, then try again. "
        "I saved that instruction for the topology stage."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.target_stage == "topology"
    assert private_message not in _text_outside_chat_history(body)


def test_guarded_schema_form_pair_keeps_not_applied_signal_and_retains(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisory-only schema form withholds the pair's source half.

    The composed turn must keep the guard's synthetic-failure status and
    error_class (review finding 2) while the retain half still settles and
    both facts stay visible in the message.
    """
    client = composer_test_client
    session_id = _create_session(client)
    TestStepChatCrossStep._seed_csv_blob(client, session_id)
    selected = TestStepChatCrossStep._respond(client, session_id, chosen=["csv"])
    assert selected["next_turn"]["type"] == "schema_form"
    private_message = "Use this CSV with the private pair-needle, and later add passthrough."

    async def pair_completion(**_kwargs: object) -> SimpleNamespace:
        tool_calls = [
            SimpleNamespace(
                id="c_source",
                function=SimpleNamespace(
                    name="resolve_source",
                    arguments=json.dumps(
                        {
                            "resolution": "source",
                            "plugin": "csv",
                            "filename": "rows.csv",
                            "mime_type": "text/csv",
                            "content": "text,note\nHello,world\n",
                            "options": {"schema": {"mode": "observed", "guaranteed_fields": ["text", "note"]}},
                            "observed_columns": ["text", "note"],
                            "sample_rows": [{"text": "Hello", "note": "world"}],
                            "assistant_message": "Created the CSV rows as the source.",
                        }
                    ),
                ),
            ),
            SimpleNamespace(
                id="c_retain",
                function=SimpleNamespace(
                    name="retain_deferred_intent",
                    arguments=json.dumps(
                        {
                            "target_stage": "topology",
                            "catalog_kind": "transform",
                            "catalog_name": "passthrough",
                            "redacted_summary": "Include the named transform during topology authoring.",
                            "constraints": [
                                {
                                    "kind": "component_count",
                                    "component_kind": "node",
                                    "plugin_kind": "transform",
                                    "plugin_name": "passthrough",
                                    "operator": "at_least",
                                    "count": 1,
                                }
                            ],
                        }
                    ),
                ),
            ),
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))])

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", pair_completion)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=selected["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "I did not apply generated source content. Review the current source form and "
        "submit it through the wizard controls. "
        "I saved that instruction for the topology stage."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.receiving_stage == "source"
    assert intent.target_stage == "topology"
    assert intent.catalog_name == "passthrough"
    assert intent.message_content_hash == stable_hash(private_message)
    # The wizard is untouched: the source form is still the live turn.
    assert body["next_turn"]["type"] == "schema_form"
    assert private_message not in _text_outside_chat_history(body)


@pytest.mark.parametrize(
    ("stage", "arguments"),
    [
        pytest.param("source", "{", id="source-invalid-json"),
        pytest.param("sink", 17, id="sink-non-string"),
        pytest.param("source", "9" * 5_000, id="source-integer-conversion-limit"),
        pytest.param("sink", "9" * 5_000, id="sink-integer-conversion-limit"),
        pytest.param("source", "[" * 10_000 + "]" * 10_000, id="source-json-recursion-limit"),
        pytest.param("sink", "[" * 10_000 + "]" * 10_000, id="sink-json-recursion-limit"),
    ],
)
def test_real_route_malformed_future_action_degrades_to_durable_clarification_retention(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    arguments: object,
) -> None:
    """Repair-exhausted retains keep the instruction as a clarification intent.

    The instruction is never discarded (R2-F15): a retain that stays malformed
    after its bounded repair turn is retained as a constraint-free
    clarification intent — unclaimable by the planner, visible and manageable
    by the user — with the raw prose confined to the private message row.
    """
    client = composer_test_client
    session_id = _create_session(client)
    if stage == "sink":
        TestStepChatCrossStep._seed_csv_blob(client, session_id)
        TestStepChatCrossStep._configure_csv_source(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == ("step_1_source" if stage == "source" else "step_2_sink")
    private_message = f"Later route the private customer-secret-needle through a transform from {stage}."
    clarification_message = (
        "I kept that future-stage instruction, but I couldn't verify its structure "
        "yet. Tell me the target stage and the concrete structural requirement — "
        "for example the plugin it must add or the connection it must produce — "
        "and I'll firm it up."
    )
    provider_calls = 0

    async def malformed_completion(**_kwargs: object) -> SimpleNamespace:
        nonlocal provider_calls
        provider_calls += 1
        tool_call = SimpleNamespace(
            id=f"call_retain_{provider_calls}",
            function=SimpleNamespace(name="retain_deferred_intent", arguments=arguments),
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])

    with client.app.state.session_engine.connect() as connection:
        state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()
    monkeypatch.setattr(chat_solver, "_litellm_acompletion", malformed_completion)
    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    response_json = response.json()
    # A malformed retain with a threadable tool call gets ONE bounded repair
    # turn before the failure is terminal; an un-threadable reply (non-string
    # arguments) stays single-shot.
    assert provider_calls == (2 if isinstance(arguments, str) else 1)
    assert response_json["assistant_message"] == clarification_message
    assert response_json["assistant_message_kind"] == "assistant"
    if before["composition_state"] is None:
        assert response_json["composition_state"]["sources"] == {}
        assert response_json["composition_state"]["nodes"] == []
        assert response_json["composition_state"]["edges"] == []
        assert response_json["composition_state"]["outputs"] == []
    else:
        for field_name in ("sources", "nodes", "edges", "outputs"):
            assert response_json["composition_state"][field_name] == before["composition_state"][field_name]
    assert private_message not in _text_outside_chat_history(response_json)
    guided = _guided(client, session_id)
    (intent,) = guided.deferred_intents
    assert intent.receiving_stage == ("source" if stage == "source" else "output")
    assert intent.target_stage == "wire_review"
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None
    assert intent.message_content_hash == stable_hash(private_message)
    assert private_message not in repr(intent.to_dict())
    # Transcript custody (R2-F15): the author's verbatim words survive in the
    # rendered transcript even when the retain FAILS all repairs.
    assert guided.chat_history[-2].content == private_message
    assert all("[Future-stage instruction submitted privately.]" not in turn.content for turn in guided.chat_history)
    assert guided.chat_history[-1].content == clarification_message
    assert len(guided.chat_history) == len(before["guided_session"]["chat_history"]) + 2
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert [content for _message_id, content in _non_root_user_rows(client, session_id)] == [private_message]
    assert all(private_message not in message.content for message in messages if message.role != "user")
    assert all(private_message not in repr(message.tool_calls) for message in messages if message.role != "user")
    with client.app.state.session_engine.connect() as connection:
        state_count_after = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()
    assert state_count_after == state_count_before + 1


@pytest.mark.parametrize("stage", ["source", "sink"])
def test_real_route_unproven_option_literal_retains_clarification_debt_without_leaking_private_prose(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    if stage == "sink":
        TestStepChatCrossStep._seed_csv_blob(client, session_id)
        TestStepChatCrossStep._configure_csv_source(client, session_id)
    private_message = "Send the full private customer-secret sentence to an arbitrary prompt option."
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary=private_message,
        constraints=(
            OptionValueConstraint(
                kind="option_value",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="55555555-5555-4555-8555-555555555555",
                    plugin_kind="transform",
                    plugin_name="passthrough",
                ),
                option_path=("prompt",),
                operator="equals",
                value=private_message,
            ),
        ),
    )
    catalog, snapshot = _policy_context(
        (("transform", "passthrough"),),
        available=frozenset({PluginId("transform", "passthrough")}),
        schemas={
            ("transform", "passthrough"): {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
            }
        },
    )
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(action))
    before = client.get(f"/api/sessions/{session_id}/guided").json()
    assert before["guided_session"]["step"] == ("step_1_source" if stage == "source" else "step_2_sink")
    with client.app.state.session_engine.connect() as connection:
        state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=before["next_turn"]["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    response_json = response.json()
    assert response_json["assistant_message"] == (
        "I kept your instruction as a pending clarification instead of applying it. "
        "I couldn't verify its structural details against your message. "
        "Restate the concrete structural requirement and I'll firm it up."
    )
    assert private_message not in _text_outside_chat_history(response_json)
    guided = _guided(client, session_id)
    (retained_intent,) = guided.deferred_intents
    assert retained_intent.constraints == ()
    assert retained_intent.catalog_kind is None
    assert private_message not in repr(retained_intent)
    assert guided.chat_history[-2].content == private_message
    assert all("[Future-stage instruction submitted privately.]" not in turn.content for turn in guided.chat_history)
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert [content for _message_id, content in _non_root_user_rows(client, session_id)] == [private_message]
    assert all(private_message not in message.content for message in messages if message.role != "user")
    assert all(private_message not in repr(message.tool_calls) for message in messages if message.role != "user")
    with client.app.state.session_engine.connect() as connection:
        state_count_after = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()
    assert state_count_after == state_count_before + 1


@pytest.mark.parametrize("stage", ["source", "sink"])
def test_exact_policy_denial_wins_over_same_name_visible_in_another_kind_at_each_guided_stage(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    if stage == "sink":
        TestStepChatCrossStep._seed_csv_blob(client, session_id)
        TestStepChatCrossStep._configure_csv_source(client, session_id)
    private_message = "Privately remember the unavailable llm transform for topology."
    catalog, snapshot = _policy_context(
        (("source", "llm"), ("transform", "llm")),
        available=frozenset({PluginId("source", "llm")}),
    )
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action(catalog_name="llm")))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message"] == (
        "The transform plugin 'llm' is not enabled by the current policy. No policy-visible transform alternatives are available."
    )
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert private_message not in _text_outside_chat_history(response.json())
    guided = _guided(client, session_id)
    assert guided.deferred_intents == ()
    assert guided.chat_history[-2].content == private_message
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert [content for _message_id, content in _non_root_user_rows(client, session_id)] == [private_message]
    assert all(private_message not in message.content for message in messages if message.role != "user")


@pytest.mark.parametrize("option_schema", [True, False])
def test_boolean_property_schema_retains_constraint_free_clarification_debt(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    option_schema: bool,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    private_message = "Privately retain a literal only if catalog authority proves it closed."
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Retain one closed prompt value.",
        constraints=(
            OptionValueConstraint(
                kind="option_value",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="55555555-5555-4555-8555-555555555555",
                    plugin_kind="transform",
                    plugin_name="passthrough",
                ),
                option_path=("prompt",),
                operator="equals",
                value="safe",
            ),
        ),
    )
    catalog, snapshot = _policy_context(
        (("transform", "passthrough"),),
        available=frozenset({PluginId("transform", "passthrough")}),
        schemas={("transform", "passthrough"): {"type": "object", "properties": {"prompt": option_schema}}},
    )
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(action))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message=private_message,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["assistant_message"] == (
        "I kept your instruction as a pending clarification instead of applying it. "
        "I couldn't verify its structural details against your message. "
        "Restate the concrete structural requirement and I'll firm it up."
    )
    assert private_message not in _text_outside_chat_history(response.json())
    guided = _guided(client, session_id)
    (retained_intent,) = guided.deferred_intents
    assert retained_intent.constraints == ()
    assert private_message not in repr(retained_intent)
    assert guided.chat_history[-2].content == private_message


@pytest.mark.parametrize(
    ("schema_info", "option_value"),
    [
        (
            PluginSchemaInfo(
                name="wrong-plugin",
                plugin_type="transform",
                description="corrupt identity",
                json_schema={"type": "object", "properties": {"prompt": {"enum": ["safe"]}}},
                knob_schema={"fields": []},
            ),
            "safe",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="dangling ref",
                json_schema={"type": "object", "properties": {"prompt": {"$ref": "#/$defs/Missing"}}},
                knob_schema={"fields": []},
            ),
            "safe",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="present null property schema",
                json_schema={"type": "object", "properties": {"prompt": None}},
                knob_schema={"fields": []},
            ),
            "safe",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="malformed restrictive schema",
                json_schema={"type": "object", "properties": {"prompt": {"enum": ["safe"], "not": None}}},
                knob_schema={"fields": []},
            ),
            "safe",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="dangling restrictive ref",
                json_schema={
                    "type": "object",
                    "properties": {"prompt": {"enum": ["safe"], "not": {"$ref": "#/$defs/Missing"}}},
                },
                knob_schema={"fields": []},
            ),
            "other",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="dynamic ref with in-domain value",
                json_schema={
                    "type": "object",
                    "properties": {"prompt": {"enum": ["safe"], "not": {"$dynamicRef": "#missing"}}},
                },
                knob_schema={"fields": []},
            ),
            "safe",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="dynamic ref with out-of-domain value",
                json_schema={
                    "type": "object",
                    "properties": {"prompt": {"enum": ["safe"], "not": {"$dynamicRef": "#missing"}}},
                },
                knob_schema={"fields": []},
            ),
            "other",
        ),
        (
            PluginSchemaInfo(
                name="passthrough",
                plugin_type="transform",
                description="nested resource dangling ref",
                json_schema={
                    "$defs": {"Mode": {"enum": ["root-safe"]}},
                    "type": "object",
                    "properties": {"prompt": {"$id": "outer", "$ref": "#/$defs/Mode"}},
                },
                knob_schema={"fields": []},
            ),
            "root-safe",
        ),
    ],
    ids=(
        "schema-identity-mismatch",
        "dangling-local-ref",
        "present-null-property",
        "malformed-restriction",
        "dangling-restriction-before-membership",
        "dynamic-ref-in-domain",
        "dynamic-ref-out-of-domain",
        "nested-resource-dangling-ref",
    ),
)
def test_catalog_schema_authority_corruption_fails_operation_without_publishing_repair_or_cohort(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    schema_info: PluginSchemaInfo,
    option_value: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    private_message = "Private future prompt text must never enter a repair response."
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="passthrough",
        redacted_summary="Retain one closed prompt value.",
        constraints=(
            OptionValueConstraint(
                kind="option_value",
                subject=PluginSubject(
                    kind="plugin",
                    subject_id="55555555-5555-4555-8555-555555555555",
                    plugin_kind="transform",
                    plugin_name="passthrough",
                ),
                option_path=("prompt",),
                operator="equals",
                value=option_value,
            ),
        ),
    )
    catalog, snapshot = _policy_context(
        (("transform", "passthrough"),),
        available=frozenset({PluginId("transform", "passthrough")}),
        schema_overrides={("transform", "passthrough"): schema_info},
    )
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(action))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    operation_id = str(uuid4())
    with client.app.state.session_engine.connect() as connection:
        states_before = connection.execute(
            select(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).all()
        messages_before = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)).all()

    response = _post(
        client,
        session_id,
        operation_id=operation_id,
        turn_token=turn["turn_token"],
        message=private_message,
    )

    assert response.status_code == 500, response.json()
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    assert "couldn't safely retain" not in response.text
    assert private_message not in response.text
    with client.app.state.session_engine.connect() as connection:
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == session_id)).all()
        messages = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)).all()
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one()
        )
    assert states == states_before
    assert messages == messages_before
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "integrity_error"
    assert operation["originating_message_id"] is None
    assert operation["result_state_id"] is None


@pytest.mark.parametrize(
    ("action", "expected_fragment", "installed", "available"),
    [
        (_action(catalog_name="not_installed_plugin"), "is not installed", (), frozenset()),
        (
            _action(catalog_name="llm"),
            "is not enabled by the current policy",
            (("transform", "llm"),),
            frozenset(),
        ),
        (
            _action(target_stage="source", catalog_kind="source", catalog_name="csv"),
            "couldn't safely retain",
            (("source", "csv"),),
            frozenset({PluginId("source", "csv")}),
        ),
    ],
)
def test_absence_policy_denial_and_current_target_clarify_without_mutation(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    action: DeferredIntentAction,
    expected_fragment: str,
    installed: tuple[tuple[PluginKind, str], ...],
    available: frozenset[PluginId],
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    catalog, snapshot = _policy_context(installed, available=available)
    monkeypatch.setattr(guided_chat_atomic, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    monkeypatch.setattr(guided_route, "_request_plugin_policy_context", lambda _request, _user: (catalog, snapshot))
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(action))

    response = _post(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn["turn_token"],
        message=f"private wrong-stage detail naming {action.catalog_name}",
    )

    assert response.status_code == 200, response.json()
    assert expected_fragment in response.json()["assistant_message"]
    assert _guided(client, session_id).deferred_intents == ()


@pytest.mark.parametrize(
    "fault_point",
    ("state_insert", "message_insert", "audit_insert", "operation_bind", "operation_complete", "operation_event"),
)
def test_fault_at_each_settlement_boundary_rolls_back_business_state_but_persists_failure_audit(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    operation_id = str(uuid4())
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action()))
    engine = client.app.state.session_engine
    with engine.connect() as connection:
        state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
        ).scalar_one()
        messages_before = _session_message_rows(connection, session_id)
    armed = True
    writes: list[str] = []
    chat_insert_count = 0

    def inject_fault(_conn, _cursor, statement, _parameters, context, _executemany):
        nonlocal armed, chat_insert_count
        if not armed:
            return
        normalized = " ".join(statement.lower().split())
        target_table = _dml_target_table(context)
        label: str | None = None
        if target_table is composition_states_table:
            label = "state_insert"
        elif target_table is chat_messages_table:
            chat_insert_count += 1
            label = "message_insert" if chat_insert_count == 1 else "audit_insert"
        elif normalized.startswith("update guided_operations") and " set originating_message_id" in normalized:
            label = "operation_bind"
        elif normalized.startswith("update guided_operations") and " set status" in normalized:
            label = "operation_complete"
        elif normalized.startswith("insert into guided_operation_events") and "operation_complete" in writes:
            label = "operation_event"
        if label is None:
            return
        writes.append(label)
        if label == fault_point:
            armed = False
            raise RuntimeError(f"injected {fault_point}")

    event.listen(engine, "before_cursor_execute", inject_fault)
    try:
        response = _post(
            client,
            session_id,
            operation_id=operation_id,
            turn_token=turn["turn_token"],
            message="private message must roll back",
        )
    finally:
        event.remove(engine, "before_cursor_execute", inject_fault)

    assert response.status_code == 500, response.json()
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == session_id)
            ).scalar_one()
            == state_count_before
        )
        messages_after = _session_message_rows(connection, session_id)
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one()
        )
        operation_events = (
            connection.execute(
                select(guided_operation_events_table.c.event_kind)
                .where(guided_operation_events_table.c.operation_id == operation_id)
                .order_by(guided_operation_events_table.c.sequence)
            )
            .scalars()
            .all()
        )
    assert operation["status"] == "failed"
    _assert_only_failure_chat_turn_audit_added(messages_before, messages_after, operation_id=operation_id)
    assert operation["originating_message_id"] is None
    assert operation["result_state_id"] is None
    assert operation["response_hash"] is None
    assert operation_events == ["claimed", "renewed", "failed"]
    assert fault_point in writes


@pytest.mark.parametrize("corruption", ("cross_session_message", "message_hash_drift"))
def test_corrupt_origin_custody_is_an_integrity_failure_and_rolls_back_atomically(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    client = composer_test_client
    target_session_id = _create_session(client)
    other_session_id = _create_session(client)
    private_message = "private future transform instruction"
    other_message = asyncio.run(
        client.app.state.session_service.add_message(
            UUID(other_session_id),
            "user",
            private_message,
            writer_principal="route_user_message",
        )
    )
    with client.app.state.session_engine.connect() as connection:
        target_state_count_before = connection.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == target_session_id)
        ).scalar_one()
        target_messages_before = _session_message_rows(connection, target_session_id)
        other_messages_before = _session_message_rows(connection, other_session_id)
    turn = client.get(f"/api/sessions/{target_session_id}/guided").json()["next_turn"]
    operation_id = str(uuid4())
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _provider(_action()))
    service = client.app.state.session_service
    real_settle = service.settle_guided_state_operation

    async def corrupt_command(command, *, payload_store=None):
        metadata = deep_thaw(command.state.composer_meta)
        guided = GuidedSession.from_dict(metadata["guided_session"])
        (intent,) = guided.deferred_intents
        if corruption == "cross_session_message":
            corrupted_intent = replace(
                intent,
                originating_message_id=str(other_message.id),
                message_content_hash=stable_hash(private_message),
            )
            originating = GuidedOriginatingUserMessageDraft(message_id=other_message.id, content=private_message)
        else:
            corrupted_intent = replace(intent, message_content_hash=stable_hash("drifted content"))
            originating = command.originating_message
        metadata["guided_session"] = replace(guided, deferred_intents=(corrupted_intent,)).to_dict()
        corrupted_state = replace(command.state, composer_meta=metadata)
        return await real_settle(
            replace(command, state=corrupted_state, originating_message=originating),
            payload_store=payload_store,
        )

    monkeypatch.setattr(service, "settle_guided_state_operation", corrupt_command)
    response = _post(
        client,
        target_session_id,
        operation_id=operation_id,
        turn_token=turn["turn_token"],
        message=private_message,
    )

    assert response.status_code == 500, response.json()
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    with client.app.state.session_engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == target_session_id)
            ).scalar_one()
            == target_state_count_before
        )
        target_messages_after = _session_message_rows(connection, target_session_id)
        other_messages_after = _session_message_rows(connection, other_session_id)
    _assert_only_failure_chat_turn_audit_added(
        target_messages_before,
        target_messages_after,
        operation_id=operation_id,
    )
    assert other_messages_after == other_messages_before
