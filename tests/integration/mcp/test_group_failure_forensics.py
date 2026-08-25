"""WS6 acceptance: nested-group failure forensics via the MCP analyzer alone.

Spec §9 row 6 acceptance criterion — from a failed depth-3 nested group, an
operator reconstructs from audit rows via the landscape MCP tools alone:
which member failed, with what reason, through which escalation chain, and
why each survivor terminated. Every assertion below reads ONLY
``LandscapeAnalyzer`` methods — the MCP tool handlers' exact targets — over
a file-backed audit database the analyzer opens read-only after the run.

Topology (depth 3, all ``require_all``), built through the REAL production
settings path exactly as ``test_collector_happy_path.py`` builds its
pipeline (META-29: no ``PipelineConfig`` collector/scope field — node ids
come off the graph, settings off ``ElspethSettings.collectors``/``.scopes``):

    source (1 row)
      -> outer scope ``document_pages`` (opener ``explode_pages``, 2 members)
         -> gate ``section_fork`` -> branches ``analysis`` / ``summary``
            ``analysis``: inner scope ``sentence_scope`` (opener
              ``explode_sentences``, 2 members) -> ``sentence_probe``, a
              ``value_transform`` whose expression divides by zero for
              exactly (page 0, sentence 1), with ``on_error: discard`` ->
              closer collector ``sentence_stitcher``
            ``summary``: passthrough
         -> coalesce ``section_merge`` (closes the fork; consumed by name)
      -> closer collector ``page_stitcher`` -> sink

Expected settlement (spec §6.3): the discarded sentence stages a
``quarantined`` loss at ``sentence_stitcher``; the inner group FAILs and —
structurally, because an enclosing bound frame exists (ADR-042; no config
field selects the arm) — stages a ``group_failed`` loss against fork
branch ``analysis`` at ``section_merge``; the coalesce FAILs and escalates a
``group_failed`` loss against page 0's member at ``page_stitcher``; the outer
group FAILs and, being outermost, handles it terminally (quarantine) — the run
terminates by SETTLEMENT as ``COMPLETED_WITH_FAILURES``. Survivors (sentence
0, branch ``summary``'s token, page 1's whole subtree) terminate
``scope_group_failed``.

The reads go through WS6 Task 7's dedicated analyzer surface
(``list_group_losses`` / ``list_group_records`` / ``get_token_lineage``,
landed 999279811) plus the pre-existing ``list_tokens``,
``get_node_states(include_context=True)`` and ``explain_token`` — every one
an MCP tool handler's exact target.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from elspeth.contracts.enums import FrameKind, GroupSettlementReason, RunStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import runs_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id

_SCOPE_GROUP_FAILED = GroupSettlementReason.SCOPE_GROUP_FAILED.value  # the persisted disposition (spec §6.3)

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
  - name: explode_pages
    plugin: json_explode
    input: rows
    on_success: pages_in
    on_error: discard
    options:
      array_field: pages
      output_field: page
      include_index: false
      schema: {{mode: observed}}
  - name: explode_sentences
    plugin: json_explode
    input: analysis
    on_success: sentences_in
    on_error: discard
    options:
      array_field: sentences
      output_field: sentence
      include_index: false
      schema: {{mode: observed}}
  - name: sentence_probe
    plugin: value_transform
    input: sentences_in
    on_success: stitch_in
    on_error: discard
    options:
      operations:
        - target: marker
          expression: "1 / (row['page'] * 2 + row['sentence'] - 1)"
      schema: {{mode: observed}}
  - name: summarize
    plugin: passthrough
    input: summary
    on_success: merge_summary
    on_error: discard
    options:
      schema: {{mode: observed}}
gates:
  - name: section_fork
    input: pages_in
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [analysis, summary]
coalesce:
  - name: section_merge
    branches: {{analysis: stitch_out, summary: merge_summary}}
    policy: require_all
    merge: nested
collectors:
  - name: sentence_stitcher
    plugin: batch_stats
    input: stitch_in
    on_success: stitch_out
    on_error: discard
    options:
      value_field: sentence
      schema: {{mode: observed}}
  - name: page_stitcher
    plugin: batch_stats
    input: section_merge
    on_success: out
    on_error: discard
    options:
      value_field: page
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode_pages
    closer: page_stitcher
    policy: require_all
  - name: sentence_scope
    opener: explode_sentences
    closer: sentence_stitcher
    policy: require_all
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_DOCUMENT = {"id": 1, "pages": [0, 1], "sentences": [0, 1]}


def _run_depth3_failure(tmp_path: Path) -> tuple[str, str]:
    """Run the depth-3 pipeline with one innermost member failing.

    Returns ``(database_url, run_id)``. Built with the real Orchestrator
    against a file-backed LandscapeDB so ``LandscapeAnalyzer`` (URL-opened,
    read-only) can attach afterwards — the operator's situation.
    """
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text(json.dumps(_DOCUMENT) + "\n")
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
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
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
    database_url = f"sqlite:///{tmp_path / 'forensics.db'}"
    db = LandscapeDB(database_url)
    try:
        Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    finally:
        db.close()
    engine = create_engine(database_url)
    with engine.connect() as conn:
        run_row = conn.execute(select(runs_table)).one()
    engine.dispose()
    # COMPLETED-family, ratified: an outermost group's failure is terminal
    # (structural quarantine, ADR-042) and thereby a
    # HANDLED failure, so the run terminates by SETTLEMENT — and with this
    # run's only source row quarantined the exact family member is
    # COMPLETED_WITH_FAILURES. FAILED here means the construction crashed
    # instead of settling: a fixture bug, never forensics data.
    assert RunStatus(run_row.status) is RunStatus.COMPLETED_WITH_FAILURES, run_row.status
    return database_url, str(run_row.run_id)


def _frames(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(frame) for frame in token["lineage_path"]]


def test_depth3_group_failure_is_reconstructible_from_mcp_tools_alone(tmp_path: Path) -> None:
    database_url, run_id = _run_depth3_failure(tmp_path)
    analyzer = LandscapeAnalyzer(database_url)
    try:
        # --- 1. Which member failed, with what reason. ---
        losses = analyzer.list_group_losses(run_id)
        assert len(losses) == 3, f"expected the 3-level escalation chain, got {losses}"
        by_closer = {loss["closer_name"]: loss for loss in losses}
        assert set(by_closer) == {"sentence_stitcher", "section_merge", "page_stitcher"}
        innermost = by_closer["sentence_stitcher"]
        assert innermost["reason"] == "quarantined"  # the discarded member's own categorical reason
        assert by_closer["section_merge"]["reason"] == "group_failed"
        assert by_closer["page_stitcher"]["reason"] == "group_failed"
        failing_token = innermost["token_id"]

        # --- 2. The escalation chain, linked through the failing token's path. ---
        tokens = {token["token_id"]: token for token in analyzer.list_tokens(run_id, limit=500)}
        path = analyzer.get_token_lineage(run_id, failing_token)  # outermost first
        assert [frame["kind"] for frame in path] == [
            FrameKind.EXPAND.value,  # outer scope member (page 0)
            FrameKind.FORK.value,  # branch "analysis"
            FrameKind.EXPAND.value,  # inner scope member (the sentence)
        ]
        # Each escalation loss names the NEXT frame outward of the same path.
        assert by_closer["sentence_stitcher"]["group_id"] == path[2]["group_id"]
        assert by_closer["sentence_stitcher"]["member_key"] == path[2]["member_key"]
        assert by_closer["section_merge"]["group_id"] == path[1]["group_id"]
        assert by_closer["section_merge"]["member_key"] == "analysis"
        assert by_closer["page_stitcher"]["group_id"] == path[0]["group_id"]
        assert by_closer["page_stitcher"]["member_key"] == path[0]["member_key"]
        # The failing member itself: the erroring transform is on its trail.
        explained = analyzer.explain_token(run_id, token_id=failing_token)
        assert "error" not in explained, f"explain_token returned an error: {explained}"
        explained_states = explained["node_states"]
        assert any("sentence_probe" in state["node_id"] for state in explained_states), explained_states

        # --- 3. The rosters those groups were accountable to. ---
        group_records = {record["group_id"]: record for record in analyzer.list_group_records(run_id)}
        assert group_records[path[0]["group_id"]]["member_count"] == 2  # pages
        assert group_records[path[2]["group_id"]]["member_count"] == 2  # sentences

        # --- 4. Why each survivor terminated: scope_group_failed. ---
        states = analyzer.get_node_states(run_id, limit=1000, include_context=True)
        states_by_token: dict[str, list[dict[str, Any]]] = {}
        for state in states:
            states_by_token.setdefault(state["token_id"], []).append(dict(state))

        def _member_disposition(state: dict[str, Any]) -> str | None:
            """The survivor's disposition as the hold's FAILED node_state records
            it (META-40: the cause rides the survivor's hold). Two writer shapes:
            the collector executor nests it under ``error.context``, the
            coalesce executor writes it at ``error`` top level."""
            error = state.get("error")
            if not isinstance(error, dict):
                return None
            direct = error.get("member_disposition")
            if isinstance(direct, str):
                return direct
            context = error.get("context")
            if isinstance(context, dict) and isinstance(context.get("member_disposition"), str):
                return str(context["member_disposition"])
            return None

        def _terminated_scope_group_failed(token_id: str) -> bool:
            return any(
                state["status"] == "failed" and _member_disposition(state) == _SCOPE_GROUP_FAILED
                for state in states_by_token.get(token_id, [])
            )

        # (a) The failing sentence's sibling inside the inner group.
        inner_survivors = [
            token_id
            for token_id, token in tokens.items()
            if any(frame["group_id"] == path[2]["group_id"] for frame in _frames(token)) and token_id != failing_token
        ]
        assert inner_survivors, "the inner group must have a surviving sibling"
        # (b) Branch "summary"'s token inside page 0's fork.
        summary_survivors = [
            token_id
            for token_id, token in tokens.items()
            if any(frame["group_id"] == path[1]["group_id"] and frame["member_key"] == "summary" for frame in _frames(token))
        ]
        assert summary_survivors, "the fork must have a summary-branch survivor"
        # (c) Page 1's subtree in the outer group: every token carrying the
        # outer group's frame under the OTHER member key.
        page1_survivors = [
            token_id
            for token_id, token in tokens.items()
            if any(frame["group_id"] == path[0]["group_id"] and frame["member_key"] != path[0]["member_key"] for frame in _frames(token))
        ]
        assert page1_survivors, "page 1's subtree must exist"
        for token_id in (*inner_survivors, *summary_survivors):
            assert _terminated_scope_group_failed(token_id), (
                f"survivor {token_id} must carry a {_SCOPE_GROUP_FAILED} disposition in its audit trail: {states_by_token.get(token_id)}"
            )
        # Page 1's subtree collapses into ONE arrival at page_stitcher (its
        # coalesce released a merged token); at least one token in the
        # subtree must carry the outer group's failure disposition.
        assert any(_terminated_scope_group_failed(token_id) for token_id in page1_survivors), (
            f"page 1's subtree must terminate {_SCOPE_GROUP_FAILED} at the outer closer"
        )
    finally:
        analyzer.close()
