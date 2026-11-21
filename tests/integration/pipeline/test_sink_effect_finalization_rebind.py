"""Finalization members are copied only when recovery rebinds their ordinal.

``SinkEffectCoordinator._request_for_effect`` pairs each finalization member
with its effect-input member and takes the ordinal from the durable row. A
member whose ordinal already matches is passed through; one whose ordinal
differs (a batch spanning several effects) is rebound.
"""

from __future__ import annotations

import cProfile
from collections.abc import Callable
from pathlib import Path

import pytest

from elspeth.contracts import NodeType, PendingOutcome, TerminalOutcome, TerminalPath, TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.sink_effects import SinkEffectFinalizationMember, SinkEffectReconcileResult, SinkEffectRole, SinkEffectState
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
from elspeth.engine.spans import SpanFactory
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.fixtures.sink_effects import DuplicateObservableTarget, PartitioningObservableSink
from tests.integration.pipeline.test_sink_effect_recovery import (
    _PENDING_SUCCESS,
    _effect_tokens,
    _executor_for,
    _fail_once,
    _failsink_setup,
    _StreamingObservableSink,
)


def _profiled_calls(profiler: cProfile.Profile, function: Callable[..., object]) -> int:
    """Count calls of one exact function's code in a cProfile run."""
    return sum(entry.callcount for entry in profiler.getstats() if entry.code is function.__code__)


def test_finalization_members_are_rebuilt_only_when_recovery_rebinds_the_ordinal(tmp_path: Path) -> None:
    """A fresh partition keeps each finalization member; recovery rebinds ordinals.

    ``_request_for_effect`` pairs finalization members with effect-input members
    and overwrites the ordinal with the durable one. On a fresh effect the
    ordinals already agree, so the member is passed through unchanged (no copy).
    When a recovered batch spans two effects, the second effect's durable
    ordinals restart at zero and the overwrite must still happen: the
    dispositions below are only correct if it does.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'finalization-ordinals.db'}")
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "primary", node_type=NodeType.SINK, plugin_name="partitioning")
        tokens = _effect_tokens(
            factory,
            run_id=run.run_id,
            source_id=source_id,
            rows=[{"value": 1, "divert": True}, {"value": 2}, {"value": 3, "divert": True}, {"value": 4}],
        )
        ctx = PluginContext(run_id=run.run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=sink_id)
        pending = PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW)
        # One target per effect, as a stream's successive effects publish separately.
        first_target = DuplicateObservableTarget()
        second_target = DuplicateObservableTarget()
        constructor = SinkEffectFinalizationMember.__post_init__

        def executor(active_factory: RecorderFactory, **kwargs: object) -> SinkExecutor:
            return SinkExecutor(
                active_factory.execution,
                active_factory.data_flow,
                SpanFactory(),
                run.run_id,
                factory=active_factory,
                worker_id="worker-a",
                coordination_token=leader_coordination_token(active_factory, run.run_id),
                **kwargs,
            )

        def sink(target: DuplicateObservableTarget) -> PartitioningObservableSink:
            created = PartitioningObservableSink(target, name="primary")
            created.node_id = sink_id
            return created

        # Fresh effect for tokens 0-1: two finalization members built by the
        # sink executor, none copied by the coordinator.
        fresh = cProfile.Profile()
        fresh.enable()
        try:
            executor(factory).write(
                sink(first_target),
                tokens[:2],
                ctx,
                1,
                sink_name="output",
                pending_outcome=pending,
                effect_mode="write",
                join_group_id_by_token={t.token_id: None for t in tokens[:2]},
            )
        finally:
            fresh.disable()
        assert _profiled_calls(fresh, constructor) == 2

        calls = 0

        def fail_once(observed: SinkEffectExecutionSeam) -> None:
            nonlocal calls
            if observed is SinkEffectExecutionSeam.BEFORE_EFFECT and calls == 0:
                calls += 1
                raise SinkEffectInjectedFault(observed)

        with pytest.raises(SinkEffectInjectedFault):
            executor(factory, sink_effect_fault_hook=fail_once).write(
                sink(second_target),
                tokens[2:],
                ctx,
                1,
                sink_name="output",
                pending_outcome=pending,
                effect_mode="write",
                join_group_id_by_token={t.token_id: None for t in tokens[2:]},
            )

        # Recover the union batch: caller ordinals 0-3, the open second effect's
        # durable ordinals 0-1 for tokens 2-3. Four built by the executor plus
        # exactly two ordinal rebinds (2 -> 0, 3 -> 1); the finalized first
        # effect is loaded, not re-requested.
        recovered_factory = make_factory(db)
        recovered = cProfile.Profile()
        recovered.enable()
        try:
            executor(recovered_factory).write(
                sink(second_target),
                tokens,
                ctx,
                1,
                sink_name="output",
                pending_outcome=pending,
                effect_mode="write",
                on_token_written=lambda token: None,
                join_group_id_by_token={t.token_id: None for t in tokens},
            )
        finally:
            recovered.disable()
        assert _profiled_calls(recovered, constructor) == 4 + 2

        effects = recovered_factory.execution.sink_effects.get_effects_for_run(run.run_id)
        assert [effect.state for effect in effects] == [SinkEffectState.FINALIZED, SinkEffectState.FINALIZED]
        second = recovered_factory.execution.sink_effects.get_members(effects[1].effect_id)
        assert [(member.token_id, member.ordinal) for member in second] == [(tokens[2].token_id, 0), (tokens[3].token_id, 1)]
        for diverted_token in (tokens[0], tokens[2]):
            outcome = recovered_factory.data_flow.get_token_outcome(diverted_token.token_id)
            assert outcome is not None
            assert (outcome.outcome, outcome.path) == (TerminalOutcome.FAILURE, TerminalPath.SINK_DISCARDED)
        for accepted_token in (tokens[1], tokens[3]):
            outcome = recovered_factory.data_flow.get_token_outcome(accepted_token.token_id)
            assert outcome is not None
            assert (outcome.outcome, outcome.path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
    finally:
        db.close()


class _ReconcilingPrimary(PartitioningObservableSink):
    def reconcile_effect(self, plan: object, ctx: object) -> SinkEffectReconcileResult:
        return _StreamingObservableSink.reconcile_effect(self, plan, ctx)


@pytest.mark.parametrize(
    ("seam", "expected_constructions", "primary_effects"),
    [
        (None, 18, 2),
        (SinkEffectExecutionSeam.BEFORE_RESERVATION, 14, 1),
        (SinkEffectExecutionSeam.BEFORE_EFFECT, 16, 2),
        (SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE, 16, 2),
    ],
    ids=["no-crash", "before-reservation", "before-effect", "after-return-before-finalize"],
)
def test_multi_effect_redrive_rebinds_finalization_ordinals_and_copies_nothing_else(
    tmp_path: Path, seam: SinkEffectExecutionSeam | None, expected_constructions: int, primary_effects: int
) -> None:
    """Primary + linked failsink: a batch of two (interrupted at ``seam``) and
    two re-drives of the wider batch of four. When the first batch reserved an
    effect, each re-drive spans that effect and a new one whose durable
    ordinals restart at zero, so the coordinator must rebind those finalization
    ordinals; without the rebind the re-drive fails with ``sink result accepted
    unknown member ordinal`` (the negative control). Every
    other finalization member is passed through, so the construction count is
    the executor's members plus the rebinds only (base 019a98e15 builds 20-22
    here: it copies every member, changed or not).
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'redrive.db'}")
    try:
        rows: list[dict[str, object]] = [{"v": 1}, {"v": 2, "divert": True}, {"v": 3}, {"v": 4, "divert": True}]
        factory, run_id, primary_id, failsink_id, edge_id, tokens, ctx = _failsink_setup(db, rows)
        primary_target = DuplicateObservableTarget()
        failsink_target = DuplicateObservableTarget()

        def write(active_factory: RecorderFactory, batch: list[TokenInfo], hook: Callable[[SinkEffectExecutionSeam], None] | None) -> None:
            primary = _ReconcilingPrimary(primary_target, name="primary")
            primary.node_id = primary_id
            failsink = _ReconcilingPrimary(failsink_target, name="failsink", divert_rows=False)
            failsink.node_id = failsink_id
            _executor_for(active_factory, run_id, fault_hook=hook).write(
                primary,
                batch,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                failsink=failsink,
                failsink_name="failsink",
                failsink_effect_mode="write",
                failsink_edge_id=edge_id,
                join_group_id_by_token={t.token_id: None for t in batch},
            )

        profiler = cProfile.Profile()
        profiler.enable()
        try:
            if seam is None:
                write(factory, tokens[:2], None)
            else:
                with pytest.raises(SinkEffectInjectedFault):
                    write(factory, tokens[:2], _fail_once(seam))
            write(make_factory(db), tokens, None)
            final_factory = make_factory(db)
            write(final_factory, tokens, None)
        finally:
            profiler.disable()

        assert _profiled_calls(profiler, SinkEffectFinalizationMember.__post_init__) == expected_constructions
        effects = final_factory.execution.sink_effects.get_effects_for_run(run_id)
        assert effects and all(effect.state is SinkEffectState.FINALIZED for effect in effects)
        primaries = sorted(
            (effect for effect in effects if effect.role is SinkEffectRole.PRIMARY),
            key=lambda effect: -1 if effect.stream_sequence is None else effect.stream_sequence,
        )
        assert len(primaries) == primary_effects
        if primary_effects == 2:
            # Caller ordinals 2 and 3 are durable ordinals 0 and 1 here: the rebind ran.
            later = final_factory.execution.sink_effects.get_members(primaries[1].effect_id)
            assert [(member.token_id, member.ordinal) for member in later] == [(tokens[2].token_id, 0), (tokens[3].token_id, 1)]
        for index, token in enumerate(tokens):
            outcome = final_factory.data_flow.get_token_outcome(token.token_id)
            assert outcome is not None
            expected_path = TerminalPath.SINK_FALLBACK_TO_FAILSINK if rows[index].get("divert") else TerminalPath.DEFAULT_FLOW
            assert outcome.path is expected_path
    finally:
        db.close()
