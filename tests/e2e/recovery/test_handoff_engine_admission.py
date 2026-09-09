"""Handoff rejects changed code before CAS and owner loss before replay."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.contracts.checkpoint import CheckpointDraft
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_start_admission import RunStartAdmissionRepository, RunStartAdmissionState
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator import Orchestrator
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _SOURCE_ROWS,
    _T0,
    _build_pipeline,
    _coord,
    _coordination_events,
    _resume,
    _resume_point,
    _run_to_interrupted_checkpoint,
)
from tests.fixtures.landscape import expire_leader_seat, make_landscape_db
from tests.fixtures.stores import MockPayloadStore


@pytest.mark.parametrize("changed_field", ["version", "source_hash"])
def test_changed_plugin_refuses_before_resume_cas(tmp_path: Path, changed_field: str) -> None:
    crashed = _run_to_interrupted_checkpoint(tmp_path, MockClock(start=_T0))
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)
    point = _resume_point(crashed)
    before = _coordination_events(crashed.db, crashed.run_id)
    if changed_field == "version":
        config.transforms[0].plugin_version = "changed-implementation"
    else:
        config.transforms[0].source_file_hash = "b" * 64
    with pytest.raises(NonResumableRunError, match="implementation changed"):
        crashed.resume_orchestrator().resume(point, config, graph, payload_store=crashed.payload_store)
    assert _coordination_events(crashed.db, crashed.run_id) == before
    assert sink.results == []


def test_owner_guard_refusal_releases_seat_without_terminalizing(tmp_path: Path) -> None:
    crashed = _run_to_interrupted_checkpoint(tmp_path, MockClock(start=_T0))
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)
    orchestrator = crashed.resume_orchestrator()

    def refuse() -> None:
        raise RuntimeError("web owner replaced")

    with patch.object(orchestrator._resume_coordinator, "_repair_resume_batches") as repair:
        with pytest.raises(RuntimeError, match="web owner replaced"):
            orchestrator.resume(_resume_point(crashed), config, graph, payload_store=crashed.payload_store, pre_effect_guard=refuse)
        repair.assert_not_called()
    assert sink.results == []
    result, _resumed_sink, _ = _resume(crashed)
    assert result.status.value == "completed"


def test_checkpoint_advanced_before_cas_is_restored_under_new_authority(tmp_path: Path) -> None:
    crashed = _run_to_interrupted_checkpoint(tmp_path, MockClock(start=_T0))
    config, graph, _sink, _source = _build_pipeline(_SOURCE_ROWS)
    point = _resume_point(crashed)
    orchestrator = crashed.resume_orchestrator()
    coordinator = orchestrator._resume_coordinator
    original_acquire = coordinator._acquire_resume_leadership
    manager = orchestrator._checkpoint_manager
    assert manager is not None
    newer_sequence = point.sequence_number + 10

    def advance_then_acquire(snapshot):
        previous = _coord(crashed).acquire_run_leadership(
            run_id=crashed.run_id, worker_id="advancing-owner", window_seconds=_DEFAULT_LEASE_SECONDS
        )
        manager.create_checkpoint(
            draft=CheckpointDraft(
                run_id=crashed.run_id,
                sequence_number=newer_sequence,
                upstream_topology_hash=point.checkpoint.full_topology_hash,
            ),
            coordination_token=previous,
        )
        _coord(crashed).release_seat(token=previous)
        return original_acquire(snapshot)

    with (
        patch.object(coordinator, "_acquire_resume_leadership", side_effect=advance_then_acquire),
        patch.object(orchestrator._checkpoints, "rebase_sequence", wraps=orchestrator._checkpoints.rebase_sequence) as rebase,
    ):
        result = orchestrator.resume(point, config, graph, payload_store=crashed.payload_store)
    assert result.status.value == "completed"
    assert rebase.call_args_list[-1].args == (newer_sequence,)


def test_fresh_run_guard_is_after_landscape_and_before_plugins() -> None:
    db = make_landscape_db()
    store = MockPayloadStore()
    factory = RecorderFactory(db, payload_store=store)
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)

    def refuse() -> None:
        run = factory.run_lifecycle.get_run("post-acquisition-refusal")
        assert run is not None
        assert run.status.value == "running"
        raise RuntimeError("web owner replaced")

    with pytest.raises(RuntimeError, match="web owner replaced"):
        Orchestrator(db).run(
            config,
            graph,
            payload_store=store,
            shutdown_event=threading.Event(),
            run_id="post-acquisition-refusal",
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
            pre_effect_guard=refuse,
        )
    run = factory.run_lifecycle.get_run("post-acquisition-refusal")
    assert run is not None
    assert run.status.value == "running"
    assert sink.results == []


def test_resume_owner_loss_after_initial_guard_does_not_terminalize(tmp_path: Path) -> None:
    crashed = _run_to_interrupted_checkpoint(tmp_path, MockClock(start=_T0))
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("web owner replaced after admission")

    with pytest.raises(RuntimeError, match="web owner replaced after admission"):
        crashed.resume_orchestrator().resume(
            _resume_point(crashed),
            config,
            graph,
            payload_store=crashed.payload_store,
            pre_effect_guard=check,
            check_coordination_latch=check,
        )
    run = crashed.factory.run_lifecycle.get_run(crashed.run_id)
    assert run is not None
    assert run.status.value == "running"
    assert sink.results == []
    result, _, _ = _resume(crashed)
    assert result.status.value == "completed"


def test_prepared_permit_restarts_after_graph_registration_crash() -> None:
    db = make_landscape_db()
    store = MockPayloadStore()
    binding = RunStartPermitBinding("prepared-retry", "permit-1", 1, "a" * 64)
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)
    original = Orchestrator(db)
    register = original._register_graph_nodes_and_edges

    def crash_after_graph(*args, **kwargs):
        register(*args, **kwargs)
        raise KeyboardInterrupt("process died after graph registration")

    kwargs = {
        "payload_store": store,
        "shutdown_event": threading.Event(),
        "run_id": binding.run_id,
        "run_start_permit": binding,
        "openrouter_catalog_sha256": "0" * 64,
        "openrouter_catalog_source": "bundled",
    }
    with patch.object(original, "_register_graph_nodes_and_edges", side_effect=crash_after_graph), pytest.raises(KeyboardInterrupt):
        original.run(config, graph, **kwargs)
    admission = RunStartAdmissionRepository(db).observe(binding)
    assert admission is not None
    assert admission.state is RunStartAdmissionState.PREPARED
    assert sink.results == []
    expire_leader_seat(db, binding.run_id)
    fresh_config, fresh_graph, fresh_sink, _ = _build_pipeline(_SOURCE_ROWS)
    result = Orchestrator(db).run(fresh_config, fresh_graph, **kwargs)
    assert result.status.value == "completed"
    assert len(fresh_sink.results) == len(_SOURCE_ROWS)

    events_before_retry = _coordination_events(db, binding.run_id)
    duplicate_config, duplicate_graph, duplicate_sink, _ = _build_pipeline(_SOURCE_ROWS)
    with pytest.raises(NonResumableRunError, match="first-effect boundary"):
        Orchestrator(db).run(duplicate_config, duplicate_graph, **kwargs)
    assert duplicate_sink.results == []
    assert _coordination_events(db, binding.run_id) == events_before_retry


def test_duplicate_prepared_dispatch_does_not_borrow_live_leader_token() -> None:
    db = make_landscape_db()
    store = MockPayloadStore()
    binding = RunStartPermitBinding("prepared-contended", "permit-1", 1, "a" * 64)
    config, graph, sink, _source = _build_pipeline(_SOURCE_ROWS)

    def race_dispatch() -> None:
        competitor_config, competitor_graph, competitor_sink, _ = _build_pipeline(_SOURCE_ROWS)
        before = _coordination_events(db, binding.run_id)
        with pytest.raises(NonResumableRunError, match="leadership is held"):
            Orchestrator(db).run(
                competitor_config,
                competitor_graph,
                payload_store=store,
                shutdown_event=threading.Event(),
                run_start_permit=binding,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        assert _coordination_events(db, binding.run_id) == before
        assert competitor_sink.results == []

    result = Orchestrator(db).run(
        config,
        graph,
        payload_store=store,
        shutdown_event=threading.Event(),
        run_start_permit=binding,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
        pre_effect_guard=race_dispatch,
    )
    assert result.status.value == "completed"
    assert len(sink.results) == len(_SOURCE_ROWS)
