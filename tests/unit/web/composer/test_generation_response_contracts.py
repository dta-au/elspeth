import json
import math
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_assistance import PluginAssistanceExample
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools import _generation_responses as responses
from elspeth.web.composer.tools import generation
from tests.unit.web.composer.test_tools import (
    _empty_state,
    _handoff_strict_preflight,
    _mock_catalog,
    _stage1_valid_preview_state,
    _SyncCallRecorder,
    _tolerant_result,
    execute_tool,
)


def collect():
    state = _empty_state()
    catalog = _mock_catalog()
    results = {}
    results["diff_empty"] = execute_tool("diff_pipeline", {}, state, catalog, baseline=state)
    results["diff_sources"] = execute_tool(
        "diff_pipeline",
        {},
        replace(
            state,
            sources={"strange/key": SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard")},
            version=2,
        ),
        catalog,
        baseline=state,
    )
    for name, text in [
        ("explain_exact", "unknown_node_type"),
        ("explain_noisy", "LOG QUARANTINE_UNKNOWN_OUTPUT END"),
        ("explain_none", "ZZZ_UNRECOGNIZED_QQQ"),
        ("explain_hint", "unknown node_type. Expected a gate."),
    ]:
        results[name] = execute_tool("explain_validation_error", {"error_text": text}, state, catalog)
    results["assistance"] = execute_tool(
        "get_plugin_assistance", {"plugin_type": "transform", "plugin_name": "passthrough"}, state, catalog
    )
    plugin_cls = generation.get_shared_plugin_manager().get_transform_by_name("passthrough")
    with patch.object(plugin_cls, "get_agent_assistance", autospec=True, return_value=None):
        results["assistance_none"] = execute_tool(
            "get_plugin_assistance", {"plugin_type": "transform", "plugin_name": "passthrough", "issue_code": "no_help"}, state, catalog
        )
    with (
        patch.object(generation, "read_litellm_model_list", autospec=True, return_value=("plain", "odd/provider", "odd/second")),
        patch.object(generation, "get_catalog_values", autospec=True, return_value=("vendor/model",)),
    ):
        for name, args in [
            ("models_summary", {}),
            ("models_filter", {"provider": "odd/", "limit": 1}),
            ("models_empty_prefix", {"provider": ""}),
            ("models_no_match", {"provider": "missing/"}),
        ]:
            results[name] = execute_tool("list_models", args, state, catalog)
    results["preview_empty"] = execute_tool("preview_pipeline", {}, state, catalog)
    proof_state = replace(
        state,
        sources={"proof": SourceSpec(plugin="csv", on_success="rows", options={"blob_ref": "claimed-id"}, on_validation_failure="discard")},
    )
    diagnostics = generation.compute_proof_diagnostics(
        proof_state, session_id="safe-session", blob_resolver=lambda _id: generation.UnresolvedClaimedProofBlob()
    )
    with patch.object(generation, "compute_proof_diagnostics", autospec=True, return_value=diagnostics):
        results["preview_proof"] = execute_tool("preview_pipeline", {}, proof_state, catalog)
    results["preview_structural"] = execute_tool(
        "preview_pipeline",
        {},
        _stage1_valid_preview_state(),
        catalog,
        runtime_preflight=_handoff_strict_preflight(),
        structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)),
    )
    return results


def _contract(name):
    if name.startswith("diff_"):
        return responses.DIFF_PIPELINE_RESPONSE_CONTRACT
    if name.startswith("explain_"):
        return responses.EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT
    if name.startswith("models_"):
        return responses.LIST_MODELS_RESPONSE_CONTRACT
    if name.startswith("preview_"):
        return responses.PREVIEW_PIPELINE_RESPONSE_CONTRACT
    assert name.startswith("assistance")
    return responses.PLUGIN_ASSISTANCE_RESPONSE_CONTRACT


@pytest.fixture(scope="module")
def producer_results():
    return collect()


def test_real_producers_preserve_frozen_baseline_bytes_and_readmit(producer_results):
    baseline = json.loads(Path(__file__).with_name("fixtures").joinpath("generation_response_wire_a6c58c68.json").read_text())
    assert set(producer_results) == set(baseline["cases"])
    for name, result in producer_results.items():
        assert result.success is baseline["cases"][name]["success"] is True
        contract = _contract(name)
        admitted = contract.admit(result.data)
        assert json.dumps(admitted.to_wire()) == baseline["cases"][name]["data"], name
        assert json.dumps(admitted.readmit(contract).to_wire()) == baseline["cases"][name]["data"], name


def test_selected_contracts_refuse_extra_root_or_missing_required_fields(producer_results):
    for name, result in producer_results.items():
        raw = deep_thaw(result.data)
        for corrupt in ({**raw, "unpublished": True}, {key: value for key, value in raw.items() if key != next(iter(raw))}):
            with pytest.raises(FrameworkBugError):
                _contract(name).admit(corrupt)


@pytest.mark.parametrize(
    "tool_name,case",
    [
        ("diff_pipeline", "diff_sources"),
        ("explain_validation_error", "explain_noisy"),
        ("get_plugin_assistance", "assistance_none"),
        ("list_models", "models_summary"),
        ("preview_pipeline", "preview_structural"),
    ],
)
def test_declaration_selects_producer_contract_before_egress(producer_results, tool_name, case):
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._registry import response_contract_for

    assert response_contract_for(tool_name) is _contract(case)
    result = producer_results[case]
    admitted = admit_discovery_result(tool_name, result)
    assert json.dumps(admitted.to_dict()) == json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "case,path,bad",
    [
        ("diff_empty", ("nodes", "added"), [1]),
        ("diff_empty", ("total_changes",), True),
        ("explain_exact", ("explanation",), 7),
        ("models_summary", ("providers", "odd"), True),
        ("models_filter", ("models",), [False]),
        ("preview_structural", ("structural_preview", "failing_checks"), [{"name": "x"}]),
        ("preview_structural", ("sources", "source", "has_schema_config"), "yes"),
        ("preview_proof", ("proof_diagnostics",), [{"code": "bad"}]),
        ("preview_empty", ("preview_errors",), [{"component": "pipeline", "message": "x", "severity": "high", "error_code": 3}]),
    ],
)
def test_corrupt_nested_producer_data_is_framework_bug(producer_results, case, path, bad):
    raw = deep_thaw(producer_results[case].data)
    cursor = raw
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = bad
    with pytest.raises(FrameworkBugError, match="Malformed generation discovery response"):
        _contract(case).admit(raw)


def test_valid_preview_call_does_not_claim_valid_pipeline(producer_results):
    result = producer_results["preview_empty"]
    assert result.success
    wire = responses.PREVIEW_PIPELINE_RESPONSE_CONTRACT.admit(result.data).to_wire()
    assert wire["preview_is_valid"] is False


def test_proof_evidence_rejects_unknown_nested_field_and_wrong_count(producer_results):
    for key, value in (("unpublished", "hidden"), ("observed_header_count", True), ("blob_id", None)):
        raw = deep_thaw(producer_results["preview_proof"].data)
        raw["proof_diagnostics"][0]["evidence_locator"][key] = value
        with pytest.raises(FrameworkBugError):
            responses.PREVIEW_PIPELINE_RESPONSE_CONTRACT.admit(raw)


def test_failed_diff_stays_validation_only():
    from elspeth.web.composer.discovery_response import admit_discovery_result

    result = execute_tool("diff_pipeline", {}, _empty_state(), _mock_catalog())
    assert not result.success
    assert result.data is None
    admitted = admit_discovery_result("diff_pipeline", result)
    assert "data" not in admitted.to_dict()
    assert any(entry["error_code"] == "diff_baseline_unavailable" for entry in admitted.to_dict()["validation"]["errors"])
    with pytest.raises(FrameworkBugError, match="Failed discovery response has unexpected data"):
        admit_discovery_result("diff_pipeline", replace(result, data={"error_code": "diff_baseline_unavailable"}))


def test_successful_explanation_code_survives(producer_results):
    wire = responses.EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT.admit(producer_results["explain_noisy"].data).to_wire()
    assert wire["error_code"] == "quarantine_unknown_output"


def test_assistance_reuses_owned_examples_and_closes_json_leaves(producer_results):
    raw = deep_thaw(producer_results["assistance"].data)
    raw["examples"] = [PluginAssistanceExample("Example", before={"strange/key": [None, 1, True, {"nested": 2.5}]}, after=None)]
    admitted = responses.PLUGIN_ASSISTANCE_RESPONSE_CONTRACT.admit(raw)
    assert admitted.to_wire()["examples"][0]["before"] == {"strange/key": [None, 1, True, {"nested": 2.5}]}
    for bad in (object(), math.inf, {1: "bad"}):
        malformed = {**raw, "examples": [{"title": "Example", "before": {"value": bad}, "after": None}]}
        with pytest.raises(FrameworkBugError):
            responses.PLUGIN_ASSISTANCE_RESPONSE_CONTRACT.admit(malformed)


class _UnrelatedModel(BaseModel):
    grammar: str = "x"


class _Impostor:
    def to_dict(self):
        return {"error_text": "x", "explanation": "x", "suggested_fix": "x"}


@pytest.mark.parametrize("value", [None, object(), _UnrelatedModel(), _Impostor()])
def test_selected_contracts_reject_unowned_carriers(value):
    for name in ("diff_empty", "explain_exact", "models_summary", "preview_empty", "assistance"):
        with pytest.raises(FrameworkBugError):
            _contract(name).admit(value)


def test_owned_response_revalidation_catches_corruption(producer_results):
    contract = responses.EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT
    owned = contract.parse(producer_results["explain_exact"].data)
    with pytest.raises(ValidationError):
        owned.explanation = "changed"
    corrupt = owned.model_copy(update={"explanation": object()})
    with pytest.raises(FrameworkBugError):
        contract.admit(corrupt)


def test_cached_assistance_rejects_raw_example_replacement(producer_results):
    contract = responses.PLUGIN_ASSISTANCE_RESPONSE_CONTRACT
    raw = deep_thaw(producer_results["assistance"].data)
    example = {"title": "Example", "before": {"value": [1, True, None]}, "after": None}
    raw["examples"] = [example]
    admitted = contract.admit(raw)
    assert admitted.readmit(contract).to_wire() == admitted.to_wire()

    owned = contract.parse(raw)
    corrupt = owned.model_copy(update={"examples": (example,)})
    with pytest.raises(FrameworkBugError):
        contract.admit(corrupt)


def test_revalidation_checks_nested_owned_values_and_nominal_subclasses(producer_results):
    contract = responses.LIST_MODELS_RESPONSE_CONTRACT
    owned = contract.parse(producer_results["models_summary"].data)
    assert type(owned) is responses.ModelProvidersResponse
    owned.providers["odd"] = True
    with pytest.raises(FrameworkBugError):
        contract.admit(owned)

    filtered = contract.parse(producer_results["models_filter"].data)
    assert type(filtered) is responses.ModelListResponse
    corrupt = filtered.model_copy(update={"models": list(filtered.models)})
    with pytest.raises(FrameworkBugError):
        contract.admit(corrupt)

    class ImpostorExplanation(responses.ExplanationResponse):
        pass

    impostor = ImpostorExplanation.model_validate(deep_thaw(producer_results["explain_exact"].data))
    with pytest.raises(FrameworkBugError):
        responses.EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT.admit(impostor)
