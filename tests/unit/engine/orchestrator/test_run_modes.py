"""Replay/verify admission cannot silently downgrade to a live run."""

from pathlib import Path
from typing import cast

import pytest

from elspeth.contracts import SinkProtocol, SourceProtocol
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.config import ElspethSettings, resolve_config
from elspeth.core.landscape.database import LandscapeDB
from elspeth.engine.orchestrator.run_modes import admit_source_run, resolve_runtime_run_mode
from elspeth.engine.orchestrator.types import PipelineConfig
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sources.csv_source import CSVSource


def _settings(mode: RunMode, *, workers: int = 1) -> ElspethSettings:
    return ElspethSettings(
        sources={"primary": {"plugin": "csv", "on_success": "output"}},
        sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        run_mode=mode,
        replay_from="run-does-not-exist" if mode is not RunMode.LIVE else None,
        concurrency={"max_workers": workers},
    )


def _pipeline(settings: ElspethSettings) -> PipelineConfig:
    return PipelineConfig(
        sources={"primary": cast(SourceProtocol, object.__new__(CSVSource))},
        transforms=[],
        sinks={"output": cast(SinkProtocol, object.__new__(CSVSink))},
        config=resolve_config(settings),
    )


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
def test_nonlive_mode_is_resolved_from_persisted_config(mode: RunMode) -> None:
    settings = _settings(mode)
    resolved = resolve_runtime_run_mode(_pipeline(settings), settings)
    assert resolved.mode is mode
    assert resolved.replay_from == "run-does-not-exist"


def test_nonlive_mode_rejects_omitted_settings_or_parallel_workers() -> None:
    settings = _settings(RunMode.REPLAY)
    with pytest.raises(OrchestrationInvariantError, match="full ElspethSettings"):
        resolve_runtime_run_mode(_pipeline(settings), None)

    parallel_settings = _settings(RunMode.REPLAY, workers=2)
    with pytest.raises(OrchestrationInvariantError, match="max_workers=1"):
        resolve_runtime_run_mode(_pipeline(parallel_settings), parallel_settings)


def test_nonlive_mode_rejects_programmatic_impostor_before_runtime_work() -> None:
    settings = _settings(RunMode.REPLAY)
    pipeline = PipelineConfig(
        sources={"primary": cast(SourceProtocol, object())},
        transforms=[],
        sinks={"output": cast(SinkProtocol, object.__new__(CSVSink))},
        config=resolve_config(settings),
    )
    with pytest.raises(OrchestrationInvariantError, match="not the reviewed built-in class"):
        resolve_runtime_run_mode(pipeline, settings)


def test_nonlive_mode_refuses_caller_supplied_noop_validator() -> None:
    settings = _settings(RunMode.REPLAY)
    with pytest.raises(TypeError, match="nonlive_plugin_validator"):
        PipelineConfig(
            sources={"primary": cast(SourceProtocol, object.__new__(CSVSource))},
            transforms=[],
            sinks={"output": cast(SinkProtocol, object.__new__(CSVSink))},
            config=resolve_config(settings),
            nonlive_plugin_validator=lambda *_: None,
        )


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
def test_nonexistent_source_run_refused_before_effects(tmp_path: Path, mode: RunMode) -> None:
    settings = _settings(mode)
    runtime_mode = resolve_runtime_run_mode(_pipeline(settings), settings)
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        with pytest.raises(OrchestrationInvariantError, match="does not exist"):
            admit_source_run(db, runtime_mode)
    finally:
        db.close()
