"""Real terminal transactions and immutable-snapshot/effective-review separation.

Stored model receipts in this module are synthetic audit fixtures, not evidence
of provider dispatch. The owning integration controls exercise actual finite
SDK-mocked END calls and public replies separately.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.advisor_audit import (
    ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
    AdvisorCheckpointPassRecord,
    AdvisorGraphReviewFact,
    AdvisorGraphReviewIdentity,
    persist_advisor_checkpoint_pass,
)
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorSignoffGateFact
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gate_fingerprint, parse_completion_gates
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationAssistantWrite, ComposerOperationFenceLost
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.graph_recovery import empty_recovery_state
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes._helpers import _composer_conversation_tool_or_llm_audit_messages
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.content_publication import split_content_publication
from tests.unit.web.coordination.test_composer_operation_authority import response, start

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    async_sdk = AsyncMock(spec=litellm.acompletion, side_effect=AssertionError("storage control must not dispatch an SDK call"))
    sync_sdk = Mock(spec=litellm.completion, side_effect=AssertionError("storage control must not execute a row provider"))
    monkeypatch.setattr(litellm, "acompletion", async_sdk)
    monkeypatch.setattr(litellm, "completion", sync_sdk)
    yield
    assert async_sdk.await_count == sync_sdk.call_count == 0


async def seed(store):
    _engine, repository, _authority, service, sid = store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        return await service.save_composition_state(
            sid,
            CompositionStateData(metadata_={"name": "Original", "description": ""}),
            provenance="session_seed",
            session_operation_context=context,
        )
    finally:
        repository.release(context)


async def begin(store, base):
    _engine, _repository, _authority, service, sid = store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Explain", state_id=base.id if base is not None else None)
    record, running = start(store, request)
    await service.add_message_with_transcript(
        sid,
        "user",
        request.content,
        operation_id=UUID(record.operation_id),
        requested_state_id=request.state_id,
        composition_state_id=request.state_id,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    return record, running


def blocked(store, running, base, *, cause=AdvisorBlockCause.UNAVAILABLE, binding="a" * 64):
    state = state_from_record(base) if base is not None else empty_recovery_state()
    fingerprint = completion_gate_fingerprint(state)
    context = running.session_operation_context
    return AdvisorGraphReviewFact(
        session_id=store[4],
        state_id=base.id if base is not None else None,
        content_hash=composition_content_hash(state),
        operation_id=context.fence.operation_id,
        terminal_operation_id=running.claim.operation_id,
        operation_epoch=context.fence.operation_epoch,
        identity=AdvisorGraphReviewIdentity(binding=binding, for_graph=fingerprint, source="unavailable"),
        action="blocked",
        gates=CompletionGateFacts(
            AdvisorSignoffGateFact(
                detail="Review unavailable",
                suggestion="Retry review",
                for_graph=fingerprint,
                note=None,
                cause=cause,
            )
        ),
    )


def draft(fact):
    envelope = fact.to_envelope()
    return AuditMessageDraft(
        role="audit",
        content=canonical_json(envelope),
        tool_calls=(envelope,),
        composition_state_id=str(fact.state_id) if fact.state_id is not None else None,
    )


async def finish(store, running, fact, *, message_id=None):
    return await store[3].complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(
            message_id=message_id or uuid4(),
            content="Trusted review status",
            raw_content=None,
            composition_state_id=fact.state_id,
        ),
        assistant_record=None,
        audit_cohort=(draft(fact),),
        audit_composition_state_id=None,
        build_response=response,
    )


def rows(store):
    with store[0].connect() as conn:
        return conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).mappings().all()


def facts(store):
    return [row for row in rows(store) if row["tool_calls"] and row["tool_calls"][0].get("_kind") == ADVISOR_GRAPH_REVIEW_AUDIT_KIND]


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_terminal_fact_projects_current_explicit_and_recovery_without_checkpoint(operation_store, present):
    store = operation_store
    base = await seed(store) if present else None
    with store[0].connect() as conn:
        original = conn.execute(select(composition_states_table)).mappings().all()
    _record, running = await begin(store, base)
    fact = blocked(store, running, base)
    terminal = await finish(store, running, fact)
    assert terminal.status == "completed"
    store[1].release(running.session_operation_context)
    with store[0].connect() as conn:
        assert conn.execute(select(composition_states_table)).mappings().all() == original
        assert store[3]._graph_review_on_connection(conn, session_id=store[4], stored=base) == fact
    assert len(facts(store)) == 1
    graph = await store[3].read_current_composition_graph(store[4])
    assert graph.effective_completion_gates == fact.gates
    assert graph.head.state_id == (base.id if present else None)
    public = await store[3].get_messages(store[4], include_private_evidence=False)
    visible = _composer_conversation_tool_or_llm_audit_messages(public)
    assert [message.role for message in visible] == ["user", "assistant"]
    if present:
        current = await store[3].get_current_state(store[4])
        explicit = await store[3].get_state(base.id)
        assert current == explicit
        assert current.id == base.id and current.version == base.version
        assert current.review_projection.stored == base
        assert parse_completion_gates(current.composer_meta) == fact.gates
        assert await store[3]._preflight_state_pair(session_id=store[4], anchor_id=base.id) == (current, current)
        assert (await store[3].get_state_versions(store[4]))[0] == base
        graph = await store[3].read_current_composition_graph(store[4])
        assert graph.record == current
    else:
        assert await store[3].get_current_state(store[4]) is None
    context = store[1].acquire(
        session_id=store[4], operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        admission = await store[3].load_graph_recovery_context(
            store[4],
            turn_start_state_id=base.id if base is not None else None,
            session_operation_context=context,
        )
        assert admission.effective_completion_gates == fact.gates
        assert (admission.record is None) is (not present)
    finally:
        store[1].release(context)


@pytest.mark.asyncio
async def test_legacy_snapshot_and_new_authored_head_never_reuse_old_graph_fact(operation_store):
    store = operation_store
    base = await seed(store)
    assert await store[3].get_current_state(store[4]) == base
    _record, running = await begin(store, base)
    fact = blocked(store, running, base)
    await finish(store, running, fact)
    store[1].release(running.session_operation_context)
    new = await seed(store)
    assert new.id != base.id and new.version == base.version + 1
    assert await store[3].get_current_state(store[4]) == new
    assert parse_completion_gates(new.composer_meta) is None
    assert (await store[3].get_state(base.id)).review_projection.review == fact


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "base", "content", "graph", "operation", "epoch", "terminal", "head", "unchanged"])
async def test_terminal_fact_rejects_wrong_custody_and_changed_head_atomically(operation_store, mismatch):
    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    fact = blocked(store, running, base)
    if mismatch == "session":
        fact = replace(fact, session_id=uuid4())
    elif mismatch == "base":
        fact = replace(fact, state_id=uuid4())
    elif mismatch == "content":
        fact = replace(fact, content_hash="b" * 64)
    elif mismatch == "graph":
        identity = replace(fact.identity, for_graph="b" * 64)
        fact = replace(
            fact, identity=identity, gates=CompletionGateFacts(replace(fact.gates.advisor_signoff, for_graph=identity.for_graph))
        )
    elif mismatch == "operation":
        fact = replace(fact, operation_id="wrong-owned-operation")
    elif mismatch == "epoch":
        fact = replace(fact, operation_epoch=fact.operation_epoch + 1)
    elif mismatch == "terminal":
        fact = replace(fact, terminal_operation_id=str(uuid4()))
    elif mismatch == "head":
        await store[3].save_composition_state(
            store[4],
            CompositionStateData(metadata_={"name": "Concurrent", "description": ""}),
            provenance="session_seed",
            session_operation_context=running.session_operation_context,
        )
    else:
        fact = replace(fact, action="unchanged")
    with pytest.raises(AuditIntegrityError):
        await finish(store, running, fact)
    assert facts(store) == []
    assert [row["role"] for row in rows(store)] == ["user"]
    assert store[2].get(session_id=store[4], operation_id=running.claim.operation_id).status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["missing", "other_binding", "flagged", "withheld", "graph_rejected", "clean"])
async def test_only_exact_admitted_clean_model_receipt_can_clear_transient_blocker(operation_store, receipt):
    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    first = blocked(
        store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED if receipt == "graph_rejected" else AdvisorBlockCause.UNAVAILABLE
    )
    await finish(store, running, first)
    store[1].release(running.session_operation_context)
    _record, running = await begin(store, base)
    fact = replace(
        blocked(store, running, base, binding="c" * 64),
        action="passed",
        gates=CompletionGateFacts(None),
        identity=AdvisorGraphReviewIdentity(binding="c" * 64, for_graph=first.identity.for_graph, source="model"),
    )
    if receipt != "missing":
        flagged = receipt == "flagged"
        await persist_advisor_checkpoint_pass(
            sessions=store[3],
            session_id=str(store[4]),
            session_operation_context=running.session_operation_context,
            record=AdvisorCheckpointPassRecord.from_findings(
                phase="end",
                pass_index=1,
                verdict="flagged" if flagged else "clean",
                source="model",
                findings_text="Synthetic receipt",
                provider_attempts=1,
                first_attempt_schema_valid=True,
                first_attempt_accepted=True,
                format_reprompt_sent=False,
                step_ids_offered=0,
                step_ids_kept=0,
                note_present=False,
                url_redactions=0,
                email_redactions=0,
                publication_verdict="WITHHOLD" if receipt == "withheld" else "ADMIT",
                publication_binding="d" * 64 if receipt == "other_binding" else fact.identity.binding,
            ),
        )
    if receipt == "clean":
        await finish(store, running, fact)
        assert len(facts(store)) == 2
        assert parse_completion_gates((await store[3].get_current_state(store[4])).composer_meta) == fact.gates
    else:
        with pytest.raises(AuditIntegrityError):
            await finish(store, running, fact)
        assert len(facts(store)) == 1
        assert parse_completion_gates((await store[3].get_current_state(store[4])).composer_meta) == first.gates


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["rollback", "retry", "precommit_cancel", "postcommit_cancel"])
async def test_terminal_fact_atomicity_and_frozen_retry(operation_store, monkeypatch, seam):
    import elspeth.web.sessions.service as service_module

    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    first = blocked(store, running, base)
    await finish(store, running, first)
    store[1].release(running.session_operation_context)
    _record, running = await begin(store, base)
    second = blocked(store, running, base, binding="e" * 64)
    second = replace(second, gates=CompletionGateFacts(replace(second.gates.advisor_signoff, detail="Second review unavailable")))
    message_id = uuid4()
    calls = []
    failures = []
    cancellation = asyncio.CancelledError("SQL transaction cancelled before commit")
    entered, release = Event(), Event()
    original = service_module.settle_composer_operation_on_connection

    def inject(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if seam == "rollback":
            failure = RuntimeError("terminal must roll back")
            failures.append(failure)
            raise failure
        if seam == "retry" and len(calls) == 1:
            raise RuntimeError("retry frozen terminal")
        return result

    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", inject)
    if seam in {"precommit_cancel", "postcommit_cancel"}:
        original_sync = store[3]._session_process_locked_begin

        from contextlib import contextmanager

        @contextmanager
        def after_commit(*args, **kwargs):
            with original_sync(*args, **kwargs) as conn:
                yield conn
                if seam == "precommit_cancel":
                    entered.set()
                    assert release.wait(5), "caller did not release precommit seam"
                    raise cancellation
            if seam == "postcommit_cancel":
                entered.set()
                assert release.wait(5), "caller did not release postcommit seam"

        monkeypatch.setattr(store[3], "_session_process_locked_begin", after_commit)
    task = asyncio.create_task(finish(store, running, second, message_id=message_id))
    if seam in {"precommit_cancel", "postcommit_cancel"}:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel(seam)
        release.set()
    if seam in {"rollback", "precommit_cancel"}:
        with pytest.raises(BaseExceptionGroup if seam == "rollback" else asyncio.CancelledError) as raised:
            await task
        if seam == "rollback":
            assert raised.value.exceptions == tuple(failures) and len(failures) == 2
        else:
            assert raised.value.__cause__ is cancellation
        assert len(facts(store)) == 1
        assert parse_completion_gates((await store[3].get_current_state(store[4])).composer_meta) == first.gates
        assert all(row["id"] != str(message_id) for row in rows(store))
    else:
        terminal = await task
        assert terminal.status == "completed"
        assert len(calls) == (2 if seam == "retry" else 1)
        assert len(facts(store)) == 2
        assert sum(row["id"] == str(message_id) for row in rows(store)) == 1
        assert parse_completion_gates((await store[3].get_current_state(store[4])).composer_meta) == second.gates
        if seam == "retry":
            assert calls[0] == calls[1], "retry must preserve exact terminal identity/bytes"


@pytest.mark.asyncio
async def test_explicit_unchanged_review_retains_prior_gate_without_latest_pass_inference(operation_store):
    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    first = blocked(store, running, base)
    await finish(store, running, first)
    store[1].release(running.session_operation_context)
    _record, running = await begin(store, base)
    current = replace(blocked(store, running, base, binding="f" * 64), action="unchanged", gates=first.gates)
    await finish(store, running, current)
    with store[0].connect() as conn:
        assert store[3]._graph_review_on_connection(conn, session_id=store[4], stored=base) == current
    assert parse_completion_gates((await store[3].get_state(base.id)).composer_meta) == first.gates


@pytest.mark.asyncio
async def test_strict_fact_codec_and_derived_unicode_bound(operation_store):
    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    fact = blocked(store, running, base)
    fact = replace(fact, gates=CompletionGateFacts(replace(fact.gates.advisor_signoff, note="😀" * 600)))
    assert AdvisorGraphReviewFact.from_envelope(fact.to_envelope()) == fact
    bound = fact.validate_for_state(state_from_record(base), prior=None)
    assert len(canonical_json(fact.to_envelope()).encode()) <= bound
    oversized = replace(fact, gates=CompletionGateFacts(replace(fact.gates.advisor_signoff, note="😀" * 601)))
    with pytest.raises(AuditIntegrityError, match="bounds"):
        oversized.validate_for_state(state_from_record(base), prior=None)
    for field, value in (("schema_version", True), ("operation_epoch", True), ("content_hash", "A" * 64), ("review_source", "invented")):
        envelope = fact.to_envelope()
        envelope["review"][field] = value
        with pytest.raises(AuditIntegrityError):
            AdvisorGraphReviewFact.from_envelope(envelope)
    envelope = fact.to_envelope()
    envelope["review"]["unexpected"] = None
    with pytest.raises(AuditIntegrityError):
        AdvisorGraphReviewFact.from_envelope(envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_writer", ["terminal", "authored"])
async def test_authored_writer_and_review_terminal_serialize_without_stale_fact(operation_store, monkeypatch, first_writer):
    """Actual overlapping worker calls, not malformed-fact perturbations."""
    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    fact = blocked(store, running, base)
    from contextlib import contextmanager

    entered, release, competing = Event(), Event(), Event()
    original_begin = store[3]._session_process_locked_begin
    original_repository_begin = store[1]._locked_transaction
    transaction_entries = []

    @contextmanager
    def observe_transaction(*args, **kwargs):
        transaction_entries.append("attempt")
        if len(transaction_entries) == 2:
            competing.set()
        with original_begin(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store[3], "_session_process_locked_begin", observe_transaction)

    @contextmanager
    def observe_repository_transaction(*args, **kwargs):
        transaction_entries.append("attempt")
        if len(transaction_entries) == 2:
            competing.set()
        with original_repository_begin(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store[1], "_locked_transaction", observe_repository_transaction)
    blocked_calls = []
    if first_writer == "terminal":
        original = store[3]._validate_terminal_graph_review

        def hold(*args, **kwargs):
            value = original(*args, **kwargs)
            blocked_calls.append("terminal")
            entered.set()
            assert release.wait(5)
            return value

        monkeypatch.setattr(store[3], "_validate_terminal_graph_review", hold)
    else:
        from elspeth.web.coordination.repository import _RepositoryCompositionStateMutations

        original = _RepositoryCompositionStateMutations.append_state

        def hold(*args, **kwargs):
            value = original(*args, **kwargs)
            blocked_calls.append("authored")
            entered.set()
            assert release.wait(5)
            return value

        monkeypatch.setattr(_RepositoryCompositionStateMutations, "append_state", hold)

    async def write():
        return await store[3].save_composition_state(
            store[4],
            CompositionStateData(metadata_={"name": "Authored change", "description": ""}),
            provenance="session_seed",
            session_operation_context=running.session_operation_context,
        )

    first = asyncio.create_task(finish(store, running, fact) if first_writer == "terminal" else write())
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    second = asyncio.create_task(write() if first_writer == "terminal" else finish(store, running, fact))
    async with asyncio.timeout(5):
        while not competing.is_set():
            await asyncio.sleep(0.01)
    assert not first.done() and not second.done(), "the first transaction remains live while the competing worker starts"
    release.set()
    succeeded = await first
    with pytest.raises(ComposerOperationFenceLost if first_writer == "terminal" else AuditIntegrityError):
        await second
    assert blocked_calls == [first_writer]
    if first_writer == "terminal":
        assert succeeded.status == "completed"
        assert len(facts(store)) == 1
        assert (await store[3].get_current_state(store[4])).id == base.id
    else:
        assert succeeded.id != base.id
        assert facts(store) == []
        assert [row["role"] for row in rows(store)] == ["user"]
        assert (await store[3].get_current_state(store[4])).id == succeeded.id


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_kind", ["fork", "revert"])
async def test_snapshot_copy_carries_effective_blocker_without_copying_authoritative_audit(operation_store, copy_kind):
    from tests.unit.web.sessions.test_fork import _fork_session

    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    fact = blocked(store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED)
    await finish(store, running, fact)
    store[1].release(running.session_operation_context)
    if copy_kind == "fork":
        user = next(row for row in rows(store) if row["role"] == "user")
        child, _messages, copied = await _fork_session(
            store[3],
            source_session_id=store[4],
            fork_message_id=UUID(user["id"]),
            new_message_content="Explain again",
            user_id="alice",
            auth_provider_type="local",
        )
        assert copied.session_id == child.id != base.session_id
        with store[0].connect() as conn:
            child_audits = (
                conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(child.id))).mappings().all()
            )
        assert not any(row["tool_calls"] and row["tool_calls"][0].get("_kind") == ADVISOR_GRAPH_REVIEW_AUDIT_KIND for row in child_audits)
    else:
        context = store[1].acquire(
            session_id=store[4], operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
        )
        try:
            reserved = await store[3].reserve_operation_receipt(
                session_id=store[4],
                operation_id=str(uuid4()),
                kind="state_revert",
                request_hash="a" * 64,
                actor="composer_route",
                lease_seconds=30,
                session_operation_context=context,
            )
            copied = await store[3].revert_state_for_operation_receipt(
                reserved.fence,
                state_id=base.id,
                expected_current_state_id=base.id,
                expected_current_state_version=base.version,
                actor="composer_route",
                response_hash_factory=lambda _state: "b" * 64,
                session_operation_context=context,
            )
        finally:
            store[1].release(context)
        assert copied.session_id == base.session_id and copied.version == base.version + 1
    assert copied.id != base.id and copied.review_projection is None
    assert parse_completion_gates(copied.composer_meta) == fact.gates
    assert composition_content_hash(state_from_record(copied)) == fact.content_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_later_user_fork_excludes_parent_review_authority(operation_store, present):
    from tests.unit.web.sessions.test_fork import _fork_session

    store = operation_store
    base = await seed(store) if present else None
    _record, running = await begin(store, base)
    prior = blocked(store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED)
    await finish(store, running, prior)
    store[1].release(running.session_operation_context)
    first_user_id = next(row["id"] for row in rows(store) if row["role"] == "user")
    later_record, running = await begin(store, base)
    later_user = next(row for row in rows(store) if row["role"] == "user" and row["id"] != first_user_id)
    await finish(store, running, blocked(store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED))
    store[1].release(running.session_operation_context)
    source_rows = rows(store)
    assert source_rows.index(facts(store)[0]) < source_rows.index(later_user)
    assert later_record.operation_id != _record.operation_id

    child, messages, copied = await _fork_session(
        store[3],
        source_session_id=store[4],
        fork_message_id=UUID(later_user["id"]),
        new_message_content="Explain the earlier review",
        user_id="alice",
        auth_provider_type="local",
    )
    with store[0].connect() as conn:
        child_rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(child.id))).mappings().all()
    assert any(message.content == "Trusted review status" for message in messages)
    assert not any(envelope.get("_kind") == ADVISOR_GRAPH_REVIEW_AUDIT_KIND for row in child_rows for envelope in row["tool_calls"] or ())
    child_context = store[1].acquire(
        session_id=child.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        admission = await store[3].load_graph_recovery_context(
            child.id,
            turn_start_state_id=copied.id if copied is not None else None,
            session_operation_context=child_context,
        )
    finally:
        store[1].release(child_context)
    if present:
        assert copied.id != base.id and copied.review_projection is None
        assert parse_completion_gates(copied.composer_meta) == prior.gates
        assert admission.effective_completion_gates == prior.gates
        assert composition_content_hash(state_from_record(copied)) == prior.content_hash
        assert not copied.is_valid
    else:
        assert copied is None and admission.record is None
        assert admission.effective_completion_gates is None
    child_state = state_from_record(admission.record) if admission.record is not None else empty_recovery_state()
    assert not child_state.sources and not child_state.nodes and not child_state.outputs


@pytest.mark.asyncio
async def test_rootless_prior_blocker_survives_version_changed_turn_without_decision(tmp_path):
    from tests.unit.web.sessions.test_routes import _make_app, _make_composer_mock
    from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

    app, service = _make_app(tmp_path)
    snapshot_factory = app.state.plugin_snapshot_factory
    session = await service.create_session("alice", "Rootless prior review", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Review") as job:
        store = (app.state.session_engine, service.session_operation_authority, job.authority, service, session.id)
        await service.add_message_with_transcript(
            session.id,
            "user",
            job.turn.request.content,
            operation_id=UUID(job.turn.operation_id),
            requested_state_id=None,
            composition_state_id=None,
            writer_principal="route_user_message",
            session_operation_context=job.lease.context,
            running=job.running,
        )
        prior = blocked(store, job.running, None, cause=AdvisorBlockCause.GRAPH_REJECTED)
        await finish(store, job.running, prior)
    app.state.plugin_snapshot_factory = snapshot_factory
    from elspeth.web.sessions.routes._helpers import _durable_completion_gates

    assert parse_completion_gates({"completion_gates": await _durable_completion_gates(service, session.id)}) == prior.gates
    changed = empty_recovery_state().with_metadata({"name": "An authored revision"})
    composer = _make_composer_mock("The draft changed; review is still pending.", state=changed)
    assert composer.compose.return_value.advisor_gate_decision is None
    app.state.composer_service = composer
    async with _running_job(app, service, session.id, kind="compose_message", content="Rename the draft") as job:
        terminal = await _run(app, job)
    assert terminal.status == "completed"
    composer.compose.assert_awaited_once()
    current = await service.get_current_state(session.id)
    assert current.version == 1 and current.review_projection is None
    assert state_from_record(current).metadata == changed.metadata
    assert parse_completion_gates(current.composer_meta) == prior.gates
    assert not current.is_valid


@pytest.mark.asyncio
async def test_interpretation_resolution_preserves_effective_blocker_on_new_authored_checkpoint(operation_store):
    from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
    from tests.unit.web.sessions.test_interpretation_events_service import _llm_node

    store = operation_store
    context = store[1].acquire(
        session_id=store[4], operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        base = await store[3].save_composition_state(
            store[4],
            CompositionStateData(nodes=[_llm_node()], metadata_={"name": "Interpretation", "description": ""}),
            provenance="session_seed",
            session_operation_context=context,
        )
        event = await store[3].create_pending_interpretation_event(
            session_id=store[4],
            composition_state_id=base.id,
            affected_node_id="llm_transform_1",
            tool_call_id="call_review",
            user_term="cool",
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft="concise",
            model_identifier="synthetic/model",
            model_version="synthetic-v1",
            provider="synthetic",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    finally:
        store[1].release(context)
    _record, running = await begin(store, base)
    fact = blocked(store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED)
    await finish(store, running, fact)
    store[1].release(running.session_operation_context)
    context = store[1].acquire(
        session_id=store[4], operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        resolved, produced = await store[3].resolve_interpretation_event(
            session_id=store[4],
            event_id=event.id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="alice",
            session_operation_context=context,
        )
    finally:
        store[1].release(context)
    assert resolved.accepted_value == "concise"
    assert produced.id != base.id and produced.version == base.version + 1
    assert produced.review_projection is None
    assert parse_completion_gates(produced.composer_meta) == fact.gates
    assert produced.nodes[0]["options"]["prompt_template"] != base.nodes[0]["options"]["prompt_template"]


@pytest.mark.asyncio
@pytest.mark.parametrize("review_kind", ["negative", "negative_absent", "positive", "damaged_legacy", "malformed_fact"])
async def test_selected_recovery_retains_only_negative_effective_review(operation_store, review_kind):
    from elspeth.web.composer.graph_recovery import GraphRecoveryIntent

    store = operation_store
    base = None if review_kind == "negative_absent" else await seed(store)
    if review_kind == "damaged_legacy":
        context = store[1].acquire(
            session_id=store[4], operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
        )
        try:
            base = await store[3].save_composition_state(
                store[4],
                CompositionStateData(
                    metadata_={"name": "Damaged legacy gate", "description": ""},
                    composer_meta={"completion_gates": {"schema_version": 999}},
                ),
                provenance="session_seed",
                session_operation_context=context,
            )
        finally:
            store[1].release(context)
    prior = None
    if review_kind in {"negative", "negative_absent", "positive"}:
        _record, running = await begin(store, base)
        prior = blocked(store, running, base, cause=AdvisorBlockCause.GRAPH_REJECTED)
        if review_kind == "positive":
            prior = replace(prior, action="passed", identity=replace(prior.identity, source="model"), gates=CompletionGateFacts(None))
            await persist_advisor_checkpoint_pass(
                sessions=store[3],
                session_id=str(store[4]),
                session_operation_context=running.session_operation_context,
                record=AdvisorCheckpointPassRecord.from_findings(
                    phase="end",
                    pass_index=1,
                    verdict="clean",
                    source="model",
                    findings_text="",
                    provider_attempts=1,
                    first_attempt_schema_valid=True,
                    first_attempt_accepted=True,
                    format_reprompt_sent=False,
                    step_ids_offered=0,
                    step_ids_kept=0,
                    note_present=False,
                    url_redactions=0,
                    email_redactions=0,
                    publication_verdict="ADMIT",
                    publication_binding=prior.identity.binding,
                ),
            )
        await finish(store, running, prior)
        store[1].release(running.session_operation_context)
    _record, running = await begin(store, base)
    admission = await store[3].load_graph_recovery_context(
        store[4],
        turn_start_state_id=base.id if base is not None else None,
        session_operation_context=running.session_operation_context,
    )
    if review_kind == "damaged_legacy":
        assert admission.context.malformed_head and admission.record is None
    intent = GraphRecoveryIntent("reset_to_turn_start", "I restored the draft; review is still needed.", None, "recovery-control")
    if review_kind == "malformed_fact":
        malformed = {"_kind": ADVISOR_GRAPH_REVIEW_AUDIT_KIND, "review": {"schema_version": 999}}
        await store[3].add_message(
            store[4],
            "audit",
            canonical_json(malformed),
            writer_principal="compose_loop",
            tool_calls=(malformed,),
            composition_state_id=base.id,
            session_operation_context=running.session_operation_context,
        )
        with pytest.raises(AuditIntegrityError, match="Graph review"):
            await store[3].prepare_graph_recovery(
                store[4],
                context=admission.context,
                intent=intent,
                session_operation_context=running.session_operation_context,
            )
        with store[0].connect() as conn:
            assert conn.execute(select(composition_states_table.c.id).order_by(composition_states_table.c.version)).scalars().all() == [
                str(base.id)
            ]
        assert store[2].get(session_id=store[4], operation_id=running.claim.operation_id).status == "running"
        return
    recovery = await store[3].prepare_graph_recovery(
        store[4],
        context=admission.context,
        intent=intent,
        session_operation_context=running.session_operation_context,
    )
    assert recovery.failure_reason is None and recovery.data is not None
    assert not recovery.data.is_valid
    if review_kind in {"negative", "negative_absent"}:
        assert parse_completion_gates(recovery.data.composer_meta) == prior.gates
    else:
        assert recovery.data.composer_meta is None, "positive review and damaged legacy gates grant no restored permission"
    terminal = await store[3].complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(
            message_id=uuid4(), content=intent.message, raw_content=None, composition_state_id=base.id if base is not None else None
        ),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
        recovery=recovery,
    )
    assert terminal.status == "completed"
    restored = await store[3].get_current_state(store[4])
    assert restored.id == recovery.state_id and restored.review_projection is None and not restored.is_valid
    publication, preserved = split_content_publication(restored)
    assert preserved == (dict(deep_thaw(recovery.data.composer_meta)) if recovery.data.composer_meta is not None else {})
    assert publication.state_id == restored.id
    assert len(facts(store)) == (1 if prior is not None else 0), "recovery does not copy private authoritative facts"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["normal", "recovery"])
@pytest.mark.parametrize("receipt_kind", ["missing", "wrong_binding", "withheld"])
async def test_both_read_qualification_owners_reject_unadmitted_lower_fact(operation_store, monkeypatch, surface, receipt_kind):
    """The lower terminal-custody carrier alone is deliberately not permission.

    Perturb its nominal result, leaving real SQL rows/terminal custody intact.
    Both full qualification owners must still execute the actual END receipt
    query. Omitting either caller's receipt check would expose this false pass.
    """
    from elspeth.web.composer.graph_recovery import GraphRecoveryIntent

    store = operation_store
    base = await seed(store)
    _record, running = await begin(store, base)
    first = blocked(store, running, base)
    if receipt_kind != "missing":
        await persist_advisor_checkpoint_pass(
            sessions=store[3],
            session_id=str(store[4]),
            session_operation_context=running.session_operation_context,
            record=AdvisorCheckpointPassRecord.from_findings(
                phase="end",
                pass_index=1,
                verdict="clean",
                source="model",
                findings_text="Synthetic receipt",
                provider_attempts=1,
                first_attempt_schema_valid=True,
                first_attempt_accepted=True,
                format_reprompt_sent=False,
                step_ids_offered=0,
                step_ids_kept=0,
                note_present=False,
                url_redactions=0,
                email_redactions=0,
                publication_verdict="WITHHOLD" if receipt_kind == "withheld" else "ADMIT",
                publication_binding="b" * 64 if receipt_kind == "wrong_binding" else first.identity.binding,
            ),
        )
    await finish(store, running, first)
    store[1].release(running.session_operation_context)
    _record, running = await begin(store, base)
    admission = await store[3].load_graph_recovery_context(
        store[4],
        turn_start_state_id=base.id,
        session_operation_context=running.session_operation_context,
    )
    unqualified = replace(first, action="passed", identity=replace(first.identity, source="model"), gates=CompletionGateFacts(None))
    original = store[3]._graph_review_for_base_on_connection
    lower_calls = []

    def false_pass(*args, **kwargs):
        actual = original(*args, **kwargs)
        assert actual == first, "only the real exact terminal-custody result is perturbed"
        lower_calls.append(actual)
        return unqualified

    monkeypatch.setattr(store[3], "_graph_review_for_base_on_connection", false_pass)
    with pytest.raises(AuditIntegrityError, match=r"receipt|admitted CLEAN"):
        if surface == "normal":
            await store[3].get_state(base.id)
        else:
            await store[3].prepare_graph_recovery(
                store[4],
                context=admission.context,
                intent=GraphRecoveryIntent("reset_to_turn_start", "Restore draft", None, "recover-check"),
                session_operation_context=running.session_operation_context,
            )
    assert lower_calls == [first]
    assert len(facts(store)) == 1
    assert store[2].get(session_id=store[4], operation_id=running.claim.operation_id).status == "running"
