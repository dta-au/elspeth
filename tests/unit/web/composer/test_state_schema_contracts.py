"""Composer schema-contract validation and advisory parity."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from elspeth.core.config import CoalesceSettings
from elspeth.plugins.infrastructure.templates import TemplateError
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    EdgeType,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.composer.yaml_generator import generate_yaml
from tests.unit.web.composer._probe_lifecycle_helpers import DelegatingPluginManagerDouble


class TestSchemaContractValidation:
    """Tests for schema contract validation (pass 9) in CompositionState.validate()."""

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _make_source(
        self,
        on_success: str = "t1",
        plugin: str = "csv",
        options: dict[str, Any] | None = None,
        on_validation_failure: str = "discard",
    ) -> SourceSpec:
        opts = dict(options or {})
        if plugin == "csv":
            opts = {"path": "/data/input.csv", **opts}
        elif plugin == "text":
            opts = {"path": "/data/input.txt", "column": "text", **opts}
        return SourceSpec(
            plugin=plugin,
            on_success=on_success,
            options=opts,
            on_validation_failure=on_validation_failure,
        )

    def _make_transform(
        self,
        id: str,
        input: str,
        on_success: str,
        plugin: str = "value_transform",
        options: dict[str, Any] | None = None,
        on_error: str = "discard",
    ) -> NodeSpec:
        opts = dict(options or {})
        if plugin == "value_transform":
            opts = {
                "schema": {"mode": "observed"},
                "operations": [{"target": "_placeholder", "expression": "row['text']"}],
                **opts,
            }
        return NodeSpec(
            id=id,
            node_type="transform",
            plugin=plugin,
            input=input,
            on_success=on_success,
            on_error=on_error,
            options=opts,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_gate(
        self,
        id: str,
        input: str,
        routes: dict[str, str],
        condition: str = "True",
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="gate",
            plugin=None,
            input=input,
            on_success=None,
            on_error=None,
            options={},
            condition=condition,
            routes=routes,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _make_coalesce(
        self,
        id: str,
        input: str,
        on_success: str | None,
        branches: tuple[str, ...] | None = None,
        merge: str = "nested",
        options: dict[str, Any] | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=id,
            node_type="coalesce",
            plugin=None,
            input=input,
            on_success=on_success,
            on_error=None,
            options=options or {},
            condition=None,
            routes=None,
            fork_to=None,
            branches=branches if branches is not None else (input,),
            policy="require_all",
            merge=merge,
        )

    def _make_output(self, name: str = "main") -> OutputSpec:
        return OutputSpec(
            name=name,
            plugin="csv",
            options={"path": f"outputs/{name}.csv", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _make_typed_edge_state(self, producer_type: str, consumer_type: str) -> CompositionState:
        """Build csv(fixed age:<producer_type>) -> value_transform(fixed age:<consumer_type>) -> sink.

        The two calls differ ONLY in the declared field type, so a test that
        pins the mismatch against its own type-agreeing control cannot pass by
        accident on an unrelated error (elspeth-f2eb8fef9f). See the clean-probe
        rule in the module docstring of the agreement suite.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1_in",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": [f"age: {producer_type}"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1_in",
                "main",
                plugin="value_transform",
                options={
                    "schema": {"mode": "fixed", "fields": [f"age: {consumer_type}"]},
                    "operations": [{"field": "age", "operation": "upper"}],
                },
            )
        )
        return state.with_output(self._make_output("main"))

    def test_edge_field_type_mismatch_is_rejected(self) -> None:
        """A plain producer/consumer field-TYPE conflict must not validate green.

        elspeth-f2eb8fef9f. Stage 1's edge-contract accounting compares field
        NAMES; the runtime compares TYPES via
        ``core/dag/schema_validation.py::validate_single_edge`` ->
        ``contracts/data.py::check_compatibility``. Before this fix a plain
        two-node pipeline whose producer declared ``age: int`` and whose
        consumer declared ``age: str`` returned ``is_valid=True`` with ZERO
        errors — byte-identical to the type-agreeing control below — while the
        DAG build raised ``EdgeContractError`` "Type mismatches: age (expected
        str, got int)". No coalesce, no row_union, no special topology.
        """
        result = self._make_typed_edge_state("int", "str").validate()

        assert not result.is_valid
        assert any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_edge_field_type_check_does_not_false_red_on_nullable(self) -> None:
        """A nullable-but-required producer field must not be read as a type conflict.

        Regression pin. The first implementation reconstructed both sides as
        PluginSchema models via ``build_coalesce_schema`` and called
        ``check_compatibility``. That factory widens a field to ``X | None``
        when ``fd.nullable or not fd.required`` — correct for coalesce output,
        where a branch can lose a ``last_wins`` collision — but the factory that
        builds ordinary source/transform schemas
        (``plugins/infrastructure/schema_factory.py::_get_python_type``) widens
        ONLY on ``not required`` and never reads ``nullable``. So this pipeline,
        whose real schemas are both plain ``int``, was REJECTED as ``int |
        None`` vs ``int``.

        A false red is worse than the gap it closed: it misdirects the LLM
        authoring loop toward a defect that does not exist, and the runtime
        would have accepted the pipeline.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1_in",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [{"name": "age", "field_type": "int", "required": True, "nullable": True}],
                    }
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1_in",
                "main",
                plugin="value_transform",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [{"name": "age", "field_type": "int", "required": True, "nullable": False}],
                    },
                    "operations": [{"field": "age", "operation": "upper"}],
                },
            )
        )
        result = state.with_output(self._make_output("main")).validate()

        assert not any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_edge_field_type_agreement_is_accepted(self) -> None:
        """Positive control for :meth:`test_edge_field_type_mismatch_is_rejected`.

        Identical topology and identical field NAME; only the declared type
        agrees. This is what makes the mismatch test falsifiable — without it a
        blanket rejection would pass the test above.
        """
        result = self._make_typed_edge_state("int", "int").validate()

        assert result.is_valid, [(e.error_code, e.message) for e in result.errors]
        assert not any(error.error_code == "edge_field_type_incompatible" for error in result.errors)

    def _make_typed_sink_edge_state(self, producer_type: str, sink_type: str) -> CompositionState:
        """Build csv(fixed value:<producer_type>) -> sink(fixed value:<sink_type>) with NO node between.

        The sink-consumer twin of :meth:`_make_typed_edge_state`. The two calls
        differ ONLY in the sink's declared field type, so the mismatch test is
        pinned against its own type-agreeing control (clean-probe rule).
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": [f"value: {producer_type}"]}},
            )
        )
        return state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "fixed", "fields": [f"value: {sink_type}"]}},
                on_write_failure="discard",
            )
        )

    def test_sink_edge_field_type_mismatch_is_rejected(self) -> None:
        """A typed source feeding a SINK that declares a conflicting field type must not validate green.

        elspeth-2ed41f0a4a (census 2026-08-17). The node-consumer mirror
        (elspeth-f2eb8fef9f) left the sink consumer uncovered: ``csv(value: str)
        -> sink(value: int)`` returned ``is_valid=True`` byte-identical to the
        type-agreeing control while the runtime edge check raised
        ``GraphValidationError`` "incompatible ... value" — a two-component
        pipeline with a typo-level mistake, no special topology.
        """
        result = self._make_typed_sink_edge_state("str", "int").validate()

        assert not result.is_valid
        assert any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]
        [entry] = [error for error in result.errors if error.error_code == "edge_field_type_incompatible"]
        assert entry.component == "output:main"

    def test_sink_edge_field_type_agreement_is_accepted(self) -> None:
        """Positive control for :meth:`test_sink_edge_field_type_mismatch_is_rejected`."""
        result = self._make_typed_sink_edge_state("str", "str").validate()

        assert result.is_valid, [(e.error_code, e.message) for e in result.errors]

    def test_sink_edge_field_type_check_abstains_on_dynamic_producer(self) -> None:
        """A transform-fed sink is a DYNAMIC producer edge: the runtime skips it, so Stage 1 must too.

        Runtime Phase-2 type validation fires only when the effective producer
        schema is a typed model (``graph.py`` observed/dynamic bypass); a
        transform producer resolves to a dynamic schema. Rejecting here would be
        a false red the runtime does not share.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1_in",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": ["value: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1_in",
                "main",
                plugin="value_transform",
                options={"schema": {"mode": "observed"}},
            )
        )
        result = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "fixed", "fields": ["value: int"]}},
                on_write_failure="discard",
            )
        ).validate()

        assert not any(error.error_code == "edge_field_type_incompatible" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def _make_string_scan_state(self, upstream_type: str) -> CompositionState:
        """Build csv(fixed value:<upstream_type>) -> keyword_filter(fields=[value]) -> sink.

        The two calls differ ONLY in the source's declared type for the scanned
        field, so the mismatch test is pinned against its own control.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="kf_in",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": [f"value: {upstream_type}"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "kf",
                "kf_in",
                "main",
                plugin="keyword_filter",
                options={"schema": {"mode": "observed"}, "fields": ["value"], "blocked_patterns": ["forbidden"]},
            )
        )
        return state.with_output(self._make_output("main"))

    def test_string_scan_field_typed_non_string_upstream_is_rejected(self) -> None:
        """A transform scanning a field its typed producer declares non-string must not validate green.

        elspeth-2ed41f0a4a (census 2026-08-17). ``keyword_filter`` declares its
        ``fields`` as ``declared_string_input_fields``; the runtime's
        ``validate_transform_string_typed_input_fields`` rejects a live
        predecessor that provably types one of them non-string, because the
        transform fails closed on the first non-string value and every row from
        that producer would be quarantined. Stage 1 had no counterpart: this
        state validated green and died at the DAG build.
        """
        result = self._make_string_scan_state("int").validate()

        assert not result.is_valid
        [entry] = [e for e in result.errors if e.error_code == "transform_string_input_field_type_incompatible"]
        assert entry.component == "node:kf"
        assert "value" in entry.message and "int" in entry.message

    def test_string_scan_field_str_upstream_is_accepted(self) -> None:
        """Positive control: the same scan over a ``str``-typed upstream field validates green."""
        result = self._make_string_scan_state("str").validate()

        assert not any(e.error_code == "transform_string_input_field_type_incompatible" for e in result.errors), result.errors
        assert result.is_valid, result.errors

    def _make_unreferenced_source_schema_state(self, source_fields: list[str]) -> CompositionState:
        """Build csv(fixed, <source_fields>) -> sink, where the SINK DECLARES NO SCHEMA.

        The absent sink schema is the whole point: Stage 1's
        ``contract_config_invalid`` parse was incidental to contract checking,
        so with nothing to compare against, the source's block was never parsed
        (elspeth-33738eedb6). ``_make_output`` always declares one, so this
        state builds its own sink.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": source_fields}},
            )
        )
        return state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv"},
                on_write_failure="discard",
            )
        )

    def test_malformed_source_schema_is_rejected_without_a_consumer_schema(self) -> None:
        """A malformed field spec must be rejected even when nothing consumes it.

        elspeth-33738eedb6. ``age_no_colon_no_type`` is not a valid field spec.
        Before this fix it validated GREEN whenever no downstream consumer
        declared a schema — because the parse fired only when something forced
        the schema to be resolved for a comparison — and died later at plugin
        construction with ``PluginConfigError``. ``source -> sink`` with a
        plain unschema'd sink is among the most common pipeline shapes.
        """
        result = self._make_unreferenced_source_schema_state(["age_no_colon_no_type"]).validate()

        assert not result.is_valid
        assert any(error.error_code == "contract_config_invalid" for error in result.errors), [
            (e.error_code, e.message) for e in result.errors
        ]

    def test_wellformed_source_schema_is_accepted_without_a_consumer_schema(self) -> None:
        """Positive control for the test above — identical but for the field spec."""
        result = self._make_unreferenced_source_schema_state(["age: int"]).validate()

        assert result.is_valid, [(e.error_code, e.message) for e in result.errors]

    def test_malformed_source_schema_is_reported_once(self) -> None:
        """The eager parse must not double-report against the lazy contract parsers.

        With a consumer schema present, both the eager sweep and the contract
        loop's own ``_parse_*`` helpers can observe the same broken spec. One
        authoring defect must yield one error.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": ["age_no_colon_no_type"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/main.csv", "schema": {"mode": "fixed", "fields": ["age: int"]}},
                on_write_failure="discard",
            )
        )

        result = state.validate()

        source_config_errors = [
            error for error in result.errors if error.error_code == "contract_config_invalid" and "age_no_colon_no_type" in error.message
        ]
        assert len(source_config_errors) == 1, [(e.component, e.message) for e in result.errors]

    def test_schema_validation_closes_every_constructed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every schema-inspection instance is owned and closed exactly once."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "mapper",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "fixed",
                        # 'text' is DECLARED because it is a mapping SOURCE and
                        # therefore a configured READ (elspeth-d4ae04b374); a
                        # fixed schema omitting it is rejected at construction
                        # (elspeth-d3958d90f5), which would build no probe at
                        # all and make this lifecycle assertion vacuous.
                        "fields": ["body: str", "text: str"],
                        "required_fields": ["body"],
                    },
                    "mapping": {"text": "body"},
                    "select_only": True,
                },
            )
        )
        state = state.with_output(self._make_output())

        state.validate()

        assert len(tracking.instances) >= 2, "fixture did not exercise the schema probe sites"
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)

    def test_declared_field_type_probe_closes_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The standalone producer-field probe owns its constructed transform."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.composer.state import _producer_declared_field_type
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )
        producer = self._make_transform(
            "mapper",
            "mapper_in",
            "main",
            plugin="field_mapper",
            options={
                # 'text' declared for the same reason as the sibling probe
                # test above: a mapping source is a configured read.
                "schema": {"mode": "fixed", "fields": ["body: str", "text: str"]},
                "mapping": {"text": "body"},
                "select_only": True,
            },
        )

        field_type = _producer_declared_field_type(
            "mapper",
            "field_mapper",
            {},
            node_by_id={"mapper": producer},
            field_name="body",
        )

        assert field_type == "str"
        assert len(tracking.instances) == 1
        assert tracking.instances[0].close_count == 1

    def _make_coalesce_schema_mode_state(
        self,
        *,
        source_schema: dict[str, Any],
        transformed_branch_schema: dict[str, Any],
        merge: str | None = "union",
        policy: str | None = "require_all",
        branch_order: tuple[str, str] = ("path_a", "path_b"),
        branch_plugin: str = "passthrough",
        timeout_seconds: float | None = None,
    ) -> CompositionState:
        """Build a schema boundary without introducing an unproven expression result type."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": source_schema},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        branch_options: dict[str, Any] = {"schema": transformed_branch_schema}
        if branch_plugin == "value_transform":
            branch_options["operations"] = [{"target": "value", "expression": "row['value']"}]
        state = state.with_node(
            self._make_transform(
                "branch_b",
                "path_b",
                "path_b_done",
                plugin=branch_plugin,
                options=branch_options,
            )
        )
        branch_connections = {"path_a": "path_a", "path_b": "path_b_done"}
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="path_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={branch_name: branch_connections[branch_name] for branch_name in branch_order},
                policy=policy,
                merge=merge,
                timeout_seconds=timeout_seconds,
            )
        )
        state = state.with_output(self._make_output("main"))
        return state

    def _make_web_scrape_to_line_explode_state(
        self,
        *,
        scrape_options: dict[str, Any] | None = None,
        line_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        scrape_opts = {
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "format": "text",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "pipeline-tests@elspeth.example.gov.au",
                "scraping_reason": "test scrape",
                "allowed_hosts": "public_only",
            },
        }
        scrape_opts.update(scrape_options or {})
        split_opts = {
            "schema": {
                "mode": "flexible",
                "fields": [
                    "url: str",
                    "content: str",
                    "content_fingerprint: str",
                ],
            },
            "required_input_fields": ["content"],
            "source_field": "content",
            "output_field": "line",
            "include_index": True,
            "index_field": "line_index",
        }
        split_opts.update(line_options or {})

        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="scrape_in",
                options={"schema": {"mode": "fixed", "fields": ["url: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "scrape_page",
                "scrape_in",
                "explode_in",
                plugin="web_scrape",
                options=scrape_opts,
            )
        )
        state = state.with_node(
            self._make_transform(
                "split_lines",
                "explode_in",
                "main",
                plugin="line_explode",
                options=split_opts,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "scrape_page"))
        state = state.with_edge(self._make_edge("e2", "scrape_page", "split_lines"))
        state = state.with_edge(self._make_edge("e3", "split_lines", "main"))
        return state

    def test_line_explode_rejects_compact_web_scrape_text(self) -> None:
        """A text scrape with the default space separator is not line-framed.

        After Phase 6 the message no longer mentions ``text_separator`` or
        ``\\n`` — fix prose belongs in PluginAssistance, addressed by
        requirement_code. The state-level surface only has to surface the
        structured violation; the agent retrieves prose via
        ``get_plugin_assistance``.
        """
        state = self._make_web_scrape_to_line_explode_state()

        result = state.validate()

        assert not result.is_valid
        assert any(
            error.component == "node:split_lines"
            and "line_explode" in error.message
            and "line_explode.source_field.line_framed_text" in error.message
            for error in result.errors
        )

    def test_line_explode_accepts_newline_framed_web_scrape_text(self) -> None:
        state = self._make_web_scrape_to_line_explode_state(
            scrape_options={"text_separator": "\n"},
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("line_explode.source_field.line_framed_text" in error.message for error in result.errors)

    def test_line_explode_accepts_markdown_web_scrape_content(self) -> None:
        state = self._make_web_scrape_to_line_explode_state(
            scrape_options={"format": "markdown"},
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def _make_edge(
        self,
        id: str,
        from_node: str,
        to_node: str,
        edge_type: EdgeType = "on_success",
    ) -> EdgeSpec:
        return EdgeSpec(id=id, from_node=from_node, to_node=to_node, edge_type=edge_type, label=None)

    def test_fixed_schema_satisfies_requirement(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert not any("contract" in e.message.lower() for e in result.errors)

    def test_text_explicit_guaranteed_fields_satisfies_requirement(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                plugin="text",
                options={
                    "column": "text",
                    "schema": {"mode": "observed", "guaranteed_fields": ["text"]},
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_field_mapper_computed_output_contract_satisfies_sink_requirement(self) -> None:
        """Composer preview must honor field_mapper's computed output contract."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.from_id == "map_body"
        assert sink_contract.producer_guarantees == ("body",)
        assert sink_contract.consumer_requires == ("body",)
        assert sink_contract.satisfied is True

    def test_named_non_first_source_contract_violation_is_reported(self) -> None:
        """Schema validation must inspect every named source, not only the compatibility source."""
        state = CompositionState(
            source=None,
            sources={
                "customers": self._make_source(
                    on_success="customer_rows",
                    options={"schema": {"mode": "fixed", "fields": ["customer_id: str"]}},
                    on_validation_failure="discard",
                ),
                "orders": self._make_source(
                    on_success="order_rows",
                    plugin="json",
                    options={"schema": {"mode": "fixed", "fields": ["refund_id: str"]}},
                    on_validation_failure="discard",
                ),
            },
            nodes=(
                self._make_transform(
                    "validate_orders",
                    "order_rows",
                    "main",
                    options={"required_input_fields": ["order_id"]},
                ),
            ),
            edges=(),
            outputs=(self._make_output("main"), self._make_output("customer_rows")),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not result.is_valid
        contract = next(edge for edge in result.edge_contracts if edge.to_id == "validate_orders")
        assert contract.from_id == "source:orders"
        assert contract.producer_guarantees == ("refund_id",)
        assert contract.consumer_requires == ("order_id",)
        assert contract.missing_fields == ("order_id",)
        assert any(
            error.component == "source:orders" and "'source:orders' -> 'validate_orders'" in error.message for error in result.errors
        )

    def test_contract_probe_constructor_exception_falls_back_instead_of_crashing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Constructor-time probe failures must not escape Stage 1 validation."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError("invalid template syntax")

            def get_transforms(self) -> list[type]:
                # ADR-007: composer now queries the plugin registry to compute
                # the known-pass-through set. For this mock, return an empty
                # list so the probe-failure path takes the non-pass-through
                # branch (medium warning, raw_guaranteed fallback) — matches
                # the v2 behavior the surrounding test pins.
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        result = state.validate()

        # The probe failure surfaces as a warning, never a crash. The raw
        # fallback for this mapper abstains (observed schema, no guarantees,
        # no participation), so the sink required-fields check defers to
        # runtime per-row validation — no error, no edge-contract verdict —
        # mirroring validate_sink_required_fields' abstention clause.
        assert result.is_valid, result.errors
        assert any("computed contract probe" in warning.message.lower() for warning in result.warnings)
        assert not any(ec.to_id == "output:main" for ec in result.edge_contracts)

    def test_contract_probe_unexpected_constructor_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Framework bugs in transform constructors must not be certified by raw fallback."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "fixed", "fields": ["body: str"]},
                    "mapping": {"text": "body"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "fixed", "fields": ["body: str"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise RuntimeError("framework bug inside transform __init__")

            def get_transforms(self) -> list[type]:
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        with pytest.raises(RuntimeError, match="framework bug inside transform __init__"):
            state.validate()

    def test_rule_c_unexpected_constructor_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rule C must apply the same probe-exception discipline as its siblings.

        Rule C (per-transform self-consistency for ``field_mapper`` with
        ``select_only: True``) constructs the transform to read its computed
        emit set. When that construction raises an unexpected exception (i.e.
        not in the closed set adjudicated by ``_is_config_probe_exception``),
        the discipline established by f3137ae8 — and already implemented by
        the producer-probe sites at ``state.py:884`` and ``state.py:1057`` and
        the semantic-validator helpers in ``_semantic_validator.py`` — is
        that the exception MUST propagate so the bug surfaces at composer-time
        rather than being silently deferred to ``/execute``. Per
        docs/guides/data-trust-and-error-handling.md §Plugin Ownership
        (plugin-as-system-code: a plugin method that raises is a bug
        we MUST know about), Rule C swallowing every exception with a bare
        ``except Exception: continue`` would conceal genuine framework bugs.

        ``_check_schema_contracts`` is invoked directly (rather than via
        ``state.validate()``) because the orchestration in ``validate()`` runs
        ``validate_semantic_contracts`` *before* the schema-contract pass, and
        ``_instantiate_consumer`` already implements the discipline — so a
        ``state.validate()``-level test would see the exception propagate from
        the earlier pass regardless of Rule C's behaviour, and silently miss
        the regression. Calling ``_check_schema_contracts`` in isolation pins
        Rule C as the discipline under test.

        Pipeline shape: source → field_mapper(select_only=True, declares an
        output field absent from the mapping) → sink. The field_mapper's
        upstream is the source sentinel (no producer-probe call), and the
        sink uses ``mode: observed`` with no required_fields (so neither
        sink-Rule-A nor sink-Rule-B reaches ``_producer_emit_profile``). Rule C
        is therefore the only probe site that calls ``create_transform``
        for the broken plugin.
        """
        from elspeth.web.composer.state import _check_schema_contracts

        source = self._make_source(
            on_success="map_select",
            plugin="text",
            options={"schema": {"mode": "observed"}},
        )
        field_mapper_node = self._make_transform(
            "map_select",
            "map_select",
            "main",
            plugin="field_mapper",
            options={
                "schema": {
                    "mode": "fixed",
                    "fields": ["body: str", "batch_size: int"],
                    "required_fields": ["body", "batch_size"],
                },
                "mapping": {"text": "body"},
                "select_only": True,
            },
        )
        sink = OutputSpec(
            name="main",
            plugin="csv",
            options={
                "path": "outputs/main.csv",
                "schema": {"mode": "observed"},
            },
            on_write_failure="discard",
        )

        class _BrokenManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise RuntimeError("framework bug inside field_mapper __init__")

            def get_transforms(self) -> list[type]:
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _BrokenManager(),
        )

        with pytest.raises(RuntimeError, match="framework bug inside field_mapper __init__"):
            _check_schema_contracts({"source": source}, (field_mapper_node,), (sink,))

    def test_contract_probe_redacts_exception_detail_from_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression (P2c): the constructor-time exception message is the
        plugin author's free-form text (plugin options, DSN fragments,
        filesystem paths, occasionally a mis-typed secret) and MUST NOT
        be surfaced to the preview response. The warning surfaced to the
        composer UI carries only ``type(exc).__name__`` — the class name
        is enough triage signal ("something about this plugin's config
        is wrong") without leaking the option values through a Stage 1
        preview endpoint.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="mapper_in",
                plugin="text",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "map_body",
                "mapper_in",
                "main",
                plugin="field_mapper",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"text": "body"},
                    "api_key": {"secret_ref": "CONSTRUCTION_PROBE_SECRET_SENTINEL"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "map_body"))
        state = state.with_edge(self._make_edge("e2", "map_body", "main"))

        # A representative secret-bearing exception message: an API URL
        # with a bearer token fragment, a DSN, and a filesystem path.
        # Production constructors have raised all three shapes.
        leaked_substrings = (
            "Authorization: Bearer sk-SUPER-SECRET-TOKEN-123",
            "postgres://admin:hunter2@db.internal:5432/prod",  # secret-scan: allow-this-line
            "/home/appuser/.ssh/id_rsa",
            "CONSTRUCTION_PROBE_SECRET_SENTINEL",
        )

        class _LeakyManager(DelegatingPluginManagerDouble):
            def create_transform(self, plugin_name: str, options: dict[str, Any]) -> object:
                raise TemplateError(f"plugin '{plugin_name}' failed to initialize: " + " | ".join(leaked_substrings))

            def get_transforms(self) -> list[type]:
                # ADR-007: composer now queries the plugin registry for the
                # known-pass-through set. Empty list keeps the probe-failure
                # path on the v2 (non-pass-through) branch — the redaction
                # test pins that path specifically.
                return []

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _LeakyManager(),
        )

        result = state.validate()

        # The warning still fires (triage signal preserved).
        probe_warnings = [w for w in result.warnings if "computed contract probe" in w.message.lower()]
        assert probe_warnings, "Probe-failure warning must still be emitted"
        # But none of the exception detail leaks into the message.
        for warning in probe_warnings:
            for leak in leaked_substrings:
                assert leak not in warning.message, (
                    f"Contract warning leaked plugin-option detail: {warning.message!r} "
                    f"contained {leak!r}. str(exc) must be replaced with type(exc).__name__."
                )
            # And the class name IS present (triage surface intact).
            assert "TemplateError" in warning.message

    def test_text_heuristic_rescues_original_bug_scenario(self) -> None:
        """Reported text-source scenario passes via the shared observed-text rule."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                plugin="text",
                options={"column": "text", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "combined",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                },
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()
        assert result.is_valid, result.errors
        edge_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert edge_contract.satisfied is True
        assert edge_contract.producer_guarantees == ("text",)
        assert edge_contract.consumer_requires == ("text",)

    def test_no_required_input_fields_skips_check(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(self._make_transform("t1", "t1", "main"))
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_empty_required_input_fields_skips_to_schema_fallback(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": []},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors

    def test_source_direct_to_sink_records_contract(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert result.is_valid, result.errors
        assert len(result.edge_contracts) == 1
        sink_contract = result.edge_contracts[0]
        assert sink_contract.from_id == "source"
        assert sink_contract.to_id == "output:main"
        assert sink_contract.satisfied is True

    def test_sink_required_fields_satisfied(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert result.is_valid, result.errors
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is True
        assert "text" in sink_contract.consumer_requires

    def test_consumer_schema_required_fields_satisfied(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"schema": {"mode": "observed", "required_fields": ["text"]}},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        edge_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert edge_contract.satisfied is True
        assert edge_contract.consumer_requires == ("text",)

    def test_observed_schema_no_guarantees_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema contract violation" in e.message.lower() for e in result.errors)
        assert any("text" in e.message for e in result.errors)

    def test_partial_match_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text", "score"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("score" in e.message for e in result.errors)

    def test_optional_field_not_guaranteed(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str?"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)

    def test_no_schema_config_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(self._make_source(options={}))
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema contract violation" in e.message.lower() for e in result.errors)

    def test_malformed_schema_emits_error(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "invalid_mode"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema" in e.message.lower() for e in result.errors)

    def test_sink_required_fields_violation_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="main",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/main.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        result = state.validate()
        assert not result.is_valid
        assert any("sink" in e.message.lower() and "text" in e.message.lower() for e in result.errors)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is False
        assert "text" in sink_contract.missing_fields

    def test_sink_required_fields_accepts_mapper_derived_target_guarantee(self) -> None:
        """Configured mapping sources make every target present on success.

        The executor dispatches any row missing a configured source before
        ``process``. The mapper therefore participates with its derived target
        guarantees even when its authored observed schema has no local
        ``guaranteed_fields``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": ["url", "summary"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"url": "url", "summary": "summary"},
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.producer_guarantees == ("summary", "url")
        assert sink_contract.satisfied is True

    @pytest.mark.parametrize("proven_sources", [("raw_url", "raw_summary"), ("raw_url",)])
    def test_select_only_mapper_declares_all_derived_target_guarantees(self, proven_sources: tuple[str, ...]) -> None:
        """Upstream schema lower bounds do not narrow successful-row outputs."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": list(proven_sources)}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"raw_url": "url", "raw_summary": "summary"},
                    "schema": {"mode": "observed", "guaranteed_fields": list(proven_sources)},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url", "summary"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert result.is_valid
        assert sink_contract.satisfied is True
        assert sink_contract.producer_guarantees == ("summary", "url")
        assert sink_contract.missing_fields == ()

    def test_sink_required_fields_inherited_participation_still_fails(self) -> None:
        """A pass-through downstream of a participating producer cannot abstain.

        Runtime parity (``walk_effective_guarantee_vote``): a pass-through
        transform's participation is its OWN vote OR any predecessor's. Here
        the source participates with explicit zero guarantees
        (``guaranteed_fields: []``) and feeds an own-abstaining passthrough,
        so the runtime marks the sink's upstream as participated-with-empty
        and ``validate_sink_required_fields`` rejects the missing field. The
        composer must reject too — treating this as abstention would pass
        preview for a pipeline the runtime refuses to build.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": []}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="passthrough",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message and "output:main" in e.message for e in result.errors)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.satisfied is False
        assert "text" in sink_contract.missing_fields

    def test_contract_probe_ignores_authoring_metadata(self) -> None:
        """Composer-only authoring keys must not break the contract probe.

        The composer stages ``interpretation_requirements`` inside node
        options; every plugin config rejects unknown keys, so probing with
        unstripped options is a guaranteed ValueError -> a spurious
        "Computed contract probe ... failed" warning surfaced to the user
        (and a fail-closed Stage-1 rejection for pass-through plugins).
        The probe must strip authoring metadata the same way YAML export does.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "observed", "guaranteed_fields": ["url", "summary"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"url": "url", "summary": "summary"},
                    "schema": {"mode": "observed"},
                    "interpretation_requirements": [
                        {
                            "id": "drop_raw_html_review",
                            "kind": "pipeline_decision",
                            "user_term": "drop_raw_html_fields",
                            "status": "resolved",
                            "draft": "Drop the scraped raw HTML fields.",
                            "event_id": "e831b8ee-2c3e-449b-975e-d213fb7eecad",
                            "accepted_value": "Drop the scraped raw HTML fields.",
                            "accepted_artifact_hash": "871d0cf160b91d8dadc0f58504a86bbed14b9fe28d05bdd0a7c918b1e5b07aa9",
                        }
                    ],
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="json",
                options={
                    "path": "outputs/results.json",
                    "schema": {"mode": "observed", "required_fields": ["url"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert result.is_valid, result.errors
        assert not any("contract probe" in w.message.lower() for w in result.warnings), [w.message for w in result.warnings]

    # ---- Mapping guarantee authority after d4ae04b374 ---------------------

    @staticmethod
    def _run_field_mapper_as_the_executor_would(options: dict[str, Any], row: Any) -> Any:
        """Execute the preflight -> process -> output-contract chain."""
        from elspeth.contracts.schema_contract import PipelineRow
        from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.testing import make_pipeline_row
        from tests.fixtures.factories import make_context

        transform = get_shared_plugin_manager().create_transform("field_mapper", options)
        try:
            pipeline_row = row if isinstance(row, PipelineRow) else make_pipeline_row(row)
            transform.input_schema.model_validate(pipeline_row.to_dict(), strict=True)
            result = transform.process(pipeline_row, make_context())
            if result.status == "success":
                verify_schema_config_mode(
                    output_schema_config=transform._output_schema_config,
                    emitted_rows=(result.row,),
                    plugin_name=transform.name,
                    node_id="t1",
                    run_id="run-1",
                    row_id="row-1",
                    token_id="token-1",
                )
            return result
        finally:
            transform.close()

    def test_rule_c_advises_per_source_class_within_one_node(self) -> None:
        """The historical Rule C classes now share one mapping authority.

        The input-side enforcement differs by representability, but every
        successful row contains every configured target. Rule C must therefore
        stay quiet instead of prescribing per-source repair clauses. The test
        name is retained so the integrated affected matrix keeps its original
        regression identifier.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(options={"schema": {"mode": "observed"}}))
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"first_name": "fname", "user.name": "uname", "Name": "nm"},
                    "schema": {"mode": "flexible", "fields": ["fname: str", "uname: str", "nm: str"]},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["fname: str", "uname: str", "nm: str"],
                        "required_input_fields": ["fname", "uname", "nm"],
                    }
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert not any(error.error_code == "transform_declared_output_not_guaranteed" for error in result.errors)
        sink_contract = next(contract for contract in result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.producer_guarantees == ("fname", "nm", "uname")
        assert sink_contract.satisfied is True

    def test_rule_c_offers_strict_only_for_a_nested_read_and_it_works(self) -> None:
        """The historical strict-only premise is retired after d4ae04b374.

        A dotted mapping with its root declared guarantees the target without
        ``strict``. The old regression identifier is retained deliberately.
        """
        state = self._empty_state()
        state = state.with_source(self._make_source(options={"schema": {"mode": "observed"}}))
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                plugin="field_mapper",
                options={
                    "select_only": True,
                    "mapping": {"user.name": "uname"},
                    "schema": {"mode": "fixed", "fields": ["user: any"]},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["uname: any"],
                        "required_input_fields": ["uname"],
                    }
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert not any(error.error_code == "transform_declared_output_not_guaranteed" for error in result.errors)
        sink_contract = next(contract for contract in result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.producer_guarantees == ("uname",)
        assert sink_contract.satisfied is True

    def test_rule_c_nested_read_remedy_is_executable_and_the_old_one_was_not(self) -> None:
        """The fixed input model names ``user``; the emitted target is not an input.

        The old target-only schema is rejected at construction. With the root
        declared, a present leaf succeeds and a missing child routes, which is
        the behavior that makes ``uname`` guaranteed
        on every successful row. The historical regression identifier is kept
        for integration-matrix traceability.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        incoherent = {
            "select_only": True,
            "mapping": {"user.name": "uname"},
            "schema": {"mode": "fixed", "fields": ["uname: str"]},
        }
        with pytest.raises(PluginConfigError, match=r"fixed schema forbids.*'user'"):
            self._run_field_mapper_as_the_executor_would(incoherent, {"user": {"name": "Ada"}})

        coherent = {
            "select_only": True,
            "mapping": {"user.name": "uname"},
            "schema": {"mode": "fixed", "fields": ["user: any"]},
        }
        present = self._run_field_mapper_as_the_executor_would(coherent, {"user": {"name": "Ada"}})
        missing_child = self._run_field_mapper_as_the_executor_would(coherent, {"user": {}})

        assert present.status == "success"
        assert present.row.to_dict() == {"uname": "Ada"}
        assert missing_child.status == "error"
        assert missing_child.reason["reason"] == "missing_field"

    def test_historical_rule_c_diagnostic_still_routes_separately_from_collision(self) -> None:
        """Persisted/older diagnostics retain code-keyed explanation parity."""
        from elspeth.web.composer.state import (
            _TRANSFORM_DECLARED_NOT_GUARANTEED_FIX,
            _TRANSFORM_OUTPUT_COLLISION_FIX,
        )
        from elspeth.web.composer.tools.generation import _VALIDATION_ERROR_PATTERNS

        def first_match_fix(message: str) -> str | None:
            for pattern, _explanation, fix in _VALIDATION_ERROR_PATTERNS:
                if re.search(pattern, message):
                    return fix
            return None

        guarantee_message = "Transform output guarantee violation: internal field_mapper contract drift."
        collision_message = (
            "Transform contract violation: node 'rewrite' (llm) declares output fields [headline] but "
            "[headline] already arrive(s) on its input row."
        )
        assert first_match_fix(guarantee_message) is _TRANSFORM_DECLARED_NOT_GUARANTEED_FIX
        assert first_match_fix(collision_message) is _TRANSFORM_OUTPUT_COLLISION_FIX

    def test_consumer_schema_required_fields_violation_fails(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={
                    "required_input_fields": [],
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)

    def test_malformed_consumer_schema_emits_error(self) -> None:
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"schema": {"mode": "invalid_mode"}},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        result = state.validate()
        assert not result.is_valid
        assert any("schema" in e.message.lower() for e in result.errors)
        assert not any(ec.to_id == "t1" for ec in result.edge_contracts)

    def test_multiple_transforms_can_share_sink_target(self) -> None:
        """Shared sink targets stay outside the internal producer namespace.

        Uses ``mode: flexible`` for t1/t2 input contracts: this test exercises
        shared-sink-target wiring, not strict input schemas. With ``mode: fixed``
        the auto-injected ``_placeholder`` field that ``_make_transform`` adds
        via its default value_transform operation would be rejected by t2's
        locked input contract (Rule A) — surfacing as a real runtime violation
        but unrelated to the wiring property under test.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "t2",
                options={
                    "required_input_fields": ["text"],
                    "schema": {"mode": "flexible", "fields": ["text: str"]},
                },
                on_error="errors",
            )
        )
        state = state.with_node(
            self._make_transform(
                "t2",
                "t2",
                "main",
                options={
                    "required_input_fields": ["text"],
                    "schema": {"mode": "flexible", "fields": ["text: str"]},
                },
                on_error="errors",
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(
            OutputSpec(
                name="errors",
                plugin="csv",
                options={
                    "path": "outputs/errors.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "t2"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("duplicate producer" in e.message.lower() for e in result.errors)
        error_sink_contracts = [ec for ec in result.edge_contracts if ec.to_id == "output:errors"]
        assert len(error_sink_contracts) == 2
        assert all(ec.satisfied for ec in error_sink_contracts)

    def test_same_gate_multiple_routes_to_same_sink_emit_one_contract(self) -> None:
        """Composer preview dedupes indistinguishable gate->sink contract rows."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="router",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "errors", "false": "errors"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="errors",
                plugin="csv",
                options={
                    "path": "outputs/errors.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "router"))

        result = state.validate()

        assert result.is_valid, result.errors
        error_sink_contracts = [ec for ec in result.edge_contracts if ec.to_id == "output:errors"]
        assert len(error_sink_contracts) == 1
        assert error_sink_contracts[0].from_id == "source"
        assert error_sink_contracts[0].satisfied is True

    def test_coalesce_placeholder_input_is_not_counted_as_consumer(self) -> None:
        """Coalesce.input is a composer placeholder, not a runtime consumer claim."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("ta", "branch_a", "a_out"))
        state = state.with_node(self._make_transform("tb", "branch_b", "b_out"))
        state = state.with_node(
            NodeSpec(
                id="merge",
                node_type="coalesce",
                plugin=None,
                input="branch_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"branch_a": "a_out", "branch_b": "b_out"},
                policy="require_all",
                merge="nested",
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(self._make_edge("e2", "fork_gate", "ta"))
        state = state.with_edge(self._make_edge("e3", "fork_gate", "tb"))
        state = state.with_edge(self._make_edge("e4", "ta", "merge"))
        state = state.with_edge(self._make_edge("e5", "tb", "merge"))
        state = state.with_edge(self._make_edge("e6", "merge", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any("duplicate consumer" in e.message.lower() for e in result.errors)

    # --- Topology cases ---

    def test_gate_inherits_source_guarantees(self) -> None:
        """Gate route targets inherit source guarantees through walk-back."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "main", "false": "errors"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "main",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out"))
        state = state.with_output(self._make_output("errors"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "t1"))

        result = state.validate()

        assert result.is_valid, result.errors
        t1_contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert t1_contract.from_id == "source"
        assert t1_contract.satisfied is True

    def test_route_gate_two_routes_inherit_guarantees(self) -> None:
        """Both route-gate paths inherit the same upstream guarantees."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "path_a", "false": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "out_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert result.is_valid, result.errors
        consumer_contracts = [ec for ec in result.edge_contracts if ec.to_id in {"ta", "tb"}]
        assert len(consumer_contracts) == 2
        assert {ec.from_id for ec in consumer_contracts} == {"source"}
        assert all(ec.satisfied for ec in consumer_contracts)

    def test_fork_gate_contract_check_skips_with_warning(self) -> None:
        """Fork-gate downstream contract checks stay unresolved with a warning."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "mid_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "mid_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="rejoin",
                node_type="coalesce",
                plugin=None,
                input="mid_a",
                on_success="merged",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"path_a": "mid_a", "path_b": "mid_b"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(self._make_output("merged"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert any("fork" in w.message.lower() and "contract" in w.message.lower() for w in result.warnings)
        assert not any(ec.to_id in {"ta", "tb"} for ec in result.edge_contracts)

    @pytest.mark.parametrize("branch_order", [("path_a", "path_b"), ("path_b", "path_a")])
    def test_union_coalesce_rejects_mixed_observed_explicit_branch_schemas_regardless_of_order(
        self,
        branch_order: tuple[str, str],
    ) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            branch_order=branch_order,
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_schema_mode_mixed"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].severity == "high"
        assert "observed" in entries[0].message.lower()
        assert "explicit" in entries[0].message.lower()

    @pytest.mark.parametrize(
        ("source_schema", "transformed_branch_schema"),
        [
            ({"mode": "observed"}, {"mode": "observed"}),
            (
                {"mode": "fixed", "fields": ["id: int", "value: int"]},
                {"mode": "fixed", "fields": ["id: int", "value: int"]},
            ),
        ],
    )
    def test_union_coalesce_accepts_homogeneous_branch_schema_modes(
        self,
        source_schema: dict[str, Any],
        transformed_branch_schema: dict[str, Any],
    ) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema=source_schema,
            transformed_branch_schema=transformed_branch_schema,
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_nested_coalesce_allows_mixed_branch_schema_modes(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            merge="nested",
        )

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_union_coalesce_abstains_when_a_branch_schema_mode_is_unresolved(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            branch_plugin="not_registered",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_union_coalesce_unresolved_branch_does_not_hide_known_mixed_modes(self) -> None:
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
        )
        gate = next(node for node in state.nodes if node.id == "fork_gate")
        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        state = state.with_node(replace(gate, fork_to=("path_a", "path_b", "path_c")))
        state = state.with_node(
            self._make_transform(
                "branch_c",
                "path_c",
                "path_c_done",
                plugin="not_registered",
                options={"schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            replace(
                coalesce,
                branches={"path_a": "path_a", "path_b": "path_b_done", "path_c": "path_c_done"},
            )
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_schema_mode_mixed"]
        assert len(entries) == 1, result.errors

    def test_union_coalesce_rejects_incompatible_shared_field_types(self) -> None:
        """Stage 1 mirrors the runtime's union type-compatibility rule.

        Battery round-6 g03 (elspeth-85f3cc3022): the composer declared the
        same field with different types on two branches it authored in one
        ``set_pipeline`` call. Stage 1 reported ``is_valid=True``, so the
        mutation envelope told the compose loop the pipeline was clean and it
        stopped; only the DAG build rejected it. The type rule is now checked
        where the authoring surface can act on it.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].severity == "high"
        assert "value" in entries[0].message
        assert "'int'" in entries[0].message
        assert "'str'" in entries[0].message

    def test_union_coalesce_type_entry_carries_structured_repair_facts(self) -> None:
        """The planner's feedback strips messages, so the facts must be structured.

        Without these the closed code names the failing NODE but never the
        FIELD, and the repair is unreachable for a field a plugin contributed
        rather than the author declaring it.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "coalesce_union_type_incompatible")
        assert entry.coalesce_union_type is not None
        detail = entry.coalesce_union_type
        assert detail.field == "value"
        assert {detail.branch_a, detail.branch_b} == {"path_a", "path_b"}
        assert {detail.type_a, detail.type_b} == {"int", "str"}
        assert entry.to_dict()["coalesce_union_type"] == {
            "field": "value",
            "branch_a": detail.branch_a,
            "type_a": detail.type_a,
            "branch_b": detail.branch_b,
            "type_b": detail.type_b,
        }

    def test_union_coalesce_type_conflict_is_policy_independent(self) -> None:
        """The verdict must not depend on the coalesce policy.

        Composer derives ``require_all`` from the policy alone while the
        runtime uses ``has_all_branch_semantics``. The two cannot disagree on
        a composer-authored pipeline (no ``quorum_count`` field exists), but
        this pins the stronger property the mirror actually relies on: the type
        conflict is raised before ``require_all`` is read at all, so no policy
        can turn the rejection on or off.
        """
        for policy in ("require_all", "quorum", "best_effort", "first"):
            state = self._make_coalesce_schema_mode_state(
                source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
                transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            )
            coalesce = next(node for node in state.nodes if node.id == "merge_results")
            state = state.with_node(replace(coalesce, policy=policy))

            result = state.validate()

            assert any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors), (
                f"policy={policy} did not reject",
            )

    def test_union_coalesce_accepts_compatible_shared_field_types(self) -> None:
        """Identical declared types across branches stay valid."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_union_coalesce_rejects_unproven_expression_output_type(self) -> None:
        """An expression target the branch does not type is ``any``: the arriving int is no proof of its output type."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: any"]},
            branch_plugin="value_transform",
        )

        result = state.validate()

        assert not result.is_valid
        [entry] = [error for error in result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert entry.coalesce_union_type is not None
        assert entry.coalesce_union_type.field == "value"
        assert {entry.coalesce_union_type.type_a, entry.coalesce_union_type.type_b} == {"int", "any"}

    def test_union_coalesce_accepts_a_typed_expression_target_as_its_declared_type(self) -> None:
        """A target the branch TYPES is the operator's output declaration, enforced by value_transform's pin (ADR-050)."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            branch_plugin="value_transform",
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_union_coalesce_type_check_abstains_on_unresolved_branch(self) -> None:
        """One resolvable branch is not enough to prove a conflict."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            branch_plugin="not_registered",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_union_coalesce_mode_mixed_suppresses_the_type_entry(self) -> None:
        """The runtime raises the mode conflict first, so only it is reported.

        Emitting both would hand the repair loop a second, phantom target on a
        node whose real defect is the mode mismatch.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
        )

        result = state.validate()

        assert any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)
        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_nested_coalesce_ignores_incompatible_shared_field_types(self) -> None:
        """Only union merge merges typed branch fields; nested keys by branch."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            merge="nested",
        )

        result = state.validate()

        assert not any(error.error_code == "coalesce_union_type_incompatible" for error in result.errors)

    def test_unset_coalesce_merge_normalizes_to_the_runtime_default(self) -> None:
        """An omitted ``merge`` becomes ``"union"``, because that is what the runtime runs.

        ``CoalesceSettings.merge`` defaults to ``"union"`` (``core/config.py``),
        so a coalesce authored without the optional field IS a union merge at
        run time. Carrying ``None`` through composer state made every union
        rule read the node as "not a union" and skip it. Normalising once at
        construction is what keeps a THIRD union rule, added later, from
        inheriting the same hole.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        assert coalesce.merge == "union"
        # Non-coalesce nodes keep None: ``merge`` is forbidden on them, and
        # row_union rejects the field outright (``row_union_field_forbidden``).
        assert next(node for node in state.nodes if node.id == "fork_gate").merge is None

    def test_unset_coalesce_merge_survives_yaml_generation(self) -> None:
        """The unset field crashed YAML generation outright — not, as assumed, ``merge: null``.

        ``to_dict()`` emits ``merge`` only when it is set, while
        ``yaml_generator`` reads ``c["merge"]`` unconditionally, so an unset
        merge raised ``KeyError`` before pydantic ever saw the config. That
        made the runtime's answer an internal crash rather than a repair
        signal. Normalising at construction closes the path that runs through
        ``state.to_dict()``; a caller injecting its own ``state_dict`` still
        bypasses ``NodeSpec`` entirely.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        node_dict = next(node for node in state.to_dict()["nodes"] if node["id"] == "merge_results")
        assert node_dict["merge"] == "union"
        assert "merge: union" in generate_yaml(state)

    def test_unset_coalesce_policy_normalizes_to_the_runtime_default(self) -> None:
        """An omitted ``policy`` becomes the runtime's own default, read from the model.

        The equality is against ``CoalesceSettings.model_fields["policy"]``
        rather than the literal ``"require_all"`` deliberately: the value of
        normalising is that the two surfaces cannot disagree about what an
        omitted field means, so if the runtime ever changes its default this
        test tracks it instead of pinning a stale copy.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        assert coalesce.policy == CoalesceSettings.model_fields["policy"].default
        # Non-coalesce nodes keep None: ``policy`` is forbidden on them, and the
        # splice canonical-transform check reads it as a disqualifier.
        assert next(node for node in state.nodes if node.id == "fork_gate").policy is None

    def test_unset_coalesce_policy_stays_valid(self) -> None:
        """The runtime runs a policy-less coalesce as require_all, so Stage 1 must accept it.

        ``CoalesceSettings.policy`` DEFAULTS to ``"require_all"``, and the
        production loader parses a policy-less coalesce without complaint.
        Stage 1 nonetheless emitted ``coalesce_missing_policy``
        (elspeth-deb2f5ed93) — a validate-red/runtime-green divergence, the
        inverse of the shapes this surface usually carries, and the one
        violation of the placement rule that Stage 1 models the runtime's
        treatment rather than inventing one.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_invalid_coalesce_policy_still_rejected(self) -> None:
        """Retiring the missing-policy code must not weaken the closed-vocabulary guard.

        The two checks shared an ``if``/``elif`` chain, so deleting the first
        arm restructures the second. A committed value outside the runtime's
        vocabulary is still valid-but-not-runnable and must still be rejected.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy="require_all_branches",
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_policy_invalid"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"

    def test_coalesce_rejects_options_that_runtime_lowering_would_erase(self) -> None:
        """A structural coalesce has no plugin options contract to author.

        The composer used to accept ``options.schema`` here, then silently
        omit the entire options mapping while lowering to ``CoalesceSettings``.
        Refuse the misleading declaration before it can persist.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
        )
        coalesce = next(node for node in state.nodes if node.id == "merge_results")
        state = state.with_node(replace(coalesce, options={"schema": {"mode": "observed"}}))

        result = state.validate()

        [entry] = [error for error in result.errors if error.error_code == "coalesce_config_invalid"]
        assert entry.component == "node:merge_results"
        assert "options" in entry.message

    def test_coalesce_merge_select_is_rejected_as_unauthorable(self) -> None:
        """``merge: select`` cannot be made runnable from the composer, so Stage 1 must say so.

        elspeth-2ed41f0a4a (census 2026-08-17). The runtime's ``select`` merge
        requires ``select_branch`` (``CoalesceSettings.validate_merge_requirements``);
        ``NodeSpec`` carries no such field and the YAML importer lists it as
        unsupported, so a coalesce authored with ``merge: select`` validated
        green and died at ``settings_load`` with "select merge strategy
        requires select_branch". A closed-vocabulary check that ADVERTISES a
        value the surface can never run is the defect, not a feature.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge="select",
        )

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "coalesce_merge_select_unsupported"]
        assert entry.component == "node:merge_results"
        assert "select_branch" in entry.message
        assert not any(e.error_code == "coalesce_merge_invalid" for e in result.errors)

    def test_coalesce_policy_quorum_is_rejected_as_unauthorable(self) -> None:
        """``policy: quorum`` needs ``quorum_count``, which no NodeSpec field carries."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy="quorum",
        )

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "coalesce_policy_quorum_unsupported"]
        assert entry.component == "node:merge_results"
        assert "quorum_count" in entry.message
        assert not any(e.error_code == "coalesce_policy_invalid" for e in result.errors)

    def test_coalesce_best_effort_without_timeout_is_rejected(self) -> None:
        """``policy: best_effort`` requires ``timeout_seconds`` at the runtime; the composer CAN set it."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy="best_effort",
        )

        result = state.validate()

        [entry] = [e for e in result.errors if e.error_code == "coalesce_best_effort_requires_timeout"]
        assert entry.component == "node:merge_results"
        assert "timeout_seconds" in entry.message

    def test_coalesce_best_effort_with_timeout_is_accepted(self) -> None:
        """Positive control for the best_effort/timeout coupling — same state plus ``timeout_seconds``."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy="best_effort",
            timeout_seconds=5.0,
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_unset_coalesce_policy_survives_yaml_generation(self) -> None:
        """The normalised value must be byte-visible in the exported YAML.

        This is the whole disclosure channel: no advisory replaces the retired
        error, so ``policy: require_all`` in the generated settings is what
        tells an author which arrival semantics they got. It also pins the
        normalisation's PLACEMENT — ``to_dict`` bypasses ``validate()``
        entirely, so a fix applied inside ``validate()`` would leave this red,
        exactly as the same seam did for ``merge``.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            policy=None,
        )

        node_dict = next(node for node in state.to_dict()["nodes"] if node["id"] == "merge_results")
        assert node_dict["policy"] == "require_all"
        assert "policy: require_all" in generate_yaml(state)

    def test_union_type_rule_applies_to_a_coalesce_with_merge_unset(self) -> None:
        """The type mirror must fire on the runtime's default merge, not only the declared one.

        Shipped by the very commit that closed the previous union divergence
        (elspeth-85f3cc3022): both union mirrors gated on ``merge == "union"``,
        so an LLM that simply omitted the optional field got ``is_valid=True``
        on a pipeline the runtime rejects. Omission is the CHEAPEST thing an
        authoring model does, which made the gap the likeliest path through
        the surface rather than an exotic one.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: str"]},
            merge=None,
        )

        result = state.validate()

        entries = [error for error in result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert len(entries) == 1, result.errors
        assert entries[0].component == "node:merge_results"
        assert entries[0].coalesce_union_type is not None
        assert entries[0].coalesce_union_type.field == "value"

    def test_mode_rule_applies_to_a_coalesce_with_merge_unset(self) -> None:
        """The mode mirror shares the gate, so it shares the repair."""
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "observed"},
            merge=None,
        )

        result = state.validate()

        assert any(error.error_code == "coalesce_schema_mode_mixed" for error in result.errors)

    def test_unset_coalesce_merge_stays_valid_when_branch_types_agree(self) -> None:
        """POSITIVE CONTROL — applying the union rules must not become rejecting the node.

        The runtime ACCEPTS an unset merge; it defaults it. Stage 1 must
        therefore run the union rules over the node, never reject it for
        having no explicit merge. Without this control a blanket rejection
        would satisfy every other test in this group.
        """
        state = self._make_coalesce_schema_mode_state(
            source_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            transformed_branch_schema={"mode": "fixed", "fields": ["id: int", "value: int"]},
            merge=None,
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_fork_gate_direct_sink_contract_checked(self) -> None:
        """Fork branches that terminate at sinks stay statically checkable."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="text",
                options={
                    "path": "/in.txt",
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={},
                fork_to=("main",),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "/out.csv",
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="g1", to_node="main", edge_type="fork", label="main"))

        result = state.validate()

        assert not result.is_valid
        assert not any("fork" in w.message.lower() and "contract" in w.message.lower() for w in result.warnings)
        sink_contract = next(ec for ec in result.edge_contracts if ec.to_id == "output:main")
        assert sink_contract.from_id == "source"
        assert sink_contract.consumer_requires == ("text",)
        assert sink_contract.satisfied is False

    def test_fork_branch_name_cannot_overlap_coalesce_branch_and_sink(self) -> None:
        """Composer must reject branch names that runtime routes to coalesce before sink."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("main", "review"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_coalesce(
                "merge",
                "branches",
                "merged",
                branches=("main", "review"),
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("merged"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(EdgeSpec(id="e2", from_node="g1", to_node="main", edge_type="fork", label="main"))
        state = state.with_edge(EdgeSpec(id="e3", from_node="g1", to_node="merge", edge_type="fork", label="review"))

        result = state.validate()

        assert not result.is_valid
        assert any("Connection names overlap with sink names" in error.message and "main" in error.message for error in result.errors)

    def test_multi_hop_transform_no_schema_breaks_chain(self) -> None:
        """A schema-less transform breaks downstream guarantees across hops."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="source_to_ta",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "source_to_ta",
                "ta_out",
                plugin="passthrough",
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "ta_out",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "ta", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.from_id == "ta"
        assert tb_contract.satisfied is False

    def test_transform_then_gate_walk_back_terminates(self) -> None:
        """Walk-back stops at the first non-gate producer in the chain."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="ta_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "ta_in",
                "gate_in",
                plugin="passthrough",
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"high": "tb_in", "low": "sink"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "tb_in",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out"))
        state = state.with_output(self._make_output("sink"))
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "ta", "g1"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("text" in e.message for e in result.errors)
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.from_id == "ta"
        assert tb_contract.satisfied is False

    def test_multi_sink_gate_routing(self) -> None:
        """Route gates emit one satisfied sink contract per direct sink target."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "sink_a", "false": "sink_b"},
            )
        )
        state = state.with_output(
            OutputSpec(
                name="sink_a",
                plugin="csv",
                options={
                    "path": "outputs/sink_a.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="sink_b",
                plugin="csv",
                options={
                    "path": "outputs/sink_b.csv",
                    "schema": {"mode": "observed", "required_fields": ["text"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "sink_a", edge_type="route_true"))
        state = state.with_edge(self._make_edge("e3", "g1", "sink_b", edge_type="route_false"))

        result = state.validate()

        assert result.is_valid, result.errors
        sink_contracts = [ec for ec in result.edge_contracts if ec.to_id in {"output:sink_a", "output:sink_b"}]
        assert len(sink_contracts) == 2
        assert {ec.to_id for ec in sink_contracts} == {"output:sink_a", "output:sink_b"}
        assert {ec.from_id for ec in sink_contracts} == {"source"}
        assert all(ec.satisfied for ec in sink_contracts)

    def test_mixed_consumer_requirements_from_same_producer(self) -> None:
        """One upstream can satisfy one route and fail another."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"true": "path_a", "false": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "path_a",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "out_b",
                options={"required_input_fields": ["score"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("score" in e.message for e in result.errors)
        ta_contract = next(ec for ec in result.edge_contracts if ec.to_id == "ta")
        assert ta_contract.satisfied is True
        tb_contract = next(ec for ec in result.edge_contracts if ec.to_id == "tb")
        assert tb_contract.satisfied is False
        assert "score" in tb_contract.missing_fields

    def test_aggregation_consumer_required_input_fields_fail(self) -> None:
        """Aggregation consumers honor required_input_fields contracts."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "value_field": "value",
                    "required_input_fields": ["value"],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)

    def test_aggregation_required_input_fields_rejected_even_when_upstream_satisfies_contract(self) -> None:
        """ADR-013 has no batch-aware pre-emission dispatch for required_input_fields."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["amount: float"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error="discard",
                options={
                    "value_field": "amount",
                    "required_input_fields": ["amount"],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output(name="main"))
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        messages = "\n".join(entry.message for entry in result.errors)
        assert "required_input_fields" in messages
        assert "batch-aware" in messages
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.satisfied is True

    def test_distribution_profile_unknown_value_type_warns_to_sample_or_use_top_k(self) -> None:
        """Observed upstream schema cannot prove value_field is numeric before execute."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="profile_in",
                options={
                    "schema": {
                        "mode": "observed",
                        "guaranteed_fields": ["community", "financial_barrier"],
                    }
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="profile_barriers",
                node_type="aggregation",
                plugin="batch_distribution_profile",
                input="profile_in",
                on_success="main",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "value_field": "financial_barrier",
                    "group_by": "community",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_edge(self._make_edge("e1", "source", "profile_barriers"))
        state = state.with_edge(self._make_edge("e2", "profile_barriers", "main"))

        result = state.validate()

        assert result.is_valid, result.errors
        warnings = [entry for entry in result.warnings if entry.component == "node:profile_barriers"]
        assert any(
            warning.severity == "high"
            and "batch_distribution_profile.value_field.numeric" in warning.message
            and "batch_top_k" in warning.message
            for warning in warnings
        )

    def test_aggregation_nested_wrapper_required_input_fields_fail(self) -> None:
        """Aggregation wrapper-shaped options.required_input_fields is honored."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "options": {
                        "value_field": "value",
                        "required_input_fields": ["value"],
                        "schema": {"mode": "observed"},
                    }
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.consumer_requires == ("value",)
        assert agg_contract.satisfied is False

    def test_aggregation_nested_wrapper_schema_required_fields_fail(self) -> None:
        """Aggregation wrapper-shaped options.schema.required_fields is honored."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={
                    "options": {
                        "value_field": "value",
                        "schema": {"mode": "observed", "required_fields": ["value"]},
                    }
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        assert any("value" in e.message for e in result.errors)
        agg_contract = next(ec for ec in result.edge_contracts if ec.to_id == "agg1")
        assert agg_contract.consumer_requires == ("value",)
        assert agg_contract.satisfied is False

    def test_aggregation_non_mapping_wrapper_options_surface_as_validation_error(self) -> None:
        """A non-Mapping ``options.options`` wrapper value surfaces as a high-severity
        ValidationEntry, not a silent fallback to the flat outer options.

        Pins the S-6 behavioral improvement: the inline duplication previously here
        silently fell through to the outer options when ``node.options["options"]``
        existed but was not a Mapping. The canonical helper
        ``get_aggregation_contract_options`` raises ValueError on that shape, and the
        ``_check_schema_contracts`` call site converts the error into a blocking
        ``ValidationEntry`` so misconfigured wrappers cannot bypass locked-input
        membership checks.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="agg1",
                options={"schema": {"mode": "fixed", "fields": ["line: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error=None,
                options={"options": "not-a-mapping"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "agg1"))

        result = state.validate()

        assert not result.is_valid
        wrapper_errors = [e for e in result.errors if e.component == "node:agg1" and "Invalid contract config" in e.message]
        assert wrapper_errors, (
            "Expected a high-severity 'Invalid contract config' error on node:agg1 "
            f"for the non-Mapping wrapper, got: {[(e.component, e.message) for e in result.errors]}"
        )
        assert any(e.severity == "high" for e in wrapper_errors)

    def test_coalesce_producer_emits_skip_warning(self) -> None:
        """A coalesce Composer cannot MIRROR stays unresolved until runtime.

        The population is ``select`` alone (``_MIRRORED_COALESCE_MERGES``): it
        forwards ONE branch's raw schema keyed by a ``select_branch`` a
        composer ``NodeSpec`` cannot carry, so there is nothing to mirror and
        abstaining with this advisory is the correct answer. Union and nested
        both resolve through the guarantee walk — see
        ``TestUnionCoalesceGuaranteeExtras`` and
        ``test_nested_coalesce_producer_resolves_to_branch_names`` below.

        The node is separately rejected as unauthorable
        (``coalesce_merge_select_unsupported``), which is why the pipeline is
        invalid; the abstention under test is that no edge contract is asserted
        against ``t1``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="fork_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        # A coalesce's branches must be produced by a gate fork_to (the
        # runtime rejects a coalesce no gate feeds; Stage 1 mirrors that as
        # ``coalesce_branch_alias_unreachable``, elspeth-2ed41f0a4a), so the
        # fixture forks first — the abstention under test is about the
        # coalesce's MERGE mode, not its wiring.
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="fork_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_coalesce(
                "after_merge",
                "branch_a",
                None,
                branches=("branch_a", "branch_b"),
                merge="select",
                # NodeSpec cannot author select_branch (that is the defect
                # this unmirrorable-merge rejection explains), and structural
                # coalesces accept no options at all.
                options={},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "after_merge",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(self._make_edge("e2", "after_merge", "t1"))

        result = state.validate()

        assert [error.error_code for error in result.errors] == ["coalesce_merge_select_unsupported"], result.errors
        assert any("coalesce node" in w.message.lower() and "runtime validator will check" in w.message.lower() for w in result.warnings)
        assert not any(ec.to_id == "t1" for ec in result.edge_contracts)

    def test_nested_coalesce_producer_resolves_to_branch_names(self) -> None:
        """The nested half of the same fixture, which no longer abstains.

        Engine parity: the DAG builder stamps a nested coalesce with a flexible
        schema keyed BY BRANCH NAME, so a consumer requiring an INNER field
        (``text``, guaranteed by the source) is rejected at build with
        ``EdgeContractError``. Stage 1 used to hand this back green with a
        "runtime validator will check this edge" advisory — a preview the
        authoring loop had nothing to repair against.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="fork_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="fork_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            self._make_coalesce(
                "after_merge",
                "branch_a",
                None,
                branches=("branch_a", "branch_b"),
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "after_merge",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "fork_gate"))
        state = state.with_edge(self._make_edge("e2", "after_merge", "t1"))

        result = state.validate()

        violation = next(error for error in result.errors if error.error_code == "schema_contract_violation")
        assert "guarantees: [branch_a, branch_b]" in violation.message
        assert "Missing fields: [text]" in violation.message
        contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert contract.producer_guarantees == ("branch_a", "branch_b")

    # --- Guard tests ---

    def test_node_id_source_is_reserved(self) -> None:
        """A node cannot reuse the source sentinel id."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "source",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "source"))

        result = state.validate()

        assert not result.is_valid
        assert any("reserved" in e.message.lower() for e in result.errors)

    def test_node_id_source_namespace_prefix_is_reserved(self) -> None:
        """Nodes cannot collide with named-source producer ids such as source:orders."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "source:orders",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "source:orders"))

        result = state.validate()

        assert not result.is_valid
        assert any(error.component == "node:source:orders" and "source producer namespace" in error.message for error in result.errors)

    @pytest.mark.parametrize("source_name", ["Orders", "bad name", "continue", "__system", "x" * 39])
    def test_plural_source_names_follow_runtime_identifier_constraints(self, source_name: str) -> None:
        """Composer Stage 1 rejects names that runtime settings would reject later."""
        state = CompositionState(
            source=None,
            sources={source_name: self._make_source("main")},
            nodes=(),
            edges=(),
            outputs=(self._make_output("main"),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert not result.is_valid
        assert any(error.component in {"source", f"source:{source_name}"} for error in result.errors)

    def test_bare_string_required_input_fields_emits_error(self) -> None:
        """Bare-string required_input_fields fails closed."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": "text"},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert not result.is_valid
        assert any("bare string" in e.message.lower() for e in result.errors)

    def test_duplicate_producer_connection_emits_error_and_skips_contracts(self) -> None:
        """Duplicate producers fail closed instead of overwriting the namespace."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_gate(
                "g1",
                "gate_in",
                {"a": "dup", "b": "path_b"},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "dup",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "path_b",
                "dup",
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_edge(self._make_edge("e1", "source", "g1"))
        state = state.with_edge(self._make_edge("e2", "g1", "ta"))
        state = state.with_edge(self._make_edge("e3", "g1", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("duplicate producer" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    def test_duplicate_consumer_connection_emits_error_and_skips_contracts(self) -> None:
        """Duplicate consumers fail closed instead of fabricating edge checks."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="shared",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "ta",
                "shared",
                "out_a",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_node(
            self._make_transform(
                "tb",
                "shared",
                "out_b",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("out_a"))
        state = state.with_output(self._make_output("out_b"))
        state = state.with_edge(self._make_edge("e1", "source", "ta"))
        state = state.with_edge(self._make_edge("e2", "source", "tb"))

        result = state.validate()

        assert not result.is_valid
        assert any("duplicate consumer" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    def test_connection_name_overlaps_sink_name_emits_error_and_skips_contracts(self) -> None:
        """Connection/sink namespace overlap aborts contract telemetry."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="t1",
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
            )
        )
        state = state.with_node(
            self._make_transform(
                "t2",
                "main",
                "out",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output("main"))
        state = state.with_output(self._make_output("out"))
        state = state.with_edge(self._make_edge("e1", "source", "t1"))
        state = state.with_edge(self._make_edge("e2", "t1", "t2"))

        result = state.validate()

        assert not result.is_valid
        assert any("disjoint" in e.message.lower() or "overlap" in e.message.lower() for e in result.errors)
        assert result.edge_contracts == ()

    # --- Data integrity ---

    def test_edge_contracts_populated_correctly(self) -> None:
        """ValidationSummary.edge_contracts carries the expected edge data."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        assert result.is_valid, result.errors
        assert len(result.edge_contracts) >= 1
        contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        assert contract.from_id == "source"
        assert contract.producer_guarantees == ("text",)
        assert contract.consumer_requires == ("text",)
        assert contract.missing_fields == ()
        assert contract.satisfied is True

    def test_edge_contract_to_dict_serialization(self) -> None:
        """A real emitted EdgeContract serializes with API-facing keys."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                options={"schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "t1",
                "t1",
                "main",
                options={"required_input_fields": ["text"]},
            )
        )
        state = state.with_output(self._make_output())
        state = state.with_edge(self._make_edge("e1", "source", "t1"))

        result = state.validate()

        contract = next(ec for ec in result.edge_contracts if ec.to_id == "t1")
        payload = contract.to_dict()
        assert payload["from"] == "source"
        assert payload["to"] == "t1"
        assert payload["producer_guarantees"] == ["text"]
        assert payload["consumer_requires"] == ["text"]
        assert payload["missing_fields"] == []
        assert payload["satisfied"] is True
        assert "from_id" not in payload
        assert "to_id" not in payload

    # --- Field-set membership tests (elspeth-3d25355784) ---
    #
    # The three S3 evaluation fixtures (msg{1,2,3}.json captured under
    # /tmp/elspeth_eval/2026-05-03/s3/) are the ground-truth reproducers for
    # the composer-time membership checks. Each surfaces a different rejection
    # shape that previously slipped past /validate (is_valid: true) and only
    # crashed at /execute with a structured engine error. The test cases below
    # mirror those YAMLs so the regression locks in the rejection at the
    # composer boundary.

    def _batch_stats_to_locked_sink_state(self) -> CompositionState:
        """v1 reproducer: ``batch_stats`` → locked-mode JSON sink."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="results",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )
        return state

    def test_v1_locked_sink_rejects_upstream_batch_size_extra(self) -> None:
        """Sink ``mode: fixed`` rejects ``batch_size`` emitted by upstream batch_stats.

        Reproduces /tmp/elspeth_eval/2026-05-03/s3/msg1.json. The composer
        previously accepted this YAML; the engine then crashed at sink_write
        with PluginContractViolation (``Extra inputs are not permitted:
        batch_size``). The new field-set membership check rejects this at
        /validate with a message that names ``batch_size`` — the same field
        the engine names — and points the operator at both fixes.
        """
        state = self._batch_stats_to_locked_sink_state()

        result = state.validate()

        assert not result.is_valid, "Composer must reject locked sink that forbids producer-emitted extras."
        sink_extra_errors = [
            e for e in result.errors if e.component == "output:results" and "batch_size" in e.message and "input is locked" in e.message
        ]
        assert sink_extra_errors, f"Expected sink locked-input rejection naming batch_size, got: {[e.message for e in result.errors]}"
        msg = sink_extra_errors[0].message
        assert "Extra fields rejected by sink input contract: [batch_size]" in msg
        assert "mode: flexible" in msg  # operator-actionable: relax sink schema
        assert "field_mapper" in msg and "select_only: true" in msg  # operator-actionable: drop extras upstream

    def _make_select_only_cleanup_state(self, *, sink_fields: list[str]) -> CompositionState:
        """batch_stats -> field_mapper(select_only) -> json sink.

        The mapper declares ``batch_size`` in its authored ``schema.fields``
        (the INPUT contract — batch_stats really does emit it) and deliberately
        omits it from the whitelist. ``sink_fields`` decides whether anything
        downstream still needs the dropped field.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "results",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": [
                            "batch_size: int",
                            "count: int",
                            "customer_tier: str",
                            "sum: float",
                        ],
                        "required_fields": ["customer_tier", "count", "sum"],
                    },
                    "required_input_fields": ["customer_tier", "count", "sum"],
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": sink_fields,
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )

        return state

    def test_select_only_may_drop_a_declared_input_field_nothing_downstream_needs(self) -> None:
        """Cleanup is what select_only is FOR, so the composer must accept it.

        Regression: elspeth-a2bf676e6f. ``schema.fields`` on a transform is the
        INPUT contract (``BaseTransform._build_output_schema_config``: "the
        transform's input schema config"), so naming ``batch_size`` there and
        leaving it out of the whitelist is an honest "save only these fields"
        gesture — note the node's own ``required_fields`` already excludes it.
        This previously produced a Rule C rejection of a correct pipeline, and
        was the shape that made a renaming mapper unsatisfiable and stalled the
        composer (elspeth-92fa1fe86e).
        """
        state = self._make_select_only_cleanup_state(sink_fields=["customer_tier: str", "count: int", "sum: float"])

        result = state.validate()

        assert result.is_valid, [e.message for e in result.errors]

    def test_select_only_drop_still_rejected_when_a_consumer_requires_the_field(self) -> None:
        """Dropping a field something downstream needs is still caught.

        The safety net the old Rule C rejection provided is not lost — it moves
        to where the requirement actually lives, so the message names the
        consumer instead of accusing the mapper of a self-inconsistency.
        """
        state = self._make_select_only_cleanup_state(sink_fields=["customer_tier: str", "count: int", "sum: float", "batch_size: int"])

        result = state.validate()

        assert not result.is_valid, "A dropped field that a consumer requires must still be rejected."
        assert any("batch_size" in e.message for e in result.errors), [e.message for e in result.errors]

    # ── Rule D: declared output collides with a definitely-arriving input field ──
    # elspeth-cfcd333f83. The runtime surface is TransformExecutor._run_preflight's
    # detect_field_collisions call, which raises PluginContractViolation on row 1.

    def _llm_options(self, response_field: str) -> dict[str, Any]:
        """Minimal constructible llm transform config for the collision probe."""
        return {
            "provider": "gateway",
            "model": "anthropic/claude-sonnet-4.6",
            "endpoint": "https://gateway.example.invalid/v1",
            "api_key": "${LLM_API_KEY}",
            "system_prompt": "You rewrite headlines. Reply with the rewritten headline only.",
            "prompt_template": "Title-case this: {headline}",
            "response_field": response_field,
            "schema": {"mode": "observed"},
        }

    def _llm_rewrite_state(
        self,
        *,
        response_field: str,
        source_plugin: str = "text",
        source_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        """source -> llm -> sink, with the llm writing ``response_field``."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rewrite",
                plugin=source_plugin,
                options=source_options if source_options is not None else {"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rewrite",
                "rewrite",
                "main",
                plugin="llm",
                options=self._llm_options(response_field),
            )
        )
        return state.with_output(self._make_output("main"))

    def test_rule_d_llm_rewrite_in_place_collides_with_source_field(self) -> None:
        """Rule D: an llm rewriting its own input field in place is rejected.

        The reported defect: a text source emits ``headline`` and an llm
        transform sets ``response_field: headline`` to title-case it. Compose
        succeeded and /validate returned is_valid=true, then the run died on
        row 1 because TransformExecutor's collision preflight rejects a
        transform whose declared_output_fields overlap the arriving row.
        """
        result = self._llm_rewrite_state(response_field="headline").validate()

        assert not result.is_valid, "Composer must reject an llm that overwrites a field already on the row."
        rule_d_errors = [e for e in result.errors if e.component == "node:rewrite" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected a Rule D rejection naming headline, got: {[e.message for e in result.errors]}"
        msg = rule_d_errors[0].message
        assert "[headline] already arrive(s) on its input row" in msg
        # Actionable in both repair directions, so the planner can fix it.
        assert "response_field" in msg
        assert "field_mapper" in msg
        # Only the colliding field is reported — the llm's other declared
        # outputs (headline_model / headline_usage) do not arrive on the row.
        contract = rule_d_errors[0].contract
        assert contract is not None
        assert contract.extra_fields == ("headline",)
        assert contract.producer == "rewrite"
        assert contract.consumer == "rewrite"

    def test_rule_d_negative_control_non_colliding_output_authors_cleanly(self) -> None:
        """Rule D stays silent when the declared output is a fresh field name.

        Same topology as the rejection above with only ``response_field``
        changed, so a failure here means Rule D fires on shape rather than on
        the collision itself.
        """
        result = self._llm_rewrite_state(response_field="headline_titlecased").validate()

        assert result.is_valid, result.errors

    def test_rule_d_abstains_when_arrival_is_not_definite(self) -> None:
        """Rule D abstains when the field is not PROVEN to arrive.

        A csv source with an observed schema declares no guaranteed fields, so
        ``_connection_definite_emits`` contributes nothing and the composer
        cannot prove ``headline`` reaches the node. These predicates are lower
        bounds: erroring on a merely-possible collision would be a false
        rejection, so the runtime preflight owns this case per-row.
        """
        result = self._llm_rewrite_state(
            response_field="headline",
            source_plugin="csv",
            source_options={"schema": {"mode": "observed"}},
        ).validate()

        assert result.is_valid, result.errors

    def test_rule_d_skips_a_select_only_field_mapper_that_cannot_overwrite(self) -> None:
        """Rule D is capability-keyed: a fresh-dict writer cannot overwrite (elspeth-6ea3619737).

        A ``select_only`` field_mapper declares its rename target (an honest
        guarantee — the mapping requires its source), and the target name
        definitely arrives on its input. But ``process`` builds its output from
        a fresh ``{}``: the arriving field is dropped, never overwritten —
        which is what select_only MEANS. Before the capability key this shape
        was rejected here and lost 100% of rows at the runtime twin.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["a", "b"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"a": "b"},
                    "select_only": True,
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_rule_d_fires_on_an_open_branch_fixed_schema_rename_onto_a_guaranteed_field(self) -> None:
        """Declaration-channel stability (elspeth-0d1da6dc44): fixed schema arms Rule D too.

        Under ``select_only: false`` the mapper deep-copies the row and writes
        the target — a real overwrite of the arriving ``c``. Spelling the input
        promise as ``mode: fixed`` required fields used to leave
        ``declared_output_fields`` empty and every collision gate asleep, while
        ``guaranteed_fields: [a, c]`` — the same promise — was rejected. Both
        spellings must reach the same verdict.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["a", "c"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"a": "c"},
                    "schema": {"mode": "fixed", "fields": ["a: str", "c: str"]},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not result.is_valid, "The fixed-schema spelling must not disarm the collision gate."
        rule_d_errors = [e for e in result.errors if e.component == "node:rename" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected a Rule D rejection naming c, got: {[e.message for e in result.errors]}"
        assert "[c] already arrive(s) on its input row" in rule_d_errors[0].message

    def test_rule_d_abstains_when_original_header_collision_depends_on_runtime_lineage(self) -> None:
        """Static validation must not reject a possible runtime identity.

        Composer knows ``full_name`` arrives but cannot know whether ``Name``
        resolves to that same field or to a different normalized/source-mapped
        key. FieldMapper's row-aware guard owns the uncertain collision; Rule D
        remains reserved for provable collisions.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["name", "full_name"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"Name": "full_name"},
                    "select_only": False,
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        rule_d_errors = [e for e in result.errors if e.component == "node:rename" and e.error_code == "transform_contract_violation"]
        assert not rule_d_errors, [e.message for e in rule_d_errors]

    def test_open_mapper_propagates_guaranteed_passthrough_to_sink_contract(self) -> None:
        """The local target lower bound composes with forwarded predecessor fields."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "fixed", "fields": ["a: str", "keep: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"a": "b"},
                    "select_only": False,
                    "schema": {"mode": "observed"},
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["keep: str", "b: str?"],
                        "required_input_fields": ["keep"],
                    }
                },
                on_write_failure="discard",
            )
        )

        result = state.validate()

        assert result.is_valid, result.errors
        sink_contract = next(contract for contract in result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.producer_guarantees == ("b", "keep")
        assert sink_contract.satisfied is True

    def test_open_mapper_forwarded_type_mismatch_is_rejected(self) -> None:
        """Composer mirrors runtime type evidence carried by an open forwarder."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "fixed", "fields": ["a: str", "keep: str"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "mapped",
                plugin="field_mapper",
                options={
                    "mapping": {"a": "b"},
                    "select_only": False,
                    "schema": {"mode": "flexible", "fields": ["a: str"]},
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "typed",
                "mapped",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"keep": "keep"},
                    "select_only": True,
                    "schema": {"mode": "flexible", "fields": ["keep: int"]},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        mismatch = [entry for entry in result.errors if entry.error_code == "edge_field_type_incompatible"]
        assert mismatch
        assert "keep (consumer expects int, producer emits str)" in mismatch[0].message

    @pytest.mark.parametrize(
        ("source_plugin", "source_options", "expects_mismatch"),
        [
            ("csv", {"schema": {"mode": "observed", "guaranteed_fields": ["a", "keep"]}}, True),
            (
                "json",
                {"path": "/data/input.json", "schema": {"mode": "observed", "guaranteed_fields": ["a", "keep"]}},
                False,
            ),
        ],
    )
    def test_open_mapper_forwards_only_known_observed_source_structural_type(
        self,
        source_plugin: str,
        source_options: dict[str, Any],
        expects_mismatch: bool,
    ) -> None:
        """Structural CSV typing propagates; an untyped observed source abstains."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                plugin=source_plugin,
                options=source_options,
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "mapped",
                plugin="field_mapper",
                options={
                    "mapping": {"a": "b"},
                    "select_only": False,
                    "schema": {"mode": "flexible", "fields": ["a: str"]},
                },
            )
        )
        state = state.with_node(
            self._make_transform(
                "typed",
                "mapped",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"keep": "keep"},
                    "select_only": True,
                    "schema": {"mode": "flexible", "fields": ["keep: int"]},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        mismatch = [entry for entry in result.errors if entry.error_code == "edge_field_type_incompatible"]
        assert bool(mismatch) is expects_mismatch
        if mismatch:
            assert "keep (consumer expects int, producer emits str)" in mismatch[0].message

    def test_rule_d_fires_on_a_required_fields_rename_onto_a_guaranteed_field(self) -> None:
        """Third declaration channel (adversarial review of a7c783423): required_fields.

        ``schema.required_fields`` is build-time fail-closed against every
        non-guaranteeing upstream, so it is the same per-row promise as
        ``guaranteed_fields`` — the same rename must get the same verdict.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rename",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["email", "username"]}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rename",
                "rename",
                "main",
                plugin="field_mapper",
                options={
                    "mapping": {"email": "username"},
                    "schema": {"mode": "observed", "required_fields": ["email", "username"]},
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not result.is_valid, "The required_fields spelling must not disarm the collision gate."
        rule_d_errors = [e for e in result.errors if e.component == "node:rename" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected a Rule D rejection naming username, got: {[e.message for e in result.errors]}"
        assert "[username] already arrive(s) on its input row" in rule_d_errors[0].message

    def test_rule_d_fires_when_only_one_row_union_arm_delivers_the_field(self) -> None:
        """Rule D rejects a collision carried by a SINGLE fan-in arm.

        ``_connection_definite_emits`` deliberately UNIONS row_union arm emit
        sets where the presence walk would intersect them, because the two
        directions have opposite safety polarities. A row_union republishes
        each arm's rows unchanged, so a field guaranteed by one arm really is
        present on that arm's rows; the executor's collision preflight runs
        per row and dies on them. Requiring the field on EVERY arm would miss
        this genuine failure, so union — not intersection — is correct here.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="gate_in",
                plugin="text",
                options={"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("arm_a_in", "arm_b_in"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        # Only arm A mints `tag`; arm B carries the bare source row.
        state = state.with_node(self._make_transform("tagger", "arm_a_in", "arm_a_out", plugin="llm", options=self._llm_options("tag")))
        state = state.with_node(
            NodeSpec(
                id="union",
                node_type="row_union",
                plugin=None,
                input="arm_a_out",
                on_success="union_out",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                # Barrier branches are keyed by FORK BRANCH NAME; arm B reaches
                # the barrier untransformed, so its key and connection coincide.
                branches={"arm_a_in": "arm_a_out", "arm_b_in": "arm_b_in"},
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(self._make_transform("retag", "union_out", "main", plugin="llm", options=self._llm_options("tag")))
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        rule_d_errors = [e for e in result.errors if e.component == "node:retag" and e.error_code == "transform_contract_violation"]
        assert rule_d_errors, f"Expected Rule D to reject the single-arm collision, got: {[e.message for e in result.errors]}"
        assert rule_d_errors[0].contract is not None
        assert "tag" in rule_d_errors[0].contract.extra_fields
        # The collision must be the ONLY reason this pipeline is rejected —
        # otherwise a structural complaint could carry the test and the fan-in
        # semantics would go unverified.
        assert [e.error_code for e in result.errors] == ["transform_contract_violation"], [e.message for e in result.errors]

    def test_rule_d_ignores_transforms_declaring_no_output_fields(self) -> None:
        """A transform that declares no output fields is never a Rule D subject.

        ``value_transform`` deliberately keeps ``declared_output_fields`` empty
        because its targets may legitimately be overwrites — the same opt-out
        the executor's collision check honours. Rule D must inherit that
        abstention rather than re-deriving emission from the schema.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="rewrite",
                plugin="text",
                options={"column": "headline", "schema": {"mode": "observed"}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "rewrite",
                "rewrite",
                "main",
                options={"operations": [{"target": "headline", "expression": "row['headline'].title()"}]},
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert result.is_valid, result.errors

    def test_v3_field_mapper_locked_input_rejects_upstream_batch_size_extra(self) -> None:
        """Rule A: locked-mode field_mapper input rejects upstream batch_stats extra.

        Reproduces /tmp/elspeth_eval/2026-05-03/s3/msg3.json. The composer
        previously accepted this YAML; the engine crashed at input validation
        with PluginContractViolation (``Extra inputs are not permitted:
        batch_size``). The field_mapper's input Pydantic model gets
        ``extra="forbid"`` because its ``schema.mode`` is fixed; upstream
        batch_stats emits ``batch_size`` which is not in the declared
        ``fields``.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "ticket_id: str",
                            "subject: str",
                            "body: str",
                            "customer_tier: str",
                            "amount: float",
                        ],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "results",
                plugin="field_mapper",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                        "required_fields": ["customer_tier", "count", "sum"],
                    },
                    "required_input_fields": ["customer_tier", "count", "sum"],
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                },
            )
        )
        state = state.with_output(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": "outputs/ticket_totals_by_tier.json",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "format": "json",
                    "indent": 2,
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )

        result = state.validate()

        assert not result.is_valid, "Composer must reject locked field_mapper input that forbids producer-emitted extras."
        consumer_extra_errors = [
            e
            for e in result.errors
            if e.component == "node:select_output_fields" and "input is locked" in e.message and "batch_size" in e.message
        ]
        assert consumer_extra_errors, (
            f"Expected consumer locked-input rejection naming batch_size, got: {[e.message for e in result.errors]}"
        )
        msg = consumer_extra_errors[0].message
        assert "Extra fields rejected by consumer input contract: [batch_size]" in msg
        assert "'aggregate_by_tier' -> 'select_output_fields'" in msg  # producer/consumer attribution
        assert "schema.mode: flexible" in msg  # operator-actionable: relax consumer schema
        assert "schema.fields" in msg  # operator-actionable: widen the field declaration
        assert "['batch_size']" in msg  # message names the specific field to add
        # When consumer IS field_mapper, the "insert a field_mapper" suggestion is degenerate.
        assert "insert a field_mapper" not in msg, "Rule A must not suggest inserting a field_mapper when the consumer is already one."

    def test_locked_input_check_does_not_fire_on_flexible_consumer(self) -> None:
        """Rule A negative: ``mode: flexible`` consumer accepts producer extras.

        Sanity guard against over-generalization: the same upstream
        (batch_stats with batch_size) feeding a ``mode: flexible`` consumer
        does not trigger the locked-input rejection. Only ``mode: fixed``
        produces ``extra="forbid"`` on the auto-generated input contract.
        """
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="aggregate_by_tier",
                plugin="csv",
                options={
                    "schema": {
                        "mode": "fixed",
                        "fields": ["customer_tier: str", "amount: float"],
                    },
                },
            )
        )
        state = state.with_node(
            NodeSpec(
                id="aggregate_by_tier",
                node_type="aggregation",
                plugin="batch_stats",
                input="aggregate_by_tier",
                on_success="select_output_fields",
                on_error="discard",
                options={
                    "schema": {"mode": "flexible", "fields": ["customer_tier: str", "amount: float"]},
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                output_mode="transform",
            )
        )
        state = state.with_node(
            self._make_transform(
                "select_output_fields",
                "select_output_fields",
                "main",
                plugin="field_mapper",
                options={
                    # Same shape as v3 except mode=flexible — extras allowed.
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "count: int", "sum: float"],
                    },
                    "mapping": {
                        "customer_tier": "customer_tier",
                        "count": "count",
                        "sum": "sum",
                    },
                    "select_only": True,
                },
            )
        )
        state = state.with_output(self._make_output("main"))

        result = state.validate()

        assert not any("input is locked" in e.message and "batch_size" in e.message for e in result.errors), (
            f"Flexible consumer must not trigger locked-input rejection, got errors: {[e.message for e in result.errors]}"
        )

    # ── Projected declared_input_fields: a transform's own options name a
    # required input column (elspeth-ada5a60249). The runtime surface is
    # DeclaredRequiredFieldsContract.pre_emission_check, which raises before
    # process() runs, so every row fails.

    def _web_scrape_options(self, url_field: str) -> dict[str, Any]:
        """Minimal constructible web_scrape config for the declared-input probe."""
        return {
            "url_field": url_field,
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                # A non-reserved domain, so the composer's abuse_contact rule
                # stays silent and these tests observe only the contract check.
                "abuse_contact": "ops@somecompany.gov.au",
                "scraping_reason": "contract validation test",
                "allowed_hosts": ["127.0.0.0/8"],
            },
            "schema": {"mode": "observed"},
        }

    def _web_scrape_state(self, *, url_field: str, source_mode: str) -> CompositionState:
        """source -> web_scrape -> sink, with the scraper's url_field under test."""
        source_schema: dict[str, Any] = (
            {"mode": "fixed", "fields": ["id: int", "url: str", "label: str"]} if source_mode == "fixed" else {"mode": "observed"}
        )
        state = self._empty_state()
        state = state.with_source(self._make_source(on_success="urls", plugin="csv", options={"schema": source_schema}))
        state = state.with_node(
            self._make_transform(
                "scraper",
                "urls",
                "main",
                plugin="web_scrape",
                options=self._web_scrape_options(url_field),
            )
        )
        return state.with_output(self._make_output("main"))

    def test_declared_input_field_missing_from_typed_producer_is_rejected(self) -> None:
        """The reported defect: a misnamed url_field composed clean and died on row 1.

        ``url_field`` never reaches ``required_input_fields`` or the ``schema:``
        block, so both raw config surfaces were blind; only the constructed
        plugin knows the column name.
        """
        result = self._web_scrape_state(url_field="page_url", source_mode="fixed").validate()

        assert not result.is_valid, "Composer must reject a url_field naming a column no producer emits."
        declared_errors = [
            e
            for e in result.errors
            if e.component == "node:scraper" and e.error_code == "schema_contract_violation" and "page_url" in e.message
        ]
        assert declared_errors, f"Expected a declared-input rejection naming page_url, got: {[e.message for e in result.errors]}"
        msg = declared_errors[0].message
        assert "Missing fields: [page_url]" in msg
        # The message must say WHERE the requirement came from, since the author
        # never wrote `required_input_fields`.
        assert "declared by its own options" in msg
        assert "url_field" in msg

    def test_declared_input_field_satisfied_by_producer_is_clean(self) -> None:
        """Negative control: the correctly wired chaosweb shape must stay valid."""
        result = self._web_scrape_state(url_field="url", source_mode="fixed").validate()

        assert result.is_valid, f"Correctly wired url_field must not be rejected, got: {[e.message for e in result.errors]}"

    def test_declared_input_field_against_observed_producer_abstains(self) -> None:
        """ABSTENTION: an observed producer proves nothing, so enforcement stays per-row.

        This is the case that separates the projected declaration from the raw
        ``required_input_fields`` surface, which fails closed here.
        """
        result = self._web_scrape_state(url_field="page_url", source_mode="observed").validate()

        assert result.is_valid, f"Observed producer must abstain, got: {[e.message for e in result.errors]}"

    def _blob_csv_expand_state(self, *, source_fields: list[str]) -> CompositionState:
        """blob_ref_field OMITTED — its default names 'blob_ref', so nothing in the options says so."""
        state = self._empty_state()
        state = state.with_source(
            self._make_source(
                on_success="manifest",
                plugin="csv",
                options={"schema": {"mode": "fixed", "fields": source_fields}},
            )
        )
        state = state.with_node(
            self._make_transform(
                "expand",
                "manifest",
                "main",
                plugin="blob_csv_expand",
                options={"columns": ["id", "text"], "schema": {"mode": "observed"}},
            )
        )
        return state.with_output(self._make_output("main"))

    def test_declared_input_field_from_option_default_is_rejected(self) -> None:
        """Omission shape: the author wrote no option at all, so only the probe can know.

        Pins that ``prepare_validation_probe_options`` preserves a
        default-derived declaration — nothing in the raw options names
        ``blob_ref``, so a probe that lost the default would leave this rule
        silently inert for every omission-shaped defect.
        """
        result = self._blob_csv_expand_state(source_fields=["manifest_index: int", "source_name: str"]).validate()

        assert not result.is_valid, "Composer must reject a default blob_ref_field no producer emits."
        declared_errors = [e for e in result.errors if e.component == "node:expand" and "blob_ref" in e.message]
        assert declared_errors, f"Expected a declared-input rejection naming blob_ref, got: {[e.message for e in result.errors]}"
        assert "declared by its own options" in declared_errors[0].message

    def test_declared_input_field_from_option_default_satisfied_is_clean(self) -> None:
        """Negative control: the canonical manifest shape guarantees blob_ref."""
        result = self._blob_csv_expand_state(source_fields=["manifest_index: int", "blob_ref: str"]).validate()

        assert result.is_valid, f"Canonical blob manifest must stay valid, got: {[e.message for e in result.errors]}"

    def test_declared_input_probe_closes_every_constructed_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The declared-input probe owns the validation-only transform it constructs."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )

        self._web_scrape_state(url_field="page_url", source_mode="fixed").validate()

        assert tracking.instances, "fixture did not exercise the declared-input probe site"
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)
