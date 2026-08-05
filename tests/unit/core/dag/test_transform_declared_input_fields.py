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
    composer-reject, never the reverse — until elspeth-9c5ff8fa7d gates the
    walk on the firewall. When that lands,
    ``test_declared_input_behind_the_firewall_is_accepted`` MUST FLIP to expect
    ``GraphValidationError``: an unchanged green there after a gated walk means
    the gate missed this path. ``test_declared_optional_field_still_arrives``
    is the opposite pin and must stay green through that change — it is the
    shape a careless gate would break. The composer half lives in
    ``tests/unit/web/composer/test_state.py::TestExtrasFirewallDirection``.
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
