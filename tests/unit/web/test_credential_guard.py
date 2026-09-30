"""Web/Composer control-plane credential refusal tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from elspeth.web.composer import provider_gateway
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import (
    CredentialMaterialRefused,
    credential_material_tool_arguments_projection,
    require_no_credential_material,
    require_no_credential_material_for_tool,
    require_no_credential_material_in_state,
    require_no_credential_material_in_tool_wire,
)
from elspeth.web.sessions.routes.workflow.approvals import ApprovalDecisionBody, ApprovalRequestBody
from elspeth.web.sessions.routes.workflow.library import CurateLibraryEntryRequest, PublishLibraryEntryRequest
from elspeth.web.sessions.routes.workflow.reviews import AttestBody, RequestReviewBody
from tests.unit.web.composer._helpers import _make_llm_response, _make_settings


@pytest.mark.parametrize("component_type", ["source", "transform", "sink"])
@pytest.mark.parametrize("plugin", [[], {}], ids=["list", "mapping"])
@pytest.mark.parametrize("credential", [False, True], ids=["ordinary", "credential"])
def test_state_guard_scans_components_before_malformed_plugin_admission(component_type: str, plugin: object, credential: bool) -> None:
    options = {"api_key": "sk-" + "a" * 24} if credential else {}
    component = {"plugin": plugin, "options": options}
    state_data: dict[str, Any] = {
        "sources": {},
        "nodes": [],
        "edges": [],
        "outputs": [],
        "metadata": {"name": "", "description": ""},
        "version": 1,
    }
    if component_type == "source":
        state_data["sources"] = {"source": {"on_success": "output", "on_validation_failure": "discard", **component}}
    elif component_type == "transform":
        state_data["nodes"] = [
            {"id": "transform", "node_type": "transform", "input": "rows", "on_success": "output", "on_error": "discard", **component}
        ]
    else:
        state_data["outputs"] = [{"name": "output", "on_write_failure": "discard", **component}]
    state = CompositionState.from_dict(state_data)

    if credential:
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_in_state(state, surface="test_surface")
    else:
        require_no_credential_material_in_state(state, surface="test_surface")


def test_state_guard_still_scans_database_url_as_a_credential_field() -> None:
    state = CompositionState.from_dict(
        {
            "sources": {},
            "nodes": [],
            "edges": [],
            "outputs": [{"name": "output", "plugin": "database", "options": {"url": "low-entropy-secret"}, "on_write_failure": "discard"}],
            "metadata": {"name": "", "description": ""},
            "version": 1,
        }
    )

    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_state(state, surface="test_surface")


@pytest.mark.parametrize("pii", ["111-22-3333", "4111 1111 1111 1111"])
def test_new_workflow_metadata_guards_do_not_widen_legacy_pii_rejection(pii: str) -> None:
    assert ApprovalRequestBody(approver_identity_id="reviewer", note=pii).note == pii
    assert ApprovalDecisionBody(decision="approved", note=pii).note == pii
    assert RequestReviewBody(state_id="state", note=pii).note == pii
    assert AttestBody(verdict="signed_off", note=pii).note == pii
    assert PublishLibraryEntryRequest(title=pii).title == pii
    assert CurateLibraryEntryRequest(note=pii).note == pii


def test_structured_refusal_contains_only_closed_evidence() -> None:
    candidate = "sk-" + "a" * 24

    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material({"ordinary": candidate}, surface="test_surface")

    payload = caught.value.to_payload()
    assert payload == {
        "error_type": "credential_material_rejected",
        "failure_code": "credential_material_rejected",
        "detail": (
            "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
        ),
        "category": "token_shape",
        "surface": "test_surface",
        "detector_version": "1",
    }
    assert candidate not in repr(caught.value)
    assert candidate not in repr(payload)


def test_structured_refusal_never_returns_a_user_controlled_mapping_key() -> None:
    candidate_key = "sk-" + "b" * 24

    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material({candidate_key: "ordinary"}, surface="test_surface")

    assert candidate_key not in repr(caught.value)
    assert candidate_key not in repr(caught.value.to_payload())


def test_tool_projection_excludes_only_blob_body_content() -> None:
    body_candidate = "sk-" + "a" * 24
    metadata_candidate = "sk-" + "b" * 24
    arguments = {"filename": metadata_candidate, "content": body_candidate, "mime_type": "text/plain"}

    projected = credential_material_tool_arguments_projection("create_blob", arguments)

    assert projected["content"] == "<data-plane-content>"
    assert arguments["content"] == body_candidate
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("create_blob", arguments, surface="test_surface")


def test_blob_body_content_remains_outside_control_plane_admission() -> None:
    candidate = "sk-" + "a" * 24

    require_no_credential_material_for_tool(
        "create_blob",
        {"filename": "input.txt", "content": candidate, "mime_type": "text/plain"},
        surface="test_surface",
    )


def test_wire_secret_ref_scans_the_option_name_without_treating_it_as_a_secret_value() -> None:
    arguments = {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": "api_key"}

    require_no_credential_material_for_tool("wire_secret_ref", arguments, surface="test_surface")

    assert arguments["option_key"] == "api_key"
    assert credential_material_tool_arguments_projection("wire_secret_ref", arguments) == {
        "name": "OPENROUTER_API_KEY",
        "target": "source",
        "structural_option_name": "api_key",
    }


def test_wire_secret_ref_rejects_a_credential_embedded_in_the_structural_option_name() -> None:
    candidate = "sk-" + "a" * 24

    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": candidate},
            surface="test_surface",
        )


def test_set_pipeline_wire_projection_excludes_nested_inline_blob_body_only() -> None:
    body_candidate = "sk-" + "a" * 24
    metadata_candidate = "sk-" + "b" * 24
    arguments = {
        "pipeline": {
            "source": {
                "plugin": "csv",
                "options": {},
                "inline_blob": {"filename": metadata_candidate, "content": body_candidate},
            }
        }
    }

    projected = credential_material_tool_arguments_projection("set_pipeline", arguments)

    assert projected["pipeline"]["source"]["inline_blob"]["content"] == "<data-plane-content>"
    assert arguments["pipeline"]["source"]["inline_blob"]["content"] == body_candidate
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("set_pipeline", arguments, surface="test_surface")


def test_set_pipeline_wire_allows_nested_inline_blob_body_content() -> None:
    candidate = "sk-" + "a" * 24
    raw = '{"pipeline":{"source":{"plugin":"csv","options":{},"inline_blob":{"filename":"input.csv","content":"' + candidate + '"}}}}'

    require_no_credential_material_in_tool_wire("set_pipeline", raw, surface="test_surface")


def test_valid_tool_wire_is_decoded_before_scanning_json_escapes() -> None:
    raw = r'{"note":"sk\u002dproj\u002d' + "a" * 24 + r'"}'

    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_tool_wire("unknown_tool", raw, surface="test_surface")


def test_malformed_tool_wire_is_scanned_before_raw_audit() -> None:
    raw = '{"note":"sk-' + "a" * 24

    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_tool_wire("unknown_tool", raw, surface="test_surface")


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": {"secret_ref": "ghp_" + "a" * 36}},
        {"api_key": {"secret_ref": "a" * 500}},
    ],
)
def test_tool_wire_rejects_credential_or_oversized_reference_contents(arguments: dict[str, Any]) -> None:
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_in_tool_wire(
            "unknown_tool",
            json.dumps(arguments),
            surface="test_surface",
        )

    assert repr(arguments) not in repr(caught.value.to_payload())


@pytest.mark.asyncio
async def test_provider_request_is_refused_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_calls: list[dict[str, Any]] = []

    async def transport(**kwargs: Any) -> object:
        transport_calls.append(kwargs)
        raise AssertionError("credential-bearing request reached transport")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", transport)
    with pytest.raises(CredentialMaterialRefused) as caught:
        await provider_gateway._litellm_acompletion(
            model="openai/test-model",
            messages=[{"role": "user", "content": "sk-" + "a" * 24}],
        )

    assert caught.value.surface == "composer_provider_request"
    assert transport_calls == []


@pytest.mark.asyncio
async def test_serialized_state_credential_is_refused_before_provider_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_calls: list[dict[str, Any]] = []

    async def transport(**kwargs: Any) -> object:
        transport_calls.append(kwargs)
        raise AssertionError("serialized credential reached transport")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", transport)
    with pytest.raises(CredentialMaterialRefused):
        await provider_gateway._litellm_acompletion(
            model="openai/test-model",
            messages=[{"role": "user", "content": '{"password":"low-entropy-secret"}'}],
        )

    assert transport_calls == []


@pytest.mark.asyncio
async def test_provider_response_is_refused_before_release(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "sk-" + "a" * 24

    async def completion(**_kwargs: Any) -> object:
        return _make_llm_response(content=f"provider repeated {candidate}")

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    gateway = ProviderGateway(
        model="openai/test-model",
        settings=_make_settings(composer_model="openai/test-model"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )

    with pytest.raises(CredentialMaterialRefused) as caught:
        await gateway._call_llm([{"role": "user", "content": "ordinary"}], [])

    assert caught.value.surface == "composer_provider_response"
    assert candidate not in repr(caught.value.to_payload())


@pytest.mark.asyncio
async def test_provider_reasoning_is_refused_before_release(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "sk-" + "a" * 24
    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = candidate

    async def completion(**_kwargs: Any) -> object:
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    gateway = ProviderGateway(
        model="openai/test-model",
        settings=_make_settings(composer_model="openai/test-model"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )

    with pytest.raises(CredentialMaterialRefused) as caught:
        await gateway._call_llm([{"role": "user", "content": "ordinary"}], [])

    assert caught.value.surface == "composer_provider_response"
    assert candidate not in repr(caught.value.to_payload())


@pytest.mark.asyncio
async def test_text_provider_reasoning_is_refused_before_release(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "sk-" + "a" * 24
    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = candidate

    async def completion(**_kwargs: Any) -> object:
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    gateway = ProviderGateway(
        model="openai/test-model",
        settings=_make_settings(composer_model="openai/test-model"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )

    with pytest.raises(CredentialMaterialRefused) as caught:
        await gateway._call_text_llm([{"role": "user", "content": "ordinary"}])

    assert caught.value.surface == "composer_provider_response"
    assert candidate not in repr(caught.value.to_payload())


def test_pipeline_data_and_sink_modules_do_not_import_control_plane_guard() -> None:
    """The bounded claim must not silently expand into row/blob mutation."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    excluded_data_plane_modules = (
        repo / "src/elspeth/engine/executors/sink.py",
        repo / "src/elspeth/plugins/infrastructure/clients/llm.py",
        repo / "src/elspeth/web/blobs/service.py",
        repo / "src/elspeth/telemetry/manager.py",
    )
    for module in excluded_data_plane_modules:
        source = module.read_text(encoding="utf-8")
        assert "credential_guard" not in source
        assert "find_credential_material" not in source
