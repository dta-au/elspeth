"""Web/Composer control-plane credential refusal tests."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from dataclasses import replace
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.credential_material import CredentialTraversalPolicy
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import credential_guard as guard
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.state import CompositionState
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.credential_guard import (
    CredentialMaterialRefused,
    credential_material_tool_arguments_projection,
    require_no_credential_material,
    require_no_credential_material_for_tool,
    require_no_credential_material_in_state,
    require_no_credential_material_in_tool_wire,
)
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.routes.workflow.approvals import ApprovalDecisionBody, ApprovalRequestBody
from elspeth.web.sessions.routes.workflow.library import CurateLibraryEntryRequest, PublishLibraryEntryRequest
from elspeth.web.sessions.routes.workflow.reviews import AttestBody, RequestReviewBody
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.session_fences import ensure_session_fence
from tests.unit.web.composer._helpers import _composer_service_with_session, _make_llm_response, _make_settings, _mock_catalog


@pytest.fixture
def provider_custody() -> Iterator[ProviderCallCustody]:
    """Reach credential admission with real fenced custody and no SDK dispatch."""
    composer, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
    assert composer._sessions_service is not None
    sessions = cast(SessionServiceImpl, composer._sessions_service)
    assert ensure_session_fence(sessions._engine, session_id, owner_instance_id="credential-guard-test")
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=UUID(session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="credential-guard-test",
        lease_seconds=60,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    custody = ProviderInvocationOwner(service=sessions, required_work=binding).mint(ProviderInvocationFamily.PRIMARY)
    try:
        yield custody
        assert custody.attempt is None
        assert custody.call is None
        coordinator.assert_completed()
    finally:
        authority.release(context)
        sessions._engine.dispose()


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
        else {"nodes": [node]}
        if tool == "set_pipeline"
        else {"pipeline": {"nodes": [node]}}
    )
    wire = {"pipeline": arguments} if tool == "set_pipeline" else arguments
    if tool == "emit_pipeline_proposal":
        # A retired tool no longer owns a credential-detector exception.
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_in_tool_wire(tool, json.dumps(wire), surface="test")
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_for_tool(tool, arguments, surface="test")
        return
    require_no_credential_material_in_tool_wire(tool, json.dumps(wire), surface="test")
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
async def test_provider_request_is_refused_before_transport(monkeypatch: pytest.MonkeyPatch, provider_custody: ProviderCallCustody) -> None:
    transport_calls: list[dict[str, Any]] = []

    async def transport(**kwargs: Any) -> object:
        transport_calls.append(kwargs)
        raise AssertionError("credential-bearing request reached transport")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", transport)
    with pytest.raises(CredentialMaterialRefused) as caught:
        await provider_gateway._litellm_acompletion(
            provider_custody=provider_custody,
            model="openai/test-model",
            prompt_guard_mount=None,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            messages=[{"role": "user", "content": "sk-" + "a" * 24}],
        )

    assert caught.value.surface == "composer_provider_request"
    assert transport_calls == []


@pytest.mark.asyncio
async def test_serialized_state_credential_is_refused_before_provider_transport(
    monkeypatch: pytest.MonkeyPatch, provider_custody: ProviderCallCustody
) -> None:
    transport_calls: list[dict[str, Any]] = []

    async def transport(**kwargs: Any) -> object:
        transport_calls.append(kwargs)
        raise AssertionError("serialized credential reached transport")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", transport)
    with pytest.raises(CredentialMaterialRefused):
        await provider_gateway._litellm_acompletion(
            provider_custody=provider_custody,
            model="openai/test-model",
            prompt_guard_mount=None,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
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


def test_retention_generic_scan_reuses_exact_inputs_and_rechecks_mutation(monkeypatch):
    original = guard.find_credential_material
    work = []

    def detect(value, policy):
        work.append((value, policy))
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)
    value = {"ordinary": [None, True, False, 7, 1.25, {"last": "safe"}]}
    with retention_stage_memo_scope() as memo:
        require_no_credential_material(value, surface="memo-control")
        require_no_credential_material(value, surface="memo-control")
        assert len(work) == 1 and memo.cached_entry_count == 1
        value["ordinary"][-1]["last"] = "changed"
        require_no_credential_material(value, surface="memo-control")
        assert len(work) == 2
    require_no_credential_material(value, surface="memo-control")
    require_no_credential_material(value, surface="memo-control")
    assert len(work) == 4 and memo.cached_entry_count == 0


def test_retention_scan_snapshots_mutable_policy_and_raises_fresh_refusals(monkeypatch):
    original = guard.find_credential_material
    work = []

    def detect(value, policy):
        work.append(policy)
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)
    names = set()
    value = {"api_key": "${CURRENT_KEY}"}
    refusals = []
    with retention_stage_memo_scope():
        for _ in range(2):
            with pytest.raises(CredentialMaterialRefused) as caught:
                require_no_credential_material(value, surface="memo-control", env_ref_names=names)
            refusals.append(caught.value)
        assert refusals[0] is not refusals[1] and refusals[0].finding is refusals[1].finding
        assert len(work) == 1
        names.add("CURRENT_KEY")
        require_no_credential_material(value, surface="memo-control", env_ref_names=names)
        assert len(work) == 2 and work[0].env_ref_names == frozenset()
        names.clear()
        with pytest.raises(CredentialMaterialRefused) as caught:
            require_no_credential_material(value, surface="memo-control", env_ref_names=names)
        assert caught.value is not refusals[0] and caught.value.finding is refusals[0].finding
        assert len(work) == 2


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("env_ref_names", {"CURRENT_KEY"}),
        ("additional_credential_fields", {"ordinary"}),
        ("max_depth", 1),
        ("max_nodes", 2),
        ("max_width", 1),
        ("max_string_bytes", 16),
    ],
)
def test_retention_scan_key_changes_execute_real_detector_for_each_policy_input(monkeypatch, field, changed):
    original = guard.find_credential_material
    value = {"ordinary": "safe"}
    policy = CredentialTraversalPolicy()
    other = replace(policy, **{field: changed})
    expected = original(value, other)
    work = []

    def detect(value, policy):
        work.append(policy)
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)
    with retention_stage_memo_scope():
        for current in (policy, policy, other, other):
            result = guard._retention_credential_finding(value, policy=current, surface="memo-control", scanner_role="generic")
        assert result == expected and len(work) == 2


def test_retention_scan_key_captures_the_current_detector_binding(monkeypatch):
    original = guard.find_credential_material
    work = []

    def one(value, policy):
        work.append("one")
        return original(value, policy)

    def two(value, policy):
        work.append("two")
        return original(value, policy)

    with retention_stage_memo_scope():
        for detector in (one, two):
            monkeypatch.setattr(guard, "find_credential_material", detector)
            for _ in range(2):
                require_no_credential_material({"ordinary": "safe"}, surface="memo-control")
    assert work == ["one", "two"]


def test_retention_scan_descriptor_preserves_order_types_and_container_boundaries():
    value = {"first": (True, 1, 1.0, -0.0), "second": [None, '{"json":true}']}
    policy = CredentialTraversalPolicy()
    key = guard._credential_scan_key(value, policy=policy, surface="memo-control", scanner_role="generic")
    assert key is not None
    restored = guard._credential_input_from_tokens(key.content)
    assert restored == value and list(restored) == ["first", "second"]
    assert type(restored["first"]) is tuple and type(restored["second"]) is list
    assert tuple(type(item) for item in restored["first"]) == (bool, int, float, float)
    assert restored["first"][-1].hex() == "-0x0.0p+0"
    reordered = guard._credential_scan_key(
        dict(reversed(list(value.items()))), policy=policy, surface="memo-control", scanner_role="generic"
    )
    assert reordered != key


def test_retention_proxy_fallback_keeps_live_backing_and_never_preaccesses_a_later_proxy(monkeypatch):
    original = guard.find_credential_material
    received = []
    accesses = []

    def detect(value, policy):
        received.append(value)
        return original(value, policy)

    class ForeignBacking(dict):
        def __len__(self):
            accesses.append("len")
            raise AssertionError("later proxy length must not be accessed")

        def items(self):
            accesses.append("items")
            raise AssertionError("later proxy items must not be accessed")

    monkeypatch.setattr(guard, "find_credential_material", detect)
    backing = {"ordinary": "safe"}
    proxy = MappingProxyType(backing)
    later = MappingProxyType(ForeignBacking())
    with retention_stage_memo_scope() as memo:
        require_no_credential_material(proxy, surface="memo-control")
        backing["api_key"] = "literal"
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material(proxy, surface="memo-control")
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material({"api_key": "literal", "later": later}, surface="memo-control")
        assert memo.cached_entry_count == 0
    assert received[0] is received[1] is proxy and accesses == []


@pytest.mark.parametrize("field", ["env_ref_names", "additional_credential_fields"])
def test_retention_foreign_policy_collection_falls_back_without_new_access(field):
    class ForeignCollection(Collection[str]):
        def __iter__(self):
            raise AssertionError("ordinary scalar scan never iterates this collection")

        def __len__(self):
            raise AssertionError("ordinary scalar scan never sizes this collection")

        def __contains__(self, value):
            raise AssertionError("ordinary scalar scan never searches this collection")

    with retention_stage_memo_scope() as memo:
        require_no_credential_material("ordinary content", surface="memo-control", **{field: ForeignCollection()})
        assert memo.cached_entry_count == 0


def test_retention_unhashable_surface_preserves_the_original_credential_refusal():
    surface = []
    with retention_stage_memo_scope() as memo:
        with pytest.raises(CredentialMaterialRefused) as caught:
            require_no_credential_material({"api_key": "literal"}, surface=surface)
        assert caught.value.surface is surface and memo.cached_entry_count == 0


def test_retention_unsupported_or_over_bound_tail_keeps_the_earlier_refusal(monkeypatch):
    original = guard.find_credential_material
    work = []
    cyclic = []
    cyclic.append(cyclic)

    class ForeignValue:
        def __repr__(self):
            raise AssertionError("foreign tail must not be rendered")

    def detect(value, policy):
        work.append(value)
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)
    tails = [ForeignValue(), 1 << 100_000, "x" * 262_145, [None] * 513, cyclic, float("nan")]
    with retention_stage_memo_scope() as memo:
        for tail in tails:
            for _ in range(2):
                with pytest.raises(CredentialMaterialRefused) as caught:
                    require_no_credential_material({"api_key": "literal", "later": tail}, surface="memo-control")
                assert caught.value.finding.category == "credential_field"
        assert memo.cached_entry_count == 0
    assert len(work) == len(tails) * 2


def test_retention_field_mapper_finding_preserves_original_options_and_skips_validation(monkeypatch):
    from elspeth.plugins.transforms.field_mapper import FieldMapperConfig

    original = guard.find_credential_material
    work = []

    def detect(value, policy):
        work.append(policy)
        return original(value, policy)

    def forbidden(*args, **kwargs):
        raise AssertionError("credential finding must return before model validation")

    monkeypatch.setattr(guard, "find_credential_material", detect)
    monkeypatch.setattr(FieldMapperConfig, "model_validate", forbidden)
    options = _field_mapper_node({"customer_key": "customer_id"})["options"] | {"tail": {"api_key": "literal"}}
    with retention_stage_memo_scope():
        assert guard.field_mapper_credential_options_projection(options) is options
        assert guard.field_mapper_credential_options_projection(options) is options
    assert len(work) == 1 and work[0] is not guard.DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
    assert work[0] == guard.DEFAULT_CREDENTIAL_TRAVERSAL_POLICY


@pytest.mark.parametrize("whole_refusal", [True, False], ids=["whole-state", "first-component"])
def test_retention_state_scan_does_not_resolve_later_policies_after_refusal(monkeypatch, whole_refusal):
    state_data = _field_mapper_state().to_dict()
    state_data["sources"] = {
        "source": {
            "plugin": "csv",
            "options": {"api_key" if whole_refusal else "extra": "literal"},
            "on_success": "rows",
            "on_validation_failure": "discard",
        }
    }
    state = CompositionState.from_dict(state_data)
    policies = []

    def policy(component, plugin):
        policies.append((component, plugin))
        if component != "source":
            raise AssertionError("later component policy must not be resolved")
        return frozenset({"extra"})

    monkeypatch.setattr(guard, "allowed_secret_ref_fields", policy)
    with retention_stage_memo_scope():
        for _ in range(2):
            with pytest.raises(CredentialMaterialRefused):
                require_no_credential_material_in_state(state, surface="memo-control")
    assert policies == ([] if whole_refusal else [("source", "csv")] * 2)
