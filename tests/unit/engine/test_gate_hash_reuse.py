"""Gate input hashes come from the state that the Landscape opens."""

from __future__ import annotations

import json
import sys
import uuid
from collections import Counter
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.contracts import RouteDestination
from elspeth.contracts.enums import NodeStateStatus, NodeType
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import stable_hash
from elspeth.core.config import GateSettings
from elspeth.core.expression_parser import ExpressionEvaluationError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, nodes_table
from elspeth.engine.executors import GateExecutor
from tests.fixtures.executors import _make_factory, _make_span_factory, _make_token
from tests.fixtures.factories import make_context
from tests.unit.engine import test_hot_path_row_cost as pins
from tests.unit.engine.conftest import make_test_step_resolver


@pytest.mark.parametrize("shape", ("linear", "fork"))
def test_gate_input_is_hashed_once_and_recorded_unchanged(shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Count actual core hash calls by lexical caller, then read the DB rows."""
    counts: Counter[int] = Counter()
    ids = count(1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(ids) << 80))
    hash_code = stable_hash.__code__
    gate_code = GateExecutor.execute_config_gate.__code__
    in_gate = False

    def profile(frame, event, _arg):
        nonlocal in_gate
        if frame.f_code is gate_code:
            in_gate = event == "call"
            return
        if not in_gate or event != "call" or frame.f_code is not hash_code:
            return
        caller = frame.f_back
        assert caller is not None
        if (Path(caller.f_code.co_filename).name, caller.f_code.co_name) not in {
            ("gate.py", "execute_config_gate"),
            ("node_states.py", "begin_node_state"),
        }:
            return
        row = frame.f_locals["obj"]
        if isinstance(row, dict) and set(row) == {"value"} and type(row["value"]) is int:
            counts[row["value"]] += 1

    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        pins._pipeline(shape, 2, db, tmp_path / "payload")
    finally:
        sys.setprofile(previous)
    try:
        with db.engine.connect() as conn:
            recorded = conn.execute(
                select(
                    node_states_table.c.token_id,
                    node_states_table.c.node_id,
                    nodes_table.c.node_type,
                    node_states_table.c.status,
                    node_states_table.c.input_hash,
                    node_states_table.c.output_hash,
                ).select_from(
                    node_states_table.join(
                        nodes_table,
                        (node_states_table.c.node_id == nodes_table.c.node_id) & (node_states_table.c.run_id == nodes_table.c.run_id),
                    )
                )
            ).all()
    finally:
        db.close()

    print("SNAPSHOT", shape, json.dumps(sorted(tuple(row) for row in recorded)))
    assert counts == {0: 1, 1: 1}
    gate_hashes = sorted(
        (input_hash, output_hash) for _token_id, _node_id, kind, _status, input_hash, output_hash in recorded if kind == NodeType.GATE.value
    )
    expected = sorted((stable_hash({"value": value}), stable_hash({"value": value})) for value in (0, 1))
    assert gate_hashes == expected


def test_gate_handled_error_uses_recorded_input_hash() -> None:
    factory = _make_factory()
    executor = GateExecutor(
        factory.execution, _make_span_factory(), make_test_step_resolver(), error_edge_ids={NodeID("cg_1"): "edge_error"}
    )
    config = GateSettings(
        name="gate", input="in", condition="row['missing'] > 0", routes={"true": "next", "false": "other"}, on_error="discard"
    )
    token = _make_token()

    outcome = executor.execute_config_gate(config, "cg_1", token, make_context(), attempt_offset=0)

    recorded_hash = factory.execution.begin_node_state.return_value.input_hash
    assert recorded_hash == stable_hash(token.row_data.to_dict())
    assert outcome.result.input_hash == recorded_hash
    assert outcome.result.output_hash is None
    assert outcome.discarded is True
    assert factory.execution.complete_node_state.call_args.kwargs["status"] == NodeStateStatus.FAILED


def test_gate_uncaught_error_records_failed_and_noncanonical_input_opens_no_state() -> None:
    factory = _make_factory()
    executor = GateExecutor(factory.execution, _make_span_factory(), make_test_step_resolver())
    config = GateSettings(name="gate", input="in", condition="row['missing'] > 0", routes={"true": "next", "false": "other"})

    with pytest.raises(ExpressionEvaluationError):
        executor.execute_config_gate(config, "cg_1", _make_token(), make_context(), attempt_offset=0)
    assert factory.execution.complete_node_state.call_args.kwargs["status"] == NodeStateStatus.FAILED

    bad_factory = _make_factory()
    bad_executor = GateExecutor(bad_factory.execution, _make_span_factory(), make_test_step_resolver())
    with pytest.raises(ValueError, match=r"non-finite|NaN"):
        bad_executor.execute_config_gate(config, "cg_1", _make_token({"value": float("nan")}), make_context(), attempt_offset=0)
    bad_factory.execution.complete_node_state.assert_not_called()


def test_gate_success_returns_recorded_input_hash_for_both_fields() -> None:
    factory = _make_factory()
    executor = GateExecutor(
        factory.execution,
        _make_span_factory(),
        make_test_step_resolver(),
        route_resolution_map={(NodeID("cg_1"), "true"): RouteDestination.discard()},
    )
    config = GateSettings(name="gate", input="in", condition="True", routes={"true": "discard", "false": "discard"})
    token = _make_token()

    outcome = executor.execute_config_gate(config, "cg_1", token, make_context(), attempt_offset=0)

    opened = factory.execution.begin_node_state.return_value
    assert opened.input_hash == stable_hash(token.row_data.to_dict())
    assert opened.state_id != opened.input_hash
    assert outcome.result.input_hash == opened.input_hash
    assert outcome.result.output_hash == opened.input_hash
