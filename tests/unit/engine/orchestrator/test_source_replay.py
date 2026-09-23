"""Source replay admission preserves audited inputs before plugin startup."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from elspeth.contracts import PluginSchema, SourceRow
from elspeth.contracts.audit import NodeStateFailed
from elspeth.contracts.enums import NodeStateStatus, NodeType, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import RowDataResult, RowDataState
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.orchestrator.source_replay import prepare_audited_sources


class _ReplaySchema(PluginSchema):
    value: int


def _source_audit(*, payload: dict[str, object] | None = None) -> tuple[MagicMock, SimpleNamespace, SimpleNamespace]:
    payload = {"value": 7} if payload is None else payload
    factory = MagicMock(spec=RecorderFactory)
    source = SimpleNamespace(
        name="rows",
        config={"path": "input.csv"},
        plugin_version="1",
        source_file_hash=None,
        output_schema=_ReplaySchema,
        load=MagicMock(side_effect=AssertionError("source.load must not run during admission")),
    )
    factory.run_lifecycle.get_run_source_lifecycle_records.return_value = {
        "source-old": SimpleNamespace(
            source_name="primary",
            source_node_id="source-old",
            lifecycle_state="exhausted",
            source_schema_json=json.dumps(_ReplaySchema.model_json_schema()),
            normalization_version=None,
        )
    }
    factory.run_lifecycle.get_source_field_resolutions.return_value = {}
    factory.data_flow.get_nodes.return_value = [
        SimpleNamespace(
            node_id="source-old",
            node_type=NodeType.SOURCE,
            plugin_name="rows",
            config_hash=stable_hash({**source.config, "source_name": "primary"}),
            config_json=json.dumps({**source.config, "source_name": "primary"}),
            plugin_version="1",
            source_file_hash=None,
            sequence_in_pipeline=0,
        )
    ]
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=False)
    factory.data_flow.get_node_contracts.return_value = (None, contract)
    row = SimpleNamespace(
        row_id="row-1",
        source_node_id="source-old",
        source_row_index=5,
        source_data_hash=stable_hash(payload),
    )
    factory.query.iter_rows_for_run.return_value = [[row]]
    factory.query.get_row_data.return_value = RowDataResult(state=RowDataState.AVAILABLE, data=payload)
    factory.query.get_tokens.return_value = []
    factory.data_flow.get_token_outcomes_for_row.return_value = []
    return factory, source, row


def test_replay_reconstructs_ordered_row_without_loading_source() -> None:
    factory, source, _row = _source_audit()

    plan = prepare_audited_sources(factory, "previous-run", {"primary": source})

    assert len(plan["primary"].rows) == 1
    assert isinstance(plan["primary"].rows[0], SourceRow)
    assert plan["primary"].rows[0].row == {"value": 7}
    assert plan["primary"].rows[0].source_row_index == 5
    source.load.assert_not_called()


def test_replay_refuses_missing_payload_before_loading_source() -> None:
    factory, source, _row = _source_audit()
    factory.query.get_row_data.return_value = RowDataResult(state=RowDataState.PURGED, data=None)

    with pytest.raises(AuditIntegrityError, match="payload is purged"):
        prepare_audited_sources(factory, "previous-run", {"primary": source})

    source.load.assert_not_called()


def _failed_source_state() -> NodeStateFailed:
    now = datetime.now(UTC)
    return NodeStateFailed(
        state_id="state-1",
        token_id="source-token",
        node_id="source-old",
        step_index=0,
        attempt=0,
        status=NodeStateStatus.FAILED,
        input_hash="a" * 64,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        error_json='{"exception":"bad value"}',
    )


def test_replay_reconstructs_quarantined_row() -> None:
    factory, source, row = _source_audit(payload={"_raw": "bad"})
    row.source_data_hash = stable_hash({"_raw": "bad"})
    factory.query.get_tokens.return_value = [SimpleNamespace(token_id="source-token")]
    factory.query.get_node_states_for_token.return_value = [_failed_source_state()]
    factory.data_flow.get_token_outcomes_for_row.return_value = [
        SimpleNamespace(path=TerminalPath.QUARANTINED_AT_SOURCE, token_id="source-token", sink_name="quarantine")
    ]
    factory.data_flow.get_validation_errors_for_row.return_value = [SimpleNamespace(row_data_json='"bad"')]

    plan = prepare_audited_sources(factory, "previous-run", {"primary": source})

    item = plan["primary"].rows[0]
    assert item.is_quarantined
    assert item.row == "bad"
    assert item.quarantine_error == "bad value"
    assert item.quarantine_destination == "quarantine"
    source.load.assert_not_called()


def test_replay_refuses_failed_source_state_without_quarantine_outcome() -> None:
    factory, source, _row = _source_audit()
    factory.query.get_tokens.return_value = [SimpleNamespace(token_id="source-token")]
    factory.query.get_node_states_for_token.return_value = [_failed_source_state()]
    factory.data_flow.get_token_outcomes_for_row.return_value = [SimpleNamespace(path=TerminalPath.DEFAULT_FLOW)]

    with pytest.raises(AuditIntegrityError, match="no quarantine outcome"):
        prepare_audited_sources(factory, "previous-run", {"primary": source})


def test_verify_detects_source_row_drift_before_yielding_changed_row() -> None:
    factory, source, _row = _source_audit()
    audited = prepare_audited_sources(factory, "previous-run", {"primary": source})["primary"]
    assert audited.schema_contract is not None
    source.load = MagicMock(return_value=iter([SourceRow.valid({"value": 9}, contract=audited.schema_contract, source_row_index=5)]))
    ctx = PluginContext(run_id="verify-run", config={})

    with pytest.raises(AuditIntegrityError, match="differs from audited run"):
        list(SourceIterationDriver._verify_source_rows(source, ctx, audited))

    source.load.assert_called_once_with(ctx)
