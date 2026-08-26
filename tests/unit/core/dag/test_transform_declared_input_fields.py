"""Build-time enforcement of a transform's declared_input_fields (elspeth-ada5a60249).

Six transform configs compute ``declared_input_fields`` as a property over
their own options (web_scrape's ``url_field``, blob_fetch's ``url_field``,
blob_csv_expand's ``blob_ref_field``, textract's ``key_field``/``bucket_field``/
``version_field``, azure document_intelligence's ``source_field``, rag's
``query_field``). The runtime enforces those per row BEFORE ``process()``, so a
declaration the upstream cannot satisfy fails 100% of rows. Both static
surfaces were blind: Phase 1 reads only the raw ``required_input_fields`` /
``schema.required_fields`` config keys, and Phase 2 reads ``input_schema``,
which is generated from the ``schema:`` block and never folds in the property.

These tests pin the projection (builder → NodeInfo), the enforcement
(``validate_transform_declared_input_fields``), and — most importantly — the
ABSTENTION that keeps the check from rejecting runnable pipelines whose
upstream contract cannot prove what it emits.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError, NodeInfo

# ---------------------------------------------------------------------------
# Hand-built graphs — the validator in isolation
# ---------------------------------------------------------------------------


def _input_graph(
    *,
    source_schema: dict[str, object],
    declared_input_fields: frozenset[str],
    edge_mode: RoutingMode = RoutingMode.MOVE,
) -> ExecutionGraph:
    """source -> transform -> sink, with the transform's input declaration under test."""
    graph = ExecutionGraph()
    graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": source_schema})
    graph.add_node(
        "t1",
        node_type=NodeType.TRANSFORM,
        plugin_name="web_scrape",
        config={"schema": {"mode": "observed"}},
        declared_input_fields=declared_input_fields,
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
    graph.add_edge("src", "t1", label="continue", mode=edge_mode)
    graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestTransformDeclaredInputFields:
    """A transform's declared input fields must be guaranteed by a participating upstream."""

    def test_participating_producer_missing_field_is_rejected(self) -> None:
        """Ticket shape: the declared input column is not among the producer's guarantees."""
        graph = _input_graph(
            source_schema={"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]},
            declared_input_fields=frozenset({"page_url"}),
        )

        with pytest.raises(GraphValidationError, match="page_url") as exc_info:
            schema_validation.validate_transform_declared_input_fields(graph)

        message = str(exc_info.value)
        assert "web_scrape" in message
        assert "src" in message

    def test_guaranteed_field_builds(self) -> None:
        """Negative control: a declaration the producer guarantees must stay buildable."""
        graph = _input_graph(
            source_schema={"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]},
            declared_input_fields=frozenset({"url"}),
        )

        schema_validation.validate_transform_declared_input_fields(graph)

    def test_abstaining_producer_is_not_checked(self) -> None:
        """ABSTENTION: an observed upstream proves nothing, so the row may well carry the field.

        This is the case that separates the projected declaration from the raw
        ``required_input_fields`` surface, which fails closed here.
        """
        graph = _input_graph(
            source_schema={"mode": "observed"},
            declared_input_fields=frozenset({"page_url"}),
        )

        schema_validation.validate_transform_declared_input_fields(graph)

    def test_divert_only_predecessor_is_not_checked(self) -> None:
        """A DIVERT payload is an error envelope, not the producer's declared row."""
        graph = _input_graph(
            source_schema={"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]},
            declared_input_fields=frozenset({"page_url"}),
            edge_mode=RoutingMode.DIVERT,
        )

        schema_validation.validate_transform_declared_input_fields(graph)

    def test_reached_through_validate_edge_compatibility(self) -> None:
        """The check is wired into the surface /validate and `elspeth run` both reach."""
        graph = _input_graph(
            source_schema={"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]},
            declared_input_fields=frozenset({"page_url"}),
        )

        with pytest.raises(GraphValidationError, match="page_url"):
            graph.validate_edge_compatibility()


class TestDeclaredInputFieldsNodeInfoGuard:
    """declared_input_fields is TRANSFORM-only, mirroring declared_output_fields."""

    def test_non_transform_node_is_rejected(self) -> None:
        """Offensive programming: stray input declarations must not sit unread on a sink."""
        with pytest.raises(GraphValidationError, match="only meaningful for TRANSFORM nodes"):
            NodeInfo(
                node_id="n1",
                node_type=NodeType.SINK,
                plugin_name="json",
                declared_input_fields=frozenset({"page_url"}),
            )

    def test_transform_node_is_accepted(self) -> None:
        """Positive control for the guard above."""
        info = NodeInfo(
            node_id="n1",
            node_type=NodeType.TRANSFORM,
            plugin_name="web_scrape",
            declared_input_fields=frozenset({"page_url"}),
        )

        assert info.declared_input_fields == frozenset({"page_url"})


class TestExtrasFirewallDirection:
    """Pins the un-gated union through a locked pass-through, and its safe direction.

    ``walk_effective_guarantee_vote`` unions a predecessor's guarantees through
    any ``passes_through_input`` node without consulting that node's extras
    firewall, so a ``mode: fixed`` transform contributes upstream fields its own
    ``extra='forbid'`` input model would kill on arrival. The validator inherits
    that over-promise and ACCEPTS a declaration nothing downstream of the
    firewall can satisfy.

    THESE TESTS PIN THE KNOWN-AND-DOCUMENTED DIRECTION, NOT DESIRED BEHAVIOUR.
    They exist so the divergence stays one-way — DAG-accept with
    composer-reject, never the reverse. The composer half lives in
    ``tests/unit/web/composer/test_state.py::TestExtrasFirewallDirection``.

    SUPERSEDED INSTRUCTION: this docstring previously told a future fixer that
    ``test_declared_input_behind_the_firewall_is_accepted`` MUST FLIP to expect
    ``GraphValidationError`` once elspeth-9c5ff8fa7d gated the walk, and that
    an unchanged green there meant the gate had missed the path. The completeness
    fix landed WITHOUT gating the field set, so that instruction no longer
    holds and the test correctly stays green. Closedness rides BESIDE
    ``vote.fields`` as ``EffectiveGuaranteeVote.closed`` rather than narrowing
    it, precisely because narrowing would re-tighten the INTERSECTION consumers
    (``validate_transform_output_field_collisions``, the coalesce branch
    builder), which are already sound against a lower bound and were never part
    of the defect. The union pinned below therefore survives, still
    over-promising and still — against set difference — able only to shrink
    ``missing``. ``test_declared_optional_field_still_arrives`` remains the
    opposite pin.
    """

    def _firewall_graph(self, llm_fields: list[str] | None = None) -> ExecutionGraph:
        """source {a,url} -> llm (locked pass-through) -> web_scrape needing 'a'."""
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            config={"schema": {"mode": "fixed", "fields": ["a: str", "url: str"], "guaranteed_fields": ["a", "url"]}},
        )
        graph.add_node(
            "llm",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "fixed", "fields": llm_fields or ["url: str"]}},
            passes_through_input=True,
        )
        graph.add_node(
            "scrape",
            node_type=NodeType.TRANSFORM,
            plugin_name="web_scrape",
            config={"schema": {"mode": "observed"}},
            declared_input_fields=frozenset({"a"}),
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
        graph.add_edge("src", "llm", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("llm", "scrape", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("scrape", "sink", label="out", mode=RoutingMode.MOVE)
        return graph

    def test_walk_carries_the_firewalled_field_through(self) -> None:
        """The mechanism: 'a' survives the vote even though the locked llm rejects it."""
        from elspeth.core.dag.guarantees import walk_effective_guarantee_vote

        vote = walk_effective_guarantee_vote(self._firewall_graph(), "llm", {})

        assert vote.participated is True
        assert "a" in vote.fields

    def test_declared_input_behind_the_firewall_is_accepted(self) -> None:
        """Consequence: the validator under-rejects here. Flip to `raises` when the walk is gated."""
        schema_validation.validate_transform_declared_input_fields(self._firewall_graph())

    def test_declared_optional_field_still_arrives(self) -> None:
        """The boundary a gate must respect: 'a' declared optional is admitted and passes through.

        The union only outruns reality for a field the fixed schema does not
        declare — and such a row dies at the firewall, so nothing reaches the
        consumer. Declare the same field optional and the row flows, making the
        walk's answer correct and this acceptance the right one. A firewall
        gate that rejects here would trade an unreachable false accept for a
        live false reject.
        """
        graph = self._firewall_graph(llm_fields=["url: str", "a: str?"])

        schema_validation.validate_transform_declared_input_fields(graph)


# ---------------------------------------------------------------------------
# Production build path — projection wiring, end to end
# ---------------------------------------------------------------------------


def _build(settings: Any) -> ExecutionGraph:
    """Drive the real instantiate -> build path (build_execution_graph validates internally)."""
    from elspeth.cli_helpers import instantiate_plugins_from_config

    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
    )


def _web_scrape_settings(*, url_field: str, source_mode: str) -> Any:
    """chaosweb's shape: fixed-mode CSV source {id,url,label} -> web_scrape -> json sink."""
    from elspeth.core.config import (
        ElspethSettings,
        SinkSettings,
        SourceSettings,
        TransformSettings,
    )

    source_schema: dict[str, Any] = (
        {"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]} if source_mode == "fixed" else {"mode": "observed"}
    )
    return ElspethSettings(
        sources={
            "primary": SourceSettings(
                plugin="csv",
                on_success="urls",
                options={"path": "input.csv", "on_validation_failure": "discard", "schema": source_schema},
            )
        },
        transforms=[
            TransformSettings(
                name="scraper",
                plugin="web_scrape",
                input="urls",
                on_success="output",
                on_error="discard",
                options={
                    "url_field": url_field,
                    "content_field": "page_content",
                    "fingerprint_field": "page_fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "contract validation test",
                        "allowed_hosts": ["127.0.0.0/8"],
                    },
                    "schema": {"mode": "observed"},
                },
            )
        ],
        sinks={
            "output": SinkSettings(
                plugin="json",
                on_write_failure="discard",
                options={"path": "out.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            )
        },
    )


class TestWebScrapeBuildPath:
    """The misnamed url_field the ticket reports must not build green."""

    def test_misnamed_url_field_against_fixed_source_is_rejected(self, plugin_manager: Any) -> None:
        """At HEAD this built green and then failed every row at runtime."""
        settings = _web_scrape_settings(url_field="page_url", source_mode="fixed")

        with pytest.raises(GraphValidationError, match="page_url"):
            _build(settings)

    def test_correct_url_field_builds(self, plugin_manager: Any) -> None:
        """Baseline: the shipped chaosweb wiring must keep building."""
        settings = _web_scrape_settings(url_field="url", source_mode="fixed")

        _build(settings)

    def test_misnamed_url_field_against_observed_source_abstains(self, plugin_manager: Any) -> None:
        """An observed source cannot prove the column absent — enforcement stays per-row."""
        settings = _web_scrape_settings(url_field="page_url", source_mode="observed")

        _build(settings)

    def test_projection_reaches_node_info(self, plugin_manager: Any) -> None:
        """The builder must carry the plugin's declaration verbatim onto the node."""
        settings = _web_scrape_settings(url_field="url", source_mode="fixed")

        graph = _build(settings)

        transform_nodes = [data["info"] for _nid, data in graph._graph.nodes(data=True) if data["info"].node_type == NodeType.TRANSFORM]
        assert len(transform_nodes) == 1
        assert transform_nodes[0].declared_input_fields == frozenset({"url"})


def _blob_csv_expand_settings(*, source_fields: list[str]) -> Any:
    """blob_csv_expand with blob_ref_field OMITTED — its default names 'blob_ref'."""
    from elspeth.core.config import (
        ElspethSettings,
        SinkSettings,
        SourceSettings,
        TransformSettings,
    )

    return ElspethSettings(
        sources={
            "primary": SourceSettings(
                plugin="csv",
                on_success="manifest",
                options={
                    "path": "manifest.csv",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": source_fields},
                },
            )
        },
        transforms=[
            TransformSettings(
                name="expand",
                plugin="blob_csv_expand",
                input="manifest",
                on_success="output",
                on_error="discard",
                options={"columns": ["id", "text"], "schema": {"mode": "observed"}},
            )
        ],
        sinks={
            "output": SinkSettings(
                plugin="json",
                on_write_failure="discard",
                options={"path": "out.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            )
        },
    )


class TestBlobCSVExpandOmissionShape:
    """blob_ref_field DEFAULTS to 'blob_ref', so the trigger is option OMISSION."""

    def test_default_blob_ref_field_absent_upstream_is_rejected(self, plugin_manager: Any) -> None:
        """The author never wrote the option; the default names a column nobody produces."""
        settings = _blob_csv_expand_settings(source_fields=["manifest_index: int", "source_name: str"])

        with pytest.raises(GraphValidationError, match="blob_ref"):
            _build(settings)

    def test_default_blob_ref_field_present_upstream_builds(self, plugin_manager: Any) -> None:
        """The canonical manifest shape guarantees blob_ref and must keep building."""
        settings = _blob_csv_expand_settings(source_fields=["manifest_index: int", "blob_ref: str"])

        _build(settings)


class TestTextractConfigShapeDependentProjection:
    """textract's declared SET depends on config shape, not just field renaming."""

    def _config(self, **overrides: Any) -> Any:
        from elspeth.plugins.transforms.aws.textract_document_analysis import (
            AWSTextractDocumentAnalysisConfig,
        )

        options: dict[str, Any] = {
            "region": "ap-southeast-2",
            "feature_types": ["TABLES"],
            "key_field": "s3_key",
            "text_field": "document_text",
            "schema": {"mode": "observed"},
        }
        options.update(overrides)
        # bucket and bucket_field are mutually exclusive document-location modes,
        # so the static-bucket default only applies when the row-field mode is off.
        if "bucket_field" not in options:
            options["bucket"] = "example-bucket"
        return AWSTextractDocumentAnalysisConfig.model_validate(options)

    def test_optional_fields_unset_declares_key_only(self) -> None:
        """bucket_field/version_field are None by default and contribute nothing."""
        config = self._config()

        assert config.declared_input_fields == frozenset({"s3_key"})

    def test_bucket_field_set_widens_the_declared_set(self) -> None:
        """Setting the option adds a second required input column."""
        config = self._config(bucket_field="s3_bucket")

        assert config.declared_input_fields == frozenset({"s3_key", "s3_bucket"})

    def test_version_field_set_widens_the_declared_set(self) -> None:
        """Same polarity for the third optional input column."""
        config = self._config(bucket_field="s3_bucket", version_field="s3_version")

        assert config.declared_input_fields == frozenset({"s3_key", "s3_bucket", "s3_version"})


# ---------------------------------------------------------------------------
# Completeness (elspeth-9c5ff8fa7d family) — an OPEN producer proves no absence
# ---------------------------------------------------------------------------


class TestOpenProducerProvesNoAbsence:
    """Set DIFFERENCE needs a COMPLETE field set, which only a CLOSED schema supplies.

    ``participates_in_propagation`` answers "does this schema have guarantees to
    contribute to an intersection" — a LOWER bound. This validator subtracts
    ``vote.fields`` from the declaration and rejects on any remainder, which is
    sound only against an UPPER bound: a set that enumerates every field a row
    leaving the producer can carry. A producer whose schema ``allows_extra_fields``
    (``observed`` or ``flexible``) supplies the first and never the second, so
    reading participation as completeness rejected pipelines that run.

    The reproduction that pins the polarity: an ``observed`` CSV source feeding
    ``field_mapper`` runs 2/2 rows green, and adding the strictly ADDITIVE
    ``guaranteed_fields: [id]`` to that same source turned it into a build-time
    rejection of the pass-through column ``colour``. Declaring MORE must never
    reject more.

    ``EffectiveGuaranteeVote.closed`` carries the second predicate, derived from
    ``SchemaConfig.allows_extra_fields`` — the extras-firewall authority the
    contract layer already owns — rather than restated here.
    """

    def _graph(
        self,
        *,
        producer_schema: dict[str, object],
        mid_schema: dict[str, object] | None = None,
        declared_input_fields: frozenset[str] = frozenset({"colour"}),
    ) -> ExecutionGraph:
        """src -> [optional pass-through] -> web_scrape declaring an input column."""
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": producer_schema})
        producer = "src"
        if mid_schema is not None:
            graph.add_node(
                "mid",
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
                config={"schema": mid_schema},
                passes_through_input=True,
            )
            graph.add_edge("src", "mid", label="continue", mode=RoutingMode.MOVE)
            producer = "mid"
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="web_scrape",
            config={"schema": {"mode": "observed"}},
            declared_input_fields=declared_input_fields,
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
        graph.add_edge(producer, "t1", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)
        return graph

    # -- GATE 1: a genuine pass-through column must not be rejected ---------

    def test_observed_producer_with_partial_guarantees_is_not_checked(self) -> None:
        """No pass-through anywhere: observed mode admits columns the guarantee omits."""
        graph = self._graph(producer_schema={"mode": "observed", "guaranteed_fields": ["id"]})

        schema_validation.validate_transform_declared_input_fields(graph)

    def test_flexible_producer_is_not_checked(self) -> None:
        """``flexible`` allows extras exactly as ``observed`` does — same verdict."""
        graph = self._graph(producer_schema={"mode": "flexible", "fields": ["id: int"]})

        schema_validation.validate_transform_declared_input_fields(graph)

    def test_pass_through_downstream_of_abstainer_is_not_checked(self) -> None:
        """The ticket shape: a pass-through's OWN additions are not the whole row.

        ``compose_propagation`` returns a bare ``frozenset``, so a pass-through
        whose predecessors all abstain reports its own added fields as though
        they were the complete set. The composed set is open for the same
        reason its predecessor was.
        """
        graph = self._graph(
            producer_schema={"mode": "observed"},
            mid_schema={"mode": "observed", "guaranteed_fields": ["note"]},
        )

        schema_validation.validate_transform_declared_input_fields(graph)

    # -- GATE 2: a field NOBODY provides must still be rejected -------------

    def test_closed_producer_missing_field_is_still_rejected(self) -> None:
        """A fixed schema is a firewall: the column is provably absent, so reject."""
        graph = self._graph(producer_schema={"mode": "fixed", "fields": ["id: int"]})

        with pytest.raises(GraphValidationError, match="colour"):
            schema_validation.validate_transform_declared_input_fields(graph)

    def test_closed_pass_through_firewall_is_still_rejected(self) -> None:
        """The firewall truncates an open upstream, so the composed set closes.

        Rows carrying ``colour`` die at the ``mode: fixed`` pass-through's
        ``extra='forbid'`` input model, so nothing downstream of it can see the
        column however dynamic the source was.
        """
        graph = self._graph(
            producer_schema={"mode": "observed"},
            mid_schema={"mode": "fixed", "fields": ["id: int", "note: str"]},
        )

        with pytest.raises(GraphValidationError, match="colour"):
            schema_validation.validate_transform_declared_input_fields(graph)

    def test_closed_producer_guaranteeing_the_field_still_builds(self) -> None:
        """Positive control for the two rejections above."""
        graph = self._graph(producer_schema={"mode": "fixed", "fields": ["id: int", "colour: str"]})

        schema_validation.validate_transform_declared_input_fields(graph)


class TestVoteCarriesClosedness:
    """``EffectiveGuaranteeVote.closed`` is the mechanism, pinned directly.

    Testing the verdict alone would leave a validator that skipped every
    predecessor indistinguishable from one that read completeness correctly.
    """

    def _producer_vote(self, schema: dict[str, object]) -> Any:
        from elspeth.core.dag.guarantees import walk_effective_guarantee_vote

        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": schema})
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
        graph.add_edge("src", "sink", label="out", mode=RoutingMode.MOVE)
        return walk_effective_guarantee_vote(graph, "src", {})

    def test_fixed_schema_is_closed(self) -> None:
        vote = self._producer_vote({"mode": "fixed", "fields": ["id: int"]})

        assert vote.participated is True
        assert vote.closed is True

    def test_observed_schema_with_guarantees_participates_but_is_open(self) -> None:
        """The exact combination that produced the false red: participating AND open."""
        vote = self._producer_vote({"mode": "observed", "guaranteed_fields": ["id"]})

        assert vote.participated is True
        assert vote.closed is False

    def test_flexible_schema_is_open(self) -> None:
        vote = self._producer_vote({"mode": "flexible", "fields": ["id: int"]})

        assert vote.closed is False

    def test_participation_and_closedness_are_independent_axes(self) -> None:
        """A bare observed schema abstains AND is open — neither implies the other."""
        vote = self._producer_vote({"mode": "observed"})

        assert vote.participated is False
        assert vote.closed is False
