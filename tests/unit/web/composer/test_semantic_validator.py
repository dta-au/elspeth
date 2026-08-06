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

        assert len(tracking.instances) >= 3, "fixture did not exercise consumer and producer probes"
        assert [instance.close_count for instance in tracking.instances] == [1] * len(tracking.instances)

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

    The llm TRANSFORM is the producer here, deliberately. The llm SOURCE's
    output_semantics() is unreachable from this validator — the probe can only
    call create_transform and BaseSource has no output_semantics() hook — so a
    source-fed edge is UNKNOWN no matter what the source declares. Using the
    source here would assert the plumbing gap, not this behaviour.
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
        """The REAL g11 topology had a transform between the llm and the sink.

        g11 was `llm source -> content_safety -> text sink`, and the authorable
        repair keeps that middle transform. Pass-through propagation is
        deliberately not performed, so an intervening producer that declares
        nothing breaks the fact chain and the edge is UNKNOWN — no matter what
        the llm upstream of it claims.

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
