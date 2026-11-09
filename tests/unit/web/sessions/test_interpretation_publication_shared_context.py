"""Fix 13: publishing R review cards for one candidate prepares and reads shared context once.

Statement counts come from SQLAlchemy ``before_cursor_execute`` on a real
in-memory SQLite session database; preparation counts come from counting
seams on the service's preparation calls and on the repository's row decoder.
Run against the pre-fix tree, the count tests fail with the per-card and
R(R-1)/2 shapes (see ``.claude/lanes/perf-eval/fix13-implementation.md``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, select

import elspeth.web.sessions.service as service_module
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationKind,
    InterpretationSource,
    InterpretationSurfaceOrigin,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.coordination.repository import _RepositoryInterpretationMutations
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData, PreparedInterpretationEventDraft
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.sessions.test_interpretation_events_service import (
    _compose_operation,
    _insert_session,
    _save_composition_state,
    engine,
    service,
)

__all__ = ["engine", "service"]

_NODE_ID = "llm_transform_1"


def _node(node_id: str, terms: list[str]) -> dict[str, Any]:
    placeholders = " ".join(f"{{{{interpretation:{term}}}}}" for term in terms)
    return {
        "id": node_id,
        "node_type": "transform",
        "plugin": "llm",
        "input": "input",
        "on_success": "out",
        "on_error": "quarantine",
        "options": {"prompt_template": f"Rate {placeholders} this."},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }


def _state(terms: list[str], *, node_per_term: bool = False) -> CompositionStateData:
    nodes = [_node(f"{_NODE_ID}_{term}", [term]) for term in terms] if node_per_term else [_node(_NODE_ID, terms)]
    return CompositionStateData(sources={}, nodes=nodes, edges=[], outputs=[], metadata_={"name": "Fix 13", "description": ""})


def _drafts(terms: list[str], *, node_per_term: bool = False) -> tuple[PreparedInterpretationEventDraft, ...]:
    return tuple(
        PreparedInterpretationEventDraft(
            event_id=uuid4(),
            affected_node_id=f"{_NODE_ID}_{term}" if node_per_term else _NODE_ID,
            tool_call_id=f"backend_auto_surface:{uuid4()}",
            user_term=term,
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft=f"draft for {term}",
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="model",
            model_version="version",
            provider="test",
            composer_skill_hash=stable_hash("skill"),
        )
        for term in terms
    )


def _is_full_state_select(statement: str) -> bool:
    """A SELECT that transfers a whole ``composition_states`` row (graph columns included)."""
    text = statement.lstrip().upper()
    return text.startswith("SELECT") and "FROM COMPOSITION_STATES" in text and "COMPOSITION_STATES.NODES" in text


def _is_head_probe(statement: str) -> bool:
    text = " ".join(statement.split()).upper()
    return text.startswith("SELECT COMPOSITION_STATES.ID FROM COMPOSITION_STATES") and "ORDER BY COMPOSITION_STATES.VERSION DESC" in text


@contextmanager
def _observed(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def observe(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@contextmanager
def _counted_preparation(service: SessionServiceImpl) -> Iterator[dict[str, int]]:
    """Counting seams on every shared-preparation step and on the repository row decoder."""
    counts = {"principal": 0, "validation_inputs": 0, "inline_config": 0, "decodes": 0}
    principal = service._session_principal_context

    async def counted_principal(*args: Any, **kwargs: Any) -> Any:
        counts["principal"] += 1
        return await principal(*args, **kwargs)

    def counting(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:
            counts[name] += 1
            return original(*args, **kwargs)

        return counted

    decode = _RepositoryInterpretationMutations._state_record
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "_session_principal_context", counted_principal)
        patch.setattr(
            service_module,
            "build_interpretation_validation_inputs",
            counting("validation_inputs", service_module.build_interpretation_validation_inputs),
        )
        patch.setattr(
            SessionServiceImpl,
            "_inline_preflight_config",
            staticmethod(counting("inline_config", SessionServiceImpl._inline_preflight_config)),
        )
        patch.setattr(_RepositoryInterpretationMutations, "_state_record", classmethod(counting("decodes", decode.__func__)))
        yield counts


async def _publish(service: SessionServiceImpl, terms: list[str]) -> tuple[UUID, list[str], dict[str, int]]:
    session_id = uuid4()
    with service._engine.begin() as conn:
        _insert_session(conn, str(session_id))
    async with _compose_operation(service, session_id) as context:
        with _counted_preparation(service) as counts, _observed(service._engine) as statements:
            await service.save_composition_state_with_interpretations(
                session_id,
                _state(terms),
                provenance="tool_call",
                interpretations=_drafts(terms),
                session_operation_context=context,
            )
    return session_id, statements, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("cards", [1, 3, 5])
async def test_one_candidate_publication_prepares_once_and_reads_its_rows_once(service: SessionServiceImpl, cards: int) -> None:
    terms = [f"term{index}" for index in range(cards)]
    session_id, statements, counts = await _publish(service, terms)

    # Shared candidate context: principal/plugin snapshot, inline config and
    # validation inputs are prepared once per publication, not once per card.
    assert counts["principal"] == 1
    assert counts["validation_inputs"] == 1
    assert counts["inline_config"] == 1
    # The candidate is both anchor and head: one full-row read and one decode
    # inside the write transaction, plus the method's own final head read.
    # Pre-fix: 2 reads + 2 decodes per card, plus k surfacing reads for card k.
    assert sum(_is_full_state_select(s) for s in statements) == 2
    assert counts["decodes"] == 1
    # The head's identity is still re-probed (and locked) for every card.
    assert sum(_is_head_probe(s) for s in statements) == cards

    events = await service.list_interpretation_events(session_id, status="pending")
    assert sorted(e.user_term for e in events) == sorted(terms)
    assert {e.choice for e in events} == {InterpretationChoice.PENDING}
    assert len({e.composition_state_id for e in events}) == 1


@pytest.mark.asyncio
async def test_terms_at_one_node_and_kind_cost_linear_statements(service: SessionServiceImpl) -> None:
    """R terms at one (node, kind): card k used to load k surfacing graphs, R(R-1)/2 in all."""
    totals = {}
    for cards in (2, 4, 8):
        _session_id, statements, counts = await _publish(service, [f"t{index}" for index in range(cards)])
        totals[cards] = len(statements)
        assert sum(_is_full_state_select(s) for s in statements) == 2
        assert counts["decodes"] == 1
    # Exactly linear: doubling the card count doubles the marginal statements.
    assert totals[8] - totals[4] == 2 * (totals[4] - totals[2])


@pytest.mark.asyncio
async def test_single_card_does_not_load_other_term_surfacing_graphs(service: SessionServiceImpl) -> None:
    """The per-transaction surfacer arm: earlier cards for other terms are read as events only."""
    session_id = uuid4()
    with service._engine.begin() as conn:
        _insert_session(conn, str(session_id))
    terms = [f"t{index}" for index in range(5)]
    record = await _save_composition_state(service, session_id, _state(terms), provenance="tool_call")
    for term in terms[:4]:
        await service.create_pending_interpretation_event(
            session_id=session_id,
            composition_state_id=record.id,
            affected_node_id=_NODE_ID,
            tool_call_id=f"call-{term}",
            user_term=term,
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft=f"draft for {term}",
            model_identifier="model",
            model_version="version",
            provider="test",
            composer_skill_hash=stable_hash("skill"),
        )
    async with _compose_operation(service, session_id) as context:
        transaction_reads: list[str] = []
        mutate = service._session_operation_authority.mutate

        def observed_mutate(*args: Any, **kwargs: Any) -> Any:
            with _observed(service._engine) as statements:
                result = mutate(*args, **kwargs)
            transaction_reads.extend(statements)
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(service._session_operation_authority, "mutate", observed_mutate)
            created = await service.create_pending_interpretation_event(
                session_id=session_id,
                composition_state_id=record.id,
                affected_node_id=_NODE_ID,
                tool_call_id="call-t4",
                user_term="t4",
                kind=InterpretationKind.VAGUE_TERM,
                llm_draft="draft for t4",
                model_identifier="model",
                model_version="version",
                provider="test",
                composer_skill_hash=stable_hash("skill"),
                session_operation_context=context,
            )
    assert created.choice is InterpretationChoice.PENDING
    # Anchor == head: one full-row read in the write transaction; the four
    # other-term pending rows no longer each load their surfacing graph.
    assert sum(_is_full_state_select(s) for s in transaction_reads) == 1
    pending = await service.list_interpretation_events(session_id, status="pending")
    assert sorted(e.user_term for e in pending) == terms


@pytest.mark.asyncio
async def test_opted_out_cards_advance_the_head_for_the_next_card(service: SessionServiceImpl) -> None:
    """Each auto-resolved card appends a head; the next card must plan against that new head.

    One node per term: several vague terms in ONE prompt are refused from the
    second card onward under opt-out (the first bake changes the prompt the
    second card reviews), on the pre-fix tree as well.
    """
    session_id = uuid4()
    with service._engine.begin() as conn:
        _insert_session(conn, str(session_id))
    terms = ["t0", "t1", "t2"]
    async with _compose_operation(service, session_id) as context:
        await service.record_session_interpretation_opt_out(session_id=session_id, actor="user:alice", session_operation_context=context)
        with _counted_preparation(service) as counts:
            final = await service.save_composition_state_with_interpretations(
                session_id,
                _state(terms, node_per_term=True),
                provenance="tool_call",
                interpretations=_drafts(terms, node_per_term=True),
                session_operation_context=context,
            )
    events = [e for e in await service.list_interpretation_events(session_id, status="all") if e.kind is not None]
    assert [e.user_term for e in events] == terms
    assert {e.choice for e in events} == {InterpretationChoice.OPTED_OUT}
    assert {e.interpretation_source for e in events} == {InterpretationSource.AUTO_INTERPRETED_OPT_OUT}
    with service._engine.connect() as conn:
        rows = conn.execute(
            select(composition_states_table.c.id, composition_states_table.c.version, composition_states_table.c.derived_from_state_id)
            .where(composition_states_table.c.session_id == str(session_id))
            .order_by(composition_states_table.c.version)
        ).all()
    # Candidate, then one appended head per card, each derived from the previous head.
    assert len(rows) == 1 + len(terms)
    assert all(rows[index + 1].derived_from_state_id == rows[index].id for index in range(len(terms)))
    assert str(final.id) == rows[-1].id
    # The context advanced only when a card changed state: the candidate is
    # decoded once, then each new head once when the next card probes it.
    assert counts["decodes"] == len(terms)
    assert counts["principal"] == 1


@pytest.mark.asyncio
async def test_publication_context_refuses_another_candidate_or_fence(service: SessionServiceImpl) -> None:
    # Imported here so the count tests above stay runnable against the pre-fix tree.
    from elspeth.web.sessions.service import _PendingInterpretationPublication

    session_id = uuid4()
    with service._engine.begin() as conn:
        _insert_session(conn, str(session_id))
    state = _state(["t0"])
    other = _state(["t0"])
    async with _compose_operation(service, session_id) as context:
        publication = _PendingInterpretationPublication(proposed_state=state, session_operation_context=context, preparation_work=None)
        arguments: dict[str, Any] = {
            "session_id": session_id,
            "composition_state_id": uuid4(),
            "affected_node_id": _NODE_ID,
            "tool_call_id": "call-t0",
            "user_term": "t0",
            "kind": InterpretationKind.VAGUE_TERM,
            "llm_draft": "draft for t0",
            "model_identifier": "model",
            "model_version": "version",
            "provider": "test",
            "composer_skill_hash": stable_hash("skill"),
            "session_operation_context": context,
            "surface_origin": InterpretationSurfaceOrigin.COMPOSER_LLM,
            "_prepare_only": True,
            "_publication": publication,
        }
        # An equal-but-distinct candidate is not the bound candidate.
        with pytest.raises(AuditIntegrityError, match="belongs to another candidate"):
            await service._prepare_or_create_pending_interpretation_event(**arguments, proposed_state=other)
        with pytest.raises(AuditIntegrityError, match="belongs to another candidate"):
            await service._prepare_or_create_pending_interpretation_event(**arguments, proposed_state=None)
        first = await service._prepare_or_create_pending_interpretation_event(**arguments, proposed_state=state)
        second = await service._prepare_or_create_pending_interpretation_event(**{**arguments, "user_term": "t0 "}, proposed_state=state)
        assert first.validator is second.validator
        assert first.command.event_id != second.command.event_id
    with pytest.raises(AuditIntegrityError, match="exactly once"):
        publication.retain(first.validator)
