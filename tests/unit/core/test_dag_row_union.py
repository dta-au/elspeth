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

from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts import Checkpoint
from elspeth.core.canonical import compute_full_topology_hash
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
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
        # ghost_branch is a PARTIAL orphan: control_branch/treatment_branch
        # ARE produced (by variant_fork), only ghost_branch is not. Rule 2's
        # roster-equality check (spec §7 rule 2) sees the producing gate
        # registered against this closer and fires there — the standalone
        # "VALIDATE ROW_UNION BRANCHES ARE PRODUCED BY GATES" orphan check
        # never runs for a partial orphan (maintainer ruling 2026-08-23);
        # it stays reachable only when EVERY declared branch is unproduced
        # (see test_wholly_unproduced_union_rejected below). Pin the rule-2
        # code and the enriched orphan-listing content so the enrichment is
        # load-bearing, not decorative — and assert the OLD standalone
        # check's message shape does not appear, even though it happens to
        # share the substring "no gate produces" with rule 2's enrichment.
        bad_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch, ghost_branch]
    on_success: union_out
"""
        with pytest.raises(GraphValidationError) as exc_info:
            _build_graph(_yaml(tmp_path, row_unions=bad_union, tail=_PASSTHROUGH_TAIL))
        message = str(exc_info.value)
        assert "roster mismatch" in message
        assert "drawn from 1 fork gate(s)" in message
        assert "'variant_fork' declares ['control_branch', 'treatment_branch']" in message
        assert "no gate produces ['ghost_branch']" in message
        assert "declares branch 'ghost_branch', but no gate produces this branch.\nBranches must be listed" not in message

    def test_wholly_unproduced_union_rejected(self, tmp_path: Path) -> None:
        # Every declared branch is unproduced (zero overlap with any gate's
        # fork_to) — no gate ever registers this closer, so rule 2's
        # per-gate loop never sees it. The standalone "VALIDATE ROW_UNION
        # BRANCHES ARE PRODUCED BY GATES" check is the only thing that
        # catches this shape (maintainer ruling 2026-08-23 verified this is
        # the one still-reachable case; the check was kept, not deleted).
        bad_union = """
row_unions:
  - name: orphan_union
    branches: [ghost_a, ghost_b]
    on_success: union_out
"""
        extra_sinks = f"""
  control_branch:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "cb.jsonl"}
      format: jsonl
      schema:
        mode: observed
  treatment_branch:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "tb.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
        with pytest.raises(GraphValidationError, match="no gate produces this branch"):
            _build_graph(_yaml(tmp_path, row_unions=bad_union, tail=_PASSTHROUGH_TAIL, extra_sinks=extra_sinks))

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
        # Rule 2's roster-equality check (spec §7 rule 2) supersedes the old
        # row_union-specific "ancestor/descendant fork generations"
        # diagnostic (maintainer ruling 2026-08-23): a closer whose roster
        # spans a nested fork generation can never equal any single
        # contributing gate's own fork_to, so this is now a roster mismatch,
        # not a bespoke fork-generation check. Pin the rule-2 code and the
        # enriched, per-gate-roster content (this is what replaces the old
        # diagnostic's value), and assert the old code does NOT fire.
        assert "roster mismatch" in message
        assert "variant_union" in message
        assert "drawn from 2 fork gate(s)" in message
        assert "'variant_fork' declares ['control_branch', 'treatment_branch']" in message
        assert "'nested_fork' declares ['inner_left', 'inner_right']" in message
        assert "declares branches produced by ancestor/descendant" not in message

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

    def test_union_rejects_branches_from_unrelated_fork_gates(self, tmp_path: Path) -> None:
        """Branches of one union must share one fork origin (elspeth-d560c5e649).

        Two independent sources feed two unrelated fork gates; one branch from
        each gate is mapped into the same row_union. Rows from different fork
        origins never share a correlation identity, so no group can ever
        complete — the graph must be rejected at build time, not discovered at
        timeout/end-of-source.
        """
        input_a = tmp_path / "input_a.jsonl"
        input_a.write_text('{"id": 1, "amount": 3}\n')
        input_b = tmp_path / "input_b.jsonl"
        input_b.write_text('{"id": 1, "amount": 5}\n')
        yaml_text = f"""
sources:
  rows_a:
    plugin: json
    on_success: routed_a
    options:
      path: {input_a}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
  rows_b:
    plugin: json
    on_success: routed_b
    options:
      path: {input_b}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: fork_alpha
    input: routed_a
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch]
  - name: fork_beta
    input: routed_b
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [treatment_branch]
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
        with pytest.raises(GraphValidationError) as exc_info:
            _build_graph(yaml_text)
        message = str(exc_info.value)
        # Rule 2's roster-equality check (spec §7 rule 2) supersedes the old
        # row_union-specific "unrelated fork gates" origin diagnostic
        # (maintainer ruling 2026-08-23): a closer whose roster is produced
        # by more than one fork gate can never equal any single gate's own
        # fork_to, so this is now a roster mismatch, not a bespoke
        # cross-origin check. Pin the rule-2 code and the enriched,
        # per-gate-roster content (this is what replaces the old
        # diagnostic's value), and assert the old code does NOT fire.
        assert "roster mismatch" in message
        assert "variant_union" in message
        assert "drawn from 2 fork gate(s)" in message
        assert "'fork_alpha' declares ['control_branch']" in message
        assert "'fork_beta' declares ['treatment_branch']" in message
        assert "declares branches produced by unrelated fork gates" not in message

    def test_union_rejects_branch_input_not_descending_from_alias(self, tmp_path: Path) -> None:
        """A mapped input must descend from its own branch alias (elspeth-d560c5e649).

        One fork gate owns both aliases, but 'control_branch' maps an input
        connection whose chain roots at a SECOND source — the alias itself is
        consumed by an unrelated chain, defeating the dangling-connection
        guard. Rows arriving from the imposter chain never traversed the fork,
        so they carry no branch identity and the group can never complete.
        """
        input_a = tmp_path / "input_a.jsonl"
        input_a.write_text('{"id": 1, "amount": 3}\n')
        input_b = tmp_path / "input_b.jsonl"
        input_b.write_text('{"id": 1, "amount": 5}\n')
        yaml_text = f"""
sources:
  rows_a:
    plugin: json
    on_success: routed
    options:
      path: {input_a}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
  rows_b:
    plugin: json
    on_success: side_feed
    options:
      path: {input_b}
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
  - name: alias_eater
    plugin: passthrough
    input: control_branch
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
  - name: imposter
    plugin: passthrough
    input: side_feed
    on_success: imposter_out
    on_error: discard
    options:
      schema:
        mode: observed
  - name: real_treatment
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
row_unions:
  - name: variant_union
    branches:
      control_branch: imposter_out
      treatment_branch: treatment_scored
    on_success: union_out
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""
        with pytest.raises(GraphValidationError) as exc_info:
            _build_graph(yaml_text)
        message = str(exc_info.value)
        assert "control_branch" in message
        assert "imposter_out" in message
        assert "graph construction bug" not in message


class TestBarrierBranchOrderIdentity:
    """Declared branch order is executable topology semantics (elspeth-9c5789c4ad).

    RowUnionExecutor releases groups in declaration order and coalesce merge
    precedence (first_wins/last_wins, nested field order) follows declaration
    order, but RFC 8785 canonical hashing sorts mapping keys — so order must be
    bound into node identity explicitly or a reorder resumes old checkpoints
    under different runtime semantics.
    """

    _REVERSED_UNION = """
row_unions:
  - name: variant_union
    branches: [treatment_branch, control_branch]
    on_success: union_out
"""

    def test_reordering_branches_changes_topology_hash_and_node_id(self, tmp_path: Path) -> None:
        graph_declared = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))
        graph_reordered = _build_graph(_yaml(tmp_path, row_unions=self._REVERSED_UNION, tail=_PASSTHROUGH_TAIL))

        assert graph_declared.get_row_union_id_map()["variant_union"] != graph_reordered.get_row_union_id_map()["variant_union"]
        assert compute_full_topology_hash(graph_declared) != compute_full_topology_hash(graph_reordered)

    _REVERSED_CHAIN_UNION = """
row_unions:
  - name: variant_union
    branches:
      treatment_branch: treatment_scored
      control_branch: control_scored
    on_success: union_out
"""

    def test_reordering_mapping_form_branches_changes_topology_hash(self, tmp_path: Path) -> None:
        """Dict-form branches must bind declaration order into identity too.

        Chain branches can only be declared in mapping form, and mapping-form
        order survives settings validation only as long as the validators
        preserve insertion order — this pins that path so a future
        'sort-for-determinism' refactor cannot silently reintroduce the bug
        for chain branches while the list-form tests stay green.
        """
        chain_transforms = _chain_branch_transforms(control_on_error="discard", treatment_on_error="discard")
        graph_declared = _build_graph(_yaml(tmp_path, row_unions=_CHAIN_UNION, branch_transforms=chain_transforms, tail=""))
        graph_reordered = _build_graph(_yaml(tmp_path, row_unions=self._REVERSED_CHAIN_UNION, branch_transforms=chain_transforms, tail=""))

        assert graph_declared.get_row_union_id_map()["variant_union"] != graph_reordered.get_row_union_id_map()["variant_union"]
        assert compute_full_topology_hash(graph_declared) != compute_full_topology_hash(graph_reordered)

    def test_identical_declaration_hashes_identically(self, tmp_path: Path) -> None:
        graph_one = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))
        graph_two = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))

        assert compute_full_topology_hash(graph_one) == compute_full_topology_hash(graph_two)

    def test_reordered_branches_fail_checkpoint_resume_closed(self, tmp_path: Path) -> None:
        graph_declared = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))
        graph_reordered = _build_graph(_yaml(tmp_path, row_unions=self._REVERSED_UNION, tail=_PASSTHROUGH_TAIL))

        checkpoint = Checkpoint(
            checkpoint_id="cp-branch-order",
            run_id="run-branch-order",
            sequence_number=1,
            created_at=datetime.now(UTC),
            upstream_topology_hash=compute_full_topology_hash(graph_declared),
            format_version=Checkpoint.CURRENT_FORMAT_VERSION,
        )
        validator = CheckpointCompatibilityValidator()

        same_order = validator.validate(checkpoint, graph_declared)
        assert same_order.can_resume

        reordered = validator.validate(checkpoint, graph_reordered)
        assert not reordered.can_resume

    def test_coalesce_branch_reorder_changes_topology_hash(self, tmp_path: Path) -> None:
        """Coalesce parity: merge precedence follows declaration order too."""

        def coalesce_yaml(branches: str) -> str:
            return _yaml(
                tmp_path,
                row_unions=f"""
coalesce:
  - name: variant_merge
    branches: {branches}
    policy: require_all
    merge: union
    union_collision_policy: first_wins
""",
                tail="""
transforms:
  - name: after_merge
    plugin: passthrough
    input: variant_merge
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
""",
            )

        graph_declared = _build_graph(coalesce_yaml("[control_branch, treatment_branch]"))
        graph_reordered = _build_graph(coalesce_yaml("[treatment_branch, control_branch]"))

        assert compute_full_topology_hash(graph_declared) != compute_full_topology_hash(graph_reordered)


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


_PRE_FORK_DIVERT_TRANSFORM = """
  - name: pre_fork_tag
    plugin: passthrough
    input: routed
    on_success: pre_forked
    on_error: errors
    options:
      schema:
        mode: observed
"""


def _with_pre_fork_divert(yaml_text: str) -> str:
    """Reroute the fork gate's input through the DIVERT-bearing pre-fork transform.

    source on_success 'routed' -> pre_fork_tag (on_error DIVERTs to 'errors')
    -> on_success 'pre_forked' -> variant_fork input. The replace targets the
    gate block's ``input: routed`` by anchoring on the adjacent ``condition:``
    line (same idiom as test_aggregation_upstream_of_the_fork_is_not_rejected).
    """
    return yaml_text.replace(
        "    input: routed\n    condition:",
        "    input: pre_forked\n    condition:",
    )


# Branch chain with a branch-local NON-fork routing gate in the middle:
# variant_fork -> tag_control (DIVERT) -> mid_gate -> polish_control -> union.
# The second transform is named so it shares no substring with 'tag_control'.
# Both routes converge on 'control_gated' (condition is always "True", so
# 'false' never actually fires) — spec §7 rule 4's forward walk rejects flat
# any in-region route to a sink, including one only reachable in the
# hypothetical branch of an always-true condition, so this gate's 'false'
# route must stay in-region like 'true' rather than naming the sink
# 'output' would previously have fallen through to pre-rule-4.
_MID_GATE = """
  - name: mid_gate
    input: control_mid
    condition: "True"
    routes:
      'true': control_gated
      'false': control_gated
"""

_MID_GATE_DIVERT = _MID_GATE.replace(
    '    condition: "True"',
    '    on_error: errors\n    condition: "True"',
)

_MID_GATE_BRANCH_TRANSFORMS = """
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_mid
    on_error: errors
    options:
      schema:
        mode: observed
  - name: polish_control
    plugin: passthrough
    input: control_gated
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

    def test_divert_upstream_of_the_fork_emits_no_group_loss_warning(self, tmp_path: Path) -> None:
        """A DIVERT before the fork strands no sibling group (elspeth-94d68e7aca).

        A row diverted upstream of the fork never forked, so there is no
        correlated group to lose. A transform-chain branch is wired
        gate -> first transform as a MOVE edge, so an unbounded backward MOVE
        walk runs straight through the union's originating fork gate into
        pre-fork topology and charges EVERY branch with the pre-fork
        transform — one false warning per branch.
        """
        graph = _build_graph(
            _with_pre_fork_divert(
                _yaml(
                    tmp_path,
                    row_unions=_CHAIN_UNION,
                    branch_transforms=_chain_branch_transforms(control_on_error="discard", treatment_on_error="discard")
                    + _PRE_FORK_DIVERT_TRANSFORM,
                    tail="",
                    extra_sinks=_error_sink(tmp_path),
                )
            )
        )
        matching = [w for w in graph.validation_warnings if w.code == "DIVERT_ROW_UNION_GROUP_LOSS"]
        assert matching == [], [w.message for w in matching]

    def test_pre_fork_divert_not_collected_alongside_branch_local_divert(self, tmp_path: Path) -> None:
        """The boundary must not mask genuine in-branch diverts — or name pre-fork ones.

        tag_control diverts INSIDE the control branch: that warning is real and
        must survive, naming tag_control only. The pre-fork transform must not
        be swept into it, and the treatment branch (whose only reachable DIVERT
        sits upstream of the fork) must not warn at all.
        """
        graph = _build_graph(
            _with_pre_fork_divert(
                _yaml(
                    tmp_path,
                    row_unions=_CHAIN_UNION,
                    branch_transforms=_chain_branch_transforms(control_on_error="errors", treatment_on_error="discard")
                    + _PRE_FORK_DIVERT_TRANSFORM,
                    tail="",
                    extra_sinks=_error_sink(tmp_path),
                )
            )
        )
        matching = [w for w in graph.validation_warnings if w.code == "DIVERT_ROW_UNION_GROUP_LOSS"]
        assert len(matching) == 1, [w.message for w in matching]
        warning = matching[0]
        assert "tag_control" in warning.message
        assert "pre_fork_tag" not in warning.message
        assert any("tag_control" in nid for nid in warning.node_ids)
        assert not any("pre_fork_tag" in nid for nid in warning.node_ids)

    def test_intermediate_gate_branch_preserves_group_loss_warning(self, tmp_path: Path) -> None:
        """Branch-local NON-fork routing gates are still crossed by the walk.

        The scan boundary is the union's ORIGINATING fork gate, not 'any gate':
        a routing gate between two branch transforms must not hide a DIVERT on
        the transform upstream of it (mirror of the coalesce-side
        test_intermediate_gate_branch_preserves_require_all_warning).
        """
        graph = _build_graph(
            _yaml(
                tmp_path,
                row_unions=_CHAIN_UNION,
                branch_transforms=_MID_GATE_BRANCH_TRANSFORMS,
                tail="",
                extra_gates=_MID_GATE,
                extra_sinks=_error_sink(tmp_path),
            )
        )
        matching = [w for w in graph.validation_warnings if w.code == "DIVERT_ROW_UNION_GROUP_LOSS"]
        assert len(matching) == 1, [w.message for w in matching]
        warning = matching[0]
        assert "tag_control" in warning.message
        assert "polish_control" not in warning.message
        assert any("tag_control" in nid for nid in warning.node_ids)

    def test_intermediate_gate_divert_warns_whole_group_loss(self, tmp_path: Path) -> None:
        branch_transforms = _MID_GATE_BRANCH_TRANSFORMS.replace(
            "    on_error: errors",
            "    on_error: discard",
            1,
        )
        graph = _build_graph(
            _yaml(
                tmp_path,
                row_unions=_CHAIN_UNION,
                branch_transforms=branch_transforms,
                tail="",
                extra_gates=_MID_GATE_DIVERT,
                extra_sinks=_error_sink(tmp_path),
            )
        )

        matching = [warning for warning in graph.validation_warnings if warning.code == "DIVERT_ROW_UNION_GROUP_LOSS"]

        assert len(matching) == 1, [warning.message for warning in matching]
        assert "mid_gate" in matching[0].message
        assert any("mid_gate" in node_id for node_id in matching[0].node_ids)

    def test_origin_fork_gate_divert_emits_no_group_loss_warning(self, tmp_path: Path) -> None:
        yaml_text = _yaml(
            tmp_path,
            row_unions=_CHAIN_UNION,
            branch_transforms=_chain_branch_transforms(control_on_error="discard", treatment_on_error="discard"),
            tail="",
            extra_sinks=_error_sink(tmp_path),
        ).replace(
            "    input: routed\n    condition:",
            "    input: routed\n    on_error: errors\n    condition:",
            1,
        )

        graph = _build_graph(yaml_text)

        assert [warning for warning in graph.validation_warnings if warning.code == "DIVERT_ROW_UNION_GROUP_LOSS"] == []


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
