"""RunContextFactory: per-run PluginContext construction and lifecycle startup.

Split out of the old ``RunExecutionCore`` by lifecycle boundary
(elspeth-b53a093321): this module owns the construction of the per-run
:class:`RunContext` — node-id assignment, PluginContext creation, the
``on_start`` plugin lifecycle (with cleanup on failure), and processor
assembly via the injected :class:`ProcessorFactory`. Shared verbatim by the
main run path (``LeaderDrainCoordinator.execute_run``) and the resume path
(``ResumeCoordinator.process_resumed_rows``).

Dependencies held by the factory:
- ``_ceremony``: RunCeremony for telemetry emission into the PluginContext
- ``_rate_limit_registry``: RateLimitRegistry for the PluginContext
- ``_concurrency_config``: RuntimeConcurrencyConfig for the PluginContext
- ``_processor_factory``: ProcessorFactory for traversal/coalesce assembly
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, cast

from elspeth.contracts import (
    TransformProtocol,
)
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.call_mode import CallModeSession
from elspeth.contracts.enums import RunMode
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.types import NodeID
from elspeth.core.replay_payload_store import SourceBoundPayloadStore, collect_source_payload_refs
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from elspeth.engine.orchestrator.cleanup import cleanup_plugins, plugin_node_scope
from elspeth.engine.orchestrator.graph_wiring import assign_plugin_node_ids
from elspeth.engine.orchestrator.run_modes import RuntimeRunMode, resolve_runtime_run_mode
from elspeth.engine.orchestrator.run_state import (
    AggNodeEntry,
    RunContext,
)
from elspeth.engine.orchestrator.source_compatibility import admit_registered_graph
from elspeth.engine.orchestrator.source_replay import AuditedSource, prepare_verified_sources

if TYPE_CHECKING:
    from elspeth.contracts.config.runtime import RuntimeConcurrencyConfig
    from elspeth.contracts.coordination import CoordinationToken
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.contracts.plugin_protocols import SinkProtocol, SourceProtocol
    from elspeth.core.config import ElspethSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.rate_limit import RateLimitRegistry
    from elspeth.engine.barrier_coordination import BarrierJournalRestoreContext
    from elspeth.engine.orchestrator.ceremony import RunCeremony
    from elspeth.engine.orchestrator.processor_factory import ProcessorFactory
    from elspeth.engine.orchestrator.run_state import (
        GraphArtifacts,
    )
    from elspeth.engine.orchestrator.types import (
        PipelineConfig,
    )


def build_agg_transform_lookup(config: PipelineConfig) -> dict[str, AggNodeEntry]:
    """Index batch-aware transforms sitting at aggregation nodes, by node id.

    config.transforms is homogeneous (Sequence[RowPlugin]) — protocol
    conformance is deliberately not re-measured here (elspeth-8783933d99):
    doing so silently dropped non-conforming transforms from the
    aggregation-timeout lookup, so their nodes never fired timeout flushes.
    """
    agg_transform_lookup: dict[str, AggNodeEntry] = {}
    if config.aggregation_settings:
        for t in config.transforms:
            if t.is_batch_aware and t.node_id is not None and t.node_id in config.aggregation_settings:
                agg_transform_lookup[t.node_id] = AggNodeEntry(transform=t, node_id=NodeID(t.node_id))
    return agg_transform_lookup


class RunContextFactory:
    """Builds the per-run RunContext: node ids, PluginContext, on_start, processor.

    Owns exactly the run-context construction lifecycle — everything that
    must happen between graph registration and the first processed row. Sink
    writing lives in :class:`~elspeth.engine.orchestrator.sink_flush.SinkFlushCoordinator`;
    processor assembly detail lives in the injected
    :class:`~elspeth.engine.orchestrator.processor_factory.ProcessorFactory`.
    """

    def __init__(
        self,
        *,
        ceremony: RunCeremony,
        rate_limit_registry: RateLimitRegistry | None,
        concurrency_config: RuntimeConcurrencyConfig | None,
        processor_factory: ProcessorFactory,
        llm_call_governance: LLMCallGovernance | None = None,
        call_mode_session_factory: Callable[[RecorderFactory, RuntimeRunMode, str], CallModeSession] | None = None,
    ) -> None:
        self._ceremony = ceremony
        self._rate_limit_registry = rate_limit_registry
        self._concurrency_config = concurrency_config
        self._processor_factory = processor_factory
        self._llm_call_governance = llm_call_governance
        self._call_mode_session_factory = call_mode_session_factory

    def initialize_run_context(
        self,
        factory: RecorderFactory,
        run_id: str,
        config: PipelineConfig,
        graph: ExecutionGraph,
        settings: ElspethSettings | None,
        artifacts: GraphArtifacts,
        payload_store: PayloadStore,
        *,
        include_source_on_start: bool = True,
        barrier_restore: BarrierJournalRestoreContext | None = None,
        shutdown_event: threading.Event | None = None,
        coordination_token: CoordinationToken | None = None,
    ) -> RunContext:
        """Initialize run context: assign node IDs, create PluginContext, call on_start, build processor.

        Args:
            include_source_on_start: If True, call source.on_start(). False for resume
                (source was fully consumed in original run).
            barrier_restore: Resume-only journal-restore inputs (F1); None on
                the normal run path.
            coordination_token: Leader fencing token (ADR-030). Threaded into
                the RowProcessor so its worker identity doubles as the
                scheduler lease_owner and so the slice-2 step-4 fenced verbs
                (repair sweep, ingest) can present it.

        Returns:
            RunContext with ctx, processor, coalesce_executor, coalesce_node_map,
            and agg_transform_lookup.
        """
        source_id = artifacts.source_id
        sink_id_map = dict(artifacts.sink_id_map)
        transform_id_map = dict(artifacts.transform_id_map)
        config_gate_id_map = dict(artifacts.config_gate_id_map)
        coalesce_id_map = dict(artifacts.coalesce_id_map)
        edge_map = dict(artifacts.edge_map)
        route_resolution_map = graph.get_route_resolution_map()
        aggregation_node_ids = frozenset(graph.get_aggregation_id_map().values())

        # Assign node_ids to all plugins
        assign_plugin_node_ids(
            sources=config.sources,
            transforms=config.transforms,
            sinks=config.sinks,
            source_id_map=artifacts.source_id_map,
            transform_id_map=transform_id_map,
            sink_id_map=sink_id_map,
            aggregation_node_ids=aggregation_node_ids,
        )

        # Create context with the PluginAuditWriter
        runtime_mode = resolve_runtime_run_mode(config, settings)
        call_mode_session: CallModeSession | None = None
        plugin_payload_store = payload_store
        if runtime_mode.mode is not RunMode.LIVE:
            source_run_id = runtime_mode.replay_from
            if source_run_id is None:
                raise RuntimeError("Replay/verify source run ID is missing")
            if factory.audited_sources is None:
                raise RuntimeError("Replay/verify source snapshot was not admitted before plugin startup")
            admit_registered_graph(factory, source_run_id=source_run_id, current_run_id=run_id)
            blob_ref_fields: set[str] = set()
            for transform in config.transforms:
                if transform.name not in {"blob_csv_expand", "blob_json_expand", "blob_text_expand", "pdf_rasterize"}:
                    continue
                if transform.name == "blob_csv_expand" and transform.config.get("source") == "field":
                    continue
                field_name = transform.config.get("blob_ref_field") or "blob_ref"
                if type(field_name) is not str or not field_name:
                    raise RuntimeError(f"Invalid blob_ref_field for {transform.name}")
                blob_ref_fields.add(field_name)
            source_refs: frozenset[str] = frozenset()
            output_refs: frozenset[str] = frozenset()
            if blob_ref_fields:
                refs = collect_source_payload_refs(
                    factory,
                    source_run_id,
                    source_store=payload_store,
                    blob_ref_fields=blob_ref_fields,
                )
                source_refs = refs.input_refs
                output_refs = refs.output_refs
            plugin_payload_store = SourceBoundPayloadStore(
                mode=runtime_mode.mode,
                source_store=payload_store,
                current_store=payload_store,
                source_refs=source_refs,
                output_refs=output_refs,
            )
            if self._call_mode_session_factory is None:
                call_mode_session = AuditedCallModeSession(
                    factory,
                    current_run_id=run_id,
                    source_run_id=source_run_id,
                    mode=runtime_mode.mode,
                )
            else:
                call_mode_session = self._call_mode_session_factory(factory, runtime_mode, run_id)
        ctx = PluginContext(
            llm_call_governance=self._llm_call_governance,
            run_id=run_id,
            config=config.config,
            landscape=factory.plugin_audit_writer(),
            payload_store=plugin_payload_store,
            rate_limit_registry=self._rate_limit_registry,
            concurrency_config=self._concurrency_config,
            telemetry_emit=self._ceremony.emit_telemetry,
            shutdown_event=shutdown_event,
            coordination_token=coordination_token,
            run_mode=runtime_mode.mode,
            replay_from=runtime_mode.replay_from,
            call_mode_session=call_mode_session,
            audited_sources=factory.audited_sources,
        )

        # Set node_id on context for source validation error attribution
        # This must be set BEFORE source.load() so that any validation errors
        # (e.g., malformed CSV rows) can be attributed to the source node
        ctx.node_id = source_id

        started_sources: dict[str, SourceProtocol] = {}
        started_transforms: list[TransformProtocol] = []
        started_sinks: dict[str, SinkProtocol] = {}
        try:
            if include_source_on_start and runtime_mode.mode is not RunMode.REPLAY:
                for source_name, source in config.sources.items():
                    with plugin_node_scope(ctx, source.node_id):
                        source.on_start(ctx)
                    started_sources[source_name] = source
                ctx.node_id = source_id
            if runtime_mode.mode is RunMode.VERIFY:
                if coordination_token is None or factory.audited_sources is None:
                    raise RuntimeError("Verify source preflight lacks coordination or audited source evidence")
                ctx.verified_sources = prepare_verified_sources(
                    factory,
                    run_id,
                    cast("Mapping[str, AuditedSource]", factory.audited_sources),
                    config.sources,
                    ctx,
                    coordination_token,
                )
            for transform in config.transforms:
                with plugin_node_scope(ctx, transform.node_id):
                    transform.on_start(ctx)
                started_transforms.append(transform)
            if runtime_mode.mode is RunMode.LIVE:
                for sink_name, sink in config.sinks.items():
                    with plugin_node_scope(ctx, sink.node_id):
                        sink.on_start(ctx)
                    started_sinks[sink_name] = sink

            processor, coalesce_node_map, coalesce_executor = self._processor_factory.build_processor(
                graph=graph,
                config=config,
                settings=settings,
                factory=factory,
                run_id=run_id,
                source_id=source_id,
                edge_map=edge_map,
                route_resolution_map=route_resolution_map,
                config_gate_id_map=config_gate_id_map,
                coalesce_id_map=coalesce_id_map,
                payload_store=payload_store,
                barrier_restore=barrier_restore,
                coordination_token=coordination_token,
            )
        except Exception as pending_exc:
            cleanup_plugins(
                config,
                ctx,
                include_source=include_source_on_start,
                pending_exc=pending_exc,
                started_sources=started_sources,
                started_transforms=tuple(started_transforms),
                started_sinks=started_sinks,
            )
            raise

        # Pre-compute aggregation transform lookup for O(1) access per timeout check
        agg_transform_lookup = build_agg_transform_lookup(config)

        return RunContext(
            ctx=ctx,
            processor=processor,
            coalesce_executor=coalesce_executor,
            coalesce_node_map=coalesce_node_map,
            agg_transform_lookup=agg_transform_lookup,
        )
