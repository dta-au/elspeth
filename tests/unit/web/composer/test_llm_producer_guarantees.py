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
    def test_unbound_llm_without_profile_or_provider_remains_incomplete(self) -> None:
        prepared = prepare_validation_probe_options(
            {
                "prompt_template": "Hi {{ row.x }}",
                "response_field": "answer",
                "required_input_fields": ["x"],
                "schema": {"mode": "observed"},
            },
            plugin="llm",
        )

        assert "provider" not in prepared
        assert "model" not in prepared

        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        with pytest.raises(ValueError) as excinfo:
            get_shared_plugin_manager().create_transform("llm", prepared)
        assert "provider: Field required" in str(excinfo.value)

    def test_profile_authored_llm_gains_complete_gateway_stub(self) -> None:
        """The public web shape carries only the alias, never provider/model."""
        prepared = prepare_validation_probe_options(
            {
                "profile": "sonnet",
                "prompt_template": "Hi {{ row.x }}",
                "response_field": "answer",
                "required_input_fields": ["x"],
                "schema": {"mode": "observed"},
            },
            plugin="llm",
        )

        assert "profile" not in prepared
        assert prepared["provider"] == "gateway"
        assert prepared["model"] == "validation-probe-model"
        assert set(prepared["required_capabilities"]) == GATEWAY_SUPPORTED_CAPABILITIES

        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        transform = get_shared_plugin_manager().create_transform("llm", prepared)
        assert "answer" in transform.declared_output_fields

    def test_profile_with_private_binding_field_is_not_normalized(self) -> None:
        """Malformed public input stays malformed instead of borrowing the stub."""
        prepared = prepare_validation_probe_options(
            {
                "profile": "sonnet",
                "model": "author-forged-model",
                "prompt_template": "Hi {{ row.x }}",
                "schema": {"mode": "observed"},
            },
            plugin="llm",
        )

        assert prepared["profile"] == "sonnet"
        assert prepared["model"] == "author-forged-model"
        assert "provider" not in prepared

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


def _llm_node(
    node_id: str,
    source: str,
    out: str,
    response_field: str,
    *,
    profile: str | None = "sonnet",
) -> NodeSpec:
    options: dict[str, Any] = {
        "prompt_template": "For {{ row.colour }} answer.",
        "response_field": response_field,
        "required_input_fields": ["colour"],
        "schema": {"mode": "observed"},
    }
    if profile is not None:
        # The real public web schema exposes only an operator profile alias.
        # Provider AND model are private and arrive at lowering.
        options["profile"] = profile
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=source,
        on_success=out,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _consumer(node_id: str, source: str, required: list[str] | None, mapping: dict[str, str]) -> NodeSpec:
    options: dict[str, Any] = {
        "mapping": mapping,
        "select_only": True,
        "schema": {"mode": "observed"},
    }
    if required is not None:
        options["required_input_fields"] = required
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="field_mapper",
        input=source,
        on_success="main",
        on_error="discard",
        options=options,
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


def _linear_summary(required: list[str] | None, *, profile: str | None = "sonnet") -> ValidationSummary:
    state = _base_state()
    state = state.with_node(
        _llm_node(
            "recommend_pairing",
            "rows",
            "pairing_done",
            "complementary_colour",
            profile=profile,
        )
    )
    state = state.with_node(
        _consumer(
            "tidy_output",
            "pairing_done",
            required,
            {"colour": "colour", "complementary_colour": "recommended_pairing"},
        )
    )
    return _with_output(state).validate()


def _fork_coalesce_summary(required: list[str] | None) -> ValidationSummary:
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
    def test_unbound_llm_draft_still_fails_closed(self) -> None:
        """The validation stub is profile lowering, not generic config repair."""
        summary = _linear_summary(["colour", "complementary_colour"], profile=None)

        assert "guarantees: [(none)]" in "\n".join(_contract_violations(summary))
        assert _probe_failed_warnings(summary)

    def test_linear_llm_consumer_requiring_response_field_is_accepted(self) -> None:
        # No hand-authored required_input_fields: the field_mapper mapping is
        # the authority that derives this consumer contract.
        summary = _linear_summary(None)
        assert _contract_violations(summary) == []
        assert summary.is_valid, [e.to_dict() for e in summary.errors]

    def test_linear_llm_probe_emits_no_probe_failure_warning(self) -> None:
        """The fail-closed arm must not be reached for a well-authored llm node."""
        summary = _linear_summary(None)
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
        """The live shape validates using field_mapper's DERIVED requirements."""
        summary = _fork_coalesce_summary(None)
        assert _contract_violations(summary) == []
        assert summary.is_valid, [e.to_dict() for e in summary.errors]

    def test_fork_coalesce_consumer_requiring_absent_field_is_still_rejected(self) -> None:
        summary = _fork_coalesce_summary(["colour", "no_such_field"])
        violations = _contract_violations(summary)
        assert violations
        assert "no_such_field" in "\n".join(violations)
