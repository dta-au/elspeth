"""Audited source admission rejects incomplete and divergent retained evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.audit import ValidationErrorRecord
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import RunMode, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.canonical import stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_dumps
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from elspeth.engine.orchestrator.source_replay import prepare_audited_sources, prepare_verified_sources
from tests.unit.engine.orchestrator.test_source_replay import _failed_source_state, _source_audit


@pytest.mark.parametrize(
    "corruption,reason",
    [
        ("declarations", "source declarations differ"),
        ("duplicate-name", "source declarations differ"),
        ("incomplete", "was not exhausted"),
        ("missing-node", "node is missing"),
        ("plugin", "plugin identity differs"),
        ("config-shape", "audited config is corrupt"),
        ("config-hash", "audited config is corrupt"),
        ("missing-name", "audited name is missing"),
        ("name", "implementation or configuration differs"),
        ("config", "implementation or configuration differs"),
        ("version", "implementation or configuration differs"),
        ("implementation", "implementation or configuration differs"),
        ("missing-schema", "has no stored schema"),
        ("schema-json", "malformed schema JSON"),
        ("schema-shape", "schema is not an object"),
        ("row-source", "undeclared source node"),
        ("missing-contract", "source contract missing"),
        ("contract-json", "malformed source contract"),
        ("contract-shape", "malformed source contract"),
        ("restored-hash", "restored payload hash mismatch"),
    ],
)
def test_admission_refuses_corrupt_source_identity_and_rows(corruption: str, reason: str) -> None:
    factory, source, row = _source_audit()
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records.return_value
    record = lifecycle["source-old"]
    node = factory.data_flow.get_nodes.return_value[0]
    if corruption == "declarations":
        record.source_name = "other"
    elif corruption == "duplicate-name":
        lifecycle["duplicate"] = copy.copy(record)
    elif corruption == "incomplete":
        record.lifecycle_state = "loading"
    elif corruption == "missing-node":
        factory.data_flow.get_nodes.return_value = []
    elif corruption == "plugin":
        node.plugin_name = "other"
    elif corruption == "config-shape":
        node.config_json = "[]"
    elif corruption == "config-hash":
        node.config_hash = "f" * 64
    elif corruption == "missing-name":
        node.config_json = json.dumps(source.config)
        node.config_hash = stable_hash(source.config)
    elif corruption == "name":
        config = {**source.config, "source_name": "other"}
        node.config_json = json.dumps(config)
        node.config_hash = stable_hash(config)
    elif corruption == "config":
        source.config = {"path": "different.csv"}
    elif corruption == "version":
        source.plugin_version = "2"
    elif corruption == "implementation":
        source.source_file_hash = "f" * 64
    elif corruption == "missing-schema":
        record.source_schema_json = None
    elif corruption == "schema-json":
        record.source_schema_json = "{"
    elif corruption == "schema-shape":
        record.source_schema_json = "[]"
    elif corruption == "row-source":
        row.source_node_id = "undeclared"
    elif corruption == "missing-contract":
        row.source_contract_json = None
    elif corruption == "contract-json":
        row.source_contract_json = "{"
    elif corruption == "contract-shape":
        row.source_contract_json = checkpoint_dumps([])
    else:
        assert corruption == "restored-hash"
        row.source_data_hash = "f" * 64
    retained_config = node.config_json
    retained_row = copy.copy(row)

    with pytest.raises(AuditIntegrityError, match=reason):
        prepare_audited_sources(factory, "previous-run", {"primary": source})

    source.load.assert_not_called()
    assert node.config_json == retained_config
    assert row == retained_row
    assert factory.execution.mock_calls == []
    assert factory.payload_store.mock_calls == []


@pytest.mark.parametrize("corruption", ["unknown-order", "reordered"])
def test_multisource_admission_preserves_the_audited_source_order(corruption: str) -> None:
    factory, source, _row = _source_audit()
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records.return_value
    secondary_record = copy.copy(lifecycle["source-old"])
    secondary_record.source_name = "secondary"
    secondary_record.source_node_id = "source-second"
    lifecycle["source-second"] = secondary_record
    secondary_node = copy.copy(factory.data_flow.get_nodes.return_value[0])
    secondary_node.node_id = "source-second"
    secondary_config = {**source.config, "source_name": "secondary"}
    secondary_node.config_json = json.dumps(secondary_config)
    secondary_node.config_hash = stable_hash(secondary_config)
    secondary_node.sequence_in_pipeline = 1
    factory.data_flow.get_nodes.return_value.append(secondary_node)
    secondary_source = copy.copy(source)
    if corruption == "unknown-order":
        secondary_node.sequence_in_pipeline = None
        sources = {"primary": source, "secondary": secondary_source}
        reason = "source order cannot be established"
    else:
        sources = {"secondary": secondary_source, "primary": source}
        reason = "source order differs"

    with pytest.raises(AuditIntegrityError, match=reason):
        prepare_audited_sources(factory, "previous-run", sources)

    source.load.assert_not_called()
    factory.query.iter_rows_for_run.assert_not_called()
    assert factory.execution.mock_calls == []


def _quarantine_audit(*, original: object = "bad") -> tuple[MagicMock, SimpleNamespace, SimpleNamespace]:
    payload = original if type(original) is dict else {"_raw": original}
    factory, source, row = _source_audit(payload=payload)
    # Reuse the fixture's token-shaped record; do not introduce structural
    # stand-ins for the production failure record, which is nominally checked.
    token = copy.copy(row)
    token.token_id = "source-token"
    factory.query.get_tokens.return_value = [token]
    factory.query.get_node_states_for_token.return_value = [_failed_source_state()]
    outcome = copy.copy(row)
    outcome.path = TerminalPath.QUARANTINED_AT_SOURCE
    outcome.token_id = "source-token"
    outcome.sink_name = "quarantine"
    factory.data_flow.get_token_outcomes_for_row.return_value = [outcome]
    factory.data_flow.get_validation_errors_for_run.return_value = [
        ValidationErrorRecord(
            error_id="error-1",
            run_id="previous-run",
            node_id="source-old",
            row_id="row-1",
            row_hash=stable_hash(original),
            row_data_json=json.dumps(original),
            error="bad value",
            schema_mode="fixed",
            destination="quarantine",
            created_at=datetime.now(UTC),
        )
    ]
    return factory, source, row


def test_generic_source_preserves_mapping_quarantine_without_validation_ledger() -> None:
    """SourceRow permits a source's own quarantine decision without a ledger ID."""
    original = {"value": "bad"}
    factory, source, _row = _quarantine_audit(original=original)
    factory.data_flow.get_validation_errors_for_run.return_value = []

    audited = prepare_audited_sources(factory, "previous-run", {"primary": source})["primary"]

    assert audited.rows[0].row == original
    assert audited.rows[0].is_quarantined
    assert audited.rows[0].validation_error_id is None
    assert audited.validation_errors == ()
    source.load.assert_not_called()


@pytest.mark.parametrize(
    "corruption,reason",
    [
        ("duplicate-failure", "source validation error evidence missing"),
        ("missing-error", "source validation error evidence missing"),
        ("duplicate-outcome", "ambiguous quarantine outcomes"),
        ("missing-sink", "quarantine destination missing"),
        ("envelope", "malformed source validation error evidence"),
        ("missing-exception", "malformed source validation error evidence"),
        ("exception-type", "malformed source validation error evidence"),
        ("exception-empty", "malformed source validation error evidence"),
        ("row-hash", "quarantine payload hash mismatch"),
        ("missing-validation", "ambiguous _raw quarantine payload"),
        ("duplicate-validation", "ambiguous quarantine validation evidence"),
        ("missing-validation-payload", "payload or linkage is invalid"),
        ("divergent-validation", "quarantine payload disagrees with validation evidence"),
    ],
)
def test_quarantine_admission_requires_one_complete_original_decision(corruption: str, reason: str) -> None:
    factory, source, row = _quarantine_audit()
    failure = factory.query.get_node_states_for_token.return_value[0]
    outcome = factory.data_flow.get_token_outcomes_for_row.return_value[0]
    error = factory.data_flow.get_validation_errors_for_run.return_value[0]
    if corruption == "duplicate-failure":
        factory.query.get_node_states_for_token.return_value.append(replace(failure, state_id="state-2"))
    elif corruption == "missing-error":
        factory.query.get_node_states_for_token.return_value = [replace(failure, error_json=None)]
    elif corruption == "duplicate-outcome":
        factory.data_flow.get_token_outcomes_for_row.return_value.append(copy.copy(outcome))
    elif corruption == "missing-sink":
        outcome.sink_name = None
    elif corruption in {"envelope", "missing-exception", "exception-type", "exception-empty"}:
        envelopes = {
            "envelope": "[]",
            "missing-exception": "{}",
            "exception-type": '{"exception":7}',
            "exception-empty": '{"exception":""}',
        }
        factory.query.get_node_states_for_token.return_value = [replace(failure, error_json=envelopes[corruption])]
    elif corruption == "row-hash":
        row.source_data_hash = "f" * 64
    elif corruption == "missing-validation":
        factory.data_flow.get_validation_errors_for_run.return_value = []
    elif corruption == "duplicate-validation":
        factory.data_flow.get_validation_errors_for_run.return_value.append(replace(error, error_id="error-2"))
    elif corruption == "missing-validation-payload":
        factory.data_flow.get_validation_errors_for_run.return_value = [replace(error, row_data_json=None)]
    else:
        assert corruption == "divergent-validation"
        factory.data_flow.get_validation_errors_for_run.return_value = [
            replace(error, row_data_json='"different"', row_hash=stable_hash("different"))
        ]
    retained_payload = dict(factory.query.get_row_data.return_value.data)

    with pytest.raises(AuditIntegrityError, match=reason):
        prepare_audited_sources(factory, "previous-run", {"primary": source})

    source.load.assert_not_called()
    assert factory.query.get_row_data.return_value.data == retained_payload
    assert factory.execution.mock_calls == []
    assert factory.payload_store.mock_calls == []


@pytest.mark.parametrize("original", ["bad", {"_raw": "bad"}], ids=["primitive", "single-key-dictionary"])
def test_quarantine_reconstruction_distinguishes_identical_stored_payloads(original: object) -> None:
    factory, source, _row = _quarantine_audit()
    error = factory.data_flow.get_validation_errors_for_run.return_value[0]
    factory.data_flow.get_validation_errors_for_run.return_value = [
        replace(error, row_data_json=json.dumps(original), row_hash=stable_hash(original))
    ]

    audited = prepare_audited_sources(factory, "previous-run", {"primary": source})["primary"]

    assert audited.rows[0].row == original
    assert type(audited.rows[0].row) is type(original)
    assert audited.rows[0].source_row_index == 5
    assert audited.rows[0].quarantine_error == "bad value"
    assert audited.rows[0].quarantine_destination == "quarantine"
    assert audited.rows[0].validation_error_id == error.error_id
    assert audited.validation_errors == tuple(factory.data_flow.get_validation_errors_for_run.return_value)
    source.load.assert_not_called()


@pytest.mark.parametrize("corruption", ["token-run", "context-run", "mode", "declarations", "snapshot-type", "node"])
def test_verify_admission_refuses_identity_drift_before_loading(corruption: str) -> None:
    factory, source, _row = _source_audit()
    audited = prepare_audited_sources(factory, "previous-run", {"primary": source})
    source.node_id = "source-new"
    session = MagicMock(spec=AuditedCallModeSession)
    session.mode = RunMode.VERIFY
    ctx = PluginContext(run_id="verify-run", config={}, run_mode=RunMode.VERIFY, replay_from="previous-run", call_mode_session=session)
    ctx.node_id = "saved-node"
    ctx.operation_id = "saved-operation"
    token = CoordinationToken(run_id="verify-run", worker_id="worker:verify-run:test", leader_epoch=1)
    expected = OrchestrationInvariantError
    if corruption == "token-run":
        token = replace(token, run_id="other-run")
    elif corruption == "context-run":
        ctx.run_id = "other-run"
    elif corruption == "mode":
        ctx.run_mode = RunMode.LIVE
    elif corruption == "declarations":
        audited = {}
        expected = AuditIntegrityError
    elif corruption == "snapshot-type":
        audited = {"primary": object()}
    else:
        assert corruption == "node"
        source.node_id = None

    with pytest.raises(expected):
        prepare_verified_sources(factory, "verify-run", audited, {"primary": source}, ctx, token)

    source.load.assert_not_called()
    assert ctx.node_id == "saved-node"
    assert ctx.operation_id == "saved-operation"
    assert factory.execution.mock_calls == []
