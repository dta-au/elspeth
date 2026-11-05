"""A paid semantic Review keeps its original D after END's retained tail overflows."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_checkpoint_end_complete_action import (
    require_original_end_outer_creator_after_paid_overflow_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import read_v4_checkpoint_end_settled_owed_history_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import read_v4_checkpoint_end_owed_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_semantic_actions import (
    read_original_dispatched_semantic_review_after_end_overflow_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture

operation_store = shared_fixture.operation_store


@pytest.mark.asyncio
async def test_actual_paid_semantic_review_end_overflow_retains_historical_dispatched_source(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=5_000_000))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)

    async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, end_reasoning_bytes=700_000) as case:
        result = case["execution"]
        assert type(result) is EndCheckpointPaidOverflowTerminal
        terminal = result.verified
        assert terminal.receipt.representation == "retained_tail"
        assert terminal.receipt.reason == "retained_payload_bytes"
        assert terminal.outer_effect_key == case["owner_effect_key"]
        assert tuple(name for name, _ in case["sdk"]) == ("semantic", "end")
        running = case["running"]
        session_id = running.claim.session_id
        operation_id = running.claim.operation_id
        names = case["runtime"].materialization.inventory.names

        with locked_session_transaction(store[0], str(session_id)) as conn:
            assert (
                read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
                    conn, session_id=session_id, operation_id=operation_id, effect_key=terminal.source.effect_key, names=names
                )
                == terminal
            )
            h_id = conn.execute(
                select(checkpoints.c.checkpoint_id)
                .where(
                    checkpoints.c.session_id == str(session_id),
                    checkpoints.c.operation_id == operation_id,
                    checkpoints.c.cursor < terminal.terminal.cursor,
                )
                .order_by(checkpoints.c.cursor.desc())
                .limit(1)
            ).scalar_one()
            owed = read_v4_checkpoint_end_settled_owed_history_on_connection(
                conn,
                session_id=str(session_id),
                operation_id=operation_id,
                attempt_id=terminal.provider_attempt_id,
                owed_checkpoint_id=h_id,
                names=names,
                child_disposition="paid_overflow",
            )
            assert owed.selected.checkpoint_id == h_id
            assert owed.outer_effect_key == terminal.outer_effect_key
            _sequence, _generation, admission = require_original_end_outer_creator_after_paid_overflow_on_connection(
                conn, owed=owed, names=names
            )
            assert admission is not None and admission.owner_effect_key == terminal.outer_effect_key
            assert admission.paid.source.effect_key == case["semantic_effect_key"]
            assert admission.paid.source.effect_key not in (terminal.outer_effect_key, terminal.source.effect_key)
            assert (
                read_original_dispatched_semantic_review_after_end_overflow_on_connection(
                    conn,
                    session_id=session_id,
                    operation_id=operation_id,
                    dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                    semantic_effect_key=case["semantic_effect_key"],
                    names=names,
                    terminal=terminal,
                )
                == admission
            )
            for substituted_key in (terminal.outer_effect_key, terminal.source.effect_key):
                with pytest.raises(AuditIntegrityError, match="Semantic SQL substituted its selected provider locator"):
                    read_original_dispatched_semantic_review_after_end_overflow_on_connection(
                        conn,
                        session_id=session_id,
                        operation_id=operation_id,
                        dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                        semantic_effect_key=substituted_key,
                        names=names,
                        terminal=terminal,
                    )
            # An altered terminal must fail its independent cold equality check;
            # this does not pretend to reach the later admitted-owner comparison.
            with pytest.raises(AuditIntegrityError, match="exact paid-overflow terminal"):
                read_original_dispatched_semantic_review_after_end_overflow_on_connection(
                    conn,
                    session_id=session_id,
                    operation_id=operation_id,
                    dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                    semantic_effect_key=case["semantic_effect_key"],
                    names=names,
                    terminal=replace(terminal, outer_effect_key=case["semantic_effect_key"]),
                )
            corrupted = conn.begin_nested()
            conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == str(session_id),
                    effects.c.operation_id == operation_id,
                    effects.c.effect_key == terminal.outer_effect_key,
                )
                .values(disposition="dispatched")
            )
            with pytest.raises(AuditIntegrityError):
                read_original_dispatched_semantic_review_after_end_overflow_on_connection(
                    conn,
                    session_id=session_id,
                    operation_id=operation_id,
                    dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                    semantic_effect_key=case["semantic_effect_key"],
                    names=names,
                    terminal=terminal,
                )
            corrupted.rollback()
