"""Shared producer-map and walk-back primitive.

Both the schema-contract validator and the semantic-contract validator
need to: (1) build a map from connection name to producer node, (2) record
which producers write DIRECTLY to each sink, and (3) walk back through
structural gates to find the real producer. This module provides the single
implementation of all three. Pass-through propagation is intentionally NOT
included — that remains schema-specific in state.py because semantic
validation does not propagate through pass-through transforms in Phase 1.

Sink-targeted edges are kept out of ``_producer_map`` on purpose: a sink is
a terminal, so nothing downstream may walk back "through" it, and a sink
name colliding with a connection name is a separate validation error. They
are not DISCARDED, though — they land in ``_sink_producers``, keyed by sink
name and holding a LIST because a sink legitimately has fan-in (several
nodes routing to one output). ``sink_producers()`` is how a sink-side
validator reaches them.

Layer: L3 (web composer application code). Imports state types from
the same layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from elspeth.web.composer.state import NodeSpec, SourceSpec


def source_producer_id(source_name: str) -> str:
    """Return the stable producer id for a named composer source."""
    return "source" if source_name == "source" else f"source:{source_name}"


def is_source_producer_id(producer_id: str) -> bool:
    """Return whether a producer id represents a source root."""
    return producer_id == "source" or producer_id.startswith("source:")


@dataclass(frozen=True, slots=True)
class ProducerEntry:
    """A producer registered against one or more connection names.

    options is the producer's raw options Mapping — NOT deep-frozen here
    because state.py already deep-freezes node options in __post_init__.
    """

    producer_id: str
    plugin_name: str | None
    options: Mapping[str, Any]


class ProducerResolver:
    """Builds and queries the connection -> producer map for a composition.

    Construction is via ``build(...)`` rather than ``__init__`` so the
    primitive can compute and report duplicates as part of its result.
    Once built, ``find_producer_for`` and ``walk_to_real_producer`` are
    pure functions of the resolver state.

    NOT a frozen dataclass: holds derived dicts/sets that are
    construction-time fixed but expensive to deep-freeze. Treat as
    effectively immutable — do not mutate the public attributes.
    """

    __slots__ = (
        "_node_by_id",
        "_producer_map",
        "_queue_predecessors",
        "_sink_producers",
        "duplicate_connections",
    )

    def __init__(
        self,
        producer_map: dict[str, ProducerEntry],
        node_by_id: dict[str, NodeSpec],
        duplicate_connections: frozenset[str],
        queue_predecessors: Mapping[str, tuple[ProducerEntry, ...]] | None = None,
        sink_producers: Mapping[str, tuple[ProducerEntry, ...]] | None = None,
    ) -> None:
        self._producer_map = producer_map
        self._node_by_id = node_by_id
        self.duplicate_connections = duplicate_connections
        # Queue fan-in predecessors, keyed by queue id, tuple sorted by
        # producer_id (elspeth-a5b86149d4). Absent for queue-free compositions;
        # every ordinary connection resolves through _producer_map as before.
        self._queue_predecessors: Mapping[str, tuple[ProducerEntry, ...]] = queue_predecessors or {}
        # Direct-to-sink producers, keyed by sink name, in registration order.
        # NOT deduplicated: two route labels from one gate onto one sink are
        # two entries, exactly as the schema-contract layer recorded them
        # before this map moved here. Both consumers dedupe on the resolved
        # producer, so preserving the raw order keeps that behaviour identical.
        self._sink_producers: Mapping[str, tuple[ProducerEntry, ...]] = sink_producers or {}

    @classmethod
    def build(
        cls,
        *,
        source: SourceSpec | None,
        sources: Mapping[str, SourceSpec] | None = None,
        nodes: tuple[NodeSpec, ...],
        sink_names: frozenset[str],
    ) -> ProducerResolver:
        producer_map: dict[str, ProducerEntry] = {}
        duplicates: set[str] = set()
        sink_producers: dict[str, list[ProducerEntry]] = {}
        node_by_id = {node.id: node for node in nodes}

        # Declared queue fan-in (elspeth-a5b86149d4): a queue's id names the
        # connection its upstream producers publish to. Discover every queue
        # before registering producers so their fan-in is routed into a
        # per-queue predecessor set instead of contending in producer_map.
        queue_nodes = {node.id: node for node in nodes if node.node_type == "queue"}
        queue_predecessors: dict[str, dict[str, ProducerEntry]] = {queue_id: {} for queue_id in queue_nodes}

        # Spec §7 rule 9 (Task 11): on_error may name an ENCLOSING bound
        # region's closer (coalesce/row_union) instead of a sink. That is a
        # DIVERT edge into an EXISTING node, not a claim to PRODUCE a
        # connection under the closer's name — a non-terminal closer already
        # registers its own id as a producer (line below), so treating the
        # on_error target the same way would falsely collide the two
        # distinct nodes as duplicate producers of one connection. Closer
        # names are therefore excluded from on_error's producer
        # registration below, same carve-out shape as sinks/discard/None.
        closer_names = {node.id for node in nodes if node.node_type in ("coalesce", "row_union")}

        def register(connection_name: str | None, entry: ProducerEntry) -> None:
            if connection_name is None or connection_name == "discard":
                return
            if connection_name in sink_names:
                # A sink is terminal: nothing downstream walks back THROUGH
                # it, so the edge never enters producer_map. It is still a
                # real producer->consumer edge, so record it here — this is
                # the map every sink-side validator reads.
                sink_producers.setdefault(connection_name, []).append(entry)
                return
            if connection_name in queue_nodes and entry.producer_id != connection_name:
                # A producer publishing to a declared queue is a queue
                # predecessor, not an ordinary single-producer registration —
                # so declared fan-in never trips the duplicate rule. Dedup by
                # producer_id (a gate routing two labels to one queue is one
                # predecessor). The queue itself is installed as the canonical
                # producer for its id after the ordinary scan.
                queue_predecessors[connection_name].setdefault(entry.producer_id, entry)
                return
            if connection_name in producer_map:
                # Same node registering multiple times against the same
                # connection (e.g. a gate with two route labels both
                # mapping to the same target) is idempotent, not a
                # duplicate. Only record duplicates when distinct
                # producers contend for the connection.
                if producer_map[connection_name].producer_id == entry.producer_id:
                    return
                duplicates.add(connection_name)
                return
            producer_map[connection_name] = entry

        source_map = dict(sources or {})
        if not source_map and source is not None:
            source_map["source"] = source

        for source_name, source_entry in source_map.items():
            register(
                source_entry.on_success,
                ProducerEntry(
                    producer_id=source_producer_id(source_name),
                    plugin_name=source_entry.plugin,
                    options=source_entry.options,
                ),
            )

        for node in nodes:
            entry = ProducerEntry(
                producer_id=node.id,
                plugin_name=node.plugin,
                options=node.options,
            )
            if node.node_type == "coalesce" and node.on_success is None:
                register(node.id, entry)
            else:
                register(node.on_success, entry)
            if node.on_error not in closer_names:
                register(node.on_error, entry)
            if node.routes is not None:
                for target in node.routes.values():
                    if target == "fork":
                        # Reserved fork-mode keyword: the DAG builder resolves it
                        # to RouteDestination.fork(), never a connection, so any
                        # number of fork gates may use it without contending.
                        continue
                    register(target, entry)
            if node.fork_to is not None:
                for target in node.fork_to:
                    register(target, entry)

        # Install each queue as the canonical, observed-schema producer of its
        # own connection id. The queue publishes implicitly under its id
        # (on_success is None), so it never registered itself in the loop above;
        # downstream walk-back therefore stops at the queue (not an arbitrary
        # predecessor). Freeze predecessors as producer_id-sorted tuples so the
        # result is independent of source/node insertion order.
        for queue_id, queue_node in queue_nodes.items():
            producer_map[queue_id] = ProducerEntry(
                producer_id=queue_id,
                plugin_name=queue_node.plugin,
                options=queue_node.options,
            )
        frozen_predecessors = {
            queue_id: tuple(sorted(entries.values(), key=lambda entry: entry.producer_id))
            for queue_id, entries in queue_predecessors.items()
        }

        return cls(
            producer_map,
            node_by_id,
            frozenset(duplicates),
            frozen_predecessors,
            {sink_name: tuple(entries) for sink_name, entries in sink_producers.items()},
        )

    def find_producer_for(self, connection_name: str) -> ProducerEntry | None:
        """Return the immediate producer for a connection, or None.

        Returns None for: unknown connection, duplicate (ambiguous)
        connection, or a connection produced only by a direct-to-sink edge.
        """
        if connection_name in self.duplicate_connections:
            return None
        if connection_name not in self._producer_map:
            return None
        return self._producer_map[connection_name]

    def queue_predecessors(self, queue_id: str) -> tuple[ProducerEntry, ...]:
        """Return the producers feeding a declared queue, sorted by producer_id.

        Empty tuple for a non-queue connection or an unknown id. These are the
        queue's upstream fan-in predecessors — deliberately kept OUT of the
        ordinary single-producer map so declared fan-in does not read as a
        duplicate. Every predecessor-aware walker (fanout, prompt-shield) must
        traverse all of these conservatively rather than the single canonical
        producer.
        """
        return self._queue_predecessors.get(queue_id, ())

    def sink_producers(self, sink_name: str) -> tuple[ProducerEntry, ...]:
        """Return the producers writing DIRECTLY to a sink, in registration order.

        Empty tuple for an unknown or unfed sink name. These are the IMMEDIATE
        producers — a gate routing to the sink is returned as the gate. Feed
        each through ``walk_entry_to_real_producer`` to reach the real upstream.

        A sink can have several producers (fan-in), which is why this returns a
        tuple rather than a single entry. Any consumer-side rule must be
        conservative across all of them.
        """
        return self._sink_producers.get(sink_name, ())

    def walk_to_real_producer(self, connection_name: str) -> ProducerEntry | None:
        """Walk back through structural gates to the true producer of a connection.

        Returns None on: unknown connection, duplicate connection, or a
        routing loop. Structural producers that are not gates (coalesce,
        queue, row_union) are RETURNED as themselves — their branch semantics
        belong to callers that need them, and their ``plugin_name`` is None,
        so a plugin probe reads them as "no declared producer".

        Sink-targeted connections are absent from the connection map by
        construction; reach those through ``sink_producers``.
        """
        if connection_name in self.duplicate_connections:
            return None
        if connection_name not in self._producer_map:
            return None
        # Seed the loop guard with the starting connection so a route that
        # cycles back to it is detected on the first repeat.
        return self._walk_back(self._producer_map[connection_name], visited={connection_name})

    def walk_entry_to_real_producer(self, producer: ProducerEntry) -> ProducerEntry | None:
        """Walk back through structural gates from an already-resolved producer.

        The entry form of ``walk_to_real_producer``, for producers that were
        never reached through the connection map — direct-to-sink edges, which
        are deliberately kept out of it. Same traversal, same stopping rules.
        """
        return self._walk_back(producer, visited=set())

    def _walk_back(self, producer: ProducerEntry, *, visited: set[str]) -> ProducerEntry | None:
        """Shared gate traversal for both walk entry points.

        Source producers (producer_id == "source" or "source:<name>") return
        immediately WITHOUT a node-table lookup. The source is registered in
        _producer_map but is intentionally absent from _node_by_id (it is not
        a NodeSpec). Any code path that called _node_by_id[producer.producer_id]
        for a source would raise KeyError — the short-circuit is load-bearing.
        """
        current = producer
        while True:
            if is_source_producer_id(current.producer_id):
                return current
            producer_node = self._node_by_id[current.producer_id]
            if producer_node.node_type != "gate":
                return current
            connection_name = producer_node.input
            if connection_name in visited:
                return None
            visited.add(connection_name)
            next_producer = self.find_producer_for(connection_name)
            if next_producer is None:
                return None
            current = next_producer

    def get_node(self, node_id: str) -> NodeSpec | None:
        """Return the registered NodeSpec for a producer id, or None.

        Returns None when the id is a source producer (sources are
        intentionally not NodeSpecs) or when the id is unknown. Schema-contract code
        interpreting source-as-producer must short-circuit on None
        rather than indexing the underlying dict.
        """
        if node_id not in self._node_by_id:
            return None
        return self._node_by_id[node_id]
