"""Real PostgreSQL proofs for durable coalesce-effect contention."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import leader_coordination_token, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.audit import Token, TokenRef
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import coalesce_effects_table, token_parents_table, tokens_table
from elspeth.core.payload_store import FilesystemPayloadStore

pytestmark = pytest.mark.testcontainer

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def _wait_for_blocker(db: LandscapeDB, *, waiter: int, blocker: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.engine.connect() as conn:
            blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter)).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError(f"PostgreSQL did not report pid {waiter} waiting for {blocker}")


@pytest.mark.timeout(120)
def test_identical_coalesce_writers_serialize_on_parents_and_reuse_one_effect(
    postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser blocks on the first leader fence, then reuses the winner's receipt."""
    first_db = LandscapeDB.from_url(postgres_url)
    second_db = LandscapeDB.from_url(postgres_url)
    payload_root = tmp_path / "payloads"
    first = RecorderFactory(first_db, payload_store=FilesystemPayloadStore(payload_root))
    second = RecorderFactory(second_db, payload_store=FilesystemPayloadStore(payload_root))
    run = first.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(first, run.run_id)
    source_id = register_test_node(first.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
    coalesce_id = register_test_node(
        first.data_flow,
        run.run_id,
        "coalesce",
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
    )
    row, root = first.data_flow.create_row_with_token(
        coordination_token=leader,
        source_node_id=source_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"source": True},
    )
    # The parents must share one innermost FORK lineage frame: a coalesce
    # closes exactly its own fork frame (spec rulings 24/28; the META-38 guard
    # in coalesce_tokens refuses frame-less parents before any effect write),
    # so mint them through the real fork path rather than as bare tokens.
    claimed_root = first.scheduler.enqueue_ready_claimed(
        member_token=leader.membership,
        token_id=root.token_id,
        row_id=row.row_id,
        node_id=None,
        step_index=3,
        ingest_sequence=0,
        row_payload_json="{}",
        lease_owner=leader.worker_id,
        lease_seconds=300,
    )
    parents, _fork_group_id = first.data_flow.fork_token(
        TokenRef(token_id=root.token_id, run_id=run.run_id),
        row.row_id,
        ["left", "right"],
        step_in_pipeline=3,
        member_token=leader.membership,
        work_item=claimed_root,
    )
    refs = tuple(TokenRef(token_id=token.token_id, run_id=run.run_id) for token in parents)
    state_ids = tuple(
        first.execution.begin_node_state(
            token_id=ref.token_id,
            node_id=coalesce_id,
            member_token=leader.membership,
            step_index=4,
            input_data={"ordinal": ordinal},
        ).state_id
        for ordinal, ref in enumerate(refs)
    )

    winner_has_authority = threading.Event()
    loser_attempting_authority = threading.Event()
    loser_has_authority = threading.Event()
    release_winner = threading.Event()
    backend_pids: dict[str, int] = {}
    original_first_lock = first.data_flow.tokens._lock_coalesce_dependencies
    original_second_lock = second.data_flow.tokens._lock_coalesce_dependencies

    def pause_winner(
        conn: Connection,
        *,
        parent_refs: Sequence[TokenRef],
        parent_state_ids: Sequence[str] | None,
        coalesce_node_id: str | None,
    ) -> None:
        original_first_lock(
            conn,
            parent_refs=parent_refs,
            parent_state_ids=parent_state_ids,
            coalesce_node_id=coalesce_node_id,
        )
        backend_pids["winner"] = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        winner_has_authority.set()
        assert release_winner.wait(timeout=10)

    def observe_loser(
        conn: Connection,
        *,
        parent_refs: Sequence[TokenRef],
        parent_state_ids: Sequence[str] | None,
        coalesce_node_id: str | None,
    ) -> None:
        original_second_lock(
            conn,
            parent_refs=parent_refs,
            parent_state_ids=parent_state_ids,
            coalesce_node_id=coalesce_node_id,
        )
        loser_has_authority.set()

    def observe_loser_fence(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if threading.current_thread().name == "coalesce-loser" and statement.lstrip().upper().startswith("UPDATE RUN_COORDINATION"):
            backend_pids["loser"] = int(conn.connection.driver_connection.info.backend_pid)
            loser_attempting_authority.set()

    event.listen(second_db.engine, "before_cursor_execute", observe_loser_fence)
    monkeypatch.setattr(first.data_flow.tokens, "_lock_coalesce_dependencies", pause_winner)
    monkeypatch.setattr(second.data_flow.tokens, "_lock_coalesce_dependencies", observe_loser)
    results: dict[str, Token | BaseException] = {}

    def materialize(name: str, factory: RecorderFactory) -> None:
        try:
            results[name] = factory.data_flow.coalesce_tokens(
                parent_refs=list(refs),
                row_id=row.row_id,
                coalesce_node_id=coalesce_id,
                parent_state_ids=state_ids,
                merged_payload={"merged": True},
                merged_contract=_CONTRACT,
                step_in_pipeline=4,
                coordination_token=leader,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            results[name] = exc

    threads = [
        threading.Thread(target=materialize, name="coalesce-winner", args=("winner", first)),
        threading.Thread(target=materialize, name="coalesce-loser", args=("loser", second)),
    ]
    try:
        threads[0].start()
        assert winner_has_authority.wait(timeout=10)
        threads[1].start()
        assert loser_attempting_authority.wait(timeout=10)
        _wait_for_blocker(first_db, waiter=backend_pids["loser"], blocker=backend_pids["winner"])
        assert not loser_has_authority.is_set()
        release_winner.set()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        assert set(results) == {"winner", "loser"}
        failures = {name: f"{type(result).__name__}: {result}" for name, result in results.items() if isinstance(result, BaseException)}
        assert not failures, failures
        winner = results["winner"]
        loser = results["loser"]
        assert isinstance(winner, Token)
        assert winner == loser
        assert backend_pids["winner"] != backend_pids["loser"]

        with first_db.connection() as conn:
            assert conn.execute(select(func.count()).select_from(coalesce_effects_table)).scalar_one() == 1
            # root + two fork children + ONE merged token: the loser reused
            # the winner's effect instead of minting a second merged token.
            assert conn.execute(select(func.count()).select_from(tokens_table)).scalar_one() == 4
            # two fork links (root -> left/right) + two coalesce links, and the
            # merged token's ordered parents are exactly the fork children.
            assert conn.execute(select(func.count()).select_from(token_parents_table)).scalar_one() == 4
            merged_parents = (
                conn.execute(
                    select(token_parents_table.c.parent_token_id)
                    .where(token_parents_table.c.token_id == winner.token_id)
                    .order_by(token_parents_table.c.ordinal)
                )
                .scalars()
                .all()
            )
            assert tuple(merged_parents) == tuple(ref.token_id for ref in refs)
    finally:
        release_winner.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=30)
        event.remove(second_db.engine, "before_cursor_execute", observe_loser_fence)
        first_db.close()
        second_db.close()
