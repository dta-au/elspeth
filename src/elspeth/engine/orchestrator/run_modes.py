"""Resolve the requested run mode at the runtime admission boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.call_mode import RuntimeRunMode as RuntimeRunMode
from elspeth.contracts.enums import RunMode, RunStatus
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.landscape.factory import RecorderFactory

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.engine.orchestrator.types import PipelineConfig


def resolve_runtime_run_mode(config: PipelineConfig, settings: ElspethSettings | None) -> RuntimeRunMode:
    """Read the persisted config and reject a conflicting settings object.

    Programmatic callers may omit ``settings``. The resolved PipelineConfig is
    still authoritative: ignoring its mode would turn an intended replay into
    a live execution while recording ``run_mode=replay`` in settings_json.
    """
    raw_mode = config.config["run_mode"] if "run_mode" in config.config else RunMode.LIVE.value
    if type(raw_mode) not in (str, RunMode):
        raise OrchestrationInvariantError("PipelineConfig.run_mode must be a string")
    try:
        mode = RunMode(raw_mode)
    except ValueError as exc:
        raise OrchestrationInvariantError(f"Unsupported PipelineConfig.run_mode: {raw_mode!r}") from exc

    replay_from = config.config["replay_from"] if "replay_from" in config.config else None
    if replay_from is not None and (type(replay_from) is not str or not replay_from.strip()):
        raise OrchestrationInvariantError("PipelineConfig.replay_from must be a non-empty run ID when set")
    if mode is not RunMode.LIVE and replay_from is None:
        raise OrchestrationInvariantError(f"PipelineConfig.replay_from is required for {mode.value} mode")

    if settings is not None and (settings.run_mode is not mode or settings.replay_from != replay_from):
        raise OrchestrationInvariantError("PipelineConfig run_mode/replay_from disagree with ElspethSettings")

    if mode is not RunMode.LIVE:
        if settings is None:
            raise OrchestrationInvariantError("Replay/verify requires full ElspethSettings for admission")
        admit_nonlive_settings(settings)
        # Public programmatic callers cannot supply their own admission rule.
        # A no-op callback would let an unreviewed plugin reach startup hooks.
        from elspeth.plugins.infrastructure.run_mode_capabilities import admit_nonlive_runtime_plugin_instances

        admit_nonlive_runtime_plugin_instances(config, settings)

    return RuntimeRunMode(mode=mode, replay_from=replay_from)


def admit_nonlive_settings(settings: ElspethSettings) -> None:
    """Refuse side channels that have no mode-aware execution contract."""
    if settings.run_mode is RunMode.LIVE:
        return
    if settings.concurrency.max_workers != 1:
        raise OrchestrationInvariantError("Replay/verify requires concurrency.max_workers=1")
    if settings.depends_on:
        raise OrchestrationInvariantError("Replay/verify with depends_on is unsupported without mode-bound dependency runs")
    if settings.collection_probes:
        raise OrchestrationInvariantError("Replay/verify with collection_probes is unsupported")
    if settings.commencement_gates:
        raise OrchestrationInvariantError("Replay/verify with commencement_gates is unsupported")
    if settings.landscape.export.enabled:
        raise OrchestrationInvariantError("Replay/verify with Landscape export is unsupported without an isolated export sink")
    if settings.telemetry.enabled:
        raise OrchestrationInvariantError("Replay/verify with telemetry exporters is unsupported")


def admit_source_run(db: LandscapeDB, runtime_mode: RuntimeRunMode, *, current_run_id: str | None = None) -> None:
    """Refuse missing, moving, or partial source runs before effects."""
    if runtime_mode.mode is RunMode.LIVE:
        return
    source_run_id = runtime_mode.replay_from
    if source_run_id is None:
        raise OrchestrationInvariantError("Replay/verify source run ID is missing")
    if source_run_id == current_run_id:
        raise OrchestrationInvariantError("A run cannot replay or verify against itself")
    source = RecorderFactory.read_only(db).run_lifecycle.get_run(source_run_id)
    if source is None:
        raise OrchestrationInvariantError(f"Replay/verify source run {source_run_id!r} does not exist")
    if source.status not in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY):
        raise OrchestrationInvariantError(
            f"Replay/verify source run {source_run_id!r} is not a completed run (status={source.status.value})"
        )
    if source.completed_at is None:
        raise OrchestrationInvariantError(f"Replay/verify source run {source_run_id!r} has no completion timestamp")


def refuse_nonlive_resume(db: LandscapeDB, run_id: str) -> None:
    """Prevent a prior replay/verify attempt from resuming as live work."""
    source = RecorderFactory.read_only(db).run_lifecycle.get_run(run_id)
    if source is not None and source.run_mode is not RunMode.LIVE:
        raise OrchestrationInvariantError(f"Cannot resume {source.run_mode.value} run {run_id!r}: mode-aware resume is not implemented")
