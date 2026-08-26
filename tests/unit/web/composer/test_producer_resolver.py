"""Tests for the shared producer-map resolver primitive."""

from __future__ import annotations

import ast
from pathlib import Path

import elspeth
from elspeth.web.composer._producer_resolver import (
    _IMPLICIT_SELF_PUBLISHING_NODE_TYPES,
    ProducerResolver,
    published_success_connection,
)
from elspeth.web.composer.state import NodeSpec, SourceSpec

_STATE_SOURCE_PATH = Path(elspeth.__file__).parent / "web" / "composer" / "state.py"


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


class TestRuntimeConnectionTargetsRestatement:
    """Pin ``state.py``'s DELIBERATE hand-written twin to the helper it excludes from.

    ``_runtime_connection_targets`` is the one site that must NOT call
    ``published_success_connection``: a queue's ``input`` IS its own id
    (``queue_node_contract_error`` enforces it), so adding a queue's id to the
    reachable TARGET set would let an orphan queue satisfy its own input and
    silently delete the ``node_input_not_reachable`` check the function exists
    to make possible.

    That carve-out is correct, but until this guard it was UNPINNED IN BOTH
    DIRECTIONS: nothing in ``tests/`` referenced either name, so the next kind
    added to ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` would not fail a single
    test for being absent at that site. That is exactly how ``aggregation``
    was missed the first time — the hand-written twin listed only
    ``("coalesce",)`` and the composer rejected a pipeline the runtime runs.

    So the expectation is DERIVED, not restated: the literal enumerated at the
    site must equal ``helper - {"queue"}``. Neither side may drift alone, and
    the site may not quietly disappear (the anchor assertions below).
    """

    @staticmethod
    def _self_publishing_membership_test() -> ast.Compare:
        """Return the single ``node.node_type in (...)`` test in the function.

        Located by STRUCTURE, never by line number: the enclosing
        ``FunctionDef`` by name, then the ``ast.Compare`` whose operator is
        ``in`` and whose left operand is the ``node.node_type`` attribute
        access. The function's other comparisons are ``!=`` against discard
        keywords, so this shape is unambiguous — and the count assertion
        below proves it stayed that way.
        """
        module = ast.parse(_STATE_SOURCE_PATH.read_text(encoding="utf-8"))
        functions = [node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == "_runtime_connection_targets"]
        assert len(functions) == 1, (
            f"Expected exactly one `_runtime_connection_targets` definition in {_STATE_SOURCE_PATH.name}, "
            f"found {len(functions)}. This guard's anchor is ambiguous — fix the anchor, do not delete the guard."
        )
        candidates = [
            node
            for node in ast.walk(functions[0])
            if isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "node_type"
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "node"
        ]
        assert len(candidates) == 1, (
            f"Expected exactly one `node.node_type in (...)` test inside `_runtime_connection_targets`, "
            f"found {len(candidates)}. If the deliberate hand-written restatement of the implicit "
            f"self-publisher set was removed or duplicated, this guard is no longer watching it — "
            f"re-anchor it, do not delete it."
        )
        return candidates[0]

    def test_the_hand_written_twin_equals_the_helper_minus_queue(self):
        compare = self._self_publishing_membership_test()
        comparator = compare.comparators[0]
        assert isinstance(comparator, ast.Tuple | ast.Set | ast.List), (
            "Expected an inline literal of node-type names at the restatement site, got "
            f"{type(comparator).__name__}. A guard can only pin a literal it can read."
        )
        enumerated = set()
        for element in comparator.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                f"Non-literal element {ast.dump(element)} at the restatement site — this guard cannot "
                "evaluate it. Keep the site a plain literal, or derive it from the helper directly."
            )
            enumerated.add(element.value)

        expected = set(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES) - {"queue"}
        assert enumerated == expected, (
            "`_runtime_connection_targets` has drifted from its authority.\n"
            f"  helper `_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`: {sorted(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES)}\n"
            f"  expected at the site (helper - {{'queue'}}):     {sorted(expected)}\n"
            f"  actually enumerated at the site:                {sorted(enumerated)}\n"
            "A kind that publishes implicitly must be listed at BOTH places. `queue` is the sole, "
            "deliberate exclusion: its `input` is its own id, so listing it there would let an orphan "
            "queue satisfy its own input and delete `node_input_not_reachable`. If a NEW kind also needs "
            "excluding, widen the exclusion here with the reason — do not silently drop it from the site."
        )

    def test_queue_is_excluded_at_the_site_but_present_in_the_helper(self):
        """The carve-out itself, stated positively so its removal is visible."""
        assert "queue" in _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, (
            "A queue publishes under its own id — if it left the helper, `published_success_connection` "
            "no longer describes the DAG builder's producer registration."
        )
        compare = self._self_publishing_membership_test()
        enumerated = {element.value for element in compare.comparators[0].elts if isinstance(element, ast.Constant)}
        assert "queue" not in enumerated, (
            "`queue` was added to `_runtime_connection_targets`'s literal. A queue's `input` IS its own id, "
            "so a queue's id in the reachable TARGET set lets an orphan queue satisfy its own input — "
            "silently deleting the `node_input_not_reachable` check this function exists to make possible."
        )
