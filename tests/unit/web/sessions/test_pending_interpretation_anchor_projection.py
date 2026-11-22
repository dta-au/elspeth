"""A committed graph review must not trip the pending-review source-state guard.

Live defect (0.8.16 battery edit 37): a no-edit composer turn on a state whose
previous no-edit turn's END review committed a graph review, while a prompt
review was open on a FILE-HELD prompt, failed with HTTP 500 "could not verify
this session's audit trail". The leaf was
``AuditIntegrityError("pending interpretation anchor changed after inline blob
snapshot")`` from ``_SessionPendingInterpretationValidator.assert_source_state``.

Cause: the create path took its expected (anchor, live) pair from
``SessionServiceImpl._preflight_state_pair``, which returned EFFECTIVE records
(stored row plus the committed graph review projected onto ``composer_meta``
and ``review_projection``), while the coordination repository plans against
RAW locked rows. Strict equality then failed by construction. The fix makes
the caller name the kind of record: the create path asks for the stored pair,
the resolve path (which re-reads effective records under the lock) for the
projected pair. The guard itself stays a strict ``!=``.

Driven for real: the blob tools (``create_blob``, ``wire_blob_inline_ref``),
``save_composition_state``, ``create_pending_interpretation_event`` (with an
inline blob reader wired, so the guard is armed),
``resolve_interpretation_event``, the composer async-operation terminal that
commits the graph review (``complete_composer_async_operation``), and
``surface_pending_interpretation_reviews_for_state``, the function the no-tool
finalize calls (``_surface_pt_and_gate_orphans_or_none`` ->
``InterpretationSurfacing.surface_pending_interpretation_reviews`` ->
``surface_pending_interpretation_reviews_for_state``). Not driven: the compose
loop and ``_try_terminate_no_tools`` themselves; committing a graph review
through them needs two complete composer turns with an END advisor review, so
the review is committed here through the same terminal method those turns use.
The graph review fact is a synthetic "blocked / unavailable" fact (no model
receipt is required for that source), as in ``test_graph_review_facts.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.app import create_app
from elspeth.web.composer.interpretation_surfacing import surface_pending_interpretation_reviews_for_state
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.composer.tools.sessions import _handle_request_interpretation_review
from elspeth.web.config import WebSettings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.interpretation_state import prompt_review_draft_from_options
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.inline_blob_preflight import InlinePreflightState, inline_blob_refs
from elspeth.web.sessions.models import blobs_table, composition_states_table, interpretation_events_table
from elspeth.web.sessions.protocol import CompositionStateRecord
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.helpers.prompt_review import reviewed_anchor_for
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.coordination.test_composer_operation_authority import admit
from tests.unit.web.sessions.test_approved_file_prompt_keeps_guarantees import _save_state
from tests.unit.web.sessions.test_graph_review_facts import begin, blocked, finish

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)

ANCHOR_MESSAGE = "pending interpretation anchor changed after inline blob snapshot"
LIVE_MESSAGE = "pending interpretation live state changed after inline blob snapshot"
BLOB_MESSAGE = "inline blob changed after preflight snapshot"
USER_TERM = "llm_prompt_template:classify"
FILE_TEXT = "Classify this row: {{ row.text }}"
USER_MESSAGE_TEXT = f"Use this exact content:\n{FILE_TEXT}"
PLAIN_LABEL_PROMPT = "Give {{ row.text }} a one-word tag"
# The tool path cannot raise llm_prompt_template (the backend surfaces it); a
# vague_term placeholder gives the tool a site of its own on the other step.
VAGUE_LABEL_PROMPT = "Give {{ row.text }} a {{interpretation:cool}} one-word tag"


def _wire_file(env: dict[str, Any], state: CompositionState, *, user_message_id: str) -> CompositionState:
    """Create a user-supplied prompt file and bind it as the llm node's prompt (a file-held prompt)."""
    blob = blob_tools.execute_tool(
        "create_blob",
        {"filename": "prompt.txt", "mime_type": "text/plain", "content": FILE_TEXT},
        blob_tools._empty_state(),
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
        user_message_id=user_message_id,
        user_message_content=USER_MESSAGE_TEXT,
    )
    assert blob.success, blob.to_dict()
    wired = blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": "node:classify.options.prompt_template", "blob_id": blob.data["blob_id"]},
        state,
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
    )
    assert wired.success, wired.to_dict()
    return wired.updated_state


def _read_blob(repository: Any, context: Any, blob_id: UUID) -> tuple[Any, bytes]:
    record = repository.mutate(context, lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    return record, Path(record.storage_path).read_bytes()


@pytest.fixture
def store(operation_store: Any, tmp_path: Path) -> tuple[Any, ...]:
    """``operation_store`` with a service that reads inline blobs, so the guard is armed."""
    engine, repository, authority, _plain, sid = operation_store
    (tmp_path / "blobs").mkdir(exist_ok=True)
    service = SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.anchor-projection"),
        session_operation_authority=repository,
        owner_instance_id="test-owner",
        inline_blob_read=lambda context, blob_id: _read_blob(repository, context, blob_id),
    )
    return engine, repository, authority, service, sid


def _acquire(store: tuple[Any, ...]) -> Any:
    _engine, repository, _authority, _service, sid = store
    return repository.acquire(session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30)


async def _create(service: SessionServiceImpl, sid: UUID, state_id: UUID, draft: str, context: Any) -> Any:
    return await service.create_pending_interpretation_event(
        session_id=sid,
        composition_state_id=state_id,
        affected_node_id="classify",
        tool_call_id="anchor-projection-test",
        user_term=USER_TERM,
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        llm_draft=draft,
        model_identifier="test-model",
        model_version="1",
        provider="test-provider",
        composer_skill_hash="a" * 64,
        session_operation_context=context,
    )


def _state_rows(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(composition_states_table).order_by(composition_states_table.c.version)).mappings()]


def _event_rows(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(interpretation_events_table).order_by(interpretation_events_table.c.created_at, interpretation_events_table.c.id)
            ).mappings()
        ]


def _two_step_state(tmp_path: Path, *, label_prompt: str) -> CompositionState:
    """csv -> classify (llm, prompt to be file-held) -> label (llm, inline prompt with a pending review) -> json."""
    state = blob_tools._field_declaring_sink_state(tmp_path)
    classify = replace(state.nodes[0], on_success="classified_rows")
    label_options = dict(classify.options)
    label_options["prompt_template"] = label_prompt
    label_options["response_field"] = "tag"
    label = replace(
        classify,
        id="label",
        input="classified_rows",
        on_success="classified",
        options=_options_with_default_prompt_template_review(node_id="label", plugin="llm", options=label_options),
    )
    return replace(state, nodes=(classify, label))


async def _commit_graph_review(store: tuple[Any, ...], state: CompositionStateRecord) -> None:
    """A no-edit turn's END review: a completed composer operation whose committed graph review binds ``state``."""
    repository = store[1]
    _record, running = await begin(store, state)
    terminal = await finish(store, running, blocked(store, running, state))
    assert terminal.status == "completed"
    repository.release(running.session_operation_context)


async def _seed(
    store: tuple[Any, ...], tmp_path: Path, *, label_prompt: str | None = None
) -> tuple[CompositionStateRecord, CompositionState, str, Any]:
    """A file-held prompt state, its open prompt review, then a committed graph review on that state.

    With ``label_prompt`` a second llm step ("label", inline prompt, pending
    review requirement but NO event yet) follows the file-held step, so a
    later create on "label" touches a different step from the open card.

    Returns the raw stored record, the wired state, the review draft and the
    open event, after asserting the trigger condition holds: the effective
    record of the state differs from its raw row only by the projection.
    """
    engine, repository, _authority, service, sid = store
    context = _acquire(store)
    try:
        env = {"engine": engine, "session_id": str(sid), "data_dir": str(tmp_path), "operation": context, "authority": repository}
        # The user message the file's verbatim content comes from.
        message = await service.add_message(
            sid, "user", USER_MESSAGE_TEXT, writer_principal="route_user_message", session_operation_context=context
        )
        base_state = (
            _two_step_state(tmp_path, label_prompt=label_prompt)
            if label_prompt is not None
            else blob_tools._field_declaring_sink_state(tmp_path)
        )
        wired = _wire_file(env, base_state, user_message_id=str(message.id))
        saved = await _save_state(service, env, wired)
        draft = prompt_review_draft_from_options(wired.nodes[0].options)
        assert draft is not None, "the wired state carries a pending llm_prompt_template requirement"
        # The review opens BEFORE any graph review exists, as at battery edit 22.
        event = await _create(service, sid, saved.id, draft, context)
    finally:
        repository.release(context)
    assert event.choice is InterpretationChoice.PENDING

    await _commit_graph_review(store, saved)

    current = await service.get_current_state(sid)
    assert current is not None and current.id == saved.id
    assert current.review_projection is not None, "precondition: a committed graph review is bound to the anchor"
    assert current.review_projection.stored == saved
    assert current != saved, "precondition: the effective record differs from the raw row"
    assert replace(current, composer_meta=saved.composer_meta, review_projection=None) == saved, "only the projection differs"
    # Either record selects the same inline blobs: the preflight config reads
    # sources, nodes and outputs, which the projection does not touch.
    assert InlinePreflightState.from_stored_state(current) == InlinePreflightState.from_stored_state(saved)
    assert inline_blob_refs(InlinePreflightState.from_stored_state(saved)), "precondition: the prompt is file-held (inline marker)"
    return saved, wired, draft, event


@pytest.mark.asyncio
async def test_reconcile_of_file_prompt_review_after_committed_graph_review_is_not_refused(store: tuple[Any, ...], tmp_path: Path) -> None:
    """RED on base with ANCHOR_MESSAGE; on the fix the open review is reconciled and nothing else is written."""
    engine, repository, _authority, service, sid = store
    saved, _wired, draft, event = await _seed(store, tmp_path)
    states_before = _state_rows(engine)
    events_before = _event_rows(engine)

    context = _acquire(store)
    try:
        reconciled = await _create(service, sid, saved.id, draft, context)
    finally:
        repository.release(context)

    assert reconciled.id == event.id, "the open review for the same prompt is reused, not duplicated"
    assert reconciled.choice is InterpretationChoice.PENDING
    assert reconciled.composition_state_id == saved.id
    assert _state_rows(engine) == states_before, "a reconcile writes no composition state"
    assert _event_rows(engine) == events_before
    current = await service.get_current_state(sid)
    assert current is not None and current.version == saved.version


@pytest.mark.asyncio
async def test_no_tool_finalize_surfacing_entry_reaches_the_create_path_and_succeeds(
    store: tuple[Any, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surfacing function the no-tool finalize calls, over the same state; RED on base with ANCHOR_MESSAGE."""
    engine, repository, _authority, service, sid = store
    saved, _wired, _draft, event = await _seed(store, tmp_path)
    states_before = _state_rows(engine)
    events_before = _event_rows(engine)
    calls: list[UUID] = []
    original = service.create_pending_interpretation_event

    async def counted(**kwargs: Any) -> Any:
        calls.append(kwargs["composition_state_id"])
        return await original(**kwargs)

    monkeypatch.setattr(service, "create_pending_interpretation_event", counted)
    context = _acquire(store)
    try:
        await surface_pending_interpretation_reviews_for_state(
            state_from_record(saved),
            sessions_service=service,
            session_id=str(sid),
            current_state_id=str(saved.id),
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="test-model",
            model_version="1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    finally:
        repository.release(context)

    assert calls == [saved.id], "the surfacing entry reached create_pending_interpretation_event exactly once"
    assert _state_rows(engine) == states_before
    assert _event_rows(engine) == events_before
    (pending,) = [row for row in _event_rows(engine) if row["choice"] == InterpretationChoice.PENDING.value]
    assert pending["id"] == str(event.id)


def _count_creates(service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, UUID]]:
    """Record the (node, state) of every ``create_pending_interpretation_event`` call, then run the real method."""
    calls: list[tuple[str, UUID]] = []
    original = service.create_pending_interpretation_event

    async def counted(**kwargs: Any) -> Any:
        calls.append((kwargs["affected_node_id"], kwargs["composition_state_id"]))
        return await original(**kwargs)

    monkeypatch.setattr(service, "create_pending_interpretation_event", counted)
    return calls


async def _surface(service: SessionServiceImpl, store: tuple[Any, ...], saved: CompositionStateRecord) -> None:
    """One surfacing pass over ``saved``, as the no-tool finalize runs it."""
    sid = store[4]
    context = _acquire(store)
    try:
        await surface_pending_interpretation_reviews_for_state(
            state_from_record(saved),
            sessions_service=service,
            session_id=str(sid),
            current_state_id=str(saved.id),
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="test-model",
            model_version="1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    finally:
        store[1].release(context)


@pytest.mark.asyncio
async def test_tool_path_review_on_another_step_than_the_open_file_card_is_not_refused(store: tuple[Any, ...], tmp_path: Path) -> None:
    """``request_interpretation_review`` (the composer TOOL path) for step "label" while the open card is on "classify".

    The guard is armed by ANY inline marker in the live state, so the
    create on a step without a file still compared projected with raw rows on
    base (RED with ANCHOR_MESSAGE).
    """
    engine, repository, _authority, service, sid = store
    saved, _wired, _draft, event = await _seed(store, tmp_path, label_prompt=VAGUE_LABEL_PROMPT)
    assert event.affected_node_id == "classify"
    states_before = _state_rows(engine)
    events_before = _event_rows(engine)
    context = _acquire(store)

    async def create(**kwargs: Any) -> Any:
        # The compose loop binds the turn's session operation context the same way.
        return await service.create_pending_interpretation_event(**kwargs, session_operation_context=context)

    try:
        result = await _handle_request_interpretation_review(
            arguments={"affected_node_id": "label", "kind": "vague_term", "user_term": "cool", "llm_draft": "Visually appealing."},
            state=state_from_record(saved),
            session_id=sid,
            composition_state_id=saved.id,
            tool_call_id="anchor-projection-tool",
            now=datetime.now(UTC),
            per_term_cap=3,
            per_session_day_cap=10,
            model_identifier="test-model",
            model_version="1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            create_pending_interpretation_event=create,
            list_interpretation_events=service.list_interpretation_events,
        )
    finally:
        repository.release(context)

    assert result.success is True
    assert result.data["_kind"] == "interpretation_review_pending"
    assert _state_rows(engine) == states_before, "a review card writes no composition state"
    events_after = _event_rows(engine)
    assert events_after[: len(events_before)] == events_before, "the open card on the other step is untouched"
    (new_row,) = events_after[len(events_before) :]
    assert (new_row["affected_node_id"], new_row["choice"], new_row["composition_state_id"]) == (
        "label",
        InterpretationChoice.PENDING.value,
        str(saved.id),
    )


@pytest.mark.asyncio
async def test_repeated_no_edit_surfacing_on_an_unchanged_state_succeeds_every_time(
    store: tuple[Any, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sticky case: pass, another no-edit turn's graph review on the SAME state, pass again.

    On base the FIRST pass already fails with ANCHOR_MESSAGE. On the fix both
    pass, and the second (with the newer graph review bound) writes nothing.
    """
    engine, _repository, _authority, service, _sid = store
    saved, _wired, _draft, event = await _seed(store, tmp_path, label_prompt=PLAIN_LABEL_PROMPT)
    calls = _count_creates(service, monkeypatch)

    await _surface(service, store, saved)
    first = list(calls)
    calls.clear()
    assert sorted(first) == [("classify", saved.id), ("label", saved.id)], "both open-review sites reach the create path"
    states_after_first = _state_rows(engine)
    events_after_first = _event_rows(engine)
    pending = [(row["affected_node_id"], row["id"]) for row in events_after_first if row["choice"] == InterpretationChoice.PENDING.value]
    assert sorted(node for node, _id in pending) == ["classify", "label"]
    assert ("classify", str(event.id)) in pending

    before = await service.get_current_state(saved.session_id)
    assert before is not None and before.review_projection is not None
    await _commit_graph_review(store, saved)
    current = await service.get_current_state(saved.session_id)
    assert current is not None and current.id == saved.id and current.review_projection is not None
    assert current.review_projection.review.terminal_operation_id != before.review_projection.review.terminal_operation_id, (
        "the second no-edit turn's graph review is the one now bound"
    )

    await _surface(service, store, saved)
    assert sorted(calls) == sorted(first)
    assert _state_rows(engine) == states_after_first
    assert _event_rows(engine) == events_after_first, "the repeat pass reconciles the same two cards"


def _interfere_after_snapshot(
    service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch, interfere: Callable[[], Awaitable[object]]
) -> list[int]:
    """Run ``interfere`` after the preflight read and blob snapshot, before the locked write."""
    original = service._prepare_inline_blob_snapshot
    ran: list[int] = []

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        snapshot = await original(*args, **kwargs)
        assert snapshot is not None, "the guard is armed only with an inline blob snapshot"
        await interfere()
        ran.append(1)
        return snapshot

    monkeypatch.setattr(service, "_prepare_inline_blob_snapshot", wrapped)
    return ran


@pytest.mark.asyncio
async def test_guard_still_refuses_an_anchor_row_rewritten_after_the_preflight_read(
    store: tuple[Any, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the stored anchor row changes (same id and version) between preflight and lock."""
    engine, repository, _authority, service, sid = store
    saved, _wired, draft, _event = await _seed(store, tmp_path)

    async def rewrite_anchor() -> None:
        # Same id and version, one stored column flipped: an ids-only or
        # version-only comparison would not see it.
        with engine.begin() as conn:
            conn.execute(
                update(composition_states_table).where(composition_states_table.c.id == str(saved.id)).values(is_valid=not saved.is_valid)
            )

    ran = _interfere_after_snapshot(service, monkeypatch, rewrite_anchor)
    events_before = _event_rows(engine)
    context = _acquire(store)
    try:
        with pytest.raises(AuditIntegrityError, match=ANCHOR_MESSAGE):
            await _create(service, sid, saved.id, draft, context)
    finally:
        repository.release(context)
    assert ran == [1]
    assert _event_rows(engine) == events_before


@pytest.mark.asyncio
async def test_guard_still_refuses_a_live_state_written_after_the_preflight_read(
    store: tuple[Any, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a new session head is saved between preflight and lock; the anchor is unchanged."""
    engine, repository, _authority, service, sid = store
    saved, wired, draft, _event = await _seed(store, tmp_path)
    context = _acquire(store)
    env = {"engine": engine, "session_id": str(sid), "data_dir": str(tmp_path), "operation": context, "authority": repository}
    later = replace(wired, metadata=PipelineMetadata(name="A later head"))
    ran = _interfere_after_snapshot(service, monkeypatch, lambda: _save_state(service, env, later))
    events_before = _event_rows(engine)
    try:
        with pytest.raises(AuditIntegrityError, match=LIVE_MESSAGE):
            await _create(service, sid, saved.id, draft, context)
    finally:
        repository.release(context)
    assert ran == [1]
    assert _event_rows(engine) == events_before
    assert [row["version"] for row in _state_rows(engine)] == [saved.version, saved.version + 1]


@pytest.mark.asyncio
async def test_blob_row_changed_after_snapshot_is_refused_by_blob_custody(
    store: tuple[Any, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the bound blob's row changes after the snapshot; ``assert_current_rows`` refuses first."""
    engine, repository, _authority, service, sid = store
    saved, _wired, draft, _event = await _seed(store, tmp_path)

    async def rename_blob() -> None:
        with engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.session_id == str(sid)).values(filename="renamed-after-snapshot.txt"))

    ran = _interfere_after_snapshot(service, monkeypatch, rename_blob)
    events_before = _event_rows(engine)
    context = _acquire(store)
    try:
        with pytest.raises(AuditIntegrityError, match=BLOB_MESSAGE):
            await _create(service, sid, saved.id, draft, context)
    finally:
        repository.release(context)
    assert ran == [1]
    assert _event_rows(engine) == events_before


@pytest.mark.asyncio
async def test_resolve_with_committed_graph_review_compares_projected_records(store: tuple[Any, ...], tmp_path: Path) -> None:
    """The resolve path compares effective with effective under the lock and still succeeds with a graph review bound."""
    engine, repository, _authority, service, sid = store
    saved, _wired, _draft, event = await _seed(store, tmp_path)

    context = _acquire(store)
    try:
        resolved, returned = await service.resolve_interpretation_event(
            session_id=sid,
            event_id=event.id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:alice",
            reviewed_anchor=await reviewed_anchor_for(service, sid, event.id),
            session_operation_context=context,
        )
    finally:
        repository.release(context)

    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    assert returned.version == saved.version + 1
    assert [row["version"] for row in _state_rows(engine)] == [saved.version, saved.version + 1]


# --- Fork: the composer's backend surfacing must open a card for every open prompt-review site on the fork head ---
#
# After a fork and a recompose, the fork head must carry a pending card for every open prompt-review site.
# On a fork made through the real route (blob copy and custody rewrite included), the surfacing entry the
# finalize calls writes one pending card per open site, and still does after a committed graph review is
# bound to the fork head.

FORK_CSV_TEXT = "text\nhello\n"


def _fork_app(tmp_path: Path) -> Any:

    data_dir = tmp_path / "data"
    for name in ("blobs", "outputs", "runs"):
        (data_dir / name).mkdir(parents=True)
    (data_dir / "payloads").mkdir(mode=0o700)
    app = create_app(
        settings=WebSettings(
            data_dir=data_dir,
            landscape_url=f"sqlite:///{data_dir}/runs/audit.db",
            payload_store_path=data_dir / "payloads",
            composer_max_composition_turns=15,
            composer_context_max_prompt_tokens=200_000,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=85.0,
            composer_boot_probe_enabled=False,
            composer_rate_limit_per_minute=10,
            # The fork route is driven directly; the async composer worker must not claim this test's operations.
            composer_async_scan_interval_seconds=60.0,
            shareable_link_signing_key=b"\x00" * 32,
        )
    )
    app.state.auth_provider.create_user("forkuser", "testpass123", display_name="Fork User")
    wire_test_pipeline_user_authority(app, identity_id="forkuser", engine=app.state.session_engine)
    return app


def _app_acquire(app: Any, sid: UUID) -> Any:
    service = app.state.session_service
    return service.session_operation_authority.acquire(
        session_id=sid,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )


def _app_store(app: Any, sid: UUID) -> tuple[Any, ...]:
    """The ``store`` tuple shape ``begin``/``blocked``/``finish`` use, over the app's own objects."""

    service = app.state.session_service
    engine = app.state.session_engine
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    return engine, service.session_operation_authority, authority, service, sid


async def _app_commit_graph_review(app: Any, sid: UUID, state: CompositionStateRecord) -> None:
    """``_commit_graph_review`` over the app: admit, claim and start under the app's owner, then commit a BLOCKED review."""

    store = _app_store(app, sid)
    _engine, repository, authority, service, _sid = store
    owner = service.session_operation_owner_instance_id
    request = SendMessageRequest(operation_id=str(uuid4()), content="Explain", state_id=state.id)
    record, _ = admit(authority, sid, request, actor="forkuser")
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id=owner, lease_seconds=30, auth_provider_type="local")
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
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
    terminal = await finish(store, running, blocked(store, running, state))
    assert terminal.status == "completed"
    repository.release(running.session_operation_context)


async def _seed_fork_parent(app: Any, sid: UUID) -> tuple[CompositionStateRecord, str]:
    """Parent head: uploaded CSV source, file-held classify prompt with an open card, inline label prompt with a pending review."""
    service = app.state.session_service
    engine = app.state.session_engine
    data_dir = Path(app.state.settings.data_dir)
    context = _app_acquire(app, sid)
    try:
        message = await service.add_message(
            sid, "user", f"{USER_MESSAGE_TEXT}\n{FORK_CSV_TEXT}", writer_principal="route_user_message", session_operation_context=context
        )
        env = {
            "engine": engine,
            "session_id": str(sid),
            "data_dir": str(data_dir),
            "operation": context,
            "authority": service.session_operation_authority,
        }
        csv_blob = blob_tools.execute_tool(
            "create_blob",
            {"filename": "input.csv", "mime_type": "text/csv", "content": FORK_CSV_TEXT},
            blob_tools._empty_state(),
            blob_tools._catalog(),
            data_dir=env["data_dir"],
            session_engine=engine,
            session_id=str(sid),
            session_operation_context=context,
            session_operation_authority=env["authority"],
            user_message_id=str(message.id),
            user_message_content=f"{USER_MESSAGE_TEXT}\n{FORK_CSV_TEXT}",
        )
        assert csv_blob.success, csv_blob.to_dict()
        with engine.connect() as conn:
            csv_path = conn.execute(
                select(blobs_table.c.storage_path).where(blobs_table.c.id == str(csv_blob.data["blob_id"]))
            ).scalar_one()
        base = _two_step_state(data_dir, label_prompt=PLAIN_LABEL_PROMPT)
        source = base.sources["source"]
        sink = base.outputs[0]
        base = replace(
            base,
            sources={"source": replace(source, options={**dict(source.options), "path": csv_path})},
            # Session-owned sink under the parent: the fork rebases it onto the child.
            outputs=(replace(sink, options={**dict(sink.options), "path": str(data_dir / "outputs" / str(sid) / "output.jsonl")}),),
        )
        wired = _wire_file(env, base, user_message_id=str(message.id))
        saved = await _save_state(service, env, wired)
        draft = prompt_review_draft_from_options(wired.nodes[0].options)
        assert draft is not None
        await _create(service, sid, saved.id, draft, context)
    finally:
        service.session_operation_authority.release(context)
    parent_prompt_marker = wired.nodes[0].options["prompt_template"]
    assert isinstance(parent_prompt_marker, Mapping)
    return saved, str(parent_prompt_marker["blob_ref"])


def _session_event_rows(engine: Any, sid: UUID) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(interpretation_events_table)
                .where(interpretation_events_table.c.session_id == str(sid))
                .order_by(interpretation_events_table.c.created_at, interpretation_events_table.c.id)
            ).mappings()
        ]


async def _surface_on(app: Any, sid: UUID, head: CompositionStateRecord) -> None:
    """The surfacing entry the no-tool finalize calls, with the finalize's default only_missing_evidence=False."""
    context = _app_acquire(app, sid)
    try:
        await surface_pending_interpretation_reviews_for_state(
            state_from_record(head),
            sessions_service=app.state.session_service,
            session_id=str(sid),
            current_state_id=str(head.id),
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="test-model",
            model_version="1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    finally:
        app.state.session_service.session_operation_authority.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_graph_review", [False, True], ids=["fresh_fork", "fork_head_with_committed_graph_review"])
async def test_fork_head_surfacing_opens_a_pending_card_for_every_open_prompt_review_site(tmp_path: Path, child_graph_review: bool) -> None:
    """A fork through the real route; surfacing on its head opens one pending card per open llm_prompt_template site.

    ``fork_head_with_committed_graph_review`` is the sticky shape on the fork: a no-edit END review committed on the
    fork head, then two surfacing passes. On the base tree it fails with ``ANCHOR_MESSAGE``.
    """

    app = _fork_app(tmp_path)
    service = app.state.session_service
    engine = app.state.session_engine
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": "forkuser", "password": "testpass123"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = await client.post("/api/sessions", headers=headers, json={"title": "Fork card rows"})
        assert created.status_code == 201, created.text
        parent_sid = UUID(created.json()["id"])
        parent_head, parent_prompt_blob = await _seed_fork_parent(app, parent_sid)
        context = _app_acquire(app, parent_sid)
        try:
            fork_point = await service.add_message(
                parent_sid,
                "user",
                "Change the tag step",
                writer_principal="route_user_message",
                composition_state_id=parent_head.id,
                session_operation_context=context,
            )
        finally:
            service.session_operation_authority.release(context)
        parent_events = _session_event_rows(engine, parent_sid)

        forked = await client.post(
            f"/api/sessions/{parent_sid}/fork",
            headers=headers,
            json={"operation_id": str(uuid4()), "from_message_id": str(fork_point.id), "new_message_content": "Change the tag step again"},
        )
        assert forked.status_code == 201, forked.text
        child_sid = UUID(forked.json()["session_id"])
        child_head = await service.get_current_state(child_sid)
        assert child_head is not None and child_head.session_id == child_sid
        child_state = state_from_record(child_head)
        child_marker = child_state.nodes[0].options["prompt_template"]
        assert isinstance(child_marker, Mapping) and child_marker["blob_ref"] != parent_prompt_blob, (
            "precondition: the fork rebased the file-held prompt"
        )
        assert inline_blob_refs(InlinePreflightState.from_stored_state(child_head)), "precondition: the fork head's prompt is file-held"
        open_sites = {node.id for node in child_state.nodes if prompt_review_draft_from_options(node.options) is not None}
        assert open_sites == {"classify", "label"}, "precondition: both steps carry an open prompt review on the fork head"
        assert _session_event_rows(engine, child_sid) == [], "precondition: a fork copies no interpretation events"

        if child_graph_review:
            await _app_commit_graph_review(app, child_sid, child_head)
            projected = await service.get_current_state(child_sid)
            assert projected is not None and projected.id == child_head.id
            assert projected.review_projection is not None, "precondition: a committed graph review is bound to the fork head"
            passes = 2
        else:
            passes = 1
        for _ in range(passes):
            await _surface_on(app, child_sid, child_head)

        child_events = _session_event_rows(engine, child_sid)
        pending_cards = [
            row
            for row in child_events
            if row["kind"] == InterpretationKind.LLM_PROMPT_TEMPLATE.value and row["choice"] == InterpretationChoice.PENDING.value
        ]
        assert sorted(row["affected_node_id"] for row in pending_cards) == sorted(open_sites), (
            "one pending card per open site, none duplicated"
        )
        assert all(row["composition_state_id"] == str(child_head.id) for row in pending_cards)
        assert all(row["tool_call_id"].startswith("backend_auto_surface:") for row in pending_cards)
        assert len(child_events) == len(pending_cards), "surfacing wrote nothing but the cards"
        assert _session_event_rows(engine, parent_sid) == parent_events, "the parent's review history is untouched"
