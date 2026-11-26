"""Boundary rejection of a redrive batch preserves completed and effect-bound members."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_primary_executor_contract_preserves_finalized_member(tmp_path: Path) -> None:
    from elspeth.contracts import NodeStateStatus, TerminalOutcome, TerminalPath
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget
    from tests.integration.pipeline.test_sink_effect_recovery import (
        _PENDING_SUCCESS,
        _executor_for,
        _primary_setup,
        _RequiredFieldsPrimarySink,
    )

    db = LandscapeDB(f"sqlite:///{tmp_path / 'finalized.db'}")
    try:
        factory, run_id, sink_id, tokens, ctx = _primary_setup(db, [{"value": 1, "must_exist": True}, {"value": 2}, {"value": 3}])
        sink = _RequiredFieldsPrimarySink(DuplicateObservableTarget(), name="primary")
        sink.node_id = sink_id
        _executor_for(factory, run_id).write(
            as_sink(sink),
            tokens[:1],
            ctx,
            1,
            sink_name="output",
            pending_outcome=_PENDING_SUCCESS,
            effect_mode="write",
            join_group_id_by_token={tokens[0].token_id: None},
        )
        finalized_state = factory.query.get_node_states_for_token(tokens[0].token_id)
        finalized_outcome = factory.data_flow.get_token_outcome(tokens[0].token_id)
        assert len(finalized_state) == 1 and finalized_state[0].status is NodeStateStatus.COMPLETED
        assert finalized_outcome is not None
        before_operations = factory.execution.get_operations_for_run(run_id)

        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            _executor_for(factory, run_id).write(
                as_sink(sink),
                tokens,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                join_group_id_by_token={token.token_id: None for token in tokens},
            )

        assert factory.query.get_node_states_for_token(tokens[0].token_id) == finalized_state
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) == finalized_outcome
        for token in tokens[1:]:
            states = [state for state in factory.query.get_node_states_for_token(token.token_id) if state.node_id == sink_id]
            assert len(states) == 1 and states[0].status is NodeStateStatus.FAILED
            outcome = factory.data_flow.get_token_outcome(token.token_id)
            assert outcome is not None and outcome.outcome is TerminalOutcome.FAILURE
            assert outcome.path is TerminalPath.UNROUTED
        before_ids = {operation.operation_id for operation in before_operations}
        added = [operation for operation in factory.execution.get_operations_for_run(run_id) if operation.operation_id not in before_ids]
        assert len(added) == 1 and added[0].operation_type == "sink_write" and added[0].status == "failed"
    finally:
        db.close()


def test_primary_boundary_preserves_published_interrupted_member(tmp_path: Path) -> None:
    from elspeth.contracts import NodeStateStatus, TerminalOutcome, TerminalPath
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget
    from tests.integration.pipeline.test_sink_effect_recovery import (
        _PENDING_SUCCESS,
        _executor_for,
        _fail_once,
        _primary_setup,
        _RequiredFieldsPrimarySink,
    )

    db = LandscapeDB(f"sqlite:///{tmp_path / 'interrupted.db'}")
    try:
        factory, run_id, sink_id, tokens, ctx = _primary_setup(db, [{"value": 1, "must_exist": True}, {"value": 2}])
        target = DuplicateObservableTarget()
        sink = _RequiredFieldsPrimarySink(target, name="primary")
        sink.node_id = sink_id
        with pytest.raises(SinkEffectInjectedFault):
            _executor_for(factory, run_id, fault_hook=_fail_once(SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE)).write(
                as_sink(sink),
                tokens[:1],
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                join_group_id_by_token={tokens[0].token_id: None},
            )
        assert target.publication_count == 1
        before_operations = factory.execution.get_operations_for_run(run_id)
        recovered = _executor_for(factory, run_id)
        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            recovered.write(
                as_sink(sink),
                tokens,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                join_group_id_by_token={token.token_id: None for token in tokens},
            )

        a_states = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == sink_id]
        assert len(a_states) == 1 and a_states[0].status is NodeStateStatus.OPEN
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) is None
        b_states = [state for state in factory.query.get_node_states_for_token(tokens[1].token_id) if state.node_id == sink_id]
        assert len(b_states) == 1 and b_states[0].status is NodeStateStatus.FAILED
        b_outcome = factory.data_flow.get_token_outcome(tokens[1].token_id)
        assert b_outcome is not None and b_outcome.outcome is TerminalOutcome.FAILURE
        assert b_outcome.path is TerminalPath.UNROUTED
        before_ids = {operation.operation_id for operation in before_operations}
        added = [operation for operation in factory.execution.get_operations_for_run(run_id) if operation.operation_id not in before_ids]
        assert len(added) == 1 and added[0].operation_type == "sink_write" and added[0].status == "failed"

        _executor_for(factory, run_id).write(
            as_sink(sink),
            tokens[:1],
            ctx,
            1,
            sink_name="output",
            pending_outcome=_PENDING_SUCCESS,
            effect_mode="write",
            join_group_id_by_token={tokens[0].token_id: None},
        )
        a_states = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == sink_id]
        assert len(a_states) == 1 and a_states[0].status is NodeStateStatus.COMPLETED
        a_outcome = factory.data_flow.get_token_outcome(tokens[0].token_id)
        assert a_outcome is not None and a_outcome.outcome is TerminalOutcome.SUCCESS
        assert target.publication_count == 1
    finally:
        db.close()


def test_primary_boundary_fails_open_nonmember_interrupted_before_reservation(tmp_path: Path) -> None:
    from collections.abc import Callable

    from elspeth.contracts import NodeStateStatus, SinkEffectRole, TerminalOutcome, TerminalPath, TokenInfo
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget
    from tests.integration.pipeline.test_sink_effect_recovery import (
        _PENDING_SUCCESS,
        _executor_for,
        _fail_once,
        _primary_setup,
        _RequiredFieldsPrimarySink,
    )

    db = LandscapeDB(f"sqlite:///{tmp_path / 'open-nonmember.db'}")
    try:
        factory, run_id, sink_id, tokens, ctx = _primary_setup(db, [{"value": 1, "must_exist": True}, {"value": 2}])
        sink = _RequiredFieldsPrimarySink(DuplicateObservableTarget(), name="primary")
        sink.node_id = sink_id

        def write(batch: list[TokenInfo], hook: Callable[[SinkEffectExecutionSeam], None] | None = None) -> None:
            _executor_for(factory, run_id, fault_hook=hook).write(
                as_sink(sink),
                batch,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                join_group_id_by_token={token.token_id: None for token in batch},
            )

        with pytest.raises(SinkEffectInjectedFault):
            write(tokens[:1], _fail_once(SinkEffectExecutionSeam.BEFORE_RESERVATION))
        a_states = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == sink_id]
        assert len(a_states) == 1 and a_states[0].status is NodeStateStatus.OPEN
        assert not factory.execution.sink_effects.get_members_for_tokens(
            run_id=run_id, sink_node_id=sink_id, role=SinkEffectRole.PRIMARY, token_ids=(tokens[0].token_id,)
        )
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) is None

        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            write(tokens)
        for token in tokens:
            states = [state for state in factory.query.get_node_states_for_token(token.token_id) if state.node_id == sink_id]
            assert len(states) == 1 and states[0].status is NodeStateStatus.FAILED
            outcome = factory.data_flow.get_token_outcome(token.token_id)
            assert outcome is not None and outcome.outcome is TerminalOutcome.FAILURE
            assert outcome.path is TerminalPath.UNROUTED
    finally:
        db.close()


def test_primary_boundary_preserves_diverted_member_with_open_anchor(tmp_path: Path) -> None:
    from collections.abc import Callable

    from elspeth.contracts import NodeStateStatus, SinkEffectRole, TokenInfo
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget
    from tests.integration.pipeline import test_sink_effect_recovery as recovery

    db = LandscapeDB(f"sqlite:///{tmp_path / 'primary-divert-anchor.db'}")
    try:
        factory, run_id, primary_id, failsink_id, edge_id, tokens, ctx = recovery._failsink_setup(
            db, [{"value": 1, "must_exist": True, "divert": True}, {"value": 2}]
        )
        primary = recovery._RequiredFieldsPrimarySink(DuplicateObservableTarget(), name="primary", divert_rows=True)
        primary.node_id = primary_id
        failsink = recovery._RejectingFailsink(DuplicateObservableTarget(), name="failsink", divert_rows=False)
        failsink.node_id = failsink_id

        def write(batch: list[TokenInfo], hook: Callable[[SinkEffectExecutionSeam], None] | None = None) -> None:
            recovery._executor_for(factory, run_id, fault_hook=hook).write(
                as_sink(primary),
                batch,
                ctx,
                1,
                sink_name="output",
                pending_outcome=recovery._PENDING_SUCCESS,
                effect_mode="write",
                failsink=as_sink(failsink),
                failsink_name="failsink",
                failsink_effect_mode="write",
                failsink_edge_id=edge_id,
                join_group_id_by_token={token.token_id: None for token in batch},
            )

        seen = [0]

        def interrupt_failsink(observed: SinkEffectExecutionSeam) -> None:
            if observed is SinkEffectExecutionSeam.BEFORE_RESERVATION:
                seen[0] += 1
                if seen[0] == 2:
                    raise SinkEffectInjectedFault(observed)

        with pytest.raises(SinkEffectInjectedFault):
            write(tokens[:1], interrupt_failsink)
        a_anchor = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == primary_id]
        assert len(a_anchor) == 1 and a_anchor[0].status is NodeStateStatus.OPEN
        assert factory.execution.sink_effects.get_members_for_tokens(
            run_id=run_id, sink_node_id=primary_id, role=SinkEffectRole.PRIMARY, token_ids=(tokens[0].token_id,)
        )
        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            write(tokens)
        assert [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == primary_id] == a_anchor
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) is None
        b_anchor = [state for state in factory.query.get_node_states_for_token(tokens[1].token_id) if state.node_id == primary_id]
        assert len(b_anchor) == 1 and b_anchor[0].status is NodeStateStatus.FAILED
    finally:
        db.close()


def test_failsink_executor_contract_preserves_finalized_member(tmp_path: Path) -> None:
    from elspeth.contracts import NodeStateStatus, TerminalOutcome, TerminalPath, TokenInfo
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget, PartitioningObservableSink
    from tests.integration.pipeline.test_sink_effect_recovery import (
        _PENDING_SUCCESS,
        _executor_for,
        _failsink_setup,
        _RejectingFailsink,
    )

    db = LandscapeDB(f"sqlite:///{tmp_path / 'failsink-finalized.db'}")
    try:
        factory, run_id, primary_id, failsink_id, edge_id, tokens, ctx = _failsink_setup(
            db,
            [
                {"value": 1, "must_exist": True, "divert": True},
                {"value": 2, "divert": True},
                {"value": 3, "divert": True},
            ],
        )
        primary = PartitioningObservableSink(DuplicateObservableTarget(), name="primary")
        primary.node_id = primary_id
        failsink = _RejectingFailsink(DuplicateObservableTarget(), name="failsink", divert_rows=False)
        failsink.node_id = failsink_id

        def write(batch: list[TokenInfo]) -> None:
            _executor_for(factory, run_id).write(
                as_sink(primary),
                batch,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                failsink=as_sink(failsink),
                failsink_name="failsink",
                failsink_effect_mode="write",
                failsink_edge_id=edge_id,
                join_group_id_by_token={token.token_id: None for token in batch},
            )

        write(tokens[:1])
        finalized_states = factory.query.get_node_states_for_token(tokens[0].token_id)
        finalized_outcome = factory.data_flow.get_token_outcome(tokens[0].token_id)
        finalized_failsink = [state for state in finalized_states if state.node_id == failsink_id]
        assert len(finalized_failsink) == 1 and finalized_failsink[0].status is NodeStateStatus.COMPLETED
        assert finalized_outcome is not None
        before_operations = factory.execution.get_operations_for_run(run_id)
        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            write(tokens)

        assert factory.query.get_node_states_for_token(tokens[0].token_id) == finalized_states
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) == finalized_outcome
        for token in tokens[1:]:
            states = factory.query.get_node_states_for_token(token.token_id)
            failsink_states = [state for state in states if state.node_id == failsink_id]
            primary_states = [state for state in states if state.node_id == primary_id]
            assert len(failsink_states) == 1 and failsink_states[0].status is NodeStateStatus.FAILED
            assert len(primary_states) == 1 and primary_states[0].status is NodeStateStatus.FAILED
            outcome = factory.data_flow.get_token_outcome(token.token_id)
            assert outcome is not None and outcome.outcome is TerminalOutcome.FAILURE
            assert outcome.path is TerminalPath.UNROUTED
        before_ids = {operation.operation_id for operation in before_operations}
        added = [operation for operation in factory.execution.get_operations_for_run(run_id) if operation.operation_id not in before_ids]
        assert len([operation for operation in added if operation.node_id == failsink_id and operation.status == "failed"]) == 1
    finally:
        db.close()


def test_failsink_boundary_preserves_published_interrupted_member(tmp_path: Path) -> None:
    from elspeth.contracts import NodeStateStatus, TerminalOutcome, TerminalPath, TokenInfo
    from elspeth.contracts.errors import SinkTransactionalInvariantError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.executors.sink_effects import SinkEffectExecutionSeam, SinkEffectInjectedFault
    from tests.fixtures.base_classes import as_sink
    from tests.fixtures.sink_effects import DuplicateObservableTarget, PartitioningObservableSink
    from tests.integration.pipeline.test_sink_effect_recovery import (
        _PENDING_SUCCESS,
        _executor_for,
        _fail_once,
        _failsink_setup,
        _RejectingFailsink,
    )

    db = LandscapeDB(f"sqlite:///{tmp_path / 'failsink-interrupted.db'}")
    try:
        factory, run_id, primary_id, failsink_id, edge_id, tokens, ctx = _failsink_setup(
            db,
            [
                {"value": 1, "must_exist": True, "divert": True},
                {"value": 2, "divert": True},
            ],
        )
        primary = PartitioningObservableSink(DuplicateObservableTarget(), name="primary")
        primary.node_id = primary_id
        failsink_target = DuplicateObservableTarget()
        failsink = _RejectingFailsink(failsink_target, name="failsink", divert_rows=False)
        failsink.node_id = failsink_id

        def write(batch: list[TokenInfo], *, fault: bool = False) -> None:
            hook = _fail_once(SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE) if fault else None
            _executor_for(factory, run_id, fault_hook=hook).write(
                as_sink(primary),
                batch,
                ctx,
                1,
                sink_name="output",
                pending_outcome=_PENDING_SUCCESS,
                effect_mode="write",
                failsink=as_sink(failsink),
                failsink_name="failsink",
                failsink_effect_mode="write",
                failsink_edge_id=edge_id,
                join_group_id_by_token={token.token_id: None for token in batch},
            )

        with pytest.raises(SinkEffectInjectedFault):
            write(tokens[:1], fault=True)
        assert failsink_target.publication_count == 1
        a_failsink_before = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == failsink_id]
        a_primary_before = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == primary_id]
        assert len(a_failsink_before) == 1 and a_failsink_before[0].status is NodeStateStatus.OPEN
        assert len(a_primary_before) == 1 and a_primary_before[0].status is NodeStateStatus.OPEN
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) is None
        before_operations = factory.execution.get_operations_for_run(run_id)

        with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
            write(tokens)

        b_states = factory.query.get_node_states_for_token(tokens[1].token_id)
        b_failsink = [state for state in b_states if state.node_id == failsink_id]
        b_primary = [state for state in b_states if state.node_id == primary_id]
        assert len(b_failsink) == 1 and b_failsink[0].status is NodeStateStatus.FAILED
        assert len(b_primary) == 1 and b_primary[0].status is NodeStateStatus.FAILED
        b_outcome = factory.data_flow.get_token_outcome(tokens[1].token_id)
        assert b_outcome is not None and b_outcome.outcome is TerminalOutcome.FAILURE
        assert b_outcome.path is TerminalPath.UNROUTED
        assert [
            state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == failsink_id
        ] == a_failsink_before
        assert [
            state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == primary_id
        ] == a_primary_before
        assert factory.data_flow.get_token_outcome(tokens[0].token_id) is None
        before_ids = {operation.operation_id for operation in before_operations}
        added = [operation for operation in factory.execution.get_operations_for_run(run_id) if operation.operation_id not in before_ids]
        failed_writes = [operation for operation in added if operation.operation_type == "sink_write" and operation.status == "failed"]
        assert len(failed_writes) == 1 and failed_writes[0].node_id == failsink_id

        write(tokens[:1])
        a_failsink_after = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == failsink_id]
        a_primary_after = [state for state in factory.query.get_node_states_for_token(tokens[0].token_id) if state.node_id == primary_id]
        assert len(a_failsink_after) == 1 and a_failsink_after[0].status is NodeStateStatus.COMPLETED
        assert len(a_primary_after) == 1 and a_primary_after[0].status is NodeStateStatus.FAILED
        a_outcome = factory.data_flow.get_token_outcome(tokens[0].token_id)
        assert a_outcome is not None and a_outcome.outcome is TerminalOutcome.TRANSIENT
        assert a_outcome.path is TerminalPath.SINK_FALLBACK_TO_FAILSINK
        assert failsink_target.publication_count == 1
    finally:
        db.close()
