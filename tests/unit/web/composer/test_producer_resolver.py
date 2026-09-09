"""Tests for the shared producer-map resolver primitive."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

import elspeth
from elspeth.core.config import (
    AggregationSettings,
    CoalesceSettings,
    CollectorSettings,
    GateSettings,
    QueueSettings,
    RowUnionSettings,
    TransformSettings,
)
from elspeth.web.composer._producer_resolver import (
    _IMPLICIT_SELF_PUBLISHING_NODE_TYPES,
    _SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS,
    ProducerResolver,
    published_success_connection,
)
from elspeth.web.composer.state import COMPOSER_NODE_TYPES, NodeSpec, SourceSpec, _runtime_connection_targets

_STATE_SOURCE_PATH = Path(elspeth.__file__).parent / "web" / "composer" / "state.py"
_PRODUCER_RESOLVER_SOURCE_PATH = Path(elspeth.__file__).parent / "web" / "composer" / "_producer_resolver.py"
_AUTHORITY_NAME = "_IMPLICIT_SELF_PUBLISHING_NODE_TYPES"
_DERIVED_NAME = "_SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS"
_AUTHORITY_MODULE = "elspeth.web.composer._producer_resolver"


def _node(
    node_id: str,
    *,
    plugin: str | None,
    node_type: str = "transform",
    input: str = "",
    on_success: str | None = None,
    on_error: str | None = None,
    options: dict | None = None,
    routes: dict[str, str] | None = None,
    fork_to: tuple[str, ...] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input,
        on_success=on_success,
        on_error=on_error,
        options=options or {},
        condition=None,
        routes=routes,
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


class TestProducerResolverBuild:
    def test_source_registers_as_producer_for_on_success(self):
        source = SourceSpec(plugin="csv", on_success="step1", options={}, on_validation_failure="discard")
        nodes = (_node("step1", plugin="t", input="step1", on_success="sink"),)
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"sink"}))

        producer = resolver.find_producer_for("step1")
        assert producer is not None
        assert producer.producer_id == "source"
        assert producer.plugin_name == "csv"

    def test_named_sources_register_stable_producer_ids(self):
        sources = {
            "customers": SourceSpec(plugin="csv", on_success="customer_rows", options={}, on_validation_failure="discard"),
            "orders": SourceSpec(plugin="json", on_success="order_rows", options={}, on_validation_failure="discard"),
        }
        resolver = ProducerResolver.build(source=None, sources=sources, nodes=(), sink_names=frozenset())

        customers = resolver.find_producer_for("customer_rows")
        orders = resolver.find_producer_for("order_rows")

        assert customers is not None
        assert customers.producer_id == "source:customers"
        assert customers.plugin_name == "csv"
        assert orders is not None
        assert orders.producer_id == "source:orders"
        assert orders.plugin_name == "json"

    def test_node_on_success_registers_producer(self):
        nodes = (_node("a", plugin="p1", input="src_out", on_success="b_in"), _node("b", plugin="p2", input="b_in", on_success="sink"))
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset({"sink"}))

        producer = resolver.find_producer_for("b_in")
        assert producer is not None
        assert producer.producer_id == "a"
        assert producer.plugin_name == "p1"

    def test_duplicate_producer_for_connection_is_recorded(self):
        nodes = (_node("a", plugin="p1", input="src", on_success="dup"), _node("b", plugin="p2", input="src", on_success="dup"))
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        assert "dup" in resolver.duplicate_connections
        assert resolver.find_producer_for("dup") is None  # ambiguous

    def test_routes_register_producers(self):
        nodes = (_node("g", plugin="gate1", node_type="gate", input="src", routes={"yes": "yes_out", "no": "no_out"}),)
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        for connection in ("yes_out", "no_out"):
            producer = resolver.find_producer_for(connection)
            assert producer is not None and producer.producer_id == "g"

    def test_fork_to_registers_producers(self):
        nodes = (_node("g", plugin="fork1", node_type="gate", input="src", fork_to=("a", "b")),)
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        for branch in ("a", "b"):
            assert resolver.find_producer_for(branch) is not None

    def test_fork_route_target_is_not_a_connection_producer(self):
        """Route target 'fork' is the reserved fork-mode keyword, not a connection.

        Two gates routing to 'fork' must not contend as duplicate producers
        (elspeth-b6940369a7); the DAG builder resolves 'fork' to
        RouteDestination.fork() and never registers it as a connection.
        """
        nodes = (
            _node("g1", plugin="gate1", node_type="gate", input="src", routes={"true": "fork"}, fork_to=("a1", "a2")),
            _node("g2", plugin="gate2", node_type="gate", input="mid", routes={"true": "fork"}, fork_to=("b1", "b2")),
        )
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        assert "fork" not in resolver.duplicate_connections
        assert resolver.find_producer_for("fork") is None

    def test_coalesce_without_on_success_publishes_under_own_id(self):
        nodes = (_node("c", plugin=None, node_type="coalesce", input="branches"),)
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        producer = resolver.find_producer_for("c")
        assert producer is not None and producer.producer_id == "c"


class TestProducerResolverWalkBack:
    def test_walk_through_gate_returns_real_producer(self):
        nodes = (
            _node("scrape", plugin="web_scrape", input="src_out", on_success="gate_in"),
            _node("g", plugin="gate1", node_type="gate", input="gate_in", on_success="explode_in"),
            _node("explode", plugin="line_explode", input="explode_in", on_success="sink"),
        )
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset({"sink"}))

        producer = resolver.walk_to_real_producer("explode_in")
        assert producer is not None
        assert producer.producer_id == "scrape"
        assert producer.plugin_name == "web_scrape"

    def test_walk_returns_none_on_routing_loop(self):
        nodes = (
            _node("g1", plugin=None, node_type="gate", input="loop_b", on_success="loop_a"),
            _node("g2", plugin=None, node_type="gate", input="loop_a", on_success="loop_b"),
        )
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        assert resolver.walk_to_real_producer("loop_a") is None

    def test_walk_returns_none_when_connection_is_duplicate(self):
        nodes = (_node("a", plugin="p1", input="src", on_success="dup"), _node("b", plugin="p2", input="src", on_success="dup"))
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        assert resolver.walk_to_real_producer("dup") is None

    def test_walk_returns_none_when_connection_unknown(self):
        nodes: tuple[NodeSpec, ...] = ()
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())

        assert resolver.walk_to_real_producer("nope") is None

    def test_walk_returns_source_producer_without_node_lookup(self):
        # Reviewer-found bug: walk_to_real_producer must NOT index
        # _node_by_id["source"]. Source producers must short-circuit
        # before any node-table lookup.
        source = SourceSpec(
            plugin="csv",
            on_success="step1",
            options={
                "path": "x.csv",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            on_validation_failure="quarantine",
        )
        resolver = ProducerResolver.build(
            source=source,
            nodes=(),
            sink_names=frozenset(),
        )
        producer = resolver.walk_to_real_producer("step1")
        assert producer is not None
        assert producer.producer_id == "source"
        assert producer.plugin_name == "csv"

    def test_walk_through_gate_to_source(self):
        source = SourceSpec(
            plugin="csv",
            on_success="gate_in",
            options={
                "path": "x.csv",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            on_validation_failure="quarantine",
        )
        nodes = (_node("g", plugin="gate1", node_type="gate", input="gate_in", on_success="explode_in"),)
        resolver = ProducerResolver.build(
            source=source,
            nodes=nodes,
            sink_names=frozenset(),
        )
        producer = resolver.walk_to_real_producer("explode_in")
        assert producer is not None
        assert producer.producer_id == "source"


def _queue(queue_id: str, *, description: str | None = None) -> NodeSpec:
    """Canonical structural queue NodeSpec: id == input, no plugin/routing,
    implicit output under its own id, description-only options."""
    return _node(
        queue_id,
        plugin=None,
        node_type="queue",
        input=queue_id,
        on_success=None,
        options=None if description is None else {"description": description},
    )


class TestProducerResolverQueue:
    """Declared queue fan-in (elspeth-a5b86149d4): many producers may publish
    one connection, the queue is that connection's canonical producer, and the
    predecessors are tracked separately from the ordinary single-producer map —
    without relaxing the duplicate-producer rule for undeclared fan-in."""

    @staticmethod
    def _two_sources() -> dict[str, SourceSpec]:
        return {
            "orders": SourceSpec(plugin="csv", on_success="inbound", options={}, on_validation_failure="discard"),
            "refunds": SourceSpec(plugin="csv", on_success="inbound", options={}, on_validation_failure="discard"),
        }

    def test_declared_queue_absorbs_fan_in_without_duplicate(self):
        resolver = ProducerResolver.build(source=None, sources=self._two_sources(), nodes=(_queue("inbound"),), sink_names=frozenset())
        assert "inbound" not in resolver.duplicate_connections

    def test_queue_is_the_canonical_producer_for_its_id(self):
        resolver = ProducerResolver.build(source=None, sources=self._two_sources(), nodes=(_queue("inbound"),), sink_names=frozenset())
        producer = resolver.find_producer_for("inbound")
        assert producer is not None
        assert producer.producer_id == "inbound"

    def test_queue_predecessors_are_sorted_and_insertion_order_independent(self):
        forward = ProducerResolver.build(source=None, sources=self._two_sources(), nodes=(_queue("inbound"),), sink_names=frozenset())
        reversed_sources = dict(reversed(list(self._two_sources().items())))
        backward = ProducerResolver.build(source=None, sources=reversed_sources, nodes=(_queue("inbound"),), sink_names=frozenset())
        for resolver in (forward, backward):
            assert [entry.producer_id for entry in resolver.queue_predecessors("inbound")] == [
                "source:orders",
                "source:refunds",
            ]

    def test_repeated_predecessor_registration_is_deduplicated(self):
        # One gate routing two labels to the queue is a single predecessor.
        gate = _node("g", plugin="gate", node_type="gate", input="src", routes={"a": "inbound", "b": "inbound"})
        src = {"s": SourceSpec(plugin="csv", on_success="src", options={}, on_validation_failure="discard")}
        resolver = ProducerResolver.build(source=None, sources=src, nodes=(gate, _queue("inbound")), sink_names=frozenset())
        assert [entry.producer_id for entry in resolver.queue_predecessors("inbound")] == ["g"]

    def test_walk_to_real_producer_returns_the_queue_not_a_predecessor(self):
        resolver = ProducerResolver.build(source=None, sources=self._two_sources(), nodes=(_queue("inbound"),), sink_names=frozenset())
        producer = resolver.walk_to_real_producer("inbound")
        assert producer is not None
        assert producer.producer_id == "inbound"

    def test_queue_predecessors_is_empty_tuple_for_a_non_queue_connection(self):
        nodes = (_node("a", plugin="p", input="src", on_success="out"),)
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset())
        assert resolver.queue_predecessors("out") == ()

    def test_undeclared_fan_in_still_reports_a_duplicate(self):
        resolver = ProducerResolver.build(source=None, sources=self._two_sources(), nodes=(), sink_names=frozenset())
        assert "inbound" in resolver.duplicate_connections
        assert resolver.find_producer_for("inbound") is None


class TestProducerResolverSinkProducers:
    """Direct-to-sink edges are kept OUT of the connection map, not discarded.

    They are still real producer->consumer edges. Before ``sink_producers``,
    the schema-contract validator rebuilt this map by hand from five call
    sites; every sink-side consumer had to mirror that walk or go blind.
    """

    def test_direct_sink_edge_is_recorded_but_not_in_the_connection_map(self):
        source = SourceSpec(plugin="csv", on_success="step1", options={}, on_validation_failure="discard")
        nodes = (_node("step1", plugin="t", input="step1", on_success="out"),)
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"out"}))

        assert resolver.find_producer_for("out") is None, "a sink is terminal — nothing walks back THROUGH it"
        producers = resolver.sink_producers("out")
        assert [producer.producer_id for producer in producers] == ["step1"]
        assert producers[0].plugin_name == "t"

    def test_unknown_or_unfed_sink_returns_empty(self):
        resolver = ProducerResolver.build(source=None, nodes=(), sink_names=frozenset({"out"}))
        assert resolver.sink_producers("out") == ()
        assert resolver.sink_producers("never_declared") == ()

    def test_source_routed_straight_at_a_sink_is_recorded(self):
        source = SourceSpec(plugin="csv", on_success="out", options={}, on_validation_failure="discard")
        resolver = ProducerResolver.build(source=source, nodes=(), sink_names=frozenset({"out"}))

        producers = resolver.sink_producers("out")
        assert [producer.producer_id for producer in producers] == ["source"]
        assert producers[0].plugin_name == "csv"

    def test_sink_fan_in_records_every_producer_in_order(self):
        source = SourceSpec(plugin="csv", on_success="step1", options={}, on_validation_failure="discard")
        nodes = (
            _node("step1", plugin="a", input="step1", on_success="out"),
            _node("step2", plugin="b", input="step1", on_success="out"),
        )
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"out"}))

        assert [producer.producer_id for producer in resolver.sink_producers("out")] == ["step1", "step2"]
        assert "out" not in resolver.duplicate_connections, "several producers writing to one sink is fan-in, not a contended connection"

    def test_error_route_to_a_sink_is_recorded_and_discard_is_not(self):
        source = SourceSpec(plugin="csv", on_success="step1", options={}, on_validation_failure="discard")
        nodes = (
            _node("step1", plugin="a", input="step1", on_success="ok", on_error="bad"),
            _node("step2", plugin="b", input="step1", on_success="ok", on_error="discard"),
        )
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"ok", "bad"}))

        assert [producer.producer_id for producer in resolver.sink_producers("bad")] == ["step1"]
        assert resolver.sink_producers("discard") == ()


class TestProducerResolverWalkEntry:
    """``walk_entry_to_real_producer`` is the entry form of the same traversal.

    Sink producers arrive as entries rather than connection names, because a
    sink-targeted edge never entered the connection map. Both entry points
    share one loop so gate traversal cannot drift between them.
    """

    def test_gate_producer_of_a_sink_walks_back_to_the_real_upstream(self):
        source = SourceSpec(plugin="csv", on_success="raw", options={}, on_validation_failure="discard")
        nodes = (
            _node("worker", plugin="t", input="raw", on_success="gate_in"),
            _node("router", plugin=None, node_type="gate", input="gate_in", routes={"pass": "out"}),
        )
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"out"}))

        direct = resolver.sink_producers("out")
        assert [producer.producer_id for producer in direct] == ["router"], "the IMMEDIATE producer is the gate"

        actual = resolver.walk_entry_to_real_producer(direct[0])
        assert actual is not None
        assert actual.producer_id == "worker", "the gate is structural — facts come from the node behind it"

    def test_walk_entry_returns_a_source_root_without_a_node_lookup(self):
        source = SourceSpec(plugin="csv", on_success="gate_in", options={}, on_validation_failure="discard")
        nodes = (_node("router", plugin=None, node_type="gate", input="gate_in", routes={"pass": "out"}),)
        resolver = ProducerResolver.build(source=source, nodes=nodes, sink_names=frozenset({"out"}))

        actual = resolver.walk_entry_to_real_producer(resolver.sink_producers("out")[0])
        assert actual is not None
        assert actual.producer_id == "source", "a source is not a NodeSpec; indexing the node table would KeyError"

    def test_walk_entry_abstains_on_a_routing_loop(self):
        nodes = (
            _node("gate_a", plugin=None, node_type="gate", input="b_out", routes={"pass": "a_out"}),
            _node("gate_b", plugin=None, node_type="gate", input="a_out", routes={"pass": "out"}),
        )
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset({"out"}))

        assert resolver.walk_entry_to_real_producer(resolver.sink_producers("out")[0]) is None


class TestPublishedSuccessConnection:
    """The one place the 'what does this node publish under?' rule is stated.

    Regression cover for the defect these tests exist to prevent: a reader
    asking ``node.on_success is not None`` instead of asking here reads a
    correctly-wired non-terminal coalesce (or any queue) as unconnected.
    That mistake shipped twice — as a false-positive W3 authoring warning,
    and as a missing outbound edge in the pipeline diagram, which drew a
    working fork/coalesce pipeline as two disconnected fragments.
    """

    def test_declared_on_success_is_the_published_connection(self):
        node = _node("step", plugin="t", input="raw", on_success="next")
        assert published_success_connection(node) == "next"

    def test_non_terminal_coalesce_publishes_under_its_own_id(self):
        node = _node("merge", plugin=None, node_type="coalesce", input="a_out")
        assert node.on_success is None, "the shape under test: on_success omitted"
        assert published_success_connection(node) == "merge"

    def test_terminal_coalesce_publishes_under_its_declared_sink(self):
        node = _node("merge", plugin=None, node_type="coalesce", input="a_out", on_success="main")
        assert published_success_connection(node) == "main", "a declared on_success always wins"

    def test_aggregation_without_on_success_publishes_under_its_own_id(self):
        """Missed on the first pass of this helper — the authority is builder.py.

        ``AggregationSettings.on_success`` is ``str | None = None``; when it is
        omitted ``core/dag/builder.py`` registers ``agg_settings.name`` as the
        producer. Excluding aggregation made the composer reject a pipeline the
        runtime builds and runs.
        """
        node = _node("agg", plugin="row_batcher", node_type="aggregation", input="rows")
        assert node.on_success is None, "the shape under test: on_success omitted"
        assert published_success_connection(node) == "agg"

    def test_aggregation_with_on_success_publishes_under_it(self):
        node = _node("agg", plugin="row_batcher", node_type="aggregation", input="rows", on_success="next")
        assert published_success_connection(node) == "next"

    def test_queue_publishes_under_its_own_id(self):
        node = _node("inbound", plugin=None, node_type="queue", input="inbound")
        assert published_success_connection(node) == "inbound"

    def test_a_fork_gate_publishes_nothing_on_its_success_channel(self):
        node = _node(
            "fan_out",
            plugin=None,
            node_type="gate",
            input="rows",
            routes={"true": "fork", "false": "fork"},
            fork_to=("left", "right"),
        )
        assert published_success_connection(node) is None, "routes/fork_to describe a gate's output, not on_success"

    def test_row_union_and_collector_never_publish_implicitly(self):
        """Both REQUIRE on_success, so neither may be given an implicit id.

        Inventing one would name a connection the DAG builder cannot resolve.
        """
        for node_type in ("row_union", "collector"):
            node = _node("closer", plugin=None, node_type=node_type, input="a_out")
            assert published_success_connection(node) is None, node_type

    def test_a_downstream_input_naming_a_non_terminal_coalesce_resolves_to_it(self):
        """The end-to-end rule: `input: "<coalesce id>"` finds the coalesce."""
        nodes = (
            _node("merge", plugin=None, node_type="coalesce", input="a_out"),
            _node("select", plugin="field_mapper", input="merge", on_success="out"),
        )
        resolver = ProducerResolver.build(source=None, nodes=nodes, sink_names=frozenset({"out"}))

        producer = resolver.find_producer_for("merge")
        assert producer is not None, "the coalesce must be findable under its own id"
        assert producer.producer_id == "merge"


class TestRuntimeConnectionTargetsDerivesFromItsAuthority:
    """Pin ``state.py``'s reachable-target site to the authority it must not restate.

    ``_runtime_connection_targets`` is the one site that must NOT call
    ``published_success_connection``: a queue's ``input`` IS its own id
    (``queue_node_contract_error`` enforces it), so adding a queue's id to the
    reachable TARGET set would let an orphan queue satisfy its own input and
    silently delete the ``node_input_not_reachable`` check the function exists
    to make possible.

    That carve-out is correct. The way it was EXPRESSED was not: the site
    hand-wrote the surviving subset as an inline literal, and the literal
    drifted — it listed only ``("coalesce",)``, so the composer rejected a
    pipeline the runtime builds and runs. An earlier version of this guard read
    that literal out of the AST and compared it to ``helper - {"queue"}``,
    which caught the drift but left the restatement in place.

    The site now DERIVES the subset from
    ``_SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS``, so that class of drift is
    structurally impossible rather than merely watched. This guard was
    re-anchored onto the derivation rather than deleted, and it watches strictly
    more than it did:

    * the site still has exactly ONE ``node.node_type in ...`` membership test
      (a second hand-written arm is the way a derivation gets quietly
      supplemented);
    * that test's operand is a NAME, not a literal, and the name is imported
      from the authority module rather than rebound locally;
    * the authority's own definition is a subtraction from the helper of
      exactly ``{"queue"}``, not a second hand-written set; and
    * every kind the derivation admits is really treated as a reachable target,
      and a queue really is not.
    """

    @staticmethod
    def _target_function() -> ast.FunctionDef:
        """Return the single ``_runtime_connection_targets`` definition in state.py.

        Located by STRUCTURE, never by line number.
        """
        module = ast.parse(_STATE_SOURCE_PATH.read_text(encoding="utf-8"))
        functions = [node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == "_runtime_connection_targets"]
        assert len(functions) == 1, (
            f"Expected exactly one `_runtime_connection_targets` definition in {_STATE_SOURCE_PATH.name}, "
            f"found {len(functions)}. This guard's anchor is ambiguous — fix the anchor, do not delete the guard."
        )
        return functions[0]

    @classmethod
    def _self_publishing_membership_test(cls) -> ast.expr:
        """Return the operand of the single ``node.node_type in ...`` test.

        The function's other comparisons are ``!=`` against discard keywords,
        so this shape is unambiguous — and the count assertion below proves it
        stayed that way. A SECOND membership arm would mean the derivation had
        been supplemented by hand, which is the drift this site is fixed
        against, so the count is part of what the guard asserts rather than
        merely how it navigates.
        """
        candidates = [
            node
            for node in ast.walk(cls._target_function())
            if isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "node_type"
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "node"
        ]
        assert len(candidates) == 1, (
            f"Expected exactly one `node.node_type in ...` test inside `_runtime_connection_targets`, "
            f"found {len(candidates)}. Either the derived membership test was removed — and this guard is "
            f"no longer watching anything — or a second arm was hand-written alongside it, which reopens "
            f"the drift the derivation closes. Re-anchor this guard, do not delete it."
        )
        return candidates[0].comparators[0]

    def test_the_site_derives_the_subset_instead_of_enumerating_it(self):
        operand = self._self_publishing_membership_test()
        assert isinstance(operand, ast.Name), (
            f"`_runtime_connection_targets` tests `node.node_type` against an inline "
            f"{type(operand).__name__}, not a name. Enumerating the implicit-publisher subset here IS the "
            f"original defect: the literal listed only `coalesce`, drifted from `{_AUTHORITY_NAME}`, and the "
            f"composer rejected a pipeline the runtime builds and runs with `node_input_not_reachable` on "
            f"the aggregation's consumer. Use `{_DERIVED_NAME}` rather than restating the subset."
        )
        assert operand.id == _DERIVED_NAME, (
            f"`_runtime_connection_targets` tests `node.node_type` against `{operand.id}`, but the derived "
            f"subset of implicit self-publishers is `{_DERIVED_NAME}`. If a different name is now the "
            f"authority for what a reachability check may treat as a target, re-point this guard at it — "
            f"do not leave the site pinned to nothing."
        )

    def test_the_derived_name_is_imported_from_the_authority_module(self):
        """A local rebinding of the same name passes the check above.

        Without this, ``_SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS = {"coalesce"}``
        written inside ``state.py`` would satisfy every structural assertion
        while being exactly the hand-written twin the derivation removes.
        """
        imported = {
            alias.asname or alias.name
            for node in ast.walk(self._target_function())
            if isinstance(node, ast.ImportFrom) and node.module == _AUTHORITY_MODULE
            for alias in node.names
        }
        assert _DERIVED_NAME in imported, (
            f"`_runtime_connection_targets` uses `{_DERIVED_NAME}` but does not import it from "
            f"`{_AUTHORITY_MODULE}` (names it imports from there: {sorted(imported) or 'none'}). A name bound "
            f"anywhere else is a hand-written twin wearing the authority's name, which is the drift this "
            f"site is fixed against."
        )

    def test_the_authority_derives_the_subset_by_subtracting_exactly_queue(self):
        """The derivation itself must be a subtraction, not a second literal.

        ``_SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS = {"coalesce", "aggregation"}``
        would satisfy every runtime assertion in this class today and drift the
        moment a fourth kind is added — the same defect, moved one file over.
        """
        module = ast.parse(_PRODUCER_RESOLVER_SOURCE_PATH.read_text(encoding="utf-8"))
        assignments = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == _DERIVED_NAME for target in node.targets)
        ]
        assert len(assignments) == 1, (
            f"Expected exactly one assignment to `{_DERIVED_NAME}` in {_PRODUCER_RESOLVER_SOURCE_PATH.name}, found {len(assignments)}."
        )
        value = assignments[0].value
        assert isinstance(value, ast.BinOp) and isinstance(value.op, ast.Sub), (
            f"`{_DERIVED_NAME}` is assigned a {type(value).__name__}, not a subtraction from "
            f"`{_AUTHORITY_NAME}`. It must be DERIVED so a fourth implicit-publisher kind lands here "
            f"without an edit; anything else is the hand-written twin one file further away."
        )
        assert isinstance(value.left, ast.Name) and value.left.id == _AUTHORITY_NAME, (
            f"`{_DERIVED_NAME}` subtracts from {ast.dump(value.left)}, not from `{_AUTHORITY_NAME}`."
        )
        assert isinstance(value.right, ast.Set), (
            f"`{_DERIVED_NAME}` subtracts a {type(value.right).__name__}; this guard can only verify a set literal of node-type names."
        )
        excluded = set()
        for element in value.right.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                f"Non-literal element {ast.dump(element)} in the exclusion set — this guard cannot evaluate it."
            )
            excluded.add(element.value)
        assert excluded == {"queue"}, (
            f"The exclusion set is {sorted(excluded)}, expected ['queue']. `queue` is the sole, deliberate "
            f"exclusion: its `input` is its own id, so listing it as a reachable target lets an orphan queue "
            f"satisfy its own input and deletes `node_input_not_reachable`. If a NEW kind also needs "
            f"excluding, widen this guard with the reason — and if `queue` left the set, say why here."
        )

    def test_queue_is_excluded_at_the_site_but_present_in_the_helper(self):
        """The carve-out itself, stated positively so its removal is visible."""
        assert "queue" in _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, (
            "A queue publishes under its own id — if it left the helper, `published_success_connection` "
            "no longer describes the DAG builder's producer registration."
        )
        assert "queue" not in _SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS, (
            "`queue` reached the subset `_runtime_connection_targets` walks. A queue's `input` IS its own id, "
            "so a queue's id in the reachable TARGET set lets an orphan queue satisfy its own input — "
            "silently deleting the `node_input_not_reachable` check that function exists to make possible."
        )

    @pytest.mark.parametrize("node_type", sorted(_SELF_PUBLISHING_KINDS_REACHABLE_AS_TARGETS))
    def test_every_admitted_kind_is_really_reached_as_a_target(self, node_type: str) -> None:
        """The behavioural half: the derivation is not merely shaped right.

        Enumerated FROM the derived constant, so a fourth implicit-publisher
        kind arrives here with a passing test rather than with an absence of
        red.
        """
        node = _node("implicit_publisher", plugin=None, node_type=node_type, input="upstream")

        assert "implicit_publisher" in _runtime_connection_targets({}, (node,)), (
            f"A '{node_type}' node with no `on_success` publishes under its own id — `core/dag/builder.py` "
            f"registers it by name — but `_runtime_connection_targets` did not treat that id as reachable. "
            f"Its consumer would be rejected with `node_input_not_reachable` on a pipeline the runtime runs."
        )

    def test_a_queue_id_is_never_reached_as_a_target(self) -> None:
        """The negative the carve-out exists for, stated behaviourally."""
        orphan_queue = _node("q", plugin=None, node_type="queue", input="q")

        assert "q" not in _runtime_connection_targets({}, (orphan_queue,)), (
            "An orphan queue's own id reached the target set, so the queue satisfies its own `input` and "
            "`node_input_not_reachable` can never fire for it."
        )


class TestEveryNodeKindIsAdjudicatedForImplicitPublishing:
    """No node kind may sit in or out of the self-publisher set unadjudicated.

    ``aggregation`` was missing from ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES``
    not because anyone decided it did not belong, but because nobody decided
    anything about it — the set was written from the two kinds in view, and the
    comment enumerating the exclusions read as considered while never having
    considered aggregation at all. Silence is the failure mode this test
    removes; the AST guard above pins ONE site against the set, this pins the
    set against the runtime.

    Membership is DERIVED from the runtime settings models, which is where the
    fact actually lives (``core/dag/builder.py`` registers a producer under the
    node's own name exactly when ``on_success`` is absent from the config).
    Each composer node kind must land in exactly one population:

    * ``on_success`` REQUIRED   -> never publishes implicitly, must be OUT;
    * ``on_success`` OPTIONAL   -> publishes under its own id when omitted,
      must be IN;
    * ``on_success`` ABSENT from the model -> the kind does not have a success
      channel at all, and only an explicit ruling below can place it.

    A new node kind, or an existing kind whose ``on_success`` optionality
    changes, lands in no population and fails here with the ways to discharge
    it. That is the point: this test would have failed on the day
    ``aggregation`` was left out.
    """

    # The two kinds whose runtime model has NO ``on_success`` field, each with
    # the ruling that places it. Field-absence alone cannot decide membership,
    # so these are adjudicated by hand and by hand ONLY here.
    _ABSENT_FIELD_RULINGS: ClassVar[dict[str, bool]] = {
        # A queue never declares on_success; its id IS the connection its
        # predecessors publish to and its consumers read from.
        "queue": True,
        # A gate's output is described by routes / fork_to, not by a success
        # channel — giving it an implicit id would invent a connection the DAG
        # builder does not resolve.
        "gate": False,
    }

    @staticmethod
    def _runtime_settings_by_node_type() -> dict[str, type]:
        return {
            "transform": TransformSettings,
            "aggregation": AggregationSettings,
            "coalesce": CoalesceSettings,
            "row_union": RowUnionSettings,
            "collector": CollectorSettings,
            "queue": QueueSettings,
            "gate": GateSettings,
        }

    def test_every_composer_node_kind_has_a_runtime_model_to_derive_from(self) -> None:
        """The coverage gate: a new composer kind cannot skip adjudication."""
        mapped = set(self._runtime_settings_by_node_type())
        assert mapped == set(COMPOSER_NODE_TYPES), (
            "Composer node kinds without a runtime settings model mapped here: "
            f"{sorted(set(COMPOSER_NODE_TYPES) - mapped)}; mapped kinds the composer does not define: "
            f"{sorted(mapped - set(COMPOSER_NODE_TYPES))}. Map the kind to its runtime settings class so "
            "the test below can derive whether it publishes implicitly."
        )

    def test_every_node_kind_is_adjudicated_for_implicit_publishing(self) -> None:
        should_publish: dict[str, bool] = {}
        for node_type, settings_cls in self._runtime_settings_by_node_type().items():
            field = settings_cls.model_fields.get("on_success")
            if field is None:
                assert node_type in self._ABSENT_FIELD_RULINGS, (
                    f"'{node_type}' has no `on_success` field in {settings_cls.__name__}, so optionality "
                    "cannot decide whether it publishes under its own id. Add an explicit ruling to "
                    "_ABSENT_FIELD_RULINGS with the reason, the way queue and gate carry one."
                )
                should_publish[node_type] = self._ABSENT_FIELD_RULINGS[node_type]
                continue
            # Required => the author must always name a target, so the kind can
            # never fall back to its own id. Optional => omitting it is exactly
            # the case builder.py registers under the node's own name.
            should_publish[node_type] = not field.is_required()

        expected = {node_type for node_type, publishes in should_publish.items() if publishes}
        actual = set(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES)

        assert actual == expected, (
            "`_IMPLICIT_SELF_PUBLISHING_NODE_TYPES` has drifted from the runtime models it describes.\n"
            f"  missing (runtime says these publish implicitly): {sorted(expected - actual)}\n"
            f"  extra   (runtime says these do NOT):             {sorted(actual - expected)}\n"
            "A kind whose runtime `on_success` is OPTIONAL publishes under its own id when the author "
            "omits it — `core/dag/builder.py` registers it by name — so the composer must say so too, or "
            "it will reject a pipeline the runtime builds and runs. This is the exact defect that shipped "
            "when `aggregation` was left out."
        )
