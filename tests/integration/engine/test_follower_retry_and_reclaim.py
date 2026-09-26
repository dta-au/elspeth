"""A follower applies the run's retry policy, and a re-claim never reuses an attempt.

RULINGS 2026-09-26 Q2 (specialist review q2-engine + q2-systems). Two changes
land together and are pinned here against a real SQLite Landscape, a real
follower built by ``build_follower_processor`` and the leader's real lease
sweep (``recover_expired_leases``); no mock replaces membership, claim,
traversal, retry or disposition.

1. **One retry authority.** An ``elspeth join`` follower builds its
   ``RetryManager`` from the run's ``settings.retry`` (admission makes the
   follower's config_hash equal to the leader's). A transient failure is then
   retried the same way whichever process claimed the row: the same
   node_state attempts, the same ``retry_exhausted`` reason and ``attempts``
   count, never the old follower-only ``transient_error_no_retry``.

2. **The re-claim attempt base comes from the Tier-1 record.** A retry writes
   node_state attempts ``offset, offset+1, ...`` inside ONE claim without
   moving the scheduler attempt; a lease rotation moves the scheduler attempt
   by exactly one. Before the fix the re-claim started at
   ``claimed.attempt - 1`` and re-inserted an attempt the lost claim had
   already written: ``UNIQUE(token_id, step_index, attempt)`` refused it, the
   reclaimer raised a Tier-1 ``LandscapeRecordError`` and the token was left
   with a FAILED work item and no outcome. The claim now starts above the
   token's recorded maximum.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import Determinism, PluginSchema, RunStatus
from elspeth.contracts.config import RuntimeRetryConfig
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import FrameworkBugError, OrchestrationInvariantError
from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import stable_hash
from elspeth.core.config import ElspethSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    node_states_table,
    token_outcomes_table,
    token_work_items_table,
    transform_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator.follower import FollowerProcessor, build_follower_processor
from elspeth.engine.orchestrator.graph_wiring import assign_plugin_node_ids, build_source_id_map, load_edge_map
from elspeth.engine.orchestrator.processor_factory import build_row_processor
from elspeth.engine.orchestrator.types import PipelineConfig
from elspeth.engine.processor import RowProcessor
from elspeth.engine.scheduler_drain import ProcessorMode
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.infrastructure import templates
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.templates import SandboxedTemplate
from tests.fixtures.base_classes import _TestSchema, create_observed_contract
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.fixtures.plugins import CollectSink, ListSource
from tests.integration.engine.test_multi_source_chaos import (
    _CLOCK_EPOCH,
    _LEASE_EXPIRY_ADVANCE,
    _build_two_source_pipeline,
    _scheduler_events,
)
from tests.integration.plugins.transforms.test_template_worker_loss import (
    _LOSS_MARKER_ENV,
    _LOST_TEXT,
    _worker_lost_on_the_first_request,
)

# The run's retry policy, parsed the way `elspeth run` / `elspeth join` parse it.
# max_delay_seconds caps the jittered backoff, so the retried attempts stay fast.
_SETTINGS_YAML = """
sources:
  stub:
    plugin: csv
    on_success: output
    options:
      path: /nonexistent/never-instantiated.csv
      on_validation_failure: discard
      schema:
        mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: /nonexistent/never-instantiated.jsonl
      format: jsonl
      schema:
        mode: observed
retry:
  max_attempts: 3
  initial_delay_seconds: 0.01
  max_delay_seconds: 0.1
"""


def _run_settings() -> ElspethSettings:
    return load_settings_from_yaml_string(_SETTINGS_YAML)


# ---------------------------------------------------------------------------
# A scripted transform and the run it executes in
# ---------------------------------------------------------------------------

# Script actions, one per plugin call; the last action repeats.
_TRANSIENT = "transient"  # raise ConnectionError (engine-retryable)
_BUST = "bust"  # the leader's sweep rotates this claim's lease, then succeed
_OK = "ok"


class _ScriptedTransform(BaseTransform):
    """Transform whose Nth call does the Nth scripted action."""

    name = "scripted_retry_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema: ClassVar[type[PluginSchema]] = _TestSchema
    output_schema: ClassVar[type[PluginSchema]] = _TestSchema

    def __init__(
        self,
        script: tuple[str, ...],
        *,
        input_connection: str,
        on_success: str,
        bust: Callable[[str], None],
        on_call: Callable[[], None] | None = None,
    ) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.input = input_connection
        self.on_success = on_success
        self.on_error = "discard"
        self._script = script
        self._bust = bust
        self._on_call = on_call
        self.calls = 0

    def process(self, row: Any, ctx: Any) -> TransformResult:
        action = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if self._on_call is not None:
            self._on_call()
        if action == _TRANSIENT:
            raise ConnectionError("scripted transient failure")
        if action == _BUST:
            self._bust(ctx.run_id)
        return TransformResult.success(row, success_reason={"action": "test"})


class _RenderTransform(BaseTransform):
    """Renders the row through the real sandboxed render worker."""

    name = "follower_render_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema: ClassVar[type[PluginSchema]] = _TestSchema
    output_schema: ClassVar[type[PluginSchema]] = _TestSchema

    def __init__(self, *, input_connection: str, on_success: str) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.input = input_connection
        self.on_success = on_success
        self.on_error = "discard"
        self._template = SandboxedTemplate("{{ row.value }}")
        self.calls = 0

    def process(self, row: Any, ctx: Any) -> TransformResult:
        self.calls += 1
        out = {**row.to_dict(), "rendered": self._template.render(row={"value": row["value"]})}
        return TransformResult.success(PipelineRow(out, create_observed_contract(out)), success_reason={"action": "processed"})


@dataclass
class _Run:
    """One run seated by a leader, with its graph registered and one token READY."""

    db: LandscapeDB
    factory: RecorderFactory
    leader: CoordinationToken
    clock: MockClock
    payload_store: FilesystemPayloadStore
    config: PipelineConfig
    graph: Any
    transforms: list[BaseTransform]
    token_id: str

    @property
    def run_id(self) -> str:
        return self.leader.run_id

    def follower(self, worker_id: str = "worker:retry-follower", *, heartbeat_seconds: int = 60) -> tuple[FollowerProcessor, PluginContext]:
        # worker_id is the run_workers primary key: scope it to the run so
        # runs sharing one database (the PostgreSQL module) never collide.
        member = self.factory.run_coordination.admit_follower(
            run_id=self.run_id, worker_id=f"{worker_id}:{self.run_id}", config_hash=stable_hash({}), window_seconds=80
        )
        follower = build_follower_processor(
            factory=self.factory,
            member_token=member,
            graph=self.graph,
            config=self.config,
            payload_store=self.payload_store,
            clock=self.clock,
            scheduler_heartbeat_seconds=heartbeat_seconds,
            retry_config=RuntimeRetryConfig.from_settings(_run_settings().retry),
        )
        ctx = PluginContext(
            run_id=self.run_id,
            config={},
            landscape=self.factory.plugin_audit_writer(),
            payload_store=self.payload_store,
            member_token=member,
        )
        for transform in self.transforms:
            transform.on_start(ctx)
        return follower, ctx

    def leader_processor(self) -> tuple[RowProcessor, PluginContext]:
        """The leader/resume processor, built by the same builder `elspeth run` uses."""
        source_ids = build_source_id_map(self.graph)
        assign_plugin_node_ids(
            sources=self.config.sources,
            transforms=self.config.transforms,
            sinks=self.config.sinks,
            source_id_map=source_ids,
            transform_id_map=self.graph.get_transform_id_map(),
            sink_id_map=self.graph.get_sink_id_map(),
            aggregation_node_ids=frozenset(self.graph.get_aggregation_id_map().values()),
        )
        processor, _coalesce_map, _coalesce_executor = build_row_processor(
            graph=self.graph,
            config=self.config,
            settings=_run_settings(),
            factory=self.factory,
            run_id=self.run_id,
            source_id=source_ids["orders"],
            edge_map=load_edge_map(self.factory.data_flow, self.run_id),
            route_resolution_map=self.graph.get_route_resolution_map(),
            config_gate_id_map=self.graph.get_config_gate_id_map(),
            coalesce_id_map=self.graph.get_coalesce_id_map(),
            payload_store=self.payload_store,
            span_factory=SpanFactory(),
            clock=self.clock,
            max_workers=None,
            telemetry=None,
            mode=ProcessorMode.LEADER,
            coordination_token=self.leader,
        )
        ctx = PluginContext(
            run_id=self.run_id,
            config={},
            landscape=self.factory.plugin_audit_writer(),
            payload_store=self.payload_store,
            member_token=self.leader.membership,
        )
        for transform in self.transforms:
            transform.on_start(ctx)
        return processor, ctx

    # -- durable read-backs ------------------------------------------------

    def node_states(self, transform: BaseTransform) -> list[tuple[int, str]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                select(node_states_table.c.attempt, node_states_table.c.status)
                .where(node_states_table.c.token_id == self.token_id)
                .where(node_states_table.c.node_id == transform.node_id)
                .order_by(node_states_table.c.attempt)
            ).all()
        return [(int(row.attempt), str(row.status)) for row in rows]

    def node_state_errors(self, transform: BaseTransform) -> list[Any]:
        with self.db.connection() as conn:
            rows = conn.execute(
                select(node_states_table.c.error_json)
                .where(node_states_table.c.token_id == self.token_id)
                .where(node_states_table.c.node_id == transform.node_id)
                .order_by(node_states_table.c.attempt)
            ).all()
        return [None if row.error_json is None else json.loads(row.error_json) for row in rows]

    def work_items(self) -> list[tuple[str, int, str | None, str | None]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                select(
                    token_work_items_table.c.status,
                    token_work_items_table.c.attempt,
                    token_work_items_table.c.pending_sink_name,
                    token_work_items_table.c.pending_outcome,
                ).where(token_work_items_table.c.token_id == self.token_id)
            ).all()
        return [(str(r.status), int(r.attempt), r.pending_sink_name, r.pending_outcome) for r in rows]

    def transform_errors(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                select(transform_errors_table.c.error_details_json, transform_errors_table.c.destination).where(
                    transform_errors_table.c.token_id == self.token_id
                )
            ).all()
        return [{**json.loads(row.error_details_json), "destination": row.destination} for row in rows]

    def outcomes(self) -> list[tuple[str | None, str, bool]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                select(token_outcomes_table.c.outcome, token_outcomes_table.c.path, token_outcomes_table.c.completed).where(
                    token_outcomes_table.c.token_id == self.token_id
                )
            ).all()
        return [(row.outcome, str(row.path), bool(row.completed)) for row in rows]


def _begin_run(
    tmp_path: Path,
    build_transforms: Callable[[Callable[[str], None], MockClock], list[BaseTransform]],
    *,
    db_url: str | None = None,
) -> _Run:
    """Seat a leader, register the graph and put one source-complete token READY at the first transform.

    ``db_url`` selects the Landscape backend: SQLite under ``tmp_path`` by
    default; the PostgreSQL testcontainer module passes its server URL.
    """
    clock = MockClock(start=_CLOCK_EPOCH)
    db = LandscapeDB(db_url if db_url is not None else f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    factory = make_factory(db, payload_store=payload_store)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1", leader_worker_id=f"retry-leader:{uuid4().hex}")
    leader = leader_coordination_token(factory, run.run_id)

    def rotate_the_in_flight_lease(run_id: str) -> None:
        """The leader's live sweep reaps this claim's lease while the plugin is inside it."""
        clock.advance(_LEASE_EXPIRY_ADVANCE)
        with db.engine.begin() as conn:
            aged = conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == TokenWorkStatus.LEASED.value)
                .values(lease_expires_at=read_landscape_transaction_time(conn) - timedelta(seconds=1))
            )
            assert aged.rowcount == 1, f"expected exactly the in-flight lease to be LEASED, matched {aged.rowcount}"
        recovered = TokenSchedulerRepository(db.engine).recover_expired_leases(coordination_token=leader, stall_budget_seconds=0)
        assert recovered == 1

    transforms = build_transforms(rotate_the_in_flight_lease, clock)
    orders = ListSource([{"src": "orders", "value": 0}], name="orders_source", on_success="inbound")
    config, graph = _build_two_source_pipeline(
        {"orders": orders}, transforms, {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}
    )
    for node in graph.get_nodes():
        register_test_node(factory.data_flow, run.run_id, node.node_id, node_type=node.node_type, plugin_name=node.plugin_name)
    for edge in graph.get_edges():
        factory.data_flow.register_edge(edge.from_node, edge.to_node, edge.label, edge.mode, coordination_token=leader)
    source_ids = build_source_id_map(graph)
    factory.run_lifecycle.record_run_source(
        source_node_id=source_ids["orders"],
        source_name="orders",
        plugin_name=config.sources["orders"].name,
        config_hash=stable_hash({}),
        lifecycle_state="loaded",
        coordination_token=leader,
    )
    # The READY item targets the first transform's node, read from the graph.
    first_node = graph.get_next_node(source_ids["orders"])
    assert first_node is not None
    data = {"src": "orders", "value": 0}
    row, token = factory.data_flow.create_row_with_token(
        source_ids["orders"], 0, data, source_row_index=0, ingest_sequence=0, coordination_token=leader
    )
    factory.execution.record_completed_node_state(token.token_id, source_ids["orders"], 0, data, data, 0.0, coordination_token=leader)
    factory.scheduler.enqueue_ready(
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=first_node,
        step_index=graph.get_node_step_map()[first_node],
        ingest_sequence=0,
        row_payload_json=factory.scheduler.serialize_row_payload(PipelineRow(data, create_observed_contract(data))),
        member_token=leader.membership,
    )
    return _Run(
        db=db,
        factory=factory,
        leader=leader,
        clock=clock,
        payload_store=payload_store,
        config=config,
        graph=graph,
        transforms=transforms,
        token_id=token.token_id,
    )


def _rotations(run: _Run) -> list[tuple[int, int]]:
    return [
        (int(event["from_attempt"]), int(event["to_attempt"]))
        for event in _scheduler_events(run.db, run.run_id)
        if event["token_id"] == run.token_id and event["event_type"] == SchedulerEventType.RECOVER_EXPIRED_LEASE.value
    ]


# ---------------------------------------------------------------------------
# T1 / T3: retry inside a claim, then a lease rotation, then a re-claim
# ---------------------------------------------------------------------------


def _retry_then_rotate(tmp_path: Path, db_url: str | None) -> tuple[_Run, _ScriptedTransform]:
    """Claim #1 on a follower: attempt 0 transient, attempt 1 loses its lease to the leader's sweep."""
    holder: list[_ScriptedTransform] = []

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        transform = _ScriptedTransform((_TRANSIENT, _BUST, _OK), input_connection="inbound", on_success="output", bust=bust)
        holder.append(transform)
        return [transform]

    run = _begin_run(tmp_path, build, db_url=db_url)
    (transform,) = holder
    follower, ctx = run.follower()
    # The post-call heartbeat observes the rotation and abandons the claim:
    # the drain returns without a disposition and without raising.
    assert follower._processor.drain_follower_ready_work(ctx) == []
    assert run.node_states(transform) == [(0, "failed"), (1, "open")]
    assert run.work_items() == [(TokenWorkStatus.READY.value, 2, None, None)]
    assert _rotations(run) == [(1, 2)]
    return run, transform


@pytest.mark.parametrize("reclaimer", ["same_follower", "other_follower"])
def test_a_follower_reclaim_after_an_in_claim_retry_writes_a_fresh_attempt(tmp_path: Path, reclaimer: str) -> None:
    """T1: the re-claim starts above the token's recorded attempts, whoever re-claims it."""
    scenario_follower_reclaim_after_an_in_claim_retry(tmp_path, reclaimer, db_url=None)


def scenario_follower_reclaim_after_an_in_claim_retry(tmp_path: Path, reclaimer: str, *, db_url: str | None) -> None:
    run, transform = _retry_then_rotate(tmp_path, db_url)
    if reclaimer == "same_follower":
        follower, ctx = run.follower("worker:retry-follower-again")
    else:
        follower, ctx = run.follower("worker:retry-follower-2")

    results = follower._processor.drain_follower_ready_work(ctx)

    assert len(results) == 1
    # Attempt 1 stays OPEN: the lost claim abandoned it on purpose. The
    # re-claim writes attempt 2, not a second attempt 1.
    assert run.node_states(transform) == [(0, "failed"), (1, "open"), (2, "completed")]
    assert run.work_items() == [(TokenWorkStatus.PENDING_SINK.value, 2, "output", "success")]
    assert run.transform_errors() == []
    assert transform.calls == 3


def test_the_leader_reclaiming_a_rotated_follower_item_completes_it(tmp_path: Path) -> None:
    """T3: the leader drains through the same claim path; the item completes, the run is not aborted."""
    scenario_leader_reclaims_a_rotated_follower_item(tmp_path, db_url=None)


def scenario_leader_reclaims_a_rotated_follower_item(tmp_path: Path, *, db_url: str | None) -> None:
    run, transform = _retry_then_rotate(tmp_path, db_url)
    processor, ctx = run.leader_processor()

    results = processor.drain_scheduled_work(ctx)

    assert [result.token.token_id for result in results] == [run.token_id]
    assert run.node_states(transform) == [(0, "failed"), (1, "open"), (2, "completed")]
    assert run.work_items() == [(TokenWorkStatus.PENDING_SINK.value, 2, "output", "success")]


# ---------------------------------------------------------------------------
# T2: retries at node N, then the lease is lost at a downstream node M
# ---------------------------------------------------------------------------


def test_a_reclaim_after_retries_upstream_and_a_loss_downstream_collides_at_neither_node(tmp_path: Path) -> None:
    """T2: one claim spans N and M; the re-drive restarts at N above every attempt the token recorded."""
    scenario_reclaim_after_upstream_retries_and_downstream_loss(tmp_path, db_url=None)


def scenario_reclaim_after_upstream_retries_and_downstream_loss(tmp_path: Path, *, db_url: str | None) -> None:
    holder: list[_ScriptedTransform] = []

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        upstream = _ScriptedTransform((_TRANSIENT, _OK), input_connection="inbound", on_success="mid", bust=bust)
        downstream = _ScriptedTransform((_BUST, _OK), input_connection="mid", on_success="output", bust=bust)
        holder.extend([upstream, downstream])
        return [upstream, downstream]

    run = _begin_run(tmp_path, build, db_url=db_url)
    upstream, downstream = holder
    follower, ctx = run.follower()
    assert follower._processor.drain_follower_ready_work(ctx) == []
    assert run.node_states(upstream) == [(0, "failed"), (1, "completed")]
    assert run.node_states(downstream) == [(0, "open")]
    assert run.work_items() == [(TokenWorkStatus.READY.value, 2, None, None)]

    other, other_ctx = run.follower("worker:retry-follower-2")
    results = other._processor.drain_follower_ready_work(other_ctx)

    assert len(results) == 1
    # The base is token-scoped: attempt 2 at BOTH nodes. A node-scoped base
    # taken from M (max 0 -> 1) would re-insert attempt 1 at N.
    assert run.node_states(upstream) == [(0, "failed"), (1, "completed"), (2, "completed")]
    assert run.node_states(downstream) == [(0, "open"), (2, "completed")]
    assert run.work_items() == [(TokenWorkStatus.PENDING_SINK.value, 2, "output", "success")]


# ---------------------------------------------------------------------------
# The claim base itself: token scope, one total base, first claims untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scheduler_attempt", "resume_attempt_offset", "recorded_attempts", "expected_offset"),
    [
        pytest.param(1, 0, (0, 1, 2), 0, id="first_claim_ignores_attempts_at_other_steps"),
        pytest.param(2, 0, (0,), 1, id="plain_rotation_keeps_the_rotation_offset"),
        pytest.param(2, 0, (0, 1), 2, id="rotation_after_an_in_claim_retry_rises_above_the_record"),
        pytest.param(2, 4, (0, 3), 1, id="restored_token_with_nothing_new_is_not_double_counted"),
        pytest.param(2, 4, (0, 3, 4, 5), 2, id="restored_token_whose_lost_claim_retried_rises_above_the_record"),
    ],
)
def test_the_claim_attempt_base_is_derived_from_the_recorded_attempts(
    tmp_path: Path,
    scheduler_attempt: int,
    resume_attempt_offset: int,
    recorded_attempts: tuple[int, ...],
    expected_offset: int,
) -> None:
    """The base is above every recorded attempt of the token, counting the token's own resume base once."""
    run = _begin_run(
        tmp_path,
        lambda bust, _clock: [_ScriptedTransform((_OK,), input_connection="inbound", on_success="output", bust=bust)],
    )
    source_id = build_source_id_map(run.graph)["orders"]
    data = {"src": "orders", "value": 0}
    for attempt in recorded_attempts:
        if attempt == 0:
            continue  # _begin_run recorded the source's attempt 0
        run.factory.execution.record_completed_node_state(
            run.token_id, source_id, 0, data, data, 0.0, coordination_token=run.leader, attempt=attempt
        )
    processor, _ctx = run.leader_processor()
    claimed = run.factory.scheduler.claim_ready(member_token=run.leader.membership, lease_owner=run.leader.worker_id, lease_seconds=300)
    assert claimed is not None and claimed.token_id == run.token_id
    token = TokenInfo(
        row_id=claimed.row_id,
        token_id=claimed.token_id,
        row_data=PipelineRow(data, create_observed_contract(data)),
        resume_attempt_offset=resume_attempt_offset,
        resume_checkpoint_id="checkpoint-under-test" if resume_attempt_offset else None,
    )

    offset = processor._scheduler_drain.claim_attempt_offset(replace(claimed, attempt=scheduler_attempt), token)

    assert offset == expected_offset
    assert resume_attempt_offset + offset > max(recorded_attempts) or scheduler_attempt == 1


# ---------------------------------------------------------------------------
# A4 + T4: a follower retries exactly like the leader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Observed:
    node_states: list[tuple[int, str]]
    transform_errors: list[dict[str, Any]]
    outcomes: list[tuple[str | None, str, bool]]
    work_items: list[tuple[str, int, str | None, str | None]]
    calls: int


def _drain_once(tmp_path: Path, script: tuple[str, ...], mode: ProcessorMode) -> _Observed:
    holder: list[_ScriptedTransform] = []

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        transform = _ScriptedTransform(script, input_connection="inbound", on_success="output", bust=bust)
        holder.append(transform)
        return [transform]

    run_dir = tmp_path / mode.value
    run_dir.mkdir()
    run = _begin_run(run_dir, build)
    (transform,) = holder
    if mode is ProcessorMode.FOLLOWER:
        follower, ctx = run.follower()
        follower._processor.drain_follower_ready_work(ctx)
    else:
        processor, ctx = run.leader_processor()
        processor.drain_scheduled_work(ctx)
    return _Observed(
        node_states=run.node_states(transform),
        transform_errors=run.transform_errors(),
        outcomes=run.outcomes(),
        work_items=[(status, attempt, sink, outcome) for status, attempt, sink, outcome in run.work_items()],
        calls=transform.calls,
    )


def test_a_follower_retries_a_transient_failure_and_the_row_succeeds(tmp_path: Path) -> None:
    """A4: two attempts on the follower, the second succeeds, nothing is routed."""
    observed = _drain_once(tmp_path, (_TRANSIENT, _OK), ProcessorMode.FOLLOWER)

    assert observed.calls == 2
    assert observed.node_states == [(0, "failed"), (1, "completed")]
    assert observed.transform_errors == []
    assert observed.work_items == [(TokenWorkStatus.PENDING_SINK.value, 1, "output", "success")]


def test_a_follower_that_spends_its_retries_routes_retry_exhausted(tmp_path: Path) -> None:
    """A4: every attempt fails; the row reaches on_error as retry_exhausted after max_attempts."""
    observed = _drain_once(tmp_path, (_TRANSIENT,), ProcessorMode.FOLLOWER)

    assert observed.calls == 3
    assert observed.node_states == [(0, "failed"), (1, "failed"), (2, "failed")]
    assert observed.transform_errors == [
        {"reason": "retry_exhausted", "error": "scripted transient failure", "attempts": 3, "destination": "discard"}
    ]
    assert observed.outcomes == [("failure", "quarantined_at_source", True)]
    assert observed.work_items == [(TokenWorkStatus.FAILED.value, 1, None, None)]


@pytest.mark.parametrize(
    "script",
    [(_TRANSIENT, _OK), (_TRANSIENT,)],
    ids=["retried_then_succeeds", "retries_exhausted"],
)
def test_leader_and_follower_record_the_same_retry_audit(tmp_path: Path, script: tuple[str, ...]) -> None:
    """T4: same pipeline, same transient pattern, same durable audit in either mode."""
    on_leader = _drain_once(tmp_path, script, ProcessorMode.LEADER)
    on_follower = _drain_once(tmp_path, script, ProcessorMode.FOLLOWER)

    assert on_follower.calls == on_leader.calls
    assert on_follower.node_states == on_leader.node_states
    assert on_follower.transform_errors == on_leader.transform_errors
    assert on_follower.outcomes == on_leader.outcomes
    assert "transient_error_no_retry" not in json.dumps(on_follower.transform_errors)


# ---------------------------------------------------------------------------
# A FAILED item is a fate only when its token recorded one
# ---------------------------------------------------------------------------


def test_a_run_completes_over_a_routed_failure_whose_token_has_its_outcome(tmp_path: Path) -> None:
    """Negative control: a routed failure leaves a FAILED item AND a completed outcome; COMPLETED is accepted."""
    scenario_run_completes_over_a_routed_failure(tmp_path, db_url=None)


def scenario_run_completes_over_a_routed_failure(tmp_path: Path, *, db_url: str | None) -> None:
    holder: list[_ScriptedTransform] = []

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        transform = _ScriptedTransform((_TRANSIENT,), input_connection="inbound", on_success="output", bust=bust)
        holder.append(transform)
        return [transform]

    run = _begin_run(tmp_path, build, db_url=db_url)
    follower, ctx = run.follower()
    follower._processor.drain_follower_ready_work(ctx)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert run.outcomes() == [("failure", "quarantined_at_source", True)]

    completed = run.factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=run.leader)

    assert completed.status is RunStatus.COMPLETED


def test_a_run_is_not_completed_over_a_claim_that_died_mid_row(tmp_path: Path) -> None:
    """A FAILED item whose token has no outcome refuses every SUCCESS status; FAILED is still recordable."""
    scenario_run_is_not_completed_over_a_claim_that_died_mid_row(tmp_path, db_url=None)


def scenario_run_is_not_completed_over_a_claim_that_died_mid_row(tmp_path: Path, *, db_url: str | None) -> None:

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        return [_ScriptedTransform((_OK,), input_connection="inbound", on_success="output", bust=bust)]

    run = _begin_run(tmp_path, build, db_url=db_url)
    follower, ctx = run.follower()
    (transform,) = run.transforms

    def tier_1_mid_row(row: Any, ctx: Any) -> TransformResult:
        raise FrameworkBugError("follower-side Tier-1 mid-row")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(FrameworkBugError):
        patch.setattr(transform, "process", tier_1_mid_row)
        follower._processor.drain_follower_ready_work(ctx)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert run.outcomes() == []

    for success_status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY):
        with pytest.raises(OrchestrationInvariantError, match="FAILED scheduler work whose token has no terminal outcome") as refused:
            run.factory.run_lifecycle.complete_run(success_status, coordination_token=run.leader)
        assert run.token_id in str(refused.value)

    failed = run.factory.run_lifecycle.complete_run(RunStatus.FAILED, coordination_token=run.leader)
    assert failed.status is RunStatus.FAILED


# ---------------------------------------------------------------------------
# The item lease is refreshed after every attempt, not once per retry sequence
# ---------------------------------------------------------------------------


def test_the_item_lease_is_refreshed_after_each_retry_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W1 (measured): each attempt's ownership check heartbeats the lease once the interval elapsed."""
    heartbeats: list[str] = []
    real_heartbeat = TokenSchedulerRepository.heartbeat_lease

    def counting_heartbeat(self: TokenSchedulerRepository, **kwargs: Any) -> Any:
        heartbeats.append(kwargs["work_item_id"])
        return real_heartbeat(self, **kwargs)

    monkeypatch.setattr(TokenSchedulerRepository, "heartbeat_lease", counting_heartbeat)
    holder: list[_ScriptedTransform] = []
    heartbeat_seconds = 10

    def build(bust: Callable[[str], None], clock: MockClock) -> list[BaseTransform]:
        # Each plugin call outlasts the heartbeat interval on the process clock.
        transform = _ScriptedTransform(
            (_TRANSIENT, _TRANSIENT, _OK),
            input_connection="inbound",
            on_success="output",
            bust=bust,
            on_call=lambda: clock.advance(heartbeat_seconds + 1),
        )
        holder.append(transform)
        return [transform]

    run = _begin_run(tmp_path, build)
    follower, ctx = run.follower(heartbeat_seconds=heartbeat_seconds)
    follower._processor.drain_follower_ready_work(ctx)

    (transform,) = holder
    assert run.node_states(transform) == [(0, "failed"), (1, "failed"), (2, "completed")]
    # One refresh after each of the three attempts, all inside ONE node
    # iteration of ONE claim; the drain's own post-traversal check falls
    # inside the interval and does not write again.
    assert len(heartbeats) == 3
    assert len(set(heartbeats)) == 1


# ---------------------------------------------------------------------------
# T5: a lost render worker on a real follower drain is retried, not routed
# ---------------------------------------------------------------------------


@contextmanager
def _first_render_worker_lost(monkeypatch: pytest.MonkeyPatch, marker: Path) -> Iterator[None]:
    monkeypatch.setenv(_LOSS_MARKER_ENV, str(marker))
    templates._stop_template_workers()
    monkeypatch.setattr(templates, "_template_worker", _worker_lost_on_the_first_request)
    try:
        yield
    finally:
        templates._stop_template_workers()


def test_a_follower_retries_a_row_whose_render_worker_was_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T5: RC-9's retryable disposition holds on `elspeth join` too; the row is never routed."""
    holder: list[_RenderTransform] = []

    def build(_bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        transform = _RenderTransform(input_connection="inbound", on_success="output")
        holder.append(transform)
        return [transform]

    run = _begin_run(tmp_path, build)
    (transform,) = holder
    marker = tmp_path / "worker-lost"
    with _first_render_worker_lost(monkeypatch, marker):
        follower, ctx = run.follower()
        results = follower._processor.drain_follower_ready_work(ctx)
    assert marker.exists(), "the first render worker was never lost; the test proved nothing"

    assert len(results) == 1
    assert transform.calls == 2
    assert run.node_states(transform) == [(0, "failed"), (1, "completed")]
    assert run.node_state_errors(transform) == [{"exception": _LOST_TEXT, "type": "TemplateWorkerLostError"}, None]
    assert run.transform_errors() == []
    assert run.work_items() == [(TokenWorkStatus.PENDING_SINK.value, 1, "output", "success")]


# ---------------------------------------------------------------------------
# A2: one retry authority, enforced both ways
# ---------------------------------------------------------------------------


def test_a_leader_processor_refuses_a_follower_retry_config(tmp_path: Path) -> None:
    run = _begin_run(
        tmp_path,
        lambda bust, _clock: [_ScriptedTransform((_OK,), input_connection="inbound", on_success="output", bust=bust)],
    )
    with pytest.raises(OrchestrationInvariantError, match="follower_retry_config was passed to a 'leader'-mode row processor"):
        build_row_processor(
            graph=run.graph,
            config=run.config,
            settings=_run_settings(),
            factory=run.factory,
            run_id=run.run_id,
            source_id=build_source_id_map(run.graph)["orders"],
            edge_map=load_edge_map(run.factory.data_flow, run.run_id),
            route_resolution_map=run.graph.get_route_resolution_map(),
            config_gate_id_map=run.graph.get_config_gate_id_map(),
            coalesce_id_map=run.graph.get_coalesce_id_map(),
            payload_store=run.payload_store,
            span_factory=SpanFactory(),
            clock=run.clock,
            max_workers=None,
            telemetry=None,
            mode=ProcessorMode.LEADER,
            coordination_token=run.leader,
            follower_retry_config=RuntimeRetryConfig.from_settings(_run_settings().retry),
        )
