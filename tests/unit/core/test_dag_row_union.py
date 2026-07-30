"""DAG construction for row_union barriers (elspeth-a5b86149d4 v1 contract).

Build-time surface: fork branches may target a row_union (identity or
per-branch transform-chain form), the released group continues on the
declared on_success connection, and the group-indivisibility guard rejects
any downstream aggregation whose trigger could fire mid-group (only the
implicit end_of_source trigger is accepted in v1) as well as any downstream
correlated barrier (coalesce / row_union), which cannot consume the N
same-row_id tokens a row_union puts on the wire.

Also covers the DIVERT_ROW_UNION_GROUP_LOSS build warning: row_union is
require_all-only and fails the whole group closed, so an on_error DIVERT
inside a branch chain discards every sibling branch's row too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.graph import GraphValidationError


def _build_graph(yaml_text: str) -> ExecutionGraph:
    settings = load_settings_from_yaml_string(yaml_text)
    bundle = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
    )


def _yaml(
    tmp_path: Path,
    *,
    row_unions: str,
    branch_transforms: str = "",
    tail: str,
    extra_gates: str = "",
    extra_sinks: str = "",
) -> str:
    """Assemble a fork topology: source -> gate fork -> [branches] -> row_union -> tail."""
    input_path = tmp_path / "input.jsonl"
    if not input_path.exists():
        input_path.write_text('{"id": 1, "amount": 3}\n{"id": 2, "amount": 5}\n')
    return f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
{extra_gates}
{branch_transforms}
{row_unions}
{tail}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
{extra_sinks}
"""


_IDENTITY_UNION = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""

_PASSTHROUGH_TAIL = """
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""


class TestRowUnionGraphBuild:
    def test_fork_branches_may_target_row_union(self, tmp_path: Path) -> None:
        graph = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))
        id_map = graph.get_row_union_id_map()
        assert set(id_map) == {"variant_union"}
        branch_map = graph.get_branch_to_row_union_map()
        assert branch_map == {
            "control_branch": "variant_union",
            "treatment_branch": "variant_union",
        }
        info = graph.get_node_info(id_map["variant_union"])
        assert info.node_type.value == "row_union"

    def test_row_union_with_transform_chains(self, tmp_path: Path) -> None:
        branch_transforms = """
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: tag_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
        chain_union = """
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: union_out
"""
        graph = _build_graph(_yaml(tmp_path, row_unions=chain_union, branch_transforms=branch_transforms, tail=""))
        union_id = graph.get_row_union_id_map()["variant_union"]
        # Both chain-end transforms must feed the union node.
        predecessor_plugins = {graph.get_node_info(edge.from_node).plugin_name for edge in graph.get_incoming_edges(union_id)}
        assert predecessor_plugins == {"passthrough"}

    def test_unproduced_branch_rejected(self, tmp_path: Path) -> None:
        bad_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch, ghost_branch]
    on_success: union_out
"""
        with pytest.raises(GraphValidationError, match="no gate produces"):
            _build_graph(_yaml(tmp_path, row_unions=bad_union, tail=_PASSTHROUGH_TAIL))

    def test_no_destination_error_names_row_unions(self, tmp_path: Path) -> None:
        # third_branch has no coalesce, no row_union, and no sink: the
        # fork-destination error must now advertise row_union branches as an
        # option alongside coalesce branches and sinks.
        yaml_text = _yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL).replace(
            "fork_to: [control_branch, treatment_branch]",
            "fork_to: [control_branch, treatment_branch, third_branch]",
        )
        with pytest.raises(GraphValidationError, match="row_union"):
            _build_graph(yaml_text)

    def test_count_trigger_downstream_of_row_union_rejected(self, tmp_path: Path) -> None:
        agg_tail = """
aggregations:
  - name: batch_totals
    plugin: batch_stats
    input: union_out
    on_success: output
    on_error: discard
    trigger:
      count: 5
    output_mode: transform
    options:
      schema:
        mode: observed
      value_field: amount
      compute_mean: true
"""
        with pytest.raises(GraphValidationError, match="end_of_source"):
            _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=agg_tail))

    def test_end_of_source_trigger_downstream_accepted(self, tmp_path: Path) -> None:
        agg_tail = """
aggregations:
  - name: batch_totals
    plugin: batch_stats
    input: union_out
    on_success: output
    on_error: discard
    trigger: {}
    output_mode: transform
    options:
      schema:
        mode: observed
      value_field: amount
      compute_mean: true
"""
        graph = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=agg_tail))
        assert graph.get_row_union_id_map()

    def test_on_success_naming_sink_rejected(self, tmp_path: Path) -> None:
        sink_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: output
"""
        with pytest.raises(GraphValidationError, match="sink"):
            _build_graph(_yaml(tmp_path, row_unions=sink_union, tail=""))

    def test_nested_fork_inside_row_union_branch_is_rejected(self, tmp_path: Path) -> None:
        """A nested fork replaces the enclosing branch identity before its union."""
        nested_fork = """
  - name: nested_fork
    input: control_staged
    condition: "True"
    routes:
      'true': fork
      'false': control_ready
    fork_to: [inner_left, inner_right]
"""
        branch_transforms = """
transforms:
  - name: stage_control
    plugin: passthrough
    input: control_branch
    on_success: control_staged
    on_error: discard
    options:
      schema:
        mode: observed
  - name: stage_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_ready
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
        row_union = """
row_unions:
  - name: variant_union
    branches:
      control_branch: control_ready
      treatment_branch: treatment_ready
    on_success: union_out
"""
        inner_sinks = f"""
  inner_left:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "inner-left.jsonl"}
      format: jsonl
      schema:
        mode: observed
  inner_right:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "inner-right.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
        with pytest.raises(GraphValidationError) as exc_info:
            _build_graph(
                _yaml(
                    tmp_path,
                    row_unions=row_union,
                    branch_transforms=branch_transforms,
                    tail="",
                    extra_gates=nested_fork,
                    extra_sinks=inner_sinks,
                )
            )
        message = str(exc_info.value)
        assert "nested_fork" in message
        assert "variant_union" in message
        assert "nested fork" in message.lower()

    def test_union_rejects_branches_produced_by_ancestor_and_nested_forks(self, tmp_path: Path) -> None:
        """A require-all union cannot span mutually exclusive fork generations."""
        nested_fork = """
  - name: nested_fork
    input: control_staged
    condition: "True"
    routes:
      'true': fork
      'false': control_ready
    fork_to: [inner_left, inner_right]
"""
        branch_transforms = """
transforms:
  - name: stage_control
    plugin: passthrough
    input: control_branch
    on_success: control_staged
    on_error: discard
    options:
      schema:
        mode: observed
  - name: stage_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_ready
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
        row_union = """
row_unions:
  - name: variant_union
    branches:
      control_branch: control_ready
      treatment_branch: treatment_ready
      inner_left: inner_left
      inner_right: inner_right
    on_success: union_out
"""

        with pytest.raises(GraphValidationError) as exc_info:
            _build_graph(
                _yaml(
                    tmp_path,
                    row_unions=row_union,
                    branch_transforms=branch_transforms,
                    tail="",
                    extra_gates=nested_fork,
                )
            )

        message = str(exc_info.value)
        assert "variant_fork" in message
        assert "nested_fork" in message
        assert "ancestor" in message.lower()

    def test_coalesce_downstream_of_row_union_rejected(self, tmp_path: Path) -> None:
        # row_union -> transform -> gate(fork_to) -> coalesce used to BUILD and
        # RUN, silently discarding half of every union group: the coalesce
        # pending map keys on (barrier, row_id) with no fork_group_id, so the
        # second same-row_id arrival per branch is rejected as a late arrival.
        downstream_fork = """
  - name: downstream_fork
    input: after_union_out
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [left_branch, right_branch]
"""
        coalesce_tail = """
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: after_union_out
    on_error: discard
    options:
      schema:
        mode: observed
coalesce:
  - name: downstream_merge
    branches: [left_branch, right_branch]
    policy: require_all
    merge: union
    on_success: output
"""
        with pytest.raises(GraphValidationError, match="cannot consume an N-to-N group"):
            _build_graph(
                _yaml(
                    tmp_path,
                    row_unions=_IDENTITY_UNION,
                    extra_gates=downstream_fork,
                    tail=coalesce_tail,
                )
            )

    def test_row_union_downstream_of_row_union_rejected(self, tmp_path: Path) -> None:
        # This topology built cleanly and then died mid-run with "Duplicate
        # arrival for branch ... indicates a bug in fork, retry, or
        # checkpoint/resume logic" — a diagnostic that blames the engine for a
        # topology the builder accepted. Reject it at build time instead.
        chained_unions = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
  - name: second_union
    branches: [left_branch, right_branch]
    on_success: second_out
"""
        downstream_fork = """
  - name: downstream_fork
    input: union_out
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [left_branch, right_branch]
"""
        tail = """
transforms:
  - name: after_second_union
    plugin: passthrough
    input: second_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
        with pytest.raises(GraphValidationError, match="cannot consume an N-to-N group"):
            _build_graph(
                _yaml(
                    tmp_path,
                    row_unions=chained_unions,
                    extra_gates=downstream_fork,
                    tail=tail,
                )
            )

    def test_coalesce_on_a_parallel_path_is_accepted(self, tmp_path: Path) -> None:
        # The guard is reachability-scoped: a coalesce that is NOT downstream of
        # the row_union stays legal even in a graph that also has a row_union.
        parallel_fork = """
  - name: parallel_fork
    input: parallel_in
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [x_branch, y_branch]
"""
        parallel_coalesce = """
coalesce:
  - name: parallel_merge
    branches: [x_branch, y_branch]
    policy: require_all
    merge: union
    on_success: output
"""
        yaml_text = _yaml(
            tmp_path,
            row_unions=_IDENTITY_UNION,
            extra_gates=parallel_fork,
            tail=_PASSTHROUGH_TAIL + parallel_coalesce,
        ).replace(
            "'false': output\n    fork_to: [control_branch, treatment_branch]",
            "'false': parallel_in\n    fork_to: [control_branch, treatment_branch]",
        )
        graph = _build_graph(yaml_text)
        assert set(graph.get_row_union_id_map()) == {"variant_union"}
        assert set(graph.get_coalesce_id_map()) == {"parallel_merge"}

    def test_branch_shared_with_coalesce_rejected(self, tmp_path: Path) -> None:
        shared = """
coalesce:
  - name: merge_point
    branches: [control_branch, treatment_branch]
    policy: require_all
    merge: union
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""
        with pytest.raises(GraphValidationError, match="coalesce"):
            _build_graph(_yaml(tmp_path, row_unions=shared, tail=_PASSTHROUGH_TAIL))


_CHAIN_UNION = """
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: union_out
"""


def _chain_branch_transforms(*, control_on_error: str, treatment_on_error: str) -> str:
    return f"""
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_scored
    on_error: {control_on_error}
    options:
      schema:
        mode: observed
  - name: tag_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_scored
    on_error: {treatment_on_error}
    options:
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""


def _error_sink(tmp_path: Path) -> str:
    return f"""
  errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "errors.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""


class TestRowUnionDivertWarnings:
    """DIVERT_ROW_UNION_GROUP_LOSS parity with the coalesce divert warnings.

    Note: ``on_error: discard`` builds NO edge (builder.py drops it the way
    sinks drop ``on_write_failure: discard``), so a DIVERT-bearing branch must
    name a real error sink.
    """

    def test_divert_in_branch_warns_whole_group_loss(self, tmp_path: Path) -> None:
        graph = _build_graph(
            _yaml(
                tmp_path,
                row_unions=_CHAIN_UNION,
                branch_transforms=_chain_branch_transforms(control_on_error="errors", treatment_on_error="discard"),
                tail="",
                extra_sinks=_error_sink(tmp_path),
            )
        )
        matching = [w for w in graph.validation_warnings if w.code == "DIVERT_ROW_UNION_GROUP_LOSS"]
        assert len(matching) == 1, [w.code for w in graph.validation_warnings]
        warning = matching[0]
        assert "tag_control" in warning.message
        assert "variant_union" in warning.message
        # The whole point: the SIBLING branch's successful row is discarded too.
        assert "treatment_branch" in warning.message
        assert any("variant_union" in nid for nid in warning.node_ids)

    def test_no_divert_emits_no_row_union_warning(self, tmp_path: Path) -> None:
        graph = _build_graph(
            _yaml(
                tmp_path,
                row_unions=_CHAIN_UNION,
                branch_transforms=_chain_branch_transforms(control_on_error="discard", treatment_on_error="discard"),
                tail="",
            )
        )
        assert [w for w in graph.validation_warnings if w.code.startswith("DIVERT_ROW_UNION")] == []

    def test_row_union_warnings_do_not_displace_coalesce_warnings(self, tmp_path: Path) -> None:
        # Both barrier kinds in one graph: set_validation_warnings ASSIGNS, so a
        # naive second call would drop the coalesce warnings entirely.
        parallel_fork = """
  - name: parallel_fork
    input: parallel_in
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [x_branch, y_branch]
"""
        parallel_tail = """
  - name: tag_x
    plugin: passthrough
    input: x_branch
    on_success: x_scored
    on_error: errors
    options:
      schema:
        mode: observed
  - name: tag_y
    plugin: passthrough
    input: y_branch
    on_success: y_scored
    on_error: discard
    options:
      schema:
        mode: observed
coalesce:
  - name: parallel_merge
    branches:
      x_branch: x_scored
      y_branch: y_scored
    policy: require_all
    merge: union
    on_success: output
"""
        yaml_text = _yaml(
            tmp_path,
            row_unions=_CHAIN_UNION,
            branch_transforms=_chain_branch_transforms(control_on_error="errors", treatment_on_error="discard") + parallel_tail,
            tail="",
            extra_gates=parallel_fork,
            extra_sinks=_error_sink(tmp_path),
        ).replace(
            "'false': output\n    fork_to: [control_branch, treatment_branch]",
            "'false': parallel_in\n    fork_to: [control_branch, treatment_branch]",
        )
        graph = _build_graph(yaml_text)
        codes = {w.code for w in graph.validation_warnings}
        assert "DIVERT_ROW_UNION_GROUP_LOSS" in codes
        assert "DIVERT_COALESCE_REQUIRE_ALL" in codes


# ===== BRANCH-INTERNAL AGGREGATION GUARD =====
#
# An aggregation INSIDE a fork branch that feeds a row_union is a different
# hazard from the downstream group-split guard above: the downstream guard
# walks forward from the barrier, so an aggregation upstream of it inside a
# branch is never examined. Its flush emits continuations through a path that
# does not carry the row_union binding, and in output_mode: transform the
# emitted children all inherit ONE buffered parent's row_id, so the group can
# never be satisfied whatever the binding does.


def _branch_agg_yaml(tmp_path: Path, *, output_mode: str) -> str:
    input_path = tmp_path / "branch_agg_input.jsonl"
    if not input_path.exists():
        input_path.write_text('{"id": 1, "copies": 1}\n')
    output_path = tmp_path / "branch_agg_out.jsonl"
    barrier_block = """row_unions:
  - name: variant_union
    branches:
      control_branch: control_ready
      treatment_branch: treatment_ready
    on_success: union_out
"""
    return f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
transforms:
  - name: treatment_identity
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_ready
    on_error: discard
    options:
      schema:
        mode: observed
  - name: union_tail
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
aggregations:
  - name: control_batch
    plugin: batch_replicate
    input: control_branch
    on_success: control_ready
    on_error: discard
    trigger: {{}}
    output_mode: {output_mode}
    options:
      schema:
        mode: observed
      copies_field: copies
      default_copies: 1
      include_copy_index: false
{barrier_block}sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
"""


def test_transform_mode_branch_aggregation_feeding_row_union_is_rejected(tmp_path: Path) -> None:
    """output_mode: transform inside a row_union branch cannot satisfy the barrier.

    expand_token stamps every emitted child with a single buffered parent's
    row_id, so one row_id contributes M arrivals (colliding when M > 1) and
    every other buffered row_id contributes none. Reject at build time with a
    diagnostic naming the aggregation, the barrier, the hazard, and the
    supported alternatives.
    """
    with pytest.raises(GraphValidationError) as exc_info:
        _build_graph(_branch_agg_yaml(tmp_path, output_mode="transform"))
    message = str(exc_info.value)
    assert "control_batch" in message
    assert "variant_union" in message
    assert "row_id" in message
    assert "passthrough" in message


def test_unrelated_row_union_fork_does_not_hide_branch_aggregation(tmp_path: Path) -> None:
    """A later fork for another union must not stop this union's branch walk."""
    input_path = tmp_path / "nested_union_agg_input.jsonl"
    input_path.write_text('{"id": 1, "copies": 1}\n')
    yaml_text = f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: outer_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': a_output
    fork_to: [a_control, a_treatment]
  - name: later_fork
    input: batch_out
    condition: "False"
    routes:
      'true': fork
      'false': a_control_ready
    fork_to: [b_left, b_right]
transforms:
  - name: stage_treatment
    plugin: passthrough
    input: a_treatment
    on_success: a_treatment_ready
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_a
    plugin: passthrough
    input: a_union_out
    on_success: a_output
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_b
    plugin: passthrough
    input: b_union_out
    on_success: b_output
    on_error: discard
    options:
      schema:
        mode: observed
aggregations:
  - name: control_batch
    plugin: batch_replicate
    input: a_control
    on_success: batch_out
    on_error: discard
    trigger: {{}}
    output_mode: transform
    options:
      schema:
        mode: observed
      copies_field: copies
      default_copies: 1
      include_copy_index: false
row_unions:
  - name: union_a
    branches:
      a_control: a_control_ready
      a_treatment: a_treatment_ready
    on_success: a_union_out
  - name: union_b
    branches: [b_left, b_right]
    on_success: b_union_out
sinks:
  a_output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "union-a.jsonl"}
      format: jsonl
      schema:
        mode: observed
  b_output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "union-b.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
    with pytest.raises(GraphValidationError) as exc_info:
        _build_graph(yaml_text)
    message = str(exc_info.value)
    assert "control_batch" in message
    assert "union_a" in message


def test_passthrough_mode_branch_aggregation_feeding_row_union_builds(tmp_path: Path) -> None:
    """passthrough preserves token identity, so the group stays satisfiable.

    The guard must not reject this shape: _route_passthrough_results validates
    1:1 and updates the ORIGINAL tokens, so each buffered row_id keeps its own
    arrival. Rejecting it would delete a real capability.
    """
    graph = _build_graph(_branch_agg_yaml(tmp_path, output_mode="passthrough"))
    assert graph is not None


def test_aggregation_upstream_of_the_fork_is_not_rejected(tmp_path: Path) -> None:
    """The guard must not cross the fork gate into pre-fork topology.

    Moving the aggregation upstream of the fork is the remedy the diagnostic
    recommends, so a walk that crossed the gate would reject its own advice.
    Fork -> branch edges are COPY only for identity branches; a transform-chain
    branch is wired gate -> first node as MOVE, so a naive backward MOVE walk
    would run straight through the gate.
    """
    yaml_text = _branch_agg_yaml(tmp_path, output_mode="passthrough")
    yaml_text = yaml_text.replace("    input: control_branch\n", "    input: routed\n")
    yaml_text = yaml_text.replace("      control_branch: control_ready\n", "      control_branch: control_branch\n")
    yaml_text = yaml_text.replace("    on_success: control_ready\n", "    on_success: pre_fork\n")
    yaml_text = yaml_text.replace("    input: routed\n    condition:", "    input: pre_fork\n    condition:")
    graph = _build_graph(yaml_text)
    assert graph is not None
