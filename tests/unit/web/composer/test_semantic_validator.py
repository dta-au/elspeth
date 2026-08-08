"""Tests for validate_semantic_contracts algorithm."""

from __future__ import annotations

import pytest

from elspeth.contracts.plugin_semantics import (
    ContentKind,
    SemanticOutcome,
    TextFraming,
)
from elspeth.web.composer._semantic_validator import validate_semantic_contracts
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)


def _wardline_state(*, text_separator: str = " ", scrape_format: str = "text"):
    """Build the canonical Wardline-shape composition: scrape -> explode -> sink.

    Required to satisfy real config validation:
    - csv source has schema (DataPluginConfig.schema_config is required)
    - web_scrape transform has schema, on_success, on_error
    - line_explode transform has schema, on_success, on_error
    - both json sinks have schema — sinks are semantic consumers too, and a
      schema-less sink fails construction as a "draft config" error, so the
      sink probe abstains and every sink-side assertion goes vacuous
    Without these, plugin construction fails as a "draft config" error,
    the validator's tolerant probe path silently skips, and the test
    becomes vacuous (no error raised, but no contract emitted either).
    """
    return CompositionState(
        metadata=PipelineMetadata(name="wardline"),
        version=1,
        edges=(),
        source=SourceSpec(
            plugin="csv",
            on_success="scrape_in",
            options={
                "path": "data/url.csv",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            on_validation_failure="quarantine",
        ),
        nodes=(
            NodeSpec(
                id="scrape",
                node_type="transform",
                plugin="web_scrape",
                input="scrape_in",
                on_success="explode_in",
                on_error="errors",
                options={
                    "schema": {"mode": "flexible", "fields": ["url: str"]},
                    "required_input_fields": ["url"],
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                    "format": scrape_format,
                    "text_separator": text_separator,
                    "http": {
                        "abuse_contact": "x@example.com",
                        "scraping_reason": "t",
                        "timeout": 5,
                        "allowed_hosts": "public_only",
                    },
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="explode",
                node_type="transform",
                plugin="line_explode",
                input="explode_in",
                on_success="sink",
                on_error="errors",
                options={
                    "schema": {"mode": "flexible", "fields": ["content: str"]},
                    "source_field": "content",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        outputs=(
            OutputSpec(
                name="sink",
                plugin="json",
                options={"path": "out.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
            OutputSpec(
                name="errors",
                plugin="json",
                options={"path": "err.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )


class TestValidateSemanticContracts:
    def test_validation_closes_consumer_and_producer_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )

        validate_semantic_contracts(_wardline_state(text_separator="\n"))

        # EXACT membership, not `>=`. Every transform node is probed as a
        # candidate consumer before its requirements are known, line_explode's
        # producer is probed for facts, and each sink is probed as a consumer
        # too. A `>=` bound would be met by the transform probes alone and
        # would therefore HIDE a leaked, never-closed sink probe — the precise
        # defect this test exists to catch.
        #
        # NO source class belongs in this list — see the failure message.
        assert sorted(type(instance._delegate).__name__ for instance in tracking.instances) == [
            "JSONSink",
            "JSONSink",
            "LineExplode",
            "WebScrapeTransform",
            "WebScrapeTransform",
        ], (
            "fixture must exercise both transform-consumer probes, the producer probe, and BOTH sink probes. "
            "Do NOT add a source class here: this fixture's source feeds web_scrape, and "
            "web_scrape.input_semantic_requirements().fields == (), so `if not requirements.fields: continue` "
            "fires before its producer is ever probed. A source probe is UNREACHABLE from this shape, not "
            "merely absent from it, and adding one would break this test rather than strengthen it. "
            "Source-probe lifecycle needs a source-fed CONSUMER and is covered by "
            "TestSourceProducerFactsAreRead::test_source_producer_probe_is_closed."
        )
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)

    def test_sink_probe_is_closed_even_when_it_leaks_no_requirements(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sink that declares nothing is still constructed, and must still close.

        The early `if not requirements.fields: continue` happens AFTER the probe
        exists, so the close has to be in a finally — an easy leak to introduce
        and an invisible one without a sink-aware tracker.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )

        validate_semantic_contracts(_wardline_state(text_separator="\n"))

        sink_probes = [instance for instance in tracking.instances if type(instance._delegate).__name__ == "JSONSink"]
        assert len(sink_probes) == 2, "both json sinks must be probed"
        assert [probe.close_count for probe in sink_probes] == [1, 1]

    def test_consumer_probe_closes_when_semantic_inspection_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.contracts.plugin_semantics import InputSemanticRequirements

        class _RaisingProbe:
            close_count = 0

            def input_semantic_requirements(self) -> InputSemanticRequirements:
                raise RuntimeError("semantic inspection failed")

            def close(self) -> None:
                self.close_count += 1

        probe = _RaisingProbe()

        class _Manager:
            def create_transform(self, plugin_name: str, options: dict[str, object]) -> _RaisingProbe:
                return probe

        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: _Manager(),
        )
        state = CompositionState(
            metadata=PipelineMetadata(name="probe-close"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="probe_in",
                options={"path": "data.csv", "schema": {"mode": "fixed", "fields": ["value: str"]}},
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="probe",
                    node_type="transform",
                    plugin="value_transform",
                    input="probe_in",
                    on_success="sink",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}, "operations": []},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(),
        )

        with pytest.raises(RuntimeError, match="semantic inspection failed"):
            validate_semantic_contracts(state)

        assert probe.close_count == 1

    def test_compact_text_produces_conflict(self):
        state = _wardline_state(text_separator=" ", scrape_format="text")
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"

        assert len(errors) == 1
        error = errors[0]
        assert error.severity == "high"
        assert "line_explode" in error.message
        assert "web_scrape" in error.message
        assert "content" in error.message
        # Diagnostic must include the requirement_code so a UI / agent
        # can look up plugin-owned assistance.
        assert "line_explode.source_field.line_framed_text" in error.message
        # Generic diagnostic must NOT contain fix-prose tokens — those
        # belong in PluginAssistance, not the validator.
        assert "text_separator" not in error.message
        assert "use markdown" not in error.message.lower()
        assert "set " not in error.message.lower()  # imperative fix-language

        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.from_id == "scrape"
        assert contract.to_id == "explode"
        assert contract.producer_field == "content"
        assert contract.consumer_field == "content"
        assert contract.outcome is SemanticOutcome.CONFLICT
        assert contract.requirement.requirement_code == "line_explode.source_field.line_framed_text"
        assert contract.producer_facts is not None
        assert contract.producer_facts.text_framing is TextFraming.COMPACT

    def test_newline_text_passes(self):
        state = _wardline_state(text_separator="\n", scrape_format="text")
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"
        assert errors == ()
        assert len(contracts) == 1
        assert contracts[0].outcome is SemanticOutcome.SATISFIED
        assert contracts[0].producer_facts.text_framing is TextFraming.NEWLINE_FRAMED

    def test_markdown_passes(self):
        state = _wardline_state(scrape_format="markdown")
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"
        assert errors == ()
        assert contracts[0].outcome is SemanticOutcome.SATISFIED
        assert contracts[0].producer_facts.content_kind is ContentKind.MARKDOWN

    def test_source_fed_consumer_discloses_unknown_without_blocking(self):
        # No source's facts are readable here — the probe can only call
        # create_transform and BaseSource has no output_semantics() hook — so a
        # source-fed edge is always UNKNOWN. Under line_explode's WARN policy
        # (ADR-039) that is DISCLOSED but must not block authoring.
        state = CompositionState(
            metadata=PipelineMetadata(name="t"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="explode_in",
                options={
                    "path": "x.csv",
                    "schema": {"mode": "fixed", "fields": ["content: str"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert errors == (), "A WARN-policy requirement must never block authoring on abstention"
        assert len(warnings) == 1, "The gap must still be DISCLOSED, not silently dropped"
        assert warnings[0].component == "node:explode"
        assert "source" in warnings[0].message
        assert "csv" in warnings[0].message
        assert "declares no semantic facts" in warnings[0].message

        assert len(contracts) == 1
        assert contracts[0].from_id == "source"
        assert contracts[0].to_id == "explode"
        assert contracts[0].producer_plugin == "csv"
        assert contracts[0].producer_facts is None
        assert contracts[0].outcome is SemanticOutcome.UNKNOWN

    def test_named_source_fed_consumer_discloses_unknown_without_blocking(self):
        # Named multi-source roots use stable producer IDs such as
        # source:orders and must receive the same UNKNOWN/WARN treatment.
        state = CompositionState(
            metadata=PipelineMetadata(name="t"),
            version=1,
            edges=(),
            source=None,
            sources={
                "orders": SourceSpec(
                    plugin="csv",
                    on_success="explode_in",
                    options={
                        "path": "orders.csv",
                        "schema": {"mode": "fixed", "fields": ["content: str"]},
                    },
                    on_validation_failure="quarantine",
                )
            },
            nodes=(
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert errors == (), "A WARN-policy requirement must never block authoring on abstention"
        assert len(warnings) == 1, "The gap must still be DISCLOSED, not silently dropped"
        assert warnings[0].component == "node:explode"
        assert "source:orders" in warnings[0].message
        assert "csv" in warnings[0].message
        assert "declares no semantic facts" in warnings[0].message

        assert len(contracts) == 1
        assert contracts[0].from_id == "source:orders"
        assert contracts[0].to_id == "explode"
        assert contracts[0].producer_plugin == "csv"
        assert contracts[0].producer_facts is None
        assert contracts[0].outcome is SemanticOutcome.UNKNOWN

    def test_undeclared_transform_producer_is_disclosed_not_blocking(self):
        # Real registered transform that does NOT declare output_semantics:
        # `passthrough` (src/elspeth/plugins/transforms/passthrough.py:39).
        # passthrough → line_explode is exactly the "pass-through degrades
        # to UNKNOWN" case the design decision documents. Under WARN it is
        # disclosed rather than refused (ADR-039).
        state = CompositionState(
            metadata=PipelineMetadata(name="t"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="pt_in",
                options={
                    "path": "x.csv",
                    "schema": {"mode": "fixed", "fields": ["content: str"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="pt",
                    node_type="transform",
                    plugin="passthrough",
                    input="pt_in",
                    on_success="explode_in",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert errors == (), "A WARN-policy requirement must never block authoring on abstention"

        assert len(contracts) == 1
        assert contracts[0].outcome is SemanticOutcome.UNKNOWN
        assert contracts[0].consumer_plugin == "line_explode"
        assert contracts[0].producer_plugin == "passthrough"

        # WARN policy → UNKNOWN producer is disclosed, not refused.
        assert len(warnings) == 1, "The gap must still be DISCLOSED, not silently dropped"
        assert "no semantic facts" in warnings[0].message.lower() or "undeclared" in warnings[0].message.lower()
        assert warnings[0].component == "node:explode"

    def test_gate_between_producer_and_consumer_is_traversed(self):
        # Gates are STRUCTURAL — plugin=None and condition/routes carry
        # the routing logic. Verified via tests/unit/web/composer/
        # test_yaml_generator.py:72 which uses the same shape.
        state = CompositionState(
            metadata=PipelineMetadata(name="t"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="src_in",
                options={
                    "path": "x.csv",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="scrape",
                    node_type="transform",
                    plugin="web_scrape",
                    input="src_in",
                    on_success="gate_in",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["url: str"]},
                        "required_input_fields": ["url"],
                        "url_field": "url",
                        "content_field": "content",
                        "fingerprint_field": "fingerprint",
                        "format": "markdown",
                        "http": {
                            "abuse_contact": "x@example.com",
                            "scraping_reason": "t",
                            "timeout": 5,
                            "allowed_hosts": "public_only",
                        },
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="g",
                    node_type="gate",
                    plugin=None,
                    input="gate_in",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="row['content']",
                    routes={"yes": "explode_in", "no": "errors"},
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"
        assert errors == ()  # markdown is line-compatible
        assert len(contracts) == 1
        assert contracts[0].outcome is SemanticOutcome.SATISFIED
        assert contracts[0].from_id == "scrape"  # walked through gate
        assert contracts[0].consumer_plugin == "line_explode"
        assert contracts[0].producer_plugin == "web_scrape"


class TestUnknownWarnPolicyIsDisclosedNotBlocking:
    """Positive coverage for the UNKNOWN + WARN branch (elspeth-7a2c9a24c3).

    Every other case in this file pins ``warnings == ()``. Without these, both
    ``else: warnings.append(entry)`` arms could be deleted and the suite would
    stay green — the feature would be dead code from the suite's perspective.
    """

    def _json_source_to_json_explode_state(self) -> CompositionState:
        """The examples/json_explode topology, verbatim.

        This exact pipeline runs green under ``elspeth run`` and was rejected
        by the composer before the WARN change.
        """
        return CompositionState(
            metadata=PipelineMetadata(name="json-explode-green-example"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="json",
                on_success="source_out",
                options={
                    "path": "orders.json",
                    "schema": {"mode": "fixed", "fields": ["order_id: int", "items: any"]},
                },
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="json_explode",
                    input="source_out",
                    on_success="output",
                    on_error="discard",
                    options={
                        "array_field": "items",
                        "output_field": "item",
                        "include_index": True,
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="output",
                    plugin="json",
                    options={"path": "order_items.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_unknown_producer_under_warn_policy_is_disclosed_not_blocking(self) -> None:
        state = self._json_source_to_json_explode_state()
        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == (), "A WARN-policy requirement must never block authoring"
        assert len(warnings) == 1, "The gap must still be DISCLOSED, not silently dropped"
        assert warnings[0].error_code == "semantic_contract_violation"
        assert warnings[0].component == "node:explode"
        assert "json_explode.array_field.list" in warnings[0].message
        assert len(contracts) == 1
        assert contracts[0].outcome is SemanticOutcome.UNKNOWN

    def test_warn_advisory_reaches_the_validation_summary_warnings_channel(self) -> None:
        """The advisory is worthless if it dies inside the validator."""
        state = self._json_source_to_json_explode_state()
        summary = state.validate()

        assert summary.is_valid, "A WARN-policy requirement must not invalidate the composition"
        assert any(entry.error_code == "semantic_contract_violation" for entry in summary.warnings), (
            "The semantic advisory must surface in ValidationSummary.warnings, not be discarded between the validator and the summary."
        )


class TestLLMJsonExplodeRegression:
    def _llm_to_json_explode_state(self) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="llm-json-explode"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="raw_rows",
                options={
                    "path": "data/responses.csv",
                    "schema": {
                        "mode": "fixed",
                        "fields": ["wave: int", "community: str", "response: str"],
                    },
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="classify_barrier",
                    node_type="transform",
                    plugin="llm",
                    input="raw_rows",
                    on_success="classified_rows",
                    on_error="discard",
                    options={
                        "schema": {
                            "mode": "fixed",
                            "fields": ["wave: int", "community: str", "response: str"],
                        },
                        "required_input_fields": ["wave", "community", "response"],
                        "provider": "openrouter",
                        "model": "anthropic/claude-3.7-sonnet",
                        "api_key": "sk-test-key",
                        "prompt_template": "Classify {{ row['response'] }}",
                        "temperature": 0,
                        "response_field": "llm_response",
                        "pool_size": 1,
                        "max_tokens": 300,
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="parse_classification",
                    node_type="transform",
                    plugin="json_explode",
                    input="classified_rows",
                    on_success="parsed_rows",
                    on_error="discard",
                    options={
                        "schema": {"mode": "observed"},
                        "array_field": "llm_response",
                        "output_field": "item",
                        "include_index": False,
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_llm_response_field_cannot_feed_json_explode_array_field(self) -> None:
        """Pin p2_t4_stress: LLM response_field is a string, not a list."""
        state = self._llm_to_json_explode_state()

        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"

        assert len(errors) == 1
        assert errors[0].component == "node:parse_classification"
        assert "json_explode.array_field.list" in errors[0].message
        assert "value_type=str" in errors[0].message
        assert "LLM response_field is str" in errors[0].message

        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.from_id == "classify_barrier"
        assert contract.to_id == "parse_classification"
        assert contract.producer_plugin == "llm"
        assert contract.consumer_plugin == "json_explode"
        assert contract.outcome is SemanticOutcome.CONFLICT
        assert contract.producer_facts is not None
        assert contract.producer_facts.fact_code == "llm.response_field.string"
        assert contract.requirement.requirement_code == "json_explode.array_field.list"

    def test_llm_response_field_to_json_explode_is_blocked_by_full_validate(self) -> None:
        state = self._llm_to_json_explode_state()

        result = state.validate()

        assert result.is_valid is False
        assert any(
            entry.component == "node:parse_classification"
            and "json_explode.array_field.list" in entry.message
            and "value_type=str" in entry.message
            for entry in result.errors
        )


class TestGenerativeProducerFeedsLineExplode:
    """ADR-039 / g11 (elspeth-afdf55a17c, elspeth-b6d9f04827).

    `llm -> line_explode` is the ONLY correct way to write generated multiline
    text to a file: the text sink writes one line per row and diverts any value
    containing CR or LF. Authoring refused that composition, so the request had
    no correct answer at all.

    The llm TRANSFORM is the producer here. Either would now work — the sink
    plumbing gave BaseSource an output_semantics() hook and taught the producer
    probe to use create_source, so LLMSource's declaration is live rather than
    dead code (see TestSourceProducerFactsAreRead). The transform is kept
    because these assertions predate that and pin the transform's own claim.
    """

    def _llm_to_line_explode_state(self) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="generate-and-write"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="raw_rows",
                options={
                    "path": "data/topics.csv",
                    "schema": {"mode": "fixed", "fields": ["topic: str"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="generate",
                    node_type="transform",
                    plugin="llm",
                    input="raw_rows",
                    on_success="explode_in",
                    on_error="discard",
                    options={
                        "schema": {"mode": "fixed", "fields": ["topic: str"]},
                        "required_input_fields": ["topic"],
                        "provider": "openrouter",
                        "model": "anthropic/claude-3.7-sonnet",
                        "api_key": "sk-test-key",
                        "prompt_template": "Write an announcement about {{ row['topic'] }}",
                        "temperature": 0,
                        "response_field": "announcement",
                        "pool_size": 1,
                        "max_tokens": 300,
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="split_lines",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="discard",
                    options={
                        "schema": {"mode": "flexible", "fields": ["announcement: str"]},
                        "source_field": "announcement",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_llm_to_line_explode_is_satisfied_not_merely_advisory(self) -> None:
        state = self._llm_to_line_explode_state()
        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == (), "The only correct spelling of 'write generated text to a file' must not be refused"
        assert warnings == (), (
            "SATISFIED, not tolerated: an advisory here would mean the outcome still rests on unknown_policy "
            "rather than on the producer's declared facts (ADR-039)."
        )

        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.from_id == "generate"
        assert contract.to_id == "split_lines"
        assert contract.producer_plugin == "llm"
        assert contract.consumer_plugin == "line_explode"
        assert contract.outcome is SemanticOutcome.SATISFIED
        assert contract.producer_facts is not None
        assert contract.producer_facts.text_framing is TextFraming.UNCONSTRAINED, (
            "The llm transform must make a POSITIVE claim. UNKNOWN is an abstention, and an abstention can "
            "never CONFLICT — which is what made `llm -> text` ungateable."
        )
        # content_kind stays an honest abstention: prose vs markdown is a real
        # per-response unknown, and claiming either manufactures false conflicts.
        assert contract.producer_facts.content_kind is ContentKind.UNKNOWN

    def test_llm_to_line_explode_is_not_blocked_by_full_validate(self) -> None:
        """The wired surface, not just the validator in isolation."""
        state = self._llm_to_line_explode_state()
        result = state.validate()

        assert not any(
            entry.component == "node:split_lines" and "line_explode.source_field.line_framed_text" in entry.message
            for entry in (*result.errors, *result.warnings)
        ), "llm -> line_explode must raise no semantic entry at all through the wired surface"

    def test_intervening_transform_degrades_to_advisory_not_refusal(self) -> None:
        """An intervening transform breaks the fact chain — the CONSUMER side.

        What this fixture builds is `csv source -> llm TRANSFORM -> passthrough
        -> line_explode`. The spliced ``passthrough`` declares no output
        semantics, so the fact chain stops there: ``line_explode``'s edge names
        ``passthrough`` as its producer and grades UNKNOWN, whatever the llm
        upstream of it claims. The assertions land on ``node:split_lines``, and
        there is deliberately NO sink in this composition.

        g11 itself was `llm SOURCE -> aws_bedrock_content_safety -> text SINK`,
        and that is the motivation for this test rather than the shape it
        pins — the authorable repair keeps the middle transform, so the
        consumer-side behaviour proved here is half of what the report needs.
        The sink-side verdict on the reported topology is pinned separately, by
        ``TestSinkSemanticAcceptanceCriteria``'s
        ``test_llm_through_an_intervening_transform_to_a_text_sink_is_not_blocked``.
        Read the two together; neither covers the other's half.

        This is the honest split, and it is why BOTH changes were needed:
        UNCONSTRAINED (ADR-039 §1) makes only the DIRECT edge factual, while
        FAIL -> WARN (§3c) is what actually unblocks the real shape. Under the
        old FAIL policy this composition was REFUSED, which is what left the
        user's goal with no correct spelling.
        """
        state = self._llm_to_line_explode_state()
        # Splice a real registered transform that declares no output semantics
        # between the generator and the exploder.
        generate = NodeSpec(
            id="generate",
            node_type="transform",
            plugin="llm",
            input="raw_rows",
            on_success="screen_in",
            on_error="discard",
            options=dict(state.nodes[0].options),
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        screen = NodeSpec(
            id="screen",
            node_type="transform",
            plugin="passthrough",
            input="screen_in",
            on_success="explode_in",
            on_error="discard",
            options={"schema": {"mode": "flexible", "fields": ["announcement: str"]}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = state.with_node(generate).with_node(screen)

        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == (), "The real g11 repair must be AUTHORABLE — this is what FAIL -> WARN buys"
        assert len(warnings) == 1, "The broken fact chain must still be disclosed"
        assert warnings[0].component == "node:split_lines"

        assert len(contracts) == 1
        assert contracts[0].producer_plugin == "passthrough"
        assert contracts[0].outcome is SemanticOutcome.UNKNOWN, (
            "Pass-through propagation is deliberately not performed, so the llm's UNCONSTRAINED claim "
            "does not survive an intervening transform. If this ever becomes SATISFIED, propagation "
            "landed and this test should be re-pointed, not deleted."
        )

    def test_unconstrained_text_is_rejected_by_a_compact_only_requirement(self) -> None:
        """The gate a later phase installs on the text sink.

        `TextSink` will require ``accepted_text_framings={COMPACT}``. This pins
        that the llm transform's real declared facts CONFLICT with it — the
        hard, policy-independent block that UNKNOWN could never reach. If this
        ever returns UNKNOWN, the g11 prevention has silently become inert.
        """
        from elspeth.contracts.plugin_semantics import (
            FieldSemanticRequirement,
            UnknownSemanticPolicy,
            compare_semantic,
        )

        state = self._llm_to_line_explode_state()
        _errors, _warnings, contracts = validate_semantic_contracts(state)
        llm_facts = contracts[0].producer_facts
        assert llm_facts is not None

        for policy in UnknownSemanticPolicy:
            compact_only = FieldSemanticRequirement(
                field_name=llm_facts.field_name,
                accepted_content_kinds=frozenset(),
                accepted_text_framings=frozenset({TextFraming.COMPACT}),
                requirement_code="text.field.single_line",
                unknown_policy=policy,
            )
            assert compare_semantic(llm_facts, compact_only) is SemanticOutcome.CONFLICT, (
                f"unknown_policy={policy.value} must not soften the block on llm -> text"
            )


class TestWardlineRegressionPin:
    """Exact options shape from the original Wardline regression YAML.

    Sourced from data/wardline_line_export_pipeline.yaml at the commit
    immediately before text_separator: '\\n' was added. If the new
    semantic-validator surface fails to block this shape, the original
    regression has recurred.

    The brief requires this test exercises ``state.validate()``, not just
    ``validate_semantic_contracts`` directly — a future refactor that
    detaches the validator from the wired surface must fail this test, not
    silently pass.
    """

    def _wardline_broken_yaml_state(self) -> CompositionState:
        # Options copied verbatim from the broken YAML revision.
        return CompositionState(
            metadata=PipelineMetadata(name="wardline-line-export"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="scrape_in",
                options={
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                    "path": "data/blobs/<uuid>/<uuid>_wardline_url.csv",
                    "on_validation_failure": "quarantine",
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="scrape_page",
                    node_type="transform",
                    plugin="web_scrape",
                    input="scrape_in",
                    on_success="explode_in",
                    on_error="errors",
                    options={
                        "schema": {
                            "mode": "flexible",
                            "fields": ["url: str"],
                        },
                        "required_input_fields": ["url"],
                        "url_field": "url",
                        "content_field": "content",
                        "fingerprint_field": "content_fingerprint",
                        "format": "text",
                        # text_separator OMITTED -> defaults to single space.
                        "fingerprint_mode": "content",
                        "strip_elements": ["script", "style"],
                        "http": {
                            "abuse_contact": "pipeline@example.com",
                            "scraping_reason": ("User requested Wardline contents exported as line-oriented JSON"),
                            "timeout": 30,
                            "allowed_hosts": "public_only",
                        },
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="split_lines",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {
                            "mode": "flexible",
                            "fields": ["content: str"],
                        },
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )

    def _wardline_fixed_yaml_state(self) -> CompositionState:
        """Same shape as the broken state but with the Wardline fix applied:
        text_separator: '\\n'. Used to assert the validator accepts the
        post-fix YAML so the test pins both directions of the regression.
        """
        broken = self._wardline_broken_yaml_state()
        # Mutate via with_node so we don't have to repeat the full options dict.
        scrape_options = dict(broken.nodes[0].options)
        scrape_options["text_separator"] = "\n"
        fixed_scrape = NodeSpec(
            id=broken.nodes[0].id,
            node_type=broken.nodes[0].node_type,
            plugin=broken.nodes[0].plugin,
            input=broken.nodes[0].input,
            on_success=broken.nodes[0].on_success,
            on_error=broken.nodes[0].on_error,
            options=scrape_options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        return broken.with_node(fixed_scrape)

    def test_wardline_broken_yaml_blocked_by_semantic_validator(self) -> None:
        state = self._wardline_broken_yaml_state()
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"

        assert len(errors) == 1
        assert errors[0].component == "node:split_lines"

        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.from_id == "scrape_page"
        assert contract.to_id == "split_lines"
        assert contract.outcome is SemanticOutcome.CONFLICT
        assert contract.requirement.requirement_code == "line_explode.source_field.line_framed_text"

    def test_wardline_broken_yaml_blocked_by_full_validate(self) -> None:
        state = self._wardline_broken_yaml_state()
        result = state.validate()
        assert result.is_valid is False
        # The wired surface must surface the structured violation, addressed by
        # plugin/requirement_code — not the legacy text_separator prose.
        assert any(
            entry.component == "node:split_lines"
            and "line_explode" in entry.message
            and "line_explode.source_field.line_framed_text" in entry.message
            for entry in result.errors
        )

    def test_wardline_fixed_yaml_passes_semantic_validator(self) -> None:
        """The post-fix YAML (text_separator='\\n') must NOT be flagged by the
        semantic validator. Pinning both directions of the regression means a
        future change that makes the validator too strict also fails this test
        — not just one that makes it too loose.
        """
        state = self._wardline_fixed_yaml_state()
        errors, warnings, contracts = validate_semantic_contracts(state)
        assert warnings == (), "FAIL-policy requirements must land in errors, never the advisory channel"
        assert errors == ()
        assert len(contracts) == 1
        assert contracts[0].outcome is SemanticOutcome.SATISFIED

    def test_wardline_fixed_yaml_passes_full_validate(self) -> None:
        state = self._wardline_fixed_yaml_state()
        result = state.validate()
        # No semantic-contract entry should appear on split_lines.
        assert not any(
            entry.component == "node:split_lines" and "line_explode.source_field.line_framed_text" in entry.message
            for entry in result.errors
        )


class TestSemanticValidatorSecretLeakage:
    SENTINEL = "PASSWORD_SENTINEL_x9q7r3"

    def test_sentinel_does_not_appear_in_validator_output(self):
        # Build a Wardline state with sentinel in non-field-name options.
        state = CompositionState(
            metadata=PipelineMetadata(name="t"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="scrape_in",
                options={
                    "path": f"data/{self.SENTINEL}/url.csv",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="scrape",
                    node_type="transform",
                    plugin="web_scrape",
                    input="scrape_in",
                    on_success="explode_in",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["url: str"]},
                        "required_input_fields": ["url"],
                        "url_field": "url",
                        "content_field": "content",
                        "fingerprint_field": "fingerprint",
                        "format": "text",
                        "text_separator": " ",
                        "http": {
                            "abuse_contact": f"x+{self.SENTINEL}@example.com",
                            "scraping_reason": f"reason-{self.SENTINEL}",
                            "timeout": 5,
                            "allowed_hosts": "public_only",
                        },
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="line_explode",
                    input="explode_in",
                    on_success="sink",
                    on_error="errors",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "source_field": "content",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": f"out-{self.SENTINEL}.json"},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json"},
                    on_write_failure="discard",
                ),
            ),
        )

        errors, warnings, contracts = validate_semantic_contracts(state)
        # No warnings == () pin here: it would make the sweep below vacuous.
        # This test guards the sentinel across EVERY validator output channel.
        for entry in (*errors, *warnings):
            assert self.SENTINEL not in entry.message, f"Sentinel leaked in validator output: {entry.message!r}"
            assert self.SENTINEL not in entry.component
        for contract in contracts:
            assert self.SENTINEL not in repr(contract)


# ──────────────────────────────────────────────────────────────────────────
# Sinks as semantic consumers (Phase 2b/2c, elspeth-afdf55a17c, ADR-039)
# ──────────────────────────────────────────────────────────────────────────


def _sink_state(
    *,
    producer: NodeSpec,
    sink_plugin: str,
    sink_field: str,
    source_fields: tuple[str, ...] = ("url: str", "topic: str"),
) -> CompositionState:
    """One producer routed straight at one sink, plus a failure sink.

    Every plugin here is fully configured on purpose. A draft config makes the
    probe abstain, and an abstaining probe emits NO sink edge at all — which
    passes any assertion phrased as "no error", so the sink tests below assert
    positively that the edge exists.
    """
    return CompositionState(
        metadata=PipelineMetadata(name="sink-semantics"),
        version=1,
        edges=(),
        source=SourceSpec(
            plugin="csv",
            on_success="producer_in",
            options={"path": "data/rows.csv", "schema": {"mode": "fixed", "fields": list(source_fields)}},
            on_validation_failure="quarantine",
        ),
        nodes=(producer,),
        outputs=(
            OutputSpec(
                name="sink",
                plugin=sink_plugin,
                options={
                    "path": "outputs/out.txt",
                    "field": sink_field,
                    # flexible, not fixed: a locked sink schema rejects the
                    # llm's own emitted model/usage fields as "extras", and a
                    # SCHEMA-contract error would mask the SEMANTIC verdict
                    # these tests exist to assert.
                    "schema": {"mode": "flexible", "fields": [f"{sink_field}: str"]},
                },
                on_write_failure="discard",
            ),
            OutputSpec(
                name="errors",
                plugin="json",
                options={"path": "outputs/err.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )


def _transform_node(**overrides: object) -> NodeSpec:
    fields: dict[str, object] = {
        "id": "produce",
        "node_type": "transform",
        "plugin": None,
        "input": "producer_in",
        "on_success": "sink",
        "on_error": "errors",
        "options": {},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }
    fields.update(overrides)
    return NodeSpec(**fields)  # type: ignore[arg-type]


def _llm_node(**overrides: object) -> NodeSpec:
    return _transform_node(
        plugin="llm",
        options={
            "schema": {"mode": "flexible", "fields": ["topic: str"]},
            "required_input_fields": ["topic"],
            "provider": "openrouter",
            "model": "anthropic/claude-3.7-sonnet",
            "api_key": "sk-test-key",
            "prompt_template": "Write an announcement about {{ row['topic'] }}",
            "temperature": 0,
            "response_field": "announcement",
            "pool_size": 1,
            "max_tokens": 300,
        },
        **overrides,
    )


def _web_scrape_node(*, scrape_format: str, text_separator: str, **overrides: object) -> NodeSpec:
    return _transform_node(
        plugin="web_scrape",
        options={
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": scrape_format,
            "text_separator": text_separator,
            "http": {
                "abuse_contact": "x@example.com",
                "scraping_reason": "t",
                "timeout": 5,
                "allowed_hosts": "public_only",
            },
        },
        **overrides,
    )


def _sink_edges(contracts: tuple, sink_name: str = "sink") -> list:
    return [contract for contract in contracts if contract.to_id == f"output:{sink_name}"]


def _one_sink_edge(contracts: tuple, sink_name: str = "sink"):
    """Return THE sink edge, failing loudly when the sink loop never ran.

    This is the vacuousness guard. Every "not blocked" assertion below passes
    identically whether the sink was evaluated and found acceptable or was
    never evaluated at all, so each one is paired with this.
    """
    edges = _sink_edges(contracts, sink_name)
    assert len(edges) == 1, (
        f"expected exactly one semantic edge into output:{sink_name}, got {len(edges)}. "
        "Zero means the sink loop did not run (or the sink probe abstained on a draft "
        "config) and the surrounding assertions are vacuous."
    )
    return edges[0]


class TestSinkSemanticAcceptanceCriteria:
    """The six outcomes g11 turns on (elspeth-afdf55a17c).

    Before this, `validate_semantic_contracts` never evaluated a sink at all:
    sinks are OutputSpec, never NodeSpec, and NodeType has no sink member, so
    no relaxation of the node loop could reach one. A TextSink requirement was
    declarable and never read.
    """

    def test_llm_to_text_sink_is_a_hard_conflict(self) -> None:
        """The g11 defect itself: generated text into a one-line-per-row sink."""
        state = _sink_state(producer=_llm_node(), sink_plugin="text", sink_field="announcement")
        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.CONFLICT
        assert edge.from_id == "produce"
        assert edge.producer_plugin == "llm"
        assert edge.consumer_plugin == "text"
        assert edge.requirement.requirement_code == "text.field.single_line_text"
        assert edge.producer_facts is not None
        assert edge.producer_facts.text_framing is TextFraming.UNCONSTRAINED

        assert len(errors) == 1, "a CONFLICT must block, not warn"
        assert warnings == ()
        assert errors[0].component == "output:sink", "a sink finding must be attributed to the sink, not to a node"
        assert errors[0].error_code == "semantic_contract_violation"

    def test_llm_to_text_conflict_is_independent_of_unknown_policy(self) -> None:
        """CONFLICT short-circuits ahead of UNKNOWN, so WARN cannot soften it.

        TextSink's policy is WARN precisely so an undeclared producer is not
        blocked. That relaxation must not also unblock a producer that makes a
        positively incompatible claim — which is the whole reason ADR-039 added
        UNCONSTRAINED as a member rather than reusing UNKNOWN.
        """
        state = _sink_state(producer=_llm_node(), sink_plugin="text", sink_field="announcement")
        _errors, _warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.requirement.unknown_policy.value == "warn"
        assert edge.outcome is SemanticOutcome.CONFLICT

    def test_llm_to_document_sink_is_satisfied(self) -> None:
        """The authorable answer, live in the same change that blocks the wrong one."""
        state = _sink_state(producer=_llm_node(), sink_plugin="document", sink_field="announcement")
        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.SATISFIED, (
            "SATISFIED, not merely tolerated: an advisory would leave the outcome resting on "
            "unknown_policy rather than on the producer's declared facts."
        )
        assert edge.consumer_plugin == "document"
        assert edge.requirement.requirement_code == "document.field.verbatim_text"
        assert TextFraming.UNCONSTRAINED in edge.requirement.accepted_text_framings
        assert errors == ()
        assert warnings == ()

    def test_web_scrape_markdown_to_text_sink_is_a_conflict(self) -> None:
        """LINE_COMPATIBLE must NOT be accepted — it is the newline-BEARING member.

        Accepting it alongside COMPACT would bless exactly this composition,
        which is the zero-byte failure the requirement exists to prevent.
        """
        state = _sink_state(
            producer=_web_scrape_node(scrape_format="markdown", text_separator=" "),
            sink_plugin="text",
            sink_field="content",
        )
        errors, _warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.CONFLICT
        assert edge.producer_facts is not None
        assert edge.producer_facts.text_framing is TextFraming.LINE_COMPATIBLE
        assert TextFraming.LINE_COMPATIBLE not in edge.requirement.accepted_text_framings
        assert len(errors) == 1

    def test_web_scrape_newline_framed_to_text_sink_is_a_conflict(self) -> None:
        """The other newline-bearing framing blocks for the same reason."""
        state = _sink_state(
            producer=_web_scrape_node(scrape_format="text", text_separator="\n"),
            sink_plugin="text",
            sink_field="content",
        )
        errors, _warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.CONFLICT
        assert edge.producer_facts is not None
        assert edge.producer_facts.text_framing is TextFraming.NEWLINE_FRAMED
        assert len(errors) == 1

    def test_web_scrape_compact_to_text_sink_is_satisfied(self) -> None:
        """The genuinely correct use of the text sink stays fully clean."""
        state = _sink_state(
            producer=_web_scrape_node(scrape_format="text", text_separator=" "),
            sink_plugin="text",
            sink_field="content",
        )
        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.SATISFIED
        assert edge.producer_facts is not None
        assert edge.producer_facts.text_framing is TextFraming.COMPACT
        assert edge.producer_facts.content_kind is ContentKind.PLAIN_TEXT
        assert errors == ()
        assert warnings == (), "a fully declared, compatible producer must leave no advisory behind"

    def test_undeclared_producer_to_text_sink_is_advisory_not_blocked(self) -> None:
        """An abstaining producer is disclosed, never refused.

        Only a positive conflicting claim blocks. Blocking here would make the
        text sink unwireable from the ordinary single-field extract — the same
        over-blocking that refused `llm -> line_explode` before Phase 1.
        """
        producer = _transform_node(
            id="produce",
            plugin="field_mapper",
            options={"schema": {"mode": "observed"}, "mapping": {"topic": "announcement"}},
        )
        state = _sink_state(producer=producer, sink_plugin="text", sink_field="announcement")
        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.UNKNOWN
        assert edge.producer_facts is None
        assert errors == (), "an undeclared producer must NOT block the text sink"
        assert len(warnings) == 1, "but the gap must be disclosed, not dropped silently"
        assert warnings[0].component == "output:sink"

    def _llm_through_line_explode_state(self, *, sink_field: str) -> CompositionState:
        """`llm -> line_explode -> text`, with the sink's field name as the knob.

        ``output_field`` is set EXPLICITLY rather than left to default, so the
        coupling this test turns on — sink field vs the field line_explode
        actually emits — is visible at both ends instead of hidden in a plugin
        default.
        """
        return CompositionState(
            metadata=PipelineMetadata(name="generate-and-write"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="producer_in",
                options={"path": "data/topics.csv", "schema": {"mode": "fixed", "fields": ["topic: str"]}},
                on_validation_failure="quarantine",
            ),
            nodes=(
                _llm_node(on_success="explode_in"),
                _transform_node(
                    id="split_lines",
                    plugin="line_explode",
                    input="explode_in",
                    options={
                        "schema": {"mode": "flexible", "fields": ["announcement: str"]},
                        "source_field": "announcement",
                        "output_field": "line",
                    },
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="text",
                    options={
                        "path": "outputs/lines.txt",
                        "field": sink_field,
                        "schema": {"mode": "fixed", "fields": [f"{sink_field}: str"]},
                    },
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "outputs/err.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_llm_through_line_explode_to_text_sink_is_not_blocked(self) -> None:
        """End to end: the composition the user actually wants must author clean.

        This is THE win of ADR-039 — the blessed repair for the `llm -> text`
        refusal must itself author clean, or the gate can say "wrong" and never
        "right". `llm -> line_explode` is SATISFIED, and so is the sink edge:
        line_explode declares `text_framing=COMPACT` on its configured
        `output_field`, which is exactly what `text` requires.

        Reads the sink's field as ``"line"`` — the field line_explode actually
        emits. This test previously read ``"line_text"``, which line_explode
        never emits, so no edge was built at all and the assertion recorded the
        UNDECLARED-producer fallback (UNKNOWN + a warning) while its docstring
        blamed a missing declaration. It passed with line_explode's
        `output_semantics()` fully reverted. The mismatch case is worth keeping,
        so it is now its own test below.
        """
        state = self._llm_through_line_explode_state(sink_field="line")
        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == (), "the only correct spelling of the user's goal must author clean"
        assert warnings == (), "a fully declared chain must not warn"

        explode_edge = next(contract for contract in contracts if contract.to_id == "split_lines")
        assert explode_edge.outcome is SemanticOutcome.SATISFIED

        sink_edge = _one_sink_edge(contracts)
        assert sink_edge.outcome is SemanticOutcome.SATISFIED
        assert sink_edge.consumer_plugin == "text"

    def test_sink_reading_a_field_the_producer_never_emits_is_advisory(self) -> None:
        """A field-name mismatch degrades to UNKNOWN, not to a refusal.

        Facts match by EXACT field name, so pointing the sink at a field
        line_explode does not emit leaves the sink's requirement with no
        producer facts to compare against. That is an honest abstention —
        advisory, never a block, because the row may still carry the field from
        somewhere this layer cannot see.
        """
        state = self._llm_through_line_explode_state(sink_field="line_text")
        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == ()
        sink_edge = _one_sink_edge(contracts)
        assert sink_edge.outcome is SemanticOutcome.UNKNOWN
        assert [warning.component for warning in warnings] == ["output:sink"]

    def test_llm_through_an_intervening_transform_to_a_text_sink_is_not_blocked(self) -> None:
        """g11's ACTUAL topology, and the single most important limit of this change.

        The pipeline that failed was `llm source -> aws_bedrock_content_safety
        -> text sink`: a transform sits BETWEEN the generative producer and the
        sink (``passthrough`` stands in for it here — any transform declaring
        no output semantics reproduces the shape).

        Semantic facts are deliberately NOT propagated through an intervening
        transform, so the fact chain stops at that transform: the sink edge
        reports ``passthrough`` as its producer with NO facts, and grades
        UNKNOWN — an advisory. It is emphatically NOT the hard CONFLICT that
        the DIRECT `llm -> text` edge now earns
        (``test_llm_to_text_sink_is_a_hard_conflict``).

        So the change does not, by itself, prevent the exact pipeline that
        produced g11. That is the honest scope and it is deliberate, not an
        oversight. Propagating an upstream producer's claim across a transform
        that has declared nothing would be inventing a fact: the transform is
        free to rewrite the field, and `aws_bedrock_content_safety` genuinely
        might. Blocking on it instead would refuse every composition with an
        undeclared middle transform — the same over-blocking that refused
        `llm -> line_explode` and left the user's goal unauthorable
        (elspeth-b6d9f04827).

        The CONSUMER-side half of this behaviour — the same broken fact chain
        seen by a transform rather than a sink — is pinned by
        ``TestGenerativeProducerFeedsLineExplode``'s
        ``test_intervening_transform_degrades_to_advisory_not_refusal``.

        If propagation ever lands this becomes SATISFIED or CONFLICT; re-point
        this test at the new truth, do not delete it.
        """
        state = _sink_state(
            producer=_llm_node(on_success="screen_in"),
            sink_plugin="text",
            sink_field="announcement",
        )
        screen = _transform_node(
            id="screen",
            plugin="passthrough",
            input="screen_in",
            on_success="sink",
            options={"schema": {"mode": "flexible", "fields": ["announcement: str"]}},
        )
        state = state.with_node(screen)

        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.UNKNOWN, (
            "an intervening transform must degrade the sink edge to an advisory. CONFLICT here would mean "
            "propagation landed silently; SATISFIED would mean a fact was invented for a transform that declared none."
        )
        assert edge.producer_plugin == "passthrough", (
            "the sink's producer is the transform immediately upstream, not the llm behind it — this is the broken fact chain made visible"
        )
        assert edge.producer_facts is None, "passthrough declares nothing, so there are no facts to compare"
        assert edge.consumer_plugin == "text"

        assert errors == (), "g11's real topology must NOT be refused — the block lands only on the direct edge"
        assert [warning.component for warning in warnings] == ["output:sink"], (
            "the gap must still be disclosed on the sink edge, and nothing else may warn"
        )


class TestSinkFanIn:
    """A sink takes MANY producers. The rule is conservative: ANY conflict blocks."""

    def _fan_in_state(self, *, second_scrape_format: str) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="sink-fan-in"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="producer_in",
                options={"path": "data/rows.csv", "schema": {"mode": "fixed", "fields": ["url: str"]}},
                on_validation_failure="quarantine",
            ),
            nodes=(
                _web_scrape_node(id="scrape_a", scrape_format="text", text_separator=" "),
                _web_scrape_node(
                    id="scrape_b",
                    input="fan_in",
                    scrape_format=second_scrape_format,
                    text_separator=" ",
                ),
                _transform_node(
                    id="tee",
                    plugin="field_mapper",
                    input="producer_in",
                    on_success="fan_in",
                    options={"schema": {"mode": "observed"}, "mapping": {"url": "url"}},
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="text",
                    options={
                        "path": "outputs/out.txt",
                        "field": "content",
                        "schema": {"mode": "fixed", "fields": ["content: str"]},
                    },
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "outputs/err.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_every_producer_of_a_sink_is_evaluated(self) -> None:
        """Both arms produce their own edge — the sink is not judged on one arm."""
        state = self._fan_in_state(second_scrape_format="text")
        _errors, _warnings, contracts = validate_semantic_contracts(state)

        edges = _sink_edges(contracts)
        assert sorted(edge.from_id for edge in edges) == ["scrape_a", "scrape_b"], (
            "sink fan-in must evaluate EVERY producer; judging one arm would let a conflicting sibling through"
        )
        assert all(edge.outcome is SemanticOutcome.SATISFIED for edge in edges)

    def test_one_conflicting_arm_blocks_the_whole_sink(self) -> None:
        """Conservative rule: a satisfied sibling does not rescue a conflicting arm."""
        state = self._fan_in_state(second_scrape_format="markdown")
        errors, _warnings, contracts = validate_semantic_contracts(state)

        edges = {edge.from_id: edge.outcome for edge in _sink_edges(contracts)}
        assert edges == {
            "scrape_a": SemanticOutcome.SATISFIED,
            "scrape_b": SemanticOutcome.CONFLICT,
        }
        assert len(errors) == 1
        assert errors[0].component == "output:sink"
        assert "scrape_b" in errors[0].message


class TestSourceProducerFactsAreRead:
    """A source-fed sink reads the SOURCE's facts, not a blanket UNKNOWN.

    BaseSource had no output_semantics() hook and the producer probe knew only
    create_transform, so LLMSource's declaration was unreachable dead code and
    every source-fed edge was UNKNOWN whatever the source claimed.
    """

    def _llm_source_state(self, *, sink_plugin: str) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="llm-source-to-sink"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="llm",
                on_success="sink",
                options={
                    "schema": {"mode": "fixed", "fields": ["announcement: str"]},
                    "provider": "openrouter",
                    "model": "anthropic/claude-3.7-sonnet",
                    "api_key": "sk-test-key",
                    "prompt_template": "Write an announcement.",
                    "response_field": "announcement",
                },
                on_validation_failure="quarantine",
            ),
            nodes=(),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin=sink_plugin,
                    options={
                        "path": "outputs/out.txt",
                        "field": "announcement",
                        "schema": {"mode": "fixed", "fields": ["announcement: str"]},
                    },
                    on_write_failure="discard",
                ),
            ),
        )

    def test_llm_source_to_text_sink_is_a_conflict(self) -> None:
        state = self._llm_source_state(sink_plugin="text")
        errors, _warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.producer_facts is not None, (
            "the source probe must READ the source's declaration; None here means create_source was never reached and the gate is inert"
        )
        assert edge.producer_facts.text_framing is TextFraming.UNCONSTRAINED
        assert edge.outcome is SemanticOutcome.CONFLICT
        assert edge.producer_plugin == "llm"
        assert edge.from_id == "source"
        assert len(errors) == 1

    def test_llm_source_to_document_sink_is_satisfied(self) -> None:
        state = self._llm_source_state(sink_plugin="document")
        errors, warnings, contracts = validate_semantic_contracts(state)

        edge = _one_sink_edge(contracts)
        assert edge.outcome is SemanticOutcome.SATISFIED
        assert errors == ()
        assert warnings == ()

    def _llm_source_to_line_explode_state(self) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="llm-source-to-line-explode"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="llm",
                on_success="split_in",
                options={
                    "schema": {"mode": "fixed", "fields": ["announcement: str"]},
                    "provider": "openrouter",
                    "model": "anthropic/claude-3.7-sonnet",
                    "api_key": "sk-test-key",
                    "prompt_template": "Write an announcement.",
                    "response_field": "announcement",
                },
                on_validation_failure="quarantine",
            ),
            nodes=(
                NodeSpec(
                    id="split_lines",
                    node_type="transform",
                    plugin="line_explode",
                    input="split_in",
                    on_success="sink",
                    on_error="discard",
                    options={
                        "schema": {"mode": "flexible", "fields": ["announcement: str"]},
                        "source_field": "announcement",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json"},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_llm_source_to_line_explode_is_satisfied(self) -> None:
        """The SOURCE arm of the elspeth-b6d9f04827 repair, not just the transform arm.

        ``llm TRANSFORM -> line_explode`` is pinned end-to-end elsewhere, but the
        llm SOURCE feeds the same repair shape through a different probe class
        (``_instantiate_source_producer``), and nothing pinned that arm — a
        regression re-skipping source probes would leave this edge UNKNOWN and
        this test is what would notice.
        """
        state = self._llm_source_to_line_explode_state()
        errors, warnings, contracts = validate_semantic_contracts(state)

        assert errors == ()
        assert warnings == (), "SATISFIED, not tolerated: the source's UNCONSTRAINED claim must decide the edge, not unknown_policy"
        edge = next(contract for contract in contracts if contract.to_id == "split_lines")
        assert edge.from_id == "source"
        assert edge.producer_plugin == "llm"
        assert edge.consumer_plugin == "line_explode"
        assert edge.producer_facts is not None, (
            "the source probe must READ the source's declaration; None here means create_source was never reached and the gate is inert"
        )
        assert edge.producer_facts.text_framing is TextFraming.UNCONSTRAINED
        assert edge.outcome is SemanticOutcome.SATISFIED

    def test_source_producer_probe_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The source probe is validation-only and must not outlive its answer.

        ``_safe_output_semantics`` constructs a real source to read its facts
        and owns the instance, so the close belongs in a ``finally``. That
        close was UNVERIFIED: the source probe is the fourth probe class and
        arrived with this branch, while the existing lifecycle coverage
        (``TestValidateSemanticContracts``) runs on a fixture whose source is
        never probed at all — see the note on its membership assertion. A
        mutation removing this close therefore survived the whole suite.

        A source-fed SINK is the shape that reaches it: the sink loop resolves
        the source as the producer and asks it for facts, so exactly one
        ``LLMSource`` and one ``TextSink`` are constructed.

        The membership assertion alone cannot catch a leak — a leaked instance
        is still recorded. ``close_count`` is the assertion that bites, and
        membership is what stops it going vacuous by keeping the leak visible
        as a named class rather than an empty list.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

        tracking = TrackingPluginManager(get_shared_plugin_manager())
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: tracking,
        )

        validate_semantic_contracts(self._llm_source_state(sink_plugin="text"))

        assert sorted(type(instance._delegate).__name__ for instance in tracking.instances) == [
            "LLMSource",
            "TextSink",
        ], "the fixture must construct a SOURCE probe — without one this test cannot observe the source close at all"

        source_probes = [instance for instance in tracking.instances if type(instance._delegate).__name__ == "LLMSource"]
        assert len(source_probes) == 1
        assert source_probes[0].close_count == 1, (
            "the source probe leaked: _safe_output_semantics owns the instance and must close it in a finally"
        )
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)


class TestSinkProbeToleratesDraftConfig:
    """A half-authored sink must not crash validation, and must not fake a verdict."""

    def test_draft_sink_config_abstains_rather_than_crashing(self) -> None:
        state = _sink_state(producer=_llm_node(), sink_plugin="text", sink_field="announcement")
        draft = state.outputs[0]
        # A text sink with no schema: exactly what an in-progress composition
        # looks like between "add the sink" and "configure it".
        drafted = OutputSpec(
            name=draft.name,
            plugin=draft.plugin,
            options={"path": "outputs/out.txt", "field": "announcement"},
            on_write_failure=draft.on_write_failure,
        )
        state = CompositionState(
            metadata=state.metadata,
            version=state.version,
            edges=state.edges,
            sources=state.sources,
            nodes=state.nodes,
            outputs=(drafted, state.outputs[1]),
        )

        errors, warnings, contracts = validate_semantic_contracts(state)

        assert _sink_edges(contracts) == [], "an unconstructable sink must assert nothing about its edge"
        assert errors == ()
        assert warnings == ()

    def test_unregistered_sink_plugin_abstains_rather_than_crashing(self) -> None:
        state = _sink_state(producer=_llm_node(), sink_plugin="text", sink_field="announcement")
        unknown = OutputSpec(
            name="sink",
            plugin="no_such_sink_plugin",
            options={"path": "outputs/out.txt"},
            on_write_failure="discard",
        )
        state = CompositionState(
            metadata=state.metadata,
            version=state.version,
            edges=state.edges,
            sources=state.sources,
            nodes=state.nodes,
            outputs=(unknown, state.outputs[1]),
        )

        errors, warnings, contracts = validate_semantic_contracts(state)

        assert _sink_edges(contracts) == []
        assert errors == ()
        assert warnings == ()


class TestSinkConflictReachesTheWiredStageOneSurface:
    """The validator in isolation is not the surface the Composer authors on.

    ``CompositionState.validate()`` is what the composer calls, and an error
    existing is not the same claim as validate() refusing — Stage 1 has
    rendered an abstention as ``is_valid: true`` before (elspeth-2ed41f0a4a).
    Assert the refusal itself.
    """

    def test_llm_to_text_is_refused_by_full_validate(self) -> None:
        state = _sink_state(producer=_llm_node(), sink_plugin="text", sink_field="announcement")
        result = state.validate()

        assert result.is_valid is False, "a sink CONFLICT must make the whole composition invalid, not merely emit an entry"
        sink_errors = [
            entry for entry in result.errors if entry.component == "output:sink" and entry.error_code == "semantic_contract_violation"
        ]
        assert len(sink_errors) == 1, "the sink violation must reach the wired surface attributed to the sink"
        assert "text.field.single_line_text" in sink_errors[0].message

    def test_llm_to_document_is_accepted_by_full_validate(self) -> None:
        """The repair must be authorable on the same surface that refuses the defect.

        Order is load-bearing: blocking llm -> text while the alternative is
        unauthorable would refuse the user's goal outright rather than redirect
        it. This asserts the alternative actually validates.
        """
        state = _sink_state(producer=_llm_node(), sink_plugin="document", sink_field="announcement")
        result = state.validate()

        assert result.is_valid is True, f"llm -> document must author clean; errors={[e.message for e in result.errors]}"
        assert not [entry for entry in result.errors if entry.error_code == "semantic_contract_violation"]

    def test_the_document_sink_is_authorized_on_the_composer_surface(self) -> None:
        """A remedy naming an unavailable plugin is not a remedy.

        TextSink's conflict assistance tells the operator to use the document
        sink, so document must be authorized wherever text is — otherwise this
        work would convert "no correct answer" into "a correct answer you are
        not allowed to author".
        """
        from elspeth.web.plugin_policy.compiler import REQUIRED_WEB_PLUGIN_IDS

        authorized = {(plugin.kind, plugin.name) for plugin in REQUIRED_WEB_PLUGIN_IDS}
        assert ("sink", "text") in authorized
        assert ("sink", "document") in authorized, (
            "the text sink's remedy names the document sink; authorizing text without document leaves "
            "generated multiline text with no authorable destination (elspeth-afdf55a17c)"
        )


class TestWebScrapeValueTypeDeclarationSideEffect:
    """Declaring value_type=STR on web_scrape hardens a second edge. Pin it.

    web_scrape abstained on value_type, so ``web_scrape -> json_explode``
    compared UNKNOWN and graded as an advisory. With the honest STR
    declaration it compares STR against {LIST} and CONFLICTs — a NEW hard
    refusal outside the sink acceptance criteria, so it is pinned deliberately
    rather than discovered later.

    The refusal is correct: json_explode expands a real list-shaped value and
    does not parse JSON text, so this composition fails at runtime. And it
    arrives with a remedy — json_explode owns producer-agnostic assistance for
    its requirement_code — so it is not a prohibition with no alternative.
    """

    def _scrape_to_json_explode_state(self) -> CompositionState:
        return CompositionState(
            metadata=PipelineMetadata(name="scrape-to-json-explode"),
            version=1,
            edges=(),
            source=SourceSpec(
                plugin="csv",
                on_success="producer_in",
                options={"path": "data/urls.csv", "schema": {"mode": "fixed", "fields": ["url: str"]}},
                on_validation_failure="quarantine",
            ),
            nodes=(
                _web_scrape_node(scrape_format="markdown", text_separator=" ", on_success="explode_in"),
                _transform_node(
                    id="expand",
                    plugin="json_explode",
                    input="explode_in",
                    options={
                        "schema": {"mode": "flexible", "fields": ["content: str"]},
                        "array_field": "content",
                    },
                ),
            ),
            outputs=(
                OutputSpec(
                    name="sink",
                    plugin="json",
                    options={"path": "out.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="errors",
                    plugin="json",
                    options={"path": "err.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
        )

    def test_a_string_producer_now_conflicts_with_json_explode(self) -> None:
        errors, _warnings, contracts = validate_semantic_contracts(self._scrape_to_json_explode_state())

        edge = next(contract for contract in contracts if contract.to_id == "expand")
        assert edge.producer_facts is not None
        assert edge.producer_facts.value_type.value == "str", "web_scrape must declare the type it provably emits"
        assert edge.outcome is SemanticOutcome.CONFLICT
        assert len(errors) == 1
        assert errors[0].component == "node:expand"

    def test_that_conflict_carries_a_named_remedy(self) -> None:
        """json_explode owns the prose, addressed by requirement_code, so the
        remedy does not depend on which producer tripped it."""
        from elspeth.web.execution._semantic_helpers import assistance_suggestion_for

        errors, _warnings, contracts = validate_semantic_contracts(self._scrape_to_json_explode_state())

        suggestion = assistance_suggestion_for(errors[0], contracts)
        assert suggestion is not None, "a new refusal class must not ship without an alternative"
        assert "list" in suggestion.lower()
