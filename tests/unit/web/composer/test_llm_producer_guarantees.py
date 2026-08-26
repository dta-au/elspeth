"""Stage 1 must compute an llm producer's REAL guarantees, not fail-closed zero.

The composer never authors an llm node's ``provider`` block — the operator
profile injects it at lowering (``plugin_policy/profiles.py::lower_options``).
Stage 1's constructor probe ran on the authored options alone, so it failed on
``provider: Field required`` for EVERY composer llm node, and
``_effective_producer_vote``'s known-pass-through fail-closed arm rendered that
permanent condition as "participates with zero guarantees". Every consumer
declaring ``required_input_fields`` downstream of an llm — directly or through
a coalesce that unions the branch votes — was rejected with
``guarantees: [(none)]``, no matter what the author declared
(elspeth-d4ae04b374's blocker; the live session 0b9edd46 shape).

The fix: ``prepare_validation_probe_options`` supplies an inert gateway
provider stub for validation-only construction, so the probe computes the same
provider-independent output contract the lowered runtime build computes.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.llm.config_validation import GATEWAY_SUPPORTED_CAPABILITIES
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationSummary,
)

# ---------------------------------------------------------------------------
# prepare_validation_probe_options unit surface
# ---------------------------------------------------------------------------


class TestProbeProviderStub:
    def test_llm_without_provider_gains_gateway_stub(self) -> None:
        prepared = prepare_validation_probe_options(
            {"prompt_template": "Hi {{ row.x }}", "model": "test-model"},
            plugin="llm",
        )
        assert prepared["provider"] == "gateway"
        assert prepared["endpoint"].startswith("https://")
        assert prepared["api_key"]
        # Derived from the closed vocabulary, so a structured-output node
        # (which demands the json_schema capability) still constructs.
        assert set(prepared["required_capabilities"]) == GATEWAY_SUPPORTED_CAPABILITIES

    def test_llm_with_authored_provider_is_untouched(self) -> None:
        options = {
            "provider": "openrouter",
            "api_key": "authored-key",
            "prompt_template": "Hi {{ row.x }}",
            "model": "openai/gpt-5-mini",
        }
        prepared = prepare_validation_probe_options(options, plugin="llm")
        assert prepared["provider"] == "openrouter"
        assert prepared["api_key"] == "authored-key"
        assert "endpoint" not in prepared
        assert "required_capabilities" not in prepared

    def test_non_llm_plugin_gains_nothing(self) -> None:
        prepared = prepare_validation_probe_options(
            {"mapping": {"a": "b"}, "schema": {"mode": "observed"}},
            plugin="field_mapper",
        )
        assert "provider" not in prepared

    def test_plugin_keyword_is_required(self) -> None:
        """A new probe call site cannot silently opt out of stub injection."""
        with pytest.raises(TypeError):
            prepare_validation_probe_options({"schema": {"mode": "observed"}})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Stage 1 behavioral surface
# ---------------------------------------------------------------------------

_SRC_SCHEMA: dict[str, Any] = {"mode": "observed", "guaranteed_fields": ["colour"]}


def _llm_node(node_id: str, source: str, out: str, response_field: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=source,
        on_success=out,
        on_error="discard",
        options={
            "prompt_template": "For {{ row.colour }} answer.",
            "model": "test-model",
            "response_field": response_field,
            "required_input_fields": ["colour"],
            "schema": {"mode": "observed"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _consumer(node_id: str, source: str, required: list[str], mapping: dict[str, str]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="field_mapper",
        input=source,
        on_success="main",
        on_error="discard",
        options={
            "mapping": mapping,
            "select_only": True,
            "schema": {"mode": "observed"},
            "required_input_fields": required,
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _base_state() -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    return state.with_source(
        SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/data/colours.csv", "schema": _SRC_SCHEMA},
            on_validation_failure="discard",
        )
    )


def _with_output(state: CompositionState) -> CompositionState:
    return state.with_output(
        OutputSpec(
            name="main",
            plugin="csv",
            options={"path": "outputs/colours.csv", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )
    )


def _contract_violations(summary: ValidationSummary) -> list[str]:
    return [entry.message for entry in summary.errors if entry.error_code == "schema_contract_violation"]


def _probe_failed_warnings(summary: ValidationSummary) -> list[str]:
    return [w.message for w in summary.warnings if "Computed contract probe" in w.message]


def _linear_summary(required: list[str]) -> ValidationSummary:
    state = _base_state()
    state = state.with_node(_llm_node("recommend_pairing", "rows", "pairing_done", "complementary_colour"))
    state = state.with_node(
        _consumer(
            "tidy_output",
            "pairing_done",
            required,
            {"colour": "colour", "complementary_colour": "recommended_pairing"},
        )
    )
    return _with_output(state).validate()


def _fork_coalesce_summary(required: list[str]) -> ValidationSummary:
    """The live-session shape: fork -> two llm arms -> union coalesce -> consumer."""
    state = _base_state()
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
            fork_to=("pairing", "hex"),
            branches=None,
            policy=None,
            merge=None,
        )
    )
    state = state.with_node(_llm_node("recommend_pairing", "pairing", "pairing_done", "complementary_colour"))
    state = state.with_node(_llm_node("get_hex", "hex", "hex_done", "hex_code"))
    state = state.with_node(
        NodeSpec(
            id="merge_branches",
            node_type="coalesce",
            plugin=None,
            input="branches",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches={"pairing": "pairing_done", "hex": "hex_done"},
            policy="require_all",
            merge="union",
        )
    )
    state = state.with_node(
        _consumer(
            "tidy_output",
            "merge_branches",
            required,
            {"colour": "colour", "complementary_colour": "recommended_pairing", "hex_code": "hex_code"},
        )
    )
    return _with_output(state).validate()


class TestLlmProducerGuarantees:
    def test_linear_llm_consumer_requiring_response_field_is_accepted(self) -> None:
        summary = _linear_summary(["colour", "complementary_colour"])
        assert _contract_violations(summary) == []
        assert summary.is_valid, [e.to_dict() for e in summary.errors]

    def test_linear_llm_probe_emits_no_probe_failure_warning(self) -> None:
        """The fail-closed arm must not be reached for a well-authored llm node."""
        summary = _linear_summary(["colour", "complementary_colour"])
        assert _probe_failed_warnings(summary) == []

    def test_linear_llm_consumer_requiring_absent_field_is_still_rejected(self) -> None:
        """Discriminator: the fix must not fail open — an honest reject survives."""
        summary = _linear_summary(["colour", "no_such_field"])
        violations = _contract_violations(summary)
        assert violations, "expected a schema_contract_violation for a field no producer emits"
        joined = "\n".join(violations)
        assert "no_such_field" in joined
        # The guarantees list must be the REAL computed set, not the empty
        # fail-closed placeholder the defect produced.
        assert "guarantees: [(none)]" not in joined
        assert "complementary_colour" in joined

    def test_fork_coalesce_llm_pipeline_is_accepted(self) -> None:
        """elspeth-d4ae04b374's live shape validates once llm guarantees are real."""
        summary = _fork_coalesce_summary(["colour", "complementary_colour", "hex_code"])
        assert _contract_violations(summary) == []
        assert summary.is_valid, [e.to_dict() for e in summary.errors]

    def test_fork_coalesce_consumer_requiring_absent_field_is_still_rejected(self) -> None:
        summary = _fork_coalesce_summary(["colour", "no_such_field"])
        violations = _contract_violations(summary)
        assert violations
        assert "no_such_field" in "\n".join(violations)
