# tests/unit/engine/test_collector_executor.py
"""CollectorExecutor arrival/loss/roster/flush semantics (WS4, spec §5).

Fixtures build a REAL landscape DB (`make_recorder_with_run`) and seed
group rosters through the production `expand_token` writer, mirroring the
Task 2/3 precedent — `BarrierRestoreReadModel.get_group_record` and its
siblings are real SQL reads with no production writer that accepts a
caller-chosen ``group_id``, so tests capture the REAL minted id from the
seeding call rather than hardcoding a literal (a mechanical adaptation of
the plan's illustrative "g-1" literals, not a behavioural change).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, PluginContractViolation
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape.schema import group_records_table, node_states_table
from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_field
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_observed_contract(*field_names: str) -> SchemaContract:
    fields = tuple(
        make_field(name=name, original_name=f"'{name}'", python_type=object, required=False, source="inferred") for name in field_names
    )
    return SchemaContract(mode="OBSERVED", fields=fields, locked=True)


class _SpanFactorySentinel:
    """Unused by CollectorExecutor's shown behaviour — stored, never invoked."""


class _FakeCollectorTransform:
    """A duck-typed BatchTransformProtocol stand-in with test-controllable behaviour."""

    def __init__(self, name: str = "recording_stitch") -> None:
        self.name = name
        self.quarantine_indices: list[int] = []
        self.return_empty_success = False
        self.return_zero_rows = False
        self.echo_rows = False
        self.raise_on_process: BaseException | None = None
        self.seen_rows: list[dict[str, Any]] = []
        self.call_count = 0

    def process(self, rows: list[PipelineRow], ctx: PluginContext) -> Any:
        self.call_count += 1
        if self.raise_on_process is not None:
            raise self.raise_on_process
        self.seen_rows = [row.to_dict() for row in rows]
        if self.return_empty_success:
            # Deliberately bypasses TransformResult.__post_init__'s own
            # "row or rows required" guard (which would raise ValueError
            # before this ever reaches the executor) — a duck-typed stand-in
            # is the only way to exercise _execute_flush's OWN equivalent
            # check (aggregation.py:527-532 precedent).
            return SimpleNamespace(status="success", row=None, rows=None, reason=None, success_reason={"action": "noop"})
        from elspeth.contracts import TransformResult

        success_reason: dict[str, Any] = {"action": "collected"}
        if self.return_zero_rows:
            # B-4: the auditable "intentionally emitted nothing" shape
            # (results.py:438-457) — NOT success_multi([]), which raises
            # ValueError (rows must not be empty). rows=() here, distinct
            # from return_empty_success's row=None/rows=None duck-type above.
            return TransformResult.success_empty(success_reason={"action": "collected_empty"})
        if self.quarantine_indices:
            success_reason["metadata"] = {"quarantined_indices": list(self.quarantine_indices)}
        if self.echo_rows:
            out_rows = tuple(rows)
        else:
            contract = rows[0].contract
            out_rows = (PipelineRow({"assembled": True, "count": len(rows)}, contract),)
        return TransformResult.success_multi(out_rows, success_reason=success_reason)


class _EnvExecutor:
    """Thin adapter injecting env.ctx into accept/notify_member_lost unless overridden."""

    def __init__(self, executor: CollectorExecutor, default_ctx: PluginContext) -> None:
        self._executor = executor
        self._default_ctx = default_ctx

    def accept(
        self, token: TokenInfo, collector_name: str, *, ctx: PluginContext | None = None, arrival_time: float | None = None
    ) -> CollectorOutcome:
        return self._executor.accept(token, collector_name, ctx if ctx is not None else self._default_ctx, arrival_time=arrival_time)

    def notify_member_lost(
        self, collector_name: str, group_id: str, member_key: str, reason: str, *, ctx: PluginContext | None = None
    ) -> CollectorOutcome | None:
        return self._executor.notify_member_lost(
            collector_name, group_id, member_key, reason, ctx if ctx is not None else self._default_ctx
        )

    def notify_empty_group(self, collector_name: str, group_id: str) -> CollectorOutcome:
        return self._executor.notify_empty_group(collector_name, group_id)

    def has_recorded_member_loss(self, collector_name: str, group_id: str, member_key: str) -> bool:
        return self._executor.has_recorded_member_loss(collector_name, group_id, member_key)

    def buffered_member_count(self) -> int:
        return self._executor.buffered_member_count()

    def get_registered_names(self) -> list[str]:
        return self._executor.get_registered_names()


class _CollectorEnv:
    """Real-DB-backed test environment for one registered collector 'stitch'."""

    def __init__(self, *, policy: str) -> None:
        self.setup = make_recorder_with_run()
        self.db = self.setup.db
        self.factory = self.setup.factory
        self.run_id = self.setup.run_id
        self.node_id = NodeID(register_test_node(self.factory.data_flow, self.run_id, "stitch", plugin_name="recording_stitch"))
        self.contract = _make_observed_contract("item")
        self.transform = _FakeCollectorTransform()
        self.ctx = PluginContext(run_id=self.run_id, config={})
        self.token_manager = TokenManager(self.factory.data_flow, step_resolver=lambda _node_id: 1)
        self._raw_executor = CollectorExecutor(
            self.factory.execution,
            _SpanFactorySentinel(),
            self.token_manager,
            self.run_id,
            step_resolver=lambda _node_id: 1,
            data_flow=self.factory.data_flow,
            barrier_restore_reads=self.factory.barrier_restore,
        )
        settings = CollectorSettings(name="stitch", plugin="recording_stitch", input="pages_in", on_success="assembled_out")
        scope = ScopeSettings(name="scope1", opener="expand_node", closer="stitch", policy=policy)
        self._raw_executor.register_collector(settings, scope, self.node_id, self.transform)
        self.executor = _EnvExecutor(self._raw_executor, self.ctx)
        self._all_members: list[TokenInfo] = []
        self._row_counter = 0

    def _seed_opener(self) -> Any:
        self._row_counter += 1
        row = self.factory.data_flow.create_row(
            self.run_id,
            self.setup.source_node_id,
            self._row_counter,
            {"seed": self._row_counter},
            source_row_index=self._row_counter,
            ingest_sequence=self._row_counter,
        )
        return self.factory.data_flow.create_token(row_id=row.row_id)

    def seed_group(self, *, count: int) -> tuple[list[TokenInfo], str]:
        """Seed a real EXPAND group via the production writer. Returns (members, group_id)."""
        opener = self._seed_opener()
        payloads = [{"item": i} for i in range(count)]
        children, group_id = self.factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=opener.token_id, run_id=self.run_id),
            row_id=opener.row_id,
            child_payloads=payloads,
            output_contract=self.contract,
        )
        members = [
            TokenInfo(
                row_id=child.row_id, token_id=child.token_id, row_data=PipelineRow(payload, self.contract), lineage_path=child.lineage_path
            )
            for child, payload in zip(children, payloads, strict=True)
        ]
        self._all_members.extend(members)
        return members, group_id

    def seed_empty_group(self) -> str:
        opener = self._seed_opener()
        return self.factory.data_flow.record_empty_expansion(TokenRef(token_id=opener.token_id, run_id=self.run_id))

    def reissue_with_new_token_id(self, original: TokenInfo) -> TokenInfo:
        """A durably-minted 'merged' token: fresh token_id, SAME frame (spec §5, arch minor 3)."""
        fresh_id = f"{original.token_id}-merged"
        self.factory.data_flow.create_token(row_id=original.row_id, token_id=fresh_id, lineage_path=original.lineage_path)
        reissued = TokenInfo(row_id=original.row_id, token_id=fresh_id, row_data=original.row_data, lineage_path=original.lineage_path)
        self._all_members.append(reissued)
        return reissued

    def plain_token(self) -> TokenInfo:
        return TokenInfo(row_id="stray-row", token_id="stray-token", row_data=PipelineRow({"x": 1}, self.contract), lineage_path=())

    def corrupt_group_record_member_count(self, *, group_id: str, member_count: int) -> None:
        from sqlalchemy import update

        with self.db.write_connection() as conn:
            conn.execute(
                update(group_records_table)
                .where(group_records_table.c.run_id == self.run_id)
                .where(group_records_table.c.group_id == group_id)
                .values(member_count=member_count)
            )

    def open_hold_token_ids(self, *, node: str) -> list[str]:
        assert node == "stitch"
        result = self.factory.barrier_restore.get_open_node_state_ids(
            self.run_id,
            node_ids=[str(self.node_id)],
            token_ids=[m.token_id for m in self._all_members],
        )
        return sorted(result.keys())

    def quarantined_token_ids(self) -> list[str]:
        ids = []
        for member in self._all_members:
            outcome = self.factory.data_flow.get_token_outcome(member.token_id)
            if outcome is not None and outcome.path is TerminalPath.QUARANTINED_AT_SOURCE:
                ids.append(member.token_id)
        return ids

    def latest_node_state_error_phase(self, *, node: str) -> str:
        import json

        from sqlalchemy import select

        assert node == "stitch"
        with self.db.connection() as conn:
            row = (
                conn.execute(
                    select(node_states_table.c.error_json)
                    .where(node_states_table.c.run_id == self.run_id)
                    .where(node_states_table.c.node_id == str(self.node_id))
                    .where(node_states_table.c.error_json.is_not(None))
                    .order_by(node_states_table.c.completed_at.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
        return str(json.loads(row["error_json"])["phase"])


@pytest.fixture
def collector_env() -> _CollectorEnv:
    return _CollectorEnv(policy="require_all")


@pytest.fixture
def best_effort_env() -> _CollectorEnv:
    return _CollectorEnv(policy="best_effort")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArrivals:
    def test_members_are_held_until_roster_closes(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=3)
        assert env.executor.accept(members[0], "stitch").held is True
        assert env.executor.accept(members[1], "stitch").held is True
        outcome = env.executor.accept(members[2], "stitch")  # roster closes
        assert outcome.held is False
        assert outcome.group_id == members[0].lineage_path[-1].group_id

    def test_arrival_resolves_member_key_from_the_innermost_expand_frame(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=1)
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.held is False  # count=1 roster closes on the single arrival

    def test_duplicate_same_token_arrival_is_an_idempotent_skip(self, collector_env: _CollectorEnv) -> None:
        # spec §5: lease-expiry redelivery is by design — CAS-fenced skip, not a crash.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        again = env.executor.accept(members[0], "stitch")
        assert again.held is True
        assert env.executor.buffered_member_count() == 1

    def test_distinct_token_for_a_settled_member_is_an_integrity_error(self, collector_env: _CollectorEnv) -> None:
        # Build-time impossible everywhere (§7 rule 5) — runtime occurrence is a bug.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        impostor = env.reissue_with_new_token_id(members[0])
        with pytest.raises(AuditIntegrityError):
            env.executor.accept(impostor, "stitch")

    def test_arriving_token_whose_innermost_frame_is_not_expand_crashes(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        stray = env.plain_token()  # lineage_path == ()
        with pytest.raises(OrchestrationInvariantError):
            env.executor.accept(stray, "stitch")

    def test_roster_cross_check_member_count_vs_frames(self, collector_env: _CollectorEnv) -> None:
        # spec §5 'minted': group_records.member_count cross-checked against
        # DISTINCT member_key in token_lineage_frames — mismatch = integrity error.
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.corrupt_group_record_member_count(group_id=group_id, member_count=3)
        with pytest.raises(AuditIntegrityError):
            env.executor.accept(members[0], "stitch")

    def test_every_arrival_journals_a_durable_hold(self, collector_env: _CollectorEnv) -> None:
        # RATIFIED pin (2026-08-22 synthesis, canon item 11): collector arrivals
        # ALWAYS journal — accept() calls begin_node_state BEFORE the buffer
        # insert, so every held member has an open node_state at the collector.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.held is True
        assert env.open_hold_token_ids(node="stitch") == [members[0].token_id]
        # The idempotent redelivery skip does NOT journal a second hold:
        env.executor.accept(members[0], "stitch")
        assert env.open_hold_token_ids(node="stitch") == [members[0].token_id]


class TestLosses:
    def test_loss_settles_a_member_and_can_close_the_roster_best_effort(self, best_effort_env: _CollectorEnv) -> None:
        env = best_effort_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.notify_member_lost("stitch", group_id, members[1].token_id, "quarantined")
        assert outcome is not None and outcome.held is False  # arrived member flushes

    def test_require_all_group_with_a_loss_fails_only_at_closure(self, collector_env: _CollectorEnv) -> None:
        # Verdicts wait for settlement (spec §6.3 item 3): a loss alone does
        # not fail a 3-member group while a member is still unsettled.
        env = collector_env
        members, group_id = env.seed_group(count=3)
        env.executor.accept(members[0], "stitch")
        mid = env.executor.notify_member_lost("stitch", group_id, members[1].token_id, "quarantined")
        assert mid is None  # roster not closed yet
        final = env.executor.accept(members[2], "stitch")  # closure
        assert final.failure_reason == "collector_missing_members"
        assert final.outcomes_recorded is True

    def test_duplicate_loss_dedup(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")
        assert env.executor.has_recorded_member_loss("stitch", group_id, members[0].token_id) is True
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")

    def test_loss_for_a_member_outside_the_roster_crashes(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        _members, group_id = env.seed_group(count=2)
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", group_id, "tok-not-a-member", "quarantined")


class TestFlush:
    def test_flush_orders_members_by_opener_ordinal_not_arrival_order(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=3)  # ordinals 0,1,2
        env.executor.accept(members[2], "stitch")  # arrive out of order
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")
        assert outcome.held is False
        # The recording plugin observed rows in ordinal order 0,1,2:
        assert env.transform.seen_rows == [m.row_data.to_dict() for m in members]
        assert outcome.consumed_tokens == tuple(members)

    def test_flush_order_resolves_a_merged_member_through_the_opener_ordinals(self, collector_env: _CollectorEnv) -> None:
        # A member whose subtree forked-and-coalesced arrives as a merged token
        # with a FRESH token_id; its member_key (the opener child's token_id)
        # still resolves the ordinal via token_parents (spec §5, arch minor 3).
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        merged = env.reissue_with_new_token_id(members[0])  # fresh token_id, same frame
        env.executor.accept(members[1], "stitch")
        outcome = env.executor.accept(merged, "stitch")
        assert outcome.held is False
        assert env.transform.seen_rows[0] == merged.row_data.to_dict()  # ordinal 0 first

    def test_group_relative_quarantine_indices(self, collector_env: _CollectorEnv) -> None:
        # Indices are relative to the FLUSHED GROUP in ordinal order —
        # validated_quarantined_indices reused with buffered_token_count=len(group).
        env = collector_env
        env.transform.quarantine_indices = [1]
        members, _group_id = env.seed_group(count=3)
        outcome = None
        for m in members:
            outcome = env.executor.accept(m, "stitch")
        assert outcome is not None and outcome.held is False
        assert env.quarantined_token_ids() == [members[1].token_id]

    def test_all_quarantined_with_output_is_an_invariant_violation(self, collector_env: _CollectorEnv) -> None:
        # aggregation.py:546-550 guard, replicated with the same semantics.
        env = collector_env
        env.transform.quarantine_indices = [0, 1]
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        with pytest.raises(OrchestrationInvariantError, match="all group members were quarantined"):
            env.executor.accept(members[1], "stitch")

    def test_success_with_neither_row_nor_rows_is_a_contract_violation(self, collector_env: _CollectorEnv) -> None:
        # aggregation.py:527-532 guard, replicated with the same semantics.
        env = collector_env
        env.transform.return_empty_success = True
        members, _group_id = env.seed_group(count=1)
        with pytest.raises(PluginContractViolation, match="neither row nor rows"):
            env.executor.accept(members[0], "stitch")

    def test_passthrough_quarantine_is_prohibited(self, collector_env: _CollectorEnv) -> None:
        # Collectors are transform-only; a plugin returning per-row passthrough
        # shape (rows out == rows in) still goes through ordinary transform-mode
        # quarantine handling — pinned via a plugin with len(rows)==len(members).
        env = collector_env
        env.transform.quarantine_indices = [0]
        env.transform.echo_rows = True  # rows out == rows in
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")
        assert outcome.held is False
        assert env.quarantined_token_ids() == [members[0].token_id]

    def test_empty_group_close_never_invokes_plugin(self, collector_env: _CollectorEnv, best_effort_env: _CollectorEnv) -> None:
        for env, expect_failure in ((collector_env, True), (best_effort_env, False)):
            group_id = env.seed_empty_group()
            outcome = env.executor.notify_empty_group("stitch", group_id)
            assert env.transform.call_count == 0
            assert outcome.closed_without_plugin == "empty_expansion"
            assert (outcome.failure_reason == "empty_expansion") is expect_failure

    def test_all_members_lost_best_effort_closes_without_plugin(self, best_effort_env: _CollectorEnv) -> None:
        env = best_effort_env
        members, group_id = env.seed_group(count=2)
        env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")
        outcome = env.executor.notify_member_lost("stitch", group_id, members[1].token_id, "quarantined")
        assert outcome is not None
        assert outcome.closed_without_plugin == "all_members_lost"
        assert env.transform.call_count == 0

    def test_plugin_emitting_zero_rows_flushes_the_contract_guard_and_mints_an_empty_release_durably(
        self, collector_env: _CollectorEnv
    ) -> None:
        """B-4: a plugin that intentionally emits nothing (TransformResult.success_empty(),
        rows=()) still DOES invoke the plugin (unlike notify_empty_group/all_members_lost,
        which close without ever calling it) — it must pass the "neither row nor rows"
        contract guard (rows=() is not None, so this is not the return_empty_success
        duck-typed shape), skip the all-quarantined guard entirely (output_rows is empty,
        so there's nothing to check members against), and mint the M=0 durable release
        group via collect_tokens (T3 fix-round ruling 1) rather than silently no-op'ing.

        (The addendum's literal "success_multi([])" is invalid — success_multi requires
        at least one row (results.py:422-424); success_empty() is the actual auditable
        zero-emission shape and is what a real plugin would return here.)
        """
        env = collector_env
        env.transform.return_zero_rows = True
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")

        assert outcome.held is False
        assert outcome.released_tokens == ()
        assert outcome.consumed_tokens == tuple(members)
        assert env.transform.call_count == 1  # the plugin WAS invoked, unlike an engine-closed empty/all-lost group

        records = env.factory.data_flow.get_group_records_for_run(env.run_id)
        release_records = [r for r in records if r["group_id"] != group_id and r["kind"] == "expand"]
        assert len(release_records) == 1
        assert release_records[0]["member_count"] == 0


def test_collector_flush_plugin_exception_autofails_with_phase(collector_env: _CollectorEnv) -> None:
    env = collector_env
    env.transform.raise_on_process = RuntimeError("boom")
    members, _group_id = env.seed_group(count=1)
    with pytest.raises(RuntimeError):
        env.executor.accept(members[0], "stitch", ctx=env.ctx)
    # NodeStateGuard auto-failed the flush state with the new phase:
    assert env.latest_node_state_error_phase(node="stitch") == "collector_flush"
