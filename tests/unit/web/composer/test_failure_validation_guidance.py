"""Tests for inline ``validation_guidance`` on failed freeform mutations.

The closed repair catalogue reached only the planner surface: its redacted
rejection feedback resolves each ``error_code`` through
``explain_validation_code`` (``pipeline_planner._allowlisted_candidate_feedback``),
while the freeform tool loop shipped a bare code and left the model to spend a
turn on ``explain_validation_error`` to learn the fix. Live session 891b7b1e
burned exactly that turn on ``no_source_configured`` (elspeth-5ff149dc4e).
The current set-pipeline argument contract rejects a null source at the schema
boundary, so the functional rejection below uses a schema-valid source whose
options fail plugin validation.

``ToolResult.validation_guidance`` closes that gap the same way
``plugin_schemas`` closed the option-shape one: a failed MUTATION carries the
catalogue's ``(explanation, suggested_fix)`` for every resolvable code, plus
the explain-tool pointer when some entry resolved to nothing.
"""

from __future__ import annotations

import json
from typing import Any

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import (
    CompositionState,
    PipelineMetadata,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools import execute_tool as _strict_execute_tool
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import finalize_tool_result
from elspeth.web.composer.tools.generation import (
    EXPLAIN_VALIDATION_ERROR_GUIDANCE,
    build_validation_guidance,
    explain_validation_code,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_tools import _mock_catalog


def _trained_context(view: PolicyCatalogView, snapshot: PluginAvailabilitySnapshot) -> ToolContext:
    return ToolContext(catalog=view, plugin_snapshot=snapshot)


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    catalog: CatalogService,
    **kwargs: Any,
) -> Any:
    """Invoke the strict dispatcher through an explicit test trust boundary."""
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return _strict_execute_tool(
        tool_name,
        arguments,
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _invalid_source_options_set_pipeline(catalog: CatalogService) -> dict[str, Any]:
    """Reach semantic rejection through a schema-valid set-pipeline call."""
    result = execute_tool(
        "set_pipeline",
        {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": 123},
                "on_validation_failure": "discard",
            },
            "nodes": [],
            "edges": [],
            "outputs": [],
        },
        _empty_state(),
        catalog,
    )
    payload: dict[str, Any] = result.to_dict()
    assert result.success is False, payload
    return payload


class TestFreeformRejectionCarriesCataloguedRepairText:
    def test_invalid_source_options_carry_the_catalogued_fix(self) -> None:
        """The production symptom: the rejection now ships its own repair text."""
        payload = _invalid_source_options_set_pipeline(_mock_catalog())

        assert [entry.get("error_code") for entry in payload["validation"]["errors"]] == ["plugin_options_invalid"]
        guidance = payload["validation_guidance"]["codes"]["plugin_options_invalid"]
        explanation, suggested_fix = explain_validation_code("plugin_options_invalid")
        assert guidance == {"explanation": explanation, "suggested_fix": suggested_fix}

    def test_a_fully_enriched_rejection_does_not_advertise_the_explain_tool(self) -> None:
        """Every code resolved, so the call would return byte-equivalent text.

        Advertising it there costs a provider turn to re-read guidance already
        in the context (elspeth-41b406c9fc).
        """
        payload = _invalid_source_options_set_pipeline(_mock_catalog())

        assert "explain_tool" not in payload["validation_guidance"]

    def test_a_successful_mutation_carries_no_guidance_key(self) -> None:
        catalog = _mock_catalog()
        result = execute_tool(
            "set_metadata",
            {"patch": {"name": "demo"}},
            _empty_state(),
            catalog,
        )
        payload = result.to_dict()
        assert result.success is True, payload
        assert "validation_guidance" not in payload


class TestValidationGuidanceBuilder:
    def test_unresolved_code_earns_the_explain_tool_pointer(self) -> None:
        """An unresolved code is the one case where the call can still add something.

        The tool also matches on the full validator message, which the freeform
        envelope carries and the code lookup cannot use.
        """
        guidance = build_validation_guidance(["no_source_configured", "code_the_catalogue_does_not_know"])

        assert guidance is not None
        assert set(guidance["codes"]) == {"no_source_configured"}
        assert guidance["explain_tool"] == EXPLAIN_VALIDATION_ERROR_GUIDANCE

    def test_entries_without_a_code_earn_the_pointer(self) -> None:
        guidance = build_validation_guidance([None])

        assert guidance is not None
        assert guidance["codes"] == {}
        assert guidance["explain_tool"] == EXPLAIN_VALIDATION_ERROR_GUIDANCE

    def test_no_entries_yields_no_payload(self) -> None:
        assert build_validation_guidance([]) is None

    def test_repeated_code_costs_the_text_once(self) -> None:
        guidance = build_validation_guidance(["no_sinks_configured", "no_sinks_configured"])

        assert guidance is not None
        assert list(guidance["codes"]) == ["no_sinks_configured"]

    def test_every_value_is_verbatim_catalogue_text(self) -> None:
        """The field carries STATIC catalogue text and nothing else.

        ``_augment_with_expected_hint`` is the live temptation, since the
        explain TOOL applies it. It must not be applied here: the mapping is
        keyed BY error_code, so one entry serves every error sharing that
        code, and a spliced per-entry message span would make N colliding
        entries whose text depended on visit order. It also keeps these bytes
        identical to the guided surface's.

        Custody is NOT the reason — the field is ``_SafeResponseEnvelope`` in
        the redaction manifest and collapses to ``<redacted-response-mapping>``
        on persist, just as ``message`` does.
        """
        codes = [
            "no_source_configured",
            "no_sinks_configured",
            "unknown_node_type",
            "duplicate_node_id",
            "reserved_node_id",
        ]
        guidance = build_validation_guidance(codes)

        assert guidance is not None
        for code in codes:
            explanation, suggested_fix = explain_validation_code(code)
            assert guidance["codes"][code] == {"explanation": explanation, "suggested_fix": suggested_fix}


class TestValidationGuidanceCustody:
    def test_provider_sees_the_text_but_persistence_sees_a_placeholder(self) -> None:
        """Declared ``_SafeResponseEnvelope``, so the audit projection collapses it.

        The provider payload (``ToolResult.to_dict``) carries the full
        catalogue text — that is the whole point of the field. The audit
        projection that reaches ``chat_messages.tool_calls`` must not: it
        collapses to a value-free placeholder, the same treatment
        ``validation.errors[].message`` gets. If a later manifest edit drops
        the ``_SafeResponseEnvelope`` declaration this fails, and the
        redaction snapshot's ``sensitive_path_count`` drops with it.
        """
        payload = _invalid_source_options_set_pipeline(_mock_catalog())
        assert "plugin schema" in json.dumps(payload["validation_guidance"]).lower()

        persisted = redact_tool_call_response("set_pipeline", payload, telemetry=NoopRedactionTelemetry())

        assert persisted["validation_guidance"] == "<redacted-response-mapping>"
        assert persisted["validation"]["errors"][0]["message"] == "<redacted-response-text>"


class TestValidationGuidanceSeesTheNormalizedErrorSet:
    def test_state_errors_added_by_normalization_are_covered(self) -> None:
        """Guidance is derived AFTER ``normalize_tool_result_validation``.

        That function REPLACES ``validation`` with the revalidated state's
        entries concatenated onto the rejections, so a handler envelope that
        arrives carrying only its rejection gains the state's codes there.
        Deriving guidance before it runs covers the rejection alone and
        silently omits every code normalization introduces — this test builds
        exactly that envelope and fails if the order is swapped.
        """
        catalog = _mock_catalog()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
        state = _empty_state()

        # A handler-shaped envelope: one rejection whose code the catalogue
        # does NOT resolve, and no state entries yet. `_state_validation_withheld`
        # stays False, so normalization revalidates and appends the empty
        # state's own no_source_configured / no_sinks_configured entries.
        handler_result = ToolResult(
            success=False,
            updated_state=state,
            validation=ValidationSummary(
                is_valid=False,
                errors=(
                    ValidationEntry(
                        component="rejected_mutation",
                        message="synthetic rejection",
                        severity="high",
                        error_code="code_the_catalogue_does_not_know",
                    ),
                ),
            ),
            affected_nodes=(),
        )
        assert build_validation_guidance(entry.error_code for entry in handler_result.validation.errors) == {
            "codes": {},
            "explain_tool": EXPLAIN_VALIDATION_ERROR_GUIDANCE,
        }, "precondition: the pre-normalization set resolves to nothing"

        finalized = finalize_tool_result(
            handler_result,
            tool_name="set_source",
            catalog=view,
            context=_trained_context(view, snapshot),
            prior_validation=state.validate(),
        )

        codes = {entry.error_code for entry in finalized.validation.errors if entry.error_code}
        assert "no_source_configured" in codes, codes
        assert finalized.validation_guidance is not None
        covered = set(finalized.validation_guidance["codes"])
        assert {code for code in codes if explain_validation_code(code) is not None} == covered
        assert "no_source_configured" in covered


class TestValidationGuidanceIsScopedToMutations:
    def test_a_failed_discovery_tool_carries_no_guidance_key(self) -> None:
        """Discovery envelopes answer to their own ``extra="forbid"`` response models.

        ``get_blob_content`` is bound to ``GetBlobContentResponseModel``, which
        does not admit the key; an unscoped augmentation would fail its
        response path closed rather than help anyone.
        """
        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "source", "name": "definitely_not_a_plugin"},
            _empty_state(),
            _mock_catalog(),
        )
        payload = result.to_dict()
        assert result.success is False, payload
        assert "validation_guidance" not in payload
