"""Review cohorts share pure preparation, never mutation authority or state heads."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import _RepositoryInterpretationMutations
from elspeth.web.sessions.models import blobs_table, composition_states_table, interpretation_events_table
from elspeth.web.sessions.pending_interpretation import _node_specs_from_state_record, _PendingInterpretationGraphs
from elspeth.web.sessions.protocol import CompositionStateData, InterpretationResolveError, PreparedInterpretationEventDraft
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.coordination.test_composer_operation_authority import start
from tests.unit.web.sessions import test_interpretation_events_service as interpretation_tests
from tests.unit.web.sessions.test_interpretation_events_service import _llm_node

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)
inline_engine = interpretation_tests.engine


def _cohort(count):
    terms = tuple(f"term{index}" for index in range(count))
    state = CompositionStateData(
        sources={},
        nodes=[_llm_node(prompt_template=" ".join("{{interpretation:" + term + "}}" for term in terms))],
        edges=[],
        outputs=[],
        metadata_={"name": "Review batch", "description": ""},
    )
    drafts = tuple(
        PreparedInterpretationEventDraft(
            event_id=uuid4(),
            affected_node_id="llm_transform_1",
            tool_call_id=f"batch-review-{index}",
            user_term=term,
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft=f"meaning {index}",
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier=f"model-{index}",
            model_version="1",
            provider="test",
            composer_skill_hash="a" * 64,
        )
        for index, term in enumerate(terms)
    )
    return state, drafts


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 5])
async def test_cohort_prepares_once_and_avoids_other_terms_graph_reads(operation_store, monkeypatch, count):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(count)
    principal_calls = 0
    record_decodes = 0
    state_identity_reads = []
    original_principal = SessionServiceImpl._session_principal_context
    original_record = _RepositoryInterpretationMutations._state_record

    async def principal(instance, *args, **kwargs):
        nonlocal principal_calls
        principal_calls += 1
        return await original_principal(instance, *args, **kwargs)

    def decode(cls, row):
        nonlocal record_decodes
        record_decodes += 1
        return original_record(row)

    def sql(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SELECT composition_states.") and "WHERE composition_states.id =" in statement:
            state_identity_reads.append(parameters)

    monkeypatch.setattr(SessionServiceImpl, "_session_principal_context", principal)
    monkeypatch.setattr(_RepositoryInterpretationMutations, "_state_record", classmethod(decode))
    event.listen(engine, "before_cursor_execute", sql)
    try:
        saved = await service.save_composition_state_with_interpretations(
            sid,
            state,
            provenance="tool_call",
            interpretations=drafts,
            session_operation_context=running.session_operation_context,
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        repository.release(running.session_operation_context)
    assert principal_calls == 1
    assert record_decodes == 1
    # Each card still reads its locked anchor. Unrelated prior terms do not
    # cause a triangular number of extra surfacing-state SELECTs.
    assert len(state_identity_reads) == count
    events = await service.list_interpretation_events(sid)
    assert len(events) == count
    for persisted, draft in zip(events, drafts, strict=True):
        assert persisted.id == draft.event_id
        assert persisted.composition_state_id == saved.id
        assert persisted.user_term == draft.user_term
        assert persisted.tool_call_id == draft.tool_call_id
        assert persisted.model_identifier == draft.model_identifier
        assert persisted.llm_draft == draft.llm_draft
        assert persisted.choice.value == "pending"


@pytest.mark.asyncio
async def test_new_cohort_reprepares_instead_of_reusing_authority(operation_store, monkeypatch):
    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(2)
    calls = 0
    original = SessionServiceImpl._session_principal_context

    async def principal(instance, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(instance, *args, **kwargs)

    monkeypatch.setattr(SessionServiceImpl, "_session_principal_context", principal)
    try:
        first = await service._prepare_pending_interpretation_cohort(
            session_id=sid,
            composition_state_id=uuid4(),
            drafts=drafts,
            session_operation_context=running.session_operation_context,
            proposed_state=state,
        )
        second = await service._prepare_pending_interpretation_cohort(
            session_id=sid,
            composition_state_id=uuid4(),
            drafts=drafts,
            session_operation_context=running.session_operation_context,
            proposed_state=replace(state, metadata_={"name": "Changed", "description": ""}),
        )
    finally:
        repository.release(running.session_operation_context)
    assert calls == 2
    assert first[0].validator is first[1].validator
    assert second[0].validator is not first[0].validator


@pytest.mark.asyncio
async def test_opt_out_cards_see_each_appended_head(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(2)
    # Different reviewed nodes preserve each card's immutable content identity;
    # resolving one term in a shared prompt correctly invalidates sibling cards.
    state = replace(
        state,
        nodes=[_llm_node(node_id=f"llm_{index}", prompt_template="{{interpretation:term" + str(index) + "}}") for index in range(2)],
    )
    drafts = tuple(replace(draft, affected_node_id=f"llm_{index}") for index, draft in enumerate(drafts))
    try:
        await service.record_session_interpretation_opt_out(
            session_id=sid,
            actor="user:alice",
            session_operation_context=running.session_operation_context,
        )
        final = await service.save_composition_state_with_interpretations(
            sid,
            state,
            provenance="tool_call",
            interpretations=drafts,
            session_operation_context=running.session_operation_context,
        )
    finally:
        repository.release(running.session_operation_context)
    assert final.version == 3
    assert final.nodes is not None
    prompt = " ".join(node["options"]["prompt_template"] for node in final.nodes)
    assert "meaning 0" in prompt and "meaning 1" in prompt
    assert "{{interpretation:" not in prompt
    events = await service.list_interpretation_events(sid)
    assert {item.id for item in events if item.kind is not None} == {draft.event_id for draft in drafts}
    assert all(item.choice.value == "opted_out" for item in events)


@pytest.mark.asyncio
async def test_late_card_refusal_rolls_back_state_and_entire_cohort(operation_store):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(2)
    drafts = (drafts[0], replace(drafts[1], affected_node_id="missing"))
    try:
        with pytest.raises(InterpretationResolveError):
            await service.save_composition_state_with_interpretations(
                sid,
                state,
                provenance="tool_call",
                interpretations=drafts,
                session_operation_context=running.session_operation_context,
            )
    finally:
        repository.release(running.session_operation_context)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(interpretation_events_table)).scalar_one() == 0


@pytest.mark.asyncio
async def test_derived_graph_scope_does_not_reuse_replacement_record(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, _drafts = _cohort(1)
    try:
        saved = await service.save_composition_state(
            sid,
            state,
            provenance="tool_call",
            session_operation_context=running.session_operation_context,
        )
    finally:
        repository.release(running.session_operation_context)
    changed = replace(saved, nodes=[_llm_node(node_id="changed")])
    cache = _PendingInterpretationGraphs()
    with cache.scope():
        original = _node_specs_from_state_record(saved)
        assert _node_specs_from_state_record(saved) is original
        replacement = _node_specs_from_state_record(changed)
        assert replacement[0].id == "changed"
        assert original[0].id == "llm_transform_1"
    assert _node_specs_from_state_record(saved) is not original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["state", "fence"])
async def test_shared_preparation_cannot_authorize_a_changed_head_or_released_fence(operation_store, change):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(1)
    saved = await service.save_composition_state(
        sid,
        state,
        provenance="tool_call",
        session_operation_context=running.session_operation_context,
    )
    (prepared,) = await service._prepare_pending_interpretation_cohort(
        session_id=sid,
        composition_state_id=saved.id,
        drafts=drafts,
        session_operation_context=running.session_operation_context,
        proposed_state=state,
    )
    if change == "state":
        await service.save_composition_state(
            sid,
            replace(state, nodes=[_llm_node(prompt_template="No review remains.")]),
            provenance="tool_call",
            session_operation_context=running.session_operation_context,
        )
        expected = InterpretationResolveError
    else:
        repository.release(running.session_operation_context)
        expected = SessionOperationFenceLost
    try:
        with pytest.raises(expected):
            await service._run_sync(
                service.session_operation_authority.mutate,
                running.session_operation_context,
                lambda transaction: transaction.interpretations.create_or_reconcile_pending(prepared.command, prepared.validator),
            )
    finally:
        if change == "state":
            repository.release(running.session_operation_context)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(interpretation_events_table)).scalar_one() == 0


@pytest.mark.asyncio
async def test_unicode_trimmed_same_term_reuses_card_and_ignores_other_term_graphs(operation_store):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    state, drafts = _cohort(2)
    saved = await service.save_composition_state_with_interpretations(
        sid,
        state,
        provenance="tool_call",
        interpretations=drafts,
        session_operation_context=running.session_operation_context,
    )
    duplicate = replace(drafts[0], event_id=uuid4(), tool_call_id="duplicate", user_term="\u2003term0\u00a0")
    (prepared,) = await service._prepare_pending_interpretation_cohort(
        session_id=sid,
        composition_state_id=saved.id,
        drafts=(duplicate,),
        session_operation_context=running.session_operation_context,
        proposed_state=state,
    )
    identity_reads = []

    def sql(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SELECT composition_states.") and "WHERE composition_states.id =" in statement:
            identity_reads.append(parameters)

    event.listen(engine, "before_cursor_execute", sql)
    try:
        result = await service._run_sync(
            service.session_operation_authority.mutate,
            running.session_operation_context,
            lambda transaction: transaction.interpretations.create_or_reconcile_pending(prepared.command, prepared.validator),
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        repository.release(running.session_operation_context)
    assert result.id == drafts[0].event_id
    assert result.user_term == "term0"
    assert len(identity_reads) == 2  # locked anchor plus matching term's surfacing state
    events = await service.list_interpretation_events(sid)
    assert {item.id for item in events} == {draft.event_id for draft in drafts}
    assert all(item.choice.value == "pending" for item in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content_hash", "storage_path"])
async def test_shared_preparation_refuses_inline_blob_content_or_storage_version_drift(inline_engine, change):
    content = b"sku,description\nhats,A fine hat\n"
    sid, blob_id = uuid4(), uuid4()
    records = []

    def read_blob(context, requested_id):
        assert context.fence.session_id == str(sid)
        assert requested_id == blob_id
        return records[0], content

    service = interpretation_tests.FencedSessionServiceHarness(
        inline_engine,
        telemetry=interpretation_tests.build_sessions_telemetry(),
        log=interpretation_tests.structlog.get_logger("review-batch-test"),
        inline_blob_read=read_blob,
    )
    saved, blob = await interpretation_tests._seed_inline_reference_review_state(service, session_id=sid, blob_id=blob_id, content=content)
    records.append(blob)
    proposed = CompositionStateData(
        sources=saved.sources,
        nodes=saved.nodes,
        edges=saved.edges,
        outputs=saved.outputs,
        metadata_=saved.metadata_,
    )
    _state, drafts = _cohort(2)
    async with interpretation_tests._compose_operation(service, sid) as context:
        prepared = await service._prepare_pending_interpretation_cohort(
            session_id=sid,
            composition_state_id=saved.id,
            drafts=drafts,
            session_operation_context=context,
            proposed_state=proposed,
        )
        assert prepared[0].validator is prepared[1].validator
        assert prepared[0].validator.inline_blob_snapshot is not None
        with inline_engine.begin() as connection:
            connection.execute(
                update(blobs_table)
                .where(blobs_table.c.id == str(blob_id))
                .values(
                    **({"content_hash": "0" * 64} if change == "content_hash" else {"storage_path": blob.storage_path + ".replacement"})
                )
            )
        with pytest.raises(AuditIntegrityError, match="inline blob changed"):
            await service._run_sync(
                service.session_operation_authority.mutate,
                context,
                lambda transaction: transaction.interpretations.create_or_reconcile_pending(prepared[0].command, prepared[0].validator),
            )
    with inline_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(interpretation_events_table)).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "cohort"])
async def test_real_single_and_cohort_paths_preserve_complete_command_payload(operation_store, monkeypatch, path):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    proposed, drafts = _cohort(2)
    now = datetime.now(UTC)
    captured = []
    original = _RepositoryInterpretationMutations.create_or_reconcile_pending

    def observe(instance, command, validator):
        captured.append(command)
        return original(instance, command, validator)

    monkeypatch.setattr(_RepositoryInterpretationMutations, "create_or_reconcile_pending", observe)
    monkeypatch.setattr(service, "_now", lambda: now)
    try:
        if path == "cohort":
            saved = await service.save_composition_state_with_interpretations(
                sid,
                proposed,
                provenance="tool_call",
                interpretations=drafts,
                session_operation_context=running.session_operation_context,
            )
        else:
            saved = await service.save_composition_state(
                sid,
                proposed,
                provenance="tool_call",
                session_operation_context=running.session_operation_context,
            )
            for index, draft in enumerate(drafts):
                await service.create_pending_interpretation_event(
                    session_id=sid,
                    composition_state_id=saved.id,
                    affected_node_id=draft.affected_node_id,
                    tool_call_id=draft.tool_call_id,
                    user_term=draft.user_term,
                    kind=draft.kind,
                    llm_draft=draft.llm_draft,
                    surface_origin=draft.surface_origin,
                    model_identifier=draft.model_identifier,
                    model_version=draft.model_version,
                    provider=draft.provider,
                    composer_skill_hash=draft.composer_skill_hash,
                    created_at=now + timedelta(microseconds=index),
                    session_operation_context=running.session_operation_context,
                )
    finally:
        repository.release(running.session_operation_context)
    events = await service.list_interpretation_events(sid)
    assert len(captured) == len(events) == len(drafts) == 2
    identifiers = []
    for index, (command, persisted, draft) in enumerate(zip(captured, events, drafts, strict=True)):
        # The public single-event path allocates its own event id; imported
        # cohorts preserve the caller's draft id. Both allocate a new marker.
        assert command.event_id == persisted.id
        if path == "cohort":
            assert command.event_id == draft.event_id
        assert type(command.event_id) is UUID and command.event_id.version == 4
        assert type(command.opt_out_marker_event_id) is UUID and command.opt_out_marker_event_id.version == 4
        identifiers.extend((command.event_id, command.opt_out_marker_event_id))
        expected = {
            "composition_state_id": saved.id,
            "affected_node_id": draft.affected_node_id,
            "tool_call_id": draft.tool_call_id,
            "user_term": draft.user_term,
            "kind": draft.kind,
            "llm_draft": draft.llm_draft,
            "surface_origin": draft.surface_origin,
            "model_identifier": draft.model_identifier,
            "model_version": draft.model_version,
            "provider": draft.provider,
            "composer_skill_hash": draft.composer_skill_hash,
            "created_at": now + timedelta(microseconds=index),
        }
        assert {
            "composition_state_id": command.composition_state_id,
            "affected_node_id": command.affected_node_id,
            "tool_call_id": command.tool_call_id,
            "user_term": command.user_term,
            "kind": command.kind,
            "llm_draft": command.llm_draft,
            "surface_origin": command.surface_origin,
            "model_identifier": command.model_identifier,
            "model_version": command.model_version,
            "provider": command.provider,
            "composer_skill_hash": command.composer_skill_hash,
            "created_at": command.created_at,
        } == expected
        assert {
            "composition_state_id": persisted.composition_state_id,
            "affected_node_id": persisted.affected_node_id,
            "tool_call_id": persisted.tool_call_id,
            "user_term": persisted.user_term,
            "kind": persisted.kind,
            "llm_draft": persisted.llm_draft,
            "surface_origin": persisted.surface_origin,
            "model_identifier": persisted.model_identifier,
            "model_version": persisted.model_version,
            "provider": persisted.provider,
            "composer_skill_hash": persisted.composer_skill_hash,
            "created_at": persisted.created_at,
        } == expected
        assert persisted.session_id == sid
        assert persisted.choice.value == "pending"
    assert len(set(identifiers)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "cohort"])
@pytest.mark.parametrize("fault", ["raw_kind", "released_fence"])
async def test_real_single_and_cohort_refusals_publish_no_command_or_state(operation_store, path, fault):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    proposed, drafts = _cohort(1)
    saved = await service.save_composition_state(
        sid,
        proposed,
        provenance="tool_call",
        session_operation_context=running.session_operation_context,
    )
    with engine.connect() as connection:
        prior_state_ids = set(connection.scalars(select(composition_states_table.c.id)))
    if fault == "released_fence":
        repository.release(running.session_operation_context)
        expected = SessionOperationFenceLost
    else:
        # Deliberate Tier-1 corruption of an otherwise admitted exact DTO:
        # exercising the service boundary must not silently coerce this string.
        object.__setattr__(drafts[0], "kind", "vague_term")
        expected = ValueError if path == "single" else AuditIntegrityError
    try:
        with pytest.raises(expected):
            if path == "cohort":
                await service.save_composition_state_with_interpretations(
                    sid,
                    proposed,
                    provenance="tool_call",
                    interpretations=drafts,
                    session_operation_context=running.session_operation_context,
                )
            else:
                draft = drafts[0]
                await service.create_pending_interpretation_event(
                    session_id=sid,
                    composition_state_id=saved.id,
                    affected_node_id=draft.affected_node_id,
                    tool_call_id=draft.tool_call_id,
                    user_term=draft.user_term,
                    kind=draft.kind,
                    llm_draft=draft.llm_draft,
                    surface_origin=draft.surface_origin,
                    model_identifier=draft.model_identifier,
                    model_version=draft.model_version,
                    provider=draft.provider,
                    composer_skill_hash=draft.composer_skill_hash,
                    session_operation_context=running.session_operation_context,
                )
    finally:
        if fault != "released_fence":
            repository.release(running.session_operation_context)
    current = await service.get_current_state(sid)
    assert current is not None and current.id == saved.id
    with engine.connect() as connection:
        assert set(connection.scalars(select(composition_states_table.c.id))) == prior_state_ids
        assert connection.scalar(select(func.count()).select_from(interpretation_events_table)) == 0
