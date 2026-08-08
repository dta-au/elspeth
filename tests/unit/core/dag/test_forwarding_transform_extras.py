"""Build-time extras firewall across FIELD-FORWARDING transforms (elspeth-15c72686f2).

``passes_through_input`` is all-or-nothing: it demands that every input field
survive. A transform that forwards the whole row except one column it consumed
cannot declare it, so the guarantee walk used to STOP at those nodes and an
upstream producer's extras became invisible to
``_validate_locked_consumer_guaranteed_extras``. The reported graph —
llm source -> line_explode -> locked text sink — built green on both gates and
then killed every row at the sink's per-row input preflight on
``<response_field>_usage`` / ``_model``.

``forwards_input_fields`` / ``removed_input_fields`` is the weaker declaration
those plugins CAN make, and ``walk_definite_emitted_fields`` is the
extras-direction walk that consumes it. Deliberately built from REAL plugin
instances rather than hand-stamped ``NodeInfo``: the defect lived in the gap
between what a plugin does at runtime and what its declarations said, so a
hand-built graph could assert the walk while leaving that gap open.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.core.config import SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import get_definite_emitted_fields, get_effective_guaranteed_fields
from elspeth.core.dag.models import EdgeContractError
from elspeth.core.dag.wiring import WiredTransform
from elspeth.plugins.sinks.text_sink import TextSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.plugins.transforms.field_mapper import FieldMapper
from elspeth.plugins.transforms.line_explode import LineExplode
from elspeth.plugins.transforms.llm.transform import LLMTransform

_RESPONSE_FIELD = "announcement"
_METADATA_EXTRAS = frozenset({f"{_RESPONSE_FIELD}_usage", f"{_RESPONSE_FIELD}_model"})


def _llm_source_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Write an announcement.",
        "response_field": _RESPONSE_FIELD,
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    options.update(overrides)
    return options


def _llm_transform_options() -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Write an announcement about {{ row.topic }}.",
        "response_field": _RESPONSE_FIELD,
        "required_input_fields": ["topic"],
        "schema": {"mode": "observed"},
    }


def _line_explode_options() -> dict[str, Any]:
    return {
        "source_field": _RESPONSE_FIELD,
        "output_field": "sentence",
        "include_index": False,
        "schema": {"mode": "observed"},
    }


def _locked_text_sink_options(field: str = "sentence") -> dict[str, Any]:
    return {
        "path": "outputs/announcement_sentences.txt",
        "field": field,
        "schema": {"mode": "fixed", "fields": [f"{field}: str"]},
        "mode": "write",
        "collision_policy": "auto_increment",
    }


def _wired(plugin: Any, *, name: str, plugin_name: str, input_conn: str, on_success: str, options: dict[str, Any]) -> WiredTransform:
    return WiredTransform(
        plugin=plugin,
        settings=TransformSettings(
            name=name,
            plugin=plugin_name,
            input=input_conn,
            on_success=on_success,
            on_error="discard",
            options=dict(options),
        ),
    )


def _expect_rejected(**kwargs: Any) -> EdgeContractError:
    """Build the graph and return the extras rejection it must raise.

    ``from_plugin_instances`` runs ``validate_edge_compatibility`` as part of
    construction, so the rejection surfaces at build rather than at a separate
    validate call — which is the point: an authored graph never reaches a run.
    The assertion is pinned to the extras arm specifically (``extra_fields``
    populated) so a graph rejected for some unrelated reason cannot pass as a
    fix for this defect.
    """
    with pytest.raises(EdgeContractError) as excinfo:
        _build_graph(**kwargs)
    error = excinfo.value
    assert error.compatibility_result is not None, f"not an extras rejection: {error}"
    assert error.compatibility_result.extra_fields, f"not an extras rejection: {error}"
    return error


def _build_graph(
    *,
    source_plugin: Any,
    source_plugin_name: str,
    source_options: dict[str, Any],
    source_connection: str,
    transforms: list[WiredTransform],
    sink: Any,
    sink_name: str,
) -> ExecutionGraph:
    return ExecutionGraph.from_plugin_instances(
        sources={"in": source_plugin},
        source_settings_map={
            "in": SourceSettings(plugin=source_plugin_name, on_success=source_connection, options=dict(source_options)),
        },
        transforms=transforms,
        sinks={sink_name: sink},
        aggregations={},
        gates=[],
        coalesce_settings=[],
    )


class TestLLMMetadataExtrasSurviveAForwardingTransform:
    """The reported graph and its transform-fed twin."""

    def test_llm_source_through_line_explode_into_locked_sink_is_rejected(self) -> None:
        """Decisive repro of the battery round-7 g11-s2 failure.

        Before the fix this built green and every row died at the sink's
        ``model_validate`` on ``announcement_usage`` / ``announcement_model``.
        """
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS

    def test_llm_transform_through_line_explode_into_locked_sink_is_rejected(self) -> None:
        """The same defect one hop earlier — and the far commoner authored shape.

        The reported ticket blamed the llm SOURCE and argued no earlier battery
        round could have found this because ``source:llm`` was unauthorable.
        That narrative is wrong: an llm TRANSFORM feeding the same exploder
        reaches the identical dead end, so the defect was always reachable.
        """
        error = _expect_rejected(
            source_plugin=CSVSource(
                {
                    "path": "data/in.csv",
                    "schema": {"mode": "fixed", "fields": ["topic: str"]},
                    "on_validation_failure": "discard",
                }
            ),
            source_plugin_name="csv",
            source_options={
                "path": "data/in.csv",
                "schema": {"mode": "fixed", "fields": ["topic: str"]},
                "on_validation_failure": "discard",
            },
            source_connection="rows",
            transforms=[
                _wired(
                    LLMTransform(_llm_transform_options()),
                    name="writer",
                    plugin_name="llm",
                    input_conn="rows",
                    on_success="written",
                    options=_llm_transform_options(),
                ),
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="written",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                ),
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        extras = frozenset(error.compatibility_result.extra_fields)
        # The csv column rides through the llm transform AND the exploder, so it
        # is a definite extra too — the walk is not special-cased to llm metadata.
        assert extras == _METADATA_EXTRAS | {"topic"}

    def test_consumed_source_field_is_not_reported_as_an_extra(self) -> None:
        """The removal set is load-bearing, not decoration.

        ``line_explode`` consumes ``announcement``; a walk that only unioned
        upstream emits would name it here and propose dropping a field the
        transform already removed — a false repair for an LLM authoring loop.
        """
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        assert _RESPONSE_FIELD not in error.compatibility_result.extra_fields

    def test_sink_admitting_the_metadata_extras_still_builds(self) -> None:
        """The two green g11 samples: a consumer whose schema tolerates the trio.

        Pins that the fix rejects on ARRIVAL, not on the presence of an llm
        upstream — otherwise it would break every working llm pipeline. The
        sink stays LOCKED (``mode: fixed`` still means ``extra='forbid'``) and
        declares the metadata OPTIONAL, which is the boundary the extras check
        documents: admitted = every declared model field, required or optional.
        Declaring them required instead would trade this defect for the
        opposite one — a sink demanding fields ``line_explode`` never
        guarantees.
        """
        sink_options = {
            "path": "outputs/announcement_sentences.txt",
            "field": "sentence",
            "schema": {
                "mode": "fixed",
                "fields": ["sentence: str", f"{_RESPONSE_FIELD}_usage: any?", f"{_RESPONSE_FIELD}_model: str?"],
            },
            "mode": "write",
            "collision_policy": "auto_increment",
        }
        _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(sink_options),
            sink_name="sentence_rows",
        )


class TestDefiniteEmitsIsSeparateFromTheGuaranteeWalk:
    """The two walks answer different questions and must keep different answers."""

    def _explode_graph(self) -> ExecutionGraph:
        return _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(
                {
                    "path": "outputs/o.txt",
                    "field": "sentence",
                    "schema": {"mode": "flexible", "fields": ["sentence: str"]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                }
            ),
            sink_name="sentence_rows",
        )

    def _explode_node(self, graph: ExecutionGraph) -> str:
        return next(node_id for node_id in graph._graph.nodes if graph.get_node_info(node_id).plugin_name == "line_explode")

    def test_presence_walk_is_unchanged_at_a_forwarding_node(self) -> None:
        """The permissive direction must NOT widen.

        ``get_effective_guaranteed_fields`` feeds sink required-field
        clearance, ``check_compatibility``'s missing-arm forgiveness, and the
        forgiven-field type walk — all of which read a wider set as PERMISSION.
        Widening it to close this gap would have loosened three gates to fix one.
        """
        graph = self._explode_graph()
        explode_node = self._explode_node(graph)

        assert get_effective_guaranteed_fields(graph, explode_node) == frozenset({"sentence"})

    def test_extras_walk_sees_the_forwarded_metadata(self) -> None:
        graph = self._explode_graph()
        explode_node = self._explode_node(graph)

        assert get_definite_emitted_fields(graph, explode_node) == frozenset({"sentence"}) | _METADATA_EXTRAS

    def test_extras_walk_is_additive_over_the_presence_walk(self) -> None:
        """Additivity is what makes the fix incapable of un-rejecting a graph."""
        graph = self._explode_graph()

        for node_id in graph._graph.nodes:
            assert get_definite_emitted_fields(graph, node_id) >= get_effective_guaranteed_fields(graph, node_id)


class TestForwardingParityAcrossTheDeclaringClass:
    """line_explode is one of four; the walk must not be exploder-specific."""

    def test_field_mapper_without_select_only_forwards_upstream_extras(self) -> None:
        """``select_only: false`` deep-copies the whole row, so extras ride through.

        This is also the shape the extras error RECOMMENDS as a repair
        ("insert a field_mapper with select_only: true"), which makes getting
        the non-select_only case right load-bearing for the advice.
        """
        mapper_options = {
            "mapping": {_RESPONSE_FIELD: "body"},
            "select_only": False,
            "schema": {"mode": "observed"},
        }
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    FieldMapper(mapper_options),
                    name="mapped",
                    plugin_name="field_mapper",
                    input_conn="brief",
                    on_success="body_rows",
                    options=mapper_options,
                )
            ],
            sink=TextSink(_locked_text_sink_options(field="body")),
            sink_name="body_rows",
        )

        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS

    def test_field_mapper_with_select_only_still_drops_the_extras(self) -> None:
        """The documented repair must actually repair.

        ``select_only: true`` emits only the mapping targets, so it declares no
        forwarding at all and the graph builds — if this regressed, every extras
        error in the tree would be proposing a fix that does not work.
        """
        mapper_options = {
            "mapping": {_RESPONSE_FIELD: "body"},
            "select_only": True,
            "schema": {"mode": "observed"},
        }
        _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    FieldMapper(mapper_options),
                    name="mapped",
                    plugin_name="field_mapper",
                    input_conn="brief",
                    on_success="body_rows",
                    options=mapper_options,
                )
            ],
            sink=TextSink(_locked_text_sink_options(field="body")),
            sink_name="body_rows",
        )


class TestForwardingDeclarationsMatchPluginBehaviour:
    """Unit-level pins on the four declarations the walk trusts."""

    def test_line_explode_declares_its_consumed_source_field(self) -> None:
        transform = LineExplode(_line_explode_options())

        assert transform.forwards_input_fields is True
        assert transform.removed_input_fields == frozenset({_RESPONSE_FIELD})

    def test_field_mapper_abstains_on_an_unresolved_original_header(self) -> None:
        """An original header removes a name only ``resolve_name`` knows at runtime.

        Declaring forwarding with an unnameable removal would predict a field
        that never arrives — a FALSE build-time rejection of a working rename
        pipeline, the one failure direction over-declaration cannot produce.
        """
        transform = FieldMapper(
            {
                "mapping": {"First Name": "given_name"},
                "select_only": False,
                "schema": {"mode": "observed"},
            }
        )

        assert transform.forwards_input_fields is False

    def test_field_mapper_dotted_source_removes_nothing(self) -> None:
        """A dotted source is a nested READ; its root field survives in the output."""
        transform = FieldMapper(
            {
                "mapping": {"meta.author": "author"},
                "select_only": False,
                "schema": {"mode": "observed"},
            }
        )

        assert transform.forwards_input_fields is True
        assert transform.removed_input_fields == frozenset()
