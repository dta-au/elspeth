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

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, PluginContractViolation
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape.scheduler.group_losses import record_group_loss
from elspeth.core.landscape.schema import group_records_table, node_states_table, token_parents_table
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
        self.return_error = False
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

        if self.return_error:
            # I-2 (fix round 2): a real "transform returned status=error"
            # failure — distinct from raise_on_process, which propagates
            # past NodeStateGuard entirely and never reaches _fail_group.
            return TransformResult.error(reason={"reason": "collector_plugin_declared_failure"})

        success_reason: dict[str, Any] = {"action": "collected"}
        if self.quarantine_indices:
            success_reason["metadata"] = {"quarantined_indices": list(self.quarantine_indices)}
        if self.return_zero_rows:
            # B-4: the auditable "intentionally emitted nothing" shape
            # (results.py:438-457) — NOT success_multi([]), which raises
            # ValueError (rows must not be empty). rows=() here, distinct
            # from return_empty_success's row=None/rows=None duck-type above.
            # Composes with quarantine_indices (I-1, fix round 2): a plugin
            # that quarantines every member AND legitimately emits nothing —
            # the metadata must still carry the indices even though rows=().
            return TransformResult.success_empty(success_reason=dict(success_reason, action="collected_empty"))
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

    def delete_token_parents_row(self, *, token_id: str) -> None:
        """Corrupt the OTHER durable authority: leaves token_id's roster
        membership (token_lineage_frames) intact but removes its
        token_parents row, so get_group_member_ordinals can no longer
        resolve its flush ordinal — the genuinely-fallible cross-table
        inconsistency FIX 1 (I-5 residual, fix round 3) targets."""
        from sqlalchemy import delete

        with self.db.write_connection() as conn:
            conn.execute(
                delete(token_parents_table)
                .where(token_parents_table.c.run_id == self.run_id)
                .where(token_parents_table.c.token_id == token_id)
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

    def node_state_error_exception_for_token(self, *, node: str, token_id: str) -> str:
        """The error_json['exception'] text for THIS token's own hold state —
        disambiguates from the flush guard's separate opener-anchored state,
        which shares the same node_id but a different token_id."""
        import json

        from sqlalchemy import select

        assert node == "stitch"
        with self.db.connection() as conn:
            row = (
                conn.execute(
                    select(node_states_table.c.error_json)
                    .where(node_states_table.c.run_id == self.run_id)
                    .where(node_states_table.c.node_id == str(self.node_id))
                    .where(node_states_table.c.token_id == token_id)
                    .where(node_states_table.c.error_json.is_not(None))
                    .order_by(node_states_table.c.completed_at.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
        return str(json.loads(row["error_json"])["exception"])


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

    def test_post_closure_same_token_redelivery_is_a_cas_fenced_idempotent_skip(self, collector_env: _CollectorEnv) -> None:
        # I-3 (fix round 2): spec §5's "duplicate arrival of the SAME token
        # for a settled member is a CAS-fenced idempotent skip" — closure
        # deletes the pending entry, so the OLD code's claimed "pending-entry
        # check below" was unreachable and every post-closure arrival raised.
        # settled_token_ids (recorded at every in-process close) makes the
        # same-token-vs-distinct-token distinction directly instead.
        env = collector_env
        members, _group_id = env.seed_group(count=1)
        first = env.executor.accept(members[0], "stitch")
        assert first.held is False  # closes on the single arrival
        redelivered = env.executor.accept(members[0], "stitch")
        assert redelivered.held is True

    def test_post_closure_distinct_token_arrival_is_still_an_integrity_error(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=1)
        env.executor.accept(members[0], "stitch")  # closes
        impostor = env.reissue_with_new_token_id(members[0])
        with pytest.raises(AuditIntegrityError, match="not among the members"):
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

    def test_ordinal_mismatch_leaves_no_phantom_entry_and_no_orphaned_node_state(self, collector_env: _CollectorEnv) -> None:
        # FIX 1 / I-5 residual (fix round 3, META-20a). pending.roster comes
        # from get_group_member_keys (token_lineage_frames); pending.ordinals
        # comes from get_group_member_ordinals (token_parents) — two
        # different tables, so a member can hold a valid roster frame with
        # no token_parents row under the opener. That is a genuine audit
        # inconsistency (the raise is honest and must stay), but the OLD
        # ordering opened a durable node_state (begin_node_state) BEFORE
        # resolving the ordinal that then aborted the arrival — the
        # node_state's state_id lived only in a local variable and was
        # never stored into a _MemberEntry, so it became permanently
        # orphaned: _fail_group/_execute_flush only ever complete holds
        # reachable through pending.arrived, and _roster_settled can never
        # be satisfied for a group missing this member from both arrived
        # and lost, so neither close path is ever reached for this group.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.delete_token_parents_row(token_id=members[0].token_id)

        with pytest.raises(AuditIntegrityError, match="has no token_parents ordinal"):
            env.executor.accept(members[0], "stitch")

        # Mechanism, not symptom: no phantom _pending entry (assertion 1)...
        assert env.executor._executor._pending == {}
        # ...AND zero open node_states at the collector node (assertion 2)
        # — the assertion that distinguishes a real fix from a reorder that
        # only moved the install and left begin_node_state before the check.
        assert env.open_hold_token_ids(node="stitch") == []

        # A follow-up arrival for a DIFFERENT, valid member of the same
        # group still opens the group and accepts cleanly (assertion 3) —
        # the aborted attempt left no poisoned state behind. (The group
        # itself can never fully settle — member[0]'s token_parents row is
        # durably gone — but that is the underlying audit inconsistency's
        # own consequence, not a defect this fix is responsible for.)
        outcome = env.executor.accept(members[1], "stitch")
        assert outcome.held is True
        assert env.open_hold_token_ids(node="stitch") == [members[1].token_id]

    def test_collector_bound_to_a_non_expand_group_crashes_at_open(self, collector_env: _CollectorEnv) -> None:
        # I-6 (fix round 2): Task 2's group_records reads are deliberately
        # kind-agnostic (a group_id is globally unique per run regardless of
        # kind), so without this assertion a collector mis-bound to a FORK
        # group (a scope/binding-registry defect, not something a real token
        # would ever legitimately carry) would proceed with a roster of
        # branch names and ordinals keyed by token_ids, surfacing later as a
        # confusing "expansion audit inconsistency" instead of the real
        # binding error at its source. Craft a token whose OWN frame claims
        # an EXPAND arrival at a group_id that was actually durably opened
        # via fork_token — accept()'s own frame-kind check only validates
        # the token's claim, not the durable record it points at.
        env = collector_env
        opener = env._seed_opener()
        (branch,), fork_group_id = env.factory.data_flow.fork_token(
            parent_ref=TokenRef(token_id=opener.token_id, run_id=env.run_id),
            row_id=opener.row_id,
            branches=["path-a"],
        )
        mismatched = TokenInfo(
            row_id=opener.row_id,
            token_id=branch.token_id,
            row_data=PipelineRow({"item": 0}, env.contract),
            lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id=fork_group_id, member_key=branch.token_id),),
        )
        with pytest.raises(AuditIntegrityError, match="not 'expand'"):
            env.executor.accept(mismatched, "stitch")

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
        # META-11.2 fix-round: the executor no longer writes ARRIVED members'
        # terminal disposition itself (matches CoalesceExecutor's Task
        # 6/Ruling 37 removal of the same class of write) — the WS3
        # settle-member seam must record consumed_tokens' terminal outcomes.
        # META-17 (fix round 2, I-7): CollectorOutcome.outcomes_recorded was
        # deleted entirely rather than carried as a boolean the seam would
        # need to consult; pin the mechanism directly instead — no durable
        # terminal write exists for either arrived member.
        assert env.factory.data_flow.get_token_outcome(members[0].token_id) is None
        assert env.factory.data_flow.get_token_outcome(members[2].token_id) is None

    def test_duplicate_loss_dedup(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")
        assert env.executor.has_recorded_member_loss("stitch", group_id, members[0].token_id) is True
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")

    def test_has_recorded_member_loss_consults_the_durable_ledger_with_no_in_memory_state(self, collector_env: _CollectorEnv) -> None:
        # I-4 (fix round 2): a resumed worker's in-memory _pending carries no
        # history before a group opens, after it closes, or across a
        # takeover — WS3's durable group_losses ledger must be consulted
        # too, not just self._pending. No accept()/notify_member_lost() call
        # precedes this: self._pending has zero entries for this group.
        env = collector_env
        members, group_id = env.seed_group(count=2)
        with env.db.write_connection() as conn:
            record_group_loss(
                conn,
                run_id=env.run_id,
                spec=GroupLossSpec(
                    closer_name="stitch",
                    group_id=group_id,
                    member_key=members[0].token_id,
                    token_id=members[0].token_id,
                    reason="quarantined",
                ),
                recorded_by="test-worker",
                now=datetime.now(UTC),
            )
        assert env.executor._executor._pending == {}
        assert env.executor.has_recorded_member_loss("stitch", group_id, members[0].token_id) is True
        assert env.executor.has_recorded_member_loss("stitch", group_id, members[1].token_id) is False

    def test_loss_for_a_member_outside_the_roster_crashes(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        _members, group_id = env.seed_group(count=2)
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", group_id, "tok-not-a-member", "quarantined")
        # I-5 (fix round 2): the rejected loss notification must not leave a
        # phantom _pending entry behind — self._pending was installed
        # BEFORE the roster-membership check in the pre-fix code.
        assert env.executor._executor._pending == {}

    def test_invalid_member_key_arrival_leaves_no_phantom_pending_entry(self, collector_env: _CollectorEnv) -> None:
        # I-5 (fix round 2): same install-after-validate fix on the accept()
        # side. A rejected arrival for a bogus member_key must not leave a
        # ghost _PendingGroup that WS5's satisfiability gate would count.
        env = collector_env
        members, group_id = env.seed_group(count=1)
        bogus = TokenInfo(
            row_id=members[0].row_id,
            token_id="bogus-token",
            row_data=members[0].row_data,
            lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id=group_id, member_key="not-a-real-member"),),
        )
        with pytest.raises(AuditIntegrityError):
            env.executor.accept(bogus, "stitch")
        assert env.executor._executor._pending == {}
        # The real member still settles normally afterward — no ghost blocked it.
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.held is False


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

    def test_all_quarantined_without_output_is_an_invariant_violation(self, collector_env: _CollectorEnv) -> None:
        # I-1 (fix round 2): the pre-fix guard only checked `output_rows and
        # not surviving`, so an all-quarantined flush that ALSO emits zero
        # rows (success_empty() + quarantine metadata covering everyone)
        # fell through to collect_tokens(members=(), …) and died two layers
        # down on its own generic "requires at least one member token"
        # guard — a confusing crash site for what is the SAME collector-level
        # invariant violation as the "with output" case above.
        env = collector_env
        env.transform.quarantine_indices = [0, 1]
        env.transform.return_zero_rows = True
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

    def test_passthrough_quarantine_goes_through_ordinary_transform_mode_handling(self, collector_env: _CollectorEnv) -> None:
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

    def test_transform_error_message_does_not_misattribute_lost_members_under_require_all(self, best_effort_env: _CollectorEnv) -> None:
        # I-2 (fix round 2): _fail_group is shared by the require_all-loss
        # arm AND the collector_transform_error arm; the OLD message
        # hardcoded "lost members ... under require_all" for both, even
        # though a transform-error failure under best_effort with no losses
        # has neither a require_all policy nor any lost members. The durable
        # audit record must not claim a cause it didn't have.
        env = best_effort_env
        env.transform.return_error = True
        members, _group_id = env.seed_group(count=1)
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.failure_reason == "collector_transform_error"
        exception_text = env.node_state_error_exception_for_token(node="stitch", token_id=members[0].token_id)
        assert "under require_all" not in exception_text
        assert "collector_transform_error" in exception_text

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
