"""WS5 Task 5 extension of the collector death-matrix family (META-6.1: the
family is integration's; WS5 reviews and EXTENDS in its own module, never
editing the seven landed scenarios in ``test_barrier_process_death_matrix``).

The two additions the plan assigns to WS5:

1. **The ordinal-vs-arrival oracle, made DISCRIMINATING.** The landed
   scenarios (a)/(b)/(e) assert ordinal flush order through the real flush,
   but in every one of them the members ARRIVE in ordinal order (member 0
   held, member 1 arrives after the takeover), so a flush that sorted by
   ARRIVAL would pass all three — the C6 lesson (test_collector_happy_path's
   deliberately unsorted authored order). Here member 1 arrives FIRST and
   is the one held across the SIGKILL; member 0 arrives last, in the fresh
   process. Arrival order is therefore the REVERSE of opener ordinal by
   construction, pinned durably from ``token_work_items.ingest_sequence``,
   and the flush must still present ``[10, 20]``. The presented order is
   witnessed DURABLY, after both processes are dead: the release group's
   representative (its ``group_records.opener_token_id`` — the parent the
   release's ``token_parents`` rows name, per META-9.2, never
   ``batch_members``) must be the ordinal-0 member, not the first arrival,
   and the released token's payload carries the order the plugin saw.

2. **The satisfiability-gate happy path** (spec §8) on a REAL killed
   mid-group image — one member held, one live, no losses — before the
   takeover, and again once the roster has closed by release. The shared
   gate both resume surfaces call must never refuse either image.

Mutant of record (sort by arrival instead of ordinal at
``CollectorExecutor._execute_flush``, run from a HEAD export): this scenario
fails deterministically; the landed (a)/(b)/(e) each PASS when run alone —
that surviving set is the discrimination gap this module closes. Measured
caveat: (b) (both members restored, equal restore arrival times) flakes
under the mutant between combined runs — tie order is unrecoverable after a
takeover, which is decision 11's whole point, and why the oracle here is
built from the opener's ordinals rather than any arrival timestamp.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, TransformResult
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.core.checkpoint.serialization import checkpoint_loads
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import group_records_table, token_parents_table, token_work_items_table, tokens_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.barrier_coordination import BarrierJournalRestoreContext
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.collector import CollectorExecutor
from elspeth.engine.processor import RowProcessor
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from tests.e2e.recovery.test_barrier_process_death_matrix import (
    _COLLECTOR,
    _COLLECTOR_NODE,
    _MEMBER_ROWS,
    _T0,
    _arrive_collector,
    _assert_group_released_in_ordinal_order,
    _assert_killed_collector_image,
    _assert_satisfiable,
    _collector_plugin,
    _collector_processor,
    _expand_group_id,
    _fresh_database,
    _kill_at_seam,
    _member_token_id,
    _members_by_ordinal,
    _mint_expand_group,
    _new_barrier_factory,
    _rebuild_member,
    _RecordingSumBatch,
    _register_worker,
    _run_fresh_recovery,
)
from tests.integration.pipeline.test_barrier_intake_dispositions import RUN_ID, USURPER, _usurp_seat

if TYPE_CHECKING:
    from collections.abc import Callable


class _OrderWitnessingSumBatch(_RecordingSumBatch):
    """The landed recording plugin, plus a DURABLE witness: the released row
    carries ``presented`` — the member values in the order the plugin saw
    them — so the order survives into the release token's payload and can be
    read after every process is dead."""

    name = "sum_batch"
    determinism = Determinism.DETERMINISTIC

    def process(self, row: Any, ctx: Any) -> Any:
        result = super().process(row, ctx)
        if not isinstance(row, list):
            return result
        presented = ",".join(str(r["value"]) for r in row)
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(normalized_name="value", original_name="value", python_type=int, required=False, source="inferred"),
                FieldContract(normalized_name="count", original_name="count", python_type=int, required=False, source="inferred"),
                FieldContract(normalized_name="presented", original_name="presented", python_type=str, required=False, source="inferred"),
            ),
            locked=True,
        )
        summed = result.row.to_dict()
        return TransformResult.success(
            PipelineRow({"value": summed["value"], "count": summed["count"], "presented": presented}, contract),
            success_reason={"action": "aggregated"},
        )


def _witnessing_collector_executor(factory: RecorderFactory, clock: MockClock) -> CollectorExecutor:
    """``_real_collector_executor`` with the order-witnessing plugin registered."""
    executor = CollectorExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=TokenManager(factory.data_flow, step_resolver=lambda _node_id: 3),
        run_id=RUN_ID,
        step_resolver=lambda _node_id: 3,
        data_flow=factory.data_flow,
        clock=clock,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_collector(
        CollectorSettings(name=str(_COLLECTOR), plugin="sum_batch", input="pages", on_success="out"),
        ScopeSettings(name="document_pages", opener="explode", closer=str(_COLLECTOR), policy="require_all"),
        _COLLECTOR_NODE,
        _OrderWitnessingSumBatch(),
    )
    return executor


# ----- first-process action (module-level: crosses the spawn boundary) -----


def _run_collector_member_1_first_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Mint a two-member group; member 1 (ordinal 1, value 20) arrives FIRST and
    is adopted; pause before member 0 arrives. Arrival order is the reverse of
    opener ordinal by construction."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _witnessing_collector_executor(factory, clock)
    processor = _collector_processor(factory, executor, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    assert _arrive_collector(factory, processor, children[1], ingest_sequence=1) == []
    pause()
    _arrive_collector(factory, processor, children[0], ingest_sequence=2)


# ----- second-process action -----


def _resume_member_0_last(db: LandscapeDB, payload_path: str) -> None:
    """Fresh process: restore the held member 1, then member 0 — the LAST
    arrival but the FIRST ordinal — arrives here and closes the roster. The
    flush must present opener-ordinal order, and the gate must be satisfiable
    once the roster is closed by release."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    _register_worker(db, USURPER, role="leader")
    executor = _witnessing_collector_executor(factory, clock)
    processor: RowProcessor = _collector_processor(
        factory,
        executor,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="process-death-collector-ws5-order", barrier_scalars=None, batch_id_remap={}
        ),
    )
    assert processor.has_blocked_barrier_work() is True
    assert _collector_plugin(executor).batch_calls == 0
    _arrive_collector(factory, processor, _rebuild_member(factory, db, 0), ingest_sequence=2)
    _assert_group_released_in_ordinal_order(db, executor)  # the landed in-memory oracle: seen == [[10, 20]]
    assert processor.has_blocked_barrier_work() is False
    _assert_satisfiable(db, nested=False)  # spec §8 happy path AFTER release: closed by release, not stranded


# ----- durable witnesses (read in the parent, every process dead) -----


def _arrival_order(db: LandscapeDB) -> list[str]:
    """Member token ids in ARRIVAL order — the journal's ingest_sequence."""
    with db.connection() as conn:
        rows = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.ingest_sequence)
            .where(token_work_items_table.c.barrier_key.is_not(None))
            .order_by(token_work_items_table.c.ingest_sequence)
        ).all()
    return [row.token_id for row in rows]


def _release_witness(db: LandscapeDB, payload_path: str) -> tuple[str, str]:
    """(representative token id, presented order) from the durable release evidence.

    META-9.2: the release's ``token_parents`` rows are keyed by
    ``parent_token_id`` = the representative member (the release group's
    ``opener_token_id``), ordered by ``ordinal`` — never ``batch_members``.
    """
    with db.connection() as conn:
        representative = str(
            conn.execute(
                select(group_records_table.c.opener_token_id).where(group_records_table.c.closes_group_id == _expand_group_id(db))
            ).scalar_one()
        )
        release_children = (
            conn.execute(
                select(token_parents_table.c.token_id)
                .where(token_parents_table.c.parent_token_id == representative)
                .order_by(token_parents_table.c.ordinal)
            )
            .scalars()
            .all()
        )
        assert len(release_children) == 1, release_children
        data_ref = conn.execute(select(tokens_table.c.token_data_ref).where(tokens_table.c.token_id == release_children[0])).scalar_one()
    envelope = checkpoint_loads(FilesystemPayloadStore(Path(payload_path)).retrieve(data_ref).decode("utf-8"))
    return representative, str(envelope["data"]["presented"])


def _exercise_collector_out_of_order_arrival_death(tmp_path: Path) -> LandscapeDB:
    database_url, payload_path = _fresh_database(tmp_path, "collector-out-of-order.db")
    _kill_at_seam(database_url, _run_collector_member_1_first_seam, (payload_path,))
    # Killed image: exactly member 1 held (ordinal 1 adopted, ordinal 0 live).
    _assert_killed_collector_image(database_url, adopted_ordinals=(1,), page_completed=False)
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        _assert_satisfiable(killed_db, nested=False)  # spec §8 happy path BEFORE takeover: never a false refuse
    _run_fresh_recovery(database_url, _resume_member_0_last, (payload_path,))
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    member_0, member_1 = _member_token_id(recovered, 0), _member_token_id(recovered, 1)
    # The premise, durable: arrival order is the REVERSE of opener ordinal.
    assert _arrival_order(recovered) == [member_1, member_0]
    assert [token_id for token_id, _ordinal in _members_by_ordinal(recovered)] == [member_0, member_1]
    # The oracle, durable: the release is keyed by the ordinal-0 member (not the
    # first arrival) and the plugin saw the members in ordinal order.
    representative, presented = _release_witness(recovered, payload_path)
    assert representative == member_0, f"release representative must be the ordinal-0 member, got {representative}"
    assert presented == ",".join(str(row["value"]) for row in _MEMBER_ROWS)  # "10,20", never the arrival order "20,10"
    return recovered


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
def test_collector_out_of_order_arrival_death_flushes_by_ordinal_and_gate_never_refuses(tmp_path: Path) -> None:
    db = _exercise_collector_out_of_order_arrival_death(tmp_path)
    db.close()
