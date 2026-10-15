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


def _field_mapper_node(mapping: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": mapping, "schema": {"mode": "observed"}},
    }


def _field_mapper_state() -> CompositionState:
    return CompositionState.from_dict(
        {
            "sources": {},
            "nodes": [_field_mapper_node({"customer_key": "customer_id"})],
            "edges": [],
            "outputs": [],
            "metadata": {"name": "synthetic", "description": ""},
            "version": 1,
        }
    )


@pytest.mark.parametrize("surface", ("wire", "semantic", "state"))
def test_owned_field_mapper_labels_are_not_credential_slots(surface: str) -> None:
    from elspeth.plugins.transforms.field_mapper import FieldMapperConfig

    node = _field_mapper_node({"customer_key": "customer_id"})
    assert FieldMapperConfig.model_validate(node["options"]).declared_input_fields == frozenset({"customer_key"})
    if surface == "wire":
        require_no_credential_material_in_tool_wire("upsert_node", json.dumps(node), surface="test")
    elif surface == "semantic":
        require_no_credential_material_for_tool("upsert_node", node, surface="test")
    else:
        require_no_credential_material_in_state(_field_mapper_state(), surface="test")


def test_owned_field_mapper_patch_uses_actual_state_identity() -> None:
    patch = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "customer_id"}}}
    require_no_credential_material_in_tool_wire(
        "patch_node_options", json.dumps(patch), surface="test", credential_state=_field_mapper_state()
    )
    require_no_credential_material_for_tool("patch_node_options", patch, surface="test", credential_state=_field_mapper_state())


@pytest.mark.parametrize("status", ("pending", "resolved"))
@pytest.mark.parametrize("surface", ("wire", "semantic", "state", "patch"))
def test_field_mapper_authoring_metadata_preserves_label_domain(status: str, surface: str) -> None:
    import hashlib

    from elspeth.web.interpretation_state import parse_interpretation_requirements

    draft = "Rename the customer identifier."
    node = _field_mapper_node({"customer_key": "customer_id"})
    node["options"]["interpretation_requirements"] = [
        {
            "id": "rename-review",
            "kind": "pipeline_decision",
            "user_term": "customer identifier",
            "status": status,
            "draft": draft,
            "event_id": "review-event" if status == "resolved" else None,
            "accepted_value": draft if status == "resolved" else None,
            "accepted_artifact_hash": hashlib.sha256(draft.encode()).hexdigest() if status == "resolved" else None,
        }
    ]
    assert parse_interpretation_requirements(node["options"])[0]["status"] == status
    data = _field_mapper_state().to_dict()
    data["nodes"] = [node]
    state = CompositionState.from_dict(data)
    if surface == "wire":
        require_no_credential_material_in_tool_wire("upsert_node", json.dumps(node), surface="test")
    elif surface == "semantic":
        require_no_credential_material_for_tool("upsert_node", node, surface="test")
    elif surface == "state":
        require_no_credential_material_in_state(state, surface="test")
    else:
        require_no_credential_material_for_tool(
            "patch_node_options",
            {"node_id": node["id"], "patch": {"mapping": {"customer_key": "customer_number"}}},
            surface="test",
            credential_state=state,
        )


@pytest.mark.parametrize("metadata", ({"api_key": "low-entropy-secret"}, {"draft": "sk-" + "a" * 24}))
def test_field_mapper_authoring_metadata_is_still_credential_scanned(metadata: dict[str, str]) -> None:
    node = _field_mapper_node({"customer_key": "customer_id"})
    node["options"]["interpretation_requirements"] = [metadata]
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_tool_wire("upsert_node", json.dumps(node), surface="test")
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("upsert_node", node, surface="test")


@pytest.mark.parametrize("tool", ("set_pipeline", "emit_pipeline_proposal", "splice_transform"))
def test_owned_field_mapper_labels_cover_identity_bearing_tool_domains(tool: str) -> None:
    node = _field_mapper_node({"customer_key": "customer_id"})
    arguments = (
        {"predecessor_id": "source", "successor_id": "output", "node": {"plugin": node["plugin"], "options": node["options"]}}
        if tool == "splice_transform"
        else {"pipeline": {"nodes": [node]}}
    )
    require_no_credential_material_in_tool_wire(tool, json.dumps(arguments), surface="test")
    require_no_credential_material_for_tool(tool, arguments, surface="test")


@pytest.mark.parametrize(
    "change", ("unknown_plugin", "unknown_tool", "wrong_kind", "extra_option", "nested_value", "token", "private_key", "url")
)
def test_field_mapper_domain_does_not_allow_credentials_or_unknown_shapes(change: str) -> None:
    node = _field_mapper_node({"customer_key": "customer_id"})
    tool = "upsert_node"
    if change == "unknown_plugin":
        node["plugin"] = "unknown_plugin"
    elif change == "unknown_tool":
        tool = "unknown_tool"
    elif change == "wrong_kind":
        node["node_type"] = "gate"
    elif change == "extra_option":
        node["options"]["password"] = "low-entropy-secret"
    elif change == "nested_value":
        node["options"]["mapping"]["customer_key"] = {"unknown": "NotAField"}
    else:
        node["options"]["mapping"]["customer_key"] = {
            "token": "sk-" + "a" * 24,
            "private_key": "-----BEGIN PRIVATE KEY-----",
            "url": "https://synthetic:password@example.test",
        }[change]
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool(tool, node, surface="test")


@pytest.mark.parametrize("change", ("missing_context", "missing_node", "spoofed_plugin", "invalid_merged_schema"))
def test_field_mapper_patch_refuses_ambiguous_or_invalid_identity(change: str) -> None:
    state = _field_mapper_state()
    patch = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "customer_id"}}}
    if change == "missing_context":
        state = None
    elif change == "missing_node":
        patch["node_id"] = "new_node_not_in_snapshot"
    elif change == "spoofed_plugin":
        data = state.to_dict()
        data["nodes"][0]["plugin"] = "llm"
        state = CompositionState.from_dict(data)
        patch["plugin"] = "field_mapper"
    else:
        patch["patch"]["schema"] = None
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_tool_wire("patch_node_options", json.dumps(patch), surface="test", credential_state=state)


def test_field_mapper_projection_is_bounded_before_config_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.field_mapper import FieldMapperConfig

    def forbidden_validation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unbounded options reached plugin validation")

    monkeypatch.setattr(FieldMapperConfig, "model_validate", forbidden_validation)
    for mapping in ({f"customer_{index}_key": "field" for index in range(513)}, {"customer_key": "x" * 262_145}):
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_for_tool("upsert_node", _field_mapper_node(mapping), surface="test")


@pytest.mark.asyncio
async def test_gateway_patch_context_is_not_sent_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "customer_id"}}}
    requests = []

    async def completion(**kwargs: Any) -> object:
        requests.append(kwargs)
        return _make_llm_response(tool_calls=[{"id": "patch-call", "name": "patch_node_options", "arguments": patch}])

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    gateway = ProviderGateway(model="openai/test-model", settings=_make_settings(), endpoint_base_url=None, endpoint_api_key=None)
    result = await gateway._call_llm([{"role": "user", "content": "synthetic"}], [], credential_state=_field_mapper_state())
    assert result.tool_batch.calls[0].function.arguments == json.dumps(patch)
    assert len(requests) == 1
    assert "credential_state" not in requests[0]


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
