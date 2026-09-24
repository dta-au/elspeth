"""Replay sink effects retain audit provenance without publishing a sink."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from elspeth.contracts import PendingOutcome, RunMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, VerificationMismatchError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.sink_effects import SinkEffectAttemptAction, SinkEffectRole, SinkEffectState
from elspeth.core.landscape.schema import sink_effect_members_table
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


@pytest.mark.parametrize("effect_count", (2, 4))
def test_virtual_effects_share_one_validated_source_scan_across_preparations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effect_count: int
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        payloads = [{"value": index} for index in range(effect_count)]
        source_run_id, sink_id, source_members = _members_with_payloads(factory, payloads)
        current_run_id, current_sink_id, current_members = _members_with_payloads(factory, payloads)
        assert sink_id == current_sink_id
        source_coordinator = SinkEffectCoordinator(
            factory=factory, worker_id="source", coordination_token=leader_token_for(db, source_run_id)
        )
        for member in source_members:
            source_coordinator.execute(_execution_request(source_run_id, sink_id, (member,)), _json_sink(tmp_path / "source.jsonl"))
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_token_for(db, source_run_id))

        repository = factory.execution.sink_effects
        source_effect_ids = {effect.effect_id for effect in repository.get_effects_for_run(source_run_id)}
        assert len(source_effect_ids) == len(source_members)
        reads = {"effects": 0, "attempts": 0, "members": 0}
        get_effects = repository.get_effects_for_run
        get_attempts = repository.get_attempts
        get_members = repository.get_members

        def counted_effects(run_id: str) -> object:
            if run_id == source_run_id:
                reads["effects"] += 1
            return get_effects(run_id)

        def counted_attempts(effect_id: str) -> object:
            if effect_id in source_effect_ids:
                reads["attempts"] += 1
            return get_attempts(effect_id)

        def counted_members(effect_id: str) -> object:
            if effect_id in source_effect_ids:
                reads["members"] += 1
            return get_members(effect_id)

        monkeypatch.setattr(repository, "get_effects_for_run", counted_effects)
        monkeypatch.setattr(repository, "get_attempts", counted_attempts)
        monkeypatch.setattr(repository, "get_members", counted_members)
        for index, member in enumerate(current_members):
            adapter = VirtualReplaySinkEffect(factory=factory, source_run_id=source_run_id, sink_node_id=sink_id)
            SinkEffectCoordinator(factory=factory, worker_id="replay", coordination_token=leader_token_for(db, current_run_id)).execute(
                _execution_request(current_run_id, sink_id, (member,)), adapter
            )
            if index == 0:
                assert reads == {"effects": 1, "attempts": len(source_members), "members": len(source_members)}
        assert reads == {"effects": 1, "attempts": len(source_members), "members": len(source_members)}
        verify_virtual_sink_members(factory, source_run_id=source_run_id, current_run_id=current_run_id, mode=RunMode.REPLAY)
        assert reads == {"effects": 3, "attempts": 2 * len(source_members), "members": 2 * len(source_members)}
    finally:
        db.close()


def test_virtual_source_cache_separates_runs_and_sink_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        source_runs: list[str] = []
        for value in (1, 2):
            source_run_id, sink_id, members = _members_with_payloads(factory, [{"value": value}])
            SinkEffectCoordinator(factory=factory, worker_id="source", coordination_token=leader_token_for(db, source_run_id)).execute(
                _execution_request(source_run_id, sink_id, members), _json_sink(tmp_path / f"source-{value}.jsonl")
            )
            factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_token_for(db, source_run_id))
            source_runs.append(source_run_id)

        repository = factory.execution.sink_effects
        get_effects = repository.get_effects_for_run
        reads = dict.fromkeys(source_runs, 0)

        def counted_effects(run_id: str) -> object:
            if run_id in reads:
                reads[run_id] += 1
            return get_effects(run_id)

        monkeypatch.setattr(repository, "get_effects_for_run", counted_effects)
        primary_first = VirtualReplaySinkEffect(factory=factory, source_run_id=source_runs[0], sink_node_id=sink_id)
        failsink_first = VirtualReplaySinkEffect(
            factory=factory, source_run_id=source_runs[0], sink_node_id=sink_id, role=SinkEffectRole.FAILSINK
        )
        primary_second = VirtualReplaySinkEffect(factory=factory, source_run_id=source_runs[1], sink_node_id=sink_id)

        first = primary_first._source_dispositions()
        assert len(first) == 1
        assert failsink_first._source_dispositions() == {}
        second = primary_second._source_dispositions()
        assert len(second) == 1 and second != first
        assert VirtualReplaySinkEffect(factory=factory, source_run_id=source_runs[0], sink_node_id=sink_id)._source_dispositions() == first
        assert reads == {source_runs[0]: 1, source_runs[1]: 1}
    finally:
        db.close()


def test_virtual_source_does_not_cache_active_run(tmp_path: Path) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        source_run_id, sink_id, members = _members_with_payloads(factory, [{"value": 1}, {"value": 2}])
        adapter = VirtualReplaySinkEffect(factory=factory, source_run_id=source_run_id, sink_node_id=sink_id)
        coordinator = SinkEffectCoordinator(factory=factory, worker_id="source", coordination_token=leader_token_for(db, source_run_id))
        coordinator.execute(_execution_request(source_run_id, sink_id, members[:1]), _json_sink(tmp_path / "source.jsonl"))
        assert len(adapter._source_dispositions()) == 1
        coordinator.execute(_execution_request(source_run_id, sink_id, members[1:]), _json_sink(tmp_path / "source.jsonl"))
        assert len(adapter._source_dispositions()) == 2
    finally:
        db.close()


def test_virtual_sink_verification_rechecks_source_attribution_after_cache_fill(tmp_path: Path) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        source_run_id, sink_id, source_members = _members_with_payloads(factory, [{"value": 1}])
        current_run_id, _, current_members = _members_with_payloads(factory, [{"value": 1}])
        SinkEffectCoordinator(factory=factory, worker_id="source", coordination_token=leader_token_for(db, source_run_id)).execute(
            _execution_request(source_run_id, sink_id, source_members), _json_sink(tmp_path / "source.jsonl")
        )
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_token_for(db, source_run_id))
        adapter = VirtualReplaySinkEffect(factory=factory, source_run_id=source_run_id, sink_node_id=sink_id)
        SinkEffectCoordinator(factory=factory, worker_id="replay", coordination_token=leader_token_for(db, current_run_id)).execute(
            _execution_request(current_run_id, sink_id, current_members), adapter
        )
        verify_virtual_sink_members(factory, source_run_id=source_run_id, current_run_id=current_run_id, mode=RunMode.VERIFY)

        source_effect_id = factory.execution.sink_effects.get_effects_for_run(source_run_id)[0].effect_id
        with db.write_connection() as connection:
            connection.execute(
                update(sink_effect_members_table)
                .where(sink_effect_members_table.c.effect_id == source_effect_id)
                .values(reason_hash="0" * 64)
            )
        with pytest.raises(AuditIntegrityError, match="source sink diversion reason disagrees"):
            verify_virtual_sink_members(factory, source_run_id=source_run_id, current_run_id=current_run_id, mode=RunMode.VERIFY)
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
