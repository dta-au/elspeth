# tests/integration/pipeline/test_rule9_error_to_closer_runtime.py
"""Spec §7 rule 9 runtime semantics — WS3 Task 9b end-to-end pin.

WS2 Task 11 (`d8da681b3`) landed rule 9's BUILD side: a transform or gate
inside a bound region may name that region's own closer (coalesce/
row_union) as `on_error`, and the builder draws a `RoutingMode.DIVERT` edge
into the closer as a structural audit marker, rejecting the out-of-region
shape. The builder comment left runtime semantics for WS3 — an N5 window
(validate-green/runtime-crash) existed until this task closed it.

Pre-flight investigation (recorded in task-9-report.md's "Task 9b"
section) found TWO independent crash layers, neither in `processor.py`:

- Layer A: `orchestrator/validation.py`'s `validate_transform_error_sinks`/
  `validate_gate_error_sinks` treated any non-"discard" `on_error` as a
  required sink name, rejecting every rule-9 config before a single row
  ran. Fixed by widening the membership test to accept a closer name too
  (`ExecutionGraph.get_error_routable_closer_names()`, threaded through
  the three preflight call sites: `preflight.py`, `resume.py`,
  `graph_registration.py`). The builder remains the sole in-region/
  out-of-region validity authority — this validator never re-derives it.
- Layer B: even past Layer A, `token_traversal.py`'s
  `handle_transform_error_status`/`handle_gate_error_outcome` built a
  sink-routed `RowResult` (`path=ON_ERROR_ROUTED`, `sink_name=on_error`)
  for ANY non-"discard" `on_error`, crashing in
  `orchestrator/outcomes.py::_route_to_sink` when the name wasn't a real
  sink. Both handlers already called `_settle_member_losses` correctly
  and unconditionally beforehand — that machinery needed no change.

  First fix attempt classified `error_sink` against
  `self._processor._group_bindings.is_error_routable_closer(...)` and
  landed a closer match on `(FAILURE, UNROUTED)` — the established
  "consumed as a lost GROUP member" path every settlement-seam consumer
  since Task 9 uses. Measurement showed this flips the transform arm's
  overall `RunStatus` (`FAILED` vs `COMPLETED_WITH_FAILURES`) against the
  omitted-on_error ("discard") twin, because `rows_quarantined` counts
  only `QUARANTINED_AT_SOURCE`, never `UNROUTED`
  (`orchestrator/counter_classification.py`), which feeds
  `derive_terminal_run_status`'s `terminal_clean_indicator` predicate.
  RULING 50 (supersedes the first attempt): spec §7 rule 9 requires the
  explicit route to settle "exactly as the omitted-on_error twin does" —
  that includes the terminal PATH, not just `group_losses`. The fix is
  now a WIDENED CONDITION on the existing discard/discarded branch (`if
  error_sink == "discard" or is_error_routable_closer(error_sink):` for
  transforms; the gate twin analogously) rather than a parallel branch —
  a rule-9 closer route takes the IDENTICAL code path as a plain discard:
  same `branch_loss_reason` ("quarantined"/"max_retries_exceeded" for
  transforms, "gate_error_discarded" for gates), same terminal path
  (`QUARANTINED_AT_SOURCE`/`GATE_ERROR_DISCARDED`), same `group_losses`
  row, same `RunStatus`. The DIVERT edge into the closer remains a pure
  STRUCTURAL AUDIT MARKER (WS2 Task 11's build side, recorded separately
  via `record_routing_event`) — nothing about the SETTLEMENT disposition
  distinguishes an explicit route from an omitted one; only the audit
  trail's `routing_event` row does.

Collector closers are DELIBERATELY EXCLUDED from both layers (WS4 Task
8-12's parity sweep, not this one's) — see the trigger comments at
`ExecutionGraph.get_error_routable_closer_names()` and
`GroupBindingRegistry.is_error_routable_closer()`.

Under Ruling 50 there is NO remaining observable divergence: `group_losses`
rows, per-token (outcome, path) pairs, closer verdicts, and
`derive_terminal_run_status`'s `RunStatus` are all byte-identical between
the explicit-route and omitted-on_error pipelines — pinned below."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select

from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import group_losses_table, token_outcomes_table, token_work_items_table
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
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager

_INPUT_CSV = "id,value\n1,10\n2,20\n3,30\n"


def _build_and_run(settings_yaml: str, tmp_path: Path, *, input_csv: str = _INPUT_CSV) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """Build+run a settings YAML through the real production path.

    Same shape as Task 9's `_build_and_run`
    (test_nested_group_settlement.py) — returns the audit DB's file path
    rather than an open ``LandscapeDB``.
    """
    input_csv_path = tmp_path / "input.csv"
    input_csv_path.write_text(input_csv)
    output_path = tmp_path / "output.jsonl"

    formatted = settings_yaml.format(input_path=input_csv_path, output_path=output_path)
    settings = load_settings_from_yaml_string(formatted)
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
        row_union_settings=list(settings.row_unions) if settings.row_unions else None,
        queues=settings.queues,
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes) if settings.scopes else None,
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
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=payload_store,
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    finally:
        db.close()

    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()] if output_path.exists() else []
    return db_path, result.to_dict(), output_rows


@dataclass
class PipelineResult:
    """Thin SQL-read probe over one run's audit DB (Task 9's own style)."""

    db_path: Path
    result_data: dict[str, Any]
    output_rows: list[dict[str, Any]]

    def _connect(self):  # sqlalchemy Connection context manager, local test helper
        return create_engine(f"sqlite:///{self.db_path}").connect()

    def group_losses(self) -> list[tuple[str, str, str]]:
        """Sorted (closer_name, member_key, reason) triples — the
        cross-pipeline comparison key (group_id is per-run random, never
        compared across pipelines)."""
        with self._connect() as conn:
            rows = conn.execute(select(group_losses_table)).mappings().all()
        return sorted((row["closer_name"], row["member_key"], row["reason"]) for row in rows)

    def token_ids_with_path(self, path: str) -> set[str]:
        """Token ids whose recorded terminal path is ``path`` — the per-run
        identity half of a member-loss pin (member keys are run-scoped)."""
        with self._connect() as conn:
            rows = conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.path == path)).all()
        return {row.token_id for row in rows}

    def token_outcome_paths(self) -> list[tuple[str, str]]:
        """Sorted (outcome, path) pairs over every recorded token_outcomes row."""
        with self._connect() as conn:
            rows = conn.execute(select(token_outcomes_table.c.outcome, token_outcomes_table.c.path)).all()
        return sorted((row.outcome, row.path) for row in rows)

    def closer_work_items(self, node_id_like: str) -> Counter[tuple[str, str]]:
        """Set-equality pin (spec §7 rule 9 brief): every (node_id, status)
        work-item row whose node_id CONTAINS ``node_id_like`` (node ids are
        content-hash-suffixed, e.g. ``coalesce_merge_c2005cad0813``) — as a
        MULTISET (9b review finding 1): a plain set deduped a pseudo-member
        row whose status already appeared, so an injected extra row at the
        closer passed unseen; counts make any extra row change the value.
        (token_id itself cannot join a cross-run twin comparison — it is
        run-scoped — so the per-token half of the pin lives in
        ``failed_token_ids_with_work_items_at``.)"""
        with self._connect() as conn:
            rows = conn.execute(select(token_work_items_table.c.node_id, token_work_items_table.c.status)).all()
        return Counter((row.node_id, row.status) for row in rows if node_id_like in row.node_id)

    def error_route_token_ids_with_work_items_at(self, node_id_like: str, *, error_paths: frozenset[str]) -> set[str]:
        """Per-run structural pin (9b review finding 1): token_ids whose
        terminal PATH is one of ``error_paths`` (the error-disposed tokens —
        quarantined_at_source for a transform's on_error, gate_error_discarded
        for a gate's) AND that own a work-item row at the matching node.
        Must be EMPTY at the closer: an error-disposed token arriving there
        as a pseudo-member via the DIVERT edge is exactly the rule-9
        violation the brief demands this module catch. Held sibling members
        (FAILURE/UNROUTED) legitimately hold closer work items — they
        arrived as REAL members before the group failed — so the filter is
        on the error-route paths, not on failure outcomes generally."""
        with self._connect() as conn:
            error_disposed = {
                row.token_id
                for row in conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.path.in_(error_paths))).all()
            }
            item_rows = conn.execute(select(token_work_items_table.c.token_id, token_work_items_table.c.node_id)).all()
        return {row.token_id for row in item_rows if node_id_like in row.node_id and row.token_id in error_disposed}


# ===== Transform arm: explicit on_error: <closer> vs the omitted ("discard") twin =====

_TRANSFORM_EXPLICIT_YAML = """
sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
gates:
  - name: split
    input: fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [a, b]
transforms:
  - name: err_a
    plugin: dag_corpus_branch_loss
    input: a
    on_success: a_out
    on_error: merge
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge
    branches: {{a: a_out, b: b}}
    policy: require_all
    merge: union
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_TRANSFORM_OMITTED_YAML = _TRANSFORM_EXPLICIT_YAML.replace("on_error: merge", "on_error: discard")


def test_transform_explicit_on_error_to_closer_settles_like_the_omitted_twin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The N5 equivalence pin: a transform naming its own enclosing
    require_all coalesce via on_error settles IDENTICALLY (same
    group_losses closer/member/reason, same held-sibling terminal path,
    same per-token (outcome, path) pairs — Ruling 50, zero divergence) to
    the same topology with on_error: discard."""
    install_corpus_plugin_manager(monkeypatch)

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    db_path, result_data, _ = _build_and_run(_TRANSFORM_EXPLICIT_YAML, explicit_dir)
    explicit = PipelineResult(db_path=db_path, result_data=result_data, output_rows=[])

    omitted_dir = tmp_path / "omitted"
    omitted_dir.mkdir()
    db_path, result_data, _ = _build_and_run(_TRANSFORM_OMITTED_YAML, omitted_dir)
    omitted = PipelineResult(db_path=db_path, result_data=result_data, output_rows=[])

    # group_losses: byte-identical closer/member/reason.
    assert explicit.group_losses() == omitted.group_losses()
    assert explicit.group_losses()  # non-empty: the loss actually fired

    # Ruling 50: every token's (outcome, path) pair is byte-identical
    # between the two pipelines, including the directly-failing token
    # ('a') — QUARANTINED_AT_SOURCE in BOTH, not UNROUTED for the
    # explicit route. The held sibling ('b') is (FAILURE, UNROUTED) in
    # both (consumed via the SAME _settle_member_losses seam regardless
    # of which arm triggered it).
    explicit_paths = explicit.token_outcome_paths()
    omitted_paths = omitted.token_outcome_paths()
    assert explicit_paths == omitted_paths
    assert explicit_paths.count(("failure", "quarantined_at_source")) == 3  # 3 rows x directly-failing 'a'
    assert explicit_paths.count(("failure", "unrouted")) == 3  # 3 rows x held 'b'

    # Ruling 50: RunStatus itself is byte-identical — the measured
    # divergence (FAILED vs COMPLETED_WITH_FAILURES) that triggered this
    # ruling is now closed.
    assert explicit.result_data["status"] == omitted.result_data["status"]
    assert explicit.result_data["rows_quarantined"] == omitted.result_data["rows_quarantined"] == 3
    assert explicit.result_data["rows_failed"] == omitted.result_data["rows_failed"] == 6

    # Closer verdict + structural pin (spec §7 rule 9 brief: set equality
    # over the FULL work-item ledger at the closer node, not
    # absence-by-sampling). The coalesce node itself is one graph node
    # reused across all 3 rows, so its (node_id, status) set naturally
    # collapses to one member when every row reaches the same terminal
    # disposition — this IS the pin: the error route contributes NOTHING
    # the omitted twin's set doesn't already have, so the two sets are
    # exactly equal, and neither contains a work-item row for the FAILING
    # TRANSFORM's own node (only the coalesce node's own barrier
    # bookkeeping — the failing token never travels the DIVERT edge as a
    # live work item).
    explicit_closer_items = explicit.closer_work_items("coalesce_merge")
    omitted_closer_items = omitted.closer_work_items("coalesce_merge")
    assert explicit_closer_items == omitted_closer_items == Counter({("coalesce_merge_c2005cad0813", "terminal"): 3})
    # Per-run half of the pin (9b review finding 1): no ERROR-DISPOSED token
    # owns a work item at the closer — the pseudo-member-via-error-route check.
    _tx_error_paths = frozenset({"quarantined_at_source"})
    assert explicit.error_route_token_ids_with_work_items_at("coalesce_merge", error_paths=_tx_error_paths) == set()
    assert omitted.error_route_token_ids_with_work_items_at("coalesce_merge", error_paths=_tx_error_paths) == set()


# ===== Collector arm (integration item 18, rule-9 parity): the same twin, over a scope =====

_COLLECTOR_EXPLICIT_YAML = """
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
  - name: err_page
    plugin: dag_corpus_branch_loss
    input: pages
    on_success: checked_pages
    on_error: page_stitcher
    options:
      schema: {{mode: observed}}
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: checked_pages
    on_success: output
    on_error: discard
    options:
      value_field: item
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_COLLECTOR_OMITTED_YAML = _COLLECTOR_EXPLICIT_YAML.replace("on_error: page_stitcher", "on_error: discard")

_COLLECTOR_INPUT_JSONL = json.dumps({"id": 1, "items": [3, 1, 2]}) + "\n" + json.dumps({"id": 2, "items": [5, 7]}) + "\n"


def _build_and_run_jsonl(settings_yaml: str, tmp_path: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """The collector twin needs a LIST-bearing input (json_explode refuses
    strings), so its source is JSONL; otherwise identical to _build_and_run."""
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text(_COLLECTOR_INPUT_JSONL)
    return _build_and_run(settings_yaml.replace("{input_path}", str(input_path)), tmp_path)


def test_transform_explicit_on_error_to_collector_settles_like_the_omitted_twin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 18 parity: a transform INSIDE a scope naming the scope's own
    collector via on_error settles identically to the on_error: discard
    twin — every member is error-disposed (quarantined_at_source), every
    loss reaches the collector through the settle seam, the require_all
    group fails at closure, the plugin never runs, and no error-disposed
    token ever travels the DIVERT edge as a live work item. group_losses are
    compared as (closer, reason) multisets: EXPAND member keys are the
    members' run-scoped token ids and cannot join a cross-run comparison."""
    install_corpus_plugin_manager(monkeypatch)

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    db_path, result_data, explicit_rows = _build_and_run_jsonl(_COLLECTOR_EXPLICIT_YAML, explicit_dir)
    explicit = PipelineResult(db_path=db_path, result_data=result_data, output_rows=explicit_rows)

    omitted_dir = tmp_path / "omitted"
    omitted_dir.mkdir()
    db_path, result_data, omitted_rows = _build_and_run_jsonl(_COLLECTOR_OMITTED_YAML, omitted_dir)
    omitted = PipelineResult(db_path=db_path, result_data=result_data, output_rows=omitted_rows)

    def closer_and_reason(result: PipelineResult) -> Counter[tuple[str, str]]:
        return Counter((closer, reason) for closer, _member_key, reason in result.group_losses())

    assert closer_and_reason(explicit) == closer_and_reason(omitted) == Counter({("page_stitcher", "quarantined"): 5})
    # Per-run member identity (C7 review M-1): the five losses name five
    # DISTINCT members, and they are exactly the tokens that were
    # error-disposed — a loss staged against the wrong member (or one member
    # five times) is a settlement bug the cross-run multiset cannot see.
    for result in (explicit, omitted):
        member_keys = [member_key for _closer, member_key, _reason in result.group_losses()]
        assert len(set(member_keys)) == 5
        assert set(member_keys) == result.token_ids_with_path("quarantined_at_source")

    explicit_paths = explicit.token_outcome_paths()
    omitted_paths = omitted.token_outcome_paths()
    assert explicit_paths == omitted_paths
    assert explicit_paths.count(("failure", "quarantined_at_source")) == 5  # every member, directly failing
    assert explicit_rows == omitted_rows == []  # the collector never flushed

    assert explicit.result_data["status"] == omitted.result_data["status"]
    assert explicit.result_data["rows_quarantined"] == omitted.result_data["rows_quarantined"] == 5

    # No member ever reached the collector as a live work item in either
    # twin (they were error-disposed upstream), and no error-disposed token
    # owns a work item at the closer — the pseudo-member-via-error-route
    # check, now for the collector kind.
    assert explicit.closer_work_items("collector_page_stitcher") == omitted.closer_work_items("collector_page_stitcher") == Counter()
    _collector_error_paths = frozenset({"quarantined_at_source"})
    assert explicit.error_route_token_ids_with_work_items_at("collector_page_stitcher", error_paths=_collector_error_paths) == set()
    assert omitted.error_route_token_ids_with_work_items_at("collector_page_stitcher", error_paths=_collector_error_paths) == set()


# ===== Gate arm: same equivalence pin, over handle_gate_error_outcome =====

_GATE_EXPLICIT_YAML = """
sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
gates:
  - name: split
    input: fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [a, b]
  - name: err_gate
    input: a
    condition: "row['nonexistent_field'] > 5"
    routes: {{"true": a_out, "false": discard}}
    on_error: merge
coalesce:
  - name: merge
    branches: {{a: a_out, b: b}}
    policy: require_all
    merge: union
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_GATE_OMITTED_YAML = _GATE_EXPLICIT_YAML.replace("on_error: merge", "on_error: discard")


def test_gate_explicit_on_error_to_closer_settles_like_the_omitted_twin(tmp_path: Path) -> None:
    """Same pin as the transform arm, over `handle_gate_error_outcome`'s
    DIVERT branch — the gate's own condition-evaluation failure (an
    undefined row field, raised at RUNTIME, not statically rejected) names
    its enclosing coalesce via on_error."""
    explicit_dir = tmp_path / "gate-explicit"
    explicit_dir.mkdir()
    db_path, result_data, _ = _build_and_run(_GATE_EXPLICIT_YAML, explicit_dir)
    explicit = PipelineResult(db_path=db_path, result_data=result_data, output_rows=[])

    omitted_dir = tmp_path / "gate-omitted"
    omitted_dir.mkdir()
    db_path, result_data, _ = _build_and_run(_GATE_OMITTED_YAML, omitted_dir)
    omitted = PipelineResult(db_path=db_path, result_data=result_data, output_rows=[])

    # group_losses: byte-identical closer/member/reason — the gate's
    # "discarded" branch already used "gate_error_discarded", and the
    # closer-route arm deliberately reuses that same reason (mirrors the
    # transform arm's "quarantined" reuse).
    assert explicit.group_losses() == omitted.group_losses()
    assert explicit.group_losses() == [("merge", "a", "gate_error_discarded")] * 3

    # Ruling 50: byte-identical (outcome, path) pairs, including the
    # directly-failing gate token ('a') — GATE_ERROR_DISCARDED in BOTH.
    explicit_paths = explicit.token_outcome_paths()
    omitted_paths = omitted.token_outcome_paths()
    assert explicit_paths == omitted_paths
    assert explicit_paths.count(("failure", "gate_error_discarded")) == 3  # 3 rows x directly-failing 'a'
    assert explicit_paths.count(("failure", "unrouted")) == 3  # 3 rows x held 'b'

    assert explicit.result_data["status"] == omitted.result_data["status"]
    assert explicit.result_data["rows_failed"] == omitted.result_data["rows_failed"] == 6

    explicit_closer_items = explicit.closer_work_items("coalesce_merge")
    omitted_closer_items = omitted.closer_work_items("coalesce_merge")
    assert explicit_closer_items == omitted_closer_items == Counter({("coalesce_merge_c2005cad0813", "terminal"): 3})
    _gate_error_paths = frozenset({"gate_error_discarded"})
    assert explicit.error_route_token_ids_with_work_items_at("coalesce_merge", error_paths=_gate_error_paths) == set()
    assert omitted.error_route_token_ids_with_work_items_at("coalesce_merge", error_paths=_gate_error_paths) == set()


# ===== Out-of-region control: build rejection must still fire (T9 lesson: match=) =====

_OUT_OF_REGION_YAML = """
sources:
  primary:
    plugin: csv
    on_success: fork_a_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
gates:
  - name: gate_a
    input: fork_a_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [a1, a2]
  - name: gate_b
    input: merge_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [b1, b2]
transforms:
  - name: a1_transform
    plugin: value_transform
    input: a1
    on_success: a1_out
    on_error: merge_b
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'a1'"}}]
coalesce:
  - name: merge_a
    branches: {{a1: a1_out, a2: a2}}
    policy: require_all
    merge: union
  - name: merge_b
    branches: {{b1: b1, b2: b2}}
    policy: require_all
    merge: union
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""


def test_out_of_region_on_error_to_closer_still_rejected_at_build(tmp_path: Path) -> None:
    """Consumed-contract pin on WS2's rule 9 (spec §7): a real closer name
    that is NOT the transform's own enclosing region's closer is rejected
    at build time — this task never relaxes region-validity, only widens
    what a validated in-region name is permitted to do at runtime."""
    with pytest.raises(GraphValidationError, match="on_error 'merge_b' names closer"):
        _build_and_run(_OUT_OF_REGION_YAML, tmp_path)
