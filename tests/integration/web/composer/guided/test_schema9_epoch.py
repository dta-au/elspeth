from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from elspeth.web.sessions.schema import SessionSchemaError, initialize_session_schema


def test_current_schema_epoch_pair_is_deliberately_pinned() -> None:
    # Deliberate literal pin: an epoch bump must consciously update this test
    # (and the release docs the docs tests check), not slide through.
    # Landscape epoch 34: unified-lineage groundwork tables (token_lineage_frames,
    # group_records, group_losses) plus token_work_items.lineage_path_json; the
    # 0.8.0 release docs and CHANGELOG already state the 34 boundary.
    # Epoch 35: WS1b flip — branch_name/fork_group_id/expand_group_id and
    # token_outcomes.expected_branches_json retired as stored columns.
    # Epoch 36 (elspeth-8655045f98): coalesce_effects.group_id, nullable=False
    # with no defaulting branch — a genuine table-shape change, not a
    # same-shape widening, so it gets its own epoch rather than folding into
    # 35 (see schema.py's epoch comment for the full arch-M1 rationale).
    # Session epoch 49 (elspeth-3e28029d2f): composition_rejection_events
    # table added — durable session-side rejection reasons (operator ruling
    # 2026-09-02: session data, not Landscape data).
    # Session epoch 50 (elspeth-ed67eb9d0d): ck_proposal_events_type widened
    # with proposal.rebased — a guided settlement that carries a pending
    # proposal across the checkpoint it writes re-pins the proposal's base
    # and records the rebinding as an appended immutable lifecycle event.
    # Session epoch 51 (elspeth-4d6c0dd0f5): the multi-replica
    # session-operation substrate lands on top of 50 — persistent
    # session-operation authority, compatible-generation membership and
    # run-start coordination, cross-replica ticket/progress/rate state,
    # bounded cleanup claims, durable proposal blob-effect receipts, and
    # seven new ``runs`` ownership/cancellation columns. 48 and 50 already
    # name different shapes on the two merged lines, so the union takes
    # the next free integer. The Landscape epoch is unchanged.
    assert SESSION_SCHEMA_EPOCH == 51
    assert SQLITE_SCHEMA_EPOCH == 36


def test_epoch_40_session_store_fails_before_schema_use(tmp_path: Path) -> None:
    path = tmp_path / "epoch-40.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE elspeth_schema_identity SET schema_epoch = 40 WHERE store_kind = 'session'"))
        connection.execute(text("PRAGMA user_version = 40"))

    # The pin lives in test_current_schema_epoch_pair_is_deliberately_pinned;
    # here the regex tracks the constant so the behavior under test (stale DB
    # fails before schema use) survives deliberate epoch bumps.
    with pytest.raises(SessionSchemaError, match=rf"SESSION_SCHEMA_EPOCH={SESSION_SCHEMA_EPOCH}.*Delete the session DB file and restart"):
        initialize_session_schema(engine)


def test_epoch_35_session_store_fails_before_schema_use(tmp_path: Path) -> None:
    path = tmp_path / "epoch-35.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE elspeth_schema_identity SET schema_epoch = 35 WHERE store_kind = 'session'"))
        connection.execute(text("PRAGMA user_version = 35"))

    with pytest.raises(SessionSchemaError, match=rf"SESSION_SCHEMA_EPOCH={SESSION_SCHEMA_EPOCH}.*Delete the session DB file and restart"):
        initialize_session_schema(engine)
