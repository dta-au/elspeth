"""Real inserted-source reads and original title constructor; no title dispatch."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import event, literal, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY, PromptGuardTrust
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_title_input import load_current_title_input
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.titles import format_default_session_title
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_ingress import begin, ingress, recovery_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = operations.operation_store


async def inserted_case(store, *, eligible=True):
    with store[0].begin() as conn:
        title = format_default_session_title(datetime(2026, 10, 24, tzinfo=UTC)) if eligible else "Operator Supplied Name"
        assert conn.execute(update(sessions_table).where(sessions_table.c.id == str(store[4])).values(title=title)).rowcount == 1
    running, work, selected, request, _seed = begin(store)
    completed = await ingress(store, running, work, selected, request)
    work.coordinator.assert_completed()
    service, _actors = current_service(store)
    return running, completed, service


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", (True, False))
async def test_current_inserted_title_source_preserves_real_values_without_writes(operation_store, eligible):
    running, completed, service = await inserted_case(operation_store, eligible=eligible)
    work = inventory_work(running)
    try:
        before = recovery_rows(operation_store[0])
        result = await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        source = result.source
        assert source.selected is completed.snapshot
        assert source.records == completed.transcript and source.records[-1].id == completed.message.id
        assert source.request.content == completed.message.content and source.actor_user_id == "alice"
        assert source.recovery.context.malformed_head is False and source.recovery.context.turn_start_state_id is None
        assert source.initial_state.metadata.name == "Untitled Pipeline" and source.initial_state.version == 1
        assert source.eligible is eligible
        if eligible:
            assert result.request is not None and result.request.kwargs["messages"] is result.request.audit_messages
            assert result.request.audit_messages[0][PROMPT_GUARD_TRUST_KEY] is PromptGuardTrust.INSTRUCTION
            assert result.request.kwargs["num_retries"] == result.request.kwargs["max_retries"] == 0
            assert result.request.kwargs["timeout"] == 5.0 and result.request.kwargs["max_tokens"] == 40
            assert completed.message.content in result.request.audit_messages[1]["content"]
        else:
            assert result.request is None
        root = decode_continuation_snapshot(completed.snapshot.payload_json)
        assert root.frames[0].ingress_phase == "inserted" and root.frames[0].bootstrap is None
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        selected = SelectedContinuationState(completed.snapshot.checkpoint_id, root, ())
        assert next_continuation_action_policy(selected=selected, policy=policy) == policy.actions.session_sql
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_source_requires_same_complete_current_root_and_original_title(operation_store):
    running, completed, service = await inserted_case(operation_store)
    work = inventory_work(running)
    try:
        assert (await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)).source.eligible
        root = decode_continuation_snapshot(completed.snapshot.payload_json)
        substituted = root.model_copy(update={"logical": root.logical.model_copy(update={"request_hash": "f" * 64})})
        altered = replace(completed.snapshot, payload_json=encode_continuation_snapshot(substituted))
        untouched = recovery_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict, match="selected CAS"):
            await load_current_title_input(service=service, selected=altered, required_work=work)
        assert recovery_rows(operation_store[0]) == untouched
        with operation_store[0].begin() as conn:
            assert (
                conn.execute(
                    update(sessions_table).where(sessions_table.c.id == str(operation_store[4])).values(title="Changed By Current Owner")
                ).rowcount
                == 1
            )
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="changed session title"):
            await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_source_refuses_reserved_phase_without_promoting_its_input(operation_store):
    running, work, selected, _request, _seed = begin(operation_store)
    service, _actors = current_service(operation_store)
    before = recovery_rows(operation_store[0])
    try:
        with pytest.raises(AuditIntegrityError, match="closed inserted initializer"):
            await load_current_title_input(service=service, selected=selected, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_title_source_new_claim_uses_same_ingress_and_refuses_old_claim(operation_store):
    running, completed, service = await inserted_case(operation_store)
    work = inventory_work(running)
    before = await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
    work.coordinator.assert_completed()
    operation_store[1].release(running.session_operation_context)
    current = new_owners(operation_store)
    engine, repository, authority, _sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    current_running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    work = inventory_work(current_running)
    service_now, _actors = current_service(current)
    try:
        selected = authority.load_continuation_snapshot(current_running)
        untouched = recovery_rows(engine)
        after = await load_current_title_input(service=service_now, selected=selected, required_work=work)
        assert after.source.records == before.source.records and after.source.request == before.source.request
        assert after.source.eligible is True and context.fence != running.session_operation_context.fence
        assert recovery_rows(engine) == untouched
        old_work = inventory_work(running)
        try:
            with pytest.raises((AuditIntegrityError, SessionOperationFenceLost, ComposerOperationFenceLost, ContinuationCASConflict)):
                await load_current_title_input(service=service, selected=completed.snapshot, required_work=old_work)
        finally:
            old_work.coordinator.assert_completed()
        assert recovery_rows(engine) == untouched
        work.coordinator.assert_completed()
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_title_source_refuses_actual_prior_quota_attempt_without_retry_or_effect_inference(operation_store):
    running, completed, service = await inserted_case(operation_store)
    work = inventory_work(running)
    custody = None
    try:
        assert (await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)).source.eligible
        with operation_store[0].begin() as conn:
            grant_test_pipeline_user(conn, identity_id="alice")
            seed_token_policies(conn, identity_id="alice")
        owner = ProviderInvocationOwner(service=operation_store[3], required_work=work)
        custody = owner.mint(ProviderInvocationFamily.TITLE)
        await custody.admit(model="test/model")
        assert custody.attempt is not None
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="prior provider/effect work"):
            await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        assert recovery_rows(operation_store[0]) == before
    finally:
        if custody is not None and custody.attempt is not None:
            assert await custody.cancel_before_sdk(model="test/model") == ()
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_original_async_graph_admission_and_same_connection_source_agree(operation_store):
    running, completed, service = await inserted_case(operation_store)
    work = inventory_work(running)
    try:
        original = await operation_store[3].load_graph_recovery_context(
            operation_store[4], turn_start_state_id=None, session_operation_context=running.session_operation_context
        )
        actual = await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        assert actual.source.recovery == original
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


def test_extracted_title_request_preserves_shared_guard_sampling_and_endpoint_values():
    prepared = _auto_title.prepare_auto_title_request(
        user_message="Use café records",
        model="test/model",
        temperature=0.2,
        seed=19,
        api_base="https://model.example.test/v1",
        api_key="fixture-key",
    )
    assert prepared.kwargs["messages"] is prepared.audit_messages
    assert prepared.kwargs["model"] == "test/model" and prepared.kwargs["temperature"] == 0.2 and prepared.kwargs["seed"] == 19
    assert prepared.kwargs["api_base"] == "https://model.example.test/v1" and prepared.kwargs["api_key"] == "fixture-key"
    assert prepared.audit_messages[0]["content"] == _auto_title._AUTO_TITLE_SYSTEM_PROMPT
    assert prepared.audit_messages[0][PROMPT_GUARD_TRUST_KEY] is PromptGuardTrust.INSTRUCTION
    assert prepared.audit_messages[1]["content"] == _auto_title._build_auto_title_user_content("Use café records")
    other = _auto_title.prepare_auto_title_request(user_message="Use café records", model="test/model", temperature=None, seed=None)
    assert "temperature" not in other.kwargs and "seed" not in other.kwargs and "api_key" not in other.kwargs
    assert other.audit_messages is not prepared.audit_messages


@pytest.mark.asyncio
async def test_title_owned_request_fault_keeps_original_parser_cause_and_closes_work(operation_store, monkeypatch):
    """Fault one real SELECT projection; do not manufacture a corrupt stored row.

    The live parent trigger forbids request_json UPDATE while queued/running.
    SQLAlchemy's scoped read hook preserves the actual selected row, predicates
    and other columns while injecting only that one source field into the
    owned parser. No Row, current claim or authorization result is fabricated.
    """
    running, completed, service = await inserted_case(operation_store)
    work = inventory_work(running)
    engine = operation_store[0]
    parent_select = select(jobs).where(
        jobs.c.session_id == str(running.claim.session_id),
        jobs.c.operation_id == running.claim.operation_id,
    )
    hits = []
    parse_errors = []
    original_validate = SendMessageRequest.model_validate_json

    def fault_parent_request(conn, statement, multiparams, params, _execution_options):
        if parent_select.compare(statement):
            assert conn.engine is engine and conn.in_transaction()
            hits.append(statement)
            statement = statement.with_only_columns(
                *(literal("{}").label(column.name) if column.name == "request_json" else column for column in jobs.c),
                maintain_column_froms=True,
            )
        return statement, multiparams, params

    def observe_parser(_cls, value, *args, **kwargs):
        try:
            return original_validate(value, *args, **kwargs)
        except ValidationError as error:
            assert value == "{}" and kwargs == {"strict": True}
            parse_errors.append(error)
            raise

    try:
        before = recovery_rows(engine)
        actual = await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        assert actual.source.eligible and actual.source.request.content == completed.message.content
        assert recovery_rows(engine) == before
        work.coordinator.assert_completed()
        monkeypatch.setattr(SendMessageRequest, "model_validate_json", classmethod(observe_parser))
        event.listen(engine, "before_execute", fault_parent_request, retval=True)
        try:
            with pytest.raises(AuditIntegrityError, match="initializer parent request violates its admitted owned shape") as caught:
                await load_current_title_input(service=service, selected=completed.snapshot, required_work=work)
        finally:
            event.remove(engine, "before_execute", fault_parent_request)
            work.coordinator.assert_completed()
        assert len(hits) == 1 and len(parse_errors) == 1
        assert caught.value.__cause__ is parse_errors[0]
        assert type(caught.value.__cause__) is ValidationError
        assert recovery_rows(engine) == before
    finally:
        operation_store[1].release(running.session_operation_context)
