"""Item 11 — the first real end-to-end collector run (integration phase 1, C6).

Source (JSONL) -> json_explode (the scope opener) -> batch_stats collector
(require_all) -> sink, through the REAL production build+run path. The six
oracle assertions, none optional: the plugin was invoked exactly once per
group; rows reach it in opener-ordinal order; one released token per output;
every member hold completed; a group_records row exists for each release
group; run status COMPLETED. COMPLETED alone is the vacuous pass (a collector
pipeline can finalize clean with the plugin never invoked), which is why the
invocation count and the release groups are asserted first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from elspeth.contracts.enums import FrameKind, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import group_records_table, node_states_table, token_outcomes_table, token_work_items_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id

_SETTINGS_YAML = """
sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    on_error: discard
    options:
      value_field: item
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
    on_group_failure: quarantine
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

# Two source rows -> two EXPAND groups of 3 and 2 members. The item values
# are deliberately NOT sorted, so "ordinal order" is observable as the
# authored list order, not a numeric coincidence.
_DOCUMENTS = [{"id": 1, "items": [3, 1, 2]}, {"id": 2, "items": [5, 7]}]


def _build_and_run(tmp_path: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
    output_path = tmp_path / "output.jsonl"
    settings = load_settings_from_yaml_string(_SETTINGS_YAML.format(input_path=input_path, output_path=output_path))
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=SinkEffectExecutionPurpose.FRESH,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes),
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    graph.validate()
    graph.validate_edge_compatibility()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    db_path = tmp_path / "audit.db"
    db = LandscapeDB(f"sqlite:///{db_path}")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    finally:
        db.close()
    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()] if output_path.exists() else []
    return db_path, result.to_dict(), output_rows


def test_collector_pipeline_runs_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    invocations: list[list[int]] = []
    original_process = BatchStats.process

    def counting_process(self: BatchStats, rows: Any, ctx: Any) -> Any:
        invocations.append([row["item"] for row in rows])
        return original_process(self, rows, ctx)

    monkeypatch.setattr(BatchStats, "process", counting_process)

    db_path, result_data, output_rows = _build_and_run(tmp_path)

    # 1 + 2. The plugin ran exactly once per group, over the members in
    # opener-ordinal order (the authored list order; the executor's own unit
    # pin, test_flush_orders_members_by_opener_ordinal_not_arrival_order, is
    # the arrival-vs-ordinal discriminator — a single-worker run arrives in
    # ordinal order by construction).
    assert invocations == [[3, 1, 2], [5, 7]]

    # 6. Run status COMPLETED.
    assert result_data["status"] == RunStatus.COMPLETED.value

    # 3. One released token per output: one stats row per group at the sink.
    assert [(row["count"], row["sum"]) for row in output_rows] == [(3, 6), (2, 12)]

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        groups = conn.execute(
            select(group_records_table.c.kind, group_records_table.c.member_count, group_records_table.c.opener_token_id)
        ).all()
        outcomes = conn.execute(
            select(
                token_outcomes_table.c.token_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.sink_name,
            )
        ).all()
        holds = conn.execute(select(node_states_table.c.node_id, node_states_table.c.token_id, node_states_table.c.completed_at)).all()
        journal = conn.execute(
            select(token_work_items_table.c.status, token_work_items_table.c.barrier_key, token_work_items_table.c.collector_name)
        ).all()

    # 5. A group_records row for each EXPAND group AND for each release
    # group (collect_tokens mints the release durably, opened by the
    # representative member).
    expand_groups = [g for g in groups if g.kind == FrameKind.EXPAND.value]
    assert sorted(g.member_count for g in expand_groups) == [1, 1, 2, 3]
    member_terminals = [(o.outcome, o.path, o.sink_name) for o in outcomes if o.path == TerminalPath.COALESCED.value]
    released_terminals = [(o.outcome, o.path, o.sink_name) for o in outcomes if o.path == TerminalPath.DEFAULT_FLOW.value]
    assert member_terminals == [(TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value, None)] * 5
    assert released_terminals == [(TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, "out")] * 2
    release_openers = {g.opener_token_id for g in expand_groups if g.member_count in (1,)}
    assert release_openers <= {o.token_id for o in outcomes if o.path == TerminalPath.COALESCED.value}

    # 4. Every member hold at the collector node completed (5 member holds
    # plus the 2 opener-anchored flush guards), and no BLOCKED journal row
    # survives the run.
    collector_holds = [h for h in holds if h.node_id.startswith("collector")]
    assert len(collector_holds) == 7
    assert all(h.completed_at is not None for h in collector_holds)
    assert [j for j in journal if j.status == TokenWorkStatus.BLOCKED.value] == []
    assert sorted({j.collector_name for j in journal if j.collector_name is not None}) == ["page_stitcher"]
    assert all(
        j.barrier_key.startswith("collector:page_stitcher:") for j in journal if j.barrier_key is not None and j.collector_name is not None
    )

    # META-32: members are consumed inputs, the release counts once.
    assert (result_data["rows_succeeded"], result_data["rows_coalesced"]) == (2, 0)
