# tests/integration/pipeline/test_nested_group_settlement.py
"""§6.1 items 3-4 end-to-end: a bound-enclosed closer failure defers its
verdict to the enclosing closer (one escalated loss, one drain cycle after
the inner roster settles); an outermost closer failure behaves exactly as
today. Built on the real Orchestrator over ``LandscapeDB``, following
tests/integration/pipeline/test_barrier_intake_dispositions.py's builder
style and tests/integration/core/dag/test_nested_fork_coalesce.py's
build-via-real-production-path harness.

WS3 Task 9 pre-check (spec §6.1 items 3-4, brief pre-check paragraph): the
brief's ``nested_row_union_lost_branch`` fixture — a row_union closing an
OUTER fork whose one branch nests an inner region before reaching the union
— has NO buildable topology under the graph builder's current validations,
in EITHER nesting direction:

- inner region closed by a coalesce, outer closer a row_union: rejected at
  ``builder.py`` ("Fork gate(s) ... nested inside a branch that feeds
  row_union ...") — a nested fork replaces the branch identity the outer
  union needs to track, so the union group could never be satisfied. This
  applies regardless of what closes the inner region (a coalesce or another
  row_union), because the walk only asks "is there a fork gate upstream that
  isn't mine", not "what closes it".
- row_union as the INNER closer feeding an outer coalesce (even through an
  intervening ordinary transform): rejected at ``builder.py`` ("... is
  downstream of row_union ... with no intervening sink") — row_union
  releases an N-to-N group and a correlated barrier cannot consume it
  without silently dropping or double-counting arrivals.

Both rejections carry their own correctness argument in the message and are
unrelated to elspeth-a01889580f's ``get_branch_first_nodes`` row_union-loop
gap (that ticket's target shape — a row_union's OWN branch fed by a
coalesce, functionally identical to the first bullet's topology — turns out
to be the SAME build-time guard's territory, not a walker crash). See
``test_nested_row_union_branch_is_unauthorable_by_design`` below, which pins
both rejections so this finding stays load-bearing rather than a comment
that rots. Task 9's third scenario is therefore proven only for the flat
(unbound) half; the nested half has no fixture to run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select

from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    group_losses_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
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

    Mirrors ``test_nested_fork_coalesce.py``'s ``_build_and_run``, except it
    returns the audit DB's file path rather than an open ``LandscapeDB`` —
    the probes below open their own short-lived read connections against it
    after the run (and after ``Orchestrator.run`` has closed its own).
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


@dataclass(frozen=True)
class _GroupLossRow:
    closer_name: str
    group_id: str
    member_key: str
    token_id: str
    reason: str


@dataclass
class PipelineResult:
    """Thin SQL-read probe over one run's audit DB (spec §6.1 Task 9 brief:
    "thin SQL reads over group_losses / token_outcomes / token_work_items,
    written in this module")."""

    db_path: Path
    result_data: dict[str, Any]
    output_rows: list[dict[str, Any]]
    outer_closer: str | None = field(default=None)

    def _connect(self):  # sqlalchemy Connection context manager, local test helper
        return create_engine(f"sqlite:///{self.db_path}").connect()

    def ledger(self) -> list[_GroupLossRow]:
        with self._connect() as conn:
            rows = conn.execute(select(group_losses_table)).mappings().all()
        return [
            _GroupLossRow(
                closer_name=row["closer_name"],
                group_id=row["group_id"],
                member_key=row["member_key"],
                token_id=row["token_id"],
                reason=row["reason"],
            )
            for row in rows
        ]

    def escalated_losses(self) -> list[_GroupLossRow]:
        return [row for row in self.ledger() if row.reason == "group_failed"]

    @property
    def consumed_member_ids(self) -> set[str]:
        """Every token this run terminalized FAILURE — the fixtures below
        each drive exactly one failing closer, so this is the same set
        ``failure_outcomes_for_group`` resolves for that closer."""
        with self._connect() as conn:
            rows = conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.outcome == "failure")).scalars().all()
        return set(rows)

    def failure_outcomes_for_group(self, closer_name: str) -> set[str]:
        """FAILURE-outcome token ids among the members this closer's work
        items name (via ``coalesce_name``/``row_union_name``)."""
        with self._connect() as conn:
            member_ids = set(
                conn.execute(
                    select(token_work_items_table.c.token_id).where(
                        (token_work_items_table.c.coalesce_name == closer_name) | (token_work_items_table.c.row_union_name == closer_name)
                    )
                )
                .scalars()
                .all()
            )
            failed_ids = set(
                conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.outcome == "failure")).scalars().all()
            )
        return member_ids & failed_ids

    def non_terminal_tokens(self) -> list[str]:
        """Zero-write completeness: every minted token has a COMPLETED
        ``token_outcomes`` row by the time the run finishes."""
        with self._connect() as conn:
            minted = set(conn.execute(select(tokens_table.c.token_id)).scalars().all())
            completed = set(
                conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars().all()
            )
        return sorted(minted - completed)


# ===== Fixture 1: flat require_all coalesce, lost branch (ruling 19, verbatim half) =====

_FLAT_COALESCE_LOST_BRANCH_YAML = """
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
  - name: lose_a
    plugin: dag_corpus_branch_loss
    input: a
    on_success: a_out
    on_error: discard
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge_paths
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


# ===== Fixture 2: nested fork-in-fork, inner require_all quarantine =====
# Task 5/6/8's coalesce-feeds-coalesce shape (verbatim topology from
# test_nested_fork_coalesce.py's _SETTINGS_YAML_TEMPLATE), with inner_a2
# genuinely lost (require_all, not best_effort) so merge_inner FAILS
# CLOSED rather than merging a partial group.

_NESTED_COALESCE_QUARANTINE_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
transforms:
  - name: lose_inner_a2
    plugin: dag_corpus_branch_loss
    input: inner_a2
    on_success: inner_a2_out
    on_error: discard
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge_inner
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2_out}}
    policy: require_all
    merge: nested
  - name: merge_outer
    branches: {{outer_a: merge_inner, outer_b: outer_b}}
    policy: require_all
    merge: nested
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


# ===== Fixture 3: flat row_union, lost branch =====

_FLAT_ROW_UNION_LOST_BRANCH_YAML = """
sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: split
    input: fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [a, b]
transforms:
  - name: lose_a
    plugin: dag_corpus_branch_loss
    input: a
    on_success: a_out
    on_error: discard
    options:
      schema: {{mode: observed}}
  - name: finalize
    plugin: value_transform
    input: union_release
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'done'"}}]
row_unions:
  - name: union_ab
    branches: {{a: a_out, b: b}}
    on_success: union_release
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""


@dataclass(frozen=True)
class _Topology:
    yaml: str
    outer_closer: str | None = None
    # Single-row input for the nested case: the brief's pin ("one inner loss
    # row, one escalated row") is a PER-ROW cardinality statement — a 3-row
    # source would legitimately produce 3 of each, which isn't what the pin
    # means to assert.
    input_csv: str = _INPUT_CSV


_TOPOLOGIES: dict[str, _Topology] = {
    "fork_coalesce_require_all_lost_branch": _Topology(yaml=_FLAT_COALESCE_LOST_BRANCH_YAML),
    "nested_fork_coalesce_inner_quarantine": _Topology(
        yaml=_NESTED_COALESCE_QUARANTINE_YAML, outer_closer="merge_outer", input_csv="id,value\n1,10\n"
    ),
    "row_union_lost_branch_top_level": _Topology(yaml=_FLAT_ROW_UNION_LOST_BRANCH_YAML),
}


@pytest.fixture
def run_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """§E.5-style real-executor harness, keyed by topology name (spec §6.1
    Task 9 brief). Every fixture here uses ``dag_corpus_branch_loss``, so the
    corpus plugin manager is installed unconditionally."""
    install_corpus_plugin_manager(monkeypatch)

    def _run(topology_name: str) -> PipelineResult:
        topology = _TOPOLOGIES[topology_name]
        case_dir = tmp_path / topology_name
        case_dir.mkdir()
        db_path, result_data, output_rows = _build_and_run(topology.yaml, case_dir, input_csv=topology.input_csv)
        return PipelineResult(db_path=db_path, result_data=result_data, output_rows=output_rows, outer_closer=topology.outer_closer)

    return _run


def test_unbound_coalesce_failure_verbatim_today(run_pipeline) -> None:
    """Outermost require_all coalesce with a lost branch: members terminalize
    FAILURE/UNROUTED, run completes, ZERO group_failed ledger rows. Pins
    ruling 19's verbatim half against the corpus fork-coalesce-policies
    expectations."""
    result = run_pipeline("fork_coalesce_require_all_lost_branch")
    assert result.failure_outcomes_for_group("merge_paths") == result.consumed_member_ids
    assert result.consumed_member_ids  # non-empty: the loss actually fired
    assert result.escalated_losses() == []


def test_nested_inner_failure_settles_outer_member(run_pipeline) -> None:
    """fork_outer -> per-branch fork_inner -> merge_inner (require_all)
    -> merge_outer (require_all). One inner branch quarantined:
    - inner loss row (closer=merge_inner, group=fg_inner, member=<branch>);
    - after the inner roster settles, ONE escalated row
      (closer=merge_outer, group=fg_outer, member=<outer branch>, reason=group_failed);
    - merge_outer renders ITS verdict from that settled member (require_all
      => outer group fails, its members terminalize);
    - every token reaches a terminal outcome (zero-write completeness)."""
    result = run_pipeline("nested_fork_coalesce_inner_quarantine")
    inner = [loss for loss in result.ledger() if loss.closer_name == "merge_inner"]
    escalated = [loss for loss in result.ledger() if loss.reason == "group_failed"]
    assert len(inner) == 1 and len(escalated) == 1
    assert escalated[0].closer_name == "merge_outer"
    assert result.non_terminal_tokens() == []


def test_row_union_failure_inside_bound_region_defers(run_pipeline) -> None:
    """With NO outer region, a row_union's own fail-closed loss ends the
    story in-line — no escalation, because there is no enclosing bound
    frame to defer to.

    This module's docstring and
    ``test_nested_row_union_branch_is_unauthorable_by_design`` cover the
    nested half the brief also names: no topology reaches processor.py's
    row_union arm with an enclosing bound frame, so that half of ruling 19's
    contrast has no fixture to run today."""
    flat = run_pipeline("row_union_lost_branch_top_level")
    assert flat.escalated_losses() == []
    assert flat.failure_outcomes_for_group("union_ab")


_NESTED_FORK_FEEDS_ROW_UNION_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
coalesce:
  - name: merge_inner
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2}}
    policy: require_all
    merge: nested
row_unions:
  - name: row_union_outer
    branches: {{outer_a: merge_inner, outer_b: outer_b}}
    on_success: assembled_out
transforms:
  - name: finalize
    plugin: value_transform
    input: assembled_out
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'done'"}}]
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_ROW_UNION_FEEDS_OUTER_COALESCE_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
row_unions:
  - name: row_union_inner
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2}}
    on_success: row_union_release
transforms:
  - name: after_union
    plugin: value_transform
    input: row_union_release
    on_success: outer_a_continuation
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'after_union'"}}]
coalesce:
  - name: merge_outer
    branches: {{outer_a: outer_a_continuation, outer_b: outer_b}}
    policy: require_all
    merge: nested
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


def test_nested_row_union_branch_is_unauthorable_by_design(tmp_path: Path) -> None:
    """Pins the Task 9 pre-check finding (module docstring): NEITHER nesting
    direction between a row_union and an enclosing bound region builds.

    - a fork nested inside a branch that feeds an outer row_union (here:
      merge_inner's own inner_fork, feeding row_union_outer's outer_a
      branch) is rejected by the SAME whole-roster-identity guard that
      blocks coalesce-in-coalesce would need if it targeted a row_union
      instead — "A nested fork replaces the enclosing branch identity ...
      the union group can never be satisfied".
    - a row_union release feeding an outer coalesce, even through an
      intervening ordinary transform, is rejected because a row_union
      releases an N-to-N group a correlated barrier cannot consume.

    elspeth-a01889580f's ``get_branch_first_nodes`` row_union-loop gap is
    real (the loop lacks the ``_is_nested_barrier_branch``/
    ``_resolve_nested_branch_first_node`` treatment the coalesce loop has),
    but every topology that would reach it is independently build-rejected
    here — this is NOT the shape that gap needs a fixture for.

    Each ``pytest.raises`` carries ``match=`` on a fragment unique to its
    OWN guard (verified against the real raised text, not guessed) — a bare
    ``GraphValidationError`` here would also pass if an unrelated validation
    error fired first, silently rotting this premise-defect pin into
    "something rejected this" instead of "THIS guard rejected this"."""
    cases = (
        (_NESTED_FORK_FEEDS_ROW_UNION_YAML, "nested fork replaces the enclosing branch identity"),
        (_ROW_UNION_FEEDS_OUTER_COALESCE_YAML, "cannot consume an N-to-N group"),
    )
    for index, (yaml_template, message_fragment) in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        with pytest.raises(GraphValidationError, match=re.escape(message_fragment)):
            _build_and_run(yaml_template, case_dir)


# ===== Final review F1: successors minted by successful inner closers =====
# Every fixture above nests on exactly ONE branch and FAILS the inner group,
# so no merged token ever shares a terminating frame with a branch token.
# The three fixtures below are the shapes where one does (a successful inner
# merge mints a successor whose popped lineage terminates at the enclosing
# frame): sibling nested regions with one healthy, a late arrival after a
# successful merge, and sequential nesting inside one branch.

_SIBLING_NESTED_REGIONS_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork_a
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
  - name: inner_fork_b
    input: outer_b
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_b1, inner_b2]
transforms:
  - name: lose_inner_a2
    plugin: dag_corpus_branch_loss
    input: inner_a2
    on_success: inner_a2_out
    on_error: discard
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge_inner_a
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2_out}}
    policy: require_all
    merge: nested
  - name: merge_inner_b
    branches: {{inner_b1: inner_b1, inner_b2: inner_b2}}
    policy: require_all
    merge: nested
  - name: merge_outer
    branches: {{outer_a: merge_inner_a, outer_b: merge_inner_b}}
    policy: require_all
    merge: nested
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

_LATE_ARRIVAL_AFTER_INNER_MERGE_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork_b
    input: outer_b
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_b1, inner_b2]
coalesce:
  - name: merge_inner_b
    branches: {{inner_b1: inner_b1, inner_b2: inner_b2}}
    policy: first
    merge: nested
  - name: merge_outer
    branches: {{outer_a: outer_a, outer_b: merge_inner_b}}
    policy: require_all
    merge: nested
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

_SEQUENTIAL_NESTING_YAML = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: first_fork_a
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [first_a1, first_a2]
  - name: second_fork_a
    input: merge_first_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [second_a1, second_a2]
transforms:
  - name: lose_second_a2
    plugin: dag_corpus_branch_loss
    input: second_a2
    on_success: second_a2_out
    on_error: discard
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge_first_a
    branches: {{first_a1: first_a1, first_a2: first_a2}}
    policy: require_all
    merge: nested
  - name: merge_second_a
    branches: {{second_a1: second_a1, second_a2: second_a2_out}}
    policy: require_all
    merge: nested
  - name: merge_outer
    branches: {{outer_a: merge_second_a, outer_b: outer_b}}
    policy: require_all
    merge: nested
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

_SUCCESSOR_TOPOLOGIES: dict[str, str] = {
    "sibling_nested_regions_one_healthy": _SIBLING_NESTED_REGIONS_YAML,
    "late_arrival_after_inner_merge": _LATE_ARRIVAL_AFTER_INNER_MERGE_YAML,
    "sequential_nesting_second_group_fails": _SEQUENTIAL_NESTING_YAML,
}


def _merged_token_ids(result: PipelineResult) -> set[str]:
    """Tokens minted by a coalesce writer: `tokens.join_group_id` is set only
    by `coalesce_tokens` (the merge-event column), never by fork/create."""
    with result._connect() as conn:
        return set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.join_group_id.is_not(None))).scalars().all())


@pytest.fixture
def run_successor_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_corpus_plugin_manager(monkeypatch)

    def _run(topology_name: str) -> PipelineResult:
        case_dir = tmp_path / topology_name
        case_dir.mkdir()
        db_path, result_data, output_rows = _build_and_run(_SUCCESSOR_TOPOLOGIES[topology_name], case_dir, input_csv="id,value\n1,10\n")
        return PipelineResult(db_path=db_path, result_data=result_data, output_rows=output_rows, outer_closer="merge_outer")

    return _run


def test_sibling_nested_regions_settle_when_the_healthy_sibling_merged(run_successor_pipeline) -> None:
    """Final review F1 (reproduced BLOCKER): nested regions on BOTH outer
    branches, inner_a fails (require_all, one branch lost), inner_b MERGES.
    The merged inner_b token and the outer_b branch token both terminate at
    the outer frame; `_group_roster_settled`'s check of outer_b must resolve
    the merged token as the live member (the consumed branch token is its
    `token_parents` ancestor) instead of crashing Tier-1 "found 2".
    Ledger: ONE escalated loss (merge_outer, outer_a); NOTHING against
    outer_b, which was never lost; every token terminal."""
    result = run_successor_pipeline("sibling_nested_regions_one_healthy")
    escalated = result.escalated_losses()
    assert [(loss.closer_name, loss.member_key) for loss in escalated] == [("merge_outer", "outer_a")]
    assert [loss for loss in result.ledger() if loss.member_key == "outer_b"] == []
    assert [loss.closer_name for loss in result.ledger() if loss.closer_name != "merge_outer"] == ["merge_inner_a"]
    assert result.non_terminal_tokens() == []
    assert result.output_rows == []  # merge_outer is require_all: the outer group fails


def test_late_arrival_after_a_successful_inner_merge_stages_no_outer_loss(run_successor_pipeline) -> None:
    """Final review F1 manifestation 1: inner_b is policy `first`, so its
    first arrival MERGES and the sibling lands as a late arrival. The
    straggler is a member terminal against an already-closed group — it is
    NOT a group failure: no FAIL note, no escalated walk, no `group_failed`
    row against outer_b (the merged token is carrying that member forward).
    merge_outer therefore merges and the row reaches the sink."""
    result = run_successor_pipeline("late_arrival_after_inner_merge")
    assert result.ledger() == []
    assert len(result.output_rows) == 1
    assert result.non_terminal_tokens() == []
    # The straggler's own terminal still lands (zero-write completeness
    # above), and it is the ONLY failure in the run.
    assert len(result.consumed_member_ids) == 1


def test_sequential_nesting_records_one_escalated_loss_against_the_merged_successor(run_successor_pipeline) -> None:
    """Final review F1 manifestation 2: fork→merge→fork→merge inside outer_a,
    the SECOND group fails. Both escalation write sites (the in-claim
    escalated walk and `_stage_pending_escalations`) must name ONE token for
    the outer_a loss — the first merge's SUCCESSOR, which opened the failed
    group and is the live token at the outer frame — or `record_group_loss`'s
    same-key-different-token check crashes the run. Pinned by asserting
    exactly one escalated row whose token is a coalesce-minted token."""
    result = run_successor_pipeline("sequential_nesting_second_group_fails")
    escalated = result.escalated_losses()
    assert [(loss.closer_name, loss.member_key) for loss in escalated] == [("merge_outer", "outer_a")]
    assert escalated[0].token_id in _merged_token_ids(result)
    assert [loss.closer_name for loss in result.ledger() if loss.closer_name != "merge_outer"] == ["merge_second_a"]
    assert result.non_terminal_tokens() == []
