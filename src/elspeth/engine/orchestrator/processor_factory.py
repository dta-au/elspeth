"""ProcessorFactory: RowProcessor and coalesce-plane assembly.

Split out of the old ``RunExecutionCore`` by lifecycle boundary
(elspeth-b53a093321): this module owns processor construction and nothing
else. :func:`build_row_processor` is THE shared construction path for
leader, resume, and follower processors (elspeth-577179bba1);
:class:`ProcessorFactory` carries the orchestrator-held assembly seams and
defaults ``mode=ProcessorMode.LEADER``.

Dependencies held by the factory:
- ``_span_factory``: SpanFactory for executor spans
- ``_clock``: Clock injected into processors/executors
- ``_concurrency_config``: RuntimeConcurrencyConfig for worker counts
- ``_telemetry``: TelemetryManagerProtocol passed directly to RowProcessor
- ``_coalesce_completed_keys_limit``: bound on CoalesceExecutor completed keys
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from elspeth.contracts import BatchTransformProtocol
from elspeth.contracts.config import RuntimeRetryConfig
from elspeth.contracts.errors import (
    FrameworkBugError,
    OrchestrationInvariantError,
)
from elspeth.contracts.schema_contract_factory import create_contract_from_config
from elspeth.contracts.types import (
    CoalesceName,
    CollectorName,
    NodeID,
    RowUnionName,
)
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.barrier_coordination import BarrierJournalRestoreContext
from elspeth.engine.orchestrator.graph_wiring import build_dag_traversal_context
from elspeth.engine.processor import RowProcessor, make_step_resolver
from elspeth.engine.retry import RetryManager
from elspeth.engine.scheduler_drain import ProcessorMode

if TYPE_CHECKING:
    from elspeth.contracts import RouteDestination
    from elspeth.contracts.config.runtime import RuntimeConcurrencyConfig
    from elspeth.contracts.coordination import CoordinationToken
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.contracts.types import (
        BranchName,
        GateName,
    )
    from elspeth.core.config import AggregationSettings, ElspethSettings, ScopeSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.clock import Clock
    from elspeth.engine.coalesce_executor import CoalesceExecutor
    from elspeth.engine.executors.collector import CollectorExecutor
    from elspeth.engine.orchestrator.ports import TelemetryManagerProtocol
    from elspeth.engine.orchestrator.types import (
        PipelineConfig,
    )
    from elspeth.engine.row_union_executor import RowUnionExecutor
    from elspeth.engine.spans import SpanFactory


class ProcessorFactory:
    """Builds the leader-mode RowProcessor and coalesce plane for a run.

    Thin stateful wrapper over the module-level :func:`build_row_processor`:
    it stores the orchestrator-held assembly seams (spans, clock,
    concurrency, telemetry, coalesce bounds) so the run-context factory can
    request a processor without threading eight arguments through every call.
    """

    def __init__(
        self,
        *,
        span_factory: SpanFactory,
        clock: Clock,
        concurrency_config: RuntimeConcurrencyConfig | None,
        telemetry: TelemetryManagerProtocol | None,
        coalesce_completed_keys_limit: int,
    ) -> None:
        self._span_factory = span_factory
        self._clock = clock
        self._concurrency_config = concurrency_config
        self._telemetry = telemetry
        self._coalesce_completed_keys_limit = coalesce_completed_keys_limit

    def build_processor(
        self,
        *,
        graph: ExecutionGraph,
        config: PipelineConfig,
        settings: ElspethSettings | None,
        factory: RecorderFactory,
        run_id: str,
        source_id: NodeID,
        edge_map: dict[tuple[NodeID, str], str],
        route_resolution_map: dict[tuple[NodeID, str], RouteDestination] | None,
        config_gate_id_map: dict[GateName, NodeID],
        coalesce_id_map: dict[CoalesceName, NodeID],
        payload_store: PayloadStore,
        barrier_restore: BarrierJournalRestoreContext | None = None,
        coordination_token: CoordinationToken | None = None,
    ) -> tuple[RowProcessor, dict[CoalesceName, NodeID], CoalesceExecutor | None]:
        """Build a leader-mode RowProcessor with all supporting infrastructure.

        Thin delegate to the shared module-level :func:`build_row_processor`
        (elspeth-577179bba1 — one construction path for leader, resume, and
        follower), forwarding this factory's stored seams and defaulting
        ``mode=ProcessorMode.LEADER``. The name and signature are load-bearing
        for the run-context factory call site and test patches.

        Returns:
            Tuple of (processor, coalesce_node_map, coalesce_executor).
        """
        return build_row_processor(
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
            span_factory=self._span_factory,
            clock=self._clock,
            max_workers=self._concurrency_config.max_workers if self._concurrency_config else None,
            telemetry=self._telemetry,
            coalesce_completed_keys_limit=self._coalesce_completed_keys_limit,
            barrier_restore=barrier_restore,
            coordination_token=coordination_token,
        )


def build_row_processor(
    *,
    graph: ExecutionGraph,
    config: PipelineConfig,
    settings: ElspethSettings | None,
    factory: RecorderFactory,
    run_id: str,
    source_id: NodeID,
    edge_map: dict[tuple[NodeID, str], str],
    route_resolution_map: dict[tuple[NodeID, str], RouteDestination] | None,
    config_gate_id_map: dict[GateName, NodeID],
    coalesce_id_map: dict[CoalesceName, NodeID],
    payload_store: PayloadStore,
    span_factory: SpanFactory,
    clock: Clock | None,
    max_workers: int | None,
    telemetry: TelemetryManagerProtocol | None,
    # Matches the Orchestrator default (core.py). Only consumed on the
    # leader/resume path — FOLLOWER mode never builds a CoalesceExecutor.
    coalesce_completed_keys_limit: int = 10000,
    mode: ProcessorMode = ProcessorMode.LEADER,
    scheduler_lease_owner: str | None = None,
    scheduler_lease_seconds: int = 300,
    scheduler_heartbeat_seconds: int = 60,
    barrier_restore: BarrierJournalRestoreContext | None = None,
    coordination_token: CoordinationToken | None = None,
) -> tuple[RowProcessor, dict[CoalesceName, NodeID], CoalesceExecutor | None]:
    """Build a RowProcessor with all supporting infrastructure.

    THE shared processor construction path (elspeth-577179bba1, absorbing the
    scope transferred from elspeth-07b2031e41 part (b)): the main run path and
    the resume path call it through :meth:`ProcessorFactory.build_processor`
    (mode=LEADER), and ``orchestrator/follower.py:build_follower_processor``
    calls it directly with ``mode=ProcessorMode.FOLLOWER`` — previously a
    hand-assembled parallel RowProcessor argument list that could drift.

    Mode gates (LEADER behavior is unchanged; FOLLOWER matches the old
    follower.py hand assembly kwarg-for-kwarg):

    - retry_manager: FOLLOWER never constructs one (follower passes
      ``settings=None`` anyway; the explicit gate documents the intent).
    - coalesce executor: FOLLOWER gets ``coalesce_executor=None`` even on a
      coalesce graph, WITHOUT the settings.coalesce raise — the barrier/
      coalesce plane is leader-only (ADR-030 §B.2).
    - coalesce_on_success_map: FOLLOWER gets ``{}`` (no local merges).
    - aggregation_settings: FOLLOWER gets ``{}`` (no trigger evaluation);
      instead ``follower_barrier_node_ids`` carries the aggregation node ID
      set so batch-aware transforms are intercepted and mark_blocked (§B).
    - source_plugin: FOLLOWER gets ``None`` (no source ingest).
    - lease owner: a follower supplies its registered worker identity (§A.1);
      a leader uses the coordination token's worker_id and RowProcessor rejects
      any distinct explicit identity; otherwise None (tokenless direct harness,
      for which RowProcessor mints its own non-maintenance identity).
    - run_coordination: derived from token presence — a follower passes
      ``coordination_token=None``, so it never receives the §C.2 housekeeping
      repository. RowProcessor validates the FOLLOWER invariants fail-closed.

    Returns:
        Tuple of (processor, coalesce_node_map, coalesce_executor).
    """
    from elspeth.engine.coalesce_executor import CoalesceExecutor
    from elspeth.engine.tokens import TokenManager

    retry_manager: RetryManager | None = None
    if settings is not None and mode is not ProcessorMode.FOLLOWER:
        retry_manager = RetryManager(RuntimeRetryConfig.from_settings(settings.retry))

    # Derive coalesce routing from graph topology unconditionally.
    # If the graph has coalesce nodes, the processor needs branch_to_coalesce
    # regardless of whether settings is available.
    branch_to_coalesce: dict[BranchName, CoalesceName] = graph.get_branch_to_coalesce_map()
    coalesce_node_map: dict[CoalesceName, NodeID] = graph.get_coalesce_id_map()
    branch_to_row_union: dict[BranchName, RowUnionName] = graph.get_branch_to_row_union_map()
    row_union_node_map: dict[RowUnionName, NodeID] = graph.get_row_union_id_map()
    # THE settle-member walk's frame resolver (spec §6.1) — stored by
    # reference on the graph (graph.py:877's docstring), never copied, so
    # this same instance is also threaded to the processor's own
    # TokenManager (via RowProcessor.__init__) for register_expand_group's
    # mint-path writes (WS3 Task 5 step 3b) to stay visible to this read.
    group_bindings = graph.get_group_bindings()

    # Build traversal context BEFORE CoalesceExecutor/TokenManager so that
    # node_step_map is available for the step_resolver closure they require.
    traversal = build_dag_traversal_context(graph, config, config_gate_id_map)

    # Build step_resolver from shared factory (single source of truth).
    # Same factory is used by RowProcessor internally for its executors.
    step_resolver = make_step_resolver(traversal.node_step_map, source_id)

    coalesce_executor: CoalesceExecutor | None = None

    if coalesce_node_map and mode is not ProcessorMode.FOLLOWER:
        # Graph has coalesce nodes — settings.coalesce is required for
        # CoalesceExecutor registration (merge policy, timeout, etc.)
        if settings is None or not settings.coalesce:
            raise OrchestrationInvariantError(
                "Graph contains coalesce nodes but settings.coalesce is missing. "
                "Coalesce settings are required when the pipeline has fork/join patterns."
            )

        # payload_store intentionally omitted: CoalesceExecutor's TokenManager only
        # calls coalesce_tokens(), which does not persist payloads (payloads are
        # recorded by the RowProcessor's TokenManager during initial token creation).
        token_manager = TokenManager(factory.data_flow, step_resolver=step_resolver)
        coalesce_executor = CoalesceExecutor(
            execution=factory.execution,
            span_factory=span_factory,
            token_manager=token_manager,
            run_id=run_id,
            step_resolver=step_resolver,
            clock=clock,
            max_completed_keys=coalesce_completed_keys_limit,
            data_flow=factory.data_flow,
            barrier_restore_reads=factory.barrier_restore,
        )

        for coalesce_settings_entry in settings.coalesce:
            coalesce_node_id = coalesce_id_map[CoalesceName(coalesce_settings_entry.name)]
            # Extract guaranteed fields from branch schemas for lost-branch audit trail.
            # Returns dict[branch_name, SchemaConfig]; we extract guaranteed fields.
            branch_schema_configs = graph.get_coalesce_branch_schemas(CoalesceName(coalesce_settings_entry.name))
            branch_schemas: dict[str, tuple[str, ...]] | None = None
            if branch_schema_configs:
                branch_schemas = {
                    branch_name: tuple(sorted(schema.get_effective_guaranteed_fields()))
                    for branch_name, schema in branch_schema_configs.items()
                }

            # Retrieve pre-computed output schema from DAG builder (P2 fix).
            # This ensures runtime contracts match build-time schema computation,
            # preserving nullable semantics from the P1 fix.
            coalesce_node_info = graph.get_node_info(coalesce_node_id)
            if coalesce_node_info.output_schema_config is None:
                raise FrameworkBugError(
                    f"Coalesce node '{coalesce_node_id}' has no output_schema_config. "
                    f"The DAG builder must populate output_schema_config for all coalesce "
                    f"nodes via _assign_schema(). This indicates a builder bug."
                )
            output_schema = create_contract_from_config(coalesce_node_info.output_schema_config)

            coalesce_executor.register_coalesce(
                coalesce_settings_entry,
                coalesce_node_id,
                branch_schemas=branch_schemas,
                output_schema=output_schema,
            )
        # F1: no blob restore here — on resume the RowProcessor rebuilds
        # coalesce pendings from journal BLOCKED rows (barrier_restore).

    row_union_executor: RowUnionExecutor | None = None
    if row_union_node_map and mode is not ProcessorMode.FOLLOWER:
        from elspeth.engine.row_union_executor import RowUnionExecutor

        if settings is None or not settings.row_unions:
            raise OrchestrationInvariantError(
                "Graph contains row_union nodes but settings.row_unions is missing. "
                "row_union settings are required when fork branches release through a union barrier."
            )
        row_union_executor = RowUnionExecutor(
            execution=factory.execution,
            span_factory=span_factory,
            run_id=run_id,
            step_resolver=step_resolver,
            clock=clock,
            max_completed_keys=coalesce_completed_keys_limit,
            data_flow=factory.data_flow,
            barrier_restore_reads=factory.barrier_restore,
        )
        for row_union_settings_entry in settings.row_unions:
            row_union_executor.register_row_union(
                row_union_settings_entry,
                row_union_node_map[RowUnionName(row_union_settings_entry.name)],
            )

    # Collector plane (EXPAND-group closers, barrier-scopes spec §5): the same
    # coalesce/row_union shape — node ids from the graph, settings from
    # ElspethSettings — never a PipelineConfig field (META-29). The transform
    # instance rides the graph's own accessor beside the id map.
    collector_executor: CollectorExecutor | None = None
    collector_node_map: dict[CollectorName, NodeID] = graph.get_collector_id_map()
    if collector_node_map and mode is not ProcessorMode.FOLLOWER:
        from elspeth.engine.executors.collector import CollectorExecutor

        if settings is None or not settings.collectors:
            raise OrchestrationInvariantError(
                "Graph contains collector nodes but settings.collectors is missing. "
                "Collector settings are required when a scope's members close through a collector barrier."
            )
        scopes_by_closer: dict[str, ScopeSettings] = {scope.closer: scope for scope in settings.scopes}
        collector_transform_map = graph.get_collector_transform_map()
        # collect_tokens never calls register_expand_group (the release-group
        # mint is durable, in data_flow), so the executor's own TokenManager
        # needs no group_bindings — same as the coalesce executor's above.
        collector_executor = CollectorExecutor(
            execution=factory.execution,
            span_factory=span_factory,
            token_manager=TokenManager(factory.data_flow, step_resolver=step_resolver),
            run_id=run_id,
            step_resolver=step_resolver,
            data_flow=factory.data_flow,
            clock=clock,
            max_completed_keys=coalesce_completed_keys_limit,
            barrier_restore_reads=factory.barrier_restore,
        )
        for collector_settings_entry in settings.collectors:
            collector_name = CollectorName(collector_settings_entry.name)
            if collector_name not in collector_node_map:
                raise OrchestrationInvariantError(
                    f"settings.collectors names {collector_settings_entry.name!r} but the graph has no such collector node "
                    f"(graph collectors: {sorted(str(name) for name in collector_node_map)})."
                )
            if collector_settings_entry.name not in scopes_by_closer:
                raise OrchestrationInvariantError(
                    f"Collector {collector_settings_entry.name!r} has no scopes: entry naming it as closer; "
                    "a collector cannot register without its scope binding (spec §7 rule 1)."
                )
            if collector_name not in collector_transform_map:
                raise OrchestrationInvariantError(
                    f"Collector {collector_settings_entry.name!r} (node {collector_node_map[collector_name]!r}) is in the graph's "
                    "collector id map but has no transform instance in its collector transform map; the builder populates both together."
                )
            collector_executor.register_collector(
                collector_settings_entry,
                scopes_by_closer[collector_settings_entry.name],
                collector_node_map[collector_name],
                # The builder rejected any collector plugin with is_batch_aware=False
                # at graph construction, so the instance IS batch-shaped here — the
                # same narrowing _process_batch_aggregation_node applies to an
                # aggregation transform after its own is_batch_aware check.
                cast(BatchTransformProtocol, collector_transform_map[collector_name]),
            )
        # The row_union precedent stops at "settings present". The per-entry
        # raise above already guarantees registered ⊆ graph; this equality
        # covers the OTHER direction — a settings list naming only SOME of
        # the graph's collectors would register that subset and silently
        # strand every group bound to the rest. Both guards are load-bearing.
        registered = {CollectorName(name) for name in collector_executor.get_registered_names()}
        if registered != set(collector_node_map):
            raise OrchestrationInvariantError(
                f"settings.collectors registered {sorted(str(name) for name in registered)} but the graph's collector "
                f"nodes are {sorted(str(name) for name in collector_node_map)}; every graph collector needs exactly "
                "one settings entry."
            )

    # Derive coalesce on_success from graph's terminal sink map (graph-authoritative),
    # falling back to settings for non-terminal coalesce nodes. Followers never
    # complete a coalesce merge locally, so their map is empty (§B.2).
    coalesce_on_success_map: dict[CoalesceName, str] = {}
    if mode is not ProcessorMode.FOLLOWER:
        terminal_sink_map = graph.get_terminal_sink_map()
        for cname, cnode_id in coalesce_node_map.items():
            if cnode_id in terminal_sink_map:
                coalesce_on_success_map[cname] = terminal_sink_map[cnode_id]
            elif settings is not None and settings.coalesce:
                for coalesce_settings_entry in settings.coalesce:
                    if CoalesceName(coalesce_settings_entry.name) == cname and coalesce_settings_entry.on_success is not None:
                        coalesce_on_success_map[cname] = coalesce_settings_entry.on_success

    # A terminal collector (on_success names a sink) releases straight to that
    # sink; graph-authoritative like coalesce_on_success_map above. Followers
    # never complete a collector flush locally, so their map is empty.
    collector_on_success_map: dict[CollectorName, str] = {}
    if mode is not ProcessorMode.FOLLOWER:
        terminal_sink_map = graph.get_terminal_sink_map()
        for collector_name, collector_node_id in collector_node_map.items():
            if collector_node_id in terminal_sink_map:
                collector_on_success_map[collector_name] = terminal_sink_map[collector_node_id]

    branch_to_sink = graph.get_branch_to_sink_map()
    unbound_branch_first_node = graph.get_unbound_branch_first_nodes()

    # ADR-030 §B (slice 5): a follower runs no trigger evaluation
    # (aggregation_settings={}) — instead follower_barrier_node_ids carries
    # the aggregation node ID set so the processor intercepts batch-aware
    # transforms at those nodes and calls mark_blocked rather than executing
    # them row-wise (trigger evaluation is leader-only, §B.2).
    typed_aggregation_settings: dict[NodeID, AggregationSettings] = {}
    follower_barrier_node_ids: frozenset[NodeID] | None = None
    if mode is ProcessorMode.FOLLOWER:
        follower_barrier_node_ids = frozenset(graph.get_aggregation_id_map().values())
    else:
        typed_aggregation_settings = {NodeID(k): v for k, v in config.aggregation_settings.items()}

    # RowProcessor still carries a run-level source view for legacy helper
    # surfaces. Per-row processing passes the active source explicitly; a
    # follower performs no source ingest (source_plugin=None) but still needs
    # source_on_success for the step_resolver/COMPLETED-routing closure.
    first_source_name, first_source = next(iter(config.sources.items()))
    first_source_on_success = first_source.on_success
    if first_source_on_success is None:
        raise OrchestrationInvariantError(
            f"Source '{first_source_name}' reached RowProcessor construction before on_success was injected. "
            "Sources must be constructed through the runtime factory bridge before execution."
        )

    # ADR-030 §A.1: the registered worker identity IS the scheduler
    # lease_owner. The follower passes its registered worker_id explicitly;
    # the leader's identity rides its coordination token. When neither was
    # threaded (repository-level test construction), RowProcessor falls back
    # to its own row-processor:{run_id}:{uuid} mint.
    resolved_lease_owner = scheduler_lease_owner
    if resolved_lease_owner is None and coordination_token is not None:
        resolved_lease_owner = coordination_token.worker_id

    processor = RowProcessor(
        execution=factory.execution,
        data_flow=factory.data_flow,
        span_factory=span_factory,
        run_id=run_id,
        source_node_id=source_id,
        source_on_success=first_source_on_success,
        source_plugin=None if mode is ProcessorMode.FOLLOWER else first_source,
        edge_map=edge_map,
        route_resolution_map=route_resolution_map,
        traversal=traversal,
        aggregation_settings=typed_aggregation_settings,
        retry_manager=retry_manager,
        coalesce_executor=coalesce_executor,
        branch_to_coalesce=branch_to_coalesce,
        row_union_executor=row_union_executor,
        branch_to_row_union=branch_to_row_union,
        collector_executor=collector_executor,
        collector_on_success_map=collector_on_success_map,
        group_bindings=group_bindings,
        branch_to_sink=branch_to_sink,
        unbound_branch_first_node=unbound_branch_first_node,
        sink_names=frozenset(config.sinks),
        coalesce_on_success_map=coalesce_on_success_map,
        barrier_restore=barrier_restore,
        barrier_restore_reads=factory.barrier_restore,
        payload_store=payload_store,
        clock=clock,
        max_workers=max_workers,
        telemetry_manager=telemetry,
        scheduler=factory.scheduler,
        scheduler_lease_owner=resolved_lease_owner,
        scheduler_lease_seconds=scheduler_lease_seconds,
        scheduler_heartbeat_seconds=scheduler_heartbeat_seconds,
        coordination_token=coordination_token,
        # §C.2 path 1 (slice 4): leader housekeeping sweep — evict dead
        # non-leader members then reap their expired item leases. None for a
        # follower or tokenless direct construction; absence never selects
        # unfenced recovery, and production leader maintenance requires token.
        run_coordination=factory.run_coordination if coordination_token is not None else None,
        follower_barrier_node_ids=follower_barrier_node_ids,
        mode=mode,
    )

    return processor, coalesce_node_map, coalesce_executor
