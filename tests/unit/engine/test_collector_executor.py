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
from elspeth.contracts.enums import FrameKind, NodeStateStatus, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, PluginContractViolation
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import GroupLossSpec, TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape.scheduler.group_losses import record_group_loss
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import group_records_table, node_states_table, token_parents_table
from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_field
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

_JOURNAL_T0 = datetime(2026, 1, 1, tzinfo=UTC)

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

    def has_replayed_member_loss(self, collector_name: str, group_id: str, member_key: str) -> bool:
        return self._executor.has_replayed_member_loss(collector_name, group_id, member_key)

    def flush_restored_complete_groups(self, *, ctx: PluginContext | None = None) -> tuple[CollectorOutcome, ...]:
        return self._executor.flush_restored_complete_groups(ctx if ctx is not None else self._default_ctx)

    def buffered_member_count(self) -> int:
        return self._executor.buffered_member_count()

    def get_registered_names(self) -> list[str]:
        return self._executor.get_registered_names()

    def restore_from_journal(
        self,
        *,
        items: list[TokenWorkItem],
        state_ids: dict[str, str],
        attempt_offsets: dict[str, int],
        resume_checkpoint_id: str,
    ) -> None:
        self._executor.restore_from_journal(
            items=items, state_ids=state_ids, attempt_offsets=attempt_offsets, resume_checkpoint_id=resume_checkpoint_id
        )


class _CollectorEnv:
    """Real-DB-backed test environment for one registered collector 'stitch'."""

    def __init__(self, *, policy: str) -> None:
        self.policy = policy
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

    def fresh_executor(self) -> _EnvExecutor:
        """A SECOND CollectorExecutor against the SAME Landscape (Task 7).

        Distinct in-memory state (own _pending/_completed_keys, own
        transform instance) sharing the same DB/run_id/node_id/registration
        as env.executor — the "takeover" shape: one process settles a
        group, a DIFFERENT process instance restores/redelivers against it,
        proving reconstruction reads durable state rather than happening to
        still hold in-memory history (task-6-7-review-prep.md AMENDMENT
        (b), takeover flavor).
        """
        transform = _FakeCollectorTransform()
        raw = CollectorExecutor(
            self.factory.execution,
            _SpanFactorySentinel(),
            self.token_manager,
            self.run_id,
            step_resolver=lambda _node_id: 1,
            data_flow=self.factory.data_flow,
            barrier_restore_reads=self.factory.barrier_restore,
        )
        settings = CollectorSettings(name="stitch", plugin="recording_stitch", input="pages_in", on_success="assembled_out")
        scope = ScopeSettings(name="scope1", opener="expand_node", closer="stitch", policy=self.policy)
        raw.register_collector(settings, scope, self.node_id, transform)
        wrapped = _EnvExecutor(raw, self.ctx)
        wrapped.transform = transform  # type: ignore[attr-defined]  # test-only convenience handle
        return wrapped

    def blocked_item_for(self, member: TokenInfo, *, collector_name: str) -> TokenWorkItem:
        """Build a BLOCKED journal row for `member` as list_blocked_barrier_items
        would return it (Task 7) — mirrors test_coalesce_executor.py's
        `_blocked_item` precedent, collector-shaped: barrier_key is the
        COMPOUND collector:<name>:<group_id> form (spec §4.3), not a bare
        name, and lineage_path carries member's real innermost EXPAND frame
        (never synthesised) since restore's I-6 cross-check depends on it.
        """
        frame = member.lineage_path[-1]
        return TokenWorkItem(
            work_item_id=f"wi-{member.token_id}",
            run_id=self.run_id,
            token_id=member.token_id,
            row_id=member.row_id,
            node_id=str(self.node_id),
            step_index=0,
            ingest_sequence=0,
            row_payload_json=TokenSchedulerRepository.serialize_row_payload(member.row_data),
            status=TokenWorkStatus.BLOCKED,
            attempt=0,
            available_at=_JOURNAL_T0,
            created_at=_JOURNAL_T0,
            updated_at=_JOURNAL_T0,
            barrier_key=collector_barrier_key(collector_name, frame.group_id),
            lineage_path=member.lineage_path,
            collector_name=collector_name,
            barrier_blocked_at=_JOURNAL_T0,
        )

    def state_ids_for(self, items: list[TokenWorkItem]) -> dict[str, str]:
        """Read back the real OPEN node_state ids for these journal items'
        tokens (Task 7) — the audit-derived state_ids restore_from_journal
        requires. The members must already have gone through a LIVE accept()
        (on env.executor, before the simulated crash) so a real PENDING hold
        exists to read back; a synthesised state_id would defeat M-4's
        open-hold cross-check by construction."""
        return self.factory.barrier_restore.get_open_node_state_ids(
            self.run_id,
            node_ids=[str(self.node_id)],
            token_ids=[item.token_id for item in items],
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

    def test_takeover_loss_replay_restages_once_then_dedups_in_memory(self, collector_env: _CollectorEnv) -> None:
        # WS4 fix round 2 (b): the journal-first loss replay reads the durable
        # ledger, so every loss it replays is in that ledger BY CONSTRUCTION.
        # A replay that dedups through has_recorded_member_loss (whose I-4
        # fallback consults that same ledger) therefore skips every takeover
        # loss forever: the rebuilt executor never learns the member is lost
        # and the group never settles. The replay predicate is the in-memory
        # has_replayed_member_loss — mirroring CoalesceExecutor /
        # RowUnionExecutor.has_recorded_branch_loss — so a takeover re-stages
        # each loss exactly once and the second replay in the same process is
        # the no-op.
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
                recorded_by="crashed-worker",
                now=datetime.now(UTC),
            )
        fresh = env.fresh_executor()
        # The durable guard already says True (the trap) ...
        assert fresh.has_recorded_member_loss("stitch", group_id, members[0].token_id) is True
        # ... but nothing has been replayed into THIS process, so replay must proceed.
        assert fresh.has_replayed_member_loss("stitch", group_id, members[0].token_id) is False
        assert fresh.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined") is None
        assert fresh._executor._pending[("stitch", group_id)].lost == {members[0].token_id: "quarantined"}
        # Second replay in the same process: the in-memory marker dedups it.
        assert fresh.has_replayed_member_loss("stitch", group_id, members[0].token_id) is True
        with pytest.raises(OrchestrationInvariantError, match="duplicate loss notification"):
            fresh.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined")
        # The re-staged loss is live roster state: the surviving member's
        # arrival settles the group (require_all -> fails on the loss).
        outcome = fresh.accept(members[1], "stitch", ctx=env.ctx)
        assert outcome.held is False
        assert outcome.failure_reason == "collector_missing_members"

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


class TestRestore:
    """restore_from_journal (Task 7, F1 resume path).

    task-6-7-review-prep.md's C-1/I-5/I-6/I-7/I-8/M-4 plus the META-20b
    AMENDMENT (settled-token-set reconstruction). See CollectorJournalRestorer's
    module docstring for the full design rationale.
    """

    def test_restore_rebuilds_buffers_and_ordinal_flush_survives_takeover(self, collector_env: _CollectorEnv) -> None:
        # Plan's Step 1 headline test, adapted to the real fixture. Arrival
        # order is unrecoverable after takeover, ordinal order is not:
        # journal items handed in reversed order, a subsequent LIVE closing
        # arrival must still flush 0,1,2.
        env = collector_env
        members, _group_id = env.seed_group(count=3)
        env.executor.accept(members[1], "stitch")
        env.executor.accept(members[2], "stitch")
        items = [env.blocked_item_for(m, collector_name="stitch") for m in (members[1], members[2])]
        state_ids = env.state_ids_for(items)
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=list(reversed(items)),
            state_ids=state_ids,
            attempt_offsets={m.token_id: 0 for m in (members[1], members[2])},
            resume_checkpoint_id="ckpt-1",
        )
        assert fresh.buffered_member_count() == 2
        # An INCOMPLETE restored roster is not the flush sweep's to close.
        assert fresh.flush_restored_complete_groups(ctx=env.ctx) == ()
        assert fresh.transform.seen_rows == []
        outcome = fresh.accept(members[0], "stitch", ctx=env.ctx)
        assert outcome.held is False
        assert fresh.transform.seen_rows == [m.row_data.to_dict() for m in members]  # ordinal order

    def test_restore_rejects_unknown_collector(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        item = env.blocked_item_for(members[0], collector_name="not-registered")
        state_ids = env.state_ids_for([item])
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="unknown collector"):
            fresh.restore_from_journal(
                items=[item], state_ids=state_ids, attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )

    def test_restore_requires_empty_executor(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")  # env.executor already has a pending group
        item = env.blocked_item_for(members[0], collector_name="stitch")
        state_ids = env.state_ids_for([item])
        with pytest.raises(OrchestrationInvariantError, match="empty executor"):
            env.executor.restore_from_journal(
                items=[item], state_ids=state_ids, attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )

    def test_restore_missing_state_id_for_a_live_item_is_an_audit_inconsistency(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        item = env.blocked_item_for(members[0], collector_name="stitch")
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="No entry in state_ids"):
            fresh.restore_from_journal(items=[item], state_ids={}, attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1")

    def test_restore_missing_attempt_offset_is_an_audit_inconsistency(self, collector_env: _CollectorEnv) -> None:
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        item = env.blocked_item_for(members[0], collector_name="stitch")
        state_ids = env.state_ids_for([item])
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="No entry in attempt_offsets"):
            fresh.restore_from_journal(items=[item], state_ids=state_ids, attempt_offsets={}, resume_checkpoint_id="ckpt-1")

    def test_restore_out_of_roster_member_crashes(self, collector_env: _CollectorEnv) -> None:
        # I-6: a journal item's frame naming a member_key the durable roster
        # does not contain must raise the SAME AuditIntegrityError the
        # fresh-arrival path gives — restore is not exempt from this
        # cross-check just because the restorer cannot perform it itself
        # (it has no access to _open_group).
        import dataclasses

        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        item = env.blocked_item_for(members[0], collector_name="stitch")
        bogus_frame = LineageFrame(kind=FrameKind.EXPAND, group_id=group_id, member_key="not-a-real-member")
        item = dataclasses.replace(item, lineage_path=(bogus_frame,))
        state_ids = env.state_ids_for([item])
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="durable roster"):
            fresh.restore_from_journal(
                items=[item], state_ids=state_ids, attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )

    def test_restore_ordinal_mismatch_crashes(self, collector_env: _CollectorEnv) -> None:
        # I-6's ordinal twin: roster-valid member, but no token_parents row
        # under the opener (the same cross-table inconsistency FIX 1 /
        # fix round 3 targets on the LIVE path — this is its restore twin).
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        env.delete_token_parents_row(token_id=members[0].token_id)
        item = env.blocked_item_for(members[0], collector_name="stitch")
        state_ids = env.state_ids_for([item])
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="token_parents ordinal"):
            fresh.restore_from_journal(
                items=[item], state_ids=state_ids, attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )

    def test_restore_rejects_a_state_id_that_does_not_name_an_open_hold(self, collector_env: _CollectorEnv) -> None:
        # M-4: presence in the caller's state_ids mapping is not enough — the
        # named state_id must actually be OPEN at this node. A stale or
        # already-completed state_id restored into a _MemberEntry would be
        # double-completed at flush.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        item = env.blocked_item_for(members[0], collector_name="stitch")
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="does not name an OPEN node_state"):
            fresh.restore_from_journal(
                items=[item],
                state_ids={members[0].token_id: "bogus-state-id"},
                attempt_offsets={members[0].token_id: 0},
                resume_checkpoint_id="ckpt-1",
            )

    def test_restore_of_a_complete_roster_flushes_through_the_post_restore_trigger(self, collector_env: _CollectorEnv) -> None:
        # I-8 under journal-before-dispatch (integration plan B7, WS4 fix
        # round 2 (a)): every arrival is journaled BLOCKED before the accept
        # that completes the roster, so a crash between the last adoption
        # and collect_tokens committing leaves a roster COMPLETE in the
        # journal on the ORDINARY crash-mid-flush path. Restore has no
        # PluginContext: it installs the group and parks it for the
        # post-restore flush sweep, which closes it exactly as a live closing
        # accept() would (ordinal order, holds completed, settled memory).
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        # The second member's own accept()-time durable hold, written
        # without the live executor (which would auto-flush the roster away).
        env.factory.execution.begin_node_state(
            token_id=members[1].token_id,
            node_id=str(env.node_id),
            run_id=env.run_id,
            step_index=1,
            input_data=members[1].row_data.to_dict(),
            attempt=0,
            resume_checkpoint_id=None,
        )
        items = [env.blocked_item_for(m, collector_name="stitch") for m in reversed(members)]
        state_ids = env.state_ids_for(items)
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=items,
            state_ids=state_ids,
            attempt_offsets={m.token_id: 0 for m in members},
            resume_checkpoint_id="ckpt-1",
        )
        # Restore installs but never flushes (no PluginContext here).
        assert fresh.buffered_member_count() == 2
        assert fresh.transform.seen_rows == []
        assert env.open_hold_token_ids(node="stitch") == sorted(m.token_id for m in members)

        outcomes = fresh.flush_restored_complete_groups(ctx=env.ctx)

        # Mutant R-1 (restore parks nothing / the sweep closes nothing):
        # the sweep must produce the closing outcome itself.
        assert len(outcomes) == 1
        assert outcomes[0].held is False
        assert outcomes[0].group_id == group_id
        assert [t.token_id for t in outcomes[0].consumed_tokens] == [m.token_id for m in members]
        assert fresh.transform.seen_rows == [m.row_data.to_dict() for m in members]  # ordinal order
        assert fresh.buffered_member_count() == 0
        assert env.open_hold_token_ids(node="stitch") == []
        # Idempotent: the parked keys were consumed.
        assert fresh.flush_restored_complete_groups(ctx=env.ctx) == ()
        # The settled memory a post-closure redelivery needs was recorded by
        # the close path, same as a live closure.
        assert fresh.accept(members[0], "stitch", ctx=env.ctx).held is True
        assert fresh.transform.seen_rows == [m.row_data.to_dict() for m in members]  # no second flush

    def _record_ledger_loss(self, env: _CollectorEnv, *, group_id: str, member: TokenInfo, reason: str, adopted: bool) -> None:
        with env.db.write_connection() as conn:
            record_group_loss(
                conn,
                run_id=env.run_id,
                spec=GroupLossSpec(
                    closer_name="stitch", group_id=group_id, member_key=member.token_id, token_id=member.token_id, reason=reason
                ),
                recorded_by="crashed-leader",
                now=datetime.now(UTC),
            )
            if adopted:
                from sqlalchemy import update

                from elspeth.core.landscape.schema import group_losses_table

                conn.execute(
                    update(group_losses_table)
                    .where(group_losses_table.c.run_id == env.run_id)
                    .where(group_losses_table.c.member_key == member.token_id)
                    .values(adopted_epoch=1)
                )

    def test_restore_rebuilds_pending_lost_from_the_full_ledger_including_adopted_losses(self, collector_env: _CollectorEnv) -> None:
        # WS4 fix round 2 (b), broadened: a loss the crashed leader already
        # ADOPTED is absent from list_unadopted_group_losses, so the
        # journal-first replay can never re-stage it — only a full-ledger
        # rebuild at restore puts it back into the roster accounting.
        env = collector_env
        members, group_id = env.seed_group(count=3)
        env.executor.accept(members[1], "stitch")
        self._record_ledger_loss(env, group_id=group_id, member=members[0], reason="quarantined", adopted=True)
        items = [env.blocked_item_for(members[1], collector_name="stitch")]
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=items, state_ids=env.state_ids_for(items), attempt_offsets={members[1].token_id: 0}, resume_checkpoint_id="ckpt-1"
        )
        # (i) Mutant L-1 (rebuild reads only UNADOPTED losses): the adopted
        # pre-crash loss must be in pending.lost after restore.
        assert fresh._executor._pending[("stitch", group_id)].lost == {members[0].token_id: "quarantined"}
        assert fresh.has_replayed_member_loss("stitch", group_id, members[0].token_id) is True
        # An outstanding member keeps the roster open; the sweep has nothing.
        assert fresh.flush_restored_complete_groups(ctx=env.ctx) == ()
        # (ii) A loss recorded AFTER restore (what the replay actually sees)
        # is re-notified exactly once in this session and settles the group.
        assert fresh.has_replayed_member_loss("stitch", group_id, members[2].token_id) is False
        outcome = fresh.notify_member_lost("stitch", group_id, members[2].token_id, "quarantined")
        assert outcome is not None and outcome.held is False
        assert outcome.failure_reason == "collector_missing_members"
        assert [t.token_id for t in outcome.consumed_tokens] == [members[1].token_id]

    def test_restore_parks_a_roster_completed_by_a_ledger_loss_for_the_flush_sweep(self, collector_env: _CollectorEnv) -> None:
        # A roster complete only once the ledger loss is rebuilt: restore
        # parks it and the sweep renders the require_all verdict through
        # _close_group -> _fail_group, exactly as a live closing loss would.
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[1], "stitch")
        self._record_ledger_loss(env, group_id=group_id, member=members[0], reason="quarantined", adopted=True)
        items = [env.blocked_item_for(members[1], collector_name="stitch")]
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=items, state_ids=env.state_ids_for(items), attempt_offsets={members[1].token_id: 0}, resume_checkpoint_id="ckpt-1"
        )
        outcomes = fresh.flush_restored_complete_groups(ctx=env.ctx)
        assert len(outcomes) == 1
        assert outcomes[0].failure_reason == "collector_missing_members"
        assert fresh.transform.seen_rows == []  # require_all failure never invokes the plugin
        assert fresh.buffered_member_count() == 0
        assert env.open_hold_token_ids(node="stitch") == []

    def test_restore_rejects_a_ledger_loss_for_a_journaled_arrival(self, collector_env: _CollectorEnv) -> None:
        # The same arrived-and-lost invariant notify_member_lost enforces live.
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        self._record_ledger_loss(env, group_id=group_id, member=members[0], reason="quarantined", adopted=False)
        items = [env.blocked_item_for(members[0], collector_name="stitch")]
        fresh = env.fresh_executor()
        with pytest.raises(OrchestrationInvariantError, match="both a journaled arrival and a ledger loss"):
            fresh.restore_from_journal(
                items=items, state_ids=env.state_ids_for(items), attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )
        assert fresh._executor._pending == {}  # validate-before-mutate

    def _park_complete_group(self, env: _CollectorEnv, fresh: _EnvExecutor, count: int = 2) -> tuple[list[TokenInfo], str]:
        """Seed a group, complete it durably outside the live executor, and
        return it ready to be restored as a parked complete roster."""
        members, group_id = env.seed_group(count=count)
        env.executor.accept(members[0], "stitch")
        for member in members[1:]:
            env.factory.execution.begin_node_state(
                token_id=member.token_id,
                node_id=str(env.node_id),
                run_id=env.run_id,
                step_index=1,
                input_data=member.row_data.to_dict(),
                attempt=0,
                resume_checkpoint_id=None,
            )
        return members, group_id

    def test_flush_sweep_resumes_after_a_plugin_exception_on_a_later_group(self, collector_env: _CollectorEnv) -> None:
        # Fix round 3 (I-1): two parked groups, the plugin raises on the
        # SECOND. The first group's close is durable and its outcome must
        # still reach the caller; the second stays parked; the retry sweep
        # delivers the first outcome and closes the second. The earlier
        # shape (clear the parked list, then close in one loop) left the
        # second group installed-but-unparked and the retry returning () —
        # the permanently-unflushable wedge the sweep exists to prevent.
        env = collector_env
        fresh = env.fresh_executor()
        members_a, group_a = self._park_complete_group(env, fresh)
        members_b, group_b = self._park_complete_group(env, fresh)
        items = [env.blocked_item_for(m, collector_name="stitch") for m in (*members_a, *members_b)]
        fresh.restore_from_journal(
            items=items,
            state_ids=env.state_ids_for(items),
            attempt_offsets={m.token_id: 0 for m in (*members_a, *members_b)},
            resume_checkpoint_id="ckpt-1",
        )
        assert fresh._executor._restored_complete_keys == [("stitch", group_a), ("stitch", group_b)]

        calls = {"n": 0}
        original_process = fresh.transform.process

        def raise_on_second(rows: list[PipelineRow], ctx: PluginContext) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom on group b")
            return original_process(rows, ctx)

        fresh.transform.process = raise_on_second  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="boom on group b"):
            fresh.flush_restored_complete_groups(ctx=env.ctx)
        # Mutant P-1 (un-park BEFORE the close): group b would be gone from
        # the parked list here and the retry would return () with b still
        # installed. Group a closed durably; b stays parked and installed.
        assert fresh._executor._restored_complete_keys == [("stitch", group_b)]
        assert ("stitch", group_b) in fresh._executor._pending
        assert ("stitch", group_a) not in fresh._executor._pending
        assert env.open_hold_token_ids(node="stitch") == sorted(m.token_id for m in members_b)

        outcomes = fresh.flush_restored_complete_groups(ctx=env.ctx)
        assert [o.group_id for o in outcomes] == [group_a, group_b]
        assert all(o.held is False for o in outcomes)
        assert fresh._executor._restored_complete_keys == []
        assert fresh.buffered_member_count() == 0
        assert env.open_hold_token_ids(node="stitch") == []
        assert fresh.flush_restored_complete_groups(ctx=env.ctx) == ()

    def test_flush_sweep_refuses_a_parked_group_that_is_no_longer_roster_complete(self, collector_env: _CollectorEnv) -> None:
        # The sweep's guard is for genuinely inconsistent rosters only:
        # nothing between restore and the sweep may legitimately close or
        # reopen a parked group, so a parked key that no longer names an
        # installed, complete group is an invariant violation, not a race.
        env = collector_env
        members, group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        env.factory.execution.begin_node_state(
            token_id=members[1].token_id,
            node_id=str(env.node_id),
            run_id=env.run_id,
            step_index=1,
            input_data=members[1].row_data.to_dict(),
            attempt=0,
            resume_checkpoint_id=None,
        )
        items = [env.blocked_item_for(m, collector_name="stitch") for m in members]
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=items,
            state_ids=env.state_ids_for(items),
            attempt_offsets={m.token_id: 0 for m in members},
            resume_checkpoint_id="ckpt-1",
        )
        del fresh._executor._pending[("stitch", group_id)]  # corrupt: the parked group vanished
        with pytest.raises(OrchestrationInvariantError, match="no longer installed roster-complete"):
            fresh.flush_restored_complete_groups(ctx=env.ctx)

    def test_takeover_reflush_after_crash_mid_flush_uses_a_distinct_attempt(self, collector_env: _CollectorEnv) -> None:
        # C-1 / META-14.1's exact required trace: "crash mid-flush ->
        # takeover -> re-flush green at attempt=1." Constructed by hand (two
        # accept() calls plus a manual begin_node_state for the opener).
        # Integration ratified journal-before-dispatch (plan B7), so the
        # closing arrival IS journaled BLOCKED in production and the
        # crash-mid-flush roster is complete in the journal — that shape is
        # the complete-roster test above (restore + post-restore flush
        # sweep). This test keeps the OTHER half of the trace: the closing
        # member's journal row was lost/never written, restore sees the N-1
        # earlier arrivals only, and a later LIVE arrival for the missing
        # member must re-flush at a DISTINCT attempt, not collide with the
        # orphaned attempt=0 flush-guard state.
        env = collector_env
        members, group_id = env.seed_group(count=3)
        env.executor.accept(members[1], "stitch")
        env.executor.accept(members[2], "stitch")
        record = env.factory.barrier_restore.get_group_record(run_id=env.run_id, group_id=group_id)
        assert record is not None
        env.factory.execution.begin_node_state(
            token_id=record.opener_token_id,
            node_id=str(env.node_id),
            run_id=env.run_id,
            step_index=1,
            input_data={"batch_rows": []},
            attempt=0,
            resume_checkpoint_id=None,
        )
        items = [env.blocked_item_for(m, collector_name="stitch") for m in (members[1], members[2])]
        state_ids = env.state_ids_for(items)
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=items,
            state_ids=state_ids,
            attempt_offsets={m.token_id: 0 for m in (members[1], members[2])},
            resume_checkpoint_id="ckpt-1",
        )
        outcome = fresh.accept(members[0], "stitch", ctx=env.ctx)
        assert outcome.held is False
        assert fresh.transform.seen_rows == [m.row_data.to_dict() for m in members]  # the re-flush actually ran

        from sqlalchemy import select

        with env.db.connection() as conn:
            attempts = sorted(
                row.attempt
                for row in conn.execute(
                    select(node_states_table.c.attempt)
                    .where(node_states_table.c.run_id == env.run_id)
                    .where(node_states_table.c.node_id == str(env.node_id))
                    .where(node_states_table.c.token_id == record.opener_token_id)
                )
            )
        # Mutant 14: the flush guard's attempt left at the literal 0 would
        # either collide (IntegrityError, test never reaches here) or,
        # if silently overwritten, leave only ONE row. Two DISTINCT
        # attempts (the orphaned 0, and the real re-flush's 1) proves both
        # that no collision occurred AND that attempt genuinely advanced.
        assert attempts == [0, 1]
        # Mutant 15 (I-5): the re-flushed members' duration_ms must not be a
        # boot-relative garbage number from a 0.0-seeded arrival_time —
        # restore-time monotonic keeps it small and truthful (post-restore
        # residence, not total hold time).
        with env.db.connection() as conn:
            durations = [
                row.duration_ms
                for row in conn.execute(
                    select(node_states_table.c.duration_ms)
                    .where(node_states_table.c.run_id == env.run_id)
                    .where(node_states_table.c.node_id == str(env.node_id))
                    .where(node_states_table.c.token_id.in_([members[1].token_id, members[2].token_id]))
                    .where(node_states_table.c.status == "completed")
                )
            ]
        assert durations and all(0 <= d < 60_000 for d in durations)  # sane, not boot-relative

    def test_restore_of_an_already_closed_group_settled_redelivery_skips_without_a_second_flush_call(
        self, collector_env: _CollectorEnv
    ) -> None:
        # META-20b takeover flavor (the AMENDMENT's required test): settling
        # done by env.executor, restore+redelivery by a SECOND executor
        # instance against the SAME Landscape — the property that
        # distinguishes "reconstructed from durable state" from "happened
        # to still be in memory". Also C-1's second required trace: "crash
        # post-flush -> takeover -> NO second plugin call."
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")
        assert outcome.held is False
        assert env.transform.call_count == 1

        fresh = env.fresh_executor()
        fresh.restore_from_journal(items=[], state_ids={}, attempt_offsets={}, resume_checkpoint_id="ckpt-1")
        # Restore did NOT re-open the group — the reconstruction populated
        # _completed_keys, not a resurrected pending entry.
        assert fresh._executor._pending == {}

        redelivered = fresh.accept(members[0], "stitch", ctx=env.ctx)
        assert redelivered.held is True  # the skip shape, not merely "no crash"
        assert fresh.transform.call_count == 0  # NO second plugin call, on the FRESH instance

    def test_restore_of_an_already_closed_group_distinct_token_redelivery_still_raises(self, collector_env: _CollectorEnv) -> None:
        # The counter-case the skip test above sits beside (mutant guard):
        # a token that was NOT among the settled members must still raise
        # after restore — proves the reconstruction returns the REAL
        # settled set, not an accept-everything shortcut.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        env.executor.accept(members[1], "stitch")
        # A DISTINCT token for the same settled member (same EXPAND frame,
        # fresh token_id) — must still raise, unlike a SAME-token
        # redelivery, which the sibling test above proves is a skip.
        distinct = env.reissue_with_new_token_id(members[0])

        fresh = env.fresh_executor()
        fresh.restore_from_journal(items=[], state_ids={}, attempt_offsets={}, resume_checkpoint_id="ckpt-1")
        with pytest.raises(AuditIntegrityError, match="not among the members"):
            fresh.accept(distinct, "stitch", ctx=env.ctx)

    def test_restore_drops_post_closure_residual_items_without_raising(self, collector_env: _CollectorEnv) -> None:
        # The step-order fix a reviewer must be able to see directly: a
        # BLOCKED-looking journal row for a member of an ALREADY-completed
        # group has no entry in state_ids/attempt_offsets (its hold is
        # COMPLETED, not OPEN — the caller derives those mappings from OPEN
        # holds only). Validating coverage BEFORE partitioning completed
        # groups would reject this as journal corruption on a perfectly
        # healthy tree; partitioning FIRST drops it silently instead.
        env = collector_env
        members, _group_id = env.seed_group(count=2)
        env.executor.accept(members[0], "stitch")
        env.executor.accept(members[1], "stitch")
        stale_item = env.blocked_item_for(members[0], collector_name="stitch")  # residual, COMPLETED not OPEN

        fresh = env.fresh_executor()
        fresh.restore_from_journal(items=[stale_item], state_ids={}, attempt_offsets={}, resume_checkpoint_id="ckpt-1")
        assert fresh._executor._pending == {}
        assert fresh._executor._completed_keys

    def test_restore_cross_checks_durable_vs_config_collector_node_and_raises_on_divergence(self, collector_env: _CollectorEnv) -> None:
        # META-22: an item declares collector_name="stitch" (config node =
        # env.node_id) for a group that durably completed at a completely
        # DIFFERENT, foreign node — durable and config derivations of "the
        # collector node for this group" disagree. Must fail loudly rather
        # than silently trusting either side (misclassifying a residual, or
        # routing a live arrival into the wrong collector's roster rebuild).
        env = collector_env
        members, group_id = env.seed_group(count=2)
        foreign_node_id = register_test_node(env.factory.data_flow, env.run_id, "some-other-node")
        member_a_frame = members[0].lineage_path[-1]
        assert member_a_frame.group_id == group_id
        state = env.factory.execution.begin_node_state(
            token_id=members[0].token_id,
            node_id=foreign_node_id,
            run_id=env.run_id,
            step_index=1,
            input_data=members[0].row_data.to_dict(),
            attempt=0,
            resume_checkpoint_id=None,
        )
        env.factory.execution.complete_node_state(state.state_id, NodeStateStatus.COMPLETED, output_data={}, duration_ms=1.0)

        item = env.blocked_item_for(members[1], collector_name="stitch")
        state_ids = env.state_ids_for([item])
        fresh = env.fresh_executor()
        with pytest.raises(AuditIntegrityError, match="disagree"):
            fresh.restore_from_journal(
                items=[item], state_ids=state_ids, attempt_offsets={members[1].token_id: 0}, resume_checkpoint_id="ckpt-1"
            )
