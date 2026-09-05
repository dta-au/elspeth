"""The composer node-kind vocabulary has ONE authority and is a checked partition
of the runtime one (elspeth-b3117ec3ac).

``COMPOSER_NODE_TYPES`` is derived from the ``NodeType`` Literal — the type
``NodeSpec.node_type`` carries — and that single constant is an operand of
both node-kind drift guards (yaml_generator's lowering guard and the
capability-skill guidance pin), so deriving it re-points both. The other
operand of each guard stays hand-written: derive both and a guard is
``x != x``. The composer vocabulary is also asserted at import to be the
runtime ``contracts.enums.NodeType`` minus the two kinds the composer authors
as sources/outputs rather than nodes.
"""

from __future__ import annotations

from typing import get_args

import pytest

from elspeth.contracts.enums import NodeType as RuntimeNodeType
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.capability_skill import CAPABILITY_CORE_NODE_GUIDANCE
from elspeth.web.composer.state import (
    COMPOSER_NODE_TYPES,
    NodeType,
    check_composer_vocabulary_partitions_runtime,
)

_RUNTIME_KINDS = frozenset(member.value for member in RuntimeNodeType)


def test_composer_node_types_is_the_literal_not_a_restatement() -> None:
    assert frozenset(get_args(NodeType)) == COMPOSER_NODE_TYPES


def test_composer_vocabulary_is_runtime_minus_source_and_sink() -> None:
    assert _RUNTIME_KINDS - {"source", "sink"} == COMPOSER_NODE_TYPES
    # And the guard that enforces it at import accepts the real pair.
    check_composer_vocabulary_partitions_runtime(COMPOSER_NODE_TYPES, _RUNTIME_KINDS)


@pytest.mark.parametrize(
    ("composer_kinds", "runtime_kinds", "named"),
    (
        # A kind added to the composer Literal alone.
        (COMPOSER_NODE_TYPES | {"kind_eight"}, _RUNTIME_KINDS, "composer-only kinds ['kind_eight']"),
        # A kind added to the runtime enum alone.
        (COMPOSER_NODE_TYPES, _RUNTIME_KINDS | {"kind_eight"}, "does not author ['kind_eight']"),
        # A kind dropped from the composer Literal.
        (COMPOSER_NODE_TYPES - {"collector"}, _RUNTIME_KINDS, "does not author ['collector']"),
    ),
)
def test_partition_guard_refuses_drift_on_either_side(composer_kinds: frozenset[str], runtime_kinds: frozenset[str], named: str) -> None:
    with pytest.raises(RuntimeError, match="vocabulary drift") as exc_info:
        check_composer_vocabulary_partitions_runtime(composer_kinds, runtime_kinds)
    assert named in str(exc_info.value)


def test_kind_eight_in_the_derived_operand_fires_both_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comment 7980's executed table, re-executed: with ONLY the derived
    operand widened (what adding a member to the Literal does), both guards
    fire. Deriving the hand-written operands as well would make both pass —
    that is the tautology this layout exists to prevent."""

    widened = COMPOSER_NODE_TYPES | {"kind_eight"}
    monkeypatch.setattr(yaml_generator, "COMPOSER_NODE_TYPES", widened)
    with pytest.raises(RuntimeError, match=r"missing YAML lowering for \['kind_eight'\]"):
        yaml_generator.generate_pipeline_dict(_linear_state())
    assert set(CAPABILITY_CORE_NODE_GUIDANCE) != widened


def _linear_state():  # type: ignore[no-untyped-def]
    from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec

    return CompositionState(
        source=SourceSpec(
            plugin="csv", on_success="src_out", options={"path": "in.csv", "schema": {"mode": "observed"}}, on_validation_failure="discard"
        ),
        nodes=(
            NodeSpec(
                id="pass",
                node_type="transform",
                plugin="passthrough",
                input="src_out",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(name="out", plugin="csv", options={"path": "out.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"),
        ),
        metadata=PipelineMetadata(name="linear", description=""),
        version=1,
    )
