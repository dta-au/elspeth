"""The build's declared-input PROOF (ADR-013 Amendment 2026-09-27, elspeth-5887fb7928 R2).

``schema_validation.declared_input_disposition`` settles a node's declared
input fields against its predecessors' presence votes in ONE walk and yields
both halves the runtime relies on:

- the REFUSAL (``participated ∧ closed ∧ f ∉ vote.fields``), which the build
  raises as a ``GraphValidationError``;
- the PROOF (``declared ∩ ⋂ live preds (vote.fields if participated else ∅)``),
  which the builder publishes on the FINAL graph and the runtime classifies a
  declared-input miss by: a miss of a proven field is Tier 1, an unproven
  field absent from the row is routed.

These tests pin the predicate (presence needs no closedness term — an OPEN
participating upstream proves), its two conservative edges (no predecessor,
any DIVERT in-edge), the per-kind map (transforms and the batch seams), and
that the builder publishes it after the rule-9 DIVERT edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.types import CoalesceName, CollectorName
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.guarantees import EffectiveGuaranteeVote
from elspeth.core.dag.models import GraphValidationError, NodeInfo
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config

_OBSERVED = {"mode": "observed"}


def _source(graph: ExecutionGraph, node_id: str, schema: dict[str, object]) -> None:
    graph.add_node(node_id, node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": schema})


def _consumer_graph(*source_schemas: dict[str, object], declared: frozenset[str] = frozenset({"b"})) -> ExecutionGraph:
    """one or more sources -> one transform declaring ``declared`` -> sink."""
    graph = ExecutionGraph()
    for index, schema in enumerate(source_schemas):
        _source(graph, f"src{index}", schema)
    graph.add_node(
        "t", node_type=NodeType.TRANSFORM, plugin_name="type_coerce", config={"schema": _OBSERVED}, declared_input_fields=declared
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": _OBSERVED})
    for index in range(len(source_schemas)):
        graph.add_edge(f"src{index}", "t", label=f"continue{index}", mode=RoutingMode.MOVE)
    graph.add_edge("t", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


def _disposition(graph: ExecutionGraph, node_id: str = "t", declared: frozenset[str] = frozenset({"b"})):
    cache: dict[str, EffectiveGuaranteeVote] = {}
    return schema_validation.declared_input_disposition(graph, node_id, declared, cache)


class TestProofPredicate:
    """Presence is proven by ``participated ∧ f ∈ vote.fields`` — no closedness term."""

    def test_open_participating_upstream_proves_its_guaranteed_field(self) -> None:
        """An observed source naming guaranteed_fields is OPEN and still PROVES what it names (architect A1)."""
        disposition = _disposition(_consumer_graph({"mode": "observed", "guaranteed_fields": ["b"]}))

        assert disposition.proven == frozenset({"b"})
        assert disposition.certain_missing == ()

    def test_closed_upstream_declaring_the_field_proves_it(self) -> None:
        disposition = _disposition(_consumer_graph({"mode": "fixed", "fields": ["a: str", "b: str"]}))

        assert disposition.proven == frozenset({"b"})
        assert disposition.certain_missing == ()

    def test_abstaining_upstream_proves_nothing(self) -> None:
        disposition = _disposition(_consumer_graph(_OBSERVED))

        assert disposition.proven == frozenset()
        assert disposition.certain_missing == ()

    def test_open_upstream_not_naming_the_field_proves_nothing_and_refuses_nothing(self) -> None:
        disposition = _disposition(_consumer_graph({"mode": "flexible", "fields": ["a: str"]}))

        assert disposition.proven == frozenset()
        assert disposition.certain_missing == ()

    def test_closed_upstream_omitting_the_field_refuses_it_and_proves_nothing(self) -> None:
        """Refusal and proof come from the same vote and are disjoint."""
        disposition = _disposition(_consumer_graph({"mode": "fixed", "fields": ["a: str"]}))

        assert disposition.certain_missing == (("src0", ("b",)),)
        assert disposition.proven == frozenset()

    def test_proof_is_intersected_with_the_declaration(self) -> None:
        disposition = _disposition(
            _consumer_graph({"mode": "fixed", "fields": ["a: str", "b: str", "c: str"]}, declared=frozenset({"b", "z"})),
            declared=frozenset({"b", "z"}),
        )

        assert disposition.proven == frozenset({"b"})

    def test_every_live_predecessor_must_prove_the_field(self) -> None:
        """A token does not carry its arrival edge, so one unproving predecessor unproves the field."""
        both = _disposition(_consumer_graph({"mode": "fixed", "fields": ["b: str"]}, {"mode": "observed", "guaranteed_fields": ["b"]}))
        one = _disposition(_consumer_graph({"mode": "fixed", "fields": ["b: str"]}, _OBSERVED))

        assert both.proven == frozenset({"b"})
        assert one.proven == frozenset()


class TestConservativeEdges:
    """The two places where set algebra alone would err toward PROVING."""

    def test_no_predecessor_proves_nothing(self) -> None:
        """⋂ over no predecessor is the universe; the proof says ∅."""
        graph = ExecutionGraph()
        graph.add_node(
            "t",
            node_type=NodeType.TRANSFORM,
            plugin_name="type_coerce",
            config={"schema": _OBSERVED},
            declared_input_fields=frozenset({"b"}),
        )

        assert _disposition(graph).proven == frozenset()

    def test_a_divert_predecessor_is_ignored_by_both_halves(self) -> None:
        """No row travels a DIVERT edge: it neither proves nor refuses (``_live_predecessors``)."""
        graph = _consumer_graph({"mode": "fixed", "fields": ["b: str"]})
        _source(graph, "errsrc", {"mode": "fixed", "fields": ["a: str"]})
        graph.add_edge("errsrc", "t", label="__error_x__", mode=RoutingMode.DIVERT)

        disposition = _disposition(graph)

        assert disposition.proven == frozenset({"b"})
        # The DIVERT predecessor would be a certain miss; the refusal never rejects a runnable pipeline for it.
        assert disposition.certain_missing == ()


class TestComputeDeclaredInputProof:
    """One entry per node that enforces an input declaration, and none elsewhere."""

    def test_every_enforcing_node_has_an_entry(self) -> None:
        graph = _consumer_graph({"mode": "fixed", "fields": ["b: str"]})
        graph.add_node("t_empty", node_type=NodeType.TRANSFORM, plugin_name="passthrough", config={"schema": _OBSERVED})
        graph.add_node(
            "agg",
            node_type=NodeType.AGGREGATION,
            plugin_name="batch_stats",
            config={"schema": _OBSERVED},
            batch_required_input_fields=frozenset({"b", "v"}),
        )
        graph.add_edge("t", "t_empty", label="next", mode=RoutingMode.MOVE)
        graph.add_edge("src0", "agg", label="to_agg", mode=RoutingMode.MOVE)

        proof = schema_validation.compute_declared_input_proof(graph)

        assert proof == {"t": frozenset({"b"}), "t_empty": frozenset(), "agg": frozenset({"b"})}

    def test_batch_required_input_fields_is_batch_node_only(self) -> None:
        with pytest.raises(GraphValidationError, match="batch_required_input_fields is only meaningful"):
            NodeInfo(node_id="n1", node_type=NodeType.TRANSFORM, plugin_name="x", batch_required_input_fields=frozenset({"b"}))

    def test_an_unpublished_graph_has_no_proof_rather_than_an_empty_one(self) -> None:
        with pytest.raises(OrchestrationInvariantError, match="no declared-input proof"):
            _consumer_graph(_OBSERVED).get_declared_input_proof()


def _build(settings_yaml: str) -> ExecutionGraph:
    settings = load_settings_from_yaml_string(settings_yaml)
    plugins = instantiate_plugins_from_config(settings, preflight_mode=True)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) or None,
        queues=settings.queues,
        row_union_settings=list(settings.row_unions) or None,
        collectors=plugins.collectors or None,
        scope_settings=list(settings.scopes) or None,
        max_bound_region_depth=settings.max_bound_region_depth,
    )


_ROW_UNION_YAML = """
sources:
  src:
    plugin: csv
    on_success: fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: observed, guaranteed_fields: [b]}}
gates:
  - name: split
    input: fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [ctl, trt]
transforms:
  - name: stage_ctl
    plugin: passthrough
    input: ctl
    on_success: ctl_out
    on_error: {ctl_on_error}
    options:
      schema: {{mode: observed}}
  - name: stage_trt
    plugin: passthrough
    input: trt
    on_success: trt_out
    on_error: discard
    options:
      schema: {{mode: observed}}
  - name: after_union
    plugin: type_coerce
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      conversions:
        - field: b
          to: int
row_unions:
  - name: variant_union
    branches:
      ctl: ctl_out
      trt: trt_out
    on_success: union_out
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""


_COALESCE_YAML = _ROW_UNION_YAML.replace(
    """row_unions:
  - name: variant_union
    branches:
      ctl: ctl_out
      trt: trt_out
    on_success: union_out
""",
    """coalesce:
  - name: variant_union
    branches:
      ctl: ctl_out
      trt: trt_out
    policy: {policy}
    merge: union
    {timeout}
""",
).replace("input: union_out", "input: variant_union")  # a coalesce with no on_success produces its own name
assert "row_unions" not in _COALESCE_YAML
assert "input: variant_union" in _COALESCE_YAML


# The red-team case (review-R2 r1 F1): an observed source forks into a
# pass-through branch (abstains: it vouches for nothing) and a branch that
# CREATES y; a union coalesce merges them and type_coerce declares y.
_LOST_BRANCH_YAML = """
sources:
  src:
    plugin: json
    on_success: rows
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema: {{mode: observed}}
gates:
  - name: g
    input: rows
    condition: "True"
    routes: {{'true': fork, 'false': out}}
    fork_to: [pa, pb]
transforms:
  - name: ta
    plugin: passthrough
    input: pa
    on_success: da
    on_error: q
    options: {{schema: {{mode: observed}}}}
  - name: tb
    plugin: value_transform
    input: pb
    on_success: db
    on_error: q
    options:
      schema: {{mode: observed}}
      operations: [{{target: y, expression: "1 // (row['a'] - 3)"}}]
  - name: tail
    plugin: type_coerce
    input: m
    on_success: out
    on_error: q
    options: {{schema: {{mode: observed}}, conversions: [{{field: y, to: str}}]}}
coalesce:
  - name: m
    branches: {{pa: da, pb: db}}
    policy: {policy}
    merge: union
    {timeout}
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options: {{path: {output_path}, format: jsonl, schema: {{mode: observed}}}}
  q:
    plugin: json
    on_write_failure: discard
    options: {{path: {quarantine_path}, format: jsonl, schema: {{mode: observed}}}}
"""


class TestLostBranchIsARowFact:
    """A field only one branch creates is proven only when every branch arrives (R2 fix round 1).

    Under a policy that can lose a branch, a merged row can be the abstaining
    pass-through branch alone, so y is a row fact: its absence routes
    (missing_field) instead of ending the run as a proven miss. Under
    require_all every branch arrives, so y is on every merged row and a miss
    there is our bug (Tier 1).
    """

    @pytest.mark.parametrize(
        ("policy", "timeout", "expected"),
        [
            pytest.param("require_all", "", frozenset({"y"}), id="require_all"),
            pytest.param("best_effort", "timeout_seconds: 5", frozenset(), id="best_effort"),
            pytest.param("first", "", frozenset(), id="first"),
        ],
    )
    def test_the_tail_proof_of_a_field_one_branch_creates(
        self, tmp_path: Path, policy: str, timeout: str, expected: frozenset[str]
    ) -> None:
        input_path = tmp_path / "in.jsonl"
        input_path.write_text('{"a": 1, "b": "2"}\n')
        graph = _build(
            _LOST_BRANCH_YAML.format(
                input_path=input_path,
                output_path=tmp_path / "out.jsonl",
                quarantine_path=tmp_path / "q.jsonl",
                policy=policy,
                timeout=timeout,
            )
        )
        tail = graph.get_transform_name_id_map()["tail"]
        assert graph.get_declared_input_proof()[tail] == expected

    @pytest.mark.parametrize(
        ("policy", "timeout"),
        [pytest.param("best_effort", "timeout_seconds: 5", id="best_effort"), pytest.param("first", "", id="first")],
    )
    def test_every_branch_guaranteeing_the_field_proves_it(self, tmp_path: Path, policy: str, timeout: str) -> None:
        """The routed missing_field text's coalesce remedy (R2 review r2 F1): every branch guarantees y → proven.

        The pa branch creates y too (value_transform guarantees its target), so
        a merged row carries y whichever branches arrive and the build proves
        it at the tail under a policy that can lose a branch.
        """
        input_path = tmp_path / "in.jsonl"
        input_path.write_text('{"a": 1, "b": "2"}\n')
        pa_passthrough = (
            "    plugin: passthrough\n    input: pa\n    on_success: da\n    on_error: q\n    options: {schema: {mode: observed}}\n"
        )
        pa_creates_y = (
            "    plugin: value_transform\n    input: pa\n    on_success: da\n    on_error: q\n"
            '    options:\n      schema: {mode: observed}\n      operations: [{target: y, expression: "0"}]\n'
        )
        yaml_text = _LOST_BRANCH_YAML.format(
            input_path=input_path,
            output_path=tmp_path / "out.jsonl",
            quarantine_path=tmp_path / "q.jsonl",
            policy=policy,
            timeout=timeout,
        )
        assert yaml_text.count(pa_passthrough) == 1
        graph = _build(yaml_text.replace(pa_passthrough, pa_creates_y))
        tail = graph.get_transform_name_id_map()["tail"]
        assert graph.get_declared_input_proof()[tail] == frozenset({"y"})


class TestPublishedOnTheFinalGraph:
    """The builder publishes the proof after the rule-9 DIVERT edges (architect A3/T6)."""

    def _proof_of_after_union(self, tmp_path: Path, ctl_on_error: str) -> frozenset[str]:
        input_path = tmp_path / "in.csv"
        input_path.write_text("a,b\n1,2\n")
        graph = _build(_ROW_UNION_YAML.format(input_path=input_path, output_path=tmp_path / "out.jsonl", ctl_on_error=ctl_on_error))
        after_union = graph.get_transform_name_id_map()["after_union"]
        return graph.get_declared_input_proof()[after_union]

    def test_without_an_error_edge_into_the_closer_the_union_proves_the_guarantee(self, tmp_path: Path) -> None:
        assert self._proof_of_after_union(tmp_path, "discard") == frozenset({"b"})

    def test_a_rule9_error_edge_into_the_closer_unproves_what_follows_it(self, tmp_path: Path) -> None:
        """The DIVERT edge into the row_union (rule 9) exists only on the FINAL graph; the proof must see it."""
        assert self._proof_of_after_union(tmp_path, "variant_union") == frozenset()

    @pytest.mark.parametrize(
        ("policy", "timeout"),
        [pytest.param("require_all", "", id="require_all"), pytest.param("best_effort", "timeout_seconds: 5", id="best_effort")],
    )
    def test_a_rule9_error_edge_into_a_coalesce_keeps_the_proof_under_either_policy(
        self, tmp_path: Path, policy: str, timeout: str
    ) -> None:
        """T6 for a coalesce closer, on the FINAL graph (the rule-9 DIVERT edge is present).

        Under rule 9 the DIVERT edge into a closer is a structural audit marker:
        the failing token terminalizes at the transform exactly as a branch
        loss and never reaches the closer (engine/token_traversal.py). The
        coalesce guarantee the builder computes (union for require_all,
        intersection otherwise) accounts for lost branches, so the
        successor's proof is the same with and without the error edge, under
        both policies. Here `b` is a field EVERY branch guarantees; a field
        only one branch creates is the lost-branch case pinned in
        TestLostBranchIsARowFact. The row_union vote above instead abstains on any DIVERT
        in-edge — conservative (it under-proves; recorded as a vote-precision
        residual for the lane), and pinned so a change to it is judged.
        """
        proofs: dict[str, frozenset[str]] = {}
        for ctl_on_error in ("discard", "variant_union"):
            case_dir = tmp_path / ctl_on_error
            case_dir.mkdir()
            input_path = case_dir / "in.csv"
            input_path.write_text("a,b\n1,2\n")
            yaml_text = _COALESCE_YAML.format(
                input_path=input_path, output_path=case_dir / "out.jsonl", ctl_on_error=ctl_on_error, policy=policy, timeout=timeout
            )
            graph = _build(yaml_text)
            closer = graph.get_coalesce_id_map()[CoalesceName("variant_union")]
            divert_in = [edge for edge in graph.get_incoming_edges(closer) if edge.mode == RoutingMode.DIVERT]
            assert len(divert_in) == (1 if ctl_on_error == "variant_union" else 0)
            proofs[ctl_on_error] = graph.get_declared_input_proof()[graph.get_transform_name_id_map()["after_union"]]

        assert proofs == {"discard": frozenset({"b"}), "variant_union": frozenset({"b"})}

    def test_the_published_proof_is_frozen_with_the_build_metadata(self, tmp_path: Path) -> None:
        input_path = tmp_path / "in.csv"
        input_path.write_text("a,b\n1,2\n")
        graph = _build(_ROW_UNION_YAML.format(input_path=input_path, output_path=tmp_path / "out.jsonl", ctl_on_error="discard"))

        with pytest.raises(GraphValidationError, match="frozen"):
            graph.set_declared_input_proof({})

    def test_the_proof_is_a_pure_function_of_the_build(self, tmp_path: Path) -> None:
        """Resume rebuilds the graph from the same settings; the proof must not depend on anything else (systems C2)."""
        input_path = tmp_path / "in.csv"
        input_path.write_text("a,b\n1,2\n")
        yaml_text = _ROW_UNION_YAML.format(input_path=input_path, output_path=tmp_path / "out.jsonl", ctl_on_error="discard")

        assert dict(_build(yaml_text).get_declared_input_proof()) == dict(_build(yaml_text).get_declared_input_proof())

    def test_the_resume_graphs_carry_the_same_proof_as_the_run(self, tmp_path: Path) -> None:
        """Systems C2: a resumed run rebuilds its graphs through the same builder, so a miss is classified identically."""
        from elspeth.cli import _build_resume_graphs

        input_path = tmp_path / "in.csv"
        input_path.write_text("a,b\n1,2\n")
        yaml_text = _ROW_UNION_YAML.format(input_path=input_path, output_path=tmp_path / "out.jsonl", ctl_on_error="discard")
        run_proof = dict(_build(yaml_text).get_declared_input_proof())
        settings = load_settings_from_yaml_string(yaml_text)

        validation_graph, execution_graph = _build_resume_graphs(settings, instantiate_plugins_from_config(settings, preflight_mode=True))

        assert dict(validation_graph.get_declared_input_proof()) == run_proof
        assert dict(execution_graph.get_declared_input_proof()) == run_proof
        assert frozenset({"b"}) in run_proof.values()


class TestRule9IntoACollector:
    """A rule-9 error edge into a collector closer is an audit marker, so the collector's proof is unchanged."""

    def test_the_collector_proves_the_same_with_and_without_the_error_edge(self, tmp_path: Path) -> None:
        example = (Path(__file__).resolve().parents[4] / "examples" / "scope_collector" / "settings.yaml").read_text()
        marker = "  on_success: pages\n  on_error: discard\n"
        assert example.count(marker) == 1
        proofs: dict[str, frozenset[str]] = {}
        for on_error in ("discard", "page_stitcher"):
            graph = _build(example.replace(marker, f"  on_success: pages\n  on_error: {on_error}\n"))
            collector = graph.get_collector_id_map()[CollectorName("page_stitcher")]
            divert_in = [edge for edge in graph.get_incoming_edges(collector) if edge.mode == RoutingMode.DIVERT]
            assert len(divert_in) == (1 if on_error == "page_stitcher" else 0)
            proofs[on_error] = graph.get_declared_input_proof()[collector]

        assert proofs == {"discard": frozenset({"reading"}), "page_stitcher": frozenset({"reading"})}


class TestFirewallOverPromise:
    """The vote over-promises through a ``mode: fixed`` pass-through; a real build refuses that edge first (T8)."""

    def _firewall_graph(self) -> ExecutionGraph:
        graph = ExecutionGraph()
        _source(graph, "src", {"mode": "fixed", "fields": ["a: str", "url: str"], "guaranteed_fields": ["a", "url"]})
        graph.add_node(
            "llm",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "fixed", "fields": ["url: str"]}},
            passes_through_input=True,
        )
        graph.add_node(
            "scrape",
            node_type=NodeType.TRANSFORM,
            plugin_name="web_scrape",
            config={"schema": _OBSERVED},
            declared_input_fields=frozenset({"a"}),
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": _OBSERVED})
        graph.add_edge("src", "llm", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("llm", "scrape", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("scrape", "sink", label="out", mode=RoutingMode.MOVE)
        return graph

    def test_the_walk_over_promises_through_the_firewall(self) -> None:
        assert _disposition(self._firewall_graph(), "scrape", frozenset({"a"})).proven == frozenset({"a"})

    def test_the_locked_consumer_mirror_refuses_the_edge_into_the_firewall(self, tmp_path: Path) -> None:
        """No row reaches the over-promised point: a real build refuses the edge into the firewall first."""
        input_path = tmp_path / "in.csv"
        input_path.write_text("a,url\n1,http://x\n")
        settings_yaml = f"""
sources:
  src:
    plugin: csv
    on_success: rows
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["a: str", "url: str"]}}
transforms:
  - name: firewall
    plugin: passthrough
    input: rows
    on_success: locked_out
    on_error: discard
    options:
      schema: {{mode: fixed, fields: ["url: str"]}}
  - name: needs_a
    plugin: type_coerce
    input: locked_out
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      conversions:
        - field: a
          to: int
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema: {{mode: observed}}
"""
        # The edge into the locked pass-through is refused (the field the walk would carry past it, 'a',
        # is exactly what the firewall forbids), so the over-promised proof is never consulted for a row.
        with pytest.raises(GraphValidationError, match=r"Extra fields forbidden by consumer: a"):
            _build(settings_yaml)
