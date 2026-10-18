"""Real CSV effects retain caller identity and stop on required-row refusal."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from elspeth.contracts import NodeType, PendingOutcome, RoutingMode, TerminalOutcome, TerminalPath, TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.sink_effects import SinkEffectRole, SinkEffectState
from elspeth.core.landscape.row_data import RowDataState
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.sinks.csv_sink import CSVRowRejectedError, CSVSink
from tests.fixtures.base_classes import create_observed_contract, inject_write_failure
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    configured = make_recorder_with_run(leader_worker_id="worker-a")
    try:
        yield configured
    finally:
        configured.db.close()


def _tokens(setup: RecorderSetup, rows: list[dict[str, object]]) -> list[TokenInfo]:
    result = []
    for index, data in enumerate(rows):
        row, token = setup.data_flow.create_row_with_token(
            source_node_id=setup.source_node_id,
            row_index=index,
            source_row_index=index,
            ingest_sequence=index,
            data=data,
            coordination_token=setup.coordination_token,
        )
        result.append(TokenInfo(row_id=row.row_id, token_id=token.token_id, row_data=PipelineRow(data, create_observed_contract(data))))
    return result


def _sink(
    setup: RecorderSetup,
    path: Path,
    *,
    name: str = "arbitrary_archive",
    encoding: str = "utf-8",
    require_all_rows: bool | None = True,
    route: str = "discard",
) -> CSVSink:
    options: dict[str, object] = {
        "path": str(path),
        "schema": {"mode": "observed"},
        "collision_policy": "fail_if_exists",
        "encoding": encoding,
    }
    if require_all_rows is not None:
        options["require_all_rows"] = require_all_rows
    sink = inject_write_failure(CSVSink(options), route)
    sink.node_id = register_test_node(setup.data_flow, setup.run_id, name, node_type=NodeType.SINK, plugin_name="csv")
    return sink


def _executor(setup: RecorderSetup) -> SinkExecutor:
    return SinkExecutor(
        setup.execution,
        setup.data_flow,
        SpanFactory(),
        setup.run_id,
        factory=setup.factory,
        worker_id="worker-a",
        coordination_token=setup.coordination_token,
    )


def _context(setup: RecorderSetup, sink: CSVSink) -> PluginContext:
    return PluginContext(run_id=setup.run_id, config={}, landscape=setup.factory.plugin_audit_writer(), node_id=sink.node_id)


def _assert_source_identity(setup: RecorderSetup, tokens: list[TokenInfo], rows: list[dict[str, object]]) -> None:
    assert len({token.row_id for token in tokens}) == len(rows)
    for index, (token, data) in enumerate(zip(tokens, rows, strict=True)):
        stored = setup.query.get_row(token.row_id)
        assert stored is not None
        assert stored.source_node_id == setup.source_node_id
        assert stored.source_row_index == index
        assert stored.ingest_sequence == index
        stored_token = setup.query.get_token(token.token_id)
        assert stored_token is not None
        assert stored_token.row_id == token.row_id
        payload = setup.query.get_row_data(token.row_id)
        assert payload.state is RowDataState.AVAILABLE
        assert payload.data == data


_ORIGIN_ERROR = compute_error_hash("synthetic origin refusal")
_PENDING = [
    PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.ON_ERROR_ROUTED, error_hash=_ORIGIN_ERROR),
    PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.QUARANTINED_AT_SOURCE, error_hash=_ORIGIN_ERROR),
    PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.GATE_ROUTED),
]


@pytest.mark.parametrize("pending", _PENDING, ids=["transform-error", "source-quarantine", "ordinary-gate-route"])
@pytest.mark.parametrize("refuse", [False, True], ids=["retained", "refused"])
def test_required_csv_policy_at_real_primary_caller(setup: RecorderSetup, tmp_path: Path, pending: PendingOutcome, refuse: bool) -> None:
    target = tmp_path / "retention.csv"
    rows: list[dict[str, object]] = [{"business_id": "same", "value": 1}, {"business_id": "same", "value": 2}]
    if refuse:
        rows[1]["extra"] = True
    tokens = _tokens(setup, rows)
    sink = _sink(setup, target)

    if refuse:
        # A same-owner retry must reuse the reservation and still refuse, rather
        # than publishing or fabricating a terminal discard to advance recovery.
        for _ in range(2):
            with pytest.raises(CSVRowRejectedError, match="require_all_rows"):
                _executor(setup).write(
                    sink,
                    tokens,
                    _context(setup, sink),
                    1,
                    sink_name="arbitrary_archive",
                    pending_outcome=pending,
                    effect_mode="write",
                    join_group_id_by_token={token.token_id: None for token in tokens},
                )
            assert list(tmp_path.iterdir()) == []
            effects = setup.execution.sink_effects.get_effects_for_run(setup.run_id)
            assert len(effects) == 1
            effect = effects[0]
            assert effect.role is SinkEffectRole.PRIMARY
            assert effect.state is SinkEffectState.RESERVED
            assert effect.plan_json is None
            assert effect.plan_hash is None
            assert effect.prepared_at is None
            assert effect.finalized_at is None
            assert effect.publication_performed is not True
            members = setup.execution.sink_effects.get_members(effect.effect_id)
            assert [(member.token_id, member.row_id, member.ingest_sequence) for member in members] == [
                (token.token_id, token.row_id, index) for index, token in enumerate(tokens)
            ]
            assert all(member.prepared_disposition is None and member.member_state is None for member in members)
            assert all(setup.data_flow.get_token_outcome(token.token_id) is None for token in tokens)
    else:
        _, counts = _executor(setup).write(
            sink,
            tokens,
            _context(setup, sink),
            1,
            sink_name="arbitrary_archive",
            pending_outcome=pending,
            effect_mode="write",
            join_group_id_by_token={token.token_id: None for token in tokens},
        )
        assert counts.total == 0
        assert target.read_text() == "business_id,value\nsame,1\nsame,2\n"
        for token in tokens:
            outcome = setup.data_flow.get_token_outcome(token.token_id)
            assert outcome is not None
            assert (outcome.outcome, outcome.path, outcome.error_hash) == (pending.outcome, pending.path, pending.error_hash)
    _assert_source_identity(setup, tokens, rows)


@pytest.mark.parametrize("require_all_rows", [None, False])
def test_real_primary_default_discard_is_unchanged(setup: RecorderSetup, tmp_path: Path, require_all_rows: bool | None) -> None:
    rows: list[dict[str, object]] = [{"value": 1}, {"value": 2, "extra": True}]
    tokens = _tokens(setup, rows)
    target = tmp_path / "ordinary.csv"
    sink = _sink(setup, target, require_all_rows=require_all_rows)
    _, counts = _executor(setup).write(
        sink,
        tokens,
        _context(setup, sink),
        1,
        sink_name="arbitrary_archive",
        pending_outcome=PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW),
        effect_mode="write",
        join_group_id_by_token={token.token_id: None for token in tokens},
    )
    assert counts.discard_mode == 1
    outcome = setup.data_flow.get_token_outcome(tokens[1].token_id)
    assert outcome is not None
    assert outcome.path is TerminalPath.SINK_DISCARDED
    assert target.read_text() == "value\n1\n"


@pytest.mark.parametrize("retention_encoding", ["ascii", "utf-8"], ids=["refused", "retained"])
def test_real_csv_linked_retention_keeps_partial_publication_and_source_identity(
    setup: RecorderSetup, tmp_path: Path, retention_encoding: str
) -> None:
    rows: list[dict[str, object]] = [{"business_id": "same", "label": "ok"}, {"business_id": "same", "label": "\u00e9"}]
    tokens = _tokens(setup, rows)
    primary_target = tmp_path / "primary.csv"
    retention_target = tmp_path / "retention.csv"
    primary = _sink(setup, primary_target, name="primary", encoding="ascii", require_all_rows=None, route="arbitrary_archive")
    retention = _sink(setup, retention_target, encoding=retention_encoding)
    edge = setup.data_flow.register_edge(
        primary.node_id, retention.node_id, "__failsink__", RoutingMode.DIVERT, coordination_token=setup.coordination_token
    )

    def write() -> None:
        _executor(setup).write(
            primary,
            tokens,
            _context(setup, primary),
            1,
            sink_name="primary",
            pending_outcome=PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW),
            effect_mode="write",
            failsink=retention,
            failsink_name="arbitrary_archive",
            failsink_effect_mode="write",
            failsink_edge_id=edge.edge_id,
            join_group_id_by_token={token.token_id: None for token in tokens},
        )

    if retention_encoding == "ascii":
        with pytest.raises(CSVRowRejectedError, match="require_all_rows"):
            write()
        assert not retention_target.exists()
        # The successfully published primary keeps its normal target lock.
        assert set(tmp_path.iterdir()) == {primary_target, tmp_path / ".primary.csv.elspeth.lock"}
        assert setup.data_flow.get_token_outcome(tokens[1].token_id) is None
    else:
        write()
        outcome = setup.data_flow.get_token_outcome(tokens[1].token_id)
        assert outcome is not None
        assert outcome.path is TerminalPath.SINK_FALLBACK_TO_FAILSINK
        assert "\u00e9" in retention_target.read_text()

    assert primary_target.read_text() == "business_id,label\nsame,ok\n"
    effects = setup.execution.sink_effects.get_effects_for_run(setup.run_id)
    assert len(effects) == 2
    primary_effect = next(effect for effect in effects if effect.role is SinkEffectRole.PRIMARY)
    linked_effect = next(effect for effect in effects if effect.role is SinkEffectRole.FAILSINK)
    assert primary_effect.state is SinkEffectState.FINALIZED
    assert linked_effect.primary_effect_id == primary_effect.effect_id
    assert linked_effect.state is (SinkEffectState.RESERVED if retention_encoding == "ascii" else SinkEffectState.FINALIZED)
    members = setup.execution.sink_effects.get_members(linked_effect.effect_id)
    assert len(members) == 1
    assert (members[0].token_id, members[0].row_id, members[0].ingest_sequence) == (tokens[1].token_id, tokens[1].row_id, 1)
    assert members[0].primary_effect_id == primary_effect.effect_id
    _assert_source_identity(setup, tokens, rows)
