"""Composer/engine parity for a coalesce node's effective output guarantees.

The composer's Stage 1 preview and the DAG builder must answer the SAME
question the same way: "what does this coalesce guarantee downstream?" The
engine's authority is ``merge_coalesce_schema``
(``core/dag/coalesce_merge.py``), which dispatches on the merge STRATEGY —
``union`` merges the branch guarantees, ``nested`` keys a flexible schema BY
BRANCH NAME. The composer's propagation vote applied the union arm to every
coalesce regardless of strategy, so a ``merge: nested`` coalesce validated
green at Stage 1 and then died at DAG build with
``EdgeContractError`` — the authoring loop's worst shape, a green preview with
no error to repair against (sibling of elspeth-ae83a6b60c, opposite polarity:
this one over-claims rather than abstains).

Both surfaces are pinned here deliberately. The claim under test is that they
AGREE; pinning only the composer would let a future edit "fix" the composer
back into disagreement with an engine nobody re-read.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.schema import SchemaConfig
from elspeth.core.config import CoalesceSettings, GateSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import get_effective_guaranteed_fields
from elspeth.core.dag.models import EdgeContractError
from elspeth.core.dag.wiring import WiredTransform
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationSummary,
)

_SOURCE_SCHEMA: dict[str, Any] = {"mode": "observed", "guaranteed_fields": ["colour"]}


# --------------------------------------------------------------------------
# Engine surface
# --------------------------------------------------------------------------


class _Source:
    name = "src"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": _SOURCE_SCHEMA}
    _on_validation_failure = "discard"
    on_success = "rows"
    _output_schema_config = None
    observed_value_type: str | None = None


class _Sink:
    name = "out"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


class _ColourConsumer:
    """Stub transform declaring ``required_input_fields: ["colour"]``."""

    input_schema = None
    output_schema = None
    creates_tokens = False
    on_success: str | None = "out"
    on_error: str | None = None
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    preserves_input_values = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.name = "consumer"
        self.config: dict[str, Any] = {
            "schema": {"mode": "observed"},
            "required_input_fields": ["colour"],
        }
        self._output_schema_config = SchemaConfig(mode="observed", fields=None)


def _engine_graph(*, merge: str, with_consumer: bool) -> ExecutionGraph:
    """src(guarantees colour) -> fork[a, b] -> coalesce 'merge' [-> consumer] -> out."""
    transforms: list[WiredTransform] = []
    if with_consumer:
        transforms.append(
            WiredTransform(
                plugin=_ColourConsumer(),  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="consumer",
                    plugin="consumer",
                    input="merge",
                    on_success="out",
                    on_error="discard",
                    options={},
                ),
            )
        )
    return ExecutionGraph.from_plugin_instances(
        sources={"src": _Source()},
        source_settings_map={"src": SourceSettings(plugin="csv", options={"path": "x.csv", "schema": _SOURCE_SCHEMA}, on_success="rows")},
        transforms=transforms,
        sinks={"out": _Sink()},  # type: ignore[dict-item]
        aggregations={},
        gates=[GateSettings(name="fork_gate", input="rows", condition="'all'", routes={"all": "fork"}, fork_to=["a", "b"])],
        coalesce_settings=[
            CoalesceSettings(
                name="merge",
                branches={"a": "a", "b": "b"},
                policy="require_all",
                merge=merge,
                **({} if with_consumer else {"on_success": "out"}),
            )
        ],
    )


def _engine_coalesce_guarantees(merge: str) -> frozenset[str]:
    graph = _engine_graph(merge=merge, with_consumer=False)
    coalesce_ids = [nid for nid in graph._graph.nodes if graph.get_node_info(nid).node_type.value == "coalesce"]
    assert len(coalesce_ids) == 1, coalesce_ids
    return get_effective_guaranteed_fields(graph, coalesce_ids[0])


# --------------------------------------------------------------------------
# Composer surface
# --------------------------------------------------------------------------


def _composer_summary(*, merge: str, required_input_field: str = "colour") -> ValidationSummary:
    """The same pipeline as ``_engine_graph``, authored as composer state."""
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    state = state.with_source(
        SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/data/input.csv", "schema": _SOURCE_SCHEMA},
            on_validation_failure="discard",
        )
    )
    state = state.with_node(
        NodeSpec(
            id="fan_out",
            node_type="gate",
            plugin=None,
            input="rows",
            on_success=None,
            on_error=None,
            options={},
            condition="'all'",
            routes={"all": "fork"},
            fork_to=("a", "b"),
            branches=None,
            policy=None,
            merge=None,
        )
    )
    state = state.with_node(
        NodeSpec(
            id="merge",
            node_type="coalesce",
            plugin=None,
            input="branches",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches={"a": "a", "b": "b"},
            policy="require_all",
            merge=merge,
        )
    )
    state = state.with_node(
        NodeSpec(
            id="consumer",
            node_type="transform",
            plugin="value_transform",
            input="merge",
            on_success="main",
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "operations": [{"target": "_placeholder", "expression": "row['text']"}],
                "required_input_fields": [required_input_field],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    state = state.with_output(
        OutputSpec(
            name="main",
            plugin="csv",
            options={"path": "outputs/main.csv", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )
    )
    return state.validate()


def _contract_violations(summary: ValidationSummary) -> list[str]:
    return [entry.message for entry in summary.errors if entry.error_code == "schema_contract_violation"]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestUnionCoalescePropagatesBranchGuarantees:
    """The briefed defect's control: a union coalesce does NOT erase guarantees.

    Both branches guarantee the same field, so union and intersection give the
    identical answer — a failure here could only mean the branch guarantees
    arrived empty, never that the policy math was wrong.
    """

    def test_engine_union_coalesce_guarantees_the_forked_field(self) -> None:
        assert _engine_coalesce_guarantees("union") == frozenset({"colour"})

    def test_composer_accepts_a_consumer_requiring_the_forked_field(self) -> None:
        summary = _composer_summary(merge="union")
        assert _contract_violations(summary) == []
        assert summary.is_valid, [entry.to_dict() for entry in summary.errors]


class TestNestedCoalesceGuaranteesAreBranchNames:
    """``merge: nested`` keys the merged schema BY BRANCH NAME, not by field.

    Regression: the composer's propagation vote ran the UNION merge on every
    coalesce, so it claimed the branches' inner fields and validated green a
    pipeline the DAG builder rejects at construction.
    """

    def test_engine_nested_coalesce_guarantees_the_branch_names(self) -> None:
        assert _engine_coalesce_guarantees("nested") == frozenset({"a", "b"})

    def test_engine_rejects_a_consumer_requiring_an_inner_field(self) -> None:
        with pytest.raises(EdgeContractError) as excinfo:
            _engine_graph(merge="nested", with_consumer=True)
        assert "colour" in str(excinfo.value)

    def test_composer_agrees_with_the_engine_rejection(self) -> None:
        """The parity claim: Stage 1 must not hand back a green nested merge."""
        summary = _composer_summary(merge="nested")
        violations = _contract_violations(summary)
        assert violations, [entry.to_dict() for entry in summary.errors]
        assert "Missing fields: [colour]" in "\n".join(violations)
        assert not summary.is_valid

    def test_composer_still_accepts_a_consumer_requiring_a_branch_name(self) -> None:
        """The other half of parity: nested guarantees are not merely EMPTY.

        Discriminates the fix from a narrowing that makes every nested coalesce
        abstain-with-empty. The engine BUILDS this pipeline (branch names are
        guaranteed under require_all), so a composer that rejects it would be a
        false reject — strictly worse than the abstention it replaced.
        """
        _engine_graph(merge="nested", with_consumer=False)  # engine builds the shape
        summary = _composer_summary(merge="nested", required_input_field="a")
        assert _contract_violations(summary) == []
        assert summary.is_valid, [entry.to_dict() for entry in summary.errors]

    def test_composer_no_longer_warns_that_the_edge_is_unchecked(self) -> None:
        """A stale "runtime will check this" advisory next to a real error is a
        misleading repair signal — the warning must retire with the abstention."""
        summary = _composer_summary(merge="nested")
        skipped = [w.message for w in summary.warnings if "Contract check skipped" in w.message]
        assert skipped == []


def _nested_coalesce_summary(*, inner_merge: str) -> ValidationSummary:
    """Outer UNION coalesce whose branch `a` is fed by an INNER coalesce.

    The topology is load-bearing and three structural rules constrain it — a
    probe that trips any of them draws unrelated errors and discriminates
    nothing:

    * every coalesce branch connection must be produced by some gate's
      ``fork_to`` (else ``coalesce_branch_alias_unreachable``);
    * both coalesces stay TERMINAL, published under their own id and consumed by
      name, because a coalesce routed into a transform is
      ``coalesce_on_success_must_be_sink``;
    * each fork gets exactly ONE closer — ``g2`` closes at ``c2`` and ``g1``
      closes at ``c1`` — else ``fork_multiple_closers_invalid``.

        src -> g1 forks [a, b]
                 a -> g2 forks [aa, ab]
                        aa -> t_aa (guarantees `extra`) -> aa_done
                        ab -> t_ab (guarantees `extra`) -> ab_done
                      c2 {aa: aa_done, ab: ab_done}        <- inner_merge
                 c1 {a: c2, b: b}                          <- union, require_all
               consumer requires `extra`

    The DISCRIMINATOR: only the inner coalesce's branches guarantee ``extra``;
    the outer coalesce's other branch ``b`` carries just ``colour``. So under the
    outer union, ``extra`` reaching the consumer proves the inner coalesce
    PARTICIPATED, and ``extra`` missing proves it ABSTAINED. Without that
    asymmetry both outcomes look identical and the probe passes either way.
    """

    def _gate(node_id: str, source: str, forks: tuple[str, ...]) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="gate",
            plugin=None,
            input=source,
            on_success=None,
            on_error=None,
            options={},
            condition="'all'",
            routes={"all": "fork"},
            fork_to=forks,
            branches=None,
            policy=None,
            merge=None,
        )

    def _coalesce(node_id: str, branches: dict[str, str], merge: str, options: dict[str, Any]) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="coalesce",
            plugin=None,
            input="branches",
            on_success=None,
            on_error=None,
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=branches,
            policy="require_all",
            merge=merge,
        )

    def _transform(node_id: str, source: str, target: str, options: dict[str, Any]) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="value_transform",
            input=source,
            on_success=target,
            on_error="discard",
            options={"operations": [{"target": "extra", "expression": "'x'"}], **options},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    guarantees_extra: dict[str, Any] = {"schema": {"mode": "flexible", "fields": ["extra: str"], "guaranteed_fields": ["extra"]}}
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    state = state.with_source(
        SourceSpec(
            plugin="csv", on_success="rows", options={"path": "/data/input.csv", "schema": _SOURCE_SCHEMA}, on_validation_failure="discard"
        )
    )
    state = state.with_node(_gate("g1", "rows", ("a", "b")))
    state = state.with_node(_gate("g2", "a", ("aa", "ab")))
    state = state.with_node(_transform("t_aa", "aa", "aa_done", guarantees_extra))
    state = state.with_node(_transform("t_ab", "ab", "ab_done", guarantees_extra))
    state = state.with_node(
        _coalesce(
            "c2",
            {"aa": "aa_done", "ab": "ab_done"},
            inner_merge,
            {"select_branch": "aa"} if inner_merge == "select" else {},
        )
    )
    state = state.with_node(_coalesce("c1", {"a": "c2", "b": "b"}, "union", {}))
    state = state.with_node(_transform("consumer", "c1", "main", {"schema": {"mode": "observed"}, "required_input_fields": ["extra"]}))
    state = state.with_output(
        OutputSpec(
            name="main", plugin="csv", options={"path": "outputs/main.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"
        )
    )
    return state.validate()


class TestUnmirrorableMergeAbstainsOnEveryPath:
    """A merge Composer cannot mirror must abstain even reached as a BRANCH.

    ``coalesce_merge_select_unsupported`` fires per node, so a select coalesce
    never gates an acceptance by itself. But the guarantee walk still REACHES it
    when it feeds another coalesce's branch, and there it used to fall through to
    the union arm and contribute the union of ALL its branches — where the
    runtime's select forwards exactly ONE. That over-claims whenever the branches
    differ: the same polarity as the nested defect, one layer down, and a live
    false accept the day ``select`` is legalised.
    """

    def test_inner_union_coalesce_participates(self) -> None:
        """Control. Without this the abstain assertions prove nothing — a probe
        that abstains everywhere would satisfy them while measuring nothing."""
        summary = _nested_coalesce_summary(inner_merge="union")
        assert _contract_violations(summary) == [], [entry.to_dict() for entry in summary.errors]

    def test_inner_select_coalesce_abstains(self) -> None:
        summary = _nested_coalesce_summary(inner_merge="select")
        violations = _contract_violations(summary)
        assert violations, (
            "the select coalesce contributed a guarantee Composer cannot mirror",
            [entry.to_dict() for entry in summary.errors],
        )
        assert "Missing fields: [extra]" in "\n".join(violations)

    def test_inner_nested_coalesce_contributes_branch_names_not_inner_fields(self) -> None:
        """The third arm, which discriminates abstain-everywhere from correct.

        A nested inner merge PARTICIPATES with its branch NAMES, so the outer
        union reports ``[aa, ab, colour]`` — and ``extra`` is still correctly
        missing because a nested merge buries inner fields under branch keys.
        """
        summary = _nested_coalesce_summary(inner_merge="nested")
        violations = _contract_violations(summary)
        assert violations, [entry.to_dict() for entry in summary.errors]
        assert "guarantees: [aa, ab, colour]" in "\n".join(violations)
