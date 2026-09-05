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
makes the second difference positive, which is what this pin refuses. The
per-node ceiling bounds the constant so a linear-but-wasteful probe cannot
hide behind exact linearity.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.composer.state import CompositionState
from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager, real_plugin_manager

# Sixteen constructions per node is roughly the count of per-node probe
# sites in _check_schema_contracts (output-schema, collision, required-input,
# emit-profile, the memoized vote) with headroom; the walk measured 12-13 per
# node once the vote was memoized.
_PER_NODE_CONSTRUCTION_CEILING = 16


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


def _transform_constructions_for_one_validate(monkeypatch: pytest.MonkeyPatch, length: int) -> int:
    manager = TrackingPluginManager(real_plugin_manager())
    monkeypatch.setattr("elspeth.plugins.infrastructure.manager.get_shared_plugin_manager", lambda: manager)
    _chain_state(length).validate()
    # The memo must not change probe ownership: every validation-only
    # instance is still closed exactly once by the site that built it.
    leaked = [tracked for tracked in manager.instances if tracked.close_count != 1]
    assert not leaked, [(type(t._delegate).__name__, t.close_count) for t in leaked]
    return len(manager.instances)


def test_schema_contract_walk_constructs_linearly_in_chain_length(monkeypatch: pytest.MonkeyPatch) -> None:
    at_10 = _transform_constructions_for_one_validate(monkeypatch, 10)
    at_20 = _transform_constructions_for_one_validate(monkeypatch, 20)
    at_40 = _transform_constructions_for_one_validate(monkeypatch, 40)

    assert at_10 >= 10, at_10  # the walk ran: at least one probe per node
    # Constant per-node cost => zero second difference. The unmemoized vote
    # measured (10, 20, 40) -> second difference in the hundreds.
    assert (at_40 - at_20) == 2 * (at_20 - at_10), (at_10, at_20, at_40)
    per_node = (at_40 - at_10) / 30
    assert per_node <= _PER_NODE_CONSTRUCTION_CEILING, (at_10, at_20, at_40, per_node)
