# tests/unit/web/composer/test_schema_contract_walk_cost.py
"""elspeth-e5a38115a6 — Stage 1's schema-contract walk does linear work in
chain length.

Measured before the fix (release/0.8.0 @ e8998f20a, in-process): one
``CompositionState.validate`` over a chain of n pass-through llm transforms
ran ``_effective_producer_vote`` n(n+1)/2 times — 820 at n=40 — and
constructed a transform on every one of them, because the pass-through vote
recurses upstream (``_connection_propagation_vote`` →
``_producer_entry_propagation_vote`` → ``_effective_producer_vote``) with no
memo, so edge k re-derives all k upstream votes. The composer's review-debt
check (``unsurfaceable_pending_interpretation_review_sites``) runs that
validate four times per import/seed, which is how an 80-node import stalled
the whole event loop for ~50 s.

The oracle is the CONSTRUCTION COUNT, not wall time: the tracked plugin
manager records every ``create_transform`` the walk makes. A chain has
exactly one edge per node, so every per-node and per-edge probe site is
linear and the second difference of the count across n=10/20/40 is ZERO.
Any site that walks upstream without a memo — the old vote, or a new one —
makes the second difference positive, which is what this pin refuses.

The per-node constant is pinned EXACTLY (elspeth-97b15928bc): every probe
site reads the node's one ``ValidationProbeCache`` instance, so a validate
constructs each transform once and the review-debt check — four validates
per import — constructs it four times. Before the cache the six sites
measured 5.9 per validate and ~24 per check, and an 80-node import sat
0.8 s from the shared 5 s preflight bound. A site that constructs on its
own again shows up here as a per-node count above the pin.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.composer.service import unsurfaceable_pending_interpretation_review_sites
from elspeth.web.composer.state import CompositionState
from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager, real_plugin_manager

# Exact, not a ceiling: one shared probe instance per node per validate.
_CONSTRUCTIONS_PER_NODE_PER_VALIDATE = 1
# The review-debt check derives the source demand twice (site enumerator and
# surface-args projection) and each derivation validates the baseline and
# the H_all stamp: four validates, so four per node.
_CONSTRUCTIONS_PER_NODE_PER_CHECK = 4


def _chain_state(length: int) -> CompositionState:
    """``source -> llm_1 -> ... -> llm_n -> results``, every llm profile-bound
    and pass-through, the same shape as the 80-node e2e fixture."""
    ids = [f"llm_stage_{index + 1:03d}" for index in range(length)]
    nodes: list[dict[str, Any]] = [
        {
            "id": ids[index],
            "node_type": "transform",
            "plugin": "llm",
            "input": "source" if index == 0 else ids[index - 1],
            "on_success": "results" if index == length - 1 else ids[index + 1],
            "on_error": "discard",
            "options": {
                "profile": "e2e-bedrock",
                "prompt_template": "Review category {{ row.category }} and return a concise classification.",
                "required_input_fields": ["category"],
                "response_field": "review",
                "schema": {"mode": "observed"},
            },
        }
        for index in range(length)
    ]
    return CompositionState.from_dict(
        {
            "version": 1,
            "metadata": {"name": "walk-cost", "description": "walk-cost"},
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": ids[0],
                    "options": {"path": "/tmp/walk-cost.csv", "blob_ref": "b", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": nodes,
            "edges": [],
            "outputs": [
                {
                    "name": "results",
                    "plugin": "csv",
                    "options": {"path": "outputs/results.csv", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        }
    )


def _constructions(monkeypatch: pytest.MonkeyPatch, length: int, walk: Any) -> int:
    """Plugin constructions ``walk`` makes over an n-node chain, all closed once.

    Counts every instance the tracked manager built (the constant source and
    sink probes included), so callers pin DIFFERENCES between chain lengths,
    where the constant cancels and only the per-node count remains.
    """
    manager = TrackingPluginManager(real_plugin_manager())
    monkeypatch.setattr("elspeth.plugins.infrastructure.manager.get_shared_plugin_manager", lambda: manager)
    walk(_chain_state(length))
    # Sharing must not change probe ownership: every validation-only
    # instance is still closed exactly once, by the cache that built it.
    leaked = [tracked for tracked in manager.instances if tracked.close_count != 1]
    assert not leaked, [(type(t._delegate).__name__, t.close_count) for t in leaked]
    return len(manager.instances)


def _assert_exactly_linear(counts: tuple[int, int, int], *, per_node: int) -> None:
    """``counts`` at n=10/20/40 grow by exactly ``per_node`` per added node."""
    at_10, at_20, at_40 = counts
    assert at_10 >= 10, counts  # the walk ran: at least one probe per node
    # Constant per-node cost => zero second difference. The unmemoized vote
    # measured (10, 20, 40) -> second difference in the hundreds.
    assert (at_40 - at_20) == 2 * (at_20 - at_10), counts
    assert (at_40 - at_10) == 30 * per_node, (counts, (at_40 - at_10) / 30)


def test_schema_contract_walk_constructs_each_transform_once_per_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = tuple(_constructions(monkeypatch, n, CompositionState.validate) for n in (10, 20, 40))

    _assert_exactly_linear(counts, per_node=_CONSTRUCTIONS_PER_NODE_PER_VALIDATE)


def test_review_debt_check_constructs_each_transform_four_times(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route-facing check's whole cost per node, pinned by count: four
    validates, one shared probe instance each. This is the number that
    bounds an import against ``composer_runtime_preflight_timeout_seconds``."""
    counts = tuple(_constructions(monkeypatch, n, unsurfaceable_pending_interpretation_review_sites) for n in (10, 20, 40))

    _assert_exactly_linear(counts, per_node=_CONSTRUCTIONS_PER_NODE_PER_CHECK)
