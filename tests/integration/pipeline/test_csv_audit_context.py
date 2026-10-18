"""Physical CSV retention context uses real effects without changing raw rows."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import delete, update

from elspeth.contracts import NodeType, PendingOutcome, RoutingMode, TerminalOutcome, TerminalPath
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import OrchestrationInvariantError, SinkEffectCapabilityError, VerificationMismatchError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.sink_effects import (
    SinkEffectExecutionPurpose,
    SinkEffectMemberCandidate,
    SinkEffectRole,
    SinkEffectRoutingContext,
)
from elspeth.core.canonical import canonical_json as pipeline_canonical_json
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_identity import resolve_sink_effect_members
from elspeth.core.landscape.schema import token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.replay_sink_effect import verify_virtual_sink_members
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSink
from tests.fixtures.base_classes import inject_write_failure
from tests.fixtures.landscape import RecorderSetup, make_factory, make_recorder_with_run, register_test_node
from tests.integration.pipeline.test_csv_required_row_retention import _context, _executor, _sink, _tokens


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[RecorderSetup]:
    configured = make_recorder_with_run(
        leader_worker_id="worker-a",
        db=LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}"),
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
    )
    try:
        yield configured
    finally:
        configured.db.close()


def _retention_sink(configured: RecorderSetup, target: Path) -> CSVSink:
    sink = inject_write_failure(
        CSVSink(
            {
                "path": str(target),
                "schema": {"mode": "observed"},
                "collision_policy": "auto_increment",
                "require_all_rows": True,
                "include_audit_context": True,
            }
        ),
        "discard",
    )
    sink.node_id = register_test_node(configured.data_flow, configured.run_id, "archive", node_type=NodeType.SINK, plugin_name="csv")
    return sink


@pytest.mark.parametrize(
    "pending",
    [
        PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.ON_ERROR_ROUTED, error_hash="a" * 16),
        PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.QUARANTINED_AT_SOURCE, error_hash="b" * 16),
        PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.GATE_ROUTED),
    ],
    ids=["transform-error", "source-quarantine", "business-gate"],
)
def test_csv_audit_context_retains_heterogeneous_rows_and_exact_provenance(setup: RecorderSetup, tmp_path: Path, pending: PendingOutcome):
    rows = [
        {"business_id": "same", "run_id": "untrusted-value", "path": "business-path", "value": {"nested": [1, True, None, 1.5]}},
        {"business_id": "same", "different": 'quotes,"\n\u00e9', "business_reason": "A declared business check failed."},
    ]
    tokens = _tokens(setup, rows)
    target = tmp_path / "archive.csv"
    sink = _retention_sink(setup, target)
    _, counts = _executor(setup).write(
        sink,
        tokens,
        _context(setup, sink),
        1,
        sink_name="archive",
        pending_outcome=pending,
        effect_mode="write",
        join_group_id_by_token={token.token_id: None for token in tokens},
    )

    assert counts.total == 0
    with target.open(newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 2
    effects = setup.execution.sink_effects.get_effects_for_run(setup.run_id)
    assert len(effects) == 1
    members = setup.execution.sink_effects.get_members(effects[0].effect_id)
    for index, (record, token, row, member) in enumerate(zip(records, tokens, rows, members, strict=True)):
        assert list(record) == [
            "run_id",
            "row_id",
            "token_id",
            "ingest_sequence",
            "outcome",
            "path",
            "error_hash",
            "primary_effect_id",
            "row_json",
        ]
        assert record["run_id"] == setup.run_id
        assert record["row_id"] == token.row_id
        assert record["token_id"] == token.token_id
        assert record["ingest_sequence"] == str(index)
        assert json.loads(record["row_json"]) == row
        assert record["primary_effect_id"] == ""
        outcome = setup.data_flow.get_token_outcome(token.token_id)
        assert outcome is not None and outcome.completed
        assert record["outcome"] == outcome.outcome.value == pending.outcome.value
        assert record["path"] == outcome.path.value == pending.path.value
        assert record["error_hash"] == (outcome.error_hash or "") == (pending.error_hash or "")
        assert member.payload_hash == sha256(pipeline_canonical_json(row).encode()).hexdigest()
        assert all(state.input_hash == member.payload_hash for state in setup.query.get_node_states_for_token(token.token_id))
    assert len({record["row_id"] for record in records}) == 2


@pytest.mark.parametrize("resume", [False, True], ids=["write", "resume-append"])
@pytest.mark.parametrize("corruption", [None, "missing", "changed_reference"], ids=["restored", "missing-context", "altered-context"])
def test_cumulative_csv_context_is_recovered_and_bound_after_factory_restart(
    setup: RecorderSetup, tmp_path: Path, corruption: str | None, resume: bool
):
    rows = [{"business_id": "same", "value": 1}, {"business_id": "same", "value": 2}]
    tokens = _tokens(setup, rows)
    target = tmp_path / "archive.csv"
    sink = _retention_sink(setup, target)
    _executor(setup).write(
        sink,
        tokens[:1],
        _context(setup, sink),
        1,
        sink_name="archive",
        pending_outcome=PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.ON_ERROR_ROUTED, error_hash="a" * 16),
        effect_mode="write",
        join_group_id_by_token={tokens[0].token_id: None},
    )
    original_bytes = target.read_bytes()
    if corruption is not None:
        with setup.db.engine.begin() as connection:
            predicate = (token_outcomes_table.c.run_id == setup.run_id) & (token_outcomes_table.c.token_id == tokens[0].token_id)
            if corruption == "missing":
                connection.execute(delete(token_outcomes_table).where(predicate))
            else:
                connection.execute(update(token_outcomes_table).where(predicate).values(error_hash="f" * 16))

    recovered = RecorderSetup(
        db=setup.db,
        factory=make_factory(setup.db, payload_store=FilesystemPayloadStore(tmp_path / "payloads")),
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        coordination_token=setup.coordination_token,
    )
    fresh_sink = inject_write_failure(CSVSink(dict(sink.config)), "discard")
    fresh_sink.node_id = sink.node_id
    effect_mode = "write"
    if resume:
        resolved = CSVSink._resolve_sink_effect_mode(dict(fresh_sink.config), purpose=SinkEffectExecutionPurpose.RESUME)
        assert resolved is not None and resolved.value == "append"
        fresh_sink.configure_for_resume()
        effect_mode = resolved.value
    executor = SinkExecutor(
        recovered.execution,
        recovered.data_flow,
        SpanFactory(),
        recovered.run_id,
        factory=recovered.factory,
        worker_id="worker-b",
        coordination_token=recovered.coordination_token,
    )

    def write_successor():
        return executor.write(
            fresh_sink,
            tokens[1:],
            _context(recovered, fresh_sink),
            1,
            sink_name="archive",
            pending_outcome=PendingOutcome(outcome=TerminalOutcome.FAILURE, path=TerminalPath.QUARANTINED_AT_SOURCE, error_hash="b" * 16),
            effect_mode=effect_mode,
            join_group_id_by_token={tokens[1].token_id: None},
        )

    if corruption is not None:
        with pytest.raises(LandscapeRecordError, match="routing context"):
            write_successor()
        assert target.read_bytes() == original_bytes
        assert recovered.data_flow.get_token_outcome(tokens[1].token_id) is None
        return

    write_successor()
    with target.open(newline="") as stream:
        records = list(csv.DictReader(stream))
    assert [record["token_id"] for record in records] == [token.token_id for token in tokens]
    assert [record["error_hash"] for record in records] == ["a" * 16, "b" * 16]
    assert [record["path"] for record in records] == ["on_error_routed", "quarantined_at_source"]
    assert [json.loads(record["row_json"]) for record in records] == rows
    effects = recovered.execution.sink_effects.get_effects_for_run(recovered.run_id)
    assert sorted(effect.stream_sequence for effect in effects) == [0, 1]

    if resume:
        # Successor inspection selects the actual predecessor target before
        # append preparation validates it; construction may select a free sibling.
        expected_headers = (
            "run_id",
            "row_id",
            "token_id",
            "ingest_sequence",
            "outcome",
            "path",
            "error_hash",
            "primary_effect_id",
            "row_json",
        )
        validation = fresh_sink.validate_output_target()
        assert validation.valid
        assert validation.target_fields == expected_headers
        published_bytes = target.read_bytes()
        with target.open(newline="") as stream:
            csv_rows = list(csv.reader(stream))
        assert tuple(csv_rows[0]) == expected_headers
        malformed_headers = (
            expected_headers[:-1],
            (expected_headers[1], expected_headers[0], *expected_headers[2:]),
        )
        try:
            for headers in malformed_headers:
                with target.open("w", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(csv_rows[1:])
                malformed_bytes = target.read_bytes()
                invalid = fresh_sink.validate_output_target()
                assert not invalid.valid
                assert invalid.error_message == "CSV audit-context envelope headers do not match"
                assert invalid.target_fields == headers
                assert invalid.schema_fields == expected_headers
                assert target.read_bytes() == malformed_bytes
        finally:
            target.write_bytes(published_bytes)
        assert target.read_bytes() == published_bytes


def test_linked_csv_context_keeps_real_diversion_and_primary_links_across_batches(setup: RecorderSetup, tmp_path: Path):
    tokens = _tokens(setup, [{"business_id": "same", "label": "\u00e9"}, {"business_id": "same", "label": "\u00f6"}])
    primary = _sink(setup, tmp_path / "primary.csv", name="primary", encoding="ascii", require_all_rows=None, route="archive")
    target = tmp_path / "archive.csv"
    retention = _retention_sink(setup, target)
    edge = setup.data_flow.register_edge(
        primary.node_id, retention.node_id, "__failsink__", RoutingMode.DIVERT, coordination_token=setup.coordination_token
    )
    for token in tokens:
        _executor(setup).write(
            primary,
            [token],
            _context(setup, primary),
            1,
            sink_name="primary",
            pending_outcome=PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW),
            effect_mode="write",
            failsink=retention,
            failsink_name="archive",
            failsink_effect_mode="write",
            failsink_edge_id=edge.edge_id,
            join_group_id_by_token={token.token_id: None},
        )
    with target.open(newline="") as stream:
        records = list(csv.DictReader(stream))
    assert [record["token_id"] for record in records] == [token.token_id for token in tokens]
    assert len({record["primary_effect_id"] for record in records}) == 2
    effects = setup.execution.sink_effects.get_effects_for_run(setup.run_id)
    for record, token in zip(records, tokens, strict=True):
        outcome = setup.data_flow.get_token_outcome(token.token_id)
        assert outcome is not None and outcome.completed
        assert record["outcome"] == outcome.outcome.value == "transient"
        assert record["path"] == outcome.path.value == "sink_fallback_to_failsink"
        assert record["error_hash"] == outcome.error_hash
        primary_effect = next(effect for effect in effects if effect.effect_id == record["primary_effect_id"])
        assert primary_effect.role is SinkEffectRole.PRIMARY
        carried = json.loads(record["row_json"])
        assert carried["__diverted_from"] == "primary"
        assert carried["__diversion_reason"].startswith("effect-diversion:")
        members = setup.execution.sink_effects.get_members(primary_effect.effect_id)
        assert [(member.token_id, member.prepared_disposition) for member in members] == [(token.token_id, "diverted")]


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
@pytest.mark.parametrize("changed", [False, True], ids=["identical-row", "changed-row"])
def test_audit_context_csv_replay_compares_raw_rows_and_never_publishes(
    setup: RecorderSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: RunMode, changed: bool
):
    target = tmp_path / "archive.csv"
    source_sink = _retention_sink(setup, target)
    source_tokens = _tokens(setup, [{"value": 1}])
    pending = PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW)
    _executor(setup).write(
        source_sink,
        source_tokens,
        _context(setup, source_sink),
        1,
        sink_name="archive",
        pending_outcome=pending,
        effect_mode="write",
        join_group_id_by_token={source_tokens[0].token_id: None},
    )
    original = target.read_bytes()
    replay = make_recorder_with_run(
        db=setup.db, leader_worker_id="replay-worker", payload_store=FilesystemPayloadStore(tmp_path / "payloads")
    )
    sink = _retention_sink(replay, target)
    tokens = _tokens(replay, [{"value": 2 if changed else 1}])
    assert tokens[0].token_id != source_tokens[0].token_id
    assert tokens[0].row_id != source_tokens[0].row_id

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("CSV publication entered during virtual replay")

    for method in ("inspect_effect", "prepare_effect", "commit_effect", "reconcile_effect"):
        monkeypatch.setattr(CSVSink, method, forbidden)
    context = PluginContext(
        run_id=replay.run_id,
        config={},
        landscape=replay.factory.plugin_audit_writer(),
        node_id=sink.node_id,
        run_mode=mode,
        replay_from=setup.run_id,
        call_mode_session=AuditedCallModeSession(
            replay.factory,
            current_run_id=replay.run_id,
            source_run_id=setup.run_id,
            mode=mode,
            coordination_token=replay.coordination_token,
        ),
    )
    artifact, _ = _executor(replay).write(
        sink,
        tokens,
        context,
        1,
        sink_name="archive",
        pending_outcome=pending,
        effect_mode="write",
        join_group_id_by_token={tokens[0].token_id: None},
    )
    assert artifact is not None and artifact.publication_performed is False
    assert target.read_bytes() == original
    if changed:
        error = VerificationMismatchError if mode is RunMode.VERIFY else OrchestrationInvariantError
        with pytest.raises(error, match="sink output differs"):
            verify_virtual_sink_members(replay.factory, source_run_id=setup.run_id, current_run_id=replay.run_id, mode=mode)
    else:
        verify_virtual_sink_members(replay.factory, source_run_id=setup.run_id, current_run_id=replay.run_id, mode=mode)


@pytest.mark.parametrize(
    "patch",
    [
        {"require_all_rows": False},
        {"schema": {"mode": "fixed", "fields": ["value: int"]}},
        {"headers": "original"},
        {"headers": {"value": "Value"}},
        {"mode": "append"},
        {"include_audit_context": "true"},
        {"include_audit_context": 1},
    ],
)
def test_csv_audit_context_refuses_ambiguous_or_lossy_configuration(tmp_path: Path, patch: dict[str, object]):
    config = {
        "path": str(tmp_path / "archive.csv"),
        "schema": {"mode": "observed"},
        "require_all_rows": True,
        "include_audit_context": True,
    }
    with pytest.raises(PluginConfigError, match="include_audit_context"):
        CSVSink({**config, **patch})


def test_csv_audit_context_refuses_audit_export_at_the_real_purpose_resolver(tmp_path: Path) -> None:
    target = tmp_path / "archive.csv"
    config = {
        "path": str(target),
        "schema": {"mode": "observed"},
        "collision_policy": "fail_if_exists",
        "require_all_rows": True,
        "include_audit_context": True,
    }
    fresh = CSVSink._resolve_sink_effect_mode(config, purpose=SinkEffectExecutionPurpose.FRESH)
    assert fresh is not None and fresh.value == "write"
    export = CSVSink._resolve_sink_effect_mode({**config, "include_audit_context": False}, purpose=SinkEffectExecutionPurpose.AUDIT_EXPORT)
    assert export is not None and export.value == "write"
    with pytest.raises(SinkEffectCapabilityError, match="audit-context envelopes are for pipeline rows"):
        CSVSink._resolve_sink_effect_mode(config, purpose=SinkEffectExecutionPurpose.AUDIT_EXPORT)
    assert not target.exists()


@pytest.mark.parametrize("mismatch", ["pending-identity", "primary-link"])
def test_real_member_resolution_refuses_a_context_that_changes_the_bound_identity(setup: RecorderSetup, mismatch: str) -> None:
    row = {"value": 1}
    token = _tokens(setup, [row])[0]
    pending_identity = {"error_hash": "a" * 16, "outcome": "failure", "path": "on_error_routed"}
    context = SinkEffectRoutingContext(outcome=TerminalOutcome.FAILURE, path=TerminalPath.ON_ERROR_ROUTED, error_hash="a" * 16)
    valid = SinkEffectMemberCandidate(token_id=token.token_id, row=row, pending_identity=pending_identity, routing_context=context)
    (member,) = resolve_sink_effect_members(setup.factory, (valid,))
    assert member.routing_context is context
    assert member.token_id == token.token_id and member.row_id == token.row_id
    assert member.row == row
    assert member.payload_hash == sha256(pipeline_canonical_json(row).encode()).hexdigest()
    assert member.pending_identity_hash == sha256(pipeline_canonical_json(pending_identity).encode()).hexdigest()

    contradictory = SinkEffectMemberCandidate(
        token_id=token.token_id,
        row=row,
        pending_identity={**pending_identity, "error_hash": "b" * 16} if mismatch == "pending-identity" else pending_identity,
        primary_effect_id="c" * 64 if mismatch == "primary-link" else None,
        routing_context=context,
    )
    message = "exact pending identity hash" if mismatch == "pending-identity" else "member primary linkage"
    with pytest.raises(ValueError, match=message):
        resolve_sink_effect_members(setup.factory, (contradictory,))


@pytest.mark.parametrize(
    "outcome,error_hash,error,message",
    [
        ("failure", "a" * 16, TypeError, "exact terminal enums"),
        (TerminalOutcome.FAILURE, "g" * 16, ValueError, "lowercase hex16"),
        (TerminalOutcome.FAILURE, None, ValueError, "error reference does not match"),
    ],
    ids=["string-enum", "malformed-reference", "missing-failure-reference"],
)
def test_actual_routing_context_refuses_malformed_provenance_before_member_resolution(
    setup: RecorderSetup, outcome, error_hash, error, message: str
) -> None:
    row = {"value": 1}
    token = _tokens(setup, [row])[0]
    with pytest.raises(error, match=message):
        resolve_sink_effect_members(
            setup.factory,
            (
                SinkEffectMemberCandidate(
                    token_id=token.token_id,
                    row=row,
                    pending_identity={"error_hash": "a" * 16, "outcome": "failure", "path": "on_error_routed"},
                    routing_context=SinkEffectRoutingContext(outcome=outcome, path=TerminalPath.ON_ERROR_ROUTED, error_hash=error_hash),
                ),
            ),
        )


@pytest.mark.parametrize(
    "foreign",
    [
        {"outcome": "failure", "path": "on_error_routed", "error_hash": "a" * 16, "primary_effect_id": None},
        object(),
    ],
    ids=["matching-json-object", "foreign-object"],
)
def test_actual_member_candidate_refuses_foreign_routing_context(setup: RecorderSetup, foreign) -> None:
    row = {"value": 1}
    token = _tokens(setup, [row])[0]
    with pytest.raises(TypeError, match="routing_context must be exact SinkEffectRoutingContext"):
        SinkEffectMemberCandidate(
            token_id=token.token_id,
            row=row,
            pending_identity={"error_hash": "a" * 16, "outcome": "failure", "path": "on_error_routed"},
            routing_context=foreign,
        )
