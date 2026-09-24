"""Replay sink effects retain audit provenance without publishing a sink."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.contracts import PendingOutcome, RunMode, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError, VerificationMismatchError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.sink_effects import SinkEffectAttemptAction, SinkEffectState
from elspeth.engine.executors.replay_sink_effect import VirtualReplaySinkEffect, verify_virtual_sink_members
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.infrastructure.base import BaseSink
from elspeth.plugins.sinks.database_sink import DatabaseSink
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db
from tests.integration.pipeline.test_builtin_sink_effect_recovery import _begin, _json_sink, _register_sink, _tokens
from tests.unit.engine.test_sink_effect_executor import _execution_request
from tests.unit.engine.test_sink_effect_virtual_predecessor import _members_with_payloads
from tests.unit.plugins.sinks.test_remote_object_sink_effects import _s3, _S3Store


@pytest.mark.parametrize("current_payloads", [[{"value": 1}], [{"value": 2}], [{"value": 1}, {"value": 1}]])
@pytest.mark.parametrize("run_mode", (RunMode.REPLAY, RunMode.VERIFY))
def test_virtual_sink_effect_compares_complete_member_set_without_publication(
    tmp_path: Path, current_payloads: list[dict[str, object]], run_mode: RunMode
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        source_run_id, source_sink_id, source_members = _members_with_payloads(factory, [{"value": 1}])
        current_run_id, current_sink_id, current_members = _members_with_payloads(factory, current_payloads)
        assert source_sink_id == current_sink_id

        SinkEffectCoordinator(
            factory=factory,
            worker_id="source",
            coordination_token=leader_token_for(db, source_run_id),
        ).execute(_execution_request(source_run_id, source_sink_id, source_members), _json_sink(tmp_path / "source.jsonl"))
        adapter = VirtualReplaySinkEffect(factory=factory, source_run_id=source_run_id, sink_node_id=current_sink_id)
        result = SinkEffectCoordinator(
            factory=factory,
            worker_id=f"virtual:{current_run_id}",
            coordination_token=leader_token_for(db, current_run_id),
        ).execute(_execution_request(current_run_id, current_sink_id, current_members), adapter)
        assert result.effect.state is SinkEffectState.FINALIZED
        assert result.effect.publication_performed is False
        assert result.effect.publication_evidence_kind == "virtual"
        assert result.artifact.publication_performed is False
        assert {attempt.action for attempt in factory.execution.sink_effects.get_attempts_for_run(current_run_id)} <= {
            SinkEffectAttemptAction.INSPECT
        }

        if current_payloads == [{"value": 1}]:
            verify_virtual_sink_members(factory, source_run_id=source_run_id, current_run_id=current_run_id, mode=run_mode)
        else:
            error_type = VerificationMismatchError if run_mode is RunMode.VERIFY else OrchestrationInvariantError
            with pytest.raises(error_type, match="sink output differs"):
                verify_virtual_sink_members(factory, source_run_id=source_run_id, current_run_id=current_run_id, mode=run_mode)
    finally:
        db.close()


@pytest.mark.parametrize("sink_kind", ("file", "sql", "cloud"))
@pytest.mark.parametrize("run_mode", (RunMode.REPLAY, RunMode.VERIFY))
def test_non_live_run_with_real_sink_records_virtual_effect_without_touching_target(
    tmp_path: Path, sink_kind: str, run_mode: RunMode
) -> None:
    """Configured file, SQL and cloud sinks cannot publish in either mode."""
    db = make_landscape_db()
    try:
        factory, run_id, source_id = _begin(db)
        sink_id = _register_sink(factory, run_id, name="output", plugin_name="json")
        sink_path = tmp_path / "output.jsonl"
        sql_path = tmp_path / "output.db"
        cloud_store = _S3Store()
        sink: BaseSink
        if sink_kind == "file":
            sink = _json_sink(sink_path)
        elif sink_kind == "sql":
            sink = DatabaseSink({"url": f"sqlite:///{sql_path}", "table": "output", "schema": {"mode": "observed"}})
        else:
            sink = _s3(cloud_store)
        sink.node_id = sink_id
        tokens = _tokens(factory, run_id=run_id, source_id=source_id, rows=[{"value": 1}])

        def forbidden(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("configured sink effect method was called during replay")

        sink.inspect_effect = forbidden
        sink.prepare_effect = forbidden
        sink.commit_effect = forbidden
        sink.reconcile_effect = forbidden
        ctx = PluginContext(
            run_id=run_id,
            config={},
            landscape=factory.plugin_audit_writer(),
            node_id=sink_id,
            run_mode=run_mode,
            replay_from="source-run",
            call_mode_session=SimpleNamespace(mode=run_mode),
        )
        artifact, diversions = SinkExecutor(
            factory.execution,
            factory.data_flow,
            SpanFactory(),
            run_id,
            factory=factory,
            worker_id="replay-sink-test",
            coordination_token=leader_token_for(db, run_id),
        ).write(
            sink,
            tokens,
            ctx,
            1,
            sink_name="output",
            pending_outcome=PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW),
            effect_mode="write",
            join_group_id_by_token={tokens[0].token_id: None},
        )
        assert diversions.total == 0
        assert artifact is not None and artifact.publication_performed is False
        assert sink_path.exists() is False
        assert sql_path.exists() is False
        assert cloud_store.requests == []
        (effect,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert effect.publication_performed is False
        assert effect.publication_evidence_kind == "virtual"
        (operation,) = factory.execution.get_operations_for_run(run_id)
        assert operation.operation_type == "sink_write"
        assert operation.sink_effect_id == effect.effect_id
        assert operation.status == "completed"
    finally:
        db.close()
