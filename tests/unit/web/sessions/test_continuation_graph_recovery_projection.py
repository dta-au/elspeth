"""Actual recovery SQL postconditions; these checks grant no paid Stop authority."""

from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.no_tool_policy import compose_graph_recovery_refusal_message
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.sessions.composer_operations import ComposerOperationAssistantWrite
from elspeth.web.sessions.composer_turn import _graph_recovery_response
from elspeth.web.sessions.continuation_graph_recovery_projection import (
    GraphRecoveryProjectionInput,
    require_graph_recovery_postconditions_on_connection,
)
from elspeth.web.sessions.graph_recovery import empty_recovery_state
from elspeth.web.sessions.models import chat_messages_table, composition_graph_parks_table, composition_states_table
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.unit.web.coordination.test_graph_recovery_persistence import begin

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


async def _prepared(store, action):
    running, admission = await begin(store)
    intent = GraphRecoveryIntent(
        action,
        "Let's discuss the recovered draft.",
        str(uuid4()) if action == "restore_parked_graph" else None,
        "recovery-projection-call",
    )
    original = ComposerResult(message=intent.message, state=empty_recovery_state(), graph_recovery_intent=intent)
    recovery = await store[3].prepare_graph_recovery(
        store[4],
        context=admission.context,
        intent=intent,
        session_operation_context=running.session_operation_context,
        candidate_state=original.state if action == "park" else None,
    )
    return running, original, GraphRecoveryProjectionInput(original, admission.context, recovery)


async def _complete(store, running, projection):
    recovery = projection.recovery
    return await store[3].complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(
            message_id=uuid4(),
            content=recovery.intent.message if recovery.failure_reason is None else compose_graph_recovery_refusal_message(),
            raw_content=None if recovery.failure_reason is None else "",
            composition_state_id=recovery.expected_state_id,
        ),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        recovery=recovery,
        build_response=_graph_recovery_response,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset_to_safe_checkpoint", "park", "restore_parked_graph"])
async def test_recovery_projection_replays_actual_terminal_rows_and_refuses_foreign_or_changed_data(operation_store, action):
    engine, repository, _authority, service, _sid = operation_store
    running, original, projection = await _prepared(operation_store, action)
    terminal = await _complete(operation_store, running, projection)
    assert terminal.status == "completed" and terminal.result_json is not None
    outcome = MessageWithStateResponse.model_validate_json(terminal.result_json)
    assert (outcome.state is None) == (action == "restore_parked_graph")
    assert outcome.recovery_notice is not None
    with engine.connect() as conn:
        row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == outcome.message.id)).one()
        assistant = service._row_to_chat_message_record(row)
        require_graph_recovery_postconditions_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=original,
            assistant=assistant,
            outcome=outcome,
            projection=projection,
        )
        foreign = replace(projection, recovery=replace(projection.recovery, session_id=uuid4()))
        with pytest.raises(AuditIntegrityError, match="original operation"):
            require_graph_recovery_postconditions_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=assistant,
                outcome=outcome,
                projection=foreign,
            )
        with pytest.raises(AuditIntegrityError):
            require_graph_recovery_postconditions_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=replace(assistant, raw_content="substituted"),
                outcome=outcome,
                projection=projection,
            )
        with pytest.raises(AuditIntegrityError):
            require_graph_recovery_postconditions_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=assistant,
                outcome=outcome.model_copy(update={"recovery_notice": None}),
                projection=projection,
            )
    # Redirect DATA to a real persisted row that is not the original receipt.
    # Append-only message protection remains enabled throughout this control.
    wrong_receipt = replace(projection, recovery=replace(projection.recovery, receipt_id=assistant.id))
    with engine.connect() as conn, pytest.raises(AuditIntegrityError, match="actual receipt pair"):
        require_graph_recovery_postconditions_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=original,
            assistant=assistant,
            outcome=outcome,
            projection=wrong_receipt,
        )
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_recovery_postcondition_failure_rolls_back_real_terminal_writes(operation_store, monkeypatch):
    from elspeth.web.sessions import service as service_module

    engine, repository, authority, service, sid = operation_store
    running, original, projection = await _prepared(operation_store, "park")
    actual_insert = service_module.SessionServiceImpl._insert_chat_message
    original_errors = []

    def insert_wrong_receipt(self, conn, **kwargs):
        if kwargs.get("message_id") == str(projection.recovery.receipt_id):
            kwargs["raw_content"] = "forged"
        return actual_insert(self, conn, **kwargs)

    def inspect_terminal(conn, selected_running, **kwargs):
        outcome = kwargs["outcome"]
        row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == outcome.message.id)).one()
        assistant = service._row_to_chat_message_record(row)
        # The ordinary recovery writer really ran, including park and receipt.
        assert conn.execute(select(func.count()).select_from(composition_graph_parks_table)).scalar_one() == 1
        try:
            require_graph_recovery_postconditions_on_connection(
                conn,
                selected_running,
                service,
                original_result=original,
                publication_result=original,
                assistant=assistant,
                outcome=outcome,
                projection=projection,
            )
        except AuditIntegrityError as error:
            original_errors.append(error)
            raise
        pytest.fail("The actual SQL checker accepted a forged receipt")

    monkeypatch.setattr(service_module.SessionServiceImpl, "_insert_chat_message", insert_wrong_receipt)
    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", inspect_terminal)
    with pytest.raises(ExceptionGroup, match="terminal SQL attempts failed") as raised:
        await _complete(operation_store, running, projection)
    assert len(original_errors) == 2
    assert raised.value.exceptions == tuple(original_errors)
    assert all(str(error) == "graph recovery projection changed its actual receipt or public response" for error in original_errors)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(composition_graph_parks_table)).scalar_one() == 0
        assert (
            conn.execute(
                select(func.count())
                .select_from(chat_messages_table)
                .where(chat_messages_table.c.role.in_(("assistant", "system", "audit")))
            ).scalar_one()
            == 0
        )
    current = authority.get(session_id=sid, operation_id=running.claim.operation_id)
    assert current is not None and current.status == "running"
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_paid_recovery_stop_rolls_back_corrupt_receipt_at_actual_terminal_sink(tmp_path, monkeypatch):
    from elspeth.web.sessions import service as service_module
    from tests.helpers.composer_operations import message_body
    from tests.helpers.continuation_app import submit_and_settle
    from tests.unit.web.sessions.test_continuation_semantic_owned_publication import actual_owned_recovery_case

    actual_settle = service_module.settle_composer_operation_on_connection
    actual_write_receipt = service_module.SessionServiceImpl._write_graph_recovery_receipt
    actual_insert = service_module.SessionServiceImpl._insert_chat_message
    receipt_ids = set()
    observed = []
    original_errors = []

    def write_receipt(self, conn, *, recovery, context, created_at):
        receipt_ids.add(str(recovery.receipt_id))
        return actual_write_receipt(self, conn, recovery=recovery, context=context, created_at=created_at)

    def insert_wrong_receipt(self, conn, **kwargs):
        if kwargs.get("message_id") in receipt_ids:
            kwargs["content"] = "This is not the original recovery receipt."
        return actual_insert(self, conn, **kwargs)

    def corrupt_owned_receipt(conn, running, **kwargs):
        projection = kwargs.get("publication_projection")
        if type(projection) is GraphRecoveryProjectionInput:
            recovery = projection.recovery
            assert recovery.failure_reason is None and recovery.park_id is not None
            assert (
                conn.execute(select(chat_messages_table.c.content).where(chat_messages_table.c.id == str(recovery.receipt_id))).scalar_one()
                == "This is not the original recovery receipt."
            )
            observed.append(recovery)
        try:
            return actual_settle(conn, running, **kwargs)
        except AuditIntegrityError as error:
            original_errors.append(error)
            raise

    monkeypatch.setattr(service_module.SessionServiceImpl, "_write_graph_recovery_receipt", write_receipt)
    monkeypatch.setattr(service_module.SessionServiceImpl, "_insert_chat_message", insert_wrong_receipt)
    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", corrupt_owned_receipt)
    async with actual_owned_recovery_case(tmp_path, monkeypatch) as case:
        harness, sdk, operation_id, _reply, _arguments, _inputs = case
        snapshot = await submit_and_settle(
            harness,
            message_body("Name the draft 'Named recovery draft', then park it and finish this request.", operation_id=operation_id),
        )
        assert len(observed) == 1, (snapshot.error, sdk.roles)
        assert len(original_errors) == 1
        assert str(original_errors[0]) == "graph recovery projection changed its actual receipt or public response"
        assert snapshot.status == "failed", snapshot
        assert sdk.roles == ["primary", "primary", "semantic"] and not sdk.steps
        recovery = observed[0]
        with harness.app.state.session_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.id == str(recovery.receipt_id))
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(composition_graph_parks_table)
                    .where(composition_graph_parks_table.c.id == str(recovery.park_id))
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    select(func.max(composition_states_table.c.version)).where(
                        composition_states_table.c.session_id == str(harness.session_id)
                    )
                ).scalar_one()
                == recovery.expected_version
            )


@pytest.mark.asyncio
async def test_existing_valid_head_park_preserves_complete_state_on_same_transaction(operation_store, monkeypatch):
    from elspeth.web.coordination.composer_operation_authority import (
        _require_graph_recovery_prior_read,
        graph_recovery_prior_read_scope,
        read_graph_recovery_prior_state_on_connection,
    )
    from elspeth.web.sessions.graph_recovery import checkpoint_data
    from elspeth.web.sessions.service import SessionServiceImpl

    engine, repository, _authority, service, sid = operation_store
    running, admission = await begin(operation_store)
    saved = await service.save_composition_state(
        sid,
        replace(checkpoint_data(empty_recovery_state()), is_valid=True),
        provenance="tool_call",
        session_operation_context=running.session_operation_context,
    )
    intent = GraphRecoveryIntent("park", "Keep this exact draft parked.", None, "park-existing")
    original = ComposerResult(message=intent.message, state=empty_recovery_state(), graph_recovery_intent=intent)
    recovery = await service.prepare_graph_recovery(
        sid,
        context=admission.context,
        intent=intent,
        session_operation_context=running.session_operation_context,
        candidate_state=original.state,
    )
    assert recovery.state_id == recovery.expected_state_id == saved.id
    projection = GraphRecoveryProjectionInput(original, admission.context, recovery)
    with engine.connect() as conn:
        prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
        assert prior is not None
        _require_graph_recovery_prior_read(conn, running, prior)
        with pytest.raises(AuditIntegrityError, match="actual pre-DML transaction read"):
            _require_graph_recovery_prior_read(conn, running, prior)
        prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
        assert prior is not None
        with pytest.raises(AuditIntegrityError, match="actual pre-DML transaction read"):
            _require_graph_recovery_prior_read(conn, running, replace(prior))
        prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
        assert prior is not None
        with pytest.raises(FrozenInstanceError):
            prior.is_valid = False
        with pytest.raises(TypeError):
            prior.metadata_["name"] = "changed alias"
        _require_graph_recovery_prior_read(conn, running, prior)
        foreign_operation = replace(running, claim=replace(running.claim, operation_id=str(uuid4())))
        foreign_actor = replace(
            running,
            session_operation_context=replace(
                running.session_operation_context,
                fence=replace(running.session_operation_context.fence, lease_token="foreign-actor"),
            ),
        )
        for foreign in (foreign_operation, foreign_actor):
            prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
            with pytest.raises(AuditIntegrityError, match="actual pre-DML transaction read"):
                _require_graph_recovery_prior_read(conn, foreign, prior)
        with pytest.raises(RuntimeError, match="failed terminal attempt"), graph_recovery_prior_read_scope(conn):
            prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
            raise RuntimeError("failed terminal attempt")
        with pytest.raises(AuditIntegrityError, match="actual pre-DML transaction read"):
            _require_graph_recovery_prior_read(conn, running, prior)
        prior = read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
        conn.rollback()
        with conn.begin(), pytest.raises(AuditIntegrityError, match="actual pre-DML transaction read"):
            _require_graph_recovery_prior_read(conn, running, prior)
    actual_settle = SessionServiceImpl._settle_graph_recovery_on_connection
    captured = []

    def capture_before_writes(self, conn, **kwargs):
        prior = read_graph_recovery_prior_state_on_connection(conn, running, service=self, recovery=kwargs["recovery"])
        assert prior is not None and prior.is_valid
        captured.append(prior)
        return actual_settle(self, conn, **kwargs)

    monkeypatch.setattr(SessionServiceImpl, "_settle_graph_recovery_on_connection", capture_before_writes)
    terminal = await _complete(operation_store, running, projection)
    assert terminal.status == "completed" and terminal.result_json is not None
    assert len(captured) == 1
    outcome = MessageWithStateResponse.model_validate_json(terminal.result_json)
    with engine.connect() as conn:
        with pytest.raises(AuditIntegrityError, match="after its terminal writes"):
            read_graph_recovery_prior_state_on_connection(conn, running, service=service, recovery=recovery)
        assistant = service._row_to_chat_message_record(
            conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == outcome.message.id)).one()
        )
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1
        require_graph_recovery_postconditions_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=original,
            assistant=assistant,
            outcome=outcome,
            projection=projection,
            recovery_prior_state=captured[0],
        )
        for prior in (None, replace(captured[0], is_valid=False), replace(captured[0], id=uuid4())):
            with pytest.raises(AuditIntegrityError, match="complete original state"):
                require_graph_recovery_postconditions_on_connection(
                    conn,
                    running,
                    service,
                    original_result=original,
                    publication_result=original,
                    assistant=assistant,
                    outcome=outcome,
                    projection=projection,
                    recovery_prior_state=prior,
                )
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_restore_actual_park_creates_bound_checkpoint_and_public_response(operation_store):
    engine, repository, _authority, service, sid = operation_store
    parked_running, _parked_original, parked = await _prepared(operation_store, "park")
    parked_terminal = await _complete(operation_store, parked_running, parked)
    assert parked_terminal.status == "completed"
    repository.release(parked_running.session_operation_context)
    running, admission = await begin(operation_store, state_id=parked.recovery.state_id)
    intent = GraphRecoveryIntent(
        "restore_parked_graph", "Restore the exact parked draft.", str(parked.recovery.park_id), "restore-existing"
    )
    original = ComposerResult(message=intent.message, state=empty_recovery_state(), graph_recovery_intent=intent)
    recovery = await service.prepare_graph_recovery(
        sid,
        context=admission.context,
        intent=intent,
        session_operation_context=running.session_operation_context,
    )
    assert recovery.failure_reason is None and recovery.target_state_id == parked.recovery.state_id
    assert recovery.state_id != recovery.expected_state_id
    projection = GraphRecoveryProjectionInput(original, admission.context, recovery)
    terminal = await _complete(operation_store, running, projection)
    assert terminal.status == "completed" and terminal.result_json is not None
    outcome = MessageWithStateResponse.model_validate_json(terminal.result_json)
    assert outcome.state is not None and outcome.message.content == intent.message
    with engine.connect() as conn:
        assistant = service._row_to_chat_message_record(
            conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == outcome.message.id)).one()
        )
        require_graph_recovery_postconditions_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=original,
            assistant=assistant,
            outcome=outcome,
            projection=projection,
        )
        wrong_lineage = replace(projection, recovery=replace(recovery, target_state_id=uuid4()))
        with pytest.raises(AuditIntegrityError, match="receipt or public response"):
            require_graph_recovery_postconditions_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=assistant,
                outcome=outcome,
                projection=wrong_lineage,
            )
    repository.release(running.session_operation_context)
