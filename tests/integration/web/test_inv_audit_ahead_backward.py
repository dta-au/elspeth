"""Spec §4.1.2 / §1.4 NFR: state-ahead-of-audit is impossible at the
schema level. After any persist_compose_turn call, the SQL predicate
below must return zero rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest
import structlog
from sqlalchemy import insert, text
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

# ``_make_session`` lives in ``tests/integration/web/conftest.py`` — a
# duplicate of the unit-test conftest helper. Importing the helper
# here keeps the per-test session-insert site uniform with the rest
# of the suite.
from .conftest import _make_session as _make_session_row

_TEST_FENCE_NAMESPACE = UUID("6794cf0c-4b9d-40b9-ad19-d6f9afff30dd")


def _test_compose_context(session_id: str) -> SessionOperationContext:
    """The exact COMPOSE context ``_make_session`` seeds a matching fence for."""
    operation_id = str(uuid5(_TEST_FENCE_NAMESPACE, session_id))
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id,
            operation_id=operation_id,
            lease_token=f"test-compose-token-{operation_id}",
            operation_epoch=1,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def _make_session(conn, *, session_id: str, **kwargs) -> None:
    """The conftest session row plus the one live exact COMPOSE fence.

    These tests insert ``sessions`` rows by hand, so the retained fence the
    lifecycle would have written is absent and every fenced writer refuses.
    Seeding it here is the same repair ``tests/helpers/session_fences.py``
    performs for the async writers -- no production check is bypassed.
    """
    _make_session_row(conn, session_id=session_id, **kwargs)
    context = _test_compose_context(session_id)
    conn.execute(
        insert(session_operation_fences_table).values(
            session_id=session_id,
            operation_id=context.fence.operation_id,
            lease_token=context.fence.lease_token,
            operation_kind=context.operation_kind.value,
            owner_instance_id="inv-audit-ahead-backward-test-owner",
            operation_epoch=context.fence.operation_epoch,
            lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
            released_at=None,
        )
    )


class _FencedComposeTurnHarness(DualFencedSessionServiceHarness):
    """Supply the sync ``persist_compose_turn`` primitive its COMPOSE authority.

    The harness's other adapters are async and cannot cover this one: the sync
    primitive refuses to run inside an event loop. The context is the exact one
    ``_make_session`` seeded a fence for, so the writer's authority check runs
    for real -- no optional-context arm and no relaxed signature.
    """

    def persist_compose_turn(self, **kwargs):
        if "session_operation_context" not in kwargs:
            kwargs["session_operation_context"] = _test_compose_context(kwargs["session_id"])
        return super().persist_compose_turn(**kwargs)


@pytest.fixture
def service(tmp_path):
    """Service with an in-memory SQLite engine. The test runs
    end-to-end against the real production code paths
    (``create_session_engine`` + ``initialize_session_schema``);
    integration here means "exercises persist_compose_turn against
    a real SQLite engine," not "uses Docker" — see the conftest
    docstring for why integration and unit conftests are separate."""
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return _FencedComposeTurnHarness(
        eng,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger(),
    )


_BACKWARD_PREDICATE = """
SELECT cs.id
  FROM composition_states cs
  LEFT JOIN chat_messages cm
    ON cm.composition_state_id = cs.id AND cm.role = 'tool'
 WHERE cs.provenance = 'tool_call' AND cs.version > 0
   AND cm.id IS NULL
"""


@pytest.fixture
def populated_audit_db(service):
    """Initialized audit DB with successful and rolled-back compose-turn writes."""

    from sqlalchemy.exc import IntegrityError

    with service._engine.begin() as conn:
        _make_session(conn, session_id="populated_audit")
    service.persist_compose_turn(
        session_id="populated_audit",
        assistant_content="ok",
        redacted_assistant_tool_calls=({"id": "tc_populated_a", "function": {"name": "f"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_populated_a",
                '{"r": 1}',
                StatePayload(
                    data=CompositionStateData(),
                    derived_from_state_id=None,
                ),
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    with service._engine.begin() as conn:
        first_state_id = conn.execute(
            text("SELECT id FROM composition_states WHERE session_id='populated_audit' ORDER BY version DESC LIMIT 1")
        ).scalar_one()
    with pytest.raises(
        IntegrityError,
        match=(
            r"(UNIQUE.*chat_messages.*session_id.*tool_call_id"
            r"|uq_chat_messages_tool_call_id)"
        ),
    ):
        service.persist_compose_turn(
            session_id="populated_audit",
            assistant_content="duplicate",
            redacted_assistant_tool_calls=({"id": "tc_populated_a", "function": {"name": "f"}},),
            redacted_tool_rows=(
                RedactedToolRow(
                    "tc_populated_a",
                    "{}",
                    StatePayload(
                        data=CompositionStateData(),
                        derived_from_state_id=None,
                    ),
                ),
            ),
            parent_composition_state_id=None,
            expected_current_state_id=first_state_id,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
        )
    return service._engine


def test_no_state_row_without_tool_row(populated_audit_db):
    """The INV-AUDIT-AHEAD backward-direction post-condition is a SQL predicate."""

    with populated_audit_db.connect() as conn:
        orphans = conn.execute(text(_BACKWARD_PREDICATE)).fetchall()
    assert orphans == []


def test_backward_direction_holds_after_successful_persist(service):
    with service._engine.begin() as conn:
        _make_session(conn, session_id="b1")
    service.persist_compose_turn(
        session_id="b1",
        assistant_content="ok",
        redacted_assistant_tool_calls=({"id": "tc_a", "function": {"name": "f"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_a",
                '{"r": 1}',
                # B1 (Phase 1 plan-review synthesis): no ``version=``;
                # ``_insert_composition_state`` allocates under the lock.
                StatePayload(
                    data=CompositionStateData(),
                    derived_from_state_id=None,
                ),
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    with service._engine.begin() as conn:
        violations = conn.execute(text(_BACKWARD_PREDICATE)).fetchall()
    assert violations == [], f"backward-direction violation rows: {violations}"


def test_backward_direction_holds_after_integrity_error_rollback(service):
    """After a failed persist_compose_turn (rolled back transaction), no
    composition_states row should be visible."""
    from sqlalchemy.exc import IntegrityError

    with service._engine.begin() as conn:
        _make_session(conn, session_id="b2")
    # First successful turn.
    service.persist_compose_turn(
        session_id="b2",
        assistant_content="",
        redacted_assistant_tool_calls=({"id": "tc_x", "function": {"name": "f"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_x",
                "{}",
                # B1: no ``version=``; helper allocates under the lock.
                StatePayload(
                    data=CompositionStateData(),
                    derived_from_state_id=None,
                ),
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    with service._engine.begin() as conn:
        first_state_id = conn.execute(
            text("SELECT id FROM composition_states WHERE session_id='b2' ORDER BY version DESC LIMIT 1")
        ).scalar_one()
    # Second turn deliberately reuses tc_x to trigger the partial
    # unique index ``uq_chat_messages_tool_call_id`` (added in Task 2).
    with pytest.raises(
        IntegrityError,
        match=(
            r"(UNIQUE.*chat_messages.*session_id.*tool_call_id"
            r"|uq_chat_messages_tool_call_id)"
        ),
    ):
        service.persist_compose_turn(
            session_id="b2",
            assistant_content="",
            redacted_assistant_tool_calls=({"id": "tc_x", "function": {"name": "f"}},),
            redacted_tool_rows=(
                RedactedToolRow(
                    "tc_x",
                    "{}",
                    # B1: no ``version=``.
                    StatePayload(
                        data=CompositionStateData(),
                        derived_from_state_id=None,
                    ),
                ),
            ),
            parent_composition_state_id=None,
            expected_current_state_id=first_state_id,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
        )
    with service._engine.begin() as conn:
        violations = conn.execute(text(_BACKWARD_PREDICATE)).fetchall()
        assert violations == []
        # And exactly one tool_call provenance row from the first (successful) turn.
        state_count = conn.execute(
            text("SELECT COUNT(*) AS c FROM composition_states WHERE session_id='b2' AND provenance='tool_call'")
        ).scalar()
        assert state_count == 1


def test_turn_assistant_row_state_diverges_from_post_turn_settlement_state(service):
    """Pin the design record from elspeth-3574f87208 (ruling 2026-09-01).

    Within one persisted compose turn the assistant row binds to the
    PRE-turn state (``parent_composition_state_id``), while each
    state-mutating tool call inserts a NEW chained state carried on its
    tool row — and the finalization surfacing pass mints its
    ``backend_auto_surface:`` interpretation events against that POST-turn
    id (``AuditOutcome.current_state_id``), never the assistant row's. A
    frontend equality join from an event's ``composition_state_id`` to the
    turn's assistant row therefore does not exist on this path; the ticket
    records that finding so the join is not re-proposed. If a refactor
    ever makes these ids equal, this test flags that the recorded
    rationale has been invalidated — re-read the ticket before relying on
    either behaviour.
    """
    with service._engine.begin() as conn:
        _make_session(conn, session_id="divergence1")
    # Turn 1 establishes the pre-turn state the second turn hangs off.
    service.persist_compose_turn(
        session_id="divergence1",
        assistant_content="ok",
        redacted_assistant_tool_calls=({"id": "tc_d1", "function": {"name": "f"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_d1",
                '{"r": 1}',
                StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    with service._engine.begin() as conn:
        pre_turn_state_id = conn.execute(
            text("SELECT id FROM composition_states WHERE session_id='divergence1' ORDER BY version DESC LIMIT 1")
        ).scalar_one()
    outcome = service.persist_compose_turn(
        session_id="divergence1",
        assistant_content="turn 2",
        redacted_assistant_tool_calls=({"id": "tc_d2", "function": {"name": "f"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_d2",
                '{"r": 2}',
                StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
        ),
        parent_composition_state_id=pre_turn_state_id,
        expected_current_state_id=pre_turn_state_id,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    with service._engine.begin() as conn:
        assistant_state_id = conn.execute(
            text("SELECT composition_state_id FROM chat_messages WHERE session_id='divergence1' AND role='assistant' AND content='turn 2'")
        ).scalar_one()
        tool_state_id = conn.execute(
            text("SELECT composition_state_id FROM chat_messages WHERE session_id='divergence1' AND role='tool' AND tool_call_id='tc_d2'")
        ).scalar_one()
    # The assistant row carries the PRE-turn state...
    assert assistant_state_id == pre_turn_state_id
    # ...the settlement id the surfacers mint against is the tool row's
    # newly inserted state...
    assert outcome.current_state_id == tool_state_id
    # ...and the two are different rows, so the event↔assistant-row join
    # fails by construction on exactly the turns that mutate the pipeline.
    assert assistant_state_id != outcome.current_state_id


def test_get_messages_orders_assistant_before_tool_rows_within_one_turn(service):
    """B2 (Phase 1 plan-review synthesis): a single ``persist_compose_turn``
    stamps every row in the turn with one shared ``created_at`` = ``now``;
    on fast SQLite the rows share a microsecond, so ``get_messages``'s
    pre-B2 ``ORDER BY created_at`` returned them nondeterministically.
    Post-B2 ``get_messages`` orders by ``sequence_no`` (allocated under
    the session write lock = monotonic and unique), so the
    intra-turn order is the order in which the writer appended rows
    (assistant first, tool rows in plan order). This test would have
    failed on the pre-B2 codebase.
    """
    from uuid import UUID

    # B2 (Phase 1 plan-review synthesis): pre-B2 this test bound
    # ``sid="ord1"`` and ``sid_uuid=UUID("00000000-...-001")``, then
    # inserted the session under ``sid`` and queried under
    # ``sid_uuid`` — two different sessions. The fix derives one
    # canonical UUID and uses its string form for ``_make_session`` /
    # ``persist_compose_turn`` and the UUID form for ``get_messages``.
    sid_uuid = UUID("00000000-0000-0000-0000-000000000001")
    sid = str(sid_uuid)
    with service._engine.begin() as conn:
        _make_session(conn, session_id=sid)
    # B2: ``assistant_id`` and ``assistant_raw_content`` were stale
    # kwargs from a prior plan draft that ``persist_compose_turn`` does
    # not declare. ``assistant_id`` is helper-generated (the test never
    # referenced the supplied value); ``assistant_raw_content`` was a
    # typo for the new ``raw_content`` parameter and is left at its
    # default ``None`` here because the test's narrative does not
    # exercise the redaction-attribution path.
    service.persist_compose_turn(
        session_id=sid,
        assistant_content="ok",
        redacted_assistant_tool_calls=(
            {"id": "tc_a", "function": {"name": "f"}},
            {"id": "tc_b", "function": {"name": "g"}},
            {"id": "tc_c", "function": {"name": "h"}},
        ),
        # B1 (Phase 1 plan-review synthesis): no ``version=`` kwargs.
        # ``_insert_composition_state`` allocates per-session contiguous
        # versions (1, 2, 3) under _session_write_lock.
        redacted_tool_rows=(
            RedactedToolRow(
                "tc_a",
                "{}",
                StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
            RedactedToolRow(
                "tc_b",
                "{}",
                StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
            RedactedToolRow(
                "tc_c",
                "{}",
                StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )

    # ``get_messages`` is async (returns ChatMessageRecord objects); the
    # post-B2 ORDER BY sequence_no clause guarantees a stable order.
    import asyncio

    msgs = asyncio.run(service.get_messages(sid_uuid))
    roles = [m.role for m in msgs]
    # Exactly four rows from this turn: 1 assistant + 3 tool. No fork
    # rows, no system messages — _make_session left the chat empty.
    assert roles == ["assistant", "tool", "tool", "tool"], (
        f"intra-turn ordering broken: expected assistant before all tool rows, "
        f"got {roles!r} — see plan §14.7 (B2 fix). The pre-B2 ORDER BY created_at "
        f"would produce a nondeterministic permutation of these four roles."
    )
    # And the tool_call_id sequence is preserved (a→b→c, the order
    # ``redacted_tool_rows`` was supplied in).
    tool_ids = [m.tool_call_id for m in msgs if m.role == "tool"]
    assert tool_ids == ["tc_a", "tc_b", "tc_c"], f"intra-tool ordering broken: {tool_ids!r}"
